#!/usr/bin/env python3
"""星钥官网 (starkeeper.chenxiuniverse.top) DNS 自动化：健康探测 / 优选 IP 轮换 / 容灾切换。

架构:
  - default 线路: A records → CF 优选 IP ×N (主，大陆直连低延迟)
  - default 线路: CNAME → starkeeper-bpw.pages.dev (容灾 backup，官方解析兜底)
  - oversea 线路: CNAME → starkeeper-bpw.pages.dev (固定不动)

用法:
  python3 starkeeper_dns.py status             查看当前 DNS 记录与状态文件
  python3 starkeeper_dns.py check              探测当前 default 线路健康 (exit 0=healthy, 1=fail)
  python3 starkeeper_dns.py rotate             候选池测可达性，A 记录更新为可达 IP 集合 (仅 primary)
  python3 starkeeper_dns.py backup             default A → 官方 CNAME (切容灾)
  python3 starkeeper_dns.py restore            default CNAME → A (候选池 IP，恢复优选直连)

环境变量: ALI_KEY_ID / ALI_KEY_SECRET (与主站 failover-dns.py 同套 secrets)

状态文件: monitoring/starkeeper_state.json — {mode, fails, healthy_streak, ips}
背景: 复用主站容灾的"权威 API 直连探测"模式 (防递归缓存失明)。

安全约定 (2026-09-11 加固): A 记录变更一律"先建后删"(converge_a_records)，
  禁止"删光再建"——中途失败会留下**零记录**的 default 线路，国内解析直接失败且无法自愈
  （与 2026-06-27 P0 自伤事故同一失败模式）。backup 因 A/CNAME 同名冲突无法先建后删，
  改为"删除后立即补建 + 补建失败则回滚写回 A 记录"。
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

DOMAIN = "chenxiuniverse.top"
RR = "starkeeper"
PAGES_HOST = "starkeeper-bpw.pages.dev"
ENDPOINT = "https://alidns.cn-hangzhou.aliyuncs.com/"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "starkeeper_state.json")

# 候选池: 2026-08-18 大陆本机实测低延迟 CF 边缘 IP + 主站 health 子域在用 IP
CANDIDATES = [
    "172.64.52.95",    # 实测 0.24s
    "162.159.39.168",  # 实测 0.45s
    "162.159.44.17",   # 实测 0.77s
    "172.66.47.89",    # 实测 0.84s
    "104.17.135.151",  # health 在用
    "104.16.89.66",    # health 在用
    "172.64.35.210",   # 实测 2.0s
]
KEEP_N = 2  # A 记录保留条数
TTL = 600   # 阿里云标准版 TTL 下限（与主站 failover-dns.py 同值）


def enc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~")


def call(action: str, **extra) -> dict:
    key_id = os.environ.get("ALI_KEY_ID", "")
    key_secret = os.environ.get("ALI_KEY_SECRET", "")
    params = {
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Action": action,
    }
    params.update(extra)
    qs = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + urllib.parse.quote(qs, safe="~")
    sig = hmac.new((key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    qs += "&Signature=" + enc(base64.b64encode(sig).decode())
    with urllib.request.urlopen(ENDPOINT + "?" + qs, timeout=10) as resp:
        return json.load(resp)


def list_records() -> list:
    d = call("DescribeDomainRecords", DomainName=DOMAIN)
    out = []
    for r in (d.get("DomainRecords") or {}).get("Record", []):
        if r.get("RR") == RR:
            out.append({"id": r["RecordId"], "type": r["Type"], "value": r["Value"], "line": r.get("Line", "default")})
    return out


def add_record(rtype: str, value: str, line: str = "default") -> None:
    call("AddDomainRecord", DomainName=DOMAIN, RR=RR, Type=rtype, Value=value, Line=line)


def delete_record(record_id: str) -> None:
    call("DeleteDomainRecord", RecordId=record_id)


def probe(ip: str) -> tuple:
    """直连探测指定 IP 的 HTTPS 可达性，返回 (ok, latency)。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}|%{time_total}",
             "--resolve", f"{RR}.{DOMAIN}:443:{ip}", "--connect-timeout", "8", "--max-time", "12",
             f"https://{RR}.{DOMAIN}/"],
            capture_output=True, text=True, timeout=20)
        code, lat = (r.stdout.strip().split("|") + ["0"])[:2]
        ok = code.isdigit() and 200 <= int(code) < 500
        return ok, float(lat)
    except Exception:
        return False, 99.0


def read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"mode": "primary", "fails": 0, "healthy_streak": 0, "ips": []}


def write_state(s: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f)


def current_default_ip_records() -> list:
    return [r for r in list_records() if r["type"] == "A" and r["line"] == "default"]


def converge_a_records(target_ips: list) -> bool:
    """把 default 线路的 A 记录收敛为 target_ips：先补齐/更新，最后删多余。

    "先建后删"是硬要求：A → A 的增改不会冲突，因此最坏情况只是**多出几条同样可用的 IP**；
    而旧实现"删光 A 再逐条加"一旦中途失败（限流/网络抖动/进程被杀），default 线路会变成
    零记录——国内用户直接解析失败，且下一轮推导为 empty 后不会自愈（需人工介入）。
    """
    current = current_default_ip_records()
    if sorted(r["value"] for r in current) == sorted(target_ips):
        return False
    for i, ip in enumerate(target_ips):
        if i < len(current):
            if current[i]["value"] != ip:
                call("UpdateDomainRecord", RecordId=current[i]["id"], RR=RR,
                     Type="A", Value=ip, Line="default", TTL=TTL)
        else:
            add_record("A", ip, "default")
    for extra in current[len(target_ips):]:
        delete_record(extra["id"])
    return True


def cmd_status() -> int:
    for r in list_records():
        print(f"  {r['line']:8s} {r['type']:6s} -> {r['value']}")
    print("state:", json.dumps(read_state(), ensure_ascii=False))
    return 0


def cmd_check() -> int:
    """探测当前 default 线路入口。A 记录逐个直连；CNAME 走普通 https 探测。"""
    records = list_records()
    a_records = [r for r in records if r["type"] == "A" and r["line"] == "default"]
    cname = [r for r in records if r["type"] == "CNAME" and r["line"] == "default"]
    if a_records:
        ok_all = True
        for r in a_records:
            ok, lat = probe(r["value"])
            print(f"  A {r['value']}: {'OK' if ok else 'FAIL'} ({lat:.2f}s)")
            ok_all = ok_all and ok
        return 0 if ok_all else 1
    if cname:
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-A", "starkeeper-monitor/1.0",
                 "--connect-timeout", "8", "--max-time", "12", f"https://{RR}.{DOMAIN}/"],
                capture_output=True, text=True, timeout=20)
            code = r.stdout.strip()
            ok = code.isdigit() and 200 <= int(code) < 500
            print(f"  CNAME(官方): HTTP {code} {'OK' if ok else 'FAIL'}")
            return 0 if ok else 1
        except Exception:
            return 1
    print("  !! 无 default 线路记录")
    return 1


def cmd_rotate() -> int:
    """候选池测可达性，default A 记录更新为可达 IP 集合。仅在 primary 语义下调用。"""
    ok_ips = [(ip, lat) for ip, lat in (probe(ip) for ip in CANDIDATES) if ip]
    ok_ips.sort(key=lambda x: x[1])
    keep = [ip for ip, _ in ok_ips[:KEEP_N]]
    if not keep:
        print("  !! 候选池全部不可达，保持现状")
        return 1
    if converge_a_records(keep):
        print("  rotate: default A ->", ", ".join(keep))
    else:
        print("  rotate: 与当前记录一致，无变更")
    return 0


def cmd_backup() -> int:
    """容灾: default A 记录 → 官方 CNAME (pages.dev 解析兜底)。

    顺序（2026-09-11 安全化，避免"零记录"线路）：
      1. 先直接补建 CNAME —— 若阿里云允许 A/CNAME 并存，这一步即完成切换，**全程无空窗**；
         CNAME 生效后再清理旧 A 记录（清理失败只是并存，解析仍走 CNAME，不影响可用性）。
      2. 若补建被拒（同名冲突）→ 退回"删 A → 立即补 CNAME"；这一路径天生存在一次 API 调用的
         空窗，因此补建失败时必须**回滚写回原 A 记录**，绝不留下零记录的 default 线路。
    """
    old_records = current_default_ip_records()
    old_values = [r["value"] for r in old_records]

    if old_values:
        try:
            add_record("CNAME", PAGES_HOST, "default")
        except Exception as e:
            print(f"  · 直接补建 CNAME 被拒（{e}）→ 退回先删 A 再补建", file=sys.stderr)
        else:
            for r in old_records:
                try:
                    delete_record(r["id"])
                except Exception as e:
                    print(f"  ⚠ 清理旧 A 记录失败: {e}（CNAME 已生效，A/CNAME 并存不影响解析）",
                          file=sys.stderr)
            print("  backup: default -> CNAME", PAGES_HOST)
            return 0

    # 回退路径：删 A → 立即补 CNAME（这一路径天生存在一次 API 调用的空窗，失败必须回滚）
    for r in old_records:
        try:
            delete_record(r["id"])
        except Exception as e:
            print(f"  ⚠ 删除旧 A 记录失败: {e}（继续尝试补建 CNAME）", file=sys.stderr)
    try:
        add_record("CNAME", PAGES_HOST, "default")
    except Exception as e:
        print(f"  !! 补建 CNAME 失败: {e} — 正在回滚写回原 A 记录", file=sys.stderr)
        rolled_back = True
        for ip in old_values:
            try:
                add_record("A", ip, "default")
            except Exception as e2:
                rolled_back = False
                print(f"  !! 回滚 A 记录 {ip} 也失败: {e2}", file=sys.stderr)
        if not rolled_back:
            print("  !! default 线路可能处于无记录状态，需人工介入！", file=sys.stderr)
        return 1
    print("  backup: default -> CNAME", PAGES_HOST)
    return 0


def cmd_restore() -> int:
    """恢复: default CNAME → A 记录 (候选池 IP)。restore 前先 rotate 验证可达性。"""
    ok = cmd_rotate()
    if ok != 0:
        return 1
    for r in list_records():
        if r["type"] == "CNAME" and r["line"] == "default":
            delete_record(r["id"])
    print("  restore: default -> A (优选 IP)")
    return 0


def cmd_candidates() -> int:
    """输出候选池为 CloudflareST 格式 CSV（供 china_speed_test.py 做 ITDog 境内测速）。"""
    print("IP地址")
    for ip in CANDIDATES:
        print(ip)
    return 0


def cmd_set_ips(ips_csv: str) -> int:
    """直接设置 default A 记录为指定 IP 列表（由 ITDog 境内测速结果驱动，跳过 runner 探测）。"""
    ips = [ip.strip() for ip in ips_csv.split(",") if ip.strip()]
    if not ips:
        print("  !! 空 IP 列表")
        return 1
    if converge_a_records(ips):
        print("  set-ips: default A ->", ", ".join(ips))
    else:
        print("  set-ips: 与当前记录一致，无变更")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "status":
        sys.exit(cmd_status())
    if action == "check":
        sys.exit(cmd_check())
    if action == "rotate":
        sys.exit(cmd_rotate())
    if action == "backup":
        sys.exit(cmd_backup())
    if action == "restore":
        sys.exit(cmd_restore())
    if action == "candidates":
        sys.exit(cmd_candidates())
    if action == "set-ips":
        sys.exit(cmd_set_ips(sys.argv[2] if len(sys.argv) > 2 else ""))
    print("unknown action:", action)
    sys.exit(2)

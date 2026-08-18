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
    for r in current_default_ip_records():
        delete_record(r["id"])
    for ip in keep:
        add_record("A", ip, "default")
    print("  rotate: default A ->", ", ".join(keep))
    return 0


def cmd_backup() -> int:
    """容灾: default A 记录 → 官方 CNAME (pages.dev 解析兜底)。"""
    for r in current_default_ip_records():
        delete_record(r["id"])
    add_record("CNAME", PAGES_HOST, "default")
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
    print("unknown action:", action)
    sys.exit(2)

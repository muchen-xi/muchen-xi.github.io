#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECS 机房节点 · 优选 IP 主节点（2026-10-25）

职责（每 3 小时一轮，systemd timer 触发）：
  1. 读 GitHub 流水线发布的 _ip-opt-gh 会签记录（ITDog 测速 + 境外测速结果）；
  2. 用 CloudflareST 在**本机房**做独立扫描，"机房自身环境"作为第一视角；
  3. 对候选 IP 逐个做 HTTPS 健康探测（带 SNI，走 --resolve 直连该 IP）；
  4. 合并打分：机房延迟为主 + ITDog 延迟为辅，得到最终排序；
  5. 写阿里云 DNS default 线路 A 记录（www / health / history / pimanager / starkeeper）；
  6. 把本轮结果写进 _ip-opt-ecs 会签记录，供 GitHub 侧与人工查看。

安全与容灾：
  - 容灾备份态（权威 A 记录全属备站 IP / starkeeper 是 CNAME）时只观测不动 DNS；
  - MODE=publish 时只写会签记录，不碰 A 记录（用于首次上线验证）；
  - 任何写 DNS 失败只告警，不阻断下一轮。

配置 /opt/ipopt/env（600）：
  ALI_KEY_ID / ALI_KEY_SECRET   阿里云解析 AK（可复用 cf-dns-auto）
  MODE=apply|publish            默认 publish（灰度验证用）
  N_A=5                         每条线路保留几个 A 记录
  DRY_RUN=0|1                   只打印不写
"""

from __future__ import annotations

import calendar
import csv
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alidns_rpc as dns  # noqa: E402

DOMAIN = os.environ.get("IPOPT_DOMAIN", "chenxiuniverse.top")
ENV_FILE = os.environ.get("IPOPT_ENV", "/opt/ipopt/env")
LOG = os.environ.get("IPOPT_LOG", "/var/log/ipopt.log")
CFST = os.environ.get("IPOPT_CFST", "/opt/cfst/CloudflareST")
SCAN_CSV = "/tmp/ipopt_scan.csv"
SYSTEMD_ENV = "/etc/morning-push.env"

BACKUP_SET = {"76.76.21.21",
              "185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"}
DEFAULT_TARGETS = ["www", "health", "history", "pimanager", "starkeeper"]
PROBE_HOST = "www." + DOMAIN
BOARD_GH = "_ip-opt-gh"
BOARD_SELF = "_ip-opt-ecs"
PROBE_TIMEOUT = 8
SCAN_TOP = 20


# ─────────────────────────── 基础设施 ───────────────────────────

def load_env_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip("\"'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG) > 2_000_000:
            with open(LOG, encoding="utf-8") as f:
                tail = f.readlines()[-3000:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except OSError:
        pass


def notify(title, desp):
    key = None
    try:
        with open(SYSTEMD_ENV, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("SENDKEY=") and not line.strip().startswith("#"):
                    key = line.split("=", 1)[1].strip().strip("\"'")
                    break
    except OSError:
        return
    if not key:
        return
    try:
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
        req = urllib.request.Request("https://sctapi.ftqq.com/%s.send" % key, data=data,
                                     headers={"User-Agent": "ecs-ipopt/1.0"})
        urllib.request.urlopen(req, timeout=10).read()
        log("NOTIFY %s" % title)
    except Exception as e:  # 通知失败绝不阻断主流程
        log("NOTIFY-FAIL %s: %s" % (type(e).__name__, e))


# ─────────────────────────── 会签板 ───────────────────────────

def _dig_txt(name):
    """无 AK 时用公开 DNS 读会签记录（_dr-* / _ip-opt-* 都是公开 TXT）。"""
    for rs in ("223.5.5.5", "119.29.29.29"):
        try:
            p = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "TXT", name, "@" + rs],
                               capture_output=True, text=True, timeout=8)
            out = (p.stdout or "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def read_board(name):
    raw = ""
    try:
        raw = dns.get_txt(name, DOMAIN) or ""
    except (Exception, SystemExit):
        raw = ""
    if not raw:
        raw = _dig_txt(name)
    if not raw:
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    raw = raw.replace('\\"', '"').replace("\\\\", "\\")
    try:
        return json.loads(raw)
    except ValueError:
        log("⚠ 会签板 %s 解析失败: %r" % (name, raw[:120]))
        return None


def publish_board(payload):
    """_ip-opt-ecs ≤255 字节；超长按 IP 数从尾部裁剪。"""
    body = dict(payload)
    text = ""
    for keep in (5, 4, 3, 2, 1):
        body["ips"] = payload["ips"][:keep]
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) <= 255:
            break
    try:
        log("会签板 %s: %s" % (BOARD_SELF, dns.set_txt(BOARD_SELF, text, DOMAIN)))
    except (Exception, SystemExit) as e:
        log("⚠ 写会签板失败（无 AK 或权限不足）: %s" % e)


# ─────────────────────────── 测量 ───────────────────────────

def cfst_scan():
    """机房本地扫描：CloudflareST 延迟测速（禁用下载测速，3Mbps 出口不测吞吐）。"""
    if not os.path.exists(CFST):
        log("⚠ 未找到 CloudflareST: %s" % CFST)
        return []
    cmd = [CFST, "-n", "60", "-t", "2", "-tl", "300", "-dd",
           "-url", "https://%s/" % PROBE_HOST, "-o", SCAN_CSV, "-p", "0"]
    t0 = time.time()
    try:
        subprocess.run(cmd, cwd=os.path.dirname(CFST), capture_output=True,
                       text=True, timeout=240)
    except subprocess.TimeoutExpired:
        log("⚠ CloudflareST 超时（>240s）")
        return []
    out = []
    try:
        with open(SCAN_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ip = row.get("IP 地址") or row.get("IP地址") or row.get("IP")
                try:
                    ms = float(row.get("平均延迟") or 0)
                except (TypeError, ValueError):
                    ms = 0.0
                if ip and 0 < ms <= 300:
                    out.append((ip, ms))
    except OSError:
        log("⚠ 读不到 CloudflareST 结果")
    log("机房扫描完成: %d 个可用 IP（%.0fs）" % (len(out), time.time() - t0))
    return out[:SCAN_TOP]


def current_dns_ips(rr="www"):
    """现网 default 线路 IP：既当基线候选（避免新 IP 不如旧 IP），也用于人工比对。"""
    try:
        p = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "A",
                            "%s.%s" % (rr, DOMAIN), "@223.5.5.5"],
                           capture_output=True, text=True, timeout=8)
        return [ln.strip() for ln in (p.stdout or "").split() if ln.strip().count(".") == 3]
    except Exception:
        return []


def probe_ip(ip, samples=3):
    """从本机房直连该 IP（SNI=PROBE_HOST）测 HTTPS 建连耗时，返回 (ok_count, median_ms)。

    必须带浏览器 UA：Cloudflare 对裸 curl UA 直接回 403（实测），
    与 health_check.py 的判定口径保持一致。
    """
    times, ok = [], 0
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    for _ in range(samples):
        try:
            p = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_appconnect}",
                 "--max-time", str(PROBE_TIMEOUT), "-A", ua, "--resolve",
                 "%s:443:%s" % (PROBE_HOST, ip), "https://%s/" % PROBE_HOST],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT + 3)
            parts = (p.stdout or "").strip().split()
            if len(parts) == 2 and parts[0].isdigit() and 200 <= int(parts[0]) < 400:
                ok += 1
                times.append(float(parts[1]) * 1000)
        except Exception:
            pass
    med = statistics.median(times) if times else 0.0
    return ok, med


# ─────────────────────────── 容灾守卫 ───────────────────────────

def www_is_backup():
    recs = [r for r in dns.describe_records(DOMAIN, "www", "A")
            if r.get("RR") == "www" and str(r.get("Type", "")).upper() == "A"
            and (r.get("Line") or "default") == "default"]
    vals = {r.get("Value") for r in recs}
    return bool(vals) and vals.issubset(BACKUP_SET)


def starkeeper_is_backup():
    recs = dns.describe_records(DOMAIN, "starkeeper")
    return any(str(r.get("Type", "")).upper() == "CNAME" for r in recs)


# ─────────────────────────── 主流程 ───────────────────────────

def main():
    load_env_file(ENV_FILE)
    mode = os.environ.get("MODE", "publish").strip().lower()
    dry_run = os.environ.get("DRY_RUN", "0").strip() in ("1", "true", "yes")
    n_a = int(os.environ.get("N_A", "5"))

    gh = read_board(BOARD_GH) or {}
    gh_age = None
    try:
        gh_age = int(time.time() - calendar.timegm(time.strptime(gh["ts"], "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        pass
    itdog = {}
    for item in gh.get("itdog", []) or []:
        ip, _, ms = str(item).partition(":")
        try:
            itdog[ip] = float(ms)
        except ValueError:
            pass
    log("GH 会签: itdog=%d 个, ove=%d 个, age=%s" % (len(itdog), len(gh.get("ove", []) or []),
                                                  "?" if gh_age is None else "%ss" % gh_age))

    candidates = {}
    for ip, ms in cfst_scan():
        candidates[ip] = ms
    for ip, ms in itdog.items():
        candidates.setdefault(ip, None)      # GH 给的候选即使本地没扫到也测一遍
    for ip in current_dns_ips():
        candidates.setdefault(ip, None)      # 现网 IP 作基线，防止"新不如旧"

    measured = {}
    for ip in list(candidates)[:40]:
        ok, med = probe_ip(ip)
        if ok < 2 or med <= 0:
            continue
        local = med
        idms = itdog.get(ip)
        score = local if idms is None else 0.65 * local + 0.35 * idms
        measured[ip] = {"local": round(local, 1), "itdog": idms, "score": round(score, 1)}
    ranked = sorted(measured.items(), key=lambda kv: kv[1]["score"])
    log("健康探测通过 %d 个；Top: %s" % (len(ranked), ", ".join(
        "%s=%.0fms" % (ip, v["score"]) for ip, v in ranked[:5])))

    if not ranked:
        notify("优选IP: 本轮无可用 IP", "机房扫描与健康探测均未得到可用 IP，DNS 未改动。")
        return 1

    chosen = [ip for ip, _ in ranked[:n_a]]
    www_backup = False
    try:
        www_backup = www_is_backup()
    except (Exception, SystemExit) as e:
        log("⚠ 容灾状态推导失败（按非备份处理）: %s" % e)

    publish_board({
        "v": 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "who": "ecs",
        "mode": mode,
        "ips": ["%s:%.0f" % (ip, v["score"]) for ip, v in ranked[:n_a]],
        "gh_age": gh_age,
    })

    if mode != "apply":
        log("MODE=%s：只观测不写 A 记录" % mode)
        return 0
    if www_backup:
        log("⚠ 权威记录显示 www 处于容灾备站状态 — 本轮不改 DNS")
        notify("优选IP: 跳过（容灾备份态）", "检测到 www 处于备站状态，机房节点不改 DNS。")
        return 0

    changed = []
    for rr in DEFAULT_TARGETS:
        if rr == "starkeeper":
            try:
                if starkeeper_is_backup():
                    log("⚠ starkeeper 处于 CNAME 备站态，跳过")
                    continue
            except (Exception, SystemExit):
                pass
        try:
            if dry_run:
                log("[DRY] %s default → %s" % (rr, chosen))
                continue
            if dns.set_a_records(rr, "default", chosen, DOMAIN):
                changed.append(rr)
                log("✅ %s default 已更新 → %s" % (rr, chosen))
        except (Exception, SystemExit) as e:
            log("❌ %s 更新失败: %s" % (rr, e))
            notify("优选IP: %s 更新失败" % rr, "%s\n\n%s" % (rr, e))

    if changed:
        notify("优选IP: 机房节点已更新 DNS",
               "更新: %s\nIP: %s\n\n评分: %s" % (
                   ",".join(changed), ", ".join(chosen),
                   ", ".join("%s=%.0f" % (ip, v["score"]) for ip, v in ranked[:n_a])))
    else:
        log("DNS 无变化")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        log("❌ 未捕获异常: %s: %s" % (type(exc).__name__, exc))
        notify("优选IP: 机房节点异常", "%s: %s" % (type(exc).__name__, exc))
        sys.exit(2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECS 探针 v3 —— 报警口径收敛为四类（2026-10-25 用户定稿）

  ① 服务器故障      — 关键 systemd 单元掉线 / 磁盘吃满 / 树莓派观察者心跳停摆 / 优选任务异常
  ② CVE 高危漏洞    — 每日一次的 Ubuntu 安全更新检查（-security 源）
  ③ 不在计划内的 IP 切换 — 权威 DNS 与机房主节点写入的期望值不一致，且不在优化窗口内
  ④ 主站宕机        — www.chenxiuniverse.top 连续 2 次不可用（裸域不在监控范围）

其余一律只写日志不推微信：health / history / starkeeper 的可用性、会签板分歧、
优选任务"已更新 DNS"之类的成功消息。

纯标准库 + dig / systemctl / apt-get，5 分钟一轮（timer 触发）。
"""

import calendar
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = "/var/log/probe.log"
STATE = "/opt/probe/state_v3.json"
EXPECTED = "/opt/ipopt/expected.json"      # 机房主节点每轮写：{"ts":epoch,"www":[...]}
PUSH_ENV = "/etc/morning-push.env"

MAIN_URL = "https://www.chenxiuniverse.top/"
PASSIVE_URLS = [                            # 只记录不告警
    ("starkeeper", "https://starkeeper.chenxiuniverse.top/"),
    ("health", "https://health.chenxiuniverse.top/"),
    ("history", "https://history.chenxiuniverse.top/"),
]
UNITS = ["mc", "1panel", "wg-quick@wg0", "microbin", "syncthing@syncthing",
         "probe.timer", "ipopt.timer"]
BACKUP_SET = {"76.76.21.21",
              "185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"}
PI_RECORD = "_dr-pi.chenxiuniverse.top"
PI_STALE = 1800              # 契约：pi 300s 写一次；30 分钟没动=观察者停了
OPT_WINDOW = 1800            # 优化窗口：期望值写入后 30 分钟内 DNS 变动不算"计划外"
TIMEOUT = 12
CVE_CHECK_INTERVAL = 86400

ctx = ssl.create_default_context()


# ────────────────────────── 基础 ──────────────────────────

def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG) > 5_000_000:
            with open(LOG, encoding="utf-8") as f:
                tail = f.readlines()[-5000:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except OSError:
        print(line, file=sys.stderr)


def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError:
        pass


def sendkey():
    try:
        with open(PUSH_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SENDKEY=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip().strip("\"'") or None
    except OSError:
        return None
    return None


def notify(kind, title, body):
    """kind 只能是四类之一；这里再兜一层，防止以后有人随手加告警。"""
    if kind not in ("服务器故障", "CVE高危漏洞", "不在计划内的IP切换", "主站宕机"):
        log("SUPPRESS-NOTIFY [%s] %s" % (kind, title))
        return
    key = sendkey()
    if not key:
        log("NOTIFY-FAIL no SENDKEY")
        return
    try:
        data = urllib.parse.urlencode({"title": "%s: %s" % (kind, title),
                                       "desp": body}).encode()
        req = urllib.request.Request("https://sctapi.ftqq.com/%s.send" % key, data=data,
                                     headers={"User-Agent": "ecs-probe/3.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            log("NOTIFY %d [%s] %s" % (r.status, kind, title))
    except Exception as e:
        log("NOTIFY-FAIL %s: %s" % (type(e).__name__, e))


# ────────────────────────── HTTP ──────────────────────────

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def http_check(url):
    for attempt in (1, 2):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                r.read(65536)
                return r.status, (time.monotonic() - t0) * 1000, None
        except urllib.error.HTTPError as e:
            return e.code, (time.monotonic() - t0) * 1000, None
        except Exception as e:
            if attempt == 1:
                time.sleep(2)
                continue
            return None, (time.monotonic() - t0) * 1000, "%s: %s" % (type(e).__name__, e)


def ok(code):
    return code is not None and 200 <= code <= 399


# ────────────────────────── ① 服务器故障 ──────────────────────────

def check_units(st, body):
    down = []
    for u in UNITS:
        state = sh(["systemctl", "is-active", u])
        if state != "active":
            down.append("%s=%s" % (u, state or "unknown"))
    prev = set(st.get("units_down", []))
    cur = set(down)
    st["units_down"] = sorted(cur)
    if cur:
        log("UNIT-DOWN %s" % ", ".join(sorted(cur)))
        # 新掉线才告警（避免每轮重复刷屏），恢复时也报一次
        new = cur - prev
        if new and not prev:
            body.append("掉线单元: " + ", ".join(sorted(cur)))
            return "服务器故障", "关键服务掉线"
    elif prev:
        log("UNIT-RECOVER %s" % ", ".join(sorted(prev)))
        return "服务器故障", "服务已恢复"
    return None


def check_disk(st, body):
    out = sh(["df", "-P", "/"])
    pct = 0
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            try:
                pct = int(parts[4].rstrip("%"))
            except ValueError:
                pass
    st["disk_pct"] = pct
    high = st.get("disk_high", 0)
    if pct >= 85:
        high += 1
        st["disk_high"] = high
        if high == 2:
            body.append("根分区使用率 %d%%" % pct)
            return "服务器故障", "磁盘空间告急"
    else:
        if high >= 2:
            st["disk_high"] = 0
            return "服务器故障", "磁盘空间已恢复"
        st["disk_high"] = 0
    return None


def dig_txt(name, resolver="223.5.5.5"):
    return sh(["dig", "+short", "+time=3", "+tries=1", "TXT", name, "@" + resolver], 10)


def check_pi(st, body):
    raw = dig_txt(PI_RECORD)
    if not raw:
        st["pi_stale"] = st.get("pi_stale", 0) + 1
        if st["pi_stale"] == 2:
            body.append("读不到树莓派会签记录 %s" % PI_RECORD)
            return "服务器故障", "树莓派观察者失联"
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    raw = raw.replace('\\"', '"').replace("\\\\", "\\")
    try:
        rec = json.loads(raw)
        ts = calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        st["pi_stale"] = st.get("pi_stale", 0) + 1
        return None
    age = int(time.time() - ts)
    if age > PI_STALE:
        st["pi_stale"] = st.get("pi_stale", 0) + 1
        if st["pi_stale"] == 2:
            body.append("树莓派心跳 %d 秒未更新" % age)
            return "服务器故障", "树莓派观察者停止心跳"
    else:
        if st.get("pi_stale", 0) >= 2:
            st["pi_stale"] = 0
            return "服务器故障", "树莓派观察者已恢复"
        st["pi_stale"] = 0
    return None


# ────────────────────────── ② CVE 高危漏洞 ──────────────────────────

def check_cve(st, body):
    last = st.get("cve_ts", 0)
    if time.time() - last < CVE_CHECK_INTERVAL:
        return None
    st["cve_ts"] = int(time.time())
    out = sh("apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null", 120)
    pkgs = []
    for line in out.splitlines():
        if line.startswith("Inst ") and ("-security" in line or "security" in line.lower()):
            m = re.match(r"Inst (\S+) ", line)
            if m:
                pkgs.append(m.group(1))
    prev = set(st.get("cve_pending", []))
    st["cve_pending"] = sorted(pkgs)
    if pkgs and set(pkgs) != prev:
        log("CVE-SECURITY-UPDATES %d: %s" % (len(pkgs), ", ".join(pkgs[:20])))
        body.append("安全源可更新 %d 个包（可能需要重启）:\n%s" %
                    (len(pkgs), "\n".join(pkgs[:20])))
        return "CVE高危漏洞", "检测到安全更新"
    return None


# ────────────────────────── ③ 不在计划内的 IP 切换 ──────────────────────────

def dig_ips(name):
    out = sh(["dig", "+short", "A", name, "@223.5.5.5"], 10)
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip().count(".") == 3})


def load_expected():
    try:
        with open(EXPECTED, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def check_dns_drift(st, body):
    exp = load_expected()
    cur = dig_ips("www.chenxiuniverse.top")
    st["dns_now"] = cur
    if not cur:
        return None                          # 解析失败不判漂移（避免误报）
    backup = bool(cur) and set(cur).issubset(BACKUP_SET)
    if backup:
        st["drift"] = st.get("drift", 0) + 1
        if st["drift"] == 2:
            body.append("www 权威 A 记录全部指向备站 IP: %s" % ", ".join(cur))
            return "不在计划内的IP切换", "www 被切到备站"
        return None
    if not exp:
        return None
    want = sorted(set(exp.get("www", [])))
    if not want:
        return None
    if set(cur) == set(want):
        if st.get("drift", 0) >= 2:
            st["drift"] = 0
            return "不在计划内的IP切换", "DNS 已回到计划内"
        st["drift"] = 0
        return None
    # 期待值是机房主节点写的；写入后 OPT_WINDOW 秒内的差异属于正常传播/更新
    age = time.time() - float(exp.get("ts", 0) or 0)
    if age <= OPT_WINDOW:
        log("DNS-DRIFT-IN-WINDOW cur=%s want=%s" % (cur, want))
        return None
    st["drift"] = st.get("drift", 0) + 1
    log("DNS-DRIFT cur=%s want=%s" % (cur, want))
    if st["drift"] == 2:
        body.append("当前 A: %s\n期望 A: %s\n（期望值由机房主节点 %s 写入）" %
                    (", ".join(cur), ", ".join(want),
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(float(exp.get("ts", 0) or 0)))))
        return "不在计划内的IP切换", "www DNS 与计划不一致"
    return None


# ────────────────────────── 主流程 ──────────────────────────

def main():
    st = load_state()
    results = []          # (kind, title, body)

    # ④ 主站宕机
    code, ms, err = http_check(MAIN_URL)
    good = ok(code)
    st["main_fails"] = 0 if good else st.get("main_fails", 0) + 1
    log("MAIN %s %sms %s" % (code, int(ms), "OK" if good else (err or "BAD")))
    if st["main_fails"] == 2:
        results.append(("主站宕机", "主站不可用",
                        "%s -> %s (%dms) %s" % (MAIN_URL, code if code else "无响应",
                                                int(ms), err or "")))
    elif st["main_fails"] > 2 and st["main_fails"] % 12 == 0:
        results.append(("主站宕机", "主站仍未恢复",
                        "%s 连续 %d 轮异常" % (MAIN_URL, st["main_fails"])))
    elif st["main_fails"] == 0 and st.get("main_prev_fails", 0) >= 2:
        results.append(("主站宕机", "主站已恢复",
                        "%s -> %s (%dms)" % (MAIN_URL, code, int(ms))))
    st["main_prev_fails"] = st["main_fails"]

    # 从属站点：只记录
    for name, url in PASSIVE_URLS:
        c, m, e = http_check(url)
        log("PASSIVE %s %s %sms %s" % (name, c, int(m), "OK" if ok(c) else (e or "BAD")))

    # ① 服务器故障
    body = []
    for fn in (check_units, check_disk, check_pi):
        r = fn(st, body)
        if r:
            results.append((r[0], r[1], "\n".join(body)))
            body = []

    # ② CVE
    body = []
    r = check_cve(st, body)
    if r:
        results.append((r[0], r[1], "\n".join(body)))

    # ③ IP 漂移
    body = []
    r = check_dns_drift(st, body)
    if r:
        results.append((r[0], r[1], "\n".join(body)))

    # 推送（同类合并）
    for kind, title, text in results:
        notify(kind, title, text + "\n\n时间: " + time.strftime("%Y-%m-%d %H:%M:%S"))

    log("HEALTH units_down=%d disk=%d%% pi_stale=%d cve_pending=%d drift=%d main_fails=%d" % (
        len(st.get("units_down", [])), st.get("disk_pct", 0), st.get("pi_stale", 0),
        len(st.get("cve_pending", [])), st.get("drift", 0), st.get("main_fails", 0)))
    save_state(st)


if __name__ == "__main__":
    main()

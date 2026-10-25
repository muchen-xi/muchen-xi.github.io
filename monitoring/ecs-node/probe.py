#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECS 协作探针 v2 —— 站点探测 + 与容灾会签板(_dr-pi/_dr-gh)交叉核对。

定位：ECS 是"国内第三方视角"，不写 DNS、不改任何站点状态，只做：
  1) 四站点 HTTP 探测（连续 2 次异常才告警，恢复发通知）
  2) 读阿里云友好的公开 TXT 会签板，监督树莓派(pi)与云监控(gh)心跳是否新鲜
  3) 交叉核对：对方说健康而 ECS 同目标不健康 / 对方说不健康而 ECS 全健康 → 分歧告警
纯标准库 + dig（不依赖第三方 Python 包）。
"""
import calendar
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = "/var/log/probe.log"
STATE = "/opt/probe/probe_state.txt"
PREV = STATE + ".prev"
PSTATE = "/opt/probe/peer_state.txt"
PPREV = PSTATE + ".prev"

# key, url, 会签板里对应的 lines 字段（None = 对方不探这个目标）
TARGETS = [
    ("www", "https://www.chenxiuniverse.top/", "www.default"),
    ("starkeeper", "https://starkeeper.chenxiuniverse.top/", "starkeeper"),
    ("health", "https://health.chenxiuniverse.top/", "www.default"),
    ("history", "https://history.chenxiuniverse.top/", None),
]
BOARD = {
    "pi": "_dr-pi.chenxiuniverse.top",
    "gh": "_dr-gh.chenxiuniverse.top",
}
# 心跳时效阈值：pi 每 300s 写一次（契约 DR_BOARD_WRITE_SECONDS），1200s 判停摆；
# gh 名义 cron */5，但 GitHub 公共仓计划任务实测被降频到 ~1.5-2.5 小时一次，
# 因此给 4 小时，避免把 GitHub 的调度降频误报成故障。
STALE_BY_PEER = {"pi": 1200, "gh": 14400}
RESOLVERS = ["223.5.5.5", "119.29.29.29"]
TIMEOUT = 10

ctx = ssl.create_default_context()


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG) > 5_000_000:
            with open(LOG, "r", encoding="utf-8") as f:
                tail = f.readlines()[-5000:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except OSError:
        print(line, file=sys.stderr)


def read_map(path):
    try:
        with open(path) as f:
            return {k: int(v) for k, v in
                    (l.split() for l in f.read().splitlines() if l.strip())}
    except (OSError, ValueError):
        return {}


def write_map(path, m, keys):
    try:
        with open(path, "w") as f:
            for k in keys:
                f.write("%s %d\n" % (k, m.get(k, 0)))
    except OSError:
        pass


def get_sendkey():
    try:
        with open("/etc/morning-push.env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SENDKEY=") and not line.startswith("#"):
                    v = line.split("=", 1)[1].strip().strip("\"'")
                    return v or None
    except OSError:
        return None
    return None


def notify(title, desp):
    key = get_sendkey()
    if not key:
        log("NOTIFY-FAIL no SENDKEY")
        return
    try:
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
        req = urllib.request.Request(
            "https://sctapi.ftqq.com/%s.send" % key, data=data,
            headers={"User-Agent": "ecs-probe/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            log("NOTIFY %d %s" % (r.status, title))
    except Exception as e:
        log("NOTIFY-FAIL %s: %s" % (type(e).__name__, e))


def check(url):
    # 失败重试一次：阿里云→Cloudflare 优选 IP 偶发握手/读取超时，
    # 单次抖动不值得计入"连续异常"（告警已要求连续 2 轮，再叠一次重试就能把单轮抖动滤掉大半）。
    code, ms, err = _check_once(url)
    if err or not ok(code):
        time.sleep(2)
        code2, ms2, err2 = _check_once(url)
        if err2 is None and ok(code2):
            log("RETRY-OK %s (首次: %s)" % (url, err or code))
            return code2, ms + ms2 + 2000, None
        return code2, ms + ms2 + 2000, err2
    return code, ms, err


def _check_once(url):
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ecs-probe/2.0)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            ms = (time.monotonic() - t0) * 1000
            r.read(65536)
            return r.status, ms, None
    except urllib.error.HTTPError as e:
        ms = (time.monotonic() - t0) * 1000
        return e.code, ms, None
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return None, ms, "%s: %s" % (type(e).__name__, e)


def ok(code):
    return code is not None and 200 <= code <= 399


def dig_txt(name):
    """公开 TXT 查询（阿里云云解析的 _dr-* 会签板是公开记录）。返回原始字符串或 None。"""
    for rs in RESOLVERS:
        try:
            p = subprocess.run(
                ["dig", "+short", "+time=3", "+tries=1", "TXT", name, "@" + rs],
                capture_output=True, text=True, timeout=8)
            out = (p.stdout or "").strip()
            if out:
                # 去掉首尾引号 + 反转义（阿里云 TXT 往返会转义内层双引号）
                s = out
                if s.startswith('"') and s.endswith('"'):
                    s = s[1:-1]
                return s.replace('\\"', '"').replace('\\\\', "\\")
        except Exception as e:
            log("DIG-ERROR %s @%s %s: %s" % (name, rs, type(e).__name__, e))
    return None


def board_read(who):
    """读会签板并返回 dict；不存在/解析失败返回 None。"""
    raw = dig_txt(BOARD[who])
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
        if not isinstance(rec, dict):
            return None
        return rec
    except ValueError:
        log("BOARD-BAD %s %r" % (who, raw[:120]))
        return None


def parse_ts(rec):
    try:
        return calendar.timegm(time.strptime(rec.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def main():
    st = read_map(STATE)
    prev = read_map(PREV)
    pst = read_map(PSTATE)
    pprev = read_map(PPREV)

    lines = []
    own = {}   # key -> (ok, code_or_none, ms, err)
    worst = []  # 用于汇总告警正文

    for key, url, peerkey in TARGETS:
        code, ms, err = check(url)
        good = ok(code)
        own[key] = (good, code, ms, err)
        if err:
            log("%s ERROR %dms %s" % (url, ms, err))
            lines.append("%s -> 异常: %s (%dms)" % (key, err, int(ms)))
        else:
            verdict = "OK" if good else "BAD"
            log("%s %s %dms %s" % (url, code, ms, verdict))
            lines.append("%s -> %s (%dms) %s" % (key, code, int(ms), "正常" if good else "异常"))
        st[key] = 0 if good else st.get(key, 0) + 1
        if not good:
            worst.append(key)

    # ---- 会签板：心跳新鲜度 + 交叉核对 ----
    peer_lines = []
    diverge_alert = False
    for who in BOARD:
        rec = board_read(who)
        k_stale = who + "_stale"
        k_div = who + "_div"
        if rec is None:
            pst[k_stale] = pst.get(k_stale, 0) + 1
            peer_lines.append("%s: 读不到会签板" % who)
            log("PEER %s absent" % who)
            if pst.get(k_stale) == 2:
                notify("站点探针: 会签板异常", "\n\n".join(peer_lines + lines) +
                       "\n\n连续 2 轮读不到会签板记录")
            continue

        ts = parse_ts(rec)
        age = None if ts is None else int(time.time() - ts)
        verdict = str(rec.get("verdict", "unknown"))
        mode = str(rec.get("mode", "?"))
        stale_max = STALE_BY_PEER.get(who, 1200)
        stale = age is None or age > stale_max
        peer_lines.append("%s: verdict=%s mode=%s age=%ss" %
                          (who, verdict, mode, "?" if age is None else age))
        log("PEER %s verdict=%s mode=%s age=%s stale=%s" %
            (who, verdict, mode, age, stale))

        if stale:
            pst[k_stale] = pst.get(k_stale, 0) + 1
            if pst.get(k_stale) == 2:
                notify("站点探针: 观察者心跳停摆",
                       "\n\n".join(peer_lines + lines) +
                       "\n\n%s 会签记录超过 %d 秒未更新（连续 2 轮）" % (who, stale_max))
        else:
            if pst.get(k_stale, 0) >= 2:
                notify("站点探针: 观察者心跳恢复", "%s 会签记录已恢复更新（age=%ss）" % (who, age))
            pst[k_stale] = 0

        # 交叉核对：只看双方都探的目标
        peer_h = peer_u = None
        if not stale:
            peer_h = peer_u = 0
            plines = rec.get("lines") or {}
            for key, _url, peerkey in TARGETS:
                if not peerkey or peerkey not in plines:
                    continue
                v = str(plines.get(peerkey))
                if v.startswith(("2", "3", "4")):
                    peer_h += 1
                else:
                    peer_u += 1
            # 对方健康 + ECS 全健康 → 一致；对方不健康 + ECS 全健康 → 分歧（可能是线路视角差异）
            if verdict == "unhealthy" and all(g for g, *_ in own.values()):
                pst[k_div] = pst.get(k_div, 0) + 1
                if pst.get(k_div) == 2:
                    diverge_alert = True
                    log("PEER %s divergence: peer=unhealthy ecs=healthy" % who)
            else:
                if pst.get(k_div, 0) >= 2:
                    notify("站点探针: 分歧解除", "%s 与 ECS 判定已一致（%s）" % (who, verdict))
                pst[k_div] = 0

    if diverge_alert:
        notify("站点探针: 观察者判定分歧",
               "\n\n".join(peer_lines + lines) +
               "\n\n树莓派/云监控说站点不健康，但 ECS 从国内直连四个目标全部正常；"
               "请勿贸然恢复 DNS，先人工确认（可能是区域性/线路差异）。")

    # ---- 自身探测告警（沿用连续 2 次规则） ----
    for key in (t[0] for t in TARGETS):
        cnt = st.get(key, 0)
        if cnt == 2:
            notify("站点探针: 异常告警",
                   "\n\n".join(lines + peer_lines) + "\n\n连续 %d 次异常: %s" % (cnt, key))
        elif cnt > 2 and cnt % 12 == 0:
            notify("站点探针: 仍在异常", "\n\n".join(lines + peer_lines))
        elif cnt == 0 and prev.get(key, 0) >= 2:
            notify("站点探针: 已恢复", "%s 已恢复正常" % key)

    write_map(STATE, st, [t[0] for t in TARGETS])
    write_map(PREV, st, [t[0] for t in TARGETS])
    pkeys = []
    for who in BOARD:
        pkeys += [who + "_stale", who + "_div"]
    write_map(PSTATE, pst, pkeys)
    write_map(PPREV, pst, pkeys)


if __name__ == "__main__":
    main()

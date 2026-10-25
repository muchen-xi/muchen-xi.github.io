#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECS 服务器日报 / 周报 —— 瑞士国际主义风格（Swiss / International Typographic Style）

内容三段（用户 2026-10-25 指定）：
  01 树莓派观察者状态
  02 过去服务器状态（24h / 7d：巡检轮次、主站可用率、单元掉线、磁盘、内存、负载、备份…）
  03 过去的运维动作（IP 优选改 DNS、MC 备份、容灾切换、四类告警、安全更新）

发信账号沿用树莓派那套（/opt/report/env，主 love.place → 备 QQ 兜底）。
用法：report.py daily|weekly [--dry-run]
"""

import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

ENV_FILE = "/opt/report/env"
LOG = "/var/log/report.log"
HISTORY = "/opt/report/history.jsonl"
PROBE_LOG = "/var/log/probe.log"
IPOPT_LOG = "/var/log/ipopt.log"
BACKUP_LOG = "/var/log/mc-backup.log"

DOMAIN = "chenxiuniverse.top"
PI_RECORD = "_dr-pi." + DOMAIN
SNAP_RECORD = "_dr-snap." + DOMAIN
UNITS = ["mc", "1panel", "wg-quick@wg0", "microbin", "syncthing@syncthing",
         "probe.timer", "ipopt.timer"]


# ───────────────────────── 基础 ─────────────────────────

def load_env(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except OSError:
        pass


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sh(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def dig_txt(name):
    for rs in ("223.5.5.5", "119.29.29.29"):
        out = sh(["dig", "+short", "+time=3", "+tries=1", "TXT", name, "@" + rs], 10)
        if out:
            return out
    return ""


def parse_board(name):
    raw = dig_txt(name)
    if not raw:
        return {}
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    raw = raw.replace('\\"', '"').replace("\\\\", "\\")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def read_log_lines(path, hours):
    """按行首 'YYYY-mm-dd HH:MM:SS' 过滤时间窗口。"""
    since = time.time() - hours * 3600
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                ts = line[:19]
                try:
                    t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                if t >= since:
                    out.append((t, line.rstrip("\n")))
    except OSError:
        pass
    return out


# ───────────────────────── 01 树莓派 ─────────────────────────

def collect_pi():
    rec = parse_board(PI_RECORD)
    if not rec:
        return {"online": False}
    age = None
    try:
        ts = datetime.strptime(rec.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - ts).total_seconds())
    except (ValueError, TypeError):
        pass
    return {
        "online": age is not None and age <= 1800,
        "age": age,
        "verdict": rec.get("verdict", "unknown"),
        "mode": rec.get("mode", "?"),
        "lines": rec.get("lines") or {},
        "temp": rec.get("temp"),
        "up": rec.get("up"),
        "seq": rec.get("seq"),
    }


# ───────────────────────── 02 服务器状态 ─────────────────────────

def collect_server(hours):
    lines = read_log_lines(PROBE_LOG, hours)
    cycles = main_ok = main_fail = passive_fail = units_down = alerts = drift = 0
    disk_max = 0
    for _, ln in lines:
        if " HEALTH " in ln:
            cycles += 1
            for part in ln.split():
                if part.startswith("disk="):
                    try:
                        disk_max = max(disk_max, int(part[5:].rstrip("%")))
                    except ValueError:
                        pass
        elif " MAIN " in ln:
            if " OK" in ln:
                main_ok += 1
            else:
                main_fail += 1
        elif " PASSIVE " in ln and " OK" not in ln:
            passive_fail += 1
        elif "UNIT-DOWN" in ln:
            units_down += 1
        elif "NOTIFY " in ln and "[服务器故障]" in ln or "NOTIFY " in ln and "[主站宕机]" in ln \
                or "NOTIFY " in ln and "[CVE高危漏洞]" in ln or "NOTIFY " in ln and "[不在计划内的IP切换]" in ln:
            alerts += 1
        elif "DNS-DRIFT" in ln:
            drift += 1

    main_total = main_ok + main_fail
    avail = round(100.0 * main_ok / main_total, 3) if main_total else None

    # 当前状态
    unit_states = {u: (sh(["systemctl", "is-active", u]) or "unknown") for u in UNITS}
    unit_ok = sum(1 for v in unit_states.values() if v == "active")
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                mem[k] = int(rest.split()[0]) // 1024
    except OSError:
        pass
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    disk = 0
    for ln in sh(["df", "-P", "/"]).splitlines()[1:]:
        parts = ln.split()
        if len(parts) >= 5:
            try:
                disk = int(parts[4].rstrip("%"))
            except ValueError:
                pass
    sys_up = 0
    try:
        with open("/proc/uptime") as f:
            sys_up = int(float(f.read().split()[0]))
    except OSError:
        pass
    mc_players = ""
    mc_players = sh("python3 /opt/mc/rcon_client.py 127.0.0.1 25575 "
                    "$(grep rcon.password /opt/mc/server.properties | cut -d= -f2) list", 10)

    return {
        "cycles": cycles, "main_ok": main_ok, "main_fail": main_fail,
        "passive_fail": passive_fail, "units_down": units_down,
        "alerts": alerts, "drift": drift, "avail": avail, "disk_max": disk_max,
        "unit_states": unit_states, "unit_ok": unit_ok, "unit_total": len(UNITS),
        "mem_total": mem.get("MemTotal", 0), "mem_avail": mem.get("MemAvailable", 0),
        "load": round(load, 2), "disk": disk, "sys_up": sys_up,
        "mc": unit_states.get("mc"), "players": mc_players,
    }


# ───────────────────────── 03 运维动作 ─────────────────────────

def collect_ops(hours):
    ops = []
    # IP 优选：DNS 变更（同一轮的多个子域合并成一条）
    ipopt_rounds = {}
    for t, ln in read_log_lines(IPOPT_LOG, hours):
        if "✅ " in ln and " default 已更新 → " in ln:
            head, _, ips = ln.partition(" default 已更新 → ")
            target = head.split("✅ ", 1)[-1].strip()
            key = (int(t // 300) * 300,
                   frozenset(re.findall(r"\d+\.\d+\.\d+\.\d+", ips)))
            ent = ipopt_rounds.setdefault(key, [t, []])
            ent[0] = min(ent[0], t)
            ent[1].append(target)
    for (_, ip_set), (t, targets) in sorted(ipopt_rounds.items(), key=lambda kv: kv[1][0]):
        ops.append((t, "IP 优选", "%s → %s" % (" · ".join(targets), ", ".join(sorted(ip_set)))))
    # MC 备份
    for t, ln in read_log_lines(BACKUP_LOG, hours):
        if "wrote /opt/mc/backups/" in ln:
            part = ln.split("wrote ", 1)[-1]
            ops.append((t, "MC 备份", part))
    # 告警推送
    for t, ln in read_log_lines(PROBE_LOG, hours):
        if "NOTIFY " in ln and "[" in ln:
            kind = ln[ln.find("[") + 1:ln.find("]")]
            if kind in ("服务器故障", "CVE高危漏洞", "不在计划内的IP切换", "主站宕机"):
                ops.append((t, "告警 · " + kind, ln.split("] ", 1)[-1]))
    # 容灾动作（会签快照）
    snap = parse_board(SNAP_RECORD)
    if snap:
        try:
            ts = datetime.strptime(snap.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts).total_seconds() <= hours * 3600:
                ops.append((ts.timestamp(), "容灾切换",
                            "%s 执行 %s" % (snap.get("who", "?"), snap.get("dir", "?"))))
        except (ValueError, TypeError):
            pass
    ops.sort(key=lambda x: x[0])
    return ops


# ───────────────────────── 渲染（瑞士风格） ─────────────────────────

FONT = "'Helvetica Neue',Helvetica,Arial,'PingFang SC','Microsoft YaHei',sans-serif"
MONO = "'SF Mono','Roboto Mono',Consolas,monospace"
RED = "#E30613"
INK = "#0A0A0A"
MUTED = "#6B6B6B"
RULE = "#D8D8D4"
PAPER = "#F4F4F2"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def fmt_ts(t):
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M")


def fmt_dur(sec):
    sec = int(sec or 0)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return "%d 天 %d 小时" % (d, h)
    if h:
        return "%d 小时 %d 分" % (h, m)
    return "%d 分" % m


def row(label, value, mono=False, accent=False):
    style = "font-family:%s;font-size:15px;font-weight:700;" % (MONO if mono else FONT)
    if accent:
        style += "color:%s;" % RED
    return (
        '<tr>'
        '<td style="padding:10px 0;border-top:1px solid %s;font-family:%s;font-size:11px;'
        'letter-spacing:1.6px;text-transform:uppercase;color:%s;vertical-align:top">%s</td>'
        '<td style="padding:10px 0;border-top:1px solid %s;text-align:right;%s">%s</td>'
        '</tr>' % (RULE, FONT, MUTED, esc(label), RULE, style, value)
    )


def section(num, title_en, title_cn, body_html):
    """一个章节：标题行（编号/中文/英文）+ 调用方传入的完整表格 HTML。

    注意：body_html 必须是**完整的** <table>…</table>，不要在 <table> 里再塞 <table>
    （浏览器会重排结构，页脚会被顶出卡片）。
    """
    return f"""
<div style="padding:36px 0 0">
  <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="font-family:{FONT};font-size:11px;letter-spacing:2px;color:{RED};font-weight:700;width:36px;vertical-align:top">{num}</td>
    <td style="font-family:{FONT};font-size:20px;font-weight:700;color:{INK};letter-spacing:-0.2px">{esc(title_cn)}
      <span style="font-size:10px;letter-spacing:2.4px;color:{MUTED};font-weight:400;margin-left:10px">{esc(title_en)}</span></td>
  </tr></table>
  <div style="padding-top:6px">{body_html}</div>
</div>"""


def build_html(report_type, pi, srv, ops, now):
    window_cn = "过去 24 小时" if report_type == "daily" else "过去 7 天"
    window_en = "24 HOURS" if report_type == "daily" else "7 DAYS"
    kind_cn = "日报" if report_type == "daily" else "周报"
    date_big = now.strftime("%d.%m.%Y")

    # 01 树莓派
    if pi.get("online"):
        pi_body = row("状态", "在线 · 心跳于 %s前" % fmt_dur(pi.get("age", 0)))
        pi_body += row("判定 / 模式", "%s · %s" % (esc(pi.get("verdict")), esc(pi.get("mode"))))
        lines = pi.get("lines") or {}
        if lines:
            pi_body += row("线路码", " · ".join("%s %s" % (k, v) for k, v in lines.items()), mono=True)
        pi_body += row("温度", ("%s ℃" % pi.get("temp")) if pi.get("temp") is not None else "—")
        pi_body += row("进程运行", fmt_dur(pi.get("up")))
    else:
        age = pi.get("age")
        pi_body = row("状态", "失联" + ((" · 最后心跳 %s 前" % fmt_dur(age)) if age else ""), accent=True)

    # 02 服务器
    avail_txt = ("%.3f%%" % srv["avail"]) if srv["avail"] is not None else "—"
    srv_body = row("巡检轮次", "%d 轮" % srv["cycles"])
    srv_body += row("主站可用率", avail_txt, accent=(srv["avail"] is not None and srv["avail"] < 99.9))
    srv_body += row("主站异常轮次", "%d 次" % srv["main_fail"], accent=srv["main_fail"] > 0)
    srv_body += row("单元掉线事件", "%d 次" % srv["units_down"], accent=srv["units_down"] > 0)
    srv_body += row("服务在线", "%d / %d" % (srv["unit_ok"], srv["unit_total"]),
                    accent=srv["unit_ok"] < srv["unit_total"])
    srv_body += row("磁盘（当前 / 时段峰值）", "%d%% · %d%%" % (srv["disk"], srv["disk_max"]),
                    accent=srv["disk"] >= 85 or srv["disk_max"] >= 85)
    if srv["mem_total"]:
        srv_body += row("内存可用", "%d MB / %d MB" % (srv["mem_avail"], srv["mem_total"]))
    srv_body += row("负载（1 分钟）", "%.2f" % srv["load"])
    srv_body += row("系统运行时长", fmt_dur(srv["sys_up"]))
    players = (srv.get("players") or "").strip()
    if "There are" in players:
        players = players.split("\n")[-1]
    srv_body += row("Minecraft", "%s%s" % (srv.get("mc", "?"),
                                           (" · " + esc(players)) if players else ""))
    srv_body += row("告警推送", "%d 条" % srv["alerts"], accent=srv["alerts"] > 0)

    # 03 运维动作
    if ops:
        op_rows = ""
        for t, tag, detail in ops[-40:]:
            d = esc(detail)
            if len(d) > 96:
                d = d[:96].rsplit(",", 1)[0] + ", …"
            op_rows += (
                '<tr>'
                '<td style="padding:9px 8px 9px 0;border-top:1px solid %s;font-family:%s;font-size:12px;color:%s;white-space:nowrap;vertical-align:top">%s</td>'
                '<td style="padding:9px 8px 9px 0;border-top:1px solid %s;font-family:%s;font-size:12px;font-weight:700;color:%s;white-space:nowrap;vertical-align:top">%s</td>'
                '<td style="padding:9px 0;border-top:1px solid %s;font-family:%s;font-size:12px;color:%s;word-break:break-all">%s</td>'
                '</tr>' % (RULE, MONO, MUTED, fmt_ts(t), RULE, FONT, INK, esc(tag),
                           RULE, MONO, INK, d)
                )
    else:
        op_rows = ('<tr><td style="padding:14px 0;border-top:1px solid %s;font-family:%s;'
                   'font-size:12px;color:%s">本时段无变更 —— 没有 IP 优选改写、没有备份、没有容灾动作、没有告警。</td></tr>'
                   % (RULE, FONT, MUTED))
    ops_block = ('<table width="100%%" cellpadding="0" cellspacing="0" border="0">'
                 '<tr><th align="left" style="font-family:%s;font-size:10px;letter-spacing:1.6px;'
                 'color:%s;font-weight:400;padding-bottom:6px">TIME</th>'
                 '<th align="left" style="font-family:%s;font-size:10px;letter-spacing:1.6px;'
                 'color:%s;font-weight:400;padding-bottom:6px">ACTION</th>'
                 '<th align="left" style="font-family:%s;font-size:10px;letter-spacing:1.6px;'
                 'color:%s;font-weight:400;padding-bottom:6px">DETAIL</th></tr>%s</table>'
                 % (FONT, MUTED, FONT, MUTED, FONT, MUTED, op_rows))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>服务器{kind_cn} {date_big}</title></head>
<body style="margin:0;padding:0;background:{PAPER}">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAPER}">
<tr><td align="center" style="padding:32px 12px">
<table width="640" cellpadding="0" cellspacing="0" border="0" style="width:640px;max-width:100%;background:#FFFFFF">
<tr><td style="padding:40px 44px 0">

  <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="font-family:{FONT};font-size:10px;letter-spacing:3px;color:{MUTED};text-transform:uppercase">Chenxi Universe · Server Operations</td>
    <td align="right" style="font-family:{FONT};font-size:10px;letter-spacing:3px;color:{INK};text-transform:uppercase">{kind_cn} / {window_en}</td>
  </tr></table>

  <div style="border-top:3px solid {INK};margin:14px 0 0"></div>

  <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="padding:22px 0 6px;font-family:{FONT};font-size:46px;line-height:1;font-weight:700;letter-spacing:-2px;color:{INK}">{date_big}</td>
    <td align="right" style="padding:22px 0 6px;font-family:{FONT};font-size:12px;color:{MUTED};vertical-align:bottom">{window_cn}</td>
  </tr></table>
  <div style="height:6px;background:{RED};width:64px;margin:0 0 8px"></div>

  {section("01", "RASPBERRY PI OBSERVER", "树莓派观察者", '<table width="100%" cellpadding="0" cellspacing="0" border="0">' + pi_body + '</table>')}
  {section("02", "SERVER STATUS", "服务器状态", '<table width="100%" cellpadding="0" cellspacing="0" border="0">' + srv_body + '</table>')}
  {section("03", "OPERATIONS LOG", "运维动作", ops_block)}

  <div style="border-top:1px solid {RULE};margin:36px 0 0"></div>
  <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="padding:18px 0 40px;font-family:{FONT};font-size:10px;line-height:1.7;color:{MUTED};letter-spacing:0.3px">
      ECS 39.101.122.237 · 乌兰察布 · 生成于 {now.strftime("%Y-%m-%d %H:%M")} (CST)<br>
      微信告警只发四类：服务器故障 · CVE 高危漏洞 · 不在计划内的 IP 切换 · 主站宕机。<br>
      本邮件由 ECS 机房节点自动发送；天气推送不受影响。
    </td>
  </tr></table>

</td></tr></table>
</td></tr></table>
</body></html>"""
    return html


def build_text(pi, srv, ops, now):
    lines = [
        "服务器报告 · %s" % now.strftime("%Y-%m-%d %H:%M"),
        "",
        "01 树莓派：%s" % ("在线" if pi.get("online") else "失联"),
        "02 服务器：巡检 %d 轮，主站可用率 %s，单元掉线 %d，磁盘 %d%%" % (
            srv["cycles"], ("%.3f%%" % srv["avail"]) if srv["avail"] is not None else "—",
            srv["units_down"], srv["disk"]),
        "03 运维动作：%d 条" % len(ops),
    ]
    for t, tag, detail in ops[-20:]:
        lines.append("  %s  %s  %s" % (fmt_ts(t), tag, detail))
    return "\n".join(lines)


# ───────────────────────── 发信 ─────────────────────────

def build_accounts():
    accounts = []
    for prefix in ("SMTP_PRIMARY", "SMTP_BACKUP"):
        server = os.environ.get(prefix + "_SERVER", "").strip()
        user = os.environ.get(prefix + "_USERNAME", "").strip()
        pwd = os.environ.get(prefix + "_PASSWORD", "").strip()
        if server and user and pwd:
            accounts.append({
                "server": server,
                "port": int(os.environ.get(prefix + "_PORT", "465") or 465),
                "user": user, "pwd": pwd,
            })
    # 兼容旧键名
    if not accounts:
        server = os.environ.get("SMTP_SERVER", "").strip()
        user = os.environ.get("SMTP_USERNAME", "").strip()
        pwd = os.environ.get("SMTP_PASSWORD", "").strip()
        if server and user and pwd:
            accounts.append({"server": server,
                             "port": int(os.environ.get("SMTP_PORT", "465") or 465),
                             "user": user, "pwd": pwd})
    return accounts


def send_mail(subject, html, text):
    to = [x.strip() for x in os.environ.get("REPORT_TO", "").split(",") if x.strip()]
    if not to:
        log("无 REPORT_TO，跳过发送")
        return False
    accounts = build_accounts()
    if not accounts:
        log("无可用 SMTP 账号，跳过发送")
        return False
    sender_name = os.environ.get("SMTP_SENDER_NAME", "晨曦的宇宙 · 机房节点")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(sender_name, "utf-8")), accounts[0]["user"]))
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    for acc in accounts:
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(acc["server"], acc["port"], timeout=25, context=ctx) as s:
                s.login(acc["user"], acc["pwd"])
                s.sendmail(acc["user"], to, msg.as_string())
            log("MAIL sent via %s (%s)" % (acc["server"], acc["user"]))
            return True
        except Exception as e:
            log("MAIL-FAIL %s (%s): %s" % (acc["server"], acc["user"], e))
    return False


# ───────────────────────── 主流程 ─────────────────────────

def main():
    load_env(ENV_FILE)
    args = sys.argv[1:]
    report_type = "weekly" if "weekly" in args else "daily"
    dry = "--dry-run" in args
    hours = 24 if report_type == "daily" else 24 * 7
    now = datetime.now()

    pi = collect_pi()
    srv = collect_server(hours)
    ops = collect_ops(hours)

    try:
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "type": report_type,
                                "pi_online": pi.get("online"), "avail": srv["avail"],
                                "ops": len(ops)}, ensure_ascii=False) + "\n")
    except OSError:
        pass

    html = build_html(report_type, pi, srv, ops, now)
    text = build_text(pi, srv, ops, now)
    kind = "日报" if report_type == "daily" else "周报"
    subject = "服务器%s · %s" % (kind, now.strftime("%Y-%m-%d"))
    log("%s: cycles=%d avail=%s units_down=%d ops=%d pi=%s dry=%s" % (
        kind, srv["cycles"], srv["avail"], srv["units_down"], len(ops),
        "online" if pi.get("online") else "offline", dry))
    if dry:
        out = "/tmp/report_%s.html" % report_type
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        log("dry-run 写入 %s" % out)
        return 0
    ok = send_mail(subject, html, text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

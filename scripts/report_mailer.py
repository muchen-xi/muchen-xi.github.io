"""
监控报告生成 + 邮件发送 — 日报 / 周报

用法:
  python scripts/report_mailer.py daily    生成过去 24h 日报并发送
  python scripts/report_mailer.py weekly   生成过去 7 天周报并发送
  python scripts/report_mailer.py daily --dry-run   仅生成 HTML，不发送

环境变量:
  ALI_KEY_ID / ALI_KEY_SECRET    — 阿里云 DNS (查询当前状态)
  SMTP_SERVER / SMTP_PORT        — 发件 SMTP (默认 smtp.qiye.aliyun.com:465)
  SMTP_USERNAME / SMTP_PASSWORD  — 发件认证
  SMTP_SENDER_NAME               — 发件人显示名 (默认 "晨曦的宇宙 · 监控")
  REPORT_TO                      — 收件人，逗号分隔
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_DIR = REPO_ROOT / "logs" / "events"
STATS_FILE = REPO_ROOT / "logs" / "stats.json"

DOMAIN = "chenxiuniverse.top"
SITES = ["www", "pimanager"]

# ─── 阿里云 DNS (延迟导入，避免未装 SDK 时脚本加载失败) ───

def _get_dns_client():
    from alibabacloud_alidns20150109.client import Client as AlidnsClient
    from alibabacloud_tea_openapi import models as open_api_models

    key_id = os.environ.get("ALI_KEY_ID", "")
    key_secret = os.environ.get("ALI_KEY_SECRET", "")
    region = os.environ.get("ALI_REGION", "cn-hangzhou")

    client = AlidnsClient(open_api_models.Config(
        access_key_id=key_id,
        access_key_secret=key_secret,
        region_id=region,
    ))
    client._endpoint = f"alidns.{region}.aliyuncs.com"
    return client


def get_current_dns_state() -> dict:
    """查询当前阿里云 DNS 状态（www/pimanager 的 default+oversea 线路 IP）"""
    try:
        from alibabacloud_alidns20150109 import models as alidns_models
        client = _get_dns_client()

        state = {}
        for rr in SITES:
            for line in ["default", "oversea"]:
                req = alidns_models.DescribeDomainRecordsRequest(
                    domain_name=DOMAIN,
                    rrkey_word=rr,
                    type_key_word="A",
                    line=line,
                )
                resp = client.describe_domain_records(req)
                ips = []
                for r in resp.body.domain_records.record:
                    if r.rr == rr and r.type == "A" and r.line == line:
                        ips.append(r.value)

                key = f"{rr}.{line}"
                state[key] = {
                    "ips": sorted(ips),
                    "is_backup": _is_gh_pages(ips),
                }
        return state
    except Exception as e:
        print(f"⚠ DNS 查询失败: {e}")
        return {}


def _is_gh_pages(ips: list[str]) -> bool:
    """判断 IP 列表是否为 GitHub Pages 备站"""
    gh_prefixes = ("185.199.108.", "185.199.109.", "185.199.110.", "185.199.111.")
    if not ips:
        return False
    return all(any(ip.startswith(p) for p in gh_prefixes) for ip in ips)


# ─── 事件读取 ───

def read_events(since_hours: int = 24) -> list[dict]:
    """读取最近 N 小时内的所有事件"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    events = []

    if not EVENTS_DIR.exists():
        return events

    for f in sorted(EVENTS_DIR.glob("*.json")):
        try:
            ev = json.loads(f.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
            if ts >= cutoff:
                ev["_file"] = f.name
                events.append(ev)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    return events


def read_stats(since_days: int = 7) -> dict:
    """读取 stats.json 中最近 N 天的统计数据"""
    if not STATS_FILE.exists():
        return {}

    try:
        all_stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    return {k: v for k, v in sorted(all_stats.items()) if k >= cutoff}


# ─── 流量统计 ───

COUNTER_URL = "https://counter.m20081225.workers.dev"


def get_traffic_stats(days: int = 7) -> dict:
    """从 counter Worker 获取页面访问量"""
    try:
        url = f"{COUNTER_URL}/stats?days={days}&sites=www,pimanager"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"⚠ 流量数据获取失败: {e}")
        return {}


def format_traffic_rows(stats: dict, report_type: str) -> str:
    """生成流量统计表格行"""
    if not stats:
        return "<tr><td colspan='4' style='color:#4a4a6a'>流量数据暂不可用</td></tr>"

    rows = ""
    for date in sorted(stats.keys(), reverse=True):
        www_count = stats[date].get("www", 0)
        pim_count = stats[date].get("pimanager", 0)
        total = www_count + pim_count
        if total == 0 and report_type == "daily":
            continue  # 日报跳过零访问
        rows += f"""<tr>
            <td>{date}</td>
            <td>{www_count}</td>
            <td>{pim_count}</td>
            <td>{total}</td>
        </tr>"""

    if not rows:
        return "<tr><td colspan='4' style='color:#4a4a6a'>暂无访问记录</td></tr>"
    return rows


# ─── 报告生成 ───

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0a0a1a; color: #c8c8d8; max-width: 640px; margin: 0 auto; padding: 24px; }
h1 { color: #7eb8ff; font-size: 1.4em; border-bottom: 1px solid #1a1a3a; padding-bottom: 8px; }
h2 { color: #a0c4ff; font-size: 1.1em; margin-top: 24px; }
.card { background: #12122a; border: 1px solid #1a1a3a; border-radius: 8px; padding: 16px; margin: 12px 0; }
.label { color: #6a6a8a; font-size: 0.85em; }
.value { color: #e0e0f0; font-size: 1.1em; font-weight: 600; }
.good { color: #4eca7a; }
.bad { color: #e0556a; }
.warn { color: #e0a040; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; margin: 2px; }
.tag-primary { background: #1a3a5a; color: #7eb8ff; }
.tag-backup { background: #3a2a1a; color: #e0a040; }
.tag-good { background: #1a3a2a; color: #4eca7a; }
.tag-bad { background: #3a1a2a; color: #e0556a; }
table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
th { text-align: left; color: #6a6a8a; padding: 6px 8px; border-bottom: 1px solid #1a1a3a; }
td { padding: 6px 8px; border-bottom: 1px solid #0f0f25; }
.footer { margin-top: 24px; padding-top: 12px; border-top: 1px solid #1a1a3a;
          font-size: 0.78em; color: #4a4a6a; text-align: center; }
a { color: #7eb8ff; }
"""


def build_html(report_type: str) -> str:
    """生成 HTML 报告"""
    if report_type == "daily":
        hours = 24
        title = "日报"
        subtitle = "过去 24 小时"
        stats_days = 2
    else:
        hours = 24 * 7
        title = "周报"
        subtitle = "过去 7 天"
        stats_days = 8

    events = read_events(since_hours=hours)
    stats = read_stats(since_days=stats_days)
    dns = get_current_dns_state()
    traffic = get_traffic_stats(days=(1 if report_type == "daily" else 7))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 统计数据 ──
    total_checks = sum(s.get("checks_total", 0) for s in stats.values())
    total_failed = sum(s.get("checks_failed", 0) for s in stats.values())
    backup_count = sum(s.get("backup_count", 0) for s in stats.values())
    restore_count = sum(s.get("restore_count", 0) for s in stats.values())

    uptime_pct = 100.0
    if total_checks > 0:
        uptime_pct = round((total_checks - total_failed) / total_checks * 100, 3)

    # ── 流量汇总 ──
    total_pv = 0
    if traffic:
        for date_data in traffic.values():
            total_pv += sum(date_data.values())

    # ── DNS 状态 ──
    dns_rows = ""
    for key, info in dns.items():
        rr, line = key.split(".", 1)
        status_class = "tag-backup" if info["is_backup"] else "tag-primary"
        status_text = "备站 GH" if info["is_backup"] else "主站 CF"
        ips_html = "<br>".join(info["ips"]) if info["ips"] else "—"
        dns_rows += f"""<tr>
            <td>{rr}.{DOMAIN}</td>
            <td>{line}</td>
            <td><span class="tag {status_class}">{status_text}</span></td>
            <td style="font-family:monospace;font-size:0.85em">{ips_html}</td>
        </tr>"""

    # ── 最近事件 ──
    event_rows = ""
    recent_events = events[-20:]  # 最多显示最近 20 条
    if recent_events:
        for ev in reversed(recent_events):
            ts = ev["ts"].replace("T", " ").replace("Z", "")
            etype = ev["type"]
            if etype == "failover_check":
                icon = "❌" if ev.get("status") == "fail" else "✅"
                detail = f"HTTP {ev.get('http_code', '?')}"
                event_rows += f"<tr><td>{ts}</td><td>{icon} 健康检查</td><td>{detail}</td></tr>"
            elif etype == "failover_action":
                icon = "🔴" if ev.get("action") == "backup" else "🟢"
                detail = f"{ev.get('action')} → {ev.get('target', '?')}"
                event_rows += f"<tr><td>{ts}</td><td>{icon} DNS切换</td><td>{detail}</td></tr>"
            elif etype == "ip_update":
                icon = "🔄" if ev.get("changed") else "➡️"
                overseas = ", ".join(ev.get("overseas", []))
                china = ", ".join(ev.get("china", []))
                detail = f"境外: {overseas}<br>境内: {china}" if ev.get("changed") else "IP 未变，跳过"
                event_rows += f"<tr><td>{ts}</td><td>{icon} IP优选</td><td>{detail}</td></tr>"
    else:
        event_rows = "<tr><td colspan='3' style='color:#4a4a6a'>暂无事件记录</td></tr>"

    # ── 每日统计表（周报专用） ──
    stats_table = ""
    if report_type == "weekly" and stats:
        for day, s in sorted(stats.items()):
            day_checks = s.get("checks_total", 0)
            day_failed = s.get("checks_failed", 0)
            day_uptime = 100 if day_checks == 0 else round((day_checks - day_failed) / day_checks * 100, 2)
            backups = s.get("backup_count", 0)
            restores = s.get("restore_count", 0)
            actions = ""
            if backups:
                actions += f"🔴×{backups} "
            if restores:
                actions += f"🟢×{restores}"
            if not actions:
                actions = "—"
            stats_table += f"""<tr>
                <td>{day}</td>
                <td>{day_checks}</td>
                <td><span class="{'good' if day_uptime >= 99.9 else 'warn' if day_uptime >= 99 else 'bad'}">{day_uptime}%</span></td>
                <td>{actions}</td>
            </tr>"""

    # ── 组装 HTML ──
    uptime_class = "good" if uptime_pct >= 99.9 else "warn" if uptime_pct >= 99 else "bad"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>晨曦的宇宙 · {title}</title>
<style>{CSS}</style></head><body>

<h1>🌐 晨曦的宇宙 · {title}</h1>
<p class="label">{subtitle} · 生成于 {now_str}</p>

<h2>📊 可用性概览</h2>
<div class="card">
  <table>
    <tr><td class="label">健康检查次数</td><td class="value">{total_checks}</td></tr>
    <tr><td class="label">故障次数</td><td class="value">{total_failed}</td></tr>
    <tr><td class="label">可用率</td><td class="value {uptime_class}">{uptime_pct}%</td></tr>
    <tr><td class="label">DNS 切换</td><td class="value">🔴 切备站 {backup_count} 次 · 🟢 恢复 {restore_count} 次</td></tr>
    <tr><td class="label">页面访问 (PV)</td><td class="value">{total_pv} 次</td></tr>
  </table>
</div>

<h2>🔗 当前 DNS 状态</h2>
<div class="card">
  <table>
    <tr><th>子域</th><th>线路</th><th>状态</th><th>IP</th></tr>
    {dns_rows}
  </table>
</div>

<h2>📈 页面访问量</h2>
<div class="card">
  <table>
    <tr><th>日期</th><th>主站 www</th><th>下载站 pimanager</th><th>合计</th></tr>
    {format_traffic_rows(traffic, report_type)}
  </table>
</div>
"""

    if report_type == "weekly" and stats_table:
        html += f"""
<h2>📅 每日统计</h2>
<div class="card">
  <table>
    <tr><th>日期</th><th>检查次数</th><th>可用率</th><th>DNS 操作</th></tr>
    {stats_table}
  </table>
</div>
"""

    html += f"""
<h2>📋 最近事件</h2>
<div class="card">
  <table>
    <tr><th>时间 (UTC)</th><th>类型</th><th>详情</th></tr>
    {event_rows}
  </table>
</div>

<div class="footer">
  <p>晨曦的宇宙 · 自动监控系统<br>
  <a href="https://www.chenxiuniverse.top">chenxiuniverse.top</a> ·
  <a href="https://github.com/muchen-xi/cf-ip-optimizer">cf-ip-optimizer</a></p>
  <p>此邮件由 GitHub Actions 自动发送</p>
</div>

</body></html>"""

    return html


# ─── 邮件发送 ───

def send_email(html: str, subject: str):
    """通过 SMTP 发送 HTML 邮件"""
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.qiye.aliyun.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender_name = os.environ.get("SMTP_SENDER_NAME", "晨曦的宇宙 · 监控")
    recipients = os.environ.get("REPORT_TO", "")

    if not username or not password:
        print("❌ 缺少 SMTP_USERNAME / SMTP_PASSWORD 环境变量")
        sys.exit(1)
    if not recipients:
        print("❌ 缺少 REPORT_TO 环境变量")
        sys.exit(1)

    to_list = [a.strip() for a in recipients.split(",") if a.strip()]

    # 构建邮件
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = formataddr((Header(sender_name, "utf-8").encode(), username))
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = Header(subject, "utf-8")

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server.starttls()

        server.login(username, password)
        server.sendmail(username, to_list, msg.as_string())
        server.quit()
        print(f"✅ 邮件已发送 → {', '.join(to_list)}")
    except smtplib.SMTPAuthenticationError:
        print("❌ SMTP 认证失败 — 检查用户名/密码")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        sys.exit(1)


# ─── main ───

def main():
    report_type = "daily"
    dry_run = False

    for a in sys.argv[1:]:
        if a == "weekly":
            report_type = "weekly"
        elif a == "--dry-run":
            dry_run = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return

    print(f"📊 生成 {report_type} 报告...")
    html = build_html(report_type)

    if dry_run:
        out_path = REPO_ROOT / "logs" / f"report_{report_type}.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        print(f"📄 [DRY RUN] 报告已写入 {out_path}")
        print(f"   文件大小: {len(html)} 字符")
        return

    # 邮件主题
    now = datetime.now()
    if report_type == "daily":
        subject = f"🌐 网站监控日报 — {now.strftime('%Y-%m-%d')}"
    else:
        # 本周五 → "2026-06-22 ~ 2026-06-26"
        week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        week_end = now.strftime("%Y-%m-%d")
        subject = f"🌐 网站监控周报 — {week_start} ~ {week_end}"

    send_email(html, subject)


if __name__ == "__main__":
    main()

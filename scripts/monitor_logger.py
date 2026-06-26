"""
监控事件日志器 — 供 failover-monitor 和 cf-ip-optimize workflow 调用

用法:
  python scripts/monitor_logger.py failover-check --status ok|fail [--http-code 200]
  python scripts/monitor_logger.py failover-action --action backup|restore [--target www.default]
  python scripts/monitor_logger.py ip-update --overseas "1.2.3.4,5.6.7.8" --china "9.10.11.12" [--changed]

事件以独立 JSON 文件写入 logs/events/，文件名含时间戳。
并发 workflow 各写各的文件，零冲突。

设计约束:
  - 每 5 分钟的例行健康检查不写事件（288条/天太吵）
  - 只记录: 状态变化(backup/restore)、IP 更新、异常故障
  - 另外维护一个轻量的 stats.json 汇总当日统计（检查次数、故障次数）
"""

import json
import os
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_DIR = REPO_ROOT / "logs" / "events"
STATS_FILE = REPO_ROOT / "logs" / "stats.json"


def ensure_dirs():
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)


def log_event(event_type: str, data: dict) -> str:
    """写入一个事件文件，返回文件路径"""
    ensure_dirs()
    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    fname = f"{ts}_{event_type}.json"
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "type": event_type}
    event.update(data)
    path = EVENTS_DIR / fname
    path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"📝 事件已记录: {fname}")
    return str(path)


def update_stats(key: str, increment: int = 1):
    """更新 stats.json 中的计数器（当日统计）"""
    ensure_dirs()
    today = time.strftime("%Y-%m-%d")
    stats = {}
    if STATS_FILE.exists():
        try:
            stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stats = {}

    if today not in stats:
        stats[today] = {"checks_total": 0, "checks_failed": 0, "backup_count": 0, "restore_count": 0}

    if key in stats[today]:
        stats[today][key] += increment
    else:
        stats[today][key] = increment

    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_failover_check():
    """记录健康检查结果 + 更新每日统计"""
    status = sys.argv[3] if len(sys.argv) > 3 else "ok"
    http_code = None
    for a in sys.argv:
        if a.startswith("--http-code="):
            http_code = a.split("=", 1)[1]

    # 始终更新统计（轻量 JSON 写入）
    update_stats("checks_total")
    if status == "fail":
        update_stats("checks_failed")

    # 只在故障时写独立事件（减少文件堆积）
    if status == "fail":
        data = {"status": status}
        if http_code:
            data["http_code"] = int(http_code) if http_code.isdigit() else http_code
        log_event("failover_check", data)


def cmd_failover_action():
    """记录 DNS 切换动作"""
    action = sys.argv[3] if len(sys.argv) > 3 else "backup"
    target = "www.default"
    for a in sys.argv:
        if a.startswith("--target="):
            target = a.split("=", 1)[1]

    log_event("failover_action", {"action": action, "target": target})
    update_stats(f"{action}_count")


def cmd_ip_update():
    """记录优选 IP 更新事件"""
    overseas = ""
    china = ""
    changed = False
    for a in sys.argv:
        if a.startswith("--overseas="):
            overseas = a.split("=", 1)[1]
        elif a.startswith("--china="):
            china = a.split("=", 1)[1]
        elif a == "--changed":
            changed = True

    data = {"changed": changed}
    if overseas:
        data["overseas"] = [ip.strip() for ip in overseas.split(",") if ip.strip()]
    if china:
        data["china"] = [ip.strip() for ip in china.split(",") if ip.strip()]

    log_event("ip_update", data)


def print_usage():
    print(__doc__)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print_usage()

    cmd = sys.argv[1]
    sub = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "failover-check":
        cmd_failover_check()
    elif cmd == "failover-action":
        cmd_failover_action()
    elif cmd == "ip-update":
        cmd_ip_update()
    else:
        print(f"❌ 未知命令: {cmd}")
        print_usage()


if __name__ == "__main__":
    main()

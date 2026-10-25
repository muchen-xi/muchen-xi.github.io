#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""优选 IP · GitHub 侧会签记录与主节点判决（2026-10-25）

子命令：
  publish-gh <china_csv> <overseas_csv>
      把本轮 ITDog 与境外测速结果写成 _ip-opt-gh TXT（≤255 字节），
      供机房主节点读取合并。

  ecs-fresh [--max-age 14400]
      判断机房主节点 _ip-opt-ecs 会签记录是否新鲜。
      输出 true/false；并在 GITHUB_ENV 里写 SKIP_CHINA_LINE=1/0，
      让 update-dns.py 在机房主节点在线时只维护境外线路（服务降级，但保留兜底）。

环境变量：ALI_KEY_ID / ALI_KEY_SECRET（复用现有 GitHub Secret）
"""

from __future__ import annotations

import calendar
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alidns_rpc as dns  # noqa: E402

DOMAIN = "chenxiuniverse.top"
BOARD_GH = "_ip-opt-gh"
BOARD_SELF = "_ip-opt-ecs"


def read_csv(path, with_ms=True, limit=5):
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ip = row.get("IP 地址") or row.get("IP地址") or row.get("IP")
                if not ip:
                    continue
                item = ip.strip()
                if with_ms:
                    raw = (row.get("平均延迟") or "").strip()
                    try:
                        item = "%s:%.0f" % (item, float(raw))
                    except ValueError:
                        continue
                rows.append(item)
                if len(rows) >= limit:
                    break
    except OSError:
        print("⚠ 读不到 %s" % path, file=sys.stderr)
    return rows


def publish_gh(china_csv, overseas_csv):
    payload = {
        "v": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "itdog": read_csv(china_csv, True, 5),
        "ove": read_csv(overseas_csv, False, 3),
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > 255:
        # 先砍 ITDog 条数，再砍境外
        for keep in (4, 3, 2, 1):
            payload["itdog"] = payload["itdog"][:keep]
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(text.encode("utf-8")) <= 255:
                break
    print("会签板 %s: %s" % (BOARD_GH, dns.set_txt(BOARD_GH, text, DOMAIN)))


def ecs_fresh(max_age):
    try:
        raw = dns.get_txt(BOARD_SELF, DOMAIN) or ""
    except Exception as e:
        print("⚠ 读 _ip-opt-ecs 失败（按不新鲜处理）: %s" % e, file=sys.stderr)
        return False
    if not raw:
        return False
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    raw = raw.replace('\\"', '"').replace("\\\\", "\\")
    try:
        rec = json.loads(raw)
        ts = calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return False
    # 只有机房节点处于 apply（真正维护 A 记录）时，GitHub 侧才让出 default 线路；
    # publish 灰度模式下机房不写 DNS，此时 GitHub 必须继续全量维护，否则境内线路会冻结。
    if str(rec.get("mode", "publish")).lower() != "apply":
        print("机房主节点处于 %s 模式 — GitHub 继续全量维护" % rec.get("mode"), file=sys.stderr)
        return False
    age = int(time.time() - ts)
    print("机房主节点会签记录 age=%ss (阈值 %ss)" % (age, max_age), file=sys.stderr)
    return age <= max_age


def write_github_env(skip_china):
    path = os.environ.get("GITHUB_ENV")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("SKIP_CHINA_LINE=%d\n" % (1 if skip_china else 0))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "publish-gh":
        publish_gh(sys.argv[2] if len(sys.argv) > 2 else "china_result.csv",
                   sys.argv[3] if len(sys.argv) > 3 else "result.csv")
        return 0
    if cmd == "ecs-fresh":
        max_age = 14400
        for i, a in enumerate(sys.argv):
            if a == "--max-age" and i + 1 < len(sys.argv):
                max_age = int(sys.argv[i + 1])
        fresh = ecs_fresh(max_age)
        write_github_env(fresh)
        print("true" if fresh else "false")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周热点采集 —— 从本机 RSSHub 抓热榜，落盘 /opt/report/hot.jsonl。

日报/周报（report.py weekly）读这个文件，按"跨天出现次数 + 榜位"挑出
「这一周最该关注的 8 件事」。采集是每天 07:30 一次，不影响 08:00 的报告。

RSSHub 跑在本机 127.0.0.1:1200（Docker，256M + 1G swap）。
"""

import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET

RSSHUB = "http://127.0.0.1:1200"
OUT = "/opt/report/hot.jsonl"
KEEP_DAYS = 35

ROUTES = [
    ("知乎热榜", "/zhihu/hot", 30),
    ("B站排行", "/bilibili/ranking/0/3/0/0", 30),
    ("Solidot", "/solidot/www", 20),
    ("36氪", "/36kr/newsflashes", 20),
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch(path, timeout=25):
    req = urllib.request.Request(RSSHUB + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_items(raw):
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if len(title) >= 6:
            out.append((title, link))
    return out


def main():
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    for source, path, limit in ROUTES:
        try:
            items = parse_items(fetch(path))[:limit]
        except Exception as e:
            print("⚠ %s 抓取失败: %s" % (source, e))
            continue
        for rank, (title, link) in enumerate(items, 1):
            rows.append({"ts": now, "date": today, "source": source,
                         "rank": rank, "title": title, "link": link})
        print("%s: %d 条" % (source, len(items)))

    if not rows:
        print("本轮无数据，不写入")
        return 1

    # 追加 + 裁剪（按日期）
    existing = []
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - KEEP_DAYS * 86400))
    try:
        with open(OUT, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("date", "") >= cutoff:
                    existing.append(d)
    except OSError:
        pass
    existing.extend(rows)
    with open(OUT + ".tmp", "w", encoding="utf-8") as f:
        for d in existing:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    os.replace(OUT + ".tmp", OUT)
    print("写入 %d 条（保留 %d 天）" % (len(rows), KEEP_DAYS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

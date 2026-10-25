#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本周热点推到 Cloudflare KV（Pages Function /api/hot 读它）。

ECS 只做出站：一个 HTTPS PUT 到 api.cloudflare.com，不开任何入站端口。
配置 /opt/report/cf.env（600）：
  CF_API_TOKEN=...
  CF_ACCOUNT_ID=...
  CF_KV_NAMESPACE_ID=...
  CF_KV_KEY=hot:latest
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R  # noqa: E402

ENV = "/opt/report/cf.env"
LOG = "/var/log/report.log"
SOURCES = ["知乎热榜", "B站排行", "Solidot", "36氪"]


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
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " [hot-push] " + msg
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main():
    load_env(ENV)
    token = os.environ.get("CF_API_TOKEN", "").strip()
    acct = os.environ.get("CF_ACCOUNT_ID", "").strip()
    ns = os.environ.get("CF_KV_NAMESPACE_ID", "").strip()
    key = os.environ.get("CF_KV_KEY", "hot:latest")
    if not (token and acct and ns):
        log("缺 CF_API_TOKEN / CF_ACCOUNT_ID / CF_KV_NAMESPACE_ID，跳过")
        return 1

    items = R.collect_hot(7)
    if not items:
        log("hot.jsonl 无数据，跳过推送")
        return 1
    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": SOURCES,
        "items": [{"title": i["title"], "link": i.get("link", ""),
                   "sources": i["sources"], "days": i["days"]} for i in items],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    url = ("https://api.cloudflare.com/client/v4/accounts/%s/storage/kv/namespaces/%s/values/%s"
           % (acct, ns, urllib.parse.quote(key, safe="")))
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            ok = json.loads(r.read().decode()).get("success")
        log("KV 写入 %s 成功=%s 条目=%d 字节=%d" % (key, ok, len(items), len(body)))
        return 0 if ok else 2
    except Exception as e:
        log("KV 写入失败: %s: %s" % (type(e).__name__, e))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

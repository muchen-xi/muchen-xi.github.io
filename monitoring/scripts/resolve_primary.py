#!/usr/bin/env python3
"""权威解析 www.chenxiuniverse.top 指定线路的 A 记录（不经递归 DNS，防缓存/线路失明）。

容灾监控 primary 模式用它获取当前权威 A 记录，再 curl --resolve 直连检测。

用法: python3 resolve_primary.py [default|oversea]   （默认 default）

背景（2026-08-15 黑洞演练）:
  - 递归 DNS 对旧记录有 ≥60min 级缓存，DNS 层攻击时检测失明
  - runner 在境外，递归解析命中 oversea 线路；容灾切换保护的却是 default 线路（中国用户）
  - 因此必须从权威 API 按指定 line 取记录，绕过递归解析

环境变量: ALI_KEY_ID / ALI_KEY_SECRET（与 failover-dns.py 相同）
失败时输出为空并退出码 1，调用方回退普通 DNS 检测。
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

DOMAIN = "chenxiuniverse.top"
ENDPOINT = "https://alidns.cn-hangzhou.aliyuncs.com/"


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


def main() -> None:
    line = sys.argv[1] if len(sys.argv) > 1 else "default"
    if line not in ("default", "oversea"):
        sys.stderr.write(f"非法线路: {line}\n")
        sys.exit(1)
    try:
        d = call(
            "DescribeDomainRecords",
            DomainName=DOMAIN,
            RRKeyWord="www",
            TypeKeyWord="A",
            Line=line,
        )
        recs = [
            r["Value"]
            for r in (d.get("DomainRecords", {}).get("Record", []) or [])
            if r.get("RR") == "www" and r.get("Type") == "A" and r.get("Line") == line
        ]
        if recs:
            print(recs[0])
    except Exception:
        sys.exit(1)  # 输出为空 + 非零退出，调用方回退


if __name__ == "__main__":
    main()

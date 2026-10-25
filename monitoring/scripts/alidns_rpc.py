#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里云云解析（AliDNS）极简客户端 —— 纯标准库，RPC 签名 V1.0。

为什么不用官方 SDK：
  - ECS 上不想为一个 TXT 读写装 alibabacloud_alidns20150109；
  - GitHub Actions 侧现有脚本已装 SDK，但共用同一套签名逻辑更不容易漂移。

凭据来源（按优先级）：
  环境变量 ALI_KEY_ID / ALI_KEY_SECRET；
  或环境变量 ALI_CRED_FILE 指向的文件（KEY=VALUE 或 "AccessKey ID: xxx" 两种写法都认）。

只用 GET 调用；写操作（Add/Update/Delete）失败不重试（重发可能造重复记录）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ENDPOINT = "https://alidns.aliyuncs.com/"
API_VERSION = "2015-01-09"


def _percent_encode(s) -> str:
    return urllib.parse.quote(str(s), safe="").replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def load_credentials():
    ak_id = os.environ.get("ALI_KEY_ID") or os.environ.get("ALIYUN_AK_ID")
    ak_secret = os.environ.get("ALI_KEY_SECRET") or os.environ.get("ALIYUN_AK_SECRET")
    if ak_id and ak_secret:
        return ak_id, ak_secret
    path = os.environ.get("ALI_CRED_FILE")
    if path:
        try:
            with open(path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line and not line.lower().startswith("accesskey"):
                        k, _, v = line.partition("=")
                        k = k.strip().upper()
                        v = v.strip().strip("\"'")
                        if k in ("ALI_KEY_ID", "ALIYUN_AK_ID", "ACCESS_KEY_ID"):
                            ak_id = ak_id or v
                        elif k in ("ALI_KEY_SECRET", "ALIYUN_AK_SECRET", "ACCESS_KEY_SECRET"):
                            ak_secret = ak_secret or v
        except OSError:
            pass
    if not (ak_id and ak_secret):
        raise SystemExit("未找到解析凭据：请设 ALI_KEY_ID/ALI_KEY_SECRET 或 ALI_CRED_FILE")
    return ak_id, ak_secret


def call(action: str, params: dict, timeout: int = 20) -> dict:
    ak_id, ak_secret = load_credentials()
    common = {
        "Action": action,
        "Format": "JSON",
        "Version": API_VERSION,
        "AccessKeyId": ak_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    common.update({k: v for k, v in params.items() if v is not None})
    canonical = "&".join("%s=%s" % (_percent_encode(k), _percent_encode(common[k])) for k in sorted(common))
    string_to_sign = "GET&%2F&" + _percent_encode(canonical)
    sig = base64.b64encode(
        hmac.new((ak_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    url = ENDPOINT + "?" + canonical + "&Signature=" + _percent_encode(sig)
    req = urllib.request.Request(url, headers={"User-Agent": "chenxi-ipopt/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError("%s HTTP %d %s" % (action, e.code, body[:300]))


def describe_records(domain: str, rr: str = "", rtype: str = "", line: str = "") -> list:
    """列出记录（会自动翻页）。"""
    out, page = [], 1
    while True:
        data = call("DescribeDomainRecords", {
            "DomainName": domain,
            "RRKeyWord": rr or None,
            "Type": rtype or None,
            "Line": line or None,
            "PageNumber": page,
            "PageSize": 100,
        })
        body = data.get("DomainRecords", {}).get("Record", []) or []
        out.extend(body)
        total = int(data.get("TotalCount", len(out)))
        if len(out) >= total or not body:
            return out
        page += 1


def _match_type(rec_type: str, want: str) -> bool:
    return str(rec_type).upper() == want.upper()


def get_txt(name: str, domain: str) -> str | None:
    """读 default 线路的 TXT 记录值；不存在返回 None。"""
    rr = name[:-len(domain) - 1] if name.endswith("." + domain) else name
    for rec in describe_records(domain, rr, "TXT"):
        if rec.get("RR") == rr and _match_type(rec.get("Type", ""), "TXT"):
            return rec.get("Value")
    return None


def set_txt(name: str, value: str, domain: str, ttl: int = 600) -> str:
    """写 default 线路 TXT（存在则更新，不存在则新增）。返回 update/add。"""
    if len(value.encode("utf-8")) > 255:
        raise ValueError("TXT 值超过 255 字节")
    rr = name[:-len(domain) - 1] if name.endswith("." + domain) else name
    for rec in describe_records(domain, rr, "TXT"):
        if rec.get("RR") == rr and _match_type(rec.get("Type", ""), "TXT") \
                and (rec.get("Line") or "default") in ("default", ""):
            if rec.get("Value") == value:
                return "unchanged"
            call("UpdateDomainRecord", {
                "RecordId": rec["RecordId"], "RR": rr, "Type": "TXT",
                "Value": value, "TTL": ttl,
            })
            return "update"
    call("AddDomainRecord", {"DomainName": domain, "RR": rr, "Type": "TXT", "Value": value, "TTL": ttl})
    return "add"


def set_a_records(rr: str, line: str, ips: list, domain: str, ttl: int = 600) -> bool:
    """把 rr+line 的 A 记录收敛为 ips（保持记录数量、多余删除、不足新增）。

    返回 True = 有变更。先比较现有一致则直接返回 False，省 API 配额。
    """
    recs = [r for r in describe_records(domain, rr, "A")
            if r.get("RR") == rr and _match_type(r.get("Type", ""), "A")
            and (r.get("Line") or "default") == line]
    current = [r.get("Value") for r in recs]
    if sorted(current) == sorted(ips):
        return False

    # 先把相同的原地保留（避免同值更新触发 DomainRecordDuplicate）
    remaining = list(ips)
    for rec in recs:
        if rec.get("Value") in remaining:
            remaining.remove(rec.get("Value"))
        else:
            call("DeleteDomainRecord", {"RecordId": rec["RecordId"]})

    for ip in remaining:
        call("AddDomainRecord", {"DomainName": domain, "RR": rr, "Type": "A",
                                 "Value": ip, "Line": line, "TTL": ttl})
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    args = dict(a.split("=", 1) for a in sys.argv[2:] if "=" in a)
    domain = args.get("domain", "chenxiuniverse.top")
    if cmd == "get" and "name" in args:
        print(get_txt(args["name"], domain) or "")
    elif cmd == "set" and "name" in args and "value" in args:
        print(set_txt(args["name"], args["value"], domain))
    elif cmd == "dump":
        for r in describe_records(domain, args.get("rr", ""), args.get("type", "")):
            print(json.dumps(r, ensure_ascii=False))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

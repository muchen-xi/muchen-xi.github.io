#!/usr/bin/env python3
"""⚠️ 临时演练工具（2026-10-01 容灾黑洞演练专用）—— 演练结束后删除。

动作（环境变量 DRILL_ACTION）:
  status  — 只读快照：打印 www 两条线路 A 记录、starkeeper 各线路 A/CNAME，
            并输出一行 SNAPSHOT_JSON（供 inject 后恢复使用）
  inject  — 注入黑洞：把 www default/oversea 的 A 记录收敛为单条黑洞 IP（默认 203.0.113.1）
  restore — 按快照恢复：DRILL_SNAPSHOT='<json>'，把 www / starkeeper 写回快照状态

环境变量: ALI_KEY_ID / ALI_KEY_SECRET（与生产脚本同套，仅 DNS 权限）
依赖: 仅标准库（签名实现与 monitoring/scripts/resolve_primary.py 同构）
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

DOMAIN = "chenxiuniverse.top"
ENDPOINT = "https://alidns.cn-hangzhou.aliyuncs.com/"
TTL = 600
BLACKHOLE = os.environ.get("BLACKHOLE_IP", "203.0.113.1")
LINES = ("default", "oversea")
PAGES_HOST = "starkeeper-bpw.pages.dev"


def enc(s):
    return urllib.parse.quote(str(s), safe="~")


def call(action, **extra):
    """阿里云 Alidns API（HMAC-SHA1 签名），失败重试 3 次。"""
    key_id = os.environ.get("ALI_KEY_ID", "")
    key_secret = os.environ.get("ALI_KEY_SECRET", "")
    last_err = None
    for attempt in range(3):
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
        try:
            with urllib.request.urlopen(ENDPOINT + "?" + qs, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {body}"
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{action} 失败: {last_err}")


def records(rr, rtype, line):
    d = call("DescribeDomainRecords", DomainName=DOMAIN, RRKeyWord=rr,
             TypeKeyWord=rtype, Line=line, PageSize=100)
    return [r for r in (d.get("DomainRecords", {}).get("Record", []) or [])
            if r.get("RR") == rr and r.get("Type") == rtype and r.get("Line") == line]


def add(rr, rtype, value, line):
    return call("AddDomainRecord", DomainName=DOMAIN, RR=rr, Type=rtype,
                Value=value, Line=line, TTL=TTL)


def update(rid, rr, rtype, value):
    return call("UpdateDomainRecord", RecordId=rid, RR=rr, Type=rtype, Value=value, TTL=TTL)


def delete(rid):
    return call("DeleteDomainRecord", RecordId=rid)


def snapshot():
    snap = {"ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "www": {}, "starkeeper": {}}
    print("── 权威记录快照 ──")
    for line in LINES:
        vals = [r["Value"] for r in records("www", "A", line)]
        snap["www"][line] = vals
        print(f"www/A/{line}: {vals}")
        a = [r["Value"] for r in records("starkeeper", "A", line)]
        c = [r["Value"] for r in records("starkeeper", "CNAME", line)]
        snap["starkeeper"][line] = {"A": a, "CNAME": c}
        print(f"starkeeper/{line}: A={a} CNAME={c}")
    print("SNAPSHOT_JSON=" + json.dumps(snap, separators=(",", ":")))
    return snap


def inject():
    snapshot()
    print(f"── 注入黑洞 {BLACKHOLE}（www 两条线路 A 记录收敛为单条）──")
    for line in LINES:
        recs = records("www", "A", line)
        if not recs:
            print(f"⚠ www/A/{line} 无记录，跳过（不注入）")
            continue
        update(recs[0]["RecordId"], "www", "A", BLACKHOLE)
        for r in recs[1:]:
            delete(r["RecordId"])
        print(f"✅ www/A/{line}: {len(recs)} 条 → 单条 {BLACKHOLE}")
    print("── 注入后复核 ──")
    for line in LINES:
        print(f"www/A/{line}: {[r['Value'] for r in records('www', 'A', line)]}")


def restore():
    raw = os.environ.get("DRILL_SNAPSHOT", "").strip()
    if not raw:
        print("❌ 缺少 DRILL_SNAPSHOT（把 status/inject 输出的 SNAPSHOT_JSON 整行贴到输入里）")
        sys.exit(1)
    snap = json.loads(raw)
    print(f"── 按快照恢复（快照时间 {snap.get('ts')}）──")
    for line in LINES:
        want = [v for v in snap.get("www", {}).get(line, []) if v]
        cur = records("www", "A", line)
        if not want:
            print(f"⚠ www/A/{line} 快照为空，跳过")
            continue
        if cur:
            update(cur[0]["RecordId"], "www", "A", want[0])
            for r in cur[1:]:
                delete(r["RecordId"])
        for v in want[1:]:
            add("www", "A", v, line)
        print(f"✅ www/A/{line} → {want}")
    for line in LINES:
        want_a = [v for v in (snap.get("starkeeper", {}).get(line, {}) or {}).get("A", []) if v]
        want_c = [v for v in (snap.get("starkeeper", {}).get(line, {}) or {}).get("CNAME", []) if v]
        cur_a = records("starkeeper", "A", line)
        cur_c = records("starkeeper", "CNAME", line)
        if not want_a and not want_c:
            continue
        if want_a and not want_c:
            for r in cur_c:
                delete(r["RecordId"])
                print(f"🗑 starkeeper/CNAME/{line} {r['Value']}")
            if not cur_a:
                for v in want_a:
                    add("starkeeper", "A", v, line)
            else:
                update(cur_a[0]["RecordId"], "starkeeper", "A", want_a[0])
                for r in cur_a[1:]:
                    delete(r["RecordId"])
                for v in want_a[1:]:
                    add("starkeeper", "A", v, line)
            print(f"✅ starkeeper/{line} → A {want_a}")
        else:
            print(f"ℹ starkeeper/{line} 快照非 A 形态（A={want_a} CNAME={want_c}），保持现状")
    print("── 恢复后复核 ──")
    for line in LINES:
        print(f"www/A/{line}: {[r['Value'] for r in records('www', 'A', line)]}")
        print(f"starkeeper/{line}: A={[r['Value'] for r in records('starkeeper', 'A', line)]} "
              f"CNAME={[r['Value'] for r in records('starkeeper', 'CNAME', line)]}")


def main():
    action = (os.environ.get("DRILL_ACTION") or "status").strip()
    print(f"演练工具动作: {action}")
    if action == "status":
        snapshot()
    elif action == "inject":
        inject()
    elif action == "restore":
        restore()
    else:
        print(f"❌ 未知动作: {action}")
        sys.exit(1)
    print("完成。")


if __name__ == "__main__":
    main()

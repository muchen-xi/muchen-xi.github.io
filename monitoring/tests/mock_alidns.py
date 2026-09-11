#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock_alidns.py — 离线假 Alidns 服务（纯标准库，仅供 monitoring/tests 对抗推演）。

目标：让 `dr_agent.py` / `dr_board.py` 在不触碰真实阿里云 DNS 的前提下，走完
"真实 HTTP API 交互 + 完整决策链 + 连续多轮探测"。

用法（手工调试）:
  python mock_alidns.py --port 8899 --seed primary
  # 另一个终端（Git Bash）:
  export ALI_ENDPOINT=http://127.0.0.1:8899/
  export ALI_KEY_ID=mock ALI_KEY_SECRET=mock
  python monitoring/scripts/dr_board.py mode www

测试内嵌:
  import mock_alidns
  srv = mock_alidns.start_mock()          # 随机端口，后台线程
  srv.set_record("www", "A", "default", "172.64.52.95")
  srv.set_fault("DescribeDomainRecords", code="InternalError", http_status=500, count=1)
  srv.stop()

实现的 Action（响应结构与真实 API 一致）:
  DescribeDomainRecords   RR/Type/Line 过滤 + PageSize（默认 20，与真实 API 一致）；
                          无匹配时 Record 为空列表；可注入 DomainRecords: null 边界形态
  AddDomainRecord         重复（同 RR+Type+Line+Value）返回 DomainRecordDuplicate（HTTP 400）
  UpdateDomainRecord      未知 RecordId 返回 InvalidRecordId
  DeleteDomainRecord      同上

故障注入（set_fault）:
  code/message/http_status  指定操作返回真实风格 {"Code":..., "Message":...}
  mode="null_records"       DescribeDomainRecords 返回 {"DomainRecords": null}
  mode="empty_records"      返回 {"DomainRecords": {"Record": []}}
  timeout=秒                 挂起指定时长后照常响应（用于触发客户端超时）
  rr="www"                  只对指定 RR 生效（可选）
  count=N                   只生效 N 次；None = 直到 clear_faults()

签名校验：只校验 AccessKeyId / Signature / SignatureMethod / Timestamp 参数存在且
Signature 非空（不重算 HMAC，不校验 AK 真伪）——与"测试不需要真凭据"配合。

注意：本文件是**测试替身**，不是生产代码；不得被生产脚本 import。
"""

import argparse
import datetime
import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOMAIN = "chenxiuniverse.top"
API_VERSION = "2015-01-09"
DEFAULT_TTL = 600
DEFAULT_PAGE_SIZE = 20  # 真实 DescribeDomainRecords 默认 20（仓库脚本因此显式传 PageSize=100）

# "记录已存在/冲突"类错误码（与 dr_agent._is_record_conflict_error 的判据对齐）
CONFLICT_CODES = ("DomainRecordDuplicate", "DomainRecordConflict", "RecordAlreadyExists")


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_id():
    return "MOCK-" + uuid.uuid4().hex[:16].upper()


class MockApiError(Exception):
    """假 API 的错误响应（转成 {"Code","Message"} + HTTP 状态码）。"""

    def __init__(self, code, message, http_status=400):
        Exception.__init__(self, "%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.http_status = http_status


class MockAlidns(object):
    """内存 zone + 假 Alidns HTTP 服务（线程内嵌或 CLI 常驻）。"""

    def __init__(self, host="127.0.0.1", port=0, verbose=False):
        self.host = host
        self.port = port
        self.verbose = verbose
        self._lock = threading.RLock()
        self._records = {}   # RecordId -> record dict（保持插入顺序）
        self._seq = 0
        self._faults = []
        self._calls = []     # [{"ts","action","params"}]
        self._httpd = None
        self._thread = None

    # ------------------------------------------------------------------ 生命周期

    def start(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MockAlidns/1.0"

            def log_message(self, fmt, *args):
                if mock.verbose:
                    print("[mock-alidns] " + (fmt % args))

            def do_GET(self):
                mock._handle(self)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mock-alidns", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
        self._httpd = None
        self._thread = None

    @property
    def url(self):
        return "http://%s:%d/" % (self.host, self.port)

    # ------------------------------------------------------------------ zone 操作

    def _next_id(self):
        self._seq += 1
        return "mock-%05d" % self._seq

    def add_record(self, rr, rtype, line, value, ttl=DEFAULT_TTL):
        """新增一条记录，返回 RecordId（不做重复校验，重复由 API 层模拟）。"""
        with self._lock:
            rid = self._next_id()
            self._records[rid] = {
                "DomainName": DOMAIN, "RecordId": rid, "RR": rr, "Type": rtype.upper(),
                "Value": value, "Line": (line or "default"), "TTL": int(ttl),
                "Status": "ENABLE", "Locked": False, "Weight": 1,
            }
            return rid

    def set_record(self, rr, rtype, line, value, ttl=DEFAULT_TTL):
        """把 (rr, type, line) 收敛为单条记录（先删同组旧记录，再加），返回 RecordId。

        测试里用 "两条线路指向 X" / "改回真实 CF IP" 这类整体替换语义。
        """
        self.delete_record(rr, rtype, line)
        return self.add_record(rr, rtype, line, value, ttl)

    def delete_record(self, rr, rtype=None, line=None):
        """删除匹配记录，返回删除条数。"""
        removed = 0
        with self._lock:
            for rid, rec in list(self._records.items()):
                if rec["RR"] != rr:
                    continue
                if rtype is not None and rec["Type"].upper() != str(rtype).upper():
                    continue
                if line is not None and rec["Line"] != line:
                    continue
                self._records.pop(rid, None)
                removed += 1
        return removed

    def get_record(self, rr, rtype=None, line=None):
        """返回第一条匹配记录的副本；不存在返回 None。"""
        recs = self.get_all(rr, rtype, line)
        return recs[0] if recs else None

    def get_all(self, rr=None, rtype=None, line=None):
        """返回匹配记录副本列表（按插入顺序）。"""
        with self._lock:
            out = []
            for rec in self._records.values():
                if rr is not None and rec["RR"] != rr:
                    continue
                if rtype is not None and rec["Type"].upper() != str(rtype).upper():
                    continue
                if line is not None and rec["Line"] != line:
                    continue
                out.append(dict(rec))
            return out

    def dump_zone(self):
        """全量 zone（副本列表，按插入顺序），供断言/打印。"""
        return self.get_all()

    def clear_zone(self):
        with self._lock:
            self._records.clear()

    # ------------------------------------------------------------------ 故障注入

    def set_fault(self, action, code=None, message=None, http_status=400,
                  mode=None, timeout=None, rr=None, count=1):
        """注入故障。action=None 表示匹配所有 Action；count=None 表示持续到 clear_faults()。"""
        with self._lock:
            self._faults.append({
                "action": action, "code": code, "message": message,
                "http_status": http_status, "mode": mode, "timeout": timeout,
                "rr": rr, "remaining": count,
            })

    def clear_faults(self):
        with self._lock:
            self._faults = []

    def _match_fault(self, action, params):
        """取出第一条命中的故障（命中一次消耗一次 count）。"""
        with self._lock:
            for fault in list(self._faults):
                if fault["action"] not in (None, action):
                    continue
                if fault["rr"] is not None and params.get("RR") != fault["rr"] \
                        and params.get("RRKeyWord") != fault["rr"]:
                    continue
                remaining = fault["remaining"]
                if remaining is not None:
                    if remaining <= 0:
                        self._faults.remove(fault)
                        continue
                    fault["remaining"] = remaining - 1
                    if fault["remaining"] <= 0:
                        self._faults.remove(fault)
                return dict(fault)
        return None

    # ------------------------------------------------------------------ 调用统计

    def calls(self, action=None, rr=None):
        """调用次数统计（rr 匹配 params 里的 RR 或 RRKeyWord）。"""
        return len(self.call_log(action=action, rr=rr))

    def call_log(self, action=None, rr=None):
        with self._lock:
            out = []
            for item in self._calls:
                if action is not None and item["action"] != action:
                    continue
                if rr is not None and item["params"].get("RR") != rr \
                        and item["params"].get("RRKeyWord") != rr:
                    continue
                out.append({"ts": item["ts"], "action": item["action"], "params": dict(item["params"])})
            return out

    def clear_calls(self):
        with self._lock:
            self._calls = []

    # ------------------------------------------------------------------ HTTP 处理

    def _handle(self, handler):
        parsed = urllib.parse.urlsplit(handler.path)
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        action = params.get("Action") or ""
        with self._lock:
            self._calls.append({"ts": time.time(), "action": action, "params": dict(params)})

        fault = self._match_fault(action, params)
        if fault is not None and fault.get("timeout"):
            time.sleep(float(fault["timeout"]))

        try:
            if not action:
                raise MockApiError("MissingParameter", "The parameter Action is mandatory.", 400)
            for key in ("AccessKeyId", "Signature", "SignatureMethod", "Timestamp"):
                if not params.get(key):
                    raise MockApiError("MissingParameter",
                                       "The parameter %s is mandatory." % key, 400)
            data = self._dispatch(action, params, fault)
        except MockApiError as e:
            self._respond(handler, e.http_status,
                          {"RequestId": _request_id(), "Code": e.code, "Message": e.message})
            return
        except Exception as e:  # 假服务内部错误也应真实风格返回
            self._respond(handler, 500,
                          {"RequestId": _request_id(), "Code": "InternalError", "Message": str(e)})
            return
        self._respond(handler, 200, data)

    def _respond(self, handler, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("x-acs-request-id", _request_id())
            handler.end_headers()
            handler.wfile.write(body)
        except Exception:
            pass  # 客户端提前断开（超时注入场景）

    def _dispatch(self, action, params, fault):
        # 故障注入优先：mode 形态 / 指定错误码
        if fault is not None:
            code = fault.get("code") or ("InternalError" if fault.get("http_status", 400) >= 500 else None)
            if code:
                raise MockApiError(code, fault.get("message") or ("mock fault: %s" % code),
                                   fault.get("http_status", 400))
        if action == "DescribeDomainRecords":
            return self._describe(params, fault)
        if action == "AddDomainRecord":
            return self._add(params)
        if action == "UpdateDomainRecord":
            return self._update(params)
        if action == "DeleteDomainRecord":
            return self._delete(params)
        raise MockApiError("InvalidAction", "Unsupported action: %s" % action, 400)

    def _describe(self, params, fault):
        if fault is not None and fault.get("mode") == "null_records":
            # 真实 API 无匹配时可能返回 DomainRecords: null（仓库脚本专门做了空值保护）
            return {"RequestId": _request_id(), "TotalCount": 0, "PageNumber": 1,
                    "PageSize": DEFAULT_PAGE_SIZE, "DomainRecords": None}
        if params.get("DomainName") not in (None, "", DOMAIN):
            raise MockApiError("InvalidDomainName", "Domain not found: %s" % params.get("DomainName"), 400)
        rr_kw = (params.get("RRKeyWord") or "").lower()
        type_kw = (params.get("TypeKeyWord") or "").upper()
        line_kw = (params.get("Line") or "").lower()
        with self._lock:
            matched = []
            for rec in self._records.values():
                if rr_kw and rr_kw not in rec["RR"].lower():
                    continue
                if type_kw and rec["Type"].upper() != type_kw:
                    continue
                if line_kw and rec["Line"].lower() != line_kw:
                    continue
                matched.append(dict(rec))
        try:
            page_size = int(params.get("PageSize") or DEFAULT_PAGE_SIZE)
        except ValueError:
            page_size = DEFAULT_PAGE_SIZE
        page_size = max(1, page_size)
        if fault is not None and fault.get("mode") == "empty_records":
            matched = []
        return {
            "RequestId": _request_id(),
            "TotalCount": len(matched),
            "PageNumber": 1,
            "PageSize": page_size,
            "DomainRecords": {"Record": matched[:page_size]},
        }

    def _add(self, params):
        for key in ("DomainName", "RR", "Type", "Value"):
            if not params.get(key):
                raise MockApiError("MissingParameter", "The parameter %s is mandatory." % key, 400)
        rr, rtype = params["RR"], params["Type"].upper()
        value = params["Value"]
        line = params.get("Line") or "default"
        try:
            ttl = int(params.get("TTL") or DEFAULT_TTL)
        except ValueError:
            raise MockApiError("InvalidParameter", "TTL must be an integer.", 400)
        with self._lock:
            for rec in self._records.values():
                if (rec["RR"], rec["Type"].upper(), rec["Line"], rec["Value"]) == (rr, rtype, line, value):
                    raise MockApiError("DomainRecordDuplicate",
                                       "The DNS record already exists.", 400)
        rid = self.add_record(rr, rtype, line, value, ttl)
        return {"RequestId": _request_id(), "RecordId": rid}

    def _find_by_id(self, record_id):
        with self._lock:
            rec = self._records.get(record_id)
            return dict(rec) if rec else None

    def _update(self, params):
        record_id = params.get("RecordId")
        if not record_id:
            raise MockApiError("MissingParameter", "The parameter RecordId is mandatory.", 400)
        rec = self._find_by_id(record_id)
        if not rec:
            raise MockApiError("InvalidRecordId", "The record id is invalid: %s" % record_id, 400)
        rr = params.get("RR") or rec["RR"]
        rtype = (params.get("Type") or rec["Type"]).upper()
        value = params.get("Value") if params.get("Value") is not None else rec["Value"]
        line = params.get("Line") or rec["Line"]
        try:
            ttl = int(params.get("TTL") or rec["TTL"])
        except ValueError:
            raise MockApiError("InvalidParameter", "TTL must be an integer.", 400)
        with self._lock:
            for rid, other in self._records.items():
                if rid == record_id:
                    continue
                if (other["RR"], other["Type"].upper(), other["Line"], other["Value"]) == (rr, rtype, line, value):
                    raise MockApiError("DomainRecordDuplicate", "The DNS record already exists.", 400)
            self._records[record_id].update({
                "RR": rr, "Type": rtype, "Value": value, "Line": line, "TTL": ttl,
            })
        return {"RequestId": _request_id(), "RecordId": record_id}

    def _delete(self, params):
        record_id = params.get("RecordId")
        if not record_id:
            raise MockApiError("MissingParameter", "The parameter RecordId is mandatory.", 400)
        with self._lock:
            if record_id not in self._records:
                raise MockApiError("InvalidRecordId", "The record id is invalid: %s" % record_id, 400)
            self._records.pop(record_id, None)
        return {"RequestId": _request_id(), "RecordId": record_id}


# ---------------------------------------------------------------------- 模块级便捷入口

_active = None


def start_mock(host="127.0.0.1", port=0, verbose=False):
    """启动内嵌 mock（后台线程），返回 MockAlidns；重复调用会替换当前活动实例。"""
    global _active
    _active = MockAlidns(host=host, port=port, verbose=verbose).start()
    return _active


def active():
    return _active


def _require():
    if _active is None:
        raise RuntimeError("mock_alidns 尚未启动：先调用 start_mock()")
    return _active


def set_record(rr, rtype, line, value):
    """【测试辅助】把 (rr, type, line) 收敛为单条记录。"""
    return _require().set_record(rr, rtype, line, value)


def add_record(rr, rtype, line, value, ttl=DEFAULT_TTL):
    """【测试辅助】新增一条记录（允许同组多条）。"""
    return _require().add_record(rr, rtype, line, value, ttl)


def get_record(rr, rtype=None, line=None):
    """【测试辅助】读第一条匹配记录（副本）。"""
    return _require().get_record(rr, rtype, line)


def get_all(rr=None, rtype=None, line=None):
    """【测试辅助】读全部匹配记录。"""
    return _require().get_all(rr, rtype, line)


def delete_record(rr, rtype=None, line=None):
    """【测试辅助】删除匹配记录，返回条数。"""
    return _require().delete_record(rr, rtype, line)


def dump_zone():
    """【测试辅助】全量 zone（副本）。"""
    return _require().dump_zone()


def clear_zone():
    _require().clear_zone()


def set_fault(action, **kwargs):
    """【测试辅助】注入故障，见 MockAlidns.set_fault。"""
    return _require().set_fault(action, **kwargs)


def clear_faults():
    _require().clear_faults()


def calls(action=None, rr=None):
    """【测试辅助】调用次数。"""
    return _require().calls(action=action, rr=rr)


def call_log(action=None, rr=None):
    """【测试辅助】调用明细（含时间戳）。"""
    return _require().call_log(action=action, rr=rr)


def api_url(base, action, omit=(), **params):
    """构造一条带"伪签名"的 GET URL，供测试直接发原始 HTTP 请求。"""
    query = {
        "Format": "JSON", "Version": API_VERSION, "AccessKeyId": "mock-ak",
        "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
        "SignatureNonce": "mock-nonce",
        "Timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Action": action, "Signature": "bW9jaw==",
    }
    query.update(params)
    for key in omit:
        query.pop(key, None)
    return base + "?" + urllib.parse.urlencode(query)


# ---------------------------------------------------------------------- CLI

def seed_zone(srv, mode):
    """预置 zone（手工调试用）：primary = 主站在线；backup = 已切到备站。"""
    now = _utc_now()
    if mode == "primary":
        srv.set_record("www", "A", "default", "172.64.52.95")
        srv.add_record("www", "A", "default", "162.159.44.17")
        srv.set_record("www", "A", "oversea", "104.19.184.186")
        srv.add_record("www", "A", "oversea", "172.66.216.152")
        srv.set_record("starkeeper", "A", "default", "172.64.52.95")
        srv.set_record("_dr-pi", "TXT", "default", json.dumps({
            "v": 1, "ts": now, "who": "pi", "seq": 1, "verdict": "healthy", "net": "ok",
            "mode": "primary", "fast": 0, "fails": 0,
            "lines": {"www.default": "200", "www.oversea": "200", "starkeeper": "200"},
        }, separators=(",", ":")))
    elif mode == "backup":
        srv.set_record("www", "A", "default", "76.76.21.21")
        srv.set_record("www", "A", "oversea", "76.76.21.21")
        srv.set_record("starkeeper", "CNAME", "default", "starkeeper-bpw.pages.dev")
        srv.set_record("_dr-snap", "TXT", "default", json.dumps({
            "v": 1, "ts": now, "who": "gh", "dir": "backup",
            "www": ["172.64.52.95", "162.159.44.17"],
            "www_oversea": ["104.19.184.186", "172.66.216.152"],
            "starkeeper": ["172.64.52.95"],
        }, separators=(",", ":")))
    return srv


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mock_alidns.py", description="离线假 Alidns 服务（仅供测试）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899, help="监听端口（默认 8899）")
    parser.add_argument("--seed", choices=("none", "primary", "backup"), default="none",
                        help="预置 zone：primary=主站在线，backup=已切备站")
    parser.add_argument("--verbose", action="store_true", help="打印每个请求")
    args = parser.parse_args(argv)
    srv = start_mock(host=args.host, port=args.port, verbose=args.verbose)
    if args.seed != "none":
        seed_zone(srv, args.seed)
    print("🔧 mock Alidns 已启动: %s（seed=%s，zone %d 条记录）" % (srv.url, args.seed, len(srv.dump_zone())))
    print("   使用: export ALI_ENDPOINT=%s ALI_KEY_ID=mock ALI_KEY_SECRET=mock" % srv.url)
    print("   Ctrl+C 退出")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        print("\n👋 mock Alidns 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

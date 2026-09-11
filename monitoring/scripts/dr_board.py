#!/usr/bin/env python3
"""容灾观察者会签板读写 + 权威状态推导（云侧与树莓派共用，纯标准库）。

用法:
  python3 dr_board.py mode www              权威推导 www 状态: primary/backup/mixed/empty
  python3 dr_board.py mode starkeeper       权威推导 starkeeper 状态（同时看 A 与 CNAME）
  python3 dr_board.py get _dr-pi            读会签板记录原始值（去引号），不存在退出 3
  python3 dr_board.py set _dr-pi '<value>'  写会签板（存在则更新/不存在则新建，TTL 600）
  python3 dr_board.py peer _dr-gh [--max-age 1200] [--target www|starkeeper]
  python3 dr_board.py --selftest            离线自检（内置假数据断言 mode/peer 语义，不联网）

  <record> 只接受 _dr-pi / _dr-gh / _dr-snap，其它名字直接拒绝（防误写）。
  peer 输出 healthy/unhealthy/stale/absent/unknown；判定类命令不因缺记录失败。

环境变量:
  ALI_KEY_ID / ALI_KEY_SECRET — 阿里云 AccessKey（必需；--selftest 除外）
  ALI_REGION                  — 阿里云区域，默认 cn-hangzhou
  ALI_ENDPOINT                — 【仅供测试】覆盖 API 端点（如 http://127.0.0.1:8899/），
                                生产环境不得设置；供 monitoring/tests 离线 mock 使用

背景:
  DR-OBSERVER-CONTRACT.md v1 规定：任何一方判断"现在处于什么状态"一律从阿里云 API
  直读权威记录推导，不读对方状态、不读 git 文件、不依赖递归 DNS 缓存。本脚本是会签板
  三个 TXT 记录（_dr-pi / _dr-gh / _dr-snap，默认线路，TTL 600）的唯一读写入口，
  云监控与树莓派共用同一份推导语义，避免两侧各自发明。

  退出码: mode 失败 → 无输出 + 1；get 记录不存在 → 3、API 失败 → 1；set 失败 → 1；
  peer API 失败 → 打印 unknown + 1（其余判定一律 0）。所有错误只写 stderr，不抛 traceback。
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

DOMAIN = "chenxiuniverse.top"
DEFAULT_REGION = "cn-hangzhou"
PAGE_SIZE = 100          # DescribeDomainRecords 必须带 PageSize，防默认 20 条截断
TXT_TTL = 600
HTTP_TIMEOUT = 10        # 所有网络调用统一超时
DEFAULT_MAX_AGE = 1200   # peer 新鲜度阈值（秒）
ALLOWED_RECORDS = ("_dr-pi", "_dr-gh", "_dr-snap")

# 备站 IP 集（与 failover-dns.py 保持一致，契约 §一）
VERCEL_IPS = ["76.76.21.21"]
GH_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]
BACKUP_SET = set(VERCEL_IPS) | set(GH_PAGES_IPS)


class BoardError(Exception):
    """会签板 / 阿里云 API 失败。调用方只需写 stderr，绝不向上抛 traceback。"""


# ---------------------------------------------------------------------------
# 阿里云 DNS API（与 resolve_primary.py 同构：GET + HMAC-SHA1，签名 base64）
# ---------------------------------------------------------------------------

def enc(s) -> str:
    return urllib.parse.quote(str(s), safe="~")


def endpoint() -> str:
    # 测试接缝（仅供离线 mock，生产环境不得设置 ALI_ENDPOINT）：
    # 覆盖默认端点，如 ALI_ENDPOINT=http://127.0.0.1:8899/
    override = (os.environ.get("ALI_ENDPOINT") or "").strip()
    if override:
        return override if override.endswith("/") else override + "/"
    region = os.environ.get("ALI_REGION") or DEFAULT_REGION
    return "https://alidns.{}.aliyuncs.com/".format(region)


def call(action: str, **extra) -> dict:
    """调用阿里云 DNS API。缺少凭据 / 网络失败 / API 报错一律抛 BoardError。"""
    key_id = os.environ.get("ALI_KEY_ID", "")
    key_secret = os.environ.get("ALI_KEY_SECRET", "")
    if not key_id or not key_secret:
        raise BoardError("缺少环境变量 ALI_KEY_ID / ALI_KEY_SECRET")
    params = {
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Action": action,
    }
    params.update(extra)
    qs = "&".join("{}={}".format(enc(k), enc(v)) for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + urllib.parse.quote(qs, safe="~")
    sig = hmac.new((key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    qs += "&Signature=" + enc(base64.b64encode(sig).decode())
    try:
        with urllib.request.urlopen(endpoint() + "?" + qs, timeout=HTTP_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise BoardError("{} HTTP {} {}".format(action, e.code, detail))
    except Exception as e:  # 超时 / DNS / 连接 / JSON 解析
        raise BoardError("{} 调用失败: {}: {}".format(action, type(e).__name__, e))
    if not isinstance(data, dict):
        raise BoardError("{} 响应不是 JSON 对象".format(action))
    if data.get("Code"):  # 阿里云错误响应（HTTP 200 也可能带 Code）
        raise BoardError("{} API 错误: {}: {}".format(action, data.get("Code"), data.get("Message")))
    return data


def list_records(rr: str, type_keyword=None) -> list:
    """DescribeDomainRecords（强制 PageSize=100），返回 RR 精确匹配的记录 dict 列表。

    响应 body / domain_records 做空值保护（阿里云在无匹配记录时返回 null）。
    """
    extra = {"DomainName": DOMAIN, "RRKeyWord": rr, "PageSize": PAGE_SIZE}
    if type_keyword:
        extra["TypeKeyWord"] = type_keyword
    data = call("DescribeDomainRecords", **extra)
    body = data.get("DomainRecords")
    if not isinstance(body, dict):
        return []
    raw = body.get("Record")
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and (r.get("RR") or "") == rr]


def line_of(rec: dict) -> str:
    return (rec.get("Line") or "default").strip().lower() or "default"


def find_txt_record(record: str):
    """默认线路上该会签记录的 dict；不存在返回 None。同名多条取第一条（告警）。"""
    recs = [r for r in list_records(record, "TXT") if line_of(r) == "default"]
    if len(recs) > 1:
        sys.stderr.write("⚠ {} 存在 {} 条 TXT 记录，取第一条\n".format(record, len(recs)))
    return recs[0] if recs else None


def read_record_value(record: str):
    """会签板原始值（去空白/去外层引号）；记录不存在或值为空返回 None。"""
    rec = find_txt_record(record)
    if not rec:
        return None
    value = normalize_txt_value(rec.get("Value"))
    return value or None


def check_record_name(record: str) -> None:
    """记录名白名单，防误写其它 DNS 记录。"""
    if record not in ALLOWED_RECORDS:
        raise BoardError("非法记录名 {}（仅允许 {}）".format(record, " / ".join(ALLOWED_RECORDS)))


# ---------------------------------------------------------------------------
# 权威状态推导（契约 §一，与 failover-dns.detect_state 同语义）
# ---------------------------------------------------------------------------

def detect_state(values) -> str:
    """按 IP 集合判定：全部备站=backup；混入主站=mixed；全主站=primary；无=empty。"""
    ips = set(values)
    if not ips:
        return "empty"
    if ips & BACKUP_SET:
        return "backup" if ips <= BACKUP_SET else "mixed"
    return "primary"


def derive_www_mode(records) -> str:
    """从 www 的 A 记录（default + oversea 两条线路）推导权威模式。"""
    ips = []
    for rec in records:
        if (rec.get("Type") or "").upper() != "A":
            continue
        if line_of(rec) not in ("default", "oversea"):
            continue
        value = (rec.get("Value") or "").strip()
        if value:
            ips.append(value)
    return detect_state(ips)


def derive_starkeeper_mode(records) -> str:
    """从 starkeeper default 线路推导：CNAME 存在=backup，A 存在=primary，并存=mixed。"""
    has_a = False
    has_cname = False
    for rec in records:
        if line_of(rec) != "default":
            continue
        rtype = (rec.get("Type") or "").upper()
        if rtype == "A":
            has_a = True
        elif rtype == "CNAME":
            has_cname = True
    if has_a and has_cname:
        return "mixed"
    if has_cname:
        return "backup"
    if has_a:
        return "primary"
    return "empty"


# ---------------------------------------------------------------------------
# 会签板读取容错 + peer 判定（契约 §二/§三）
# ---------------------------------------------------------------------------

def unescape_txt(value: str) -> str:
    """反转义 DNS TXT 表示格式里的 \\" 与 \\\\。

    实测（2026-09-11 真实 API）：写入 {"a":1} 读回 {\\"a\\":1}（阿里云自动转义内层双引号）；
    写入 "quoted" 读回 quoted（外层引号被剥掉）。因此读取端必须反转义，
    否则 JSON 解析失败 → peer 误判 absent → 恢复闸门失效（会签通道等于不通）。
    只处理 \\" 与 \\\\（载荷是单行 ASCII JSON，不产生 \\n 等其它转义）。
    """
    out = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n and value[i + 1] in ('"', "\\"):
            out.append(value[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def normalize_txt_value(raw) -> str:
    """TXT 读取容错：去首尾空白 → 剥外层双引号 → 反转义内层引号（见 unescape_txt）。"""
    if raw is None:
        return ""
    value = str(raw).strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1].strip()
    return unescape_txt(value)


def parse_ts(value):
    """解析 UTC ISO8601（契约格式 %Y-%m-%dT%H:%M:%SZ）；失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        dt = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def judge_target(lines, target: str) -> str:
    """契约 §三 目标感知语义：只看 lines 里该目标的线路条目。

    全部为 2xx/3xx/4xx → healthy；任一条为 000 或 5xx → unhealthy；
    该目标一个 key 都没有 → unknown。

    注：契约 §2.1 示例里 starkeeper 的 key 无线路后缀（"starkeeper"），而 §3 写作
    "以 <t>. 开头"；此处两种形态都接受（key == t 或 key.startswith(t + ".")），
    详见验收报告中的契约不一致说明。
    """
    if not isinstance(lines, dict):
        return "unknown"
    keys = [
        k for k in lines.keys()
        if isinstance(k, str) and (k == target or k.startswith(target + "."))
    ]
    if not keys:
        return "unknown"
    healthy_all = True
    for key in keys:
        code = str(lines.get(key, "")).strip()
        if code == "000":
            return "unhealthy"
        if not code.isdigit():
            healthy_all = False  # 非数字码无法证明健康，保守判不健康
            continue
        num = int(code)
        if num >= 500:
            return "unhealthy"
        if not 200 <= num < 500:
            healthy_all = False
    return "healthy" if healthy_all else "unhealthy"


def evaluate_peer(raw, max_age: int = DEFAULT_MAX_AGE, target=None, now=None) -> str:
    """推导 peer 结论：absent / stale / unknown / healthy / unhealthy。

    raw 为 TXT 原始值（可带引号）；解析失败视为不存在（契约 §2.2 读取容错）。
    """
    value = normalize_txt_value(raw)
    if not value:
        return "absent"
    try:
        obj = json.loads(value)
    except Exception:
        return "absent"
    if not isinstance(obj, dict):
        return "absent"
    ts = parse_ts(obj.get("ts"))
    if ts is None:
        return "stale"  # 无法确认新鲜度的记录不可信，按过期处理
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if (current - ts).total_seconds() > max_age:
        return "stale"
    if target:
        return judge_target(obj.get("lines"), target)
    verdict = obj.get("verdict")
    if verdict in ("healthy", "unhealthy"):
        return verdict
    return "unknown"


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_mode(target: str) -> int:
    try:
        if target == "www":
            mode = derive_www_mode(list_records("www", "A"))
        else:
            # 契约 §一：starkeeper 要同时看 A 与 CNAME，查询时不限定 TypeKeyWord
            mode = derive_starkeeper_mode(list_records("starkeeper"))
    except BoardError as e:
        sys.stderr.write("❌ mode {} 失败: {}\n".format(target, e))
        return 1  # 无输出 + 退出 1，调用方可回退旧逻辑
    print(mode)
    sys.stderr.write("📡 权威推导 {} = {}\n".format(target, mode))
    return 0


def cmd_get(record: str) -> int:
    try:
        check_record_name(record)
        value = read_record_value(record)
    except BoardError as e:
        sys.stderr.write("❌ get {} 失败: {}\n".format(record, e))
        return 1
    if value is None:
        sys.stderr.write("⚠ get {}: 记录不存在\n".format(record))
        return 3
    print(value)
    return 0


def cmd_set(record: str, value: str) -> int:
    try:
        check_record_name(record)
        if not value.strip():
            raise BoardError("值不能为空")
        size = len(value.encode("utf-8"))
        if size > 255:
            sys.stderr.write("⚠ set {}: 值 {} 字节超过契约 255 上限，写入可能被阿里云拒绝\n".format(record, size))
        existing = find_txt_record(record)
        if existing:
            call(
                "UpdateDomainRecord",
                RecordId=existing.get("RecordId", ""),
                RR=record,
                Type="TXT",
                Value=value,
                Line="default",
                TTL=TXT_TTL,
            )
            verb = "更新"
        else:
            call(
                "AddDomainRecord",
                DomainName=DOMAIN,
                RR=record,
                Type="TXT",
                Value=value,
                Line="default",
                TTL=TXT_TTL,
            )
            verb = "新建"
    except BoardError as e:
        sys.stderr.write("❌ set {} 失败: {}\n".format(record, e))
        return 1
    sys.stderr.write("✅ set {}: {}成功 (TTL {}s, {} 字节)\n".format(record, verb, TXT_TTL, size))
    return 0


def cmd_peer(record: str, max_age: int, target) -> int:
    try:
        check_record_name(record)
    except BoardError as e:
        sys.stderr.write("❌ peer {}: {}\n".format(record, e))
        return 1
    try:
        raw = read_record_value(record)
    except BoardError as e:
        sys.stderr.write("❌ peer {} 读取失败: {}\n".format(record, e))
        print("unknown")  # 契约 §三：API 失败打印 unknown + 退出 1
        return 1
    verdict = evaluate_peer(raw, max_age=max_age, target=target)
    print(verdict)
    sys.stderr.write("📡 peer {} (max-age={}s{}) → {}\n".format(
        record, max_age, ", target={}".format(target) if target else "", verdict))
    return 0


def parse_peer_args(args):
    """解析 peer 的 <record> 与 --max-age/--target，返回 (record, max_age, target)。"""
    record = None
    max_age = DEFAULT_MAX_AGE
    target = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--max-age":
            if i + 1 >= len(args):
                raise ValueError("--max-age 缺少参数")
            try:
                max_age = int(args[i + 1])
            except ValueError:
                raise ValueError("--max-age 必须是整数: {}".format(args[i + 1]))
            if max_age <= 0:
                raise ValueError("--max-age 必须为正数")
            i += 2
            continue
        if arg == "--target":
            if i + 1 >= len(args) or args[i + 1] not in ("www", "starkeeper"):
                raise ValueError("--target 只接受 www / starkeeper")
            target = args[i + 1]
            i += 2
            continue
        if arg.startswith("--"):
            raise ValueError("未知参数: {}".format(arg))
        if record is not None:
            raise ValueError("多余的位置参数: {}".format(arg))
        record = arg
        i += 1
    if record is None:
        raise ValueError("缺少 <record>")
    return record, max_age, target


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------

def cmd_selftest() -> int:
    """内置假数据断言 mode 推导 + peer 目标语义（不联网、不需要凭据）。"""
    base = datetime.datetime(2026, 9, 11, 10, 0, 0, tzinfo=datetime.timezone.utc)
    passed = []
    failed = []

    def check(cond, label):
        (passed if cond else failed).append(label)
        print("{} {}".format("✅" if cond else "❌", label))

    def fake(rr, rtype, value, line="default"):
        return {"RR": rr, "Type": rtype, "Value": value, "Line": line}

    def board(verdict="healthy", ts_offset=-60, lines=None):
        ts = (base + datetime.timedelta(seconds=ts_offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return json.dumps({
            "v": 1, "ts": ts, "who": "pi", "seq": 1, "verdict": verdict,
            "net": "ok", "mode": "primary", "fast": 0, "fails": 0, "lines": lines or {},
        }, separators=(",", ":"))

    print("📡 dr_board --selftest（离线自检，不联网、不需要凭据）")

    # --- mode www ---
    check(derive_www_mode([]) == "empty", "mode www: 无记录 → empty")
    check(derive_www_mode([
        fake("www", "A", "104.26.8.55"),
        fake("www", "A", "172.67.70.227"),
        fake("www", "A", "104.19.184.186", "oversea"),
    ]) == "primary", "mode www: 两条线路均主站 IP → primary")
    check(derive_www_mode([
        fake("www", "A", "76.76.21.21"),
        fake("www", "A", "185.199.111.153", "oversea"),
    ]) == "backup", "mode www: 两条线路全部备站 IP → backup")
    check(derive_www_mode([
        fake("www", "A", "76.76.21.21"),
        fake("www", "A", "104.19.184.186", "oversea"),
    ]) == "mixed", "mode www: 一条备站一条主站 → mixed")
    check(derive_www_mode([
        fake("www", "A", "76.76.21.21"),
        fake("www", "A", "104.26.8.55"),
    ]) == "mixed", "mode www: 同线路内混合 → mixed")
    check(derive_www_mode([fake("www", "CNAME", "example.com")]) == "empty",
          "mode www: CNAME 不计入 A 判据 → empty")
    check(detect_state(["185.199.108.153"]) == "backup", "detect_state: GH Pages 属于备站 IP 集")

    # --- mode starkeeper ---
    check(derive_starkeeper_mode([]) == "empty", "mode starkeeper: 无记录 → empty")
    check(derive_starkeeper_mode([fake("starkeeper", "A", "172.64.52.95")]) == "primary",
          "mode starkeeper: default A → primary")
    check(derive_starkeeper_mode([fake("starkeeper", "CNAME", "starkeeper-bpw.pages.dev")]) == "backup",
          "mode starkeeper: default CNAME → backup")
    check(derive_starkeeper_mode([
        fake("starkeeper", "A", "172.64.52.95"),
        fake("starkeeper", "CNAME", "starkeeper-bpw.pages.dev"),
    ]) == "mixed", "mode starkeeper: A 与 CNAME 并存 → mixed")
    check(derive_starkeeper_mode([
        fake("starkeeper", "CNAME", "starkeeper-bpw.pages.dev", "oversea"),
    ]) == "empty", "mode starkeeper: oversea 线路不影响 default 判定")

    # --- TXT 读取容错 ---
    check(normalize_txt_value('  "abc"  ') == "abc", "TXT 容错: 去空白 + 剥外层引号")
    check(normalize_txt_value('"  {"v":1}  "') == '{"v":1}', "TXT 容错: 剥引号后再去空白")
    check(normalize_txt_value(None) == "", "TXT 容错: None → 空")
    # 阿里云 TXT 往返转义（2026-09-11 真实 API 实测）：写 {"a":1} 读回 {\"a\":1}
    check(normalize_txt_value('{\\"v\\":1,\\"lines\\":{\\"www.default\\":\\"200\\"}}')
          == '{"v":1,"lines":{"www.default":"200"}}', "TXT 容错: 反转义内层引号（阿里云表示格式）")
    check(normalize_txt_value('"{\\"v\\":1}"') == '{"v":1}', "TXT 容错: 外层引号 + 内层转义同时存在")
    check(normalize_txt_value('{\\"a\\":\\"x\\\\\\\\y\\"}') == '{"a":"x\\\\y"}', "TXT 容错: 双反斜杠还原")
    check(normalize_txt_value("plain") == "plain", "TXT 容错: 无转义原样返回")

    # --- peer 缺省语义（整条 verdict） ---
    check(evaluate_peer(board(ts_offset=-60), now=base) == "healthy", "peer: 新鲜 healthy → healthy")
    check(evaluate_peer(board(verdict="unhealthy", ts_offset=-60), now=base) == "unhealthy",
          "peer: 新鲜 unhealthy → unhealthy")
    check(evaluate_peer(board(verdict="unknown", ts_offset=-60), now=base) == "unknown",
          "peer: 新鲜但 verdict=unknown → unknown")
    check(evaluate_peer(board(ts_offset=-1199), now=base) == "healthy",
          "peer: 默认 max-age=1200 内不 stale")
    check(evaluate_peer(board(ts_offset=-1201), now=base) == "stale", "peer: 超 max-age → stale")
    check(evaluate_peer(board(ts_offset=-100), max_age=60, now=base) == "stale",
          "peer: --max-age 生效")
    check(evaluate_peer("not json", now=base) == "absent", "peer: JSON 解析失败 → absent")
    check(evaluate_peer("", now=base) == "absent", "peer: 值为空 → absent")
    check(evaluate_peer(None, now=base) == "absent", "peer: 记录不存在 → absent")
    check(evaluate_peer(json.dumps({"ts": "garbage", "verdict": "healthy"}), now=base) == "stale",
          "peer: ts 不可解析 → stale")

    # --- peer --target 目标语义 ---
    lines_ok = {"www.default": "200", "www.oversea": "301", "starkeeper": "404"}
    check(evaluate_peer(board(lines=lines_ok), target="www", now=base) == "healthy",
          "peer --target www: 全部 2xx/3xx/4xx → healthy")
    check(evaluate_peer(board(lines={"www.default": "200", "www.oversea": "000"}), target="www", now=base)
          == "unhealthy", "peer --target www: 任一 000 → unhealthy")
    check(evaluate_peer(board(lines={"www.default": "503"}), target="www", now=base) == "unhealthy",
          "peer --target www: 任一 5xx → unhealthy")
    check(evaluate_peer(board(verdict="unhealthy", lines={"www.default": "200"}), target="starkeeper", now=base)
          == "unknown", "peer --target starkeeper: 无该目标 key → unknown")
    check(evaluate_peer(board(lines={"starkeeper": "200"}), target="starkeeper", now=base) == "healthy",
          "peer --target starkeeper: 兼容无线路后缀 key（契约 §2.1 示例）")
    check(evaluate_peer(board(lines={"starkeeper.default": "200"}), target="starkeeper", now=base) == "healthy",
          "peer --target starkeeper: 兼容带线路后缀 key")
    check(evaluate_peer(board(lines={"www.default": "abc"}), target="www", now=base) == "unhealthy",
          "peer --target www: 非数字码保守判 unhealthy")
    check(evaluate_peer(board(lines={"www.default": "200"}, ts_offset=-9999), target="www", now=base) == "stale",
          "peer --target: stale 优先于目标语义")
    check(evaluate_peer(board(verdict="unknown", lines={"www.default": "200"}, ts_offset=-60), target="www", now=base)
          == "healthy", "peer --target: 忽略 verdict，只看 lines")

    # --- 记录名白名单 ---
    try:
        check_record_name("_dr-evil")
        check(False, "白名单: 拒绝 _dr-evil")
    except BoardError:
        check(True, "白名单: 拒绝 _dr-evil")
    try:
        for name in ALLOWED_RECORDS:
            check_record_name(name)
        check(True, "白名单: 接受 _dr-pi / _dr-gh / _dr-snap")
    except BoardError:
        check(False, "白名单: 接受 _dr-pi / _dr-gh / _dr-snap")

    total = len(passed) + len(failed)
    if failed:
        print("❌ selftest 失败: {}/{} 项未通过".format(len(failed), total))
        for label in failed:
            print("   - {}".format(label))
        return 1
    print("✅ selftest 全部通过: {}/{}".format(total, total))
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def print_usage() -> None:
    sys.stderr.write(__doc__ + "\n")


def _utf8_stdio() -> None:
    """Windows / 重定向场景下保证 emoji 输出不因编码崩溃（失败则忽略）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main() -> int:
    _utf8_stdio()
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        return cmd_selftest()
    if not args:
        print_usage()
        return 2
    cmd = args[0]
    if cmd == "mode" and len(args) == 2 and args[1] in ("www", "starkeeper"):
        return cmd_mode(args[1])
    if cmd == "get" and len(args) == 2:
        return cmd_get(args[1])
    if cmd == "set" and len(args) == 3:
        return cmd_set(args[1], args[2])
    if cmd == "peer" and len(args) >= 2:
        try:
            record, max_age, target = parse_peer_args(args[1:])
        except ValueError as e:
            sys.stderr.write("❌ peer 参数错误: {}\n".format(e))
            print_usage()
            return 2
        return cmd_peer(record, max_age, target)
    sys.stderr.write("❌ 未知或参数不足的命令: {}\n".format(" ".join(args)))
    print_usage()
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # 最后兜底：绝不把 traceback 抛给调用方
        sys.stderr.write("❌ 未预期错误: {}: {}\n".format(type(e).__name__, e))
        sys.exit(1)

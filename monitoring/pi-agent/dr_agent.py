#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dr-agent — 树莓派侧容灾观察者 / 切换执行器（纯标准库，零第三方依赖）。

契约：monitoring/DR-OBSERVER-CONTRACT.md（已冻结，不得违背）
运行环境：Raspberry Pi Zero W（armv6 / 512MB / 单核 1GHz），systemd 常驻，
          不安装任何 pip 包（只允许 Python 标准库 + 系统 curl）。

职责
  - 30s 一个 tick 的常驻循环：slow 模式做 TCP 轻探，周期性做完整探测；
    TCP 轻探连续失败或完整探测判不健康 → 升入 fast 模式，每 tick 完整探测。
  - 完整探测：权威 A 记录（阿里云 API）→ curl --resolve 直连 + 递归 DNS 对照
    + 自身网络对照探针（223.5.5.5 / 两个中立站点 / vercel-test 备站验证域）。
  - 切换（阶段一默认只切不恢复）：连续 3 次完整探测不健康 + 权威推导 primary
    + 自身网络正常 → 按契约切 www（default/oversea → Vercel IP）与 starkeeper
    （default → CNAME starkeeper-bpw.pages.dev），切换前 best-effort 写 _dr-snap。
  - 会签板：每轮写 _dr-pi（含 temp/up），读 _dr-snap 与 _dr-gh（peer 判定）。
  - 时钟防线：Pi Zero W 无 RTC，用 HTTP Date 头校时，偏差超限拒绝一切 DNS 写。
  - SMTP 告警 + 每日 08:00 心跳邮件；同类告警 30 分钟节流。

用法
  python3 dr_agent.py --loop                  # systemd 常驻
  python3 dr_agent.py --once                  # 只跑一轮后退出
  python3 dr_agent.py --once --dry-run        # 不写 DNS / 不写 TXT / 不发邮件
  python3 dr_agent.py --ticks 5               # 同一进程内连续跑 5 轮 tick 后退出
  python3 dr_agent.py --status                # 打印上次状态与最近判定
  python3 dr_agent.py --selftest              # 环境自检（✅ ⚠ ❌）
  python3 dr_agent.py --config /etc/dr-agent.env --state-dir /var/lib/dr-agent

设计要点
  - 绝不因为会签板 / 告警 / 快照的任何失败而阻断切换（契约第五节）。
  - 状态文件仅在内容变化时写盘（SD 卡保护）；日志 RotatingFileHandler（512KB×2）。
  - 进程重启后重置状态机（slow + 计数清零），第一轮全探重新建立判定，
    不拿旧证据切换。
  - 探测本站一律 User-Agent: chenxiuniverse-monitor/1.0（站点 middleware 会
    403 掉 curl / python-urllib 的默认 UA，误判会自伤）。
  - `_dr-pi` 心跳按 DR_BOARD_WRITE_SECONDS（默认 300s）降频：verdict/fails/fast/
    net/mode 任一变化立即写；无变化时才按最小间隔写（重启后第一轮必写一次）。
  - DR_SWITCH_ENABLED=0 时只闸住 DNS 写动作（切换/恢复都拦），探测、判定、告警、
    心跳与 state.json 全部照常 —— 供上线初期零 DNS 风险验证链路。

测试接缝（仅供离线对抗推演，生产环境不得设置）
  - 环境变量 ALI_ENDPOINT 覆盖默认 `https://alidns.{region}.aliyuncs.com/` 端点，
    指向 monitoring/tests/mock_alidns.py（如 http://127.0.0.1:8899/）。
  - `--ticks N` 在同一进程内跑 N 轮 tick，用于累积"连续 N 次不健康"等跨轮计数
    （`--once` 语义不变，约等于 --ticks 1）。
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import random
import shutil
import smtplib
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parsedate_to_datetime
from logging.handlers import RotatingFileHandler

# Windows 本地控制台为 GBK，强制 UTF-8 输出避免 emoji 打印崩溃（仓库既有风格）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

LOG = logging.getLogger("dr-agent")

APP_NAME = "dr-agent"
APP_VERSION = "1.0.0"

# ─────────────────────────── 常量（与契约 / 仓库脚本保持一致） ───────────────────────────

DOMAIN = "chenxiuniverse.top"
HOST_WWW = "www." + DOMAIN
HOST_STAR = "starkeeper." + DOMAIN
HOST_BACKUP_VERIFY = "vercel-test." + DOMAIN
PAGES_HOST = "starkeeper-bpw.pages.dev"

# 备站 IP 集（契约第一节，与 failover-dns.py 一致）
VERCEL_IPS = ["76.76.21.21"]
GH_PAGES_IPS = ["185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"]
BACKUP_SET = set(VERCEL_IPS) | set(GH_PAGES_IPS)

# 会签板（契约第二节）
BOARD_PI = "_dr-pi"
BOARD_GH = "_dr-gh"
BOARD_SNAP = "_dr-snap"
BOARD_TTL = 600
MAX_TXT_BYTES = 255

# 探测 UA：对本站必须带监控 UA，否则被自己的 WAF 403（高频探测误判 → 误切）
UA = "chenxiuniverse-monitor/1.0"
# 中立站点用普通浏览器 UA，避免第三方 WAF 把监控 UA 当爬虫
UA_NEUTRAL = ("Mozilla/5.0 (X11; Linux armv6l) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 自身网络对照探针（国内受众视角）
NEUTRAL_DNS = "223.5.5.5"
NEUTRAL_URLS = ["https://www.baidu.com/", "https://www.aliyun.com/"]

DEFAULT_CONFIG_PATH = "/etc/dr-agent.env"
DEFAULT_STATE_DIR = "/var/lib/dr-agent"

# A / CNAME 记录 TTL（与仓库脚本一致）
RECORD_TTL = 600

# 告警节流 / 心跳
ALERT_THROTTLE_SECONDS = 1800
HEARTBEAT_HOUR = 8
CLOCK_CHECK_SECONDS = 1800

CURL_BIN = shutil.which("curl")

# 全部配置项及默认值（KEY=VALUE，来自 /etc/dr-agent.env 或进程环境变量）
DEFAULT_CONFIG = {
    "ALI_KEY_ID": "",
    "ALI_KEY_SECRET": "",
    "ALI_REGION": "cn-hangzhou",
    "DR_ROLE": "switch_only",          # switch_only | full
    "DR_TARGETS": "www,starkeeper",
    "DR_TICK_SECONDS": "30",
    "DR_FULL_PROBE_SECONDS": "300",
    "DR_FAST_PROBE_SECONDS": "30",
    "DR_TCP_FAILS_TO_ESCALATE": "2",    # 兼容拼写 DR_TCP_FAILS_TO_ESCAPE
    "DR_FAILS_TO_SWITCH": "3",
    "DR_STREAK_TO_RESTORE": "3",
    "DR_MIN_DWELL_SECONDS": "1800",
    "DR_PEER_MAX_AGE_SECONDS": "1200",
    "DR_CLOCK_SKEW_MAX": "300",
    "DR_CLOCK_CHECK_SECONDS": str(CLOCK_CHECK_SECONDS),
    "DR_BOARD_WRITE_SECONDS": "300",
    "DR_SWITCH_ENABLED": "1",
    "DR_ALERT_ENABLED": "1",
    "DR_DRY_RUN": "0",
    "SMTP_SERVER": "smtp.qiye.aliyun.com",
    "SMTP_PORT": "465",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_SENDER_NAME": "晨曦的宇宙 · 树莓派观察者",
    "REPORT_TO": "",
}


class ConfigError(Exception):
    """配置错误（缺文件 / 非法值），由 main 捕获后友好退出。"""


class AliError(Exception):
    """阿里云 API 调用失败（含 HTTP 错误体摘要）。"""


# ─────────────────────────── 时间 / 判定小工具 ───────────────────────────

def utc_now_str():
    """契约要求的时间格式：UTC ISO8601 秒精度。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_epoch(ts):
    """解析契约时间戳为 epoch 秒；失败返回 None（调用方降级）。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.datetime.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        pass
    try:
        dt = datetime.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def ok_code(code):
    """健康判据（与仓库一致）：HTTP 2xx/3xx/4xx = 健康；000 或 5xx = 不健康。"""
    if not code:
        return False
    code = str(code)
    if not code.isdigit():
        return False
    return 200 <= int(code) < 500


def _int_or(value, default):
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _bool_or(value, default):
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


# ─────────────────────────── 配置加载 ───────────────────────────

def parse_env_file(path):
    """解析 systemd EnvironmentFile 风格 KEY=VALUE（容忍 export / 引号 / 行内注释）。"""
    out = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
                if value and "\\" in value:
                    # 还原 install.sh 写入的 C 风格转义（\" 与 \\）
                    chars = []
                    i = 0
                    while i < len(value):
                        if value[i] == "\\" and i + 1 < len(value) and value[i + 1] in ('"', "\\"):
                            chars.append(value[i + 1])
                            i += 2
                        else:
                            chars.append(value[i])
                            i += 1
                    value = "".join(chars)
            else:
                # 去掉行内注释（只有 " #" 形式才算注释，避免误伤 URL 片段）
                idx = value.find(" #")
                if idx >= 0:
                    value = value[:idx].strip()
            if key:
                out[key] = value
    return out


def load_config(path, explicit=False, state_dir=None):
    """加载配置：默认值 < 配置文件 < 进程环境变量（systemd EnvironmentFile 已注入环境）。"""
    values = dict(DEFAULT_CONFIG)
    file_values = {}
    if os.path.exists(path):
        try:
            file_values = parse_env_file(path)
        except Exception as e:
            raise ConfigError("配置文件解析失败 %s: %s" % (path, e))
    elif explicit:
        raise ConfigError("配置文件不存在: %s" % path)
    for key in DEFAULT_CONFIG:
        if key in file_values:
            values[key] = file_values[key]
        env_v = os.environ.get(key)
        if env_v is not None and env_v != "":
            values[key] = env_v

    # 兼容历史拼写 DR_TCP_FAILS_TO_ESCAPE
    escalate_raw = values.get("DR_TCP_FAILS_TO_ESCALATE")
    if (escalate_raw in (None, "") or escalate_raw == DEFAULT_CONFIG["DR_TCP_FAILS_TO_ESCALATE"]) \
            and file_values.get("DR_TCP_FAILS_TO_ESCAPE"):
        escalate_raw = file_values["DR_TCP_FAILS_TO_ESCAPE"]
    if os.environ.get("DR_TCP_FAILS_TO_ESCAPE"):
        escalate_raw = os.environ["DR_TCP_FAILS_TO_ESCAPE"]

    targets = []
    for part in str(values.get("DR_TARGETS", "www,starkeeper")).split(","):
        t = part.strip().lower()
        if not t:
            continue
        if t not in ("www", "starkeeper"):
            LOG.warning("⚠ 忽略未知 DR_TARGETS 目标: %s", t)
            continue
        if t not in targets:
            targets.append(t)
    if not targets:
        targets = ["www", "starkeeper"]

    role = str(values.get("DR_ROLE", "switch_only")).strip().lower()
    if role not in ("switch_only", "full"):
        LOG.warning("⚠ DR_ROLE=%s 非法，回退 switch_only", role)
        role = "switch_only"

    cfg = {
        "config_path": path,
        "state_dir": state_dir or DEFAULT_STATE_DIR,
        "ali_key_id": values.get("ALI_KEY_ID", "").strip(),
        "ali_key_secret": values.get("ALI_KEY_SECRET", "").strip(),
        "ali_region": (values.get("ALI_REGION", "cn-hangzhou") or "cn-hangzhou").strip(),
        "role": role,
        "targets": targets,
        "tick_seconds": max(5, _int_or(values.get("DR_TICK_SECONDS"), 30)),
        "full_probe_seconds": max(30, _int_or(values.get("DR_FULL_PROBE_SECONDS"), 300)),
        "fast_probe_seconds": max(5, _int_or(values.get("DR_FAST_PROBE_SECONDS"), 30)),
        "tcp_fails_to_escalate": max(1, _int_or(escalate_raw, 2)),
        "fails_to_switch": max(1, _int_or(values.get("DR_FAILS_TO_SWITCH"), 3)),
        "streak_to_restore": max(1, _int_or(values.get("DR_STREAK_TO_RESTORE"), 3)),
        "min_dwell_seconds": max(0, _int_or(values.get("DR_MIN_DWELL_SECONDS"), 1800)),
        "peer_max_age_seconds": max(60, _int_or(values.get("DR_PEER_MAX_AGE_SECONDS"), 1200)),
        "clock_skew_max": max(30, _int_or(values.get("DR_CLOCK_SKEW_MAX"), 300)),
        "clock_check_seconds": max(60, _int_or(values.get("DR_CLOCK_CHECK_SECONDS"), CLOCK_CHECK_SECONDS)),
        "board_write_seconds": max(5, _int_or(values.get("DR_BOARD_WRITE_SECONDS"), 300)),
        "switch_enabled": _bool_or(values.get("DR_SWITCH_ENABLED"), True),
        "alert_enabled": _bool_or(values.get("DR_ALERT_ENABLED"), True),
        "dry_run": _bool_or(values.get("DR_DRY_RUN"), False),
        "smtp_server": (values.get("SMTP_SERVER", "smtp.qiye.aliyun.com") or "").strip(),
        "smtp_port": max(1, _int_or(values.get("SMTP_PORT"), 465)),
        "smtp_username": (values.get("SMTP_USERNAME", "") or "").strip(),
        "smtp_password": values.get("SMTP_PASSWORD", "") or "",
        "smtp_sender_name": values.get("SMTP_SENDER_NAME", DEFAULT_CONFIG["SMTP_SENDER_NAME"]),
        "report_to": [x.strip() for x in str(values.get("REPORT_TO", "")).split(",") if x.strip()],
    }
    return cfg


def has_credentials(cfg):
    return bool(cfg.get("ali_key_id")) and bool(cfg.get("ali_key_secret"))


# ─────────────────────────── 阿里云 DNS（stdlib HMAC-SHA1 签名） ───────────────────────────

class AliDNS(object):
    """阿里云 DNS OpenAPI 极简客户端（纯标准库，签名方式沿用仓库脚本）。"""

    def __init__(self, key_id, key_secret, region="cn-hangzhou", timeout=10):
        self.key_id = key_id
        self.key_secret = key_secret
        self.region = region or "cn-hangzhou"
        self.timeout = timeout
        self.endpoint = "https://alidns.%s.aliyuncs.com/" % self.region
        # 测试接缝（仅供离线 mock，生产环境不得设置 ALI_ENDPOINT）：
        # 覆盖默认端点，如 ALI_ENDPOINT=http://127.0.0.1:8899/
        override = (os.environ.get("ALI_ENDPOINT") or "").strip()
        if override:
            self.endpoint = override if override.endswith("/") else override + "/"

    @staticmethod
    def _enc(s):
        return urllib.parse.quote(str(s), safe="~")

    def call(self, action, **extra):
        params = {
            "Format": "JSON",
            "Version": "2015-01-09",
            "AccessKeyId": self.key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Action": action,
        }
        params.update(extra)
        qs = "&".join("%s=%s" % (self._enc(k), self._enc(v)) for k, v in sorted(params.items()))
        string_to_sign = "GET&%2F&" + urllib.parse.quote(qs, safe="~")
        sig = hmac.new((self.key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
        qs += "&Signature=" + self._enc(base64.b64encode(sig).decode())
        url = self.endpoint + "?" + qs
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise AliError("HTTP %s: %s" % (e.code, body[:240]))
        except Exception as e:
            raise AliError("%s" % e)

    def records(self, rr=None, rtype=None, line=None, page_size=100):
        """查询记录（服务端只按 RR/类型模糊匹配，客户端精确过滤；Line 本地过滤更稳）。"""
        extra = {"DomainName": DOMAIN, "PageSize": page_size}
        if rr:
            extra["RRKeyWord"] = rr
        if rtype:
            extra["TypeKeyWord"] = rtype
        data = self.call("DescribeDomainRecords", **extra)
        body = data.get("DomainRecords") or {}
        out = []
        for r in body.get("Record", []) or []:
            if rr and r.get("RR") != rr:
                continue
            if rtype and r.get("Type") != rtype:
                continue
            rec_line = r.get("Line") or "default"
            if line and rec_line != line:
                continue
            out.append({
                "id": r.get("RecordId"),
                "rr": r.get("RR"),
                "type": r.get("Type"),
                "value": (r.get("Value") or "").strip(),
                "line": rec_line,
            })
        return out

    def add(self, rr, rtype, value, line="default", ttl=RECORD_TTL):
        return self.call("AddDomainRecord", DomainName=DOMAIN, RR=rr, Type=rtype,
                         Value=value, Line=line, TTL=ttl)

    def update(self, record_id, rr, rtype, value, line="default", ttl=RECORD_TTL):
        return self.call("UpdateDomainRecord", RecordId=record_id, RR=rr, Type=rtype,
                         Value=value, Line=line, TTL=ttl)

    def delete(self, record_id):
        return self.call("DeleteDomainRecord", RecordId=record_id)


# ─────────────────────────── 网络探测（curl 优先，stdlib 兜底） ───────────────────────────

def http_get_stdlib(url, ip=None, timeout=8, ua=UA):
    """标准库 HTTPS/HTTP GET：只读响应头，返回 (code, headers, err)。

    ip 非空时直连该 IP（等价 curl --resolve），SNI 仍用 URL 的 hostname。
    证书校验失败（Pi 上缺 CA 常见）降级为不校验证书继续，保证判据看 HTTP 码。
    """
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    if not host:
        return "000", {}, "bad url"
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    addr = ip or host
    sock = None
    try:
        sock = socket.create_connection((addr, port), timeout=timeout)
        if u.scheme == "https":
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                LOG.warning("⚠ %s 证书校验失败（尝试忽略证书继续，建议安装 ca-certificates）", host)
                try:
                    sock.close()
                except Exception:
                    pass
                sock = socket.create_connection((addr, port), timeout=timeout)
                sock = ssl._create_unverified_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n") % (path, host, ua)
        sock.sendall(req.encode("ascii", "replace"))
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
        lines = head.split("\r\n")
        if not lines or not lines[0]:
            return "000", {}, "empty response"
        parts = lines[0].split()
        code = parts[1] if len(parts) > 1 and parts[1].isdigit() else "000"
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return code, headers, ""
    except Exception as e:
        return "000", {}, "%s" % e
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def _curl_http_code(url, resolve=None, timeout=8, ua=UA):
    """curl 子进程取 HTTP 码（任务要求的首选路径，含 UA 与超时约束）。"""
    cmd = [CURL_BIN, "-s", "-o", os.devnull, "-w", "%{http_code}", "-A", ua,
           "--connect-timeout", "5", "--max-time", str(timeout)]
    if resolve:
        cmd += ["--resolve", resolve]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 8)
        code = (proc.stdout or "").strip()[:3]
        return code if code.isdigit() else "000"
    except Exception:
        return "000"


def probe_http(url, resolve=None, timeout=8, ua=UA):
    """取 HTTP 码：curl 优先；curl 不存在回退 stdlib ssl+socket 手写请求。

    resolve 形如 "host:443:IP"，用于绕过递归 DNS 直连指定 IP。
    """
    if CURL_BIN:
        return _curl_http_code(url, resolve=resolve, timeout=timeout, ua=ua)
    ip = None
    if resolve:
        parts = resolve.split(":")
        if len(parts) >= 3:
            ip = parts[2]
    code, _headers, err = http_get_stdlib(url, ip=ip, timeout=timeout, ua=ua)
    if code == "000" and err:
        LOG.debug("stdlib 探测 %s 失败: %s", url, err)
    return code


def tcp_probe(ip, port=443, timeout=5):
    """TCP 轻探：只做三次握手，不做 TLS / HTTP。"""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


# ─────────────────────────── 递归 DNS 查询（stdlib，UDP） ───────────────────────────

def _dns_encode_name(name):
    out = b""
    for part in name.rstrip(".").split("."):
        raw = part.encode("ascii", "replace")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _dns_read_name(data, off):
    """解析（可能带压缩指针的）域名，返回 (name, next_offset)。"""
    labels = []
    jumps = 0
    next_off = off
    while True:
        if off >= len(data):
            raise ValueError("DNS 报文截断")
        ln = data[off]
        if ln & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                raise ValueError("DNS 指针截断")
            ptr = struct.unpack(">H", data[off:off + 2])[0] & 0x3FFF
            if jumps == 0:
                next_off = off + 2
            jumps += 1
            if jumps > 12:
                raise ValueError("DNS 指针循环")
            off = ptr
            continue
        if ln == 0:
            if jumps == 0:
                next_off = off + 1
            break
        off += 1
        labels.append(data[off:off + ln].decode("ascii", "replace"))
        off += ln
    return ".".join(labels), next_off


def dns_query(server, name, qtype=1, timeout=4):
    """最小 DNS 查询（UDP，A/其它类型 rdlength 原样跳过）。

    返回 {"rcode": int, "a": [ip...], "cname": [name...], "answers": int}。
    失败抛异常，调用方降级。
    """
    tid = random.randint(1, 65535)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    packet = header + _dns_encode_name(name) + struct.pack(">HH", qtype, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _peer = sock.recvfrom(4096)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if len(data) < 12:
        raise ValueError("DNS 响应过短")
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != tid:
        raise ValueError("DNS 事务 ID 不匹配")
    rcode = flags & 0x0F
    if rcode != 0:
        raise ValueError("DNS rcode=%d" % rcode)
    off = 12
    for _ in range(qd):
        _qname, off = _dns_read_name(data, off)
        off += 4
    a_records = []
    cname_records = []
    for _ in range(an):
        _rname, off = _dns_read_name(data, off)
        if off + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rdata = data[off:off + rdlen]
        if rtype == 1 and rdlen == 4:
            a_records.append(socket.inet_ntoa(rdata))
        elif rtype == 5:
            try:
                cname_records.append(_dns_read_name(data, off)[0])
            except Exception:
                pass
        off += rdlen
    return {"rcode": rcode, "a": a_records, "cname": cname_records, "answers": an}


# ─────────────────────────── 状态持久化（仅变化时写盘） ───────────────────────────

class State(object):
    """state.json：SD 卡保护 —— 内容不变绝不写盘；写入用临时文件 + rename 原子替换。"""

    FIELDS = (
        "seq", "mode_state", "tcp_fails", "fails", "streak",
        "last_probe", "last_full_probe_epoch", "last_switch_ts", "last_switch_epoch",
        "last_switch_dir", "last_verdict", "last_net", "last_mode", "last_lines",
        "last_main_lines", "last_detail", "last_peer", "tcp_cache", "target_modes",
        "clock_skew", "clock_checked_at", "alerts", "last_heartbeat",
        "board_sig", "last_board_write_epoch",
    )

    def __init__(self, path):
        self.path = path
        self.data = {
            "seq": 0,
            "mode_state": "slow",       # slow | fast
            "tcp_fails": 0,
            "fails": 0,
            "streak": 0,
            "last_probe": "",
            "last_full_probe_epoch": 0.0,
            "last_switch_ts": "",
            "last_switch_epoch": 0.0,
            "last_switch_dir": "",
            "last_verdict": "unknown",
            "last_net": "ok",
            "last_mode": "empty",
            "last_lines": {},
            "last_main_lines": {},
            "last_detail": [],
            "last_peer": "",
            "tcp_cache": {},
            "target_modes": {},
            "clock_skew": None,
            "clock_checked_at": 0.0,
            "alerts": {},
            "last_heartbeat": "",
            "board_sig": "",
            "last_board_write_epoch": 0.0,
        }
        self._last_dump = None

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key in self.FIELDS:
                    if key in loaded:
                        self.data[key] = loaded[key]
            self._last_dump = json.dumps(self.data, sort_keys=True, ensure_ascii=False)
        except FileNotFoundError:
            pass
        except Exception as e:
            LOG.warning("⚠ 状态文件读取失败（使用默认值）: %s", e)
        return self

    def save(self, force=False):
        dump = json.dumps(self.data, sort_keys=True, ensure_ascii=False)
        if not force and dump == self._last_dump:
            return False
        tmp = self.path + ".tmp"
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True))
            os.replace(tmp, self.path)
            self._last_dump = dump
            return True
        except Exception as e:
            LOG.warning("⚠ 状态文件写入失败: %s", e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False


# ─────────────────────────── 时钟防线（Pi Zero W 无 RTC） ───────────────────────────

class ClockGuard(object):
    """用 HTTP 响应 Date 头校时；偏差超限时拒绝一切 DNS 写（阿里云签名会失效）。"""

    def __init__(self, max_skew):
        self.max_skew = max_skew
        self.skew = None
        self.checked_at = 0.0

    def update_from_headers(self, headers):
        raw = (headers or {}).get("date")
        if not raw:
            return
        try:
            server_dt = parsedate_to_datetime(raw)
        except Exception:
            return
        if server_dt is None:
            return
        if server_dt.tzinfo is None:
            server_dt = server_dt.replace(tzinfo=datetime.timezone.utc)
        self.skew = (datetime.datetime.now(datetime.timezone.utc) - server_dt).total_seconds()
        self.checked_at = time.time()

    def refresh(self, force=False):
        """主动取一次 Date 头（启动时 + 每 DR_CLOCK_CHECK_SECONDS）。"""
        if not force and (time.time() - self.checked_at) < CLOCK_CHECK_SECONDS:
            return
        for url in NEUTRAL_URLS:
            _code, headers, _err = http_get_stdlib(url, timeout=6, ua=UA_NEUTRAL)
            if headers:
                self.update_from_headers(headers)
                if self.skew is not None:
                    return
        self.checked_at = time.time()

    def allow_write(self):
        """无法测出偏差时放行（降级），测出偏差则严格执行。"""
        if self.skew is None:
            return True
        return abs(self.skew) <= self.max_skew


# ─────────────────────────── 主机信息 / 告警 ───────────────────────────

def read_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def read_disk_percent(path):
    try:
        usage = shutil.disk_usage(path)
        if usage.total <= 0:
            return None
        return round(usage.used * 100.0 / usage.total, 1)
    except Exception:
        return None


def read_sys_uptime():
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


class Alerter(object):
    """SMTP_SSL 告警；同类 30 分钟节流；DRY-RUN / 未配置时静默跳过。"""

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state

    def _configured(self):
        return bool(self.cfg["smtp_server"] and self.cfg["smtp_username"]
                    and self.cfg["smtp_password"] and self.cfg["report_to"])

    def send(self, kind, subject, body, force=False):
        if not self.cfg["alert_enabled"]:
            LOG.info("🔕 告警已关闭（DR_ALERT_ENABLED=0），跳过: %s", subject)
            return False
        if self.cfg["dry_run"]:
            LOG.info("⚠ [DRY RUN] 不发送邮件: %s", subject)
            return False
        if not self._configured():
            LOG.warning("⚠ SMTP 未配置完整，跳过邮件: %s", subject)
            return False
        now = time.time()
        alerts = self.state.data.setdefault("alerts", {})
        last = alerts.get(kind, 0)
        if not force and (now - last) < ALERT_THROTTLE_SECONDS:
            LOG.info("🔕 同类告警节流中（%s），跳过: %s", kind, subject)
            return False
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = formataddr((str(Header(self.cfg["smtp_sender_name"], "utf-8")),
                                      self.cfg["smtp_username"]))
            msg["To"] = ", ".join(self.cfg["report_to"])
            with smtplib.SMTP_SSL(self.cfg["smtp_server"], self.cfg["smtp_port"], timeout=25) as smtp:
                smtp.login(self.cfg["smtp_username"], self.cfg["smtp_password"])
                smtp.sendmail(self.cfg["smtp_username"], self.cfg["report_to"], msg.as_string())
            alerts[kind] = now
            LOG.info("📧 告警已发送: %s", subject)
            return True
        except Exception as e:
            LOG.warning("⚠ 告警邮件发送失败（不影响主流程）: %s", e)
            return False


# ─────────────────────────── 会签板（契约第二节） ───────────────────────────

def unescape_txt(value):
    """反转义阿里云 TXT 表示格式里的 \\" 与 \\\\。

    实测（2026-09-11 真实 API）：写入 {"a":1} 读回 {\\"a\\":1} —— 服务端在存储/返回时会自动
    转义内层双引号。若不反转义，json.loads 必然失败 → 会签板被误判为不存在（心跳等于白写，
    对方闸门失效）。只处理 \\" 与 \\\\（载荷是单行 ASCII JSON）。
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


def read_txt(ali, rr, log_degrade=True):
    """读 TXT 值（去空白 / 剥引号 / 反转义）；失败返回 None，绝不抛。"""
    if ali is None:
        return None
    try:
        recs = ali.records(rr, "TXT", "default")
        if not recs:
            return None
        raw = (recs[0].get("value") or "").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        raw = unescape_txt(raw)
        return raw or None
    except Exception as e:
        if log_degrade:
            LOG.warning("⚠ 读取会签板 %s 失败（降级继续）: %s", rr, e)
        return None


def read_board(ali, rr):
    """读会签板 JSON（契约 2.1）；解析失败视为不存在。"""
    raw = read_txt(ali, rr)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        LOG.warning("⚠ 会签板 %s 值不是合法 JSON，视为不存在", rr)
        return None


def write_txt(ali, rr, value):
    """写/更新 TXT（同值跳过，避免无谓 API 调用）；返回 True/False，绝不抛。"""
    if ali is None:
        return False
    try:
        recs = ali.records(rr, "TXT", "default")
        if recs:
            current = (recs[0].get("value") or "").strip()
            if len(current) >= 2 and current[0] == current[-1] and current[0] in ("'", '"'):
                current = current[1:-1]
            current = unescape_txt(current)   # 服务端会转义内层引号，比较前必须先还原
            if current == value:
                return True
            ali.update(recs[0]["id"], rr, "TXT", value, "default", BOARD_TTL)
            for extra in recs[1:]:
                try:
                    ali.delete(extra["id"])
                except Exception:
                    pass
        else:
            ali.add(rr, "TXT", value, "default", BOARD_TTL)
        LOG.info("💾 已写 %s.%s（%d 字节）", rr, DOMAIN, len(value.encode("utf-8")))
        return True
    except Exception as e:
        LOG.warning("⚠ 写会签板 %s 失败（降级继续，不影响切换）: %s", rr, e)
        return False


def read_snap(ali):
    """读快照（契约 2.2，含读取容错）。"""
    raw = read_txt(ali, BOARD_SNAP)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        LOG.warning("⚠ 快照 %s 解析失败，视为不存在", BOARD_SNAP)
        return None


def snap_has_ips(snap):
    if not isinstance(snap, dict):
        return False
    for key in ("www", "www_oversea", "starkeeper"):
        val = snap.get(key)
        if isinstance(val, list) and val:
            return True
    return False


def peer_verdict(gh, max_age):
    """契约第三节 peer 语义（本地实现，用于恢复闸门）。"""
    if not gh:
        return "absent"
    ts_epoch = parse_iso_epoch(gh.get("ts"))
    if ts_epoch is None:
        return "stale"
    if (time.time() - ts_epoch) > max_age:
        return "stale"
    verdict = gh.get("verdict")
    if verdict not in ("healthy", "unhealthy"):
        return "unknown"
    return verdict


def trim_snap_value(payload):
    """快照长度裁剪（契约 2.2：先删 starkeeper → 数组截到 2 → 截到 1）。"""
    def dump(p):
        return json.dumps(p, separators=(",", ":"), ensure_ascii=False)

    text = dump(payload)
    if len(text.encode("utf-8")) <= MAX_TXT_BYTES:
        return text
    p = dict(payload)
    p.pop("starkeeper", None)
    text = dump(p)
    if len(text.encode("utf-8")) <= MAX_TXT_BYTES:
        return text
    for keep in (2, 1):
        p2 = dict(p)
        for key in ("www", "www_oversea"):
            if isinstance(p2.get(key), list):
                p2[key] = p2[key][:keep]
        text = dump(p2)
        if len(text.encode("utf-8")) <= MAX_TXT_BYTES:
            return text
    # 极端兜底：只保留 www 的第一个 IP（仍保证 JSON 合法）
    p3 = dict(p)
    for key in list(p3.keys()):
        if key not in ("v", "ts", "who", "dir"):
            val = p3.get(key)
            if isinstance(val, list) and val:
                p3[key] = val[:1]
            else:
                p3.pop(key, None)
    return dump(p3)


def build_pi_payload(ctx):
    """构造 _dr-pi 单行紧凑 JSON（契约 2.1，字段名不可改，≤255 字节）。"""
    state = ctx.state.data
    payload = {
        "v": 1,
        "ts": utc_now_str(),
        "who": "pi",
        "seq": int(state.get("seq", 0)),
        "verdict": state.get("last_verdict", "unknown"),
        "net": state.get("last_net", "ok"),
        "mode": state.get("last_mode", "empty"),
        "fast": 1 if state.get("mode_state") == "fast" else 0,
        "fails": int(state.get("fails", 0)),
        "lines": state.get("last_lines") or {},
    }
    temp = read_temp()
    if temp is not None:
        payload["temp"] = temp
    up = int(time.monotonic() - ctx.start_mono)
    if up > 0:
        payload["up"] = up

    def dump(p):
        return json.dumps(p, separators=(",", ":"), ensure_ascii=False)

    text = dump(payload)
    if len(text.encode("utf-8")) <= MAX_TXT_BYTES:
        return text
    payload.pop("temp", None)
    text = dump(payload)
    if len(text.encode("utf-8")) <= MAX_TXT_BYTES:
        return text
    payload.pop("up", None)
    text = dump(payload)
    if len(text.encode("utf-8")) <= MAX_TXT_BYTES:
        return text
    payload["lines"] = dict((k, str(v)[:3]) for k, v in (payload.get("lines") or {}).items())
    text = dump(payload)
    LOG.warning("⚠ _dr-pi 内容超长（%d 字节），已裁剪", len(text.encode("utf-8")))
    return text


def board_write_due(ctx, now=None):
    """_dr-pi 心跳降频判定（DR_BOARD_WRITE_SECONDS）。

    规则（契约要求：对端新鲜度阈值 1200s，300s 心跳足够）：
      - verdict / fails / fast / net / mode 任一变化 → 立即写（状态变化才是对端关心的）；
      - 全部未变化 → 距上次成功写入 >= DR_BOARD_WRITE_SECONDS 才写一次心跳。
    进程重启后 board_sig 为空（build_ctx 重置），第一轮必然写一次，让对端立刻看到上线。
    返回 (是否该写, 本次状态签名)；签名由调用方在写成功后落 state。
    """
    state = ctx.state.data
    sig = "%s|%s|%s|%s|%s" % (
        state.get("last_verdict"),
        int(state.get("fails", 0)),
        "fast" if state.get("mode_state") == "fast" else "slow",
        state.get("last_net"),
        state.get("last_mode"),
    )
    if sig != (state.get("board_sig") or ""):
        return True, sig
    interval = max(5, int(ctx.cfg.get("board_write_seconds") or 300))
    last = float(state.get("last_board_write_epoch") or 0)
    if not last or ((now if now is not None else time.time()) - last) >= interval:
        return True, sig
    return False, sig


# ─────────────────────────── 权威状态推导（契约第一节） ───────────────────────────

def authoritative_www_ips(ali, line):
    if ali is None:
        return []
    try:
        return [r["value"] for r in ali.records("www", "A", line) if r["value"]]
    except Exception as e:
        LOG.warning("⚠ 权威查询 www(%s) A 记录失败: %s", line, e)
        return []


def derive_www_mode(ali):
    """返回 primary/backup/mixed/empty；API 失败返回 None（调用方视为未知）。"""
    if ali is None:
        return None
    try:
        recs = ali.records("www", "A")
    except Exception as e:
        LOG.warning("⚠ 权威查询 www A 记录失败: %s", e)
        return None
    values = set(r["value"] for r in recs if r["value"])
    if not values:
        return "empty"
    if values <= BACKUP_SET:
        return "backup"
    if not (values & BACKUP_SET):
        return "primary"
    return "mixed"


def derive_starkeeper_mode(ali):
    """契约第一节：default 线路 CNAME=backup；A=primary；并存=mixed；无=empty。"""
    if ali is None:
        return None
    try:
        recs = ali.records("starkeeper")
    except Exception as e:
        LOG.warning("⚠ 权威查询 starkeeper 记录失败: %s", e)
        return None
    a_recs = [r for r in recs if r["type"] == "A" and r["line"] == "default"]
    c_recs = [r for r in recs if r["type"] == "CNAME" and r["line"] == "default"]
    if a_recs and c_recs:
        return "mixed"
    if c_recs:
        return "backup"
    if a_recs:
        return "primary"
    return "empty"


def aggregate_mode(target_modes, fallback="empty"):
    """把各目标 mode 汇总为契约允许的单个值（用于 _dr-pi.mode）。"""
    values = [m for m in target_modes.values() if m and m != "unknown"]
    if not values:
        return fallback
    if all(m == "primary" for m in values):
        return "primary"
    if all(m == "backup" for m in values):
        return "backup"
    if all(m == "empty" for m in values):
        return "empty"
    return "mixed"


# ─────────────────────────── 完整探测 ───────────────────────────

def probe_net(ctx):
    """自身网络对照探针：223.5.5.5 DNS + 两个中立站点 + vercel-test 备站验证域。

    判据（任务要求）：
      - 备站验证域可达而 CF 线路不可达 → 确认 CF 侧故障，可切；
      - 连中立站点与备站都不通 → net=broken / verdict=unknown，绝不切换，只告警。
    """
    detail = []
    dns_ok = False
    try:
        result = dns_query(NEUTRAL_DNS, "www.baidu.com", 1, timeout=4)
        dns_ok = bool(result.get("a"))
        detail.append("DNS(223.5.5.5) %s" % ("正常" if dns_ok else "无应答"))
    except Exception as e:
        detail.append("DNS(223.5.5.5) 失败: %s" % e)

    neutral_codes = []
    for url in NEUTRAL_URLS:
        code, headers, _err = http_get_stdlib(url, timeout=6, ua=UA_NEUTRAL)
        neutral_codes.append(code)
        ctx.clock.update_from_headers(headers)
    neutral_ok = any(ok_code(c) for c in neutral_codes)
    detail.append("中立站点 %s" % ",".join(neutral_codes))

    backup_code = probe_http("https://%s/" % HOST_BACKUP_VERIFY, timeout=8)
    backup_ok = ok_code(backup_code)
    detail.append("备站验证域 %s" % backup_code)

    if neutral_ok:
        net = "ok"
    elif backup_ok:
        # 中立站点全挂但备站验证域可达：出口网络仍通，大概率是第三方侧问题
        net = "ok"
        detail.append("中立站点异常但备站验证域可达 → 判定出口网络正常（CF 侧故障可确认）")
    else:
        net = "broken"
        detail.append("中立站点与备站验证域均不可达 → 自身网络异常，不切换")

    return {
        "net": net,
        "detail": detail,
        "dns_ok": dns_ok,
        "neutral_codes": neutral_codes,
        "backup_code": backup_code,
        "backup_ok": backup_ok,
    }


def _probe_ip_list(ips, host, label):
    """一条线路的所有 A 记录逐个 --resolve 直连；任一健康即该线路健康。

    返回 (code, all_codes)。全部失败时返回第一个非 000 码（更能说明问题）。
    """
    codes = []
    for ip in ips[:3]:
        code = probe_http("https://%s/" % host, resolve="%s:443:%s" % (host, ip), timeout=8)
        codes.append((ip, code))
        if ok_code(code):
            LOG.info("  %s %s → HTTP %s ✅", label, ip, code)
            return code, codes
        LOG.info("  %s %s → HTTP %s ❌", label, ip, code)
    for _ip, code in codes:
        if code != "000":
            return code, codes
    return "000", codes


def probe_targets(ctx, res):
    """按 DR_TARGETS 做权威推导 + 直连探测；写 res["lines"] / res["targets"]。"""
    cfg = ctx.cfg
    ali = ctx.ali
    for target in cfg["targets"]:
        if target == "www":
            mode = derive_www_mode(ali)
            if mode is None:
                res["targets"]["www"] = {"mode": "unknown", "fresh": False}
                res["api_error"] = True
                continue
            res["targets"]["www"] = {"mode": mode, "fresh": True}
            if mode == "empty":
                res["lines"]["www.default"] = "000"
                res["lines"]["www.oversea"] = "000"
                res["detail"].append("www 无 A 记录（empty）")
                continue
            for line in ("default", "oversea"):
                key = "www." + line
                ips = authoritative_www_ips(ali, line)
                if not ips:
                    res["lines"][key] = "000"
                    res["detail"].append("%s 无 A 记录" % key)
                    continue
                res["www_auth_ips"] = sorted(set(res.get("www_auth_ips", [])) | set(ips))
                code, _codes = _probe_ip_list(ips, HOST_WWW, key)
                res["lines"][key] = code
                if ok_code(code):
                    ctx.state.data.setdefault("tcp_cache", {})[key] = ips[0]
        else:
            mode = derive_starkeeper_mode(ali)
            if mode is None:
                res["targets"]["starkeeper"] = {"mode": "unknown", "fresh": False}
                res["api_error"] = True
                continue
            res["targets"]["starkeeper"] = {"mode": mode, "fresh": True}
            if mode == "empty":
                res["lines"]["starkeeper"] = "000"
                res["detail"].append("starkeeper 无记录（empty）")
                continue
            a_ips = []
            try:
                a_ips = [r["value"] for r in ali.records("starkeeper", "A", "default") if r["value"]]
            except Exception as e:
                res["detail"].append("starkeeper A 记录查询失败: %s" % e)
            if a_ips:
                code, _codes = _probe_ip_list(a_ips, HOST_STAR, "starkeeper")
                res["lines"]["starkeeper"] = code
                if ok_code(code):
                    ctx.state.data.setdefault("tcp_cache", {})["starkeeper"] = a_ips[0]
            else:
                # backup（CNAME）形态：走普通递归解析探测官方入口
                code = probe_http("https://%s/" % HOST_STAR, timeout=8)
                res["lines"]["starkeeper"] = code
                ctx.state.data.setdefault("tcp_cache", {}).pop("starkeeper", None)
                LOG.info("  starkeeper(CNAME) → HTTP %s %s", code, "✅" if ok_code(code) else "❌")


def probe_main_station(ctx, res):
    """backup 语义下额外探测快照里的主站 IP（恢复判定依据，与 GH workflow 一致）。

    结果写 res["main_lines"]（本地状态展示用，不进会签板 lines）。
    """
    if ctx.ali is None:
        return
    snap = res.get("snap")
    if not snap_has_ips(snap):
        return
    plan = []
    if "www" in ctx.cfg["targets"]:
        plan.append(("www.default", snap.get("www"), HOST_WWW))
        plan.append(("www.oversea", snap.get("www_oversea"), HOST_WWW))
    if "starkeeper" in ctx.cfg["targets"]:
        plan.append(("starkeeper", snap.get("starkeeper"), HOST_STAR))
    for key, ips, host in plan:
        if not isinstance(ips, list) or not ips:
            continue
        code, _codes = _probe_ip_list(ips[:3], host, "主站 " + key)
        res["main_lines"][key] = code


def probe_recursive(ctx, res):
    """递归解析对照：223.5.5.5 解析 www 主域名，与权威记录比对（劫持/污染信号）。

    只在权威推导为 primary 时比对：backup/mixed 时递归缓存里还是切换前的旧记录
    （TTL 内必然不一致），比对会产生假告警。
    """
    www_info = res.get("targets", {}).get("www") or {}
    if www_info.get("mode") != "primary":
        res["detail"].append("非 primary 语义，跳过递归对照（避免切换后 TTL 内的假告警）")
        return
    try:
        result = dns_query(NEUTRAL_DNS, HOST_WWW, 1, timeout=4)
        recursive = sorted(set(result.get("a") or []))
    except Exception as e:
        res["detail"].append("递归对照失败: %s" % e)
        return
    res["recursive"] = recursive
    auth = set(res.get("www_auth_ips") or [])
    if not recursive:
        res["detail"].append("递归解析(223.5.5.5) 无 A 记录")
        return
    if not auth:
        return
    if not (set(recursive) & auth):
        res["hijack"] = True
        res["detail"].append("递归解析 %s 与权威记录 %s 不一致 → 疑似劫持/污染"
                             % (",".join(recursive), ",".join(sorted(auth))))
        # 国内用户走递归解析：递归结果不可达 = 用户路径故障 → 计入不健康
        code, _codes = _probe_ip_list(recursive, HOST_WWW, "递归视角 www")
        if not ok_code(code):
            res["hijack_unhealthy"] = True
            res["detail"].append("且递归解析结果不可达（用户路径故障）→ 计入不健康")


def probe_full(ctx):
    """一轮完整探测，返回判定结果 dict（不写任何状态/看板，纯读）。"""
    cfg = ctx.cfg
    state = ctx.state.data
    res = {
        "ts": utc_now_str(),
        "lines": {},
        "main_lines": {},
        "targets": {},
        "mode": state.get("last_mode", "empty"),
        "mode_fresh": False,
        "net": "ok",
        "verdict": "unknown",
        "hijack": False,
        "recursive": [],
        "api_error": False,
        "detail": [],
        "snap": None,
    }

    net = probe_net(ctx)
    res["net"] = net["net"]
    res["net_detail"] = net["detail"]

    # 快照（backup 语义下探测主站恢复用）与 peer(_dr-gh) 判定；读不到一律降级
    res["snap"] = read_snap(ctx.ali)
    res["peer"] = peer_verdict(read_board(ctx.ali, BOARD_GH), cfg["peer_max_age_seconds"])

    if ctx.ali is None:
        res["detail"].append("无阿里云凭据：降级为递归解析视角（只读，不切换）")
        _probe_degraded(ctx, res)
    else:
        probe_targets(ctx, res)

    fresh_modes = {}
    for target, info in res["targets"].items():
        if info.get("fresh"):
            fresh_modes[target] = info["mode"]
    res["mode_fresh"] = bool(fresh_modes)
    res["mode"] = aggregate_mode(fresh_modes, fallback=state.get("last_mode", "empty"))

    if "www" in cfg["targets"]:
        probe_recursive(ctx, res)

    # backup 目标：探测快照中的主站 IP（恢复判定）
    if any((res["targets"].get(t) or {}).get("mode") == "backup" for t in cfg["targets"]):
        probe_main_station(ctx, res)

    # ── 统一判定 ──
    bad_serving = [k for k, v in res["lines"].items() if not ok_code(v)]
    main_vals = [v for v in res["main_lines"].values()]
    main_ok = all(ok_code(v) for v in main_vals) if main_vals else None

    if res["net"] == "broken":
        res["verdict"] = "unknown"
        res["detail"].append("自身网络异常 → verdict=unknown，绝不切换")
    elif not res["lines"]:
        res["verdict"] = "unknown"
        res["detail"].append("无凭据（降级视角）" if ctx.ali is None else "权威解析失败（API 异常）")
    elif not fresh_modes:
        res["verdict"] = "unknown"
        res["detail"].append("无凭据：降级视角不作为切换依据" if ctx.ali is None
                             else "权威状态推导失败（API 异常）")
    elif bad_serving:
        res["verdict"] = "unhealthy"
        res["detail"].append("不健康线路: %s" % ",".join(sorted(bad_serving)))
    elif res.get("hijack_unhealthy"):
        res["verdict"] = "unhealthy"
        res["detail"].append("递归解析结果不可达（劫持/污染影响真实用户路径）→ 计入不健康")
    elif main_ok is False:
        # 处于 backup 语义且主站仍不可达 → 恢复条件不成立，保持不健康计数
        res["verdict"] = "unhealthy"
        res["detail"].append("备站服务中但主站 IP 仍不可达（未到恢复时机）")
    else:
        res["verdict"] = "healthy"

    return res


def _probe_degraded(ctx, res):
    """无凭据时的降级探测：只用 223.5.5.5 递归结果做直连（只读，不参与切换）。"""
    for target, host in (("www", HOST_WWW), ("starkeeper", HOST_STAR)):
        if target not in ctx.cfg["targets"]:
            continue
        res["targets"][target] = {"mode": "unknown", "fresh": False}
        try:
            result = dns_query(NEUTRAL_DNS, host, 1, timeout=4)
            ips = sorted(set(result.get("a") or []))
        except Exception as e:
            res["detail"].append("%s 递归解析失败: %s" % (host, e))
            continue
        if not ips:
            continue
        key = "www.default" if target == "www" else "starkeeper"
        code, _codes = _probe_ip_list(ips, host, "递归视角 " + key)
        res["lines"][key] = code


def light_probe(ctx):
    """TCP 轻探最近一次权威查询缓存的 IP。返回 (ok, detail)。"""
    cache = ctx.state.data.get("tcp_cache") or {}
    pairs = [(k, v) for k, v in cache.items() if v]
    if not pairs:
        return True, "无缓存 IP（下一轮将做完整探测）"
    bad = []
    good = []
    for key, ip in pairs:
        if tcp_probe(ip):
            good.append("%s(%s)" % (key, ip))
        else:
            bad.append("%s(%s)" % (key, ip))
    if bad:
        return False, "TCP 轻探失败: " + ", ".join(bad)
    return True, "TCP 轻探通过: " + ", ".join(good)


# ─────────────────────────── 切换 / 恢复动作 ───────────────────────────

def set_a_records(ctx, rr, line, ips):
    """把 rr(line) 的 A 记录收敛为 ips（更新已有 + 新增 + 删多余）；无变化返回 False。"""
    ali = ctx.ali
    current = ali.records(rr, "A", line)
    if sorted(r["value"] for r in current) == sorted(ips):
        return False
    for i, ip in enumerate(ips):
        if i < len(current):
            ali.update(current[i]["id"], rr, "A", ip, line, RECORD_TTL)
        else:
            ali.add(rr, "A", ip, line, RECORD_TTL)
    for extra in current[len(ips):]:
        ali.delete(extra["id"])
    return True


def _is_record_conflict_error(err):
    """判断 add 失败是否属于"记录已存在/同名冲突"类（此类才允许退化为先删后建）。

    阿里云同名 RR+线路 只允许一条记录时，add CNAME 会返回 DomainRecordDuplicate；
    限流 / 网络 / 权限等其它错误一律不得删任何记录（防裸域）。
    """
    text = str(err).lower()
    return any(marker in text for marker in (
        "domainrecordduplicate", "domainrecordconflict",
        "recordduplicate", "recordalreadyexists",
    ))


def _delete_records_best_effort(ctx, recs, what):
    """逐条删除，失败只告警（绝不抛）。返回成功删除的条数。"""
    deleted = 0
    for rec in recs:
        try:
            ctx.ali.delete(rec["id"])
            deleted += 1
        except Exception as e:
            LOG.warning("⚠ 删除 %s 记录失败（保持现状，下一轮可收敛）: %s", what, e)
    return deleted


def starkeeper_to_backup(ctx):
    """starkeeper default → CNAME（先建后删：任何一步失败都不允许留下无记录裸域）。

    1. 先 add CNAME；成功后逐条删 A（删失败只告警 → 落到 mixed，线路仍可用）。
    2. add 失败：
       - 错误码属于"记录已存在/冲突"类 → 才允许退化为"删 A 后立即再 add"；
         二次 add 仍失败 → 告警"无记录状态，需人工介入"并返回 False；
       - 其它错误（限流/网络/权限）→ 一条记录都不许动，告警后返回 False。
    """
    ali = ctx.ali
    a_recs = ali.records("starkeeper", "A", "default")
    c_recs = ali.records("starkeeper", "CNAME", "default")
    if c_recs:
        # CNAME 已就位：只需清理多余的 A（best-effort，失败落到 mixed）
        _delete_records_best_effort(ctx, a_recs, "starkeeper A")
        return bool(a_recs)
    try:
        ali.add("starkeeper", "CNAME", PAGES_HOST, "default", RECORD_TTL)
    except Exception as e:
        if not _is_record_conflict_error(e):
            LOG.error("❌ starkeeper CNAME 写入失败（非记录冲突，未改动任何记录）: %s", e)
            ctx.alerts.send("starkeeper_switch_fail",
                            "[DR] ❌ starkeeper 切换失败（保持原记录，未留裸域）",
                            "add CNAME 失败: %s\n当前 A 记录保持原样（线路仍可用）；"
                            "请检查 AK 权限 / 限流 / 网络后重试。" % e)
            return False
        # 冲突类（常见：同名 A 记录占用）→ 退化为先删 A 再立即补 add
        LOG.warning("⚠ starkeeper CNAME 与现有记录冲突（%s）→ 先删 A 再立即补 add", e)
        _delete_records_best_effort(ctx, a_recs, "starkeeper A")
        try:
            ali.add("starkeeper", "CNAME", PAGES_HOST, "default", RECORD_TTL)
        except Exception as e2:
            LOG.error("❌ starkeeper default 处于无记录状态，需人工介入: %s", e2)
            ctx.alerts.send("starkeeper_bare",
                            "[DR] ❌ starkeeper default 无记录，需人工介入",
                            "冲突退化后二次 add 仍失败: %s\n"
                            "当前 starkeeper default 可能没有任何记录 → 国内解析会直接失败。\n"
                            "请人工在控制台补 CNAME %s（或执行 failover-dns.py backup）。" % (e2, PAGES_HOST))
            return False
        return True
    _delete_records_best_effort(ctx, a_recs, "starkeeper A")
    return True


def starkeeper_to_restore(ctx, ips):
    """starkeeper default → A 记录（先写后删：写回失败时 CNAME 保持不动，线路仍可用）。"""
    ali = ctx.ali
    c_recs = ali.records("starkeeper", "CNAME", "default")
    a_recs = ali.records("starkeeper", "A", "default")
    if not c_recs and sorted(r["value"] for r in a_recs) == sorted(ips):
        return False
    # 1) 先把 A 写回（已有则 update、不足则 add；多余的最后删）
    try:
        for i, ip in enumerate(ips):
            if i < len(a_recs):
                ali.update(a_recs[i]["id"], "starkeeper", "A", ip, "default", RECORD_TTL)
            else:
                ali.add("starkeeper", "A", ip, "default", RECORD_TTL)
        for extra in a_recs[len(ips):]:
            ali.delete(extra["id"])
    except Exception as e:
        LOG.error("❌ starkeeper A 记录写回失败（CNAME 保持不动，线路仍可用）: %s", e)
        ctx.alerts.send("starkeeper_restore_fail",
                        "[DR] ❌ starkeeper 恢复失败（CNAME 保持不动，线路仍可用）",
                        "写回 A 记录失败: %s\n未删除任何 CNAME：starkeeper 仍走备站入口，"
                        "不会出现裸域。请检查 AK 权限 / 限流 / 网络后重试。" % e)
        return False
    # 2) A 全部就位后才删 CNAME（删失败只告警：落到 mixed，下一轮可收敛）
    _delete_records_best_effort(ctx, c_recs, "starkeeper CNAME")
    return True


def verify_ips(ips, host):
    """恢复前逐 IP 可达校验（--resolve 直连，HTTP < 500 视为可用）。"""
    verified = []
    for ip in ips:
        code = probe_http("https://%s/" % host, resolve="%s:443:%s" % (host, ip), timeout=8)
        if ok_code(code):
            verified.append(ip)
        else:
            LOG.warning("    ⚠ 恢复目标 %s 不可达（HTTP %s），跳过", ip, code)
    return verified


def collect_snapshot_arrays(ctx):
    """切换前采集主站原始 IP（快照回退值）。读失败降级为空 dict。"""
    arrays = {}
    if "www" in ctx.cfg["targets"]:
        default_ips = authoritative_www_ips(ctx.ali, "default")
        oversea_ips = authoritative_www_ips(ctx.ali, "oversea")
        if default_ips:
            arrays["www"] = default_ips
        if oversea_ips:
            arrays["www_oversea"] = oversea_ips
    if "starkeeper" in ctx.cfg["targets"]:
        try:
            star_ips = [r["value"] for r in ctx.ali.records("starkeeper", "A", "default") if r["value"]]
            if star_ips:
                arrays["starkeeper"] = star_ips
        except Exception as e:
            LOG.warning("⚠ 采集 starkeeper 快照失败: %s", e)
    return arrays


def write_snap(ctx, direction, fallback_arrays):
    """写 _dr-snap（best-effort，绝不阻断切换；契约 2.2 的保留语义在此实现）。"""
    if ctx.cfg["dry_run"]:
        LOG.info("⚠ [DRY RUN] 将写快照 dir=%s 回退数组=%s", direction, json.dumps(fallback_arrays, ensure_ascii=False))
        return False
    old = read_snap(ctx.ali)
    if snap_has_ips(old):
        # 已存在有效快照 → 保留其 IP 数组（防把被攻击/黑洞的当前记录当恢复目标）
        arrays = {}
        for key in ("www", "www_oversea", "starkeeper"):
            val = old.get(key)
            if isinstance(val, list) and val:
                arrays[key] = [str(x) for x in val]
        LOG.info("💾 已存在有效快照 — 保留其 IP 数组（只更新 ts/who/dir）")
    else:
        arrays = fallback_arrays
    payload = {"v": 1, "ts": utc_now_str(), "who": "pi", "dir": direction}
    payload.update(arrays)
    value = trim_snap_value(payload)
    ok = write_txt(ctx.ali, BOARD_SNAP, value)
    if not ok:
        LOG.warning("⚠ 快照写入失败（不影响切换，恢复时以人工手册为准）")
    return ok


def _switch_body(ctx, res, planned, changes):
    state = ctx.state.data
    lines_txt = ", ".join("%s=%s" % (k, v) for k, v in sorted(res.get("lines", {}).items())) or "无"
    return (
        "树莓派观察者已执行容灾切换（阶段一：只切不恢复）。\n\n"
        "时间: %s (UTC)\n"
        "触发: 完整探测连续 %d 次不健康（阈值 %d）；权威状态 %s；自身网络 %s\n"
        "看到的 HTTP 码: %s\n"
        "递归对照: %s\n"
        "改了什么:\n  - %s\n"
        "快照: _dr-snap.%s（dir=backup, ts=%s）\n"
        "本地状态: %s\n\n"
        "如需人工恢复（阶段一手动）:\n"
        "  1) 查看快照: python3 monitoring/scripts/dr_board.py get _dr-snap\n"
        "  2) www default/oversea A 记录改回快照 IP；starkeeper default 删除 CNAME、加回快照 A 记录\n"
        "  3) 或执行: python3 monitoring/scripts/failover-dns.py restore（需 ALI_KEY_ID/SECRET）\n"
        "  4) 重启观察者: sudo systemctl restart dr-agent\n"
    ) % (
        utc_now_str(), int(state.get("fails", 0)), ctx.cfg["fails_to_switch"],
        res.get("mode"), res.get("net"), lines_txt,
        ",".join(res.get("recursive") or []) or "无",
        "\n  - ".join(changes) if changes else "（无变更）",
        DOMAIN, state.get("last_switch_ts") or utc_now_str(),
        os.path.join(ctx.cfg["state_dir"], "state.json"),
    )


def execute_backup(ctx, res, planned):
    """执行切换（仅 planned 中推导为 primary 的目标）。返回 True/False。"""
    state = ctx.state.data
    cfg = ctx.cfg
    LOG.warning("🔁 满足切换条件（连续 %s 次完整探测不健康）→ 执行切换: %s", state.get("fails"), ",".join(planned))
    if not ctx.clock.allow_write():
        LOG.error("❌ 时钟偏差 %.0fs 超过 %ds，拒绝 DNS 写操作", ctx.clock.skew or 0, cfg["clock_skew_max"])
        ctx.alerts.send("clock_skew", "[DR] ❌ 时钟偏差超限，已拒绝切换",
                        "本机时钟与 HTTP Date 偏差 %.0f 秒，超过 DR_CLOCK_SKEW_MAX=%d。\n"
                        "阿里云 API 签名会失效，已拒绝一切 DNS 写入。\n"
                        "请在树莓派上校准时间: sudo date -s ... 或安装 chrony/systemd-timesyncd。"
                        % (ctx.clock.skew or 0, cfg["clock_skew_max"]))
        return False

    arrays = collect_snapshot_arrays(ctx)
    try:
        write_snap(ctx, "backup", arrays)
    except Exception as e:
        LOG.warning("⚠ 快照写入异常（按契约不阻断切换）: %s", e)

    if cfg["dry_run"]:
        LOG.info("⚠ [DRY RUN] 将切换 %s；快照回退数组=%s", planned, json.dumps(arrays, ensure_ascii=False))
        for target in planned:
            if target == "www":
                for line in ("default", "oversea"):
                    LOG.info("⚠ [DRY RUN] www(%s) → %s", line, VERCEL_IPS)
            else:
                LOG.info("⚠ [DRY RUN] starkeeper(default) → CNAME %s", PAGES_HOST)
        return False

    changes = []
    errors = []
    for target in planned:
        try:
            if target == "www":
                for line in ("default", "oversea"):
                    current = [r["value"] for r in ctx.ali.records("www", "A", line)]
                    if sorted(current) == sorted(VERCEL_IPS):
                        changes.append("www(%s) 已是备站，跳过" % line)
                        continue
                    set_a_records(ctx, "www", line, VERCEL_IPS)
                    changes.append("www(%s): %s → %s" % (line, ",".join(sorted(current)) or "无", ",".join(VERCEL_IPS)))
            else:
                current = [r["value"] for r in ctx.ali.records("starkeeper", "A", "default")]
                if starkeeper_to_backup(ctx):
                    changes.append("starkeeper(default): A %s → CNAME %s"
                                   % (",".join(sorted(current)) or "无", PAGES_HOST))
                else:
                    changes.append("starkeeper(default) 已处于备站状态，跳过")
        except Exception as e:
            errors.append("%s: %s" % (target, e))

    now_ts = utc_now_str()
    now_epoch = time.time()
    state["last_switch_ts"] = now_ts
    state["last_switch_epoch"] = now_epoch
    state["last_switch_dir"] = "backup"

    if errors:
        LOG.error("❌ 切换失败/半切: %s", "; ".join(errors))
        ctx.alerts.send("switch_fail", "[DR] ❌ 容灾切换失败（可能半切）",
                        "目标: %s\n错误:\n  - %s\n已完成变更:\n  - %s\n"
                        "请人工核对 www / starkeeper 解析状态。"
                        % (",".join(planned), "\n  - ".join(errors),
                           "\n  - ".join(changes) if changes else "（无）"))
        return False

    state["fails"] = 0
    state["streak"] = 0
    # 缓存换成备站入口，轻探立刻盯新路径（starkeeper 切的是 CNAME，无 IP 可轻探）
    cache = state.setdefault("tcp_cache", {})
    if "www" in planned:
        cache["www.default"] = VERCEL_IPS[0]
        cache["www.oversea"] = VERCEL_IPS[0]
    if "starkeeper" in planned:
        cache.pop("starkeeper", None)
    LOG.warning("✅ 切换完成: %s", "; ".join(changes))
    ctx.alerts.send("switch_ok", "[DR] 🔁 已切换到备站", _switch_body(ctx, res, planned, changes))
    return True


def _restore_body(ctx, planned, changes, peer, dwell, snap):
    return (
        "树莓派观察者已执行恢复（DR_ROLE=full）。\n\n"
        "时间: %s (UTC)\n"
        "目标: %s\n"
        "peer(_dr-gh): %s（对方 unhealthy 会阻断恢复；stale/absent 视为失联降级放行）\n"
        "最小驻留: %s\n"
        "快照 ts: %s\n"
        "改了什么:\n  - %s\n\n"
        "如需再次人工切换: python3 monitoring/scripts/failover-dns.py backup\n"
    ) % (
        utc_now_str(), ",".join(planned), peer,
        ("%.0fs" % dwell) if dwell is not None else "无快照（视为满足）",
        (snap or {}).get("ts", "无"),
        "\n  - ".join(changes) if changes else "（无）",
    )


def maybe_restore(ctx, res):
    """恢复判定：阶段一只记录 + 告警；DR_ROLE=full 才执行（含 peer 闸门 + 驻留 + 逐 IP 校验）。"""
    cfg = ctx.cfg
    state = ctx.state.data
    if int(state.get("streak", 0)) < cfg["streak_to_restore"]:
        return
    candidates = [t for t in cfg["targets"]
                  if (res["targets"].get(t) or {}).get("mode") in ("backup", "mixed")]
    if not candidates:
        return

    snap = res.get("snap")
    peer = res.get("peer") or peer_verdict(read_board(ctx.ali, BOARD_GH), cfg["peer_max_age_seconds"])
    snap_epoch = parse_iso_epoch((snap or {}).get("ts")) if snap else None
    dwell = (time.time() - snap_epoch) if snap_epoch else None

    gates = []
    if peer == "unhealthy":
        gates.append("peer(_dr-gh) 明确不健康")
    if dwell is not None and dwell < cfg["min_dwell_seconds"]:
        gates.append("最小驻留时间未到（%.0fs < %ds）" % (dwell, cfg["min_dwell_seconds"]))

    if cfg["role"] != "full":
        LOG.warning("⚠ 恢复条件满足但 DR_ROLE=switch_only（阶段一只切不恢复）: 目标=%s peer=%s 驻留=%s 闸门=%s",
                    ",".join(candidates), peer,
                    ("%.0fs" % dwell) if dwell is not None else "无",
                    ",".join(gates) or "无")
        ctx.alerts.send("restore_gated", "[DR] ⚠ 恢复条件满足，但阶段一不执行恢复",
                        "目标: %s\n连续健康 %d 次\npeer(_dr-gh): %s\n最小驻留: %s\n"
                        "未满足闸门: %s\n\n阶段一（DR_ROLE=switch_only）只切换、不恢复；如需自动恢复请切到 DR_ROLE=full。"
                        % (",".join(candidates), int(state.get("streak", 0)), peer,
                           ("%.0fs" % dwell) if dwell is not None else "无快照（视为满足）",
                           ",".join(gates) or "无"))
        return

    if gates:
        LOG.warning("⚠ 恢复被闸门拦下: %s", "; ".join(gates))
        ctx.alerts.send("restore_gated", "[DR] ⚠ 恢复被闸门拦下",
                        "目标: %s\npeer(_dr-gh): %s\n驻留: %s\n闸门:\n  - %s\n"
                        % (",".join(candidates), peer,
                           ("%.0fs" % dwell) if dwell is not None else "无", "\n  - ".join(gates)))
        return

    if not cfg.get("switch_enabled", True):
        # DR_SWITCH_ENABLED=0：恢复路径同样只闸 DNS 写（阶段一本就不恢复，行为不变）
        LOG.warning("⚠ 本应恢复主站（%s），但 DR_SWITCH_ENABLED=0：仅告警，不写 DNS", ",".join(candidates))
        ctx.alerts.send("restore_disabled", "[DR] ⚠ 本应恢复到主站，但 DR_SWITCH_ENABLED=0（仅告警）",
                        "目标: %s\n连续健康 %d 次，闸门均通过，但 DR_SWITCH_ENABLED=0：不执行 DNS 写入。\n"
                        "如需自动恢复请设置 DR_SWITCH_ENABLED=1 并重启服务。"
                        % (",".join(candidates), int(state.get("streak", 0))))
        return

    if not snap_has_ips(snap):
        LOG.warning("⚠ 缺少有效快照，无法确定恢复目标（保持备站）")
        ctx.alerts.send("restore_gated", "[DR] ⚠ 缺少快照，恢复被拦下",
                        "目标: %s\n_dr-snap 不存在或没有有效 IP 数组，拒绝猜测恢复目标。\n"
                        "请人工恢复（见 README 手动恢复手册）。" % ",".join(candidates))
        return

    if not ctx.clock.allow_write():
        LOG.error("❌ 时钟偏差超限，拒绝恢复写操作")
        ctx.alerts.send("clock_skew", "[DR] ❌ 时钟偏差超限，已拒绝恢复",
                        "请先校准树莓派时间。")
        return

    if cfg["dry_run"]:
        LOG.info("⚠ [DRY RUN] 将恢复 %s（快照 %s）", candidates, snap.get("ts"))
        return

    changes = []
    errors = []
    for target in candidates:
        try:
            if target == "www":
                for line in ("default", "oversea"):
                    key = "www" if line == "default" else "www_oversea"
                    ips = snap.get(key) or []
                    if not ips:
                        errors.append("www(%s) 快照无 IP，跳过" % line)
                        continue
                    verified = verify_ips([str(x) for x in ips], HOST_WWW)
                    if not verified:
                        errors.append("www(%s) 快照 IP 全部不可达，跳过" % line)
                        continue
                    if set_a_records(ctx, "www", line, verified):
                        changes.append("www(%s) → %s" % (line, ",".join(verified)))
            else:
                ips = snap.get("starkeeper") or []
                if not ips:
                    errors.append("starkeeper 快照无 IP，跳过")
                    continue
                verified = verify_ips([str(x) for x in ips], HOST_STAR)
                if not verified:
                    errors.append("starkeeper 快照 IP 全部不可达，跳过")
                    continue
                if starkeeper_to_restore(ctx, verified):
                    changes.append("starkeeper(default) → A %s" % ",".join(verified))
        except Exception as e:
            errors.append("%s: %s" % (target, e))

    if errors and not changes:
        LOG.error("❌ 恢复失败: %s", "; ".join(errors))
        ctx.alerts.send("restore_fail", "[DR] ❌ 恢复失败",
                        "目标: %s\n错误:\n  - %s" % (",".join(candidates), "\n  - ".join(errors)))
        return
    if errors:
        LOG.warning("⚠ 恢复部分完成: %s", "; ".join(errors))

    try:
        write_snap(ctx, "restore", {})
    except Exception as e:
        LOG.warning("⚠ 恢复后快照写入异常（不阻断）: %s", e)

    state["streak"] = 0
    state["last_switch_ts"] = utc_now_str()
    state["last_switch_epoch"] = time.time()
    state["last_switch_dir"] = "restore"
    cache = state.setdefault("tcp_cache", {})
    if "www" in candidates:
        www_ips = [str(x) for x in (snap.get("www") or [])]
        ova_ips = [str(x) for x in (snap.get("www_oversea") or [])]
        if www_ips:
            cache["www.default"] = www_ips[0]
        if ova_ips:
            cache["www.oversea"] = ova_ips[0]
    if "starkeeper" in candidates and snap.get("starkeeper"):
        cache["starkeeper"] = str(snap["starkeeper"][0])
    LOG.warning("✅ 恢复完成: %s", "; ".join(changes) if changes else "无变更")
    ctx.alerts.send("restore_ok", "[DR] ✅ 已恢复到主站", _restore_body(ctx, candidates, changes, peer, dwell, snap))


def maybe_switch(ctx, res):
    """切换判定：连续不健康 + 推导 primary + 自身网络正常；幂等保护 backup/mixed/empty。"""
    cfg = ctx.cfg
    state = ctx.state.data
    if int(state.get("fails", 0)) < cfg["fails_to_switch"]:
        return
    now = time.time()
    last_switch = float(state.get("last_switch_epoch") or 0)
    if last_switch and (now - last_switch) < 300:
        LOG.info("🔁 距上次切换不足 300s，本轮跳过切换尝试（防抖）")
        return
    if res.get("net") != "ok":
        LOG.warning("⚠ 自身网络异常（net=%s），拒绝切换", res.get("net"))
        return

    planned = []
    refused = []
    for target in cfg["targets"]:
        info = res["targets"].get(target) or {}
        mode = info.get("mode")
        if not info.get("fresh") or mode in (None, "unknown"):
            refused.append("%s（权威状态未知）" % target)
            continue
        if mode == "primary":
            planned.append(target)
        else:
            refused.append("%s（状态 %s，拒绝覆盖 — 幂等保护）" % (target, mode))

    if refused:
        LOG.warning("⚠ 以下目标不切换: %s", "; ".join(refused))
    if not planned:
        ctx.alerts.send("switch_refused", "[DR] ⚠ 切换条件满足但被幂等保护拒绝",
                        "触发: 连续 %d 次完整探测不健康\n被拒绝目标:\n  - %s\n\n"
                        "推导状态非 primary（backup/mixed/empty）时不覆盖 DNS、不写快照。\n"
                        "请人工核对解析状态。"
                        % (int(state.get("fails", 0)), "\n  - ".join(refused)))
        return

    if not cfg.get("switch_enabled", True):
        # DR_SWITCH_ENABLED=0：零 DNS 风险验证档 —— 只闸住 DNS 写，其余照常
        LOG.warning("⚠ 本应切换到备站（%s），但 DR_SWITCH_ENABLED=0：仅告警，不写 DNS", ",".join(planned))
        ctx.alerts.send("switch_disabled", "[DR] ⚠ 本应切换到备站，但 DR_SWITCH_ENABLED=0（仅告警）",
                        "完整探测连续 %d 次不健康，权威状态满足切换条件，目标: %s\n"
                        "但 DR_SWITCH_ENABLED=0：不执行任何 DNS 写入、不写快照（零风险验证档）。\n"
                        "如需实际切换请设置 DR_SWITCH_ENABLED=1 并重启服务。"
                        % (int(state.get("fails", 0)), ",".join(planned)))
        return

    execute_backup(ctx, res, planned)


# ─────────────────────────── 心跳邮件 ───────────────────────────

def heartbeat_check(ctx):
    """每日 08:00（本地时间）发心跳邮件：温度 / 磁盘 / uptime / 判定。"""
    cfg = ctx.cfg
    state = ctx.state.data
    if not cfg["alert_enabled"] or cfg["dry_run"]:
        return
    local = time.localtime()
    if local.tm_hour < HEARTBEAT_HOUR:
        return
    today = time.strftime("%Y-%m-%d", local)
    if state.get("last_heartbeat") == today:
        return
    temp = read_temp()
    disk = read_disk_percent(cfg["state_dir"])
    up = int(time.monotonic() - ctx.start_mono)
    sys_up = read_sys_uptime()
    body = (
        "树莓派观察者心跳（每日 08:00）。\n\n"
        "时间: %s (UTC) / 本地 %s\n"
        "判定: %s（net=%s, mode=%s, fast=%d, fails=%d, streak=%d）\n"
        "线路: %s\n"
        "温度: %s\n"
        "磁盘(状态目录): %s\n"
        "进程 uptime: %ds / 系统 uptime: %s\n"
        "时钟偏差: %s\n"
        "上次切换: %s (%s)\n"
        "最近详情: %s\n"
    ) % (
        utc_now_str(), time.strftime("%Y-%m-%d %H:%M:%S", local),
        state.get("last_verdict"), state.get("last_net"), state.get("last_mode"),
        1 if state.get("mode_state") == "fast" else 0,
        int(state.get("fails", 0)), int(state.get("streak", 0)),
        ", ".join("%s=%s" % (k, v) for k, v in sorted((state.get("last_lines") or {}).items())) or "无",
        ("%.1f ℃" % temp) if temp is not None else "获取不到（非树莓派或权限不足）",
        ("%.1f%%" % disk) if disk is not None else "获取不到",
        up, ("%ds" % sys_up) if sys_up is not None else "获取不到",
        ("%.1fs" % ctx.clock.skew) if ctx.clock.skew is not None else "未测出",
        state.get("last_switch_ts") or "从未", state.get("last_switch_dir") or "-",
        " | ".join(state.get("last_detail") or []) or "无",
    )
    if ctx.alerts.send("heartbeat", "[DR] 📡 树莓派观察者心跳 %s" % today, body, force=True):
        state["last_heartbeat"] = today


# ─────────────────────────── tick 主流程 ───────────────────────────

class Ctx(object):
    pass


def reset_restart_state(state):
    """进程重启后重置状态机（契约要求：回 slow、计数清零、第一轮全探重新判定）。

    只重置"证据类"字段；seq / tcp_cache / last_switch_* / alerts / last_* 展示字段保留，
    因此不会拿重启前的失败计数去切换。
    """
    changed = []
    for key, default in (("mode_state", "slow"), ("tcp_fails", 0), ("fails", 0), ("streak", 0),
                         ("last_full_probe_epoch", 0.0)):
        if state.data.get(key) != default:
            changed.append(key)
        state.data[key] = default
    if changed:
        LOG.info("♻️ 进程启动重置状态机（%s）→ slow + 计数清零，第一轮完整探测重新建立判定",
                 ",".join(changed))


def build_ctx(cfg):
    ctx = Ctx()
    ctx.cfg = cfg
    ctx.state = State(os.path.join(cfg["state_dir"], "state.json")).load()
    reset_restart_state(ctx.state)
    # 会签板心跳：board_sig 不跨进程保留 → 重启后第一轮必写一次，让对端立刻看到本节点上线；
    # 上次写入时间戳仍从 state.json 读回，避免重启造成额外的心跳写。
    ctx.state.data["board_sig"] = ""
    ctx.start_mono = time.monotonic()
    ctx.ali = None
    if has_credentials(cfg):
        ctx.ali = AliDNS(cfg["ali_key_id"], cfg["ali_key_secret"], cfg["ali_region"])
    elif not cfg["dry_run"]:
        # --loop / --once 的调用方已拦截，此处只兜底
        LOG.warning("⚠ 未配置 ALI_KEY_ID/ALI_KEY_SECRET，无法读写会签板与权威记录")
    ctx.clock = ClockGuard(cfg["clock_skew_max"])
    skew = ctx.state.data.get("clock_skew")
    if isinstance(skew, (int, float)):
        ctx.clock.skew = float(skew)
        ctx.clock.checked_at = float(ctx.state.data.get("clock_checked_at") or 0)
    ctx.alerts = Alerter(cfg, ctx.state)
    return ctx


def run_tick(ctx):
    """一个 tick：探测 → 计数 → 决策 → 会签板 → 心跳。异常不冒泡到循环外。"""
    cfg = ctx.cfg
    state = ctx.state.data
    state["seq"] = int(state.get("seq", 0)) + 1
    now = time.time()
    last_full = float(state.get("last_full_probe_epoch") or 0)
    in_fast = state.get("mode_state") == "fast"
    fast_interval = max(cfg["tick_seconds"], cfg["fast_probe_seconds"])
    due_full = (
        last_full <= 0
        or not (state.get("tcp_cache") or {})
        or (now - last_full) >= cfg["full_probe_seconds"]
        or (in_fast and (now - last_full) >= fast_interval)
    )

    if due_full:
        res = probe_full(ctx)
        state["last_full_probe_epoch"] = now
        state["last_probe"] = utc_now_str()
        state["last_verdict"] = res["verdict"]
        state["last_net"] = res["net"]
        state["last_lines"] = res["lines"]
        state["last_main_lines"] = res["main_lines"]
        state["last_detail"] = (res["detail"] or [])[:6]
        state["last_peer"] = res.get("peer") or ""
        for target, info in (res["targets"] or {}).items():
            if info.get("fresh"):
                state.setdefault("target_modes", {})[target] = info["mode"]
        state["last_mode"] = res["mode"]

        if res["verdict"] == "healthy":
            state["fails"] = 0
            state["streak"] = int(state.get("streak", 0)) + 1
        elif res["verdict"] == "unhealthy":
            state["fails"] = int(state.get("fails", 0)) + 1
            state["streak"] = 0
        # unknown：冻结计数（自身网络/API 故障时不累积误判）

        if res["verdict"] == "healthy":
            if state.get("mode_state") == "fast":
                LOG.info("✅ 完整探测恢复健康 → 退回 slow 模式")
            state["mode_state"] = "slow"
            state["tcp_fails"] = 0
        elif res["verdict"] == "unhealthy" and state.get("mode_state") == "slow":
            state["mode_state"] = "fast"
            state["tcp_fails"] = 0
            LOG.warning("⚠ 完整探测不健康 → 进入 fast 模式（每 tick 完整探测，加速确认）")

        if res["net"] == "broken":
            ctx.alerts.send("net_broken", "[DR] ⚠ 观察者自身网络异常，暂停判定",
                            "自身网络对照探针: %s\n"
                            "verdict=unknown，本轮不切换、不计数（防止家宽故障误触发切换）。"
                            % " | ".join(res.get("net_detail") or []))
        if ctx.clock.skew is not None and abs(ctx.clock.skew) > cfg["clock_skew_max"]:
            ctx.alerts.send("clock_skew", "[DR] ❌ 时钟偏差超限，DNS 写已被拒绝",
                            "偏差 %.0fs > DR_CLOCK_SKEW_MAX=%d，阿里云签名会失效。\n"
                            "请校准树莓派时间。" % (ctx.clock.skew, cfg["clock_skew_max"]))
        if res.get("hijack"):
            ctx.alerts.send("hijack", "[DR] ⚠ 递归解析与权威记录不一致（疑似劫持/污染）",
                            "递归(223.5.5.5): %s\n权威: %s\n线路: %s\n详情: %s"
                            % (",".join(res.get("recursive") or []) or "无",
                               ",".join(res.get("www_auth_ips") or []) or "无",
                               ", ".join("%s=%s" % (k, v) for k, v in sorted(res["lines"].items())),
                               " | ".join(res["detail"])))
        if res.get("api_error") and res["verdict"] == "unknown" and res["net"] == "ok":
            ctx.alerts.send("api_error", "[DR] ⚠ 阿里云 API 异常，权威状态推导失败",
                            "已降级：不切换、不恢复、只告警。\n详情: %s" % " | ".join(res["detail"]))

        if res["verdict"] == "unhealthy":
            try:
                maybe_switch(ctx, res)
            except Exception:
                LOG.exception("❌ 切换流程异常（已兜底）")
        elif res["verdict"] == "healthy":
            try:
                maybe_restore(ctx, res)
            except Exception:
                LOG.exception("❌ 恢复判定异常（已兜底）")
    else:
        ok, detail = light_probe(ctx)
        if ok:
            if int(state.get("tcp_fails", 0)) > 0:
                LOG.info("✅ %s", detail)
            state["tcp_fails"] = 0
        else:
            state["tcp_fails"] = int(state.get("tcp_fails", 0)) + 1
            LOG.warning("⚠ %s（连续 %d 次）", detail, state["tcp_fails"])
            if state.get("mode_state") == "slow" and state["tcp_fails"] >= cfg["tcp_fails_to_escalate"]:
                state["mode_state"] = "fast"
                LOG.warning("⚠ TCP 轻探连续 %d 次失败 → 升级 fast 模式（每 tick 完整探测）",
                            state["tcp_fails"])

    # 会签板：_dr-pi 心跳降频（状态变化立即写；无变化按 DR_BOARD_WRITE_SECONDS 最小间隔）
    if ctx.ali is not None and not cfg["dry_run"]:
        due, board_sig = board_write_due(ctx)
        if due:
            try:
                if write_txt(ctx.ali, BOARD_PI, build_pi_payload(ctx)):
                    state["board_sig"] = board_sig
                    state["last_board_write_epoch"] = time.time()
            except Exception as e:
                LOG.warning("⚠ 会签板写入异常（降级继续）: %s", e)

    state["clock_skew"] = ctx.clock.skew
    state["clock_checked_at"] = ctx.clock.checked_at

    LOG.info("📡 tick#%d verdict=%s net=%s mode=%s lines=%s state=%s fails=%d streak=%d tcp_fails=%d",
             int(state.get("seq", 0)), state.get("last_verdict"), state.get("last_net"),
             state.get("last_mode"),
             ",".join("%s=%s" % (k, v) for k, v in sorted((state.get("last_lines") or {}).items())) or "-",
             state.get("mode_state"), int(state.get("fails", 0)), int(state.get("streak", 0)),
             int(state.get("tcp_fails", 0)))

    try:
        heartbeat_check(ctx)
    except Exception:
        LOG.exception("❌ 心跳检查异常（已兜底）")
    ctx.clock.refresh()


# ─────────────────────────── 子命令 ───────────────────────────

def cmd_loop(cfg):
    if not has_credentials(cfg):
        print("❌ 缺少 ALI_KEY_ID / ALI_KEY_SECRET，--loop 拒绝启动（请检查 %s）" % cfg["config_path"],
              file=sys.stderr)
        return 2
    ctx = build_ctx(cfg)
    ctx.clock.refresh(force=True)
    LOG.info("📡 dr-agent v%s 启动：tick=%ds full=%ds fast=%ds role=%s targets=%s dry_run=%s state_dir=%s",
             APP_VERSION, cfg["tick_seconds"], cfg["full_probe_seconds"], cfg["fast_probe_seconds"],
             cfg["role"], ",".join(cfg["targets"]), cfg["dry_run"], cfg["state_dir"])
    LOG.info("📋 配置来源: %s（AK=%s, SMTP=%s）", cfg["config_path"],
             "已设置" if cfg["ali_key_id"] else "缺失",
             "已设置" if (cfg["smtp_username"] and cfg["smtp_password"]) else "缺失/不完整")
    try:
        while True:
            started = time.time()
            try:
                run_tick(ctx)
            except Exception:
                LOG.exception("❌ tick 异常（已兜底，循环继续）")
            ctx.state.save()
            elapsed = time.time() - started
            sleep_for = max(1.0, cfg["tick_seconds"] - elapsed)
            if elapsed > cfg["tick_seconds"]:
                LOG.warning("⚠ 本轮耗时 %.0fs 超过 tick=%ds（探测超时），立即进入下一轮",
                            elapsed, cfg["tick_seconds"])
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        LOG.info("👋 收到中断信号，保存状态后退出")
        ctx.state.save()
        return 0


def cmd_once(cfg):
    if not has_credentials(cfg) and not cfg["dry_run"]:
        print("❌ 缺少 ALI_KEY_ID / ALI_KEY_SECRET，--once 无法推导权威状态（可用 --dry-run 降级观察）",
              file=sys.stderr)
        return 2
    ctx = build_ctx(cfg)
    ctx.clock.refresh(force=True)
    if ctx.ali is None:
        LOG.warning("⚠ 无凭据：降级为递归解析视角，只输出判定，不做任何写操作")
    try:
        run_tick(ctx)
    except Exception as e:
        LOG.exception("❌ 单轮执行异常")
        print("❌ 执行失败: %s" % e, file=sys.stderr)
        return 1
    if not cfg["dry_run"]:
        ctx.state.save()
    state = ctx.state.data
    print("📡 本轮判定: %s | net=%s | mode=%s | fast=%d | fails=%d | streak=%d | lines=%s"
          % (state.get("last_verdict"), state.get("last_net"), state.get("last_mode"),
             1 if state.get("mode_state") == "fast" else 0,
             int(state.get("fails", 0)), int(state.get("streak", 0)),
             ",".join("%s=%s" % (k, v) for k, v in sorted((state.get("last_lines") or {}).items())) or "-"))
    for line in state.get("last_detail") or []:
        print("    · %s" % line)
    return 0


def cmd_ticks(cfg, ticks):
    """同一进程内连续跑 N 轮 tick 后退出（测试/演练接缝；--once ≈ --ticks 1）。

    tick 间按 DR_TICK_SECONDS 休眠（与 --loop 一致），让完整探测间隔与跨轮计数
    （连续不健康/连续健康）真实累积。不改变任何判定逻辑。
    """
    if not has_credentials(cfg) and not cfg["dry_run"]:
        print("❌ 缺少 ALI_KEY_ID / ALI_KEY_SECRET，--ticks 无法推导权威状态（可用 --dry-run 降级观察）",
              file=sys.stderr)
        return 2
    ctx = build_ctx(cfg)
    ctx.clock.refresh(force=True)
    if ctx.ali is None:
        LOG.warning("⚠ 无凭据：降级为递归解析视角，只输出判定，不做任何写操作")
    rc = 0
    for index in range(ticks):
        started = time.time()
        try:
            run_tick(ctx)
        except Exception:
            LOG.exception("❌ 第 %d/%d 轮 tick 异常（已兜底，继续后续轮次）", index + 1, ticks)
            rc = 1
        if not cfg["dry_run"]:
            ctx.state.save()
        if index + 1 < ticks:
            elapsed = time.time() - started
            sleep_for = max(1.0, cfg["tick_seconds"] - elapsed)
            time.sleep(sleep_for)
    state = ctx.state.data
    print("📡 %d 轮后判定: %s | net=%s | mode=%s | fast=%d | fails=%d | streak=%d | lines=%s"
          % (ticks, state.get("last_verdict"), state.get("last_net"), state.get("last_mode"),
             1 if state.get("mode_state") == "fast" else 0,
             int(state.get("fails", 0)), int(state.get("streak", 0)),
             ",".join("%s=%s" % (k, v) for k, v in sorted((state.get("last_lines") or {}).items())) or "-"))
    for line in state.get("last_detail") or []:
        print("    · %s" % line)
    return rc


def cmd_status(cfg):
    ctx_state = State(os.path.join(cfg["state_dir"], "state.json")).load()
    d = ctx_state.data
    print("📡 dr-agent v%s — 上次状态（%s）" % (APP_VERSION, os.path.join(cfg["state_dir"], "state.json")))
    print("  判定: %s | net=%s | 权威 mode=%s | %s 模式"
          % (d.get("last_verdict"), d.get("last_net"), d.get("last_mode"),
             d.get("mode_state")))
    print("  计数: fails=%d streak=%d tcp_fails=%d seq=%d"
          % (int(d.get("fails", 0)), int(d.get("streak", 0)),
             int(d.get("tcp_fails", 0)), int(d.get("seq", 0))))
    print("  线路(当前服务路径): %s"
          % (", ".join("%s=%s" % (k, v) for k, v in sorted((d.get("last_lines") or {}).items())) or "无"))
    if d.get("last_main_lines"):
        print("  主站直连(快照 IP): %s"
              % ", ".join("%s=%s" % (k, v) for k, v in sorted((d.get("last_main_lines") or {}).items())))
    print("  最近完整探测: %s" % (d.get("last_probe") or "从未"))
    print("  peer(_dr-gh): %s" % (d.get("last_peer") or "未知（未读/无记录）"))
    print("  上次切换: %s (%s)" % (d.get("last_switch_ts") or "从未", d.get("last_switch_dir") or "-"))
    skew = d.get("clock_skew")
    print("  时钟偏差: %s" % (("%.1fs" % skew) if isinstance(skew, (int, float)) else "未测出"))
    alerts = d.get("alerts") or {}
    if alerts:
        print("  最近告警: %s" % ", ".join(
            "%s@%s" % (k, time.strftime("%m-%d %H:%M", time.localtime(v))) for k, v in sorted(alerts.items())))
    for line in d.get("last_detail") or []:
        print("    · %s" % line)
    print("  配置文件: %s | role=%s targets=%s dry_run=%s"
          % (cfg["config_path"], cfg["role"], ",".join(cfg["targets"]), cfg["dry_run"]))
    return 0


def cmd_selftest(cfg):
    """环境自检：逐项 ✅ ⚠ ❌；有 ❌ 时退出码 1。"""
    results = []

    def report(icon, name, detail):
        results.append((icon, name, detail))
        print("%s %s — %s" % (icon, name, detail))

    print("📡 dr-agent v%s 环境自检" % APP_VERSION)
    print("─" * 64)

    # 1) Python 版本
    if sys.version_info >= (3, 9):
        report("✅", "Python 版本", "%d.%d.%d（要求 ≥ 3.9）" % sys.version_info[:3])
    else:
        report("❌", "Python 版本", "%d.%d.%d（过低，要求 ≥ 3.9）" % sys.version_info[:3])

    # 2) curl
    if CURL_BIN:
        report("✅", "curl", CURL_BIN)
    else:
        report("⚠", "curl", "未找到，将回退 stdlib ssl+socket 手写请求（功能可用，建议 apt install curl）")

    # 3) DNS
    try:
        result = dns_query(NEUTRAL_DNS, "www.baidu.com", 1, timeout=4)
        if result.get("a"):
            report("✅", "DNS 查询", "223.5.5.5 正常（www.baidu.com → %s）" % ",".join(result["a"][:2]))
        else:
            report("❌", "DNS 查询", "223.5.5.5 无 A 记录应答")
    except Exception as e:
        report("❌", "DNS 查询", "223.5.5.5 失败: %s" % e)

    # 4) 阿里云 API
    if has_credentials(cfg):
        try:
            ali = AliDNS(cfg["ali_key_id"], cfg["ali_key_secret"], cfg["ali_region"], timeout=8)
            data = ali.call("DescribeDomainRecords", DomainName=DOMAIN, PageSize=1)
            total = ((data.get("DomainRecords") or {}).get("TotalCount"))
            report("✅", "阿里云 DNS API", "签名调用成功（DescribeDomainRecords TotalCount=%s）" % total)
        except Exception as e:
            report("❌", "阿里云 DNS API", "调用失败: %s" % str(e)[:160])
    else:
        report("❌", "阿里云 DNS API", "缺少 ALI_KEY_ID / ALI_KEY_SECRET（--loop 将拒绝启动）")
        try:
            code, _h, err = http_get_stdlib("https://alidns.%s.aliyuncs.com/" % cfg["ali_region"], timeout=6)
            if code != "000":
                report("⚠", "阿里云 API 可达性", "端点可达（HTTP %s），但缺凭据无法验证签名" % code)
            else:
                report("⚠", "阿里云 API 可达性", "端点不可达/无响应: %s" % (err or "无响应"))
        except Exception as e:
            report("⚠", "阿里云 API 可达性", "探测异常: %s" % e)

    # 5) SMTP 配置
    missing = [k for k in ("smtp_server", "smtp_port", "smtp_username", "smtp_password", "report_to")
               if not cfg.get(k)]
    if missing:
        report("❌", "SMTP 配置", "缺少: %s（告警与心跳将无法发送）" % ",".join(missing))
    else:
        try:
            sock = socket.create_connection((cfg["smtp_server"], cfg["smtp_port"]), timeout=6)
            sock.close()
            report("✅", "SMTP 配置", "%s:%d 配置齐全且端口可达（收件人 %d 个）"
                   % (cfg["smtp_server"], cfg["smtp_port"], len(cfg["report_to"])))
        except Exception as e:
            report("⚠", "SMTP 配置", "配置齐全但端口不可达: %s" % e)

    # 6) 时钟偏差
    guard = ClockGuard(cfg["clock_skew_max"])
    guard.refresh(force=True)
    if guard.skew is None:
        report("⚠", "时钟偏差", "无法从 HTTP Date 头测得（离线？）；写操作将放行（降级）")
    elif abs(guard.skew) <= cfg["clock_skew_max"]:
        report("✅", "时钟偏差", "%.1fs（阈值 %ds）" % (guard.skew, cfg["clock_skew_max"]))
    else:
        report("❌", "时钟偏差", "%.1fs 超过阈值 %ds — 一切 DNS 写入会被拒绝，请校准时间"
               % (guard.skew, cfg["clock_skew_max"]))

    # 7) 状态目录
    try:
        os.makedirs(cfg["state_dir"], exist_ok=True)
        probe_file = os.path.join(cfg["state_dir"], ".selftest.tmp")
        with open(probe_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe_file)
        report("✅", "状态目录", "%s 可写" % cfg["state_dir"])
    except Exception as e:
        report("❌", "状态目录", "%s 不可写: %s" % (cfg["state_dir"], e))

    # 8) 配置文件
    if os.path.exists(cfg["config_path"]):
        mode = oct(os.stat(cfg["config_path"]).st_mode & 0o777)
        icon = "✅" if (os.stat(cfg["config_path"]).st_mode & 0o077) == 0 else "⚠"
        report(icon, "配置文件", "%s 存在（权限 %s%s）"
               % (cfg["config_path"], mode, "" if icon == "✅" else "，建议 chmod 600"))
    else:
        report("⚠", "配置文件", "%s 不存在（使用默认值 + 进程环境变量）" % cfg["config_path"])

    # 9) 运行参数
    report("📡", "运行参数", "role=%s targets=%s tick=%ds full=%ds fast=%ds dry_run=%s switch_enabled=%d"
           % (cfg["role"], ",".join(cfg["targets"]), cfg["tick_seconds"],
              cfg["full_probe_seconds"], cfg["fast_probe_seconds"], cfg["dry_run"],
              1 if cfg.get("switch_enabled", True) else 0))

    fails = [r for r in results if r[0] == "❌"]
    print("─" * 64)
    if fails:
        print("❌ 自检未通过：%d 项失败 — 请修复后再启动 --loop" % len(fails))
        return 1
    print("✅ 自检通过")
    return 0


# ─────────────────────────── 入口 ───────────────────────────

def setup_logging(state_dir):
    LOG.setLevel(logging.INFO)
    LOG.handlers = []
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    LOG.addHandler(stream)
    try:
        os.makedirs(state_dir, exist_ok=True)
        handler = RotatingFileHandler(os.path.join(state_dir, "dr-agent.log"),
                                      maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(fmt)
        LOG.addHandler(handler)
    except Exception as e:
        LOG.warning("⚠ 日志文件不可用（仅 stdout/journald）: %s", e)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dr_agent.py",
        description="树莓派容灾观察者 / 切换执行器（纯标准库，契约 monitoring/DR-OBSERVER-CONTRACT.md）")
    parser.add_argument("--loop", action="store_true", help="常驻循环（systemd 用，30s 一 tick）")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--ticks", type=int, default=0, metavar="N",
                        help="同一进程内连续跑 N 轮 tick 后退出（测试/演练接缝，约等于 N 次 --once）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不写 DNS、不写 TXT、不发邮件，只输出判定（可与 --once 组合）")
    parser.add_argument("--status", action="store_true", help="打印上次状态与最近判定")
    parser.add_argument("--selftest", action="store_true", help="环境自检（版本/curl/DNS/API/SMTP/时钟/目录）")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, metavar="PATH",
                        help="配置文件路径（默认 /etc/dr-agent.env）")
    parser.add_argument("--state-dir", default=None, metavar="PATH",
                        help="状态目录（默认 /var/lib/dr-agent）")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        cfg = load_config(args.config, explicit=(args.config != DEFAULT_CONFIG_PATH),
                          state_dir=args.state_dir)
    except ConfigError as e:
        print("❌ %s" % e, file=sys.stderr)
        return 2
    if args.dry_run:
        cfg["dry_run"] = True

    setup_logging(cfg["state_dir"])
    LOG.info("📡 dr-agent v%s 配置载入（config=%s, state_dir=%s, dry_run=%s）",
             APP_VERSION, cfg["config_path"], cfg["state_dir"], cfg["dry_run"])

    if args.selftest:
        return cmd_selftest(cfg)
    if args.status:
        return cmd_status(cfg)
    if args.loop:
        return cmd_loop(cfg)
    if args.ticks and args.ticks > 0:
        return cmd_ticks(cfg, args.ticks)
    if args.once:
        return cmd_once(cfg)
    parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

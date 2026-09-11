#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_coop_scenarios.py — 双观察者容灾对抗推演（纯标准库，不依赖 pytest）。

一键运行:
  python monitoring/tests/test_coop_scenarios.py

覆盖 S0-S12（详见 monitoring/tests/README.md）：
  S0  mock 契约自检（签名校验 / Duplicate / DomainRecords=null 边界 / 超时注入）
  S1  健康：主站在线 → 不切换、判定 healthy、云侧 mode=primary
  S2  黑洞注入：连续 3 次不健康 → 切换 www→Vercel + 写 _dr-snap + 云侧 mode=backup
  S3  拉锯防护：mode=backup + peer(_dr-pi--target www)=unhealthy → 云侧规则不得恢复
  S4  阶段一：DR_ROLE=switch_only 即使判定恢复也只告警、不动 DNS
  S5  Pi 失联降级：_dr-pi stale/absent → 云侧放行恢复（显式断言规则）
  S6  快照防污染：已有有效 _dr-snap 时切换只更新 ts/who/dir，IP 数组不变
  S7  时钟防线：skew>300s → allow_write()=False 且拒绝 DNS 写
  S8  幂等：zone 已是 backup → 拒绝切换、不改快照、不重复写记录
  S9  API 故障降级：Describe 500 → agent verdict=unknown 不切换；dr_board 失败退出 1
  S10 验证档：DR_SWITCH_ENABLED=0 + 黑洞 → 不写 DNS、无快照、只告警、心跳照常
  S11 心跳降频：DR_BOARD_WRITE_SECONDS 生效；状态变化立即写；ts 不超时
  S12 starkeeper 安全：先建后删，任何失败都不留无记录裸域

约定
  - 所有阿里云 API 打到本进程内嵌的 mock（monitoring/tests/mock_alidns.py），
    绝不触碰真实 DNS / API / 邮件；除探测目标本身（CF/Vercel 真实 IP）外不依赖外部服务。
  - 每个场景使用独立临时目录做 config/state，绝不碰 /etc、/var。
  - 判定类断言基于真实 HTTP 交互后的 mock zone 与进程输出，失败即 exit 1。
"""

import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PI_AGENT_DIR = os.path.join(REPO, "monitoring", "pi-agent")
PI_AGENT = os.path.join(PI_AGENT_DIR, "dr_agent.py")
DR_BOARD = os.path.join(REPO, "monitoring", "scripts", "dr_board.py")
DOMAIN = "chenxiuniverse.top"

sys.path.insert(0, HERE)
sys.path.insert(0, PI_AGENT_DIR)
import mock_alidns  # noqa: E402
import dr_agent    # noqa: E402  （S7/S12 进程内直测用）

# 进程内直测（S7/S12）时静默 dr_agent 的 WARNING 兜底输出，避免污染测试摘要
import logging  # noqa: E402
dr_agent.LOG.addHandler(logging.NullHandler())

# 真实可达的 Cloudflare 优选 IP / Vercel 备站 IP / RFC5737 不可路由黑洞
CF_IP_A = "172.64.52.95"
CF_IP_B = "162.159.44.17"
CF_IP_OVERSEA = "104.19.184.186"
VERCEL_IP = "76.76.21.21"
BLACKHOLE = "203.0.113.1"

# 场景配置模板：全部无邮件、假 AK、DR_TARGETS=www（聚焦 www 决策链）
BASE_CONFIG = {
    "ALI_KEY_ID": "LTAI-mock-test",
    "ALI_KEY_SECRET": "mock-secret",
    "ALI_REGION": "cn-hangzhou",
    "DR_ROLE": "switch_only",
    "DR_TARGETS": "www",
    "DR_TICK_SECONDS": "5",
    "DR_FULL_PROBE_SECONDS": "30",
    "DR_FAST_PROBE_SECONDS": "5",
    "DR_TCP_FAILS_TO_ESCALATE": "2",
    "DR_FAILS_TO_SWITCH": "3",
    "DR_STREAK_TO_RESTORE": "3",
    "DR_MIN_DWELL_SECONDS": "1800",
    "DR_PEER_MAX_AGE_SECONDS": "1200",
    "DR_CLOCK_SKEW_MAX": "300",
    "DR_BOARD_WRITE_SECONDS": "300",
    "DR_SWITCH_ENABLED": "1",
    "DR_ALERT_ENABLED": "0",
    "DR_DRY_RUN": "0",
    "SMTP_SERVER": "",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "REPORT_TO": "",
}


# ─────────────────────────── 基础工具 ───────────────────────────

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def fresh_ts(offset_seconds=0):
    return (utc_now() + datetime.timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_age_seconds(ts):
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return (utc_now() - dt).total_seconds()
    except Exception:
        return None


def clean_env():
    """剔除宿主环境里可能干扰测试的 DR_/ALI_/SMTP_ 变量。"""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("DR_", "ALI_", "SMTP_")) or key == "REPORT_TO":
            env.pop(key, None)
    return env


def write_config(path, **overrides):
    values = dict(BASE_CONFIG)
    values.update(overrides)
    lines = ["# 自动生成的测试配置（monitoring/tests/test_coop_scenarios.py）"]
    for key in sorted(values):
        lines.append("%s=%s" % (key, values[key]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def make_env(prefix="dr-coop-", **overrides):
    """临时 config + state 目录（绝不碰 /etc、/var）。"""
    tmp = tempfile.mkdtemp(prefix=prefix)
    cfg_path = os.path.join(tmp, "dr-agent.env")
    state_dir = os.path.join(tmp, "state")
    write_config(cfg_path, **overrides)
    return tmp, cfg_path, state_dir


def run_agent(cfg_path, state_dir, endpoint, args, timeout=240, env_extra=None):
    """子进程跑真实 CLI；返回 (CompletedProcess, 耗时秒)。"""
    env = clean_env()
    env["ALI_ENDPOINT"] = endpoint
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, PI_AGENT, "--config", cfg_path, "--state-dir", state_dir] + list(args)
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=timeout)
    return proc, time.time() - started


def run_board(endpoint, args, timeout=40):
    """子进程跑 dr_board.py CLI（mock 端点 + 假 AK）。"""
    env = clean_env()
    env["ALI_ENDPOINT"] = endpoint
    env["ALI_KEY_ID"] = "mock-ak"
    env["ALI_KEY_SECRET"] = "mock-secret"
    return subprocess.run([sys.executable, DR_BOARD] + list(args), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)


def raw_http_status(url, timeout=5):
    """发原始 GET，返回 (status, json_body)；HTTPError 也解析 body。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}


def raw_http_json(url, timeout=5):
    status, data = raw_http_status(url, timeout=timeout)
    if status != 200:
        raise AssertionError("期望 200，实际 %s: %s" % (status, data))
    return data


def board_json(srv, rr):
    """读会签板 TXT 并解析 JSON；不存在/解析失败返回 None。"""
    rec = srv.get_record(rr, "TXT", "default")
    if not rec:
        return None
    value = (rec.get("Value") or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def zone_values(srv, rr, rtype=None, line=None):
    return sorted(r["Value"] for r in srv.get_all(rr, rtype, line))


def zone_id_value_map(srv, rr=None, rtype=None, line=None):
    return dict((r["RecordId"], r["Value"]) for r in srv.get_all(rr, rtype, line))


def pi_write_log(srv):
    """按时间顺序返回 _dr-pi 的写入（Add/Update）列表 [(ts, value)]。"""
    out = []
    for call in srv.call_log():
        if call["action"] not in ("AddDomainRecord", "UpdateDomainRecord"):
            continue
        if call["params"].get("RR") != "_dr-pi":
            continue
        out.append((call["ts"], call["params"].get("Value", "")))
    return out


def cloud_restore_allowed(mode, peer, streak=3, dwell_seconds=None,
                          min_streak=3, min_dwell=1800):
    """契约 §四 恢复条件的可执行复刻（云侧 failover-monitor 判定）：

    mode ∈ {backup, mixed}（mixed 时主站可达另判）且 streak>=3 且 peer != unhealthy
    且距 _dr-snap.ts >= 1800s（缺快照视为满足）→ 才允许恢复。
    """
    if mode not in ("backup", "mixed"):
        return False
    if streak < min_streak:
        return False
    if peer == "unhealthy":
        return False
    if dwell_seconds is not None and dwell_seconds < min_dwell:
        return False
    return True


def tcp_ok(host, port=443, timeout=4):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


class AlertRecorder(object):
    """替换 ctx.alerts，记录告警调用（不联网不发信）。"""

    def __init__(self):
        self.items = []

    def send(self, kind, subject, body, force=False):
        self.items.append({"kind": kind, "subject": subject, "body": body})
        return True

    def kinds(self):
        return [item["kind"] for item in self.items]


class LogCollector(object):
    """收集 dr_agent.LOG 的日志文本（进程内场景断言用）。"""

    def __init__(self):
        self.lines = []

    def contains(self, needle):
        return any(needle in line for line in self.lines)


# ─────────────────────────── 场景框架 ───────────────────────────

SCENARIOS = []


def scenario(sid, title, needs_net=True):
    def deco(fn):
        SCENARIOS.append((sid, title, fn, needs_net))
        return fn
    return deco


class Checker(object):
    def __init__(self, sid, title):
        self.sid = sid
        self.title = title
        self.checks = []
        self.notes = []

    def check(self, cond, label):
        ok = bool(cond)
        self.checks.append((ok, label))
        print("   %s %s" % ("✅" if ok else "❌", label))
        return ok

    def check_eq(self, actual, expected, label):
        return self.check(actual == expected, "%s（期望 %r，实际 %r）" % (label, expected, actual))

    def note(self, msg):
        self.notes.append(msg)
        print("   ℹ %s" % msg)

    @property
    def failed(self):
        return [label for ok, label in self.checks if not ok]


def precheck():
    """网络前提核验（探测目标真实可达是场景语义的一部分，仅告警不中止）。"""
    print("🔎 网络前提核验（探测目标真实可达性）")
    targets = [("主站 CF IP", CF_IP_A), ("备站 Vercel IP", VERCEL_IP), ("中立站点 baidu", "www.baidu.com")]
    warn = []
    for label, host in targets:
        ok = tcp_ok(host)
        print("   %s %s (%s:443)" % ("✅" if ok else "⚠", label, host))
        if not ok:
            warn.append(label)
    if warn:
        print("   ⚠ 以下目标 TCP 443 不可达，相关场景可能失败: %s" % ",".join(warn))
    else:
        print("   ✅ 前提满足（agent 的 HTTP 探测路径可用）")


# ─────────────────────────── S0 mock 契约 ───────────────────────────

@scenario("S0", "mock 契约：签名校验 / Duplicate / DomainRecords=null / 超时注入", needs_net=False)
def s0(t):
    srv = mock_alidns.start_mock()
    try:
        # 1) 无匹配 → Record 为空列表（真实 API 空响应形态）
        url = mock_alidns.api_url(srv.url, "DescribeDomainRecords",
                                  DomainName=DOMAIN, RRKeyWord="www", PageSize="100")
        data = raw_http_json(url)
        t.check_eq((data.get("DomainRecords") or {}).get("Record"), [], "无匹配时 DomainRecords.Record=[]")
        t.check_eq(data.get("TotalCount"), 0, "TotalCount=0")

        # 2) 缺 Signature → HTTP 400 MissingParameter（真实风格错误体）
        url = mock_alidns.api_url(srv.url, "DescribeDomainRecords",
                                  omit=("Signature",), DomainName=DOMAIN)
        status, data = raw_http_status(url)
        t.check_eq((status, data.get("Code")), (400, "MissingParameter"), "缺 Signature → 400 MissingParameter")

        # 3) DomainRecords=null 边界：dr_board 空值保护 → mode empty（不是崩溃）
        srv.set_record("www", "A", "default", "1.2.3.4")
        srv.set_fault("DescribeDomainRecords", mode="null_records", count=1)
        proc = run_board(srv.url, ["mode", "www"])
        t.check_eq((proc.returncode, proc.stdout.strip()), (0, "empty"),
                   "DomainRecords=null → dr_board mode www 输出 empty 且 rc=0（空值保护）")
        srv.clear_faults()

        # 4) 重复添加 → DomainRecordDuplicate（commit ec544af 被咬过的真实行为）
        url = mock_alidns.api_url(srv.url, "AddDomainRecord", DomainName=DOMAIN, RR="www",
                                  Type="A", Value="1.2.3.4", Line="default", TTL="600")
        status, data = raw_http_status(url)
        t.check_eq((status, data.get("Code")), (400, "DomainRecordDuplicate"), "重复 Add → 400 DomainRecordDuplicate")

        # 5) set_record / dump_zone 辅助函数
        srv.clear_zone()
        srv.set_record("_dr-pi", "TXT", "default", '{"v":1}')
        srv.set_record("_dr-pi", "TXT", "default", '{"v":2}')
        zone = mock_alidns.dump_zone()
        t.check_eq([(r["RR"], r["Type"], r["Value"]) for r in zone], [("_dr-pi", "TXT", '{"v":2}')],
                   "set_record 收敛为单条 + dump_zone 可读")

        # 6) 超时注入：客户端按自身超时失败（服务端挂起后照常响应）
        srv.set_fault("DescribeDomainRecords", timeout=1.0, count=1)
        url = mock_alidns.api_url(srv.url, "DescribeDomainRecords", DomainName=DOMAIN)
        timed_out = False
        try:
            urllib.request.urlopen(url, timeout=0.3).read()
        except Exception:
            timed_out = True
        t.check(timed_out, "超时注入：客户端在自身 timeout 内失败")
        srv.clear_faults()
    finally:
        srv.stop()


# ─────────────────────────── S1 健康 ───────────────────────────

@scenario("S1", "健康：主站在线不切换，_dr-pi healthy，云侧 mode=primary")
def s1(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env()
    try:
        srv.set_record("www", "A", "default", CF_IP_A)
        srv.add_record("www", "A", "default", CF_IP_B)
        srv.set_record("www", "A", "oversea", CF_IP_OVERSEA)
        before = zone_id_value_map(srv, "www", "A")

        proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "3"], timeout=180)
        out = proc.stdout + proc.stderr
        t.check_eq(proc.returncode, 0, "agent --ticks 3 退出码 0")
        t.check("verdict=healthy" in out, "进程输出出现 verdict=healthy")
        t.check(len(pi_write_log(srv)) >= 1, "_dr-pi 心跳已写入（首次）")
        pi = board_json(srv, "_dr-pi")
        t.check(pi is not None, "_dr-pi 是合法 JSON")
        if pi:
            t.check_eq(pi.get("verdict"), "healthy", "_dr-pi.verdict=healthy")
            t.check_eq(pi.get("net"), "ok", "_dr-pi.net=ok")
            t.check_eq(pi.get("mode"), "primary", "_dr-pi.mode=primary")
            t.check_eq(pi.get("fails"), 0, "_dr-pi.fails=0")
            t.check_eq(pi.get("who"), "pi", "_dr-pi.who=pi")
            t.check_eq((pi.get("lines") or {}).get("www.default"), "200", "_dr-pi.lines[www.default]=200")
            t.check_eq((pi.get("lines") or {}).get("www.oversea"), "200", "_dr-pi.lines[www.oversea]=200")
        t.check(srv.get_record("_dr-snap", "TXT", "default") is None, "未写 _dr-snap（未切换）")
        t.check_eq(zone_id_value_map(srv, "www", "A"), before, "www 记录（RecordId/Value）完全未动")
        mode = run_board(srv.url, ["mode", "www"])
        t.check_eq((mode.returncode, mode.stdout.strip()), (0, "primary"), "云侧 dr_board mode www → primary")
    finally:
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── S2 黑洞注入 → 切换 ───────────────────────────

@scenario("S2", "黑洞注入：连续 3 次不健康 → www 两条线路切 Vercel + 写 _dr-snap")
def s2(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env()
    try:
        srv.set_record("www", "A", "default", BLACKHOLE)
        srv.set_record("www", "A", "oversea", BLACKHOLE)
        t.check(srv.get_all("www", "A") and zone_values(srv, "www", "A") == [BLACKHOLE, BLACKHOLE],
                "注入完成：www 两条线路 = 203.0.113.1")

        proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "3"], timeout=240)
        out = proc.stdout + proc.stderr
        t.check_eq(proc.returncode, 0, "agent --ticks 3 退出码 0")
        t.check("满足切换条件" in out, "日志出现『满足切换条件』")
        t.check("✅ 切换完成" in out, "日志出现『✅ 切换完成』")
        t.check_eq(zone_values(srv, "www", "A", "default"), [VERCEL_IP], "mock zone www(default) → 76.76.21.21")
        t.check_eq(zone_values(srv, "www", "A", "oversea"), [VERCEL_IP], "mock zone www(oversea) → 76.76.21.21")
        t.check_eq(len(srv.get_all("www", "A")), 2, "www 总记录数=2（原地 update，无重复添加）")

        snap = board_json(srv, "_dr-snap")
        t.check(snap is not None, "_dr-snap 已写入")
        if snap:
            t.check_eq(sorted(snap.get("www") or []), [BLACKHOLE], "快照 www = 切换前的主站 IP")
            t.check_eq(sorted(snap.get("www_oversea") or []), [BLACKHOLE], "快照 www_oversea = 切换前的主站 IP")
            t.check_eq(snap.get("dir"), "backup", "快照 dir=backup")
            t.check_eq(snap.get("who"), "pi", "快照 who=pi")
        pi = board_json(srv, "_dr-pi")
        t.check(pi is not None and pi.get("verdict") == "unhealthy", "切换瞬间 _dr-pi.verdict=unhealthy（对端可见的原始证据）")
        mode = run_board(srv.url, ["mode", "www"])
        t.check_eq((mode.returncode, mode.stdout.strip()), (0, "backup"), "云侧 dr_board mode www → backup")
    finally:
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── S3 拉锯防护 ───────────────────────────

@scenario("S3", "拉锯防护（核心）：mode=backup + peer=unhealthy → 云侧规则不得恢复")
def s3(t):
    srv = mock_alidns.start_mock()
    try:
        # 承接 S2 的终态：权威=备站；_dr-pi 仍是切换瞬间 agent 写下的 unhealthy/000 证据
        srv.set_record("www", "A", "default", VERCEL_IP)
        srv.set_record("www", "A", "oversea", VERCEL_IP)
        srv.set_record("_dr-pi", "TXT", "default", json.dumps({
            "v": 1, "ts": fresh_ts(), "who": "pi", "seq": 3, "verdict": "unhealthy",
            "net": "ok", "mode": "primary", "fast": 1, "fails": 3,
            "lines": {"www.default": "000", "www.oversea": "000"},
        }, separators=(",", ":")))
        # 主站 IP 恢复可达（真实联网核验）+ 快照持有可恢复的主站 IP
        srv.set_record("_dr-snap", "TXT", "default", json.dumps({
            "v": 1, "ts": fresh_ts(-3600), "who": "pi", "dir": "backup",
            "www": [CF_IP_A, CF_IP_B], "www_oversea": [CF_IP_OVERSEA],
        }, separators=(",", ":")))

        mode = run_board(srv.url, ["mode", "www"])
        t.check_eq((mode.returncode, mode.stdout.strip()), (0, "backup"), "权威 mode=backup（zone 仍是 Vercel）")
        peer = run_board(srv.url, ["peer", "_dr-pi", "--target", "www"])
        t.check_eq((peer.returncode, peer.stdout.strip()), (0, "unhealthy"),
                   "peer _dr-pi --target www = unhealthy（agent 未确认恢复）")
        t.check(tcp_ok(CF_IP_A), "前提：主站 IP 恢复可达（%s:443）" % CF_IP_A)

        # 模拟云监控判定：mode=backup + peer=unhealthy → 不得恢复
        t.check(cloud_restore_allowed("backup", "unhealthy", streak=5, dwell_seconds=99999) is False,
                "云侧规则：mode=backup + peer=unhealthy → 不得恢复")
        t.check(cloud_restore_allowed("backup", "healthy", streak=5, dwell_seconds=99999) is True,
                "对照：peer=healthy 时同一规则允许恢复（证明规则非恒 False）")
        # 没有任何组件执行恢复：zone 必须仍是 Vercel
        t.check_eq(zone_values(srv, "www", "A"), [VERCEL_IP, VERCEL_IP], "断言：zone 仍是 Vercel IP（未发生拉锯恢复）")
    finally:
        srv.stop()


# ─────────────────────────── S4 阶段一不恢复 ───────────────────────────

@scenario("S4", "阶段一：DR_ROLE=switch_only 判定恢复也只告警、不动 DNS")
def s4(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env(DR_ROLE="switch_only")
    try:
        srv.set_record("www", "A", "default", VERCEL_IP)
        srv.set_record("www", "A", "oversea", VERCEL_IP)
        snap_value = json.dumps({
            "v": 1, "ts": fresh_ts(-3600), "who": "pi", "dir": "backup",
            "www": [CF_IP_A, CF_IP_B], "www_oversea": [CF_IP_OVERSEA],
        }, separators=(",", ":"))
        srv.set_record("_dr-snap", "TXT", "default", snap_value)
        before_zone = zone_id_value_map(srv, "www", "A")

        proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "16"], timeout=240)
        out = proc.stdout + proc.stderr
        t.check_eq(proc.returncode, 0, "agent --ticks 16 退出码 0")
        t.check("verdict=healthy" in out, "主站与备站均健康 → verdict=healthy")
        t.check("恢复条件满足但 DR_ROLE=switch_only" in out,
                "输出出现『恢复条件满足但 DR_ROLE=switch_only（阶段一只切不恢复）』")
        t.check(("streak=3" in out) or ("streak=4" in out) or ("streak=5" in out),
                "连续健康计数已到恢复阈值（streak>=3）")
        t.check_eq(zone_id_value_map(srv, "www", "A"), before_zone, "zone 保持 Vercel（未执行恢复）")
        snap = srv.get_record("_dr-snap", "TXT", "default")
        t.check(snap is not None and snap["Value"] == snap_value, "_dr-snap 未被改写")
    finally:
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── S5 Pi 失联降级 ───────────────────────────

@scenario("S5", "Pi 失联降级：_dr-pi stale/absent → 云侧放行恢复", needs_net=False)
def s5(t):
    srv = mock_alidns.start_mock()
    try:
        srv.set_record("www", "A", "default", VERCEL_IP)
        srv.set_record("www", "A", "oversea", VERCEL_IP)
        # ts=40 分钟前 → 超过默认 max-age 1200s
        srv.set_record("_dr-pi", "TXT", "default", json.dumps({
            "v": 1, "ts": fresh_ts(-2400), "who": "pi", "seq": 9, "verdict": "unhealthy",
            "net": "ok", "mode": "backup", "fast": 0, "fails": 0,
            "lines": {"www.default": "000", "www.oversea": "000"},
        }, separators=(",", ":")))
        peer = run_board(srv.url, ["peer", "_dr-pi", "--target", "www"])
        t.check_eq((peer.returncode, peer.stdout.strip()), (0, "stale"),
                   "ts 40 分钟前 → peer=stale（不影响退出码）")

        srv.delete_record("_dr-pi", "TXT", "default")
        peer_absent = run_board(srv.url, ["peer", "_dr-pi", "--target", "www"])
        t.check_eq((peer_absent.returncode, peer_absent.stdout.strip()), (0, "absent"),
                   "记录不存在 → peer=absent")
        peer_gh = run_board(srv.url, ["peer", "_dr-gh", "--target", "www"])
        t.check_eq((peer_gh.returncode, peer_gh.stdout.strip()), (0, "absent"),
                   "_dr-gh absent → peer=absent")

        # 契约 §四：对方失联不阻断恢复（stale/absent 视为通过），unhealthy 才阻断
        t.check(cloud_restore_allowed("backup", "stale", streak=5, dwell_seconds=99999) is True,
                "云侧规则：peer=stale → 允许恢复（降级放行）")
        t.check(cloud_restore_allowed("backup", "absent", streak=5, dwell_seconds=99999) is True,
                "云侧规则：peer=absent → 允许恢复（降级放行）")
        t.check(cloud_restore_allowed("backup", "unknown", streak=5, dwell_seconds=99999) is True,
                "云侧规则：peer=unknown → 允许恢复")
        t.check(cloud_restore_allowed("backup", "unhealthy", streak=5, dwell_seconds=99999) is False,
                "云侧规则：peer=unhealthy → 阻断恢复（对照）")
    finally:
        srv.stop()


# ─────────────────────────── S6 快照防污染 ───────────────────────────

@scenario("S6", "快照防污染：已有有效 _dr-snap → IP 数组不变，只更新 ts/who/dir")
def s6(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env()
    try:
        old_ts = fresh_ts(-7200)
        old_arrays = {"www": [CF_IP_A, CF_IP_B], "www_oversea": [CF_IP_OVERSEA]}
        srv.set_record("_dr-snap", "TXT", "default", json.dumps({
            "v": 1, "ts": old_ts, "who": "gh", "dir": "restore",
            "www": old_arrays["www"], "www_oversea": old_arrays["www_oversea"],
        }, separators=(",", ":")))
        # 注入黑洞触发切换（此时若"采集当前记录"会得到黑洞 IP，防污染必须拒绝覆盖）
        srv.set_record("www", "A", "default", BLACKHOLE)
        srv.set_record("www", "A", "oversea", BLACKHOLE)

        proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "3"], timeout=240)
        out = proc.stdout + proc.stderr
        t.check_eq(proc.returncode, 0, "agent --ticks 3 退出码 0")
        t.check("保留其 IP 数组" in out, "日志出现『保留其 IP 数组』（防污染生效）")
        t.check_eq(zone_values(srv, "www", "A"), [VERCEL_IP, VERCEL_IP], "切换已执行（zone=Vercel）")

        snap = board_json(srv, "_dr-snap")
        t.check(snap is not None, "_dr-snap 仍存在")
        if snap:
            t.check_eq(snap.get("www"), old_arrays["www"], "快照 www 数组保持原值（未写入黑洞 IP）")
            t.check_eq(snap.get("www_oversea"), old_arrays["www_oversea"], "快照 www_oversea 数组保持原值")
            t.check_eq(snap.get("who"), "pi", "快照 who 更新为 pi")
            t.check_eq(snap.get("dir"), "backup", "快照 dir 更新为 backup")
            t.check(snap.get("ts") != old_ts, "快照 ts 已更新")
            age = ts_age_seconds(snap.get("ts"))
            t.check(age is not None and age < 300, "新 ts 为当前时间（age<300s）")
    finally:
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── S7 时钟防线 ───────────────────────────

@scenario("S7", "时钟防线：skew>300s → allow_write=False 且拒绝 DNS 写", needs_net=False)
def s7(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env()
    env_backup = dict((k, os.environ.get(k)) for k in ("ALI_ENDPOINT", "ALI_KEY_ID", "ALI_KEY_SECRET"))
    log_collector = LogCollector()
    handler = None
    orig_probe_net = dr_agent.probe_net
    orig_refresh = None
    try:
        srv.set_record("www", "A", "default", BLACKHOLE)
        srv.set_record("www", "A", "oversea", BLACKHOLE)
        os.environ["ALI_ENDPOINT"] = srv.url
        os.environ["ALI_KEY_ID"] = "mock-ak"
        os.environ["ALI_KEY_SECRET"] = "mock-secret"
        cfg = dr_agent.load_config(cfg_path, explicit=True, state_dir=state_dir)
        ctx = dr_agent.build_ctx(cfg)
        # 注入时钟偏差：模拟 Pi 无 RTC 且校时失败（不依赖真实 Date 头，避免被网络校时覆盖）
        ctx.clock.skew = 999.0
        ctx.clock.checked_at = time.time()
        ctx.state.data["fails"] = 3  # 已满足连续不健康阈值，唯一闸门就是时钟
        ctx.state.data["last_full_probe_epoch"] = 0.0

        # 冻结自身网络探针与周期校时（测试专用 monkeypatch，保证 skew 不被覆盖）
        orig_refresh = ctx.clock.refresh
        dr_agent.probe_net = lambda c: {
            "net": "ok", "detail": ["(S7 注入: 跳过真实自身网络对照)"],
            "dns_ok": True, "neutral_codes": ["200"], "backup_code": "200", "backup_ok": True,
        }
        ctx.clock.refresh = lambda force=False: None
        handler = _make_log_handler(log_collector)
        dr_agent.LOG.addHandler(handler)

        before = zone_id_value_map(srv, "www", "A")
        t.check(ctx.clock.allow_write() is False, "allow_write()=False（skew=999 > DR_CLOCK_SKEW_MAX=300）")
        dr_agent.run_tick(ctx)
        t.check(ctx.clock.allow_write() is False, "tick 后 allow_write() 仍为 False")
        t.check(log_collector.contains("时钟偏差") and log_collector.contains("拒绝 DNS 写"),
                "输出出现『时钟偏差…拒绝 DNS 写操作』")
        t.check_eq(zone_id_value_map(srv, "www", "A"), before, "www 记录未被改写（未切换）")
        t.check(srv.get_record("_dr-snap", "TXT", "default") is None, "未写 _dr-snap（未切换）")
    finally:
        if handler is not None:
            dr_agent.LOG.removeHandler(handler)
        try:
            dr_agent.probe_net = orig_probe_net
            ctx.clock.refresh = orig_refresh
        except Exception:
            pass
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _make_log_handler(collector):
    import logging

    class _Handler(logging.Handler):
        def emit(self, record):
            try:
                collector.lines.append(record.getMessage())
            except Exception:
                collector.lines.append(str(record.msg))

    return _Handler()


# ─────────────────────────── S8 幂等 ───────────────────────────

@scenario("S8", "幂等：zone 已是 backup → 拒绝切换、不改快照、不重复写记录")
def s8(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env()
    try:
        srv.set_record("www", "A", "default", VERCEL_IP)
        srv.set_record("www", "A", "oversea", VERCEL_IP)
        # 快照主站 IP 不可达 → 主站未恢复 → verdict=unhealthy，fails 会累积到阈值
        snap_value = json.dumps({
            "v": 1, "ts": fresh_ts(-3600), "who": "pi", "dir": "backup",
            "www": [BLACKHOLE], "www_oversea": [BLACKHOLE],
        }, separators=(",", ":"))
        srv.set_record("_dr-snap", "TXT", "default", snap_value)
        before_zone = zone_id_value_map(srv, "www", "A")
        before_zone_dump = srv.dump_zone()

        proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "3"], timeout=240)
        out = proc.stdout + proc.stderr
        t.check_eq(proc.returncode, 0, "agent --ticks 3 退出码 0")
        t.check("幂等保护" in out, "输出出现『幂等保护』（状态 backup 拒绝覆盖）")
        t.check("✅ 切换完成" not in out, "没有出现『切换完成』")
        t.check_eq(zone_id_value_map(srv, "www", "A"), before_zone, "www 记录与 RecordId 完全未动")
        t.check_eq(len(srv.get_all("www", "A")), 2, "没有重复写记录")
        snap = srv.get_record("_dr-snap", "TXT", "default")
        t.check(snap is not None and snap["Value"] == snap_value, "_dr-snap 未被改写")
        before_other = [r for r in before_zone_dump if r["RR"] != "_dr-pi"]
        after_other = [r for r in srv.dump_zone() if r["RR"] != "_dr-pi"]
        t.check_eq(len(after_other), len(before_other), "除 _dr-pi 心跳外 zone 记录数不变（无重复写）")
        pi = board_json(srv, "_dr-pi")
        t.check(pi is not None and pi.get("verdict") == "unhealthy", "_dr-pi 心跳仍在写（verdict=unhealthy）")
    finally:
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── S9 API 故障降级 ───────────────────────────

@scenario("S9", "API 故障降级：Describe 500 → agent unknown 不切换；dr_board 退出 1")
def s9(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env()
    try:
        srv.set_record("www", "A", "default", BLACKHOLE)
        srv.set_record("www", "A", "oversea", BLACKHOLE)
        srv.set_fault("DescribeDomainRecords", code="InternalError",
                      message="mock injected 500", http_status=500, count=None)

        proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "2"], timeout=180)
        out = proc.stdout + proc.stderr
        t.check_eq(proc.returncode, 0, "agent 不崩（退出码 0）")
        t.check("verdict=unknown" in out, "判定 verdict=unknown（API 异常冻结计数）")
        t.check("权威状态推导失败" in out or "API 异常" in out, "输出说明 API 异常降级")
        t.check_eq(zone_values(srv, "www", "A"), [BLACKHOLE, BLACKHOLE], "zone 未被改写（不切换）")
        t.check(srv.get_record("_dr-snap", "TXT", "default") is None, "未写 _dr-snap")
        t.check(srv.get_record("_dr-pi", "TXT", "default") is None, "会签板写失败也不中断主流程")

        mode = run_board(srv.url, ["mode", "www"])
        t.check_eq(mode.returncode, 1, "dr_board mode www 退出码 1")
        t.check_eq(mode.stdout.strip(), "", "dr_board 无 stdout 输出（调用方可回退旧逻辑）")
        t.check("❌ mode www 失败" in mode.stderr, "dr_board stderr 带 ❌ 错误说明")
    finally:
        srv.clear_faults()
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── S10 DR_SWITCH_ENABLED=0 ───────────────────────────

@scenario("S10", "验证档：DR_SWITCH_ENABLED=0 + 黑洞 → 不写 DNS、无快照、只告警、心跳照常")
def s10(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env(DR_SWITCH_ENABLED="0")
    try:
        srv.set_record("www", "A", "default", BLACKHOLE)
        srv.set_record("www", "A", "oversea", BLACKHOLE)
        before = zone_id_value_map(srv, "www", "A")

        proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "3"], timeout=240)
        out = proc.stdout + proc.stderr
        t.check_eq(proc.returncode, 0, "agent --ticks 3 退出码 0")
        t.check("本应切换到备站" in out and "DR_SWITCH_ENABLED=0" in out,
                "输出出现『本应切换到备站…DR_SWITCH_ENABLED=0…仅告警』")
        t.check("✅ 切换完成" not in out, "没有执行切换（无『切换完成』）")
        t.check_eq(zone_values(srv, "www", "A"), [BLACKHOLE, BLACKHOLE],
                   "zone 仍是黑洞 IP（没有被改成 Vercel）")
        t.check_eq(zone_id_value_map(srv, "www", "A"), before, "www 记录（RecordId/Value）完全未动")
        t.check(srv.get_record("_dr-snap", "TXT", "default") is None, "_dr-snap 未被写入")
        pi = board_json(srv, "_dr-pi")
        t.check(pi is not None, "_dr-pi 心跳仍在写入")
        if pi:
            t.check(pi.get("seq", 0) >= 3, "_dr-pi.seq 随 tick 递增（seq=%s）" % pi.get("seq"))
            age = ts_age_seconds(pi.get("ts"))
            t.check(age is not None and age < 60, "_dr-pi.ts 仍在刷新（age<60s）")
            t.check_eq(pi.get("verdict"), "unhealthy", "_dr-pi.verdict=unhealthy（判定照常）")

        # 补充（进程内）：恢复路径同样被 DR_SWITCH_ENABLED=0 闸住（DR_ROLE=full 也不写 DNS）
        tmp2, cfg2, state2 = make_env(DR_SWITCH_ENABLED="0", DR_ROLE="full")
        env_backup = dict((k, os.environ.get(k)) for k in ("ALI_ENDPOINT", "ALI_KEY_ID", "ALI_KEY_SECRET"))
        handler = None
        log_collector = LogCollector()
        try:
            os.environ["ALI_ENDPOINT"] = srv.url
            os.environ["ALI_KEY_ID"] = "mock-ak"
            os.environ["ALI_KEY_SECRET"] = "mock-secret"
            cfg_full = dr_agent.load_config(cfg2, explicit=True, state_dir=state2)
            ctx = dr_agent.build_ctx(cfg_full)
            ctx.state.data["streak"] = 3
            alerts = AlertRecorder()
            ctx.alerts = alerts
            handler = _make_log_handler(log_collector)
            dr_agent.LOG.addHandler(handler)
            res = {
                "targets": {"www": {"mode": "backup", "fresh": True}},
                "snap": {"v": 1, "ts": fresh_ts(-3600), "who": "pi", "dir": "backup",
                         "www": [CF_IP_A], "www_oversea": [CF_IP_OVERSEA]},
                "peer": "absent",
            }
            before_restore = zone_id_value_map(srv, "www", "A")
            dr_agent.maybe_restore(ctx, res)
            t.check(log_collector.contains("本应恢复主站") and log_collector.contains("DR_SWITCH_ENABLED=0"),
                    "补充：DR_ROLE=full + 闸门关闭 → 输出『本应恢复主站…DR_SWITCH_ENABLED=0』")
            t.check("restore_disabled" in alerts.kinds(), "补充：恢复路径发出只告警（restore_disabled）")
            t.check_eq(zone_id_value_map(srv, "www", "A"), before_restore, "补充：zone 未被恢复改写")
        finally:
            if handler is not None:
                dr_agent.LOG.removeHandler(handler)
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── S11 心跳降频 ───────────────────────────

@scenario("S11", "心跳降频：DR_BOARD_WRITE_SECONDS 生效 / 变化立即写 / ts 不超时")
def s11(t):
    # ---- 阶段 A：稳态下写入次数 ≈ elapsed/interval（而非每 tick 一次） ----
    srv_a = mock_alidns.start_mock()
    tmp_a, cfg_a, state_a = make_env(DR_BOARD_WRITE_SECONDS="15")
    try:
        srv_a.set_record("www", "A", "default", CF_IP_A)
        srv_a.set_record("www", "A", "oversea", CF_IP_OVERSEA)
        srv_a.clear_calls()
        proc, elapsed = run_agent(cfg_a, state_a, srv_a.url, ["--ticks", "8"], timeout=200)
        writes = len(pi_write_log(srv_a))
        t.check_eq(proc.returncode, 0, "阶段 A：agent --ticks 8 退出码 0")
        upper = 1 + int(elapsed / 15.0) + 2
        t.check(writes < 8, "阶段 A：_dr-pi 写入次数 %d < tick 数 8（降频生效）" % writes)
        t.check(2 <= writes <= upper,
                "阶段 A：写入次数 %d 符合 elapsed/interval 预期（2..%d，elapsed=%.0fs）" % (writes, upper, elapsed))
        pi = board_json(srv_a, "_dr-pi")
        age = ts_age_seconds((pi or {}).get("ts"))
        t.check(age is not None and age < 60,
                "阶段 A：稳态 _dr-pi.ts 仍在刷新（age=%.0fs < 60s，远小于 1200s 过期阈值）" % (age or -1))
        t.check(age is not None and age < 1200, "阶段 A：ts 未超过 peer 新鲜度阈值 1200s")
    finally:
        srv_a.stop()
        shutil.rmtree(tmp_a, ignore_errors=True)

    # ---- 阶段 B：判定中途变化 → 立即写（不等 interval 到点） ----
    srv_b = mock_alidns.start_mock()
    tmp_b, cfg_b, state_b = make_env(DR_BOARD_WRITE_SECONDS="120", DR_TICK_SECONDS="5")
    env = clean_env()
    env["ALI_ENDPOINT"] = srv_b.url
    try:
        srv_b.set_record("www", "A", "default", CF_IP_A)
        srv_b.set_record("www", "A", "oversea", CF_IP_OVERSEA)
        srv_b.clear_calls()
        cmd = [sys.executable, PI_AGENT, "--config", cfg_b, "--state-dir", state_b, "--ticks", "8"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env)
        out = ""
        try:
            # 等第一帧心跳（进程重启后第一轮必写）
            deadline = time.time() + 25
            while time.time() < deadline and len(pi_write_log(srv_b)) < 1:
                time.sleep(0.3)
            first_writes = pi_write_log(srv_b)
            t.check(len(first_writes) >= 1, "阶段 B：首轮心跳已写（重启后第一轮必写）")
            # 稳态（interval=120s，远未到点）突然黑洞化 → 判定变化
            srv_b.set_record("www", "A", "default", BLACKHOLE)
            srv_b.set_record("www", "A", "oversea", BLACKHOLE)
            out, _ = proc.communicate(timeout=200)
        finally:
            if proc.poll() is None:
                proc.kill()
        writes = pi_write_log(srv_b)
        t.check_eq(proc.returncode, 0, "阶段 B：agent --ticks 8 退出码 0")
        t.check(len(writes) >= 2, "阶段 B：变化后新增了 _dr-pi 写入（共 %d 次）" % len(writes))
        if len(writes) >= 2:
            gap = writes[-1][0] - writes[0][0]
            t.check(gap < 120, "阶段 B：变化写入间隔 %.0fs < DR_BOARD_WRITE_SECONDS=120（变化即写，不等间隔）" % gap)
            final = board_json(srv_b, "_dr-pi")
            t.check(final is not None and final.get("verdict") == "unhealthy",
                    "阶段 B：最后一次心跳 verdict=unhealthy（状态变化已上报）")
        t.check("verdict=unhealthy" in out, "阶段 B：进程输出确认判定变化")
    finally:
        srv_b.stop()
        shutil.rmtree(tmp_b, ignore_errors=True)


# ─────────────────────────── S12 starkeeper 安全（先建后删） ───────────────────────────

@scenario("S12", "starkeeper 安全：先建后删，任何失败都不留无记录裸域", needs_net=False)
def s12(t):
    srv = mock_alidns.start_mock()
    tmp, cfg_path, state_dir = make_env()
    env_backup = dict((k, os.environ.get(k)) for k in ("ALI_ENDPOINT", "ALI_KEY_ID", "ALI_KEY_SECRET"))
    try:
        os.environ["ALI_ENDPOINT"] = srv.url
        os.environ["ALI_KEY_ID"] = "mock-ak"
        os.environ["ALI_KEY_SECRET"] = "mock-secret"
        cfg = dr_agent.load_config(cfg_path, explicit=True, state_dir=state_dir)
        ctx = dr_agent.build_ctx(cfg)

        # ---- 断言 1：add CNAME 失败（非冲突类）→ A 记录原样、无空记录态、有告警 ----
        srv.clear_zone()
        srv.clear_faults()
        srv.add_record("starkeeper", "A", "default", CF_IP_A)
        srv.add_record("starkeeper", "A", "default", CF_IP_B)
        before = zone_id_value_map(srv, "starkeeper", "A", "default")
        srv.set_fault("AddDomainRecord", code="Throttling.User",
                      message="Request was denied due to user flow control.",
                      http_status=400, rr="starkeeper", count=None)
        alerts = AlertRecorder()
        ctx.alerts = alerts
        ok = dr_agent.starkeeper_to_backup(ctx)
        t.check(ok is False, "断言1：非冲突 add 失败 → 返回 False")
        t.check_eq(zone_id_value_map(srv, "starkeeper", "A", "default"), before,
                   "断言1：starkeeper A 记录原样还在（RecordId/Value 未动）")
        t.check_eq(zone_values(srv, "starkeeper", "CNAME", "default"), [],
                   "断言1：没有 CNAME（未出现半切并存）")
        t.check(len(srv.get_all("starkeeper", "A", "default")) > 0, "断言1：不是无记录裸域")
        t.check("starkeeper_switch_fail" in alerts.kinds(), "断言1：有告警（starkeeper_switch_fail）")

        # ---- 断言 2：删 A 失败 → A+CNAME 并存（mixed）、CNAME 已生效 ----
        srv.clear_zone()
        srv.clear_faults()
        srv.add_record("starkeeper", "A", "default", CF_IP_A)
        srv.set_fault("DeleteDomainRecord", code="Throttling.User",
                      message="Request was denied due to user flow control.",
                      http_status=400, count=None)
        alerts = AlertRecorder()
        ctx.alerts = alerts
        ok = dr_agent.starkeeper_to_backup(ctx)
        t.check(ok is True, "断言2：CNAME 就位后删 A 失败仍视为切换动作完成")
        t.check_eq(zone_values(srv, "starkeeper", "CNAME", "default"),
                   ["starkeeper-bpw.pages.dev"], "断言2：CNAME 已生效")
        t.check_eq(zone_values(srv, "starkeeper", "A", "default"), [CF_IP_A],
                   "断言2：A 仍在（删除失败保留）")
        t.check_eq(dr_agent.derive_starkeeper_mode(ctx.ali), "mixed", "断言2：推导状态=mixed")

        # ---- 断言 3：成功路径 → 只剩 CNAME、A 全清 ----
        srv.clear_zone()
        srv.clear_faults()
        srv.add_record("starkeeper", "A", "default", CF_IP_A)
        srv.add_record("starkeeper", "A", "default", CF_IP_B)
        alerts = AlertRecorder()
        ctx.alerts = alerts
        ok = dr_agent.starkeeper_to_backup(ctx)
        t.check(ok is True, "断言3：成功路径返回 True")
        t.check_eq(zone_values(srv, "starkeeper", "A", "default"), [], "断言3：A 记录全清")
        t.check_eq(zone_values(srv, "starkeeper", "CNAME", "default"),
                   ["starkeeper-bpw.pages.dev"], "断言3：只剩 CNAME starkeeper-bpw.pages.dev")
        t.check_eq(dr_agent.derive_starkeeper_mode(ctx.ali), "backup", "断言3：推导状态=backup")

        # ---- 断言 4：恢复方向 A 写回失败 → CNAME 仍然存在（不被删成裸域） ----
        srv.set_fault("AddDomainRecord", code="Throttling.User",
                      message="Request was denied due to user flow control.",
                      http_status=400, rr="starkeeper", count=None)
        alerts = AlertRecorder()
        ctx.alerts = alerts
        ok = dr_agent.starkeeper_to_restore(ctx, [CF_IP_A, CF_IP_B])
        t.check(ok is False, "断言4：A 写回失败 → 返回 False")
        t.check_eq(zone_values(srv, "starkeeper", "CNAME", "default"),
                   ["starkeeper-bpw.pages.dev"], "断言4：CNAME 仍然存在（未删成裸域）")
        t.check("starkeeper_restore_fail" in alerts.kinds(), "断言4：有告警（starkeeper_restore_fail）")

        # ---- 补充：冲突类错误才允许退化（删 A 后立即 add），且二次失败要告警 ----
        srv.clear_zone()
        srv.clear_faults()
        srv.add_record("starkeeper", "A", "default", CF_IP_A)
        srv.set_fault("AddDomainRecord", code="DomainRecordDuplicate",
                      message="The DNS record already exists.", http_status=400,
                      rr="starkeeper", count=1)
        alerts = AlertRecorder()
        ctx.alerts = alerts
        ok = dr_agent.starkeeper_to_backup(ctx)
        t.check(ok is True, "补充：冲突类错误 → 退化『删 A 后立即 add』成功")
        t.check_eq(zone_values(srv, "starkeeper", "CNAME", "default"),
                   ["starkeeper-bpw.pages.dev"], "补充：退化后 CNAME 就位")
        t.check_eq(zone_values(srv, "starkeeper", "A", "default"), [], "补充：退化后 A 已清理")

        srv.clear_zone()
        srv.clear_faults()
        srv.add_record("starkeeper", "A", "default", CF_IP_A)
        srv.set_fault("AddDomainRecord", code="DomainRecordDuplicate",
                      message="The DNS record already exists.", http_status=400,
                      rr="starkeeper", count=None)
        alerts = AlertRecorder()
        ctx.alerts = alerts
        ok = dr_agent.starkeeper_to_backup(ctx)
        t.check(ok is False, "补充：冲突退化后二次 add 仍失败 → 返回 False")
        t.check("starkeeper_bare" in alerts.kinds(), "补充：告警『无记录状态，需人工介入』")
        bare = [a for a in alerts.items if a["kind"] == "starkeeper_bare"]
        t.check(bool(bare) and "无记录" in bare[0]["subject"] and "没有" in bare[0]["body"],
                "补充：告警 subject/body 说明无记录风险")
    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── 运行器 ───────────────────────────

def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # 可选过滤：python test_coop_scenarios.py S0 S12（开发时只跑指定场景）
    wanted = set(a.upper() for a in sys.argv[1:] if not a.startswith("-"))
    selected = [item for item in SCENARIOS if not wanted or item[0].upper() in wanted]
    unknown = wanted - set(item[0].upper() for item in SCENARIOS)
    if unknown:
        print("❌ 未知场景: %s（可选: %s）" % (",".join(sorted(unknown)),
                                             ",".join(item[0] for item in SCENARIOS)))
        return 2
    print("=" * 72)
    print("🧪 双观察者容灾对抗推演（离线 mock Alidns，全部 API 不出本机）")
    if wanted:
        print("   仅运行: %s" % ",".join(item[0] for item in selected))
    print("=" * 72)
    precheck()
    print()

    total_checks = 0
    total_failed = 0
    failed_scenarios = []
    started_all = time.time()
    for sid, title, fn, _needs_net in selected:
        print("── %s %s" % (sid, title))
        checker = Checker(sid, title)
        started = time.time()
        try:
            fn(checker)
        except Exception:
            traceback.print_exc()
            checker.check(False, "场景执行异常（见上方 traceback）")
        elapsed = time.time() - started
        failed = checker.failed
        total_checks += len(checker.checks)
        total_failed += len(failed)
        if failed:
            failed_scenarios.append(sid)
            print("   ❌ %s 失败 %d/%d 项（%.1fs）" % (sid, len(failed), len(checker.checks), elapsed))
        else:
            print("   ✅ %s 通过 %d/%d 项（%.1fs）" % (sid, len(checker.checks), len(checker.checks), elapsed))
        print()

    print("=" * 72)
    if failed_scenarios:
        print("❌ 汇总：%d 个场景未通过（%s）；断言 %d/%d 项通过；总耗时 %.0fs"
              % (len(failed_scenarios), ",".join(failed_scenarios),
                 total_checks - total_failed, total_checks, time.time() - started_all))
        return 1
    print("✅ 汇总：%d/%d 场景全部通过；断言 %d/%d 项通过；总耗时 %.0fs"
          % (len(selected), len(selected), total_checks, total_checks, time.time() - started_all))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⚠ 已中断")
        sys.exit(130)

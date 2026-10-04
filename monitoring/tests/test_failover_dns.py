#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_failover_dns.py — failover-dns.py 离线测试（health 跟随语义，2026-10-04 加入）。

纯标准库、不联网、不装 SDK：sys.modules 注入 alibabacloud 桩模块后用 importlib
加载 monitoring/scripts/failover-dns.py（文件名带连字符，不能直接 import）。
假 client 内存 zone 直调 cmd_backup/cmd_restore（两者都接受 client 形参，不走 get_client）；
STATE_FILE 指向临时目录；verify_ips / read_dr_snap / write_dr_snap 全部 monkeypatch。

覆盖：
  F1 目标常量：DEFAULT_TARGETS = www×2 + health×2（health 在 www 之后）；
     state_targets 过滤 health（不写状态文件/不进幂等守卫）
  F2 backup：www 与 health 两线路一起切 Vercel；状态文件只有 www/www_oversea 键
  F3 restore（legacy 状态文件仅 www 键）：health 复用 www 同线路已验证 IP；
     且 verify_ips 只对 www 调用（health 零独立校验 = 零 split state）
  F4 restore：www verify 全空 → www 保持备站，health 跟随跳过（不写回）
  F5 幂等守卫不含 health：health 已是备站脏状态时 backup 仍能切 www（不 self-abort）
  F6 health 无 A 记录：backup/restore 均跳过（不凭空建记录）——与树莓派行为一致

运行: python monitoring/tests/test_failover_dns.py    （退出码 0/1）
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(REPO, "monitoring", "scripts", "failover-dns.py")

VERCEL_IP = "76.76.21.21"
CF_IP_A = "172.64.52.95"
CF_IP_B = "162.159.44.17"


class Checker(object):
    def __init__(self):
        self.checks = []

    def check(self, cond, label):
        ok = bool(cond)
        self.checks.append((ok, label))
        print("   %s %s" % ("✅" if ok else "❌", label))
        return ok

    def check_eq(self, actual, expected, label):
        return self.check(actual == expected, "%s（期望 %r，实际 %r）" % (label, expected, actual))

    @property
    def failed(self):
        return [label for ok, label in self.checks if not ok]


# ─────────────────────────── SDK 桩 + 模块加载 ───────────────────────────

def _make_request_class():
    class _Req(object):
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
    return _Req


class FakeConfig(object):
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeClient(object):
    """内存 zone 假 Alidns client（覆盖 failover-dns.py 用到的全部接口）。"""

    def __init__(self):
        self._records = {}  # rid -> dict(rr, type, value, line)
        self._seq = 0

    # ---- 测试侧 zone 操作 ----
    def add(self, rr, value, line="default", rtype="A"):
        self._seq += 1
        rid = "r%03d" % self._seq
        self._records[rid] = {"rr": rr, "type": rtype, "value": value, "line": line}
        return rid

    def zone(self, rr=None, line=None):
        return sorted(v["value"] for v in self._records.values()
                      if (rr is None or v["rr"] == rr) and (line is None or v["line"] == line))

    def count(self, rr=None):
        return sum(1 for v in self._records.values() if rr is None or v["rr"] == rr)

    # ---- SDK 接口（与 alibabacloud SDK 响应结构对齐）----
    def describe_domain_records(self, req):
        rr = getattr(req, "rrkey_word", None)
        line = (getattr(req, "line", None) or "default").lower()
        type_kw = (getattr(req, "type_key_word", None) or "").upper()
        recs = [
            SimpleNamespace(record_id=rid, rr=v["rr"], type=v["type"],
                            value=v["value"], line=v["line"])
            for rid, v in self._records.items()
            if v["rr"] == rr and (v["line"] or "default").lower() == line
            and (not type_kw or v["type"].upper() == type_kw)
        ]
        body = SimpleNamespace(domain_records=SimpleNamespace(record=recs))
        return SimpleNamespace(body=body)

    def update_domain_record(self, req):
        self._records[req.record_id]["value"] = req.value
        return SimpleNamespace(body=SimpleNamespace())

    def add_domain_record(self, req):
        self.add(req.rr, req.value, getattr(req, "line", "default"), req.type)
        return SimpleNamespace(body=SimpleNamespace())

    def delete_domain_record(self, req):
        self._records.pop(req.record_id, None)
        return SimpleNamespace(body=SimpleNamespace())


def load_failover_module():
    """注入 SDK 桩后加载 failover-dns.py（即使真实 SDK 已安装也不用它）。"""
    pkg = types.ModuleType("alibabacloud_alidns20150109")
    mod_client = types.ModuleType("alibabacloud_alidns20150109.client")
    mod_client.Client = object  # 测试直接传 fake client，不实例化
    mod_models = types.ModuleType("alibabacloud_alidns20150109.models")
    req_cls = _make_request_class()
    for name in ("DescribeDomainRecordsRequest", "UpdateDomainRecordRequest",
                 "AddDomainRecordRequest", "DeleteDomainRecordRequest",
                 "DescribeDomainRecordInfoRequest"):
        setattr(mod_models, name, req_cls)
    pkg.client = mod_client
    pkg.models = mod_models

    tea = types.ModuleType("alibabacloud_tea_openapi")
    tea_models = types.ModuleType("alibabacloud_tea_openapi.models")
    tea_models.Config = FakeConfig
    tea.models = tea_models

    for name, mod in (
        ("alibabacloud_alidns20150109", pkg),
        ("alibabacloud_alidns20150109.client", mod_client),
        ("alibabacloud_alidns20150109.models", mod_models),
        ("alibabacloud_tea_openapi", tea),
        ("alibabacloud_tea_openapi.models", tea_models),
    ):
        sys.modules[name] = mod

    spec = importlib.util.spec_from_file_location("failover_dns_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─────────────────────────── 用例 ───────────────────────────

def f1_constants(mod, t, tmp):
    rrs = [item["rr"] for item in mod.DEFAULT_TARGETS]
    t.check_eq(rrs, ["www", "www", "health", "health"],
               "DEFAULT_TARGETS = www×2 + health×2（health 必须排在 www 之后）")
    t.check_eq([item["rr"] for item in mod.FAILOVER_TARGETS][-1], "pimanager",
               "FAILOVER_TARGETS 末位是 pimanager（仅 --pimanager 纳入）")
    t.check_eq([item["rr"] for item in mod.state_targets(mod.DEFAULT_TARGETS)], ["www", "www"],
               "state_targets 过滤 health（不写状态文件/不进幂等守卫）")
    t.check_eq(mod.state_targets(mod.FAILOVER_TARGETS)[-1]["rr"], "pimanager",
               "state_targets 保留 pimanager（仅过滤 health）")


def f2_backup(mod, t, tmp):
    client = FakeClient()
    client.add("www", CF_IP_A, "default")
    client.add("www", CF_IP_B, "oversea")
    client.add("health", CF_IP_A, "default")
    client.add("health", CF_IP_B, "oversea")
    client.add("pimanager", CF_IP_A, "default")  # 默认目标不含 pimanager

    mod.cmd_backup(client)

    t.check_eq(client.zone("www", "default"), [VERCEL_IP], "www(default) → Vercel")
    t.check_eq(client.zone("www", "oversea"), [VERCEL_IP], "www(oversea) → Vercel")
    t.check_eq(client.zone("health", "default"), [VERCEL_IP], "health(default) 跟随 → Vercel")
    t.check_eq(client.zone("health", "oversea"), [VERCEL_IP], "health(oversea) 跟随 → Vercel")
    t.check_eq(client.count("health"), 2, "health 总记录数=2（原地 update，无重复添加）")
    t.check_eq(client.zone("pimanager", "default"), [CF_IP_A], "pimanager 未被默认目标触碰")
    t.check(mod.STATE_FILE.exists(), "状态文件已写入")
    state = json.loads(mod.STATE_FILE.read_text(encoding="utf-8"))
    t.check("health" not in state and "health_oversea" not in state,
            "状态文件无 health/health_oversea 键（health 不落盘）")
    t.check_eq(state.get("www"), [CF_IP_A], "状态文件 www = 切换前主站 IP")
    t.check_eq(state.get("www_oversea"), [CF_IP_B], "状态文件 www_oversea = 切换前主站 IP")


def f3_restore_follows_www(mod, t, tmp):
    client = FakeClient()
    client.add("www", VERCEL_IP, "default")
    client.add("www", VERCEL_IP, "oversea")
    client.add("health", VERCEL_IP, "default")
    client.add("health", VERCEL_IP, "oversea")
    # legacy 状态文件：仅 www 键（health 加入前备份的形态）
    mod.STATE_FILE.write_text(json.dumps({
        "timestamp": "2026-10-04T00:00:00Z", "action": "backup",
        "www": [CF_IP_A], "www_oversea": [CF_IP_B],
    }), encoding="utf-8")

    verify_calls = []

    def fake_verify(ips, host):
        verify_calls.append((tuple(sorted(ips)), host))
        return list(ips)

    mod.verify_ips = fake_verify
    mod.cmd_restore(client)

    t.check_eq(client.zone("www", "default"), [CF_IP_A], "www(default) 恢复 → CF IP")
    t.check_eq(client.zone("www", "oversea"), [CF_IP_B], "www(oversea) 恢复 → CF IP")
    t.check_eq(client.zone("health", "default"), [CF_IP_A],
               "health(default) 复用 www 同线路 IP 恢复")
    t.check_eq(client.zone("health", "oversea"), [CF_IP_B],
               "health(oversea) 复用 www 同线路 IP 恢复")
    t.check_eq(len(verify_calls), 2,
               "verify_ips 仅被调 2 次（www 双线路；health 零独立校验 = 无 split 可能）")


def f4_restore_www_failed_health_skips(mod, t, tmp):
    client = FakeClient()
    client.add("www", VERCEL_IP, "default")
    client.add("www", VERCEL_IP, "oversea")
    client.add("health", VERCEL_IP, "default")
    client.add("health", VERCEL_IP, "oversea")
    mod.STATE_FILE.write_text(json.dumps({
        "timestamp": "2026-10-04T00:00:00Z", "action": "backup",
        "www": [CF_IP_A], "www_oversea": [CF_IP_B],
    }), encoding="utf-8")

    mod.verify_ips = lambda ips, host: []  # www 目标全部不可达（含 fallback）
    mod.cmd_restore(client)

    t.check_eq(client.zone("www", "default"), [VERCEL_IP], "www(default) verify 全空 → 保持备站")
    t.check_eq(client.zone("www", "oversea"), [VERCEL_IP], "www(oversea) verify 全空 → 保持备站")
    t.check_eq(client.zone("health", "default"), [VERCEL_IP],
               "health(default) 跟随跳过 → 保持备站（不写回）")
    t.check_eq(client.zone("health", "oversea"), [VERCEL_IP],
               "health(oversea) 跟随跳过 → 保持备站")


def f5_guard_excludes_health(mod, t, tmp):
    client = FakeClient()
    client.add("www", CF_IP_A, "default")      # www 处于 primary
    client.add("www", CF_IP_B, "oversea")
    client.add("health", VERCEL_IP, "default")  # health 残留备站（脏状态）
    client.add("health", VERCEL_IP, "oversea")

    try:
        mod.cmd_backup(client)
        aborted = False
    except SystemExit:
        aborted = True

    t.check(aborted is False, "health 脏状态不触发幂等守卫 self-abort（守卫只看 www/pimanager）")
    t.check_eq(client.zone("www", "default"), [VERCEL_IP], "www(default) 仍能正常切到 Vercel")
    t.check_eq(client.zone("www", "oversea"), [VERCEL_IP], "www(oversea) 仍能正常切到 Vercel")
    t.check_eq(client.zone("health", "default"), [VERCEL_IP], "health(default) 已是备站 → 幂等跳过")


def f6_no_record_no_create(mod, t, tmp):
    # backup：health 无记录 → 跳过，不凭空建记录
    client = FakeClient()
    client.add("www", CF_IP_A, "default")
    client.add("www", CF_IP_B, "oversea")

    mod.cmd_backup(client)
    t.check_eq(client.zone("health"), [], "backup：health 无 A 记录 → 未创建任何记录")
    t.check_eq(client.zone("www", "default"), [VERCEL_IP], "backup：www 切换不受影响")

    # restore：health 无记录 → 同样跳过
    mod.STATE_FILE.write_text(json.dumps({
        "timestamp": "2026-10-04T00:00:00Z", "action": "backup",
        "www": [CF_IP_A], "www_oversea": [CF_IP_B],
    }), encoding="utf-8")
    mod.verify_ips = lambda ips, host: list(ips)
    mod.cmd_restore(client)
    t.check_eq(client.zone("health"), [], "restore：health 无 A 记录 → 仍未创建记录")
    t.check_eq(client.zone("www", "default"), [CF_IP_A], "restore：www 正常恢复")


# ─────────────────────────── 运行器 ───────────────────────────

def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("=" * 72)
    print("🧪 failover-dns.py 离线测试（health 跟随语义；SDK 桩 + 内存 zone，零网络）")
    print("=" * 72)

    mod = load_failover_module()
    t = Checker()

    cases = [
        ("F1 目标常量与 state_targets", f1_constants),
        ("F2 backup：www+health 同切、状态文件无 health", f2_backup),
        ("F3 restore：health 复用 www 已验证 IP", f3_restore_follows_www),
        ("F4 restore：www 失败时 health 跟随跳过", f4_restore_www_failed_health_skips),
        ("F5 守卫不含 health（脏状态不阻塞 www）", f5_guard_excludes_health),
        ("F6 health 无记录不凭空创建", f6_no_record_no_create),
    ]

    for title, fn in cases:
        print("── %s" % title)
        tmp = tempfile.mkdtemp(prefix="fd-test-")
        # 每个用例前重置 monkeypatch（去网络 + 状态文件指向临时目录）
        mod.STATE_FILE = Path(tmp) / ".failover_state.json"
        mod.verify_ips = lambda ips, host: list(ips)
        mod.read_dr_snap = lambda: {}
        mod.write_dr_snap = lambda *a, **k: True
        try:
            fn(mod, t, tmp)
        except Exception:
            import traceback
            traceback.print_exc()
            t.check(False, "用例执行异常（见上方 traceback）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print()

    failed = t.failed
    print("=" * 72)
    if failed:
        print("❌ 汇总：%d/%d 项未通过" % (len(failed), len(t.checks)))
        for label in failed:
            print("   - %s" % label)
        return 1
    print("✅ 汇总：断言 %d/%d 项全部通过" % (len(t.checks), len(t.checks)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⚠ 已中断")
        sys.exit(130)

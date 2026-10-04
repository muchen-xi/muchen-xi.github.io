#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mailer_accounts.py — dr_agent 发件账号主/备逻辑本地测试（纯标准库，不依赖 pytest）。

dr-agent 的对抗推演（test_coop_scenarios.py）把 DR_ALERT_ENABLED=0，不碰发信路径；
本测试专门补上这块：配置解析（主/备/旧键兼容）→ 账号顺序 → 主账号失败自动切备用。

不联网、不发信：把 dr_agent.smtplib.SMTP_SSL 换成假实现（按服务器名决定成败），
只验证"选账号 + 试下一个 + 记账"的逻辑。

用法: python monitoring/tests/test_mailer_accounts.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "pi-agent"))

import dr_agent  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok))
    line = "%s %s" % ("✅" if ok else "❌", name)
    if detail and not ok:
        line += "\n     ↳ %s" % detail
    print(line)


def write_env(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class FakeState(object):
    def __init__(self):
        self.data = {}


class FakeSmtp(object):
    """假 SMTP_SSL：记录调用；按 self.fail_for 里的服务器名抛异常。"""
    calls = []

    def __init__(self, server, port, timeout=None):
        self.server = server
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        FakeSmtp.calls.append(("login", self.server, user))
        if self.server in FakeSmtp.fail_for:
            raise OSError("connection refused (mock)")

    def sendmail(self, sender, to, msg):
        FakeSmtp.calls.append(("sendmail", self.server, sender, tuple(to)))


def build_cfg(work, lines, name="dr-agent.env"):
    path = os.path.join(work, name)
    write_env(path, lines)
    return dr_agent.load_config(path, explicit=True, state_dir=os.path.join(work, "state"))


def main():
    work = tempfile.mkdtemp(prefix="dr-mailer-test-")
    base_env = [
        "ALI_KEY_ID=x", "ALI_KEY_SECRET=y",
        "REPORT_TO=chenxi20081128@outlook.com",
        "SMTP_SENDER_NAME=\"晨曦的宇宙 · 树莓派观察者\"",
    ]
    try:
        # 1) 主 + 备：顺序与字段
        cfg = build_cfg(work, base_env + [
            "SMTP_PRIMARY_SERVER=mail.gov.moe", "SMTP_PRIMARY_PORT=465",
            "SMTP_PRIMARY_USERNAME=chenxi@love.place", "SMTP_PRIMARY_PASSWORD=p1",
            "SMTP_BACKUP_SERVER=smtp.qq.com", "SMTP_BACKUP_PORT=465",
            "SMTP_BACKUP_USERNAME=m20081225@qq.com", "SMTP_BACKUP_PASSWORD=p2",
        ])
        names = [a["name"] for a in cfg["smtp_accounts"]]
        check("主+备：账号顺序为 主→备", names == ["主账号", "备用账号"], names)
        check("主+备：主账号取 SMTP_PRIMARY_*",
              cfg["smtp_accounts"][0]["server"] == "mail.gov.moe"
              and cfg["smtp_accounts"][0]["username"] == "chenxi@love.place",
              cfg["smtp_accounts"][0])
        check("主+备：兼容别名指向主账号", cfg["smtp_server"] == "mail.gov.moe")

        # 2) 只有旧键 SMTP_* → 单账号（主），行为与旧版本一致
        cfg = build_cfg(work, base_env + [
            "SMTP_SERVER=smtp.qq.com", "SMTP_PORT=465",
            "SMTP_USERNAME=m20081225@qq.com", "SMTP_PASSWORD=p2",
        ], name="legacy.env")
        check("旧键：解析成单个主账号",
              len(cfg["smtp_accounts"]) == 1 and cfg["smtp_accounts"][0]["server"] == "smtp.qq.com",
              cfg["smtp_accounts"])

        # 3) 主账号发不出去 → 自动切备用（假 SMTP：mail.gov.moe 必失败）
        cfg = build_cfg(work, base_env + [
            "SMTP_PRIMARY_SERVER=mail.gov.moe", "SMTP_PRIMARY_PORT=465",
            "SMTP_PRIMARY_USERNAME=chenxi@love.place", "SMTP_PRIMARY_PASSWORD=p1",
            "SMTP_BACKUP_SERVER=smtp.qq.com", "SMTP_BACKUP_PORT=465",
            "SMTP_BACKUP_USERNAME=m20081225@qq.com", "SMTP_BACKUP_PASSWORD=p2",
        ], name="failover.env")
        FakeSmtp.calls = []
        FakeSmtp.fail_for = {"mail.gov.moe"}
        real_smtp = dr_agent.smtplib.SMTP_SSL
        state = FakeState()
        dr_agent.smtplib.SMTP_SSL = FakeSmtp
        try:
            ok = dr_agent.Alerter(cfg, state).send("test", "⚠ [DR] 发信通道本地测试", "正文", force=True)
        finally:
            dr_agent.smtplib.SMTP_SSL = real_smtp
        check("切备用：send 返回 True", ok is True, ok)
        servers = [c[1] for c in FakeSmtp.calls if c[0] == "login"]
        check("切备用：先试主账号再试备用", servers == ["mail.gov.moe", "smtp.qq.com"], servers)
        sent_from = [c[2] for c in FakeSmtp.calls if c[0] == "sendmail"]
        check("切备用：由备用账号发出", sent_from == ["m20081225@qq.com"], sent_from)
        check("切备用：写入告警节流记账", state.data.get("alerts", {}).get("test"), state.data)

        # 4) 全部账号失败 → 返回 False 且不抛异常（主循环不受影响）
        FakeSmtp.calls = []
        FakeSmtp.fail_for = {"mail.gov.moe", "smtp.qq.com"}
        dr_agent.smtplib.SMTP_SSL = FakeSmtp
        try:
            state = FakeState()
            ok = dr_agent.Alerter(cfg, state).send("test2", "⚠ [DR] 全部失败测试", "正文", force=True)
        finally:
            dr_agent.smtplib.SMTP_SSL = real_smtp
        check("全败：返回 False 不抛出", ok is False, ok)
        check("全败：不写节流记账（下次还能重试）", "alerts" not in state.data or "test2" not in state.data.get("alerts", {}))

        # 5) 完全没配账号 → 静默跳过（旧行为）
        cfg = build_cfg(work, base_env, name="none.env")
        check("未配账号：_configured() 为 False",
              dr_agent.Alerter(cfg, FakeState())._configured() is False)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for _n, ok in RESULTS if ok)
    print("\n════════ 汇总: %d/%d 通过 ════════" % (passed, len(RESULTS)))
    for name, ok in RESULTS:
        if not ok:
            print("  ❌ %s" % name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

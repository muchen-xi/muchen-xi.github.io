# -*- coding: utf-8 -*-
"""starkeeper_dns.py 的"零记录"安全回归测试（纯标准库，无需凭据、不联网）。

背景：2026-09-11 审计发现 `cmd_rotate` / `cmd_set_ips` / `cmd_backup` 都是"删光记录再重建"。
中途失败（限流/网络抖动/进程被杀）会让 starkeeper 的 default 线路变成**零记录** ——
国内用户直接解析失败，且下一轮推导为 empty 后不会自愈。这与该站 2026-06-27 P0 自伤事故
属同一失败模式，故加固为"先建后删"并为 backup 增加失败回滚。

本测试用假 `call()` 替换阿里云 API，逐操作检查核心不变量：
    **default 线路的记录数，在任何时刻都不能变成 0**（backup 的冲突回退路径除外，
    该路径天生存在一次调用的空窗，故要求失败时必须回滚）。

2026-09-12 黑洞演练缺陷 #3 补充：`cmd_restore` 旧顺序"先写 A 后删 CNAME"在 CNAME 存在时
必然撞 `DomainRecordConflict`（假 API 已按真实行为拒绝"default 有 CNAME 时写 A"）。
restore 的正确顺序"删 default CNAME → 立即写 A"天生存在一次调用空窗（CNAME/A 互斥决定），
因此其不变量为：**写 A 失败必须回滚 CNAME，结束状态绝不为零记录**，且 **oversea 线路
CNAME 是固定配置，任何路径都不得触碰**。

跑法： python monitoring/tests/test_starkeeper_safety.py
"""
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "monitoring" / "scripts"))

import starkeeper_dns as sk  # noqa: E402

zone = {}                 # RecordId -> {type, value, line}
next_id = [1]
history = []              # 操作序列
min_default = [999]       # 全程 default 线路最少记录数
reject_cname_adds = [0]   # 前 N 次 CNAME 补建被拒（模拟同名冲突）
fail_a_after = [None]     # 允许成功 N 次写 A 之后全部失败（模拟 converge 中途写失败）
a_add_ok = [0]            # 已成功的写 A 次数


def default_a():
    return sorted(r["value"] for r in zone.values() if r["line"] == "default" and r["type"] == "A")


def default_count():
    return len([r for r in zone.values() if r["line"] == "default"])


def has_cname():
    return any(r["type"] == "CNAME" and r["line"] == "default" for r in zone.values())


def check_invariant():
    min_default[0] = min(min_default[0], default_count())


def fake_call(action, **kw):
    check_invariant()
    if action == "DescribeDomainRecords":
        return {"DomainRecords": {"Record": [
            {"RecordId": i, "RR": "starkeeper", "Type": r["type"],
             "Value": r["value"], "Line": r["line"]} for i, r in zone.items()]}}
    if action == "AddDomainRecord":
        if kw["Type"] == "A" and kw["Line"] == "default" and any(
                r["type"] == "CNAME" and r["line"] == "default" for r in zone.values()):
            # 真实阿里云行为：default 线路存在 CNAME 时写 A 必被拒（演练缺陷 #3 的根因）
            raise Exception("DomainRecordConflict: default 线路已有 CNAME")
        if kw["Type"] == "A" and fail_a_after[0] is not None:
            if a_add_ok[0] >= fail_a_after[0]:
                raise Exception("injected: 写 A 记录失败")
            a_add_ok[0] += 1
        if kw["Type"] == "CNAME" and reject_cname_adds[0] > 0:
            reject_cname_adds[0] -= 1
            raise Exception("DomainRecordConflict: CNAME 与现有 A 记录冲突")
        for r in zone.values():
            if (r["type"], r["value"], r["line"]) == (kw["Type"], kw["Value"], kw["Line"]):
                raise Exception("DomainRecordDuplicate")
        zone[next_id[0]] = {"type": kw["Type"], "value": kw["Value"], "line": kw["Line"]}
        next_id[0] += 1
        history.append(("add", kw["Type"], kw["Value"]))
    elif action == "DeleteDomainRecord":
        history.append(("delete", zone[kw["RecordId"]]["type"], zone[kw["RecordId"]]["value"]))
        del zone[kw["RecordId"]]
    elif action == "UpdateDomainRecord":
        history.append(("update", zone[kw["RecordId"]]["value"], "->", kw["Value"]))
        zone[kw["RecordId"]]["value"] = kw["Value"]
    else:
        raise Exception("unexpected action " + action)
    check_invariant()
    return {}


sk.call = fake_call
results = []


def case(name, ok, detail=""):
    results.append(bool(ok))
    print(("✅ " if ok else "❌ ") + name + ("  " + detail if detail else ""))


def reset(records=(), reject_cname=0, fail_a_after_n=None):
    zone.clear()
    next_id[0] = 1
    history.clear()
    reject_cname_adds[0] = reject_cname
    fail_a_after[0] = fail_a_after_n
    a_add_ok[0] = 0
    for t, v, line in records:
        zone[next_id[0]] = {"type": t, "value": v, "line": line}
        next_id[0] += 1
    min_default[0] = 999
    check_invariant()


def make_probe(reachable):
    """构造只读探测桩：reachable 集合内返回可达，其余不可达。"""
    reach = set(reachable)

    def _probe(ip):
        return (ip in reach, 0.1 if ip in reach else 99.0)

    return _probe


def main():
    # ── 1. A 记录轮换：先建后删 ──────────────────────────────────
    reset([("A", "1.1.1.1", "default"), ("A", "2.2.2.2", "default"), ("A", "3.3.3.3", "default")])
    sk.converge_a_records(["9.9.9.9", "8.8.8.8"])
    case("轮换：最终记录正确", default_a() == ["8.8.8.8", "9.9.9.9"], str(default_a()))
    case("轮换：全程无零记录空窗", min_default[0] >= 1, f"最少={min_default[0]}")
    w = [i for i, h in enumerate(history) if h[0] in ("add", "update")]
    d = [i for i, h in enumerate(history) if h[0] == "delete"]
    case("轮换：先建后删顺序", (not d) or (w and max(w) < min(d)), str(history))

    # ── 2. 幂等 ─────────────────────────────────────────────────
    reset([("A", "1.1.1.1", "default")])
    case("幂等：同值不改", sk.converge_a_records(["1.1.1.1"]) is False)

    # ── 3. backup 直建成功（A/CNAME 可并存）→ 零空窗 ──────────────
    reset([("A", "1.1.1.1", "default"), ("A", "2.2.2.2", "default")], reject_cname=0)
    rc = sk.cmd_backup()
    case("backup 直建：返回 0 + CNAME 就位", rc == 0 and has_cname())
    case("backup 直建：旧 A 已清理", default_a() == [], str(default_a()))
    case("backup 直建：全程无零记录空窗", min_default[0] >= 1, f"最少={min_default[0]}")

    # ── 4. CNAME 被拒 → 退回"删 A 后补建"（成功）─────────────────
    reset([("A", "1.1.1.1", "default"), ("A", "2.2.2.2", "default")], reject_cname=1)
    rc = sk.cmd_backup()
    case("backup 回退：返回 0 + CNAME 就位", rc == 0 and has_cname())
    case("backup 回退：旧 A 已清理", default_a() == [], str(default_a()))

    # ── 5. CNAME 连续被拒 → 回滚写回 A，不留零记录 ────────────────
    reset([("A", "1.1.1.1", "default"), ("A", "2.2.2.2", "default")], reject_cname=99)
    buf, old_err = io.StringIO(), sys.stderr
    sys.stderr = buf
    rc = sk.cmd_backup()
    sys.stderr = old_err
    case("backup 全失败：返回 1", rc == 1)
    case("backup 全失败：回滚后 A 记录仍在", default_a() == ["1.1.1.1", "2.2.2.2"], str(default_a()))
    case("backup 全失败：结束时线路非空", default_count() >= 1)

    # ── 6. API 全面故障 → 干净返回 1 并提示人工介入 ────────────────
    def dead_call(action, **kw):
        if action == "DescribeDomainRecords":
            return fake_call(action, **kw)
        raise Exception("injected: API 全面故障")

    reset([("A", "1.1.1.1", "default")], reject_cname=99)
    sk.call = dead_call
    buf, old_err = io.StringIO(), sys.stderr
    sys.stderr = buf
    rc = sk.cmd_backup()
    sys.stderr = old_err
    case("回滚失败：返回 1 且提示人工介入", rc == 1 and "人工介入" in buf.getvalue())
    sk.call = fake_call  # 恢复假 API（dead_call 只服务上一场景）

    # ── 7. restore 正常：只读探测 → 删 default CNAME → 立即写 A ────
    # 假 API 已强制"CNAME 存在时写 A 被拒"，旧实现（先写 A 后删 CNAME）在此必然失败
    sk.probe = make_probe(sk.CANDIDATES)
    reset([("CNAME", sk.PAGES_HOST, "default"), ("CNAME", sk.PAGES_HOST, "oversea")])
    oversea_id = [i for i, r in zone.items() if r["line"] == "oversea"][0]
    oversea_before = dict(zone[oversea_id])
    rc = sk.cmd_restore()
    want = sorted(sk.CANDIDATES[:sk.KEEP_N])
    case("restore 正常：返回 0 + default A 就位", rc == 0 and default_a() == want, str(default_a()))
    case("restore 正常：default CNAME 已清理", not has_cname())
    case("restore 正常：oversea CNAME 原样未被触碰", zone.get(oversea_id) == oversea_before,
         str(zone.get(oversea_id)))
    dels = [i for i, h in enumerate(history) if h[0] == "delete"]
    writes = [i for i, h in enumerate(history) if h[0] in ("add", "update")]
    case("restore 顺序：先删 CNAME 后写 A", bool(dels and writes) and max(dels) < min(writes),
         str(history))

    # ── 8. restore 候选全不可达：一条记录都不动 ─────────────────────
    sk.probe = make_probe([])
    reset([("CNAME", sk.PAGES_HOST, "default"), ("CNAME", sk.PAGES_HOST, "oversea")])
    snapshot = {i: dict(r) for i, r in zone.items()}
    buf, old_err = io.StringIO(), sys.stderr
    sys.stderr = buf
    rc = sk.cmd_restore()
    sys.stderr = old_err
    case("restore 全不可达：返回 1", rc == 1)
    case("restore 全不可达：记录零改动", zone == snapshot and history == [], str(history))

    # ── 9. restore 写 A 失败：回滚 CNAME，不留裸域 ──────────────────
    sk.probe = make_probe(sk.CANDIDATES)
    reset([("CNAME", sk.PAGES_HOST, "default")], fail_a_after_n=0)
    buf, old_err = io.StringIO(), sys.stderr
    sys.stderr = buf
    rc = sk.cmd_restore()
    sys.stderr = old_err
    case("restore 写A失败：返回 1", rc == 1)
    case("restore 写A失败：CNAME 已回滚", has_cname() and default_a() == [],
         f"cname={has_cname()} A={default_a()}")
    case("restore 写A失败：default 线路非空", default_count() >= 1)

    # ── 10. restore 部分写 A 后失败且 CNAME 回滚被拒：线路仍非空 ─────
    reset([("CNAME", sk.PAGES_HOST, "default")], reject_cname=1, fail_a_after_n=1)
    buf, old_err = io.StringIO(), sys.stderr
    sys.stderr = buf
    rc = sk.cmd_restore()
    sys.stderr = old_err
    case("restore 部分写A失败：返回 1", rc == 1)
    case("restore 部分写A失败：线路非空（部分 A 在位）+ 明确告警",
         default_count() >= 1 and not has_cname() and "非裸域" in buf.getvalue(),
         f"default={default_count()} stderr={buf.getvalue().strip()[:80]}")

    print()
    print(f"结果：{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

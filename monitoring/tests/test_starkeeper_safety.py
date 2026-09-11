# -*- coding: utf-8 -*-
"""starkeeper_dns.py 的"零记录"安全回归测试（纯标准库，无需凭据、不联网）。

背景：2026-09-11 审计发现 `cmd_rotate` / `cmd_set_ips` / `cmd_backup` 都是"删光记录再重建"。
中途失败（限流/网络抖动/进程被杀）会让 starkeeper 的 default 线路变成**零记录** ——
国内用户直接解析失败，且下一轮推导为 empty 后不会自愈。这与该站 2026-06-27 P0 自伤事故
属同一失败模式，故加固为"先建后删"并为 backup 增加失败回滚。

本测试用假 `call()` 替换阿里云 API，逐操作检查核心不变量：
    **default 线路的记录数，在任何时刻都不能变成 0**（backup 的冲突回退路径除外，
    该路径天生存在一次调用的空窗，故要求失败时必须回滚）。

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


def reset(records=(), reject_cname=0):
    zone.clear()
    next_id[0] = 1
    history.clear()
    reject_cname_adds[0] = reject_cname
    for t, v, line in records:
        zone[next_id[0]] = {"type": t, "value": v, "line": line}
        next_id[0] += 1
    min_default[0] = 999
    check_invariant()


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

    print()
    print(f"结果：{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

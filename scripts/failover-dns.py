"""
DNS 容灾切换脚本 — A 记录架构版

用法:
  python3 failover-dns.py backup      切换 www default A → GH Pages IPs (备站)
  python3 failover-dns.py restore     恢复 www default A → CF 优选 IPs (主站)
  python3 failover-dns.py status      查看当前 DNS 状态
  python3 failover-dns.py backup --pimanager --dry-run

环境变量:
  ALI_KEY_ID          — 阿里云 AccessKey ID
  ALI_KEY_SECRET      — 阿里云 AccessKey Secret
  ALI_REGION           — 阿里云区域，默认 cn-hangzhou

DNS 架构 (post CF Pages 迁移):
  - www       → A records (default: CF优选IP ×N, oversea: CF优选IP ×N)
  - pimanager → A records (同 split-line 结构)
  - @ (root)  → A records (GH Pages IPs, 不变)

容灾策略:
  - PRIMARY (正常):  www default A → CF优选IPs → CF Pages
  - BACKUP (容灾):   www default A → GH Pages IPs → GitHub Pages
  - oversea 线路不受影响
  - root @ 记录不变
  - pimanager 可选容灾 (--pimanager)

状态文件:
  .failover_state.json  保存 backup 前的原始 A 记录 IP，restore 时读取。
  如果文件丢失，restore 使用内置 fallback CF IPs。
"""

import json
import os
import sys
import time
from pathlib import Path

from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_alidns20150109 import models as alidns_models
from alibabacloud_tea_openapi import models as open_api_models

DOMAIN = "chenxiuniverse.top"
TTL = 600

# GitHub Pages 已知 IP 段 (Fastly CDN)
GH_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]

# 如果状态文件丢失，restore 时用这些已知可用的 CF IP 兜底
FALLBACK_CF_IPS = [
    "104.26.8.55",
    "104.26.9.55",
    "172.67.70.227",
]

# 状态文件路径（项目根目录）
STATE_FILE = Path(__file__).resolve().parent.parent / ".failover_state.json"

# 需要容灾的子域（仅 default 线路，oversea 不动）
# 注: pimanager 使用 CNAME 架构，不走 A 记录容灾
FAILOVER_TARGETS = [
    {"rr": "www", "line": "default"},
]


# ---------------------------------------------------------------------------
# 阿里云 DNS 客户端
# ---------------------------------------------------------------------------

def get_client() -> AlidnsClient:
    """创建阿里云 DNS 客户端。缺少凭据时立即报错退出。"""
    key_id = os.environ.get("ALI_KEY_ID")
    key_secret = os.environ.get("ALI_KEY_SECRET")

    if not key_id:
        print("❌ 缺少环境变量 ALI_KEY_ID", file=sys.stderr)
        sys.exit(1)
    if not key_secret:
        print("❌ 缺少环境变量 ALI_KEY_SECRET", file=sys.stderr)
        sys.exit(1)

    region = os.environ.get("ALI_REGION", "cn-hangzhou")

    client = AlidnsClient(open_api_models.Config(
        access_key_id=key_id,
        access_key_secret=key_secret,
        region_id=region,
    ))
    client._endpoint = f"alidns.{region}.aliyuncs.com"
    return client


# ---------------------------------------------------------------------------
# DNS 记录查询 / 更新
# ---------------------------------------------------------------------------

def get_records(client: AlidnsClient, rr: str, line: str) -> list[dict]:
    """
    查询指定子域+线路的 A 记录。
    返回 [{"record_id": ..., "value": ..., "rr": ..., "line": ...}, ...]
    """
    req = alidns_models.DescribeDomainRecordsRequest(
        domain_name=DOMAIN,
        rrkey_word=rr,
        type_key_word="A",
        line=line,
    )
    resp = client.describe_domain_records(req)
    records = [
        {"record_id": r.record_id, "value": r.value, "rr": r.rr, "line": r.line}
        for r in resp.body.domain_records.record
        if r.rr == rr and r.type == "A" and r.line == line
    ]
    return records


def get_current_ips(client: AlidnsClient, rr: str, line: str) -> list[str]:
    """获取当前 DNS A 记录的 IP 列表（排序后）。"""
    records = get_records(client, rr, line)
    return sorted(r["value"] for r in records)


def update_to_ips(
    client: AlidnsClient,
    rr: str,
    line: str,
    new_ips: list[str],
    dry_run: bool = False,
) -> bool:
    """
    更新某个子域+线路的 A 记录，使其 IP 列表与 new_ips 一致。

    策略:
      1. API 查询现有记录（自动发现 RecordId）
      2. 新旧 IP 排序后完全一致 → 跳过
      3. 逐个更新已有记录，不够则新增，多余则删除

    返回 True 表示有变更（或 dry-run 时有潜在变更）。
    """
    records = get_records(client, rr, line)
    current_ips = [r["value"] for r in records]
    current_ids = [r["record_id"] for r in records]

    if sorted(new_ips) == sorted(current_ips):
        return False  # 无变更

    if dry_run:
        return True  # 有潜在变更，但不执行

    # 更新已有记录 / 新增
    for i, ip in enumerate(new_ips):
        if i < len(current_ids):
            req = alidns_models.UpdateDomainRecordRequest(
                record_id=current_ids[i],
                rr=rr,
                type="A",
                value=ip,
                line=line,
                ttl=TTL,
            )
            client.update_domain_record(req)
            old = current_ips[i] if i < len(current_ips) else "?"
            print(f"  更新 #{i + 1}: {old} -> {ip}")
        else:
            req = alidns_models.AddDomainRecordRequest(
                domain_name=DOMAIN,
                rr=rr,
                type="A",
                value=ip,
                line=line,
                ttl=TTL,
            )
            client.add_domain_record(req)
            print(f"  新增 #{i + 1}: {ip}")

    # 删除多余的旧记录
    for j in range(len(new_ips), len(current_ids)):
        try:
            req = alidns_models.DeleteDomainRecordRequest(record_id=current_ids[j])
            client.delete_domain_record(req)
            old = current_ips[j] if j < len(current_ips) else "?"
            print(f"  删除多余 #{j + 1}: {old}")
        except Exception as e:
            print(f"  ⚠ 删除 #{j + 1} 失败: {e}")

    return True


# ---------------------------------------------------------------------------
# 状态判断
# ---------------------------------------------------------------------------

def detect_state(ips: list[str]) -> str:
    """根据 IP 列表判断当前是 PRIMARY / BACKUP / MIXED。"""
    ips_set = set(ips)
    gh_set = set(GH_PAGES_IPS)

    if not ips_set:
        return "EMPTY"

    if ips_set & gh_set:
        if ips_set.issubset(gh_set):
            return "BACKUP"
        else:
            return "MIXED"

    return "PRIMARY"


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

def cmd_status(client: AlidnsClient) -> None:
    """查看当前 DNS 容灾状态。"""
    print(f"{'=' * 60}")
    print(f"DNS 容灾状态  @  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"域名: {DOMAIN}")
    print(f"{'=' * 60}")

    for target in FAILOVER_TARGETS:
        rr = target["rr"]
        line = target["line"]
        records = get_records(client, rr, line)
        ips = sorted(r["value"] for r in records)
        state = detect_state(ips)

        icon = {"PRIMARY": "[PRIMARY]", "BACKUP": "[BACKUP]", "MIXED": "[MIXED]", "EMPTY": "[EMPTY]"}.get(state, "[?]")
        label = f"{rr}.{DOMAIN} ({line})"
        print(f"\n  {label}: {icon} {state}")
        for r in records:
            print(f"    {r['value']}  (RecordId: {r['record_id']})")
        if not records:
            print(f"    (无记录)")

    # 状态文件
    print(f"\n  状态文件: {STATE_FILE} ", end="")
    if STATE_FILE.exists():
        print("(存在)")
        try:
            state_data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            print(f"    保存时间: {state_data.get('timestamp', '?')}")
            for target in FAILOVER_TARGETS:
                rr = target["rr"]
                saved = state_data.get(rr, [])
                if saved:
                    print(f"    保存的 {rr}: {saved}")
        except Exception as e:
            print(f"    ⚠ 无法解析: {e}")
    else:
        print("(不存在 — restore 时将使用内置 fallback IPs)")

    print()


def cmd_backup(
    client: AlidnsClient,
    dry_run: bool = False,
    include_pimanager: bool = False,
) -> None:
    """切换到备站: 将 default 线路 A 记录指向 GH Pages IPs。"""
    targets = FAILOVER_TARGETS if include_pimanager else FAILOVER_TARGETS[:1]

    # 1. 保存当前状态
    state: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": "backup",
    }
    for target in targets:
        rr = target["rr"]
        line = target["line"]
        state[rr] = get_current_ips(client, rr, line)

    if dry_run:
        print(f"💾 [DRY RUN] 将保存状态到 {STATE_FILE}")
        print(f"   {json.dumps(state, indent=2)}")
    else:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"💾 状态已保存到 {STATE_FILE}")

    # 2. 切换到 GH Pages IPs
    changed_any = False
    for target in targets:
        rr = target["rr"]
        line = target["line"]
        current_ips = get_current_ips(client, rr, line)

        print(f"\n  {rr}.{DOMAIN} ({line}):")
        print(f"    当前: {current_ips}")
        print(f"    目标: {GH_PAGES_IPS}")

        if update_to_ips(client, rr, line, GH_PAGES_IPS, dry_run):
            changed_any = True
            if dry_run:
                print(f"    [DRY RUN] 将切换 {rr} -> GH Pages IPs")
            else:
                print(f"    OK {rr} 已切到 GH Pages (备站)")
        else:
            print(f"    (已是备站，跳过)")

    if dry_run:
        print(f"\n⚠ DRY RUN 完成 — 未实际修改 DNS")
    elif changed_any:
        print(f"\nOK DNS 已切到备站 (GitHub Pages) @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print(f"\n  无需变更")


def cmd_restore(
    client: AlidnsClient,
    dry_run: bool = False,
    include_pimanager: bool = False,
) -> None:
    """恢复到主站: 将 default 线路 A 记录恢复为 CF 优选 IPs。"""
    targets = FAILOVER_TARGETS if include_pimanager else FAILOVER_TARGETS[:1]

    # 1. 读取状态文件
    saved_state: dict = {}
    if STATE_FILE.exists():
        try:
            saved_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            print(f"📂 读取状态文件 (保存于 {saved_state.get('timestamp', '?')})")
        except Exception as e:
            print(f"⚠ 状态文件解析失败: {e}，使用 fallback IPs", file=sys.stderr)
    else:
        print(f"⚠ 状态文件不存在 ({STATE_FILE})，使用内置 fallback CF IPs", file=sys.stderr)

    # 2. 逐个恢复
    changed_any = False
    for target in targets:
        rr = target["rr"]
        line = target["line"]
        current_ips = get_current_ips(client, rr, line)

        target_ips = saved_state.get(rr, []) if saved_state else []
        if not target_ips:
            target_ips = list(FALLBACK_CF_IPS)
            print(f"  ⚠ {rr}: 无保存的 IP，fallback -> {target_ips}")

        print(f"\n  {rr}.{DOMAIN} ({line}):")
        print(f"    当前: {current_ips}")
        print(f"    目标: {target_ips}")

        if update_to_ips(client, rr, line, target_ips, dry_run):
            changed_any = True
            if dry_run:
                print(f"    [DRY RUN] 将恢复 {rr} -> CF 优选 IPs")
            else:
                print(f"    OK {rr} 已恢复到 CF 优选 IPs (主站)")
        else:
            print(f"    (已是主站状态，跳过)")

    if dry_run:
        print(f"\n⚠ DRY RUN 完成 — 未实际修改 DNS")
    elif changed_any:
        print(f"\nOK DNS 已恢复到主站 (CF Pages) @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print(f"\n  无需变更")


def print_usage() -> None:
    """打印用法并退出。"""
    print(__doc__)
    print("选项:")
    print("  --dry-run      不实际修改 DNS，仅预览")
    print("  --pimanager    同时切换 pimanager 子域（默认仅 www）")
    print()
    print("命令:")
    print("  backup   切换到备站 (www default A -> GH Pages IPs)")
    print("  restore  恢复到主站 (www default A -> 保存的 CF 优选 IPs)")
    print("  status   查看当前 DNS 容灾状态")
    sys.exit(1)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    dry_run = "--dry-run" in flags
    include_pimanager = "--pimanager" in flags

    if not args:
        print_usage()

    action = args[0]
    if action not in ("backup", "restore", "status"):
        print(f"❌ 未知命令: {action}", file=sys.stderr)
        print_usage()

    client = get_client()

    if action == "status":
        cmd_status(client)
    elif action == "backup":
        cmd_backup(client, dry_run=dry_run, include_pimanager=include_pimanager)
    elif action == "restore":
        cmd_restore(client, dry_run=dry_run, include_pimanager=include_pimanager)


if __name__ == "__main__":
    main()

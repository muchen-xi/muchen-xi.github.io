"""
分线路 DNS 更新脚本
用法: python3 monitoring/scripts/update-dns.py [result.csv] [china_result.csv]

读取境外测速和境内测速结果，分别更新阿里云 DNS 的:
  - 境外线路 (oversea): 用境外测速的 Top 3 IP
  - 默认线路 (default): 用境内测速的 Top 3 IP → 国内用户走这条

目标子域: www
(chenxiuniverse.top 根域保持 GH Pages 301 跳转，不动)

环境变量:
  ALI_KEY_ID          — 阿里云 AccessKey ID
  ALI_KEY_SECRET      — 阿里云 AccessKey Secret
  ALI_REGION           — 阿里云区域，默认 cn-hangzhou

变更检测: 新旧 IP 完全一致则跳过更新，节省 API 配额。

RecordId 管理:
  优先从环境变量读取 (RECORD_IDS_WWW_DEFAULT 等，逗号分隔)。
  未设置时自动从 DNS 查询获取。
"""

import csv
import json
import os
import sys
import time
from pathlib import Path

from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_alidns20150109 import models as alidns_models
from alibabacloud_tea_openapi import models as open_api_models

DOMAIN = "chenxiuniverse.top"
TOP_N = 3  # 每条线路保留 N 个最优 IP
TTL = 600

# 需要管理的子域和线路
TARGETS = [
    {"rr": "www", "line": "default", "csv": "china"},      # 国内 → 境内优选
    {"rr": "www", "line": "oversea", "csv": "overseas"},    # 境外 → 境外优选
    {"rr": "pimanager", "line": "default", "csv": "china"},
    {"rr": "pimanager", "line": "oversea", "csv": "overseas"},
    {"rr": "health", "line": "default", "csv": "china"},     # 健康仪表盘
    {"rr": "health", "line": "oversea", "csv": "overseas"},
    {"rr": "history", "line": "default", "csv": "china"},    # 网页进化史
    {"rr": "history", "line": "oversea", "csv": "overseas"},
    {"rr": "starkeeper", "line": "default", "csv": "china"},  # 星钥官网（oversea 保持官方 CNAME 不动）
]

# RecordId 环境变量映射
RECORD_ID_ENV_MAP = {
    ("www", "default"): "RECORD_IDS_WWW_DEFAULT",
    ("www", "oversea"): "RECORD_IDS_WWW_OVERSEA",
    ("pimanager", "default"): "RECORD_IDS_PIMANAGER_DEFAULT",
    ("pimanager", "oversea"): "RECORD_IDS_PIMANAGER_OVERSEA",
    ("health", "default"): "RECORD_IDS_HEALTH_DEFAULT",
    ("health", "oversea"): "RECORD_IDS_HEALTH_OVERSEA",
    ("history", "default"): "RECORD_IDS_HISTORY_DEFAULT",
    ("history", "oversea"): "RECORD_IDS_HISTORY_OVERSEA",
}


# 容灾切换目标（mode=backup 时跳过，避免覆盖容灾 DNS 切换）
FAILOVER_TARGETS = {"www", "pimanager"}


def is_failover_backup() -> bool:
    """容灾备份模式检测：读取 .failover_count.json，mode=backup 时跳过容灾目标更新。"""
    state_path = Path(__file__).resolve().parent.parent / ".failover_count.json"
    try:
        d = json.loads(state_path.read_text(encoding="utf-8"))
        return d.get("mode") == "backup"
    except Exception:
        return False


def is_starkeeper_backup() -> bool:
    """星钥容灾备份模式检测：starkeeper_state.json mode=backup 时跳过 starkeeper 更新
    （starkeeper 容灾时 default 线路被切为官方 CNAME，优选流程不得改回 A 记录）。"""
    state_path = Path(__file__).resolve().parent.parent / "starkeeper_state.json"
    try:
        d = json.loads(state_path.read_text(encoding="utf-8"))
        return d.get("mode") == "backup"
    except Exception:
        return False


def read_ips(csv_path: str, top_n: int = TOP_N) -> list[str]:
    """读取 CloudflareST 格式 CSV，返回前 N 个 IP"""
    ips = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ip = row.get("IP地址") or row.get("IP 地址") or row.get("IP")
                if ip:
                    ips.append(ip)
    except FileNotFoundError:
        print(f"⚠ {csv_path} 不存在，跳过")
    return ips[:top_n]


def get_record_ids_from_env(rr: str, line: str) -> list[str] | None:
    """从环境变量获取预配置的 RecordId 列表"""
    env_key = RECORD_ID_ENV_MAP.get((rr, line))
    if not env_key:
        return None
    val = os.environ.get(env_key, "").strip()
    if val:
        return [v.strip() for v in val.split(",") if v.strip()]
    return None


def get_record_ids_from_api(client: AlidnsClient, rr: str, line: str) -> list[str]:
    """从阿里云 API 查询现有 A 记录的 RecordId"""
    req = alidns_models.DescribeDomainRecordsRequest(
        domain_name=DOMAIN,
        rrkey_word=rr,
        type_key_word="A",
        line=line,
    )
    resp = client.describe_domain_records(req)
    records = [
        r for r in resp.body.domain_records.record
        if r.rr == rr and r.type == "A" and r.line == line
    ]
    return [r.record_id for r in records]


def get_current_ips(client: AlidnsClient, rr: str, line: str) -> list[str]:
    """获取当前 DNS 记录的 IP 列表"""
    req = alidns_models.DescribeDomainRecordsRequest(
        domain_name=DOMAIN,
        rrkey_word=rr,
        type_key_word="A",
        line=line,
    )
    resp = client.describe_domain_records(req)
    records = [
        r for r in resp.body.domain_records.record
        if r.rr == rr and r.type == "A" and r.line == line
    ]
    return sorted([r.value for r in records])


def set_ips(
    client: AlidnsClient,
    rr: str,
    line: str,
    new_ips: list[str],
    dry_run: bool = False,
) -> bool:
    """
    更新某个子域+线路的 A 记录为新 IP 列表。
    返回 True 表示有变更，False 表示跳过。

    策略:
      1. 查询现有记录（优先环境变量 RecordId，否则 API 查询）
      2. 比较新旧 IP，一致则跳过
      3. 不够就新增，多了就删除，已有的逐个更新
    """
    # 获取现有 RecordId（优先环境变量，否则 API 查询）
    raw_record_ids = get_record_ids_from_env(rr, line)
    if raw_record_ids is None:
        raw_record_ids = get_record_ids_from_api(client, rr, line)

    # 验证 RecordId 有效性，过滤掉已删除的记录
    record_ids = []
    current_ips = []
    for rid in raw_record_ids:
        try:
            req = alidns_models.DescribeDomainRecordInfoRequest(record_id=rid)
            resp = client.describe_domain_record_info(req)
            record_ids.append(rid)
            current_ips.append(resp.body.value)
        except Exception:
            # 记录可能已被删除，跳过
            print(f"  ⚠ RecordId {rid} 已失效，跳过")

    # 变更检测
    if sorted(new_ips) == sorted(current_ips):
        print(f"  {rr}.{DOMAIN} ({line}): IP 未变，跳过")
        return False

    if dry_run:
        print(f"  [DRY RUN] {rr}.{DOMAIN} ({line}): {current_ips} → {new_ips}")
        return True

    # 更新/新增
    for i, ip in enumerate(new_ips):
        if i < len(record_ids):
            old = current_ips[i] if i < len(current_ips) else "?"
            if old == ip:
                # 同值更新会触发阿里云 DomainRecordDuplicate（400）
                print(f"  跳过 #{i+1}: {old}（值未变）")
                continue
            # 更新现有记录
            req = alidns_models.UpdateDomainRecordRequest(
                record_id=record_ids[i],
                rr=rr,
                type="A",
                value=ip,
                line=line,
                ttl=TTL,
            )
            client.update_domain_record(req)
            print(f"  更新 #{i+1}: {old} → {ip}")
        else:
            # 新增记录
            req = alidns_models.AddDomainRecordRequest(
                domain_name=DOMAIN,
                rr=rr,
                type="A",
                value=ip,
                line=line,
                ttl=TTL,
            )
            try:
                client.add_domain_record(req)
                print(f"  新增 #{i+1}: {ip}")
            except Exception as e:
                if "DomainRecordDuplicate" in str(e):
                    # 记录已存在（例如对位偏移时新 IP 已在 DNS 中），幂等跳过
                    print(f"  跳过 #{i+1}: {ip}（已存在）")
                else:
                    raise

    # 删除多余的旧记录
    for j in range(len(new_ips), len(record_ids)):
        try:
            req = alidns_models.DeleteDomainRecordRequest(record_id=record_ids[j])
            client.delete_domain_record(req)
            old = current_ips[j] if j < len(current_ips) else "?"
            print(f"  删除多余: {old}")
        except Exception as e:
            print(f"  删除 #{j+1} 失败: {e}")

    return True


def main():
    overseas_csv = sys.argv[1] if len(sys.argv) > 1 else "result.csv"
    china_csv = sys.argv[2] if len(sys.argv) > 2 else "china_result.csv"
    dry_run = "--dry-run" in sys.argv

    # 读取 IP
    overseas_ips = read_ips(overseas_csv)
    china_ips = read_ips(china_csv)

    if not overseas_ips and not china_ips:
        print("❌ 无可用 IP，不更新 DNS")
        return

    print(f"境外优选 IP ({len(overseas_ips)}): {overseas_ips}")
    print(f"境内优选 IP ({len(china_ips)}): {china_ips}")

    # 容灾备份模式保护：跳过容灾目标，避免 IP 优选把 DNS 切回主站破坏容灾
    backup_mode = is_failover_backup()
    if backup_mode:
        print("⚠ 检测到容灾备份模式 (mode=backup) — 跳过 www/pimanager 更新，避免破坏容灾切换")
    starkeeper_backup = is_starkeeper_backup()
    if starkeeper_backup:
        print("⚠ 星钥官网容灾备份模式 (starkeeper_state.json) — 跳过 starkeeper 更新")

    if dry_run:
        print("⚠ DRY RUN 模式 — 不会实际修改 DNS\n")

    # 检查必需的环境变量
    key_id = os.environ.get("ALI_KEY_ID")
    key_secret = os.environ.get("ALI_KEY_SECRET")
    if not key_id or not key_secret:
        print("❌ 缺少 ALI_KEY_ID 或 ALI_KEY_SECRET 环境变量", file=sys.stderr)
        print("  请在 GitHub Secrets 或环境中设置这两个变量", file=sys.stderr)
        sys.exit(1)

    # 连接阿里云 DNS
    region = os.environ.get("ALI_REGION", "cn-hangzhou")
    client = AlidnsClient(open_api_models.Config(
        access_key_id=key_id,
        access_key_secret=key_secret,
        region_id=region,
    ))
    client._endpoint = f"alidns.{region}.aliyuncs.com"

    # 先确定哪些子域今天有 default 线路数据（避免仅更新 oversea 而无 default 兜底）
    default_ok = set()
    for target in TARGETS:
        if target["line"] == "default":
            ips = china_ips if target["csv"] == "china" else overseas_ips
            if ips:
                default_ok.add(target["rr"])

    # 逐个子域+线路更新
    changed = False
    for target in TARGETS:
        rr = target["rr"]
        line = target["line"]
        csv_source = target["csv"]

        if backup_mode and rr in FAILOVER_TARGETS:
            print(f"⚠ {rr}.{DOMAIN} ({line}): 容灾备份模式，跳过（保护容灾切换）")
            continue
        if rr == "starkeeper" and starkeeper_backup:
            print(f"⚠ {rr}.{DOMAIN} ({line}): 星钥容灾备份模式，跳过")
            continue

        ips = china_ips if csv_source == "china" else overseas_ips
        if not ips:
            print(f"⚠ {rr}.{DOMAIN} ({line}): 无对应 IP，跳过")
            continue
        if line == "oversea" and rr not in default_ok:
            print(f"⚠ {rr}.{DOMAIN} ({line}): 今天无 {rr} 的 default 线路数据，跳过 oversea 更新（避免仅剩 oversea 无兜底记录）")
            continue

        print(f"\n{rr}.{DOMAIN} ({line}):")
        if set_ips(client, rr, line, ips, dry_run):
            changed = True

    if not changed:
        print("\n✅ 所有记录均为最新，无需更新")
    elif not dry_run:
        print(f"\n✅ DNS 更新完成 @ {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 输出 JSON 摘要（写入 GitHub Step Summary 或 stdout）
    summary = {
        "domain": DOMAIN,
        "overseas_ips": overseas_ips,
        "china_ips": china_ips,
        "changed": changed,
        "dry_run": dry_run,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    summary_json = json.dumps(summary, ensure_ascii=False)
    # GitHub Actions: 写入 $GITHUB_STEP_SUMMARY
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as f:
                f.write(f"## DNS 更新摘要\n\n```json\n{summary_json}\n```\n")
        except OSError:
            pass
    # 也输出到 stdout 供手动运行时查看
    print(f"\n📋 {summary_json}")


if __name__ == "__main__":
    main()

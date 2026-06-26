"""
分线路 DNS 更新脚本
用法: python3 scripts/update_dns.py [result.csv] [china_result.csv]

读取境外测速和境内测速结果，分别更新阿里云 DNS 的:
  - 境外线路 (oversea): 用境外测速的 Top 3 IP
  - 默认线路 (default): 用境内测速的 Top 3 IP → 国内用户走这条

目标子域: www, pimanager
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
]

# RecordId 环境变量映射
RECORD_ID_ENV_MAP = {
    ("www", "default"): "RECORD_IDS_WWW_DEFAULT",
    ("www", "oversea"): "RECORD_IDS_WWW_OVERSEA",
    ("pimanager", "default"): "RECORD_IDS_PIMANAGER_DEFAULT",
    ("pimanager", "oversea"): "RECORD_IDS_PIMANAGER_OVERSEA",
}


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
        rr_keyword=rr,
        type_keyword="A",
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
        rr_keyword=rr,
        type_keyword="A",
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
            old = current_ips[i] if i < len(current_ips) else "?"
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
            client.add_domain_record(req)
            print(f"  新增 #{i+1}: {ip}")

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

    # 逐个子域+线路更新
    changed = False
    for target in TARGETS:
        rr = target["rr"]
        line = target["line"]
        csv_source = target["csv"]

        ips = china_ips if csv_source == "china" else overseas_ips
        if not ips:
            print(f"⚠ {rr}.{DOMAIN} ({line}): 无对应 IP，跳过")
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
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
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

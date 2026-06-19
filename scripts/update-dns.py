"""
解析 CloudflareSpeedTest 测速结果，更新阿里云 DNS A 记录
用法: python3 update-dns.py result.csv
环境变量: ALI_KEY_ID, ALI_KEY_SECRET
"""
import csv, os, sys, time
from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_alidns20150109 import models as alidns_models
from alibabacloud_tea_openapi import models as open_api_models

DOMAIN = 'chenxiuniverse.top'
TOP_N = 3  # 保留前 N 个最优 IP

def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else 'result.csv'

    # 1. 解析最优 IP
    best_ips = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ip = row.get('IP地址') or row.get('IP')
            if ip:
                best_ips.append(ip)
    best_ips = best_ips[:TOP_N]
    print(f'优选 IP ({len(best_ips)}): {best_ips}')

    if not best_ips:
        print('未找到可用 IP，不更新 DNS')
        return

    # 2. 连接阿里云 DNS
    client = AlidnsClient(open_api_models.Config(
        access_key_id=os.environ['ALI_KEY_ID'],
        access_key_secret=os.environ['ALI_KEY_SECRET'],
    ))
    client._endpoint = 'alidns.cn-hangzhou.aliyuncs.com'

    # 3. 获取现有 A 记录
    req = alidns_models.DescribeDomainRecordsRequest(domain_name=DOMAIN, rr_keyword='@', type_keyword='A')
    resp = client.describe_domain_records(req)
    records = [r for r in resp.body.domain_records.record if r.rr == '@' and r.type == 'A']
    print(f'现有 @ A 记录: {len(records)} 条')

    # 4. 更新/新增/删除
    for i, ip in enumerate(best_ips):
        req_update = alidns_models.UpdateDomainRecordRequest(
            record_id=records[i].record_id,
            rr='@', type='A', value=ip,
            line=records[i].line if hasattr(records[i], 'line') else 'default',
            ttl=600,
        )
        client.update_domain_record(req_update)
        print(f'  更新 #{i+1}: {records[i].value} → {ip}')

    # 删除多余的旧记录
    for j in range(len(best_ips), len(records)):
        req_del = alidns_models.DeleteDomainRecordRequest(record_id=records[j].record_id)
        client.delete_domain_record(req_del)
        print(f'  删除多余记录: {records[j].value}')

    # 5. 如果记录不够，新增
    for k in range(len(records), len(best_ips)):
        req_add = alidns_models.AddDomainRecordRequest(
            domain_name=DOMAIN, rr='@', type='A',
            value=best_ips[k], line='default', ttl=600,
        )
        client.add_domain_record(req_add)
        print(f'  新增记录: {best_ips[k]}')

    print('DNS 更新完成 ✅')
    print(f'下次扫描: {time.strftime("%Y-%m-%d %H:%M:%S")}')


if __name__ == '__main__':
    main()

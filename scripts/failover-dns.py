"""
DNS 容灾切换脚本
用法: python3 failover-dns.py backup|restore
环境变量: ALI_KEY_ID, ALI_KEY_SECRET
"""
import os, sys
from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_alidns20150109 import models as alidns_models
from alibabacloud_tea_openapi import models as open_api_models

DOMAIN = 'chenxiuniverse.top'
PRIMARY_WWW = 'muchen-xi.github.io'
BACKUP_WWW = 'backup.chenxiuniverse.top'
PRIMARY_A = ['185.199.108.153', '185.199.109.153', '185.199.110.153', '185.199.111.153']
BACKUP_A = ['104.21.80.200', '172.67.180.100', '104.21.64.150']

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'check'
    client = AlidnsClient(open_api_models.Config(
        access_key_id=os.environ['ALI_KEY_ID'],
        access_key_secret=os.environ['ALI_KEY_SECRET'],
    ))
    client._endpoint = 'alidns.cn-hangzhou.aliyuncs.com'

    # 获取所有记录
    req = alidns_models.DescribeDomainRecordsRequest(domain_name=DOMAIN)
    resp = client.describe_domain_records(req)
    records = resp.body.domain_records.record

    www_default = [r for r in records if r.rr == 'www' and r.type == 'CNAME' and r.line == 'default']
    a_records = [r for r in records if r.rr == '@' and r.type == 'A']

    if action == 'backup':
        new_www = BACKUP_WWW
        new_a = BACKUP_A
    elif action == 'restore':
        new_www = PRIMARY_WWW
        new_a = PRIMARY_A
    else:
        # check mode: just report current state
        if www_default:
            v = www_default[0].value
            state = '备站' if v == BACKUP_WWW else '主站'
            print(f'当前 www: {v} ({state})')
        return

    # 更新 www CNAME
    if www_default:
        r = www_default[0]
        if r.value != new_www:
            client.update_domain_record(alidns_models.UpdateDomainRecordRequest(
                record_id=r.record_id, rr='www', type='CNAME',
                value=new_www, line='default', ttl=600,
            ))
            print(f'www CNAME: {r.value} → {new_www}')

    # 更新 @ A 记录
    for i, ip in enumerate(new_a):
        if i < len(a_records):
            if a_records[i].value != ip:
                client.update_domain_record(alidns_models.UpdateDomainRecordRequest(
                    record_id=a_records[i].record_id, rr='@', type='A',
                    value=ip, line='default', ttl=600,
                ))
                print(f'@ A #{i}: {a_records[i].value} → {ip}')

    print(f'✅ DNS 已切换到: {"备站(CF Pages)" if action == "backup" else "主站(GitHub Pages)"}')


if __name__ == '__main__':
    main()

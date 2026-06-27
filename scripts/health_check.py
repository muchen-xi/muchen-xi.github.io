"""
IP 可用性验证脚本 — 直接 HTTPS 验证（零 Worker 依赖）
用法: python3 scripts/health_check.py [input.csv] [output.csv] [--target example.com]

输入: 候选 IP CSV（CloudflareST 格式或纯 IP 列表）
输出: 有效 IP 的 CSV（通过直接 HTTPS 验证的 IP）

验证方式:
  对每个候选 IP，curl --resolve 强制通过该 IP 连接目标站点。
  HTTP 200 + body 非空 → 有效。不再依赖任何 Worker。

P0 v3 修复 (2026-06-27):
  - 彻底移除 Worker 依赖（health.m20081225.workers.dev 配额打爆后全链阻断）
  - 改为直接 curl 目标站点验证回源，测试用户真实路径
  - 更可靠、更简单、零外部依赖

环境变量: 无需
"""

import csv
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_TARGET = "www.chenxiuniverse.top"
CSV_HEADER = "IP 地址,已发送,已接收,丢包率,平均延迟,下载速度 (MB/s)"
TIMEOUT = 15  # 单次 curl 超时秒数
MAX_WORKERS = 10  # 并行验证数


def read_ips(csv_path: str) -> list[str]:
    """读取候选 IP 列表"""
    ips = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ip = row.get("IP地址") or row.get("IP 地址") or row.get("IP")
                if ip:
                    ips.append(ip)
    except FileNotFoundError:
        print(f"⚠ {csv_path} 不存在")
    return ips


def check_ip(ip: str, target: str) -> dict | None:
    """
    直接通过 --resolve 强制连接指定 CF IP，验证目标站点是否可达。
    返回 {ip, latency_ms, http_code, size} 或 None（不可达）。
    """
    try:
        start = time.monotonic()
        result = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-w",
                "%{http_code}\n%{time_total}\n%{size_download}",
                "--resolve", f"{target}:443:{ip}",
                "--connect-timeout", "10",
                "--max-time", str(TIMEOUT),
                f"https://{target}/",
            ],
            capture_output=True, text=True, timeout=TIMEOUT + 5,
        )
        elapsed = time.monotonic() - start

        lines = result.stdout.strip().split("\n")
        http_code = lines[0].strip() if lines else "000"
        curl_time = float(lines[1]) if len(lines) > 1 else elapsed
        size = int(lines[2]) if len(lines) > 2 else 0

        # HTTP 200 且有内容返回 → 有效
        if http_code == "200" and size > 100:
            return {
                "ip": ip,
                "latency_ms": round(curl_time * 1000, 2),
                "http_code": http_code,
                "size": size,
            }

        # HTTP 200 但 body 太小（可能是 CF 错误页）
        if http_code == "200" and size <= 100:
            print(f"  ⚠️ {ip}: HTTP 200 但响应仅 {size} 字节（疑似错误页）")
            return None

        # 其他状态码
        return None

    except (subprocess.TimeoutExpired, Exception):
        return None


def main():
    input_csv = sys.argv[1] if len(sys.argv) > 1 else "result.csv"
    output_csv = sys.argv[2] if len(sys.argv) > 2 else "valid_ips.csv"

    # 解析 --target 参数
    target = DEFAULT_TARGET
    extra_args = []
    for arg in sys.argv[3:]:
        if arg.startswith("--target="):
            target = arg.split("=", 1)[1]
        else:
            extra_args.append(arg)

    # 1. 读取候选 IP
    candidate_ips = read_ips(input_csv)
    if not candidate_ips:
        print("❌ 无候选 IP")
        sys.exit(1)

    print(f"直接 HTTPS 验证 {len(candidate_ips)} 个候选 IP → {target} ...")

    # 2. 并行验证
    valid = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_ip, ip, target): ip for ip in candidate_ips}
        for f in as_completed(futures):
            r = f.result()
            if r:
                valid.append(r)
                print(f"  ✅ {r['ip']}: {r['latency_ms']}ms (HTTP {r['http_code']}, {r['size']}B)")
            else:
                print(f"  ❌ {futures[f]}: 不可达")

    # 3. 排序输出（按延迟升序）
    valid.sort(key=lambda x: x["latency_ms"])
    valid_count = len(valid)
    print(f"\n有效 IP: {valid_count}/{len(candidate_ips)}")

    # 4. 阈值判断：阻断坏 IP 部署
    if valid_count == 0:
        print("❌ 健康验证失败: 所有IP均不可达，阻断部署", file=sys.stderr)
        sys.exit(1)
    elif valid_count == 1:
        print(f"⚠️ 仅 {valid_count} 个IP通过验证，不足以冗余部署", file=sys.stderr)
    else:
        print(f"✅ {valid_count} 个IP通过健康验证")

    # 5. 写入 CSV（兼容 CloudflareST 格式，供 update-dns.py 消费）
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        f.write(CSV_HEADER + "\n")
        for r in valid:
            ms = r["latency_ms"]
            speed = round(max(0.5, 200 / max(ms, 1)), 2)
            f.write(f"{r['ip']},4,4,0.00,{ms},{speed}\n")

    print(f"结果已写入 {output_csv}")

    # 输出供 workflow 读取
    with open(output_csv, newline="", encoding="utf-8") as f:
        print(f.read())


if __name__ == "__main__":
    main()

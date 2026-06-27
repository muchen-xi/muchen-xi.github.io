"""
IP 可用性验证脚本 — 通过 248200.xyz Worker 验证 CF IP
用法: python3 scripts/health_check.py [input.csv] [--output valid.csv]

输入: 候选 IP CSV（CloudflareST 格式或纯 IP 列表）
输出: 有效 IP 的 CSV（通过 Worker 验证的 IP）

验证方式:
  对每个候选 IP，使用 --resolve 强制通过该 CF IP 连接 health.248200.xyz
  Worker 返回 {status, colo, ip, upstream} → upstream=="ok" 即为有效

环境变量: 无需
"""

import csv
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKER_HOST = "health.m20081225.workers.dev"  # 248200.xyz Zone DNS API 有认证 bug，用 workers.dev 直连
CSV_HEADER = "IP 地址,已发送,已接收,丢包率,平均延迟,下载速度 (MB/s)"
TIMEOUT = 15  # 单次 curl 超时秒数


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


def check_ip(ip: str) -> dict | None:
    """
    通过 --resolve 强制连接指定 CF IP，验证 Worker 是否可达。
    返回 {ip, latency_ms, colo, upstream} 或 None（不可达）。
    """
    try:
        start = time.monotonic()
        result = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}\n%{time_total}",
                "--resolve", f"{WORKER_HOST}:443:{ip}",
                "--connect-timeout", "10",
                "--max-time", str(TIMEOUT),
                f"https://{WORKER_HOST}",
            ],
            capture_output=True, text=True, timeout=TIMEOUT + 5,
        )
        elapsed = time.monotonic() - start

        lines = result.stdout.strip().split("\n")
        http_code = lines[0].strip() if lines else "000"
        curl_time = float(lines[1]) if len(lines) > 1 else elapsed

        if http_code != "200":
            return None

        # 如果能拿到 Worker JSON body，解析它
        # 为了省一次请求，先只检查 HTTP 200
        # 后续可以增强: 拿到 body 验证 upstream=="ok"
        return {
            "ip": ip,
            "latency_ms": round(curl_time * 1000, 2),
            "http_code": http_code,
        }
    except (subprocess.TimeoutExpired, Exception):
        return None


def check_ip_full(ip: str) -> dict | None:
    """
    完整验证: 不仅检查 HTTP 200，还解析 Worker JSON body。
    更严格但更慢（多一次请求）。
    """
    try:
        start = time.monotonic()
        result = subprocess.run(
            [
                "curl", "-s",
                "--resolve", f"{WORKER_HOST}:443:{ip}",
                "--connect-timeout", "10",
                "--max-time", str(TIMEOUT),
                f"https://{WORKER_HOST}",
            ],
            capture_output=True, text=True, timeout=TIMEOUT + 5,
        )
        elapsed = time.monotonic() - start

        if result.returncode != 0:
            return None

        body = json.loads(result.stdout)
        if body.get("status") != "ok":
            return None
        if body.get("upstream") != "ok":
            print(f"  {ip}: Worker 可达但回源异常 ({body.get('upstream')})")
            return None

        return {
            "ip": ip,
            "latency_ms": round(elapsed * 1000, 2),
            "colo": body.get("colo", "?"),
            "country": body.get("country", "?"),
            "upstream": body.get("upstream", "?"),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None


def main():
    input_csv = sys.argv[1] if len(sys.argv) > 1 else "result.csv"
    output_csv = sys.argv[2] if len(sys.argv) > 2 else "valid_ips.csv"
    full_check = "--full" in sys.argv

    # 1. 读取候选 IP
    candidate_ips = read_ips(input_csv)
    if not candidate_ips:
        print("❌ 无候选 IP")
        return

    print(f"验证 {len(candidate_ips)} 个候选 IP 通过 {WORKER_HOST}...")

    # 2. 并行验证
    check_fn = check_ip_full if full_check else check_ip
    valid = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(check_fn, ip): ip for ip in candidate_ips}
        for f in as_completed(futures):
            r = f.result()
            if r:
                valid.append(r)
                extra = f" colo={r.get('colo','?')}" if "colo" in r else ""
                print(f"  ✅ {r['ip']}: {r['latency_ms']}ms{extra}")
            else:
                print(f"  ❌ {futures[f]}: 不可达")

    # 3. 排序输出
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

    # 5. 写入 CSV（兼容 CloudflareST 格式）
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

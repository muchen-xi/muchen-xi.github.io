"""
境内测速脚本 — 从中国三网节点测 CF 候选 IP
用法: python3 scripts/china_speed_test.py [result.csv] [--output china_result.csv]

输入: CloudflareST 输出的 result.csv（境外测速结果）
输出: china_result.csv（同 CloudflareST 格式: IP地址,已发送,已接收,丢包率,平均延迟,下载速度）

测速源优先级（三级 fallback）:
  1. ITDog via Playwright — 浏览器自动化控制 itdog.cn/ping，中国三网节点实测
  2. Boce REST API      — api.boce.com/v3，需 BOCE_API_KEY 环境变量
  3. 预置亚太区 CF IP   — 硬编码延迟估算值，最终兜底

环境变量:
  BOCE_API_KEY  — Boce.com API key（可选，注册免费 200 次配额）

依赖:
  pip install playwright && playwright install --with-deps chromium
  GitHub Actions 需添加 playwright 安装步骤（见 workflow 示例）
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

# ─── 预置亚太区 CF IP（所有在线 API 不可用时的最终 fallback） ───
# 这些 IP 在亚太区 CF 边缘节点（HKG/NRT/SIN/ICN）延迟较低
# 延迟值来自历史实测平均值，仅供紧急降级使用
FALLBACK_IPS = [
    ("162.159.39.168", 68),
    ("172.64.52.95", 72),
    ("162.159.44.17", 75),
    ("162.159.38.15", 78),
    ("172.64.52.9", 80),
    ("162.159.39.67", 82),
    ("162.159.44.235", 85),
    ("108.162.198.190", 88),
    ("162.159.36.100", 90),
    ("172.64.53.50", 92),
]

CSV_HEADER = "IP 地址,已发送,已接收,丢包率,平均延迟,下载速度 (MB/s)"
TOP_N = 3                       # 输出前 N 个最优 IP
MAX_CANDIDATES = 10             # 单次最多测 10 个 IP（ITDog 逐 IP 耗时）
HTTP_TIMEOUT = 30               # HTTP 请求超时（秒）
ITDOG_PAGE_TIMEOUT = 60000      # Playwright 页面加载超时（ms）
ITDOG_RESULT_TIMEOUT = 45000    # Playwright 等待结果表超时（ms）
ITDOG_NAVIGATE_WAIT = 2000      # 导航后额外等待（ms），防反爬


# ═══════════════════════════════════════════════════════════════
#  Utility helpers
# ═══════════════════════════════════════════════════════════════

def http_get(url, timeout=HTTP_TIMEOUT):
    """HTTP GET, 返回解析后的 JSON dict。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url, data, timeout=HTTP_TIMEOUT):
    """HTTP POST JSON, 返回解析后的 JSON dict。"""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def estimate_speed(avg_ms: float) -> float:
    """
    基于延迟估算下载速度（MB/s）。

    使用简化的 TCP 吞吐量模型: speed = MSS / (RTT * sqrt(loss))
    在零丢包假设下简化为 speed ~ 1 / RTT。

    NOTE: 这是估算值，非真实下载测速。真实吞吐量受带宽、
    TCP 拥塞窗口、链路质量等多因素影响。CloudflareST 境外
    测速才会给出真实下载速度；此脚本专注于境内延迟评估。
    """
    if avg_ms <= 0:
        return 10.0
    speed = 200.0 / avg_ms
    return round(max(0.1, min(10.0, speed)), 2)


def speed_str(avg_ms: float, estimated: bool = False) -> str:
    """格式化为字符串，估算值加标记。"""
    s = estimate_speed(avg_ms)
    if estimated:
        return f"{s} (估)"
    return str(s)


# ═══════════════════════════════════════════════════════════════
#  Tier 1: ITDog via Playwright — 浏览器自动化
# ═══════════════════════════════════════════════════════════════

# ITDog ping 页面 CSS 选择器（按优先级排列，多个备用）
_ITDOG_SELECTORS = {
    "host_input": [
        "input#host",
        "input[name='host']",
        "input[placeholder*='域名']",
        "input[placeholder*='IP']",
        "input[placeholder*='host']",
        "input[type='text']:not([readonly])",
    ],
    "ping_button": [
        "button:has-text('Ping')",
        "button:has-text('测')",
        "button[type='submit']",
        "a:has-text('Ping')",
        "input[type='submit']",
    ],
    "result_table": [
        "div.result table",
        "div#result table",
        "table.table",
        "table.result-table",
        "table",
    ],
}


def _find(page, selectors, description="element"):
    """用多个备用选择器查找第一个可见元素。"""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    return None


def _parse_result_row(cells: list) -> dict | None:
    """
    解析 ITDog 结果表格的一行。
    典型列序: 节点 | 位置 | 运营商 | 发送 | 接收 | 丢包率 | 最小 | 平均 | 最大
    返回 {"sent": int, "recv": int, "avg": float} 或 None。
    """
    texts = [c.inner_text().strip() for c in cells if c.inner_text().strip()]

    # 收集所有可解析为数字的值（原始顺序）
    numeric_vals = []
    for t in texts:
        clean = t.replace("%", "").replace("ms", "").replace(" ", "").strip()
        try:
            numeric_vals.append(float(clean))
        except ValueError:
            numeric_vals.append(None)

    sent = recv = avg_latency = None

    # ── 策略 A: 按位置解析（如果列数 >= 9） ──
    if len(texts) >= 9:
        try:
            sent = int(float(texts[3].strip()))
            recv = int(float(texts[4].strip()))
            avg_latency = float(texts[7].replace("ms", "").strip())
            if 0 <= sent <= 10 and 0 <= recv <= sent and avg_latency > 0:
                return {"sent": sent, "recv": recv, "avg": avg_latency}
        except (ValueError, IndexError):
            pass

    # ── 策略 B: 启发式 — 在数值中找 sent(4)/recv(0-4)/avg(10-500ms) ──
    valid = [(i, v) for i, v in enumerate(numeric_vals) if v is not None]
    sent_candidates = [(i, int(v)) for i, v in valid if 3 <= v <= 10 and v == int(v)]
    latency_candidates = [(i, v) for i, v in valid if 5 <= v <= 600]

    if not sent_candidates or not latency_candidates:
        return None

    # sent = 第一个 small-int（通常是 4）
    sent_idx, sent = sent_candidates[0]
    # recv = 紧接 sent 之后的 small-int
    for idx, v in sent_candidates[1:]:
        if idx == sent_idx + 1 and v <= sent:
            recv = v
            break
    if recv is None:
        recv = sent  # 保守假设无丢包

    # avg_latency = 倒数第 2 或第 3 个 latency 值（min, avg, max 中的 avg）
    if len(latency_candidates) >= 2:
        avg_latency = latency_candidates[-2][1]
    elif latency_candidates:
        avg_latency = latency_candidates[-1][1]

    if avg_latency and avg_latency > 0:
        return {"sent": sent, "recv": recv, "avg": avg_latency}
    return None


def _itdog_ping_one(browser, ip: str) -> dict | None:
    """
    通过 Playwright 在 ITDog 页面上测试单个 IP 的延迟。
    每次创建独立的 browser context/page，规避 cookie/状态污染。
    """
    from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

    context = page = None
    try:
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # 导航到 ITDog ping 页面
        try:
            page.goto(
                "https://www.itdog.cn/ping",
                timeout=ITDOG_PAGE_TIMEOUT,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(ITDOG_NAVIGATE_WAIT)
        except (PlaywrightError, PlaywrightTimeout) as e:
            print(f"    ITDog page load error: {e}")
            return None

        # 填入目标 IP
        host_input = _find(page, _ITDOG_SELECTORS["host_input"], "host input")
        if not host_input:
            print(f"    ITDog: cannot find host input field (page structure changed?)")
            return None
        try:
            host_input.click()
            host_input.fill("")
            host_input.type(ip, delay=50)
        except (PlaywrightError, PlaywrightTimeout) as e:
            print(f"    ITDog: cannot fill host input: {e}")
            return None
        page.wait_for_timeout(500)

        # 点击 Ping 按钮
        ping_btn = _find(page, _ITDOG_SELECTORS["ping_button"], "ping button")
        if not ping_btn:
            print(f"    ITDog: cannot find ping button")
            return None
        try:
            ping_btn.click()
        except (PlaywrightError, PlaywrightTimeout) as e:
            print(f"    ITDog: cannot click ping button: {e}")
            return None

        # 等待结果表格出现（轮询）
        table = None
        deadline = time.time() + ITDOG_RESULT_TIMEOUT / 1000.0
        while time.time() < deadline:
            table = _find(page, _ITDOG_SELECTORS["result_table"], "result table")
            if table:
                break
            page.wait_for_timeout(1500)
        if not table:
            print(f"    ITDog: result table did not appear within {ITDOG_RESULT_TIMEOUT}ms")
            return None
        # 给结果一点渲染时间
        page.wait_for_timeout(2000)

        # 抽取所有数据行（跳过表头）
        rows = table.query_selector_all("tbody tr") or table.query_selector_all("tr")
        if not rows:
            print(f"    ITDog: no result rows found")
            return None

        total_sent = 0
        total_recv = 0
        total_ms = 0.0
        node_count = 0

        for row in rows:
            cells = row.query_selector_all("td")
            if not cells or len(cells) < 4:
                continue
            parsed = _parse_result_row(cells)
            if parsed:
                total_sent += parsed["sent"]
                total_recv += parsed["recv"]
                total_ms += parsed["avg"]
                node_count += 1

        if node_count == 0:
            print(f"    ITDog: could not parse any node data for {ip}")
            return None

        avg_ms = round(total_ms / node_count, 2)
        loss_pct = round((total_sent - total_recv) / max(total_sent, 1) * 100, 2)

        return {
            "ip": ip,
            "sent": total_sent,
            "received": total_recv,
            "loss": loss_pct,
            "avg_ms": avg_ms,
        }

    except Exception as e:
        print(f"    ITDog ping {ip} unexpected error: {type(e).__name__}: {e}")
        return None
    finally:
        for obj in (page, context):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass


def itdog_probe() -> bool:
    """
    快速探测：验证 ITDog 页面能否正常加载、关键 UI 元素是否存在。
    成功返回 True，失败返回 False（不抛异常）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright 未安装 — 跳过 ITDog")
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(
                    "https://www.itdog.cn/ping",
                    timeout=ITDOG_PAGE_TIMEOUT,
                    wait_until="domcontentloaded",
                )
                page.wait_for_timeout(2000)
                has_input = _find(page, _ITDOG_SELECTORS["host_input"]) is not None
                has_btn = _find(page, _ITDOG_SELECTORS["ping_button"]) is not None
                page.close()
                if has_input and has_btn:
                    print("  ITDog probe: 页面正常，UI 元素 OK")
                    return True
                else:
                    print(f"  ITDog probe: 页面加载但 UI 元素缺失 "
                          f"(input={'OK' if has_input else 'MISS'}, "
                          f"btn={'OK' if has_btn else 'MISS'})")
                    return False
            finally:
                browser.close()
    except Exception as e:
        print(f"  ITDog probe 失败: {type(e).__name__}: {e}")
        return False


def itdog_ping_batch(ips: list[str]) -> list[dict]:
    """
    批量通过 ITDog Playwright 测多个 IP。
    单个浏览器实例内逐个测试，避免并发触发反爬。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright 未安装 — 跳过 ITDog")
        return []

    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for idx, ip in enumerate(ips, 1):
                    print(f"  [{idx}/{len(ips)}] ITDog ping {ip} ...")
                    r = _itdog_ping_one(browser, ip)
                    if r:
                        results.append(r)
                        print(f"    -> 延迟 {r['avg_ms']}ms  丢包 {r['loss']}%  "
                              f"({r['sent']}/{r['received']})")
                    else:
                        print(f"    -> 失败")
            finally:
                browser.close()
    except Exception as e:
        print(f"  ITDog batch 致命错误: {type(e).__name__}: {e}")

    return results


# ═══════════════════════════════════════════════════════════════
#  Tier 2: Boce REST API
# ═══════════════════════════════════════════════════════════════

def boce_ping(ip: str) -> dict | None:
    """
    Boce.com ping 单个 IP。
    需要 BOCE_API_KEY 环境变量（boce.com 免费注册，200 次配额）。
    API: GET https://api.boce.com/v3/task/create/curl?key=KEY&host=IP
    """
    boce_key = os.environ.get("BOCE_API_KEY", "")
    if not boce_key:
        return None
    try:
        resp = http_get(
            f"https://api.boce.com/v3/task/create/curl?key={boce_key}&host={ip}",
            timeout=HTTP_TIMEOUT,
        )
        if resp.get("error_code") != 0:
            print(f"    Boce API error: {resp.get('msg', resp.get('error', ''))}")
            return None
        data = resp.get("data", {})
        ping_data = data.get("ping", {})
        sent = int(ping_data.get("sent", 4))
        recv = int(ping_data.get("received", 4))
        loss_val = ping_data.get("loss", 0)
        loss_pct = float(loss_val) if isinstance(loss_val, (int, float)) else float(ping_data.get("loss_percent", 0))
        avg = float(ping_data.get("avg", 0))
        if avg <= 0:
            return None
        return {
            "ip": ip,
            "sent": sent,
            "received": recv,
            "loss": loss_pct,
            "avg_ms": avg,
        }
    except Exception as e:
        print(f"    Boce ping {ip} 失败: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  Tier 3: 预置亚太区 CF IP（最终 fallback）
# ═══════════════════════════════════════════════════════════════

def fallback_results(ips: list[str]) -> list[dict]:
    """
    使用预置亚太区 CF 边缘 IP 及历史延迟估算值。
    当所有在线测速 API 都不可用时使用。
    """
    results = []
    ip_set = set(ips)
    # 优先匹配输入中的 IP
    for ip, base_ms in FALLBACK_IPS:
        if ip in ip_set:
            results.append({
                "ip": ip,
                "sent": 4,
                "received": 4,
                "loss": 0.00,
                "avg_ms": base_ms,
            })
    # 如果输入 IP 都不在预置列表中，直接用前 TOP_N 个预置 IP
    if not results:
        for ip, base_ms in FALLBACK_IPS[:TOP_N]:
            results.append({
                "ip": ip,
                "sent": 4,
                "received": 4,
                "loss": 0.00,
                "avg_ms": base_ms,
            })
    return results


# ═══════════════════════════════════════════════════════════════
#  Candidate IP reader
# ═══════════════════════════════════════════════════════════════

def read_candidates(csv_path: str) -> list[str]:
    """从 CloudflareST 格式 CSV 读取候选 IP 列表。"""
    ips = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ip = (row.get("IP地址") or row.get("IP 地址")
                      or row.get("IP") or row.get("ip", "").strip())
                if ip:
                    ips.append(ip)
    except FileNotFoundError:
        print(f"  {csv_path} 不存在")
    return ips


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "result.csv"
    output = sys.argv[2] if len(sys.argv) > 2 else "china_result.csv"
    if output.startswith("--"):
        # Handle case: script.py --output china_result.csv (no result.csv)
        output = sys.argv[1] if len(sys.argv) > 1 else "china_result.csv"
        csv_path = "result.csv"

    print(f"=== 境内测速 === @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"输入: {csv_path}  输出: {output}")

    # ── 1. 读取候选 IP ──
    candidate_ips = read_candidates(csv_path)
    if candidate_ips:
        candidates = candidate_ips[:MAX_CANDIDATES]
        print(f"\n候选 IP ({len(candidates)} 个，来自 {csv_path}):")
        for ip in candidates:
            print(f"  {ip}")
    else:
        candidates = [ip for ip, _ in FALLBACK_IPS[:MAX_CANDIDATES]]
        print(f"\n候选 IP ({len(candidates)} 个，来自 fallback 列表):")
        for ip in candidates:
            print(f"  {ip}")

    results = []
    source_label = ""  # 记录最终使用的测速源

    # ── Tier 1: ITDog via Playwright ──
    print("\n── Tier 1: ITDog (Playwright 浏览器自动化) ──")
    if itdog_probe():
        results = itdog_ping_batch(candidates)
        if len(results) >= TOP_N:
            results.sort(key=lambda x: (x["loss"], x["avg_ms"]))
            source_label = "ITDog (Playwright)"
            print(f"  ITDog 完成: {len(results)} 个成功结果")
        else:
            print(f"  ITDog 结果不足 ({len(results)}/{TOP_N}) — 降至 Tier 2")
            results = []
    else:
        print("  ITDog 不可用 — 降至 Tier 2")

    # ── Tier 2: Boce REST API ──
    if len(results) < TOP_N:
        print("\n── Tier 2: Boce REST API ──")
        boce_key = os.environ.get("BOCE_API_KEY", "")
        if not boce_key:
            print("  BOCE_API_KEY 未设置 — 跳过 Boce")
        else:
            boce_results = []
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(boce_ping, ip): ip for ip in candidates}
                try:
                    for f in as_completed(futures, timeout=HTTP_TIMEOUT):
                        try:
                            r = f.result()
                            if r:
                                boce_results.append(r)
                                print(f"  {r['ip']}: {r['avg_ms']}ms, {r['loss']}% loss")
                        except Exception:
                            pass
                except FuturesTimeoutError:
                    print("  Boce 批量请求超时（部分结果可能丢失）")
            if len(boce_results) >= TOP_N:
                results = boce_results
                results.sort(key=lambda x: (x["loss"], x["avg_ms"]))
                source_label = "Boce REST API"
                print(f"  Boce 完成: {len(results)} 个结果")
            else:
                print(f"  Boce 结果不足 ({len(boce_results)}/{TOP_N}) — 降至 Tier 3")

    # ── Tier 3: 预置亚太区 CF IP ──
    if len(results) < TOP_N:
        print("\n── Tier 3: 预置亚太区 CF IP（最终 fallback）──")
        results = fallback_results(candidates)
        source_label = "预置亚太 IP (估)"
        print(f"  Fallback: {len(results)} 个 IP（延迟为历史估算值）")

    # ── 输出 ──
    best = results[:TOP_N]
    print(f"\n=== 境内优选 IP (Top {len(best)}) [{source_label}] ===")

    with open(output, "w", newline="", encoding="utf-8") as f:
        f.write(CSV_HEADER + "\n")
        for r in best:
            is_estimated = ("估" in source_label)
            spd = speed_str(r["avg_ms"], estimated=is_estimated)
            f.write(
                f"{r['ip']},{r['sent']},{r['received']},"
                f"{r['loss']:.2f},{r['avg_ms']},{spd}\n"
            )
            tag = " (估)" if is_estimated else ""
            print(f"  {r['ip']}: {r['avg_ms']}ms, "
                  f"丢包 {r['loss']}%, 下载 {spd} MB/s{tag}")

    print(f"\n结果已写入 {output}")

    # 打印文件内容供 workflow log 查看
    print(f"\n── {output} ──")
    with open(output, newline="", encoding="utf-8") as f:
        print(f.read())

    if "估" in source_label:
        print("\n[*] 注意: 下载速度为基于延迟的估算值，非真实下载测速结果。")


if __name__ == "__main__":
    main()

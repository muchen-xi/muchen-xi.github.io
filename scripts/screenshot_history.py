"""
每日主站截图 + 变动检测
用法: python3 scripts/screenshot_history.py

1. Playwright 截取 www.chenxiuniverse.top 全页
2. 保存 screenshots/YYYY-MM-DD.webp (缩略图, ~30KB)
3. 与前一天对比，检测视觉变动
4. 更新 screenshots/manifest.json
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

tz_cst = timezone(timedelta(hours=8))
TODAY = datetime.now(tz_cst).strftime("%Y-%m-%d")
SCREENSHOTS_DIR = Path("screenshots")
MANIFEST_PATH = SCREENSHOTS_DIR / "manifest.json"
URL = "https://www.chenxiuniverse.top"
VIEWPORT = {"width": 1280, "height": 720}
THUMB_WIDTH = 320  # 缩略图宽度


def load_manifest():
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_manifest(data):
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def compare_images(path_a: Path, path_b: Path) -> float:
    """比较两张图片的差异度 (0.0 = 完全相同, 1.0 = 完全不同)"""
    try:
        from PIL import Image
        import statistics

        img_a = Image.open(path_a).convert("L").resize((128, 72))
        img_b = Image.open(path_b).convert("L").resize((128, 72))

        pixels_a = list(img_a.getdata())
        pixels_b = list(img_b.getdata())

        diffs = [abs(a - b) / 255.0 for a, b in zip(pixels_a, pixels_b)]
        return statistics.mean(diffs)
    except Exception:
        return -1  # 无法比较


def main():
    print(f"[screenshot] {TODAY}")

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()

    # 检查今天是否已截图
    today_path = SCREENSHOTS_DIR / f"{TODAY}.webp"
    saved_path = today_path  # 实际落盘文件（可能是 .png 兜底）
    if today_path.exists():
        print(f"  [skip] {TODAY}.webp exists")
    elif (SCREENSHOTS_DIR / f"{TODAY}.png").exists():
        # 上次 Pillow 兜底留下的 PNG
        saved_path = SCREENSHOTS_DIR / f"{TODAY}.png"
        print(f"  [skip] {TODAY}.png exists")
    else:
        # Playwright 截图
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport=VIEWPORT)
                page.goto(URL, wait_until="networkidle", timeout=30000)

                # 截图 (Playwright 只支持 png/jpeg)
                tmp = str(today_path).replace('.webp', '.png')
                page.screenshot(path=tmp, type="png")
                browser.close()

            # 转为 WebP 缩略图 (Pillow)
            saved_path = today_path  # 默认 .webp
            try:
                from PIL import Image
                img = Image.open(tmp)
                ratio = THUMB_WIDTH / img.width
                thumb_h = int(img.height * ratio)
                img = img.resize((THUMB_WIDTH, thumb_h), Image.LANCZOS)
                img.save(today_path, "WEBP", quality=50)
                os.remove(tmp)  # 删除原始 PNG
            except Exception:
                import shutil
                # Pillow 不可用：保留 PNG 原名，避免 .webp 名存 PNG 字节导致浏览器拒绝解码
                saved_path = SCREENSHOTS_DIR / f"{TODAY}.png"
                shutil.move(tmp, saved_path)

            size_kb = saved_path.stat().st_size / 1024
            print(f"  [ok] {saved_path.name} ({size_kb:.0f}KB)")

        except Exception as e:
            print(f"  [FAIL] screenshot: {e}", file=sys.stderr)
            sys.exit(1)

    # 与前一天（非今天）对比 — 避免同一天重复运行与自身比较、禁用变动检测
    prev_entry = None
    for e in reversed(manifest):
        if e.get("date") != TODAY:
            prev_entry = e
            break

    existing_idx = next((i for i, e in enumerate(manifest) if e.get("date") == TODAY), None)
    change_detected = False
    diff_pct = 0.0

    if prev_entry:
        prev_path = SCREENSHOTS_DIR / prev_entry["file"]
        if prev_path.exists() and saved_path.exists():
            diff = compare_images(prev_path, saved_path)
            if diff < 0:
                print(f"  [warn] PIL not available, using file size comparison")
                # 简单文件大小比较
                size_ratio = abs(saved_path.stat().st_size - prev_path.stat().st_size) / max(prev_path.stat().st_size, 1)
                diff_pct = size_ratio * 100
                change_detected = size_ratio > 0.05  # 5% 文件大小变化阈值
            else:
                diff_pct = diff * 100
                change_detected = diff > 0.03  # 3% 像素差异阈值

            if change_detected:
                print(f"  [CHANGED] diff={diff_pct:.1f}%")
            else:
                print(f"  [same] diff={diff_pct:.1f}%")
        else:
            print(f"  [warn] previous screenshot not found, skipping comparison")
    else:
        print(f"  [info] first screenshot, no baseline")

    # 更新 manifest：今天已存在则原位更新，不重复追加
    today_info = {
        "date": TODAY,
        "file": saved_path.name,
        "changed": change_detected,
        "diff_pct": round(diff_pct, 2),
    }
    if existing_idx is not None:
        manifest[existing_idx] = today_info
        print(f"  [update] 覆盖 manifest 中 {TODAY} 的记录")
    else:
        manifest.append(today_info)
    save_manifest(manifest)

    # 输出 JSON 供 workflow 使用
    print(f"\nMANIFEST_JSON={json.dumps(today_info)}")

    # 变动标记写入环境文件
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"SCREENSHOT_CHANGED={'true' if change_detected else 'false'}\n")
            f.write(f"SCREENSHOT_DIFF={diff_pct:.1f}\n")
            f.write(f"SCREENSHOT_DATE={TODAY}\n")


if __name__ == "__main__":
    main()

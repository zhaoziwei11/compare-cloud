# -*- coding: utf-8 -*-
"""
云端版抓取脚本（GitHub Actions / Linux 运行）。

与本地版 fetch_compare_data.py 的区别：
  - 本地版用本机 Edge 持久化 profile 的登录态（browser_data_dir）
  - 云端版改用环境变量 CHENGYUN_COOKIES（JSON 数组，由 Cookie-Editor 导出）注入登录态
  - 浏览器用 Playwright 自带的 chromium（headless），不依赖 msedge
  - 结果 CSV 写到 data/ 目录，供 workflow 推到 data 分支、前端从 raw.githubusercontent.com 读取

复用 fetch_compare_data.py 的 fetch_waybill / fetch_billing（页面交互逻辑完全一致）。

用法:
    CHENGYUN_COOKIES='[{"name":"...","value":"...","domain":"...","path":"/"}]' \
        python fetch_cloud.py --start 2026-08-01 --end 2026-08-02 --time-field "磅单审核通过时间"
    # 不传 --start/--end 时，自动算「上一个工作日..昨天」（仅工作日跑）
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[ERROR] Playwright 未安装, 请先运行: pip install playwright && playwright install chromium")
    sys.exit(1)

from fetch_compare_data import fetch_waybill, fetch_billing, OUTPUT_DIR
from holidays import is_workday, compute_auto_range

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def write_meta(ok, start, end, time_field, error=None, skipped=False):
    meta = {
        "ok": ok,
        "skipped": skipped,
        "start": start,
        "end": end,
        "time_field": time_field,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
    }
    (DATA_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD（不传则自动算）")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（不传则自动算）")
    parser.add_argument("--time-field", default="磅单审核通过时间", help="运单列表时间字段")
    args = parser.parse_args()

    today = datetime.now().date()

    # 自动模式：仅工作日跑
    if args.start and args.end:
        start, end = args.start, args.end
    else:
        if not is_workday(today):
            reason = "周末" if today.weekday() >= 5 else "法定节假日"
            print(f"[skip] 今日 {today.isoformat()} 为{reason}, 跳过自动获取")
            write_meta(False, None, None, args.time_field, skipped=True)
            return
        sd, ed = compute_auto_range(today)
        start, end = sd.strftime("%Y-%m-%d"), ed.strftime("%Y-%m-%d")

    cookies_raw = os.environ.get("CHENGYUN_COOKIES")
    if not cookies_raw:
        print("[ERROR] 缺少 CHENGYUN_COOKIES 环境变量（请在仓库 Secrets 中配置）")
        write_meta(False, start, end, args.time_field, error="missing CHENGYUN_COOKIES")
        sys.exit(1)
    try:
        cookies = json.loads(cookies_raw)
    except Exception as e:
        print(f"[ERROR] CHENGYUN_COOKIES 不是合法 JSON: {e}")
        write_meta(False, start, end, args.time_field, error="bad cookies json")
        sys.exit(1)

    print(f"[run] 抓取范围 {start} ~ {end}, 时间字段={args.time_field}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(locale="zh-CN", viewport={"width": 1600, "height": 1000})
        try:
            context.add_cookies(cookies)
        except Exception as e:
            print(f"[ERROR] 注入 cookie 失败: {e}")
            write_meta(False, start, end, args.time_field, error=f"cookie inject: {e}")
            browser.close()
            sys.exit(1)

        page = context.new_page()
        # 自动接受导出时的二次确认弹窗, 防止 download 事件不触发
        page.on("dialog", lambda d: (print(f"  [dialog] accept: {(d.message or '')[:60]}"), d.accept()))

        try:
            wb = fetch_waybill(page, args.time_field, start, end)
            bi = fetch_billing(page, start, end)
            # 复制到 data/ 供 workflow 推送
            shutil.copy(wb, DATA_DIR / "waybill_compare.csv")
            shutil.copy(bi, DATA_DIR / "billing_compare.csv")
            write_meta(True, start, end, args.time_field)
            print(f"[OK] 完成 -> {DATA_DIR}/waybill_compare.csv, billing_compare.csv")
        except Exception as e:
            print(f"[ERROR] 抓取失败: {e}")
            write_meta(False, start, end, args.time_field, error=str(e))
            browser.close()
            sys.exit(2)
        finally:
            browser.close()


if __name__ == "__main__":
    main()

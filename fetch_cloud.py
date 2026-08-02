# -*- coding: utf-8 -*-
"""
云端版抓取脚本（GitHub Actions / Linux 运行）—— 纯 Token 调接口版。

与本地原版 fetch_compare_data.py 的本质区别:
  本地版依赖本机 Edge 持久化 profile 里的登录态(localStorage 的 Token 由页面 JS 注入请求头);
  云端版直接用 CHENGYUN_TOKEN 环境变量, 在请求头里带 Token 调 gateway.91msl.com 接口,
  不再需要 Playwright / 浏览器 / cookie, 更轻更快更稳。

请求体参数来自仓库内的 params_waybill.json / params_billing.json 模版(含 __START__/__END__ 日期占位),
由 GitHub Actions 每日自动填入「上一个工作日..昨天」范围。模版的真实字段名由页面 Network 抓到的
请求体校准(见 README)。

用法:
    CHENGYUN_TOKEN=xxxx python fetch_cloud.py --start 2026-08-01 --end 2026-08-02
    # 不传 --start/--end 时, 自动算「上一个工作日..昨天」(仅工作日跑)
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("[ERROR] requests 未安装, 请先运行: pip install requests")
    sys.exit(1)

from holidays import is_workday, compute_auto_range

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

WAYBILL_BASE = "https://gateway.91msl.com/clx-performance/pc/carrier/orderChild/"
BILLING_BASE = "https://gateway.91msl.com/clx-performance/pc/carrier/settlementDriver/"


def build_headers():
    token = os.environ.get("CHENGYUN_TOKEN")
    if not token:
        print("[ERROR] 缺少 CHENGYUN_TOKEN 环境变量（请在仓库 Secrets 中配置）")
        sys.exit(1)
    return {
        "Token": token,
        "Origin": "https://chengyun.91msl.com",
        "Referer": "https://chengyun.91msl.com/order-center/waybill-manage/waybill-list",
        "Client-Type": "pc",
        "Product-Code": "carrier-platform-npc",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36",
    }


def load_template(name):
    p = Path(__file__).parent / name
    if not p.exists():
        print(f"[ERROR] 缺少模版文件 {name}")
        sys.exit(1)
    return json.loads(p.read_text(encoding="utf-8"))


def fill_dates(tmpl, start, end):
    s = json.dumps(tmpl, ensure_ascii=False)
    s = s.replace("__START__", start).replace("__END__", end)
    return json.loads(s)


def extract_records(j):
    if not isinstance(j, dict) or j.get("code") != 0:
        return None
    d = j.get("data")
    if d is None:
        return None
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("records", "list", "rows", "items"):
            if isinstance(d.get(k), list):
                return d[k]
    return None


def fetch_all(base_url, endpoint, body, headers, max_pages=300):
    """分页拉取全部记录; pageSize 默认 99999, 超出再翻页。"""
    all_recs = []
    page = 1
    while page <= max_pages:
        b = dict(body)
        b["page"] = page
        b["pageSize"] = b.get("pageSize", 200)
        try:
            r = requests.post(base_url + endpoint, json=b, headers=headers, timeout=120)
            j = r.json()
        except Exception as e:
            print(f"[ERROR] 请求 {endpoint} 失败: {e}")
            return None
        recs = extract_records(j)
        if recs is None:
            if page == 1:
                print(f"[ERROR] endpoint={endpoint} 返回异常: code={j.get('code')} msg={j.get('msg')} body={r.text[:200]}")
                return None
            break
        all_recs.extend(recs)
        total = (j.get("data") or {}).get("total", len(recs))
        if len(all_recs) >= total or len(recs) == 0:
            break
        page += 1
    return all_recs


def records_to_csv(records, path):
    if not records:
        print(f"[WARN] {path.name} 无数据, 跳过")
        return False
    cols = []
    for r in records:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in records:
            if not isinstance(r, dict):
                w.writerow([])
                continue
            w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in cols])
    print(f"[OK] {path.name}: {len(records)} 行, {len(cols)} 列")
    return True


def write_meta(ok, start, end, error=None, skipped=False):
    meta = {
        "ok": ok,
        "skipped": skipped,
        "start": start,
        "end": end,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
    }
    (DATA_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD（不传则自动算）")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（不传则自动算）")
    ap.add_argument("--time-field", default="磅单审核通过时间", help="运单列表时间字段(仅记录用)")
    args = ap.parse_args()

    today = datetime.now().date()
    if args.start and args.end:
        start, end = args.start, args.end
    else:
        if not is_workday(today):
            reason = "周末" if today.weekday() >= 5 else "法定节假日"
            print(f"[skip] 今日 {today.isoformat()} 为{reason}, 跳过自动获取")
            write_meta(False, None, None, skipped=True)
            return
        sd, ed = compute_auto_range(today)
        start, end = sd.strftime("%Y-%m-%d"), ed.strftime("%Y-%m-%d")

    print(f"[run] 抓取范围 {start} ~ {end}, 时间字段={args.time_field}")
    headers = build_headers()

    # 来源一: 运单列表
    try:
        wb_tmpl = load_template("params_waybill.json")
        wb_body = fill_dates(wb_tmpl["body"], start, end)
        wb_recs = fetch_all(WAYBILL_BASE, wb_tmpl["endpoint"], wb_body, headers)
        if wb_recs:
            records_to_csv(wb_recs, DATA_DIR / "waybill_compare.csv")
        else:
            write_meta(False, start, end, error="waybill 无数据或接口异常")
            sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 运单获取失败: {e}")
        write_meta(False, start, end, error=str(e))
        sys.exit(2)

    # 来源二: 货主运单计费
    try:
        bi_tmpl = load_template("params_billing.json")
        bi_body = fill_dates(bi_tmpl["body"], start, end)
        bi_recs = fetch_all(BILLING_BASE, bi_tmpl["endpoint"], bi_body, headers)
        if bi_recs:
            records_to_csv(bi_recs, DATA_DIR / "billing_compare.csv")
        else:
            print("[WARN] 计费无数据, 仅运单已更新")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[WARN] 计费获取失败(不影响运单): {e}")

    write_meta(True, start, end)
    print(f"[OK] 完成 -> {DATA_DIR}")


if __name__ == "__main__":
    main()

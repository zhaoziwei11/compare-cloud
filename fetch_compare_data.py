# -*- coding: utf-8 -*-
"""
承运云表格对比 - 数据自动获取
两个数据源:
  来源一: 订单中心 -> 运单管理 -> 运单列表  (时间字段可切换: 磅单审核通过时间/接单时间/完成时间/...)
  来源二: 财务中心 -> 计费管理 -> 货主运单计费  (固定创建时间, datetimerange)

复用 withdraw_report.py 的范式: launch_persistent_context + channel=msedge,
el-form-item__label 定位, Vue handleOptionSelect, daterange input fill, expect_download 抓 xlsx。

用法:
    python fetch_compare_data.py --start 2026-06-28 --end 2026-07-28 --time-field "接单时间"
    python fetch_compare_data.py --start 2026-06-28 --end 2026-07-28 --time-field "磅单审核通过时间" --json-status
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("[ERROR] Playwright 未安装, 请先运行: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from openpyxl import load_workbook
except ImportError:
    print("[ERROR] openpyxl 未安装, 请先运行: pip install openpyxl")
    sys.exit(1)

# ============ 配置(同 withdraw_report.py 复用) ============
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

BASE_URL = CONFIG["base_url"]
BROWSER_DATA_DIR = CONFIG["browser_data_dir"]
HEADLESS = CONFIG.get("headless", True)
TIMEOUT = CONFIG.get("timeout_ms", 30000)

# 输出目录(供后端 /api/data 读取, 也供前端 fetch)
OUTPUT_DIR = SCRIPT_DIR / "compare_data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 状态文件(供后端轮询进度)
STATUS_FILE = OUTPUT_DIR / "fetch_status.json"

# 页面 URL
URL_WAYBILL = "https://chengyun.91msl.com/order-center/waybill-manage/waybill-list"
URL_BILLING = "https://chengyun.91msl.com/financial-center/billing-manage/owner-billing"

# 时间字段选项(从前端下拉传入, 这里仅作校验/默认)
DEFAULT_TIME_FIELDS = [
    "预约时间", "提前出发时间", "接单时间", "磅单审核通过时间",
    "前往货源地时间", "到达货源地时间", "装车成功时间", "前往目的地时间",
    "到达目的地时间", "货主确认车辆时间", "系统自动确认收货时间",
    "收货待确认时间", "确认收货时间", "待结算时间", "完成时间",
]

# ============ 状态写入 ============
def write_status(stage, message, progress=None, done=False, error=None):
    """写入当前进度到 STATUS_FILE, 供后端轮询"""
    payload = {
        "stage": stage,
        "message": message,
        "progress": progress,  # 0-100
        "done": done,
        "error": error,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ============ 通用: 设置 daterange / datetimerange ============
def set_date_range(page, label_text, start_str, end_str, datetime_mode=False):
    """
    通用日期范围设置: 通过 label 找到 el-form-item, 取 daterange/datetimerange 两个 input, fill + Tab。
    datetime_mode=True 时, 格式 "YYYY-MM-DD HH:MM:SS", 否则 "YYYY-MM-DD"。
    """
    cls = "el-date-editor--datetimerange" if datetime_mode else "el-date-editor--daterange"
    print(f"  -> 设置[{label_text}]: {start_str} ~ {end_str} ({'datetime' if datetime_mode else 'date'})")

    # 优先通过 label 精确定位
    inputs = None
    try:
        label = page.locator(f".el-form-item__label:text-is('{label_text}')")
        label.wait_for(timeout=8000)
        form_item = label.locator("xpath=..")
        inputs = form_item.locator(f".el-form-item__content .{cls} input").all()
        if len(inputs) < 2:
            raise RuntimeError(f"{label_text} 容器下未找到两个 input")
    except Exception as e:
        # 兜底: 整页第一个该类型 daterange
        print(f"  [WARN] label 定位失败({e}), 改用兜底选择器")
        inputs = page.locator(f".{cls} input").all()[:2]
        if len(inputs) < 2:
            raise RuntimeError(f"无法定位 {label_text} 输入框")

    inputs[0].click(timeout=5000)
    inputs[0].fill(start_str)
    page.wait_for_timeout(300)
    inputs[1].click(timeout=5000)
    inputs[1].fill(end_str)
    inputs[1].press("Tab")
    page.wait_for_timeout(600)


# ============ 通用: 设置 el-select 下拉 ============
def set_select_by_label(page, label_text, option_text):
    """
    设置 el-select 下拉值: 优先用 Vue handleOptionSelect, 备选 click dropdown item。
    label_text: 形如"时间筛选" "提现状态"
    option_text: 要选的选项文本, 如"接单时间" "待提现"
    """
    print(f"  -> 设置下拉[{label_text}] = {option_text}")

    js = f"""
    (option_text) => {{
        // 通过 label 文本找到 el-form-item
        const labels = Array.from(document.querySelectorAll('.el-form-item__label'));
        const labelEl = labels.find(el => el.textContent && el.textContent.trim() === '{label_text}');
        if (!labelEl) return {{error: 'label not found: {label_text}'}};

        const formItem = labelEl.closest('.el-form-item');
        if (!formItem) return {{error: 'form-item not found'}};
        const selectWrap = formItem.querySelector('.el-select');
        if (!selectWrap) return {{error: 'select not found in form-item'}};

        // 优先 Vue 直接设置
        const vue = selectWrap.__vue__ || selectWrap.__VUE__;
        if (vue && vue.handleOptionSelect) {{
            const options = vue.options || [];
            const option = options.find(o => (o.currentLabel || o.label || '') === option_text);
            if (option) {{
                vue.handleOptionSelect(option);
                return {{ok: true, via: 'vue', selected: option_text}};
            }}
        }}
        // 备选: 点击 input, 等下拉弹出, 点击对应 item
        const input = selectWrap.querySelector('.el-input__inner') || selectWrap.querySelector('input');
        if (!input) return {{error: 'input not found'}};
        input.click();
        let optionEl = null;
        for (let i = 0; i < 30; i++) {{
            const dropdowns = document.querySelectorAll('.el-select-dropdown');
            for (const d of dropdowns) {{
                const opts = d.querySelectorAll('.el-select-dropdown__item');
                optionEl = Array.from(opts).find(o => o.textContent.trim() === option_text);
                if (optionEl) break;
            }}
            if (optionEl) break;
            const start = Date.now();
            while (Date.now() - start < 100) {{}}
        }}
        if (!optionEl) return {{error: 'option not found after click'}};
        optionEl.click();
        return {{ok: true, via: 'click', selected: option_text}};
    }}
    """
    res = page.evaluate(js, option_text)
    if not res or not res.get("ok"):
        raise RuntimeError(f"下拉[{label_text}] = {option_text} 失败: {res}")
    page.wait_for_timeout(600)


# ============ 通用: 点击"查询"按钮 ============
def click_query(page):
    print("  -> 点击查询")
    page.locator("button:has-text('查询'), .el-button--primary:has-text('查询')").first.click(timeout=10000)
    page.wait_for_timeout(2500)


# ============ 失败诊断收集器 ============
EXPORT_DIAG = {"console": [], "responses": [], "requests": []}


def _on_response(r):
    """收集导出/错误类响应的状态码与响应体片段, 供失败时排查。"""
    try:
        url = r.url
        status = r.status
        if status >= 400 or "export" in url.lower() or "download" in url.lower():
            body = ""
            try:
                b = r.body()
                body = b.decode("utf-8", "ignore") if b else ""
            except Exception:
                body = "(body unavailable)"
            EXPORT_DIAG["responses"].append(
                f"[{status}] {r.request.method} {url}\n    body: {body[:400]}"
            )
    except Exception:
        pass


def _on_request(req):
    """收集承运云/网关域上的所有 POST 请求, 用于对比列表查询 vs 导出的参数差异。"""
    try:
        url = req.url
        method = req.method
        if method != "POST":
            return
        # 只关注承运云和它的网关域上的 API 请求
        if not ("gateway.91msl.com" in url or "chengyun.91msl.com" in url or "admin.91msl.com" in url):
            return
        post = req.post_data or ""
        snippet = post[:600] + ("...(截断)" if len(post) > 600 else "")
        EXPORT_DIAG["requests"].append(
            f"{method} {url}\n    body: {snippet}"
        )
    except Exception:
        pass


# 已知导出 endpoint (从 diag 中确认); 后续如变更只改这里
_EXPORT_URL = "https://gateway.91msl.com/clx-performance/pc/carrier/orderChild/exportCarrierOrderChildList"
_BILLING_EXPORT_URL = "https://gateway.91msl.com/clx-performance/pc/carrier/billing/exportOwnerOrderBillingList"  # 备用, 实际由 fetch_billing 内点击触发


def _try_direct_export(page, key, custom_url=None):
    """
    兜底: 浏览器内 fetch 多种 endpoint/参数, 记录全部响应到 EXPORT_DIAG。
    返回 (xlsx_path, 说明) 或 ("json_rows", rows_list, full_json) 或 None。
    """
    # 复用 diag 里记录的最近一次 POST body(更稳, 直接拷贝已验证有效的请求)
    last_body = ""
    for req_str in reversed(EXPORT_DIAG.get("requests", [])):
        if "export" in req_str:
            idx = req_str.find("body:")
            if idx >= 0:
                last_body = req_str[idx + 5:].strip()
                break
    base = {}
    if last_body and last_body.startswith("{"):
        try:
            base = json.loads(last_body)
        except Exception:
            base = {}
    if not base:
        base = {
            "orderNo": "", "orderSource": " ", "childNo": "", "orderGoodsNo": "",
            "truckNo": "", "driverName": "", "driverMobile": "",
            "sendAddress": "", "receiveAddress": "",
            "statusList": [], "timeType": 2,
            "ownerUserNos": [], "takeOrderWay": " ",
            "driverStatus": [], "electronicCodeChildType": [],
            "templateId": 69,
        }

    # 在浏览器内 fetch, 自动带 cookie/Origin/Referer, 不被 CORS 拦截
    common_headers = {
        "Referer": page.url,
        "Origin": "https://chengyun.91msl.com",
        "Accept": "application/json, text/plain, */*",
    }

    # 多种 endpoint 兜底
    endpoints = [
        (_EXPORT_URL, "export"),
        ("https://gateway.91msl.com/clx-performance/pc/carrier/orderChild/listCarrierOrderChildList", "list"),
        ("https://gateway.91msl.com/clx-performance/pc/carrier/orderChild/queryCarrierOrderChildList", "query"),
        ("https://gateway.91msl.com/clx-performance/pc/carrier/orderChild/pageCarrierOrderChildList", "page"),
    ]
    if custom_url:
        endpoints.insert(0, (custom_url, "custom"))

    all_records = []  # 用于累积分页数据
    last_err = None
    for url, kind in endpoints:
        # 1) 用 base + pageSize=99999 试一次
        params1 = {**base, "pageNum": 1, "pageSize": 99999}
        # 2) 用 base + pageSize=-1 试一次
        params2 = {**base, "pageNum": 1, "pageSize": -1}
        # 3) 用 base 原始参数(无 pageNum/pageSize) 试一次
        params3 = {k: v for k, v in base.items() if k not in ("pageNum", "pageSize")}

        for variant_name, params in (
            (f"{kind}/pageSize=99999", params1),
            (f"{kind}/pageSize=-1", params2),
            (f"{kind}/原始", params3),
        ):
            try:
                # 记录请求
                EXPORT_DIAG["requests"].append(
                    "DIRECT " + url + " [" + variant_name + "]\n    body: " + json.dumps(params, ensure_ascii=False)[:500]
                )
                print(f"  -> 直接 POST [{variant_name}] -> {url}")
                # 用 page.context.request 绕过浏览器 CORS（gateway 域不带 CORS 头, 浏览器 fetch 会抛 TypeError: Failed to fetch）
                # 但 APIContext 自动复用浏览器 cookie/会话, 不需要手动带 cookie
                try:
                    resp = page.context.request.post(
                        url,
                        data=json.dumps(params),
                        headers={
                            "Content-Type": "application/json",
                            "Referer": page.url,
                            "Origin": "https://chengyun.91msl.com",
                            "Accept": "application/json, text/plain, */*",
                        },
                        timeout=30000,
                    )
                    status = resp.status
                    body_bytes = resp.body() if hasattr(resp, "body") else b""
                    body_text = body_bytes.decode("utf-8", "ignore") if body_bytes else ""
                    result = {"ok": True, "status": status, "body": body_text[:800]}
                except Exception as ex:
                    result = {"ok": False, "err": str(ex)}
                # 记录响应
                EXPORT_DIAG["responses"].append(
                    f"DIRECT [{variant_name}] -> {url} : {json.dumps(result, ensure_ascii=False)[:600]}"
                )
                if not result or not result.get("ok"):
                    last_err = f"{variant_name}: 请求失败 {result}"
                    continue
                body_text = result.get("body", "")
                # xlsx 二进制流(浏览器 fetch 不会返二进制 base64 头吗? 通常会, 但这里安全起见用 base64 检测)
                if body_text.startswith("PK") or body_text[:2] == "PK":
                    # 直接是 xlsx 文本(不可能, 但兜底)
                    tmp = OUTPUT_DIR / f"_tmp_{key}.xlsx"
                    tmp.write_bytes(body_text.encode("utf-8", "ignore")[:200])
                # 尝试 base64 -> 解码检测
                try:
                    import base64 as _b64
                    raw = _b64.b64decode(body_text[:200])
                    if raw[:2] == b"PK":
                        tmp = OUTPUT_DIR / f"_tmp_{key}.xlsx"
                        tmp.write_bytes(_b64.b64decode(body_text))
                        return (tmp, f"base64-xlsx {variant_name}")
                except Exception:
                    pass
                # 解析 JSON
                try:
                    j = json.loads(body_text)
                except Exception:
                    last_err = f"{variant_name}: 非 JSON, 前80={body_text[:80]!r}"
                    continue
                code = j.get("code")
                data = j.get("data")
                if code == 0 and data:
                    # data 可能是 base64 编码的 xlsx
                    if isinstance(data, str) and len(data) > 100:
                        try:
                            raw = _b64.b64decode(data)
                            if raw[:2] == b"PK":
                                tmp = OUTPUT_DIR / f"_tmp_{key}.xlsx"
                                tmp.write_bytes(raw)
                                return (tmp, f"data-base64-xlsx {variant_name}")
                        except Exception:
                            pass
                    # data 是行列表(分页接口的 records 字段)
                    records = None
                    if isinstance(data, list) and data:
                        records = data
                    elif isinstance(data, dict):
                        records = (
                            data.get("records")
                            or data.get("list")
                            or data.get("rows")
                            or data.get("items")
                            or []
                        )
                    if records and isinstance(records, list) and records:
                        all_records.extend(records)
                        # 如果返回 total 超过已取, 继续分页
                        total = (
                            j.get("total")
                            or (data.get("total") if isinstance(data, dict) else None)
                            or len(records)
                        )
                        page_size = params.get("pageSize") or 20
                        if total > len(all_records) and page_size and page_size > 0:
                            for pn in range(2, (total // page_size) + 2):
                                params_p = {**params, "pageNum": pn}
                                rp = page.evaluate(
                                    """async ({url, params, headers}) => {
                                        const r = await fetch(url, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(params) });
                                        return await r.text();
                                    }""",
                                    {"url": url, "params": params_p, "headers": common_headers},
                                )
                                try:
                                    pj = json.loads(rp)
                                except Exception:
                                    break
                                pd = pj.get("data") or {}
                                pr = (
                                    (pd.get("records") if isinstance(pd, dict) else None)
                                    or (pd.get("list") if isinstance(pd, dict) else None)
                                    or (pd if isinstance(pd, list) else None)
                                    or []
                                )
                                if not pr:
                                    break
                                all_records.extend(pr)
                                if len(all_records) >= total:
                                    break
                        return ("json_rows", all_records, j)
                last_err = f"{variant_name}: JSON code={code}, data={str(data)[:100]!r}"
            except Exception as ex:
                last_err = f"{variant_name}: {ex}"
    print(f"  -> 直接 fetch 全部失败: {last_err}")
    return None


def _rows_to_csv(rows, key):
    """rows: openpyxl 读取的 values 列表。智能找表头并写 CSV。返回 CSV 路径。"""
    if not rows:
        raise RuntimeError(f"{key}: 导出内容为空")
    header_idx = 0
    for i, r in enumerate(rows):
        cells = [c for c in r if c is not None and str(c).strip()]
        if len(cells) >= 3 and sum(1 for c in cells if isinstance(c, str)) >= 2:
            header_idx = i
            break
    headers = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    data_rows = rows[header_idx + 1:]
    csv_path = OUTPUT_DIR / f"{key}.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in data_rows:
            if not any(c is not None and str(c).strip() for c in r):
                continue
            writer.writerow([c if c is not None else "" for c in r])
    print(f"  -> CSV 已保存: {csv_path} (表头: {len(headers)} 列, 数据: {len(data_rows)} 行)")
    return str(csv_path)


# ============ 通用: 导出 + 转 CSV ============
def click_export_and_save_csv(page, key, sheet_index=0):
    """
    点"导出"按钮, 抓 xlsx 下载, 转 CSV(跳过表头之上可能的标题行/合并行)。
    增强: 精确按钮选择器、原生 dialog 自动接受、Element UI 确认弹窗处理、失败时截图诊断。
    返回 CSV 路径。
    """
    print(f"  -> 点击导出 (key={key})")
    # 清空上一轮诊断, 只保留本次导出窗口的线索
    EXPORT_DIAG["console"].clear()
    EXPORT_DIAG["responses"].clear()
    EXPORT_DIAG["requests"].clear()

    # 0) 检测是否已跳转到登录页
    if "/login" in page.url:
        raise RuntimeError(f"页面已跳转到登录页({page.url}), 登录态过期, 请重新登录承运云后重试")

    # 1) 精确选择 Element UI 导出按钮(button + el-button + 含"导出"文本)
    export_btn = page.locator("button.el-button:has-text('导出')").first
    try:
        export_btn.wait_for(state="visible", timeout=8000)
    except Exception:
        shot = OUTPUT_DIR / f"_fail_{key}_no_button.png"
        try: page.screenshot(path=str(shot), full_page=True)
        except Exception: pass
        raise RuntimeError(f"未找到导出按钮(URL={page.url}), 截图={shot}")

    # 2) 点击 + 等 download; 若出现 Element UI 确认弹窗则点"确定"
    try:
        with page.expect_download(timeout=60000) as dl_info:
            export_btn.click()
            try:
                ok_btn = page.locator(
                    ".el-message-box .el-button--primary, "
                    ".el-message-box button.el-button:has-text('确定')"
                ).first
                ok_btn.wait_for(state="visible", timeout=2500)
                print("  -> 检测到导出确认弹窗, 点击确定")
                ok_btn.click()
            except Exception:
                # 没有弹窗, 属正常
                pass
    except Exception as e:
        # ---- 兜底: 绕过按钮, 直接 POST 到导出接口并尝试取 xlsx ----
        print(f"  -> 按钮导出未触发 download, 尝试直接 POST 兜底: {e}")
        direct_result = _try_direct_export(page, key)
        if direct_result:
            # 两种成功形态: (xlsx_path, note) 或 ("json_rows", rows_list, full_json)
            if direct_result[0] == "json_rows":
                _, rows_list, full_json = direct_result
                # 构造一个伪-rows 给 _rows_to_csv (header + data_rows)
                headers = list(rows_list[0].keys()) if rows_list else []
                rows_for_csv = [headers] + [
                    [r.get(h, "") for h in headers] for r in rows_list
                ]
                _note = f"直接 POST 返回 JSON 行列表 {len(rows_list)} 行"
            else:
                tmp_xlsx, _note = direct_result
                wb = load_workbook(tmp_xlsx, data_only=True)
                ws = wb.active
                rows_for_csv = list(ws.values)
                try:
                    tmp_xlsx.unlink()
                except Exception:
                    pass
            # 写诊断(说明走的是兜底分支)
            shot = OUTPUT_DIR / f"_fail_{key}_export.png"
            try: page.screenshot(path=str(shot), full_page=True)
            except Exception: pass
            diag_path = OUTPUT_DIR / f"_diag_{key}.txt"
            try:
                with open(diag_path, "w", encoding="utf-8") as f:
                    f.write(
                        f"URL={page.url}\n\n"
                        f"按钮导出超时({e}), 已通过直接 POST 兜底成功({_note})\n\n"
                        f"--- console ({len(EXPORT_DIAG['console'])} 条) ---\n"
                        + "\n".join(EXPORT_DIAG["console"])
                        + f"\n\n--- responses ({len(EXPORT_DIAG['responses'])} 条) ---\n"
                        + "\n".join(EXPORT_DIAG["responses"])
                        + f"\n\n--- requests ({len(EXPORT_DIAG['requests'])} 条) ---\n"
                        + "\n".join(EXPORT_DIAG["requests"])
                    )
            except Exception:
                diag_path = None
            return _rows_to_csv(rows_for_csv, key)

        # ---- 真失败: 写诊断并抛错 ----
        shot = OUTPUT_DIR / f"_fail_{key}_export.png"
        try: page.screenshot(path=str(shot), full_page=True)
        except Exception: pass
        diag_path = OUTPUT_DIR / f"_diag_{key}.txt"
        try:
            with open(diag_path, "w", encoding="utf-8") as f:
                f.write(
                    f"URL={page.url}\n\n"
                    f"--- console ({len(EXPORT_DIAG['console'])} 条) ---\n"
                    + "\n".join(EXPORT_DIAG["console"])
                    + f"\n\n--- responses ({len(EXPORT_DIAG['responses'])} 条) ---\n"
                    + "\n".join(EXPORT_DIAG["responses"])
                    + f"\n\n--- requests ({len(EXPORT_DIAG['requests'])} 条) ---\n"
                    + "\n".join(EXPORT_DIAG["requests"])
                )
        except Exception:
            diag_path = None
        raise RuntimeError(
            f"点击导出失败(按钮 + 直接 POST 都未拿到文件): {e}; "
            f"诊断截图={shot}; 诊断文本={diag_path}; 当前URL={page.url}"
        )

    dl = dl_info.value
    suffix = (dl.suggested_filename.split('.')[-1] or "xlsx").lower()
    tmp_xlsx = OUTPUT_DIR / f"_tmp_{key}.{suffix}"
    dl.save_as(str(tmp_xlsx))

    # openpyxl 读取, 自动跳过非数据行(第一行为字段名的行)
    wb = load_workbook(tmp_xlsx, data_only=True)
    ws = wb.active
    rows = list(ws.values)
    if not rows:
        raise RuntimeError(f"导出文件为空: {tmp_xlsx}")

    # 智能找表头: 找第一个非空行作为 header
    header_idx = 0
    for i, r in enumerate(rows):
        cells = [c for c in r if c is not None and str(c).strip()]
        # 表头特征: 多列, 文本为主
        if len(cells) >= 3 and sum(1 for c in cells if isinstance(c, str)) >= 2:
            header_idx = i
            break

    headers = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    data_rows = rows[header_idx + 1:]

    csv_path = OUTPUT_DIR / f"{key}.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in data_rows:
            # 跳过全空行
            if not any(c is not None and str(c).strip() for c in r):
                continue
            writer.writerow([c if c is not None else "" for c in r])

    # 清理临时 xlsx
    try:
        tmp_xlsx.unlink()
    except Exception:
        pass

    print(f"  -> CSV 已保存: {csv_path} (表头: {len(headers)} 列, 数据: {len(data_rows)} 行)")
    return str(csv_path)


# ============ 来源一: 运单列表 ============
def fetch_waybill(page, time_field, start_date, end_date):
    """
    1. 打开运单列表页
    2. 设置「时间筛选」下拉 = time_field (接单时间/磅单审核通过时间/完成时间/...)
    3. 设置「选择时间」daterange = start ~ end
    4. 点击查询
    5. 点击导出, 转 CSV
    返回 CSV 路径
    """
    print(f"\n[运单列表] 开始获取 ({time_field}, {start_date} ~ {end_date})")
    page.goto(URL_WAYBILL, wait_until="domcontentloaded", timeout=TIMEOUT)
    page.wait_for_timeout(2000)
    # 等关键控件出现
    page.locator(".el-form-item__label:text-is('时间筛选')").wait_for(timeout=15000)

    set_select_by_label(page, "时间筛选", time_field)
    set_date_range(page, "选择时间", start_date, end_date, datetime_mode=False)
    click_query(page)
    return click_export_and_save_csv(page, "waybill_compare")


# ============ 来源二: 货主运单计费 ============
def fetch_billing(page, start_date, end_date):
    """
    1. 打开货主运单计费页
    2. 设置「创建时间」datetimerange = start 00:00:00 ~ end 23:59:59
    3. 点击查询
    4. 点击导出, 转 CSV
    """
    print(f"\n[货主运单计费] 开始获取 ({start_date} ~ {end_date})")
    page.goto(URL_BILLING, wait_until="domcontentloaded", timeout=TIMEOUT)
    page.wait_for_timeout(2000)
    page.locator(".el-form-item__label:text-is('创建时间')").wait_for(timeout=15000)

    # datetimerange 需要带时分
    set_date_range(
        page, "创建时间",
        f"{start_date} 00:00:00",
        f"{end_date} 23:59:59",
        datetime_mode=True,
    )
    click_query(page)
    return click_export_and_save_csv(page, "billing_compare")


# ============ 主函数 ============
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--time-field", default="接单时间", help="运单列表时间字段(默认: 接单时间)")
    parser.add_argument("--json-status", action="store_true", help="以 JSON 格式输出最终状态")
    parser.add_argument("--debug", action="store_true", help="有头模式(打开 Edge 看)")
    args = parser.parse_args()

    headless = HEADLESS and not args.debug
    time_field = args.time_field
    if time_field not in DEFAULT_TIME_FIELDS:
        print(f"[WARN] 时间字段 [{time_field}] 不在已知选项中, 仍将尝试设置")

    write_status("init", f"准备启动浏览器, 时间字段={time_field}, 范围={args.start}~{args.end}", progress=0)

    result = {
        "ok": False,
        "waybill_csv": None,
        "billing_csv": None,
        "time_field": time_field,
        "start": args.start,
        "end": args.end,
        "error": None,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=BROWSER_DATA_DIR,
                channel="msedge",
                headless=headless,
                locale="zh-CN",
                viewport={"width": 1600, "height": 1000},
            )
            page = browser.new_page()
            # 自动接受所有原生弹窗(alert/confirm/prompt), 防止导出时的二次确认阻塞导致 download 事件不触发
            page.on("dialog", lambda d: (print(f"  [dialog] accept: {(d.message or '')[:60]}"), d.accept()))
            # 诊断: 收集控制台日志与导出/错误类响应, 失败时写入 _diag_*.txt
            page.on("console", lambda m: EXPORT_DIAG["console"].append(f"[{m.type}] {m.text}"))
            page.on("response", lambda r: _on_response(r))
            page.on("request", lambda req: _on_request(req))

            # 1. 运单列表
            write_status("waybill", f"正在获取运单列表 ({time_field})", progress=10)
            try:
                result["waybill_csv"] = fetch_waybill(page, time_field, args.start, args.end)
            except Exception as e:
                err = f"运单列表获取失败: {e}"
                print(f"[ERROR] {err}")
                write_status("waybill", err, error=err, done=True)
                result["error"] = err
                browser.close()
                if args.json_status:
                    print(json.dumps(result, ensure_ascii=False))
                sys.exit(2)

            write_status("waybill", f"运单列表完成: {result['waybill_csv']}", progress=55)

            # 2. 货主计费
            write_status("billing", f"正在获取货主运单计费", progress=60)
            try:
                result["billing_csv"] = fetch_billing(page, args.start, args.end)
            except Exception as e:
                err = f"货主计费获取失败: {e}"
                print(f"[ERROR] {err}")
                write_status("billing", err, error=err, done=True)
                result["error"] = err
                browser.close()
                if args.json_status:
                    print(json.dumps(result, ensure_ascii=False))
                sys.exit(3)

            write_status("done", f"全部完成, 两个 CSV 已就绪", progress=100, done=True)
            browser.close()

        result["ok"] = True

    except Exception as e:
        err = f"浏览器启动失败: {e}"
        print(f"[ERROR] {err}")
        result["error"] = err
        write_status("init", err, error=err, done=True)
        if args.json_status:
            print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    if args.json_status:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"\n[OK] 全部完成")
        print(f"  运单: {result['waybill_csv']}")
        print(f"  计费: {result['billing_csv']}")


if __name__ == "__main__":
    main()

# compare-cloud（承运云表格对比 · 云端免费版）

把「表格对比」功能的后端搬到 **GitHub Actions** 上跑：每天北京时间 8:00（工作日）自动抓取承运云运单列表 + 货主计费，结果 CSV 推到 `data` 分支，前端（github.io 工作台）直接读取，**零成本、无需常开电脑、任何设备打开即用**。

## 架构

```
GitHub Actions (UTC 0:00 周一~周五 自动触发)
  ├─ 起临时 Ubuntu 机器（免费额度内）
  ├─ 用 CHENGYUN_COOKIES 登录态伪装已登录浏览器
  ├─ Playwright 抓 运单列表 + 货主计费 → CSV
  └─ 推到 data 分支

你打开 https://zhaoziwei11.github.io/workbench/ 的「表格对比」页
  └─ 直接从 raw.githubusercontent.com/.../data/*.csv 读最新数据
```

## 数据地址（前端读取用）

- 运单列表：`https://raw.githubusercontent.com/zhaoziwei11/compare-cloud/data/waybill_compare.csv`
- 货主计费：`https://raw.githubusercontent.com/zhaoziwei11/compare-cloud/data/billing_compare.csv`
- 元信息（抓取时间/范围/是否成功）：`https://raw.githubusercontent.com/zhaoziwei11/compare-cloud/data/meta.json`

> raw.githubusercontent.com 默认带 `Access-Control-Allow-Origin: *`，github.io 前端可跨域直接 fetch。

## 一次性配置：导出 cookie

抓取靠你浏览器里承运云的**登录态 cookie**（不是账号密码）。只需导出一次，过期后重导：

1. 在 **Edge/Chrome** 打开并登录 `https://chengyun.91msl.com`
2. 装扩展 **Cookie-Editor**（Edge / Chrome 商店搜得到）
3. 点扩展图标 → 右上角「导出」→ 选 **Export as JSON** → 复制整段文本
4. 浏览器打开本仓库 **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `CHENGYUN_COOKIES`
   - Secret: 粘贴刚才复制的 JSON
5. 回到 **Actions** 标签页，手动跑一次 `daily-compare-fetch` 验证

cookie 过期时 Action 会失败（GitHub 发邮件），重新导出第 3 步的 JSON、更新 Secret 即可。

## 手动触发自定义日期

仓库 **Actions → daily-compare-fetch → Run workflow**，可填 `start` / `end` / `time_field`，不填则自动算「上一个工作日..昨天」。

## 本地调试

```bash
pip install -r requirements.txt
playwright install chromium
CHENGYUN_COOKIES="$(cat cookies.json)" python fetch_cloud.py --start 2026-08-01 --end 2026-08-02
```

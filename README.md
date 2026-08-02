# compare-cloud · 承运云表格对比（GitHub Actions 云端免费抓取）

每天**北京时间 8:00（工作日）**自动抓取承运云「运单列表 + 货主运单计费」，结果以 CSV 推到 `data` 分支，
前端（workbench 的「表格对比」页）直接从 `raw.githubusercontent.com/zhaoziwei11/compare-cloud/data/` 读取。

**零成本、不用常开电脑、任何设备打开网页即可看。**

## 鉴权方式（重要）

承运云用 **`Token` 请求头**鉴权（不是 cookie）。把登录后的 Token 存入仓库 Secret **`CHENGYUN_TOKEN`** 即可。
获取 Token：在已登录的 `chengyun.91msl.com` 页面按 F12 → Network → 任意 XHR 请求 → Request Headers 里的 `Token:` 值。

> Token 长期有效（除非改密码），比 cookie 稳。过期后重新填一次 Secret 即可。

## 配置 Secret

仓库 `Settings → Secrets and variables → Actions → New repository secret`：
- Name: `CHENGYUN_TOKEN`
- Secret: 你的 Token 值

## 手动触发

`Actions → daily-compare-fetch → Run workflow`，可填 `start`/`end` 手动指定范围。

## 请求体模版

`params_waybill.json` / `params_billing.json` 是接口请求体模版，含 `__START__`/`__END__` 日期占位，
由脚本在运行时替换为抓取范围。若接口字段名变更，改这两个文件即可，无需改代码。

## 本地调试

```bash
CHENGYUN_TOKEN=xxxx python fetch_cloud.py --start 2026-08-01 --end 2026-08-02
```

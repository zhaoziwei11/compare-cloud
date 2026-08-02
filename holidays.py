# -*- coding: utf-8 -*-
"""2026 年中国法定节假日 / 调休补班日判定（与 compare_backend.py 保持一致）。

仅用于 GitHub Actions 每日自动计算抓取日期范围：上一个工作日 .. 昨天。
"""
from datetime import date, timedelta

# 2026 法定节假日（放假，不含调休补班日）
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}

# 2026 调休补班日（周末上班，仍算工作日）
MAKEUP_WORKDAYS_2026 = {
    "2026-01-04",
    "2026-02-14", "2026-02-28",
    "2026-05-09",
    "2026-09-20", "2026-10-10",
}


def is_workday(d: date) -> bool:
    s = d.strftime("%Y-%m-%d")
    if s in MAKEUP_WORKDAYS_2026:
        return True
    if s in HOLIDAYS_2026:
        return False
    if d.weekday() >= 5:  # 5=周六 6=周日
        return False
    return True


def last_workday(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_workday(cur):
        cur -= timedelta(days=1)
    return cur


def compute_auto_range(today: date):
    """返回 (start, end)：上一个工作日 .. 昨天。保证 start <= end。"""
    start = last_workday(today)
    end = today - timedelta(days=1)
    if start > end:
        start = end
    return start, end

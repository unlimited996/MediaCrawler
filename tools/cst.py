# -*- coding: utf-8 -*-
"""统一的东八区（UTC+8）时间工具。

背景：数据库 timestamptz 列存的是 UTC；前端早期直接把 UTC 字符串切片显示，
导致页面时间比真实本地时间少 8 小时。现在所有面向展示的时间都经由本模块
转成 UTC+8 后再输出，杜绝"存对了、显示错"的问题。
"""
from datetime import datetime, timedelta, timezone

# 东八区（中国标准时间）。项目面向中文用户，统一用它做日历日/小时分桶。
CST = timezone(timedelta(hours=8), name="CST")


def utc_now() -> datetime:
    """当前 UTC 时间（带时区，写库用）。"""
    return datetime.now(timezone.utc)


def to_cst(dt) -> datetime:
    """任意时间（naive 按 UTC 解释）→ UTC+8。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CST)


def cst_now() -> datetime:
    """当前 UTC+8 时间。"""
    return datetime.now(CST)


def fmt_cst(dt, with_seconds: bool = False) -> str:
    """格式化为 UTC+8 字符串：'2026-10-16 16:42'（默认不带秒）。"""
    d = to_cst(dt)
    if d is None:
        return ""
    return d.strftime("%Y-%m-%d %H:%M:%S") if with_seconds else d.strftime("%Y-%m-%d %H:%M")


def fmt_cst_short(dt) -> str:
    """简短格式：'10-16 16:42'，用于时间轴节点。"""
    d = to_cst(dt)
    if d is None:
        return ""
    return d.strftime("%m-%d %H:%M")


def cst_iso(dt) -> str:
    """带 +08:00 偏移的 ISO 字符串（前端可直接 new Date() 解析）。"""
    d = to_cst(dt)
    return d.isoformat() if d else ""


def day_label(day_offset: int) -> str:
    """日历日偏移 → 中文标签：今天 / 昨天 / 前天 / N 天前。"""
    if day_offset <= 0:
        return "今天"
    if day_offset == 1:
        return "昨天"
    if day_offset == 2:
        return "前天"
    return f"{day_offset} 天前"


def cst_hour_bounds(dt) -> tuple:
    """给定时间所在 CST 自然小时的 [起, 止)，两端均为 UTC（供 SQL 比较）。"""
    d = to_cst(dt)
    start = d.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

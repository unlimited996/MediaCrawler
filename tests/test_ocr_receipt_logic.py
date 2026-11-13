# -*- coding: utf-8 -*-
"""ENTITLEMENT 批次 1 服务端测试：OCR 行解析与东八区自然日口径。

不依赖 DB——`_parse_receipt_line` / `_ocr_today` 都是纯函数，
从 api.routers.recipe 导入会拉起 fastapi + 全部模型依赖，单独提取逻辑验证。
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from api.routers.recipe import (  # noqa: E402
    OCR_DAILY_LIMIT, _ocr_today, _parse_receipt_line,
)


def test_parse_line_space_and_unit():
    assert _parse_receipt_line("西红柿 500克") == {"name": "西红柿", "detail": "500克"}
    assert _parse_receipt_line("牛腩 1.5kg") == {"name": "牛腩", "detail": "1.5kg"}


def test_parse_line_tight_and_multiplier():
    assert _parse_receipt_line("鸡蛋6个") == {"name": "鸡蛋", "detail": "6个"}
    assert _parse_receipt_line("土豆 x3") == {"name": "土豆", "detail": "3"}
    assert _parse_receipt_line("小葱*2把") == {"name": "小葱", "detail": "2把"}


def test_parse_line_name_only():
    assert _parse_receipt_line("香菜") == {"name": "香菜", "detail": None}


def test_parse_line_garbage_filtered():
    assert _parse_receipt_line("12.50") is None          # 价格行
    assert _parse_receipt_line("   ") is None            # 空行
    assert _parse_receipt_line("上海市浦东新区世纪大道100号家乐福超市欢迎光临") is None  # 地址长行


def test_quota_is_lv1_three_per_day():
    """批次 1：未接入 membership 前固定 Lv1=3 次/日。"""
    assert OCR_DAILY_LIMIT == 3


def test_today_is_shanghai_calendar_day():
    """自然日口径必须与 App 端按天清零一致：东八区（+8，无夏令时）。"""
    today = _ocr_today()
    assert isinstance(today, date)
    assert today == datetime.now(tz=ZoneInfo("Asia/Shanghai") if _has_tzdata()
                                 else timezone(timedelta(hours=8))).date()


def _has_tzdata() -> bool:
    try:
        ZoneInfo("Asia/Shanghai")
        return True
    except Exception:
        return False


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                           if k.startswith("test_") and callable(v)}.items()):
        fn()
        print(f"PASS {name}")
    print("all passed")

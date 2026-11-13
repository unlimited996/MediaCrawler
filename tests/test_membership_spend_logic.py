# -*- coding: utf-8 -*-
"""ENTITLEMENT 批次 6 测试：SKU 结构、满级权益口径、兑换语义规则。

DB 行为（条件更新防透支、顺延、幂等）由部署后远程验收覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.routers.recipe import (  # noqa: E402
    MEMBER_INGREDIENT_LIMIT, MEMBER_OCR_DAILY_LIMIT, MEMBERSHIP_FULL_LEVEL,
    MEMBERSHIP_SKUS, _sku_by_id, membership_entitlements, membership_level_of,
)


def test_sku_range_covers_1day_to_1year():
    days = [s["days"] for s in MEMBERSHIP_SKUS]
    assert min(days) == 1 and max(days) == 365
    # 单调递增且价格随天数上涨
    pairs = list(zip(days, [s["cost"] for s in MEMBERSHIP_SKUS]))
    for (d1, c1), (d2, c2) in zip(pairs, pairs[1:]):
        assert d2 > d1 and c2 > c1
    # 单日成本应低于长档（买长档更划算）
    unit_costs = [s["cost"] / s["days"] for s in MEMBERSHIP_SKUS]
    assert unit_costs[0] > unit_costs[-1], "长档单日成本应低于日卡"


def test_sku_lookup():
    assert _sku_by_id("vip_1d")["days"] == 1
    assert _sku_by_id("vip_365d")["days"] == 365
    assert _sku_by_id("vip_999d") is None
    assert _sku_by_id("") is None


def test_member_entitlements_are_full_level():
    """会员期权益必须与满级(Lv5)口径一致：食材 50 / OCR 10。"""
    assert MEMBERSHIP_FULL_LEVEL == 5
    full = membership_entitlements(5, 0)
    assert MEMBER_INGREDIENT_LIMIT == full["customIngredientLimit"] == 50
    assert MEMBER_OCR_DAILY_LIMIT == full["ocrDailyLimit"] == 10


def test_effective_level_semantics_documented():
    """兑换期间 level=满级；过期后回落 points 等级——由 _effective_level 实现，
    该函数需要 DB，这里验证基础等级计算与回落一致性。"""
    assert membership_level_of(230) == 3  # 兑换期过后 points 不变 → 回 Lv3


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                           if k.startswith("test_") and callable(v)}.items()):
        fn()
        print(f"PASS {name}")
    print("all passed")

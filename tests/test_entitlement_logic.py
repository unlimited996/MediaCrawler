# -*- coding: utf-8 -*-
"""ENTITLEMENT 批次 2/3 服务端测试：签名协议、等级/权益计算、幂等发分语义。

不依赖 DB 的纯逻辑部分。DB 相关行为（UNIQUE 幂等、汇总累加）由验收清单
在部署环境实测覆盖。
"""
from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.routers.recipe import (  # noqa: E402
    MEMBERSHIP_TITLES, POINTS_PER_MEAL_LOGGED, _admin_guard, _signing_string,
    membership_entitlements, membership_level_of,
)


def _sign(secret_hex: str, method: str, path: str, body: bytes,
          nonce: str, ts: str) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    raw = _signing_string(method, path, body_hash, nonce, ts)
    return hmac.new(bytes.fromhex(secret_hex), raw.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def test_signing_string_format():
    raw = _signing_string("POST", "/api/ocr/receipt",
                          "deadbeef" * 8, "n1", "1700000000")
    assert raw == "POST\n/api/ocr/receipt\n" + ("deadbeef" * 8) + "\nn1\n1700000000"


def test_sign_roundtrip():
    secret = "ab" * 32
    sig = _sign(secret, "POST", "/api/meal-log", b'{"a":1}', "nonce-x", "1700000000")
    assert len(sig) == 64  # sha256 hex
    # 同输入可复现
    assert sig == _sign(secret, "POST", "/api/meal-log", b'{"a":1}', "nonce-x", "1700000000")
    # body 变了签名必须变
    assert sig != _sign(secret, "POST", "/api/meal-log", b'{"a":2}', "nonce-x", "1700000000")


def test_level_thresholds():
    assert membership_level_of(0) == 1
    assert membership_level_of(59) == 1
    assert membership_level_of(60) == 2
    assert membership_level_of(180) == 3
    assert membership_level_of(400) == 4
    assert membership_level_of(800) == 5
    assert membership_level_of(9**9) == 5
    assert len(MEMBERSHIP_TITLES) == 5


def test_entitlements_formula():
    ent = membership_entitlements(3, ocr_used_today=2)
    assert ent["customIngredientLimit"] == 30   # level x 10
    assert ent["ocrDailyLimit"] == 6           # Lv3-4
    assert ent["ocrUsedToday"] == 2
    assert membership_entitlements(5, 0)["ocrDailyLimit"] == 10
    assert membership_entitlements(1, 0)["ocrDailyLimit"] == 3


def test_meal_award_constants():
    assert POINTS_PER_MEAL_LOGGED == 10


def test_admin_guard_fail_closed():
    """未配置 ADMIN_TOKEN 时必须 fail-closed（403）。"""
    import os
    import api.routers.recipe as mod
    os.environ.pop("ADMIN_TOKEN", None)
    mod._admin_token.cache_clear()
    class _FakeReq:
        headers = {}
    try:
        with _admin_guard(_FakeReq()):
            pass
        raise AssertionError("未配置 ADMIN_TOKEN 却放行了")
    except Exception as e:
        assert getattr(e, "status_code", None) == 403


def test_admin_guard_accepts_valid_token():
    import os
    os.environ["ADMIN_TOKEN"] = "s3cret"
    import api.routers.recipe as mod
    mod._admin_token.cache_clear()
    class _FakeReq:
        headers = {"X-Admin-Token": "s3cret"}
    with _admin_guard(_FakeReq()):
        pass
    os.environ.pop("ADMIN_TOKEN", None)
    mod._admin_token.cache_clear()


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                           if k.startswith("test_") and callable(v)}.items()):
        fn()
        print(f"PASS {name}")
    print("all passed")

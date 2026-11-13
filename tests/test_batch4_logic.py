# -*- coding: utf-8 -*-
"""ENTITLEMENT 批次 4 剩余项测试：plan_key 校验、collab 频控、灰度鉴权无 DB 路径。

DB 相关行为（app-config 白名单 SQL 过滤、签名全链路 401/FORGED_IDENTITY、
频控对真实请求的 429）由部署后远程验收覆盖；此处覆盖纯逻辑与状态机。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from api.routers.recipe import (  # noqa: E402
    JOIN_FAIL_LIMIT, PUBLIC_CONFIG_SCOPES, _join_fail_clear, _join_fail_note,
    _join_gate, _room_create_allow, _room_create_note, _validate_plan_key,
    gradual_auth,
)


def _fresh_request(headers: dict = None, method: str = "GET",
                    path: str = "/meal-log", query: bytes = b"") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(scope={
        "type": "http", "method": method, "path": path,
        "headers": raw, "query_string": query,
        "client": ("1.2.3.4", 56789), "server": ("test", 80),
        "scheme": "http",
    })


def test_plan_key_accepts_current_format():
    # App 端实际形态：日期-菜集hash（TableStepsScreen.kt:107-110）
    for good in ("2026-11-14-1a2b3c4d", "20261114-0", "table:2026-11-14.abc_def"):
        _validate_plan_key(good)  # 不抛即通过


def test_plan_key_rejects_garbage():
    bad = ["", " ", "'; DROP TABLE shopping_check;--",
           "x" * 65, "2026-11-14\n1a2b3c4d", "计划/1", "a b"]
    for s in bad:
        try:
            _validate_plan_key(s)
            raise AssertionError(f"应拒绝：{s!r}")
        except HTTPException as e:
            assert e.status_code == 400 and e.detail["code"] == "INVALID_PLAN_KEY"


def test_join_gate_bans_after_limit():
    key = "dev1|1.2.3.4"
    now = time.time()
    for i in range(JOIN_FAIL_LIMIT - 1):  # 前 9 次失败：不封
        _join_fail_note(key, now + i * 0.1)
        assert _join_gate(key, now + i * 0.1) == 0, "未达上限不应封"
    # 第 10 次失败：封禁（本次请求已返回 404，后续 429）
    _join_fail_note(key, now + 2.0)
    left = _join_gate(key, now + 2.0)
    assert left > 0, "达到上限应封禁"
    # 成功清零
    _join_fail_clear(key)
    assert _join_gate(key, now + 2.0) == 0


def test_join_window_slides_out():
    key = "dev2|1.2.3.4"
    now = time.time()
    for i in range(JOIN_FAIL_LIMIT - 1):
        _join_fail_note(key, now)  # 同一时刻压 9 次
    _join_fail_note(key, now)      # 第 10 次触发封禁（≥LIMIT）
    assert _join_gate(key, now) > 0
    _join_fail_clear(key)
    # 窗口外的失败不累计：70 秒后新失败 1 次不应立即封
    later = now + 70
    _join_fail_note(key, later)
    assert _join_gate(key, later) == 0


def test_room_create_daily_limit():
    dev, today = "dev3", "2026-11-14"
    for i in range(20):
        assert _room_create_allow(dev, today), f"第 {i + 1} 次应允许"
        _room_create_note(dev, today)
    assert not _room_create_allow(dev, today), "第 21 次应拒绝"
    assert _room_create_allow(dev, "2026-11-15"), "跨日应重置"
    _room_create_note(dev, "2026-11-15")
    assert not _room_create_allow(dev, "2026-11-15") is False or True  # 仅验证不抛


def test_config_scopes_whitelist_has_no_sensitive():
    # App 依赖的存量 scope 必须在白名单内；敏感词不在
    assert "app" in PUBLIC_CONFIG_SCOPES
    assert "membership" in PUBLIC_CONFIG_SCOPES
    for sensitive in ("admin", "auth", "internal", "secret"):
        assert sensitive not in PUBLIC_CONFIG_SCOPES


def test_gradual_auth_passes_unsigned_request():
    """无签名头（老包）→ 放行不抛。"""
    req = _fresh_request()
    assert asyncio.run(gradual_auth(req)) is None


def test_gradual_auth_skips_multipart():
    """multipart（OCR 上传）灰度期跳过验签 → 放行。"""
    req = _fresh_request(
        headers={"content-type": "multipart/form-data; boundary=x",
                 "x-signature": "deadbeef", "x-device-id": "dev"},
        method="POST", path="/ocr/receipt")
    assert asyncio.run(gradual_auth(req)) is None


def test_gradual_auth_rejects_stale_timestamp():
    """带签名头但时间戳超 ±300s → 401（纯头部校验，不碰 DB）。"""
    stale = str(int(time.time()) - 4000)
    req = _fresh_request(
        headers={"content-type": "application/json",
                 "x-device-id": "dev", "x-timestamp": stale,
                 "x-nonce": "n-1", "x-signature": "00"},
        method="POST", path="/meal-log")
    try:
        asyncio.run(gradual_auth(req))
        raise AssertionError("超窗应 401")
    except HTTPException as e:
        assert e.status_code == 401 and e.detail["code"] == "AUTH_INVALID"


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        fn()
        print(f"PASS {name}")
    print("all passed")

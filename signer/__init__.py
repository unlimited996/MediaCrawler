# -*- coding: utf-8 -*-
"""签名抽象层（方案 #5：JS 签名逻辑解耦重构）。

目标：
1. 把"签名怎么算"从平台 client 里抽出来，client 只关心"要签什么"
2. 隔离执行引擎依赖（纯 Python / Node execjs / 未来可加 WASM、远程签名服务）
3. 为 #3「去除 Playwright 依赖」铺路 —— 签名不需要浏览器，登录才需要

用法：
    from signer import get_signer
    headers = get_signer("xhs").sign(uri="/api/xxx", data={...}, cookie_str="a1=...", method="GET")
"""
from .base import BaseSigner
from .registry import get_signer, register_signer, available_platforms

__all__ = ["BaseSigner", "get_signer", "register_signer", "available_platforms"]

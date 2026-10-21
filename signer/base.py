# -*- coding: utf-8 -*-
"""签名器抽象基类。"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Union


def make_trace_id() -> str:
    """生成链路追踪 ID（各平台字段名不同，由子类决定放哪个头）。"""
    return uuid.uuid4().hex


class BaseSigner(ABC):
    """所有签名器的统一接口。

    约定：sign() 返回**需要合并进请求头**的字典，key 就是最终的 HTTP 头名。
    实现类必须保证：同样入参 → 同样输出（纯函数），便于对拍验证。
    """

    platform: str = ""
    # 执行引擎：pure / node —— 用于前端展示与"去浏览器"能力判断
    engine: str = "pure"
    # 是否需要浏览器环境（True 的签名器无法在无浏览器模式下工作）
    requires_browser: bool = False

    @abstractmethod
    def sign(
        self,
        uri: str,
        data: Optional[Union[Dict, str]] = None,
        cookie_str: str = "",
        method: str = "POST",
        **kwargs: Any,
    ) -> Dict[str, str]:
        """生成签名请求头。

        Args:
            uri: 请求路径（可能已带 query string）
            data: GET 的 params 或 POST 的 payload
            cookie_str: 原始 Cookie 串
            method: GET / POST
        """
        raise NotImplementedError

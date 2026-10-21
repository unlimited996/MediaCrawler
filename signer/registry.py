# -*- coding: utf-8 -*-
"""签名器注册表：按平台取签名器，便于替换实现（纯算法 / Node / 远程服务）。"""
from __future__ import annotations

from typing import Dict, Optional, Type

from .base import BaseSigner

_REGISTRY: Dict[str, Type[BaseSigner]] = {}
_INSTANCES: Dict[str, BaseSigner] = {}


def register_signer(signer_cls: Type[BaseSigner]) -> None:
    _REGISTRY[signer_cls.platform] = signer_cls


def get_signer(platform: str) -> Optional[BaseSigner]:
    """获取平台签名器；未注册的平台返回 None（该平台可能不需要签名）。"""
    if platform in _INSTANCES:
        return _INSTANCES[platform]

    cls = _REGISTRY.get(platform)
    if cls is None:
        # 惰性导入各实现，避免无谓的 execjs / xhshow 加载
        if platform == "xhs":
            from .xhs_signer import XhsSigner

            cls = XhsSigner
        elif platform in ("dy", "douyin"):
            from .node_signer import DouyinSigner

            cls = DouyinSigner
        elif platform == "zhihu":
            from .node_signer import ZhihuSigner

            cls = ZhihuSigner
        if cls is not None:
            register_signer(cls)

    if cls is None:
        return None

    _INSTANCES[platform] = cls()
    return _INSTANCES[platform]


def available_platforms() -> Dict[str, Dict[str, str]]:
    """列出已注册的签名器及其执行引擎（供前端/诊断接口展示）。"""
    for p in ("xhs", "dy", "zhihu"):
        get_signer(p)
    return {
        platform: {"platform": cls.platform, "engine": cls.engine,
                   "requires_browser": cls.requires_browser}
        for platform, cls in _REGISTRY.items()
    }

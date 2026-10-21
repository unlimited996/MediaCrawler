# -*- coding: utf-8 -*-
"""基于 Node（execjs）的签名器：抖音 / 知乎。

不依赖浏览器，只依赖本机 Node 运行时。JS 文件用绝对路径加载，
避免原来 `open('libs/douyin.js')` 依赖当前工作目录的隐患。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Union

from .base import BaseSigner

LIBS_DIR = Path(__file__).resolve().parents[1] / "libs"


class NodeJsSigner(BaseSigner):
    """execjs 包装基类：延迟编译 + 进程内缓存。"""

    engine = "node"
    requires_browser = False
    js_file: str = ""

    def __init__(self) -> None:
        self._compiled = None

    def _load(self):
        if self._compiled is None:
            import execjs

            path = LIBS_DIR / self.js_file
            if not path.exists():
                raise FileNotFoundError(f"签名 JS 不存在：{path}")
            # utf-8-sig 兼容带 BOM 的 JS 文件
            self._compiled = execjs.compile(path.read_text(encoding="utf-8-sig"))
        return self._compiled

    def node_available(self) -> bool:
        try:
            import execjs

            execjs.get().name  # 拿不到运行时会抛异常
            return True
        except Exception:
            return False

    def sign(self, *args: Any, **kwargs: Any) -> Dict[str, str]:
        raise NotImplementedError


class DouyinSigner(NodeJsSigner):
    """抖音 a_bogus 签名。

    注意：抖音另有 `get_a_bogus_from_playwright()` 走浏览器执行的路径，
    本签名器走纯 Node，配合 #3 的"无浏览器模式"使用。
    """

    platform = "dy"
    js_file = "douyin.js"

    def sign(
        self,
        uri: str,
        data: Optional[Union[Dict, str]] = None,
        cookie_str: str = "",
        method: str = "GET",
        params: Optional[str] = None,
        user_agent: str = "",
        **kwargs: Any,
    ) -> Dict[str, str]:
        js = self._load()
        sign_js_name = "sign_reply" if "/reply" in uri else "sign_datail"
        value = js.call(sign_js_name, params if params is not None else data, user_agent)
        return {"a_bogus": value}


class ZhihuSigner(NodeJsSigner):
    """知乎 x-zse-96 签名。"""

    platform = "zhihu"
    js_file = "zhihu.js"

    def sign(
        self,
        uri: str,
        data: Optional[Union[Dict, str]] = None,
        cookie_str: str = "",
        method: str = "GET",
        url: Optional[str] = None,
        cookies: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, str]:
        js = self._load()
        result = js.call("get_sign", url or uri, cookies or cookie_str)
        if isinstance(result, dict):
            return {"x-zse-96": result.get("x-zse-96", "")}
        return {"x-zse-96": str(result)}

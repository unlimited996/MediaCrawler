# -*- coding: utf-8 -*-
"""小红书签名器：纯 Python 实现（xhshow），不需要浏览器。"""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from .base import BaseSigner, make_trace_id


class XhsSigner(BaseSigner):
    """基于 xhshow 的纯算法签名。

    说明：仓库里的 `media_platform/xhs/playwright_sign.py` 虽然文件名带 playwright，
    实际用的就是 xhshow 纯 Python 实现（历史命名遗留）。这里把它抽出来，
    使 client 不再直接依赖平台目录下的模块。
    """

    platform = "xhs"
    engine = "pure"
    requires_browser = False

    def sign(
        self,
        uri: str,
        data: Optional[Union[Dict, str]] = None,
        cookie_str: str = "",
        method: str = "POST",
        **kwargs: Any,
    ) -> Dict[str, str]:
        from xhshow import Xhshow

        client = Xhshow()
        payload = data if isinstance(data, dict) else {}

        if method.upper() == "POST":
            headers = client.sign_headers_post(uri=uri, cookies=cookie_str, payload=payload)
        else:
            headers = client.sign_headers_get(uri=uri, cookies=cookie_str, params=payload)

        return {
            "X-S": headers.get("x-s", ""),
            "X-T": headers.get("x-t", ""),
            "x-S-Common": headers.get("x-s-common", ""),
            "X-B3-Traceid": headers.get("x-b3-traceid") or make_trace_id(),
        }

# -*- coding: utf-8 -*-
"""媒体 URL 兜底：把「带时间戳签名的过期外链」换成免签名镜像。

小红书图片外链形如：

    http://sns-webpic-qc.xhscdn.com/202610301618/cd31778d.../notes_pre_post/xxx!nd_dft_wlteh_webp_3
                                  └─ 生成时间 ─┘ └─ 签名 ─┘ └──────── 对象 key ────────┘

时间戳一过就 403，签名无法在客户端重算。但 CDN 的免签名域名
（``sns-img-qc`` / ``-hw`` / ``-bd`` / ``-qn``）对同一对象 key **仍然返回 200**
（2026-11-09 实测：过期 10 天的链接换域名后 200 image/webp）。

注意：这些域名带防盗链 —— 浏览器带本站 Referer 会 403，带小红书 Referer
或不带 Referer 是 200。所以前端 ``<img>`` 必须加 ``referrerPolicy="no-referrer"``。
"""
from __future__ import annotations

import re
from typing import Optional

#: 带签名的小红书图片 CDN：<host>/<10位时间戳>/<签名>/<对象 key>
_SIGNED_RE = re.compile(
    r"^https?://sns-webpic-[a-z0-9]+\.xhscdn\.com/\d+/[^/?#]+/([^?#]+)")

#: 已经是免签名域名
_PLAIN_RE = re.compile(r"^https?://sns-img-[a-z0-9]+\.xhscdn\.com/")

MIRROR_HOST = "https://sns-img-qc.xhscdn.com"


def xhs_mirror_url(url: str) -> Optional[str]:
    """返回免签名镜像 URL；不适用时返回 None（非小红书图 / 结构不符）。

    只保留对象 key（含 ``!`` 后缀），丢掉时间戳与签名两段。
    """
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if _PLAIN_RE.match(u):
        return u
    m = _SIGNED_RE.match(u)
    if m:
        return f"{MIRROR_HOST}/{m.group(1)}"
    return None

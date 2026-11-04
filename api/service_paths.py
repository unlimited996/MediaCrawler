# -*- coding: utf-8 -*-
"""对外 URL 拼装（跨服务/拆分部署时唯一可信来源）。

为什么需要它：接口返回给前端的媒体链接是**绝对路径**，浏览器会直接请求。
这些路径必须带对服务名前缀 —— 原来硬编码 `/api/data/media/...`，
拆分成 crawler / library 两个服务后，`/api/...` 根本不存在（404），
`/data/api/...` 也不再是服务名。

统一从 `config.LIBRARY_API_PREFIX`（默认 `/library/api`）拼，
单体入口（api/main.py）也挂了同一前缀，所以两种部署方式下都能直接用。
"""
from __future__ import annotations

import config


def library_api(path: str = "") -> str:
    """内容库 API 的绝对路径，例如 library_api('assets/raw/a.png')。"""
    prefix = getattr(config, "LIBRARY_API_PREFIX", "/library/api").rstrip("/")
    p = (path or "").lstrip("/")
    return f"{prefix}/{p}" if p else prefix


def asset_url(kind: str, path: str) -> str:
    """内容库里的文件类资源：kind 取 raw / media / files / download。"""
    return library_api(f"assets/{kind}/{str(path).lstrip('/')}")


def media_url(platform: str, content_id: str, filename: str) -> str:
    """媒体文件（图片/视频）的对外地址。"""
    return library_api(f"assets/media/{platform}/{content_id}/{filename}")

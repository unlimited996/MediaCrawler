# -*- coding: utf-8 -*-
"""WebUI 静态资源挂载（单体 main.py 与 data 服务共用）。

注意：**必须在所有路由注册完之后调用**，否则 `/{name}` 形式的挂载会抢先
匹配掉 API 路径。
"""
from __future__ import annotations

import os

from fastapi.staticfiles import StaticFiles


def mount_webui(app, webui_dir: str) -> bool:
    """把前端构建产物挂到应用上；目录不存在返回 False（API 仍可用）。"""
    if not os.path.exists(webui_dir):
        return False

    assets_dir = os.path.join(webui_dir, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    logos_dir = os.path.join(webui_dir, "logos")
    if os.path.exists(logos_dir):
        app.mount("/logos", StaticFiles(directory=logos_dir), name="logos")

    # webui 下其余一级目录（emoji/ 等）自动挂到同名路径，
    # 免得每加一个 public 子目录就要回来改代码
    reserved = {"assets", "logos", "index.html"}
    for entry in sorted(os.listdir(webui_dir)):
        full = os.path.join(webui_dir, entry)
        if entry in reserved or not os.path.isdir(full):
            continue
        if any(getattr(r, "path", None) == f"/{entry}" for r in app.routes):
            continue
        app.mount(f"/{entry}", StaticFiles(directory=full), name=f"webui-{entry}")

    app.mount("/static", StaticFiles(directory=webui_dir), name="webui-static")
    return True

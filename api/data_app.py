# -*- coding: utf-8 -*-
"""data 服务（独立应用）：数据增删改查 + 配置 + S3 备份 + 菜谱域，
同时充当**网关**——把 `/crawler/**` 按服务名代理到 crawler 服务。

对外路径约定：
    /data/api/**     本服务自己的接口（故事/评论/媒体/导出/标签/S3/菜谱）
    /crawler/api/**  经 Nacos 发现转发给 crawler 服务（见 api/gateway.py）
    /                前端静态页（webui 构建产物）
    /data/health     健康检查

启动命令：
    uvicorn api.data_app:app --host 0.0.0.0 --port 8081
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from .gateway import router as gateway_router
from .routers import (app_config_router, data_router, discover_router,
                      export_router, media_router, recipe_router,
                      s3_backup_router, stance_tags_router, story_router)
from .routers.internal import router as internal_router
from .services import boot_tasks, discovery
from .static_mounts import mount_webui

SERVICE_PREFIX = "/data"
API_PREFIX = f"{SERVICE_PREFIX}/api"

WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")

app = FastAPI(
    title="MediaCrawler · Data Service",
    description="数据增删改查服务（独立部署，同时承担按服务名路由的网关）",
    version="1.0.0",
    docs_url=f"{SERVICE_PREFIX}/docs",
    openapi_url=f"{SERVICE_PREFIX}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 本服务业务路由：/data/api/** ----
app.include_router(story_router, prefix=API_PREFIX)
app.include_router(data_router, prefix=API_PREFIX)
app.include_router(discover_router, prefix=API_PREFIX)
app.include_router(export_router, prefix=API_PREFIX)
app.include_router(stance_tags_router, prefix=API_PREFIX)
app.include_router(s3_backup_router, prefix=API_PREFIX)
app.include_router(recipe_router, prefix=API_PREFIX)
app.include_router(media_router, prefix=API_PREFIX)
app.include_router(app_config_router, prefix=API_PREFIX)
app.include_router(internal_router, prefix=API_PREFIX)


@app.get(f"{SERVICE_PREFIX}/health", tags=["health"])
@app.get("/health", tags=["health"])
async def health():
    return {
        "service": getattr(config, "DATA_SERVICE_NAME", "mediacrawler-data"),
        "role": "data+gateway",
        "status": "ok",
        "version": "1.0.0",
        "gateway_enabled": bool(getattr(config, "GATEWAY_ENABLED", True)),
        "discovery": discovery.status(),
    }


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend():
    """前端首页（同时支持 HEAD，便于健康检查/预览探测）。"""
    from fastapi.responses import FileResponse

    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": "MediaCrawler · Data Service",
        "version": "1.0.0",
        "docs": f"{SERVICE_PREFIX}/docs",
        "note": "WebUI 未构建：cd webui && npm run build",
    }


# ---- 网关：按 URL 里的服务名转发（必须在本服务路由之后、静态挂载之前）----
if getattr(config, "GATEWAY_ENABLED", True):
    app.include_router(gateway_router)

# ---- 静态资源必须最后挂 ----
mount_webui(app, WEBUI_DIR)


@app.on_event("startup")
async def _on_startup() -> None:
    boot_tasks.install_loop_exception_handler()
    boot_tasks.start_storage_warmup()
    discovery.register_self()
    boot_tasks.start_stance_backfill()
    boot_tasks.start_stance_library()
    boot_tasks.start_s3_backup()
    boot_tasks.start_recipe_boot()
    print(f"[data] 服务已启动：{API_PREFIX}/**  pid={os.getpid()} "
          f"gateway={bool(getattr(config, 'GATEWAY_ENABLED', True))}", flush=True)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    discovery.shutdown()

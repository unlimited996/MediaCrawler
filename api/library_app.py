# -*- coding: utf-8 -*-
"""library 服务（独立应用）：**内容库** —— 抓回来的帖子/评论/媒体/标签/菜谱
的查询与增删改查，外加配置、S3 备份、前端静态页，并兼任**网关**。

（原来叫 data 服务，名字太笼统：`data` 只说"数据"，看不出是哪类数据。
  这里装的是「抓取产出的内容资产」：笔记/评论/图片视频/标签词库/菜谱。）

对外路径约定：
    /library/api/**   本服务自己的接口（stories/assets/export/stance-tags/s3-backup/recipes）
    /crawler/api/**   经 Nacos 发现转发给 crawler 服务（见 api/gateway.py）
    /                 前端静态页（webui 构建产物）
    /library/health   健康检查

启动命令：
    uvicorn api.library_app:app --host 0.0.0.0 --port 8081
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from .gateway import router as gateway_router
from .routers import (accounts_router, app_config_router, config_router,
                      emoji_assets_router,
                      data_router, discover_router, env_router, export_router,
                      media_router, recipe_router, s3_backup_router,
                      scheduler_router, stance_tags_router, story_router,
                      websocket_router)
from .routers.internal import router as internal_router
from .services import boot_tasks, discovery
from .static_mounts import mount_webui

#: 服务名（也是 URL 前缀）和 API 前缀
SERVICE_PREFIX = "/library"
API_PREFIX = f"{SERVICE_PREFIX}/api"

WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")

app = FastAPI(
    title="MediaCrawler · Library Service",
    description="内容库服务：抓取产出的内容资产查询与增删改查"
                "（独立部署，同时承担按服务名路由的网关）",
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

# ---- 本服务业务路由：/library/api/** ----
app.include_router(story_router, prefix=API_PREFIX)
app.include_router(data_router, prefix=API_PREFIX)
app.include_router(discover_router, prefix=API_PREFIX)
app.include_router(export_router, prefix=API_PREFIX)
app.include_router(stance_tags_router, prefix=API_PREFIX)
app.include_router(s3_backup_router, prefix=API_PREFIX)
app.include_router(emoji_assets_router, prefix=API_PREFIX)
app.include_router(recipe_router, prefix=API_PREFIX)
app.include_router(media_router, prefix=API_PREFIX)
app.include_router(app_config_router, prefix=API_PREFIX)
app.include_router(internal_router, prefix=API_PREFIX)

# ---- 管理类接口（配置 / 定时任务 / 账号池）----
# 这三块都是**数据库 + 共享 logs 目录**驱动，不依赖爬虫进程：
#   * 配置读写        -> crawl_config_item 表
#   * 定时任务 CRUD   -> scheduled_task 表；「立即执行」只是往 scheduled_task_run
#                        写一条 queued，由 crawler 侧的 worker 拾取执行（本来就
#                        是这么设计的，本进程根本不跑调度）
#   * 账号池          -> 账号表（Cookie 加密存储）
#   * 停止抓取        -> 往挂载的 ./logs 写 .stop_crawl 信号，crawler 容器能看到
# 因此挂在 library 上也是语义正确的，好处是：日常改配置/建任务只用到 library，
# crawler 可以长期不更新、不重建（前端也不必经网关多一跳）。
app.include_router(scheduler_router, prefix=API_PREFIX)
app.include_router(config_router, prefix=API_PREFIX)
app.include_router(accounts_router, prefix=API_PREFIX)
# 实时日志/统计：日志桥 tail 的是挂载进来的 ./logs/scheduler_live.jsonl，
# 两个容器看到的是同一个文件，所以 library 也能提供同一条 WS 通道。
app.include_router(websocket_router, prefix=API_PREFIX)
app.include_router(env_router, prefix=API_PREFIX)


@app.get(f"{SERVICE_PREFIX}/health", tags=["health"])
@app.get("/health", tags=["health"])
async def health():
    return {
        "service": getattr(config, "LIBRARY_SERVICE_NAME", "mediacrawler-library"),
        "role": "library+gateway",
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
        "message": "MediaCrawler · Library Service",
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
    boot_tasks.start_emoji_library()
    boot_tasks.start_recipe_boot()
    # 实时日志桥：tail 共享的 logs/scheduler_live.jsonl 并广播给 WS 客户端。
    # 不启动的话前端连上 /library/api/ws/logs 只会收到历史补发、没有实时日志。
    try:
        from .routers.websocket import start_broadcaster
        start_broadcaster()
    except Exception as e:
        print(f"[library] 日志桥启动失败（不影响接口）：{e}", flush=True)
    print(f"[library] 服务已启动：{API_PREFIX}/**  pid={os.getpid()} "
          f"gateway={bool(getattr(config, 'GATEWAY_ENABLED', True))}", flush=True)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    discovery.shutdown()

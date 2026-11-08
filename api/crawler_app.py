# -*- coding: utf-8 -*-
"""crawler 服务（独立应用）：抓取控制 + 定时调度 + 账号池 + 抓取配置。

对外路径约定：`/crawler/api/**`（URL 里的 `crawler` 就是服务名）
    /crawler/api/crawler/**     手动抓取
    /crawler/api/scheduler/**   定时任务
    /crawler/api/accounts/**    账号池
    /crawler/api/config/**      抓取配置（crawl_config_item）
    /crawler/api/ws/logs        WebSocket 实时日志
    /crawler/health             健康检查（含下游/注册状态）

启动命令：
    uvicorn api.crawler_app:app --host 0.0.0.0 --port 8001
（抓取执行器 worker 是另一个进程：`python -m worker`，可同镜像不同命令启动）
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from .routers import (accounts_router, config_router, crawler_router, env_router,
                      login_session_router, scheduler_router, websocket_router)
from .services import boot_tasks, discovery

#: 服务名（也是 URL 前缀）和 API 前缀
SERVICE_PREFIX = "/crawler"
API_PREFIX = f"{SERVICE_PREFIX}/api"

app = FastAPI(
    title="MediaCrawler · Crawler Service",
    description="抓取与调度服务（独立部署）",
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

# ---- 路由：全部挂在 /crawler/api 下 ----
app.include_router(crawler_router, prefix=API_PREFIX)
app.include_router(scheduler_router, prefix=API_PREFIX)
app.include_router(accounts_router, prefix=API_PREFIX)
app.include_router(config_router, prefix=API_PREFIX)
app.include_router(websocket_router, prefix=API_PREFIX)
app.include_router(env_router, prefix=API_PREFIX)
# WebUI 原版登录页会话（截图 + 坐标回传，登录态写进共享 browser_data 卷）
app.include_router(login_session_router, prefix=API_PREFIX)


@app.get(f"{SERVICE_PREFIX}/health", tags=["health"])
@app.get("/health", tags=["health"])
async def health():
    """健康检查：DB / 注册状态 / 执行器（worker）存活。"""
    from .services.worker_control import worker_info

    worker = worker_info()
    return {
        "service": getattr(config, "CRAWLER_SERVICE_NAME", "mediacrawler-crawler"),
        "role": "crawler",
        "status": "ok",
        "version": "1.0.0",
        "worker_alive": bool(worker.get("alive")),
        "worker": worker,
        "discovery": discovery.status(),
    }


@app.get(f"{SERVICE_PREFIX}/api/service-info", tags=["health"])
async def service_info():
    """给网关/运维看的自述信息。"""
    return {
        "service": getattr(config, "CRAWLER_SERVICE_NAME", "mediacrawler-crawler"),
        "prefix": API_PREFIX,
        "routes": ["crawler", "scheduler", "accounts", "config", "ws"],
        "port": int(getattr(config, "SERVICE_PORT", 8001)),
    }


@app.on_event("startup")
async def _on_startup() -> None:
    boot_tasks.install_loop_exception_handler()
    # 注册到 Nacos（失败不影响启动）
    discovery.register_self()
    # 抓取运行时：默认交给独立 worker；SCHEDULER_IN_API=1 时在本进程内跑
    boot_tasks.start_crawler_runtime()
    # 实时日志桥：服务一启动就增量 tail worker 的 scheduler_live.jsonl，
    # 不必等第一个 WS 连上来才启动（WS 连接时 start_broadcaster 也是幂等的）。
    try:
        from .routers.websocket import start_broadcaster

        start_broadcaster()
    except Exception as e:
        print(f"[crawler] 日志桥启动失败（不影响服务）：{e}", flush=True)
    print(f"[crawler] 服务已启动：{SERVICE_PREFIX}/api/**  "
          f"worker={os.getpid()}", flush=True)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    discovery.shutdown()

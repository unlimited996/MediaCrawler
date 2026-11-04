# -*- coding: utf-8 -*-
"""按「URL 里的服务名」路由的反向代理（网关）。

规则（路径的第一段就是服务名）：

    /crawler/api/**  -> crawler 服务（Nacos 里注册的 mediacrawler-crawler）
    /data/api/**     -> 本进程（data 服务自己处理，不走代理）
    /crawler/api/ws/logs、/crawler/api/ws/status -> WebSocket 代理

实例从 Nacos 发现；Nacos 不可用时回退 `CRAWLER_BASE_URL`（默认 127.0.0.1:8001）。
所以同一个 URL 在「单机双进程」和「容器 + Nacos」下都能用。
"""
from __future__ import annotations

import asyncio
import json
from typing import Iterable

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.responses import Response

import config
from .services import discovery

router = APIRouter(tags=["gateway"])

#: 逐跳首部（不能透传，否则上游/下游连接会错乱）
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "accept-encoding",
}

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def _crawler_service() -> str:
    return getattr(config, "CRAWLER_SERVICE_NAME", "mediacrawler-crawler")


def _filtered_headers(headers: Iterable) -> dict:
    return {k: v for k, v in headers if k.lower() not in _HOP_HEADERS}


@router.api_route("/crawler/{path:path}", methods=_METHODS, include_in_schema=False)
async def proxy_crawler(request: Request, path: str):
    """把 /crawler/** 转发到 crawler 服务实例。"""
    base = discovery.pick(_crawler_service())
    if not base:
        return Response(
            content=json.dumps({"detail": "crawler 服务不可用（Nacos 未发现实例，"
                                          "且未配置 CRAWLER_BASE_URL）"},
                               ensure_ascii=False).encode("utf-8"),
            status_code=503, media_type="application/json")

    url = f"{base.rstrip('/')}/crawler/{path}"
    body = await request.body()
    timeout = float(getattr(config, "SERVICE_CALL_TIMEOUT", 30))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            upstream = await client.request(
                request.method, url,
                params=dict(request.query_params),
                content=body or None,
                headers=_filtered_headers(request.headers.items()),
            )
    except httpx.HTTPError as e:
        return Response(
            content=json.dumps(
                {"detail": f"crawler 服务调用失败：{type(e).__name__}"},
                ensure_ascii=False).encode("utf-8"),
            status_code=502, media_type="application/json")

    resp_headers = _filtered_headers(upstream.headers.items())
    resp_headers.pop("content-encoding", None)
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=resp_headers,
                    media_type=upstream.headers.get("content-type"))


async def _proxy_ws(ws: WebSocket, upstream_path: str) -> None:
    """WebSocket 双向透传（日志流用）。"""
    await ws.accept()
    base = discovery.pick(_crawler_service())
    if not base:
        try:
            await ws.send_json({"type": "error",
                                "message": "crawler 服务不可用（Nacos 未发现实例）"})
            await ws.close(code=1011)
        except Exception:
            pass
        return

    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") \
        + upstream_path
    try:
        import websockets

        async with websockets.connect(ws_url, open_timeout=10) as upstream:
            async def client_to_upstream():
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_client():
                async for m in upstream:
                    if isinstance(m, bytes):
                        await ws.send_bytes(m)
                    else:
                        await ws.send_text(m)

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": f"上游 WS 断开：{e}"})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@router.websocket("/crawler/api/ws/logs")
async def proxy_ws_logs(ws: WebSocket):
    await _proxy_ws(ws, "/crawler/api/ws/logs")


@router.websocket("/crawler/api/ws/status")
async def proxy_ws_status(ws: WebSocket):
    await _proxy_ws(ws, "/crawler/api/ws/status")


@router.get("/gateway/status", include_in_schema=False)
async def gateway_status():
    """网关自身状态：谁能被路由到、Nacos 是否健康。"""
    svc = _crawler_service()
    instances = discovery.discover(svc)
    return {
        "gateway": "ok",
        "routes": {
            "/crawler/**": {"service": svc, "instances": instances},
            "/data/**": {"service": getattr(config, "DATA_SERVICE_NAME",
                                           "mediacrawler-data"),
                         "instances": ["self"]},
        },
        "discovery": discovery.status(),
    }

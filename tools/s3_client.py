# -*- coding: utf-8 -*-
"""S3 兼容对象存储客户端（boto3 异步封装）。

适用 Garage / MinIO / AWS S3：SigV4 + path-style 寻址。
两个本机特有的兜底：
1. endpoint 域名解析抖动（joyeuse.cn 时好时坏）——先按原域名建 client，
   连接失败时用 `socket.gethostbyname` 解析出的 IP 重试一次；
2. boto3 全是阻塞调用——统一走 `run_in_executor`，不卡事件循环。

client 按凭据维度缓存，改配置（新密钥/新 endpoint）自然换 key，不读旧缓存。
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlsplit


@dataclass(frozen=True)
class S3Settings:
    """一次备份任务生效的连接参数（由 S3Config 行解密后构造）。"""

    endpoint: str
    access_key_id: str
    secret_access_key: str
    region: str = "garage"
    bucket: str = "photos"
    addressing_style: str = "path"

    @property
    def cache_key(self) -> str:
        return "|".join([self.endpoint, self.access_key_id,
                         self.secret_access_key[:8], self.region,
                         self.addressing_style])


#: client 缓存：key = S3Settings.cache_key。线程内复用，避免反复建 session。
_CLIENTS: Dict[str, Any] = {}


def _ip_endpoint(endpoint: str) -> Optional[str]:
    """把 endpoint 的域名换成 IP；解析失败返回 None（沿用原值）。"""
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        if not host:
            return None
        ip = socket.gethostbyname(host)
        if not ip or ip == host:
            return None
        netloc = f"{ip}:{parts.port}" if parts.port else ip
        return f"{parts.scheme}://{netloc}"
    except Exception:
        return None


def _make_client(settings: S3Settings, endpoint: Optional[str] = None):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint or settings.endpoint,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name=settings.region or "us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.addressing_style or "path"},
            connect_timeout=10,
            read_timeout=180,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


async def get_client(settings: S3Settings, allow_ip_fallback: bool = True):
    """取（并缓存）boto3 client。DNS 抖动时返回 IP 版 endpoint 的 client。"""
    key = settings.cache_key + ("|ip" if allow_ip_fallback else "")
    client = _CLIENTS.get(key)
    if client is not None:
        return client
    loop = asyncio.get_running_loop()
    client = await loop.run_in_executor(None, _make_client, settings)
    _CLIENTS[key] = client
    return client


async def call(settings: S3Settings, op: str, **kwargs):
    """执行一次 S3 操作（阻塞调用走线程池）。

    连接类错误自动用 IP endpoint 重试一次——域名解析瞬时失败时
    botocore 抛 EndpointConnectionError，换 IP 直连通常立刻恢复。
    """
    loop = asyncio.get_running_loop()
    try:
        client = await get_client(settings)
        fn = getattr(client, op)
        return await loop.run_in_executor(None, lambda: fn(**kwargs))
    except Exception as e:
        if not _is_connection_error(e):
            raise
        ip_ep = _ip_endpoint(settings.endpoint)
        if not ip_ep:
            raise
        client = await get_client(settings, allow_ip_fallback=True)
        # get_client 缓存键已含 ip 标记，这里显式换 endpoint 再建一个
        client = await loop.run_in_executor(
            None, lambda: _make_client(settings, endpoint=ip_ep))
        fn = getattr(client, op)
        return await loop.run_in_executor(None, lambda: fn(**kwargs))


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in ("EndpointConnectionError", "ConnectTimeoutError",
                "ConnectionError", "ConnectionClosedError"):
        return True
    text = str(exc).lower()
    return "failed to connect" in text or "connection" in text and "refused" in text


def invalidate_cache() -> None:
    """配置变更后清掉 client 缓存（密钥/endpoint 换了必须重建）。"""
    _CLIENTS.clear()


# ---------------------------------------------------------------------------
# 高层操作：全部 async，内部已处理线程池与 IP 兜底
# ---------------------------------------------------------------------------

async def test_connection(settings: S3Settings) -> Dict[str, Any]:
    """连通性测试：head_bucket，失败退回 list_objects_v2(MaxKeys=1)。"""
    import time

    started = time.monotonic()
    try:
        await call(settings, "head_bucket", Bucket=settings.bucket)
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as e1:
        try:
            await call(settings, "list_objects_v2", Bucket=settings.bucket, MaxKeys=1)
            return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000),
                    "note": "head_bucket 不可用，list 正常"}
        except Exception as e2:
            return {"ok": False, "error": f"{type(e2).__name__}: {e2 or e1}"}


async def upload_file(settings: S3Settings, local_path: str, key: str,
                      content_type: Optional[str] = None) -> Dict[str, Any]:
    """上传本地文件。返回 {etag, size}。

    直接读字节走 put_object：备份的文件以图片/视频为主，单文件远小于
    分片阈值（大文件场景以后要升级再换 multipart），少一套代码路径。
    """
    import os

    size = os.path.getsize(local_path)
    with open(local_path, "rb") as f:
        data = f.read()
    resp = await call(settings, "put_object", Bucket=settings.bucket, Key=key,
                      Body=data, ContentType=content_type or _guess_type(key))
    return {"etag": (resp.get("ETag") or "").strip('"'), "size": size}


async def upload_bytes(settings: S3Settings, data: bytes, key: str,
                       content_type: Optional[str] = None) -> Dict[str, Any]:
    """上传内存字节（远端直取场景）。"""
    resp = await call(settings, "put_object", Bucket=settings.bucket, Key=key,
                      Body=data, ContentType=content_type or _guess_type(key))
    return {"etag": (resp.get("ETag") or "").strip('"'), "size": len(data)}


async def head_object(settings: S3Settings, key: str) -> Optional[Dict[str, Any]]:
    """对象元信息；不存在返回 None。"""
    try:
        return await call(settings, "head_object", Bucket=settings.bucket, Key=key)
    except Exception as e:
        if type(e).__name__ in ("NoSuchKey", "404", "ClientError"):
            code = ""
            try:
                code = str(e.response.get("Error", {}).get("Code", ""))
            except Exception:
                pass
            if code in ("404", "NoSuchKey"):
                return None
        raise


async def get_object(settings: S3Settings, key: str) -> Optional[bytes]:
    """取对象字节；不存在返回 None。"""
    try:
        resp = await call(settings, "get_object", Bucket=settings.bucket, Key=key)
    except Exception as e:
        code = ""
        try:
            code = str(e.response.get("Error", {}).get("Code", ""))
        except Exception:
            pass
        if code in ("404", "NoSuchKey"):
            return None
        raise
    body = resp.get("Body")
    return body.read() if body is not None else None


async def delete_object(settings: S3Settings, key: str) -> bool:
    await call(settings, "delete_object", Bucket=settings.bucket, Key=key)
    return True


async def copy_object(settings: S3Settings, src_key: str, dest_key: str) -> bool:
    """服务端拷贝对象（不下载字节）；Garage / MinIO / AWS 通用。"""
    await call(settings, "copy_object", Bucket=settings.bucket, Key=dest_key,
              CopySource={"Bucket": settings.bucket, "Key": src_key})
    return True


def _guess_type(key: str) -> str:
    import mimetypes

    return mimetypes.guess_type(key)[0] or "application/octet-stream"

# -*- coding: utf-8 -*-
"""视频转储接口回归：新增 / 查询 / 删除 / Range 播放 / 原始文件大小。

用内存 SQLite + mock S3，不触碰线上库；覆盖「现有接口风格与错误处理」：
422 参数、404 不存在、503 配置库不可用、502 转储失败。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from starlette.requests import Request

from database.models_v2 import BaseV2, VideoAsset
from api.routers import videos


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):
    return "INTEGER"


class FakeBody:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    # 必须用文件而不是 :memory: —— 接口 finally 会 dispose engine，
    # 内存库在新连接上会变成空库；文件库 dispose 后数据仍在。
    dbfile = str(tmp_path / "v.db").replace("\\", "/")
    engine = create_async_engine(f"sqlite+aiosqlite:///{dbfile}")
    async with engine.begin() as conn:
        await conn.run_sync(BaseV2.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        return engine, Session()

    monkeypatch.setattr(videos, "_session", _session)
    yield engine, Session


def _request(rng: str | None = None) -> Request:
    headers = [(b"host", b"test")]
    if rng:
        headers.append((b"range", rng.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/", "headers": headers,
        "query_string": b"", "scheme": "http", "server": ("test", 80),
        "client": ("test", 1234), "root_path": "",
    })


@pytest.mark.asyncio
async def test_create_returns_original_size(db, monkeypatch):
    engine, Session = db

    async def fake_settings(session, bucket=None):
        return SimpleNamespace(bucket="videos"), 1

    async def fake_target(session):
        return SimpleNamespace(bucket="videos"), ""

    async def fake_archive(session, row, settings, key_prefix=""):
        row.status = "uploaded"
        row.object_key = "xhs/videos/abc/a.mp4"
        row.original_size_bytes = 12_345_678
        row.stored_size_bytes = 9_876_543
        row.codec_v, row.codec_a = "h264", "aac"
        await session.commit()
        return {"ok": True, "id": row.id,
                "original_size_bytes": 12_345_678}

    monkeypatch.setattr(videos.store, "video_target", fake_target)
    monkeypatch.setattr(videos.store, "archive_video", fake_archive)

    out = await videos.create_video(videos.VideoCreate(
        content_id="note1", source_url="https://cdn/x.mp4", story_id=7))
    assert out["original_size_bytes"] == 12_345_678
    assert out["stored_size_bytes"] == 9_876_543
    assert out["object_url"].endswith(f"/videos/{out['id']}/stream")

    listed = await videos.list_videos(page=1, page_size=20)
    assert listed["total"] == 1
    assert listed["items"][0]["original_size_bytes"] == 12_345_678

    one = await videos.get_video(out["id"])
    assert one["original_size_bytes"] == 12_345_678
    await engine.dispose()


@pytest.mark.asyncio
async def test_validation_and_404(db):
    engine, _ = db
    with pytest.raises(HTTPException) as e:
        await videos.create_video(videos.VideoCreate(content_id="", source_url=""))
    assert e.value.status_code == 422

    with pytest.raises(HTTPException) as e:
        await videos.get_video(999)
    assert e.value.status_code == 404

    with pytest.raises(HTTPException) as e:
        await videos.delete_video(999)
    assert e.value.status_code == 404
    await engine.dispose()


@pytest.mark.asyncio
async def test_archive_failure_maps_to_502(db, monkeypatch):
    engine, _ = db

    async def fake_settings(session, bucket=None):
        return SimpleNamespace(bucket="videos"), 1

    async def fake_target(session):
        return SimpleNamespace(bucket="videos"), ""

    async def fake_archive(session, row, settings, key_prefix=""):
        return {"ok": False, "error": "候选地址全部失败"}

    monkeypatch.setattr(videos.store, "video_target", fake_target)
    monkeypatch.setattr(videos.store, "archive_video", fake_archive)

    with pytest.raises(HTTPException) as e:
        await videos.create_video(videos.VideoCreate(
            content_id="note2", source_url="https://cdn/x.mp4"))
    assert e.value.status_code == 502
    assert "候选地址" in e.value.detail
    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_removes_record(db, monkeypatch):
    engine, Session = db
    async with Session() as s:
        s.add(VideoAsset(platform="xhs", content_id="n3",
                         source_url="https://cdn/x.mp4", status="pending"))
        await s.commit()
        vid = (await s.execute(select(VideoAsset.id))).scalar()

    async def fake_settings(session, bucket=None):
        return SimpleNamespace(bucket="videos"), 1

    monkeypatch.setattr(videos.store, "video_settings", fake_settings)
    # 无 object_key：不会真的调用 S3，只验证记录被删
    out = await videos.delete_video(vid)
    assert out["success"] is True
    async with Session() as s:
        assert (await s.execute(select(VideoAsset).where(
            VideoAsset.id == vid))).scalar_one_or_none() is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_stream_supports_range_206(db, monkeypatch):
    import tools.s3_client as s3

    # 强制走容器内字节代理，验证 fallback 路径仍支持 Range/206
    monkeypatch.setenv("VIDEO_STREAM_MODE", "proxy")
    engine, Session = db
    async with Session() as s:
        row = VideoAsset(platform="xhs", content_id="n4",
                         source_url="https://cdn/x.mp4", status="uploaded",
                         object_key="xhs/videos/n4/a.mp4", bucket="videos")
        s.add(row)
        await s.commit()
        vid = row.id

    async def fake_settings(session, bucket=None):
        return SimpleNamespace(bucket="videos"), 1

    async def fake_call(settings, op, **kwargs):
        assert op == "get_object"
        assert kwargs["Bucket"] == "videos"
        assert kwargs["Range"] == "bytes=0-1023"
        return {"Body": FakeBody(b"0123456789"),
                "ContentType": "video/mp4",
                "ContentRange": "bytes 0-9/100",
                "ContentLength": 10}

    monkeypatch.setattr(videos.store, "video_settings", fake_settings)
    monkeypatch.setattr(s3, "call", fake_call)

    resp = await videos.stream_video(vid, _request("bytes=0-1023"))
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-9/100"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.media_type == "video/mp4"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stream_redirects_to_presigned_url(db, monkeypatch):
    """默认路径：307 到 S3 预签名 URL，浏览器直连避免容器内代理卡死。"""
    import tools.s3_client as s3

    class FakeClient:
        def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
            assert op == "get_object"
            assert Params == {"Bucket": "videos", "Key": "xhs/videos/n5/a.mp4"}
            assert ExpiresIn == 3600
            return "http://s3.example/videos/xhs/videos/n5/a.mp4?X-Amz-Signature=abc"

    async def fake_settings(session, bucket=None):
        return SimpleNamespace(bucket="videos"), 1

    async def fake_get_client(settings, allow_ip_fallback=True):
        return FakeClient()

    monkeypatch.setenv("VIDEO_STREAM_MODE", "presign")
    monkeypatch.setattr(videos.store, "video_settings", fake_settings)
    monkeypatch.setattr(s3, "get_client", fake_get_client)

    engine, Session = db
    async with Session() as s:
        row = VideoAsset(platform="xhs", content_id="n5",
                         source_url="https://cdn/x.mp4", status="uploaded",
                         object_key="xhs/videos/n5/a.mp4", bucket="videos")
        s.add(row)
        await s.commit()
        vid = row.id

    resp = await videos.stream_video(vid, _request("bytes=0-1023"))
    assert resp.status_code == 307
    assert "X-Amz-Signature=abc" in resp.headers["location"]
    assert resp.headers["cache-control"] == "no-store"
    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_self_heals(db):
    from tools import video_store

    engine, Session = db
    async with Session() as s:
        a = await video_store.enqueue_video(
            s, platform="xhs", content_id="n5",
            source_url="https://cdn/a.mp4", backup_urls=["https://cdn/b.mp4"])
        b = await video_store.enqueue_video(
            s, platform="xhs", content_id="n5",
            source_url="https://cdn/a.mp4")
        await s.commit()
        assert a == b
        row = (await s.execute(select(VideoAsset))).scalar_one()
        assert row.backup_urls == ["https://cdn/b.mp4"]
        row.status = "failed"
        row.retry_count = 3
        await s.commit()
        await video_store.enqueue_video(
            s, platform="xhs", content_id="n5", source_url="https://cdn/a.mp4")
        await s.commit()
        await s.refresh(row)
        assert row.status == "pending" and row.retry_count == 0
        assert video_store.video_fields(row)["original_size_bytes"] is None
    await engine.dispose()


def _req(host: str):
    return SimpleNamespace(headers={"host": host})


def _s3(endpoint: str):
    from tools.s3_client import S3Settings
    return S3Settings(endpoint=endpoint, access_key_id="GK1",
                      secret_access_key="x" * 20, bucket="videos")


def test_presign_picks_endpoint_by_request_host(monkeypatch):
    """内网访客拿内网 S3 地址，外网访客拿公网地址（否则一边必然播不了）。"""
    monkeypatch.delenv("S3_PRESIGN_FORCE", raising=False)
    pub = _s3("http://www.joyeuse.cn:3900")
    lan = _s3("http://192.168.10.38:3900")

    assert videos.presign_settings_for(_req("192.168.10.38:18081"), pub, lan) is lan
    assert videos.presign_settings_for(_req("127.0.0.1:18081"), pub, lan) is lan
    assert videos.presign_settings_for(_req("www.joyeuse.cn:18081"), pub, lan) is pub
    # 没有配置内网 endpoint 时永远用公网
    assert videos.presign_settings_for(_req("192.168.10.38:18081"), pub, pub) is pub


def test_presign_force_override(monkeypatch):
    pub = _s3("http://www.joyeuse.cn:3900")
    lan = _s3("http://192.168.10.38:3900")
    monkeypatch.setenv("S3_PRESIGN_FORCE", "public")
    assert videos.presign_settings_for(_req("192.168.1.5:18081"), pub, lan) is pub
    monkeypatch.setenv("S3_PRESIGN_FORCE", "internal")
    assert videos.presign_settings_for(_req("www.joyeuse.cn"), pub, lan) is lan


def test_presign_rejects_container_name(monkeypatch):
    """S3_INTERNAL_ENDPOINT 配成容器名时，浏览器解析不了，必须回退公网。"""
    monkeypatch.delenv("S3_PRESIGN_FORCE", raising=False)
    pub = _s3("http://www.joyeuse.cn:3900")
    lan = _s3("http://dxflrs-garage:3900")   # 无点 = 容器名
    assert videos.presign_settings_for(_req("192.168.10.38:18081"), pub, lan) is pub

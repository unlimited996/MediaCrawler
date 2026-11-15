# -*- coding: utf-8 -*-
"""XHS 视频转储链路回归。

覆盖线上真实踩过的两个坑：
1. SSR（__INITIAL_STATE__）里的视频分档是 camelCase（masterUrl/backupUrls），
   只认 snake_case 会让免登录兜底抓到的视频静默解析成 0 条；
2. 存量视频帖抓取时没开转储开关，库里没有视频记录，需要能按 story 补建登记。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from database.models_v2 import BaseV2, SourceSite, Story, StoryChapter, VideoAsset
from media_platform.xhs.media import extract_video_urls
from tools import video_store


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):
    return "INTEGER"


# ---------------------------------------------------------------------------
# 1. 视频直链提取：两种命名风格都要认
# ---------------------------------------------------------------------------

SSR_VIDEO = {
    "media": {
        "stream": {
            "h264": [{
                "masterUrl": "http://cdn/ssr_720.mp4?sign=a",
                "backupUrls": ["http://bak/ssr_720.mp4"],
                "height": 720,
                "avgBitrate": 1_900_000,
            }],
            "h265": [],
        }
    }
}

API_VIDEO = {
    "media": {
        "stream": {
            "h264": [{
                "master_url": "http://cdn/api_720.mp4?sign=b",
                "backup_urls": ["http://bak/api_720.mp4"],
                "height": 720,
                "avg_bitrate": 1_900_000,
            }],
        }
    }
}


def test_extract_camel_case_from_ssr():
    urls = extract_video_urls({"type": "video", "video": SSR_VIDEO})
    assert urls, "SSR 驼峰字段必须能解析出直链"
    assert urls[0].startswith("http://cdn/ssr_720.mp4")
    assert "http://bak/ssr_720.mp4" in urls


def test_extract_snake_case_from_api():
    urls = extract_video_urls({"type": "video", "video": API_VIDEO})
    assert urls[0].startswith("http://cdn/api_720.mp4")
    assert "http://bak/api_720.mp4" in urls


def test_extract_ignores_image_notes():
    assert extract_video_urls({"type": "normal", "video": SSR_VIDEO}) == []


# ---------------------------------------------------------------------------
# 2. 按 story 补建视频登记
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    dbfile = str(tmp_path / "v.db").replace("\\", "/")
    engine = create_async_engine(f"sqlite+aiosqlite:///{dbfile}")
    async with engine.begin() as conn:
        await conn.run_sync(BaseV2.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, Session
    await engine.dispose()


async def _seed_story(Session, *, ext: dict, content_id: str = "note-x") -> int:
    async with Session() as s:
        # SMALLINT 主键在 SQLite 里不自增，显式给 id
        site = SourceSite(id=1, code="xhs", name="小红书")
        s.add(site)
        await s.flush()
        story = Story(title="t", dedupe_key=f"xhs:{content_id}",
                      primary_site_id=site.id, ext={})
        s.add(story)
        await s.flush()
        s.add(StoryChapter(
            story_id=story.id, source_site_id=site.id,
            source_content_id=content_id, seq_no=1,
            source_url=f"https://www.xiaohongshu.com/explore/{content_id}",
            ext=ext,
        ))
        await s.commit()
        return story.id


@pytest.mark.asyncio
async def test_backfill_uses_stored_video_url(db):
    _, Session = db
    story_id = await _seed_story(Session, ext={
        "type": "video",
        "video_url": "http://cdn/a.mp4, http://cdn/b.mp4",
    })
    async with Session() as s:
        out = await video_store.backfill_story(s, story_id)
        assert out["ok"], out
        row = (await s.execute(
            select(VideoAsset).where(VideoAsset.id == out["id"]))).scalar_one()
        assert row.status == "pending"
        assert row.story_id == story_id
        assert row.content_id == "note-x"
        assert row.source_url == "http://cdn/a.mp4"
        assert row.backup_urls == ["http://cdn/b.mp4"]


@pytest.mark.asyncio
async def test_backfill_rejects_non_video(db):
    _, Session = db
    story_id = await _seed_story(Session, ext={"type": "normal"})
    async with Session() as s:
        out = await video_store.backfill_story(s, story_id)
        assert not out["ok"]
        assert "不是视频帖" in out["error"]


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db):
    _, Session = db
    story_id = await _seed_story(Session, ext={
        "type": "video", "video_url": "http://cdn/a.mp4"})
    async with Session() as s:
        a = await video_store.backfill_story(s, story_id)
        b = await video_store.backfill_story(s, story_id)
        assert a["id"] == b["id"]
        cnt = (await s.execute(select(VideoAsset))).scalars().all()
        assert len(cnt) == 1


# ---------------------------------------------------------------------------
# 3. 抓取侧登记钩子：开关打开才登记（线上就是这里没开，视频压根没入库）
# ---------------------------------------------------------------------------

def _video_item(content_id: str) -> dict:
    return {
        "note_id": content_id,
        "type": "video",
        "title": "姥姥版白炖小鸡土豆",
        "desc": "能喝汤能吃肉还能拌饭",
        "video_url": "http://cdn/a.mp4,http://cdn/b.mp4",
        "time": 1794620362,
        "creator_hash": "hash-x",
        "nickname": "测试",
        "liked_count": "12",
        "comment_count": "3",
        "note_url": f"https://www.xiaohongshu.com/explore/{content_id}",
        "xsec_token": "tok",
    }


async def _run_hook(monkeypatch, Session, item) -> None:
    from contextlib import asynccontextmanager

    from tools import v2_sync

    @asynccontextmanager
    async def fake_session():
        s = Session()
        try:
            yield s
        finally:
            await s.close()

    async def fake_site(session, code):
        return 1

    monkeypatch.setattr(v2_sync, "_v2_session", fake_session)
    monkeypatch.setattr(v2_sync, "_get_site_id", fake_site)
    assert await v2_sync.sync_content_to_v2("xhs", item) is True


@pytest.mark.asyncio
async def test_hook_registers_video_when_enabled(db, monkeypatch):
    import config as cfg

    _, Session = db
    monkeypatch.setattr(cfg, "ENABLE_VIDEO_BACKUP", True, raising=False)
    await _run_hook(monkeypatch, Session, _video_item("note-on"))
    async with Session() as s:
        row = (await s.execute(select(VideoAsset))).scalar_one()
        assert row.status == "pending"
        assert row.content_id == "note-on"
        assert row.source_url == "http://cdn/a.mp4"
        assert row.backup_urls == ["http://cdn/b.mp4"]


@pytest.mark.asyncio
async def test_hook_skips_video_when_disabled(db, monkeypatch):
    import config as cfg

    _, Session = db
    monkeypatch.setattr(cfg, "ENABLE_VIDEO_BACKUP", False, raising=False)
    await _run_hook(monkeypatch, Session, _video_item("note-off"))
    async with Session() as s:
        assert (await s.execute(select(VideoAsset))).scalars().all() == []


# ---------------------------------------------------------------------------
# 4. library 镜像里没有 tenacity/playwright，ssr 解析必须还能导入
# ---------------------------------------------------------------------------

def test_ssr_importable_without_crawler_deps():
    """library 容器只装了运行 library 需要的依赖。

    `POST /videos/backfill` 的兜底路径要 import `media_platform.xhs.ssr`，
    而该包的 `__init__` 原本会拉进 core（依赖 tenacity/playwright）。
    用子进程屏蔽这两个包，确认轻量环境仍能解析视频直链。
    """
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import importlib.abc, sys
        BLOCK = {"tenacity", "playwright"}
        class B(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in BLOCK:
                    raise ImportError(fullname)
                return None
        sys.meta_path.insert(0, B())
        import media_platform.xhs.ssr as ssr
        import media_platform.xhs.media as media
        assert hasattr(ssr, "fetch_note_by_url")
        urls = media.extract_video_urls(
            {"type": "video", "video": {"media": {"stream": {"h264": [
                {"masterUrl": "http://x"}]}}}})
        assert urls == ["http://x"], urls
        print("OK")
    """)
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(root),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-1000:]

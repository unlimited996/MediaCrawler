# -*- coding: utf-8 -*-
"""S3 备份去重（tools/media_dedup.py）单元测试。

不连真实 S3：用内存 SQLite 造 media_backup 行，monkeypatch s3_client 的
head/copy/delete 为计数桩，验证：
- analyze_duplicates 正确按 sha256 分组、统计冗余对象与可收回体积；
- dedup_backups(dry_run=True) 不改动任何数据；
- dedup_backups(dry_run=False) 把所有重复行改键到规范键、删除冗余对象、
  保留规范对象、返回与报告一致的处理量。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tools.media_dedup as media_dedup
from database.models_v2 import BaseV2, MediaBackup


# ---- 内存 SQLite ----
ENGINE = create_async_engine(
    "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, future=True
)
SESSIONMAKER = sessionmaker(ENGINE, class_=AsyncSession, expire_on_commit=False)


def _make_row(rid, media_id, checksum, key, size, content_id="n1", status="uploaded"):
    return MediaBackup(
        id=rid,
        media_id=media_id, story_id=1, platform="xhs", content_id=content_id,
        media_type="image", object_key=key, source_url=f"http://x/{media_id}",
        mime_type="image/webp", size_bytes=size, checksum=checksum, status=status,
    )


async def _seed():
    async with ENGINE.begin() as conn:
        await conn.run_sync(BaseV2.metadata.create_all)
    async with SESSIONMAKER() as s:
        # 跨用例复用同一内存库，先清空再播种，避免 media_id 唯一约束冲突
        await s.execute(MediaBackup.__table__.delete())
        await s.commit()
        # 组A：3 张相同字节（不同 content_id/键），大小均 100
        s.add(_make_row(1, 1, "aaa", "xhs/image/n1/001.webp", 100, "n1"))
        s.add(_make_row(2, 2, "aaa", "xhs/image/n2/001.webp", 100, "n2"))
        s.add(_make_row(3, 3, "aaa", "xhs/image/n3/001.webp", 100, "n3"))
        # 组B：2 张相同字节，大小均 50
        s.add(_make_row(4, 4, "bbb", "xhs/image/n4/001.webp", 50, "n4"))
        s.add(_make_row(5, 5, "bbb", "xhs/image/n5/001.webp", 50, "n5"))
        # 非重复：单独一张
        s.add(_make_row(6, 6, "ccc", "xhs/image/n6/001.webp", 200, "n6"))
        # 无校验和 / 非 uploaded：不应被纳入
        s.add(_make_row(7, 7, None, "xhs/image/n7/001.webp", 10, "n7", "pending"))
        s.add(_make_row(8, 8, "ddd", "xhs/image/n8/001.webp", 10, "n8", "failed"))
        await s.commit()


class _FakeS3:
    """记录 copy / delete 调用，head_object 命中即视为存在。"""

    def __init__(self):
        self.deleted = []
        self.copied = []

    async def head_object(self, settings, key):
        # 源对象都“存在”；规范键初始不存在，等拷贝后由 delete 桩标记
        return {"ContentLength": 100} if not key.startswith("dedup/") else None

    async def copy_object(self, settings, src_key, dest_key):
        self.copied.append((src_key, dest_key))

    async def get_object(self, settings, key):
        return b"\x00" * 10

    async def upload_bytes(self, settings, data, key, content_type=None):
        return {"etag": "x", "size": len(data)}

    async def delete_object(self, settings, key):
        self.deleted.append(key)


@pytest.fixture
def fake_s3(monkeypatch):
    f = _FakeS3()
    # head_object：普通键视为存在，dedup/ 规范键初始不存在（需先拷贝）
    monkeypatch.setattr("tools.media_dedup._head",
                        lambda settings, key: f.head_object(settings, key))
    # _ensure_canonical：直接服务端拷贝（跳过真实 s3_client）
    async def _ensure(settings, src_key, dest_key):
        await f.copy_object(settings, src_key, dest_key)
    monkeypatch.setattr(media_dedup, "_ensure_canonical", _ensure)
    # _delete_redundant 内部 from tools.s3_client import delete_object，替换之
    monkeypatch.setattr("tools.s3_client.delete_object",
                        lambda settings, key: f.delete_object(settings, key))
    return f


def test_analyze_groups_correctly():
    asyncio.run(_seed())
    async def _run():
        async with SESSIONMAKER() as s:
            rep = await media_dedup.analyze_duplicates(s)
            return rep
    rep = asyncio.run(_run())
    # 2 个重复组；冗余对象 = (3-1)+(2-1) = 3；可收回 = 100*2 + 50*1 = 250
    assert rep["dup_groups"] == 2, rep
    assert rep["redundant_objects"] == 3, rep
    assert rep["reclaimable_bytes"] == 250, rep
    assert rep["analyzed_rows"] == 6  # 不含 pending/failed/无校验和
    # 每组都有规范键（组A 取首行 xhs/.../n1/001.webp，组B 取 n4）
    by_csum = {g["checksum"]: g for g in rep["groups"]}
    assert by_csum["aaa"]["count"] == 3
    assert by_csum["bbb"]["count"] == 2


def test_dry_run_touches_nothing():
    asyncio.run(_seed())
    f = _FakeS3()
    async def _run():
        async with SESSIONMAKER() as s:
            return await media_dedup.dedup_backups(s, f, dry_run=True)
    stats = asyncio.run(_run())
    assert stats["dry_run"] is True
    assert stats["objects_deleted"] == 3
    assert stats["bytes_reclaimed"] == 250
    assert f.deleted == []  # 没真删


def test_dedup_repoints_and_deletes(fake_s3):
    asyncio.run(_seed())
    f = fake_s3
    async def _run():
        async with SESSIONMAKER() as s:
            stats = await media_dedup.dedup_backups(s, f, dry_run=False)
            # 复查 DB：所有 aaa/bbb 组的 object_key 都改指向规范键（组内首行键）
            rows = (await s.execute(
                __import__("sqlalchemy").select(MediaBackup)
                .where(MediaBackup.checksum.in_(["aaa", "bbb"]))
            )).scalars().all()
            return stats, rows
    stats, rows = asyncio.run(_run())

    # 规范键 = 组内首行现有键；删除行数-1 个冗余，可收回 = 总行体积 - 规范对象体积
    assert stats["groups"] == 2
    assert stats["objects_deleted"] == 3, stats   # (3-1)+(2-1)
    assert stats["bytes_reclaimed"] == 250, stats  # 100*2 + 50*1
    keys = {r.object_key for r in rows}
    assert keys == {"xhs/image/n1/001.webp", "xhs/image/n4/001.webp"}, keys
    # 非重复 / 无校验和行未被改动
    async def _check():
        async with SESSIONMAKER() as s:
            ccc = (await s.execute(
                __import__("sqlalchemy").select(MediaBackup)
                .where(MediaBackup.checksum == "ccc"))).scalar_one()
            n7 = (await s.execute(
                __import__("sqlalchemy").select(MediaBackup)
                .where(MediaBackup.media_id == 7))).scalar_one()
            n8 = (await s.execute(
                __import__("sqlalchemy").select(MediaBackup)
                .where(MediaBackup.media_id == 8))).scalar_one()
            return ccc.object_key, n7.object_key, n8.object_key
    ccc_key, n7_key, n8_key = asyncio.run(_check())
    assert ccc_key == "xhs/image/n6/001.webp"
    assert n7_key == "xhs/image/n7/001.webp"  # pending，未动
    assert n8_key == "xhs/image/n8/001.webp"  # failed，未动


def test_exclude_keys_protects_selected_copies(fake_s3):
    asyncio.run(_seed())
    f = fake_s3
    # 排除组A 中的 n3 副本：它不应被改键、也不应被删除；其余合并照常
    async def _run():
        async with SESSIONMAKER() as s:
            stats = await media_dedup.dedup_backups(
                s, f, dry_run=False,
                exclude_keys={"xhs/image/n3/001.webp"})
            rows = (await s.execute(
                __import__("sqlalchemy").select(MediaBackup)
                .where(MediaBackup.checksum == "aaa")
            )).scalars().all()
            return stats, {r.object_key for r in rows}
    stats, keys = asyncio.run(_run())
    # 组A：非排除副本 n1/n2 合并到 n1，n3 保留；组B：n4/n5 合并到 n4
    assert stats["protected"] == 1, stats
    assert stats["objects_deleted"] == 2, stats   # 组A删1 + 组B删1
    assert stats["bytes_reclaimed"] == 150, stats  # 组A 100 + 组B 50
    # 被排除的 n3 仍在库中且未被改键
    assert "xhs/image/n3/001.webp" in keys, keys
    # 非排除副本合并到规范键 n1
    assert "xhs/image/n1/001.webp" in keys, keys


def test_idempotent_rerun(fake_s3):
    asyncio.run(_seed())
    f = fake_s3
    async def _run():
        async with SESSIONMAKER() as s:
            first = await media_dedup.dedup_backups(s, f, dry_run=False)
            # 二次执行：所有行已指向规范键，无可删冗余对象
            second = await media_dedup.dedup_backups(s, f, dry_run=False)
            return first, second
    first, second = asyncio.run(_run())
    assert first["objects_deleted"] == 3, first
    assert second["objects_deleted"] == 0, second
    assert second["groups"] == 0, second

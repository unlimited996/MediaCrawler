# -*- coding: utf-8 -*-
"""故事检索接口（主链路：检索读取）。

GET /api/stories              列表（平台/标签/关键词筛选，热度/时间/点赞排序，分页）
GET /api/stories/{id}         详情（含章节、标签、附件）
GET /api/stories/{id}/comments 评论树（主楼分页 + 楼中楼按时间）
GET /api/stories/{id}/watch    关注状态（关注此贴 / 关注此人）
POST /api/stories/{id}/watch/{kind}  开关关注（kind=note|author）
GET /api/stats/overview       总览统计
GET /api/tags                 标签列表
"""
import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import and_, delete as sa_delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database.models_v2 import (
    Author, Comment, MediaAttachment, SourceSite, Story, StoryChapter, StoryTag, Tag,
)

router = APIRouter(prefix="/stories", tags=["story"])

SORT_FIELDS = {
    "heat": (Story.heat_score.desc(), Story.last_update_ts.desc()),
    "time": (Story.last_update_ts.desc(),),
    "like": (Story.like_count.desc(),),
    "comment": (Story.comment_count.desc(),),
}


async def _session():
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return None, None
    return engine, AsyncSession(engine, expire_on_commit=False)


def _comment_public(c: Comment, author_name: Optional[str],
                     author_avatar: Optional[str] = None,
                     author_url: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": c.id,
        "content": c.content_text,
        "author": author_name,
        "author_avatar": author_avatar or "",
        "author_url": author_url or "",
        "like_count": c.like_count or 0,
        "sub_count": c.sub_count or 0,
        "publish_ts": c.publish_ts,
        "depth": c.depth,
        "parent_id": c.parent_id,
        "sentiment": c.sentiment,
        "stance": c.stance,
    }


@router.get("")
async def list_stories(
    platform: Optional[str] = None,
    category: Optional[str] = None,
    tag: Optional[str] = None,
    keyword: Optional[str] = None,
    sort: str = "heat",
    start_ts: Optional[int] = Query(None, description="起始发布时间（毫秒时间戳）"),
    end_ts: Optional[int] = Query(None, description="截止发布时间（毫秒时间戳）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """故事列表：支持平台 / 标签 / 关键词 / 时间范围筛选。"""
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "note": "配置库不可用"}
    try:
        q = select(Story).where(Story.deleted_at.is_(None), Story.status.in_(("active", "deleted")))

        if platform:
            q = q.join(SourceSite, Story.primary_site_id == SourceSite.id).where(
                SourceSite.code == platform)
        if category:
            q = q.where(Story.category == category)
        if tag:
            q = q.join(StoryTag, StoryTag.story_id == Story.id).join(
                Tag, Tag.id == StoryTag.tag_id).where(Tag.name == tag)
        if keyword:
            like = f"%{keyword}%"
            q = q.where(or_(Story.title.ilike(like), Story.summary.ilike(like)))
        if start_ts:
            q = q.where(Story.first_publish_ts >= start_ts)
        if end_ts:
            q = q.where(Story.first_publish_ts <= end_ts)

        total = (await session.execute(
            select(func.count()).select_from(q.subquery())
        )).scalar() or 0

        for order in SORT_FIELDS.get(sort, SORT_FIELDS["heat"]):
            q = q.order_by(order)
        rows = (await session.execute(
            q.offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()

        if not rows:
            return {"items": [], "total": total, "page": page, "page_size": page_size}

        ids = [s.id for s in rows]
        tag_rows = (await session.execute(
            select(StoryTag.story_id, Tag.name)
            .join(Tag, Tag.id == StoryTag.tag_id)
            .where(StoryTag.story_id.in_(ids))
        )).all()
        tags_by_story: Dict[int, List[str]] = {}
        for sid, name in tag_rows:
            tags_by_story.setdefault(sid, []).append(name)

        site_rows = (await session.execute(select(SourceSite))).scalars().all()
        site_name = {s.id: s.code for s in site_rows}

        items = [{
            "id": s.id,
            "title": s.title,
            "summary": (s.summary or "")[:200],
            "cover_url": s.cover_url,
            "category": s.category,
            "platform": site_name.get(s.primary_site_id),
            "like_count": s.like_count,
            "comment_count": s.comment_count,
            "heat_score": float(s.heat_score or 0),
            "publish_ts": s.first_publish_ts,
            "tags": tags_by_story.get(s.id, []),
        } for s in rows]

        return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/{story_id}")
async def get_story(story_id: int):
    engine, session = await _session()
    if session is None:
        return {"detail": "配置库不可用"}
    try:
        story = (await session.execute(
            select(Story).where(Story.id == story_id, Story.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not story:
            return {"detail": "not found"}

        chapters = (await session.execute(
            select(StoryChapter).where(StoryChapter.story_id == story_id)
            .order_by(StoryChapter.seq_no)
        )).scalars().all()

        tags = (await session.execute(
            select(Tag.name).join(StoryTag, StoryTag.tag_id == Tag.id)
            .where(StoryTag.story_id == story_id)
        )).scalars().all()

        media = []
        if chapters:
            media = (await session.execute(
                select(MediaAttachment).where(
                    MediaAttachment.target_type == "chapter",
                    MediaAttachment.target_id.in_([c.id for c in chapters]),
                ).order_by(MediaAttachment.sort_order)
            )).scalars().all()

        author_name = None
        author_avatar = None
        author_url = None
        if story.author_id:
            author_name = (await session.execute(
                select(Author.nickname).where(Author.id == story.author_id)
            )).scalar()
            author_avatar = (await session.execute(
                select(Author.avatar_url).where(Author.id == story.author_id)
            )).scalar()
            author_url = (await session.execute(
                select(Author.profile_url).where(Author.id == story.author_id)
            )).scalar()

        # 平台代码（与列表接口一致：走 primary_site_id → SourceSite.code），
        # 前端详情页按它分发到各平台的专属布局
        platform_code = None
        if story.primary_site_id:
            platform_code = (await session.execute(
                select(SourceSite.code).where(SourceSite.id == story.primary_site_id)
            )).scalar()

        return {
            "id": story.id,
            "title": story.title,
            "summary": story.summary,
            "cover_url": story.cover_url,
            "category": story.category,
            "status": story.status,
            "platform": platform_code,
            "author": author_name,
            "author_avatar": author_avatar or "",
            "author_url": author_url or "",
            "like_count": story.like_count,
            "comment_count": story.comment_count,
            "view_count": story.view_count,
            "heat_score": float(story.heat_score or 0),
            "publish_ts": story.first_publish_ts,
            "update_ts": story.last_update_ts,
            "tags": list(tags),
            "chapters": [{
                "id": c.id,
                "seq_no": c.seq_no,
                "title": c.title,
                "content": c.content_text,
                "source_url": c.source_url,
                "publish_ts": c.publish_ts,
                "like_count": c.like_count,
                "comment_count": c.comment_count,
                "completeness": float(c.completeness) if c.completeness else None,
            } for c in chapters],
            "media": [{
                "type": m.media_type,
                "url": m.source_url,
                "local_path": m.local_path,
                # 本地已下载的文件走 API 路由吐出（CDN 防盗链，浏览器直连 403）
                "local_url": (
                    f"/api/data/media/{platform_code}/"
                    f"{Path(m.local_path).parent.name}/"
                    f"{Path(m.local_path).name}"
                    if m.local_path else None
                ),
                "sort_order": m.sort_order,
            } for m in media],
        }
    finally:
        await session.close()
        await engine.dispose()


@router.get("/{story_id}/comments")
async def get_story_comments(
    story_id: int,
    sort: str = "like",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    reply_limit: int = Query(20, ge=0, le=1000,
                             description="每条主评论最多返回多少条回复，0=不限"),
    sentiment: Optional[str] = Query(None, description="按情感筛选：positive/neutral/negative"),
    stance: Optional[str] = Query(None, description="按立场筛选：agree/neutral/oppose（相对发帖人观点）"),
):
    """评论树：先取主楼（depth=0）分页，再按 root_id 拉出楼中楼。

    root_id 冗余的价值就在这里 —— 不需要递归查询。

    回复默认只返回前 reply_limit 条并附 reply_count 总数：一条主评论动辄上千条
    回复，全量下发会让单页响应到几百 KB。前端默认折叠回复，点「展开」时再走
    /comments/{id}/replies 拉全量。

    sentiment 筛选依赖 sentiment 列有值；老数据为 NULL，查询前会先按需回填
    （幂等，只补缺失行），保证筛选与统计立即可用。
    """
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0}
    try:
        sentiment = (sentiment or "").strip().lower() or None
        if sentiment and sentiment not in ("positive", "neutral", "negative"):
            sentiment = None
        stance = (stance or "").strip().lower() or None
        if stance and stance not in ("agree", "neutral", "oppose"):
            stance = None

        # 需要情感/立场维度时，先补齐该 story 的缺失判定（幂等，只补缺失行）
        from tools.v2_sync import backfill_comment_sentiments, backfill_comment_stances

        await backfill_comment_sentiments(story_id=story_id)
        await backfill_comment_stances(story_id=story_id)

        base = select(Comment).where(
            Comment.story_id == story_id, Comment.deleted_at.is_(None), Comment.depth == 0)
        if sentiment:
            base = base.where(Comment.sentiment == sentiment)
        if stance:
            base = base.where(Comment.stance == stance)
        total = (await session.execute(
            select(func.count()).select_from(base.subquery()))).scalar() or 0

        order = (Comment.like_count.desc() if sort == "like"
                 else Comment.publish_ts.asc())
        mains = (await session.execute(
            base.order_by(order).offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()

        # 立场分布与内容赞同度（主指标，全量口径不随筛选变化——与 Steam 一致：
        # 筛选只影响「显示哪些」，赞同度是内容的整体画像）
        stance_rows = (await session.execute(
            select(Comment.stance, func.count())
            .where(Comment.story_id == story_id, Comment.deleted_at.is_(None),
                   Comment.depth == 0)
            .group_by(Comment.stance)
        )).all()
        st_dist = {"agree": 0, "neutral": 0, "oppose": 0}
        for name, cnt in stance_rows:
            key = (name or "neutral").lower()
            if key in st_dist:
                st_dist[key] = int(cnt)
        from tools.stance import agreement_label

        st_total = sum(st_dist.values())
        agreement = agreement_label(st_dist["agree"], st_total, st_dist["neutral"])

        # 情感分布保留作辅助参考
        sent_rows = (await session.execute(
            select(Comment.sentiment, func.count())
            .where(Comment.story_id == story_id, Comment.deleted_at.is_(None),
                   Comment.depth == 0)
            .group_by(Comment.sentiment)
        )).all()
        s_dist = {"positive": 0, "neutral": 0, "negative": 0}
        for name, cnt in sent_rows:
            key = (name or "neutral").lower()
            if key in s_dist:
                s_dist[key] = int(cnt)

        if not mains:
            return {"items": [], "total": total, "page": page, "page_size": page_size,
                    "stance_stats": {**st_dist, "total": st_total, "agreement": agreement},
                    "sentiment_stats": {**s_dist, "total": sum(s_dist.values())}}

        main_ids = [m.id for m in mains]
        subs = (await session.execute(
            select(Comment).where(Comment.root_id.in_(main_ids), Comment.depth > 0)
            .order_by(Comment.publish_ts.asc())
        )).scalars().all()

        author_ids = {c.author_id for c in mains + subs if c.author_id}
        names: Dict[int, str] = {}
        avatars: Dict[int, str] = {}
        urls: Dict[int, str] = {}
        if author_ids:
            rows = (await session.execute(
                select(Author.id, Author.nickname, Author.avatar_url, Author.profile_url)
                .where(Author.id.in_(author_ids))
            )).all()
            names = {r[0]: r[1] for r in rows}
            avatars = {r[0]: r[2] for r in rows if r[2]}
            urls = {r[0]: r[3] for r in rows if r[3]}

        subs_by_root: Dict[int, List[Dict[str, Any]]] = {}
        for s in subs:
            subs_by_root.setdefault(s.root_id, []).append(
                _comment_public(s, names.get(s.author_id), avatars.get(s.author_id), urls.get(s.author_id)))

        items = []
        for m in mains:
            payload = _comment_public(m, names.get(m.author_id), avatars.get(m.author_id), urls.get(m.author_id))
            subs = subs_by_root.get(m.id, [])
            payload["reply_count"] = len(subs)
            payload["replies"] = subs if (reply_limit <= 0 or reply_limit >= len(subs)) else subs[:reply_limit]
            items.append(payload)

        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "stance_stats": {**st_dist, "total": st_total, "agreement": agreement},
                "sentiment_stats": {**s_dist, "total": sum(s_dist.values())}}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/{story_id}/comments/{comment_id}/replies")
async def get_comment_replies(
    story_id: int,
    comment_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=2000),
):
    """单条主评论的全部楼中楼（前端点「展开 N 条回复」时按需拉取）。"""
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0}
    try:
        base = select(Comment).where(
            Comment.story_id == story_id, Comment.deleted_at.is_(None),
            Comment.root_id == comment_id, Comment.depth > 0)
        total = (await session.execute(
            select(func.count()).select_from(base.subquery()))).scalar() or 0
        subs = (await session.execute(
            base.order_by(Comment.publish_ts.asc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()

        author_ids = {c.author_id for c in subs if c.author_id}
        names: Dict[int, str] = {}
        avatars: Dict[int, str] = {}
        urls: Dict[int, str] = {}
        if author_ids:
            rows = (await session.execute(
                select(Author.id, Author.nickname, Author.avatar_url, Author.profile_url)
                .where(Author.id.in_(author_ids))
            )).all()
            names = {r[0]: r[1] for r in rows}
            avatars = {r[0]: r[2] for r in rows if r[2]}
            urls = {r[0]: r[3] for r in rows if r[3]}

        items = [_comment_public(s, names.get(s.author_id), avatars.get(s.author_id), urls.get(s.author_id))
                 for s in subs]
        return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/stats/overview")
async def stats_overview():
    engine, session = await _session()
    if session is None:
        return {}
    try:
        story_count = (await session.execute(
            select(func.count()).select_from(Story))).scalar() or 0
        comment_count = (await session.execute(
            select(func.count()).select_from(Comment))).scalar() or 0
        author_count = (await session.execute(
            select(func.count()).select_from(Author))).scalar() or 0
        chapter_count = (await session.execute(
            select(func.count()).select_from(StoryChapter))).scalar() or 0

        by_platform = (await session.execute(
            select(SourceSite.code, func.count(Story.id))
            .join(Story, Story.primary_site_id == SourceSite.id)
            .group_by(SourceSite.code)
        )).all()

        top_tags = (await session.execute(
            select(Tag.name, func.count(StoryTag.story_id))
            .join(StoryTag, StoryTag.tag_id == Tag.id)
            .group_by(Tag.name).order_by(func.count(StoryTag.story_id).desc()).limit(10)
        )).all()

        return {
            "story_count": story_count,
            "chapter_count": chapter_count,
            "comment_count": comment_count,
            "author_count": author_count,
            "by_platform": {code: cnt for code, cnt in by_platform},
            "top_tags": [{"name": n, "count": c} for n, c in top_tags],
        }
    finally:
        await session.close()
        await engine.dispose()


@router.get("/tags/list")
async def list_tags(limit: int = Query(50, ge=1, le=200)):
    engine, session = await _session()
    if session is None:
        return {"items": []}
    try:
        rows = (await session.execute(
            select(Tag.name, func.count(StoryTag.story_id))
            .join(StoryTag, StoryTag.tag_id == Tag.id)
            .group_by(Tag.name).order_by(func.count(StoryTag.story_id).desc()).limit(limit)
        )).all()
        return {"items": [{"name": n, "count": c} for n, c in rows]}
    finally:
        await session.close()
        await engine.dispose()


# v1 遗留原始表：平台码 -> (内容表, 内容ID列, 评论表, 评论表内容ID列)
# 硬删除时必须同步清理，否则下次 backfill_v1_to_v2 / 重新拉取会把它「复活」。
_V1_TABLES: Dict[str, Any] = {
    "xhs": ("xhs_note", "note_id", "xhs_note_comment", "note_id"),
    "dy": ("douyin_aweme", "aweme_id", "douyin_aweme_comment", "aweme_id"),
    "ks": ("kuaishou_video", "video_id", "kuaishou_video_comment", "video_id"),
    "bili": ("bilibili_video", "video_id", "bilibili_video_comment", "video_id"),
    "wb": ("weibo_note", "note_id", "weibo_note_comment", "note_id"),
    "tieba": ("tieba_note", "note_id", "tieba_comment", "note_id"),
    "zhihu": ("zhihu_content", "content_id", "zhihu_comment", "content_id"),
}

CASCADE_KEYS = ("chapters", "comments", "media", "tags",
                "files", "legacy_rows", "homefeed_rows")


async def _hard_delete_story(session: AsyncSession, story_id: int,
                             purge_files: bool = True) -> Optional[Dict[str, int]]:
    """物理删除一条 story 及其全部关联数据（不可恢复）。

    清理范围：
      v2   story / story_chapter / comment / story_tag / media_attachment
      v1   平台原始表（xhs_note 等）与其评论表 —— 不清会被回填/重拉复活
      v2   homefeed_content（首页流缓存）
      磁盘 附件本地文件，空的内容目录一并清掉

    返回各类删除行数；story 不存在时返回 None。
    注意：已软删除（deleted_at 非空）的记录同样会被物理清掉。
    """
    from tools.paths import DATA_DIR

    story = (await session.execute(
        select(Story).where(Story.id == story_id)
    )).scalar_one_or_none()
    if not story:
        return None

    chapters = (await session.execute(
        select(StoryChapter).where(StoryChapter.story_id == story_id)
    )).scalars().all()
    chapter_ids = [c.id for c in chapters]

    comments = (await session.execute(
        select(Comment).where(Comment.story_id == story_id)
    )).scalars().all()

    # ---- 1. 附件行（story / chapter / comment 三种 target）----
    conds = [and_(MediaAttachment.target_type == "story",
                  MediaAttachment.target_id == story_id)]
    if chapter_ids:
        conds.append(and_(MediaAttachment.target_type == "chapter",
                          MediaAttachment.target_id.in_(chapter_ids)))
    comment_ids = [c.id for c in comments]
    if comment_ids:
        conds.append(and_(MediaAttachment.target_type == "comment",
                          MediaAttachment.target_id.in_(comment_ids)))
    media = (await session.execute(
        select(MediaAttachment).where(or_(*conds))
    )).scalars().all()

    # ---- 2. 磁盘上的媒体文件（只删项目 data 目录内的，防误删）----
    files_removed = 0
    if purge_files:
        try:
            data_root = Path(DATA_DIR).resolve()
        except OSError:
            data_root = None
        touched_dirs = set()
        if data_root is not None:
            for m in media:
                raw = m.local_path
                if not raw:
                    continue
                try:
                    fp = Path(raw).resolve()
                except OSError:
                    continue
                if data_root not in fp.parents:
                    continue
                try:
                    if fp.is_file():
                        fp.unlink()
                        files_removed += 1
                        touched_dirs.add(fp.parent)
                except OSError:
                    continue
            for d in touched_dirs:
                try:
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    continue

    # ---- 3. v2 子表（先删子表，避免外键报错）----
    media_count = 0
    if media:
        media_count = (await session.execute(
            sa_delete(MediaAttachment).where(
                MediaAttachment.id.in_([m.id for m in media]))
        )).rowcount or 0
    tags = (await session.execute(
        sa_delete(StoryTag).where(StoryTag.story_id == story_id))).rowcount or 0
    comments_n = (await session.execute(
        sa_delete(Comment).where(Comment.story_id == story_id))).rowcount or 0
    chapters_n = (await session.execute(
        sa_delete(StoryChapter).where(StoryChapter.story_id == story_id))).rowcount or 0

    # ---- 4. v1 原始表 + 首页流缓存（否则会被回填 / 重新拉取复活）----
    site_ids = {c.source_site_id for c in chapters if c.source_site_id}
    if story.primary_site_id:
        site_ids.add(story.primary_site_id)
    code_by_id: Dict[int, str] = {}
    if site_ids:
        rows = (await session.execute(
            select(SourceSite.id, SourceSite.code).where(SourceSite.id.in_(site_ids))
        )).all()
        code_by_id = dict(rows)

    legacy = feed = 0
    seen = set()
    for c in chapters:
        platform = code_by_id.get(c.source_site_id)
        content_id = (c.source_content_id or "").strip()
        if not platform or not content_id or (platform, content_id) in seen:
            continue
        seen.add((platform, content_id))

        feed += (await session.execute(
            text("DELETE FROM homefeed_content WHERE platform=:p AND content_id=:cid"),
            {"p": platform, "cid": content_id})).rowcount or 0

        tpl = _V1_TABLES.get(platform)
        if not tpl:
            continue
        c_tbl, c_col, m_tbl, m_col = tpl
        legacy += (await session.execute(
            text(f"DELETE FROM {m_tbl} WHERE {m_col}=:cid"),
            {"cid": content_id})).rowcount or 0
        legacy += (await session.execute(
            text(f"DELETE FROM {c_tbl} WHERE {c_col}=:cid"),
            {"cid": content_id})).rowcount or 0

    # ---- 5. 主记录 ----
    await session.execute(sa_delete(Story).where(Story.id == story_id))

    return {"chapters": chapters_n, "comments": comments_n, "media": media_count,
            "tags": tags, "files": files_removed,
            "legacy_rows": legacy, "homefeed_rows": feed}


@router.post("/batch-delete")
async def batch_delete_stories(payload: Dict[str, Any] = Body(default={})):
    """批量硬删除：body {"ids": [1,2,3], "purge_files": true}。

    物理删除 story 及 chapter / comment / tag / 附件（默认连磁盘媒体文件一起删），
    并清理 v1 原始表与首页流缓存，避免被回填或重新拉取「复活」。
    单条失败回滚后继续，不影响其余；不存在的 id 直接跳过（幂等）。
    """
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, (list, tuple)):
        raise HTTPException(status_code=400, detail="ids 必须是 ID 数组")
    purge_files = bool(payload.get("purge_files", True))

    # 去重 + 过滤非正整数，避免脏数据把 SQL 搞崩
    story_ids: List[int] = []
    for v in ids:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0 and iv not in story_ids:
            story_ids.append(iv)
    if not story_ids:
        return {"success": True, "deleted_count": 0, "requested": 0,
                "cascade": {k: 0 for k in CASCADE_KEYS}, "failed": []}

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        cascade = {k: 0 for k in CASCADE_KEYS}
        deleted_count = 0
        failed: List[Dict[str, Any]] = []
        for sid in story_ids:
            try:
                stat = await _hard_delete_story(session, sid, purge_files)
            except Exception as exc:                      # 单条出错不拖垮整批
                await session.rollback()
                failed.append({"id": sid, "error": str(exc)[:200]})
                continue
            if not stat:
                continue
            deleted_count += 1
            for k in cascade:
                cascade[k] += stat.get(k, 0) or 0
        await session.commit()
        return {"success": True, "deleted_count": deleted_count,
                "requested": len(story_ids), "cascade": cascade,
                "failed": failed}
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/{story_id}")
async def delete_story(story_id: int, purge_files: bool = True):
    """物理删除单条故事（含章节 / 评论 / 标签 / 附件 / 本地媒体文件）。

    不可恢复。purge_files=false 可保留磁盘上的媒体文件。
    """
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stat = await _hard_delete_story(session, story_id, purge_files)
        if not stat:
            raise HTTPException(status_code=404, detail="故事不存在")
        await session.commit()
        return {"success": True, "deleted_id": story_id, "cascade": stat}
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 关注（定时更新）
#
# 详情页的【关注此贴】/【关注此人】：复用现有定时调度器（api/services/
# task_scheduler.py，纯 asyncio，无第三方依赖）建一条「每天跑一次」的任务，
# 抓取参数与详情页「重新拉取」完全一致（平台 / 原文链接 / 开评论+子评论+
# 媒体 / 直写 PG）。任务编码固定为 watch-note-{id} / watch-author-{平台}-{作者}，
# 同一个对象反复开关只会复用同一条任务，不会堆积。
# ---------------------------------------------------------------------------

#: 关注任务的周期：每天一次
WATCH_INTERVAL_SEC = 86400


async def _load_watch_targets(story_id: int) -> Optional[Dict[str, Any]]:
    """取建关注任务需要的信息：平台、原文链接、作者主页。"""
    engine, session = await _session()
    if session is None:
        return None
    try:
        story = (await session.execute(
            select(Story).where(Story.id == story_id, Story.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not story:
            return None

        platform_code = None
        if story.primary_site_id:
            platform_code = (await session.execute(
                select(SourceSite.code).where(SourceSite.id == story.primary_site_id)
            )).scalar()

        # 原文链接：优先章节上的（详情页「重新拉取」用的就是它）
        source_url = (await session.execute(
            select(StoryChapter.source_url).where(
                StoryChapter.story_id == story_id,
                StoryChapter.source_url.isnot(None),
            ).order_by(StoryChapter.seq_no).limit(1)
        )).scalar() or ""

        author_url = ""
        author_name = ""
        if story.author_id:
            row = (await session.execute(
                select(Author.nickname, Author.profile_url)
                .where(Author.id == story.author_id)
            )).first()
            if row:
                author_name = row[0] or ""
                author_url = row[1] or ""

        return {
            "story_id": story_id,
            "title": story.title or "",
            "platform": platform_code or "",
            "source_url": source_url,
            "author_id": story.author_id,
            "author_name": author_name,
            "author_url": author_url,
        }
    finally:
        await session.close()
        await engine.dispose()


def _watch_codes(info: Dict[str, Any]) -> Dict[str, str]:
    return {
        "note": f"watch-note-{info['story_id']}",
        "author": f"watch-author-{info['platform']}-{info.get('author_id') or 0}",
    }


def _watch_view(task: Optional[Dict[str, Any]], supported: bool) -> Dict[str, Any]:
    """任务行 → 前端开关状态。任务不存在 = 未关注。"""
    if not task:
        return {"enabled": False, "task_id": None, "supported": supported,
                "last_run_at": None, "last_status": None, "last_error": None,
                "next_run_at": None, "stopped": False, "stop_reason": ""}
    opts = task.get("options") or {}
    return {
        "enabled": bool(task.get("enabled")),
        "task_id": task.get("id"),
        "supported": supported,
        "last_run_at": task.get("last_run_at"),
        "last_status": task.get("last_status"),
        "last_error": task.get("last_error"),
        "next_run_at": task.get("next_run_at"),
        # stopped：任务被自动停了（目标没了），不是用户手动关的
        "stopped": bool(opts.get("auto_stopped")) and not task.get("enabled"),
        "stop_reason": opts.get("auto_stop_reason") or "",
    }


def _watch_options() -> Dict[str, Any]:
    """抓取选项：与详情页「重新拉取」保持一致。"""
    return {
        "login_type": "cookie",
        "enable_comments": True,
        "enable_sub_comments": True,
        "enable_media": True,
        "save_option": "postgres",
        "headless": True,
        "cookies": "",
        "max_comments_count": None,
    }


@router.get("/{story_id}/watch")
async def get_story_watch(story_id: int):
    """关注状态：【关注此贴】/【关注此人】两个开关的当前状态。"""
    info = await _load_watch_targets(story_id)
    if info is None:
        return {"detail": "not found"}
    from ..services.task_scheduler import task_scheduler

    codes = _watch_codes(info)
    note = await task_scheduler.get_task_by_code(codes["note"])
    author = await task_scheduler.get_task_by_code(codes["author"])
    return {
        "note": _watch_view(note, bool(info["source_url"])),
        "author": _watch_view(author, bool(info["author_url"])),
    }


@router.post("/{story_id}/watch/{kind}")
async def set_story_watch(story_id: int, kind: str,
                          payload: Dict[str, Any] = Body(default={})):
    """开关关注。kind=note | author，body: {"enabled": true|false}"""
    if kind not in ("note", "author"):
        raise HTTPException(status_code=400, detail="kind 只能是 note 或 author")
    enabled = bool(payload.get("enabled", True))

    info = await _load_watch_targets(story_id)
    if info is None:
        raise HTTPException(status_code=404, detail="内容不存在")

    from ..services.task_scheduler import task_scheduler

    codes = _watch_codes(info)
    code = codes[kind]
    if kind == "note":
        if not info["source_url"]:
            raise HTTPException(status_code=400, detail="该内容没有原文链接，无法关注此贴")
        target = info["source_url"]
        task_type = "note"
        name = f"关注此贴：{(info['title'] or '未命名')[:24]}"
    else:
        if not info["author_url"]:
            raise HTTPException(status_code=400, detail="该作者没有主页链接，无法关注此人")
        target = info["author_url"]
        task_type = "profile"
        name = f"关注此人：{(info['author_name'] or '未知作者')[:24]}"

    task = await task_scheduler.get_task_by_code(code)

    if task is None:
        if not enabled:
            return {kind: _watch_view(None, True), "kind": kind}
        task = await task_scheduler.create_task({
            "task_code": code,
            "name": name,
            "platform": info["platform"],
            "task_type": task_type,
            "target": target,
            "mode": "incremental",
            "schedule_type": "interval",
            "interval_sec": WATCH_INTERVAL_SEC,
            "options": _watch_options(),
            "enabled": True,
        })
    else:
        # 手动开关一律清掉「自动停止」标记：用户重新关注即视为已知情，
        # 手动关闭也不该再提示「已自动停止」
        opts = dict(task.get("options") or {})
        for k in ("auto_stopped", "auto_stop_reason", "auto_stopped_at"):
            opts.pop(k, None)
        task = await task_scheduler.update_task(
            task["id"], {"enabled": enabled, "options": opts,
                         "target": target, "name": name}) or task

    if enabled and bool(payload.get("run_now", True)):
        # 立刻拉一次，关注后不用等明天才看到数据。
        # 放到后台跑：抓取动辄几分钟，绝不能挂在这个请求上（否则前端一直转圈）
        async def _kickoff() -> None:
            try:
                await task_scheduler.trigger(task["id"])
            except Exception as e:  # 立即执行失败不影响开关状态，下次调度仍会跑
                print(f"[watch] 关注后首次拉取失败 task={task['id']}: {e}")

        asyncio.create_task(_kickoff())

    return {"kind": kind, kind: _watch_view(task, True)}

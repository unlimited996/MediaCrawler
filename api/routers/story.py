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
    Author, Comment, MediaAttachment, MediaBackup, SourceSite, Story,
    StoryChapter, StoryEditLog, StoryTag, Tag,
)

from ..service_paths import asset_url as _asset_url, library_api as _library_api

router = APIRouter(prefix="/stories", tags=["story"])

SORT_FIELDS = {
    "heat": (Story.heat_score.desc(), Story.last_update_ts.desc()),
    "time": (Story.last_update_ts.desc(),),
    "like": (Story.like_count.desc(),),
    "comment": (Story.comment_count.desc(),),
    # 发布时间倒序（最新在前）：列表默认排序用这个，与「按时间」（最后更新）
    # 区分开，两者语义不同，不要合并
    "publish": (Story.first_publish_ts.desc(), Story.id.desc()),
}


async def _session():
    from config.db_config import resolve_config_db_type
    from database.db_session import SharedEngineHandle, get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return None, None
    # 返回共享引擎的薄壳：endpoint 的 finally 里会调 engine.dispose()，
    # 直接给真引擎会把全局连接池拆掉，下个请求就得重连远端库（几百毫秒）。
    return SharedEngineHandle(engine), AsyncSession(engine, expire_on_commit=False)


TAG_NAME_BY_CODE: Dict[str, Any] = {}
TAG_COLOR_BY_CODE: Dict[str, Any] = {}
_TAG_MAPS_AT = 0.0


def _ensure_tag_maps() -> None:
    """刷新标签 code → 名称 / 颜色的缓存（60 秒 TTL，改完标签库最多 1 分钟可见）。"""
    global _TAG_MAPS_AT
    import time as _time
    if TAG_NAME_BY_CODE and (_time.time() - _TAG_MAPS_AT) < 60:
        return
    try:
        from tools.stance import active_tags
        tags = active_tags() or []
        TAG_NAME_BY_CODE.clear()
        TAG_COLOR_BY_CODE.clear()
        for t in tags:
            if t.get("code"):
                TAG_NAME_BY_CODE[t["code"]] = t.get("name")
                TAG_COLOR_BY_CODE[t["code"]] = t.get("color")
        _TAG_MAPS_AT = _time.time()
    except Exception:
        pass


def _comment_public(c: Comment, author_name: Optional[str],
                     author_avatar: Optional[str] = None,
                     author_url: Optional[str] = None,
                     pictures: Optional[List[Dict[str, Any]]] = None,
                     reply_to: Optional[str] = None) -> Dict[str, Any]:
    _ensure_tag_maps()
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
        "root_id": c.root_id,
        # 对话目标：子评论回复的作者昵称（由后端按 parent_id 映射得出，
        # 子评论之间互相回复时指向被回复的那条子评论作者，而非永远指向主评论作者）
        "reply_to": reply_to or None,
        # 评论 IP 属地（原站「IP属地：xx」），重抓带回时入库
        "ip_location": c.ip_location or "",
        "sentiment": c.sentiment,
        "stance": c.stance,
        # 态度标签（P3）：徽标展示用，没判过的回落为空
        "stance_tag_code": c.stance_tag_code,
        "stance_tag_name": TAG_NAME_BY_CODE.get(c.stance_tag_code or ""),
        "stance_tag_color": TAG_COLOR_BY_CODE.get(c.stance_tag_code or ""),
        "stance_confidence": (float(c.stance_confidence)
                              if c.stance_confidence is not None else None),
        # 评论配图（表情包 / 图片评论）：后端已解析成本地 / S3 / 远程 URL，
        # 前端直接网格渲染。此前这个字段从未随评论下发，导致表情包看不到。
        "pictures": pictures or [],
    }


async def _load_comment_pictures(session, comment_ids: List[int]
                                 ) -> Dict[int, List[Dict[str, Any]]]:
    """批量取出评论配图（target_type='comment'）并解析成前端可直出的 URL。

    返回 {comment_id: [{url, local_url, backup_url, source}]}。
    URL 解析优先级（与帖子媒体一致）：本地副本 → S3 备份 → 平台远程外链。
    本地副本走 {LIBRARY_API_PREFIX}/assets/raw/{local_path}（绕 CDN 防盗链 + 签名过期 403）。
    """
    out: Dict[int, List[Dict[str, Any]]] = {}
    if not comment_ids:
        return out
    media_rows = (await session.execute(
        select(MediaAttachment).where(
            MediaAttachment.target_type == "comment",
            MediaAttachment.target_id.in_(comment_ids),
        ).order_by(MediaAttachment.target_id, MediaAttachment.sort_order)
    )).scalars().all()
    if not media_rows:
        return out
    media_ids = [m.id for m in media_rows]
    backups = (await session.execute(
        select(MediaBackup.media_id, MediaBackup.object_key).where(
            MediaBackup.media_id.in_(media_ids))
    )).all()
    backup_map = {mid: key for mid, key in backups}
    from tools.paths import DATA_DIR
    for m in media_rows:
        local_url = None
        if m.local_path:
            try:
                rel = Path(m.local_path).relative_to(DATA_DIR).as_posix()
            except Exception:
                rel = str(m.local_path).replace("\\", "/")
            local_url = _asset_url("raw", rel)
        backup_url = (_library_api(f"s3-backup/object/{backup_map[m.id]}")
                      if m.id in backup_map else None)
        resolved = local_url or backup_url or (m.source_url or None)
        src = ("local" if local_url else
               "backup" if backup_url else
               "remote" if m.source_url else "none")
        lst = out.setdefault(m.target_id, [])
        # 去重保险：同一张图可能因重抓产生两条媒体记录（不同 CDN 签名 URL、
        # 相同 local_path），按 local_url 跳过重复，避免前端重复渲染同一张图。
        if local_url and any(e.get("local_url") == local_url for e in lst):
            continue
        lst.append({
            "url": resolved,
            "local_url": local_url,
            "backup_url": backup_url,
            "source": src,
        })
    return out


@router.get("")
async def list_stories(
    platform: Optional[str] = None,
    category: Optional[str] = None,
    tag: Optional[str] = Query(
        None, description="标签筛选：单个标签名，或逗号分隔多个（如 mbti,INTJ）"),
    tag_match: str = Query(
        "any", description="多标签匹配模式：any=命中任一（OR），all=同时含全部（AND）"),
    keyword: Optional[str] = None,
    sort: str = "heat",
    start_ts: Optional[int] = Query(None, description="起始发布时间（毫秒时间戳）"),
    end_ts: Optional[int] = Query(None, description="截止发布时间（毫秒时间戳）"),
    # 发帖人筛选（多选）：传多个 author_ids=1&author_ids=2，命中任一即展示
    author_ids: Optional[List[int]] = Query(None, description="按发帖人筛选，可多选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """故事列表：支持平台 / 标签 / 关键词 / 时间范围 / 发帖人筛选。"""
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "note": "配置库不可用"}
    try:
        q = select(Story).where(Story.deleted_at.is_(None), Story.status.in_(("active", "deleted", "hidden")))

        if platform:
            q = q.join(SourceSite, Story.primary_site_id == SourceSite.id).where(
                SourceSite.code == platform)
        if category:
            q = q.where(Story.category == category)
        # 标签多选：逗号分隔多个标签名，any=命中任一 / all=同时含全部。
        # 用 EXISTS 子查询而不是 join —— join 会让「多标签命中同一帖子」重复出
        # 行（计数虚高 + 列表重复），EXISTS 天然去重。
        tag_names = [x.strip() for x in (tag or "").split(",") if x.strip()]
        if tag_match not in ("any", "all"):
            tag_match = "any"
        if tag_names:
            conds = []
            for name in tag_names:
                conds.append(
                    select(StoryTag.tag_id)
                    .join(Tag, Tag.id == StoryTag.tag_id)
                    .where(StoryTag.story_id == Story.id, Tag.name == name)
                    .exists()
                )
            q = q.where(and_(*conds) if tag_match == "all" else or_(*conds))
        if keyword:
            like = f"%{keyword}%"
            q = q.where(or_(Story.title.ilike(like), Story.summary.ilike(like)))
        if start_ts:
            q = q.where(Story.first_publish_ts >= start_ts)
        if end_ts:
            q = q.where(Story.first_publish_ts <= end_ts)
        if author_ids:
            q = q.where(Story.author_id.in_(author_ids))

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

        # 发帖人信息（列表要展示头像 + 昵称）：一次批量查，避免 N+1
        author_ids_on_page = {s.author_id for s in rows if s.author_id}
        author_map: Dict[int, Any] = {}
        if author_ids_on_page:
            a_rows = (await session.execute(
                select(Author.id, Author.nickname, Author.avatar_url, Author.profile_url)
                .where(Author.id.in_(author_ids_on_page))
            )).all()
            author_map = {r[0]: {"name": r[1], "avatar": r[2] or "", "url": r[3] or ""}
                          for r in a_rows}

        # 封面 / 正文摘录（卡片视图要用）
        #   cover_local_url : 帖子首张图，已落盘的本地副本（外链带签名会过期，实测 403）
        #   cover_url       : 帖子首张图，无本地副本时的外链
        #   excerpt         : 正文前 50 字，无图时卡片显示它
        ch_rows = (await session.execute(
            select(StoryChapter.id, StoryChapter.story_id, StoryChapter.content_text)
            .where(StoryChapter.story_id.in_(ids))
        )).all() if ids else []
        chapter_ids = [r[0] for r in ch_rows]
        excerpt_map: Dict[int, str] = {}
        for _cid, _sid, _txt in ch_rows:
            if _sid in excerpt_map or not _txt:
                continue
            excerpt_map[_sid] = " ".join(str(_txt).split())[:50]

        cover_local: Dict[int, str] = {}
        cover_first: Dict[int, str] = {}
        if chapter_ids:
            from tools.paths import DATA_DIR as _DATA_DIR
            cov = (await session.execute(
                select(MediaAttachment.target_id, MediaAttachment.local_path,
                       MediaAttachment.source_url, MediaAttachment.media_type,
                       StoryChapter.source_content_id, StoryChapter.story_id,
                       StoryChapter.source_site_id)
                .join(StoryChapter, MediaAttachment.target_id == StoryChapter.id)
                .where(MediaAttachment.target_type == "chapter",
                       MediaAttachment.target_id.in_(chapter_ids),
                       MediaAttachment.media_type == "image")
                .order_by(MediaAttachment.sort_order.asc())
            )).all()
            site_codes = {x.id: x.code for x in (await session.execute(
                select(SourceSite))).scalars().all()}
            for _tid, lp, src, _mt, content_id, sid, site_id in cov:
                # 首张图外链（不论有没有本地副本）
                if sid not in cover_first and src:
                    cover_first[sid] = src
                if sid in cover_local or not lp:
                    continue
                try:
                    rel = Path(lp).resolve().relative_to(_DATA_DIR.resolve())
                    # 二进制直出走 assets/raw/（/files/ 是文本预览，图片会 400）
                    cover_local[sid] = _asset_url("raw", rel.as_posix())
                except Exception:
                    p = Path(lp)
                    cover_local[sid] = (
                        f"{_library_api()}/assets/media/{site_codes.get(site_id) or ''}/"
                        f"{content_id}/{p.name}")

        # ---- S3 备份（P4）：封面第三级回退 + 全量备份徽标 ----
        # 访问链：本地缓存 → 平台远程 → S3 备份（前两级都没有才用备份地址）
        backup_cover: Dict[int, str] = {}
        if chapter_ids:
            b_rows = (await session.execute(
                select(MediaBackup.object_key, MediaBackup.status,
                       MediaAttachment.media_type, StoryChapter.story_id)
                .join(MediaAttachment, MediaBackup.media_id == MediaAttachment.id)
                .join(StoryChapter, MediaAttachment.target_id == StoryChapter.id)
                .where(StoryChapter.story_id.in_(ids),
                       MediaAttachment.target_type == "chapter",
                       MediaBackup.status == "uploaded")
                .order_by(MediaAttachment.sort_order.asc())
            )).all()
            for _key, _st, _mt, sid in b_rows:
                # 只取首张已备份的图片做封面回退
                if sid in backup_cover or _mt != "image":
                    continue
                backup_cover[sid] = _library_api(f"s3-backup/object/{_key}")

        from tools.media_backup import story_backup_state

        backup_state = await story_backup_state(session, ids)

        def _backup_complete(sid: int) -> bool:
            st = backup_state.get(sid) or {}
            return st.get("total", 0) > 0 and st.get("uploaded", 0) >= st.get("total", 0)

        items = [{
            "id": s.id,
            "title": s.title,
            "summary": (s.summary or "")[:200],
            # 首张图：本地副本优先（外链会过期），再平台外链，最后 S3 备份
            "cover_url": (cover_local.get(s.id)
                          or cover_first.get(s.id)
                          or backup_cover.get(s.id)
                          or s.cover_url or ""),
            "cover_local_url": cover_local.get(s.id) or "",
            "cover_backup_url": backup_cover.get(s.id) or "",
            # 无图时卡片显示正文前 50 字
            "excerpt": excerpt_map.get(s.id) or (s.summary or "")[:50],
            "category": s.category,
            "platform": site_name.get(s.primary_site_id),
            "media_count": int(s.media_count or 0),
            "edit_count": int(s.edit_count or 0),
            # 备份进度：全部媒体已入 S3 时前端给「已备份」小标识
            "backup_total": (backup_state.get(s.id) or {}).get("total", 0),
            "backup_uploaded": (backup_state.get(s.id) or {}).get("uploaded", 0),
            "backup_complete": _backup_complete(s.id),
            "author_id": s.author_id,
            "author": (author_map.get(s.author_id) or {}).get("name") or "",
            "author_avatar": (author_map.get(s.author_id) or {}).get("avatar") or "",
            "author_url": (author_map.get(s.author_id) or {}).get("url") or "",
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


@router.get("/authors/list")
async def list_authors(
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    """发帖人列表（数据管理页的多选筛选用）。

    按发帖量倒序返回，带头像与帖子数；可带平台与昵称关键词缩小范围
    （作者表有 3000+ 行，前端靠这两个条件收敛候选，不做全量下拉）。
    """
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0}
    try:
        q = (select(Author.id, Author.nickname, Author.avatar_url, Author.profile_url,
                func.count(Story.id).label("story_count"))
         .join(Story, Story.author_id == Author.id)
         .where(Story.deleted_at.is_(None),
                Story.status.in_(("active", "deleted", "hidden"))))
        if platform:
            q = q.where(Story.primary_site_id.in_(
                select(SourceSite.id).where(SourceSite.code == platform)))
        if keyword:
            like = f"%{keyword}%"
            q = q.where(Author.nickname.ilike(like))
        rows = (await session.execute(
            q.group_by(Author.id, Author.nickname, Author.avatar_url, Author.profile_url)
            .order_by(func.count(Story.id).desc(), Author.id.asc())
            .limit(limit)
        )).all()
        return {
            "items": [{
                "id": r[0],
                "name": r[1] or "",
                "avatar": r[2] or "",
                "url": r[3] or "",
                "story_count": int(r[4] or 0),
            } for r in rows],
            "total": len(rows),
        }
    finally:
        await session.close()
        await engine.dispose()


@router.get("/edits/recent")
async def list_recent_edits(limit: int = Query(20, ge=1, le=100)):
    """最近的编辑记录（跨内容）。"""
    engine, session = await _session()
    if session is None:
        return {"items": []}
    try:
        rows = (await session.execute(
            select(StoryEditLog, Story.title)
            .join(Story, StoryEditLog.story_id == Story.id)
            .order_by(StoryEditLog.edit_ts.desc()).limit(limit)
        )).all()
        return {"items": [{
            "id": r[0].id,
            "story_id": r[0].story_id,
            "story_title": r[1],
            "edit_ts": r[0].edit_ts,
            "changed_fields": r[0].changed_fields or [],
            "source": r[0].source,
        } for r in rows]}
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

        # S3 备份地址（P4）：本地没了、外链挂了之后的第三级访问来源
        backup_urls: Dict[int, str] = {}
        if media:
            b_rows = (await session.execute(
                select(MediaBackup.media_id, MediaBackup.object_key)
                .where(MediaBackup.media_id.in_([m.id for m in media]),
                       MediaBackup.status == "uploaded")
            )).all()
            backup_urls = {mid: _library_api(f"s3-backup/object/{key}")
                           for mid, key in b_rows}

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

        # 备份进度（P4）：全部媒体已入 S3 时详情页展示「已备份」标识
        from tools.media_backup import story_backup_detail

        _bk = await story_backup_detail(session, story_id)

        return {
            "id": story.id,
            "title": story.title,
            "summary": story.summary,
            "cover_url": story.cover_url,
            "category": story.category,
            "status": story.status,
            "media_count": int(story.media_count or 0),
            "edit_count": int(story.edit_count or 0),
            "edited_at": story.edited_at,
            "platform": platform_code,
            "backup_total": _bk["total"],
            "backup_uploaded": _bk["uploaded"],
            "backup_complete": _bk["complete"],
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
                "storage_type": m.storage_type or "url",
                "source_status": m.source_status or "unknown",
                # 本地已下载的文件走 API 路由吐出（CDN 防盗链，浏览器直连 403）
                "local_url": (
                    f"{_library_api()}/assets/media/{platform_code}/"
                    f"{Path(m.local_path).parent.name}/"
                    f"{Path(m.local_path).name}"
                    if m.local_path else None
                ),
                # S3 备份地址（P4）：本地 / 外链都不可用时兜底
                "backup_url": backup_urls.get(m.id),
                # 访问链优先级：本地 → 平台远程 → S3 备份
                "source": ("local" if m.local_path
                           else ("remote" if m.source_url
                                 else ("s3" if backup_urls.get(m.id) else "none"))),
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
    tag: Optional[str] = Query(None, description="按态度标签筛选：stance_tag.code，逗号分隔多个"),
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
        # 态度标签筛选（库表驱动，支持逗号分隔多值）
        tag_list = [x.strip() for x in (tag or "").split(",") if x.strip()]

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
        if tag_list:
            base = base.where(Comment.stance_tag_code.in_(tag_list))
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

        # 态度标签分布（库表驱动；按库内顺序输出，前端据此渲染筛选按钮与占比）
        tag_rows = (await session.execute(
            select(Comment.stance_tag_code, func.count())
            .where(Comment.story_id == story_id, Comment.deleted_at.is_(None),
                   Comment.depth == 0, Comment.stance_tag_code.isnot(None))
            .group_by(Comment.stance_tag_code)
        )).all()
        tag_dist = {code: int(cnt) for code, cnt in tag_rows if code}
        try:
            from tools.stance import active_tags
            _ensure_tag_maps()
            tag_stats = [{
                "code": t.get("code"),
                "name": t.get("name"),
                "color": t.get("color"),
                "polarity": t.get("polarity"),
                "count": int(tag_dist.get(t.get("code") or "", 0)),
            } for t in (active_tags() or [])]
        except Exception:
            tag_stats = [{"code": c, "name": c, "color": None, "polarity": None,
                          "count": n} for c, n in tag_dist.items()]

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
                    "sentiment_stats": {**s_dist, "total": sum(s_dist.values())},
                    "tag_stats": tag_stats}

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
        all_ids = list(main_ids) + [s.id for s in subs]
        pics = await _load_comment_pictures(session, all_ids)
        # 对话目标：按 parent_id 映射出被回复作者的昵称（子评论互回也正确指向，
        # 而非永远指向主评论作者）
        id2author = {c.id: names.get(c.author_id) for c in mains + subs}

        def _pub(c: Comment) -> Dict[str, Any]:
            # 与原站一致：只有回复其他子评论（parent != root）才标对话目标；
            # 直接回复主评论的不显示「回复 @」前缀
            rt = id2author.get(c.parent_id) if (c.parent_id and c.parent_id != c.root_id) else None
            return _comment_public(c, names.get(c.author_id), avatars.get(c.author_id),
                                   urls.get(c.author_id), pics.get(c.id), reply_to=rt)

        for s in subs:
            subs_by_root.setdefault(s.root_id, []).append(_pub(s))

        items = []
        for m in mains:
            payload = _pub(m)
            subs = subs_by_root.get(m.id, [])
            payload["reply_count"] = len(subs)
            payload["replies"] = subs if (reply_limit <= 0 or reply_limit >= len(subs)) else subs[:reply_limit]
            items.append(payload)

        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "stance_stats": {**st_dist, "total": st_total, "agreement": agreement},
                "sentiment_stats": {**s_dist, "total": sum(s_dist.values())},
                "tag_stats": tag_stats}
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

        # 主评论（根）也参与作者解析：顶层子评论回复主评论时，reply_to 指向主评论作者
        root = (await session.execute(
            select(Comment).where(Comment.id == comment_id,
                                  Comment.deleted_at.is_(None))
        )).scalar_one_or_none()

        author_ids = {c.author_id for c in subs if c.author_id}
        if root and root.author_id:
            author_ids.add(root.author_id)
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

        pics = await _load_comment_pictures(session, [s.id for s in subs])
        # 对话目标昵称：按 parent_id 映射（根 + 全部子评论都在映射内）。
        # 与原站一致：只有回复其他子评论（parent != root）才返回 reply_to，
        # 直接回复主评论的不显示「回复 @」前缀
        id2author: Dict[int, Optional[str]] = {s.id: names.get(s.author_id) for s in subs}
        if root:
            id2author[root.id] = names.get(root.author_id)
        items = [_comment_public(s, names.get(s.author_id), avatars.get(s.author_id),
                                urls.get(s.author_id), pics.get(s.id),
                                reply_to=id2author.get(s.parent_id)
                                if (s.parent_id and s.parent_id != s.root_id) else None)
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


@router.post("/{story_id}/tags")
async def add_story_tag(story_id: int,
                        name: str = Query(..., min_length=1, max_length=128)):
    """给帖子加一个自定义标签（详情页标签栏的「+」入口）。

    标签按名字全局复用（tag_type=custom、source_site_id 为空表示自建），
    已存在就只补关联，重复点击不会建出重复标签。
    """
    tag_name = name.strip()
    if not tag_name:
        raise HTTPException(status_code=400, detail="标签名不能为空")
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        story = await session.get(Story, story_id)
        if story is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        tag = (await session.execute(
            select(Tag).where(Tag.name == tag_name, Tag.source_site_id.is_(None))
        )).scalars().first()
        if tag is None:
            tag = Tag(name=tag_name, tag_type="custom")
            session.add(tag)
            await session.flush()
        linked = (await session.execute(
            select(StoryTag).where(StoryTag.story_id == story_id,
                                   StoryTag.tag_id == tag.id)
        )).scalar_one_or_none()
        if linked is None:
            session.add(StoryTag(story_id=story_id, tag_id=tag.id))
        await session.commit()
        return {"status": "ok", "name": tag_name, "tag_id": int(tag.id)}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/tags/list")
async def list_tags(limit: int = Query(50, ge=1, le=500),
                    offset: int = Query(0, ge=0),
                    q: Optional[str] = Query(None, description="标签名模糊搜索")):
    """标签候选：支持分页（懒加载）+ 关键词搜索。

    原先固定按热度取前 50 条且没返回总数，下拉里只能看到一小部分标签，
    也没法滚到底继续加载（2026-10-29）。返回 total 供前端判断还有没有更多。
    """
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0, "offset": offset, "limit": limit}
    try:
        base = select(Tag.name, func.count(StoryTag.story_id)).join(
            StoryTag, StoryTag.tag_id == Tag.id).group_by(Tag.name)
        if q:
            base = base.where(Tag.name.ilike(f"%{q.strip()}%"))
        rows = (await session.execute(
            base.order_by(func.count(StoryTag.story_id).desc())
            .offset(offset).limit(limit)
        )).all()
        # 总数是「去重后的标签个数」，与上面 group_by 口径一致
        total_stmt = select(func.count(func.distinct(Tag.name)))
        if q:
            total_stmt = total_stmt.where(Tag.name.ilike(f"%{q.strip()}%"))
        total = (await session.execute(total_stmt)).scalar() or 0
        return {
            "items": [{"name": n, "count": c} for n, c in rows],
            "total": int(total),
            "offset": offset,
            "limit": limit,
        }
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
                "files", "legacy_rows", "homefeed_rows",
                "s3_purged", "s3_orphaned")


async def _hard_delete_story(session: AsyncSession, story_id: int,
                             purge_files: bool = True) -> Optional[Dict[str, int]]:
    """物理删除一条 story 及其全部关联数据（不可恢复）。

    清理范围：
      v2   story / story_chapter / comment / story_tag / media_attachment
      v1   平台原始表（xhs_note 等）与其评论表 —— 不清会被回填/重拉复活
      v2   homefeed_content（首页流缓存）
      磁盘 附件本地文件，空的内容目录一并清掉
      S3   该帖的备份对象与 media_backup 记录（S3_BACKUP_PURGE_WITH_STORY，
            默认开）—— 原贴没了备份一并删除；删除失败转 orphaned 待重试

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

    # ---- 1.5 S3 备份联动清除（原贴删除 → 备份一并删除）----
    # 在删 media 行之前做：备份记录的 media_id 还指着这些行。
    # 不自行 commit——由调用方（单条/批量删除）统一提交，批量回滚时
    # 备份记录与删除动作保持同一事务语义。
    s3_purged = s3_orphaned = 0
    if media:
        try:
            from tools.media_backup import purge_media_backups, purge_with_story

            if purge_with_story():
                purge_stat = await purge_media_backups(
                    session, [m.id for m in media])
                s3_purged = purge_stat.get("purged", 0)
                s3_orphaned = purge_stat.get("orphaned", 0)
                if s3_orphaned:
                    from tools import utils as _utils

                    _utils.logger.warning(
                        f"[story.delete] story {story_id} 有 {s3_orphaned} 个 "
                        f"S3 对象删除失败，转 orphaned 待重试")
        except Exception as e:
            # 备份清除失败绝不能阻断删帖主流程
            from tools import utils as _utils

            _utils.logger.warning(
                f"[story.delete] S3 备份联动清除异常（不影响删帖）: {e}")

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
    # 备份记录已在 1.5 步联动清除（成功即删记录，失败转 orphaned）
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
            "legacy_rows": legacy, "homefeed_rows": feed,
            "s3_purged": s3_purged, "s3_orphaned": s3_orphaned}


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



# ---------------------------------------------------------------------------
# 帖子编辑记录（P2）
#
# /edits 列表 + /edits/{edit_id} 详情，供详情页「编辑记录」弹层做前后比对。
# ---------------------------------------------------------------------------

@router.get("/{story_id}/edits")
async def list_story_edits(story_id: int, limit: int = Query(50, ge=1, le=200)):
    """某条内容的编辑记录列表（只给摘要，详情单独取）。"""
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0}
    try:
        rows = (await session.execute(
            select(StoryEditLog).where(StoryEditLog.story_id == story_id)
            .order_by(StoryEditLog.edit_ts.desc()).limit(limit)
        )).scalars().all()
        return {
            "items": [{
                "id": e.id,
                "edit_ts": e.edit_ts,
                "changed_fields": e.changed_fields or [],
                "source": e.source,
                "chapter_id": e.chapter_id,
            } for e in rows],
            "total": len(rows),
        }
    finally:
        await session.close()
        await engine.dispose()


@router.get("/{story_id}/edits/{edit_id}")
async def get_story_edit(story_id: int, edit_id: int):
    """单条编辑记录：含 before / after / diff，前端直接双栏比对。"""
    engine, session = await _session()
    if session is None:
        return {"detail": "配置库不可用"}
    try:
        e = await session.get(StoryEditLog, edit_id)
        if e is None or e.story_id != story_id:
            return {"detail": "not found"}
        return {
            "id": e.id,
            "story_id": e.story_id,
            "chapter_id": e.chapter_id,
            "edit_ts": e.edit_ts,
            "changed_fields": e.changed_fields or [],
            "before": e.before or {},
            "after": e.after or {},
            "diff": e.diff or [],
            "source": e.source,
        }
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 评论表态：手动指定 / 修正（P3）
#
# 自动判定难免有误，允许人工改成标签库里任一启用的标签；
# 同时写回 stance（polarity 归并），保证赞同度统计口径不变。
# ---------------------------------------------------------------------------

@router.patch("/comments/{comment_id}/stance")
async def set_comment_stance(comment_id: int, payload: Dict[str, Any] = Body(default={})):
    """手动设置一条评论的态度标签。

    body: {"tag_code": "sarcasm"} 或 {"stance": "agree"}（后者取该极性下的首个标签）
    """
    from database.models_v2 import Comment as _C, StanceTag
    from tools.stance import active_tags, ensure_seeded, load_library

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        await ensure_seeded(session)
        lib = await load_library(session=session)
        ver = lib.get("version") or 0
        tags = [t for t in (active_tags() or [])]

        c = await session.get(_C, comment_id)
        if c is None:
            raise HTTPException(status_code=404, detail="评论不存在")

        code = str(payload.get("tag_code") or "").strip()
        if not code:
            want = str(payload.get("stance") or "").strip()
            hit = next((t for t in tags if t.get("polarity") == want), None)
            if not hit:
                raise HTTPException(status_code=400, detail="tag_code 或 stance 必填")
            code = hit.get("code") or ""

        tag = next((t for t in tags if t.get("code") == code), None)
        if tag is None:
            raise HTTPException(status_code=400, detail=f"未知标签 {code}")

        c.stance_tag_id = tag.get("id")
        c.stance_tag_code = tag.get("code")
        c.stance = tag.get("polarity") or "neutral"
        c.stance_confidence = 1.0            # 人工判定=最高置信
        c.stance_judged_ver = ver
        await session.commit()

        _ensure_tag_maps()
        return {
            "id": c.id,
            "stance": c.stance,
            "stance_tag_code": c.stance_tag_code,
            "stance_tag_name": tag.get("name"),
            "stance_tag_color": tag.get("color"),
        }
    finally:
        await session.close()
        await engine.dispose()


@router.get("/comments/stance-tags")
async def list_comment_stance_tags():
    """评论表态可选标签（供前端下拉 / 筛选栏渲染，含颜色与极性）。"""
    from tools.stance import active_tags, ensure_seeded, load_library

    engine, session = await _session()
    if session is None:
        return {"items": []}
    try:
        await ensure_seeded(session)
        await load_library(session=session)
        return {"items": [{
            "code": t.get("code"), "name": t.get("name"),
            "color": t.get("color"), "polarity": t.get("polarity"),
            "is_fallback": bool(t.get("is_fallback")),
        } for t in (active_tags() or [])]}
    finally:
        await session.close()
        await engine.dispose()

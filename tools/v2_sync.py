# -*- coding: utf-8 -*-
"""v1 → v2 实时同步。

背景：爬虫的 DB store 写的是 v1 遗留表（xhs_note 等 14 张），而 WebUI 的
数据管理页读的是 v2 统一模型（story / story_chapter / comment ...）。
之前两者之间只有一次性迁移脚本（scripts/migrate_jsonl_to_v2.py，扫 jsonl），
导致「抓取成功但数据管理里找不到」。

本模块把迁移脚本里的映射逻辑提炼成可实时调用的 upsert：
爬虫每写入一条 v1 记录，就同步映射一条 v2 记录。

设计约定：
- v2 同步失败**不能**拖垮 v1 主写（v1 是既有事实标准），因此内部使用独立
  session + 独立事务，异常只记日志；
- 幂等 upsert：重复抓取同一条内容时，更新互动数与头像，不产生重复行；
- 与 migrate 脚本的分工：本模块负责「实时增量」，migrate 负责「存量回补」，
  两边映射规则保持一致（migrate 遇到已存在 chapter 会 skip，不会冲突）。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import get_async_engine
from config.db_config import resolve_config_db_type
from database.models_v2 import (
    Author, Comment, MediaAttachment, SourceSite, Story, StoryChapter,
    StoryEditLog, StoryTag, Tag,
)
from tools import utils
from tools.crawler_util import safe_int
from tools.sentiment import analyze_sentiment

#: story.ext 里缓存「发帖人核心观点」的键；判定逻辑升级时改版本号即自动失效重算
STANCE_CACHE_KEY = "stance_info_v4"


def _judge_sentiment(item: Dict[str, Any]) -> str:
    """对一条评论（拍平后的 store dict）做情感判定，返回 positive/neutral/negative。

    各平台内容键名不统一，用 _pick 兼容；判定失败不影响入库（退 neutral）。
    """
    try:
        text = str(_pick(item, _COMMENT_TEXT_KEYS) or "")
        return analyze_sentiment(text)[0]
    except Exception:
        return "neutral"


async def _stance_for(story_id: Optional[int], text: str) -> str:
    """判定一条评论相对发帖人观点的立场（agree/neutral/oppose）。

    story_id 缺失（孤儿评论）或判定失败都退 neutral，不影响入库。
    """
    return (await _stance_detail_for(story_id, text)).get("stance", "neutral")


async def _stance_detail_for(story_id: Optional[int], text: str,
                             sentiment: Optional[str] = None) -> Dict[str, Any]:
    """按态度标签库判定（P3），返回 stance / tag_code / tag_id / confidence。

    标签库每次判定前按版本号刷新，新增或修改标签后下次判定立即生效。
    """
    if not story_id:
        return {"stance": "neutral"}
    try:
        from tools.stance import ensure_seeded, judge_with_library, load_library

        await ensure_seeded()
        lib = await load_library()
        post_stance = await get_story_stance(story_id)
        r = judge_with_library(text or "", post_stance, sentiment)
        r["version"] = lib.get("version")
        return r
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 立场判定失败，退 neutral: {e}")
        return {"stance": "neutral"}

# ---------------------------------------------------------------------------
# session 工厂：与 API 层（api/routers/story.py）使用同一套 db_type 解析，
# 保证爬虫进程、API 进程、独立脚本三种场景下都能连到同一个库。
# 注意不能用 database.db_session.get_session() —— 它只看 config.SAVE_DATA_OPTION，
# 在 API 进程里该值是默认的 "jsonl"，会静默拿到 None session。
# ---------------------------------------------------------------------------


#: 参与编辑比对的字段（数值字段单独用「有变化」判定，避免点赞波动刷记录）
_EDIT_TEXT_FIELDS = ("title", "content_text")
_EDIT_NUM_FIELDS = ("like_count", "comment_count")


def _diff_fields(before: Dict[str, Any], after: Dict[str, Any]) -> list:
    """生成 [{field, before, after, kind}]，前端直接渲染双栏对比。"""
    out = []
    for f in _EDIT_TEXT_FIELDS + _EDIT_NUM_FIELDS:
        b, a = before.get(f), after.get(f)
        if b == a:
            continue
        out.append({
            "field": f,
            "before": "" if b is None else str(b),
            "after": "" if a is None else str(a),
            "kind": "number" if f in _EDIT_NUM_FIELDS else "text",
        })
    return out


async def record_story_edit(session, story_id: int, chapter_id: Optional[int],
                            before: Dict[str, Any], after: Dict[str, Any],
                            source: str = "crawl") -> bool:
    """内容发生变化时写一条编辑记录（P2）。无变化返回 False。"""
    try:
        import config
        if not getattr(config, "ENABLE_STORY_EDIT_LOG", True):
            return False
    except Exception:
        pass
    diff = _diff_fields(before, after)
    if not diff:
        return False
    try:
        session.add(StoryEditLog(
            story_id=story_id,
            chapter_id=chapter_id,
            edit_ts=int(time.time() * 1000),
            changed_fields=[d["field"] for d in diff],
            before={k: before.get(k) for k in before},
            after={k: after.get(k) for k in after},
            diff=diff,
            source=source,
        ))
        story = await session.get(Story, story_id)
        if story is not None:
            story.edited_at = int(time.time() * 1000)
            story.edit_count = int(story.edit_count or 0) + 1
        return True
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 写编辑记录失败 story={story_id}: {e}")
        return False

@asynccontextmanager
async def _v2_session():
    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        yield None
        return
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# 字段兼容表：7 个平台的 store 拍平 dict 键名不统一，集中在这里适配
# ---------------------------------------------------------------------------

_CONTENT_ID_KEYS = ("note_id", "aweme_id", "video_id", "content_id", "id")
_COMMENT_ID_KEYS = ("comment_id", "id")
_COMMENT_TEXT_KEYS = ("content", "content_text", "text", "desc")
_TITLE_KEYS = ("title",)
_TEXT_KEYS = ("desc", "content_text", "content")
_URL_KEYS = ("note_url", "aweme_url", "video_url", "content_url")
_LIKE_KEYS = ("liked_count", "voteup_count", "like_count", "total_replay_num")
_COMMENT_COUNT_KEYS = ("comment_count", "total_replay_num")
_PUBLISH_KEYS = ("time", "create_time", "publish_time", "created_time")
_IMAGE_KEYS = ("image_list",)
_TAG_KEYS = ("tag_list",)
_AVATAR_KEYS = ("author_avatar", "avatar_url")
_NICKNAME_KEYS = ("nickname", "user_nickname")


def _pick(item: Dict[str, Any], keys, default: Any = "") -> Any:
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return default


# 热度公式与 scripts/recompute_heat.py 保持一致（单一事实来源）：
# 点赞×1 + 评论×3 + 收藏×2 + 分享×2，再按发布时间做 30 天衰减。
_W_LIKE, _W_COMMENT, _W_COLLECT, _W_SHARE = 1.0, 3.0, 2.0, 2.0
_DECAY_DAYS = 30.0


def compute_heat_score(like: float = 0, comment: float = 0, collect: float = 0,
                       share: float = 0, publish_ts=None) -> float:
    """计算 story 热度分（数据管理页默认排序字段）。"""
    import time
    raw = like * _W_LIKE + comment * _W_COMMENT + collect * _W_COLLECT + share * _W_SHARE
    age_days = 0.0
    if publish_ts:
        ts = safe_int(publish_ts)
        if ts > 10_000_000_000:
            ts //= 1000
        age_days = max(0.0, (time.time() - ts) / 86400.0)
    return round(raw / (1.0 + age_days / _DECAY_DAYS), 4)


def _split_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value)
    if not text.strip():
        return []
    for sep in (",", "，"):
        if sep in text:
            return [p.strip() for p in text.split(sep) if p.strip()]
    return [text.strip()] if text.strip() else []


# site_id 缓存：SourceSite 是静态注册表，没必要每条记录都查库
_site_id_cache: Dict[str, Optional[int]] = {}
_site_id_lock = asyncio.Lock()


async def _get_site_id(session: AsyncSession, platform_code: str) -> Optional[int]:
    async with _site_id_lock:
        if platform_code in _site_id_cache:
            return _site_id_cache[platform_code]
    row = (await session.execute(
        select(SourceSite).where(SourceSite.code == platform_code)
    )).scalar_one_or_none()
    async with _site_id_lock:
        _site_id_cache[platform_code] = row.id if row else None
    return row.id if row else None


async def _get_or_create_author(session: AsyncSession, site_id: int,
                                anon_hash: str, nickname: str = "",
                                avatar: str = "",
                                profile_url: str = "") -> Optional[int]:
    if not anon_hash:
        return None
    row = (await session.execute(
        select(Author).where(Author.source_site_id == site_id,
                             Author.anon_hash == anon_hash)
    )).scalar_one_or_none()
    if not row:
        row = Author(source_site_id=site_id, anon_hash=anon_hash,
                     nickname=nickname or None, avatar_url=avatar or None,
                     profile_url=profile_url or None)
        session.add(row)
        await session.flush()
    else:
        if avatar and not row.avatar_url:
            row.avatar_url = avatar
        if profile_url and not row.profile_url:
            row.profile_url = profile_url
    return row.id


async def _get_or_create_tag(session: AsyncSession, site_id: int, name: str) -> Optional[int]:
    name = str(name).strip().lstrip("#")
    if not name:
        return None
    row = (await session.execute(
        select(Tag).where(Tag.source_site_id == site_id, Tag.name == name)
    )).scalar_one_or_none()
    if not row:
        row = Tag(source_site_id=site_id, name=name, tag_type="topic")
        session.add(row)
        await session.flush()
    return row.id


async def sync_content_to_v2(platform_code: str, item: Dict[str, Any]) -> bool:
    """把一条内容（拍平后的 store dict）upsert 进 v2。成功返回 True。"""
    content_id = str(_pick(item, _CONTENT_ID_KEYS) or "")
    if not content_id:
        return False
    try:
        async with _v2_session() as session:
            if session is None:
                return False
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                utils.logger.warning(
                    f"[v2_sync] 未注册的站点 {platform_code}，跳过内容 {content_id}")
                return False

            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_site_id == site_id,
                    StoryChapter.source_content_id == content_id)
            )).scalar_one_or_none()

            author_id = await _get_or_create_author(
                session, site_id,
                str(item.get("creator_hash") or ""),
                str(_pick(item, _NICKNAME_KEYS) or ""),
                str(_pick(item, _AVATAR_KEYS) or ""),
                str(item.get("author_url") or ""),
            )
            like_count = safe_int(_pick(item, _LIKE_KEYS) or 0)
            comment_count = safe_int(_pick(item, _COMMENT_COUNT_KEYS) or 0)
            collect_count = safe_int(item.get("collected_count") or 0)
            share_count = safe_int(item.get("share_count") or 0)
            publish_ts = safe_int(_pick(item, _PUBLISH_KEYS) or 0) or None
            heat = compute_heat_score(like_count, comment_count,
                                      collect_count, share_count, publish_ts)
            title = str(_pick(item, _TITLE_KEYS) or "")[:500] or "（无标题）"
            text = str(_pick(item, _TEXT_KEYS) or "")
            url = str(_pick(item, _URL_KEYS) or "") or None

            if chapter is None:
                # ---- 新建：story + chapter + 媒体 + 标签 ----
                story = Story(
                    title=title,
                    summary=text[:2000] or None,
                    cover_url=None,
                    dedupe_key=f"{platform_code}:{content_id}",
                    primary_site_id=site_id,
                    author_id=author_id,
                    chapter_count=1,
                    comment_count=comment_count,
                    like_count=like_count,
                    heat_score=heat,
                    first_publish_ts=publish_ts,
                    last_update_ts=publish_ts,
                    ext={"source_keyword": item.get("source_keyword", "")},
                )
                session.add(story)
                await session.flush()

                chapter = StoryChapter(
                    story_id=story.id,
                    source_site_id=site_id,
                    author_id=author_id,
                    source_content_id=content_id,
                    source_url=url,
                    seq_no=1,
                    title=title,
                    content_text=text or None,
                    like_count=like_count,
                    comment_count=comment_count,
                    share_count=safe_int(item.get("share_count") or 0) or None,
                    collect_count=safe_int(item.get("collected_count") or 0) or None,
                    publish_ts=publish_ts,
                    declared_comment_count=comment_count or None,
                    ext={"type": item.get("type", ""),
                         "video_url": item.get("video_url", ""),
                         # xsec_token 是详情接口的必需参数，重新拉取时要用
                         "xsec_token": item.get("xsec_token", ""),
                         # 抓取时间：每次 upsert 刷新，供增量对比判断
                         # 「新增 / 已刷新 / 未变化」（见 get_content_index）
                         "crawled_at": utils.get_current_timestamp(),
                         "crawl_source": item.get("crawl_source", "")},
                )
                session.add(chapter)
                await session.flush()

                # 全部图片入账（原来硬截断 [:9]，多图笔记会丢图）；
                # 同时探测本地是否已下载（CDN 有防盗链，浏览器直连会 403，
                # 详情页优先读本地文件）
                from media_downloader.paths import build_media_dir
                from tools.paths import DATA_DIR
                media_dir = build_media_dir(DATA_DIR, platform_code, content_id)
                for idx, img in enumerate(_split_list(_pick(item, _IMAGE_KEYS)), 1):
                    img = str(img)
                    if not img.startswith(("http://", "https://")):
                        continue
                    local_path = ""
                    for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
                        candidate = media_dir / f"{idx:03d}{ext}"
                        if candidate.exists():
                            local_path = str(candidate)
                            break
                    session.add(MediaAttachment(
                        target_type="chapter", target_id=chapter.id,
                        media_type="image", source_url=img,
                        local_path=local_path or None,
                        sort_order=idx - 1,
                        download_status="done" if local_path else "pending",
                    ))

                tag_ids = []
                for tag_name in _split_list(_pick(item, _TAG_KEYS))[:10]:
                    tid = await _get_or_create_tag(session, site_id, tag_name)
                    if tid and tid not in tag_ids:
                        tag_ids.append(tid)
                for kw in _split_list(item.get("source_keyword"))[:3]:
                    tid = await _get_or_create_tag(session, site_id, kw)
                    if tid and tid not in tag_ids:
                        tag_ids.append(tid)
                for tid in tag_ids:
                    session.add(StoryTag(story_id=story.id, tag_id=tid))
            else:
                # ---- 已存在：刷新互动数与头像（重复抓取同一条内容） ----
                story = (await session.execute(
                    select(Story).where(Story.id == chapter.story_id)
                )).scalar_one_or_none()
                # 编辑记录：先快照旧值，改完再比对（P2）
                _edit_before = {
                    "title": story.title if story else None,
                    "content_text": chapter.content_text,
                    "like_count": chapter.like_count,
                    "comment_count": chapter.comment_count,
                }
                if story is not None:
                    story.like_count = like_count
                    story.comment_count = comment_count
                    story.last_update_ts = publish_ts
                    story.heat_score = compute_heat_score(
                        like_count, comment_count, collect_count, share_count,
                        publish_ts or story.first_publish_ts)
                    if title != "（无标题）":
                        story.title = title
                    if author_id and not story.author_id:
                        story.author_id = author_id
                chapter.like_count = like_count
                chapter.comment_count = comment_count
                if publish_ts:
                    chapter.publish_ts = publish_ts
                # source_url 有新值就覆盖：早期数据/测试数据的 URL 可能缺
                # xsec_token（详情接口必需），不覆盖会导致重新拉取必然失败
                if url:
                    chapter.source_url = url
                if text:
                    chapter.content_text = text
                # xsec_token 刷新进 ext
                token = str(item.get("xsec_token") or "")
                ext = dict(chapter.ext or {})
                if token:
                    ext["xsec_token"] = token
                # 抓取时间每次刷新：执行前后对比 ext.crawled_at 即可
                # 区分「新增 / 已刷新 / 未变化」（见 get_content_index）
                ext["crawled_at"] = utils.get_current_timestamp()
                if item.get("crawl_source"):
                    ext["crawl_source"] = item.get("crawl_source")
                chapter.ext = ext

                # ---- 媒体补齐：存量记录当初可能没写全（旧版有 [:9] 截断、
                # 早期版本没探测本地文件），这里按 source_url 去重补齐 ----
                existing_urls = {row[0] for row in (await session.execute(
                    select(MediaAttachment.source_url).where(
                        MediaAttachment.target_type == "chapter",
                        MediaAttachment.target_id == chapter.id)
                )).all()}
                if len(existing_urls) != len(_split_list(_pick(item, _IMAGE_KEYS))):
                    from media_downloader.paths import build_media_dir
                    from tools.paths import DATA_DIR
                    media_dir = build_media_dir(DATA_DIR, platform_code, content_id)
                    for idx, img in enumerate(_split_list(_pick(item, _IMAGE_KEYS)), 1):
                        img = str(img)
                        if img in existing_urls or not img.startswith(("http://", "https://")):
                            continue
                        local_path = ""
                        for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
                            candidate = media_dir / f"{idx:03d}{ext}"
                            if candidate.exists():
                                local_path = str(candidate)
                                break
                        session.add(MediaAttachment(
                            target_type="chapter", target_id=chapter.id,
                            media_type="image", source_url=img,
                            local_path=local_path or None,
                            sort_order=idx - 1,
                            download_status="done" if local_path else "pending",
                        ))

                # 编辑记录：与抓取前的快照比对，有差异才落一条
                if story is not None:
                    await record_story_edit(
                        session, story.id, chapter.id, _edit_before,
                        {"title": story.title,
                         "content_text": chapter.content_text,
                         "like_count": chapter.like_count,
                         "comment_count": chapter.comment_count},
                        source=str(item.get("crawl_source") or "crawl"))

            await session.commit()
            return True
    except Exception as e:  # v2 是衍生数据，失败不影响 v1 主写
        utils.logger.warning(f"[v2_sync] 内容同步失败 {platform_code}/{content_id}: {e}")
        return False


async def get_content_index(platform_code: str, user_id: str = "",
                            content_id: str = "") -> Dict[str, int]:
    """读取某作者（或单条内容）在 v2 里的内容索引：{source_content_id: crawled_at}。

    用途：
    1. 主页增量拉取前预读「已知帖子」集合，翻到整页已知就停止翻页；
    2. 执行前后各取一次，对比得到新增 / 已刷新 / 未变化条数。

    crawled_at 是每次内容 upsert 时写入的抓取时间（epoch ms），
    值发生变化即代表这条内容本轮被重新抓取过。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return {}
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return {}
            stmt = select(StoryChapter.source_content_id, StoryChapter.ext).where(
                StoryChapter.source_site_id == site_id)
            if content_id:
                stmt = stmt.where(StoryChapter.source_content_id == str(content_id))
            elif user_id:
                from tools.user_hash import anonymize_user_id

                author_id = (await session.execute(
                    select(Author.id).where(
                        Author.source_site_id == site_id,
                        Author.anon_hash == anonymize_user_id(user_id))
                )).scalar_one_or_none()
                if author_id is None:
                    return {}
                stmt = stmt.where(StoryChapter.author_id == author_id)
            else:
                return {}
            rows = (await session.execute(stmt)).all()
            index: Dict[str, int] = {}
            for cid, ext in rows:
                ts = 0
                if isinstance(ext, dict):
                    try:
                        ts = int(ext.get("crawled_at") or 0)
                    except (TypeError, ValueError):
                        ts = 0
                index[str(cid)] = ts
            return index
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 读取内容索引失败 {platform_code}: {e}")
        return {}


async def backfill_comment_sentiments(story_id: Optional[int] = None,
                                      limit: int = 20000) -> int:
    """给 sentiment 为空的评论批量补情感判定（存量数据一次性回填）。

    情感是评论区筛选/好评度统计的基础字段，老数据入库时还没有这一列语义，
    这里按需补齐。幂等：只处理 sentiment 为 NULL 的行。
    """
    updated = 0
    try:
        async with _v2_session() as session:
            if session is None:
                return 0
            stmt = select(Comment).where(Comment.sentiment.is_(None))
            if story_id is not None:
                stmt = stmt.where(Comment.story_id == story_id)
            rows = (await session.execute(stmt.limit(limit))).scalars().all()
            for c in rows:
                try:
                    c.sentiment = analyze_sentiment(c.content_text or "")[0]
                    updated += 1
                except Exception:
                    c.sentiment = "neutral"
            await session.commit()
            if updated:
                utils.logger.info(f"[v2_sync] 情感回填完成：{updated} 条")
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 情感回填失败: {e}")
    return updated


async def get_story_stance(story_id: int) -> Dict[str, Any]:
    """读取（缺省时现算并缓存）发帖人核心观点。

    帖子立场存 story.ext.stance；老数据没有就先算后写，后续查询直接用。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return {"stance": "neutral", "keywords": [], "thesis": "", "confidence": 0.0}
            story = await session.get(Story, story_id)
            if story is None:
                return {"stance": "neutral", "keywords": [], "thesis": "", "confidence": 0.0}
            ext = dict(story.ext or {})
            # 缓存键带版本号：判定逻辑升级（话题画像剥离 #话题#、相关性放宽）
            # 后必须重算，否则老缓存一直生效
            cached = ext.get(STANCE_CACHE_KEY)
            if isinstance(cached, dict) and cached.get("stance"):
                return cached
            # 现算：标题 + 正文首章
            chapter = (await session.execute(
                select(StoryChapter).where(StoryChapter.story_id == story_id)
                .order_by(StoryChapter.seq_no.asc()).limit(1)
            )).scalar_one_or_none()
            from tools.stance import extract_post_stance

            info = extract_post_stance(
                title=story.title or "",
                content=(chapter.content_text if chapter else "") or "")
            ext[STANCE_CACHE_KEY] = info
            story.ext = ext
            await session.commit()
            return info
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 读取帖子立场失败 story={story_id}: {e}")
        return {"stance": "neutral", "keywords": [], "thesis": "", "confidence": 0.0}


async def backfill_comment_stances(story_id: Optional[int] = None,
                                   limit: int = 20000) -> int:
    """批量回填评论立场（相对发帖人观点的 agree/neutral/oppose）。

    流程：先确保每个 story 的帖子立场已缓存，再逐条判定评论立场。
    幂等：只处理 stance 为 NULL 的评论。
    """
    from tools.stance import judge_comment_stance

    updated = 0
    try:
        async with _v2_session() as session:
            if session is None:
                return 0
            stmt = select(Comment).where(Comment.stance.is_(None))
            if story_id is not None:
                stmt = stmt.where(Comment.story_id == story_id)
            rows = (await session.execute(stmt.limit(limit))).scalars().all()
            if not rows:
                return 0

            # 按 story 分组，每组只算一次帖子立场
            by_story: Dict[int, List] = {}
            for c in rows:
                by_story.setdefault(c.story_id, []).append(c)

            # 标签库：先确保种子已写入并载入内存，整批共用一个版本
            try:
                from tools.stance import (ensure_seeded, judge_with_library,
                                          load_library)
                await ensure_seeded(session)
                lib = await load_library(session=session)
                tags = lib.get("tags") or []
                ver = lib.get("version") or 0
            except Exception:
                tags, ver = [], 0

            for sid, comments in by_story.items():
                post_stance = await get_story_stance(sid)
                for c in comments:
                    try:
                        if tags:
                            r = judge_with_library(
                                c.content_text or "", post_stance, c.sentiment,
                                tags=tags)
                        else:
                            r = judge_comment_stance(
                                c.content_text or "", post_stance, c.sentiment)
                        c.stance = r.get("stance", "neutral")
                        # 标签库字段（P3）：供筛选 / 徽标展示
                        c.stance_tag_id = r.get("tag_id")
                        c.stance_tag_code = r.get("tag_code")
                        try:
                            c.stance_confidence = r.get("confidence")
                        except Exception:
                            pass
                        c.stance_judged_ver = ver or None
                        updated += 1
                    except Exception:
                        c.stance = "neutral"
            await session.commit()
            if updated:
                utils.logger.info(f"[v2_sync] 立场回填完成：{updated} 条")
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 立场回填失败: {e}")
    return updated


async def sync_comment_to_v2(platform_code: str, item: Dict[str, Any]) -> bool:
    """把一条评论 upsert 进 v2。成功返回 True。"""
    comment_id = str(_pick(item, _COMMENT_ID_KEYS) or "")
    if not comment_id:
        return False
    try:
        async with _v2_session() as session:
            if session is None:
                return False
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return False

            existing = (await session.execute(
                select(Comment).where(Comment.source_site_id == site_id,
                                      Comment.source_comment_id == comment_id)
            )).scalar_one_or_none()

            author_id = await _get_or_create_author(
                session, site_id,
                str(item.get("creator_hash") or ""),
                str(_pick(item, _NICKNAME_KEYS) or ""),
                str(_pick(item, _AVATAR_KEYS) or ""),
                str(item.get("author_url") or ""),
            )

            if existing is not None:
                # 刷新点赞 / 子评论数 / 头像
                existing.like_count = safe_int(item.get("like_count") or 0)
                existing.sub_count = safe_int(item.get("sub_comment_count") or 0)
                if author_id and not existing.author_id:
                    existing.author_id = author_id
                # 老数据没有情感判定：补上（后续抓取都会带）
                if not existing.sentiment:
                    existing.sentiment = _judge_sentiment(item)
                # 老数据没有立场判定：补上（相对发帖人观点的赞同/中立/反对）
                if not existing.stance:
                    existing.stance = await _stance_for(
                        existing.story_id, str(item.get("content") or ""))
                # 老数据没有标签：按最新标签库补判（P3）
                if not existing.stance_tag_code:
                    _d = await _stance_detail_for(
                        existing.story_id, str(item.get("content") or ""),
                        existing.sentiment)
                    existing.stance_tag_id = _d.get("tag_id")
                    existing.stance_tag_code = _d.get("tag_code")
                    existing.stance_confidence = _d.get("confidence")
                    existing.stance_judged_ver = _d.get("version")

                await session.commit()
                return True

            # 找到评论所属的 story / chapter
            content_id = str(_pick(item, ("note_id", "content_id")) or "")
            chapter = None
            if content_id:
                chapter = (await session.execute(
                    select(StoryChapter).where(
                        StoryChapter.source_site_id == site_id,
                        StoryChapter.source_content_id == content_id)
                )).scalar_one_or_none()

            if chapter is not None:
                story_id = chapter.story_id
                chapter_id = chapter.id
            else:
                # 评论先于内容到达：挂到兜底 story，等内容到了再由回补脚本修正
                story_id = await _ensure_orphan_story(session, site_id, platform_code, content_id)
                chapter_id = None

            _c_text = str(item.get("content") or "")
            _c_sent = _judge_sentiment(item)
            _c_st = await _stance_detail_for(story_id, _c_text, _c_sent)
            session.add(Comment(
                story_id=story_id,
                chapter_id=chapter_id,
                source_site_id=site_id,
                author_id=author_id,
                source_comment_id=comment_id,
                parent_id=None,
                root_id=None,
                depth=0,
                content_text=str(item.get("content") or ""),
                like_count=safe_int(item.get("like_count") or 0),
                sub_count=safe_int(item.get("sub_comment_count") or 0),
                publish_ts=safe_int(_pick(item, ("create_time", "publish_time")) or 0) or None,
                sentiment=_c_sent,
                stance=_c_st.get("stance", "neutral"),
                stance_tag_id=_c_st.get("tag_id"),
                stance_tag_code=_c_st.get("tag_code"),
                stance_confidence=_c_st.get("confidence"),
                stance_judged_ver=_c_st.get("version"),
                # parent 原始 ID 先落 ext，等父评论到达后由 backfill_comment_links 回填
                ext={"parent_comment_id": str(item.get("parent_comment_id") or "")},
            ))
            await session.commit()
            return True
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 评论同步失败 {platform_code}/{comment_id}: {e}")
        return False


async def _ensure_orphan_story(session: AsyncSession, site_id: int,
                               platform_code: str, content_id: str) -> int:
    dedupe = f"{platform_code}:{content_id}"
    row = (await session.execute(
        select(Story).where(Story.dedupe_key == dedupe)
    )).scalar_one_or_none()
    if row:
        return row.id
    row = Story(title=f"（内容待补）{content_id}", dedupe_key=dedupe,
                primary_site_id=site_id, status="active")
    session.add(row)
    await session.flush()
    return row.id


async def backfill_comment_links(platform_code: str) -> int:
    """回填评论的 parent_id / root_id / depth。

    实时同步时评论常常先于其父评论到达（父评论可能还没抓到），
    这里在抓完一批后统一修补。幂等，可反复执行。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return 0
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return 0
            # 只加载「待回填」的评论 + 被引用为父的评论，别把整表读进内存
            pending = (await session.execute(
                select(Comment).where(
                    Comment.source_site_id == site_id,
                    Comment.ext.isnot(None),
                ).order_by(Comment.id)
            )).scalars().all()
            pending = [c for c in pending if (c.ext or {}).get("parent_comment_id")]
            if not pending:
                return 0

            parent_srcs = {(c.ext or {})["parent_comment_id"] for c in pending}
            parents = (await session.execute(
                select(Comment).where(
                    Comment.source_site_id == site_id,
                    Comment.source_comment_id.in_(parent_srcs),
                )
            )).scalars().all()
            by_src = {p.source_comment_id: p for p in parents}

            # 多轮收敛：父评论自己也可能待回填，一轮下来它的 depth 还是 0，
            # 直接算会给子评论算错层级（多级嵌套尤其容易错）。
            updated = 0
            for _round in range(5):
                batch = 0
                for c in pending:
                    if c.parent_id is not None:
                        continue
                    parent_src = (c.ext or {}).get("parent_comment_id")
                    parent = by_src.get(str(parent_src)) if parent_src else None
                    if parent is None or (
                        parent.parent_id is None and (parent.ext or {}).get("parent_comment_id")
                    ):
                        # 父自己也还待回填，等下一轮再定层级
                        continue
                    c.parent_id = parent.id
                    c.root_id = parent.root_id or parent.id
                    c.depth = int(parent.depth or 0) + 1
                    c.ext = {}      # 用完即清；ext 非空列，不能置 None
                    batch += 1
                updated += batch
                if batch == 0:
                    break
            await session.commit()
            return updated
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 评论树回填失败 {platform_code}: {e}")
        return 0


# ---------------------------------------------------------------------------
# jsonl 镜像：DB 存储模式下同时落一份 jsonl，供 WebUI「数据浏览」页读取
# （该页扫的是 data/<platform>/jsonl/*.jsonl，纯 DB 模式下不会产生这些文件）
# ---------------------------------------------------------------------------

_jsonl_writers: Dict[tuple, Any] = {}


async def mirror_to_jsonl(platform: str, item: Dict[str, Any], kind: str) -> None:
    import config
    from tools.async_file_writer import AsyncFileWriter
    from var import crawler_type_var

    if config.SAVE_DATA_OPTION not in ("db", "postgres", "sqlite"):
        return
    try:
        key = (platform, crawler_type_var.get())
        writer = _jsonl_writers.get(key)
        if writer is None:
            writer = AsyncFileWriter(platform=platform,
                                     crawler_type=crawler_type_var.get())
            _jsonl_writers[key] = writer
        await writer.write_to_jsonl(
            item=item,
            item_type="contents" if kind == "content" else "comments")
    except Exception as e:
        utils.logger.warning(f"[v2_sync] jsonl 镜像失败 {platform}: {e}")


async def persist_to_v2(platform_code: str, item: Dict[str, Any], kind: str) -> bool:
    """store 层统一调用入口：v2 模型同步 + jsonl 镜像。

    kind: "content" | "comment"
    任何失败都只记日志，绝不影响 v1/原始存储主流程。
    """
    if kind == "content":
        ok = await sync_content_to_v2(platform_code, item)
        await mirror_to_jsonl(platform_code, item, "content")
        return ok
    ok = await sync_comment_to_v2(platform_code, item)
    await mirror_to_jsonl(platform_code, item, "comment")
    return ok


async def mark_story_deleted(platform_code: str, content_id: str) -> bool:
    """平台侧内容已被删除时，把本地 story 标记为 deleted。

    调用时机：爬虫重新抓取该内容时平台返回空（note 不存在/已删），
    由平台 core 在拿到空结果后调用。标记后数据仍保留在库里可查，
    前端会显示「已删除」徽标，而不是从列表里悄悄消失。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return False
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return False
            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_site_id == site_id,
                    StoryChapter.source_content_id == str(content_id))
            )).scalar_one_or_none()
            if chapter is None:
                return False
            story = (await session.execute(
                select(Story).where(Story.id == chapter.story_id)
            )).scalar_one_or_none()
            if story is None or story.status == "deleted":
                return False
            story.status = "deleted"
            await session.commit()
            utils.logger.info(
                f"[v2_sync] 平台内容已删除，标记 story {story.id} ({platform_code}/{content_id})")
            return True
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 标记删除失败 {platform_code}/{content_id}: {e}")
        return False

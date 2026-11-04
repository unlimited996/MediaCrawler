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
import mimetypes
import random
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update, delete, or_
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import get_async_engine
from config.db_config import resolve_config_db_type
from database.models_v2 import (
    Author, Comment, MediaAttachment, SourceSite, Story, StoryChapter,
    StoryEditLog, StoryTag, Tag,
)
import config
import httpx

from tools import utils
from tools.crawler_util import safe_int
from tools.paths import DATA_DIR
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


async def _load_media_index(session, chapter_id: int) -> Dict[str, MediaAttachment]:
    """已有媒体按 source_url 建索引（用于只增不删的 upsert）。"""
    rows = (await session.execute(
        select(MediaAttachment).where(
            MediaAttachment.target_type == "chapter",
            MediaAttachment.target_id == chapter_id)
    )).scalars().all()
    return {m.source_url: m for m in rows if m.source_url}


async def _upsert_media(session, chapter_id: int, url: str, idx: int,
                        local_path: str, index: Dict[str, MediaAttachment],
                        platform_code: str = "", content_id: str = "",
                        story_id: Optional[int] = None) -> str:
    """媒体入库：只新增，不删除。

    - 新 URL → INSERT（带 storage_type / source_status / downloaded_at）
    - 已有 URL → 只补空字段（local_path / checksum / 存储类型），
      本次抓取没带回来的历史媒体永不删除（story 硬删除才级联）
    - 入库后顺手入 S3 备份队列（P4，无启用配置时自动跳过）
    """
    lp = local_path or None
    existing = index.get(url)
    if existing is not None:
        # 本地文件后来才下载完：回补路径，不动其它字段
        if lp and not existing.local_path:
            existing.local_path = lp
            existing.storage_type = "local"
            existing.download_status = "done"
            try:
                existing.size_bytes = Path(lp).stat().st_size
                existing.mime_type = (mimetypes.guess_type(lp)[0]
                                      or existing.mime_type or "image/jpeg")
            except Exception:
                pass
            try:
                from sqlalchemy import text as _text
                existing.downloaded_at = (await session.execute(
                    _text("SELECT now()"))).scalar()
            except Exception:
                pass
        # 存量/新补的媒体都确保在备份队列里（幂等，media_id 判重）
        if platform_code:
            try:
                from tools.media_backup import enqueue_backup

                await enqueue_backup(session, existing, platform_code,
                                     content_id, story_id)
            except Exception:
                pass
        return "updated"

    media = MediaAttachment(
        target_type="chapter", target_id=chapter_id,
        media_type="image", source_url=url,
        local_path=lp,
        sort_order=idx - 1,
        download_status="done" if lp else "pending",
        storage_type="local" if lp else "url",
        source_status="alive" if url else "unknown",
    )
    session.add(media)
    await session.flush()   # 拿 id 供备份队列引用
    index[url] = media
    if platform_code:
        try:
            from tools.media_backup import enqueue_backup

            await enqueue_backup(session, media, platform_code,
                                 content_id, story_id)
        except Exception:
            pass
    return "inserted"


# ---------------------------------------------------------------------------
# 评论图片：入库 + S3 备份（target_type='comment'）
# 评论的图片在 v1 仅以 pictures 文本形式存在，v2 之前完全没落盘/备份；
# 这里在评论同步时把每张图下载到本地、登记 media_attachment，再入 S3 备份队列。
# 兼容三种 pictures 来源：实时抓取 [{url_default,url}]、PDF 导入/旧数据逗号分隔串、纯字符串列表。
# ---------------------------------------------------------------------------

def _normalize_picture_urls(pictures: Any) -> List[str]:
    """把 pictures 字段归一化成 http(s) URL 列表。"""
    out: List[str] = []
    if not pictures:
        return out
    if isinstance(pictures, str):
        for part in pictures.split(","):
            part = part.strip()
            if part.startswith(("http://", "https://")):
                out.append(part)
        return out
    if isinstance(pictures, (list, tuple)):
        for p in pictures:
            if isinstance(p, str):
                if p.startswith(("http://", "https://")):
                    out.append(p)
            elif isinstance(p, dict):
                url = p.get("url_default") or p.get("url") or ""
                if url.startswith(("http://", "https://")):
                    out.append(url)
    return out


# 评论图下载 UA：与 API 请求头刻意保持一致（Windows + Chrome 137），
# 避免「同一会话两种指纹」被平台关联识别。
_COMMENT_IMAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)


async def _download_comment_image(url: str, dest: Path,
                                  client: Optional[httpx.AsyncClient] = None
                                  ) -> Optional[Path]:
    """尽力下载评论图片到本地（抓取时 URL 尚新鲜）。失败返回 None，不抛异常。

    client 可复用：由调用方持有单个 AsyncClient，避免每张图都新建/销毁连接
    （之前每张图一个独立 client，虽然串行，但建连开销大、指纹也不稳定）。
    """
    try:
        from media_downloader.paths import guess_extension

        ext = guess_extension(url, default=".jpg")
        dest = dest.with_suffix(ext) if not dest.suffix else dest
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        own = client is None
        if own:
            client = httpx.AsyncClient(follow_redirects=True, timeout=15.0)
        try:
            r = await client.get(
                url,
                headers={
                    "User-Agent": _COMMENT_IMAGE_UA,
                    "Referer": "https://www.xiaohongshu.com/",
                },
            )
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
                return dest
        finally:
            if own and client is not None:
                await client.aclose()
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 评论图下载失败 {url[:80]}: {e}")
    return None


async def _upsert_comment_media(session, comment_id: int, url: str, idx: int,
                                local_path: str, platform_code: str,
                                content_id: str, story_id: Optional[int]) -> None:
    """评论媒体入库（按 source_url / local_path 幂等）+ 入 S3 备份队列。

    去重说明：同一张评论图在「重新抓取」时 XHS 会下发带新 CDN 签名的 URL，
    source_url 因此变化，仅靠 (target_type, target_id, source_url) 唯一约束拦不住，
    会重复建媒体行并重复备份。但下载落盘文件名是确定性的
    （{comment_id}_{idx:03d}），同一张图永远落到同一个 local_path，所以额外按
    local_path 命中已存在行即可彻底去重。
    """
    conditions = [
        MediaAttachment.target_type == "comment",
        MediaAttachment.target_id == comment_id,
    ]
    if local_path:
        conditions.append(
            (MediaAttachment.source_url == url)
            | (MediaAttachment.local_path == local_path)
        )
    else:
        conditions.append(MediaAttachment.source_url == url)
    existing = (await session.execute(
        select(MediaAttachment).where(*conditions)
    )).scalar_one_or_none()
    if existing is not None:
        # 刷新为最新 CDN 签名 URL（若本次重抓拿到新签名），保留本地副本不变
        if url and existing.source_url != url:
            existing.source_url = url
            existing.source_status = "alive"
        if local_path and not existing.local_path:
            existing.local_path = local_path
            existing.download_status = "done"
            existing.storage_type = "local"
        if platform_code:
            try:
                from tools.media_backup import enqueue_backup

                await enqueue_backup(session, existing, platform_code,
                                     content_id, story_id)
            except Exception:
                pass
        return
    media = MediaAttachment(
        target_type="comment", target_id=comment_id,
        media_type="image", source_url=url, local_path=local_path or None,
        sort_order=idx - 1,
        download_status="done" if local_path else "pending",
        storage_type="local" if local_path else "url",
        source_status="alive" if url else "unknown",
    )
    session.add(media)
    await session.flush()
    if platform_code:
        try:
            from tools.media_backup import enqueue_backup

            await enqueue_backup(session, media, platform_code,
                                 content_id, story_id)
        except Exception:
            pass


async def _sync_comment_media(session, comment_id: int, pictures: Any,
                              platform_code: str, content_id: str,
                              story_id: Optional[int]) -> None:
    """评论同步时把评论图片下载/登记/入 S3 备份队列。

    幂等：重复同步同一评论不会重复建媒体行（按 source_url 唯一约束）。
    """
    if not getattr(config, "ENABLE_COMMENT_IMAGE_BACKUP", True):
        return
    urls = _normalize_picture_urls(pictures)
    if not urls:
        return
    from media_downloader.paths import build_media_dir

    media_dir = (build_media_dir(DATA_DIR, platform_code, content_id)
                 / "comments" / str(comment_id))
    stem_base = str(comment_id)
    # 复用单个 client：评论图之间加小随机间隔，避免抓取期零间隔密集请求
    # （之前每张图独立 client 且零间隔，是被风控的隐藏放大器之一）。
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        for idx, url in enumerate(urls, 1):
            local_path = ""
            lp = await _download_comment_image(
                url, media_dir / f"{stem_base}_{idx:03d}", client)
            if lp is not None:
                local_path = str(lp)
            await _upsert_comment_media(session, comment_id, url, idx, local_path,
                                        platform_code, content_id, story_id)
            if idx < len(urls):
                await asyncio.sleep(random.uniform(0.3, 1.2))


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


def comment_completeness(fetched: int, declared: int) -> float:
    """评论完整率 = 已抓评论数 / 原帖显示评论量。

    declared（平台声明的评论数）为 0/缺失时返回 1.0：没有评论需求的帖子
    天然算「抓全了」，不该被反复重抓。
    """
    if declared <= 0:
        return 1.0
    return max(0.0, min(1.0, float(fetched) / float(declared)))


def is_comment_complete(fetched: int, declared: int,
                        target_ratio: float = 0.0) -> bool:
    """评论是否已抓够：fetched / declared >= target_ratio（默认配置的 90%）。

    这是增量任务「跳过 / 重抓」的唯一判据 —— 不再有「最多重试几次」的
    硬上限，抓没抓全由数据本身回答。
    """
    if target_ratio <= 0:
        target_ratio = float(getattr(config, "COMMENT_COMPLETENESS_TARGET", 0.0) or 0.9)
    return comment_completeness(fetched, declared) >= target_ratio


def is_comment_complete_full(fetched: int, declared: int, ext: Any = None,
                             target_ratio: float = 0.0) -> bool:
    """完整率判据（含 ext 里的策略性标记），增量跳过判定的统一入口。

    三条命中任一即算「完成、可跳过」：
      1) 已抓/声明 >= 目标比例（正常路径）；
      2) ext[COMMENT_FULLY_FETCHED_KEY]：评论已翻到 has_more=False 抓全 ——
         平台声明数常虚高（含已删/折叠），可抓的都抓到了，直接视为 100%，
         否则这类帖会每轮反复重抓、白打请求且容易撞 461；
      3) 已抓 >= 分母：平台少报声明数时，抓到的比声明还多，同样算完成。

    注意：`is_comment_complete` 仍是纯比例判据（单测覆盖）；本函数把它与
    两条 ext/数量兜底合并，供 get_archived_content_ids / get_backfill_candidates
    统一调用，避免两处逻辑漂移。
    """
    if is_comment_complete(fetched, declared, target_ratio):
        return True
    ext = ext or {}
    if ext.get(COMMENT_FULLY_FETCHED_KEY, False):
        return True
    if declared > 0 and fetched >= declared:
        return True
    return False



def plan_backfill(incomplete: List[tuple], backfill_limit: int = 0,
                  backfill_max_comments: int = 0):
    """把「未达标帖子」切成 (本轮补抓, 顺延下一轮) 两份。

    incomplete 的元素是 (content_id, fetched, declared, ratio)。

    挑选规则：
      1. 差得越少越先补（declared-fetched 升序）—— 差 16 条的几分钟就能达标，
         差 8000 条的要跑几小时；先啃小的整体达标率上升更快。
      2. backfill_limit > 0：最多挑这么多篇。
      3. backfill_max_comments > 0：本轮「待补条数」累计不超过该预算
         （第一条永远放行，避免单篇超大帖把预算卡成 0 篇死锁）。

    注意：顺延不是放弃 —— 这些帖子的完整率仍 < 阈值，下一轮照样进候选池。
    """
    # 优先级铁律：**评论为 0 的帖子（fetched=0）最优先** —— 它们才是真正
    # 「没抓到评论」的坏数据，用户显式要求先补它们。否则按「差多少」升序，
    # 差得少的先补（快速达标）。
    # 这样评论为空的帖子不再被「快满的帖子」永远压在队列底部饿死：每轮先啃
    # 一批空评论帖（同档内按声明数升序，小帖先补、快速出成果），大帖（声明
    # 几千条）自然排在同档末尾，待小帖清空后轮到它，而不是被无限顺延。
    ordered = sorted(incomplete, key=lambda x: (0 if x[1] == 0 else 1, x[2] - x[1], x[2]))
    selected = ordered
    if backfill_limit > 0:
        selected = selected[:backfill_limit]
    if backfill_max_comments > 0:
        picked, acc = [], 0
        for row in selected:
            gap = max(0, row[2] - row[1])
            if picked and acc + gap > backfill_max_comments:
                break
            picked.append(row)
            acc += gap
        selected = picked
    return selected, ordered[len(selected):]


async def get_archived_content_ids(platform_code: str,
                                   target_ratio: float = 0.0,
                                   backfill_limit: int = 0,
                                   backfill_max_comments: int = 0) -> set:
    """返回该平台「已归档完整」的帖子 ID 集合（story_chapter.source_content_id）。

    account 模式（点赞/收藏）用它做增量：已归档的帖子不再重抓详情+评论。

    完整性判定（唯一标准）：
    **已抓评论数 / 原帖显示评论量 >= target_ratio（默认 90%）**。

    这里不再有「零评论帖最多重试 N 次」这种拍脑袋的上限（2026-10-28 用户
    明确否掉）：是否重抓完全由数据说话 —— 评论抓够了就跳过，没抓够就补。
    上一轮撞验证码只存了正文的帖子，完整率是 0%，会一直留在待补池里。

    backfill_limit / backfill_max_comments > 0 时限流：本轮只挑差得最少的
    若干篇（并受总条数预算约束）去补，其余不达标的帖子本轮先跳过（仍在待补
    池里，下一轮继续），避免 100+ 篇、单篇几千条评论一次性打出去再次触发
    风控。**这不是重试次数上限** —— 没有任何帖子会因为「试过几次」被永久放弃。

    两条排除规则：
    1. 已标 deleted / hidden 的帖子不算归档完成 —— 可能只是临时不可见。
    2. 平台声明评论数为 0/缺失时视为完整（`comment_completeness` 返回 1.0）。

    查询失败（数据库不可达）时返回空集合，调用方按「全部都要抓」处理。
    """
    if target_ratio <= 0:
        target_ratio = float(getattr(config, "COMMENT_COMPLETENESS_TARGET", 0.0) or 0.9)
    try:
        async with _v2_session() as session:
            if session is None:
                return set()
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return set()
            # 一条查询拿全：帖子 ID + 状态 + 声明评论数 + 已抓评论数（避免逐篇 N+1）
            rows = (await session.execute(
                select(StoryChapter.source_content_id, Story.status, Story.id,
                       StoryChapter.declared_comment_count,
                       StoryChapter.comment_count,
                       Story.comment_count,
                       StoryChapter.ext,
                       func.count(Comment.id))
                .join(Story, Story.id == StoryChapter.story_id, isouter=True)
                .outerjoin(Comment, Comment.story_id == Story.id)
                .where(StoryChapter.source_site_id == site_id)
                .group_by(StoryChapter.source_content_id, Story.status, Story.id,
                          StoryChapter.declared_comment_count,
                          StoryChapter.comment_count,
                          Story.comment_count, StoryChapter.ext)
            )).all()

            ids = set()
            incomplete: List[tuple] = []    # (content_id, fetched, declared, ratio)
            for (content_id, status, story_id, declared_c, chapter_c,
                 story_c, ext, cmt_count) in rows:
                if not content_id or story_id is None:
                    continue
                if (status or "") in ("deleted", "hidden"):
                    continue
                # 声明数优先级：章节字段（详情接口原值）> 章节冗余 > story 冗余
                declared = int(declared_c or chapter_c or story_c or 0)
                # 扣掉「楼中楼深度上限主动放弃」的条数：这些是我们策略性不抓的，
                # 留在分母里会让帖子永远凑不满目标比例、每轮被当成未达标重抓，
                # 而重抓只能再翻同样的几页 —— 纯浪费请求且毫无进展。
                declared = max(0, declared - int(
                    (ext or {}).get(COMMENT_SKIPPED_KEY, 0) or 0))
                fetched = int(cmt_count or 0)
                if is_comment_complete_full(fetched, declared, ext, target_ratio):
                    ids.add(str(content_id))
                else:
                    incomplete.append((str(content_id), fetched, declared,
                                       comment_completeness(fetched, declared)))

            if incomplete:
                selected, deferred = plan_backfill(
                    incomplete, backfill_limit, backfill_max_comments)
                if deferred:
                    # 本轮暂缓的帖子先当「已归档」跳过，但它们仍未达标，
                    # 下一轮仍会重新进入待补池 —— 不会被永久放弃
                    for cid, _f, _d, _r in deferred:
                        ids.add(cid)
                    utils.logger.info(
                        f"[v2_sync] 增量：另有 {len(deferred)} 篇未达标，按本轮"
                        f"预算（≤{backfill_limit or '不限'} 篇 / "
                        f"≤{backfill_max_comments or '不限'} 条）顺延到下一轮补抓")
                detail = "，".join(
                    f"{cid} {fetched}/{declared}({ratio:.0%})"
                    for cid, fetched, declared, ratio in selected[:10])
                utils.logger.info(
                    f"[v2_sync] 增量：{len(selected)} 篇帖子评论完整率未达 "
                    f"{target_ratio:.0%}，本次要重抓（跳过 {len(ids)} 篇已达标）。"
                    f"待补明细：{detail}"
                    + ("…" if len(selected) > 10 else ""))
            return ids
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 读取已归档帖子 ID 失败 {platform_code}: {e}")
        return set()


def _extract_xsec_token(ext: Any, source_url: str = "") -> str:
    """从 story_chapter.ext['xsec_token'] 或 source_url 的查询串里取 xsec_token。"""
    if isinstance(ext, dict):
        tok = ext.get("xsec_token")
        if tok:
            return str(tok)
    if source_url:
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(source_url).query)
            vals = qs.get("xsec_token")
            if vals:
                return vals[0]
        except Exception:
            pass
    return ""


async def get_backfill_candidates(platform_code: str,
                                  target_ratio: float = 0.0,
                                  limit: int = 0,
                                  max_comments: int = 0) -> List[Dict[str, Any]]:
    """返回本轮「优先续抓」的未完成帖子（含 content_id / xsec_token / 声明数）。

    与 `get_archived_content_ids` 用同一套完整性判定与 `plan_backfill` 挑选规则，
    区别是把选中的帖子连同已存的 `xsec_token` 一起返回 —— 抓取端可以**直接按
    note_id 拉详情+评论**，不必先把整份点赞/收藏列表从头翻一遍才能遇到它们。

    limit / max_comments 语义同 `plan_backfill`（0 = 不限）。查询失败返回空列表。
    """
    if target_ratio <= 0:
        target_ratio = float(getattr(config, "COMMENT_COMPLETENESS_TARGET", 0.0) or 0.9)
    try:
        async with _v2_session() as session:
            if session is None:
                return []
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return []
            rows = (await session.execute(
                select(StoryChapter.source_content_id, StoryChapter.source_url,
                       StoryChapter.ext, Story.status,
                       StoryChapter.declared_comment_count,
                       StoryChapter.comment_count, Story.comment_count,
                       func.count(Comment.id))
                .join(Story, Story.id == StoryChapter.story_id, isouter=True)
                .outerjoin(Comment, Comment.story_id == Story.id)
                .where(StoryChapter.source_site_id == site_id)
                .group_by(StoryChapter.source_content_id, StoryChapter.source_url,
                          StoryChapter.ext, Story.status,
                          StoryChapter.declared_comment_count,
                          StoryChapter.comment_count, Story.comment_count)
            )).all()

            incomplete: List[tuple] = []
            meta: Dict[str, Dict[str, Any]] = {}
            for (content_id, source_url, ext, status, declared_c, chapter_c,
                 story_c, cmt_count) in rows:
                if not content_id:
                    continue
                if (status or "") in ("deleted", "hidden"):
                    continue
                declared = int(declared_c or chapter_c or story_c or 0)
                # 扣掉楼中楼深度上限主动放弃的条数（同 get_archived_content_ids）
                declared = max(0, declared - int(
                    (ext or {}).get(COMMENT_SKIPPED_KEY, 0) or 0))
                fetched = int(cmt_count or 0)
                if is_comment_complete_full(fetched, declared, ext, target_ratio):
                    continue
                cid = str(content_id)
                token = _extract_xsec_token(ext, source_url or "")
                if not token:
                    # 没有 xsec_token：按 note_id 直连详情会 404。留在待补池里会
                    # 让「有待补」恒成立 → 列表阶段永远不执行 → 新帖永远发现不了
                    # （死锁）。这里排除它，交给「待补清空后重新拉列表」时带新
                    # token 重新发现。
                    continue
                incomplete.append((cid, fetched, declared,
                                   comment_completeness(fetched, declared)))
                meta[cid] = {
                    "content_id": cid,
                    "source_url": source_url or "",
                    "xsec_token": token,
                    "fetched": fetched,
                    "declared": declared,
                }

            if not incomplete:
                return []
            selected, _deferred = plan_backfill(incomplete, limit, max_comments)
            out: List[Dict[str, Any]] = []
            for cid, fetched, declared, ratio in selected:
                item = dict(meta.get(cid) or {"content_id": cid})
                item.update({"fetched": fetched, "declared": declared,
                             "ratio": round(ratio, 4)})
                out.append(item)
            return out
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 读取待补帖子失败 {platform_code}: {e}")
        return []


# ---------------------------------------------------------------------------
# 评论断点续抓：把每篇帖子的「评论分页游标」持久化到 story_chapter.ext，
# 抓取中断后下一次执行从最后一个成功抓取的评论之后继续，不重头开始、不重复。
# 续抓进度结构（存于 ext["comment_resume"]）：
#   {
#     "top_cursor": str,        # 主评论下一页游标（从它继续即跳过已抓页）
#     "top_count": int,         # 已抓一级评论条数（承接 max_count 上限，避免重复计数）
#     "sub_cursors": {root_id: cursor},  # 楼中楼没翻完的根评论，待续抓收尾
#     "reached_end": bool,      # 是否已翻到 has_more=False（真·抓全）
#     "updated_at": int,
#   }
# ---------------------------------------------------------------------------
COMMENT_RESUME_KEY = "comment_resume"

# 楼中楼深度上限（XHS_SUB_COMMENT_MAX_PAGES）触顶时主动放弃的子评论条数。
# 完整率分母要扣掉它，否则这些帖永远不达标、每轮白抓。详见 save_comment_skipped_sub。
COMMENT_SKIPPED_KEY = "comment_skipped_sub"

# 评论已「翻到 has_more=False」抓全的标记（story_chapter.ext）。
# 小红书平台声明的评论数普遍虚高（含已删/折叠/私密评论），导致「已抓/声明」
# 永远到不了 90%，本该跳过的帖被每轮反复重抓、白打请求还容易撞 461。
# 一旦评论翻到 has_more=False，说明可抓的都抓到了，直接视为 100% 完成、
# 下一轮增量跳过 —— 不再依赖声明数这个不可靠的分母。
COMMENT_FULLY_FETCHED_KEY = "comment_fully_fetched"


async def get_comment_resume(platform_code: str,
                            content_id: str) -> Optional[Dict[str, Any]]:
    """读取某帖的评论续抓进度（story_chapter.ext["comment_resume"]）。

    返回进度字典；不存在/查不到时返回 None。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return None
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return None
            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_site_id == site_id,
                    StoryChapter.source_content_id == str(content_id))
            )).scalar_one_or_none()
            if chapter is None:
                return None
            ext = chapter.ext or {}
            return ext.get(COMMENT_RESUME_KEY)
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 读取评论续抓进度失败 {platform_code}/{content_id}: {e}")
        return None


async def save_comment_resume(platform_code: str, content_id: str,
                             top_cursor: str, top_count: int,
                             sub_cursors: Dict[str, str],
                             reached_end: bool = False) -> None:
    """把评论续抓进度写回 story_chapter.ext["comment_resume"]。

    每翻完一页评论就调用一次（page 级持久化），保证中断后能从最近一页之后恢复；
    sub_cursors 里只保留仍带游标（未翻完）的根评论，已翻完的会被剔除。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return
            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_site_id == site_id,
                    StoryChapter.source_content_id == str(content_id))
            )).scalar_one_or_none()
            if chapter is None:
                utils.logger.warning(
                    f"[v2_sync] 续抓进度写入失败：找不到章节 "
                    f"{platform_code}/{content_id}")
                return
            ext = dict(chapter.ext or {})
            ext[COMMENT_RESUME_KEY] = {
                "top_cursor": top_cursor or "",
                "top_count": int(top_count or 0),
                "sub_cursors": {
                    str(k): str(v) for k, v in (sub_cursors or {}).items() if v
                },
                "reached_end": bool(reached_end),
                "updated_at": int(time.time() * 1000),
            }
            chapter.ext = ext
            await session.commit()
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 写入评论续抓进度失败 {platform_code}/{content_id}: {e}")


async def clear_comment_resume(platform_code: str, content_id: str) -> None:
    """评论抓全（reached_end）后清掉续抓进度，避免下轮误续或误判未完成。"""
    try:
        async with _v2_session() as session:
            if session is None:
                return
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return
            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_site_id == site_id,
                    StoryChapter.source_content_id == str(content_id))
            )).scalar_one_or_none()
            if chapter is None:
                return
            ext = dict(chapter.ext or {})
            if COMMENT_RESUME_KEY in ext:
                ext.pop(COMMENT_RESUME_KEY)
                chapter.ext = ext
                await session.commit()
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 清除评论续抓进度失败 {platform_code}/{content_id}: {e}")


async def save_comment_skipped_sub(platform_code: str, content_id: str,
                                   skipped: int) -> None:
    """记录「因楼中楼深度上限主动放弃」的子评论条数。

    单独存一个 ext 字段（COMMENT_SKIPPED_KEY），不放在 comment_resume 里 ——
    后者在评论翻到底时会被 clear_comment_resume 清掉，而这个值恰恰要在
    「翻到底之后」继续给完整率判定读。

    完整率分母 = 平台声明数 - 本值。否则策略性放弃会让帖子永远凑不满
    目标比例、每轮都被当成未达标重抓，而重抓又只能再翻同样的几页，
    纯浪费请求且毫无进展（这正是限制翻页深度必须配套改判定的原因）。
    """
    if not content_id:
        return
    try:
        async with _v2_session() as session:
            if session is None:
                return
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return
            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_site_id == site_id,
                    StoryChapter.source_content_id == str(content_id))
            )).scalar_one_or_none()
            if chapter is None:
                return
            ext = dict(chapter.ext or {})
            if skipped > 0:
                ext[COMMENT_SKIPPED_KEY] = int(skipped)
            else:
                ext.pop(COMMENT_SKIPPED_KEY, None)
            chapter.ext = ext
            await session.commit()
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 写入楼中楼放弃条数失败 {platform_code}/{content_id}: {e}")


async def mark_comment_fully_fetched(platform_code: str, content_id: str) -> None:
    """标记某帖评论已翻到 has_more=False（全部可抓的都抓到了）。

    与 save_comment_skipped_sub 同理，单独存一个 ext 字段（
    COMMENT_FULLY_FETCHED_KEY），不放在 comment_resume 里 —— 后者在翻到底时
    会被 clear_comment_resume 清掉，而这个标记恰恰要在「翻到底之后」继续给
    增量跳过判定读。

    作用：平台声明评论数虚高（含已删/折叠）时，即使「已抓/声明」< 90%，只要
    已翻到 has_more=False 就视为 100% 完成、下一轮跳过，避免反复重抓。
    """
    if not content_id:
        return
    try:
        async with _v2_session() as session:
            if session is None:
                return
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return
            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_site_id == site_id,
                    StoryChapter.source_content_id == str(content_id))
            )).scalar_one_or_none()
            if chapter is None:
                return
            ext = dict(chapter.ext or {})
            ext[COMMENT_FULLY_FETCHED_KEY] = True
            chapter.ext = ext
            await session.commit()
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 标记评论抓全失败 {platform_code}/{content_id}: {e}")


async def get_declared_comment_counts(platform_code: str,
                                      content_ids: Optional[List[str]] = None
                                      ) -> Dict[str, int]:
    """批量取帖子的「原帖显示评论量」，供抓取时推算单篇评论上限。

    返回 {source_content_id: declared}；查不到的不在字典里（调用方按 0 处理）。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return {}
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return {}
            stmt = select(StoryChapter.source_content_id,
                          StoryChapter.declared_comment_count,
                          StoryChapter.comment_count,
                          Story.comment_count).join(
                Story, Story.id == StoryChapter.story_id, isouter=True
            ).where(StoryChapter.source_site_id == site_id)
            if content_ids:
                stmt = stmt.where(
                    StoryChapter.source_content_id.in_([str(i) for i in content_ids]))
            rows = (await session.execute(stmt)).all()
            out: Dict[str, int] = {}
            for content_id, declared_c, chapter_c, story_c in rows:
                if not content_id:
                    continue
                out[str(content_id)] = int(declared_c or chapter_c or story_c or 0)
            return out
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 读取声明评论数失败 {platform_code}: {e}")
        return {}


async def get_comment_progress(platform_code: str,
                               content_ids: Optional[List[str]] = None
                               ) -> Dict[str, tuple]:
    """批量取 (已抓评论数, 原帖显示评论量)，供抓取前**排序**用。

    为什么需要：抓取被风控打断时，如果按「列表原始顺序」一篇篇抓，
    结果就是每篇都只抓了个开头（0%→40%、0%→1%），一轮下来一篇都没收口。
    改成「快完成的先抓」，一轮能真正抓完若干篇，剩余缺口也不会被摊薄
    （2026-10-29 用户反馈「每篇抓一点，应该先排序再集中抓一篇」）。

    返回 {source_content_id: (fetched, declared)}；查不到的按 (0, 0) 处理。
    """
    out: Dict[str, tuple] = {}
    try:
        async with _v2_session() as session:
            if session is None:
                return out
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return out
            stmt = select(StoryChapter.source_content_id,
                          StoryChapter.declared_comment_count,
                          StoryChapter.comment_count,
                          Story.comment_count,
                          func.count(Comment.id)).join(
                Story, Story.id == StoryChapter.story_id, isouter=True
            ).outerjoin(
                Comment, Comment.story_id == Story.id
            ).where(StoryChapter.source_site_id == site_id).group_by(
                StoryChapter.source_content_id,
                StoryChapter.declared_comment_count,
                StoryChapter.comment_count,
                Story.comment_count)
            if content_ids:
                stmt = stmt.where(StoryChapter.source_content_id.in_(
                    [str(i) for i in content_ids]))
            for content_id, declared_c, chapter_c, story_c, fetched in (
                    await session.execute(stmt)).all():
                if not content_id:
                    continue
                declared = int(declared_c or chapter_c or story_c or 0)
                out[str(content_id)] = (int(fetched or 0), declared)
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 读取评论抓取进度失败 {platform_code}: {e}")
    return out


async def refresh_comment_completeness(platform_code: str,
                                       content_ids: Optional[List[str]] = None
                                       ) -> int:
    """把「已抓评论数 / 原帖显示评论量」写回 story_chapter。

    - `fetched_comment_count` / `completeness` 两个字段此前一直是空的
      （只有一次性迁移脚本写过），现在每次抓完评论就刷一次；
    - 增量跳过判定（get_archived_content_ids）虽然自己实时聚合，但前端
      「完整率」展示依赖这两个字段；
    - content_ids 为空时刷新该平台全部章节。

    返回更新的章节条数。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return 0
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return 0
            stmt = select(StoryChapter, Story.comment_count,
                          func.count(Comment.id)).join(
                Story, Story.id == StoryChapter.story_id, isouter=True
            ).outerjoin(
                Comment, Comment.story_id == Story.id
            ).where(StoryChapter.source_site_id == site_id).group_by(
                StoryChapter.id, Story.comment_count)
            if content_ids:
                stmt = stmt.where(StoryChapter.source_content_id.in_(
                    [str(i) for i in content_ids]))
            rows = (await session.execute(stmt)).all()

            updated = 0
            for chapter, story_c, cmt_count in rows:
                declared = int(chapter.declared_comment_count
                               or chapter.comment_count or story_c or 0)
                # 扣掉楼中楼深度上限主动放弃的条数（口径同 get_archived_content_ids，
                # 否则前端展示的完整率会因为这些策略性放弃而永远上不去）
                declared = max(0, declared - int(
                    (chapter.ext or {}).get(COMMENT_SKIPPED_KEY, 0) or 0))
                fetched = int(cmt_count or 0)
                chapter.fetched_comment_count = fetched
                fully = bool((chapter.ext or {}).get(COMMENT_FULLY_FETCHED_KEY, False))
                if fully or (declared > 0 and fetched >= declared):
                    # 已翻到 has_more=False 抓全，或已抓数 >= 平台声明数：
                    # 这类帖平台声明数普遍虚高（含已删/折叠/私密评论），用声明数当
                    # 分母会永远算「没抓够」→ 每轮被当成未达标重抓、白打请求还撞
                    # 461。把分母纠正为真实已抓数，完整率永久 100%，下一轮必跳过，
                    # 不再反复重抓已抓全的帖。
                    chapter.declared_comment_count = fetched
                    chapter.completeness = 1.0
                elif declared > 0:
                    chapter.completeness = round(
                        comment_completeness(fetched, declared), 4)
                updated += 1
            if updated:
                await session.commit()
            return updated
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 刷新评论完整率失败 {platform_code}: {e}")
        return 0


async def _get_or_create_author(session: AsyncSession, site_id: int,
                                anon_hash: str, nickname: str = "",
                                avatar: str = "",
                                profile_url: str = "") -> Optional[int]:
    """取/建作者，并尽力回填缺失字段（头像 / 主页）。

    身份匹配优先级：
      1) anon_hash（实时抓取与导入通常一致）
      2) profile_url（实时重抓必带真实主页链接；可救回「导入时哈希不一致」
         或「导入时头像/主页缺失」的历史作者 —— 这是"重新抓取自动恢复"的关键）

    命中后：头像/主页为空则回填；若以 profile_url 命中但与现有 anon_hash 不符，
    统一为本次（实时）哈希，避免下次再走 profile_url 分支。

    若同一 profile_url 下存在多条作者（重复导入导致，例如同一人既有空头像行
    又有带头像行），自动合并：评论迁到「规范行」（优先有头像、否则 id 最小），
    并删掉未被 story 引用的孤儿重复行。
    """
    anon_hash = anon_hash or ""
    profile_url = profile_url or ""

    # 1) 优先按 anon_hash 匹配
    row = None
    if anon_hash:
        row = (await session.execute(
            select(Author).where(Author.source_site_id == site_id,
                                 Author.anon_hash == anon_hash)
        )).scalar_one_or_none()

    # 2) 回退按 profile_url 匹配（实时重抓必带真实主页链接；可救回「导入时哈希
    #    不一致」或「导入时头像/主页缺失」的历史作者 —— 这是"重新抓取自动恢复"的关键）
    if row is None and profile_url:
        dupes = (await session.execute(
            select(Author).where(Author.source_site_id == site_id,
                                 Author.profile_url == profile_url)
        )).scalars().all()
        if dupes:
            # 规范行：优先已有头像，否则 id 最小
            if any(a.avatar_url for a in dupes):
                row = max(dupes, key=lambda a: (bool(a.avatar_url), -a.id))
            else:
                row = min(dupes, key=lambda a: a.id)

    if row is None:
        row = Author(source_site_id=site_id, anon_hash=anon_hash,
                     nickname=nickname or None, avatar_url=avatar or None,
                     profile_url=profile_url or None)
        session.add(row)
        await session.flush()
        return row.id

    # 已存在：若 profile_url 非空，先把同 profile_url 的重复作者合并进本行
    #（消除"同一人既有空头像行又有带头像行"的重复，评论一并迁到规范行）
    if profile_url:
        await _merge_duplicate_authors(session, site_id, profile_url, row.id)

    # 回填空字段
    if avatar and not row.avatar_url:
        row.avatar_url = avatar
    if profile_url and not row.profile_url:
        row.profile_url = profile_url
    # 以 profile_url 命中但哈希不一致 → 统一为实时哈希，后续直连命中
    if anon_hash and row.anon_hash != anon_hash:
        row.anon_hash = anon_hash
    return row.id


async def _merge_duplicate_authors(session: AsyncSession, site_id: int,
                                  profile_url: str, keep_id: int) -> None:
    """把同 profile_url 的其它作者行合并进 keep_id。

    仅迁移评论引用；删除未被任何 story 引用的孤儿重复行（保留被 story 引用的，
    避免外键 / 数据丢失）。幂等：合并一次后不再有重复。
    """
    dupes = (await session.execute(
        select(Author).where(Author.source_site_id == site_id,
                             Author.profile_url == profile_url,
                             Author.id != keep_id)
    )).scalars().all()
    if not dupes:
        return
    dupe_ids = [a.id for a in dupes]
    # 评论：全部迁到规范行
    await session.execute(
        update(Comment).where(Comment.author_id.in_(dupe_ids))
        .values(author_id=keep_id)
    )
    # 删除未被 story 引用的孤儿重复行
    referenced = (await session.execute(
        select(Story.author_id).where(Story.author_id.in_(dupe_ids))
    )).scalars().all()
    safe_del = [i for i in dupe_ids if i not in set(referenced)]
    if safe_del:
        await session.execute(
            delete(Author).where(Author.id.in_(safe_del))
        )


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
                _media_index = await _load_media_index(session, chapter.id)
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
                    await _upsert_media(session, chapter.id, img, idx,
                                        local_path, _media_index,
                                        platform_code=platform_code,
                                        content_id=content_id,
                                        story_id=story.id)

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
                # 声明评论数必须跟着刷新：它既是「评论完整率」的分母，
                # 也是单篇评论抓取上限的依据（见 core.batch_get_note_comments）。
                # 只在拿到有效值时才覆盖，避免某次详情缺字段把分母清空。
                if comment_count:
                    chapter.declared_comment_count = comment_count
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
                    # 只增不删：新 URL 才插入；已有 URL 只回补本地路径；
                    # 本次没带回来的历史媒体保留不动
                    _media_index = await _load_media_index(session, chapter.id)
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
                        await _upsert_media(session, chapter.id, img, idx,
                                            local_path, _media_index,
                                            platform_code=platform_code,
                                            content_id=content_id,
                                            story_id=chapter.story_id)

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


async def get_run_crawl_stats(platform_code: str, start_ts: int,
                              end_ts: int) -> tuple:
    """统计 [start_ts, end_ts]（epoch ms）时间窗内被抓取的内容数。

    账号维度（登录用户点赞/收藏）任务用：抓来的帖子作者各异，没法按
    user_id / content_id 预读索引，改为按「执行时间窗」归因——
    chapter.ext.crawled_at 落在窗内的 story 即本轮抓过；
    story.created_at 也在窗内 = 本轮新增，否则 = 本轮刷新。
    返回 (新增, 已刷新)。
    """
    new = updated = 0
    try:
        async with _v2_session() as session:
            if session is None:
                return (0, 0)
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return (0, 0)
            # 本轮有评论入库的 story（按评论 created_at 归属）。
            # 待补帖会「跳过详情、只拉评论」，此时 chapter.ext.crawled_at 不会更新；
            # 只按 crawled_at 归因会导致「评论进了库但统计为 0」。
            # 评论入库时间才是真正「本轮动过」的证据。
            start_dt = datetime.fromtimestamp(start_ts / 1000.0, tz=timezone.utc)
            end_dt = datetime.fromtimestamp(end_ts / 1000.0, tz=timezone.utc)
            touched: Dict[int, int] = {}
            for sid, c in (await session.execute(
                    select(Comment.story_id, func.count(Comment.id))
                    .where(Comment.created_at >= start_dt,
                           Comment.created_at <= end_dt)
                    .group_by(Comment.story_id))).all():
                touched[int(sid)] = int(c or 0)

            rows = (await session.execute(
                select(StoryChapter.ext, Story.created_at, StoryChapter.story_id)
                .join(Story, Story.id == StoryChapter.story_id)
                .where(StoryChapter.source_site_id == site_id)
            )).all()
            for ext, created_at, story_id in rows:
                ts = 0
                if isinstance(ext, dict):
                    try:
                        ts = int(ext.get("crawled_at") or 0)
                    except (TypeError, ValueError):
                        ts = 0
                if not (start_ts <= ts <= end_ts) and not touched.get(
                        int(story_id or 0)):
                    continue
                if created_at is not None:
                    try:
                        created_ts = int(created_at.timestamp() * 1000)
                    except (TypeError, ValueError, OSError):
                        created_ts = 0
                    if created_ts >= start_ts:
                        new += 1
                        continue
                updated += 1
    except Exception as e:
        utils.logger.warning(f"[v2_sync] 统计执行窗内容失败 {platform_code}: {e}")
    return (new, updated)


async def get_completeness_snapshot(platform_code: str) -> Dict[str, Dict]:
    """抓取前拍「评论完整率」快照：content_id -> {fetched, declared, completeness}。

    **必须在起子进程之前调用**。抓完 tuning.completeness 会被
    `refresh_comment_completeness()` 覆盖成本轮结果，事后再查只剩「之后」，
    两次数相减才是「本轮补齐了多少」。
    """
    out: Dict[str, Dict] = {}
    try:
        async with _v2_session() as session:
            if session is None:
                return out
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return out
            rows = (await session.execute(
                select(StoryChapter.source_content_id,
                       StoryChapter.declared_comment_count,
                       StoryChapter.comment_count,
                       Story.comment_count,
                       func.count(Comment.id))
                .join(Story, Story.id == StoryChapter.story_id, isouter=True)
                .outerjoin(Comment, Comment.story_id == Story.id)
                .where(StoryChapter.source_site_id == site_id)
                .group_by(StoryChapter.source_content_id,
                          StoryChapter.declared_comment_count,
                          StoryChapter.comment_count, Story.comment_count)
            )).all()
            for cid, dc, cc, sc, cmt in rows:
                if not cid:
                    continue
                declared = int(dc or cc or sc or 0)
                fetched = int(cmt or 0)
                out[str(cid)] = {
                    "fetched": fetched,
                    "declared": declared,
                    "completeness": round(
                        comment_completeness(fetched, declared), 4),
                }
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 拍摄完整率快照失败 {platform_code}: {e}")
    return out


async def get_run_crawl_detail(platform_code: str, start_ts: int, end_ts: int,
                               before_map: Optional[Dict[str, Dict]] = None
                               ) -> Dict[str, Any]:
    """本次执行动了哪些帖子、评论补齐率前后各是多少（执行记录下拉用）。

    归因规则与 `get_run_crawl_stats` 完全一致（chapter.ext.crawled_at 落在
    执行窗内；story.created_at 也在窗内算新增，否则算刷新），保证明细条数与
    运行记录上的 headline 计数对得上。

    `before_map` 为 `get_completeness_snapshot()` 在抓取前拍的快照；缺失项
    （本轮新入库的帖子）按 fetched=0 处理，展示成 0% → X%。
    """
    before_map = before_map or {}
    added: List[Dict] = []
    updated: List[Dict] = []
    try:
        async with _v2_session() as session:
            if session is None:
                return {"added": added, "updated": updated}
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return {"added": added, "updated": updated}

            # 「之前」= 本轮开始前已入库的评论数，按评论入库时间戳还原。
            # 比抓取前的内存快照更可靠：历史运行记录也能回填出前后对比。
            cut_dt = datetime.fromtimestamp(start_ts / 1000.0, tz=timezone.utc)
            end_dt = datetime.fromtimestamp(end_ts / 1000.0, tz=timezone.utc)
            before_cnt: Dict[int, int] = {}
            for sid, c in (await session.execute(
                    select(Comment.story_id, func.count(Comment.id))
                    .where(Comment.created_at < cut_dt)
                    .group_by(Comment.story_id))).all():
                before_cnt[int(sid)] = int(c or 0)
            # 本轮有新评论入库的 story：作为「本轮动过」的补充归属依据
            # （待补帖只拉评论、不更新 chapter.ext.crawled_at）。
            touched: Dict[int, int] = {}
            for sid, c in (await session.execute(
                    select(Comment.story_id, func.count(Comment.id))
                    .where(Comment.created_at >= cut_dt,
                           Comment.created_at <= end_dt)
                    .group_by(Comment.story_id))).all():
                touched[int(sid)] = int(c or 0)

            rows = (await session.execute(
                select(StoryChapter.source_content_id,
                       StoryChapter.declared_comment_count,
                       StoryChapter.comment_count,
                       Story.comment_count,
                       Story.created_at, Story.title,
                       Story.id,
                       func.count(Comment.id),
                       StoryChapter.ext)
                .join(Story, Story.id == StoryChapter.story_id)
                .outerjoin(Comment, Comment.story_id == Story.id)
                .where(StoryChapter.source_site_id == site_id)
                .group_by(StoryChapter.source_content_id,
                          StoryChapter.declared_comment_count,
                          StoryChapter.comment_count, Story.comment_count,
                          Story.created_at, Story.title, Story.id,
                          StoryChapter.ext)
            )).all()

            for cid, dc, cc, sc, created_at, title, story_id, cmt, ext in rows:
                if not cid:
                    continue
                ts = 0
                if isinstance(ext, dict):
                    try:
                        ts = int(ext.get("crawled_at") or 0)
                    except (TypeError, ValueError):
                        ts = 0
                if not (start_ts <= ts <= end_ts) and not touched.get(
                        int(story_id or 0)):
                    continue  # 不是本轮抓的

                declared = int(dc or cc or sc or 0)
                fetched = int(cmt or 0)
                after = {
                    "fetched": fetched,
                    "declared": declared,
                    "completeness": round(
                        comment_completeness(fetched, declared), 4),
                }
                prev = int(before_cnt.get(int(story_id or 0)) or 0)
                before = {
                    "fetched": prev,
                    "declared": declared,
                    "completeness": round(
                        comment_completeness(prev, declared), 4),
                }

                is_new = False
                if created_at is not None:
                    try:
                        is_new = int(created_at.timestamp() * 1000) >= start_ts
                    except (TypeError, ValueError, OSError):
                        is_new = False

                item = {
                    "content_id": str(cid),
                    "title": (title or "")[:80],
                    "before": before,
                    "after": after,
                    # 本轮实际补进来的评论条数
                    "gained": max(0, after["fetched"] - before["fetched"]),
                }
                (added if is_new else updated).append(item)

            # 补齐多的排前面，方便一眼看出本轮最有价值的成果
            for bucket in (added, updated):
                bucket.sort(key=lambda x: (-x["gained"], x["content_id"]))
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 统计执行窗明细失败 {platform_code}: {e}")
    return {"added": added, "updated": updated}


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

            # 评论所属内容 ID（笔记 note_id）：两分支共用
            content_id = str(_pick(item, ("note_id", "content_id")) or "")

            if existing is not None:
                # 刷新点赞 / 子评论数 / 头像
                existing.like_count = safe_int(item.get("like_count") or 0)
                existing.sub_count = safe_int(item.get("sub_comment_count") or 0)
                # 重抓解析出的规范作者若与当前不同（profile_url 合并/去重场景），
                # 把老评论重归属到规范作者，使其头像一并恢复
                if author_id and existing.author_id != author_id:
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
                # 评论 IP 属地：重抓带回时补上（小概率变化，仅在缺失时回填）
                _ip = item.get("ip_location") or ""
                if _ip and not existing.ip_location:
                    existing.ip_location = str(_ip)

                # 评论正文回写：重抓带回的是平台最新真实内容，必须覆盖。
                # 早期抓取会剥离 [表情] 标记（如 [暗中观察R]）、或小红书改文案，
                # 若不回写，正文会永久陈旧 / 缺表情（典型表现：楼中楼子评论
                # 「用户发的表情」不显示——刷新前端也没用，因为数据源就没有）。
                # 评论不可编辑，最新抓取即权威，故直接用新值覆盖；空值不覆盖以防丢失。
                _new_content = str(item.get("content") or "").strip()
                if _new_content:
                    existing.content_text = _new_content

                # 对话目标：重抓带回真实 target_comment 时刷新 ext，
                # 由抓取结束后的 backfill_comment_links 重新对齐层级。
                # 旧数据扁平化（parent 全指向主评论）靠这里纠正。
                _new_parent_src = str(item.get("parent_comment_id") or "")
                if _new_parent_src:
                    _ext = dict(existing.ext or {})
                    if _ext.get("parent_comment_id") != _new_parent_src:
                        _ext["parent_comment_id"] = _new_parent_src
                        existing.ext = _ext

                # 评论图片入库 + S3 备份（幂等；实时抓取会带回 pictures）
                try:
                    await _sync_comment_media(session, existing.id,
                                              item.get("pictures"), platform_code,
                                              content_id, existing.story_id)
                except Exception as e:
                    utils.logger.warning(
                        f"[v2_sync] 评论图同步失败 {comment_id}: {e}")

                await session.commit()
                return True

            # 找到评论所属的 story / chapter
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
            comment = Comment(
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
                ip_location=str(item.get("ip_location") or "") or None,
                sentiment=_c_sent,
                stance=_c_st.get("stance", "neutral"),
                stance_tag_id=_c_st.get("tag_id"),
                stance_tag_code=_c_st.get("tag_code"),
                stance_confidence=_c_st.get("confidence"),
                stance_judged_ver=_c_st.get("version"),
                # parent 原始 ID 先落 ext，等父评论到达后由 backfill_comment_links 回填
                ext={"parent_comment_id": str(item.get("parent_comment_id") or "")},
            )
            session.add(comment)
            await session.flush()  # 拿评论 id 供评论图登记
            # 评论图片入库 + S3 备份（幂等；实时抓取会带回 pictures）
            try:
                await _sync_comment_media(session, comment.id,
                                          item.get("pictures"), platform_code,
                                          content_id, story_id)
            except Exception as e:
                utils.logger.warning(
                    f"[v2_sync] 评论图同步失败 {comment_id}: {e}")
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
    """回填/重排评论的 parent_id / root_id / depth。

    实时同步时评论常常先于其父评论到达（父评论可能还没抓到），
    这里在抓完一批后统一修补。幂等，可反复执行。

    重抓纠正：存量评论重抓后 ext.parent_comment_id 会被刷新为真实
    target_comment（旧数据可能是扁平化的「全指向主评论」），这里把
    挂接层级与 ext 对齐；父评论缺失（已删/未抓到）时保留旧挂接不动，
    保证评论不会从楼中楼里消失。
    """
    try:
        async with _v2_session() as session:
            if session is None:
                return 0
            site_id = await _get_site_id(session, platform_code)
            if site_id is None:
                return 0
            # 全站评论源 id → 对象映射：父评论可能是无 ext 的存量行，必须都在映射里
            comments = (await session.execute(
                select(Comment).where(Comment.source_site_id == site_id)
            )).scalars().all()
            by_src: Dict[str, Comment] = {
                c.source_comment_id: c for c in comments if c.source_comment_id}
            pending = [c for c in comments if (c.ext or {}).get("parent_comment_id")]
            if not pending:
                return 0

            updated = 0
            for _round in range(10):
                batch = 0
                for c in pending:
                    parent_src = str((c.ext or {}).get("parent_comment_id") or "")
                    parent = by_src.get(parent_src)
                    if parent is None or parent is c:
                        # 父评论缺失：保留旧挂接，不破坏展示
                        continue
                    if parent.parent_id is None and (parent.ext or {}).get("parent_comment_id"):
                        # 父自己也待回填，等下一轮定层级（多级嵌套收敛）
                        continue
                    new_parent = parent.id
                    new_root = parent.root_id or parent.id
                    new_depth = int(parent.depth or 0) + 1
                    if (c.parent_id, c.root_id, c.depth) == (new_parent, new_root, new_depth):
                        # 已对齐：清 ext 即完成（幂等）
                        c.ext = {}
                        batch += 1
                        continue
                    c.parent_id = new_parent
                    c.root_id = new_root
                    c.depth = new_depth
                    c.ext = {}      # 用完即清；ext 非空列，不能置 None
                    batch += 1
                    updated += 1
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


async def mark_story_status(platform_code: str, content_id: str, status: str) -> bool:
    """平台侧内容状态变化时，把本地 story 标记为指定状态。

    status 取值：
      - "deleted"（已删除，平台 code -510000）：作者/平台删了笔记
      - "hidden" （作者已隐藏，平台 code -510001）：笔记仅自己/粉丝可见

    调用时机：爬虫重新抓取该内容时平台返回对应的错误码，由平台 core 在
    捕获到 NoteNotFoundError / NoteHiddenError 后调用。标记后数据仍保留在
    库里可查，前端会显示对应徽标（【原贴已删除】/【原贴已隐藏】），而不是
    从列表里悄悄消失。status 已是目标值时不重复写（幂等）。
    """
    if status not in ("deleted", "hidden"):
        return False
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
            if story is None or story.status == status:
                return False
            story.status = status
            await session.commit()
            utils.logger.info(
                f"[v2_sync] 平台内容状态变化，标记 story {story.id} "
                f"({platform_code}/{content_id}) → {status}")
            return True
    except Exception as e:
        utils.logger.warning(
            f"[v2_sync] 标记状态失败 {platform_code}/{content_id}={status}: {e}")
        return False


async def mark_story_deleted(platform_code: str, content_id: str) -> bool:
    """向后兼容封装：标记 story 为 deleted（旧调用方仍可用）。"""
    return await mark_story_status(platform_code, content_id, "deleted")

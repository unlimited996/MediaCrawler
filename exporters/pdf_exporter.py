# -*- coding: utf-8 -*-
"""把爬取结果渲染成排版紧凑、可离线阅读的 PDF。

排版经验来源：小红书留档实践（单博主 9 篇 / 11812 条评论 → 1410 页，完整率 99.6%）：
1. 图片并发预下载 → 压缩 → base64 内嵌，保证 PDF 单文件离线可读；下载失败降级为占位符，不中断
2. 评论 8.6pt 卡片紧凑排版（不这么做同样内容会膨胀到 2000+ 页）
3. 主评论按点赞降序、楼中楼按时间升序
4. 灌水评论（纯表情 / ≤2 字 / 无点赞）折叠为一行计数，避免篇幅爆炸
5. 表情占位符 `[微笑R]` 降级为去方括号的灰色小标签，绝不留突兀的方括号
"""
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image

# 单批与图片上限，防止一次导出把服务拖死
MAX_RECORDS = 200
MAX_IMAGES = 24          # 整份 PDF 的图片总预算
PER_RECORD_IMAGES = 6    # 单条内容最多带几张图
IMAGE_TIMEOUT = 8.0
IMAGE_MAX_SIDE = 900
IMAGE_QUALITY = 72

# 评论正文里的表情占位符：[微笑R] / [doge]
EMOJI_RE = r"\[[^\[\]\s]{1,12}\]"

# 灌水评论阈值：点赞低于该值且正文极短时折叠
SPAM_LIKE_THRESHOLD = 3
SPAM_TEXT_LEN = 2

CSS = """
@page { size: A4; margin: 12mm 12mm 14mm; }
* { box-sizing: border-box; }
body { font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
       "Source Han Sans SC", sans-serif; font-size: 10.5pt; line-height: 1.6;
       color: #1f2328; margin: 0; }
.doc-title { font-size: 18pt; font-weight: 700; margin: 0 0 4mm; }
.doc-meta { font-size: 8.5pt; color: #6b7280; margin-bottom: 6mm;
            border-bottom: 1px solid #e5e7eb; padding-bottom: 3mm; }
.card { border: 1px solid #e5e7eb; border-radius: 4px; padding: 3.5mm;
        margin-bottom: 3.5mm; page-break-inside: avoid; }
.card-title { font-size: 12pt; font-weight: 600; margin: 0 0 2mm; }
.card-sub { font-size: 8.5pt; color: #6b7280; margin-bottom: 2mm; }
.avatar { display: inline-block; width: 5.2mm; height: 5.2mm; border-radius: 50%;
          object-fit: cover; vertical-align: -1.2mm; margin-right: 1.2mm;
          border: 0.2mm solid #e5e7eb; background: #f3f4f6; }
.platform { display: inline-block; font-size: 7.5pt; padding: 0.4mm 1.6mm;
            border-radius: 2mm; background: #eef2ff; color: #4338ca;
            margin-right: 1.5mm; }
.body { white-space: pre-wrap; word-break: break-word; margin: 0 0 2mm; }
.tags { margin-bottom: 2mm; }
.tag { display: inline-block; font-size: 8pt; color: #4b5563;
       background: #f3f4f6; border-radius: 2mm; padding: 0.3mm 1.6mm;
       margin-right: 1.2mm; }
.stats { font-size: 8.5pt; color: #6b7280; }
.stats span { margin-right: 3mm; }
.images { margin-top: 2mm; }
.images img { width: 100%; max-height: 90mm; object-fit: contain;
              border-radius: 2mm; display: block; margin-bottom: 1.5mm; }
.img-fail { font-size: 7.5pt; color: #9ca3af; font-style: italic; }
.cmt { font-size: 8.6pt; line-height: 1.5; border-top: 1px dashed #e5e7eb;
       padding: 1.6mm 0; page-break-inside: avoid; }
.cmt-head { color: #6b7280; font-size: 7.8pt; margin-bottom: 0.6mm; }
.cmt-sub { margin-left: 4mm; border-left: 2px solid #f3f4f6; padding-left: 2mm; }
.emo { display: inline-block; font-size: 7.5pt; color: #6b7280;
       background: #f9fafb; border-radius: 1mm; padding: 0 0.8mm; }
.folded { font-size: 7.8pt; color: #9ca3af; font-style: italic; padding: 1mm 0; }
.footer-note { margin-top: 6mm; font-size: 7.5pt; color: #9ca3af;
               border-top: 1px solid #e5e7eb; padding-top: 2mm; }
"""

PLATFORM_LABELS = {
    "xhs": "小红书", "dy": "抖音", "ks": "快手", "bili": "B站",
    "wb": "微博", "tieba": "贴吧", "zhihu": "知乎",
}


# --------------------------------------------------------------------------
# 数据读取与规范化
# --------------------------------------------------------------------------
def load_records(file_path: Path, limit: int = MAX_RECORDS) -> List[Dict[str, Any]]:
    """读取 json / jsonl / csv 数据文件，返回记录列表。"""
    suffix = file_path.suffix.lower()
    records: List[Dict[str, Any]] = []

    if suffix == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        records = data if isinstance(data, list) else [data]
    elif suffix == ".jsonl":
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    elif suffix == ".csv":
        import csv
        with open(file_path, "r", encoding="utf-8", newline="") as f:
            records = list(csv.DictReader(f))
    else:
        raise ValueError(f"Unsupported file type for PDF export: {suffix}")

    return records[:limit]


def _pick(item: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _fmt_time(value: Any) -> str:
    """时间戳（秒或毫秒）→ 可读时间；非数字原样返回。"""
    if value in (None, "", 0):
        return ""
    try:
        ts = int(value)
        if ts > 10_000_000_000:  # 毫秒
            ts //= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def _split_list(value: Any) -> List[str]:
    """图/标签字段可能是逗号字符串、JSON 字符串或列表。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    text = str(value)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(v) for v in parsed if v]
    except (json.JSONDecodeError, TypeError):
        pass
    return [p for p in text.split(",") if p.strip()]


def is_comment_record(item: Dict[str, Any]) -> bool:
    """判断一条记录是不是评论（而不是笔记/视频）。"""
    if _pick(item, ["content", "comment_content"], ""):
        return True
    return False


def normalize(item: Dict[str, Any], platform: str) -> Dict[str, Any]:
    """把不同平台的记录统一成渲染所需的结构。"""
    if is_comment_record(item):
        return {
            "kind": "comment",
            "platform": platform,
            # 主评论自身 ID，用于把楼中楼挂到正确的楼层下
            "id": str(_pick(item, ["comment_id", "id"], "")),
            "author": _pick(item, ["nickname", "user_nickname", "user_name"], "匿名"),
            "author_avatar": _pick(item, ["author_avatar", "avatar_url"], ""),
            "text": str(_pick(item, ["content", "comment_content"], "")),
            "time": _fmt_time(_pick(item, ["create_time", "publish_time", "time"], "")),
            "like": _pick(item, ["like_count", "comment_like_count", "liked_count"], 0),
            "is_sub": bool(_pick(item, ["parent_comment_id", "root_id"], "")),
            "root_id": str(_pick(item, ["parent_comment_id", "root_id"], "")),
        }

    return {
        "kind": "content",
        "platform": platform,
        "title": _pick(item, ["title", "desc", "content_text", "note_title"], "（无标题）"),
        "body": str(_pick(item, ["desc", "content", "text", "content_text"], "")),
        "author": _pick(item, ["nickname", "user_nickname", "user_name"], "匿名"),
        "author_avatar": _pick(item, ["author_avatar", "avatar_url"], ""),
        "time": _fmt_time(_pick(item, ["time", "create_time", "publish_time", "last_update_time"], "")),
        "liked": _pick(item, ["liked_count", "voteup_count", "like_count"], ""),
        "comment": _pick(item, ["comment_count", "comments_count", "total_replay_num"], ""),
        "collected": _pick(item, ["collected_count", "video_favorite_count"], ""),
        "shared": _pick(item, ["share_count", "shared_count", "video_share_count"], ""),
        "tags": _split_list(_pick(item, ["tag_list", "tags"], "")),
        "images": _split_list(_pick(item, ["image_list", "images", "cover_url", "video_cover_url"], "")),
        "url": _pick(item, ["note_url", "aweme_url", "video_url", "content_url"], ""),
    }


# --------------------------------------------------------------------------
# 图片：并发下载 → 压缩 → base64 内嵌（离线可读）
# --------------------------------------------------------------------------
def _compress_image(data: bytes) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, IMAGE_MAX_SIDE / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


async def fetch_images_as_data_uri(urls: List[str]) -> Dict[str, str]:
    """并发下载图片并转成 data URI；失败的返回空字符串（由调用方降级为占位符）。"""
    urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]
    urls = urls[:MAX_IMAGES]
    if not urls:
        return {}

    async def _one(client: httpx.AsyncClient, url: str) -> tuple:
        try:
            resp = await client.get(url)
            if resp.status_code != 200 or not resp.content:
                return url, ""
            compressed = await asyncio.to_thread(_compress_image, resp.content)
            if not compressed:
                return url, ""
            return url, "data:image/jpeg;base64," + base64.b64encode(compressed).decode("ascii")
        except Exception:
            return url, ""

    async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT, follow_redirects=True) as client:
        results = await asyncio.gather(*[_one(client, u) for u in urls], return_exceptions=True)

    mapping: Dict[str, str] = {}
    for item in results:
        if isinstance(item, tuple):
            mapping[item[0]] = item[1]
    return mapping


# --------------------------------------------------------------------------
# HTML 渲染
# --------------------------------------------------------------------------
def _render_emoji(text: str) -> str:
    """表情占位符 [微笑R] → 去方括号的灰色小标签（没有映射表时的降级方案）。"""
    import re

    def _sub(match):
        name = match.group(0)[1:-1]
        return f'<span class="emo">{html.escape(name)}</span>'

    return re.sub(EMOJI_RE, _sub, html.escape(text))


def _is_spam(text: str, like: Any) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    try:
        like_num = int(str(like or 0))
    except (TypeError, ValueError):
        like_num = 0
    # 正文极短且没什么点赞 → 视作灌水
    return len(stripped) <= SPAM_TEXT_LEN and like_num < SPAM_LIKE_THRESHOLD


def dedupe_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去重：分页重叠会让同一条评论/笔记被抓多次（历史数据里已存在重复）。

    评论按评论 ID 去重；内容按业务主键去重，都没有主键时退化为「作者+正文」。
    """
    seen = set()
    result = []
    for r in records:
        if r.get("kind") == "comment":
            key = ("c", str(r.get("id") or ""), str(r.get("text") or ""))
        else:
            key = ("n", str(r.get("url") or ""), str(r.get("title") or ""), str(r.get("author") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(r)
    return result


def render_html(records: List[Dict[str, Any]], meta: Dict[str, Any],
                image_map: Optional[Dict[str, str]] = None,
                allow_remote_images: bool = False) -> str:
    image_map = image_map or {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<style>{CSS}</style></head><body>",
        f"<h1 class='doc-title'>{html.escape(str(meta.get('title', 'MediaCrawler 数据导出')))}</h1>",
        "<div class='doc-meta'>",
        f"来源文件：{html.escape(str(meta.get('source', '')))}　·　"
        f"平台：{html.escape(str(meta.get('platform_label', '全部')))}　·　"
        f"记录数：{len(records)}　·　生成时间：{now}",
        "</div>",
    ]

    comments = [r for r in records if r.get("kind") == "comment"]
    contents = [r for r in records if r.get("kind") == "content"]

    for item in contents:
        platform = PLATFORM_LABELS.get(item["platform"], item["platform"])
        parts.append("<div class='card'>")
        parts.append(
            f"<div class='card-title'><span class='platform'>{html.escape(platform)}</span>"
            f"{html.escape(str(item['title']))}</div>"
        )
        avatar_html = ""
        av_uri = (image_map or {}).get(item.get("author_avatar") or "")
        if av_uri:
            avatar_html = f"<img class='avatar' src='{av_uri}'/>"
        sub = f"作者：{avatar_html}{html.escape(str(item['author']))}"
        if item["time"]:
            sub += f"　·　{html.escape(str(item['time']))}"
        parts.append(f"<div class='card-sub'>{sub}</div>")
        if item["body"]:
            parts.append(f"<div class='body'>{_render_emoji(str(item['body']))}</div>")
        if item["tags"]:
            tags = "".join(f"<span class='tag'>#{html.escape(t)}</span>" for t in item["tags"][:20])
            parts.append(f"<div class='tags'>{tags}</div>")
        stats = []
        for label, key in (("点赞", "liked"), ("评论", "comment"),
                           ("收藏", "collected"), ("分享", "shared")):
            if item.get(key) not in ("", None):
                stats.append(f"<span>{label} {html.escape(str(item[key]))}</span>")
        if stats:
            parts.append(f"<div class='stats'>{''.join(stats)}</div>")
        if item["url"]:
            parts.append(
                f"<div class='stats'><span>链接：{html.escape(str(item['url']))}</span></div>"
            )
        images = item.get("images") or []
        if images:
            parts.append("<div class='images'>")
            shown = 0
            skipped = 0
            for url in images[:PER_RECORD_IMAGES]:
                data_uri = image_map.get(url)
                if data_uri:
                    parts.append(f"<img src='{data_uri}'/>")
                    shown += 1
                elif allow_remote_images and url.startswith(("http://", "https://")):
                    # 未内嵌成功时保留外链；注意这会让 Chromium 在渲染时重新下载原图，
                    # 可能显著撑大 PDF，所以只在显式允许远程图片时使用。
                    parts.append(f"<img src='{html.escape(url)}'/>")
                    shown += 1
                else:
                    skipped += 1
            if shown == 0:
                parts.append("<div class='img-fail'>(图片不可用)</div>")
            elif skipped:
                parts.append(f"<div class='img-fail'>(另有 {skipped} 张图片未内嵌)</div>")
            parts.append("</div>")
        parts.append("</div>")

    if comments:
        # 主评论按点赞降序，楼中楼按时间升序；灌水折叠
        mains = [c for c in comments if not c.get("is_sub")]
        subs = [c for c in comments if c.get("is_sub")]
        subs_by_root: Dict[str, List[Dict[str, Any]]] = {}
        for c in subs:
            subs_by_root.setdefault(c.get("root_id", ""), []).append(c)
        for lst in subs_by_root.values():
            lst.sort(key=lambda x: str(x.get("time") or ""))

        def _like_num(c):
            try:
                return int(str(c.get("like") or 0))
            except (TypeError, ValueError):
                return 0

        mains.sort(key=_like_num, reverse=True)

        parts.append("<div class='card'>")
        parts.append(
            f"<div class='card-title'>评论（{len(comments)} 条，"
            f"主楼 {len(mains)} · 回复 {len(subs)}）</div>"
        )
        folded = 0
        for c in mains:
            if _is_spam(str(c.get("text", "")), c.get("like")):
                folded += 1
                continue
            c_av = (image_map or {}).get(c.get("author_avatar") or "")
            head = (f"<img class='avatar' src='{c_av}'/>" if c_av else "") + \
                html.escape(str(c['author']))
            if c.get("time"):
                head += f"　·　{html.escape(str(c['time']))}"
            if str(c.get("like") or 0) not in ("", "0"):
                head += f"　·　赞 {html.escape(str(c['like']))}"
            parts.append(
                f"<div class='cmt'><div class='cmt-head'>{head}</div>"
                f"{_render_emoji(str(c.get('text', '')))}</div>"
            )
            for s in subs_by_root.get(str(c.get("id", "")), []):
                like_txt = (
                    f"　·　赞 {html.escape(str(s['like']))}"
                    if str(s.get("like") or 0) not in ("", "0") else ""
                )
                parts.append(
                    f"<div class='cmt cmt-sub'>"
                    f"<div class='cmt-head'>{html.escape(str(s['author']))}"
                    f"{'　·　' + html.escape(str(s['time'])) if s.get('time') else ''}"
                    f"{like_txt}</div>"
                    f"{_render_emoji(str(s.get('text', '')))}</div>"
                )
        if folded:
            parts.append(f"<div class='folded'>（已折叠 {folded} 条灌水评论）</div>")
        parts.append("</div>")

    parts.append(
        "<div class='footer-note'>本文件由 MediaCrawler 生成，仅供学习与研究使用，"
        "请遵守目标平台的使用条款与 LICENSE 约束。</div>"
    )
    parts.append("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------
# 出口：渲染 PDF
# --------------------------------------------------------------------------
@asynccontextmanager
async def session_ctx(engine):
    """统一的 AsyncSession 上下文，保证异常时也关闭会话。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()


async def _print_pdf(doc_html: str, out_path: Path) -> None:
    """把 HTML 用 Chromium 打印成 PDF。"""
    from playwright.async_api import async_playwright

    # 容器内以 root 跑 Chromium 必须关沙箱，复用与爬虫一致的处理方式
    from tools.crawler_util import chromium_launch_kwargs

    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        launch_kw = chromium_launch_kwargs(p.chromium)
        browser = await p.chromium.launch(**launch_kw)
        try:
            page = await browser.new_page()
            await page.set_content(doc_html, wait_until="load")
            await page.emulate_media(media="print")
            await page.pdf(
                path=str(out_path),
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
            )
        finally:
            await browser.close()


async def export_stories_pdf(story_ids: List[int], out_path: Path,
                             embed_images: bool = True) -> Dict[str, Any]:
    """按勾选的故事 ID 从 v2 库表导出 PDF（数据管理页用）。

    与 export_pdf（按数据文件导出）的区别：这里导出的是**用户勾选的条目**，
    内容来自统一模型，天然带章节、标签、评论树与媒体附件。
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from database.models_v2 import (
        Author, Comment, MediaAttachment, SourceSite, Story, StoryChapter, StoryTag, Tag,
    )

    if not story_ids:
        raise ValueError("没有勾选任何数据")

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        raise RuntimeError("配置库不可用")

    records: List[Dict[str, Any]] = []
    titles: List[str] = []

    async with session_ctx(engine) as session:
        for story_id in story_ids:
            story = (await session.execute(
                select(Story).where(Story.id == story_id))).scalar_one_or_none()
            if not story:
                continue

            chapters = (await session.execute(
                select(StoryChapter).where(StoryChapter.story_id == story_id)
                .order_by(StoryChapter.seq_no))).scalars().all()
            comments = (await session.execute(
                select(Comment).where(Comment.story_id == story_id)
                .order_by(Comment.depth, Comment.like_count.desc()))).scalars().all()
            tags = (await session.execute(
                select(Tag.name).join(StoryTag, StoryTag.tag_id == Tag.id)
                .where(StoryTag.story_id == story_id))).scalars().all()

            author_name = None
            author_avatar = None
            if story.author_id:
                author_name = (await session.execute(
                    select(Author.nickname).where(Author.id == story.author_id))).scalar()
                author_avatar = (await session.execute(
                    select(Author.avatar_url).where(Author.id == story.author_id))).scalar()

            platform_code = ""
            if story.primary_site_id:
                platform_code = (await session.execute(
                    select(SourceSite.code).where(SourceSite.id == story.primary_site_id))).scalar() or ""

            media_urls: List[str] = []
            if chapters:
                media = (await session.execute(
                    select(MediaAttachment).where(
                        MediaAttachment.target_type == "chapter",
                        MediaAttachment.target_id.in_([c.id for c in chapters]),
                    ).order_by(MediaAttachment.sort_order))).scalars().all()
                media_urls = [m.source_url for m in media]

            main = chapters[0] if chapters else None
            titles.append(story.title)
            records.append({
                "kind": "content",
                "platform": platform_code,
                "title": story.title,
                "body": (main.content_text if main else None) or story.summary or "",
                "author": author_name or "匿名",
                "author_avatar": author_avatar or "",
                # 必须走 _fmt_time：库里是毫秒时间戳，直接渲染会输出一串数字
                "time": _fmt_time(story.first_publish_ts or (main.publish_ts if main else None)),
                "liked": story.like_count,
                "comment": story.comment_count,
                "collected": main.collect_count if main else None,
                "shared": main.share_count if main else None,
                "tags": list(tags),
                "images": media_urls,
                "url": main.source_url if main else None,
            })

            author_ids = {c.author_id for c in comments if c.author_id}
            names: Dict[int, str] = {}
            avatars: Dict[int, str] = {}
            if author_ids:
                rows = (await session.execute(
                    select(Author.id, Author.nickname, Author.avatar_url)
                    .where(Author.id.in_(author_ids)))).all()
                names = {r[0]: r[1] for r in rows}
                avatars = {r[0]: r[2] for r in rows if r[2]}

            for c in comments:
                records.append({
                    "kind": "comment",
                    "platform": platform_code,
                    "id": str(c.id),
                    "author": names.get(c.author_id) or "匿名",
                    "author_avatar": avatars.get(c.author_id) or "",
                    "text": c.content_text,
                    "time": _fmt_time(c.publish_ts),
                    "like": c.like_count,
                    "is_sub": bool(c.parent_id),
                    "root_id": str(c.root_id) if c.root_id else "",
                })

    if not records:
        raise ValueError("勾选的数据不存在或已删除")

    image_map: Dict[str, str] = {}
    if embed_images:
        urls: List[str] = []
        for r in records:
            # 作者头像优先入列（体积小、信息密度高）
            av = r.get("author_avatar")
            if av and len(urls) < MAX_IMAGES:
                urls.append(av)
            for u in (r.get("images") or [])[:PER_RECORD_IMAGES]:
                if len(urls) >= MAX_IMAGES:
                    break
                urls.append(u)
        if urls:
            image_map = await fetch_images_as_data_uri(urls)

    doc_html = render_html(
        records,
        {
            "title": titles[0] if len(titles) == 1 else f"数据导出（{len(titles)} 条）",
            "source": f"勾选导出 {len(titles)} 条故事",
            "platform_label": PLATFORM_LABELS.get(
                records[0].get("platform", ""), "全部") if records else "全部",
        },
        image_map=image_map,
        allow_remote_images=not embed_images,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    await _print_pdf(doc_html, out_path)

    return {
        "file": str(out_path),
        "name": out_path.name,
        "record_count": len(titles),
        "image_count": sum(1 for v in image_map.values() if v),
        "size": out_path.stat().st_size if out_path.exists() else 0,
    }


async def export_pdf(file_path: Path, out_path: Path, limit: int = MAX_RECORDS,
                     platform: str = "", embed_images: bool = True) -> Dict[str, Any]:
    """把数据文件渲染成 PDF，返回导出结果信息。"""
    from playwright.async_api import async_playwright

    records_raw = load_records(file_path, limit=limit)
    if not records_raw:
        raise ValueError("数据文件为空，无可导出内容")

    # 平台推断：显式指定优先，其次从文件路径猜
    if not platform:
        rel = str(file_path).lower()
        for key in PLATFORM_LABELS:
            if f"{key}_" in rel or f"/{key}/" in rel or f"\\{key}\\" in rel:
                platform = key
                break
    platform = platform or ""

    records = dedupe_records([normalize(r, platform) for r in records_raw])

    # 图片预算：整份 PDF 共 MAX_IMAGES 张，单条内容最多 PER_RECORD_IMAGES 张。
    # 不做预算控制的话，Chromium 会在渲染时把外链原图全部下载进 PDF，体积会失控。
    image_map: Dict[str, str] = {}
    if embed_images:
        urls: List[str] = []
        for r in records:
            # 作者头像优先入列（体积小、信息密度高）
            av = r.get("author_avatar")
            if av and len(urls) < MAX_IMAGES:
                urls.append(av)
            for u in (r.get("images") or [])[:PER_RECORD_IMAGES]:
                if len(urls) >= MAX_IMAGES:
                    break
                urls.append(u)
            if len(urls) >= MAX_IMAGES:
                break
        if urls:
            image_map = await fetch_images_as_data_uri(urls)

    doc_html = render_html(
        records,
        {
            "title": "MediaCrawler 数据导出",
            "source": file_path.name,
            "platform_label": PLATFORM_LABELS.get(platform, "全部") if platform else "全部",
        },
        image_map=image_map,
        allow_remote_images=not embed_images,
    )

    await _print_pdf(doc_html, out_path)

    return {
        "file": str(out_path),
        "name": out_path.name,
        "record_count": len(records),
        "image_count": sum(1 for v in image_map.values() if v),
        "size": out_path.stat().st_size if out_path.exists() else 0,
    }

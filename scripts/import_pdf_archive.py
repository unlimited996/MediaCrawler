# -*- coding: utf-8 -*-
"""小红书 PDF 留档导入 pgsql：原页面数据字段 + 缓存页面图片。

背景
----
`C:\\Users\\11826\\WorkBuddy\\2026-10-14-11-20-16` 下是小红书网页留档：
  - `momo(小红书)主页留档/01..09-*.pdf`  作者 momo 的 9 篇帖子
  - `有点无解 好无力 2026-10-14留档.pdf`  另一篇笔记存档
每个 PDF 的页面结构：第 1 页=标题/作者/发布时间/互动数/正文 + 头像，
中间页=帖子配图（一页一张），之后=评论与回复（含评论 ID / 用户 ID），
末页页脚带「笔记 ID / 用户主页 / 生成时间」。

本脚本把这些**原页面数据字段**与**内嵌配图**导入 pgsql：
  - 走 store.xhs.update_xhs_note / update_xhs_note_comment（v1 落库），
    自动触发 v2 同步（story / chapter / comment / media_attachment）；
  - 配图抽取保存到 `data/xhs/media/{note_id}/001.jpg ...`，
    v2 同步会识别本地文件写入 local_path，并自动入 S3 备份队列；
  - 幂等：按 note_id upsert，重复执行不产生重复数据。

用法
----
    python scripts/import_pdf_archive.py --dir "C:\\Users\\11826\\WorkBuddy\\2026-10-14-11-20-16"
    python scripts/import_pdf_archive.py --file "xxx.pdf"          # 只导一个
    python scripts/import_pdf_archive.py --dir ... --dry-run       # 只解析不入库
    python scripts/import_pdf_archive.py --dir ... --no-images     # 不抽图
"""
import argparse
import asyncio
import io
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --------------------------------------------------------------------------- #
# PDF 解析
# --------------------------------------------------------------------------- #

#: 互动行：115 点赞 20 收藏 301 评论 41 分享
_RE_INTERACT = re.compile(r"(\d+)\s*点赞")
_RE_COLLECT = re.compile(r"(\d+)\s*收藏")
_RE_COMMENT_CNT = re.compile(r"(\d+)\s*评论")
_RE_SHARE = re.compile(r"(\d+)\s*分享")
#: 绝对日期：2026年09月01日 22:40 / 2026年09月01日 / 09-01
_RE_DATE_FULL = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?")
_RE_DATE_SHORT = re.compile(r"(?<!\d)(\d{2})-(\d{2})(?!\d)")
#: 相对时间：昨天 20:08 / 46分钟前 / 1小时前 / 21分钟前
_RE_YESTERDAY = re.compile(r"昨天\s*(\d{1,2}):(\d{2})")
_RE_MIN_AGO = re.compile(r"(\d+)\s*分钟前")
_RE_HOUR_AGO = re.compile(r"(\d+)\s*小时前")
#: 页脚
_RE_FOOT_NOTE = re.compile(r"笔记\s*ID[:：]\s*([0-9a-fA-F]+)")
_RE_FOOT_USER = re.compile(r"用户主页[:：]\s*([0-9a-fA-F]+)")
_RE_FOOT_TIME = re.compile(r"(?:抓取|生成)时间[:：]\s*(\d{4})-(\d{2})-(\d{2})\s*(\d{1,2}):(\d{2})")
#: 评论 ID 行
_RE_CMT_ID = re.compile(
    r"评论\s*ID\s*[:：]?\s*([0-9a-zA-Z]+)\s*　?\s*·\s*　?\s*用户\s*ID\s*[:：]?\s*([0-9a-zA-Z]+)")
#: 主评论行（序号 + 昵称 [+ 徽标]） / 回复行
_RE_MAIN_CMT = re.compile(r"^(\d+)\s+(\S+)(?:\s+.*)?$")
_RE_REPLY_CMT = re.compile(r"^↳\s*(\S+)")
#: 互动行里的点赞（评论 meta）
_RE_LIKE = re.compile(r"赞\s*(\d+)")
_RE_IP = re.compile(r"IP\s*属地\s*[:：]?\s*(\S+)")
#: 内容区块标记（页面装饰，导入时剥掉）
_RE_SECTION_MARK = re.compile(r"^(正文|背景|内容|正文\s*CONTENT|背景\s*BACKGROUND)\s*[A-Z]*$")


def _parse_date(text: str, ref: Optional[datetime] = None,
                default_year: Optional[int] = None) -> Optional[int]:
    """把页面上的日期字符串转 epoch ms；解析不了返回 None。"""
    if not text:
        return None
    m = _RE_DATE_FULL.search(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        try:
            return int(datetime(y, mo, d, hh, mm, tzinfo=timezone(timedelta(hours=8)))
                       .timestamp() * 1000)
        except ValueError:
            return None
    m = _RE_DATE_SHORT.search(text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = default_year or (ref.year if ref else datetime.now().year)
        try:
            return int(datetime(y, mo, d, tzinfo=timezone(timedelta(hours=8)))
                       .timestamp() * 1000)
        except ValueError:
            return None
    ref = ref or datetime.now(timezone(timedelta(hours=8)))
    m = _RE_YESTERDAY.search(text)
    if m:
        base = ref - timedelta(days=1)
        try:
            dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                              second=0, microsecond=0)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    m = _RE_MIN_AGO.search(text)
    if m:
        return int((ref - timedelta(minutes=int(m.group(1)))).timestamp() * 1000)
    m = _RE_HOUR_AGO.search(text)
    if m:
        return int((ref - timedelta(hours=int(m.group(1)))).timestamp() * 1000)
    return None


def _clean_str(s: Any) -> Any:
    """清洗 PDF 抽取文本里的非法字符。

    PostgreSQL UTF8 不接受 NUL（\\x00），PDF 文本抽取偶发带入；
    其它 C0 控制字符（换行/制表符除外）一并去掉，避免入库报
    CharacterNotInRepertoireError 整条丢弃。
    """
    if not isinstance(s, str):
        return s
    if not s:
        return s
    out = []
    for ch in s:
        o = ord(ch)
        if ch in ("\n", "\r", "\t") or o >= 0x20:
            # 孤立代理项同样无法编码进 UTF8
            if 0xD800 <= o <= 0xDFFF:
                continue
            out.append(ch)
    return "".join(out)


def _page1_parse(lines: List[str]) -> Dict[str, Any]:
    """解析第 1 页头部：标题 / 作者 / 发布时间 / IP / 互动数 / 正文起点。"""
    # 去掉首行留档标识（「小红书 · 帖子留档 第 1 / 9 篇」等）
    if lines and "小红书" in lines[0] and ("留档" in lines[0] or "存档" in lines[0]):
        lines = lines[1:]
    title = lines[0] if lines else ""

    interact_idx = None
    for i, ln in enumerate(lines):
        if _RE_INTERACT.search(ln):
            interact_idx = i
            break
    if interact_idx is None:
        interact_idx = min(3, len(lines))

    author_lines = lines[1:interact_idx]
    nickname, ip_location, publish_ms = "", "", None
    for ln in author_lines:
        m = _RE_DATE_FULL.search(ln)
        if m and publish_ms is None:
            publish_ms = _parse_date(ln)
        m = _RE_IP.search(ln)
        if m:
            ip_location = m.group(1).strip()
        if "小红书号" in ln:
            nickname = ln.split("小红书号")[0].strip().split(" ")[0]
        elif "发表于" in ln:
            # 「小 小红薯68DD5A3A · 发表于 ...」：昵称取 · 前最后一个词
            # （行首偶有头像字符残留，不能取第一个词）
            nickname = ln.split("发表于")[0].replace("·", " ").strip().split(" ")[-1]
    if not nickname and author_lines:
        nickname = author_lines[0].split("·")[0].replace(" ", "")[:32]

    interact_line = lines[interact_idx] if interact_idx < len(lines) else ""

    def _num(rx) -> int:
        m = rx.search(interact_line)
        return int(m.group(1)) if m else 0

    interact = {
        "liked_count": _num(_RE_INTERACT),
        "collected_count": _num(_RE_COLLECT),
        "comment_count": _num(_RE_COMMENT_CNT),
        "share_count": _num(_RE_SHARE),
    }

    # 正文：互动行之后的所有行，剥掉区块标记
    content_lines = lines[interact_idx + 1:]
    while content_lines and _RE_SECTION_MARK.match(content_lines[0]):
        content_lines = content_lines[1:]
    return {"title": title, "nickname": nickname, "ip_location": ip_location,
            "publish_ms": publish_ms, "interact": interact,
            "content_head": content_lines}


def _parse_comment_blocks(text: str, ref_now: Optional[datetime],
                          default_year: Optional[int]) -> List[Dict[str, Any]]:
    """解析评论区文本 → 评论/回复列表（块终止于「评论 ID … 用户 ID …」行）。"""
    lines = [ln.rstrip() for ln in text.split("\n")]
    blocks: List[List[str]] = []
    buf: List[str] = []
    for ln in lines:
        if _RE_CMT_ID.search(ln):
            m = _RE_CMT_ID.search(ln)
            blocks.append((buf, m.group(1), m.group(2)))
            buf = []
        else:
            if ln.strip():
                buf.append(ln.strip())
    out: List[Dict[str, Any]] = []
    last_main_id = ""
    for buf_lines, cmt_id, user_id in blocks:
        if not buf_lines:
            continue
        # 找到块的起点（主评论/回复行），之前的是页眉噪音
        start = None
        for i, ln in enumerate(buf_lines):
            if _RE_MAIN_CMT.match(ln) or _RE_REPLY_CMT.match(ln):
                start = i
                break
        if start is None:
            continue
        head = buf_lines[start]
        body = buf_lines[start + 1:]
        is_reply = bool(_RE_REPLY_CMT.match(head))
        if is_reply:
            nickname = _RE_REPLY_CMT.match(head).group(1)
            parent_id = last_main_id
        else:
            m = _RE_MAIN_CMT.match(head)
            nickname = m.group(2)
            parent_id = ""
            last_main_id = cmt_id

        # 末尾一行是 meta（地区 · 时间 · 赞 N），其余是内容
        meta = ""
        content_lines = body
        if body and ("·" in body[-1] or _RE_LIKE.search(body[-1])
                     or _RE_MIN_AGO.search(body[-1]) or _RE_HOUR_AGO.search(body[-1])):
            meta = body[-1]
            content_lines = body[:-1]
        region = meta.split("·")[0].strip() if meta else ""
        like_m = _RE_LIKE.search(meta)
        like_count = int(like_m.group(1)) if like_m else 0
        create_ms = _parse_date(meta, ref=ref_now, default_year=default_year)

        item = {
            "id": cmt_id,
            "note_id": "",
            "content": _clean_str("".join(content_lines)),
            "create_time": create_ms,
            "like_count": like_count,
            "sub_comment_count": 0,
            "pictures": [],
            "user_info": {"user_id": _clean_str(user_id),
                          "nickname": _clean_str(nickname),
                          "image": "", "region": _clean_str(region)},
            "target_comment": {"id": parent_id} if is_reply else {"id": ""},
        }
        out.append(item)
    return out


def parse_pdf(path: Path) -> Dict[str, Any]:
    """解析一个留档 PDF → 结构化数据（不入库）。"""
    from pypdf import PdfReader
    from PIL import Image

    reader = PdfReader(str(path))
    pages = reader.pages
    n = len(pages)
    if n == 0:
        raise ValueError("空 PDF")

    texts = [(p.extract_text() or "") for p in pages]

    # 评论区起始页
    comment_start = n
    for i, t in enumerate(texts):
        if _RE_CMT_ID.search(t):
            comment_start = i
            break

    # 第 1 页头部
    p1_lines = [ln.strip() for ln in texts[0].split("\n") if ln.strip()]
    head = _page1_parse(p1_lines)

    # 正文：第 1 页互动行之后 + 后续非评论页的文本（图片页无文本）
    content_parts = list(head["content_head"])
    for i in range(1, comment_start):
        for ln in texts[i].split("\n"):
            ln = ln.strip()
            if ln and not _RE_SECTION_MARK.match(ln):
                content_parts.append(ln)
    content = "\n".join(content_parts).strip()

    # 末页页脚
    foot = texts[-1]
    note_id = (_RE_FOOT_NOTE.search(foot) or [None, ""])[1] if _RE_FOOT_NOTE.search(foot) else ""
    user_id = (_RE_FOOT_USER.search(foot) or [None, ""])[1] if _RE_FOOT_USER.search(foot) else ""
    ref_now = None
    m = _RE_FOOT_TIME.search(foot)
    if m:
        try:
            ref_now = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                               int(m.group(4)), int(m.group(5)),
                               tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            ref_now = None

    # 图片：评论区之前的页面；小图（头像，第 1 页）与配图（大图）分开
    photos: List[Tuple[bytes, int, int]] = []
    avatar: Optional[bytes] = None
    for i in range(0, comment_start):
        for img in pages[i].images:
            try:
                im = Image.open(io.BytesIO(img.data))
                w, h = im.size
            except Exception:
                continue
            if min(w, h) < 200:
                if i == 0 and avatar is None:
                    avatar = img.data
                continue
            photos.append((img.data, w, h))

    # 评论
    cmt_text = "\n".join(texts[comment_start:]) if comment_start < n else ""
    comments = _parse_comment_blocks(
        cmt_text, ref_now=ref_now,
        default_year=(ref_now.year if ref_now else None))
    for c in comments:
        c["note_id"] = note_id

    return {
        "file": str(path),
        "note_id": note_id,
        "user_id": user_id,
        "title": head["title"],
        "nickname": head["nickname"],
        "ip_location": head["ip_location"],
        "publish_ms": head["publish_ms"],
        "interact": head["interact"],
        "content": content,
        "photos": photos,
        "avatar": avatar,
        "comments": comments,
        "ref_now": ref_now,
    }


# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #

def _media_dir(note_id: str) -> Path:
    from tools.paths import DATA_DIR

    d = DATA_DIR / "xhs" / "media" / note_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def import_note(parsed: Dict[str, Any], save_images: bool = True,
                      with_comments: bool = True) -> Dict[str, Any]:
    """把解析结果写入 pgsql（v1 + v2 自动同步）。"""
    import config

    config.SAVE_DATA_OPTION = "postgres"  # 强制走数据库（脚本默认可能是 jsonl）

    from store import xhs as xhs_store
    from var import source_keyword_var

    note_id = parsed["note_id"]
    if not note_id:
        raise ValueError("PDF 里解析不到笔记 ID（页脚缺失）")

    source_keyword_var.set("pdf_archive")

    # 1) 配图落盘：data/xhs/media/{note_id}/001.jpg ...（v2 同步按序号探测本地文件）
    image_items: List[Dict[str, Any]] = []
    saved = 0
    if save_images and parsed["photos"]:
        mdir = _media_dir(note_id)
        for idx, (data, w, h) in enumerate(parsed["photos"], 1):
            fn = f"{idx:03d}.jpg"
            fp = mdir / fn
            fp.write_bytes(data)
            saved += 1
            image_items.append({
                "url_default": f"pdf-archive://{note_id}/{fn}",
                "url": f"pdf-archive://{note_id}/{fn}",
                "width": w, "height": h, "live_photo": False,
            })

    # 2) 头像落盘（小图，不参与帖子媒体序号）
    avatar_rel = ""
    if save_images and parsed["avatar"]:
        mdir = _media_dir(note_id)
        fp = mdir / "_avatar.jpg"
        fp.write_bytes(parsed["avatar"])
        avatar_rel = f"xhs/media/{note_id}/_avatar.jpg"

    # 3) 作者 ID：页脚有就用真的（与站内抓取数据合并同一作者）；
    #    没有（个人存档 PDF）用稳定伪 ID，避免不同作者撞同一个匿名哈希
    user_id = parsed["user_id"] or f"pdfarchive_{parsed['nickname'] or 'unknown'}"

    note_item = {
        "note_id": note_id,
        "type": "normal",
        "title": _clean_str(parsed["title"]) or "",
        "desc": _clean_str(parsed["content"]) or "",
        "time": parsed["publish_ms"],
        "last_update_time": parsed["publish_ms"],
        "ip_location": parsed["ip_location"],
        "user": {"user_id": user_id, "nickname": _clean_str(parsed["nickname"]) or "",
                 "avatar": f"/api/data/raw/{avatar_rel}" if avatar_rel else "",
                 "xsec_token": ""},
        "interact_info": {
            "liked_count": str(parsed["interact"]["liked_count"]),
            "collected_count": str(parsed["interact"]["collected_count"]),
            "comment_count": str(parsed["interact"]["comment_count"]),
            "share_count": str(parsed["interact"]["share_count"]),
        },
        "image_list": image_items,
        "tag_list": [],
        "video": None,
        "xsec_token": "",
    }

    await xhs_store.update_xhs_note(note_item)

    # 4) 媒体直注册（v2）：archive 图片的 source_url 是 pdf-archive://  scheme，
    #    v2_sync 只认 http(s) 前缀会跳过，所以这里按 chapter 直接 upsert，
    #    并自动入 S3 备份队列（本地文件已在第 1 步落盘）
    media_n = 0
    if save_images and image_items:
        try:
            media_n = await _register_media(note_id, image_items)
        except Exception as e:
            print(f"    [warn] v2 媒体注册失败: {e}")

    # 5) 头像回写 v2 Author（store 层会过滤非 http 的头像值，
    #    本地留档头像只能同步后补；前端 <img src="/api/data/raw/..."> 可直接用）
    if avatar_rel:
        try:
            await _patch_v2_author_avatar(user_id, avatar_rel)
        except Exception as e:
            print(f"    [warn] v2 头像回写失败: {e}")

    # 6) 评论（含楼中楼）
    n_cmt = 0
    if with_comments:
        for c in parsed["comments"]:
            try:
                await xhs_store.update_xhs_note_comment(note_id, c)
                n_cmt += 1
            except Exception as e:
                print(f"    [warn] 评论 {c.get('id')} 入库失败: {e}")
        # 评论同步时父评论可能还没到（v2 设计：parent 原始 ID 先落 ext），
        # 回填 parent_id / root_id / depth，让楼中楼结构立即可见
        try:
            from tools.v2_sync import backfill_comment_links

            linked = await backfill_comment_links("xhs")
            if linked:
                print(f"    [info] 评论父子链接回填 {linked} 条")
        except Exception as e:
            print(f"    [warn] 评论父子链接回填失败: {e}")

    return {"note_id": note_id, "title": parsed["title"],
            "images": media_n, "comments": n_cmt,
            "declared_comments": parsed["interact"]["comment_count"]}


async def _register_media(note_id: str, image_items: List[Dict[str, Any]]) -> int:
    """把留档图片注册为 v2 media_attachment（幂等 upsert）+ 刷新帖子媒体数。

    返回注册条数。备份入队失败不影响注册结果。
    """
    from sqlalchemy import select

    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from database.models_v2 import (MediaAttachment, SourceSite, Story,
                                     StoryChapter)
    from tools.paths import DATA_DIR
    from tools.v2_sync import _load_media_index, _upsert_media

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return 0
    from sqlalchemy.ext.asyncio import AsyncSession

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            chapter = (await session.execute(
                select(StoryChapter).where(
                    StoryChapter.source_content_id == note_id)
            )).scalar_one_or_none()
            if chapter is None:
                print(f"    [warn] v2 里找不到章节 {note_id}，媒体未注册")
                return 0
            story = (await session.execute(
                select(Story).where(Story.id == chapter.story_id)
            )).scalar_one_or_none()
            site_code = None
            if chapter.source_site_id:
                site_code = (await session.execute(
                    select(SourceSite.code).where(
                        SourceSite.id == chapter.source_site_id)
                )).scalar()

            index = await _load_media_index(session, chapter.id)
            media_dir = DATA_DIR / "xhs" / "media" / note_id
            n = 0
            for idx, img in enumerate(image_items, 1):
                url = img["url_default"]
                fn = f"{idx:03d}.jpg"
                fp = media_dir / fn
                local_path = str(fp) if fp.is_file() else ""
                await _upsert_media(session, chapter.id, url, idx,
                                    local_path, index,
                                    platform_code=site_code or "xhs",
                                    content_id=note_id,
                                    story_id=chapter.story_id)
                n += 1

            # 刷新帖子媒体数与封面（与 backfill_media_local --all-counts 同规则）
            if story is not None:
                rows = (await session.execute(
                    select(MediaAttachment).where(
                        MediaAttachment.target_type == "chapter",
                        MediaAttachment.target_id == chapter.id,
                        MediaAttachment.media_type == "image")
                    .order_by(MediaAttachment.sort_order)
                )).scalars().all()
                story.media_count = len(rows)
                if rows and not story.cover_media_id:
                    story.cover_media_id = rows[0].id
            await session.commit()
            return n
    finally:
        await engine.dispose()


async def _patch_v2_author_avatar(user_id: str, avatar_rel: str) -> None:
    """v2 Author.avatar_url 补本地留档头像（按匿名哈希定位）。"""
    from sqlalchemy import select

    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from database.models_v2 import Author
    from tools.user_hash import anonymize_user_id

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return
    try:
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine, expire_on_commit=False) as s:
            anon = anonymize_user_id(user_id)
            row = (await s.execute(
                select(Author).where(Author.anon_hash == anon))).scalar_one_or_none()
            if row is not None:
                row.avatar_url = f"/api/data/raw/{avatar_rel}"
                await s.commit()
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# 目录遍历
# --------------------------------------------------------------------------- #

def collect_pdfs(target: Path) -> List[Path]:
    """收集 PDF：文件则自身，目录则递归（保持文件名排序，存档顺序即帖子顺序）。"""
    if target.is_file():
        return [target] if target.suffix.lower() == ".pdf" else []
    out = [p for p in sorted(target.rglob("*.pdf")) if p.is_file()]
    return out


async def main_async(args: argparse.Namespace) -> int:
    target = Path(args.dir or args.file)
    pdfs = collect_pdfs(target)
    if not pdfs:
        print(f"[import] 目录下没有 PDF: {target}")
        return 1
    print(f"[import] 待导入 {len(pdfs)} 个 PDF"
          f"{'（dry-run 只解析）' if args.dry_run else ''}")

    ok = fail = skipped = 0
    seen_notes: set = set()
    for i, pdf in enumerate(pdfs, 1):
        try:
            parsed = parse_pdf(pdf)
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(pdfs)}] 解析失败 {pdf.name}: {type(e).__name__}: {e}")
            continue

        # 索引/封面类 PDF（无笔记 ID）不是帖子留档，跳过不算失败
        if not parsed["note_id"]:
            skipped += 1
            print(f"[{i}/{len(pdfs)}] 跳过（非笔记留档，无笔记 ID）: {pdf.name}")
            continue
        # 目录里可能有多份拷贝（留档目录 + 嵌套 pdf/），按 note_id 去重
        if parsed["note_id"] in seen_notes:
            skipped += 1
            print(f"[{i}/{len(pdfs)}] 跳过（重复拷贝）: {pdf.name} "
                  f"note_id={parsed['note_id']}")
            continue
        seen_notes.add(parsed["note_id"])

        head = (f"[{i}/{len(pdfs)}] {parsed['note_id']} "
                f"《{parsed['title'][:30]}》 作者={parsed['nickname']} "
                f"图={len(parsed['photos'])} 评论={len(parsed['comments'])}"
                f"（页面标注 {parsed['interact']['comment_count']}）")
        if args.dry_run:
            print(head, "-> dry-run 跳过入库")
            ok += 1
            continue
        try:
            summary = await import_note(
                parsed,
                save_images=not args.no_images,
                with_comments=not args.no_comments)
            ok += 1
            print(f"{head} -> 入库成功 存图={summary['images']} "
                  f"入评论={summary['comments']}")
        except Exception as e:
            fail += 1
            print(f"{head} -> 入库失败: {type(e).__name__}: {e}")

    print(f"[import] 完成：成功 {ok}，失败 {fail}，跳过 {skipped}")
    return 0 if fail == 0 else 2


def main() -> None:
    ap = argparse.ArgumentParser(description="小红书 PDF 留档导入 pgsql")
    ap.add_argument("--dir", help="留档目录（递归找 PDF）")
    ap.add_argument("--file", help="单个 PDF 文件")
    ap.add_argument("--dry-run", action="store_true", help="只解析不入库")
    ap.add_argument("--no-images", action="store_true", help="不抽取内嵌图片")
    ap.add_argument("--no-comments", action="store_true", help="不导入评论")
    args = ap.parse_args()
    if not args.dir and not args.file:
        ap.error("必须指定 --dir 或 --file")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()

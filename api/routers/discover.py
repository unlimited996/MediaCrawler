# -*- coding: utf-8 -*-
"""发现页接口：HomeFeed 与热搜榜单（方案 #10）。

GET  /api/discover/boards              支持的榜单平台
POST /api/discover/hot/capture         实时抓取并落库（一份快照）
GET  /api/discover/hot                 最新一份榜单
GET  /api/discover/hot/board/{id}      指定快照的榜单内容
DELETE /api/discover/hot/board/{id}    删除一份快照
GET  /api/discover/hot/history         历史快照时间轴（按东八区策展）
GET  /api/discover/hot/compare         两份快照对比（新进/退出/升降）
GET  /api/discover/homefeed            HomeFeed 列表

时间轴策展规则（均按东八区 UTC+8 的日历日/自然小时计算）：
- 今天：最多 10 条；同一自然小时内只保留最新的一条（新的覆盖旧的）
- 昨天：只显示 1 条（最新的一条，不占用今天的 10 条份额）
- 前天：只显示 1 条
- 更早：不显示（最多展示前 3 天）
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from tools.cst import cst_iso, cst_now, fmt_cst, fmt_cst_short, to_cst

router = APIRouter(prefix="/discover", tags=["discover"])

# 时间轴策展参数
MAX_TODAY_NODES = 10      # 今天最多展示几条
PAST_DAYS_NODES = 1       # 昨天/前天各展示几条
MAX_DAY_SPAN = 3          # 最多展示前几天的历史（含今天）


async def _session():
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return None, None
    return engine, AsyncSession(engine, expire_on_commit=False)


@router.get("/boards")
async def list_boards():
    from discovery.hot import SUPPORTED_HOT_BOARDS
    from discovery.homefeed import SUPPORTED_HOMEFEED

    return {
        "hot": [{"platform": p, "board_code": c["code"], "board_name": c["name"]}
                for p, c in SUPPORTED_HOT_BOARDS.items()],
        "homefeed": list(SUPPORTED_HOMEFEED),
    }


@router.post("/hot/capture")
async def capture_hot(platform: str = "wb", limit: Optional[int] = Query(None, ge=1, le=200)):
    # limit=None 表示不截断，取平台返回的全部热搜（微博通常 ≥50 条）
    from discovery.hot import capture_hot_board

    try:
        result = await capture_hot_board(platform, limit)
        return {"code": 0, "data": result}
    except ValueError as e:
        return {"code": 40001, "message": str(e)}
    except Exception as e:
        return {"code": 50001, "message": f"{type(e).__name__}: {str(e)[:300]}"}


@router.get("/hot")
async def latest_hot(platform: str = "wb", board_code: Optional[str] = None):
    from database.models_v2 import HotBoard, HotBoardItem

    engine, session = await _session()
    if session is None:
        return {"code": 50001, "message": "配置库不可用"}
    try:
        q = select(HotBoard).where(HotBoard.platform == platform)
        if board_code:
            q = q.where(HotBoard.board_code == board_code)
        board = (await session.execute(
            q.order_by(desc(HotBoard.captured_at)).limit(1))).scalar_one_or_none()
        if not board:
            return {"code": 0, "data": None, "message": "尚无榜单快照，请先调用 capture"}

        items = (await session.execute(
            select(HotBoardItem).where(HotBoardItem.board_id == board.id)
            .order_by(HotBoardItem.rank_no)
        )).scalars().all()

        return {"code": 0, "data": {
            "board_id": board.id,
            "platform": board.platform,
            "board_code": board.board_code,
            "board_name": board.board_name,
            "captured_at": cst_iso(board.captured_at),
            "captured_at_local": fmt_cst(board.captured_at),
            "items": [{
                "rank_no": i.rank_no, "title": i.title,
                "hot_value": i.hot_value, "hot_value_text": i.hot_value_text,
                "content_url": i.content_url, "tag": i.tag,
            } for i in items],
        }}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/hot/board/{board_id}")
async def get_board(board_id: int):
    """取指定快照的榜单内容（时光机按时间点回看用）。"""
    from database.models_v2 import HotBoard, HotBoardItem

    engine, session = await _session()
    if session is None:
        return {"code": 50001, "message": "配置库不可用"}
    try:
        board = (await session.execute(
            select(HotBoard).where(HotBoard.id == board_id)
        )).scalar_one_or_none()
        if not board:
            return {"code": 40401, "message": "榜单快照不存在"}

        items = (await session.execute(
            select(HotBoardItem).where(HotBoardItem.board_id == board.id)
            .order_by(HotBoardItem.rank_no)
        )).scalars().all()

        return {"code": 0, "data": {
            "board_id": board.id,
            "platform": board.platform,
            "board_code": board.board_code,
            "board_name": board.board_name,
            "captured_at": cst_iso(board.captured_at),
            "captured_at_local": fmt_cst(board.captured_at),
            "items": [{
                "rank_no": i.rank_no, "title": i.title,
                "hot_value": i.hot_value, "hot_value_text": i.hot_value_text,
                "content_url": i.content_url, "tag": i.tag,
            } for i in items],
        }}
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/hot/board/{board_id}")
async def delete_board(board_id: int):
    """删除一份榜单快照（条目随快照级联删除）。"""
    from sqlalchemy import delete

    from database.models_v2 import HotBoard

    engine, session = await _session()
    if session is None:
        return {"code": 50001, "message": "配置库不可用"}
    try:
        board = (await session.execute(
            select(HotBoard).where(HotBoard.id == board_id)
        )).scalar_one_or_none()
        if not board:
            return {"code": 40401, "message": "榜单快照不存在"}

        await session.execute(delete(HotBoard).where(HotBoard.id == board_id))
        await session.commit()
        return {"code": 0, "data": {"deleted": board_id}}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/hot/history")
async def hot_history(platform: Optional[str] = None, limit: int = Query(100, ge=1, le=200)):
    """历史快照时间轴（按东八区策展，返回已按时间升序排好的节点）。

    策展规则见模块 docstring：今天 ≤10 条（每小时仅最新一条）、
    昨天/前天各 1 条、最多前 3 天。
    """
    from database.models_v2 import HotBoard

    engine, session = await _session()
    if session is None:
        return {"items": []}
    try:
        q = select(HotBoard)
        if platform:
            q = q.where(HotBoard.platform == platform)
        rows = (await session.execute(
            q.order_by(desc(HotBoard.captured_at)).limit(limit))).scalars().all()

        today = cst_now().date()

        # 按东八区日历日分组（rows 已按时间倒序）
        by_day: Dict[Any, List[Any]] = {}
        for r in rows:
            by_day.setdefault(to_cst(r.captured_at).date(), []).append(r)

        curated: List[Any] = []
        for day in sorted(by_day.keys(), reverse=True):
            offset = (today - day).days
            if offset < 0 or offset >= MAX_DAY_SPAN:
                continue
            day_rows = by_day[day]          # 时间倒序
            if offset == 0:
                # 今天：同一自然小时只保留最新一条，最多 MAX_TODAY_NODES 条
                seen_hours = set()
                kept = []
                for r in day_rows:
                    h = to_cst(r.captured_at).hour
                    if h in seen_hours:
                        continue
                    seen_hours.add(h)
                    kept.append(r)
                    if len(kept) >= MAX_TODAY_NODES:
                        break
                curated.extend(kept)
            else:
                # 昨天/前天：只保留最新的一条
                curated.extend(day_rows[:PAST_DAYS_NODES])

        curated.sort(key=lambda r: r.captured_at)   # 时间轴按旧 → 新

        items = []
        for r in curated:
            cst = to_cst(r.captured_at)
            offset = (today - cst.date()).days
            items.append({
                "board_id": r.id,
                "platform": r.platform,
                "board_code": r.board_code,
                "board_name": r.board_name,
                "captured_at": cst_iso(r.captured_at),
                "captured_at_local": fmt_cst(r.captured_at),
                "captured_at_short": fmt_cst_short(r.captured_at),
                "day_offset": offset,
                "hour": cst.hour,
                "item_count": r.item_count,
            })
        return {"items": items, "today": today.isoformat()}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/hot/compare")
async def compare_hot(board_a: int, board_b: int):
    """对比两份榜单快照：新进榜 / 已退出 / 排名上升 / 排名下降。"""
    from database.models_v2 import HotBoard, HotBoardItem

    engine, session = await _session()
    if session is None:
        return {"code": 50001, "message": "配置库不可用"}
    try:
        async def _items(board_id: int) -> Dict[str, int]:
            rows = (await session.execute(
                select(HotBoardItem).where(HotBoardItem.board_id == board_id)
            )).scalars().all()
            return {r.title: r.rank_no for r in rows}

        a = await _items(board_a)
        b = await _items(board_b)
        if not a or not b:
            return {"code": 40401, "message": "榜单不存在或为空"}

        new = [t for t in b if t not in a]
        gone = [t for t in a if t not in b]
        moved = []
        for t, rank_b in b.items():
            if t in a:
                delta = a[t] - rank_b
                if delta != 0:
                    moved.append({"title": t, "from": a[t], "to": rank_b,
                                  "delta": delta})   # delta>0 表示上升
        moved.sort(key=lambda x: -abs(x["delta"]))

        return {"code": 0, "data": {
            "board_a": board_a, "board_b": board_b,
            "new": new, "gone": gone, "moved": moved[:30],
            "unchanged_count": len([t for t in b if t in a and a[t] == b[t]]),
        }}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/homefeed")
async def list_homefeed(platform: Optional[str] = None,
                        sort: str = "publish",
                        page: int = Query(1, ge=1),
                        page_size: int = Query(20, ge=1, le=100)):
    from database.models_v2 import HomefeedContent

    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0}
    try:
        q = select(HomefeedContent)
        if platform:
            q = q.where(HomefeedContent.platform == platform)
        order = (HomefeedContent.publish_ts.desc() if sort == "publish"
                 else HomefeedContent.liked_count.desc())
        rows = (await session.execute(
            q.order_by(order).offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        return {
            "items": [{
                "id": r.id, "platform": r.platform, "content_id": r.content_id,
                "title": r.title, "desc": (r.desc or "")[:200],
                "author_nickname": r.author_nickname, "cover_url": r.cover_url,
                "content_url": r.content_url, "liked_count": r.liked_count,
                "comment_count": r.comment_count, "publish_ts": r.publish_ts,
            } for r in rows],
            "total": len(rows), "page": page, "page_size": page_size,
        }
    finally:
        await session.close()
        await engine.dispose()

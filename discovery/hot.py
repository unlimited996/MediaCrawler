# -*- coding: utf-8 -*-
"""热搜榜单抓取。

优先用公开接口（不需要登录和签名），因此这部分**不依赖浏览器**，
在无浏览器模式下也能跑。新增平台只需在这里加一个函数并登记到 SUPPORTED_HOT_BOARDS。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from tools.cst import cst_hour_bounds, fmt_cst, utc_now

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _safe_int(v) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


async def _get_json(url: str, headers: Optional[Dict] = None, timeout: float = 15.0,
                    params: Optional[Dict] = None):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if headers:
        h.update(headers)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=h, params=params)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# 各平台实现
# ---------------------------------------------------------------------------

# 微博热搜接口（ajax/side/hotSearch）会在 realtime 里混入商业推广项。
# 实证：2026-10-19「代言人热巴出席阿道夫品牌大会」以 rank 7 插在中段，
# 靠位置/热度都区分不出来，只能靠接口自带的广告标识。
# 已知标识字段：is_ad / topic_ad / ad_channel / label_name / flag_desc。
_AD_LABEL_WORDS = ("广告", "ad", "ads", "promotion", "推广")


def _is_weibo_ad(it: Dict[str, Any]) -> bool:
    """判断微博热搜条目是否为广告/商业推广。"""
    if not isinstance(it, dict):
        return False
    # 1. 显式布尔标
    if it.get("is_ad") in (1, True, "1", "true", "True"):
        return True
    if it.get("topic_ad"):
        return True
    if it.get("ad_channel"):
        return True
    # 2. 文本标签里带广告字样（label_name 如 "广告"、flag_desc 如 "广告推广"）
    for k in ("label_name", "flag_desc", "category", "flag", "icon"):
        v = str(it.get(k) or "").strip()
        if not v:
            continue
        if any(w in v.lower() for w in _AD_LABEL_WORDS):
            return True
    return False


async def fetch_weibo_hot(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """微博热搜（公开接口）。limit=None 表示不截断，取接口返回的全部（≥50 条）。"""
    # 必须带 Referer，否则返回 403
    data = await _get_json(
        "https://weibo.com/ajax/side/hotSearch",
        headers={"Referer": "https://weibo.com/", "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    realtime = (data.get("data") or {}).get("realtime") or []
    # 先剔广告再截断：广告项若排在前列，先截断会把真热搜挤出榜单；
    # 剔完再连续编号，rank 才反映真实榜位
    realtime = [it for it in realtime if not _is_weibo_ad(it)]
    if limit:
        realtime = realtime[:limit]
    items: List[Dict[str, Any]] = []
    for idx, it in enumerate(realtime[:limit], 1):
        word = it.get("word") or it.get("note") or ""
        if not word:
            continue
        num = it.get("num") or it.get("real_hot") or 0
        items.append({
            "rank_no": idx,
            "title": word,
            "hot_value": _safe_int(num),
            "hot_value_text": str(num),
            "content_url": f"https://s.weibo.com/weibo?q={quote(word)}",
            "tag": it.get("category") or it.get("label_name") or it.get("flag_desc"),
        })
    return items


async def fetch_zhihu_hot(limit: Optional[int] = None, cookie: Optional[str] = None) -> List[Dict[str, Any]]:
    """知乎热榜。**需要登录态**（无 cookie 会返回 401 身份未经过验证）。"""
    url = "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total"
    headers = {"Referer": "https://www.zhihu.com/hot",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    data = await _get_json(url, headers=headers, params={"limit": limit, "desktop": "true"})
    rows = data.get("data") or []
    if limit:
        rows = rows[:limit]
    items: List[Dict[str, Any]] = []
    for idx, it in enumerate(rows[:limit], 1):
        target = it.get("target") or {}
        title = target.get("title") or it.get("title") or ""
        if not title:
            continue
        detail = it.get("detail_text") or ""      # 形如 "1234 万热度"
        num = None
        if detail:
            digits = "".join(ch for ch in detail if ch.isdigit())
            num = _safe_int(digits) if digits else None
        items.append({
            "rank_no": idx,
            "title": title,
            "hot_value": num,
            "hot_value_text": detail or None,
            "content_url": target.get("url") or None,
            "tag": it.get("target", {}).get("type") or None,
        })
    return items


async def fetch_douyin_hot(limit: Optional[int] = None, cookie: Optional[str] = None) -> List[Dict[str, Any]]:
    """抖音热榜（通常需要登录态，接口随平台调整可能变化）。"""
    url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
           "?device_platform=webapp&aid=6383")
    headers = {"Referer": "https://www.douyin.com/"}
    if cookie:
        headers["Cookie"] = cookie
    data = await _get_json(url, headers=headers)
    word_list = ((data.get("data") or {}).get("word_list")) or []
    if limit:
        word_list = word_list[:limit]
    items: List[Dict[str, Any]] = []
    for idx, it in enumerate(word_list[:limit], 1):
        word = it.get("word") or ""
        if not word:
            continue
        items.append({
            "rank_no": idx,
            "title": word,
            "hot_value": _safe_int(it.get("hot_value")),
            "hot_value_text": str(it.get("hot_value") or ""),
            "content_url": f"https://www.douyin.com/hot/{it.get('sentence_id') or ''}",
            "tag": it.get("group_id") or None,
        })
    return items


async def fetch_bili_hot(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """B 站热搜（公开接口，需带 Referer）。"""
    url = "https://api.bilibili.com/x/web-interface/wbi/search/square"
    data = await _get_json(
        url,
        headers={"Referer": "https://www.bilibili.com/"},
        params={"limit": 10, "platform": "web"},
    )
    trending = (data.get("data") or {}).get("trending") or {}
    lst = trending.get("list") or []
    if limit:
        lst = lst[:limit]
    items: List[Dict[str, Any]] = []
    for idx, it in enumerate(lst, 1):
        keyword = it.get("keyword") or it.get("show_name") or ""
        if not keyword:
            continue
        items.append({
            "rank_no": idx,
            "title": keyword,
            "hot_value": None,
            "hot_value_text": None,          # B 站不返回热度数值
            "content_url": f"https://search.bilibili.com/all?keyword={quote(keyword)}",
            "tag": None,
        })
    return items


# 平台 → (榜单代码, 榜单名, 抓取函数)
# 小红书没有公开的热搜榜单接口（只有需登录的首页推荐流），故不提供。
SUPPORTED_HOT_BOARDS: Dict[str, Dict[str, Any]] = {
    "wb": {"code": "wb_hot", "name": "微博热搜", "fetch": fetch_weibo_hot,
           "requires_login": False},
    "bili": {"code": "bili_hot", "name": "B站热搜", "fetch": fetch_bili_hot,
             "requires_login": False},
    "zhihu": {"code": "zhihu_hot", "name": "知乎热榜", "fetch": fetch_zhihu_hot,
              "requires_login": True},
    "dy": {"code": "dy_hot", "name": "抖音热榜", "fetch": fetch_douyin_hot,
           "requires_login": True},
}


async def _cookie_for(platform: str) -> Optional[str]:
    """需要登录的平台，从账号池取 Cookie（方案 #2 的衔接点）。"""
    try:
        from account import pick_account

        acc = await pick_account(platform)
        return acc.get("cookie") if acc else None
    except Exception:
        return None


async def fetch_hot_board(platform: str, limit: Optional[int] = None) -> Dict[str, Any]:
    """抓取某平台热榜（只抓不存）。"""
    conf = SUPPORTED_HOT_BOARDS.get(platform)
    if not conf:
        raise ValueError(
            f"暂不支持该平台的热榜：{platform}，已支持 {list(SUPPORTED_HOT_BOARDS)}")

    fetch = conf["fetch"]
    if conf.get("requires_login"):
        cookie = await _cookie_for(platform)
        items = await fetch(limit, cookie=cookie)
    else:
        items = await fetch(limit)
    return {
        "platform": platform,
        "board_code": conf["code"],
        "board_name": conf["name"],
        "captured_at": utc_now().isoformat(timespec="seconds"),
        "captured_at_cst": fmt_cst(utc_now()),
        "items": items,
    }


async def capture_hot_board(platform: str, limit: Optional[int] = None,
                            task_id: Optional[int] = None) -> Dict[str, Any]:
    """抓取并落库（一次抓取 = 一份榜单快照，支持历史对比）。

    同一平台、同一东八区自然小时内的旧快照会被新快照**覆盖**
    （时间轴规则：每小时最多 1 条，新的覆盖旧的并重新计时）。
    """
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from database.models_v2 import HotBoard, HotBoardItem

    board = await fetch_hot_board(platform, limit)
    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        raise RuntimeError("配置库不可用")

    now_utc = utc_now()
    hour_start, hour_end = cst_hour_bounds(now_utc)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        # 覆盖同一自然小时内的旧快照（HotBoardItem 随 board 级联删除）
        await session.execute(delete(HotBoard).where(
            HotBoard.platform == platform,
            HotBoard.captured_at >= hour_start,
            HotBoard.captured_at < hour_end,
        ))

        snap = HotBoard(
            platform=platform,
            board_code=board["board_code"],
            board_name=board["board_name"],
            captured_at=now_utc,
            task_id=task_id,
            item_count=len(board["items"]),
        )
        session.add(snap)
        await session.flush()

        for it in board["items"]:
            session.add(HotBoardItem(
                board_id=snap.id,
                rank_no=it["rank_no"],
                title=it["title"],
                hot_value=it.get("hot_value"),
                hot_value_text=it.get("hot_value_text"),
                content_url=it.get("content_url"),
                tag=it.get("tag"),
            ))
        await session.commit()
        board_id = snap.id

    await engine.dispose()
    return {"board_id": board_id, **{k: v for k, v in board.items() if k != "items"},
            "item_count": len(board["items"])}

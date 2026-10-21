# -*- coding: utf-8 -*-
"""小红书「免登录」抓取通道：直接解析 explore 详情页的 SSR 数据。

背景（实测结论，2026-10）
------------------------
小红书 web 详情页 `https://www.xiaohongshu.com/explore/<note_id>` 的服务端渲染
HTML 里内嵌 `window.__INITIAL_STATE__`，其中 `note.noteDetailMap` 含完整笔记
（标题/正文/图片/作者/互动数/首屏评论）。这条路径：

* **不需要 cookie、不需要登录、不需要 x-s 签名** —— 只是一次普通 GET
* **前提是 URL 里带的 xsec_token 仍然有效**

xsec_token 由平台签发、与 note_id 绑定、有时效（分享出来的通常只有几小时）。
实测 30 条历史链接：29 条 token 已过期 → 302 到 /login 登录墙；
把 URL 改写成 app_share 格式**完全没有区别**（A/B 结果一致），
说明**能不能看只取决于 token 新鲜度，与 URL 格式无关**。

注意：未登录状态下无法自己换发新 token —— 搜索/首页接口一律返回
`code=-101 无登录信息`（已用 Playwright 抓包验证）。
新 token 只能来自：① 登录态下的搜索/首页接口（现有 crawler 路径）；
② 用户手动分享出来的链接。所以本模块定位是「**拿到分享链接就能免登录抓**」，
以及现有签名/登录链路失效时的兜底通道。
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

from tools import utils

# 桌面版 Chrome UA：小红书对非浏览器 UA 直接返回风控页
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}

# 详情页可能出现在这些路径下（含短链 /discovery/item/ 与 /explore/）
_NOTE_PATH_RE = re.compile(r"/(?:explore|discovery/item|item)/(?P<note_id>[0-9a-fA-F]{16,32})")


class SsrFetchError(Exception):
    """免登录抓取失败。kind 用于上层区分处理：

    - ``login_wall``  token 过期 / 被要求登录（需要换一条新鲜分享链接）
    - ``not_found``   笔记不存在或已被作者删除
    - ``no_ssr``      页面里没有 __INITIAL_STATE__（风控页 / 异常返回）
    - ``parse``       SSR 数据存在但结构不符合预期
    - ``network``     网络层异常
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def parse_note_url(url: str) -> Tuple[str, str, str]:
    """从小红书链接里拆出 (note_id, xsec_token, xsec_source)。

    支持完整分享链接（带一堆 app_platform/shareRedId 等参数）与裸链接。
    note_id 解析不出来时返回空串，由调用方决定报错方式。
    """
    if not url:
        return "", "", ""
    raw = url.strip()
    if "//" not in raw:  # 允许直接传 note_id
        raw = "https://www.xiaohongshu.com/explore/" + raw
    parsed = urlparse(raw)
    qs = parse_qs(parsed.query)
    token = (qs.get("xsec_token") or [""])[0]
    source = (qs.get("xsec_source") or [""])[0]

    m = _NOTE_PATH_RE.search(parsed.path)
    note_id = m.group("note_id") if m else ""
    if not note_id:
        # 形如 /user/profile/xxx 或带 ?noteId= 的跳转链接
        note_id = (qs.get("noteId") or qs.get("note_id") or [""])[0]
    return note_id, token, source


def build_explore_url(note_id: str, xsec_token: str, xsec_source: str = "app_share") -> str:
    """拼出免登录可访问的详情页 URL（token 必须仍然有效）。"""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source={xsec_source}&type=normal"
    return url


def extract_initial_state(html: str) -> Dict[str, Any]:
    """从 HTML 里抠出 `window.__INITIAL_STATE__` 并反序列化。

    小红书这段是 JS 字面量而非严格 JSON（含 `undefined`），
    统一替换成 `null` 再 json.loads。
    """
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        raise SsrFetchError("no_ssr", "页面中没有 window.__INITIAL_STATE__（可能是风控页）")
    raw = m.group(1).replace("undefined", "null")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SsrFetchError("parse", f"__INITIAL_STATE__ 不是合法 JSON: {e}")


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def ssr_to_note_item(note_id: str, detail: Dict[str, Any],
                     xsec_token: str = "") -> Dict[str, Any]:
    """把 SSR 的 noteDetailMap 条目转成 store.xhs 期望的**原始**结构，
    这样可以直接喂给 `store.xhs.update_xhs_note()`，复用下游全部逻辑
    （v1 落库 + v2 同步 + 媒体下载 + 作者主页 URL）。
    """
    note = _as_dict(detail.get("note"))
    if not note:
        raise SsrFetchError("parse", "noteDetailMap 里没有 note 对象")
    user = _as_dict(note.get("user"))
    inter = _as_dict(note.get("interactInfo"))

    images: List[Dict[str, Any]] = []
    for img in _as_list(note.get("imageList")):
        img = _as_dict(img)
        if not img:
            continue
        images.append({
            "url_default": img.get("urlDefault") or img.get("url") or "",
            "url": img.get("url") or img.get("urlDefault") or "",
            "width": img.get("width"),
            "height": img.get("height"),
            "live_photo": img.get("livePhoto", False),
        })

    return {
        "note_id": note.get("noteId") or note_id,
        "type": note.get("type") or "normal",
        "title": note.get("title") or "",
        "desc": note.get("desc") or "",
        "time": note.get("time"),
        "last_update_time": note.get("lastUpdateTime") or note.get("time"),
        "ip_location": note.get("ipLocation") or "",
        "user": {
            "user_id": user.get("userId") or "",
            "nickname": user.get("nickname") or "",
            "avatar": user.get("avatar") or "",
            "xsec_token": user.get("xsecToken") or "",
        },
        "interact_info": {
            "liked_count": inter.get("likedCount") or inter.get("niceCount") or "0",
            "collected_count": inter.get("collectedCount") or "0",
            "comment_count": inter.get("commentCount") or "0",
            "share_count": inter.get("shareCount") or "0",
        },
        "image_list": images,
        "tag_list": _as_list(note.get("tagList")),
        "video": note.get("video"),
        "xsec_token": xsec_token or note.get("xsecToken") or "",
    }


def ssr_to_comment_items(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 SSR 首屏评论转成 store.xhs 期望的结构（含楼中楼）。"""
    out: List[Dict[str, Any]] = []
    for cm in _as_list(_as_dict(detail.get("comments")).get("list")):
        cm = _as_dict(cm)
        if not cm or not cm.get("id"):
            continue
        user = _as_dict(cm.get("userInfo") or cm.get("user"))
        out.append({
            "id": cm.get("id"),
            "note_id": cm.get("noteId") or "",
            "content": cm.get("content") or "",
            "create_time": cm.get("createTime"),
            "like_count": cm.get("likeCount") or 0,
            "sub_comment_count": cm.get("subCommentCount") or 0,
            "pictures": [{"url_default": p.get("urlDefault") or p.get("url") or ""}
                         for p in _as_list(cm.get("pictures"))],
            "user_info": {
                "user_id": user.get("userId") or "",
                "nickname": user.get("nickname") or "",
                # 评论接口的头像字段叫 image，pick_avatar 的候选键已包含
                "image": user.get("image") or user.get("avatar") or "",
            },
            "target_comment": {"id": cm.get("targetComment", {}).get("id", "")
                               if isinstance(cm.get("targetComment"), dict) else ""},
        })
        for sub in _as_list(cm.get("subComments")):
            sub = _as_dict(sub)
            if not sub or not sub.get("id"):
                continue
            su = _as_dict(sub.get("userInfo") or sub.get("user"))
            out.append({
                "id": sub.get("id"),
                "note_id": sub.get("noteId") or cm.get("noteId") or "",
                "content": sub.get("content") or "",
                "create_time": sub.get("createTime"),
                "like_count": sub.get("likeCount") or 0,
                "sub_comment_count": 0,
                "pictures": [{"url_default": p.get("urlDefault") or p.get("url") or ""}
                             for p in _as_list(sub.get("pictures"))],
                "user_info": {
                    "user_id": su.get("userId") or "",
                    "nickname": su.get("nickname") or "",
                    "image": su.get("image") or su.get("avatar") or "",
                },
                "target_comment": {"id": cm.get("id")},
            })
    return out


async def fetch_note_by_url(url: str, *, timeout: float = 25.0,
                            client: Optional[httpx.AsyncClient] = None
                            ) -> Dict[str, Any]:
    """免登录抓取一篇笔记，返回可直接交给 store 的原始结构。

    返回的 dict：`{"note_item": {...}, "comments": [...], "note_id": str, "url": str}`
    """
    note_id, token, source = parse_note_url(url)
    if not note_id:
        raise SsrFetchError("parse", f"无法从链接里解析 note_id: {url[:80]}")
    target = url if token else build_explore_url(note_id, token, source or "app_share")

    owns_client = client is None
    cli = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=_HEADERS)
    try:
        try:
            resp = await cli.get(target)
        except httpx.HTTPError as e:
            raise SsrFetchError("network", f"请求失败: {type(e).__name__} {e}")

        final_url = str(resp.url)
        if resp.status_code != 200:
            raise SsrFetchError("network", f"HTTP {resp.status_code}")
        if "/login" in final_url:
            raise SsrFetchError("login_wall",
                                "被重定向到登录页：xsec_token 已过期或无效，需要一条新的分享链接")
        if "/404" in final_url or "你访问的页面不见了" in resp.text:
            raise SsrFetchError("not_found", "笔记不存在或已删除")

        state = extract_initial_state(resp.text)
        detail_map = _as_dict(_as_dict(state.get("note")).get("noteDetailMap"))
        if not detail_map:
            raise SsrFetchError("login_wall",
                                "SSR 里没有笔记数据（多半是 token 失效被降级）")
        detail = _as_dict(detail_map.get(note_id) or next(iter(detail_map.values()), {}))
        note_item = ssr_to_note_item(note_id, detail, token)
        return {
            "note_id": note_id,
            "url": target,
            "note_item": note_item,
            "comments": ssr_to_comment_items(detail),
        }
    finally:
        if owns_client:
            await cli.aclose()


async def probe_url(url: str, *, timeout: float = 20.0) -> Dict[str, Any]:
    """只探测一条链接当前能否免登录打开，不解析内容。

    返回 `{"ok": bool, "kind": str, "note_id": str, "detail": str}`，
    kind 取值同 SsrFetchError：ok / login_wall / not_found / no_ssr / parse / network。
    """
    note_id, _token, _src = parse_note_url(url)
    try:
        data = await fetch_note_by_url(url, timeout=timeout)
        title = data["note_item"].get("title") or ""
        return {"ok": True, "kind": "ok", "note_id": note_id,
                "detail": f"{title[:30]}"}
    except SsrFetchError as e:
        return {"ok": False, "kind": e.kind, "note_id": note_id, "detail": str(e)[:120]}
    except Exception as e:  # 兜底，探测不该抛
        return {"ok": False, "kind": "parse", "note_id": note_id,
                "detail": f"{type(e).__name__}: {e}"[:120]}


async def fetch_and_store(url: str, *, with_comments: bool = True) -> Dict[str, Any]:
    """抓一篇笔记并直接入库（v1 + v2），返回抓取结果摘要。

    复用 store.xhs.update_xhs_note / update_xhs_note_comment，
    下游的媒体下载、v2 同步、作者主页 URL 全部自动生效。
    """
    from store.xhs import update_xhs_note, update_xhs_note_comment

    data = await fetch_note_by_url(url)
    await update_xhs_note(data["note_item"])
    n_comments = 0
    if with_comments:
        for cm in data["comments"]:
            await update_xhs_note_comment(data["note_id"], cm)
            n_comments += 1
    utils.logger.info(
        f"[xhs.ssr] 免登录抓取成功 note_id={data['note_id']} 评论={n_comments}")
    return {**data, "stored_comments": n_comments}

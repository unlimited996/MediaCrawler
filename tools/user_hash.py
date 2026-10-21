# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# 本文件为 MediaCrawler 的一部分。
#
# 隐私策略（按使用方要求调整）：
# - 昵称：**不再脱敏**，按平台返回的原文存储（`mask_nickname` 已退化为直通函数，
#   保留函数名是为了不动上层 20+ 处调用点）。
# - 用户 ID：仍转匿名哈希（`anonymize_user_id`），仅用于创作者分组聚合，
#   不暴露真实身份；如需同样保留原始 ID，可自行改为直通。
import hashlib


def anonymize_user_id(user_id) -> str:
    """把原始用户 ID 转成匿名哈希，用于内容/评论记录的创作者分组，
    不暴露真实身份。返回 sha256 截断 16 位的十六进制串。"""
    if user_id is None:
        return ""
    s = str(user_id).strip()
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def mask_nickname(name) -> str:
    """昵称直通：按需求已取消脱敏，原样返回。

    历史说明：上游教学版曾在此做"首尾各留 1 字、中间星号"的脱敏
    （如「珠***椒」）。当前版本取消该行为，存储完整昵称。
    """
    if name is None:
        return ""
    return str(name)


# 各平台作者头像字段名不尽相同，按优先级尝试。
# 注意小红书的两套结构：笔记详情用 user.avatar，评论接口用 user_info.image
# （同名不同义，必须都列，否则评论头像会漏）。
_AVATAR_KEYS = (
    "avatar", "avatar_url", "avatar_medium", "avatar_larger", "avatar_thumb",
    "face", "headUrl", "head_url", "profile_image_url", "avatar_hd",
    "image", "user_avatar", "portrait",
)

# 兜底扫描：以上候选键都没命中时，按「键名像头像 + 值是 http(s) URL」再扫一遍。
# 各平台命名太随意（xhs 评论的 image、B站 member 的 face 等），宁可多认几个键。
_AVATAR_KEY_HINTS = ("avatar", "image", "face", "head", "portrait", "icon")


def normalize_avatar(url) -> str:
    """规范化头像 URL：补齐协议、过滤明显非法的值。

    平台偶尔会返回空串、协议相对地址（//host/x.jpg）或本地路径，
    直接存进库会在前端 <img> 里变成裂图，这里统一处理。
    """
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u or u.lower() in ("none", "null", "undefined"):
        return ""
    if u.startswith("//"):                      # 协议相对地址
        u = "https:" + u
    if not u.startswith(("http://", "https://")):
        return ""                               # 本地路径/相对路径不采用
    if len(u) > 2048:
        return ""
    return u


def pick_avatar(user_info, extra_keys=()) -> str:
    """从平台用户信息 dict 中取头像 URL（按候选字段名依次尝试）。"""
    if not isinstance(user_info, dict):
        return ""
    for key in (*_AVATAR_KEYS, *extra_keys):
        v = user_info.get(key)
        if v:
            normalized = normalize_avatar(v)
            if normalized:
                return normalized
    # 兜底：键名命中头像特征且值是合法 URL（防御各平台的奇葩命名）
    for key, v in user_info.items():
        if not isinstance(v, str) or not v:
            continue
        lk = str(key).lower()
        if any(h in lk for h in _AVATAR_KEY_HINTS):
            normalized = normalize_avatar(v)
            if normalized:
                return normalized
    return ""


# ---------------------------------------------------------------------------
# 用户主页 URL：点击头像/昵称跳转到平台个人主页
# ---------------------------------------------------------------------------

# 各平台用户 ID 在原始 user 对象里的键名（按优先级）
_PROFILE_ID_KEYS = {
    "xhs": ("user_id",),
    "dy": ("sec_uid", "uid"),
    "ks": ("id", "user_id", "author_id"),
    "bili": ("mid",),
    "wb": ("id", "idstr"),
    "tieba": ("portrait", "id"),
    "zhihu": ("url_token", "id"),
}


def build_profile_url(platform: str, user_info, fallback_id: str = "") -> str:
    """按平台规则拼用户主页 URL。

    user_info 是平台返回的**原始** user dict（未匿名化之前的），
    例如小红书 note_card.user、抖音 aweme.author。各平台的 ID 键名
    与主页格式都不同，集中在这里维护。

    注意：store 层落库的 creator_hash 是匿名化结果，不能反推主页，
    所以主页 URL 必须在拍平时用原始 ID 现算。
    """
    if not isinstance(user_info, dict):
        user_info = {}
    keys = _PROFILE_ID_KEYS.get(platform, ())
    uid = ""
    for k in keys:
        v = user_info.get(k)
        if v not in (None, ""):
            uid = str(v)
            break
    if not uid:
        uid = str(fallback_id or "")
    if not uid:
        return ""
    if platform == "xhs":
        return f"https://www.xiaohongshu.com/user/profile/{uid}"
    if platform == "dy":
        return f"https://www.douyin.com/user/{uid}"
    if platform == "ks":
        return f"https://www.kuaishou.com/profile/{uid}"
    if platform == "bili":
        return f"https://space.bilibili.com/{uid}"
    if platform == "wb":
        return f"https://weibo.com/u/{uid}"
    if platform == "tieba":
        from urllib.parse import quote
        return f"https://tieba.baidu.com/home/main?id={quote(uid)}"
    if platform == "zhihu":
        return f"https://www.zhihu.com/people/{uid}"
    return ""

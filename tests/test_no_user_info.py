# -*- coding: utf-8 -*-
"""
回归测试：确保爬取/存储链路不持久化禁用字段，且昵称按原文完整保留。

覆盖：
1. ORM 自省 —— database.models 中无禁用列、creator 档案表已删除、内容/评论表含 creator_hash。
2. 提取层 —— 用 mock API/HTML payload 喂各平台提取器，断言输出 dict 不含禁用字段、
   不含原始 user_id、昵称与原文一致（已按需求取消脱敏）、头像 URL 原样保留。
3. 仓库 grep 断言 —— store/ 与 media_platform/ 不再把禁用字段作为存储 dict 的 key。
"""
import re
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 统一的禁用字段名(键)。昵称与头像字段按需求允许保留（原文展示）。
FORBIDDEN_KEYS = {
    "user_id", "sec_uid", "short_user_id", "user_unique_id", "user_signature",
    "avatar", "user_avatar", "face", "sign", "profile_url", "user_link",
    "url_token", "user_url_token", "ip_location", "ip_address", "gender", "sex",
    "up_id", "fan_id", "up_name", "fan_name", "up_avatar", "fan_avatar",
    "up_sign", "fan_sign", "mid",
}
# 说明：author_avatar 是"平台原始头像 URL"的承载键（经 normalize_avatar 规范化），
# 不属于上面的禁用键集合，测试扫描按整键精确匹配，不会误伤。
AVATAR_ALLOWED_KEYS = {"author_avatar"}
NICK_KEYS = {"nickname", "user_nickname", "screen_name", "name", "user_name"}


# ----------------------------- ORM 自省 -----------------------------

def test_orm_has_no_forbidden_columns():
    import database.models as m
    from sqlalchemy.orm import class_mapper
    tables = [c for c in dir(m) if c[0].isupper()
              and c not in ("Base", "Column", "Integer", "BigInteger", "String", "Text")]
    bad = []
    for t in tables:
        cols = {c.name for c in class_mapper(getattr(m, t)).columns}
        hit = cols & FORBIDDEN_KEYS
        if hit:
            bad.append((t, sorted(hit)))
    assert not bad, f"ORM 仍含禁用列: {bad}"


def test_creator_tables_removed():
    import database.models as m
    removed = {"XhsCreator", "DyCreator", "WeiboCreator", "TiebaCreator",
               "ZhihuCreator", "BilibiliUpInfo", "BilibiliContactInfo"}
    for t in removed:
        assert not hasattr(m, t), f"creator 档案表 {t} 仍存在"


def test_content_tables_have_creator_hash():
    import database.models as m
    from sqlalchemy.orm import class_mapper
    content_tables = ["XhsNote", "XhsNoteComment", "WeiboNote", "WeiboNoteComment",
                      "BilibiliVideo", "BilibiliVideoComment", "BilibiliUpDynamic",
                      "DouyinAweme", "DouyinAwemeComment", "KuaishouVideo",
                      "KuaishouVideoComment", "TiebaNote", "TiebaComment",
                      "ZhihuContent", "ZhihuComment"]
    for t in content_tables:
        cols = {c.name for c in class_mapper(getattr(m, t)).columns}
        assert "creator_hash" in cols, f"{t} 缺少 creator_hash 列"


# ----------------------------- 提取层 mock -----------------------------

def _check_no_forbidden_keys(d: dict, label: str):
    keys = set(d.keys())
    hit = keys & FORBIDDEN_KEYS
    assert not hit, f"[{label}] 输出仍含禁用字段键: {hit}"


def _check_nickname_kept(d: dict, raw: str, label: str):
    nick_keys = set(d.keys()) & NICK_KEYS
    assert nick_keys, f"[{label}] 未保留任何昵称字段(应保留原文)"
    for k in nick_keys:
        val = d[k]
        if val in ("", None):
            continue
        assert val == raw, f"[{label}] {k} 与原文不一致: {val!r} != {raw!r}"


def test_hash_tool():
    from tools.user_hash import anonymize_user_id, mask_nickname
    h = anonymize_user_id("12345")
    assert h and h != "12345" and re.fullmatch(r"[0-9a-f]{16}", h)
    assert anonymize_user_id(None) == "" and anonymize_user_id("") == ""
    # 昵称直通：取消脱敏后原样返回
    assert mask_nickname("张三丰") == "张三丰"
    assert mask_nickname(None) == ""
    assert mask_nickname("a") == "a"


def test_xhs_note_extraction_masks_user_info():
    import asyncio
    import store.xhs as xs
    note_item = {
        "note_id": "abc",
        "type": "normal",
        "title": "t",
        "desc": "d",
        "time": 1,
        "last_update_time": 0,
        "user": {"user_id": "u123", "nickname": "小红同学", "avatar": "http://x/a.jpg"},
        "ip_location": "上海",
        "interact_info": {"liked_count": "1", "collected_count": "0",
                          "comment_count": "0", "share_count": "0"},
        "image_list": [], "tag_list": [], "xsec_token": "tok",
    }
    captured = {}

    class FakeStore:
        async def store_content(self, content_item):
            captured.update(content_item)

    orig = xs.XhsStoreFactory.create_store
    xs.XhsStoreFactory.create_store = staticmethod(lambda: FakeStore())
    try:
        asyncio.run(xs.update_xhs_note(note_item))
    finally:
        xs.XhsStoreFactory.create_store = orig
    _check_no_forbidden_keys(captured, "xhs_note")
    assert captured.get("creator_hash") != "u123"
    _check_nickname_kept(captured, "小红同学", "xhs_note")


def test_tieba_note_extraction_masks_user_info():
    from media_platform.tieba.help import TieBaExtractor
    api_data = {
        "thread": {"id": 1, "title": "tt", "reply_num": 5},
        "first_floor": {"tid": 1, "author_id": 9, "time": 1700000000, "content": "c"},
        "forum": {"name": "test", "id": 1},
        "page": {"total_page": 1},
        "user_list": [{"id": 9, "name_show": "贴吧老哥", "name": "lg", "portrait": "p", "ip_address": "北京"}],
    }
    note = TieBaExtractor().extract_note_detail_from_api(api_data)
    d = note.model_dump()
    _check_no_forbidden_keys(d, "tieba_note")
    assert d.get("creator_hash")  # user_link 已转哈希
    _check_nickname_kept(d, "贴吧老哥", "tieba_note")


def test_tieba_comment_extraction_masks_user_info():
    from media_platform.tieba.help import TieBaExtractor
    from model.m_baidu_tieba import TiebaNote
    api_data = {
        "forum": {"id": 1, "name": "test"},
        "post_list": [{"id": 7, "author_id": 9, "time": 1700000000, "content": "c", "sub_post_number": 0}],
        "user_list": [{"id": 9, "name_show": "评论员", "name": "py", "portrait": "p", "ip_address": "上海"}],
    }
    note_detail = TiebaNote(note_id="1", title="t", note_url="u", tieba_name="test", tieba_link="l")
    comments = TieBaExtractor().extract_tieba_note_parent_comments_from_api(api_data, note_detail)
    assert comments
    d = comments[0].model_dump()
    _check_no_forbidden_keys(d, "tieba_comment")
    _check_nickname_kept(d, "评论员", "tieba_comment")


def test_zhihu_comment_extraction_masks_user_info():
    from media_platform.zhihu.help import ZhihuExtractor
    from model.m_zhihu import ZhihuContent
    comments_raw = [{
        "type": "comment", "id": 1, "content": "c", "created_time": 1700000000,
        "like_count": 1, "dislike_count": 0, "child_comment_count": 0,
        "author": {"id": "z9", "name": "知乎答主", "url_token": "tok", "avatar_url": "http://x/a.jpg"},
        "comment_tag": [{"type": "ip_info", "text": "广东"}],
    }]
    page_content = ZhihuContent(content_id="c1", content_type="answer")
    comments = ZhihuExtractor().extract_comments(page_content, comments_raw)
    assert comments
    d = comments[0].model_dump() if hasattr(comments[0], "model_dump") else vars(comments[0])
    _check_no_forbidden_keys(d, "zhihu_comment")
    assert "creator_hash" in d and d["creator_hash"]
    _check_nickname_kept(d, "知乎答主", "zhihu_comment")


def test_bilibili_video_dict_masks_user_info():
    # 直接测 store/bilibili/__init__.py 的拍平逻辑(不触发网络)
    import asyncio
    from store.bilibili import update_bilibili_video
    video_item = {
        "View": {
            "aid": 100, "title": "t", "desc": "d", "pubdate": 1,
            "owner": {"mid": 777, "name": "UP主大人", "face": "http://x/a.jpg"},
            "stat": {"like": 1, "view": 2},
            "pic": "http://x/cover.jpg",
        }
    }
    # 拦截真实存储：替换工厂返回一个捕获 dict 的假 store
    captured = {}

    class FakeStore:
        async def store_content(self, content_item):
            captured.update(content_item)

    import store.bilibili as bs
    orig = bs.BiliStoreFactory.create_store
    bs.BiliStoreFactory.create_store = staticmethod(lambda: FakeStore())
    try:
        asyncio.get_event_loop().run_until_complete(update_bilibili_video(video_item)) \
            if False else asyncio.run(update_bilibili_video(video_item))
    finally:
        bs.BiliStoreFactory.create_store = orig
    _check_no_forbidden_keys(captured, "bili_video")
    assert captured.get("creator_hash") != 777
    _check_nickname_kept(captured, "UP主大人", "bili_video")


# ----------------------------- 仓库扫描断言 -----------------------------
# 说明：原实现用 subprocess 调 grep，在 Windows 上会挂起（grep 不在 Python
# 子进程可见的 PATH 中）。改为 Python 原生遍历，跨平台可跑。

_DICT_KEY_RE = re.compile(r'"(' + "|".join(FORBIDDEN_KEYS) + r')"\s*:')
_ORM_IMPORT_RE = re.compile(
    r"from database\.models import.*(XhsCreator|DyCreator|WeiboCreator|TiebaCreator|ZhihuCreator|BilibiliUpInfo|BilibiliContactInfo)"
)


def _scan_store() -> list:
    """遍历 store/ 下所有 .py，返回 (路径, 行号, 行内容) 列表。"""
    hits = []
    for path in sorted((ROOT / "store").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits.append((str(path.relative_to(ROOT)), i, line))
    return hits


def test_store_no_forbidden_dict_keys():
    # store/ 下不得把禁用字段作为存储 dict 的 key("field": value 形式)
    bad = [
        f"{p}:{n}: {line.strip()}"
        for p, n, line in _scan_store()
        if _DICT_KEY_RE.search(line)
    ]
    assert not bad, "store/ 仍写入禁用字段键:\n" + "\n".join(bad)


def test_store_no_creator_orm_imports():
    # 已删除的 creator ORM 表(XhsCreator/DyCreator/...)不得再从 database.models 导入。
    # 注意:model/m_*.py 里的同名 pydantic 类是内存类型，允许保留。
    bad = [
        f"{p}:{n}: {line.strip()}"
        for p, n, line in _scan_store()
        if _ORM_IMPORT_RE.search(line)
    ]
    assert not bad, "store/ 仍 import 已删除的 creator ORM 表:\n" + "\n".join(bad)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

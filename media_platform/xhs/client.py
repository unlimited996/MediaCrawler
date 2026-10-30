# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/client.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import json
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union
from urllib.parse import quote, urlencode

# 子评论（楼中楼）翻页参数：
# - SUB_COMMENT_PAGE_NUM：每页条数。原写死 10，热门主评论要翻十几页，
#   限流下很容易中途断掉；提到 30 折中（请求数减半，单页也不超接口上限）。
# - MAX_SUB_COMMENT_PAGES：单条主评论的翻页上限，防接口一直返回
#   has_more=true 时死循环。
SUB_COMMENT_PAGE_NUM = 30
MAX_SUB_COMMENT_PAGES = 100

import httpx
from playwright.async_api import BrowserContext, Page
from tenacity import (
    RetryError,
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_fixed,
)
from tools import anti_risk
from tools.httpx_util import make_async_client

import config
from base.base_crawler import AbstractApiClient
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import (
    CaptchaError,
    DataFetchError,
    IPBlockError,
    NoteHiddenError,
    NoteNotFoundError,
    PlatformAccessError,
)
from .field import SearchNoteType, SearchSortType
from .help import get_search_id
from .extractor import XiaoHongShuExtractor
from .playwright_sign import sign_with_xhshow
from signer import get_signer


class XiaoHongShuClient(AbstractApiClient, ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,  # If media crawling is enabled, Xiaohongshu long videos need longer timeout
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        if config.XHS_INTERNATIONAL:
            self._host = "https://webapi.rednote.com"
            self._domain = "https://www.rednote.com"
        else:
            self._host = "https://edith.xiaohongshu.com"
            self._domain = "https://www.xiaohongshu.com"
        self.cookie_urls = [self._domain]
        self.IP_ERROR_STR = "Network connection error, please check network settings or restart"
        self.IP_ERROR_CODE = 300012
        self.SECURITY_LIMIT_CODE = 300011
        self.NOTE_NOT_FOUND_CODE = -510000
        self.NOTE_ABNORMAL_STR = "Note status abnormal, please check later"
        self.NOTE_ABNORMAL_CODE = -510001
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._extractor = XiaoHongShuExtractor()
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)

    async def _pre_headers(self, url: str, params: Optional[Dict] = None, payload: Optional[Dict] = None) -> Dict:
        """请求头参数签名 (使用 xhshow 纯算法)

        Args:
            url: 请求 URI path
            params: GET 请求参数
            payload: POST 请求参数

        Returns:
            Dict: 签名后的请求头参数
        """
        if params is not None:
            data = params
            method = "GET"
        elif payload is not None:
            data = payload
            method = "POST"
        else:
            raise ValueError("params or payload is required")

        # 走签名抽象层（默认 xhshow 纯算法）；取不到签名器时回退到原实现
        cookie_str = self.headers.get("Cookie", "")
        signer = get_signer("xhs")
        if signer is not None:
            try:
                headers = signer.sign(uri=url, data=data, cookie_str=cookie_str, method=method)
            except Exception as e:  # 签名失败不能让整个请求静默丢头
                utils.logger.warning(
                    f"[XiaoHongShuClient._pre_headers] 签名器异常，回退原实现：{type(e).__name__}: {e}")
                headers = None
        else:
            headers = None

        if not headers:
            signs = sign_with_xhshow(uri=url, data=data, cookie_str=cookie_str, method=method)
            headers = {
                "X-S": signs["x-s"],
                "X-T": signs["x-t"],
                "x-S-Common": signs["x-s-common"],
                "X-B3-Traceid": signs["x-b3-traceid"],
            }
        self.headers.update(headers)
        return self.headers

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        # 这些异常重试毫无意义（删除/隐藏/风控/验证码），直接抛给上层处理
        retry=retry_if_not_exception_type(
            (NoteNotFoundError, NoteHiddenError, IPBlockError,
             PlatformAccessError, CaptchaError)
        ),
    )
    async def request(self, method, url, **kwargs) -> Union[str, Any]:
        """
        Wrapper for httpx common request method, processes request response
        Args:
            method: Request method
            url: Request URL
            **kwargs: Other request parameters, such as headers, body, etc.

        Returns:

        """
        # Check if proxy is expired before each request
        await self._refresh_proxy_if_expired()

        # return response.text
        return_response = kwargs.pop("return_response", False)
        async with make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        if response.status_code in {401, 403, 429}:
            raise PlatformAccessError(
                f"XHS request blocked with HTTP {response.status_code}"
            )

        if response.status_code == 471 or response.status_code == 461:
            # 触发验证码：这里原本抛的是裸 Exception，会被 tenacity 重试 3 次，
            # 等于对着风控再补两发请求（2026-10-28：一轮 100 篇点赞帖把账号
            # 打进 461，每篇评论都重试 3 次 = 300 次无效请求）。改为专用异常 +
            # 不重试，上层拿到后熔断剩余任务。
            # 头可能缺失（部分 461/471 响应不带），取不到就别再抛 KeyError
            verify_type = response.headers.get("Verifytype", "")
            verify_uuid = response.headers.get("Verifyuuid", "")
            msg = f"CAPTCHA appeared, request failed, Verifytype: {verify_type}, Verifyuuid: {verify_uuid}, Response: {response}"
            utils.logger.error(msg)
            raise CaptchaError(msg)

        response_data: Optional[Dict] = None
        try:
            candidate_data = response.json()
            if isinstance(candidate_data, dict):
                response_data = candidate_data
        except (TypeError, ValueError):
            pass

        response_code = (
            str(response_data.get("code"))
            if response_data is not None and response_data.get("code") is not None
            else ""
        )
        if response_code == str(self.IP_ERROR_CODE):
            raise IPBlockError(self.IP_ERROR_STR)
        if response_code == str(self.SECURITY_LIMIT_CODE):
            raise PlatformAccessError(
                f"XHS account security restriction, code: {self.SECURITY_LIMIT_CODE}"
            )

        if return_response:
            return response.text
        data: Dict = response_data if response_data is not None else response.json()
        if data["success"]:
            return data.get("data", data.get("success", {}))
        # IP_ERROR_CODE / SECURITY_LIMIT_CODE are already handled above, before return_response.
        elif data["code"] == self.NOTE_NOT_FOUND_CODE:
            # 笔记已被作者/平台删除（code -510000）：明确"已删除"
            raise NoteNotFoundError(
                f"Note deleted or not found, code: {self.NOTE_NOT_FOUND_CODE}")
        elif data["code"] == self.NOTE_ABNORMAL_CODE:
            # 笔记状态异常（code -510001，"Note status abnormal"）：通常代表作者已隐藏
            # 该笔记（仅自己可见 / 仅粉丝可见），与"已删除"是不同的状态，需分别标记
            raise NoteHiddenError(
                f"Note hidden/abnormal by author, code: {self.NOTE_ABNORMAL_CODE}")
        else:
            err_msg = data.get("msg", None) or f"{response.text}"
            raise DataFetchError(err_msg)

    @staticmethod
    def _build_query_string(params: Dict) -> str:
        """Build URL query string with encoding matching browser behavior (commas not encoded)"""
        parts = []
        for key, value in params.items():
            value_str = str(value) if value is not None else ""
            parts.append(f"{key}={quote(value_str, safe=',')}")
        return "&".join(parts)

    async def get(self, uri: str, params: Optional[Dict] = None) -> Dict:
        """
        GET request, signs request headers
        Args:
            uri: Request route
            params: Request parameters

        Returns:

        """
        headers = await self._pre_headers(uri, params)
        # Build URL manually to ensure query string encoding matches the sign string
        # (httpx's default params encoding differs from browser/XHS frontend behavior)
        if params:
            full_url = f"{self._host}{uri}?{self._build_query_string(params)}"
        else:
            full_url = f"{self._host}{uri}"

        return await self.request(
            method="GET", url=full_url, headers=headers
        )

    async def post(self, uri: str, data: dict, **kwargs) -> Dict:
        """
        POST request, signs request headers
        Args:
            uri: Request route
            data: Request body parameters

        Returns:

        """
        headers = await self._pre_headers(uri, payload=data)
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return await self.request(
            method="POST",
            url=f"{self._host}{uri}",
            data=json_str,
            headers=headers,
            **kwargs,
        )

    async def query_self(self) -> Optional[Dict]:
        """
        Query self user info to check login state
        Returns:
            Dict: User info if logged in, None otherwise
        """
        uri = "/api/sns/web/v1/user/selfinfo"
        headers = await self._pre_headers(uri, params={})
        async with make_async_client(proxy=self.proxy) as client:
            response = await client.get(f"{self._host}{uri}", headers=headers)
            if response.status_code == 200:
                return response.json()
        return None

    async def pong(self) -> bool:
        """
        Check if login state is still valid by querying self user info
        Returns:
            bool: True if logged in, False otherwise
        """
        utils.logger.info("[XiaoHongShuClient.pong] Begin to check login state...")
        ping_flag = False
        try:
            self_info: Dict = await self.query_self()
            if self_info and self_info.get("data", {}).get("result", {}).get("success"):
                ping_flag = True
        except Exception as e:
            utils.logger.error(
                f"[XiaoHongShuClient.pong] Check login state failed: {e}, and try to login again..."
            )
            ping_flag = False
        utils.logger.info(f"[XiaoHongShuClient.pong] Login state result: {ping_flag}")
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        """
        Update cookies method provided by API client, usually called after successful login
        Args:
            browser_context: Browser context object

        Returns:

        """
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def get_note_by_keyword(
        self,
        keyword: str,
        search_id: str = get_search_id(),
        page: int = 1,
        page_size: int = 20,
        sort: SearchSortType = SearchSortType.GENERAL,
        note_type: SearchNoteType = SearchNoteType.ALL,
    ) -> Dict:
        """
        Search notes by keyword
        Args:
            keyword: Keyword parameter
            page: Page number
            page_size: Page data length
            sort: Search result sorting specification
            note_type: Type of note to search

        Returns:

        """
        uri = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": search_id,
            "sort": sort.value,
            "note_type": note_type.value,
        }
        return await self.post(uri, data)

    async def get_note_by_id(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
    ) -> Dict:
        """
        Get note detail API
        Args:
            note_id: Note ID
            xsec_source: Channel source
            xsec_token: Token returned from search keyword result list

        Returns:

        """
        if xsec_source == "":
            xsec_source = "pc_search"

        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source,
            "xsec_token": xsec_token,
        }
        uri = "/api/sns/web/v1/feed"
        res = await self.post(uri, data)
        if res and res.get("items"):
            res_dict: Dict = res["items"][0]["note_card"]
            return res_dict
        # When crawling frequently, some notes may have results while others don't
        utils.logger.error(
            f"[XiaoHongShuClient.get_note_by_id] get note id:{note_id} empty and res:{res}"
        )
        return dict()

    async def get_note_comments(
        self,
        note_id: str,
        xsec_token: str,
        cursor: str = "",
    ) -> Dict:
        """
        Get first-level comments API
        Args:
            note_id: Note ID
            xsec_token: Verification token
            cursor: Pagination cursor

        Returns:

        """
        uri = "/api/sns/web/v2/comment/page"
        params = {
            "note_id": note_id,
            "cursor": cursor,
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
        }
        return await self.get(uri, params)

    async def get_note_sub_comments(
        self,
        note_id: str,
        root_comment_id: str,
        xsec_token: str,
        num: int = 10,
        cursor: str = "",
    ):
        """
        Get sub-comments under specified parent comment API
        Args:
            note_id: Post ID of sub-comments
            root_comment_id: Root comment ID
            xsec_token: Verification token
            num: Pagination quantity
            cursor: Pagination cursor

        Returns:

        """
        uri = "/api/sns/web/v2/comment/sub/page"
        params = {
            "note_id": note_id,
            "root_comment_id": root_comment_id,
            "num": str(num),
            "cursor": cursor,
            "image_formats": "jpg,webp,avif",
            "top_comment_id": "",
            "xsec_token": xsec_token,
        }
        return await self.get(uri, params)

    async def get_note_all_comments(
        self,
        note_id: str,
        xsec_token: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_count: int = 10,
        start_cursor: str = "",
        top_count_in: int = 0,
        sub_cursors: Optional[Dict[str, str]] = None,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Get all first-level comments under specified note, this method will continuously find all comment information under a post

        断点续抓（2026-10-29）：
        支持从「上一次抓取中断处」继续，而不是从头重翻。续抓状态由调用方通过
        start_cursor / top_count_in / sub_cursors 传入（来自持久化进度），并通过
        progress_callback(note_id, state, finished) 每翻完一页/结束时回写进度。

        Args:
            note_id: Note ID
            xsec_token: Verification token
            crawl_interval: Crawl delay per note (seconds)
            callback: 每批评论入库回调（同步写库，幂等 upsert）
            max_count: 单篇一级评论上限（<=0 不限制，翻到 has_more=False）
            start_cursor: 续抓起点（主评论下一页游标）；空串=从头
            top_count_in: 已抓一级评论数（承接 max_count 计数，避免续抓后重复计数）
            sub_cursors: 上一轮没翻完的根评论楼中楼游标 {root_id: cursor}
            progress_callback: 进度持久化回调，签名
                (note_id, {"top_cursor","top_count","sub_cursors","reached_end"}, finished)
        Returns:
            List[Dict] 全部评论（含子评论）
        """
        result = []
        seen_comment_ids: set = set()
        # 跨阶段共享：阶段一登记内嵌预览后，阶段二翻页不会再重复入库
        seen_sub_ids: set = set()
        comments_has_more = True
        comments_cursor = start_cursor or ""
        # max_count 约束的是「一级评论」条数：<=0 表示不限制，一直翻到
        # has_more=False（按原帖显示的评论量抓全）。
        unlimited = max_count <= 0
        top_count = int(top_count_in or 0)
        # 本轮回填 / 续抓后仍没翻完的根评论楼中楼游标（page 级累积，供续抓恢复）
        open_sub: Dict[str, str] = {}
        # 阶段二待展开的主楼（阶段一只收集，不发 sub/page 请求）
        pending_roots: List[Dict] = []

        # -------- 阶段 1：先把整篇一级评论翻到底（真人=把评论列表滚到底）--------
        # 这一阶段只打 comment/page 一个 endpoint，绝不穿插 sub/page。
        # 旧实现是「翻一页主评论 → 立刻翻这页的楼中楼 → 再翻下一页」，
        # 两个 endpoint 交替（A B B A B B…）是典型爬虫行为画像；真人形态
        # 是先把评论列表滚到底（A A A…），再逐条点开回复（B B B…）。
        # 参考小红书留档 skill 的风控结论：被判机器人的不是签名，是请求节奏与形态。
        while comments_has_more and (unlimited or top_count < max_count):
            comments_res = await self.get_note_comments(
                note_id=note_id, xsec_token=xsec_token, cursor=comments_cursor
            )
            comments_has_more = comments_res.get("has_more", False)
            comments_cursor = comments_res.get("cursor", "")
            if "comments" not in comments_res:
                utils.logger.info(
                    f"[XiaoHongShuClient.get_note_all_comments] No 'comments' key found in response: {comments_res}"
                )
                break
            comments = comments_res["comments"]
            # 主评论分页之间存在重叠，同一条评论可能出现在多个分页里，
            # 不去重会导致同一条被重复入库/重复统计（完整率按总数算会严重偏差）。
            comments = [c for c in comments if c.get("id") not in seen_comment_ids]
            seen_comment_ids.update(c.get("id") for c in comments if c.get("id"))
            if not unlimited and top_count + len(comments) > max_count:
                comments = comments[: max_count - top_count]
            top_count += len(comments)
            if callback:
                await callback(note_id, comments)
            await anti_risk.interval_sleep(crawl_interval, "分页/翻页间隔")
            result.extend(comments)
            # 内嵌楼中楼预览：随主评论一起返回，零额外请求，先落库
            result.extend(await self._drain_inline_subs(
                comments, note_id, callback, seen_sub_ids))
            # 只把「确实还有回复要翻」的主楼留到阶段二（省掉大量无效请求）
            pending_roots.extend(c for c in comments if self._sub_needs_paging(c))
            # 每翻完一页就持久化续抓进度（page 级）。
            # 阶段一若被中断，已抓主楼的回复还没翻：把它们以「空游标」记进
            # sub_cursors，下次续抓会从头补翻（空游标=从头，见阶段二处理）。
            if progress_callback:
                pending_map: Dict[str, str] = {
                    c.get("id"): "" for c in pending_roots if c.get("id")}
                pending_map.update(dict(sub_cursors or {}))  # 保留上一轮遗留游标
                await progress_callback(note_id, {
                    "top_cursor": comments_cursor,
                    "top_count": top_count,
                    "sub_cursors": pending_map,
                    "reached_end": (not comments_has_more),
                }, finished=False)

        # -------- 阶段 2：再逐条展开楼中楼（真人=逐条点「展开 N 条回复」）--------
        # 主评论全部翻完后才切到 sub/page endpoint，并连续打完，不再与
        # comment/page 交替。上一轮没翻完的根评论（sub_cursors）也在这里收尾。
        if pending_roots or sub_cursors:
            utils.logger.info(
                f"[XiaoHongShuClient.get_note_all_comments] note {note_id} "
                f"阶段2：展开 {len(pending_roots)} 个主楼的回复"
                + (f"，并收尾上一轮 {len(sub_cursors or {})} 个未完成的楼中楼"
                   if sub_cursors else ""))
            subs, page_open = await self.get_comments_all_sub_comments(
                comments=pending_roots,
                xsec_token=xsec_token,
                crawl_interval=crawl_interval,
                callback=callback,
                note_id=note_id,
                start_sub_cursors=sub_cursors,
                seen_sub_ids=seen_sub_ids,
            )
            result.extend(subs)
            open_sub.update(page_open)
        # 全部翻页结束：通知上层 reached_end（是否翻到 has_more=False）。
        if progress_callback:
            await progress_callback(note_id, {
                "top_cursor": comments_cursor,
                "top_count": top_count,
                "sub_cursors": open_sub,
                "reached_end": (not comments_has_more),
            }, finished=True)
        utils.logger.info(
            f"[XiaoHongShuClient.get_note_all_comments] note {note_id} 抓取结束："
            f"一级评论 {top_count} 条（续抓起点={'有' if start_cursor else '无'}）"
            + ("（不限制，翻到底）" if unlimited else f" / 上限 {max_count}")
            + f"，含子评论共 {len(result)} 条")
        return result

    @staticmethod
    def _sub_needs_paging(comment: Dict) -> bool:
        """该主楼是否还需要「阶段二」去翻楼中楼。

        与 _fetch_sub_pages 的翻页判据保持一致：
        接口给了 has_more 就听它的；没给时用「内嵌预览数 < 声明回复数」兜底。
        返回 False 的根评论在阶段二会被整个跳过——这是省请求量的关键
        （大量主楼根本没有回复，逐条去问就是纯浪费，还会坐实机器人画像）。
        """
        sub_count = int(comment.get("sub_comment_count") or 0)
        inline = comment.get("sub_comments") or []
        if bool(comment.get("sub_comment_has_more")):
            return True
        return bool(sub_count and len(inline) < sub_count)

    async def _drain_inline_subs(
        self,
        comments: List[Dict],
        note_id: Optional[str],
        callback: Optional[Callable],
        seen_sub_ids: set,
    ) -> List[Dict]:
        """把主评论响应里内嵌的楼中楼预览先入库（零额外请求）。

        主评论对象自带 sub_comments 预览，真人滑到这里这些回复已经可见。
        不入库会漏掉大量回复；这里登记进 seen_sub_ids 后，阶段二翻页时
        同一批数据不会被重复写入。
        """
        out: List[Dict] = []
        for c in comments or []:
            inline = c.get("sub_comments") or []
            if not inline:
                continue
            cid = c.get("note_id") or note_id
            fresh = [s for s in inline if s.get("id") and s.get("id") not in seen_sub_ids]
            seen_sub_ids.update(s.get("id") for s in fresh if s.get("id"))
            if fresh:
                if callback:
                    await callback(cid, fresh)
                out.extend(fresh)
        return out

    async def _fetch_sub_pages(
        self,
        note_id: str,
        root_comment_id: str,
        xsec_token: str,
        crawl_interval: float,
        callback: Optional[Callable],
        seen_sub_ids: set,
        inline_subs: Optional[List[Dict]] = None,
        sub_count: int = 0,
        initial_has_more: bool = True,
        initial_cursor: str = "",
    ) -> tuple:
        """翻某个根评论的楼中楼，返回 (评论列表, 未完成游标 or None)。

        把 get_comments_all_sub_comments 里「单根翻页」逻辑抽出来复用：既服务
        正常主评论的楼中楼，也服务「续抓」时从指定游标接着翻。
        返回值的第二个元素：若楼中楼没翻完（接口报错/翻页触顶）返回「最后有效
        游标」供续抓；若正常翻到底返回 None。
        """
        result: List[Dict] = []
        # 1) 主评论响应里内嵌的子评论预览
        if inline_subs:
            fresh = [c for c in inline_subs if c.get("id") not in seen_sub_ids]
            seen_sub_ids.update(c.get("id") for c in fresh if c.get("id"))
            if fresh:
                if callback:
                    await callback(note_id, fresh)
                result.extend(fresh)
        # 2) 是否需要继续翻页（接口没给 has_more 时，用「预览数 < 声明总数」兜底）
        has_more = bool(initial_has_more)
        if not has_more and sub_count and len(inline_subs or []) < sub_count:
            has_more = True
        if not has_more:
            return result, None
        cursor = initial_cursor or ""
        last_cursor = cursor
        page = 0
        while has_more:
            page += 1
            if page > MAX_SUB_COMMENT_PAGES:
                utils.logger.warning(
                    f"[XiaoHongShuClient._fetch_sub_pages] root_comment_id: "
                    f"{root_comment_id} 翻页超过 {MAX_SUB_COMMENT_PAGES} 页，停止")
                return result, last_cursor  # 触顶：未翻完，留待续抓
            try:
                comments_res = await self.get_note_sub_comments(
                    note_id=note_id,
                    root_comment_id=root_comment_id,
                    xsec_token=xsec_token,
                    num=SUB_COMMENT_PAGE_NUM,
                    cursor=cursor,
                )
            except DataFetchError as e:
                utils.logger.warning(
                    f"[XiaoHongShuClient._fetch_sub_pages] Failed to get "
                    f"sub-comments for root_comment_id: {root_comment_id}: {e}. "
                    f"留待续抓。")
                # 失败必须退避再翻下一个根：零间隔连打是风控最明显的特征
                await anti_risk.xhs_sleep(f"root {root_comment_id} 楼中楼失败退避")
                return result, last_cursor  # 报错：未翻完，留待续抓
            if not isinstance(comments_res, dict):
                utils.logger.info(
                    f"[XiaoHongShuClient._fetch_sub_pages] No response for "
                    f"root_comment_id: {root_comment_id}")
                return result, last_cursor
            page_comments = comments_res.get("comments") or []
            utils.logger.info(
                f"[XiaoHongShuClient._fetch_sub_pages] "
                f"root={root_comment_id} page={page} "
                f"has_more={comments_res.get('has_more')} "
                f"cursor={str(comments_res.get('cursor'))[:48]!r} "
                f"got={len(page_comments)} declared={sub_count}")
            has_more = bool(comments_res.get("has_more")
                            or comments_res.get("sub_comment_has_more"))
            new_cursor = comments_res.get("cursor") or ""
            if not page_comments:
                break  # 翻到底
            if new_cursor and new_cursor == cursor:
                break  # 游标没动，防死循环
            cursor = new_cursor
            last_cursor = cursor
            fresh = [c for c in page_comments if c.get("id") not in seen_sub_ids]
            seen_sub_ids.update(c.get("id") for c in fresh if c.get("id"))
            if not fresh:
                # 整页都是已采过的评论：游标在漂、has_more 却一直是 true，
                # 继续翻只是在空转，直接收尾（视为翻完）。
                utils.logger.info(
                    f"[XiaoHongShuClient._fetch_sub_pages] root={root_comment_id} "
                    f"page={page} 整页均为重复评论，停止翻页")
                break
            if callback:
                await callback(note_id, fresh)
            await anti_risk.interval_sleep(crawl_interval, "分页/翻页间隔")
            result.extend(fresh)
        return result, None  # 正常翻完

    async def get_comments_all_sub_comments(
        self,
        comments: List[Dict],
        xsec_token: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        note_id: Optional[str] = None,
        start_sub_cursors: Optional[Dict[str, str]] = None,
        seen_sub_ids: Optional[set] = None,
    ) -> tuple:
        """
        Get all second-level comments under specified first-level comments.

        返回 (评论列表, open_sub)，其中 open_sub = {root_id: cursor} 记录「没翻完」
        的根评论楼中楼（接口报错/翻页触顶），供断点续抓在下一轮单独收尾。

        Args:
            comments: 主评论列表（每条约带 note_id / sub_comment_count /
                      sub_comments 预览 / sub_comment_has_more / sub_comment_cursor）
            xsec_token: Verification token
            crawl_interval: Crawl delay per comment (seconds)
            callback: 每批楼中楼入库回调
            note_id: 笔记 ID（续抓收尾时给没有主评论对象的根评论用）
            start_sub_cursors: 上一轮没翻完的根评论楼中楼游标（续抓收尾用）
            seen_sub_ids: 外部传入的「已抓子评论 ID」集合（两阶段抓取时由
                阶段一内嵌预览登记后共享进来，避免阶段二重复入库）
        """
        if not config.ENABLE_GET_SUB_COMMENTS:
            utils.logger.info(
                f"[XiaoHongShuClient.get_comments_all_sub_comments] Crawling sub_comment mode is not enabled"
            )
            return [], {}

        result: List[Dict] = []
        # 跨主评论共享：子评论 ID 全局唯一，同一子评论可能出现在内嵌预览和
        # 翻页结果里（也可能因分页重叠重复出现），必须去重后再入库。
        seen_sub_ids = seen_sub_ids if seen_sub_ids is not None else set()
        open_sub: Dict[str, str] = {}

        # 0) 续抓收尾：先把上一轮中断、没翻完的根评论楼中楼补齐。这些根的主评论
        #    已在上一轮入库，不会在下面的主评论循环里出现，所以必须单独处理。
        #    cursor 为空 = 该主楼还没翻过（阶段一被中断的遗留），从头翻即可。
        handled_roots: set = set()
        for root_id, cursor in list(dict(start_sub_cursors or {}).items()):
            if not root_id:
                continue
            subs, still = await self._fetch_sub_pages(
                note_id=note_id, root_comment_id=root_id,
                xsec_token=xsec_token, crawl_interval=crawl_interval,
                callback=callback, seen_sub_ids=seen_sub_ids,
                initial_has_more=True, initial_cursor=cursor or "",
            )
            handled_roots.add(root_id)
            result.extend(subs)
            if still:
                open_sub[root_id] = still

        # 1) 正常：本批主评论的楼中楼
        for comment in comments:
            try:
                cid = comment.get("note_id")
                root_comment_id = comment.get("id")
                if root_comment_id in handled_roots:
                    continue  # 阶段二收尾时已翻过，不重复请求
                sub_count = int(comment.get("sub_comment_count") or 0)
                inline_subs = comment.get("sub_comments") or []
                initial_has_more = bool(comment.get("sub_comment_has_more"))
                initial_cursor = comment.get("sub_comment_cursor") or ""
                subs, still = await self._fetch_sub_pages(
                    note_id=cid, root_comment_id=root_comment_id,
                    xsec_token=xsec_token, crawl_interval=crawl_interval,
                    callback=callback, seen_sub_ids=seen_sub_ids,
                    inline_subs=inline_subs, sub_count=sub_count,
                    initial_has_more=initial_has_more,
                    initial_cursor=initial_cursor,
                )
                result.extend(subs)
                if still:
                    open_sub[root_comment_id] = still
            except (CaptchaError, IPBlockError, PlatformAccessError):
                # 风控异常绝不能在这里被吞掉！曾经用裸 `except Exception` 兜底，
                # 461/471 被当成「单条评论处理失败」→ continue 到下一个根评论，
                # 零间隔再打一发，一秒钟能连撞好几发 461，而且整批不熔断
                # （2026-10-29：RUN 99 出现 45 条 461 却仍被判定 success，
                # 账号实际早已在风控态，后续两轮全被带崩）。
                # 这里的正确处理是往上抛，交给 core.get_comments 熔断整批。
                raise
            except Exception as e:
                utils.logger.error(
                    f"[XiaoHongShuClient.get_comments_all_sub_comments] Error processing comment: {comment.get('id', 'unknown')}, error: {e}. Continuing with next comment."
                )
                # 非风控失败也要退避：否则被限流时会以零间隔连打下一个根评论，
                # 进一步坐实风控特征。
                await anti_risk.xhs_sleep(f"root {root_comment_id} 楼中楼失败退避")
                continue  # Continue to next comment
        return result, open_sub

    async def get_creator_info(
        self, user_id: str, xsec_token: str = "", xsec_source: str = ""
    ) -> Dict:
        """
        Get user profile brief information by parsing user homepage HTML
        The PC user homepage has window.__INITIAL_STATE__ variable, just parse it

        Args:
            user_id: User ID
            xsec_token: Verification token (optional, pass if included in URL)
            xsec_source: Channel source (optional, pass if included in URL)

        Returns:
            Dict: Creator information
        """
        # Build URI, add xsec parameters to URL if available
        uri = f"/user/profile/{user_id}"
        if xsec_token and xsec_source:
            uri = f"{uri}?xsec_token={xsec_token}&xsec_source={xsec_source}"

        html_content = await self.request(
            "GET", self._domain + uri, return_response=True, headers=self.headers
        )
        return self._extractor.extract_creator_info_from_html(html_content)

    async def get_notes_by_creator(
        self,
        creator: str,
        cursor: str,
        page_size: int = 30,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> Dict:
        """
        Get creator's notes
        Args:
            creator: Creator ID
            cursor: Last note ID from previous page
            page_size: Page data length
            xsec_token: Verification token
            xsec_source: Channel source

        Returns:

        """
        uri = f"/api/sns/web/v1/user_posted"
        params = {
            "num": page_size,
            "cursor": cursor,
            "user_id": creator,
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
            "xsec_source": xsec_source,
        }
        return await self.get(uri, params)

    async def get_all_notes_by_creator(
        self,
        user_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
        known_note_ids: Optional[set] = None,
    ) -> List[Dict]:
        """
        Get all posts published by specified user, this method will continuously find all post information under a user
        Args:
            user_id: User ID
            crawl_interval: Crawl delay (seconds)
            callback: Update callback function after one pagination crawl ends
            xsec_token: Verification token
            xsec_source: Channel source
            known_note_ids: 增量模式已入库的 note_id 集合。
                传入后：整页笔记全部已存在时停止翻页（主页基本按时间倒序，
                翻到全旧的一页说明剩下的也都是旧的）；含新笔记的页只把
                新笔记交给 callback。不传 = 全量模式，行为与原来完全一致。

        Returns:

        """
        result = []
        notes_has_more = True
        notes_cursor = ""
        while notes_has_more and len(result) < config.CRAWLER_MAX_NOTES_COUNT:
            notes_res = await self.get_notes_by_creator(
                user_id, notes_cursor, xsec_token=xsec_token, xsec_source=xsec_source
            )
            if not notes_res:
                utils.logger.error(
                    f"[XiaoHongShuClient.get_notes_by_creator] The current creator may have been banned by xhs, so they cannot access the data."
                )
                break

            notes_has_more = notes_res.get("has_more", False)
            notes_cursor = notes_res.get("cursor", "")
            if "notes" not in notes_res:
                utils.logger.info(
                    f"[XiaoHongShuClient.get_all_notes_by_creator] No 'notes' key found in response: {notes_res}"
                )
                break

            notes = notes_res["notes"]
            utils.logger.info(
                f"[XiaoHongShuClient.get_all_notes_by_creator] got user_id:{user_id} notes len : {len(notes)}"
            )

            remaining = config.CRAWLER_MAX_NOTES_COUNT - len(result)
            if remaining <= 0:
                break

            if known_note_ids is not None:
                # 增量模式：本页有未知笔记才继续，且只处理未知的
                fresh = [n for n in notes
                         if str(n.get("note_id")) not in known_note_ids]
                if not fresh:
                    utils.logger.info(
                        f"[XiaoHongShuClient.get_all_notes_by_creator] 增量模式："
                        f"user_id:{user_id} 本页 {len(notes)} 条均已入库，停止翻页")
                    break
                notes_to_add = fresh[:remaining]
            else:
                notes_to_add = notes[:remaining]
            if callback:
                await callback(notes_to_add)

            result.extend(notes_to_add)
            await anti_risk.interval_sleep(crawl_interval, "分页/翻页间隔")

        utils.logger.info(
            f"[XiaoHongShuClient.get_all_notes_by_creator] Finished getting notes for user {user_id}, total: {len(result)}"
        )
        return result

    async def get_self_user_id(self) -> str:
        """取当前登录用户自己的 user_id。

        优先级（2026-10-28 修正）：
        1. /api/sns/web/v2/user/me（以及 v1）：顶层直接给 user_id，是唯一权威来源。
           —— 此前漏了这一步，直接落到 mentions 兜底，拿到的其实是「提到我的那条
           笔记的作者」，很可能是别人（实测本号真实 ID 6689eeca…，兜底拿到
           64494df1…），于是点赞/收藏列表永远为空。
        2. selfinfo 的 data.user_id / data.result.data.user_id（多数只有 red_id）
        3. 互动列表 you/mentions 里的 user_info.userid（最不可靠，仅兜底）
        取不到返回空串（调用方据此报错，避免拿空 ID 去打点赞接口）。
        """
        # 1) user/me —— 本人身份的唯一权威来源
        for uri in ("/api/sns/web/v2/user/me", "/api/sns/web/v1/user/me"):
            try:
                res = await self.get(uri, {})
            except (CaptchaError, IPBlockError, PlatformAccessError):
                # 已经在风控态下就别再拿后面的接口试一遍了：
                # 身份检查阶段撞验证码必须立刻往上抛，让 core 收手
                raise
            except Exception as e:
                utils.logger.warning(
                    f"[XiaoHongShuClient.get_self_user_id] {uri} 失败: {e}")
                continue
            uid = (res or {}).get("user_id")
            if not uid:
                uid = ((res or {}).get("data") or {}).get("user_id")
            if uid:
                return str(uid)

        info = await self.query_self()
        if isinstance(info, dict):
            data = info.get("data") or {}
            for path in (
                lambda: data.get("user_id"),
                lambda: (data.get("result") or {}).get("data", {}).get("user_id"),
                lambda: (data.get("data") or {}).get("user_id"),
                lambda: (data.get("result") or {}).get("user_id"),
            ):
                try:
                    uid = path()
                except Exception:
                    uid = None
                if uid:
                    return str(uid)
        # 兜底：互动列表里本人笔记的 user_info.userid
        # 注意：这条路径拿到的是「提到我的笔记作者」，可能不是本人，
        # 只能在没有其它办法时用；正常情况上面的 user/me 已经返回了。
        try:
            res = await self.get("/api/sns/web/v1/you/mentions",
                                 {"num": 5, "cursor": ""})
            for item in (res or {}).get("message_list") or []:
                ii = item.get("item_info") or {}
                ui = ii.get("user_info") or {}
                uid = ui.get("userid") or ui.get("user_id")
                if uid:
                    utils.logger.warning(
                        f"[XiaoHongShuClient.get_self_user_id] 回退到互动列表推断"
                        f"本人 ID={uid}（可能与真实不一致，仅兜底）")
                    return str(uid)
        except Exception as e:
            utils.logger.warning(
                f"[XiaoHongShuClient.get_self_user_id] 互动列表兜底失败: {e}")
        return ""

    async def get_user_liked_notes(
        self,
        user_id: str,
        cursor: str = "",
        page_size: int = 30,
    ) -> Dict:
        """分页拉取「我的点赞」列表（登录用户点过赞的笔记）。

        接口：GET /api/sns/web/v1/note/like/page
        参数与网页端请求保持一致（num/cursor/user_id/image_formats 以及空的
        xsec_token/xsec_source —— 浏览器即使没有 token 也会带上这两个键）。
        注意：服务端固定 10 条/页，num=30 也不会多给，必须靠 cursor 翻页。
        """
        uri = "/api/sns/web/v1/note/like/page"
        params = {
            "num": page_size,
            "cursor": cursor,
            "user_id": user_id,
            "image_formats": "jpg,webp,avif",
            "xsec_token": "",
            "xsec_source": "",
        }
        return await self.get(uri, params)

    async def get_user_collected_notes(
        self,
        user_id: str,
        cursor: str = "",
        page_size: int = 30,
    ) -> Dict:
        """分页拉取「我的收藏」列表（登录用户收藏的笔记）。

        接口：GET /api/sns/web/v2/note/collect/page
        """
        uri = "/api/sns/web/v2/note/collect/page"
        params = {
            "num": page_size,
            "cursor": cursor,
            "user_id": user_id,
            "image_formats": "jpg,webp,avif",
            "xsec_token": "",
            "xsec_source": "",
        }
        return await self.get(uri, params)

    @staticmethod
    def _extract_behavior_notes(res: Dict) -> tuple:
        """从点赞/收藏接口响应里取 (notes, has_more, cursor)。

        兼容两种包裹：data.notes / data.items，has_more/cursor 缺省为 False/""。
        """
        notes = res.get("notes") if isinstance(res, dict) else None
        if not notes and isinstance(res, dict):
            notes = res.get("items")
        notes = notes or []
        has_more = bool(res.get("has_more", False)) if isinstance(res, dict) else False
        cursor = (res.get("cursor") or "") if isinstance(res, dict) else ""
        return notes, has_more, cursor

    async def get_all_behavior_notes(
        self,
        user_id: str,
        behaviors: Optional[List[str]] = None,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_notes: Optional[int] = None,
    ) -> List[Dict]:
        """拉取登录用户「点赞 + 收藏」的笔记列表（去重后按出现顺序返回）。

        behaviors: 子集 ["likes", "collects"]，默认两个都拉。
        callback: 每拉到一批（去重后）笔记后回调。签名为
            `callback(batch)` 或 `callback(batch, label)`；返回一个值表示是否
            继续翻页：
              - 返回 False 表示「够了，别再翻页了」（调用方按自己的额度判断，
                比如增量模式下已经凑够本批要抓的新帖）；
              - 返回 dict `{"stop": bool, "queued": int}` 时，stop 同 False 语义，
                queued 为本页真正排进抓取队列（详情+评论）的帖子数——为 0 时
                列表翻页不再随机休眠（没有评论抓取就不需要节流）。
        max_notes: 每个行为的硬上限（条，仅防跑飞），默认 10000 ≈ 翻到底。

        重要（2026-10-28 修两个 bug）：
        1) 上限必须按「每个行为」独立计数，且不能用 CRAWLER_MAX_NOTES_COUNT
           （默认 15，本意是单博主抓几条）。旧实现用全局 len(result) 判断，
           点赞一旦超过 15 条，收藏的 while 条件直接为假 → 收藏一条不抓；
           点赞本身也在 20 条（2 页）时被截断（实测本号点赞 495 条）。
        2) 「跳过已归档的帖子」不能靠调小 max_notes 实现：那样翻页会卡在
           最前面几页、永远翻不到后面的新内容。这里只负责把列表翻完，
           是否需要继续由 callback 的返回值决定。
        """
        behaviors = behaviors or ["likes", "collects"]
        if max_notes is None:
            max_notes = 10000
        max_notes = max(1, max_notes)

        seen: set = set()
        result: List[Dict] = []
        fetchers = []
        if "likes" in behaviors:
            fetchers.append(("点赞", "likes", self.get_user_liked_notes))
        if "collects" in behaviors:
            fetchers.append(("收藏", "collects", self.get_user_collected_notes))

        for label, behavior_key, fetcher in fetchers:
            cursor = ""
            has_more = True
            pages = 0
            got_this = 0          # 本行为已收条数（只算去重后的新笔记）
            raw_this = 0          # 本行为接口返回条数（含跨行为重复）
            stopped_by_caller = False
            while has_more and got_this < max_notes:
                prev_cursor = cursor
                res = await fetcher(user_id, cursor)
                if not res:
                    utils.logger.error(
                        f"[XiaoHongShuClient.get_all_behavior_notes] {label}列表拉取失败"
                        f"（user_id={user_id}），可能登录态失效或无权限")
                    break
                notes, has_more, cursor = self._extract_behavior_notes(res)
                if not notes:
                    utils.logger.info(
                        f"[XiaoHongShuClient.get_all_behavior_notes] {label}列表本页为空，停止翻页")
                    break
                pages += 1
                raw_this += len(notes)
                # 本页排进「详情+评论」抓取的目标数，必须每页重置：
                # 若只在 `if fresh:` 里初始化，某页去重后为空（点赞/收藏
                # 交叉重复）时会沿用上一页的值，导致该睡不睡 / 不该睡却睡。
                # 默认 0（本页没新帖 = 没有任何抓取目标）；
                # None = 旧式回调（排队数未知，保守按需要休眠）。
                queued_this = 0
                fresh = []
                for n in notes:
                    nid = str(n.get("note_id") or n.get("id") or "")
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    fresh.append(n)
                utils.logger.info(
                    f"[XiaoHongShuClient.get_all_behavior_notes] {label}第 {pages} 页："
                    f"{len(notes)} 条，去重后新增 {len(fresh)} 条（累计扫描 {raw_this} 条）")
                if fresh:
                    stop = False
                    if callback:
                        try:
                            ret = await callback(fresh, behavior_key)
                        except TypeError:
                            # 兼容只接受一个参数的旧回调
                            ret = await callback(fresh)
                        if isinstance(ret, dict):
                            # 新契约：{"stop": bool, "queued": int}
                            # queued 为 0 说明本页全是已跳过（视频/已归档）的帖，
                            # 没有评论抓取目标，没必要随机休眠。
                            stop = bool(ret.get("stop", False))
                            queued_this = int(ret.get("queued", 0))
                        else:
                            # 旧式回调只回 bool，排队数未知 → 保守（照旧休眠）
                            stop = (ret is False)
                            queued_this = None
                    result.extend(fresh)
                    got_this += len(fresh)
                    if stop:
                        stopped_by_caller = True
                        utils.logger.info(
                            f"[XiaoHongShuClient.get_all_behavior_notes] {label}"
                            f"调用方已凑够本批要抓的帖子，停止翻页"
                            f"（已扫 {pages} 页 / {raw_this} 条）")
                        break
                if not cursor and has_more:
                    break  # 没有游标却声称还有更多，防死循环
                if cursor and cursor == prev_cursor:
                    utils.logger.warning(
                        f"[XiaoHongShuClient.get_all_behavior_notes] {label}游标未前进"
                        f"（{cursor}），停止翻页防死循环")
                    break
                # 随机休眠只在本页确实有「要抓详情+评论」的帖子时才做：
                # 增量模式下绝大部分页是已归档帖（queued=0），白睡毫无意义——
                # 既不会触发重的评论请求，也不需要为真人节奏铺垫。
                # 另外「末页不休眠」：本页回来时已经 has_more=False（或额度已满、
                # 不会再翻），循环下一轮必然退出，这时再睡 5-15s 纯属白等
                # （实测第 50 页回来后又睡了 12.81s 才收工）。
                more_pages = has_more and got_this < max_notes
                if more_pages and (queued_this is None or queued_this > 0):
                    reason = f"{label}列表翻页间隔"
                    if queued_this is not None:
                        reason += f"（本页待抓 {queued_this} 篇）"
                    await anti_risk.interval_sleep(crawl_interval, reason)

            utils.logger.info(
                f"[XiaoHongShuClient.get_all_behavior_notes] {label}抓取结束："
                f"{pages} 页 / 接口共 {raw_this} 条 / 去重后 {got_this} 条"
                f"（has_more={has_more}"
                f"{'，调用方喊停' if stopped_by_caller else ''}）")

        utils.logger.info(
            f"[XiaoHongShuClient.get_all_behavior_notes] 用户 {user_id} 点赞+收藏"
            f"共 {len(result)} 条笔记（去重后）")
        return result

    async def get_note_short_url(self, note_id: str) -> Dict:
        """
        Get note short URL
        Args:
            note_id: Note ID

        Returns:
        """
        uri = f"/api/sns/web/short_url"
        data = {"original_url": f"{self._domain}/discovery/item/{note_id}"}
        return await self.post(uri, data=data, return_response=True)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_not_exception_type(
            (RetryError, IPBlockError, PlatformAccessError)
        ),
    )
    async def get_note_by_id_from_html(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        enable_cookie: bool = False,
    ) -> Optional[Dict]:
        """
        Get note details by parsing note detail page HTML, this interface may fail, retry 3 times here
        copy from https://github.com/ReaJason/xhs/blob/eb1c5a0213f6fbb592f0a2897ee552847c69ea2d/xhs/core.py#L217-L259
        thanks for ReaJason
        Args:
            note_id:
            xsec_source:
            xsec_token:
            enable_cookie:

        Returns:

        """
        url = (
            f"{self._domain}/explore/"
            + note_id
            + f"?xsec_token={xsec_token}&xsec_source={xsec_source}"
        )
        copy_headers = self.headers.copy()
        if not enable_cookie:
            del copy_headers["Cookie"]

        html = await self.request(
            method="GET", url=url, return_response=True, headers=copy_headers
        )

        return self._extractor.extract_note_detail_from_html(note_id, html)

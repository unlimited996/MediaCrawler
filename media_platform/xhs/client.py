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
    DataFetchError,
    IPBlockError,
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
        retry=retry_if_not_exception_type(
            (NoteNotFoundError, IPBlockError, PlatformAccessError)
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
            # someday someone maybe will bypass captcha
            verify_type = response.headers["Verifytype"]
            verify_uuid = response.headers["Verifyuuid"]
            msg = f"CAPTCHA appeared, request failed, Verifytype: {verify_type}, Verifyuuid: {verify_uuid}, Response: {response}"
            utils.logger.error(msg)
            raise Exception(msg)

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
        elif data["code"] in (self.NOTE_NOT_FOUND_CODE, self.NOTE_ABNORMAL_CODE):
            raise NoteNotFoundError(f"Note not found or abnormal, code: {data['code']}")
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
    ) -> List[Dict]:
        """
        Get all first-level comments under specified note, this method will continuously find all comment information under a post
        Args:
            note_id: Note ID
            xsec_token: Verification token
            crawl_interval: Crawl delay per note (seconds)
            callback: Callback after one note crawl ends
            max_count: Maximum number of comments to crawl per note
        Returns:

        """
        result = []
        seen_comment_ids: set = set()
        comments_has_more = True
        comments_cursor = ""
        while comments_has_more and len(result) < max_count:
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
            if len(result) + len(comments) > max_count:
                comments = comments[: max_count - len(result)]
            if callback:
                await callback(note_id, comments)
            await anti_risk.interval_sleep(crawl_interval, "分页/翻页间隔")
            result.extend(comments)
            sub_comments = await self.get_comments_all_sub_comments(
                comments=comments,
                xsec_token=xsec_token,
                crawl_interval=crawl_interval,
                callback=callback,
            )
            result.extend(sub_comments)
        return result

    async def get_comments_all_sub_comments(
        self,
        comments: List[Dict],
        xsec_token: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Get all second-level comments under specified first-level comments, this method will continuously find all second-level comment information under first-level comments
        Args:
            comments: Comment list
            xsec_token: Verification token
            crawl_interval: Crawl delay per comment (seconds)
            callback: Callback after one comment crawl ends

        Returns:

        """
        if not config.ENABLE_GET_SUB_COMMENTS:
            utils.logger.info(
                f"[XiaoHongShuCrawler.get_comments_all_sub_comments] Crawling sub_comment mode is not enabled"
            )
            return []

        result = []
        # 跨主评论共享：子评论 ID 全局唯一，同一子评论可能出现在内嵌预览和
        # 翻页结果里（也可能因分页重叠重复出现），必须去重后再入库。
        seen_sub_ids: set = set()
        for comment in comments:
            try:
                note_id = comment.get("note_id")
                root_comment_id = comment.get("id")
                sub_count = int(comment.get("sub_comment_count") or 0)
                inline_subs = comment.get("sub_comments") or []

                # 1) 主评论响应里内嵌的子评论预览
                if inline_subs:
                    fresh = [c for c in inline_subs if c.get("id") not in seen_sub_ids]
                    seen_sub_ids.update(c.get("id") for c in fresh if c.get("id"))
                    if fresh and callback:
                        await callback(note_id, fresh)

                # 2) 判断是否还有更多子评论。
                #    不能只信 sub_comment_has_more 标志——接口改字段名时会静默漏采；
                #    用「预览数 < 声明总数」做兜底，保证楼中楼抓全。
                has_more = bool(comment.get("sub_comment_has_more"))
                if not has_more and sub_count and len(inline_subs) < sub_count:
                    has_more = True
                if not has_more:
                    continue

                cursor = comment.get("sub_comment_cursor") or ""
                page = 0
                while has_more:
                    page += 1
                    if page > MAX_SUB_COMMENT_PAGES:
                        utils.logger.warning(
                            f"[XiaoHongShuClient.get_comments_all_sub_comments] "
                            f"root_comment_id: {root_comment_id} 翻页超过 {MAX_SUB_COMMENT_PAGES} 页，停止")
                        break
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
                            f"[XiaoHongShuClient.get_comments_all_sub_comments] Failed to get sub-comments for note_id: {note_id}, root_comment_id: {root_comment_id}, error: {e}. Skipping this comment's sub-comments."
                        )
                        break

                    if not isinstance(comments_res, dict):
                        utils.logger.info(
                            f"[XiaoHongShuClient.get_comments_all_sub_comments] No response found for note_id: {note_id}"
                        )
                        break

                    page_comments = comments_res.get("comments") or []
                    # 诊断日志：记录接口真实返回形态。曾出现「每页只回 1 条、
                    # has_more 立即变 false」导致楼中楼严重漏采，靠这行定位。
                    utils.logger.info(
                        f"[XiaoHongShuClient.get_comments_all_sub_comments] "
                        f"root={root_comment_id} page={page} "
                        f"keys={sorted(comments_res.keys())} "
                        f"has_more={comments_res.get('has_more')} "
                        f"cursor={str(comments_res.get('cursor'))[:48]!r} "
                        f"got={len(page_comments)} declared={sub_count}")

                    has_more = bool(comments_res.get("has_more")
                                    or comments_res.get("sub_comment_has_more"))
                    new_cursor = comments_res.get("cursor") or ""
                    if not page_comments:
                        break
                    if new_cursor and new_cursor == cursor:
                        # 游标没动：再翻也是同一页，退出防死循环
                        break
                    cursor = new_cursor

                    fresh = [c for c in page_comments if c.get("id") not in seen_sub_ids]
                    seen_sub_ids.update(c.get("id") for c in fresh if c.get("id"))
                    if fresh and callback:
                        await callback(note_id, fresh)
                    await anti_risk.interval_sleep(crawl_interval, "分页/翻页间隔")
                    result.extend(fresh)
            except Exception as e:
                utils.logger.error(
                    f"[XiaoHongShuClient.get_comments_all_sub_comments] Error processing comment: {comment.get('id', 'unknown')}, error: {e}. Continuing with next comment."
                )
                continue  # Continue to next comment
        return result

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

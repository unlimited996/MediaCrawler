# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/schemas/crawler.py
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

from enum import Enum
from typing import Optional, Literal, List
from pydantic import BaseModel, Field, field_validator


MAX_API_LIMIT_COUNT = 10000


class PlatformEnum(str, Enum):
    """Supported media platforms"""
    XHS = "xhs"
    DOUYIN = "dy"
    KUAISHOU = "ks"
    BILIBILI = "bili"
    WEIBO = "wb"
    TIEBA = "tieba"
    ZHIHU = "zhihu"


class LoginTypeEnum(str, Enum):
    """Login method"""
    QRCODE = "qrcode"
    PHONE = "phone"
    COOKIE = "cookie"


class CrawlerTypeEnum(str, Enum):
    """Crawler type"""
    SEARCH = "search"
    DETAIL = "detail"
    CREATOR = "creator"


class SaveDataOptionEnum(str, Enum):
    """Data save option

    只保留本项目真正实现的存储方式：文件类（csv/json/jsonl/excel）、
    sqlite、MySQL(db)、postgres。mongodb 无 store 实现，已移除——
    原先它在枚举里，选中后运行时会以难以定位的方式失败。
    """
    CSV = "csv"
    DB = "db"
    JSON = "json"
    JSONL = "jsonl"
    SQLITE = "sqlite"
    EXCEL = "excel"
    POSTGRES = "postgres"


class CrawlerStartRequest(BaseModel):
    """Crawler start request"""
    platform: PlatformEnum
    login_type: LoginTypeEnum = LoginTypeEnum.QRCODE
    crawler_type: CrawlerTypeEnum = CrawlerTypeEnum.SEARCH
    keywords: str = ""  # Keywords for search mode
    specified_ids: str = ""  # Post/video ID list for detail mode, comma-separated
    creator_ids: str = ""  # Creator ID list for creator mode, comma-separated
    start_page: int = 1
    enable_comments: bool = True
    enable_sub_comments: bool = False
    enable_media: bool = False
    save_option: SaveDataOptionEnum = SaveDataOptionEnum.JSONL
    cookies: str = ""
    headless: bool = False
    max_notes_count: Optional[int] = Field(default=None, ge=1, le=MAX_API_LIMIT_COUNT)
    max_comments_count: Optional[int] = Field(default=None, ge=1, le=MAX_API_LIMIT_COUNT)


QueueStatus = Literal["pending", "running", "done", "failed", "cancelled"]


class QueueAddRequest(BaseModel):
    """往抓取队列里追加目标。

    urls 每项是一条笔记/视频链接或 ID（详情模式）、一个关键词（搜索模式）、
    一个创作者 ID（创作者模式）。platform / crawler_type 与抓取选项对整批生效。
    """
    urls: List[str] = Field(default_factory=list)
    platform: PlatformEnum
    crawler_type: CrawlerTypeEnum = CrawlerTypeEnum.DETAIL
    login_type: LoginTypeEnum = LoginTypeEnum.QRCODE
    start_page: int = 1
    enable_comments: bool = True
    enable_sub_comments: bool = False
    enable_media: bool = False
    save_option: SaveDataOptionEnum = SaveDataOptionEnum.JSONL
    cookies: str = ""
    headless: bool = True
    max_notes_count: Optional[int] = Field(default=None, ge=1, le=MAX_API_LIMIT_COUNT)
    max_comments_count: Optional[int] = Field(default=None, ge=1, le=MAX_API_LIMIT_COUNT)

    @field_validator("urls", mode="before")
    @classmethod
    def _split_urls(cls, v):
        """允许直接贴一整段文本：按换行 / 逗号切分。

        不按空格切——关键词"Vibe Coding"这种带空格的必须保持完整。
        """
        if isinstance(v, str):
            import re as _re
            return [s.strip() for s in _re.split(r"[\n,]+", v) if s.strip()]
        return v


class QueueItem(BaseModel):
    """抓取队列里的一条任务"""
    id: int
    url: str
    platform: str
    crawler_type: str
    status: QueueStatus
    added_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    batch_id: Optional[int] = None


class QueueListResponse(BaseModel):
    """队列列表 + 概览"""
    items: List[QueueItem] = Field(default_factory=list)
    pending: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0
    auto_run: bool = True
    batch_size: int = 5


class CrawlerStatusResponse(BaseModel):
    """Crawler status response"""
    status: Literal["idle", "running", "stopping", "error"]
    platform: Optional[str] = None
    crawler_type: Optional[str] = None
    started_at: Optional[str] = None
    error_message: Optional[str] = None
    queue_pending: int = 0
    queue_running: int = 0


class LogEntry(BaseModel):
    """Log entry"""
    id: int
    timestamp: str
    level: Literal["info", "warning", "error", "success", "debug"]
    message: str


class DataFileInfo(BaseModel):
    """Data file information"""
    name: str
    path: str
    size: int
    modified_at: str
    record_count: Optional[int] = None

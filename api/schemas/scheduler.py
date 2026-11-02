# -*- coding: utf-8 -*-
"""定时拉取任务的 API 数据模型。"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .crawler import LoginTypeEnum, PlatformEnum, SaveDataOptionEnum


MIN_INTERVAL_SEC = 60          # 1 分钟
MAX_INTERVAL_SEC = 2592000     # 30 天

#: 任务维度：profile=关注的用户 / note=关注的帖子 / account=扫码登录用户（点赞·收藏图文帖）
TaskType = Literal["profile", "note", "account"]
#: 账号维度可抓取的行为来源（网页端无「我评论过的笔记」列表接口，评论暂不可用）
BehaviorLiteral = Literal["likes", "collects"]


class TaskOptions(BaseModel):
    """任务的抓取选项（与手动抓取配置保持一致）。

    ⚠️ 这是**入参模型的第二个白名单**：pydantic 默认 `extra="ignore"`，
    模型里没声明的字段会被**静默丢弃**，根本到不了 `_normalize_options`。
    2026-10-29 就因此连续踩了两次：
      - `max_notes`（本次抓取上限）丢弃 → 用户填了 100 仍按默认跑，
        「设了上限却只更新 20 篇」的真因（当时只改了 _normalize_options，
        其实请求在 schema 层就被吃掉了，改动等于没生效）。
    现在显式声明 + `extra="allow"` 兜底，避免以后再静默吞掉新选项。

    注：原 `ignore_risk_cooldown`（风控冷却豁免）已于 2026-11-02 随冷却闸口
    一并移除——平台限流改由单轮请求预算护栏在撞 461 之前主动停手，
    不再需要「强制绕过冷却」的开关。
    """
    model_config = ConfigDict(extra="allow")

    login_type: LoginTypeEnum = LoginTypeEnum.COOKIE
    enable_comments: bool = True
    enable_sub_comments: bool = False
    enable_media: bool = False
    save_option: SaveDataOptionEnum = SaveDataOptionEnum.POSTGRES
    headless: bool = True
    cookies: str = ""
    max_comments_count: Optional[int] = Field(default=None, ge=1, le=10000)
    #: 账号维度专属：抓取哪些行为来源的帖子（likes=点赞 / collects=收藏）
    behaviors: List[BehaviorLiteral] = Field(default_factory=lambda: ["likes", "collects"])
    #: 账号维度专属：账号池账号 ID（人读名一并存，便于列表展示）
    account_id: Optional[int] = None
    account_name: Optional[str] = None
    #: 本次每个行为最多抓多少条新帖（None=用爬虫默认，配置项 xhs_behavior_max_notes）
    max_notes: Optional[int] = Field(default=None, ge=1, le=10000)
    #: 抓取模式（顶层 mode 列才是主来源，这里仅兼容存进 options 的情况）
    mode: Optional[str] = None


class ScheduledTaskCreate(BaseModel):
    """创建定时任务。"""
    name: str = Field(min_length=1, max_length=128)
    platform: PlatformEnum
    task_type: TaskType
    target: str = Field(min_length=1)
    mode: Literal["full", "incremental"] = "incremental"
    schedule_type: Literal["interval", "cron"] = "interval"
    interval_sec: Optional[int] = Field(default=3600, ge=MIN_INTERVAL_SEC,
                                        le=MAX_INTERVAL_SEC)
    cron_expr: Optional[str] = None
    max_concurrency: int = Field(default=1, ge=1, le=10)
    timeout_sec: int = Field(default=1800, ge=60, le=86400)
    retry_count: int = Field(default=1, ge=0, le=5)
    retry_delay_sec: int = Field(default=60, ge=0, le=3600)
    rate_interval_sec: int = Field(default=3, ge=0, le=60)
    options: TaskOptions = Field(default_factory=TaskOptions)
    enabled: bool = True

    @field_validator("target")
    @classmethod
    def _strip_target(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("目标地址不能为空")
        if len(v) > 2000:
            raise ValueError("目标地址过长（>2000 字符）")
        return v

    @field_validator("cron_expr")
    @classmethod
    def _validate_cron(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        # 复用调度器的解析器，非法表达式在入口就拦掉
        from ..services.task_scheduler import parse_cron
        try:
            parse_cron(v)
        except Exception as e:
            raise ValueError(f"cron 表达式无效：{e}")
        return v

    def model_post_init(self, __context) -> None:
        # 调度方式与参数的交叉校验（pydantic v2 的 model_validator 替代写法）
        if self.schedule_type == "interval" and not self.interval_sec:
            raise ValueError("interval 调度必须提供 interval_sec")
        if self.schedule_type == "cron" and not self.cron_expr:
            raise ValueError("cron 调度必须提供 cron_expr")


class ScheduledTaskUpdate(BaseModel):
    """更新定时任务（只传要改的字段）。"""
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    target: Optional[str] = None
    mode: Optional[Literal["full", "incremental"]] = None
    schedule_type: Optional[Literal["interval", "cron"]] = None
    interval_sec: Optional[int] = Field(default=None, ge=MIN_INTERVAL_SEC,
                                        le=MAX_INTERVAL_SEC)
    cron_expr: Optional[str] = None
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=10)
    timeout_sec: Optional[int] = Field(default=None, ge=60, le=86400)
    retry_count: Optional[int] = Field(default=None, ge=0, le=5)
    retry_delay_sec: Optional[int] = Field(default=None, ge=0, le=3600)
    rate_interval_sec: Optional[int] = Field(default=None, ge=0, le=60)
    options: Optional[TaskOptions] = None
    enabled: Optional[bool] = None

    @field_validator("cron_expr")
    @classmethod
    def _validate_cron(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        from ..services.task_scheduler import parse_cron
        try:
            parse_cron(v.strip())
        except Exception as e:
            raise ValueError(f"cron 表达式无效：{e}")
        return v.strip()


class ScheduledTaskOut(BaseModel):
    id: int
    task_code: str
    name: str
    platform: str
    task_type: str
    #: 维度（由 task_type 派生）：author=关注的用户 / note=关注的帖子 / account=扫码登录用户
    dimension: str = "note"
    target: str
    mode: str
    schedule_type: str
    interval_sec: Optional[int] = None
    cron_expr: Optional[str] = None
    max_concurrency: int = 1
    timeout_sec: int = 1800
    retry_count: int = 1
    retry_delay_sec: int = 60
    rate_interval_sec: int = 3
    options: dict = Field(default_factory=dict)
    enabled: bool = True
    status: str = "idle"
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    next_run_at: Optional[str] = None
    run_count: int = 0
    created_at: Optional[str] = None


class ScheduledTaskRunOut(BaseModel):
    id: int
    task_id: int
    trigger_type: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    new_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    attempt_count: int = 1
    error_message: Optional[str] = None
    log_tail: list = Field(default_factory=list)
    # 细粒度统计：{added:[...], updated:[...]}，含前后评论完整率。
    # 历史运行记录没有该字段，前端渲染前做空值兜底。
    stats_json: Optional[dict] = None


class SchedulerStatusOut(BaseModel):
    started: bool = False
    enabled: bool = True
    max_concurrency: int = 1
    tick_sec: int = 15
    total_tasks: int = 0
    enabled_tasks: int = 0
    running_tasks: int = 0
    last_tick_at: Optional[str] = None
    last_error: str = ""

# -*- coding: utf-8 -*-
"""运行时配置：优先读库表 crawl_config_item，库不可用则回退到 base_config 常量。

设计要点：
1. 进程启动时 load() 一次，之后同步读内存缓存，抓取热路径零 IO
2. immediate 类配置更新后调用 refresh() 立即生效；next_task 类在任务启动时快照
3. 库表缺失任何一项都回退到旧常量 —— 保证现有功能不受影响

用法：
    from config.runtime_config import runtime_config
    await runtime_config.load()                       # 启动时
    runtime_config.get_bool("enable_get_sub_comments")
    runtime_config.get_int("crawler_max_notes_count")
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Dict, Optional

# cfg_key -> base_config 中的旧常量名（回退用）
LEGACY_FALLBACK: Dict[str, str] = {
    "platform": "PLATFORM",
    "xhs_international": "XHS_INTERNATIONAL",
    "keywords": "KEYWORDS",
    "login_type": "LOGIN_TYPE",
    "cookies": "COOKIES",
    "crawler_type": "CRAWLER_TYPE",
    "start_page": "START_PAGE",
    "crawler_max_notes_count": "CRAWLER_MAX_NOTES_COUNT",
    "max_concurrency_num": "MAX_CONCURRENCY_NUM",
    "enable_get_comments": "ENABLE_GET_COMMENTS",
    "crawler_max_comments_count_single_notes": "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES",
    "enable_get_sub_comments": "ENABLE_GET_SUB_COMMENTS",
    "crawler_max_sleep_sec": "CRAWLER_MAX_SLEEP_SEC",
    "enable_ip_proxy": "ENABLE_IP_PROXY",
    "ip_proxy_provider_name": "IP_PROXY_PROVIDER_NAME",
    "ip_proxy_pool_count": "IP_PROXY_POOL_COUNT",
    "static_proxy_url": "STATIC_PROXY_URL",
    "headless": "HEADLESS",
    "save_login_state": "SAVE_LOGIN_STATE",
    "enable_cdp_mode": "ENABLE_CDP_MODE",
    "cdp_connect_existing": "CDP_CONNECT_EXISTING",
    "cdp_debug_port": "CDP_DEBUG_PORT",
    "cdp_headless": "CDP_HEADLESS",
    "auto_close_browser": "AUTO_CLOSE_BROWSER",
    "custom_browser_path": "CUSTOM_BROWSER_PATH",
    "browser_launch_timeout": "BROWSER_LAUNCH_TIMEOUT",
    "save_data_option": "SAVE_DATA_OPTION",
    "save_data_path": "SAVE_DATA_PATH",
    "enable_get_media": "ENABLE_GET_MEDIA",
    "enable_get_wordcloud": "ENABLE_GET_WORDCLOUD",
    "custom_words": "CUSTOM_WORDS",
    "stop_words_file": "STOP_WORDS_FILE",
    "font_path": "FONT_PATH",
    "user_data_dir": "USER_DATA_DIR",
    "disable_ssl_verify": "DISABLE_SSL_VERIFY",
    # 小红书账号备份参数（2026-10-29 纳管，原仅在 config/xhs_config.py 以常量存在）
    "xhs_behavior_max_notes": "XHS_BEHAVIOR_MAX_NOTES",
    "xhs_behavior_incremental": "XHS_BEHAVIOR_INCREMENTAL",
    "xhs_comment_max_per_note": "XHS_COMMENT_MAX_PER_NOTE",
    "xhs_comment_count_margin": "XHS_COMMENT_COUNT_MARGIN",
    # 抓取节奏 / 验证码等待（2026-11-04 纳管：慢速抓全，避免累计请求量撞 461）
    "xhs_sleep_min_sec": "XHS_SLEEP_MIN_SEC",
    "xhs_sleep_max_sec": "XHS_SLEEP_MAX_SEC",
    "xhs_captcha_wait_sec": "XHS_CAPTCHA_WAIT_SEC",
    "xhs_interleave_every_pages": "XHS_INTERLEAVE_EVERY_PAGES",
    "xhs_461_interleave_retries": "XHS_461_INTERLEAVE_RETRIES",
    # 单轮请求预算护栏（2026-11-02 纳管，原仅在 config/xhs_config.py 以常量存在）
    "xhs_detail_budget_per_round": "XHS_DETAIL_BUDGET_PER_ROUND",
    "xhs_comment_budget_per_round": "XHS_COMMENT_BUDGET_PER_ROUND",
    # 进程级节奏参数（2026-11-06 纳管：改这些不用重建镜像，详见 SYNC_TO_CONFIG）
    "comment_completeness_target": "COMMENT_COMPLETENESS_TARGET",
    "page_goto_timeout_sec": "PAGE_GOTO_TIMEOUT_SEC",
    "scheduler_max_concurrency": "SCHEDULER_MAX_CONCURRENCY",
    "scheduler_tick_sec": "SCHEDULER_TICK_SEC",
    "scheduler_busy_wait_sec": "SCHEDULER_BUSY_WAIT_SEC",
    "scheduler_run_history": "SCHEDULER_RUN_HISTORY",
    "scheduler_active_hour_start": "SCHEDULER_ACTIVE_HOUR_START",
    "scheduler_active_hour_end": "SCHEDULER_ACTIVE_HOUR_END",
    "scheduler_jitter_min_sec": "SCHEDULER_JITTER_MIN_SEC",
    "scheduler_jitter_max_sec": "SCHEDULER_JITTER_MAX_SEC",
    "scheduler_window_spread_sec": "SCHEDULER_WINDOW_SPREAD_SEC",
    "risk_cooldown_hours": "RISK_COOLDOWN_HOURS",
    "s3_backup_purge_with_story": "S3_BACKUP_PURGE_WITH_STORY",
    "xhs_max_concurrency_num": "XHS_MAX_CONCURRENCY_NUM",
    "xhs_behavior_early_stop_pages": "XHS_BEHAVIOR_EARLY_STOP_PAGES",
    "xhs_sub_comment_max_pages": "XHS_SUB_COMMENT_MAX_PAGES",
}

#: 这些配置项在库里改完后要**同步回 config 模块属性**，让长驻进程
#: （worker / 调度器 / API）不必重启就能读到新值 —— 这样调参不需要重建
#: 镜像、也不需要重启容器。
#:
#: 为什么需要：这些常量原本是 `config/base_config.py` 的模块级变量，
#: 消费方写的是 `config.SCHEDULER_TICK_SEC` 这种属性访问。库里改值后
#: 只更新 RuntimeConfig 缓存并不会影响那些模块属性。这里在每次 load 后
#: 回写，属性访问就能拿到新值。
#:
#: 注意：`from config import XXX` 这种**早先绑定**的名字不会跟着变，
#: 所以抓取子进程仍走 build_crawler_env 注入环境变量（每次任务启动刷新）。
#: 键 -> (子模块名, 模块内属性名)
SYNC_TO_CONFIG: Dict[str, tuple] = {
    "comment_completeness_target": ("base_config", "COMMENT_COMPLETENESS_TARGET"),
    "page_goto_timeout_sec": ("base_config", "PAGE_GOTO_TIMEOUT_SEC"),
    "scheduler_max_concurrency": ("base_config", "SCHEDULER_MAX_CONCURRENCY"),
    "scheduler_tick_sec": ("base_config", "SCHEDULER_TICK_SEC"),
    "scheduler_busy_wait_sec": ("base_config", "SCHEDULER_BUSY_WAIT_SEC"),
    "scheduler_run_history": ("base_config", "SCHEDULER_RUN_HISTORY"),
    "scheduler_active_hour_start": ("base_config", "SCHEDULER_ACTIVE_HOUR_START"),
    "scheduler_active_hour_end": ("base_config", "SCHEDULER_ACTIVE_HOUR_END"),
    "scheduler_jitter_min_sec": ("base_config", "SCHEDULER_JITTER_MIN_SEC"),
    "scheduler_jitter_max_sec": ("base_config", "SCHEDULER_JITTER_MAX_SEC"),
    "scheduler_window_spread_sec": ("base_config", "SCHEDULER_WINDOW_SPREAD_SEC"),
    "risk_cooldown_hours": ("base_config", "RISK_COOLDOWN_HOURS"),
    "s3_backup_purge_with_story": ("base_config", "S3_BACKUP_PURGE_WITH_STORY"),
    "xhs_max_concurrency_num": ("xhs_config", "XHS_MAX_CONCURRENCY_NUM"),
    "xhs_behavior_early_stop_pages": ("xhs_config", "XHS_BEHAVIOR_EARLY_STOP_PAGES"),
    "xhs_sub_comment_max_pages": ("xhs_config", "XHS_SUB_COMMENT_MAX_PAGES"),
}


def _parse(value: Optional[str], value_type: str) -> Any:
    """按 value_type 把字符串还原成对应类型。"""
    if value is None:
        return None
    text = str(value)
    try:
        if value_type == "bool":
            return text.strip().lower() in ("true", "1", "yes", "y", "on")
        if value_type == "int":
            return int(text)
        if value_type == "decimal":
            return float(Decimal(text))
        if value_type in ("enum", "string"):
            return text
        if value_type == "list":
            return [p.strip() for p in text.split(",") if p.strip()]
        if value_type == "json":
            import json
            return json.loads(text)
    except Exception:
        return None
    return text


class RuntimeConfig:
    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}
        self._types: Dict[str, str] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._loaded = False
        self.config_version = 0     # 全局版本号，供前端轮询
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    async def load(self, force: bool = False) -> None:
        """从库表加载配置；库不可用时静默降级为常量回退。"""
        async with self._lock:
            if self._loaded and not force:
                return
            try:
                rows = await self._fetch_items()
            except Exception:
                rows = []

            values, types, meta = {}, {}, {}
            max_version = 0
            for r in rows:
                key = r["cfg_key"]
                value_type = r["value_type"]
                raw = r["current_value"] if r["current_value"] is not None else r["default_value"]
                parsed = _parse(raw, value_type)
                if parsed is None:
                    continue
                values[key] = parsed
                types[key] = value_type
                meta[key] = {
                    "label": r["label"],
                    "description": r["description"],
                    "value_type": value_type,
                    "default_value": r["default_value"],
                    "enum_options": r["enum_options"],
                    "min_value": str(r["min_value"]) if r["min_value"] is not None else None,
                    "max_value": str(r["max_value"]) if r["max_value"] is not None else None,
                    "regex_rule": r["regex_rule"],
                    "unit": r["unit"],
                    "apply_timing": r["apply_timing"],
                    "is_advanced": r["is_advanced"],
                    "masked": r["masked"],
                    "group": r["group_code"],
                    "version": r["version"],
                }
                max_version = max(max_version, int(r["version"] or 1))

            if values:
                self._values, self._types, self._meta = values, types, meta
                self.config_version = max_version
                # 把进程级参数回写到 config 模块属性：改配置不需要重启/重建
                self._sync_to_config_modules()
            self._loaded = True

    def _sync_to_config_modules(self) -> None:
        """见 SYNC_TO_CONFIG：让 `config.XXX` 属性读到库里的最新值。"""
        import importlib

        import config as _config_pkg

        for cfg_key, (mod_name, attr) in SYNC_TO_CONFIG.items():
            if cfg_key not in self._values:
                continue
            value = self._values[cfg_key]
            try:
                mod = importlib.import_module(f"config.{mod_name}")
                setattr(mod, attr, value)
            except Exception:
                continue
            # config/__init__.py 是 `from .base_config import *`，包级属性是
            # 导入时复制的一份，必须一起改，否则 config.X 与
            # config.base_config.X 会不一致。
            if hasattr(_config_pkg, attr):
                try:
                    setattr(_config_pkg, attr, value)
                except Exception:
                    pass

    async def _fetch_items(self):
        # 延迟导入，避免 config 与 database 循环依赖
        from sqlalchemy import select

        from config.db_config import resolve_config_db_type
        from database.db_session import get_async_engine
        from database.models_v2 import CrawlConfigGroup, CrawlConfigItem
        from sqlalchemy.ext.asyncio import AsyncSession

        engine = get_async_engine(resolve_config_db_type())
        if engine is None:
            return []
        async with AsyncSession(engine, expire_on_commit=False) as session:
            rows = (await session.execute(
                select(
                    CrawlConfigItem.cfg_key, CrawlConfigItem.value_type,
                    CrawlConfigItem.current_value, CrawlConfigItem.default_value,
                    CrawlConfigItem.label, CrawlConfigItem.description,
                    CrawlConfigItem.enum_options, CrawlConfigItem.min_value,
                    CrawlConfigItem.max_value, CrawlConfigItem.regex_rule,
                    CrawlConfigItem.unit, CrawlConfigItem.apply_timing,
                    CrawlConfigItem.is_advanced, CrawlConfigItem.masked,
                    CrawlConfigItem.version, CrawlConfigGroup.code.label("group_code"),
                ).join(CrawlConfigGroup, CrawlConfigItem.group_id == CrawlConfigGroup.id)
                .where(CrawlConfigItem.visible.is_(True))
            )).mappings().all()
        return rows

    async def refresh(self) -> None:
        """配置变更后热加载（immediate 类）。"""
        await self.load(force=True)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _fallback(self, key: str, default: Any = None) -> Any:
        import config
        legacy = LEGACY_FALLBACK.get(key)
        if legacy and hasattr(config, legacy):
            return getattr(config, legacy)
        return default

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        return self._fallback(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, None)
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("true", "1", "yes", "y", "on")

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_str(self, key: str, default: str = "") -> str:
        v = self.get(key, None)
        return default if v is None else str(v)

    def get_list(self, key: str, default=None) -> list:
        v = self.get(key, None)
        if isinstance(v, list):
            return v
        if v is None:
            return default or []
        return [p.strip() for p in str(v).split(",") if p.strip()]

    def snapshot(self) -> Dict[str, Any]:
        """任务启动时快照（next_task 语义）。"""
        return dict(self._values)

    def meta(self) -> Dict[str, Dict[str, Any]]:
        return self._meta


runtime_config = RuntimeConfig()

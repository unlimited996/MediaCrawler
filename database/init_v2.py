# -*- coding: utf-8 -*-
"""v2 模型建表 + 基础数据种子（幂等，可重复执行）。

用法：
    python -m database.init_v2                 # 用 config.SAVE_DATA_OPTION 对应的库
    python -m database.init_v2 postgres        # 显式指定

建表用 project 已有的 async engine（asyncpg / aiosqlite），不引入 psycopg2。
"""
import asyncio
import sys
from typing import Dict, List

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import get_async_engine
from database.models_v2 import (
    BaseV2, CrawlConfigGroup, CrawlConfigItem, SchemaMigration, SourceSite,
)

SCHEMA_VERSION = "2026.10.1"

SOURCE_SITES: List[Dict] = [
    {"code": "xhs", "name": "小红书", "base_url": "https://www.xiaohongshu.com", "site_type": "social"},
    {"code": "dy", "name": "抖音", "base_url": "https://www.douyin.com", "site_type": "video"},
    {"code": "ks", "name": "快手", "base_url": "https://www.kuaishou.com", "site_type": "video"},
    {"code": "bili", "name": "哔哩哔哩", "base_url": "https://www.bilibili.com", "site_type": "video"},
    {"code": "wb", "name": "微博", "base_url": "https://weibo.com", "site_type": "social"},
    {"code": "tieba", "name": "百度贴吧", "base_url": "https://tieba.baidu.com", "site_type": "forum"},
    {"code": "zhihu", "name": "知乎", "base_url": "https://www.zhihu.com", "site_type": "forum"},
]

CONFIG_GROUPS: List[Dict] = [
    {"code": "crawl", "name": "抓取行为", "sort_weight": 10, "icon": "download",
     "description": "控制抓什么、抓多少、怎么翻页"},
    {"code": "risk", "name": "代理与风控", "sort_weight": 20, "icon": "shield",
     "description": "IP 代理池与反风控策略"},
    {"code": "browser", "name": "浏览器", "sort_weight": 30, "icon": "monitor",
     "description": "Playwright / CDP 相关设置"},
    {"code": "storage", "name": "存储与产物", "sort_weight": 40, "icon": "database",
     "description": "数据落盘方式与附件下载"},
    {"code": "system", "name": "系统", "sort_weight": 50, "icon": "settings",
     "description": "进程级设置，部分需重启生效"},
    {"code": "xhs_backup", "name": "小红书账号备份", "sort_weight": 60, "icon": "bookmark",
     "description": "账号点赞/收藏备份任务的抓取与补抓策略"},
]

# 全部来自 config/base_config.py 的硬编码常量（35 项）
CONFIG_ITEMS: List[Dict] = [
    # ---------------- crawl ----------------
    dict(group="crawl", key="platform", label="目标平台", value_type="enum",
         default="xhs", timing="next_task", weight=10,
         enum=[("xhs", "小红书"), ("dy", "抖音"), ("ks", "快手"), ("bili", "B站"),
               ("wb", "微博"), ("tieba", "贴吧"), ("zhihu", "知乎")],
         desc="默认抓取的平台"),
    dict(group="crawl", key="xhs_international", label="小红书国际版", value_type="bool",
         default="false", timing="next_task", weight=15,
         desc="开启后走 rednote.com，cookie 域使用 .rednote.com"),
    dict(group="crawl", key="keywords", label="搜索关键词", value_type="list",
         default="", timing="next_task", weight=20,
         desc="英文逗号分隔，最多 20 个；**搜索模式下必填**，留空会拒绝启动"),
    dict(group="crawl", key="login_type", label="登录方式", value_type="enum",
         default="qrcode", timing="next_task", weight=25,
         enum=[("qrcode", "扫码登录"), ("phone", "手机号"), ("cookie", "Cookie")],
         desc="扫码失败时可改用 Cookie 方式"),
    dict(group="crawl", key="cookies", label="Cookie", value_type="string",
         default="", timing="next_task", weight=30, masked=True, advanced=True,
         desc="登录方式为 cookie 时必填；内容敏感，前端掩码显示"),
    dict(group="crawl", key="crawler_type", label="抓取类型", value_type="enum",
         default="search", timing="next_task", weight=35,
         enum=[("search", "关键词搜索"), ("detail", "指定详情"), ("creator", "创作者主页"),
               ("homefeed", "首页推荐"), ("hot", "热搜榜单")],
         desc="决定本轮抓取的入口"),
    dict(group="crawl", key="start_page", label="起始页码", value_type="int",
         default="1", min=1, max=9999, timing="next_task", weight=40,
         desc="断点续爬可从指定页继续"),
    dict(group="crawl", key="crawler_max_notes_count", label="最大抓取条数", value_type="int",
         default="15", min=1, max=10000, timing="next_task", weight=45, unit="条",
         desc="单个关键词最多抓多少条内容"),
    dict(group="crawl", key="max_concurrency_num", label="并发数", value_type="int",
         default="1", min=1, max=50, timing="next_task", weight=50, unit="个",
         desc="并发过高会显著提升风控命中率"),
    dict(group="crawl", key="enable_get_comments", label="抓取评论", value_type="bool",
         default="true", timing="next_task", weight=55,
         desc="关闭则只抓内容不抓评论"),
    dict(group="crawl", key="crawler_max_comments_count_single_notes", label="单篇最大评论数",
         value_type="int", default="10", min=1, max=1000, timing="next_task", weight=60, unit="条",
         desc="每条内容最多抓多少条评论"),
    dict(group="crawl", key="enable_get_sub_comments", label="抓取子评论", value_type="bool",
         default="false", timing="next_task", weight=65,
         desc="开启后逐条主楼抓取楼中楼回复，请求量显著上升，请配合限速使用"),
    dict(group="crawl", key="crawler_max_sleep_sec", label="请求间隔", value_type="decimal",
         default="2", min=0.1, max=60, timing="immediate", weight=70, unit="秒",
         desc="每次请求后的休眠，立即生效；调低会显著增加风控风险"),
    dict(group="crawl", key="enable_checkpoint", label="断点续爬", value_type="bool",
         default="false", timing="next_task", weight=75,
         desc="记录每页抓取进度，中断后用相同参数重跑会自动跳过已完成的部分"),
    # ---------------- risk ----------------
    dict(group="risk", key="enable_ip_proxy", label="启用 IP 代理", value_type="bool",
         default="false", timing="next_task", weight=10,
         desc="启用后使用代理池，需要先配置 Redis"),
    dict(group="risk", key="ip_proxy_provider_name", label="代理服务商", value_type="enum",
         default="kuaidaili", timing="next_task", weight=15,
         enum=[("kuaidaili", "快代理"), ("wandouhttp", "豌豆HTTP"), ("static", "静态代理")],
         desc="选择代理 IP 的提供方"),
    dict(group="risk", key="ip_proxy_pool_count", label="代理池大小", value_type="int",
         default="2", min=1, max=100, timing="next_task", weight=20, unit="个",
         desc="池中维护的 IP 数量"),
    dict(group="risk", key="static_proxy_url", label="静态代理地址", value_type="string",
         default="", regex=r"^https?://", timing="next_task", weight=25, advanced=True,
         desc="形如 http://user:password@host:port"),
    # ---------------- browser ----------------
    dict(group="browser", key="enable_browser", label="启用浏览器", value_type="bool",
         default="true", timing="next_task", weight=5,
         desc="关闭后走无浏览器模式：不启动 Playwright，直接用配置里的 Cookie 发请求。"
              "需先填入有效 Cookie，否则会因未登录直接失败"),
    dict(group="browser", key="headless", label="无头模式", value_type="bool",
         default="false", timing="next_task", weight=10,
         desc="关闭则会弹出浏览器窗口，便于手动过验证"),
    dict(group="browser", key="save_login_state", label="保存登录状态", value_type="bool",
         default="true", timing="next_task", weight=15,
         desc="开启后复用本地登录态，避免每次扫码"),
    dict(group="browser", key="enable_cdp_mode", label="CDP 模式", value_type="bool",
         default="false", timing="next_task", weight=20,
         desc="连接本机已有 Chrome 的调试端口；端口被占用会导致启动失败"),
    dict(group="browser", key="cdp_connect_existing", label="连接已有浏览器", value_type="bool",
         default="true", timing="next_task", weight=25, advanced=True,
         desc="CDP 模式下复用已打开的浏览器实例"),
    dict(group="browser", key="cdp_debug_port", label="CDP 调试端口", value_type="int",
         default="9222", min=1024, max=65535, timing="next_task", weight=30,
         desc="需与浏览器启动参数一致"),
    dict(group="browser", key="cdp_headless", label="CDP 无头模式", value_type="bool",
         default="false", timing="next_task", weight=35, advanced=True, desc=""),
    dict(group="browser", key="auto_close_browser", label="自动关闭浏览器", value_type="bool",
         default="true", timing="next_task", weight=40, advanced=True,
         desc="任务结束后自动关闭浏览器进程"),
    dict(group="browser", key="custom_browser_path", label="自定义浏览器路径", value_type="string",
         default="", timing="next_task", weight=45, advanced=True,
         desc="留空则自动探测本机 Chrome/Edge"),
    dict(group="browser", key="browser_launch_timeout", label="浏览器启动超时", value_type="int",
         default="60", min=5, max=600, timing="next_task", weight=50, unit="秒",
         desc="超过该时间仍未连上则判定失败"),
    # ---------------- storage ----------------
    dict(group="storage", key="save_data_option", label="存储方式", value_type="enum",
         default="jsonl", timing="next_task", weight=10,
         enum=[("jsonl", "JSONL 文件"), ("json", "JSON 文件"), ("csv", "CSV"),
               ("sqlite", "SQLite"), ("mysql", "MySQL"), ("postgres", "PostgreSQL"),
               ("mongodb", "MongoDB"), ("excel", "Excel")],
         desc="决定抓取结果落到哪里"),
    dict(group="storage", key="save_data_path", label="数据保存路径", value_type="string",
         default="", timing="next_task", weight=15, advanced=True,
         desc="留空则保存到 data 目录"),
    dict(group="storage", key="enable_get_media", label="下载媒体附件", value_type="bool",
         default="false", timing="next_task", weight=20,
         desc="下载图片/视频到本地，会显著增加耗时与磁盘占用"),
    dict(group="storage", key="enable_get_wordcloud", label="生成词云", value_type="bool",
         default="false", timing="next_task", weight=25,
         desc="仅 json/jsonl 存储模式支持"),
    dict(group="storage", key="custom_words", label="自定义词云词", value_type="json",
         default="{}", timing="next_task", weight=30, advanced=True,
         desc="JSON 对象，用于词云分词"),
    dict(group="storage", key="stop_words_file", label="停用词文件", value_type="string",
         default="./docs/hit_stopwords.txt", timing="next_task", weight=35, advanced=True, desc=""),
    dict(group="storage", key="font_path", label="词云字体路径", value_type="string",
         default="./docs/STZHONGS.TTF", timing="next_task", weight=40, advanced=True, desc=""),
    # ---------------- system ----------------
    dict(group="system", key="user_data_dir", label="浏览器用户目录", value_type="string",
         default="%s_user_data_dir", timing="next_task", weight=10, advanced=True,
         desc="%s 会被替换为平台名"),
    dict(group="system", key="disable_ssl_verify", label="跳过 SSL 校验", value_type="bool",
         default="false", timing="restart", weight=15, advanced=True,
         desc="开启会降低安全性，需重启服务生效"),
    dict(group="system", key="log_level", label="日志级别", value_type="enum",
         default="INFO", timing="immediate", weight=20,
         enum=[("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR")],
         desc="立即生效"),
    # ---------------- xhs_backup ----------------
    dict(group="xhs_backup", key="xhs_behavior_max_notes", label="备份单次抓取上限", value_type="int",
         default="100", min=1, max=5000, timing="next_task", weight=10, unit="篇",
         desc="账号点赞/收藏备份任务每轮最多抓多少篇（新帖 + 待补抓）。调大可让每轮覆盖更多帖子，但更易触发风控"),
    dict(group="xhs_backup", key="xhs_behavior_incremental", label="增量模式", value_type="bool",
         default="true", timing="next_task", weight=15,
         desc="开启后已归档（评论已完整）的帖子跳过详情与评论，只抓新帖与未完整的帖子"),
    dict(group="xhs_backup", key="xhs_comment_backfill_max_comments", label="每篇预留评论额度", value_type="int",
         default="4000", min=0, max=100000, timing="next_task", weight=20, unit="条",
         desc="每轮补抓批次的总评论预算 = 单次抓取上限 × 本值；决定一轮能补多少篇未完成评论的帖子。"
              "此前硬编码 3000 导致每轮只补约 8 篇"),
    dict(group="xhs_backup", key="xhs_comment_max_per_note", label="单篇评论硬顶", value_type="int",
         default="0", min=0, max=100000, timing="next_task", weight=25, unit="条",
         desc="单篇最多抓多少条评论；0 = 不限制（翻到 has_more=false 或达到声明数）"),
    dict(group="xhs_backup", key="xhs_comment_count_margin", label="评论数余量", value_type="int",
         default="30", min=0, max=10000, timing="next_task", weight=30, unit="条",
         desc="判定评论是否抓全时，允许比声明数少这么多条仍算完整，容忍平台计数延迟"),
    dict(group="xhs_backup", key="xhs_detail_budget_per_round", label="单轮详情请求预算", value_type="int",
         default="25", min=0, max=200, timing="next_task", weight=35, unit="次",
         desc="单轮最多发起多少篇笔记详情请求（点赞/收藏备份每篇=1次请求）。"
              "小红书按『单轮累计请求量』限流，约 80 次即触发 461；本预算在撞 461 之前"
              "主动停手，剩余帖子留到下一轮。0 = 不限制（回到旧行为，有撞 461 风险）"),
    dict(group="xhs_backup", key="xhs_comment_budget_per_round", label="单轮评论请求预算", value_type="int",
         default="20", min=0, max=200, timing="next_task", weight=40, unit="次",
         desc="单轮最多打多少次评论接口（主评论+楼中楼都算）。与『详情预算』合计应"
              "稳定低于 ~80 才安全：默认 25+20+列表≈9 ≈ 54。两者之和别超过 ~75。"
              "0 = 不限制（有撞 461 风险）"),
]


def _enum_options(pairs) -> List[Dict]:
    return [{"value": v, "label": l} for v, l in pairs] if pairs else None


async def _seed(session: AsyncSession) -> Dict[str, int]:
    stats = {"site": 0, "group": 0, "item": 0}

    # 来源站点
    for s in SOURCE_SITES:
        exists = await session.execute(select(SourceSite).where(SourceSite.code == s["code"]))
        if exists.scalar_one_or_none():
            continue
        session.add(SourceSite(**s))
        stats["site"] += 1

    # 配置分组
    group_ids: Dict[str, int] = {}
    for g in CONFIG_GROUPS:
        row = (await session.execute(
            select(CrawlConfigGroup).where(CrawlConfigGroup.code == g["code"])
        )).scalar_one_or_none()
        if not row:
            row = CrawlConfigGroup(**g)
            session.add(row)
            await session.flush()
            stats["group"] += 1
        group_ids[g["code"]] = row.id

    # 配置项
    for it in CONFIG_ITEMS:
        row = (await session.execute(
            select(CrawlConfigItem).where(CrawlConfigItem.cfg_key == it["key"])
        )).scalar_one_or_none()
        if row:
            continue
        session.add(CrawlConfigItem(
            group_id=group_ids[it["group"]],
            cfg_key=it["key"],
            label=it["label"],
            description=it.get("desc", ""),
            value_type=it["value_type"],
            default_value=str(it["default"]),
            current_value=str(it["default"]),
            enum_options=_enum_options(it.get("enum")),
            min_value=it.get("min"),
            max_value=it.get("max"),
            regex_rule=it.get("regex"),
            unit=it.get("unit"),
            apply_timing=it["timing"],
            sort_weight=it.get("weight", 100),
            is_advanced=bool(it.get("advanced", False)),
            masked=bool(it.get("masked", False)),
            version=1,
        ))
        stats["item"] += 1

    # 结构版本登记
    ver = (await session.execute(
        select(SchemaMigration).where(SchemaMigration.version == SCHEMA_VERSION)
    )).scalar_one_or_none()
    if not ver:
        session.add(SchemaMigration(
            version=SCHEMA_VERSION,
            alembic_rev=None,
            description="story aggregation model v2 + config tables",
            rollback_sql="DROP TABLE IF EXISTS crawl_config_audit, crawl_config_item, "
                         "crawl_config_group, schema_migration, crawl_checkpoint, "
                         "crawl_task_log, crawl_task, media_attachment, story_tag, tag, "
                         "comment, story_chapter, story, author, source_site CASCADE;",
        ))

    # S3 备份配置种子（P4）：库为空时写入 garage 配置，幂等
    try:
        from tools.media_backup import ensure_seed_config

        await ensure_seed_config(session)
    except Exception as e:
        print(f"[init_v2] S3 种子配置跳过: {e}")

    # 清理已废弃的配置项（2026-11-02：风控冷却闸口已移除，
    # risk_cooldown_hours 不再有任何读取方，从页面与库中删掉避免误导）
    try:
        dead = await session.execute(
            select(CrawlConfigItem).where(CrawlConfigItem.cfg_key == "risk_cooldown_hours"))
        for row in dead.scalars().all():
            await session.delete(row)
    except Exception as e:
        print(f"[init_v2] 清理 risk_cooldown_hours 跳过: {e}")

    # S3 / 媒体备份表登记（2026.10.2）
    ver2 = (await session.execute(
        select(SchemaMigration).where(SchemaMigration.version == "2026.10.2")
    )).scalar_one_or_none()
    if not ver2:
        session.add(SchemaMigration(
            version="2026.10.2",
            alembic_rev=None,
            description="s3_config + media_backup (media auto backup to S3)",
            rollback_sql="DROP TABLE IF EXISTS media_backup, s3_config CASCADE;",
        ))

    await session.commit()
    return stats


#: 老库补列：create_all 不会 ALTER，历史库需要显式加列（全部 IF NOT EXISTS）
ADD_COLUMN_SQL = [
    ("media_attachment", "storage_type", "VARCHAR(16) NOT NULL DEFAULT 'url'"),
    ("media_attachment", "storage_key", "TEXT"),
    ("media_attachment", "checksum", "VARCHAR(64)"),
    ("media_attachment", "source_status", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
    ("media_attachment", "source_checked_at", "TIMESTAMPTZ"),
    ("media_attachment", "downloaded_at", "TIMESTAMPTZ"),
    ("media_attachment", "inline_data", "BYTEA"),
    ("media_attachment", "inline_mime", "VARCHAR(64)"),
    ("story", "media_count", "INTEGER NOT NULL DEFAULT 0"),
    ("story", "cover_media_id", "BIGINT"),
    ("story", "edited_at", "BIGINT"),
    ("story", "edit_count", "INTEGER NOT NULL DEFAULT 0"),
    ("comment", "stance_tag_id", "BIGINT"),
    ("comment", "stance_tag_code", "VARCHAR(32)"),
    ("comment", "stance_confidence", "NUMERIC(4,2)"),
    ("comment", "stance_judged_ver", "BIGINT"),
    ("comment", "ip_location", "VARCHAR(64)"),
    # 运行记录细粒度统计（新增/更新帖子 + 前后完整率），2026-10-29 加。
    # 不填则老记录下拉不显示统计区，前端做了空值兜底。
    ("scheduled_task_run", "stats_json", "JSONB"),
]


async def _ensure_columns(conn) -> int:
    added = 0
    for table, column, ddl in ADD_COLUMN_SQL:
        try:
            await conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))
            added += 1
        except Exception as e:  # 表不存在等情况跳过，不阻断初始化
            print(f"[init_v2] 补列跳过 {table}.{column}: {e}")
    return added


async def init_v2(db_type: str = None) -> Dict[str, int]:
    engine = get_async_engine(db_type)
    if engine is None:
        raise RuntimeError("当前存储方式不是数据库（json/jsonl/csv），请指定 db_type")

    async with engine.begin() as conn:
        await conn.run_sync(BaseV2.metadata.create_all)
        # 历史库补列（图片持久化 / 编辑记录 / 态度标签库）
        await _ensure_columns(conn)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        stats = await _seed(session)

    await engine.dispose()
    return stats


def main() -> None:
    db_type = sys.argv[1] if len(sys.argv) > 1 else None
    stats = asyncio.run(init_v2(db_type))
    print(f"[init_v2] schema={SCHEMA_VERSION} 新增 站点{stats['site']} "
          f"分组{stats['group']} 配置项{stats['item']}")


if __name__ == "__main__":
    main()

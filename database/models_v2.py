# -*- coding: utf-8 -*-
"""故事聚合系统 v2 数据模型。

与 v1（database/models.py，7 平台 × 内容/评论）并存：v2 是统一模型，
v1 保持不变，双轨期结束后再下线，保证存量数据与现有功能不受影响。

扩展约定：
- 需要筛选/排序/统计的字段 → 结构化列
- 平台特有、只做展示或归档的 → ext JSONB（新增字段零迁移）
- 每张表带 schema_ver，标记写入时的结构版本，读取时按版本解析以兼容旧行
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON,
    LargeBinary, Numeric, SmallInteger, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

# PG 用 JSONB（支持 GIN 索引），其它方言退化为 JSON
JsonType = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")

BaseV2 = declarative_base()


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SoftDeleteMixin:
    deleted_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# 1. 来源站点
# ---------------------------------------------------------------------------
class SourceSite(BaseV2, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "source_site"

    id = Column(SmallInteger, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False, unique=True)          # xhs / dy / ks ...
    name = Column(String(64), nullable=False)
    base_url = Column(String(255))
    site_type = Column(String(32), nullable=False, server_default="social")
    enabled = Column(Boolean, nullable=False, server_default="true")
    capabilities = Column(JsonType, nullable=False, default=dict)   # 站点能力：是否需要浏览器等
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("idx_site_enabled", "enabled"),
    )


# ---------------------------------------------------------------------------
# 2. 作者（不落可识别个人信息，沿用匿名哈希）
# ---------------------------------------------------------------------------
class Author(BaseV2, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "author"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    anon_hash = Column(String(64), nullable=False)
    source_site_id = Column(SmallInteger, ForeignKey("source_site.id"), nullable=False)
    nickname = Column(String(128))
    avatar_url = Column(Text)
    profile_url = Column(Text)
    follower_count = Column(BigInteger)
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_author_site_hash", "source_site_id", "anon_hash", unique=True),
        Index("idx_author_nick", "nickname"),
    )


# ---------------------------------------------------------------------------
# 3. 故事（聚合后的事件）
# ---------------------------------------------------------------------------
class Story(BaseV2, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "story"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    title = Column(Text, nullable=False)
    summary = Column(Text)
    cover_url = Column(Text)
    dedupe_key = Column(String(128))
    category = Column(String(64))
    status = Column(String(16), nullable=False, server_default="active")
    primary_site_id = Column(SmallInteger, ForeignKey("source_site.id"))
    author_id = Column(BigInteger, ForeignKey("author.id"))

    # 高频统计字段（排序/筛选用，必须结构化）
    chapter_count = Column(Integer, nullable=False, server_default="0")
    comment_count = Column(Integer, nullable=False, server_default="0")
    like_count = Column(BigInteger, nullable=False, server_default="0")
    view_count = Column(BigInteger)
    heat_score = Column(Numeric(12, 4), nullable=False, server_default="0")
    first_publish_ts = Column(BigInteger)
    last_update_ts = Column(BigInteger)
    # ---- 图片 / 编辑记录（P1/P2）----
    media_count = Column(Integer, nullable=False, server_default="0")
    cover_media_id = Column(BigInteger)
    edited_at = Column(BigInteger)
    edit_count = Column(Integer, nullable=False, server_default="0")

    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_story_dedupe", "dedupe_key", unique=True),
        Index("idx_story_heat", "heat_score", "last_update_ts"),
        Index("idx_story_cat_ts", "category", "last_update_ts"),
        Index("idx_story_status", "status"),
    )


# ---------------------------------------------------------------------------
# 4. 章节 / 正文（承载原始抓取内容，带溯源坐标）
# ---------------------------------------------------------------------------
class StoryChapter(BaseV2, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "story_chapter"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    story_id = Column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), nullable=False)
    source_site_id = Column(SmallInteger, ForeignKey("source_site.id"), nullable=False)
    author_id = Column(BigInteger, ForeignKey("author.id"))
    source_content_id = Column(String(255), nullable=False)   # 平台原始 ID
    source_url = Column(Text)
    seq_no = Column(Integer, nullable=False, server_default="1")

    title = Column(Text)
    content_text = Column(Text)
    content_html = Column(Text)

    like_count = Column(BigInteger, nullable=False, server_default="0")
    comment_count = Column(BigInteger, nullable=False, server_default="0")
    share_count = Column(BigInteger)
    collect_count = Column(BigInteger)
    publish_ts = Column(BigInteger)
    source_publish_text = Column(String(64))

    declared_comment_count = Column(Integer)      # 平台声明数，用于完整率
    fetched_comment_count = Column(Integer, nullable=False, server_default="0")
    completeness = Column(Numeric(5, 4))
    content_hash = Column(String(64))

    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_chapter_source", "source_site_id", "source_content_id", unique=True),
        Index("idx_chapter_story", "story_id", "seq_no"),
        Index("idx_chapter_publish", "publish_ts"),
        Index("idx_chapter_author", "author_id"),
    )


# ---------------------------------------------------------------------------
# 5. 评论（parent_id 自关联 + root_id 冗余，支持无限层级）
# ---------------------------------------------------------------------------
class Comment(BaseV2, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "comment"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    story_id = Column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), nullable=False)
    chapter_id = Column(BigInteger, ForeignKey("story_chapter.id", ondelete="CASCADE"))
    source_site_id = Column(SmallInteger, ForeignKey("source_site.id"), nullable=False)
    author_id = Column(BigInteger, ForeignKey("author.id"))

    source_comment_id = Column(String(255), nullable=False)
    parent_id = Column(BigInteger, ForeignKey("comment.id", ondelete="CASCADE"))
    root_id = Column(BigInteger, ForeignKey("comment.id", ondelete="CASCADE"))
    depth = Column(SmallInteger, nullable=False, server_default="0")

    content_text = Column(Text, nullable=False)
    like_count = Column(BigInteger, nullable=False, server_default="0")
    sub_count = Column(Integer, nullable=False, server_default="0")
    publish_ts = Column(BigInteger)
    # 评论 IP 属地（原站「IP属地：xx」）。重抓时由 store 层从评论原始 JSON 提取。
    ip_location = Column(String(64))

    sentiment = Column(String(16))                # AI 分析产物
    # 立场：相对发帖人核心观点的 agree / neutral / oppose（内容赞同度指标）
    stance = Column(String(16))
    # ---- 态度标签库（P3）----
    # 命中的标签（库表驱动，不再硬编码）；stance 保留为 polarity 三态归并
    stance_tag_id = Column(BigInteger, ForeignKey("stance_tag.id"))
    stance_tag_code = Column(String(32))
    stance_confidence = Column(Numeric(4, 2))
    stance_judged_ver = Column(BigInteger)        # 判定时的标签库版本，便于重算
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_comment_source", "source_site_id", "source_comment_id", unique=True),
        Index("idx_cmt_story_like", "story_id", "like_count"),
        Index("idx_cmt_tag", "stance_tag_code"),
        Index("idx_cmt_root", "root_id", "publish_ts"),
        Index("idx_cmt_parent", "parent_id"),
        Index("idx_cmt_chapter", "chapter_id", "like_count"),
        Index("idx_cmt_sentiment", "story_id", "sentiment"),
    )


# ---------------------------------------------------------------------------
# 6. 标签
# ---------------------------------------------------------------------------
class Tag(BaseV2, TimestampMixin):
    __tablename__ = "tag"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    source_site_id = Column(SmallInteger, ForeignKey("source_site.id"))  # NULL=全局标签
    tag_type = Column(String(16), nullable=False, server_default="topic")
    ext = Column(JsonType, nullable=False, default=dict)

    __table_args__ = (
        Index("uq_tag_site_name", "source_site_id", "name", unique=True),
        Index("idx_tag_name", "name"),
    )


class StoryTag(BaseV2, TimestampMixin):
    __tablename__ = "story_tag"

    story_id = Column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(BigInteger, ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True)
    weight = Column(Numeric(5, 2), nullable=False, server_default="1.0")

    __table_args__ = (
        Index("idx_storytag_tag", "tag_id", "story_id"),
    )


# ---------------------------------------------------------------------------
# 7. 媒体附件（多态：story / chapter / comment）
# ---------------------------------------------------------------------------
class MediaAttachment(BaseV2, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "media_attachment"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    target_type = Column(String(16), nullable=False)   # story / chapter / comment
    target_id = Column(BigInteger, nullable=False)
    media_type = Column(String(16), nullable=False)    # image / video / audio / emoji
    source_url = Column(Text, nullable=False)
    local_path = Column(Text)
    mime_type = Column(String(64))
    size_bytes = Column(BigInteger)
    # ---- 图片持久化（P1）----
    # 存储位置：local(本地 DATA_DIR) / object(对象存储) / inline(小图编码入库) / url(仅外链)
    storage_type = Column(String(16), nullable=False, server_default="url")
    # 对象存储 object key 或相对路径，配套 storage_type 使用
    storage_key = Column(Text)
    # 内容 sha256：跨 story 去重同一张图，删除 story 时不误删仍被引用的文件
    checksum = Column(String(64))
    # 外链健康度：alive / dead / unknown（原帖删除或签名过期后置 dead）
    source_status = Column(String(16), nullable=False, server_default="unknown")
    source_checked_at = Column(DateTime(timezone=True))
    downloaded_at = Column(DateTime(timezone=True))
    # 仅 <256KB 的小图/头像兜底编码入库；大图一律落盘或对象存储
    inline_data = Column(LargeBinary)
    inline_mime = Column(String(64))
    width = Column(Integer)
    height = Column(Integer)
    duration_sec = Column(Integer)
    sort_order = Column(Integer, nullable=False, server_default="0")
    download_status = Column(String(16), nullable=False, server_default="pending")
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("idx_media_target", "target_type", "target_id", "sort_order"),
        Index("idx_media_status", "download_status"),
        Index("idx_media_checksum", "checksum"),
        Index("idx_media_storage", "storage_type", "download_status"),
        Index("uq_media_src", "target_type", "target_id", "source_url", unique=True),
    )


# ---------------------------------------------------------------------------
# 8. 抓取任务 / 日志 / 断点
# ---------------------------------------------------------------------------
class CrawlTask(BaseV2, TimestampMixin):
    __tablename__ = "crawl_task"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_code = Column(String(64), nullable=False, unique=True)
    source_site_id = Column(SmallInteger, ForeignKey("source_site.id"))
    crawl_type = Column(String(32), nullable=False)
    params_json = Column(JsonType, nullable=False, default=dict)
    config_snapshot = Column(JsonType)       # 本轮生效配置（next_task 语义必需）
    status = Column(String(16), nullable=False, server_default="pending")
    progress_json = Column(JsonType)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    error_message = Column(Text)

    __table_args__ = (
        Index("idx_task_site_status", "source_site_id", "status"),
        Index("idx_task_created", "created_at"),
    )


class CrawlTaskLog(BaseV2):
    __tablename__ = "crawl_task_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(BigInteger, ForeignKey("crawl_task.id", ondelete="CASCADE"), nullable=False)
    level = Column(String(16), nullable=False)
    event = Column(String(64))
    message = Column(Text)
    context_json = Column(JsonType)
    risk_code = Column(String(32))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_tasklog_task", "task_id", "created_at"),
        Index("idx_tasklog_level", "level", "created_at"),
        Index("idx_tasklog_risk", "risk_code", "created_at"),
    )


class CrawlCheckpoint(BaseV2):
    __tablename__ = "crawl_checkpoint"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(BigInteger, ForeignKey("crawl_task.id", ondelete="CASCADE"), nullable=False)
    scope_type = Column(String(32), nullable=False)
    scope_key = Column(String(255), nullable=False)
    cursor = Column(Text)
    page_no = Column(Integer, nullable=False, server_default="1")
    state = Column(String(16), nullable=False, server_default="pending")
    fetched_count = Column(Integer, nullable=False, server_default="0")
    declared_count = Column(Integer)
    payload_json = Column(JsonType)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    last_error = Column(Text)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("uq_ck_scope", "task_id", "scope_type", "scope_key", "page_no", unique=True),
        Index("idx_ck_state", "task_id", "state"),
    )


# ---------------------------------------------------------------------------
# 9. 结构版本登记
# ---------------------------------------------------------------------------
class SchemaMigration(BaseV2):
    __tablename__ = "schema_migration"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    version = Column(String(32), nullable=False, unique=True)
    alembic_rev = Column(String(64))
    description = Column(Text)
    applied_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    rollback_sql = Column(Text)


# ---------------------------------------------------------------------------
# 10. 配置持久化（替代散落的硬编码开关）
# ---------------------------------------------------------------------------
class CrawlConfigGroup(BaseV2, TimestampMixin):
    __tablename__ = "crawl_config_group"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(64), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    description = Column(Text)
    sort_weight = Column(Integer, nullable=False, server_default="100")
    icon = Column(String(64))


class CrawlConfigItem(BaseV2, TimestampMixin):
    __tablename__ = "crawl_config_item"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    group_id = Column(BigInteger, ForeignKey("crawl_config_group.id", ondelete="CASCADE"), nullable=False)
    cfg_key = Column(String(64), nullable=False, unique=True)
    label = Column(String(128), nullable=False)
    description = Column(Text)
    value_type = Column(String(16), nullable=False)      # bool/int/decimal/string/enum/list/json
    default_value = Column(Text)
    current_value = Column(Text)
    enum_options = Column(JsonType)
    min_value = Column(Numeric(20, 6))
    max_value = Column(Numeric(20, 6))
    regex_rule = Column(String(255))
    unit = Column(String(16))
    apply_timing = Column(String(16), nullable=False, server_default="next_task")
    sort_weight = Column(Integer, nullable=False, server_default="100")
    is_advanced = Column(Boolean, nullable=False, server_default="false")
    visible = Column(Boolean, nullable=False, server_default="true")
    masked = Column(Boolean, nullable=False, server_default="false")   # 敏感项前端掩码
    version = Column(Integer, nullable=False, server_default="1")      # 乐观锁
    updated_by = Column(String(64))

    __table_args__ = (
        Index("idx_cfg_group", "group_id", "sort_weight", "cfg_key"),
    )


# ---------------------------------------------------------------------------
# 10b. HomeFeed 与热搜榜单（方案 #10）
# ---------------------------------------------------------------------------
class HomefeedContent(BaseV2, TimestampMixin):
    """首页推荐流内容。"""

    __tablename__ = "homefeed_content"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False)
    source_site_id = Column(SmallInteger, ForeignKey("source_site.id"))
    content_id = Column(String(255), nullable=False)
    title = Column(Text)
    desc = Column(Text)
    author_hash = Column(String(64))
    author_nickname = Column(String(128))
    cover_url = Column(Text)
    content_url = Column(Text)
    liked_count = Column(BigInteger, nullable=False, server_default="0")
    comment_count = Column(BigInteger, nullable=False, server_default="0")
    share_count = Column(BigInteger)
    publish_ts = Column(BigInteger)
    task_id = Column(BigInteger)
    raw_json = Column(JsonType)
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_feed_platform_content", "platform", "content_id", unique=True),
        Index("idx_feed_platform_ts", "platform", "publish_ts"),
        Index("idx_feed_like", "platform", "liked_count"),
    )


class HotBoard(BaseV2):
    """热搜榜单快照（按抓取时间存多份，支持历史对比）。"""

    __tablename__ = "hot_board"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False)
    board_code = Column(String(64), nullable=False)      # wb_hot / zhihu_hot ...
    board_name = Column(String(128))
    captured_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    task_id = Column(BigInteger)
    item_count = Column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        Index("uq_board_snapshot", "platform", "board_code", "captured_at", unique=True),
        Index("idx_board_latest", "platform", "board_code", "captured_at"),
    )


class HotBoardItem(BaseV2):
    """榜单条目。"""

    __tablename__ = "hot_board_item"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    board_id = Column(BigInteger, ForeignKey("hot_board.id", ondelete="CASCADE"), nullable=False)
    rank_no = Column(Integer, nullable=False)
    title = Column(Text, nullable=False)
    hot_value = Column(BigInteger)
    hot_value_text = Column(String(64))                  # 平台原始展示值（如 "123万"）
    content_url = Column(Text)
    tag = Column(String(64))

    __table_args__ = (
        Index("uq_board_item_rank", "board_id", "rank_no", unique=True),
        Index("idx_board_item_title", "title"),
    )


# ---------------------------------------------------------------------------
# 11. 账号池 / 代理池（方案 #2）
# ---------------------------------------------------------------------------
class Account(BaseV2, TimestampMixin):
    """抓取账号池。cookie 加密存储，不落明文。"""

    __tablename__ = "account"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False)
    account_name = Column(String(128), nullable=False)       # 本地标识
    cookie_cipher = Column(Text)                             # Fernet 加密后的 Cookie
    login_type = Column(String(16))                          # qrcode/phone/cookie
    status = Column(String(16), nullable=False, server_default="active")  # active/cooling/banned/disabled
    health_score = Column(Integer, nullable=False, server_default="100")
    profile_dir = Column(Text)                               # 独立浏览器 profile（会话隔离）
    last_used_at = Column(DateTime(timezone=True))
    cooldown_until = Column(DateTime(timezone=True))
    fail_count = Column(Integer, nullable=False, server_default="0")
    success_count = Column(Integer, nullable=False, server_default="0")
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_account_platform_name", "platform", "account_name", unique=True),
        Index("idx_account_pick", "platform", "status", "health_score"),
    )


class ProxyIp(BaseV2, TimestampMixin):
    """代理 IP 池：持久化 + 健康度，替代原来的纯内存池。"""

    __tablename__ = "proxy_ip"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    provider = Column(String(32), nullable=False)
    ip = Column(String(64), nullable=False)
    port = Column(Integer, nullable=False)
    protocol = Column(String(16), nullable=False, server_default="http")
    region = Column(String(64))
    status = Column(String(16), nullable=False, server_default="unknown")  # ok/slow/dead/unknown
    latency_ms = Column(Integer)
    success_count = Column(Integer, nullable=False, server_default="0")
    fail_count = Column(Integer, nullable=False, server_default="0")
    expire_at = Column(DateTime(timezone=True))
    ext = Column(JsonType, nullable=False, default=dict)

    __table_args__ = (
        Index("uq_proxy_ip_port", "ip", "port", "protocol", unique=True),
        Index("idx_proxy_pick", "status", "latency_ms"),
    )


class CrawlConfigAudit(BaseV2):
    __tablename__ = "crawl_config_audit"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cfg_key = Column(String(64), nullable=False)
    old_value = Column(Text)
    new_value = Column(Text)
    operator = Column(String(64))
    reason = Column(Text)
    changed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_cfg_audit", "cfg_key", "changed_at"),
    )


# ---------------------------------------------------------------------------
# 11. 定时拉取任务 / 执行记录
#
# 两类任务粒度：
#   profile —— 按用户主页定期全量 / 增量刷新该作者的全部帖子
#   note    —— 按帖子地址定期更新单帖内容
# 调度支持 interval（秒）与 cron（5 段表达式）两种方式；
# 并发 / 超时 / 重试 / 限流都是任务级配置，见 api/services/task_scheduler.py。
# ---------------------------------------------------------------------------
class ScheduledTask(BaseV2, TimestampMixin):
    __tablename__ = "scheduled_task"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_code = Column(String(64), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    platform = Column(String(16), nullable=False)          # xhs / dy / ks ...
    task_type = Column(String(16), nullable=False)         # profile | note
    target = Column(Text, nullable=False)                  # 主页 URL 或帖子 URL
    mode = Column(String(16), nullable=False, server_default="incremental")  # full | incremental

    # 调度配置：二选一（interval 模式用 interval_sec，cron 模式用 cron_expr）
    schedule_type = Column(String(16), nullable=False, server_default="interval")
    interval_sec = Column(Integer)
    cron_expr = Column(String(64))

    # 执行控制（任务级配置化）
    max_concurrency = Column(Integer, nullable=False, server_default="1")   # 爬虫进程内并发
    timeout_sec = Column(Integer, nullable=False, server_default="1800")    # 单次执行超时
    retry_count = Column(Integer, nullable=False, server_default="1")       # 失败重试次数
    retry_delay_sec = Column(Integer, nullable=False, server_default="60")  # 重试间隔
    rate_interval_sec = Column(Integer, nullable=False, server_default="3") # 翻页/请求限流间隔

    # 抓取选项快照（login_type / enable_comments / save_option / headless / cookies ...）
    options_json = Column(JsonType, nullable=False, default=dict)

    enabled = Column(Boolean, nullable=False, server_default="true")
    status = Column(String(16), nullable=False, server_default="idle")      # idle | running | error
    last_run_at = Column(DateTime(timezone=True))
    last_status = Column(String(16))                                         # success | failed | timeout | skipped
    last_error = Column(Text)
    next_run_at = Column(DateTime(timezone=True))
    run_count = Column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        Index("idx_sched_enabled_next", "enabled", "next_run_at"),
        Index("idx_sched_platform", "platform", "task_type"),
    )


class ScheduledTaskRun(BaseV2):
    __tablename__ = "scheduled_task_run"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(BigInteger, ForeignKey("scheduled_task.id", ondelete="CASCADE"),
                     nullable=False)
    trigger_type = Column(String(16), nullable=False, server_default="manual")  # schedule | manual
    status = Column(String(16), nullable=False, server_default="running")      # running | success | failed | timeout | skipped
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True))
    duration_ms = Column(Integer)
    # 增量统计
    new_count = Column(Integer, nullable=False, server_default="0")       # 本次新增帖子
    updated_count = Column(Integer, nullable=False, server_default="0")   # 本次刷新（已存在）
    skipped_count = Column(Integer, nullable=False, server_default="0")   # 未变化
    attempt_count = Column(Integer, nullable=False, server_default="1")   # 实际尝试次数（含重试）
    error_message = Column(Text)
    log_json = Column(JsonType)                                            # 最近若干行子进程日志
    # 本次运行的细粒度统计：新增/更新了哪些帖子 + 补抓前后的评论完整率。
    # 只靠 new_count/updated_count 三个数字说不清「刷了谁、补了多少」，
    # 前端执行记录下拉据此展示明细。
    stats_json = Column(JsonType)

    __table_args__ = (
        Index("idx_run_task_started", "task_id", "started_at"),
        Index("idx_run_status", "status", "started_at"),
    )


# ---------------------------------------------------------------------------
# 12. 帖子编辑记录（P2）
# ---------------------------------------------------------------------------
class StoryEditLog(BaseV2):
    """帖子编辑记录：每次抓取检测到内容变化时落一条，支持前后比对。"""

    __tablename__ = "story_edit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    story_id = Column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), nullable=False)
    chapter_id = Column(BigInteger, ForeignKey("story_chapter.id", ondelete="CASCADE"))
    edit_ts = Column(BigInteger, nullable=False)
    changed_fields = Column(JsonType, nullable=False, default=list)
    before = Column(JsonType, nullable=False, default=dict)
    after = Column(JsonType, nullable=False, default=dict)
    diff = Column(JsonType, nullable=False, default=list)
    source = Column(String(32))                   # crawl / manual / backfill
    crawl_task_id = Column(BigInteger)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_edit_story", "story_id", "edit_ts"),
    )


# ---------------------------------------------------------------------------
# 13. 回复态度标签库（P3）
# ---------------------------------------------------------------------------
class StanceTag(BaseV2):
    """态度标签定义（可维护，判定逻辑与代码解耦）。"""

    __tablename__ = "stance_tag"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False, unique=True)
    name = Column(String(64), nullable=False)
    name_i18n = Column(JsonType, nullable=False, default=dict)
    # 归并到赞同度的三态：agree / neutral / oppose
    polarity = Column(String(16), nullable=False, server_default="neutral")
    color = Column(String(16))
    description = Column(Text)
    examples = Column(JsonType, nullable=False, default=list)
    enabled = Column(Boolean, nullable=False, server_default="true")
    sort_order = Column(Integer, nullable=False, server_default="0")
    # 命中冲突时的优先级，越小越先匹配
    priority = Column(Integer, nullable=False, server_default="100")
    is_fallback = Column(Boolean, nullable=False, server_default="false")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    __table_args__ = (
        Index("idx_stance_tag_enabled", "enabled", "sort_order"),
    )


class StanceTagRule(BaseV2):
    """标签判定规则（与标签解耦，可单条启停）。"""

    __tablename__ = "stance_tag_rule"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tag_id = Column(BigInteger, ForeignKey("stance_tag.id", ondelete="CASCADE"),
                    nullable=False)
    # keyword 子串 / regex 正则 / sentiment 情感映射 / laughter 纯笑声 / mock 嘲讽句式
    match_type = Column(String(16), nullable=False, server_default="keyword")
    pattern = Column(Text, nullable=False)
    weight = Column(Numeric(4, 2), nullable=False, server_default="1.0")
    negatable = Column(Boolean, nullable=False, server_default="true")
    enabled = Column(Boolean, nullable=False, server_default="true")
    note = Column(String(255))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    __table_args__ = (
        Index("idx_rule_tag", "tag_id", "enabled"),
    )


class StanceTagVersion(BaseV2):
    """标签库版本号（单行）：判定进程据此刷新内存缓存。"""

    __tablename__ = "stance_tag_version"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    version = Column(BigInteger, nullable=False, server_default="1")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())


# ---------------------------------------------------------------------------
# 14. S3 对象存储配置与媒体备份（P4）
# ---------------------------------------------------------------------------
class S3Config(BaseV2, TimestampMixin):
    """S3 兼容对象存储客户端配置（Garage / MinIO / AWS S3 通用）。

    支持多配置共存 + 启用状态管理；备份任务取 enabled 的第一条。
    SecretKey 加密落库（account.crypto，Fernet），API 下发只给掩码。
    """

    __tablename__ = "s3_config"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False, unique=True)     # 配置名，如 garage
    endpoint = Column(String(255), nullable=False)             # http://host:port
    access_key_id = Column(String(255), nullable=False)        # Key ID
    secret_cipher = Column(Text, nullable=False)               # Secret Key 密文
    region = Column(String(64), nullable=False, server_default="garage")
    bucket = Column(String(128), nullable=False, server_default="photos")
    # path / virtual：Garage 与自建 MinIO 一般用 path-style
    addressing_style = Column(String(16), nullable=False, server_default="path")
    enabled = Column(Boolean, nullable=False, server_default="true")

    # 连通性探测状态（前端展示 + 备份前自检）
    last_check_at = Column(DateTime(timezone=True))
    last_check_ok = Column(Boolean)
    last_error = Column(Text)

    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("idx_s3_config_enabled", "enabled"),
    )


class MediaBackup(BaseV2, TimestampMixin):
    """媒体备份记录：一张媒体一行，随原帖删除而联动清除（S3 对象一并删）。

    刻意不加外键：删帖时先删 S3 对象再删记录，删除失败的记录转 orphaned
    （原帖已删、对象待清理）——这种「悬空」状态必须能存在，级联外键会把它
    一起删掉，从而丢失待清理对象的线索。media_id / story_id 仅作检索字段。
    status: pending / uploading / uploaded / failed / orphaned（待清理）
    """

    __tablename__ = "media_backup"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    media_id = Column(BigInteger, nullable=False)              # 原 media_attachment.id
    story_id = Column(BigInteger)                               # 冗余：按帖子查备份
    s3_config_id = Column(BigInteger)                           # 备份到哪个配置（可悬空）

    platform = Column(String(32))
    content_id = Column(String(255))
    media_type = Column(String(16), nullable=False, server_default="image")
    # 对象键：{platform}/{media_type}/{content_id}/{filename}，保留本地目录语义
    object_key = Column(Text, nullable=False)
    # 平台远程地址（访问链第三级之外的原始出处，备份时直取的兜底源）
    source_url = Column(Text)
    mime_type = Column(String(64))
    size_bytes = Column(BigInteger)
    # 内容校验：本地文件算 sha256；远端直取时存 S3 ETag
    checksum = Column(String(64))

    # pending(待备份/重试中) / uploading / uploaded / failed(重试耗尽，待手动)
    status = Column(String(16), nullable=False, server_default="pending")
    retry_count = Column(Integer, nullable=False, server_default="0")
    max_retries = Column(Integer, nullable=False, server_default="3")
    # 指数退避：下次可重试时间（pending 且到期才会被批处理选中）
    next_retry_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    uploaded_at = Column(DateTime(timezone=True))

    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_backup_media", "media_id", unique=True),
        Index("idx_backup_status", "status", "next_retry_at"),
        Index("idx_backup_story", "story_id"),
        Index("idx_backup_platform", "platform", "status"),
    )

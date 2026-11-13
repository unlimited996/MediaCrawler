# -*- coding: utf-8 -*-
"""菜谱域数据模型（KitchenHelper）。

与 v2 故事聚合模型（database/models_v2.py）并存：这里用独立的 ``BaseRecipe``，
create_all 只作用于本文件的表，不会碰到故事/采集相关的 44 张表。

命名与约定：
- 全部表放在独立 schema ``kitchenhelper`` 下，**表名不带前缀**（靠 schema 区分，
  不再需要 recipe_ 前缀）。仍逐条显式写 schema，不依赖 search_path ——
  public 里已有同名 ``tag`` 表（故事标签），改 search_path 会让老代码串表。
- 表名：category / tag / recipe / ingredient / step / recipe_tag(关联表) / app_config
- 字段长度按 catalog.json 实测最大值留余量（如配料名最长 82 字，取 200）。
- 需要在列表里筛选/排序的字段（分类、难度、耗时）做成结构化列；
  平台/业务扩展字段放 ``ext`` JSONB，加字段不用改表。
- 关联关系：
    category 1 ──< recipe >── N recipe_tag(关联) N ──> 1 tag
                    ├──< ingredient  (UNIQUE(recipe_id, seq))
                    └──< step        (UNIQUE(recipe_id, seq))
  子表全部 ON DELETE CASCADE；分类用 RESTRICT，防止误删还有菜谱的分类。
"""
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey,
    Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import declarative_base, relationship

from .models_v2 import JsonType

#: 菜谱域独占的 PostgreSQL schema（在 imdb 库内与 public 隔离）
SCHEMA = "kitchenhelper"

#: 菜谱域独立基类：create_all 只建本文件的表（含 schema）
BaseRecipe = declarative_base()

#: 难度枚举（对应 docs/COOKING_CURRICULUM.md 的 L1~L5）
DIFFICULTY_VALUES = ("L1", "L2", "L3", "L4", "L5")

#: 菜谱状态
RECIPE_STATUS_VALUES = ("published", "draft", "archived")


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# 1. 分类
# ---------------------------------------------------------------------------
class RecipeCategory(BaseRecipe, TimestampMixin):
    """菜谱分类（早餐/炒菜/蒸菜…）。id 用英文 slug，name 是展示名。"""

    __tablename__ = "category"
    __table_args__ = {"schema": SCHEMA}

    id = Column(String(32), primary_key=True)                      # breakfast / steamed
    name = Column(String(32), nullable=False, unique=True)          # 早餐 / 蒸菜
    sort_order = Column(SmallInteger, nullable=False, server_default="0")
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    recipes = relationship("Recipe", back_populates="category", lazy="noload")


# ---------------------------------------------------------------------------
# 2. 标签
# ---------------------------------------------------------------------------
class RecipeTag(BaseRecipe, TimestampMixin):
    """菜谱标签（家常/快手/川菜/辣/高蛋白…）一个菜谱可绑多个。"""

    __tablename__ = "tag"
    __table_args__ = {"schema": SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(24), nullable=False, unique=True)          # 家常
    slug = Column(String(32), nullable=False, unique=True)          # home-style
    # manual=人工维护 / derived=从食材步骤推导 / tech=技法
    kind = Column(String(16), nullable=False, server_default="derived")
    sort_order = Column(SmallInteger, nullable=False, server_default="0")
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")


# ---------------------------------------------------------------------------
# 3. 菜谱主表
# ---------------------------------------------------------------------------
class Recipe(BaseRecipe, TimestampMixin):
    """菜谱。主键形如 ``蒸菜/酱香小河虾``（分类名/菜名），与 App 深链共用同一 id。"""

    __tablename__ = "recipe"

    id = Column(String(64), primary_key=True)
    title = Column(String(120), nullable=False)
    category_id = Column(String(32),
                         ForeignKey(f"{SCHEMA}.category.id", ondelete="RESTRICT"),
                         nullable=False)
    # 对象键而非 URL：换 CDN / 换桶只改基址，不用刷全表
    cover_key = Column(String(255))
    process_key = Column(String(255))
    intro = Column(Text)
    servings = Column(SmallInteger, nullable=False, server_default="1")
    # 估算值 = 步骤计时合计 + 每料 1 分钟备料，下限 8 分钟
    duration_minutes = Column(SmallInteger)
    difficulty = Column(String(2))
    # 「品类」型菜谱（包子/套餐）的可选规格
    variants = Column(JsonType, nullable=False, default=list)
    # 检索用冗余列：标题 + 分类 + 食材名 + 标签名，写入时重建
    search_text = Column(Text, nullable=False, server_default="")
    status = Column(String(16), nullable=False, server_default="published")
    sort_order = Column(SmallInteger, nullable=False, server_default="0")
    ext = Column(JsonType, nullable=False, default=dict)
    schema_ver = Column(Integer, nullable=False, server_default="1")

    # --- v2：对齐「菜品 / 收藏榜 / 发现」三个页面 ---
    # 每份热量（kcal），由 cooking.estimate_calories 按食材克数 + 营养表估算，可为空
    calories_kcal = Column(Integer)
    # 菜系：家常综合 / 川菜 / 粤菜 / 湘菜 / 东北菜 …（从标签推导，可人工覆盖）
    cuisine = Column(String(24))
    # 收藏总数：由 recipe_feedback 聚合维护，供「收藏榜」排序
    favorite_count = Column(Integer, nullable=False, server_default="0")
    # 被做过的次数（记食里关联到本菜谱时 +1）
    cooked_count = Column(Integer, nullable=False, server_default="0")
    # 来源：NULL=自家菜谱；非空表示「发现」页里别人贡献的菜（如「小厨」「村吕」）
    source = Column(String(32))
    # 上新时间：发现页「最新」排序用
    published_at = Column(DateTime(timezone=True))

    category = relationship("RecipeCategory", back_populates="recipes", lazy="noload")
    ingredients = relationship(
        "RecipeIngredient", back_populates="recipe", lazy="noload",
        order_by="RecipeIngredient.seq", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    steps = relationship(
        "RecipeStep", back_populates="recipe", lazy="noload",
        order_by="RecipeStep.seq", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tag_links = relationship(
        "RecipeTagLink", back_populates="recipe", lazy="noload",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("servings >= 1 AND servings <= 50",
                        name="ck_recipe_servings_range"),
        CheckConstraint("duration_minutes IS NULL OR duration_minutes > 0",
                        name="ck_recipe_duration_positive"),
        CheckConstraint(
            "difficulty IS NULL OR difficulty IN ('L1','L2','L3','L4','L5')",
            name="ck_recipe_difficulty_enum"),
        CheckConstraint(
            "status IN ('published','draft','archived')",
            name="ck_recipe_status_enum"),
        Index("idx_recipe_category", "category_id"),
        Index("idx_recipe_difficulty", "difficulty"),
        Index("idx_recipe_duration", "duration_minutes"),
        Index("idx_recipe_status", "status"),
        # 检索索引用 pg_trgm 的 GIN（中文子串匹配），由 tools/recipe_import.py
        # 以原生 SQL 建，避免 create_all 在没装 pg_trgm 的库上失败
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 4. 配料（1 菜谱 : N 行）
# ---------------------------------------------------------------------------
class RecipeIngredient(BaseRecipe):
    """配料。amount 为空表示「适量」，此时 unit 允许为空。"""

    __tablename__ = "ingredient"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recipe_id = Column(String(64),
                       ForeignKey(f"{SCHEMA}.recipe.id", ondelete="CASCADE"),
                       nullable=False)
    seq = Column(SmallInteger, nullable=False)
    name = Column(String(200), nullable=False)
    amount = Column(Numeric(10, 2))
    unit = Column(String(16))
    note = Column(String(120))

    recipe = relationship("Recipe", back_populates="ingredients", lazy="noload")

    __table_args__ = (
        UniqueConstraint("recipe_id", "seq", name="uq_ingredient_seq"),
        CheckConstraint("seq >= 1", name="ck_ingredient_seq_positive"),
        CheckConstraint("amount IS NULL OR amount > 0",
                        name="ck_ingredient_amount_positive"),
        # 有用量必须有单位，否则前端没法展示
        CheckConstraint("amount IS NULL OR unit IS NOT NULL",
                        name="ck_ingredient_unit_required"),
        Index("idx_ingredient_recipe", "recipe_id"),
        Index("idx_ingredient_name", "name"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 5. 步骤（1 菜谱 : N 行，按 seq 排序展示）
# ---------------------------------------------------------------------------
class RecipeStep(BaseRecipe):
    """制作步骤。description 保留原始文案（含「1.」序号前缀）。"""

    __tablename__ = "step"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recipe_id = Column(String(64),
                       ForeignKey(f"{SCHEMA}.recipe.id", ondelete="CASCADE"),
                       nullable=False)
    seq = Column(SmallInteger, nullable=False)
    description = Column(Text, nullable=False)
    image_key = Column(String(255))
    duration_seconds = Column(Integer)

    recipe = relationship("Recipe", back_populates="steps", lazy="noload")

    __table_args__ = (
        UniqueConstraint("recipe_id", "seq", name="uq_step_seq"),
        CheckConstraint("seq >= 1", name="ck_step_seq_positive"),
        CheckConstraint("duration_seconds IS NULL OR duration_seconds > 0",
                        name="ck_step_duration_positive"),
        Index("idx_step_recipe", "recipe_id"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 6. 菜谱 ↔ 标签（M:N）
# ---------------------------------------------------------------------------
class RecipeTagLink(BaseRecipe):
    """菜谱与标签的关联表（联合主键，两端级联删除）。"""

    __tablename__ = "recipe_tag"

    recipe_id = Column(String(64),
                       ForeignKey(f"{SCHEMA}.recipe.id", ondelete="CASCADE"),
                       primary_key=True)
    tag_id = Column(Integer,
                    ForeignKey(f"{SCHEMA}.tag.id", ondelete="CASCADE"),
                    primary_key=True)

    recipe = relationship("Recipe", back_populates="tag_links", lazy="noload")
    tag = relationship("RecipeTag", lazy="noload")

    __table_args__ = (
        Index("idx_recipe_tag_tag", "tag_id"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 7. 应用配置（KV，值为 JSON）
# ---------------------------------------------------------------------------
class AppConfig(BaseRecipe, TimestampMixin):
    """App 配置中心：一个 key 一个 JSON 值，供客户端拉取后本地缓存。

    与 s3_config 的区别：s3_config 是服务端的连接凭据；这里放的是**下发给客户端**
    的展示/开关类配置（主题、功能开关、文案、图片基址等）。
    """

    __tablename__ = "app_config"

    key = Column(String(64), primary_key=True)                      # home.banner
    value = Column(JsonType, nullable=False, default=dict)
    scope = Column(String(32), nullable=False, server_default="app")  # app / platform
    version = Column(Integer, nullable=False, server_default="1")
    description = Column(String(255))
    schema_ver = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("idx_app_config_scope", "scope"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 8. 用户反馈：收藏 / 不想吃（设备维度，无需登录）
# ---------------------------------------------------------------------------
class RecipeFeedback(BaseRecipe):
    """一道菜对一个设备的反馈。收藏数即 favorite 行数，不另存冗余计数表。

    ``device_id`` 由 App 首次启动生成并持久化，换设备视为另一个人——
    在没有账号体系前，这是最轻的做法；接入账号后把 device_id 换成 user_id 即可。
    """

    __tablename__ = "recipe_feedback"

    recipe_id = Column(String(64),
                       ForeignKey(f"{SCHEMA}.recipe.id", ondelete="CASCADE"),
                       primary_key=True)
    device_id = Column(String(64), primary_key=True)
    kind = Column(String(12), primary_key=True)      # favorite / dislike
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('favorite','dislike')", name="ck_feedback_kind"),
        Index("idx_feedback_kind_recipe", "kind", "recipe_id"),
        Index("idx_feedback_device", "device_id"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 9. 记食：一天的饭桌记录（照片 / 一句话 / 关联菜谱）
# ---------------------------------------------------------------------------
class MealLog(BaseRecipe):
    """一条饮食记录。同一天可以有早/午/晚多条（截图里「已记 0 / 3 条」）。"""

    __tablename__ = "meal_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False)
    eaten_on = Column(Date, nullable=False)
    meal = Column(String(12), nullable=False, server_default="dinner")
    note = Column(Text)
    image_key = Column(String(255))
    recipe_ids = Column(JsonType, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "meal IN ('breakfast','lunch','dinner','snack')",
            name="ck_meal_log_meal_enum"),
        Index("idx_meal_log_device_date", "device_id", "eaten_on"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 10. 我的餐桌：配好的整桌方案（最多 20 桌）
# ---------------------------------------------------------------------------
class TablePlanRecord(BaseRecipe, TimestampMixin):
    """用户保存的餐桌。``recipe_ids`` 存 [(role, recipeId)] 结构。"""

    __tablename__ = "table_plan"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False)
    title = Column(String(64))
    people = Column(SmallInteger, nullable=False, server_default="2")
    taste = Column(String(16), nullable=False, server_default="SAVORY")
    recipe_ids = Column(JsonType, nullable=False, default=list)
    is_favorite = Column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        Index("idx_table_plan_device", "device_id", "created_at"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 11. 食材营养表：算每份热量用
# ---------------------------------------------------------------------------
class IngredientNutrition(BaseRecipe):
    """按关键词匹配的千卡/100g。只做估算，够「471 kcal/份」这种展示精度。

    匹配规则：食材名包含 keyword 即命中，取最长命中（「五花肉」优先于「肉」）。
    """

    __tablename__ = "ingredient_nutrition"

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(32), nullable=False, unique=True)
    kcal_per_100g = Column(Numeric(7, 1), nullable=False)
    category = Column(String(16))

    __table_args__ = {"schema": SCHEMA}


# ---------------------------------------------------------------------------
# 12. 菜品备注：做饭页里「点备注」记自己的手感/提醒
# ---------------------------------------------------------------------------
class RecipeNote(BaseRecipe, TimestampMixin):
    """一道菜在某个设备上的一条备注。

    一个人对一道菜通常只要一条「自己的做法」备注（截图里「点备注 → 之前的备注页
    或新建备注」），所以主键是 (recipe_id, device_id)，再次保存即覆盖。
    """

    __tablename__ = "recipe_note"

    recipe_id = Column(String(64),
                       ForeignKey(f"{SCHEMA}.recipe.id", ondelete="CASCADE"),
                       primary_key=True)
    device_id = Column(String(64), primary_key=True)
    content = Column(Text, nullable=False, server_default="")

    __table_args__ = (
        Index("idx_recipe_note_device", "device_id"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 13. 买菜勾选：这一桌的采购清单谁已经买了
# ---------------------------------------------------------------------------
class ShoppingCheck(BaseRecipe):
    """一条「这个食材已买」的记录。

    按 (device_id, plan_key, item_name) 唯一。``plan_key`` 由客户端按
    「日期 + 菜谱集合」算，同一桌反复进出勾选状态不丢；换一桌就是新的 key，
    旧勾选自然作废，不会把上一桌的进度串过来。
    """

    __tablename__ = "shopping_check"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False)
    plan_key = Column(String(64), nullable=False)
    item_name = Column(String(64), nullable=False)
    checked = Column(Boolean, nullable=False, server_default="true")
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("device_id", "plan_key", "item_name",
                         name="uq_shopping_check_item"),
        Index("idx_shopping_check_plan", "device_id", "plan_key"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 14. 我的常点：外卖/常去店的个人清单
# ---------------------------------------------------------------------------
class TakeoutSpot(BaseRecipe):
    """「我的常点」里的一条：店名 + 分类 + 备注（截图 图5）。"""

    __tablename__ = "takeout_spot"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False)
    name = Column(String(64), nullable=False)
    category = Column(String(16), nullable=False, server_default="全部")
    note = Column(String(120))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("device_id", "name", name="uq_takeout_spot_name"),
        Index("idx_takeout_spot_device", "device_id"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 15. 用户档案：device_id → 昵称 + 头像 emoji（轻量账号，无密码）
# ---------------------------------------------------------------------------
class UserProfile(BaseRecipe, TimestampMixin):
    """App 设备的用户档案。身份仍然是 device_id（沿用现有体系），
    这里只补展示信息，让协作房间里能认出彼此。"""

    __tablename__ = "user_profile"

    device_id = Column(String(64), primary_key=True)
    nickname = Column(String(24), nullable=False)
    avatar = Column(String(8), nullable=False, server_default="🙂")

    __table_args__ = (
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 16. 协作配餐：房间（一桌菜 + 邀请码 + 成员 + 投票）
# ---------------------------------------------------------------------------
class CollabRoom(BaseRecipe, TimestampMixin):
    """「一起定这桌」的房间。房主创建并带初始菜品，成员凭 6 位码加入，
    对每道菜投「加/去」，房主定稿后房间关闭。

    status: open（讨论中） / decided（已定稿） / closed（房主解散）
    """

    __tablename__ = "collab_room"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(6), nullable=False, unique=True)
    host_device_id = Column(String(64), nullable=False)
    title = Column(String(48), nullable=False, server_default="这一桌")
    recipe_ids = Column(JsonType, nullable=False, default=list)
    people = Column(SmallInteger, nullable=False, server_default="2")
    status = Column(String(8), nullable=False, server_default="open")

    __table_args__ = (
        CheckConstraint("status IN ('open','decided','closed')",
                        name="ck_collab_room_status"),
        Index("idx_collab_room_host", "host_device_id"),
        {"schema": SCHEMA},
    )


class CollabMember(BaseRecipe):
    """房间成员。加入时把当时的昵称/头像快照存进来，房间展示不再依赖档案更新。"""

    __tablename__ = "collab_member"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    room_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.collab_room.id", ondelete="CASCADE"),
                     nullable=False)
    device_id = Column(String(64), nullable=False)
    nickname = Column(String(24), nullable=False)
    avatar = Column(String(8), nullable=False, server_default="🙂")
    joined_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("room_id", "device_id", name="uq_collab_member_once"),
        Index("idx_collab_member_room", "room_id"),
        {"schema": SCHEMA},
    )


class CollabVote(BaseRecipe):
    """对某道菜的表态：add（加这道）/ remove（去这道）。
    同一设备对同一道菜同 stance 只记一票；对同一道菜 add 与 remove 互斥（后投覆盖先投）。"""

    __tablename__ = "collab_vote"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    room_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.collab_room.id", ondelete="CASCADE"),
                     nullable=False)
    device_id = Column(String(64), nullable=False)
    recipe_id = Column(String(120), nullable=False)
    stance = Column(String(8), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("stance IN ('add','remove')", name="ck_collab_vote_stance"),
        UniqueConstraint("room_id", "device_id", "recipe_id", name="uq_collab_vote_dish"),
        Index("idx_collab_vote_room_dish", "room_id", "recipe_id"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 17. 反馈：用户提交 / 我的反馈（App「设置 → 意见反馈」）
# ---------------------------------------------------------------------------
class FeedbackEntry(BaseRecipe, TimestampMixin):
    """一条用户反馈。status: open（待处理）/ done（已处理）。"""

    __tablename__ = "feedback"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False)
    content = Column(String(500), nullable=False)
    status = Column(String(8), nullable=False, server_default="open")

    __table_args__ = (
        CheckConstraint("status IN ('open','done')", name="ck_feedback_status"),
        Index("idx_feedback_entry_device", "device_id", "created_at"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 18. OCR 每日用量（服务端配额，自然日=Asia/Shanghai）
# ---------------------------------------------------------------------------
class OcrUsage(BaseRecipe):
    """小票 OCR 的按日用量（服务端配额记账）。

    used_on 是东八区自然日；同一设备同一天一行，UPSERT 计数。
    客户端清 App 数据不再能重置次数（ENTITLEMENT R6）。
    """

    __tablename__ = "ocr_usage"

    device_id = Column(String(64), primary_key=True)
    used_on = Column(Date, primary_key=True)
    used_count = Column(SmallInteger, nullable=False, server_default="0")


# ---------------------------------------------------------------------------
# 19. 设备凭证（ENTITLEMENT 批次 2：HMAC 签名鉴权）
# ---------------------------------------------------------------------------
class DeviceCredential(BaseRecipe, TimestampMixin):
    """deviceId → secret 哈希。secret 明文只在注册响应里出现一次，库内只存 SHA-256。

    支持存量 UUID claim：老 App 的 deviceId 不变，只补绑 secret（零破坏迁移）。
    """

    __tablename__ = "device_credential"

    device_id = Column(String(64), primary_key=True)
    secret_hash = Column(String(64), nullable=False)
    revoked = Column(Boolean, nullable=False, server_default="false")


# ---------------------------------------------------------------------------
# 20. 成长值/柴豆流水（ENTITLEMENT 批次 3：所有权益变动的唯一事实来源）
# ---------------------------------------------------------------------------
class GrowthEvent(BaseRecipe):
    """一条发分流水。UNIQUE(device_id, event_type, ref_id) 幂等：
    meal_logged 按 clientLogId 去重（防重放刷分）；
    recipe_cooked 按 recipeId 去重（同菜二次记食不再 +5）。
    """

    __tablename__ = "growth_event"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False)
    event_type = Column(String(16), nullable=False)
    ref_id = Column(String(120), nullable=False)
    points = Column(SmallInteger, nullable=False, server_default="0")
    coins = Column(SmallInteger, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('meal_logged','recipe_cooked','coins_spend','admin_adjust')",
            name="ck_growth_event_type"),
        UniqueConstraint("device_id", "event_type", "ref_id", name="uq_growth_event_ref"),
        Index("idx_growth_event_device", "device_id", "created_at"),
        {"schema": SCHEMA},
    )


# ---------------------------------------------------------------------------
# 21. 会员汇总快照（ENTITLEMENT 批次 3：读放大优化，可由流水重建）
# ---------------------------------------------------------------------------
class MembershipState(BaseRecipe, TimestampMixin):
    """成长值/柴豆汇总。以流水为唯一事实来源，此处只是缓存的汇总。"""

    __tablename__ = "membership_state"

    device_id = Column(String(64), primary_key=True)
    points = Column(Integer, nullable=False, server_default="0")
    coins = Column(Integer, nullable=False, server_default="0")


# ---------------------------------------------------------------------------
# 22. 自定义食材台账（ENTITLEMENT 批次 5：配额在服务端"数得出来"的数据前提）
# ---------------------------------------------------------------------------
class UserIngredient(BaseRecipe, TimestampMixin):
    """用户自定义食材。云端是唯一事实来源，客户端 FridgeStore 降级为缓存。"""

    __tablename__ = "user_ingredient"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False)
    name = Column(String(64), nullable=False)
    category = Column(String(16), nullable=False, server_default="其他")
    plan = Column(String(16), nullable=False, server_default="不设提醒")
    note = Column(String(120))

    __table_args__ = (
        UniqueConstraint("device_id", "name", name="uq_user_ingredient_name"),
        Index("idx_user_ingredient_device", "device_id", "created_at"),
        {"schema": SCHEMA},
    )


#: 建表/校验时按依赖顺序使用
ALL_TABLES = (
    RecipeCategory, RecipeTag, Recipe, RecipeIngredient, RecipeStep,
    RecipeTagLink, AppConfig,
    # v2：菜品页 / 记食 / 我的餐桌
    RecipeFeedback, MealLog, TablePlanRecord, IngredientNutrition,
    # v3：做饭页备注 / 采购勾选 / 我的常点
    RecipeNote, ShoppingCheck, TakeoutSpot,
    # v4：用户档案 + 协作配餐
    UserProfile, CollabRoom, CollabMember, CollabVote,
    # v4.1：反馈（提交 / 我的反馈）
    FeedbackEntry,
    # v4.2：服务端配额记账（OCR 每日次数）
    OcrUsage,
    # v4.3：设备凭证 + 成长值流水/汇总（ENTITLEMENT 批次 2/3）
    DeviceCredential, GrowthEvent, MembershipState,
    # v4.4：自定义食材台账（ENTITLEMENT 批次 5）
    UserIngredient,
)

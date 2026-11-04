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
    BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
    Numeric, SmallInteger, String, Text, UniqueConstraint, func,
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


#: 建表/校验时按依赖顺序使用
ALL_TABLES = (
    RecipeCategory, RecipeTag, Recipe, RecipeIngredient, RecipeStep,
    RecipeTagLink, AppConfig,
)

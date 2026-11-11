# -*- coding: utf-8 -*-
"""菜谱域 v2 结构升级 + 数据回填（菜品页 / 收藏榜 / 发现 / 记食 / 我的餐桌）。

三件事，全部幂等，可重复执行：

1. **改结构**：``recipe`` 补 6 列（热量/菜系/收藏数/做过次数/来源/上新时间），
   并新建 ``recipe_feedback``、``meal_log``、``table_plan``、``ingredient_nutrition``。
   用 ``ADD COLUMN IF NOT EXISTS``，不动已有数据。
2. **灌营养表**：约 70 条常见食材的 kcal/100g，用来估算「471 kcal/份」。
3. **回填**：菜系从标签推导；热量按「食材克数 × 营养表」累加再除以份数。

用法::

    python -m tools.recipe_schema_v2 --dry-run   # 只看会做什么
    python -m tools.recipe_schema_v2             # 执行
    python -m tools.recipe_schema_v2 --report    # 只看回填结果
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from database.models_recipe import BaseRecipe, IngredientNutrition, SCHEMA

#: recipe 新增列： (列名, 类型定义)
NEW_RECIPE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("calories_kcal", "integer"),
    ("cuisine", "varchar(24)"),
    ("favorite_count", "integer NOT NULL DEFAULT 0"),
    ("cooked_count", "integer NOT NULL DEFAULT 0"),
    ("source", "varchar(32)"),
    ("published_at", "timestamptz"),
)

#: 常见食材热量（kcal / 100g）。数值取常规食物成分表的常见值，用于估算，不作为营养学依据。
NUTRITION_SEED: Tuple[Tuple[str, float, str], ...] = (
    # 油脂
    ("食用油", 899, "油脂"), ("色拉油", 899, "油脂"), ("猪油", 897, "油脂"), ("黄油", 717, "油脂"),
    # 肉类
    ("五花肉", 518, "肉类"), ("猪蹄", 260, "肉类"), ("排骨", 278, "肉类"), ("肉末", 395, "肉类"),
    ("猪肉", 395, "肉类"), ("肥肠", 191, "肉类"), ("猪肝", 129, "肉类"), ("牛腩", 332, "肉类"),
    ("牛肉", 250, "肉类"), ("羊肉", 203, "肉类"), ("鸡翅", 202, "肉类"), ("鸡腿", 181, "肉类"),
    ("鸡胸", 133, "肉类"), ("鸡肉", 167, "肉类"), ("鸭肉", 240, "肉类"), ("培根", 541, "肉类"),
    ("香肠", 508, "肉类"), ("腊肉", 498, "肉类"), ("火腿", 330, "肉类"), ("午餐肉", 229, "肉类"),
    # 水产
    ("鱼", 104, "水产"), ("虾", 93, "水产"), ("蟹", 103, "水产"), ("鱿鱼", 84, "水产"), ("贝", 60, "水产"),
    # 蛋豆
    ("鸡蛋", 143, "蛋豆"), ("豆腐", 82, "蛋豆"), ("香干", 140, "蛋豆"), ("豆干", 140, "蛋豆"),
    ("千张", 262, "蛋豆"), ("腐竹", 461, "蛋豆"), ("素鸡", 194, "蛋豆"), ("豆浆", 31, "蛋豆"),
    # 主食
    ("米饭", 116, "主食"), ("米", 346, "主食"), ("面粉", 366, "主食"), ("面条", 286, "主食"),
    ("馒头", 223, "主食"), ("红薯", 106, "主食"), ("玉米", 112, "主食"), ("燕麦", 338, "主食"),
    ("粉丝", 338, "主食"), ("粉条", 337, "主食"),
    # 蔬菜菌菇
    ("土豆", 77, "蔬菜"), ("白菜", 18, "蔬菜"), ("青菜", 15, "蔬菜"), ("菠菜", 28, "蔬菜"),
    ("西兰花", 36, "蔬菜"), ("番茄", 20, "蔬菜"), ("茄子", 23, "蔬菜"), ("黄瓜", 16, "蔬菜"),
    ("萝卜", 21, "蔬菜"), ("胡萝卜", 39, "蔬菜"), ("洋葱", 40, "蔬菜"), ("南瓜", 26, "蔬菜"),
    ("冬瓜", 12, "蔬菜"), ("苦瓜", 19, "蔬菜"), ("豆角", 34, "蔬菜"), ("芹菜", 17, "蔬菜"),
    ("莲藕", 73, "蔬菜"), ("山药", 57, "蔬菜"), ("木耳", 205, "蔬菜"), ("香菇", 26, "蔬菜"),
    ("金针菇", 32, "蔬菜"), ("平菇", 24, "蔬菜"), ("豆芽", 47, "蔬菜"), ("酸菜", 22, "蔬菜"),
    ("青椒", 22, "蔬菜"), ("尖椒", 22, "蔬菜"), ("干辣椒", 298, "蔬菜"), ("小米辣", 32, "蔬菜"),
    # 调味 / 干货
    ("糖", 400, "调味"), ("花生", 574, "调味"), ("芝麻", 559, "调味"), ("淀粉", 346, "调味"),
    ("蚝油", 114, "调味"), ("酱油", 63, "调味"), ("醋", 31, "调味"), ("豆瓣酱", 178, "调味"),
    ("辣椒酱", 120, "调味"), ("花椒", 258, "调味"), ("蒜", 128, "调味"), ("姜", 41, "调味"),
    ("葱", 30, "调味"), ("香菜", 31, "调味"),
)

#: 标签 → 菜系（顺序敏感：先匹配到的优先）
CUISINE_BY_TAG: Tuple[Tuple[str, str], ...] = (
    ("川菜", "川菜"), ("湘菜", "湘菜"), ("粤菜", "粤菜"), ("东北", "东北菜"),
    ("江浙", "江浙菜"), ("苏菜", "江浙菜"), ("西北", "西北菜"), ("鲁菜", "鲁菜"),
    ("闽菜", "闽菜"), ("徽菜", "徽菜"),
)
DEFAULT_CUISINE = "家常综合"


def _log(msg: str) -> None:
    print(f"[schema-v2] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. 结构
# ---------------------------------------------------------------------------
async def upgrade_schema(engine) -> Dict[str, Any]:
    report: Dict[str, Any] = {"added_columns": [], "created_tables": []}

    async with engine.begin() as conn:
        for name, ddl_type in NEW_RECIPE_COLUMNS:
            await conn.execute(text(
                f'ALTER TABLE "{SCHEMA}".recipe ADD COLUMN IF NOT EXISTS {name} {ddl_type}'
            ))
            report["added_columns"].append(name)

    existing = set(BaseRecipe.metadata.tables.keys())
    async with engine.begin() as conn:
        await conn.run_sync(BaseRecipe.metadata.create_all)
    report["created_tables"] = sorted(t for t in existing if t.startswith(f"{SCHEMA}."))
    return report


# ---------------------------------------------------------------------------
# 2. 营养表
# ---------------------------------------------------------------------------
async def seed_nutrition(session: AsyncSession) -> int:
    existing = {
        row[0] for row in (await session.execute(
            text(f'SELECT keyword FROM "{SCHEMA}".ingredient_nutrition'))).fetchall()
    }
    added = 0
    for keyword, kcal, category in NUTRITION_SEED:
        if keyword in existing:
            continue
        session.add(IngredientNutrition(
            keyword=keyword, kcal_per_100g=Decimal(str(kcal)), category=category
        ))
        added += 1
    await session.flush()
    return added


# ---------------------------------------------------------------------------
# 3. 回填
# ---------------------------------------------------------------------------
async def backfill_cuisine(session: AsyncSession) -> int:
    """按标签推导菜系；已经有值的（人工改过）不覆盖。"""
    rows = (await session.execute(text(f"""
        SELECT r.id, coalesce(string_agg(t.name, ','), '')
        FROM "{SCHEMA}".recipe r
        LEFT JOIN "{SCHEMA}".recipe_tag l ON l.recipe_id = r.id
        LEFT JOIN "{SCHEMA}".tag t ON t.id = l.tag_id
        WHERE r.cuisine IS NULL
        GROUP BY r.id
    """))).fetchall()

    updated = 0
    for recipe_id, tag_names in rows:
        cuisine = DEFAULT_CUISINE
        for keyword, name in CUISINE_BY_TAG:
            if keyword in (tag_names or ""):
                cuisine = name
                break
        await session.execute(
            text(f'UPDATE "{SCHEMA}".recipe SET cuisine = :c WHERE id = :i'),
            {"c": cuisine, "i": recipe_id},
        )
        updated += 1
    return updated


async def backfill_calories(session: AsyncSession) -> Dict[str, int]:
    """按「食材克数 × 营养表」估算每份热量。

    匹配规则：食材名包含 keyword 即命中，取**最长**命中（「五花肉」优先于「肉」）。
    只有单位为 g 且数量有效的食材参与统计；估不出来的保持 NULL，不编数字。
    """
    nutrition: List[Tuple[str, float]] = [
        (row[0], float(row[1])) for row in (await session.execute(
            text(f'SELECT keyword, kcal_per_100g FROM "{SCHEMA}".ingredient_nutrition')
        )).fetchall()
    ]
    # 长关键词优先，避免「肉」抢在「五花肉」前面
    nutrition.sort(key=lambda x: len(x[0]), reverse=True)

    recipes = (await session.execute(text(f"""
        SELECT r.id, r.servings, coalesce(json_agg(
                   json_build_object('name', i.name, 'amount', i.amount, 'unit', i.unit)
               ) FILTER (WHERE i.id IS NOT NULL), '[]'::json) AS items
        FROM "{SCHEMA}".recipe r
        LEFT JOIN "{SCHEMA}".ingredient i ON i.recipe_id = r.id
        GROUP BY r.id, r.servings
    """))).fetchall()

    updated = skipped = clamped = 0
    for recipe_id, servings, items in recipes:
        total = 0.0
        matched = False
        for item in items or []:
            name = item.get("name") or ""
            amount = item.get("amount")
            unit = (item.get("unit") or "").strip().lower()
            if amount is None or unit not in ("g", "克"):
                continue
            grams = float(amount)
            # 汤底/整袋米这类一次几公斤的用量，会把「每份」算成几千千卡，
            # 直接不参与统计（宁可这人不显示热量，也不给假数字）
            if grams <= 0 or grams > 1500:
                continue
            for keyword, kcal in nutrition:
                if keyword in name:
                    total += grams * kcal / 100.0
                    matched = True
                    break
        if not matched:
            skipped += 1
            # 必须清空：否则上一轮算出来的旧值（比如米饭 17300）会一直留着
            await session.execute(
                text(f'UPDATE "{SCHEMA}".recipe SET calories_kcal = NULL WHERE id = :i'),
                {"i": recipe_id},
            )
            continue

        per_serving = int(round(total / max(int(servings or 1), 1)))
        # 合理性窗口：一道菜每份 <30 或 >1200 kcal 都不像真值，置空
        if per_serving < 30 or per_serving > 1200:
            clamped += 1
            await session.execute(
                text(f'UPDATE "{SCHEMA}".recipe SET calories_kcal = NULL WHERE id = :i'),
                {"i": recipe_id},
            )
            continue
        await session.execute(
            text(f'UPDATE "{SCHEMA}".recipe SET calories_kcal = :k WHERE id = :i'),
            {"k": per_serving, "i": recipe_id},
        )
        updated += 1
    return {"updated": updated, "skipped": skipped, "out_of_range": clamped}


async def refresh_favorite_counts(session: AsyncSession) -> int:
    """收藏数按 recipe_feedback 重算（防并发漂移）。"""
    res = await session.execute(text(f"""
        UPDATE "{SCHEMA}".recipe r
        SET favorite_count = coalesce(f.cnt, 0)
        FROM (
            SELECT recipe_id, count(*) AS cnt
            FROM "{SCHEMA}".recipe_feedback
            WHERE kind = 'favorite' GROUP BY recipe_id
        ) f
        WHERE f.recipe_id = r.id AND r.favorite_count <> f.cnt
    """))
    await session.execute(text(f"""
        UPDATE "{SCHEMA}".recipe r SET favorite_count = 0
        WHERE r.favorite_count <> 0
          AND NOT EXISTS (
            SELECT 1 FROM "{SCHEMA}".recipe_feedback f
            WHERE f.recipe_id = r.id AND f.kind = 'favorite')
    """))
    return res.rowcount or 0


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
async def report(session: AsyncSession) -> None:
    rows = (await session.execute(text(f"""
        SELECT
          count(*)                                        AS total,
          count(calories_kcal)                            AS with_kcal,
          count(cuisine)                                  AS with_cuisine,
          sum(favorite_count)                             AS fav_total,
          min(calories_kcal), max(calories_kcal), round(avg(calories_kcal))
        FROM "{SCHEMA}".recipe
    """))).fetchone()
    _log(f"菜谱 {rows[0]} 道 | 有热量 {rows[1]} | 有菜系 {rows[2]} | 收藏合计 {rows[3]}")
    _log(f"热量范围 {rows[4]} ~ {rows[5]} kcal，均值 {rows[6]}")

    cuisines = (await session.execute(text(f"""
        SELECT cuisine, count(*) FROM "{SCHEMA}".recipe
        GROUP BY cuisine ORDER BY count(*) DESC
    """))).fetchall()
    _log("菜系分布：" + "，".join(f"{c or '未分类'} {n}" for c, n in cuisines))

    for table in ("recipe_feedback", "meal_log", "table_plan", "ingredient_nutrition"):
        n = (await session.execute(
            text(f'SELECT count(*) FROM "{SCHEMA}".{table}'))).scalar()
        _log(f"{table}: {n} 行")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _main_async(args: argparse.Namespace) -> int:
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        _log("当前配置不是数据库")
        return 2
    if engine.dialect.name != "postgresql":
        _log(f"只支持 postgresql（当前 {engine.dialect.name}）")
        return 2

    try:
        if args.report:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                await report(s)
            return 0

        if args.dry_run:
            _log(f"将补列：{[c for c, _ in NEW_RECIPE_COLUMNS]}")
            _log("将新建表：recipe_feedback / meal_log / table_plan / ingredient_nutrition")
            _log(f"营养表将灌入 {len(NUTRITION_SEED)} 条")
            return 0

        info = await upgrade_schema(engine)
        _log(f"结构就绪：{len(info['added_columns'])} 列 / {len(info['created_tables'])} 张表")

        async with AsyncSession(engine, expire_on_commit=False) as s:
            added = await seed_nutrition(s)
            await s.commit()
            _log(f"营养表新增 {added} 条")

            cuisine_n = await backfill_cuisine(s)
            await s.commit()
            _log(f"菜系回填 {cuisine_n} 道")

            cal = await backfill_calories(s)
            await s.commit()
            _log(f"热量回填 {cal['updated']} 道（无克数 {cal['skipped']} 道，"
                 f"超范围置空 {cal['out_of_range']} 道）")

            await refresh_favorite_counts(s)
            await s.commit()

            await report(s)
        return 0
    finally:
        await engine.dispose()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="菜谱域 v2 结构升级与回填")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    ap.add_argument("--report", action="store_true", help="只输出回填结果")
    return asyncio.run(_main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""菜谱数据入库工具（KitchenHelper → PostgreSQL）。

三件事：
1. ``ensure_schema``  建表 + 建 pg_trgm 检索索引（幂等，只补缺）
2. ``import_catalog`` 把 catalog.json 幂等导入 6 张业务表 + 重建 search_text
3. ``verify``         入库后自检（行数、外键、序号连续性、amount/unit 配对）

用法::

    python -m tools.recipe_import                 # 建表 + 导入 + 自检
    python -m tools.recipe_import --dry-run       # 只解析校验，不写库
    python -m tools.recipe_import --verify-only   # 只跑自检
    python -m tools.recipe_import --source xxx.json
    python -m tools.recipe_import --reset         # 清空菜谱域后重灌

数据源默认按顺序找：$KH_CATALOG → ../KitchenHelperAndroid/app/src/main/assets/catalog.json
→ data/recipe_catalog.json。找到哪个会打印出来。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database.models_recipe import (
    SCHEMA, AppConfig, BaseRecipe, Recipe, RecipeCategory, RecipeIngredient,
    RecipeStep, RecipeTag, RecipeTagLink,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 数据源候选（按顺序找第一个存在的）
#:   1. $KH_CATALOG（显式指定最优先）
#:   2. KitchenHelper Android 工程里由 build_catalog.py 生成的最新 catalog.json
#:   3. 后端自带的种子副本（部署机没有 Android 工程时用这份；已纳入版本管理）
SOURCE_CANDIDATES = (
    os.getenv("KH_CATALOG", ""),
    str(REPO_ROOT.parent / "KitchenHelperAndroid" / "app" / "src" / "main" / "assets" / "catalog.json"),
    str(Path(REPO_ROOT.anchor) / "KitchenHelperAndroid" / "app" / "src" / "main" / "assets" / "catalog.json"),
    str(REPO_ROOT / "database" / "seed" / "recipe_catalog.json"),
    str(REPO_ROOT / "data" / "recipe_catalog.json"),
    str(REPO_ROOT / "assets" / "catalog.json"),
)

#: 检索索引（pg_trgm，中文子串匹配；没装扩展时自动跳过）
TRGM_INDEXES = (
    ("idx_recipe_search_trgm", "recipe", "search_text"),
    ("idx_ingredient_name_trgm", "ingredient", "name"),
)


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------
async def ensure_schema(engine, *, create_extension: bool = True) -> Dict[str, Any]:
    """建 schema + 表 + 索引。返回 {pg_trgm, tables, trgm_indexes}。"""
    result: Dict[str, Any] = {"pg_trgm": False, "tables": 0, "trgm_indexes": []}

    if engine.dialect.name == "postgresql":
        async with engine.begin() as conn:
            # create_all 会连带建 schema，但某些权限受限的场景下先显式建更直观
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        if create_extension:
            async with engine.begin() as conn:
                try:
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                    result["pg_trgm"] = True
                except Exception as e:  # 没有超级用户权限时降级：功能可用，只是没索引
                    print(f"[recipe] pg_trgm 创建失败（跳过索引，查询仍可用）：{e}")

    async with engine.begin() as conn:
        await conn.run_sync(BaseRecipe.metadata.create_all)
    result["tables"] = len(BaseRecipe.metadata.tables)

    if result["pg_trgm"]:
        async with engine.begin() as conn:
            for name, table, column in TRGM_INDEXES:
                try:
                    await conn.execute(text(
                        f'CREATE INDEX IF NOT EXISTS {name} ON "{SCHEMA}".{table} '
                        f'USING gin ({column} gin_trgm_ops)'
                    ))
                    result["trgm_indexes"].append(name)
                except Exception as e:
                    print(f"[recipe] 建索引 {name} 失败：{e}")
    return result


# ---------------------------------------------------------------------------
# 解析数据源
# ---------------------------------------------------------------------------
def resolve_source(explicit: Optional[str] = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"数据源不存在：{p}")
        return p
    for cand in SOURCE_CANDIDATES:
        if cand and Path(cand).is_file():
            return Path(cand)
    raise SystemExit(
        "找不到 catalog.json。用 --source 指定，或设置环境变量 KH_CATALOG。\n"
        "候选路径：\n  " + "\n  ".join(c for c in SOURCE_CANDIDATES if c)
    )


def load_catalog(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("categories", "tags", "recipes", "ingredients", "steps"):
        if key not in data:
            raise SystemExit(f"catalog 缺少字段 {key}（来源 {path}）")
    data.setdefault("recipeTags", [])
    return data


# ---------------------------------------------------------------------------
# search_text 构建（标题 + 分类 + 食材 + 标签，去重保序）
# ---------------------------------------------------------------------------
def build_search_text(title: str, category_name: str, ingredient_names: Iterable[str],
                      tag_names: Iterable[str]) -> str:
    parts: List[str] = [title, category_name]
    parts.extend(ingredient_names)
    parts.extend(tag_names)
    seen: Dict[str, None] = {}
    for p in parts:
        p = (p or "").strip()
        if p:
            seen.setdefault(p, None)
    return " ".join(seen.keys())


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
async def import_catalog(
    session: AsyncSession,
    data: Dict[str, Any],
    *,
    replace_children: bool = True,
    reset: bool = False,
) -> Dict[str, Any]:
    """把 catalog 字典幂等写入库。

    - 分类/标签：按主键 upsert
    - 菜谱：按 id upsert，覆盖标量字段
    - 配料/步骤/标签关联：先按 recipe_id 删净再插（保证 seq 与源一致）
    - reset=True：先清空菜谱域（子表→主表→标签/分类）
    """
    stats = {"categories": 0, "tags": 0, "recipes": 0, "ingredients": 0,
             "steps": 0, "tagLinks": 0, "reset": reset,
             "created": {"categories": 0, "tags": 0, "recipes": 0}}

    if reset:
        await session.execute(delete(RecipeTagLink))
        await session.execute(delete(RecipeIngredient))
        await session.execute(delete(RecipeStep))
        await session.execute(delete(Recipe))
        await session.execute(delete(RecipeTag))
        await session.execute(delete(RecipeCategory))
        await session.flush()

    # ---- 分类 ----
    cat_rows = {c["id"]: c for c in data["categories"]}
    existing_cats = {c.id: c for c in (await session.execute(
        select(RecipeCategory))).scalars().all()}
    for cid, c in cat_rows.items():
        row = existing_cats.get(cid)
        if row is None:
            session.add(RecipeCategory(id=cid, name=c["name"],
                                       sort_order=int(c.get("sortOrder") or 0)))
            stats["created"]["categories"] += 1
        else:
            row.name = c["name"]
            row.sort_order = int(c.get("sortOrder") or 0)
    stats["categories"] = len(cat_rows)
    await session.flush()

    # ---- 标签（保留源 id，插完校正序列，避免后续自增撞主键）----
    tag_rows = {int(t["id"]): t for t in data["tags"]}
    existing_tags = {t.id: t for t in (await session.execute(
        select(RecipeTag))).scalars().all()}
    for tid, t in tag_rows.items():
        row = existing_tags.get(tid)
        if row is None:
            session.add(RecipeTag(id=tid, name=t["name"], slug=t["slug"],
                                  kind=(t.get("kind") or "derived")))
            stats["created"]["tags"] += 1
        else:
            row.name = t["name"]
            row.slug = t["slug"]
    stats["tags"] = len(tag_rows)
    await session.flush()
    if session.get_bind().dialect.name == "postgresql" and tag_rows:
        await session.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{SCHEMA}.tag','id'), "
            f"GREATEST((SELECT COALESCE(MAX(id),1) FROM {SCHEMA}.tag), 1))"
        ))

    # ---- 子表数据按 recipe 分组 ----
    ing_by_recipe: Dict[str, List[Dict[str, Any]]] = {}
    for i in data["ingredients"]:
        ing_by_recipe.setdefault(i["recipeId"], []).append(i)
    step_by_recipe: Dict[str, List[Dict[str, Any]]] = {}
    for s in data["steps"]:
        step_by_recipe.setdefault(s["recipeId"], []).append(s)
    tags_by_recipe: Dict[str, List[int]] = {}
    for rt in data["recipeTags"]:
        tags_by_recipe.setdefault(rt["recipeId"], []).append(int(rt["tagId"]))

    recipe_ids = [r["id"] for r in data["recipes"]]

    if replace_children and recipe_ids:
        # 分块删除，避免 IN 列表过长
        for chunk in _chunks(recipe_ids, 500):
            await session.execute(
                delete(RecipeIngredient).where(RecipeIngredient.recipe_id.in_(chunk)))
            await session.execute(
                delete(RecipeStep).where(RecipeStep.recipe_id.in_(chunk)))
            await session.execute(
                delete(RecipeTagLink).where(RecipeTagLink.recipe_id.in_(chunk)))
        await session.flush()

    # ---- 菜谱主表 ----
    existing_recipes = {r.id: r for r in (await session.execute(
        select(Recipe))).scalars().all()}
    new_recipes: List[Recipe] = []
    for r in data["recipes"]:
        cat = cat_rows.get(r["categoryId"])
        cat_name = cat["name"] if cat else (r.get("categoryName") or "")
        tag_names = [tag_rows[t]["name"] for t in tags_by_recipe.get(r["id"], [])
                     if t in tag_rows]
        ing_names = [i["name"] for i in ing_by_recipe.get(r["id"], [])]
        search_text = build_search_text(r["title"], cat_name, ing_names, tag_names)

        payload = dict(
            title=r["title"],
            category_id=r["categoryId"],
            cover_key=r.get("coverKey"),
            process_key=r.get("processKey"),
            intro=r.get("intro"),
            servings=int(r.get("servings") or 1),
            duration_minutes=r.get("durationMinutes"),
            difficulty=r.get("difficulty"),
            variants=r.get("variants") or [],
            search_text=search_text,
            status=r.get("status") or "published",
            sort_order=int(r.get("sortOrder") or 0),
        )
        row = existing_recipes.get(r["id"])
        if row is None:
            new_recipes.append(Recipe(id=r["id"], **payload))
            stats["created"]["recipes"] += 1
        else:
            for k, v in payload.items():
                setattr(row, k, v)
        stats["recipes"] += 1
    if new_recipes:
        session.add_all(new_recipes)
    await session.flush()

    # ---- 子表批量插入 ----
    ing_rows: List[Dict[str, Any]] = []
    for rid in recipe_ids:
        for idx, i in enumerate(sorted(ing_by_recipe.get(rid, []),
                                      key=lambda x: x["seq"]), start=1):
            ing_rows.append(dict(
                recipe_id=rid,
                seq=int(i.get("seq") or idx),
                name=i["name"],
                amount=i.get("amount"),
                unit=(i.get("unit") or ("g" if i.get("amount") is not None else None)),
                note=i.get("note"),
            ))
    step_rows: List[Dict[str, Any]] = []
    for rid in recipe_ids:
        for idx, s in enumerate(sorted(step_by_recipe.get(rid, []),
                                      key=lambda x: x["seq"]), start=1):
            step_rows.append(dict(
                recipe_id=rid,
                seq=int(s.get("seq") or idx),
                description=s["description"],
                image_key=s.get("imageKey"),
                duration_seconds=s.get("durationSeconds"),
            ))
    link_rows: List[Dict[str, Any]] = []
    for rid in recipe_ids:
        for tid in dict.fromkeys(tags_by_recipe.get(rid, [])):
            if tid in tag_rows:
                link_rows.append(dict(recipe_id=rid, tag_id=tid))

    if ing_rows:
        await session.execute(RecipeIngredient.__table__.insert(), ing_rows)
    if step_rows:
        await session.execute(RecipeStep.__table__.insert(), step_rows)
    if link_rows:
        await session.execute(RecipeTagLink.__table__.insert(), link_rows)
    stats["ingredients"] = len(ing_rows)
    stats["steps"] = len(step_rows)
    stats["tagLinks"] = len(link_rows)

    # ---- 配置中心写一条版本元信息，客户端可据此判断是否需要重新拉整包 ----
    from datetime import datetime, timezone

    meta = {
        "version": data.get("version", 1),
        "generatedAt": data.get("generatedAt"),
        "importedAt": datetime.now(timezone.utc).isoformat(),
        "counts": {k: stats[k] for k in
                   ("categories", "tags", "recipes", "ingredients", "steps", "tagLinks")},
    }
    cfg = await session.get(AppConfig, "catalog.meta")
    if cfg is None:
        session.add(AppConfig(key="catalog.meta", value=meta, scope="app",
                              description="菜谱库版本与行数（导入时自动更新）"))
    else:
        cfg.value = meta
        cfg.version = (cfg.version or 1) + 1
    return stats


def _chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
async def verify(session: AsyncSession) -> Dict[str, Any]:
    """入库自检：行数 + 三类一致性问题。"""
    async def count(model) -> int:
        return (await session.execute(select(func.count()).select_from(model))).scalar() or 0

    rep: Dict[str, Any] = {
        "counts": {
            "categories": await count(RecipeCategory),
            "tags": await count(RecipeTag),
            "recipes": await count(Recipe),
            "ingredients": await count(RecipeIngredient),
            "steps": await count(RecipeStep),
            "tagLinks": await count(RecipeTagLink),
        },
        "problems": [],
        "notes": [],
    }

    # 原生 SQL 一律带 schema 前缀（不依赖 search_path）
    q = f'"{SCHEMA}".'

    # 1) 外键悬挂：配料的 recipe_id 找不到菜谱（正常应为 0）
    orphan_ing = (await session.execute(text(
        f"select count(*) from {q}ingredient i "
        f"left join {q}recipe r on r.id = i.recipe_id where r.id is null"))).scalar()
    if orphan_ing:
        rep["problems"].append(f"配料悬挂 {orphan_ing} 行")

    # 2) 序号不连续 / 不从 1 开始
    bad_seq = (await session.execute(text(
        "select count(*) from ("
        f"  select recipe_id from {q}ingredient group by recipe_id"
        "  having max(seq) <> count(*) or min(seq) <> 1) t"))).scalar()
    if bad_seq:
        rep["problems"].append(f"{bad_seq} 个菜谱的配料序号不连续")

    bad_step_seq = (await session.execute(text(
        "select count(*) from ("
        f"  select recipe_id from {q}step group by recipe_id"
        "  having max(seq) <> count(*) or min(seq) <> 1) t"))).scalar()
    if bad_step_seq:
        rep["problems"].append(f"{bad_step_seq} 个菜谱的步骤序号不连续")

    # 3) 有用量却没单位
    bad_unit = (await session.execute(text(
        f"select count(*) from {q}ingredient "
        "where amount is not null and unit is null"))).scalar()
    if bad_unit:
        rep["problems"].append(f"{bad_unit} 行配料有 amount 缺 unit")

    # 4) 没有任何配料的菜谱：品类/套餐型（用 `## 品类` 列变体）源数据本就没有配料栏，
    #    属于正常数据形态，只提示不报错
    empty = (await session.execute(text(
        f"select count(*) from {q}recipe r where not exists "
        f"(select 1 from {q}ingredient i where i.recipe_id = r.id)"))).scalar()
    if empty:
        rep["notes"].append(f"{empty} 个菜谱没有配料（品类型菜谱，源数据如此）")

    # 5) 分类为空
    no_cat = (await session.execute(text(
        f"select count(*) from {q}recipe r left join {q}category c "
        "on c.id = r.category_id where c.id is null"))).scalar()
    if no_cat:
        rep["problems"].append(f"{no_cat} 个菜谱分类不存在")

    return rep


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _main_async(args: argparse.Namespace) -> int:
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    db_type = resolve_config_db_type()
    engine = get_async_engine(db_type)
    if engine is None:
        print(f"[recipe] 配置库类型 {db_type} 不支持（需要 postgres/mysql/sqlite）")
        return 2
    try:
        if not args.verify_only:
            sch = await ensure_schema(engine)
            print(f"[recipe] 建表完成：{sch['tables']} 张表，"
                  f"pg_trgm={sch['pg_trgm']}，索引 {sch['trgm_indexes']}")

        if args.verify_only:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                rep = await verify(s)
            print(f"[recipe] 自检：{json.dumps(rep['counts'], ensure_ascii=False)}")
            print("[recipe] 问题：" + ("无" if not rep["problems"] else "；".join(rep["problems"])))
            if rep["notes"]:
                print("[recipe] 提示：" + "；".join(rep["notes"]))
            return 0 if not rep["problems"] else 1

        src = resolve_source(args.source)
        data = load_catalog(src)
        print(f"[recipe] 数据源：{src}")
        print(f"[recipe] 解析：{len(data['recipes'])} 菜谱 / "
              f"{len(data['ingredients'])} 配料 / {len(data['steps'])} 步骤 / "
              f"{len(data['recipeTags'])} 标签关联")

        if args.dry_run:
            dup = len(data["recipes"]) - len({r["id"] for r in data["recipes"]})
            unknown_cat = {r["categoryId"] for r in data["recipes"]} - {c["id"] for c in data["categories"]}
            unknown_tag = {int(t["tagId"]) for t in data["recipeTags"]} - {int(t["id"]) for t in data["tags"]}
            print(f"[recipe] dry-run：重复 id {dup}，未知分类 {sorted(unknown_cat)}，"
                  f"未知标签 {sorted(unknown_tag)}")
            return 0 if not (dup or unknown_cat or unknown_tag) else 1

        async with AsyncSession(engine, expire_on_commit=False) as s:
            stats = await import_catalog(s, data, reset=args.reset)
            await s.commit()
            rep = await verify(s)
        print(f"[recipe] 写入：{json.dumps(stats, ensure_ascii=False)}")
        print(f"[recipe] 库内：{json.dumps(rep['counts'], ensure_ascii=False)}")
        print("[recipe] 问题：" + ("无" if not rep["problems"] else "；".join(rep["problems"])))
        if rep["notes"]:
            print("[recipe] 提示：" + "；".join(rep["notes"]))
        return 0 if not rep["problems"] else 1
    finally:
        await engine.dispose()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="菜谱数据入库（KitchenHelper）")
    ap.add_argument("--source", help="catalog.json 路径")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    ap.add_argument("--verify-only", action="store_true", help="只跑自检")
    ap.add_argument("--reset", action="store_true", help="先清空菜谱域再导入")
    args = ap.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

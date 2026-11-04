# -*- coding: utf-8 -*-
"""把菜谱域的表从 public 搬到独立 schema，并去掉 recipe_ 前缀。

背景：最初 7 张表建在 ``public`` 下、靠 ``recipe_`` 前缀避让（public 里已有
``tag`` 表用于故事标签）。后来改为独立 schema ``kitchenhelper``，表名不再需要前缀。

用 ``ALTER TABLE ... SET SCHEMA`` + ``RENAME TO`` 搬迁，**不重灌数据**；
索引/约束/序列跟着重命名，保持和 ``database/models_recipe.py`` 里的声明一致
（否则下次 create_all 会重复建索引）。

幂等：已经是新结构的库上再跑一次只会打印「无需迁移」。

用法::

    python -m tools.recipe_move_schema --dry-run   # 只看现状
    python -m tools.recipe_move_schema             # 执行迁移
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text

from database.models_recipe import SCHEMA

#: (旧表名 in public, 新表名 in SCHEMA)。顺序敏感：
#: public.recipe_tag（标签）要先腾出并改名为 tag，public.recipe_tag_link（关联）
#: 才能占用 kitchenhelper.recipe_tag 这个名字。
TABLE_MOVES: Tuple[Tuple[str, str], ...] = (
    ("recipe_category", "category"),
    ("recipe_tag", "tag"),
    ("recipe", "recipe"),
    ("recipe_ingredient", "ingredient"),
    ("recipe_step", "step"),
    ("recipe_tag_link", "recipe_tag"),
    ("app_config", "app_config"),
)

#: 索引/约束/序列的旧名 → 新名（去前缀后与新表名一致）
OBJECT_RENAMES: Tuple[Tuple[str, str, str], ...] = (
    # (类型, 旧名, 新名)  类型 = index / constraint / sequence
    ("index", "idx_recipe_ingredient_recipe", "idx_ingredient_recipe"),
    ("index", "idx_recipe_ingredient_name", "idx_ingredient_name"),
    ("index", "idx_recipe_ingredient_name_trgm", "idx_ingredient_name_trgm"),
    ("index", "idx_recipe_step_recipe", "idx_step_recipe"),
    ("index", "idx_recipe_tag_link_tag", "idx_recipe_tag_tag"),
    ("constraint", "uq_recipe_ingredient_seq", "uq_ingredient_seq"),
    ("constraint", "uq_recipe_step_seq", "uq_step_seq"),
    ("constraint", "recipe_category_pkey", "category_pkey"),
    ("constraint", "recipe_tag_pkey", "tag_pkey"),
    ("constraint", "recipe_ingredient_pkey", "ingredient_pkey"),
    ("constraint", "recipe_step_pkey", "step_pkey"),
    ("constraint", "recipe_category_name_key", "category_name_key"),
    ("constraint", "recipe_tag_name_key", "tag_name_key"),
    ("constraint", "recipe_tag_slug_key", "tag_slug_key"),
    ("sequence", "recipe_ingredient_id_seq", "ingredient_id_seq"),
    ("sequence", "recipe_step_id_seq", "step_id_seq"),
    ("sequence", "recipe_tag_id_seq", "tag_id_seq"),
    # CHECK 与外键是建表时 PG 自动命名的，同样带旧前缀，一并对齐到模型里的声明
    ("constraint", "ck_recipe_ingredient_seq_positive", "ck_ingredient_seq_positive"),
    ("constraint", "ck_recipe_ingredient_amount_positive", "ck_ingredient_amount_positive"),
    ("constraint", "ck_recipe_ingredient_unit_required", "ck_ingredient_unit_required"),
    ("constraint", "ck_recipe_step_seq_positive", "ck_step_seq_positive"),
    ("constraint", "ck_recipe_step_duration_positive", "ck_step_duration_positive"),
    ("constraint", "recipe_category_id_fkey", "recipe_category_fkey"),
    ("constraint", "recipe_ingredient_recipe_id_fkey", "ingredient_recipe_fkey"),
    ("constraint", "recipe_step_recipe_id_fkey", "step_recipe_fkey"),
    ("constraint", "recipe_tag_link_pkey", "recipe_tag_pkey"),
    ("constraint", "recipe_tag_link_recipe_id_fkey", "recipe_tag_recipe_fkey"),
    ("constraint", "recipe_tag_link_tag_id_fkey", "recipe_tag_tag_fkey"),
)


async def _exists(conn, kind: str, name: str, schema: str) -> bool:
    sql = {
        "index": "select 1 from pg_indexes where schemaname = :s and indexname = :n",
        "table": "select 1 from pg_tables where schemaname = :s and tablename = :n",
        "constraint": (
            "select 1 from pg_constraint c join pg_class t on t.oid = c.conrelid "
            "join pg_namespace ns on ns.oid = t.relnamespace "
            "where ns.nspname = :s and c.conname = :n"
        ),
        "sequence": (
            "select 1 from pg_class c join pg_namespace ns on ns.oid = c.relnamespace "
            "where ns.nspname = :s and c.relkind = 'S' and c.relname = :n"
        ),
    }[kind]
    return bool((await conn.execute(text(sql), {"s": schema, "n": name})).scalar())


async def _existing(conn, kind: str, name: str, schema: str) -> bool:
    return await _exists(conn, kind, name, schema)


async def plan(engine) -> List[Dict[str, Any]]:
    """算出要做什么，供 --dry-run 展示。"""
    steps: List[Dict[str, Any]] = []
    async with engine.connect() as conn:
        has_schema = bool((await conn.execute(text(
            "select 1 from pg_namespace where nspname = :s"), {"s": SCHEMA})).scalar())
        steps.append({"action": "create-schema" if not has_schema else "schema-exists",
                      "target": SCHEMA})
        for old, new in TABLE_MOVES:
            in_public = await _exists(conn, "table", old, "public")
            in_target = await _exists(conn, "table", new, SCHEMA)
            if in_public and not in_target:
                steps.append({"action": "move+rename", "from": f"public.{old}",
                              "to": f"{SCHEMA}.{new}"})
            elif in_target:
                steps.append({"action": "skip (already)", "target": f"{SCHEMA}.{new}"})
            else:
                steps.append({"action": "skip (missing)", "target": f"public.{old}"})
    return steps


async def migrate(engine) -> List[str]:
    """执行迁移，返回执行过的动作（便于日志/回滚参考）。"""
    done: List[str] = []
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        done.append(f'create schema if not exists "{SCHEMA}"')

        for old, new in TABLE_MOVES:
            in_public = await _exists(conn, "table", old, "public")
            in_target = await _exists(conn, "table", new, SCHEMA)
            if not in_public:
                continue
            if in_target:
                # 目标已存在：不再搬迁，避免覆盖（这种情况需要人工确认）
                print(f"[move] 跳过 public.{old}：{SCHEMA}.{new} 已存在，请人工核对")
                continue
            await conn.execute(text(
                f'ALTER TABLE public."{old}" SET SCHEMA "{SCHEMA}"'))
            if old != new:
                await conn.execute(text(
                    f'ALTER TABLE "{SCHEMA}"."{old}" RENAME TO "{new}"'))
            done.append(f"public.{old} -> {SCHEMA}.{new}")

        # 索引/约束/序列改名（不存在就跳过，故逐条 try）
        for kind, old_name, new_name in OBJECT_RENAMES:
            try:
                if kind == "index":
                    exists = await _exists(conn, "index", old_name, SCHEMA)
                    if exists and not await _exists(conn, "index", new_name, SCHEMA):
                        await conn.execute(text(
                            f'ALTER INDEX "{SCHEMA}"."{old_name}" RENAME TO "{new_name}"'))
                        done.append(f"index {old_name} -> {new_name}")
                elif kind == "constraint":
                    exists = await _exists(conn, "constraint", old_name, SCHEMA)
                    if not exists:
                        continue
                    row = (await conn.execute(text(
                        "select t.relname from pg_constraint c "
                        "join pg_class t on t.oid = c.conrelid "
                        "join pg_namespace ns on ns.oid = t.relnamespace "
                        "where ns.nspname = :s and c.conname = :n"),
                        {"s": SCHEMA, "n": old_name})).scalar()
                    if row:
                        await conn.execute(text(
                            f'ALTER TABLE "{SCHEMA}"."{row}" '
                            f'RENAME CONSTRAINT "{old_name}" TO "{new_name}"'))
                        done.append(f"constraint {old_name} -> {new_name}")
                elif kind == "sequence":
                    exists = await _exists(conn, "sequence", old_name, SCHEMA)
                    if exists and not await _exists(conn, "sequence", new_name, SCHEMA):
                        await conn.execute(text(
                            f'ALTER SEQUENCE "{SCHEMA}"."{old_name}" RENAME TO "{new_name}"'))
                        done.append(f"sequence {old_name} -> {new_name}")
            except Exception as e:
                print(f"[move] {kind} {old_name} 改名失败（忽略）：{e}")
    return done


async def _main_async(args: argparse.Namespace) -> int:
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        print("[move] 当前配置库不是数据库，无法迁移")
        return 2
    if engine.dialect.name != "postgresql":
        print(f"[move] 只支持 postgresql（当前 {engine.dialect.name}）")
        return 2
    try:
        if args.dry_run:
            for s in await plan(engine):
                print("  ", s)
            return 0

        actions = await migrate(engine)
        if not actions:
            print("[move] 无需迁移（已是目标结构）")
        else:
            print(f"[move] 完成 {len(actions)} 步：")
            for a in actions:
                print("  -", a)

        # 迁移后核对行数
        async with engine.connect() as conn:
            for _, new in TABLE_MOVES:
                if await _exists(conn, "table", new, SCHEMA):
                    n = (await conn.execute(text(
                        f'select count(*) from "{SCHEMA}"."{new}"'))).scalar()
                    print(f"  {SCHEMA}.{new}: {n} 行")
                else:
                    print(f"  {SCHEMA}.{new}: 缺失")
        return 0
    finally:
        await engine.dispose()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="菜谱表迁移到独立 schema 并去前缀")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不改库")
    args = ap.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())

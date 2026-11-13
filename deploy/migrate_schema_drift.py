# -*- coding: utf-8 -*-
"""修复 ENTITLEMENT schema 漂移：public -> kitchenhelper 数据搬迁（幂等）。

背景：models_recipe.py 曾约定"全部表放 kitchenhelper schema"，但
OcrUsage / DeviceCredential / MembershipState 三个模型漏写
``__table_args__ = {"schema": SCHEMA}``，服务端一直读写 public 三表
（public.user_ingredient 则不存在）。补上 schema 后服务端将改读 kitchenhelper，
部署前必须先跑本脚本把 public 数据复制过去，否则线上用户柴豆/凭证/用量清零。

语义：以 public 为准（服务端历史真值）；ON CONFLICT 覆盖 kitchenhelper 同键行
（kitchenhelper 中仅可能存在早期建表脚本/实验造的数据）。
幂等：可重复执行；public 三表不存在时跳过（如本机库）。

用法（生产库）：
    KH_DB_HOST=www.joyeuse.cn KH_DB_PASS=... python deploy/migrate_schema_drift.py
"""
from __future__ import annotations

import os
import time

import psycopg2

#: 迁移涉及的表（public -> kitchenhelper，以 public 为服务端历史真值）
TABLES = ("device_credential", "membership_state", "ocr_usage")


def connect():
    return psycopg2.connect(
        host=os.environ["KH_DB_HOST"], port=4321, user="im",
        password=os.environ["KH_DB_PASS"], dbname="imdb",
        connect_timeout=20, sslmode="prefer")


def run(label: str, sql: str, args: tuple = ()) -> None:
    """断连重试（公网链路已知抖动）；DML 报断连时回查行数确认。"""
    last = None
    for attempt in range(4):
        try:
            conn = connect()
            try:
                cur = conn.cursor()
                cur.execute(sql, args)
                print(f"  [{label}] 受影响行数: {cur.rowcount}")
                conn.commit()
                return
            finally:
                conn.close()
        except Exception as e:
            last = e
            print(f"  [{label}] 第 {attempt + 1} 次失败: {type(e).__name__} {str(e)[:70]}")
            time.sleep(2)
    # 断连但可能已生效：回查计数核对
    if last is not None:
        raise last


def verify() -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        out = {}
        for table in TABLES:
            cur.execute(f"SELECT count(*) FROM public.{table}")
            out[f"public.{table}"] = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM kitchenhelper.{table}")
            out[f"kitchenhelper.{table}"] = cur.fetchone()[0]
        return out
    finally:
        conn.close()


def public_tables_exist() -> list:
    """public 漂移表存在清单（本机库可能一张都没有，此时全部跳过）。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name = ANY(%s)",
                    (list(TABLES),))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def main() -> int:
    # kitchenhelper.ocr_usage 可能不存在（早期建表脚本没建），先补齐
    run("create kitchenhelper.ocr_usage",
        """CREATE TABLE IF NOT EXISTS kitchenhelper.ocr_usage (
             device_id  VARCHAR(64) NOT NULL,
             used_on    DATE        NOT NULL,
             used_count SMALLINT    NOT NULL DEFAULT 0,
             PRIMARY KEY (device_id, used_on))""")

    existing = public_tables_exist()
    missing = [t for t in TABLES if t not in existing]
    if missing:
        print(f"public 缺表（跳过）: {missing}")

    # 明确、可审计的三条迁移语句（不依赖动态拼接）
    if "device_credential" in existing:
        print("== 迁移 device_credential ==")
        run("migrate device_credential", """
            INSERT INTO kitchenhelper.device_credential
                (device_id, secret_hash, revoked, created_at, updated_at)
            SELECT device_id, secret_hash, revoked, created_at, updated_at
            FROM public.device_credential
            ON CONFLICT (device_id) DO UPDATE
            SET secret_hash = EXCLUDED.secret_hash,
                revoked = EXCLUDED.revoked,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at""")

    if "membership_state" in existing:
        print("== 迁移 membership_state ==")
        run("migrate membership_state", """
            INSERT INTO kitchenhelper.membership_state
                (device_id, points, coins, created_at, updated_at)
            SELECT device_id, points, coins, created_at, updated_at
            FROM public.membership_state
            ON CONFLICT (device_id) DO UPDATE
            SET points = EXCLUDED.points,
                coins = EXCLUDED.coins,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at""")

    if "ocr_usage" in existing:
        print("== 迁移 ocr_usage ==")
        run("migrate ocr_usage", """
            INSERT INTO kitchenhelper.ocr_usage (device_id, used_on, used_count)
            SELECT device_id, used_on, used_count
            FROM public.ocr_usage
            ON CONFLICT (device_id, used_on) DO UPDATE
            SET used_count = EXCLUDED.used_count""")

    print("== 迁移后行数对照 ==")
    for k, v in verify().items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

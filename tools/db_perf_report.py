# -*- coding: utf-8 -*-
"""数据库性能一键自查（只读，不写任何数据）。

    python -m tools.db_perf_report

输出 7 段：
    1. 建连耗时（TCP + 认证）
    2. 连续 select 1 往返
    3. 目标查询执行两次（判断是不是磁盘休眠/缓存冷）
    4. EXPLAIN ANALYZE（服务端真实执行时间）
    5. 锁等待 / 长事务（有没有东西卡住 SELECT）
    6. 表膨胀与统计信息（public + kitchenhelper）
    7. pg_stat_statements 历史慢查询 + 关键参数

判断口径：
    客户端 3s 但 EXPLAIN 的 actual time < 1ms  → 连接/网络/客户端工具的问题
    两次执行差很多（第一次慢）                → NAS 磁盘休眠或缓存冷
    两次都慢且 EXPLAIN 也慢                   → 锁、IO 或膨胀，看第 5/6 段
"""
from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config.db_config import postgres_db_config


def _url() -> str:
    c = postgres_db_config
    auth = f"{quote(str(c['user']), safe='')}:{quote(str(c['password']), safe='')}"
    return f"postgresql+asyncpg://{auth}@{c['host']}:{c['port']}/{c['db_name']}"


async def _section(conn, title: str, sql: str, limit: int = 20) -> None:
    print(f"\n=== {title} ===")
    try:
        rows = (await conn.execute(text(sql))).fetchall()
        if not rows:
            print("  (无数据)")
            return
        for row in rows[:limit]:
            print("  " + " | ".join("" if v is None else str(v) for v in row))
    except Exception as e:
        # 一条失败不能让事务挂掉，否则后续查询全部报 InFailedSQLTransaction
        try:
            await conn.rollback()
        except Exception:
            pass
        msg = str(e)
        if "pg_stat_statements must be loaded" in msg:
            print("  未启用：需要在 postgresql.conf 加 "
                  "shared_preload_libraries='pg_cron,pg_stat_statements' 后重启")
        else:
            print(f"  查询失败（可忽略）：{type(e).__name__}: {msg[:120]}")


async def main() -> int:
    c = postgres_db_config
    print(f"目标库: {c['host']}:{c['port']}/{c['db_name']}  user={c['user']}")

    t0 = time.perf_counter()
    engine = create_async_engine(_url(), echo=False, connect_args={"timeout": 20})
    try:
        conn = await engine.connect()
    except Exception as e:
        print(f"\n[失败] 建连失败（{time.perf_counter()-t0:.1f}s）：{type(e).__name__}: {e}")
        print("  → 端口不通 / 防火墙 / 网络代理。先在同机器用客户端确认这个 host:port 能连。")
        return 2

    try:
        print(f"\n① 建连(TCP+认证): {(time.perf_counter()-t0)*1000:.0f} ms")

        for i in range(4):
            t = time.perf_counter()
            await conn.execute(text("select 1"))
            print(f"② select 1 第{i+1}次: {(time.perf_counter()-t)*1000:.1f} ms")

        for i in range(3):
            t = time.perf_counter()
            rows = (await conn.execute(text("SELECT * FROM tag LIMIT 1"))).fetchall()
            print(f"③ SELECT * FROM tag LIMIT 1 第{i+1}次: "
                  f"{(time.perf_counter()-t)*1000:.1f} ms  rows={len(rows)}")

        print("\n=== ④ EXPLAIN ANALYZE: SELECT * FROM public.tag LIMIT 1 ===")
        for row in (await conn.execute(text(
                "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM public.tag LIMIT 1"))).fetchall():
            print("  " + row[0])

        await _section(conn, "⑤ 当前活动会话（wait_event_type=Lock 就是被锁住了）", """
            SELECT pid, state, wait_event_type, wait_event,
                   round(extract(epoch from now()-query_start)::numeric, 2) AS seconds,
                   left(query, 70)
            FROM pg_stat_activity
            WHERE datname = current_database() AND state <> 'idle'
            ORDER BY seconds DESC NULLS LAST
        """)

        await _section(conn, "⑤b 长事务（>1 分钟，会阻止 autovacuum）", """
            SELECT pid, round(extract(epoch from now()-xact_start)::numeric,1) AS secs,
                   state, left(query, 60)
            FROM pg_stat_activity
            WHERE xact_start IS NOT NULL AND now()-xact_start > interval '1 minute'
            ORDER BY secs DESC
        """)

        await _section(conn, "⑥ 表大小 / 死元组 / 最近 vacuum", """
            SELECT schemaname, relname, n_live_tup, n_dead_tup,
                   to_char(last_autovacuum, 'MM-DD HH24:MI') AS last_avac,
                   to_char(last_autoanalyze, 'MM-DD HH24:MI') AS last_aanal,
                   pg_size_pretty(pg_total_relation_size(relid)) AS size
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(relid) DESC
        """, 15)

        await _section(conn, "⑦ 历史慢查询 TOP10（pg_stat_statements）", """
            SELECT calls, round(mean_exec_time::numeric,2) AS avg_ms,
                   round(max_exec_time::numeric,1) AS max_ms, rows, left(query, 70)
            FROM pg_stat_statements
            ORDER BY mean_exec_time DESC
        """, 10)

        await _section(conn, "⑦b 关键参数", """
            SELECT name, setting, coalesce(unit,'') FROM pg_settings
            WHERE name IN ('shared_buffers','work_mem','effective_cache_size',
                           'random_page_cost','max_connections','synchronous_commit')
            ORDER BY name
        """)

        await _section(conn, "⑦c 数据库/schema 体积", """
            SELECT schemaname,
                   pg_size_pretty(sum(pg_total_relation_size(relid))) AS size,
                   count(*) AS tables
            FROM pg_stat_user_tables
            GROUP BY schemaname
            ORDER BY sum(pg_total_relation_size(relid)) DESC
        """)
        return 0
    finally:
        await conn.close()
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

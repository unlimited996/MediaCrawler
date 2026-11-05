-- =============================================================================
-- 慢查询自查脚本（PostgreSQL 16 / imdb 库）
--
-- 用法：用 psql 或任意客户端整段执行，按注释里的「怎么读」判断根因。
-- 核心思路：先把「SQL 本身慢」和「连接/锁/磁盘慢」分开，再对症下药。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. 先确认是不是 SQL 本身慢（最关键的一步）
--    连跑两次：第一次 3s、第二次 1ms  → 磁盘休眠 / 缓存冷，不是 SQL 的问题
--    两次都 3s                        → 看第 1、2 步
-- -----------------------------------------------------------------------------
\timing on
SELECT * FROM public.tag LIMIT 1;
SELECT * FROM public.tag LIMIT 1;

-- 服务端真实执行时间（actual time 才是 SQL 的耗时；Planning/Execution Time 也是服务端）
EXPLAIN (ANALYZE, BUFFERS, TIMING ON) SELECT * FROM public.tag LIMIT 1;

-- -----------------------------------------------------------------------------
-- 1. 锁等待：有 DDL / VACUUM FULL / 长事务时会卡住 SELECT
--    看 wait_event_type = 'Lock' 的会话，以及 granted = false 的锁申请
-- -----------------------------------------------------------------------------
SELECT pid, usename, state, wait_event_type, wait_event,
       now() - query_start AS duration, left(query, 80) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND state <> 'idle'
ORDER BY duration DESC;

SELECT c.relname, l.mode, l.granted, l.pid,
       a.state, left(a.query, 60) AS query
FROM pg_locks l
JOIN pg_class c ON c.oid = l.relation
LEFT JOIN pg_stat_activity a ON a.pid = l.pid
WHERE c.relname IN ('tag', 'story', 'recipe')
ORDER BY l.granted;

-- 长事务（会阻止 autovacuum 回收，间接导致膨胀 → 变慢）
SELECT pid, now() - xact_start AS xact_age, state, left(query, 70)
FROM pg_stat_activity
WHERE xact_start IS NOT NULL AND now() - xact_start > interval '1 minute'
ORDER BY xact_age DESC;

-- -----------------------------------------------------------------------------
-- 2. 表膨胀 / 统计信息是否过期
--    n_dead_tup 很大、last_autovacuum 很久以前 → 需要 VACUUM ANALYZE
-- -----------------------------------------------------------------------------
SELECT schemaname, relname, n_live_tup, n_dead_tup,
       last_vacuum, last_autovacuum, last_analyze, last_autoanalyze,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       seq_scan, idx_scan
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;

-- -----------------------------------------------------------------------------
-- 3. 历史慢查询（pg_stat_statements 已安装）
--    按平均耗时排序，找真正拖慢系统的语句
-- -----------------------------------------------------------------------------
SELECT calls,
       round(mean_exec_time::numeric, 2) AS avg_ms,
       round(max_exec_time::numeric, 1)  AS max_ms,
       round(total_exec_time::numeric, 0) AS total_ms,
       rows,
       left(query, 90) AS query
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 15;

-- -----------------------------------------------------------------------------
-- 4. 服务器参数与资源
-- -----------------------------------------------------------------------------
SELECT name, setting, unit
FROM pg_settings
WHERE name IN ('shared_buffers', 'work_mem', 'maintenance_work_mem',
               'effective_cache_size', 'random_page_cost', 'max_connections',
               'synchronous_commit', 'checkpoint_timeout', 'autovacuum');

SELECT checkpoints_timed, checkpoints_req,
       pg_size_pretty(buffers_checkpoint * 8192) AS written_at_checkpoint,
       pg_size_pretty(buffers_backend * 8192)     AS written_by_backend
FROM pg_stat_bgwriter;

-- 数据库总大小与各 schema 占比
SELECT schemaname,
       pg_size_pretty(sum(pg_total_relation_size(relid))) AS size,
       count(*) AS tables
FROM pg_stat_user_tables
GROUP BY schemaname
ORDER BY sum(pg_total_relation_size(relid)) DESC;

-- -----------------------------------------------------------------------------
-- 5. 定时任务是否正在跑（pg_cron 已装，备份/清理任务会抢 IO）
-- -----------------------------------------------------------------------------
SELECT jobid, jobname, schedule, active FROM cron.job;
SELECT jobid, status, start_time, end_time, left(return_message, 60)
FROM cron.job_run_details
ORDER BY start_time DESC
LIMIT 10;

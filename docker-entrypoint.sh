#!/usr/bin/env bash
# 容器入口：
#   1) 按需等待依赖的数据库/中间件端口就绪
#   2) 建好运行时目录（日志/数据/登录态，均建议由 volume 挂出来）
#   3) 按 APP_ROLE 决定是否顺带拉起抓取执行器（worker）
#   4) 前台跑主进程，并把 stdout/stderr 同时写进 /app/logs 便于挂载排障
#
# APP_ROLE：
#   crawler : 抓取服务（默认同容器再起一个 worker）
#   library : 内容库服务 + 网关（不跑 worker）
#   worker  : 只跑抓取执行器（独立容器部署时用）
#   all     : 一体化（旧行为）
set -e

APP_ROLE="${APP_ROLE:-all}"
LOG_DIR="${LOG_DIR:-/app/logs}"
LOG_FILE="$LOG_DIR/${APP_ROLE}.console.log"

wait_for_port() {
    # $1 host  $2 port  $3 timeout(sec)
    python - "$1" "$2" "$3" <<'PY'
import socket
import sys
import time

host, port, timeout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
deadline = time.time() + timeout
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            print("[entrypoint] %s:%s is up" % (host, port))
            sys.exit(0)
    except OSError as exc:
        print("[entrypoint] waiting for %s:%s ... (%s)" % (host, port, exc))
        time.sleep(2)
print("[entrypoint] ERROR: timeout waiting for %s:%s" % (host, port), file=sys.stderr)
sys.exit(1)
PY
}

mkdir -p /app/data /app/browser_data /app/database "$LOG_DIR"

if [ -n "${WAIT_FOR_DB_HOST:-}" ]; then
    wait_for_port "${WAIT_FOR_DB_HOST}" "${WAIT_FOR_DB_PORT:-5432}" "${WAIT_FOR_DB_TIMEOUT:-60}"
fi

# ---- 抓取执行器（worker）---------------------------------------------------
# crawler / all：同容器再起一个 worker（有文件锁保证全局只有一个在跑）
# library：不跑；worker：只跑它
case "$APP_ROLE" in
    crawler|all)
        if [ "${WORKER_ENABLED:-1}" = "1" ]; then
            echo "[entrypoint] starting crawl worker (role=$APP_ROLE)"
            python -m worker >>"$LOG_DIR/worker.console.log" 2>&1 &
        fi
        ;;
esac

if [ "$APP_ROLE" = "worker" ]; then
    echo "[entrypoint] worker-only mode"
    exec python -m worker
fi

echo "[entrypoint] role=$APP_ROLE service=${SERVICE_NAME:-?} port=${PORT:-?}"
echo "[entrypoint] logs -> $LOG_FILE (挂载该目录即可离线排障)"
echo "[entrypoint] starting: $*"

# 同时写文件与 stdout（docker logs 仍可用）。
# 用进程替换（bash 特性）而不是管道：这样 $! 拿到的是**应用进程**的 PID，
# 收到 TERM 时才能正确转发；换成 `cmd | tee &` 拿到的会是 tee 的 PID。
"$@" > >(tee -a "$LOG_FILE") 2>&1 &
child=$!
trap 'kill -TERM "$child" 2>/dev/null || true' TERM INT
wait "$child"

#!/usr/bin/env sh
# 容器入口：
#   1) 按需等待依赖的数据库/中间件端口就绪
#   2) 建好运行时目录
#   3) exec 交给真正的服务进程（保证 PID 1 是它，信号/docker stop 才能正常传播）
set -e

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

mkdir -p /app/data /app/browser_data /app/database

if [ -n "${WAIT_FOR_DB_HOST:-}" ]; then
    wait_for_port "${WAIT_FOR_DB_HOST}" "${WAIT_FOR_DB_PORT:-5432}" "${WAIT_FOR_DB_TIMEOUT:-60}"
fi

echo "[entrypoint] starting: $*"
exec "$@"

#!/usr/bin/env bash
# =============================================================================
# 服务器一键更新：拉取代码 -> 构建 crawler/library 两张镜像 -> 打 tag
#                 -> 登录腾讯云 CCR -> 推送 -> （可选）本机重启容器
#
# 为什么不用 git clone：国内服务器到 github.com:443 极不稳定（git ls-remote
# 常年超时），但 api.github.com 与 codeload.github.com 稳定可达。
# 所以这里用「API 取 commit sha + codeload 下 tarball + rsync 覆盖」拉代码，
# 服务器不需要 .git，也不依赖 GitHub 主站。
#
# 用法（在服务器上）：
#   mc-update                          # 完整流程
#   SKIP_PUSH=1 mc-update              # 只构建验证，不登录/不推送
#   APPLY_LOCAL=1 mc-update            # 推完顺便把本机容器换成新镜像
#   WEBUI_BUILD=0 mc-update            # 跳过前端重编（仅当 api/webui 已有产物）
#
# 凭据：从 deploy/.env 读 TENCENT_REGISTRY_USERNAME / TENCENT_REGISTRY_PASSWORD；
#       也可先手工 `docker login ccr.ccs.tencentyun.com`，脚本会复用已存凭据。
#       **不要把密码写进本脚本或提交到仓库。**
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"
SELF="$(readlink -f "$0")"
COMPOSE_FILE="docker-compose.split.yml"

GITHUB_REPO="${GITHUB_REPO:-unlimited996/MediaCrawler}"
BRANCH="${BRANCH:-main}"
REGISTRY="${REGISTRY:-ccr.ccs.tencentyun.com}"
SKIP_PUSH="${SKIP_PUSH:-0}"
APPLY_LOCAL="${APPLY_LOCAL:-0}"
WEBUI_BUILD="${WEBUI_BUILD:-1}"

log() { echo "[$(date '+%F %T')] $*"; }
die() { echo "[$(date '+%F %T')] !! $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1) 拉代码：api.github.com 取 sha -> codeload 下 tarball -> rsync 覆盖
#    运行时文件（.env / 日志 / 数据 / 浏览器登录态 / 已构建前端）不删。
# ---------------------------------------------------------------------------
sync_code() {
    log "取 ${GITHUB_REPO}@${BRANCH} 最新 commit"
    SHA="$(curl -fsSL --max-time 30 \
        "https://api.github.com/repos/${GITHUB_REPO}/commits/${BRANCH}" \
        | jq -r '.sha // empty')"
    [ -n "$SHA" ] || die "取不到最新 commit sha（api.github.com 不通？）"
    SHORT_SHA="${SHA:0:7}"
    log "目标提交 ${SHORT_SHA}"

    TMP="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$TMP'" EXIT
    log "下载源码 tarball"
    curl -fsSL --max-time 300 \
        "https://codeload.github.com/${GITHUB_REPO}/tar.gz/${SHA}" \
        -o "$TMP/src.tar.gz" || die "codeload 下载失败"
    tar xzf "$TMP/src.tar.gz" -C "$TMP"
    SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
    [ -n "$SRC" ] || die "tarball 解压失败"

    log "同步到 ${REPO_DIR}"
    # 注意 database/ 既是**代码目录**（db_session.py / models_v2.py，打进镜像）
    # 又是 sqlite 的数据目录（compose 挂载 ./database:/app/database）。
    # 早先整体 exclude 'database/' 导致里面的代码永远停留在首次同步的版本
    # （镜像里 SharedEngineHandle 缺失就是这样来的）。这里只排除数据库文件本身，
    # .py / seed/ 照常同步。
    rsync -a --delete \
        --exclude 'deploy/.env' \
        --exclude 'logs/' --exclude 'data/' --exclude 'browser_data/' \
        --exclude 'database/*.db' --exclude 'database/*.db-*' \
        --exclude 'database/*.sqlite' --exclude 'database/*.sqlite3' \
        --exclude 'api/webui/' \
        "$SRC/" "$REPO_DIR/"
    chmod +x "$REPO_DIR/deploy/server-update.sh" \
             "$REPO_DIR/deploy/build-push.sh" 2>/dev/null || true
    rm -rf "$TMP"
    trap - EXIT
}

BEFORE="$(md5sum "$SELF" | cut -d' ' -f1)"
sync_code
AFTER="$(md5sum "$SELF" | cut -d' ' -f1)"

# 脚本自身被更新 -> 用**新脚本**再跑一遍。
# 关键：重跑时也要重新 sync_code —— 刚才那次同步用的是旧脚本的排除规则，
# 新规则（比如不再整体排除 database/）必须再同步一次才生效。
# _MC_UPDATED=1 只用来防止无限循环，不跳过同步。
if [ "$BEFORE" != "$AFTER" ] && [ "${_MC_UPDATED:-0}" != "1" ]; then
    log "脚本已更新，用新脚本重新同步并继续"
    export _MC_UPDATED=1
    exec bash "$SELF" "$@"
fi

# ---------------------------------------------------------------------------
# 2) 环境文件
# ---------------------------------------------------------------------------
if [ ! -f deploy/.env ]; then
    cp deploy/.env.example deploy/.env
    chmod 600 deploy/.env
    log "已生成 deploy/.env（首次使用请填数据库与 CCR 凭据）"
fi
set -a
# shellcheck disable=SC1091
. deploy/.env
set +a
export IMAGE_TAG=latest
NAMESPACE="${TENCENT_NAMESPACE:-unlimited996}"

# ---------------------------------------------------------------------------
# 3) 构建前先校验推送凭据（别等构建 20 分钟才告诉用户没密码）
# ---------------------------------------------------------------------------
if [ "$SKIP_PUSH" != "1" ]; then
    if [ -z "${TENCENT_REGISTRY_USERNAME:-}" ] || [ -z "${TENCENT_REGISTRY_PASSWORD:-}" ]; then
        if ! grep -q "$REGISTRY" "${HOME}/.docker/config.json" 2>/dev/null; then
            die "缺少 CCR 凭据：在 deploy/.env 填 TENCENT_REGISTRY_USERNAME / TENCENT_REGISTRY_PASSWORD，或先 docker login ${REGISTRY}；只想构建验证用 SKIP_PUSH=1"
        fi
        log "复用已保存的 docker login 凭据"
    fi
fi

# ---------------------------------------------------------------------------
# 4) 构建（前端 npm build 在 Dockerfile Stage 1，只编一次，两张镜像共用）
# ---------------------------------------------------------------------------
log "构建 crawler + library（PLAYWRIGHT_BROWSER=${PLAYWRIGHT_BROWSER:-chromium}）"
SKIP_WEBUI_BUILD=$([ "$WEBUI_BUILD" = "1" ] && echo 0 || echo 1) \
    docker compose -f "$COMPOSE_FILE" --env-file deploy/.env build

CRAWLER="${REGISTRY}/${NAMESPACE}/mediacrawler-crawler"
LIBRARY="${REGISTRY}/${NAMESPACE}/mediacrawler-library"

if [ "$SKIP_PUSH" = "1" ]; then
    log "SKIP_PUSH=1：只构建，不推送"
    docker images | grep -E "mediacrawler-(crawler|library)" || true
    exit 0
fi

# ---------------------------------------------------------------------------
# 5) 登录 + 打 tag + 推送
# ---------------------------------------------------------------------------
if [ -n "${TENCENT_REGISTRY_USERNAME:-}" ] && [ -n "${TENCENT_REGISTRY_PASSWORD:-}" ]; then
    log "登录 ${REGISTRY}"
    echo "${TENCENT_REGISTRY_PASSWORD}" | docker login "${REGISTRY}" \
        --username "${TENCENT_REGISTRY_USERNAME}" --password-stdin >/dev/null
fi

# CCR 偶尔对大 blob 返回 502 Bad Gateway，且 docker push 会挂住不再输出。
# 单次上限给到 60 分钟：小带宽机器上光是 873MB 的 Python 依赖层就要传
# 20~30 分钟，设太短会在传完前掐断，重试又从头开始，永远推不上去。
# 已推过的层会命中 "Layer already exists"，重试成本很低。
push_with_retry() {
    local ref="$1"
    for attempt in 1 2 3; do
        log "push ${ref}（第 ${attempt}/3 次）"
        if timeout 3600 docker push "${ref}"; then
            return 0
        fi
        sleep 10
    done
    die "推送失败（重试 3 次）：${ref}"
}

for img in "${CRAWLER}" "${LIBRARY}"; do
    docker tag "${img}:latest" "${img}:sha-${SHORT_SHA}"
    push_with_retry "${img}:latest"
    push_with_retry "${img}:sha-${SHORT_SHA}"
done

# ---------------------------------------------------------------------------
# 6) 可选：本机把容器换成新镜像（默认关，服务一般跑在群晖/别的机器）
# ---------------------------------------------------------------------------
if [ "$APPLY_LOCAL" = "1" ]; then
    log "本机更新容器（--no-deps，不动正在跑的抓取）"
    docker compose -f "$COMPOSE_FILE" --env-file deploy/.env up -d --no-deps crawler library
fi

log "完成 @ ${SHORT_SHA}"
log "  ${CRAWLER}:latest"
log "  ${LIBRARY}:latest"
if [ "$APPLY_LOCAL" != "1" ]; then
    log "在运行服务的机器上执行：docker compose pull && docker compose up -d"
fi

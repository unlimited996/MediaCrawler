#!/usr/bin/env bash
# =============================================================================
# 服务器一键更新：拉取代码 -> 构建 crawler/library 两张镜像 -> 打 tag
#                 -> 登录腾讯云 CCR -> 推送 -> （可选）本机重启容器
#
# 用法（在服务器上）：
#   mc-update                          # 完整流程
#   SKIP_PUSH=1 mc-update              # 只构建验证，不推送（首次排障用）
#   APPLY_LOCAL=1 mc-update            # 推完顺便把本机容器换成新镜像
#   WEBUI_BUILD=0 mc-update            # 跳过前端重编（仅当 api/webui 已有产物）
#
# 凭据：从 deploy/.env 读取 TENCENT_REGISTRY_USERNAME / TENCENT_REGISTRY_PASSWORD；
#       也可先手工 `docker login ccr.ccs.tencentyun.com`，脚本会复用已存凭据。
#       **不要把密码写进本脚本或提交到仓库。**
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"
SELF="$(readlink -f "$0")"
COMPOSE_FILE="docker-compose.split.yml"

REGISTRY="${REGISTRY:-ccr.ccs.tencentyun.com}"
SKIP_PUSH="${SKIP_PUSH:-0}"
APPLY_LOCAL="${APPLY_LOCAL:-0}"
WEBUI_BUILD="${WEBUI_BUILD:-1}"

log() { echo "[$(date '+%F %T')] $*"; }
die() { echo "[$(date '+%F %T')] !! $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1) 拉代码。脚本自身若被更新，重新执行一遍，避免用旧逻辑跑新代码。
# ---------------------------------------------------------------------------
log "拉取最新代码：git pull --ff-only ($REPO_DIR)"
BEFORE="$(md5sum "$SELF" | cut -d' ' -f1)"
git fetch --all --prune
git pull --ff-only || die "git pull 失败（服务器上有本地改动？先 git status 检查，别覆盖）"
AFTER="$(md5sum "$SELF" | cut -d' ' -f1)"
if [ "$BEFORE" != "$AFTER" ]; then
    log "脚本已被更新，重新执行"
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
# 脚本推送统一用 :latest + :sha-xxx，避免 .env 里 IMAGE_TAG 覆盖造成 tag 漂移
export IMAGE_TAG=latest
NAMESPACE="${TENCENT_NAMESPACE:-unlimited996}"

# ---------------------------------------------------------------------------
# 3) 构建前先校验推送凭据（别等构建 20 分钟才告诉用户没密码）
# ---------------------------------------------------------------------------
if [ "$SKIP_PUSH" != "1" ]; then
    if [ -z "${TENCENT_REGISTRY_USERNAME:-}" ] || [ -z "${TENCENT_REGISTRY_PASSWORD:-}" ]; then
        if ! grep -q "$REGISTRY" "${HOME}/.docker/config.json" 2>/dev/null; then
            die "缺少 CCR 凭据：请在 deploy/.env 填 TENCENT_REGISTRY_USERNAME / TENCENT_REGISTRY_PASSWORD，或先 docker login $REGISTRY；只想构建验证用 SKIP_PUSH=1"
        fi
        log "复用已保存的 docker login 凭据"
    fi
fi

# ---------------------------------------------------------------------------
# 4) 构建（前端 npm build 在 Dockerfile Stage 1，只编一次，两张镜像共用）
# ---------------------------------------------------------------------------
log "构建 crawler + library 镜像（PLAYWRIGHT_BROWSER=${PLAYWRIGHT_BROWSER:-chromium}）"
SKIP_WEBUI_BUILD=$([ "$WEBUI_BUILD" = "1" ] && echo 0 || echo 1) \
    docker compose -f "$COMPOSE_FILE" --env-file deploy/.env build

SHORT_SHA="$(git rev-parse --short=7 HEAD)"
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

for img in "${CRAWLER}" "${LIBRARY}"; do
    docker tag "${img}:latest" "${img}:sha-${SHORT_SHA}"
    log "push ${img}:latest"
    docker push "${img}:latest"
    log "push ${img}:sha-${SHORT_SHA}"
    docker push "${img}:sha-${SHORT_SHA}"
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

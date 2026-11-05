#!/usr/bin/env bash
# 本地构建并推送两个镜像（GitHub Actions 不可用时的兜底通道）。
#
# 用法：
#   export TENCENT_REGISTRY_USERNAME=1000xxxxxxx
#   export TENCENT_REGISTRY_PASSWORD=********
#   ./deploy/build-push.sh                 # 构建 + 推送 latest 与 sha-xxxxxxx
#   TAG=v1.0.0 ./deploy/build-push.sh      # 额外打版本 tag
#   PUSH=0 ./deploy/build-push.sh          # 只构建不推送
#   PLAYWRIGHT_BROWSER=chromium-headless-shell ./deploy/build-push.sh
#                                          # 轻量镜像（省约 600MB，跨境推送慢时用）
#
# 说明：构建会走 Dockerfile 的 Stage 1（npm build 前端），所以镜像自带界面；
#       两张镜像共用同一份前端与依赖层，第二次构建几乎全命中缓存。
set -euo pipefail

cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-ccr.ccs.tencentyun.com}"
NAMESPACE="${TENCENT_NAMESPACE:-unlimited996}"
PUSH="${PUSH:-1}"
TAG="${TAG:-}"
COMPOSE_FILE="docker-compose.split.yml"

SHORT_SHA="$(git rev-parse --short=7 HEAD 2>/dev/null || echo dev)"
CRAWLER="${REGISTRY}/${NAMESPACE}/mediacrawler-crawler"
LIBRARY="${REGISTRY}/${NAMESPACE}/mediacrawler-library"

echo "==> 构建（crawler + library，含前端 npm build）"
docker compose -f "${COMPOSE_FILE}" build

if [ "${PUSH}" != "1" ]; then
    echo "==> PUSH=0，跳过推送。本地镜像："
    docker images | grep -E "mediacrawler-(crawler|library)" || true
    exit 0
fi

if [ -z "${TENCENT_REGISTRY_USERNAME:-}" ] || [ -z "${TENCENT_REGISTRY_PASSWORD:-}" ]; then
    echo "!! 缺少 TENCENT_REGISTRY_USERNAME / TENCENT_REGISTRY_PASSWORD" >&2
    echo "   若本机已 docker login ${REGISTRY}，可直接 export 两个变量为任意非空值跳过此检查。" >&2
    exit 1
fi

echo "==> 登录 ${REGISTRY}"
echo "${TENCENT_REGISTRY_PASSWORD}" | docker login "${REGISTRY}" \
    --username "${TENCENT_REGISTRY_USERNAME}" --password-stdin

for img in "${CRAWLER}" "${LIBRARY}"; do
    docker tag "${img}:latest" "${img}:sha-${SHORT_SHA}"
    echo "==> push ${img}:latest  ${img}:sha-${SHORT_SHA}"
    docker push "${img}:latest"
    docker push "${img}:sha-${SHORT_SHA}"
    if [ -n "${TAG}" ]; then
        docker tag "${img}:latest" "${img}:${TAG}"
        docker push "${img}:${TAG}"
        echo "    + ${img}:${TAG}"
    fi
done

echo "==> 完成"
echo "    ${CRAWLER}:latest"
echo "    ${LIBRARY}:latest"

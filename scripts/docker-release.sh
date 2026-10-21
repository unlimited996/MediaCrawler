#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# MediaCrawler 镜像构建 / 推送 / 离线导出
#
#   ./scripts/docker-release.sh 1.0.0              # 构建并打 1.0.0 + latest
#   ./scripts/docker-release.sh 1.0.0 --push       # 推到 REGISTRY
#   ./scripts/docker-release.sh 1.0.0 --save       # 导出 tar.gz 给离线环境
#   ./scripts/docker-release.sh                    # 不传版本则用 git describe
#
# 环境变量：
#   REGISTRY      镜像仓库前缀，如 registry.example.com/mediacrawler（配了才能 --push）
#   SKIP_WEBUI_BUILD=1   跳过容器内前端构建，沿用宿主机 api/webui
# -----------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="${1:-}"
PUSH=0
SAVE=0
for arg in "$@"; do
    case "$arg" in
        --push) PUSH=1 ;;
        --save) SAVE=1 ;;
    esac
done

if [ -z "$VERSION" ]; then
    VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
    VERSION="${VERSION#v}"
fi

IMAGE="${REGISTRY:-mediacrawler}"
echo "[release] image  = ${IMAGE}"
echo "[release] version= ${VERSION}"

echo "[release] building ..."
docker build \
    --build-arg "SKIP_WEBUI_BUILD=${SKIP_WEBUI_BUILD:-0}" \
    -t "${IMAGE}:${VERSION}" \
    -t "${IMAGE}:latest" \
    .

echo "[release] built ${IMAGE}:${VERSION} and ${IMAGE}:latest"

if [ "$PUSH" = "1" ]; then
    if [ -z "${REGISTRY:-}" ]; then
        echo "[release] ERROR: --push 需要设置 REGISTRY，例如："
        echo "          REGISTRY=registry.example.com/mediacrawler $0 $VERSION --push"
        exit 1
    fi
    docker push "${IMAGE}:${VERSION}"
    docker push "${IMAGE}:latest"
    echo "[release] pushed"
fi

if [ "$SAVE" = "1" ]; then
    OUT="mediacrawler-${VERSION}.tar.gz"
    docker save "${IMAGE}:${VERSION}" | gzip > "$OUT"
    echo "[release] saved -> ${OUT}"
    echo "          目标机器还原： docker load -i ${OUT}"
fi

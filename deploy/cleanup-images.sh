#!/usr/bin/env bash
# 构建前清理「不再需要的旧版项目镜像」，释放磁盘空间。
#
# 只碰本项目仓库前缀 ${REGISTRY}/${NAMESPACE}/mediacrawler-* ：
#   * 保留 :latest（下次构建的基础缓存，也是 compose 正在用的标签）
#   * 保留任何被容器（含已停止容器）引用的镜像
#   * 删除其余旧 tag（历史 sha-* / 手工版本 tag）
#   * 删除 dangling 层（无 tag、无容器引用的构建残留）
# 其它项目的镜像一律不动。
#
# 用法：bash deploy/cleanup-images.sh [registry] [namespace]
set -uo pipefail

REGISTRY="${1:-ccr.ccs.tencentyun.com}"
NAMESPACE="${2:-unlimited996}"
PREFIX="${REGISTRY}/${NAMESPACE}/mediacrawler-"

log() { echo "[$(date '+%F %T')] [cleanup] $*"; }

if ! command -v docker >/dev/null 2>&1; then
    log "docker 不可用，跳过清理"
    exit 0
fi

before="$(df -h / | awk 'NR==2 {print $4}')"
log "清理前 / 可用：${before}"

# 正在被容器使用的镜像 ID（sha256:...），一个都不能删
used_ids="$(docker ps -aq 2>/dev/null | while read -r cid; do
    [ -n "$cid" ] || continue
    docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true
done | sort -u)"

removed=0
skipped=0
while read -r repo tag id; do
    [ -n "${id:-}" ] || continue
    case "$repo" in "$PREFIX"*) ;; *) continue ;; esac
    # latest 保留：构建缓存基础，且部署侧 compose 固定引用它
    [ "$tag" = "latest" ] && continue
    if printf '%s\n' "$used_ids" | grep -qF "$id"; then
        log "跳过（容器仍在引用）${repo}:${tag}"
        skipped=$((skipped + 1))
        continue
    fi
    if docker rmi "${repo}:${tag}" >/dev/null 2>&1; then
        log "删除旧版 ${repo}:${tag}"
        removed=$((removed + 1))
    fi
done < <(docker images --no-trunc --format '{{.Repository}} {{.Tag}} {{.ID}}' 2>/dev/null)

# <none> 层没有仓库名，无法按前缀区分；它们必然是「无 tag 且无容器引用」的
# 构建残留，docker image prune 也只认这一类，不会碰到任何在用/有标签的镜像。
pruned="$(docker image prune -f --filter 'dangling=true' 2>/dev/null \
          | tail -1 || true)"

after="$(df -h / | awk 'NR==2 {print $4}')"
log "移除旧 tag ${removed} 个，跳过在用 ${skipped} 个；${pruned:-悬空层已处理}"
log "清理后 / 可用：${after}"

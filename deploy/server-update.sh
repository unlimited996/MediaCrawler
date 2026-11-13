#!/usr/bin/env bash
# =============================================================================
# 服务器一键更新：拉取代码 -> 构建 crawler/library 两张镜像 -> 推送 latest
#                 -> 登录腾讯云 CCR -> 推送 -> （可选）本机重启容器
#
# 为什么不用 git clone：国内服务器到 github.com:443 极不稳定（git ls-remote
# 常年超时），但 api.github.com 与 codeload.github.com 稳定可达。
# 所以这里用「API 取 commit sha + codeload 下 tarball + rsync 覆盖」拉代码，
# 服务器不需要 .git，也不依赖 GitHub 主站。
#
# 用法（在服务器上）：
#   mc-update                          # 完整流程（按变更范围自动决定建哪张镜像）
#   FORCE_ALL=1 mc-update              # 忽略变更检测，强制重建两张镜像
#   SKIP_PUSH=1 mc-update              # 只构建验证，不登录/不推送
#   APPLY_LOCAL=1 mc-update            # 推完顺便把本机容器换成新镜像
#   WEBUI_BUILD=0 mc-update            # 跳过前端重编（仅当 api/webui 已有产物）
#
# 按需构建：脚本记录上次构建的 commit（.build-state.json），用 GitHub compare
# 接口拿到两次提交之间的文件清单，判断哪张镜像真的需要重建：
#   * 只改 webui/docs/library 专属接口 -> 只建 library（crawler 不动）
#   * 只改爬虫专属（media_platform/store/...）-> 只建 crawler
#   * 改了共享模块（tools/config/database/...）或说不清 -> 两张都建
#   * commit 没变 -> 直接跳过，不构建也不推送
# 想强制全量（例如改了 deploy/.env 里的构建参数）就 FORCE_ALL=1。
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
STATE_FILE="$REPO_DIR/.build-state.json"

GITHUB_REPO="${GITHUB_REPO:-unlimited996/MediaCrawler}"
BRANCH="${BRANCH:-main}"
REGISTRY="${REGISTRY:-ccr.ccs.tencentyun.com}"
SKIP_PUSH="${SKIP_PUSH:-0}"
APPLY_LOCAL="${APPLY_LOCAL:-0}"
WEBUI_BUILD="${WEBUI_BUILD:-1}"
FORCE_ALL="${FORCE_ALL:-0}"

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
        --exclude '.build-state.json' \
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
# 4) 决定构建哪张镜像（按变更范围）
#
# 判据：GitHub compare 接口给出两次提交之间改动的文件清单。
#   * 「不影响 crawler」的白名单：前端产物、文档、部署脚本、library 专属接口
#   * 「只影响 crawler」的白名单：媒体平台/存储层/爬虫接口与服务
# 白名单之外的（tools/、config/、database/、requirements、Dockerfile…）
# 一律视为共享变更 -> 两张都重建，宁可多建也不让镜像版本错配。
# ---------------------------------------------------------------------------
CRAWLER="${REGISTRY}/${NAMESPACE}/mediacrawler-crawler"
LIBRARY="${REGISTRY}/${NAMESPACE}/mediacrawler-library"

# 改了这些文件不需要重建 crawler（crawler 既不提供前端，也不挂这些路由）
CRAWLER_IGNORABLE='^(webui/|docs/|deploy/|\.github/|[^/]*\.md$|api/webui/|api/library_app\.py$|api/gateway\.py$|api/routers/(story|data|discover|export|stance_tags|s3_backup|recipe|media|app_config|internal)\.py$|requirements-core\.txt$)'
# 改了这些文件不需要重建 library
LIBRARY_IGNORABLE='^(media_platform/|store/|api/routers/(crawler|accounts|scheduler)\.py$|api/crawler_app\.py$|api/services/(profile_crawler|task_scheduler|crawler_manager|crawl_queue|worker_control|crawl_control)\.py$|worker\.py$|main\.py$)'
# 纯文档/CI 改动：镜像内容确实会变（COPY .），但功能完全不受影响，直接跳过
DOC_ONLY='^(docs/|[^/]*\.md$|\.github/)'

BUILD_CRAWLER=1
BUILD_LIBRARY=1

github_compare() {
    # 输出两提交之间改动的文件名（每行一个）；失败返回非 0
    curl -fsSL --max-time 30 \
        "https://api.github.com/repos/${GITHUB_REPO}/compare/$1...$2" \
        | jq -r '.files[]?.filename // empty'
}

if [ "$FORCE_ALL" = "1" ]; then
    log "FORCE_ALL=1：强制重建两张镜像"
elif [ -f "$STATE_FILE" ]; then
    PREV_SHA="$(jq -r '.sha // empty' "$STATE_FILE" 2>/dev/null || true)"
    if [ -n "$PREV_SHA" ] && [ "$PREV_SHA" = "$SHA" ]; then
        log "提交 ${SHORT_SHA} 已经是当前构建版本，跳过构建与推送"
        log "（改了 deploy/.env 里的构建参数请用 FORCE_ALL=1 mc-update）"
        exit 0
    fi
    if [ -n "$PREV_SHA" ]; then
        CHANGED="$(mktemp)"
        if github_compare "$PREV_SHA" "$SHA" > "$CHANGED" 2>/dev/null; then
            if [ ! -s "$CHANGED" ]; then
                # compare 成功但没有文件差异（例如空提交）
                log "两次提交之间没有文件变化，跳过构建"
                BUILD_CRAWLER=0
                BUILD_LIBRARY=0
            else
                TOTAL="$(wc -l < "$CHANGED" | tr -d ' ')"
                log "自 ${PREV_SHA:0:7} 以来改动 ${TOTAL} 个文件，判定需要重建的镜像"
                if ! grep -qvE "$DOC_ONLY" "$CHANGED"; then
                    BUILD_CRAWLER=0
                    BUILD_LIBRARY=0
                    log "只有文档/CI 改动，镜像无需重建"
                else
                    if ! grep -qvE "$CRAWLER_IGNORABLE" "$CHANGED"; then
                        BUILD_CRAWLER=0
                    fi
                    if ! grep -qvE "$LIBRARY_IGNORABLE" "$CHANGED"; then
                        BUILD_LIBRARY=0
                    fi
                fi
            fi
        else
            log "变更清单获取失败，保守起见重建两张镜像"
        fi
        rm -f "$CHANGED"
    fi
fi

TARGETS=""
[ "$BUILD_CRAWLER" = "1" ] && TARGETS="crawler"
[ "$BUILD_LIBRARY" = "1" ] && TARGETS="${TARGETS:+$TARGETS }library"
if [ -z "$TARGETS" ]; then
    log "本次改动不影响任何镜像，跳过构建与推送"
    exit 0
fi
log "本次构建：${TARGETS}（PLAYWRIGHT_BROWSER=${PLAYWRIGHT_BROWSER:-chromium}）"

# ---------------------------------------------------------------------------
# 4.5) 构建前清理旧版项目镜像：只删本项目、非 latest、非在用的旧 tag，
#      释放磁盘后再构建。失败不阻断构建（磁盘紧张时只是少回收一点）。
# ---------------------------------------------------------------------------
bash deploy/cleanup-images.sh "$REGISTRY" "$NAMESPACE" \
    || log "构建前清理未成功，继续构建"

# ---------------------------------------------------------------------------
# 5) 构建
# ---------------------------------------------------------------------------
SKIP_WEBUI_BUILD=$([ "$WEBUI_BUILD" = "1" ] && echo 0 || echo 1) \
    docker compose -f "$COMPOSE_FILE" --env-file deploy/.env build $TARGETS

# 构建成功即记录，避免下次同 commit 重复构建。
# 注意：SKIP_PUSH 也照样记录 —— 状态文件的含义是「这个 commit 已构建过」，
# 与有没有推到镜像仓库无关。
write_state() {
    cat > "$STATE_FILE" <<EOF
{"sha":"${SHA}","short_sha":"${SHORT_SHA}","built_at":"$(date -Iseconds)","targets":"${TARGETS}","pushed":"$1"}
EOF
}

if [ "$SKIP_PUSH" = "1" ]; then
    write_state "0"
    log "SKIP_PUSH=1：只构建，不推送"
    docker images | grep -E "mediacrawler-(crawler|library)" || true
    exit 0
fi

# 推送：只推 latest 这一个标签（部署侧 compose 全部写死 :latest）。
# 早期版本会额外打 :sha-xxxxxxx 便于回滚，但 CCR 控制台会把两个标签并列展示，
# 容易被误认为「跑的不是 latest」；且两个 tag 指向同一 manifest，单独删 sha
# 标签反而会破坏 latest。按需求统一只保留 latest，commit 溯源交给
# .build-state.json 与构建日志。
# ---------------------------------------------------------------------------
# 6) 登录 + 推送 latest（只推本次构建过的）
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

PUSHED=()
if [ "$BUILD_CRAWLER" = "1" ]; then
    push_with_retry "${CRAWLER}:latest"
    PUSHED+=("${CRAWLER}")
fi
if [ "$BUILD_LIBRARY" = "1" ]; then
    push_with_retry "${LIBRARY}:latest"
    PUSHED+=("${LIBRARY}")
fi

# 推送完成：更新状态（pushed=1），下次据此判断该不该重建
write_state "1"

# ---------------------------------------------------------------------------
# 7) 可选：本机把容器换成新镜像（默认关，服务一般跑在群晖/别的机器）
# ---------------------------------------------------------------------------
if [ "$APPLY_LOCAL" = "1" ]; then
    log "本机更新容器（--no-deps，不动正在跑的抓取）"
    docker compose -f "$COMPOSE_FILE" --env-file deploy/.env up -d --no-deps $TARGETS
fi

log "完成 @ ${SHORT_SHA}（本次构建：${TARGETS}）"
for img in ${PUSHED[@]+"${PUSHED[@]}"}; do
    log "  ${img}:latest"
done
if [ "$APPLY_LOCAL" != "1" ]; then
    log "在运行服务的机器上执行：docker compose pull && docker compose up -d"
fi

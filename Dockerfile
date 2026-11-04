# =============================================================================
# MediaCrawler 镜像（多目标构建）
#
#   Stage 1  webui-builder : Node 构建 React 前端 -> /app/api/webui
#   Stage 2  runtime       : Python 3.11 + Node(execjs) + Chromium + FastAPI
#   Stage 3+ 按服务拆分的三个目标（同一份代码，不同启动命令）：
#            crawler : 抓取服务    /crawler/api/**  （容器内同时拉起 worker）
#            library : 内容库服务  /library/api/** + 网关（按服务名转发 /crawler/**）
#            all     : 一体化（旧行为，本地/单机用；也是默认目标）
#
# 构建（推荐用 buildx 一次构建两个服务镜像）：
#   docker build --target crawler -t mediacrawler-crawler:latest .
#   docker build --target library -t mediacrawler-library:latest .
#   docker build -t mediacrawler:latest .            # = 默认 all 目标
# 跳过前端重新构建（沿用宿主机已构建好的 api/webui，省 1~2 分钟）：
#   docker build --target library --build-arg SKIP_WEBUI_BUILD=1 -t mediacrawler-library .
# 更多： docs/DEPLOY_SPLIT.md
# =============================================================================

# -----------------------------------------------------------------------------
# 全局构建参数
#
# DOCKER_MIRROR : 基础镜像源前缀。默认 docker.io/（官方），国内拉不动 Docker Hub 时
#                 改成国内加速源，例如：
#                   docker build --build-arg DOCKER_MIRROR=docker.m.daocloud.io/ .
# NPM_REGISTRY  : Stage 1 装 npm 包用的源（默认淘宝镜像，官方源在国内经常连不上）
# PIP_INDEX_URL : Python 包源，留空则用官方 PyPI
# -----------------------------------------------------------------------------
ARG DOCKER_MIRROR=docker.io/
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PIP_INDEX_URL=

# -----------------------------------------------------------------------------
# Stage 1：构建 React 前端
#
# 注意 vite.config.ts 里 outDir 是 '../api/webui'，是相对 webui/ 根目录往外一层，
# 所以这里必须按项目原本的目录层级布局（/app/webui + /app/api），
# 不能把 WORKDIR 设成 webui 自己，否则产物会落到镜像根目录之外去。
# -----------------------------------------------------------------------------
FROM ${DOCKER_MIRROR}node:22-alpine AS webui-builder

ARG SKIP_WEBUI_BUILD=0
ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

COPY webui/package.json webui/package-lock.json ./webui/

# 挂载 npm 缓存，二次构建会快很多
RUN --mount=type=cache,target=/root/.npm \
    if [ "$SKIP_WEBUI_BUILD" = "1" ]; then \
        echo "[webui] SKIP_WEBUI_BUILD=1, skip npm ci"; \
    else \
        cd /app/webui && npm ci --no-audit --no-fund --registry="$NPM_REGISTRY"; \
    fi

COPY webui/ ./webui/

RUN --mount=type=cache,target=/root/.npm \
    if [ "$SKIP_WEBUI_BUILD" = "1" ]; then \
        echo "[webui] SKIP_WEBUI_BUILD=1, skip npm run build (沿用宿主机已构建产物)"; \
        mkdir -p /app/api/webui; \
    else \
        cd /app/webui && npm run build; \
    fi


# -----------------------------------------------------------------------------
# Stage 2：运行时（两个服务镜像共用）
# -----------------------------------------------------------------------------
FROM ${DOCKER_MIRROR}python:3.11-slim AS runtime

# PYTHONUNBUFFERED   : 容器内实时看到爬虫子进程日志（WebSocket 推送也依赖它）
# PLAYWRIGHT_*       : 浏览器统一装到 /ms-playwright；root 运行必须关沙箱
# HEADLESS           : 容器内没有图形界面，必须无头（配置文件里已支持环境变量覆盖）
# TZ                 : 时间统一东八区，日志时间才对得上
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_NO_SANDBOX=1 \
    HEADLESS=true

WORKDIR /app

# 1) 系统依赖
#    nodejs        : pyexecjs 靠它执行 libs/*.js（抖音、知乎的签名脚本），
#                    缺了 node 会在 import media_platform.douyin.help 时直接崩
#    libgl1 等     : opencv-python / matplotlib 运行时动态库
#    fonts-wqy-*   : 词云与图表里的中文不变成方块
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs \
        bash \
        curl \
        ca-certificates \
        tzdata \
        libglib2.0-0 \
        libgl1 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libatspi2.0-0 \
        fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 2) Python 依赖
#    --retries/--timeout：pandas(13M)、opencv(~65M)、playwright(~40M) 这些大轮子
#    在弱网下会被掐断连接，默认重试 5 次不够，这里放宽。
COPY requirements.txt ./
ARG PIP_INDEX_URL=
RUN PIP_ARGS="--no-cache-dir --retries 10 --timeout 120"; \
    if [ -n "$PIP_INDEX_URL" ]; then PIP_ARGS="$PIP_ARGS -i $PIP_INDEX_URL"; fi; \
    pip install $PIP_ARGS -r requirements.txt

# 3) Playwright 浏览器（--with-deps 会补齐 Chromium 缺的系统库）
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# 4) 应用源码
COPY . /app

# 5) 前端产物：步骤 4 已带上宿主机构建结果，这里用 Stage 1 的产物覆盖，
#    保证镜像自包含（不依赖宿主机有没有跑过 npm run build）。
#    SKIP_WEBUI_BUILD=1 时 Stage 1 只是个空目录，COPY 会合并而非清空，
#    所以宿主机的 api/webui 仍然生效。
COPY --from=webui-builder /app/api/webui/ /app/api/webui/

# 6) 运行时目录
#    /app/logs          -> 应用日志（worker.out.log / scheduler_live.jsonl /
#                          crawler.console.log），建议挂出来排障
#    /app/data          -> 抓取产物
#    /app/browser_data  -> 浏览器登录态（必须持久化，否则每起重扫码）
#    /app/database      -> sqlite / 导出
#    /app/deploy        -> 外部挂进来的环境变量文件目录
RUN mkdir -p /app/data /app/browser_data /app/database /app/logs /app/deploy \
    && chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]


# -----------------------------------------------------------------------------
# 目标 1：crawler 服务（抓取 + 调度 + 账号池 + 抓取配置）
#
#   对外路径 /crawler/api/**；容器内同时拉起抓取执行器 worker
#   （WORKER_ENABLED=0 可关闭，改用独立 worker 容器）
# -----------------------------------------------------------------------------
FROM runtime AS crawler

ENV APP_ROLE=crawler \
    SERVICE_NAME=mediacrawler-crawler \
    SERVICE_PORT=8001 \
    PORT=8001 \
    WORKER_ENABLED=1

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/crawler/health" || exit 1

CMD ["uvicorn", "api.crawler_app:app", "--host", "0.0.0.0", "--port", "8001", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]


# -----------------------------------------------------------------------------
# 目标 2：library 服务（内容库：帖子/评论/媒体/标签/菜谱 + 配置 + S3
#                    + 前端静态页 + 服务名网关）
#
#   对外路径 /library/api/**；同时把 /crawler/** 按服务名转发给 crawler 服务。
#   这是**对外唯一入口**（浏览器只访问它）。
# -----------------------------------------------------------------------------
FROM runtime AS library

ENV APP_ROLE=library \
    SERVICE_NAME=mediacrawler-library \
    SERVICE_PORT=8081 \
    PORT=8081 \
    WORKER_ENABLED=0 \
    GATEWAY_ENABLED=1

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/library/health" || exit 1

CMD ["uvicorn", "api.library_app:app", "--host", "0.0.0.0", "--port", "8081", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]


# -----------------------------------------------------------------------------
# 目标 3：一体化（旧行为；放在最后 = `docker build .` 的默认目标）
# -----------------------------------------------------------------------------
FROM runtime AS all

ENV APP_ROLE=all \
    SERVICE_NAME=mediacrawler-all \
    PORT=8081 \
    WORKER_ENABLED=1

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8081"]

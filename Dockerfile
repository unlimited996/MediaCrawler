# =============================================================================
# MediaCrawler 镜像（多目标构建）
#
#   webui-builder    : Node 构建 React 前端 -> /app/api/webui
#   base-runtime     : python:3.11-slim + 通用系统库
#   runtime          : + library 的 Python 依赖（requirements-core.txt）
#   crawler-runtime  : + 爬虫系统库与 Python 依赖（requirements-crawler.txt）
#   browser-runtime  : + Playwright 浏览器内核
#   app-library / app-browser : 各自的源码 + 前端产物 + 运行目录
#
# 三个可交付目标：
#   crawler : 抓取服务    /crawler/api/**  （容器内同时拉起 worker）
#   library : 内容库服务  /library/api/** + 网关（按服务名转发 /crawler/**）
#   all     : 一体化（旧行为，本地/单机用；也是默认目标）
#
# 分层的目的（实测收益）
#   library 不跑抓取，却曾因 requirements.txt 单文件 + tools.utils 的 `import *`
#   背上 playwright / opencv / matplotlib / pandas / numpy 等约 700MB 依赖。
#   拆开后 library 只剩 Web/DB/业务必需项，镜像显著变小、跨境推送也快得多。
#
# 构建：
#   docker build --target crawler -t mediacrawler-crawler:latest .
#   docker build --target library -t mediacrawler-library:latest .
#   docker build -t mediacrawler:latest .            # = 默认 all 目标
# 跳过前端重新构建（沿用宿主机已构建好的 api/webui）：
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
# APT_MIRROR    : Debian 源；国内构建必须换（deb.debian.org 会卡死 apt-get）
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
# Stage 2：基础运行时（两个服务共用）
#
#   bash          : docker-entrypoint.sh 用的是 bash 进程替换（tee 重定向）
#   curl          : HEALTHCHECK
#   tzdata        : TZ=Asia/Shanghai，日志时间才对得上
#   libglib2.0-0  : pillow / 部分 DB 驱动的动态依赖
#
# Chromium 运行库、nodejs、中文字体都放到 crawler-runtime：library 一行都用不到。
# -----------------------------------------------------------------------------
FROM ${DOCKER_MIRROR}python:3.11-slim AS base-runtime

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

# 国内服务器构建时 deb.debian.org 会卡死（实测 apt-get 挂 23 分钟无进展），
# 用 --build-arg APT_MIRROR=mirrors.ustc.edu.cn 换国内源；CI 在海外不传、
# 保持官方源。compose 里默认就是国内源。
ARG APT_MIRROR=deb.debian.org
RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        bash \
        curl \
        ca-certificates \
        tzdata \
        libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
              /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
              /usr/share/locale/* /tmp/*


# -----------------------------------------------------------------------------
# Stage 3：library 的 Python 依赖（内容库 + 网关 + 前端）
# -----------------------------------------------------------------------------
FROM base-runtime AS runtime

# --retries/--timeout：弱网下大轮子会被掐断连接，默认重试 5 次不够，这里放宽。
# 装完清掉运行时用不到的测试代码/类型存根/字节码（镜像 ENV 已设
# PYTHONDONTWRITEBYTECODE=1，不会再生成 pyc），纯省体积、不影响功能。
COPY requirements-core.txt ./
ARG PIP_INDEX_URL=
RUN PIP_ARGS="--no-cache-dir --retries 10 --timeout 120"; \
    if [ -n "$PIP_INDEX_URL" ]; then PIP_ARGS="$PIP_ARGS -i $PIP_INDEX_URL"; fi; \
    pip install $PIP_ARGS -r requirements-core.txt \
    && (find /usr/local/lib/python3.11/site-packages -type d \
            \( -name '__pycache__' -o -name 'tests' -o -name 'test' \) \
            -prune -exec rm -rf {} + 2>/dev/null || true) \
    && (find /usr/local/lib/python3.11/site-packages \
            \( -name '*.pyi' -o -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true) \
    && rm -rf /root/.cache /tmp/*


# -----------------------------------------------------------------------------
# Stage 4：crawler 的额外依赖
#
#   nodejs        : pyexecjs 靠它执行 libs/*.js（抖音、知乎的签名脚本），
#                   缺了 node 会在 import media_platform.douyin.help 时直接崩
#   libgl1        : opencv 的动态依赖（headless 版已去掉 GUI，但仍需基础 GL）
#   fonts-wqy-*   : 词云与图表里的中文不变成方块
#   Chromium 运行库不在这里列 —— 下一层的 `playwright install --with-deps` 会补齐
# -----------------------------------------------------------------------------
FROM runtime AS crawler-runtime

ARG APT_MIRROR=deb.debian.org
RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        nodejs \
        libgl1 \
        fonts-wqy-zenhei \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
              /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
              /usr/share/locale/* /tmp/*

# requirements-crawler.txt 内 `-r requirements-core.txt`，core 已装好会被跳过。
COPY requirements-core.txt requirements-crawler.txt ./
ARG PIP_INDEX_URL=
RUN PIP_ARGS="--no-cache-dir --retries 10 --timeout 120"; \
    if [ -n "$PIP_INDEX_URL" ]; then PIP_ARGS="$PIP_ARGS -i $PIP_INDEX_URL"; fi; \
    pip install $PIP_ARGS -r requirements-crawler.txt \
    && (find /usr/local/lib/python3.11/site-packages -type d \
            \( -name '__pycache__' -o -name 'tests' -o -name 'test' \) \
            -prune -exec rm -rf {} + 2>/dev/null || true) \
    && (find /usr/local/lib/python3.11/site-packages \
            \( -name '*.pyi' -o -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true) \
    && rm -rf /root/.cache /tmp/*


# -----------------------------------------------------------------------------
# Stage 5：浏览器内核（只有 crawler / all 需要）
#
# 浏览器必须用服务端无头实例：WebUI 只是发指令的控制面板，抓取是后台任务
# （关掉页面也要跑完），且平台签名 JS / Cookie / 指纹 / CDP 控制都不可能
# 由浏览器沙箱里的前端 JS 完成。
# -----------------------------------------------------------------------------
FROM crawler-runtime AS browser-runtime

# --with-deps 补齐 Chromium 缺的系统库。默认完整 chromium（兼容 CDP 等场景），
# 可加 --build-arg PLAYWRIGHT_BROWSER=chromium-headless-shell 只装 headless
# shell（省约 600MB）：容器内 HEADLESS=true、无 X server，抓取行为一致。
ARG PLAYWRIGHT_BROWSER=chromium
# 国内构建若 playwright CDN 也慢，可传
# --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/
ARG PLAYWRIGHT_DOWNLOAD_HOST=
RUN if [ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then export PLAYWRIGHT_DOWNLOAD_HOST; fi; \
    playwright install --with-deps "${PLAYWRIGHT_BROWSER}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
              /usr/share/doc/* /usr/share/man/* /usr/share/info/* /tmp/*


# -----------------------------------------------------------------------------
# Stage 6：共享源码层（scratch，不含任何依赖/构建产物）
#
# 以前两个 app stage 各自 `COPY . /app`：同一份源码被打进两张镜像，还顺带
# 带上 webui/ 源码、宿主机 api/webui 产物、deploy/、临时脚本等运行用不到的东西。
# 现在只 COPY 运行时真正需要的 Python 包与入口文件，两个 app stage 都从这一层
# 取，源码在 registry 里只存一份；前端产物只给 library（crawler 不提供静态页）。
# -----------------------------------------------------------------------------
FROM scratch AS app-source

# 注意：COPY 多源只会把各目录的**内容**平铺进目标，不会保留目录名，
# 所以运行时包必须逐条 COPY（不能写成 `COPY a b c /app/`）。
COPY account /app/account
COPY api /app/api
COPY base /app/base
COPY cache /app/cache
COPY cmd_arg /app/cmd_arg
COPY config /app/config
COPY constant /app/constant
COPY database /app/database
COPY discovery /app/discovery
COPY exporters /app/exporters
COPY libs /app/libs
COPY media_downloader /app/media_downloader
COPY media_platform /app/media_platform
COPY model /app/model
COPY proxy /app/proxy
COPY signer /app/signer
COPY skills /app/skills
COPY store /app/store
COPY tools /app/tools
COPY main.py worker.py var.py docker-entrypoint.sh pyproject.toml \
     requirements-core.txt requirements-crawler.txt /app/


FROM runtime AS app-library

# 视频转储要统一转 H.264/AAC MP4：ffmpeg/ffprobe 只装在 library 最终层。
# 视频下载/转码/上传都由 library 的后台批处理完成，crawler 只登记 pending，
# 所以 crawler 镜像不需要 ffmpeg —— 避免两份镜像各自背一套编解码依赖。
ARG APT_MIRROR=deb.debian.org
RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
              /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
              /usr/share/locale/* /tmp/*

COPY --from=app-source /app/ /app/
# 前端产物：唯一需要静态页的镜像
COPY --from=webui-builder /app/api/webui/ /app/api/webui/

RUN mkdir -p /app/data /app/browser_data /app/database /app/logs /app/deploy \
    && chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]


FROM browser-runtime AS app-browser

COPY --from=app-source /app/ /app/

RUN mkdir -p /app/data /app/browser_data /app/database /app/logs /app/deploy \
    && chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]


# -----------------------------------------------------------------------------
# 目标 1：crawler 服务（抓取 + 调度 + 账号池 + 抓取配置）
#
#   对外路径 /crawler/api/**；容器内同时拉起抓取执行器 worker
#   （WORKER_ENABLED=0 可关闭，改用独立 worker 容器）
# -----------------------------------------------------------------------------
FROM app-browser AS crawler

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
#   这是**对外唯一入口**（浏览器只访问它），也是体积最小的镜像。
# -----------------------------------------------------------------------------
FROM app-library AS library

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
FROM app-browser AS all

ENV APP_ROLE=all \
    SERVICE_NAME=mediacrawler-all \
    PORT=8081 \
    WORKER_ENABLED=1

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8081"]

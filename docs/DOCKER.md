# MediaCrawler Docker 部署说明

一条命令起全套服务：**Web 控制台（含前端）+ API + 爬虫运行时（Python / Node / Chromium）**。

> 前置：安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Windows / macOS）
> 或 Docker Engine + Compose 插件（Linux）。

---

## 1. 最快上手

```bash
# ① 生成本地配置（第一次）
cp .env.example .env

# ② 编辑 .env —— 至少把数据库指对（见下方「数据库怎么填」）

# ③ 构建并后台启动
docker compose up -d --build

# ④ 打开浏览器
#    http://localhost:8081
```

常用操作：

```bash
docker compose logs -f        # 跟踪日志（爬虫子进程日志也会实时打在这里）
docker compose ps             # 看容器状态与健康度
docker compose restart        # 重启
docker compose down           # 停止并移除容器（volume 保留）
docker compose down -v        # 连 volume 一起删（登录态会丢）
```

---

## 2. 数据库怎么填

容器里 **没有图形界面**，也没有 `127.0.0.1 == 你的电脑` 这回事。三种情况：

| 场景 | `.env` 里怎么写 |
|---|---|
| **用 compose 自带 PG**（完全离线自测） | `POSTGRES_DB_HOST=postgres` `POSTGRES_DB_PORT=5432`，并用 `docker compose --profile with-db up -d` 启动 |
| **连宿主机上的 PG / Redis** | 主机名填 `host.docker.internal`（Windows/macOS 直接可用；**Linux** 需额外加 `extra_hosts: "host.docker.internal:host-gateway"`） |
| **连外部远程库**（如线上 PG） | 直接填真实域名 / IP，和本地一样 |

内置 PG 的参数在 compose 里可以改：

```bash
PG_LOCAL_PORT=5433 PG_LOCAL_USER=im PG_LOCAL_PASSWORD=xxx PG_LOCAL_DB=imdb \
  docker compose --profile with-db up -d
```

启动时如果不确定库有没有就绪，可以用

```bash
WAIT_FOR_DB_HOST=postgres WAIT_FOR_DB_PORT=5432 WAIT_FOR_DB_TIMEOUT=60 docker compose up -d
```

entrypoint 会先轮询端口，通了再拉起应用，避免首屏一堆连接超时。

---

## 3. 日常用镜像发布（推荐流程）

以后测试、发布都走镜像，不要再在服务器上拉代码手工起：

```bash
./scripts/docker-release.sh 1.0.0
# 产出 myrepo/mediacrawler:1.0.0 和 :latest（REGISTRY 即镜像前缀）

# 2) 推到仓库（配置好了 REGISTRY 时才会推，否则只本地产出）
REGISTRY=registry.example.com/mediacrawler ./scripts/docker-release.sh 1.0.0 --push

# 3) 导出离线包给没网的环境
./scripts/docker-release.sh 1.0.0 --save
#   产出 mediacrawler-1.0.0.tar.gz，目标机器：
#   docker load -i mediacrawler-1.0.0.tar.gz
```

在目标机器上（只有 `docker-compose.yml` + `.env` 就能跑，不需要源码）：

```bash
cat > .env <<'EOF'
IMAGE_NAME=myrepo/mediacrawler
IMAGE_TAG=1.0.0
EOF
docker compose pull
docker compose up -d
```

> `IMAGE_TAG` 用固定版本号，别用 `latest` —— 否则回滚时说不清跑的是哪一版。
> 这一条只针对**离线 tar 包交付**（`docker save/load`）；
> CCR 上的拆分部署统一用 `:latest`，见 `docs/DEPLOY_SPLIT.md` 3.6。

---

## 4. 构建选项

> 全部写在 `.env` 里即可，`docker compose build` 会自动读。

| 变量 | 默认 | 说明 |
|---|---|---|
| `SKIP_WEBUI_BUILD` | `0` | 设 `1` 跳过容器内 `npm ci` + `npm run build`，直接沿用宿主机构建好的 `api/webui/`。本机刚 build 过前端时用它能省 1~2 分钟 |
| `DOCKER_MIRROR` | `docker.io/` | 基础镜像源前缀。**Docker Hub 拉不动时**改成国内加速源，结尾斜杠别漏，如 `docker.m.daocloud.io/` |
| `NPM_REGISTRY` | `https://registry.npmmirror.com` | Stage 1 装 npm 包的源 |
| `PIP_INDEX_URL` | 空（官方 PyPI） | Python 包源，慢的话填 `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `IMAGE_NAME` / `IMAGE_TAG` | `mediacrawler` / `latest` | 镜像名与标签 |
| `APP_PORT` | `8081` | 宿主机映射端口 |
| `CONTAINER_NAME` | `mediacrawler` | 容器名 |

例：

```bash
SKIP_WEBUI_BUILD=1 docker compose up -d --build
```

### Docker Desktop 拉不动官方镜像怎么办

报错长这样就是 Hub 被挡了：

```
failed to do request: Head "https://registry-1.docker.io/v2/...":
dial tcp ...: connectex: No connection could be made
```

两条路，任选其一：

1. **改 `DOCKER_MIRROR`**（推荐，只影响本项目）——在 `.env` 里写
   `DOCKER_MIRROR=docker.m.daocloud.io/`，再 `docker compose build`。
   验证某个源是否可用：`curl -sI https://<源>/v2/`，返回 `401` 就是通的。
2. **给 Docker Desktop 配代理** —— Settings → Resources → Proxies，
   HTTP/HTTPS 填 `http://host.docker.internal:<你的代理端口>`。
   （注意别填 `127.0.0.1`，在 Docker 的虚拟机里那是它自己。）

---

## 5. 数据与持久化

`docker-compose.yml` 挂了三个 volume，重建容器不会丢：

| 宿主机目录 | 容器路径 | 内容 |
|---|---|---|
| `./data` | `/app/data` | 抓取下载的媒体文件 |
| `./browser_data` | `/app/browser_data` | **浏览器登录态** —— 扫码一次之后不用反复扫；删了这个目录等于重新登录 |
| `./database` | `/app/database` | sqlite / 导出文件等 |

---

## 6. 容器里踩过的坑（都已在本仓库修好）

1. **Chromium 必须以 root 关沙箱跑**。
   `tools/crawler_util.py::chromium_launch_kwargs()` 读到 `PLAYWRIGHT_NO_SANDBOX=1`
   会自动加 `--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage --disable-gpu`。
   compose 里已经设了这个环境变量，**不要删**。
   `/dev/shm` 在容器里默认只有 64M，不加 `--disable-dev-shm-usage` 会随机崩浏览器。

2. **没有图形界面，必须无头**。
   `config/base_config.py` 的 `HEADLESS` 已支持环境变量覆盖，compose 设了 `HEADLESS=true`。
   > 代价：容器里**没法扫码登录**。小红书请用 `--lt cookie` 模式，把有效 Cookie
   > 贴到「认证」里；或在本地有头模式扫一次，把 `browser_data/` 拷到服务器。

3. **pyexecjs 需要 Node**。
   `libs/*.js` 里是抖音、知乎的签名脚本，走 `pyexecjs` 调 Node 执行。
   镜像里装了 `nodejs`，缺了会在 `import media_platform.douyin.help` 时直接崩。

4. **前端产物目录是 `api/webui/`，不是 `webui/dist/`**。
   `vite.config.ts` 的 `outDir: '../api/webui'` 是相对 `webui/` 往外一层。
   Stage 1 刻意保持了 `/app/webui` + `/app/api` 的真实层级，别改成单独 WORKDIR `/webui`。

5. **中文字体**。词云与图表需要 `fonts-wqy-zenhei`，否则全是方块。

---

## 7. 服务起不来的排查顺序

```bash
docker compose logs --tail=100 mediacrawler   # 看启动日志
docker compose exec mediacrawler sh           # 进容器
  curl -fsS http://127.0.0.1:8081/api/health  # 健康检查
  python -c "import config; print(config.HEADLESS)"      # 应为 True
  ls /ms-playwright                                      # Chromium 在不在
  python -c "import node; " 2>/dev/null; node -v         # Node 在不在
```

常见错误：

- `…/chrome: error while loading shared libraries` → `playwright install --with-deps chromium` 没跑完整，重构建镜像
- 页面 500 `/assets/...` 404 → `api/webui/` 是空的，去掉 `SKIP_WEBUI_BUILD` 重新构建
- 登录一直失败 → 容器无头没法扫码，见第 6 条第 2 点

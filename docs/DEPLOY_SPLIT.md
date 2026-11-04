# 拆分部署指南（crawler / library 两个独立应用 + Nacos）

> 2026-11-04：把原来「爬虫 + 数据增删改查」混在一个 FastAPI 应用里的结构，
> 拆成两个可独立部署、独立重启的服务，用 **URL 里的服务名**路由，
> **Nacos** 做注册发现与跨服务调用。

---

## 1. 拆分后的样子

```
                      浏览器 / 调用方
                            │
        ┌───────────────────┴────────────────────┐
        │  http://host:8081/crawler/api/...      │  ← 服务名在 URL 里
        │  http://host:8081/library/api/...      │
        ▼
┌──────────────────────────────────────────────┐
│ library 内容库服务（8081）                     │
│  /library/api/** 本地处理（内容查询/增删改查）  │
│  /crawler/**    按服务名转发 → crawler 服务    │──┐
│  /              前端静态页                     │  │ Nacos 发现
└──────────────────────────────────────────────┘  │
                                                   ▼
                              ┌────────────────────────────────┐
                              │ crawler 服务（8001）            │
                              │  /crawler/api/** 抓取/调度/账号 │
                              └───────────────┬────────────────┘
                                              │ 子进程
                                      ┌───────▼────────┐
                                      │ worker（执行器）│
                                      └────────────────┘
```

| 服务 | 端口 | 职责 | 路径前缀 |
|---|---|---|---|
| `mediacrawler-library` | 8081 | 内容库：帖子/评论/媒体/标签/菜谱、配置、S3、前端静态页、**网关** | `/library/api/**` |
| `mediacrawler-crawler` | 8001 | 抓取、定时调度、账号池、抓取配置、日志 WS | `/crawler/api/**` |

两个服务**共享同一个数据库**；跨服务只做两类通信：
- 网关按服务名转发（同步 HTTP / WebSocket）
- crawler 跑完一轮 → POST `data` 的 `/library/api/internal/events`（异步、可丢）

服务名、路径前缀、端口都可以用环境变量改，见 `deploy/.env.example`。

---

## 2. Nacos 准备（一次性）

Nacos 部署在 `www.joyeuse.cn:8085`。两个服务启动时会自动注册：

| 注册名 | 说明 |
|---|---|
| `mediacrawler-crawler` | crawler 服务实例 |
| `mediacrawler-library` | 内容库服务实例 |

验证：

```bash
# 服务列表应能看到两个服务
curl "http://www.joyeuse.cn:8085/nacos/v1/ns/service/list?pageNo=1&pageSize=50"

# 实例列表（应返回容器 IP:端口）
curl "http://www.joyeuse.cn:8085/nacos/v1/ns/instance/list?serviceName=mediacrawler-crawler"
```

Nacos 控制台：`http://www.joyeuse.cn:8085/nacos`（默认 nacos/nacos）。

> **Nacos 挂了会怎样？** 服务照常启动（注册失败只打日志），网关自动回退到
> 静态地址 `CRAWLER_BASE_URL`（compose 里是 `http://crawler:8001`）。
> 也就是说不影响可用性，只是失去动态扩缩容能力。

---

## 3. GitHub Actions 流水线

> **fork 仓库必须先手动启用一次 Actions**：GitHub 对 fork 默认**不运行任何工作流**，
> 表现就是 Actions 页面一直显示 "There are no workflow runs yet"（即使 workflow
> 文件存在、状态是 active，push 也不会触发）。
>
> 开启方法（二选一）：
> 1. 打开 https://github.com/unlimited996/MediaCrawler/actions ，点页面上的绿色按钮
>    **"I understand my workflows, go ahead and enable them"**；
> 2. 或进 Settings → Actions → General → 选 **Allow all actions and reusable workflows** → Save。
>
> 开启**不会补跑历史提交**，开启后需要再产生一次 push，或在 Actions 页面点
> `build-and-push` → **Run workflow** 手动触发一次。

文件：`.github/workflows/build-push.yml`

**触发**：push 到 `main`、push `v*` tag、或手动 Run workflow。

**产物**（矩阵并行构建两个镜像）：

```
ccr.ccs.tencentyun.com/unlimited996/mediacrawler-crawler:latest
ccr.ccs.tencentyun.com/unlimited996/mediacrawler-crawler:sha-<7位>
ccr.ccs.tencentyun.com/unlimited996/mediacrawler-library:latest
ccr.ccs.tencentyun.com/unlimited996/mediacrawler-library:sha-<7位>
```

**构建内容**：两张镜像都在 Stage 1 用 Node 构建前端（`webui/` → `api/webui/`），
所以镜像自带界面，不依赖宿主机是否跑过 `npm run build`。一个 job 顺序构建两张镜像，
前端只编一次、apt/pip/playwright 层共用 GHA 缓存。

### 3.1 配置密钥（必须）

仓库 → **Settings → Secrets and variables → Actions**：

| 类型 | 名称 | 值 |
|---|---|---|
| Secret | `TENCENT_REGISTRY_USERNAME` | 腾讯云镜像仓库用户名（`1000xxxxxxx`） |
| Secret | `TENCENT_REGISTRY_PASSWORD` | 镜像仓库密码 / 临时 token |
| Variable | `TENCENT_NAMESPACE` | 镜像仓库命名空间，默认 `unlimited996` |

> 未配置密钥时流水线**只构建不推送**（不会红），页面上会提示缺哪两个 secret。

### 3.2 手动触发

Actions → `build-and-push` → Run workflow（可填 `extra_tag`，例如 `v1.0.0`）。

---

## 3.3 本地构建（不打 CI 时）

`docker-compose.split.yml` 里两个服务都带 `build:` 段，可以直接本地构建（会先 npm build 前端）：

```bash
docker compose -f docker-compose.split.yml build          # 两张镜像，前端只编一次
docker compose -f docker-compose.split.yml build crawler  # 只构建抓取服务
SKIP_WEBUI_BUILD=1 docker compose -f docker-compose.split.yml build   # 跳过前端重编
```

镜像名与 CI 一致（`ccr.ccs.tencentyun.com/unlimited996/mediacrawler-{crawler,library}:latest`），
所以构建完 `docker compose up -d` 起的本地镜像、和服务器 `docker compose pull` 拉的镜像完全同源。

一键构建并推送（GitHub Actions 暂时不可用时的兜底）：

```bash
export TENCENT_REGISTRY_USERNAME=1000xxxxxxx
export TENCENT_REGISTRY_PASSWORD=********
./deploy/build-push.sh                 # 构建 + 推 latest 与 sha-xxxxxxx
TAG=v1.0.0 ./deploy/build-push.sh      # 额外打版本 tag
PUSH=0 ./deploy/build-push.sh          # 只构建不推送
```

## 4. 镜像发布（在服务器上）

```bash
# 1) 登录镜像仓库（密码就是上面那个 secret）
docker login ccr.ccs.tencentyun.com -u 1000xxxxxxx

# 2) 拉取（tag 用 latest 或具体的 sha-xxxxxxx）
docker pull ccr.ccs.tencentyun.com/unlimited996/mediacrawler-crawler:latest
docker pull ccr.ccs.tencentyun.com/unlimited996/mediacrawler-library:latest
```

---

## 5. 上线（推荐 docker compose）

```bash
# 目录约定（与 compose 里的挂载一致）
mkdir -p /opt/mediacrawler/{logs,data,browser_data,database,deploy}
cd /opt/mediacrawler

# 1) 拷贝编排文件（仓库根目录）
#    docker-compose.split.yml、deploy/.env.example
cp deploy/.env.example deploy/.env
vi deploy/.env          # 填数据库口令、Nacos 地址、抓取间隔等

# 2) 起服务
docker compose -f docker-compose.split.yml --env-file deploy/.env up -d

# 3) 看状态
docker compose -f docker-compose.split.yml ps
curl -fsS http://127.0.0.1:8081/library/health
curl -fsS http://127.0.0.1:8081/crawler/health

# 4) 看日志
docker compose -f docker-compose.split.yml logs -f --tail=100
```

浏览器打开 `http://<服务器>:8081/` 即可。

### 5.1 挂载出来的是什么（排障核心）

| 宿主机 | 容器内 | 用途 |
|---|---|---|
| `./logs/` | `/app/logs` | `crawler.console.log`、`data.console.log`、`worker.console.log`（含 traceback）、`worker.out.log`、`scheduler_live.jsonl`（前端控制台同源） |
| `./deploy/.env` | `/app/deploy/.env`（只读） | **启动环境变量**，改完 `docker compose ... up -d` 生效 |
| `./browser_data/` | `/app/browser_data` | 浏览器登录态（不挂 = 每轮重新扫码） |
| `./data/` | `/app/data` | 抓取产物 / 媒体文件 |
| `./database/` | `/app/database` | sqlite 与导出 |

容器标准输出同时写进 `logs/<role>.console.log`，所以 **`docker logs` 和挂载文件都能看**，
容器删了日志也还在。

> 注意：`deploy/.env` 已经在 `.gitignore` 和 `.dockerignore` 里，不会被提交、也不进镜像。

### 5.2 只重启其中一个（这就是拆分的目的）

```bash
docker compose -f docker-compose.split.yml restart library   # 接口重启，抓取不受影响
docker compose -f docker-compose.split.yml restart crawler   # 抓取重启，接口不掉线
```

升级镜像：

```bash
docker compose -f docker-compose.split.yml pull
docker compose -f docker-compose.split.yml up -d --no-deps library   # 只换 library
docker compose -f docker-compose.split.yml up -d --no-deps crawler  # 只换 crawler
```

`--no-deps` 是关键：换 library 时不会顺带重启 crawler（正在跑的抓取不中断）。

---

## 6. 单独跑 worker（可选）

crawler 容器默认「API + worker」同容器。想拆得更开（抓取与接口彻底分开扩缩）：

```bash
# crawler 容器只跑接口
docker run -d --name mc-crawler-api \
  --env-file deploy/.env \
  -e APP_ROLE=crawler -e WORKER_ENABLED=0 -e PORT=8001 \
  -v $PWD/logs:/app/logs -v $PWD/browser_data:/app/browser_data \
  -p 8001:8001 \
  ccr.ccs.tencentyun.com/unlimited996/mediacrawler-crawler:latest

# 独立 worker 容器（同镜像，换命令）
docker run -d --name mc-worker \
  --env-file deploy/.env -e APP_ROLE=worker \
  -v $PWD/logs:/app/logs -v $PWD/browser_data:/app/browser_data \
  ccr.ccs.tencentyun.com/unlimited996/mediacrawler-crawler:latest python -m worker
```

worker 有文件锁（`logs/.worker.lock`），多起不会重复执行；跨主机部署时锁不共享，
请确保只有一台跑 worker，或用 `SERVICE_NAME` + Nacos 做单实例选主。

---

## 7. 排障速查

| 现象 | 先看什么 |
|---|---|
| 页面打开空白 / 404 | `library` 容器是否起了；`curl :8081/` 是否 200 |
| `/crawler/api/**` 返回 503 | 网关没发现实例：`curl :8081/gateway/status`；看 `NACOS_SERVER` 与 `CRAWLER_BASE_URL` |
| 接口 500 | `logs/library.console.log` 尾部 traceback |
| 抓取不动 | `logs/worker.console.log`、`logs/worker.out.log`；`curl :8081/crawler/health` 看 `worker_alive` |
| 定时任务一直 `queued` | worker 不在线（`worker_alive=false`）或 `SCHEDULER_ENABLED=0` |
| Nacos 里看不到实例 | 容器网络到 8085 不通；`SERVICE_IP` 需手工指定时设成宿主机可达 IP |
| 想改配置但不想改库 | 改 `deploy/.env` 后 `up -d`（启动类参数），页面可调的参数在「配置管理」里 |

健康检查：

```bash
curl -fsS :8081/library/health      # data + 网关
curl -fsS :8081/crawler/health   # 经网关看 crawler
curl -fsS :8081/gateway/status   # 路由表 + Nacos 发现结果
```

---

## 8. 本地开发（不起容器）

```bash
# 终端 1：crawler
SERVICE_NAME=mediacrawler-crawler SERVICE_PORT=8001 \
  ./.venv/Scripts/python.exe -m uvicorn api.crawler_app:app --port 8001

# 终端 2：data + 网关
SERVICE_NAME=mediacrawler-library SERVICE_PORT=8081 \
  CRAWLER_BASE_URL=http://127.0.0.1:8001 \
  ./.venv/Scripts/python.exe -m uvicorn api.library_app:app --port 8081

# 终端 3：抓取执行器（可选，crawler 会自动拉起）
./.venv/Scripts/python.exe -m worker
```

只想跑一个进程时用一体化入口（同时挂 `/api`、`/crawler/api`、`/library/api`）：

```bash
./.venv/Scripts/python.exe -m uvicorn api.main:app --port 8081
```

前端构建：`cd webui && npm run build`（产物 `api/webui/`，由 library 服务托管）。

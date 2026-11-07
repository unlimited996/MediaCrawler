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
        │  http://host:18081/crawler/api/...      │  ← 服务名在 URL 里
        │  http://host:18081/library/api/...      │
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

当前部署是 **Nacos v3.2.4**（容器端口映射：`8080→8085` 控制台、`8848→9000` OpenAPI、`9848→9001` gRPC）。

> ⚠️ **最容易踩的坑**：`NACOS_SERVER` 要填 **OpenAPI 端口 9000**，不是控制台 8085。
> 填 8085 会一直注册失败（那是 3.x 新拆出来的控制台 Web 端口）。

两个服务启动时自动注册：

| 注册名 | 说明 |
|---|---|
| `mediacrawler-crawler` | crawler 服务实例 |
| `mediacrawler-library` | 内容库服务实例 |

验证（v3 客户端 API，不需要鉴权）：

```bash
B=http://www.joyeuse.cn:9000/nacos/v3/client/ns/instance/list
curl "$B?serviceName=mediacrawler-crawler&groupName=DEFAULT_GROUP&namespaceId=public"
curl "$B?serviceName=mediacrawler-library&groupName=DEFAULT_GROUP&namespaceId=public"
# 期望：{"code":0,"message":"success","data":[{"ip":"...","port":8001,"healthy":true,...}]}
```

控制台：`http://www.joyeuse.cn:8085/`（3.x 控制台，登录后可在「服务管理 → 服务列表」里看到两个服务）。

### 2.1 API 版本差异（已在客户端里适配）

| | Nacos 1.x / 2.x | **Nacos 3.x（当前）** |
|---|---|---|
| 注册/摘除 | `/nacos/v1/ns/instance` | `/nacos/v3/client/ns/instance` |
| 实例查询 | `/nacos/v1/ns/instance/list` | `/nacos/v3/client/ns/instance/list` |
| 心跳 | `PUT /instance/beat` | **没有 beat 接口**（实测 501） |
| 保活方式 | 每 5s 心跳 | **每 5s 重新注册一次**（实测 5s 稳定保活；停 15s 变 unhealthy、30s 被摘除） |
| 鉴权 | 可选 | 客户端 API **不需要**；`/nacos/v3/admin/**`（服务列表等）需要 |

`tools/nacos_client.py` 会自动探测（`NACOS_API=auto`），也可以强制 `v3` / `v1`。

### 2.2 注册 IP 必须可达（本地开发注意）

注册到 Nacos 的 IP 由 `SERVICE_IP` 决定，留空则自动探测（容器里就是容器 IP，正确）。

**本地开发**时如果服务只监听 `127.0.0.1`，就一定要设 `SERVICE_IP=127.0.0.1`；
否则会注册成局域网 IP（如 `192.168.x.x`），网关拿到后连不上 → `/crawler/**` 返回 **502**。

```bash
SERVICE_IP=127.0.0.1 SERVICE_PORT=8001 uvicorn api.crawler_app:app --host 127.0.0.1 --port 8001
```

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

## 3.4 构建卡在 `exporting to image` 的处置

**现象**：CI 的「构建并推送 crawler」跑 30+ 分钟不动，日志停在：

```
#25 [auth] ... pull,push token for ccr.ccs.tencentyun.com     #25 DONE 0.0s
#26 exporting to GitHub Actions Cache                          #26 DONE 5.8s
#24 exporting to image                                        ← 卡在这里
```

认证已过、GHA 缓存导出也已完成，说明**镜像早就构建完了**，卡的是 `--push`
把最终镜像推到 CCR。两个叠加原因：

1. `docker/build-push-action@v6` 默认带 `--attest type=provenance,mode=max`，
   会额外推一个 OCI 1.1 attestation manifest；腾讯云 CCR 对它支持不完善，
   典型表现就是卡在导出阶段不返回。→ 工作流已加 `provenance: false`。
2. `runtime` 层含完整 Chromium（约 770MB），加 Python 依赖后单镜像 1.5GB
   量级；GitHub runner 在海外，跨境推国内 CCR 极慢。
   → 单步 `timeout-minutes: 30`、job `timeout-minutes: 70`，到点给明确结论，
   不再无限挂到 6 小时默认值。

**已落地的修复**（`.github/workflows/build-push.yml`）：关闭 provenance、两级超时、
`记录结果` 改 `if: always()` 输出兜底指引、手动触发新增 `push_images` 输入
（填 `false` 只构建验证，几分钟出结果）。job 开了 `cancel-in-progress`，
**再次 push 会自动取消卡住的旧 run**。

**仍然推不动时**（按优先级）：

```bash
# 1) 国内网络直接推，绕开跨境（最快）
export TENCENT_REGISTRY_USERNAME=1000xxxxxxx
export TENCENT_REGISTRY_PASSWORD=********
./deploy/build-push.sh

# 2) 或推轻量镜像：只装 headless shell，省约 600MB
PLAYWRIGHT_BROWSER=chromium-headless-shell ./deploy/build-push.sh
```

容器内 `HEADLESS=true` 且没有 X server，headless shell 与完整 Chromium 抓取行为
一致；`Dockerfile` 的 `ARG PLAYWRIGHT_BROWSER` 和 compose 的
`${PLAYWRIGHT_BROWSER:-chromium}` 默认仍是完整 chromium，兼容性不变。

> 想确认是不是「单纯慢」：Actions 手动 Run workflow 并把 `push_images` 填
> `false`，只构建不推送应在几分钟内完成。

---

## 3.5 为什么镜像里要带浏览器，以及怎么把体积降下来

### 为什么不能用「打开前端页面的用户浏览器」

WebUI 只是控制面板：`POST /crawler/start` 发完指令，抓取由服务端后台进程
（worker / crawler 容器里的 Playwright）执行，用户关掉页面也要继续跑几小时。
浏览器沙箱里的前端 JS 做不到这件事：

- **跨域**：小红书 / 抖音接口不允许第三方源 AJAX，前端拿不到数据；
- **签名与登录态**：平台签名脚本、Cookie、指纹、代理都在服务端统一维护，
  凭证属于账号池，不能下发到每个客户端；
- **控制权**：Playwright 要通过 CDP 协议驱动一个受控浏览器进程，网页 JS
  既不能启动本地进程，也不能连调试端口；
- **可复现**：定时任务的抓取结果不能依赖某个人电脑上 Chrome 的版本和插件。

> 项目确实有 `ENABLE_CDP_MODE` / `CDP_CONNECT_EXISTING=True`，可以连「用户本地
> 已开远程调试的 Chrome」，但那指的是**运行爬虫的那台机器**上的 Chrome
> （本机开发模式），不是访问 WebUI 的人的浏览器；容器里没有，默认关闭。

### 真正多余的浏览器

`runtime` stage 早先装了完整 Chromium，`crawler` / `library` / `all` 都
`FROM runtime` → **library（内容库 + 网关 + 前端静态页）也白背了约 770MB
浏览器，而它一行浏览器代码都不执行**。两张镜像各推一遍，跨境上传约 3GB，
这是 CI 30~45 分钟超时的主因。

### 已做的拆分（2026-11-05）

- `Dockerfile` 新增 `browser-runtime` stage：只有 `crawler` / `all` 从它派生，
  `library` 仍 `FROM runtime`，**镜像里彻底没有浏览器**。
- CI 的 `build-args` 加 `PLAYWRIGHT_BROWSER=chromium-headless-shell`：容器内
  `HEADLESS=true`、无 X server，只需无头内核，比完整 Chromium 省约 600MB。
- 两张镜像总上传量约 3GB → 1.3GB；step 超时 45 分钟、job 100 分钟。
- 本地 `docker compose build` 默认仍是完整 chromium（兼容性最好）；想和 CI
  产物一致就设 `PLAYWRIGHT_BROWSER=chromium-headless-shell`。

### 想让浏览器完全不进镜像（架构改造，未实施）

可用项目自带的 CDP 模式连一个独立浏览器容器（如 `browserless/chrome`），
crawler 通过 `ws://browser:9222` 连接，镜像就只剩 Python 运行时。代价：需要改
`tools/cdp_browser.py` 的连接分支（当前 `CDP_CONNECT_EXISTING` 只连 localhost）
并新增一个常驻浏览器服务，风控指纹 / 登录态要另做持久化。

### 依赖按服务分层（2026-11-06）

去掉浏览器只解决了 apt 那一层。**pip 层还有一个更大的浪费**：原来只有一份
`requirements.txt`，library 一次 `from api.library_app import app` 就会顺带加载
`playwright / opencv / matplotlib / jieba / wordcloud / pandas / numpy`，
1.61GB 里有约 700MB 是 library 一辈子用不到的。

现在拆成两层：

| 文件 | 内容 | 谁装 |
|---|---|---|
| `requirements-core.txt` | FastAPI / SQLAlchemy / asyncpg / httpx / boto3 / pillow / jieba / openpyxl … | `library`、`crawler` 都装 |
| `requirements-crawler.txt` | `-r requirements-core.txt` + playwright / opencv / matplotlib / wordcloud / pandas / pyexecjs / xhshow / parsel / pandas … | 只有 `crawler`、`all` |
| `requirements.txt` | `-r requirements-crawler.txt` | 本地开发、老脚本（装全量，保持兼容） |

`Dockerfile` 的 stage 链也随之变成：

```
base-runtime    python:3.11-slim + bash/curl/tzdata/libglib2.0-0
  └ runtime     + requirements-core.txt            ← library 到此为止
      └ crawler-runtime  + nodejs/libgl1/中文字体 + requirements-crawler.txt
          └ browser-runtime  + playwright install --with-deps
```

为了做到这一点，代码里三处「隐式耦合」被改成延迟导入（行为不变）：

- `tools/crawler_util.py`：PIL 只在显示登录二维码时用；playwright 仅作类型注解
  （改 `TYPE_CHECKING` + `from __future__ import annotations`）。
- `tools/slider_util.py`：`cv2` / `numpy` 只在滑块识别的方法里用。
- `tools/words.py`：`jieba` / `matplotlib` / `wordcloud` 只在生成词云时用。
- 顺带断掉了 `tools.utils` ↔ `tools.crawler_util` 的循环导入（原来靠
  `import *` 的副作用才能跑通，`from tools.utils import utils` 是笔误，已改成
  `from tools import utils`）。

**功能影响**：`library` 镜像没有浏览器，`POST /library/api/export/pdf`
（和 `/export/stories`）会返回 **503 + 明确提示**，而不是之前的 500。
需要 PDF 就用 `crawler` / `all` 镜像，或改用 CSV/JSON 导出。
Excel 预览原来是 `pandas`，已改用 `openpyxl` 重写（省 150MB），
`.xlsx/.xlsm` 行为一致，仅旧版 `.xls` 不再支持（提示另存为 .xlsx）。

---

## 3.6 服务器一键脚本（国内构建 + 推送，绕开 Actions 跨境）

GitHub runner 在海外，跨境推 CCR 极慢；国内服务器到 CCR 往返约 0.1s，
所以在服务器上构建推送更稳。因此 `.github/workflows/build-push.yml` 已**去掉
`on.push`**，只剩 Actions 页面手动触发；日常构建推送都走这里的 `mc-update`。

> ⚠️ 国内服务器到 `github.com:443` 常年超时，**`git clone` / `git pull` 不可用**；
> 但 `api.github.com`（取 commit sha）和 `codeload.github.com`（下源码 tarball）
> 稳定。脚本走这两条通道，服务器**不需要 .git**。

```bash
# 首次：codeload 拉代码（不是 git clone）
mkdir -p /opt/mediacrawler && cd /opt/mediacrawler
SHA=$(curl -fsSL https://api.github.com/repos/unlimited996/MediaCrawler/commits/main | jq -r .sha)
curl -fsSL "https://codeload.github.com/unlimited996/MediaCrawler/tar.gz/$SHA" \
  | tar xz --strip-components=1
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
vi deploy/.env     # 填 TENCENT_REGISTRY_USERNAME / TENCENT_REGISTRY_PASSWORD

# 固定入口（一次性）
printf '#!/usr/bin/env bash\nexec bash /opt/mediacrawler/deploy/server-update.sh "$@"\n' \
  > /usr/local/bin/mc-update
chmod +x /usr/local/bin/mc-update

# 以后每次只要一条
mc-update
```

`deploy/server-update.sh` 流程：API 取 sha → codeload 下 tarball →
`rsync --delete` 覆盖代码（保留 `deploy/.env`、日志、数据、浏览器登录态）→
**按变更范围决定建哪张镜像** → 打 `latest` + `sha-xxxxxxx` → 登录 CCR →
推送（只推构建过的）→ 可选本机 `up -d`。

| 环境变量 | 默认 | 用途 |
|---|---|---|
| `SKIP_PUSH=1` | 0 | 只构建验证，不登录/不推送 |
| `APPLY_LOCAL=1` | 0 | 推完顺带把本机容器换成新镜像 |
| `WEBUI_BUILD=0` | 1 | 跳过前端重编（仅当 `api/webui` 已有产物） |
| `FORCE_ALL=1` | 0 | 忽略变更检测，强制重建两张镜像 |

### 按需构建（避免 crawler 无谓重建）

脚本把上次成功构建的 commit 记在 `.build-state.json`，下次用 GitHub
compare 接口取两次提交之间改动的文件清单来判断：

| 改动范围 | 结果 |
|---|---|
| 只有 `docs/`、`*.md`、`.github/` | 两张都跳过 |
| `webui/`、library 专属接口（story/data/…）、`api/library_app.py`、`api/gateway.py` | 只建 **library** |
| `media_platform/`、`store/`、爬虫接口与服务、`worker.py`、`main.py` | 只建 **crawler** |
| `tools/`、`config/`、`database/`、`requirements*`、`Dockerfile` 等共享部分 | 两张都建 |
| commit 没变 | 直接退出，不构建也不推送 |

改过 `deploy/.env` 里的构建参数（如 `PLAYWRIGHT_BROWSER`）时 commit 不变，
需要 `FORCE_ALL=1 mc-update` 强制重建。compare 接口拿不到清单时保守地两张都建。

凭据优先读 `deploy/.env`，其次复用 `~/.docker/config.json` 里已保存的
`docker login`；**构建前就检查**，没凭据不会白跑 20 分钟。`deploy/.env` 已在
`.gitignore` 里，脚本自身不含任何密码。

> 脚本会先拉 tarball，若自身被更新会用新脚本**重新同步一次**再继续（旧脚本的
> rsync 排除规则可能有问题，必须让新规则真正生效）。

**长构建建议后台跑**：全量构建 20~30 分钟，SSH 长连接可能被中间网络断开并
带走进程（实测 `nohup/setsid` 在这台机器上仍会被会话清理）。用 systemd
transient service 最稳：

```bash
systemctl reset-failed mc-update.service 2>/dev/null || true
systemd-run --unit=mc-update --description="MediaCrawler build and push" \
  /bin/bash -c 'cd /opt/mediacrawler && exec mc-update > /var/log/mc-update.log 2>&1'

systemctl status mc-update.service     # 看是否还在跑
tail -f /var/log/mc-update.log         # 看进度
```

> 三个国内源是这台服务器能构建成功的关键：apt → `mirrors.ustc.edu.cn`、
> pip → `mirrors.aliyun.com/pypi/simple`、npm → `registry.npmmirror.com`；
> 代码走 `api.github.com` + `codeload.github.com`（`github.com` 主站不通）。
> compose 已默认前三个，CI 不经过 compose，仍用官方源。

**小带宽服务器推 CCR**：镜像含 873MB（Python 依赖）+ 376MB（浏览器）两个
大层，并发上传时 CCR 网关容易返回 502。在 `/etc/docker/daemon.json` 加
`"max-concurrent-uploads": 1` 后 `systemctl reload docker`（不会重启容器），
让大层独占带宽；脚本的 `push_with_retry` 单次超时给到 60 分钟并重试 3 次，
避免上传没结束就被掐断、重试又从头开始。

---

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
curl -fsS http://127.0.0.1:18081/library/health
curl -fsS http://127.0.0.1:18081/crawler/health

# 4) 看日志
docker compose -f docker-compose.split.yml logs -f --tail=100
```

浏览器打开 `http://<服务器>:18081/` 即可。

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

### 5.3 用域名访问 / 群晖 Docker：9000 不是业务网关

> **常见误区**：`www.joyeuse.cn:9000` 是 **Nacos 的 OpenAPI 端口**（容器内 8848
> → 宿主 9000），它只提供 `/nacos/v3/client/ns/**` 这类注册/发现接口，**不是
> HTTP 业务网关**，不会转发 `/crawler/api/**` 或 `/library/api/**`。
> 直接请求 `http://www.joyeuse.cn:9000/crawler/api/crawler/status` 只会拿到
> Nacos 的 404/501 错误，永远拿不到 crawler 的状态。
>
> Nacos 在这里只是「地址簿」：crawler 启动时把自己的 `ip:port` 注册进去，
> **library 服务内置的网关**去查这个地址簿，再把请求转发给 crawler。
> 业务流量不经过 Nacos。

**统一入口是 library**（宿主 `18081` → 容器 `8081`，
compose 里 `ports: "${LIBRARY_PORT:-18081}:8081"`）。crawler 另外映射了
宿主 `18001` → 容器 `8001`，只在直连调试时用；正常访问全部走 library 网关：

| 想调的服务 | 正确 URL |
|---|---|
| crawler 抓取状态（经网关转发） | `http://www.joyeuse.cn:18081/crawler/api/crawler/status` |
| library 数据列表（本地处理） | `http://www.joyeuse.cn:18081/library/api/stories?sort=publish&page=1&page_size=20` |
| 前端页面 | `http://www.joyeuse.cn:18081/` |

`sort=publish`（发布时间倒序）是合法值，另外还支持 `heat` / `time` / `like` / `comment`。

#### 想用 `http://www.joyeuse.cn/`（不带端口）

在群晖「控制面板 → 应用程序 → 反向代理」（或 Nginx）加一条规则：

```
来源： http://www.joyeuse.cn/          （80 / 443）
目标： http://<群晖内网IP>:18081/
```

之后即可用 `http://www.joyeuse.cn/crawler/api/crawler/status` 与
`http://www.joyeuse.cn/library/api/stories?sort=publish&page=1&page_size=20`。

> **不要把 9000 当业务端口**：9000 已被 Nacos 占用（9001 是 gRPC）。硬要复用就得
> 先把 Nacos 挪到别的宿主端口，再让反代/业务监听 9000 —— 注册中心与业务混在
> 同一端口，后续排障会很痛，不建议。

#### 群晖 Container Manager 导入 compose（目录问题）

用 `deploy/synology/docker-compose.prefilled.yml`（已填好群晖本机 PostgreSQL），
或仓库里的无密码模板 `deploy/synology/docker-compose.yml`。
> ⚠️ **`POSTGRES_DB_NAME` 必须是实际存数据的库名**（本项目是 `imdb`，不是
> `postgres` 默认库）。填错的表现很有迷惑性：容器正常启动、健康检查 200、
> 配置页也有数据，但**帖子/评论全空** —— 因为连到了另一套刚建好的空表。
> 换库名后要 `docker compose up -d` 重建容器才生效。


**群晖 DSM 的 Docker 不会自动创建 bind mount 的宿主目录**（Linux Docker 上会）。
直接写 `./logs:/app/logs` 会报：

```
Error response from daemon: Bind mount failed:
'/volume1/docker/crawler/browser_data' does not exist
```

所以群晖专用 compose 用 **named volumes**（`mc-logs` / `mc-data` /
`mc-browser-data`），由 Docker 自动创建，不需要预先建任何目录。两个服务共用
同一份卷（logs 是跨进程日志桥、browser_data 是登录态），不能各建一份。
`docker compose down` 不会删数据，只有 `down -v` 会。

想在 File Station 里直接看文件，就先建目录再换回 bind mount：

```bash
mkdir -p /volume1/docker/crawler/{logs,data,browser_data}
# 然后把 compose 里的 mc-logs 换回 ./logs（另外两个同理）
```

#### 群晖 Docker GUI 单独起两个容器（不用 compose）时必改

compose 里的 `http://crawler:8001` 依赖 compose 服务名，GUI 起容器时不存在：

1. `SERVICE_IP` 填 **群晖宿主机 LAN IP**（如 `192.168.1.10`），并给 crawler 容器
   映射 `8001:8001`。**别填 `127.0.0.1`**（指向容器自己，library 网关从另一个容器
   连不上 → `/crawler/api/**` 全部 502）；留空探测到的是容器内网 IP，跨主机或
   Nacos 在宿主机/公网时同样不可达。
2. `CRAWLER_BASE_URL` 改成 `http://<宿主IP>:8001`（Nacos 挂掉时的静态兜底）。
3. 两个容器加入同一个自定义 docker 网络，或直接用宿主 IP 互联。
4. 只把 library 的 `8081` 映射到宿主并做反代；crawler 不必对外暴露。
5. 两个容器共享同一个 PostgreSQL（`deploy/.env` 里的数据库配置保持一致）。

#### 如果确实要「按服务名 + Nacos」做独立网关

现有 `api/gateway.py` 是 **library 内置**网关，不是独立组件。若要在 Nacos 前放一层
按路径转发的独立网关（Nginx / Traefik / Spring Cloud Gateway），需要新增该组件并
让两个服务都注册到 Nacos；这属于新增基础设施，当前架构不需要，也没有改代码的必要。

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
| 页面打开空白 / 404 | `library` 容器是否起了；`curl :18081/` 是否 200 |
| `/crawler/api/**` 返回 503 | 网关没发现实例：`curl :18081/gateway/status`；看 `NACOS_SERVER`（要 9000 不是 8085）与 `CRAWLER_BASE_URL` |
| `/crawler/api/**` 返回 502 | Nacos 里有实例但连不上：多半是 `SERVICE_IP` 注册成了不可达 IP（本地开发设成 `127.0.0.1`） |
| 接口 500 | `logs/library.console.log` 尾部 traceback |
| 抓取不动 | `logs/worker.console.log`、`logs/worker.out.log`；`curl :18081/crawler/health` 看 `worker_alive` |
| 定时任务一直 `queued` | worker 不在线（`worker_alive=false`）或 `SCHEDULER_ENABLED=0` |
| Nacos 里看不到实例 | 容器网络到 **9000**（OpenAPI）不通；`SERVICE_IP` 手工指定时要填对端可达 IP |
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

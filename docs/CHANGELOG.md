# 更新日志（CHANGELOG）

本项目自建的 WebUI / v2 数据层相关变更统一记录在此文件，按倒序排列。
每条记录与一次 Git 提交对应，提交信息前缀与这里的标题保持一致。

格式参考 Keep a Changelog：`新增 / 变更 / 修复` 三类。

## [2026-11-09] Nacos 地址改内网：端口纠正 8085 → 9000

排查 DDNS 漂移（`www.joyeuse.cn` 解析到旧 IP `117.25.104.151`，S3/Garage
因此连不上）时，要求把 Nacos 地址也统一改成内网 IP。

- **端口纠正**：需求里给的 `192.168.10.38:8085` 是 Nacos 3.x 的**控制台
  Web 端口**（容器 8080 → 宿主 8085）；`NACOS_SERVER` 必须填 OpenAPI 的
  **9000**（容器 8848 → 宿主 9000）。填 8085 会一直注册失败。
- `deploy/.env.example`、`deploy/synology/docker-compose.yml` /
  `prefilled.yml`（crawler + library 各一处）统一为
  `NACOS_SERVER=192.168.10.38:9000`，并补注释区分「同内网用内网 IP」与
  「跨公网必须用公网可达地址」。
- 群晖当前 `NACOS_ENABLED=0`，靠 `CRAWLER_BASE_URL=http://crawler:8001`
  静态互联（`/library/health` 实测 `nacos_enabled:false`）。新变量不产生
  连接、不影响运行；`tools/nacos_client.py::get_client()` 读
  `os.getenv("NACOS_SERVER", ...)`，显式设了就会覆盖代码兜底默认值。
  环境变量变更需 `docker compose up -d` 重建容器才反映到 health 显示。
- **不建议在群晖启用 Nacos**：网关拿注册表的 `ip:port` 直连 crawler，
  而群晖宿主映射是 `18001:8001`，注册端口容易对不上；同 compose 静态互联更稳。
- 代码里 `config/base_config.py` / `tools/nacos_client.py` 的
  `www.joyeuse.cn:9000` 是 `os.getenv` 的兜底默认值，两处部署都已显式设
  env，永远走不到；改它要重建两张镜像且会拖累本地开发，故保留。
- 服务器 `/opt/mediacrawler/deploy/.env` 不改：那台机器是公网 ECS
  `1.94.118.54`，`192.168.10.38` 是群晖内网 IP，从 ECS 不可达；且当前
  服务器上没有运行 MediaCrawler 容器。
- `docs/DEPLOY_SPLIT.md` 新增「`NACOS_SERVER` 该填哪个地址」表格。

## [2026-11-09] 容器抓取命中安全验证：弹出窗口永远看不到 → 推送到 WebUI

### 现象
定时任务 `--lt cookie` 已登录，打开首页仍命中安全验证，日志一路打印：

```
[XiaoHongShuCrawler] 首页命中安全验证，等待人工处理 ...
[XiaoHongShuCrawler] 命中验证码 → 准备弹出浏览器（主页面有 / 无头模式=False）
[XiaoHongShuCrawler] 已临时打开浏览器窗口用于人工过验证
[anti_risk] 触发小红书安全验证！浏览器窗口已置前 ...
最长等待 300s
```

用户看不到任何窗口，300s 后任务失败/重试。

### 两个独立的原因
1. **登录 ≠ 免风控**：Cookie/web_session 只解决「你是谁」，安全验证看的是
   设备指纹 + 出口 IP + 行为。容器跑 `chrome-headless-shell`（headless、
   TLS/JA3 与真实 Chrome 不同）、家宽动态 IP、定时任务固定节奏，都会触发
   小红书风控，跟有没有登录无关。
2. **弹窗在容器里根本不存在**：`_human_verify` 里 `need_spawn = True` 写死，
   一律走 `_spawn_verify_browser()` 的
   `chromium.launch(headless=False)`。容器没有 X server，镜像里也只有
   `chrome-headless-shell`（不支持有头模式）；`launch()` 返回成功被当成
   「窗口已打开」写进日志，实际什么都没有。`focus_browser_window()` 在
   非 Windows 直接返回 False。`wait_for_human_verify` 于是干等 300s。

### 修复：复用命中验证的那个页面，推到 WebUI
不再另起浏览器 —— 主流程的 `self.context_page` 本来就停在验证页，
把它通过共享文件推给 crawler API 进程（同容器共用 `mc-logs` 卷，
与 `scheduler_live.jsonl` / `.stop_crawl` 同一套机制）：

```
logs/captcha_session/active.json   存在即「需要人工验证」，前端据此自动弹窗
                    frame.jpg     0.8s 原子替换的页面截图
                    cmd.jsonl     API 追加写指令，抓取进程按字节偏移消费
                    done.json     用户点「我已完成验证」→ 等待循环立即放行
```

- 新增 `tools/captcha_bridge.py`（抓取子进程侧）：`is_remote_mode()` 默认
  「Linux 且无 DISPLAY」= 容器；`drive()` 后台截图 + 指令回放，支持
  `click / drag(滑块,分步移动) / type / key / scroll / reload / done`。
- 新增 `api/routers/captcha_session.py`（API 侧纯文件中转）：
  `status / screenshot / input / done`，挂到 `crawler_app`。
- `core.py::_human_verify` 增加远程分支 → `_human_verify_remote()`：
  不 spawn、不 `goto`（避免冲掉验证页）、复用 `self.context_page`；
  结束后沿用原有链路把 Cookie 回灌 httpx + 账号池（持久化，下轮可用）。
  本机 Windows / 有桌面的 Linux 原弹窗逻辑**完全不变**。
- `anti_risk.wait_for_human_verify` 加向后兼容的可选 `done_check`，
  三个等待循环里检查；用户点「我已完成验证」不用等满 300s。
- 前端 `CaptchaDialog`：每 2s 轮询 status，active 由 false→true 自动弹出；
  截图轻点=点击、按住拖动=滑块；原始尺寸/适应屏幕切换、手机全屏、
  `z-[80]` 浮在登录页与小二维码弹窗之上；最小化后留「安全验证待处理」入口。

### 验证
- 本地桥接模拟：status/screenshot/click/drag/type/key/done 全 200；
  `drag` 正确产生 move→down→move(steps=18)→up；done 后 status 立即 inactive、
  `active.json` 清理；`wait_for_human_verify(done_check=...)` **1.0s** 放行
  （原来要等 300s）。
- `py_compile` 全部通过、`main.py --help` 正常、`npm run build` 通过。

### 仍需注意
这是「触发后能过」的治标方案，**风控根因（headless 指纹 / 出口 IP）没变**，
以后还会再触发。想降低频率：用 `CDP_HOST` 连本地真实 Chrome、换出口 IP/代理、
装完整 chromium、降频 + 拉长间隔。需要彻底无人值守时，建议改成
「命中验证即熔断并等冷却」而不是每次等人来点。

## [2026-11-09] 手机端登录页兼容 + 帖子图片加载不出来的真实根因

### 手机端「看不到验证码弹窗」
1920×1080 的原版页面截图被 `w-full` 压到手机宽度（约 375px），
验证码/二维码缩成一小块；右下角小弹窗 `z-[70]` 还盖着底部工具栏。

- 弹窗手机端全屏（`h-full w-full rounded-none`），桌面仍是居中卡片
- 截图默认**原始尺寸 1920px + 横向/纵向滚动**（桌面默认适应宽度），
  提供「原始尺寸 / 适应屏幕」切换；坐标换算仍按 `getBoundingClientRect()`
- 工具栏输入框手机占满宽度、按钮自动换行，新增「回顶部」方便找验证码弹窗
- 小二维码弹窗手机移到左上并缩小（`w-52` / `h-28`），桌面保持右下 `w-64`

### 图片加载不出来（帖子 307）：不是代码 bug，是三个候选源全废
`MediaImage` 的候选回退逻辑（本地 → 外链 → S3 备份）本身正常，实测：

| 源 | 结果 |
|---|---|
| `local_url` | `null` —— 群晖 `mc-data` 卷里没有本地副本 |
| `url`（xhscdn 外链） | **403** —— 带时间戳的签名链接已过期；服务端 `avatar-proxy` 代取同样 403 |
| `backup_url` | **502** —— `Could not connect to endpoint "http://117.25.104.151:3900/photos/..."` |

S3 配置里的 endpoint 是 `http://www.joyeuse.cn:3900`（动态公网域名，
当前解析到 117.25.104.151）。容器内部访问自己的公网 IP 会踩 NAT 回环，
DDNS 一变就彻底连不上。**修法**：在 WebUI 的 S3 备份配置里把 endpoint
改成内网地址 `http://192.168.10.38:3900`（或 Garage 容器服务名），
用配置页的「测试连接」验证；备份图恢复后 `MediaImage` 会自动回退显示。

## [2026-11-09] 修复图片 404 与恢复二维码小弹窗

### 1. 数据库读取的图片显示不出来
拆分部署后 library 挂的是 `/library/api/**`，但后端 6 处、前端 3 处
仍返回/请求旧的 `/api/...` 绝对路径，浏览器直接 404：
- `story.py`（S3 备份封面 / 媒体 URL ×3）、`s3_backup.py`（object_url）、
  `export.py`（download_url ×2）、`recipe.py`（media URL）
- 前端 `api.ts`（导出下载）、`S3BackupView.tsx`（备份图片 ×2）

统一改用 `api/service_paths.library_api()`（单体 `api/main.py` 也把同一批
路由挂到了 `/library/api`，本地开发不受影响）。

### 2. 二维码小弹窗恢复显示
原版登录页里的二维码只是截图的一部分，缩放后不好扫；右下角小弹窗
显示的是原始 base64 图，清晰可扫。之前打开大页面时：
- 大弹窗 `z-[60]` 全屏遮罩盖住 `z-50` 的小弹窗
- 登录会话期间没有抓取任务，`/crawler/qrcode` 返回 null

修复：
- 小弹窗改为默认展开、`z-[70]`（浮在大页面之上）
- 登录会话从 `img.qrcode-img` 取原始 base64，内容变化时才写一条
  `[QRCODE]` 到共享 `logs/scheduler_live.jsonl`；
  `CrawlerManager.get_qrcode()` 的已有回退会读出来给小弹窗显示原图

## [2026-11-08] 修复 WebUI 原版登录页被判「网络存在风险」

登录会话最初只用了裸 `launch_persistent_context`，而抓取任务
`XiaoHongShuCrawler.launch_browser()` 带了一整套反检测参数。两者共用同一个
`browser_data/<platform>_user_data_dir`，指纹不一致 → 小红书风控页。

补齐（`api/routers/login_session.py`）：
- `user_agent` 固定为与抓取一致的 Chrome/126（**关键**：headless shell 默认 UA
  含 `HeadlessChrome`，一眼被识别）
- viewport 1920x1080（原 1280x800）
- 三个 stealth args：`--disable-blink-features=AutomationControlled` /
  `--exclude-switches=enable-automation` / `--disable-infobars`
- `proxy=system_playwright_proxy()`、`accept_downloads=True`
- `extra_http_headers`: `Accept-Language: zh-CN,zh;q=0.9,en;q=0.8`、
  `Upgrade-Insecure-Requests: 1`
- `locale=zh-CN`、`timezone_id=Asia/Shanghai`
- `add_init_script` 抹掉 `navigator.webdriver`、补 languages/plugins/window.chrome

镜像内实测：UA 不含 `HeadlessChrome`、`navigator.webdriver` 非 true、
viewport 1920x1080、languages `['zh-CN']`、时区 `Asia/Shanghai`。

> 上限：TLS/JA3 指纹与出口 IP 是 Playwright 改不了的。若补齐后仍提示风险，
> 只能改用 `CDP_HOST` 连本地真实 Chrome（见「容器连本地 Chrome」一节）。

## [2026-11-08] WebUI 原版登录页（登录态自动落盘，无需手工导出 Cookie）

需求：除了二维码还要能短信验证，且要在 WebUI 里看到**原版页面**；
登录态必须自动写进容器，不能让用户手工复制 Cookie。

### 实现
- 新增 `api/routers/login_session.py`（crawler 侧，浏览器依赖在这一侧）：
  用「截图 + 坐标/文本回传」代替 Xvfb/noVNC —— 零系统包、零新端口、
  镜像体积不变。接口：`start` / `screenshot` (JPEG) / `input`
  (click/type/key/scroll/reload) / `status` / `stop`。
- **共享 profile**：`user_data_dir` 与 `XiaoHongShuCrawler.launch_browser()`
  一致（`browser_data/<platform>_user_data_dir`），Chromium 关闭时自动把
  Cookie 刷进 `mc-browser-data` 卷，后续抓取直接已登录。
- 登录态检测：`web_session` cookie；前端轮询 status 显示「已登录」。
- 导航放后台任务，避免 `page.goto` 拖过前端 axios 30s 超时；
  加载失败可点「刷新页面」。
- 互斥：会话写 `logs/.login_session_active`，`task_scheduler._pickup_queued`
  见标记即暂停拾取（任务保持 queued，会话结束后自动执行）；start 时若有
  running 任务返回 409。
- 前端 `LoginPageDialog`（App 层右下角）+ `loginSessionApi`。

### 验证（真实镜像内 ASGI 端到端）
```
start 200 / screenshot 200 image/jpeg / click·type·key·reload 200
status 200 (active, idle_left_sec) / stop 200 / 标记已清理
profile 目录存在，289 个文件（Chromium 落盘）
```
前端 `npm run build` 通过。

## [2026-11-07] 修复 worker 模式下 WebUI 看不到扫码登录二维码

### 背景
容器首次跑抓取时未登录，日志会输出 `[QRCODE]data:image/png;base64,...`，
前端 `LoginQrcodeFab` 每 2 秒轮询 `/crawler/qrcode` 把它渲染成图片。

### 断点
`CrawlerManager` 只从**自己拉起的子进程 stdout** 解析 `[QRCODE]` 存进
`self.qr_code`。但拆分部署后抓取子进程由 **worker** 拉起，stdout 不经过
crawler API 进程，二维码只落在挂载共享的
`logs/scheduler_live.jsonl`（JSONL，每行 at/level/msg）。
于是 `/crawler/qrcode` 恒返回 `{"qr": null}`，WebUI 浮窗永远不出现，
用户只能对着日志里一大串 base64 干等 120 秒超时。

### 修复
`get_qrcode()` 增加日志回退：
- `self.qr_code` 不新鲜时，倒序扫 `scheduler_live.jsonl` 最近 800 行取最新
  一条 `[QRCODE]`，解析出 data URL
- 日志里的 `at`（无时区）补上 CST 后做 5 分钟有效期判断，取最新、判过期
- 本地模式（API 自己拉起子进程）行为不变，优先用内存里的二维码
- 日志路径抽成模块级 `LIVE_LOG_PATH`，便于测试

### 验证
本地：新码返回、多行取最新、过期返回 None、无码返回 None。
镜像内端到端：`GET /crawler/api/crawler/qrcode` → 200，
`qr = data:image/png;base64,TEST_QR_FROM_WORKER`（来自模拟的 worker 日志）。

## [2026-11-07] 修复 `launch_persistent_context` 找不到 Chromium 可执行文件

### 现象
容器关掉 CDP 后走标准模式，随即崩：
`BrowserType.launch_persistent_context: Executable doesn't exist at
/ms-playwright/chromium-1248/chrome-linux64/chrome`

### 根因
构建参数为省镜像体积设了 `PLAYWRIGHT_BROWSER=chromium-headless-shell`，
镜像里只有 `chromium_headless_shell-1248`。但 `SAVE_LOGIN_STATE=True` 时
`XiaoHongShuCrawler.launch_browser` 用 `launch_persistent_context`，
Playwright 默认按**完整 chromium** 的路径找可执行文件。
`tools/crawler_util.chromium_launch_kwargs()` 本是版本回退兜底，却只扫
`chromium-*` 目录（且漏了新版 `chrome-linux64`），不认识
`chromium_headless_shell-*`，于是没兜住。

### 修复
扩展回退候选：识别 `chromium_headless_shell-*/chrome-headless-shell-*`、
补齐 `chrome-linux64`、用 `rsplit` 正确取两种目录的修订号；同版本时
完整 chromium 优先（rank +1）。实测 headless shell **可以**跑
`launch_persistent_context`，因此无需加装 600MB 完整 chromium，
浏览器层与镜像体积保持不变。

### 验证（重建后镜像内复现完整链路）
```
playwright 期望路径: /ms-playwright/chromium-1248/chrome-linux64/chrome（不存在）
回退选中: /ms-playwright/chromium_headless_shell-1248/.../chrome-headless-shell
launch_persistent_context OK: ok
```
镜像内 `PLAYWRIGHT_NO_SANDBOX=1` 已默认设置，root 运行不会卡在沙箱检查。

## [2026-11-07] CDP 目标地址可配置（容器可连局域网里的 Chrome）

`tools/cdp_browser.py` 原来把目标写死成 `localhost`（socket 探测、
`/json/version`、WebSocket 直连共 3 处），容器里的 localhost 是容器自己，
所以无法连接宿主机/另一台电脑上开了远程调试的 Chrome。

- 新增 `CDP_HOST`（`config/base_config.py`，默认 `localhost`，
  本机开发行为不变）；三处地址统一改走 `_cdp_host()`。
- Chrome 回报的 `webSocketDebuggerUrl` 里若含 localhost/127.0.0.1，
  自动替换成 `CDP_HOST`，否则容器会连回自己。
- 日志提示带上目标 host，便于排查。
- 群晖 compose 默认仍 `XHS_ENABLE_CDP_MODE=false`（无人值守定时任务
  不依赖用户电脑开机）；文档新增「容器连本地 Chrome」一节的启动参数、
  防火墙要求、安全风险与适用场景。

实测（新镜像）：默认 `CDP_HOST=localhost`；设 `CDP_HOST=192.168.10.50`
后 `_cdp_host()` 正确返回该 IP；`main.py --help` 与默认关闭 CDP 的行为不变。

## [2026-11-07] 容器抓取卡在 CDP 9222：补上小红书平台专属开关

`ENABLE_CDP_MODE`（base_config）默认 False，但 `XHS_ENABLE_CDP_MODE`
（xhs_config）默认 **True**，`media_platform/xhs/core.py` 又优先读后者。
该模式为本地开发设计（连本机 Chrome:9222 复用登录态），容器里没有本地浏览器，
于是每秒刷 `CDP port 9222 is not accessible`、等 60s 后任务失败。

- 群晖 compose 的 crawler 服务新增 `XHS_ENABLE_CDP_MODE: "false"` /
  `XHS_CDP_CONNECT_EXISTING: "false"`，配合 `HEADLESS: "true"` 走镜像自带
  headless chromium；扫码登录由 `[QRCODE]` 日志推给 WebUI，不依赖 GUI。
- `deploy/.env.example` 同步加注释说明「容器部署必须为 false」。
- 实测：旧默认 `use_cdp=True`；加变量后 `use_cdp=False`，容器内
  `chromium.launch()` 渲染正常。
- 已知限制：配置页 `enable_cdp_mode` 映射到 `config.ENABLE_CDP_MODE`，
  对小红书不生效（xhs 读 `XHS_*`），容器请以环境变量为准。

## [2026-11-07] 修复容器执行抓取报 `ModuleNotFoundError: No module named 'cache'`

### 根因
`.dockerignore` 里有一行裸 `cache`，本意是排除运行时缓存，但项目根目录的
`cache/` 是**Python 源码包**（`abs_cache.py` / `cache_factory.py` /
`local_cache.py` / `redis_cache.py`），被 `proxy/base_proxy.py`、各平台登录、
短信等 8 处引用。于是 `crawler` 镜像里缺整个包：

```
main.py -> media_platform.bilibili -> proxy.proxy_ip_pool -> proxy.base_proxy
        -> from cache.abs_cache import AbstractCache  => ModuleNotFoundError
```

容器能起、health 200（API 进程不加载平台模块），但一执行定时任务抓取就崩，
worker 按策略 60s 重试仍然失败。镜像内实测只有 `cache: MISSING`，其它模块齐全。

### 修复
- `.dockerignore`：删掉裸 `cache`，改为 `.cache` / `**/.cache` / `**/*.cache`，
  并写明「根目录 cache/ 是源码包，禁止排除」。
- `api/routers/env.py`：把 `cache` 加入 `_COMMON_MODULES` 自检，
  以后再出现打包遗漏，`/library/api/env/check` 与 `/crawler/api/env/check`
  会直接报「缺少依赖：cache」，不用等抓取才暴露。

### 验证（重建后的 crawler 镜像）
```
/app/cache/ 五个 .py 齐全
python main.py --help                     正常（完整 import 链）
from media_platform.bilibili import BilibiliCrawler
from proxy.base_proxy import *
import cache.abs_cache                    -> IMPORT_OK
```

> 部署注意：镜像 tag 是 `latest`，群晖上必须显式 `docker compose pull crawler`
> 才会拿到新镜像，只 `up -d` 不会自动更新。

## [2026-11-06] 控制台配色修复 + 执行记录提速 + 管理功能迁 library + 按需构建 + 配置入库

### 修复：系统控制台「文字消失」
`Terminal` 容器固定深色底（`#0d1117` / `#161b22`），但「抓取 / 入库」这两个
标签文字**没指定颜色**，继承的是 body 的 `text-cyber-text-primary` ——
浅色主题下那是近黑色（15 23 42），落在深灰底上几乎看不见，
表现为「只看到数字，看不到标签」。

- `Terminal.tsx`：容器统一加 `text-[#c9d1d9]`；统计标签用 `#8b949e`、
  数字用固定亮色 `#79c0ff` / `#56d364`；banner 与提示符也改成固定色。
- `TerminalLine.tsx`：日志级别配色从主题 neon 变量改为固定 GitHub Dark
  色板（info `#79c0ff`、success `#56d364`、warning `#e3b341`、error `#ff7b72`）。
  终端永远是深底，不该跟着主题变。
- `ScheduledTasksView.tsx`：执行记录里的正文数字（新增/更新/实时进度/错误信息）
  从 neon 主色改成 `-dim` 变体 —— 浅色主题下主色落在白面板上对比度只有
  2.6~3.4:1，小字号看不清；dim 变体到 4.0~4.6:1。

### 修复：使用须知弹窗与「环境检测」误报
- `LicenseDisclaimer`：弹窗内容框是 `bg-black/50` + `text-cyber-text-primary`，
  浅色主题下等于「半透明黑底 + 近黑字」，条款完全看不清。改为固定深色
  （`#161b22` / `#0d1117` + `#c9d1d9`），与全黑遮罩一致。
- **环境检测接口迁移**：`GET /api/env/check` 原来只写在单体 `api/main.py`，
  而且实现是跑 `python main.py --help`。拆分后 library 镜像没有爬虫依赖，
  这个子进程必然失败，前端于是永远弹「无法连接到 API 服务器」。
  现在搬到 `api/routers/env.py`（三个应用共用），改成按服务职责真实探测：
  依赖是否齐全（`find_spec`）、数据库能否连、crawler 服务能否发现。

### 性能：执行记录加载
- 后端 `list_runs` 默认**不再返回** `stats_json` / `log_json`。一页 50 条
  记录里这两个字段通常几十~几百 KB，而列表页根本不用（只有展开才看）。
  实测 5 条：**~660KB → 2.3KB**；10 条 5.0KB。
- 新增 `GET /scheduler/runs/{run_id}` 返回单条明细；列表项带
  `has_details` 提示可展开。
- 前端 `useSchedulerRuns` 轮询间隔按状态动态：有 running/queued 时 4s，
  全部结束 20s（原来固定 5s，记录不变化也在打）。
- 展开某条时才请求 `useSchedulerRunDetail`，已结束记录缓存 60s。

### 架构：管理功能迁到 library
`scheduler` / `config` / `accounts` / `ws` 四组路由本质是**数据库 + 共享
logs 目录**驱动，不依赖爬虫进程，现在同时挂在 library 上：

- 配置读写 → `crawl_config_item` 表
- 定时任务 CRUD → `scheduled_task` 表；「立即执行」写一条 queued，
  由 crawler 的 worker 拾取（本来就是这套设计，library 不跑调度）
- 账号池 → 账号表
- 停止抓取 → 往挂载的 `./logs` 写 `.stop_crawl`，crawler 容器能看到
- 实时日志 → tail 共享的 `logs/scheduler_live.jsonl`

前端 `CRAWLER_SEGMENTS` 从 `{crawler, scheduler, accounts, config}` 收窄到
`{crawler}`，WS 地址改 `/library/api/ws/logs`。**crawler 现在只负责**：
启动/停止抓取、队列、抓取状态。日常管理功能改配置/建任务/看日志都不碰它。

### 部署：按需构建
`deploy/server-update.sh` 记录上次构建的 commit（`.build-state.json`），
用 GitHub compare 接口取两次提交间的文件清单判断该建哪张镜像：

| 改动范围 | 构建 |
|---|---|
| 纯文档 / CI | 都跳过 |
| webui、library 专属接口 | 只 library |
| media_platform、store、爬虫接口 | 只 crawler |
| tools/config/database/requirements 等共享 | 两张都建 |
| commit 没变 | 直接退出，不构建不推送 |

推送也只推本次构建过的；`FORCE_ALL=1` 可强制全量（改了 .env 构建参数时用）。

### 配置：进程参数入库（改配置不用重建镜像）
原本只存在于 `config/*.py` 常量或容器环境变量的 16 项参数纳入配置页，
新增「定时任务」分组：

- 抓取类（next_task）：评论完整率目标、单账号并发、列表空页提前停止、
  楼中楼最大页数、页面打开超时
- 调度类（immediate）：同时执行任务数、扫描间隔、活跃时段起止、
  执行抖动上下限、忙碌等待上限、保留执行记录数
- 风控/存储：风控冷却时长、删帖是否连带删 S3 备份

实现：`runtime_config.SYNC_TO_CONFIG` 在每次 `load()` 后把库值**回写**
`config.base_config` / `config.xhs_config` 的模块属性，消费方的
`config.SCHEDULER_TICK_SEC` 这类属性访问立刻读到新值；worker 的调度循环
每 60 秒自动 refresh 一次，所以改配置页**不需要重启容器**。

> 仍需环境变量的只有部署拓扑（`NACOS_*`、`SERVICE_*`、`CRAWLER_BASE_URL`
> 等）：这些要在进程启动前就确定，且注册中心地址本身不能存在被它管理的库里。

## [2026-11-06] 修复服务器同步漏掉 database/ 代码（镜像里是旧版 db_session）

### 问题
`deploy/server-update.sh` 的 rsync 整体排除了 `database/`：
`--exclude 'database/'`。但 `database/` 同时是**代码目录**
（`db_session.py` / `models_v2.py` 要打进镜像）和 sqlite 数据目录
（compose 挂载 `./database:/app/database`）。结果是服务器上的
`database/` 永远停留在首次同步的版本。

现场表现：library 容器启动正常、健康检查 200，但
`GET /library/api/stories` 直接 500：
`ImportError: cannot import name 'SharedEngineHandle' from 'database.db_session'`
—— 镜像里是 11-05 的旧 `db_session.py`。

### 修复
- rsync 只排除数据库文件本身：
  `--exclude 'database/*.db' 'database/*.db-*' 'database/*.sqlite*'`，
  `.py` 与 `seed/` 照常同步。
- 脚本自我更新后要**重新同步一次**：原先检测到脚本变化就 `exec` 重跑，
  但重跑分支跳过了整个拉取阶段 —— 于是「新排除规则」永远没机会生效
  （第一次同步用的还是旧规则）。现在把下载+rsync 抽成 `sync_code()`，
  重跑时也执行，`_MC_UPDATED=1` 只用于防循环，不再跳过同步。

> 教训：容器挂载目录与代码目录重合时，同步脚本不能按目录整体排除。

## [2026-11-06] library 镜像依赖分层：pip 层再砍约 700MB

### 背景
上一轮只把 Chromium 从 library 镜像里拆掉（apt 层），但 pip 层仍有约 700MB
无用依赖：library 一次 `import api.library_app` 就会经 `tools.utils` 的
`import *` 顺带加载 playwright / opencv / matplotlib / jieba / wordcloud /
pandas / numpy。

### 变更：requirements 拆两层
- 新增 `requirements-core.txt`（library 用：FastAPI/SQLAlchemy/asyncpg/httpx/
  boto3/pillow/jieba/openpyxl 等）。
- 新增 `requirements-crawler.txt`（`-r requirements-core.txt` + playwright/
  opencv/matplotlib/wordcloud/pandas/pyexecjs/xhshow/parsel 等）。
- `requirements.txt` 改为 `-r requirements-crawler.txt`，本地开发与老脚本
  仍装全量，行为不变。
- **补上原本缺失的 `asyncpg`**：它从没写进 requirements，只是本机 `.venv`
  手工装过，镜像里没有 postgres 驱动（容器内连库会直接失败）。

### 变更：Dockerfile stage 链
`base-runtime`（系统库）→ `runtime`（+core，library 到此为止）→
`crawler-runtime`（+nodejs/libgl1/中文字体 + crawler 依赖）→
`browser-runtime`（+playwright install --with-deps）→ `app-library` / `app-browser`。
源码 COPY 移到两条应用分支，改代码只重建最后几层，apt/pip 缓存仍命中。

### 变更：消除隐式耦合（行为不变）
- `tools/crawler_util.py`：PIL 改函数内导入；playwright 类型改 `TYPE_CHECKING`
  + `from __future__ import annotations`。
- `tools/slider_util.py`：`cv2` / `numpy` 改方法内导入。
- `tools/words.py`：`jieba` / `matplotlib` / `wordcloud` 改方法内导入，
  matplotlib 显式用 Agg 后端。
- 断掉 `tools.utils` ↔ `tools.crawler_util` 的循环导入：把笔误
  `from tools.utils import utils`（靠 `import *` 副作用才成立）改成
  `from tools import utils`（`tools/async_file_writer.py`、`proxy/base_proxy.py`），
  `crawler_util` 顶层不再 `from . import utils`。

### 变更：功能降级（显式化）
- `api/routers/export.py`：PDF 导出前检查 Playwright，缺失时返回 **503 +
  可操作提示**（原来是 ImportError 被吞成 500）；`/export/pdf` 与
  `/export/stories` 都适用。
- `api/routers/data.py`：Excel 预览从 pandas 改 openpyxl（`read_only` 流式读），
  省 pandas+numpy 约 150MB；`.xls` 旧格式提示另存为 `.xlsx`。

### 验证
- 屏蔽 playwright/cv2/matplotlib/wordcloud/pandas/numpy/execjs/xhshow/parsel/
  humps/redis/typer/motor/tenacity 后：三个入口（library 104 / crawler 42 /
  main 257 路由）全部正常加载。
- library 22 个只读接口 + 带参数接口（stories 详情/评论/编辑记录/watch）
  全部 200；PDF 导出 503 + 中文提示。
- 爬虫侧 20 个模块（各平台 client、store、main、cmd_arg）导入无失败。

## [2026-11-06] 实时抓取/入库统计 + 消除 library 的爬虫依赖导入耦合

### 新增：实时统计（执行记录 + 系统控制台）
- worker 每轮开始打 `[MC_RUN_BEGIN] run_id=… task_id=…`；
- crawler API 的日志桥（`_live_log_tailer`）逐行解析并累计：
  * **抓取** = `[XxxCrawler.xxx]` / `[XxxClient.xxx]` 请求标记
  * **入库** = `[store.<platform>.<func>]` 落库标记
- 通过 WS 推 `{"type":"stats"}` 给系统控制台（0.5s 节流，批尾补发最终值），
  同时随 `/scheduler/status.live_stats` 给执行记录页（原本就 5s 轮询）；
- WS 新连接补发一次快照；服务重启时从日志尾部（最后一个 `[MC_RUN_BEGIN]`
  之后）重建计数，避免重启后归零。
- 新增 `api/services/live_stats.py`；前端 `crawlerStore` 增 `liveStats`，
  `Terminal` 头部与执行记录面板/运行中条目显示「抓取 N / 入库 M」。

### 变更：导入解耦（为 library 瘦身铺路）
- `api/services/__init__.py`：`task_scheduler` 改 PEP 562 惰性导出；
- `api/routers/__init__.py`：所有 router 改惰性导出。
- 原因：library 只要 `from .routers import …` 就会加载 scheduler_router →
  task_scheduler → tools.anti_risk → tools.utils → playwright / cv2，
  把整套爬虫依赖拖进「只有数据接口 + 前端」的进程。
- 实测：改动后 `from api.library_app import app` **不再加载**
  cv2/playwright/matplotlib/jieba；三入口路由数不变（library 104 / crawler 42 / main 257）。

### 验证
- 端到端：WS 连接补发 stats；追加模拟日志后收到
  `fetched=2 saved=3 run_id=999`，`/scheduler/status.live_stats` 同值，
  计数按轮重置正常。
- 前端 `npm run build` 通过（`index-DYDNPNgt.js`）。

### library 镜像 1.61GB 的构成（本次未再减小）
- pip 671MB + apt 378MB + python:3.11-slim 基础约 141MB。
- 它 `FROM runtime`，而 runtime 是给 crawler 准备的公共层：33 个 Python 依赖
  （playwright 140MB / opencv 72MB / pandas 79MB / numpy 73MB / jieba 42MB /
  matplotlib 35MB…）+ Chromium 系统库 + nodejs + Mesa。
  本次只切断了**启动时**的导入耦合，依赖包本身仍在镜像里。
- 要压到 ~700MB 需把 requirements 拆 core/crawler + apt 分层，并把
  watch / PDF 导出等模块改懒加载后做全接口冒烟 —— 属独立改造，暂未做。

## [2026-11-05] 移除 push 自动构建 + 生产镜像零风险瘦身

### 变更
- `.github/workflows/build-push.yml`：去掉 `on.push`，**只剩 `workflow_dispatch`
  手动触发**。GitHub runner 跨境推 CCR 反复超时，构建/推送统一走国内服务器
  `mc-update`；`deploy.yml` 之前已改手动触发，现在两个 workflow 都不会被 push 触发。
- `requirements.txt`：
  * `opencv-python` → `opencv-python-headless`：容器无图形界面，`cv2` API 完全一致，
    去掉 Qt/GUI 动态库（`opencv_python.libs` 约 116MB）；
  * `pytest` / `pytest-asyncio` 移到新增 `requirements-dev.txt`，生产镜像不装测试依赖。
- `Dockerfile`：apt 层 `apt-get clean` + 清 `/usr/share/{doc,man,info,locale}`、
  `/var/cache/apt`；pip 层清 `__pycache__` / `tests` / `*.pyi` / `*.pyc`（运行时不读，
  镜像已设 `PYTHONDONTWRITEBYTECODE=1`）；浏览器层同样清 apt 文档缓存。
- **没有拆依赖集合**：在完整镜像里实测 `from api.library_app import app` 确实会加载
  `cv2` / `playwright` / `numpy` / `PIL` / `sqlalchemy`（共享 `tools/` 的顶层 import），
  拆 requirements 会让 library 启动即 ImportError，所以只做「删运行时垃圾 +
  换等价轻量包」，功能零影响。

### 基线（瘦身前，服务器实测）
- crawler 2.41GB / library 1.87GB；
- 分层：873MB(pip) + 380MB(apt) + 376MB(浏览器) + 基础镜像。

## [2026-11-05] 修复国内服务器构建 apt-get 卡死

### 现象
- 服务器 `SKIP_PUSH=1 mc-update` 跑到 `apt-get` 后挂 **23 分钟**、CPU 0.1%、
  Docker 构建缓存 13 分钟没有任何增长 —— 卡在 Debian 官方源。

### 根因 / 修复
- 实测 `deb.debian.org` 3.7s 且连接会挂住；`mirrors.ustc.edu.cn` 0.085s、
  `mirrors.aliyun.com` HTTPS 0.126s。
- `Dockerfile` 新增 `ARG APT_MIRROR=deb.debian.org`：非默认值时 sed 把
  `/etc/apt/sources.list.d/debian.sources`（兼容旧的 sources.list）里的
  `http://deb.debian.org` 换成国内镜像；CI 在海外不传该参数，仍用官方源。
- `docker-compose.split.yml` 两个服务 `build.args` 加
  `APT_MIRROR: ${APT_MIRROR:-mirrors.ustc.edu.cn}`（CI 不经过 compose，不受影响）。
- `deploy/.env.example` 补说明；commit `3bb5179`。
- 同一轮又发现 `pip install` 卡在 `files.pythonhosted.org`（`jieba` 读超时）：
  实测 pypi.org 超时、阿里云源 0.078s。compose 两个服务补
  `PIP_INDEX_URL: ${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}`；
  `Dockerfile` 另加可选 `PLAYWRIGHT_DOWNLOAD_HOST`（默认空=官方 CDN，
  CDN 慢时可在 `.env` 设 npmmirror 镜像，无需再改代码）。
  CI 不经过 compose，pip/Playwright 仍用官方源。

## [2026-11-05] 新增服务器一键构建推送脚本（绕开 Actions 跨境）

### 背景
- GitHub runner 跨境推 CCR 反复超时；国内服务器到 CCR 往返约 0.1s，
  改在服务器（Ubuntu 24.04 / Docker 29 / Compose v5）上拉代码构建推送。

### 新增
- **网络适配**：实测国内服务器 `github.com:443` 常年超时（`git clone` /
  `git ls-remote` 均失败），但 `api.github.com` 0.75s、`codeload.github.com`
  19MB/22s 可用 → 脚本改用「API 取 commit sha + codeload tarball + rsync 覆盖」
  拉代码，服务器不需要 `.git`，不依赖 GitHub 主站。
- `deploy/server-update.sh`：取 sha → 下 tarball → `rsync --delete` 覆盖（保留
  `deploy/.env`/日志/数据/浏览器登录态）→ `docker compose build`
  两张镜像 → 打 `latest` + `sha-<7>` → 登录 CCR → `docker push` →
  可选 `APPLY_LOCAL=1` 本机 `up -d`。
  * 凭据从 `deploy/.env` 读（或复用已保存的 `docker login`），**构建前先校验**，
    缺凭据直接 fail fast，不白构建；脚本自身不含密码。
  * 脚本先 `git pull`，若自身被更新则 `exec` 重新执行，保证用新逻辑。
  * 开关：`SKIP_PUSH=1`（只构建验证）、`APPLY_LOCAL=1`、`WEBUI_BUILD=0`。
- `deploy/.env.example`：补 `TENCENT_REGISTRY_USERNAME` /
  `TENCENT_REGISTRY_PASSWORD` 占位与 `PLAYWRIGHT_BROWSER=chromium-headless-shell`。
- `docs/DEPLOY_SPLIT.md` 3.6：首次 clone / 建 `/usr/local/bin/mc-update`
  固定入口 / 以后一条 `mc-update`。

## [2026-11-05] 镜像瘦身：library 不再带浏览器、crawler 改无头内核

### 现象 / 追问
- 加了 `provenance: false` 和 30 分钟超时后，run `41958984216` 的
  「构建并推送 crawler」**正好 30.0 分钟被超时砍掉**（不是构建错误，是跨境
  1.5GB 没传完）。
- 用户质疑：为什么把浏览器内核封进镜像，不能用打开前端页面的用户浏览器？

### 结论
- 抓取得用**服务端无头浏览器**：WebUI 只是控制面板，抓取是关掉页面也要继续跑的
  后台任务；跨域限制、平台签名 JS、Cookie/指纹、CDP 进程控制都不可能由浏览器
  沙箱里的前端 JS 完成。项目自带的 `ENABLE_CDP_MODE` /
  `CDP_CONNECT_EXISTING` 连的是**运行爬虫那台机器**的 Chrome，不是访客浏览器。
- 但质疑戳中了真正的浪费：`runtime` 装了完整 Chromium，`library` 也
  `FROM runtime` → 内容库/网关镜像白背约 770MB 浏览器，一行浏览器代码都不跑。

### 修复
- `Dockerfile`：新增 `browser-runtime` stage（只有它 `playwright install`）；
  `crawler` / `all` 改 `FROM browser-runtime`，`library` 保持 `FROM runtime`
  → library 镜像彻底不含浏览器。
- `.github/workflows/build-push.yml`：两个 build step 的 `build-args` 加
  `PLAYWRIGHT_BROWSER=chromium-headless-shell`（容器 `HEADLESS=true`、无 X
  server，比完整 Chromium 省约 600MB）；step 超时 30→45、job 70→100。
- 总上传量约 3GB → 1.3GB。本地 compose 默认仍是完整 chromium，兼容性不变。
- `docs/DEPLOY_SPLIT.md` 新增 3.5 节解释架构原因与「浏览器完全不进镜像」的
  CDP 改造方向（未实施）。

### 验收
- 静态解析 Dockerfile stage 链：`crawler`/`all` 含浏览器、`library` 不含，
  `runtime` 自身不再执行 `playwright install`。
- PyYAML 校验 workflow：两个 step 均 `timeout=45` / `provenance=false` /
  build-args 含 headless-shell；job `timeout=100`。
- 本机 Docker Desktop 守护进程未运行，无法实构建；由 CI 新 run 验证。

## [2026-11-05] 修复 CI 构建卡在 exporting to image（CCR attestation + 跨境大镜像）

### 现象
- run `41954222099` 的「构建并推送 crawler」停在 `#24 exporting to image`
  33m+ 无任何进展；而认证 `#25 DONE 0.0s`、GHA 缓存导出 `#26 DONE 5.8s`
  都已完成 —— 镜像其实早就构建完了，卡的是最后推送到 CCR。

### 根因
1. `docker/build-push-action@v6` 默认生成 provenance attestation
   （日志里 `--attest type=provenance,mode=max`），会额外推一个 OCI 1.1
   attestation manifest；腾讯云 CCR 对它支持不完善，典型表现就是导出阶段挂起。
2. `runtime` 层装了完整 Chromium（约 770MB），单镜像 1.5GB 量级，
   GitHub 海外 runner 跨境推 `ccr.ccs.tencentyun.com` 极慢。
3. job 和 step 都没有超时，会一直挂到 Actions 默认的 6 小时。

### 修复
- `.github/workflows/build-push.yml`：两个 build step 加 `provenance: false`、
  `timeout-minutes: 30`（step 级，不能写进 `with:`）；job `timeout-minutes: 70`；
  `记录结果` 改 `if: always()` 并在失败时输出兜底指引；`workflow_dispatch`
  新增 `push_images` 输入（`false` = 只构建验证，几分钟出结果）。
- `Dockerfile`：新增 `ARG PLAYWRIGHT_BROWSER=chromium`（**默认行为不变**），
  可切 `chromium-headless-shell` 省约 600MB；`docker-compose.split.yml` 与
  `deploy/build-push.sh` 透传该变量。
- `docs/DEPLOY_SPLIT.md` 新增 3.4 节：诊断思路、只构建验证、Cancel 卡住的 run、
  国内 `deploy/build-push.sh` 兜底、轻量镜像用法。

### 验收
- PyYAML 解析 workflow：`provenance=false`、step 级 `timeout-minutes=30`、
  job `70`、dispatch inputs 含 `push_images`、`记录结果` 带 `always()`，
  且 `with:` 中没有误放的 `timeout-minutes`。
- `concurrency.cancel-in-progress=true`：本次 push 会自动取消卡住的旧 run。

## [2026-11-05] 修复顶部「数据管理」快捷平台标签消失，补群晖 / 域名部署说明

### 现象
- 拆分部署（或只起 crawler 服务）后，顶部「数据管理」旁边的快捷平台标签
  （小红书 / 抖音 / 快手 / B站 / 微博 / 贴吧 / 知乎）整排不见了，
  只剩「数据管理」一个按钮，且没有任何报错提示。

### 根因
- 平台列表端点 `GET /api/config/platforms` 当初是**直接写在单体 `api/main.py`**
  上的，没有进 `config_router`。拆分后 crawler 服务只挂载 `config_router`，
  于是前端 `/crawler/api/config/platforms` 返回 404 → `usePlatforms()` 拿到空数组
  → `Sidebar` 里 `list.map()` 什么都不渲染。
- 前端也没有兜底：接口一失败快捷标签就整体消失。

### 修复
- 把端点与 `SUPPORTED_PLATFORMS` 常量统一挪到 `api/routers/config_admin.py`：
  单体的 `/api/config/platforms` 与 `/crawler/api/config/platforms`、拆分后的
  crawler `/crawler/api/config/platforms` 都由 `include_router` 自动拥有；
  删掉 `api/main.py` 里的重复定义（避免两条同名路由行为分叉）。
- `webui/src/components/layout/Sidebar.tsx` 加静态兜底 `FALLBACK_PLATFORMS`：
  接口 404 / 服务未就绪时快捷标签仍然显示。
- 前端 `npm run build` 产物 `index-zkZA07uK.js`。

### 文档 / 配置
- `docs/DEPLOY_SPLIT.md` 新增 5.3：明确 **9000 是 Nacos OpenAPI 端口、不是业务
  网关**；统一入口是 library 的 8081；给出 crawler/library 两个正确 URL、
  群晖反向代理规则、GUI 单容器起服务时 `SERVICE_IP` / `CRAWLER_BASE_URL` 的填法。
- `deploy/.env.example` 把 `SERVICE_IP` 的三种场景（compose / 本地双进程 / 群晖）
  与「别填 127.0.0.1」的坑写进注释。

### 验收
- ASGI 内存调用（不需要起服务）：`/crawler/api/config/platforms`（crawler_app）、
  `/api/config/platforms` 与 `/crawler/api/config/platforms`（main）均 200，
  返回 7 个平台；`py_compile` 通过；前端构建通过。

## [2026-11-05] 修复定时任务状态不同步、抓取日志不滚动，新增「停止当前抓取」

### 现象
- 定时任务页顶部显示「未启动」，但执行记录 / 任务卡片明明显示「正在执行」。
- 抓取页面打开后日志不滚动，看不到独立执行器（worker）里正在跑的抓取输出。
- 页面上没有手段终止一轮正在跑的抓取，只能等它跑完或去手工杀进程。

### 根因
1. 拆分部署后调度跑在独立 worker 进程里，API 进程内
   `task_scheduler.started` 恒为 `false`、`_states` 恒为空：
   - 顶部徽章只读 `started` → 永远「未启动」；
   - `/scheduler/status` 的 `running_tasks` 只数内存 `_states` → 恒为 0，
     前端据此禁用「停止抓取」按钮，状态一滞后按钮就点不了。
2. 实时日志原本只走 crawler 进程的内存队列（手动抓取用），worker 的日志
   写在 `logs/scheduler_live.jsonl`，WS 通道没有读它 → 定时任务跑起来页面无输出。

### 修复
- **状态**：`get_status()` 在内存计数为 0 时改从 DB 统计
  `ScheduledTaskRun.status='running'`；前端徽章改为 `started || worker_alive`，
  并展示 `mode` / `queued_runs` 徽标。
- **日志桥**：新增 `_live_log_tailer()` 每秒增量 tail `scheduler_live.jsonl`
  并广播；WS 连接时补发最近 100 条；crawler 启动即拉起该 tailer，
  不再等第一个连接。实测补发 100 条 + 新日志 1 秒内推送成功。
- **停止抓取**：新增 `tools/crawl_stop.py`（共享目录信号文件 + 120s TTL），
  `api/services/crawl_control.py` 统一入口；`POST /crawler/stop` 与
  `POST /scheduler/stop` 都走它；worker 侧 `execute_task` 加每秒看门狗，
  命中即杀抓取子进程树、把本轮记为 `stopped`（不自动重试、进度保留）；
  新一轮开始先 `clear_stop()` 避免残留信号误杀。
- **前端**：定时任务页新增「停止抓取」按钮（不再依赖可能滞后的 `hasRunning`
  做禁用，后端幂等：没任务在跑返回 `stopped:false` + 提示）；
  `RunStatus` 增加 `stopped`，中英文 i18n 补齐 `actions.stop` / `queuedRuns`。

### 验收
- 后端 `py_compile` 全通过；`npm run build` 产出 `index-Cxu9a5ga.js`。
- 重启后 8001 / 8081 均监听，Nacos 注册 `mediacrawler-crawler` /
  `mediacrawler-library` 成功；`/crawler/api/scheduler/status` 经网关返回
  `worker_alive=true`、`mode=worker`。
- WS 自检：连接补发 100 条历史，追加一行后 1 秒内收到广播。
- `POST /crawler/api/scheduler/stop` 返回 200 且结构完整。

## [2026-11-05] 修复 Nacos 3.x 注册失败（v1 接口已移除 / 无 beat 接口 / 端口填错）

### 现象
- 服务启动打印 `注册失败 mediacrawler-crawler -> ...（Nacos 不可达或参数被拒）`，
  Nacos 控制台看不到实例，网关只能走静态地址。

### 根因（实测 Nacos v3.2.4）
1. **端口填错**：`NACOS_SERVER` 默认写的是 `8085`，那是 3.x 的**控制台**端口；
   OpenAPI 在 8848（宿主映射 **9000**）。
2. **v1 接口已移除**：`/nacos/v1/ns/service/list` 返回 `501 no such api`，
   v2 返回 404。命名服务改到 **`/nacos/v3/client/ns/**`**（客户端 API，无需鉴权），
   admin 接口 `/nacos/v3/admin/**` 才需要登录。
3. **没有 beat 接口**：`PUT/POST /nacos/v3/client/ns/instance/beat` 均 501。
   实测：临时实例注册后 **15s 不续期变 unhealthy，30s 被摘除**。

### 修复
- `tools/nacos_client.py` 重写为 **v3/v1 自适应**（`NACOS_API=auto`，也支持强制）：
  * 写操作 `POST/DELETE /instance`（v3 与 v1 参数一致）；
  * 发现 `GET /instance/list`，兼容 v3 `{code,data}` 与 v1 `{hosts}` 两种响应；
  * **保活**：v3 下每 `NACOS_HEARTBEAT_SEC`（默认 5s）重新注册一次（实测可稳定保持
    healthy），v1 仍走 beat；注册/摘除/发现均做了失败降级。
  * 新增 admin 服务列表（需要账号密码时才用），拿不到就返回空。
- `config/base_config.py`：`NACOS_SERVER` 默认改 **`www.joyeuse.cn:9000`**，
  新增 `NACOS_API`、`NACOS_HEARTBEAT_SEC`，并注明「9000 是 API、8085 是控制台」。
- `deploy/.env.example`、`docs/DEPLOY_SPLIT.md` 同步端口与版本差异说明；
  排障表新增「502 = 注册 IP 不可达（本地开发要设 `SERVICE_IP=127.0.0.1`）」。

### 另外
- `.github/workflows/deploy.yml`（VitePress → Pages）改为**仅手动触发**：
  本仓库是 fork、未开 Pages，push 触发每次都失败（`Get Pages site failed`），
  与镜像流水线无关却把 Actions 页面刷红。

### 实测
- 本机起两个服务 → Nacos 里 `mediacrawler-crawler(8001)`、`mediacrawler-library(8081)`
  均为 `healthy: true`，5s 保活生效；网关 `gateway/status` 显示从 Nacos 解析到
  `http://127.0.0.1:8001`，`/crawler/api/*`、`/library/api/*` 均 200。

## [2026-11-04] 变更 前端纳入 compose 构建；CI 改为一个 job 顺序产出两个镜像

### 变更
- **compose 可直接构建**：`docker-compose.split.yml` 两个服务都加了 `build:` 段
  （`target: crawler` / `target: library`，参数 `NPM_REGISTRY`、`SKIP_WEBUI_BUILD`）。
  `docker compose build` 会先跑 Stage 1 的 `npm run build`（前端 → `api/webui/`）再打进镜像，
  镜像名与 CI 一致，本地构建的镜像和服务器拉取的镜像同源。
- `env_file` 改为可选（`required: false`）：没建 `deploy/.env` 时也能 `config` / `build`。
- **CI 由矩阵双 job 改为单 job 顺序构建**：前端只 `npm build` 一次，第二张镜像命中
  同一 GHA cache scope（apt/pip/playwright 层只装一次），两张镜像必然来自同一份前端 + 同一份代码，
  且在同一次运行里一起推送。新增 digest 汇总到 Job Summary。
- `deploy/.env.example`、`docs/DEPLOY_SPLIT.md` 补充构建参数与「fork 仓库需先启用 Actions」说明。

## [2026-11-04] 变更 data 服务更名为 library（内容库），并把 /data 子路径改为 /assets

### 背景
- `data` 这个名字太笼统，看不出到底装的是哪类数据。这个服务承载的是**抓取产出的
  内容资产**：帖子/评论/图片视频、标签词库、菜谱，以及导出与 S3 备份。

### 变更（全链路改名）
| 项 | 原 | 现 |
|---|---|---|
| 应用模块 | `api/data_app.py` | `api/library_app.py` |
| 服务注册名 | `mediacrawler-data` | `mediacrawler-library` |
| URL 前缀 | `/data/api/**` | `/library/api/**` |
| 文件/媒体子路径 | `/data/files`、`/data/stats`、`/data/media` | `/assets/files`、`/assets/stats`、`/assets/media` |
| 镜像 | `mediacrawler-data` | `mediacrawler-library` |
| 环境变量 | `DATA_SERVICE_NAME`/`DATA_BASE_URL`/`DATA_PORT` | `LIBRARY_SERVICE_NAME`/`LIBRARY_BASE_URL`/`LIBRARY_PORT` |
| 跨服务方法 | `discovery.notify_data_service` | `discovery.notify_library_service` |
| 健康检查 | `/data/health` | `/library/health` |

同步改动：`config/base_config.py`、`api/gateway.py`、`api/main.py`（单体入口双挂前缀）、
`api/services/task_scheduler.py`、`webui/src/lib/api.ts`、`webui/src/components/data/platform/shared.tsx`、
`Dockerfile`（target `data`→`library`）、`docker-entrypoint.sh`、`docker-compose.split.yml`、
`deploy/.env.example`、`.github/workflows/build-push.yml`、`docs/DEPLOY_SPLIT.md`、
`tests/test_service_split.py`。

### 修复（顺带发现的真问题）
- `api/routers/story.py` 里媒体/原始文件链接**硬编码** `/api/data/...`：拆分成两个服务后
  这个路径根本不存在（404），图片会全部裂掉。新增 `api/service_paths.py`
  （`library_api()` / `asset_url()` / `media_url()`），统一按 `LIBRARY_API_PREFIX`
  （默认 `/library/api`）拼装，单体与拆分两种部署都能用。
- 新增回归：`test_media_urls_carry_library_prefix`、`test_library_app_routes_are_prefixed_by_service_name`
  断言旧 `/data/api/**` 必须彻底消失。

### 兼容性
- **不保留 `/data/**` 旧地址**（会返回 404），前端已同步改为 `/library/api/**`；
  如果外部有脚本在调旧地址，需要改。Nacos 里旧服务名 `mediacrawler-data` 会随实例下线自动摘除。

## [2026-11-04] 变更 按服务名拆分为 crawler / data 两个独立应用（Nacos 注册发现 + 双镜像流水线）

### 背景
- 原来「爬虫 + 数据增删改查」混在一个 FastAPI 应用里：接口与抓取同生共死，
  没法独立扩缩容/独立重启，也没法分别发版。

### 变更
- **两个独立应用**（同一份代码，不同入口）：
  * `api/crawler_app.py` → `/crawler/api/**`（抓取/调度/账号/抓取配置/日志 WS），默认 8001；
  * `api/data_app.py` → `/data/api/**`（故事/评论/导出/标签/S3/菜谱/配置），默认 8081，
    并**内置网关**：把 `/crawler/**` 按服务名转发给 crawler，同时托管前端静态页。
  * 一体化入口 `api/main.py` 保留，并把同一批路由**额外挂到 `/crawler/api`、`/data/api`**，
    本地单进程开发与线上两服务用同一份前端。
- **Nacos 注册发现**（`tools/nacos_client.py` + `api/services/discovery.py`）：
  零第三方依赖的 v1 OpenAPI 客户端（注册/心跳/摘除/发现/可选鉴权）；
  服务名 `mediacrawler-crawler` / `mediacrawler-data`；Nacos 不可用时自动回退静态地址，
  **不影响可用性**。
- **跨服务调用**：crawler 每轮跑完 `POST /data/api/internal/events`（异步、超时 5s、失败只打日志）。
- **前端**：`webui/src/lib/api.ts` 按 URL 首段自动加 `/crawler/api` 或 `/data/api` 前缀，
  调用方代码不变；WS 改为 `/crawler/api/ws/logs`。
- **Docker**：`Dockerfile` 改为多目标（`crawler` / `data` / `all`），入口脚本按 `APP_ROLE`
  决定是否同容器拉起 worker，并把 stdout 同时写入 `/app/logs/<role>.console.log`。
- **编排**：新增 `docker-compose.split.yml`（两服务 + 日志/环境变量/登录态/数据全部挂载）。
- **CI**：`.github/workflows/build-push.yml`，push 触发矩阵构建两个镜像并推送到
  `ccr.ccs.tencentyun.com/<ns>/mediacrawler-{crawler,data}`；缺密钥时降级为「只构建不推送」。
- 新增 `tests/test_service_split.py`（5 项契约测试）与 `docs/DEPLOY_SPLIT.md`。

### 兼容性
- 旧路径 `/api/**` 在单体入口下仍然可用；拆分部署下需改用 `/crawler/api` 或 `/data/api`。
- `GET /api/config/options` 移入 `config_admin` 路由（crawler 域），接口行为不变。

## [2026-11-04] 变更 抓取执行器与接口进程彻底分离（互不影响重启）

### 背景
- 原来 FastAPI 进程既提供接口，又跑定时调度 + 抓取子进程：重启 API 会打断
  跑了几小时的抓取；起第二个实例还会执行「恢复残留 running 记录」，把对方
  正在跑的记录误标成 failed。

### 变更
- 新增 `worker.py`：独立**抓取执行器**进程（`python -m worker`），独占
  `logs/.worker.lock`（单实例），跑 `task_scheduler` + 抓取子进程，不提供 HTTP。
- `SCHEDULER_IN_API`（默认 **0**）：API 启动时不再把调度循环塞进自身，只确保
  执行器在线（`WORKER_AUTO_SPAWN=1` 时自动 detached 拉起）。设 1 回到旧行为。
- 两个进程只通过「数据库 + 日志文件」通信：
  * 手动触发 → API 往 `scheduled_task_run` 写一条 `status='queued'`；
  * worker 每 tick 拾取 `queued` 记录执行（带同任务幂等保护）；
  * worker 实时日志写 `logs/scheduler_live.jsonl`，`/api/crawler/logs` 跨进程读取合并。
- 新增 `tools/proc_lock.py`：OS 文件锁 + PID 探活（Windows 上不能用
  `os.kill(pid,0)`，会真的杀进程，改走 OpenProcess/GetExitCodeProcess）；
  抓取通道标记 `logs/.crawl_channel.json` 让「API 手动抓取」与「执行器抓取」
  互相看得见，避免两个浏览器同时打同一账号。
- `/api/scheduler/status` 增加 `mode`（worker/inline/offline）、`worker`、`worker_alive`、`queued_runs`。
- Docker：`docker-entrypoint.sh` 在容器内额外后台起一份 worker（`WORKER_ENABLED=0` 可关）。

### 行为
- 重启 API → 执行器与其正在跑的抓取**不受影响**（实测 worker pid 不变）。
- 重启执行器 → 接口不掉线；未完成帖靠断点续抓在下一轮继续。
- 执行器不在线时手动触发会「排队」，等执行器上线后自动执行（不再静默失败）。

## [2026-11-04] 修复 任务仍在执行但执行记录显示失败（第二个后端实例误杀）

### 现象
- 8081 上的爬虫子进程仍在跑（评论持续入库），但最新执行记录显示 failed。

### 根因
- 同一项目目录下还有**另一个后端实例（端口 8082，来自另一个 WorkBuddy 会话）**，
  与 8081 共用同一个 PostgreSQL。
- 它的启动恢复 `task_scheduler._recover_stale_runs()` 会把**所有** `status=running`
  的运行记录标记为 failed（"服务重启，执行中断"）；但 8081 的子进程并没有被杀，
  继续在跑 → 记录失败、任务照跑。
- 另：scheduler tick 报 `asyncpg.ConnectionDoesNotExistError`（连接被中途掐断）。

### 修复
- `api/main.py` 新增**单实例锁**（OS 文件锁 `logs/.api_instance.lock`，进程退出自动释放）：
  第二个实例拿不到锁 → 只提供 API、**不启动调度器**，不会再误杀运行记录。
- `database/db_session.py` 引擎加 `pool_pre_ping=True` + `pool_recycle=1800`，
  防连接被服务端/中间层掐断。
- 注：需重启后端才生效；为避免打断仍在跑的 run#135 子进程，本次未重启 8081。

## [2026-11-04] 修复 跑了几小时却显示「补进 0 条评论 / 无新增刷新」

### 现象
- run#135 实跑 **286 分钟**（≈4.8h），期间评论库真实新增 **~3156 条**，
  但运行记录 `new=0 updated=0`，UI「统计明细」显示「补进 0 条 / 无新增或刷新」。

### 根因
- `get_run_crawl_stats()` / `get_run_crawl_detail()` 只用 `chapter.ext.crawled_at`
  是否落在执行窗内来判定「本轮动过哪些帖」。
- 2026-11-03 起「待补帖跳过详情、直接用存量 token 拉评论」，`crawled_at`
  （只在详情 upsert 时写）不再更新 → 统计认为本轮没动任何帖 → 0。
- 数据其实已正常入库，纯粹是**统计归属口径**问题。

### 修复
- 两个统计函数增加「按 `Comment.created_at` 落在 `[start,end]`」归属：
  有评论入库的 story 即视为本轮动过（`crawled_at` 作补充），取并集。
- 验证：run#135 按新口径回算 = 刷新 4 篇、本轮补进 **3180 条**（如 3240→6128）；
  已回填该历史运行记录的 `stats_json/new_count/updated_count`。回归 51 项全过。

## [2026-11-04] 新增 穿插式抓取（评论翻页穿插详情/回首页滚动，撞 461 先穿插重试）

### 背景（用户判断）
- 风控根因不只是「请求量」，更是「长时间只打同一个接口」的机器画像。真人会在评论
  之间回首页、滚信息流、再看详情。所以应在评论翻页中**穿插其他动作**，撞 461 时
  先穿插几次再重试，而不是直接放弃。

### 变更
- 新增 `anti_risk.human_interlude(page, base_url, scrolls)`：回首页 + 滚动几下
  （无浏览器时退化为短暂休眠）。
- `client.get_note_all_comments` 阶段一：每 N 页评论插入一次
  `_interleave_diversion`（拉一次 `/v1/feed` 详情 + 回首页滚动），打断单一接口画像。
- `core.get_comments` 撞 461：先「回首页+滚动+拉详情 → 重试评论接口」最多 N 次
  （**每次从上次断点续，不重头翻**），全失败才弹窗等人工验证。
- 配置页新增「每N页穿插一次」(默认 4)、「461穿插重试次数」(默认 3)；env
  `XHS_INTERLEAVE_EVERY_PAGES` / `XHS_461_INTERLEAVE_RETRIES`，已播种+注入。

### 验证
- 配置页 4/3，`build_crawler_env` 注入 4/3；回归 51 项全过。保留间隔 35-55s、
  验证码人工等待 300s、窗口必现。

## [2026-11-04] 修正 撞验证码必须弹可见窗口等人工（撤销「快速失败」）

### 用户反馈
- 「为什么要裸 461 快速失败？我要的就是手动输入过验证，但一直没看到验证码弹出来就停止抓取了。」

### 修正
- **撤销**上一条「裸 461/471 快速熔断」：删除 `_is_bare_rate_limit()` 及提前 return。
  撞 461 一律走 `_human_verify`（弹窗 + 等待），不再替用户决定放弃。
- `XHS_CAPTCHA_WAIT_SEC` 30 → **300s**（配置页「验证码人工等待」可调大），留足人工时间。
- **窗口必现**：`_human_verify` 改为**一律另开专用可见窗口**（`headless=False`），
  不再复用不可靠的主浏览器（CDP 回退的无头实例 / 后台窗口，用户常看不到）。
- 专用窗口**继承主浏览器实时 Cookie**（优先于 config.COOKIES），避免弹出未登录页。

### 保留
- 抓取间隔 35-55s（慢速抓全，降低累计请求量）保持不变。

## [2026-11-04] 修复 连续触发 461 风控（慢速抓全 + 裸 461 快速熔断）

### 现象（run#132/133/134）
- 三次评论抓取均 461，**成功篇数 5 → 2 → 0 递减**；run#134 150 篇全失败，
  却跑了 77 分钟。账号在惩罚态被反复捶，越捶越死。

### 根因
1. **间隔太快**：评论翻页 5-15s，小时内可发 240-720 次评论请求，远超小红书
   「累计请求量」阈值 → 必然 461。
2. **撞 461 后仍弹窗等 300s 人工验证**（无人值守时纯空转），期间继续捶号；
   成功数逐轮下降即惩罚升级的表现。

### 修复
- **抓取间隔 5-15s → 35-55s**（配置页 `抓取间隔下限/上限`，env `XHS_SLEEP_MIN/MAX_SEC`），
  把小时请求量压到阈值内，实现「慢速抓全」而非撞线。
- **验证码等待 300s → 30s**（配置页 `验证码人工等待`，env `XHS_CAPTCHA_WAIT_SEC`）；
  并从配置页注入子进程（`build_crawler_env`）。
- **裸 461/471（无验证页）快速熔断**：`_is_bare_rate_limit()` 判定后不弹窗、不等待，
  直接结束本轮；只有真验证码页才走人工验证。杜绝 77 分钟空转。
- 新增 3 个配置项种子（`init_v2 postgres` 播种）+ `runtime_config` 映射 + env 注入。

### 验证
- 配置页显示 35/55/30；`build_crawler_env` 注入 35.0/55.0/30；子进程解析一致；
  裸 461→快速熔断、真验证码→人工验证。回归 51 项全过。

## [2026-11-03] 变更 待补帖直接用存量 token 抓评论，跳过逐篇详情

### 现象
- 待补 157 篇时，仍逐篇 `get_note_detail_async_task` + 每篇 5-15s「详情间隔」，
  光详情就要 ~24 分钟才轮到评论。用户要求直接拉数据。

### 变更（`core.get_account_behavior_notes`）
- 待补帖不再排详情任务，改为收集 `(note_id, xsec_token)` 直接并入
  `batch_get_note_comments`（详情/正文/媒体早已在库，只缺评论）。
- 汇总日志改为「新增详情 N + 待补直连评论 M」；列表为空/页面兜底判断同步修正。

### 兼容（token 可能过期）
- `batch_get_note_comments(..., backfill_ids=)` + `get_comments(..., fallback_detail=True)`：
  待补帖评论失败（RetryError/登录失效）时，回退拉一次该帖详情换新 token 再试一次
  （仅单篇、概率低，不影响其它帖）。

### 验证（实跑 run#132）
- 日志：`新增详情 0 + 待补直连评论 157` → `Begin batch get note comments` →
  `Begin get note id comments`，**详情间隔出现 0 次**，直接拉评论。回归 51 项全过。

## [2026-11-03] 修复 重跑仍在翻整份列表（私有常量 `config._XHS_...` 取不到）

### 现象
- 用户重跑任务 27，日志仍在「点赞列表翻页间隔」逐页翻整个点赞列表，不补未完成帖。
- 日志：`读取已归档列表失败，退回全量模式：AttributeError: module 'config'
  has no attribute '_XHS_COMMENT_BACKFILL_PER_POST'`；`读取待补帖子失败` 同因。
- 后果：`get_archived_content_ids`（决定跳过哪些已完成帖）与 `get_backfill_candidates`
  （选未完成帖）双双崩溃 → 退回「全量模式」→ 每轮从头翻整个列表 + 重抓所有帖。

### 根因
- `config/base_config.py` 用 `from .xhs_config import *` 导出，星号导入**跳过下划线
  开头**的名字；`config/__init__` 也不直接导出 xhs_config。故 `config._XHS_COMMENT_
  BACKFILL_PER_POST` 必然 AttributeError（实测 False）。

### 修复
- `core.py` 改为 `from config.xhs_config import _XHS_COMMENT_BACKFILL_PER_POST`，
  两处改用模块级名字，不再走 `config._xxx`。
- 新增回归 `test_backfill_per_post_constant_not_accessed_via_config_package`。

### 验证（实跑 run#130）
- 日志：`增量：160 篇未达 90%，跳过 97 篇已达标` → `优先续抓未完成帖子 159 篇
  （按 note_id 直连，本轮不翻列表）` → `跳过点赞/收藏列表翻页`，**无 AttributeError、
  无列表翻页间隔**。回归 51 项全过。

## [2026-11-03] 变更 有待补帖时只补它们、不再翻整份列表

### 用户要求
- 「之前抓到的详情评论没达标，下次执行别再把所有帖子读一遍；优先读未完成的帖子，
  没有未完成的了再重新拉取列表。」

### 变更（`core.get_account_behavior_notes`）
- 有待补帖（`backfill_queued>0`）时：本轮**只按 note_id 直连补这些未完成帖**，
  `list_budget=0` 且**跳过整个点赞/收藏列表翻页**，省掉几十页列表请求，
  也避免新帖不断涌入待补池。
- 没有待补帖时：才重新拉列表发现新增。
- 移除旧的「给新增帖预留 1/5 额度」逻辑（与「待补优先、清空再拉列表」相悖）。

### 修复
- `get_backfill_candidates` 排除**没有 xsec_token** 的候选：这类帖按 note_id 直连
  详情会 404，留在待补池会让「有待补」恒成立 → 列表阶段永远不执行 → 新帖永远
  发现不了（死锁）。现在交给「待补清空后重新拉列表」时带新 token 重新发现。

### 测试
- 相关回归 50 项全过；实测当前待补 157 篇均已带 xsec_token。

## [2026-11-03] 变更 移除单轮请求数量上限；根治「已完成帖被反复重抓」

### 背景（用户反馈）
- 「正常爬取就是要慢慢抓取而不是限制抓取数量，不要每次都引入一堆抓取数的限制」。
- 「抓取了那么多批量结果只入库了一条数据？为什么还抓取那么多已抓取的数据？」

### 数据证据（run#127，手动跑点赞备份）
- 本轮更新 100 篇，**净新增评论合计仅 1 条**，99 篇 `gained=0`（早已抓全却被重抓）。
- 真·待补：97 篇已完成（跳过）、158 篇 `fetched=0` 从未抓过评论（声明 500-600 条/篇）。
- 结论：不是「只入库一条」，而是本轮 99 篇已完成帖被重复抓取、净增量极小，还白打
  请求把单轮请求量堆到 461。

### 变更
- 彻底移除「单轮请求数量上限」：`XHS_DETAIL_BUDGET_PER_ROUND` /
  `XHS_COMMENT_BUDGET_PER_ROUND` 的计数与拦截逻辑（client/core）全部删除，
  `build_crawler_env` 不再注入这两个 env；配置项标记「已弃用」仅作展示。
  风控改由「慢速随机间隔 5-15s + 撞 461 退避并断点续抓」承担。
- `XHS_BEHAVIOR_MAX_NOTES` 支持 `0 = 不限制`（默认改 0）：不限制单轮篇数，
  待补篇数与评论条数预算同步按「0=不限」处理，去掉隐式总量上限；靠增量跳过已完成帖
  + 慢速间隔自然收敛，而不是靠硬数量上限截断。

### 修复
- 根因（已完成帖被反复重抓）：`refresh_comment_completeness` 在「已翻到 has_more=False
  抓全」或「已抓 >= 平台声明数」时，把 `declared_comment_count` 永久纠正为真实已抓数，
  完整率置 100%。这样平台声明数虚高（含已删/折叠/私密评论）的帖子不会再被每轮当成
  「未达标」重抓 —— 下一轮必跳过，彻底终结「抓一堆、净增一条」的浪费。

### 测试
- 重写 `tests/test_xhs_comment_budget.py`：锁「预算属性/方法已移除」、「无上限下评论
  必须翻到 has_more=False 抓全」、「build_crawler_env 仍会在任务启动时 refresh()」。
- 评论/行为/调度相关回归 50 项全过。

## [2026-11-03] 修复 评论续抓进度持久化失败（`save_comment_resume` 误传参）

### 现象
- 11-03 任务 27 手动跑（00:35→02:36 CST）撞 461：`评论接口触发风控：CaptchaError`，
  `本轮 100 篇里有 76 篇没抓到评论`。
- 日志每翻一页评论都刷：`[xhs] 续抓进度持久化失败 ...: save_comment_resume()
  got an unexpected keyword argument 'skipped_sub_count'`。

### 根因
- `media_platform/xhs/core.py::_on_progress` 的 `else` 分支调 `save_comment_resume`
  时**多传了 `skipped_sub_count`** 关键字参数；该函数签名只接受
  `(platform_code, content_id, top_cursor, top_count, sub_cursors, reached_end)`，
  多出的参数直接 TypeError → 被 except 兜成 warning，**续抓进度从不落库**。
- 后果：没抓完的帖下轮从第一页重抓，白打请求、加速累计量撞 461。`skipped_sub_count`
  本该只给 `save_comment_skipped_sub`（已在其上方单独调用，位置参数 `skipped`）。

### 修复
- 删掉 `save_comment_resume` 调用里多余的 `skipped_sub_count=int(...)` 三行。
- 新增契约测试 `tests/test_xhs_comment_budget.py::test_save_comment_resume_signature_contract`
  （锁 `save_comment_resume` 不接受 skipped 类参数、`save_comment_skipped_sub` 保留）。
- 评论相关 46 项 + 预算 8 项全过。

### 另：主因是评论预算=0
- 配置页 `xhs_comment_budget_per_round` 当前仍 = 0（不限制）。用户 11-02 主动改成 0，
  等于关掉评论接口硬闸，单轮评论请求累积到 ~90 触发 461。**建议填回 20**（默认）。

## [2026-11-02] 修复 平台声明评论数虚高导致达标帖每轮反复重抓（99% 帖进待补池）

### 现象
- 增量任务把「完成度 99% / 98% / 100%」的帖子也排进待补池反复拉（只补 +4/+3/+2 条），
  用户预期是「≥90% 跳过、低完成度优先」，看到高完成度帖被抓一脸懵。

### 根因
- 完整率判据 `is_comment_complete` 只看 `已抓/平台声明数`。小红书平台声明的评论数
  **普遍虚高**（含已删/折叠/私密评论），实际能翻到 `has_more=False` 抓全的只有更少。
- 旧逻辑：翻到 `has_more=False`（已抓全）时只 `clear_comment_resume`，**没有标记完成**。
  于是 `337/400=84% < 90%` → 判「未完成」→ 每轮重拉 → 白打请求 + 撞 461 风险。

### 修复
- `tools/v2_sync.py` 新增 `COMMENT_FULLY_FETCHED_KEY` 与 `mark_comment_fully_fetched()`：
  评论翻到 `has_more=False` 时打持久化标记（存在 `chapter.ext`，不在会被清的 resume 里）。
- 新增统一判据 `is_comment_complete_full(fetched, declared, ext, target_ratio)`，三条命中
  其一即「完成、可跳过」：①比例达标 ②已标记抓全（视为 100%）③已抓 ≥ 分母（平台少报）。
  `get_archived_content_ids` / `get_backfill_candidates` 两处跳过判定改用它。
- `refresh_comment_completeness` 对已抓全帖直接写 `completeness = 1.0`，前端不再显示「差一点」。
- `media_platform/xhs/core.py::_on_progress`：`reached_end` 时调用 `mark_comment_fully_fetched`。

### 验证
- 新增回归 `tests/test_xhs_comment_completeness.py::test_comment_fully_fetched_skips_next_round`
  / `test_fetched_gte_declared_is_complete`；评论相关 46 项全过。
- 诊断脚本确认：当前待补池「已抓≥分母却仍待补」的假性未达标 = 0 篇，系统为干净稳态。

## [2026-11-02] 修复 单轮预算页改成 0 仍按默认 20 执行（next_task 配置不生效）

### 现象
- 配置页把「单轮评论请求预算」改成 **0（不限制）** 并保存成功（库里 value=0、
  version 递增），但下一轮任务日志仍显示「预算上限 20」，实际仍按 20 截断。

### 根因
- 该配置 `apply_timing = next_task`。`config_admin` 的 PUT/batch 只在编辑了
  **immediate** 项时才调用 `runtime_config.refresh()`（`need_reload` 门控），
  所以 next_task 项改完，`runtime_config` 的内存缓存**仍是进程启动时的旧值**（默认 20）。
- `build_crawler_env` 直接读 `runtime_config.get_int(...)` 这个旧缓存，把 20 注入
  子进程环境变量，于是「改 0 → 仍 20」。凡是 next_task 类的预算/额度都受此影响。

### 修复
- `api/services/profile_crawler.py::build_crawler_env` 改为 `async`，在构造 env
  前先 `await runtime_config.refresh()` 重新从 `crawl_config_item` 拉最新值，
  保证任务启动时读到的是库里的当前值（next_task 语义 = 下一轮生效）。
- 唯一调用点 `_run_once` 改为 `env = await build_crawler_env(task)`。
- 附带收益：所有 next_task 配置项的页面改动都会在本轮任务启动时生效，不再依赖重启。

### 验证
- 改后直接调用 `build_crawler_env({"platform":"xhs"})`：评论预算注入 `'0'`、
  详情预算注入 `'25'`（修复前评论会注入 `'20'`）。
- 子进程侧 `XHS_COMMENT_BUDGET_PER_ROUND=0` → `config.XHS_COMMENT_BUDGET_PER_ROUND = 0`（不限制）。
- 新增回归 `tests/test_xhs_comment_budget.py::test_build_crawler_env_reads_fresh_db_value_for_next_task`
  （断言 refresh 被调用一次，且用刷新后的 0 覆盖旧缓存 20）；`tests/` 相关 13 项全过。

### 提醒
- **0 = 不限制**：评论接口硬闸关闭后，单轮累计评论请求会重新冲到 ~90 并再次触发
  461。若要长期稳定，建议填 **20**（默认）；详情 25 + 评论 20 + 列表 ≈ 9 ≈ 54，留足余量。

## [2026-11-02] 清理冗余旋钮「每篇预留评论额度」

### 背景
- 配置管理页「小红书账号备份」分组里，「单轮评论请求预算」(XHS_COMMENT_BUDGET_PER_ROUND)
  与「每篇预留评论额度」(XHS_COMMENT_BACKFILL_MAX_COMMENTS) 两个旋钮都挂在
  「每轮评论工作量」上，用户误以为重复。
- 二者机制不同但效果重叠：前者是**全局请求次数硬闸**（到数即停，真正防 461）；
  后者只是 **backfill 选帖启发式**（算 `本次抓取上限 × 本值` 决定挑哪些帖，从不干预
  实际请求次数）。当前前者=20（≈最多 20 次评论页就停），后者算出的选帖预算=100×4000=40万
  评论——前者永远先截断后者，后者在当前数值下基本是死配置，连它本想防的
  「单篇巨帖吃光整批额度」也没达成（它算的是整批总量，不过滤单篇）。

### 变更 / 修复
- 删除「每篇预留评论额度」用户旋钮：移除 `init_v2` 种子 + 库行清理、`runtime_config`
  回退键、`config` 的 env 定义。选帖软过滤改为内部常量 `_XHS_COMMENT_BACKFILL_PER_POST=4000`
  （沿用原等价效果，不暴露成旋钮）。`core.get_account_behavior_notes` 两处改用该常量。
- 页面现仅剩三个语义不打架的评论相关旋钮：**备份单次抓取上限**(篇数) /
  **单篇评论硬顶**(单帖深度) / **单轮评论请求预算**(全局硬闸)。
- 回归 17 项测试通过，配置页已确认不再显示该旋钮。

## [2026-11-02] 修复 详情接口仍撞 461（第二道出血点）+ 详情/评论双预算护栏

### 背景（为什么「改了还是一样」又发生）
- 上轮加的 `XHS_COMMENT_BUDGET_PER_ROUND=60` 只罩**评论接口**。但「点赞/收藏备份」
  任务每轮要把账号里**所有点赞帖的详情**都拉一遍（一篇 = 一次 note 详情请求）：
  2026-11-02 09:55 那轮队列里 87 篇 → **87 次详情请求**，单这一项就超过小红书
  单轮累计请求量阈值(~80-90) → **详情接口直接 461**，熔断被置位、评论阶段整批被跳过
  （现象：「没抓取几个评论又触发熔断」——其实是评论阶段根本没开始）。
- 关键修正：小红书限流是**单轮累计请求量**（详情+评论+列表翻页 全算），不是纯速率。
  只限评论、不限详情，等于只堵了一个口子。必须按「接口类型」分别设上限。

### 新增 / 变更
- **双预算护栏**（合计必须 < ~80）：
  - `config.XHS_DETAIL_BUDGET_PER_ROUND`（默认 **25**）：本轮最多拉多少篇帖子详情。
  - `XHS_COMMENT_BUDGET_PER_ROUND` 默认 **60 → 20**（主评论+楼中楼都算）。
  - 外加列表翻页约 5-9 次，合计默认 ≈ 50，留足余量；0 表示不限制（不推荐）。
- `client` 新增详情预算计数与护栏 `_detail_req_tick` / `_detail_budget_exhausted`
  （独立于评论预算，互不干扰，跨运行归零）。
- `core.get_note_detail_async_task` 在详情预算耗尽时**直接跳过该帖**（返回 None，不发起
  请求），剩余帖子靠增量逻辑自然留到下一轮；`get_specified_notes` 走同一方法自动覆盖。

### 修复
- 评论预算默认值 60→20，使「详情25 + 评论20 + 列表≈9 ≈ 54」稳定低于阈值，账号不再进惩罚态。

### 验证
- `tests/test_xhs_comment_budget.py` 扩到 6 项：新增详情预算计数 / 预算耗尽跳过 / 预算0不限。
- 回归：comment_budget 6 + sub_comment_depth 5 + human_order 3 + comment_resume 4 + 评论补抓 1 = 19 项全过（已知 flaky 的 risk_circuit 未纳入）。
- `config` 实测 DETAIL=25 / COMMENT=20。

## [2026-11-02] 移除风控冷却闸口 + 预算配置接入配置管理页

### 背景（用户要求）
- 用户认为「风控冷却」（`risk_cooldown_hours` + 任务选项 `ignore_risk_cooldown` +
  手动运行 `force`）是垃圾限制、一点用没有——撞 461 后冷却期内不让跑，反而把任务
  拖死；而真正能防 461 的是「单轮请求预算护栏」（`XHS_DETAIL_BUDGET_PER_ROUND` /
  `XHS_COMMENT_BUDGET_PER_ROUND`），在撞 461 之前就主动停手。
- 另外用户指出两个预算配置只写在代码里、没进「配置管理」页，无法在界面调。

### 变更
- **彻底移除风控冷却机制**：
  - 删除 `task_scheduler._risk_cooldown_remaining_sec` 及 `execute_task` 内的冷却闸口
    （撞 461 后「几小时内禁止发起」的拦截逻辑）。
  - 删除 `ignore_risk_cooldown` 选项（schema / `_normalize_options` / 前端复选框 /
    `scheduler.ts` 类型 / 测试）；删除 `trigger` 与 `run_task` 的 `force` 参数。
  - 从 `init_v2` 种子 + `runtime_config.LEGACY_FALLBACK` 删除 `risk_cooldown_hours`，
    并在 `_seed` 中清理库中已存在的该死配置项。
  - 平台限流改由单轮请求预算护栏兜底，正常调度不再被「冷却」拦截。
- **预算配置接入「配置管理」页（小红书账号备份 分组）**：
  - `init_v2` 新增种子 `xhs_detail_budget_per_round`（默认 25）、
    `xhs_comment_budget_per_round`（默认 20）。
  - `runtime_config.LEGACY_FALLBACK` 新增对应回退；`build_crawler_env` 注入这两个
    环境变量，使页面改值经 `runtime_config` 落库后**真正传进爬虫子进程**生效
    （之前只在代码里硬编码，页面改了也白改）。

### 验证
- `tests/test_scheduler_options.py` 去掉 `ignore_risk_cooldown` 断言（改用 headless
  等真实字段），6 项全过。
- 后端重启后 `/config/groups` 的「小红书账号备份」分组应出现「单轮详情请求预算 /
  单轮评论请求预算」两项；改值后下一轮爬虫即按新值执行。

## [2026-10-31] 修复 评论接口仍撞 461 的根因 + 新增「单轮评论请求量硬上限」护栏

### 背景（为什么「改了还是一样触发风控」）
- 上轮（`0fa5283` 楼中楼深度上限 + 单轮 40 篇）的**修复根本没在那轮跑过**：
  那一轮的子进程在 **06:50** 就 spawn 出来（每次任务 Popen 现起新进程、读当时磁盘
  代码，单轮跑 ~90 分钟），而深度上限提交于 **07:48**。于是 51 条回复的 root 被翻了
  10 页（5 条/页），整轮请求量爆炸，08:21 撞 461。
- 更深的真因：**小红书对评论接口是「累计请求量」触发限流，不是纯速率**。第一轮
  （07:08→07:32）只翻了 ~90 次评论请求就把账号打挂（81/82 篇没评论），光是头一篇
  热门帖的深层楼中楼就把整轮预算吃光，连第 1 篇都没抓完。5-15s 页间隔压得住速率，
  压不住单轮总量。
- 并发 bug：小红书 461 是**接口层**返回的，浏览器页面永远正常 →
  `anti_risk.wait_for_human_verify` 一律返回 True → `core._human_verify`
  **把 `_risk_abort_reason` 清空**（误判「验证通过」），于是重试又撞 461、同轮后续
  batch（新帖→补抓）继续打评论接口，把账号越推越死。

### 新增
- **`config.XHS_COMMENT_BUDGET_PER_ROUND`（默认 60，0=不限制）**：单轮评论接口
  请求量硬上限（主评论页 + 楼中楼页都算）。达到上限即在撞 461 之前**主动停手**，
  剩余帖子留到下一轮（尊重风控冷却），账号永不进入惩罚态。默认 60 明显低于触发阈值
  ~90；想更快可往上调，但 >90 会重新撞 461。
- client 端计数 + 护栏：`_comment_req_tick` / `_comment_budget_exhausted`，
  跨「同一次运行里的所有批量评论批次」累计（共享账号/IP），跨运行（新子进程）归零。

### 修复
- **熔断不再被误解除**：`_human_verify` 不再无条件清空 `_risk_abort_reason`；
  只有「人工验证后重试接口真正成功」才解除熔断（见 `core.get_comments` 重试成功分支）。
  否则 XHS 无验证码场景会一直误判通过 → 连续撞 461。
- `batch_get_note_comments` 与 `get_comments` 在预算耗尽时直接跳过，不再打接口；
  `_fetch_sub_pages` 预算触顶返回 `last_cursor` 供续抓（属主动停止，非失败）。

### 验证
- 新增 `tests/test_xhs_comment_budget.py` 3 项：楼中楼达预算停止并留游标 /
  主评论达预算阶段1 提前结束 / 预算 0=不限制（同时关深度上限隔离变量）。
- 回归：comment_budget 3 + sub_comment_depth 5 + human_order 3 + comment_resume 4 +
  risk_abort 5 = 20 项全过。

## [2026-10-31] 变更 楼中楼翻页深度上限 + 单轮额度降到 40（砍无效请求）

### 背景（实跑实测，2026-10-31 06:50 那轮）
- `sub/page` **服务端固定只返回 5 条/页**（请求 `num=30` 也只给 5，和主评论固定
  10 条/页同理）。声明 39 条回复的 root 要翻 8 页、51 条要翻 10 页，按 5-15s
  页间隔算**单个 root 就要 1.5-2 分钟** —— 这是请求量爆炸的真凶。
- 旧常量 `MAX_SUB_COMMENT_PAGES = 100` 只防死循环（单 root 最多 500 次请求），
  起不到限流作用。
- 两阶段改造后评论阶段耐受从 9 分钟提升到 24.5 分钟（+170%），说明形态有效，
  但**仍会撞 461** —— 存在与顺序无关的速率/总量上限，只能靠削减请求量解决。

### 变更
- **新增 `config.XHS_SUB_COMMENT_MAX_PAGES`（默认 3，0=不限制）**：单条主评论的
  楼中楼最多翻 3 页（≈15 条回复），超出主动放弃。删除写死的 `MAX_SUB_COMMENT_PAGES`。
- **截断语义（关键，容易写错）**：触顶截断是**主动放弃**而非失败，因此
  `_fetch_sub_pages` 触顶时返回 `None` 游标。若沿用旧逻辑留游标，下一轮只会再翻
  同样的 3 页、永远翻不到底 → 帖子永远凑不满完整率 → 每轮重抓 → 反而打更多请求。
  接口报错中断属「失败」，仍然保留游标供续抓（两者必须区分）。
- **完整率分母扣减**：新增 `tools/v2_sync.save_comment_skipped_sub` +
  `COMMENT_SKIPPED_KEY`（`story_chapter.ext`），把主动放弃的条数从声明数里扣掉。
  `get_archived_content_ids` / `get_backfill_candidates` / `refresh_comment_completeness`
  三处口径统一。否则这些帖永远不达标、每轮白抓。
- **`XHS_BEHAVIOR_MAX_NOTES` 100 → 40**（单轮处理篇数），配合上面砍请求量。

### 验证
- 新增 `tests/test_xhs_sub_comment_depth.py` 5 项：触顶停止翻页 / 触顶不留游标 /
  报错保留游标（与截断相反）/ 0=不限制 / 未触顶时 skipped=0。
- 回归：resume 5 + risk_abort 4 + human_order 3 + completeness 15 + order 8 +
  behavior_paging 13 + scheduler_options 6 = 54 项全过。

## [2026-10-31] 变更 评论抓取改为两阶段「真人顺序」，降低行为画像风控

### 背景
抓取日志出现 `461`×16、验证码×6、`471`×3、`RISK_ABORT`×2。参考小红书留档 skill 的
风控结论：**被判定为机器人的不是签名，而是请求的节奏与形态**。
排查确认间隔本身已是 5-15s 随机（`_xhs_crawl_interval()` 返回区间），问题出在
**接口顺序**：旧实现每翻一页主评论就立刻翻这一页的楼中楼，两个 endpoint 交替
（`page → sub → page → sub…`），服务端看到的就是「顺 cursor 链走到底 + 两端点来回切」。

### 变更（`media_platform/xhs/client.py`）
- **`get_note_all_comments` 拆成两阶段**，贴合真人操作顺序：
  - 阶段 1：只打 `comment/page`，把整篇一级评论翻到底（真人=把评论列表滚到底）
  - 阶段 2：主评论翻完后才切 `sub/page`，逐条展开楼中楼（真人=逐条点「展开 N 条回复」）
  - 请求序列从 `A B B A B B…` 变为 `A A … B B …`
- **新增 `_drain_inline_subs`**：主评论响应内嵌的楼中楼预览零额外请求直接入库，
  并登记 `seen_sub_ids`，阶段二翻页不重复写入。旧逻辑靠 `_fetch_sub_pages` 顺带处理
  内嵌，两阶段后必须在阶段一先落库，否则会漏回复。
- **新增 `_sub_needs_paging`**：阶段一只把「确实还有回复要翻」的主楼留到阶段二，
  `sub_comment_count=0` 且无 `has_more` 的主楼整个跳过——省掉大量无效请求
  （skill 里收益最大的一条）。
- `get_comments_all_sub_comments` 新增 `seen_sub_ids` 参数（跨阶段共享去重集合）；
  并支持 `start_sub_cursors` 里**空游标=从头翻**，已处理过的 root 用 `handled_roots`
  去重，不重复请求。
- **断点续抓兼容**：阶段一每页把累积的 `pending_roots` 以空游标写进 `sub_cursors`
  （并保留上一轮遗留游标），阶段一被中断也不丢楼中楼。

### 验证
- 新增 `tests/test_xhs_comment_human_order.py`（3 项，全通过）：
  请求序列两阶段严格分离 / 无回复主楼不发请求 / 内嵌预览不额外发请求。
- 回归：`comment_resume` 5、`comment_completeness` 15、`comment_order` 8、
  `behavior_paging` 13 —— 全部通过。
- `test_xhs_risk_circuit.py` 2 项失败为**既有问题**：用 `git stash` 去掉本次改动后
  重跑同样超时（exit=124），且该测试用 `FakeClient` 整段替换了 `get_note_all_comments`，
  不受本次改动影响。

### 备注
本次只调整**形态（顺序）**，未改间隔（已是 5-15s 随机）。

## [2026-10-30] 修复 看图弹窗点空白处无法关闭 + 重复检测分组支持整行展开并排看图

### 背景
- 看图弹窗：关闭只能靠右上角 ×，用户反馈 × 不好点中，希望点空白处即关闭。
- 重复检测（存储去重分组）：分组列表没有滚动条，且必须点小三角才能展开，
  无法直观看出「到底是哪几张图片重复」。

### 修复
- **看图弹窗点空白处可关闭**（`components/data/platform/ImageViewer.tsx`）：
  原判断 `e.target !== e.currentTarget` 恒不成立——图片区里有一层 `h-full w-full`
  的居中容器铺满整个区域，点击空白处时 target 是那层容器而非外层容器，
  于是只有 × 能关。改为「点在 `IMG` 或按钮上不关，其余（含空白处）都关」；
  保留拖拽后不误关（`movedRef`）与翻页箭头不误关（`closest('button')`）。
  外层加 `cursor-zoom-out` + `title="点击空白处关闭"` 提示。

### 变更
- **重复检测分组交互改造**（`views/S3BackupView.tsx`）：
  - 整行可点展开/收起（原先只有三角箭头是按钮），内部「整组保留」复选框容器
    加 `stopPropagation`，避免勾选时误触发展开/收起。
  - 展开后组内所有重复副本**横向并排**显示（`flex gap-2 overflow-x-auto`，
    每张 `w-24 / sm:w-28`、不换行、可左右滑动），替代原来 3/4/6 列的小网格，
    一眼看出重复的是哪几张。
  - 点缩略图用 `ImageViewer` 打开大图，并可在该组所有副本间左右翻页对比
    （大图走 `/api/s3-backup/object/{key}` 后端代理，不依赖平台外链）。
  - 分组列表加独立滚动区（`max-h-[320px] overflow-y-auto`），避免展开组过多时
    把下方统计卡片与配置列表挤出可视区。

### 验证
- `npm run build`（含 `tsc -b` 类型检查）通过。
- 后端 `/api/s3-backup/dedup/report`：78 组重复 / 313 冗余对象 / 可收回 13.6MB，
  分组字段（`keys`/`sizes`/`canonical_key`/`content_ids`）与前端渲染所需一致。
- `/api/s3-backup/object/{key}` 返回 200 / `image/webp`，看图器大图可正常加载。

## [2026-10-30] 新增 点击图片应用内弹窗看图（可缩放/翻页，不再跳新标签）

### 背景
此前帖子正文图、评论配图都用 `<a href={m.url} target="_blank">` 在新标签页打开
平台远程原链；小红书图床带时间签名，老链接常已 403，且「点图跳出去」体验差。
需求：点击图片改为应用内弹窗打开，弹窗内可缩放图片大小，不要跳新路径。

### 新增
- **全局看图弹窗 `ImageViewer`**（`components/data/platform/ImageViewer.tsx`）：
  基于 `@radix-ui/react-dialog` 的全屏遮罩 + 暗色舞台。支持滚轮/按钮缩放（50%~600%）、
  放大后拖拽平移、双击放大/复位、多图左右翻页（← → 键 / 左右箭头按钮）、
  点空白处关闭、Esc 关闭。复用与 `MediaImage` 一致的「本地 → 平台远程 → S3 备份」
  候选源重试（某候选 403 自动换下一个）。通过 `ImageViewerContext` 暴露 `open()`，
  在 `App.tsx` 顶层用 `ImageViewerProvider` 包裹全站。
- **`MediaImage` 接入点击看图**：新增 `zoomable`（默认 true）、`gallery`
  （一组图候选源列表，用于弹窗内翻页）、`galleryIndex` 三个 props；点击图片
  `cursor-zoom-in` 并调用 `viewer.open()`。评论配图 `CommentImages` 同步改为
  点击弹出，不再 `<a target="_blank">`。

### 变更
- **11 个平台布局**去掉帖子正文图的 `<a href target="_blank">` 外包装，改为给
  `MediaImage` 传入 `gallery={media.map(m=>m.candidates)}` + `galleryIndex`，
  点击即在应用内弹窗看大图并可翻页（B站/抖音/快手封面图也沿用同一弹窗）。
- **列表卡片封面**（`DataManagementView` / `GuestView`）显式 `zoomable={false}`，
  保留「整卡点击跳详情」行为，不被看图弹窗拦截。
- 「查看原文」外链（`sourceUrl`）与作者主页链接仍按原意新标签打开，未改动。

### 验证
- `npm run build`（含 `tsc -b` 类型检查）通过；grep 确认帖子正文图/评论图已无
  `<a ... target="_blank">` 残留（仅剩「查看原文」/作者主页等有意外链）。

## [2026-10-30] 新增 帖子详情进 URL 路由（刷新不丢）+ 点赞/收藏优先续抓未完成帖

### 背景
1. 帖子详情此前只存在组件内部 state，刷新页面就回到列表，很不方便；
   希望详情有独立 URL，刷新/分享能直接定位到该帖。
2. account 模式（点赞/收藏）抓取每次都从列表第一页翻起，即使大部分帖子
   已归档，也要把整份列表从头扫一遍；应优先续抓「之前没抓完」的帖子。

### 新增
- **前端路由承载帖子详情**：`useHashRoute` 路由格式扩展为
  `#/{view}[/{platform}][/story/{id}]`。打开帖子即把 `storyId` 写进 URL，
  刷新/分享链接可直接恢复到该帖（`#/data/xhs/story/8452`）。
  `DataManagementView` / `GuestView` 的详情由路由驱动（不再用内部 state）；
  「返回列表」= 去掉 URL 的 story 段，列表筛选/分页状态保持不变。
  S3 备份「定位到原贴」事件也改为带 storyId 的跳转。
- **点赞/收藏优先续抓未完成帖**：`tools/v2_sync.get_backfill_candidates()`
  按评论完整率筛出本轮待补帖子，连同 `story_chapter.ext` 里已存的
  `xsec_token` 一起返回；`core.get_account_behavior_notes` 在翻列表**之前**
  直接按 note_id 拉这些帖子的详情+评论，不必先把整份列表翻一遍。
- **列表翻页早停**：新增 `XHS_BEHAVIOR_EARLY_STOP_PAGES`（默认 3）。点赞/收藏
  按时间倒序，连续 3 页无「新增待抓帖」（全已归档/视频）即停止翻页，
  不再每次从头扫完整列表。0 = 关闭早停。

### 变更
- 本轮额度分配：未完成帖优先占用，但给新增帖预留约 1/5（避免待补池长期
  清不空时列表额度恒为 0、新点赞永远进不来）；列表阶段额度 = 总额度 − 续抓数。
- 早停判断抽成纯函数 `core.update_idle_streak()`，便于单测。

### 验证
- `tests/test_xhs_behavior_paging.py` 新增 4 例：空转计数/归零、早停关闭、
  495 条列表不整份扫完（5 页即停）、`xsec_token` 提取回退；全部 13 例通过。
- 真实库验证 `get_backfill_candidates`：待补 100 篇，均带 xsec_token。
- 前端 `npm run build` 通过（tsc 无错）。

## [2026-10-30] 修复 去重执行报错（NameError）+ 分组可展开预览/逐张排除

### 背景
去重面板上线后，点「分析报告」报 `NameError: name 'active_settings' is not defined`——
执行去重后台任务里引用了 `active_settings` 却没导入，失败信息被存进 `_DEDUP_LAST`，
报告接口把它带出来显示。同时用户要求在分组里能展开看每组所有图片、并排除不合适
清理的数据。

### 修复 / 变更
- `api/routers/s3_backup.py`：顶部补 `from tools.media_backup import active_settings`
  （修复执行去重崩溃）；`POST /s3-backup/dedup/run` 新增 `exclude_keys` 入参透传给
  去重逻辑。
- `tools/media_dedup.py`：
  - `analyze_duplicates` 每组明细新增 `sizes`（与 `keys` 一一对应）与 `content_ids`
    （该组涉及的帖/评论去重列表），供前端预览与判断合并范围。
  - `dedup_backups` 新增 `exclude_keys` 参数：这些对象键**绝不**改键、绝不删除（用户
    在 UI 里勾掉的副本）；整组被排除或不足 2 个可合并副本时跳过该组，其余合并照常。
- 前端 `S3BackupView` 去重面板：
  - 「查看分组」改为可展开的手风琴：每组可展开预览**所有图片**（经 `/api/s3-backup/
    object/{key}` 直接代理 S3），并显示涉及 content_id、每张体积。
  - 每张图片带「保留」勾选（精确按对象键排除），也支持「整组保留」；排除状态带
    清空入口；「执行去重」按钮显示已保留数量，并把 `exclude_keys` 传给后端。

### 验证
- `tests/test_s3_dedup.py`：新增「排除指定副本」用例（被排除键不改键/不删、其余合并
  照常、protected 计数正确），共 5 例全通过。
- 真实库只读分析（修复后）：2481 已备份 → 75 重复组、301 冗余对象、约 13.5MB 可收回；
  `last_run` 为 `None`（无旧报错）。

## [2026-10-30] 新增 S3 备份去重（按内容合并重复图片）

### 背景
`media_backup` 的对象键是 `{platform}/{type}/{content_id}/{filename}`，同一张图只要
出现在不同帖子/评论里就被存成 N 个内容一致、键不同的 S3 对象，白白占存储。

### 变更
- 新增 `tools/media_dedup.py`：
  - `analyze_duplicates(session)`：只读，按 `media_backup.checksum`（上传时算的
    sha256）分组，统计重复组数 / 冗余对象数 / 可收回体积，及每组明细。相同
    checksum = 相同字节，物理去重 100% 安全。
  - `dedup_backups(session, settings, dry_run)`：把每组冗余行 `object_key` 改指向
    一个**规范键**（优先复用已有 `dedup/` 键，否则用组内首行现有键），删除其余
    「行数-1」个冗余 S3 对象，保留 1 份，腾出空间。`dry_run=True` 只出报告不改动。
  - 安全：只读 `uploaded` 且有 checksum 的行；删对象前确认无其它行引用；拷贝失败
    的组整体跳过绝不删原对象；幂等（重跑已合并组为 no-op）。
  - `tools/s3_client.py` 新增 `copy_object`（服务端拷贝，带下载重传兜底）。
- `api/routers/s3_backup.py` 新增：`GET /s3-backup/dedup/report`（只读分析）、
  `POST /s3-backup/dedup/run`（`execute:true` 才真改键+删对象，后台任务+记住上次结果）。
- `api/routers/data.py`：`/api/data/media/...` 的 S3 回退匹配放宽成按文件名后缀
  （`/{filename}`），去重后内容寻址键也能命中，不回退 404。
- 前端 `S3BackupView` 新增「存储去重」面板：重复组/冗余对象/可收回体积三卡片 +
  「分析报告」「执行去重」（带确认）按钮 + 分组明细。

### 验证
- `tests/test_s3_dedup.py`：4 例（分组统计 / dry-run 不动数据 / 合并改键+删冗余 /
  幂等重跑），内存 SQLite + monkeypatch S3 客户端，全部通过。
- 真实库只读分析：2472 已备份行 → 69 重复组、295 冗余对象、约 12.4MB 可收回。

## [2026-10-30] 修复 图片显示不出来（多级回退）+ 标签多选 + 标签数排版

### 问题
1. **详情页/列表封面「图片显示不出来」**：
   `INTJ社交指南——亲密关系篇`（story 215）等老笔记的媒体 `local_path` 为空、
   只剩平台CDN外链，而小红书图床是**带时间签名的临时地址**，过期必 403
   （实测 `sns-webpic-qc.xhscdn.com/202610281035/...` → **403**）。
   前端 `pickMedia` 只取单一 `url`（= 这条已死的远程链接），失败就留一张破图，
   **从来不会退回到已经备好的 S3 备份**（`/api/s3-backup/object/...` 实测 **200 /
   image/webp / 17KB**）。列表卡片同理：`cover = cover_local_url || cover_url`，
   `cover_backup_url` 明明有值却被完全忽略。
2. **标签筛选只能单选**：下拉每一项点了即替换，无法叠加多标签。
3. **标签数量换行**：`<span class="block truncate">名称</span>` 后接数量 span，
   数量被挤到下一行，扫读时很别扭。

### 变更
- 新增 `components/data/platform/shared.tsx::MediaImage`：按**候选源链逐个重试**
  （本地副本 → 平台远程 → S3 备份），全部失败才回调 `onFailed` 交给上层退化成
  文字封面；配套的 `pickMedia` 现在返回 `candidates`，新增 `pickCoverMedia()`
  统一处理 `cover_local_url → cover_url → cover_backup_url → 首图`。
- 10 个平台布局（xhs/zhihu/weibo/bili/dy/ks/tieba/twitter/facebook/reddit）、
  评论配图 `CommentImages`、列表/访客网格卡片、`Bili/Douyin/Kuaishou` 竖版封面
  全部改用 `MediaImage`，不再有裸 `<img src={单源}>`。
- 标签筛选改**多选**：`tag` state → `tags: string[]`，勾选即生效且弹层不关闭，
  按钮显示「首个标签 +N」与数量徽标，支持一键清空；多标签时可切换
  **任一（OR）/ 同时满足（AND）**。
- 标签行排版修正：`Checkbox + 名称(flex-1 truncate) + 数量(右)` 单行 flex，
  数量固定右对齐不再换行（与发帖人筛选下拉一致）。
- 后端 `GET /api/stories` 支持多标签：`tag` 逗号分隔 + `tag_match=any|all`；
  改用 **EXISTS 子查询**替代原来的 join（join 会让多标签命中同一帖时重复出行、
  计数虚高）。单标签语义不变，向后兼容。
- 前端兼容未重启的老后端：多标签先走服务端（`tag=a,b`），老后端返回空时自动
  退化成「按标签分别取回 + 本地合并/排序/分页」（`queryStories`），
  **无需重启也能用**；后端重启后自动走服务端那条路。

### 验证
- `npm run build`（tsc -b + vite build）通过。
- 直连真实库校验新 SQL（`list_stories` 单测式脚本）：
  单标签 INTJ=7、mbti=5；`INTJ,mbti` OR=11（=并集）、AND=1（=交集，story 215）；
  不存在标签=0；分页两页无重叠。
- 旧后端行为确认：`tag=INTJ,mbti` → total 0（触发前端兼容合并），`tag=INTJ` → 7。
- 后端本次**未重启**（正在抓取）；前端产物由 StaticFiles 按请求读盘，
  构建完刷新页面即生效。



### 问题
用户反馈两点：

1. **评论抓取每篇只抓一点**：执行记录里 99 篇帖子是
   `0%→40% (669/1683)`、`0%→1% (11/1285)`、其余 **97 篇全 0%**。
   原因是原先按列表原始顺序串行抓，中途被风控打断时，每篇都只抓了个开头，
   一轮下来没有任何一篇真正抓完，下一轮还得从头再摊一遍。
2. **撞验证码时浏览器没弹出来（或一闪就关）**：日志里只有
   `人工验证后评论重试失败`，**没有任何弹窗记录**，说明弹窗链路静默失效：
   - `_is_headless_now()` 只看配置推断。CDP 配了 `XHS_CDP_HEADLESS=False`，
     但实际回退到标准模式时 `headless` 由 `config.HEADLESS` 决定（可能是 True）
     → 误判「窗口可见」→ 既不置前也不另开窗口，用户什么都看不到；
   - `_human_verify` 里 `verified` 未初始化，弹窗流程一旦抛异常会变成
     `UnboundLocalError`，**把真实错误盖掉且不留任何日志**；
   - 无验证页时人工窗口只有 45s，且页面本身没有任何提示（461 是接口层返回，
     页面看上去完全正常），用户根本不知道要做什么；
   - 重试失败后没有重新熔断，于是下一篇又白撞一次 461（`6acca231` 只抓到 1%）。

### 变更
- **评论抓取改为「先排序、再一篇抓到底」**：新增
  `tools/v2_sync.get_comment_progress()` 取「已抓/声明」，`core.comment_fetch_order()`
  按「完成度降序 → 评论区小的优先」排序，快收口的先抓完落库；
  未完成的靠断点续抓继续，不再被摊薄。
- `_is_headless_now()` 优先用启动时记录的**实际** headless（`_browser_headless_actual`）。
- `_human_verify`：主窗口置前失败（找不到窗口句柄）时自动改开专用可见窗口；
  每一步都写日志；`verified` 预置 `False` 并兜底捕获异常，弹窗失效不再静默；
  弹窗配额改为「确认有可操作窗口后才占用」，起浏览器失败不会白白耗掉本轮机会。
- `wait_for_human_verify`：无验证页时人工窗口跟随 `timeout`（默认 300s，
  不再 45s 就走），每分钟提醒一次；新增页面顶部红色横幅（`_show_banner`）
  明示「现在需要你做什么」；轮询间隔下限保护，避免 `poll_sec=0` 死循环。
- 人工验证后重试**仍被风控**时重新 `_abort()`，不再多撞下一篇。

### 验证
- `TaskOptions` 无关；本次为爬虫侧改动。
- 新增 `tests/test_xhs_comment_order.py`（8 条）：排序优先级 4 条 +
  实际 headless 判定 + 横幅健壮性 + 人工窗口时长跟随 timeout。
- 相关回归 **33 passed**（评论排序 / 验证码弹窗 / 风控传播 / 分页休眠 / 任务选项）。
- 真实库验证 `get_comment_progress` + 排序：
  `55/55`、`439/437`、`1761/1760` 等已完成帖排最前，`0/708`、`0/7510` 排最后。
- 后端已重启（8081，HTTP 200）。

## [2026-10-29] 修复 任务选项保存不生效：入参模型白名单吞字段

### 问题
用户在「定时任务 → 抓取选项」勾选「忽略风控冷却」后点保存，**界面刷新后仍是未勾选**。

排查确认：`api/schemas/scheduler.py::TaskOptions` 是**入参侧的第一道白名单**，
pydantic 默认 `extra="ignore"`，模型里没声明的字段在请求进入路由**之前**就被丢弃，
`_normalize_options` 再改也无济于事。

连带发现更严重的问题：**`max_notes` 同样被丢弃**。也就是说之前那轮
「`_normalize_options` 白名单补上 max_notes」的修复**实际从未生效** ——
用户在「本次抓取上限」里填 100，存进库的永远是 `null`，任务一直按配置默认值跑。
这正是「设了上限却只更新 20 篇」的真正根因。

### 变更
- `TaskOptions` 显式声明 `max_notes` / `mode` / `ignore_risk_cooldown`，
  并设 `model_config = ConfigDict(extra="allow")`，杜绝以后再静默吞掉新选项。
- `_normalize_options` 末尾把未识别的键原样透传（schema → 归一化 → 落库三层，
  任何一层吞字段，表现都是「前端改了保存没变化」且完全无报错）。
- 新增 `tests/test_scheduler_options.py`（6 条），覆盖：已知字段穿透、未知字段透传、
  默认值语义（`False` / `None`）、create/update 两个入口、以及模拟前端 PUT 的全链路。

### 验证
- `TaskOptions(ignore_risk_cooldown=True, max_notes=50)` → 两字段均保留（修复前均为丢失）。
- 端到端：`PUT /api/scheduler/tasks/27` 保存 `ignore_risk_cooldown=true` → 返回 `True`，
  重新 `GET` 仍为 `True`（存档为 `False` 时即用户遇到的现象）。
- 相关回归 25 passed；后端已重启。

## [2026-10-29] 修复 风控冷却豁免开关实际未生效（ignore_risk_cooldown）

### 问题
上一版冷却闸口的提示写着「确需立刻跑，在任务选项里设 `ignore_risk_cooldown=true`」，
但**代码里从来没读过这个开关**（全仓库只有那行日志文案提到它），
用户在配置页也找不到 —— 它本来就不是全局配置项，而是任务级选项。

另外 `_normalize_options` 是固定白名单，未知键会被静默丢弃，
所以即便前端传了这个字段也存不住（和当年 `max_notes` 被吃掉是同一个坑）。

### 变更
- `api/routers/scheduler.py::_normalize_options` 显式保留 `ignore_risk_cooldown`；
- `api/services/task_scheduler.py` 冷却判断前真正读取该选项：为真则
  `cooldown_left = 0` 放行，并在任务日志里写明「原本还需 X 小时，本次强制执行」，
  用户能清楚知道自己在绕过什么；
- `POST /api/scheduler/tasks/{id}/run` 新增 `force` 查询参数：
  手动「立即运行」属于明确人工意图，`?force=true` 一次性豁免冷却，
  **只影响本次触发、不落库改任务配置**（`trigger(force=)` → `_execute(force_ignore_cooldown=)`）；
- 前端「定时任务 → 抓取选项」新增**「忽略风控冷却」勾选框**（中英文案齐全），
  悬停提示说明了风险和「用完记得取消」。

### 验证
- `_normalize_options({'ignore_risk_cooldown': True, 'max_notes': 50})` →
  两个字段都被保留；不传时默认 `False`。
- `GET /openapi.json` 确认 `/api/scheduler/tasks/{task_id}/run` 参数含 `force`。
- 后端已重启（8081，HTTP 200），前端已重新构建进 `api/webui/`。

## [2026-10-29] 新增 撞验证码时主动弹出浏览器让人工过验证

### 问题
撞 461/471 后虽然写了「等用户人工验证」，但实际从来没弹出过窗口：
- `get_note_detail_async_task` 的内层 `except` 先把 `CaptchaError` 吃掉
  （`_abort` + `return None`），外层那段 `wait_for_human_verify` 是**死代码**；
- 即便走到了，`wait_for_human_verify` 也是拿停在首页的 `context_page` 直接判断
  「页面有没有验证码」——页面本来就没有验证页（461 是接口层返回的），
  于是判定通过/放弃，用户完全不知道发生过什么；
- 无头模式、无浏览器模式下 `context_page is None`，直接返回 False。

### 变更
- `tools/anti_risk.py::wait_for_human_verify` 重写为「真的把窗口摆到用户面前」：
  - **OS 级窗口置前**（新增 `focus_browser_window()`，`ShowWindow(SW_RESTORE)`
    + `SetForegroundWindow`，因为 `bring_to_front()` 只激活标签页，
    窗口最小化/被覆盖时用户仍然看不见）；
  - 支持 `url` 参数：主动导航到**出事的那一篇笔记**，让验证页/登录墙真的显现；
  - 先给 `grace_sec`（默认 8s）渲染宽限，避免「页面还没出来」被误判成已完成；
  - 页面全程无验证码时不再直接放弃：留出 `manual_window_sec`（默认 45s）
    人工窗口，然后按「浏览器正常、httpx Cookie 不同步」返回 True；
- `media_platform/xhs/core.py` 新增 `_human_verify(url)` 统一入口：
  - **每轮只弹一次**（`_human_verify_tried`），不会连着几篇 461 反复打断；
  - 无浏览器模式**或主流程无头**时，临时拉起一个可见浏览器
    （`_spawn_verify_browser`）专供人工操作，用完关闭并 `playwright.stop()`；
  - 验证后**回灌 Cookie**：浏览器 → httpx（`update_cookies`）+ 账号池
    （`sync_login_to_pool`）。用户过验证/重新登录的结果只存在于浏览器里，
    不同步的话 httpx 还拿旧 Cookie 继续撞 461；
  - 验证通过 → 解除熔断、当场重试当前那篇笔记/评论；再次 461 则真熔断。
- 三处失效的调用点（详情内层、评论 CaptchaError/IPBlock、搜索/作者页）全部
  改走 `_human_verify`。

### 验证
- 新增 `tests/test_anti_risk_verify.py`（6 条）：无页面直接 False、验证页必须
  消失才通过、无验证页走人工窗口后返回 True、必须导航到出事 URL、超时返回
  False、窗口置前在任意平台不抛异常。
- 连同 `test_xhs_risk_abort.py` / `test_xhs_behavior_paging.py` 共 **19 passed**。
- 实测：`_spawn_verify_browser()` 能拉起可见窗口，`focus_browser_window()`
  返回 True，首页标题正常 `小红书 - 你的生活兴趣社区`，`page_has_captcha=False`
  ——说明当前账号在**浏览器侧**是好的，461 只出现在 httpx 接口层，
  这正是「同步 Cookie 再重试一次」要解决的问题。

## [2026-10-29] 修复 风控反复触发：461 被吞 + 无跨轮冷却

### 现象
定时任务 #27（account 点赞备份）一天内连续三轮撞 461 验证码：
`RUN 99`（12:46，显示 success）→ `RUN 100`（16:39 熔断，95 篇评论未抓）
→ `RUN 101`（17:29 又是 461，43 篇后整轮报废）。

### 根因（两层，缺一不可）
1. **461 被 `except Exception` 吞掉，根本没熔断**（真凶）
   `media_platform/xhs/client.py::get_comments_all_sub_comments` 对每条根评论的
   楼中楼抓取用裸 `except Exception` 兜底 → `CaptchaError` 被当成「单条评论处理失败」
   → `continue` 到下一个根评论，**零间隔再打一发**。
   日志实测 13:57:45 同一秒连撞 3 条 461；整批不熔断，
   `_risk_abort_reason` 永远不置位 → RUN 99 出现 45 条 461 仍被判 **success**，
   下一轮自然毫无防备地继续跑，把账号越推越死。
2. **没有跨轮冷却**：RUN 100 熔断后仅 32 分钟又开跑（RUN 101），
   平台惩罚窗口以小时计，43 篇就被二次拦住。

### 修复
- `client.py::get_comments_all_sub_comments`：
  风控异常（`CaptchaError` / `IPBlockError` / `PlatformAccessError`）**一律 re-raise**，
  交给 `core.get_comments` 熔断整批；普通异常才跳过该根评论，且**先退避再继续**。
- `client.py::_fetch_sub_pages`：`DataFetchError` 分支补退避，避免零间隔连打。
- `client.py::get_self_user_id`：身份探测阶段撞风控立即上抛，不再依次试
  v1/v2 user/me → selfinfo → mentions（那等于 3-4 发无效请求）。
- **风控冷却闸口**（新增）：命中验证码/限流熔断后，同平台 + 同目标
  （account 任务的 target 即账号池 ID）在 `risk_cooldown_hours`（默认 6 小时）
  内不再发起任务，直接记 `skipped` 并说明原因。
  - 配置项 `risk_cooldown_hours` 落在「代理与风控」分组，前端可编辑；
    `0` = 关闭冷却；任务选项 `ignore_risk_cooldown=true` 可强制本次执行。
  - 实现：`task_scheduler._risk_cooldown_remaining_sec()`，读
    `scheduled_task_run` 最近一条含「触发平台风控」的记录算剩余时间；
    判断失败按「不冷却」放行（绝不因冷却逻辑拦住正常调度）。

### 验证
- 新增 `tests/test_xhs_risk_abort.py`（4 条）：风控异常必须穿透（Captcha/IPBlock）、
  部分成功后撞风控仍整批上抛、普通异常跳过但每次失败都退避；连同分页回归共 **13 passed**。
- 冷却闸口直调：`#27 → 5.86h`，#27 同平台不同 target → `0`，其它平台 → `0`。
- 端到端：手动触发 #27 实际落 `RUN 102 = skipped`（「风控冷却中（还需约 5.8 小时）」），
  **没有起爬虫**；重启 8081 后 `/api/config/groups` 可见 `risk_cooldown_hours = 6 小时`。
- 数据修正：`RUN 100/101` 的 `error_message` 由「内容已设为私密」「服务重启中断」
  改回真实原因（触发平台风控 / 461 熔断），冷却判定与历史复盘才有据可依。

## [2026-10-29] 优化 列表翻页休眠：末页不再白睡，风控熔断不再自动重试

### 修复：跳过视频帖/已归档帖后仍在随机休眠
- 现象：account 备份任务（#27）列表阶段日志里「跳过视频帖」成片出现，
  但仍每页睡 5-15s；第 50 页回来（`has_more=False`）后又睡了 12.81s
  才打印「抓取结束」——这段完全是白等。
- 排查：实时日志交叉比对「跳过视频帖」ID 集合与「排进详情」ID 集合，
  **交集为 0**，说明视频帖过滤本身是对的，问题只在休眠时机。
- 根因（两处）：
  1. 休眠判断在循环**末尾**执行，未考虑本页回来时翻页其实已经结束
     （`has_more=False` 或额度已满），末页固定白睡 5-15s；
  2. `queued_this`（本页排进详情队列的帖子数）只在 `if fresh:` 分支里
     重置，某页去重后为空（点赞/收藏交叉重复）时会沿用上一页的值，
     导致该睡不睡 / 不该睡却睡。
- 修复（`media_platform/xhs/client.py::get_all_behavior_notes`）：
  1. 末页不睡：`more_pages = has_more and got_this < max_notes` 为假时跳过休眠；
  2. `queued_this` 提到每页开头重置，默认 0（本页无新帖=无抓取目标），
     仅旧式 bool 回调才置 `None`（未知，保守休眠）；
  3. 休眠日志带上本页待抓篇数（`（本页待抓 N 篇）`），一眼能看出为什么睡。

### 修复：风控熔断后仍自动重试，等于刚踩刹车又踩油门
- 现象：17:29 因 `CaptchaError` 熔断结束后，17:30 立刻又起一轮重试，
  再次撞验证码；账号已处于风控态，白打请求只会越推越死。
- 修复（`api/services/profile_crawler.py::execute_task`）：结果为
  「触发平台风控」的失败**不再自动重试**，直接收手等下次排期
  （留出冷却/人工过验证码的时间），并在日志里明示。

### 验证
- `tests/test_xhs_behavior_paging.py` 新增 3 条回归：末页不睡、整页无抓取
  目标不睡、中间空页不继承上一页 `queued`；全部 **9 passed**。
- 重启 8081 后端；被中断的 RUN#101 由启动自愈标为「服务重启，执行中断」，
  #27 状态归位 `idle` / `last_status=failed`，下次排期 11-02。

## [2026-10-29] 修复 定时任务「目标失效自动停用」误杀 account 备份任务

### 修复：自动停用逻辑误判 + 误停整个备份任务
- 现象：定时任务 #27（account 点赞备份）日志报「风控熔断 CaptchaError」，
  但定时任务页显示「内容已设为私密（已自动停止该定时任务）」，并整任务被停用。
- 根因：`api/services/task_scheduler.py::_finalize` 对所有失败任务扫描**整段
  200 行日志尾**做子串匹配，命中了某篇公开笔记正文里的「私密马赛克」玩梗文案
  （`私密` 二字），误判为「目标已私密」→ 自动停用任务，并把真实错误覆盖成「私密」。
- 修复：
  1. 自动停用只对**单目标关注**类任务生效（`task_type` 为 `note` / `profile`）；
     `account` 一次爬几十上百篇，单条被删/私密是常态，绝不再因此停用整任务。
  2. 保留对 `note`/`profile` 的原有判定（不回归），仅排除多条目维度。
- 数据修正：把被误停的 #27 恢复 `enabled=True`，清除 `auto_stopped` 标记，
  并把被覆盖的 `last_error` 改回真实原因（风控 CaptchaError 熔断）。
- 验证：`py_compile` 通过；重启 8081 后 API 确认 #27 已启用、`auto_stopped=None`。

## [2026-10-29] 配置管理页：把 XHS 备份参数纳管进前端可编辑的配置项

### 新增：补齐「配置管理」前端页，暴露此前不可见的运行时配置
- 背景：此前新增的小红书账号备份参数（`XHS_BEHAVIOR_MAX_NOTES`、
  `XHS_COMMENT_BACKFILL_MAX_COMMENTS`、`XHS_BEHAVIOR_INCREMENTAL`、
  `XHS_COMMENT_MAX_PER_NOTE`、`XHS_COMMENT_COUNT_MARGIN`）只写在
  `config/xhs_config.py` 里、走 env/常量，既没进 `CrawlConfigItem` 表、也没被前端展示，
  改了还得动 .env 重启。而 `api/routers/config_admin.py` 这套「DB 纳管 + 版本化编辑」后端
  早已存在却**从未接前端 UI**（连原有的 35 项也看不到）。
- 做法：
  - 后端：`database/init_v2.py` 新增分组 `xhs_backup`（小红书账号备份）并注册上述 5 项
    （带 label/描述/默认值/范围/生效时机）；`config/runtime_config.py::LEGACY_FALLBACK`
    补 5 条 key→常量映射，保证老库未播种时行为不变；
    `media_platform/xhs/core.py` 改从 `runtime_config` 读取这 5 个参数
    （默认回退到模块常量，行为不变，但前端改完下次任务即生效）。
  - 前端：新增 `webui/src/views/ConfigAdminView.tsx`，消费 `/api/config/groups` 并按分组
    渲染 int/decimal/bool/enum/string 内联编辑（带范围/正则校验、乐观锁版本冲突提示、
    高级项开关、生效时机徽标）；`api.ts` 加 `configAdminApi`；`Sidebar`/`App`/`useHashRoute`
    接入 `config` 视图（导航「配置」按钮，`nav.config` 文案早已存在）。
- 验证：`init_v2 postgres` 播种 1 分组 + 5 项；`/api/config/groups` 含 `xhs_backup`；
  PUT 编辑接口改值/回退正常；`runtime_config.load()` 能读到 DB 值。

### 修复：配置页打开后显示「暂无配置项」（拿不到数据）
- 根因：本项目 `axios` 拦截器**不拆包**，`response.data` 是信封
  `{code,message,data,request_id}`，业务数据在信封的 `data` 里（其它正常页如
  `HotTimelineView.tsx` 都是 `.data.data` 取值）。但 `configAdminApi` 的 `getGroups`
  只返回了一层 `response.data`（信封），页面再 `.groups` 落到 `envelope.groups`
  （不存在）→ 渲染成「暂无配置项」。
- 做法：`configAdminApi.getGroups/updateItem` 改为 `.then((r) => r.data.data)` 直接返回
  信封内的业务数据；`ConfigAdminView` 的 `queryFn` 去掉多余的 `.data`。

## [2026-10-29] 修复 account 备份任务「只拉 9 个帖子」

### 修复：点赞/收藏备份每轮只重抓 ~8~9 篇，其余 160+ 篇被「假装已归档」永久跳过
- 现象：任务 #27（account / 备份点赞贴）增量运行，每轮新增 0、更新 8~9 篇，
  长期卡在「只拉 9 个」；库内 xhs 246 篇笔记里 **151 篇评论数为 0（评论完全没备份到）**，
  未达标 168 篇、待补评论总量 68 万条。
- 根因：`media_platform/xhs/core.py::get_account_behavior_notes` 调
  `get_archived_content_ids` 时，把评论「条数」预算写成死值
  `XHS_COMMENT_BACKFILL_MAX_COMMENTS=3000`。`plan_backfill` 按评论缺口升序选帖，
  3000 条缺口预算只够挑 ~8 篇；其余未达标帖被「顺延」加进 `known_ids` 当已归档跳过，
  于是每轮只重抓 8~9 篇，其余永远轮不到。
- 做法：评论条数预算改为 **随「本次抓取上限」(XHS_BEHAVIOR_MAX_NOTES) 联动** =
  本次抓取上限 × `XHS_COMMENT_BACKFILL_MAX_COMMENTS`（重定义为「每篇预留评论额度」，
  默认 4000），使每轮选中的待补批次 ≈ 本次抓取上限（默认 100）。死值 3000 不再卡死批次。
- 说明：笔记正文早已全部入库（246 篇），缺的是评论；该预算只决定「本轮挑哪些帖重抓」，
  真正抓几条评论仍由 `XHS_COMMENT_MAX_PER_NOTE` 管。任务 #27 超时本就是 86400s（24h），
  一个批次能跑完一大批，无需为本次调整超时（仅把同样常超时的 #25 profile 任务提到 7200s）。

## [2026-10-29] 优化：点赞/收藏列表翻页随机休眠改为「有评论抓取才睡」

### 变更：列表翻页仅在「本页有要抓详情+评论的帖子」时才随机休眠
- 背景：account 备份任务在 `get_all_behavior_notes` 的列表翻页循环里**每一页**都调
  `anti_risk.interval_sleep`（默认 5-15s 随机）。增量模式下绝大部分页是已归档帖
  （视频帖/`known_ids` 命中），本页没有任何「详情+评论」抓取目标，却照样每页白睡
  5-15s（如 28 页约 4~5 分钟纯等待），纯属浪费。
- 做法：
  - 回调契约升级：`core.py::_on_batch` 返回值由 `bool` 改为
    `dict {"stop": bool, "queued": int}`，`queued` 为本页真正排进抓取队列的帖子数
    （视频帖/已归档帖不计）。旧式返回 `True/False` 的回调仍兼容（保守按需要休眠）。
  - `client.py::get_all_behavior_notes` 仅在 `queued > 0`（本页确有评论抓取目标）时才
    做随机休眠；`queued=0`（整页已跳过）直接跳过。无抓取目标就不为节流白白等待。
- 验证：`tests/test_xhs_behavior_paging.py` 6 项全过（含新增 dict 契约回归：
  `queued=0` 的已归档页不停止翻页、仍能翻过老帖继续往后走）。
- 注意：纯后端逻辑改动，**需重启爬虫/调度进程才生效**（运行中的进程加载的是旧代码）。

## [2026-10-29] 统一「本次抓取上限」：补抓与新帖共用一个上限

### 变更：删除冗余的 XHS_COMMENT_BACKFILL_MAX_NOTES，补抓篇数上限并入「本次抓取上限」
- 背景：UI「本次抓取上限」(任务选项 `max_notes`) 落到爬虫侧是 `XHS_BEHAVIOR_MAX_NOTES`
  （默认 100，管新帖）；而评论补抓另有一个独立变量 `XHS_COMMENT_BACKFILL_MAX_NOTES`
  （默认 20，管待补老帖）。两者各算各的 —— 任务不填 `max_notes` 时，补抓回落到 20，
  于是出现「上限设了 100、一轮却只补 20 篇就停」的歧义（这正是 run#96/98 只更新
  十几篇的原因）。
- 做法：删除 `XHS_COMMENT_BACKFILL_MAX_NOTES`，`config/xhs_config.py` 与
  `media_platform/xhs/core.py` 的 `get_archived_content_ids` 调用统一改用
  `XHS_BEHAVIOR_MAX_NOTES`；`api/services/profile_crawler.py::build_crawler_env`
  只在 `max_notes` 有值时设置 `XHS_BEHAVIOR_MAX_NOTES`（不再单独设补抓额度）。
- 保留 `XHS_COMMENT_BACKFILL_MAX_COMMENTS`（评论「条数」总预算，默认 3000）作为
  防马拉松会话的第二道闸 —— 它与「篇数上限」是不同维度，不归到「本次抓取上限」字段。

### 验证
- `py_compile` 三个改动文件通过；全仓已无 `XHS_COMMENT_BACKFILL_MAX_NOTES` 代码引用
  （仅历史 CHANGELOG / 注释保留）。

## [2026-10-29] 评论抓取支持「断点续抓」

### 新增：评论分页进度持久化与断点续抓
背景：抓取大量帖子时按评论量由少到多排序，评论量大的帖子（动辄数万条）单次会话
可能持续数小时，一旦被中断（风控熔断 / 子进程被杀 / 手动停止），下一次执行会从头
重翻该帖全部评论 —— 既浪费额度又拉长会话、更易触发风控。

- DB 不新增表：续抓进度复用 `story_chapter.ext["comment_resume"]`，结构
  `{top_cursor, top_count, sub_cursors, reached_end, updated_at}`，幂等可回退。
- `tools/v2_sync`：
  - `get_comment_resume / save_comment_resume / clear_comment_resume`
    —— 读写/清除续抓进度（查不到/写失败都静默降级，不影响主流程）。
- `media_platform/xhs/client.py`：
  - `get_note_all_comments` 新增 `start_cursor / top_count_in / sub_cursors /
    progress_callback` 参数：从「上次中断处」继续，而非重头翻；每翻完一页调一次
    `progress_callback` 持久化，翻到 `has_more=False` 时标记 `reached_end`。
  - 抽出 `_fetch_sub_pages`（单根楼中楼翻页），`get_comments_all_sub_comments`
    改为返回 `(评论列表, open_sub)`，`open_sub` 记录「没翻完的根评论游标」供续抓
    收尾；支持 `start_sub_cursors` 从指定游标续抓楼中楼。
- `media_platform/xhs/core.py`：
  - `get_comments` 抓取前读 `get_comment_resume` 作为续抓起点；抓取中经
    `progress_callback` 持久化；`reached_end` 时 `clear_comment_resume`（抓全），
    风控熔断/异常中断时不清除（保留最近一页游标）。评论按 `source_comment_id`
    upsert，中断重翻同页不产生重复数据。

### 测试
- 新增 `tests/test_xhs_comment_resume.py`：①顶层评论多轮中断（每轮 2 页）累计抓全
  120 条、不重复不漏、不重头翻；②楼中楼续抓原语（中断返回最后有效游标、续抓收尾
  无重复）；③续抓进度助手读写 `ext` 正确。
- 同步修正 `test_xhs_risk_circuit.py` 两个 fake client 的 `get_note_all_comments`
  签名（兼容新增 kwargs）。

### 验证
- `test_xhs_comment_resume.py` / `test_xhs_comment_completeness.py` 20 项全过；
  `test_xhs_risk_circuit.py` 3 项全过。

## [2026-10-29] 执行记录展示统计明细 + 修复「抓取上限形同虚设」

### 修复：抓取上限设了 100，却只更新 20 篇就停
- 两条额度互不相干：任务选项 `max_notes` 只注入 `XHS_BEHAVIOR_MAX_NOTES`
  （**新帖**额度），而控制「已入库但评论未补齐的老帖每轮补多少」的
  `XHS_COMMENT_BACKFILL_MAX_NOTES` 一直取配置默认值 **20**，不受任务选项约束。
- 结果：本轮没有真正的新帖时，唯一有活干的就是补抓通道，而它一轮最多 20 篇 →
  现象就是「上限设了 100，更新 20 篇就结束」。
- `build_crawler_env`：任务选项 `max_notes` 现在**同时**注入补抓预算，
  「抓取上限」才名副其实。

### 新增：执行记录下拉显示「新增/更新了哪些帖子 + 前后补齐率」
- DB：`scheduled_task_run.stats_json`（JSONB），走 `init_v2.ADD_COLUMN_SQL`
  幂等补列，老记录为 NULL、前端有空值兜底。
- `tools/v2_sync`：
  - `get_completeness_snapshot(platform)` —— 抓取**前**拍完整率快照。
    必须在起子进程之前调用，抓完会被 `refresh_comment_completeness()` 覆盖。
  - `get_run_crawl_detail(platform, start, end, before_map)` —— 按同一套
    `crawled_at` 执行窗归因，返回 added / updated 明细，含 before/after 的
    fetched/declared/completeness 与本轮实际补进条数 `gained`。
- `api/services/profile_crawler.py`：`CrawlExecutionResult.stats`，
  `_run_once` 前拍快照、后算明细（统计失败不阻断主流程）。
- `api/services/task_scheduler.py`：summary 透传 → `_finish_run` 落
  `run.stats_json` → `_run_dict` 下发；`ScheduledTaskRunOut` 补
  `stats_json` 字段（否则 Pydantic response_model 会把新字段过滤掉）。
- 前端：展开 run 后先显示统计区（新增/刷新分组、每行 `ID 前%→后%
  fetched 前→后/声明 +N 标题`），超过 50 行折叠计数，再显示原运行日志。

### 验证
- 真库：快照 243 篇；run 94 窗口 added 0 / **updated 18**，与记录的
  `updated_count=18` 完全一致；run 90 窗口 added 81，新增帖归因正确（0% 起算）。
- 接口：`/api/scheduler/runs` 已下发 `stats_json`（历史记录为 null）。
- 前端 `tsc -b && vite build` 通过；xhs 相关 + api 限制测试 39 项全过。

## [2026-10-28·续5] 修复增量补抓「评论为空的帖子永远轮不到」

### 根因（run 94：12:08–12:49，点赞账户任务，success / upd=18 / new=0）
- 增量补抓用 `plan_backfill` 按「差多少（gap）升序」挑每轮要重抓的帖子，
  **评论为 0 的帖子缺口最大（gap=声明数），永远排在队列最底部**。
- 只要还有任何「差几条就满」的帖子占着每轮 20 个名额，那 196 篇评论为空的
  帖子（尤其你看得见的大帖 8989/7510/1735 条评论）就**永远顺延、饿死**。
- 实测：run 94 重抓的 18 篇全是 gap 极小的「快满了」的帖子（55/78/2/14/53…），
  评论为空的帖子 0 篇被补 → 现象就是「只刷了 18 条、没有新增数据、空评论帖没动」。

### 修复（`tools/v2_sync.py::plan_backfill`）
- 优先级铁律：**评论为 0（fetched=0）的帖子最优先**，其余再按 gap 升序。
- 同档内按声明数升序（小帖先补、快速出成果）；大帖（声明几千条）自然排在同档
  末尾，待小帖清空后轮到 —— 不再被「快满的帖子」无限压在底部饿死。
- 验证：`get_archived_content_ids` 在 `backfill_limit=20 / maxc=3000` 下，
  `selected` 现已 **20 篇全为评论为 0**（修复前只有 17 篇、且混排 3 篇快满帖）。
- 测试：`test_xhs_comment_completeness.py` 修正旧断言 + 新增
  `test_backfill_comment_empty_first`，15 项全过。

### 提示
- 每轮仍只补 `XHS_COMMENT_BACKFILL_MAX_NOTES`（默认 20）篇，想一次多补几篇
  把该值调大即可（注意配合 `timeout_sec` 与风控预算）。
- 爬虫是调度器现起子进程、每次运行重新 import，改完无需重启后端。

## [2026-10-28·续4] 风控放大器根治：被 461 后不再狂打接口、UA 指纹自洽

### 根因（run 92 / 08:41–09:06，点赞账户任务）
- 列表 + 详情阶段前 24 分钟正常，`new_count=2`；09:05:58 起瞬间爆发
  几百条 `461 CAPTCHA（Verifytype 217）`，每秒 10+ 条，把账号彻底锁死。
- 真正放大器：**详情阶段被风控后，每篇帖子仍级联发 4 个接口**
  （feed → html+cookie → html-cookie → SSR），且**失败路径零间隔**，
  加上 `RISK_FAIL_LIMIT=3` 允许风控状态下继续撞 3 篇才停 → 几十篇帖子
  在数秒内连发几百个 461。
- 叠加指纹矛盾：API 请求头 `user-agent=Mac/Chrome137` 但
  `sec-ch-ua=136` 且 `sec-ch-ua-platform=Windows`，三重不一致，强风控指纹。
- 评论图下载每张图独立 client 且零间隔，是抓取期另一个隐藏密度源。

### 变更 / 修复
- **461/471 验证码（CaptchaError）= 账号级风控，首次命中即熔断整批**
  （新增 `_abort(reason, exc_type)`，验证码分支首次即停；403/429/IP 限流等
  非验证码仍走连续 `RISK_FAIL_LIMIT` 次才熔断，避免偶发单条误杀整批）。
- **兜底通道（html / html-nocookie / SSR）每个都捕获风控并立即熔断**，
  不再级联发请求；只有「非风控」的空结果 / token 过期才走兜底。
- **失败路径也加退避**（`xhs_sleep` 详情失败间隔），避免零间隔紧挨着打下一帖。
- **UA 指纹自洽**：统一为 `Windows NT 10.0 + Chrome/137`，且 `sec-ch-ua`
  版本号从 136 改成 137，与 UA、platform 三者一致（评论图下载 UA 已同款）。
- **评论图下载降密度**：复用单个 `httpx.AsyncClient`，每张图之间加
  `0.3–1.2s` 随机间隔（`tools/v2_sync._download_comment_image` /
  `_sync_comment_media`）。
- 测试 `test_xhs_risk_circuit.py` 重写以匹配新语义：验证码首次即停
  （calls==1）、非验证码连续失败才熔断、成功后清零 streak。

### 验证
- xhs 四套测试 26 项全绿；后端 8081 已重启加载新代码。
- 注意：账号当前仍在验证码风控冷却期，跑之前先在浏览器过验证码或等 1–2
  小时冷却，任务上限先设小（20–30）。

## [2026-10-28·续3] 评论抓够与否改为「完整率」判据，删掉拍脑袋的重试次数

### 变更
- **删掉「零评论帖最多重试 3 次」的硬上限**（`story.ext.comment_retry_count`
  与 `incomplete_retry_limit` 一并移除）。这个数字跟帖子到底抓没抓全毫无
  关系：撞风控的帖子 3 轮后照样被当成已归档，评论永远补不上；真·零评论的
  帖子则白跑 3 轮。
- **新的唯一判据**：`已抓评论数 / 原帖显示评论量 >= 90%`
  （`tools/v2_sync.is_comment_complete` / `COMMENT_COMPLETENESS_TARGET`，
  可在 `config/base_config.py` 调整）。未达标的帖子会一直留在待补池，
  每轮增量自动继续补，没有次数上限；声明数为 0/缺失视为无评论需求，直接
  算完整。
- 单篇抓取上限改为按声明数推算（`XHS_COMMENT_MAX_PER_NOTE=0` 不限制，
  `_comment_max_count`），不再统一吃全局 `CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES=30`
  —— 声明 800 条只抓 30 条，完整率 3.7%，却被增量当成已归档。
- `story_chapter.declared_comment_count` 在内容更新的分支也会刷新（原先
  只在建行时写一次）。
- 新增 `refresh_comment_completeness()`：每次抓完评论把
  `fetched_comment_count` / `completeness` 写回章节表（这两个字段此前只有
  一次性迁移脚本写过，其余时间一直是空的）。
- 每轮补抓有工作量预算（`XHS_COMMENT_BACKFILL_MAX_NOTES=20` 篇 /
  `XHS_COMMENT_BACKFILL_MAX_COMMENTS=3000` 条），差得少的优先补，超预算的
  顺延下一轮。**这不是重试次数**：顺延的帖子仍未达标，下轮照样进候选池。

### 修复
- `client.get_note_all_comments`：`max_count` 改为约束**一级评论**条数
  （原先用 `len(result)` 混算子评论，子评论多的帖子主评论只抓到几条就判
  「够了」）；`max_count<=0` 表示翻到 `has_more=False`。
- `get_comments` 打印每篇的「原帖显示 N 条 → 本次最多抓 M 条」，跑完
  `get_note_all_comments` 打印「一级评论 X 条 / 含子评论 Y 条」，便于核对
  完整率。

### 验证
- 新增 `tests/test_xhs_comment_completeness.py`（14 项）：90% 阈值边界、
  风控零评论必须继续重抓、声明数为 0 视为完整、单篇上限跟随声明数、
  子评论不占额度、补抓预算切片（篇数 / 条数 / 不死锁）。
- 真实库跑通：128 篇里 11 篇达标、117 篇未达标（多为上一轮风控留下的
  0% 帖）；按本轮预算只挑 20 篇补、97 篇顺延。
- 回归：`test_xhs_behavior_paging` / `test_xhs_risk_circuit` / `test_api_limits`
  全绿（顺手修了 `test_api_limits` 里 mock 仍返回 `True` 的旧契约）。

## [2026-10-28·续2] 点赞帖「只入库正文、没有评论」根因修复（并发 + 风控熔断）

### 现象
07:09 那轮点赞任务抓回 100 篇帖子（正文/图片正常），但**评论一条都没有**
（平台声明合计 28 万条）；任务却仍报 **success**。

### 根因
1. **详情请求并发失控**：`get_account_behavior_notes` 给每篇详情都
   `asyncio.Semaphore(_xhs_concurrency())` new 一个信号量 —— 100 篇等于
   同时并发 100 个请求，秒级打爆风控。改为**整批共用**一个信号量。
2. **触发验证码后还继续加码**：461/471 原本抛裸 `Exception`，被 tenacity
   重试 3 次 → 每篇 3 次请求 × 100 篇 = 300 次无效请求；
   `get_comments` 对每篇只记一条 warning 后继续，从不熔断。
3. **失败被吞**：一条笔记评论失败不影响退出码，任务显示 success，
   用户只能自己猜「是没评论还是被风控」。

### 修复
- 新增 `media_platform/xhs/exception.py::CaptchaError`（461/471 专用，
  不重试）；验证码响应头缺失时不再抛 KeyError。
- `get_comments`：CaptchaError / IPBlockError / PlatformAccessError 分别
  记录真实原因（RetryError 会解出 `last_attempt` 的根因），连续
  `RISK_FAIL_LIMIT`(3) 次即**熔断整批评论**，剩余笔记不再发请求。
- 熔断时打印 `[MEDIACRAWLER_RISK_ABORT]`；`profile_crawler` 扫到该标记
  就把运行记录标为 **failed**（附原文），而不是 success。
- 详情请求整批共用一个并发闸门。

### 连带修复：增量把「零评论」帖永久跳过
风控中断后那 100 篇被判为「已归档」，下次增量会永远跳过、评论再无机会补。
`get_archived_content_ids` 现在把**零评论的帖子视为未归档**
（→ 2026-10-28·续3 已把这段「重试 3 次」改成按评论完整率判定）。

### 验证
- `tests/test_xhs_risk_circuit.py`：20 篇全撞验证码时只尝试 3 次即熔断；
  前 2 篇失败后成功则清零计数、不误熔断。
- 实测改完后 `CaptchaError` 能直接暴露「HTTP 461 Verifytype 217」，
  不再是一句无信息的 RetryError。

### 续（同日 07:5x）——上一节改动引入的回归，已修
`CaptchaError` 改成「不重试」后，**详情页那段 `except RetryError: pass`
就抓不到它了**，异常从 `get_note_detail_async_task` 直接冒出去，把
`asyncio.gather` 打崩 → 整个爬虫进程挂掉（实测 07:45 的 traceback）。

- `get_note_detail_async_task`：补 catch `CaptchaError / IPBlockError /
  PlatformAccessError`，计入风控连续失败并熔断，返回 None。
- `get_account_behavior_notes`：**身份检查和列表阶段**就撞验证码时立刻
  收手（原先会先翻 25 页列表、再抓 100 篇详情，白打几百个请求），
  两处都打印 `[MEDIACRAWLER_RISK_ABORT]`。
- `batch_get_note_comments`：详情已熔断时不再起评论请求。
- 列表翻页间隔由 client 默认的 **1s/页**改为小红书统一的 5-15s 随机
  （点赞列表要翻几十页，1s 一页太急）。
- 新增回归用例 `test_captcha_in_detail_task_does_not_crash`：详情接口抛
  CaptchaError 时 `asyncio.gather` 必须正常返回 `[None]*5` 而不是崩溃。

## [2026-10-28·续] 点赞/收藏列表抓取补完（额度截断 + 增量 + 翻页收敛）

### 修复
- **点赞列表只抓到 20 条就停、收藏一条不抓**（真根因：额度用错）。
  `get_all_behavior_notes` 用全局 `len(result) < CRAWLER_MAX_NOTES_COUNT`
  （默认 **15**，本意是「单博主最多抓几条」）做翻页条件：
  - 点赞 10 条/页，2 页就到 20 条 → 点赞被截断（实测本号 **495 条**点赞）；
  - 点赞一旦超过 15 条，收藏的 while 条件直接为假 → **收藏一条都不抓**。
  改为「每个行为独立计额度 + 调用方喊停」：
  - 列表本身翻到 `has_more=False` 为止（跨过的老帖不消耗额度）；
  - 新增 `config.XHS_BEHAVIOR_MAX_NOTES`（默认 **100 条/行为**，任务选项
    `max_notes` 可覆盖），只对「本次要抓的新帖」计额度；
  - 回调支持 `return False` 叫停翻页，不再靠调小上限实现（那样会永远卡在
    最前面几页、翻不到后面的新内容）。
- **account 模式的收藏列表即使有数据也不抓**：同上，额度独立后
  `likes,collects` 两个行为都会跑完。
- **楼中楼单条主评论翻 40+ 页空转**：`get_comments_all_sub_comments` 里
  `has_more` 恒为 true、游标一直在漂、整页都是重复评论时会一直翻下去
  （每页 8-15s，实测把任务拖成几十分钟）。现在「整页均为重复评论」即收尾。

### 新增
- **account 模式增量抓取**（`XHS_BEHAVIOR_INCREMENTAL`，默认开）：
  `tools/v2_sync.get_archived_content_ids()` 取库内已归档帖子 ID，
  已归档的跳过详情+评论，只补新点赞；`mode=full` 或环境变量设 0 时全量重抓。
  495 条点赞全量重抓评论要十几小时，日常备份任务按增量跑才有可行性。
- 点赞/收藏接口补上与网页端一致的 `xsec_token` / `xsec_source`（空值）参数。

### 验证
- 新增回归测试 `tests/test_xhs_behavior_paging.py`（造 495 条点赞 + 37 条收藏，
  不连网不连库）：全量模式 495/37（旧实现 20/**0**）；每行为额度 25 各自生效；
  「前 200 条已归档 + 额度 30」时能跳过老帖继续翻到第 21 页之后；游标不前进即停。
- 真实账号实跑（额度 2）：点赞第 3 页凑够额度叫停、收藏正常抓到 8 条，
  跳过视频 19 篇 / 跳过已归档 7 篇 / 待抓图文 4 篇（2+2，按行为独立）。

## [2026-10-28] 点赞/收藏任务拉不到数据根因修复 + 楼中楼回复对象顺序

### 修复
- **account 维度「拉取我的点赞/收藏」永远为空**（根因：用错了 user_id）。
  `XiaoHongShuClient.get_self_user_id()` 原先跳过 `/user/me`，直接落到
  `you/mentions` 兜底，而 mentions 里的 `item_info.user_info.userid` 是
  「提到我的那条笔记的作者」，极可能是别人（实测本号真实 ID `6689eeca…`，
  兜底拿到 `64494df1…`）。拿别人的 ID 去查点赞/收藏，接口自然稳定返回
  `{"notes": [], "has_more": false}`——签名、登录态其实都是好的。
  现改为：`/api/sns/web/v2/user/me` → `/v1/user/me` → selfinfo → mentions
  （最后一档打 warning 提示可能不准）。修复后同一账号点赞 20 条、
  图文帖 7 篇（视频帖按规则跳过 13 篇）正常入库。
- **楼中楼「回复 @xxx」显示顺序**：原渲染为「回复 @被回复者 作者名：内容」，
  作者名跑到后面；改为与原站一致的「作者名 回复 @被回复者：内容」
  （`webui/src/components/data/platform/shared.tsx`）。

### 变更
- **定时任务等待抓取通道期间可见化**：`profile_crawler._wait_for_channel`
  等待时每 30s 打一条「排队等待中：已等 Ns / 上限 Ms」日志（此前 300s
  静默等待，用户点了「立即执行」看不到任何动静）；通道空闲时补一条
  「已等待 Ns，开始抓取」。
- `SCHEDULER_BUSY_WAIT_SEC` 默认 300s → **1800s**：整帖重抓常要 15-25 分钟，
  300s 会让刚点的任务静悄悄被跳过。
- 跳过原因文案补全（说明是「通道被占用」而非幂等保护），前端
  `useRunTask` 优先展示后端 `error`，不再一律提示「该任务正在执行中」。

### 新增
- `XiaoHongShuCrawler._collect_behavior_notes_via_page()`：接口拿不到列表时
  的后备路径——在浏览器页面打开本人主页「赞过/收藏」并滚动，截获页面自身
  发出的列表请求；页面上下文自动注入账号 cookie（CDP 复用失败落到全新
  浏览器时也能带上登录态）。附诊断日志：页地址/标题、是否点中标签、
  页面实际请求过的接口清单，便于改版后排查。

## [2026-10-27] 子评论对话目标 + IP 属地/时间展示

### 新增
- **评论 IP 属地**：`comment` 表新增 `ip_location` 列（`database/init_v2.py`
  补列迁移）；`store/xhs/__init__.py` 从评论原始 JSON 提取
  `comment_item.ip_location`，经 `persist_to_v2` → `sync_comment_to_v2`
  写入 v2（create/update 均回填，空值不覆盖已有）。API `_comment_public`
  下发 `ip_location`，前端主评论信息行与楼中楼均显示「IP属地：xx」。
- **子评论对话目标（回复 @xxx）**：子评论并非都回复主评论——XHS 原始
  `target_comment` 才是真实被回复对象。store 层已把 `target_comment.id`
  写入 `parent_comment_id`，`sync_comment_to_v2` 多轮收敛回填
  parent_id/root_id/depth（深度 ≥2 支持子评论互回）。API 按 parent_id
  映射出被回复者昵称下发 `reply_to`，前端渲染「回复 @xxx」前缀。
- **时间显示**：楼中楼每条子评论统一显示 `fmtTs(publish_ts)` 时间。

### 变更
- `reply_to` 仅当 `parent_id != root_id`（回复其他子评论）时下发：
  直接回复主评论的不显示「回复 @」前缀，与原站一致；原实现对所有子评论
  都注入回复目标，会把直接回复也渲染成「回复 @主评论作者」。

### 验证
- story 50（v1 含 910 条跨子回复）API 实测：depth=2 评论 `reply_to`
  与数据库中 parent 评论作者逐一吻合（momo / 桃子小大人 / ᅠᅠᅠ）；
  `GET /api/stories/50/comments/{id}/replies` 同样正确。
- story 62（「男生视角怎么看？…」）重抓后 `ip_location` 由全空逐步回填，
  跨子回复随子评论抓取进度入库；历史扁平数据（PDF 导入来源）无法本地恢复，
  依赖登录态重抓。

## [2026-10-27] 评论配图「看不到」根因修复 + 获取/备份去重

### 修复
- **评论配图前端看不到的真正根因：前端始终没构建**。
  此前 `CommentImages` 组件与 `XhsLayout`/`CommentReplies` 的渲染调用虽已落地，
  但 `webui` 从未 `npm run build`，`api/webui/assets` 里的产物是旧包、不含
  `CommentImages`，所以浏览器永远不画评论图。本次重新 `npm run build`
  （产物 `index-CBO2MFQL.js`），后端 `/api/data/raw/` 早已能 200 出图，
  刷新即生效。
- **获取去重（防重复入库 + 重复备份）**：
  `tools/v2_sync._upsert_comment_media` 原仅按 `(target_type, target_id,
  source_url)` 唯一约束去重。重抓时 XHS 下发带新 CDN 签名的 URL 使 source_url
  变化，约束拦不住 → 同一张图被插两遍（本地同一 `local_path`、S3 同一对象键、
  两条 `media_backup`）。现额外按确定性 `local_path` 命中已存在行，
  并顺带把最新 CDN 签名刷回 `source_url`。
- **显示去重（防重复渲染）**：`api/routers/story._load_comment_pictures` 按
  `local_url` 跳过重复媒体，前端不再同一张图出现两次。
- **存量重复数据清理**：`scripts/_t_cleanup_dup_comment_media.py` 按
  `(target_id, local_path)` 分组删重（全局清掉 12 条重复行），经
  `purge_media_backups` 的「交叉引用保护」删除其备份记录时保留仍被保留行
  引用的同一 S3 对象，无对象误删。

### 验证
- 目标评论 `6a635b3b`（「后续的聊天记录…纯爱」，story 62 / 内容
  `6a635a78000000001b01fe77`）`pictures` 由 16 条（8 唯一 ×2）降为 8 条唯一；
  8 条均各带 1 条 `media_backup`（S3 uploaded），全局无残留 `local_path` 重复组。
- `GET /api/data/raw/xhs/media/6a635a78000000001b01fe77/comments/4991/4991_001.jpg`
  → 200 / 88KB；`index.html` 已引用新包 `index-CBO2MFQL.js` 且可 200 加载。

## [2026-10-27] 评论表情包渲染 + 修复头像部分恢复诊断

### 修复
- **评论表情包看不到（前端 + API 双层缺口）**：
  - 后端 `api/routers/story.py::_comment_public` 此前从不返回评论 `pictures`
    字段；前端 `XhsLayout` 评论区从不渲染评论配图。评论图其实已被
    `tools/v2_sync._sync_comment_media` 下载到本地并登记
    `MediaAttachment(target_type='comment')` + S3 备份，只是没下发/没画。
  - 新增 `_load_comment_pictures` 批量解析评论配图 URL（本地 `/api/data/raw/`
    → S3 `/api/s3-backup/object/` → 平台远程，按 source 标注），并注入
    `get_stories/{id}/comments` 与 `.../replies` 两接口。
  - 前端 `shared.tsx` 新增 `CommentImages` 组件（单图大、多图三列网格，
    新标签开原图），`XhsLayout` 主评论与 `CommentReplies` 楼中楼均渲染。
  - 修 Windows 路径坑：`relative_to(DATA_DIR)` 出反斜杠导致 URL 路由匹配不到，
    改用 `.as_posix()` 转正斜杠。
- **API 验证**：`/api/stories/68/comments` 对带图评论返回 `pictures` 本地 URL，
  raw 路由 200 出图；`npm run build` 通过。

### 说明
- **头像「部分恢复」非代码缺陷，是数据回填口径**：前端头像取 `Author.
  avatar_url`，`_sync_author` 仅空值补；PDF 导入时评论头像为空 → 这些作者
  一直缺头像。本次重抓主评论被重新抓取并回填作者头像（恢复），但楼中楼回复
  因风控 `DataFetchError` 未能重抓（不恢复）→ 表现为部分恢复。
- **CDP 404**：日志里 `connect_over_cdp` 超时后回退标准模式（账号池 cookie，
  login=True），不影响笔记/主评论；回复抓不到是风控，不是 CDP。

## [2026-10-27] 定时任务三维度 + 重拉时区分已删除/已隐藏

### 背景
- 重新拉取帖子时平台会返回两种「目标没了」：作者/平台删除（code -510000）
  与作者隐藏（code -510001）。此前一律按 deleted 标记，无法区分
  「婆婆对我要离婚的态度」类已删帖与「有点无解 好无力」类已隐藏帖。
- 定时任务只有「主页/单帖」两类粒度，无法按业务维度配置与筛选
  （关注的用户 / 关注的帖子 / 扫码登录用户的点赞·收藏图文帖）。

### 新增
- **原贴状态区分**：`xhs/exception.py` 新增 `NoteHiddenError`；
  `client.py` 按 -510000 / -510001 分别抛 `NoteNotFoundError` /
  `NoteHiddenError`；`core.py` 捕获后调 `tools/v2_sync.mark_story_status`
  （原 `mark_story_deleted` 保留为向后兼容封装）标 `deleted` / `hidden`；
  列表与作者筛选把 `hidden` 纳入可见；详情页新增【原贴已隐藏】徽标
  （橙色，与粉色【已删除】并列）。
- **任务维度体系**（api/services/task_scheduler.py）：
  `DIMENSION_BY_TASK_TYPE` 把 profile→author（关注的用户）、note→note
  （关注的帖子）、account→account（扫码登录用户）三个维度落到任务行，
  `_task_dict` 输出 `dimension` 字段；`GET /api/scheduler/dimensions`
  返回维度元信息 + 账号池可用的扫码登录账号。
- **账号维度抓取**（task_type=account）：
  - `cmd_arg` 增加 `account` 枚举与 `--account_id`；`config/xhs_config.py`
    增加 `XHS_ACCOUNT_ID` / `XHS_ACCOUNT_BEHAVIORS`（likes,collects）。
  - `xhs/client.py`：`get_self_user_id`（selfinfo 多路径 + 互动列表
    `you/mentions` 兜底取本人 user_id）、`get_user_liked_notes`
    （`/api/sns/web/v1/note/like/page`）、`get_user_collected_notes`
    （`/api/sns/web/v2/note/collect/page`）、`get_all_behavior_notes`
    分页去重拉全。
  - `xhs/core.py`：`get_account_behavior_notes` 拉点赞+收藏图文帖
    （视频帖跳过），复用 `get_note_detail_async_task` 详情管线——
    评论/媒体/S3 备份全链路继承；v2 upsert 天然去重，已存在帖子
    增量刷新评论。无浏览器模式登录态按 `XHS_ACCOUNT_ID` 从账号池
    精确解析（`AccountPool.get_account` 按 ID 取，多账号互不串）。
  - `profile_crawler`：account → `--type account --account_id`，
    行为来源经 `XHS_ACCOUNT_BEHAVIORS` 环境变量注入；执行统计走
    `tools/v2_sync.get_run_crawl_stats`（按执行时间窗归因新增/刷新）。
  - 调度器执行时 account 任务按 target（账号池 ID）精确取该账号 cookie，
    不落入「随便挑一个账号」的兜底。
- **前端**（ScheduledTasksView）：维度筛选栏（全部/关注用户/关注帖子/
  登录用户，带各维度计数）；创建/编辑表单三维度切换，账号维度选账号池
  账号 + 勾选行为来源（点赞/收藏）；任务卡片按维度显示徽标与账号名。
  `types/scheduler.ts` 增加 `TaskDimension` / `BehaviorSource` /
  `SchedulerDimensions`；i18n zh/en 补键。

### 变更
- `api/schemas/scheduler.py`：`task_type` 增加 `account`；`TaskOptions`
  增加 `behaviors` / `account_id` / `account_name`；`ScheduledTaskOut`
  增加 `dimension`。`scheduler` 路由 options 归一化统一走
  `_normalize_options`（白名单放行账号维度字段）。

### 已知限制
- 网页端没有「我评论过的笔记」列表接口，账号维度的评论来源暂不支持
  （UI 已注明）。
- `note/like/page` 与 `note/collect/page` 对非页面上下文请求可能返回
  空列表（平台风控，会话级 X-S-Common 绑定页面请求）。列表为空时任务
  正常结束并在日志给出明确警告；有数据时建议浏览器模式（CDP）执行。

### 验证
- 服务启动回归：`/api/stories/authors/list`、`/api/stories` 正常。
- `GET /api/scheduler/dimensions` 返回三维度 + 账号池账号（id=5 default）。
- 创建/删除 account 任务 API 全字段往返正确（dimension=account、
  behaviors/account_id/account_name 落库）。
- `build_crawler_command` 生成 `--type account --account_id 5`，
  `XHS_ACCOUNT_BEHAVIORS` 环境变量正确。
- 在线冒烟：账号 cookie 解析 → pong → mentions 兜底取到本人 user_id
  （64494df1000000000f007053）→ 点赞/收藏列表空（该会话被风控拦截），
  优雅降级并给出警告，无异常退出。

## [2026-10-27] 评论头像/图片缺失修复 + 评论图 S3 备份

### 背景（根因）
- momo（author_id=3847，含《男生视角怎么看？发现男朋友和前任的秘密》等 9 帖）
  的 11,575 条评论中 95% 评论者无头像。根因：这 9 帖全部来自 `pdf_archive`
  留档导入，`scripts/import_pdf_archive.py` 当时把每条评论的
  `user_info.image`（评论者头像）与 `pictures`（评论图片）硬编码为空，
  导致评论者 `author.avatar_url` 从未被填充、评论图也从未入库。
- PDF 留档本身不内嵌「评论者头像」与「评论图」，因此从 PDF 无法补全，
  必须从用户页用登录态重新抓取（与用户此前要求一致）。

### 新增
- `tools/v2_sync.py` 评论图入库 + S3 备份：`_normalize_picture_urls` /
  `_download_comment_image` / `_upsert_comment_media` / `_sync_comment_media`
  接入 `sync_comment_to_v2` 两个分支（新增与更新）。评论 `pictures` 归一化后
  下载到本地 `data/{platform}/media/{note_id}/comments/{comment_id}/`，
  登记 `MediaAttachment(target_type='comment')` 并按 `source_url` 幂等，
  同时入 S3 备份队列（`enqueue_backup`）。开关 `ENABLE_COMMENT_IMAGE_BACKUP`。
- `scripts/backfill_comment_avatars.py`：存量 9 帖头像/图片回填脚本。按
  `--author-profile <hash>` 或 `--note-ids` 取笔记，用登录态 cookie 模式
  重抓评论并落库。cookie 来源优先 `config.COOKIES`，其次 account 账号池
  （扫码登录后会写入），无需手动填值。`--with-sub` 同时抓楼中楼补齐更多
  评论者头像；`--max-comments` 控单帖量。
- `import_pdf_archive.py`：评论 item 增加注释，明确 PDF 不内嵌头像/图片，
  须改走实时抓取（`backfill_comment_avatars.py`）补齐。

### 修复
- 评论者头像链路打通：实时重抓经 `store/xhs.update_xhs_note_comment` 写入
  顶层 `author_avatar`（`pick_avatar` 取 `user_info.image`），`v2_sync`
  `_get_or_create_author` 仅在 author 头像为空时补，因此重抓即可回填全部
  （含楼中楼）评论者头像，且不覆盖已有真实头像。

### 验证
- 静态：`py_compile` 三文件全通过；`_normalize_picture_urls` 对
  list-dict / csv / list-str / 空串 / None 五种来源归一化正确；
  `guess_extension` 取扩展名正确。
- 链路核对：`store/xhs.update_xhs_note_comment` 第 141/144 行确实产出
  `author_avatar` 与 `pictures`；`sync_comment_to_v2` 两分支均已调用
  `_sync_comment_media`；`XiaoHongShuClient.get_note_all_comments` 的
  `callback` 签名 `(note_id, comments)` 与 `batch_update_xhs_note_comments` 一致。
- 运行时回填需在有效登录态下执行（小红书评论接口需 cookie）：
  `python scripts/backfill_comment_avatars.py --author-profile 5cc009750000000016009a5a --with-sub`
- 待执行验证：登录态就绪后跑上述命令，抽查 `author.avatar_url` 覆盖率与
  `media_attachment(target_type='comment')` 行数。

## [2026-10-27·续] 重新抓取自愈机制（系统性防复发）

### 背景（更深层根因）
- 上一轮把根因归结为「PDF 导入硬编码空头像」。进一步追查发现更关键的阻断点：
  ① 9 帖在 `story_chapter.source_url` 上**其实存了真实的小红书链接**，所以
  前端「重新拉取」本就能抓到真数据；② 但 PDF 导入的评论 author 与实时重抓的
  评论 author **哈希不一致**——同一人（如 momo）被建成两条 author
  （3832 空头像 / 3847 有头像）。`_get_or_create_author` 只按 `anon_hash`
  匹配，重抓会新建/命中另一行，老评论仍指向空头像旧行 → 头像永远补不回来。
- 结论：要让「重新抓取」真正自愈，必须在作者身份上做**按主页链接(profile_url)
  的归并**，而不仅是「补空值」。

### 变更（让重新抓取自动恢复）
- `tools/v2_sync.py` `_get_or_create_author` 重写：身份匹配优先级
  `anon_hash` → `profile_url`；命中后回填空头像/空主页；若以 profile_url 命中
  但哈希不一致，统一为实时哈希（后续直连命中）。新增 `_merge_duplicate_authors`：
  同一 `profile_url` 下的重复 author 自动合并，评论迁到「规范行」（优先有头像、
  否则 id 最小），并删掉未被 story 引用的孤儿重复行。
- `sync_comment_to_v2`「已存在评论」分支：若重抓解析出的规范作者与当前不同
  （profile_url 合并/去重场景），把老评论重归属到规范作者，使其头像一并恢复。
- `scripts/import_pdf_archive.py`：评论 item 新增 `author_url`，用提取到的
  `user_id` 经 `build_profile_url` 现算主页链接。使**未来**导入的作者也带
  profile_url，重抓时能被同一机制对上 —— 从导入侧杜绝「身份不可恢复」。
- 评论图入库 + S3 备份（`_sync_comment_media`）保持：每次抓取/重抓都会补下
  缺失的评论图并触发 S3 备份（幂等）。

### 效果
- 现在对任一帖子点「重新拉取」（或按作者主页重抓），系统会：补齐评论者头像
  （含楼中楼）、下载缺失评论图并 S3 备份、合并重复作者、把老评论重归属到规范
  作者 —— **无需任何一次性脚本**。旧的 `scripts/backfill_comment_avatars.py`
  仅作为可选的批量触发手段保留，正常自愈走 UI「重新拉取」即可。

## [2026-10-25] hash 路由 + 访客浏览页

### 新增
- URL 跟随菜单变化（`webui/src/lib/useHashRoute.ts`）：`#/crawl`、`#/data/xhs`、
  `#/backup`、`#/guest`…，刷新/分享链接即恢复对应视图；备份记录「跳转到
  原贴」同样带动 URL 变化。无需服务端改动（hash 不参与请求路径）
- 访客浏览页 `#/guest`（`webui/src/views/GuestView.tsx`）：只读视图，只有
  筛选（平台/关键词/排序/时间范围）+ 浏览 + 导出当前结果（PDF，一次最多
  200 条，超限提示截断）；访客视图隐藏侧边栏管理菜单、许可/环境弹窗与
  登录悬浮按钮，自带主题/语言切换与「进入完整版」入口
- `PlatformStoryDetail` 新增 `readonly` 模式：隐藏关注/编辑记录/备份入队/
  重新拉取/删除，备份状态仅作静态展示；不提供改判 Context，立场徽标
  自动不可点（访客详情复用）
### 验证
- 浏览器实测：初始进入→#/crawl；切备份→#/backup；切小红书→#/data/xhs；
  刷新保持；备份页定位跳转→#/data/xhs；访客页筛选微博/小红书 URL 保持
  #/guest；访客只读详情管理按钮为空；全程 0 控制台错误

## [2026-10-25] 备份记录支持删除与跳转原贴

### 新增
- `DELETE /api/s3-backup/items/{id}`：删除单条备份记录，连同 S3 对象一起
  清除（对象仍被其它记录引用时仅删记录）；S3 删除失败不阻断，记录照删
- 备份记录操作列新增「跳转到原贴」按钮（紫色准星图标）：自动切到数据
  管理视图、对齐平台上下文并打开该帖详情（App 级 `mc:open-story` 事件 +
  `openStoryId` 机制，DataManagementView 挂载即开详情）
- 删除按钮扩展到全部状态（此前仅 orphaned/uploaded 可删，pending/failed
  只能重试不能删），删除前 confirm 明示「S3 对象将一并删除」
### 验证
- E2E：测试帖+已上传备份 → DELETE → 记录与 S3 对象双双清除
  （head_object 确认），测试帖清理 0 残留
- 浏览器：首屏 20 行定位/删除按钮齐全；点击定位成功跳转《两个相亲对象》
  详情页（含「已备份 2/2」标识），0 控制台错误

## [2026-10-25] 修复备份页「最近错误」恢复后仍显示历史 403

### 修复
- `backup_stats()` 的「最近错误」此前取任意一条 `last_error` 非空的记录，
  5 条 10-21 的 CDN 403 老失败项会长期占据横幅，即使之后 189 次备份全部
  成功、连接早已恢复；现改为：错误之后没有任何成功上传时才展示
  （错误时间 `<=` 最近成功上传时间判定为已恢复、自动隐藏），且只统计
  活跃状态（pending/uploading/failed/orphaned）的失败
- 前端横幅改为可点击（一键筛选失败记录查看详情），hover 说明恢复后自动隐藏

### 验证
- stats 接口 `last_error: None`，failed 计数仍为 5（未解决项不丢）；
  浏览器实测横幅消失、0 控制台错误

## [2026-10-25] 删帖联动清除 S3 备份（含失败重试与手动清理）

### 变更
- **原贴硬删除 → S3 备份一并删除**：`_hard_delete_story` 在删 media 行前
  调用 `purge_media_backups`，删除该帖全部备份对象与 media_backup 记录；
  批量删除同样生效（cascade 统计新增 `s3_purged` / `s3_orphaned`），
  删帖成功提示里会显示「N 个 S3 备份」。
- 配置开关 `S3_BACKUP_PURGE_WITH_STORY`（默认开，环境变量可覆盖）：
  关掉后回到「帖子删了仍可靠 object_key 取回」的旧语义。
- `MediaBackup` 新增 `orphaned`（待清理）状态语义：删帖时 S3 对象删除
  失败（网络/配置问题）不阻断删帖，记录转 orphaned 保留线索。

### 新增
- **交叉引用保护**：同一 object_key 仍被未删除的备份记录引用时
  （跨帖同键），保留对象、只删自己的记录，避免误删别人还在用的备份。
- **自动重试**：备份后台循环每次 tick 调用 `retry_orphaned_purges`，
  孤立备份的 S3 删除自动重试，成功即清记录。
- **管理页手动清理**：`POST /s3-backup/items/{id}/purge`（单条）与
  `POST /s3-backup/purge-orphaned`（全部）；图片备份页新增「待清理
  （原帖已删）」统计卡、「清理孤立备份」按钮、状态下拉「待清理」选项、
  记录行删除图标；删帖提示如遇失败项会引导去备份页重试。

### 验证
- E2E 三场景全过：① 测试帖+已上传备份 → API 删帖 → S3 对象与记录
  双双删除（`s3_purged=1`）；② orphaned 记录经 purge 端点清除对象+记录；
  ③ 两条记录共享 object_key 时删其一：对象保留、自己的记录删、别人的
  记录在（`kept_ref=1`）；
- 测试残留 0 行；前端 `tsc -b` + `npm run build` 通过，备份页浏览器
  冒烟 0 报错。

---

## [2026-10-23] 数据库异常丢失评论恢复（数据修复，无代码变更）

### 修复
- 10-21 20:08 起 joyeuse.cn:5432 故障约 46 小时（期间服务器迁 IP），
  PDF 留档导入中断：06 篇 500 条评论丢失、07/08/09 三篇整篇未入。
- DB 于 10-23 18:24 恢复后补导 4 个 PDF：**7258 条评论全部恢复**，
  与预生成快照（`_t_recovery/comments_snapshot.jsonl`）数量一致。
- 最终库状态：10 篇留档合计 **11727 条评论**、106/106 媒体已入 S3、
  100% 立场标签判定、楼中楼 6531 条全部挂父；各篇完成率 96-99%
  （缺口为 PDF 存档本身未渲染到底的部分）。

---

## [2026-10-21] 小红书反检测配置 / 定时任务反风控 / PDF 留档导入

### 新增
- **小红书反检测配置**（`config/xhs_config.py`，全部可用环境变量覆盖）：
  - `XHS_ENABLE_CDP_MODE=True`：默认走 CDP 连接本地真实浏览器，
    复用登录态/扩展/历史，不带 Playwright 自动化标识
    （标准模式回退路径也补了 `--exclude-switches=enable-automation`、
    `--disable-blink-features=AutomationControlled`、`--disable-infobars`）；
  - `XHS_CDP_HEADLESS=False`：默认关闭无头（有头窗口风控阈值最低）；
  - `XHS_CDP_CONNECT_EXISTING=True`：连接已开远程调试的浏览器；
  - `XHS_AUTO_CLOSE_BROWSER=False`：浏览器常开，登录态/指纹跨任务连续
    （cleanup 不再关用户上下文与 CDP 连接，atexit 也不杀进程）；
  - `XHS_MAX_CONCURRENCY_NUM=1`：并发恒 1，串行请求最像真人；
  - `XHS_SLEEP_MIN_SEC=5 / XHS_SLEEP_MAX_SEC=15`：请求间隔 5-15s 随机
    （client 的分页/翻页循环改为区间感知 `interval_sleep`，兼容固定值）。
- **验证码人工介入**（`tools/anti_risk.py`）：命中验证码/安全限制时打印醒目指引，
  轮询等待用户在真实浏览器里手动完成验证后自动继续（默认 300s 上限）；
  首页、搜索翻页、作者主页、笔记详情四条路径都已接入，
  人工验证成功后自动重试一次。无浏览器模式下降级为等待并跳过。
- **定时任务反风控**（`api/services/task_scheduler.py` + 配置）：
  - 执行窗口默认 **6-24 点**（`SCHEDULER_ACTIVE_HOUR_START/END`），
    窗口外到点的任务推迟到下次开窗并叠加 0-300s 随机分散，避免齐步走；
  - 任务真正发起前随机抖动 **1-5s**（`SCHEDULER_JITTER_MIN/MAX_SEC`），
    多个任务同时到点时错开起跑。
- **PDF 留档导入脚本**（`scripts/import_pdf_archive.py`）：把
  `WorkBuddy\2026-10-14-11-20-16` 下小红书网页留档 PDF 的**原页面数据字段**
  （标题/作者/发布时间/IP/互动数/正文/评论与楼中楼）与**内嵌配图**导入 pgsql：
  走 store.xhs（v1 落库自动同步 v2 story/chapter/comment/media），
  配图落盘 `data/xhs/media/{note_id}/001.jpg` 后直注册 media_attachment
  并自动入 S3 备份队列；楼中楼经 `backfill_comment_links` 回填父子链接；
  按 note_id 幂等去重（目录里多份拷贝自动跳过）。

### 变更
- `CDPBrowserManager` 新增 `connect_existing / auto_close` 参数与
  `_keep_browser_open` 语义：连接已有浏览器时 `browser.close()` 会把用户
  整个浏览器关掉，现在「常开」模式下只丢弃引用不断连接。
- `config/base_config.py` 新增 `_env_float` 助手与调度器反风控配置组。
- 「已备份」徽标改为**纯图标**（无文字，hover 显示 N/N 进度）。
- 详情页「编辑记录」按钮在**内容未发生变化（edit_count=0）时禁用**，
  hover 提示「内容未发生变化，暂无编辑记录」。

### 验证
- 小红书配置加载：CDP=True / HEADLESS=False / CONNECT_EXISTING=True /
  AUTO_CLOSE=False / 并发=1 / 间隔 5-15 随机，全部生效；
- CDP 实连测试：程序自行拉起 Chrome 154 → connect_over_cdp → 打开小红书首页；
  `cleanup(auto_close=False)` 确认浏览器进程保持常开（修复了 keep_open
  语义写反的 bug：曾把 auto_close 的值直接赋给 keep_open）；
- 调度窗口：凌晨 3 点判定不在窗口、下次开窗=当日 06:00，23:30 在窗口内；
- PDF 导入：单文件链路（正文+互动数+155 条评论+配图+楼中楼 86 条链接+
  自动入 S3 备份 uploaded）全部验证通过；全量导入 6/10 篇完成
  （momo 5 篇 + 根目录 1 篇，约 4200 条评论、75 张配图，图片已自动入 S3）；
  剩余 4 篇进行中时 **joyeuse.cn:5432 数据库侧故障**（同主机 S3 3900 正常，
  DNS 正常，PG 端口 TCP 超时，API 与导入同时受影响），属外部不可用，
  数据库恢复后重跑同一命令即可幂等续导：
  `python scripts/import_pdf_archive.py --dir "C:\Users\11826\WorkBuddy\2026-10-14-11-20-16"`；
- 6 条评论曾因 PDF 抽取带入的 NUL 字节入库失败，脚本已加 `_clean_str`
  清洗（NUL/C0 控制符/孤立代理项），重跑时自动补入；
- 前端 `tsc -b` 与 `npm run build` 通过；浏览器冒烟 0 控制台错误；
  网格「已备份」纯图标徽标、无编辑记录时「编辑记录」禁用均实测通过。

---

## [2026-10-21] S3 客户端配置持久化与图片自动备份

### 新增
- **S3 配置持久化**（`s3_config` 表，`tools/media_backup.py` 种子配置）：
  Endpoint / Key ID / Secret Key（Fernet 加密落库，API 只回掩码）/ Region /
  Bucket / 寻址方式全字段持久化，支持多配置增删改查与启用状态管理；
  备份任务取第一个启用配置。首次启动自动写入 garage 种子配置
  （joyeuse.cn:3900 / photos 桶），幂等按名判重。
- **媒体自动备份**（`media_backup` 表 + `tools/media_backup.py`）：
  - 对象键保留本地目录语义 `{platform}/{media_type}/{content_id}/{filename}`
    （按平台、文件类型分文件夹；CDN 后缀 `!nd_dft_wlteh_webp_3` 也能识别扩展名）；
  - 新抓取媒体经 `v2_sync._upsert_media` 自动入队；历史媒体一键入队
    （`POST /s3-backup/enqueue-missing`）；API 启动时起 60s 定时循环自动跑批；
  - 本地文件优先上传，本地没了自动从平台远程直取（httpx 带 Referer）；
    上传后 `head_object` 校验大小，不一致按失败处理；
  - 失败指数退避（60s × 2^n，最多 3 次），耗尽转 failed 待手动重试；
    批处理并发 4、锁互斥，单条失败不影响整批。
- **备份记录不随帖子删除**：`media_backup` 刻意不加外键（story 硬删除级联不到它），
  帖子删除后图片仍可靠 object_key 从备份地址取回；删除接口返回 `backups_kept` 计数。
- **访问链第三级**：`GET /s3-backup/object/{key}` 流式代理 S3 对象
  （帖子列表封面与详情媒体按 本地缓存 → 平台远程 → S3 备份 顺序回退，
  详情 media 增 `backup_url` 与 `source` 字段）。
- **前端备份管理页**（`views/S3BackupView.tsx`，导航「图片备份」）：
  配置卡片（启停 / 测试连接 / 编辑 / 删除，Secret 掩码）、统计卡片
  （待备份 / 已备份 / 失败 / 体积）、备份记录表（状态 / 平台筛选、分页、
  单条重试、对象地址直开）、立即备份 / 重试失败 / 历史入队操作。
- **已备份标识**：全部图片视频入 S3 的帖子，网格卡片右上角显示绿色
  「已备份」小徽标（hover 看 N/N 进度）；详情页顶部按钮显示
  `已备份 N/N` 或「备份此帖」（点击入队并执行）。

### 变更
- `account/crypto.py` 拆出通用 `encrypt_text / decrypt_text`，Cookie 加解密委托其上。
- `api/main.py` 启动任务：建表后种子 S3 配置 + 起 60s 备份循环（无启用配置时静默跳过）。
- `pickMedia`（shared.tsx）访问链扩展为 local → remote → s3，并输出 `source` 字段。

### 验证
- boto3 链路自检：连接（head_bucket）→ 上传 → head 校验 → 读回字节一致 → 删除；
- 真实 garage 上传 88 个媒体（11.7MB），S3 内目录结构
  `xhs/image/{content_id}/001.webp` 正确，`/api/s3-backup/object/...` 代理 200
  且字节数与源一致（RIFF/Webp magic 正确）；
- 重试机制：5 个远端 403 的媒体按 60s→120s→240s 退避重试，耗尽转 failed，
  `POST /retry` 重置 5 条后重新入队；
- 删除保留：临时帖子硬删除后 `media_backup` 行存活（返回 `backups_kept: 1`），
  object_url 仍可构造；media 行已删；
- 列表 / 详情接口正确返回 `backup_complete`、`backup_uploaded/total`、
  `cover_backup_url`、media `backup_url/source`；
- 前端 `tsc -b` 与 `npm run build` 通过。

---

## [2026-10-21] 修复网格图 404 / 评论表态标签筛选与改判

### 修复
- **网格视图图片加载不出来（根因）**：列表返回的本地封面路径拼错前缀。
  `/api/data/files/{path}` 是**文本预览**路由（只认 json/jsonl/csv，图片直接 400），
  而 `/api/data/{path}` 根本不存在（404）。新增二进制直出路由
  `GET /api/data/raw/{file_path:path}`（限制在 DATA_DIR 内、防目录穿越、
  按后缀给 Content-Type、带缓存头），封面改走它。实测 4 张封面全部 200。
- 特定平台列表视图下不再显示平台品牌标识（进入平台页时列表已天然同质，
  再放标识是冗余）；仅在「全部平台」视图保留。

### 新增
- **评论表态标签筛选**：`GET /stories/{id}/comments` 增 `tag` 参数
  （按 `stance_tag_code` 过滤，逗号分隔多值），并返回 `tag_stats`
  （按标签库的标签顺序输出 名称/颜色/条数）。工具栏新增「表态」筛选行，
  标签名 + 条数按品牌色渲染，hover 显示占比，可多选（或关系）。
- **评论表态可人工改判**：`PATCH /stories/comments/{id}/stance`
  （body `{"tag_code": "..."}`），写回标签字段并按 polarity 同步 `stance`，
  保证赞同度统计口径不变；徽标点击即弹出标签选择器（新增
  `StanceEditContext`，11 个平台布局不必层层传 props）。
- `GET /stories/comments/stance-tags`：改判可选标签列表（含颜色与极性）。
- 徽标改库表驱动：优先显示命中的态度标签名 + 标签色，未重判的旧数据回落三态徽标。

### 变更（补充：表态筛选并入立场栏）
- 表态不再单占一行，改为**立场栏的下钻子项**：`立场 [全部|赞同|中立|反对] › [全部|细分表态…]`。
  选中某立场后只列该极性下的表态（阴阳 / 质疑 / 嘲讽 归入反对，无语 / 观望 / 笑声
  归入中立，神评归入赞同）；未选立场时只列有数据的表态，避免一行塞满空标签。
- 交互：点细分表态 = 单选下钻（同时把分段按钮切到对应极性）；再点一次回到该立场
  全部；切换立场会清空细分选择。后端 `tag_stats` 增 `polarity` 字段供前端归类。

### 验证
- `tag_stats` 带 polarity 正确输出；`stance=oppose&tag=mock` 组合筛选生效
  （返回「有4个灶给你狂的」/ 嘲讽 1 条）；
- 4 张本地封面 `/api/data/raw/...` 全部 200（image/webp、image/jpeg）；
- `tag_stats` 正确输出 10 个标签的分布；`tag=doubt/sarcasm/speechless` 筛选生效
  （该帖这几类确为 0 条）；
- 改判：评论 549 → 阴阳（stance 同步 oppose）→ 改回赞同（stance 同步 agree）；
- 前端 `tsc -b` 与 `npm run build` 通过。

---

## [2026-10-21] 态度标签库管理页 / 编辑记录 UI / 平台页品牌化

### 新增
- **态度标签库管理页**（`views/StanceTagsView.tsx`，顶部导航「态度标签库」入口）：
  - 标签增删改查：编码、名称、极性（归入赞同度）、颜色、优先级、启用/停用、
    兜底标签标记、判定描述与示例（每行一条）；
  - 规则增删改查：`keyword / regex / sentiment / laughter / popular` 五种匹配类型，
    可单条启停并带权重；
  - 试判定：输入回复 + 帖子标题，即时返回命中标签、极性、置信度与命中原因；
  - 「刷新缓存」按钮强制重载判定缓存；写操作后自动失效缓存，改完下次判定即生效。
- **帖子编辑记录**（`components/data/StoryEditLogDialog.tsx`）：详情页新增「编辑记录」
  按钮（带条数徽标），弹层左侧按时间列历次编辑，右侧双栏展示 修改前 / 修改后 差异。
- **后端 `/api/stance-tags/rejudge`**：按最新标签库重判历史评论（可选 story_id / limit /
  force），新增或修改标签后用于刷新存量数据；本次已重判 3705 条。
- 详情接口新增 `media_count / edit_count / edited_at`；评论输出新增
  `stance_tag_code / stance_tag_name / stance_tag_color / stance_confidence`。

### 变更
- **平台页视觉改造**：
  - 新增 `PlatformBrand`：平台品牌标识三级降级 —— logo 图（`public/logos`，现覆盖
    小红书 / 抖音 / B站）→ 内置 SVG 图标（知乎/微博/贴吧/快手/X/Facebook/Reddit/
    小红书兜底）→ 品牌色首字块，任一级别都不会出现破图或空白；
  - 新增 `PlatformPageHeader`：品牌标识 + 平台中文名 + 品牌色描边渐变头部，
    一眼区分当前平台页；全部平台用中性青色；
  - 移除页内「数据管理」标题与「平台」下拉（顶部导航已有平台入口，避免重复入口），
    平台完全由导航传入；列表项前与顶部平台入口的圆点均换成平台品牌标识。
- **卡片封面修正**：有图时展示帖子内部首张图片（本地副本优先，外链签名过期实测
  403），无图或图加载失败时展示正文前 50 字（新字段 `excerpt`），仍无内容才回落
  品牌色首字块。
- `backfill_media_local.py` 支持 `--all-counts`：全量刷新 `story.media_count`
  与 `cover_media_id`（历史已挂过的帖子此前计数停留在 0）。

### 修复
- 标签库判定优先级：`无语 / 哈哈哈` 等显式特征词此前被赞同/反对的泛化情感规则
  抢先命中。现按「笑声(5) → 阴阳(10) → 质疑(12) → 嘲讽(14) → 无语(16) → 观望(18) →
  赞同(40) → 反对(42) → 神评(60) → 兜底(90)」排序，显式特征词优先于情感推断。

### 变更（补充）
- **抓取策略「只新增不删除」落地**：`v2_sync` 媒体入库改为 upsert
  （`_load_media_index` + `_upsert_media`）—— 新 URL 才 INSERT（带 storage_type /
  source_status），已有 URL 只回补 `local_path / size / mime / downloaded_at`，
  本次抓取没带回来的历史媒体永不删除（只有 story 硬删除才级联）。
  原先两处是无条件 `session.add`，重复抓取同一条会撞 `uq_media_src` 唯一约束
  导致整条同步失败。

### 验证
- 只增不删：同一条内容连续同步两次，媒体 4 → 6（两条新 URL 各插一次），
  第二次不重复插入、不报错，本地路径全部回补；
- 标签库 10 个标签、CRUD 与试判定接口通过；`好棒棒，真有你的 → 阴阳`、
  `无语了 → 无语`、`哈哈哈 → 笑声`、`蹲一个后续 → 观望`、`不敢苟同 → 反对`；
- rejudge 重判 3705 条评论，标签字段已回填（如「有4个灶给你狂的」→ 嘲讽）；
- 列表接口封面返回本地副本路径（`/api/data/xhs/media/.../001.webp`）；
- 前端 `tsc -b` 与 `npm run build` 均通过。

---

## [2026-10-21] 内容列表：昵称截断 + 小红书式网格视图

### 新增
- **网格视图**（`StoryCard`，小红书卡片布局）：
  - 响应式列数 `2 / sm:3 / lg:4 / xl:5 / 2xl:6`，卡片宽度自适应、间距一致（`gap-3`）；
  - 封面统一 `aspect-[3/4]` + `object-cover`，不变形；封面裂图（CDN 防盗链）
    回落到「标题首字 + 平台色」色块，不留破图；
  - 标题 `line-clamp-2` 最多两行后截断；
  - 卡片底部：作者头像 + 昵称（truncate、hover 显示全名）+ 点赞/评论数
    （评论数窄屏隐藏）；
  - 勾选框浮在封面左上角（阻止冒泡，勾选不会误开详情），整卡点击进详情。
- **视图切换入口**：筛选栏「列表 / 网格」图标分段按钮，选择写入
  `localStorage`（`mc_data_view_mode`），刷新后保留。
- 移动端适配：列表行整行可点进详情，「详情」按钮窄屏隐藏；网格移动端 2 列。

### 变更
- 列表模式发帖人昵称固定显示**前 2 个字符 + 省略号**，hover（`title`）显示完整
  昵称；昵称列定宽 `w-[2.6rem]` + 不换行，多行列表对齐不错位。

---

## [2026-10-21] 数据管理列表：发帖人展示 / 发布时间默认排序 / 发帖人多选筛选

### 变更（只动列表的展示字段、排序与筛选，其它列、分页与既有接口行为不变）
- 列表项去掉平台标签，改为展示**发帖人头像 + 昵称**；头像走本地代理绕 CDN
  防盗链，加载失败自动回落到「名称首字」渐变头像
  （`PlatformAvatar` 的裂图兜底由隐藏改为切字母头像）。
- 默认排序改为**按发布时间倒序**（最新在前）：新增 `sort=publish`
  （`first_publish_ts desc`），热度 / 时间（最后更新）/ 点赞 / 评论四种排序
  语义与顺序保持不变；前端排序下拉新增「按发布时间」并设为默认。
- 新增**发帖人多选筛选**：勾选一个或多个用户，列表只展示这些用户的帖子
  （服务端过滤，跨分页生效），清空即恢复全部；切换平台时自动清空发帖人选择。

### 后端（均为增量，不传新参数时行为与原来一致）
- `GET /api/stories` 新增可选 `author_ids`（可重复传，命中任一即展示）；
  返回项新增 `author_id / author / author_avatar / author_url`（批量查，无 N+1）；
  `SORT_FIELDS` 新增 `publish`。
- 新增 `GET /api/stories/authors/list?platform=&keyword=&limit=`：按发帖量倒序
  返回发帖人候选（id / 昵称 / 头像 / 主页 / 帖子数），供多选面板使用
  （作者表 3000+ 行，靠平台 + 昵称关键词收敛，不做全量下拉）。

### 验证
- `sort=publish` → 列表按 publish_ts 倒序；
- `author_ids=544&author_ids=302` → 3 条（2+1），`author_ids=543` → 1 条，
  不传参数 → 与改动前一致（4 条）；
- `/stories/authors/list?platform=xhs` 正确返回头像、主页与帖子数。

---

## [2026-10-21] 顶部导航改版：去掉使用须知横幅，数据管理按平台拆分

### 变更
- 移除顶部居中的「使用须知」警示横幅（免责声明弹窗保留为首次使用时弹出一次）。
- 顶部栏改为两行：
  - 第一行：Logo + 抓取 / 热搜时光机 / 定时任务 / 数据浏览 + 运行状态 + 主题/语言；
  - 第二行：**数据管理按平台拆分的入口**（数据管理=全部 + 小红书 / 抖音 / 快手 /
    B站 / 微博 / 贴吧 / 知乎），每个入口带平台品牌色圆点与描边，选中时填充品牌
    底色；窄屏可横向滚动。
- 平台入口与数据管理页双向联动：`DataManagementView` 新增受控 props
  （`platform` / `onPlatformChange`），顶部切换或页内下拉切换都会同步，
  并自动回到第一页、清空勾选；不传 props 时仍退回内部状态，可单独复用。
- 平台名优先显示中文（`platformTheme` 的品牌名），后端下发英文名的情况下
  只在未知平台时退回英文。

---

## [2026-10-21] 详情页关注（定时更新）+ 子评论交互收敛

### 新增
- **【关注此贴】/【关注此人】**（详情页顶部）：复用现有定时调度器
  （`api/services/task_scheduler.py`，纯 asyncio，未引入任何第三方调度依赖）
  建一条「每 24 小时一次」的任务；抓取参数与详情页「重新拉取」完全一致
  （平台 / 原文链接或作者主页 / 开评论+子评论+媒体 / 直写 PG / headless /
  cookie 走账号池）。
- 接口：
  - `GET /api/stories/{id}/watch` —— 返回 note / author 两个开关的状态
    （enabled / task_id / supported / last_run_at / last_status / last_error /
    next_run_at / stopped / stop_reason）；
  - `POST /api/stories/{id}/watch/{kind}` —— `kind=note|author`，
    body `{"enabled": bool, "run_now": bool=true}`。
- 任务编码固定 `watch-note-{story_id}` / `watch-author-{平台}-{作者id}`：
  同一对象反复开关只复用同一条任务，不会堆积；开关状态落 `scheduled_task`
  表，服务重启后自动恢复。开启后会**立即后台拉取一次**（`asyncio.create_task`，
  不挂在请求上），之后每天一次。
- **目标失效自动停任务**：`task_scheduler.detect_fatal_target_error()` 识别
  「帖子/笔记/用户不存在、已被删除、已失效、无权限访问、私密、仅粉丝可见」等
  表述，命中即在 `_finish_run` 里把任务置 `enabled=False` 并写入
  `options.auto_stopped / auto_stop_reason`；前端 30s 轮询到 `stopped=true`
  时弹出明确提示（含原因），按钮自动回到关闭态。重新关注会清除该标记。

### 变更
- 子评论不再参与「内容赞同度」：赞同度口径本身就是主评论（depth=0）对发帖人
  观点的立场，楼中楼多在跟其他评论者对话，判定基准不同；前端移除子评论的
  立场徽标，后端统计本来就只算主评论。
- 子评论的显示开关从详情页顶部栏**移到评论区筛选栏**（排序 / 立场 / 回复 /
  子评论 / 搜索 同一行）。
- 子评论头像点击行为与主评论一致：改走 `AuthorLink`，有 `author_url` 时包
  `<a>` 跳转该用户主页，没有则退化为不带链接的头像（不留死链）。
- `task_scheduler.create_task()` 支持外部指定 `task_code`（关注任务的幂等锚点）；
  `update_task()` 在 `enabled` 变化时也重算 `next_run_at`。

### 验证
- `GET /stories/50/watch` → note/author 均 `supported=true`、未关注；
- 开启 → 建任务 `watch-note-50`（note / interval 86400 / 目标为原文链接，
  选项含 enable_comments+sub_comments+media、save_option=postgres），
  `next_run_at` 为次日；
- 模拟一次「帖子不存在」的失败执行 → 任务自动 `enabled=false`、
  `stopped=true`、`stop_reason=帖子不存在`、`last_error=帖子不存在（已自动停止该定时任务）`；
- 重新开启 → 自动停止标记清空；取消关注 → `enabled=false`；
- 子评论接口确认每条回复都带 `author_avatar` 与 `author_url`；
- 验证用的两条测试任务已从调度器删除。

---

## [2026-10-21] 内容赞同度（立场判定）重构 + 海外平台差异化布局

### 新增
- **`tools/stance.py`**：内容赞同度判定引擎。
  - `extract_post_stance()` 提取发帖人核心观点（support / oppose / neutral）、
    话题关键词与一句话摘要，作为评论立场判定的基准；
  - `judge_comment_stance()` 对每条评论判 `agree / neutral / oppose`，
    优先级：指向发帖人的直接表达 > 嘲讽质疑 > 纯笑声噪音 > 情感×帖子立场映射；
  - `agreement_label()` 输出「内容赞同度」档位，沿用 Steam 中文区七档文案
    （一致赞同 / 特别赞同 / 多半赞同 / 观点分化 / 多半反对 / 强烈反对 / 一致反对）。
- **`database/models_v2.py`**：`Comment` 新增 `stance` 列
  （`ALTER TABLE comment ADD COLUMN IF NOT EXISTS stance VARCHAR(16)`）。
- **`tools/v2_sync.py`**：
  - `backfill_comment_stances()` 批量回填立场（幂等，只补 NULL）；
  - `get_story_stance()` 计算并缓存发帖人观点到 `story.ext`，
    缓存键带版本号（`STANCE_CACHE_KEY`），判定逻辑升级后自动重算。
- **三个海外平台布局**：`TwitterLayout` / `FacebookLayout` / `RedditLayout`，
  并在 `platformTheme.ts` 注册 `twitter` / `facebook` / `reddit` 主题。
  - X(Twitter)：无重卡片、细分隔线，头像列+内容列，底部四枚互动图标等距，
    评论为回复流 + 左侧 thread line；
  - Facebook：三段式分隔（正文→互动统计→赞/评论/分享三等分按钮），
    评论为灰底圆角气泡 + 「赞 · 回复 · 时间」文字操作条；
  - Reddit：左侧竖排投票列（上/下箭头 + karma），`r/xx · u/author` 元信息行，
    灰底 pill 操作条，评论区贯通竖线 + 逐层缩进。
- **`StanceBadge` 组件**（`shared.tsx`）：评论/回复上的立场徽标，
  赞同绿 / 中立灰 / 反对粉，支持深色舞台（抖音）。

### 变更
- 评论区指标由「情感好评度」切换为「**内容赞同度**」：
  - `GET /api/stories/{id}/comments` 新增 `stance` 筛选参数
    （agree/neutral/oppose），返回 `stance_stats` 与 `agreement` 档位；
  - `CommentToolbar` 统计行改为赞同度档位 + 赞同/中立/反对分布，
    筛选分段由「好评/中立/差评」改为「赞同/中立/反对」；
  - 赞同度分母取**已表态评论**（赞同 + 反对），中立评论不计入——中文评论区
    无立场内容常占七八成，计入分母会把「8 赞 1 踩」压成「强烈反对」。
- 子评论区：每条回复渲染对应用户头像（`author_avatar`，走本地代理绕防盗链，
  加载失败回落字母头像），并附立场徽标。
- 全部 8 个国内平台布局的评论项均显示立场徽标。
- `api/main.py` 启动后台任务纳入立场回填（与情感回填并行，失败不影响 API）。

### 修复
- 立场判定过度保守（47 条评论里 46 条判中立）：
  相关性判定原来只比对分词关键词，短评论几乎全部落空。改为
  「关键词命中 + 字级别重合」双层判定，且只在**完全不沾边**的短评论上判无关。
- 话题标签污染话题画像：`#自建房[话题]#` 之类的词被当成核心话题。
  提取关键词时剥离话题标签，但相关性比对仍用全文（否则「自建房」比对不到）。
- 纯笑声评论（「哈哈哈」「笑死」「[笑哭R]」）被情感词典判正面而误判成赞同，
  新增噪音识别：剔除笑声与表情后无实质内容一律中立。
- 嘲讽类表达（「给你狂的」「多此一举」「凡尔赛」）字面无贬义词、情感词典判中立，
  新增 `_MOCK_OPPOSE` 中优先级规则判反对。

### 验证
- `python tools/stance.py` 自带用例 11/11 通过；
- 接口实测：story 50 → `{agree: 8, neutral: 38, oppose: 1}`，档位 `rating.agreeVery`；
  `?stance=oppose` 筛选返回 1 条；回复列表均带 `author_avatar`。

---

## [2026-10-20] 情感判定与好评度展示（已被本次重构取代）

- `tools/sentiment.py` 词典驱动的情感判定，评论区展示 Steam 风格好评度档位；
- 评论区新增排序 / 情感筛选 / 是否含回复 / 关键词搜索工具条。

---

## [2026-10-19] 用户主页全量拉取 + 定时拉取任务

- 用户主页全量拉取：分页、增量去重、重试限流超时、记录抓取时间；
- 定时拉取任务：interval / Cron 两种粒度、两种任务粒度、执行日志、幂等去重。

---

## [2026-10-18] 抓取队列与 Docker 一键部署

- 抓取队列：爬虫忙时新任务入队，前一个跑完按顺序执行；
- Docker 多阶段构建（Vite 产物输出到 `api/webui`）+ `docker-compose.yml`
  + `docker-entrypoint.sh`，详见 `docs/DOCKER.md`。

---

## [2026-10-16] v2 数据层与平台化详情页

- v2 统一数据模型（story / chapter / comment / author / tag）与实时同步
  （`tools/v2_sync.py`），爬虫写 v1、WebUI 读 v2；
- 数据管理页按平台分发各自的详情布局（小红书 / 知乎 / 微博 / 抖音 / B站 /
  贴吧 / 快手 / 通用），滚动加载评论、子评论默认折叠。

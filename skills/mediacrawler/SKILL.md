---
name: mediacrawler
description: 多平台内容抓取与故事聚合。当用户需要抓取小红书/抖音/快手/B站/微博/贴吧/知乎的笔记、视频、评论，或要把抓取结果检索、导出 PDF 时使用。内含环境自检、配置项清单、检索 API 与已知坑位。
---

# MediaCrawler — 多平台抓取与故事聚合

核心链路：**抓取 → 持久化存储 → 检索读取 → 对外输出**。
支持 7 个平台：小红书(xhs)、抖音(dy)、快手(ks)、B站(bili)、微博(wb)、贴吧(tieba)、知乎(zhihu)。

---

## 一、环境准备

```bash
cd <项目根目录>
python -m venv venv && venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# Linux/macOS: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

venv/Scripts/python.exe -m playwright install chromium    # 浏览器内核（必需）
venv/Scripts/python.exe scripts/check_env.py              # 环境自检，一次看清缺什么
```

浏览器内核默认装在 `~/.cache/ms-playwright`（Windows `%LOCALAPPDATA%\ms-playwright`）。
C 盘紧张时可改到别的盘：`set PLAYWRIGHT_BROWSERS_PATH=D:\playwright-browsers`。

## 二、数据库与配置

1. 复制 `.env.example` 为 `.env`，填写数据库连接（MySQL / PostgreSQL / SQLite 任选）
2. 建表与配置种子（幂等，可重复执行）：

```bash
venv/Scripts/python.exe -m database.init_v2 postgres      # 建立 v2 统一模型 + 配置表
```

配置**已全部入库**（37 项，5 个分组：抓取行为 / 代理与风控 / 浏览器 / 存储与产物 / 系统）。
不要再改 `config/base_config.py`，改库表即可：

| 常用配置 | 说明 |
|---|---|
| `crawl.enable_checkpoint` | 断点续爬。相同参数重跑 = 续跑，自动跳过已完成页 |
| `crawl.enable_get_sub_comments` | 抓取楼中楼（**会显著增加请求量**，配合限速用） |
| `crawl.crawler_max_sleep_sec` | 请求间隔（**立即生效**，调低会明显增加风控命中） |
| `browser.enable_browser` | 关闭后走**无浏览器模式**，不启动 Playwright，直接用 Cookie |
| `browser.enable_cdp_mode` | 连接本机已有 Chrome 的 9222 端口 |
| `storage.save_data_option` | 落盘方式，默认 jsonl |

通过 API 改配置（乐观锁 + 校验 + 审计）：

```bash
curl -X PUT http://127.0.0.1:8081/api/config/items/crawler_max_sleep_sec \
  -H "Content-Type: application/json" -d '{"value":3,"version":1}'
```

## 三、启动

### WebUI（推荐）

```bash
venv/Scripts/python.exe -m uvicorn api.main:app --port 8081 --host 127.0.0.1
# 打开 http://127.0.0.1:8081  → 配置面板 + 实时日志 + 数据管理
```

### 命令行

```bash
venv/Scripts/python.exe main.py --platform xhs --lt qrcode --type search
venv/Scripts/python.exe main.py --init_db postgres          # 建 v1 表
venv/Scripts/python.exe main.py --help                      # 全部参数
```

首次运行会弹浏览器扫码登录。登录态默认保存，下次免扫码。

## 四、检索与导出

```bash
# 检索（平台/标签/关键词筛选，热度/时间/点赞排序）
curl "http://127.0.0.1:8081/api/stories?sort=heat&page_size=20"
curl "http://127.0.0.1:8081/api/stories/10"                 # 详情（含章节/标签/附件）
curl "http://127.0.0.1:8081/api/stories/10/comments"        # 评论树（主楼按赞 + 楼中楼）
curl "http://127.0.0.1:8081/api/stories/stats/overview"     # 总量/分平台/热门标签

# 导出 PDF（数据管理页也有按钮）
curl -X POST http://127.0.0.1:8081/api/export/pdf \
  -H "Content-Type: application/json" \
  -d '{"file_path":"xhs\\jsonl\\search_contents_2026-10-16.jsonl","limit":50}'
```

存量 jsonl 导入 v2 表（幂等）：

```bash
venv/Scripts/python.exe scripts/migrate_jsonl_to_v2.py xhs
venv/Scripts/python.exe scripts/recompute_heat.py      # 重算热度（排序核心字段）
```

## 五、已知坑位（都踩过）

1. **8080 常被占用**。服务起不来先看 `netstat -ano | findstr :8080`（Linux `ss -lntp`）。
2. **CDP 模式默认会失败**：它连本机 9222 端口，若被别的程序占用会超时 60s 后崩。
   默认已关闭（`browser.enable_cdp_mode=false`），不要轻易打开。
3. **密码/URL 特殊字符**：数据库密码含 `@` `#` 会让连接串解析错位。
   代码已用 `_build_db_url()` 转义；`.env` 里含 `#` 的值必须加引号，如 `REDIS_DB_PWD="root#2018"`。
4. **`/api/env/check` 曾卡满 30 秒**：它起子进程跑 `uv run`，而 uv 会重新解析依赖。
   已改为用当前解释器，秒回。
5. **`data.py` 曾不认 jsonl**：只认 json/csv/xlsx，而默认存储就是 jsonl，导致数据页空白。已修复。
6. **WebSocket 一直 404**：uvicorn 需要 `websockets` 库才能处理 WS 升级。
   如果日志出现 `Unsupported upgrade request`，`pip install websockets`。
7. **子评论默认不抓**：`crawl.enable_get_sub_comments=false`。
   开启后请求量大幅上升，务必同时调大 `crawler_max_sleep_sec`。
8. **风控是行为级的，不是 IP 级**：300011=账号异常（行为画像命中），300012=IP 风险。
   加代理解决不了行为画像，要靠限速 + 抖动 + 减少无效请求。
9. **评论分页有重叠**：同一条评论会出现在多个分页里，统计必须按 ID 去重。
10. **Windows 的 ConnectionResetError(10054)**：客户端断开时 asyncio Proactor 会抛，
    不是业务错误，已在服务启动时加异常处理器忽略。

## 六、目录速查

| 路径 | 作用 |
|---|---|
| `media_platform/<平台>/` | 各平台抓取逻辑（core / client / login） |
| `signer/` | 签名抽象层（xhs 纯 Python；douyin/zhihu 走 Node） |
| `account/` | 账号池与代理池（Cookie 加密存储） |
| `database/models_v2.py` | v2 统一模型（story/chapter/comment/task/config…） |
| `api/routers/` | API：crawler / data / story / export / config / websocket |
| `exporters/` | PDF 导出渲染 |
| `store/checkpoint.py` | 断点续爬 |
| `scripts/` | 迁移、热度重算、环境自检 |

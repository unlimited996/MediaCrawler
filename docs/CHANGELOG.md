# 更新日志（CHANGELOG）

本项目自建的 WebUI / v2 数据层相关变更统一记录在此文件，按倒序排列。
每条记录与一次 Git 提交对应，提交信息前缀与这里的标题保持一致。

格式参考 Keep a Changelog：`新增 / 变更 / 修复` 三类。

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

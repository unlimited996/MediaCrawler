# 更新日志（CHANGELOG）

本项目自建的 WebUI / v2 数据层相关变更统一记录在此文件，按倒序排列。
每条记录与一次 Git 提交对应，提交信息前缀与这里的标题保持一致。

格式参考 Keep a Changelog：`新增 / 变更 / 修复` 三类。

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

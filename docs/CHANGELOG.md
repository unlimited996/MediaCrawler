# 更新日志（CHANGELOG）

本项目自建的 WebUI / v2 数据层相关变更统一记录在此文件，按倒序排列。
每条记录与一次 Git 提交对应，提交信息前缀与这里的标题保持一致。

格式参考 Keep a Changelog：`新增 / 变更 / 修复` 三类。

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

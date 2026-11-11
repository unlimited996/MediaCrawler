# 菜谱后端（KitchenHelper × MediaCrawler FastAPI）

把 KitchenHelper 的菜谱数据从「App 本地 assets/catalog.json + 私有 Garage 桶」迁到
**PostgreSQL（结构化数据）+ Garage（图片二进制）+ 现有 FastAPI 统一出口**。
没有新增服务、没有新增依赖，直接复用 `MediaCrawler/api` 这套 FastAPI + SQLAlchemy + Garage 客户端。

- 模型：`database/models_recipe.py`（7 张表，独立 `BaseRecipe`，全部在 schema `kitchenhelper` 下）
- 路由：`api/routers/recipe.py`（`/api/recipes`、`/api/media`、`/api/app-config`）
- 导入：`tools/recipe_import.py`（建 schema/表 + pg_trgm 索引 + 幂等灌数 + 自检）
- 迁移：`tools/recipe_move_schema.py`（把表从 public 搬进独立 schema 并去前缀）
- 种子：`database/seed/recipe_catalog.json`（catalog.json 的后端副本，已纳入版本管理）

## 一、表结构

全部表放在 **独立 schema `kitchenhelper`** 下（同在 `imdb` 库内），表名**不带前缀**：

```
kitchenhelper.category 1 ──< recipe >── N recipe_tag N ──> 1 tag
                              ├──< ingredient   UNIQUE(recipe_id, seq)
                              └──< step         UNIQUE(recipe_id, seq)

kitchenhelper.app_config  （独立 KV，供客户端拉配置）
```

为什么用 schema 而不是前缀：`public` 下已经有 44 张故事/采集的表，其中就有一张
`tag`（故事标签，1156 行）。建独立 schema 后，`tag` 这个名字可以放心用，也不用
每张表都挂 `recipe_` 前缀。

**不依赖 `search_path`**：模型里每张表都显式声明 `{"schema": "kitchenhelper"}`，
所有原生 SQL 都写全限定名。原因是一旦把 `search_path` 改成 `kitchenhelper,public`，
老代码里不带 schema 的 `select ... from tag` 就会打到菜谱标签上 —— 这是必须避免的串表。

菜谱主键沿用 App 深链用的 `分类名/菜名`（如 `蒸菜/酱香小河虾`），前端不用做映射。

| 表（`kitchenhelper.`） | 主键 | 关键列 | 约束 |
| --- | --- | --- | --- |
| `category` | `id varchar(32)` | `name varchar(32) UNIQUE`、`sort_order` | — |
| `tag` | `id serial` | `name varchar(24) UNIQUE`、`slug varchar(32) UNIQUE`、`kind` | schema 隔离，与 `public.tag`（故事标签）互不干扰 |
| `recipe` | `id varchar(64)` | `title`、`category_id FK`、`cover_key`、`process_key`、`intro`、`servings`、`duration_minutes`、`difficulty`、`variants jsonb`、`search_text`、`status` | `servings 1..50`；`duration>0`；`difficulty∈L1..L5`；`status∈published/draft/archived`；分类外键 **RESTRICT** |
| `ingredient` | `id bigserial` | `recipe_id FK`、`seq`、`name`、`amount numeric(10,2)`、`unit`、`note` | `UNIQUE(recipe_id,seq)`；`seq>=1`；`amount>0`；**有 amount 必须有 unit**；级联删除 |
| `step` | `id bigserial` | `recipe_id FK`、`seq`、`description`、`image_key`、`duration_seconds` | `UNIQUE(recipe_id,seq)`；`seq>=1`；`duration>0`；级联删除 |
| `recipe_tag` | `(recipe_id, tag_id)` | 复合主键（关联表） | 两端级联删除 |
| `app_config` | `key varchar(64)` | `value jsonb`、`scope`、`version`、`description` | — |

字段长度按真实数据实测取整（配料名最长 82 字 → `varchar(200)`；步骤最长 109 字 → `text`），
不是拍脑袋的 `varchar(255)` 一把梭。

索引（`create_all` 建常规索引，trgm 索引用原生 SQL 单独建，避免没装扩展时建表失败；
索引/约束/序列名同样不带 `recipe_` 前缀，与模型声明一一对应）：

```sql
idx_recipe_category / idx_recipe_difficulty / idx_recipe_duration / idx_recipe_status
idx_ingredient_recipe / idx_ingredient_name
idx_recipe_tag_tag
idx_recipe_search_trgm  ON kitchenhelper.recipe     USING gin (search_text gin_trgm_ops)
idx_ingredient_name_trgm ON kitchenhelper.ingredient USING gin (name gin_trgm_ops)
```

> 如果是从早期版本升级（表曾在 `public` 下且带 `recipe_` 前缀），跑一次
> `python -m tools.recipe_move_schema` 即可原地搬迁 + 改名，不重灌数据
> （整个迁移在单事务里，失败自动回滚）。

## 二、接口

统一前缀 `/api`。菜谱对象用 **camelCase**（和 App 现有 catalog.json 解析一致），
列表信封用 snake_case（和后端其它路由一致）。

### 读

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/recipes` | 列表 + 检索 + 分页 |
| GET | `/recipes/{id}` | 详情（配料/步骤按 seq、标签、相似推荐） |
| GET | `/recipes/categories` | 分类 + 菜谱数 |
| GET | `/recipes/tags` | 标签 + 使用次数（按次数降序） |
| GET | `/recipes/stats` | 总量、有图数、按分类/难度分布 |
| GET | `/recipes/bundle` | 整包（catalog.json 同构），ETag + gzip |

`/recipes` 支持的查询参数：

| 参数 | 说明 |
| --- | --- |
| `q` | 关键词，空格分词 **AND**，匹配菜名/分类/食材/标签（走 `search_text`） |
| `category` | 分类 **id 或中文名**都认 |
| `tags` | 标签名逗号分隔，如 `川菜,辣` |
| `tag_mode` | `any`（默认）/ `all` |
| `difficulty` | `L1`~`L5` |
| `max_duration` | 耗时上限（分钟） |
| `sort` | `relevance`（默认）/ `title` / `duration` / `difficulty` |
| `page` / `page_size` | 默认 20，上限 200 |

实测：

```
GET /api/recipes?q=鸡                    → total 105
GET /api/recipes?q=河虾                   → total 2（含「酱香小河虾」，食材命中）
GET /api/recipes?tags=川菜,辣&tag_mode=all → total 47
GET /api/recipes?category=蒸菜&difficulty=L3 → 粉蒸肉、酱香小河虾
```

响应（截断）：

```json
{
  "items": [{
    "id": "蒸菜/酱香小河虾", "title": "酱香小河虾",
    "categoryId": "steamed", "categoryName": "蒸菜",
    "coverKey": "kitchenhelper/food/蒸菜/酱香小河虾.webp",
    "coverUrl": "/api/media/kitchenhelper/food/%E8%92%B8%E8%8F%9C/...webp",
    "servings": 1, "durationMinutes": 15, "difficulty": "L3",
    "tags": ["家常","快手","川菜","辣","蒸","高蛋白"]
  }],
  "total": 180, "page": 1, "page_size": 20,
  "query": { "q": null, "category": null, "tags": null, "tag_mode": "any" }
}
```

详情额外返回 `ingredients[]`（`seq/name/amount/unit/note`）、`steps[]`
（`seq/description/imageKey/imageUrl/durationSeconds`）、`tagIds[]`、`variants[]`、
`related[]`（共享标签数 `sharedTags` + 同分类标记 `sameCategory`）。

### 写（管理用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/recipes` | upsert 单个（含配料/步骤/标签，传了就整段替换） |
| PUT | `/recipes/{id}` | 部分更新，只改请求里出现的字段 |
| DELETE | `/recipes/{id}` | 删除（子表级联） |
| POST | `/recipes/import` | 整包导入，body 为 catalog.json 结构，`{"reset":true}` 可先清空 |

写操作会重建 `search_text`、自动创建未知标签、并**立即失效 bundle 缓存**（ETag 随之变化）。
`difficulty` / `status` / `categoryId` 非法时返回 400 而不是 500。

### 媒体

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/HEAD | `/media/{key}` | 由服务端签名转发 Garage 对象 |

- `ETag` 来自 S3 对象，`If-None-Match` 命中直接 **304**（不回源）
- `Cache-Control: public, max-age=604800, stale-while-revalidate=86400`
- 分块吐流（256KB），不整包读进内存
- 这是绕开「Garage 私有桶不支持匿名读」的正式方案，替代了之前的本地 `remote_proxy.py`
- 本地联调：`s3_config.endpoint` 存的是 NAS 内网地址（`192.168.x.x`），办公网访问不到，
  起服务时加 `S3_ENDPOINT_OVERRIDE=http://<公网主机>:3900` 即可临时改走公网，
  不动库里的配置、也不影响线上进程

### 记食 / 我的餐桌（v2）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/meal-log?deviceId=&month=2026-11` | 按月取记录，附 `byDay` 打点计数 |
| GET | `/meal-log?deviceId=&day=2026-11-11` | 单日记录 |
| POST | `/meal-log` | `{"deviceId","eatenOn","meal","note","imageKey","recipeIds"}` |
| PATCH | `/meal-log/{id}` | 改餐次/日期/心情/这桌菜（**不重复累加** `cooked_count`） |
| DELETE | `/meal-log/{id}?deviceId=` | 删除 |
| GET | `/table-plans?deviceId=&favorite=` | 我的餐桌（上限 20，超了 POST 返回 409） |
| POST | `/table-plans` | `{"deviceId","title","people","taste","dishes":[...]}` |
| DELETE | `/table-plans/{id}?deviceId=` | 删除 |

`POST /meal-log` 关联了菜谱时会把对应 `recipe.cooked_count` +1——这是「做过几次」的
唯一来源，所以编辑（PATCH）刻意不碰计数，避免改个错别字把热度刷上去。

### 做饭页 / 采购 / 常点（v3）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/recipe-notes/{id}?deviceId=` | 这道菜的备注，没写过返回 `note: null` |
| PUT | `/recipe-notes/{id}` | `{"deviceId","content"}`，一人一菜一条，再存即覆盖（≤2000 字） |
| GET | `/shopping-checks?deviceId=&planKey=` | 这一桌已买齐的食材名 |
| PUT | `/shopping-checks` | `{"deviceId","planKey","itemName","checked"}`，`(device,plan,item)` 唯一 |
| GET | `/takeout-spots?deviceId=` | 我的常点 |
| POST | `/takeout-spots` | `{"deviceId","name","category","note"}`，同名返回 409 |
| DELETE | `/takeout-spots/{id}?deviceId=` | 删除常点 |

`planKey` 由客户端按「日期 + 菜谱集合」算，同一桌反复进出勾选状态不丢；换一桌就是新 key，
旧勾选自然作废，不会把上一桌的进度串过来。

> 图片源：`s3_config.endpoint` 已改成 `http://www.joyeuse.cn:3900`（公网），
> 本地后端 + 远端 PG（`www.joyeuse.cn:4321`）即可直接取图，不再需要 `S3_ENDPOINT_OVERRIDE`。

### 配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/app-config` | `{"items": {key: value}, "meta": {...}}` |
| GET | `/app-config/{key}` | 单条 |
| PUT | `/app-config/{key}` | `{"value": ..., "scope": "app", "description": "..."}` |
| DELETE | `/app-config/{key}` | 删除 |

导入时会自动写入 `catalog.meta`（版本、生成时间、各类行数），客户端可用它判断是否需要重新拉整包。

## 三、导入与运维

```bash
cd D:/workspace/MediaCrawler

# 建表 + 导入 + 自检（幂等，可重复跑）
.venv/Scripts/python.exe -m tools.recipe_import

# 只看解析结果不写库
.venv/Scripts/python.exe -m tools.recipe_import --dry-run

# 只跑自检
.venv/Scripts/python.exe -m tools.recipe_import --verify-only

# 清空菜谱域后重灌
.venv/Scripts/python.exe -m tools.recipe_import --reset

# 指定数据源
.venv/Scripts/python.exe -m tools.recipe_import --source D:/x/catalog.json
```

数据源查找顺序：`$KH_CATALOG` → `KitchenHelperAndroid/app/src/main/assets/catalog.json`
→ `database/seed/recipe_catalog.json`。找到哪个会打印，避免「以为导的是新数据」。
部署机没有 Android 工程时会自动用 `database/seed/` 里那份（已纳入 git）。

自检覆盖：外键悬挂、配料/步骤序号是否从 1 连续、`amount` 与 `unit` 配对、分类是否存在。
品类型菜谱（包子/饭团/杂粮饭，只有 `## 品类` 没有配料）单独归到「提示」，不算失败。

服务启动时会自动 `create_all` 补建缺失表（幂等，不动已有数据），失败只打日志不阻断 API。

### 从 public 搬到独立 schema（仅老库需要）

```bash
# 只看会做什么
.venv/Scripts/python.exe -m tools.recipe_move_schema --dry-run
# 执行：建 schema、搬 7 张表、去前缀、重命名索引/约束/序列
.venv/Scripts/python.exe -m tools.recipe_move_schema
```

幂等：已是新结构时只打印「无需迁移」。整个迁移在一个事务里，中途失败整体回滚，
不会留下「搬了一半」的状态。

### 启动服务

```bash
cd D:/workspace/MediaCrawler
.venv/Scripts/python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8081
# 已有实例占着 8081 时换端口：--port 8082
```

启动日志里会有一行 `[recipe] 表就绪 7 张：180 菜谱 / 932 配料 / ...`，
看到它说明菜谱域已挂好。接口文档在 `/docs`。

> 改完 `api/` 下的代码必须**重启进程**才生效（没有 --reload 时不会热更新）。

## 四、和 App 对接（已完成）

Android 端（`D:/KitchenHelperAndroid`）已切过来，数据模型没动，只换了数据来源：

1. **菜谱整包**：`data/CatalogFetcher.kt` 请求 `GET {base}/api/recipes/bundle`，
   带 `If-None-Match`，304 直接复用 `filesDir` 缓存；失败再回落 `assets/catalog.json`。
2. **图片**：`BuildConfig.IMAGE_BASE_URL` 默认 = `{base}/api/media`，
   清单里的 `coverKey`/`processKey` 是对象键，直接拼即可。

编译期一个开关搞定（默认指向模拟器宿主机）：

```bash
./gradlew assembleRelease -PapiBaseUrl=http://10.0.2.2:8081/api
```

实测（Pixel 5 / API 34）：

| 场景 | 结果 |
| --- | --- |
| 把 `assets/catalog.json` 与 `assets/Library` 全部移出 APK 后打包 | 仍显示 180 道菜、缩略图正常 → 数据与图片确实都来自本服务 |
| 第二次启动 | 日志 `304 Not Modified`，不重复下载 316KB |
| 停掉服务 + `pm clear` 清缓存 | 日志 `来源=assets，180 道菜`，离线可用 |
| release 包（R8 裁剪）体积 | 1.8MB |

## 五、性能与注意事项

- **检索**：`search_text` 是写入时维护的冗余列（标题+分类+食材+标签），
  多词 AND 走 `ILIKE '%x%'`，由 pg_trgm 的 GIN 索引支撑。
  数据量小（180 行）时 PG 会主动选择顺序扫描，这是正确选择；关闭 seqscan 后可验证
  确实走 `Bitmap Index Scan on idx_recipe_search_trgm`。
  单字查询（`q=鸡`）trigram 退化，量大后再考虑 `pg_bigm` 或 jieba/zhparser 分词。
- **整包**：冷启动 542ms / 热 31ms（30s 进程内缓存），309KB → gzip 41KB（13%）。
- **N+1**：列表页标签用一次 `IN` 查询批量取，详情页固定 5 条 SQL，没有逐行查询。
- **Garage 限制**：仍不支持 ACL / bucket policy，所以「公开读」只能靠本服务的
  `/api/media` 代理（或另行上 CDN 回源）。这是架构上的唯一取舍。
- **凭据**：S3 密钥继续走 `s3_config` 表（Fernet 加密，API 只回掩码），本次没有新增明文配置。

## 六、已知边界

- `intro` 目前全为空（源数据没有简介），前端需回落到分类短评；表字段已就位。
- `difficulty` 只有 46 道菜有值（来自 `curriculum.json` 的 L1~L5），其余为 NULL，
  所以 `/recipes/stats` 里 `byDifficulty.unknown = 134` 是正常的。
- 步骤配图 `image_key` 恒为 NULL（源数据没有分步图），字段预留。
- 收藏/库存仍存在 App 本地（SharedPreferences / JSON），未入库；需要多端同步时再加
  `user_favorite` / `user_inventory` 两张表即可，主键建议 `(device_id, recipe_id)`。

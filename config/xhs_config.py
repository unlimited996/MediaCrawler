# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/xhs_config.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。


# Xiaohongshu platform configuration

# Sorting method, the specific enumeration value is in media_platform/xhs/field.py
SORT_TYPE = "popularity_descending"

# Specify the note URL list, which must carry the xsec_token parameter
XHS_SPECIFIED_NOTE_URL_LIST = [
    "https://www.xiaohongshu.com/explore/6aa3d827000000002802c5c8?xsec_token=YBIq8sY-0_K3BACQ7z57J9xJcdflrd8BAf5_zBeJtFMOQ=&xsec_source=pc_creatormng"
    # ........................
]

# Specify the creator URL list, which needs to carry xsec_token and xsec_source parameters.

XHS_CREATOR_ID_LIST = [
    "https://www.xiaohongshu.com/user/profile/5f58bd990000000001003753?xsec_token=ABYVg1evluJZZzpMX-VWzchxQ1qSNVW3r-jOEnKqMcgZw=&xsec_source=pc_search"
    # ........................
]

# account 模式：账号池中的账号 ID（数字字符串）。
# 定时任务「登录用户」维度用它指定抓哪个扫码登录用户的点赞/收藏图文帖；
# 为空时由爬虫从账号池挑健康度最高的账号。
XHS_ACCOUNT_ID = ""

# account 模式抓取的行为来源：likes（点赞）/ collects（收藏），逗号分隔。
# 说明：小红书网页端没有「我评论过的笔记」列表接口，评论维度暂无法覆盖。
XHS_ACCOUNT_BEHAVIORS = "likes,collects"

# ---- 环境变量助手（与 base_config 的同名实现保持一致，避免循环导入）----
import os as _os


def _env_int(name: str, default: int) -> int:
    raw = _os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _os.getenv(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = _os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ==================== 小红书反检测 / 反风控配置 ====================
# 目标：最大化复用本地真实浏览器的登录态与浏览器指纹，把「机器特征」
# 降到最低；请求节奏全部随机化，避免固定间隔被风控识别。
#
# 以下每一项都可用环境变量覆盖（便于 Docker / 临时切换），
# 也可在 WebUI 配置面板（crawl.cdp_* 系列）调整。

# 是否默认走 CDP 模式：连接本地真实 Chrome/Edge（复用登录态、扩展与历史），
# 不带 Playwright 的自动化测试标识（--enable-automation 等已剔除）
XHS_ENABLE_CDP_MODE = _env_bool("XHS_ENABLE_CDP_MODE", True)

# True = 连接用户已经开着远程调试的浏览器（反检测效果最好）；
# False = 由程序自己拉起一个带调试端口的浏览器
XHS_CDP_CONNECT_EXISTING = _env_bool("XHS_CDP_CONNECT_EXISTING", True)

# CDP 模式是否无头。小红书默认关闭无头（有头窗口风控阈值最低，
# 且验证码弹出时用户能第一时间看到并处理）
XHS_CDP_HEADLESS = _env_bool("XHS_CDP_HEADLESS", False)

# 抓取结束后是否关闭浏览器。小红书默认 False：保持浏览器常开，
# 登录态/Cookie/指纹跨任务连续，避免每次冷启动
XHS_AUTO_CLOSE_BROWSER = _env_bool("XHS_AUTO_CLOSE_BROWSER", False)

# 并发数。小红书默认 1：串行请求最像真人，风控最低
XHS_MAX_CONCURRENCY_NUM = _env_int("XHS_MAX_CONCURRENCY_NUM", 1)

# 抓取请求之间的随机间隔（秒）：每次在 [MIN, MAX] 里随机取一个值
XHS_SLEEP_MIN_SEC = _env_float("XHS_SLEEP_MIN_SEC", 5)
XHS_SLEEP_MAX_SEC = _env_float("XHS_SLEEP_MAX_SEC", 15)

# 触发安全验证时，等待用户人工完成验证的最长时间（秒）
XHS_CAPTCHA_WAIT_SEC = _env_int("XHS_CAPTCHA_WAIT_SEC", 300)

# ---- account 模式（登录用户的点赞 / 收藏）----
# 「本次抓取上限」：本轮任务最多处理多少篇笔记。**新点赞/收藏 + 已入库待补评论
# 的老帖共用同一个上限**，这是 UI「本次抓取上限」字段（任务选项 max_notes）落到
# 爬虫侧的唯一值；不填则用这里默认 40。
# 2026-10-31 实跑结论：一轮 82 篇详情连打约 9.5 分钟就撞 461（无论详情还是评论
# 接口，撑的时间都高度一致），说明存在与顺序无关的速率上限。把单轮会话压到
# 40 篇，配合下面 XHS_SUB_COMMENT_MAX_PAGES 砍掉楼中楼深翻，才能真正缩短会话。
# 坑（2026-10-29）：原先补抓另有独立的 XHS_COMMENT_BACKFILL_MAX_NOTES（默认 20），
# 与「本次抓取上限」各算各的，于是「上限设了 100、补抓却只有 20」—— 已删除该
# 冗余变量，补抓直接复用本值，保证「本次抓取上限」名副其实。
# 为什么需要这个上限：单篇热门帖评论可达数万条，一轮无限制全抓会变成数小时、
# 数千请求的连续会话 —— 正是小红书行为风控（验证码/访问频繁）的典型触发面。
# 上限把单次会话长度锁住，跑不完的部分由增量模式在后续任务里继续补。
XHS_BEHAVIOR_MAX_NOTES = _env_int("XHS_BEHAVIOR_MAX_NOTES", 40)

# 是否增量：已入库的帖子只跳过详情+评论，只补新帖。
# 495 条点赞全量重抓评论要跑十几个小时，日常备份任务没有意义；
# 想强制刷新老帖（补头像/补评论图等）时把它设成 0。
XHS_BEHAVIOR_INCREMENTAL = _env_bool("XHS_BEHAVIOR_INCREMENTAL", True)

# 列表翻页早停：连续 N 页都没有「新增待抓帖」（全是已归档/视频）就停止翻页。
# 点赞/收藏列表按时间倒序（新帖在最前），一旦连续几页都是老的已处理内容，
# 后面的只会更老 —— 提前收工，避免每次任务都把整份列表从头翻一遍。
# 未完成的帖子不靠列表发现，而是由「按 note_id + 已存 xsec_token 直接续抓」
# 优先处理（见 core.get_account_behavior_notes）。0 = 关闭早停。
XHS_BEHAVIOR_EARLY_STOP_PAGES = _env_int("XHS_BEHAVIOR_EARLY_STOP_PAGES", 3)

# ---- 评论抓取的「量」怎么定（2026-10-28 重做）----
# 单篇评论抓取上限（一级评论条数）。0 = 不额外限制，按「原帖显示评论量」
# 抓全（declared + 余量）；>0 时取 min(按声明数算出的量, 本值) 作为硬上限。
# 说明：原先小红书统一吃 CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES=30，
# 但声明 1000 条的帖子抓 30 条就「抓完了」的说法站不住脚 —— 完整率
# 只有 3%，增量模式却当它已归档，评论永远补不上。
# 代价：热门帖要翻很多页（每页 10 条、页间隔 5-15s），一篇 1000 条
# 评论约 100 页 ≈ 20 分钟。想控制时长就把它设成 200/500 之类。
XHS_COMMENT_MAX_PER_NOTE = _env_int("XHS_COMMENT_MAX_PER_NOTE", 0)

# 声明评论数的余量：平台显示的评论数常含已删除/被折叠的，按它的
# 100% 当上限容易差几条翻不到底，多给一点余量。
XHS_COMMENT_COUNT_MARGIN = _env_int("XHS_COMMENT_COUNT_MARGIN", 30)

# 楼中楼（子评论）翻页深度上限：单条主评论最多翻几页楼中楼。
# 2026-10-31 实跑实测：**sub/page 服务端固定只返回 5 条/页**（请求 num=30 也只给
# 5，和主评论固定 10 条/页同理）。于是声明 39 条回复的 root 要翻 8 页、51 条要翻
# 10 页，按 5-15s 页间隔算单个 root 就要 1.5-2 分钟 —— 这是请求量爆炸的真凶。
# 默认 3 页 ≈ 15 条回复，够覆盖绝大多数楼中楼的正常讨论深度。
# 0 = 不限制（回到旧行为，热门帖会深翻到上百页、极易触发风控）。
#
# 重要：触顶截断属于**主动放弃**，会被计入「策略放弃条数」并从完整率分母里扣掉
# （见 tools/v2_sync 的 skipped_sub_count），否则这些帖永远凑不满 90%、
# 每轮都被当成未达标重抓，反而白打更多请求。
XHS_SUB_COMMENT_MAX_PAGES = _env_int("XHS_SUB_COMMENT_MAX_PAGES", 3)

# ---------------------------------------------------------------------------
# 单轮评论接口请求量硬上限（默认 60，0 = 不限制/回到旧行为）。
#
# 背景：小红书对「评论接口」的限流是**按累计请求量**触发的，不是纯速率。
# 实测 2026-10-31：一轮里热门帖的深层楼中楼把评论接口请求推到 ~90 次就把账号
# 打挂（461），导致该轮 81/82 篇没抓到评论、整轮报废。5-15s 的页间隔只能压低
# 速率，压不住「单轮总量」——一篇声明 800 条评论的帖子深翻起来就是几十上百次。
#
# 这个预算是 461 之前的**主动止血点**：达到上限就停止继续打评论接口
# （comment/page 与 sub/page 都算），把剩余帖子留到下一轮（尊重风控冷却）。
# 设成明显低于触发阈值（~90）的值，账号就永远不会进入惩罚态，下一轮能立刻再跑。
#
# 取舍：预算越小越安全但单轮进度越慢（60 ≈ 热门帖 1-2 篇/轮）。想更快就调大，
# 但超过 ~90 就会重新撞 461。0 表示不限制（完全交给熔断 + 冷却兜底）。
# ---------------------------------------------------------------------------
XHS_COMMENT_BUDGET_PER_ROUND = _env_int("XHS_COMMENT_BUDGET_PER_ROUND", 60)

# 评论补抓的「篇数」上限已统一到上面的 XHS_BEHAVIOR_MAX_NOTES
# （即「本次抓取上限」字段），不再单独设值——避免「字段设了 100、补抓却只有 20」
# 的歧义。下面这个是另一个维度的预算（评论「条数」总预算），见其注释。

# 评论补抓「每篇预留额度」：account 备份任务里，评论条数预算 =
# 本次抓取上限(XHS_BEHAVIOR_MAX_NOTES) × 本值，从而使每轮选中的待补批次
# ≈ 本次抓取上限（不再被死值压成 9 篇，详见 core.get_account_behavior_notes）。
# 它与「篇数上限」是不同维度：篇数上限管「处理多少篇」，本值管「每篇预留
# 多少条评论额度」。50000 条评论 ≈ 5000 页 × 5-15s ≈ 十几小时，所以单帖
# 评论极多的帖子（声明数几万）仍会按 XHS_COMMENT_MAX_PER_NOTE 分轮补齐。
XHS_COMMENT_BACKFILL_MAX_COMMENTS = _env_int("XHS_COMMENT_BACKFILL_MAX_COMMENTS", 4000)



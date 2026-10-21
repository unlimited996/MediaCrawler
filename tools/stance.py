# -*- coding: utf-8 -*-
"""内容赞同度：发帖人观点提取 + 评论立场判定。

与 sentiment.py（绝对情感）的区别：本模块判定评论**相对于发帖人观点**的立场，
输出 赞同(agree) / 中立(neutral) / 反对(oppose) 三态，用于「内容赞同度」指标。

流程：
1. extract_post_stance()  分析发帖人核心观点（对话题持支持/反对/中立），
   并提取话题关键词，供后续评论做话题一致性校验；
2. judge_comment_stance() 对单条评论做立场判定：
   a. 直接指向发帖人的表达（「说得对」「不敢苟同」）优先级最高；
   b. 无指代时结合评论自身情感与帖子立场做映射
      （帖子支持 + 评论正面 → 赞同；帖子批评 + 评论负面 → 赞同）；
   c. 话题不相关或置信度不足一律中立——宁可中立不可错判。
"""

import re
import time
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

# 指向发帖人的直接态度表达
_AGREE_DIRECT = {
    "说得对", "说得好", "没错", "确实", "的确", "确实如此", "就是这样",
    "赞同", "支持楼主", "支持作者", "支持博主",
    "三观正", "三观好正", "三观超正", "三观太正", "人间清醒", "清醒",
    "感谢分享", "谢谢分享", "感谢科普", "受教了", "长见识了", "开眼了",
    "干货", "干货满满", "有用", "很有帮助", "帮助很大", "学到了",
    "收藏了", "码住", "马克", "顶你", "顶楼主", "支持一下", "力挺",
    "在理", "有理", "有道理", "确实是", "就是这样", "一语中的", "一针见血",
    "客观", "中肯", "公道", "良心", "良心帖", "良心博主", "敢说", "说实话",
    "说到点子上", "说到做到", "看透了", "看明白", "看懂了",
}
_OPPOSE_DIRECT = {
    "不敢苟同", "不苟同", "不敢认同", "无法认同", "不能认同", "难以认同",
    "反对", "抗议", "不敢恭维", "不敢苟同。", "楼主错了", "作者错了",
    "博主错了", "你错了", "搞错了", "弄错了", "因果倒置", "本末倒置",
    "以偏概全", "一概而论", "一刀切", "扣帽子", "带节奏", "蹭流量",
    "编的", "剧本", "摆拍", "照骗", "营销号", "标题党", "引流",
    "误导", "误人子弟", "毒鸡汤", "洗地", "硬洗", "瞎编", "造谣",
    "假的", "不属实", "不可信", "存疑", "言过其实",
    "想多了", "想太多", "过了", "过头", "言重了",
}
# 注意：「离谱 / 逆天 / 双标 / 就这」这类词常针对话题对象而非发帖人，
# 放进 direct 会把「一起吐槽」误判成反对——交给情感映射处理。

# 嘲讽 / 质疑发帖人的口语表达：情感词典判不出负面（字面无贬义词），
# 但语义上是在怼发帖人，单列一档中优先级判反对。
_MOCK_OPPOSE = {
    "给你狂的", "给你能的", "多此一举", "有什么用", "有啥用", "不至于吧",
    "你确定", "认真的吗", "认真的么", "真的假的", "一眼假", "编的吧",
    "装的吧", "别装了", "凡尔赛", "秀优越", "何不食肉糜", "谁问你了",
    "没人问你", "有必要吗", "想啥呢", "想什么呢", "大可不必",
}

# 帖子立场信号
_POST_SUPPORT = {
    "推荐", "安利", "种草", "喜欢", "爱了", "支持", "赞同", "赞成",
    "感谢", "感恩", "辛苦", "厉害", "优秀", "值得", "靠谱", "真香",
    "太好", "完美", "惊艳", "治愈", "干货", "收藏", "码住",
    "强烈推荐", "回购", "无限回购",
}
_POST_OPPOSE = {
    "吐槽", "批评", "曝光", "避雷", "避坑", "翻车", "塌房", "举报",
    "投诉", "维权", "退款", "退货", "差评", "垃圾", "恶心", "无语",
    "离谱", "逆天", "抽象", "下头", "油腻", "双标", "道德绑架",
    " PUA", "pua", "PUA", "洗地", "硬洗", "辟谣", "打假", "假的",
    "反对", "抵制", "拒绝", "千万别", "不要再", "避雷了", "踩坑",
    "后悔", "被骗", "割韭菜", "智商税", "标题党", "营销号",
}

# 纯笑声 / 情绪符号：哈哈哈、笑死、[笑哭R] 之类。
# 这类评论只是在「笑」，既不赞同也不反对发帖人，必须判中立——
# 否则情感词典把「笑」算正面，会整片误判成赞同。
_LAUGH_RE = re.compile(
    r"哈哈|呵呵|嘿嘿|嘻嘻|笑死|笑哭|笑不活|笑尿|笑崩|[哈呵嘿嘻笑哭]{1,}|"
    r"\[[^\[\]]{1,8}[Rr]\]|[\U0001F300-\U0001FAFF\u2600-\u27BF]")

# 话题无关的短评论（纯打卡/围观）——立场必中立
_OFFTOPIC_MARKERS = {
    "路过", "围观", "吃瓜", "前排", "沙发", "留名", "记号", "打卡",
    "mark", "MARK", "Mark", "膜拜", "沾沾", "接好运", "求更新",
}

# 高频虚词：做字面相关性时不过滤会把「的/了/个」当成话题重合
_COMMON_CHARS = set(
    "的了是在我有和就不人都一个上下中也很还又把被对但而或什么怎么如果可以因为所以自己一样没有看到"
    "这那你我们他们她们它吗呢吧啊哦嗯哈")

# 停用词（提取话题关键词时过滤）
_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "我们", "你们",
    "他们", "这", "那", "这个", "那个", "有", "和", "就", "不", "人",
    "都", "一个", "上", "下", "中", "为", "也", "很", "还", "又", "被",
    "把", "对", "但", "但是", "而且", "或者", "什么", "怎么", "如果",
    "可以", "因为", "所以", "自己", "一样", "不是", "没有", "看到",
}

# ---------------------------------------------------------------------------
# 发帖人核心观点
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


# 话题串：`#论文排版[话题]#` / `#AI` —— 平台把话题拼在正文里，
# 参与关键词提取会污染话题画像（一堆与正文无关的词被当成核心话题）
_TOPIC_TAG_RE = re.compile(r"#([^#\]]+?)\[话题\]#|#([A-Za-z0-9_\-一-龥]{1,30})")


def strip_topics(text: str) -> str:
    """去掉文本里的话题标签，只留正文（用于话题画像与相关性比对）。"""
    return _clean(_TOPIC_TAG_RE.sub(" ", text or ""))


def extract_keywords(text: str, top_k: int = 8) -> List[str]:
    """提取话题关键词（jieba TF 排序；失败时退化为 2-4 字滑窗）。"""
    cleaned = _clean(text)
    if not cleaned:
        return []
    try:
        import jieba
        words = [w.strip() for w in jieba.lcut(cleaned)
                 if len(w.strip()) >= 2 and w.strip() not in _STOPWORDS]
    except Exception:
        words = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,4}", cleaned)
    freq: Dict[str, int] = {}
    for w in words:
        if w in _STOPWORDS or len(w) < 2:
            continue
        freq[w] = freq.get(w, 0) + 1
    # 长词优先（信息量更高），同频按长度
    ranked = sorted(freq.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    return [w for w, _ in ranked[:top_k]]


def extract_post_stance(title: str = "", content: str = "",
                        summary: str = "") -> Dict[str, Any]:
    """分析发帖人核心观点。

    返回 {
      stance:   support | oppose | neutral   （发帖人对核心话题的态度）
      keywords: [话题关键词]                  （评论做话题一致性校验用）
      thesis:   核心观点一句话摘要
      confidence: 0~1
    }
    """
    title_c = _clean(title)
    body_c = _clean(content) or _clean(summary)
    raw = f"{title_c} {body_c}".strip()
    if not raw:
        return {"stance": "neutral", "keywords": [], "thesis": "",
                "topic_text": "", "confidence": 0.0}

    # 话题画像只取正文（剥离 #话题#），否则话题词会盖过真正的讨论对象
    topic_text = strip_topics(raw)
    support_hits = sum(1 for w in _POST_SUPPORT if w in topic_text)
    oppose_hits = sum(1 for w in _POST_OPPOSE if w in topic_text)

    if oppose_hits > support_hits:
        stance = "oppose"
    elif support_hits > oppose_hits:
        stance = "support"
    else:
        stance = "neutral"

    keywords = extract_keywords(topic_text or raw)
    # 核心观点：标题优先，标题为空取正文首句
    thesis = title_c or (body_c.split("。")[0][:80] if body_c else "")
    confidence = min(1.0, abs(oppose_hits - support_hits) / 3.0) if stance != "neutral" else 0.3
    # topic_text 保留话题标签：相关性比对要认 `#自建房[话题]#` 里的「自建房」，
    # 只在提取关键词时剔除，避免话题词盖过真正的讨论对象
    return {"stance": stance, "keywords": keywords, "thesis": thesis[:120],
            "topic_text": raw[:300], "confidence": round(confidence, 2)}


# ---------------------------------------------------------------------------
# 评论立场判定
# ---------------------------------------------------------------------------

def _topic_overlap(comment: str, keywords: List[str]) -> float:
    """评论与帖子话题的重叠度（命中关键词数 / 总关键词数）。"""
    if not keywords:
        return 0.0
    hits = sum(1 for k in keywords if k in comment)
    return hits / len(keywords)


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fa5"


def _char_overlap(comment: str, topic_text: str) -> float:
    """字面重合度：评论里的实词字有多少出现在帖子话题里。

    关键词（分词）命中率对短评论太苛刻——「有4个灶给你狂的」里的「灶」
    未必凑成跟帖子一样的词，但明显在讨论同一件事。所以关键词之外再兜一层
    字级别重合，高频虚词（的/了/啊/哈）先剔掉，避免「哈哈哈」这类评语
    被误判成相关。
    """
    if not comment or not topic_text:
        return 0.0
    cs = {c for c in comment if _is_cjk(c) and c not in _COMMON_CHARS}
    if not cs:
        return 0.0
    ts = {c for c in topic_text if _is_cjk(c)}
    if not ts:
        return 0.0
    return len(cs & ts) / len(cs)


def _relatedness(comment: str, keywords: List[str], topic_text: str) -> float:
    """评论与帖子话题的相关度 0~1：关键词命中 > 字面重合。"""
    if any(k and k in comment for k in keywords):
        return 1.0
    return _char_overlap(comment, topic_text)


def judge_comment_stance(comment_text: str,
                         post_stance: Optional[Dict[str, Any]] = None,
                         comment_sentiment: Optional[str] = None) -> Dict[str, Any]:
    """判定单条评论相对发帖人观点的立场。

    返回 {stance: agree|neutral|oppose, confidence: 0~1, reason: 判定依据}
    """
    text = _clean(comment_text)
    if not text:
        return {"stance": "neutral", "confidence": 0.0, "reason": "empty"}

    ps = post_stance or {}
    p_stance = ps.get("stance", "neutral")
    keywords = ps.get("keywords") or []
    topic_text = ps.get("topic_text") or ""

    # 纯围观/打卡 → 中立
    if any(m in text for m in _OFFTOPIC_MARKERS) and len(text) <= 20:
        return {"stance": "neutral", "confidence": 0.5, "reason": "offtopic"}

    # 1) 直接指向发帖人的表达，优先级最高
    if any(w in text for w in _OPPOSE_DIRECT):
        return {"stance": "oppose", "confidence": 0.9, "reason": "direct_oppose"}
    if any(w in text for w in _AGREE_DIRECT):
        return {"stance": "agree", "confidence": 0.9, "reason": "direct_agree"}

    # 1.2) 嘲讽 / 质疑发帖人的口语表达（情感词典判不出负面）
    if any(w in text for w in _MOCK_OPPOSE):
        return {"stance": "oppose", "confidence": 0.7, "reason": "mock_oppose"}

    # 1.5) 去掉笑声与表情后没剩下实质内容 → 中立（纯情绪，无立场）
    residue = re.sub(r"[\s，。！？、,.!?~～…\-—_]", "", _LAUGH_RE.sub("", text))
    if len(residue) < 2:
        return {"stance": "neutral", "confidence": 0.5, "reason": "noise_only"}

    # 2) 话题不相关 → 中立（不参与赞同度）
    #    阈值放宽：关键词命中或字面重合任一成立即算相关，只有又短又完全
    #    不沾边（「哈哈哈」「太绝了」这类无指向感叹）才判无关。
    rel = _relatedness(text, keywords, topic_text)
    if rel <= 0 and len(text) < 40:
        return {"stance": "neutral", "confidence": 0.4, "reason": "off_topic"}

    # 3) 情感 × 帖子立场映射
    if comment_sentiment is None:
        from tools.sentiment import analyze_sentiment
        comment_sentiment = analyze_sentiment(text)[0]

    if comment_sentiment == "positive":
        mapped = "agree" if p_stance in ("support", "neutral") else "oppose"
    elif comment_sentiment == "negative":
        mapped = "agree" if p_stance == "oppose" else "oppose"
    else:
        # 中立情感：话题高度相关时按「不反对」处理，否则中立
        if p_stance != "neutral" and rel >= 0.5:
            return {"stance": "agree", "confidence": 0.4, "reason": "related_neutral"}
        return {"stance": "neutral", "confidence": 0.3, "reason": "neutral_tone"}

    conf = 0.5 if p_stance == "neutral" else 0.6
    if rel >= 0.34:
        conf += 0.2
    return {"stance": mapped, "confidence": round(min(conf, 0.9), 2),
            "reason": f"map({p_stance}+{comment_sentiment})"}


# ---------------------------------------------------------------------------
# 赞同度档位（对齐 Steam 中文区结构，文案改为「赞同」系）
# ---------------------------------------------------------------------------

AGREEMENT_TIERS = (
    (0.95, "rating.agreeOverwhelming"),    # 一致赞同
    (0.80, "rating.agreeVery"),            # 特别赞同
    (0.70, "rating.agreeMostly"),          # 多半赞同
    (0.40, "rating.agreeMixed"),           # 观点分化
    (0.20, "rating.opposeMostly"),         # 多半反对
    (0.05, "rating.opposeVery"),           # 强烈反对
    (0.00, "rating.opposeOverwhelming"),   # 一致反对
)


def agreement_label(agree: int, total: int, neutral: int = 0) -> str:
    """赞同率 → 档位文案 key。

    分母取**已表态评论**（agree + oppose），不计中立：中文评论区里「哈哈哈
    /路过/接好运」这类无立场评论往往占七八成，把它们算进分母会把「8 赞 1 踩」
    硬生生压成「强烈反对」。没任何人表态时返回空串（前端不展示档位）。
    """
    if total <= 0:
        return ""
    decided = max(0, total - max(0, neutral))
    if decided <= 0:
        return ""
    ratio = agree / decided
    for threshold, label in AGREEMENT_TIERS:
        if ratio >= threshold:
            return label
    return "rating.opposeOverwhelming"


if __name__ == "__main__":
    # ---- 发帖人观点 ----
    posts = [
        ("大家评评理🥺 我家在四川，灶都是 4 个…", "吐槽贴：对家里灶台布置不满"),
        ("这款面霜我真的无限回购", "种草"),
        ("今天路过一家咖啡店，随手拍了一张", "中性记录"),
    ]
    for title, note in posts:
        ps = extract_post_stance(title=title)
        print(f"帖子[{ps['stance']:8s}] {note}: {ps['thesis'][:30]} | kw={ps['keywords'][:4]}")

    # ---- 评论立场（以「吐槽贴」为基准） ----
    post = extract_post_stance(title="大家评评理🥺 我家在四川，灶都是 4 个，3 个柴火灶，一个煤气灶")
    print("\n基准帖子立场:", post["stance"], "| 关键词:", post["keywords"][:5])
    cases = [
        ("感谢分享，说得好", "agree"),
        ("不敢苟同，楼主这观点有问题", "oppose"),
        ("路过围观", "neutral"),
        ("确实，这种布局太离谱了", "agree"),      # 一起吐槽 → 赞同
        ("我觉得这样挺合理的啊，楼主想多了", "oppose"),  # 反驳 → 反对
        ("哈哈哈笑死", "neutral"),                      # 纯笑声 → 中立
        ("以偏概全了吧", "oppose"),
        ("有4个灶给你狂的", "oppose"),                    # 嘲讽发帖人 → 反对
        ("垃圾桶上锁 多此一举", "oppose"),                # 质疑做法 → 反对
        ("现在回想，我导人太好了！！！帮我改了7稿", "neutral"),  # 与基准帖无关 → 中立
        ("在你们嘲讽贴主的时候，他已经去平分万元奖金了", "neutral"),
    ]
    ok = 0
    for text, expect in cases:
        r = judge_comment_stance(text, post)
        mark = "OK " if r["stance"] == expect else "MISS"
        if r["stance"] == expect:
            ok += 1
        print(f"{mark} [{r['stance']:7s} {r['confidence']:+.1f} {r['reason']:16s}] {text[:28]}")
    print(f"{ok}/{len(cases)} passed")


# ---------------------------------------------------------------------------
# 态度标签库（P3）：判定逻辑与代码解耦
#
# 标签与规则全部存在库表（stance_tag / stance_tag_rule），本模块只负责：
#   1. 加载库表到内存（版本号比对，改完立即生效）；
#   2. 按 priority + weight 匹配规则；
#   3. 一条都没命中 → 返回 is_fallback 标签。
# 库还没初始化时（进程刚起 / 独立脚本）用内置种子兜底，行为与旧版一致。
# ---------------------------------------------------------------------------

#: 内置种子：库表为空时写入；也作为无库场景的兜底判定依据
SEED_TAGS: List[Dict[str, Any]] = [
    {
        "code": "agree", "name": "赞同", "polarity": "agree", "color": "#22c55e",
        "priority": 40, "sort_order": 10, "is_fallback": False,
        "description": "认同、支持发帖人的观点或做法",
        "examples": ["说得对", "确实如此", "收藏了，谢谢分享"],
        "rules": [("keyword", w, 1.0) for w in sorted(_AGREE_DIRECT)]
                 + [("sentiment", "positive|support,neutral", 0.6),
                    ("sentiment", "negative|oppose", 0.6),
                    ("sentiment", "neutral|support,oppose", 0.4)],
    },
    {
        "code": "oppose", "name": "反对", "polarity": "oppose", "color": "#f43f5e",
        "priority": 42, "sort_order": 20, "is_fallback": False,
        "description": "明确反驳发帖人的观点",
        "examples": ["不敢苟同", "以偏概全", "楼主想多了"],
        "rules": [("keyword", w, 1.0) for w in sorted(_OPPOSE_DIRECT)]
                 + [("sentiment", "positive|oppose", 0.6),
                    ("sentiment", "negative|support,neutral", 0.6)],
    },
    {
        "code": "mock", "name": "嘲讽", "polarity": "oppose", "color": "#f43f5e",
        "priority": 14, "sort_order": 30, "is_fallback": False,
        "description": "嘲讽、调侃发帖人（字面常无贬义词，词典判不出负面）",
        "examples": ["有4个灶给你狂的", "垃圾桶上锁 多此一举", "凡尔赛"],
        "rules": [("keyword", w, 1.0) for w in sorted(_MOCK_OPPOSE)],
    },
    {
        "code": "sarcasm", "name": "阴阳", "polarity": "oppose", "color": "#fb923c",
        "priority": 10, "sort_order": 40, "is_fallback": False,
        "description": "阴阳怪气、反话正说",
        "examples": ["好棒棒", "可还行", "真有你的", "不愧是你"],
        "rules": [("keyword", w, 1.0) for w in
                  ("好棒棒", "可还行", "真有你的", "不愧是你", "阴阳怪气",
                   "阴阳", "这也叫", "我竟无言)", "好家伙")],
    },
    {
        "code": "doubt", "name": "质疑", "polarity": "oppose", "color": "#eab308",
        "priority": 12, "sort_order": 50, "is_fallback": False,
        "description": "质疑真实性、动机或数据",
        "examples": ["真的假的", "编的吧", "求锤", "数据哪来的"],
        "rules": [("keyword", w, 1.0) for w in
                  ("真的假的", "真的吗", "你确定", "认真的吗", "认真的么",
                   "一眼假", "编的吧", "编的", "求锤", "数据哪来的",
                   "有证据吗", "营销吧")],
    },
    {
        "code": "speechless", "name": "无语", "polarity": "neutral", "color": "#a78bfa",
        "priority": 16, "sort_order": 60, "is_fallback": False,
        "description": "无语、服了，不站队只是吐槽情绪",
        "examples": ["无语", "服了", "麻了", "醉了"],
        "rules": [("keyword", w, 1.0) for w in
                  ("无语", "服了", "麻了", "醉了", "我服", "受不了", "离了大谱")],
    },
    {
        "code": "wait", "name": "观望", "polarity": "neutral", "color": "#38bdf8",
        "priority": 18, "sort_order": 70, "is_fallback": False,
        "description": "吃瓜观望、等后续，不下结论",
        "examples": ["蹲一个后续", "看看再说", "观望一下"],
        "rules": [("keyword", w, 1.0) for w in
                  ("蹲一个", "蹲后续", "等后续", "看看再说", "观望", "等一个",
                   "坐等", "前排吃瓜", "持续关注")],
    },
    {
        "code": "god_comment", "name": "神评", "polarity": "agree", "color": "#f472b6",
        "priority": 60, "sort_order": 80, "is_fallback": False,
        "description": "高赞且被认同的精彩评论（点赞进入全帖前 30%）",
        "examples": ["这条评论比正文还精彩"],
        "rules": [("popular", "0.3", 1.0)],
    },
    {
        "code": "neutral", "name": "中立", "polarity": "neutral", "color": "#94a3b8",
        "priority": 90, "sort_order": 990, "is_fallback": True,
        "description": "无明确立场（含纯笑声、打卡围观）",
        "examples": ["路过", "前排"],
        "rules": [],
    },
    {
        "code": "laugh", "name": "笑声", "polarity": "neutral", "color": "#cbd5e1",
        "priority": 5, "sort_order": 5, "is_fallback": False,
        "description": "纯笑声 / 表情刷屏，无实质观点",
        "examples": ["哈哈哈", "hhhh", "笑死"],
        "rules": [("laughter", "", 0.5)],
    },
]

#: 进程内缓存：{version, tags:[{...rules}], loaded_at}
_LIB_CACHE: Dict[str, Any] = {"version": 0, "tags": [], "loaded_at": 0.0}
#: 库不可用时用种子构造的库（结构同 _LIB_CACHE["tags"]）
_SEED_LIB: Optional[List[Dict[str, Any]]] = None


def _seed_lib() -> List[Dict[str, Any]]:
    global _SEED_LIB
    if _SEED_LIB is None:
        _SEED_LIB = [{
            "id": None,
            "code": t["code"], "name": t["name"], "polarity": t["polarity"],
            "color": t.get("color"), "priority": t.get("priority", 100),
            "sort_order": t.get("sort_order", 0),
            "is_fallback": bool(t.get("is_fallback")),
            "rules": [{"match_type": m, "pattern": p, "weight": float(w)}
                      for m, p, w in t.get("rules", [])],
        } for t in SEED_TAGS]
    return _SEED_LIB


async def ensure_seeded(session=None) -> int:
    """库表为空时写入内置种子标签（9 个），并置版本号。返回标签数。

    幂等：已存在任何标签就直接返回。启动 / 首次判定时调用。
    """
    try:
        from database.models_v2 import StanceTag, StanceTagRule, StanceTagVersion
        from sqlalchemy import select as _sel, func as _func

        own = session is None
        if own:
            from tools.v2_sync import _v2_session
            ctx = _v2_session()
            session = await ctx.__aenter__()
        else:
            ctx = None
        try:
            if session is None:
                return 0
            cnt = (await session.execute(
                _sel(_func.count()).select_from(StanceTag))).scalar() or 0
            if cnt:
                # 补齐后续版本新增的内置标签（已存在的 code 不动，避免覆盖用户改动）
                have = {c for c in (await session.execute(
                    _sel(StanceTag.code))).scalars().all()}
                added = 0
                for t in SEED_TAGS:
                    if t["code"] in have:
                        continue
                    tag = StanceTag(
                        code=t["code"], name=t["name"], polarity=t["polarity"],
                        color=t.get("color"), description=t.get("description"),
                        examples=list(t.get("examples") or []), enabled=True,
                        sort_order=t.get("sort_order", 0),
                        priority=t.get("priority", 100),
                        is_fallback=bool(t.get("is_fallback")),
                    )
                    session.add(tag)
                    await session.flush()
                    for mt, pat, w in (t.get("rules") or []):
                        session.add(StanceTagRule(
                            tag_id=tag.id, match_type=mt, pattern=pat,
                            weight=w, enabled=True))
                    added += 1
                if added:
                    v = (await session.execute(
                        _sel(StanceTagVersion).order_by(
                            StanceTagVersion.id).limit(1))).scalar_one_or_none()
                    if v is None:
                        session.add(StanceTagVersion(version=1))
                    else:
                        v.version = int(v.version or 0) + 1
                    await session.commit()

                # 有标签但没版本号（老库）→ 补一行
                v = (await session.execute(
                    _sel(StanceTagVersion.id).limit(1))).scalar()
                if v is None:
                    session.add(StanceTagVersion(version=1))
                    await session.commit()
                return int(cnt)
            for t in SEED_TAGS:
                tag = StanceTag(
                    code=t["code"], name=t["name"], polarity=t["polarity"],
                    color=t.get("color"), description=t.get("description"),
                    examples=list(t.get("examples") or []),
                    enabled=True, sort_order=t.get("sort_order", 0),
                    priority=t.get("priority", 100),
                    is_fallback=bool(t.get("is_fallback")),
                )
                session.add(tag)
                await session.flush()
                for mt, pat, w in (t.get("rules") or []):
                    session.add(StanceTagRule(
                        tag_id=tag.id, match_type=mt, pattern=pat,
                        weight=w, enabled=True))
            session.add(StanceTagVersion(version=1))
            await session.commit()
            try:
                await load_library(session=session, force=True)
            except Exception:
                pass
            return len(SEED_TAGS)
        finally:
            if own and ctx is not None:
                await ctx.__aexit__(None, None, None)
    except Exception as e:
        try:
            from tools import utils
            utils.logger.warning(f"[stance] 种子标签写入失败（回退内置）: {e}")
        except Exception:
            print(f"[stance] 种子标签写入失败（回退内置）: {e}")
        return 0


async def load_library(session=None, force: bool = False) -> Dict[str, Any]:
    """从库表加载标签库到内存（版本没变就不查库）。"""
    global _LIB_CACHE
    try:
        from database.models_v2 import StanceTag, StanceTagRule, StanceTagVersion
        from sqlalchemy import select as _sel

        own = session is None
        if own:
            from tools.v2_sync import _v2_session
            ctx = _v2_session()
            session = await ctx.__aenter__()
        else:
            ctx = None
        try:
            if session is None:
                return _LIB_CACHE
            ver = (await session.execute(
                _sel(StanceTagVersion.version).order_by(StanceTagVersion.id).limit(1)
            )).scalar()
            ver = int(ver or 1)
            if (not force) and _LIB_CACHE.get("version") == ver and _LIB_CACHE.get("tags"):
                return _LIB_CACHE
            tags = (await session.execute(
                _sel(StanceTag).order_by(StanceTag.priority, StanceTag.sort_order)
            )).scalars().all()
            rules = (await session.execute(_sel(StanceTagRule))).scalars().all()
            by_tag: Dict[int, List[Dict[str, Any]]] = {}
            for r in rules:
                if not r.enabled:
                    continue
                by_tag.setdefault(r.tag_id, []).append({
                    "id": r.id, "match_type": r.match_type, "pattern": r.pattern,
                    "weight": float(r.weight or 1.0), "negatable": bool(r.negatable),
                })
            lib = [{
                "id": t.id, "code": t.code, "name": t.name,
                "name_i18n": t.name_i18n or {}, "polarity": t.polarity,
                "color": t.color, "priority": t.priority, "sort_order": t.sort_order,
                "is_fallback": bool(t.is_fallback), "description": t.description,
                "examples": t.examples or [],
                "rules": by_tag.get(t.id, []),
            } for t in tags if t.enabled]
            _LIB_CACHE = {"version": ver, "tags": lib, "loaded_at": time.time()}
            return _LIB_CACHE
        finally:
            if own and ctx is not None:
                await ctx.__aexit__(None, None, None)
    except Exception as e:
        # 延迟导入：独立脚本直接跑本文件时 tools 包可能不在 sys.path
        try:
            from tools import utils
            utils.logger.warning(f"[stance] 加载标签库失败，回退内置种子: {e}")
        except Exception:
            print(f"[stance] 加载标签库失败，回退内置种子: {e}")
        return _LIB_CACHE


def library_version() -> int:
    return int(_LIB_CACHE.get("version") or 0)


def active_tags() -> List[Dict[str, Any]]:
    """当前生效的标签（供筛选 UI / 徽标渲染）。没加载过就给种子。"""
    tags = _LIB_CACHE.get("tags")
    if tags:
        return tags
    return _seed_lib()


def _rule_hit(rule: Dict[str, Any], text: str, sentiment: str,
              p_stance: str, rel: float, like_ratio: float) -> bool:
    mt = (rule.get("match_type") or "keyword").lower()
    pat = rule.get("pattern") or ""
    if mt == "keyword":
        return bool(pat) and pat in text
    if mt == "regex":
        try:
            return re.search(pat, text) is not None
        except re.error:
            return False
    if mt == "laughter":
        residue = re.sub(r"[\s，。！？、,.!?~～…\-—_]", "", _LAUGH_RE.sub("", text))
        return len(residue) < 2
    if mt == "sentiment":
        # pattern: "<sentiment>|<post_stance 列表或 *>"
        try:
            want, stances = pat.split("|", 1)
        except ValueError:
            want, stances = pat, "*"
        if want not in ("positive", "neutral", "negative"):
            return False
        if sentiment != want:
            return False
        if stances.strip() == "*":
            return True
        return p_stance in [x.strip() for x in stances.split(",") if x.strip()]
    if mt == "popular":
        try:
            return like_ratio >= float(pat or 0.3)
        except (TypeError, ValueError):
            return False
    return False


def judge_with_library(text: str, post_stance: Optional[Dict[str, Any]] = None,
                       comment_sentiment: Optional[str] = None,
                       like_ratio: float = 0.0,
                       tags: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """按标签库判定：命中返回标签，未命中返回兜底标签。"""
    lib = tags or active_tags()
    t = _clean(text)
    ps = post_stance or {}
    p_stance = ps.get("stance", "neutral")
    keywords = ps.get("keywords") or []
    topic_text = ps.get("topic_text") or ""
    sentiment = comment_sentiment or ""
    if not sentiment:
        if not t:
            sentiment = "neutral"
        else:
            try:
                from tools.sentiment import analyze_sentiment
                sentiment = analyze_sentiment(t)[0]
            except Exception:
                sentiment = "neutral"

    rel = _relatedness(t, keywords, topic_text) if t else 0.0

    ordered = sorted(lib, key=lambda x: (int(x.get("priority") or 100),
                                         int(x.get("sort_order") or 0)))
    for tag in ordered:
        for rule in tag.get("rules") or []:
            if _rule_hit(rule, t, sentiment, p_stance, rel, like_ratio):
                return {
                    "stance": tag.get("polarity") or "neutral",
                    "tag_code": tag.get("code"),
                    "tag_id": tag.get("id"),
                    "tag_name": tag.get("name"),
                    "tag_color": tag.get("color"),
                    "confidence": round(min(0.95, 0.5 + float(rule.get("weight") or 1.0) * 0.4), 2),
                    "reason": f"{tag.get('code')}:{rule.get('match_type')}",
                }

    # 兜底：库里标了 is_fallback 的标签，没有就用 neutral
    fb = next((x for x in ordered if x.get("is_fallback")), None)
    if fb is None:
        fb = next((x for x in ordered if x.get("code") == "neutral"), None)
    if fb is None:
        fb = {"code": "neutral", "name": "中立", "polarity": "neutral",
              "color": "#94a3b8", "id": None}
    return {
        "stance": fb.get("polarity") or "neutral",
        "tag_code": fb.get("code") or "neutral",
        "tag_id": fb.get("id"),
        "tag_name": fb.get("name") or "中立",
        "tag_color": fb.get("color"),
        "confidence": 0.3,
        "reason": "fallback",
    }

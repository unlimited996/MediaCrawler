# -*- coding: utf-8 -*-
"""中文评论情感判定。

社交平台评论没有显式「赞/踩」，需要根据内容与语气自动判定
好评（positive）/ 中立（neutral）/ 差评（negative），用于评论区
筛选与整体好评度统计（参考 Steam 商店的「特别好评」等档位）。

实现：轻量词典 + 原文本子串扫描，不引入新依赖。
- 情感词分强弱两档；情感词前 1-2 字出现否定词则翻转极性（中文否定前置）
- 情感词前有程度词则权重 ×1.5；转折词（但是/不过…）之后的首个情感词 ×0.5
- emoji 作为辅助信号；|score| < 1 判中立——社交语境里大量玩梗/陈述句，
  宁可中立不可误判
"""

from typing import Dict, List, Tuple

# 负面词：强（-2）明确的批评/攻击/避雷，弱（-1）抱怨/不满/调侃
_NEG_STRONG = {
    "垃圾", "恶心", "呕吐", "去死", "滚蛋", "傻逼", "傻缺", "脑残", "智障",
    "恶臭", "晦气", "避雷", "拉黑", "举报", "骗子", "诈骗", "造假",
    "抄袭", "剽窃", "盗图", "侵权", "害人", "害人不浅",
    "翻车", "塌房", "没救了", "没治", "无可救药", "毁三观", "三观不正",
    "毒鸡汤", "不敢苟同", "不苟同", "差评", "避坑", "坑钱", "抢钱",
    "太恶心", "真恶心", "恶心至极", "极其恶心", "智商税", "割韭菜",
}
_NEG_WEAK = {
    "无语", "无力", "无奈", "烦躁", "烦死", "烦人", "讨厌", "嫌弃", "反感",
    "可怕", "恐怖", "吓人", "阴间", "下头", "油腻", "双标", "道德绑架",
    "PUA", "pua", "画饼", "假的", "虚假", "冒充", "照骗", "摆拍", "剧本",
    "演得", "演戏", "作秀", "炒作", "营销", "浪费", "白费", "鸡肋",
    "降智", "反智", "破防", "红温", "急眼", "跳脚", "急了", "恼羞成怒",
    "后悔", "晚节不保", "可惜", "遗憾", "惋惜", "同情", "心疼", "可怜",
    "惨", "惨兮兮", "惨案", "悲剧", "凄惨", "漏洞", "毛病", "错误",
    "误导", "误人子弟", "坑人", "贵", "死贵", "服了", "醉了", "吐了",
    "批评", "吐槽", "diss", "DISS", "杠精", "反对", "抗议", "投诉",
    "维权", "退款", "退货", "差劲", "太差", "很差", "真差", "差得",
    "烂", "烂片", "烂尾", "糟糕", "尬", "尴尬", "劣", "劣质", "敷衍",
    "划水", "摆烂", "躺平", "抽象", "逆天", "离大谱", "离谱",
}

# 正面词：强（+2）明确赞扬/感谢/收获，弱（+1）喜欢/有趣/轻度认可
_POS_STRONG = {
    "感谢", "谢谢", "多谢", "感恩", "辛苦", "干货", "学到了", "受教",
    "长知识", "长见识", "开眼", "专业", "靠谱", "大神", "大佬", "牛逼",
    "牛哇", "厉害", "优秀", "优质", "精品", "神作", "巅峰", "天花板",
    "封神", "yyds", "绝了", "绝绝子", "绝佳", "完美", "满分", "高分",
    "推荐", "安利", "种草", "回购", "超值", "值得", "值了", "划算",
    "实惠", "真香", "好看", "漂亮", "惊艳", "颜值", "可爱", "萌",
    "治愈", "暖心", "温暖", "温柔", "笑死", "笑不活", "爆笑",
    "舒服", "舒适", "安逸", "享受", "解压", "放松", "支持", "赞同",
    "认同", "认可", "点赞", "好评", "爱了", "喜欢", "心动", "向往",
    "成功", "顺利", "搞定", "拿下", "收获", "收获满满", "受益",
    "很有帮助", "有用", "学到很多", "收藏", "码住", "马克",
}
_POS_WEAK = {
    "不错", "可以", "还行", "挺好", "还好", "ojbk", "有趣", "有意思",
    "好玩", "逗", "乐子", "节目效果", "期待", "盼望", "希望", "低调",
    "朴实", "真实", "真诚", "实在", "实用", "干净", "整洁", "香", "甜",
}

# 否定词：出现在情感词前 1-2 字时翻转其极性
_NEGATIONS = {"不", "没", "别", "甭", "勿", "莫", "未", "非", "无", "毫无",
              "并不", "算不上", "谈不上", "不见得"}

# 程度词：出现在情感词前 1-2 字时权重 ×1.5
_INTENSIFIERS = {"很", "太", "超", "贼", "特", "特别", "尤其", "真", "真的",
                 "非常", "十分", "格外", "相当", "巨", "极度", "无比",
                 "如此", "这么", "那么", "好", "挺"}

# 转折词：其后 6 字内的首个情感词权重 ×0.5（「有点无语，但是也能理解」）
_CONJUNCTIONS = {"但是", "可是", "不过", "然而", "却", "只是", "虽说"}

# 含情感单字但整体不是情感表达的常见词，扫描前用占位符屏蔽，防止误判
_EXCLUDE_WORDS = {
    "差不多", "差异", "差别", "差距", "差错", "阴差阳错", "价差", "温差",
    "不错过", "来不及", "迫不及待", "无所谓", "无比", "无限", "无与伦比",
    "不可思议", "不在话下", "毫不犹豫",
}

# emoji 信号
_NEG_EMOJI = {"😡", "🤬", "💢", "👎", "😤", "😠", "🤮", "😰", "😨", "💀",
              "😅", "😓", "😩", "😫", "😒", "🙄", "😪", "💔", "⚰", "🤢"}
_POS_EMOJI = {"😍", "❤", "👍", "🤣", "😂", "🥰", "😘", "🤗", "💕", "💖",
              "✨", "🎉", "🔥", "💯", "🏆", "😄", "😁", "🙏", "💪", "🌟"}

# 纯语气词评论的兜底（「哈哈哈哈」「xswl」）
_LAUGH_MARKERS = ("哈", "xswl", "hhh", "233")

# 情感词表：词 -> 极性
_WORDS: Dict[str, float] = {}
for _w in _NEG_STRONG:
    _WORDS[_w] = -2.0
for _w in _NEG_WEAK:
    _WORDS.setdefault(_w, -1.0)
for _w in _POS_STRONG:
    _WORDS[_w] = 2.0
for _w in _POS_WEAK:
    _WORDS.setdefault(_w, 1.0)
# 长的排前面，保证「不敢苟同」先于「不」类短否定词命中
_SORTED_WORDS: List[str] = sorted(_WORDS, key=len, reverse=True)


def _mask_excludes(text: str) -> str:
    """把排除词替换成等长占位符（\u0000），避免其内部的情感字被误扫。"""
    masked = text
    for ex in _EXCLUDE_WORDS:
        if ex in masked:
            masked = masked.replace(ex, "\u0000" * len(ex))
    return masked


def _find_hits(masked: str) -> List[Tuple[int, int, float, str]]:
    """扫描所有情感词命中，返回 [(start, end, polarity, word)]，按位置排序。"""
    hits: List[Tuple[int, int, float, str]] = []
    for word in _SORTED_WORDS:
        pol = _WORDS[word]
        start = 0
        while True:
            idx = masked.find(word, start)
            if idx < 0:
                break
            hits.append((idx, idx + len(word), pol, word))
            start = idx + 1
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    # 去掉被更长命中完全覆盖的短词（如「不敢苟同」命中后不再算「同」）
    kept: List[Tuple[int, int, float, str]] = []
    for h in hits:
        if any(h[0] >= k[0] and h[1] <= k[1] and h != k for k in kept):
            continue
        kept.append(h)
    return kept


def _prefix_has(masked: str, pos: int, vocab: set, max_len: int = 3) -> bool:
    """情感词前 max_len 个字符里是否出现词表中的词（否定/程度词前置修饰）。"""
    lo = max(0, pos - max_len)
    window = masked[lo:pos]
    for w in vocab:
        if w in window:
            return True
    return False


def _score_text(text: str) -> float:
    """词典 + 规则打分。"""
    masked = _mask_excludes(text)
    hits = _find_hits(masked)
    if not hits:
        return 0.0

    # 转折词位置：其后首个情感词降权
    conj_positions = [masked.find(c) for c in _CONJUNCTIONS if masked.find(c) >= 0]

    total = 0.0
    for start, end, pol, _word in hits:
        weight = pol
        # 否定翻转（情感词前 1-2 字）
        if _prefix_has(masked, start, _NEGATIONS, max_len=2):
            weight = -weight
        # 程度放大（情感词前 1-2 字；程度词本身可能带否定，如「不太好」）
        if _prefix_has(masked, start, _INTENSIFIERS, max_len=2):
            weight *= 1.5
        # 转折降权：情感词前 6 字内有转折词
        if any(0 <= start - cp <= 6 for cp in conj_positions):
            weight *= 0.5
        total += weight
    return total


def analyze_sentiment(text: str) -> Tuple[str, float]:
    """判定单条评论情感。

    返回 (sentiment, score)：sentiment ∈ positive / neutral / negative。
    任何异常都退化为 neutral，绝不影响主流程。
    """
    if not text or not text.strip():
        return "neutral", 0.0
    try:
        raw = str(text)
        score = _score_text(raw)

        # emoji 辅助信号
        for ch in raw:
            if ch in _NEG_EMOJI:
                score -= 1.5
            elif ch in _POS_EMOJI:
                score += 1.5

        # 密集感叹号视为情绪强化（幅度很小）
        if raw.count("！") + raw.count("!") >= 3:
            score += 0.5 if score >= 0 else -0.5

        # 纯语气评论（「哈哈哈哈」「xswl」）按轻度正面
        stripped = raw.replace(" ", "")
        if abs(score) < 0.01 and any(k in stripped for k in _LAUGH_MARKERS) and len(stripped) <= 12:
            score = 1.0

        # 死区：|score| < 1 一律中立，避免轻量信号误判
        if score >= 1:
            return "positive", round(score, 2)
        if score <= -1:
            return "negative", round(score, 2)
        return "neutral", round(score, 2)
    except Exception:
        return "neutral", 0.0


# Steam 中文区好评度档位（好评率 = positive / total）
RATING_TIERS = (
    (0.95, "rating.overwhelminglyPositive"),   # 好评如潮
    (0.80, "rating.veryPositive"),             # 特别好评
    (0.70, "rating.mostlyPositive"),           # 多半好评
    (0.40, "rating.mixed"),                    # 褒贬不一
    (0.20, "rating.mostlyNegative"),           # 多半差评
    (0.05, "rating.veryNegative"),             # 特别差评
    (0.00, "rating.overwhelminglyNegative"),   # 差评如潮
)


def rating_label(positive: int, total: int) -> str:
    """好评率 → Steam 风格档位文案 key。total=0 返回空串。"""
    if total <= 0:
        return ""
    ratio = positive / total
    for threshold, label in RATING_TIERS:
        if ratio >= threshold:
            return label
    return "rating.overwhelminglyNegative"


if __name__ == "__main__":
    cases = [
        ("感谢分享！干货满满，学到了", "positive"),
        ("笑死我了哈哈哈哈", "positive"),
        ("这不就是骗人的吗，避雷了", "negative"),
        ("垃圾东西，恶心", "negative"),
        ("有点无语，但是也能理解吧", "negative"),
        ("路过围观", "neutral"),
        ("演技太差了，看得尴尬", "negative"),
        ("真的很好吃，已经无限回购了", "positive"),
        ("不敢苟同", "negative"),
        ("不好看", "negative"),
        ("不支持作者的观点", "negative"),
        ("C# 和 F# 的讨论", "neutral"),
        ("和室友的差异不大，差不多价位", "neutral"),
        ("毫不犹豫选择收藏，太有用了", "positive"),
        ("价格死贵，智商税", "negative"),
        ("作者不容易，辛苦了", "positive"),
        ("", "neutral"),
    ]
    ok = 0
    for text, expect in cases:
        got, score = analyze_sentiment(text)
        if got == expect:
            ok += 1
        print(f"{'OK ' if got == expect else 'MISS'} [{got:8s} {score:+.1f}] {text[:32]}")
    print(f"{ok}/{len(cases)} passed")

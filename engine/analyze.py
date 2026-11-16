"""节拍切分与高光打分。

短剧的"高光"不是画面好看，而是**台词在冲突**：打脸、揭身份、下跪、退婚、签字。
有字幕就能读到这些信号，所以打分以台词文本为主、节奏为辅。

节拍（beat）= 一句或连续几句台词构成的完整表达单元，是顺剪的最小单位。
"""
from __future__ import annotations

import math
import random
import re

from . import project as proj_mod

# ── 词库：短剧高频冲突信号 ───────────────────────────────────────────
LEX = {
    "conflict": (3.2, [
        "打我", "打你", "一巴掌", "巴掌", "跪下", "下跪", "滚", "滚出去", "滚开", "闭嘴",
        "废物", "垃圾", "贱", "狗", "畜生", "找死", "你算什么东西", "算哪根葱", "凭什么",
        "离婚", "退婚", "休想", "做梦", "恶心", "恶心我", "不要脸", "背叛", "出轨",
        "小三", "贱人", "滚蛋", "跪下道歉", "道歉", "磕头",
    ]),
    "reveal": (3.6, [
        "集团", "董事", "董事长", "总裁", "继承人", "千金", "少爷", "夫人", "大小姐",
        "原来", "竟然", "居然", "其实", "身份", "亲子鉴定", "遗嘱", "股权", "收购",
        "破产", "家产", "真实身份", "我的女儿", "我的儿子", "亲生", "养女", "替我",
        # 「当年被赶出家门的人回来了」这类身世揭晓，以前一个词都命不中
        "赶出家门", "逐出家门", "原来是你", "竟然是", "真面目", "真身",
    ]),
    "reversal": (3.0, [
        "没想到", "想不到", "三年后", "五年后", "十年后", "回国", "归来", "重生", "穿越",
        "后会有期", "你等着", "等着瞧", "迟早", "总有一天", "现在轮到我", "风水轮流转",
        "我回来了", "我回来了", "报应", "后悔", "晚了",
    ]),
    "emotion": (2.0, [
        "哭", "求你", "别走", "恨", "爱", "心痛", "颤抖", "疯了", "崩溃", "绝望",
        "救命", "对不起", "为什么", "不要", "放过我", "我错了", "原谅",
    ]),
    "power": (1.8, [
        "亿", "千万", "百万", "合同", "签字", "股票", "家族", "嫁", "娶", "订婚",
        "婚礼", "彩礼", "豪门", "总裁夫人", "公司", "股份",
    ]),
}

# 英文短剧（狼人 / 先婚后爱 / 复仇 / 豪门）高频冲突信号，按词边界匹配
EN_LEX = {
    "conflict": (3.2, [
        "shut up", "get out", "get lost", "slap", "kneel", "kneel down", "divorce",
        "bastard", "bitch", "trash", "worthless", "how dare", "who do you think",
        "disgusting", "cheat", "cheated", "betray", "affair", "mistress", "reject",
        "rejected", "banish", "banished", "rogue", "hate you", "kill you", "pathetic",
        "loser", "shame on you", "apologize", "on your knees", "not good enough",
    ]),
    "reveal": (3.6, [
        "alpha", "luna", "beta", "omega", "heir", "inheritance", "billion", "million",
        "ceo", "chairman", "identity", "illegitimate", "real daughter", "real son",
        "pregnant", "fated mate", "mate bond", "reject the bond", "your majesty",
        "the truth", "all along", "actually", "in fact", "turns out", "secretly",
        "my pack", "the pack", "bloodline", "firstborn", "contract marriage",
    ]),
    "reversal": (3.0, [
        "three years", "five years", "ten years", "came back", "i'm back", "revenge",
        "regret", "you'll see", "sooner or later", "my turn", "karma", "reborn",
        "second chance", "from now on", "watch me", "i warned you", "not anymore",
        "used to", "finally", "at last",
    ]),
    "emotion": (2.0, [
        "cry", "crying", "please", "don't go", "don't leave", "hate", "love you",
        "sorry", "forgive", "help me", "save me", "i'm scared", "afraid", "alone",
        "heartbroken", "miss you", "stay with me",
    ]),
    "power": (1.8, [
        "wedding", "marriage", "married", "engagement", "bride", "groom", "contract",
        "sign", "shares", "fortune", "inheritance", "family name", "mafia", "don",
        "king", "queen", "crown", "throne", "empire",
    ]),
}

EN_FILLER = ["okay", "ok", "yes", "no", "yeah", "thanks", "thank you", "hello", "hi",
             "sure", "fine", "alright", "right", "well", "oh", "wow", "hmm", "uh"]

# ── 亲密 / 接触：吻戏、拥抱、床戏 ────────────────────────────────────
# 这类戏不靠"冲突"抓人，靠情绪张力。词表刻意收窄：
# 只认"吻/亲我/抱住/床上"这些明确指向亲密关系的组合，
# 不用「亲」「抱」「睡」单字——否则"亲妈""抱歉""睡觉"全会误伤。
INTIMACY_STRONG = {
    "吻", "亲吻", "吻我", "吻了", "吻上", "亲我", "亲上", "亲了", "亲下去",
    "抱住", "抱我", "搂住", "搂着", "怀里", "床上", "一起睡", "陪我睡", "上床",
    "脱衣服", "脱掉衣服", "衣衫不整", "肌肤", "身体",
    "kiss", "kissed", "kissing", "in bed", "sleep with", "make love",
    "undress", "naked", "take off your", "touch me", "bedroom",
}
INTIMACY_WEAK = {
    "拥抱", "抱着", "胸口", "肩膀", "锁骨", "呼吸", "心跳", "脸红", "害羞",
    "暧昧", "缠绵", "贴近", "耳语", "低喘",
    "hug", "hugged", "hold me", "closer", "lips", "neck", "skin", "moan",
    "body", "shirt",
}
# 巴掌：动作冲击 + 这几个词基本可以确定是打脸戏，单独标出来方便挑
SLAP_WORDS = ("巴掌", "耳光", "扇了", "扇他", "扇她", "扇你", "扇在",
              "slap", "slapped", "smack")
KISS_WORDS = ("吻", "亲我", "亲吻", "kiss")

_INTIMACY_EN = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(w) for w in
                              sorted(INTIMACY_STRONG | INTIMACY_WEAK, key=len, reverse=True)
                              if w.isascii()) + r")(?![a-z])", re.I)


_INT_STRONG_CN = tuple(sorted((w for w in INTIMACY_STRONG if not w.isascii()),
                              key=len, reverse=True))
_INT_WEAK_CN = tuple(sorted((w for w in INTIMACY_WEAK if not w.isascii()),
                            key=len, reverse=True))
# 快速预筛：任何亲密词出现都必然带着它的首字。绝大多数台词一个都没有，
# 先用一次 C 正则扫首字，命中不了就直接返回，省掉 60 次 find。
_INT_CHAR = re.compile("[" + re.escape("".join(sorted({w[0] for w in
                                                       _INT_STRONG_CN + _INT_WEAK_CN}))) + "]")


def _intimacy_score(text: str) -> float:
    """台词里的亲密信号强度 0~1。一个强词 0.6，两个封顶；弱词各 0.18。

    按「长词先占位、同长度按出现位置」做区间去重：「吻」在「吻了」里面
    不重复计分。词表在模块加载时就按长度排好，不再每次调用重排。
    """
    if not text:
        return 0.0
    low = text.lower()
    en = len(set(m.group(0).lower() for m in _INTIMACY_EN.finditer(low)))
    if not en and not _INT_CHAR.search(low):
        return 0.0
    spans: list[tuple[int, int]] = []

    def mark(word: str) -> int:
        n = 0
        start = 0
        while True:
            i = low.find(word, start)
            if i < 0:
                break
            end = i + len(word)
            if not any(i < e and s < end for s, e in spans):
                spans.append((i, end))
                n += 1
            start = end
        return n

    strong = 0
    for w in _INT_STRONG_CN:
        strong += mark(w)
    weak = 0
    for w in _INT_WEAK_CN:
        weak += mark(w)
    strong += en
    return round(min(1.0, min(2, strong) * 0.6 + min(3, weak) * 0.18), 3)


def _subtype(text: str, intim: float) -> str:
    """给候选一个更好认的小标签：巴掌 / 亲吻 / 亲密。只做标注，不改分数。"""
    low = (text or "").lower()
    if any(w in low for w in SLAP_WORDS):
        return "slap"
    if any(w in low for w in KISS_WORDS):
        return "kiss"
    return "intimate" if intim >= 0.5 else ""

# 结尾悬念（英文）
EN_SUSPENSE: list[tuple[str, float]] = [
    ("who are you", 3.0), ("who is he", 3.0), ("who is she", 3.0), ("who did this", 2.8),
    ("what happened", 2.6), ("what is this", 2.4), ("what's going on", 2.4),
    ("why did you", 2.4), ("how could you", 2.4), ("impossible", 2.6),
    ("that's impossible", 2.8), ("the truth", 2.8), ("secret", 2.6),
    ("you don't know", 2.2), ("i need to tell you", 2.6), ("there's something", 2.2),
    ("to be continued", 3.4), ("next time", 2.6), ("next episode", 3.2),
    ("wait", 1.8), ("what the hell", 2.2), ("oh my god", 2.0), ("no way", 2.4),
    ("something's wrong", 2.6), ("we have a problem", 2.6), ("they're here", 2.6),
    ("watch out", 2.4), ("run", 2.0), ("it's him", 2.8), ("it's her", 2.8),
    ("i know your secret", 3.2), ("you're pregnant", 3.2), ("i'm pregnant", 3.2),
]

# 贴标签用的集合：中文 + 英文都要认，否则英剧的标签会全掉成"剧情节点"
LEX_CONFLICT = set(LEX["conflict"][1]) | set(EN_LEX["conflict"][1])
LEX_REVEAL = set(LEX["reveal"][1]) | set(EN_LEX["reveal"][1])

_EN_ORDER = [(cat, w, kw) for cat, (w, words) in EN_LEX.items() for kw in words]
_EN_SUSPENSE = list(EN_SUSPENSE)          # (kw, wt)，保持定义顺序


class _WordTrie:
    """多模式匹配：一次扫描找出所有命中的词（含包含关系）。

    英文词库里有 `contract marriage` 与 `marriage`、`banished` 与 `banish`
    这种互相包含的条目，原来的实现是「每个词单独 search」，两者都会命中。
    合并成一个交替正则只能返回最长的那条，会改变打分，所以这里用 trie
    逐位置下探，命中集合与原实现一致。中文仍走 `in`——C 实现比 Python
    逐字符循环更快，没必要换。
    """

    __slots__ = ("root",)
    END = None

    def __init__(self, words):
        self.root: dict = {}
        for w in words:
            node = self.root
            for ch in w:
                node = node.setdefault(ch, {})
            node[self.END] = w

    def scan(self, text: str, boundary: bool = False) -> set[str]:
        out: set[str] = set()
        root = self.root
        n = len(text)
        for i in range(n):
            if boundary and i and "a" <= text[i - 1] <= "z":
                continue
            node = root
            j = i
            while j < n:
                node = node.get(text[j])
                if node is None:
                    break
                j += 1
                w = node.get(self.END)
                if w is not None and (not boundary or j >= n or not ("a" <= text[j] <= "z")):
                    out.add(w)
        return out


_EN_TRIE = _WordTrie([kw for _, _, kw in _EN_ORDER])
_EN_SUS_TRIE = _WordTrie([kw for kw, _ in EN_SUSPENSE])

# 水词/寒暄：信息量低，优先删
FILLER = ["嗯", "哦", "啊", "好的", "是", "我知道了", "你先出去", "你先下去",
          "没事", "没什么", "走吧", "来了", "请", "谢谢", "再见", "好", "对"]

# 结尾悬念词库：出现在剧情后段时，这句话就是"留扣"——让观众不看完不甘心
SUSPENSE_WORDS: list[tuple[str, float]] = [
    # 追问 / 未解
    ("到底", 2.4), ("究竟", 2.4), ("难道", 2.2), ("怎么会", 2.2), ("怎么回事", 2.4),
    ("不可能", 2.6), ("不会吧", 2.4), ("真相", 2.8), ("秘密", 2.6), ("隐瞒", 2.2),
    ("是谁", 2.8), ("你是谁", 3.0), ("他是谁", 3.0), ("那是谁", 2.8), ("伪装", 2.2),
    ("藏着", 2.0), ("瞒着", 2.2), ("身份", 1.6),
    # 反转 / 预告
    ("竟然是", 2.8), ("居然是", 2.8), ("原来是他", 3.0), ("原来是她", 3.0),
    ("下一集", 3.2), ("下集", 2.6), ("预告", 2.4), ("欲知后事", 3.2),
    ("未完待续", 3.2), ("下回分解", 3.0), ("且看下集", 3.0), ("突然", 1.6),
    ("大结局", 2.0),
    # 危机 / 悬
    ("危险", 2.4), ("快跑", 2.6), ("不好", 1.8), ("小心", 1.6), ("来人", 2.0),
    ("出事了", 2.8), ("不对劲", 2.4), ("有人来了", 2.4), ("中计", 2.6), ("上当了", 2.6),
    ("你等着", 2.0), ("没完", 1.8), ("这一切", 1.8),
]

_PUNCT = re.compile(r"[？?！!]")
_SENT = re.compile(r"[。！？!?…”」』]$")


def _hits(text: str) -> tuple[float, list[str]]:
    score = 0.0
    hits: list[str] = []
    for cat, (w, words) in LEX.items():
        for kw in words:
            if kw in text:
                score += w
                if kw not in hits:
                    hits.append(kw)
    lower = text.lower()
    found = _EN_TRIE.scan(lower, boundary=True)
    # 按词库定义顺序累加：`inheritance` 在 reveal / power 里各有一条，
    # 原实现会加两次，这里同样按两条各加一次，不能改成 dict 去重。
    for cat, w, kw in _EN_ORDER:
        if kw in found:
            score += w
            if kw not in hits:
                hits.append(kw)
    return score, hits


def suspense_of(text: str) -> tuple[float, list[str]]:
    """一句话/一段话的「留扣」强度：悬念词 + 没回答的问句。

    开头钩子要"炸"，结尾钩子要"悬"——这个分数只用来判断"停在这里，
    观众会不会想看下一集"，和冲突分（LEX）是两套体系。
    """
    if not text:
        return 0.0, []
    low = text.lower()
    hits = [(w, wt) for w, wt in SUSPENSE_WORDS if w in text]
    found = _EN_SUS_TRIE.scan(low, boundary=True)
    hits += [(kw, wt) for kw, wt in _EN_SUSPENSE if kw in found]
    q = text.count("？") + text.count("?")
    return round(sum(wt for _, wt in hits) + q * 1.1, 2), sorted({w for w, _ in hits})


def attach_acts(beats: list[dict], acts: list[dict] | None,
                pad0: float = 0.8, pad1: float = 1.0) -> None:
    """把画面/声音冲击事件挂到节拍上。

    短剧里最抓人的往往是"动手"的那一下——扇巴掌、泼酒、摔杯子、推下楼梯，
    这些镜头常常一句话都没有，光看台词永远选不出来。vision 扫出来的冲击事件
    落在哪个节拍上，就给哪个节拍加"动作分"。
    """
    if not acts:
        for b in beats:
            b.setdefault("act", 0.0)
            b.setdefault("aud", 0.0)
            b.setdefault("vis", 0.0)
        return
    # 先按集分桶：以前每个 beat 都要遍历全剧 300+ 个动作点，40 集就是 20 万次比较
    by_ep: dict[int, list[dict]] = {}
    for a in acts:
        by_ep.setdefault(a["ep"], []).append(a)
    for b in beats:
        best = 0.0
        best_a = 0.0
        best_v = 0.0
        n = 0
        kinds: list[str] = []
        for a in by_ep.get(b["ep"], ()):
            if a["g1"] < b["g0"] - pad0 or a["g0"] > b["g1"] + pad1:
                continue
            n += 1
            if a["score"] > best:
                best = a["score"]
            av = float(a.get("a", 0.0))
            vv = float(a.get("v", 0.0))
            if av > best_a:
                best_a = av
            if vv > best_v:
                best_v = vv
            if vv >= 0.5:
                kinds.append("动作")
            elif av >= 0.8:
                kinds.append("爆发")
        b["act"] = round(best, 3)
        b["aud"] = round(best_a, 3)      # 声音冲击：用来区分"真在吵"和"配乐一直很响"
        b["vis"] = round(best_v, 3)      # 画面冲击：动作幅度
        b["actn"] = n
        b["act_kind"] = kinds[0] if kinds else ("爆发" if best else "")


def _density(text: str, dur: float) -> float:
    """语速密度：中文按字/秒，英文按词/秒（两种剧都要能用）。"""
    if dur <= 0.2:
        return 0.0
    # 逐字符判断比 re.findall 建一个临时列表轻，607 个节拍累计下来差一截
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk:
        cps = cjk / dur
        # 3-6 字/秒是正常语速，越快越紧绷
        return max(0.0, min(1.6, (cps - 1.8) / 3.2))
    words = len(re.findall(r"[A-Za-z']+", text))
    if words < 2:
        return 0.0
    wps = words / dur
    return max(0.0, min(1.6, (wps - 2.0) / 2.0))


def segment_beats(proj: dict, gap_merge: float = 0.42, lead: float = 0.16,
                  tail: float = 0.30, max_dur: float = 9.0) -> list[dict]:
    """把台词合并成节拍。

    默认一句一拍（二创顺剪本来就是一句一个切点），只有间隔极短（<0.42s）
    或总长超过 9 秒时才会合并/断开，避免单个节拍过大导致无法精细取舍。
    """
    beats: list[dict] = []
    for ep in proj["episodes"]:
        cues = ep["cues"]
        if not cues:
            continue
        group: list[dict] = []
        for c in cues:
            if not group:
                group = [c]
                continue
            gap = c["start"] - group[-1]["end"]
            span = c["end"] - group[0]["start"]
            if gap <= gap_merge and span <= max_dur:
                group.append(c)
            else:
                beats.append(_mk_beat(ep, group, lead, tail))
                group = [c]
        if group:
            beats.append(_mk_beat(ep, group, lead, tail))
    beats.sort(key=lambda b: b["g0"])
    for i, b in enumerate(beats):
        b["id"] = i
    return beats


def _mk_beat(ep: dict, group: list[dict], lead: float, tail: float) -> dict:
    start = max(0.0, group[0]["start"] - lead)
    end = min(ep["dur"], group[-1]["end"] + tail)
    text = " ".join(c["text"] for c in group)
    dur = max(0.3, end - start)
    kw, hits = _hits(text)
    low = text.strip().lower()
    fill = sum(1 for f in FILLER if text.strip().startswith(f) or text.strip() == f)
    if not fill:
        words = low.split()
        if words and words[0].strip(",.!?") in EN_FILLER and len(words) <= 3:
            fill = 1
    punct = min(1.2, len(_PUNCT.findall(text)) * 0.4)
    dens = _density(text, dur)
    # 长度甜点：3-14 秒最像"一场戏"，过长会拖，过短没信息
    sweet = 1.0 - min(1.0, abs(dur - 7.0) / 12.0)
    score = kw * 1.0 + dens * 2.2 + punct * 1.4 + sweet * 0.8 - fill * 1.6
    score += min(1.2, len(hits) * 0.25)
    return {
        "ep": ep["idx"],
        "g0": round(ep["offset"] + start, 3),
        "g1": round(ep["offset"] + end, 3),
        "start": round(start, 3),
        "end": round(end, 3),
        "dur": round(dur, 3),
        "text": text,
        "cues": len(group),
        "score": round(max(0.0, score), 3),
        "hits": hits[:6],
    }


def _hook_kind(win: list[dict], text: str, susp: float,
               act_n: float | None = None) -> str:
    """给候选贴一个"为什么它算高光"的标签，方便一眼挑选。

    act_n 是按本剧分布归一后的冲击强度（1.0 ≈ 本剧常态），
    直接用原始分数会因为不同剧的音量/运镜基准不同而整部剧都变成"动作冲突"。
    """
    if act_n is None:
        act_n = max((b.get("act", 0.0) for b in win), default=0.0) / 1.6
    has_conflict = any(h in LEX_CONFLICT for b in win for h in b.get("hits", []))
    has_reveal = any(b.get("hits") and set(b["hits"]) & set(LEX_REVEAL)
                     for b in win)
    if act_n >= 1.35:
        return "动作冲突" if has_conflict or any(b.get("act_kind") == "动作" for b in win) else "爆发点"
    if has_conflict:
        return "台词冲突"
    if has_reveal:
        return "身份反转"
    if susp >= 3.0:
        return "悬念留扣"
    return "剧情节点"


# ── 无台词时间窗：独立候选来源 ───────────────────────────────────────
def _silence_windows(proj: dict, beats: list[dict]) -> list[tuple]:
    """每集内「没有台词」的时间段：(g0, g1, 集号, 集起点, 集终点)。

    segment_beats 只切有台词的区间，find_highlights 又只在这些节拍上滑窗，
    所以沉默段里的高能画面连候选都不是——先算出补集给它们一个入口。
    """
    by_ep: dict[int, list[tuple[float, float]]] = {}
    for b in beats:
        by_ep.setdefault(b["ep"], []).append((b["g0"], b["g1"]))
    out: list[tuple] = []
    for ep in proj.get("episodes", []):
        idx = int(ep["idx"])
        lo = float(ep["offset"])
        hi = lo + float(ep["dur"])
        cur = lo
        for s, e in sorted(by_ep.get(idx, [])):
            s, e = min(hi, max(lo, s)), min(hi, max(lo, e))
            if s > cur + 0.05:
                out.append((cur, s, idx, lo, hi))
            cur = max(cur, e)
        if hi > cur + 0.05:
            out.append((cur, hi, idx, lo, hi))
    return out


def find_visual_highlights(proj: dict, beats: list[dict], acts: list[dict] | None,
                           min_dur: float = 3.0, max_dur: float = 20.0,
                           target_dur: float = 7.0, front_pad: float = 0.6,
                           merge_gap: float = 1.2, min_score: float = 1.0,
                           per_ep_limit: int = 3, max_total: int = 40) -> list[dict]:
    """从无台词时间段里，用画面/声音冲击事件生成独立钩子候选。

    只在「沉默区 + 有达标冲击事件」时出候选：没扫过动作点、扫描失败、
    或纯风景空镜都一条不加，自动退回原来的对白候选池，不影响既有行为。
    输出字段与 find_highlights 的候选一致，额外带 visual_only / _vscore，
    由 find_highlights 统一做量纲对齐和排序。

    注：完整的硬门槛（片尾卡、转场闪白、不适画面）属于后续步骤；这里只做
    轻量保护，避免片头黑场和「集末纯音频」的片尾曲被当成高能钩子。
    """
    if not acts:
        return []
    windows = _silence_windows(proj, beats)
    if not windows:
        return []
    by_ep: dict[int, list[dict]] = {}
    for a in acts:
        by_ep.setdefault(int(a["ep"]), []).append(a)

    out: list[dict] = []
    for lo, hi, idx, ep_lo, ep_hi in windows:
        evs = [a for a in by_ep.get(idx, []) if lo <= a["g0"] < hi]
        if not evs:
            continue
        evs.sort(key=lambda a: a["g0"])
        clusters: list[list[dict]] = []
        for a in evs:
            if clusters and a["g0"] - clusters[-1][-1]["g1"] <= merge_gap:
                clusters[-1].append(a)
            else:
                clusters.append([a])
        for cl in clusters:
            strength = max(float(a.get("score", 0.0)) for a in cl)
            if strength < min_score:
                continue
            v = max(float(a.get("v", 0.0)) for a in cl)
            # 高能点必须落在片头：窗口起点贴着事件起点往前留 0.6 秒，往后取满 7 秒。
            # 不能像台词候选那样把事件摆在窗口正中，否则成片要空等三四秒才等到
            # 那一下，开头钩子就废了（叠加默认「开头入戏」提前 1 秒后约 1.6 秒入点）。
            t0 = max(lo, cl[0]["g0"] - front_pad)
            t1 = min(hi, max(t0 + target_dur, cl[-1]["g1"] + 0.3))
            if t1 - t0 < min_dur:
                t0 = max(lo, t1 - min_dur)
            dur = t1 - t0
            if dur < min_dur or dur > max_dur:
                continue
            if t0 < ep_lo + 0.3:                     # 片头黑场 / logo 的第一帧
                continue
            if t1 > ep_hi - 0.3 and v < 0.3:         # 集末只有声音，多半是片尾曲
                continue
            out.append({
                "g0": round(t0, 3), "g1": round(t1, 3),
                "dur": round(dur, 3), "ep": idx,
                "text": "", "score": 0.0,
                "act": round(strength, 2), "susp": 0.0,
                "at_end": False,
                "kind": "动作冲突" if v >= 0.5 else "爆发点",
                "hits": [], "beats": [],
                "visual_only": True,
                "_vscore": round(strength, 3),
                "_v": round(v, 3),
                "_a": round(max(float(x.get("a", 0.0)) for x in cl), 3),
                "_burst": round(strength, 3),
            })
            if len(out) >= max_total:
                break
        if len(out) >= max_total:
            break

    out.sort(key=lambda c: (c["ep"], -c["_vscore"]))
    kept: list[dict] = []
    cnt: dict[int, int] = {}
    for c in out:
        if cnt.get(c["ep"], 0) >= per_ep_limit:
            continue
        cnt[c["ep"]] = cnt.get(c["ep"], 0) + 1
        kept.append(c)
    kept.sort(key=lambda c: c["g0"])
    return kept


def _ensure_visual(picked: list[dict], cands: list[dict], reserve: int) -> list[dict]:
    """保证候选池里至少有几条纯画面钩子，否则新来源会被全局排序整个吃掉。

    只替换分数最低的非视觉候选；完整的类型配额与近重复去重留给下一步。
    """
    if reserve <= 0:
        return picked
    chosen = {id(c) for c in picked}
    have = sum(1 for c in picked if c.get("visual_only"))
    pool = [c for c in cands if c.get("visual_only") and id(c) not in chosen]
    pool.sort(key=lambda c: -c.get("_eff", c["score"]))
    for c in pool:
        if have >= reserve:
            break
        victims = [p for p in picked if not p.get("visual_only")]
        if not victims:
            break
        victim = min(victims, key=lambda p: p.get("_eff", p["score"]))
        picked.remove(victim)
        picked.append(c)
        chosen.add(id(c))
        have += 1
    return picked


# ── 类型判定 / 证据 / 配额重排（可替换的规则层） ─────────────────────
TYPE_ORDER = ("energy", "action", "dialog", "reveal", "intimacy",
              "suspense", "tension", "performance")
TYPE_LABELS = {
    "energy": "高能画面", "action": "动作冲突", "dialog": "台词冲突",
    "reveal": "身份反转", "suspense": "悬念留扣", "intimacy": "亲密接触",
    "performance": "戏点张力", "tension": "情感张力",
}
# 旧前端只认五类；kind 原值不动，新类型通过 primary_label 对外展示
TYPE_KIND_ALIAS = {
    "energy": "爆发点", "action": "动作冲突", "dialog": "台词冲突",
    "reveal": "身份反转", "suspense": "悬念留扣", "intimacy": "剧情节点",
    "performance": "剧情节点", "tension": "剧情节点",
}
# 稀缺度加成：新类型天然少见，给一点入池优势，但不改变展示分
TYPE_BOOST = {"energy": 0.10, "performance": 0.08, "tension": 0.08,
              "action": 0.04, "suspense": 0.03, "reveal": 0.02,
              "intimacy": 0.06, "dialog": 0.0}

# 前 15 集是短剧留人的关键区。候选池在这里刻意加密：
# 排序小幅加权 + 每集多放一条 + 前段最多占七成，既不挤干净后段，
# 又能让前段每一两集就有一个能用的开头。
FRONT_EPS = 15
FRONT_BOOST = 1.20
FRONT_SHARE = 0.80       # 前段最多占八成：加密是"更密"，不是"只剩前段"
FRONT_MIN_SHARE = 0.45   # 前 15 集至少占候选池的 45%


def _clamp01(x: float) -> float:
    return round(min(1.0, max(0.0, float(x))), 3)


def _pct(values: list[float], q: float, default: float) -> float:
    """取分位值（忽略 0）。用来给"冲击强度"定本剧自己的基准线。"""
    vals = sorted(v for v in values if v and v > 0.0)
    if not vals:
        return default
    return float(vals[min(len(vals) - 1, int(len(vals) * q))])


def classify_candidate(c: dict, *, conflict_words: set, reveal_words: set,
                       ep_count: int) -> dict:
    """给一条候选补齐类型、证据、质量与排序字段（原地修改并返回）。

    **代理判定声明**：这里只用现有低层信号（台词命中、画面/声音冲击强度、
    时长、位置）区分「强运动+强声音」「静+长」「静+有声音」这类可观测形态，
    不是语义级的内容识别。判定表集中在 rules 里；以后信号层升级到景别、
    人物距离、表情等，只需替换 _signals 计算与 rules，下面的字段契约不变。
    """
    hits = set(c.get("hits") or [])
    text = c.get("text") or ""
    dur = max(0.3, float(c.get("dur", 0.0)))
    ep = int(c.get("ep", 1))
    v = float(c.pop("_v", 0.0))
    a = float(c.pop("_a", 0.0))
    # 没有归一值时按 1.6 的常态基准折算，别让不同剧的音量基准把强度顶满
    burst = float(c.pop("_burst", float(c.get("act", 0.0)) / 1.6))
    has_dialog = bool(text.strip() or c.get("beats"))

    dialog = _clamp01(len(hits & conflict_words) / 2.0 + min(1.0, len(text) / 40.0) * 0.25)
    reveal = _clamp01(len(hits & reveal_words) / 1.5)
    intim = _clamp01(float(c.get("intim", 0.0)))
    susp_s = _clamp01(float(c.get("susp", 0.0)) / 3.0)
    motion = _clamp01(v / 1.4 if v else burst / 2.5)
    audio = _clamp01(a / 1.4 if a else 0.0)
    intensity = _clamp01(burst / 2.5)
    stillness = _clamp01(1.0 - motion - 0.25 * audio + (0.2 if dur >= 6.0 else 0.0))
    position = _clamp01(1.0 - (ep - 1) / max(1, ep_count))
    signals = {
        "dialog": dialog, "reveal": reveal, "suspense": susp_s, "intimacy": intim,
        "motion": motion, "audio": audio, "intensity": intensity,
        "stillness": stillness, "position": position,
    }

    # 顺序判断代替 lambda 列表：1255 个候选 × 8 条 lambda 的创建与调用
    # 是实打实的开销，判断顺序与原 rules 表完全一致
    types: list[str] = []
    if motion >= 0.55 and intensity >= 0.5 and audio >= 0.25:
        types.append("energy")
    if motion >= 0.42 or intensity >= 0.62:
        types.append("action")
    if dialog >= 0.35:
        types.append("dialog")
    if reveal >= 0.34:
        types.append("reveal")
    # 亲密戏排在冲突/反转之后：一句"你凭什么亲我"该算台词冲突而不是亲密接触
    if intim >= 0.50:
        types.append("intimacy")
    if susp_s >= 0.34:
        types.append("suspense")
    if stillness >= 0.50 and audio >= 0.28 and dialog <= 0.30:
        types.append("tension")
    if stillness >= 0.62 and dialog <= 0.45:
        types.append("performance")
    if not types:
        types = ["dialog" if has_dialog else "performance"]
    primary = types[0]
    primary_signal = {
        "energy": (motion + intensity + audio) / 3.0,
        "action": max(motion, intensity),
        "dialog": dialog, "reveal": reveal, "suspense": susp_s,
        "intimacy": intim,
        "tension": (stillness + audio) / 2.0,
        "performance": stillness * (1.0 - dialog),
    }.get(primary, 0.3)
    confidence = _clamp01(0.25 + 0.65 * primary_signal
                          + (0.10 if len(types) > 1 else 0.0))

    evidence: list[dict] = []
    if hits:
        evidence.append({"kind": "dialog", "strength": dialog,
                         "note": "命中台词：" + " / ".join(sorted(hits)[:3])})
    if intim:
        evidence.append({"kind": "dialog", "strength": intim,
                         "note": "亲密 / 接触台词"})
    if motion or intensity:
        evidence.append({"kind": "visual", "strength": max(motion, intensity),
                         "note": "画面冲击 / 运动强度"})
    if audio:
        evidence.append({"kind": "audio", "strength": audio, "note": "声音强度"})
    if c.get("at_end"):
        evidence.append({"kind": "structure", "strength": 0.6, "note": "落在集末"})
    if not has_dialog:
        evidence.append({"kind": "silence", "strength": stillness, "note": "该段没有台词"})

    # 首帧/语义完整目前由候选生成阶段的「贴边排除」保证，未做逐帧检测；
    # 信号层补齐后这里可以升级为真实检测结果，字段名不变。
    quality = {
        "playable": 3.0 <= dur <= 20.0,
        "first_frame_ok": True,
        "complete": (not c.get("at_end", False)) or dur >= 4.5,
    }
    source = "fusion" if (has_dialog and (motion or audio)) else ("dialog" if has_dialog else "visual")

    rank = float(c.get("score", 0.0))
    rank *= (1.0 + TYPE_BOOST.get(primary, 0.0)
             + (0.05 if len(types) > 1 else 0.0) + 0.03 * position)
    if confidence < 0.45:
        rank *= 0.92

    c.update({
        "types": types,
        "primary_type": primary,
        "primary_label": TYPE_LABELS.get(primary, "剧情节点"),
        "subtype": _subtype(text, intim),
        "confidence": confidence,
        "signals": signals,
        "evidence": evidence,
        "source": source,
        "has_dialog": has_dialog,
        "rank_score": round(rank, 2),
        "rank_reason": (TYPE_LABELS.get(primary, "剧情节点") + " · "
                        + ("多源印证 · " if len(types) > 1 else "")
                        + ("稀缺加成 · " if TYPE_BOOST.get(primary, 0) >= 0.08 else "")
                        + f"第{ep}集"),
        "dedup_key": f"{ep}@{round(float(c['g0']) / 2.0)}",
        "quality": quality,
    })
    return c


def _overlap_near(a: dict, b: dict) -> bool:
    if a["ep"] != b["ep"]:
        return False
    ov = min(a["g1"], b["g1"]) - max(a["g0"], b["g0"])
    return ov > 0.6 * max(0.1, min(a["dur"], b["dur"]))


def dedup_near(picked: list[dict], cands: list[dict], goal: int) -> list[dict]:
    """同集内窗口重叠超过 60% 视为近重复，保留排序分高的，再从全池补足。"""
    kept: list[dict] = []
    for c in sorted(picked, key=lambda x: -x["rank_score"]):
        if not any(_overlap_near(c, k) for k in kept):
            kept.append(c)
    target = min(goal, len(picked)) if picked else 0
    if len(kept) < target:
        chosen = {id(c) for c in kept}
        for c in sorted(cands, key=lambda x: -x["rank_score"]):
            if len(kept) >= target:
                break
            if id(c) in chosen or any(_overlap_near(c, k) for k in kept):
                continue
            kept.append(c)
            chosen.add(id(c))
    return kept


def cap_visual(picked: list[dict], cands: list[dict], goal: int,
               reserve: int, ratio: float = 0.55) -> list[dict]:
    """给非对白候选设总量上限，保证对白型钩子不被整体挤掉（不退化约束）。

    只换出排序分最低的纯画面候选，补入不与现有片段近重复的对白候选。
    """
    cap = max(reserve, int(goal * ratio))
    chosen = {id(c) for c in picked}
    for _ in range(goal):
        vis = [p for p in picked if p.get("visual_only")]
        if len(vis) <= cap:
            break
        victim = min(vis, key=lambda p: p["rank_score"])
        repl = sorted((c for c in cands if id(c) not in chosen
                       and not c.get("visual_only")
                       and not any(_overlap_near(c, p) for p in picked)),
                      key=lambda x: -x["rank_score"])
        if not repl:
            break
        picked.remove(victim)
        picked.append(repl[0])
        chosen.add(id(repl[0]))
    return picked


def ensure_front(picked: list[dict], cands: list[dict], goal: int,
                 front_min: int, front_max: int | None = None) -> list[dict]:
    """最后一道：把前 15 集的条数收进 [front_min, front_max]。

    前面的去重、画面保底、类型配额都按分数补位，会把前段候选挤出去；
    用户要的是"前 15 集密集"，所以这里宁可牺牲一点整体分，也要把前段填够。
    同时守住上限：前段超过八成时换回分数最高的后段候选，别把后段挤干净。
    换入换出都检查近重复，不会塞进同一段。
    """
    if front_min <= 0:
        return picked
    chosen = {id(c) for c in picked}

    have = [p for p in picked if p.get("front")]
    if len(have) < front_min:
        pool = sorted((c for c in cands if c.get("front") and id(c) not in chosen),
                      key=lambda x: -x.get("rank_score", 0))
        for c in pool:
            if len(have) >= front_min:
                break
            if any(_overlap_near(c, p) for p in picked):
                continue
            back = [p for p in picked if not p.get("front")]
            if not back:
                break
            picked.remove(min(back, key=lambda p: p.get("rank_score", 0)))
            picked.append(c)
            chosen.add(id(c))
            have.append(c)

    if front_max:
        while True:
            front = [p for p in picked if p.get("front")]
            if len(front) <= front_max:
                break
            victim = min(front, key=lambda p: p.get("rank_score", 0))
            pool = [c for c in cands
                    if not c.get("front") and id(c) not in chosen
                    and not any(_overlap_near(c, p) for p in picked if p is not victim)]
            if not pool:
                break
            repl = max(pool, key=lambda c: c.get("rank_score", 0))
            picked.remove(victim)
            picked.append(repl)
            chosen.add(id(repl))
    return picked


def interleave_types(picked: list[dict]) -> list[dict]:
    """展示顺序按类型轮转：前几条就能看到不同钩子，组内仍按强度降序。

    只改数组顺序、不改分数；配额与去重都在它之前完成，避免补进来的候选
    又把同一类型堆在一起。
    """
    groups: dict[str, list[dict]] = {}
    for c in picked:
        groups.setdefault(c.get("primary_type") or "other", []).append(c)
    for g in groups.values():
        g.sort(key=lambda x: -x.get("_eff", x["rank_score"]))
    out: list[dict] = []
    order = list(TYPE_ORDER) + ["other"]
    while any(groups.get(t) for t in order):
        for t in order:
            g = groups.get(t)
            if g:
                out.append(g.pop(0))
    return out


def apply_type_quotas(picked: list[dict], cands: list[dict], goal: int,
                      min_per_type: int = 1, max_per_type: int | None = None) -> list[dict]:
    """配额优先于纯分数：每类先保底 N 条、再限制上限，最后才比强度。

    上限按候选总数取比例（至少 4 条）：固定 4 条会在「整部剧都是台词冲突」
    的片子上把真正的高光挤出去——实测 24 条候选里有 8 条台词钩子时，
    后 4 条会被配额换成 6 分级别的凑数片段。补位还带质量闸门：
    换进来的至少要有被换掉那条一半的分，宁可不换也不拉低准头。
    """
    if not picked:
        return picked
    cap = max_per_type if max_per_type is not None else max(4, int(round(goal * 0.45)))
    chosen = {id(c) for c in picked}
    # 全池只按分数排一次。以前每个 pool() 都 sorted 一遍，24 轮下来是最大热点
    # （1255 个候选 × 24 次排序），而调用方每次只需要「最优的那一条」。
    ordered = sorted(cands, key=lambda c: -c.get("rank_score", 0))

    def pool(t=None, exclude=None):
        """返回第一个合格候选。同集才做重叠判断，跨集直接跳过（等价但快得多）。"""
        by_ep: dict[int, list[dict]] = {}
        for p in picked:
            by_ep.setdefault(p["ep"], []).append(p)
        for c in ordered:
            if id(c) in chosen:
                continue
            ct = c.get("primary_type")
            if t is not None and ct != t:
                continue
            if exclude is not None and ct == exclude:
                continue
            ps = by_ep.get(c["ep"])
            if ps and any(_overlap_near(c, p) for p in ps):
                continue
            return c
        return None

    for _ in range(goal):
        counts: dict[str, int] = {}
        for p in picked:
            t = p.get("primary_type") or "other"
            counts[t] = counts.get(t, 0) + 1
        over = next((t for t, n in counts.items() if n > cap), None)
        if over is None:
            break
        victim = min((p for p in picked if (p.get("primary_type") or "other") == over),
                     key=lambda p: p["rank_score"])
        repl = pool(exclude=over)
        if repl is None:
            break
        if repl.get("rank_score", 0.0) < victim.get("rank_score", 1.0) * 0.55:
            break                              # 补位的明显更差：宁可少一条也别拉低准头
        picked.remove(victim)
        picked.append(repl)
        chosen.add(id(repl))

    available = {c.get("primary_type") for c in cands if c.get("primary_type")}
    for t in TYPE_ORDER:
        if t not in available:
            continue
        for _ in range(min_per_type):
            if sum(1 for p in picked if p.get("primary_type") == t) >= min_per_type:
                break
            repl = pool(t=t)
            if repl is None:
                break
            victim = min(picked, key=lambda p: p["rank_score"])
            if repl.get("rank_score", 0.0) < victim.get("rank_score", 1.0) * 0.5:
                break                          # 为了凑类型把分数砍半，不值得
            picked.remove(victim)
            picked.append(repl)
            chosen.add(id(repl))
    return picked


def hook_key(c: dict) -> str:
    """候选的稳定身份：和前端 hookKey 同一格式（集号@全局秒数，保留 1 位小数）。

    前端微调位置会另存 hkShift，这里用的是未微调的原始起点，换一批 / 记录已展示
    都按它去重，不会因为用户挪了半秒就把同一段当成新画面。
    """
    return f"{int(c['ep'])}@{float(c['g0']):.1f}"


def find_highlights(beats: list[dict], top_k: int = 18,
                    min_dur: float = 4.5, max_dur: float = 20.0,
                    min_keep: int = 15, seed: int | None = None,
                    extra_visual: list[dict] | None = None,
                    visual_reserve: int = 3, type_quota: int = 1,
                    front_eps: int = FRONT_EPS,
                    front_boost: float = FRONT_BOOST,
                    exclude_keys: set[str] | None = None,
                    want_pool: bool = False):
    """找出 15-20 个高光开头候选。

    一段剧情算不算高光，看三件事：
    1. **台词在冲突**——打脸、揭身份、退婚、下跪（LEX 词库 + 感叹/反问）
    2. **画面在动手**——扇巴掌、泼酒、摔杯子、推人（vision 扫出来的冲击事件，
       这类镜头往往一句台词都没有，只靠字幕永远抓不到）
    3. **位置合适**——卡在集末、靠近反转，作为开头更抓人

    以前只取前 6 个，用户真正能挑的太少。现在按"集"配额铺开：
    每集最多 2 条、同集间隔 5 秒以上，先铺满 18 个；实在不够再放宽。

    seed：同一个剧本换台电脑、或者点「换一批」时给候选打散一点。
    评分只做 ±8% 的抖动，所以差一截的候选不会顶掉明显更好的，
    但相邻分数的会互换位置、入选的那 18 条也会换掉一部分（实测 18 条里换 10 条上下，
    最好的一条也常常换人）——这样两台电脑导入同一部剧不会得到一模一样的开头池。
    """
    cands: list[dict] = []
    n = len(beats)
    eps_last: dict[int, dict] = {}
    for b in beats:
        eps_last[b["ep"]] = b
    # 冲击强度按本剧自身分布归一：不同剧的配乐音量、运镜幅度差好几倍，
    # 用固定阈值会让整部剧的候选全被判成「动作冲突」（实测一部 44 集的剧
    # 941 个候选里 906 条都是 action，台词型钩子一条都进不了池）。
    # 归一之后只有明显高于本剧常态的段落才算动作 / 爆发。
    act_ref = max(1.0, _pct([float(b.get("act") or 0.0) for b in beats], 0.75, 1.6))
    aud_ref = max(0.8, _pct([float(b.get("aud") or 0.0) for b in beats], 0.75, 0.8))
    for i in range(n):
        for take in (1, 2, 3, 4, 5):
            j = i + take
            if j > n:
                break
            win = beats[i:j]
            if len({b["ep"] for b in win}) > 1:
                break
            g0, g1 = win[0]["g0"], win[-1]["g1"]
            dur = g1 - g0
            if dur > max_dur:
                break
            if dur < 2.6:
                continue
            text = " ".join(b["text"] for b in win)
            susp, shits = suspense_of(text)
            hits = sorted(set(shits) | {h for b in win for h in b.get("hits", [])})
            intim = _intimacy_score(text)
            punct = min(1.6, len(_PUNCT.findall(text)) * 0.35)
            act = max(b.get("act", 0.0) for b in win)
            act_n = min(2.6, act / act_ref)
            aud_n = min(2.0, max(b.get("aud", 0.0) for b in win) / aud_ref)
            vis = max(b.get("vis", 0.0) for b in win)
            # 短促但够炸：2.6-4.5 秒的一句"滚出去""你算什么东西"以前整段被丢掉，
            # 而这类恰恰是用户最想拿来当第一帧的。放宽门槛，但要求有硬信号。
            short_strong = dur < min_dur and (
                act_n >= 0.85 or intim >= 0.5 or punct >= 1.4
                or sum(1 for h in hits if h in LEX_CONFLICT) >= 2)
            if dur < min_dur:
                if not short_strong:
                    continue
                # 往前补一小段入戏（最多 2 秒，且不跨过本集开头），补成能用的长度
                floor = 0.0
                if i > 0 and beats[i - 1]["ep"] != win[0]["ep"]:
                    floor = beats[i - 1]["g1"]
                g0 = max(floor, g0 - min(2.0, min_dur - dur))
                dur = g1 - g0
                if dur < 2.8:
                    continue
            last = eps_last.get(win[0]["ep"])
            at_end = bool(last) and win[-1]["id"] == last["id"]
            # 长度偏置校正：直接相加会让"1 句狠话 + 4 句废话"的 20 秒长窗
            # 永远压过"整段都是冲突"的 6 秒短窗。以最高那一拍为主、其余折半计入。
            parts = [float(b["score"]) for b in win]
            raw = max(parts) + 0.5 * (sum(parts) - max(parts))
            # 冲击前置：开头 4 秒内就打起来，比"铺垫 8 秒才动手"更适合当钩子。
            # 以前只看整段总分，"安静铺垫 + 一句狠话"和"一巴掌开局"得分接近。
            peak_t = max(win, key=lambda b: (float(b.get("act") or 0.0), -b["g0"]))["g0"]
            lead_bonus = act_n * max(0.0, 1.0 - max(0.0, float(peak_t) - g0) / 4.0)
            # 密度加权：同样信息量，越短越紧
            score = (raw * (1.0 + min(0.5, 4.0 / max(1.0, dur))) * 0.95
                     + act_n * 2.2 + aud_n * 0.6 + susp * 0.8 + punct * 0.6
                     + 1.1 * lead_bonus
                     + (0.7 if short_strong else 0.0)
                     + (1.6 if at_end else 0.0))
            cands.append({
                "g0": g0, "g1": g1,
                "dur": round(dur, 3), "ep": win[0]["ep"],
                "text": text[:200],
                "score": round(score, 2),
                "act": round(act, 2),
                "susp": round(susp, 2),
                "intim": intim,
                "at_end": at_end,
                "short_strong": bool(short_strong),
                "kind": _hook_kind(win, text, susp, act_n),
                "hits": hits[:6],
                "beats": [b["id"] for b in win],
                "_v": round(vis, 3),
                "_a": round(aud_n, 3),
                "_burst": round(act_n, 3),
            })

    # 视觉候选的分数来自另一套量纲（事件强度约 0.85~3），直接混排会被台词分
    # 全压下去。用台词候选分数的中位数当锚点，把它们映射到同一区间的中段。
    if extra_visual:
        ds = sorted(c["score"] for c in cands)
        # 锚点取台词候选的高分位（P85）而不是全体中位数：真正入池的本来就是
        # 高分段，用中位数会把视觉候选永远压在池底，只能靠保底位露面。
        # 锚点取台词候选的 P90：真正入池的本来就是高分段。以前用 P75 并把视觉
        # 候选封顶在锚点，结果强打斗永远排在十几条普通台词后面（实测前 10 条
        # 一条画面钩子都没有）。
        anchor = max(4.0, ds[min(len(ds) - 1, int(len(ds) * 0.90))] if ds else 12.0)
        for vc in extra_visual:
            c = dict(vc)                     # 别改动调用方持有的对象
            raw = float(c.pop("_vscore", 0.0))
            t = min(1.0, max(0.0, (raw - 0.85) / 2.15))
            # 0.78~1.10 倍锚点：强的画面钩子能进第一梯队，弱的仍排在后面
            c["score"] = round(anchor * (0.78 + 0.32 * t), 2)
            # 视觉候选的冲击 / 声音也走同一套归一，"动作"标签才有可比性
            c["_burst"] = round(min(2.6, float(c.get("_burst") or 0.0) / act_ref), 3)
            c["_a"] = round(min(2.0, float(c.get("_a") or 0.0) / aud_ref), 3)
            cands.append(c)

    # 统一补齐类型 / 证据 / 质量 / 排序分；不合格的直接挡在门外（L0 轻量门槛）
    ep_count = len({c["ep"] for c in cands}) or 1
    kept_cands = []
    for c in cands:
        classify_candidate(c, conflict_words=LEX_CONFLICT,
                           reveal_words=LEX_REVEAL, ep_count=ep_count)
        if c["quality"]["playable"]:
            kept_cands.append(c)
    cands = kept_cands

    # 前 15 集标记：只在「总集数超过 front_eps」时生效，
    # 短片（≤15 集）不该有一半候选被当成"前段"额外加权
    ep_total = len({c["ep"] for c in cands}) or 1
    front_active = front_eps > 0 and ep_total > front_eps
    if front_active:
        for c in cands:
            if int(c["ep"]) <= front_eps:
                c["front"] = True

    # 抖动只作用在排序分上，且只在同档内小幅波动；展示分 score 保持原义
    # 打巴掌 / 吻戏 / 亲密接触再给一档小加成：这类节点是留人的主力
    def _hot(c: dict) -> bool:
        return (c.get("subtype") in ("slap", "kiss")
                or float(c.get("intim", 0.0)) >= 0.5)

    if seed:
        rng = random.Random(int(seed) & 0xFFFFFFFF)
        for c in cands:
            base = c["rank_score"] * (front_boost if c.get("front") else 1.0)
            if _hot(c):
                base *= 1.05
            c["_eff"] = base * (1.0 + rng.uniform(-0.08, 0.08))
    else:
        for c in cands:
            base = c["rank_score"] * (front_boost if c.get("front") else 1.0)
            if _hot(c):
                base *= 1.05
            c["_eff"] = base
    cands.sort(key=lambda c: (-c["_eff"], c["g0"]))

    # 「换一批」：把已经展示过的候选整批排除，再从剩下没露过面的里组一批。
    # 放在排序之后、配额之前，剩下的池子仍按原规则做去重 / 配额 / 前段密度。
    if exclude_keys:
        cands = [c for c in cands if hook_key(c) not in exclude_keys]
    pool_total = len(cands)

    def pick(want: int, per_ep: int, gap: float) -> list[dict]:
        picked: list[dict] = []
        cnt: dict[int, int] = {}
        front_n = 0
        # 前段最多占八成：加密是"更密"，不是"只有前段"
        front_limit = max(1, int(round(want * FRONT_SHARE))) if front_active else want
        # 前段保底：先按分数从前 15 集挑够 45%，再拿其余名额和全剧比。
        # 否则前段好候选会被后段更炸的段落整体压掉，"前 15 集密集"就落不了地。
        front_min = min(front_limit, max(1, int(round(want * 0.45)))) if front_active else 0

        def add(c: dict) -> bool:
            nonlocal front_n
            if cnt.get(c["ep"], 0) >= (per_ep + 1 if c.get("front") else per_ep):
                return False
            if c.get("front") and front_n >= front_limit:
                return False
            if any(c["g0"] < p["g1"] + gap and p["g0"] < c["g1"] + gap for p in picked):
                return False
            picked.append(c)
            cnt[c["ep"]] = cnt.get(c["ep"], 0) + 1
            if c.get("front"):
                front_n += 1
            return True

        for c in cands:                       # 第一轮：只补前段，补到保底数
            if len(picked) >= front_min:
                break
            if c.get("front"):
                add(c)
        for c in cands:                       # 第二轮：全体按分数补齐
            if len(picked) >= want:
                break
            add(c)
        return picked

    # 一级级放宽，直到够数。先"铺开"，再"堆密"——宁可每集一条也不要把 18 条挤在两集里
    # 15-20 集的剧按 1.2 倍集数给名额：只有 1 倍时，某一集多挑一条就会有另一集
    # 整集没候选（实测 20 集的剧正好漏掉 4 个真实高光）。24 集以上仍封顶 24。
    goal = min(top_k, max(min_keep, int(round(len({b["ep"] for b in beats}) * 1.2))))
    picked: list[dict] = []
    for per_ep, gap in ((2, 5.0), (3, 3.0), (4, 1.5), (99, 0.4)):
        picked = pick(goal, per_ep, gap)
        if len(picked) >= goal:
            break

    picked = dedup_near(picked, cands, goal)
    picked = _ensure_visual(picked, cands, min(visual_reserve, goal))
    picked = cap_visual(picked, cands, goal, min(visual_reserve, goal))
    # 配额放最后一道：去重补位后仍要守住每类上下限，且补入时不再引入近重复
    picked = apply_type_quotas(picked, cands, goal, min_per_type=type_quota)
    # 前段密度是硬要求，放在配额之后兜底（配额补位会按分数把前段挤掉）
    picked = ensure_front(picked, cands, goal,
                          max(1, int(round(goal * FRONT_MIN_SHARE))) if front_active else 0,
                          int(round(goal * FRONT_SHARE)) if front_active else None)
    picked = interleave_types(picked)                  # 最后按类型轮转，避免同类霸榜
    for i, p in enumerate(picked):
        for k in ("_eff", "_v", "_a", "_burst", "_vscore"):
            p.pop(k, None)
        p["id"] = i
    if want_pool:
        return picked, pool_total
    return picked


def find_endings(beats: list[dict], total: float, top_k: int = 8,
                 min_dur: float = 4.0, max_dur: float = 18.0) -> list[dict]:
    """在剧情后段找「结尾钩子」候选。

    结尾钩子和开头钩子目的不同：开头钩子要"炸"，结尾钩子要"悬"——
    一个没解开的问题、一次突转、一句留扣，让观众必须去 App 看后续。
    只在全剧最后 10%（45-150 秒）里找：这段会被挪到成片最末，若取自更靠前
    的剧情，正片讲完结局又倒回去，观众会觉得顺序错乱。越接近剧终权重越高。
    """
    if not beats or total <= 0:
        return []
    tail = min(150.0, max(45.0, total * 0.10))
    zone = [b for b in beats if b["g1"] >= total - tail]
    cands: list[dict] = []
    n = len(zone)
    for i in range(n):
        for take in (1, 2, 3, 4, 5):
            j = i + take
            if j > n:
                break
            win = zone[i:j]
            if len({b["ep"] for b in win}) > 1:
                break
            dur = win[-1]["g1"] - win[0]["g0"]
            if dur > max_dur:
                break
            if dur < min_dur:
                continue
            text = " ".join(b["text"] for b in win)
            sw, shits = suspense_of(text)
            recency = (win[0]["g0"] - (total - tail)) / tail  # 0→1，越靠剧终越高
            score = sw + sum(b["score"] for b in win) * 0.55 + recency * 4.2
            cands.append({
                "g0": win[0]["g0"], "g1": win[-1]["g1"],
                "dur": round(dur, 3), "ep": win[0]["ep"],
                "text": text[:180],
                "score": round(score, 2),
                "hits": sorted(set(shits)
                               | {h for b in win for h in b["hits"]})[:6],
                "beats": [b["id"] for b in win],
            })
    cands.sort(key=lambda c: -c["score"])
    picked: list[dict] = []
    for c in cands:
        if any(not (c["g1"] <= p["g0"] or c["g0"] >= p["g1"]) for p in picked):
            continue
        picked.append(c)
        if len(picked) >= top_k:
            break
    # 尾部候选可能太扎堆，补一个「剧终最后一段」兜底，保证至少有一个真结尾
    if picked and not any(c["g1"] >= total - tail * 0.15 for c in picked):
        lastwin: list[dict] = []
        for b in reversed(zone):
            lastwin.insert(0, b)
            dur = lastwin[-1]["g1"] - lastwin[0]["g0"]
            if dur >= max(4.0, min_dur):
                break
            if dur > max_dur or len(lastwin) > 5:
                break
        if min_dur <= lastwin[-1]["g1"] - lastwin[0]["g0"] <= max_dur:
            picked.append({
                "g0": lastwin[0]["g0"], "g1": lastwin[-1]["g1"],
                "dur": round(lastwin[-1]["g1"] - lastwin[0]["g0"], 3),
                "ep": lastwin[0]["ep"],
                "text": " ".join(b["text"] for b in lastwin)[:180],
                "score": 0.0, "hits": [], "beats": [b["id"] for b in lastwin],
                "note": "剧终",
            })
    for i, p in enumerate(picked):
        p["id"] = i
    return picked


def hook_batch(proj: dict, exclude_keys: set[str] | None = None,
               seed: int | None = None, top_k: int = 24,
               min_keep: int = 15) -> tuple[list[dict], int]:
    """按排除集合组一批高光开头（换一批用）。

    返回 (候选, 排除之后池子里还剩多少条候选)。界面用剩余数告诉用户
    "候选池还剩 N 条没看过"，而不是只报本批有几条。
    """
    beats = segment_beats(proj)
    acts = proj.get("acts") or []
    attach_acts(beats, acts)
    visual = find_visual_highlights(proj, beats, acts)
    sd = proj.get("hook_seed") if seed is None else seed
    return find_highlights(beats, top_k=top_k, min_keep=min_keep, seed=sd,
                           extra_visual=visual, exclude_keys=exclude_keys,
                           want_pool=True)


def analyze(proj: dict, seed: int | None = None) -> dict:
    beats = segment_beats(proj)
    acts = proj.get("acts") or []
    attach_acts(beats, acts)
    # 无台词时间窗里的高能画面，走独立候选来源；没有动作点时返回空表，
    # find_highlights 会自然退回原来的纯对白候选池
    visual = find_visual_highlights(proj, beats, acts)
    dialog = sum(b["dur"] for b in beats)
    total = proj["total"]
    # 钩子池的随机种子跟着项目走：项目里存了就每次一致，没有（老项目）就保持原来的确定性排序
    sd = proj.get("hook_seed") if seed is None else seed
    # 换过一批之后，重开项目要回到**最后展示的那一批**，而不是又回到第一屏。
    # 直接存下那一批的内容：只存 key 再重算的话，排除旧候选会改变去重/配额的
    # 补位顺序，回来的一批和用户刚看到的对不上。
    cur_items = proj.get("hook_current_items") or []
    hl = None
    if cur_items and all(1 <= int(c.get("ep", 0)) <= len(proj["episodes"])
                         and float(c.get("g1", 0)) <= total + 1.0 for c in cur_items):
        hl = [dict(c) for c in cur_items]
        for i, h in enumerate(hl):
            h["id"] = i
    if not hl:
        shown = set(proj.get("hook_shown") or [])
        current = set(proj.get("hook_current") or [])
        hl = find_highlights(beats, top_k=24, min_keep=15, seed=sd,
                             extra_visual=visual, exclude_keys=shown - current)
    return {
        "beats": beats,
        "highlights": hl,
        "hook_meta": {
            "front_eps": FRONT_EPS,
            "front_count": sum(1 for c in hl if c.get("front")),
            "eps": len(proj["episodes"]),
            "boost": FRONT_BOOST,
        },
        "acts": len(acts),
        "visual_cands": len(visual),
        "endings": find_endings(beats, total),
        "stats": {
            "total": round(total, 1),
            "beats": len(beats),
            "dialog": round(dialog, 1),
            "silence": round(max(0.0, total - dialog), 1),
            "eps": len(proj["episodes"]),
            "sub_eps": proj.get("sub_episodes", 0),
            "avg_beat": round(dialog / max(1, len(beats)), 1),
        },
    }

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

_EN_PATTERNS = [(cat, w, kw, re.compile(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])"))
                for cat, (w, words) in EN_LEX.items() for kw in words]
_EN_SUSPENSE = [(kw, wt, re.compile(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])"))
                for kw, wt in EN_SUSPENSE]

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
    for cat, w, kw, pat in _EN_PATTERNS:
        if pat.search(lower):
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
    hits += [(kw, wt) for kw, wt, pat in _EN_SUSPENSE if pat.search(low)]
    q = len(re.findall(r"[？?]", text))
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
        return
    for b in beats:
        best = 0.0
        n = 0
        kinds: list[str] = []
        for a in acts:
            if a["ep"] != b["ep"]:
                continue
            if a["g1"] < b["g0"] - pad0 or a["g0"] > b["g1"] + pad1:
                continue
            n += 1
            if a["score"] > best:
                best = a["score"]
            if a.get("v", 0) >= 0.5:
                kinds.append("动作")
            elif a.get("a", 0) >= 0.8:
                kinds.append("爆发")
        b["act"] = round(best, 3)
        b["actn"] = n
        b["act_kind"] = kinds[0] if kinds else ("爆发" if best else "")


def _density(text: str, dur: float) -> float:
    """语速密度：中文按字/秒，英文按词/秒（两种剧都要能用）。"""
    if dur <= 0.2:
        return 0.0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
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


def _hook_kind(win: list[dict], text: str, susp: float) -> str:
    """给候选贴一个"为什么它算高光"的标签，方便一眼挑选。"""
    act = max((b.get("act", 0.0) for b in win), default=0.0)
    has_conflict = any(h in LEX_CONFLICT for b in win for h in b.get("hits", []))
    has_reveal = any(b.get("hits") and set(b["hits"]) & set(LEX_REVEAL)
                     for b in win)
    if act >= 2.2:
        return "动作冲突" if has_conflict or any(b.get("act_kind") == "动作" for b in win) else "爆发点"
    if has_conflict:
        return "台词冲突"
    if has_reveal:
        return "身份反转"
    if susp >= 3.0:
        return "悬念留扣"
    return "剧情节点"


def find_highlights(beats: list[dict], top_k: int = 18,
                    min_dur: float = 4.5, max_dur: float = 20.0,
                    min_keep: int = 15, seed: int | None = None) -> list[dict]:
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
    for i in range(n):
        for take in (1, 2, 3, 4):
            j = i + take
            if j > n:
                break
            win = beats[i:j]
            if len({b["ep"] for b in win}) > 1:
                break
            dur = win[-1]["g1"] - win[0]["g0"]
            if dur > max_dur:
                break
            if dur < min_dur:
                continue
            text = " ".join(b["text"] for b in win)
            raw = sum(b["score"] for b in win)
            act = max(b.get("act", 0.0) for b in win)
            susp, shits = suspense_of(text)
            punct = min(1.6, len(_PUNCT.findall(text)) * 0.35)
            last = eps_last.get(win[0]["ep"])
            at_end = bool(last) and win[-1]["id"] == last["id"]
            # 密度加权：同样信息量，越短越紧
            score = (raw * (1.0 + min(0.5, 4.0 / max(1.0, dur))) * 0.95
                     + act * 2.4 + susp * 0.8 + punct * 0.6
                     + (1.6 if at_end else 0.0))
            hits = sorted(set(shits) | {h for b in win for h in b.get("hits", [])})
            cands.append({
                "g0": win[0]["g0"], "g1": win[-1]["g1"],
                "dur": round(dur, 3), "ep": win[0]["ep"],
                "text": text[:200],
                "score": round(score, 2),
                "act": round(act, 2),
                "susp": round(susp, 2),
                "at_end": at_end,
                "kind": _hook_kind(win, text, susp),
                "hits": hits[:6],
                "beats": [b["id"] for b in win],
            })

    # 抖动只影响「谁先被挑中」，不动展示出去的分数
    if seed:
        rng = random.Random(int(seed) & 0xFFFFFFFF)
        for c in cands:
            c["_eff"] = c["score"] * (1.0 + rng.uniform(-0.08, 0.08))
    else:
        for c in cands:
            c["_eff"] = c["score"]
    cands.sort(key=lambda c: (-c["_eff"], c["g0"]))

    def pick(want: int, per_ep: int, gap: float) -> list[dict]:
        picked: list[dict] = []
        cnt: dict[int, int] = {}
        for c in cands:
            if len(picked) >= want:
                break
            if cnt.get(c["ep"], 0) >= per_ep:
                continue
            if any(c["g0"] < p["g1"] + gap and p["g0"] < c["g1"] + gap
                   for p in picked):
                continue
            picked.append(c)
            cnt[c["ep"]] = cnt.get(c["ep"], 0) + 1
        return picked

    # 一级级放宽，直到够数。先"铺开"，再"堆密"——宁可每集一条也不要把 18 条挤在两集里
    goal = min(top_k, max(min_keep, len({b["ep"] for b in beats})))
    picked: list[dict] = []
    for per_ep, gap in ((2, 5.0), (3, 3.0), (4, 1.5), (99, 0.4)):
        picked = pick(goal, per_ep, gap)
        if len(picked) >= goal:
            break

    picked.sort(key=lambda c: (-c["_eff"], c["g0"]))
    for i, p in enumerate(picked):
        p.pop("_eff", None)
        p["id"] = i
    return picked


def find_endings(beats: list[dict], total: float, top_k: int = 5,
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


def analyze(proj: dict, seed: int | None = None) -> dict:
    beats = segment_beats(proj)
    acts = proj.get("acts") or []
    attach_acts(beats, acts)
    dialog = sum(b["dur"] for b in beats)
    total = proj["total"]
    # 钩子池的随机种子跟着项目走：项目里存了就每次一致，没有（老项目）就保持原来的确定性排序
    sd = proj.get("hook_seed") if seed is None else seed
    return {
        "beats": beats,
        "highlights": find_highlights(beats, top_k=18, min_keep=15, seed=sd),
        "acts": len(acts),
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

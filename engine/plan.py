"""顺剪规划：从选定的高光开头起，顺着剧情一路往下接，卡在最抓人的地方收尾。

以前的做法是"全剧切幕、每幕按冲突分挑台词"，结果是把 40 集剪成几十段碎镜头，
观众根本接不上前因后果。现在的逻辑只有一条线：

    开头钩子 → 第 1 集这段往后 → 第 2 集 → 第 3 集 → …… 直到时长够
             → 停在一个"没讲完"的卡点（未答问题 / 未落动作 / 将揭身份）
             → 引导尾帧

关键点：
1. 正片是**连续播放**的——窗口内每一集从头到尾整段保留，不按台词密度挑镜头。
   字幕稀疏的剧里，两句台词之间那 20 秒空白往往正是剧情在演，剪掉就断了。
2. "停在哪"才需要挑：默认优先停在**悬念卡点**，而不是集末或剧终。
   未回答的问句、没说完的话、即将发生的动作、下一拍才揭晓的身份/真相都加分；
   "没事了 / 在一起了 / 真相大白"这类收束句扣分。集末只有本身带悬念才优先，
   不会为了"收得完整"硬把成片拉到集末。
3. 只有用户手动点了某句"不要这句"，或开了紧凑模式，才会从连续画面里挖掉一段。
4. 批量出片时同一开头的多条会换一个结尾候选、换几处较长的空镜来剪，
   结构一致但画面确实不同。

结尾的"悬念"只作用于**正片最后一个卡点**，与用户上传的「引导尾帧」无关：
尾帧照常播放，不会为了收束额外加字卡或补一段完整结局。
"""
from __future__ import annotations

import random

from . import analyze as az
from . import tailcard
from . import project as proj_mod

MIN_TAIL = 8.0      # 结尾最少这么长，太短就是"刚开头就结束"
MIN_BLOCK = 0.35    # 小于这个长度的碎块直接丢掉

# 收束 / 完结信号：停在这些句子上，观众会觉得"故事已经讲完了"，不再想追。
# 只收强信号短语，避免"完蛋""没完"这类反而带悬念的词被误判。
CLOSED_PHRASES = (
    "结束了", "到此为止", "没事了", "都过去了", "真相大白", "水落石出",
    "大结局", "全剧终", "圆满结束", "和解了", "原谅你了", "我们结婚",
    "在一起了", "就这么定了", "解决了", "安心了", "放心了", "尘埃落定",
)

# 未完成 / 悬置信号：问题、威胁、命令、突然发现。suspense_of 已覆盖一部分，
# 这里补的是"话没落地"的口语信号。
OPEN_PHRASES = (
    "等一下", "等等", "站住", "住手", "放开", "别动", "凭什么", "什么意思",
    "你说什么", "为什么", "你等着", "走着瞧", "没完", "我告诉你", "你听好",
)

REVEAL_PHRASES = ("原来", "竟然", "居然", "身份", "真相", "秘密", "伪装", "继承人")

# 回望 / 总结式旁白：在回顾已经发生的事，停在这里像"讲完了"，不是留扣。
NARRATIVE_PHRASES = (
    "后来我才", "后来才", "如果当时", "多年以后", "回想起来", "那时候我",
    "at the time", "i didn't realize", "i did not realize", "looking back",
    "i wished", "never knew",
)



def _clamp(ep: dict, t: float) -> float:
    return max(0.0, min(ep["dur"], t))


def _beats_in(beats: list[dict], g0: float, g1: float) -> list[dict]:
    """窗口内的节拍：按节拍中心点判定。

    用"有重叠就算"会把边界处只沾到零点几秒的长句整句拉进来，
    钩子窗口 13 秒能变成 20 秒；按中心点判定宽度才跟候选一致。
    """
    return [b for b in beats
            if g0 - 0.01 <= (b["g0"] + b["g1"]) / 2 <= g1 + 0.01]


def _hook_seg(proj: dict, beats: list[dict], hook: dict | None,
              lead: float = 0.0) -> dict | None:
    """把选中的开头钩子变成成片的第一段。

    lead 是「开头入戏」：从钩子起点再往前多切几秒。打脸、扇巴掌这类动作
    往往比台词早半秒到两秒出现，按台词起点切会正好错过动作，看起来就像
    "成片的钩子比预览晚了"。往前留一点，动作才在片头里。

    另外，窗口里一个"节拍中心"都没有时（手动加的钩子、或者整段挪到了两句
    台词之间的空镜里），以前直接 return None，成片会退回"从第 1 集 0:00 顺剪"——
    用户选了钩子却看到成片开头完全不对，就是这么来的。现在退一步：
    有重叠的节拍就用它算文字，一句都没有也照样切这一刀（你点哪段就出哪段）。
    """
    if not hook or hook.get("g1", 0) <= hook.get("g0", 0):
        return None
    hb = _beats_in(beats, hook["g0"], hook["g1"])
    if not hb:
        hb = [b for b in beats
              if b["g1"] > hook["g0"] + 0.02 and b["g0"] < hook["g1"] - 0.02]
    ep_idx = hb[0]["ep"] if hb else proj_mod.loc(proj, max(0.0, float(hook["g0"])))[0]
    ep = proj_mod.ep_by_idx(proj, ep_idx)
    ep_lo = float(ep["offset"])
    ep_hi = ep_lo + float(ep["dur"])
    g0 = max(ep_lo, float(hook["g0"]) - max(0.0, float(lead)))
    g1 = min(ep_hi, float(hook["g1"]))
    if g1 <= g0:
        return None
    return {
        "ep": ep_idx,
        "g0": round(g0, 3), "g1": round(g1, 3),
        "dur": round(g1 - g0, 3),
        "text": " ".join(b["text"] for b in hb)[:220],
        "hits": sorted({h for b in hb for h in b["hits"]}),
        "score": round(sum(b["score"] for b in hb), 2),
        "kind": "hook", "keep": True, "beats": [b["id"] for b in hb],
    }


def _walk(pool: list[dict], base: float, span: float) -> float:
    """从 base 起按顺序往后走，返回"刚够 span 秒"的那个节拍的结束时间。"""
    end = base
    for b in pool:
        if b["g1"] <= base + 0.02:
            continue
        end = b["g1"]
        if end - base >= span:
            break
    return end


def _ep_end_g(proj: dict, beats: list[dict], ep_idx: int) -> float:
    """某一集的"集末卡点"：该集最后一个节拍的结束时间。"""
    try:
        ep = proj_mod.ep_by_idx(proj, ep_idx)
    except KeyError:
        return 0.0
    bl = [b for b in beats if b["ep"] == ep_idx]
    return max(b["g1"] for b in bl) if bl else ep["offset"] + ep["dur"]


def _has_phrase(text: str, words) -> bool:
    return bool(text) and any(w in text for w in words)


def _is_closed(text: str) -> bool:
    """这句话是不是"收束/完结"信号。停在这里观众容易觉得已经讲完。"""
    return _has_phrase(text, CLOSED_PHRASES)


def _has_reveal(text: str, hits=()) -> bool:
    return _has_phrase(text, REVEAL_PHRASES) or bool(set(hits or ()) & az.LEX_REVEAL)


def _open_loop(b: dict, next_b: dict | None) -> tuple[float, list[str], str, bool]:
    """给"停在这一拍"的悬念强度打分。

    不是找"最炸"的句子，而是找**没讲完**的地方：
      · 有上下文的问句 / 悬念词        —— 观众想知道答案（日常寒暄不算）
      · 话没说完（… / ——）              —— 语句悬在半空
      · 真实的动作 / 情绪冲击            —— 动作或情绪没有落地
      · 下一拍才动手 / 才揭身份          —— 停在这里正好留扣
      · 下一拍就是收束句                —— 现在停，把"完"留在画面外
    停在"没事了 / 在一起了 / 真相大白"这类句子上会明显扣分；
    "后来我才知道 / 如果当时 / At the time, I didn't realize"这类回望旁白也扣分，
    它们是在总结，不是在留扣。
    返回 (分数, 信号, 主类型, 是否收束/回望句)。
    """
    text = (b.get("text") or "").strip()
    low = text.lower()
    score = 0.0
    signals: list[str] = []

    susp, _hits = az.suspense_of(text)
    q = text.count("？") + text.count("?")
    # suspense_of 把每个问号按 1.1 计入了；这里剥掉，问句按"有没有上下文"另算，
    # 避免 "Do you really need the extra butter?" 这种日常问句被当成悬念。
    susp_eff = max(0.0, susp - q * 1.1)
    has_susp = susp_eff >= 0.5
    if has_susp:
        score += min(6.0, susp_eff * 1.35)
        signals.append("悬念词")

    has_conflict = _has_phrase(text, OPEN_PHRASES)
    ctx = (float(b.get("score") or 0.0) >= 3.2
           or bool(b.get("hits")) or has_conflict
           or (float(b.get("act") or 0.0) >= 2.2
               and (float(b.get("vis") or 0.0) >= 0.35
                    or float(b.get("aud") or 0.0) >= 1.0))
           or susp_eff >= 1.0)
    has_q = bool(q and ctx)
    if q:
        if has_q:
            score += min(3.0, 2.2 + (q - 1) * 0.5)
            signals.append("未答问题")
        else:
            score += 0.3          # 日常问句：不构成钩子，但不至于倒扣
            signals.append("普通问句")

    if has_conflict:
        score += 1.0
        signals.append("话没落地")

    if text.endswith(("…", "……", "——", "-")):
        score += 1.2
        signals.append("话没说完")

    act = float(b.get("act") or 0.0)
    aud = float(b.get("aud") or 0.0)
    vis = float(b.get("vis") or 0.0)
    act_kind = str(b.get("act_kind") or "")
    has_act = False
    has_emotion = False
    if act_kind == "动作" and act >= 1.0:
        score += min(3.2, 1.2 + act * 0.75)
        signals.append("动作未收")
        has_act = True
    elif act_kind == "爆发" and act >= 1.2 and (vis >= 0.35 or (aud >= 1.0 and text.endswith(("！", "!")))):
        score += 1.6
        signals.append("情绪未落")
        has_emotion = True
    elif act >= 2.8 and (vis >= 0.35 or aud >= 1.0):
        # 没有动作分类、但有明显冲击：少量计入，避免纯配乐/运镜被当成动作
        score += 1.0
        signals.append("冲击未落")
        has_act = True

    has_reveal = False
    if next_b:
        nt = next_b.get("text") or ""
        n_act = float(next_b.get("act") or 0.0)
        n_vis = float(next_b.get("vis") or 0.0)
        n_aud = float(next_b.get("aud") or 0.0)
        n_kind = str(next_b.get("act_kind") or "")
        if n_kind == "动作" and n_act >= 1.0 and act < n_act * 0.7:
            score += 2.2
            signals.append("动作将起")
            has_act = True
        elif (n_kind == "爆发" and n_act >= 1.2
              and (n_vis >= 0.35 or n_aud >= 1.0) and act < n_act * 0.7):
            score += 1.2
            signals.append("情绪将起")
            has_emotion = True
        if _has_reveal(nt, next_b.get("hits")) and not _has_reveal(text, b.get("hits")):
            score += 2.0
            signals.append("身份将揭")
            has_reveal = True
        if _is_closed(nt) and not _is_closed(text):
            score += 1.8
            signals.append("下一句才收束")

    closed = _is_closed(text)
    if closed:
        score -= 5.5
        signals.append("收束句")

    # 回望/总结式旁白：信息已经"讲完"，不是悬念
    if _has_phrase(low, NARRATIVE_PHRASES):
        score -= 3.0
        signals.append("旁白回望")
        has_q = has_act = has_emotion = has_reveal = has_susp = False

    # 极短的语气词（"嗯""哦"）本身不构成钩子；带问号/命令的仍保留
    if text and len(text) <= 3 and not q and not has_conflict:
        score -= 1.0

    if closed or "旁白回望" in signals:
        hook_type = ""
    elif has_q:
        hook_type = "问题"
    elif has_reveal:
        hook_type = "反转"
    elif has_act:
        hook_type = "动作"
    elif has_emotion:
        hook_type = "情绪"
    elif has_susp:
        hook_type = "悬念"
    elif has_conflict:
        hook_type = "冲突"
    else:
        hook_type = ""

    seen = set()
    uniq = []
    for s in signals:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return round(score, 3), uniq, hook_type, bool(closed or "旁白回望" in signals)



def _end_candidates(proj: dict, beats: list[dict], base: float,
                    t0: float, body_budget: float) -> list[dict]:
    """在"时长差不多够了"的位置附近，给每个可能的结束点打分。

    默认不是"找一个完整结尾"，而是**找一个没讲完的卡点**：

      · 未答问题 / 悬念词        —— 观众正想知道答案（主要加分）
      · 话没说完 / 情绪没落地    —— 语句或情绪悬在半空
      · 动作还没发生（下一拍才起）—— 停在动手前，追看动力最强
      · 下一拍才揭身份 / 真相    —— 现在停，把"揭晓"留在画面外
      · 下一拍就是收束句         —— 现在停，避免"这就演完了"的感觉
      · 时长贴合 / 后面还有剧    —— 保持节奏自然，不为了悬念拖太长
      · 集末卡点                —— 只给少量加分；集末本身是收束句时反而扣分
      · 收束句（没事了 / 在一起了 / 真相大白）—— 明显扣分，拒绝生硬完结
    只在节拍结束点取值，保证不会掐在半句话上。
    """
    eps = {e["idx"]: e for e in proj["episodes"]}
    last_of: dict[int, dict] = {}
    for b in beats:
        last_of[b["ep"]] = b
    total = proj["total"]
    # 允许比目标略早停（0.60）—— 悬念卡点常常就在收束句前一两拍；
    # 但不为了一个"看起来有情绪"的集末把 3 分钟的目标拉到 3 分 40 秒。
    lo = base + max(MIN_TAIL, body_budget * 0.60)
    hi = t0 + max(20.0, min(90.0, body_budget * 0.16))

    raw: list[dict] = []
    for i, b in enumerate(beats):
        g1 = b["g1"]
        if g1 < lo or g1 > hi or g1 > total + 0.5:
            continue
        tail = beats[max(0, i - 1):i + 1]
        text = " ".join(x["text"] for x in tail).strip()
        next_b = beats[i + 1] if i + 1 < len(beats) else None
        susp, shits = az.suspense_of(text)
        open_score, signals, hook_type, closed = _open_loop(b, next_b)
        ep = eps.get(b["ep"])
        off_end = (ep["offset"] + ep["dur"] - g1) if ep else 99.0
        is_last = last_of.get(b["ep"]) is b
        elapsed = g1 - base
        dev = abs(elapsed - body_budget) / max(1.0, body_budget)

        if is_last:
            # 集末只是"轻微优先"，不再是必选项：真正决定的是这一拍有没有悬念。
            # 离目标太远（>22%）继续压低，避免 6 分钟目标被 4 分钟的集末硬拽走。
            bonus = 2.6 if off_end <= 2.5 else 1.8
            if dev > 0.22:
                bonus *= max(0.35, 1.0 - (dev - 0.22) * 1.8)
        elif off_end <= 6.0:
            bonus = 1.2
        elif off_end <= 12.0:
            bonus = 0.4
        else:
            bonus = -0.8        # 掐在集中间，观众能感觉到"被切了"

        fit = -abs(elapsed - body_budget) / 7.0
        if elapsed < body_budget * 0.88:
            fit -= 1.8          # 明显短于目标：节奏会显得仓促
        if is_last:
            fit *= 0.7
        if dev > 0.12:
            # 超出 12% 后加速扣分：悬念不能成为拖长成片的理由
            fit -= (dev - 0.12) * body_budget / 2.0
        more = 2.5 if g1 < total - 3.0 else -6.0
        content = sum(x["score"] for x in tail) * 0.20
        # 有效悬念优先：宁可早/晚几秒，也停在一个真留扣的地方，而不是
        # 为了卡准目标时长落回普通剧情点。弱信号（<1.5）不算，避免硬造。
        hook_bonus = 1.2 if open_score >= 1.5 else 0.0
        if open_score >= 3.0:
            hook_bonus += 0.6
        score = open_score + hook_bonus + bonus + fit + more + content
        hook_ok = bool(open_score >= 1.5
                       and abs(elapsed - body_budget) <= max(12.0, body_budget * 0.12))
        raw.append({
            "g0": round(tail[0]["g0"], 3), "g1": round(g1, 3), "ep": b["ep"],
            "score": round(score, 2), "open": round(open_score, 2),
            "hook_ok": hook_ok,
            "susp": round(susp, 2),
            "signals": signals, "hook_type": hook_type, "closed": bool(closed),
            "hits": shits[:4],
            "at_ep_end": bool(is_last), "off_end": round(off_end, 2),
            "text": text[:120],
            "elapsed": round(elapsed, 1),
        })

    raw.sort(key=lambda c: -c["score"])
    picked: list[dict] = []
    for c in raw:
        if any(abs(c["g1"] - p["g1"]) < 4.0 for p in picked):
            continue
        picked.append(c)
        if len(picked) >= 8:
            break
    for c in picked:
        c["reason"] = _reason(c, total)
    return picked


def _reason(c: dict, total: float) -> str:
    ep = c.get("ep")
    if c["g1"] >= total - 3.0:
        return f"停在第 {ep} 集剧终"
    if c.get("closed"):
        return f"停在第 {ep} 集自然剧情点"
    if float(c.get("open") or 0.0) >= 1.5 and (c.get("signals") or c.get("hook_type")):
        hits = c.get("hits") or []
        hint = "、".join(str(h) for h in hits[:2]) or (c.get("text") or "").strip()[:18]
        if c.get("at_ep_end") and c.get("off_end", 99) <= 2.5:
            return f"卡在第 {ep} 集结尾 · 留悬念"
        return (f"停在第 {ep} 集「{hint}」· 留悬念" if hint
                else f"停在第 {ep} 集 · 留悬念")
    if c.get("at_ep_end") and c.get("off_end", 99) <= 2.5:
        return f"卡在第 {ep} 集结尾"
    if c.get("hits"):
        return f"停在第 {ep} 集「{'、'.join(c['hits'][:2])}」"
    if c.get("off_end", 99) <= 6.0:
        return f"卡在第 {ep} 集最后一段"
    return f"停在第 {ep} 集剧情卡点"


def _blocks(proj: dict, base: float, end_t: float,
            drops: list[dict], win: list[dict]) -> list[dict]:
    """把 [base, end_t] 切成"连续播放块"：天然按集切，被挖掉的地方再断开。

    每一块就是一段连续画面。集与集之间本来就要硬切，所以跨集一定断开。
    """
    out: list[dict] = []
    for ep in proj["episodes"]:
        a = max(base, ep["offset"])
        b = min(end_t, ep["offset"] + ep["dur"])
        if b - a < MIN_BLOCK:
            continue
        cuts = sorted((d for d in drops
                       if a - 0.01 <= d["g0"] and d["g1"] <= b + 0.01),
                      key=lambda d: d["g0"])
        cur = a
        for d in cuts:
            if d["g0"] - cur >= MIN_BLOCK:
                out.append({"ep": ep["idx"], "g0": cur, "g1": d["g0"]})
            cur = max(cur, d["g1"])
        if b - cur >= MIN_BLOCK:
            out.append({"ep": ep["idx"], "g0": cur, "g1": b})

    for s in out:
        s["dur"] = round(s["g1"] - s["g0"], 3)
        inner = [x for x in win if x["g0"] >= s["g0"] - 0.05 and x["g1"] <= s["g1"] + 0.05]
        s["text"] = " ".join(x["text"] for x in inner)[:220]
        s["hits"] = sorted({h for x in inner for h in x["hits"]})[:6]
        s["score"] = round(sum(x["score"] for x in inner), 2)
        s["beats"] = [x["id"] for x in inner]
        s["kind"] = "body"
        s["keep"] = True
    return out


def _apply_tail_trim(proj: dict, segs: list[dict], tail_mode: str,
                     tail_fixed: float, beats: list[dict] | None = None) -> list[dict]:
    """集与集的接缝处，切掉上一集末尾的平台片尾卡（去尾帧）。

    只动"后面还接下一集"的块：成片最后一段的尾巴原样保留（后面接的是固定
    结尾视频），结尾视频本身不经过这里，永远不会被裁。
    """
    mode = (tail_mode or "auto").lower()
    if mode == "off" or len(segs) < 2:
        return []
    eps = {e["idx"]: e for e in proj["episodes"]}
    cuts: list[dict] = []
    for i in range(len(segs) - 1):
        s, nxt = segs[i], segs[i + 1]
        if s.get("kind") == "hook" or nxt["ep"] <= s["ep"]:
            continue                      # 同集内的断点（挖句）不是集与集的接缝
        ep = eps.get(s["ep"])
        if not ep:
            continue
        if s["g1"] < ep["offset"] + ep["dur"] - 0.6:
            continue                      # 这段没走到集末，末尾没有卡片
        sec, by = tailcard.trim_for(ep.get("path", ""), ep.get("dur", 0.0),
                                    mode, tail_fixed)
        sec = min(sec, max(0.0, s["dur"] - 0.6))   # 别把这一块切没了
        if beats and sec > 0:
            # 只保护「跨进定帧段」的台词：正片最后一句如果还在说，不能切掉。
            # 完全落在定帧段内的"台词"通常是卡片上的文字被 OCR 认了出来
            # （实测片尾卡上的 "TR" 会把裁剪量压成 0），不参与保护。
            card_start = s["g1"] - sec
            tail_beats = [b for b in beats
                          if b["ep"] == s["ep"]
                          and b["g0"] < card_start < b["g1"] <= s["g1"] + 0.05]
            if tail_beats:
                sec = min(sec, max(0.0, s["g1"] - max(b["g1"] for b in tail_beats)))
        if sec <= 0.05:
            continue
        s["g1"] = round(s["g1"] - sec, 3)
        s["dur"] = round(s["dur"] - sec, 3)
        cuts.append({"ep": s["ep"], "sec": round(sec, 2), "by": by})
    return cuts


def _trim(win: list[dict], base: float, end_t: float, rt: float,
          rng: random.Random | None) -> list[dict]:
    """紧凑模式：从连续画面里挖掉低分水词短句。

    只动"短、低分、不在开头两句/结尾三句"的节拍，且不连着挖。
    默认 rt=0（一句不挖，完全连续），这是"顺接剧情"该有的样子。
    """
    if rt <= 0.005 or len(win) < 10:
        return []
    span = max(1.0, end_t - base)
    budget = span * rt
    med = sorted(b["score"] for b in win)[len(win) // 2]
    guard = {b["id"] for b in win[:2]} | {b["id"] for b in win[-3:]}
    pool = [b for b in win if b["id"] not in guard
            and b["dur"] <= 6.0 and b["score"] <= med + 0.4]
    pool.sort(key=lambda b: b["score"])
    if rng:
        head = pool[:max(3, len(pool) // 2)]
        rng.shuffle(head)
        pool = head + pool[len(head):]
    drop: list[dict] = []
    got = 0.0
    for b in pool:
        if got >= budget:
            break
        if got + b["dur"] > budget + 2.5:
            continue
        if any(abs(b["g0"] - d["g1"]) < 1.2 or abs(d["g0"] - b["g1"]) < 1.2
               for d in drop):
            continue
        drop.append(b)
        got += b["dur"]
    return drop


def _vary_cuts(win: list[dict], base: float, end_t: float,
               rng: random.Random, n: int = 2, lo_gap: float = 6.0) -> list[dict]:
    """批量差异化：挑一两处较长空镜，各剪掉一两秒。

    只在台词之间的空白里动手——改的是节奏，不动剧情；画面会有细微跳变，
    正是"同开头不同成片"要的效果。字幕密的剧找不到 6 秒空镜时会用更小的
    lo_gap 再试一轮（见 build 里的兜底），保证至少剪出一处不同。
    """
    gaps: list[tuple[float, float]] = []
    cur = base
    for b in win:
        if b["g0"] - cur >= lo_gap:
            gaps.append((cur, b["g0"]))
        cur = max(cur, b["g1"])
    if end_t - cur >= lo_gap:
        gaps.append((cur, end_t))
    if not gaps:
        return []
    out: list[dict] = []
    for a, b in rng.sample(gaps, min(n, len(gaps))):
        if rng.random() < 0.3 and lo_gap >= 6.0:
            continue
        cut = min(b - a - 1.5, rng.uniform(0.8, 2.6))
        if cut <= 0.5:
            continue
        out.append({"g0": round(a, 3), "g1": round(a + cut, 3), "text": "",
                    "hits": [], "score": 0.0})
    return out


def _decide_end(proj: dict, beats: list[dict], base: float, t0: float,
                body_budget: float, end_mode: str, end_hook: dict | None,
                end_ep: int | None, rng: random.Random | None,
                pick_idx: int = 0
                ) -> tuple[float, str, list[dict], bool]:
    """定结尾：指定 > 自动留悬念（批量时按 pick_idx 轮换次优结尾）。

    自动模式优先停在"没讲完"的卡点（未答问题 / 未落动作 / 将揭身份），
    集末只在本身带悬念时轻微优先；收束句扣分，不会为了完整结局硬拉时长。
    返回 (结束时间, 理由, 候选列表, 是否用了指定点)。指定点离目标时长太远时
    会被忽略并退回自动，第四项为 False，上层据此给用户一句提示。

    pick_idx 是批量差异化的关键：第 2 条取第 2 优候选、第 3 条取第 3 优……
    轮换而不是加权随机——随机大多落回 top1，两条成片会一模一样。
    """
    total = proj["total"]
    cands = _end_candidates(proj, beats, base, t0, body_budget)
    if end_mode == "point" and end_hook and end_hook.get("g1", 0) > base + MIN_TAIL:
        g = min(total, float(end_hook["g1"]))
        # 指定的结尾不能离目标时长太远，否则成片会突然长出/短掉一大截
        if g <= t0 + max(25.0, body_budget * 0.15):
            return g, "按指定的结尾钩子收尾", cands, True
    if end_mode == "ep_end" and end_ep:
        g = _ep_end_g(proj, beats, int(end_ep))
        if g > base + MIN_TAIL and g <= t0 + max(30.0, body_budget * 0.18):
            return g, f"卡在第 {int(end_ep)} 集结尾", cands, True

    if not cands:
        return t0, "按目标时长收尾", [], False
    # 两层选择：目标 ±12% 内如果有真悬念（open≥1.5），优先它；
    # 没有才按总分选最贴合的自然剧情点，避免为了卡准时长落回普通收束。
    hooks = [c for c in cands if c.get("hook_ok")]
    first = (max(hooks, key=lambda c: (float(c.get("open") or 0.0),
                                       float(c.get("score") or 0.0)))
             if hooks else cands[0])
    if pick_idx > 0 and len(cands) > 1:
        # 轮换到第 pick_idx 优的"替代结尾"，但替代者也得贴着目标时长——
        # 否则第 4 条会轮到一个 190s 的结尾，目标 150s 直接超 27%
        rest = [c for c in cands if c is not first]
        dev_cap = max(12.0, body_budget * 0.12)
        alts = [c for c in rest if abs(c["elapsed"] - body_budget) <= dev_cap]
        if not alts:
            # 一个贴合的替代都没有：挑偏差最小的两个替代，别硬上离谱的
            alts = sorted(rest, key=lambda c: abs(c["elapsed"] - body_budget))[:2]
        pick = alts[(pick_idx - 1) % len(alts)]
    else:
        pick = first        # 标准方案：有真悬念优先悬念，否则最优自然点
    return pick["g1"], pick["reason"], cands, False


def _finalize(proj: dict, hook_seg: dict | None, segs: list[dict],
              end_reason: str, meta: dict) -> dict:
    for s in segs:
        s.setdefault("kind", "body")
        s.setdefault("keep", True)
    if segs:
        segs[-1]["kind"] = "end_hook"
    ordered = ([hook_seg] if hook_seg else []) + segs
    if not ordered:
        raise RuntimeError("这个开头后面没有可用剧情了，换个更靠前的开头试试")

    for s in ordered:
        ep = proj_mod.ep_by_idx(proj, s["ep"])
        s["start"] = round(_clamp(ep, s["g0"] - ep["offset"]), 3)
        s["end"] = round(_clamp(ep, s["g1"] - ep["offset"]), 3)
        s["dur"] = round(max(0.2, s["end"] - s["start"]), 3)
        s["file"] = ep["file"]
        s["ep_idx"] = s["ep"]
    for i, s in enumerate(ordered):
        s["id"] = i

    body = [s for s in ordered if s.get("kind") != "hook"]
    est = sum(s["dur"] for s in ordered)
    first_g0 = ordered[0]["g0"]
    last_g1 = ordered[-1]["g1"]
    f_ep, f_t = proj_mod.loc(proj, first_g0)
    t_ep, t_t = proj_mod.loc(proj, max(first_g0, last_g1 - 0.05))
    cov: dict[int, float] = {}
    for s in body:
        cov[s["ep"]] = round(cov.get(s["ep"], 0.0) + s["dur"], 1)
    drop_ids = sorted(set(meta.get("drop_ids", [])))
    em = meta.get("end_meta") or {}
    return {
        "segments": ordered,
        "beats_keep": sorted({b for s in segs for b in s["beats"]}),
        "beats_drop": drop_ids,
        "beats_win": sorted(set(meta.get("win_ids", []))),
        "hook": hook_seg,
        "end": {
            "g0": ordered[-1]["g0"], "g1": ordered[-1]["g1"],
            "ep": ordered[-1]["ep"], "dur": ordered[-1]["dur"],
            "text": ordered[-1]["text"], "reason": end_reason,
            "hits": ordered[-1].get("hits", []),
            "open_loop": round(float(em.get("open") or 0.0), 2),
            "signals": list(em.get("signals") or []),
            "hook_type": em.get("hook_type", ""),
            "closed": bool(em.get("closed")),
        },
        "target": round(float(meta.get("target", 0.0)), 1),
        "estimated": round(est, 1),
        "kept_ratio": round(est / max(1.0, proj["total"]), 4),
        "kept": len(segs), "dropped": len(drop_ids),
        "mode": "linear",
        "end_reason": end_reason,
        "from": {"ep": f_ep, "t": round(f_t, 1), "g0": round(first_g0, 3)},
        "to": {"ep": t_ep, "t": round(t_t, 1), "g1": round(last_g1, 3)},
        "ep_span": [min(cov) if cov else f_ep, max(cov) if cov else t_ep],
        "coverage": [{"ep": k, "keep": v} for k, v in sorted(cov.items())],
        "shortage": bool(meta.get("shortage", False)),
        "tightness": round(float(meta.get("tightness", 0.0)), 3),
        "hook_lead": round(float(meta.get("lead", 0.0)), 2),
        "end_cands": meta.get("end_cands", [])[:8],
        "end_forced": bool(meta.get("end_forced", False)),
        "end_note": meta.get("end_note", ""),
        "tail_mode": meta.get("tail_mode", "auto"),
        "tail_cuts": meta.get("tail_cuts", []),
        "tail_total": round(sum(c["sec"] for c in meta.get("tail_cuts", [])), 2),
    }


def build(proj: dict, beats: list[dict], target: float = 360.0,
          hook: dict | None = None, acts: int = 0,
          end_hook: dict | None = None, seed: int | None = None,
          jitter: float = 0.0, end_mode: str = "auto",
          tightness: float = 0.0, end_ep: int | None = None,
          vary: int = 0, lead: float = 0.0,
          tail_mode: str = "auto", tail_fixed: float = 2.0) -> dict:
    """从开头钩子往后顺剪出一条成片。

    target:    目标时长（秒），不含片头卡与片尾
    hook:      {"g0","g1"} 开头钩子；None = 从第 1 集开头顺剪
    end_hook:  {"g0","g1"} 指定结尾点（end_mode="point" 时生效）
    end_mode:  auto 自动留悬念（未答问题 / 未落动作 / 将揭身份）/
               ep_end 卡指定集末 / point 用指定结尾钩子
    tightness: 紧凑程度，从连续画面里挖掉低分短句的比例上限（0 = 完全连续）
    seed+jitter: 节奏上的随机抖动（挖空镜、紧凑比例微调）
    vary:      批量差异化的"第几条"（0=标准方案）。>0 时结尾强制轮换到第
               vary 优候选、空镜兜底放宽到 2.5 秒——保证同一开头出多条时
               条条不同，而不是靠随机碰运气（随机大多落回同一个结尾）。
    lead:      开头入戏（秒）：钩子起点再往前切这么多，别错过比台词早出现的动作
    tail_mode: 去尾帧：auto 自动识别平台片尾定帧（识别不到不裁）/
               fixed 统一裁固定秒数 / off 不裁。只切集与集的接缝
    tail_fixed: tail_mode="fixed" 时的秒数
    """
    if not beats:
        raise RuntimeError("没有读到台词，无法做剧情顺剪。请检查字幕文件是否配对成功")

    lead = max(0.0, min(5.0, float(lead or 0.0)))
    rng = random.Random(seed) if (seed is not None and (jitter > 0 or vary > 0)) else None
    rt = max(0.0, min(0.3, float(tightness)))
    if rng:
        rt = max(0.0, min(0.32, rt + rng.uniform(-0.03, 0.03)))

    hook_seg = _hook_seg(proj, beats, hook, lead)
    hset = set(hook_seg["beats"]) if hook_seg else set()
    pool = [b for b in beats if b["id"] not in hset]
    base = hook_seg["g1"] if hook_seg else 0.0
    hook_dur = hook_seg["dur"] if hook_seg else 0.0
    body_budget = max(20.0, target - hook_dur)
    total = proj["total"]
    tail_avail = max(0.0, total - base)
    shortage = tail_avail < body_budget * 0.95
    if shortage:
        body_budget = max(MIN_TAIL, min(body_budget, tail_avail))

    # 1) 先把窗口推到"时长差不多够"的位置（连续播放，跨度就是时长）
    span = min(body_budget / max(0.7, 1.0 - rt * 0.85), max(1.0, tail_avail))
    t0 = _walk(pool, base, span)

    # 2) 在这个位置附近挑最好的结束点（vary>0 时轮换次优结尾）
    end_t, reason, cands, forced = _decide_end(proj, beats, base, t0, body_budget,
                                               end_mode, end_hook, end_ep, rng,
                                               pick_idx=max(0, int(vary)))
    end_t = max(base + MIN_TAIL, min(total, end_t))
    end_meta = next((c for c in cands if abs(c["g1"] - end_t) <= 0.05), {})
    note = ""
    if end_mode == "point" and not forced and end_hook:
        note = "指定的结尾离目标时长太远，已改回自动择优"

    # 3) 最终窗口 → 挖减 → 切连续块
    win = [b for b in pool if base - 0.02 <= b["g1"] <= end_t + 0.02]
    if not win:
        raise RuntimeError(
            f"这个开头（第 {proj_mod.loc(proj, base)[0]} 集）后面已经没有剧情了，"
            "换一个更靠前的开头试试")
    drops: list[dict] = _trim(win, base, end_t, rt, rng)
    if rng:
        vc = _vary_cuts(win, base, end_t, rng)
        if not vc and vary > 0:
            # 字幕密的剧找不到 6 秒空镜：放宽到 2.5 秒再试，
            # 保证同开头的多条至少在节奏上不一样
            vc = _vary_cuts(win, base, end_t, rng, n=3, lo_gap=2.5)
        drops += vc
    segs = _blocks(proj, base, end_t, drops, win)
    tail_cuts = _apply_tail_trim(proj, segs, tail_mode, tail_fixed, beats)

    out = _finalize(proj, hook_seg, segs, reason, {
        "target": target, "shortage": shortage, "tightness": rt,
        "end_cands": cands, "end_meta": end_meta,
        "end_forced": forced, "end_note": note,
        "lead": lead, "tail_mode": tail_mode, "tail_cuts": tail_cuts,
        "drop_ids": [b["id"] for b in drops if "id" in b],
        "win_ids": [b["id"] for b in win],
    })
    out["end_style"] = "forced" if forced else "open_loop"
    # 太靠后的开头只能顺出残片，直接说清楚比出一段 3 秒的片子好
    floor = max(30.0, min(90.0, target * 0.45))
    if out["estimated"] < floor:
        ep0 = proj_mod.loc(proj, base)[0]
        raise RuntimeError(
            f"这个开头太靠后了：第 {ep0} 集这段之后只剩 {int(out['estimated'])} 秒剧情，"
            f"凑不满 {int(target)} 秒。换一个前几集的开头，或者把目标时长改短")
    return out


def from_keep(proj: dict, beats: list[dict], drop_ids: list[int],
              hook: dict | None, target: float = 360.0,
              end_hook: dict | None = None, end_t: float | None = None,
              lead: float = 0.0, tail_mode: str = "auto",
              tail_fixed: float = 2.0, **kw) -> dict:
    """用户在时间轴上点掉某些台词后，重切连续块（被点掉的地方断开）。"""
    hook_seg = _hook_seg(proj, beats, hook, lead)
    hset = set(hook_seg["beats"]) if hook_seg else set()
    dset = {int(i) for i in drop_ids if int(i) not in hset}
    drops = [b for b in beats if b["id"] in dset]
    base = hook_seg["g1"] if hook_seg else 0.0
    if end_t is None:
        end_t = max(base + MIN_TAIL, base + max(MIN_TAIL, target))
    end_t = max(base + MIN_TAIL, min(proj["total"], float(end_t)))
    win = [b for b in beats if base - 0.02 <= b["g1"] <= end_t + 0.02 and b["id"] not in hset]
    segs = _blocks(proj, base, end_t, drops, win)
    tail_cuts = _apply_tail_trim(proj, segs, tail_mode, tail_fixed, beats)
    out = _finalize(proj, hook_seg, segs, "手动调整后的结尾",
                    {"target": target, "drop_ids": sorted(dset),
                     "lead": lead, "tail_mode": tail_mode, "tail_cuts": tail_cuts,
                     "win_ids": [b["id"] for b in win]})
    out["from_keep"] = True
    out["end_style"] = "manual"
    return out


def restat(proj: dict, plan: dict) -> dict:
    """界面切换 keep 之后重算时长。"""
    total = sum(s["dur"] for s in plan["segments"] if s.get("keep", True))
    kept = sum(1 for s in plan["segments"] if s.get("keep", True))
    plan["estimated"] = round(total, 1)
    plan["kept_ratio"] = round(total / max(1.0, proj["total"]), 4)
    plan["kept"] = kept
    plan["dropped"] = len(plan["segments"]) - kept
    return plan

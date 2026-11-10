"""顺剪规划：从选定的高光开头起，顺着剧情一路往下接，卡在最抓人的地方收尾。

以前的做法是"全剧切幕、每幕按冲突分挑台词"，结果是把 40 集剪成几十段碎镜头，
观众根本接不上前因后果。现在的逻辑只有一条线：

    开头钩子 → 第 1 集这段往后 → 第 2 集 → 第 3 集 → …… 直到时长够
             → 停在一个"好结尾"（集末卡点 / 悬念留扣 / 剧终）
             → 追更引导卡 → 引导尾帧

关键点：
1. 正片是**连续播放**的——窗口内每一集从头到尾整段保留，不按台词密度挑镜头。
   字幕稀疏的剧里，两句台词之间那 20 秒空白往往正是剧情在演，剪掉就断了。
2. "停在哪"才需要挑：在时长目标附近给每个可能的结束点打分——集末天然是短剧
   埋钩子的位置（大加分），句子本身带悬念词/没回答的问句也加分，再减去离目标
   时长的偏差。分最高的就是成片结尾，后面的内容全部不要。
3. 只有用户手动点了某句"不要这句"，或开了紧凑模式，才会从连续画面里挖掉一段。
4. 批量出片时同一开头的多条会换一个结尾候选、换几处较长的空镜来剪，
   结构一致但画面确实不同。
"""
from __future__ import annotations

import random

from . import analyze as az
from . import tailcard
from . import project as proj_mod

MIN_TAIL = 8.0      # 结尾最少这么长，太短就是"刚开头就结束"
MIN_BLOCK = 0.35    # 小于这个长度的碎块直接丢掉


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


def _end_candidates(proj: dict, beats: list[dict], base: float,
                    t0: float, body_budget: float) -> list[dict]:
    """在"时长差不多够了"的位置附近，给每个可能的结束点打分。

    分数构成：
      · 集末卡点 —— 一集的最后结尾是短剧本来就埋好的钩子，优先级最高（大加分，
        且时长差一点也从宽：想更准调目标时长就行）
      · 悬念词 / 没回答的问句 —— 观众正好奇着，剪在这里最想追
      · 时长贴合 —— 离目标时长每差 9 秒扣 1 分（卡集末时减半计）
      · 后面还有剧 —— 有后续才有"追更"的意义，剧终剪法反而扣分
    只在节拍结束点取值，保证不会掐在半句话上。
    """
    eps = {e["idx"]: e for e in proj["episodes"]}
    last_of: dict[int, dict] = {}
    for b in beats:
        last_of[b["ep"]] = b
    total = proj["total"]
    lo = base + max(MIN_TAIL, body_budget * 0.62)
    hi = t0 + max(40.0, min(150.0, body_budget * 0.28))

    raw: list[dict] = []
    for i, b in enumerate(beats):
        g1 = b["g1"]
        if g1 < lo or g1 > hi or g1 > total + 0.5:
            continue
        tail = beats[max(0, i - 1):i + 1]
        text = " ".join(x["text"] for x in tail).strip()
        susp, hits = az.suspense_of(text)
        ep = eps.get(b["ep"])
        off_end = (ep["offset"] + ep["dur"] - g1) if ep else 99.0
        is_last = last_of.get(b["ep"]) is b
        elapsed = g1 - base
        dev = abs(elapsed - body_budget) / max(1.0, body_budget)
        if is_last:
            bonus = 12.0 if off_end <= 2.5 else 9.0
            # 集末优先，但离目标时长太远（>22%）也压一压，
            # 别让 6 分钟的目标被一个早到的集末硬拉成 4 分钟
            if dev > 0.22:
                bonus *= max(0.30, 1.0 - (dev - 0.22) * 1.8)
        elif off_end <= 6.0:
            bonus = 4.5
        elif off_end <= 12.0:
            bonus = 2.0
        else:
            bonus = -1.2        # 掐在集中间，观众能感觉到"被切了"
        fit = -abs(elapsed - body_budget) / 7.0
        if elapsed < body_budget * 0.85:
            fit -= 2.5          # 明显短于目标：观众会觉得"这就完了？"
        if is_last:
            fit *= 0.45         # 卡集末优先：时长差一点从宽
        more = 3.0 if g1 < total - 3.0 else -4.0
        score = (susp * 1.15 + bonus + fit + more
                 + sum(x["score"] for x in tail) * 0.22)
        raw.append({
            "g0": round(tail[0]["g0"], 3), "g1": round(g1, 3), "ep": b["ep"],
            "score": round(score, 2), "susp": susp, "hits": hits[:4],
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
    if c["g1"] >= total - 3.0:
        return f"停在第 {c['ep']} 集剧终"
    if c["at_ep_end"] and c["off_end"] <= 2.5:
        return f"卡在第 {c['ep']} 集结尾"
    if c["hits"]:
        return f"停在第 {c['ep']} 集「{'、'.join(c['hits'][:2])}」"
    if c["off_end"] <= 6.0:
        return f"卡在第 {c['ep']} 集最后一段"
    return f"停在第 {c['ep']} 集剧情卡点"


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
        if beats:
            # 卡片总是压在没有台词的尾巴上；万一兜底秒数会切进最后一句台词，就停在那里
            tail_beats = [b for b in beats
                          if b["ep"] == s["ep"] and s["g0"] < b["g1"] <= s["g1"] + 0.05]
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
    """定结尾：指定 > 自动择优（批量时按 pick_idx 轮换次优结尾）。

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
    if pick_idx > 0 and len(cands) > 1:
        # 轮换到第 pick_idx 优的"替代结尾"，但替代者也得贴着目标时长——
        # 否则第 4 条会轮到一个 190s 的结尾，目标 150s 直接超 27%
        dev_cap = max(12.0, body_budget * 0.12)
        alts = [c for c in cands[1:] if abs(c["elapsed"] - body_budget) <= dev_cap]
        if not alts:
            # 一个贴合的替代都没有：挑偏差最小的两个替代，别硬上离谱的
            alts = sorted(cands[1:], key=lambda c: abs(c["elapsed"] - body_budget))[:2]
        pick = alts[(pick_idx - 1) % len(alts)]
    else:
        pick = cands[0]     # 标准方案永远用最优候选，可预期、可复现
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
    end_mode:  auto 自动择优 / ep_end 卡指定集末 / point 用指定结尾钩子
    tightness: 紧凑程度，从连续画面里挖掉低分短句的比例上限（0 = 完全连续）
    seed+jitter: 节奏上的随机抖动（挖空镜、紧凑比例微调）
    vary:      批量差异化的"第几条"（0=标准方案）。>0 时结尾强制轮换到第
               vary 优候选、空镜兜底放宽到 2.5 秒——保证同一开头出多条时
               条条不同，而不是靠随机碰运气（随机大多落回同一个结尾）。
    lead:      开头入戏（秒）：钩子起点再往前切这么多，别错过比台词早出现的动作
    tail_mode: 去尾帧：auto 自动识别平台片尾卡（识别不到按 0.8 秒兜底）/
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
        "end_cands": cands, "end_forced": forced, "end_note": note,
        "lead": lead, "tail_mode": tail_mode, "tail_cuts": tail_cuts,
        "drop_ids": [b["id"] for b in drops if "id" in b],
        "win_ids": [b["id"] for b in win],
    })
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

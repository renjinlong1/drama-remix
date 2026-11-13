"""自动混剪：按目标主题 / 时长 / 节奏风格，从多模态融合单元里挑片段组成片。

和「顺剪」的区别：顺剪是从一个开头往后连续播，这里是在整部剧里**挑着剪**，
所以三件事必须额外做对，否则就是一堆跳主题、忽快忽慢、硬切的碎片：

  主题统一   目标主题做硬约束；自由文本走词库 + 字面 + n-gram 相关度，
             匹配不到就明确报「主题冲突」，不拿不相干的片段凑数
  节奏一致   节奏用**素材内部分位**衡量（不是绝对分数，慢剧也有自己的快段），
             按风格挑对应分位的片段；素材本身没有节奏差异就报「节奏不匹配」
  叙事衔接   只按 (集号, 时间) 往前走，不倒叙；相邻片段算转场代价
             （主题 / 情绪 / 节奏 / 画面亮度色温 / 声音能量 / 跨集跨度），
             代价高的跳过或加短淡入淡出；切点永远落在台词边界上

另外有关键信息保底：全局得分最高的 N 段优先入选，避免"为了节奏顺把爆点全剪没了"。
所有失败路径都返回结构化反馈，前端能直接显示原因和可操作建议。
"""
from __future__ import annotations

import math
from collections import Counter

from . import multimodal as mm

STYLES: dict[str, dict] = {
    "calm":   {"label": "舒缓叙事", "rank": 0.25, "tol": 0.42,
               "min_len": 8.0, "max_len": 16.0, "fade": 0.22},
    "steady": {"label": "均衡推进", "rank": 0.50, "tol": 0.40,
               "min_len": 6.0, "max_len": 12.0, "fade": 0.14},
    "tight":  {"label": "紧凑快剪", "rank": 0.74, "tol": 0.36,
               "min_len": 4.0, "max_len": 9.0, "fade": 0.06},
    "burst":  {"label": "高能爆发", "rank": 0.90, "tol": 0.32,
               "min_len": 3.0, "max_len": 7.0, "fade": 0.0},
}
TOPIC_THRESH = 0.34
MIN_FILL = 0.75        # 可用素材少于目标的 75% 直接判「素材不足」
LOOKAHEAD = 7
MIN_SEG = 1.6


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _fb(code: str, title: str, msg: str, hints: list[str],
        metrics: dict, ok: bool = False) -> dict:
    return {"ok": ok, "code": code, "level": "success" if ok else "error",
            "title": title, "msg": msg, "hints": [h for h in hints if h],
            "metrics": metrics}


def _percentile_ranks(values: list[float]) -> list[float]:
    """同分并列取平均名次，避免一批一模一样的节奏值被强行排出高低。"""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.5] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        mid = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = mid
        i = j + 1
    return ranks


def _topic_vec(u: dict) -> list[float]:
    return [u.get("topics", {}).get(k, 0.0) for k in mm.TOPIC_ORDER]


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / max(1e-6, na * nb)


def _scene(c: dict, key: str, default: float = 0.0) -> float:
    return float((c.get("scene") or {}).get(key, default) or 0.0)


def _rhythm(c: dict, key: str, default: float = 0.0) -> float:
    return float((c.get("rhythm") or {}).get(key, default) or 0.0)


def transition_cost(a: dict, b: dict) -> float:
    """相邻两段的「生硬程度」：主题、情绪、节奏、画面、声音、跨集跨度。"""
    sim = _cos(_topic_vec(a), _topic_vec(b))
    topic = 0.28 * (1.0 - sim)
    emo_a = (a.get("emotion") or {}).get("label", "")
    emo_b = (b.get("emotion") or {}).get("label", "")
    emo = 0.0 if emo_a == emo_b else 0.10
    pace = 0.26 * min(1.0, abs(_rhythm(a, "pace") - _rhythm(b, "pace")) * 3.0)
    visual = 0.18 * min(1.0,
                        abs(_scene(a, "bright") - _scene(b, "bright")) * 1.6
                        + abs(_scene(a, "warm") - _scene(b, "warm")) * 1.4
                        + abs(_scene(a, "color") - _scene(b, "color")) * 0.8)
    energy = 0.12 * abs(_rhythm(a, "energy") - _rhythm(b, "energy"))
    ep_gap = 0.08 if int(b.get("ep", 0)) - int(a.get("ep", 0)) >= 3 else 0.0
    time_gap = 0.06 if (a.get("ep") == b.get("ep")
                        and b.get("g0", 0) - a.get("g1", 0) > 45) else 0.0
    return round(min(1.0, topic + emo + pace + visual + energy + ep_gap + time_gap), 4)


def _clip_from(group: list[dict], style: dict, rank: float, relevance: float) -> dict:
    first, last = group[0], group[-1]
    dur = last["end"] - first["start"]
    tot = sum(u["dur"] for u in group) or 1.0

    def avg(fn) -> float:
        return sum(fn(u) * u["dur"] for u in group) / tot

    rhythm_fit = _clamp01(1.0 - abs(rank - style["rank"]) / style["tol"])
    key = max(u["key"]["score"] for u in group)
    conf = avg(lambda u: u["confidence"])
    score = (0.36 * relevance + 0.24 * key + 0.20 * rhythm_fit
             + 0.12 * conf + 0.08 * avg(lambda u: u["rhythm"]["pace"]))
    topics = {k: round(avg(lambda u, _k=k: u["topics"].get(_k, 0.0)), 3)
              for k in mm.TOPIC_ORDER}
    emo = Counter(u["emotion"]["label"] for u in group).most_common(1)[0][0]
    return {
        "ids": [u["id"] for u in group], "ep": first["ep"],
        "start": round(first["start"], 3), "end": round(last["end"], 3),
        "g0": first["g0"], "g1": last["g1"], "dur": round(dur, 3),
        "text": " ".join(u["text"] for u in group)[:260],
        "topic": max(topics, key=lambda k: topics[k]), "topics": topics,
        "emotion": {"label": emo},
        "scene": {"label": first["scene"]["label"],
                  "bright": round(avg(lambda u: u["scene"]["bright"]), 3),
                  "warm": round(avg(lambda u: u["scene"]["warm"]), 3),
                  "color": round(avg(lambda u: u["scene"]["color"]), 3),
                  "shots": sum(u["scene"]["shots"] for u in group)},
        "rhythm": {"pace": round(avg(lambda u: u["rhythm"]["pace"]), 3),
                   "energy": round(avg(lambda u: u["rhythm"]["energy"]), 3),
                   "motion": round(avg(lambda u: u["rhythm"]["motion"]), 3)},
        "rank": round(rank, 3), "relevance": round(relevance, 3),
        "rhythm_fit": round(rhythm_fit, 3), "key": round(key, 3),
        "confidence": round(conf, 3), "score": round(score, 4),
        "beats": [b for u in group for b in u["beats"]],
        "evidence": [e for u in group for e in u["evidence"]][:8],
    }


def build(proj: dict, units: list[dict], *, topic: str = "", target: float = 180.0,
          style: str = "steady", transition: str = "auto",
          relax: bool = False) -> dict:
    """返回 {ok, feedback, plan, clips, topics, metrics}；plan 可直接送 render.render()。"""
    style = style if style in STYLES else "steady"
    st = STYLES[style]
    target_raw = float(target or 180.0)
    target = max(30.0, min(900.0, target_raw))
    clamp_note = (f"目标时长超出 30~900 秒范围，已按 {target:.0f} 秒处理"
                  if abs(target - target_raw) > 0.5 else "")
    topic = str(topic or "").strip()
    if not units:
        return {"ok": False, "plan": None, "feedback": _fb(
            "insufficient", "没有可识别的素材",
            "还没有多模态识别结果。先点「识别素材」，或重新导入带字幕 / 原声的剧集。",
            ["点「识别素材」重新扫描画面与声音", "确认剧集文件夹里有字幕或硬字幕"],
            {"units": 0, "available": 0.0, "target": target})}

    # ── 主题约束 ────────────────────────────────────────────────
    rel = [_clamp01(mm.query_match(topic, u)[0]) for u in units]
    matched = [i for i, r in enumerate(rel) if r >= TOPIC_THRESH]
    avail = sum(units[i]["dur"] for i in matched)
    detected = mm.summarize(units)["topics"][:4]
    base = {"units": len(units), "matched": len(matched),
            "available": round(avail, 1), "target": target,
            "detected_topics": detected}

    if topic and (not matched or max(rel) < 0.42) and not relax:
        names = "、".join(t["name"] for t in detected[:3]) or "（未识别到明确主题）"
        return {"ok": False, "plan": None, "feedback": _fb(
            "topic_conflict", "目标主题与素材冲突",
            f"素材里没有和「{topic}」相关的片段。识别到的主要主题：{names}。",
            [f"改用识别到的主题：{detected[0]['name']}" if detected else "",
             "清空目标主题，让工具按素材主导主题自动组片",
             "换一部剧，或先补做画面字幕识别（OCR）"], base)}
    if avail < target * MIN_FILL and not relax:
        more = round(avail / max(1.0, target) * 100)
        return {"ok": False, "plan": None, "feedback": _fb(
            "insufficient", "素材不足，凑不够目标时长",
            f"符合主题的可用片段只有 {avail:.0f} 秒，约为目标的 {more}%，硬凑会重复或跑题。",
            [f"把目标时长降到 {max(30, int(avail * 0.9 / 10) * 10)} 秒以内",
             "清空目标主题，用全部素材", "勾选「放宽限制」允许短片输出",
             "换一部剧 / 补做字幕识别"], base)}

    # ── 节奏约束：用素材内部分位，慢剧也能有自己的「快段」 ────────
    pool = [units[i] for i in matched] or units
    pool_rel = [rel[i] for i in matched] or [1.0] * len(units)
    paces = [u["rhythm"]["pace"] for u in pool]
    ranks = _percentile_ranks(paces)
    sp = sorted(paces)
    spread = sp[int(len(sp) * 0.9)] - sp[int(len(sp) * 0.1)] if len(sp) > 3 else 0.0
    style_fit = {k: round(sum(1 for r in ranks
                              if _clamp01(1.0 - abs(r - cfg["rank"]) / cfg["tol"]) >= 0.5)
                          / max(1, len(ranks)), 3) for k, cfg in STYLES.items()}
    best_style = max(style_fit, key=lambda k: style_fit[k])
    good = sum(1 for r in ranks
               if _clamp01(1.0 - abs(r - st["rank"]) / st["tol"]) >= 0.5)
    rmetrics = {**base, "pace_median": round(sp[len(sp) // 2], 3),
                "pace_spread": round(spread, 3), "style": style,
                "style_fit": style_fit, "best_style": best_style}
    need_fast = style in ("tight", "burst")
    mismatch = (need_fast and (spread < 0.10 or max(paces) < 0.34)
                and good < max(2, len(pool) // 4))
    if mismatch and not relax:
        return {"ok": False, "plan": None, "feedback": _fb(
            "rhythm_mismatch", "节奏风格与素材不匹配",
            f"素材节奏分布很平（快慢差 {spread:.2f}，最快 {max(paces):.2f}），"
            f"剪不出「{st['label']}」要的快节奏起伏；硬切只会变成一堆零碎短镜头。",
            [f"改用「{STYLES[best_style]['label']}」（贴合度 {style_fit[best_style]:.0%}）",
             "换节奏风格为舒缓 / 均衡", "勾选「放宽限制」按素材原有节奏输出"],
            rmetrics)}

    # ── 片段构建：相邻单元按风格长度合并，切点始终在台词边界 ──────
    id2rank = {u["id"]: ranks[i] for i, u in enumerate(pool)}
    id2rel = {u["id"]: pool_rel[i] for i, u in enumerate(pool)}
    clips: list[dict] = []
    group: list[dict] = []

    def flush(g: list[dict]):
        if not g:
            return
        clips.append(_clip_from(
            g, st,
            sum(id2rank[u["id"]] for u in g) / len(g),
            sum(id2rel[u["id"]] for u in g) / len(g)))

    for u in pool:
        if group and (u["ep"] != group[0]["ep"]
                      or u["end"] - group[0]["start"] > st["max_len"]):
            flush(group)
            group = []
        group.append(u)
        if u["end"] - group[0]["start"] >= st["min_len"]:
            flush(group)
            group = []
    flush(group)
    if not clips:
        return {"ok": False, "plan": None, "feedback": _fb(
            "insufficient", "没有可用的成片片段",
            "融合结果被主题 / 节奏条件全部过滤掉了。放宽主题或换一种节奏风格再试。",
            ["清空目标主题", "勾选「放宽限制」"], base)}

    # ── 关键信息保底 ────────────────────────────────────────────
    key_quota = max(2, min(8, int(target / 90) + 2))
    key_ids = {u["id"] for u in sorted(pool, key=lambda u: -u["key"]["score"])[:key_quota]}
    forced = {i for i, c in enumerate(clips) if key_ids & set(c["ids"])}

    # ── 顺序选择：只看前方 LOOKAHEAD 个片段，扣分转场代价 ─────────
    chosen: list[int] = []
    total = 0.0
    cursor = 0
    last: dict | None = None
    while cursor < len(clips) and total < target * 0.985:
        best_i, best_gain = -1, -1e9
        for i in range(cursor, min(len(clips), cursor + LOOKAHEAD)):
            c = clips[i]
            cost = transition_cost(last, c) if last else 0.0
            gain = c["score"] - 0.55 * cost
            if i in forced:
                gain += 0.30                        # 爆点优先
            if total + c["dur"] > target * 1.14:
                gain -= 0.35                        # 别一步冲过头
            if gain > best_gain:
                best_i, best_gain = i, gain
        if best_i < 0:
            break
        chosen.append(best_i)
        total += clips[best_i]["dur"]
        last = clips[best_i]
        cursor = best_i + 1
    for i in sorted(forced):
        if i not in chosen and total < target * 1.12:
            chosen.append(i)
            total += clips[i]["dur"]
    # 补时长：局部择优可能跳过一些片段，素材够就必须把目标时长填满，
    # 否则明明有 8 段可用却只输出 5 段。补进来的片段仍按叙事顺序排列。
    if total < target * 0.985:
        for i in range(len(clips)):
            if total >= target * 0.985:
                break
            if i not in chosen:
                chosen.append(i)
                total += clips[i]["dur"]
    chosen.sort()

    # ── 生成渲染片段，末段裁剪到目标时长 ─────────────────────────
    segs: list[dict] = []
    used: list[int] = []
    acc = 0.0
    for i in chosen:
        c = clips[i]
        dur = c["dur"]
        if acc + dur > target * 1.02:
            dur = round(target - acc, 3)
            if dur < MIN_SEG:
                continue
        segs.append({
            "id": len(segs), "ep": c["ep"], "start": c["start"],
            "end": round(c["start"] + dur, 3), "dur": round(dur, 3),
            "keep": True, "kind": "body", "beats": c["beats"],
            "text": c["text"], "topic": c["topic"],
            "emotion": c["emotion"]["label"], "scene": c["scene"]["label"],
            "pace": c["rhythm"]["pace"], "key": c["key"],
            "confidence": c["confidence"], "score": c["score"],
            "evidence": c["evidence"], "relevance": c["relevance"],
            "fade_in": 0.0, "fade_out": 0.0,
        })
        used.append(i)
        acc += dur
        if acc >= target * 0.985:
            break
    if not segs:
        return {"ok": False, "plan": None, "feedback": _fb(
            "insufficient", "选不出成片片段",
            "按当前主题与节奏条件挑不出足够长的片段。放宽条件或降低目标时长。",
            ["清空目标主题", "降低目标时长"], rmetrics)}

    # ── 转场：代价高的加短淡入淡出，代价低的保持硬切 ─────────────
    costs: list[float] = []
    for k in range(len(segs) - 1):
        cost = transition_cost(clips[used[k]], clips[used[k + 1]])
        costs.append(cost)
        if transition == "cut" or st["fade"] <= 0:
            continue
        if transition == "soft" or cost > 0.30:
            fd = round(min(st["fade"], min(segs[k]["dur"], segs[k + 1]["dur"]) / 5), 2)
            if fd >= 0.04:
                segs[k]["fade_out"] = fd
                segs[k + 1]["fade_in"] = fd

    # ── 反馈与统计 ──────────────────────────────────────────────
    estimated = round(sum(s["dur"] for s in segs), 1)
    fill = estimated / target
    # 关键信息保底的达成率：全局最关键的 N 段里，有多少真的进了成片
    key_kept = sum(1 for i in chosen if key_ids & set(clips[i]["ids"]))
    key_total = max(1, len(key_ids))
    topic_purity = round(sum(s["relevance"] for s in segs) / len(segs), 3)
    metrics = {
        "target": target, "estimated": estimated, "fill": round(fill, 3),
        "segments": len(segs), "candidates": len(clips),
        "topic_purity": topic_purity,
        "key_coverage": round(min(1.0, key_kept / key_total), 3),
        "pace_avg": round(sum(s["pace"] for s in segs) / len(segs), 3),
        "pace_spread": round(spread, 3),
        "transition_avg": round(sum(costs) / len(costs), 3) if costs else 0.0,
        "transition_max": round(max(costs), 3) if costs else 0.0,
        "confidence": round(sum(s["confidence"] for s in segs) / len(segs), 3),
        "style_fit": style_fit, "style": style, "detected_topics": detected,
    }
    warnings: list[str] = []
    if clamp_note:
        warnings.append(clamp_note)
    if topic and topic_purity < 0.60 and max(rel) >= 0.42:
        warnings.append(f"与「{topic}」强相关的片段不多（一致度 {topic_purity:.0%}），"
                        "已用近似片段补足，建议降低目标时长或清空主题")
    if fill < 0.92:
        warnings.append(f"素材只够 {estimated:.0f} 秒（目标 {target:.0f} 秒），成片偏短")
    if metrics["key_coverage"] < 0.5:
        warnings.append("高信息量片段保留偏少，可放宽节奏风格再看一遍")
    if metrics["transition_avg"] > 0.35:
        warnings.append("相邻片段差异偏大，已自动加淡入淡出缓冲")
    if mm_source_degraded(units):
        warnings.append("尚未做画面 / 声音深度识别，节奏与场景判断基于台词")
    feedback = {"ok": True, "code": "partial" if warnings else "ok",
                "level": "warning" if warnings else "success",
                "title": "方案已生成" + ("（有提示）" if warnings else ""),
                "msg": f"选出 {len(segs)} 段 / {estimated:.0f} 秒，主题一致度 "
                       f"{topic_purity:.0%}，平均转场代价 {metrics['transition_avg']:.2f}。",
                "hints": warnings, "metrics": metrics}
    topic_effective = (topic if topic else
                       (detected[0]["name"] if detected else "未指定"))
    plan = {
        "mode": "auto_remix", "segments": segs, "target": target,
        "estimated": estimated, "kept": len(segs),
        "kept_ratio": round(estimated / max(1.0, proj.get("total", 1)), 4),
        "topic": topic, "topic_effective": topic_effective,
        "style": style, "style_label": st["label"], "transition": transition,
        "metrics": metrics, "warnings": warnings,
        "coverage": [{"ep": e, "keep": round(sum(s["dur"] for s in segs if s["ep"] == e), 1)}
                     for e in sorted({s["ep"] for s in segs})],
        "from": {"ep": segs[0]["ep"], "t": segs[0]["start"]},
        "to": {"ep": segs[-1]["ep"], "t": segs[-1]["end"]},
        "ep_span": [min(s["ep"] for s in segs), max(s["ep"] for s in segs)],
    }
    return {"ok": True, "plan": plan, "clips": [clips[i] for i in used],
            "topics": detected, "feedback": feedback, "metrics": metrics}


def mm_source_degraded(units: list[dict]) -> bool:
    """融合单元里没有任何画面 / 声音信号 = 纯文本降级。"""
    if not units:
        return True
    return not any(u["modalities"]["visual"] or u["modalities"]["audio"] for u in units)

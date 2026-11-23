"""高光剪辑：整剧扫描高光片段 → 按目标时长选片 → 每段去尾帧 → 拼成一条集锦。

和「顺剪」「自动混剪」的区别：
  · 顺剪：选一个开头，从那里**连续**往后播到目标时长；
  · 自动混剪：按主题 / 节奏在多模态单元里挑片段；
  · 高光剪辑：不设主题，直接拿整部剧的冲突 / 动作 / 反转高光，
    按目标时长选够条数，按叙事顺序拼成一条，**每段末尾都去掉定帧尾帧**。

规则（导出前由 build 写进 plan，render 只按 plan 执行）：
1. 片段只来自本剧高光候选池，同集不重叠、间隔 ≥ GAP，每集最多 PER_EP 段；
2. 总时长贴近 target：< 95% 标 shortage，> 102% 不允许（选片时已卡住）；
3. 结尾可 auto / point（指定候选 key）/ custom（自定义集数与起止秒），
   指定的结尾片段永远排在成片最后一段；
4. 每段末尾用 tailcard 检测定帧并裁掉（auto），也可 fixed / off；
5. 按 max_size_mb（默认且最大 400）反推视频码率，render 用 b:v + maxrate
   编码，超限时再降码率重压，保证文件不超过上限。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

from . import analyze as az
from . import project as proj_mod
from . import tailcard

MIN_SEG = 2.5          # 单段最短，太短一闪而过
MAX_SEG = 20.0         # 单段最长，超过就截断（保留开头冲击）
PER_EP = 4             # 每集最多取几段
GAP = 6.0              # 同集两段之间至少隔这么久
TAIL_WORKERS = 4       # 尾帧检测并发（每段一次轻量抽帧）
MAX_SIZE_MB = 400.0    # 导出文件硬上限
AUDIO_KBPS = 96        # 集锦统一音频码率
MIN_VIDEO_KBPS = 300   # 再低就没法看了，低于此值直接判目标不可达


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _key(ep: int, g0: float) -> str:
    return f"{int(ep)}@{float(g0):.1f}"


def _overlap(a: dict, b: dict, gap: float = 0.0) -> bool:
    """两段是否重叠（同集才算）；gap>0 时连"挨得太近"也算。"""
    if int(a["ep"]) != int(b["ep"]):
        return False
    return (a["g0"] < b["g1"] + gap) and (b["g0"] < a["g1"] + gap)


def _seg_from_c(proj: dict, c: dict) -> dict | None:
    """候选（全局秒）→ 渲染片段（集内 start/end）。"""
    try:
        ep = proj_mod.ep_by_idx(proj, int(c["ep"]))
    except KeyError:
        return None
    ep_lo = float(ep["offset"])
    ep_hi = ep_lo + float(ep["dur"])
    g0 = max(ep_lo + 0.02, float(c.get("g0", 0.0)))
    g1 = min(ep_hi - 0.02, float(c.get("g1", 0.0)))
    if g1 - g0 < MIN_SEG:
        return None
    if g1 - g0 > MAX_SEG:
        g1 = g0 + MAX_SEG
    return {
        "ep": int(ep["idx"]), "g0": round(g0, 3), "g1": round(g1, 3),
        "start": round(g0 - ep_lo, 3), "end": round(g1 - ep_lo, 3),
        "dur": round(g1 - g0, 3),
        "text": str(c.get("text") or "")[:220],
        "score": round(float(c.get("score") or 0.0), 2),
        "kind": "highlight", "hits": list(c.get("hits") or [])[:6],
        "tail_trim": 0.0, "tail_by": "none",
    }


def _pool(proj: dict, beats: list[dict], acts: list[dict]) -> list[dict]:
    """整剧高光候选池：先拿大池，再按集做"不重叠 + 间隔"清洗。

    find_highlights 原本是给"开头钩子"用的，默认只取 18-24 条；这里把
    top_k / min_keep 放大到 400，让每集都有足够候选，再自己按集限额。
    """
    visual = az.find_visual_highlights(proj, beats, acts or [])
    cands = az.find_highlights(
        beats, top_k=400, min_keep=400, extra_visual=visual,
        visual_reserve=8, type_quota=0, front_eps=0)
    by_ep: dict[int, list[dict]] = {}
    for c in cands:
        by_ep.setdefault(int(c["ep"]), []).append(c)
    out: list[dict] = []
    for ep, lst in by_ep.items():
        lst.sort(key=lambda c: (-float(c.get("score") or 0.0),
                                float(c.get("g0") or 0.0)))
        picked: list[dict] = []
        for c in lst:
            if any(_overlap(c, p, GAP) for p in picked):
                continue
            picked.append(c)
            if len(picked) >= PER_EP:
                break
        out.extend(picked)
    out.sort(key=lambda c: (int(c["ep"]), float(c["g0"])))
    return out


def _custom_seg(proj: dict, ep_no, start, end) -> tuple[dict | None, str]:
    try:
        ep = proj_mod.ep_by_idx(proj, int(ep_no))
    except (KeyError, TypeError, ValueError):
        return None, "自定义结尾的集数不对"
    try:
        a = max(0.0, float(start))
        b = min(float(ep["dur"]), float(end))
    except (TypeError, ValueError):
        return None, "自定义结尾的起止秒数不对"
    if b - a < MIN_SEG:
        return None, f"自定义结尾至少 {MIN_SEG:.1f} 秒"
    if b - a > MAX_SEG:
        b = a + MAX_SEG
    return {
        "ep": int(ep["idx"]), "g0": round(ep["offset"] + a, 3),
        "g1": round(ep["offset"] + b, 3),
        "start": round(a, 3), "end": round(b, 3),
        "dur": round(b - a, 3), "text": "自定义结尾", "score": 0.0,
        "kind": "highlight", "hits": [], "tail_trim": 0.0, "tail_by": "none",
    }, ""


def _trim_one(proj: dict, seg: dict, mode: str, fixed: float) -> dict:
    """给一段去尾帧；返回裁切记录。直接改 seg 的 end/g1/dur。"""
    try:
        ep = proj_mod.ep_by_idx(proj, int(seg["ep"]))
    except KeyError:
        return {"ep": seg["ep"], "sec": 0.0, "by": "none"}
    if mode == "off":
        return {"ep": seg["ep"], "sec": 0.0, "by": "off"}
    path = str(ep.get("path") or "")
    if seg["end"] >= float(ep["dur"]) - 0.35:
        sec, by = tailcard.trim_for(path, float(ep["dur"]), mode, fixed)
    else:
        sec, by = tailcard.trim_at(path, float(seg["end"]), mode, fixed)
    sec = min(float(sec), max(0.0, float(seg["dur"]) - MIN_SEG))
    if sec <= 0.05:
        return {"ep": seg["ep"], "sec": 0.0, "by": "none"}
    sec = round(sec, 3)
    seg["end"] = round(float(seg["end"]) - sec, 3)
    seg["g1"] = round(float(seg["g1"]) - sec, 3)
    seg["dur"] = round(float(seg["dur"]) - sec, 3)
    seg["tail_trim"] = sec
    seg["tail_by"] = by
    return {"ep": seg["ep"], "sec": sec, "by": by}


def _bitrate(duration: float, max_size_mb: float) -> dict:
    """按目标大小反推码率。留 48kbps 容器/封装余量，音频固定 96k。"""
    duration = max(1.0, float(duration))
    cap = _clamp(float(max_size_mb or MAX_SIZE_MB), 50.0, MAX_SIZE_MB)
    total_kbps = cap * 8192.0 / duration
    video = max(MIN_VIDEO_KBPS, int(round(total_kbps - AUDIO_KBPS - 48)))
    est = (video + AUDIO_KBPS) * duration / 8192.0
    return {
        "video_kbps": video, "audio_kbps": AUDIO_KBPS,
        "max_size_mb": round(cap, 1), "total_kbps": round(total_kbps, 1),
        "estimated_mb": round(est, 1), "within": est <= cap + 0.5,
    }


def _feedback(ok: bool, level: str, title: str, msg: str,
              hints: list[str] | None = None) -> dict:
    return {"ok": ok, "level": level, "title": title, "msg": msg,
            "hints": [h for h in (hints or []) if h]}


def build(proj: dict, beats: list[dict], acts: list[dict] | None = None, *,
          target: float = 600.0, end_mode: str = "auto",
          end_key: str = "", end_ep=None, end_start=None, end_end=None,
          tail_mode: str = "auto", tail_fixed: float = 0.5,
          max_size_mb: float = MAX_SIZE_MB, relax: bool = False) -> dict:
    """返回 {ok, plan, feedback, end_options, metrics}；plan 可直接送 render。"""
    target = _clamp(float(target or 600.0), 60.0, 900.0)
    tail_mode = tail_mode if tail_mode in ("auto", "fixed", "off") else "auto"
    end_mode = end_mode if end_mode in ("auto", "point", "custom") else "auto"
    if end_mode == "point" and not end_key:
        end_mode = "auto"            # 只切到"指定"但还没选，按自动走
    max_size_mb = _clamp(float(max_size_mb or MAX_SIZE_MB), 50.0, MAX_SIZE_MB)

    if not beats:
        return {"ok": False, "plan": None, "end_options": [],
                "feedback": _feedback(
                    False, "error", "没有可用的高光素材",
                    "这部剧还没有识别到台词 / 画面节拍，无法扫描高光。",
                    ["确认字幕已配对，或勾选画面硬字幕识别后重新扫描"])}

    pool = _pool(proj, beats, acts or [])
    if not pool:
        return {"ok": False, "plan": None, "end_options": [],
                "feedback": _feedback(
                    False, "error", "没有识别到高光片段",
                    "整剧没有达到门槛的冲突 / 动作 / 反转片段。",
                    ["换一部冲突更密集的剧", "确认字幕与画面识别已完成"])}

    cands: list[dict] = []
    for c in pool:
        s = _seg_from_c(proj, c)
        if s:
            s["_key"] = _key(s["ep"], s["g0"])
            cands.append(s)
    if not cands:
        return {"ok": False, "plan": None, "end_options": [],
                "feedback": _feedback(False, "error", "高光片段都太短",
                                      "候选片段不足 2.5 秒，拼不出可用的集锦。")}

    end_seg: dict | None = None
    end_note = ""
    if end_mode == "point" and end_key:
        end_seg = next((s for s in cands if s["_key"] == str(end_key)), None)
        if not end_seg:
            return {"ok": False, "plan": None, "end_options": [],
                    "feedback": _feedback(
                        False, "error", "指定的结尾片段不存在",
                        "这个结尾候选不在当前高光池里，可能已换剧或重新识别。",
                        ["改用自动结尾，或重新生成方案后再选"])}
        cands = [s for s in cands if s is not end_seg]
    elif end_mode == "custom":
        end_seg, err = _custom_seg(proj, end_ep, end_start, end_end)
        if not end_seg:
            return {"ok": False, "plan": None, "end_options": [],
                    "feedback": _feedback(False, "error", "自定义结尾不可用", err,
                                          ["换一段 2.5~20 秒、在本集时长内的片段"])}
        end_seg["_key"] = _key(end_seg["ep"], end_seg["g0"])
        cands = [s for s in cands if not _overlap(s, end_seg)]

    end_dur = float(end_seg["dur"]) if end_seg else 0.0
    if end_dur > target - MIN_SEG:
        return {"ok": False, "plan": None, "end_options": [],
                "feedback": _feedback(
                    False, "error", "结尾片段太长",
                    f"结尾片段 {end_dur:.1f} 秒，已经超过目标时长 {target:.0f} 秒。",
                    ["缩短结尾片段，或把目标时长调大"])}

    # ── 选片：高分优先，同集不重叠；给尾帧预留约 3% 余量 ──────────
    def pick(limit: float, exclude: set[str]) -> tuple[list[dict], float]:
        got: list[dict] = []
        total = 0.0
        for c in sorted(cands, key=lambda x: (-x["score"], x["ep"], x["g0"])):
            if c["_key"] in exclude:
                continue
            if any(_overlap(c, p) for p in got):
                continue
            if end_seg and _overlap(c, end_seg):
                continue
            if total + c["dur"] > limit:
                continue
            got.append(c)
            total += c["dur"]
            if total >= target * 0.98:
                break
        return got, total

    # 上限严格卡在目标的 102%：强制结尾本身也算在总时长里，不能先选满再拼结尾
    chosen, picked_dur = pick(target * 1.02 - end_dur, set())
    # 高分优先可能因为"下一段太长"提前停：再按时间顺序补一轮，别浪费素材
    if picked_dur < target * 0.95 - end_dur:
        for c in sorted(cands, key=lambda x: (x["ep"], x["g0"])):
            if c in chosen or c["_key"] in {s["_key"] for s in chosen}:
                continue
            if any(_overlap(c, p) for p in chosen):
                continue
            if end_seg and _overlap(c, end_seg):
                continue
            if picked_dur + c["dur"] > target * 1.02 - end_dur:
                continue
            chosen.append(c)
            picked_dur += c["dur"]
            if picked_dur >= target * 0.98 - end_dur:
                break

    allseg = chosen + ([end_seg] if end_seg else [])
    if not allseg:
        return {"ok": False, "plan": None, "end_options": [],
                "feedback": _feedback(False, "error", "选不出可用的高光片段",
                                      "目标时长太短，放不下任何一段高光。")}

    # ── 每段去尾帧（并发；同一集只读一次视频，但各自抽自己的末尾） ──
    with ThreadPoolExecutor(max_workers=TAIL_WORKERS) as ex:
        cuts = list(ex.map(lambda s: _trim_one(proj, s, tail_mode, tail_fixed),
                           allseg))

    def total_now() -> float:
        return sum(float(s["dur"]) for s in allseg)

    # 尾帧裁掉后不够长：从剩下的候选里补，补进来的也要去尾帧
    guard = 0
    while total_now() < target * 0.95 and guard < 24:
        guard += 1
        used = {s["_key"] for s in allseg}
        rest = [c for c in cands
                if c["_key"] not in used
                and not any(_overlap(c, p) for p in allseg)
                and not (end_seg and _overlap(c, end_seg))]
        if not rest:
            break
        rest.sort(key=lambda x: (-x["score"], x["ep"], x["g0"]))
        add = rest[0]
        _trim_one(proj, add, tail_mode, tail_fixed)
        chosen.append(add)
        allseg.append(add)
        cuts.append({"ep": add["ep"], "sec": add.get("tail_trim", 0.0),
                     "by": add.get("tail_by", "none")})

    body = sorted(chosen, key=lambda s: (s["ep"], s["g0"]))
    segs = body + ([end_seg] if end_seg else [])
    for i, s in enumerate(segs):
        s["id"] = i
        s["keep"] = True
        s["kind"] = "highlight"
        s.setdefault("fade_in", 0.0)
        s.setdefault("fade_out", 0.0)
        s["end_clip"] = bool(end_seg and s is end_seg)
        s.pop("_key", None)

    estimated = round(sum(s["dur"] for s in segs), 1)
    complete = estimated >= target * 0.95
    bit = _bitrate(estimated, max_size_mb)
    tail_cuts = [c for c in cuts if c.get("sec", 0) > 0]
    tail_total = round(sum(c["sec"] for c in tail_cuts), 2)
    fill = round(estimated / max(1.0, target), 3)

    end_reason = ("自动收尾（按叙事顺序最后一段）" if end_mode == "auto"
                  else f"指定第 {end_seg['ep']} 集高光片段收尾" if end_mode == "point"
                  else f"自定义第 {end_seg['ep']} 集 {end_seg['start']:.1f}-{end_seg['end']:.1f}s 收尾")
    if end_mode != "auto" and end_note:
        end_reason += "；" + end_note

    hints: list[str] = []
    if not complete:
        hints.append(f"整剧高光只够 {estimated:.0f} 秒（{len(segs)} 段），"
                     f"目标 {target:.0f} 秒；可降低目标时长或放宽限制")
    if tail_total > 0:
        hints.append(f"已从 {len(tail_cuts)} 段末尾去掉定帧，共 {tail_total:.1f} 秒")
    if not bit["within"]:
        hints.append(f"按 {max_size_mb:.0f} MB 上限算出的视频码率只有 "
                     f"{bit['video_kbps']} kbps，目标时长偏长，可能压不到上限内")

    feedback = _feedback(
        complete and bit["within"],
        "success" if complete and bit["within"] else "warning",
        "高光方案已生成" if complete and bit["within"] else "方案已生成（有提示）",
        f"共 {len(segs)} 段 / {estimated:.0f} 秒，目标 {target:.0f} 秒；"
        f"视频码率 {bit['video_kbps']} kbps，预计 {bit['estimated_mb']:.0f} MB"
        f"（上限 {max_size_mb:.0f} MB）。",
        hints)

    # 结尾候选：把用户当前指定的那一段也放回去，重新生成方案时下拉还能选中
    opt_src = list(cands)
    if end_seg:
        ek = _key(end_seg["ep"], end_seg["g0"])
        if all(_key(c["ep"], c["g0"]) != ek for c in opt_src):
            opt_src.append(end_seg)
    end_options = [
        {"key": _key(c["ep"], c["g0"]), "ep": c["ep"],
         "g0": c["g0"], "g1": c["g1"], "dur": c["dur"],
         "start": c.get("start", 0.0), "text": c.get("text", ""),
         "score": c.get("score", 0.0)}
        for c in sorted(opt_src, key=lambda x: (x["ep"], x["g0"]),
                        reverse=True)[:12]
    ]

    plan = {
        "mode": "highlight_cut", "segments": segs,
        "target": round(target, 1), "estimated": estimated,
        "clip_count": len(segs), "fill": fill, "complete": complete,
        "shortage": not complete,
        "end_mode": end_mode, "end_reason": end_reason,
        "end": {
            "ep": segs[-1]["ep"], "g0": segs[-1]["g0"], "g1": segs[-1]["g1"],
            "dur": segs[-1]["dur"], "text": segs[-1].get("text", ""),
            "reason": end_reason, "manual": end_mode != "auto",
        },
        "end_options": end_options,
        "tail_mode": tail_mode, "tail_total": tail_total,
        "tail_cuts": tail_cuts, "max_size_mb": round(max_size_mb, 1),
        "bitrate": bit,
        "pool_size": len(pool), "candidate_count": len(cands),
        "coverage": [{"ep": e, "keep": round(sum(s["dur"] for s in segs if s["ep"] == e), 1)}
                     for e in sorted({s["ep"] for s in segs})],
    }
    return {"ok": True, "plan": plan, "feedback": feedback,
            "end_options": end_options,
            "metrics": {"target": target, "estimated": estimated,
                        "clips": len(segs), "fill": fill,
                        "tail_total": tail_total, "bitrate": bit,
                        "pool": len(pool), "candidates": len(cands)}}

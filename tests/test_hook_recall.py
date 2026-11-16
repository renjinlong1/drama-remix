"""高光识别召回 / 标注准确率测试（带人工标注的合成剧集）。

为什么要用合成剧集：真实片子没有人工标注，只能看"像不像"；这里按短剧的
常见结构造 12 集、每集 6 条台词，把"哪一段是真正的高光"事先标好，就能算出
召回率、标注准确率，回归时能直接看到涨跌。

覆盖三类容易漏的高光：
- 集末 3 秒的短促狠话（以前整段被时长门槛丢掉）
- 台词在冲突 / 揭身份（配乐一直很响时，以前会被误标成"动作冲突"）
- 没有台词的打斗（靠画面/声音事件生成的独立候选）

用法：runtime\\python.exe tests\\test_hook_recall.py
输出：tests/results/hook_recall.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import analyze as A  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []

FILLER = [
    "我们走吧，今天先到这里",
    "你先坐下，我慢慢跟你说",
    "这件事以后再说吧",
    "天气不错，出去走走",
    "我明白了，你别担心",
    "好，那就这样定了",
]
DLG = [
    "你算什么东西，滚出去！",
    "我一巴掌扇死你，给我跪下道歉",
    "你不过是个废物，凭什么站在这里",
    "你这个贱人，竟然敢背叛我",
    "闭嘴！再说一句我就让你滚蛋",
    "滚出去，别再让我看见你",
]
REVEAL = [
    "我就是集团继承人，董事长的女儿",
    "当年被赶出家门的那个人，现在回来了",
    "真相就是：他才是少爷，你不是",
]
SHORT = ["滚出去！", "你给我跪下！", "你算什么东西？", "离婚！现在就离！"]


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def build_corpus() -> tuple[dict, list[dict]]:
    """20 集合成剧：返回 (项目, 人工标注的高光区间)。

    每集恰好一个"真正的高光"，按集分配类型（台词冲突 / 揭身份 / 集末短促狠话 /
    无台词打斗），这样 24 个候选名额足够覆盖全部标注，召回率才有可比性。

    背景配乐一直很响（每条台词都挂一个 score=3.4 的声音事件），
    模拟真实短剧里"整部剧音量都很大"的情况——旧的固定阈值会把
    所有候选都判成动作冲突。
    """
    episodes: list[dict] = []
    acts: list[dict] = []
    gt: list[dict] = []

    def kind_of(ep: int) -> str:
        if ep <= 6:
            return "dialog"
        if ep <= 11:
            return "reveal"
        if ep <= 15:
            return "short"
        return "visual"

    for ep in range(1, 21):
        offset = (ep - 1) * 90.0
        kind = kind_of(ep)
        plan: list[tuple[str, str]] = [("filler", FILLER[(ep + k) % len(FILLER)])
                                       for k in range(6)]
        idx = 5 if kind == "short" else (ep % 6)
        if kind == "dialog":
            plan[idx] = ("dialog", DLG[(ep - 1) % len(DLG)])
        elif kind == "reveal":
            plan[idx] = ("reveal", REVEAL[(ep - 7) % len(REVEAL)])
        elif kind == "short":
            plan[5] = ("short", SHORT[(ep - 12) % len(SHORT)])
        silent_at = idx if kind == "visual" else None
        cues: list[dict] = []
        cursor = 0.5
        for k, (text_kind, text) in enumerate(plan):
            if silent_at is not None and k == silent_at:
                # 一段 12 秒没有台词的打斗：独立视觉候选
                v0 = offset + cursor + 3.5
                v1 = v0 + 3.0
                acts.append({"ep": ep, "g0": round(v0, 2), "g1": round(v1, 2),
                             "v": 0.92, "a": 0.95, "score": 2.7})
                gt.append({"ep": ep, "g0": v0, "g1": v1,
                           "kind": "visual", "family": "visual"})
                cursor += 12.0
            dur = 3.0 if text_kind == "short" else (7.0 if text_kind in ("dialog", "reveal") else 5.0)
            start = cursor
            end = start + dur
            cues.append({"start": round(start, 2), "end": round(end, 2), "text": text})
            g0 = offset + start
            g1 = offset + end
            # 一直存在的背景声音（配乐 / 环境声）
            acts.append({"ep": ep, "g0": round(g0, 2), "g1": round(g1, 2),
                         "v": 0.02, "a": 0.9, "score": 3.4})
            if text_kind in ("dialog", "reveal", "short"):
                gt.append({"ep": ep, "g0": g0, "g1": g1, "kind": text_kind,
                           "family": "reveal" if text_kind == "reveal" else "dialog"})
            cursor = end + 0.6
        episodes.append({"idx": ep, "name": f"{ep:02d}.mp4", "path": "", "dur": 90.0,
                         "offset": offset, "cues": cues})
    proj = {"name": "__hooK_recall__", "episodes": episodes, "total": 20 * 90.0,
            "acts": acts, "sub_episodes": 20}
    return proj, gt


def cover(c: dict, g: dict) -> float:
    """候选覆盖标注区间的比例（按标注区间长度算）。"""
    if int(c["ep"]) != int(g["ep"]):
        return 0.0
    ov = min(float(c["g1"]), g["g1"]) - max(float(c["g0"]), g["g0"])
    return max(0.0, ov) / max(0.1, g["g1"] - g["g0"])


def evaluate(a: dict, gt: list[dict]) -> dict:
    hl = a["highlights"]
    covered: list[dict] = []
    type_ok = 0
    aligned = 0
    ranks: list[int] = []
    for g in gt:
        best = None
        for i, c in enumerate(hl):
            if cover(c, g) >= 0.5 and (best is None or i < best[0]):
                best = (i, c)
        if not best:
            continue
        covered.append(g)
        ranks.append(best[0])
        # 起点对齐：成片第一帧就该是那一下，而不是前一段铺垫
        if -1.7 <= float(best[1]["g0"]) - g["g0"] <= 1.2:
            aligned += 1
        t = best[1].get("primary_type")
        if g["family"] == "visual":
            ok = t in ("action", "energy")
        else:
            ok = t == g["family"]
        type_ok += 1 if ok else 0
    used = sum(1 for c in hl if any(cover(c, g) >= 0.4 for g in gt))
    return {
        "highlights": len(hl),
        "gt": len(gt),
        "recall": round(len(covered) / max(1, len(gt)), 3),
        "type_acc": round(type_ok / max(1, len(covered)), 3),
        "aligned": round(aligned / max(1, len(covered)), 3),
        "precision": round(used / max(1, len(hl)), 3),
        "median_rank": sorted(ranks)[len(ranks) // 2] if ranks else None,
        "missed": [g["kind"] for g in gt if g not in covered],
    }


def main() -> int:
    print("高光识别召回 / 标注准确率")
    proj, gt = build_corpus()
    t0 = time.time()
    a = A.analyze(proj)
    ms = (time.time() - t0) * 1000
    m = evaluate(a, gt)

    print(f"  合成剧：{len(proj['episodes'])} 集 / {len(gt)} 个标注高光 / "
          f"候选 {m['highlights']} 条 / 耗时 {ms:.0f}ms")
    print(f"  召回 {m['recall']:.0%} · 标注准确 {m['type_acc']:.0%} · "
          f"起点对齐 {m['aligned']:.0%} · 命中占比 {m['precision']:.0%} · "
          f"中位排名 {m['median_rank']} · 漏掉 {m['missed']}")

    ck("集末 3 秒短促高光能进候选", "short" not in m["missed"], m["missed"])
    ck("无台词打斗能进候选", "visual" not in m["missed"], m["missed"])
    ck("揭身份能进候选", "reveal" not in m["missed"], m["missed"])
    ck("整体召回 ≥ 80%", m["recall"] >= 0.80, m["recall"])
    ck("类型标注准确率 ≥ 70%", m["type_acc"] >= 0.70, m["type_acc"])
    ck("起点对齐 ≥ 80%（第一帧就是那一下）", m["aligned"] >= 0.80, m["aligned"])
    ck("候选里凑数片段不超过 25%", m["precision"] >= 0.75, m["precision"])
    ck("候选里不再清一色动作冲突",
       len({c.get("primary_type") for c in a["highlights"]}) >= 3,
       sorted({c.get("primary_type") for c in a["highlights"]}))
    ck("诊断耗时 < 400ms", ms < 400, f"{ms:.0f}ms")

    # 同一输入两次结果一致（识别必须是确定性的）
    b = A.analyze(proj)
    ck("两次识别结果一致",
       [(c["ep"], c["g0"]) for c in a["highlights"]] ==
       [(c["ep"], c["g0"]) for c in b["highlights"]])

    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "hook_recall.json").write_text(json.dumps(
        {"metrics": m, "checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

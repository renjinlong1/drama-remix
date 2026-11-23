"""钩子池容量 / 可配置上限 / 场景簇换批 专项测试。

覆盖：
  A 容量：40 集合成剧能铺到接近 100 条，且同场戏不重复占位
  B 上限：find_highlights / analyze / API 的 10-100 边界与项目保存
  C 场景簇换批：连续多批与历史场景不重叠；exclude_keys 旧接口仍可用
  D 兼容：老项目没有 hook_limit 时按 100；hook_current_items 重开还原；payload 带上限

用法：runtime\\python.exe tests\\test_hook_limit.py
输出：tests/results/hook_limit.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

import app as appmod  # noqa: E402
from engine import analyze as A  # noqa: E402
from engine import project as projmod  # noqa: E402
from test_hook_recall import build_corpus  # noqa: E402
from test_hooks_policy import make_beats  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []
NAME = "__hooklimit__"


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name +
          (f"   [{detail}]" if detail and not cond else ""))


def keys(hs) -> set[str]:
    return {A.hook_key(c) for c in hs}


def scene_overlap(a: dict, b: dict) -> float:
    """同集重叠秒数 / 较短一段长度；不同集返回 0。"""
    if int(a["ep"]) != int(b["ep"]):
        return 0.0
    ov = min(float(a["g1"]), float(b["g1"])) - max(float(a["g0"]), float(b["g0"]))
    return max(0.0, ov) / max(0.1, min(float(a["g1"]) - float(a["g0"]),
                                       float(b["g1"]) - float(b["g0"])))


def test_capacity() -> None:
    print("\n[A] 100 条钩子池容量")
    beats = make_beats(40, per_ep=8)
    hl = A.find_highlights(beats, top_k=100, min_keep=100)
    ck("40 集能铺出接近 100 条", len(hl) >= 80, len(hl))
    ck("不超过 100 条硬上限", len(hl) <= 100, len(hl))
    ck("覆盖足够多集", len({c["ep"] for c in hl}) >= 20, len({c["ep"] for c in hl}))
    ck("每条都有集号/起止/分数",
       all(c.get("ep") and c.get("g1", 0) > c.get("g0", 0)
           and "score" in c for c in hl))

    # 同集台词候选不能有 >50% 重叠（场景簇应已合并）；视觉候选是独立场景，跳过
    bad = []
    for i, a in enumerate(hl):
        for b in hl[i + 1:]:
            if a.get("visual_only") or b.get("visual_only"):
                continue
            if scene_overlap(a, b) > 0.5:
                bad.append((a["ep"], round(a["g0"], 1), round(b["g0"], 1)))
    ck("同一场戏不会重复占候选位", not bad, bad[:3])


def test_limits() -> None:
    print("\n[B] 上限配置与边界")
    beats = make_beats(40, per_ep=8)
    for lim in (10, 30, 60):
        hl = A.find_highlights(beats, top_k=lim, min_keep=lim)
        ck(f"find_highlights top_k={lim} 生效", len(hl) <= lim, len(hl))
        ck(f"find_highlights top_k={lim} 不空", len(hl) >= min(lim, 5), len(hl))

    proj, _gt = build_corpus()
    a = A.analyze(proj, hook_limit=30)
    ck("analyze hook_limit=30 生效", len(a["highlights"]) <= 30, len(a["highlights"]))
    ck("analyze 返回上限字段", a.get("hook_limit") == 30, a.get("hook_limit"))
    ck("hook_meta 带上限", (a.get("hook_meta") or {}).get("limit") == 30,
       a.get("hook_meta"))

    a = A.analyze(proj, hook_limit=5)
    ck("下限钳到 10", a.get("hook_limit") == 10 and len(a["highlights"]) <= 10,
       (a.get("hook_limit"), len(a["highlights"])))
    a = A.analyze(proj, hook_limit=200)
    ck("上限钳到 100", a.get("hook_limit") == 100, a.get("hook_limit"))

    proj["hook_limit"] = 30
    a = A.analyze(proj)
    ck("项目里保存的上限优先生效", a.get("hook_limit") == 30, a.get("hook_limit"))
    ck("项目上限限制候选数", len(a["highlights"]) <= 30, len(a["highlights"]))

    first = a["highlights"]
    proj["hook_current_items"] = first[:12]
    again = A.analyze(proj)
    ck("重开项目按 hook_current_items 还原", len(again["highlights"]) == 12,
       len(again["highlights"]))
    ck("还原时仍带项目上限", again.get("hook_limit") == 30, again.get("hook_limit"))


def test_scene_reseed() -> None:
    print("\n[C] 场景簇换批")
    # 60 集才有足够场景支撑 3 批 × 100 条；40 集到第 3 批会自然抽干
    beats = make_beats(60, per_ep=8)
    shown: list[dict] = []
    for batch in range(1, 4):
        hl = A.find_highlights(beats, top_k=100, min_keep=100,
                               exclude=shown or None)
        ck(f"第 {batch} 批非空", len(hl) >= 3, len(hl))
        bad = []
        for c in hl:
            for x in shown:
                if scene_overlap(c, x) > 0.4:
                    bad.append((c["ep"], round(c["g0"], 1), round(x["g0"], 1)))
                    break
        ck(f"第 {batch} 批与历史场景不重叠", not bad, bad[:3])
        ck(f"第 {batch} 批 key 也不重叠", not (keys(hl) & keys(shown)),
           list(keys(hl) & keys(shown))[:3])
        shown += hl

    # 旧接口：exclude_keys 仍能按 key 排除
    hl = A.find_highlights(beats, top_k=100, min_keep=100,
                           exclude_keys=keys(shown) or None)
    ck("exclude_keys 旧接口仍生效", not (keys(hl) & keys(shown)),
       list(keys(hl) & keys(shown))[:3])


def test_api() -> None:
    print("\n[D] API 与兼容")
    proj, _gt = build_corpus()
    proj["name"] = NAME
    proj["folder"] = ""
    for e in proj["episodes"]:
        e.setdefault("file", e.get("name") or f"{e['idx']:02d}.mp4")
        e.setdefault("width", 720)
        e.setdefault("height", 1280)
        e.setdefault("sub", "")
        e.setdefault("cues", [])
    a = A.analyze(proj)
    with appmod.LOCK:
        old = (appmod.STATE.get("project"), appmod.STATE.get("analysis"),
               appmod.STATE.get("plan"))
        appmod.STATE.update(project=proj, analysis=a, plan=None)
    appmod._prewarm_thumbs = lambda: None
    try:
        r = appmod.hooks_reseed({"hook_limit": 30, "new_seed": False,
                                 "exclude": []})
        ck("API 改上限 30 生效", r.get("hook_limit") == 30
           and len(r.get("highlights") or []) <= 30,
           (r.get("hook_limit"), len(r.get("highlights") or [])))
        ck("改上限写入项目", proj.get("hook_limit") == 30, proj.get("hook_limit"))

        r = appmod.hooks_reseed({"hook_limit": 5, "new_seed": False,
                                 "exclude": []})
        ck("API 下限钳到 10", r.get("hook_limit") == 10
           and len(r.get("highlights") or []) <= 10,
           (r.get("hook_limit"), len(r.get("highlights") or [])))
        r = appmod.hooks_reseed({"hook_limit": 200, "new_seed": False,
                                 "exclude": []})
        ck("API 上限钳到 100", r.get("hook_limit") == 100, r.get("hook_limit"))

        # 换一批：场景簇排除，返回当前批 + 上限字段
        r = appmod.hooks_reseed({"hook_limit": 100})
        ck("换批返回上限字段", r.get("hook_limit") == 100, r.get("hook_limit"))
        ck("换批返回当前批", bool(r.get("highlights")))

        # 重置后回到第一批（同 seed/上限）
        r1 = appmod.hooks_reseed({"reset": True})
        r2 = appmod.hooks_reseed({"reset": True})
        ck("重置可复现", keys(r1.get("highlights") or []) == keys(r2.get("highlights") or []),
           (len(r1.get("highlights") or []), len(r2.get("highlights") or [])))

        payload = appmod._proj_payload(proj, a)
        ck("项目 payload 带 hook_limit", payload.get("hook_limit") == 100,
           payload.get("hook_limit"))
        ck("旧项目缺省按 100", A.analyze({**proj, "hook_limit": 0}).get("hook_limit") == 100)
    finally:
        with appmod.LOCK:
            appmod.STATE.update(project=old[0], analysis=old[1], plan=old[2])
        for p in (projmod.PROJ / f"{NAME}.json",
                  projmod.PROJ / f"{NAME}.json.bak"):
            try:
                p.unlink()
            except OSError:
                pass


def main() -> int:
    print("钩子池容量 / 上限 / 场景簇换批")
    for fn in (test_capacity, test_limits, test_scene_reseed, test_api):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ck(fn.__name__ + " 未抛异常", False, repr(e))
    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "hook_limit.json").write_text(json.dumps(
        {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

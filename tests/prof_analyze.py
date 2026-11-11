"""分析链路性能剖析：各阶段耗时、热点函数、内存峰值、payload 体积。

用法：runtime\\python.exe tests/prof_analyze.py [项目文件名]
输出：tests/results/profile_before.json / profile_after.json（按 --tag 区分）
"""
from __future__ import annotations

import cProfile
import io
import json
import pstats
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import analyze as A  # noqa: E402
from engine import plan as P  # noqa: E402
from harness import fingerprint  # noqa: E402

PROJ = ROOT / "projects" / "9273_20261111_5694.json"
RESULTS = ROOT / "tests" / "results"


def load() -> dict:
    return json.loads(PROJ.read_text(encoding="utf-8"))


def timed(fn):
    t0 = time.perf_counter()
    r = fn()
    return (time.perf_counter() - t0) * 1000, r


def main() -> None:
    tag = "after"
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--before" in sys.argv:
        tag = "before"
    proj = load()
    print(f"项目：{proj['name']} · {len(proj['episodes'])} 集 · {proj['total']:.0f}s · "
          f"{len(proj.get('acts') or [])} 个动作点")

    stages: dict[str, float] = {}
    ms, beats = timed(lambda: A.segment_beats(proj))
    stages["segment_beats"] = ms
    print(f"  segment_beats      {ms:8.2f} ms  beats={len(beats)}")
    ms, _ = timed(lambda: A.attach_acts(beats, proj.get("acts") or []))
    stages["attach_acts"] = ms
    print(f"  attach_acts        {ms:8.2f} ms")
    ms, vis = timed(lambda: A.find_visual_highlights(proj, beats, proj.get("acts") or []))
    stages["visual"] = ms
    print(f"  visual_highlights  {ms:8.2f} ms  {len(vis)} 个")
    ms, hl = timed(lambda: A.find_highlights(beats, top_k=24, min_keep=15, extra_visual=vis))
    stages["highlights"] = ms
    print(f"  find_highlights    {ms:8.2f} ms  {len(hl)} 个")
    ms, ends = timed(lambda: A.find_endings(beats, proj["total"]))
    stages["endings"] = ms
    print(f"  find_endings       {ms:8.2f} ms  {len(ends)} 个")

    # 全量 analyze 三次取均值（含上面的全部阶段）
    runs = []
    for _ in range(3):
        ms, a = timed(lambda: A.analyze(proj))
        runs.append(ms)
    stages["analyze_avg"] = sum(runs) / len(runs)
    print(f"  analyze 全量       {stages['analyze_avg']:8.2f} ms  "
          f"（{', '.join(f'{x:.0f}' for x in runs)}）")

    # 方案构建（选开头后必跑）
    pl_runs = []
    for _ in range(3):
        ms, pl = timed(lambda: P.build(proj, a["beats"], target=360,
                                       hook={"g0": hl[0]["g0"], "g1": hl[0]["g1"]}))
        pl_runs.append(ms)
    stages["plan_avg"] = sum(pl_runs) / len(pl_runs)
    print(f"  plan.build         {stages['plan_avg']:8.2f} ms  "
          f"（{', '.join(f'{x:.0f}' for x in pl_runs)}）")

    # 热点
    pr = cProfile.Profile()
    pr.enable()
    A.analyze(proj)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(18)
    top = s.getvalue()
    print("\n--- cProfile (cumulative top 18) ---")
    print("\n".join(top.splitlines()[:34]))

    # 内存峰值：只跑 analyze
    tracemalloc.start()
    A.analyze(proj)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n内存峰值 analyze: {peak / 1048576:.2f} MB（当前 {cur / 1048576:.2f} MB）")

    # payload 体积与序列化耗时
    import app as appmod
    ms, payload = timed(lambda: appmod._proj_payload(proj, a))
    raw = json.dumps(payload, ensure_ascii=False)
    ms2 = timed(lambda: json.dumps(payload, ensure_ascii=False))[0]
    print(f"_proj_payload      {ms:8.2f} ms · {len(raw) / 1024:.0f} KB · "
          f"json.dumps {ms2:.2f} ms")
    parts = {k: len(json.dumps(v, ensure_ascii=False)) for k, v in payload.items()}
    for k, v in sorted(parts.items(), key=lambda x: -x[1])[:6]:
        print(f"    {k:12s} {v / 1024:7.1f} KB")
    stages["payload_ms"] = ms
    stages["payload_kb"] = round(len(raw) / 1024, 1)
    stages["payload_dumps_ms"] = ms2
    stages["mem_peak_mb"] = round(peak / 1048576, 2)

    fp = fingerprint(proj, a, pl)
    print(f"\n指纹 {fp}")
    out = {"tag": tag, "project": proj["name"], "stages": stages,
           "fingerprint": fp, "beats": len(beats), "highlights": len(hl),
           "profile": top.splitlines()[:34]}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"profile_{tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 tests/results/profile_{tag}.json")


if __name__ == "__main__":
    main()

"""去尾帧端到端验证：真实项目建方案 → 出片 → 核对时长与接缝裁剪。

用法：runtime\\python.exe tests/e2e_tailcard.py
输出：tests/results/e2e_tailcard.json
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from engine import analyze as A, ffmpegx, plan as P, render as R  # noqa: E402

PROJ = ROOT / "projects" / "9273_20261111_5694.json"
RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def main() -> int:
    if not PROJ.exists():
        print("缺少真实项目，跳过")
        return 0
    p = json.loads(PROJ.read_text(encoding="utf-8"))
    print(f"项目 {p['name']} · {len(p['episodes'])} 集")
    a = A.analyze(p)
    hook = min(a["highlights"], key=lambda c: c["ep"])
    pl = P.build(p, a["beats"], target=240,
                 hook={"g0": hook["g0"], "g1": hook["g1"]},
                 tail_mode="auto")
    cuts = pl.get("tail_cuts") or []
    print(f"方案：{pl['from']} → {pl['to']}，估算 {pl['estimated']:.1f}s")
    print("接缝裁剪：", [(c["ep"], c["sec"], c["by"]) for c in cuts])

    ck("跨集方案产生接缝", len(cuts) >= 1, len(cuts))
    ck("每处裁剪 1.2~2.0s", all(1.2 <= c["sec"] <= 2.0 for c in cuts),
       [c["sec"] for c in cuts])
    ck("依据是识别到卡片", all(c["by"] == "card" for c in cuts),
       [c["by"] for c in cuts])
    ck("裁剪总量与逐集之和一致",
       abs(pl["tail_total"] - sum(c["sec"] for c in cuts)) < 0.05,
       f"{pl['tail_total']} vs {sum(c['sec'] for c in cuts)}")

    # 方案里每段的结束时间不能越过该集正片末尾（卡片已扣除）
    eps = {e["idx"]: e for e in p["episodes"]}
    over = []
    for s in pl["segments"]:
        ep = eps[s["ep"]]
        limit = ep["offset"] + ep["dur"]
        if s["g1"] > limit + 0.05:
            over.append((s["ep"], round(s["g1"] - limit, 2)))
    ck("没有片段越过集末", not over, over[:3])

    tmp = Path(tempfile.mkdtemp(prefix="dc_tail_"))
    out = tmp / "tail_check.mp4"
    try:
        t0 = time.time()
        r = R.render(p, pl, str(out), {"size": "720x1280", "fit": "crop", "crf": 24})
        print(f"渲染 {time.time() - t0:.1f}s → {r['duration']}s / {r['size_mb']}MB")
        ck("成片生成", out.exists() and out.stat().st_size > 10000, out.stat().st_size)
        ck("成片时长贴合方案", abs(r["duration"] - pl["estimated"]) <= 1.5,
           f"{r['duration']} vs {pl['estimated']:.1f}")

        # 接缝处画面核对：裁掉卡片后，接缝后应该是下一集的画面，而不是定帧卡
        if cuts:
            import numpy as np

            def gray_at(path, t):
                args = ["-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=64:36,format=gray",
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"]
                data = ffmpegx.run_out(args)
                if not data or len(data) < 64 * 36:
                    return None
                return np.frombuffer(data[: 64 * 36], dtype=np.uint8).reshape(36, 64).astype(np.int16)

            seg = next((s for s in pl["segments"] if s["ep"] == cuts[0]["ep"]
                        and s.get("kind") != "hook"), None)
            if seg:
                seam = seg["g1"] - pl["from"]["g0"]
                first_ep = eps[cuts[0]["ep"]]
                tail_t = first_ep["dur"] - 0.6
                a = gray_at(out, seam + 0.2)
                nxt = next((e for e in p["episodes"] if e["idx"] == seg["ep"] + 1), None)
                b = gray_at(nxt["path"], 0.3) if nxt else None
                c = gray_at(first_ep["path"], tail_t)
                if a is not None and b is not None and c is not None:
                    d_next = float(np.abs(a - b).mean())
                    d_card = float(np.abs(a - c).mean())
                    print(f"接缝后 vs 下一集开头 {d_next:.1f}；vs 原集末尾卡片 {d_card:.1f}")
                    ck("接缝后画面来自下一集", d_next < d_card, f"{d_next:.1f} vs {d_card:.1f}")
                    ck("接缝后已不在卡片上", d_card > 12, f"{d_card:.1f}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "e2e_tailcard.json").write_text(json.dumps(
        {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
         "cuts": cuts, "estimated": pl["estimated"],
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

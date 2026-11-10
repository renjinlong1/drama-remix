"""视频链路性能基准：分阶段耗时 / 峰值内存 / 稳定性 / 结果指纹。

用法：
  runtime\\python.exe tests\\bench.py --tag baseline  --repeat 3
  runtime\\python.exe tests\\bench.py --tag optimized --repeat 3

每个用例在**独立子进程**里跑完整链路，避免峰值工作集跨轮污染；
同一用例重复多轮用于观察耗时漂移与内存增长（泄漏迹象）。
输出：tests/results/bench_<tag>.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ROOT, FIX, RESULTS, PY, cases, case_dir, fingerprint, temp_counts  # noqa: E402

MARK = "__BENCH_JSON__"
STAGE_NAMES = ("probe", "scan", "rescan", "vision", "analyze", "plan", "render")


def worker(case_name: str, do_render: bool) -> dict:
    from harness import Stages, peak_rss_mb, rss_mb
    from engine import ffmpegx, project as projmod, vision
    from engine import analyze as analyzemod, plan as planmod, render as rendermod

    c = next(x for x in cases() if x["name"] == case_name)
    folder = case_dir(case_name)
    st = Stages()
    r: dict = {"case": case_name, "stages": {}, "peak_rss_mb": 0.0,
               "rss_mb": 0.0, "fingerprint": "", "error": ""}
    try:
        vids = [v for v in folder.glob("01.*")
                if v.suffix.lower() in (".mp4", ".mkv", ".mov", ".avi", ".webm")]
        st.run("probe", lambda: ffmpegx.probe(vids[0]))
        proj = st.run("scan", lambda: projmod.scan(str(folder), "bench_" + case_name))
        # 同进程内重复导入：专门用来观察探测缓存 / 重复读取的开销
        st.run("rescan", lambda: projmod.scan(str(folder), "bench_" + case_name))
        # 取前 2 集做代表性「内容拆解」，避免 8 集全片解码把基准时间拉得过长
        vproj = {**proj, "episodes": proj["episodes"][:2]}
        v = st.run("vision", lambda: vision.scan_project(vproj, ROOT, force=True))
        proj["acts"] = v["events"]
        a = st.run("analyze", lambda: analyzemod.analyze(proj))
        target = 60.0
        pl = st.run("plan", lambda: planmod.build(proj, a["beats"], target=target))
        r["fingerprint"] = fingerprint(proj, a, pl, proj.get("acts"))

        if do_render:
            out = FIX / "_out" / ("bench_" + case_name + ".mp4")
            out.parent.mkdir(parents=True, exist_ok=True)

            def do():
                return rendermod.render(proj, pl, str(out),
                                        {"size": "match", "fit": "crop",
                                         "crf": 28, "preset": "ultrafast"})

            rr = st.run("render", do)
            r["render"] = {"duration": rr["duration"],
                           "size_kb": round(Path(rr["video"]).stat().st_size / 1024, 1)}
            out.unlink(missing_ok=True)
        r["stages"] = st.data
        r["total_s"] = st.total
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        r["stages"] = st.data
        r["total_s"] = st.total
    r["peak_rss_mb"] = peak_rss_mb()
    r["rss_mb"] = rss_mb()
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--render-cases", default="h264_360x640_6s")
    ap.add_argument("--worker", default=None)
    ap.add_argument("--do-render", type=int, default=0)
    args = ap.parse_args()

    if args.worker:
        print(MARK + json.dumps(worker(args.worker, bool(args.do_render)),
                                ensure_ascii=False))
        return 0

    if not args.tag:
        ap.error("主模式需要 --tag（如 baseline / optimized）")
    cs = cases()
    if not cs:
        print("请先运行 gen_fixtures.py 生成素材")
        return 2
    RESULTS.mkdir(parents=True, exist_ok=True)
    renders = set(x for x in args.render_cases.split(",") if x)
    rows = []
    for rep in range(args.repeat):
        for c in cs:
            cmd = [str(PY), str(Path(__file__).resolve()),
                   "--worker", c["name"], "--do-render",
                   "1" if c["name"] in renders else "0"]
            t = time.perf_counter()
            try:
                p = subprocess.run(cmd, capture_output=True, timeout=900)
                wall = round(time.perf_counter() - t, 2)
                line = next((l for l in p.stdout.decode("utf-8", "replace").splitlines()
                             if l.startswith(MARK)), None)
                if not line:
                    rows.append({"case": c["name"], "repeat": rep,
                                 "error": "worker 无有效输出",
                                 "stderr": p.stderr.decode("utf-8", "replace")[-500:]})
                    print(f"  [{args.tag}] rep{rep} {c['name']}: WORKER FAILED")
                    continue
                d = json.loads(line[len(MARK):])
            except Exception as e:
                rows.append({"case": c["name"], "repeat": rep,
                             "error": f"worker 异常 {type(e).__name__}: {e}"})
                continue
            d["repeat"] = rep
            d["wall_s"] = wall
            rows.append(d)
            print(f"  [{args.tag}] rep{rep} {c['name']}: "
                  f"total={d.get('total_s')}s peak={d.get('peak_rss_mb')}MB "
                  f"fp={d.get('fingerprint')} err={d.get('error', '')[:40]}")

    by: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("error") and r.get("total_s") is not None:
            by.setdefault(r["case"], []).append(r)
    summary = {}
    for k, rs in by.items():
        fps = sorted({r["fingerprint"] for r in rs})
        summary[k] = {
            "runs": len(rs),
            "total_s_avg": round(sum(r["total_s"] for r in rs) / len(rs), 3),
            "wall_s_avg": round(sum(r.get("wall_s", 0) for r in rs) / len(rs), 3),
            "peak_rss_mb_avg": round(sum(r["peak_rss_mb"] for r in rs) / len(rs), 2),
            "peak_rss_mb_max": max(r["peak_rss_mb"] for r in rs),
            "rss_mb_avg": round(sum(r["rss_mb"] for r in rs) / len(rs), 2),
            "stages_avg": {
                s: round(sum(r["stages"].get(s, 0) for r in rs) / len(rs), 3)
                for s in STAGE_NAMES if any(s in r["stages"] for r in rs)},
            "fingerprints": fps,
            "stable": len(fps) == 1,
        }
    out = {"tag": args.tag, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
           "repeat": args.repeat, "cases": len(cs),
           "render_cases": sorted(renders), "summary": summary,
           "rows": rows, "temp": temp_counts()}
    path = RESULTS / f"bench_{args.tag}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("写入", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

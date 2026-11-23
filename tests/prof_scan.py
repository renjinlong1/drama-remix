"""两段式导入 / 动作缓存 / 8 路并发的可复现基准。

量四件事（同一段素材、独立缓存目录，不污染 data/act）：
  1. 缓存未命中：8 路全扫一批
  2. 缓存命中：同一文件夹重复请求 / 改剧名重导（应为 0 解码）
  3. 并发度：4 路 vs 8 路的整批耗时
  4. 两段式收益：首屏（清点缓存 + 剧情分析）vs 同步扫完整剧的估算等待

用法：
  runtime\\python.exe tests/prof_scan.py
输出：tests/results/scan_perf.json，并在控制台打印对比表。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import analyze as analyzemod  # noqa: E402
from engine import ffmpegx, hwaccel, vision  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CACHE = Path(tempfile.gettempdir()) / "dc_scan_perf"
N_EP = 8


def pick_case() -> tuple[dict, dict]:
    """优先用真实 40 集项目的前 8 集；没有就合成 8 段 6 秒样片。"""
    for f in sorted((ROOT / "projects").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:      # noqa: BLE001
            continue
        eps = d.get("episodes") or []
        if len(eps) >= N_EP and sum(1 for e in eps if Path(e.get("path", "")).exists()) >= N_EP:
            meta = {"source": f.name, "total_eps": len(eps),
                    "minutes": round(sum(float(e.get("dur") or 0) for e in eps[:N_EP]) / 60, 1)}
            return {**d, "episodes": eps[:N_EP]}, meta

    folder = CACHE / "fixture" / "合成剧"
    shutil.rmtree(folder.parent, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    src = ROOT / "tests" / "fixtures" / "h264_360x640_6s" / "01.mp4"
    info = ffmpegx.probe(src)
    eps = []
    for i in range(1, N_EP + 1):
        v = folder / f"{i:02d}.mp4"
        shutil.copyfile(src, v)
        eps.append({"idx": i, "file": v.name, "path": str(v),
                    "dur": round(info["duration"], 3),
                    "has_audio": bool(info["has_audio"]),
                    "offset": round((i - 1) * info["duration"], 3)})
    proj = {"name": "合成剧", "folder": str(folder), "episodes": eps,
            "total": round(info["duration"] * N_EP, 3), "sub_episodes": N_EP}
    meta = {"source": "tests/fixtures", "total_eps": N_EP,
            "minutes": round(info["duration"] * N_EP / 60, 2)}
    return proj, meta


def run_once(proj: dict, workers: int, force: bool, name_suffix: str = "",
             drop: bool = True) -> dict:
    os.environ["DC_ACT_WORKERS"] = str(workers)
    if drop:
        vision.drop_cache(proj, CACHE)
    p = dict(proj, name=(proj.get("name") or "") + name_suffix)
    t = time.time()
    r = vision.scan_project(p, CACHE, force=force)
    dt = round(time.time() - t, 3)
    return {"secs": dt, "scanned": r["scanned"], "events": len(r["events"]),
            "cache_hit": r["cache_hit"], "pending": r["pending"],
            "batch_hw": r.get("batch_hw"), "batch_reason": r.get("batch_reason", "")}


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    proj, meta = pick_case()
    n = len(proj["episodes"])
    print(f"素材：{meta['source']} · 前 {n} 集 / {meta['minutes']} 分钟"
          f"（整剧 {meta['total_eps']} 集）")

    st = hwaccel.set_mode("auto")
    print(f"硬件解码：{st['mode']} / {st.get('backend') or '软件'} · "
          f"批量策略：{st.get('batch')}")
    out: dict = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), **meta,
                 "hw_mode": st["mode"], "hw_backend": st.get("backend") or "",
                 "batch": st.get("batch"), "runs": {}}
    # 首屏成本在空缓存下量：清点待扫集数 + 剧情分析（不含任何整集解码）
    t = time.time()
    vision.pending(proj, CACHE)
    t_pending = round(time.time() - t, 3)
    t = time.time()
    analyzemod.analyze(proj)
    t_analyze = round(time.time() - t, 3)
    try:
        w8a = run_once(proj, 8, True)
        out["runs"]["miss_8w"] = w8a
        print(f"缓存未命中 8 路：{w8a['secs']}s / {n} 集"
              f"（{round(w8a['secs'] / n, 2)}s 每集，{w8a['events']} 个动作点，"
              f"{'硬件' if w8a['batch_hw'] else '软件'}解码）")

        hit = run_once(proj, 8, False, drop=False)
        out["runs"]["hit_same_folder"] = hit
        print(f"同文件夹重复请求：{hit['secs']}s（扫描 {hit['scanned']} 集，命中 {hit['cache_hit']} 集）")

        ren = run_once(proj, 8, False, name_suffix="（改了剧名）", drop=False)
        out["runs"]["hit_renamed"] = ren
        print(f"改剧名重导：{ren['secs']}s（扫描 {ren['scanned']} 集，命中 {ren['cache_hit']} 集）")

        w4 = run_once(proj, 4, True)
        out["runs"]["miss_4w"] = w4
        w8b = run_once(proj, 8, True)
        out["runs"]["miss_8w_2"] = w8b
        best8 = min(w8a["secs"], w8b["secs"])
        speed = round(w4["secs"] / max(0.01, best8), 2)
        out["speedup_4_to_8"] = speed
        print(f"4 路：{w4['secs']}s · 8 路：{best8}s（{speed}×）")

        first_screen = round(t_pending + t_analyze, 3)
        per_ep = best8 / max(1, n)
        full_acts = round(per_ep * meta["total_eps"], 1)
        out["deferred"] = {"pending_secs": t_pending, "analyze_secs": t_analyze,
                           "first_screen_secs": first_screen,
                           "full_acts_estimate_secs": full_acts,
                           "saved_wait_secs": round(max(0.0, full_acts - first_screen), 1)}
        print(f"两段式首屏（清点缓存 + 剧情分析）：{first_screen}s · "
              f"整剧动作估算 {full_acts}s（省去首屏等待 {out['deferred']['saved_wait_secs']}s）")
    finally:
        os.environ.pop("DC_ACT_WORKERS", None)
        try:
            vision.drop_cache(proj, CACHE)
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(CACHE, ignore_errors=True)

    (RESULTS / "scan_perf.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("结果已写入 tests/results/scan_perf.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

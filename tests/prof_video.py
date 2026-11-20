"""视频处理链路性能基线：扫描 / 分析 / 出片，分别测耗时与进程内存峰值。

素材用一份自动生成的 3 分钟 720x1280 测试片（TEMP 里缓存一次），
8 集复用同一个文件：结果可重复，不依赖用户机器上是否还有原片。

用法：
  runtime\\python.exe tests\\prof_video.py before     # 写 tests/results/video_perf_before.json
  runtime\\python.exe tests\\prof_video.py after      # 写 tests/results/video_perf_after.json
  runtime\\python.exe tests\\prof_video.py compare    # 对比两份结果

每个 stage 在独立子进程里跑，峰值内存互不干扰（父进程只负责调度）。
内存取 Windows 的 PeakWorkingSetSize：只统计 Python 进程本身，
ffmpeg 子进程的内存另算，不计入。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "tests" / "results"
PY = str(Path(sys.executable))
WORK = Path(tempfile.gettempdir()) / "dc_perf"
CLIP = WORK / "ep.mp4"
DUR = 180.0
EPS = 8


def _peak_mb() -> float:
    if os.name != "nt":
        return 0.0
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    fn.restype = wintypes.BOOL
    ok = fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    if not ok:
        return 0.0
    return round(pmc.PeakWorkingSetSize / 1048576, 1)


def _clip() -> Path:
    """生成一段有运动、有声音的 3 分钟竖屏测试片（只生成一次）。"""
    if CLIP.exists():
        return CLIP
    WORK.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from engine import ffmpegx
    args = ["-y", "-f", "lavfi", "-i",
            "testsrc2=size=720x1280:rate=30:duration=%.1f" % DUR,
            "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=%.1f" % DUR,
            "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", "1200k", "-g", "60", "-c:a", "aac", "-b:a", "96k",
            "-shortest", str(CLIP)]
    ffmpegx.run(args, timeout=600)
    return CLIP


def _proj(n: int = EPS) -> dict:
    sys.path.insert(0, str(ROOT))
    from engine import ffmpegx

    path = _clip()
    info = ffmpegx.probe(path)
    episodes = []
    offset = 0.0
    for i in range(1, n + 1):
        cues = []
        t = 0.4
        j = 0
        while t < info["duration"] - 1.0:
            cues.append({"i": j, "start": round(t, 2), "end": round(t + 4.0, 2),
                         "text": "第%d集 你算什么东西 滚出去 我一巴掌扇死你" % i})
            t += 5.0
            j += 1
        episodes.append({
            "idx": i, "file": f"{i:02d}.mp4", "path": str(path),
            "sub": f"{i:02d}.srt", "sub_path": "", "sub_source": "file",
            "ocr": None, "dur": round(info["duration"], 3),
            "width": info["width"], "height": info["height"],
            "fps": info["fps"] or 30.0, "has_audio": True,
            "offset": round(offset, 3), "cues": cues,
        })
        offset += info["duration"]
    return {"name": "__perf__", "folder": str(WORK), "episodes": episodes,
            "total": round(offset, 3), "sub_episodes": n}


def stage_scan(n: int = EPS) -> dict:
    sys.path.insert(0, str(ROOT))
    from engine import vision

    proj = _proj(n)
    t = time.time()
    r = vision.scan_project(proj, ROOT, force=True)
    dt = time.time() - t
    try:
        (ROOT / "data" / "act" / "__perf__.json").unlink()
    except OSError:
        pass
    return {"secs": round(dt, 2), "per_ep": round(dt / max(1, n), 3),
            "eps": n, "events": len(r["events"]), "minutes": round(n * DUR / 60, 1),
            "peak_mb": _peak_mb()}


def stage_analyze() -> dict:
    sys.path.insert(0, str(ROOT))
    from engine import analyze

    proj = _proj()
    t = time.time()
    a = analyze.analyze(proj)
    dt = time.time() - t
    return {"secs": round(dt, 3), "beats": len(a["beats"]),
            "highlights": len(a["highlights"]), "peak_mb": _peak_mb()}


def stage_render() -> dict:
    sys.path.insert(0, str(ROOT))
    from engine import analyze, plan as planmod, render

    proj = _proj()
    a = analyze.analyze(proj)
    hook = min(a["highlights"], key=lambda c: c["ep"])
    pl = planmod.build(proj, a["beats"], target=120, hook=hook)
    planmod.restat(proj, pl)
    out = WORK / "out.mp4"
    t = time.time()
    r = render.render(proj, pl, str(out), {"size": "360x640", "crf": 30,
                                          "preset": "veryfast"})
    dt = time.time() - t
    return {"secs": round(dt, 2), "duration": r["duration"],
            "segments": r["segments"], "size_mb": r["size_mb"],
            "peak_mb": _peak_mb()}


STAGES = {"scan": stage_scan, "analyze": stage_analyze, "render": stage_render}


def run_stage(name: str) -> dict:
    code = ("import sys, json; sys.path.insert(0, r'%s');"
            "sys.path.insert(0, r'%s');"
            "import prof_video as P; print('@@'+json.dumps(P.STAGES['%s']()))"
            % (ROOT, ROOT / "tests", name))
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=1800)
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("@@"):
            return json.loads(line[2:])
    raise RuntimeError(f"{name} 失败：{p.stderr[-800:]}")


def collect() -> dict:
    out = {}
    for name in STAGES:
        print(f"… {name}", flush=True)
        out[name] = run_stage(name)
        print("  ", out[name], flush=True)
    return out


def compare(path_a: Path, path_b: Path) -> None:
    a = json.loads(path_a.read_text(encoding="utf-8"))
    b = json.loads(path_b.read_text(encoding="utf-8"))
    print(f"{'stage':8} {'item':10} {'before':>10} {'after':>10} {'change':>9}")
    for st in a:
        for k in a[st]:
            if not isinstance(a[st][k], (int, float)):
                continue
            va, vb = float(a[st][k]), float(b[st][k])
            if va == 0:
                continue
            chg = (vb - va) / va * 100
            print(f"{st:8} {k:10} {va:10.2f} {vb:10.2f} {chg:+8.1f}%")


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "before").lower()
    RESULTS.mkdir(parents=True, exist_ok=True)
    if mode == "compare":
        compare(RESULTS / "video_perf_before.json", RESULTS / "video_perf_after.json")
        return 0
    data = collect()
    (RESULTS / f"video_perf_{mode}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""顺剪工作台 · 测试公共设施。

提供：素材清单、进程峰值/当前内存、结果指纹、临时文件计数、阶段计时。
被 gen_fixtures.py / test_pipeline.py / bench.py 共用，不依赖 Web 服务。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIX = ROOT / "tests" / "fixtures"
RESULTS = ROOT / "tests" / "results"
PY = ROOT / "runtime" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)


def manifest() -> dict:
    p = FIX / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"cases": []}


def cases() -> list[dict]:
    return manifest().get("cases", [])


def case_dir(name: str) -> Path:
    return FIX / name


def _pmc() -> tuple[float, float]:
    """(当前工作集 MB, 峰值工作集 MB)。Windows 用 K32GetProcessMemoryInfo。"""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            if fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return (round(c.WorkingSetSize / 1048576.0, 2),
                        round(c.PeakWorkingSetSize / 1048576.0, 2))
        except Exception:
            pass
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        return round(v, 2), round(v, 2)
    except Exception:
        return 0.0, 0.0


def rss_mb() -> float:
    return _pmc()[0]


def peak_rss_mb() -> float:
    return _pmc()[1]


def temp_counts() -> dict:
    """观察中间文件是否堆积（渲染工作目录、抽帧缓存）。"""
    projects = ROOT / "projects"
    thumbs = ROOT / "data" / "thumbs"
    dirs = [p for p in projects.glob("_r*") if p.is_dir()]
    return {
        "render_dirs": len(dirs),
        "render_files": sum(len(list(p.glob("*"))) for p in dirs),
        "thumbs": len([p for p in thumbs.glob("*") if p.is_file()]) if thumbs.exists() else 0,
    }


def fingerprint(proj: dict, analysis: dict, plan: dict, acts=None) -> str:
    """把「探测/拆解/候选/方案/动作点」压成短哈希，用于比对优化前后结果是否一致。"""
    payload = {
        "eps": [{"file": e["file"], "dur": round(e["dur"], 2),
                 "w": e["width"], "h": e["height"], "audio": e["has_audio"],
                 "cues": len(e["cues"])} for e in proj["episodes"]],
        "beats": [[b["ep"], round(b["g0"], 2), round(b["g1"], 2), b["text"]]
                  for b in analysis["beats"]],
        "highlights": [[h["ep"], round(h["g0"], 2), round(h["g1"], 2),
                        h.get("kind"), h.get("primary_type"),
                        round(h.get("score", 0), 2)]
                       for h in analysis["highlights"]],
        "endings": [[round(x["g0"], 2), round(x["g1"], 2)]
                    for x in analysis.get("endings", [])],
        "plan": {"estimated": round(plan.get("estimated", 0), 2),
                 "segments": [[s["ep"], round(s["start"], 2), round(s["end"], 2)]
                              for s in plan.get("segments", [])]},
        "acts": [[a["ep"], round(a["g0"], 2), round(a["g1"], 2),
                  round(a["score"], 3)] for a in (acts or [])],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Stages:
    """分阶段计时；每个用例在独立子进程里跑，避免峰值内存跨轮污染。"""

    def __init__(self):
        self.data: dict[str, float] = {}

    def run(self, name: str, fn):
        t = time.perf_counter()
        r = fn()
        self.data[name] = round(time.perf_counter() - t, 3)
        return r

    @property
    def total(self) -> float:
        return round(sum(self.data.values()), 3)

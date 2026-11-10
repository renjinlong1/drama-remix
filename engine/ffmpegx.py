"""ffmpeg 定位、探测与执行。

不依赖 ffprobe：时长/分辨率/音轨全部从 `ffmpeg -i` 的 stderr 解析，
只需要一个 ffmpeg.exe 就能跑完整条流水线。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_FFMPEG: str | None = None


def _candidates() -> list[str]:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cands = [
        str(ROOT / "bin" / exe),
        shutil.which("ffmpeg") or "",
        r"C:\Program Files\magic-cut\resources\app.asar.unpacked\node_modules\ffmpeg-static\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    return [c for c in cands if c and Path(c).exists()]


def ffmpeg() -> str:
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    for c in _candidates():
        try:
            p = subprocess.run([c, "-version"], capture_output=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW)
            if p.returncode == 0:
                _FFMPEG = c
                return c
        except Exception:
            continue
    raise RuntimeError("找不到可用的 ffmpeg，请把 ffmpeg.exe 放到 bin/ 目录下")


def run(args: list[str], timeout: int = 3600, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run([ffmpeg(), "-hide_banner", "-nostdin", *args],
                       capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        tail = "\n".join(err.strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg 执行失败：{tail}")
    return p


def run_text(args: list[str], timeout: int = 3600) -> str:
    p = run(args, timeout=timeout, check=False)
    return p.stderr.decode("utf-8", "replace")


def run_out(args: list[str], timeout: int = 600) -> bytes:
    """跑 ffmpeg 并返回 stdout 二进制（如 image2pipe 的帧流）。失败返回空。"""
    p = run(args, timeout=timeout, check=False)
    return p.stdout if p.returncode == 0 else b""


_TIME = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def probe(path: str | Path) -> dict:
    """返回 duration/width/height/fps/has_audio/vcodec/acodec/sample_rate。"""
    out = run_text(["-i", str(path)], timeout=60)
    info = {"path": str(path), "duration": 0.0, "width": 0, "height": 0,
            "fps": 0.0, "has_audio": False, "has_video": False,
            "vcodec": "", "acodec": "", "sample_rate": 0, "channels": 0}
    m = _TIME.search(out)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    vs = re.search(r"Stream #\S+.*?Video:\s*([a-zA-Z0-9_]+).*?(\d{2,5})x(\d{2,5})", out)
    if vs:
        info["has_video"] = True
        info["vcodec"] = vs.group(1)
        info["width"] = int(vs.group(2))
        info["height"] = int(vs.group(3))
    fs = re.search(r"(\d+(?:\.\d+)?)\s*fps", out)
    if fs:
        info["fps"] = float(fs.group(1))
    ass = re.search(r"Stream #\S+.*?Audio:\s*([a-zA-Z0-9_]+).*?(\d{4,6})\s*Hz.*?(\w+),\s*([\w ]+)", out)
    if ass:
        info["has_audio"] = True
        info["acodec"] = ass.group(1)
        info["sample_rate"] = int(ass.group(2))
        info["channels"] = 2 if "stereo" in (ass.group(3) + ass.group(4)) else 1
    else:
        a2 = re.search(r"Stream #\S+.*?Audio:\s*([a-zA-Z0-9_]+)", out)
        if a2:
            info["has_audio"] = True
            info["acodec"] = a2.group(1)
            info["sample_rate"] = 48000
            info["channels"] = 2
    if not info["width"] or not info["height"]:
        raise RuntimeError(f"无法解析视频信息：{Path(path).name}")
    return info


def version() -> str:
    out = run_text(["-version"], timeout=30)
    return out.splitlines()[0] if out else "unknown"


def even(n: float) -> int:
    return int(round(n / 2)) * 2

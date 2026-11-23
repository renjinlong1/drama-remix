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
import threading
from collections import OrderedDict
from pathlib import Path

from . import hwaccel

ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_FFMPEG: str | None = None

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv",
              ".ts", ".wmv", ".mpg", ".mpeg", ".m2ts", ".3gp", ".ogv", ".vob"}
# 这些输入不是"要解码的视频文件"，注入 -hwaccel 没有意义甚至会出错
_NON_FILE_PREFIX = ("lavfi", "concat:", "pipe:", "http://", "https://", "rtsp://",
                    "udp://", "rtmp://", "data:", "fd:", "-")
# 超过这个行数就不再为重试缓存（说明硬件已经在稳定出数据，重试没意义）
HW_LINE_RETRY_CAP = 50000


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


# ── 硬件解码注入 ──────────────────────────────────────────────────────
def _first_video_input(args: list[str]) -> int | None:
    """第一个"真实视频文件"输入的 -i 下标；没有就返回 None。

    `-hwaccel` 必须放在它作用的那个 -i 前面。lavfi / concat 清单 / 图片 /
    管道都不解码，注入了反而可能让 ffmpeg 去初始化一个用不上的设备。
    """
    for i, a in enumerate(args):
        if a != "-i" or i + 1 >= len(args):
            continue
        src = str(args[i + 1])
        low = src.lower()
        if any(low.startswith(p) for p in _NON_FILE_PREFIX):
            continue
        if Path(src).suffix.lower() in VIDEO_EXTS:
            return i
    return None


def _no_decode(args: list[str]) -> bool:
    """`-c copy` 只搬流不解码，没必要开硬件设备。"""
    for i, a in enumerate(args):
        if a in ("-c", "-c:v", "-vcodec") and i + 1 < len(args):
            if str(args[i + 1]).lower() == "copy":
                return True
    return False


def hw_inject(args: list[str], allow_hw: bool = True) -> tuple[list[str], bool]:
    """返回 (注入后的参数, 是否用了硬件)。调用方据此决定要不要做软件重试。"""
    if not allow_hw or _no_decode(args):
        return list(args), False
    pre = hwaccel.prefix()
    if not pre:
        return list(args), False
    idx = _first_video_input(args)
    if idx is None:
        return list(args), False
    return list(args[:idx]) + pre + list(args[idx:]), True


def _run_raw(args: list[str], timeout: int, check: bool) -> subprocess.CompletedProcess:
    p = subprocess.run([ffmpeg(), "-hide_banner", "-nostdin", "-nostats", *args],
                       capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        tail = "\n".join(err.strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg 执行失败：{tail}")
    return p


def run(args: list[str], timeout: int = 3600, check: bool = True,
        allow_hw: bool = True) -> subprocess.CompletedProcess:
    """执行 ffmpeg；硬件解码失败时自动用软件重跑一次。

    关键判定：只有"硬件失败 + 软件成功"才算一次硬件故障并计入降级计数。
    两条都失败说明是命令本身的问题（路径、参数、坏文件），不误伤硬件状态。
    """
    hw_args, used = hw_inject(args, allow_hw)
    if not used:
        # 只统计"确实在解码视频文件"的命令；probe(allow_hw=False)、
        # -version、concat -c copy 这类不算解码次数
        if allow_hw and _first_video_input(args) is not None:
            hwaccel.record_decode("software")
        return _run_raw(args, timeout, check)
    try:
        p = _run_raw(hw_args, timeout, False)
    except subprocess.TimeoutExpired:
        hwaccel.report_failure("硬件解码超时")
        hwaccel.record_decode("software")
        return _run_raw(args, timeout, check)
    if p.returncode == 0:
        hwaccel.record_decode("hardware")
        return p
    err = p.stderr.decode("utf-8", "replace")
    p2 = _run_raw(args, timeout, False)
    if p2.returncode == 0:
        hwaccel.report_failure(" / ".join(err.strip().splitlines()[-3:]) or f"rc={p.returncode}")
        hwaccel.record_decode("software")
        return p2
    if check:
        tail = "\n".join(p2.stderr.decode("utf-8", "replace").strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg 执行失败：{tail}")
    return p2


def run_text(args: list[str], timeout: int = 3600, allow_hw: bool = True) -> str:
    p = run(args, timeout=timeout, check=False, allow_hw=allow_hw)
    return p.stderr.decode("utf-8", "replace")


def run_out(args: list[str], timeout: int = 600, allow_hw: bool = True) -> bytes:
    """跑 ffmpeg 并返回 stdout 二进制（如 image2pipe 的帧流）。失败返回空。"""
    p = run(args, timeout=timeout, check=False, allow_hw=allow_hw)
    return p.stdout if p.returncode == 0 else b""


_TIME = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")

# 探测结果缓存：一次导入要给每集启一个 ffmpeg 进程（约 0.06~0.12s），
# 重复导入 / 打开项目 / 包装预览会对同一批文件反复探测。按「绝对路径+大小+mtime」
# 做有界 LRU，文件被替换会自动失效；返回副本，调用方改字典不会污染缓存。
_PROBE_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_PROBE_LOCK = threading.Lock()
_PROBE_MAX = 256


def _probe_key(path: str | Path) -> tuple:
    p = Path(path)
    try:
        st = p.stat()
        return (str(p.resolve()), st.st_size, st.st_mtime_ns)
    except OSError:
        return (str(p), 0, 0)


def clear_probe_cache() -> None:
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


def probe(path: str | Path, use_cache: bool = True) -> dict:
    """返回 duration/width/height/fps/has_audio/vcodec/acodec/sample_rate。"""
    key = _probe_key(path) if use_cache else None
    if key is not None:
        with _PROBE_LOCK:
            hit = _PROBE_CACHE.get(key)
            if hit is not None:
                _PROBE_CACHE.move_to_end(key)
                return dict(hit)

    # 纯探测（没有输出文件，ffmpeg 必然 rc=1）：不注入硬件，省一次设备初始化
    out = run_text(["-i", str(path)], timeout=60, allow_hw=False)
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

    if key is not None:
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = dict(info)
            _PROBE_CACHE.move_to_end(key)
            while len(_PROBE_CACHE) > _PROBE_MAX:
                _PROBE_CACHE.popitem(last=False)
    return info


def _iter_once(args: list[str], stream: str, timeout: int, holder: dict):
    """跑一次并逐行产出；结束时把 returncode 写进 holder 供外层判断是否重试。"""
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-nostats", *args]
    if stream == "stdout":
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        pipe = p.stdout
    else:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             creationflags=CREATE_NO_WINDOW)
        pipe = p.stderr
    try:
        # 看门狗：管道读阻塞时（解码器卡死 / 坏文件）也能按时收场，
        # 不至于让一次导入永远挂在这一集上
        timer = threading.Timer(timeout, lambda: p.poll() is None and p.kill())
        timer.daemon = True
        timer.start()
        for raw in pipe:
            yield raw.decode("utf-8", "replace").rstrip("\r\n")
    finally:
        try:
            timer.cancel()
        except Exception:   # noqa: BLE001
            pass
        try:
            pipe.close()
        except Exception:
            pass
        try:
            p.wait(timeout=min(30, timeout))
        except Exception:
            p.kill()
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        holder["rc"] = p.returncode


def iter_lines(args: list[str], stream: str = "stderr", timeout: int = 1800,
               allow_hw: bool = False):
    """逐行流式读 ffmpeg 输出，不把整个 stdout/stderr 攒在内存里。

    blackframe 日志走 stderr、ametadata=print:file=- 走 stdout；只接管需要的那一路，
    另一路直接 DEVNULL，避免双管道互相等待。生成器结束时确保子进程被回收。

    allow_hw 默认关闭：这条只服务 vision 的「合并解码失败后的两条兜底老路」，
    存在的意义就是最大兼容性，用最保守的软件解码才兜得住（见 hwaccel 模块头 4）。
    显式打开时会先试硬件，失败则丢弃缓冲的行、用软件整体重跑；输出超过
    HW_LINE_RETRY_CAP 行就转为直通（硬件已在稳定出数据，放弃重试保住流式内存）。
    """
    hw_args, used = hw_inject(args, allow_hw)
    if not used:
        if allow_hw and _first_video_input(args) is not None:
            hwaccel.record_decode("software")
        yield from _iter_once(args, stream, timeout, {})
        return

    holder: dict = {}
    buf: list[str] = []
    overflow = False
    for line in _iter_once(hw_args, stream, timeout, holder):
        if overflow:
            yield line
            continue
        buf.append(line)
        if len(buf) > HW_LINE_RETRY_CAP:
            overflow = True
            yield from buf
            buf = []
    if overflow or holder.get("rc") == 0:
        hwaccel.record_decode("hardware")
        return
    hwaccel.report_failure(f"iter_lines rc={holder.get('rc')}")
    hwaccel.record_decode("software")
    yield from _iter_once(args, stream, timeout, {})


_FFMPEG_VER: str | None = None


def version() -> str:
    """ffmpeg 版本串。`-version` 每次都要起一个进程，缓存住别反复问。"""
    global _FFMPEG_VER
    if _FFMPEG_VER:
        return _FFMPEG_VER
    out = run_text(["-version"], timeout=30)
    _FFMPEG_VER = out.splitlines()[0] if out else "unknown"
    return _FFMPEG_VER


def even(n: float) -> int:
    return int(round(n / 2)) * 2

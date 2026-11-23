"""硬件解码：自动识别 → 自动启用 → 失败自动回退软件解码。

设计要点（踩过的坑都写在这里，改之前先看）：

1. **只看 `ffmpeg -hwaccels` 不够**。它列的是"编译时支持"，不代表这台机器
   真有设备。实测本机 RTX 3060 上 `qsv` 在列表里，但实跑立刻报
   `Error creating a MFX session: -9 / Device creation failed`（rc=171）。
   所以每个候选都必须拿真实视频**解一帧**验证。

2. **ffmpeg 会静默回退**。片源编解码器/位深不被硬件支持时，`-hwaccel` 照样
   rc=0，只是悄悄用了软件解码。自检必须要求 stderr 里出现硬件启用证据
   （`Selecting decoder ... hwaccel method` / `Reinit context ... pix_fmt: cuda`），
   否则判为"假启用"，不能算通过。

3. **不设 `-hwaccel_output_format`**。设了帧就留在显存里，本项目的
   `fps/scale/format=gray/tblend/blackframe`、`overlay`、`rawvideo` 全是 CPU
   滤镜，会直接报 `Impossible to convert between the formats`。保持默认，
   让 ffmpeg 把解码结果下载回系统内存：解码在 GPU、滤镜在 CPU，两边都舒服。
   实测 4 种后端的 pblack 序列与软件解码逐条一致。

4. **兜底路径故意不用硬件**。`vision._diff_series/_audio_series` 是"合并解码
   失败后的最后两条老路"，存在的意义就是最大兼容性；给它们上硬件反而违背
   兜底语义。这两条走 `iter_lines`，保持软件解码。

5. **结果结构零改动**。模式/失败次数只记在本模块的全局状态里，通过
   `/api/hwaccel` 反馈；不写进 `data/act/*.json`，回归指纹不变。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE_SAMPLE = ROOT / "data" / "hwaccel_probe.mp4"

# 运行期连续失败多少次就整进程降级到软件解码（单次失败只回退这一条命令）
FAIL_THRESHOLD = 3
# 自检总预算：驱动异常时不能让服务启动卡住
DETECT_BUDGET = 30.0
PER_TRY_TIMEOUT = 12

# 平台优先序。NVIDIA 独显上 NVDEC(cuda) 最快；d3d11va 兼容性最广
# （N/A/I 三家都能用）；dxva2 是老的 D3D9 通道；qsv 只有 Intel 核显/独显。
# 实测本机（RTX 3060）cuda / d3d11va / dxva2 均通过，qsv 失败。
_PLATFORM_ORDER: dict[str, list[str]] = {
    "nt": ["cuda", "d3d11va", "dxva2", "qsv", "vulkan"],
    "posix": ["cuda", "vaapi", "qsv", "videotoolbox", "vdpau", "vulkan"],
}
_BACKEND_LABEL = {
    "cuda": "NVIDIA NVDEC / CUDA",
    "d3d11va": "Direct3D 11 Video Acceleration",
    "dxva2": "DirectX Video Acceleration 2",
    "qsv": "Intel Quick Sync Video",
    "vaapi": "VA-API (Linux)",
    "videotoolbox": "Apple VideoToolbox",
    "vdpau": "VDPAU",
    "vulkan": "Vulkan Video",
}

# 硬件确实被启用的证据（任一命中即可）。
# 注意：**不能**把 `Selecting decoder 'xxx' because of requested hwaccel method`
# 当作证据 —— 它只说明"请求了 hwaccel"，解码器仍可能是软件名。实测 mpeg4 片
# `-hwaccel cuda` 会打印 `Selecting decoder 'mpeg4' ...` 且 rc=0，实际完全软件
# 解码。必须看到硬件帧格式（pix_fmt: cuda / Format cuda chosen）才算真启用。
_OK_EVIDENCE = (
    re.compile(r"Reinit context to .*pix_fmt:\s*(cuda|d3d11|dxva2_vld|qsv|vaapi|videotoolbox|vdpau|vulkan)", re.I),
    re.compile(r"Format\s+(cuda|d3d11|dxva2_vld|qsv|vaapi|videotoolbox|vdpau|vulkan)\s+chosen", re.I),
)
# rc=0 但硬件初始化失败、ffmpeg 悄悄退回软件时，也会出现这些字样，
# 必须单独拦住，否则会把"假启用"当成通过。
_DEVICE_FAIL = (
    "device creation failed", "no device available for decoder",
    "hardware device setup failed", "error creating a mfx session",
    "decoder setup failed", "failed to create hw",
)
# 只用于把失败原因说人话，不参与判定（日志里常有无关的 "could not"）
_FAIL_PATTERNS = (
    "device creation failed", "no device available", "hardware device setup failed",
    "error creating", "error initializing", "cannot load", "failed to create",
    "impossible to convert", "not supported", "unknown error occurred",
    "decoder setup failed", "invalid device",
)

# 两把锁：_LOCK 只保护状态字段的短读写（status/prefix 永不等待长任务）；
# _DETECT_LOCK 串行化耗时的自检/基准，避免多个线程同时起一堆 ffmpeg。
_LOCK = threading.RLock()
_DETECT_LOCK = threading.Lock()
_STATE: dict = {
    "checked": False,
    "mode": "software",
    "backend": None,
    "capable": False,
    "benchmark": {},
    "advisory": "",
    "compiled": [],
    "candidates": [],
    "attempts": [],
    "reason": "尚未检测",
    "sample": "",
    "checked_at": 0,
    "override": "",
    "degraded": False,
    "failures": 0,
    "fallbacks": 0,
    "last_error": "",
    "decodes": {"hardware": 0, "software": 0},
    "ffmpeg_version": "",
}
_SAMPLE_OVERRIDE: str | None = None
_ENV_MODE = (os.environ.get("DC_HWACCEL") or "").strip().lower()


def _ff() -> str:
    from . import ffmpegx  # 延迟导入：ffmpegx 顶层会 import 本模块
    return ffmpegx.ffmpeg()


def _flags() -> int:
    from . import ffmpegx
    return ffmpegx.CREATE_NO_WINDOW


def _short(s: str, n: int = 240) -> str:
    s = " ".join(str(s or "").split())
    return s[-n:] if len(s) > n else s


def compiled_methods() -> list[str]:
    """`ffmpeg -hwaccels` 列出的编译期支持后端（不代表设备可用）。"""
    try:
        p = subprocess.run([_ff(), "-hide_banner", "-nostdin", "-hwaccels"],
                           capture_output=True, timeout=20, creationflags=_flags())
        text = (p.stdout or b"").decode("utf-8", "replace")
        out = []
        for line in text.splitlines():
            s = line.strip().lower()
            if not s or s.startswith("hardware acceleration") or s.startswith("ffmpeg"):
                continue
            if re.fullmatch(r"[a-z0-9_]+", s):
                out.append(s)
        return out
    except Exception:  # noqa: BLE001
        return []


def _sample_ok(path: Path) -> bool:
    """样片必须真的能解出一帧。

    以前只看文件大小，截断/写坏的片（可能仍 >2KB）会被一直复用，
    结果是所有后端自检全失败、整机被误判成"不支持硬件解码"。
    """
    try:
        if not path.exists() or path.stat().st_size <= 2048:
            return False
        p = subprocess.run([_ff(), "-hide_banner", "-nostdin", "-nostats",
                            "-v", "error", "-i", str(path),
                            "-frames:v", "1", "-f", "null", "-"],
                           capture_output=True, timeout=15, creationflags=_flags())
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _make_sample(dest: Path) -> bool:
    """生成 1 秒 h264 自检片。只认 h264：mpeg4 会被硬件静默软解，验不了硬件。"""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    cmd = [_ff(), "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "1",
           "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           str(dest)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=_flags())
        if p.returncode == 0 and dest.exists() and dest.stat().st_size > 2048:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _ensure_sample() -> str:
    """返回可用的自检片；优先复用，坏了重造；data 不可写就落临时目录。

    不依赖项目里有没有剧、也不依赖发布包是否带 tests/fixtures —— 便携包
    解出来就能自检。用户显式指定的片源只校验、不重造。
    """
    if _SAMPLE_OVERRIDE:
        p = Path(_SAMPLE_OVERRIDE)
        return str(p) if _sample_ok(p) else ""
    if _sample_ok(PROBE_SAMPLE):
        return str(PROBE_SAMPLE)
    try:
        PROBE_SAMPLE.unlink(missing_ok=True)
    except OSError:
        pass
    # data 目录可能只读（便携包放在 Program Files 等），退到系统临时目录
    for dest in (PROBE_SAMPLE, Path(tempfile.gettempdir()) / "dc_hwaccel_probe.mp4"):
        if _make_sample(dest):
            return str(dest)
    return ""


_MAX_SAMPLE_LEN = 4096


def valid_sample(path) -> str:
    """校验用户指定的自检片源，只接受本机存在的视频文件。

    拒绝 UNC（\\\\server\\share）、URL/协议（http://、concat: 等）和以 - 开头的串，
    避免 `/api/hwaccel/recheck` 变成任意文件探测或网络请求入口。
    合法时返回规范化路径，非法返回 ""。
    """
    if not path:
        return ""
    s = str(path).strip().strip('"').strip("'")
    if not s or len(s) > _MAX_SAMPLE_LEN or s.startswith("-"):
        return ""
    low = s.lower()
    if low.startswith("\\\\") or low.startswith("//") or "://" in low:
        return ""
    p = Path(s)
    try:
        if not p.is_file():
            return ""
    except OSError:
        return ""
    from . import ffmpegx  # 延迟导入：ffmpegx 顶层会 import 本模块
    if p.suffix.lower() not in ffmpegx.VIDEO_EXTS:
        return ""
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def set_sample(path: str | None) -> None:
    """让上层用真实片源重检（比合成片更有代表性）。"""
    global _SAMPLE_OVERRIDE
    good = valid_sample(path)
    with _LOCK:
        _SAMPLE_OVERRIDE = good or None
        _STATE["checked"] = False


def _verify(name: str, sample: str) -> tuple[bool, str, int]:
    """真解一帧，确认硬件解码被启用。返回 (ok, 原因, 耗时ms)。"""
    t0 = time.time()
    cmd = [_ff(), "-hide_banner", "-nostdin", "-nostats", "-v", "verbose",
           "-hwaccel", name, "-i", sample, "-an", "-frames:v", "1",
           "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=PER_TRY_TIMEOUT,
                           creationflags=_flags())
        err = (p.stderr or b"").decode("utf-8", "replace")
        low = err.lower()
        ms = int((time.time() - t0) * 1000)
    except subprocess.TimeoutExpired:
        return False, "自检超时（驱动无响应）", int((time.time() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        return False, f"自检异常：{_short(str(e), 100)}", int((time.time() - t0) * 1000)

    def explain_hit(patterns: tuple[str, ...]) -> str:
        for pat in patterns:
            for ln in err.splitlines():
                if pat in ln.lower():
                    return ln.strip()
        return ""

    if p.returncode != 0:
        hit = explain_hit(_FAIL_PATTERNS)
        tail = [ln.strip() for ln in err.splitlines() if ln.strip()][-2:]
        detail = _short(hit or " / ".join(tail), 140)
        # Windows 的负退出码会被 Python 回绕成无符号大数，显示成有符号更好读
        rc = p.returncode - 2 ** 32 if p.returncode > 2 ** 31 else p.returncode
        return False, f"解码失败(rc={rc})：{detail}", ms
    # rc=0 也可能是"硬件初始化失败 → 静默用软件解完"，先拦这一类
    hit = explain_hit(_DEVICE_FAIL)
    if hit:
        return False, f"设备初始化失败：{_short(hit, 120)}", ms
    # 最后要求硬件启用证据，否则就是 ffmpeg 静默回退到软件解码
    evidence = None
    for rx in _OK_EVIDENCE:
        m = rx.search(err)
        if m:
            evidence = m.group(0).strip()
            break
    if not evidence:
        return False, "ffmpeg 未真正启用硬件解码（静默回退到软件）", ms
    return True, evidence, ms


def _candidate_order(compiled: list[str]) -> list[str]:
    pref = _PLATFORM_ORDER.get("nt" if os.name == "nt" else "posix", [])
    ordered = [m for m in pref if m in compiled]
    ordered += [m for m in compiled if m not in ordered]
    return ordered


def benchmark(sample: str, backend: str, loops: int = 24,
              rounds: int = 2) -> dict:
    """拿本工具真实的滤镜链量一次硬件 vs 软件吞吐。

    注意：-stream_loop 把 1 秒自检片循环解码成十几秒的量，避免"只解一帧、
    全在测设备初始化"。这不等于真实长片的绝对值，但方向一致，且不需要
    用户先导入一部剧。两轮取最小值压掉调度抖动。
    """
    base = ["-stream_loop", str(loops), "-i", sample, "-t", "12", "-an",
            "-vf", "fps=10,scale=128:-2,format=gray,tblend=all_mode=difference",
            "-f", "null", "-"]

    def once(pre: list[str]) -> tuple[float, int]:
        cmd = [_ff(), "-hide_banner", "-nostdin", "-nostats", "-v", "error",
               *pre, *base]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=60,
                               creationflags=_flags())
            return time.time() - t0, p.returncode
        except Exception:  # noqa: BLE001
            return float("inf"), -1

    def best(pre: list[str]) -> float:
        # 只统计成功轮次：命令失败时耗时再短也不能算"更快"，
        # 否则一次秒退的坏后端会被判成加速几十倍。
        vals = []
        for _ in range(rounds):
            secs, rc = once(pre)
            if rc == 0 and secs > 0:
                vals.append(secs)
        return min(vals) if vals else float("inf")

    sw = best([])
    hw = best(["-hwaccel", backend])
    if sw == float("inf") or hw == float("inf"):
        return {"hw_ms": 0, "sw_ms": 0, "ratio": 0.0, "faster": False,
                "valid": False, "loops": loops}
    ratio = hw / sw
    return {"hw_ms": round(hw * 1000), "sw_ms": round(sw * 1000),
            "ratio": round(ratio, 3), "faster": ratio <= 0.95,
            "valid": True, "loops": loops}


def detect(force: bool = False, sample: str | None = None,
           wait: bool | None = None) -> dict:
    """检测环境并选定解码后端。进程内缓存，force 可强制重检。

    并发语义：
      - `status()` / `prefix()` 永远只读快照，不会被自检卡住；
      - 普通调用拿不到自检锁就返回当前快照（checking=true），不阻塞；
      - set_mode / recheck 传 force=True 并等待，保证拿到最终结果。

    模式（DC_HWACCEL 环境变量或 /api/hwaccel/mode）：
      auto    默认。设备可用即自动启用硬件解码（严格按"支持时启用"），
              同时跑一次收益基准，把实测加速比与建议写进状态反馈。
      smart   设备可用且实测更快才启用；否则自动回退软件并说明原因。
      off     关闭硬件解码。
      具体名   如 cuda / d3d11va，强制启用该后端（跳过收益校验）。
    """
    global _SAMPLE_OVERRIDE
    if sample is not None:
        good = valid_sample(sample)
        if not good:
            return status()          # 非法片源不改状态；API 层会先返回 400
        with _LOCK:
            _SAMPLE_OVERRIDE = good
        force = True
    with _LOCK:
        if _STATE["checked"] and not force:
            return status()
        # 检测一开始就把 checked 置回 False：status() 显示 checking，
        # prefix() 立即回到软件，避免半成品状态被拿去拼命令
        _STATE["checked"] = False
        _STATE["reason"] = "正在检测硬件解码能力…"
    if wait is None:
        wait = bool(force)
    got = _DETECT_LOCK.acquire(timeout=(DETECT_BUDGET + 120) if wait else 0)
    if not got:
        # 已有线程在检测：直接返回快照，不把调用方堵在锁上
        return status()
    try:
        with _LOCK:
            if _STATE["checked"] and not force:
                return status()
            _STATE["checked"] = False
        return _detect_impl()
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            _STATE.update({"checked": True, "mode": "software", "backend": None,
                           "capable": False,
                           "reason": f"自检异常，已回退软件解码：{_short(e, 160)}"})
        return status()
    finally:
        _DETECT_LOCK.release()


def _detect_impl() -> dict:
    """真正的耗时检测。调用方已持有 _DETECT_LOCK，但不持 _LOCK。"""
    with _LOCK:
        override = _STATE.get("override") or _ENV_MODE or "auto"

    def snap(**kw) -> None:
        with _LOCK:
            _STATE.update(kw)

    compiled = compiled_methods()
    smp = _ensure_sample()
    t_start = time.time()
    snap(compiled=compiled, checked_at=int(time.time()), sample=smp,
         attempts=[], candidates=[], mode="software", backend=None,
         capable=False, benchmark={}, advisory="", degraded=False,
         failures=0, fallbacks=0, last_error="",
         override=override if override != "auto" else "")

    ver = ""
    try:
        # -version 写 stdout（run_text 返回 stderr，会拿到空串），这里直读
        p = subprocess.run([_ff(), "-hide_banner", "-version"],
                           capture_output=True, timeout=20, creationflags=_flags())
        lines = (p.stdout or b"").decode("utf-8", "replace").splitlines()
        ver = lines[0][:120] if lines else ""
    except Exception:  # noqa: BLE001
        pass
    snap(ffmpeg_version=ver)

    if override == "off":
        snap(reason="已关闭硬件解码（DC_HWACCEL=off 或手动关闭），使用软件解码")
        return _done()
    if not smp:
        snap(reason=("指定的自检片源不可用（需要本机存在且能解码的视频文件），"
                     "自动使用软件解码" if _SAMPLE_OVERRIDE else
                     "无法生成自检样片（data 目录与临时目录都不可写），"
                     "自动使用软件解码"))
        return _done()
    if not compiled:
        snap(reason="当前 ffmpeg 编译时未包含任何硬件加速后端，自动使用软件解码")
        return _done()

    forced = False
    if override in ("auto", "smart"):
        cands = _candidate_order(compiled)
        snap(reason="自动检测中")
    else:
        if override not in compiled:
            snap(reason=(f"指定的后端 {override} 不在 ffmpeg 支持列表 "
                         f"({', '.join(compiled)}) 中，自动回退软件解码"))
            return _done()
        cands = [override]
        forced = True
    snap(candidates=list(cands))

    chosen = None
    for name in cands:
        if time.time() - t_start > DETECT_BUDGET:
            with _LOCK:
                _STATE["attempts"].append({"name": name, "ok": False,
                                           "reason": "超过自检总预算，停止尝试", "ms": 0})
            break
        ok, why, ms = _verify(name, smp)
        with _LOCK:
            _STATE["attempts"].append({"name": name, "ok": ok,
                                       "reason": why, "ms": ms})
        if ok:
            chosen = name
            break
    if not chosen:
        with _LOCK:
            attempts = list(_STATE["attempts"])
        brief = "; ".join(f"{a['name']}={a['reason']}" for a in attempts[:3])
        snap(capable=False,
             reason="未找到可用的硬件解码后端，自动回退软件解码。" + _short(brief, 260))
        return _done()

    snap(capable=True)
    bm: dict = {}
    if not forced:
        bm = benchmark(smp, chosen)
        snap(benchmark=bm)
        if bm.get("valid"):
            pct = abs(round((1 - bm["ratio"]) * 100))
            if bm["faster"]:
                snap(advisory=f"本机实测硬件解码快 {pct}%，可放心使用")
            else:
                snap(advisory=(f"短样片基准：硬件 {bm['hw_ms']}ms / 软件 "
                               f"{bm['sw_ms']}ms（慢 {pct}%）。本工具以 CPU 滤镜为主，"
                               f"解码帧要回传内存，抵消了硬件收益"))
    with _LOCK:
        advisory = _STATE["advisory"]
    if override == "smart" and bm.get("valid") and not bm.get("faster"):
        snap(mode="software", backend=None,
             reason="硬件设备可用，但实测本工具链路上更慢，已自动回退软件解码",
             advisory=advisory + "；已按 smart 自动回退，如需强制启用可指定后端名")
    else:
        snap(mode="hardware", backend=chosen,
             reason=f"已启用硬件解码：{_BACKEND_LABEL.get(chosen, chosen)} "
                    f"({chosen})；实解一帧验证通过")
        if bm.get("valid") and not bm["faster"]:
            snap(advisory=advisory + "；追求速度可切 smart / off")
    return _done()


def _done() -> dict:
    with _LOCK:
        _STATE["checked"] = True
    return status()


def status() -> dict:
    """给 API / 前端的完整状态快照（返回副本，调用方改不污染内部）。"""
    with _LOCK:
        s = dict(_STATE)
        s["attempts"] = [dict(a) for a in _STATE["attempts"]]
        s["compiled"] = list(_STATE["compiled"])
        s["candidates"] = list(_STATE["candidates"])
        s["benchmark"] = dict(_STATE["benchmark"])
        s["decodes"] = dict(_STATE["decodes"])
        s["backend_label"] = _BACKEND_LABEL.get(s["backend"] or "", s["backend"] or "")
        s["enabled"] = s["mode"] == "hardware"
        s["checking"] = not s["checked"]
        return s


def prefix() -> list[str]:
    """给 ffmpeg 命令用的硬件解码参数；软件解码 / 检测未完成时返回 []。

    只读快照，绝不触发检测：否则每条 ffmpeg 命令都要等一次自检，
    启动预热或驱动异常时整条流水线会被拖住。

    永远不带 `-hwaccel_output_format`：解码帧下载回系统内存，
    保证本项目所有 CPU 滤镜链可用（见模块头说明 3）。
    """
    with _LOCK:
        if not _STATE["checked"] or _STATE["mode"] != "hardware" or not _STATE["backend"]:
            return []
        return ["-hwaccel", _STATE["backend"]]


def record_decode(mode: str) -> None:
    """统计一次实际解码用的是硬件还是软件，供状态反馈。"""
    with _LOCK:
        key = "hardware" if mode == "hardware" else "software"
        _STATE["decodes"][key] = _STATE["decodes"].get(key, 0) + 1


def report_failure(err: str) -> None:
    """运行期硬件解码失败：本次自动回退软件；连续失败达阈值则整进程降级。

    单次失败不禁用硬件 —— 可能只是某个特殊片源（10bit / AV1 / 坏文件）。
    ffmpeg 自身也会对不支持的格式静默软解，所以真正走到这里的很少。
    """
    with _LOCK:
        st = _STATE
        st["failures"] += 1
        st["fallbacks"] += 1
        st["last_error"] = _short(str(err), 200)
        if st["failures"] >= FAIL_THRESHOLD and st["mode"] == "hardware":
            st["degraded"] = True
            st["mode"] = "software"
            st["reason"] = (f"硬件解码连续失败 {st['failures']} 次，已自动降级为软件解码；"
                            f"最近错误：{st['last_error']}")


def set_mode(mode: str) -> dict:
    """运行时切换：auto / smart / off / 具体后端名。返回切换后的状态。"""
    global _ENV_MODE
    m = str(mode or "auto").strip().lower()
    with _LOCK:
        _ENV_MODE = ""                 # 手动设置覆盖启动时的环境变量
        _STATE["override"] = "" if m == "auto" else m
        _STATE["checked"] = False
        _STATE["degraded"] = False
        _STATE["failures"] = 0
        _STATE["fallbacks"] = 0
        _STATE["last_error"] = ""
    return detect(force=True)


def recheck(sample: str | None = None) -> dict:
    """重新自检（换显卡/驱动、插上扩展坞后手动触发）。"""
    with _LOCK:
        _STATE["checked"] = False
        _STATE["degraded"] = False
        _STATE["failures"] = 0
        _STATE["fallbacks"] = 0
        _STATE["last_error"] = ""
    return detect(force=True, sample=sample)


def invalidate() -> None:
    """ffmpeg 可执行文件变化后清空缓存（ffmpegx.clear_probe_cache 同款语义）。"""
    with _LOCK:
        _STATE["checked"] = False

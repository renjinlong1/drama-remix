"""动作高光检测：从原片画面与声音里找「打起来」的瞬间。

短剧的高光一多半不靠台词——扇巴掌、泼酒、摔杯子、推下楼梯、当众下跪，
这些镜头往往只有一下闷响、一帧剧烈晃动，字幕根本不提。台词分再高也抓不到。

这里不做"识别动作类别"（那需要训练模型），而是找**动作事件的强度**：
一次剧烈的画内运动 + 一声短促的脆响，就足以定位"出事了"的那几秒。
真正的语义由后续打分里的台词线索和用户自己挑来完成。

两路信号，都是一次 ffmpeg 扫描：

1. 画面（fps=10，缩到 128 宽灰度，相邻帧相减）
   blackframe 的 pblack 告诉我们"这一帧有多少像素几乎没变"。
   动作发生时 pblack 骤降。剪辑点也会骤降，但只有 1 帧；
   真人动作会连续 3 帧以上（≥0.3 秒）——用这个差别把剪辑点剔掉。
2. 声音（0.1 秒一窗，astats 取 Peak_level）
   巴掌、砸杯、泼水是"瞬间峰值远高于周围"的宽带噪声。
   用 ±2 秒的滚动中位数做基线，高出 9 dB 以上算一次冲击。
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpegx

try:                      # numpy 跟着 cv2 一起进便携包；缺了就走旧的 astats 路径
    import numpy as np
except Exception:         # noqa: BLE001
    np = None

DIFF_FPS = 10
DIFF_W = 128
DIFF_THRESHOLD = 42      # 单像素灰度差 > 42 记为"变了"
MOVE_PBLACK = 80         # 旧的固定判定；现在只当上限用，实际阈值按本集噪声水平自适应
MOVE_MIN = 45            # 阈值下限：再低就是把压缩噪声也当动作
MOVE_SPAN = 15           # 比"本集静态基线"低这么多才算这一帧在动
MIN_RUN = 2              # 连续 ≥2 帧（0.2 秒）才算动作，单帧是剪辑点
AUD_WIN = 0.1
# 声音峰值必须按**源采样率**的 1024 样本一块来算：astats 就是这么输出的
# （44.1kHz 源 → 23.22ms/块，48kHz 源 → 21.33ms/块）。以前这里写死 48k
# 重采样，时间轴会一路累积偏移，事件落点对不上。
AUD_BLOCK = 1024
AUD_SR = 48000           # 源采样率探测不到时的兜底
AUD_SPIKE = 9.0          # 比滚动中位数高 9 dB
AUD_FLOOR = -18.0        # 绝对下限：太小声的窗口不参与（避免底噪抖动）
PCM_CHUNK = 262144       # 流式读取块大小：一块 256KB ≈ 2.7 秒 48k 单声道
VISION_WORKERS = 4       # 整剧扫描并发上限（每路还会限制 ffmpeg 线程数）

_BF = re.compile(r"pblack:(\d+)\s+pts:\d+\s+t:([\d.]+)")
_PEAK = re.compile(r"lavfi\.astats\.(\d+)\.Peak_level=(-?[\d.]+|inf|-inf|nan)", re.I)
_PTS = re.compile(r"^frame:(\d+)\s+pts:\d+\s+pts_time:([\d.]+)")


def _pct(values: list[float], q: float, default: float) -> float:
    vals = sorted(values)
    if not vals:
        return default
    return float(vals[min(len(vals) - 1, int(len(vals) * q))])


def _move_threshold(series: list[tuple[float, int]]) -> int:
    """按本集自己的"静态基线"定动作阈值。

    干净数字片：p90≈100 → 88（比旧的 80 稍敏感，暗场/小动作也能抓到）；
    低码率、手机翻录、强噪点：p90≈88 → 73（收紧，避免整集都在"动"）。
    这样同一套代码在 4K 片源和 360p 录屏上的判定口径接近，不再靠手调常量。
    """
    if not series:
        return MOVE_PBLACK
    p90 = _pct([float(pb) for _, pb in series], 0.90, MOVE_PBLACK)
    return int(min(MOVE_PBLACK + 8, max(MOVE_MIN, p90 - MOVE_SPAN)))


# ── 单路信号 ─────────────────────────────────────────────────────────
# 以前把 ffmpeg 的 stdout+stderr 全量 capture 成一个大字符串再 splitlines，
# 长片（尤其 astats 每窗多行）会同时占着 bytes / 解码后的 str / 行列表三份内存。
# 改为逐行流式解析：峰值内存只跟「最终 series」有关，不再跟日志体积有关。
def _diff_series(video: str) -> list[tuple[float, int]]:
    """[(t, pblack)]，pblack 越小说明画面动得越厉害。"""
    args = [
        "-i", video, "-an",
        "-vf", (f"fps={DIFF_FPS},scale={DIFF_W}:-2,format=gray,"
                f"tblend=all_mode=difference,"
                f"blackframe=amount=0:threshold={DIFF_THRESHOLD}"),
        "-f", "null", "-",
    ]
    series: list[tuple[float, int]] = []
    for line in ffmpegx.iter_lines(args, "stderr"):
        if "blackframe" not in line:
            continue
        m = _BF.search(line)
        if m:
            series.append((float(m.group(2)), int(m.group(1))))
    return series


def _audio_series(video: str) -> list[tuple[float, float]]:
    """[(t, peak_db)]，0.1 秒一窗。"""
    args = [
        "-i", video, "-vn",
        "-af", f"astats=metadata=1:reset=1:length={AUD_WIN},ametadata=print:file=-",
        "-f", "null", "-",
    ]
    series: list[tuple[float, float]] = []
    t = 0.0
    for line in ffmpegx.iter_lines(args, "stdout"):
        line = line.strip()
        m = _PTS.match(line)
        if m:
            t = float(m.group(2))
            continue
        m = _PEAK.match(line)
        if not m:
            continue
        chan, val = m.group(1), m.group(2).lower()
        if chan != "1":            # 只看第一个声道，双声道数值一样
            continue
        db = -90.0
        if val in ("inf", "+inf"):
            db = 0.0
        elif val not in ("-inf", "nan"):
            try:
                db = float(val)
            except ValueError:
                db = -90.0
        series.append((t, db))
    return series


def _pcm_chunk_peaks(chunk: bytes, carry: bytes, sr: int,
                     start_idx: int) -> tuple[list[tuple[float, float]], bytes, int]:
    """PCM 分块求峰值：只保留不到一个块（4KB）的尾巴，内存与片长无关。

    以前把整集 PCM 读进内存再 reshape 成 float32，3 分钟 48k 单声道要
    17MB 原始字节 + 35MB 浮点副本 + 35MB 绝对值副本，4 路并发峰值 350MB。
    现在按 256KB 读、按 1024 样本求峰值，单路常驻 < 1MB。
    """
    buf = carry + chunk
    n = len(buf) // 2
    if n < AUD_BLOCK:
        return [], buf, start_idx
    a = np.frombuffer(buf[: n * 2], dtype="<i2")
    nb = len(a) // AUD_BLOCK
    if not nb:
        return [], buf, start_idx
    blocks = a[: nb * AUD_BLOCK].reshape(nb, AUD_BLOCK)
    # int16 的 -32768 直接取 abs 会溢出成负数，先升到 int32 再取绝对峰值
    mx = blocks.max(axis=1).astype(np.int32)
    mn = blocks.min(axis=1).astype(np.int32)
    peak = np.maximum(mx, -mn).astype(np.float32) / 32768.0
    db = 20.0 * np.log10(np.maximum(peak, 1e-7))
    step = AUD_BLOCK / max(1, sr)
    carry = buf[nb * AUD_BLOCK * 2:]
    rows = [((start_idx + k) * step, round(float(v), 2)) for k, v in enumerate(db)]
    return rows, carry, start_idx + nb


def _pcm_series(pcm: bytes, sr: int = AUD_SR) -> list[tuple[float, float]]:
    """PCM(s16 单声道) → [(t, peak_db)]，与 astats 的逐块统计同一粒度。

    逐 1024 样本一块，块内取绝对峰值转 dBFS。核心分块逻辑与流式扫描共用，
    保证"整段读"和"边解边读"两条路径结果完全一致。
    """
    if np is None or len(pcm) < 4:
        return []
    pcm = pcm[: len(pcm) // 2 * 2]
    out: list[tuple[float, float]] = []
    carry, idx = b"", 0
    for off in range(0, len(pcm), PCM_CHUNK):
        rows, carry, idx = _pcm_chunk_peaks(pcm[off:off + PCM_CHUNK], carry, sr, idx)
        out.extend(rows)
    return out


def _scan_signals(video: str, has_audio: bool = True,
                  sr: int | None = None, threads: int = 2,
                  timeout: float = 600.0) -> tuple[list, list]:
    """一次解码同时拿画面帧差与声音峰值，边解边读不攒整段。

    以前 `_diff_series` 和 `_audio_series` 各起一个 ffmpeg，同一集解码两遍；
    合并成一条 `-filter_complex` 后只解码一次（81 秒正片 2166ms → 710ms）。
    现在 stdout 的 PCM 也改成流式：不再 communicate() 攒完整段再算，
    峰值内存从「片长×8 字节」降到「一个读块」。
    """
    if sr is None:
        try:
            sr = int(ffmpegx.probe(video).get("sample_rate") or 0)
        except Exception:       # noqa: BLE001
            sr = 0
    sr = sr or AUD_SR
    fc = (f"[0:v]fps={DIFF_FPS},scale={DIFF_W}:-2,format=gray,"
          f"tblend=all_mode=difference,"
          f"blackframe=amount=0:threshold={DIFF_THRESHOLD}[v]")
    maps = ["-map", "[v]", "-f", "null", "-"]
    if has_audio:
        # 不重采样：保留源采样率，块时长才和 astats 对得上
        fc += ";[0:a]aformat=sample_fmts=s16:channel_layouts=mono[a]"
        maps += ["-map", "[a]", "-f", "s16le", "-"]
    threads = max(1, int(threads))
    cmd = [ffmpegx.ffmpeg(), "-hide_banner", "-nostdin", "-nostats", "-v", "info",
           "-threads", str(threads), "-filter_threads", str(threads),
           "-i", str(video), "-filter_complex", fc, *maps]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         bufsize=0, creationflags=ffmpegx.CREATE_NO_WINDOW)

    series: list[tuple[float, int]] = []
    audio: list[tuple[float, float]] = []
    err_tail: deque[str] = deque(maxlen=8)

    def read_stderr() -> None:
        for raw in p.stderr:
            line = raw.decode("utf-8", "replace")
            if "blackframe" in line:
                m = _BF.search(line)
                if m:
                    series.append((float(m.group(2)), int(m.group(1))))
            elif line.strip():
                err_tail.append(line.rstrip())

    reader = threading.Thread(target=read_stderr, daemon=True, name="ff-stderr")
    reader.start()
    # 看门狗：解码卡住时直接杀进程，不让一次导入永远挂在那里
    killed = {"hit": False}

    def _watchdog() -> None:
        if p.poll() is None:
            killed["hit"] = True
            p.kill()

    timer = threading.Timer(timeout, _watchdog)
    timer.daemon = True
    timer.start()
    timed_out = False
    carry, idx = b"", 0
    try:
        while True:
            chunk = p.stdout.read(PCM_CHUNK)
            if not chunk:
                break
            if has_audio and np is not None:
                rows, carry, idx = _pcm_chunk_peaks(chunk, carry, sr, idx)
                audio.extend(rows)
        reader.join(timeout=30)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            p.kill()
            p.wait(timeout=10)
    finally:
        timer.cancel()
        for pipe in (p.stdout, p.stderr):
            try:
                pipe.close()
            except Exception:   # noqa: BLE001
                pass
        if p.poll() is None:
            p.kill()

    if timed_out or killed["hit"]:
        raise RuntimeError("动作扫描超时")
    if not series and p.returncode != 0:
        tail = [ln for ln in list(err_tail)[-3:] if ln.strip()]
        raise RuntimeError("ffmpeg 解码失败：" + " / ".join(tail))
    return series, audio


# ── 合成事件 ─────────────────────────────────────────────────────────
def _motion_runs(series: list[tuple[float, int]],
                 thr: int | None = None) -> list[dict]:
    """把连续低 pblack 的帧并成"动作段"，单帧突变（剪辑点）丢弃。"""
    thr = _move_threshold(series) if thr is None else thr
    runs: list[dict] = []
    cur: list[tuple[float, int]] = []
    for t, pb in series:
        if pb <= thr:
            cur.append((t, pb))
        else:
            if len(cur) >= MIN_RUN:
                runs.append(_run_dict(cur, thr))
            cur = []
    if len(cur) >= MIN_RUN:
        runs.append(_run_dict(cur, thr))
    return runs


def _run_dict(cur: list[tuple[float, int]], thr: int = MOVE_PBLACK) -> dict:
    t0, t1 = cur[0][0], cur[-1][0]
    # 深度按同一阈值归一：低码率片子的"35 vs 阈值 75"和干净片子的
    # "40 vs 阈值 85"会得到接近的强度，不再因为画质不同而整体偏强/偏弱
    depth = statistics.mean(max(0, thr - pb) for _, pb in cur) / max(1, thr)
    return {"t0": round(t0, 2), "t1": round(t1, 2),
            "dur": round(max(0.1, t1 - t0 + 1.0 / DIFF_FPS), 2),
            "v": round(min(1.6, depth * 2.2), 3)}


def _audio_spikes(series: list[tuple[float, float]]) -> list[dict]:
    """找"脆响"：比周围响很多、而且收得极快。

    巴掌 / 砸杯 / 泼水是一下子就过去的（几十毫秒），音乐起伏、哭喊会拖好几秒。
    两者都用"比 ±0.25 秒中位数高多少 dB"起步，再用**峰值持续时长**分开：
    持续 ≤3 个窗（≈60ms）= 冲击；更长 = 情绪/音乐，也算戏剧点但分低一些。
    """
    if not series:
        return []
    vals = [v for _, v in series]
    n = len(vals)
    # 窗口步长从序列本身量出来（astats 与 PCM 两条路径都是 21.3ms 左右），
    # 不再写死 0.021，换采样率时 hold 判定不会跟着漂
    step = AUD_BLOCK / AUD_SR
    if n > 2:
        step = statistics.median(series[i + 1][0] - series[i][0] for i in range(min(n - 1, 40)))
    half = 12                      # ±0.25 秒（每窗 ≈21ms）
    # 整体音量很低的片子（手机翻录、压得很狠）不能用固定的绝对下限，
    # 按整体中位数往下放一档，否则一句都挑不出来
    floor = min(AUD_FLOOR, statistics.median(vals) + 14.0)

    # 滚动中位数一次算完：先 copy 成连续内存再排序，比 stride 视图直接
    # np.median 快 3 倍、比逐窗 statistics.median 快 8 倍（8400 窗 9ms → 1ms）。
    bases = None
    if np is not None and n:
        arr = np.asarray(vals, dtype=np.float32)
        padded = np.pad(arr, (half, half), mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, half * 2 + 1)
        bases = np.sort(windows.copy(), axis=1)[:, half]

    out: list[dict] = []
    i = 0
    while i < n:
        t, v = series[i]
        if bases is not None:
            base = float(bases[i])
        else:
            win = vals[max(0, i - half):i + half + 1]
            base = statistics.median(win) if win else -60.0
        diff = v - base
        if diff < AUD_SPIKE or v < floor:
            i += 1
            continue
        # 量一下这次峰值的持续长度
        j = i
        while j + 1 < n and series[j + 1][1] >= v - 8.0:
            j += 1
        hold = (j - i + 1) * step
        sharp = hold <= 0.09
        kind = "impact" if sharp else ("cry" if hold >= 0.5 else "swell")
        out.append({"t": round(t, 2), "hold": round(hold, 3), "kind": kind,
                    "db": round(v, 1),
                    "a": round(min(1.6, (diff - AUD_SPIKE) / 11.0 + (0.55 if sharp else 0.3)), 3)})
        i = j + 1
    return out


def _merge(motion: list[dict], spikes: list[dict]) -> list[dict]:
    """画面动作与声音冲击在 ±0.6 秒内算同一次事件，分数叠加。"""
    events: list[dict] = []
    used = [False] * len(spikes)
    for m in motion:
        ev = {"t0": m["t0"], "t1": m["t1"], "dur": m["dur"],
              "v": m["v"], "a": 0.0, "score": m["v"]}
        for i, s in enumerate(spikes):
            if used[i]:
                continue
            if m["t0"] - 0.6 <= s["t"] <= m["t1"] + 0.6:
                used[i] = True
                ev["a"] = max(ev["a"], s["a"])
                ev["score"] += s["a"] * 1.15
                break
        events.append(ev)
    for i, s in enumerate(spikes):
        if used[i]:
            continue
        events.append({"t0": round(s["t"] - 0.15, 2), "t1": round(s["t"] + 0.35, 2),
                       "dur": 0.5, "v": 0.0, "a": s["a"], "score": s["a"] * 0.8})
    events.sort(key=lambda e: e["t0"])
    # 2 秒内的事件并成一个峰值，避免同一巴掌算成三次
    merged: list[dict] = []
    for e in events:
        if merged and e["t0"] - merged[-1]["t1"] <= 1.6:
            last = merged[-1]
            last["t1"] = max(last["t1"], e["t1"])
            last["dur"] = round(last["t1"] - last["t0"], 2)
            last["v"] = max(last["v"], e["v"])
            last["a"] = max(last["a"], e["a"])
            last["score"] = round(max(last["score"], e["score"]) + 0.35, 3)
        else:
            merged.append(dict(e))
    for e in merged:
        e["score"] = round(e["score"], 3)
    # 单帧/双帧闪白、闪电、相机补光：没有声音、持续不到 0.35 秒的不算动作。
    # 真正的巴掌/摔门会带声音，纯画面动作也至少 3 帧（0.3 秒）。
    merged = [e for e in merged if e["a"] > 0 or e["dur"] >= 0.35]
    return merged


def _default_threads() -> int:
    """单集解码留给 ffmpeg 的线程数：4 路并发时别把 16 核全占满。"""
    cpus = os.cpu_count() or 4
    return max(1, min(3, cpus // 4))


def scan_episode(video: str | Path, has_audio: bool | None = None,
                 threads: int | None = None, timeout: float | None = None) -> dict:
    """一集的全部动作峰值。只保留有画面的强事件，最多 40 个。"""
    video = str(video)
    t_start = time.time()
    info: dict = {}
    if has_audio is None:
        try:
            info = ffmpegx.probe(video)
            has_audio = bool(info.get("has_audio"))
        except Exception:       # noqa: BLE001 探测失败就按有声试，失败会回退
            has_audio = True
    if timeout is None:
        dur = float(info.get("duration") or 0.0)
        timeout = max(120.0, min(900.0, dur * 8)) if dur else 600.0
    try:
        if np is None:
            raise RuntimeError("numpy 不可用，退回逐次解码")
        diff, audio = _scan_signals(video, has_audio,
                                    threads=threads or _default_threads(),
                                    timeout=timeout)
    except Exception as e:      # noqa: BLE001 合并解码失败 → 退回两条独立老路
        if "超时" in str(e):    # 已经卡住了，再跑两条老路只会再等一遍
            raise
        try:
            diff = _diff_series(video)
        except Exception:       # noqa: BLE001
            diff = []
        try:
            audio = _audio_series(video)
        except Exception:       # noqa: BLE001
            audio = []
    try:
        motion = _motion_runs(diff)
    except Exception:           # noqa: BLE001 画面读不了就算了，还有声音
        motion = []
    try:
        spikes = _audio_spikes(audio)
    except Exception:           # noqa: BLE001
        spikes = []
    events = _merge(motion, spikes)
    events = [e for e in events if e["score"] >= 0.85][:40]
    return {"events": events, "motion": len(motion), "spikes": len(spikes),
            "secs": round(time.time() - t_start, 2)}


# ── 整剧缓存 ─────────────────────────────────────────────────────────
def cache_path(name: str, root: Path) -> Path:
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "project"
    return root / "data" / "act" / f"{safe}.json"


def scan_project(proj: dict, root: Path, progress=None, force: bool = False) -> dict:
    """扫描整剧，结果按「剧名 + 每集首尾」存盘，重扫只补新集。"""
    path = cache_path(proj["name"], root)
    cache: dict = {}
    if path.exists() and not force:
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception:   # noqa: BLE001
            cache = {}
    eps: dict = cache.get("eps") or {}

    def stale(ep: dict) -> bool:
        r = eps.get(str(ep["idx"]))
        if not r:
            return True
        # 换了片源（同名不同文件 / 时长变了）就重扫，避免拿旧集的动作点
        return (r.get("file") != ep["file"]
                or abs(float(r.get("dur", -1)) - float(ep["dur"])) > 0.5)

    todo = [e for e in proj["episodes"] if force or stale(e)]
    done = 0
    failed: list[dict] = []
    total = max(1, len(todo))
    cpus = os.cpu_count() or 4
    workers = max(1, min(VISION_WORKERS, cpus // 4 or 1, len(todo) or 1))
    threads = max(1, min(3, cpus // max(1, workers)))

    def one(ep: dict) -> tuple[dict, dict]:
        try:
            res = scan_episode(ep["path"], ep.get("has_audio"),
                               threads=threads)
        except Exception as e:          # noqa: BLE001 单集失败不能拖垮整次导入
            print(f"[vision] 第{ep['idx']}集扫描失败：{e}", flush=True)
            res = {"events": [], "motion": 0, "spikes": 0, "secs": 0.0,
                   "error": str(e)[:200]}
        res["file"] = ep["file"]
        res["dur"] = round(float(ep["dur"]), 3)
        return ep, res

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for ep, res in pool.map(one, todo):
                eps[str(ep["idx"])] = res
                done += 1
                if res.get("error"):
                    failed.append({"ep": ep["idx"], "msg": res["error"]})
                if progress:
                    progress(done / total * 100.0, f"动作扫描 {done}/{total} 集")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": proj["name"], "eps": eps},
                               ensure_ascii=False), encoding="utf-8")
    all_events: list[dict] = []
    for e in proj["episodes"]:
        r = eps.get(str(e["idx"])) or {}
        for ev in r.get("events", []):
            all_events.append({
                "ep": e["idx"],
                "g0": round(e["offset"] + ev["t0"], 3),
                "g1": round(e["offset"] + ev["t1"], 3),
                "v": ev.get("v", 0.0), "a": ev.get("a", 0.0),
                "score": ev.get("score", 0.0),
            })
    return {"events": all_events, "eps": len(eps), "scanned": len(todo),
            "total": len(proj["episodes"]), "failed": failed}

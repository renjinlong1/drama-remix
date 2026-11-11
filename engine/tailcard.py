"""去尾帧：识别每集末尾的平台片尾定帧，并给出该裁掉多少秒。

短剧平台常在每集最后贴一张静止的片尾卡（「下集更精彩」/ App 引导 / 黑场定帧），
正片切到下一集时它会被一起带上，集与集之间显得割裂。

识别依据（只看得懂"画面有没有在动"，不需要语义模型）：

1. 抽每集最后 6 秒、8fps、96 宽灰度帧（rawvideo 管道，不落盘、不解码 PNG）；
2. **末尾必须是定帧**：最后 0.4 秒的帧间平均差全部 < 1.0（数字片源的静止画面
   编码后帧差接近 0；实拍慢镜头一般还有 2~15 的呼吸/推镜）；
3. 从末尾往前扩展连续静止段，碰到第一个 ≥1.0 的帧就停 —— 只吃**最后一段**，
   中间剧情里的静止镜头不会被连带；
4. 再往前补最多 0.3 秒的淡入/渐隐过渡（帧差 < 3.0 才算），**不把正片动作
   当卡片入场**：旧实现固定往前吃 1.2 秒，真实 1.5 秒的卡片会被裁成 2.25 秒；
5. 卡片长度 = 集长 − 起点。上限 4 秒（超过多半是长黑场，按 4 秒处理并降置信度），
   不足 0.45 秒不裁。

结果按 (路径, mtime, 时长, 版本) 缓存在 data/tailcard.json，同一集只分析一次。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

from . import ffmpegx

WINDOW = 6.0          # 只看最后 6 秒
FPS = 8.0             # 0.125 秒精度
WIDTH = 96
STILL = 1.0           # 定帧判定：相邻帧平均灰度差
FADE = 3.0            # 淡入/渐隐过渡的帧差上限
FADE_MAX = 0.3        # 过渡最多补这么久
TAIL_GUARD = 0.4      # 末尾至少要有这么长的定帧才算数
MIN_CARD = 0.45       # 短于这个不裁（避免误伤正常静止镜头）
MAX_CARD = 4.0        # 卡片长度上限
VERSION = "v2"

_LOCK = threading.Lock()
_CACHE_PATH = os.path.join("data", "tailcard.json")
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except OSError:
        pass


def _key(path: str, dur: float) -> str:
    try:
        mt = int(os.path.getmtime(path))
    except OSError:
        mt = 0
    h = hashlib.sha1(f"{os.path.normcase(os.path.abspath(path))}|{mt}".encode(),
                     usedforsecurity=False).hexdigest()[:16]
    return f"{VERSION}|{h}|{dur:.2f}"


def _frames(path: str, start: float, n: int):
    """从 start 起抽 n 帧，直接读裸灰度数据（比 PNG + PIL 快一个量级）。"""
    import numpy as np

    info = ffmpegx.probe(path)
    w = WIDTH
    h = max(2, int(round(w * info["height"] / max(1, info["width"]) / 2)) * 2)
    args = ["-loglevel", "error", "-ss", f"{start:.3f}", "-i", path,
            "-frames:v", str(n), "-vf", f"fps={FPS},scale={w}:{h},format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    data = ffmpegx.run_out(args)
    size = w * h
    if not data or len(data) < size:
        return None
    if len(data) % size:
        data = data[: len(data) // size * size]
    return np.frombuffer(data, dtype=np.uint8).reshape(-1, h, w).astype(np.int16)


def measure_ex(path: str, dur: float) -> tuple[float, str]:
    """识别这集末尾的片尾卡：返回 (秒数, 置信度)。

    置信度：high（定帧 ≥0.8s）/ mid（0.45~0.8s）/ low（超过 4s 上限或异常）/
    none（末尾没有定帧，不裁）。
    """
    import numpy as np

    if not path or not os.path.exists(path) or dur <= 3.0:
        return 0.0, "none"
    win = min(WINDOW, max(2.0, dur * 0.5))
    n = int(round(win * FPS)) + 2
    start = max(0.0, dur - win - 0.2)
    frames = _frames(path, start, n)
    if frames is None or len(frames) < max(4, int(round(0.6 * FPS))):
        return 0.0, "none"

    diffs = np.abs(frames[1:] - frames[:-1]).mean(axis=(1, 2)).astype(float)
    ts = [start + i / FPS for i in range(len(frames))]

    # 1) 末尾必须是定帧：最后 TAIL_GUARD 秒每一帧都要几乎不变
    tail_n = max(2, int(round(TAIL_GUARD * FPS)))
    if len(diffs) < tail_n or float(diffs[-tail_n:].max()) >= STILL:
        return 0.0, "none"

    # 2) 从末尾往前吃连续静止段；碰到第一帧"在动"就停，只取最后一段
    i = len(diffs) - 1
    while i >= 0 and diffs[i] < STILL:
        i -= 1
    still_start = ts[i + 1]
    still_len = dur - still_start

    # 3) 淡入/渐隐过渡最多再补 FADE_MAX（只有帧差 < FADE 才算过渡）
    j, extra = i, 0.0
    while j >= 0 and diffs[j] < FADE and extra + 1.0 / FPS <= FADE_MAX + 1e-9:
        j -= 1
        extra += 1.0 / FPS
    card = dur - ts[j + 1]

    if card < MIN_CARD:
        return 0.0, "none"
    conf = "high" if still_len >= 0.8 else "mid"
    if card > MAX_CARD:
        return round(MAX_CARD, 2), "low"
    return round(card, 2), conf


def card_len(path: str, dur: float) -> float:
    """带缓存的识别入口。"""
    k = _key(path, dur)
    with _LOCK:
        c = _load().get(k)
        if isinstance(c, list) and len(c) == 2:
            return float(c[0])
    sec, conf = measure_ex(path, dur)
    with _LOCK:
        _load()[k] = [sec, conf]
        _save()
    return sec


def trim_for(path: str, dur: float, mode: str,
             fixed: float) -> tuple[float, str]:
    """按模式返回 (该切掉的秒数, 依据)。

    依据：card / card-low 识别到定帧；none 末尾没有定帧（不裁）；
    fixed 固定秒数；off 不切。
    """
    if mode == "off":
        return 0.0, "off"
    if mode == "fixed":
        return round(max(0.0, min(MAX_CARD, float(fixed or 0))), 2), "fixed"
    k = _key(path, dur)
    with _LOCK:
        c = _load().get(k)
    if isinstance(c, list) and len(c) == 2:
        sec, conf = float(c[0]), str(c[1])
    else:
        sec, conf = measure_ex(path, dur)
        with _LOCK:
            _load()[k] = [sec, conf]
            _save()
    if conf == "none" or sec <= 0:
        return 0.0, "none"
    return sec, ("card" if conf != "low" else "card-low")

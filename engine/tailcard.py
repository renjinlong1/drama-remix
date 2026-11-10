"""去尾帧：识别每集末尾的平台片尾卡片。

短剧平台常在每集最后 2~8 秒贴一张静止的片尾卡（「下集更精彩」/ App 引导 / 黑场
定帧），正片切到下一集时它会被一起带上，集与集之间就显得割裂。

识别思路（engine/plan._apply_tail_trim 只在「集与集的接缝」上用这里的秒数）：
  1. 抽每集最后 15 秒、4fps、64 宽灰度帧（image2pipe，不落盘）；
  2. 末尾必须是"静止段"——最后 0.6s 帧间差 < 1.5，否则视为没有卡片，返回 0；
  3. 从静止段往前走：连续帧间差小（<6，字幕淡出/缓动都算卡片一部分）一路吃掉，
     直到碰到一个硬切（≥12）；必要时再往外多跨一帧硬切边界（黑场过渡）；
  4. 卡片起点 = 走停下的位置，卡长 = 集长 − 起点。上限 12 秒（防把长片尾字幕
     全吞了），不足 1.0 秒视为"识别不到"，上层按兜底 0.8 秒处理。

结果按 (路径, mtime, 时长, 版本) 缓存在 data/tailcard.json，同一集只分析一次。
"""

from __future__ import annotations

import hashlib
import io as _io
import json
import os
import threading

from . import ffmpegx

WINDOW = 15.0        # 只看每集最后 15 秒
FPS = 4.0
CUT = 12.0           # 硬切判定（帧间灰度均值差）
SOFT = 8.0           # 入场动画判定（帧间灰度均值差；正片字幕画面实测 ≤6.2）
STILL = 0.8           # 末尾静止判定（平均帧间差）
STILL_MAX = 1.5       # 静止段内单帧差上限
LEAD = 1.2            # 卡片入场动画最多算这么久
MAX_CARD = 6.0       # 卡长上限（超过这个长度多半是慢镜头，不是卡片）
MIN_CARD = 1.0       # 小于这个不算识别到（上层走兜底）
FALLBACK = 0.8       # 兜底秒数（识别不到的集）

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
    return f"v1|{h}|{dur:.2f}"


def _frames(path: str, start: float, n: int) -> list:
    """从 start 起抽 n 张 64 宽灰度帧，直接从管道读，不写临时文件。"""
    args = ["-loglevel", "error", "-ss", f"{start:.3f}", "-i", path,
            "-frames:v", str(n), "-vf",
            f"fps={FPS},scale=64:-2,format=gray", "-f", "image2pipe", "-vcodec", "png", "-"]
    data = ffmpegx.run_out(args)
    out, pos = [], 0
    while pos < len(data):
        end = data.find(b"IEND", pos)
        if end < 0:
            break
        end += 8
        out.append(_io.BytesIO(data[pos:end]))
        pos = end
    if not out:
        return []
    import numpy as np
    from PIL import Image
    imgs = []
    for buf in out:
        try:
            imgs.append(np.asarray(Image.open(buf), dtype=np.float32))
        except Exception:  # noqa: BLE001
            continue
    return imgs


def measure(path: str, dur: float) -> float:
    """识别这集末尾的片尾卡长度（秒）。识别不到返回 0.0。"""
    import numpy as np

    if not path or not os.path.exists(path) or dur <= 3.0:
        return 0.0
    win = min(WINDOW, dur * 0.4)
    n = int(win * FPS)
    imgs = _frames(path, max(0.0, dur - win - 0.3), n + 2)
    if len(imgs) < 8:
        return 0.0
    t0 = max(0.0, dur - win - 0.3)
    diffs = [float(np.abs(imgs[i] - imgs[i - 1]).mean()) for i in range(1, len(imgs))]
    # 1) 末尾必须是"几乎完全静止"：平台卡片是定帧，帧间差 ~0；
    #    缓推、慢走这类剧情镜头虽然差得少，但不会这么干净
    tail_n = max(3, int(0.8 * FPS))
    tail = diffs[-tail_n:]
    if sum(tail) / len(tail) >= STILL or max(tail) >= STILL_MAX:
        return 0.0
    # 2) 从末尾往回吃静止段，长度记 S
    i = len(diffs) - 1
    while i >= 0 and diffs[i] < STILL_MAX:
        i -= 1
    static_len = (len(diffs) - 1 - i) / FPS
    if static_len >= win - 0.3:                 # 整个窗口都静止：长片尾/定帧
        return round(min(win, MAX_CARD), 2)
    # 3) 卡片入场动画（放大/淡入/滑入）最多再算 LEAD 秒，别把前面的剧情动作吃进来
    lead_frames = int(LEAD * FPS)
    j, eaten = i, 0
    while j >= 0 and diffs[j] >= SOFT and eaten < lead_frames:
        j -= 1
        eaten += 1
    card = static_len + eaten / FPS
    if card < MIN_CARD:
        return 0.0
    return round(min(card, MAX_CARD), 2)


def card_len(path: str, dur: float) -> float:
    """带缓存的识别入口。"""
    k = _key(path, dur)
    with _LOCK:
        c = _load().get(k)
        if c is not None:
            return float(c)
    c = measure(path, dur)
    with _LOCK:
        _load()[k] = c
        _save()
    return c


def trim_for(path: str, dur: float, mode: str,
             fixed: float) -> tuple[float, str]:
    """按模式返回 (该切掉的秒数, 依据)。依据：card 识别到卡片 / fallback 兜底 /
    fixed 固定秒数 / off 不切。"""
    if mode == "off":
        return 0.0, "off"
    if mode == "fixed":
        return round(max(0.0, min(MAX_CARD, float(fixed or 0))), 2), "fixed"
    c = card_len(path, dur)
    return (c, "card") if c >= MIN_CARD else (FALLBACK, "fallback")

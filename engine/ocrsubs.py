"""硬字幕 OCR：把烧在画面上的台词识别成带时间码的字幕。

很多短剧原片没有外挂字幕，字幕是直接烧在画面里的（画面下方一段白色文字带）。
这里用离线 OCR（RapidOCR / ONNX，无需联网）在字幕带里逐帧找字，合并成 cue 写 srt，
后面整条顺剪流水线照常吃字幕，不需要改任何分析逻辑。

性能上有两层预筛，避免对「没有字幕」的帧白跑模型：
  1) 文字存在性：只保留有「亮字 + 暗边」且呈横向排布的帧；
  2) 变化检测：字幕没换字的连续帧直接复用上一帧结果。
实测 720×1280 单集 2 分钟，1.6 万帧级视频约 20-40 秒可识别完。
"""
from __future__ import annotations

import re
import shutil
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path

from . import ffmpegx

# 字幕可能出现的纵向区间（占画面高度比例）。取宽一点，靠后面的静态文字过滤兜住台标。
DEFAULT_BAND = (0.52, 0.99)
DEFAULT_FPS = 2.0

_ENGINE = None
_ENGINE_ERR = ""


# ── 引擎 ────────────────────────────────────────────────────────────
def available() -> tuple[bool, str]:
    """OCR 组件是否可用（未装时给出安装提示，不抛异常）。"""
    global _ENGINE_ERR
    if _ENGINE is not None:
        return True, ""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True, ""
    except Exception as e:  # noqa: BLE001
        _ENGINE_ERR = ("未安装画面字幕识别组件。请在工具目录执行："
                       "pip install rapidocr-onnxruntime  （首次需联网，之后完全离线）")
        return False, f"{_ENGINE_ERR}\n{e}"


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


# ── 文本处理 ────────────────────────────────────────────────────────
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_PUNCT = re.compile(r"[\s，。！？、,.!?…·:;\"'“”‘’()（）《》\-—_~]+")
_ONLY_NUM = re.compile(r"^[\d\s:：.\-—~%/]+$")


def _norm(t: str) -> str:
    return _PUNCT.sub("", (t or "").lower())


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _join(lines: list[str]) -> str:
    if any(_CJK.search(x) for x in lines):
        return "".join(lines)
    return " ".join(lines)


# ── 画面分析 ────────────────────────────────────────────────────────
def _text_mask(gray):
    """返回「像文字」的像素掩码；没有成片文字时返回 None。纯 numpy，约 3ms。"""
    import numpy as np

    bright = gray > 185
    if int(bright.sum()) < 80:
        return None
    pad = np.pad(gray, 6, mode="edge")
    h, w = gray.shape
    top = pad[0:h, 6:6 + w]
    bot = pad[12:12 + h, 6:6 + w]
    mask = bright & ((top < 95) | (bot < 95))
    rows = mask.sum(axis=1)
    if int((rows > max(8, w * 0.012)).sum()) < 3:
        return None
    return mask


def _sig(mask):
    """指纹取「整条字幕带的中间 80% 宽度」——既避开角落台标，又能感知字幕换字。"""
    import numpy as np

    h, w = mask.shape
    sub = mask[:, int(w * 0.10):int(w * 0.90)]
    if sub.size == 0:
        sub = mask
    gh, gw = 8, 32
    bh, bw = max(1, sub.shape[0] // gh), max(1, sub.shape[1] // gw)
    small = sub[:gh * bh, :gw * bw].reshape(gh, bh, gw, bw).mean(axis=(1, 3))
    return (small > 0.12).astype("uint8")


def _sig_sim(a, b) -> float:
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    diff = int((a != b).sum())
    return 1.0 - diff / a.size


def _ocr_frame(arr) -> tuple[str, float]:
    """识别一帧字幕带，返回 (文本, 置信度)。

    画面里可能同时有全息面板、道具上的字、台标，只挑「水平居中 + 位置最靠下」的那一组
    ——短剧字幕基本都在这条线上。
    """
    res, _ = _engine()(arr)
    if not res:
        return "", 0.0
    h, w = arr.shape[:2]
    cx = w / 2
    items = []
    for box, txt, sc in res:
        try:
            sc = float(sc)
        except (TypeError, ValueError):
            sc = 0.0
        txt = (txt or "").strip()
        if sc < 0.5 or len(_norm(txt)) < 1:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if x1 - x0 < w * 0.03:
            continue
        if abs((x0 + x1) / 2 - cx) > w * 0.24:
            continue
        items.append({"x0": x0, "x1": x1, "y0": y0, "y1": y1,
                      "text": txt, "score": sc})
    if not items:
        return "", 0.0
    # 按纵向聚成若干「文字块」，一块可能就是一行或两行字幕
    items.sort(key=lambda it: it["y0"])
    heights = sorted(it["y1"] - it["y0"] for it in items)
    lh = max(8.0, heights[len(heights) // 2])
    clusters: list[list[dict]] = []
    for it in items:
        if clusters and it["y0"] - clusters[-1][-1]["y1"] <= lh * 0.8:
            clusters[-1].append(it)
        else:
            clusters.append([it])
    # 字幕的特征：宽、水平居中、位置偏下。三样一起打分，挑最像字幕的那块
    best, best_score = None, -1.0
    for cl in clusters:
        x0 = min(it["x0"] for it in cl)
        x1 = max(it["x1"] for it in cl)
        y1 = max(it["y1"] for it in cl)
        width = (x1 - x0) / w
        off = abs((x0 + x1) / 2 - cx) / w
        score = width * 1.0 - off * 1.2 + (y1 / h) * 0.55
        if score > best_score:
            best, best_score = cl, score
    if not best:
        return "", 0.0
    best.sort(key=lambda it: it["y0"])
    lines: list[str] = []
    scores: list[float] = []
    for it in best:
        txt = it["text"]
        if lines and _norm(lines[-1]) == _norm(txt):
            continue
        if lines and _norm(txt).startswith(_norm(lines[-1])):
            lines[-1] = txt      # 同一行被拆成两段：留更完整的
            scores[-1] = it["score"]
            continue
        lines.append(txt)
        scores.append(it["score"])
    text = _join(lines).strip()
    if len(_norm(text)) < 2 or _ONLY_NUM.match(text):
        return "", 0.0
    return text, sum(scores) / len(scores)


# ── 逐集识别 ────────────────────────────────────────────────────────
def _grab_band(video: str | Path, out_dir: Path, fps: float, band: tuple[float, float]):
    lo, hi = band
    vf = f"fps={fps},crop=iw:ih*{hi - lo:.4f}:0:ih*{lo:.4f}"
    ffmpegx.run(["-y", "-i", str(video), "-vf", vf, "-q:v", "3",
                 "-vsync", "0", str(out_dir / "%06d.jpg")], timeout=1800)
    return sorted(out_dir.glob("*.jpg"))


def ocr_video(video: str | Path, fps: float = DEFAULT_FPS,
              band: tuple[float, float] = DEFAULT_BAND,
              progress=None, label: str = "") -> dict:
    """识别一集画面里的硬字幕，返回 {cues, frames, hit_frames, ocr_calls, secs}。"""
    import numpy as np
    from PIL import Image

    ok, msg = available()
    if not ok:
        raise RuntimeError(msg)
    _engine()  # 预热，避免第一帧计入耗时

    t_start = time.time()
    info = ffmpegx.probe(video)
    dur = float(info.get("duration") or 0)
    if dur <= 0:
        raise RuntimeError(f"读不到视频时长：{Path(video).name}")

    tmp = Path(tempfile.mkdtemp(prefix="ocr_"))
    try:
        frames = _grab_band(video, tmp, fps, band)
        total = len(frames)
        obs: list[tuple[float, str, float]] = []
        hit = 0
        calls = 0
        prev_sig = None
        prev_text = ""
        for i, f in enumerate(frames):
            t = i / fps
            with Image.open(f) as im:
                rgb = im.convert("RGB")
                arr = np.asarray(rgb)
            gray = np.asarray(Image.fromarray(arr).convert("L"), dtype=np.int16)
            mask = _text_mask(gray)
            if mask is None:
                prev_sig, prev_text = None, ""
                continue
            hit += 1
            sig = _sig(mask)
            if prev_sig is not None and prev_text and _sig_sim(sig, prev_sig) > 0.985:
                obs.append((t, prev_text, 1.0))
                prev_sig = sig
                continue
            text, score = _ocr_frame(arr)
            calls += 1
            prev_sig, prev_text = sig, text
            if text:
                obs.append((t, text, score))
            if progress and i % 30 == 0:
                prefix = f"{label} · " if label else ""
                progress(i / max(1, total), f"{prefix}识别字幕 {i}/{total} 帧")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    cues = _build_cues(obs, fps, dur, len(frames))
    return {"cues": cues, "frames": len(frames), "hit_frames": hit,
            "ocr_calls": calls, "secs": round(time.time() - t_start, 1)}


def _build_cues(obs: list[tuple[float, str, float]], fps: float, dur: float,
                n_frames: int = 0) -> list[dict]:
    """把逐帧识别结果合并成 cue：同句连帧合并，做静态台标过滤。"""
    if not obs:
        return []
    # 1) 静态文字（台标/水印/常驻面板）过滤：占了大半时长才算台标，真字幕很少这么长
    freq: dict[str, int] = {}
    for _, text, _ in obs:
        k = _norm(text)
        freq[k] = freq.get(k, 0) + 1
    total = max(1, n_frames or len(obs))
    obs = [o for o in obs
           if not (freq.get(_norm(o[1]), 0) > total * 0.55 and len(_norm(o[1])) <= 16)
           and freq.get(_norm(o[1]), 0) <= total * 0.85]

    # 2) 连续相似文本合并
    step = 1.0 / fps
    groups: list[dict] = []
    for t, text, score in obs:
        if groups and _similar(_norm(groups[-1]["text"]), _norm(text)) >= 0.72 \
                and t - groups[-1]["t1"] <= step * 2.2:
            g = groups[-1]
            g["t1"] = t
            if len(_norm(text)) > len(_norm(g["text"])):
                g["text"] = text        # 保留更完整的那次识别
            g["scores"].append(score)
        else:
            groups.append({"t0": t, "t1": t, "text": text, "scores": [score]})

    cues: list[dict] = []
    for g in groups:
        start = max(0.0, g["t0"] - step * 0.45)
        end = min(dur, g["t1"] + step * 0.9)
        if end - start < 0.35 or len(_norm(g["text"])) < 2:
            continue
        # 一条字幕很少停留超过 6 秒：识别卡住时切成几段，别让它吞掉后面的对白
        span = end - start
        if span > 6.0:
            n = int(min(6, max(2, round(span / 3.5))))
            for k in range(n):
                s0 = start + span * k / n
                s1 = start + span * (k + 1) / n
                cues.append({"i": len(cues), "start": round(s0, 3),
                             "end": round(s1, 3), "text": g["text"]})
            continue
        cues.append({"i": len(cues), "start": round(start, 3),
                     "end": round(end, 3), "text": g["text"]})
    # 3) 时间轴收口：前一条不要压住后一条
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"] - 0.05:
            cues[i]["end"] = round(max(cues[i]["start"] + 0.3,
                                       cues[i + 1]["start"] - 0.05), 3)
    return cues


# ── srt 输出 ────────────────────────────────────────────────────────
def _ts(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms, s = 0, s + 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[dict], path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_ts(c['start'])} --> {_ts(c['end'])}", c["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)

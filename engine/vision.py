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
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpegx

DIFF_FPS = 10
DIFF_W = 128
DIFF_THRESHOLD = 42      # 单像素灰度差 > 42 记为"变了"
MOVE_PBLACK = 80         # pblack 低于这个值 = 这一帧有大面积变化
MIN_RUN = 2              # 连续 ≥2 帧（0.2 秒）才算动作，单帧是剪辑点
AUD_WIN = 0.1
AUD_SPIKE = 9.0          # 比滚动中位数高 9 dB
AUD_FLOOR = -18.0        # 绝对下限：太小声的窗口不参与（避免底噪抖动）

_BF = re.compile(r"pblack:(\d+)\s+pts:\d+\s+t:([\d.]+)")
_PEAK = re.compile(r"lavfi\.astats\.(\d+)\.Peak_level=(-?[\d.]+|inf|-inf|nan)", re.I)
_PTS = re.compile(r"^frame:(\d+)\s+pts:\d+\s+pts_time:([\d.]+)")


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


# ── 合成事件 ─────────────────────────────────────────────────────────
def _motion_runs(series: list[tuple[float, int]]) -> list[dict]:
    """把连续低 pblack 的帧并成"动作段"，单帧突变（剪辑点）丢弃。"""
    runs: list[dict] = []
    cur: list[tuple[float, int]] = []
    for t, pb in series:
        if pb <= MOVE_PBLACK:
            cur.append((t, pb))
        else:
            if len(cur) >= MIN_RUN:
                runs.append(_run_dict(cur))
            cur = []
    if len(cur) >= MIN_RUN:
        runs.append(_run_dict(cur))
    return runs


def _run_dict(cur: list[tuple[float, int]]) -> dict:
    t0, t1 = cur[0][0], cur[-1][0]
    depth = statistics.mean(max(0, MOVE_PBLACK - pb) for _, pb in cur) / MOVE_PBLACK
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
    half = 12                      # ±0.25 秒（每窗 ≈21ms）
    # 整体音量很低的片子（手机翻录、压得很狠）不能用固定的绝对下限，
    # 按整体中位数往下放一档，否则一句都挑不出来
    floor = min(AUD_FLOOR, statistics.median(vals) + 14.0)
    out: list[dict] = []
    i = 0
    while i < n:
        t, v = series[i]
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
        hold = (j - i + 1) * 0.021
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
    return merged


def scan_episode(video: str | Path) -> dict:
    """一集的全部动作峰值。只保留有画面的强事件，最多 40 个。"""
    video = str(video)
    t_start = time.time()
    try:
        motion = _motion_runs(_diff_series(video))
    except Exception:       # noqa: BLE001 画面读不了就算了，还有声音
        motion = []
    try:
        spikes = _audio_spikes(_audio_series(video))
    except Exception:       # noqa: BLE001
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
    total = max(1, len(todo))

    def one(ep: dict) -> tuple[dict, dict]:
        res = scan_episode(ep["path"])
        res["file"] = ep["file"]
        res["dur"] = round(float(ep["dur"]), 3)
        return ep, res

    if todo:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for ep, res in pool.map(one, todo):
                eps[str(ep["idx"])] = res
                done += 1
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
            "total": len(proj["episodes"])}

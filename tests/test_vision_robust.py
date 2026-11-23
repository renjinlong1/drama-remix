"""画面/声音信号层的稳健性测试：不同画质、低音量、闪白、流式 PCM。

不依赖视频文件，直接用合成的 pblack / dB 序列驱动，跑得快、断言稳定。
覆盖：
  A 自适应运动阈值：干净片与高噪声片都能抓同一段动作，纯噪声不误报
  B 闪白/闪电过滤：无声音的 2 帧突变不算动作，有声音则保留
  C 低音量片源仍能找到脆响
  D 流式 PCM：整段读与分块读结果一致（块边界任意切）
  E 单集扫描失败不影响整剧，失败集被记录

用法：runtime\\python.exe tests\\test_vision_robust.py
输出：tests/results/vision_robust.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import vision as V  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def pb_series(baseline: int, lo: float, hi: float, dip_to: int,
              dip_start: float = 10.0, dip_len: float = 0.6) -> list[tuple[float, int]]:
    out = []
    t = 0.0
    while t < hi:
        pb = dip_to if dip_start <= t < dip_start + dip_len else baseline
        out.append((round(t, 2), pb))
        t += 1.0 / V.DIFF_FPS
    return out


def test_threshold() -> None:
    print("\n[A] 自适应运动阈值")
    clean = pb_series(98, 0, 20, 50)
    noisy = pb_series(88, 0, 20, 50)
    tc, tn = V._move_threshold(clean), V._move_threshold(noisy)
    ck("干净片阈值更宽松（≥82）", tc >= 82, tc)
    ck("噪声片阈值更严格（≤78）", tn <= 78, tn)
    ck("阈值不越界", V.MOVE_MIN <= tn <= V.MOVE_PBLACK + 8 and
       V.MOVE_MIN <= tc <= V.MOVE_PBLACK + 8, (tn, tc))

    rc = V._motion_runs(clean)
    rn = V._motion_runs(noisy)
    ck("干净片抓到一段动作", len(rc) == 1, rc)
    ck("噪声片抓到同一段动作", len(rn) == 1, rn)
    if rc and rn:
        ck("两种画质时长接近（±0.15s）",
           abs(rc[0]["dur"] - rn[0]["dur"]) <= 0.15, (rc[0]["dur"], rn[0]["dur"]))
        ck("两种画质强度接近（±0.25）",
           abs(rc[0]["v"] - rn[0]["v"]) <= 0.25, (rc[0]["v"], rn[0]["v"]))

    flat_noise = [(i / 10.0, 88) for i in range(200)]
    ck("整集稳定噪声不误报", V._motion_runs(flat_noise) == [],
       V._motion_runs(flat_noise))

    # 三档画质基线（干净 / 一般压缩 / 高噪声）都要抓得到同一段动作
    strengths = []
    for base in (98, 90, 84):
        runs = V._motion_runs(pb_series(base, 0, 20, 45))
        ck(f"基线 {base} 抓到动作", len(runs) == 1, runs)
        if runs:
            strengths.append(runs[0]["v"])
    ck("三档画质强度差 ≤0.3",
       len(strengths) == 3 and max(strengths) - min(strengths) <= 0.3, strengths)


def test_flash() -> None:
    print("\n[B] 闪白 / 闪电过滤")
    s = pb_series(98, 0, 12, 20, dip_start=5.0, dip_len=0.2)
    runs = V._motion_runs(s)
    ck("2 帧突变先被识别为动作段", len(runs) == 1, runs)
    ck("没有声音的闪白被过滤", V._merge(runs, []) == [],
       V._merge(runs, []))
    spike = [{"t": 5.05, "hold": 0.02, "kind": "impact", "db": -3.0, "a": 1.2}]
    kept = V._merge(runs, spike)
    ck("带声音的同一段保留", len(kept) == 1, kept)
    if kept:
        ck("声音计入了事件分", kept[0]["a"] > 0 and kept[0]["score"] > 0.8, kept[0])


def test_low_volume() -> None:
    print("\n[C] 低音量片源")
    step = 0.02
    n = int(30 / step)
    quiet = [(i * step, -55.0) for i in range(n)]
    loud = list(quiet)
    for i in range(400, 403):
        loud[i] = (loud[i][0], -22.0)
    sp = V._audio_spikes(loud)
    ck("安静片源仍能找到脆响", len(sp) >= 1, sp[:2])
    if sp:
        ck("脆响落点准确（±0.1s）", abs(sp[0]["t"] - 8.0) <= 0.1, sp[0]["t"])
        ck("脆响被判定为冲击", sp[0]["kind"] == "impact", sp[0]["kind"])
    ck("全程安静不误报", V._audio_spikes(quiet) == [], V._audio_spikes(quiet)[:2])

    # 向量化滚动中位数必须与旧的逐窗 statistics.median 完全一致
    import random
    import statistics
    random.seed(3)
    n = 3000
    series = [(i * 0.0213, -30 + random.uniform(-2, 2)) for i in range(n)]
    for c in (300, 900, 1500, 2400):
        for k in range(c, c + 3):
            series[k] = (series[k][0], -10.0)

    def ref(series):
        vals = [v for _, v in series]
        m = len(vals)
        half = 12
        floor = min(V.AUD_FLOOR, statistics.median(vals) + 14.0)
        out = []
        i = 0
        while i < m:
            t, v = series[i]
            win = vals[max(0, i - half):i + half + 1]
            base = statistics.median(win) if win else -60.0
            if v - base < V.AUD_SPIKE or v < floor:
                i += 1
                continue
            j = i
            while j + 1 < m and series[j + 1][1] >= v - 8.0:
                j += 1
            hold = (j - i + 1) * 0.0213
            out.append((round(t, 2), round(hold, 3),
                        "impact" if hold <= 0.09 else ("cry" if hold >= 0.5 else "swell")))
            i = j + 1
        return out

    got = [(x["t"], x["hold"], x["kind"]) for x in V._audio_spikes(series)]
    ck("向量化与逐窗实现结果一致", got == ref(series), (len(got), len(ref(series))))


def test_pcm_stream() -> None:
    print("\n[D] 流式 PCM")
    import numpy as np
    rng = np.random.default_rng(7)
    a = (rng.normal(0, 3000, 48000 * 3)).astype("<i2")
    pcm = a.tobytes()
    full = V._pcm_series(pcm, 48000)

    # 参考实现：一次性 reshape 求峰值
    n = len(a) // V.AUD_BLOCK
    blocks = a[: n * V.AUD_BLOCK].reshape(n, V.AUD_BLOCK).astype(np.int32)
    peak = np.maximum(blocks.max(axis=1), -blocks.min(axis=1)) / 32768.0
    ref = 20 * np.log10(np.maximum(peak, 1e-7))
    ck("整段读块数与参考一致", len(full) == n, (len(full), n))
    if full and len(full) == n:
        diff = max(abs(full[i][1] - round(float(ref[i]), 2)) for i in range(n))
        ck("整段读数值与参考一致（<0.01dB）", diff < 0.01, diff)

    # 任意字节切分（含奇数长度）结果必须一致
    out, carry, idx = [], b"", 0
    cuts = [1, 999, 2047, 4097, 100003]
    prev = 0
    for c in cuts + [len(pcm)]:
        rows, carry, idx = V._pcm_chunk_peaks(pcm[prev:c], carry, 48000, idx)
        out.extend(rows)
        prev = c
    ck("分块读块数一致", len(out) == len(full), (len(out), len(full)))
    ck("分块读时间戳连续",
       all(abs(out[i][0] - i * V.AUD_BLOCK / 48000) < 1e-6 for i in range(len(out))),
       "timestamp")
    ck("分块读数值一致",
       all(abs(out[i][1] - full[i][1]) < 1e-9 for i in range(min(len(out), len(full)))),
       "value")


def test_failure_isolation() -> None:
    print("\n[E] 单集失败隔离")
    proj = {"name": "__vis_test__", "total": 3.0, "episodes": [
        {"idx": i, "path": f"x{i}.mp4", "file": f"{i}.mp4", "dur": 1.0,
         "has_audio": False, "offset": float(i - 1)}
        for i in (1, 2, 3)]}
    old = V.scan_episode

    def fake(path, has_audio=None, threads=None, timeout=None, use_hw=None):
        if path == "x2.mp4":
            raise RuntimeError("坏文件")
        return {"events": [{"t0": 0.1, "t1": 0.3, "dur": 0.2, "v": 1.0,
                            "a": 0.0, "score": 1.0}],
                "motion": 1, "spikes": 0, "secs": 0.01}

    V.scan_episode = fake
    try:
        r = V.scan_project(proj, ROOT, force=True)
    finally:
        V.scan_episode = old
        try:
            V.cache_path(proj["name"], ROOT).unlink()
        except OSError:
            pass
    ck("失败集被记录", len(r["failed"]) == 1 and r["failed"][0]["ep"] == 2, r["failed"])
    ck("其余集正常产出", len(r["events"]) == 2, r["events"])
    ck("扫描统计完整", r["eps"] == 3 and r["scanned"] == 3, r)


def test_bad_files() -> None:
    print("\n[G] 导入容错：坏文件跳过")
    import shutil
    import tempfile
    sys.path.insert(0, str(ROOT))
    from engine import project as P

    d = Path(tempfile.gettempdir()) / "dc_bad_import"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    src = ROOT / "tests" / "fixtures" / "h264_360x640_6s" / "01.mp4"
    for i in (1, 2):
        shutil.copyfile(src, d / f"{i:02d}.mp4")
    (d / "03.mp4").write_bytes(b"not a video at all")
    try:
        proj = P.scan(d, "__bad_import__")
        ck("可读文件正常导入", len(proj["episodes"]) == 2, len(proj["episodes"]))
        ck("坏文件被记录并跳过",
           len(proj.get("skipped") or []) == 1 and proj["skipped"][0]["file"] == "03.mp4",
           proj.get("skipped"))
    except Exception as e:      # noqa: BLE001
        ck("可读文件正常导入", False, e)
        ck("坏文件被记录并跳过", False, e)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    d2 = Path(tempfile.gettempdir()) / "dc_bad_import2"
    shutil.rmtree(d2, ignore_errors=True)
    d2.mkdir(parents=True)
    (d2 / "01.mp4").write_bytes(b"broken")
    try:
        P.scan(d2, "__bad_import2__")
        ck("全部不可读时明确报错", False, "没有抛错")
    except RuntimeError as e:
        ck("全部不可读时明确报错", "读不出来" in str(e), str(e))
    finally:
        shutil.rmtree(d2, ignore_errors=True)


def main() -> int:
    print("画面/声音信号稳健性")
    test_threshold()
    test_flash()
    test_low_volume()
    test_pcm_stream()
    test_failure_isolation()
    test_bad_files()
    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "vision_robust.json").write_text(json.dumps(
        {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

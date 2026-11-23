"""硬件解码收益基准（可复现）。

对同一段 3 分钟 720x1280 有运动 + 有声音的片源，分别用软件解码与
ffmpeg 编译支持的每个硬件后端，量三件事：

  scan_single   本工具真实的合并扫描命令（fps/scale/gray/tblend/blackframe + PCM）
  scan_parallel 4 路并发（scan_project 的实际并发度）
  pure_decode   纯解码丢弃（-f null，无滤镜），用来区分"解码本身"和"整条链路"

用法：
  runtime\\python.exe tests\\prof_hwaccel.py
输出：tests/results/hwaccel_perf.json，并在控制台打印对比表。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from engine import ffmpegx, vision, hwaccel  # noqa: E402
from prof_video import _clip  # noqa: E402

RESULTS = ROOT / "tests" / "results"
WORKERS = 4


def scan_cmd(clip: str, name: str | None) -> list[str]:
    hwaccel.set_mode("off")            # 让 _scan_cmd 不自动注入，由本脚本自己控制
    cmd, _ = vision._scan_cmd(clip, True, 2, use_hw=False)
    if name:
        i = cmd.index("-i")
        cmd = cmd[:i] + ["-hwaccel", name] + cmd[i:]
    return cmd


def scan_once(cmd: list[str], sr: int) -> dict:
    t = time.time()
    s, a, rc, tail, to, kill = vision._scan_once(cmd, True, sr, 600)
    rc = rc - 2 ** 32 if isinstance(rc, int) and rc > 2 ** 31 else rc
    return {"secs": round(time.time() - t, 2), "diff": len(s), "audio": len(a),
            "rc": rc, "timeout": to or kill,
            "ok": rc == 0 and len(s) > 0 and len(a) > 0 and not (to or kill)}


def pure_decode(clip: str, name: str | None) -> dict:
    cmd = [ffmpegx.ffmpeg(), "-hide_banner", "-nostdin", "-nostats", "-v", "error"]
    if name:
        cmd += ["-hwaccel", name]
    cmd += ["-i", clip, "-an", "-f", "null", "-"]
    t = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=300,
                           creationflags=ffmpegx.CREATE_NO_WINDOW)
        rc = p.returncode - 2 ** 32 if p.returncode > 2 ** 31 else p.returncode
    except Exception as e:  # noqa: BLE001
        return {"secs": -1, "rc": -1, "ok": False, "error": str(e)}
    return {"secs": round(time.time() - t, 2), "rc": rc, "ok": rc == 0}


def main() -> int:
    clip = str(_clip())
    info = ffmpegx.probe(clip)
    sr = info["sample_rate"] or 48000
    backends = [None] + hwaccel.compiled_methods()
    out: dict = {"clip": clip, "duration": info["duration"],
                 "resolution": f"{info['width']}x{info['height']}",
                 "workers": WORKERS, "backends": {}}
    print(f"片源 {out['resolution']} / {out['duration']}s，后端 {backends[1:]}\n")

    for name in backends:
        key = name or "software"
        cmd = scan_cmd(clip, name)
        single = scan_once(cmd, sr)
        t = time.time()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            par = list(pool.map(lambda _: scan_once(cmd, sr), range(WORKERS)))
        parallel = round(time.time() - t, 2)
        pd = pure_decode(clip, name)
        out["backends"][key] = {"scan_single": single,
                                "scan_parallel_secs": parallel,
                                "pure_decode": pd}
        flag = "" if single["ok"] else "  ← 解码失败，数据无效"
        print(f"{key:10s} 单路 {single['secs']:6.2f}s | "
              f"4路并发 {parallel:6.2f}s | 纯解码 {pd['secs']:6.2f}s | "
              f"diff/audio {single['diff']}/{single['audio']} rc={single['rc']}{flag}")

    sw = out["backends"]["software"]
    print("\n相对软件解码（>1 表示更慢，仅统计成功产出数据的后端）：")
    for key, v in out["backends"].items():
        if key == "software" or not v["scan_single"]["ok"]:
            continue
        print(f"  {key:10s} 单路 ×{v['scan_single']['secs'] / max(0.01, sw['scan_single']['secs']):.2f} | "
              f"4路 ×{v['scan_parallel_secs'] / max(0.01, sw['scan_parallel_secs']):.2f} | "
              f"纯解码 ×{v['pure_decode']['secs'] / max(0.01, sw['pure_decode']['secs']):.2f}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "hwaccel_perf.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    hwaccel.set_mode("auto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

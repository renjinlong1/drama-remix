"""尾帧识别边界测试：合成素材覆盖常见与极端情况。

用例：
  A 末尾 1.5s 冻结   → 应识别 1.3~1.9s
  B 末尾 0.6s 冻结   → 应识别 0.45~0.95s（不足 1 秒也要裁）
  C 全程运动         → 0（不能误裁）
  D 末尾 5s 冻结     → 4.0s 上限、低置信
  E 中间静止后继续动 → 0（只裁最后一段，不碰中间静止镜头）
  F 过短视频         → 0

用法：runtime\\python.exe tests/test_tailcard.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import ffmpegx, tailcard  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "_tail"
RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def make(name: str, dur: float, vf: str = "") -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{name}.mp4"
    if out.exists():
        return out
    args = [str(ROOT / "bin" / "ffmpeg.exe"), "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=360x640:rate=25:duration={dur}"]
    if vf:
        args += ["-vf", vf]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(args, check=True, capture_output=True)
    return out


def make_middle_still() -> Path:
    """3 秒运动 + 2 秒静止 + 3 秒运动：中间的静止段不该被当成片尾卡。"""
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "middle_still.mp4"
    if out.exists():
        return out
    args = [str(ROOT / "bin" / "ffmpeg.exe"), "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=25:duration=3",
            "-f", "lavfi", "-i", "color=c=gray:s=360x640:rate=25:duration=2",
            "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=25:duration=3",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(args, check=True, capture_output=True)
    return out


def main() -> int:
    t0 = time.time()
    print("生成合成素材…")
    a = make("still_1_5", 6.5, "tpad=stop_mode=clone:stop_duration=1.5")
    b = make("still_0_6", 6.5, "tpad=stop_mode=clone:stop_duration=0.6")
    c = make("moving", 8.0)
    d = make("still_5", 6.5, "tpad=stop_mode=clone:stop_duration=5")
    e = make_middle_still()
    f = make("short", 2.5)
    print(f"  用时 {time.time() - t0:.1f}s")

    for item in (a, b, c, d, e, f):
        info = ffmpegx.probe(str(item))
        sec, conf = tailcard.measure_ex(str(item), info["duration"])
        print(f"  {item.stem:14s} dur={info['duration']:.2f}s → {sec}s / {conf}")

    info = lambda p: ffmpegx.probe(str(p))["duration"]
    sec, conf = tailcard.measure_ex(str(a), info(a))
    ck("A 末尾 1.5s 冻结", 1.3 <= sec <= 1.9, f"{sec}s")
    ck("A 高置信", conf == "high", conf)

    sec, conf = tailcard.measure_ex(str(b), info(b))
    ck("B 末尾 0.6s 冻结仍要裁", 0.45 <= sec <= 0.95, f"{sec}s")
    ck("B 置信度 mid", conf in ("mid", "high"), conf)

    sec, conf = tailcard.measure_ex(str(c), info(c))
    ck("C 全程运动不裁", sec == 0.0 and conf == "none", f"{sec}s/{conf}")

    sec, conf = tailcard.measure_ex(str(d), info(d))
    ck("D 超长定帧按 4s 上限", sec == 4.0, f"{sec}s")
    ck("D 低置信", conf == "low", conf)

    sec, conf = tailcard.measure_ex(str(e), info(e))
    ck("E 中间静止、末尾在动 → 不裁", sec == 0.0, f"{sec}s/{conf}")

    sec, conf = tailcard.measure_ex(str(f), info(f))
    ck("F 过短视频不处理", sec == 0.0 and conf == "none", f"{sec}s/{conf}")

    ck("缺失文件不抛异常", tailcard.measure_ex(str(OUT / "nope.mp4"), 10.0) == (0.0, "none"), "")

    # trim_for 的行为
    sec, by = tailcard.trim_for(str(c), info(c), "auto", 2.0)
    ck("auto 未识别时不瞎兜底", sec == 0.0 and by == "none", f"{sec}/{by}")
    sec, by = tailcard.trim_for(str(a), info(a), "fixed", 2.0)
    ck("fixed 模式按设定秒数", sec == 2.0 and by == "fixed", f"{sec}/{by}")
    sec, by = tailcard.trim_for(str(a), info(a), "off", 2.0)
    ck("off 模式不裁", sec == 0.0 and by == "off", f"{sec}/{by}")

    shutil.rmtree(OUT, ignore_errors=True)
    bad = [x for x in CHECKS if not x["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "tailcard.json").write_text(json.dumps(
        {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

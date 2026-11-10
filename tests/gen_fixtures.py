"""生成覆盖不同容器 / 编码 / 分辨率 / 时长 / 音轨的合成测试素材。

用法：runtime\\python.exe tests\\gen_fixtures.py
产物：tests/fixtures/<case>/01.<ext> + 01.srt，以及 manifest.json
素材由 ffmpeg lavfi 合成（无需真实片源、可重复生成），目录已被 .gitignore 忽略。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 嵌入式 Python 的 ._pth 不会把脚本目录放进 sys.path，显式补上
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ROOT, FIX  # noqa: E402

FF = ROOT / "bin" / "ffmpeg.exe"
CREATE_NO_WINDOW = 0x08000000
EPISODES = 8

# name, ext, vcodec, w, h, dur, audio, 额外编码参数
CASES = [
    ("h264_360x640_6s",      ".mp4",  "libx264", 360, 640, 6,  True,
     ["-preset", "ultrafast", "-crf", "30"]),
    ("h264_720x1280_15s",    ".mp4",  "libx264", 720, 1280, 15, True,
     ["-preset", "ultrafast", "-crf", "30"]),
    ("h264_720x1280_45s",    ".mp4",  "libx264", 720, 1280, 45, True,
     ["-preset", "ultrafast", "-crf", "32"]),
    ("mkv_720x1280_15s",     ".mkv",  "libx264", 720, 1280, 15, True,
     ["-preset", "ultrafast", "-crf", "30"]),
    ("mov_360x640_8s",       ".mov",  "libx264", 360, 640, 8,  True,
     ["-preset", "ultrafast", "-crf", "30"]),
    ("avi_360x640_8s",       ".avi",  "mpeg4",   360, 640, 8,  True,
     ["-q:v", "5"]),
    ("webm_360x640_8s",      ".webm", "libvpx",  360, 640, 8,  True,
     ["-deadline", "realtime", "-cpu-used", "8", "-b:v", "600k"]),
    ("h264_1280x720_10s",    ".mp4",  "libx264", 1280, 720, 10, True,
     ["-preset", "ultrafast", "-crf", "30"]),
    ("h264_360x640_6s_mute", ".mp4",  "libx264", 360, 640, 6,  False,
     ["-preset", "ultrafast", "-crf", "30"]),
]

LINES = ["滚出去！你算什么东西", "董事长来了", "亲子鉴定结果出来了",
         "三年后我回来了", "这到底是谁", "离婚吧，休想", "原来是你", "你等着"]


def _tc(x: float) -> str:
    m = int(x // 60)
    s = x - m * 60
    return f"00:{m:02d}:{s:06.3f}".replace(".", ",")


def srt_for(dur: float) -> str:
    n = max(2, min(len(LINES), int(dur // 2)))
    step = dur / n
    blocks = []
    for i in range(n):
        s = i * step + 0.2
        e = min(dur - 0.2, s + min(2.0, step - 0.4))
        blocks.append(f"{i + 1}\n{_tc(s)} --> {_tc(e)}\n{LINES[i % len(LINES)]}\n")
    return "\n".join(blocks)


def build_case(c) -> dict:
    name, ext, vcodec, w, h, dur, audio, extra = c
    d = FIX / name
    d.mkdir(parents=True, exist_ok=True)
    out = d / ("01" + ext)
    cmd = [str(FF), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=25:duration={dur}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={dur}"]
    cmd += ["-map", "0:v:0"]
    if audio:
        # WebM 只收 Opus / Vorbis 音轨，AAC 会直接写不了文件头
        acodec = "libopus" if ext == ".webm" else "aac"
        cmd += ["-map", "1:a:0", "-c:a", acodec, "-b:a", "64k"]
    cmd += ["-c:v", vcodec, *extra, "-pix_fmt", "yuv420p", "-shortest", str(out)]
    r = subprocess.run(cmd, capture_output=True, timeout=300,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:])
    # 真实短剧是多集连播；每个用例铺成 8 集，硬链接复用同一文件不额外占空间。
    # 这样总时长 > 30 秒，plan.build 的成片下限才能走到，也能覆盖跨集顺剪。
    srt = srt_for(dur)
    for i in range(1, EPISODES + 1):
        v = d / f"{i:02d}{ext}"
        if i > 1:
            if v.exists():
                v.unlink()
            try:
                os.link(out, v)
            except OSError:
                shutil.copy2(out, v)
        (d / f"{i:02d}.srt").write_text(srt, encoding="utf-8")
    return {"name": name, "ext": ext, "vcodec": vcodec, "w": w, "h": h,
            "dur": dur, "audio": audio, "folder": str(d), "file": out.name,
            "eps": EPISODES, "total": dur * EPISODES}


def main() -> int:
    FIX.mkdir(parents=True, exist_ok=True)
    t = time.time()
    built = []
    for c in CASES:
        try:
            m = build_case(c)
            built.append(m)
            print("  [OK]  ", m["name"])
        except Exception as e:
            print("  [SKIP]", c[0], "->", e)
    ver = subprocess.run([str(FF), "-version"], capture_output=True, text=True,
                         creationflags=CREATE_NO_WINDOW).stdout.splitlines()[0]
    (FIX / "manifest.json").write_text(
        json.dumps({"ffmpeg": ver, "cases": built,
                    "generated": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成 {len(built)}/{len(CASES)} 个素材，用时 {time.time() - t:.1f}s")
    return 0 if built else 1


if __name__ == "__main__":
    raise SystemExit(main())

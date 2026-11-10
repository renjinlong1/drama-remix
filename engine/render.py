"""渲染管线：逐段裁切 → 统一规格 → 拼接 → 叠加角标 + 原声归一。

短剧二创的成片要求很具体：
- 所有片段必须编码参数完全一致，否则 concat 会花屏
- 原声要保留，但音量要拉到平台标准（-16 LUFS）
- 角标必须全片常驻；片头剧名卡、结尾追更卡已移除，成片只保留剧情本身 + 引导尾帧
"""
from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpegx, pack
from . import project as proj_mod

ROOT = Path(__file__).resolve().parent.parent
FPS = 30
# 片段逐段编码是渲染最重的一段；限 2 路并发，别让同时启动的 ffmpeg 把内存顶高
RENDER_CUT_WORKERS = 2


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def pick_size(proj: dict, spec: str = "match", fit: str = "crop") -> tuple[int, int]:
    if spec and spec != "match" and "x" in spec:
        w, h = spec.lower().split("x")
        return _even(float(w)), _even(float(h))
    e = proj["episodes"][0]
    return _even(e["width"]), _even(e["height"])


def _vfilter(w: int, h: int, fit: str) -> str:
    if fit == "pad":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS},format=yuv420p")
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={FPS},format=yuv420p")


def _cut(seg: dict, proj: dict, work: Path, i: int, w: int, h: int, fit: str,
         crf: int, preset: str) -> Path:
    ep = proj_mod.ep_by_idx(proj, seg["ep"])
    out = work / f"s{i:03d}.mp4"
    dur = max(0.25, seg["dur"])
    vf = _vfilter(w, h, fit)
    args = ["-y", "-ss", f"{seg['start']:.3f}", "-t", f"{dur:.3f}", "-i", ep["path"]]
    if ep.get("has_audio"):
        args += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                 "-map", "0:v:0", "-map", "1:a:0"]
    args += [
        "-vf", vf,
        "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-g", str(FPS * 2),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    ffmpegx.run(args, timeout=1800)
    return out


def _card_video(png: str, out: Path, dur: float, w: int, h: int, crf: int, preset: str) -> Path:
    args = ["-y", "-loop", "1", "-t", f"{dur:.2f}", "-i", png,
            "-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", f"scale={w}:{h},setsar=1,fps={FPS},format=yuv420p",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-g", str(FPS * 2),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-shortest", str(out)]
    ffmpegx.run(args, timeout=600)
    return out


VID_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".wmv"}


def _end_frame_piece(src: str, out: Path, ef_dur: float, w: int, h: int, fit: str,
                     crf: int, preset: str) -> tuple[Path, float]:
    """引导尾帧：图片 → 静帧按设定秒数；mp4 等视频 → 按原时长原样播放（含自带声音）。"""
    if Path(src).suffix.lower() not in VID_EXTS:
        png3 = out.with_suffix(".png")
        pack.prepare_end_frame(src, png3, w, h)
        dur = max(0.5, ef_dur)
        return _card_video(str(png3), out, dur, w, h, crf, preset), dur
    try:
        info = ffmpegx.probe(src)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"引导尾帧文件无法识别（不是有效的图片/视频）：{e}") from None
    dur = max(0.5, info["duration"])
    args = ["-y", "-i", src]
    if info["has_audio"]:
        args += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                 "-map", "0:v:0", "-map", "1:a:0"]
    args += [
        "-vf", _vfilter(w, h, fit),
        "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-g", str(FPS * 2),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    ffmpegx.run(args, timeout=1200)
    return out, dur


def _cleanup_work(work: Path) -> None:
    """清掉中间片段。渲染异常时也必须执行，否则 projects/_r* 会一直堆在磁盘上。"""
    try:
        for f in work.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        work.rmdir()
    except BaseException:  # noqa: BLE001  删不掉就留给下次启动的 _clean_stale_work
        pass


def render(proj: dict, plan: dict, out_path: str, opts: dict | None = None,
           emit=None) -> dict:
    work = Path(proj_mod.PROJ) / ("_r" + str(os.getpid()))
    _cleanup_work(work)
    work.mkdir(parents=True, exist_ok=True)
    try:
        return _render_impl(proj, plan, out_path, opts, emit, work)
    finally:
        _cleanup_work(work)


def _render_impl(proj: dict, plan: dict, out_path: str, opts, emit, work: Path) -> dict:
    opts = opts or {}

    w, h = pick_size(proj, opts.get("size", "match"), opts.get("fit", "crop"))
    crf = int(opts.get("crf", 20))
    preset = opts.get("preset", "veryfast")
    logo_path = opts.get("logo") or ""
    overlay_path = opts.get("overlay") or ""
    ov_opacity = float(opts.get("overlay_opacity", 1.0))
    end_frame_path = opts.get("end_frame") or ""
    ef_dur = float(opts.get("end_frame_dur", 2.5))
    has_end_frame = bool(end_frame_path and Path(end_frame_path).exists())
    has_overlay = bool(overlay_path and Path(overlay_path).exists())
    ef_real = max(0.5, ef_dur)  # 尾帧实际时长：视频尾帧按原时长，静帧按设定

    def say(p: float, msg: str):
        if emit:
            emit(p, msg)

    segs = [s for s in plan["segments"] if s.get("keep", True)]
    if not segs:
        raise RuntimeError("没有选中任何片段，无法渲染")

    # 片序：开头钩子 + 连续正片 → 引导尾帧（不再生成片头剧名卡 / 结尾追更卡）
    pieces: list[Path] = []
    total = len(segs) + (1 if has_end_frame else 0)
    done = 0

    fit = opts.get("fit", "crop")

    def cut_one(item):
        k, s = item
        return k, _cut(s, proj, work, k, w, h, fit, crf, preset)

    cut_results: dict[int, Path] = {}
    with ThreadPoolExecutor(max_workers=RENDER_CUT_WORKERS) as pool:
        for k, path in pool.map(cut_one, enumerate(segs)):
            cut_results[k] = path
            done += 1
            pct = 4 + 60 * done / max(1, total)
            say(min(64, pct), f"剪辑片段 {k + 1}/{len(segs)} · 第 {segs[k]['ep']} 集")
    # pool.map 按输入顺序产出，这里再按编号显式取一次，保证拼接顺序稳定
    pieces.extend(cut_results[k] for k in range(len(segs)))

    if has_end_frame:
        ef_piece, ef_real = _end_frame_piece(end_frame_path, work / "c998.mp4", ef_dur,
                                             w, h, opts.get("fit", "crop"), crf, preset)
        pieces.append(ef_piece)
        say(68, "引导尾帧")

    # ── 拼接 ──
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    joined = work / "joined.mp4"
    ffmpegx.run(["-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)],
                timeout=1800)
    say(72, "片段拼接完成")

    # ── 包装模板 + 角标 + 原声归一 ──
    out_path = str(Path(out_path))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    args = ["-y", "-i", str(joined)]
    has_logo = bool(logo_path and Path(logo_path).exists())
    layers = 0
    fc = ""
    cur = "0:v"
    if has_overlay:
        ov = work / "overlay.png"
        pack.prepare_overlay(overlay_path, ov, w, h, ov_opacity)
        args += ["-i", str(ov)]
        # 尾帧是整屏设计稿，默认不盖模板（可用 template_on_endframe 打开）
        en = ""
        if has_end_frame and not bool(opts.get("template_on_endframe", False)):
            body_dur = max(0.0, ffmpegx.probe(str(joined))["duration"] - ef_real)
            en = f":enable='lt(t,{body_dur:.3f})'"
        fc += f"[{layers + 1}:v]format=rgba[il{layers}];"
        if not has_logo:
            fc += f"[{cur}][il{layers}]overlay=0:0:format=auto{en},format=yuv420p[v]"
        else:
            fc += f"[{cur}][il{layers}]overlay=0:0:format=auto{en}[tm{layers}];"
            cur = f"tm{layers}"
        layers += 1
    if has_logo:
        lg = work / "logo.png"
        pack.prepare_logo(logo_path, lg, w, float(opts.get("logo_ratio", 0.14)))
        margin = max(12, int(h * 0.022))
        xy = {
            "tr": f"W-w-{margin}:{margin}",
            "tl": f"{margin}:{margin}",
            "br": f"W-w-{margin}:H-h-{margin * 2}",
            "bl": f"{margin}:H-h-{margin * 2}",
        }.get(opts.get("logo_pos", "tr"), f"W-w-{margin}:{margin}")
        args += ["-i", str(lg)]
        fc += f"[{layers + 1}:v]format=rgba[il{layers}];"
        fc += f"[{cur}][il{layers}]overlay={xy}:format=auto,format=yuv420p[v]"
        layers += 1
    if layers:
        args += ["-filter_complex", fc, "-map", "[v]", "-map", "0:a:0",
                 "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                 "-g", str(FPS * 2), "-pix_fmt", "yuv420p",
                 "-profile:v", "high", "-level", "4.1"]
    else:
        # 没有角标 / 模板时画面不需要任何改动：直接拷贝已编码片段，只重编码音频
        # 做 loudnorm。省掉一次全片二次转码，时长/分辨率/帧率不变，还少一次画质损失。
        args += ["-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy"]
    args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
             "-movflags", "+faststart", out_path]
    say(78, "叠加模板角标与音量归一")
    ffmpegx.run(args, timeout=2400)

    try:
        info = ffmpegx.probe(out_path)
    except Exception:  # noqa: BLE001  成片已生成，探测失败只影响返回的时长信息
        info = {"duration": 0.0, "width": 0, "height": 0}
    say(100, "成片完成")
    return {
        "video": out_path,
        "duration": round(info["duration"], 2),
        "width": info["width"], "height": info["height"],
        "size_mb": round(Path(out_path).stat().st_size / 1048576, 1),
        "segments": len(segs),
        "logo": bool(logo_path and Path(logo_path).exists()),
        "overlay": has_overlay, "end_frame": has_end_frame,
    }

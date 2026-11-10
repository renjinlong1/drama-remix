"""字幕解析：srt / ass / vtt，多编码自适应，以及分集与字幕的配对。

台词时间轴是这个工具的地基——顺剪能不能成立，全看字幕读得准不准。
"""
from __future__ import annotations

import re
from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16")
SUB_EXT = (".srt", ".ass", ".ssa", ".vtt")
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".ts", ".m4v", ".webm", ".rmvb")


def read_text(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for enc in ENCODINGS:
        try:
            t = raw.decode(enc)
            if t.count("\ufffd") < 3:
                return t
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


_TAG = re.compile(r"<[^>]+>")
_ASS_TAG = re.compile(r"\{[^}]*\}")


def clean(text: str) -> str:
    t = _ASS_TAG.sub("", text)
    t = _TAG.sub("", t)
    t = t.replace("\\N", " ").replace("\\n", " ").replace("\u3000", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _t2s(t: str) -> float:
    t = t.strip().replace(",", ".")
    parts = t.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return -1.0


_SRT_TIME = re.compile(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})")
_ASS_LINE = re.compile(r"^Dialogue:\s*[^,]*,([^,]*),([^,]*),")


def parse_srt(text: str) -> list[dict]:
    cues: list[dict] = []
    blocks = re.split(r"\r?\n\r?\n", text.replace("\r\n", "\n").strip())
    for b in blocks:
        lines = [ln for ln in b.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = 0
        ti = None
        for i, ln in enumerate(lines[:3]):
            if _SRT_TIME.search(ln):
                ti = i
                break
        if ti is None:
            continue
        m = _SRT_TIME.search(lines[ti])
        st, en = _t2s(m.group(1)), _t2s(m.group(2))
        body = " ".join(lines[ti + 1:])
        body = clean(body)
        if st < 0 or en <= st or not body:
            continue
        cues.append({"i": idx, "start": st, "end": en, "text": body})
    return _normalize(cues)


def parse_ass(text: str) -> list[dict]:
    cues: list[dict] = []
    in_events = False
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_events = s.lower().startswith("[events")
            continue
        if not in_events or not s.lower().startswith("dialogue:"):
            continue
        body = s[len("Dialogue:"):]
        parts = body.split(",", 9)
        if len(parts) < 10:
            continue
        st, en = _t2s(parts[1]), _t2s(parts[2])
        txt = clean(parts[9])
        if st < 0 or en <= st or not txt:
            continue
        cues.append({"i": len(cues), "start": st, "end": en, "text": txt})
    return _normalize(cues)


def parse_vtt(text: str) -> list[dict]:
    cues: list[dict] = []
    for m in re.finditer(r"(\d{1,2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}\.\d{3})[^\n]*\n((?:(?!\n\n).)*)",
                         text.replace("\r\n", "\n"), re.S):
        st, en = _t2s(m.group(1)), _t2s(m.group(2))
        txt = clean(m.group(3))
        if st < 0 or en <= st or not txt:
            continue
        cues.append({"i": len(cues), "start": st, "end": en, "text": txt})
    return _normalize(cues)


def _normalize(cues: list[dict]) -> list[dict]:
    """按开始时间排序，去掉完全重复的时间轴。"""
    cues.sort(key=lambda c: (c["start"], c["end"]))
    out: list[dict] = []
    for c in cues:
        if out and abs(out[-1]["start"] - c["start"]) < 0.02 and out[-1]["text"] == c["text"]:
            continue
        c["i"] = len(out)
        out.append(c)
    return out


def load_subtitle(path: str | Path) -> list[dict]:
    text = read_text(path)
    ext = Path(path).suffix.lower()
    if ext in (".ass", ".ssa"):
        cues = parse_ass(text)
        if not cues:
            cues = parse_srt(text)
    elif ext == ".vtt":
        cues = parse_vtt(text)
    else:
        cues = parse_srt(text)
    return cues


def natural_key(s: str):
    """自然排序：第2集 < 第10集。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_videos(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    return sorted(files, key=lambda p: natural_key(p.name))


def list_subs(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    return sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUB_EXT],
                  key=lambda p: natural_key(p.name))


def pair_subtitles(videos: list[Path], subs: list[Path]) -> dict[str, Path | None]:
    """给每个视频找字幕：先同名匹配，再按顺序对齐，最后退回目录内唯一字幕。"""
    result: dict[str, Path | None] = {str(v): None for v in videos}
    if not subs:
        return result
    stems = {str(v): v.stem.lower() for v in videos}
    used: set[str] = set()
    # 1) 同名（允许 01.srt / 01.zh.srt / 01.chs.srt）
    for v in videos:
        stem = stems[str(v)]
        best = None
        for s in subs:
            sl = s.stem.lower()
            if sl == stem or sl.startswith(stem + ".") or sl.startswith(stem + "_"):
                best = s
                break
        if best:
            result[str(v)] = best
            used.add(str(best))
    # 2) 数量对齐，按序号一一对应
    rest_v = [v for v in videos if result[str(v)] is None]
    rest_s = [s for s in subs if str(s) not in used]
    if rest_v and len(rest_v) == len(rest_s):
        for v, s in zip(rest_v, rest_s):
            result[str(v)] = s
    # 3) 目录只有一个字幕 → 全部共用（整部剧一个字幕文件）
    elif len(subs) == 1:
        for v in videos:
            if result[str(v)] is None:
                result[str(v)] = subs[0]
    return result

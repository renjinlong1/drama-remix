"""项目模型：导入分集、配对字幕、建立整剧时间轴。

整剧时间轴 = 各集按顺序首尾相接，每集有一个 offset。
分析、打分、规划都在全局时间轴上做，渲染时再映射回（集, 集内时间）。
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpegx, ocrsubs, subs

ROOT = Path(__file__).resolve().parent.parent
PROJ = ROOT / "projects"
OCR_DIR = ROOT / "data" / "ocr"


def _safe_name(name: str) -> str:
    return "".join(ch for ch in (name or "") if ch not in '\\/:*?"<>|').strip() or "project"


def _cached_ocr_srt(name: str, idx: int) -> Path | None:
    """以前 OCR 识别过的字幕（data/ocr/<剧名>/epNN.srt）。"""
    for cand in {_safe_name(name), _safe_name(Path(name).name)}:
        p = OCR_DIR / cand / f"ep{idx:02d}.srt"
        if p.exists() and p.stat().st_size > 40:
            return p
    return None


def scan(folder: str | Path, name: str = "") -> dict:
    folder = Path(folder)
    if not folder.exists():
        raise RuntimeError("文件夹不存在")
    videos = subs.list_videos(folder)
    if not videos:
        raise RuntimeError("这个文件夹里没有找到视频文件（支持 mp4/mkv/mov/avi 等）")
    sub_files = subs.list_subs(folder)
    pairs = subs.pair_subtitles(videos, sub_files)
    pname = name or folder.name

    # 探测并发做：每集一次 ffmpeg -i 约 60~120ms，44 集串行要 4 秒；
    # 8 路并发后 ≈0.6 秒，且 probe 有按「路径+大小+mtime」的缓存。
    def probe_one(v: Path):
        try:
            return v, ffmpegx.probe(v), ""
        except Exception as e:      # noqa: BLE001 坏文件跳过，别让整次导入失败
            return v, None, str(e)

    workers = max(1, min(8, os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        probed = list(pool.map(probe_one, videos))
    skipped = [{"file": v.name, "error": err[:200]} for v, info, err in probed if info is None]
    playable = [(v, info) for v, info, _err in probed if info is not None]
    if not playable:
        raise RuntimeError("这些视频都读不出来：" +
                           " / ".join(x["file"] for x in skipped[:3]))
    if skipped:
        print(f"[scan] 跳过 {len(skipped)} 个无法读取的文件："
              + " / ".join(x["file"] for x in skipped[:3]), flush=True)

    episodes: list[dict] = []
    offset = 0.0
    for i, (v, info) in enumerate(playable):
        sp = pairs.get(str(v))
        cues = subs.load_subtitle(sp) if sp else []
        src = "file" if sp else ""
        # 没有字幕文件时，接回之前从画面识别出来的字幕，避免重新导入把 OCR 结果丢掉
        # 先按文件夹名找（真正的剧身份），再退回剧名，防止改了剧名就串到别的剧
        if not cues:
            cached = _cached_ocr_srt(folder.name, i + 1) or _cached_ocr_srt(pname, i + 1)
            if cached:
                # 旧 OCR 缓存坏了（半截文件 / 换系统时编码变了）不能让整次导入失败：
                # 当作没有字幕继续走，需要时用户还能手动重跑画面识别。
                try:
                    cues = subs.load_subtitle(cached)
                except Exception:  # noqa: BLE001
                    cues = []
                if cues:
                    src = "ocr"
                    sp = cached
        ep = {
            "idx": i + 1,
            "file": v.name,
            "path": str(v),
            "sub": sp.name if sp else "",
            "sub_path": str(sp) if sp else "",
            "sub_source": src,
            "ocr": None,
            "dur": round(info["duration"], 3),
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"] or 30.0,
            "has_audio": info["has_audio"],
            "offset": round(offset, 3),
            "cues": cues,
        }
        offset += info["duration"]
        episodes.append(ep)

    with_cues = sum(1 for e in episodes if e["cues"])
    proj = {
        "name": pname,
        "folder": str(folder),
        "created_at": time.strftime("%Y-%m-%d %H:%M"),
        "episodes": episodes,
        "total": round(offset, 3),
        "sub_episodes": with_cues,
        "sub_total": len(sub_files),
        "ocr_eps": sum(1 for e in episodes if e["sub_source"] == "ocr"),
        "skipped": skipped,
    }
    return proj


def apply_ocr(proj: dict, progress=None, force: bool = False,
              fps: float = ocrsubs.DEFAULT_FPS) -> dict:
    """对没有字幕的集做画面硬字幕识别（OCR），把结果写进 srt 缓存并挂到项目上。

    只补「没有台词」的集；已经配了字幕文件的集不动。force=True 时连之前 OCR 出来的重做。
    """
    ok, msg = ocrsubs.available()
    if not ok:
        raise RuntimeError(msg)
    # 缓存目录用文件夹名（剧的真实身份）：剧名是用户随手起的，改了就会串到别的剧
    _fname = Path(proj.get("folder") or "").name
    out_dir = OCR_DIR / _safe_name(_fname or proj["name"])
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = []
    for e in proj["episodes"]:
        if e.get("cues"):
            if force and e.get("sub_source") == "ocr":
                targets.append(e)
        else:
            targets.append(e)
    if not targets:
        return {"eps": 0, "cues": 0, "secs": 0.0, "empty": 0}

    total_cues = 0
    empty = 0
    secs = 0.0
    n = len(targets)
    for i, ep in enumerate(targets):
        label = f"第 {ep['idx']} 集"

        def sub_progress(p: float, m: str, _i=i, _lab=label):
            if progress:
                progress((_i + min(0.98, p)) / n * 100, f"{_lab} · {m}")

        r = ocrsubs.ocr_video(ep["path"], fps=fps, progress=sub_progress, label="")
        srt = out_dir / f"ep{ep['idx']:02d}.srt"
        ocrsubs.write_srt(r["cues"], srt)
        ep["cues"] = r["cues"]
        ep["sub"] = srt.name
        ep["sub_path"] = str(srt)
        ep["sub_source"] = "ocr"
        ep["ocr"] = {"frames": r["frames"], "hit_frames": r["hit_frames"],
                     "ocr_calls": r["ocr_calls"], "secs": r["secs"]}
        total_cues += len(r["cues"])
        secs += r["secs"]
        if not r["cues"]:
            empty += 1
        if progress:
            progress((i + 1) / n * 100,
                     f"{label} · 识别出 {len(r['cues'])} 句台词（{i + 1}/{n} 集）")

    proj["sub_episodes"] = sum(1 for e in proj["episodes"] if e["cues"])
    proj["ocr_eps"] = sum(1 for e in proj["episodes"] if e.get("sub_source") == "ocr")
    return {"eps": n, "cues": total_cues, "secs": round(secs, 1), "empty": empty}


def all_cues(proj: dict) -> list[dict]:
    """展平成全局时间轴的台词列表。"""
    out = []
    for ep in proj["episodes"]:
        for c in ep["cues"]:
            out.append({
                "ep": ep["idx"], "i": c["i"],
                "start": c["start"], "end": c["end"],
                "g0": round(ep["offset"] + c["start"], 3),
                "g1": round(ep["offset"] + c["end"], 3),
                "text": c["text"],
            })
    out.sort(key=lambda c: c["g0"])
    return out


def ep_by_idx(proj: dict, idx: int) -> dict:
    for e in proj["episodes"]:
        if e["idx"] == idx:
            return e
    raise KeyError(idx)


def loc(proj: dict, gtime: float) -> tuple[int, float]:
    """全局时间 → (集号, 集内时间)。"""
    for e in proj["episodes"]:
        if e["offset"] <= gtime < e["offset"] + e["dur"]:
            return e["idx"], max(0.0, gtime - e["offset"])
    last = proj["episodes"][-1]
    return last["idx"], max(0.0, min(last["dur"], gtime - last["offset"]))


def save(proj: dict) -> str:
    PROJ.mkdir(exist_ok=True)
    path = PROJ / f"{_safe_name(proj['name'])}.json"
    # 同名项目换了另一部剧：先把旧存档另存一份 .bak，万一判断错了还能捞回来
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if str(prev.get("folder") or "") != str(proj.get("folder") or ""):
                (PROJ / (path.stem + ".json.bak")).write_bytes(path.read_bytes())
        except Exception:  # noqa: BLE001
            pass
    slim = {**proj}
    path.write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    return str(path)


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def folder_ok(proj: dict) -> bool:
    """这部剧的源视频是不是还在这台电脑上。

    项目文件里存的是绝对路径。换台电脑（或把移动硬盘拔了）以后，
    文件还在、视频没了——这时候打开只会看到一堆黑缩略图，
    必须提前判断出来，别让人以为"识别错了"。
    """
    folder = str(proj.get("folder") or "").strip()
    if folder:
        try:
            return Path(folder).is_dir()
        except OSError:
            return False
    eps = proj.get("episodes") or []
    if not eps:
        return False
    try:
        return Path(str(eps[0].get("path") or "")).exists()
    except OSError:
        return False


_RECENT_CACHE: dict = {"key": None, "items": []}


def recent(limit: int = 12) -> list[dict]:
    """最近项目列表。

    以前每次调用都把 12 个项目 JSON 全量读出来解析（单个 150KB 上下），
    而 /api/status 每次刷新页面都会问一次。这里按「文件名 + mtime + 大小」
    缓存元数据，只有项目真有变化才重读；missing 仍然实时判断（拔盘要能立刻反映）。
    """
    PROJ.mkdir(exist_ok=True)
    stamped = []
    for p in PROJ.glob("*.json"):
        try:
            st = p.stat()
        except OSError:
            continue
        stamped.append((st.st_mtime, st.st_size, p))
    stamped.sort(reverse=True)
    files = stamped[:limit]
    key = tuple((str(p), m, sz) for m, sz, p in files)
    if _RECENT_CACHE["key"] == key:
        base = _RECENT_CACHE["items"]
    else:
        base = []
        for _m, _sz, p in files:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                base.append({"name": d.get("name", p.stem), "total": d.get("total", 0),
                             "eps": len(d.get("episodes", [])), "path": str(p),
                             "folder": d.get("folder", ""),
                             "created_at": d.get("created_at", "")})
            except Exception:
                continue
        _RECENT_CACHE.update(key=key, items=base)
    return [{**x, "missing": not folder_ok(x)} for x in base]


def forget(name: str) -> list[str]:
    """彻底清掉一部剧在本机留下的**项目记录**。

    删「最近项目」时只 unlink 掉 projects/*.json 是不够的：同名 .json.bak
    会留着旧时间轴，data/act 的动作点缓存会让重新导入挂上旧片的事件，
    recent() 的元数据缓存还可能把已删的条目再吐回列表。这三处一起清，
    重新导入才会真正按新项目走一遍扫描分析。

    data/ocr 的字幕缓存按界面承诺保留（按文件夹名索引、带自校验），
    原片与成片也一律不动。
    """
    safe = _safe_name(name)
    removed: list[str] = []
    for p in (PROJ / f"{safe}.json", PROJ / f"{safe}.json.bak",
              ROOT / "data" / "act" / f"{safe}.json",
              ROOT / "data" / "multimodal" / f"{safe}.json"):
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except OSError:
            continue
    _RECENT_CACHE.update(key=None, items=[])   # 下一轮 recent() 必须重读目录
    return removed


def purge_missing() -> list[str]:
    """删掉源视频已经不在这台电脑上的项目（只动 projects/*.json）。

    data/ocr 里的字幕缓存保留：真有那么一天把原片找回来，重新导入还能直接命中。
    """
    PROJ.mkdir(exist_ok=True)
    gone: list[str] = []
    for p in PROJ.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if folder_ok(d):
            continue
        try:
            p.unlink()
            gone.append(str(d.get("name") or p.stem))
        except OSError:
            continue
    _RECENT_CACHE.update(key=None, items=[])
    return gone

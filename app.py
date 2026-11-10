"""枫的瞬剪 · 本地服务。

启动后浏览器打开 127.0.0.1:8780，三步产出素材：
导入剧集 → 选钩子 → 顺剪出片。
"""
from __future__ import annotations

import json
import os
import re
import random
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import analyze as analyzemod
from engine import ffmpegx, naming, ocrsubs, pack
from engine import plan as planmod
from engine import project as projmod
from engine import render as rendermod
from engine import subs, vision

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
THUMBS = DATA / "thumbs"
OUTPUT = ROOT / "output"
for d in (DATA, UPLOADS, THUMBS, OUTPUT, ROOT / "projects"):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="枫的瞬剪")
app.mount("/assets", StaticFiles(directory=str(WEB)), name="assets")

STATE: dict = {"project": None, "analysis": None, "plan": None, "job": None,
               "exports": set()}
LOCK = threading.Lock()

# 浏览器预览要直读的本地素材类型（/api/file）
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
}


def _clean_stale_work():
    """清掉上次异常退出留下的渲染中间目录（_r*），1 小时内的不动，可能在用。"""
    for d in (ROOT / "projects").glob("_r*"):
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        except BaseException:  # noqa: BLE001
            pass


# ── 任务 ────────────────────────────────────────────────────────────
def _job_start(kind: str):
    with LOCK:
        if STATE["job"] and STATE["job"]["state"] == "running":
            raise HTTPException(409, "有任务正在跑，等它完成")
        STATE["job"] = {"kind": kind, "state": "running", "progress": 0,
                        "msg": "准备中", "result": None, "t0": time.time()}


def _job_emit(pct: float, msg: str):
    if STATE["job"]:
        STATE["job"]["progress"] = round(max(0, min(100, pct)), 1)
        STATE["job"]["msg"] = msg


def _job_done(result=None, err: str | None = None):
    if not STATE["job"]:
        return
    if err:
        STATE["job"].update(state="error", msg=err, progress=100)
    else:
        STATE["job"].update(state="done", progress=100, msg="完成", result=result)


def _bg(fn, *a, **kw):
    def run():
        try:
            r = fn(*a, **kw)
            _job_done(result=r)
        except Exception as e:  # noqa: BLE001
            _job_done(err=str(e))
    threading.Thread(target=run, daemon=True).start()


# ── 基础 ────────────────────────────────────────────────────────────
def _asset_ver() -> str:
    """前端资源指纹：改过 app.js/style.css 就换版本号，避免浏览器拿旧 JS。"""
    v = 0
    for n in ("app.js", "style.css", "index.html"):
        try:
            v = max(v, int((WEB / n).stat().st_mtime))
        except OSError:
            pass
    return str(v)


@app.middleware("http")
async def _asset_no_cache(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/assets/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    v = _asset_ver()
    for n in ("app.js", "style.css"):
        html = html.replace(f'/assets/{n}"', f'/assets/{n}?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/status")
def status():
    av = _asset_ver()
    try:
        path = ffmpegx.ffmpeg()
        ver = ffmpegx.version()
    except Exception as e:  # noqa: BLE001
        return {"ffmpeg": False, "error": str(e), "asset_ver": av}
    ok_ocr, ocr_msg = ocrsubs.available()
    return {"ffmpeg": True, "ffmpeg_path": path, "ffmpeg_version": ver,
            "ocr": ok_ocr, "ocr_msg": ocr_msg,
            "projects": projmod.recent(8), "asset_ver": av}


def _check_scan_folder(folder: str) -> None:
    """导入前把路径问题说清楚，别让后台任务抛一句英文 OSError 就没了下文。"""
    if not folder:
        raise ValueError("请先填剧集文件夹路径，或点「选择…」挑一个文件夹")
    p = Path(folder)
    try:
        exists, is_dir = p.exists(), p.is_dir()
    except OSError as e:
        raise ValueError(f"路径无法访问：{folder}（{e}）。盘符是否插好、网络盘是否连上？") from None
    if not exists:
        raise ValueError(f"文件夹不存在：{folder}。移动硬盘 / 网络盘没连上，或路径已经改过？")
    if not is_dir:
        raise ValueError(f"这不是文件夹：{folder}，请选择装剧集的文件夹，不要直接选视频文件")
    try:
        next(p.iterdir(), None)
    except PermissionError as e:
        raise ValueError(f"没有权限读取这个文件夹：{folder}。换一个目录，或以管理员身份运行（{e}）") from None
    except OSError as e:
        raise ValueError(f"读取文件夹失败：{folder}（{e}）") from None


@app.post("/api/scan")
def scan(body: dict = Body(...)):
    # 从资源管理器粘过来的路径常带引号 / 全角空格，先统一规范化
    folder = (body.get("folder") or "").strip().strip('"').strip("'").replace("\u3000", " ").strip()
    name = (body.get("name") or "").strip()
    use_ocr = bool(body.get("ocr", True))
    name, notice = _resolve_proj_name(name, folder)
    _job_start("scan")

    def work():
        _check_scan_folder(folder)
        _job_emit(2, "扫描剧集与字幕")
        p = projmod.scan(folder, name)
        # 钩子池的随机种子：换台电脑 / 重新导入同一部剧，高光开头不会一模一样
        p["hook_seed"] = random.randint(1, 2 ** 31 - 1)
        # 没有字幕文件的集：从画面里的硬字幕识别台词
        if use_ocr and p["sub_episodes"] < len(p["episodes"]):
            def ocr_progress(pct, msg):
                _job_emit(5 + pct * 0.85, "OCR · " + msg)
            try:
                r = projmod.apply_ocr(p, progress=ocr_progress)
                if r["eps"]:
                    print(f"[ocr] {r['eps']} 集，{r['cues']} 句台词，用时 {r['secs']}s", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[ocr] 跳过：{e}", flush=True)
        _job_emit(86, "扫描画面冲突（打人 / 砸东西 / 情绪爆发）")
        try:
            v = vision.scan_project(
                p, ROOT,
                progress=lambda pct, msg: _job_emit(86 + pct * 0.08, msg))
            p["acts"] = v["events"]
            print(f"[acts] {len(v['events'])} 个动作点 / {v['eps']} 集", flush=True)
        except Exception as e:  # noqa: BLE001
            p["acts"] = []
            print(f"[acts] 跳过：{e}", flush=True)
        projmod.save(p)
        _job_emit(95, "分析剧情")
        a = analyzemod.analyze(p)
        with LOCK:
            STATE["project"] = p
            STATE["analysis"] = a
            STATE["plan"] = None
        out = _proj_payload(p, a)
        if notice:
            out["notice"] = notice
        return out
    _bg(work)
    return {"ok": True, "name": name, "notice": notice}


def _resolve_proj_name(name: str, folder: str) -> tuple[str, str]:
    """同名但指向另一个文件夹时改用别的名字。

    导入面板的「剧名」默认沿用上一部剧的名字，选了新文件夹却忘了改，
    就会把原来那部剧的项目文件覆盖掉（整剧时间轴、动作点全丢）。
    """
    if not name or not folder:
        return name, ""
    path = projmod.PROJ / (projmod._safe_name(name) + ".json")
    if not path.exists():
        return name, ""
    try:
        old = projmod.load(path)
        old_folder = str(old.get("folder") or "")
    except Exception:  # noqa: BLE001
        return name, ""

    def norm(p: str) -> str:
        try:
            return os.path.normcase(os.path.abspath(p))
        except Exception:  # noqa: BLE001
            return (p or "").lower()

    if old_folder and norm(old_folder) == norm(folder):
        return name, ""
    # 同名项目的源素材已经不在这台电脑上（换机后带过来的旧存档）：
    # 那个项目本来就用不了，这次直接沿用这个名字；旧文件另存一份 .bak 兜底
    if old_folder and not Path(old_folder).exists():
        return name, f"「{name}」的旧存档源视频不在这台电脑上，本次导入会替换它（旧文件已备份为 {name}.json.bak）"

    alt = _free_proj_name(Path(folder).name.strip() or name)
    return alt, f"「{name}」已经是另一部剧的项目，这次改存为「{alt}」，原项目不会被覆盖"


def _free_proj_name(base: str) -> str:
    """找一个还没被占用的项目名：X → X (2) → X (3)…"""
    cand = base
    i = 2
    while (projmod.PROJ / (projmod._safe_name(cand) + ".json")).exists():
        cand = f"{base} ({i})"
        i += 1
    return cand


@app.post("/api/ocr")
def run_ocr(body: dict = Body(default={})):
    """给当前项目里没台词的集补做画面字幕识别。"""
    with LOCK:
        p = STATE["project"]
    if not p:
        raise HTTPException(400, "还没有导入剧集")
    force = bool(body.get("force", False))
    _job_start("ocr")

    def work():
        r = projmod.apply_ocr(p, progress=lambda pct, msg: _job_emit(pct, "OCR · " + msg),
                              force=force)
        projmod.save(p)
        _job_emit(95, "重新分析剧情")
        a = analyzemod.analyze(p)
        with LOCK:
            STATE["analysis"] = a
            STATE["plan"] = None
        out = _proj_payload(p, a)
        out["ocr"] = r
        return out
    _bg(work)
    return {"ok": True}


@app.post("/api/acts")
def scan_acts(body: dict = Body(default={})):
    """重扫画面冲突点（扇巴掌 / 砸东西 / 情绪爆发）。

    台词分只能看见"说了什么"，动手的镜头往往一句台词都没有，靠这一步补上。
    """
    with LOCK:
        p = STATE["project"]
    if not p:
        raise HTTPException(400, "还没有导入剧集")
    force = bool(body.get("force", True))
    _job_start("acts")

    def work():
        v = vision.scan_project(
            p, ROOT, force=force,
            progress=lambda pct, msg: _job_emit(pct * 0.9, msg))
        p["acts"] = v["events"]
        projmod.save(p)
        _job_emit(94, "重新分析剧情")
        a = analyzemod.analyze(p)
        with LOCK:
            STATE["analysis"] = a
            STATE["plan"] = None
        out = _proj_payload(p, a)
        out["acts_info"] = {"events": len(v["events"]), "eps": v["eps"],
                            "scanned": v["scanned"]}
        return out
    _bg(work)
    return {"ok": True}


@app.post("/api/project/delete")
def delete_project(body: dict = Body(...)):
    """删掉一个项目存档（只删 projects/*.json）。

    OCR 字幕缓存留在 data/ocr 里不动——同一部剧以后再导一遍还是秒级，
    所以误删了也不至于要重跑几十分钟识别。
    """
    raw = (body.get("path") or "").strip()
    if not raw:
        raise HTTPException(400, "缺少项目路径")
    root = Path(projmod.PROJ).resolve()
    try:
        target = Path(raw).resolve()
        target.relative_to(root)          # 只允许删 projects 目录下的文件
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "只能删除项目存档（projects 目录内）")
    if target.suffix.lower() != ".json" or not target.exists():
        raise HTTPException(404, "项目文件不存在")

    name = target.stem
    with LOCK:
        cur = STATE["project"]
        is_cur = bool(cur) and projmod._safe_name(cur.get("name") or "") == name
        if is_cur:
            STATE["project"] = None
            STATE["analysis"] = None
            STATE["plan"] = None
    try:
        target.unlink()
    except OSError as e:
        raise HTTPException(500, f"删除失败：{e}")
    return {"ok": True, "name": name, "current": is_cur}


@app.post("/api/projects/purge_missing")
def purge_missing():
    """一键清掉源视频不在这台电脑上的项目（换电脑后收拾旧存档用）。"""
    gone = projmod.purge_missing()
    drop = {projmod._safe_name(x) for x in gone}
    with LOCK:
        cur = STATE["project"]
        if cur and projmod._safe_name(cur.get("name") or "") in drop:
            STATE["project"] = None
            STATE["analysis"] = None
            STATE["plan"] = None
    return {"ok": True, "removed": gone}


@app.post("/api/open")
def open_project(body: dict = Body(...)):
    path = body.get("path") or ""
    if not Path(path).exists():
        raise HTTPException(404, "项目文件不存在")
    p = projmod.load(path)
    if "acts" not in p:      # 老项目没扫过动作点；空列表也算扫过，避免每次打开都重跑
        try:
            v = vision.scan_project(p, ROOT)     # 命中缓存时几乎不耗时
            if v["events"]:
                p["acts"] = v["events"]
                projmod.save(p)
        except Exception:  # noqa: BLE001
            pass
    a = analyzemod.analyze(p)
    with LOCK:
        STATE["project"] = p
        STATE["analysis"] = a
        STATE["plan"] = None
    return _proj_payload(p, a)


def _proj_payload(p: dict, a: dict) -> dict:
    eps = [{"idx": e["idx"], "file": e["file"], "dur": round(e["dur"], 2),
            "w": e["width"], "h": e["height"], "sub": e["sub"],
            "cues": len(e["cues"]), "offset": e["offset"],
            "sub_source": e.get("sub_source", "")} for e in p["episodes"]]
    return {
        "name": p["name"], "folder": p.get("folder", ""), "total": p["total"], "eps": eps,
        "folder_ok": projmod.folder_ok(p),
        "sub_episodes": p["sub_episodes"],
        "ocr_eps": p.get("ocr_eps", 0),
        "stats": a["stats"],
        "beats": a["beats"],
        "highlights": a["highlights"],
        "hook_meta": a.get("hook_meta") or {},
        "acts": a.get("acts", 0),
        "endings": a.get("endings", []),
    }


@app.get("/api/state")
def state():
    _restore_last_project()
    with LOCK:
        p, a, pl = STATE["project"], STATE["analysis"], STATE["plan"]
    if not p:
        return {"ready": False}
    out = _proj_payload(p, a)
    out["ready"] = True
    out["plan"] = pl
    return out


@app.post("/api/hooks/reseed")
def hooks_reseed():
    """换一批高光开头候选。

    同一部剧、同一台电脑点多少次都会换出不同的候选池（评分只抖动 ±6%，
    不会把明显更差的段落顶上来）；种子写进项目文件，下次打开还是这一批。
    """
    with LOCK:
        p = STATE["project"]
    if not p:
        raise HTTPException(400, "还没导入剧集")
    p["hook_seed"] = random.randint(1, 2 ** 31 - 1)
    projmod.save(p)
    a = analyzemod.analyze(p)
    with LOCK:
        STATE["analysis"] = a
        STATE["plan"] = None        # 钩子换了，旧方案的开头已经不是这一批
    return {"seed": p["hook_seed"], "highlights": a["highlights"]}


def _restore_last_project():
    """服务重启后自动接回上次用的项目，省得每次都要在「最近项目」里点一下。

    只接回**源视频还在这台电脑上**的项目。便携包如果是带着旧存档过来的，
    那些记着别人电脑路径（F:\\…）的项目就跳过——否则一打开就是一屏黑缩略图，
    还会让人误以为"导入新剧却识别成了旧剧"。
    """
    with LOCK:
        if STATE["project"]:
            return
    try:
        items = projmod.recent(12)
    except Exception:  # noqa: BLE001
        return
    for it in items:
        if it.get("missing"):
            continue
        path = Path(it.get("path") or (Path(projmod.PROJ) / (projmod._safe_name(it["name"]) + ".json")))
        if not path.exists():
            continue
        try:
            p = projmod.load(path)
            a = analyzemod.analyze(p)
        except Exception:  # noqa: BLE001
            continue
        with LOCK:
            if not STATE["project"]:
                STATE["project"], STATE["analysis"], STATE["plan"] = p, a, None
        return


@app.post("/api/plan")
def make_plan(body: dict = Body(...)):
    with LOCK:
        p, a = STATE["project"], STATE["analysis"]
    if not p:
        raise HTTPException(400, "还没有导入剧集")
    if not (a and a.get("beats")):
        raise HTTPException(400, NO_SUB_MSG)
    target = float(body.get("target", 360))
    if target < 60 or target > 900:
        raise HTTPException(400, "目标时长请填 1-15 分钟")
    hook = body.get("hook")
    end_hook = body.get("end_hook")
    tm, tf = _to_tail(body)
    try:
        pl = planmod.build(p, a["beats"], target=target, hook=hook,
                           end_hook=end_hook,
                           end_mode=str(body.get("end_mode") or "auto"),
                           tightness=float(body.get("tightness", 0.0)),
                           end_ep=_to_int(body.get("end_ep")),
                           lead=_to_lead(body.get("hook_lead")),
                           tail_mode=tm, tail_fixed=tf)
    except RuntimeError as e:
        # 开头太靠后、没有可用剧情这类问题，是用户能改的，给 400 而不是 500
        raise HTTPException(400, str(e)) from None
    planmod.restat(p, pl)
    with LOCK:
        STATE["plan"] = pl
    return pl


@app.post("/api/plan/ends")
def plan_ends(body: dict = Body(...)):
    """某个开头在当前目标时长下能用的结尾候选（给批量结尾指定面板用）。

    结尾候选是跟着开头走的：换个开头，可行的结束点就不一样，
    所以面板要对每个批量开头单独算一遍（纯计算，毫秒级）。
    """
    with LOCK:
        p, a = STATE["project"], STATE["analysis"]
    if not p:
        raise HTTPException(400, "还没有导入剧集")
    if not (a and a.get("beats")):
        raise HTTPException(400, NO_SUB_MSG)
    target = float(body.get("target", 360))
    if target < 60 or target > 900:
        raise HTTPException(400, "目标时长请填 1-15 分钟")
    try:
        tm, tf = _to_tail(body)
        pl = planmod.build(p, a["beats"], target=target, hook=body.get("hook"),
                           tightness=float(body.get("tightness", 0.0)),
                           lead=_to_lead(body.get("hook_lead")),
                           tail_mode=tm, tail_fixed=tf)
    except RuntimeError as e:
        return {"ends": [], "error": str(e)}
    return {"ends": pl.get("end_cands", [])}


def _to_int(v):
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _to_tail(body: dict) -> tuple[str, float]:
    """去尾帧：(模式, 固定秒数)。auto 自动识别 / fixed 固定秒数 / off 不去。"""
    mode = str(body.get("tail_mode") or "auto").lower()
    if mode not in ("auto", "fixed", "off"):
        mode = "auto"
    try:
        fixed = max(0.3, min(6.0, float(body.get("tail_fixed", 2.0))))
    except (TypeError, ValueError):
        fixed = 2.0
    return mode, fixed


def _to_lead(v) -> float:
    """开头入戏（秒）：钩子起点再往前切多少。0-5 秒，非法值当 0。"""
    try:
        return max(0.0, min(5.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


@app.post("/api/plan/beats")
def plan_beats(body: dict = Body(...)):
    """时间轴上微调节拍后重新拼段。"""
    with LOCK:
        p, a, pl = STATE["project"], STATE["analysis"], STATE["plan"]
    if not p or not a:
        raise HTTPException(400, "还没有导入剧集")
    if not a.get("beats"):
        raise HTTPException(400, NO_SUB_MSG)
    drop = body.get("beats_drop") or []
    hook = body.get("hook") or (pl or {}).get("hook")
    target = float(body.get("target") or (pl or {}).get("target") or 360)
    end_t = (pl or {}).get("to", {}).get("g1")
    if body.get("end_t"):
        end_t = float(body["end_t"])
    lead = _to_lead(body.get("hook_lead", (pl or {}).get("hook_lead", 0.0)))
    tm, tf = _to_tail({"tail_mode": body.get("tail_mode", (pl or {}).get("tail_mode")),
                       "tail_fixed": body.get("tail_fixed", 2.0)})
    try:
        newpl = planmod.from_keep(p, a["beats"], drop, hook, target, end_t=end_t,
                                  lead=lead, tail_mode=tm, tail_fixed=tf)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from None
    planmod.restat(p, newpl)
    with LOCK:
        STATE["plan"] = newpl
    return newpl


def _pack_opts(body: dict, title: str) -> dict:
    """渲染/批量共用的包装与输出参数。"""
    return {
        "size": body.get("size", "match"),
        "fit": body.get("fit", "crop"),
        "crf": int(body.get("crf", 20)),
        "title": title,
        "logo": body.get("logo", ""),
        "logo_pos": body.get("logo_pos", "tr"),
        "logo_ratio": float(body.get("logo_ratio", 0.14)),
        "overlay": body.get("overlay", ""),
        "overlay_opacity": float(body.get("overlay_opacity", 1.0)),
        "end_frame": body.get("end_frame", ""),
        "end_frame_dur": float(body.get("end_frame_dur", 2.5)),
        "template_on_endframe": bool(body.get("template_on_endframe", False)),
    }


def _safe_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", title)[:40] or "成片"


def _name_tpl(body: dict) -> str:
    """取导出命名模板；语法不对就地 400，别等渲染到一半才失败。"""
    tpl = str(body.get("name_tpl") or "").strip()
    if not tpl:
        return ""
    try:
        naming.parse(tpl)
    except naming.NameTemplateError as e:
        raise HTTPException(400, str(e)) from None
    return tpl


def _out_dir(body: dict, safe: str) -> Path:
    """成片落到哪儿：留空就放 output/；指定了本地文件夹就在里面建一个「剧名_时间」子文件夹。

    用户选的路径可能是 D 盘任意目录，先建好再把目录登记进 STATE["exports"]，
    好让 /api/video、/api/file 放行这些成片的预览。
    """
    raw = str(body.get("outdir") or "").strip().strip('"')
    if not raw:
        return OUTPUT
    base = Path(raw).expanduser()
    if not base.is_absolute():
        base = ROOT / base
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"保存目录建不了：{e}") from None
    if not base.is_dir():
        raise HTTPException(400, f"这不是文件夹：{base}")
    if body.get("outdir_sub", True):
        stem = f"{safe}_{time.strftime('%Y%m%d-%H%M%S')}"
        d = base / stem
        n = 1
        while d.exists():          # 同一秒连开两批，别互相覆盖
            n += 1
            d = base / f"{stem}_{n}"
        try:
            d.mkdir(parents=True)
        except OSError as e:
            raise HTTPException(400, f"保存目录建不了：{e}") from None
    else:
        d = base
    with LOCK:
        STATE["exports"].add(str(d.resolve()))
    return d


def _alloc_items(items: list, total: int | None) -> list[tuple[dict, int]]:
    """把「导出成片数量」分到各开头。

    total 为空/0 → 用每张卡片右下 ×N 自己设的条数（老逻辑）；
    total 有值     → 总数优先，在各开头之间轮流平均分，顺序靠前的多拿一条。
    """
    items = [it for it in items if it and it.get("hook")]
    if not items:
        return []
    if total and total > 0:
        total = max(1, min(200, int(total)))
        n = len(items)
        if total < n:                      # 总数比开头还少：只留前 total 个开头各 1 条
            return [(it, 1) for it in items[:total]]
        base, extra = divmod(total, n)
        return [(it, base + (1 if i < extra else 0)) for i, it in enumerate(items)]
    return [(it, max(1, min(20, int(it.get("count") or 1)))) for it in items]


@app.post("/api/name/preview")
def name_preview(body: dict = Body(...)):
    """命名模板实时预览。

    只做格式化，不查磁盘——真实导出遇到同名会自动往后顺延，这里不假装知道。
    前后端共用这一份规则，避免界面预览和实际文件名对不上。
    """
    tpl = str(body.get("name_tpl") or "").strip()
    if not tpl:
        return {"ok": True, "empty": True, "names": []}
    try:
        r = naming.preview(tpl, body.get("count") or 3)
    except naming.NameTemplateError as e:
        return {"ok": False, "msg": str(e)}
    r["ok"] = True
    return r


@app.post("/api/render")
def render(body: dict = Body(...)):
    with LOCK:
        p, pl = STATE["project"], STATE["plan"]
    if not p:
        raise HTTPException(400, "还没有导入剧集")
    if not pl:
        raise HTTPException(400, "先生成顺剪方案")
    title = (body.get("title") or p["name"]).strip()
    safe = _safe_name(title)
    tpl = _name_tpl(body)
    outdir = _out_dir(body, safe)
    if tpl:
        # 模板模式：末尾数字就是起始序号；同名文件由 Namer 顺延，不覆盖旧成片
        out = naming.Namer(outdir, tpl).next()
    else:
        out = outdir / f"{safe}.mp4"
        if out.exists():
            out = outdir / f"{safe}_{random.randint(1, 999)}.mp4"
    opts = _pack_opts(body, title)
    _job_start("render")

    def work():
        r = rendermod.render(p, pl, str(out), opts, _job_emit)
        r["dir"] = str(outdir)
        r["name"] = out.name
        return r

    _bg(work)
    return {"ok": True, "out": str(out), "dir": str(outdir)}


@app.post("/api/batch")
def batch(body: dict = Body(...)):
    """批量产出：[{hook:{g0,g1}, count:n, ends:[{g0,g1}|null,...]}]，
    可用 total 直接指定本批总条数；ends[k-1] 为第 k 条指定结尾钩子（null=自动轮换）。"""
    with LOCK:
        p, a = STATE["project"], STATE["analysis"]
    if not p or not a:
        raise HTTPException(400, "还没有导入剧集")
    if not a.get("beats"):
        raise HTTPException(400, NO_SUB_MSG)
    items = body.get("items") or []
    if not items:
        raise HTTPException(400, "先在高光钩子候选里用 ×N 选出要批量产出的开头")
    total = _to_int(body.get("total"))
    alloc = _alloc_items(items, total)
    if not alloc:
        raise HTTPException(400, "先在高光钩子候选里用 ×N 选出要批量产出的开头")
    target = float(body.get("target", 360))
    if target < 60 or target > 900:
        raise HTTPException(400, "目标时长请填 1-15 分钟")
    end_hook = body.get("end_hook")
    jitter = max(0.0, min(0.5, float(body.get("jitter", 0.12))))
    end_mode = str(body.get("end_mode") or "auto")
    tightness = float(body.get("tightness", 0.0))
    end_ep = _to_int(body.get("end_ep"))
    lead = _to_lead(body.get("hook_lead"))
    tm, tf = _to_tail(body)
    title = (body.get("title") or p["name"]).strip()
    safe = _safe_name(title)
    tpl = _name_tpl(body)
    outdir = _out_dir(body, safe)
    namer = naming.Namer(outdir, tpl) if tpl else None
    opts = _pack_opts(body, title)

    jobs = []
    for hi, (it, n) in enumerate(alloc, 1):
        # ends: 每条成片可单独指定结尾钩子 {g0,g1}（下标 = 第几条-1），null = 自动
        ends = it.get("ends") if isinstance(it.get("ends"), list) else []
        ends = list(ends)[:n] + [None] * max(0, n - len(ends))
        for k in range(1, n + 1):
            jobs.append((hi, k, n, it["hook"], ends[k - 1]))

    _job_start("batch")

    def work():
        results = []
        errors = []
        total_jobs = len(jobs)
        for idx, (hi, k, n, hook, end_pick) in enumerate(jobs):
            # 第 1 条用标准方案；第 k 条 vary=k-1：结尾强制轮换到第 k 优候选，
            # 而不是靠随机碰运气（随机大多落回 top1，两条就一模一样了）。
            # 本条指定了结尾（批量结尾指定面板）→ 用 point 模式精确卡到那里
            eh_k = None
            if isinstance(end_pick, dict) and end_pick.get("g1"):
                try:
                    g1 = min(p["total"], max(0.0, float(end_pick["g1"])))
                    if g1 > 0:
                        eh_k = {"g0": max(0.0, float(end_pick.get("g0") or 0)), "g1": g1}
                except (TypeError, ValueError):
                    eh_k = None
            seed = random.randrange(1, 2**31) if k > 1 else None
            try:
                pl = planmod.build(p, a["beats"], target=target, hook=hook,
                                   end_hook=eh_k or end_hook,
                                   end_mode="point" if eh_k else end_mode,
                                   seed=seed,
                                   jitter=jitter if seed else 0.0,
                                   tightness=tightness,
                                   end_ep=end_ep, vary=k - 1, lead=lead,
                                   tail_mode=tm, tail_fixed=tf)
            except Exception as e:  # noqa: BLE001  单个开头排不出方案，跳过别拖垮整批
                errors.append({"hook": hi, "copy": k, "msg": str(e)})
                print(f"[batch] 开头{hi} 第{k}条 跳过：{e}", flush=True)
                continue
            planmod.restat(p, pl)
            with LOCK:
                STATE["plan"] = pl
            # 序号按「实际开始渲染」的顺序发：方案排不出来的那条不占号，
            # 渲染阶段失败的那条占号作废（不回收），避免复用号撞上残留半成品
            out = namer.next() if namer else outdir / f"{safe}_开头{hi:02d}_{k:02d}.mp4"

            def emit(pct: float, msg: str, _idx=idx, _hi=hi, _k=k, _n=n):
                _job_emit((_idx + max(0.0, min(100.0, pct)) / 100.0) / total_jobs * 100.0,
                          f"开头{_hi} · 第{_k}/{_n}条 · {msg}")

            try:
                r = rendermod.render(p, pl, str(out), opts, emit)
            except Exception as e:  # noqa: BLE001  单条渲染失败，别把整批带下去
                errors.append({"hook": hi, "copy": k, "msg": f"渲染失败：{e}"})
                print(f"[batch] 开头{hi} 第{k}条 渲染失败：{e}", flush=True)
                continue
            # 指定的结尾若离目标时长太远会被引擎退回自动，这里按实际落点如实标注
            to_g1 = float((pl.get("to") or {}).get("g1") or 0)
            spec_used = bool(eh_k) and abs(to_g1 - eh_k["g1"]) < 0.5
            results.append({"video": r["video"], "duration": r["duration"],
                            "size_mb": r["size_mb"], "hook": hi, "copy": k,
                            "name": out.name,
                            "from": pl.get("from"), "to": pl.get("to"),
                            "end_reason": pl.get("end_reason"),
                            "end_spec": spec_used})
        return {"count": len(results), "results": results, "errors": errors,
                "dir": str(outdir), "total": total_jobs}

    _bg(work)
    return {"ok": True, "total": len(jobs), "dir": str(outdir)}


@app.post("/api/reveal")
def reveal(body: dict = Body(...)):
    """在系统文件管理器里打开成片所在文件夹。"""
    raw = str(body.get("path") or "").strip().strip('"')
    if not raw:
        raise HTTPException(400, "还没指定文件夹")
    p = Path(raw).expanduser()
    if not p.is_dir():
        raise HTTPException(404, "文件夹不存在，可能被移走了")
    try:
        if os.name == "nt":
            os.startfile(str(p))  # noqa: S606  本地单机工具，打开自己的目录
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"打不开资源管理器：{e}") from None
    return {"ok": True, "path": str(p)}


@app.get("/api/job")
def job():
    return STATE["job"] or {"state": "idle", "progress": 0, "msg": ""}


@app.get("/api/video")
def video(path: str):
    p = Path(path)
    if not p.exists() or p.suffix.lower() != ".mp4":
        raise HTTPException(404, "文件不存在")
    rp = p.resolve()
    with LOCK:
        exports = set(STATE["exports"])
    # 只放行项目内文件，和本次会话导出到外部文件夹的成片
    if ROOT not in rp.parents and str(rp.parent) not in exports:
        raise HTTPException(403, "只允许访问项目内文件")
    return FileResponse(str(p), media_type="video/mp4")


@app.get("/api/file")
def get_file(p: str):
    """本地素材直读：图片（logo/模板/尾帧）+ 视频（成片预览用的视频尾帧）。"""
    f = Path(p)
    mt = MEDIA_TYPES.get(f.suffix.lower())
    if not f.exists() or not mt:
        raise HTTPException(404, "文件不存在，或不是可预览的图片/视频")
    return FileResponse(str(f), media_type=mt)


@app.get("/api/src")
def src_video(ep: int):
    """某一集的原片视频流，给高光钩子/结尾候选做预览播放（支持 Range 拖动）。"""
    with LOCK:
        p = STATE["project"]
    if not p:
        raise HTTPException(400, "还没有导入剧集")
    try:
        e = projmod.ep_by_idx(p, int(ep))
    except KeyError:
        raise HTTPException(404, f"没有第 {ep} 集")
    f = Path(e["path"])
    if not f.exists():
        raise HTTPException(404, f"找不到第 {ep} 集的视频文件：{e['file']}")
    return FileResponse(str(f), media_type="video/mp4")


THUMB_LIMIT = 2000
_thumb_new = 0


def _prune_thumbs(limit: int = THUMB_LIMIT) -> None:
    """抽帧缓存 LRU：只留最近 limit 张。

    以前每张缩略图按「文件名+时间点」永久留盘，长时间多剧切换后会无限堆积。
    只在生成新图时低频触发，避免每次请求都扫一遍目录。
    """
    try:
        files = [p for p in THUMBS.glob("*.jpg") if p.is_file()]
        if len(files) <= limit:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[:len(files) - limit]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


@app.get("/api/frame")
def frame(ep: int, t: float = 1.0):
    global _thumb_new
    with LOCK:
        p = STATE["project"]
    if not p:
        raise HTTPException(400, "还没有导入剧集")
    e = projmod.ep_by_idx(p, ep)
    tt = max(0.0, min(e["dur"] - 0.1, t))
    key = THUMBS / f"{Path(e['file']).stem}_{int(tt * 2)}.jpg"
    if not key.exists():
        ffmpegx.run(["-y", "-ss", f"{tt:.2f}", "-i", e["path"], "-frames:v", "1",
                     "-vf", "scale=180:-2", "-q:v", "5", str(key)], timeout=60, check=False)
        _thumb_new += 1
        if _thumb_new % 50 == 0:
            _prune_thumbs()
    if not key.exists():
        raise HTTPException(404, "取帧失败")
    return FileResponse(str(key), media_type="image/jpeg")


VID_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".wmv"}


@app.get("/api/doctor")
def doctor():
    """自检：换电脑后画面黑、取帧失败时，一眼看出缺什么。
    检查 ffmpeg 能否执行、目录能否写、源视频还在不在、真取一帧试试。"""
    items: list[dict] = []

    def add(ok: bool, item: str, msg: str) -> None:
        items.append({"ok": bool(ok), "item": item, "msg": msg})

    ff = None
    try:
        ff = ffmpegx.ffmpeg()
        # -version 的输出在 stdout（probe 用的 stderr 是空的），别拿错流
        r = ffmpegx.run(["-version"], timeout=20, check=False)
        lines = (r.stdout or r.stderr).decode("utf-8", "replace").strip().splitlines()
        ver = lines[0] if lines else "可用"
        add(True, "ffmpeg 引擎", f"{ver}（{ff}）")
    except Exception as e:  # noqa: BLE001
        add(False, "ffmpeg 引擎", f"用不了：{e}。多半是 bin\\ffmpeg.exe 被杀毒软件隔离了，"
                                  f"把整个文件夹加进白名单再解压一次")

    for name, d in (("data 目录", DATA), ("缩略图缓存", THUMBS), ("输出目录", OUTPUT)):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".wtest"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add(True, name + "可写", str(d))
        except Exception as e:  # noqa: BLE001
            add(False, name + "可写", f"{d} 写不进去：{e}（别放在只读位置或受保护目录）")

    with LOCK:
        p = STATE["project"]
    if not p:                    # 刚重启还没人调过 /api/state 时，先把上次的项目接回来
        _restore_last_project()
        with LOCK:
            p = STATE["project"]
    if p and p.get("episodes"):
        e = p["episodes"][0]
        src = Path(e.get("path", ""))
        add(src.exists(), "源视频",
            f"第 {e['idx']} 集：{src}" if src.exists()
            else f"第 {e['idx']} 集文件不在了：{src}——视频文件夹没跟过来，或盘符/路径变了，"
                 f"重新导入一次这个项目的文件夹即可")
        if ff and src.exists():
            tmp = THUMBS / "_doctor.jpg"
            r = ffmpegx.run(["-y", "-ss", "1.00", "-i", str(src), "-frames:v", "1",
                             "-vf", "scale=180:-2", "-q:v", "5", str(tmp)],
                            timeout=60, check=False)
            ok = tmp.exists() and tmp.stat().st_size > 0
            if ok:
                add(True, "取帧实测", f"第 {e['idx']} 集能正常出图（{tmp.stat().st_size // 1024} KB）")
                tmp.unlink(missing_ok=True)
            else:
                err = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
                add(False, "取帧实测", "抽帧失败：" + " / ".join(err))
    else:
        add(True, "源视频", "还没导入项目，导入后再自检")

    try:
        free = shutil.disk_usage(str(OUTPUT)).free / 2 ** 30
        add(free > 2, "磁盘空间", f"输出盘还剩 {free:.1f} GB")
    except Exception:  # noqa: BLE001
        pass

    add(True, "运行环境", f"Python {sys.version.split()[0]} · {sys.platform}")
    return {"ok": all(x["ok"] for x in items), "items": items}



@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "x.png").suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp") and ext not in VID_EXTS:
        raise HTTPException(400, "只支持 png/jpg/webp 图片或 mp4/mov 等视频")
    name = f"{int(time.time())}_{random.randint(100, 999)}{ext}"
    dest = UPLOADS / name
    dest.write_bytes(await file.read())
    return {"path": str(dest), "name": file.filename}


@app.get("/api/media/info")
def media_info(p: str = ""):
    """探测上传的尾帧素材：图片还是视频、时长、并生成一张缩略图。"""
    src = p.strip().strip('"')
    f = Path(src)
    if not f.exists():
        raise HTTPException(404, "文件不存在")
    ext = f.suffix.lower()
    is_video = ext in VID_EXTS
    out = {"is_video": is_video, "duration": 0.0, "width": 0, "height": 0,
           "has_audio": False, "thumb": ""}
    key = THUMBS / f"up_{f.stem[:40]}_{f.stat().st_size}.jpg"
    if is_video:
        try:
            info = ffmpegx.probe(src)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"视频无法识别：{e}") from None
        out.update(duration=round(info["duration"], 2), width=info["width"],
                   height=info["height"], has_audio=info["has_audio"])
        if not key.exists():
            ffmpegx.run(["-y", "-ss", f"{min(1.0, max(0.1, info['duration'] / 2)):.2f}",
                         "-i", src, "-frames:v", "1", "-q:v", "4", str(key)],
                        timeout=60, check=False)
    else:
        try:
            from PIL import Image
            with Image.open(src) as im:
                out["width"], out["height"] = im.size
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "不是有效的图片文件") from None
        key = Path(src)
    if key.exists():
        out["thumb"] = "/api/file?p=" + requests_quote(str(key))
    return out


def requests_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


CARD_KINDS = ("overlay", "endframe")

NO_SUB_MSG = ("没有读到台词：这个文件夹里既没有字幕文件（srt / ass / vtt），"
              "画面里也没识别到硬字幕。可以点「从画面识别字幕」再试一次，"
              "或给每集配上字幕文件后重新扫描——字幕和视频放同一文件夹、同名或按集数对应均可。")


@app.post("/api/card/preview")
def card_preview(body: dict = Body(...)):
    """生成模板叠加 / 引导尾帧 预览图，不跑整片。"""
    with LOCK:
        p = STATE["project"]
    kind = body.get("kind", "overlay")
    if kind not in CARD_KINDS:
        raise HTTPException(400, "未知预览类型")
    size = body.get("size", "match")
    w, h = rendermod.pick_size(p, size) if p else (720, 1280)
    bg = None
    if p:
        e = p["episodes"][0]
        bg = str(THUMBS / f"preview_{e['idx']}.jpg")
        if not Path(bg).exists():
            ffmpegx.run(["-y", "-ss", "1", "-i", e["path"], "-frames:v", "1",
                         "-vf", f"scale={w}:-2", "-q:v", "4", bg], timeout=60, check=False)
    out = DATA / f"_card_{kind}.png"
    if kind == "overlay":
        src = body.get("overlay") or ""
        if not src or not Path(src).exists():
            raise HTTPException(400, "先上传包装模板 png")
        opacity = float(body.get("overlay_opacity", 1.0))
        pack.compose_preview(bg if bg and Path(bg).exists() else None,
                             src, out, w, h, opacity)
    else:  # endframe
        src = body.get("end_frame") or ""
        if not src or not Path(src).exists():
            raise HTTPException(400, "先上传引导尾帧（png 或 mp4）")
        if Path(src).suffix.lower() in VID_EXTS:
            # 视频尾帧：抽中段一帧作为预览（实际成片播放整段视频）
            try:
                d = ffmpegx.probe(src)["duration"]
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"视频无法识别：{e}") from None
            ffmpegx.run(["-y", "-ss", f"{min(1.0, max(0.1, d / 2)):.2f}", "-i", src,
                         "-frames:v", "1", "-vf",
                         f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                         f"crop={w}:{h}", "-q:v", "4", str(out)], timeout=60)
        else:
            pack.prepare_end_frame(src, out, w, h)
    return {"url": f"/api/card/show?kind={kind}&t={int(time.time() * 1000)}"}


@app.get("/api/card/show")
def card_show(kind: str = "overlay"):
    if kind not in CARD_KINDS:
        raise HTTPException(400, "未知预览类型")
    f = DATA / f"_card_{kind}.png"
    if not f.exists():
        raise HTTPException(404, "还没生成预览")
    return FileResponse(str(f), media_type="image/png")


@app.post("/api/folder/pick")
def pick_folder():
    """调系统选择框选文件夹（Windows）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        d = filedialog.askdirectory(title="选择剧集所在文件夹")
        root.destroy()
        return {"folder": d or ""}
    except Exception as e:  # noqa: BLE001
        return {"folder": "", "error": str(e)}


@app.get("/api/folder/list")
def folder_list(path: str = ""):
    """内置目录浏览器：path 为空列盘符，否则列出该目录下的子文件夹 + 视频/字幕文件。

    返回字段 dirs / videos / subs 保持向后兼容，额外提供 files / empty / notice。
    """
    clean = (path or "").strip().strip('"').strip("'").replace("\u3000", " ").strip()
    if not clean:
        return _list_drives()
    if os.name == "nt":
        # 盘符后面那个冒号是合法的，其余 Windows 禁止的字符要单独说清楚，
        # 否则会被误报成「路径不存在」，让人白查一遍移动硬盘
        tail = clean[2:] if len(clean) > 2 and clean[1] == ":" else clean
        bad = [c for c in '<>"|?*' if c in tail]
        if bad:
            raise HTTPException(
                400, f"路径里有 Windows 不允许的字符（{' '.join(bad)}）：{clean}。请重新粘贴完整路径"
            )
    try:
        p = Path(clean)
        exists = p.exists()
    except (OSError, ValueError) as e:
        raise HTTPException(
            400, f"路径格式不正确或无法访问：{clean}（{e}）。检查盘符是否插好、网络盘是否已连接"
        ) from None
    if not exists:
        raise HTTPException(
            404, f"路径不存在或当前访问不到：{clean}。移动硬盘 / 网络盘是不是没连上，或文件夹改名了？"
                 f"点下方「回到此电脑」重新选盘符，也可以直接在上方粘贴完整路径"
        )
    try:
        is_dir = p.is_dir()
    except OSError as e:
        raise HTTPException(403, f"无法判断这是不是文件夹：{clean}（{e}）") from None
    if not is_dir:
        raise HTTPException(400, f"这不是文件夹：{clean}。请选择装剧集的「文件夹」，不要直接选视频文件")
    return _list_dir(str(p))


def _list_drives() -> dict:
    """列出本机盘符；某个盘读不了只跳过它，不让整页失败。"""
    if os.name != "nt":
        return _list_dir(str(Path.home()))
    import string
    drives: list[dict] = []
    failed: list[str] = []
    for ch in string.ascii_uppercase:
        d = f"{ch}:\\"
        try:
            if Path(d).exists():
                drives.append({"name": f"{ch} 盘", "path": d})
        except (OSError, ValueError):
            failed.append(ch)
    notice = ""
    if not drives:
        notice = "没检测到可用盘符，可以直接在上方输入框粘贴完整路径后回车"
    elif failed:
        notice = "部分盘符暂时读不到，已跳过：" + "、".join(failed)
    return {"path": "", "parent": None, "dirs": drives,
            "files": [], "videos": 0, "subs": 0,
            "empty": not drives, "notice": notice}


def _list_dir(path_str: str) -> dict:
    """读一个目录：子文件夹 + 视频/字幕文件。

    只遍历一次 iterdir；单个坏链接 / 无权限条目只跳过自己，不再让整页 500。
    这里不复用 subs.list_videos()：那个函数是给导入流程用的，未做异常保护，
    在目录浏览器里直接调会因为一个读不了的条目把整个接口带崩。
    """
    p = Path(path_str)
    dirs: list[dict] = []
    files: list[dict] = []
    skipped = 0
    try:
        entries = list(p.iterdir())
    except PermissionError as e:
        raise HTTPException(
            403, f"没有权限读取这个文件夹：{p}。换一个目录，或右键「以管理员身份运行」启动工具（{e}）"
        ) from None
    except OSError as e:
        raise HTTPException(
            403, f"读取目录失败：{p}（{e}）。磁盘是不是拔了、网络盘掉线了，或路径太长？"
        ) from None

    for c in sorted(entries, key=lambda x: x.name.lower()):
        name = c.name
        if name.startswith((".", "$")) or name in ("System Volume Information",):
            continue
        try:
            is_dir = c.is_dir()
            is_file = c.is_file() if not is_dir else False
        except OSError:
            skipped += 1
            continue
        if is_dir:
            dirs.append({"name": name, "path": str(c)})
        elif is_file:
            ext = c.suffix.lower()
            if ext in subs.VIDEO_EXT:
                kind = "video"
            elif ext in subs.SUB_EXT:
                kind = "sub"
            else:
                continue
            try:
                size = c.stat().st_size
            except OSError:
                size = 0
            files.append({"name": name, "path": str(c), "kind": kind, "size": size})

    videos = sum(1 for f in files if f["kind"] == "video")
    subs_n = sum(1 for f in files if f["kind"] == "sub")
    notice = f"有 {skipped} 个条目读不到（权限不足或链接损坏），已跳过" if skipped else ""
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs,
            "files": files, "videos": videos, "subs": subs_n,
            "empty": not dirs and not files, "notice": notice}


@app.exception_handler(HTTPException)
def _http_err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
def _unhandled(request, exc: Exception):
    """兜底：任何未预期异常也把具体原因返给前端，别只给一句 Internal Server Error。"""
    msg = str(exc).strip() or exc.__class__.__name__
    return JSONResponse({"detail": f"服务内部错误：{msg}"}, status_code=500)


def _port_busy(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _ours_on_port(port: int) -> bool:
    """端口上是不是已经有一个枫的瞬剪在跑（别把别的软件当成自己）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    _clean_stale_work()
    port = int(os.environ.get("DC_PORT", 8780))
    url = f"http://127.0.0.1:{port}"
    if _port_busy(port) and _ours_on_port(port):
        # 已经开着一个枫的瞬剪了：直接把界面带到前台，不要抢端口报错
        print(f"\n  枫的瞬剪已经在运行：{url}\n  正在打开浏览器……\n")
        if not os.environ.get("DC_NO_BROWSER"):
            webbrowser.open(url)
        time.sleep(1.0)
        return
    print(f"\n  枫的瞬剪已启动：{url}\n  关闭此窗口即退出\n")
    if not os.environ.get("DC_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port,
                log_level=os.environ.get("DC_LOG", "warning"))


if __name__ == "__main__":
    main()

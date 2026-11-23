"""两段式导入 / 动作缓存 / 8 路并发的回归测试。

覆盖：
  A 缓存键：同一文件夹改剧名仍命中；不同目录同名文件夹互不串数据
  B 缓存隔离：未命中、坏 JSON、folder 元数据不符、老「按剧名」缓存迁移
  C 增量与失败：改片源只重扫该集；失败集下次自动重试
  D 并发与幂等：同缓存并发只解码一批；重复请求 scanned=0；force 才重扫
  E 并发度：默认 8 路上限、DC_ACT_WORKERS / DC_ACT_THREADS 覆盖
  F 接口：阶段字段、auto/defer/sync 边界、延后后 /api/acts 补齐
  G 真实小样片：首次扫描 / 二次命中 / 删除缓存

用法：runtime\\python.exe tests/test_scan_phases.py
输出：tests/results/scan_phases.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import ffmpegx, vision as V  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []
TMP = Path(tempfile.gettempdir()) / "dc_scan_phases"


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name +
          (f"   [{detail}]" if detail and not cond else ""))


def reset_tmp(sub: str = "") -> Path:
    d = TMP / sub if sub else TMP
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_proj(root: Path, name: str, n: int, folder_name: str = "剧",
              sub: str = "") -> dict:
    folder = root / (sub or name) / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    eps = []
    for i in range(1, n + 1):
        f = folder / f"{i:02d}.mp4"
        f.write_bytes(b"fake-video")
        eps.append({"idx": i, "file": f.name, "path": str(f),
                    "dur": 10.0, "has_audio": False, "offset": (i - 1) * 10.0})
    return {"name": name, "folder": str(folder), "episodes": eps, "total": n * 10.0}


def fake_episode(marker: str = "", calls: dict | None = None, fail: set[int] | None = None,
                 sleep: float = 0.0):
    def scan(path, has_audio=None, threads=None, timeout=None, use_hw=None):
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
            calls["active"] = calls.get("active", 0) + 1
            calls["peak"] = max(calls.get("peak", 0), calls["active"])
        if sleep:
            time.sleep(sleep)
        if calls is not None:
            calls["active"] -= 1
        ep = int(Path(path).stem) if Path(path).stem.isdigit() else 0
        if fail and ep in fail:
            raise RuntimeError("模拟第 %d 集坏文件" % ep)
        return {"events": [{"t0": 0.1, "t1": 0.4, "dur": 0.3,
                            "v": 1.0, "a": marker and 1.0 or 0.0,
                            "score": 1.0}],
                "motion": 1, "spikes": 0, "secs": 0.01}
    return scan


# ── A 缓存键 ────────────────────────────────────────
def test_keys() -> None:
    print("\n[A] 缓存键：文件夹名 + 路径哈希")
    a = {"name": "剧A", "folder": str(TMP / "a" / "剧A")}
    b = {"name": "改个名", "folder": str(TMP / "a" / "剧A")}
    c = {"name": "剧A", "folder": str(TMP / "b" / "剧A")}
    d = {"name": "没有目录"}
    ka, kb, kc, kd = V.cache_key(a), V.cache_key(b), V.cache_key(c), V.cache_key(d)
    ck("同一文件夹改剧名仍是同一个键", ka == kb, (ka, kb))
    ck("不同目录的同名文件夹键不同", ka != kc, (ka, kc))
    ck("键里带文件夹名，便于排查", ka.startswith("剧A~"), ka)
    ck("路径哈希长度固定为 8", len(ka.split("~")[-1]) == 8, ka)
    ck("没有文件夹时退回剧名（老项目兼容）", kd == "没有目录", kd)
    p = V.cache_path(ka, TMP)
    ck("缓存落在 data/act", p.parent == TMP / "data" / "act", p)
    ck("缓存文件名可安全落盘", p.suffix == ".json" and "~" in p.name, p.name)


# ── B 缓存隔离 / 未命中 / 迁移 ──────────────────────
def test_isolation() -> None:
    print("\n[B] 缓存隔离、未命中与老缓存迁移")
    root = reset_tmp("b")
    orig = V.scan_episode
    calls = {"n": 0}
    try:
        V.scan_episode = fake_episode("A", calls)
        a = make_proj(root, "剧A", 3, sub="a")
        r1 = V.scan_project(a, root, force=True)
        ck("首次全部扫描", r1["scanned"] == 3 and calls["n"] == 3, (r1["scanned"], calls["n"]))
        ck("首次无缓存命中", r1["cache_hit"] == 0, r1["cache_hit"])

        calls["n"] = 0
        a2 = dict(a)
        a2["name"] = "改了剧名"
        r2 = V.scan_project(a2, root)
        ck("改剧名仍命中同一份缓存", r2["scanned"] == 0 and calls["n"] == 0,
           (r2["scanned"], calls["n"]))
        ck("命中后事件完全一致", r2["events"] == r1["events"], len(r2["events"]))
        ck("缓存命中数正确", r2["cache_hit"] == 3, r2["cache_hit"])

        calls["n"] = 0
        b = make_proj(root, "剧A", 3, sub="b")
        r3 = V.scan_project(b, root)
        ck("不同目录同名文件夹不串数据", r3["scanned"] == 3 and calls["n"] == 3,
           (r3["scanned"], calls["n"]))
        ck("两份缓存文件并存", V.cache_file(a, root) != V.cache_file(b, root)
           and V.cache_file(a, root).exists() and V.cache_file(b, root).exists())

        # 坏 JSON → 当作未命中，而不是抛异常
        V.cache_file(b, root).write_text("{not json", encoding="utf-8")
        calls["n"] = 0
        r4 = V.scan_project(b, root)
        ck("坏缓存当未命中并重建", r4["scanned"] == 3 and calls["n"] == 3,
           (r4["scanned"], calls["n"]))

        # folder 元数据不符 → 整份不认（防路径哈希碰撞 / 文件夹被搬走）
        data = json.loads(V.cache_file(b, root).read_text(encoding="utf-8"))
        data["folder"] = str(root / "别的目录" / "剧A")
        V.cache_file(b, root).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        calls["n"] = 0
        r5 = V.scan_project(b, root)
        ck("folder 对不上时拒绝复用", r5["scanned"] == 3 and calls["n"] == 3,
           (r5["scanned"], calls["n"]))

        # 老「按剧名」缓存迁移：剧名一致 + 文件/时长对得上才认
        old_root = reset_tmp("b_old")
        V.scan_episode = fake_episode("old", calls)
        old = make_proj(old_root, "迁移剧", 2)
        legacy = old_root / "data" / "act" / "迁移剧.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"name": "迁移剧", "eps": {
            "1": {"file": "01.mp4", "dur": 10.0, "events": []},
            "2": {"file": "02.mp4", "dur": 10.0, "events": []}}}), encoding="utf-8")
        calls["n"] = 0
        r6 = V.scan_project(old, old_root)
        ck("老缓存被识别并迁移", r6["scanned"] == 0 and r6["cache_hit"] == 2,
           (r6["scanned"], r6["cache_hit"]))
        ck("迁移后写新键、清老文件",
           V.cache_file(old, old_root).exists() and not legacy.exists(),
           (V.cache_file(old, old_root).name, legacy.exists()))

        # 老缓存与当前片源对不上 → 不迁移，正常重扫
        wrong_root = reset_tmp("b_wrong")
        wrong = make_proj(wrong_root, "迁移剧", 2)
        legacy2 = wrong_root / "data" / "act" / "迁移剧.json"
        legacy2.parent.mkdir(parents=True, exist_ok=True)
        legacy2.write_text(json.dumps({"name": "迁移剧", "eps": {
            "1": {"file": "01.mp4", "dur": 99.0, "events": []}}}), encoding="utf-8")
        calls["n"] = 0
        r7 = V.scan_project(wrong, wrong_root)
        ck("对不上的老缓存不迁移", r7["scanned"] == 2 and calls["n"] == 2,
           (r7["scanned"], calls["n"]))
    finally:
        V.scan_episode = orig


# ── C 增量与失败重试 ────────────────────────────────
def test_incremental() -> None:
    print("\n[C] 增量补扫与失败重试")
    root = reset_tmp("c")
    orig = V.scan_episode
    calls = {"n": 0}
    try:
        V.scan_episode = fake_episode("", calls, fail={2})
        p = make_proj(root, "增量剧", 4)
        r1 = V.scan_project(p, root, force=True)
        ck("失败集被记录", len(r1["failed"]) == 1 and r1["failed"][0]["ep"] == 2, r1["failed"])
        ck("其余集正常产出", len(r1["events"]) == 3, len(r1["events"]))
        ck("失败集仍算待扫", r1["pending"] == 1, r1["pending"])

        V.scan_episode = fake_episode("", calls)
        calls["n"] = 0
        r2 = V.scan_project(p, root)
        ck("第二次只重试失败集", r2["scanned"] == 1 and calls["n"] == 1,
           (r2["scanned"], calls["n"]))
        ck("重试成功后全量事件齐全", len(r2["events"]) == 4 and r2["pending"] == 0,
           (len(r2["events"]), r2["pending"]))

        p["episodes"][2]["file"] = "03-new.mp4"
        calls["n"] = 0
        r3 = V.scan_project(p, root)
        ck("换片源只重扫这一集", r3["scanned"] == 1 and calls["n"] == 1,
           (r3["scanned"], calls["n"]))

        calls["n"] = 0
        r4 = V.scan_project(p, root, force=True)
        ck("force 才整剧重扫", r4["scanned"] == 4 and calls["n"] == 4,
           (r4["scanned"], calls["n"]))
    finally:
        V.scan_episode = orig


# ── D 并发与幂等 ────────────────────────────────────
def test_concurrent() -> None:
    print("\n[D] 并发去重与重复请求")
    root = reset_tmp("d")
    orig = V.scan_episode
    calls = {"n": 0, "active": 0, "peak": 0}
    old_env = os.environ.get("DC_ACT_WORKERS")
    os.environ["DC_ACT_WORKERS"] = "8"
    try:
        V.scan_episode = fake_episode("", calls, sleep=0.15)
        p = make_proj(root, "并发剧", 6)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(V.scan_project, p, root)
            time.sleep(0.04)
            f2 = pool.submit(V.scan_project, p, root)
            r1, r2 = f1.result(), f2.result()
        ck("并发调用只解码一批", calls["n"] == 6, calls["n"])
        ck("并发度达到 6 路（请求 6 集 / 上限 8）", calls["peak"] == 6, calls["peak"])
        ck("先到者扫全、后到者命中",
           sorted([r1["scanned"], r2["scanned"]]) == [0, 6],
           (r1["scanned"], r2["scanned"]))
        ck("两个调用返回同一份结果", r1["events"] == r2["events"], len(r1["events"]))

        calls["n"] = 0
        r3 = V.scan_project(p, root)
        ck("顺序重复请求幂等", r3["scanned"] == 0 and calls["n"] == 0,
           (r3["scanned"], calls["n"]))

        calls["n"] = 0
        r4 = V.scan_project(p, root, force=True)
        ck("显式 force 才重新解码", r4["scanned"] == 6 and calls["n"] == 6,
           (r4["scanned"], calls["n"]))

        # 项目被删：should_stop 生效，不留半份缓存
        q = make_proj(root, "中止剧", 3, sub="stop")
        r5 = V.scan_project(q, root, should_stop=lambda: True)
        ck("should_stop 立即中止", r5["aborted"] is True and r5["scanned"] == 0, r5.get("aborted"))
        ck("中止后不留下缓存", not V.cache_file(q, root).exists(), V.cache_file(q, root))
    finally:
        V.scan_episode = orig
        if old_env is None:
            os.environ.pop("DC_ACT_WORKERS", None)
        else:
            os.environ["DC_ACT_WORKERS"] = old_env


# ── E 并发度计算 ────────────────────────────────────
def test_workers() -> None:
    print("\n[E] 并发度与线程数")
    old_cpu = V.os.cpu_count
    old_w, old_t = os.environ.pop("DC_ACT_WORKERS", None), os.environ.pop("DC_ACT_THREADS", None)
    try:
        V.os.cpu_count = lambda: 16
        ck("16 核 → 8 路", V._scan_workers(40) == 8, V._scan_workers(40))
        V.os.cpu_count = lambda: 4
        ck("4 核 → 2 路（不硬上 8 路）", V._scan_workers(40) == 2, V._scan_workers(40))
        V.os.cpu_count = lambda: 32
        ck("再多的核也不超过 8 路", V._scan_workers(40) == 8, V._scan_workers(40))
        ck("待扫集数少于并发时按集数收", V._scan_workers(3) == 3, V._scan_workers(3))
        ck("没有待扫时至少 1 路", V._scan_workers(0) == 1, V._scan_workers(0))
        os.environ["DC_ACT_WORKERS"] = "5"
        ck("DC_ACT_WORKERS 可覆盖", V._scan_workers(40) == 5, V._scan_workers(40))
        os.environ["DC_ACT_WORKERS"] = "99"
        ck("环境变量也会被 8 路上限截断", V._scan_workers(40) == 8, V._scan_workers(40))
        os.environ.pop("DC_ACT_WORKERS", None)
        os.environ["DC_ACT_THREADS"] = "3"
        ck("DC_ACT_THREADS 可覆盖", V._scan_threads(8) == 3, V._scan_threads(8))
        os.environ.pop("DC_ACT_THREADS", None)
        V.os.cpu_count = lambda: 16
        ck("8 路 × 2 线程", V._scan_threads(8) == 2, V._scan_threads(8))
        from engine import hwaccel as H
        st = H.status()
        ck("状态带批量抓取建议", "batch" in st and isinstance(st["batch"].get("hardware"), bool), st.get("batch"))
    finally:
        V.os.cpu_count = old_cpu
        for k, v in (("DC_ACT_WORKERS", old_w), ("DC_ACT_THREADS", old_t)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── F 接口：阶段与两段式 ────────────────────────────
def test_api() -> None:
    print("\n[F] 两段式接口与阶段状态")
    import app as A

    ck("auto 0 集 → cached", A._acts_decision(0, "auto") == (False, "cached"), A._acts_decision(0, "auto"))
    ck("auto 8 集 → 同步", A._acts_decision(8, "auto") == (False, "inline"), A._acts_decision(8, "auto"))
    ck("auto 9 集 → 延后", A._acts_decision(9, "auto") == (True, "deferred"), A._acts_decision(9, "auto"))
    ck("defer 模式有活就延后", A._acts_decision(1, "defer") == (True, "deferred"), A._acts_decision(1, "defer"))
    ck("sync 模式一定同步", A._acts_decision(40, "sync") == (False, "inline"), A._acts_decision(40, "sync"))

    bad = False
    try:
        A.scan({"folder": "x", "acts": "__bad__"})
    except Exception as e:  # noqa: BLE001
        bad = getattr(e, "status_code", 0) == 400
    ck("非法 acts 模式返回 400", bad)
    with A.LOCK:
        A.STATE["job"] = None
    A._job_start("scan")
    ck("任务起始阶段 queued", A.STATE["job"]["phase"] == "queued", A.STATE["job"])
    A._job_emit(50, "x", "acts")
    ck("阶段可切到 acts", A.STATE["job"]["phase"] == "acts", A.STATE["job"])
    A._job_done({"ok": True})
    ck("完成后阶段 done", A.STATE["job"]["phase"] == "done", A.STATE["job"])
    with A.LOCK:
        A.STATE["job"] = None

    # 完整两段式：第一段延后，第二段按 force=False 补齐
    folder = reset_tmp("f") / "剧"
    folder.mkdir(parents=True, exist_ok=True)
    eps = [{"idx": i, "file": f"{i:02d}.mp4", "path": str(folder / f"{i:02d}.mp4"),
            "dur": 10.0, "width": 720, "height": 1280, "sub": "",
            "cues": [{"t0": 0, "t1": 2, "text": "滚出去"}],
            "offset": (i - 1) * 10.0, "has_audio": True} for i in range(1, 11)]
    beats = [{"id": 0, "ep": 1, "g0": 0, "g1": 2, "start": 0, "end": 2, "dur": 2,
              "text": "滚出去", "cues": 1, "score": 2, "hits": ["滚"]}]
    proj = {"name": "__scan_phase__", "folder": str(folder), "episodes": eps,
            "total": 100.0, "sub_episodes": 10, "ocr_eps": 0}
    ana = {"stats": {}, "beats": beats, "highlights": [], "hook_meta": {},
           "acts": 0, "endings": []}

    saved = {k: getattr(A.projmod, k) for k in ("scan", "save")}
    savedv = {k: getattr(A.vision, k) for k in ("pending", "cached_events", "scan_project")}
    saveda, savedpre = A.analyzemod.analyze, A._prewarm_thumbs
    savedemit = A._job_emit
    seen: list[str] = []
    force_arg = {"v": None}

    def rec_emit(pct, msg, phase=""):
        if phase:
            seen.append(phase)
        return savedemit(pct, msg, phase)

    try:
        A._prewarm_thumbs = lambda: None
        A.projmod.scan = lambda f, n: dict(proj, name=n)
        A.projmod.save = lambda p: None
        A.vision.pending = lambda p, r: 10
        A.vision.cached_events = lambda p, r: []
        A.analyzemod.analyze = lambda p: dict(ana)
        A._job_emit = rec_emit
        with A.LOCK:
            A.STATE.update(project=None, analysis=None, plan=None, job=None,
                           forgotten=set())
        A.scan({"folder": str(folder), "name": "__scan_phase__", "ocr": False, "acts": "auto"})
        j = {}
        for _ in range(120):
            j = A.STATE.get("job") or {}
            if j.get("state") in ("done", "error"):
                break
            time.sleep(0.05)
        res = j.get("result") or {}
        ai = res.get("acts_info") or {}
        ck("第一段正常完成", j.get("state") == "done", j)
        ck("第一段阶段覆盖 index/acts/analyze",
           {"index", "acts", "analyze"} <= set(seen), seen)
        ck("待扫 10 集时延后动作扫描",
           res.get("acts_mode") == "deferred" and ai.get("deferred") is True,
           (res.get("acts_mode"), ai))
        ck("第一段返回 pending_acts", res.get("pending_acts") == 10, res.get("pending_acts"))

        def fake_scan(p, root, progress=None, force=False, should_stop=None):
            force_arg["v"] = force
            if progress:
                progress(100, "动作扫描 10/10 集（8 路）")
            return {"events": [], "eps": 10, "scanned": 10, "total": 10, "failed": [],
                    "pending": 0, "cache_key": "k", "cache_file": "f", "cache_hit": 0,
                    "cache_written": True, "aborted": False, "cache_error": ""}

        A.vision.scan_project = fake_scan
        with A.LOCK:
            A.STATE["job"] = None
        A.scan_acts({})
        j2 = {}
        for _ in range(120):
            j2 = A.STATE.get("job") or {}
            if j2.get("state") in ("done", "error"):
                break
            time.sleep(0.05)
        res2 = j2.get("result") or {}
        ai2 = res2.get("acts_info") or {}
        ck("第二段正常完成", j2.get("state") == "done", j2)
        ck("第二段默认 force=False（重复请求幂等）", force_arg["v"] is False, force_arg)
        ck("补扫后 pending 归零", res2.get("pending_acts") == 0, res2.get("pending_acts"))
        ck("补扫记录 before/after",
           ai2.get("pending_before") == 10 and ai2.get("pending_after") == 0, ai2)
        ck("补扫阶段包含 acts/analyze", {"acts", "analyze"} <= set(seen), seen)
    finally:
        A._prewarm_thumbs = savedpre
        A.analyzemod.analyze = saveda
        A._job_emit = savedemit
        for k, v in saved.items():
            setattr(A.projmod, k, v)
        for k, v in savedv.items():
            setattr(A.vision, k, v)
        with A.LOCK:
            A.STATE.update(project=None, analysis=None, plan=None, job=None,
                           forgotten=set())


# ── G 真实小样片 ────────────────────────────────────
def test_real_fixture() -> None:
    print("\n[G] 真实小样片：首次扫描 / 二次命中 / 删除缓存")
    src = ROOT / "tests" / "fixtures" / "h264_360x640_6s" / "01.mp4"
    if not src.exists():
        ck("测试样片存在", False, src)
        return
    root = reset_tmp("g")
    folder = root / "真实剧"
    folder.mkdir(parents=True, exist_ok=True)
    info = ffmpegx.probe(src)
    eps = []
    for i in (1, 2):
        f = folder / f"{i:02d}.mp4"
        shutil.copyfile(src, f)
        eps.append({"idx": i, "file": f.name, "path": str(f),
                    "dur": round(info["duration"], 3),
                    "has_audio": bool(info["has_audio"]),
                    "offset": (i - 1) * info["duration"]})
    p = {"name": "真实剧", "folder": str(folder), "episodes": eps,
         "total": info["duration"] * 2}
    t = time.time()
    r1 = V.scan_project(p, root, force=True)
    ck("真实样片首次全部扫描", r1["scanned"] == 2, r1)
    ck("扫描结果带批量解码方式",
       isinstance(r1.get("batch_hw"), bool) and bool(r1.get("batch_reason")), r1.get("batch_reason"))
    ck("首次扫描落盘", V.cache_file(p, root).exists(), V.cache_file(p, root))
    t = time.time()
    r2 = V.scan_project(p, root)
    ck("二次扫描零解码", r2["scanned"] == 0 and time.time() - t < 1.0,
       (r2["scanned"], round(time.time() - t, 3)))
    ck("二次扫描事件一致", r2["events"] == r1["events"], len(r1["events"]))
    removed = V.drop_cache(p, root)
    ck("drop_cache 删除新键缓存", not V.cache_file(p, root).exists() and len(removed) >= 1, removed)


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for fn in (test_keys, test_isolation, test_incremental, test_concurrent,
               test_workers, test_api, test_real_fixture):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 单个用例崩了也要把其余跑完
            import traceback
            traceback.print_exc()
            ck(f"{fn.__name__} 未抛异常", False, str(e))
    bad = [c for c in CHECKS if not c["ok"]]
    out = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
           "secs": round(time.time() - t0, 2),
           "total": len(CHECKS), "failed": len(bad),
           "checks": CHECKS}
    (RESULTS / "scan_phases.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过，用时 {out['secs']}s")
    if bad:
        print("失败项：")
        for c in bad:
            print("  -", c["name"], c["detail"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

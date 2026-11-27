"""接口健壮性与数据落盘回归测试。

覆盖这次全面检查发现的问题：
  A 集号越界：/api/frame 要 404，不能把 KeyError 冒成 500
  B 动作缓存单条事件损坏：跳过坏条目，整部剧仍能打开
  C 项目存档原子写：不留半截 JSON、不留 .tmp、换剧仍生成 .bak
  D 数字参数容错：非法输入回落到默认值 / 400，不能 500
  E 渲染 concat 清单：路径含单引号要按 ffmpeg 规则转义
  F 损坏的「当前一批钩子」不阻断 analyze
  G 动作缓存写入不留 .tmp
  H 并发环境变量写错时兜到 1 路

用法：runtime\\python.exe tests/test_api_guards.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

import app as appmod  # noqa: E402
from engine import analyze as analyzemod  # noqa: E402
from engine import project as projmod  # noqa: E402
from engine import render as rendermod  # noqa: E402
from engine import vision as visionmod  # noqa: E402

CHECKS: list[dict] = []
RESULTS = ROOT / "tests" / "results"


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name +
          (f"   [{detail}]" if detail and not cond else ""))


def _ep(idx: int, offset: float, dur: float) -> dict:
    return {"idx": idx, "file": f"{idx:02d}.mp4", "path": f"x/{idx:02d}.mp4",
            "dur": dur, "width": 720, "height": 1280, "offset": offset,
            "has_audio": True, "cues": [], "sub": ""}


def test_frame_bad_ep():
    """A：集号越界是 404，且合法集号照常取帧。"""
    eps = [_ep(1, 0.0, 10.0)]
    appmod.STATE["project"] = {"name": "__guards__", "folder": "x",
                               "episodes": eps, "total": 10.0}
    called = {"n": 0}
    tmpdir = Path(tempfile.mkdtemp(prefix="dc_frame_"))
    shot = tmpdir / "frame.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xd9")
    orig = appmod._ensure_thumb
    appmod._ensure_thumb = lambda e, t: called.__setitem__("n", called["n"] + 1) or shot
    try:
        for bad in (99, 0, -3):
            try:
                appmod.frame(bad, 1.0)
                ck(f"第 {bad} 集应报错", False, "没有抛异常")
            except HTTPException as e:
                ck(f"第 {bad} 集返回 404", e.status_code == 404, e.status_code)
        appmod.frame(1, 1.0)
        ck("合法集号仍能取帧", called["n"] == 1, called)
    finally:
        appmod._ensure_thumb = orig
        appmod.STATE["project"] = None
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_cached_events_tolerant():
    """B：坏事件条目跳过，有效条目保持原来的时间轴换算。"""
    proj = {"episodes": [_ep(1, 0.0, 10.0), _ep(2, 10.0, 10.0)]}
    eps = {
        "1": {"file": "01.mp4", "dur": 10.0, "events": [
            {"t0": 1.0, "t1": 2.5, "v": 1.0, "a": 0.0, "score": 2.0},
            {"g0": 3, "g1": 4},                    # 老格式 / 手改坏
            {"t0": "坏", "t1": 4.0},                # 非数字
            {"t0": 5.0, "t1": 4.0},                 # 时间倒挂
        ]},
        "2": {"file": "02.mp4", "dur": 10.0, "events": [{"t0": 0.5, "t1": 1.5}]},
    }
    ev = visionmod._cached_events(proj, eps)
    ck("坏事件不抛异常", isinstance(ev, list), type(ev))
    ck("只保留 2 条有效事件", len(ev) == 2, [(e["ep"], e["g0"], e["g1"]) for e in ev])
    ck("第 1 集事件换算正确", (ev[0]["ep"], ev[0]["g0"], ev[0]["g1"]) == (1, 1.0, 2.5), ev[0])
    ck("第 2 集带上 offset", (ev[1]["ep"], ev[1]["g0"], ev[1]["g1"]) == (2, 10.5, 11.5), ev[1])
    ck("缺省 v/a/score 补 0", ev[1]["v"] == 0.0 and ev[1]["score"] == 0.0, ev[1])


def test_project_save_atomic():
    """C：原子写 + 无 .tmp 残留 + 换剧仍备份 + recent 能读到。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="dc_save_"))
    orig = projmod.PROJ
    projmod.PROJ = tmpdir
    try:
        p = {"name": "守卫测试剧", "folder": "D:/media/a", "total": 10.0,
             "episodes": [_ep(1, 0.0, 10.0)], "sub_episodes": 0}
        path = projmod.save(p)
        ck("存档写入成功", Path(path).exists(), path)
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        ck("存档内容完整", d["name"] == p["name"] and len(d["episodes"]) == 1, d.get("name"))
        leftovers = [x.name for x in tmpdir.iterdir() if x.name.endswith(".tmp")]
        ck("没有残留 .tmp", not leftovers, leftovers)

        # 同名但换了文件夹：旧存档要留 .bak
        p2 = dict(p, folder="D:/media/b")
        projmod.save(p2)
        ck("换文件夹时生成 .bak", (tmpdir / "守卫测试剧.json.bak").exists(),
           [x.name for x in tmpdir.iterdir()])
        ck("主存档换成新内容",
           json.loads(Path(path).read_text(encoding="utf-8"))["folder"] == "D:/media/b", "")
        ck("recent 能读到这部剧",
           any(x["name"] == "守卫测试剧" for x in projmod.recent(8)),
           [x["name"] for x in projmod.recent(8)])
    finally:
        projmod.PROJ = orig
        projmod._RECENT_CACHE.update(key=None, items=[])
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_numeric_guard():
    """D：非法数字回落默认值；目标时长非法给 400 而不是 500。"""
    ck("整数原样保留", appmod._to_num("360", 0) == 360.0, appmod._to_num("360", 0))
    ck("浮点原样保留", appmod._to_num(2.5, 0) == 2.5, appmod._to_num(2.5, 0))
    ck("空字符串回落默认", appmod._to_num("", 20) == 20.0, appmod._to_num("", 20))
    ck("None 回落默认", appmod._to_num(None, 20) == 20.0, appmod._to_num(None, 20))
    ck("乱码回落默认", appmod._to_num("abc", 14) == 14.0, appmod._to_num("abc", 14))
    ck("inf 回落默认", appmod._to_num(float("inf"), 7) == 7.0, appmod._to_num(float("inf"), 7))
    ck("nan 回落默认", appmod._to_num(float("nan"), 7) == 7.0, appmod._to_num(float("nan"), 7))

    opts = appmod._pack_opts({"crf": "", "logo_ratio": "abc",
                              "overlay_opacity": None, "end_frame_dur": "坏"}, "t")
    ck("crf 非法回落 20", opts["crf"] == 20, opts["crf"])
    ck("logo_ratio 非法回落 0.14", opts["logo_ratio"] == 0.14, opts["logo_ratio"])
    ck("opacity 非法回落 1.0", opts["overlay_opacity"] == 1.0, opts["overlay_opacity"])
    ck("尾帧时长非法回落 2.5", opts["end_frame_dur"] == 2.5, opts["end_frame_dur"])
    good = appmod._pack_opts({"crf": 18, "logo_ratio": 0.2}, "t")
    ck("合法值不被改写", good["crf"] == 18 and good["logo_ratio"] == 0.2, good)

    appmod.STATE["project"] = {"name": "__guards__", "folder": "x",
                               "episodes": [_ep(1, 0.0, 10.0)], "total": 10.0}
    appmod.STATE["analysis"] = {"beats": [
        {"id": 0, "ep": 1, "g0": 0.0, "g1": 2.0, "start": 0.0, "end": 2.0,
         "dur": 2.0, "text": "滚出去", "cues": 1, "score": 2.0, "hits": [],
         "act": 0.0, "aud": 0.0, "vis": 0.0}]}
    try:
        for body in ({"target": "abc"}, {"target": None}, {"target": "10分"}):
            try:
                appmod.make_plan(body)
                ck(f"目标时长 {body!r} 应报 400", False, "没有抛异常")
            except HTTPException as e:
                ck(f"目标时长 {body!r} → 400", e.status_code == 400, e.status_code)
        try:
            appmod.plan_ends({"target": "abc"})
            ck("plan/ends 非法时长也走 400", False, "没有抛异常")
        except HTTPException as e:
            ck("plan/ends 非法时长也走 400", e.status_code == 400, e.status_code)
        ck("合法目标时长仍能排出方案",
           appmod.plan_ends({"target": 60})["ends"] is not None, "")
    finally:
        appmod.STATE["project"] = None
        appmod.STATE["analysis"] = None


def test_concat_escape():
    """E：concat 清单的转义规则。"""
    line = rendermod._concat_line(Path("E:/顺剪/XX's 工作台/_r1/s000.mp4"))
    ck("单引号转义为 '\\''", "'\\''" in line, line)
    ck("路径用正斜杠（无反斜杠分隔符）",
       line.startswith("file 'E:/顺剪/XX") and "\\\\" not in line, line)
    ck("以 file 开头、换行结尾", line.startswith("file '") and line.endswith("'\n"), line)
    plain = rendermod._concat_line(Path("E:/a/b.mp4"))
    ck("普通路径保持原样", plain == "file 'E:/a/b.mp4'\n", plain)


def test_analyze_bad_batch():
    """F：存档里的当前一批钩子坏掉时，analyze 重新算而不是抛异常。"""
    proj = {"name": "t", "folder": "x", "total": 20.0,
            "episodes": [{"idx": 1, "file": "01.mp4", "path": "x/01.mp4", "dur": 20.0,
                          "width": 720, "height": 1280, "offset": 0.0, "sub": "a",
                          "sub_source": "file", "has_audio": True,
                          "cues": [{"i": 0, "start": 0.5, "end": 3.0, "text": "你给我滚出去"},
                                   {"i": 1, "start": 4.0, "end": 7.0,
                                    "text": "难道你不知道我是谁？"}]}],
            "sub_episodes": 1,
            "hook_current_items": [{"ep": "坏", "g1": "x"}],
            "hook_shown": [], "hook_current": []}
    try:
        r = analyzemod.analyze(proj)
        ck("坏批次不阻断分析", len(r["beats"]) == 2, len(r.get("beats") or []))
        ck("能重新算出一批候选", len(r["highlights"]) >= 1, len(r.get("highlights") or []))
    except Exception as e:  # noqa: BLE001
        ck("坏批次不阻断分析", False, f"{type(e).__name__}: {e}")


def test_write_cache_no_tmp():
    """G：动作缓存写入后不留 .tmp，且能被读回。"""
    root = Path(tempfile.mkdtemp(prefix="dc_cache_"))
    try:
        proj = {"name": "缓存剧", "folder": str(root / "素材" / "缓存剧"),
                "episodes": [_ep(1, 0.0, 10.0)]}
        key = visionmod.cache_key(proj)
        path = visionmod.cache_path(key, root)
        visionmod._write_cache(path, proj, key,
                               {"1": {"file": "01.mp4", "dur": 10.0,
                                      "events": [{"t0": 1.0, "t1": 2.0}]}})
        ck("缓存文件已写", path.exists(), path)
        tmp = [x.name for x in path.parent.iterdir() if x.name.endswith(".tmp")]
        ck("没有残留 .tmp", not tmp, tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        ck("缓存结构完整", data["key"] == key and "1" in data["eps"], list(data.get("eps") or {}))
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_worker_env_fallback():
    """H：并发环境变量写错时兜到 1 路，不抛异常。"""
    old = os.environ.get("DC_ACT_WORKERS")
    try:
        os.environ["DC_ACT_WORKERS"] = "abc"
        ck("非法并发数 → 1 路", visionmod._scan_workers(40) == 1, visionmod._scan_workers(40))
        os.environ["DC_ACT_WORKERS"] = "0"
        ck("0 并发 → 1 路", visionmod._scan_workers(40) == 1, visionmod._scan_workers(40))
        os.environ["DC_ACT_WORKERS"] = "5"
        ck("合法并发数生效", visionmod._scan_workers(40) == 5, visionmod._scan_workers(40))
    finally:
        if old is None:
            os.environ.pop("DC_ACT_WORKERS", None)
        else:
            os.environ["DC_ACT_WORKERS"] = old


def test_stale_tmp_cleanup():
    """I：启动清理会删掉过期的半截 .tmp，但保留 1 小时内的和正常项目。"""
    root = Path(tempfile.mkdtemp(prefix="dc_stale_"))
    proj = root / "projects"
    proj.mkdir(parents=True)
    old_tmp = proj / ".旧剧.json.1234.tmp"
    new_tmp = proj / ".新剧.json.5678.tmp"
    keep = proj / "正常剧.json"
    for f in (old_tmp, new_tmp, keep):
        f.write_text("{}", encoding="utf-8")
    stale = 2 * 3600
    os.utime(old_tmp, (old_tmp.stat().st_atime - stale, old_tmp.stat().st_mtime - stale))
    orig_root = appmod.ROOT
    appmod.ROOT = root
    try:
        appmod._clean_stale_work()
        ck("过期 .tmp 被清掉", not old_tmp.exists(), old_tmp.name)
        ck("新 .tmp 保留（可能正在写）", new_tmp.exists(), new_tmp.name)
        ck("正常项目不受影响", keep.exists(), keep.name)
    finally:
        appmod.ROOT = orig_root
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    for fn in (test_frame_bad_ep, test_cached_events_tolerant, test_project_save_atomic,
               test_numeric_guard, test_concat_escape, test_analyze_bad_batch,
               test_write_cache_no_tmp, test_worker_env_fallback,
               test_stale_tmp_cleanup):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ck(f"{fn.__name__} 未抛异常", False, str(e))
    bad = [c for c in CHECKS if not c["ok"]]
    out = {"when": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
           "total": len(CHECKS), "failed": len(bad), "checks": CHECKS}
    (RESULTS / "api_guards.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
    if bad:
        for c in bad:
            print("  -", c["name"], c["detail"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

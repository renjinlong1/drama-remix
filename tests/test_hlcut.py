"""高光剪辑测试：整剧选片、目标时长 / 条数、去尾帧、结尾指定、400MB 码率与渲染上限。

用法：runtime\\python.exe tests\\test_hlcut.py
输出：tests/results/hlcut.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import ffmpegx, hlcut, render, tailcard  # noqa: E402
import app as APP  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "h264_720x1280_15s" / "01.mp4"
RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name +
          (f"   [{detail}]" if detail and not cond else ""))


def _proj(n_ep: int = 20, dur: float = 300.0) -> dict:
    fix = str(FIX.resolve())
    return {
        "name": "hl_test", "total": n_ep * dur,
        "episodes": [{"idx": i, "file": f"{i:02d}.mp4", "path": fix,
                      "dur": dur, "offset": (i - 1) * dur,
                      "width": 1280, "height": 720, "has_audio": True}
                     for i in range(1, n_ep + 1)],
    }


def _beats(proj: dict, per_ep: int = 30) -> list[dict]:
    out, bid = [], 0
    for ep in proj["episodes"]:
        for i in range(per_ep):
            s = i * 9.5
            text = ("你给我滚出去！" if i % 3 == 0
                    else "你算什么东西，退婚！" if i % 3 == 1 else "为什么这样对我")
            out.append({
                "id": bid, "ep": ep["idx"], "g0": ep["offset"] + s,
                "g1": ep["offset"] + s + 4.6, "start": s, "end": s + 4.6,
                "dur": 4.6, "text": text, "cues": 1,
                "score": 3.0 + (i % 5),
                "hits": ["滚出去"] if i % 3 == 0 else ["退婚"],
            })
            bid += 1
    return out


def _no_tail():
    """规划逻辑测试先关掉真实抽帧，尾帧单独用合成片验证。"""
    orig_at, orig_for = tailcard.trim_at, tailcard.trim_for
    tailcard.trim_at = lambda *a, **k: (0.0, "none")
    tailcard.trim_for = lambda *a, **k: (0.0, "none")
    return orig_at, orig_for


def _restore_tail(orig) -> None:
    tailcard.trim_at, tailcard.trim_for = orig


# ── A 选片 / 时长 / 条数 ─────────────────────────────────────────────
def test_plan() -> None:
    proj = _proj()
    beats = _beats(proj)
    orig = _no_tail()
    try:
        r = hlcut.build(proj, beats, [], target=600)
    finally:
        _restore_tail(orig)
    p = r.get("plan") or {}
    ck("方案可用", r.get("ok") and bool(p), r.get("feedback"))
    ck("目标是 10 分钟", p.get("target") == 600, p.get("target"))
    ck("时长贴近目标", 600 * 0.95 <= p.get("estimated", 0) <= 600 * 1.02,
       p.get("estimated"))
    ck("条数由时长自动决定", p.get("clip_count", 0) >= 10, p.get("clip_count"))
    segs = p.get("segments") or []
    ck("按叙事顺序排列",
       all((a["ep"], a["start"]) <= (b["ep"], b["start"])
           for a, b in zip(segs, segs[1:])),
       [(s["ep"], s["start"]) for s in segs[:3]])
    ck("同集片段不重叠",
       all(not (a["ep"] == b["ep"] and a["g0"] < b["g1"] and b["g0"] < a["g1"])
           for i, a in enumerate(segs) for b in segs[i + 1:]),
       len(segs))
    ck("每段时长在 2.5~20 秒",
       all(2.5 - 0.01 <= s["dur"] <= 20.01 for s in segs),
       [round(s["dur"], 1) for s in segs[:5]])
    ck("覆盖多集", len({s["ep"] for s in segs}) >= 8,
       len({s["ep"] for s in segs}))
    ck("自动结尾有理由", bool(p.get("end_reason")), p.get("end_reason"))


# ── B 结尾：指定 / 自定义 ────────────────────────────────────────────
def test_endings() -> None:
    proj = _proj()
    beats = _beats(proj)
    orig = _no_tail()
    try:
        base = hlcut.build(proj, beats, [], target=300)
        key = base["plan"]["end_options"][0]["key"]
        r = hlcut.build(proj, beats, [], target=300, end_mode="point",
                        end_key=key)
        p = r.get("plan") or {}
        ck("指定结尾片段成功", r.get("ok") and p.get("end", {}).get("manual"),
           (r.get("feedback"), key))
        if p:
            last = p["segments"][-1]
            ck("指定片段排在最后一段", last.get("end_clip") is True and
               f"{last['ep']}@{last['g0']:.1f}" == key,
               (last.get("end_clip"), last["ep"], last["g0"], key))
            ck("指定结尾计入总时长",
               300 * 0.90 <= p["estimated"] <= 300 * 1.02, p["estimated"])

        r2 = hlcut.build(proj, beats, [], target=300, end_mode="custom",
                         end_ep=10, end_start=120, end_end=128)
        p2 = r2.get("plan") or {}
        ck("自定义结尾成功", r2.get("ok") and bool(p2), r2.get("feedback"))
        if p2:
            last = p2["segments"][-1]
            ck("自定义结尾参数准确",
               last["ep"] == 10 and abs(last["start"] - 120) < 0.01
               and abs(last["dur"] - 8) < 0.01,
               (last["ep"], last["start"], last["dur"]))
            ck("自定义结尾标记为手动", p2["end"]["manual"] is True)

        bad = hlcut.build(proj, beats, [], target=300, end_mode="custom",
                          end_ep=10, end_start=10, end_end=11)
        ck("过短的自定义结尾被拒绝", bad.get("ok") is False, bad.get("feedback"))

        r3 = hlcut.build(proj, beats, [], target=300, end_mode="point",
                         end_key="")
        ck("指定但没选时回退自动", r3.get("ok") and
           r3["plan"]["end_mode"] == "auto", r3.get("plan", {}).get("end_mode"))
    finally:
        _restore_tail(orig)


# ── C 尾帧：真实检测 + 规划应用 ──────────────────────────────────────
def test_tail_trim() -> None:
    tmp = Path(tempfile.gettempdir()) / "dc_hl_tailcase.mp4"
    ff = ffmpegx.ffmpeg()
    p = subprocess.run([ff, "-hide_banner", "-nostdin", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:d=8",
                        "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=25:d=4",
                        "-filter_complex",
                        "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p",
                        "-c:v", "libx264", "-preset", "ultrafast", str(tmp)],
                       capture_output=True, timeout=60,
                       creationflags=ffmpegx.CREATE_NO_WINDOW)
    try:
        ck("生成带定帧尾的测试片", p.returncode == 0 and tmp.exists(), p.returncode)
        sec, conf = tailcard.measure_at(str(tmp), 12.0)
        ck("片段末尾定帧被识别", sec >= 1.0 and conf in ("high", "mid"),
           (sec, conf))
        sec2, _ = tailcard.measure_at(str(tmp), 6.0)
        ck("运动结尾不误裁", sec2 == 0.0, sec2)
        ck("fixed 模式按固定秒数", tailcard.trim_at(str(tmp), 6.0, "fixed", 0.5)[0] == 0.5)
    finally:
        tmp.unlink(missing_ok=True)

    # 规划层：每段都去尾帧，裁切量写入 plan
    proj = _proj(10)
    beats = _beats(proj)
    orig_at, orig_for = tailcard.trim_at, tailcard.trim_for
    tailcard.trim_at = lambda *a, **k: (1.0, "card")
    tailcard.trim_for = lambda *a, **k: (1.0, "card")
    try:
        r = hlcut.build(proj, beats, [], target=240, tail_mode="auto")
    finally:
        _restore_tail((orig_at, orig_for))
    p = r.get("plan") or {}
    segs = p.get("segments") or []
    ck("每段都记录尾帧裁切",
       bool(segs) and all(float(s.get("tail_trim", 0)) > 0 for s in segs),
       [(s.get("tail_trim"), s.get("dur")) for s in segs[:3]])
    ck("尾帧总秒数汇总", p.get("tail_total", 0) > 0, p.get("tail_total"))
    ck("裁切后仍不短于 2.5 秒",
       all(s["dur"] >= 2.5 - 0.01 for s in segs), [s["dur"] for s in segs[:5]])


# ── D 400MB 码率预算 ─────────────────────────────────────────────────
def test_bitrate() -> None:
    bit = hlcut._bitrate(600, 400)
    ck("10 分钟 400MB 码率合理",
       4800 <= bit["video_kbps"] <= 5600, bit)
    ck("预计大小不超 400MB", bit["estimated_mb"] <= 400, bit)
    ck("上限被硬限制在 400MB",
       hlcut._bitrate(600, 1000)["max_size_mb"] == 400.0,
       hlcut._bitrate(600, 1000)["max_size_mb"])
    ck("短目标仍落在 400MB 内", hlcut._bitrate(120, 400)["estimated_mb"] <= 400,
       hlcut._bitrate(120, 400))


# ── E 渲染：码率 + 大小兜底 ──────────────────────────────────────────
def test_render_limit() -> None:
    info = ffmpegx.probe(str(FIX))
    proj = {"name": "hl", "total": 15.0, "episodes": [{
        "idx": 1, "file": "01.mp4", "path": str(FIX.resolve()),
        "dur": 15.0, "offset": 0.0, "width": info["width"],
        "height": info["height"], "has_audio": bool(info["has_audio"])}]}
    plan = {"segments": [
        {"id": 0, "ep": 1, "start": 0.0, "end": 4.0, "dur": 4.0, "keep": True},
        {"id": 1, "ep": 1, "start": 6.0, "end": 10.0, "dur": 4.0, "keep": True},
    ]}
    tmp = Path(tempfile.gettempdir())
    for cap in (4, 2):
        out = tmp / f"dc_hl_limit_{cap}.mp4"
        try:
            r = render.render(proj, plan, str(out), {
                "size": "match", "fit": "crop", "crf": 20,
                "vbitrate": 3000, "audio_kbps": 96, "max_size_mb": cap})
            ck(f"渲染不超过 {cap}MB", r["size_mb"] <= cap and r["within_limit"],
               r["size_mb"])
            ck(f"返回码率与段数（{cap}MB）",
               r["bitrate_kbps"] > 0 and r["segments"] == 2,
               (r["bitrate_kbps"], r["segments"]))
            if cap == 2:
                ck("超限时自动降码率重压", r["shrinks"] >= 1, r["shrinks"])
        finally:
            out.unlink(missing_ok=True)


# ── F 接口 ──────────────────────────────────────────────────────────
def test_api() -> None:
    proj = _proj(10)
    beats = _beats(proj)
    with APP.LOCK:
        old = (APP.STATE.get("project"), APP.STATE.get("analysis"),
               APP.STATE.get("hlplan"))
        APP.STATE.update(project=proj, analysis={"beats": beats}, hlplan=None)
    orig = _no_tail()
    try:
        r = APP.highlight_plan({"target": 240, "end_mode": "auto"})
        ck("POST /api/highlight/plan 可用",
           r.get("ok") and bool(APP.STATE.get("hlplan")), r.get("feedback"))
        ck("plan 写入 STATE", bool((APP.STATE.get("hlplan") or {}).get("segments")))
        from fastapi import HTTPException
        bad = False
        try:
            APP.highlight_plan({"target": 30})
        except HTTPException as e:
            bad = e.status_code == 400
        ck("目标时长越界返回 400", bad)
        with APP.LOCK:
            APP.STATE["hlplan"] = None
        bad = False
        try:
            APP.highlight_render({})
        except HTTPException as e:
            bad = e.status_code == 400
        ck("没方案时拒绝渲染", bad)
        with APP.LOCK:
            APP.STATE["hlplan"] = {"complete": False, "segments": [{"id": 0}]}
        bad = False
        try:
            APP.highlight_render({})
        except HTTPException as e:
            bad = e.status_code == 400
        ck("素材不足默认拒绝渲染", bad)
    finally:
        _restore_tail(orig)
        with APP.LOCK:
            APP.STATE.update(project=old[0], analysis=old[1], hlplan=old[2])


def main() -> int:
    print("高光剪辑测试 ·", FIX)
    if not FIX.exists():
        print("缺少测试片源，请先跑 gen_fixtures.py")
        return 2
    for fn in (test_plan, test_endings, test_tail_trim, test_bitrate,
               test_render_limit, test_api):
        print("─", fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ck(fn.__name__ + " 未抛异常", False, repr(e))
    ok = sum(1 for c in CHECKS if c["ok"])
    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "hlcut.json").write_text(
        json.dumps({"ok": len(bad) == 0, "total": len(CHECKS),
                    "passed": ok, "failed": bad},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{ok}/{len(CHECKS)} 通过；失败 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())

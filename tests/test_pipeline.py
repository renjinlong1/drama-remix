"""视频处理链路系统测试。

覆盖：容器/编码、竖屏与横屏分辨率、时长、有无音轨、字幕配对、
节拍拆解、钩子候选、顺剪方案、实际出片，以及「同一输入两次结果一致」。

用法：runtime\\python.exe tests\\test_pipeline.py
输出：tests/results/test_results.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ROOT, FIX, RESULTS, cases, case_dir, fingerprint, temp_counts  # noqa: E402

from engine import ffmpegx, project as projmod, subs
from engine import analyze as analyzemod, plan as planmod, render as rendermod

OLD_FIELDS = {"id", "g0", "g1", "dur", "ep", "text", "score", "act", "susp",
              "at_end", "kind", "hits", "beats"}
NEW_FIELDS = {"types", "primary_type", "primary_label", "confidence", "signals",
              "evidence", "source", "has_dialog", "rank_score", "rank_reason",
              "dedup_key", "quality"}
RENDER_CASES = {"h264_360x640_6s", "mkv_720x1280_15s", "h264_1280x720_10s"}


def run_case(c: dict, do_render: bool) -> dict:
    folder = case_dir(c["name"])
    rec: dict = {"case": c["name"], "checks": [], "errors": []}

    def ck(name, cond, detail=""):
        rec["checks"].append({"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            rec["errors"].append(f"{name}: {detail}")

    try:
        vids = subs.list_videos(folder)
        ck("视频可列举", len(vids) == c.get("eps", 1),
           f"找到 {len(vids)} 个，期望 {c.get('eps', 1)}")
        info = ffmpegx.probe(vids[0])
        ck("分辨率", (info["width"], info["height"]) == (c["w"], c["h"]),
           f"{info['width']}x{info['height']}，期望 {c['w']}x{c['h']}")
        ck("时长", abs(info["duration"] - c["dur"]) < 0.8,
           f"{info['duration']:.2f}s，期望 {c['dur']}s")
        ck("音轨", info["has_audio"] == c["audio"],
           f"has_audio={info['has_audio']}，期望 {c['audio']}")

        eps = c.get("eps", 1)
        proj = projmod.scan(str(folder), "t_" + c["name"])
        ck("导入分集", len(proj["episodes"]) == eps,
           f"{len(proj['episodes'])} 集，期望 {eps}")
        ck("字幕配对", proj["sub_episodes"] == eps
           and all(len(e["cues"]) > 0 for e in proj["episodes"]),
           f"字幕集={proj['sub_episodes']} 首集台词={len(proj['episodes'][0]['cues'])}")
        ck("整剧时间轴", abs(proj["total"] - c.get("total", c["dur"])) < 1.0,
           f"{proj['total']:.1f}s")

        a = analyzemod.analyze(proj)
        ck("节拍拆解", len(a["beats"]) > 0, f"{len(a['beats'])} 个节拍")
        ck("钩子字段兼容", all(OLD_FIELDS <= set(h) and NEW_FIELDS <= set(h)
                          for h in a["highlights"]),
           f"{len(a['highlights'])} 条候选")
        # 30 秒是 plan.build 的成片下限；统一用 60 秒目标，多集素材都能凑满
        target = 60.0
        pl = planmod.build(proj, a["beats"], target=target)
        ck("顺剪方案", pl["estimated"] > 0 and len(pl["segments"]) > 0,
           f"estimated={pl['estimated']:.1f}s segments={len(pl['segments'])}")

        # 同一输入再跑一遍，输出结果必须完全一致（优化不得改变行为）
        a2 = analyzemod.analyze(proj)
        pl2 = planmod.build(proj, a2["beats"], target=target)
        fp1 = fingerprint(proj, a, pl)
        fp2 = fingerprint(proj, a2, pl2)
        ck("结果一致", fp1 == fp2, f"{fp1} / {fp2}")
        rec["fingerprint"] = fp1

        if do_render:
            out = FIX / "_out" / (c["name"] + ".mp4")
            out.parent.mkdir(parents=True, exist_ok=True)
            r = rendermod.render(proj, pl, str(out),
                                 {"size": "match", "fit": "crop", "crf": 28,
                                  "preset": "ultrafast"})
            ck("成片存在", out.exists() and out.stat().st_size > 1000,
               f"{out.stat().st_size if out.exists() else 0} 字节")
            oi = ffmpegx.probe(out)
            ck("成片规格", (oi["width"], oi["height"]) == (c["w"], c["h"]),
               f"{oi['width']}x{oi['height']}")
            ck("成片可播", oi["duration"] > 0.5, f"{oi['duration']:.2f}s")
            rec["render"] = {"duration": round(oi["duration"], 2),
                             "size_kb": round(out.stat().st_size / 1024, 1),
                             "reported": r["duration"]}
            out.unlink(missing_ok=True)
    except Exception as e:
        rec["errors"].append(f"异常 {type(e).__name__}: {e}")
        rec["trace"] = traceback.format_exc(limit=3)
    return rec


def test_leak_guards() -> list[dict]:
    """验证两处泄漏修复：渲染异常仍清中间目录、抽帧缓存有上限。"""
    import shutil
    import tempfile
    from pathlib import Path as P

    checks: list[dict] = []

    def ck(name, cond, detail=""):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    c = cases()[0]
    proj = projmod.scan(str(case_dir(c["name"])), "leak_test")
    a = analyzemod.analyze(proj)
    pl = planmod.build(proj, a["beats"], target=60.0)
    bad = dict(pl)
    bad["segments"] = [dict(pl["segments"][0], ep=9999)]  # 故意让 _cut 抛错
    before = temp_counts()["render_dirs"]
    out = FIX / "_out" / "_leak_probe.mp4"
    try:
        rendermod.render(proj, bad, str(out),
                         {"size": "match", "fit": "crop", "crf": 28, "preset": "ultrafast"})
    except Exception:
        pass
    after = temp_counts()["render_dirs"]
    ck("渲染失败不残留中间目录", after == before, f"{before} -> {after}")
    out.unlink(missing_ok=True)

    try:
        import app as webapp  # noqa: WPS433  只在测试里导入，避免污染基准进程
        tmp = P(tempfile.mkdtemp(prefix="feng_thumbs_"))
        old = webapp.THUMBS
        webapp.THUMBS = tmp
        try:
            for i in range(30):
                (tmp / f"t{i:03d}.jpg").write_bytes(b"x")
            webapp._prune_thumbs(limit=10)
            left = len(list(tmp.glob("*.jpg")))
            ck("抽帧缓存有上限", left == 10, f"30 -> {left}")
        finally:
            webapp.THUMBS = old
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        ck("抽帧缓存有上限", False, f"{type(e).__name__}: {e}")
    return checks


def main() -> int:
    cs = cases()
    if not cs:
        print("请先运行 gen_fixtures.py 生成素材")
        return 2
    RESULTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    before = temp_counts()
    recs = []
    for c in cs:
        r = run_case(c, c["name"] in RENDER_CASES)
        recs.append(r)
        flag = "PASS" if not r["errors"] else "FAIL"
        print(f"  {flag} {c['name']}  checks={len(r['checks'])}  fp={r.get('fingerprint', '-')}")
        for e in r["errors"][:3]:
            print("       -", e)
    guards = test_leak_guards()
    for g in guards:
        print(f"  {'PASS' if g['ok'] else 'FAIL'} {g['name']}  {g['detail']}")
    after = temp_counts()
    failed = sum(1 for r in recs if r["errors"]) + sum(1 for g in guards if not g["ok"])
    out = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cases": len(recs),
        "passed": len(recs) - sum(1 for r in recs if r["errors"]),
        "checks": sum(len(r["checks"]) for r in recs) + len(guards),
        "failed": failed,
        "guards": guards,
        "elapsed_s": round(time.time() - t0, 1),
        "temp_before": before, "temp_after": after,
        "records": recs,
    }
    (RESULTS / "test_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"通过 {out['passed']}/{out['cases']} 个用例 + {len(guards)} 项泄漏防护，"
          f"共 {out['checks']} 项断言，用时 {out['elapsed_s']}s；"
          f"临时文件 {before} -> {after}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

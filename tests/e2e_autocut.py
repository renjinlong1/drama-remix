"""自动混剪端到端：识别 → 选片 → 真实渲染 mp4（含软转场淡入淡出）。

用法：runtime\\python.exe tests/e2e_autocut.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import analyze as analyzemod  # noqa: E402
from engine import autocut as ac  # noqa: E402
from engine import ffmpegx  # noqa: E402
from engine import multimodal as mm  # noqa: E402
from engine import project as pm  # noqa: E402
from engine import render as rendermod  # noqa: E402

CHECKS: list[dict] = []


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


FIX = ROOT / "tests" / "fixtures" / "h264_360x640_6s"


def main():
    proj = pm.scan(FIX, "__ac_e2e__")
    proj["acts"] = []
    a = analyzemod.analyze(proj)
    with tempfile.TemporaryDirectory(prefix="dc_ace2e_") as td:
        tmp = Path(td)
        scanned = mm.scan_project(proj, tmp, force=True)
        ck("全剧识别完成", scanned["scanned"] == len(proj["episodes"]), scanned)
        raw = json.loads(Path(scanned["path"]).read_text(encoding="utf-8"))
        fused = mm.fuse(proj, a["beats"], [], raw, a["highlights"])
        ck("融合置信度提升", fused["stats"]["confidence"] > 0.6, fused["stats"]["confidence"])

        r = ac.build(proj, fused["units"], target=36, style="calm", transition="soft")
        ck("方案生成成功", r["ok"] and r["plan"] is not None, r["feedback"])
        plan = r["plan"]
        ck("软转场已写入方案", any(s["fade_in"] > 0 or s["fade_out"] > 0
                             for s in plan["segments"]), "")
        out = tmp / "auto_remix.mp4"
        logs = []

        def emit(pct, msg):
            logs.append((round(pct), msg))

        res = rendermod.render(proj, plan, str(out),
                               {"size": "360x640", "crf": 28, "preset": "ultrafast"}, emit)
        ck("成片文件落盘", out.exists() and out.stat().st_size > 10000,
           out.stat().st_size if out.exists() else 0)
        info = ffmpegx.probe(out)
        ck("成片时长贴近目标", abs(info["duration"] - plan["estimated"]) <= 3.0,
           (round(info["duration"], 2), plan["estimated"]))
        ck("成片分辨率正确", (info["width"], info["height"]) == (360, 640),
           (info["width"], info["height"]))
        ck("渲染段数一致", res["segments"] == len(plan["segments"]),
           (res["segments"], len(plan["segments"])))
        ck("渲染进度覆盖全链路", logs and logs[-1][0] == 100 and any(
           "片段" in m for _p, m in logs), logs[-3:])
        ck("关键信息进成片", r["metrics"]["key_coverage"] >= 0.5, r["metrics"]["key_coverage"])
        ck("主题一致度达标", r["metrics"]["topic_purity"] >= 0.8, r["metrics"]["topic_purity"])
        print(f"\n  成片 {info['duration']:.2f}s / {info['width']}x{info['height']} / "
              f"{out.stat().st_size / 1048576:.2f}MB / {res['segments']} 段")

    bad = [c for c in CHECKS if not c["ok"]]
    print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
    if bad:
        print("失败：", [c["name"] for c in bad])
        sys.exit(1)


if __name__ == "__main__":
    main()

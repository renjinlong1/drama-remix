"""自动混剪单测：主题 / 时长 / 节奏约束、反馈分支、叙事顺序与转场。

用法：runtime\\python.exe tests/test_autocut.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import analyze as analyzemod  # noqa: E402
from engine import autocut as ac  # noqa: E402
from engine import multimodal as mm  # noqa: E402
from engine import project as pm  # noqa: E402

CHECKS: list[dict] = []


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


FIX = ROOT / "tests" / "fixtures" / "h264_360x640_6s"
p = pm.scan(FIX, "__ac_unit__")
p["acts"] = []
a = analyzemod.analyze(p)
# 直接构造缓存结构，测试不往 data/ 里写东西
raw = {"version": mm.CACHE_VERSION, "name": p["name"],
       "eps": {str(ep["idx"]): mm.scan_episode(ep) for ep in p["episodes"]}}
f = mm.fuse(p, a["beats"], [], raw, a["highlights"])
units = f["units"]
ck("素材单元就绪", len(units) >= 8, len(units))

# ── 正常方案 ─────────────────────────────────────────────────────
r = ac.build(p, units, target=36, style="steady")
ck("基础方案成功", r["ok"] and r["plan"] is not None, r["feedback"])
plan = r["plan"] or {}
segs = plan.get("segments", [])
ck("片段非空", len(segs) >= 3, len(segs))
ck("成片贴近目标时长", abs(sum(s["dur"] for s in segs) - 36) <= 2.0,
   sum(s["dur"] for s in segs))
ck("全部片段可渲染", all(s["dur"] > 0 and s["start"] >= 0 and s["ep"] > 0 for s in segs), "")
ck("叙事顺序不倒叙", all((segs[i]["ep"], segs[i]["start"]) <=
                     (segs[i + 1]["ep"], segs[i + 1]["start"])
                     for i in range(len(segs) - 1)), "")
starts = {(u["ep"], u["start"]) for u in units}
ck("切点落在台词边界", all((s["ep"], s["start"]) in starts for s in segs),
   [(s["ep"], s["start"]) for s in segs[:3]])
ck("关键信息保底达成", r["metrics"]["key_coverage"] >= 0.5, r["metrics"]["key_coverage"])
ck("转场代价有数值", 0 <= r["metrics"]["transition_avg"] <= 1, r["metrics"]["transition_avg"])
ck("方案可直接送渲染", plan.get("mode") == "auto_remix" and all(
   k in segs[0] for k in ("ep", "start", "dur", "keep")), "")

# ── 主题冲突 ─────────────────────────────────────────────────────
bad = ac.build(p, units, topic="量子物理", target=36)
ck("主题冲突给出明确反馈", (not bad["ok"] and bad["feedback"]["code"] == "topic_conflict"),
   bad["feedback"])
ck("主题冲突列出识别结果", "识别到的主要主题" in bad["feedback"]["msg"], bad["feedback"]["msg"])
ck("主题冲突给可操作建议", len(bad["feedback"]["hints"]) >= 2, bad["feedback"]["hints"])
ck("主题冲突不出方案", bad["plan"] is None, "")

# ── 素材不足 ─────────────────────────────────────────────────────
short = ac.build(p, units, target=600)
ck("素材不足给出明确反馈", (not short["ok"] and short["feedback"]["code"] == "insufficient"),
   short["feedback"])
ck("素材不足列出可用秒数", "只有" in short["feedback"]["msg"], short["feedback"]["msg"])
ck("素材不足建议降时长", any("降低" in h or "降到" in h for h in short["feedback"]["hints"]),
   short["feedback"]["hints"])

# ── 节奏不匹配 ───────────────────────────────────────────────────
rh = ac.build(p, units, target=36, style="burst")
ck("节奏不匹配给出明确反馈", (not rh["ok"] and rh["feedback"]["code"] == "rhythm_mismatch"),
   rh["feedback"])
m = rh["feedback"]["metrics"]
ck("节奏反馈含分布与贴合度", "pace_spread" in m and "style_fit" in m and "best_style" in m, m)
ck("节奏反馈推荐替代风格", any("改用" in h for h in rh["feedback"]["hints"]),
   rh["feedback"]["hints"])
ck("舒缓风格可用", ac.build(p, units, target=36, style="calm")["ok"] is True, "")

# ── 放宽限制 ─────────────────────────────────────────────────────
relaxed = ac.build(p, units, target=600, style="burst", relax=True)
ck("放宽限制后仍出方案", relaxed["ok"] and relaxed["plan"] is not None, relaxed["feedback"])
ck("放宽限制标注短片", any("偏短" in w for w in relaxed["plan"]["warnings"]),
   relaxed["plan"]["warnings"])
ck("目标时长超上限被夹住", relaxed["plan"]["target"] <= 900, relaxed["plan"]["target"])

# ── 转场 ─────────────────────────────────────────────────────────
soft = ac.build(p, units, target=36, style="calm", transition="soft")
ss = soft["plan"]["segments"]
ck("软转场生成淡入淡出", any(s["fade_in"] > 0 or s["fade_out"] > 0 for s in ss),
   [(s["fade_in"], s["fade_out"]) for s in ss])
ck("淡化时长不超过片段 1/5", all(s["fade_in"] <= s["dur"] / 5 + 0.01
                            and s["fade_out"] <= s["dur"] / 5 + 0.01 for s in ss), "")
hard = ac.build(p, units, target=36, style="calm", transition="cut")
ck("硬切模式不加淡化", all(s["fade_in"] == 0 and s["fade_out"] == 0
                      for s in hard["plan"]["segments"]), "")

# ── 转场代价：越不像越贵 ─────────────────────────────────────────
base = units[0]
twin = json.loads(json.dumps(base))
far = json.loads(json.dumps(base))
far["topics"] = {k: (1.0 if k == "日常过渡" else 0.0) for k in mm.TOPIC_ORDER}
far["emotion"]["label"] = "温暖亲密"
far["scene"].update(bright=0.9, warm=0.2, color=0.8)
far["rhythm"].update(pace=0.95, energy=0.95)
near_cost = ac.transition_cost(base, twin)
far_cost = ac.transition_cost(base, far)
ck("相同片段转场代价低", near_cost < 0.05, near_cost)
ck("差异大转场代价高", far_cost > near_cost + 0.2, (near_cost, far_cost))
ck("转场代价封顶 1", 0 <= far_cost <= 1, far_cost)

# ── 空输入 / 降级 ────────────────────────────────────────────────
empty = ac.build(p, [], target=60)
ck("空素材给出反馈", (not empty["ok"] and empty["feedback"]["code"] == "insufficient"),
   empty["feedback"])
textonly = mm.fuse(p, a["beats"], [], None, a["highlights"])
degraded = ac.build(p, textonly["units"], target=36)
ck("纯文本降级仍可出方案", degraded["ok"] is True, degraded["feedback"])
ck("降级明确提示", any("深度识别" in w for w in degraded["plan"]["warnings"]),
   degraded["plan"]["warnings"])
ck("百分位同分不分高低", ac._percentile_ranks([0.2] * 5) == [0.5] * 5,
   ac._percentile_ranks([0.2] * 5))

# ── 前后端接线契约 ──────────────────────────────────────────────
APP = (ROOT / "app.py").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
RENDER = (ROOT / "engine" / "render.py").read_text(encoding="utf-8")
for path in ("/api/multimodal", "/api/multimodal/scan",
             "/api/autocut/plan", "/api/autocut/render"):
    ck("接口已注册 " + path, f'"{path}"' in APP, path)
ck("前端有自动混剪页签", 'data-t="auto"' in HTML and 'id="paneAuto"' in HTML, "")
ck("前端有目标主题/时长/节奏", all(k in HTML for k in
   ("acTopic", "acTarget", "acStyle", "acTrans", "acRelax")), "")
ck("前端有反馈与片段清单", all(k in JS for k in
   ("function acFeedback", "function acMetrics", "function acPlanView")), "")
ck("渲染支持软转场淡入淡出", "fade=t=in" in RENDER and "afade=t=out" in RENDER, "")
ck("删除项目会清多模态缓存", "multimodal" in (ROOT / "engine" / "project.py").read_text(encoding="utf-8"), "")

bad = [c for c in CHECKS if not c["ok"]]
print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
if bad:
    print("失败：", [c["name"] for c in bad])
    sys.exit(1)

"""多模态识别单测：中英主题、自由主题匹配、底层采样、融合字段与缓存。

用法：runtime\\python.exe tests/test_multimodal.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import analyze as analyzemod  # noqa: E402
from engine import multimodal as mm  # noqa: E402
from engine import project as pm  # noqa: E402

CHECKS: list[dict] = []


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


FIX = ROOT / "tests" / "fixtures" / "h264_360x640_6s"

# ── 文本主题 ─────────────────────────────────────────────────────
cn = mm.topic_scores("滚出去！你算什么东西，董事长来了，亲子鉴定结果出来了")
ck("中文冲突主题最高", max(cn, key=cn.get) == "冲突对抗", cn)
ck("中文身份线索被识别", cn["身份真相"] > 0.1, cn)
en = mm.topic_scores("I love you, my dear husband, please don't leave me")
ck("英文亲密主题被识别", en["亲密情感"] > 0.2, en)
ck("英文复合词边界匹配", mm.topic_scores("This marriage was a deal")["家族权谋"] > 0, "")
ck("空文本不臆造主题", all(v == 0 for v in mm.topic_scores("").values()), mm.topic_scores(""))
ck("无命中文本归到日常过渡", mm.topic_scores("嗯")[ "日常过渡"] > 0, "")

u = {"text": "我要报复他们，滚出去", "topics": mm.topic_scores("我要报复他们，滚出去")}
ck("黑话「复仇」归入冲突", mm.query_match("复仇", u)[1] == "主题词库", mm.query_match("复仇", u))
ck("别名「甜宠」归入亲密", mm.query_match("甜宠", u)[0] < 0.4, mm.query_match("甜宠", u))
ck("字面命中给满分", mm.query_match("滚出去", u)[0] == 1.0, mm.query_match("滚出去", u))
ck("n-gram 近似匹配", mm.query_match("报复", u)[0] > 0, mm.query_match("报复", u))
ck("无匹配明确返回 0", mm.query_match("量子物理", u) == (0.0, "无匹配"), mm.query_match("量子物理", u))
ck("未指定主题不筛片段", mm.query_match("", u)[0] == 1.0, "")

# ── 底层采样（真实 ffmpeg） ──────────────────────────────────────
p = pm.scan(FIX, "__mm_unit__")
p["acts"] = []
a = analyzemod.analyze(p)
ep = p["episodes"][0]
samples = mm._visual_samples(ep["path"])
audio = mm._audio_envelope(ep["path"], True)
ck("画面采样有帧", len(samples) >= 2, len(samples))
ck("画面采样 5 列", all(len(x) == 5 for x in samples), samples[:1])
ck("画面特征在合理范围", all(0 <= x[1] <= 1 and 0 <= x[3] <= 1 for x in samples), samples[:1])
ck("声音包络有窗", len(audio) >= 8, len(audio))
ck("声音包络 3 列且 dB<=0", all(len(x) == 3 and x[1] <= 0.01 for x in audio), audio[:2])
one = mm.scan_episode(ep)
ck("单集扫描带文件/时长/镜头", one["file"] == ep["file"] and one["dur"] > 0
   and isinstance(one["shots"], list), one)
ck("无声素材不炸", mm._audio_envelope(ep["path"], False) == [], "")

# ── 增量缓存 ─────────────────────────────────────────────────────
with tempfile.TemporaryDirectory(prefix="dc_mm_") as td:
    root = Path(td)
    r1 = mm.scan_project(p, root)
    r2 = mm.scan_project(p, root)
    ck("首次全量扫描", r1["scanned"] == len(p["episodes"]), r1)
    ck("二次命中增量缓存", r2["scanned"] == 0, r2)
    raw = json.loads(Path(r1["path"]).read_text(encoding="utf-8"))
    ck("缓存版本号正确", raw["version"] == mm.CACHE_VERSION, raw.get("version"))
    ck("缓存按集分开", len(raw["eps"]) == len(p["episodes"]), len(raw["eps"]))

    f = mm.fuse(p, a["beats"], [], raw, a["highlights"])
    units = f["units"]
    ck("融合单元非空", len(units) >= len(p["episodes"]), len(units))
    ck("融合走缓存", f["source"] == "cache", f["source"])
    keys = ("ep", "g0", "g1", "start", "end", "dur", "text", "topics", "topic",
            "emotion", "scene", "rhythm", "key", "confidence", "evidence", "modalities")
    ck("融合字段齐全", all(all(k in u for k in keys) for u in units), "")
    ck("置信度在 0~1", all(0 <= u["confidence"] <= 1 for u in units), "")
    ck("节奏在 0~1", all(0 <= u["rhythm"]["pace"] <= 1 for u in units), "")
    ck("四路信号都进证据", all(len(u["evidence"]) >= 2 for u in units),
       units[0]["evidence"] if units else "")
    ck("单元按集与时间有序", all(
        (units[i]["ep"], units[i]["g0"]) <= (units[i + 1]["ep"], units[i + 1]["g0"])
        for i in range(len(units) - 1)), "")
    ck("单元不跨集", all(u["ep"] == next(e["idx"] for e in p["episodes"]
                                   if e["idx"] == u["ep"]) for u in units), "")
    st = mm.summarize(units)
    ck("概况含主题场景情绪", st["topics"] and st["scenes"] and st["emotions"], st)

    plain = mm.fuse(p, a["beats"], [], None, a["highlights"])
    ck("无缓存降级为纯文本", plain["source"] == "text-only", plain["source"])
    ck("降级仍有可用单元", len(plain["units"]) == len(units), "")
    ck("空单元概况安全", mm.summarize([])["units"] == 0, "")

bad = [c for c in CHECKS if not c["ok"]]
print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
if bad:
    print("失败：", [c["name"] for c in bad])
    sys.exit(1)

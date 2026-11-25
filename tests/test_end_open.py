"""顺剪结尾「留悬念」回归测试。

覆盖：
  A 未答问题优先于集末收束句
  B 下一拍才揭身份：停在揭晓之前
  C 集末是收束句时不强行卡集末
  D 显式指定结尾仍然精确生效
  E 没有悬念时停在自然剧情点，不伪造“留悬念”
  F 同一输入结果可复现
  G 悬念结尾不改变正片结构，也不侵入引导尾帧

用法：runtime\\python.exe tests/test_end_open.py
输出：tests/results/end_open.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import plan as P  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name +
          (f"   [{detail}]" if detail and not cond else ""))


def project(n_eps: int = 1, dur: float = 120.0) -> dict:
    eps = []
    for i in range(1, n_eps + 1):
        eps.append({"idx": i, "file": f"{i:02d}.mp4", "path": f"x{i}.mp4",
                    "dur": dur, "offset": (i - 1) * dur,
                    "width": 720, "height": 1280, "sub": "", "cues": []})
    return {"name": "结尾测试", "episodes": eps, "total": n_eps * dur}


def beat(ep: int, start: float, text: str, act: float = 0.0,
         score: float = 2.0, dur: float = 5.5) -> dict:
    ep_off = 0.0 if ep == 1 else 60.0
    return {"ep": ep, "g0": ep_off + start, "g1": ep_off + start + dur,
            "start": start, "end": start + dur, "dur": dur,
            "text": text, "cues": 1, "score": score, "hits": [],
            "act": act, "aud": 0.0, "vis": 0.0}


def tidy(raw: list[dict]) -> list[dict]:
    raw.sort(key=lambda b: b["g0"])
    for i, b in enumerate(raw):
        b["id"] = i
    return raw


def neutral(n_eps: int = 1) -> list[dict]:
    out = []
    for ep in range(1, n_eps + 1):
        for i in range(10):
            out.append(beat(ep, i * 6.0, "我们继续处理这件事。"))
    return tidy(out)


def test_question_over_closure() -> None:
    print("\n[A] 未答问题优先于集末收束句")
    p = project(1, 120.0)
    b = neutral()
    b[8] = beat(1, 48.0, "难道你一直都在骗我？", score=3.5)
    b[9] = beat(1, 54.0, "太好了，我们终于在一起了。", score=5.0)
    b = tidy(b)
    pl = P.build(p, b, target=54.0)
    end = pl["end"]
    ck("停在问题句而不是后面的收束句", end["g1"] < 54.0, end["g1"])
    ck("结尾被标为未答问题", "未答问题" in (end.get("signals") or []), end.get("signals"))
    ck("悬念分大于 0", end.get("open_loop", 0) > 2.0, end.get("open_loop"))
    ck("不标记为收束结尾", end.get("closed") is False, end.get("closed"))
    ck("结果理由写明留悬念", "悬念" in (pl.get("end_reason") or ""), pl.get("end_reason"))
    ck("结尾不是剧终", end["g1"] < p["total"] - 3.0, end["g1"])


def test_reveal_before_reveal() -> None:
    print("\n[B] 下一拍才揭身份：停在揭晓之前")
    p = project(1, 120.0)
    b = neutral()
    b[8] = beat(1, 48.0, "你根本不知道我是谁。", score=3.0)
    b[9] = beat(1, 54.0, "原来你就是集团继承人。", score=4.2)
    b = tidy(b)
    pl = P.build(p, b, target=54.0)
    end = pl["end"]
    ck("停在揭晓前", end["g1"] < 54.0, end["g1"])
    ck("识别出身份将揭", "身份将揭" in (end.get("signals") or []), end.get("signals"))
    ck("理由写明留悬念", "悬念" in (pl.get("end_reason") or ""), pl.get("end_reason"))


def test_ep_end_closure_not_forced() -> None:
    print("\n[C] 集末是收束句时不强行卡集末")
    p = project(2, 60.0)
    b = neutral(2)
    # 第 1 集最后一句是完整收束；第 2 集第 6 秒才有问题
    for x in b:
        if x["ep"] == 1 and abs(x["g0"] - 54.0) < 0.01:
            x["text"] = "太好了，我们终于在一起了。"
            x["score"] = 6.0
        if x["ep"] == 2 and abs(x["g0"] - 66.0) < 0.01:
            x["text"] = "难道你忘了今晚是什么日子？"
            x["score"] = 3.0
    b = tidy(b)
    pl = P.build(p, b, target=60.0)
    end = pl["end"]
    ck("没有停在第 1 集收束句上", not (end["ep"] == 1 and end["g1"] >= 59.0),
       (end["ep"], end["g1"]))
    ck("停在收束之前或第 2 集悬念点", end["g1"] < 59.5 or end["ep"] == 2,
       (end["ep"], end["g1"]))
    ck("有明确的留扣信号",
       bool(set(end.get("signals") or []) & {"未答问题", "下一句才收束", "动作将起"}),
       end.get("signals"))
    ck("不标记为收束结尾", end.get("closed") is False, end.get("closed"))


def test_explicit_point() -> None:
    print("\n[D] 显式指定结尾仍然精确生效")
    p = project(1, 120.0)
    b = neutral()
    b[8] = beat(1, 48.0, "难道你一直都在骗我？", score=3.5)
    b = tidy(b)
    pl = P.build(p, b, target=54.0, end_mode="point",
                 end_hook={"g0": 50.0, "g1": 55.5})
    ck("指定点精确落位", abs(pl["end"]["g1"] - 55.5) < 0.01, pl["end"]["g1"])
    ck("标记为指定结尾", pl.get("end_style") == "forced" and pl.get("end_forced") is True,
       (pl.get("end_style"), pl.get("end_forced")))
    ck("理由写明按指定结尾", "指定" in (pl.get("end_reason") or ""), pl.get("end_reason"))


def test_no_fake_hook() -> None:
    print("\n[E] 没有悬念时停在自然剧情点，不伪造留悬念")
    p = project(1, 120.0)
    b = [beat(1, i * 6.0, "没事了，都过去了。", score=3.0) for i in range(20)]
    b = tidy(b)
    pl = P.build(p, b, target=54.0)
    end = pl["end"]
    ck("收束句被标记", end.get("closed") is True, end.get("closed"))
    ck("不伪造悬念分", end.get("open_loop", 0) <= 0, end.get("open_loop"))
    ck("理由不声称留悬念", "悬念" not in (pl.get("end_reason") or ""), pl.get("end_reason"))
    ck("理由说明自然剧情点", "自然剧情点" in (pl.get("end_reason") or ""), pl.get("end_reason"))


def test_reproducible() -> None:
    print("\n[F] 同一输入结果可复现")
    p = project(1, 120.0)
    b = neutral()
    b[8] = beat(1, 48.0, "你到底是谁？", score=3.0)
    b = tidy(b)
    r1 = P.build(p, b, target=54.0)
    r2 = P.build(p, b, target=54.0)
    ck("两次结尾一致", r1["end"] == r2["end"], (r1["end"]["g1"], r2["end"]["g1"]))
    ck("两次理由一致", r1["end_reason"] == r2["end_reason"],
       (r1["end_reason"], r2["end_reason"]))


def test_structure_and_tail_frame() -> None:
    print("\n[G] 悬念结尾不改正片结构，也不侵入引导尾帧")
    p = project(1, 120.0)
    b = neutral()
    b[8] = beat(1, 48.0, "难道你一直都在骗我？", score=3.5)
    b = tidy(b)
    pl = P.build(p, b, target=54.0)
    segs = pl["segments"]
    ck("最后一段仍是正片", bool(segs) and segs[-1].get("kind") == "end_hook", segs[-1].get("kind") if segs else "")
    ck("结尾时间与最后一段一致", abs(pl["end"]["g1"] - segs[-1]["g1"]) < 0.01,
       (pl["end"]["g1"], segs[-1]["g1"]))
    ck("方案里没有引导尾帧段", not any(s.get("kind") in ("end_frame", "card") for s in segs),
       [s.get("kind") for s in segs])
    ck("没有为收束新增额外分段", len(segs) == 1, len(segs))
    ck("结尾理由只描述正片卡点", "尾帧" not in (pl.get("end_reason") or ""), pl.get("end_reason"))


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    for fn in (test_question_over_closure, test_reveal_before_reveal,
               test_ep_end_closure_not_forced, test_explicit_point,
               test_no_fake_hook, test_reproducible, test_structure_and_tail_frame):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ck(f"{fn.__name__} 未抛异常", False, str(e))
    bad = [c for c in CHECKS if not c["ok"]]
    out = {"when": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
           "total": len(CHECKS), "failed": len(bad), "checks": CHECKS}
    (RESULTS / "end_open.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
    if bad:
        for c in bad:
            print("  -", c["name"], c["detail"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

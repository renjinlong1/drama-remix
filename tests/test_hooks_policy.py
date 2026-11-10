"""钩子策略测试：亲密/巴掌识别、前 15 集加密、结尾候选数量。

不依赖视频，直接用合成 beats 驱动 analyze 的候选层，跑得快、断言稳定。

用法：runtime\\python.exe tests\\test_hooks_policy.py
输出：tests/results/hooks_policy.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import analyze as A  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def test_intimacy_words() -> None:
    print("\n[1] 亲密 / 巴掌词库")
    cases = {
        "他吻了她": (0.6, "kiss"),
        "你凭什么亲我": (0.6, "kiss"),
        "她扑进他怀里": (0.6, "intimate"),
        "抱住我，别松手": (0.6, "intimate"),
        "他抬手就是一巴掌": (0.0, "slap"),
        "She kissed him softly": (0.6, "kiss"),
    }
    for text, (score, sub) in cases.items():
        s = A._intimacy_score(text)
        ck(f"「{text}」亲密度 {score}", abs(s - score) < 0.01, s)
        ck(f"「{text}」子标签 {sub}", A._subtype(text, s) == sub, A._subtype(text, s))

    # 日常用法不能误伤
    for text in ("抱歉我来晚了", "亲妈也不会这样", "我去睡觉了", "他抱着胳膊站着"):
        ck(f"不误伤「{text}」", A._intimacy_score(text) < 0.3, A._intimacy_score(text))


def make_beats(eps: int, per_ep: int = 6, front_weak: bool = True) -> list[dict]:
    """合成一部 eps 集的剧：前段台词轻冲突，后段强冲突，用来验证前段保底。"""
    weak = ["我们走吧", "今天天气不错", "你先坐下", "这事以后再说", "我明白了", "好"]
    strong = ["你算什么东西", "滚出去", "我一巴掌扇死你", "跪下道歉", "离婚", "你这个废物"]
    intimate = ["他吻了她", "她扑进他怀里", "你凭什么亲我"]
    beats: list[dict] = []
    bid = 0
    for ep in range(1, eps + 1):
        for k in range(per_ep):
            if 3 <= k <= 4 and ep <= max(3, eps // 2):
                text = intimate[(ep + k) % len(intimate)]
            elif front_weak and ep <= 15:
                text = weak[k % len(weak)]
            else:
                text = strong[k % len(strong)]
            sc, hits = A._hits(text)
            susp, _ = A.suspense_of(text)
            g0 = (ep - 1) * 90.0 + k * 11.0
            beats.append({"id": bid, "ep": ep, "g0": g0, "g1": g0 + 8.0, "dur": 8.0,
                          "text": text, "score": sc, "act": 0.0, "susp": susp,
                          "hits": hits, "g1_rel": 0})
            bid += 1
    return beats


def test_front_density() -> None:
    print("\n[2] 前 15 集加密")
    beats = make_beats(24)
    hl = A.find_highlights(beats, top_k=24, min_keep=15)
    goal = min(24, max(15, len({b["ep"] for b in beats})))
    front = [c for c in hl if c.get("front")]
    lo = int(round(goal * A.FRONT_MIN_SHARE))
    hi = int(round(goal * A.FRONT_SHARE))
    ck("候选数量不超过 top_k", len(hl) <= 24, len(hl))
    ck(f"前 15 集至少 {lo} 条", len(front) >= min(lo, len(hl)), f"{len(front)}/{len(hl)}")
    ck(f"前 15 集不超过 {hi} 条（不挤干净后段）", len(front) <= hi, f"{len(front)}/{len(hl)}")
    ck("后段仍有候选", len(hl) - len(front) >= 3, len(hl) - len(front))

    short = A.find_highlights(make_beats(12), top_k=24, min_keep=15)
    ck("≤15 集不启用前段加密", all(not c.get("front") for c in short), "不该有 front 标记")

    # 前段台词明显更弱时，仍然要凑够保底（否则"前 15 集密集"落不了地）
    weak = make_beats(24, front_weak=True)
    hl_w = A.find_highlights(weak, top_k=24, min_keep=15)
    ck("前段台词偏弱时仍保住密度",
       sum(1 for c in hl_w if c.get("front")) >= min(lo, len(hl_w)),
       sum(1 for c in hl_w if c.get("front")))


def test_types() -> None:
    print("\n[3] 亲密类型与标签")
    beats = make_beats(24)
    hl = A.find_highlights(beats, top_k=24, min_keep=15)
    types = {c["primary_type"] for c in hl}
    ck("候选里出现亲密接触类型", "intimacy" in types, sorted(types))
    subs = {c.get("subtype") for c in hl}
    ck("有 kiss 子标签", "kiss" in subs, sorted(s for s in subs if s))
    ck("有 slap 子标签", "slap" in subs, sorted(s for s in subs if s))
    ck("每个候选都有 subtype 字段", all("subtype" in c for c in hl), "")
    ck("未知类型不会崩", all(c["primary_type"] in A.TYPE_LABELS for c in hl), "")


def test_endings() -> None:
    print("\n[4] 结尾候选数量")
    beats = make_beats(24)
    total = beats[-1]["g1"]
    ends = A.find_endings(beats, total, top_k=8)
    ck("结尾候选最多 8 个", len(ends) <= 8, len(ends))
    ck("结尾候选都在剧终区", all(e["g1"] >= total - min(150.0, max(45.0, total * 0.10)) - 0.1
                          for e in ends), "越界")


def test_determinism() -> None:
    print("\n[5] 确定性")
    beats = make_beats(24)
    a = [c["g0"] for c in A.find_highlights(beats, top_k=24, min_keep=15)]
    b = [c["g0"] for c in A.find_highlights(beats, top_k=24, min_keep=15)]
    ck("同样输入两次结果一致", a == b, f"{a[:3]} / {b[:3]}")


def main() -> int:
    print("钩子策略测试")
    test_intimacy_words()
    test_front_density()
    test_types()
    test_endings()
    test_determinism()
    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "hooks_policy.json").write_text(json.dumps(
        {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

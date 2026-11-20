"""词库匹配等价性测试：trie 版必须与旧的「逐词 in / 逐词 search」完全一致。

覆盖项目里全部节拍文本 + 包含关系词 + 中英混排 + 空串。
用法：runtime\\python.exe tests\\test_analyze_equiv.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import analyze as A  # noqa: E402

def _any_project() -> Path | None:
    """随便找一部已导入的剧当语料；固定的文件名可能在别的机器上不存在。"""
    cands = sorted((ROOT / "projects").glob("*.json"),
                   key=lambda p: p.stat().st_size, reverse=True)
    return cands[0] if cands else None


PROJ = _any_project() or (ROOT / "projects" / "__missing__.json")
CHECKS: list[dict] = []


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


# ── 参考实现：优化前的写法 ───────────────────────────────
_REF_EN = [(cat, w, kw, re.compile(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])"))
           for cat, (w, words) in A.EN_LEX.items() for kw in words]
_REF_SUS = [(kw, wt, re.compile(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])"))
            for kw, wt in A.EN_SUSPENSE]


def hits_ref(text: str):
    score = 0.0
    hits: list[str] = []
    for cat, (w, words) in A.LEX.items():
        for kw in words:
            if kw in text:
                score += w
                if kw not in hits:
                    hits.append(kw)
    lower = text.lower()
    for cat, w, kw, pat in _REF_EN:
        if pat.search(lower):
            score += w
            if kw not in hits:
                hits.append(kw)
    return score, hits


def suspense_ref(text: str):
    if not text:
        return 0.0, []
    low = text.lower()
    hits = [(w, wt) for w, wt in A.SUSPENSE_WORDS if w in text]
    hits += [(kw, wt) for kw, wt, pat in _REF_SUS if pat.search(low)]
    q = len(re.findall(r"[？?]", text))
    return round(sum(wt for _, wt in hits) + q * 1.1, 2), sorted({w for w, _ in hits})


def intimacy_ref(text: str) -> float:
    """优化前的实现：每个中文词各做一次 find + 区间去重。"""
    if not text:
        return 0.0
    low = text.lower()
    spans: list[tuple[int, int]] = []

    def mark(word: str) -> int:
        n = 0
        start = 0
        while True:
            i = low.find(word, start)
            if i < 0:
                break
            end = i + len(word)
            if not any(i < e and s < end for s, e in spans):
                spans.append((i, end))
                n += 1
            start = end
        return n

    strong = 0
    for w in sorted((w for w in A.INTIMACY_STRONG if not w.isascii()), key=len, reverse=True):
        strong += mark(w)
    weak = 0
    for w in sorted((w for w in A.INTIMACY_WEAK if not w.isascii()), key=len, reverse=True):
        weak += mark(w)
    en = len(set(m.group(0).lower() for m in A._INTIMACY_EN.finditer(low)))
    strong += en
    return round(min(1.0, min(2, strong) * 0.6 + min(3, weak) * 0.18), 3)


SAMPLES = [
    "", "好", "你算什么东西", "我一巴掌扇死你", "跪下道歉",
    "He said: contract marriage, not marriage.",
    "banished! he was banished", "rejected and reject again",
    "cheated, cheat, cheating", "inheritance and inheritance again",
    "I know your secret", "That's impossible", "who are you? who is he?",
    "嫁给我好吗？", "我回来了，我回来了", "别走！不要走？",
    "Don't leave me, don't go", "kneel down before me", "hate you so much",
    "get out! get lost!", "你是我的女儿？", "真相到底是什么？",
    "mixed 中文 and English 吻了我",
]


def main() -> int:
    print("词库匹配等价性")
    samples = list(SAMPLES)
    if PROJ.exists():
        proj = json.loads(PROJ.read_text(encoding="utf-8"))
        beats = A.segment_beats(proj)
        samples += [b["text"] for b in beats]
        ck("项目节拍已载入", len(beats) > 100, len(beats))
    ck("样本数", len(samples) > 20, len(samples))

    bad_hits = []
    bad_sus = []
    bad_int = []
    for t in samples:
        old = hits_ref(t)
        new = A._hits(t)
        if abs(old[0] - new[0]) > 1e-9 or old[1] != new[1]:
            bad_hits.append((t[:40], old, new))
        o2 = suspense_ref(t)
        n2 = A.suspense_of(t)
        if abs(o2[0] - n2[0]) > 1e-9 or o2[1] != n2[1]:
            bad_sus.append((t[:40], o2, n2))
        o3 = intimacy_ref(t)
        n3 = A._intimacy_score(t)
        if abs(o3 - n3) > 1e-9:
            bad_int.append((t[:40], o3, n3))

    ck("_hits 全部一致", not bad_hits, bad_hits[:3])
    ck("suspense_of 全部一致", not bad_sus, bad_sus[:3])
    ck("_intimacy_score 全部一致", not bad_int, bad_int[:3])

    # 包含关系词必须同时命中
    s, h = A._hits("contract marriage")
    ck("包含词同时命中 contract marriage", "contract marriage" in h and "marriage" in h, h)
    s2, h2 = A._hits("inheritance")
    ck("重复词仍加两次权重", abs(s2 - (3.6 + 1.8)) < 1e-9, s2)
    s3, h3 = A._hits("我回来了")
    ck("中文重复词加两次", abs(s3 - 6.0) < 1e-9, s3)

    bad = [c for c in CHECKS if not c["ok"]]
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

"""「换一批」候选池测试：排除已展示、抽干后的提示与重置、重开项目还原。

核心要求：
- 换一批必须从**没展示过**的画面里抽，和之前几批没有重叠；
- 池子抽干时不能返回空列表、也不能悄悄退回旧候选，要给出明确提示；
- 重开项目（analyze）看到的是**最后展示的那一批**，不是又跳回第一屏。

用法：runtime\\python.exe tests\\test_hook_pool.py
输出：tests/results/hook_pool.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

import app as appmod  # noqa: E402
from engine import analyze as A  # noqa: E402
from engine import project as projmod  # noqa: E402
from test_hook_recall import build_corpus  # noqa: E402

RESULTS = ROOT / "tests" / "results"
CHECKS: list[dict] = []
NAME = "__hookpool__"


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def keys(hs) -> list[str]:
    return [A.hook_key(c) for c in hs]


def cleanup() -> None:
    for p in (projmod.PROJ / f"{NAME}.json", projmod.PROJ / f"{NAME}.json.bak"):
        try:
            p.unlink()
        except OSError:
            pass


def main() -> int:
    print("换一批候选池")
    cleanup()
    proj, _gt = build_corpus()
    proj["name"] = NAME
    a = A.analyze(proj)

    appmod.STATE["project"] = proj
    appmod.STATE["analysis"] = a
    appmod.STATE["plan"] = None
    appmod._prewarm_thumbs = lambda: None

    first = keys(a["highlights"])
    ck("初始候选非空", len(first) >= 15, len(first))
    # 初始那批还没进过 hook_shown：第一次换一批必须把它算作已展示
    ck("初始项目没有展示记录", not proj.get("hook_shown"), proj.get("hook_shown"))

    seen: set[str] = set(first)
    batches = [first]
    exhausted = None
    for i in range(12):
        r = appmod.hooks_reseed({})
        if r.get("exhausted"):
            exhausted = r
            break
        k = keys(r["highlights"])
        ck(f"第 {i + 1} 批不与之前的批次重叠", not (set(k) & seen),
           sorted(set(k) & seen)[:3])
        ck(f"第 {i + 1} 批有足够候选", len(k) >= 6, len(k))
        seen |= set(k)
        batches.append(k)

    ck("连续换批最终会抽干", exhausted is not None, "12 次内没抽干")
    if exhausted:
        ck("抽干时给出提示文案", bool(exhausted.get("msg")), exhausted.get("msg"))
        ck("抽干时保留当前这一批", keys(exhausted.get("highlights") or []) == batches[-1],
           "返回的不是当前批")
        ck("抽干时允许从头再看", exhausted.get("can_reset") is True, exhausted)
        ck("抽干后没有可换的剩余", exhausted.get("left", 0) <= 2, exhausted.get("left"))
        # 重开项目：应该是最后展示的那一批
        again = keys(A.analyze(proj)["highlights"])
        ck("重开项目回到最后一批", again == batches[-1], f"{again[:3]} vs {batches[-1][:3]}")

        r = appmod.hooks_reseed({"reset": True})
        ck("重置后重新从第一批开始", keys(r["highlights"]) == first,
           f"{keys(r['highlights'])[:3]} vs {first[:3]}")
        ck("重置后展示记录清零重算", len(proj.get("hook_shown") or []) == len(first),
           len(proj.get("hook_shown") or []))

    # hook_key 必须忽略 0.1 秒以内的抖动（前端微调存的是原始起点）
    ck("候选身份按 0.1 秒取整",
       A.hook_key({"ep": 3, "g0": 10.04}) == A.hook_key({"ep": 3, "g0": 10.01}) == "3@10.0",
       A.hook_key({"ep": 3, "g0": 10.04}))
    ck("不同起点是不同候选",
       A.hook_key({"ep": 3, "g0": 10.06}) == "3@10.1", A.hook_key({"ep": 3, "g0": 10.06}))

    cleanup()
    appmod.STATE["project"] = None
    appmod.STATE["analysis"] = None

    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "hook_pool.json").write_text(json.dumps(
        {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
         "batches": len(batches), "shown": len(seen),
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项；抽了 {len(batches)} 批 / "
          f"{len(seen)} 个不重复候选")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

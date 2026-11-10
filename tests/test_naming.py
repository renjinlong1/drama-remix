"""导出文件名模板测试：规则单测 + 真实 HTTP 接口冒烟。

覆盖：模板解析、序号起点、前导零位宽、非法字符、扩展名、保留名、
同名顺延、批内唯一、预览接口。

用法：runtime\\python.exe tests\\test_naming.py
输出：tests/results/naming_results.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import naming  # noqa: E402

RESULTS = ROOT / "tests" / "results"
PY = ROOT / "runtime" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

CHECKS: list[dict] = []


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def expect_error(name: str, tpl: str, keyword: str = "") -> None:
    try:
        naming.parse(tpl)
    except naming.NameTemplateError as e:
        ck(name, keyword in str(e) if keyword else True, str(e))
    except Exception as e:  # noqa: BLE001
        ck(name, False, f"抛了别的异常：{e}")
    else:
        ck(name, False, "没有报错")


def test_parse() -> None:
    print("\n[1] 模板解析")
    tpl = "1110-任金龙-狼人-5451-原切（中）-1"
    p, s, w = naming.parse(tpl)
    ck("用户示例：前缀原样保留", p == "1110-任金龙-狼人-5451-原切（中）-", p)
    ck("用户示例：起始序号 = 1", s == 1, s)
    ck("用户示例：不补零", w == 0, w)
    ck("用户示例：第 1 条", naming.format_name(p, s, w) == tpl, naming.format_name(p, s, w))
    ck("用户示例：第 2 条自动 +1",
       naming.format_name(p, s + 1, w) == "1110-任金龙-狼人-5451-原切（中）-2",
       naming.format_name(p, s + 1, w))
    ck("用户示例：第 10 条自然进位",
       naming.format_name(p, s + 9, w).endswith("-10"),
       naming.format_name(p, s + 9, w))

    p2, s2, w2 = naming.parse("狼人-007")
    ck("前导零：前缀", p2 == "狼人-", p2)
    ck("前导零：起点 7", s2 == 7, s2)
    ck("前导零：位宽 3", w2 == 3, w2)
    ck("前导零：生成 007", naming.format_name(p2, s2, w2) == "狼人-007", naming.format_name(p2, s2, w2))
    ck("前导零：生成 010", naming.format_name(p2, s2 + 3, w2) == "狼人-010", naming.format_name(p2, s2 + 3, w2))

    p3, s3, _ = naming.parse("某剧-42.mp4")
    ck("带扩展名：自动剥掉", p3 == "某剧-" and s3 == 42, f"{p3} / {s3}")

    p4, _, _ = naming.parse('a/b:c*d?e"f<g>h|i-3')
    ck("非法字符剔除", p4 == "abcdefghi-", p4)

    p5, _, _ = naming.parse("剧名. - 5")
    ck("尾部空格/点清理", p5 == "剧名. -", p5)

    p6, s6, _ = naming.parse("  CON-1  ")
    ck("首尾空白清理且不影响命名", p6 == "CON-" and s6 == 1, f"{p6} / {s6}")

    p7, s7, _ = naming.parse("\u3000剧名　副标题-3\u3000")
    ck("首尾全角空格去掉、中间原样保留",
       p7 == "剧名　副标题-" and s7 == 3, f"{p7!r} / {s7}")

    expect_error("空模板报错", "", "不能为空")
    expect_error("只有数字报错", "123", "前面")
    expect_error("末尾无数字报错", "只有剧名", "末尾必须是数字")
    expect_error("只有扩展名报错", ".mp4", "不能只有扩展名")
    expect_error("序号过大报错", "剧名-9999999", "太大")
    expect_error("前缀过长报错", "长" * 120 + "-1", "太长")


def test_namer() -> None:
    print("\n[2] 批次发号与唯一性")
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        n = naming.Namer(folder, "1110-任金龙-狼人-5451-原切（中）-1")
        got = [n.next().name for _ in range(3)]
        ck("从起始值开始依次递增",
           got == ["1110-任金龙-狼人-5451-原切（中）-1.mp4",
                   "1110-任金龙-狼人-5451-原切（中）-2.mp4",
                   "1110-任金龙-狼人-5451-原切（中）-3.mp4"], got)
        ck("批内互不重复", len(set(got)) == 3, got)

        # 模拟已落盘的旧成片：第 1、2 条都在，新一批应从 3 开始
        for name in got[:2]:
            (folder / name).write_bytes(b"old")
        n2 = naming.Namer(folder, "1110-任金龙-狼人-5451-原切（中）-1")
        ck("已存在同名自动顺延", n2.next().name.endswith("-3.mp4"), n2.next_num)
        ck("顺延后不会重复发号", n2.next().name.endswith("-4.mp4"), n2.next_num)

        # 前导零位宽在真实目录里也要保持
        n3 = naming.Namer(folder, "测试-008")
        a, b = n3.next().name, n3.next().name
        ck("前导零文件按位宽命名", (a, b) == ("测试-008.mp4", "测试-009.mp4"), f"{a} / {b}")
        ck("跨进位数位正确", naming.Namer(folder, "测试-009").next().name == "测试-009.mp4")

        n4 = naming.Namer(folder, "批量-1")
        names = [n4.next().name for _ in range(200)]
        ck("连续 200 条全唯一", len(set(names)) == 200, len(set(names)))
        ck("连续 200 条末号正确", names[-1].endswith("-200.mp4"), names[-1])

        n5 = naming.Namer(folder, "大小写-1")
        (folder / "大小写-1.mp4").write_bytes(b"x")
        ck("大小写不同也视为同一文件", n5.next().name.endswith("-2.mp4"), n5.next_num)


def test_preview() -> None:
    print("\n[3] 预览输出")
    r = naming.preview("剧名-9", 3)
    ck("预览 3 条", r["names"] == ["剧名-9.mp4", "剧名-10.mp4", "剧名-11.mp4"], r["names"])
    ck("预览返回起点", r["start"] == 9, r["start"])
    r2 = naming.preview("剧名-1", 999)
    ck("预览数量封顶 20", len(r2["names"]) == 20, len(r2["names"]))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def test_http() -> None:
    print("\n[4] HTTP 接口")
    port = free_port()
    env = dict(os.environ, DC_PORT=str(port), DC_NO_BROWSER="1", DC_LOG="error")
    proc = subprocess.Popen([str(PY), str(ROOT / "app.py")], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        ok = False
        for _ in range(80):
            try:
                with urllib.request.urlopen(base + "/api/status", timeout=1) as r:
                    ok = r.status == 200
                if ok:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        ck("服务可启动", ok, base)
        if not ok:
            return

        r = post(base + "/api/name/preview",
                 {"name_tpl": "1110-任金龙-狼人-5451-原切（中）-1", "count": 3})
        ck("接口返回成功", r.get("ok") is True, r)
        ck("接口预览与规则一致",
           r.get("names") == ["1110-任金龙-狼人-5451-原切（中）-1.mp4",
                              "1110-任金龙-狼人-5451-原切（中）-2.mp4",
                              "1110-任金龙-狼人-5451-原切（中）-3.mp4"], r.get("names"))
        ck("接口回传起点", r.get("start") == 1, r.get("start"))

        r2 = post(base + "/api/name/preview", {"name_tpl": "没有末尾数字"})
        ck("非法模板 ok=false", r2.get("ok") is False, r2)
        ck("非法模板给中文提示", "末尾必须是数字" in str(r2.get("msg")), r2.get("msg"))

        r3 = post(base + "/api/name/preview", {"name_tpl": ""})
        ck("空模板标记 empty", r3.get("empty") is True and r3.get("names") == [], r3)

        with urllib.request.urlopen(base + "/api/status", timeout=5) as r:
            st = json.loads(r.read().decode())
        ck("服务状态正常", st.get("ffmpeg") is True, list(st)[:4])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    print("导出文件名模板测试")
    test_parse()
    test_namer()
    test_preview()
    test_http()

    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
           "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    (RESULTS / "naming_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

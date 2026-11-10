"""导出命名端到端验证：真实扫描 → 真实渲染 → 检查落盘文件名。

验证三件事：
1. 模板末尾数字作为起点，批内逐条 +1；
2. 同一目录再导一批，自动从已有文件之后续号，不覆盖；
3. 清空模板后退回默认命名（剧名_开头NN_NN.mp4）。

用法：runtime\\python.exe tests\\e2e_naming.py
输出：tests/results/e2e_naming.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "runtime" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

FIXTURE = ROOT / "tests" / "fixtures" / "h264_720x1280_15s"
RESULTS = ROOT / "tests" / "results"
TPL = "1110-任金龙-狼人-5451-原切（中）-1"
EXPECT_1 = [f"1110-任金龙-狼人-5451-原切（中）-{i}.mp4" for i in (1, 2)]
EXPECT_2 = [f"1110-任金龙-狼人-5451-原切（中）-{i}.mp4" for i in (3, 4)]
PROJ = "e2e命名测试"

CHECKS: list[dict] = []


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def wait_job(base: str, timeout: float = 900) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = req(base + "/api/job")
        if st.get("state") in ("done", "error"):
            return st
        time.sleep(0.4)
    raise TimeoutError("任务超时")


def main() -> int:
    if not FIXTURE.exists():
        print(f"缺少测试素材：{FIXTURE}，先跑 tests/gen_fixtures.py")
        return 2
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    outdir = Path(tempfile.mkdtemp(prefix="dc_name_"))
    env = dict(os.environ, DC_PORT=str(port), DC_NO_BROWSER="1", DC_LOG="error")
    proc = subprocess.Popen([str(PY), str(ROOT / "app.py")], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    proj_file = ROOT / "projects" / (PROJ + ".json")
    try:
        for _ in range(100):
            try:
                req(base + "/api/status")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

        print("[1] 扫描样片")
        req(base + "/api/scan", {"folder": str(FIXTURE), "name": PROJ, "ocr": False})
        st = wait_job(base)
        ck("扫描完成", st.get("state") == "done", st.get("msg"))
        payload = st.get("result") or {}
        hooks = payload.get("highlights") or []
        ck("有可用开头候选", bool(hooks), len(hooks))
        if not hooks:
            return 1
        h = hooks[0]
        ck("候选带时间区间", h.get("g1", 0) > h.get("g0", 0), h)

        base_body = {
            "title": "测试剧", "target": 60,
            "outdir": str(outdir), "outdir_sub": False,
            "items": [{"hook": {"g0": h["g0"], "g1": h["g1"]}, "count": 2}],
            "total": 0,
        }

        print("[2] 第一批：模板从 -1 起")
        req(base + "/api/batch", dict(base_body, name_tpl=TPL))
        st = wait_job(base)
        ck("第一批完成", st.get("state") == "done", st.get("msg"))
        res1 = (st.get("result") or {}).get("results") or []
        errs1 = (st.get("result") or {}).get("errors") or []
        ck("第一批出 2 条", len(res1) == 2, f"{len(res1)} 条，errors={errs1}")
        names1 = [r.get("name") for r in res1]
        ck("第一批文件名符合模板", names1 == EXPECT_1, names1)
        ck("第一批文件真实落盘", all((outdir / n).exists() for n in names1),
           [n for n in names1 if not (outdir / n).exists()])
        ck("第一批大小非零", all((outdir / n).stat().st_size > 1000 for n in names1),
           [n for n in names1 if (outdir / n).exists() and (outdir / n).stat().st_size <= 1000])

        print("[3] 第二批：同目录自动续号")
        req(base + "/api/batch", dict(base_body, name_tpl=TPL))
        st = wait_job(base)
        res2 = (st.get("result") or {}).get("results") or []
        names2 = [r.get("name") for r in res2]
        ck("第二批出 2 条", len(res2) == 2, len(res2))
        ck("第二批从 -3 续号", names2 == EXPECT_2, names2)
        ck("两批文件名互不重复", not (set(names1) & set(names2)), set(names1) & set(names2))
        ck("旧文件未被覆盖", all((outdir / n).exists() for n in names1), "第一批文件丢失")

        print("[4] 第三批：清空模板回到默认命名")
        req(base + "/api/batch", dict(base_body, name_tpl=""))
        st = wait_job(base)
        res3 = (st.get("result") or {}).get("results") or []
        names3 = [r.get("name") for r in res3]
        ck("第三批出 2 条", len(res3) == 2, len(res3))
        ck("默认命名格式", all(n and n.startswith("测试剧_开头01_") for n in names3), names3)
        ck("默认命名与模板命名不冲突", not (set(names3) & (set(names1) | set(names2))), names3)

        print("[5] 单条出片：起始序号不是 1 也照写")
        req(base + "/api/render", {
            "title": "测试剧", "target": 60,
            "outdir": str(outdir), "outdir_sub": False,
            "name_tpl": "单条测试-10",
        })
        st = wait_job(base)
        one = st.get("result") or {}
        ck("单条完成", st.get("state") == "done", st.get("msg"))
        ck("单条文件名 = 起始序号", one.get("name") == "单条测试-10.mp4", one.get("name"))
        ck("单条文件落盘", (outdir / "单条测试-10.mp4").exists(), outdir)

        # 模板非法时必须在建目录/开任务前就 400
        print("[6] 非法模板拦截")
        try:
            req(base + "/api/batch", dict(base_body, name_tpl="末尾没有数字"))
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode() or "{}")
            ck("非法模板返回 400", e.code == 400, e.code)
            ck("提示说明末尾要数字", "末尾必须是数字" in str(body.get("detail")), body.get("detail"))
        else:
            ck("非法模板返回 400", False, "竟然通过了")
        st = req(base + "/api/job")
        ck("非法模板没有启动任务", st.get("state") != "running", st.get("state"))

        on_disk = sorted(p.name for p in outdir.glob("*.mp4"))
        ck("目录文件数与预期一致", len(on_disk) == 7, on_disk)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(outdir, ignore_errors=True)
        if proj_file.exists():
            proj_file.unlink()

    bad = [c for c in CHECKS if not c["ok"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "e2e_naming.json").write_text(json.dumps(
        {"checks": CHECKS, "total": len(CHECKS), "failed": len(bad),
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n共 {len(CHECKS)} 项，失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

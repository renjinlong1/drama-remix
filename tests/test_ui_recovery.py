"""保存位置 / 导出标题 / 最近项目三处交互失效的回归测试。

覆盖：
  A 半新半旧页面：app.js 不能因为缺 DOM 元素而中断绑定（静态契约 + 空节点兜底）
  B 任务失败收尾：watchJob 出错必须放开「扫描并分析」
  C 兜底脚本：index.html 独立接管三个关键按钮
  D 删除最近项目：存档 / .bak / 动作缓存 / recent 缓存 / STATE 全部清掉
  E 同名重新导入：墓碑被撤销，项目能重新落盘
  F 坏掉的 OCR 缓存不能让整次扫描失败
  G 系统目录选择框不再依赖便携包里没有的 tkinter

用法：runtime\\python.exe tests/test_ui_recovery.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as appmod  # noqa: E402
from engine import project as projmod  # noqa: E402
from engine import subs  # noqa: E402

CHECKS: list[dict] = []


def ck(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
PY = (ROOT / "app.py").read_text(encoding="utf-8")
PROJPY = (ROOT / "engine" / "project.py").read_text(encoding="utf-8")

# ── A 缺元素不能中断整段脚本 ──────────────────────────────────
ck("$ 对缺失元素返回游离节点", "document.createElement('div')" in JS and "_MISSING" in JS)
ck("$ 缺失元素只提示一次", "if (!_MISSING.has(s))" in JS)
ck("toast 用原生 getElementById（不能吃游离节点）", "let el = document.getElementById('toast');" in JS)
ck("脚本末尾标记 __APP_READY__", "window.__APP_READY__ = true;" in JS)
ck("构建标记已更新", "'2026-11-13.1'" in JS)

# ── B 任务失败收尾 ───────────────────────────────────────────
ck("endScan 统一收尾函数存在", "function endScan()" in JS)
ck("watchJob 结束分支调用 endScan", "clearInterval(S.jobTimer);\n      endScan();" in JS)
ck("doScan 异常分支调用 endScan", "} catch (e) {\n    endScan();" in JS)
ck("doScan 成功分支调用 endScan", "watchJob((res) => {\n      endScan();" in JS)
ck("新建项目时复位扫描按钮", "endScan();            // 重新导入前" in JS)
ck("删除项目时复位扫描按钮", "S.scanPending = false;\n        $('#btnScan').disabled = false;" in JS)

# ── C index.html 兜底 ────────────────────────────────────────
ck("兜底以 __APP_READY__ 为开关", "if (window.__APP_READY__) return;" in HTML)
ck("兜底接管选文件夹", "t.id === 'btnPickDir'" in HTML)
ck("兜底接管导出标题清空", "t.id === 'btnClearName'" in HTML)
ck("兜底接管扫描并分析", "t.id === 'btnScan'" in HTML)
ck("兜底自带目录浏览", "function miniPicker()" in HTML and "/api/folder/list?path=" in HTML)
ck("兜底不会和已有 onclick 重复触发", "typeof t.onclick === 'function'" in HTML)

# ── D/E 删除与重新导入 ───────────────────────────────────────
with tempfile.TemporaryDirectory(prefix="dc_ui_") as td:
    tmp = Path(td)
    old_proj, old_root = projmod.PROJ, projmod.ROOT
    projmod.PROJ, projmod.ROOT = tmp / "projects", tmp
    (tmp / "projects").mkdir()
    (tmp / "data" / "act").mkdir(parents=True)
    name = "回归测试剧"
    safe = projmod._safe_name(name)
    jf = tmp / "projects" / f"{safe}.json"
    bak = tmp / "projects" / f"{safe}.json.bak"
    act = tmp / "data" / "act" / f"{safe}.json"
    jf.write_text(json.dumps({"name": name, "folder": str(tmp), "episodes": []}), encoding="utf-8")
    bak.write_text("{}", encoding="utf-8")
    act.write_text("{}", encoding="utf-8")
    projmod._RECENT_CACHE.update(key=("stale",), items=[{"name": name}])
    appmod.STATE.update(project={"name": name}, analysis={"x": 1}, plan={"y": 2},
                        forgotten=set())

    r = appmod.delete_project({"path": str(jf)})
    ck("删除接口返回 ok / 名称 / 源目录", r.get("ok") and r.get("name") == safe and "folder" in r, r)
    ck("项目存档已删", not jf.exists())
    ck("同名 .json.bak 已删", not bak.exists())
    ck("画面动作缓存已删", not act.exists())
    ck("recent 元数据缓存已失效", projmod._RECENT_CACHE["key"] is None)
    ck("当前 STATE 已清空", appmod.STATE["project"] is None and appmod.STATE["plan"] is None)
    ck("已立墓碑防止后台扫描写回", safe in appmod.STATE["forgotten"])
    r2 = appmod.delete_project({"path": str(jf)})
    ck("重复删除不报错（幂等）", r2.get("ok") is True, r2)

    # 模拟 scan() 入口：同名重新导入先撤销墓碑
    with appmod.LOCK:
        appmod.STATE["forgotten"].discard(projmod._safe_name(name))
    ck("重新导入会撤销墓碑", safe not in appmod.STATE["forgotten"])
    ck("scan 入口含墓碑撤销代码", 'STATE["forgotten"].discard(projmod._safe_name(name))' in PY)
    ck("scan 收尾含墓碑检查", 'in STATE["forgotten"]' in PY)

    # purge_missing 同样做彻底清理
    jf.write_text(json.dumps({"name": name, "folder": "Z:\\不存在的盘", "episodes": []}), encoding="utf-8")
    bak.write_text("{}", encoding="utf-8")
    appmod.STATE["forgotten"].clear()
    gone = appmod.purge_missing()
    ck("清理失效返回被删名称", name in gone.get("removed", []), gone)
    ck("清理失效也删 .bak / 立墓碑", not bak.exists() and safe in appmod.STATE["forgotten"])

    projmod.PROJ, projmod.ROOT = old_proj, old_root
    appmod.STATE.update(project=None, analysis=None, plan=None, forgotten=set())
    projmod._RECENT_CACHE.update(key=None, items=[])

# ── F 坏 OCR 缓存容错 ────────────────────────────────────────
ck("scan 读取 OCR 缓存有异常保护", "except Exception:  # noqa: BLE001\n                    cues = []" in PROJPY)
old_pair, old_load, old_cached = (subs.pair_subtitles, subs.load_subtitle,
                                  projmod._cached_ocr_srt)
try:
    fixture = ROOT / "tests" / "fixtures" / "h264_360x640_6s"
    subs.pair_subtitles = lambda videos, files: {}
    projmod._cached_ocr_srt = lambda name, idx: fixture / "01.mp4"   # 存在即可
    def boom(_p):
        raise ValueError("坏掉的 srt")
    subs.load_subtitle = boom
    p = projmod.scan(fixture, "坏缓存测试")
    ck("坏 OCR 缓存不阻断扫描", all(not e["cues"] for e in p["episodes"]), p["sub_episodes"])
except Exception as e:  # noqa: BLE001
    ck("坏 OCR 缓存不阻断扫描", False, e)
finally:
    subs.pair_subtitles, subs.load_subtitle = old_pair, old_load
    projmod._cached_ocr_srt = old_cached

# ── G 系统目录选择框 ─────────────────────────────────────────
ck("系统目录选择框不再依赖 tkinter", "import tkinter" not in PY and "SHBrowseForFolderW" in PY)
old_os_name = os.name
try:
    appmod.os.name = "posix"
    r = appmod.pick_folder()
    ck("非 Windows 明确返回原因", r.get("folder") == "" and "Windows" in r.get("error", ""), r)
finally:
    appmod.os.name = old_os_name
if os.name == "nt":
    import ctypes  # noqa: E402
    dll = ctypes.windll.shell32
    orig = dll.SHBrowseForFolderW

    class FakeDialog:
        argtypes = None
        restype = None

        def __call__(self, *_a):
            return 0            # 用户点了取消

    try:
        dll.SHBrowseForFolderW = FakeDialog()
        r = appmod.pick_folder()
        ck("Windows 取消选择返回空路径且无异常", r == {"folder": ""}, r)
    finally:
        dll.SHBrowseForFolderW = orig

bad = [c for c in CHECKS if not c["ok"]]
print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
if bad:
    print("失败：", [c["name"] for c in bad])
    sys.exit(1)

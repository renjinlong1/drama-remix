"""硬件解码自动识别 / 启用 / 失败回退 / 状态反馈测试。

覆盖：
  A 能力探测：编译支持列表、真实解一帧验证、能力/后端/基准/状态快照
  B 模式切换：auto / smart / off / 指定后端 / 非法后端
  C 运行期失败：自动回退计数、连续失败整进程降级、recheck 恢复
  D ffmpegx 注入判定：视频/图片/lavfi/concat/-c copy/probe/allow_hw=False
  E ffmpegx.run 回退：硬件失败→软件成功、两条都失败不误计故障
  F vision 主扫描回退：硬件那次失败后整体软件重跑且数据完整
  G 结果一致性：硬件与软件解码的画面/声音/事件/多模态特征逐条一致
  H 接口：/api/hwaccel、mode、recheck、/api/status、/api/doctor、非法模式 400

用法：runtime\\python.exe tests\\test_hwaccel.py
输出：tests/results/hwaccel.json；有失败时退出码 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import ffmpegx, hwaccel, vision, multimodal  # noqa: E402
import app as APP  # noqa: E402

RESULTS = ROOT / "tests" / "results"
FIX = ROOT / "tests" / "fixtures" / "h264_720x1280_15s" / "01.mp4"
VIDEO = str(FIX)
CHECKS: list[dict] = []


def ck(name: str, cond, detail="") -> None:
    CHECKS.append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name +
          (f"   [{detail}]" if detail and not cond else ""))


def _hardware() -> bool:
    return hwaccel.status()["mode"] == "hardware"


def _fake_result(rc: int, stdout: bytes = b"", stderr: bytes = b""):
    r = type("Completed", (), {})()
    r.returncode, r.stdout, r.stderr = rc, stdout, stderr
    return r


# ── A 能力探测 ────────────────────────────────────────────────────────
def test_detect() -> None:
    compiled = hwaccel.compiled_methods()
    ck("读取编译支持列表", isinstance(compiled, list), compiled)
    s = hwaccel.set_mode("auto")
    expect = "hardware" if s["capable"] else "software"
    ck("auto 按设备能力决策", s["mode"] == expect, s["reason"])
    ck("后端属于编译支持列表", not s["backend"] or s["backend"] in s["compiled"])
    ck("自检有逐后端尝试记录", isinstance(s["attempts"], list) and len(s["attempts"]) > 0)
    if s["capable"]:
        ck("至少一个后端实解一帧通过", any(a["ok"] for a in s["attempts"]))
        ck("prefix 为 -hwaccel <后端>", hwaccel.prefix() == ["-hwaccel", s["backend"]],
           hwaccel.prefix())
        ck("自检样片真实存在", bool(s["sample"]) and Path(s["sample"]).exists())
        ck("收益基准已产出", s["benchmark"].get("valid") is True, s["benchmark"])
    ck("状态含反馈字段", all(k in s for k in
                            ("reason", "advisory", "fallbacks", "decodes", "degraded")))
    s2 = hwaccel.status()
    s2["reason"] = "被改了"
    ck("status 返回副本", hwaccel.status()["reason"] != "被改了")


# ── B 模式切换 ────────────────────────────────────────────────────────
def test_modes() -> None:
    s = hwaccel.set_mode("off")
    ck("off 关闭硬件", s["mode"] == "software" and hwaccel.prefix() == [])
    ck("off 原因清晰", "关闭" in s["reason"], s["reason"])

    compiled = hwaccel.compiled_methods()
    if compiled:
        s = hwaccel.set_mode(compiled[0])
        ck("指定后端强制启用", s["mode"] == "hardware" and s["backend"] == compiled[0],
           s["reason"])
        ck("强制模式跳过收益基准", not s["benchmark"], s["benchmark"])

    s = hwaccel.set_mode("__no_such_backend__")
    ck("非法后端回退软件", s["mode"] == "software")
    ck("非法后端原因清晰", "不在 ffmpeg 支持列表" in s["reason"], s["reason"])

    s = hwaccel.set_mode("smart")
    bm = s.get("benchmark") or {}
    if bm.get("valid"):
        expect = "hardware" if bm["faster"] else "software"
        ck("smart 按实测收益决策", s["mode"] == expect, bm)
    ck("smart 仍记录设备能力", s["capable"] is True)
    hwaccel.set_mode("auto")


# ── C 运行期失败与降级 ────────────────────────────────────────────────
def test_failure() -> None:
    hwaccel.set_mode("auto")
    if not _hardware():
        print("  SKIP 本机无可用硬件后端，跳过降级用例")
        return
    for i in range(hwaccel.FAIL_THRESHOLD):
        hwaccel.report_failure(f"模拟设备丢失 {i}")
    s = hwaccel.status()
    ck("连续失败整进程降级", s["degraded"] and s["mode"] == "software")
    ck("降级后 prefix 为空", hwaccel.prefix() == [])
    ck("降级原因写明回退", "降级" in s["reason"] and "软件" in s["reason"], s["reason"])
    ck("最近错误被记录", "模拟设备丢失" in s["last_error"])
    s = hwaccel.recheck()
    ck("recheck 清掉失败计数", s["failures"] == 0 and not s["degraded"])
    ck("recheck 恢复硬件", s["mode"] == "hardware")


# ── D 注入判定 ────────────────────────────────────────────────────────
def test_inject() -> None:
    hwaccel.set_mode("auto")
    if not _hardware():
        print("  SKIP 无硬件后端，注入用例按软件模式验证")
    cases = [
        ("视频输入", ["-y", "-ss", "1", "-i", VIDEO, "-frames:v", "1", "x.jpg"], True),
        ("concat 清单", ["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "j.mp4"], False),
        ("图片 + lavfi 音频", ["-y", "-i", "card.png", "-f", "lavfi", "-i",
                               "anullsrc=r=48000:cl=stereo", "c.mp4"], False),
        ("无输入 -version", ["-version"], False),
        ("-c copy 不解码", ["-y", "-i", VIDEO, "-c", "copy", "o.mp4"], False),
        ("probe 显式禁用", ["-i", VIDEO], False),
    ]
    for name, args, expect in cases:
        allow = name != "probe 显式禁用"
        injected, used = ffmpegx.hw_inject(args, allow_hw=allow)
        ck(f"注入判定 · {name}", used == (expect and _hardware()),
           f"used={used} args={injected[:6]}")
        if used:
            i = injected.index("-i")
            ck(f"注入位置正确 · {name}", injected[i - 2] == "-hwaccel")
    injected, used = ffmpegx.hw_inject(["-i", VIDEO], allow_hw=False)
    ck("allow_hw=False 不注入", not used)


# ── E ffmpegx.run 回退 ────────────────────────────────────────────────
def test_run_fallback() -> None:
    hwaccel.set_mode("auto")
    if not _hardware():
        print("  SKIP 无硬件后端，跳过 run 回退用例")
        return
    orig = ffmpegx._run_raw
    calls: list[list[str]] = []
    args = ["-y", "-i", VIDEO, "-frames:v", "1", "x.jpg"]

    def fake_hw_fail(a, timeout, check):
        calls.append(a)
        return _fake_result(1 if "-hwaccel" in a else 0,
                            b"OK" if "-hwaccel" not in a else b"",
                            b"Device creation failed" if "-hwaccel" in a else b"")

    ffmpegx._run_raw = fake_hw_fail
    try:
        before = hwaccel.status()["fallbacks"]
        r = ffmpegx.run(args, check=True)
        ck("run 硬件失败→软件重试", len(calls) == 2 and r.returncode == 0, calls)
        ck("回退已上报", hwaccel.status()["fallbacks"] == before + 1)
        ck("软件结果作为返回值", r.stdout == b"OK")
    finally:
        ffmpegx._run_raw = orig

    calls.clear()
    ffmpegx._run_raw = lambda a, t, c: _fake_result(1, b"", "参数错误".encode("utf-8"))
    try:
        before = hwaccel.status()["failures"]
        raised = False
        try:
            ffmpegx.run(args, check=True)
        except RuntimeError:
            raised = True
        ck("两条都失败按原语义抛错", raised)
        ck("命令自身错误不误计硬件故障", hwaccel.status()["failures"] == before)
    finally:
        ffmpegx._run_raw = orig


# ── F vision 主扫描回退 ───────────────────────────────────────────────
def test_vision_fallback() -> None:
    hwaccel.set_mode("auto")
    if not _hardware():
        print("  SKIP 无硬件后端，跳过 vision 回退用例")
        return
    info = ffmpegx.probe(VIDEO)
    sr = info["sample_rate"] or 48000
    orig = vision._scan_once
    seen: list[bool] = []

    def fake(cmd, has_audio, srate, timeout):
        used = "-hwaccel" in cmd
        seen.append(used)
        if used:
            return [], [], 1, ["模拟硬件解码失败"], False, False
        return orig(cmd, has_audio, srate, timeout)

    vision._scan_once = fake
    try:
        before = hwaccel.status()["fallbacks"]
        d, a = vision._scan_signals(VIDEO, bool(info["has_audio"]), sr=sr,
                                    threads=2, timeout=180)
        ck("vision 硬件失败整体软件重跑", len(seen) == 2 and seen[0] and not seen[1], seen)
        ck("软件重跑拿到完整数据", len(d) > 0 and len(a) > 0, (len(d), len(a)))
        ck("vision 回退已上报", hwaccel.status()["fallbacks"] == before + 1)
    finally:
        vision._scan_once = orig
    hwaccel.recheck()


# ── G 结果一致性 ──────────────────────────────────────────────────────
def test_equiv() -> None:
    info = ffmpegx.probe(VIDEO)
    sr = info["sample_rate"] or 48000
    ha = bool(info["has_audio"])
    hwaccel.set_mode("auto")
    d1, a1 = vision._scan_signals(VIDEO, ha, sr=sr, threads=2, timeout=180)
    e1 = vision.scan_episode(VIDEO, ha, threads=2, timeout=180)
    m1 = multimodal.scan_episode({"path": VIDEO, "file": "01.mp4",
                                  "dur": info["duration"], "has_audio": ha})
    hwaccel.set_mode("off")
    d2, a2 = vision._scan_signals(VIDEO, ha, sr=sr, threads=2, timeout=180)
    e2 = vision.scan_episode(VIDEO, ha, threads=2, timeout=180)
    m2 = multimodal.scan_episode({"path": VIDEO, "file": "01.mp4",
                                  "dur": info["duration"], "has_audio": ha})
    ck("画面 pblack 序列逐条一致", d1 == d2, f"{len(d1)} vs {len(d2)}")
    ck("声音峰值序列逐条一致", a1 == a2, f"{len(a1)} vs {len(a2)}")
    ck("动作事件结果一致", e1["events"] == e2["events"])
    ck("动作计数一致", (e1["motion"], e1["spikes"]) == (e2["motion"], e2["spikes"]))
    ck("多模态画面采样一致", m1["samples"] == m2["samples"])
    ck("多模态声音包络一致", m1["audio"] == m2["audio"])
    ck("多模态镜头切换一致", m1["shots"] == m2["shots"])
    hwaccel.set_mode("auto")


# ── H 接口 ───────────────────────────────────────────────────────────
def test_api() -> None:
    s = APP.hwaccel_state()
    ck("GET /api/hwaccel 状态完整", all(k in s for k in
                                        ("mode", "capable", "attempts", "benchmark", "decodes")))
    s = APP.hwaccel_mode({"mode": "off"})
    ck("POST mode=off 生效", s["mode"] == "software")
    s = APP.hwaccel_mode({"mode": "auto"})
    ck("POST mode=auto 生效", s["mode"] in ("hardware", "software"))
    s = APP.hwaccel_recheck({})
    ck("POST recheck 可用", s["checked"] and s["failures"] == 0)
    st = APP.status()
    ck("/api/status 带 hwaccel 字段", "hwaccel" in st and "mode" in st["hwaccel"])
    try:
        d = APP.doctor()
        ck("/api/doctor 含硬件解码项", any(x["item"] == "硬件解码" for x in d["items"]))
        ck("/api/doctor 含后端自检项", any(x["item"] == "后端自检" for x in d["items"]))
    except Exception as e:  # noqa: BLE001
        ck("/api/doctor 可用", False, e)
    from fastapi import HTTPException
    bad = False
    try:
        APP.hwaccel_mode({"mode": "__invalid__"})
    except HTTPException:
        bad = True
    ck("非法模式返回 400", bad)
    hwaccel.set_mode("auto")


def main() -> int:
    print("硬件解码测试 ·", VIDEO)
    if not FIX.exists():
        print("缺少测试片源，请先跑 gen_fixtures.py")
        return 2
    for fn in (test_detect, test_modes, test_failure, test_inject,
               test_run_fallback, test_vision_fallback, test_equiv, test_api):
        print("─", fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ck(fn.__name__ + " 未抛异常", False, repr(e))
    ok = sum(1 for c in CHECKS if c["ok"])
    bad = [c for c in CHECKS if not c["ok"]]
    hwaccel.set_mode("auto")
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "hwaccel.json").write_text(
        json.dumps({"ok": len(bad) == 0, "total": len(CHECKS),
                    "passed": ok, "failed": bad,
                    "final_status": hwaccel.status()},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{ok}/{len(CHECKS)} 通过；失败 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
import tempfile
import threading
import time
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
    ck("非法后端原因清晰",
       ("不在 ffmpeg 支持列表" in s["reason"]) or ("未包含任何硬件加速后端" in s["reason"]),
       s["reason"])

    s = hwaccel.set_mode("smart")
    bm = s.get("benchmark") or {}
    if bm.get("valid"):
        expect = "hardware" if bm["faster"] else "software"
        ck("smart 按实测收益决策", s["mode"] == expect, bm)
    # 无独显 / 精简 ffmpeg 时 capable 必须是 False，不能硬性要求 True
    ck("smart 结果自洽",
       (s["mode"] == "hardware" and s["capable"]) or
       (s["mode"] == "software" and ("软件" in s["reason"] or "关闭" in s["reason"])),
       (s["mode"], s["capable"], s["reason"]))
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


# ── I 假硬件识别（P0） ────────────────────────────────────────────────
def test_no_false_hw() -> None:
    """mpeg4 等编码会被 ffmpeg 静默软解，不能算硬件自检通过。"""
    import subprocess
    tmp = Path(tempfile.gettempdir()) / "dc_mpeg4_false_hw.mp4"
    p = subprocess.run([ffmpegx.ffmpeg(), "-hide_banner", "-nostdin", "-y",
                        "-v", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=320x180:rate=25", "-t", "1",
                        "-an", "-c:v", "mpeg4", "-q:v", "5", str(tmp)],
                       capture_output=True, timeout=60,
                       creationflags=ffmpegx.CREATE_NO_WINDOW)
    try:
        ck("生成 mpeg4 测试片", p.returncode == 0 and tmp.exists(), p.returncode)
        backend = hwaccel.status()["backend"] or "cuda"
        ok, why, ms = hwaccel._verify(backend, str(tmp))
        ck("静默软解不算硬件通过", not ok, why)
        ck("未通过原因写明软解/失败", ("软件" in why) or ("失败" in why), why)
    finally:
        tmp.unlink(missing_ok=True)


# ── J 自检期间不阻塞（P0） ────────────────────────────────────────────
def test_lock_nonblocking() -> None:
    if not _hardware():
        print("  SKIP 无硬件，跳过自检阻塞用例")
        return
    orig = hwaccel.benchmark
    hwaccel.benchmark = lambda *a, **k: (time.sleep(1.2), {"valid": False})[1]
    hwaccel._STATE["checked"] = False
    th = threading.Thread(target=hwaccel.detect, kwargs={"force": True}, daemon=True)
    th.start()
    time.sleep(0.2)
    t = time.time(); hwaccel.status(); ds = time.time() - t
    t = time.time(); pre = hwaccel.prefix(); dp = time.time() - t
    th.join(timeout=30)
    hwaccel.benchmark = orig
    ck("检测中 status 不阻塞", ds < 0.3, f"{ds:.2f}s")
    ck("检测中 prefix 不阻塞且回软件", dp < 0.3 and pre == [], f"{dp:.2f}s {pre}")
    ck("检测完成后恢复硬件", hwaccel.status()["mode"] == "hardware")


# ── K 收益基准过滤失败轮次（P0） ──────────────────────────────────────
def test_benchmark_rc() -> None:
    orig = hwaccel.subprocess.run

    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = b""
            self.stderr = b""

    hwaccel.subprocess.run = lambda cmd, **kw: R(1 if "-hwaccel" in cmd else 0)
    try:
        bm = hwaccel.benchmark("x.mp4", "cuda", loops=1, rounds=1)
        ck("基准排除失败轮次", bm.get("valid") is False, bm)
    finally:
        hwaccel.subprocess.run = orig


# ── L sample 白名单（P1） ────────────────────────────────────────────
def test_sample_guard() -> None:
    bad = [str(ROOT / "app.py"), "http://127.0.0.1:9/x.mp4",
           r"\\server\share\x.mp4", "-i", "", str(ROOT / "no_such.mp4")]
    for s in bad:
        ck("拒绝非法 sample", hwaccel.valid_sample(s) == "", s[:40])
    ck("接受真实视频 sample", hwaccel.valid_sample(VIDEO) != "", VIDEO)
    from fastapi import HTTPException
    for s in (str(ROOT / "app.py"), "http://127.0.0.1:9/x.mp4"):
        raised = False
        try:
            APP.hwaccel_recheck({"sample": s})
        except HTTPException as e:
            raised = e.status_code == 400
        ck("API 非法 sample 返回 400", raised, s[:40])
    ok = APP.hwaccel_recheck({"sample": VIDEO})
    ck("API 接受真实视频 sample",
       ok.get("checked") is True and bool(ok.get("sample")), ok.get("sample"))
    hwaccel._SAMPLE_OVERRIDE = None


# ── M 坏样片自动重造（P1） ────────────────────────────────────────────
def test_sample_repair() -> None:
    bad = Path(tempfile.gettempdir()) / "dc_repair_probe.mp4"
    bad.write_bytes(b"X" * 5000)
    orig = hwaccel.PROBE_SAMPLE
    hwaccel.PROBE_SAMPLE = bad
    hwaccel._SAMPLE_OVERRIDE = None
    try:
        got = hwaccel._ensure_sample()
        ck("坏样片被重造且可解码",
           bool(got) and hwaccel._sample_ok(Path(got)), got)
    finally:
        hwaccel.PROBE_SAMPLE = orig
        bad.unlink(missing_ok=True)


# ── N 版本串（P1） ──────────────────────────────────────────────────
def test_version() -> None:
    ffmpegx._FFMPEG_VER = None
    v = ffmpegx.version()
    ck("ffmpeg 版本读到 stdout", "ffmpeg version" in v.lower(), v[:80])
    st = APP.status()
    ck("/api/status 版本不是 unknown",
       "unknown" not in str(st.get("ffmpeg_version", "")).lower(),
       st.get("ffmpeg_version"))


# ── O 无 GPU / 精简 ffmpeg 可移植（P1） ──────────────────────────────
def test_modes_portable() -> None:
    orig = hwaccel.compiled_methods
    hwaccel.compiled_methods = lambda: []
    try:
        s = hwaccel.set_mode("smart")
        ck("无编译后端时明确回退",
           s["mode"] == "software" and "未包含" in s["reason"], s["reason"])
        ck("无编译后端时 capable=false", s["capable"] is False)
        s = hwaccel.set_mode("__no_such_backend__")
        ck("无编译后端时原因可读",
           s["mode"] == "software" and
           ("未包含" in s["reason"] or "不在 ffmpeg 支持列表" in s["reason"]),
           s["reason"])
    finally:
        hwaccel.compiled_methods = orig
    s = hwaccel.set_mode("auto")
    ck("恢复自动模式", s["checked"] is True)


# ── P iter_lines 溢出统计（P1） ──────────────────────────────────────
def test_iter_overflow() -> None:
    if not _hardware():
        print("  SKIP 无硬件，跳过 iter_lines 溢出用例")
        return
    cap, once = ffmpegx.HW_LINE_RETRY_CAP, ffmpegx._iter_once

    def fake(args, stream, timeout, holder):
        for i in range(5):
            yield f"line{i}"
        holder["rc"] = 1

    ffmpegx.HW_LINE_RETRY_CAP = 3
    ffmpegx._iter_once = fake
    try:
        before = hwaccel.status()["decodes"]["software"]
        bf = hwaccel.status()["fallbacks"]
        lines = list(ffmpegx.iter_lines(["-i", VIDEO], allow_hw=True))
        ck("溢出后输出不重复", lines == [f"line{i}" for i in range(5)], lines)
        ck("溢出且失败记软件", hwaccel.status()["decodes"]["software"] == before + 1)
        ck("溢出失败计入回退", hwaccel.status()["fallbacks"] == bf + 1)
    finally:
        ffmpegx.HW_LINE_RETRY_CAP = cap
        ffmpegx._iter_once = once


# ── Q 前端状态刷新（P1） ────────────────────────────────────────────
def test_frontend_refresh() -> None:
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    ck("前端有自检轮询", "function pollHw" in js and "d.checking" in js)
    ck("前端定时刷新徽标", "renderHwChip(st.hwaccel)" in js)
    ck("前端切换按钮有处理中提示", "自检中…" in js and "切换中…" in js)


def main() -> int:
    print("硬件解码测试 ·", VIDEO)
    if not FIX.exists():
        print("缺少测试片源，请先跑 gen_fixtures.py")
        return 2
    for fn in (test_detect, test_modes, test_failure, test_inject,
               test_run_fallback, test_vision_fallback, test_equiv, test_api,
               test_no_false_hw, test_lock_nonblocking, test_benchmark_rc,
               test_sample_guard, test_sample_repair, test_version,
               test_modes_portable, test_iter_overflow, test_frontend_refresh):
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

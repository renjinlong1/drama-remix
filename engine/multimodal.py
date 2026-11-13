"""多模态识别：主题 / 场景 / 情绪 / 关键内容 / 节奏 + 跨模态关联。

便携包里没有 torch / scipy / 语义模型，也不该为了一个功能往里塞几百 MB 权重。
这里做的是**可解释的特征融合**，四条信号各司其职、按时间轴对齐：

  文本  字幕 / OCR 台词 → 主题词分布、情绪词、关键信息（冲突、反转、悬念）
  画面  每 2 秒一帧 64×36 → 亮度、色温、色彩、镜头切换（直方图 + 帧差）
  声音  8kHz 单声道 PCM → 0.5 秒窗 RMS / 峰值 / 静音比例
  动作  vision 已有的 10fps 画面冲击 + 声音瞬态事件（打人、砸东西）

一个「融合单元」= 一段对齐到台词边界的时间窗，四路信号全部挂到同一时间轴上，
每路都留证据与置信度。跨模态关联不是把分数相加，而是看几路信号**是否同时指向
同一结论**：台词在吵 + 声音有峰值 + 画面在动 = 高置信的冲突段；只有一路有信号
= 低置信，混剪时自然排在后面。

输出的是纯 Python 基础类型，落盘缓存后前端和自动混剪共用同一份结果。
"""
from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpegx

try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None

# ── 采样参数 ─────────────────────────────────────────────────────────
SAMPLE_STEP = 2.0          # 每 2 秒取一帧
SAMPLE_W, SAMPLE_H = 64, 36
FRAME_BYTES = SAMPLE_W * SAMPLE_H * 3
AUD_SR = 8000
AUD_STEP = 0.5
WIN_MIN, WIN_MAX = 4.0, 14.0
UNIFORM_WIN = 7.0
SHOT_DIFF = 0.16           # 相邻采样帧归一化差异超过它算一次镜头切换
CACHE_VERSION = 2

# ── 主题词库（中英双语：片源常是英文剧、中文字幕，两种都要认） ──────
TOPICS: dict[str, dict] = {
    "亲密情感": {
        "cn": ["爱", "喜欢", "心动", "吻", "抱", "牵手", "老公", "老婆", "亲爱的",
               "在一起", "嫁给", "娶", "婚姻", "婚礼", "暧昧", "想你", "疼你", "心软"],
        "en": ["love", "kiss", "marry", "marriage", "wedding", "husband", "wife",
               "darling", "dear", "baby", "hold me", "miss you", "feel", "heart",
               "touch", "close", "together", "romance"],
        "aliases": ["爱情", "感情", "恋爱", "甜蜜", "亲密", "浪漫", "romance", "love"],
        "valence": 0.55, "arousal": 0.35,
    },
    "冲突对抗": {
        "cn": ["滚", "打", "巴掌", "耳光", "闭嘴", "算什么", "凭什么", "敢", "吵架",
               "离婚", "退婚", "赶出去", "羞辱", "欺负", "报复", "恨", "怒", "骂"],
        "en": ["get out", "shut up", "slap", "hit", "how dare", "divorce", "hate",
               "leave", "never", "worthless", "bastard", "fight", "argue", "insult",
               "revenge", "nightmare", "deal", "let me go"],
        "aliases": ["打脸", "冲突", "吵架", "对抗", "撕逼", "conflict", "fight"],
        "valence": -0.75, "arousal": 0.90,
    },
    "身份真相": {
        "cn": ["身份", "真相", "原来", "其实", "鉴定", "亲子", "遗产", "总裁", "董事长",
               "揭露", "暴露", "冒充", "替身", "真正", "居然", "竟然", "发现"],
        "en": ["truth", "identity", "actually", "reveal", "secret", "real", "heir",
               "dna", "test", "result", "discover", "found out", "pretend", "replace",
               "marquess", "lord", "duke", "lady", "company", "chairman"],
        "aliases": ["反转", "真相", "揭晓", "身份", "reveal", "truth"],
        "valence": 0.10, "arousal": 0.70,
    },
    "危机悬疑": {
        "cn": ["小心", "危险", "快跑", "救命", "失踪", "绑架", "威胁", "阴谋", "查",
               "谁", "为什么", "怎么办", "等着", "别", "来不及", "死", "血"],
        "en": ["danger", "run", "help", "please", "who", "why", "what if", "wait",
               "threat", "kidnap", "missing", "kill", "die", "blood", "hurry",
               "don't", "can't", "nightmare", "afraid", "scared"],
        "aliases": ["悬念", "危机", "悬疑", "紧张", "suspense", "thriller"],
        "valence": -0.45, "arousal": 0.80,
    },
    "家族权谋": {
        "cn": ["家族", "父亲", "母亲", "爷爷", "奶奶", "少爷", "小姐", "夫人", "契约",
               "协议", "股份", "权力", "继承", "安排", "规矩", "长辈", "管家"],
        "en": ["family", "father", "mother", "sir", "madam", "contract", "agreement",
               "shares", "power", "inherit", "arrange", "rule", "butler", "estate",
               "please your husband", "marriage was a deal"],
        "aliases": ["家族", "权谋", "宅斗", "商战", "family", "power"],
        "valence": -0.10, "arousal": 0.45,
    },
    "日常过渡": {
        "cn": ["好的", "嗯", "走吧", "吃饭", "明天", "早上", "谢谢", "没关系", "到了",
               "稍等", "请进", "坐"],
        "en": ["okay", "yes", "no", "thanks", "thank you", "hello", "hi", "morning",
               "tomorrow", "let's go", "come in", "sit down", "wait", "sure"],
        "aliases": ["日常", "过渡", "铺垫", "闲聊", "daily", "slice"],
        "valence": 0.05, "arousal": 0.15,
    },
}
TOPIC_ORDER = list(TOPICS)
# 别名 → 标准主题：用户手填目标主题时先归到标准类，再叠加字面匹配
ALIAS_MAP = {a.lower(): name for name, cfg in TOPICS.items() for a in cfg["aliases"]}
ALIAS_MAP.update({name.lower(): name for name in TOPICS})
# 平台黑话 → 标准主题：用户常填「复仇 / 甜宠 / 打脸」这类词，词库里要能落回去
QUERY_SYNONYMS = {
    "复仇": "冲突对抗", "报仇": "冲突对抗", "打脸": "冲突对抗", "虐渣": "冲突对抗",
    "撕逼": "冲突对抗", "爽点": "冲突对抗", "逆袭": "身份真相", "掉马": "身份真相",
    "认亲": "身份真相", "豪门": "家族权谋", "宅斗": "家族权谋", "宫斗": "家族权谋",
    "商战": "家族权谋", "替身": "身份真相", "追妻": "亲密情感", "甜宠": "亲密情感",
    "先婚后爱": "亲密情感", "撒糖": "亲密情感", "虐恋": "亲密情感", "暧昧": "亲密情感",
    "狼人": "身份真相", "吸血鬼": "危机悬疑", "重生": "身份真相", "穿越": "身份真相",
    "悬疑": "危机悬疑", "追杀": "危机悬疑", "逃脱": "危机悬疑", "日常": "日常过渡",
    "铺垫": "日常过渡", "复仇线": "冲突对抗", "感情线": "亲密情感",
}

POS_WORDS = ("爱", "喜欢", "谢谢", "恭喜", "太好了", "放心", "开心", "幸福", "抱", "吻",
             "love", "happy", "thank", "glad", "safe", "beautiful", "please", "sorry")
NEG_WORDS = ("滚", "恨", "闭嘴", "滚出去", "死", "滚开", "恶心", "滚蛋", "离婚", "欺负",
             "hate", "get out", "shut up", "kill", "worthless", "nightmare", "leave me")
AROUSE_WORDS = ("快", "住手", "不要", "救命", "竟然", "居然", "马上", "立刻", "！", "!",
                "stop", "now", "hurry", "don't", "never", "what", "why", "help")

_EN_WORD = re.compile(r"[a-z']{2,}")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def _norm(s: str) -> str:
    return _PUNCT.sub(" ", str(s or "").lower()).strip()


def _ngrams(s: str) -> set[str]:
    """中文取双字、英文取词；短查询也能和长台词算重叠。"""
    s = _norm(s)
    grams = {"".join(m) for m in re.findall(r"(?=([\u4e00-\u9fff]{2}))", s)}
    grams |= set(_EN_WORD.findall(s))
    return {g for g in grams if g}


def _kw_hits(text: str, words: list[str]) -> int:
    """中文子串 + 英文词边界；返回命中次数（同一词重复出现按一次计）。"""
    low = text.lower()
    n = 0
    for w in words:
        if not w:
            continue
        if any("\u4e00-\u9fff" <= ch <= "\u9fff" for ch in w):
            if w in text:
                n += 1
        elif re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
            n += 1
    return n


def topic_scores(text: str) -> dict[str, float]:
    """一段台词在六个主题上的强度（0~1）。"""
    out: dict[str, float] = {}
    if not text.strip():
        return {k: 0.0 for k in TOPIC_ORDER}
    length = max(1.0, math.sqrt(len(text) / 24.0))
    for name, cfg in TOPICS.items():
        hits = _kw_hits(text, cfg["cn"]) + _kw_hits(text, cfg["en"])
        out[name] = min(1.0, hits / (1.6 * length))
    # 全都没命中 → 归到日常过渡，别让"无信息"看起来和"高置信"一样
    if max(out.values()) <= 0.001:
        out["日常过渡"] = 0.22
    return out


def topic_of(unit: dict) -> str:
    return max(unit["topics"], key=lambda k: unit["topics"][k])


def query_match(query: str, unit: dict) -> tuple[float, str]:
    """目标主题（自由文本）与片段的相关度，返回 (分数, 匹配方式)。

    三路取最大：标准主题别名 → 该主题强度；字面包含 → 高分；字/词 n-gram 重叠 → 软匹配。
    用户填「复仇」「狼人」「西幻」这种词库里没有的词也能靠 n-gram 找到相关片段。
    """
    q = str(query or "").strip()
    if not q:
        return 1.0, "未指定"
    ql = q.lower()
    # 主题词库：低底分 + 主题强度，别让「沾一点边」的片段全部混进来
    for syn, name in QUERY_SYNONYMS.items():
        if syn == q or syn in q or q in syn:
            return min(1.0, 0.10 + 0.90 * unit["topics"].get(name, 0.0)), "主题词库"
    if ql in ALIAS_MAP:
        name = ALIAS_MAP[ql]
        return min(1.0, 0.10 + 0.90 * unit["topics"].get(name, 0.0)), "主题词库"
    if len(ql) >= 2:
        for alias, name in ALIAS_MAP.items():
            if len(alias) >= 2 and (alias in ql or ql in alias):
                return min(1.0, 0.10 + 0.90 * unit["topics"].get(name, 0.0)), "主题词库"
    text = _norm(unit.get("text", ""))
    if _norm(q) and _norm(q) in text:
        return 1.0, "台词命中"
    qg, tg = _ngrams(q), _ngrams(text)
    if qg and tg:
        dice = 2 * len(qg & tg) / (len(qg) + len(tg))
        if dice > 0:
            return round(min(0.9, dice * 1.6), 3), "语义近似"
    return 0.0, "无匹配"


# ── 单集底层信号 ─────────────────────────────────────────────────────
def _visual_samples(video: str) -> list[list]:
    """[[t, 亮度, 色温, 色彩, 与上一帧差异], ...]，每 2 秒一行。"""
    raw = ffmpegx.run_out(
        ["-i", video, "-an", "-vf", f"fps=1/{SAMPLE_STEP:g},scale={SAMPLE_W}:{SAMPLE_H}",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], timeout=900)
    if np is None or not raw:
        return []
    n = len(raw) // FRAME_BYTES
    if not n:
        return []
    arr = np.frombuffer(raw[: n * FRAME_BYTES], dtype=np.uint8).reshape(n, SAMPLE_H, SAMPLE_W, 3)
    f = arr.astype(np.float32) / 255.0
    bright = f.mean(axis=(1, 2, 3))
    warm = f[:, :, :, 0].mean(axis=(1, 2)) - f[:, :, :, 2].mean(axis=(1, 2))
    color = (f.max(axis=3) - f.min(axis=3)).mean(axis=(1, 2))
    flat = f.reshape(n, -1)
    diff = np.zeros(n, dtype=np.float32)
    if n > 1:
        diff[1:] = np.abs(flat[1:] - flat[:-1]).mean(axis=1)
    return [[round(i * SAMPLE_STEP, 2), round(float(bright[i]), 4),
             round(float(warm[i]), 4), round(float(color[i]), 4),
             round(float(diff[i]), 4)] for i in range(n)]


def _audio_envelope(video: str, has_audio: bool) -> list[list]:
    """[[t, rms_db, peak_db], ...]，0.5 秒一窗。"""
    if not has_audio:
        return []
    raw = ffmpegx.run_out(
        ["-i", video, "-vn", "-ac", "1", "-ar", str(AUD_SR),
         "-acodec", "pcm_s16le", "-f", "s16le", "-"], timeout=900)
    if np is None or len(raw) < 4:
        return []
    a = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
    win = int(AUD_SR * AUD_STEP)
    n = len(a) // win
    if not n:
        return []
    blocks = a[: n * win].reshape(n, win).astype(np.float32) / 32768.0
    rms = np.sqrt((blocks * blocks).mean(axis=1))
    peak = np.abs(blocks).max(axis=1)
    db = lambda x: 20.0 * np.log10(np.maximum(x, 1e-6))  # noqa: E731
    return [[round(i * AUD_STEP, 2), round(float(db(rms[i])), 2),
             round(float(db(peak[i])), 2)] for i in range(n)]


def scan_episode(ep: dict) -> dict:
    """一集的画面 / 声音底层特征。失败只让该集降级，不拖垮整剧。"""
    t0 = time.time()
    path = str(ep["path"])
    has_audio = bool(ep.get("has_audio"))
    try:
        samples = _visual_samples(path)
    except Exception as e:  # noqa: BLE001
        print(f"[mm] 画面采样跳过 {ep.get('file')}: {e}", flush=True)
        samples = []
    try:
        audio = _audio_envelope(path, has_audio)
    except Exception as e:  # noqa: BLE001
        print(f"[mm] 声音采样跳过 {ep.get('file')}: {e}", flush=True)
        audio = []
    # 镜头切换：相邻采样帧的归一化差异显著高于本集中位数
    shots: list[float] = []
    if len(samples) > 3:
        base = sorted(s[4] for s in samples[1:])[len(samples) // 2]
        thr = max(SHOT_DIFF, base * 2.2)
        shots = [round(s[0], 2) for s in samples[1:] if s[4] >= thr]
    return {"file": ep.get("file", ""), "dur": round(float(ep["dur"]), 3),
            "samples": samples, "audio": audio, "shots": shots,
            "secs": round(time.time() - t0, 2)}


def cache_path(name: str, root: Path) -> Path:
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "project"
    return root / "data" / "multimodal" / f"{safe}.json"


def scan_project(proj: dict, root: Path, progress=None, force: bool = False) -> dict:
    """整剧扫描（每集一次画面 + 一次声音解码），按集文件 + 时长做增量缓存。"""
    path = cache_path(proj["name"], root)
    cache: dict = {}
    if path.exists() and not force:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if d.get("version") == CACHE_VERSION:
                cache = d
        except Exception:  # noqa: BLE001  缓存坏了就当没有，重新扫
            cache = {}
    eps: dict = dict(cache.get("eps") or {})

    def stale(ep: dict) -> bool:
        r = eps.get(str(ep["idx"]))
        return (not r or r.get("file") != ep.get("file")
                or abs(float(r.get("dur", -1)) - float(ep["dur"])) > 0.5)

    todo = [e for e in proj["episodes"] if force or stale(e)]

    def one(ep: dict) -> tuple[int, dict]:
        return ep["idx"], scan_episode(ep)

    if todo:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, (idx, res) in enumerate(pool.map(one, todo), 1):
                eps[str(idx)] = res
                if progress:
                    progress(i / len(todo) * 100.0, f"多模态识别 {i}/{len(todo)} 集")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": CACHE_VERSION, "name": proj["name"], "eps": eps},
                               ensure_ascii=False), encoding="utf-8")
    return {"eps": len(eps), "scanned": len(todo), "path": str(path)}


# ── 融合 ─────────────────────────────────────────────────────────────
def _units_from_beats(proj: dict, beats: list[dict]) -> list[dict]:
    """把台词节拍拼成 4~14 秒的融合单元；无台词的集用等长窗口兜底。"""
    units: list[dict] = []
    for ep in proj["episodes"]:
        eidx, off, dur = ep["idx"], float(ep["offset"]), float(ep["dur"])
        mine = [b for b in beats if b["ep"] == eidx]
        if not mine:
            t = 0.0
            while t < dur - 0.8:
                units.append({"ep": eidx, "g0": round(off + t, 3),
                              "g1": round(off + min(dur, t + UNIFORM_WIN), 3),
                              "text": "", "beats": [], "score": 0.0})
                t += UNIFORM_WIN
            continue
        group: list[dict] = []
        for b in mine:
            if not group:
                group = [b]
                continue
            span = b["g1"] - group[0]["g0"]
            if span > WIN_MAX:
                units.append(_mk_unit(eidx, group, off, dur))
                group = [b]
            elif span >= WIN_MIN:
                group.append(b)
                units.append(_mk_unit(eidx, group, off, dur))
                group = []
            else:
                group.append(b)
        if group:
            units.append(_mk_unit(eidx, group, off, dur))
    units.sort(key=lambda u: (u["ep"], u["g0"]))
    for i, u in enumerate(units):
        u["id"] = i
    return units


def _mk_unit(ep: int, group: list[dict], off: float, dur: float) -> dict:
    g0 = max(off, group[0]["g0"] - 0.15)
    g1 = min(off + dur, group[-1]["g1"] + 0.25)
    return {"ep": ep, "g0": round(g0, 3), "g1": round(g1, 3),
            "text": " ".join(b["text"] for b in group)[:400],
            "beats": [b["id"] for b in group],
            "score": round(sum(float(b.get("score", 0)) for b in group), 3)}


def _window(vals: list[list], t0: float, t1: float, col: int) -> list[float]:
    return [float(v[col]) for v in vals if t0 <= v[0] < t1]


def _mean(xs: list[float], default: float = 0.0) -> float:
    return sum(xs) / len(xs) if xs else default


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def fuse(proj: dict, beats: list[dict] | None = None, acts: list[dict] | None = None,
         mm: dict | None = None, highlights: list[dict] | None = None) -> dict:
    """把四路信号对齐成融合单元。没有 mm 缓存时纯文本也能跑（降级但可用）。"""
    beats = beats if beats is not None else []
    acts = acts or []
    raw = (mm or {}).get("eps") or {}
    hl = highlights or []
    units = _units_from_beats(proj, beats)
    by_ep_acts: dict[int, list[dict]] = {}
    for a in acts:
        by_ep_acts.setdefault(a.get("ep", 0), []).append(a)
    by_ep_hl: dict[int, list[dict]] = {}
    for h in hl:
        by_ep_hl.setdefault(h.get("ep", 0), []).append(h)

    out: list[dict] = []
    for u in units:
        ep = proj["episodes"][u["ep"] - 1] if 0 < u["ep"] <= len(proj["episodes"]) else None
        off = float(ep["offset"]) if ep else 0.0
        t0, t1 = u["g0"] - off, u["g1"] - off
        dur = max(0.3, u["g1"] - u["g0"])
        feat = raw.get(str(u["ep"])) or {}
        samples, audio = feat.get("samples") or [], feat.get("audio") or []
        shots = [t for t in (feat.get("shots") or []) if t0 <= t < t1]

        # 文本信号
        tp = topic_scores(u["text"])
        pos, neg, aro = (_kw_hits(u["text"], list(POS_WORDS)),
                         _kw_hits(u["text"], list(NEG_WORDS)),
                         _kw_hits(u["text"], list(AROUSE_WORDS)))
        topic = max(tp, key=lambda k: tp[k])

        # 画面信号
        br = _mean(_window(samples, t0, t1, 1), 0.42)
        wm = _mean(_window(samples, t0, t1, 2), 0.0)
        co = _mean(_window(samples, t0, t1, 3), 0.15)
        shot_rate = len(shots) / dur

        # 声音信号
        rms = _window(audio, t0, t1, 1)
        pk = _window(audio, t0, t1, 2)
        energy = _clamp01((_mean(rms, -40.0) + 48.0) / 36.0)
        silent = (sum(1 for x in rms if x < -38.0) / len(rms)) if rms else 0.0
        peaks = sum(1 for x in pk if x > -12.0)

        # 动作信号
        ev = [a for a in by_ep_acts.get(u["ep"], ())
              if a["g1"] >= u["g0"] - 0.4 and a["g0"] <= u["g1"] + 0.4]
        motion = _clamp01(sum(float(a.get("v", 0)) for a in ev) / 2.0)
        act_max = max((float(a.get("score", 0)) for a in ev), default=0.0)

        # 情绪：文本极性 + 声音能量 + 画面运动 → 效价 / 唤醒，四象限
        valence = _clamp01(0.5 + 0.13 * (pos - neg) + 0.12 * wm)
        arousal = _clamp01(0.12 + 0.42 * energy + 0.28 * motion + 0.10 * min(1.0, aro / 2))
        valence = max(0.0, min(1.0, valence - 0.22 * neg / max(1.0, neg + pos + 1)))
        if arousal >= 0.60 and valence < 0.46:
            emotion = "紧张冲突"
        elif arousal >= 0.60:
            emotion = "情绪爆发"
        elif valence >= 0.58:
            emotion = "温暖亲密"
        elif valence <= 0.36:
            emotion = "压抑低沉"
        else:
            emotion = "平静叙事"

        # 场景：亮度 / 色温 / 镜头密度，附可核查的数值证据
        if br < 0.24:
            scene = "暗场夜戏"
        elif wm > 0.045 and br > 0.42:
            scene = "暖光室内"
        elif co > 0.30:
            scene = "鲜艳外景"
        elif shot_rate > 0.45:
            scene = "多镜快切"
        else:
            scene = "常规中景"

        # 节奏：镜头密度 + 台词密度 + 声音峰值 + 画面运动，归一化到 0~1
        speech = len(u["beats"]) / dur
        pace = _clamp01(0.34 * min(1.0, shot_rate / 0.6)
                        + 0.28 * min(1.0, speech / 0.45)
                        + 0.22 * min(1.0, peaks / max(1.0, dur * 0.6))
                        + 0.16 * motion)
        # 静音多的段落节奏更慢
        pace = _clamp01(pace - 0.16 * silent)

        # 关键信息：已有高光分 + 台词分 + 动作分 + 主题稀有度
        hov = max((float(h.get("score", 0)) for h in by_ep_hl.get(u["ep"], ())
                   if h["g1"] > u["g0"] and h["g0"] < u["g1"]), default=0.0)
        key = _clamp01(0.22 * min(1.0, _norm_highlight(hov))
                       + 0.24 * min(1.0, u["score"] / 12.0)
                       + 0.24 * min(1.0, act_max / 2.0)
                       + 0.16 * max(tp.values())
                       + 0.14 * min(1.0, len(u["text"]) / 90.0))

        # 跨模态关联：几路信号在场 + 结论是否一致
        present = [bool(u["text"].strip()), bool(samples), bool(audio), bool(ev or shots)]
        agree = 0.0
        if neg and (peaks or motion > 0.3):
            agree += 0.18
        if pos and wm > 0.01 and arousal < 0.6:
            agree += 0.12
        if tp.get(topic, 0) > 0.30 and (ev or samples):
            agree += 0.10
        confidence = _clamp01(0.12 + 0.72 * (sum(present) / 4.0) + agree)
        evidence = []
        if u["text"].strip():
            evidence.append(f"台词主题「{topic}」{tp[topic]:.2f}")
        if samples:
            evidence.append(f"画面{scene} 亮度{br:.2f} 镜头{len(shots)}")
        if audio:
            evidence.append(f"声音能量{energy:.2f} 峰值{peaks} 静音{silent:.0%}")
        if ev:
            evidence.append(f"动作事件{len(ev)} 最强{act_max:.2f}")

        out.append({
            "id": u["id"], "ep": u["ep"], "g0": u["g0"], "g1": u["g1"],
            "start": round(t0, 3), "end": round(t1, 3), "dur": round(dur, 3),
            "text": u["text"], "beats": u["beats"],
            "topics": {k: round(v, 3) for k, v in tp.items()}, "topic": topic,
            "emotion": {"label": emotion, "valence": round(valence, 3),
                        "arousal": round(arousal, 3)},
            "scene": {"label": scene, "bright": round(br, 3), "warm": round(wm, 3),
                      "color": round(co, 3), "shots": len(shots),
                      "shot_rate": round(shot_rate, 3)},
            "rhythm": {"pace": round(pace, 3), "speech": round(speech, 3),
                       "energy": round(energy, 3), "motion": round(motion, 3),
                       "peaks": peaks, "silent": round(silent, 3)},
            "key": {"score": round(key, 3),
                    "reasons": ([f"高光候选 {hov:.1f}"] if hov else [])
                    + ([f"台词分 {u['score']:.1f}"] if u["score"] > 4 else [])
                    + ([f"动作强度 {act_max:.1f}"] if act_max > 0.8 else [])},
            "confidence": round(confidence, 3), "evidence": evidence,
            "modalities": {"text": present[0], "visual": present[1],
                           "audio": present[2], "events": present[3]},
        })
    return {"version": CACHE_VERSION, "units": out,
            "stats": summarize(out), "source": "cache" if raw else "text-only"}


def _norm_highlight(x: float) -> float:
    """高光原始分（常见 3~25）压到 0~1。"""
    return min(1.0, x / 16.0)


def summarize(units: list[dict]) -> dict:
    if not units:
        return {"units": 0, "seconds": 0.0, "topics": [], "emotions": [],
                "scenes": [], "pace": 0.0, "confidence": 0.0}
    total = sum(u["dur"] for u in units)
    tsec: dict[str, float] = {}
    emo: dict[str, int] = {}
    scn: dict[str, int] = {}
    for u in units:
        for k, v in u["topics"].items():
            tsec[k] = tsec.get(k, 0.0) + v * u["dur"]
        emo[u["emotion"]["label"]] = emo.get(u["emotion"]["label"], 0) + 1
        scn[u["scene"]["label"]] = scn.get(u["scene"]["label"], 0) + 1
    topics = [{"name": k, "seconds": round(v, 1),
               "share": round(v / max(1.0, total), 3)}
              for k, v in sorted(tsec.items(), key=lambda x: -x[1])]
    return {"units": len(units), "seconds": round(total, 1),
            "topics": topics, "emotions": emo, "scenes": scn,
            "pace": round(_mean([u["rhythm"]["pace"] for u in units]), 3),
            "confidence": round(_mean([u["confidence"] for u in units]), 3)}

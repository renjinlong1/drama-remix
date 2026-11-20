# 枫的瞬剪 · 视频链路系统测试与性能对比

生成时间：见 `results/comparison.json`。基线版本 `v2026.11.10-hookui`，对照版本为本次优化后的工作区。
测量方法：**每个用例在独立子进程里跑完整链路、重复 3 轮取均值**，峰值内存取 Windows `PeakWorkingSetSize`，避免跨轮污染。

## 一、测试矩阵

素材由内置 ffmpeg 的 lavfi 合成，可重复生成；每个用例铺成 8 集（硬链接复用同一文件），贴近真实多集短剧，也让 `plan.build()` 的 30 秒成片下限能走到。

| 用例 | 容器/编码 | 分辨率 | 单集时长 | 音轨 |
|---|---|---|---|---|
| h264_360x640_6s | MP4 / H.264 | 360×640 竖屏 | 6s | 有 |
| h264_720x1280_15s | MP4 / H.264 | 720×1280 竖屏 | 15s | 有 |
| h264_720x1280_45s | MP4 / H.264 | 720×1280 竖屏 | 45s | 有 |
| mkv_720x1280_15s | MKV / H.264 | 720×1280 竖屏 | 15s | 有 |
| mov_360x640_8s | MOV / H.264 | 360×640 竖屏 | 8s | 有 |
| avi_360x640_8s | AVI / MPEG-4 | 360×640 竖屏 | 8s | 有 |
| webm_360x640_8s | WebM / VP8 + Opus | 360×640 竖屏 | 8s | 有 |
| h264_1280x720_10s | MP4 / H.264 | 1280×720 横屏 | 10s | 有 |
| h264_360x640_6s_mute | MP4 / H.264 | 360×640 竖屏 | 6s | 无 |

阶段覆盖：视频探测 → 导入/字幕配对 → 动作与音频拆解 → 节拍/钩子分析 → 顺剪规划 → 实际出片。
每个用例还做「同一输入跑两次」的结果指纹比对；3 个代表性用例做真实渲染。

## 二、优化项

| # | 位置 | 改动 | 效果 |
|---|---|---|---|
| 1 | `engine/ffmpegx.py` | `probe()` 加有界 LRU 缓存（key = 绝对路径+大小+mtime，上限 256，返回副本） | 重复导入/打开项目不再为每集重启 ffmpeg |
| 2 | `engine/ffmpegx.py` + `engine/vision.py` | 新增 `iter_lines()` 流式读取；`_diff_series/_audio_series` 改为逐行解析，不再 `capture_output` 后拼大字符串再 `splitlines()` | 消除三份日志副本，长片峰值内存大幅下降 |
| 3 | `engine/render.py` | 无 logo/模板时最后一步视频流 `-c:v copy`，只重编码音频做 loudnorm | 省掉一次全片二次转码，少一次画质损失 |
| 4 | `engine/render.py` | 8 个片段的逐段编码改为 2 路受限并发 | 顺剪渲染提速，主进程内存不上升 |
| 5 | `engine/render.py` / `app.py` | 渲染中间目录改 `try/finally` 清理；抽帧缓存加 2000 张 LRU 上限 | 修复失败渲染残留中间文件、缩略图无限堆积 |
| 6 | `engine/ffmpegx.py` | `run()` 统一加 `-nostats` | 减少长任务的无用日志输出 |

## 三、性能对比

### 总体

| 指标 | 优化前 | 优化后 | 变化 |
|---|---:|---:|---:|
| 9 个用例总耗时（各 3 轮均值之和） | 18.385s | 12.026s | **−34.6%** |
| 平均峰值内存（9 用例均值） | 36.42 MB | 27.71 MB | **−23.9%** |
| 45s 长片峰值内存 | 62.61 MB | 27.72 MB | **−55.7%** |
| 含出片用例总耗时 | 6.512s | 4.477s | −31.3% |

### 关键阶段（各用例均值）

| 阶段 | 优化前 | 优化后 | 变化 | 说明 |
|---|---:|---:|---:|---|
| `probe`（单文件） | 0.115s | 0.116s | ±0 | 首次探测仍要启一个 ffmpeg |
| `scan`（8 集首次导入） | 0.479s | 0.421s | −12.1% | 首次填充缓存 |
| `rescan`（同进程重复导入） | 0.476s | **0.003s** | **−99.4%** | 缓存命中，不再重启 8 个进程 |
| `vision`（2 集内容拆解） | 0.392s | 0.384s | −2.2% | 耗时基本持平，收益在内存 |
| `analyze` / `plan` | 0.003s / 0.001s | 0.003s / 0.001s | ±0 | 未改动 |
| `render`（8 段出片，1 个用例） | 5.199s | **3.694s** | **−28.9%** | 视频直拷 + 2 路并发 |

### 各用例耗时与峰值内存

| 用例 | 耗时 前→后 | 变化 | 峰值内存 前→后 | 变化 |
|---|---:|---:|---:|---:|
| h264_360x640_6s（含出片） | 6.512→4.477s | −31.3% | 32.94→31.44 MB | −4.6% |
| h264_720x1280_15s | 1.548→0.994s | −35.8% | 36.43→27.25 MB | −25.2% |
| h264_720x1280_45s | 2.092→1.521s | −27.3% | 62.61→27.72 MB | −55.7% |
| mkv_720x1280_15s | 1.620→1.032s | −36.3% | 36.51→27.21 MB | −25.5% |
| mov_360x640_8s | 1.339→0.806s | −39.8% | 32.31→27.05 MB | −16.3% |
| avi_360x640_8s | 1.311→0.790s | −39.7% | 32.69→27.17 MB | −16.9% |
| webm_360x640_8s | 1.306→0.799s | −38.8% | 32.85→27.20 MB | −17.2% |
| h264_1280x720_10s | 1.441→0.901s | −37.5% | 33.92→27.19 MB | −19.8% |
| h264_360x640_6s_mute | 1.216→0.706s | −42.0% | 27.49→27.20 MB | −1.1% |

## 四、结果一致性与稳定性

- **指纹一致**：9/9 用例的探测结果、字幕台词、节拍、钩子候选、动作点、顺剪方案哈希在优化前后**完全相同**（`fingerprint_same: true`）。
  流式解析没有丢失任何 blackframe / astats 日志行。
- **重复运行稳定**：每个用例连跑 3 轮，指纹完全相同；峰值内存无明显增长趋势（45s 长片优化前 52~68 MB 波动，优化后稳定在 27.6~27.8 MB）。
- **系统测试**：`test_pipeline.py` 通过 9/9 用例、110 项断言、0 失败；含 3 个真实出片用例的分辨率/时长/可播性校验。
- **HTTP 回归**：`/`、`/api/status`、`/api/state`、`/api/job`、`/api/doctor`、`/docs`、`/openapi.json`、`/api/folder/list`、三个静态资源全部 200。
- **临时文件**：渲染目录与抽帧缓存前后均为 0，无堆积。

## 五、发现并修复的问题

| 问题 | 修复 | 验证 |
|---|---|---|
| 渲染中途抛异常时 `projects/_r*` 中间片段不清理，只在服务启动时按 1 小时阈值兜底 | 清理移入 `finally`，任何路径都执行 | `渲染失败不残留中间目录 0 → 0` |
| `/api/frame` 抽帧按「文件名+时间点」永久留盘，长期多剧切换无限增长 | 加 2000 张 LRU，每生成 50 张低频检查一次 | `抽帧缓存有上限 30 → 10` |
| 无包装时成片仍做一次全片视频二次编码 | 视频流直拷，仅音频 loudnorm | 出片用例的时长/分辨率/可播性校验通过 |
| WebM 素材生成时误用 AAC 导致写不了文件头（测试设施问题） | 按容器选 `libopus` | 9/9 素材生成成功 |

## 六、交付物与复现

```
tests/
  harness.py            公共设施（峰值内存、结果指纹、阶段计时、临时文件计数）
  gen_fixtures.py       生成 9 类合成素材（8 集/用例）
  test_pipeline.py      系统测试 + 一致性 + 两项泄漏防护
  bench.py              性能基准（--tag baseline|optimized --repeat 3）
  PERFORMANCE_REPORT.md 本报告
  results/
    test_results.json    系统测试明细
    bench_baseline.json  优化前 3 轮原始数据
    bench_optimized.json 优化后 3 轮原始数据
    comparison.json      逐项对比数据
```

复现（在 `drama-remix` 目录下）：

```bash
runtime\python.exe tests\gen_fixtures.py
runtime\python.exe tests\test_pipeline.py
runtime\python.exe tests\bench.py --tag baseline  --repeat 3 --render-cases h264_360x640_6s
runtime\python.exe tests\bench.py --tag optimized --repeat 3 --render-cases h264_360x640_6s
```

## 七、已知限制

- 性能数据基于合成素材与本机 ffmpeg 6.1.1；真实片源（更高码率、更长集数、字幕密度）的绝对值会不同，相对趋势可复现。
- 峰值内存统计的是**主 Python 进程**工作集；ffmpeg 子进程内存由 OS 单独管理，不在该数字内。2 路并发会短暂增加系统总内存，已通过限制并发数控制。
- `-c:v copy` 只在没有 logo / 包装模板时启用；有叠加层时仍走重编码，输出画面语义一致但字节流与优化前不同（不再有二次编码损失）。
- `probe` 缓存按文件大小 + mtime 失效；若原片被原地覆写且大小/mtime 都没变（极少见），可调用 `ffmpegx.clear_probe_cache()` 强制失效。

## 八、画面解析优化（第二轮：单次解码 + PCM 峰值）

### 调研结论

目标不是找镜头切换点，而是找**画面内的剧烈动作**（巴掌、推搡、摔杯），因此以"镜头边界检测"为目标的方案并不直接适用：

| 方案 | 结论 |
|---|---|
| PySceneDetect | 精度好，但按帧进 Python/OpenCV，长片吞吐低；引入 opencv 依赖且与本项目"便携单文件"取向冲突 |
| FFmpeg `scdet` / `select='gt(scene,…)'` | 快，但只对硬切敏感，动作发生在同一镜头内时几乎不响应 |
| TransNetV2 等深度学习模型 | 需要 PyTorch/TF + GPU，安装包数百 MB，本地 CPU 推理比现有方法慢一个量级 |
| **FFmpeg 一次解码 + 现有 blackframe 信号 + PCM/numpy 峰值**（采用） | 不引入新依赖；画面分支逐帧结果与旧实现**完全一致**；声音分支去掉 astats 的逐块多指标统计 |

关键发现：旧实现里**声音分析比画面还慢**（45 秒素材 510ms vs 290ms），瓶颈是 `astats` 每 21ms 输出十几项指标再逐行解析，而不是解码本身。

### 实现

`engine/vision.py::_scan_signals()`：一条 `-filter_complex` 同时输出两路——
画面仍走 `fps=10 → 灰度 128 宽 → tblend → blackframe`（解析逻辑不变），声音走 `[0:a]aformat=s16:mono` 直接送 PCM 到 stdout，由 `_pcm_series()` 用 numpy 按 1024 样本一块求峰值转 dBFS。

两个必须踩准的点：

1. **保留源采样率**。最初重采样到 48kHz，而片源是 44.1kHz，块时长从 23.22ms 变 21.33ms，135 秒后时间轴累积偏移 0.9 秒，事件落点全错。改为按 `probe().sample_rate` 的 1024 样本切块后，行数与 astats 完全一致（3474/3474）。
2. **不能用 16kHz**。巴掌的峰值是毫秒级瞬态，降采样后抗混叠滤波会把尖峰削平（实测漏掉一次 impact、dB 低 7~9）。

### 性能

| 素材 | 优化前 | 优化后 | 变化 |
|---|---:|---:|---:|
| fixture 15s | 429 ms | 236 ms | **−45.0%** |
| fixture 45s | 766 ms | 392 ms | **−48.8%** |
| 真实第 1 集 81s | 2100 ms | 777 ms | **−63.0%** |
| 真实第 2 集 135s | 3557 ms | 1256 ms | **−64.7%** |
| 真实第 3 集 114s | 3010 ms | 1160 ms | **−61.5%** |
| 真实第 4 集 109s | 2844 ms | 980 ms | **−65.5%** |
| 真实 4 集合计 | 11511 ms | 4174 ms | **−63.7%** |

短素材受进程启动/滤镜初始化固定开销影响，收益小一些；正片长度（80 秒以上）稳定在 −62%~−66%。

### 一致性

- 画面帧差序列 `_diff_series` 与合并版的逐帧结果**完全相同**（805/805 行、449/449 行逐元素相等），`_motion_runs` 输出也完全相同。
- 声音 dB 序列与 astats：行数一致，均值差 1.40 dB、P95 差 3.25 dB、相关系数 **0.987**（差异来自 astats 的 RMS/Peak 归一化细节）。
- 真实 4 集事件级对比：旧事件 33 个，新实现**覆盖 32 个（97%）**，另有 5 个新增（略更灵敏），没有整类漏检。
- `test_pipeline.py` 110 项断言全部通过，9/9 用例的"同输入两次结果一致"仍然成立。

### 复现

```bat
runtime\python.exe tests\test_pipeline.py
runtime\python.exe tests\test_hooks_policy.py
```

## 九、分析链路与状态接口优化（第三轮）

### 定位方法

新增 `tests/prof_analyze.py`：对真实项目（40 集 / 4258 秒 / 607 个节拍 / 361 个动作点）分段计时 + `cProfile` + `tracemalloc`，并把优化前后的结果指纹一起存档到 `tests/results/profile_before.json` / `profile_after.json`。

### 基线瓶颈（优化前）

| 阶段 | 耗时 | 说明 |
|---|---:|---|
| `segment_beats` | 56.5 ms | 词库逐词 `in` + 118 个英文正则逐个 `search` |
| `attach_acts` | 8.1 ms | 每个节拍遍历全剧 361 个动作点（约 22 万次比较） |
| `find_highlights` | 128.3 ms | `apply_type_quotas` 每轮都全池排序；`_intimacy_score` 每次重排词表 |
| `find_endings` | 1.9 ms | — |
| **`analyze` 合计** | **188.8 ms** | |
| `/api/status` | ~145 ms | 每次都起一个 `ffmpeg -version`（实测 114ms）+ 解析最近 12 个项目 JSON |
| `/api/state`（已分析后） | 14.4 ms | FastAPI 递归编码 174KB 对象树 |

### 优化项与权衡

| # | 位置 | 改动 | 收益 | 潜在权衡 |
|---|---|---|---|---|
| 1 | `apply_type_quotas` | 全池只按分数排一次；`pool()` 找到第一条合格候选就返回（调用方本来就只用 `[0]`）；重叠判断只在同集内做 | 该函数从热点首位消失，`find_highlights` −61% | 以后若要「取前 N 条补位」需改回收集模式 |
| 2 | `_WordTrie` 替换 118 个英文正则 | 一次扫描找出所有命中（含 `contract marriage`/`marriage` 这类包含关系），仍按词库定义顺序累加 | `segment_beats` 56.5→13.3 ms | Python 逐字符下探在超长文本上优势会缩小；当前节拍文本很短 |
| 3 | `_intimacy_score` | 词表在模块加载时按长度排好；先用一次 C 正则扫首字，没命中直接返回 0 | 1224 次调用从 63ms 降到约 10ms | 极少数「含首字但不含词」的文本会多走一次完整逻辑，可忽略 |
| 4 | `attach_acts` | 动作点先按集分桶，节拍只遍历本集的 | 8.1→0.7 ms | 无 |
| 5 | `_density` | 中文字数用逐字符判断，不再 `re.findall` 建临时列表 | 小幅 | 无 |
| 6 | `classify_candidate` | 8 条 lambda 规则表改为顺序 `if` | 1255 次调用省下 lambda 创建/调用 | 规则顺序必须与表保持一致（已注释标明） |
| 7 | `ffmpegx.version()` | 结果缓存 | `/api/status` 省一次 114ms 的进程启动 | 换 ffmpeg 二进制后需重启服务 |
| 8 | `ocrsubs.available()` | 结果缓存 | 未安装组件时不再反复 import 失败 | 装好组件后需重启服务 |
| 9 | `projmod.recent()` | 按「文件名 + mtime + 大小」缓存元数据，`missing` 仍实时判断 | 省掉每次刷新解析 12 个 150KB JSON | 项目被外部改写但 mtime/大小不变时不会刷新（概率极低） |
| 10 | `/api/state` | 直接 `json.dumps` + `Response`，绕开 FastAPI 递归编码 | 14.4→3.9 ms，少一份 170KB 级对象树 | 以后若加入 datetime 等非基本类型需自行转换 |
| 11 | `/api/frame` | 抽帧改为 `_ensure_thumb()`，6 路信号量；分析完成后后台预热前 24 个候选帧 | 首屏卡片命中缓存时 2.5ms/张（冷抽 139ms） | 预热占用约 3.5 秒后台 CPU；极端并发时请求排队，但总吞吐更稳 |

### 结果

| 指标 | 优化前 | 优化后 | 变化 |
|---|---:|---:|---:|
| `segment_beats` | 56.46 ms | **13.26 ms** | **−76.5%** |
| `attach_acts` | 8.08 ms | **0.73 ms** | **−91.0%** |
| `find_highlights` | 128.29 ms | **50.37 ms** | **−60.7%** |
| `find_endings` | 1.89 ms | **0.66 ms** | −65.1% |
| `plan.build` | 2.18 ms | **1.24 ms** | −43.1% |
| **`analyze` 全量** | **188.82 ms** | **64.15 ms** | **−66.0%** |
| `/api/status` | ~145 ms | **2.0 ms** | **−98.6%** |
| `/api/state`（热） | 14.4 ms | **3.9 ms** | **−72.9%** |
| `analyze` 内存峰值 | 3.61 MB | 3.59 MB | −0.6% |
| 缩略图冷抽 | 139 ms/张 | 命中 2.5 ms/张 | — |

预热实测：清空 24 张候选帧后，后台 0.5s 生成 3 张、1s 生成 7 张、3.5s 全部 24 张——用户看到首屏时前两行卡片基本已就绪。

### 行为一致性

- **结果指纹完全一致**：优化前后 `fingerprint` 均为 `cfc92e3c94ee42a6`（40 集真实项目，含候选池、排序、方案）。
- **词库匹配等价性**：新增 `tests/test_analyze_equiv.py`，用优化前的实现做参考，对项目全部 607 条节拍 + 25 条构造样本逐条比对 `_hits` / `suspense_of` / `_intimacy_score`，8 项断言全过；包含关系词（`contract marriage`）与重复词（`inheritance` 加两次、`我回来了` 加两次）都有专门断言。
- **回归**：`test_pipeline` 110 项、`test_hooks_policy` 30 项、`test_naming` 42 项、`e2e_naming` 22 项，合计 **212 项断言全过**。
- 9/9 fixtures 用例的「同输入两次结果一致」仍然成立。

### 内存与分配

`analyze` 峰值本来就只有 3.6MB，真正的浪费在重复分配：`/api/status` 每次重建 12 个项目对象的中间树、`/api/state` 每次让 FastAPI 复制一份 170KB 对象树、`_intimacy_score` 每次重排词表。这三处都已消除；`analyze` 峰值保持在 3.59MB，没有用内存换速度。

### 复现

```bat
runtime\python.exe tests\prof_analyze.py --before   :: 需在旧版本上跑
runtime\python.exe tests\prof_analyze.py
runtime\python.exe tests\test_analyze_equiv.py
```

---

## 十、视频管线优化（第四轮：流式解码 + 画质自适应 + 容错）

### 瓶颈定位

用 `tests/prof_video.py` 造了一份 8 集 × 3 分钟 / 720×1280 / 带声音的合成素材
（TEMP 缓存，不依赖用户原片），在独立子进程里测每阶段耗时与 **PeakWorkingSetSize**：

| 阶段 | 优化前 | 说明 |
|---|---|---|
| 动作扫描（8 集 / 24 分钟） | 10.57 s / **334.6 MB** | 内存随片长线性增长 |
| 剧情分析 | 0.061 s / 24.9 MB | 上一轮已优化，保持 |
| 渲染 120 秒成片（2 段） | 10.37 s / 33.6 MB | 编码为主 |

334 MB 的来源很明确：`_scan_signals` 用 `communicate()` 把整集 PCM 收进内存，
再 `reshape → astype(float32) → abs` 复制三份。3 分钟 48k 单声道是 17 MB 原始字节，
浮点副本 35 MB、绝对值再 35 MB，4 路并发 ≈ 350 MB。解码本身不是瓶颈，
**内存和 GC 压力才是**。

### 关键改动

| 文件 / 函数 | 改动 | 收益 |
|---|---|---|
| `vision._scan_signals` | stdout PCM 改为 256 KB 分块流式读取；stderr 独立线程逐行解析；看门狗按片长杀进程 | 单路常驻 <1 MB，整机峰值 **−83.5%**；坏文件不再挂死 |
| `vision._pcm_chunk_peaks` | 按 1024 样本块求峰值，int32 防 `-32768` 溢出；不足一块的尾巴留在 carry | 与整段读结果逐值一致（测试 D） |
| `vision._audio_spikes` | 滚动中位数改为连续内存排序（`sliding_window_view.copy()` + `np.sort`） | 8400 窗 9.5 ms → **3.1 ms（3.1×）**，结果逐条一致 |
| `vision._move_threshold` | 动作阈值按本集 pblack 的 p90 自适应（`min(88, max(45, p90−15))`），`_run_dict` 按同一阈值归一 | 干净片更敏感、噪声片不误报；三档画质强度差 ≤0.3 |
| `vision._merge` | 无声音、持续 <0.35 s 的画面突变（闪白 / 闪电）不计为动作 | 减少假高光，真动作（≥3 帧或有声）保留 |
| `vision.scan_project` | 并发数按核数收敛（≤4），每路 `-threads`/`-filter_threads` 限 3；单集异常只记录不拖垮整剧 | 4K/低配机器不再线程超订；一批里坏一集仍能出结果 |
| `ffmpegx.iter_lines` | 增加超时看门狗 | 回退路径也不会永久阻塞 |
| `project.scan` | 44 集 ffprobe 改 8 路并发；读不出的文件跳过并写入 `skipped`；全坏才报错 | 实测 44 集 **2316 ms → 635 ms（3.6×）** |
| `render` | 每路 x264 限 `RENDER_THREADS`（≤8），裁剪/尾帧/包装统一 | 2 路并发不再互相抢 16 个核；120 s 成片 10.37 → 10.26 s |
| `analyze.find_visual_highlights` | 视觉候选门槛按本剧事件分 p60 自适应，低音量片源不再一个画面钩子都出不来 | 召回提升，正常片源门槛不变 |
| `multimodal._audio_envelope` | 8k PCM 也改流式，256 KB 块算 RMS/峰值 | 3 路并发少占 ~36 MB |
| `app._thumb_key` | 缩略图 key 加入源文件路径哈希 | 修掉「两部剧都有 01.mp4 时缩略图串剧」的错图问题 |

### 指标（同一台 16 核机器，前后各跑一次 `prof_video`）

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 扫描 24 分钟素材 · 峰值内存 | 334.6 MB | **55.1 MB** | **−83.5%** |
| 扫描耗时 | 10.57 s | 10.37 s | −1.9%（解码受限，波动 ±5%） |
| 扫描吞吐 | — | 24 分钟 / 10.4 s ≈ **139× 实时** | — |
| 音频峰值检测（8400 窗） | 9.5 ms | **3.1 ms** | **−67%** |
| 导入 44 集探测 | 2316 ms | **635 ms** | **3.6×** |
| 渲染 120 s / 360×640 | 10.37 s / 33.6 MB | 10.26 s / 33.7 MB | 持平 |
| 剧情分析 | 61 ms / 24.9 MB | 63 ms / 26.0 MB | 持平 |

> 扫描耗时没有大幅下降是预期的：这一阶段 90% 的时间在 ffmpeg 解码 720p 全帧，
> Python 侧原本只占很小一块；本轮的收益主要是**内存、稳定性、容错**，
> 以及不再因为某一集坏掉而整批失败。

### 识别质量与画质稳定性

`tests/test_vision_robust.py`（32 项）用合成 pblack / dB 序列 + 真实短视频覆盖：

- **三档画质**（静态基线 98 / 90 / 84）都能抓到同一段 0.6 s 动作，强度差 ≤0.3；
  整集稳定噪声（p90≈88）不产生任何动作段。
- **闪白/闪电**：2 帧无声音突变被过滤；同一段带声音冲击则保留并计入分值。
- **低音量片源**：全程 −55 dB 的录音里，−22 dB、3 个窗的脆响仍能被定位（±0.1 s），
  全程安静不误报。
- **流式 PCM**：整段读 vs 任意字节切分（含奇数长度）块数、时间戳、dB 值完全一致。
- **失败隔离**：3 集里 1 集抛错，另 2 集照常产出，失败集写进 `failed`。
- **导入容错**：2 个正常文件 + 1 个假 mp4 → 导入 2 集、`skipped` 记录 1 个；全坏才报错。

钩子层回归（`test_hook_recall` 10 项 / `test_hook_pool` 22 项 / `test_hooks_policy` 30 项）
指标不变：20 集标注集召回 100%、类型标注 100%、起点对齐 100%。

### 回归验证步骤

```bat
:: 1. 基线 / 优化后性能（各约 20 秒，写 tests/results/video_perf_*.json）
runtime\python.exe tests\prof_video.py before
runtime\python.exe tests\prof_video.py after
runtime\python.exe tests\prof_video.py compare

:: 2. 信号层与容错
runtime\python.exe tests\test_vision_robust.py      :: 32 项
runtime\python.exe tests\test_multimodal.py         :: 35 项

:: 3. 钩子与方案
runtime\python.exe tests\test_hook_recall.py        :: 10 项
runtime\python.exe tests\test_hook_pool.py          :: 22 项
runtime\python.exe tests\test_hooks_policy.py       :: 30 项

:: 4. 全链路
runtime\python.exe tests\test_pipeline.py           :: 110 项
runtime\python.exe tests\bench.py --tag v2 --repeat 1
```

判定标准：`prof_video compare` 中 `scan.peak_mb` 必须显著低于基线；
`test_vision_robust` / `test_pipeline` / 钩子三套 0 失败；
`bench` 每个用例 `stable=true`（同一输入多次结果指纹一致）。

### 已知限制

- 解码仍是扫描的主要成本；未启用硬件解码（不同机器驱动差异大，便携包不做默认）。
- 自动混剪的深度识别仍是「画面 + 声音」两次解码，合并需要双管道，收益小于风险，暂缓。
- 4K / 长剧（单集 >10 分钟）未在真实素材上压测；超时按 8× 片长、上限 900 s 保护。

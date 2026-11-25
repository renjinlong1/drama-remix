# 顺剪工作台（drama-remix）

本地运行的短剧顺剪 / 混剪工具：导入整剧后自动分集、配对字幕、识别画面动作与高光钩子，
再按目标时长顺剪出片。全程离线，不上传素材。

## 功能

- **整剧导入**：分集、字幕配对（srt / ass / ssa / vtt）、无字幕集画面硬字幕 OCR。
- **高光钩子**：默认最多 100 条候选，按冲突 / 动作 / 反转 / 悬念分类；支持上限调整与场景簇换批。
- **顺剪**：从选定钩子往后顺剪到目标时长，可挖掉指定台词、自动裁片尾定帧；结尾默认停在**没讲完的悬念卡点**（未答问题 / 未落动作 / 将揭身份），不强制集末或完整收束。
- **自动混剪**：按主题、目标时长与节奏挑片段。
- **高光剪辑**：整剧扫描高光，按目标时长决定片段数；每段去尾帧，结尾可自动 / 指定 / 自定义；默认 10 分钟、400 MB 上限。
- **导出**：命名模板、logo / 模板角标、引导尾帧、批量多档产出。
- **性能**：两段式导入（时间轴先出、动作扫描后台补）、8 路动作扫描、按素材文件夹隔离的增量缓存、硬件解码自动探测与回退。

## 快速开始

### 便携包（推荐）

发布包已带 `runtime/` 与 `bin/ffmpeg.exe`，双击 `启动.bat`，浏览器打开 `http://127.0.0.1:8780`。

### 从源码运行

```bat
安装依赖.bat
开发启动.bat
```

开发模式端口为 `8790`，接口文档在 `http://127.0.0.1:8790/docs`。

依赖：Python 3.13、FastAPI、Uvicorn、Pillow、python-multipart、rapidocr-onnxruntime；
另需把 `ffmpeg.exe` / `ffprobe.exe` 放到 `bin/`。

## 目录

| 目录 | 作用 |
|---|---|
| `app.py` | FastAPI 服务与任务接口 |
| `engine/` | 字幕、OCR、节拍分析、钩子、顺剪规划、混剪、高光、渲染、命名、硬件解码 |
| `web/` | 单页前端（原生 JS / Canvas 时间轴） |
| `tests/` | 可复现的回归与性能基准 |
| `docs/` | 界面截图 |
| `使用说明.md` | 完整使用说明 |

`runtime/`、`bin/`、`projects/`、`data/`、`output/`、`samples/` 与测试样片不进仓库（见 `.gitignore`）。

## 测试

```bat
runtime\python.exe tests\test_scan_phases.py
runtime\python.exe tests\test_pipeline.py
runtime\python.exe tests\test_hwaccel.py
runtime\python.exe tests\prof_scan.py
```

完整套件与性能数据见 `tests/PERFORMANCE_REPORT.md`。

## 说明

- 素材版权归原权利人所有，本工具只做本地剪辑处理。
- 仓库未声明开源许可证；如需复用代码，请先联系作者。

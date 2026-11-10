"""枫的瞬剪 · 便携发布打包。

从当前目录生成一份「干净的绿色包」：源码 + 运行时 + ffmpeg + 说明文档，
不带项目存档 / OCR 缓存 / 上传素材 / 成片 / Git 元数据。

用法：
    runtime\\python.exe 打包发布.py            # 生成 zip 到 发布/
    runtime\\python.exe 打包发布.py --check   # 只列清单和体积，不写文件
文件名带 git describe 出来的版本标记，方便追溯这一包对应哪次提交。
"""
from __future__ import annotations

import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "发布"
CHECK = "--check" in sys.argv

# 完整打包的目录（运行时与二进制不在 git 里，但发布包必须带）
DIRS = ("engine", "web", "bin", "runtime", "docs")
# 根目录里带上的文件
FILES = ("app.py", "启动.bat", "安装依赖.bat", "使用说明.md", "新电脑使用说明.txt")
# zip 里保留空目录结构，用户解压后不用手动建
EMPTY_DIRS = ("projects", "data", "data/thumbs", "data/uploads", "output")
SKIP_PARTS = {"__pycache__", ".git", "发布", ".idea", ".vscode"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".zip", ".tmp"}


def git_version() -> str:
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
        v = (r.stdout or "").strip()
        return (v or "nogit").replace("/", "-").replace(":", "-")
    except Exception:
        return "nogit"


def skip(rel: Path) -> bool:
    if any(p in SKIP_PARTS for p in rel.parts):
        return True
    if rel.suffix.lower() in SKIP_SUFFIX:
        return True
    return rel.name.endswith(".json.bak")


def collect() -> list[Path]:
    out: list[Path] = []
    for d in DIRS:
        base = ROOT / d
        if not base.is_dir():
            print(f"  [警告] 缺少目录 {d}/，发布包会跑不起来")
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and not skip(p.relative_to(ROOT)):
                out.append(p)
    for f in FILES:
        p = ROOT / f
        if p.is_file():
            out.append(p)
        else:
            print(f"  [警告] 缺少文件 {f}")
    return out


def main() -> None:
    ver = git_version()
    entries = collect()
    total = sum(p.stat().st_size for p in entries)
    print(f"  枫的瞬剪 · 发布打包")
    print(f"  版本标记 : {ver}")
    print(f"  文件数量 : {len(entries)}")
    print(f"  原始体积 : {total / 1024 / 1024:.1f} MB")
    if CHECK:
        print("  --check 模式：只列清单，未生成文件")
        for p in entries[:8]:
            print("    ", p.relative_to(ROOT))
        if len(entries) > 8:
            print(f"     ... 其余 {len(entries) - 8} 个")
        return

    OUT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    out = OUT_DIR / f"枫的瞬剪_{stamp}_{ver}.zip"
    info = (
        "枫的瞬剪 · 便携发布包\n"
        f"版本标记: {ver}\n"
        f"打包时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"文件数量: {len(entries)}\n"
        "说明: 本包不含任何项目存档、OCR 字幕缓存、上传素材和成片。\n"
        "升级已有安装时只覆盖 app.py / engine/ / web/ / *.bat，不要覆盖 projects / data / output。\n"
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in EMPTY_DIRS:
            z.writestr(d + "/", b"")
        z.writestr("BUILD_INFO.txt", info.encode("utf-8"))
        for i, p in enumerate(entries, 1):
            z.write(p, arcname=str(p.relative_to(ROOT)).replace("\\", "/"))
            if i % 400 == 0:
                print(f"  已写入 {i}/{len(entries)} ...")
    print(f"  完成: {out}")
    print(f"  压缩包: {out.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()

@echo off
title 枫的瞬剪 · 安装依赖
cd /d "%~dp0"
set PYTHONIOENCODING=gbk
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [!] 没有找到 Python
  pause
  exit /b 1
)
echo 使用国内镜像安装依赖（首次约 1-3 分钟）...
"%PY%" -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
"%PY%" -m pip install fastapi uvicorn pillow python-multipart rapidocr-onnxruntime -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
echo.
echo 完成。双击「启动.bat」即可使用。
pause

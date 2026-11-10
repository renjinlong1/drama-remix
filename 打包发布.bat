@echo off
title 枫的瞬剪 · 打包发布
cd /d "%~dp0"
rem 由「打包发布.py」在本目录 发布\ 下生成干净的便携 zip
rem 只看清单不写文件：runtime\python.exe 打包发布.py --check
set PYTHONIOENCODING=gbk
if not exist "%~dp0runtime\python.exe" (
  echo   [!] 缺少 runtime\python.exe
  pause
  exit /b 1
)
"%~dp0runtime\python.exe" "%~dp0打包发布.py"
echo.
pause

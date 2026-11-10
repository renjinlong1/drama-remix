@echo off
title 顺剪工作台
cd /d "%~dp0"
set PYTHONIOENCODING=gbk
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not defined PY call :probe "python"
if not defined PY (
  for %%D in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "C:/Python313/python.exe" "C:/Python312/python.exe") do (
    if exist "%%~D" call :probe "%%~D"
  )
)
if not defined PY (
  echo.
  echo   [!] 没有找到 Python，请先安装 Python 3.10 以上版本
  echo       下载：https://www.python.org/downloads/  安装时勾选 Add to PATH
  echo.
  pause
  exit /b 1
)
"%PY%" -c "import fastapi, uvicorn, PIL" 2>nul
if errorlevel 1 call "%~dp0安装依赖.bat"
echo.
echo   启动中……浏览器会自动打开 http://127.0.0.1:8780
echo   关闭这个窗口就退出程序
echo.
"%PY%" app.py
if errorlevel 1 pause
goto :eof

:probe
rem 只有真能跑起来才算数（Microsoft Store 的 python 是空壳，打印提示就退出）
if not exist %1 goto :eof
%1 -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

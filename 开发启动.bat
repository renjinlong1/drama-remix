@echo off
title 枫的瞬剪 · 开发模式
cd /d "%~dp0"
rem ------------------------------------------------------------
rem  开发模式（日常出片请双击「启动.bat」）
rem    · 端口 8790、不自动弹浏览器、请求日志全开
rem    · 改 Python 代码：关掉本窗口重跑
rem    · 改 web/ 前端：浏览器 Ctrl+Shift+R 强刷即可，不用重启
rem    · 接口调试：http://127.0.0.1:8790/docs
rem    · 代码入口：app.py(接口) engine/(算法) web/app.js(界面)
rem ------------------------------------------------------------
set PYTHONIOENCODING=gbk
set DC_PORT=8790
set DC_NO_BROWSER=1
set DC_LOG=info
if not exist "%~dp0runtime\python.exe" (
  echo   [!] 缺少 runtime\python.exe，请从原便携包拷回本目录
  pause
  exit /b 1
)
echo.
echo   枫的瞬剪 · 开发模式   http://127.0.0.1:8790/docs
echo   改完 Python 重跑本窗口；改完前端按 Ctrl+Shift+R
echo   关闭此窗口即退出
echo.
"%~dp0runtime\python.exe" app.py
if errorlevel 1 pause

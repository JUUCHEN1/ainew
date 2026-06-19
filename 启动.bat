@echo off
chcp 65001 >nul
REM ============================================================
REM  漫创AI Web · 一键启动 (Windows)
REM  双击本文件即可同时启动后端和前端，并自动打开浏览器。
REM ============================================================
cd /d "%~dp0"

REM 优先使用项目内置虚拟环境，其次系统 python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [libai-web] 使用 Python: %PY%
"%PY%" run_all.py

pause

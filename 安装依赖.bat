@echo off
chcp 65001 >nul
REM ============================================================
REM  漫创AI Web · 安装 Python 依赖 (Windows，首次运行一次即可)
REM  会在项目内创建 .venv 虚拟环境并安装 requirements.txt
REM ============================================================
cd /d "%~dp0"

echo [libai-web] 创建虚拟环境 .venv ...
python -m venv .venv
if errorlevel 1 (
  echo [libai-web] 创建虚拟环境失败，请确认已安装 Python 3.10+ 并加入 PATH
  pause
  exit /b 1
)

echo [libai-web] 安装依赖 ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo [libai-web] 依赖安装完成。之后双击 "启动.bat" 即可运行。
pause

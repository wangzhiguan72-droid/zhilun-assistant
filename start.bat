@echo off
REM 智论助手 MVP 启动脚本（Windows）
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 找不到 .venv\Scripts\python.exe，请先创建虚拟环境。
    echo        参考 README.md 中的"启动方式"小节。
    pause
    exit /b 1
)
echo [智论助手] 启动 Flask 服务（默认 http://127.0.0.1:5000）
echo           数据分析 tab + 论文排查 tab 都在这里。
echo.
.venv\Scripts\python.exe app.py
pause
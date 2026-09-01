@echo off
rem 一键启动 dashboard + 决策循环（独立于 AI 会话，不会被回收）
chcp 65001 >nul
cd /d "%~dp0.."
echo [MAT] 启动 multi-agent-trader 服务 (http://127.0.0.1:8000) ...
echo [MAT] 日志输出到 data\web_console.log，停止请关闭本窗口或按 Ctrl+C
PYTHONIOENCODING=utf-8 .venv\Scripts\python.exe scripts\run_web.py >> data\web_console.log 2>&1

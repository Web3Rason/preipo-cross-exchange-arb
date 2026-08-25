@echo off
cd /d "%~dp0"

:: 清掉還佔著 5032 的舊程序
for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":5032 .*LISTENING"') do (
    taskkill /F /PID %%A >nul 2>&1
)

:: 透過 Python launcher 啟動（父 batch 結束後仍存活）
python launcher.py

@echo off
REM PreIPO 下架巡查 — 排程觸發入口（%~dp0 = 這支 .bat 所在目錄，.. = 專案根）
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
python scripts\check_delistings.py >> logs\delisting_check.log 2>&1

@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONWARNINGS=ignore:urllib3
set PYTHONPATH=E:\project\daily-checkin
set DAILY_CHECKIN_DB=E:\project\daily-checkin\data\system.db
set DAILY_CHECKIN_TASKS_DIR=E:\project\daily-checkin\data\tasks
set DAILY_CHECKIN_CREDENTIALS_FILE=E:\project\daily-checkin\data\credentials.json
set DAILY_CHECKIN_ACCOUNTS_FILE=E:\project\daily-checkin\data\accounts.md
set DAILY_CHECKIN_CREDENTIAL_BACKEND=file
cd /d E:\project\daily-checkin
python stealth_checkin_runner.py --cdp 9222 %*
pause

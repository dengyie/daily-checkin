@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONWARNINGS=ignore:urllib3
powershell -ExecutionPolicy Bypass -File E:\project\daily-checkin\scripts\start-all-win.ps1

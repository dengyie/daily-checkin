# Start both API (8765) and Frontend (8766) in separate background processes
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore:urllib3"
$env:PYTHONPATH = "E:\project\daily-checkin"
$env:DAILY_CHECKIN_DB = "E:\project\daily-checkin\data\system.db"
$env:DAILY_CHECKIN_TASKS_DIR = "E:\project\daily-checkin\data\tasks"

Set-Location "E:\project\daily-checkin"

# Check if 8765 already running
$p8765 = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $p8765) {
    Start-Process powershell -ArgumentList "-NoExit", "-File", "E:\project\daily-checkin\scripts\start-api-win.ps1" -WindowStyle Minimized
    Write-Host "API (8765) process launched." -ForegroundColor Green
} else {
    Write-Host "API (8765) is already running." -ForegroundColor Yellow
}

# Check if 8766 already running
$p8766 = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if (-not $p8766) {
    Start-Process powershell -ArgumentList "-NoExit", "-File", "E:\project\daily-checkin\scripts\start-web-win.ps1" -WindowStyle Minimized
    Write-Host "Web UI (8766) process launched." -ForegroundColor Green
} else {
    Write-Host "Web UI (8766) is already running." -ForegroundColor Yellow
}

Write-Host "Daily Check-in Web: http://127.0.0.1:8766" -ForegroundColor Cyan

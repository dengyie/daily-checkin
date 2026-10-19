# Start daily-checkin Frontend Web UI (port 8766) on Windows
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore:urllib3"
$env:PYTHONPATH = "E:\project\daily-checkin"
Set-Location "E:\project\daily-checkin"
Write-Host "Starting daily-checkin Frontend Web UI on http://127.0.0.1:8766..." -ForegroundColor Green
python scripts\serve-frontend.py --host 127.0.0.1 --port 8766

# Start daily-checkin API backend (port 8765) on Windows
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore:urllib3"
$env:PYTHONPATH = "E:\project\daily-checkin"
$env:DAILY_CHECKIN_DB = "E:\project\daily-checkin\data\system.db"
$env:DAILY_CHECKIN_TASKS_DIR = "E:\project\daily-checkin\data\tasks"
$env:DAILY_CHECKIN_CREDENTIALS_FILE = "E:\project\daily-checkin\data\credentials.json"
$env:DAILY_CHECKIN_ACCOUNTS_FILE = "E:\project\daily-checkin\data\accounts.md"
$env:DAILY_CHECKIN_CREDENTIAL_BACKEND = "file"
# Optional Web UI access password gate: set in user environment or leave empty for local development
if (-not $env:DAILY_CHECKIN_WEB_PASSWORD) {
    # $env:DAILY_CHECKIN_WEB_PASSWORD = ""
}

# 后台启动：pythonw 无控制台窗口，脚本立即返回，不占用调用方终端
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "daily-checkin API 8765 already listening; skip." -ForegroundColor Yellow
    exit 0
}

Start-Process -FilePath "E:\code\conda\pythonw.exe" `
    -ArgumentList @("E:\project\daily-checkin\scripts\launch-bg.pyw", "api") `
    -WorkingDirectory "E:\project\daily-checkin" `
    -WindowStyle Hidden

Write-Host "daily-checkin API backend launched in background on http://127.0.0.1:8765 (log: data\api.log)" -ForegroundColor Green


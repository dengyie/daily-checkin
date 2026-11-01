# Start daily-checkin Frontend Web UI (port 8766) on Windows
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore:urllib3"
$env:PYTHONPATH = "E:\project\daily-checkin"
# 后台启动：pythonw 无控制台窗口，脚本立即返回，不占用调用方终端
if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "daily-checkin Web UI 8766 already listening; skip." -ForegroundColor Yellow
    exit 0
}

Start-Process -FilePath "E:\code\conda\pythonw.exe" `
    -ArgumentList @("E:\project\daily-checkin\scripts\launch-bg.pyw", "web") `
    -WorkingDirectory "E:\project\daily-checkin" `
    -WindowStyle Hidden

Write-Host "daily-checkin Frontend Web UI launched in background on http://127.0.0.1:8766 (log: data\web.log)" -ForegroundColor Green

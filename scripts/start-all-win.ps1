# Executor-only node entrypoint (no local frontend).
# 2026-11-09 consolidation: the panel (check.mangoqwq.com) is the ONLY
# frontend. Windows keeps just the check-in runner + dispatch agent; the old
# local API/UI tasks (DailyCheckinAPI8765 / DailyCheckinWeb8766) are disabled.
# This script is the manual bootstrap after a reboot or agent restart.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore:urllib3"

Set-Location "E:\project\daily-checkin"

$agent = Get-NetTCPConnection -LocalPort 9777 -State Listen -ErrorAction SilentlyContinue
if (-not $agent) {
    Start-Process -FilePath "wscript.exe" -ArgumentList "//B", "E:\project\daily-checkin\scripts\start-dispatch-agent-win-hidden.vbs" -WindowStyle Hidden
    Write-Host "Dispatch agent launched (127.0.0.1:9777)." -ForegroundColor Green
} else {
    Write-Host "Dispatch agent already listening on 9777." -ForegroundColor Yellow
}

Write-Host "Unified panel: https://check.mangoqwq.com" -ForegroundColor Cyan
Write-Host "Local UI 8765/8766 is retired; use the panel for all viewing/actions." -ForegroundColor DarkGray

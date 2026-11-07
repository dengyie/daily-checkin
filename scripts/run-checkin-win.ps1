# Run daily checkin on Windows 9222 CDP
param(
    [string]$Only = "",
    [switch]$DryRun
)

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

Set-Location "E:\project\daily-checkin"

$argsList = @("stealth_checkin_runner.py", "--cdp", "9222")
if ($Only) {
    $argsList += @("--only", $Only)
}
if ($DryRun) {
    $argsList += @("--dry-run")
}

Write-Host "Starting Windows daily-checkin runner..." -ForegroundColor Cyan
python @argsList
$rc = $LASTEXITCODE

# 2026-11-06 多节点: 晨批结束后把本节点权威库原子同步到面板副本（保鲜），
# 使 win 站点的任务状态在 check.mangoqwq.com 统一清单中可见。失败不致命。
$env:DAILY_CHECKIN_SYNC_SSH_ALIAS = "pxed-direct"
$env:DAILY_CHECKIN_SYNC_REMOTE_PATH = "/data/daily-checkin/data/system-win.db"
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\project\daily-checkin\scripts\sync-db-to-panel.ps1"

exit $rc

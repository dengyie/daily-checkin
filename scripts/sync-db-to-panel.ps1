# Per-node authoritative-DB sync: Windows executor -> panel replica (atomic).
# Windows twin of scripts/sync-db-to-pxed.sh — uploads to a temp name and the
# panel host renames it into place, so panel readers never see a torn DB.
# Env (set by launch-dispatch-agent-win.ps1 / deployment):
#   DAILY_CHECKIN_DB                local authoritative DB
#   DAILY_CHECKIN_SYNC_SSH_ALIAS    panel ssh alias (default pxed-direct)
#   DAILY_CHECKIN_SYNC_REMOTE_PATH  replica path (default system-win.db)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$Db          = $env:DAILY_CHECKIN_DB
$SshAlias    = if ($env:DAILY_CHECKIN_SYNC_SSH_ALIAS) { $env:DAILY_CHECKIN_SYNC_SSH_ALIAS } else { "pxed-direct" }
$RemotePath  = if ($env:DAILY_CHECKIN_SYNC_REMOTE_PATH) { $env:DAILY_CHECKIN_SYNC_REMOTE_PATH } else { "/data/daily-checkin/data/system-win.db" }
$RemoteDir   = ($RemotePath -replace '/[^/]+$', '')
$RemoteTmp   = "$RemoteDir/.system.db.incoming"

if (-not $Db -or -not (Test-Path $Db)) {
    Write-Host "sync-db-to-panel: DAILY_CHECKIN_DB not set or missing, skip"
    exit 0
}

# Flush WAL into the main DB. Use python (always present on the node; the
# sqlite3 CLI often is not) so the scp of a single file carries all state.
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $env:SYNC_DB_PATH = $Db
    & python -c "import os, sqlite3; sqlite3.connect(os.environ['SYNC_DB_PATH']).execute('PRAGMA wal_checkpoint(TRUNCATE)')" 2>$null
}

scp -q -o ConnectTimeout=10 -o StrictHostKeyChecking=no $Db "${SshAlias}:${RemoteTmp}"
if ($LASTEXITCODE -ne 0) { Write-Host "sync-db-to-panel: scp failed"; exit 1 }

ssh -q -o ConnectTimeout=10 -o StrictHostKeyChecking=no $SshAlias "mv ${RemoteTmp} ${RemotePath}"
if ($LASTEXITCODE -ne 0) { Write-Host "sync-db-to-panel: remote mv failed"; exit 1 }

Write-Host "sync-db-to-panel: ok -> ${SshAlias}:${RemotePath}"
exit 0

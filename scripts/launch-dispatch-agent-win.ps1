# Dispatch agent launcher for Windows executor nodes (2026-11-06 multi-node).
# Mirrors scripts/run-checkin-win.ps1 conventions: inline env, E:\ project root,
# UTF-8 console. Run hidden at logon via start-dispatch-agent-win-hidden.vbs +
# a Scheduled Task (DailyCheckinDispatchAgent).
#
# Prerequisites (one-time, see docs/dispatch-test-plan.md L17):
#   1. ssh key authorized on the panel host for the reverse tunnel + sync
#      (ssh-keygen -t ed25519, then append pubkey to panel's authorized_keys).
#   2. ~/.ssh/config alias "pxed-direct" pointing at the panel host.
#   3. DAILY_CHECKIN_DISPATCH_TOKEN exported below (from panel add-node win).

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore:urllib3"
$env:PYTHONPATH = "E:\project\daily-checkin"

# --- executor-node authoritative DB (independent from the Mac node) ---
$env:DAILY_CHECKIN_DB = "E:\project\daily-checkin\data\system.db"
$env:DAILY_CHECKIN_TASKS_DIR = "E:\project\daily-checkin\data\tasks"
$env:DAILY_CHECKIN_CREDENTIALS_FILE = "E:\project\daily-checkin\data\credentials.json"
$env:DAILY_CHECKIN_CREDENTIAL_BACKEND = "file"
$env:DAILY_CHECKIN_LOG_DIR = "E:\project\daily-checkin\log"

# --- dispatch agent wiring (panel pushes jobs here via the ssh -R tunnel) ---
if (-not $env:DAILY_CHECKIN_DISPATCH_TOKEN) {
    # Persisted in the node's user env by deployment; never hardcode here.
    $env:DAILY_CHECKIN_DISPATCH_TOKEN = [Environment]::GetEnvironmentVariable("DAILY_CHECKIN_DISPATCH_TOKEN", "User")
}
if (-not $env:DAILY_CHECKIN_DISPATCH_TOKEN) {
    Write-Host "FATAL: DAILY_CHECKIN_DISPATCH_TOKEN not set (user env or current session)" -ForegroundColor Red
    exit 2
}
$env:DAILY_CHECKIN_DISPATCH_CALLBACK_URL = "https://check-api.mangoqwq.com"
$env:DAILY_CHECKIN_DISPATCH_BIND = "127.0.0.1"
$env:DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS = "pxed-direct"
$env:DAILY_CHECKIN_DISPATCH_TUNNEL_REMOTE_PORT = "19778"   # per-node tunnel port on the panel host
# Node-side proxies (clash/sing-box) intermittently kill outbound TLS to the
# panel's public domain — route result/heartbeat callbacks through a second
# ssh -L loopback tunnel to the panel API instead (agent rewrites its
# callback URL automatically when this alias is set).
$env:DAILY_CHECKIN_CALLBACK_TUNNEL_ALIAS = "pxed-direct"
$env:DAILY_CHECKIN_CALLBACK_TUNNEL_LOCAL_PORT = "19780"
# Windows morning batch (DailyCheckinRun) also fires at 08:10 -> same quiet window.
$env:DAILY_CHECKIN_AGENT_QUIET_WINDOW = "07:30-08:45"
# Strict attach-only (P0): dispatched runs MUST attach to the watchdog-managed
# Chrome on 9222 — never auto-launch a second Chrome. When CDP is down the
# runner fails fast with a clear cdp_down attribution instead of wandering.
$env:CDP_HTTP = "http://127.0.0.1:9222"
$env:DAILY_CHECKIN_AGENT_CDP = "http://127.0.0.1:9222"
$env:DAILY_CHECKIN_AGENT_LOG_FILE = "E:\project\daily-checkin\log\dispatch-agent.log"

# --- per-node replica sync target (panel displays task rows per node later) ---
$env:DAILY_CHECKIN_SYNC_SSH_ALIAS = "pxed-direct"
$env:DAILY_CHECKIN_SYNC_REMOTE_PATH = "/data/daily-checkin/data/system-win.db"

Set-Location "E:\project\daily-checkin"

# Resident-agent visibility: agent prints land in this transcript (no console
# window; wscript hides everything). Without it, agent diagnostics are lost.
New-Item -ItemType Directory -Force -Path "E:\project\daily-checkin\log" | Out-Null
Start-Transcript -Path "E:\project\daily-checkin\log\dispatch-agent.transcript.log" -Append | Out-Null

Write-Host "Starting Windows dispatch agent (multi-node executor)..." -ForegroundColor Cyan
try {
    python -m checkin_core.dispatch_agent
} finally {
    Stop-Transcript | Out-Null
}

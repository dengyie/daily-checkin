# Run daily-checkin Doctor self-check
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
python stealth_checkin_runner.py --doctor

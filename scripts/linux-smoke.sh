#!/usr/bin/env bash
# Linux portability smoke for daily-checkin (runs on GitHub Actions ubuntu, and
# on other headless Linux hosts). Verifies the pieces that differ from macOS:
#   - Python byte-compiles on Linux (no macOS-only syntax/imports)
#   - playwright + PyYAML import (runner's only hard deps)
#   - keychain module: file backend is the default on non-darwin; no silent
#     fallback (unknown backend raises); web factory returns a file store
#   - supervisor config syntax is parseable
#   - SQLite WAL works on the local filesystem (a standalone lock/flock sanity)
#
# This deliberately does NOT touch any real site or a browser; no credentials
# are exercised beyond the 0600-file unit tests already in the test suite.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==[1/5] py_compile =="
python3 -m py_compile \
  stealth_checkin_runner.py \
  scripts/daily-checkin-cron.py \
  checkin_core/*.py \
  checkin_core/providers/*.py

echo "==[2/5] import deps =="
python3 -c "import playwright, yaml; from importlib.metadata import version; print('playwright', version('playwright')); print('pyyaml', version('PyYAML'))"

echo "==[3/5] credential backend selection =="
# On Linux the default backend must be the file store; an unknown backend must
# fail loudly instead of falling back to plaintext. Secrets never print.
ACTUAL=$(python3 - <<'PY'
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
td = tempfile.mkdtemp()
os.environ["DAILY_CHECKIN_CREDENTIALS_DIR"] = td
from checkin_core.keychain import credential_store_from_env
s = credential_store_from_env()
print(type(s).__name__)
PY
)
if [ "$ACTUAL" != "FileCredentials" ]; then
  echo "FAIL: default backend on Linux is $ACTUAL, want FileCredentials" >&2; exit 1
fi
echo "default backend = $ACTUAL (ok)"

if python3 -c "import os; os.environ['DAILY_CHECKIN_CREDENTIAL_BACKEND']='bogus'; from checkin_core.keychain import credential_store_from_env; credential_store_from_env()" 2>/dev/null; then
  echo "FAIL: unknown credential backend did not raise" >&2; exit 1
fi
echo "unknown backend raises (ok)"

echo "==[4/6] supervisor config syntax =="
# Structural parse of the deployment config with configparser (always runs —
# supervisor may not be installed on a bare CI runner). Also run the fixture
# through supervisorctl when present; a connection-refused (no running
# supervisor) proves the config PARSED, while a *.conf parse error is a real
# regression. Error lines mentioning 'conf'/'parse' mark a broken config.
python3 - <<'PY'
import configparser, sys
cfg = configparser.RawConfigParser()
if not cfg.read("scripts/supervisor-test.conf"):
    print("FAIL: supervisor-test.conf not readable"); sys.exit(1)
need = {"command", "directory", "autostart", "autorestart", "stopasgroup", "killasgroup", "stdout_logfile", "stderr_logfile"}
for sec in cfg.sections():
    if not sec.startswith("program:"):
        continue
    missing = need - set(cfg.options(sec))
    if missing:
        print(f"FAIL: {sec} missing keys {sorted(missing)}"); sys.exit(1)
    cmd = cfg.get(sec, "command").strip()
    if not cmd.startswith("/"):
        print(f"FAIL: {sec} command is not absolute: {cmd!r}"); sys.exit(1)
print("configparser structural check ok")
PY
if command -v supervisorctl >/dev/null 2>&1; then
  out="$(supervisorctl -c scripts/supervisor-test.conf status 2>&1 || true)"
  if printf '%s' "$out" | grep -qiE "\.conf.*(?:error|invalid|unrecognized)|parse error"; then
    echo "FAIL: supervisor parsed the config as invalid:"; printf '%s\n' "$out"; exit 1
  fi
  echo "supervisorctl present; config parse-level ok"
else
  echo "supervisorctl not installed; structural parse above suffices"
fi

echo "==[6/6] sqlite WAL + local flock sanity =="
python3 - <<'PY'
import os, tempfile, fcntl, sqlite3
from pathlib import Path
td = Path(tempfile.mkdtemp())
db = td / "smoke.db"
conn = sqlite3.connect(str(db))
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE t(x)")
conn.commit(); conn.close()
fd = os.open(td / "smoke.lock", os.O_CREAT | os.O_RDWR, 0o600)
# Non-blocking exclusive flock must succeed on a local dir (this would fail if
# cwd were a filesystem with no flock, e.g. some NFS setups).
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as e:
    print("FAIL: flock unavailable:", e); raise SystemExit(1)
fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
print("sqlite WAL + flock ok")
PY

echo "ALL LINUX SMOKE CHECKS PASS"
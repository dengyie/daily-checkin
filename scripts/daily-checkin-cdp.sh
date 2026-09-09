#!/bin/bash
# Hermes pre-run entry. Install as a regular file in ~/.hermes/scripts/.
# It loads local paths, then execs the Python cron entry so Hermes owns the
# actual runner process and can terminate it without leaving an orphan child.
set -uo pipefail

LOCAL_ENV="${DAILY_CHECKIN_ENV_FILE:-$HOME/.config/daily-checkin/env}"
if [[ -f "$LOCAL_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
  set +a
fi

ROOT="${DAILY_CHECKIN_ROOT:-}"
LOG_DIR="${DAILY_CHECKIN_LOG_DIR:-$HOME/.hermes/checkin}"
mkdir -p "$LOG_DIR"

write_wrapper_status() {
  local exit_code="$1" reason="$2"
  python3 - "$LOG_DIR" "$exit_code" "$reason" <<'PYEOF' 2>/dev/null || true
import datetime, json, os, pathlib, sys
root, exit_code, reason = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
root.mkdir(parents=True, exist_ok=True)
payload = {
    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    "exit": exit_code, "reason": reason, "source": "cron", "mode": "business",
    "cdp": {"http": None, "headless": None, "pid": None, "port": None},
    "ok": 0, "already": 0, "fail": 0, "total": 0, "fail_sites": [],
    "evidence_counts": {}, "attribution_counts": {}, "failure_counts": {},
}
target = root / "last-attempt.json"
tmp = target.with_name(target.name + ".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
os.replace(tmp, target)
PYEOF
}

if [[ -z "$ROOT" ]]; then
  echo "FATAL: DAILY_CHECKIN_ROOT is not configured" >&2
  write_wrapper_status 2 no_root
  echo '{"wakeAgent": true, "exit": 2, "reason": "no_root"}'
  exit 2
fi

PY="$ROOT/.venv/bin/python"
ENTRY="$ROOT/scripts/daily-checkin-cron.py"
if [[ ! -x "$PY" ]]; then
  echo "FATAL: venv python missing: $PY" >&2
  write_wrapper_status 2 no_venv
  echo '{"wakeAgent": true, "exit": 2, "reason": "no_venv"}'
  exit 2
fi
if [[ ! -f "$ENTRY" ]]; then
  echo "FATAL: cron entry missing: $ENTRY" >&2
  write_wrapper_status 2 no_runner
  echo '{"wakeAgent": true, "exit": 2, "reason": "no_runner"}'
  exit 2
fi

export CAPTCHA_WAIT_S="${CAPTCHA_WAIT_S:-40}"
export CF_WAIT_S="${CF_WAIT_S:-40}"
export SSO_TIMEOUT_S="${SSO_TIMEOUT_S:-45}"
export CTA_WAIT_S="${CTA_WAIT_S:-8}"
export SIGN_WAIT_S="${SIGN_WAIT_S:-8}"
export MIN_DWELL_AFTER_SSO_CLICK_S="${MIN_DWELL_AFTER_SSO_CLICK_S:-4}"
export SITE_TIMEOUT_S="${SITE_TIMEOUT_S:-90}"
export BATCH_TIMEOUT_S="${BATCH_TIMEOUT_S:-6600}"
export BATCH_CLEANUP_RESERVE_S="${BATCH_CLEANUP_RESERVE_S:-30}"
export CONNECT_RETRIES="${CONNECT_RETRIES:-3}"
export CONNECT_RETRY_BACKOFF_S="${CONNECT_RETRY_BACKOFF_S:-1.5}"
export PYTHONUNBUFFERED=1

cd "$ROOT"
exec "$PY" "$ENTRY" "$@"

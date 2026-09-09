#!/bin/zsh
# launchd entry for the daily-checkin project's own scheduler.
# Invoked by com.mango.daily-checkin (LaunchAgents) every day at 08:10.
# This script belongs INSIDE the check-in project so scheduling logic is
# self-contained (not owned by Hermes or any external platform).
#
# Behaviour:
#   - sources the STAGING env (independent DB/log/cred; CDP_HTTP=127.0.0.1:9222
#     reuse of the logged-in profile).
#   - execs scripts/daily-checkin-cron.py which does CDP preflight itself:
#       * if 9222 is unreachable -> writes last-attempt.json with
#         reason=cdp_down and exits 3 (NO auto-launch of Chrome).
#       * holds the run lock; a second concurrent batch exits 4.
#       * runs the full business batch (--source cron), writes logs + DB.
#       * exit 0 only if every site succeeded.
set -uo pipefail

# Common tool paths that launchd's minimal env does not provide.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

ENV_FILE="${DAILY_CHECKIN_ENV_FILE:-$HOME/daily-checkin-staging/env.sh}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "FATAL: env file not found: $ENV_FILE" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

ROOT="${DAILY_CHECKIN_ROOT:-}"
if [[ -z "$ROOT" || ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "FATAL: DAILY_CHECKIN_ROOT invalid or venv missing: ${ROOT:-}" >&2
  exit 2
fi

LOG_DIR="${DAILY_CHECKIN_LOG_DIR:-}"
[[ -n "$LOG_DIR" ]] && mkdir -p "$LOG_DIR"

cd "$ROOT" || { echo "FATAL: cannot cd $ROOT" >&2; exit 2; }
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/daily-checkin-cron.py" --source cron "$@"
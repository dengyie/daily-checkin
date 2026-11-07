#!/bin/zsh
# launchd entry for the dispatch agent (executor node, Mac).
# Invoked by com.mango.daily-checkin-agent (KeepAlive + RunAtLoad): a resident
# but idle stdlib HTTP server that ONLY receives panel-pushed jobs over the
# EasyTier mesh — no polling, no scheduled wakeups, near-zero idle footprint.
#
# Sources the same staging env as the daily batch (CDP_HTTP=127.0.0.1:9222,
# authoritative DB, credentials backend, TG secrets) so dispatched jobs run
# with identical configuration to the cron batch.
set -uo pipefail

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

cd "$ROOT" || { echo "FATAL: cannot cd $ROOT" >&2; exit 2; }
exec "$ROOT/.venv/bin/python" -m checkin_core.dispatch_agent

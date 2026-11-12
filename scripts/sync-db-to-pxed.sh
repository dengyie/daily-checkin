#!/bin/zsh
# Atomic authoritative-DB sync executor-node -> panel replica.
#
# Why the dance: scp writes the destination file non-atomically (truncate +
# rewrite), so a panel reader could observe a torn database mid-transfer.
# We instead upload to a temp name and `mv` it into place, which is atomic
# on the same filesystem. WAL is checkpointed first so the single .db file
# is self-contained.
#
# Per-node targets (multi-node, 2026-11-06): every executor node syncs its
# OWN DB to its own replica path on the panel host, so nodes never clobber
# each other. Defaults keep the legacy Mac behaviour byte-for-byte.
#   DAILY_CHECKIN_DB                    local authoritative DB (required)
#   DAILY_CHECKIN_SYNC_SSH_ALIAS        ssh alias of the panel host (default pxed-direct)
#   DAILY_CHECKIN_SYNC_REMOTE_PATH      replica path on the panel host
#                                       (default /data/daily-checkin/data/system.db;
#                                        e.g. Windows node -> system-win.db)
#
# Best-effort by contract: callers (daily batch, dispatch agent) treat a
# failure as non-fatal — the panel just shows stale state until the next sync.
set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DB="${DAILY_CHECKIN_DB:-}"
SSH_ALIAS="${DAILY_CHECKIN_SYNC_SSH_ALIAS:-pxed-direct}"
REMOTE_PATH="${DAILY_CHECKIN_SYNC_REMOTE_PATH:-/data/daily-checkin/data/system.db}"
# POSIX dirname, not zsh's `${path}` + history-modifier syntax: the shebang
# only governs direct execution — `bash script.sh` (e.g. an operator one-liner
# sourcing env.sh) would die with "h: unbound variable" under `set -u`
# (2026-11-13).
REMOTE_DIR="$(dirname "$REMOTE_PATH")"
REMOTE_TMP="${REMOTE_DIR}/.system.db.incoming"

if [[ -z "$DB" || ! -f "$DB" ]]; then
  echo "sync-db-to-pxed: DAILY_CHECKIN_DB not set or missing, skip" >&2
  exit 0
fi

# Flush WAL into the main DB so scp of a single file carries all state.
sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true

scp -q -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
  "$DB" "${SSH_ALIAS}:${REMOTE_TMP}" 2>/dev/null || { echo "sync-db-to-pxed: scp failed" >&2; exit 1; }

# Atomic swap on the remote side. system.db-wal/-shm are intentionally left
# alone: short-lived panel readers recreate them; the panel no longer writes
# pending-task rows (snapshot materialize=False), so no stale WAL frames can
# be replayed onto the replaced file.
ssh -q -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$SSH_ALIAS" \
  "mv ${REMOTE_TMP} ${REMOTE_PATH}" 2>/dev/null \
  || { echo "sync-db-to-pxed: remote mv failed" >&2; exit 1; }

exit 0

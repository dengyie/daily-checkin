"""SQLite authority for daily-checkin tasks, sites, runs, jobs and credential refs."""
from __future__ import annotations

import json
import logging
import sqlite3
import os
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def obsidian_vault_root() -> Path:
    env_vault = os.environ.get("OBSIDIAN_VAULT_ROOT")
    if env_vault:
        return Path(env_vault).resolve()
    return (
        Path.home()
        / "Library"
        / "Mobile Documents"
        / "iCloud~md~obsidian"
        / "Documents"
        / "obsidian-note"
    ).resolve()


def obsidian_daily_task_path(day: str) -> Path:
    return (obsidian_vault_root() / "Note" / "Task" / "daily" / f"{day} 每日任务.md").resolve()


def default_db_path() -> Path:
    env_db = os.environ.get("DAILY_CHECKIN_DB")
    if env_db:
        return Path(env_db)
    staging_db = Path.home() / "daily-checkin-staging" / "data" / "system.db"
    if staging_db.exists():
        return staging_db
    return Path.home() / ".hermes" / "checkin" / "system.db"


DEFAULT_DB = default_db_path()
SCHEMA_VERSION = 10
logger = logging.getLogger("checkin.store")

DEFAULT_SYSTEM_CONFIGS: dict[str, str] = {
    "schedule_cron_time": "08:10",
    "batch_timeout_s": "2400",
    "site_timeout_s": "60",
    "cf_wait_s": "30",
    "captcha_wait_s": "30",
    "sso_timeout_s": "30",
    "connect_retries": "2",
    "tg_notify_enabled": "true",
    "tg_bot_token": "",
    "tg_chat_id": "",
    "tg_notify_policy": "all",
    "ai_inspector_enabled": "false",
    "ai_inspector_base_url": "",
    "ai_inspector_api_key": "",
    "ai_inspector_model": "gpt-4o-mini",
    "ai_inspector_policy": "on_failure",
    "ai_inspector_fallback_enabled": "false",
    "ai_inspector_fallback_base_url": "",
    "ai_inspector_fallback_api_key": "",
    "ai_inspector_fallback_model": "gpt-4o-mini",
    "ai_inspector_channels": "[]",
    "ai_auto_retry_enabled": "true",
    "ai_auto_retry_max": "3",
}



class SystemStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        committed = False
        try:
            yield db
            db.commit()
            committed = True
        finally:
            if not committed:
                # Roll back any partial writes if the caller raised before the
                # success commit. Without this, an exception after an INSERT
                # would still be persisted by the commit above being skipped —
                # but a raise inside `yield` bypasses commit() and leaves the
                # transaction open; explicit rollback prevents partial writes
                # (e.g. status set to "done") from leaking past a failed method.
                try:
                    db.rollback()
                except Exception:
                    logger.warning("rollback failed", exc_info=True)
            db.close()

    def migrate(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sites(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    url TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'legacy_browser',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    credential_ref TEXT,
                    checkin_mode TEXT NOT NULL DEFAULT 'auto',
                    tags TEXT NOT NULL DEFAULT '',
                    last_balance REAL,
                    last_balance_text TEXT NOT NULL DEFAULT '',
                    last_balance_currency TEXT NOT NULL DEFAULT 'USD',
                    last_reward_text TEXT NOT NULL DEFAULT '',
                    balance_updated_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_tasks(
                    day TEXT NOT NULL,
                    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source TEXT NOT NULL DEFAULT 'system',
                    obsidian_path TEXT,
                    obsidian_name TEXT,
                    last_reason TEXT NOT NULL DEFAULT '',
                    last_reward_text TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(day, site_id)
                );
                CREATE TABLE IF NOT EXISTS site_health(
                    site_id INTEGER PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'active',
                    failures INTEGER NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs(
                    id INTEGER PRIMARY KEY,
                    day TEXT NOT NULL,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    exit_code INTEGER,
                    reason TEXT NOT NULL DEFAULT '',
                    cdp_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS run_items(
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    site_id INTEGER REFERENCES sites(id),
                    site_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    site_names_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS credentials(
                    ref TEXT PRIMARY KEY,
                    site_id INTEGER REFERENCES sites(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_config(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_inspections(
                    id INTEGER PRIMARY KEY,
                    day TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'cron',
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    latency_ms INTEGER DEFAULT 0,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    remediation_log TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_balances(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    day TEXT NOT NULL,
                    amount REAL,
                    raw_text TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT 'USD',
                    source TEXT NOT NULL DEFAULT 'api',
                    reward_amount REAL,
                    reward_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(site_id, day)
                );
                CREATE TABLE IF NOT EXISTS deleted_sites (
                    name TEXT PRIMARY KEY,
                    deleted_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'user_delete'
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_day_status ON daily_tasks(day,status);
                CREATE INDEX IF NOT EXISTS idx_items_run ON run_items(run_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,id);
                CREATE INDEX IF NOT EXISTS idx_ai_inspections_day ON ai_inspections(day);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_balances_site_day ON account_balances(site_id,day);
                """
            )
            # Version-gated schema migration: CREATE TABLE IF NOT EXISTS is a
            # no-op on existing DBs, so columns added to the DDL after a DB was
            # created must be added explicitly here. Each migration below runs
            # only once, when the stored schema_version is older than the step.
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            stored_version = int(row[0]) if row else 0
            if stored_version < SCHEMA_VERSION:
                if stored_version < 2:
                    # v1 -> v2: sites gained the credential_ref column.
                    SystemStore._migrate_column(db, "sites", "credential_ref", "TEXT")
                if stored_version < 3:
                    # v2 -> v3: site_health + credentials tables are created by
                    # the CREATE TABLE IF NOT EXISTS block above; no ALTERs needed.
                    pass
                if stored_version < 4:
                    # v3 -> v4: checkin_mode and tags on sites table.
                    SystemStore._migrate_column(db, "sites", "checkin_mode", "TEXT NOT NULL DEFAULT 'auto'")
                    SystemStore._migrate_column(db, "sites", "tags", "TEXT NOT NULL DEFAULT ''")
                if stored_version < 5:
                    # v4 -> v5: system_config table for dynamic scheduler & notify configuration.
                    pass
                if stored_version < 6:
                    # v5 -> v6: ai_inspections table for post-checkin AI inspection & auto-remediation.
                    pass
                if stored_version < 7:
                    # v6 -> v7: sites balance snapshot columns + account_balances table
                    SystemStore._migrate_column(db, "sites", "last_balance", "REAL")
                    SystemStore._migrate_column(db, "sites", "last_balance_text", "TEXT NOT NULL DEFAULT ''")
                    SystemStore._migrate_column(db, "sites", "balance_updated_at", "TEXT NOT NULL DEFAULT ''")
                if stored_version < 8:
                    # v7 -> v8: sites last_balance_currency column
                    SystemStore._migrate_column(db, "sites", "last_balance_currency", "TEXT NOT NULL DEFAULT 'USD'")
                if stored_version < 9:
                    # v8 -> v9: sites last_reward_text, daily_tasks last_reward_text, account_balances reward fields
                    SystemStore._migrate_column(db, "sites", "last_reward_text", "TEXT NOT NULL DEFAULT ''")
                    SystemStore._migrate_column(db, "daily_tasks", "last_reward_text", "TEXT NOT NULL DEFAULT ''")
                    SystemStore._migrate_column(db, "account_balances", "reward_amount", "REAL")
                    SystemStore._migrate_column(db, "account_balances", "reward_text", "TEXT NOT NULL DEFAULT ''")
                if stored_version < 10:
                    # v9 -> v10: deleted_sites tombstone table to prevent resurrected deleted sites
                    db.execute(
                        """CREATE TABLE IF NOT EXISTS deleted_sites (
                               name TEXT PRIMARY KEY,
                               deleted_at TEXT NOT NULL,
                               reason TEXT NOT NULL DEFAULT 'user_delete'
                           )"""
                    )
            db.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _migrate_column(db, table: str, col_name: str, col_def: str) -> None:
        """Add column to table if it doesn't exist. Safe to call repeatedly."""
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if col_name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
            logger.info("migrate: added column %s.%s", table, col_name)

    @staticmethod
    def now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def upsert_site(self, name: str, url: str = "", provider: str = "legacy_browser") -> int:
        now = self.now()
        with self.connect() as db:
            db.execute("DELETE FROM deleted_sites WHERE name=?", (name,))
            db.execute(
                """INSERT INTO sites(name,url,provider,created_at,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE sites.url END,
                     provider=CASE WHEN excluded.provider<>'' THEN excluded.provider ELSE sites.provider END,
                     updated_at=excluded.updated_at""",
                (name, url, provider, now, now),
            )
            return int(db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()[0])

    def upsert_task(
        self, day: str, name: str, url: str = "", *, provider: str = "legacy_browser",
        checkin_mode: str = "auto", tags: str = "", status: str = "pending",
        source: str = "system", obsidian_path: str | None = None,
    ) -> None:
        """Insert/replace one daily task, atomically with its site row.

        The site upsert and the task INSERT share one transaction (previously
        two connections: upsert_site committed alone, so a failure on the task
        INSERT left a stray committed site row).
        """
        now = self.now()
        with self.connect() as db:
            db.execute("DELETE FROM deleted_sites WHERE name=?", (name,))
            db.execute(
                """INSERT INTO sites(name,url,provider,checkin_mode,tags,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE sites.url END,
                     provider=CASE WHEN excluded.provider<>'' THEN excluded.provider ELSE sites.provider END,
                     checkin_mode=CASE WHEN excluded.checkin_mode<>'' THEN excluded.checkin_mode ELSE sites.checkin_mode END,
                     tags=CASE WHEN excluded.tags<>'' THEN excluded.tags ELSE sites.tags END,
                     updated_at=excluded.updated_at""",
                (name, url, provider, checkin_mode, tags, now, now),
            )
            site_id = int(db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()[0])
            db.execute(
                """INSERT INTO daily_tasks(day,site_id,status,source,obsidian_path,obsidian_name,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(day,site_id) DO UPDATE SET
                     status=excluded.status, source=excluded.source,
                     obsidian_path=COALESCE(excluded.obsidian_path,daily_tasks.obsidian_path),
                     obsidian_name=excluded.obsidian_name, updated_at=excluded.updated_at""",
                (day, site_id, status, source, obsidian_path, name, now),
            )

    def import_obsidian_tasks(
        self, day: str, rows: Iterable[tuple[str, str, bool]], path: str,
    ) -> int:
        """Import new catalog rows without overriding authoritative system state.

        Single transaction for the whole batch (was N+1 connections: one per row
        for upsert_site + one for the task upsert). Authorization contract is
        unchanged — ON CONFLICT keeps current status/source, only refreshing
        obsidian_path/obsidian_name. provider is preserved for existing sites
        (a hardcoded legacy_browser would clobber wisart/newapi/arkengine
        classifications set by other writers).
        """
        count = 0
        now = self.now()
        with self.connect() as db:
            tombstones = {row[0] for row in db.execute("SELECT name FROM deleted_sites").fetchall()}
            for name, url, done in rows:
                if name in tombstones:
                    logger.info("skipping tombstoned deleted site in obsidian import: %s", name)
                    continue
                db.execute(
                    """INSERT INTO sites(name,url,provider,created_at,updated_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(name) DO UPDATE SET
                         url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE sites.url END,
                         provider=sites.provider,
                         updated_at=excluded.updated_at""",
                    (name, url, "legacy_browser", now, now),
                )
                site_id = int(db.execute(
                    "SELECT id FROM sites WHERE name=?", (name,)
                ).fetchone()[0])
                db.execute(
                    """INSERT INTO daily_tasks(
                           day,site_id,status,source,obsidian_path,obsidian_name,updated_at
                       ) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(day,site_id) DO UPDATE SET
                           obsidian_path=excluded.obsidian_path,
                           obsidian_name=excluded.obsidian_name,
                           updated_at=excluded.updated_at""",
                    (
                        day, site_id, "done" if done else "pending", "obsidian",
                        path, name, now,
                    ),
                )
                count += 1
        return count

    def is_site_deleted(self, name: str) -> bool:
        clean = str(name).strip() if name else ""
        if not clean:
            return False
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM deleted_sites WHERE name=?", (clean,)).fetchone()
            return row is not None

    def get_deleted_site_names(self) -> set[str]:
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT name FROM deleted_sites").fetchall()}

    def tasks(self, day: str, *, pending_only: bool = False) -> list[dict[str, Any]]:
        where = "AND t.status='pending'" if pending_only else ""
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT s.id site_id,s.name,s.url,s.provider,s.enabled,s.credential_ref,
                           COALESCE(s.checkin_mode,'auto') checkin_mode,
                           COALESCE(s.tags,'') tags,
                           s.last_balance,
                           COALESCE(s.last_balance_text,'') last_balance_text,
                           COALESCE(s.last_balance_currency,'USD') last_balance_currency,
                           COALESCE(s.last_reward_text,'') last_reward_text,
                           COALESCE(s.balance_updated_at,'') balance_updated_at,
                           s.created_at site_created_at,
                           t.day,t.status,t.source,t.obsidian_path,t.last_reason,
                           CASE WHEN t.last_reward_text<>'' THEN t.last_reward_text ELSE s.last_reward_text END task_reward_text,
                           t.updated_at
                           ,COALESCE(h.status,'active') site_health_status
                           ,COALESCE(h.failures,0) site_health_failures
                           ,COALESCE(h.last_reason,'') site_health_reason
                    FROM daily_tasks t JOIN sites s ON s.id=t.site_id
                    LEFT JOIN site_health h ON h.site_id=s.id
                    WHERE t.day=? AND s.enabled=1 {where} ORDER BY s.id""",
                (day,),
            ).fetchall()
            return [dict(r) for r in rows]

    def materialize_day(self, day: str) -> int:
        """Create today's pending tasks from the authoritative enabled site catalog."""
        now = self.now()
        with self.connect() as db:
            before = db.total_changes
            db.execute(
                """INSERT OR IGNORE INTO daily_tasks(
                       day,site_id,status,source,obsidian_name,updated_at
                   )
                   SELECT ?,id,'pending','system',name,? FROM sites WHERE enabled=1""",
                (day, now),
            )
            return db.total_changes - before

    def set_task_result(
        self, day: str, name: str, status: str, reason: str = "",
        *, only_if_not_done: bool = False,
    ) -> None:
        # only_if_not_done guards the crash path: a business result that already
        # committed "done" must never be overwritten by a later "failed" written
        # from the per-site exception handler (e.g. when add_run_item raises after
        # the success status was already persisted).
        guard = " AND status!='done'" if only_if_not_done else ""
        with self.connect() as db:
            db.execute(
                f"""UPDATE daily_tasks SET status=?,last_reason=?,updated_at=?
                   WHERE day=? AND site_id=(SELECT id FROM sites WHERE name=?){guard}""",
                (status, reason, self.now(), day, name),
            )

    def record_site_health(self, name: str, status: str, reason: str = "") -> None:
        """Update durable health only for a real site attempt."""
        deterministic = {
            "dead_url", "blocked", "business_ineligible", "feature_unavailable",
        }
        now = self.now()
        with self.connect() as db:
            site = db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                return
            site_id = int(site[0])
            current = db.execute(
                "SELECT failures,last_reason FROM site_health WHERE site_id=?", (site_id,)
            ).fetchone()
            if status in {"OK", "ALREADY"}:
                next_status, failures, next_reason = "active", 0, ""
            elif reason in deterministic:
                previous_failures = int(current[0]) if current else 0
                previous_reason = str(current[1]) if current else ""
                failures = previous_failures + 1 if previous_reason == reason else 1
                next_status = "suppressed" if failures >= 2 else "active"
                next_reason = reason
            else:
                # A transient/auth/UI failure is new evidence, so do not let
                # an old deterministic streak suppress the next full run.
                failures = 0
                next_status = "active"
                next_reason = ""
            db.execute(
                "INSERT INTO site_health(site_id,status,failures,last_reason,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(site_id) DO UPDATE SET status=excluded.status,failures=excluded.failures,"
                "last_reason=excluded.last_reason,updated_at=excluded.updated_at",
                (site_id, next_status, failures, next_reason, now),
            )

    def save_ai_inspection(
        self,
        day: str,
        model: str,
        latency_ms: int,
        status: str,
        summary: str,
        structured_json: str,
        remediation_log: str = "[]",
        source: str = "cron",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> int:
        now = self.now()
        with self.connect() as db:
            cur = db.execute(
                """INSERT INTO ai_inspections(
                       day, source, model, prompt_tokens, completion_tokens,
                       latency_ms, status, summary, structured_json, remediation_log, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    day, source, model, int(prompt_tokens), int(completion_tokens),
                    int(latency_ms), status, summary, structured_json, remediation_log, now,
                ),
            )
            return int(cur.lastrowid)

    def get_latest_ai_inspection(self, day: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM ai_inspections WHERE day=? ORDER BY id DESC LIMIT 1",
                (day,),
            ).fetchone()
            return dict(row) if row else None

    def update_latest_ai_remediation_log(
        self,
        day: str,
        remediation_log: Sequence[dict[str, Any]] | str,
    ) -> bool:
        """Update remediation_log for the latest ai_inspection on given day."""
        log_json = (
            remediation_log
            if isinstance(remediation_log, str)
            else json.dumps(list(remediation_log), ensure_ascii=False)
        )
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM ai_inspections WHERE day=? ORDER BY id DESC LIMIT 1",
                (day,),
            ).fetchone()
            if not row:
                return False
            db.execute(
                "UPDATE ai_inspections SET remediation_log=? WHERE id=?",
                (log_json, row["id"]),
            )
            return True

    def get_site_failures(self, site_name: str) -> int:
        with self.connect() as db:
            row = db.execute(
                """SELECT sh.failures FROM site_health sh
                   JOIN sites s ON sh.site_id = s.id
                   WHERE s.name = ?""",
                (site_name,),
            ).fetchone()
            return int(row["failures"]) if row else 0

    def get_today_retry_count(self, site_name: str, day: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key = ?",
                (f"ai_retry:{day}:{site_name}",),
            ).fetchone()
            return int(row["value"]) if row else 0

    def increment_today_retry_count(self, site_name: str, day: str) -> int:
        with self.connect() as db:
            key = f"ai_retry:{day}:{site_name}"
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            new_val = (int(row["value"]) + 1) if row else 1
            db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(new_val)),
            )
            return new_val

    def suppress_site(self, name: str, reason: str = "ai:suppressed") -> None:
        now = self.now()
        with self.connect() as db:
            site = db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                return
            site_id = int(site[0])
            db.execute(
                "INSERT INTO site_health(site_id, status, failures, last_reason, updated_at) "
                "VALUES(?, 'suppressed', 1, ?, ?) "
                "ON CONFLICT(site_id) DO UPDATE SET status='suppressed', "
                "last_reason=excluded.last_reason, updated_at=excluded.updated_at",
                (site_id, reason, now),
            )

    def create_run(self, day: str, source: str = "cli", cdp: dict | None = None) -> int:
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO runs(day,source,started_at,cdp_json) VALUES(?,?,?,?)",
                (day, source, self.now(), json.dumps(cdp or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def add_run_item(self, run_id: int, result: Any) -> None:
        raw = asdict(result) if is_dataclass(result) else dict(result)
        name = str(raw.get("site") or "")
        evidence = {k: raw.get(k) for k in (
            "action", "confirmation", "identity", "pre_state", "post_state",
            "transition", "attribution", "business_status", "business_reason",
            "projection_status", "projection_reason", "balance",
        )}
        if not evidence.get("attribution"):
            evidence["attribution"] = "legacy_unknown"
        with self.connect() as db:
            site = db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()
            db.execute(
                """INSERT INTO run_items(run_id,site_id,site_name,status,stage,reason,provider,
                       latency_ms,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (run_id, site[0] if site else None, name, raw.get("status", "FAIL"),
                 raw.get("stage", ""), raw.get("reason", ""), raw.get("provider", ""),
                 int(raw.get("latency_ms") or 0), json.dumps(evidence, ensure_ascii=False), self.now()),
            )

    def record_site_balance(self, name: str, day: str, balance: Any) -> None:
        """Record account balance snapshot on sites table and append to account_balances."""
        if not balance:
            return
        raw = asdict(balance) if is_dataclass(balance) else dict(balance)
        amount = raw.get("amount")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        raw_text = str(raw.get("raw_text") or "").strip()
        currency = str(raw.get("currency") or "USD").strip()
        source = str(raw.get("source") or "api").strip()
        reward_amount = raw.get("reward_amount")
        try:
            reward_amount = float(reward_amount) if reward_amount is not None else None
        except (TypeError, ValueError):
            reward_amount = None
        reward_text = str(raw.get("reward_text") or "").strip()
        now = self.now()
        with self.connect() as db:
            site = db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                return
            site_id = site[0]
            db.execute(
                """UPDATE sites SET
                     last_balance=CASE WHEN ? IS NOT NULL THEN ? ELSE last_balance END,
                     last_balance_text=CASE WHEN ?<>'' THEN ? ELSE last_balance_text END,
                     last_balance_currency=CASE WHEN ?<>'' THEN ? ELSE last_balance_currency END,
                     last_reward_text=CASE WHEN ?<>'' THEN ? ELSE last_reward_text END,
                     balance_updated_at=?
                   WHERE id=?""",
                (amount, amount, raw_text, raw_text, currency, currency, reward_text, reward_text, now, site_id),
            )
            if reward_text:
                db.execute(
                    """UPDATE daily_tasks SET last_reward_text=?
                       WHERE day=? AND site_id=?""",
                    (reward_text, day, site_id),
                )
            db.execute(
                """INSERT INTO account_balances(site_id, day, amount, raw_text, currency, source,
                                                reward_amount, reward_text, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(site_id, day) DO UPDATE SET
                     amount=COALESCE(excluded.amount, account_balances.amount),
                     raw_text=CASE WHEN excluded.raw_text<>'' THEN excluded.raw_text ELSE account_balances.raw_text END,
                     currency=CASE WHEN excluded.currency<>'' THEN excluded.currency ELSE account_balances.currency END,
                     source=excluded.source,
                     reward_amount=COALESCE(excluded.reward_amount, account_balances.reward_amount),
                     reward_text=CASE WHEN excluded.reward_text<>'' THEN excluded.reward_text ELSE account_balances.reward_text END,
                     created_at=excluded.created_at""",
                (site_id, day, amount, raw_text, currency, source, reward_amount, reward_text, now),
            )

    def get_site_balance_history(self, name: str, limit: int = 30) -> list[dict[str, Any]]:
        """Return historical balance records for a site."""
        try:
            safe_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            safe_limit = 30
        with self.connect() as db:
            site = db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                return []
            site_id = site[0]
            rows = db.execute(
                """SELECT id, site_id, day, amount, raw_text, currency, source,
                          reward_amount, reward_text, created_at
                   FROM account_balances
                   WHERE site_id=?
                   ORDER BY day DESC, id DESC
                   LIMIT ?""",
                (site_id, safe_limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_site_balance_manual(
        self,
        name: str,
        amount: float | None,
        currency: str = "USD",
        raw_text: str = "",
        day: str = "",
        source: str = "manual",
    ) -> dict[str, Any] | None:
        """Manually update or adjust a site balance and record to daily history."""
        day = day or self.now()[:10]
        currency = (currency or "USD").strip().upper()
        if not raw_text and amount is not None:
            if currency == "USD":
                raw_text = f"${amount:.2f}"
            elif currency == "CNY":
                raw_text = f"¥{amount:.2f}"
            elif currency == "POINTS":
                formatted_amt = int(amount) if amount.is_integer() else f"{amount:.1f}"
                raw_text = f"{formatted_amt} 积分"
            else:
                raw_text = f"{amount:.2f} {currency}"
        evidence = {
            "amount": amount,
            "raw_text": raw_text,
            "currency": currency,
            "source": source,
        }
        with self.connect() as db:
            site = db.execute("SELECT id FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                raise ValueError(f"site not found: {name}")
        self.record_site_balance(name, day, evidence)
        return evidence

    def finish_run(self, run_id: int, exit_code: int, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET finished_at=?,exit_code=?,reason=? WHERE id=?",
                (self.now(), int(exit_code), reason, run_id),
            )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            runs = [dict(r) for r in db.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()]
            for run in runs:
                run["items"] = [dict(x) for x in db.execute(
                    "SELECT site_name,status,stage,reason,provider,latency_ms,evidence_json "
                    "FROM run_items WHERE run_id=? ORDER BY id", (run["id"],)
                ).fetchall()]
            return runs

    def create_job(self, kind: str, site_names: list[str] | None = None) -> int:
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO jobs(kind,site_names_json,created_at) VALUES(?,?,?)",
                (kind, json.dumps(site_names or [], ensure_ascii=False), self.now()),
            )
            return int(cur.lastrowid)

    def claim_job(self, job_id: int) -> bool:
        """Atomically transition a job from queued to running.

        Returns True if this worker process successfully claimed the job,
        False if another worker already claimed it or status changed.
        """
        now = self.now()
        with self.connect() as db:
            cur = db.execute(
                "UPDATE jobs SET status='running',started_at=? WHERE id=? AND status='queued'",
                (now, job_id),
            )
            return cur.rowcount > 0

    def update_job(self, job_id: int, status: str, **fields: Any) -> None:
        allowed = {"started_at", "finished_at", "exit_code", "message"}
        vals = {k: v for k, v in fields.items() if k in allowed}
        sets = ["status=?"] + [f"{k}=?" for k in vals]
        with self.connect() as db:
            db.execute(
                f"UPDATE jobs SET {','.join(sets)} WHERE id=?",
                [status, *vals.values(), job_id],
            )

    def jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()]

    def set_credential_ref(self, site_name: str, ref: str, kind: str, label: str = "") -> None:
        """Record a credential reference, atomically with its site row.

        The site upsert, credential INSERT, and site.credential_ref update share
        one transaction (previously the site upsert committed on its own).
        """
        now = self.now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO sites(name,url,provider,created_at,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE sites.url END,
                     provider=CASE WHEN excluded.provider<>'' THEN excluded.provider ELSE sites.provider END,
                     updated_at=excluded.updated_at""",
                (site_name, "", "legacy_browser", now, now),
            )
            site_id = int(db.execute("SELECT id FROM sites WHERE name=?", (site_name,)).fetchone()[0])
            db.execute(
                "INSERT INTO credentials(ref,site_id,kind,label,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(ref) DO UPDATE SET kind=excluded.kind,label=excluded.label,updated_at=excluded.updated_at",
                (ref, site_id, kind, label, now),
            )
            db.execute("UPDATE sites SET credential_ref=?,updated_at=? WHERE id=?", (ref, now, site_id))

    def update_site_config(self, name: str, **fields: Any) -> dict[str, Any]:
        """Update mutable configuration fields of a site row."""
        allowed = {"checkin_mode", "tags", "enabled", "url", "provider"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            raise ValueError("no valid fields to update")
        now = self.now()
        updates["updated_at"] = now
        sets = [f"{k}=?" for k in updates]
        params = list(updates.values()) + [name]
        with self.connect() as db:
            cur = db.execute(f"UPDATE sites SET {', '.join(sets)} WHERE name=?", params)
            if cur.rowcount == 0:
                # Upsert site if not present
                db.execute(
                    """INSERT INTO sites(name,url,provider,checkin_mode,tags,enabled,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        name,
                        str(updates.get("url", "")),
                        str(updates.get("provider", "legacy_browser")),
                        str(updates.get("checkin_mode", "auto")),
                        str(updates.get("tags", "")),
                        int(updates.get("enabled", 1)),
                        now,
                        now,
                    ),
                )
            row = db.execute("SELECT * FROM sites WHERE name=?", (name,)).fetchone()
            return dict(row) if row else {}

    def delete_credential_ref(self, ref: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE sites SET credential_ref=NULL WHERE credential_ref=?", (ref,))
            db.execute("DELETE FROM credentials WHERE ref=?", (ref,))

    def delete_site(self, name: str, ignore_missing: bool = False) -> dict[str, Any]:
        """Permanently delete a site by name, cascading daily_tasks, site_health,
        account_balances, and credentials rows. Preserves historical run_items by disassociating site_id.
        Records a tombstone in deleted_sites and projects removal to Obsidian task files.
        If ignore_missing=True, gracefully tombstones and cleans up Markdown even if the site
        is absent from the sites table.
        Returns dictionary with deleted site metadata and counts.
        """
        clean_name = str(name).strip() if name is not None else ""
        if not clean_name:
            raise ValueError("site name required")

        now = self.now()
        with self.connect() as db:
            site = db.execute(
                "SELECT id, name, credential_ref FROM sites WHERE name=?",
                (clean_name,),
            ).fetchone()
            if not site:
                if not ignore_missing:
                    raise ValueError(f"site not found: {clean_name}")
                # Defensive tombstone even if site row does not exist in sites table
                db.execute(
                    """INSERT INTO deleted_sites(name, deleted_at, reason)
                       VALUES(?, ?, 'user_delete')
                       ON CONFLICT(name) DO UPDATE SET deleted_at=excluded.deleted_at""",
                    (clean_name, now),
                )
                site_id = None
                cred_ref = None
                cred_refs: list[str] = []
                tasks_count = 0
                obsidian_paths: list[str] = []
            else:
                site_id = int(site["id"])
                cred_ref = site["credential_ref"]

                # Collect all credential refs associated with this site before deletion
                cred_refs = [
                    row[0] for row in db.execute(
                        "SELECT ref FROM credentials WHERE site_id=? AND ref IS NOT NULL AND ref!=''",
                        (site_id,),
                    ).fetchall()
                ]
                if cred_ref and cred_ref not in cred_refs:
                    cred_refs.append(cred_ref)

                # Collect any obsidian task file paths referencing this site before cascade delete
                obsidian_paths = [
                    row[0] for row in db.execute(
                        "SELECT DISTINCT obsidian_path FROM daily_tasks WHERE site_id=? AND obsidian_path IS NOT NULL AND obsidian_path!=''",
                        (site_id,),
                    ).fetchall()
                ]

                tasks_count = int(db.execute(
                    "SELECT COUNT(*) FROM daily_tasks WHERE site_id=?",
                    (site_id,),
                ).fetchone()[0])

                # Disassociate run_items.site_id to NULL to preserve historical records safely
                db.execute("UPDATE run_items SET site_id=NULL WHERE site_id=?", (site_id,))

                # Record tombstone in deleted_sites to prevent resurrection on future sync/cron
                db.execute(
                    """INSERT INTO deleted_sites(name, deleted_at, reason)
                       VALUES(?, ?, 'user_delete')
                       ON CONFLICT(name) DO UPDATE SET deleted_at=excluded.deleted_at""",
                    (clean_name, now),
                )

                # Explicitly delete child rows to guarantee cascade even when PRAGMA foreign_keys is OFF
                db.execute("DELETE FROM daily_tasks WHERE site_id=?", (site_id,))
                db.execute("DELETE FROM site_health WHERE site_id=?", (site_id,))
                db.execute("DELETE FROM credentials WHERE site_id=?", (site_id,))
                db.execute("DELETE FROM account_balances WHERE site_id=?", (site_id,))
                db.execute("DELETE FROM sites WHERE id=?", (site_id,))

        # Synchronously project deletion to active Obsidian markdown daily task files
        self._project_obsidian_site_deleted(clean_name, candidate_paths=obsidian_paths)

        return {
            "name": clean_name,
            "site_id": site_id,
            "credential_ref": cred_ref,
            "credential_refs": cred_refs,
            "deleted_tasks": tasks_count,
        }

    def _project_obsidian_site_deleted(
        self, name: str, candidate_paths: list[str | Path] | None = None
    ) -> bool:
        """Best-effort removal of markdown task lines for a deleted site from disk.
        Removes all matching check-in task lines for this site.
        Returns True if at least one file was modified, False otherwise.
        """
        import re
        from datetime import datetime
        vault_root = obsidian_vault_root()
        today = datetime.now().strftime("%Y-%m-%d")
        vault_default = obsidian_daily_task_path(today)

        paths_to_check: list[Path] = []
        if candidate_paths:
            for cp in candidate_paths:
                p = Path(cp).resolve()
                if p not in paths_to_check:
                    paths_to_check.append(p)

        if vault_default not in paths_to_check:
            paths_to_check.append(vault_default)

        escaped = re.escape(name)
        pattern = re.compile(
            rf"^\s*-\s*\[[ xX]\]\s*(?=.*#task)(?=.*#日常)[^\n]*\[{escaped}\](?:\([^)]*\))?[^\n]*$",
            re.IGNORECASE,
        )

        modified = False
        for p in paths_to_check:
            try:
                if not p.is_file():
                    continue
                is_candidate = candidate_paths and any(p == Path(cp).resolve() for cp in candidate_paths)
                if not is_candidate and not p.is_relative_to(vault_root) and p != vault_default:
                    continue

                lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
                new_lines = []
                file_changed = False
                for line in lines:
                    stripped = line.rstrip("\r\n")
                    if pattern.match(stripped):
                        file_changed = True
                        continue
                    new_lines.append(line)

                if file_changed:
                    tmp = p.with_name(p.name + ".tmp")
                    tmp.write_text("".join(new_lines), encoding="utf-8")
                    tmp.replace(p)
                    logger.info("projected site deletion to obsidian: %s (removed %s)", p, name)
                    modified = True
            except Exception as e:
                logger.warning("failed to project site deletion to %s: %s", p, e)

        return modified


    def _project_obsidian_manual_done(self, day: str, name: str) -> bool:
        """Best-effort update of markdown task note on disk if available.
        Returns True if a matching markdown task line was updated, False otherwise."""
        import re
        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
            logger.warning("invalid day format for obsidian projection: %s", day)
            return False

        vault_root = obsidian_vault_root()
        vault_default = obsidian_daily_task_path(day)

        candidate_paths = []
        with self.connect() as db:
            row = db.execute(
                "SELECT obsidian_path FROM daily_tasks WHERE day=? AND site_id=(SELECT id FROM sites WHERE name=?)",
                (day, name),
            ).fetchone()
            if row and row["obsidian_path"]:
                cand = Path(row["obsidian_path"]).resolve()
                if cand.is_relative_to(vault_root):
                    candidate_paths.append(cand)
                else:
                    logger.warning("obsidian_path outside vault root ignored: %s", cand)

        if vault_default not in candidate_paths:
            candidate_paths.append(vault_default)

        projected = False
        for p in candidate_paths:
            try:
                if p.is_file():
                    text = p.read_text(encoding="utf-8")
                    if re.search(rf"(?m)^\s*-\s*\[[xX]\]\s*#task\s*#日常\s*\[{re.escape(name)}\]", text):
                        projected = True
                        continue
                    patterns = [
                        rf"(?m)^(\s*-\s*)\[\s\](\s*#task\s*#日常\s*\[{re.escape(name)}\]\([^)]*\).*)",
                        rf"(?m)^(\s*-\s*)\[\s\](\s*#task\s*#日常\s*\[{re.escape(name)}\].*)",
                        rf"(?m)^(\s*-\s*)\[\s\](\s*#task\s*#日常[^\n]*\[{re.escape(name)}\].*)",
                    ]
                    for pat in patterns:
                        new_text, n = re.subn(pat, r"\1[x]\2", text, count=1)
                        if n:
                            new_text = re.sub(
                                rf"(?m)^(\s*-\s*\[[xX]\]\s*#task\s*#日常\s*\[{re.escape(name)}\].*?)(\s+#auto-fail:\S+)",
                                r"\1",
                                new_text,
                            )
                            tmp = p.with_name(p.name + ".tmp")
                            tmp.write_text(new_text, encoding="utf-8")
                            tmp.replace(p)
                            logger.info("projected manual done to obsidian: %s", p)
                            projected = True
                            break
            except Exception as e:
                logger.warning("failed to project manual done to %s: %s", p, e)
        return projected

    def complete_task_manually(self, day: str, name: str) -> dict[str, Any]:
        """Toggle a site task's manual status for the given day.
        If the task is already completed it reverts to pending (un-do), otherwise marks it done,
        updates health, records a run item, and syncs obsidian if the file exists."""
        import re
        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
            raise ValueError(f"invalid day format (expected YYYY-MM-DD): {day}")
        now = self.now()
        # Toggle: an already-completed task is reverted back to pending (未打卡).
        with self.connect() as db:
            cur = db.execute(
                "SELECT dt.status FROM daily_tasks dt "
                "JOIN sites s ON s.id=dt.site_id WHERE dt.day=? AND s.name=?",
                (day, name),
            ).fetchone()
            already_done = bool(cur and cur["status"] in ("done", "OK", "ALREADY"))
        if already_done:
            return self.revert_task_manually(day, name)
        with self.connect() as db:
            site = db.execute("SELECT id, url, provider FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                # Upsert fallback site record for manual or sync-discovered tasks
                db.execute(
                    """INSERT INTO sites(name,url,provider,checkin_mode,tags,enabled,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (name, "", "manual", "manual", "manual", 1, now, now),
                )
                site = db.execute("SELECT id, url, provider FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                raise ValueError(f"site not found: {name}")
            site_id = int(site["id"])
            provider = str(site["provider"] or "manual")

            db.execute(
                """INSERT INTO daily_tasks(day,site_id,status,source,obsidian_name,last_reason,updated_at)
                   VALUES(?,?,'done','manual',?,'manual_confirmed',?)
                   ON CONFLICT(day,site_id) DO UPDATE SET
                     status='done', source='manual', last_reason='manual_confirmed', updated_at=excluded.updated_at""",
                (day, site_id, name, now),
            )
            db.execute(
                """INSERT INTO site_health(site_id,status,failures,last_reason,updated_at) VALUES(?,'active',0,'',?)
                   ON CONFLICT(site_id) DO UPDATE SET status='active',failures=0,last_reason='',updated_at=excluded.updated_at""",
                (site_id, now),
            )
            cur = db.execute(
                "INSERT INTO runs(day,source,started_at,finished_at,exit_code,reason,cdp_json) VALUES(?,?,?,?,0,'manual_done','{}')",
                (day, "manual", now, now),
            )
            run_id = int(cur.lastrowid)
            evidence = {
                "action": {"name": "manual_checkin", "status": "OK"},
                "confirmation": {"type": "manual_confirmation", "user_verified": True},
                "attribution": "manual_user_action",
                "business_status": "done",
                "business_reason": "manual_confirmed",
            }
            db.execute(
                """INSERT INTO run_items(run_id,site_id,site_name,status,stage,reason,provider,latency_ms,evidence_json,created_at)
                   VALUES(?,?,?,'OK','manual','manual_confirmed',?,0,?,?)""",
                (run_id, site_id, name, provider, json.dumps(evidence, ensure_ascii=False), now),
            )

        projected = self._project_obsidian_manual_done(day, name)
        return {"ok": True, "site": name, "day": day, "status": "done", "action": "mark_done", "obsidian_projected": projected}


    def revert_task_manually(self, day: str, name: str) -> dict[str, Any]:
        """Revert a manually completed site task back to pending for the given day, and sync obsidian if file exists."""
        import re
        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
            raise ValueError(f"invalid day format (expected YYYY-MM-DD): {day}")
        now = self.now()
        with self.connect() as db:
            site = db.execute("SELECT id, url, provider FROM sites WHERE name=?", (name,)).fetchone()
            if not site:
                raise ValueError(f"site not found: {name}")
            site_id = int(site["id"])
            provider = str(site["provider"] or "manual")

            db.execute(
                """INSERT INTO daily_tasks(day,site_id,status,source,obsidian_name,last_reason,updated_at)
                   VALUES(?,?,'pending','manual',?,'manual_reverted',?)
                   ON CONFLICT(day,site_id) DO UPDATE SET
                     status='pending', source='manual', last_reason='manual_reverted', updated_at=excluded.updated_at""",
                (day, site_id, name, now),
            )
            cur = db.execute(
                "INSERT INTO runs(day,source,started_at,finished_at,exit_code,reason,cdp_json) VALUES(?,?,?,?,0,'manual_reverted','{}')",
                (day, "manual", now, now),
            )
            run_id = int(cur.lastrowid)
            db.execute(
                """INSERT INTO run_items(run_id,site_id,site_name,status,stage,reason,provider,latency_ms,evidence_json,created_at)
                   VALUES(?,?,?,'PENDING','manual','manual_reverted',?,0,'{}',?)""",
                (run_id, site_id, name, provider, now),
            )

        projected = self._project_obsidian_manual_revert(day, name)
        return {"ok": True, "site": name, "day": day, "status": "pending", "action": "revert", "obsidian_projected": projected}


    def _project_obsidian_manual_revert(self, day: str, name: str) -> bool:
        """Best-effort update of markdown task note on disk back to unchecked if available.
        Returns True if a matching markdown task line was reverted, False otherwise."""
        import re
        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
            logger.warning("invalid day format for obsidian revert projection: %s", day)
            return False

        vault_root = obsidian_vault_root()
        vault_default = obsidian_daily_task_path(day)

        candidate_paths = []
        with self.connect() as db:
            row = db.execute(
                "SELECT obsidian_path FROM daily_tasks WHERE day=? AND site_id=(SELECT id FROM sites WHERE name=?)",
                (day, name),
            ).fetchone()
            if row and row["obsidian_path"]:
                cand = Path(row["obsidian_path"]).resolve()
                if cand.is_relative_to(vault_root):
                    candidate_paths.append(cand)
                else:
                    logger.warning("obsidian_path outside vault root ignored: %s", cand)

        if vault_default not in candidate_paths:
            candidate_paths.append(vault_default)

        projected = False
        for p in candidate_paths:
            try:
                if p.is_file():
                    text = p.read_text(encoding="utf-8")
                    if re.search(rf"(?m)^\s*-\s*\[\s\]\s*#task\s*#日常\s*\[{re.escape(name)}\]", text):
                        projected = True
                        continue
                    patterns = [
                        rf"(?m)^(\s*-\s*)\[[xX]\](\s*#task\s*#日常\s*\[{re.escape(name)}\]\([^)]*\).*)",
                        rf"(?m)^(\s*-\s*)\[[xX]\](\s*#task\s*#日常\s*\[{re.escape(name)}\]\s*$)",
                        rf"(?m)^(\s*-\s*)\[[xX]\](\s*#task\s*#日常[^\n]*\[{re.escape(name)}\].*)",
                    ]
                    for pat in patterns:
                        new_text, n = re.subn(pat, r"\1[ ]\2", text, count=1)
                        if n:
                            tmp = p.with_name(p.name + ".tmp")
                            tmp.write_text(new_text, encoding="utf-8")
                            tmp.replace(p)
                            logger.info("projected manual revert to obsidian: %s", p)
                            projected = True
                            break
            except Exception as e:
                logger.warning("failed to project manual revert to %s: %s", p, e)
        return projected


    def credential_refs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                """SELECT c.ref,s.name site,c.kind,c.label,c.updated_at
                   FROM credentials c LEFT JOIN sites s ON s.id=c.site_id
                   ORDER BY c.updated_at DESC"""
            ).fetchall()]

    def daily_history(self, days: int = 14) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT day,
                          COUNT(*) as total,
                          SUM(CASE WHEN status IN ('done', 'OK', 'ALREADY') THEN 1 ELSE 0 END) as done_count,
                          SUM(CASE WHEN status IN ('failed', 'FAIL') THEN 1 ELSE 0 END) as fail_count,
                          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_count
                   FROM daily_tasks
                   GROUP BY day
                   ORDER BY day DESC
                   LIMIT ?""",
                (max(1, min(days, 90)),),
            ).fetchall()
            return [dict(r) for r in rows]

    def total_runs_count(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) FROM runs").fetchone()
            return int(row[0]) if row else 0

    def snapshot(self, day: str) -> dict[str, Any]:
        self.materialize_day(day)
        return {
            "schema_version": SCHEMA_VERSION,
            "day": day,
            "tasks": self.tasks(day),
            "credentials": self.credential_refs(),
            "runs": self.recent_runs(30),
            "jobs": self.jobs(30),
            "history": self.daily_history(14),
            "total_runs_count": self.total_runs_count(),
            "ai_inspection": self.get_latest_ai_inspection(day),
        }


    def get_config(self, key: str, default: str | None = None) -> str | None:
        """Get a configuration string value from system_config table or fallback."""
        with self.connect() as db:
            row = db.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
            if row is not None:
                return str(row["value"])
        if default is not None:
            return default
        # Environment fallback map
        env_map = {
            "schedule_cron_time": "08:10",
            "batch_timeout_s": os.environ.get("BATCH_TIMEOUT_S", "2400"),
            "site_timeout_s": os.environ.get("SITE_TIMEOUT_S", "60"),
            "cf_wait_s": os.environ.get("CF_WAIT_S", "30"),
            "captcha_wait_s": os.environ.get("CAPTCHA_WAIT_S", "30"),
            "sso_timeout_s": os.environ.get("SSO_TIMEOUT_S", "30"),
            "connect_retries": os.environ.get("CONNECT_RETRIES", "2"),
            "tg_notify_enabled": "true" if os.environ.get("DAILY_CHECKIN_TG_BOT_TOKEN") else "false",
            "tg_bot_token": os.environ.get("DAILY_CHECKIN_TG_BOT_TOKEN", ""),
            "tg_chat_id": os.environ.get("DAILY_CHECKIN_TG_CHAT_ID", ""),
            "tg_notify_policy": "all",
            "ai_inspector_enabled": os.environ.get("CHECKIN_AI_INSPECTOR_ENABLED", "false"),
            "ai_inspector_base_url": os.environ.get("CHECKIN_AI_INSPECTOR_BASE_URL", ""),
            "ai_inspector_api_key": os.environ.get("CHECKIN_AI_INSPECTOR_API_KEY", ""),
            "ai_inspector_model": os.environ.get("CHECKIN_AI_INSPECTOR_MODEL", "gpt-4o-mini"),
            "ai_inspector_policy": os.environ.get("CHECKIN_AI_INSPECTOR_POLICY", "on_failure"),
            "ai_inspector_fallback_enabled": os.environ.get("CHECKIN_AI_INSPECTOR_FALLBACK_ENABLED", "false"),
            "ai_inspector_fallback_base_url": os.environ.get("CHECKIN_AI_INSPECTOR_FALLBACK_BASE_URL", ""),
            "ai_inspector_fallback_api_key": os.environ.get("CHECKIN_AI_INSPECTOR_FALLBACK_API_KEY", ""),
            "ai_inspector_fallback_model": os.environ.get("CHECKIN_AI_INSPECTOR_FALLBACK_MODEL", "gpt-4o-mini"),
            "ai_inspector_channels": os.environ.get("CHECKIN_AI_INSPECTOR_CHANNELS", "[]"),
            "ai_auto_retry_enabled": os.environ.get("CHECKIN_AI_AUTO_RETRY_ENABLED", "true"),
            "ai_auto_retry_max": os.environ.get("CHECKIN_AI_AUTO_RETRY_MAX", "3"),
        }
        return env_map.get(key, DEFAULT_SYSTEM_CONFIGS.get(key, ""))

    def get_ai_channels(self) -> list[Any]:
        """Return prioritized list of configured AI channels with fallback support."""
        from checkin_core.ai_inspector_models import AIChannelConfig
        channels_json = self.get_config("ai_inspector_channels", "[]") or "[]"
        channels: list[AIChannelConfig] = []
        if channels_json and channels_json != "[]":
            try:
                raw_list = json.loads(channels_json)
                if isinstance(raw_list, list):
                    for item in raw_list:
                        if isinstance(item, dict):
                            channels.append(AIChannelConfig.from_dict(item))
            except Exception as e:
                logger.warning("Failed to parse ai_inspector_channels: %s", e)

        if not channels:
            primary_url = (self.get_config("ai_inspector_base_url") or "").strip()
            primary_key = (self.get_config("ai_inspector_api_key") or "").strip()
            primary_model = (self.get_config("ai_inspector_model") or "gpt-4o-mini").strip()
            if primary_url and primary_key:
                channels.append(AIChannelConfig(
                    name="Primary (主用渠道)",
                    base_url=primary_url,
                    api_key=primary_key,
                    model=primary_model,
                    enabled=True,
                ))

            fb_enabled = str(self.get_config("ai_inspector_fallback_enabled") or "false").lower() in ("true", "1", "yes")
            fb_url = (self.get_config("ai_inspector_fallback_base_url") or "").strip()
            fb_key = (self.get_config("ai_inspector_fallback_api_key") or "").strip()
            fb_model = (self.get_config("ai_inspector_fallback_model") or "gpt-4o-mini").strip()
            if fb_enabled and fb_url and fb_key:
                channels.append(AIChannelConfig(
                    name="Fallback (备用降级渠道)",
                    base_url=fb_url,
                    api_key=fb_key,
                    model=fb_model,
                    enabled=True,
                ))
        return channels

    def get_all_configs(self) -> dict[str, str]:
        """Return all merged system configs with defaults and environment fallbacks."""
        configs = dict(DEFAULT_SYSTEM_CONFIGS)
        # Apply environment defaults
        for k in configs:
            val = self.get_config(k)
            if val is not None:
                configs[k] = val
        # Apply DB stored overrides
        with self.connect() as db:
            for r in db.execute("SELECT key, value FROM system_config").fetchall():
                configs[r["key"]] = str(r["value"])
        return configs

    def set_config(self, key: str, value: str) -> None:
        """Set a single config key-value pair."""
        now = self.now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO system_config(key, value, updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, str(value), now),
            )

    def set_configs(self, mapping: dict[str, Any]) -> dict[str, str]:
        """Set multiple configuration keys in one transaction."""
        now = self.now()
        with self.connect() as db:
            for k, v in mapping.items():
                if k in DEFAULT_SYSTEM_CONFIGS or k.startswith("custom_"):
                    db.execute(
                        """INSERT INTO system_config(key, value, updated_at) VALUES(?,?,?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                        (k, str(v), now),
                    )
        return self.get_all_configs()


def store_from_env() -> SystemStore:

    return SystemStore(default_db_path())

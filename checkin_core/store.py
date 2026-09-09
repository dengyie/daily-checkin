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
SCHEMA_VERSION = 5
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
                CREATE INDEX IF NOT EXISTS idx_tasks_day_status ON daily_tasks(day,status);
                CREATE INDEX IF NOT EXISTS idx_items_run ON run_items(run_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,id);
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
            for name, url, done in rows:
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

    def tasks(self, day: str, *, pending_only: bool = False) -> list[dict[str, Any]]:
        where = "AND t.status='pending'" if pending_only else ""
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT s.id site_id,s.name,s.url,s.provider,s.enabled,s.credential_ref,
                           COALESCE(s.checkin_mode,'auto') checkin_mode,
                           COALESCE(s.tags,'') tags,
                           s.created_at site_created_at,
                           t.day,t.status,t.source,t.obsidian_path,t.last_reason,t.updated_at
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
            "projection_status", "projection_reason",
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

    def delete_site(self, name: str) -> dict[str, Any]:
        """Permanently delete a site by name, cascading daily_tasks, site_health,
        and credentials rows. Preserves historical run_items by disassociating site_id.
        Returns dictionary with deleted site metadata and counts.
        """
        clean_name = str(name).strip() if name is not None else ""
        if not clean_name:
            raise ValueError("site name required")

        with self.connect() as db:
            site = db.execute(
                "SELECT id, name, credential_ref FROM sites WHERE name=?",
                (clean_name,),
            ).fetchone()
            if not site:
                raise ValueError(f"site not found: {clean_name}")

            site_id = int(site["id"])
            cred_ref = site["credential_ref"]

            tasks_count = int(db.execute(
                "SELECT COUNT(*) FROM daily_tasks WHERE site_id=?",
                (site_id,),
            ).fetchone()[0])

            # Disassociate run_items.site_id to NULL to preserve historical records safely
            db.execute("UPDATE run_items SET site_id=NULL WHERE site_id=?", (site_id,))

            # Delete the site (foreign key cascades to daily_tasks, site_health, credentials)
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))

            return {
                "name": clean_name,
                "site_id": site_id,
                "credential_ref": cred_ref,
                "deleted_tasks": tasks_count,
            }


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
        }
        return env_map.get(key, DEFAULT_SYSTEM_CONFIGS.get(key, ""))

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

from __future__ import annotations

import asyncio
import http.client
import json
import os
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from checkin_core.keychain import (
    FileCredentials,
    KeychainCredentials,
    SERVICE,
    credential_store_from_env,
    make_ref,
)
from checkin_core.store import SCHEMA_VERSION, SystemStore
from checkin_core.web import CheckinWebApp
import checkin_core.web as web_module
from checkin_core.frontend import FrontendHandler, serve as serve_frontend


class FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class StoreTests(unittest.TestCase):
    def test_obsidian_import_and_independent_pending_tasks(self):
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            count = store.import_obsidian_tasks(
                "2026-08-04",
                [("A", "https://a.example/checkin", False), ("B", "https://b.example", True)],
                "/missing/obsidian.md",
            )
            self.assertEqual(count, 2)
            self.assertEqual([x["name"] for x in store.tasks("2026-08-04", pending_only=True)], ["A"])
            store.set_task_result("2026-08-04", "A", "done")
            self.assertEqual(store.tasks("2026-08-04")[0]["status"], "done")
            # A stale/open external checkbox cannot overwrite system authority.
            store.import_obsidian_tasks(
                "2026-08-04", [("A", "https://a.example/checkin", False)], "/later.md"
            )
            self.assertEqual(store.tasks("2026-08-04")[0]["status"], "done")
            self.assertTrue((Path(td) / "system.db").is_file())

    def test_run_history_persists_structured_evidence(self):
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_task("2026-08-04", "A", "https://a.example")
            run_id = store.create_run("2026-08-04", "web", {"port": 9222})
            store.add_run_item(run_id, {
                "site": "A", "status": "OK", "stage": "confirm", "provider": "x",
                "reason": "", "latency_ms": 12,
                "action": {"kind": "dom_click"},
                "confirmation": {"kind": "dom_strong"}, "identity": None,
            })
            store.finish_run(run_id, 0, "done")
            run = store.recent_runs(1)[0]
            self.assertEqual(run["source"], "web")
            self.assertEqual(run["items"][0]["site_name"], "A")
            self.assertIn("dom_click", run["items"][0]["evidence_json"])
            self.assertIn("legacy_unknown", run["items"][0]["evidence_json"])

    def test_set_task_result_only_if_not_done_preserves_done(self):
        """Crash path guard: a committed done must survive a later failed write."""
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_task("2026-08-19", "A", "https://a.example")
            store.set_task_result("2026-08-19", "A", "done")
            # Guarded write must NOT overwrite done.
            store.set_task_result(
                "2026-08-19", "A", "failed", "crash_after_done", only_if_not_done=True
            )
            row = [t for t in store.tasks("2026-08-19") if t["name"] == "A"][0]
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["last_reason"], "")
            # Unguarded write DOES overwrite (normal path).
            store.set_task_result("2026-08-19", "A", "failed", "explicit")
            row = [t for t in store.tasks("2026-08-19") if t["name"] == "A"][0]
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["last_reason"], "explicit")

    def test_set_task_result_only_if_not_done_updates_non_done(self):
        """Guarded write still updates a pending/failed row (guard is not a no-op)."""
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_task("2026-08-19", "A", "https://a.example")
            store.set_task_result(
                "2026-08-19", "A", "failed", "boom", only_if_not_done=True
            )
            row = [t for t in store.tasks("2026-08-19") if t["name"] == "A"][0]
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["last_reason"], "boom")

    def test_complete_task_manually_marks_done_and_records_run(self):
        """complete_task_manually sets status=done, resets health, and adds a manual run record."""
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_task("2026-08-28", "SiteManual", "https://manual.example")
            store.record_site_health("SiteManual", "suppressed", "dead_url")
            res = store.complete_task_manually("2026-08-28", "SiteManual")
            self.assertTrue(res["ok"])
            tasks = store.tasks("2026-08-28")
            target = [t for t in tasks if t["name"] == "SiteManual"][0]
            self.assertEqual(target["status"], "done")
            self.assertEqual(target["site_health_status"], "active")
            runs = store.recent_runs(1)
            self.assertEqual(runs[0]["source"], "manual")
            self.assertEqual(runs[0]["items"][0]["site_name"], "SiteManual")
            self.assertEqual(runs[0]["items"][0]["status"], "OK")

            # Day validation prevents directory traversal
            with self.assertRaisesRegex(ValueError, "invalid day format"):
                store.complete_task_manually("../../../evil", "SiteManual")

            # Obsidian path outside vault root is ignored without raising error or touching the file
            evil_file = Path(td) / "evil.txt"
            evil_file.write_text("- [ ] #task #日常 [SiteManual](https://manual.example)")
            with store.connect() as db:
                db.execute(
                    "UPDATE daily_tasks SET obsidian_path=? WHERE day='2026-08-28'",
                    (str(evil_file),),
                )
            res = store.complete_task_manually("2026-08-28", "SiteManual")
            self.assertTrue(res["ok"])
            # The external evil file remains unchanged because it was outside vault root
            self.assertIn("- [ ]", evil_file.read_text())

    def test_complete_task_manually_toggles_back_to_pending(self):
        """Calling complete_task_manually again on an already-done task reverts it to pending (未打卡)."""
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_task("2026-08-28", "SiteToggle", "https://toggle.example")

            # First call marks done
            res = store.complete_task_manually("2026-08-28", "SiteToggle")
            self.assertEqual(res["status"], "done")
            self.assertEqual(res["action"], "mark_done")
            tasks = store.tasks("2026-08-28")
            target = [t for t in tasks if t["name"] == "SiteToggle"][0]
            self.assertEqual(target["status"], "done")

            # Second call toggles back to pending
            res2 = store.complete_task_manually("2026-08-28", "SiteToggle")
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["status"], "pending")
            self.assertEqual(res2["action"], "revert")
            tasks2 = store.tasks("2026-08-28")
            target2 = [t for t in tasks2 if t["name"] == "SiteToggle"][0]
            self.assertEqual(target2["status"], "pending")
            self.assertEqual(target2["last_reason"], "manual_reverted")

            # Third call marks done again (toggle keeps working)
            res3 = store.complete_task_manually("2026-08-28", "SiteToggle")
            tasks3 = store.tasks("2026-08-28")
            target3 = [t for t in tasks3 if t["name"] == "SiteToggle"][0]
            self.assertEqual(target3["status"], "done")

    def test_import_obsidian_tasks_preserves_non_legacy_provider(self):
        """Regression: import must not clobber provider set by another writer."""
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_site("W", "https://w.example", provider="wisart")
            store.import_obsidian_tasks(
                "2026-08-19", [("W", "https://w.example", False)], "/obs.md"
            )
            row = [t for t in store.tasks("2026-08-19") if t["name"] == "W"][0]
            self.assertEqual(row["provider"], "wisart")
            # New site from import still gets the default provider.
            store.import_obsidian_tasks(
                "2026-08-19", [("N", "https://n.example", False)], "/obs.md"
            )
            row = [t for t in store.tasks("2026-08-19") if t["name"] == "N"][0]
            self.assertEqual(row["provider"], "legacy_browser")

    def test_import_obsidian_tasks_respects_catalog_authoritative_urls(self):
        """Regression 2026-11-13: import must not roll a freshly updated catalog
        URL back to the stale note-row URL.

        Real failure: apikey.casa was re-adapted (/profile → /redeem) in yaml +
        DB, then the daily import of the 11-13 task file (seeded from 11-12
        rows still carrying /profile) overwrote sites.url back to /profile
        (ON CONFLICT url CASE). resolve_site honors prefer_catalog_url so
        navigation stayed correct, but the panel showed the dead URL and every
        future task-file seed inherited the stale row URL.
        Catalog-authoritative sites (prefer_catalog_url) must win over note
        rows — same precedence resolve_site already applies at navigation.
        """
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_site("K", "https://k.example/redeem", provider="legacy_browser")
            # Note row still carries the old URL (stale seed chain).
            store.import_obsidian_tasks(
                "2026-11-13", [("K", "https://k.example/profile", False)], "/obs.md",
                catalog_urls={"K": "https://k.example/redeem"},
            )
            with store.connect() as db:
                url = db.execute("SELECT url FROM sites WHERE name='K'").fetchone()[0]
            self.assertEqual(url, "https://k.example/redeem")
            # A non-catalog site keeps the existing note-URL-wins contract.
            store.upsert_site("Z", "https://z.example/old")
            store.import_obsidian_tasks(
                "2026-11-13", [("Z", "https://z.example/new", False)], "/obs.md",
                catalog_urls={"K": "https://k.example/redeem"},
            )
            with store.connect() as db:
                url = db.execute("SELECT url FROM sites WHERE name='Z'").fetchone()[0]
            self.assertEqual(url, "https://z.example/new")
            # New sites from import are inserted with the note URL as before.
            store.import_obsidian_tasks(
                "2026-11-13", [("N", "https://n.example", False)], "/obs.md",
                catalog_urls={"K": "https://k.example/redeem"},
            )
            row = [t for t in store.tasks("2026-11-13") if t["name"] == "N"][0]
            self.assertEqual(row["name"], "N")

    def test_connect_rolls_back_partial_writes_on_exception(self):
        """A raise inside the with-block must not persist earlier statements."""
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_site("A", "https://a.example")
            try:
                with store.connect() as db:
                    db.execute("UPDATE sites SET url='https://evil.example' WHERE name='A'")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            with store.connect() as db:
                url = db.execute("SELECT url FROM sites WHERE name='A'").fetchone()[0]
            self.assertEqual(url, "https://a.example")

    def test_next_day_materializes_from_system_catalog_without_obsidian(self):
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_site("A", "https://a.example")
            self.assertEqual(store.materialize_day("2026-08-05"), 1)
            tasks = store.tasks("2026-08-05", pending_only=True)
            self.assertEqual([(x["name"], x["source"]) for x in tasks], [("A", "system")])

    def test_site_health_suppresses_repeated_deterministic_failure_and_recovers(self):
        with TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_task("2026-08-04", "A", "https://a.example")
            store.record_site_health("A", "FAIL", "dead_url")
            self.assertEqual(store.tasks("2026-08-04")[0]["site_health_status"], "active")
            store.record_site_health("A", "FAIL", "dead_url")
            row = store.tasks("2026-08-04")[0]
            self.assertEqual(row["site_health_status"], "suppressed")
            self.assertEqual(row["site_health_failures"], 2)
            store.record_site_health("A", "ALREADY", "")
            self.assertEqual(store.tasks("2026-08-04")[0]["site_health_status"], "active")
            store.record_site_health("A", "FAIL", "dead_url")
            store.record_site_health("A", "FAIL", "timeout")
            row = store.tasks("2026-08-04")[0]
            self.assertEqual(row["site_health_status"], "active")
            self.assertEqual(row["site_health_failures"], 0)
            self.assertEqual(row["site_health_reason"], "")

    def test_site_health_migration_adds_table_to_existing_schema(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "system.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE sites(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'legacy_browser', enabled INTEGER NOT NULL DEFAULT 1,
                    credential_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE daily_tasks(
                    day TEXT NOT NULL, site_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    source TEXT NOT NULL DEFAULT 'system', obsidian_path TEXT, obsidian_name TEXT,
                    last_reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(day, site_id)
                );
                INSERT INTO meta(key,value) VALUES('schema_version','2');
            """)
            db.commit()
            db.close()
            store = SystemStore(path)
            store.upsert_task("2026-08-04", "A", "https://a.example")
            store.record_site_health("A", "FAIL", "dead_url")
            self.assertEqual(store.tasks("2026-08-04")[0]["site_health_status"], "active")

    def test_migration_adds_missing_column_and_tables_to_old_schema(self):
        """Truly-old DB (no credential_ref column, no site_health/credentials
        tables) must be upgraded by migrate(): ALTER adds the column, and the
        CREATE TABLE IF NOT EXISTS block adds the missing tables."""
        with TemporaryDirectory() as td:
            path = Path(td) / "system.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE sites(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'legacy_browser', enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE daily_tasks(
                    day TEXT NOT NULL, site_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    source TEXT NOT NULL DEFAULT 'system', obsidian_path TEXT, obsidian_name TEXT,
                    last_reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(day, site_id)
                );
                CREATE TABLE runs(
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, exit_code INTEGER,
                    reason TEXT NOT NULL DEFAULT '', cdp_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE run_items(
                    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, site_id INTEGER,
                    site_name TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER NOT NULL DEFAULT 0, evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, site_names_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, exit_code INTEGER, message TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO meta(key,value) VALUES('schema_version','1');
            """)
            db.commit()
            db.close()
            store = SystemStore(path)
            # Sites column must have been added by _migrate_column.
            store.upsert_task("2026-08-04", "A", "https://a.example")
            store.set_credential_ref("A", "ref-A", "token")
            refs = store.credential_refs()
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["ref"], "ref-A")
            self.assertEqual(refs[0]["site"], "A")
            # site_health + credentials tables must have been created.
            store.record_site_health("A", "OK", "")
            row = store.tasks("2026-08-04")[0]
            self.assertEqual(row["site_health_status"], "active")
            # schema_version is bumped and INSERTs work.
            with store.connect() as db:
                version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(version, str(SCHEMA_VERSION))

    def test_upsert_task_rolls_back_site_when_task_insert_fails(self):
        """Fix 1 regression: the site upsert and task INSERT share one
        transaction. If the task INSERT fails, the site row must be rolled back
        rather than committed in isolation (previously upsert_site committed on
        its own connection, leaving a stray site row)."""
        with TemporaryDirectory() as td:
            path = Path(td) / "system.db"
            db = sqlite3.connect(path)
            # A CHECK that can never pass makes the task INSERT fail after the
            # site INSERT has already run on the same connection.
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE sites(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'legacy_browser', enabled INTEGER NOT NULL DEFAULT 1,
                    credential_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE daily_tasks(
                    day TEXT NOT NULL, site_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    source TEXT NOT NULL DEFAULT 'system', obsidian_path TEXT, obsidian_name TEXT,
                    last_reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(day, site_id),
                    CHECK(day <> day)
                );
                INSERT INTO meta(key,value) VALUES('schema_version','3');
            """)
            db.commit()
            db.close()
            store = SystemStore(path)
            with self.assertRaises(sqlite3.IntegrityError):
                store.upsert_task("2026-08-04", "B", "https://b.example")
            with store.connect() as db:
                n = db.execute("SELECT COUNT(*) FROM sites WHERE name='B'").fetchone()[0]
            self.assertEqual(n, 0)

    def test_set_credential_ref_rolls_back_site_when_credential_insert_fails(self):
        """Fix 1 regression: set_credential_ref must not commit the site row if
        the credential INSERT fails mid-transaction."""
        with TemporaryDirectory() as td:
            path = Path(td) / "system.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE sites(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'legacy_browser', enabled INTEGER NOT NULL DEFAULT 1,
                    credential_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE credentials(
                    ref TEXT PRIMARY KEY, site_id INTEGER, kind TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    CHECK(ref <> ref)
                );
                INSERT INTO meta(key,value) VALUES('schema_version','3');
            """)
            db.commit()
            db.close()
            store = SystemStore(path)
            with self.assertRaises(sqlite3.IntegrityError):
                store.set_credential_ref("C", "ref-C", "token")
            with store.connect() as db:
                n = db.execute("SELECT COUNT(*) FROM sites WHERE name='C'").fetchone()[0]
            self.assertEqual(n, 0)

    def test_credential_db_contains_reference_not_secret(self):
        with TemporaryDirectory() as td:
            db_path = Path(td) / "system.db"
            store = SystemStore(db_path)
            ref = make_ref("A", "token")
            store.set_credential_ref("A", ref, "token", "primary")
            blob = db_path.read_bytes()
            self.assertIn(ref.encode(), blob)
            self.assertNotIn(b"actual-secret-value", blob)
            row = store.tasks("2026-08-04")
            self.assertEqual(row, [])

    def test_record_site_balance_updates_snapshot_and_appends_history(self):
        """RFC-043: record_site_balance sets latest balance and appends to account_balances."""
        with TemporaryDirectory() as td:
            db_path = Path(td) / "system.db"
            store = SystemStore(db_path)
            store.upsert_task("2026-10-10", "SiteA", "https://sitea.example")

            # Initial task list has no balance
            tasks = store.tasks("2026-10-10")
            self.assertEqual(len(tasks), 1)
            self.assertIsNone(tasks[0]["last_balance"])
            self.assertEqual(tasks[0]["last_balance_text"], "")

            # Record balance
            store.record_site_balance("SiteA", "2026-10-10", {
                "amount": 15.2,
                "raw_text": "$15.20",
                "currency": "USD",
                "source": "api",
            })

            # Check snapshot in tasks(day)
            tasks_updated = store.tasks("2026-10-10")
            self.assertEqual(tasks_updated[0]["last_balance"], 15.2)
            self.assertEqual(tasks_updated[0]["last_balance_text"], "$15.20")
            self.assertEqual(tasks_updated[0]["last_balance_currency"], "USD")
            self.assertTrue(tasks_updated[0]["balance_updated_at"])

            # Check history in account_balances table
            with store.connect() as db:
                rows = db.execute("SELECT amount, raw_text, currency, source FROM account_balances WHERE day='2026-10-10'").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["amount"], 15.2)
                self.assertEqual(rows[0]["raw_text"], "$15.20")
                self.assertEqual(rows[0]["currency"], "USD")
                self.assertEqual(rows[0]["source"], "api")

            # P2: Repeated record_site_balance updates the existing row via ON CONFLICT without bloating
            store.record_site_balance("SiteA", "2026-10-10", {
                "amount": 16.5,
                "raw_text": "$16.50",
                "currency": "USD",
                "source": "api",
            })
            with store.connect() as db:
                rows_repeat = db.execute("SELECT amount, raw_text FROM account_balances WHERE day='2026-10-10'").fetchall()
                self.assertEqual(len(rows_repeat), 1)
                self.assertEqual(rows_repeat[0]["amount"], 16.5)
                self.assertEqual(rows_repeat[0]["raw_text"], "$16.50")

    def test_get_site_balance_history_and_update_manual(self):
        """RFC-043: get_site_balance_history returns ledger rows; update_site_balance_manual records adjustment."""
        with TemporaryDirectory() as td:
            db_path = Path(td) / "system.db"
            store = SystemStore(db_path)
            store.upsert_task("2026-10-10", "SiteLedger", "https://ledger.example")

            # Nonexistent site returns empty history and raises on manual update
            self.assertEqual(store.get_site_balance_history("NonExistent"), [])
            with self.assertRaises(ValueError):
                store.update_site_balance_manual("NonExistent", 10.0)

            # Record manual balance
            res = store.update_site_balance_manual("SiteLedger", 28.5, currency="USD", day="2026-10-10")
            self.assertEqual(res["amount"], 28.5)
            self.assertEqual(res["currency"], "USD")
            self.assertEqual(res["raw_text"], "$28.50")
            self.assertEqual(res["source"], "manual")

            # Snapshot updated
            tasks = store.tasks("2026-10-10")
            self.assertEqual(tasks[0]["last_balance"], 28.5)
            self.assertEqual(tasks[0]["last_balance_text"], "$28.50")

            # History query
            hist = store.get_site_balance_history("SiteLedger")
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["day"], "2026-10-10")
            self.assertEqual(hist[0]["amount"], 28.5)
            self.assertEqual(hist[0]["source"], "manual")

    def test_add_run_item_persists_balance_in_evidence_json(self):
        """RFC-043: run_items.evidence_json includes serialized balance evidence."""
        with TemporaryDirectory() as td:
            db_path = Path(td) / "system.db"
            store = SystemStore(db_path)
            store.upsert_task("2026-10-10", "SiteB", "https://siteb.example")
            run_id = store.create_run("2026-10-10", "cli")
            store.add_run_item(run_id, {
                "site": "SiteB",
                "status": "OK",
                "stage": "confirm",
                "provider": "legacy_browser",
                "reason": "",
                "latency_ms": 150,
                "action": {"kind": "dom_click"},
                "confirmation": {"kind": "dom_strong"},
                "balance": {
                    "amount": 25.0,
                    "raw_text": "$25.00",
                    "currency": "USD",
                    "source": "api",
                },
            })
            store.finish_run(run_id, 0, "done")
            runs = store.recent_runs(1)
            item = runs[0]["items"][0]
            evidence = json.loads(item["evidence_json"])
            self.assertIn("balance", evidence)
            self.assertEqual(evidence["balance"]["amount"], 25.0)
            self.assertEqual(evidence["balance"]["raw_text"], "$25.00")

    def test_extract_dom_balance_currency_detection(self):
        """RFC-043 Finding 3: Suffix units ('元', '刀') and symbols are correctly parsed."""
        from unittest.mock import AsyncMock
        from stealth_checkin_runner import _extract_dom_balance

        # 1. Chinese Yuan suffix
        page_cny = AsyncMock()
        page_cny.evaluate = AsyncMock(return_value={"raw": "当前余额: 12.50 元", "val": "12.50 元"})
        b_cny = asyncio.run(_extract_dom_balance(page_cny))
        self.assertIsNotNone(b_cny)
        self.assertEqual(b_cny.amount, 12.5)
        self.assertEqual(b_cny.currency, "CNY")

        # 2. USD dollar suffix
        page_usd = AsyncMock()
        page_usd.evaluate = AsyncMock(return_value={"raw": "可用额度: 30.00 刀", "val": "30.00 刀"})
        b_usd = asyncio.run(_extract_dom_balance(page_usd))
        self.assertIsNotNone(b_usd)
        self.assertEqual(b_usd.amount, 30.0)
        self.assertEqual(b_usd.currency, "USD")

        # 3. Points suffix
        page_pts = AsyncMock()
        page_pts.evaluate = AsyncMock(return_value={"raw": "剩余积分: 500 积分", "val": "500 积分", "type": "points"})
        b_pts = asyncio.run(_extract_dom_balance(page_pts))
        self.assertIsNotNone(b_pts)
        self.assertEqual(b_pts.amount, 500.0)
        self.assertEqual(b_pts.currency, "POINTS")

    def test_confirm_signal_line_keeps_reward_context(self):
        """pap.pmcat.top Finding: toast line 「签到成功，获得 $0.106」 carries the reward
        amount; ok_result must keep the full line (not just the matched snippet) so
        _enrich_result_balance can parse reward_text."""
        from stealth_checkin_runner import (
            _extract_reward_from_text,
            confirm_signal_line,
            reward_context_summary,
        )

        page_text = "签到成功，获得 $0.106\n今日已签到\n已连续签到 1 天 · 累计获得 $0.106"
        line = confirm_signal_line(page_text, "签到成功")
        self.assertEqual(line, "签到成功，获得 $0.106")
        self.assertEqual(_extract_reward_from_text(line), "+$0.106")

        # prefer the reward-bearing line when the confirm word repeats across
        # lines (review P3: first-hit could pick a reward-free legend line)
        repeated = "签到成功规则：签到成功后领取\n签到成功，获得 $0.106"
        self.assertEqual(confirm_signal_line(repeated, "签到成功"), "签到成功，获得 $0.106")

        # weak "today+weak" signal and missing-line fallback pass through unchanged
        self.assertEqual(confirm_signal_line("已签到 1 天", "today+weak"), "today+weak")
        self.assertEqual(confirm_signal_line("nothing here", "签到成功"), "签到成功")

        # cumulative totals on other lines must NOT leak into the reward
        self.assertEqual(_extract_reward_from_text("已连续签到 1 天 · 累计获得 $0.106"), "")

    def test_reward_context_summary_closes_toast_timing_window(self):
        """Review P3: 「今日已签到」 is matched first in STRONG_SUCCESS_PATTERNS, so sig
        can resolve from the flipped button line while the reward toast still sits on
        another line of the merged text; reward_context_summary must salvage it."""
        from stealth_checkin_runner import _extract_reward_from_text, reward_context_summary

        text = "签到成功，获得 $0.106\n今日已签到\n已连续签到 1 天 · 累计获得 $0.106"
        summary = reward_context_summary(text, "今日已签到")
        self.assertEqual(summary, "今日已签到\n签到成功，获得 $0.106")
        self.assertEqual(_extract_reward_from_text(summary), "+$0.106")

        # no reward line anywhere (toast expired) → unchanged; cumulative line never appended
        self.assertEqual(reward_context_summary("今日已签到\n已连续签到 1 天 · 累计获得 $0.106", "今日已签到"), "今日已签到")

        # summary already carrying a reward → returned as-is
        self.assertEqual(reward_context_summary(text, "签到成功，获得 $0.106"), "签到成功，获得 $0.106")

        # output never exceeds dom_confirmation's 160-char cap
        long_text = "x" * 200 + "\n签到成功，获得 $0.106"
        self.assertLessEqual(len(reward_context_summary(long_text, "今日已签到")), 160)

    def test_dom_extractor_and_toast_selector_regression_anchors(self):
        """Review P3: DOM extractor JS is inline (evaluate mocks bypass it); lock the
        sibling-card grandparent hop and the pap-style snack toast selector against
        silent regression."""
        import inspect
        from stealth_checkin_runner import _extract_dom_balance, page_text

        extractor_src = inspect.getsource(_extract_dom_balance)
        self.assertIn("let hop = node.parentElement", extractor_src)
        self.assertIn("i === 0", extractor_src)
        self.assertIn("grandparent", extractor_src)

        page_text_src = inspect.getsource(page_text)
        self.assertIn(".snack", page_text_src)

    def test_dialog_dismiss_js_rules(self):
        """Ensure _DIALOG_DISMISS_JS covers semantic dialogs, Tailwind fixed overlays and guards."""
        from stealth_checkin_runner import _DIALOG_DISMISS_JS
        self.assertIn('data-slot="dialog-close"', _DIALOG_DISMISS_JS)
        self.assertIn('.snowapi-dialog-content', _DIALOG_DISMISS_JS)
        self.assertIn('role="dialog"', _DIALOG_DISMISS_JS)
        self.assertIn('div.fixed.inset-0', _DIALOG_DISMISS_JS)
        self.assertIn('aria-label*="关闭"', _DIALOG_DISMISS_JS)
        self.assertIn('全部已读', _DIALOG_DISMISS_JS)
        self.assertIn('玩法说明', _DIALOG_DISMISS_JS)
        self.assertIn('公告', _DIALOG_DISMISS_JS)
        self.assertIn('通知', _DIALOG_DISMISS_JS)
        self.assertIn('data-dismissed', _DIALOG_DISMISS_JS)
        self.assertIn('立即签到', _DIALOG_DISMISS_JS)
        self.assertIn('今日已签', _DIALOG_DISMISS_JS)
        self.assertIn('摸猫', _DIALOG_DISMISS_JS)
        self.assertIn('hasActiveAction', _DIALOG_DISMISS_JS)
        self.assertIn('hasStatusAction', _DIALOG_DISMISS_JS)
        # 验证已消除对正文含每日签到与积分的过度跳过 (P2 根因防护)
        self.assertNotIn('/每日签到/.test(text) && /签到|领取|积分/.test(text)', _DIALOG_DISMISS_JS)

    def test_ilovecat_adapter_overview_resolution(self):
        """Ensure ilovecat resolves /dashboard/overview without forced override to /playground."""
        from stealth_checkin_runner import resolve_site, is_ilovecat_site
        self.assertTrue(is_ilovecat_site("https://api.ilovecat520.me/dashboard/overview"))
        self.assertTrue(is_ilovecat_site("https://api.ilovecat520.me/playground"))

        adapter = resolve_site("靴貓公益站", "https://api.ilovecat520.me/dashboard/overview")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.kind, "ilovecat")
        self.assertEqual(adapter.url, "https://api.ilovecat520.me/dashboard/overview")

    def test_dismiss_obstructing_dialogs_loop(self):
        """Ensure dismiss_obstructing_dialogs handles dialog dismissal and returns count."""
        import io
        from unittest.mock import AsyncMock, patch
        from stealth_checkin_runner import dismiss_obstructing_dialogs

        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=1)
        with patch("sys.stdout", new_callable=io.StringIO) as fake_out:
            closed = asyncio.run(dismiss_obstructing_dialogs(page, stage="post_load"))
            self.assertEqual(closed, 1)
            self.assertIn("dismissed 1 obstruction dialog(s) [post_load]", fake_out.getvalue())
        self.assertEqual(page.evaluate.call_count, 1)

        # Default stage and non-callable or empty object returns 0 without raising
        with patch("sys.stdout", new_callable=io.StringIO) as fake_out2:
            closed_default = asyncio.run(dismiss_obstructing_dialogs(page))
            self.assertEqual(closed_default, 1)
            self.assertIn("dismissed 1 obstruction dialog(s) [interaction]", fake_out2.getvalue())
        self.assertEqual(asyncio.run(dismiss_obstructing_dialogs(None)), 0)
        self.assertEqual(asyncio.run(dismiss_obstructing_dialogs(object())), 0)


class KeychainTests(unittest.TestCase):
    def test_keychain_commands_and_errors_never_return_secret(self):
        calls = []
        inputs = []
        def runner(args, **kwargs):
            calls.append(args)
            inputs.append(kwargs.get("input"))
            return FakeCompleted(0, "secret-from-keychain\n" if "find-generic-password" in args else "")
        kc = KeychainCredentials(runner)
        kc.put("ref", "super-secret")
        self.assertEqual(kc.get("ref"), "secret-from-keychain")
        kc.delete("ref")
        self.assertEqual(calls[0][0:2], ["security", "add-generic-password"])
        self.assertIn(SERVICE, calls[0])
        self.assertNotIn("super-secret", calls[0])
        self.assertEqual(inputs[0], "super-secret\nsuper-secret\n")

        def failing(args, **kwargs):
            return FakeCompleted(1, "")
        with self.assertRaisesRegex(RuntimeError, "Keychain write failed") as ctx:
            KeychainCredentials(failing).put("ref", "do-not-leak")
        self.assertNotIn("do-not-leak", str(ctx.exception))


class FileCredentialsTests(unittest.TestCase):
    def test_roundtrip_get_delete(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "credentials.json"
            store = FileCredentials(path)
            store.put("daily-checkin:abc", "super-secret")
            self.assertEqual(store.get("daily-checkin:abc"), "super-secret")
            store.delete("daily-checkin:abc")
            self.assertIsNone(store.get("daily-checkin:abc"))

    @unittest.skipIf(sys.platform == "win32", "POSIX file permission mode 0600/0700 not applicable on Windows")
    def test_file_and_dir_mode_0600_0700(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "sub" / "credentials.json"
            store = FileCredentials(path)
            store.put("ref", "super-secret")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_write_failure_never_leaks_secret(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "credentials.json"
            store = FileCredentials(path)
            # Force an unwritable path: make the file a directory so the open
            # for writing raises without ever echoing the secret.
            store.put("a", "leak-me")
            path.unlink()
            path.mkdir()  # now a directory; open in _write must fail
            with self.assertRaises(Exception) as ctx:
                store.put("b", "leak-me")
            self.assertNotIn("leak-me", str(ctx.exception))

    def test_put_rejects_empty(self):
        with TemporaryDirectory() as td:
            store = FileCredentials(Path(td) / "credentials.json")
            with self.assertRaisesRegex(ValueError, "required"):
                store.put("ref", "")
            with self.assertRaisesRegex(ValueError, "required"):
                store.put("", "s")

    def test_get_ignores_missing_or_corrupt_file(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "credentials.json"
            store = FileCredentials(path)
            self.assertIsNone(store.get("k"))
            path.write_text("not json{{{", encoding="utf-8")
            self.assertIsNone(store.get("k"))

    def test_multiple_refs_coexist(self):
        with TemporaryDirectory() as td:
            store = FileCredentials(Path(td) / "credentials.json")
            store.put("k1", "s1")
            store.put("k2", "s2")
            self.assertEqual(store.get("k1"), "s1")
            self.assertEqual(store.get("k2"), "s2")
            store.delete("k1")
            self.assertIsNone(store.get("k1"))
            self.assertEqual(store.get("k2"), "s2")

    def test_corrupt_file_then_put_does_not_destroy_existing(self):
        # A transiently-corrupted credential file must NOT be silently truncated
        # away by a subsequent put; the write path fails closed instead.
        with TemporaryDirectory() as td:
            path = Path(td) / "credentials.json"
            store = FileCredentials(path)
            store.put("keep-me", "secret-1")
            # Corrupt the file out from under the store.
            path.write_text("not json{{{", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "corrupt|unreadable"):
                store.put("new", "secret-2")
            # The corrupted (not wiped) bytes survive; a brand-new empty store
            # would have lost secret-1 entirely via O_TRUNC.
            self.assertIn("not json", path.read_text(encoding="utf-8"))

    @unittest.skipIf(sys.platform == "win32", "POSIX directory permission checking not applicable on Windows")
    def test_put_refuses_when_parent_not_private(self):
        # If the credential directory is world/group-writable (not 0700 self-owned)
        # at put time, put must refuse rather than write into a looser directory.
        # __init__ forces 0700, so we relax the parent *after* construction to
        # simulate a post-init permission drift (defense-in-depth guard).
        with TemporaryDirectory() as td:
            cred_dir = Path(td) / "cred"
            store = FileCredentials(cred_dir / "credentials.json")
            cred_dir.chmod(0o755)  # relax after init
            with self.assertRaisesRegex(RuntimeError, "not private"):
                store.put("ref", "s")


class CredentialStoreFactoryTests(unittest.TestCase):
    """Guards the two security-relevant factory rules the refactor claims:
    no silent downgrade, and explicit backend selection."""

    def setUp(self):
        # snapshot env so each test starts clean
        self._env = {k: os.environ.get(k) for k in (
            "DAILY_CHECKIN_CREDENTIAL_BACKEND",
            "DAILY_CHECKIN_CREDENTIALS_FILE",
            "DAILY_CHECKIN_CREDENTIALS_DIR",
        )}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_unknown_backend_raises_not_fallback(self):
        os.environ["DAILY_CHECKIN_CREDENTIAL_BACKEND"] = "bogus"
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            credential_store_from_env()

    def test_credentials_file_takes_precedence_over_dir(self):
        with TemporaryDirectory() as td:
            explicit = Path(td) / "explicit.json"
            os.environ["DAILY_CHECKIN_CREDENTIAL_BACKEND"] = "file"
            os.environ["DAILY_CHECKIN_CREDENTIALS_DIR"] = str(Path(td) / "ignored-dir")
            os.environ["DAILY_CHECKIN_CREDENTIALS_FILE"] = str(explicit)
            store = credential_store_from_env()
            self.assertEqual(Path(store.path).resolve(), explicit.resolve())

    def test_dir_used_when_file_absent(self):
        with TemporaryDirectory() as td:
            d = Path(td) / "cred-dir"
            os.environ["DAILY_CHECKIN_CREDENTIAL_BACKEND"] = "file"
            os.environ["DAILY_CHECKIN_CREDENTIALS_DIR"] = str(d)
            store = credential_store_from_env()
            self.assertEqual(Path(store.path).resolve(), (d / "credentials.json").resolve())

    def test_credentials_dir_stripped_of_whitespace(self):
        with TemporaryDirectory() as td:
            d = Path(td) / "cred-dir"
            os.environ["DAILY_CHECKIN_CREDENTIAL_BACKEND"] = "file"
            os.environ["DAILY_CHECKIN_CREDENTIALS_DIR"] = "  " + str(d) + "  "
            store = credential_store_from_env()
            self.assertEqual(Path(store.path).resolve(), (d / "credentials.json").resolve())

    def test_keychain_backend_refused_on_non_darwin(self):
        import checkin_core.keychain as kc
        original = kc.sys.platform
        kc.sys.platform = "linux"
        try:
            os.environ["DAILY_CHECKIN_CREDENTIAL_BACKEND"] = "keychain"
            with self.assertRaisesRegex(RuntimeError, "unavailable on this platform"):
                credential_store_from_env()
        finally:
            kc.sys.platform = original

    def test_default_is_file_backend_on_non_darwin(self):
        import checkin_core.keychain as kc
        original = kc.sys.platform
        kc.sys.platform = "linux"
        with TemporaryDirectory() as td:
            os.environ["DAILY_CHECKIN_CREDENTIALS_DIR"] = td
            try:
                store = credential_store_from_env()
                self.assertIsInstance(store, FileCredentials)
            finally:
                kc.sys.platform = original


class FakeCredentials:
    def __init__(self):
        self.values = {}
    def put(self, ref, secret):
        self.values[ref] = secret
    def delete(self, ref):
        self.values.pop(ref, None)


class WebTests(unittest.TestCase):
    def setUp(self):
        self._orig_password = os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)
        self.td = TemporaryDirectory()
        self.store = SystemStore(Path(self.td.name) / "system.db")
        self.creds = FakeCredentials()
        self.app = CheckinWebApp(self.store, self.creds)
        self.server = __import__("http.server").server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.app.handler()
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.td.cleanup()
        if self._orig_password is not None:
            os.environ["DAILY_CHECKIN_WEB_PASSWORD"] = self._orig_password
        else:
            os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)

    def request_json(self, method, path, body=None, csrf=None, auth=None):
        headers = {"Content-Type": "application/json"}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if auth is not None:
            headers["Authorization"] = "Bearer " + auth
        self.conn.request(method, path, json.dumps(body or {}).encode(), headers)
        response = self.conn.getresponse()
        data = json.loads(response.read())
        return response.status, data

    def test_frontend_and_state_api(self):
        # The backend (8765) is API-only since the split; static pages are served
        # by the separate frontend process (8766). So "/" here is 404, not HTML.
        self.conn.request("GET", "/")
        response = self.conn.getresponse()
        response.read()
        self.assertEqual(response.status, 404)
        status, state = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        self.assertEqual(status, 200)
        self.assertTrue(state["csrf"])
        self.assertIn("tasks", state)
        self.assertIn("credentials", state)

    def test_host_header_is_loopback_only(self):
        self.conn.request("GET", "/api/state", headers={"Host": "evil.example"})
        response = self.conn.getresponse()
        response.read()
        self.assertEqual(response.status, 403)

    def test_host_header_allows_local_check_domains(self):
        """hosts-file local domains used by the production UI must pass Host checks."""
        for host in (
            "check.mangoqwq.com:8765",
            "check-api.mangoqwq.com:8765",
            "check.example.com:8765",
        ):
            self.conn.request(
                "GET",
                "/api/state",
                headers={"Host": host, "Authorization": "Bearer " + self.app.auth_token},
            )
            response = self.conn.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200, host)
            self.assertIn("tasks", body)

    def test_api_state_requires_bearer_token(self):
        status, data = self.request_json("GET", "/api/state")
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "auth")

    def test_api_state_rejects_wrong_token(self):
        status, _ = self.request_json("GET", "/api/state", auth="wrong-token")
        self.assertEqual(status, 401)

    def test_password_gate_accepts_configured_password(self):
        import os
        os.environ["DAILY_CHECKIN_WEB_PASSWORD"] = "s3cret"
        try:
            # Wrong password → 401.
            wrong = self.conn.request("GET", "/api/state", headers={"X-DailyCheckin-Password": "nope"})
            self.assertEqual(self.conn.getresponse().status, 401)
            # Correct password → 200 + csrf.
            self.conn.request("GET", "/api/state", headers={"X-DailyCheckin-Password": "s3cret"})
            response = self.conn.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertTrue(body["csrf"])
        finally:
            os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)

    def test_post_requires_bearer_token_before_csrf(self):
        # Auth is checked before CSRF: without a token the request must 401,
        # not 403, so a token-less caller can never learn the CSRF token.
        status, data = self.request_json("POST", "/api/run", {"sites": ["A"]})
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "auth")

    def test_cors_allows_configured_origin(self):
        import os
        os.environ["DAILY_CHECKIN_WEB_PASSWORD"] = "p"
        os.environ["DAILY_CHECKIN_WEB_ORIGINS"] = "http://127.0.0.1:8766,http://localhost:8766"
        try:
            # GET with a matching Origin gets ACAO + Vary, works with password.
            self.conn.request(
                "GET", "/api/state",
                headers={
                    "Origin": "http://127.0.0.1:8766",
                    "X-DailyCheckin-Password": "p",
                },
            )
            resp = self.conn.getresponse()
            body = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "http://127.0.0.1:8766")
            self.assertIn("Origin", (resp.getheader("Vary") or ""))
            self.assertTrue(body["csrf"])
        finally:
            os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)
            os.environ.pop("DAILY_CHECKIN_WEB_ORIGINS", None)

    def test_cors_rejects_unknown_origin(self):
        import os
        os.environ["DAILY_CHECKIN_WEB_PASSWORD"] = "p"
        os.environ["DAILY_CHECKIN_WEB_ORIGINS"] = "http://127.0.0.1:8766"
        try:
            # Even with the right password, a non-allow-listed Origin must not
            # get CORS headers (browser blocks the read) and must not leak data.
            self.conn.request(
                "GET", "/api/state",
                headers={"Origin": "http://evil.example", "X-DailyCheckin-Password": "p"},
            )
            resp = self.conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)  # API still 200 (auth passed)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        finally:
            os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)
            os.environ.pop("DAILY_CHECKIN_WEB_ORIGINS", None)

    def test_options_preflight_permitted_origin_only(self):
        import os
        os.environ["DAILY_CHECKIN_WEB_PASSWORD"] = "p"
        os.environ["DAILY_CHECKIN_WEB_ORIGINS"] = "http://127.0.0.1:8766"
        try:
            # Preflight with allowed origin → 204 + Allow-Methods/Headers.
            self.conn.request(
                "OPTIONS", "/api/run",
                headers={
                    "Origin": "http://127.0.0.1:8766",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "x-dailycheckin-password,x-csrf-token,content-type",
                },
            )
            resp = self.conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.getheader("Access-Control-Allow-Methods"), "GET, POST, OPTIONS")
            self.assertIn("X-DailyCheckin-Password", resp.getheader("Access-Control-Allow-Headers") or "")
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "http://127.0.0.1:8766")
            # Unknown origin preflight → 403 and no ACAO.
            self.conn.request(
                "OPTIONS", "/api/run",
                headers={
                    "Origin": "http://attacker.invalid",
                    "Access-Control-Request-Method": "POST",
                },
            )
            resp = self.conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 403)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        finally:
            os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)
            os.environ.pop("DAILY_CHECKIN_WEB_ORIGINS", None)

    def test_default_cors_allows_mangoqwq_frontend_origin(self):
        """Production UI is http://check.mangoqwq.com:8766; default CORS must include it."""
        import os
        os.environ.pop("DAILY_CHECKIN_WEB_ORIGINS", None)
        os.environ["DAILY_CHECKIN_WEB_PASSWORD"] = "p"
        origin = "http://check.mangoqwq.com:8766"
        try:
            self.conn.request(
                "OPTIONS",
                "/api/state",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Host": "check-api.mangoqwq.com:8765",
                },
            )
            resp = self.conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), origin)

            self.conn.request(
                "GET",
                "/api/state",
                headers={
                    "Origin": origin,
                    "Host": "check-api.mangoqwq.com:8765",
                    "X-DailyCheckin-Password": "p",
                },
            )
            resp = self.conn.getresponse()
            body = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), origin)
            self.assertIn("tasks", body)
        finally:
            os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)

    def test_api_independent_of_static_pages(self):
        # API must work without the static page routes present (frontend split).
        status, _ = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        self.assertEqual(status, 200)

    def test_post_requires_auth_before_csrf_with_password(self):
        import os
        os.environ["DAILY_CHECKIN_WEB_PASSWORD"] = "s3cret"
        try:
            status, data = self.request_json("POST", "/api/run", {"sites": ["A"]})
            self.assertEqual(status, 401)
            self.assertEqual(data["error"], "auth")
        finally:
            os.environ.pop("DAILY_CHECKIN_WEB_PASSWORD", None)

    def test_backend_is_api_only(self):
        # Since the frontend split, the backend (8765) exposes only /api/*
        # and rejects all non-API paths — static assets moved to the 8766 process.
        for path in ("/", "/index.html", "/app.js", "/style.css", "/favicon.ico"):
            resp = self.conn.request("GET", path)
            self.assertEqual(self.conn.getresponse().status, 404, path)

    @unittest.skipIf(sys.platform == "win32", "POSIX file permission mode 0600 not applicable on Windows")
    def test_token_file_created_with_0600(self):
        token_path = Path(self.td.name) / "web.token"
        self.assertTrue(token_path.is_file())
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.app.auth_token, token_path.read_text(encoding="utf-8").strip())

    def test_write_requires_csrf_and_secret_goes_only_to_fake_keychain(self):
        status, _ = self.request_json("POST", "/api/tasks", {"name": "A"}, auth=self.app.auth_token)
        self.assertEqual(status, 403)
        token = self.app.csrf
        status, _ = self.request_json(
            "POST", "/api/credentials",
            {"site": "A", "kind": "token", "secret": "web-secret", "label": "main"},
            token, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        self.assertIn("web-secret", self.creds.values.values())
        db_bytes = Path(self.td.name, "system.db").read_bytes()
        self.assertNotIn(b"web-secret", db_bytes)

    def test_run_endpoint_queues_same_executor_without_running_browser(self):
        day = datetime.now().strftime("%Y-%m-%d")
        self.store.upsert_task(day, "A", "https://a.example", status="done")
        submitted = []
        self.app.executor.submit = lambda site_names=None: submitted.append(site_names or []) or 77
        status, data = self.request_json(
            "POST", "/api/run", {"sites": ["A"]}, self.app.csrf, auth=self.app.auth_token
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["job_id"], 77)
        self.assertEqual(submitted, [["A"]])

    def test_submit_does_not_reopen_completed_task(self):
        day = datetime.now().strftime("%Y-%m-%d")
        self.store.upsert_task(day, "A", "https://a.example", status="done")
        executor = web_module.JobExecutor(self.store, start_worker=False)
        executor.submit(["A"])
        self.assertEqual(self.store.tasks(day)[0]["status"], "done")
        queued_job, queued_names = executor._queue.get_nowait()
        self.assertGreater(queued_job, 0)
        self.assertEqual(queued_names, ["A"])

    def test_runner_timeout_kills_and_waits_for_process_group(self):
        calls = []

        class TimedOutProcess:
            pid = 4242
            returncode = -signal.SIGTERM
            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(["runner"], timeout)
            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                return self.returncode

        old_popen = web_module.subprocess.Popen
        had_killpg = hasattr(web_module.os, "killpg")
        old_killpg = getattr(web_module.os, "killpg", None)
        web_module.subprocess.Popen = lambda *a, **k: TimedOutProcess()
        if sys.platform != "win32":
            web_module.os.killpg = lambda pid, sig: calls.append(("killpg", pid, sig))
        try:
            job_id = self.store.create_job("site", ["A"])
            self.app.executor._run(job_id, ["A"])
            if sys.platform != "win32":
                self.assertIn(("killpg", 4242, signal.SIGTERM), calls)
            self.assertIn(("wait", 10), calls)
            job = [j for j in self.store.jobs() if j["id"] == job_id][0]
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["exit_code"], 2)
            self.assertIn("runner_timeout", job["message"])
        finally:
            web_module.subprocess.Popen = old_popen
            if had_killpg:
                web_module.os.killpg = old_killpg
            elif hasattr(web_module.os, "killpg"):
                delattr(web_module.os, "killpg")

    def test_recover_stale_jobs_requeues_queued_rows(self):
        """After a web-process restart, persistent 'queued' jobs must be
        re-enqueued into the in-memory FIFO so they are not stranded. The DB
        row stays 'queued' until the worker picks it up, so a further crash
        before execution would still recover the same job (nothing lost)."""
        j1 = self.store.create_job("all", ["A"])
        j2 = self.store.create_job("site", ["B"])
        self.store.update_job(j2, "running")
        executor = web_module.JobExecutor(self.store, start_worker=False)
        requeued = executor.recover_stale_jobs()
        self.assertEqual(requeued, 1)  # only the queued one
        queued = []
        while True:
            try:
                queued.append(executor._queue.get_nowait())
            except queue.Empty:
                break
        self.assertEqual([(job_id, names) for job_id, names in queued], [(j1, ["A"])])
        # Running row must be untouched (must never double-trigger a live job).
        job = [j for j in self.store.jobs() if j["id"] == j2][0]
        self.assertEqual(job["status"], "running")
        # Not yet executed -> DB still queued, so a second restart re-recovers it.
        job = [j for j in self.store.jobs() if j["id"] == j1][0]
        self.assertEqual(job["status"], "queued")

    def test_api_state_materializes_day_for_ui(self):
        """GET /api/state materializes today's pending tasks from enabled sites so the UI has cards."""
        self.store.upsert_site("SiteA", "https://a.com", "browser")
        day = datetime.now().strftime("%Y-%m-%d")
        status, data = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        self.assertEqual(status, 200)
        self.assertTrue(any(t["name"] == "SiteA" for t in data["tasks"]))

    def test_api_complete_task_manually(self):
        """POST /api/tasks/complete marks a task as done and resets health."""
        self.store.upsert_task("2026-08-28", "SiteManualWeb", "https://manual.example")
        status, data = self.request_json(
            "POST", "/api/tasks/complete",
            {"name": "SiteManualWeb", "day": "2026-08-28"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertIn("obsidian_projected", data)
        tasks = self.store.tasks("2026-08-28")
        target = [t for t in tasks if t["name"] == "SiteManualWeb"][0]
        self.assertEqual(target["status"], "done")

        # Second POST toggles back to pending (未打卡)
        status, data2 = self.request_json(
            "POST", "/api/tasks/complete",
            {"name": "SiteManualWeb", "day": "2026-08-28"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data2.get("status"), "pending")
        tasks2 = self.store.tasks("2026-08-28")
        target2 = [t for t in tasks2 if t["name"] == "SiteManualWeb"][0]
        self.assertEqual(target2["status"], "pending")

    def test_claim_job_cas_prevents_duplicate_runs(self):
        """claim_job atomically transitions status from queued to running only once."""
        j1 = self.store.create_job("site", ["A"])
        # First claim succeeds
        self.assertTrue(self.store.claim_job(j1))
        # Second claim on already-running job fails
        self.assertFalse(self.store.claim_job(j1))
        # Non-existent job fails
        self.assertFalse(self.store.claim_job(99999))

    def test_api_state_head_request(self):
        """HEAD /api/state returns 200 with auth and 401 without auth."""
        import http.client
        conn = http.client.HTTPConnection(self.server.server_address[0], self.server.server_port)
        # Without auth -> 401
        conn.request("HEAD", "/api/state")
        res = conn.getresponse()
        self.assertEqual(res.status, 401)
        res.read()

        # With auth -> 200
        conn.request("HEAD", "/api/state", headers={"Authorization": f"Bearer {self.app.auth_token}"})
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        res.read()
        conn.close()

    def test_complete_task_manually_auto_creates_missing_site(self):
        """complete_task_manually gracefully upserts an uncataloged site instead of raising."""
        day = "2026-08-28"
        res = self.store.complete_task_manually(day, "完全未录入站")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("status"), "done")
        task = [t for t in self.store.tasks(day) if t["name"] == "完全未录入站"]
        self.assertEqual(len(task), 1)
        self.assertEqual(task[0]["status"], "done")

    def test_csrf_token_persistence(self):
        """CSRF token is persisted in data dir and survives app restarts."""
        from checkin_core.web import CheckinWebApp, load_or_create_csrf, CSRF_FILENAME
        csrf_file = self.store.path.parent / CSRF_FILENAME
        token1 = load_or_create_csrf(csrf_file)
        self.assertTrue(token1)
        # Re-loading returns the exact same token
        token2 = load_or_create_csrf(csrf_file)
        self.assertEqual(token1, token2)

    def test_api_sync_obsidian(self):
        """POST /api/sync/obsidian validates day and syncs task state."""
        status, data = self.request_json(
            "POST", "/api/sync/obsidian",
            {"day": "2026-08-28"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("day"), "2026-08-28")

        # Invalid day rejected
        status, data = self.request_json(
            "POST", "/api/sync/obsidian",
            {"day": "invalid-day-string"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid day format", data["error"])

    def test_credential_api_supports_password_token_and_cookie_kinds(self):
        """POST /api/credentials stores token, cookie, and password kinds correctly."""
        # 1. Token kind
        status, data = self.request_json(
            "POST", "/api/credentials",
            {"site": "TokenSite", "kind": "token", "secret": "sk-test-token", "label": "token-lbl"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        token_ref = data["ref"]
        self.assertEqual(self.creds.values.get(token_ref), "sk-test-token")
        refs = self.store.credential_refs()
        target = [r for r in refs if r["site"] == "TokenSite"][0]
        self.assertEqual(target["kind"], "token")
        self.assertEqual(target["label"], "token-lbl")

        # 2. Cookie kind
        status, data = self.request_json(
            "POST", "/api/credentials",
            {"site": "CookieSite", "kind": "cookie", "secret": "session=abc; uid=1", "label": "cookie-lbl"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        cookie_ref = data["ref"]
        self.assertEqual(self.creds.values.get(cookie_ref), "session=abc; uid=1")

        # 3. Password kind (stores json with account and password)
        pwd_payload = json.dumps({"account": "user@test.com", "password": "secure_pass_123"})
        status, data = self.request_json(
            "POST", "/api/credentials",
            {"site": "PasswordSite", "kind": "password", "secret": pwd_payload, "label": "user@test.com"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        pwd_ref = data["ref"]
        stored_pwd = json.loads(self.creds.values.get(pwd_ref))
        self.assertEqual(stored_pwd["account"], "user@test.com")
        self.assertEqual(stored_pwd["password"], "secure_pass_123")

        # 4. Delete
        status, data = self.request_json(
            "POST", "/api/credentials/delete",
            {"ref": pwd_ref},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 200)
        self.assertNotIn(pwd_ref, self.creds.values)
        self.assertFalse(any(r["ref"] == pwd_ref for r in self.store.credential_refs()))

    def test_task_and_credential_api_reject_dirty_names(self):
        """The write endpoints sanitize site names before they touch the DB."""
        # Explicitly overwrite the raw-name path: control chars (including \t) must be rejected.
        status, data = self.request_json(
            "POST", "/api/credentials",
            {"site": "bad\x00site", "kind": "token", "secret": "s"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("control", data["error"])

        # Tab character rejected
        status, data = self.request_json(
            "POST", "/api/credentials",
            {"site": "bad\tsite", "kind": "token", "secret": "s"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("control", data["error"])

        # Invalid or removed credential kind rejected
        for bad_kind in ("invalid_kind_foo", "account"):
            status, data = self.request_json(
                "POST", "/api/credentials",
                {"site": "GoodsSite", "kind": bad_kind, "secret": "s"},
                self.app.csrf, auth=self.app.auth_token,
            )
            self.assertEqual(status, 400)
            self.assertIn("invalid credential kind", data["error"])

        # Commas in site name on /api/run rejected
        status, data = self.request_json(
            "POST", "/api/run",
            {"sites": ["site1,site2"]},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("commas", data["error"])

    def test_api_tasks_rejects_control_chars_and_long_name(self):
        """POST /api/tasks and other endpoints validate clean scalar names:
        rejecting control characters, empty names, or names exceeding 120 chars."""
        # 1. Reject control characters (\n, \r, \x00)
        status, data = self.request_json(
            "POST", "/api/tasks",
            {"name": "Evil\nSite", "url": "https://evil.com"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("control characters", data["error"])

        # 2. Reject excessively long names (>120 chars)
        long_name = "A" * 121
        status, data = self.request_json(
            "POST", "/api/tasks",
            {"name": long_name, "url": "https://evil.com"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("too long", data["error"])

    def test_stale_queue_row_not_lost_if_recovery_runs_before_pickup(self):
        """Recovery enqueues any still-queued row even if called more than
        once before the worker picks it up — the DB row stays 'queued' until
        execution, so a clean restart never drops a recovered job."""
        self.store.create_job("all", ["Z"])
        executor = web_module.JobExecutor(self.store, start_worker=False)
        self.assertEqual(executor.recover_stale_jobs(), 1)
        # Worker has not run, so the row is still 'queued'; a second recovery
        # before pickup finds it again (app calls recovery exactly once at
        # startup, so this never double-executes in practice).
        self.assertEqual(executor.recover_stale_jobs(), 1)
        self.assertEqual(executor._queue.qsize(), 2)


class FrontendServerTests(unittest.TestCase):
    """Dedicated static frontend service (checkin_core.frontend, port 8766):

    - serves only the exact whitelisted files, and only read-only
    - maps ``/`` to ``index.html`` so the bare origin works
    - rejects nested paths, traversal, unknown files, and non-loopback Host headers
    - returns CSP / no-store / nosniff on every asset
    """

    FILES = {
        "index.html": "<!doctype html><title>Daily Check-in</title>",
        "app.js": "console.log('daily-checkin')",
        "style.css": "body { color: #333 }",
    }

    def setUp(self):
        self.td = TemporaryDirectory()
        self.dir = Path(self.td.name)
        for name, content in self.FILES.items():
            (self.dir / name).write_text(content, encoding="utf-8")
        # Build the server in the main thread (port known immediately), then
        # serve on a daemon thread — same pattern as WebTests. frontend_dir is
        # set the way serve() sets it so the handler code path is identical.
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FrontendHandler)
        self.server.frontend_dir = self.dir
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.td.cleanup()

    def request(self, method, path, headers=None):
        self.conn.request(method, path, headers=headers or {})
        resp = self.conn.getresponse()
        return resp, resp.read()

    def test_bare_origin_serves_index(self):
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn("Daily Check-in", body.decode())

    def test_only_whitelisted_files_served(self):
        resp, _ = self.request("GET", "/app.js")
        self.assertEqual(resp.status, 200)
        resp, _ = self.request("GET", "/style.css")
        self.assertEqual(resp.status, 200)
        resp, _ = self.request("GET", "/favicon.ico")
        self.assertEqual(resp.status, 404)

    def test_nested_and_traversal_paths_rejected(self):
        for path in ("/../system.db", "/..%2f..%2fsecret", "/sub/app.js"):
            resp, _ = self.request("GET", path)
            self.assertEqual(resp.status, 404, path)

    def test_security_headers_present(self):
        resp, _ = self.request("GET", "/app.js")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))
        # Never allow inline script (XSS amp); only self + the API origin.
        self.assertNotIn("unsafe-inline", resp.getheader("Content-Security-Policy"))

    def test_csp_permits_api_connect(self):
        from checkin_core.frontend import DEFAULT_API_BASE

        resp, _ = self.request("GET", "/app.js")
        csp = resp.getheader("Content-Security-Policy")
        # The split frontend calls the separate API (8765); an opaque
        # connect-src 'self' would let the browser block every API fetch.
        self.assertIn(f"connect-src 'self' {DEFAULT_API_BASE}", csp)

    def test_host_header_non_loopback_forbidden(self):
        resp, _ = self.request("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(resp.status, 403)

    def test_host_header_allows_mangoqwq_frontend_domain(self):
        resp, body = self.request("GET", "/", headers={"Host": "check.mangoqwq.com:8766"})
        self.assertEqual(resp.status, 200)
        self.assertIn("Daily Check-in", body.decode())

    def test_csp_permits_mangoqwq_api_connect(self):
        resp, _ = self.request("GET", "/app.js")
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("http://check.mangoqwq.com:8765", csp)
        self.assertIn("http://check-api.mangoqwq.com:8765", csp)


class FrontendStaticAssetIntegrityTests(unittest.TestCase):
    """Sanity checks for static asset styles, dynamic form controls, and CSP compliance."""

    def setUp(self):
        self.web_dir = Path(__file__).parent.parent / "web"
        self.index_html = (self.web_dir / "index.html").read_text(encoding="utf-8")
        self.app_js = (self.web_dir / "app.js").read_text(encoding="utf-8")
        self.style_css = (self.web_dir / "style.css").read_text(encoding="utf-8")

    def test_zero_inline_styles_across_web_assets(self):
        """Strict CSP compliance: no inline style="..." anywhere in web directory."""
        for path in self.web_dir.glob("*.*"):
            if path.suffix in (".html", ".js"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("style=", text, f"Found inline style in {path.name}")

    def test_is_hidden_css_rule_exists(self):
        """Ensure .is-hidden rule with display: none !important exists in style.css."""
        self.assertIn(".is-hidden", self.style_css)
        self.assertIn("display: none", self.style_css)

    def test_account_row_and_kind_selector_exist_in_html(self):
        """Ensure cred-account-row and cred-kind elements exist in index.html with is-hidden."""
        self.assertIn('id="cred-account-row"', self.index_html)
        self.assertIn('class="form-group-item is-hidden"', self.index_html)
        self.assertIn('id="cred-kind"', self.index_html)

    def test_app_js_handles_dynamic_credential_kind_switching(self):
        """Ensure app.js binds listeners for password vs token/cookie kind toggling."""
        self.assertIn("updateMainCredKindUi", self.app_js)
        self.assertIn("updateCredKindUi", self.app_js)
        self.assertIn('formObj.kind === "password"', self.app_js)

    def test_manual_checkin_page_elements_exist_in_html_and_js(self):
        """Ensure manual check-in workspace markup and routing logic exist."""
        self.assertIn('id="pane-manual"', self.index_html)
        self.assertIn('id="manual-search-input"', self.index_html)
        self.assertIn('id="manual-filter-select"', self.index_html)
        self.assertIn('id="manual-tiles-container"', self.index_html)
        self.assertIn('id="sidebar-manual-badge"', self.index_html)
        self.assertIn("getManualTasks", self.app_js)
        self.assertIn("check.mangoqwq.com", self.app_js)
        self.assertIn("handleManualCheckin", self.app_js)
        self.assertIn("/api/sites/update", self.app_js)
        self.assertIn("/api/sites/delete", self.app_js)
        self.assertIn("site-drawer-delete-site-btn", self.app_js)
        self.assertIn(".danger-zone-card", self.style_css)

    def test_credential_deletion_delegation_and_clean_render(self):
        """Ensure credential deletion is handled by event delegation with data-del-cred and no redundant querySelectorAll."""
        self.assertIn('data-del-cred="${esc(c.ref)}"', self.app_js)
        self.assertIn('const delCredBtn = e.target.closest("[data-del-cred]");', self.app_js)
        self.assertNotIn('credsTbody.querySelectorAll("[data-del-cred]")', self.app_js)

    def test_tags_picker_idempotency_guard(self):
        """Ensure tag picker inputs and buttons use _pickerBound to prevent listener accumulation on view switches."""
        self.assertIn("_pickerBound", self.app_js)
        self.assertIn("container._currentAddCustomTag = addCustomTag;", self.app_js)

    def test_modular_rendering_and_debounced_scoped_updates(self):
        """Ensure render pipeline supports scoped renders and search input uses debounce."""
        self.assertIn('render("tasks")', self.app_js)
        self.assertIn('render("manual")', self.app_js)
        self.assertIn("function computeTaskStats()", self.app_js)
        self.assertIn("function renderTasksView(", self.app_js)
        self.assertIn("function renderManualView()", self.app_js)

    def test_pending_count_excludes_suppressed_sites(self):
        """待办计数必须和列表过滤同一口径：熔断中的 pending 不能让芯片显示数字、格子却为空。"""
        self.assertIn("function isPendingTask(task)", self.app_js)
        self.assertIn("list = list.filter(isPendingTask);", self.app_js)
        counter = self.app_js.split("autoTasks.forEach((task) => {", 1)[1].split("});", 1)[0]
        self.assertIn("else if (isPendingTask(task)) counts.pending++;", counter)
        self.assertNotIn("else counts.pending++;", counter)

    def test_clean_custom_elements_and_no_phantom_definitions(self):
        """Ensure valid custom elements exist and phantom unrendered classes are removed."""
        self.assertIn('customElements.define("ui-icon"', self.app_js)
        self.assertIn('customElements.define("status-indicator"', self.app_js)
        self.assertIn('customElements.define("theme-toggle"', self.app_js)
        self.assertIn('customElements.define("prompt-chips"', self.app_js)
        self.assertNotIn("class KpiCardElement", self.app_js)
        self.assertNotIn("class SiteTileElement", self.app_js)

    def test_balance_overview_and_sort_markup_integrity(self):
        """RFC-043: Verify markup, styling, and JS logic for balance overview, sorting and drawer."""
        # HTML assertions
        self.assertIn('id="overview-balance-card"', self.index_html)
        self.assertIn('id="overview-balance-body"', self.index_html)
        self.assertIn('id="jump-to-balance-tasks-btn"', self.index_html)
        self.assertIn('value="balance_desc"', self.index_html)
        self.assertIn('value="balance_asc"', self.index_html)
        self.assertIn('data-site-tab="balance"', self.index_html)
        self.assertIn('id="site-pane-balance"', self.index_html)

        # CSS assertions
        self.assertIn(".balance-stat-grid", self.style_css)
        self.assertIn(".balance-stat-box", self.style_css)
        self.assertIn(".balance-stat-icon.usd", self.style_css)
        self.assertIn(".balance-stat-icon.cny", self.style_css)
        self.assertIn(".balance-stat-icon.points", self.style_css)
        self.assertIn(".balance-stat-icon.coverage", self.style_css)
        self.assertIn(".balance-leaderboard-card", self.style_css)
        self.assertIn(".balance-history-badge.manual", self.style_css)
        self.assertIn(".tile-reward-badge", self.style_css)
        self.assertIn(".tile-balance-badge", self.style_css)

        # JS assertions
        self.assertIn('currentSort === "balance_desc"', self.app_js)
        self.assertIn('currentSort === "balance_asc"', self.app_js)
        self.assertIn('currentFilter === "含资产"', self.app_js)
        self.assertIn('api("/api/sites/balance"', self.app_js)
        self.assertIn('/api/sites/balance/history', self.app_js)
        self.assertIn('site-drawer-balance-form', self.app_js)
        self.assertIn('site-drawer-balance-history-wrap', self.app_js)
        self.assertIn('tile-reward-badge', self.app_js)
        self.assertIn('task_reward_text', self.app_js)
        self.assertIn('isDone && (task.task_reward_text || task.last_reward_text)', self.app_js)

    def test_reward_and_balance_store_persistence(self):
        """Schema v9: Verify last_reward_text and reward fields persist to sites and account_balances."""
        with TemporaryDirectory() as td:
            db_path = Path(td) / "test_reward_store.db"
            store = SystemStore(db_path)
            day = "2026-10-10"
            site_name = "test-reward-site"
            store.upsert_task(day, site_name, "https://example.com")

            from checkin_core.models import BalanceEvidence
            evidence = BalanceEvidence(
                amount=12.50,
                raw_text="$12.50",
                currency="USD",
                source="api",
                reward_text="+BTC 0.84",
            )
            store.record_site_balance(site_name, day, evidence)

            tasks = store.tasks(day)
            matched = next((t for t in tasks if t["name"] == site_name), None)
            self.assertIsNotNone(matched)
            self.assertEqual(matched.get("last_reward_text"), "+BTC 0.84")
            self.assertEqual(matched.get("task_reward_text"), "+BTC 0.84")
            self.assertEqual(matched.get("last_balance_text"), "$12.50")

            history = store.get_site_balance_history(site_name)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].get("reward_text"), "+BTC 0.84")

    def test_partial_balance_update_does_not_wipe_existing_balance(self):
        """P1 root-cause regression: reward-only updates must not wipe existing balances to NULL or empty string."""
        with TemporaryDirectory() as td:
            db_path = Path(td) / "test_p1_store.db"
            store = SystemStore(db_path)
            day = "2026-10-10"
            site_name = "p1-balance-site"
            store.upsert_task(day, site_name, "https://example.com")

            from checkin_core.models import BalanceEvidence
            # First, record a full account balance
            store.record_site_balance(
                site_name,
                day,
                BalanceEvidence(amount=25.0, raw_text="$25.00", currency="USD", source="api")
            )
            site = store.tasks(day)[0]
            self.assertEqual(site["last_balance"], 25.0)
            self.assertEqual(site["last_balance_text"], "$25.00")

            # Second, record a reward-only update where amount is None and raw_text is empty
            store.record_site_balance(
                site_name,
                day,
                BalanceEvidence(amount=None, raw_text="", currency="USD", reward_text="+10 积分", source="detail")
            )
            site = store.tasks(day)[0]
            # Existing balance must be preserved!
            self.assertEqual(site["last_balance"], 25.0)
            self.assertEqual(site["last_balance_text"], "$25.00")
            self.assertEqual(site["last_reward_text"], "+10 积分")

            # History on conflict should also retain amount and raw_text
            history = store.get_site_balance_history(site_name)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["amount"], 25.0)
            self.assertEqual(history[0]["raw_text"], "$25.00")
            self.assertEqual(history[0]["reward_text"], "+10 积分")

    def test_tasks_query_empty_task_reward_text_falls_back_to_site_reward(self):
        """P2 root-cause regression: empty string in task.last_reward_text must fall back to site.last_reward_text."""
        with TemporaryDirectory() as td:
            db_path = Path(td) / "test_p2_store.db"
            store = SystemStore(db_path)
            day = "2026-10-10"
            site_name = "p2-fallback-site"
            store.upsert_task("2026-10-09", site_name, "https://example.com")

            from checkin_core.models import BalanceEvidence
            # Record reward on site for yesterday
            store.record_site_balance(
                site_name,
                "2026-10-09",
                BalanceEvidence(amount=10.0, raw_text="$10.00", reward_text="+BTC 0.84")
            )

            # Today's task is created with empty task_reward_text (default '')
            store.materialize_day(day)
            tasks = store.tasks(day)
            matched = next(t for t in tasks if t["name"] == site_name)
            self.assertEqual(matched["task_reward_text"], "+BTC 0.84")

            # If today's task has its own reward, it overrides site reward
            store.record_site_balance(
                site_name,
                day,
                BalanceEvidence(reward_text="+BTC 1.20")
            )
            tasks = store.tasks(day)
            matched = next(t for t in tasks if t["name"] == site_name)
            self.assertEqual(matched["task_reward_text"], "+BTC 1.20")

    def test_enrich_result_balance_uses_confirmation_summary(self):
        """P2 root-cause regression: confirmation.summary must be parsed when result.detail is empty."""
        import asyncio
        from checkin_core.models import ConfirmationEvidence
        from stealth_checkin_runner import CheckinResult, SiteAdapter, _enrich_result_balance

        class DummyAdapter:
            name = "dummy-site"
            kind = "dummy"
            url = "https://example.com"

        conf = ConfirmationEvidence(
            kind="dom_strong",
            source="dom",
            summary="签到成功！获得鸡腿 5 个",
            observed_at="2026-10-10T12:00:00",
        )
        result = CheckinResult(status="OK", detail="", confirmation=conf)
        enriched = asyncio.run(_enrich_result_balance(None, DummyAdapter(), result))
        self.assertIsNotNone(enriched.balance)
        self.assertEqual(enriched.balance.reward_text, "鸡腿 5 个")



class SiteConfigUpdateApiTests(unittest.TestCase):
    """Tests for updating site config attributes (mode, tags, url, provider)."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.db_path = Path(self.td.name) / "test_sites.db"
        self.store = SystemStore(self.db_path)
        self.day = "2026-08-28"
        self.store.upsert_task(self.day, "auto-site", "https://auto.example.com")
        self.store.upsert_task(self.day, "manual-site", "https://manual.example.com")
        self.creds = FakeCredentials()
        self.app = CheckinWebApp(self.store, self.creds, require_auth=False)
        self.server = __import__("http.server").server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.app.handler()
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.td.cleanup()

    def test_store_update_site_config(self):
        self.store.update_site_config("manual-site", checkin_mode="manual", tags="captcha,turnstile")
        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertEqual(tasks["manual-site"]["checkin_mode"], "manual")
        self.assertEqual(tasks["manual-site"]["tags"], "captcha,turnstile")
        self.assertEqual(tasks["auto-site"]["checkin_mode"], "auto")

    def test_store_update_site_config_cleans_manual_tag_on_auto(self):
        # Set manual mode with manual tag
        self.store.update_site_config("manual-site", checkin_mode="manual", tags="manual,custom")
        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertEqual(tasks["manual-site"]["checkin_mode"], "manual")
        self.assertIn("manual", tasks["manual-site"]["tags"])

        # Switch to auto with tags including manual -> manual tag must be stripped
        self.store.update_site_config("manual-site", checkin_mode="auto", tags="manual,custom")
        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertEqual(tasks["manual-site"]["checkin_mode"], "auto")
        self.assertEqual(tasks["manual-site"]["tags"], "custom")

        # Now re-add manual tag and switch to auto without passing tags -> must be stripped from DB
        self.store.update_site_config("manual-site", checkin_mode="manual", tags="manual,vip")
        self.store.update_site_config("manual-site", checkin_mode="auto")
        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertEqual(tasks["manual-site"]["checkin_mode"], "auto")
        self.assertEqual(tasks["manual-site"]["tags"], "vip")

    def test_is_manual_row_priority(self):
        from stealth_checkin_runner import is_manual_row
        # 1. Explicit auto with manual tag -> False (not manual)
        self.assertFalse(is_manual_row({"checkin_mode": "auto", "tags": "manual"}))
        self.assertFalse(is_manual_row({"checkin_mode": "auto", "tags": "custom,manual,vip"}))
        self.assertFalse(is_manual_row({"checkin_mode": "Auto", "tags": "MANUAL"}))

        # 2. Explicit manual -> True (manual)
        self.assertTrue(is_manual_row({"checkin_mode": "manual", "tags": ""}))
        self.assertTrue(is_manual_row({"checkin_mode": "manual", "tags": "auto"}))
        self.assertTrue(is_manual_row({"checkin_mode": "MANUAL", "tags": None}))

        # 3. Mode not set (or empty) -> fallback to tags
        self.assertTrue(is_manual_row({"checkin_mode": "", "tags": "manual"}))
        self.assertTrue(is_manual_row({"checkin_mode": None, "tags": "some,manual,tag"}))
        self.assertFalse(is_manual_row({"checkin_mode": "", "tags": "auto"}))
        self.assertFalse(is_manual_row({"checkin_mode": None, "tags": ""}))
        self.assertFalse(is_manual_row({}))

    def test_api_update_site_config(self):
        # Get CSRF from state
        self.conn.request("GET", "/api/state")
        res = self.conn.getresponse()
        csrf = json.loads(res.read())["csrf"]

        # Post update
        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        body = json.dumps({"name": "auto-site", "checkin_mode": "manual", "tags": "2fa"}).encode()
        self.conn.request("POST", "/api/sites/update", body, headers)
        resp = self.conn.getresponse()
        data = json.loads(resp.read())
        self.assertEqual(resp.status, 200)
        self.assertTrue(data.get("ok"))

        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertEqual(tasks["auto-site"]["checkin_mode"], "manual")
        self.assertEqual(tasks["auto-site"]["tags"], "2fa")

    def test_api_create_task_with_full_config(self):
        self.conn.request("GET", "/api/state")
        res = self.conn.getresponse()
        csrf = json.loads(res.read())["csrf"]

        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        body = json.dumps({
            "day": self.day,
            "name": "brand-new-site",
            "url": "https://newsite.com/checkin",
            "provider": "new_api",
            "checkin_mode": "manual",
            "tags": "vip,cloudflare"
        }).encode()
        self.conn.request("POST", "/api/tasks", body, headers)
        resp = self.conn.getresponse()
        data = json.loads(resp.read())
        self.assertEqual(resp.status, 200)
        self.assertTrue(data.get("ok"))

        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertIn("brand-new-site", tasks)
        site = tasks["brand-new-site"]
        self.assertEqual(site["url"], "https://newsite.com/checkin")
        self.assertEqual(site["provider"], "new_api")
        self.assertEqual(site["checkin_mode"], "manual")
        self.assertEqual(site["tags"], "vip,cloudflare")

    def test_api_update_site_config_rejects_invalid_mode(self):
        self.conn.request("GET", "/api/state")
        res = self.conn.getresponse()
        csrf = json.loads(res.read())["csrf"]

        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        body = json.dumps({"name": "auto-site", "checkin_mode": "invalid_mode"}).encode()
        self.conn.request("POST", "/api/sites/update", body, headers)
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 400)

    def test_store_update_site_config_ignores_unallowed_fields(self):
        # unallowed fields must be ignored and not cause SQL errors
        self.store.update_site_config("manual-site", checkin_mode="manual", unknown_column="malicious_val")
        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertEqual(tasks["manual-site"]["checkin_mode"], "manual")

    def test_store_delete_site_cascades_and_preserves_run_items(self):
        # Create a site with health, credential, and run_item
        self.store.upsert_task(self.day, "to-delete-site", "https://todelete.com", status="pending")
        self.store.record_site_health("to-delete-site", "suspended", "account closed")
        self.store.set_credential_ref("to-delete-site", "cred:to-delete-site:token", "token", "test")
        
        # Record a run item for this site
        run_id = self.store.create_run(self.day, "test")
        self.store.add_run_item(
            run_id,
            {"site": "to-delete-site", "status": "OK", "stage": "confirm", "reason": "success", "attribution": "test"},
        )

        # Delete the site
        res = self.store.delete_site("to-delete-site")
        self.assertEqual(res["name"], "to-delete-site")
        self.assertEqual(res["credential_ref"], "cred:to-delete-site:token")
        self.assertEqual(res["deleted_tasks"], 1)

        # Verify site is removed from sites table
        with self.store.connect() as db:
            site = db.execute("SELECT * FROM sites WHERE name='to-delete-site'").fetchone()
            self.assertIsNone(site)

            # daily_tasks cascaded
            dt = db.execute("SELECT * FROM daily_tasks WHERE obsidian_name='to-delete-site'").fetchall()
            self.assertEqual(len(dt), 0)

            # credentials cascaded
            cred = db.execute("SELECT * FROM credentials WHERE ref='cred:to-delete-site:token'").fetchone()
            self.assertIsNone(cred)

            # run_items preserved with site_id NULL but site_name intact
            items = db.execute("SELECT site_id, site_name, status FROM run_items WHERE site_name='to-delete-site'").fetchall()
            self.assertEqual(len(items), 1)
            self.assertIsNone(items[0]["site_id"])
            self.assertEqual(items[0]["site_name"], "to-delete-site")
            self.assertEqual(items[0]["status"], "OK")

    def test_store_delete_site_validations(self):
        with self.assertRaises(ValueError):
            self.store.delete_site("")
        with self.assertRaises(ValueError):
            self.store.delete_site("non-existent-site-xyz")

    def test_api_delete_site_success_and_purges_keychain(self):
        # Setup site with credential in fake keychain
        self.store.upsert_task(self.day, "api-del-site", "https://apidel.com", status="pending")
        self.app.credentials.put("cred:api-del-site:token", "secret123")
        self.store.set_credential_ref("api-del-site", "cred:api-del-site:token", "token", "lbl")

        self.assertEqual(self.app.credentials.values.get("cred:api-del-site:token"), "secret123")

        # Get CSRF from state
        self.conn.request("GET", "/api/state")
        res = self.conn.getresponse()
        csrf = json.loads(res.read())["csrf"]

        # Call POST /api/sites/delete
        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        body = json.dumps({"name": "api-del-site"}).encode()
        self.conn.request("POST", "/api/sites/delete", body, headers)
        resp = self.conn.getresponse()
        data = json.loads(resp.read())
        self.assertEqual(resp.status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("name"), "api-del-site")

        # Verify credential in keychain is wiped
        self.assertNotIn("cred:api-del-site:token", self.app.credentials.values)

        # Verify site is gone from tasks
        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertNotIn("api-del-site", tasks)
        self.assertTrue(self.store.is_site_deleted("api-del-site"))

    def test_delete_site_records_tombstone_and_projects_obsidian_removal(self):
        """Deleting a site must record tombstone and strip task line from markdown note."""
        note_path = Path(self.td.name) / f"{self.day} 每日任务.md"
        note_path.write_text(
            f"# {self.day} 每日任务\n"
            "- [ ] #task #日常 [del-proj-site](https://delproj.example.com) #auto-fail:no_button\n"
            "- [ ] #task #日常 [other-site](https://other.example.com)\n",
            encoding="utf-8",
        )
        self.store.upsert_task(
            self.day,
            "del-proj-site",
            "https://delproj.example.com",
            obsidian_path=str(note_path),
        )

        self.assertFalse(self.store.is_site_deleted("del-proj-site"))
        res = self.store.delete_site("del-proj-site")
        self.assertEqual(res["name"], "del-proj-site")

        # 1. Verify tombstone is recorded
        self.assertTrue(self.store.is_site_deleted("del-proj-site"))
        self.assertIn("del-proj-site", self.store.get_deleted_site_names())

        # 2. Verify Obsidian note was updated and site line removed
        note_content = note_path.read_text(encoding="utf-8")
        self.assertNotIn("del-proj-site", note_content)
        self.assertIn("other-site", note_content)

    def test_import_obsidian_tasks_skips_tombstoned_deleted_sites(self):
        """import_obsidian_tasks must never resurrect a tombstoned deleted site."""
        self.store.upsert_task(self.day, "to-be-tombstoned", "https://tombstone.example.com")
        self.store.delete_site("to-be-tombstoned")
        self.assertTrue(self.store.is_site_deleted("to-be-tombstoned"))

        # Attempt to import rows containing the deleted site (e.g. from older note or sync)
        rows = [
            ("to-be-tombstoned", "https://tombstone.example.com", False),
            ("fresh-site", "https://fresh.example.com", False),
        ]
        imported_count = self.store.import_obsidian_tasks(self.day, rows, "/fake/path.md")
        self.assertEqual(imported_count, 1)

        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertNotIn("to-be-tombstoned", tasks)
        self.assertIn("fresh-site", tasks)

    def test_upsert_site_clears_tombstone_when_explicitly_readded(self):
        """Explicitly upserting a site clears its tombstone, allowing re-addition."""
        self.store.upsert_task(self.day, "readd-site", "https://readd.example.com")
        self.store.delete_site("readd-site")
        self.assertTrue(self.store.is_site_deleted("readd-site"))

        # Explicit upsert
        self.store.upsert_site("readd-site", "https://readd.example.com/new")
        self.assertFalse(self.store.is_site_deleted("readd-site"))

        # Subsequent import now succeeds
        rows = [("readd-site", "https://readd.example.com/new", False)]
        self.store.import_obsidian_tasks(self.day, rows, "/fake/path.md")
        tasks = {t["name"]: t for t in self.store.tasks(self.day)}
        self.assertIn("readd-site", tasks)

    def test_ensure_daily_task_file_filters_deleted_sites_from_seed(self):
        """ensure_daily_task_file must filter out tombstoned sites when seeding new day note."""
        from stealth_checkin_runner import ensure_daily_task_file

        seed_file = Path(self.td.name) / "2026-08-27 每日任务.md"
        seed_file.write_text(
            "# 任务\n"
            "- [x] #task #日常 [dead-site](https://dead.com)\n"
            "- [x] #task #日常 [alive-site](https://alive.com)\n",
            encoding="utf-8",
        )
        self.store.upsert_task("2026-08-27", "dead-site", "https://dead.com")
        self.store.delete_site("dead-site")

        new_day = "2026-08-28"
        target_file = Path(self.td.name) / f"{new_day} 每日任务.md"
        content, created = ensure_daily_task_file(
            str(target_file), day=new_day, excluded_sites=self.store.get_deleted_site_names()
        )
        self.assertTrue(created)
        self.assertNotIn("dead-site", content)
        self.assertIn("alive-site", content)
        self.assertIn("- [ ] #task #日常 [alive-site]", content)

    def test_delete_site_markdown_projection_robust_against_tag_order_and_case(self):
        """_project_obsidian_site_deleted must strip matching lines regardless of tag order or case."""
        note_path = Path(self.td.name) / f"{self.day} 每日任务.md"
        note_path.write_text(
            f"# {self.day} 每日任务\n"
            "- [ ] #日常 #task [CaseSite](https://case.example.com)\n"
            "- [x] #task #日常 [casesite]\n"
            "- [ ] #task #日常 [kept-site](https://kept.example.com)\n",
            encoding="utf-8",
        )
        self.store.upsert_task(
            self.day,
            "casesite",
            "https://case.example.com",
            obsidian_path=str(note_path),
        )

        self.store.delete_site("casesite")
        content = note_path.read_text(encoding="utf-8")
        self.assertNotIn("casesite", content.lower())
        self.assertIn("kept-site", content)

    def test_solve_captcha_with_vision_credential_fallback_and_safety(self):
        """solve_captcha_with_vision should safely fallback to store config and never crash if unset."""
        from stealth_checkin_runner import solve_captcha_with_vision

        old_env = os.environ.get("DAILY_CHECKIN_VISION_API_KEY")
        try:
            if "DAILY_CHECKIN_VISION_API_KEY" in os.environ:
                del os.environ["DAILY_CHECKIN_VISION_API_KEY"]

            # Empty image
            self.assertEqual(solve_captcha_with_vision(b""), "")

            # Non-empty image with no key configured anywhere should safely return "" without exception
            with mock.patch("checkin_core.store.SystemStore.get_config", return_value=""):
                res = solve_captcha_with_vision(b"fake-image-bytes")
                self.assertEqual(res, "")
        finally:
            if old_env is not None:
                os.environ["DAILY_CHECKIN_VISION_API_KEY"] = old_env

    def test_api_delete_site_error_handling(self):
        self.conn.request("GET", "/api/state")
        res = self.conn.getresponse()
        csrf = json.loads(res.read())["csrf"]

        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        # Empty name
        self.conn.request("POST", "/api/sites/delete", json.dumps({"name": ""}).encode(), headers)
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 400)

        # Nonexistent site
        self.conn.request("POST", "/api/sites/delete", json.dumps({"name": "not-exists-999"}).encode(), headers)
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 400)

    def test_api_ai_latest_endpoint(self):
        # Query when no inspection exists
        self.conn.request("GET", f"/api/ai/latest?day={self.day}")
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertTrue(data.get("ok"))
        self.assertIsNone(data.get("inspection"))

        # Record an inspection
        self.store.save_ai_inspection(
            day=self.day,
            model="gpt-4o-mini",
            latency_ms=120,
            status="success",
            summary="All clear",
            structured_json=json.dumps({"summary": "All clear", "inspections": []}),
            remediation_log=json.dumps([{"site_name": "foo", "action": "retry", "status": "scheduled"}]),
            prompt_tokens=100,
            completion_tokens=20,
        )

        # Query again
        self.conn.request("GET", f"/api/ai/latest?day={self.day}")
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertTrue(data.get("ok"))
        insp = data.get("inspection")
        self.assertIsNotNone(insp)
        self.assertEqual(insp["model"], "gpt-4o-mini")
        self.assertEqual(insp["summary"], "All clear")
        self.assertEqual(insp["prompt_tokens"], 100)
        self.assertEqual(insp["completion_tokens"], 20)

    def test_api_ai_test_endpoint(self):
        self.conn.request("GET", "/api/state")
        res = self.conn.getresponse()
        csrf = json.loads(res.read())["csrf"]

        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}

        # Missing base_url and api_key
        body = json.dumps({"base_url": "", "api_key": ""}).encode()
        self.conn.request("POST", "/api/ai/test", body, headers)
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 400)

        # Mock successful external call
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "pong"}}]
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            body = json.dumps({
                "channel_name": "TestPrimary",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-fake-test-key",
                "model": "gpt-4o-mini"
            }).encode()
            self.conn.request("POST", "/api/ai/test", body, headers)
            resp = self.conn.getresponse()
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("channel_name"), "TestPrimary")
            self.assertEqual(data.get("reply"), "pong")

    def test_api_site_balance_endpoints(self):
        """RFC-043: Verify POST /api/sites/balance and GET /api/sites/balance/history."""
        # 1. Get CSRF token
        self.conn.request("GET", "/api/state")
        res = self.conn.getresponse()
        csrf = json.loads(res.read())["csrf"]
        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}

        # 2. Reject missing site name
        self.conn.request("POST", "/api/sites/balance", json.dumps({"amount": 10.5}).encode(), headers)
        self.assertEqual(self.conn.getresponse().status, 400)

        # 3. Reject invalid amount (including NaN and Infinity)
        self.conn.request("POST", "/api/sites/balance", json.dumps({"name": "auto-site", "amount": "abc"}).encode(), headers)
        self.assertEqual(self.conn.getresponse().status, 400)
        self.conn.request("POST", "/api/sites/balance", json.dumps({"name": "auto-site", "amount": "nan"}).encode(), headers)
        self.assertEqual(self.conn.getresponse().status, 400)
        self.conn.request("POST", "/api/sites/balance", json.dumps({"name": "auto-site", "amount": "inf"}).encode(), headers)
        self.assertEqual(self.conn.getresponse().status, 400)

        # 3b. Reject invalid day format
        self.conn.request("POST", "/api/sites/balance", json.dumps({"name": "auto-site", "amount": 10.0, "day": "2026/10/10"}).encode(), headers)
        self.assertEqual(self.conn.getresponse().status, 400)
        self.conn.request("POST", "/api/sites/balance", json.dumps({"name": "auto-site", "amount": 10.0, "day": "invalid-date"}).encode(), headers)
        self.assertEqual(self.conn.getresponse().status, 400)

        # 4. Successful manual balance update
        body = json.dumps({
            "name": "auto-site",
            "amount": 42.5,
            "currency": "USD",
            "raw_text": "$42.50",
            "day": self.day,
        }).encode()
        self.conn.request("POST", "/api/sites/balance", body, headers)
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("name"), "auto-site")
        self.assertEqual(data["balance"]["amount"], 42.5)

        # 5. Query history for site (including limit parameter testing)
        self.conn.request("GET", "/api/sites/balance/history?site=auto-site&limit=10")
        h_resp = self.conn.getresponse()
        self.assertEqual(h_resp.status, 200)
        h_data = json.loads(h_resp.read())
        self.assertTrue(h_data.get("ok"))
        self.assertEqual(len(h_data.get("history", [])), 1)
        self.assertEqual(h_data["history"][0]["amount"], 42.5)
        self.assertEqual(h_data["history"][0]["raw_text"], "$42.50")
        self.assertEqual(h_data["history"][0]["source"], "manual")

        # 5b. Query history with invalid limit parameter gracefully defaults
        self.conn.request("GET", "/api/sites/balance/history?site=auto-site&limit=invalid")
        h_resp2 = self.conn.getresponse()
        self.assertEqual(h_resp2.status, 200)
        h_data2 = json.loads(h_resp2.read())
        self.assertTrue(h_data2.get("ok"))

    def test_store_delete_site_multi_credentials_cleanup(self):
        # Create site
        self.store.upsert_task(self.day, "multi-cred-site", "https://multicred.com", status="pending")
        with self.store.connect() as db:
            site = db.execute("SELECT id FROM sites WHERE name='multi-cred-site'").fetchone()
            site_id = site["id"]
            # Insert secondary credential directly into credentials table
            db.execute(
                "INSERT INTO credentials(ref, site_id, kind, label, updated_at) VALUES (?, ?, 'cookie', 'secondary', ?)",
                ("cred:multi-cred-site:cookie", site_id, self.store.now()),
            )
        # Set primary credential ref
        self.app.credentials.put("cred:multi-cred-site:token", "tok_val")
        self.app.credentials.put("cred:multi-cred-site:cookie", "cook_val")
        self.store.set_credential_ref("multi-cred-site", "cred:multi-cred-site:token", "token", "primary")

        # Call delete_site
        res = self.store.delete_site("multi-cred-site")
        self.assertIn("cred:multi-cred-site:token", res["credential_refs"])
        self.assertIn("cred:multi-cred-site:cookie", res["credential_refs"])

        # Check DB rows in child tables are cleaned up
        with self.store.connect() as db:
            remaining_creds = db.execute("SELECT COUNT(*) FROM credentials WHERE site_id=?", (site_id,)).fetchone()[0]
            self.assertEqual(remaining_creds, 0)

    def test_delete_site_ignore_missing_and_api(self):
        # 1. delete_site on non-existent with ignore_missing=True
        res = self.store.delete_site("ghost-site-abc", ignore_missing=True)
        self.assertEqual(res["name"], "ghost-site-abc")
        self.assertIsNone(res["site_id"])
        self.assertEqual(res["deleted_tasks"], 0)
        self.assertTrue(self.store.is_site_deleted("ghost-site-abc"))

        # 2. Call API with ignore_missing=True
        self.conn.request("GET", "/api/state")
        csrf = json.loads(self.conn.getresponse().read())["csrf"]
        headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        body = json.dumps({"name": "ghost-site-xyz", "ignore_missing": True}).encode()
        self.conn.request("POST", "/api/sites/delete", body, headers)
        resp = self.conn.getresponse()
        resp_data = resp.read()
        self.assertEqual(resp.status, 200, f"delete failed: {resp_data.decode()}")
        data = json.loads(resp_data)
        self.assertTrue(data.get("ok"))
        self.assertTrue(self.store.is_site_deleted("ghost-site-xyz"))

    def test_terminate_process_tree_cross_platform(self):
        from checkin_core.web import JobExecutor
        executor = JobExecutor(self.store, start_worker=False)

        # Mock a running process
        proc_mock = mock.MagicMock()
        proc_mock.pid = 999999
        proc_mock.poll.return_value = None

        # Test POSIX path
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("os.killpg", create=True) as mock_killpg:
            executor._terminate_process_tree(proc_mock, timeout=1.0)
            mock_killpg.assert_called_with(999999, signal.SIGTERM)

        # Test Windows path
        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.run") as mock_subrun:
            executor._terminate_process_tree(proc_mock, timeout=1.0)
            mock_subrun.assert_called_once()
            args = mock_subrun.call_args[0][0]
            self.assertEqual(args[:4], ["taskkill", "/F", "/T", "/PID"])
            self.assertEqual(args[4], "999999")


class LocalModeDispatchEndpointTests(unittest.TestCase):
    """Legacy local mode (default, executor on the same host) must be untouched:
    no dispatch store, node callbacks unreachable, /api/run still local-executes."""

    def setUp(self):
        self._backup = os.environ.pop("DAILY_CHECKIN_DISPATCH_MODE", None)
        self.td = TemporaryDirectory()
        self.store = SystemStore(Path(self.td.name) / "system.db")
        self.app = CheckinWebApp(self.store, FakeCredentials())
        self.server = __import__("http.server").server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.app.handler()
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.td.cleanup()
        if self._backup is not None:
            os.environ["DAILY_CHECKIN_DISPATCH_MODE"] = self._backup

    def test_local_mode_has_no_dispatch_store(self):
        self.assertIsNone(self.app.dispatch)
        self.assertIsNone(self.app.delivery)
        self.assertEqual(self.app.dispatch_mode, "local")

    def test_node_callbacks_are_401_without_dispatch(self):
        headers = {"Content-Type": "application/json", "X-Node-Token": "anything"}
        self.conn.request("POST", "/api/dispatch/heartbeat", "{}", headers)
        response = self.conn.getresponse()
        data = json.loads(response.read())
        self.assertEqual(response.status, 401)
        self.assertEqual(data["error"], "auth")

    def test_local_run_still_uses_legacy_executor(self):
        with mock.patch.object(self.app.executor, "submit", return_value=55) as submit:
            headers = {
                "Content-Type": "application/json",
                "X-CSRF-Token": self.app.csrf,
                "Authorization": "Bearer " + self.app.auth_token,
            }
            self.conn.request("POST", "/api/run", json.dumps({"sites": ["A"]}), headers)
            response = self.conn.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.status, 202)
            self.assertEqual(data["job_id"], 55)
            submit.assert_called_once_with(["A"])


class DispatchUiContractTests(unittest.TestCase):
    """Dispatch 前端契约（源码锚定式，仓库无前端 JS 测试框架）。"""

    def setUp(self):
        web = Path(__file__).resolve().parents[1] / "web"
        self.app_js = (web / "app.js").read_text(encoding="utf-8")
        self.index_html = (web / "index.html").read_text(encoding="utf-8")
        self.style_css = (web / "style.css").read_text(encoding="utf-8")

    def test_trigger_run_renders_remote_push_toast_without_hardcoded_cdp(self):
        self.assertIn('data.mode === "remote"', self.app_js)
        self.assertIn("已直推对应节点执行", self.app_js)
        # 误导性写死文案必须移除（作业 #420 根因之一）。
        self.assertNotIn("正在附着 Chrome 9222", self.app_js)

    def test_manual_complete_handles_dispatch_queue_response(self):
        self.assertIn('res.queued && res.mode === "remote"', self.app_js)
        self.assertIn("已发布手动完成指令至节点", self.app_js)

    def test_queue_view_renders_dispatch_node_bar(self):
        self.assertIn('id="dispatch-node-bar"', self.index_html)
        self.assertIn("renderQueueView", self.app_js)
        m = self.app_js.index("function renderQueueView")
        block = self.app_js[m:self.app_js.index("function renderHealthView")]
        self.assertIn("state.dispatch", block)
        self.assertIn('dispatch.mode === "remote"', block)
        self.assertIn("n.online", block)
        self.assertIn("在线", block)
        self.assertIn("离线", block)
        self.assertIn("j.node", block)

    def test_dispatch_node_styles_exist_without_inline_styles(self):
        self.assertIn(".dispatch-node-bar", self.style_css)
        self.assertIn(".dispatch-node-bar.is-visible", self.style_css)
        self.assertIn(".dispatch-node-hint", self.style_css)
        self.assertIn(".dispatch-job-node", self.style_css)
        self.assertNotIn("style=", self.index_html)
        self.assertNotIn("style=", self.app_js)

    def test_trigger_run_handles_multi_node_job_list(self):
        m = self.app_js.index("async function triggerRun")
        block = self.app_js[m:self.app_js.index("async function", m + 10)]
        self.assertIn("data.jobs ||", block)
        self.assertIn("已直推对应节点执行", block)
        # 旧的单作业文案被多作业摘要取代（仍然兼容单作业回退形状）。
        self.assertIn("job_id: data.job_id", block)

    def test_task_tile_renders_execution_channel_chip(self):
        """统一清单：执行平台只是任务上的渠道参数（tile-category-tag node）。"""
        m = self.app_js.index("function renderTile(")
        block = self.app_js[m:self.app_js.index("function renderTilesGrid")]
        self.assertIn('tile-category-tag node', block)
        self.assertIn("task.node", block)
        self.assertIn("执行渠道", block)
        self.assertIn(".tile-category-tag.node", self.style_css)

    def test_tasks_view_renders_platform_filter_chips(self):
        """平台分组筛选：remote 模式下按执行节点拆分清单（待签计数），与状态筛选正交。"""
        m = self.app_js.index("function renderTasksView")
        block = self.app_js[m:self.app_js.index("function renderManualView")]
        self.assertIn('id="tasks-node-filter-chips"', self.index_html)
        self.assertIn("#tasks-node-filter-chips", block)
        self.assertIn("currentNodeFilter", block)
        # 每个节点 chip 带该平台任务总数与待签计数
        self.assertIn("待签", block)
        self.assertIn("全签", block)
        # getFilteredTasks 里 node 筛选只在 remote 模式应用（本地模式不误伤）。
        # 注意函数顺序: getFilteredTasks 之后紧跟 renderTile。
        g = self.app_js.index("function getFilteredTasks")
        gblock = self.app_js[g:self.app_js.index("function renderTile(")]
        self.assertIn("currentNodeFilter", gblock)
        self.assertIn('state.dispatch && state.dispatch.mode === "remote"', gblock)
        # 选中事件绑定 + CSS 存在且默认隐藏
        self.assertIn("nodeFilterChips.addEventListener", self.app_js)
        self.assertIn(".node-filter-chips", self.style_css)
        self.assertIn(".node-filter-chips.is-visible", self.style_css)

    def test_node_filter_select_strips_full_count_paren_group(self):
        """节点 chip 选中解析必须整组剥离计数括号（2026-11-07 实测踩坑回归锚点）。

        chip 文案形如 "mac (114 · 待签113)"：旧解析 \\(\\d+\\)$ 只认纯数字尾、
        匹配失败，再剥 ·.*$ 后残留 "mac (114" —— 过滤条件恒假 → 点 mac/win
        平台标签得到空清单。修复=经共享 parseChipValue 整组剥离。
        """
        m = self.app_js.index("const nodeFilterChips")
        block = self.app_js[m:self.app_js.index("const manualFilterChips")]
        self.assertIn("parseChipValue(", block)
        # 旧的缺陷正则不得回到本 handler（纯数字尾匹配在 "· 待签N)" 形态下必然失败）
        self.assertNotIn(r'replace(/\s*\(\d+\)$/, "")', block)
        self.assertNotIn(r'replace(/\s*·.*$/', block)

    def test_chip_value_round_trip_shared_helper(self):
        """「label ↔ 筛选值」往返契约收敛到共享 stripCountSuffix/parseChipValue。

        深度 review（2026-11-07）：同类 \\(\\d+\\)$ 解析散在 prompt-chips 组件
        高亮 + filterChips / manualFilterChips / nodeFilterChips 三处 select，
        任一处往 label 加后缀就会静默复现空集 bug——必须单点实现。
        """
        self.assertIn("function stripCountSuffix", self.app_js)
        self.assertIn("function parseChipValue", self.app_js)
        self.assertIn(r'replace(/\s*\([^)]*\)\s*$/, "")', self.app_js)
        for anchor in ("filterChips.addEventListener", "nodeFilterChips.addEventListener",
                       "manualFilterChips.addEventListener"):
            m = self.app_js.index(anchor)
            block = self.app_js[m:m + 400]
            self.assertIn("parseChipValue(", block, anchor)
        # prompt-chips 组件的 active 高亮匹配同契约（第 5 处同类，组件内联正则退役）
        self.assertIn("const activeClean = stripCountSuffix(active)", self.app_js)
        self.assertIn("const itemClean = stripCountSuffix(item)", self.app_js)
        # 旧纯数字尾正则全量退役（全文件范围，含组件）
        self.assertNotIn(r'replace(/\s*\(\d+\)$/, "")', self.app_js)

    def test_node_filter_uses_dispatch_default_node_fallback(self):
        """过滤与 chip 计数同口径：缺 node 行按 default_node 归属，而非硬编码 mac。

        深度 review（2026-11-07）：getFilteredTasks 用 `t.node || "mac"`、节点
        chip 计数用 `t.node || dispatch.default_node || "mac"`——default_node 改为
        非 mac 时会出现「计入 chip 计数却被该 chip 筛选排除」的隐形行。
        """
        g = self.app_js.index("function getFilteredTasks")
        gblock = self.app_js[g:self.app_js.index("function renderTile(")]
        self.assertIn("state.dispatch.default_node", gblock)
        self.assertNotIn('t.node || "mac"', gblock)

    def test_node_filter_resets_when_node_deregistered(self):
        """节点下线/改名后陈旧筛选自愈：过滤前把失效节点选择重置为「全部」。

        深度 review（2026-11-07）：remove-node 后 chips 高亮回落「全部」但
        currentNodeFilter 仍按旧节点名硬滤 → 0 卡片且无提示。重置必须发生在
        getFilteredTasks() 之前（renderTasksView 开头），否则晚一拍。
        """
        m = self.app_js.index("function renderTasksView")
        block = self.app_js[m:self.app_js.index("function renderManualView")]
        self.assertIn("liveNames.includes(currentNodeFilter)", block)
        self.assertIn('currentNodeFilter = "全部"', block)
        # 自愈先于过滤执行（注释里出现 getFilteredTasks() 字样，用语句全文锚定）
        self.assertLess(block.index("liveNames.includes(currentNodeFilter)"),
                        block.index("const filteredTasks = getFilteredTasks()"))

    def test_node_chip_active_highlight_exact_prefix(self):
        """chip 高亮精确到 "名称 (" 前缀：节点名互为前缀（win / win-x）时不点亮错误项。"""
        m = self.app_js.index("const activeSug")
        block = self.app_js[m:m + 400]
        self.assertIn("startsWith(`${currentNodeFilter} (`", block)


class DispatchRemoteWebTests(unittest.TestCase):
    """Cloud-panel ("remote") dispatch mode: /api/run only enqueues and the
    node callbacks (result/heartbeat) authenticate with the per-node token.

    The local-mode /api/run contract is covered by WebTests above and must
    stay untouched — dispatch is opt-in via DAILY_CHECKIN_DISPATCH_MODE.
    """

    def setUp(self):
        self._env_backup = {
            key: os.environ.pop(key, None)
            for key in (
                "DAILY_CHECKIN_DISPATCH_MODE",
                "DAILY_CHECKIN_DISPATCH_DB",
                "DAILY_CHECKIN_DISPATCH_SWEEP_INTERVAL",
                "DAILY_CHECKIN_DISPATCH_AUTO_WAKE",
                "DAILY_CHECKIN_WEB_PASSWORD",
            )
        }
        os.environ["DAILY_CHECKIN_DISPATCH_MODE"] = "remote"
        self.td = TemporaryDirectory()
        os.environ["DAILY_CHECKIN_DISPATCH_DB"] = str(Path(self.td.name) / "dispatch.db")
        # Keep the background delivery thread idle for the whole test; the
        # sweeper is exercised directly in tests/test_dispatch.py.
        os.environ["DAILY_CHECKIN_DISPATCH_SWEEP_INTERVAL"] = "3600"
        os.environ["DAILY_CHECKIN_DISPATCH_AUTO_WAKE"] = "0"
        self.store = SystemStore(Path(self.td.name) / "system.db")
        self.creds = FakeCredentials()
        self.app = CheckinWebApp(self.store, self.creds)
        self.node_token = self.app.dispatch.add_node("mac", "http://127.0.0.1:19999")
        self.server = __import__("http.server").server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.app.handler()
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.td.cleanup()
        for key, value in self._env_backup.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def request_json(self, method, path, body=None, csrf=None, auth=None, node_token=None):
        headers = {"Content-Type": "application/json"}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if auth is not None:
            headers["Authorization"] = "Bearer " + auth
        if node_token is not None:
            headers["X-Node-Token"] = node_token
        self.conn.request(method, path, json.dumps(body or {}).encode(), headers)
        response = self.conn.getresponse()
        data = json.loads(response.read())
        return response.status, data

    def test_run_endpoint_enqueues_dispatch_job_without_local_execution(self):
        status, data = self.request_json(
            "POST", "/api/run", {"sites": ["伏枥"]}, self.app.csrf, auth=self.app.auth_token
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["mode"], "remote")
        self.assertEqual(data["node"], "mac")
        job = self.app.dispatch.get_job(data["job_id"])
        self.assertEqual(job["kind"], "site")
        self.assertEqual(job["sites"], ["伏枥"])
        self.assertEqual(job["status"], "queued")
        # Nothing was handed to the legacy in-process executor.
        self.assertEqual(self.app.executor._queue.qsize(), 0)

    def test_run_all_endpoint_enqueues_all_kind(self):
        status, data = self.request_json(
            "POST", "/api/run", {"sites": []}, self.app.csrf, auth=self.app.auth_token
        )
        self.assertEqual(status, 202)
        job = self.app.dispatch.get_job(data["job_id"])
        self.assertEqual(job["kind"], "all")

    def test_multi_node_run_fans_out_per_node_jobs(self):
        """Mixed Mac/Windows selection → one queued job per node (site_nodes map)."""
        self.app.dispatch.add_node("win", "http://127.0.0.1:19778")
        self.app.dispatch.set_site_node("hiyo", "win")
        self.app.dispatch.set_site_node("relayfor", "win")
        status, data = self.request_json(
            "POST", "/api/run", {"sites": ["伏枥", "hiyo", "A", "relayfor"]},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["mode"], "remote")
        # Two jobs, first-seen node order; back-compat top-level shape absent
        # for multi-job responses (frontend reads data.jobs).
        self.assertEqual(len(data["jobs"]), 2)
        self.assertEqual(data["jobs"][0]["node"], "mac")
        self.assertEqual(data["jobs"][0]["sites"], ["伏枥", "A"])
        self.assertEqual(data["jobs"][1]["node"], "win")
        self.assertEqual(data["jobs"][1]["sites"], ["hiyo", "relayfor"])
        self.assertNotIn("job_id", data)
        job_a = self.app.dispatch.get_job(data["jobs"][0]["job_id"])
        job_b = self.app.dispatch.get_job(data["jobs"][1]["job_id"])
        self.assertEqual(job_a["kind"], "site")
        self.assertEqual(job_a["node"], "mac")
        self.assertEqual(job_b["node"], "win")

    def test_run_all_fans_out_to_every_registered_node(self):
        """一次触发，多平台并行：run_all 为每个注册节点各入队一个全量批次作业。"""
        self.app.dispatch.add_node("win", "http://127.0.0.1:19778")
        status, data = self.request_json(
            "POST", "/api/run", {"sites": []}, self.app.csrf, auth=self.app.auth_token
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["mode"], "remote")
        self.assertEqual(
            [(j["node"], j["sites"]) for j in data["jobs"]], [("mac", []), ("win", [])]
        )
        kinds = {self.app.dispatch.get_job(j["job_id"])["kind"] for j in data["jobs"]}
        self.assertEqual(kinds, {"all"})

    def test_state_merges_node_replica_tasks_with_channel_tags(self):
        """统一清单：面板把其它节点的只读副本任务合并进 /api/state，
        每行带 node 渠道参数（2026-11-06 win=system-win.db）。"""
        day = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.store.upsert_task(day, "伏枥", "https://dash.free-ai.de5.net/profile")
        win_replica = Path(self.td.name) / "system-win.db"
        win_store = SystemStore(win_replica)
        win_store.upsert_task(day, "hiyo", "https://hiyo.example")
        os.environ["DAILY_CHECKIN_NODE_REPLICAS"] = f"win={win_replica}"
        try:
            _, state = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        finally:
            os.environ.pop("DAILY_CHECKIN_NODE_REPLICAS", None)
        nodes = {t["name"]: t["node"] for t in state["tasks"]}
        # Primary replica rows tagged with the default node.
        self.assertEqual(nodes.get("伏枥"), "mac")
        # Second replica's rows merged and tagged with their channel.
        self.assertEqual(nodes.get("hiyo"), "win")
        # The panel must not have written to the replica (read-only merge).
        self.assertEqual(
            [t["status"] for t in win_store.tasks(day) if t["name"] == "hiyo"],
            ["pending"],
        )

    def test_state_dedupes_site_across_nodes_by_pinned_owner(self):
        """同一站点在两个节点的目录里都 enabled 时，清单只保留归属节点那一行。

        2026-11-09：hiyo/relayfor 在 Mac staging DB 里也 enabled，面板"全部"
        视图会渲染成两张同名卡片；site_nodes 的 pin 才是唯一归属。
        """
        day = datetime.now().strftime("%Y-%m-%d")
        self.store.upsert_task(day, "hiyo", "https://hiyo.example")
        win_replica = Path(self.td.name) / "system-win.db"
        win_store = SystemStore(win_replica)
        win_store.upsert_task(day, "hiyo", "https://hiyo.example")
        self.app.dispatch.set_site_node("hiyo", "win")
        os.environ["DAILY_CHECKIN_NODE_REPLICAS"] = f"win={win_replica}"
        try:
            _, state = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        finally:
            os.environ.pop("DAILY_CHECKIN_NODE_REPLICAS", None)
        rows = [t for t in state["tasks"] if t["name"] == "hiyo"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["node"], "win")

    def test_state_keeps_default_row_when_owner_has_no_replica(self):
        """pin 到 win 但 win 副本还没有该行时，不能把站点从清单里弄丢。"""
        day = datetime.now().strftime("%Y-%m-%d")
        self.store.upsert_task(day, "hiyo", "https://hiyo.example")
        win_replica = Path(self.td.name) / "system-win.db"
        SystemStore(win_replica)  # empty replica (schema only, no rows)
        self.app.dispatch.set_site_node("hiyo", "win")
        os.environ["DAILY_CHECKIN_NODE_REPLICAS"] = f"win={win_replica}"
        try:
            _, state = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        finally:
            os.environ.pop("DAILY_CHECKIN_NODE_REPLICAS", None)
        rows = [t for t in state["tasks"] if t["name"] == "hiyo"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["node"], "mac")
        self.app.dispatch.set_site_node("hiyo", "")

    def test_dedupe_by_owner_pure_function(self):
        """Pure helper semantics: pin wins; otherwise last seen; order stable."""
        from checkin_core.web import _dedupe_tasks_by_owner

        rows = [
            {"name": "a", "node": "mac", "status": "done"},
            {"name": "b", "node": "mac", "status": "pending"},
            {"name": "a", "node": "win", "status": "failed"},
            {"name": "b", "node": "win", "status": "done"},
            {"name": "c", "node": "mac", "status": "pending"},
            {"name": "c", "node": "win", "status": "done"},
        ]
        out = _dedupe_tasks_by_owner(rows, {"a": "mac", "b": "win"})
        self.assertEqual([t["name"] for t in out], ["a", "b", "c"])
        self.assertEqual({t["name"]: (t["node"], t["status"]) for t in out}, {
            "a": ("mac", "done"),   # pinned row, even though win row came later
            "b": ("win", "done"),   # pinned row
            "c": ("win", "done"),   # unpinned -> last seen
        })
        # Pinned owner has no row: keep a row instead of dropping the site.
        out = _dedupe_tasks_by_owner(rows, {"b": "missing"})
        self.assertEqual([t["name"] for t in out], ["a", "b", "c"])
        self.assertEqual({t["name"]: t["node"] for t in out}["b"], "win")
        # Rows without a name are dropped, not rendered as blank tiles.
        self.assertEqual(
            _dedupe_tasks_by_owner([{"name": "a", "node": "mac"}, {"node": "mac"}], {}),
            [{"name": "a", "node": "mac"}],
        )

    def test_state_missing_replica_is_tolerated(self):
        os.environ["DAILY_CHECKIN_NODE_REPLICAS"] = (
            f"win={Path(self.td.name) / 'nonexistent.db'}"
        )
        try:
            status, state = self.request_json("GET", "/api/state", auth=self.app.auth_token)
            self.assertEqual(status, 200)
            self.assertEqual(
                [t["node"] for t in state["tasks"]],
                ["mac"] * len(state["tasks"]),
            )
        finally:
            os.environ.pop("DAILY_CHECKIN_NODE_REPLICAS", None)

    def test_single_node_run_keeps_backcompat_shape(self):
        status, data = self.request_json(
            "POST", "/api/run", {"sites": ["伏枥"]}, self.app.csrf, auth=self.app.auth_token
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["job_id"], data["jobs"][0]["job_id"])
        self.assertEqual(data["node"], "mac")

    def test_manual_complete_routes_via_site_node_map(self):
        self.app.dispatch.add_node("win", "http://127.0.0.1:19778")
        self.app.dispatch.set_site_node("hiyo", "win")
        day = datetime.now().strftime("%Y-%m-%d")
        self.store.upsert_task(day, "hiyo", "")
        status, data = self.request_json(
            "POST", "/api/tasks/complete", {"name": "hiyo", "day": day},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["node"], "win")
        job = self.app.dispatch.get_job(data["job_id"])
        self.assertEqual(job["node"], "win")
        self.assertEqual(job["payload"], {"site": "hiyo", "day": day})

    def test_tasks_complete_enqueues_manual_dispatch_job(self):
        self.store.upsert_task(datetime.now().strftime("%Y-%m-%d"), "pmcat", "")
        day = datetime.now().strftime("%Y-%m-%d")
        status, data = self.request_json(
            "POST", "/api/tasks/complete", {"name": "pmcat", "day": day},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 202)
        self.assertTrue(data["queued"])
        self.assertEqual(data["mode"], "remote")
        job = self.app.dispatch.get_job(data["job_id"])
        self.assertEqual(job["kind"], "manual")
        self.assertEqual(job["payload"], {"site": "pmcat", "day": day})
        # The replica DB must stay untouched by the panel itself.
        row = self.store.tasks(day)
        statuses = [t["status"] for t in row if t["name"] == "pmcat"]
        self.assertEqual(statuses, ["pending"])

    def test_remote_mode_blocks_authoritative_writes(self):
        """Only Obsidian sync stays hard-blocked: everything else now
        dispatches a job to the owning node instead of refusing."""
        status, data = self.request_json(
            "POST", "/api/sync/obsidian", {"day": "2026-11-06"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("执行节点", data["error"])

    def test_authoritative_writes_dispatch_instead_of_blocking(self):
        """Every panel write to the authoritative DB / keychain enqueues a
        job for the owning node and returns 202 with dispatch metadata."""
        day = datetime.now().strftime("%Y-%m-%d")
        cases = (
            ("/api/tasks", {"name": "newsite", "url": "https://a.example"}, "add_site"),
            ("/api/sites/update", {"name": "newsite", "checkin_mode": "manual"}, "update_site"),
            ("/api/credentials", {"site": "newsite", "kind": "token", "secret": "s3cret"}, "set_credential"),
            ("/api/credentials/delete", {"ref": "site:newsite:token"}, "delete_credential"),
            ("/api/config/update", {"configs": {"batch_timeout_s": 600}}, "update_config"),
            ("/api/sites/balance", {"name": "newsite", "amount": "12.5"}, "update_balance"),
        )
        for path, body, expected_kind in cases:
            status, data = self.request_json(
                "POST", path, body, self.app.csrf, auth=self.app.auth_token
            )
            self.assertEqual(status, 202, path)
            self.assertTrue(data["queued"], path)
            self.assertEqual(data["mode"], "remote", path)
            job = self.app.dispatch.get_job(data["job_id"])
            self.assertEqual(job["kind"], expected_kind, path)
            self.assertEqual(job["status"], "queued", path)

    def test_sites_delete_enqueues_delete_site_dispatch_job(self):
        """Remote deletion dispatches to the executor node owning the
        authoritative DB + credentials backend; the panel replica stays
        untouched until the node executes and syncs back."""
        self.app.dispatch.add_node("win", "http://127.0.0.1:19778")
        self.app.dispatch.set_site_node("hiyo", "win")
        status, data = self.request_json(
            "POST", "/api/sites/delete", {"name": "hiyo"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 202)
        self.assertTrue(data["queued"])
        self.assertEqual(data["mode"], "remote")
        self.assertEqual(data["node"], "win")
        job = self.app.dispatch.get_job(data["job_id"])
        self.assertEqual(job["kind"], "delete_site")
        self.assertEqual(job["payload"], {"site": "hiyo", "ignore_missing": False})

    def test_node_callback_requires_token(self):
        status, data = self.request_json("POST", "/api/dispatch/result", {"job_id": 1, "status": "done"})
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "auth")
        status, _ = self.request_json(
            "POST", "/api/dispatch/result", {"job_id": 1, "status": "done"},
            node_token="wrong-token",
        )
        self.assertEqual(status, 401)

    def test_node_result_rejects_invalid_status_and_job_id(self):
        status, data = self.request_json(
            "POST", "/api/dispatch/result", {"job_id": 1, "status": "running"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("status", data["error"])
        status, data = self.request_json(
            "POST", "/api/dispatch/result", {"job_id": "abc", "status": "done"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("job_id", data["error"])

    def test_run_validation_still_enforced_in_remote_mode(self):
        status, data = self.request_json(
            "POST", "/api/run", {"sites": "not-a-list"}, self.app.csrf, auth=self.app.auth_token
        )
        self.assertEqual(status, 400)
        self.assertIn("list", data["error"])
        status, data = self.request_json(
            "POST", "/api/run", {"sites": ["A,B"]}, self.app.csrf, auth=self.app.auth_token
        )
        self.assertEqual(status, 400)
        self.assertIn("comma", data["error"])
        # Nothing was enqueued by the rejected payloads.
        self.assertEqual(self.app.dispatch.jobs(10), [])

    def test_manual_done_result_echoes_to_panel_replica(self):
        """A manual completion reported done by the node is replayed on the
        panel replica immediately, so the UI flips without waiting for the
        node's scp DB sync (~2-10s). Echo must be idempotent-safe: the later
        sync carries identical state."""
        day = datetime.now().strftime("%Y-%m-%d")
        # The panel replica in production already carries the site row plus
        # today's pending daily_task; /api/state only shows sites that have a
        # daily_tasks row for the day (tasks() joins on it).
        self.store.upsert_task(day, "echo-site", "https://echo.example",
                               checkin_mode="manual", tags="manual",
                               status="pending", source="manual")
        status, data = self.request_json(
            "POST", "/api/tasks/complete", {"name": "echo-site", "day": day},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 202)
        self.assertTrue(data["queued"])
        job_id = data["job_id"]
        self.assertEqual(self.app.dispatch.get_job(job_id)["kind"], "manual")
        # Replica untouched while the job is only queued.
        task = next(t for t in self.store.tasks(day) if t["name"] == "echo-site")
        self.assertEqual(task["status"], "pending")
        # Node reports done → panel echoes the write locally.
        status, data = self.request_json(
            "POST", "/api/dispatch/result",
            {"job_id": job_id, "status": "done", "exit_code": 0,
             "message": "[echo-site] manual done"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        state = self.request_json("GET", "/api/state", auth=self.app.auth_token)[1]
        task = next(t for t in state["tasks"] if t["name"] == "echo-site")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["last_reason"], "manual_confirmed")
        # Echo is toggle-safe: a second complete (already done) would revert
        # on the node, but the echo must never crash the result endpoint.
        status, data = self.request_json(
            "POST", "/api/tasks/complete", {"name": "echo-site", "day": day},
            self.app.csrf, auth=self.app.auth_token,
        )
        job_id2 = data["job_id"]
        # A failed manual result must NOT echo anything.
        status, _ = self.request_json(
            "POST", "/api/dispatch/result",
            {"job_id": job_id2, "status": "failed", "exit_code": 2, "message": "boom"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 200)
        state = self.request_json("GET", "/api/state", auth=self.app.auth_token)[1]
        task = next(t for t in state["tasks"] if t["name"] == "echo-site")
        self.assertEqual(task["status"], "done")

    def test_update_site_result_echoes_to_panel_replica(self):
        """update_site reported done is replayed locally (same contract as
        manual completion) so config toggles flip without the scp sync."""
        day = datetime.now().strftime("%Y-%m-%d")
        self.store.upsert_task(day, "echo-config-site", "https://c.example",
                               checkin_mode="auto", status="pending", source="system")
        status, data = self.request_json(
            "POST", "/api/sites/update", {"name": "echo-config-site", "checkin_mode": "manual"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 202)
        job_id = data["job_id"]
        self.assertEqual(self.app.dispatch.get_job(job_id)["kind"], "update_site")
        status, _ = self.request_json(
            "POST", "/api/dispatch/result",
            {"job_id": job_id, "status": "done", "exit_code": 0, "message": "ok"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 200)
        state = self.request_json("GET", "/api/state", auth=self.app.auth_token)[1]
        task = next(t for t in state["tasks"] if t["name"] == "echo-config-site")
        self.assertEqual(task["checkin_mode"], "manual")

    def test_failed_update_site_result_does_not_echo(self):
        day = datetime.now().strftime("%Y-%m-%d")
        self.store.upsert_task(day, "no-echo-site", "https://n.example",
                               checkin_mode="auto", status="pending", source="system")
        status, data = self.request_json(
            "POST", "/api/sites/update", {"name": "no-echo-site", "checkin_mode": "manual"},
            self.app.csrf, auth=self.app.auth_token,
        )
        job_id = data["job_id"]
        status, _ = self.request_json(
            "POST", "/api/dispatch/result",
            {"job_id": job_id, "status": "failed", "exit_code": 2, "message": "boom"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 200)
        state = self.request_json("GET", "/api/state", auth=self.app.auth_token)[1]
        task = next(t for t in state["tasks"] if t["name"] == "no-echo-site")
        self.assertEqual(task["checkin_mode"], "auto")

    def test_tasks_complete_validation_enforced_in_remote_mode(self):
        status, data = self.request_json(
            "POST", "/api/tasks/complete", {"name": "pmcat", "day": "2026/11/06"},
            self.app.csrf, auth=self.app.auth_token,
        )
        self.assertEqual(status, 400)
        self.assertIn("day format", data["error"])
        self.assertEqual(self.app.dispatch.jobs(10), [])

    def test_state_does_not_materialize_replica(self):
        """Panel host treats system.db as a read-only replica: /api/state must
        not create pending rows (materialize=False), while the executor-side
        default snapshot() still materializes."""
        day = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO sites(name,url,provider,checkin_mode,enabled,created_at,updated_at)
                   VALUES('新站X','https://x.example','legacy_browser','auto',1,?,?)""",
                (now, now),
            )
        _, state = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        self.assertEqual([t["name"] for t in state["tasks"] if t["name"] == "新站X"], [])
        # Default snapshot() (executor-local web UI behaviour) still materializes.
        self.store.snapshot(day)
        _, state2 = self.request_json("GET", "/api/state", auth=self.app.auth_token)
        self.assertIn("新站X", [t["name"] for t in state2["tasks"]])

    def test_node_result_finishes_delivered_job(self):
        _, data = self.request_json(
            "POST", "/api/run", {"sites": ["伏枥"]}, self.app.csrf, auth=self.app.auth_token
        )
        job_id = data["job_id"]
        # Ownership guard still holds from 'queued' (2026-11-06 race fix lets
        # the owning node close an accepted-but-undelivered job, so prove
        # non-owners cannot via store-level ownership, then use the real token
        # for the HTTP path).
        self.assertFalse(self.app.dispatch.finish(job_id, "other-node", "done", 0, "forged"))
        self.assertEqual(self.app.dispatch.get_job(job_id)["status"], "queued")
        # Outcome callback beats the delivery CAS (fast node): result wins.
        status, data = self.request_json(
            "POST", "/api/dispatch/result",
            {"job_id": job_id, "status": "done", "exit_code": 0,
             "message": "[伏枥] ALREADY (4.6s)"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        job = self.app.dispatch.get_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertIn("ALREADY", job["message"])
        # Late delivery CAS must not resurrect a terminal job.
        self.assertFalse(self.app.dispatch.mark_delivered(job_id))
        self.assertEqual(self.app.dispatch.get_job(job_id)["status"], "done")
        # A second callback must not overwrite the terminal state.
        status, _ = self.request_json(
            "POST", "/api/dispatch/result",
            {"job_id": job_id, "status": "failed", "exit_code": 2, "message": "double"},
            node_token=self.node_token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(
            self.app.dispatch.get_job(job_id)["message"], "[伏枥] ALREADY (4.6s)"
        )

    def test_node_heartbeat_marks_node_online_in_state(self):
        status, _ = self.request_json(
            "POST", "/api/dispatch/heartbeat", {}, node_token=self.node_token
        )
        self.assertEqual(status, 200)
        status, state = self.request_json(
            "GET", "/api/state", auth=self.app.auth_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["dispatch"]["mode"], "remote")
        nodes = state["dispatch"]["nodes"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["name"], "mac")
        self.assertTrue(nodes[0]["online"])
        self.assertNotIn("token", nodes[0])
        self.assertNotIn("token_hash", nodes[0])

    def test_state_jobs_come_from_dispatch_queue(self):
        self.request_json(
            "POST", "/api/run", {"sites": ["伏枥"]}, self.app.csrf, auth=self.app.auth_token
        )
        status, state = self.request_json(
            "GET", "/api/state", auth=self.app.auth_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(state["jobs"]), 1)
        job = state["jobs"][0]
        self.assertEqual(job["kind"], "site")
        self.assertEqual(job["node"], "mac")
        self.assertEqual(job["status"], "queued")

    def test_full_dispatch_loop_with_mock_agent(self):
        """End-to-end over real HTTP: panel enqueue → DeliveryWorker._deliver_http
        → mock node agent → agent posts result via /api/dispatch/result → done."""
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = []

        class MockAgent(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/dispatch":
                    self.send_response(404)
                    self.end_headers()
                    return
                if (self.headers.get("X-Node-Token") or "") != self.node_token_ref[0]:
                    self.send_response(401)
                    self.end_headers()
                    return
                received.append(body)
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                # Node reports back over the public callback (real HTTP).
                panel_port = self.panel_port_ref[0]
                req = urllib.request.Request(
                    f"http://127.0.0.1:{panel_port}/api/dispatch/result",
                    data=json.dumps({
                        "job_id": body["job_id"],
                        "status": "done",
                        "exit_code": 0,
                        "message": "[伏枥] ALREADY (4.6s)",
                    }).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Node-Token": self.node_token_ref[0],
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5).read()

            # per-server references wired after construction
            node_token_ref = [None]
            panel_port_ref = [None]

        agent_server = ThreadingHTTPServer(("127.0.0.1", 0), MockAgent)
        MockAgent.node_token_ref[0] = self.node_token
        MockAgent.panel_port_ref[0] = self.server.server_port
        agent_thread = threading.Thread(target=agent_server.serve_forever, daemon=True)
        agent_thread.start()
        try:
            # Point the registered node at the mock agent and re-enqueue.
            self.app.dispatch.remove_node("mac")
            mock_token = self.app.dispatch.add_node(
                "mac", f"http://127.0.0.1:{agent_server.server_port}"
            )
            MockAgent.node_token_ref[0] = mock_token
            _, data = self.request_json(
                "POST", "/api/run", {"sites": ["伏枥"]}, self.app.csrf, auth=self.app.auth_token
            )
            job_id = data["job_id"]
            pushed = self.app.delivery.sweep()
            self.assertEqual(pushed, 1)
            # Wait for the async node callback to land.
            for _ in range(50):
                job = self.app.dispatch.get_job(job_id)
                if job["status"] == "done":
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["exit_code"], 0)
            self.assertIn("ALREADY", job["message"])
            self.assertEqual(received[0]["sites"], ["伏枥"])
            self.assertEqual(received[0]["kind"], "site")
        finally:
            agent_server.shutdown()
            agent_server.server_close()
            agent_thread.join(3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

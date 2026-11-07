from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from checkin_core.dispatch import (
    DEFAULT_NODE,
    DeliveryWorker,
    DispatchStore,
    dispatch_db_path,
    dispatch_mode,
    hash_token,
)

ROOT = Path(__file__).resolve().parents[1]

STORE_DIR = Path(__file__).resolve().parents[1]


class DispatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = DispatchStore(Path(self.td.name) / "dispatch.db")

    def tearDown(self):
        self.td.cleanup()

    def test_enqueue_and_jobs_shape_matches_legacy(self):
        job_id = self.store.enqueue("site", site_names=["伏枥"], node="mac")
        jobs = self.store.jobs(10)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        # Frontend renderQueueView compatibility: legacy column names.
        for key in ("id", "kind", "site_names_json", "status", "created_at",
                    "started_at", "finished_at", "exit_code", "message"):
            self.assertIn(key, job)
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["kind"], "site")
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["exit_code"])
        self.assertEqual(job["sites"], ["伏枥"])
        self.assertEqual(job["node"], "mac")

    def test_enqueue_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            self.store.enqueue("explode")

    def test_mark_delivered_cas(self):
        job_id = self.store.enqueue("all", node="mac")
        self.assertTrue(self.store.mark_delivered(job_id))
        # Second delivery of the same queued row must not happen (CAS).
        self.assertFalse(self.store.mark_delivered(job_id))
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "running")
        self.assertIsNotNone(job["started_at"])
        # Attempts are cumulative across claim/retry cycles (not zeroed here).
        self.assertEqual(job["delivery_attempts"], 0)

    def test_finish_ownership_and_status_guard(self):
        job_id = self.store.enqueue("site", site_names=["A"], node="mac")
        # Wrong node ownership is rejected even from 'queued' (forged callback).
        self.assertFalse(self.store.finish(job_id, "home-win", "done", 0, "forged"))
        self.assertEqual(self.store.get_job(job_id)["status"], "queued")
        # Owner may finish from 'queued': a fast node's result callback can beat
        # the panel's mark_delivered sweep (real race fixed 2026-11-06).
        self.assertTrue(self.store.finish(job_id, "mac", "done", 0, "[A] ALREADY (4.6s)"))
        # Terminal state is exactly-once — a second callback (or late delivery
        # CAS) must not rewrite it.
        self.assertFalse(self.store.finish(job_id, "mac", "failed", 2, "double"))
        self.assertFalse(self.store.mark_delivered(job_id))
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["exit_code"], 0)
        self.assertIn("ALREADY", job["message"])
        self.assertIsNotNone(job["finished_at"])

    def test_revert_delivery_returns_job_to_queue(self):
        job_id = self.store.enqueue("site", site_names=["A"], node="mac")
        self.store.mark_delivered(job_id)
        self.assertTrue(self.store.revert_delivery(job_id))
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["started_at"])
        # Revert leaves terminal states untouched (node result won the race).
        self.assertTrue(self.store.finish(job_id, "mac", "done", 0, "fast"))
        self.assertFalse(self.store.revert_delivery(job_id))
        self.assertEqual(self.store.get_job(job_id)["status"], "done")

    def test_finish_rejects_invalid_status_and_truncates_message(self):
        job_id = self.store.enqueue("manual", payload={"site": "pmcat", "day": "2026-11-06"})
        with self.assertRaises(ValueError):
            self.store.finish(job_id, "mac", "running", 0, "")
        self.store.mark_delivered(job_id)
        long_msg = "x" * 9000
        self.assertTrue(self.store.finish(job_id, "mac", "failed", 2, long_msg))
        self.assertLessEqual(len(self.store.get_job(job_id)["message"]), 4000)

    def test_delivery_attempts_auto_fail(self):
        job_id = self.store.enqueue("site", site_names=["A"], node="mac")
        for _ in range(3):
            attempts = self.store.increment_delivery_attempt(job_id, max_attempts=3)
        self.assertEqual(attempts, 3)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("delivery_failed", job["message"])
        self.assertEqual(job["exit_code"], 2)

    def test_reap_stale_running(self):
        job_id = self.store.enqueue("all", node="mac")
        # Fresh running job must survive the reaper.
        self.store.mark_delivered(job_id)
        self.assertEqual(self.store.reap_stale_running(max_age_s=7200), [])
        # Backdate started_at beyond the window → reaped with clear message.
        old = (datetime.now() - timedelta(seconds=7300)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.store.connect() as db:
            db.execute("UPDATE dispatch_jobs SET started_at=? WHERE id=?", (old, job_id))
        reaped = self.store.reap_stale_running(max_age_s=7200)
        self.assertEqual(reaped, [job_id])
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("node_timeout", job["message"])

    def test_nodes_token_and_heartbeat(self):
        token = self.store.add_node("mac", "http://10.144.144.100:9777")
        self.assertTrue(token)
        # Token resolves back to the node; wrong token resolves to nothing.
        self.assertEqual(self.store.verify_node_token(token), "mac")
        self.assertIsNone(self.store.verify_node_token("wrong"))
        self.assertIsNone(self.store.verify_node_token(""))
        # Node listing must never leak token material.
        for node in self.store.nodes():
            self.assertNotIn("token", node)
            self.assertNotIn("token_hash", node)
            self.assertFalse(node["online"])
        self.assertTrue(self.store.heartbeat("mac"))
        self.assertTrue(self.store.nodes()[0]["online"])
        # A beat within the online window (750s, must cover the 300s agent
        # cadence + clock skew) keeps the node online.
        old = (datetime.now() - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.store.connect() as db:
            db.execute("UPDATE dispatch_nodes SET last_seen_at=? WHERE name='mac'", (old,))
        self.assertTrue(self.store.nodes()[0]["online"])
        # Only beats older than the window go dark.
        older = (datetime.now() - timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.store.connect() as db:
            db.execute("UPDATE dispatch_nodes SET last_seen_at=? WHERE name='mac'", (older,))
        self.assertFalse(self.store.nodes()[0]["online"])
        # Duplicate registration is rejected.
        with self.assertRaises(ValueError):
            self.store.add_node("mac", "http://other:1")
        self.assertTrue(self.store.remove_node("mac"))
        self.assertIsNone(self.store.verify_node_token(token))

    def test_default_node(self):
        self.assertEqual(self.store.default_node(), DEFAULT_NODE)
        self.store.add_node("home-win", "http://10.144.144.10:9777")
        self.store.add_node("mac", "http://10.144.144.100:9777")
        # First registered node wins as the default target.
        self.assertEqual(self.store.default_node(), "home-win")

    def test_hash_token_is_sha256_hex(self):
        import hashlib
        self.assertEqual(hash_token("abc"), hashlib.sha256(b"abc").hexdigest())


class DeliveryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = DispatchStore(Path(self.td.name) / "dispatch.db")

    def tearDown(self):
        self.td.cleanup()

    def test_sweep_pushes_queued_and_marks_running(self):
        token = self.store.add_node("mac", "http://10.144.144.100:9777")
        job_id = self.store.enqueue("site", site_names=["伏枥"], node="mac")
        delivered = []

        def fake_deliver(job, node, timeout_s):
            delivered.append((job["id"], node["name"], node["base_url"], job["sites"]))
            return True

        worker = DeliveryWorker(self.store, deliver_fn=fake_deliver, start_worker=False)
        pushed = worker.sweep()
        self.assertEqual(pushed, 1)
        self.assertEqual(delivered, [(job_id, "mac", "http://10.144.144.100:9777", ["伏枥"])])
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "running")

    def test_sweep_keeps_job_queued_on_node_unreachable(self):
        self.store.add_node("mac", "http://10.144.144.100:9777")
        job_id = self.store.enqueue("all", node="mac")
        worker = DeliveryWorker(
            self.store, deliver_fn=lambda *a: False, max_attempts=3, start_worker=False
        )
        self.assertEqual(worker.sweep(), 0)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["delivery_attempts"], 1)
        # Exhaust attempts → auto-failed with delivery_failed.
        for _ in range(2):
            worker.sweep()
        self.assertEqual(self.store.get_job(job_id)["status"], "failed")

    def test_sweep_reaps_stale_before_delivery(self):
        job_id = self.store.enqueue("all", node="mac")
        self.store.mark_delivered(job_id)
        old = (datetime.now() - timedelta(seconds=99999)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.store.connect() as db:
            db.execute("UPDATE dispatch_jobs SET started_at=? WHERE id=?", (old, job_id))
        worker = DeliveryWorker(self.store, deliver_fn=lambda *a: True, start_worker=False)
        worker.sweep()
        self.assertEqual(self.store.get_job(job_id)["status"], "failed")

    def test_idle_sweep_makes_no_delivery_calls(self):
        # The no-polling contract: with an empty queue the sweeper must not
        # touch any node agent at all.
        self.store.add_node("mac", "http://10.144.144.100:9777")
        calls = []

        def fake_deliver(*a):
            calls.append(a)
            return True

        worker = DeliveryWorker(self.store, deliver_fn=fake_deliver, start_worker=False)
        self.assertEqual(worker.sweep(), 0)
        self.assertEqual(calls, [])


class DispatchConfigTests(unittest.TestCase):
    def test_dispatch_mode_defaults_to_local(self):
        original = os.environ.pop("DAILY_CHECKIN_DISPATCH_MODE", None)
        try:
            self.assertEqual(dispatch_mode(), "local")
            os.environ["DAILY_CHECKIN_DISPATCH_MODE"] = "remote"
            self.assertEqual(dispatch_mode(), "remote")
            os.environ["DAILY_CHECKIN_DISPATCH_MODE"] = "typo"
            self.assertEqual(dispatch_mode(), "local")
        finally:
            if original is not None:
                os.environ["DAILY_CHECKIN_DISPATCH_MODE"] = original
            else:
                os.environ.pop("DAILY_CHECKIN_DISPATCH_MODE", None)

    def test_dispatch_db_path_env_and_store_sibling(self):
        original = os.environ.pop("DAILY_CHECKIN_DISPATCH_DB", None)
        try:
            self.assertEqual(
                dispatch_db_path(Path("/tmp/x/system.db")), Path("/tmp/x/dispatch.db")
            )
            os.environ["DAILY_CHECKIN_DISPATCH_DB"] = "/tmp/custom/dispatch.db"
            self.assertEqual(dispatch_db_path(), Path("/tmp/custom/dispatch.db"))
        finally:
            if original is not None:
                os.environ["DAILY_CHECKIN_DISPATCH_DB"] = original
            else:
                os.environ.pop("DAILY_CHECKIN_DISPATCH_DB", None)


class SiteNodeRoutingTests(unittest.TestCase):
    """多节点路由（2026-11-06 Windows 接入）：站点→节点映射与按节点分组派发。"""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = DispatchStore(Path(self.td.name) / "dispatch.db")
        self.store.add_node("mac", "http://127.0.0.1:19777")
        self.store.add_node("win", "http://127.0.0.1:19778")

    def tearDown(self):
        self.td.cleanup()

    def test_set_get_and_unset_site_node(self):
        self.assertIsNone(self.store.node_for_site("hiyo"))
        self.store.set_site_node("hiyo", "win")
        self.assertEqual(self.store.node_for_site("hiyo"), "win")
        # Re-assign overwrites.
        self.store.set_site_node("hiyo", "mac")
        self.assertEqual(self.store.node_for_site("hiyo"), "mac")
        # Empty node removes the pin -> falls back to default.
        self.store.set_site_node("hiyo", "")
        self.assertIsNone(self.store.node_for_site("hiyo"))
        with self.assertRaises(ValueError):
            self.store.set_site_node("", "win")

    def test_resolve_groups_unassigned_sites_to_default_node(self):
        groups = self.store.resolve_nodes_for_sites(["A", "B"])
        self.assertEqual(groups, {"mac": ["A", "B"]})

    def test_resolve_groups_pinned_sites_by_node_preserving_order(self):
        self.store.set_site_node("hiyo", "win")
        self.store.set_site_node("relayfor", "win")
        groups = self.store.resolve_nodes_for_sites(["伏枥", "hiyo", "A", "relayfor", "hiyo"])
        # First-seen group order preserved; per-group site order preserved.
        self.assertEqual(list(groups.keys()), ["mac", "win"])
        self.assertEqual(groups["mac"], ["伏枥", "A"])
        self.assertEqual(groups["win"], ["hiyo", "relayfor", "hiyo"])

    def test_resolve_empty_sites_yields_no_groups(self):
        self.assertEqual(self.store.resolve_nodes_for_sites([]), {})

    def test_cli_assign_site_and_list(self):
        env = dict(os.environ)
        self.db_path = Path(self.td.name) / "cli.db"
        env["DAILY_CHECKIN_DISPATCH_DB"] = str(self.db_path)
        store = DispatchStore(self.db_path)
        store.add_node("win", "http://x:1")
        for args in (["assign-site", "hiyo", "win"], ["assign-site", "relayfor", "win"]):
            proc = subprocess.run(
                [sys.executable, "-m", "checkin_core.dispatch", *args],
                cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        listing = subprocess.run(
            [sys.executable, "-m", "checkin_core.dispatch", "list-sites"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertIn("hiyo\twin", listing.stdout)
        self.assertIn("relayfor\twin", listing.stdout)
        # CLI prints the resolved DB path (游离库 pitfall guard).
        self.assertIn("dispatch db:", listing.stdout)
        unassign = subprocess.run(
            [sys.executable, "-m", "checkin_core.dispatch", "unassign-site", "relayfor"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(unassign.returncode, 0, unassign.stderr)
        self.assertEqual(store.node_for_site("relayfor"), None)


class DeliverHttpTests(unittest.TestCase):
    """DeliveryWorker._deliver_http against a real local HTTP server."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.received: list[dict] = []
        self.status_map: dict[str, int] = {}
        self.td = TemporaryDirectory()
        self.store = DispatchStore(Path(self.td.name) / "dispatch.db")
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                panel.received.append({
                    "path": self.path,
                    "token": self.headers.get("X-Node-Token"),
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                })
                status = panel.status_map.get(self.path, 202)
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.td.cleanup()

    def _node(self, base_url: str | None) -> dict:
        return {"name": "mac", "base_url": base_url, "token": "tok-1"}

    def _job(self) -> dict:
        job_id = self.store.enqueue("site", site_names=["伏枥"], node="mac")
        return self.store.get_job(job_id)

    def _worker(self) -> DeliveryWorker:
        return DeliveryWorker(self.store, start_worker=False)

    def test_deliver_http_accepts_202_with_token_and_payload(self):
        worker = self._worker()
        self.assertTrue(
            worker._deliver_http(self._job(), self._node(f"http://127.0.0.1:{self.server.server_port}"), 5.0)
        )
        self.assertEqual(len(self.received), 1)
        req = self.received[0]
        self.assertEqual(req["path"], "/dispatch")
        self.assertEqual(req["token"], "tok-1")
        self.assertEqual(req["body"]["kind"], "site")
        self.assertEqual(req["body"]["sites"], ["伏枥"])
        self.assertEqual(req["body"]["node"], "mac")

    def test_deliver_http_503_busy_is_retry_not_crash(self):
        self.status_map["/dispatch"] = 503
        worker = self._worker()
        self.assertFalse(worker._deliver_http(self._job(), self._node(f"http://127.0.0.1:{self.server.server_port}"), 5.0))

    def test_deliver_http_unreachable_returns_false(self):
        worker = self._worker()
        # Port is free (nothing listening) → connection refused/timeout → False.
        self.assertFalse(worker._deliver_http(self._job(), self._node("http://127.0.0.1:1"), 1.0))

    def test_deliver_http_empty_base_url_is_false(self):
        worker = self._worker()
        self.assertFalse(worker._deliver_http(self._job(), self._node(""), 1.0))


class DispatchCliTests(unittest.TestCase):
    """`python -m checkin_core.dispatch` operator CLI on the panel host."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.db = Path(self.td.name) / "dispatch.db"
        self.env = dict(os.environ)
        self.env["DAILY_CHECKIN_DISPATCH_DB"] = str(self.db)

    def tearDown(self):
        self.td.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "checkin_core.dispatch", *args],
            cwd=str(ROOT), env=self.env, capture_output=True, text=True, timeout=60,
        )

    def test_add_node_prints_token_once_and_list_hides_it(self):
        proc = self._run("add-node", "mac", "http://10.144.144.100:9777")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        token_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("node token")]
        self.assertEqual(len(token_line), 1)
        token = token_line[0].rsplit(" ", 1)[-1]
        listing = self._run("list-nodes")
        self.assertIn("mac", listing.stdout)
        self.assertIn("http://10.144.144.100:9777", listing.stdout)
        self.assertNotIn(token, listing.stdout)
        # Duplicate registration rejected.
        dup = self._run("add-node", "mac", "http://other:1")
        self.assertNotEqual(dup.returncode, 0)

    def test_jobs_listing_shows_dispatch_rows(self):
        store = DispatchStore(self.db)
        store.add_node("mac", "http://x:9777")
        job_id = store.enqueue("site", site_names=["伏枥"])
        store.mark_delivered(job_id)
        store.finish(job_id, "mac", "done", 0, "[伏枥] ALREADY")
        proc = self._run("jobs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("#1", proc.stdout)
        self.assertIn("done", proc.stdout)
        self.assertIn("伏枥", proc.stdout)

    def test_remove_node(self):
        self._run("add-node", "tmp-node", "http://x:1")
        proc = self._run("remove-node", "tmp-node")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("removed", proc.stdout)


class SyncScriptGuardTests(unittest.TestCase):
    """scripts/sync-db-to-pxed.sh guard paths (no network involved)."""

    def test_missing_db_env_exits_zero_silently(self):
        env = dict(os.environ)
        env.pop("DAILY_CHECKIN_DB", None)
        proc = subprocess.run(
            ["/bin/zsh", str(ROOT / "scripts" / "sync-db-to-pxed.sh")],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("skip", proc.stderr)

    def test_launch_agent_script_sources_env_and_execs_agent(self):
        text = (ROOT / "scripts" / "launch-dispatch-agent.sh").read_text(encoding="utf-8")
        self.assertIn("dispatch agent", text.splitlines()[1])
        self.assertIn("daily-checkin-staging/env.sh", text)
        self.assertIn("-m checkin_core.dispatch_agent", text)
        self.assertIn("exec", text)


if __name__ == "__main__":
    unittest.main()

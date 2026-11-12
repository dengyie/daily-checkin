"""Dispatch control plane: durable job queue + executor-node registry.

The cloud panel (pxed) never runs the check-in runner itself. ``POST /api/run``
enqueues a durable job row here, and the delivery worker pushes queued jobs
directly to the executor node's agent over the EasyTier mesh (e.g.
``http://10.144.144.100:9777/dispatch``). The node executes against its local
Chrome CDP + authoritative DB and reports back via ``/api/dispatch/result``.

Job rows intentionally mirror the legacy ``jobs`` table column names
(id/kind/site_names_json/status/created_at/started_at/finished_at/exit_code/
message) so the web frontend's queue view renders both shapes unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DISPATCH_DB_ENV = "DAILY_CHECKIN_DISPATCH_DB"
DISPATCH_MODE_ENV = "DAILY_CHECKIN_DISPATCH_MODE"
SWEEP_INTERVAL_ENV = "DAILY_CHECKIN_DISPATCH_SWEEP_INTERVAL"
DELIVER_TIMEOUT_ENV = "DAILY_CHECKIN_DISPATCH_DELIVER_TIMEOUT"
MAX_ATTEMPTS_ENV = "DAILY_CHECKIN_DISPATCH_MAX_DELIVERY_ATTEMPTS"
STALE_RUNNING_S_ENV = "DAILY_CHECKIN_DISPATCH_STALE_RUNNING_S"
AUTO_WAKE_ENV = "DAILY_CHECKIN_DISPATCH_AUTO_WAKE"

DEFAULT_NODE = "mac"
# Newly enqueued jobs wake the delivery thread immediately, so this interval
# only governs retry cadence against unreachable nodes. A 5s floor hammered
# a down node with failed 10s pushes; 30s keeps the same 2h retry budget
# (240 attempts) with far less traffic.
DEFAULT_SWEEP_INTERVAL_S = 30.0
DEFAULT_DELIVER_TIMEOUT_S = 10.0
DEFAULT_MAX_DELIVERY_ATTEMPTS = 240  # ~2h at 30s sweeps
DEFAULT_STALE_RUNNING_S = 7200  # 2h: agent accepted but never reported back
# "Online" badge window. MUST comfortably exceed the agent heartbeat cadence
# (300s default): with a 90s window a node is only "online" during a 90s slice
# of every 5-minute cycle, so the panel reads offline almost all the time
# (observed 2026-11-06). 750s ≈ 2.5 missed beats before going dark; also
# absorbs a few minutes of clock skew between the panel host and the agents.
NODE_ONLINE_WINDOW_S = 750

# "delete_site": panel-driven site deletion runs on the executor node that
# owns the authoritative DB + credentials backend (the panel host's system.db
# is a synced replica; deleting locally there would be clobbered back).
# "add_site"/"update_site"/"set_credential"/"delete_credential"/"update_config":
# same principle — every panel write that lands in the authoritative DB or the
# keychain must execute on the owning node and propagate back via the DB sync.
VALID_JOB_KINDS = (
    "site", "all", "manual", "delete_site",
    "add_site", "update_site", "set_credential", "delete_credential",
    "update_config", "update_balance",
)
VALID_FINAL_STATUSES = ("done", "failed")
_MAX_MESSAGE_LEN = 4000


def dispatch_mode() -> str:
    """'remote' when the panel only enqueues (cloud), else legacy 'local'."""
    return "remote" if os.environ.get(DISPATCH_MODE_ENV, "").strip() == "remote" else "local"


def dispatch_db_path(store_path: Path | str | None = None) -> Path:
    env = os.environ.get(DISPATCH_DB_ENV, "").strip()
    if env:
        return Path(env)
    if store_path is not None:
        return Path(store_path).parent / "dispatch.db"
    return Path.home() / ".hermes" / "checkin" / "dispatch.db"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DispatchStore:
    """SQLite-backed durable dispatch queue + node registry.

    Kept separate from system.db because the cloud's system.db is a replica
    overwritten wholesale by the executor's scp sync; queue rows written there
    would be destroyed on every sync (and a node-written WAL could corrupt the
    replaced file). Per-call connections (no persistent fd) so an atomic file
    replacement by the executor never invalidates an open reader.
    """

    def __init__(self, path: Path | str, *, on_enqueue: Callable[[int], None] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.on_enqueue = on_enqueue
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._migrate()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _migrate(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS dispatch_nodes(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    base_url TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_jobs(
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    site_names_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    node TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    last_delivery_at TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_jobs_status
                    ON dispatch_jobs(status, id);
                CREATE INDEX IF NOT EXISTS idx_dispatch_jobs_node
                    ON dispatch_jobs(node, status, id);
                CREATE TABLE IF NOT EXISTS site_nodes(
                    site_name TEXT PRIMARY KEY,
                    node TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ jobs

    def enqueue(self, kind: str, site_names: list[str] | None = None,
                payload: dict | None = None, node: str = DEFAULT_NODE) -> int:
        if kind not in VALID_JOB_KINDS:
            raise ValueError(f"invalid dispatch job kind: {kind}")
        names = [str(n) for n in (site_names or [])]
        with self.connect() as db:
            cur = db.execute(
                """INSERT INTO dispatch_jobs(
                       kind, site_names_json, payload_json, node, status, created_at
                   ) VALUES(?,?,?,?, 'queued', ?)""",
                (
                    kind,
                    json.dumps(names, ensure_ascii=False),
                    json.dumps(payload or {}, ensure_ascii=False),
                    node,
                    _now(),
                ),
            )
            job_id = int(cur.lastrowid)
        if self.on_enqueue is not None:
            try:
                self.on_enqueue(job_id)
            except Exception:
                # A wake-up hint must never make an enqueue look failed.
                logger.exception("dispatch on_enqueue callback failed for job #%s", job_id)
        return job_id

    def _row_to_job(self, row) -> dict:
        job = dict(row)
        try:
            job["sites"] = json.loads(job.get("site_names_json") or "[]")
        except (TypeError, ValueError):
            job["sites"] = []
        try:
            job["payload"] = json.loads(job.get("payload_json") or "{}")
        except (TypeError, ValueError):
            job["payload"] = {}
        return job

    def get_job(self, job_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM dispatch_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            return self._row_to_job(row) if row else None

    def queued_jobs(self, node: str | None = None, *, backoff: bool = False) -> list[dict]:
        """Oldest-first queued jobs for the delivery sweeper.

        backoff=True skips jobs whose next retry is not due yet: a failing
        push (node busy / quiet window / tunnel down) is retried with an
        exponential delay instead of hammering the node every sweep. The
        2026-11-12 quiet-window storm pushed a single manual job 19 times
        (30s sweep × the whole 07:30-08:45 window) before it was accepted.
        """
        with self.connect() as db:
            if node:
                rows = db.execute(
                    "SELECT * FROM dispatch_jobs WHERE status='queued' AND node=? ORDER BY id ASC",
                    (node,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM dispatch_jobs WHERE status='queued' ORDER BY id ASC"
                ).fetchall()
        if not backoff:
            return [self._row_to_job(r) for r in rows]
        now = datetime.now()
        due: list[dict] = []
        for r in rows:
            job = self._row_to_job(r)
            attempts = int(job.get("delivery_attempts") or 0)
            last = job.get("last_delivery_at")
            if attempts and last:
                try:
                    age = (now - datetime.fromisoformat(last)).total_seconds()
                except (TypeError, ValueError):
                    age = None
                # First retry after 30s, then 1m, 2m, 4m ... capped at 10m.
                delay = min(30.0 * (2 ** max(0, attempts - 1)), 600.0)
                if age is not None and age < delay:
                    continue
            due.append(job)
        return due

    def jobs(self, limit: int = 30) -> list[dict]:
        """Newest-first list for the panel; shape-compatible with legacy jobs()."""
        limit = max(1, min(int(limit), 200))
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM dispatch_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def mark_delivered(self, job_id: int) -> bool:
        """CAS queued -> running once the node agent accepted the push.

        Only flips a job that is still queued; delivery_attempts are kept so
        the claim-then-push retry counter accumulates across sweeps instead of
        being zeroed on every (re)claim. A job the node already finished is
        left untouched (returns False).
        """
        with self.connect() as db:
            cur = db.execute(
                """UPDATE dispatch_jobs SET status='running', started_at=?,
                       last_delivery_at=?
                   WHERE id=? AND status='queued'""",
                (_now(), _now(), int(job_id)),
            )
            return cur.rowcount > 0

    def revert_delivery(self, job_id: int) -> bool:
        """Undo the queued->running claim when the push did not reach the node.

        The sweeper claims the job (queued -> running) *before* the HTTP push
        so an immediate node callback always finds the job finishable; if the
        push then fails with nothing accepted we put it back in the queue for
        the next retry. Accumulated delivery_attempts are kept so the retry
        exhaustion counter still advances; a job the node already finished is
        left untouched.
        """
        with self.connect() as db:
            cur = db.execute(
                """UPDATE dispatch_jobs SET status='queued', started_at=NULL
                   WHERE id=? AND status='running'""",
                (int(job_id),),
            )
            return cur.rowcount > 0

    def increment_delivery_attempt(self, job_id: int, max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS) -> int:
        """Count a failed push; auto-fail the job once attempts are exhausted."""
        now = _now()
        with self.connect() as db:
            db.execute(
                """UPDATE dispatch_jobs SET delivery_attempts=delivery_attempts+1,
                       last_delivery_at=? WHERE id=? AND status='queued'""",
                (now, int(job_id)),
            )
            row = db.execute(
                "SELECT delivery_attempts FROM dispatch_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            attempts = int(row["delivery_attempts"]) if row else 0
            if attempts >= max_attempts:
                db.execute(
                    """UPDATE dispatch_jobs SET status='failed', finished_at=?,
                           exit_code=2, message=?
                       WHERE id=? AND status='queued'""",
                    (
                        now,
                        f"delivery_failed: 节点连续 {attempts} 次不可达（离线/休眠或网络中断），已放弃投递",
                        int(job_id),
                    ),
                )
            return attempts

    def finish(self, job_id: int, node: str, status: str, exit_code: int | None = None,
               message: str = "") -> bool:
        """Terminal state write from the executor node.

        Only the node that owns the job may finish it. Both 'queued' and
        'running' are accepted as source states: a fast node can report its
        result before the panel's delivery sweeper marked the push as
        delivered (real race, 2026-11-06 full-loop test) — the node can only
        know the job_id by having received the push, so an owner-reported
        terminal state from 'queued' is a legitimate delivery-accepted report.
        """
        if status not in VALID_FINAL_STATUSES:
            raise ValueError(f"invalid final status: {status}")
        with self.connect() as db:
            cur = db.execute(
                """UPDATE dispatch_jobs SET status=?, finished_at=?, exit_code=?, message=?
                   WHERE id=? AND node=? AND status IN ('queued','running')""",
                (
                    status,
                    _now(),
                    int(exit_code) if exit_code is not None else None,
                    str(message or "")[:_MAX_MESSAGE_LEN],
                    int(job_id),
                    node,
                ),
            )
            return cur.rowcount > 0

    def reap_stale_running(self, max_age_s: int = DEFAULT_STALE_RUNNING_S) -> list[int]:
        """Fail running jobs whose node never reported back in time (crash/sleep)."""
        cutoff = (datetime.now() - timedelta(seconds=int(max_age_s))).strftime("%Y-%m-%dT%H:%M:%S")
        stale: list[int] = []
        with self.connect() as db:
            rows = db.execute(
                """SELECT id, node, started_at FROM dispatch_jobs
                   WHERE status='running' AND started_at IS NOT NULL AND started_at < ?
                   ORDER BY id ASC""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                cur = db.execute(
                    """UPDATE dispatch_jobs SET status='failed', finished_at=?, exit_code=2,
                           message=?
                       WHERE id=? AND status='running'""",
                    (
                        _now(),
                        "node_timeout: 节点受理后未在时限内回传结果（节点离线或崩溃）",
                        int(row["id"]),
                    ),
                )
                if cur.rowcount:
                    stale.append(int(row["id"]))
        return stale

    # ------------------------------------------------------------ site nodes

    def set_site_node(self, site_name: str, node: str) -> None:
        """Pin a site to an executor node; empty node removes the assignment."""
        site_name = str(site_name or "").strip()
        node = str(node or "").strip()
        if not site_name:
            raise ValueError("site name required")
        with self.connect() as db:
            if not node:
                db.execute("DELETE FROM site_nodes WHERE site_name=?", (site_name,))
                return
            db.execute(
                """INSERT INTO site_nodes(site_name, node, updated_at) VALUES(?,?,?)
                   ON CONFLICT(site_name) DO UPDATE SET
                     node=excluded.node, updated_at=excluded.updated_at""",
                (site_name, node, _now()),
            )

    def node_for_site(self, site_name: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT node FROM site_nodes WHERE site_name=?", (str(site_name).strip(),)
            ).fetchone()
            return str(row["node"]) if row else None

    def site_node_map(self) -> dict[str, str]:
        with self.connect() as db:
            rows = db.execute("SELECT site_name, node FROM site_nodes ORDER BY site_name").fetchall()
            return {str(r["site_name"]): str(r["node"]) for r in rows}

    def resolve_nodes_for_sites(self, site_names: list[str]) -> dict[str, list[str]]:
        """Group site names by their target executor node.

        Sites with an explicit site_nodes assignment go to that node; the rest
        fall back to the default node. Order within each group follows the
        input order; groups are returned in first-seen order.
        """
        groups: dict[str, list[str]] = {}
        fallback = self.default_node()
        for raw in site_names:
            name = str(raw).strip()
            if not name:
                continue
            node = self.node_for_site(name) or fallback
            groups.setdefault(node, []).append(name)
        return groups

    # ----------------------------------------------------------------- nodes

    def add_node(self, name: str, base_url: str) -> str:
        """Register an executor node; returns the plaintext token exactly once."""
        name = str(name or "").strip()
        base_url = str(base_url or "").strip().rstrip("/")
        if not name:
            raise ValueError("node name required")
        if not base_url:
            raise ValueError("node base_url required")
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.connect() as db:
            try:
                db.execute(
                    """INSERT INTO dispatch_nodes(name, base_url, token, token_hash, created_at)
                       VALUES(?,?,?,?,?)""",
                    (name, base_url, token, hash_token(token), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"node already exists: {name}") from exc
        return token

    def remove_node(self, name: str) -> bool:
        with self.connect() as db:
            cur = db.execute("DELETE FROM dispatch_nodes WHERE name=?", (str(name).strip(),))
            return cur.rowcount > 0

    def node_record(self, name: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM dispatch_nodes WHERE name=?", (str(name).strip(),)
            ).fetchone()
            return dict(row) if row else None

    def nodes(self) -> list[dict]:
        """Node list for the panel; never exposes token material."""
        now = datetime.now()
        with self.connect() as db:
            rows = db.execute("SELECT * FROM dispatch_nodes ORDER BY id ASC").fetchall()
        out = []
        for row in rows:
            node = dict(row)
            node.pop("token", None)
            node.pop("token_hash", None)
            seen = _parse_ts(node.get("last_seen_at"))
            node["online"] = bool(
                seen and (now - seen) <= timedelta(seconds=NODE_ONLINE_WINDOW_S)
            )
            out.append(node)
        return out

    def default_node(self) -> str:
        """First registered node, else the conventional default name."""
        with self.connect() as db:
            row = db.execute(
                "SELECT name FROM dispatch_nodes ORDER BY id ASC LIMIT 1"
            ).fetchone()
            return str(row["name"]) if row else DEFAULT_NODE

    def registered_nodes(self) -> list[str]:
        """All registered node names in registration order (run-all fan-out)."""
        with self.connect() as db:
            rows = db.execute("SELECT name FROM dispatch_nodes ORDER BY id ASC").fetchall()
            return [str(r["name"]) for r in rows]

    def heartbeat(self, node: str) -> bool:
        with self.connect() as db:
            cur = db.execute(
                "UPDATE dispatch_nodes SET last_seen_at=? WHERE name=?",
                (_now(), str(node).strip()),
            )
            return cur.rowcount > 0

    def verify_node_token(self, token: str) -> str | None:
        """Resolve the node name presenting this token (constant-time compare)."""
        if not token:
            return None
        presented = hash_token(str(token))
        with self.connect() as db:
            rows = db.execute("SELECT name, token_hash FROM dispatch_nodes").fetchall()
        for row in rows:
            if secrets.compare_digest(presented, str(row["token_hash"] or "")):
                return str(row["name"])
        return None


class DeliveryWorker:
    """Background sweeper that pushes queued jobs to node agents.

    Only does work while jobs are actually queued — an idle panel produces zero
    traffic toward the Mac node (no polling pressure). Delivery failures keep
    the job queued for retry; after ``max_attempts`` consecutive failures the
    job is failed with a clear message.
    """

    def __init__(
        self,
        store: DispatchStore,
        *,
        deliver_fn=None,
        interval_s: float | None = None,
        deliver_timeout_s: float | None = None,
        max_attempts: int | None = None,
        stale_max_age_s: int | None = None,
        start_worker: bool = True,
        auto_wake: bool | None = None,
        retry_backoff: bool | None = None,
    ):
        self.store = store
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self.interval_s = float(
            interval_s if interval_s is not None
            else os.environ.get(SWEEP_INTERVAL_ENV, DEFAULT_SWEEP_INTERVAL_S)
        )
        self.auto_wake = (
            _env_flag(AUTO_WAKE_ENV, True) if auto_wake is None else bool(auto_wake)
        )
        self.deliver_timeout_s = float(
            deliver_timeout_s if deliver_timeout_s is not None
            else os.environ.get(DELIVER_TIMEOUT_ENV, DEFAULT_DELIVER_TIMEOUT_S)
        )
        self.max_attempts = int(
            max_attempts if max_attempts is not None
            else os.environ.get(MAX_ATTEMPTS_ENV, DEFAULT_MAX_DELIVERY_ATTEMPTS)
        )
        self.stale_max_age_s = int(
            stale_max_age_s if stale_max_age_s is not None
            else os.environ.get(STALE_RUNNING_S_ENV, DEFAULT_STALE_RUNNING_S)
        )
        # Retry backoff for failing pushes (exponential, capped 10m). Tests
        # pass retry_backoff=False to sweep in a tight loop without wall time.
        self.backoff = (
            retry_backoff if retry_backoff is not None
            else _env_flag("DAILY_CHECKIN_DISPATCH_RETRY_BACKOFF", True)
        )
        self._deliver_fn = deliver_fn or self._deliver_http
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(target=self._work, daemon=True)
            self._thread.start()

    def wake(self, _job_id: int | None = None) -> None:
        """Immediately wake the sweeper to push newly enqueued jobs without polling delay.

        The optional argument lets this be wired directly as
        ``DispatchStore.on_enqueue`` (which passes the new job id).
        """
        self._wake_event.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake_event.set()

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                logging.exception("dispatch delivery sweep failed")
            # Sleep up to interval_s, or wake immediately if wake() was called.
            self._wake_event.wait(self.interval_s)
            self._wake_event.clear()

    def sweep(self) -> int:
        """One delivery pass; returns the number of jobs pushed successfully.

        Claim-then-push ordering: the job is CAS'd queued -> running *before*
        the HTTP push. A node that accepts and finishes a fast job within the
        push window can then always write its terminal result (otherwise the
        callback raced the mark_delivered and got 409 — real bug observed
        2026-11-06). If the push does not get accepted, the claim is reverted
        and a delivery attempt is counted; a node-result that beat us wins.
        """
        self.store.reap_stale_running(self.stale_max_age_s)
        pushed = 0
        for job in self.store.queued_jobs(backoff=self.backoff):
            node = self.store.node_record(job["node"])
            if not node:
                self.store.increment_delivery_attempt(job["id"], self.max_attempts)
                continue
            if not self.store.mark_delivered(job["id"]):
                # Already terminal (node callback won the race) — nothing to do.
                continue
            try:
                ok = bool(self._deliver_fn(job, node, self.deliver_timeout_s))
            except Exception:
                ok = False
            if ok:
                pushed += 1
            else:
                # Push failed: return to the queue unless the node already
                # reported a terminal state in the meantime.
                self.store.revert_delivery(job["id"])
                self.store.increment_delivery_attempt(job["id"], self.max_attempts)
        return pushed

    def _deliver_http(self, job: dict, node: dict, timeout_s: float) -> bool:
        base_url = str(node.get("base_url") or "").rstrip("/")
        if not base_url:
            return False
        payload = {
            "job_id": int(job["id"]),
            "kind": job["kind"],
            "sites": job.get("sites") or [],
            "payload": job.get("payload") or {},
            "node": job["node"],
        }
        req = urllib.request.Request(
            f"{base_url}/dispatch",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Node-Token": str(node.get("token") or ""),
                "User-Agent": "DailyCheckinDispatcher/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status in (200, 202)
        except urllib.error.HTTPError as exc:
            # 409 busy (morning batch running etc.) → keep queued for retry.
            try:
                exc.read()
            except Exception:
                pass
            return False
        except Exception:
            return False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def make_delivery_worker(store: DispatchStore, auto_wake: bool | None = None) -> DeliveryWorker:
    """DeliveryWorker configured from DAILY_CHECKIN_DISPATCH_* env vars.

    Wires ``store.on_enqueue`` to ``worker.wake`` so a newly enqueued job is
    pushed immediately instead of waiting for the sweep interval. Set
    ``DAILY_CHECKIN_DISPATCH_AUTO_WAKE=0`` (or pass ``auto_wake=False``) to
    keep enqueueing fire-and-forget; tests use that to hold the worker idle.
    """
    worker = DeliveryWorker(
        store,
        interval_s=_env_float(SWEEP_INTERVAL_ENV, DEFAULT_SWEEP_INTERVAL_S),
        deliver_timeout_s=_env_float(DELIVER_TIMEOUT_ENV, DEFAULT_DELIVER_TIMEOUT_S),
        max_attempts=_env_int(MAX_ATTEMPTS_ENV, DEFAULT_MAX_DELIVERY_ATTEMPTS),
        stale_max_age_s=_env_int(STALE_RUNNING_S_ENV, DEFAULT_STALE_RUNNING_S),
        auto_wake=auto_wake,
    )
    if worker.auto_wake:
        store.on_enqueue = worker.wake
    return worker


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m checkin_core.dispatch",
        description="Dispatch queue control-plane CLI (run on the panel host).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add-node", help="register an executor node; prints its token once")
    p_add.add_argument("name")
    p_add.add_argument("base_url", help="agent URL reachable from this host, e.g. http://127.0.0.1:19777")
    p_rm = sub.add_parser("remove-node", help="deregister an executor node")
    p_rm.add_argument("name")
    p_assign = sub.add_parser("assign-site", help="pin a site to an executor node (multi-node routing)")
    p_assign.add_argument("site")
    p_assign.add_argument("node")
    p_unassign = sub.add_parser("unassign-site", help="remove a site->node assignment (falls back to default node)")
    p_unassign.add_argument("site")
    sub.add_parser("list-nodes", help="list registered nodes (no secrets)")
    sub.add_parser("list-sites", help="list site->node assignments")
    sub.add_parser("jobs", help="show recent dispatch jobs")
    args = parser.parse_args()

    store = DispatchStore(dispatch_db_path())
    # Pitfall guard (2026-11-06 实际踩过): CLI always prints which DB it is
    # about to touch, so an un-sourced env can never silently write a stray
    # ~/.hermes/checkin/dispatch.db next to the production one.
    print(f"dispatch db: {store.path}")
    if args.cmd == "add-node":
        token = store.add_node(args.name, args.base_url)
        print(f"node registered: {args.name} -> {args.base_url}")
        print(f"node token (store now, shown once): {token}")
    elif args.cmd == "remove-node":
        print("removed" if store.remove_node(args.name) else "not found")
    elif args.cmd == "assign-site":
        store.set_site_node(args.site, args.node)
        print(f"site assigned: {args.site} -> {args.node}")
    elif args.cmd == "unassign-site":
        store.set_site_node(args.site, "")
        print(f"site unassigned: {args.site}")
    elif args.cmd == "list-nodes":
        for node in store.nodes():
            seen = node.get("last_seen_at") or "-"
            state = "online" if node.get("online") else "offline"
            print(f"{node['name']}\t{node['base_url']}\t{seen}\t{state}")
    elif args.cmd == "list-sites":
        for site, node in store.site_node_map().items():
            print(f"{site}\t{node}")
    elif args.cmd == "jobs":
        for job in store.jobs(30):
            names = ",".join(job.get("sites") or [])
            print(
                f"#{job['id']}\t{job['kind']}\t{job['node']}\t{job['status']}"
                f"\texit={job['exit_code']}\t{job['created_at']}\t{names}"
            )


if __name__ == "__main__":
    main()

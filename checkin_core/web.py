"""Local-only control plane and web API for daily-checkin."""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import secrets
import subprocess
import signal
import sys
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from checkin_core.keychain import CredentialStore, make_ref, credential_store_from_env
from checkin_core.notify import _send_telegram_raw
from checkin_core.store import SystemStore, store_from_env, obsidian_daily_task_path, read_only_tasks
from checkin_core.dispatch import (
    DeliveryWorker,
    DispatchStore,
    convention_node_for_site,
    dispatch_db_path,
    dispatch_mode,
    make_delivery_worker,
)

# Unified multi-node panel (2026-11-06): additional executor nodes' replica
# DBs to merge into /api/state's task list, as "node=path" pairs separated by
# commas (e.g. "win=/data/daily-checkin/data/system-win.db"). The default
# node's tasks come from the primary store; every row gets a "node" field —
# the execution channel is just an attribute of the task, the frontend stays
# one unified list.
NODE_REPLICAS_ENV = "DAILY_CHECKIN_NODE_REPLICAS"


def node_replicas() -> dict[str, Path]:
    raw = os.environ.get(NODE_REPLICAS_ENV, "")
    out: dict[str, Path] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        node, _, path = part.partition("=")
        node = node.strip()
        path = path.strip()
        if node and path:
            out[node] = Path(path)
    return out

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILENAME = "web.token"
CSRF_FILENAME = "csrf.token"


def load_or_create_csrf(csrf_path: Path) -> str:
    """Read durable CSRF token across web process restarts, creating if absent."""
    try:
        token = csrf_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    csrf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(csrf_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
        finally:
            try:
                os.chmod(csrf_path, 0o600)
            except OSError:
                pass
    except FileExistsError:
        return csrf_path.read_text(encoding="utf-8").strip()
    return token


def load_or_create_token(token_path: Path) -> tuple[str, bool]:
    """Read the bearer token, creating it with 0600 permissions if absent.

    Returns (token, created). 'created' is True only when a new file was just
    created — callers should print the token to stderr exactly once on creation
    so the operator can copy it into the web console.
    """
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token, False
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(token_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
        finally:
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                pass
    except FileExistsError:
        # Another process raced us to create it; respect their value.
        return token_path.read_text(encoding="utf-8").strip(), False
    return token, True


# Optional Password Path: when WEB_PASSWORD is set, clients may authenticate
# with `X-DailyCheckin-Password: <password>` in place of the bearer token.
# Password lives in the environment (never in git / logs / UI).
WEB_PASSWORD_ENV = "DAILY_CHECKIN_WEB_PASSWORD"


def web_password() -> str | None:
    val = os.environ.get(WEB_PASSWORD_ENV)
    if val is not None:
        val = val.strip()
    return val or None


ALLOWED_CRED_KINDS = {"token", "cookie", "password"}


def _constant_eq(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return secrets.compare_digest(a, b)


# CORS allow-list for the separate static frontend (loopback :8766 plus the
# hosts-file local domains). Never wildcard; untrusted origins are rejected.
_DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:8766,http://localhost:8766,"
    "http://check.example.com:8766,https://check.example.com,"
    "http://check.mangoqwq.com:8766,http://check-api.mangoqwq.com:8766,"
    "https://check.mangoqwq.com,https://check-api.mangoqwq.com,"
    "http://check.mangoqwq.com,http://check-api.mangoqwq.com"
)
CORS_ORIGINS_ENV = "DAILY_CHECKIN_WEB_ORIGINS"
CORS_ALLOW_METHODS = "GET, POST, OPTIONS"
CORS_ALLOW_HEADERS = "Authorization, Content-Type, X-CSRF-Token, X-DailyCheckin-Password"


def allowed_cors_origins() -> set[str]:
    raw = os.environ.get(CORS_ORIGINS_ENV, _DEFAULT_ALLOWED_ORIGINS)
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


# Site/task name sanitation: name is the natural key used to match catalog
# sites and to write the Obsidian projection, so it must stay a clean, bounded
# scalar. A control character or an unreasonable length would silently pollute
# the authoritative sites/daily_tasks tables via the write endpoints.
_MAX_SITE_NAME_LEN = 120


def _clean_site_name(raw: object | None) -> str:
    """Strip and length-check a site/task name. Returns cleaned name or raises
    ValueError when the value is empty or unusable."""
    name = str(raw or "").strip()
    if not name:
        raise ValueError("name required")
    if len(name) > _MAX_SITE_NAME_LEN:
        raise ValueError(f"name too long (>{_MAX_SITE_NAME_LEN} chars)")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("name contains control characters")
    return name


def include_cors_headers(handler) -> None:
    """Emit Access-Control-* headers for a request whose origin is allow-listed."""
    origin = handler.headers.get("Origin", "").strip().rstrip("/")
    if origin in allowed_cors_origins():
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")


def _dedupe_tasks_by_owner(
    rows: list[dict], pinned: dict[str, str],
) -> list[dict]:
    """Collapse multi-node task rows so each site name appears exactly once.

    A site has one owner (the ``site_nodes`` pin, falling back to convention_node_for_site).
    The pinned node's row wins when present; otherwise the last-seen row is kept,
    so a pinned-but-not-yet-synced site never vanishes (replica rows are appended
    after the default node's rows, so "last seen" prefers the replica).

    When a site name appears across multiple executor nodes, cross-node collision
    is detected and tagged with ``cross_node_conflict=True`` and ``competing_nodes``
    so that multi-account masking is observable in the dashboard / API.
    """
    node_names_by_site: dict[str, set[str]] = {}
    for task in rows:
        name = str(task.get("name") or "")
        node = str(task.get("node") or "")
        if name and node:
            node_names_by_site.setdefault(name, set()).add(node)

    chosen: dict[str, dict] = {}
    locked: set[str] = set()
    for task in rows:
        name = str(task.get("name") or "")
        if not name:
            continue
        owner = pinned.get(name) or convention_node_for_site(name)
        if owner and str(task.get("node")) != owner:
            if name not in locked:
                chosen[name] = task  # fallback until the owner row arrives
            continue
        chosen[name] = task
        if owner:
            locked.add(name)

    # Tag conflicts if a site had multiple competing nodes
    tagged: list[dict] = []
    for name, task in chosen.items():
        competing = node_names_by_site.get(name) or set()
        if len(competing) > 1:
            # Copy before tagging: `chosen` holds the caller's own row dicts,
            # and silently adding keys to them is a hidden side effect on the
            # /api/state payload built earlier in the same request.
            task = dict(task)
            task["cross_node_conflict"] = True
            task["competing_nodes"] = sorted(competing)
        tagged.append(task)

    return tagged


class JobExecutor:
    def __init__(
        self,
        store: SystemStore,
        python: str | None = None,
        *,
        start_worker: bool = True,
    ):
        self.store = store
        self.python = python or sys.executable
        self._queue: queue.Queue[tuple[int, list[str]]] = queue.Queue()
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(target=self._work, daemon=True)
            self._worker.start()

    def submit(self, site_names: list[str] | None = None) -> int:
        names = [str(x).strip() for x in (site_names or []) if str(x).strip()]
        job_id = self.store.create_job("site" if names else "all", names)
        self._queue.put((job_id, names))
        return job_id

    def _work(self) -> None:
        while True:
            job_id, names = self._queue.get()
            try:
                self._run(job_id, names)
            finally:
                self._queue.task_done()

    def recover_stale_jobs(self) -> int:
        """Re-enqueue durable queued jobs after a web restart.

        The in-memory FIFO is lost whenever the API process restarts, but the
        jobs table persists. Without this, any job accepted before the crash
        sits in ``queued`` forever and is never executed (the old worker thread
        is gone). Recovery is idempotent: only ``queued`` (never started) rows
        are requeued; ``running``/``done``/``failed`` rows are left untouched so
        an interrupted subprocess cannot be double-triggered.
        """
        requeued = 0
        for job in self.store.jobs(limit=200):
            if job.get("status") != "queued":
                continue
            try:
                raw_list = json.loads(job.get("site_names_json") or "[]")
                names = []
                for x in raw_list:
                    try:
                        clean_n = _clean_site_name(x)
                        if "," not in clean_n:
                            names.append(clean_n)
                    except ValueError:
                        pass
            except (TypeError, ValueError):
                names = []
            self._queue.put((int(job["id"]), names))
            requeued += 1
        if requeued:
            print(f"web: requeued {requeued} stale job(s) after restart", flush=True)
        return requeued

    def _terminate_process_tree(self, process: subprocess.Popen, timeout: float = 10.0) -> None:
        """Terminate a runner process and any spawned child processes cross-platform."""
        poll_fn = getattr(process, "poll", None)
        if callable(poll_fn) and poll_fn() is not None:
            return
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                pass
            try:
                process.wait(timeout=timeout)
            except Exception:
                pass
            return

        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=timeout)
            except Exception:
                pass
        except ProcessLookupError:
            pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _run(self, job_id: int, names: list[str]) -> None:
        if not self.store.claim_job(job_id):
            # Job was already picked up or completed by another worker/process
            return
        cmd = [self.python, str(ROOT / "stealth_checkin_runner.py"), "--source", "web"]
        if names:
            cmd.extend(["--only", ",".join(names)])
        env = dict(os.environ)
        env.setdefault("DAILY_CHECKIN_DB", str(self.store.path))
        try:
            process = subprocess.Popen(
                cmd, cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=7200)
            except subprocess.TimeoutExpired:
                # Own and terminate the whole runner process group; do not
                # leave a side-effecting browser runner acting after the job
                # has been marked failed.
                self._terminate_process_tree(process)
                self.store.update_job(
                    job_id, "failed", finished_at=self.store.now(), exit_code=2,
                    message="runner_timeout: process group terminated and reaped",
                )
                return
            result = subprocess.CompletedProcess(
                cmd, process.returncode, stdout=stdout or "", stderr=stderr or ""
            )
            message = (result.stdout + "\n" + result.stderr)[-4000:]
            self.store.update_job(
                job_id, "done" if result.returncode == 0 else "failed",
                finished_at=self.store.now(), exit_code=result.returncode, message=message,
            )
        except Exception as exc:
            logging.exception("runner launch failed (job %s)", job_id)
            self.store.update_job(
                job_id, "failed", finished_at=self.store.now(), exit_code=2,
                message=f"runner failed: {type(exc).__name__}",
            )



class CheckinWebApp:
    def __init__(
        self,
        store: SystemStore | None = None,
        credentials=None,
        require_auth: bool = True,
    ):
        self.require_auth = require_auth
        self.store = store or store_from_env()
        self.token_path = self.store.path.parent / TOKEN_FILENAME
        self.auth_token, self.token_created = load_or_create_token(self.token_path)
        self.credentials = credentials or credential_store_from_env()
        self.csrf_path = self.store.path.parent / CSRF_FILENAME
        self.csrf = load_or_create_csrf(self.csrf_path)
        # Dispatch mode: the cloud panel ("remote") only enqueues durable jobs
        # and pushes them to executor-node agents; it never spawns a local
        # runner (there is no CDP browser on the panel host). Local mode keeps
        # the legacy in-process executor untouched.
        self.dispatch_mode = dispatch_mode()
        self.dispatch: DispatchStore | None = None
        self.delivery: DeliveryWorker | None = None
        if self.dispatch_mode == "remote":
            self.dispatch = DispatchStore(dispatch_db_path(self.store.path))
            self.executor = JobExecutor(self.store, start_worker=False)
            self.delivery = make_delivery_worker(self.dispatch)
        else:
            self.executor = JobExecutor(self.store)
            # Durable jobs survived a possible earlier web-process crash; requeue
            # any that were still pending before the API went away.
            self.executor.recover_stale_jobs()

    # Paths whose writes are only meaningful on the executor node that owns
    # the authoritative DB (the panel host's system.db is a synced replica
    # that would be clobbered by the next push). The panel rejects them with
    # a clear operator-facing message instead of silently losing the data.
    REMOTE_BLOCKED_WRITE_PATHS = frozenset({
        "/api/sync/obsidian",
        # Everything below was removed from this list and dispatches to the
        # owning executor node instead (authoritative DB + keychain live
        # there): add_site (/api/tasks), update_site (/api/sites/update),
        # set_credential/delete_credential (/api/credentials*),
        # update_config (/api/config/update), delete_site (/api/sites/delete).
    })
    REMOTE_BLOCKED_MESSAGE = "看板模式下该操作需在执行节点本地完成（权威数据在 Mac 节点）"

    def _wake_delivery(self) -> None:
        """Nudge the dispatch sweeper so a new job/result is handled at once."""
        if self.delivery is not None and self.delivery.auto_wake:
            self.delivery.wake()

    def _dispatch_write(self, kind: str, payload: dict, node_hint: str | None = None):
        """Enqueue an authoritative-write job for the owning executor node and
        return the 202 payload the frontend's dispatch-tracking toast expects.
        Every panel write that lands in the authoritative DB / keychain goes
        through this: the panel replica must never be written directly."""
        node = node_hint
        site = str(payload.get("site") or "").strip()
        if not node and site:
            node = self.dispatch.node_for_site(site)
        if not node:
            node = self.dispatch.default_node()
        job_id = self.dispatch.enqueue(kind, payload=payload, node=node)
        self._wake_delivery()
        return {
            "ok": True,
            "queued": True,
            "job_id": job_id,
            "node": node,
            "mode": "remote",
        }

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DailyCheckin/1"

            def log_message(self, format: str, *args) -> None:
                try:
                    print(f"web {self.address_string()} {format % args}", flush=True)
                except Exception:
                    pass

            def allowed_host(self) -> bool:
                host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
                return host in {
                    "127.0.0.1",
                    "localhost",
                    "::1",
                    "check.example.com",
                    "check-api.example.com",
                    "check.mangoqwq.com",
                    "check-api.mangoqwq.com",
                }

            def send_json(self, data, status=HTTPStatus.OK):
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                include_cors_headers(self)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def read_json(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    raise ValueError("request too large")
                ct = self.headers.get("Content-Type", "")
                if ct and not ct.split(";")[0].strip().lower() == "application/json":
                    raise ValueError("Content-Type must be application/json")
                return json.loads(self.rfile.read(length) or b"{}")

            def check_csrf(self) -> bool:
                return secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), app.csrf)

            def check_auth(self) -> bool:
                if not getattr(app, "require_auth", True) or os.environ.get("DAILY_CHECKIN_NO_AUTH") == "1":
                    return True
                # Password gate (X-DailyCheckin-Password) takes precedence.
                pwd = web_password()
                req_pwd = (self.headers.get("X-DailyCheckin-Password") or "").strip()
                if pwd is not None and _constant_eq(req_pwd, pwd):
                    return True
                if pwd is not None:
                    return False
                # Legacy/token fallback: Authorization: Bearer <web.token>
                header = self.headers.get("Authorization", "")
                scheme, _, token = header.partition(" ")
                if scheme.lower() != "bearer" or not token:
                    return False
                return _constant_eq(token.strip(), app.auth_token)

            def check_node_token(self) -> str | None:
                """Authenticate an executor-node callback via X-Node-Token.

                Returns the authenticated node name, or None. Machine-to-machine
                path: intentionally bypasses web auth/CSRF but still passes the
                allowed_host gate checked by every do_* entrypoint.
                """
                if not app.dispatch:
                    return None
                token = (self.headers.get("X-Node-Token") or "").strip()
                return app.dispatch.verify_node_token(token)

            def handle_dispatch_api(self, path: str) -> None:
                node = self.check_node_token()
                if not node:
                    self.send_json({"error": "auth"}, HTTPStatus.UNAUTHORIZED)
                    return
                if path == "/api/dispatch/heartbeat":
                    app.dispatch.heartbeat(node)
                    self.send_json({"ok": True, "node": node})
                    return
                if path == "/api/dispatch/result":
                    try:
                        body = self.read_json()
                    except ValueError as exc:
                        self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                    status = str(body.get("status") or "").strip()
                    if status not in ("done", "failed"):
                        self.send_json(
                            {"error": "status must be 'done' or 'failed'"},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                    try:
                        job_id = int(body.get("job_id"))
                    except (TypeError, ValueError):
                        self.send_json({"error": "job_id required"}, HTTPStatus.BAD_REQUEST)
                        return
                    exit_code = body.get("exit_code")
                    try:
                        exit_code = int(exit_code) if exit_code is not None else None
                    except (TypeError, ValueError):
                        exit_code = None
                    message = str(body.get("message") or "")[:4000]
                    if not app.dispatch.finish(job_id, node, status, exit_code, message):
                        # Wrong owner / unknown job / non-running state.
                        self.send_json(
                            {"error": "job not finishable by this node"},
                            HTTPStatus.CONFLICT,
                        )
                        return
                    if status == "done":
                        self._echo_remote_write(job_id, node)
                    app._wake_delivery()
                    self.send_json({"ok": True, "job_id": job_id, "status": status})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def _echo_remote_write(self, job_id: int, node: str) -> None:
                """Replay a fast config write locally the moment a node reports done.

                The panel replica would otherwise stay stale until the node's
                scp DB sync lands (~2-10s), which is the whole wait behind a
                manual completion click. Replaying the same write here is
                idempotent — the later scp carries identical state.
                """
                try:
                    job = app.dispatch.get_job(job_id)
                except Exception:
                    job = None
                if not job:
                    return
                kind = job.get("kind")
                payload = job.get("payload") or {}
                message = str(job.get("message") or "")
                try:
                    if kind == "manual":
                        day = str(payload.get("day") or "").strip()
                        site = str(payload.get("site") or "").strip()
                        if not (re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day) and site):
                            return
                        # complete_task_manually is a TOGGLE; blind replay could
                        # land the opposite of what the node actually did when
                        # the replica lags. The agent reports its resolved
                        # action in the result message JSON — replay that.
                        action = None
                        try:
                            action = str(json.loads(message).get("action") or "")
                        except (TypeError, ValueError):
                            pass
                        if action == "revert":
                            app.store.revert_task_manually(day, site, project_obsidian=False)
                        else:
                            # No parseable message → assume the common path
                            # (mark_done); a stale-replica revert would then be
                            # corrected by the scp sync seconds later.
                            app.store.complete_task_manually(day, site, project_obsidian=False)
                        self._checkpoint_replica_wal()
                    elif kind == "update_site":
                        site = str(payload.get("site") or "").strip()
                        if not site:
                            return
                        updates = {
                            k: payload[k]
                            for k in ("checkin_mode", "tags", "enabled", "url", "provider")
                            if k in payload
                        }
                        if updates:
                            app.store.update_site_config(site, **updates)
                            self._checkpoint_replica_wal()
                    elif kind == "delete_site":
                        site = str(payload.get("site") or "").strip()
                        if site:
                            app.store.delete_site(
                                site,
                                ignore_missing=bool(payload.get("ignore_missing", False)),
                            )
                            self._checkpoint_replica_wal()
                except Exception:
                    # Echo is an optimization; the scp sync is the source of truth.
                    logging.exception(
                        "echo replay of job #%s from node %s failed", job_id, node
                    )

            def _checkpoint_replica_wal(self) -> None:
                """Flush the replica WAL after an echo write.

                The per-node sync dance (sync-db-to-pxed.sh) atomically mv's a
                whole new system.db over the replica while deliberately leaving
                any system.db-wal in place — safe only while the panel never
                writes. Echo writes break that premise: an un-checkpointed WAL
                would replay stale echo-era pages over the freshly synced DB.
                """
                try:
                    app.store.checkpoint_wal()
                except Exception:
                    logging.exception("replica WAL checkpoint after echo failed")

            def do_HEAD(self):
                if not self.allowed_host():
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                path = urlparse(self.path).path
                if path.startswith("/api/") and not self.check_auth():
                    self.send_response(HTTPStatus.UNAUTHORIZED)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    include_cors_headers(self)
                    self.end_headers()
                    return
                if path == "/api/state":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    include_cors_headers(self)
                    self.end_headers()
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_GET(self):
                if not self.allowed_host():
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                path = urlparse(self.path).path
                if path.startswith("/api/") and not self.check_auth():
                    self.send_json({"error": "auth"}, HTTPStatus.UNAUTHORIZED)
                    return
                if path == "/api/state":
                    day = datetime.now().strftime("%Y-%m-%d")
                    if app.dispatch_mode == "remote":
                        # Panel host: read-only replica + dispatch queue view.
                        data = app.store.snapshot(day, materialize=False)
                        data["jobs"] = app.dispatch.jobs(30)
                        default_node = app.dispatch.default_node()
                        # Unified multi-node task list: default node's rows
                        # from the primary replica, other nodes' rows merged
                        # from their own read-only replicas; each row carries
                        # its execution channel in "node".
                        for t in data["tasks"]:
                            t["node"] = default_node
                        rows = list(data["tasks"])
                        for node, replica_path in node_replicas().items():
                            for t in read_only_tasks(replica_path, day):
                                t["node"] = node
                                rows.append(t)
                        # Collapse duplicated names: a site has exactly ONE
                        # owner (the site_nodes pin), otherwise the same
                        # enabled site renders twice (hiyo/relayfor were
                        # enabled on both mac and win on 2026-11-09).
                        data["tasks"] = _dedupe_tasks_by_owner(
                            rows, app.dispatch.site_node_map()
                        )
                        data["dispatch"] = {
                            "mode": "remote",
                            "default_node": default_node,
                            "nodes": app.dispatch.nodes(),
                        }
                    else:
                        data = app.store.snapshot(day)
                    data["csrf"] = app.csrf
                    self.send_json(data)
                    return
                if path == "/api/config":
                    configs = app.store.get_all_configs()
                    self.send_json({"ok": True, "configs": configs})
                    return
                if path == "/api/ai/latest":
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    day = (params.get("day") or [None])[0] or datetime.now().strftime("%Y-%m-%d")
                    inspection = app.store.get_latest_ai_inspection(day)
                    self.send_json({"ok": True, "inspection": inspection})
                    return
                if path == "/api/sites/balance/history":
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    site_name_raw = (params.get("site") or [None])[0]
                    if not site_name_raw:
                        self.send_json({"error": "site parameter required"}, HTTPStatus.BAD_REQUEST)
                        return
                    site_name = unquote(site_name_raw).strip()
                    limit_raw = (params.get("limit") or [None])[0]
                    limit = 30
                    if limit_raw:
                        try:
                            limit = max(1, min(int(limit_raw), 100))
                        except (ValueError, TypeError):
                            limit = 30
                    history = app.store.get_site_balance_history(site_name, limit=limit)
                    self.send_json({"ok": True, "site": site_name, "history": history})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_OPTIONS(self):
                if not self.allowed_host():
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                origin = self.headers.get("Origin", "").strip().rstrip("/")
                if origin not in allowed_cors_origins():
                    self.send_json({"error": "origin"}, HTTPStatus.FORBIDDEN)
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                include_cors_headers(self)
                self.send_header("Access-Control-Allow-Methods", CORS_ALLOW_METHODS)
                self.send_header("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS)
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Access-Control-Request-Method, Access-Control-Request-Headers")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                if not self.allowed_host():
                    print(f"web [rejected:host] {self.path} host={self.headers.get('Host')}", flush=True)
                    self.send_json({"error": "host"}, HTTPStatus.FORBIDDEN)
                    return
                path = urlparse(self.path).path
                # Executor-node callbacks (result/heartbeat) authenticate with
                # the per-node token, not the web password/CSRF pair.
                if path.startswith("/api/dispatch/"):
                    self.handle_dispatch_api(path)
                    return
                if not self.check_auth():
                    print(f"web [rejected:auth] {self.path}", flush=True)
                    self.send_json({"error": "auth"}, HTTPStatus.UNAUTHORIZED)
                    return
                if not self.check_csrf():
                    print(f"web [rejected:csrf] {self.path}", flush=True)
                    self.send_json({"error": "csrf"}, HTTPStatus.FORBIDDEN)
                    return
                try:
                    body = self.read_json()
                    if app.dispatch_mode == "remote" and path in app.REMOTE_BLOCKED_WRITE_PATHS:
                        self.send_json({"error": app.REMOTE_BLOCKED_MESSAGE}, HTTPStatus.BAD_REQUEST)
                        return
                    if path == "/api/run":
                        raw_names = body.get("sites") or []
                        if not isinstance(raw_names, list):
                            raise ValueError("sites must be a list")
                        names = [_clean_site_name(n) for n in raw_names]
                        names = [n for n in names if n]
                        for n in names:
                            if "," in n:
                                raise ValueError("site name cannot contain commas")
                        if app.dispatch_mode == "remote":
                            if not names:
                                # Full-batch run fans out to EVERY registered
                                # node: each agent executes its own enabled
                                # catalog against its own authoritative DB in
                                # parallel — one click, all platforms together.
                                jobs = []
                                for node in app.dispatch.registered_nodes():
                                    job_id = app.dispatch.enqueue("all", node=node)
                                    jobs.append({"job_id": job_id, "node": node, "sites": []})
                                payload = {"mode": "remote", "jobs": jobs}
                                if len(jobs) == 1:
                                    payload["job_id"] = jobs[0]["job_id"]
                                    payload["node"] = jobs[0]["node"]
                                self.send_json(payload, HTTPStatus.ACCEPTED)
                                return
                            # Multi-node routing: group the requested sites by
                            # their pinned executor node (site_nodes map, else
                            # the default node) and enqueue one job per node,
                            # so a mixed Mac/Windows selection fans out into
                            # per-node jobs that run against each node's own
                            # authoritative DB.
                            groups = app.dispatch.resolve_nodes_for_sites(names)
                            jobs = []
                            for node, node_names in groups.items():
                                job_id = app.dispatch.enqueue(
                                    "site", site_names=node_names, node=node
                                )
                                jobs.append({"job_id": job_id, "node": node, "sites": node_names})
                            payload = {"mode": "remote", "jobs": jobs}
                            if len(jobs) == 1:
                                # Back-compat single-job shape for the frontend.
                                payload["job_id"] = jobs[0]["job_id"]
                                payload["node"] = jobs[0]["node"]
                            app._wake_delivery()
                            self.send_json(payload, HTTPStatus.ACCEPTED)
                            return
                        self.send_json({"job_id": app.executor.submit(names)}, HTTPStatus.ACCEPTED)
                        return
                    if path == "/api/tasks":
                        name = _clean_site_name(body.get("name"))
                        url = str(body.get("url") or "").strip()
                        provider = str(body.get("provider") or "legacy_browser").strip()
                        checkin_mode = str(body.get("checkin_mode") or "auto").strip().lower()
                        tags = str(body.get("tags") or "").strip()
                        day = str(body.get("day") or datetime.now().strftime("%Y-%m-%d")).strip()
                        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
                            raise ValueError(f"invalid day format (expected YYYY-MM-DD): {day}")
                        if not name:
                            raise ValueError("name required")
                        if name.startswith("雅思"):
                            raise ValueError("name reserved: 雅思")
                        if checkin_mode not in ("auto", "manual"):
                            raise ValueError("checkin_mode must be 'auto' or 'manual'")
                        if app.dispatch_mode == "remote":
                            # Site catalog writes belong to the executor node
                            # owning the authoritative DB (same contract as
                            # delete_site); the replica stays read-only.
                            self.send_json(
                                app._dispatch_write(
                                    "add_site",
                                    {
                                        "site": name, "url": url, "provider": provider,
                                        "checkin_mode": checkin_mode, "tags": tags, "day": day,
                                    },
                                    node_hint=app.dispatch.node_for_site(name),
                                ),
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        app.store.upsert_task(
                            day, name, url, provider=provider,
                            checkin_mode=checkin_mode, tags=tags,
                            status="pending", source="system",
                        )
                        self.send_json({"ok": True})
                        return
                    if path == "/api/tasks/complete":
                        name = _clean_site_name(body.get("name"))
                        day = str(body.get("day") or datetime.now().strftime("%Y-%m-%d")).strip()
                        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
                            raise ValueError(f"invalid day format (expected YYYY-MM-DD): {day}")
                        if app.dispatch_mode == "remote":
                            # Manual completion must land on the executor whose
                            # authoritative DB owns the task (and its Obsidian
                            # projection), so it is dispatched like a check-in
                            # run and routed via the site_nodes map.
                            node = app.dispatch.node_for_site(name) or app.dispatch.default_node()
                            job_id = app.dispatch.enqueue(
                                "manual",
                                payload={"site": name, "day": day},
                                node=node,
                            )
                            self.send_json(
                                {
                                    "ok": True,
                                    "queued": True,
                                    "job_id": job_id,
                                    "node": node,
                                    "mode": "remote",
                                },
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        res = app.store.complete_task_manually(day, name)
                        self.send_json(res)
                        return
                    if path == "/api/sync/obsidian":
                        day = str(body.get("day") or datetime.now().strftime("%Y-%m-%d")).strip()
                        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
                            raise ValueError(f"invalid day format (expected YYYY-MM-DD): {day}")
                        count = 0
                        vault_path = obsidian_daily_task_path(day)
                        if vault_path.is_file():
                            text = vault_path.read_text(encoding="utf-8")
                            from stealth_checkin_runner import parse_all_daily_tasks
                            rows = parse_all_daily_tasks(text)
                            count = app.store.import_obsidian_tasks(day, rows, str(vault_path))
                        app.store.materialize_day(day)
                        self.send_json({"ok": True, "count": count, "day": day})
                        return
                    if path == "/api/sites/update":
                        name = _clean_site_name(body.get("name"))
                        if not name:
                            raise ValueError("name required")
                        updates = {}
                        if "checkin_mode" in body:
                            mode = str(body["checkin_mode"]).strip().lower()
                            if mode not in ("auto", "manual"):
                                raise ValueError("checkin_mode must be 'auto' or 'manual'")
                            updates["checkin_mode"] = mode
                        if "tags" in body:
                            updates["tags"] = str(body["tags"]).strip()
                        if "enabled" in body:
                            updates["enabled"] = 1 if body["enabled"] else 0
                        if "url" in body:
                            updates["url"] = str(body["url"]).strip()
                        if "provider" in body:
                            updates["provider"] = str(body["provider"]).strip()
                        if app.dispatch_mode == "remote":
                            if not updates:
                                raise ValueError("no valid fields to update")
                            self.send_json(
                                app._dispatch_write(
                                    "update_site", {"site": name, **updates},
                                    node_hint=app.dispatch.node_for_site(name),
                                ),
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        res = app.store.update_site_config(name, **updates)
                        self.send_json({"ok": True, "site": res})
                        return
                    if path == "/api/sites/balance":
                        name = _clean_site_name(body.get("name"))
                        if not name:
                            raise ValueError("name required")
                        amount_raw = body.get("amount")
                        amount = None
                        if amount_raw is not None and str(amount_raw).strip() != "":
                            try:
                                amount = float(amount_raw)
                                if not math.isfinite(amount):
                                    raise ValueError("amount must be a finite number")
                            except (ValueError, TypeError):
                                raise ValueError("amount must be a valid finite number")
                        currency = str(body.get("currency") or "USD").strip().upper()
                        raw_text = str(body.get("raw_text") or "").strip()
                        day = str(body.get("day") or "").strip()
                        if day and not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", day):
                            raise ValueError(f"invalid day format (expected YYYY-MM-DD): {day}")
                        if app.dispatch_mode == "remote":
                            # Balance ledger lives in the authoritative DB.
                            self.send_json(
                                app._dispatch_write(
                                    "update_balance",
                                    {
                                        "site": name, "amount": amount_raw,
                                        "currency": currency, "raw_text": raw_text, "day": day,
                                    },
                                    node_hint=app.dispatch.node_for_site(name),
                                ),
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        res = app.store.update_site_balance_manual(
                            name,
                            amount=amount,
                            currency=currency,
                            raw_text=raw_text,
                            day=day,
                            source="manual",
                        )
                        self.send_json({"ok": True, "name": name, "balance": res})
                        return
                    if path == "/api/sites/delete":
                        name = _clean_site_name(body.get("name"))
                        if not name:
                            raise ValueError("name required")
                        ignore_missing = bool(body.get("ignore_missing", False))
                        if app.dispatch_mode == "remote":
                            # Deletion must run where the authoritative DB and
                            # the credentials backend (keychain) live, then the
                            # node's DB sync erases the row from the panel
                            # replica — same contract as manual completion.
                            node = app.dispatch.node_for_site(name) or app.dispatch.default_node()
                            job_id = app.dispatch.enqueue(
                                "delete_site",
                                payload={"site": name, "ignore_missing": ignore_missing},
                                node=node,
                            )
                            self.send_json(
                                {
                                    "ok": True,
                                    "queued": True,
                                    "job_id": job_id,
                                    "node": node,
                                    "mode": "remote",
                                },
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        res = app.store.delete_site(name, ignore_missing=ignore_missing)
                        cred_refs = res.get("credential_refs") or ([res.get("credential_ref")] if res.get("credential_ref") else [])
                        for c_ref in cred_refs:
                            if c_ref:
                                try:
                                    app.credentials.delete(c_ref)
                                except Exception as exc:
                                    logging.warning("failed to delete keychain ref %s for site %s: %s", c_ref, name, exc)
                        self.send_json({"ok": True, "name": name, "deleted_tasks": res["deleted_tasks"]})
                        return
                    if path == "/api/credentials":
                        site = _clean_site_name(body.get("site"))
                        kind = str(body.get("kind") or "token").strip().lower()
                        secret = str(body.get("secret") or "")
                        label = str(body.get("label") or "").strip()
                        if not site or not secret:
                            raise ValueError("site and secret required")
                        if kind not in ALLOWED_CRED_KINDS:
                            raise ValueError(f"invalid credential kind: {kind}")
                        if app.dispatch_mode == "remote":
                            # Secrets live in the executor node's keychain.
                            self.send_json(
                                app._dispatch_write(
                                    "set_credential",
                                    {"site": site, "kind": kind, "secret": secret, "label": label},
                                    node_hint=app.dispatch.node_for_site(site),
                                ),
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        ref = make_ref(site, kind)
                        app.credentials.put(ref, secret)
                        app.store.set_credential_ref(site, ref, kind, label)
                        self.send_json({"ok": True, "ref": ref})
                        return
                    if path == "/api/credentials/delete":
                        ref = str(body.get("ref") or "").strip()
                        if not ref:
                            raise ValueError("ref required")
                        if app.dispatch_mode == "remote":
                            self.send_json(
                                app._dispatch_write("delete_credential", {"ref": ref}),
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        app.credentials.delete(ref)
                        app.store.delete_credential_ref(ref)
                        self.send_json({"ok": True})
                        return
                    if path == "/api/config/update":
                        mapping = body.get("configs") if isinstance(body.get("configs"), dict) else body
                        if not isinstance(mapping, dict):
                            raise ValueError("configs must be a dict")
                        # Validate integer/numeric bounds
                        num_keys = {
                            "batch_timeout_s": (60, 7200),
                            "site_timeout_s": (10, 600),
                            "sso_timeout_s": (5, 300),
                            "cf_wait_s": (5, 300),
                            "captcha_wait_s": (5, 300),
                            "connect_retries": (0, 10),
                            "ai_auto_retry_max": (1, 10),
                        }
                        for k, (min_v, max_v) in num_keys.items():
                            if k in mapping:
                                try:
                                    val = int(mapping[k])
                                    if not (min_v <= val <= max_v):
                                        raise ValueError(f"{k} must be between {min_v} and {max_v}")
                                except (ValueError, TypeError):
                                    raise ValueError(f"{k} must be an integer between {min_v} and {max_v}")
                        if app.dispatch_mode == "remote":
                            # Batch/runtime configs are consumed by the runner
                            # on the executor node (cron schedule, timeouts,
                            # AI inspector...) — the write must land there.
                            self.send_json(
                                app._dispatch_write("update_config", {"configs": mapping}),
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        updated = app.store.set_configs(mapping)
                        self.send_json({"ok": True, "configs": updated})
                        return
                    if path == "/api/ai/test":
                        channel_name = str(body.get("channel_name") or body.get("name") or "Primary").strip()
                        is_fallback = "fallback" in channel_name.lower() or "备用" in channel_name
                        url_key = "ai_inspector_fallback_base_url" if is_fallback else "ai_inspector_base_url"
                        key_key = "ai_inspector_fallback_api_key" if is_fallback else "ai_inspector_api_key"
                        model_key = "ai_inspector_fallback_model" if is_fallback else "ai_inspector_model"

                        base_url = str(
                            body.get("base_url")
                            or body.get(url_key)
                            or app.store.get_config(url_key)
                            or ""
                        ).strip().rstrip("/")
                        api_key = str(
                            body.get("api_key")
                            or body.get(key_key)
                            or app.store.get_config(key_key)
                            or ""
                        ).strip()
                        model = str(
                            body.get("model")
                            or body.get(model_key)
                            or app.store.get_config(model_key)
                            or "gpt-4o-mini"
                        ).strip()
                        if not base_url:
                            raise ValueError(f"[{channel_name}] API Base URL 不能为空")
                        if not api_key:
                            raise ValueError(f"[{channel_name}] API Key 不能为空")

                        import time, urllib.request, urllib.error
                        from checkin_core.ai_inspector import create_ssl_context
                        test_url = f"{base_url}/chat/completions"
                        test_payload = {
                            "model": model,
                            "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 8,
                        }
                        req = urllib.request.Request(
                            test_url,
                            data=json.dumps(test_payload).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {api_key}",
                                "User-Agent": "HermesCheckinInspector/1.0",
                            },
                            method="POST",
                        )
                        ctx = create_ssl_context()
                        t0 = time.monotonic()
                        try:
                            with urllib.request.urlopen(req, timeout=12.0, context=ctx) as resp:
                                resp_data = json.loads(resp.read().decode("utf-8"))
                                latency_ms = int((time.monotonic() - t0) * 1000)
                                choices = resp_data.get("choices", [])
                                reply_text = choices[0].get("message", {}).get("content", "").strip() if choices else "OK"
                                self.send_json({
                                    "ok": True,
                                    "channel_name": channel_name,
                                    "model": model,
                                    "latency_ms": latency_ms,
                                    "reply": reply_text[:80],
                                    "sample_reply": reply_text[:80],
                                })
                                return
                        except urllib.error.HTTPError as http_err:
                            body_snippet = http_err.read().decode("utf-8", errors="replace")[:200]
                            raise ValueError(f"[{channel_name}] HTTP {http_err.code}: {body_snippet}")
                        except urllib.error.URLError as url_err:
                            raise ValueError(f"[{channel_name}] 网络连接失败: {url_err.reason}")
                        except Exception as test_exc:
                            raise ValueError(f"[{channel_name}] 模型调用失败: {test_exc}")
                    if path == "/api/notify/test":
                        token = str(body.get("tg_bot_token") or app.store.get_config("tg_bot_token") or "").strip()
                        chat_id = str(body.get("tg_chat_id") or app.store.get_config("tg_chat_id") or "").strip()
                        if not token or not chat_id:
                            raise ValueError("TG Bot Token and Chat ID are required for test")
                        text = str(body.get("text") or "").strip()
                        if not text:
                            text = (
                                f"🔔 【公益站签到系统】TG 通知通道测试\n"
                                f"──────────────────\n"
                                f"• 测试时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                                f"• 通道状态: 正常连通 (Connected)\n"
                                f"• 消息来源: Web 控制台调度配置面板"
                            )
                        ok = _send_telegram_raw(token, chat_id, text)
                        if not ok:
                            raise ValueError("TG 消息发送失败，请检查 Bot Token 与 Chat ID 是否有效")
                        self.send_json({"ok": True})
                        return
                    self.send_error(HTTPStatus.NOT_FOUND)
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except Exception as exc:
                    logging.exception("api error %s", path)
                    self.send_json({"error": type(exc).__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)

        return Handler


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    store: SystemStore | None = None,
    require_auth: bool = True,
):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("daily-checkin UI may bind only to loopback")
    app = CheckinWebApp(store=store, require_auth=require_auth)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), app.handler())
    print(f"Daily Check-in UI: http://{host}:{server.server_port}", flush=True)
    # Print the bearer token to stderr exactly once, only when this startup
    # created the token file (so the secret never lands in normal logs on
    # subsequent restarts).
    if app.token_created:
        print(f"web bearer token: {app.auth_token}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-auth", action="store_true", help="Disable auth check")
    args = parser.parse_args()
    serve(args.host, args.port, require_auth=not args.no_auth)

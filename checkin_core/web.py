"""Local-only control plane and web API for daily-checkin."""
from __future__ import annotations

import json
import logging
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
from urllib.parse import urlparse

from checkin_core.keychain import CredentialStore, make_ref, credential_store_from_env
from checkin_core.notify import _send_telegram_raw
from checkin_core.store import SystemStore, store_from_env, obsidian_daily_task_path

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
    return os.environ.get(WEB_PASSWORD_ENV) or None


ALLOWED_CRED_KINDS = {"token", "cookie", "password"}


def _constant_eq(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return secrets.compare_digest(a, b)


# CORS allow-list for the separate static frontend (http://127.0.0.1:8766 / https://check.example.com)
# calling this API on 8765 / check-api.example.com. Never wildcard; untrusted origins are rejected.
_DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:8766,http://localhost:8766,http://check.example.com:8766,https://check.example.com"
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
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
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
        self.executor = JobExecutor(self.store)
        self.csrf_path = self.store.path.parent / CSRF_FILENAME
        self.csrf = load_or_create_csrf(self.csrf_path)
        # Durable jobs survived a possible earlier web-process crash; requeue
        # any that were still pending before the API went away.
        self.executor.recover_stale_jobs()

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DailyCheckin/1"

            def log_message(self, format: str, *args) -> None:
                print(f"web {self.address_string()} {format % args}", flush=True)

            def allowed_host(self) -> bool:
                host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
                return host in {"127.0.0.1", "localhost", "::1", "check.example.com", "check-api.example.com"}

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
                if pwd is not None and _constant_eq(self.headers.get("X-DailyCheckin-Password"), pwd):
                    return True
                if pwd is not None:
                    return False
                # Legacy/token fallback: Authorization: Bearer <web.token>
                header = self.headers.get("Authorization", "")
                scheme, _, token = header.partition(" ")
                if scheme.lower() != "bearer" or not token:
                    return False
                return _constant_eq(token.strip(), app.auth_token)

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
                    data = app.store.snapshot(day)
                    data["csrf"] = app.csrf
                    self.send_json(data)
                    return
                if path == "/api/config":
                    configs = app.store.get_all_configs()
                    self.send_json({"ok": True, "configs": configs})
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
                if not self.check_auth():
                    print(f"web [rejected:auth] {self.path}", flush=True)
                    self.send_json({"error": "auth"}, HTTPStatus.UNAUTHORIZED)
                    return
                if not self.check_csrf():
                    print(f"web [rejected:csrf] {self.path}", flush=True)
                    self.send_json({"error": "csrf"}, HTTPStatus.FORBIDDEN)
                    return
                path = urlparse(self.path).path
                try:
                    body = self.read_json()
                    if path == "/api/run":
                        raw_names = body.get("sites") or []
                        if not isinstance(raw_names, list):
                            raise ValueError("sites must be a list")
                        names = [_clean_site_name(n) for n in raw_names]
                        names = [n for n in names if n]
                        for n in names:
                            if "," in n:
                                raise ValueError("site name cannot contain commas")
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
                        res = app.store.update_site_config(name, **updates)
                        self.send_json({"ok": True, "site": res})
                        return
                    if path == "/api/sites/delete":
                        name = _clean_site_name(body.get("name"))
                        if not name:
                            raise ValueError("name required")
                        res = app.store.delete_site(name)
                        cred_ref = res.get("credential_ref")
                        if cred_ref:
                            try:
                                app.credentials.delete(cred_ref)
                            except Exception as exc:
                                logging.warning("failed to delete keychain ref %s for site %s: %s", cred_ref, name, exc)
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
                        ref = make_ref(site, kind)
                        app.credentials.put(ref, secret)
                        app.store.set_credential_ref(site, ref, kind, label)
                        self.send_json({"ok": True, "ref": ref})
                        return
                    if path == "/api/credentials/delete":
                        ref = str(body.get("ref") or "").strip()
                        if not ref:
                            raise ValueError("ref required")
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
                        }
                        for k, (min_v, max_v) in num_keys.items():
                            if k in mapping:
                                try:
                                    val = int(mapping[k])
                                    if not (min_v <= val <= max_v):
                                        raise ValueError(f"{k} must be between {min_v} and {max_v}")
                                except (ValueError, TypeError):
                                    raise ValueError(f"{k} must be an integer between {min_v} and {max_v}")
                        updated = app.store.set_configs(mapping)
                        self.send_json({"ok": True, "configs": updated})
                        return
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

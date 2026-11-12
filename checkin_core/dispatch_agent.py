"""Executor-node dispatch agent (runs on the machine with Chrome CDP 9222).

The cloud panel pushes jobs here over the EasyTier mesh; this agent never
polls anything. It listens on the mesh IP only (never 0.0.0.0), verifies the
shared per-node token in constant time, executes the job against the local
runner/authoritative DB, then reports the result back to the panel over the
public HTTPS callback and refreshes the panel's replica DB via the atomic
sync script.

Idle footprint is a sleeping ThreadingHTTPServer plus a heartbeat thread —
no scheduled wakeups, no outbound traffic until a job actually arrives.
"""
from __future__ import annotations

import argparse
import atexit
import datetime
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # POSIX only; Windows uses msvcrt below (2026-11-06 multi-node support).
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt  # type: ignore
except ImportError:
    msvcrt = None

ROOT = Path(__file__).resolve().parents[1]

BIND_ENV = "DAILY_CHECKIN_DISPATCH_BIND"
PORT_ENV = "DAILY_CHECKIN_DISPATCH_PORT"
TOKEN_ENV = "DAILY_CHECKIN_DISPATCH_TOKEN"
CALLBACK_ENV = "DAILY_CHECKIN_DISPATCH_CALLBACK_URL"
HEARTBEAT_ENV = "DAILY_CHECKIN_AGENT_HEARTBEAT_S"
JOB_TIMEOUT_ENV = "DAILY_CHECKIN_AGENT_JOB_TIMEOUT_S"
RESULT_RETRIES_ENV = "DAILY_CHECKIN_AGENT_RESULT_RETRIES"
# Reverse tunnel mode: when DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS is set, the
# agent binds loopback only and keeps a persistent `ssh -R` tunnel to the
# panel host (alias from ~/.ssh/config), so the panel pushes jobs to its own
# loopback and they reach this agent without any inbound surface on the node.
TUNNEL_ALIAS_ENV = "DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS"
TUNNEL_REMOTE_PORT_ENV = "DAILY_CHECKIN_DISPATCH_TUNNEL_REMOTE_PORT"
# Morning-batch quiet window ("HH:MM-HH:MM", local time): inside this window
# the agent refuses (503) every push so a dispatched job can never hold the
# runner lock across the 08:10 cron batch (which exits 4 when locked).
# A full dispatched batch self-limits to 40min, so a window starting ≥40min
# before the cron fire guarantees the lock is free at 08:10; the flock probe
# covers the batch tail after the window ends.
QUIET_WINDOW_ENV = "DAILY_CHECKIN_AGENT_QUIET_WINDOW"
# When set, the agent passes `--cdp <url>` to the runner explicitly — the
# runner then runs in strict attach-only mode (P0: never launches Chrome; a
# down CDP fails fast with a clear attribution instead of wandering through
# auto-discovery/launch paths).
AGENT_CDP_ENV = "DAILY_CHECKIN_AGENT_CDP"
AGENT_LOG_FILE_ENV = "DAILY_CHECKIN_AGENT_LOG_FILE"
# Optional callback forward tunnel: when set, the agent additionally keeps a
# `ssh -L` tunnel to the panel host and switches result/heartbeat callbacks
# from the public HTTPS URL to the tunnel loopback. Rationale (2026-11-06 win
# node): node-side proxies (clash/sing-box TUN) intermittently kill outbound
# TLS to the panel's public domain; the ssh tunnel is the same proven
# Mac-initiated path the delivery uses. Value format: "<local_port>", target
# is always the panel host's loopback API port 8765.
CALLBACK_TUNNEL_ALIAS_ENV = "DAILY_CHECKIN_CALLBACK_TUNNEL_ALIAS"
CALLBACK_TUNNEL_LOCAL_PORT_ENV = "DAILY_CHECKIN_CALLBACK_TUNNEL_LOCAL_PORT"
# Single-instance lock: two agents fighting over the same reverse port is the
# 2026-11-09 outage mode ("remote port forwarding failed for listen port 19778").
# The lock file lives next to the durable agent log by default.
AGENT_LOCK_ENV = "DAILY_CHECKIN_AGENT_LOCK_FILE"
DEFAULT_CALLBACK_TUNNEL_LOCAL_PORT = 19780
DEFAULT_BIND = "10.144.144.100"
DEFAULT_PORT = 9777
DEFAULT_TUNNEL_REMOTE_PORT = 19777
DEFAULT_CALLBACK = "https://check-api.mangoqwq.com"
DEFAULT_HEARTBEAT_S = 300
DEFAULT_JOB_TIMEOUT_S = 7200  # mirrors the legacy JobExecutor 2h cap
DEFAULT_RESULT_RETRIES = 8
_MAX_MESSAGE_LEN = 4000
_VALID_KINDS = (
    "site", "all", "manual", "delete_site",
    "add_site", "update_site", "set_credential", "delete_credential",
    "update_config", "update_balance",
)

_RESULT_BACKOFF_S = (2, 4, 8, 16, 30, 30, 30, 30)


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


def _log(msg: str) -> None:
    line = f"[dispatch-agent {time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # Optional durable log: the Windows resident agent has no console, and
    # PowerShell transcripts cannot capture native exe stdout reliably (PS 5.1).
    log_file = os.environ.get(AGENT_LOG_FILE_ENV, "").strip()
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _constant_eq(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    import secrets

    return secrets.compare_digest(a, b)


def parse_quiet_window(raw: str | None) -> tuple["time", "time"] | None:
    """Parse 'HH:MM-HH:MM' into (start, end) time objects; None if unset/bad."""
    if not raw or not raw.strip():
        return None
    try:
        start_s, end_s = raw.strip().split("-", 1)
        start = datetime.datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.datetime.strptime(end_s.strip(), "%H:%M").time()
        return start, end
    except ValueError:
        return None


def in_quiet_window(now: "time", window: tuple["time", "time"] | None) -> bool:
    if window is None:
        return False
    start, end = window
    if start <= end:
        return start <= now <= end
    # Window crossing midnight (e.g. 23:30-00:30).
    return now >= start or now <= end


def runner_lock_probe() -> bool:
    """True when the runner batch lock is free (a morning batch is not running).

    Non-blocking probe on the same lock file the runner uses, cross-platform:
    fcntl.flock on POSIX, msvcrt.locking on Windows. The fd is closed
    immediately so the runner can acquire it cleanly afterwards.
    """
    log_dir = os.environ.get("DAILY_CHECKIN_LOG_DIR", "")
    lock_dir = Path(log_dir) if log_dir else Path.home() / "daily-checkin-staging" / "log"
    lock_path = lock_dir / "daily-checkin.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
    except OSError:
        return True  # cannot even probe; let the runner decide
    try:
        if msvcrt is not None:  # Windows
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return True
        if fcntl is not None:  # POSIX
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        return True  # no locking primitive available; let the runner decide
    except OSError:
        return False
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _agent_lock_path() -> Path:
    raw = os.environ.get(AGENT_LOCK_ENV, "").strip()
    if raw:
        return Path(raw)
    log_dir = os.environ.get("DAILY_CHECKIN_LOG_DIR", "").strip()
    base = Path(log_dir) if log_dir else Path.home() / ".hermes" / "checkin"
    return base / "dispatch-agent.lock"


def acquire_single_instance(path: Path):
    """Hold an exclusive lock for the agent's lifetime.

    Returns (state, handle) where state is "ok" (handle must stay open), "busy"
    (another agent owns it) or "unavailable" (no usable lock primitive/path —
    proceed anyway, the old behaviour).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        return "unavailable", None
    try:
        if msvcrt is not None:  # Windows: lock byte 0 without blocking
            # "a+" 在非空文件上位置停在 EOF；msvcrt.locking 锁的是当前位置起
            # 的区间。不先 seek(0)，第一个 agent 写入 pid 后第二个 agent 锁
            # 的是 EOF 字节，与 holder 的字节 0 区间不冲突——单实例锁失效
            # （2026-11-11 windows-smoke 实测锁形同虚设）。
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:  # POSIX: flock is released on process exit
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.close()
        except OSError:
            pass
        return "busy", None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    return "ok", fh


def reverse_tunnel_spec(remote_port: int, port: int) -> str:
    """ssh -R spec: panel-host ``remote_port`` -> node agent ``port``."""
    return f"127.0.0.1:{remote_port}:127.0.0.1:{port}"


def forward_tunnel_spec(callback_port: int) -> str:
    """ssh -L spec: node ``callback_port`` -> panel-host API 8765."""
    return f"127.0.0.1:{callback_port}:127.0.0.1:8765"


def kill_stale_tunnels(specs: list[str]) -> int:
    """Kill ssh children left behind by a previous agent process.

    Windows has no parent-death signal: when the agent is killed hard its
    `ssh -R` child survives and keeps the panel's remote port bound forever, so
    every new agent dies with "remote port forwarding failed for listen port
    19778" (2026-11-09 outage). Sweep by the exact forwarding spec before this
    process starts its own tunnels. Safe because the single-instance lock is
    acquired first, so any match is an orphan.
    """
    killed = 0
    for spec in specs:
        try:
            if msvcrt is not None:  # Windows
                ps = (
                    "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" "
                    f"| Where-Object {{ $_.CommandLine -like '*{spec}*' }} "
                    "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "
                    "Write-Output $_.ProcessId }"
                )
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, text=True, timeout=20,
                )
                killed += sum(
                    1 for line in (proc.stdout or "").splitlines() if line.strip().isdigit()
                )
            else:  # POSIX: pgrep the exact spec so we count every orphan, not 0/1
                proc = subprocess.run(
                    ["pgrep", "-f", re.escape(spec)], capture_output=True, text=True, timeout=10
                )
                for pid in (proc.stdout or "").split():
                    if not pid.isdigit():
                        continue
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        killed += 1
                    except OSError as exc:
                        _log(f"cannot kill stale tunnel pid {pid}: {exc}")
        except Exception as exc:
            _log(f"stale tunnel sweep failed for {spec}: {type(exc).__name__}: {exc}")
    if killed:
        _log(f"killed {killed} stale tunnel process(es)")
        time.sleep(1.0)  # let the panel's sshd release the forwarded ports
    return killed


class DispatchAgent:
    def __init__(self):
        self.tunnel_alias = (os.environ.get(TUNNEL_ALIAS_ENV) or "").strip()
        self.bind = (os.environ.get(BIND_ENV) or "").strip()
        if not self.bind:
            # Tunnel mode rides ssh -R: no inbound surface needed, loopback suffices.
            self.bind = "127.0.0.1" if self.tunnel_alias else DEFAULT_BIND
        self.port = _env_int(PORT_ENV, DEFAULT_PORT)
        self.tunnel_remote_port = _env_int(TUNNEL_REMOTE_PORT_ENV, DEFAULT_TUNNEL_REMOTE_PORT)
        self.token = (os.environ.get(TOKEN_ENV) or "").strip()
        self.callback = (os.environ.get(CALLBACK_ENV) or DEFAULT_CALLBACK).strip().rstrip("/")
        self.heartbeat_s = _env_float(HEARTBEAT_ENV, DEFAULT_HEARTBEAT_S)
        self.job_timeout_s = _env_float(JOB_TIMEOUT_ENV, DEFAULT_JOB_TIMEOUT_S)
        self.result_retries = _env_int(RESULT_RETRIES_ENV, DEFAULT_RESULT_RETRIES)
        self.quiet_window = parse_quiet_window(os.environ.get(QUIET_WINDOW_ENV))
        self.callback_tunnel_alias = (os.environ.get(CALLBACK_TUNNEL_ALIAS_ENV) or "").strip()
        self.callback_tunnel_port = _env_int(
            CALLBACK_TUNNEL_LOCAL_PORT_ENV, DEFAULT_CALLBACK_TUNNEL_LOCAL_PORT
        )
        if self.callback_tunnel_alias:
            # Route callbacks through the ssh -L loopback tunnel instead of
            # the public HTTPS domain (node-proxy-proof, encrypted end to end).
            self.callback = f"http://127.0.0.1:{self.callback_tunnel_port}"
        if not self.token:
            raise SystemExit(f"FATAL: {TOKEN_ENV} is required (0600 env, never commit)")
        self._job_mutex = threading.Lock()
        self._stop = threading.Event()
        # Job completion and the day-rollover heartbeat both trigger a DB
        # sync. It is best-effort and posts to one fixed remote temp path,
        # so overlapping copies must be skipped rather than raced.
        self._sync_lock = threading.Lock()
        self._tunnel_procs: list[subprocess.Popen] = []
        self._tunnel_procs_lock = threading.Lock()
        self._lock_handle = None

    def in_quiet_window_now(self) -> bool:
        """True when local time is inside the morning-batch quiet window."""
        return in_quiet_window(datetime.datetime.now().time(), self.quiet_window)

    # ------------------------------------------------------------------ HTTP

    def handler(self):
        agent = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DailyCheckinDispatchAgent/1"

            def log_message(self, format: str, *args) -> None:
                try:
                    print(f"agent {self.address_string()} {format % args}", flush=True)
                except Exception:
                    pass

            def _send_json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 100_000:
                    raise ValueError("request too large")
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                if self.path.rstrip("/") == "/health":
                    self._send_json({
                        "ok": True,
                        "node": agent.bind,
                        "busy": agent._job_mutex.locked(),
                    })
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self):
                if self.path.rstrip("/") == "/health":
                    self._send_json({"ok": True, "node": agent.bind})
                    return
                if self.path.rstrip("/") != "/dispatch":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not _constant_eq(
                    (self.headers.get("X-Node-Token") or "").strip(), agent.token
                ):
                    self._send_json({"error": "auth"}, HTTPStatus.UNAUTHORIZED)
                    return
                try:
                    body = self._read_json()
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                kind = str(body.get("kind") or "").strip()
                if kind not in _VALID_KINDS:
                    self._send_json(
                        {"error": f"kind must be one of {list(_VALID_KINDS)}"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                job_id = body.get("job_id")
                try:
                    job_id = int(job_id)
                except (TypeError, ValueError):
                    self._send_json({"error": "job_id required"}, HTTPStatus.BAD_REQUEST)
                    return
                # Serialize execution on the node: one runner at a time. Busy
                # (another job running or the 08:10 morning batch holds the
                # runner lock) → 503 so the panel keeps the job queued and
                # redelivers later instead of failing it.
                if agent.in_quiet_window_now():
                    self._send_json(
                        {"error": "busy", "reason": "quiet_window"},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                if agent._job_mutex.locked() or not runner_lock_probe():
                    self._send_json({"error": "busy"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                if not agent._job_mutex.acquire(blocking=False):
                    self._send_json({"error": "busy"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                threading.Thread(
                    target=agent.execute_job,
                    args=(job_id, kind, body),
                    daemon=True,
                ).start()
                self._send_json(
                    {"ok": True, "accepted": True, "job_id": job_id},
                    HTTPStatus.ACCEPTED,
                )

        return Handler

    # ------------------------------------------------------------- execution

    def execute_job(self, job_id: int, kind: str, body: dict) -> None:
        status, exit_code, message = "failed", 2, ""
        try:
            if kind in ("site", "all"):
                status, exit_code, message = self._run_sites(kind, body)
            elif kind == "manual":
                status, exit_code, message = self._run_manual(body)
            elif kind == "delete_site":
                status, exit_code, message = self._run_delete_site(body)
            elif kind in ("add_site", "update_site", "set_credential", "delete_credential", "update_config", "update_balance"):
                status, exit_code, message = self._run_config_write(kind, body)
            else:  # defensive; handler already validates
                status, exit_code, message = "failed", 2, f"unknown kind: {kind}"
        except Exception as exc:
            status, exit_code = "failed", 2
            message = f"agent error: {type(exc).__name__}: {exc}"[:_MAX_MESSAGE_LEN]
            _log(f"job #{job_id} crashed: {exc}")
        finally:
            # Report-then-release must be fault-isolated: an unexpected crash
            # in _post_result must never skip the mutex release,
            # or the node stops accepting jobs until the agent restarts
            # (reproduced: one raising _post_result left the lock held and the
            # next dispatch got 503 busy forever).
            try:
                self._post_result(job_id, status, exit_code, message)
            except Exception as exc:
                _log(f"post-job reporting crashed (job #{job_id}): {type(exc).__name__}: {exc}")
            finally:
                if self._job_mutex.locked():
                    self._job_mutex.release()
                _log(f"job #{job_id} finished: {status} exit={exit_code}")
            # Mutex is freed so the node can accept incoming jobs immediately;
            # DB sync follows without keeping the node in a false busy state.
            try:
                self._sync_db()
            except Exception as exc:
                _log(f"post-job db sync error (job #{job_id}): {type(exc).__name__}: {exc}")

    def _terminate_process_tree(self, process: subprocess.Popen, timeout_s: float = 10.0) -> None:
        """Kill the runner process tree, cross-platform (mirrors legacy JobExecutor).

        Windows orphaned grandchildren keep the stdout/stderr pipe handles open
        (real-world hang: 2026-11-06 dispatched job #2 on the win node), so
        killing just the runner is not enough — taskkill /T covers the tree;
        POSIX uses the process group the runner was spawned into.
        """
        if sys.platform == "win32" and msvcrt is not None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            return
        if fcntl is not None and hasattr(os, "killpg"):
            try:
                pgid = os.getpgid(process.pid)
            except ProcessLookupError:
                return
            try:
                os.killpg(pgid, signal.SIGTERM)
                try:
                    process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
                    process.wait(timeout=timeout_s)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            return
        try:
            process.kill()
        except Exception:
            pass

    def _run_sites(self, kind: str, body: dict) -> tuple[str, int, str]:
        sites = [str(s) for s in (body.get("sites") or []) if str(s).strip()]
        if kind == "site" and not sites:
            return "failed", 2, "kind=site requires non-empty sites"
        cmd = [sys.executable, str(ROOT / "stealth_checkin_runner.py"), "--source", "web"]
        if kind == "site":
            cmd.extend(["--only", ",".join(sites)])
        cdp = os.environ.get(AGENT_CDP_ENV, "").strip()
        if cdp:
            cmd.extend(["--cdp", cdp])
        _log(f"exec: {' '.join(cmd[:6])}{' ...' if len(cmd) > 6 else ''}")
        # Run the runner in its own process group (POSIX) / process group flag
        # (Windows) so a hung site can never outlive the job timeout and
        # orphaned children can be reaped as a tree.
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs,
            )
        except Exception as exc:
            return "failed", 2, f"runner_spawn_failed: {type(exc).__name__}: {exc}"[:_MAX_MESSAGE_LEN]

        deadline = time.monotonic() + self.job_timeout_s
        stdout = stderr = ""
        timed_out = False
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=15)
                break
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    # Runner exited but pipe EOF never arrived — orphaned
                    # grandchildren hold the inherited handles. Kill the tree
                    # and drain once more.
                    self._terminate_process_tree(proc)
                    try:
                        stdout, stderr = proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        stdout, stderr = "", ""
                    break
                if time.monotonic() >= deadline:
                    self._terminate_process_tree(proc)
                    timed_out = True
                    try:
                        stdout, stderr = proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        stdout, stderr = "", ""
                    break
        message = ((stdout or "") + "\n" + (stderr or ""))[-_MAX_MESSAGE_LEN:]
        if timed_out:
            return (
                "failed",
                2,
                f"runner_timeout: process tree terminated after {int(self.job_timeout_s)}s {message}"[:_MAX_MESSAGE_LEN],
            )
        status = "done" if (proc.returncode or 0) == 0 else "failed"
        return status, proc.returncode, message

    def _run_manual(self, body: dict) -> tuple[str, int, str]:
        from checkin_core.store import store_from_env

        site = str(body.get("payload", {}).get("site") or "").strip()
        day = str(body.get("payload", {}).get("day") or "").strip()
        if not site or not day:
            return "failed", 2, "kind=manual requires payload.site and payload.day"
        _log(f"manual complete: site={site} day={day}")
        res = store_from_env().complete_task_manually(day, site)
        return "done", 0, json.dumps(res, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

    def _run_delete_site(self, body: dict) -> tuple[str, int, str]:
        """Delete a site on THIS node: authoritative DB cascade + keychain
        credentials + Obsidian projection, then the DB syncs back to the
        panel replica (mirrors the local-mode /api/sites/delete handler)."""
        from checkin_core.keychain import credential_store_from_env
        from checkin_core.store import store_from_env

        site = str(body.get("payload", {}).get("site") or "").strip()
        ignore_missing = bool(body.get("payload", {}).get("ignore_missing", False))
        if not site:
            return "failed", 2, "kind=delete_site requires payload.site"
        _log(f"delete site: name={site} ignore_missing={ignore_missing}")
        res = store_from_env().delete_site(site, ignore_missing=ignore_missing)
        cred_refs = res.get("credential_refs") or (
            [res["credential_ref"]] if res.get("credential_ref") else []
        )
        deleted_creds = 0
        for c_ref in cred_refs:
            if not c_ref:
                continue
            try:
                credential_store_from_env().delete(c_ref)
                deleted_creds += 1
            except Exception as exc:
                _log(f"keychain delete failed for {c_ref} (site {site}): {exc}")
        summary = {
            "ok": True,
            "name": site,
            "deleted_tasks": res.get("deleted_tasks", 0),
            "deleted_credentials": deleted_creds,
        }
        return "done", 0, json.dumps(summary, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

    def _run_config_write(self, kind: str, body: dict) -> tuple[str, int, str]:
        """Panel-driven authoritative-DB writes (site catalog, credentials,
        system config), executed on THIS node then synced back to the panel
        replica. Each mirrors its local-mode web handler exactly."""
        from checkin_core.keychain import credential_store_from_env, make_ref
        from checkin_core.store import store_from_env

        payload = body.get("payload", {}) or {}
        store = store_from_env()
        if kind == "add_site":
            name = str(payload.get("site") or "").strip()
            if not name:
                return "failed", 2, "kind=add_site requires payload.site"
            store.upsert_task(
                str(payload.get("day") or self._today()),
                name,
                str(payload.get("url") or ""),
                provider=str(payload.get("provider") or "legacy_browser"),
                checkin_mode=str(payload.get("checkin_mode") or "auto"),
                tags=str(payload.get("tags") or ""),
                status="pending", source="system",
            )
            return "done", 0, json.dumps({"ok": True, "name": name, "action": "add_site"}, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

        if kind == "update_site":
            name = str(payload.get("site") or "").strip()
            if not name:
                return "failed", 2, "kind=update_site requires payload.site"
            fields = {}
            for key in ("checkin_mode", "tags", "enabled", "url", "provider"):
                if key in payload and payload[key] is not None:
                    fields[key] = payload[key]
            if not fields:
                return "failed", 2, "kind=update_site requires at least one field"
            res = store.update_site_config(name, **fields)
            return "done", 0, json.dumps({"ok": True, "name": name, "action": "update_site", "site": res}, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

        if kind == "set_credential":
            site = str(payload.get("site") or "").strip()
            secret = str(payload.get("secret") or "")
            if not site or not secret:
                return "failed", 2, "kind=set_credential requires payload.site and payload.secret"
            kind_cred = str(payload.get("kind") or "token").strip().lower()
            if kind_cred not in ("token", "cookie", "password"):
                return "failed", 2, f"invalid credential kind: {kind_cred}"
            from checkin_core.keychain import make_ref as _make_ref
            ref = _make_ref(site, kind_cred)
            credential_store_from_env().put(ref, secret)
            store.set_credential_ref(site, ref, kind_cred, str(payload.get("label") or ""))
            return "done", 0, json.dumps({"ok": True, "ref": ref, "action": "set_credential"}, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

        if kind == "delete_credential":
            ref = str(payload.get("ref") or "").strip()
            if not ref:
                return "failed", 2, "kind=delete_credential requires payload.ref"
            credential_store_from_env().delete(ref)
            store.delete_credential_ref(ref)
            return "done", 0, json.dumps({"ok": True, "ref": ref, "action": "delete_credential"}, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

        if kind == "update_config":
            mapping = payload.get("configs") if isinstance(payload.get("configs"), dict) else payload
            if not isinstance(mapping, dict) or not mapping:
                return "failed", 2, "kind=update_config requires payload.configs dict"
            # Numeric bounds mirror the web handler validation so a bad panel
            # payload fails here with the same message it would get locally.
            num_keys = {
                "batch_timeout_s": (60, 7200), "site_timeout_s": (10, 600),
                "sso_timeout_s": (5, 300), "cf_wait_s": (5, 300),
                "captcha_wait_s": (5, 300), "connect_retries": (0, 10),
                "ai_auto_retry_max": (1, 10),
            }
            for k, (min_v, max_v) in num_keys.items():
                if k in mapping:
                    try:
                        val = int(mapping[k])
                        if not (min_v <= val <= max_v):
                            raise ValueError(f"{k} must be between {min_v} and {max_v}")
                    except (ValueError, TypeError):
                        return "failed", 2, f"{k} must be an integer between {min_v} and {max_v}"
            store.set_configs(mapping)
            return "done", 0, json.dumps({"ok": True, "action": "update_config", "keys": sorted(mapping.keys())}, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

        if kind == "update_balance":
            name = str(payload.get("site") or "").strip()
            if not name:
                return "failed", 2, "kind=update_balance requires payload.site"
            amount = None
            amount_raw = payload.get("amount")
            if amount_raw is not None and str(amount_raw).strip() != "":
                try:
                    amount = float(amount_raw)
                    if not math.isfinite(amount):
                        raise ValueError("amount must be a finite number")
                except (ValueError, TypeError):
                    return "failed", 2, "amount must be a valid finite number"
            res = store.update_site_balance_manual(
                name,
                amount=amount,
                currency=str(payload.get("currency") or "USD").strip().upper(),
                raw_text=str(payload.get("raw_text") or "").strip(),
                day=str(payload.get("day") or ""),
                source="manual",
            )
            return "done", 0, json.dumps({"ok": True, "name": name, "action": "update_balance", "balance": res}, ensure_ascii=False)[:_MAX_MESSAGE_LEN]

        return "failed", 2, f"unknown config-write kind: {kind}"

    @staticmethod
    def _today() -> str:
        import datetime
        return datetime.date.today().isoformat()

    # -------------------------------------------------------------- callback
    def _post_json(self, path: str, payload: dict, timeout_s: float = 10.0) -> bool:
        req = urllib.request.Request(
            f"{self.callback}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Node-Token": self.token,
                "User-Agent": "DailyCheckinDispatchAgent/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                resp.read()
                return resp.status in (200, 202)
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:
                pass
            _log(f"callback {path} -> HTTP {exc.code}")
            return False
        except Exception as exc:
            _log(f"callback {path} failed: {type(exc).__name__}: {exc}")
            return False

    def _post_result(self, job_id: int, status: str, exit_code: int, message: str) -> None:
        payload = {
            "job_id": int(job_id),
            "status": status,
            "exit_code": int(exit_code),
            "message": str(message or "")[:_MAX_MESSAGE_LEN],
        }
        attempts = max(1, self.result_retries)
        for i in range(attempts):
            if self._post_json("/api/dispatch/result", payload):
                _log(f"result reported for job #{job_id} (attempt {i + 1})")
                return
            if i + 1 < attempts:
                time.sleep(_RESULT_BACKOFF_S[min(i, len(_RESULT_BACKOFF_S) - 1)])
        # Panel-side reaper (2h) closes the job if every retry failed.
        _log(f"result reporting exhausted for job #{job_id}; panel reaper will close it")

    def _sync_db(self) -> None:
        # Per-node replica sync to the panel; platform twin scripts keep the
        # same atomic scp+mv contract (Windows: sync-db-to-panel.ps1).
        # Best-effort by contract: a skipped/concurrent sync is fine (the
        # panel just shows the previous snapshot until the next trigger).
        if not self._sync_lock.acquire(blocking=False):
            _log("panel DB sync already in flight; skipping duplicate trigger")
            return
        try:
            if sys.platform == "win32":
                script = ROOT / "scripts" / "sync-db-to-panel.ps1"
                cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
            else:
                script = ROOT / "scripts" / "sync-db-to-pxed.sh"
                if not script.is_file():
                    return
                cmd = ["/bin/zsh", str(script)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if proc.returncode == 0:
                    _log("panel DB sync ok")
                else:
                    _log(f"panel DB sync failed rc={proc.returncode}: {(proc.stderr or '')[-300:]}")
            except Exception as exc:
                _log(f"panel DB sync error: {type(exc).__name__}: {exc}")
        finally:
            self._sync_lock.release()

    # ------------------------------------------------------------- heartbeat

    def _heartbeat_loop(self) -> None:
        last_materialized_day = ""
        while not self._stop.wait(self.heartbeat_s):
            try:
                self._post_json("/api/dispatch/heartbeat", {"node": self.bind}, timeout_s=10.0)
            except Exception:
                pass
            # Day rollover: the panel is a read-only replica and cannot
            # materialize the new day's pending rows itself (that write
            # belongs to the authoritative DB here). Without this the task
            # list stays empty on the panel from 00:00 until the 08:10 batch.
            # Idempotent (INSERT OR IGNORE), so once per day is enough.
            try:
                today = datetime.date.today().isoformat()
                if today != last_materialized_day:
                    from checkin_core.store import store_from_env
                    store_from_env().materialize_day(today)
                    last_materialized_day = today
                    _log(f"materialized daily tasks for {today}")
                    # Push the fresh day's rows to the panel right away;
                    # otherwise the list stays empty until the next job sync.
                    self._sync_db()
            except Exception as exc:
                _log(f"day materialize failed: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- ssh tunnel

    def _tunnel_loop(self, *, forward: bool = False, local_port: int = 0,
                     remote_spec: str = "") -> None:
        """Keep an ssh tunnel alive.

        forward=False (delivery): reverse `ssh -R panel_loop:port -> agent` so
        the panel can push without any inbound surface on this node.
        forward=True (callback): local `ssh -L agent_loop:port -> panel API`
        so callbacks avoid the node's proxy-tampered public egress.
        """
        flag = "-L" if forward else "-R"
        bind = "127.0.0.1"
        spec = (
            f"{bind}:{local_port}:{remote_spec}" if forward
            else f"{bind}:{self.tunnel_remote_port}:{bind}:{self.port}"
        )
        while not self._stop.is_set():
            cmd = [
                "ssh", "-N", "-T",
                "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                flag, spec,
            ]
            if not forward:
                cmd.append(self.tunnel_alias)
            else:
                cmd.append(self.callback_tunnel_alias)
            _log(f"{'forward' if forward else 'reverse'} tunnel {spec} -> {self.tunnel_alias if not forward else self.callback_tunnel_alias}")
            proc = None
            try:
                proc = subprocess.Popen(cmd)
                with self._tunnel_procs_lock:
                    self._tunnel_procs.append(proc)
                while proc.poll() is None and not self._stop.is_set():
                    time.sleep(2)
            except Exception as exc:
                _log(f"tunnel spawn failed: {type(exc).__name__}: {exc}")
            if proc is not None:
                with self._tunnel_procs_lock:
                    try:
                        self._tunnel_procs.remove(proc)
                    except ValueError:
                        pass
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            if self._stop.is_set():
                return
            time.sleep(5)

    def _shutdown_tunnels(self) -> None:
        """Terminate tracked ssh children (atexit; hard-kill orphans are swept
        by the next agent via kill_stale_tunnels)."""
        with self._tunnel_procs_lock:
            procs = list(self._tunnel_procs)
            self._tunnel_procs.clear()
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------ main

    def serve_forever(self) -> None:
        lock_state, self._lock_handle = acquire_single_instance(_agent_lock_path())
        if lock_state == "busy":
            _log("another dispatch-agent instance is running; exiting")
            return
        # Orphans from a hard-killed predecessor must go before we bind the
        # same remote/local ports, otherwise ssh exits on every attempt.
        specs = []
        if self.tunnel_alias:
            specs.append(reverse_tunnel_spec(self.tunnel_remote_port, self.port))
        if self.callback_tunnel_alias:
            specs.append(forward_tunnel_spec(self.callback_tunnel_port))
        kill_stale_tunnels(specs)
        atexit.register(self._shutdown_tunnels)

        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        if self.tunnel_alias:
            threading.Thread(
                target=self._tunnel_loop,
                kwargs={"forward": False},
                daemon=True,
            ).start()
        if self.callback_tunnel_alias:
            threading.Thread(
                target=self._tunnel_loop,
                kwargs={"forward": True,
                        "local_port": self.callback_tunnel_port,
                        "remote_spec": "127.0.0.1:8765"},
                daemon=True,
            ).start()

        def _term(_sig, _frame):
            self._stop.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)

        server = None
        for attempt in range(1, 61):  # ~15min of retries, then exit for launchd
            try:
                ThreadingHTTPServer.allow_reuse_address = True
                server = ThreadingHTTPServer((self.bind, self.port), self.handler())
                break
            except OSError as exc:
                # The EasyTier TUN interface may come up after this agent at
                # boot; KeepAlive restarts us if we exhaust the retries.
                _log(f"bind {self.bind}:{self.port} retry {attempt}: {exc}")
                self._stop.wait(15)
                if self._stop.is_set():
                    return
        if server is None:
            raise SystemExit(1)
        self.server = server
        _log(
            f"listening on {self.bind}:{self.port} (callback {self.callback})"
            + (f", quiet window {os.environ.get(QUIET_WINDOW_ENV)}" if self.quiet_window else "")
        )
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m checkin_core.dispatch_agent",
        description="Executor-node agent: receives panel-pushed jobs over the mesh.",
    )
    parser.add_argument("--once-health", action="store_true",
                        help="print resolved config and exit (sanity check)")
    args = parser.parse_args()
    agent = DispatchAgent()
    if args.once_health:
        print(json.dumps({
            "bind": agent.bind,
            "port": agent.port,
            "tunnel_alias": agent.tunnel_alias or None,
            "tunnel_remote_port": agent.tunnel_remote_port if agent.tunnel_alias else None,
            "callback": agent.callback,
            "heartbeat_s": agent.heartbeat_s,
            "token_present": bool(agent.token),
            "runner": str(ROOT / "stealth_checkin_runner.py"),
        }, ensure_ascii=False))
        return
    agent.serve_forever()


if __name__ == "__main__":
    main()

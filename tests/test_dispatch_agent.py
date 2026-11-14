"""Dispatch agent (executor node) test suite — full behavioural coverage.

Layers covered (mirrors docs/dispatch-test-plan.md):
  1. HTTP surface: token auth, payload validation, busy (mutex/runner-lock) 503,
     health endpoint — via the real handler served on loopback.
  2. Accept→execute→report→release pipeline: 202 then background execution
     posts the result to the panel callback and refreshes the replica DB.
  3. Runner invocation contract: --source web / --only mapping, exit-code
     mapping, 2h timeout mapping, message truncation.
  4. Manual-complete dispatch and payload validation.
  5. Result reporting retries (transient panel outage, exhausted retries).
  6. Runner-lock probe (morning-batch busy detection) and startup guards.

The agent never touches real infrastructure in tests: the panel is a local
mock server, runner execution is patched at subprocess.run, and _sync_db is
always patched out.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

import checkin_core.dispatch_agent as agent_module
from checkin_core.dispatch_agent import DispatchAgent, runner_lock_probe

TOKEN = "agent-test-token"


class MockPanel:
    """Stands in for the pxed panel callback API (result/heartbeat)."""

    def __init__(self):
        self.received: list[dict] = []
        self.status_map: dict[str, int] = {}
        self.fail_first: dict[str, int] = {}  # path -> number of initial 500s
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def _handler(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                panel.received.append({
                    "path": self.path,
                    "token": self.headers.get("X-Node-Token"),
                    "body": body,
                })
                status = panel.status_map.get(self.path, 200)
                if panel.fail_first.get(self.path):
                    panel.fail_first[self.path] -= 1
                    status = 500
                raw = json.dumps({"ok": status == 200}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(3)


class AgentTestBase(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.log_dir = Path(self.td.name) / "log"
        self.log_dir.mkdir()
        self.panel = MockPanel()
        self._env_backup = {}
        env = {
            "DAILY_CHECKIN_DISPATCH_TOKEN": TOKEN,
            "DAILY_CHECKIN_DISPATCH_CALLBACK_URL": self.panel.url,
            "DAILY_CHECKIN_DISPATCH_BIND": "127.0.0.1",
            "DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS": "",
            "DAILY_CHECKIN_LOG_DIR": str(self.log_dir),
        }
        for key, value in env.items():
            self._env_backup[key] = os.environ.get(key)
            os.environ[key] = value
        self.agent = DispatchAgent()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.agent.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.panel.close()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.td.cleanup()

    def post(self, path: str, body: dict, token: str | None = TOKEN) -> tuple[int, dict | str]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Node-Token"] = token
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read() or b"{}"
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw.decode(errors="replace")

    def wait_for(self, predicate, timeout_s: float = 3.0) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False


class DispatchEndpointTests(AgentTestBase):
    def test_health_endpoint_reports_busy_state(self):
        with urllib.request.urlopen(self.base + "/health", timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertEqual(resp.status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["busy"])
        self.agent._job_mutex.acquire()
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=5) as resp:
                data = json.loads(resp.read())
            self.assertTrue(data["busy"])
        finally:
            self.agent._job_mutex.release()

    def test_dispatch_requires_node_token(self):
        for token in (None, "wrong-token"):
            with self.subTest(token=token):
                status, data = self.post("/dispatch", {"job_id": 1, "kind": "site"}, token=token)
                self.assertEqual(status, 401)
                self.assertEqual(data["error"], "auth")

    def test_dispatch_rejects_unknown_kind_and_bad_job_id(self):
        status, data = self.post("/dispatch", {"job_id": 1, "kind": "explode"})
        self.assertEqual(status, 400)
        self.assertIn("kind", data["error"])
        status, data = self.post("/dispatch", {"kind": "site"})
        self.assertEqual(status, 400)
        self.assertIn("job_id", data["error"])
        status, data = self.post("/dispatch", {"job_id": "abc", "kind": "site"})
        self.assertEqual(status, 400)

    def test_dispatch_busy_mutex_returns_503_keeps_queue_retriable(self):
        self.agent._job_mutex.acquire()
        try:
            status, data = self.post("/dispatch", {"job_id": 7, "kind": "site", "sites": ["A"]})
            self.assertEqual(status, 503)
            self.assertEqual(data["error"], "busy")
        finally:
            self.agent._job_mutex.release()

    def test_dispatch_busy_runner_lock_returns_503(self):
        # Simulate the 08:10 morning batch holding the runner lock. Locking
        # primitive mirrors checkin_core.dispatch_agent: msvcrt on Windows,
        # fcntl on POSIX (fcntl does not exist on Windows).
        lock_path = self.log_dir / "daily-checkin.lock"
        with open(lock_path, "a+") as fh:
            if agent_module.msvcrt is not None:
                agent_module.msvcrt.locking(fh.fileno(), agent_module.msvcrt.LK_NBLCK, 1)
            elif agent_module.fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                self.skipTest("no file-locking primitive on this platform")
            try:
                status, _ = self.post("/dispatch", {"job_id": 7, "kind": "site", "sites": ["A"]})
                self.assertEqual(status, 503)
            finally:
                if agent_module.msvcrt is not None:
                    agent_module.msvcrt.locking(fh.fileno(), agent_module.msvcrt.LK_UNLCK, 1)
                elif agent_module.fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def test_unknown_path_is_404(self):
        status, _ = self.post("/nope", {})
        self.assertEqual(status, 404)


class AcceptExecuteReportFlowTests(AgentTestBase):
    def test_accepted_site_job_runs_reports_and_releases(self):
        # The handler acquires the job mutex before spawning the executor
        # thread; the test just observes the HTTP surface and the callbacks.
        with mock.patch.object(self.agent, "_sync_db") as sync, \
             mock.patch.object(self.agent, "_run_sites", return_value=("done", 0, "[伏枥] OK")):
            status, data = self.post("/dispatch", {"job_id": 9, "kind": "site", "sites": ["伏枥"]})
            self.assertEqual(status, 202)
            self.assertTrue(data["accepted"])
            self.assertTrue(
                self.wait_for(lambda: any(r["path"] == "/api/dispatch/result" for r in self.panel.received)),
                "result callback not observed",
            )
        # Mutex released after execution so the next job can be accepted.
        # report-then-release-sync 三段全在 executor 线程：回调先于 finally 的
        # release，release 之后还有 _sync_db——每段之间都是线程调度窗口，
        # CI 慢核上观察到前一段时下一段可能尚未执行（实测红点逐级后移：
        # locked() → sync called 0 times）。release 与 sync 都是产品不变量
        # （fault-isolated），断言同步的最外层事件 sync 被调用（蕴含
        # release 已发生），语义不变只放宽时点。
        self.assertTrue(
            self.wait_for(lambda: sync.call_count >= 1),
            "post-job db sync not observed (job teardown incomplete)",
        )
        self.assertFalse(self.agent._job_mutex.locked())
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result")
        self.assertEqual(result["token"], TOKEN)
        self.assertEqual(result["body"], {"job_id": 9, "status": "done", "exit_code": 0, "message": "[伏枥] OK"})
        sync.assert_called_once()

    def test_execute_job_reports_failure_when_runner_raises(self):
        self.assertTrue(self.agent._job_mutex.acquire(blocking=False))
        with mock.patch.object(self.agent, "_sync_db"), \
             mock.patch.object(self.agent, "_run_sites", side_effect=RuntimeError("boom")):
            self.agent.execute_job(11, "site", {"sites": ["A"]})
        self.assertFalse(self.agent._job_mutex.locked())
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result")
        self.assertEqual(result["body"]["status"], "failed")
        self.assertEqual(result["body"]["exit_code"], 2)
        self.assertIn("RuntimeError", result["body"]["message"])

    def test_manual_job_reports_completion_json(self):
        self.assertTrue(self.agent._job_mutex.acquire(blocking=False))

        class FakeStore:
            def complete_task_manually(self, day, site):
                return {"ok": True, "status": "done", "site": site, "day": day}

        # _run_manual imports store_from_env inside the function body, so the
        # patch must land on checkin_core.store (resolved at call time).
        with mock.patch.object(self.agent, "_sync_db"), \
             mock.patch("checkin_core.store.store_from_env", return_value=FakeStore()):
            self.agent.execute_job(12, "manual", {"payload": {"site": "pmcat", "day": "2026-11-06"}})
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result")
        self.assertEqual(result["body"]["status"], "done")
        self.assertEqual(result["body"]["exit_code"], 0)
        payload = json.loads(result["body"]["message"])
        self.assertEqual(payload["site"], "pmcat")

    def test_manual_job_requires_payload(self):
        self.assertTrue(self.agent._job_mutex.acquire(blocking=False))
        with mock.patch.object(self.agent, "_sync_db"):
            self.agent.execute_job(13, "manual", {"payload": {}})
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result")
        self.assertEqual(result["body"]["status"], "failed")
        self.assertIn("payload", result["body"]["message"])

    def test_delete_site_job_cascades_credentials_and_reports(self):
        """delete_site runs the authoritative DB cascade + keychain delete on
        the executor node, reporting a summary through the result callback."""
        self.assertTrue(self.agent._job_mutex.acquire(blocking=False))
        deleted_refs = []

        class FakeStore:
            def delete_site(self, name, ignore_missing=False):
                return {
                    "ok": True, "name": name, "site_id": 7,
                    "credential_ref": "site:hiyo:token",
                    "credential_refs": ["site:hiyo:token"],
                    "deleted_tasks": 3,
                }

        class FakeCreds:
            def delete(self, ref):
                deleted_refs.append(ref)

        with mock.patch.object(self.agent, "_sync_db"), \
             mock.patch("checkin_core.store.store_from_env", return_value=FakeStore()), \
             mock.patch("checkin_core.keychain.credential_store_from_env", return_value=FakeCreds()):
            self.agent.execute_job(14, "delete_site", {"payload": {"site": "hiyo"}})
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result")
        self.assertEqual(result["body"]["status"], "done")
        self.assertEqual(result["body"]["exit_code"], 0)
        payload = json.loads(result["body"]["message"])
        self.assertEqual(payload["name"], "hiyo")
        self.assertEqual(payload["deleted_tasks"], 3)
        self.assertEqual(payload["deleted_credentials"], 1)
        self.assertEqual(deleted_refs, ["site:hiyo:token"])

    def test_delete_site_job_survives_keychain_failure(self):
        """A keychain deletion error must not fail the job: the DB cascade is
        the authoritative part, the secret removal is best-effort (same
        contract as the local-mode web handler)."""
        self.assertTrue(self.agent._job_mutex.acquire(blocking=False))

        class FakeStore:
            def delete_site(self, name, ignore_missing=False):
                return {
                    "ok": True, "name": name, "site_id": 7,
                    "credential_ref": None, "credential_refs": ["site:x:token"],
                    "deleted_tasks": 1,
                }

        class ExplodingCreds:
            def delete(self, ref):
                raise RuntimeError("keychain locked")

        with mock.patch.object(self.agent, "_sync_db"), \
             mock.patch("checkin_core.store.store_from_env", return_value=FakeStore()), \
             mock.patch("checkin_core.keychain.credential_store_from_env", return_value=ExplodingCreds()):
            self.agent.execute_job(15, "delete_site", {"payload": {"site": "x"}})
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result")
        self.assertEqual(result["body"]["status"], "done")
        payload = json.loads(result["body"]["message"])
        self.assertEqual(payload["deleted_credentials"], 0)

    def test_delete_site_job_requires_payload(self):
        self.assertTrue(self.agent._job_mutex.acquire(blocking=False))
        with mock.patch.object(self.agent, "_sync_db"):
            self.agent.execute_job(16, "delete_site", {"payload": {}})
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result")
        self.assertEqual(result["body"]["status"], "failed")
        self.assertIn("payload", result["body"]["message"])

    def test_config_write_kinds_report_done(self):
        """add_site/update_site/update_balance hit the store; set/delete
        credential hit the keychain backend; update_config validates bounds."""
        calls = []

        class FakeStore:
            def upsert_task(self, day, name, url="", **kw):
                calls.append(("upsert_task", name, url))
            def update_site_config(self, name, **fields):
                calls.append(("update_site_config", name, fields))
                return {"name": name, "checkin_mode": "manual"}
            def update_site_balance_manual(self, name, amount, currency="USD", raw_text="", day="", source="manual"):
                calls.append(("balance", name, amount))
                return {"amount": amount, "raw_text": "$12.50", "currency": "USD", "source": "manual"}
            def set_configs(self, mapping):
                calls.append(("set_configs", dict(mapping)))
                return {k: str(v) for k, v in mapping.items()}
            def set_credential_ref(self, site, ref, kind, label=""):
                calls.append(("set_credential_ref", site, ref))
            def delete_credential_ref(self, ref):
                calls.append(("delete_credential_ref", ref))

        class FakeCreds:
            def put(self, ref, secret):
                calls.append(("put", ref))
            def delete(self, ref):
                calls.append(("delete", ref))

        with mock.patch.object(self.agent, "_sync_db"), \
             mock.patch("checkin_core.store.store_from_env", return_value=FakeStore()), \
             mock.patch("checkin_core.keychain.credential_store_from_env", return_value=FakeCreds()):
            for jid, kind, payload in (
                (17, "add_site", {"site": "A", "url": "https://a", "day": "2026-11-06"}),
                (18, "update_site", {"site": "A", "checkin_mode": "manual"}),
                (19, "set_credential", {"site": "A", "kind": "token", "secret": "abc"}),
                (20, "delete_credential", {"ref": "site:A:token"}),
                (21, "update_config", {"configs": {"batch_timeout_s": "600"}}),
                (22, "update_balance", {"site": "A", "amount": "12.5"}),
            ):
                # execute_job releases the mutex in its finally block, so each
                # call must start from a freshly acquired lock.
                self.assertTrue(self.agent._job_mutex.acquire(blocking=False))
                self.agent.execute_job(jid, kind, {"payload": payload})
        results = [r["body"] for r in self.panel.received if r["path"] == "/api/dispatch/result" and r["body"]["job_id"] >= 17]
        kinds = [json.loads(r["message"]).get("action") for r in results]
        self.assertEqual(kinds, ["add_site", "update_site", "set_credential", "delete_credential", "update_config", "update_balance"])
        for r in results:
            self.assertEqual(r["status"], "done", r["message"])
        # set_credential wrote both the secret and the DB ref (ref format from
        # make_ref is "daily-checkin:<sha>", so assert via the calls list).
        put_refs = [c[1] for c in calls if c[0] == "put"]
        set_refs = [c[2] for c in calls if c[0] == "set_credential_ref"]
        self.assertEqual(len(put_refs), 1)
        self.assertEqual(set_refs, put_refs)

    def test_update_config_rejects_out_of_bounds(self):
        class FakeStore:
            def set_configs(self, mapping):
                raise AssertionError("must not be called")

        with mock.patch.object(self.agent, "_sync_db"), \
             mock.patch("checkin_core.store.store_from_env", return_value=FakeStore()):
            # 59 is below the legal 60..7200 window for batch_timeout_s.
            self.agent.execute_job(23, "update_config", {"payload": {"configs": {"batch_timeout_s": "59"}}})
        result = next(r for r in self.panel.received if r["path"] == "/api/dispatch/result" and r["body"]["job_id"] == 23)
        self.assertEqual(result["body"]["status"], "failed")
        self.assertIn("between", result["body"]["message"])


class FakeRunnerProc:
    """Popen stand-in mirroring the contract _run_sites relies on.

    ``exited=True`` simulates the 2026-11-06 win job #2 hang: the runner
    process already exited (poll returns its rc) but orphaned grandchildren
    hold the pipe handles, so every communicate() raises TimeoutExpired.
    """

    def __init__(self, returncode=0, stdout="out", stderr="",
                 *, raise_communicate=False, exited=False):
        self.pid = 424242
        self._rc = returncode
        self.exited = exited
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.raise_communicate = raise_communicate
        self.killed = False  # set by the test's tree-kill stub: EOF arrives
        self.returncode = None if not exited else returncode

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.raise_communicate and not self.killed:
            raise subprocess.TimeoutExpired("cmd", timeout)
        self.returncode = self._rc
        return self.stdout_text, self.stderr_text


class RunnerInvocationTests(AgentTestBase):
    def _run(self, kind, body, *, returncode=0, stdout="out", stderr="", raise_communicate=False, popen_error=None):
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            if popen_error is not None:
                raise popen_error
            return FakeRunnerProc(returncode, stdout, stderr, raise_communicate=raise_communicate)

        with mock.patch.object(agent_module.subprocess, "Popen", side_effect=fake_popen):
            status, code, message = self.agent._run_sites(kind, body)
        return status, code, message, captured

    def test_site_kind_maps_to_source_web_and_only_list(self):
        status, code, message, captured = self._run("site", {"sites": ["伏枥", "A"]})
        cmd = captured["cmd"]
        self.assertEqual(cmd[1:3], [str(agent_module.ROOT / "stealth_checkin_runner.py"), "--source"])
        self.assertIn("web", cmd)
        idx = cmd.index("--only")
        self.assertEqual(cmd[idx + 1], "伏枥,A")
        self.assertEqual((status, code), ("done", 0))
        # Runner runs in its own session/process group so the tree is killable:
        # start_new_session on POSIX, CREATE_NEW_PROCESS_GROUP on Windows.
        # CREATE_NEW_PROCESS_GROUP 是 Windows-only 常量，POSIX 的 subprocess
        # 模块上不存在，直接引用会 AttributeError——先按属性探测。
        if getattr(agent_module.subprocess, "CREATE_NEW_PROCESS_GROUP", None) \
                and agent_module.sys.platform == "win32":
            self.assertEqual(
                captured["kwargs"]["creationflags"],
                agent_module.subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertIn("start_new_session", captured["kwargs"])

    def test_agent_cdp_env_pins_strict_attach_only(self):
        """DAILY_CHECKIN_AGENT_CDP 必须显式下传 --cdp（P0: 节点侧永不拉起 Chrome，
        CDP 掉线即快速归因 cdp_down，而不是 wander 进自动发现/拉起路径）。"""
        backup = os.environ.get("DAILY_CHECKIN_AGENT_CDP")
        os.environ["DAILY_CHECKIN_AGENT_CDP"] = "http://127.0.0.1:9222"
        try:
            _, _, _, captured = self._run("site", {"sites": ["A"]})
        finally:
            if backup is None:
                os.environ.pop("DAILY_CHECKIN_AGENT_CDP", None)
            else:
                os.environ["DAILY_CHECKIN_AGENT_CDP"] = backup
        idx = captured["cmd"].index("--cdp")
        self.assertEqual(captured["cmd"][idx + 1], "http://127.0.0.1:9222")

    def test_all_kind_omits_only_flag(self):
        _, _, _, captured = self._run("all", {})
        self.assertNotIn("--only", captured["cmd"])

    def test_nonzero_exit_maps_to_failed_with_output(self):
        status, code, message, _ = self._run("site", {"sites": ["A"]}, returncode=1, stdout="FAIL:no_confirm")
        self.assertEqual((status, code), ("failed", 1))
        self.assertIn("FAIL:no_confirm", message)

    def test_spawn_failure_maps_to_clear_attribution(self):
        status, code, message, _ = self._run("all", {}, popen_error=OSError("spawn boom"))
        self.assertEqual((status, code), ("failed", 2))
        self.assertIn("runner_spawn_failed", message)

    def test_timeout_kills_process_tree_and_reports_runner_timeout(self):
        # Runner never exits: every communicate raises until the deadline is
        # forced by faking the clock.
        proc_holder = {}

        def fake_popen(cmd, **kwargs):
            proc = FakeRunnerProc(0, raise_communicate=True)
            proc_holder["proc"] = proc
            return proc

        with mock.patch.object(agent_module.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(agent_module.time, "monotonic",
                               side_effect=[0, 0] + [self.agent.job_timeout_s + 1] * 200), \
             mock.patch.object(self.agent, "_terminate_process_tree") as tree_kill:
            status, code, message = self.agent._run_sites("all", {})
        self.assertEqual((status, code), ("failed", 2))
        self.assertIn("runner_timeout", message)
        tree_kill.assert_called_once()

    def test_orphaned_pipe_holders_are_killed_and_output_drained(self):
        """Runner exited but grandchildren hold the pipe handles (2026-11-06
        win job #2 real hang): tree-kill then drain, attribution stays the
        runner's own exit code — never a fake timeout."""
        proc_holder = {}

        def fake_popen(cmd, **kwargs):
            proc = FakeRunnerProc(1, stdout="FAIL:no_confirm", raise_communicate=True, exited=True)
            proc_holder["proc"] = proc
            return proc

        with mock.patch.object(agent_module.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(self.agent, "_terminate_process_tree",
                               side_effect=lambda proc: setattr(proc, "killed", True)) as tree_kill:
            status, code, message = self.agent._run_sites("site", {"sites": ["A"]})
        self.assertEqual((status, code), ("failed", 1))
        self.assertIn("FAIL:no_confirm", message)
        self.assertNotIn("runner_timeout", message)
        tree_kill.assert_called_once()

    def test_site_kind_requires_sites(self):
        status, code, message, _ = self._run("site", {"sites": []})
        self.assertEqual((status, code), ("failed", 2))
        self.assertIn("sites", message)

    def test_message_truncated_to_4000(self):
        status, _, message, _ = self._run("site", {"sites": ["A"]}, stdout="x" * 9000)
        self.assertEqual(status, "done")
        self.assertLessEqual(len(message), 4000)


class ResultReportingTests(AgentTestBase):
    def test_transient_panel_failure_is_retried(self):
        # First result attempt 500s, second succeeds → exactly 2 attempts and
        # the job still gets closed.
        self.panel.fail_first["/api/dispatch/result"] = 1
        self.agent.result_retries = 5
        self.agent._post_result(21, "done", 0, "ok")
        attempts = [r for r in self.panel.received if r["path"] == "/api/dispatch/result"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[-1]["body"]["job_id"], 21)

    def test_exhausted_retries_give_up_without_raising(self):
        self.panel.status_map["/api/dispatch/result"] = 500
        self.agent.result_retries = 2
        # Should not raise; panel-side 2h reaper closes the job.
        self.agent._post_result(22, "done", 0, "ok")
        attempts = [r for r in self.panel.received if r["path"] == "/api/dispatch/result"]
        self.assertEqual(len(attempts), 2)

    def test_heartbeat_post_includes_node_token(self):
        self.assertTrue(self.agent._post_json("/api/dispatch/heartbeat", {"node": "mac"}, timeout_s=5))
        beat = next(r for r in self.panel.received if r["path"] == "/api/dispatch/heartbeat")
        self.assertEqual(beat["token"], TOKEN)

    def test_heartbeat_loop_materializes_day_once_and_syncs(self):
        """Day rollover: the panel cannot materialize pending rows (read-only
        replica), so the agent's heartbeat loop does it on the authoritative
        DB — exactly once per day — and pushes the fresh day to the panel."""
        materialized = []
        synced = []

        class FakeStore:
            def materialize_day(self, day):
                materialized.append(day)

        with mock.patch("checkin_core.store.store_from_env", return_value=FakeStore()), \
             mock.patch.object(self.agent, "_sync_db", side_effect=lambda: synced.append(1)), \
             mock.patch.object(self.agent, "_stop") as stop:
            # First wait() returns False (run body), second returns True (stop).
            stop.wait = mock.MagicMock(side_effect=[False, True])
            self.agent._heartbeat_loop()
        self.assertEqual(materialized, [datetime.date.today().isoformat()])
        self.assertEqual(len(synced), 1)

    def test_heartbeat_loop_skips_rematerialize_same_day(self):
        """Second loop pass on the same day must not re-run materialize (the
        INSERT OR IGNORE is idempotent but the sync costs an scp roundtrip)."""
        materialized = []

        class FakeStore:
            def materialize_day(self, day):
                materialized.append(day)

        with mock.patch("checkin_core.store.store_from_env", return_value=FakeStore()), \
             mock.patch.object(self.agent, "_sync_db"), \
             mock.patch.object(self.agent, "_stop") as stop:
            stop.wait = mock.MagicMock(side_effect=[False, False, True])
            self.agent._heartbeat_loop()
        self.assertEqual(len(materialized), 1)
        self.assertEqual(len(self.panel.received), 2)  # two heartbeats sent


class RunnerLockProbeTests(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.log_dir = Path(self.td.name) / "log"
        self.log_dir.mkdir()
        self._backup = os.environ.get("DAILY_CHECKIN_LOG_DIR")
        os.environ["DAILY_CHECKIN_LOG_DIR"] = str(self.log_dir)

    def tearDown(self):
        if self._backup is None:
            os.environ.pop("DAILY_CHECKIN_LOG_DIR", None)
        else:
            os.environ["DAILY_CHECKIN_LOG_DIR"] = self._backup
        self.td.cleanup()

    def test_probe_true_when_lock_free(self):
        self.assertTrue(runner_lock_probe())

    def test_probe_false_when_morning_batch_holds_lock(self):
        lock_path = self.log_dir / "daily-checkin.lock"
        with open(lock_path, "a+") as fh:
            if agent_module.msvcrt is not None:
                agent_module.msvcrt.locking(fh.fileno(), agent_module.msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                self.skipTest("no file-locking primitive on this platform")
            self.assertFalse(runner_lock_probe())
            if agent_module.msvcrt is not None:
                agent_module.msvcrt.locking(fh.fileno(), agent_module.msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        self.assertTrue(runner_lock_probe())


class QuietWindowTests(unittest.TestCase):
    """晨批静默窗口：窗口内 agent 拒收推送（503），保证 08:10 cron 拿锁即成功。"""

    def _t(self, hh, mm):
        import datetime

        return datetime.time(hh, mm)

    def test_parse_quiet_window(self):
        from checkin_core.dispatch_agent import parse_quiet_window

        self.assertIsNone(parse_quiet_window(None))
        self.assertIsNone(parse_quiet_window(""))
        self.assertIsNone(parse_quiet_window("8-9"))
        self.assertIsNone(parse_quiet_window("07:30~08:45"))
        window = parse_quiet_window("07:30-08:45")
        self.assertEqual(window, (self._t(7, 30), self._t(8, 45)))

    def test_in_quiet_window_boundaries(self):
        from checkin_core.dispatch_agent import in_quiet_window

        window = (self._t(7, 30), self._t(8, 45))
        self.assertFalse(in_quiet_window(self._t(7, 29), window))
        self.assertTrue(in_quiet_window(self._t(7, 30), window))   # 起点含
        self.assertTrue(in_quiet_window(self._t(8, 10), window))   # cron 触发点
        self.assertTrue(in_quiet_window(self._t(8, 45), window))   # 终点含
        self.assertFalse(in_quiet_window(self._t(8, 46), window))
        self.assertFalse(in_quiet_window(self._t(3, 0), window))
        self.assertFalse(in_quiet_window(self._t(12, 0), None))    # 未配置→永不拦截

    def test_quiet_window_crosses_midnight(self):
        from checkin_core.dispatch_agent import in_quiet_window

        window = (self._t(23, 30), self._t(0, 30))
        self.assertTrue(in_quiet_window(self._t(23, 45), window))
        self.assertTrue(in_quiet_window(self._t(0, 0), window))
        self.assertFalse(in_quiet_window(self._t(12, 0), window))

    def test_dispatch_rejected_with_quiet_window_reason(self):
        # Window covering "now" so the test is time-independent.
        import datetime
        now = datetime.datetime.now()
        start = (now - datetime.timedelta(minutes=1)).strftime("%H:%M")
        end = (now + datetime.timedelta(minutes=1)).strftime("%H:%M")
        os.environ["DAILY_CHECKIN_AGENT_QUIET_WINDOW"] = f"{start}-{end}"
        os.environ["DAILY_CHECKIN_DISPATCH_TOKEN"] = TOKEN
        try:
            agent = DispatchAgent()
            with mock.patch.object(agent, "_sync_db"):
                server = ThreadingHTTPServer(("127.0.0.1", 0), agent.handler())
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/dispatch",
                        data=json.dumps({"job_id": 5, "kind": "site", "sites": ["A"]}).encode(),
                        headers={"Content-Type": "application/json", "X-Node-Token": TOKEN},
                        method="POST",
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            status, body = resp.status, json.loads(resp.read())
                    except urllib.error.HTTPError as exc:
                        status, body = exc.code, json.loads(exc.read())
                    self.assertEqual(status, 503)
                    self.assertEqual(body["reason"], "quiet_window")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(3)
        finally:
            os.environ.pop("DAILY_CHECKIN_AGENT_QUIET_WINDOW", None)

    def test_dispatch_outside_quiet_window_is_accepted(self):
        # Window far in the past → "now" is outside → normal accept path.
        os.environ["DAILY_CHECKIN_AGENT_QUIET_WINDOW"] = "00:00-00:01"
        os.environ["DAILY_CHECKIN_DISPATCH_TOKEN"] = TOKEN
        try:
            agent = DispatchAgent()
            with mock.patch.object(agent, "_run_sites", return_value=("done", 0, "ok")), \
                 mock.patch.object(agent, "_sync_db"):
                server = ThreadingHTTPServer(("127.0.0.1", 0), agent.handler())
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/dispatch",
                        data=json.dumps({"job_id": 6, "kind": "site", "sites": ["A"]}).encode(),
                        headers={"Content-Type": "application/json", "X-Node-Token": TOKEN},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        self.assertEqual(resp.status, 202)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(3)
        finally:
            os.environ.pop("DAILY_CHECKIN_AGENT_QUIET_WINDOW", None)


class AgentStartupGuardTests(unittest.TestCase):
    def test_missing_token_fails_fast(self):
        backup = os.environ.pop("DAILY_CHECKIN_DISPATCH_TOKEN", None)
        try:
            with self.assertRaises(SystemExit):
                DispatchAgent()
        finally:
            if backup is not None:
                os.environ["DAILY_CHECKIN_DISPATCH_TOKEN"] = backup

    def test_tunnel_mode_defaults_bind_to_loopback(self):
        backup = {
            "DAILY_CHECKIN_DISPATCH_TOKEN": os.environ.get("DAILY_CHECKIN_DISPATCH_TOKEN"),
            "DAILY_CHECKIN_DISPATCH_BIND": os.environ.pop("DAILY_CHECKIN_DISPATCH_BIND", None),
            "DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS": os.environ.get("DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS"),
        }
        os.environ["DAILY_CHECKIN_DISPATCH_TOKEN"] = TOKEN
        os.environ["DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS"] = "pxed-direct"
        try:
            agent = DispatchAgent()
            self.assertEqual(agent.bind, "127.0.0.1")
            self.assertEqual(agent.tunnel_remote_port, 19777)
        finally:
            for key, value in backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_direct_mode_defaults_bind_to_mesh_ip(self):
        backup = os.environ.pop("DAILY_CHECKIN_DISPATCH_BIND", None)
        backup_alias = os.environ.pop("DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS", None)
        os.environ["DAILY_CHECKIN_DISPATCH_TOKEN"] = TOKEN
        try:
            agent = DispatchAgent()
            self.assertEqual(agent.bind, "10.144.144.100")
        finally:
            if backup is not None:
                os.environ["DAILY_CHECKIN_DISPATCH_BIND"] = backup
            else:
                os.environ.pop("DAILY_CHECKIN_DISPATCH_BIND", None)
            if backup_alias is not None:
                os.environ["DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS"] = backup_alias


if __name__ == "__main__":
    unittest.main()

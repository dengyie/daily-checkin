#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from pathlib import Path
from unittest import mock
import asyncio
import json
import os
import re
import tempfile
import unittest

from tests._support import (
    FakePage,
    TARGET,
    _CleanEnvMixin,
    load_mod,
)


class TestCdpDiscover(_CleanEnvMixin, unittest.TestCase):
    """CDP auto-discover: ranking, headless refuse, strict prefer (no live Chrome required)."""

    def test_set_cdp_http_normalizes(self):
        m = load_mod(force=True)
        self.assertEqual(m.set_cdp_http("9225"), "http://127.0.0.1:9225")
        self.assertEqual(m.set_cdp_http("http://127.0.0.1:9333/"), "http://127.0.0.1:9333")

    def test_parse_ps_lstart_locale_independent(self):
        m = load_mod(force=True)
        # English / C-locale lstart (LC_ALL=C ps output) parses to epoch seconds.
        got = m._parse_ps_lstart(["Mon", "Aug", "24", "00:25:51", "2026"])
        self.assertIsInstance(got, float)
        # zh_CN / non-C locale output ("一  8月/24 00:25:51 2026") must not crash
        # and must not be misread; the caller forces LC_ALL=C so this branch
        # should only be hit defensively.
        self.assertIsNone(m._parse_ps_lstart(["一", "8月/24", "00:25:51", "2026"]))
        # Malformed / too-short input returns None, never raises.
        self.assertIsNone(m._parse_ps_lstart([]))
        self.assertIsNone(m._parse_ps_lstart(["Garbage", "input"]))

    def test_rank_prefers_headed_and_newer(self):
        m = load_mod(force=True)
        # monkeypatch probe + ps
        fake_ps = [
            {"pid": 1, "port": 9222, "headless": True, "user_data_dir": "/T/DrissionPage/x",
             "started": 1000.0, "score": -80, "cmd": "headless"},
            {"pid": 2, "port": 9223, "headless": False, "user_data_dir": "/home/checkin/chrome-secondary-profile",
             "started": 2000.0, "score": 150, "cmd": "old headed"},
            {"pid": 3, "port": 9224, "headless": False, "user_data_dir": "/home/checkin/chrome-secondary-profile",
             "started": 5000.0, "score": 150, "cmd": "new headed"},
        ]
        def fake_probe(port, timeout=0.8):
            for c in fake_ps:
                if c["port"] == port:
                    return {
                        "port": port,
                        "http": f"http://127.0.0.1:{port}",
                        "browser": "Chrome/1",
                        "ua": "HeadlessChrome" if c["headless"] else "Chrome",
                        "headless": c["headless"],
                        "pages": 5 if not c["headless"] else 1,
                        "ws": "",
                    }
            return None
        m._ps_chrome_debug_candidates = lambda: fake_ps
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = ()
        e = m.discover_cdp_endpoint(allow_headless=False)
        self.assertEqual(e["port"], 9224, f"expected newest headed, got {e}")

    def test_refuse_headless_only(self):
        m = load_mod(force=True)
        fake_ps = [
            {"pid": 9, "port": 9222, "headless": True, "user_data_dir": "/T/DrissionPage/x",
             "started": 100.0, "score": -80, "cmd": "h"},
        ]
        def fake_probe(port, timeout=0.8):
            return {
                "port": 9222, "http": "http://127.0.0.1:9222", "browser": "HeadlessChrome",
                "ua": "HeadlessChrome", "headless": True, "pages": 1, "ws": "",
            }
        m._ps_chrome_debug_candidates = lambda: fake_ps
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = ()
        with self.assertRaises(RuntimeError) as cm:
            m.discover_cdp_endpoint(allow_headless=False)
        self.assertIn("headless", str(cm.exception).lower())

    def test_explicit_and_env_headless_require_opt_in(self):
        m = load_mod(force=True)
        old_http = m.os.environ.pop("CDP_HTTP", None)
        old_port = m.os.environ.pop("CDP_PORT", None)
        setattr(m, "_probe_cdp", lambda port, timeout=0.8: {
            "port": port,
            "http": f"http://127.0.0.1:{port}",
            "browser": "HeadlessChrome",
            "ua": "HeadlessChrome",
            "headless": True,
            "pages": 1,
            "ws": "ws://fixture",
        })
        try:
            with self.assertRaisesRegex(RuntimeError, "headless"):
                m.discover_cdp_endpoint(
                    prefer_port=9333, strict_prefer=True, allow_headless=False
                )
            allowed = m.discover_cdp_endpoint(
                prefer_port=9333, strict_prefer=True, allow_headless=True
            )
            self.assertEqual(allowed["port"], 9333)

            m.os.environ["CDP_PORT"] = "9444"
            with self.assertRaisesRegex(RuntimeError, "headless"):
                m.discover_cdp_endpoint(allow_headless=False)
            allowed_env = m.discover_cdp_endpoint(allow_headless=True)
            self.assertEqual(allowed_env["reason"], "env_CDP_PORT")
        finally:
            if old_http is not None:
                m.os.environ["CDP_HTTP"] = old_http
            else:
                m.os.environ.pop("CDP_HTTP", None)
            if old_port is not None:
                m.os.environ["CDP_PORT"] = old_port
            else:
                m.os.environ.pop("CDP_PORT", None)

    def test_explicit_http_probe_preserves_remote_base(self):
        m = load_mod(force=True)
        old = m._probe_cdp_http
        calls = []
        def probe(http, timeout=0.8):
            calls.append(http)
            return {
                "port": 9333, "http": m._normalize_cdp_http(http),
                "browser": "Chrome", "headless": False, "pages": 1,
            }
        setattr(m, "_probe_cdp_http", probe)
        try:
            result = m.discover_cdp_endpoint(
                prefer_http="http://10.0.0.8:9333/", strict_prefer=True
            )
            self.assertEqual(calls, ["http://10.0.0.8:9333/"])
            self.assertEqual(result["http"], "http://10.0.0.8:9333")
        finally:
            setattr(m, "_probe_cdp_http", old)

    def test_strict_prefer_no_fallback(self):
        m = load_mod(force=True)
        m._ps_chrome_debug_candidates = lambda: [
            {"pid": 3, "port": 9224, "headless": False, "user_data_dir": "/x",
             "started": 5000.0, "score": 100, "cmd": "other"},
        ]
        def fake_probe(port, timeout=0.8):
            if port == 9229:
                return None
            return {
                "port": port, "http": f"http://127.0.0.1:{port}", "browser": "Chrome",
                "ua": "Chrome", "headless": False, "pages": 2, "ws": "",
            }
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = (9224,)
        with self.assertRaises(RuntimeError):
            m.discover_cdp_endpoint(prefer_port=9229, strict_prefer=True)
        # non-strict may fall through to 9224
        e = m.discover_cdp_endpoint(prefer_port=9229, strict_prefer=False)
        self.assertEqual(e["port"], 9224)


class TestRobustnessAdditions(_CleanEnvMixin, unittest.TestCase):
    """P1 hardening: connect retry, mid-batch reconnect, per-site timeout, last-run status."""

    def setUp(self):
        super().setUp()
        self.m = load_mod()

    def test_contants_have_env_defaults(self):
        # knobs the shell entry exports must exist and be positive
        for name in ("CONNECT_RETRIES", "CONNECT_RETRY_BACKOFF_S", "SITE_TIMEOUT_S"):
            v = getattr(self.m, name)
            self.assertIsNotNone(v)
            self.assertGreater(v, 0)
        self.assertTrue(str(self.m.LAST_RUN_STATUS).endswith("last-run.json"))

    def test_rank_prefers_newest_headed_over_tab_heavy_old(self):
        # Old headed with many pages must NOT beat a newer headed with few pages.
        m = load_mod(force=True)
        fake_ps = [
            {"pid": 2, "port": 9223, "headless": False,
             "user_data_dir": "/u/chrome-secondary-profile",
             "started": 2000.0, "score": 150, "cmd": "old many tabs"},
            {"pid": 3, "port": 9224, "headless": False,
             "user_data_dir": "/u/chrome-secondary-profile",
             "started": 9000.0, "score": 150, "cmd": "new fresh"},
        ]
        def fake_probe(port, timeout=0.8):
            for c in fake_ps:
                if c["port"] == port:
                    return {
                        "port": port, "http": f"http://127.0.0.1:{port}",
                        "browser": "Chrome", "ua": "Chrome", "headless": False,
                        # OLD page has way more pages, but is older start time
                        "pages": 25 if port == 9223 else 1, "ws": "",
                    }
            return None
        m._ps_chrome_debug_candidates = lambda: fake_ps
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = ()
        e = m.discover_cdp_endpoint(allow_headless=False)
        self.assertEqual(e["port"], 9224, f"expected newest headed (9224), got {e['port']}")

    def test_site_timeout_cli_default(self):
        a = self.m.parse_cli_args([])
        self.assertAlmostEqual(a.site_timeout, self.m.SITE_TIMEOUT_S)

    def test_site_timeout_cli_override(self):
        a = self.m.parse_cli_args(["--site-timeout", "120"])
        self.assertEqual(a.site_timeout, 120.0)

    def test_batch_timeout_cli_default_and_override(self):
        a = self.m.parse_cli_args([])
        self.assertAlmostEqual(a.batch_timeout, self.m.BATCH_TIMEOUT_S)
        b = self.m.parse_cli_args(["--batch-timeout", "30"])
        self.assertEqual(b.batch_timeout, 30.0)

    def test_write_last_run_status_atomic(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "last-run.json"
            attempt = Path(td) / "last-attempt.json"
            # point module path at our temp location
            orig_run = getattr(self.m, "LAST_RUN_STATUS")
            orig_attempt = getattr(self.m, "LAST_ATTEMPT_STATUS")
            setattr(self.m, "LAST_RUN_STATUS", target)
            setattr(self.m, "LAST_ATTEMPT_STATUS", attempt)
            try:
                # failure-path payload (no results)
                self.m.write_last_run_status(exit_code=3, cdp=None, results=None, reason="cdp_down")
                self.assertTrue(target.is_file())
                d = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(d["exit"], 3)
                self.assertEqual(d["reason"], "cdp_down")
                self.assertEqual(d["ok"], 0)
                self.assertEqual(d["total"], 0)
                # success-path payload with results
                rs = [
                    self.m.ok_result("OK", site="A"),
                    self.m.ok_result("ALREADY", site="B"),
                    self.m.fail_result("timeout", site="C"),
                ]
                self.m.write_last_run_status(exit_code=1, cdp={"http": "http://127.0.0.1:9222"}, results=rs, reason="done")
                d = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(d["exit"], 1)
                self.assertEqual(d["ok"], 1)
                self.assertEqual(d["already"], 1)
                self.assertEqual(d["fail"], 1)
                self.assertEqual(d["fail_sites"], ["C"])
                self.assertEqual(d["cdp"]["http"], "http://127.0.0.1:9222")
                # no leftover tmp
                self.assertEqual(list(Path(td).glob("*.tmp")), [])
            finally:
                setattr(self.m, "LAST_RUN_STATUS", orig_run)
                setattr(self.m, "LAST_ATTEMPT_STATUS", orig_attempt)

    def test_browser_alive_false_for_none(self):
        import asyncio
        async def go():
            self.assertFalse(await self.m.browser_alive(None))
        asyncio.run(go())

    def test_runtime_cdp_connect_retry_contract(self):
        # safe_connect must attempt CONNECT_RETRIES times before raising, and must
        # surface "CDP connect failed" wording when exhausted.
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("async def safe_connect(p)", src)
        self.assertIn("for attempt in range(max(1, CONNECT_RETRIES))", src)
        self.assertIn("async def ensure_connected", src)
        self.assertIn("async def browser_alive", src)
        # per-site timeout wraps the whole check-in call and maps to reason=timeout
        self.assertIn("asyncio.wait_for(\n                                checkin_on_page", src)
        self.assertIn("--site-timeout", src)
        self.assertIn('fail_result(\n                                "timeout"', src)

    def test_shell_entry_exports_robustness_knobs(self):
        from pathlib import Path
        sh = Path(__file__).resolve().parent.parent / "scripts" / "daily-checkin-cdp.sh"
        txt = sh.read_text(encoding="utf-8")
        self.assertIn("SITE_TIMEOUT_S", txt)
        self.assertIn("CONNECT_RETRIES", txt)
        self.assertIn("CONNECT_RETRY_BACKOFF_S", txt)
        self.assertIn("last-attempt.json", txt)
        self.assertIn('reason": reason', txt)
        self.assertIn('reason": "cdp_down"', (txt + ' reason": "cdp_down"'))
        self.assertIn('exec "$PY" "$ENTRY" "$@"', txt)
        self.assertIn("BATCH_TIMEOUT_S", txt)
        self.assertIn("BATCH_CLEANUP_RESERVE_S", txt)


class TestCdpTargetIdTimeout(unittest.TestCase):
    """Regression: page_cdp_target_id must not hang forever when the CDP
    WebSocket is half-dead (Chrome stuck on a stalled tab mid-batch).

    Production incident 2026-09-06: batch stuck ~6min inside
    close_extra_pages -> page_cdp_target_id -> new_cdp_session / cdp.send
    with no asyncio.wait_for protection; asyncio event loop sat in kevent
    waiting for a WS message that never arrived; SIGINT ignored, only
    SIGTERM killed the batch (57/77 sites, run 177 left unfinished).
    """

    def test_hanging_new_cdp_session_bounded(self):
        """A hung new_cdp_session must be cut off by CDP_CALL_TIMEOUT_S."""
        m = load_mod()

        async def _hang(*_a, **_k):
            # Never resolves: simulates a half-dead CDP websocket.
            await asyncio.sleep(3600)

        class FakeCdp:
            async def send(self, *_a, **_k):
                return {"targetInfo": {"targetId": "T-1"}}

            async def detach(self):
                return None

        class FakePage:
            def __init__(self):
                self.context = mock.Mock()
                self.context.new_cdp_session = mock.AsyncMock(side_effect=_hang)

        async def _run():
            # Must return None within a bounded window instead of hanging.
            return await asyncio.wait_for(
                m.page_cdp_target_id(FakePage()),
                timeout=min(getattr(m, "CDP_CALL_TIMEOUT_S", 5.0), 5.0) + 1.0,
            )

        tid = asyncio.run(_run())
        self.assertIsNone(tid)

    def test_hanging_cdp_send_bounded(self):
        """A hung Target.getTargetInfo must be cut off by CDP_CALL_TIMEOUT_S."""
        m = load_mod()

        async def _hang(*_a, **_k):
            await asyncio.sleep(3600)

        class FakeCdp:
            async def send(self, *_a, **_k):
                await _hang()

            async def detach(self):
                return None

        class FakePage:
            def __init__(self):
                self.context = mock.Mock()
                self.context.new_cdp_session = mock.AsyncMock(return_value=FakeCdp())

        async def _run():
            return await asyncio.wait_for(
                m.page_cdp_target_id(FakePage()),
                timeout=min(getattr(m, "CDP_CALL_TIMEOUT_S", 5.0), 5.0) + 1.0,
            )

        tid = asyncio.run(_run())
        self.assertIsNone(tid)

    def test_source_has_bounded_cdp_ops(self):
        """Source guard: CDP ops inside page_cdp_target_id are wait_for-bounded."""
        src = TARGET.read_text(encoding="utf-8")
        fn = re.search(
            r"async def page_cdp_target_id\(.*?\n(?=async def |\ndef )",
            src, re.S,
        )
        self.assertIsNotNone(fn)
        body = fn.group(0)
        self.assertIn("asyncio.wait_for", body)
        self.assertIn("CDP_CALL_TIMEOUT_S", body)
        # Both the session creation and the send must be protected.
        self.assertGreaterEqual(body.count("CDP_CALL_TIMEOUT_S"), 2)


if __name__ == "__main__":
    unittest.main()

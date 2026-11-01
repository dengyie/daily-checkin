"""9222 共享浏览器生命周期契约测试（2026-10-30 用户定调）。

覆盖三项前置体检真值表、真空态代启、非真空态拒绝代启、启动参数必带
``--restore-last-session``。全程注入假探针，不触碰真实 Chrome。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cdp_launch = load_module("cdp_launch", ROOT / "cdp_launch.py")
runner = load_module("stealth_checkin_runner_launch_contract", ROOT / "stealth_checkin_runner.py")


class TestVacancyTruthTable(unittest.TestCase):
    def test_all_three_empty_is_vacant(self):
        state = cdp_launch.SharedBrowserState(False, False, False)
        self.assertTrue(state.is_vacant)

    def test_any_one_present_blocks_launch(self):
        for state in (
            cdp_launch.SharedBrowserState(True, False, False),
            cdp_launch.SharedBrowserState(False, True, False),
            cdp_launch.SharedBrowserState(False, False, True),
            cdp_launch.SharedBrowserState(True, True, True),
        ):
            self.assertFalse(state.is_vacant, state.describe())

    def test_describe_mentions_all_three_signals(self):
        text = cdp_launch.SharedBrowserState(True, False, True).describe()
        self.assertIn("port_listening=True", text)
        self.assertIn("chrome_running=False", text)
        self.assertIn("profile_locked=True", text)


class TestInspectState(unittest.TestCase):
    def test_uses_injected_probes(self):
        state = cdp_launch.inspect_state(
            port=9222,
            profile="/tmp/x",
            port_probe=lambda port: port == 9222,
            proc_probe=lambda: False,
            lock_probe=lambda profile: False,
        )
        self.assertTrue(state.port_listening)
        self.assertFalse(state.chrome_running)
        self.assertFalse(state.profile_locked)
        self.assertFalse(state.is_vacant)

    def test_profile_lock_detects_singleton_file(self, ):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            self.assertFalse(cdp_launch.profile_locked(profile))
            (profile / "SingletonLock").write_text("", encoding="utf-8")
            self.assertTrue(cdp_launch.profile_locked(profile))


class TestLaunchArguments(unittest.TestCase):
    def test_launch_includes_restore_last_session_and_profile(self):
        captured: list[list[str]] = []
        cdp_launch.launch_shared_browser(
            port=9222,
            profile=Path("/tmp/fake-profile"),
            chrome_bin="/fake/chrome",
            launcher=lambda argv: captured.append(argv),
            wait_seconds=0.1,
            sleep_fn=lambda _s: None,
            port_probe=lambda port: True,
        )
        self.assertEqual(len(captured), 1)
        argv = captured[0]
        self.assertIn("--restore-last-session", argv)
        self.assertIn("--remote-debugging-port=9222", argv)
        self.assertIn("--remote-debugging-address=127.0.0.1", argv)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in argv))
        self.assertIn("--no-first-run", argv)
        self.assertIn("--no-default-browser-check", argv)


class TestRunnerWiring(unittest.TestCase):
    """discover_cdp_endpoint / ensure_shared_cdp_endpoint 的行为锁。"""

    def _patched(self):
        m = runner
        old_probe = m._probe_cdp
        old_inspect = m.cdp_launch.inspect_state
        old_launch = m.cdp_launch.launch_shared_browser
        return m, old_probe, old_inspect, old_launch

    def _restore(self, m, old_probe, old_inspect, old_launch):
        m._probe_cdp = old_probe
        m.cdp_launch.inspect_state = old_inspect
        m.cdp_launch.launch_shared_browser = old_launch

    def test_attaches_when_port_is_up_and_never_launches(self):
        m, p, i, l = self._patched()
        launched: list = []
        m._probe_cdp = lambda port: {
            "port": port, "http": f"http://127.0.0.1:{port}",
            "browser": "Chrome/156", "ua": "Chrome", "headless": False, "pages": 3, "ws": "",
        }
        m.cdp_launch.inspect_state = lambda **kw: (_ for _ in ()).throw(
            AssertionError("attach 路径不得做体检/代启")
        )
        m.cdp_launch.launch_shared_browser = lambda **kw: launched.append(kw)
        try:
            info = m.ensure_shared_cdp_endpoint(port=9222, profile="/tmp/p")
        finally:
            self._restore(m, p, i, l)
        self.assertEqual(info["port"], 9222)
        self.assertEqual(info["reason"], "attached")
        self.assertEqual(launched, [])

    def test_vacant_state_launches(self):
        m, p, i, l = self._patched()
        launched: list[dict] = []
        calls = {"n": 0}

        def fake_probe(port):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return {
                "port": port, "http": f"http://127.0.0.1:{port}",
                "browser": "Chrome/156", "ua": "Chrome", "headless": False, "pages": 22, "ws": "",
            }

        m._probe_cdp = fake_probe
        m.cdp_launch.inspect_state = lambda **kw: cdp_launch.SharedBrowserState(False, False, False)
        m.cdp_launch.launch_shared_browser = lambda **kw: launched.append(kw)
        try:
            info = m.ensure_shared_cdp_endpoint(port=9222, profile="/tmp/p")
        finally:
            self._restore(m, p, i, l)
        self.assertEqual(info["reason"], "launched")
        self.assertEqual(len(launched), 1)

    def test_non_vacant_state_refuses_to_launch(self):
        for state in (
            cdp_launch.SharedBrowserState(True, False, False),
            cdp_launch.SharedBrowserState(False, True, False),
            cdp_launch.SharedBrowserState(False, False, True),
        ):
            m, p, i, l = self._patched()
            launched: list = []
            m._probe_cdp = lambda port: None
            m.cdp_launch.inspect_state = lambda **kw: state
            m.cdp_launch.launch_shared_browser = lambda **kw: launched.append(kw)
            try:
                with self.assertRaises(RuntimeError) as cm:
                    m.ensure_shared_cdp_endpoint(port=9222, profile="/tmp/p")
            finally:
                self._restore(m, p, i, l)
            self.assertIn("vacant", str(cm.exception).lower())
            self.assertEqual(launched, [], f"{state.describe()} 时不得代启")

    def test_discover_without_allow_launch_never_starts_browser(self):
        m, p, i, l = self._patched()
        launched: list = []
        m._ps_chrome_debug_candidates = lambda: []
        m._DEFAULT_CDP_PORTS = ()
        m._probe_cdp = lambda port: None
        m.cdp_launch.launch_shared_browser = lambda **kw: launched.append(kw)
        try:
            with self.assertRaises(RuntimeError) as cm:
                m.discover_cdp_endpoint()
        finally:
            self._restore(m, p, i, l)
            m._ps_chrome_debug_candidates = m._ps_chrome_debug_candidates
        self.assertIn("no live chrome cdp", str(cm.exception).lower())
        self.assertEqual(launched, [])


if __name__ == "__main__":
    unittest.main()

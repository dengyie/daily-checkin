#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from pathlib import Path
from unittest import mock
import contextlib
import inspect
import json
import os
import tempfile
import unittest



class TestDoctorHelpers(unittest.TestCase):
    """--doctor 健康自检的纯函数与接线守卫."""

    def setUp(self):
        self.m = __import__("stealth_checkin_runner")

    def test_recent_failure_summary_groups_and_truncates(self):
        rows = [("no_button", "a"), ("no_button", "b"), ("timeout", "c")]
        lines = self.m._summarize_recent_failures(rows)
        self.assertEqual(lines[0].split(" ")[0], "FAIL:no_button")
        self.assertIn("2", lines[0])
        many = [(f"r{i % 3}", f"site{i}") for i in range(30)]
        joined = " | ".join(self.m._summarize_recent_failures(many))
        self.assertIn("等10站", joined)

    def test_doctor_exit_code_aggregation(self):
        self.assertEqual(self.m._doctor_exit_code(["OK", "OK"]), 0)
        self.assertEqual(self.m._doctor_exit_code(["OK", "WARN"]), 1)
        self.assertEqual(self.m._doctor_exit_code(["OK", "WARN", "FAIL"]), 2)
        self.assertEqual(self.m._doctor_exit_code([]), 0)

    def test_doctor_skip_local_web_when_migrated_remote(self):
        """2026-11-06 Mac web 迁移 pxed 后, DAILY_CHECKIN_WEB_MIGRATED_REMOTE=1
        使 doctor 对本机 8765/8766 报 SKIP 而非 FAIL(不再恒定 exit=2)。"""
        import contextlib
        import io
        import tempfile
        from pathlib import Path as P
        from unittest import mock

        from checkin_core.store import SystemStore

        with tempfile.TemporaryDirectory() as td:
            def fake_status(url, timeout=4.0):
                if "/json/version" in url:
                    return 200
                return None  # 8765/8766 无本地监听(已迁移)

            with mock.patch.object(self.m, "discover_cdp_endpoint",
                                   return_value={"http": "http://127.0.0.1:9222", "headless": False}), \
                 mock.patch.object(self.m, "_doctor_http_status", side_effect=fake_status), \
                 mock.patch.object(self.m, "store_from_env",
                                   return_value=SystemStore(P(td) / "system.db")):
                # 迁移模式: 8765/8766 → SKIP, 不产生 FAIL
                os.environ["DAILY_CHECKIN_WEB_MIGRATED_REMOTE"] = "1"
                try:
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        code = self.m.run_doctor()
                    out = buf.getvalue()
                finally:
                    os.environ.pop("DAILY_CHECKIN_WEB_MIGRATED_REMOTE", None)
                self.assertIn("[SKIP] API 8765", out)
                self.assertIn("[SKIP] FE 8766", out)
                self.assertNotIn("应启动 `-m checkin_core.web", out)
                self.assertEqual(code, 0)
                # 未迁移(Windows 节点仍跑本地 web): 保持原 FAIL 语义
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = self.m.run_doctor()
                out = buf.getvalue()
                self.assertIn("[FAIL] API 8765", out)
                self.assertEqual(code, 2)

    def test_doctor_log_dir_resolves_staging_env(self):
        """doctor 必须按 env > staging env.sh > 默认解析日志目录,不 source 任意 shell."""
        import tempfile
        from pathlib import Path as P
        f = self.m._doctor_resolve_log_dir
        with mock.patch.dict(os.environ, {"DAILY_CHECKIN_LOG_DIR": "/tmp/x-log"}):
            self.assertEqual(f(), P("/tmp/x-log"))
        with mock.patch.dict(os.environ, {"DAILY_CHECKIN_LOG_DIR": ""}), \
             tempfile.TemporaryDirectory() as td:
            fake_home = P(td)
            env_dir = fake_home / "daily-checkin-staging"
            env_dir.mkdir()
            (env_dir / "env.sh").write_text(
                'export DAILY_CHECKIN_ROOT=/repo\nexport DAILY_CHECKIN_LOG_DIR="/tmp/staging-log"\n',
                encoding="utf-8",
            )
            with mock.patch.object(self.m.Path, "home", staticmethod(lambda: fake_home)):
                self.assertEqual(f(), P("/tmp/staging-log"))
            (env_dir / "env.sh").unlink()
            with mock.patch.object(self.m.Path, "home", staticmethod(lambda: fake_home)):
                self.assertEqual(f(), self.m.CHECKIN_LOG_DIR)

    def test_doctor_dispatch_precedes_business_run(self):
        """--doctor 必须在任何业务运行/CDP 连接之前短路返回."""
        import inspect as _inspect
        src = _inspect.getsource(self.m.run)
        idx = src.find("run_doctor()")
        self.assertGreater(idx, -1, "run() 应在入口处分派 --doctor")
        self.assertLess(idx, src.find("parse_cli_args(argv)") + len("parse_cli_args(argv)") + 200)


class TestSsoOriginMatch(unittest.TestCase):
    """SSO 回跳 netloc 判定:必须精确匹配 host,禁止子串匹配."""

    def setUp(self):
        self.m = __import__("stealth_checkin_runner")

    def test_exact_netloc_matches(self):
        self.assertTrue(self.m._sso_origin_matches("nexavlinks.com", "https://nexavlinks.com/check-in"))
        self.assertTrue(self.m._sso_origin_matches("nexavlinks.com", "nexavlinks.com/profile"))

    def test_substring_host_never_matches(self):
        self.assertFalse(self.m._sso_origin_matches("x.com", "https://ax.com/profile"))
        self.assertFalse(self.m._sso_origin_matches("relayfor.xyz", "https://evil-relayfor.xyz/"))
        self.assertFalse(self.m._sso_origin_matches("", "https://nexavlinks.com/"))


if __name__ == "__main__":
    unittest.main()

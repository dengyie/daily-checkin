"""Tests for system_config schema, dynamic configuration, and web API / notify."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from checkin_core.keychain import credential_store_from_env
from checkin_core.notify import notify_daily_summary, build_daily_summary_text
from checkin_core.store import SystemStore, DEFAULT_SYSTEM_CONFIGS
from checkin_core.web import CheckinWebApp
from http.server import ThreadingHTTPServer


class TestSystemConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SystemStore(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_default_configs_loaded(self):
        configs = self.store.get_all_configs()
        self.assertEqual(configs["schedule_cron_time"], "08:10")
        self.assertEqual(configs["batch_timeout_s"], "2400")
        self.assertEqual(configs["site_timeout_s"], "60")
        self.assertEqual(configs["tg_notify_policy"], "all")

    def test_set_and_get_config(self):
        self.store.set_config("site_timeout_s", "90")
        self.assertEqual(self.store.get_config("site_timeout_s"), "90")

        all_cfgs = self.store.get_all_configs()
        self.assertEqual(all_cfgs["site_timeout_s"], "90")

    def test_set_configs_batch(self):
        res = self.store.set_configs({
            "batch_timeout_s": "3600",
            "tg_notify_enabled": "false",
            "tg_chat_id": "12345678",
        })
        self.assertEqual(res["batch_timeout_s"], "3600")
        self.assertEqual(res["tg_notify_enabled"], "false")
        self.assertEqual(res["tg_chat_id"], "12345678")


class TestNotifyWithDynamicConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SystemStore(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _res(self, status, site="站A", reason=""):
        return mock.Mock(status=status, site=site, reason=reason)

    def test_notify_disabled_by_config(self):
        self.store.set_configs({
            "tg_notify_enabled": "false",
            "tg_bot_token": "token123",
            "tg_chat_id": "chat123",
        })
        with mock.patch("checkin_core.notify._send_telegram_raw") as raw:
            sent = notify_daily_summary(
                [self._res("OK")], "2026-09-02", run_scope="full", store=self.store
            )
            self.assertFalse(sent)
            raw.assert_not_called()

    def test_notify_fail_only_policy_skips_when_all_success(self):
        self.store.set_configs({
            "tg_notify_enabled": "true",
            "tg_bot_token": "token123",
            "tg_chat_id": "chat123",
            "tg_notify_policy": "fail_only",
        })
        with mock.patch("checkin_core.notify._send_telegram_raw") as raw:
            # All OK -> should skip
            sent = notify_daily_summary(
                [self._res("OK"), self._res("ALREADY")], "2026-09-02", run_scope="full", store=self.store
            )
            self.assertFalse(sent)
            raw.assert_not_called()

            # Has FAIL -> should send
            raw.return_value = True
            sent2 = notify_daily_summary(
                [self._res("OK"), self._res("FAIL", reason="timeout")],
                "2026-09-02", run_scope="full", store=self.store
            )
            self.assertTrue(sent2)
            raw.assert_called_once()


class TestConfigWebApi(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "system.db")
        self.cred_dir = os.path.join(self.tmp_dir.name, "cred")
        self.env_patch = mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_DB": self.db_path,
            "DAILY_CHECKIN_CREDENTIAL_BACKEND": "file",
            "DAILY_CHECKIN_CREDENTIALS_DIR": self.cred_dir,
            "DAILY_CHECKIN_WEB_PASSWORD": "testpassword",
            "DAILY_CHECKIN_WEB_ORIGINS": "http://127.0.0.1:8766",
        })
        self.env_patch.start()
        self.app = CheckinWebApp(
            store=SystemStore(self.db_path),
            credentials=credential_store_from_env(),
            require_auth=False,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler())
        self.server.csrf = self.app.csrf
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env_patch.stop()
        self.tmp_dir.cleanup()

    def _get(self, path: str):
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-CSRF-Token": self.server.csrf},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_get_and_update_config_api(self):
        status, data = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("batch_timeout_s", data["configs"])

        # Update configs
        status, update_res = self._post("/api/config/update", {
            "configs": {
                "schedule_cron_time": "09:30",
                "site_timeout_s": "75",
                "tg_notify_enabled": "true",
            }
        })
        self.assertEqual(status, 200)
        self.assertTrue(update_res["ok"])
        self.assertEqual(update_res["configs"]["schedule_cron_time"], "09:30")
        self.assertEqual(update_res["configs"]["site_timeout_s"], "75")

    def test_notify_test_api(self):
        with mock.patch("checkin_core.web._send_telegram_raw", return_value=True) as raw:
            status, res = self._post("/api/notify/test", {
                "tg_bot_token": "fake_token",
                "tg_chat_id": "fake_chat_id",
            })
            self.assertEqual(status, 200)
            self.assertTrue(res["ok"])
            raw.assert_called_once()

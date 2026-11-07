#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from unittest import mock
import json
import os
import unittest



class TestNotifyDailySummary(unittest.TestCase):
    """Pure tests for checkin_core.notify (no network: format + gating only)."""

    def setUp(self):
        from checkin_core import notify as _notify

        self.notify = _notify

    def _res(self, status, site="站A", reason=""):
        # notify.build_daily_summary_text only reads .status/.site/.reason
        # (duck-typed), so a plain object avoids importing the runner module
        # (which requires playwright).
        return mock.Mock(status=status, site=site, reason=reason)


    def test_all_success_summary(self):
        results = [self._res("OK", f"站{i}") for i in range(10)]
        text = self.notify.build_daily_summary_text(results, "2026-08-23")
        self.assertIn("✅", text)
        self.assertIn("成功 10/10", text)
        self.assertIn("08-23", text)
        self.assertIn("🆕 今日签到", text)
        self.assertNotIn("❌", text)

    def test_mixed_fail_summary_groups_by_reason(self):
        results = [
            self._res("OK", "站A"),
            self._res("ALREADY", "站B"),
            self._res("FAIL", "站C", "no_button"),
            self._res("FAIL", "站D", "no_button"),
            self._res("FAIL", "站E", "timeout"),
        ]
        text = self.notify.build_daily_summary_text(results, "2026-08-23")
        self.assertIn("⚠️", text)
        self.assertIn("成功 2/5", text)
        self.assertIn("❌ 失败 3 站", text)
        self.assertIn("no_button: 站C、站D", text)
        self.assertIn("timeout: 站E", text)
        self.assertIn("🔄 今日已签: 站B", text)

    def test_empty_results_no_crash(self):
        text = self.notify.build_daily_summary_text([], "2026-08-23")
        self.assertIn("成功 0/0", text)

    def test_gate_skips_targeted_scope(self):
        # targeted scope never sends, even with credentials present
        with unittest.mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_TG_BOT_TOKEN": "t",
            "DAILY_CHECKIN_TG_CHAT_ID": "1",
        }):
            self.assertFalse(
                self.notify.notify_daily_summary(
                    [self._res("OK")], "2026-08-23", run_scope="targeted"
                )
            )

    def test_gate_skips_when_unconfigured(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                self.notify.notify_daily_summary(
                    [self._res("OK")], "2026-08-23", run_scope="full"
                )
            )

    def test_gate_skips_success_by_default(self):
        # 2026-11-05 成功静默: full scope + credentials but 0 failures and
        # NOTIFY_ON_SUCCESS unset → no TG send (基本不报).
        with unittest.mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_TG_BOT_TOKEN": "12345:TESTTOKEN",
            "DAILY_CHECKIN_TG_CHAT_ID": "987654321",
        }, clear=False):
            os.environ.pop("NOTIFY_ON_SUCCESS", None)
            with mock.patch.object(self.notify, "_send_telegram_raw") as raw:
                called = self.notify.notify_daily_summary(
                    [self._res("OK", "站A")], "2026-08-23", run_scope="full"
                )
        self.assertFalse(called)
        raw.assert_not_called()

    def test_gate_sends_for_configured_full_scope(self):
        # Full scope + configured credentials + NOTIFY_ON_SUCCESS=true →
        # _send_telegram_raw is invoked exactly once with token/chat_id and
        # built summary text (success notify is now opt-in).
        token = "12345:TESTTOKEN"
        chat_id = "987654321"
        with unittest.mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_TG_BOT_TOKEN": token,
            "DAILY_CHECKIN_TG_CHAT_ID": chat_id,
            "NOTIFY_ON_SUCCESS": "true",
        }, clear=False):
            with mock.patch.object(
                self.notify, "_send_telegram_raw", return_value=True
            ) as raw:
                called = self.notify.notify_daily_summary(
                    [self._res("OK", "站A")], "2026-08-23", run_scope="full"
                )
        self.assertTrue(called)
        raw.assert_called_once()
        args, _ = raw.call_args
        self.assertEqual(args[0], token)
        self.assertEqual(args[1], chat_id)
        summary = args[2]
        self.assertIn("✅", summary)
        self.assertIn("成功 1/1", summary)

    def test_gate_does_not_send_for_targeted_scope(self):
        # targeted scope must not reach _send_telegram_raw even when configured.
        with unittest.mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_TG_BOT_TOKEN": "t",
            "DAILY_CHECKIN_TG_CHAT_ID": "1",
        }, clear=False):
            with mock.patch.object(
                self.notify, "_send_telegram_raw", return_value=True
            ) as raw:
                called = self.notify.notify_daily_summary(
                    [self._res("OK")], "2026-08-23", run_scope="targeted"
                )
        self.assertFalse(called)
        raw.assert_not_called()


class TestTelegramSend(unittest.TestCase):
    """Real tests for checkin_core.notify._send_telegram_raw (Fix 1)."""

    def setUp(self):
        from checkin_core import notify as _notify

        self.notify = _notify

    def _fake_resp(self, status, body_bytes):
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=ctx)
        ctx.__exit__ = mock.Mock(return_value=False)
        ctx.status = status
        ctx.read = mock.Mock(return_value=body_bytes)
        return ctx

    def test_send_ok_true(self):
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": true, "result": {"message_id": 1}}'
            )
            result = self.notify._send_telegram_raw("tok", "123", "hi")
        self.assertTrue(result)

    def test_send_ok_false_rejected(self):
        # HTTP 200 but Telegram reports {"ok": false} (Fix 1 bug: a
        # syntactically-valid-but-nonexistent chat_id silently fails).
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": false, "error_code": 400, "description": "chat not found"}'
            )
            result = self.notify._send_telegram_raw("tok", "999", "hi")
        self.assertFalse(result)

    def test_send_http_error_false(self):
        # Non-200 status → return False before even reading body.
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                502, b'{"ok": false}'
            )
            result = self.notify._send_telegram_raw("tok", "123", "hi")
        self.assertFalse(result)

    def test_send_network_exception_false(self):
        # urlopen raising → caught by try/except → False.
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.side_effect = ConnectionError("boom")
            result = self.notify._send_telegram_raw("tok", "123", "hi")
        self.assertFalse(result)

    def test_request_url_and_body(self):
        # Capture the Request urlopen receives: URL has token + /sendMessage,
        # and JSON body has chat_id/text/disable_web_page_preview.
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": true, "result": {}}'
            )
            self.notify._send_telegram_raw("MYTOKEN", "chat-1", "你好 world")
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertIsInstance(req, self.notify.urllib.request.Request)
        self.assertIn("MYTOKEN", req.full_url)
        self.assertIn("/sendMessage", req.full_url)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], "chat-1")
        self.assertEqual(body["text"], "你好 world")
        self.assertTrue(body["disable_web_page_preview"])

    def test_text_truncation_when_exceeding_max_len(self):
        long_text = "x" * 5000
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": true, "result": {}}'
            )
            self.notify._send_telegram_raw("MYTOKEN", "chat-1", long_text)
        req = urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertLessEqual(len(body["text"]), self.notify.MAX_TG_TEXT_LEN)
        self.assertTrue(body["text"].endswith("(已截断)"))

    def test_ssl_context_fallback(self):
        ctx = self.notify._get_ssl_context()
        # On macOS or any Unix with /etc/ssl/cert.pem, context should be returned or None
        self.assertTrue(ctx is None or isinstance(ctx, self.notify.ssl.SSLContext))


if __name__ == "__main__":
    unittest.main()

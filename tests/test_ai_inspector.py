"""Unit test suite for RFC-042 AI Inspector and Auto-Remediation Engine.

Covers:
1. Domain DTOs and validation (SiteInspectionResult, InspectionReport).
2. EvidenceSanitizer zero-leakage redaction (Bearer, JWT, credentials, emails, UIDs, truncation).
3. SystemStore schema v6 persistence, retry counters, and site suppression.
4. AIInspector engine execution, JSON fence stripping, double confidence gating, error recovery.
5. RemediationEngine anti-storm retry budget, auto-suppression, and subprocess scheduling.
6. Telegram notification summary rendering with AI inspection results.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from checkin_core.ai_inspector import AIInspector
from checkin_core.ai_inspector_models import (
    AIChannelConfig,
    InspectionReport,
    SiteInspectionResult,
    VALID_CATEGORIES,
)
from checkin_core.ai_sanitizer import EvidenceSanitizer
from checkin_core.notify import build_daily_summary_text, notify_daily_summary
from checkin_core.remediation_engine import RemediationEngine
from checkin_core.store import SystemStore


@dataclass
class MockCheckinResult:
    site: str
    status: str
    reason: str = ""
    stage: str = ""
    detail: str = ""
    elapsed_s: float = 1.0


class TestAIInspectorModels(unittest.TestCase):
    """Test domain model parsing, validation, and sanitization boundaries."""

    def test_valid_site_inspection_result(self):
        data = {
            "site": "test_site",
            "category": "transient_flake",
            "confidence": 0.92,
            "root_cause": "502 Bad Gateway from upstream CDN",
            "can_auto_retry": True,
            "suggested_action": "Wait 60s and retry once",
        }
        res = SiteInspectionResult.from_dict(data)
        self.assertEqual(res.site, "test_site")
        self.assertEqual(res.category, "transient_flake")
        self.assertEqual(res.confidence, 0.92)
        self.assertTrue(res.can_auto_retry)
        self.assertEqual(res.to_dict(), data)

    def test_invalid_category_fallback(self):
        data = {
            "site": "test_site",
            "category": "some_hallucinated_category",
            "confidence": 0.85,
        }
        res = SiteInspectionResult.from_dict(data)
        self.assertEqual(res.category, "unknown")

    def test_confidence_clamping_and_coercion(self):
        res_high = SiteInspectionResult.from_dict({"confidence": 1.5})
        self.assertEqual(res_high.confidence, 1.0)

        res_low = SiteInspectionResult.from_dict({"confidence": -0.5})
        self.assertEqual(res_low.confidence, 0.0)

        res_invalid = SiteInspectionResult.from_dict({"confidence": "not_a_number"})
        self.assertEqual(res_invalid.confidence, 0.0)

    def test_inspection_report_serialization(self):
        report_data = {
            "summary": "Today 1 site had transient network timeout",
            "total_failures": 1,
            "inspections": [
                {
                    "site": "alpha",
                    "category": "transient_flake",
                    "confidence": 0.95,
                    "root_cause": "Connection timeout",
                    "can_auto_retry": True,
                    "suggested_action": "Retry",
                }
            ],
            "auto_retry_targets": ["alpha"],
            "channel_name": "",
            "fallback_chain": [],
        }
        report = InspectionReport.from_dict(report_data)
        self.assertEqual(report.summary, "Today 1 site had transient network timeout")
        self.assertEqual(len(report.inspections), 1)
        self.assertEqual(report.auto_retry_targets, ["alpha"])
        self.assertEqual(report.channel_name, "")
        self.assertEqual(report.fallback_chain, [])
        self.assertEqual(report.to_dict(), report_data)


class TestEvidenceSanitizer(unittest.TestCase):
    """Test zero-leakage evidence sanitizer P0 security red lines."""

    def test_sanitize_bearer_token(self):
        text = "Failed POST https://api.site.com with headers: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.test"
        sanitized = EvidenceSanitizer.sanitize_text(text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", sanitized)
        self.assertIn("Bearer [REDACTED]", sanitized)

    def test_sanitize_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        text = f"Invalid session token={jwt} returned from auth endpoint"
        sanitized = EvidenceSanitizer.sanitize_text(text)
        self.assertNotIn(jwt, sanitized)
        self.assertIn("[JWT_REDACTED]", sanitized)

    def test_sanitize_credentials_kv(self):
        cases = [
            ("password=SuperSecretPassword123", "password=[REDACTED]"),
            ("token:'my_secret_api_token'", "token=[REDACTED]"),
            ('secret="production_key_456789"', "secret=[REDACTED]"),
            ("cookie=session_9876543210_token", "cookie=[REDACTED]"),
        ]
        for raw, expected in cases:
            res = EvidenceSanitizer.sanitize_text(raw)
            self.assertEqual(res, expected)

    def test_sanitize_email(self):
        text = "Login error for user admin@superdomain.org with invalid credentials"
        sanitized = EvidenceSanitizer.sanitize_text(text)
        self.assertNotIn("admin@superdomain.org", sanitized)
        self.assertIn("[EMAIL_REDACTED]", sanitized)

    def test_sanitize_uid(self):
        text = "Failed query for uid=98765432 and user_id: 123456"
        sanitized = EvidenceSanitizer.sanitize_text(text)
        self.assertNotIn("98765432", sanitized)
        self.assertNotIn("123456", sanitized)
        self.assertIn("uid=[UID_REDACTED]", sanitized)
        self.assertIn("user_id=[UID_REDACTED]", sanitized)

    def test_max_length_truncation(self):
        long_text = "A" * 600
        sanitized = EvidenceSanitizer.sanitize_text(long_text, max_length=100)
        self.assertTrue(sanitized.endswith("…[truncated]"))
        self.assertEqual(len(sanitized), 100 + len("…[truncated]"))

    def test_sanitize_task_context(self):
        raw_ctx = {
            "name": "example_site",
            "url": "https://example.com/api",
            "adapter": "newapi",
            "stage": "auth",
            "reason": "bad_credentials",
            "detail": "Failed login for admin@domain.com password=mySecretPass999",
            "elapsed_s": "3.456",
            "historical_failures": "5",
        }
        safe_ctx = EvidenceSanitizer.sanitize_task_context(raw_ctx)
        self.assertEqual(safe_ctx["site"], "example_site")
        self.assertEqual(safe_ctx["elapsed_s"], 3.5)
        self.assertEqual(safe_ctx["historical_failures"], 5)
        self.assertNotIn("admin@domain.com", safe_ctx["detail"])
        self.assertNotIn("mySecretPass999", safe_ctx["detail"])
        self.assertIn("[EMAIL_REDACTED]", safe_ctx["detail"])
        self.assertIn("password=[REDACTED]", safe_ctx["detail"])


class TestStoreInspectionPersistence(unittest.TestCase):
    """Test SQLite system.db schema v6 extensions for AI inspection."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SystemStore(self.tmp.name)
        # Seed test site
        self.store.upsert_site("test_site", url="https://test.com", provider="generic")

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_save_and_get_ai_inspection(self):
        day = "2026-10-07"
        report_json = json.dumps({"summary": "test", "total_failures": 0})
        row_id = self.store.save_ai_inspection(
            day=day,
            model="gpt-4o-mini",
            latency_ms=120,
            status="ok",
            summary="All clean",
            structured_json=report_json,
            source="cron",
            prompt_tokens=150,
            completion_tokens=50,
        )
        self.assertGreater(row_id, 0)

        record = self.store.get_latest_ai_inspection(day)
        self.assertIsNotNone(record)
        self.assertEqual(record["day"], day)
        self.assertEqual(record["model"], "gpt-4o-mini")
        self.assertEqual(record["latency_ms"], 120)
        self.assertEqual(record["summary"], "All clean")
        self.assertEqual(record["prompt_tokens"], 150)
        self.assertEqual(record["completion_tokens"], 50)

    def test_update_latest_ai_remediation_log(self):
        day = "2026-10-07"
        self.store.save_ai_inspection(
            day=day,
            model="gpt-4o-mini",
            latency_ms=100,
            status="ok",
            summary="testing",
            structured_json="{}",
            remediation_log="[]",
        )
        records = [
            {"site_name": "alpha", "action": "suppress", "status": "success"},
            {"site_name": "beta", "action": "retry", "status": "scheduled"},
        ]
        ok = self.store.update_latest_ai_remediation_log(day, records)
        self.assertTrue(ok)
        rec = self.store.get_latest_ai_inspection(day)
        self.assertIsNotNone(rec)
        parsed = json.loads(rec["remediation_log"])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["site_name"], "alpha")
        self.assertEqual(parsed[1]["action"], "retry")

    def test_retry_count_tracking(self):
        day = "2026-10-07"
        site = "alpha_site"

        self.assertEqual(self.store.get_today_retry_count(site, day), 0)
        new_cnt = self.store.increment_today_retry_count(site, day)
        self.assertEqual(new_cnt, 1)
        self.assertEqual(self.store.get_today_retry_count(site, day), 1)

        new_cnt2 = self.store.increment_today_retry_count(site, day)
        self.assertEqual(new_cnt2, 2)
        self.assertEqual(self.store.get_today_retry_count(site, day), 2)

    def test_suppress_site(self):
        self.store.suppress_site("test_site", reason="ai:disabled:service discontinued")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT sh.status, sh.last_reason FROM site_health sh "
                "JOIN sites s ON sh.site_id = s.id WHERE s.name = ?",
                ("test_site",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "suppressed")
            self.assertEqual(row["last_reason"], "ai:disabled:service discontinued")


class TestAIInspectorEngine(unittest.IsolatedAsyncioTestCase):
    """Test AIInspector LLM execution, fence stripping, double gating."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SystemStore(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    async def test_inspect_failures_empty_input(self):
        inspector = AIInspector(
            store=self.store,
            base_url="https://api.openai.com/v1",
            api_key="test-key",
        )
        report = await inspector.inspect_failures("2026-10-07", [])
        self.assertIsNone(report)

    async def test_inspect_failures_gating_and_fences(self):
        mock_llm_response = {
            "summary": "1 transient flake, 1 auth failure, 1 low confidence flake",
            "inspections": [
                {
                    "site": "site_flake_high_conf",
                    "category": "transient_flake",
                    "confidence": 0.90,
                    "root_cause": "504 Gateway Timeout",
                    "can_auto_retry": True,
                    "suggested_action": "Retry in 60s",
                },
                {
                    "site": "site_flake_low_conf",
                    "category": "transient_flake",
                    "confidence": 0.70,  # below 0.85 threshold
                    "root_cause": "Unknown timeout",
                    "can_auto_retry": True,
                    "suggested_action": "Check site",
                },
                {
                    "site": "site_auth_expired",
                    "category": "auth_expired",
                    "confidence": 0.99,
                    "root_cause": "Session token rejected 401",
                    "can_auto_retry": True,  # Non-transient category must be blocked
                    "suggested_action": "Update token",
                },
            ],
            "auto_retry_targets": ["site_flake_high_conf", "site_flake_low_conf", "site_auth_expired"],
        }

        # Simulate LLM returning markdown fences: ```json { ... } ```
        fence_wrapped_content = f"```json\n{json.dumps(mock_llm_response)}\n```"

        inspector = AIInspector(
            store=self.store,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )

        with mock.patch.object(
            inspector,
            "_invoke_llm_api",
            return_value=(mock_llm_response, 200, 80),
        ):
            failed = [
                MockCheckinResult(site="site_flake_high_conf", status="FAIL", reason="timeout"),
                MockCheckinResult(site="site_flake_low_conf", status="FAIL", reason="timeout"),
                MockCheckinResult(site="site_auth_expired", status="FAIL", reason="unauthorized"),
            ]
            report = await inspector.inspect_failures("2026-10-07", failed)

            self.assertIsNotNone(report)
            self.assertEqual(report.total_failures, 3)
            # Double confidence gating must ONLY allow site_flake_high_conf!
            self.assertEqual(report.auto_retry_targets, ["site_flake_high_conf"])

            # Verify persisted inspection record
            db_record = self.store.get_latest_ai_inspection("2026-10-07")
            self.assertIsNotNone(db_record)
            self.assertEqual(db_record["status"], "ok")
            self.assertEqual(db_record["prompt_tokens"], 200)

    async def test_strip_markdown_fence(self):
        raw = "```json\n{\"foo\": \"bar\"}\n```"
        stripped = AIInspector._strip_markdown_fence(raw)
        self.assertEqual(stripped, '{"foo": "bar"}')

    async def test_llm_exception_safety(self):
        inspector = AIInspector(
            store=self.store,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )
        with mock.patch.object(
            inspector,
            "_invoke_llm_api",
            side_effect=Exception("Network error"),
        ):
            failed = [MockCheckinResult(site="err_site", status="FAIL")]
            report = await inspector.inspect_failures("2026-10-07", failed)
            self.assertIsNone(report)

    async def test_multi_channel_primary_success(self):
        channels = [
            AIChannelConfig(name="Primary", base_url="https://primary.api", api_key="sk-p", model="deepseek-chat"),
            AIChannelConfig(name="Fallback", base_url="https://fallback.api", api_key="sk-f", model="gpt-4o-mini"),
        ]
        inspector = AIInspector(store=self.store, channels=channels)
        mock_response = {
            "summary": "OK",
            "inspections": [
                {
                    "site": "site_a",
                    "category": "transient_flake",
                    "confidence": 0.9,
                    "root_cause": "timeout",
                    "can_auto_retry": True,
                    "suggested_action": "retry",
                }
            ],
            "auto_retry_targets": ["site_a"],
        }
        with mock.patch.object(inspector, "_invoke_llm_api", return_value=(mock_response, 50, 20)) as mock_invoke:
            failed = [MockCheckinResult(site="site_a", status="FAIL")]
            report = await inspector.inspect_failures("2026-10-07", failed)
            self.assertIsNotNone(report)
            self.assertEqual(report.channel_name, "Primary")
            self.assertEqual(report.fallback_chain, [])
            self.assertEqual(mock_invoke.call_count, 1)

    async def test_multi_channel_fallback_degradation(self):
        channels = [
            AIChannelConfig(name="Primary", base_url="https://primary.api", api_key="sk-p", model="deepseek-chat"),
            AIChannelConfig(name="Fallback", base_url="https://fallback.api", api_key="sk-f", model="gpt-4o-mini"),
        ]
        inspector = AIInspector(store=self.store, channels=channels)
        fallback_response = {
            "summary": "Recovered via Fallback",
            "inspections": [
                {
                    "site": "site_b",
                    "category": "auth_expired",
                    "confidence": 0.95,
                    "root_cause": "401 token expired",
                    "can_auto_retry": False,
                    "suggested_action": "relogin",
                }
            ],
            "auto_retry_targets": [],
        }
        with mock.patch.object(
            inspector,
            "_invoke_llm_api",
            side_effect=[Exception("HTTP 429 Rate Limit Exceeded"), (fallback_response, 60, 25)],
        ) as mock_invoke:
            failed = [MockCheckinResult(site="site_b", status="FAIL")]
            report = await inspector.inspect_failures("2026-10-07", failed)
            self.assertIsNotNone(report)
            self.assertEqual(report.channel_name, "Fallback")
            self.assertEqual(len(report.fallback_chain), 1)
            self.assertIn("Primary", report.fallback_chain[0])
            self.assertEqual(mock_invoke.call_count, 2)

    async def test_multi_channel_all_exhausted(self):
        channels = [
            AIChannelConfig(name="Primary", base_url="https://primary.api", api_key="sk-p", model="deepseek-chat"),
            AIChannelConfig(name="Fallback", base_url="https://fallback.api", api_key="sk-f", model="gpt-4o-mini"),
        ]
        inspector = AIInspector(store=self.store, channels=channels)
        with mock.patch.object(
            inspector,
            "_invoke_llm_api",
            side_effect=[Exception("500 Internal Server Error"), Exception("Connection Timeout")],
        ) as mock_invoke:
            failed = [MockCheckinResult(site="site_c", status="FAIL")]
            report = await inspector.inspect_failures("2026-10-07", failed)
            self.assertIsNone(report)
            self.assertEqual(mock_invoke.call_count, 2)

    def test_store_get_ai_channels_fallback_config(self):
        self.store.set_configs({
            "ai_inspector_base_url": "https://p.test/v1",
            "ai_inspector_api_key": "sk-primary",
            "ai_inspector_model": "deepseek-chat",
            "ai_inspector_fallback_enabled": "true",
            "ai_inspector_fallback_base_url": "https://fb.test/v1",
            "ai_inspector_fallback_api_key": "sk-fallback",
            "ai_inspector_fallback_model": "gpt-4o-mini",
        })
        channels = self.store.get_ai_channels()
        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0].base_url, "https://p.test/v1")
        self.assertEqual(channels[0].api_key, "sk-primary")
        self.assertEqual(channels[0].model, "deepseek-chat")
        self.assertEqual(channels[1].base_url, "https://fb.test/v1")
        self.assertEqual(channels[1].api_key, "sk-fallback")
        self.assertEqual(channels[1].model, "gpt-4o-mini")


class TestRemediationEngine(unittest.IsolatedAsyncioTestCase):
    """Test anti-storm retry budget, auto-suppression, and scheduling."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SystemStore(self.tmp.name)
        self.store.upsert_site("down_site", url="https://down.com", provider="generic")

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_filter_safe_retry_targets_budget(self):
        day = "2026-10-07"
        targets = ["site_a", "site_b", "site_c", "site_d"]

        # 1. First time: capped at max_targets (3)
        safe = RemediationEngine.filter_safe_retry_targets(targets, day, self.store, max_targets=3)
        self.assertEqual(safe, ["site_a", "site_b", "site_c"])

        # 2. Simulate site_b already retried once today
        self.store.increment_today_retry_count("site_b", day)

        safe_after = RemediationEngine.filter_safe_retry_targets(targets, day, self.store, max_targets=3)
        self.assertEqual(safe_after, ["site_a", "site_c", "site_d"])

    def test_apply_auto_suppression(self):
        report = InspectionReport(
            summary="Test",
            total_failures=2,
            inspections=[
                SiteInspectionResult(
                    site="down_site",
                    category="upstream_disabled",
                    confidence=0.95,
                    root_cause="Domain expired, owner closed checkin",
                    can_auto_retry=False,
                    suggested_action="Disable site",
                ),
                SiteInspectionResult(
                    site="uncertain_site",
                    category="upstream_disabled",
                    confidence=0.80,  # Below 0.90 threshold
                    root_cause="Possible maintenance",
                    can_auto_retry=False,
                    suggested_action="Review",
                ),
            ],
            auto_retry_targets=[],
        )

        suppressed = RemediationEngine.apply_auto_suppression(report, self.store)
        self.assertEqual(suppressed, ["down_site"])

        with self.store.connect() as db:
            row = db.execute(
                "SELECT sh.status, sh.last_reason FROM site_health sh "
                "JOIN sites s ON sh.site_id = s.id WHERE s.name = ?",
                ("down_site",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "suppressed")
            self.assertIn("ai:disabled", row["last_reason"])

    def test_schedule_retries_increments_and_creates_task(self):
        day = "2026-10-07"
        targets = ["site_x", "site_y"]

        with mock.patch("subprocess.Popen") as mock_popen:
            scheduled = RemediationEngine.schedule_retries(targets, day, self.store, delay_seconds=0.1)
            self.assertEqual(scheduled, ["site_x", "site_y"])
            # Verified retry count incremented
            self.assertEqual(self.store.get_today_retry_count("site_x", day), 1)
            self.assertEqual(self.store.get_today_retry_count("site_y", day), 1)

            mock_popen.assert_called_once()
            call_kwargs = mock_popen.call_args[1]
            self.assertTrue(call_kwargs.get("start_new_session"))
            call_cmd = mock_popen.call_args[0][0]
            self.assertIn("-c", call_cmd)
            self.assertIn("site_x,site_y", call_cmd[2])

    async def test_execute_isolated_retry_subprocess_call(self):
        with mock.patch("asyncio.sleep", return_value=None), \
             mock.patch("asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = mock.AsyncMock()
            proc_mock.communicate.return_value = (b"ok", b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock

            await RemediationEngine._execute_isolated_retry(
                ["site_a", "site_b"],
                "2026-10-07",
                Path("/tmp/fake_runner.py"),
                delay_seconds=0.01,
            )
            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            self.assertIn("--only", call_args)
            self.assertIn("site_a,site_b", call_args)


class TestTelegramNotificationAIReport(unittest.TestCase):
    """Test build_daily_summary_text with AI inspection report section."""

    def test_build_daily_summary_text_with_ai_report(self):
        results = [
            MockCheckinResult(site="ok_site", status="OK"),
            MockCheckinResult(site="fail_site", status="FAIL", reason="504 Timeout"),
        ]
        ai_report = InspectionReport(
            summary="1 site had upstream gateway timeout",
            total_failures=1,
            inspections=[
                SiteInspectionResult(
                    site="fail_site",
                    category="transient_flake",
                    confidence=0.92,
                    root_cause="Cloudflare 504 Gateway Timeout",
                    can_auto_retry=True,
                    suggested_action="Retry in 60s",
                )
            ],
            auto_retry_targets=["fail_site"],
        )

        text = build_daily_summary_text(results, "2026-10-07", ai_report=ai_report)
        self.assertIn("AI 智能质检归因:", text)
        self.assertIn("💡 结论: 1 site had upstream gateway timeout", text)
        self.assertIn("• fail_site: [transient_flake] Cloudflare 504 Gateway Timeout -> Retry in 60s", text)
        self.assertIn("⚡ 已触发自愈重试: fail_site", text)


if __name__ == "__main__":
    unittest.main()

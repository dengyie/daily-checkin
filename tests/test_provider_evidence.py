from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from checkin_core.models import (
    ActionEvidence,
    ConfirmationEvidence,
    IdentityEvidence,
    ProviderInspection,
    evidence_dict,
    evidence_is_confirming,
)
from checkin_core.providers import LegacyBrowserProvider, ProviderContext, WisartProvider
from checkin_core.registry import ProviderRegistry


class DummyResult:
    def __init__(self):
        self.provider = "legacy_browser"
        self.stage = ""


async def dummy_performer(_ctx: ProviderContext):
    return DummyResult()


def load_runner(name: str) -> Any:
    runner_path = Path(__file__).resolve().parents[1] / "stealth_checkin_runner.py"
    spec = importlib.util.spec_from_file_location(name, runner_path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProviderEvidenceTests(unittest.TestCase):
    def test_sso_target_snapshot_redacts_query_and_fragment(self):
        module = load_runner("provider_runner_sso_snapshot_test")
        original = module.cdp_target_list
        module.cdp_target_list = lambda: [{
            "id": "oauth-target",
            "openerId": "root-target",
            "type": "page",
            "url": "https://linux.do/oauth/authorize?code=secret#fragment",
        }]
        try:
            snapshot = module.sso_target_snapshot()
            self.assertEqual(snapshot[0]["url"], "https://linux.do/oauth/authorize")
            self.assertNotIn("code=secret", json.dumps(snapshot))
            self.assertEqual(snapshot[0]["opener_id"], "root-target")
        finally:
            module.cdp_target_list = original

    def test_sso_popup_tracker_captures_popup_emitted_during_click(self):
        module = load_runner("provider_runner_sso_popup_tracker_test")

        class Page:
            def __init__(self):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

            def click_linuxdo(self):
                self.handlers["popup"](object())

        page = Page()
        popups = {}
        state = module.register_sso_popup_tracker(page, popups)
        page.click_linuxdo()
        self.assertTrue(state["attached"])
        self.assertEqual(state["events"], 1)
        self.assertEqual(len(popups), 1)

    def test_evidence_contract_is_json_serializable(self):
        payload = {
            "action": evidence_dict(ActionEvidence(kind="native_click", target="button:立即签到")),
            "confirmation": evidence_dict(
                ConfirmationEvidence(
                    kind="server_status",
                    source="GET /api/user/checkin",
                    summary="checked_in_today=true",
                    observed_at="2026-08-04T00:00:00",
                    checked_in_today=True,
                )
            ),
            "identity": evidence_dict(IdentityEvidence(matched=None, source="single-profile")),
        }
        json.dumps(payload, ensure_ascii=False)
        self.assertTrue(
            evidence_is_confirming(
                ConfirmationEvidence(
                    kind="dom_strong",
                    source="legacy_dom_gate",
                    summary="签到成功",
                    observed_at="2026-08-04T00:00:00",
                )
            )
        )

    def test_empty_confirmation_cannot_authorize_success(self):
        self.assertFalse(evidence_is_confirming(None))
        self.assertFalse(
            evidence_is_confirming(
                ConfirmationEvidence(
                    kind="dom_strong", source="", summary="", observed_at=""
                )
            )
        )

    def test_registry_prefers_wisart_then_falls_back(self):
        registry = ProviderRegistry(
            [WisartProvider(dummy_performer), LegacyBrowserProvider(dummy_performer)]
        )
        self.assertEqual(
            registry.resolve(
                site="图片公益站",
                url="https://wisart.kuaileshifu.com/#/checkin",
                adapter_kind="browser",
            ).name,
            "wisart",
        )
        self.assertEqual(
            registry.resolve(
                site="other",
                url="https://example.com/profile",
                adapter_kind="browser",
            ).name,
            "legacy_browser",
        )

    def test_wisart_provider_keeps_write_api_disabled(self):
        provider = WisartProvider(dummy_performer)
        ctx = ProviderContext(
            site="图片公益站",
            url="https://wisart.kuaileshifu.com/#/checkin",
            adapter_kind="browser",
            page=object(),
        )
        inspection = asyncio.run(provider.inspect(ctx))
        self.assertEqual(inspection.api_capability, "unknown")
        self.assertEqual(inspection.status_capability, "unknown")
        self.assertIn("write API disabled", " ".join(inspection.notes))

    def test_wisart_bridge_adds_provider_without_changing_result(self):
        provider = WisartProvider(dummy_performer)
        ctx = ProviderContext(
            site="图片公益站",
            url="https://wisart.kuaileshifu.com/#/checkin",
            adapter_kind="browser",
            page=object(),
        )
        result = asyncio.run(provider.perform(ctx))
        self.assertEqual(result.provider, "wisart")
        self.assertEqual(result.stage, "legacy")

    def test_models_contain_no_credential_payload_fields(self):
        fields = set(asdict(ActionEvidence(kind="none")))
        fields |= set(
            asdict(
                ConfirmationEvidence(
                    kind="dom_strong",
                    source="x",
                    summary="y",
                    observed_at="z",
                )
            )
        )
        forbidden = {"cookie", "authorization", "token", "headers", "body"}
        self.assertTrue(fields.isdisjoint(forbidden))

    def test_last_run_aggregates_evidence_and_failures(self):
        module = load_runner("provider_runner_test")
        with TemporaryDirectory() as td:
            original_run = module.LAST_RUN_STATUS
            original_attempt = module.LAST_ATTEMPT_STATUS
            module.LAST_RUN_STATUS = Path(td) / "last-run.json"
            module.LAST_ATTEMPT_STATUS = Path(td) / "last-attempt.json"
            try:
                ok = module.ok_result(
                    "OK",
                    confirmation=ConfirmationEvidence(
                        kind="server_status",
                        source="fixture",
                        summary="checked_in_today=true",
                        observed_at="2026-08-04T00:00:00",
                    ),
                )
                fail = module.fail_result("status_unconfirmed", site="wisart")
                module.write_last_run_status(
                    exit_code=1, cdp=None, results=[ok, fail], reason="done"
                )
                data = json.loads(module.LAST_RUN_STATUS.read_text())
                self.assertEqual(data["evidence_counts"], {"server_status": 1})
                self.assertEqual(data["failure_counts"], {"status_unconfirmed": 1})
            finally:
                module.LAST_RUN_STATUS = original_run
                module.LAST_ATTEMPT_STATUS = original_attempt

    def test_singleton_lock_rejects_second_holder(self):
        module = load_runner("provider_runner_lock_test")
        with TemporaryDirectory() as td:
            original = module.RUN_LOCK_PATH
            module.RUN_LOCK_PATH = Path(td) / "runner.lock"
            first = module.acquire_run_lock()
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(module.acquire_run_lock())
            finally:
                assert first is not None
                fcntl.flock(first.fileno(), fcntl.LOCK_UN)
                first.close()
                module.RUN_LOCK_PATH = original

    def test_browser_alive_rejects_half_dead_cdp(self):
        module = load_runner("provider_runner_alive_test")
        browser = type("Browser", (), {"contexts": [object()]})()
        original = module.http_call
        try:
            module.http_call = lambda *args, **kwargs: {}
            self.assertFalse(asyncio.run(module.browser_alive(browser)))
            module.http_call = lambda *args, **kwargs: {
                "webSocketDebuggerUrl": "ws://fixture/devtools/browser/1"
            }
            self.assertTrue(asyncio.run(module.browser_alive(browser)))
        finally:
            module.http_call = original

    def test_browser_alive_detects_broken_wire_cdp(self):
        module = load_runner("provider_runner_wire_alive_test")
        # Mock browser with new_browser_cdp_session that raises TargetClosedError (pipe broken)
        class BrokenSessionBrowser:
            def __init__(self):
                self.contexts = [object()]
            async def new_browser_cdp_session(self):
                raise RuntimeError("Target page, context or browser has been closed")

        browser = BrokenSessionBrowser()
        original = module.http_call
        try:
            module.http_call = lambda *args, **kwargs: {
                "webSocketDebuggerUrl": "ws://fixture/devtools/browser/1"
            }
            # Even though HTTP endpoint is responsive, broken wire probe must report False!
            self.assertFalse(asyncio.run(module.browser_alive(browser)))
        finally:
            module.http_call = original

    def test_browser_alive_passes_when_wire_cdp_healthy(self):
        module = load_runner("provider_runner_wire_ok_test")
        class HealthySession:
            async def detach(self):
                pass

        class HealthyBrowser:
            def __init__(self):
                self.contexts = [object()]
            async def new_browser_cdp_session(self):
                return HealthySession()

        browser = HealthyBrowser()
        original = module.http_call
        try:
            module.http_call = lambda *args, **kwargs: {
                "webSocketDebuggerUrl": "ws://fixture/devtools/browser/1"
            }
            self.assertTrue(asyncio.run(module.browser_alive(browser)))
        finally:
            module.http_call = original

    def test_owned_target_cleanup_closes_only_opener_descendants(self):
        module = load_runner("provider_runner_target_test")

        class Page:
            def __init__(self, target_id):
                self.target_id = target_id
                self.closed = False
            async def close(self):
                self.closed = True

        owned = Page("popup-owned")
        nested = Page("popup-nested")
        unrelated = Page("user-new-tab")
        browser = type("Browser", (), {
            "contexts": [type("Context", (), {"pages": [owned, nested, unrelated]})()]
        })()
        original_list = module.cdp_target_list
        original_id = module.page_cdp_target_id
        module.cdp_target_list = lambda: [
            {"id": "popup-owned", "openerId": "root"},
            {"id": "popup-nested", "openerId": "popup-owned"},
            {"id": "user-new-tab", "openerId": "some-user-tab"},
        ]
        async def fake_id(page):
            return page.target_id
        module.page_cdp_target_id = fake_id
        try:
            count = asyncio.run(
                module.close_extra_pages(
                    browser, set(), owned_root_target_id="root"
                )
            )
            self.assertEqual(count, 2)
            self.assertTrue(owned.closed)
            self.assertTrue(nested.closed)
            self.assertFalse(unrelated.closed)
        finally:
            module.cdp_target_list = original_list
            module.page_cdp_target_id = original_id


    def test_classify_page_block_detects_chrome_neterror_pages(self):
        module = load_runner("provider_runner_neterror_test")
        classify = module.classify_page_block

        # Chrome ERR_CONNECTION_CLOSED
        res1 = classify("无法访问此网站\n\nyuanshen.我爱你 意外终止了连接。\nERR_CONNECTION_CLOSED")
        self.assertIsNotNone(res1)
        self.assertEqual(res1[0], "upstream_unavailable")

        # Chrome ERR_TIMED_OUT
        res2 = classify("无法访问此网站\nmotomoto.lol 的响应时间过长。\nERR_TIMED_OUT\n重新加载")
        self.assertIsNotNone(res2)
        self.assertEqual(res2[0], "upstream_unavailable")

        # English Chrome net-error
        res3 = classify("This site can’t be reached\nERR_NAME_NOT_RESOLVED")
        self.assertIsNotNone(res3)
        self.assertEqual(res3[0], "upstream_unavailable")

    def test_extract_newapi_balance_parses_quota_correctly(self):
        module = load_runner("provider_runner_newapi_balance_test")
        class FakePage:
            async def evaluate(self, script):
                return {"quota": 1250000}
        page = FakePage()
        res = asyncio.run(module._extract_newapi_balance(page))
        self.assertIsNotNone(res)
        self.assertEqual(res.amount, 2.5)
        self.assertEqual(res.raw_text, "$2.50")
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.quota_raw, 1250000)
        self.assertEqual(res.source, "api")

    def test_extract_glados_balance_parses_status_api(self):
        module = load_runner("provider_runner_glados_balance_test")
        class FakePage:
            async def evaluate(self, script):
                return {"points": 420.0, "leftDays": "18"}
        page = FakePage()
        res = asyncio.run(module._extract_glados_balance(page))
        self.assertIsNotNone(res)
        self.assertEqual(res.amount, 420.0)
        self.assertEqual(res.raw_text, "420 积分 (剩 18 天)")
        self.assertEqual(res.currency, "POINTS")
        self.assertEqual(res.source, "api")

    def test_extract_dom_balance_parses_currency(self):
        module = load_runner("provider_runner_dom_balance_test")
        class FakePage:
            async def evaluate(self, script):
                return {"type": "money", "raw": "当前余额: ¥99.50", "val": "¥99.50"}
        page = FakePage()
        res = asyncio.run(module._extract_dom_balance(page))
        self.assertIsNotNone(res)
        self.assertEqual(res.amount, 99.5)
        self.assertEqual(res.currency, "CNY")
        self.assertEqual(res.source, "dom")

    def test_extract_balance_safely_handles_hang_with_timeout(self):
        module = load_runner("provider_runner_hang_balance_test")
        class HangingPage:
            async def evaluate(self, script):
                await asyncio.sleep(5)
                return {"quota": 1000}
        page = HangingPage()
        adapter = type("Adapter", (), {"kind": "newapi_profile", "url": "https://api.example.com/profile"})()
        # Should timeout at 3.0s and return None safely
        res = asyncio.run(module.extract_balance_safely(page, adapter))
        self.assertIsNone(res)

    def test_extract_balance_safely_skips_newapi_probe_on_browser_site(self):
        """P1: non-NewAPI sites (e.g. browser) must never be probed with /api/user/self."""
        module = load_runner("provider_runner_guard_test")
        probed_scripts = []
        class FakePage:
            async def evaluate(self, script):
                probed_scripts.append(script)
                return None
        page = FakePage()
        adapter = type("Adapter", (), {"kind": "browser", "url": "https://developer.huaweicloud.com/grow"})()
        asyncio.run(module.extract_balance_safely(page, adapter))
        # Ensure /api/user/self was NEVER evaluated on this browser page
        for s in probed_scripts:
            self.assertNotIn("/api/user/self", s)

    def test_extract_dom_balance_supports_cross_container_matching(self):
        """P2: DOM fallback supports split label and value structures from modern SPAs."""
        module = load_runner("provider_runner_dom_split_test")
        class FakePage:
            async def evaluate(self, script):
                return {"type": "money", "raw": "当前余额 150.00 元", "val": "150.00 元"}
        page = FakePage()
        res = asyncio.run(module._extract_dom_balance(page))
        self.assertIsNotNone(res)
        self.assertEqual(res.amount, 150.0)
        self.assertEqual(res.currency, "CNY")
        self.assertEqual(res.source, "dom")

    def test_extract_emrank_balance_parses_quota_and_reward(self):
        """EMRank extractor parses Token quota into M Token and extracts reward."""
        module = load_runner("provider_runner_emrank_balance_test")
        class FakePage:
            async def evaluate(self, script):
                return {
                    "amount": 246.735735,
                    "raw_text": "246.735735 M",
                    "rewardText": "到账 10 M Token",
                }
        page = FakePage()
        adapter = type("Adapter", (), {"name": "emrank", "kind": "browser", "url": "https://emrank.tech/account"})()
        res = asyncio.run(module.extract_balance_safely(page, adapter))
        self.assertIsNotNone(res)
        self.assertEqual(res.amount, 246.735735)
        self.assertEqual(res.raw_text, "246.735735 M")
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.source, "api")
        self.assertIn("10", res.reward_text)

    def test_extract_reward_emrank_units(self):
        """Reward extraction regex supports M Token and Token units."""
        module = load_runner("provider_runner_reward_test")
        r1 = module._extract_reward_from_text("签到成功，到账 10 M Token")
        self.assertEqual(r1, "+10 M Token")
        r2 = module._extract_reward_from_text("今日签到获得 5000000 Tokens")
        self.assertEqual(r2, "+5000000 Tokens")


if __name__ == "__main__":
    unittest.main(verbosity=2)

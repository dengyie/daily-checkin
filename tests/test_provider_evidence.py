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


if __name__ == "__main__":
    unittest.main(verbosity=2)

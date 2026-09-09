from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_runner(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "stealth_checkin_runner.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeLocator:
    def __init__(self):
        self.first = self

    async def click(self, **kwargs):
        return None

    async def evaluate(self, expression):
        return None

    async def count(self):
        return 0

    async def is_visible(self, **kwargs):
        return False


class FakePage:
    async def goto(self, *args, **kwargs): return None
    async def evaluate(self, *args, **kwargs): return "ready"


class FakeFrame:
    def __init__(self, url: str):
        self._url = url
        self._loc = FakeLocator()

    @property
    def url(self) -> str:
        return self._url

    def locator(self, sel):
        return self._loc


class FakePageWithFrames:
    def __init__(self, frames: list):
        self.frames = frames
        self._ctx = None

    @property
    def url(self):
        return self.frames[0].url if self.frames else ""

    @property
    def context(self):
        if self._ctx is None:
            class Ctx:
                async def cookies(self, urls=None):
                    return [{"name": "cf_clearance", "value": "x"*43}]
            self._ctx = Ctx()
        return self._ctx

    async def evaluate(self, *a, **k):
        return "ready"

    def locator(self, sel):
        return FakeLocator()


class ReviewFixTests(unittest.TestCase):
    def test_only_linuxdo_hosts_are_authorization_pages(self):
        m = load_runner("review_linuxdo_oauth_host")
        self.assertTrue(
            m.is_linuxdo_authorize_url(
                "https://connect.linux.do/oauth2/authorize?client_id=x"
            )
        )
        self.assertTrue(
            m.is_linuxdo_authorize_url(
                "https://linux.do/oauth/authorize?client_id=x"
            )
        )
        self.assertFalse(
            m.is_linuxdo_authorize_url(
                "https://free.vipclaude.codes/oauth/linuxdo"
            )
        )

    def test_sso_callback_upstream_failure_is_terminal(self):
        m = load_runner("review_sso_callback_upstream")
        self.assertEqual(
            m.sso_terminal_page_status(
                "Bad gateway Error code 502\nHost Error",
                "https://free.vipclaude.codes/oauth/linuxdo",
            ),
            "UPSTREAM_UNAVAILABLE",
        )
        self.assertIsNone(
            m.sso_terminal_page_status(
                "授权应用访问您的 LinuxDO 账户",
                "https://connect.linux.do/oauth2/authorize",
            )
        )

    def test_connection_closed_navigation_is_upstream_unavailable(self):
        m = load_runner("review_connection_closed")
        self.assertEqual(
            m.classify_navigation_error(
                "Page.goto: net::ERR_CONNECTION_CLOSED at "
                "https://welfare.0xpsyche.me/profile"
            ),
            "upstream_unavailable",
        )

    def test_goto_commit_timeout_is_upstream_unavailable(self):
        m = load_runner("review_navigation_timeout")
        message = "Page.goto: Timeout 30000ms exceeded."
        self.assertEqual(
            m.classify_navigation_error(
                message, "chrome-error://chromewebdata/"
            ),
            "upstream_unavailable",
        )
        self.assertEqual(
            m.classify_navigation_error(
                message, "https://slow.example/profile"
            ),
            "upstream_unavailable",
        )
        self.assertEqual(
            m.classify_navigation_error(
                "Locator.click: Timeout 3000ms exceeded.",
                "https://slow.example/profile",
            ),
            "error",
        )

    def test_explicit_prefer_port_outranks_environment(self):
        m = load_runner("review_prefer")
        old_probe = m._probe_cdp
        old_http = os.environ.get("CDP_HTTP")
        os.environ["CDP_HTTP"] = "http://127.0.0.1:9999"
        m._probe_cdp = lambda port: {"port": port, "http": f"http://127.0.0.1:{port}", "headless": False}
        try:
            result = m.discover_cdp_endpoint(prefer_port=9333, strict_prefer=True)
            self.assertEqual(result["port"], 9333)
            self.assertEqual(result["reason"], "prefer_port")
        finally:
            m._probe_cdp = old_probe
            if old_http is None: os.environ.pop("CDP_HTTP", None)
            else: os.environ["CDP_HTTP"] = old_http

    def test_checkin_profile_is_hard_preferred_over_newer_regular_chrome(self):
        m = load_runner("review_profile")
        old_ps, old_probe = m._ps_chrome_debug_candidates, m._probe_cdp
        m._ps_chrome_debug_candidates = lambda: [
            {"pid": 1, "port": 9222, "headless": False, "user_data_dir": "/Users/x/chrome-checkin-profile", "started": 100, "score": 600},
            {"pid": 2, "port": 9333, "headless": False, "user_data_dir": "/Users/x/Default", "started": 999, "score": 100},
        ]
        m._probe_cdp = lambda port: {"port": port, "http": f"http://127.0.0.1:{port}", "headless": False, "pages": 1}
        try:
            result = m.discover_cdp_endpoint()
            self.assertEqual(result["port"], 9222)
        finally:
            m._ps_chrome_debug_candidates, m._probe_cdp = old_ps, old_probe

    def test_find_temp_page_never_binds_unproven_blank(self):
        m = load_runner("review_target")
        page = type("Page", (), {"url": "about:blank"})()
        browser = type("Browser", (), {"contexts": [type("Context", (), {"pages": [page]})()]})()
        old_id = m.page_cdp_target_id
        async def no_id(_): return None
        m.page_cdp_target_id = no_id
        async def run():
            old_sleep = m.asyncio.sleep
            async def fast_sleep(_): return None
            m.asyncio.sleep = fast_sleep
            try: return await m.find_temp_page(browser, "ours", set())
            finally: m.asyncio.sleep = old_sleep
        try:
            self.assertIsNone(asyncio.run(run()))
        finally:
            m.page_cdp_target_id = old_id

    def test_live_click_evidence_and_post_click_done_is_ok(self):
        m = load_runner("review_action")
        adapter = m.SiteAdapter("A", "https://a.example", sign_selectors=["button"], already_selectors=["done"])
        page = FakePage()
        old = {name: getattr(m, name) for name in (
            "wait_text_ready", "wait_out_cloudflare", "maybe_sso_if_needed", "page_text",
            "already_done", "wait_out_captcha", "wait_for_any_visible", "is_valid_checkin_confirm",
        )}
        calls = {"already": 0}
        async def none(*a, **k): return None
        async def text(*a, **k): return "ready"
        async def already(*a, **k):
            calls["already"] += 1
            return "btn:今日已签到" if calls["already"] >= 2 else None
        async def any_visible(*a, **k): return FakeLocator(), "button:has-text(立即签到)"
        m.wait_text_ready = none
        m.wait_out_cloudflare = none
        m.maybe_sso_if_needed = none
        m.page_text = text
        m.already_done = already
        m.wait_out_captcha = none
        m.wait_for_any_visible = any_visible
        m.is_valid_checkin_confirm = lambda _: False
        try:
            result = asyncio.run(m.legacy_checkin_on_page(page, adapter))
            self.assertEqual(result.status, "OK")
            self.assertEqual(result.action.kind, "dom_click")
            self.assertIn("立即签到", result.action.target)
            self.assertEqual(result.confirmation.kind, "dom_done_state")
            self.assertEqual(result.pre_state, "PENDING")
            self.assertEqual(result.post_state, "DONE")
            self.assertEqual(result.transition, "PENDING_TO_DONE")
            self.assertEqual(result.attribution, "runner")
        finally:
            for name, value in old.items(): setattr(m, name, value)

    def test_provider_engine_has_runtime_legacy_rollback(self):
        m = load_runner("review_rollback")
        old_engine = m.PROVIDER_ENGINE
        old_legacy = m.legacy_checkin_on_page
        async def legacy(*args, **kwargs):
            return m.ok_result("ALREADY", detail="already")
        m.PROVIDER_ENGINE = "legacy"
        m.legacy_checkin_on_page = legacy
        try:
            result = asyncio.run(m.checkin_on_page(object(), m.SiteAdapter("A", "https://a.example")))
            self.assertEqual(result.provider, "legacy_browser")
            self.assertEqual(result.status, "FAIL")
            self.assertEqual(result.reason, "unattributed_success")
            self.assertIsNone(result.confirmation)
        finally:
            m.PROVIDER_ENGINE = old_engine
            m.legacy_checkin_on_page = old_legacy

    def test_native_click_records_native_action_and_disables_overlay(self):
        m = load_runner("review_native_action")
        adapter = m.resolve_site("wxiai", "https://api.wxiai.com/workspace")
        self.assertTrue(adapter.use_native_click)
        self.assertFalse(adapter.use_overlay_zapper)
        src = (ROOT / "stealth_checkin_runner.py").read_text(encoding="utf-8")
        self.assertIn('kind="native_click" if adapter.use_native_click else "dom_click"', src)

    def test_native_click_uses_element_evaluate_not_locator_click(self):
        m = load_runner("review_native_behavior")
        adapter = m.SiteAdapter(
            "wxiai", "https://api.wxiai.com/workspace",
            sign_selectors=["button"], already_selectors=["done"],
            use_native_click=True, use_overlay_zapper=False,
        )
        calls = []

        class Locator:
            async def click(self, **kwargs):
                calls.append("locator.click")
                raise AssertionError("native path used locator click")
            async def evaluate(self, expression):
                calls.append(expression)

        class Page(FakePage):
            pass

        old = {name: getattr(m, name) for name in (
            "wait_text_ready", "wait_out_cloudflare", "maybe_sso_if_needed", "page_text",
            "already_done", "wait_out_captcha", "wait_for_any_visible", "is_valid_checkin_confirm",
        )}
        async def none(*a, **k): return None
        async def text(*a, **k): return "ready"
        async def any_visible(*a, **k): return Locator(), "button:has-text(立即签到)"
        m.wait_text_ready = none
        m.wait_out_cloudflare = none
        m.maybe_sso_if_needed = none
        m.page_text = text
        m.already_done = none
        m.wait_out_captcha = none
        m.wait_for_any_visible = any_visible
        m.is_valid_checkin_confirm = lambda _: True
        try:
            result = asyncio.run(m.legacy_checkin_on_page(Page(), adapter))
            self.assertEqual(result.action.kind, "native_click")
            self.assertIn("(el) => el.click()", calls)
            self.assertNotIn("locator.click", calls)
        finally:
            for name, value in old.items(): setattr(m, name, value)

    def test_action_is_preserved_when_post_click_path_raises(self):
        m = load_runner("review_action_exception")
        adapter = m.SiteAdapter("A", "https://a.example", sign_selectors=["button"])

        class RaisingLocator(FakeLocator):
            async def click(self, **kwargs):
                raise RuntimeError("click failed")

        class RaisingPage(FakePage):
            async def evaluate(self, *args, **kwargs):
                return None

        old = {name: getattr(m, name) for name in (
            "wait_text_ready", "wait_out_cloudflare", "maybe_sso_if_needed", "page_text",
            "already_done", "wait_out_captcha", "wait_for_any_visible", "is_valid_checkin_confirm",
        )}
        async def none(*a, **k): return None
        async def text(*a, **k): return "ready"
        async def any_visible(*a, **k): return RaisingLocator(), "button"
        m.wait_text_ready = none
        m.wait_out_cloudflare = none
        m.maybe_sso_if_needed = none
        m.page_text = text
        m.already_done = none
        m.wait_out_captcha = none
        m.wait_for_any_visible = any_visible
        m.is_valid_checkin_confirm = lambda _: False
        try:
            result = asyncio.run(m.legacy_checkin_on_page(RaisingPage(), adapter))
            self.assertEqual(result.status, "FAIL")
            self.assertIsNotNone(result.action)
            self.assertEqual(result.action.kind, "dom_click")
        finally:
            for name, value in old.items(): setattr(m, name, value)

    def test_post_click_business_restriction_is_not_no_confirm(self):
        m = load_runner("review_post_click_block")
        adapter = m.SiteAdapter(
            "A", "https://a.example/profile", kind="newapi_profile",
            sign_selectors=["button"], already_selectors=["done"],
        )
        state = {"clicked": False}

        class Locator(FakeLocator):
            async def click(self, **kwargs):
                state["clicked"] = True

        old = {name: getattr(m, name) for name in (
            "wait_text_ready", "wait_out_cloudflare", "maybe_sso_if_needed", "page_text",
            "page_url", "already_done", "wait_out_captcha", "wait_for_any_visible",
            "is_valid_checkin_confirm", "classify_page_block",
        )}
        async def none(*a, **k): return None
        async def text(*a, **k): return "今日还差 ¥5.00 当天需消耗 ¥5.00 永久余额"
        async def url(*a, **k): return "https://a.example/profile"
        async def visible(*a, **k): return Locator(), "button:has-text(立即签到)"
        original_classifier = m.classify_page_block
        def classify(body, page_url=""):
            return original_classifier(body, page_url) if state["clicked"] else None
        m.wait_text_ready = none
        m.wait_out_cloudflare = none
        m.maybe_sso_if_needed = none
        m.page_text = text
        m.page_url = url
        m.already_done = none
        m.wait_out_captcha = none
        m.wait_for_any_visible = visible
        m.is_valid_checkin_confirm = lambda _: False
        m.classify_page_block = classify
        try:
            result = asyncio.run(m.legacy_checkin_on_page(FakePage(), adapter))
            self.assertEqual(result.status, "FAIL")
            self.assertEqual(result.reason, "business_ineligible")
            self.assertIsNotNone(result.action)
        finally:
            for name, value in old.items(): setattr(m, name, value)

    def test_post_click_captcha_wait_runs_once_per_action(self):
        m = load_runner("review_single_captcha_wait")
        adapter = m.SiteAdapter(
            "A", "https://a.example/profile", kind="newapi_profile",
            already_selectors=["done"],
        )
        calls = {"wait": 0}

        async def blocking(*args, **kwargs):
            return True

        async def cleared(*args, **kwargs):
            calls["wait"] += 1
            return None

        async def text(*args, **kwargs):
            return "dashboard without confirmation"

        async def no_already(*args, **kwargs):
            return ""

        async def fast_sleep(*args, **kwargs):
            return None

        old = {name: getattr(m, name) for name in (
            "captcha_blocking", "wait_out_captcha", "page_text", "already_done",
        )}
        old_sleep = m.asyncio.sleep
        m.captcha_blocking = blocking
        m.wait_out_captcha = cleared
        m.page_text = text
        m.already_done = no_already
        m.asyncio.sleep = fast_sleep
        try:
            action = m.ActionEvidence(kind="dom_click", target="button")
            result = asyncio.run(
                m._click_cta_and_confirm(
                    FakePage(), FakeLocator(), "button", adapter, action
                )
            )
            self.assertEqual(result.reason, "interactive")
            self.assertEqual(calls["wait"], 1)
        finally:
            for name, value in old.items(): setattr(m, name, value)
            m.asyncio.sleep = old_sleep

    def test_turnstile_timeout_never_clicks_business_cta(self):
        m = load_runner("review_turnstile_no_blind_click")
        adapter = m.SiteAdapter("A", "https://a.example/checkin")
        calls = {"click": 0}

        class Locator(FakeLocator):
            async def click(self, **kwargs):
                calls["click"] += 1

        async def token_timeout(*args, **kwargs):
            return False

        old_wait = m.wait_turnstile_token
        m.wait_turnstile_token = token_timeout
        try:
            action = m.ActionEvidence(kind="dom_click", target="button")
            result = asyncio.run(
                m._click_cta_and_confirm(
                    FakePage(), Locator(), "button", adapter, action
                )
            )
            self.assertEqual(result.reason, "interactive")
            self.assertEqual(result.stage, "protection")
            self.assertEqual(calls["click"], 0)
        finally:
            m.wait_turnstile_token = old_wait

    def test_604020_catalog_marks_missing_checkin_capability(self):
        m = load_runner("review_604020_capability")
        adapter = m.resolve_site("604020", "https://search.604020.xyz/")
        self.assertTrue(adapter.feature_unavailable_reason)

        class Page(FakePage):
            url = "https://search.604020.xyz/"

        async def none(*args, **kwargs):
            return None

        async def false(*args, **kwargs):
            return False

        async def no_already(*args, **kwargs):
            return ""

        async def no_visible(*args, **kwargs):
            return None, None

        async def dashboard(*args, **kwargs):
            return (
                "Searchix 控制台 令牌 使用日志 充值中心 个人设置 "
                "接入指南 USERNAME 永久 $2.08 今日 $2.05"
            )

        old = {name: getattr(m, name) for name in (
            "wait_text_ready", "wait_out_cloudflare", "maybe_sso_if_needed",
            "page_text", "already_done", "wait_out_captcha", "captcha_blocking",
            "wait_for_any_visible", "first_visible",
        )}
        m.wait_text_ready = none
        m.wait_out_cloudflare = none
        m.maybe_sso_if_needed = none
        m.page_text = dashboard
        m.already_done = no_already
        m.wait_out_captcha = none
        m.captcha_blocking = false
        m.wait_for_any_visible = no_visible
        m.first_visible = no_visible
        try:
            result = asyncio.run(m.legacy_checkin_on_page(Page(), adapter))
            self.assertEqual(result.reason, "feature_unavailable")
            self.assertEqual(result.detail, adapter.feature_unavailable_reason)
        finally:
            for name, value in old.items(): setattr(m, name, value)

    def test_health_metadata_failure_never_demotes_business_result(self):
        m = load_runner("review_health_best_effort")
        result = m.ok_result("OK", site="A", detail="签到成功")

        class BrokenStore:
            def record_site_health(self, *args):
                raise RuntimeError("database is locked")

        self.assertFalse(m.record_site_health_best_effort(BrokenStore(), "A", result))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.detail, "签到成功")

    def test_custom_log_dir_owns_jsonl_last_run_and_lock(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("DAILY_CHECKIN_LOG_DIR")
            os.environ["DAILY_CHECKIN_LOG_DIR"] = td
            try:
                m = load_runner("review_custom_log_dir")
                root = Path(td)
                self.assertEqual(m.CHECKIN_LOG_DIR, root)
                self.assertEqual(m.LAST_RUN_STATUS, root / "last-run.json")
                self.assertEqual(m.RUN_LOCK_PATH, root / "daily-checkin.lock")
                m.append_checkin_log(m.fail_result("fixture", site="A"), "2099-01-01")
                m.write_last_run_status(
                    exit_code=0, cdp=None, results=[], reason="dry_run"
                )
                self.assertTrue((root / "2099-01-01.jsonl").is_file())
                payload = json.loads((root / "last-run.json").read_text())
                self.assertEqual(payload["reason"], "dry_run")
            finally:
                if old is None:
                    os.environ.pop("DAILY_CHECKIN_LOG_DIR", None)
                else:
                    os.environ["DAILY_CHECKIN_LOG_DIR"] = old

    def test_append_checkin_log_uses_field_allowlist_and_trims(self):
        """Log payload is allowlist-only (no asdict), latency_ms is int, long fields trim."""
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("DAILY_CHECKIN_LOG_DIR")
            os.environ["DAILY_CHECKIN_LOG_DIR"] = td
            try:
                m = load_runner("review_log_allowlist")
                result = m.ok_result(
                    "OK", site="A", detail="x" * 500, latency_ms=123,
                    provider="wisart",
                )
                m.append_checkin_log(result, "2099-01-02")
                line = (Path(td) / "2099-01-02.jsonl").read_text().strip()
                payload = json.loads(line)
                # HARD-CODED expected key set (NOT derived from
                # _LOG_SCALAR_FIELDS): if someone adds a junk field to the
                # allowlist OR reverts to asdict(), this comparison fails.
                # (Set-equality against set(m._LOG_SCALAR_FIELDS) would be
                # self-oracle — allowlist is a mirror of the dataclass.)
                self.assertEqual(
                    set(payload),
                    {
                        "status", "reason", "detail", "site", "adapter",
                        "marked", "provider", "stage", "pre_state", "post_state",
                        "transition", "attribution", "business_status",
                        "business_reason", "projection_status",
                        "projection_reason", "latency_ms", "action",
                        "confirmation", "identity", "ts",
                    },
                )
                self.assertEqual(payload["latency_ms"], 123)
                self.assertIsInstance(payload["latency_ms"], int)
                self.assertEqual(payload["provider"], "wisart")
                self.assertEqual(len(payload["detail"]), m._LOG_MAX_FIELD_LEN)
                # Type guard: allowlist scalars are str/None; evidence is dict/None.
                for key in set(payload) - {"action", "confirmation", "identity", "ts"}:
                    self.assertNotIsInstance(payload[key], (dict, list),
                                             f"{key} is a nested object in the log (leak?)")
                for key in ("action", "confirmation", "identity"):
                    self.assertTrue(payload[key] is None or isinstance(payload[key], dict))
            finally:
                if old is None:
                    os.environ.pop("DAILY_CHECKIN_LOG_DIR", None)
                else:
                    os.environ["DAILY_CHECKIN_LOG_DIR"] = old

    def test_append_checkin_log_evidence_goes_through_evidence_dict(self):
        """Nested action/confirmation/identity are type-guarded evidence_dict output."""
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("DAILY_CHECKIN_LOG_DIR")
            os.environ["DAILY_CHECKIN_LOG_DIR"] = td
            try:
                m = load_runner("review_log_evidence")
                from checkin_core.models import ActionEvidence
                result = m.ok_result(
                    "OK", site="A",
                    action=ActionEvidence(kind="dom_click", target="btn", attempted_at="t"),
                )
                m.append_checkin_log(result, "2099-01-03")
                payload = json.loads((Path(td) / "2099-01-03.jsonl").read_text().strip())
                self.assertEqual(payload["action"]["kind"], "dom_click")
                self.assertEqual(payload["action"]["target"], "btn")
                self.assertIsNone(payload["confirmation"])
                self.assertIsNone(payload["identity"])
            finally:
                if old is None:
                    os.environ.pop("DAILY_CHECKIN_LOG_DIR", None)
                else:
                    os.environ["DAILY_CHECKIN_LOG_DIR"] = old

    def test_retry_auto_fail_falls_back_to_db_failed_rows(self):
        """Without Obsidian projection, --retry-auto-fail retries today's DB failed rows."""
        from checkin_core.store import SystemStore
        with tempfile.TemporaryDirectory() as td:
            store = SystemStore(Path(td) / "system.db")
            store.upsert_task("2026-08-19", "A", "https://a.example")
            store.upsert_task("2026-08-19", "B", "https://b.example")
            store.upsert_task("2026-08-19", "C", "https://c.example")
            store.set_task_result("2026-08-19", "A", "failed", "no_confirm")
            store.set_task_result("2026-08-19", "B", "done")
            # C stays pending.
            m = load_runner("review_db_failed_rows")
            system_rows = m.select_db_failed_rows(
                store.tasks("2026-08-19", pending_only=False)
            )
            self.assertEqual([r["name"] for r in system_rows], ["A"])

    def test_dry_run_writes_attempt_without_business_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            m = load_runner("review_attempt_split")
            m.LAST_RUN_STATUS = Path(td) / "last-run.json"
            m.LAST_ATTEMPT_STATUS = Path(td) / "last-attempt.json"
            m.write_last_run_status(
                exit_code=0, cdp=None, results=[], reason="dry_run",
                source="cron", mode="dry_run", update_business_run=False,
            )
            self.assertFalse(m.LAST_RUN_STATUS.exists())
            self.assertEqual(
                json.loads(m.LAST_ATTEMPT_STATUS.read_text())["mode"], "dry_run"
            )

    def test_dry_run_early_exit_writes_custom_last_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = root / "tasks"
            logs = root / "logs"
            tasks.mkdir()
            day = datetime.now().strftime("%Y-%m-%d")
            (tasks / f"{day} 每日任务.md").write_text(
                "- [ ] #task #日常 [fixture](https://fixture.example/profile)\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env.update({
                "DAILY_CHECKIN_TASKS_DIR": str(tasks),
                "DAILY_CHECKIN_DB": str(root / "system.db"),
                "DAILY_CHECKIN_LOG_DIR": str(logs),
            })
            result = subprocess.run(
                [sys.executable, str(ROOT / "stealth_checkin_runner.py"), "--dry-run"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            payload = json.loads((logs / "last-attempt.json").read_text())
            self.assertEqual(payload["reason"], "dry_run")
            self.assertEqual(payload["exit"], 0)
            self.assertEqual(payload["total"], 0)
            self.assertFalse((logs / "last-run.json").exists())

    def test_dry_run_with_no_pending_tasks_does_not_replace_business_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = root / "tasks"
            logs = root / "logs"
            tasks.mkdir()
            logs.mkdir()
            day = datetime.now().strftime("%Y-%m-%d")
            (tasks / f"{day} 每日任务.md").write_text(
                "- [x] #task #日常 [fixture](https://fixture.example/profile)\n",
                encoding="utf-8",
            )
            business = b'{"reason":"done","total":75}\n'
            (logs / "last-run.json").write_bytes(business)
            env = dict(os.environ)
            env.update({
                "DAILY_CHECKIN_TASKS_DIR": str(tasks),
                "DAILY_CHECKIN_DB": str(root / "system.db"),
                "DAILY_CHECKIN_LOG_DIR": str(logs),
            })
            result = subprocess.run(
                [sys.executable, str(ROOT / "stealth_checkin_runner.py"), "--dry-run"],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            attempt = json.loads((logs / "last-attempt.json").read_text())
            self.assertEqual((attempt["reason"], attempt["mode"]), ("dry_run", "dry_run"))
            self.assertEqual((logs / "last-run.json").read_bytes(), business)

    def test_dry_run_uses_sqlite_tasks_when_obsidian_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / "logs"
            db_path = root / "system.db"
            day = datetime.now().strftime("%Y-%m-%d")
            from checkin_core.store import SystemStore

            SystemStore(db_path).upsert_task(
                day, "fixture", "https://fixture.example/profile", status="pending"
            )
            env = dict(os.environ)
            env.update({
                "DAILY_CHECKIN_TASKS_DIR": str(root / "missing-tasks"),
                "DAILY_CHECKIN_DB": str(db_path),
                "DAILY_CHECKIN_LOG_DIR": str(logs),
            })
            result = subprocess.run(
                [sys.executable, str(ROOT / "stealth_checkin_runner.py"), "--dry-run"],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("Obsidian projection unavailable; continuing with SQLite", result.stdout)
            self.assertIn("Pending sites: 1", result.stdout)
            self.assertIn("fixture [newapi_profile]", result.stdout)

    def test_targeted_scope_never_replaces_full_run_summary(self):
        m = load_runner("review_targeted_scope")
        full_args = m.parse_cli_args([])
        only_args = m.parse_cli_args(["--only", "A,B"])
        retry_args = m.parse_cli_args(["--retry-auto-fail"])
        task_args = m.parse_cli_args(["--task-file", "/tmp/probe.md"])
        self.assertEqual(m.classify_run_scope(full_args, set()), ("full", []))
        self.assertEqual(
            m.classify_run_scope(only_args, {"A", "B"}),
            ("targeted", ["A", "B"]),
        )
        self.assertEqual(m.classify_run_scope(retry_args, set())[0], "targeted")
        self.assertEqual(m.classify_run_scope(task_args, set())[0], "targeted")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m.LAST_ATTEMPT_STATUS = root / "last-attempt.json"
            m.LAST_RUN_STATUS = root / "last-run.json"
            full = b'{"scope":"full","total":75}\n'
            m.LAST_RUN_STATUS.write_bytes(full)
            result = m.ok_result("OK", site="A", detail="领取成功")
            m.write_last_run_status(
                exit_code=0, cdp=None, results=[result],
                reason="done", source="cli", scope="targeted",
                target_sites=["A"], update_business_run=False,
            )
            attempt = json.loads(m.LAST_ATTEMPT_STATUS.read_text())
            self.assertEqual(attempt["scope"], "targeted")
            self.assertEqual(attempt["target_sites"], ["A"])
            self.assertEqual(m.LAST_RUN_STATUS.read_bytes(), full)

    def test_web_full_run_never_replaces_cron_summary(self):
        """A web-triggered full batch is a manual rerun and must not mask the
        daily cron summary in last-run.json, even though its scope is 'full'."""
        m = load_runner("review_web_full")
        self.assertTrue(m.should_update_business_run("full", "cron"))
        self.assertTrue(m.should_update_business_run("full", "cli"))
        self.assertFalse(m.should_update_business_run("full", "web"))
        self.assertFalse(m.should_update_business_run("targeted", "web"))
        self.assertFalse(m.should_update_business_run("targeted", "cron"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m.LAST_ATTEMPT_STATUS = root / "last-attempt.json"
            m.LAST_RUN_STATUS = root / "last-run.json"
            cron_summary = b'{"scope":"full","source":"cron","total":75}\n'
            m.LAST_RUN_STATUS.write_bytes(cron_summary)
            result = m.ok_result("OK", site="A", detail="领取成功")
            web_scope = "full"
            m.write_last_run_status(
                exit_code=0, cdp=None, results=[result],
                reason="done", source="web", scope=web_scope,
                target_sites=[],
                update_business_run=m.should_update_business_run(web_scope, "web"),
            )
            self.assertEqual(m.LAST_RUN_STATUS.read_bytes(), cron_summary)
            attempt = json.loads(m.LAST_ATTEMPT_STATUS.read_text())
            self.assertEqual(attempt["scope"], "full")

    def test_projection_unavailable_counts_include_failed_business_runs(self):
        """projection_failure_counts must remain symmetric: a FAIL where the
        durable sink is unavailable is also a failed projection (previously only
        successful business runs were counted, hiding 56 of 79 rows)."""
        m = load_runner("review_proj_sym")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m.LAST_ATTEMPT_STATUS = root / "last-attempt.json"
            m.LAST_RUN_STATUS = root / "last-run.json"
            fail_ok = m.ok_result("OK", site="x", detail="ok")
            fail_ok.projection_status = "failed"
            fail_ok.projection_reason = "task_projection_unavailable:/no/tasks"
            failed = m.fail_result("no_button", site="y")
            failed.projection_status = "failed"
            failed.projection_reason = "task_projection_unavailable:/no/tasks"
            m.write_last_run_status(
                exit_code=1, cdp=None, results=[fail_ok, failed],
                reason="fail", source="cron", scope="full",
            )
            payload = json.loads(m.LAST_RUN_STATUS.read_text())
            self.assertEqual(
                payload["projection_failure_counts"]["task_projection_unavailable"], 2
            )

    def test_hard_cdp_failure_overrides_prior_success(self):
        m = load_runner("review_hard_error")
        results = [m.ok_result("OK", site="first", detail="confirmed")]
        exit_code, reason = m.finalize_batch_status(
            results, (2, "cdp_lost:websocket reset")
        )
        self.assertEqual(exit_code, 2)
        self.assertTrue(reason.startswith("cdp_lost:"))

    def test_run_set_excludes_catalog_sites_missing_from_task_file(self):
        """materialize_day adds every enabled catalog site as pending, but the
        projection sink (task file) has no row for them — running them would
        trip task_projection_missing and abort the batch. The run set must be
        restricted to names present in the file."""
        m = load_runner("review_sink_align")
        src = (ROOT / "stealth_checkin_runner.py").read_text(encoding="utf-8")
        run_body = src.split("async def run(", 1)[1]
        # filter applied to the pending system_rows before building adapters
        self.assertIn("file_task_names", run_body)
        self.assertIn(
            'system_rows = [r for r in system_rows if r.get("name") in file_task_names]',
            run_body,
        )
        # behavior: parse_open_tasks drives the allowed set
        content = (
            "- [ ] #task #日常 [A](https://a)\n"
            "- [x] #task #日常 [B](https://b)\n"
        )
        names = {n for n, _ in m.parse_open_tasks(content)}
        self.assertEqual(names, {"A"})  # B done → not open → not eligible

    def test_explicit_cdp_probes_once_without_rediscovery(self):
        """An explicit --cdp must probe that exact endpoint, not re-rank all
        Chrome instances (which could select a different one after cron's
        preflight already fixed the endpoint)."""
        src = (ROOT / "stealth_checkin_runner.py").read_text(encoding="utf-8")
        run_body = src.split("async def run(", 1)[1]
        cdp_branch = run_body.split("if args.cdp:", 1)[1].split("else:", 1)[0]
        self.assertIn("_probe_cdp_http(http)", cdp_branch)
        self.assertNotIn("discover_cdp_endpoint(", cdp_branch)

    def test_passive_turnstile_frame_does_not_block_enabled_cta(self):
        """Turnstile challenge frames load their URL internally; the iframe's
        DOM src does not contain 'turnstile'. The frame must be detected, but a
        passive token widget must not enter the 40s human captcha gate before
        wait_turnstile_token gets a chance to poll it."""
        m = load_runner("review_turnstile")
        page = FakePageWithFrames([
            FakeFrame("https://check.verydio.com/"),
            FakeFrame(
                "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/"
                "h/b/turnstile/f/av0/0x4AAAAAA"
            ),
        ])
        self.assertTrue(asyncio.run(m.captcha_dom_present(page)))
        self.assertFalse(asyncio.run(m.captcha_blocking(page)))

    def test_hcaptcha_frame_remains_blocking(self):
        m = load_runner("review_hcaptcha")
        page = FakePageWithFrames([
            FakeFrame("https://checkin.new-api.abrdns.com/checkin"),
            FakeFrame("https://newassets.hcaptcha.com/captcha/v1/fixture/static/hcaptcha.html"),
        ])
        self.assertTrue(asyncio.run(m.captcha_dom_present(page)))
        self.assertTrue(asyncio.run(m.captcha_blocking(page)))

    def test_image_captcha_modal_is_interactive(self):
        m = load_runner("review_image_captcha_modal")
        text = "每日签到\n立即签到\n安全验证\n刷新验证码\n取消\n确认"
        self.assertTrue(m.has_interactive_captcha(text))

    def test_image_captcha_modal_dom_blocks_when_body_preview_is_truncated(self):
        m = load_runner("review_image_captcha_dom")

        class Locator(FakeLocator):
            def __init__(self, present=False):
                self.present = present
                self.first = self

            async def count(self):
                return 1 if self.present else 0

        class Page(FakePageWithFrames):
            def __init__(self):
                super().__init__([])

            async def evaluate(self, *args, **kwargs):
                return "dashboard preview without modal text"

            def locator(self, selector):
                return Locator(
                    "安全验证" in selector and "刷新验证码" in selector
                )

        self.assertTrue(asyncio.run(m.captcha_blocking(Page())))

    def test_shield_pow_challenge_is_clicked(self):
        m = load_runner("review_shield_pow")
        calls = []

        class Locator(FakeLocator):
            def __init__(self, selector):
                self.selector = selector
                self.first = self

            async def is_visible(self, **kwargs):
                return self.selector == ".pow-icon"

            async def click(self, **kwargs):
                calls.append(self.selector)

        class Page:
            frames = []

            def locator(self, selector):
                return Locator(selector)

        self.assertTrue(asyncio.run(m.try_click_challenge_widgets(Page())))
        self.assertEqual(calls, [".pow-icon"])

    def test_shield_pow_dom_is_blocking_until_solved(self):
        m = load_runner("review_shield_pow_blocking")

        class Locator(FakeLocator):
            def __init__(self, selector):
                self.selector = selector
                self.first = self

            async def count(self):
                return 1 if ".pow-captcha" in self.selector else 0

        class Page(FakePageWithFrames):
            def __init__(self):
                super().__init__([])

            async def evaluate(self, *args, **kwargs):
                return "dashboard preview without challenge text"

            def locator(self, selector):
                return Locator(selector)

        self.assertTrue(asyncio.run(m.captcha_blocking(Page())))

    def test_cf_clearance_cookie_detected(self):
        m = load_runner("review_clearance")
        page = FakePageWithFrames([FakeFrame("http://fapi.leileihog.top/profile")])
        self.assertTrue(asyncio.run(m.has_cf_clearance(page)))

    def test_cf_clearance_cookie_is_scoped_to_current_site(self):
        m = load_runner("review_clearance_scope")
        requested = []

        class Ctx:
            async def cookies(self, urls=None):
                requested.extend(urls or [])
                return []

        class Page(FakePageWithFrames):
            url = "https://current.example/profile"

        page = Page([FakeFrame("https://current.example/profile")])
        page._ctx = Ctx()
        self.assertFalse(asyncio.run(m.has_cf_clearance(page)))
        self.assertEqual(requested, ["https://current.example/profile"])

    def test_no_turnstile_frame_not_blocking(self):
        m = load_runner("review_noturnstile")
        page = FakePageWithFrames([FakeFrame("http://fapi.leileihog.top/profile")])
        # no challenge frame, no cf cookie in this variant
        class NoCookieCtx:
            async def cookies(self, urls=None):
                return [{"name": "session", "value": "abc"}]
        page._ctx = NoCookieCtx()
        self.assertFalse(asyncio.run(m.captcha_dom_present(page)))


class PersistenceContractTests(unittest.TestCase):
    """Real fake-store behavior: each site persisted exactly once with the
    correct projection_status / projection_reason, independent of the
    business outcome."""

    def _harness(self):
        m = load_runner("review_persist")
        calls = {"log": [], "item": [], "results": [], "task": []}

        def fake_log(result, day=None):
            calls["log"].append(result)
        def fake_item(run_id, result):
            calls["item"].append(result)
        def fake_task(day, name, status, reason=""):
            calls["task"].append((name, status, reason))

        old_log, old_item = m.append_checkin_log, None
        m.append_checkin_log = fake_log
        return m, calls, fake_item, fake_task, old_log

    def _simulate(self, m, calls, fake_item, fake_task, result, apply_ret):
        """Mirror the batch tail: apply projection then persist exactly once."""
        run_id = 1
        today = "2026-08-10"
        name = result.site
        if result.ok:
            content, changed = apply_ret
            if changed:
                result.marked = True
                result.projection_status = "succeeded"
                result.projection_reason = ""
                fake_task(today, name, "done")
            else:
                result.projection_status = "failed"
                result.projection_reason = f"task_projection_missing:{name}"
                result.business_status = result.status
                result.business_reason = result.reason
                fake_task(today, name, "done")
        else:
            fch = apply_ret[1]
            if fch:
                result.projection_status = "succeeded"
                result.projection_reason = ""
            else:
                result.projection_status = "failed"
                result.projection_reason = f"task_projection_missing:{name}"
            fake_task(today, name, "failed", result.reason or "fail")
        m.append_checkin_log(result, today)
        fake_item(run_id, result)
        calls["results"].append(result)
        return result

    def test_success_projection_success_writes_once_done(self):
        m, calls, fake_item, fake_task, _ = self._harness()
        r = m.ok_result("OK", site="A", detail="confirmed")
        self._simulate(m, calls, fake_item, fake_task, r, ("content", True))
        self.assertEqual(len(calls["log"]), 1)
        self.assertEqual(len(calls["item"]), 1)
        self.assertEqual(len(calls["results"]), 1)
        self.assertEqual(r.projection_status, "succeeded")
        self.assertEqual(r.projection_reason, "")
        self.assertEqual(calls["task"], [("A", "done", "")])

    def test_success_projection_failure_preserves_business_success(self):
        m, calls, fake_item, fake_task, _ = self._harness()
        r = m.ok_result("OK", site="A", detail="confirmed")
        out = self._simulate(m, calls, fake_item, fake_task, r, ("content", False))
        self.assertEqual(len(calls["log"]), 1)
        self.assertEqual(len(calls["item"]), 1)
        self.assertEqual(len(calls["results"]), 1)
        self.assertEqual(out.status, "OK")
        self.assertEqual(out.projection_status, "failed")
        self.assertEqual(out.projection_reason, "task_projection_missing:A")
        self.assertEqual(calls["task"], [("A", "done", "")])

    def test_failure_projection_success_marks_succeeded(self):
        m, calls, fake_item, fake_task, _ = self._harness()
        r = m.fail_result("no_confirm", site="B", detail="no confirm")
        out = self._simulate(m, calls, fake_item, fake_task, r, ("content", True))
        self.assertEqual(out.projection_status, "succeeded")
        self.assertEqual(out.projection_reason, "")
        self.assertEqual(len(calls["log"]), 1)
        self.assertEqual(len(calls["item"]), 1)
        self.assertEqual(calls["task"], [("B", "failed", "no_confirm")])

    def test_failure_projection_failure_marks_failed(self):
        m, calls, fake_item, fake_task, _ = self._harness()
        r = m.fail_result("no_confirm", site="B", detail="no confirm")
        out = self._simulate(m, calls, fake_item, fake_task, r, ("content", False))
        self.assertEqual(out.projection_status, "failed")
        self.assertEqual(out.projection_reason, "task_projection_missing:B")
        self.assertEqual(len(calls["log"]), 1)
        self.assertEqual(len(calls["item"]), 1)

    def test_crash_projection_success_marks_succeeded(self):
        m, calls, fake_item, fake_task, _ = self._harness()
        r = m.fail_result("crash", site="C", detail="boom")
        out = self._simulate(m, calls, fake_item, fake_task, r, ("content", True))
        self.assertEqual(out.projection_status, "succeeded")
        self.assertEqual(len(calls["log"]), 1)
        self.assertEqual(len(calls["item"]), 1)


class AuthGateTests(unittest.TestCase):
    """Coverage for is_auth_gated_empty + the legacy no_button auth-gate
    SSO retry dispatch (commit 1cad285 / 57c9e84).  The gate fires only when
    the SPA renders a 404 shell while the URL stays on the target route."""

    SHELL = "404 糟糕！页面未找到！ 返回 返回主页"   # 404 shell, no chrome
    DASH = "跳到主内容 Toggle Sidebar 控制台 概览 数据看板 个人资料"  # signed-in chrome
    LOGIN = "请先登录 用户名 密码 登录"              # looks_logged_out True

    def test_is_auth_gated_empty_branches(self):
        m = load_runner("gate_is_empty")
        self.assertTrue(m.is_auth_gated_empty(""))                 # empty body
        self.assertTrue(m.is_auth_gated_empty(self.SHELL))         # 404 shell, no chrome
        self.assertFalse(m.is_auth_gated_empty(self.DASH))         # dashboard chrome
        self.assertFalse(m.is_auth_gated_empty(self.LOGIN))        # login form → not empty
        # 404 token but with dashboard chrome → not gated (already logged in)
        self.assertFalse(m.is_auth_gated_empty(self.SHELL + " " + self.DASH))

    class _GatePage(FakePage):
        def __init__(self, url):
            self._url = url

        @property
        def url(self):
            return self._url

    def _run_gate(self, label, sso_force_status, page_text_factory,
                  expect_status, expect_reason):
        m = load_runner("gate_" + label.replace(" ", "_").replace("*", "x"))
        adapter = m.SiteAdapter("A", "https://a.example/console/personal",
                                kind="newapi_profile")

        async def none(*a, **k): return None
        async def false(*a, **k): return False
        async def no_captcha(*a, **k): return None
        async def any_visible_none(*a, **k): return (None, None)

        async def sso(page, force=False, browser=None):
            return sso_force_status if force else None

        old = {name: getattr(m, name) for name in (
            "wait_text_ready", "wait_out_cloudflare", "maybe_sso_if_needed", "page_text",
            "already_done", "wait_out_captcha", "wait_for_any_visible", "captcha_blocking",
            "first_visible",
        )}
        m.wait_text_ready = none
        m.wait_out_cloudflare = none
        m.maybe_sso_if_needed = sso
        m.page_text = page_text_factory
        m.already_done = none
        m.wait_out_captcha = no_captcha
        m.wait_for_any_visible = any_visible_none
        m.captcha_blocking = false
        m.first_visible = any_visible_none
        try:
            page = self._GatePage("https://a.example/console/personal")
            result = asyncio.run(m.legacy_checkin_on_page(page, adapter))
            self.assertEqual(result.status, expect_status,
                             f"{label}: status {result.status}")
            self.assertEqual(result.reason, expect_reason,
                             f"{label}: reason {result.reason}")
        finally:
            for name, value in old.items():
                setattr(m, name, value)

    def _shell_text(self, page, n=1200):
        return self.SHELL

    def _login_text(self, page, n=1200):
        return self.LOGIN

    def test_gate_cloudflare_maps_to_cloudflare(self):
        async def shell(page, n=1200): return self.SHELL
        self._run_gate("cf", "CLOUDFLARE", shell, "FAIL", "cloudflare")

    def test_gate_timeout_maps_to_timeout(self):
        async def shell(page, n=1200): return self.SHELL
        self._run_gate("to", "TIMEOUT", shell, "FAIL", "timeout")

    def test_gate_fail_string_maps_to_auth_required(self):
        async def shell(page, n=1200): return self.SHELL
        self._run_gate("fail", "FAIL:oauth", shell, "FAIL", "auth_required")

    def test_gate_no_button_dead_page_classifies_after_sso_attempt(self):
        async def shell(page, n=1200): return self.SHELL
        self._run_gate("nobtn_dead", "NO_BUTTON", shell, "FAIL", "dead_url")

    def test_gate_none_dead_page_classifies_after_sso_attempt(self):
        async def shell(page, n=1200): return self.SHELL
        self._run_gate("none_dead", None, shell, "FAIL", "dead_url")

    def test_gate_sso_ok_but_route_still_404_is_dead_url(self):
        async def shell(page, n=1200): return self.SHELL
        self._run_gate("ok_dead", "OK", shell, "FAIL", "dead_url")

    def test_gate_no_button_with_login_surface_is_auth_required(self):
        # page shows a real login surface throughout → is_auth_page intercepts
        # at the early 2882 check, still auth_required.
        async def login(page, n=1200): return self.LOGIN
        self._run_gate("nobtn_login", "NO_BUTTON", login, "FAIL", "auth_required")


class CloudflareDetectionTests(unittest.TestCase):
    """is_cloudflare must only fire on real CF/Turnstile challenges, not on a
    benign page-footer mention of Turnstile (verydio's BadAppleAPI page)."""

    def _lc(self, url, text):
        m = load_runner("review_cloudflare")
        return m.is_cloudflare(url, text)

    def test_turnstile_footer_mention_not_blocked(self):
        """verydio home footer 'Turnstile 已启用' contains 'turnstile' but is a
        fully-checked-in page — must NOT be treated as CF-blocked."""
        url = "https://check.verydio.com/"
        text = (
            "深色\nBADAPPLEAPI\nBadAppleAPI 每日签到3-10+\nmango\nLINUX DO 登录"
            " · your_qq_email@example.com\n退出\n当前额度\n196.24\n签到累计获得\n229.00\n"
            "连续签到\n5 天\n今日状态\n未签到\n上次签到：2026-08-11 · +11.00\n"
            "立即签到 刷新额度\n时区 Asia/Shanghai · Cloudflare Workers · Turnstile 已启用"
        )
        self.assertFalse(self._lc(url, text))

    def test_cloudflare_hosted_url_blocked(self):
        self.assertTrue(self._lc("https://challenges.cloudflare.com/cdn-cgi/cf-challenge", ""))

    def test_cf_challenge_text_blocked(self):
        self.assertTrue(self._lc("https://a.example/", "cf-challenge"))

    def test_checking_browser_blocked(self):
        self.assertTrue(self._lc("https://a.example/", "Checking your browser before accessing"))

    def test_just_a_moment_blocked(self):
        self.assertTrue(self._lc("https://a.example/", "Verifying you are human. This may take a few seconds. Just a moment…"))

    def test_verify_you_are_human_blocked(self):
        self.assertTrue(self._lc("https://a.example/", "Verify you are human"))

    def test_cn_challenge_text_blocked(self):
        self.assertTrue(self._lc("https://a.example/", "请完成安全验证"))

    def test_turnstile_with_challenge_text_blocked(self):
        # real Turnstile interstitial text: challenge phrase + turnstile
        self.assertTrue(self._lc("https://a.example/", "请完成安全验证 turnstile"))


class TurnstileTokenWaitTests(unittest.TestCase):
    """wait_turnstile_token must block only until [[name=cf-turnstile-response]]
    carries a visible token, and never block sites with no Turnstile widget."""

    def _page_returns(self, seq: list):
        """FakePage whose evaluate returns 'none' first (probe) then walks seq."""
        calls = {"n": 0}

        class P(FakePage):
            async def evaluate(self, expr, *a, **k):
                i = calls["n"]
                calls["n"] += 1
                if i == 0:
                    return "none" if seq[0] == "none" else "pending"
                # real wait loop consumes the rest of seq
                j = calls["n"] - 1
                return seq[j] if j < len(seq) else seq[-1]
        return P, calls

    def test_no_turnstile_returns_true_immediately(self):
        m = load_runner("review_tsw_no")
        P, calls = self._page_returns(["none"])
        self.assertTrue(asyncio.run(m.wait_turnstile_token(P())))
        self.assertLessEqual(calls["n"], 1)

    def test_token_ready_polls_until_value(self):
        m = load_runner("review_tsw_ready")
        # probe → pending, then pending, then ready
        P, _ = self._page_returns(["pending", "pending", "ready"])
        self.assertTrue(asyncio.run(m.wait_turnstile_token(P(), max_s=5)))

    def test_token_never_ready_times_out_false(self):
        m = load_runner("review_tsw_timeout")
        P, _ = self._page_returns(["pending"])
        self.assertFalse(asyncio.run(m.wait_turnstile_token(P(), max_s=0.5)))

    def test_evaluate_raises_returns_true(self):
        """Probe crash (e.g. mid-SPA-redirect) must not block the click —
        callers prefer a wrong-but-fast click over a 12s stall."""
        m = load_runner("review_tsw_throw")

        class P(FakePage):
            async def evaluate(self, expr, *a, **k):
                raise RuntimeError("context destroyed")
        self.assertTrue(asyncio.run(m.wait_turnstile_token(P())))

    def test_probe_checks_layout_not_only_offsetparent(self):
        """Turnstile renders offscreen (offsetParent null) while the hidden input
        still carries a valid token. The probe must also consult element size —
        pinned against a regression to offsetParent-only visibility."""
        m = load_runner("review_tsw_layout")
        probe = m._TURNSTILE_TOKEN_PROBE
        self.assertIn("getBoundingClientRect", probe)
        self.assertIn("offsetParent", probe)
        self.assertIn("r.width > 0 || r.height > 0", probe)


if __name__ == "__main__":
    unittest.main(verbosity=2)

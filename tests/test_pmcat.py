"""pmcat(plan.pmcat.top / PM-Plan)签到流程回归测试.

2026-11-01 接入的站点是自研 SPA + 滑块拼接验证码:
  /api/me → /api/captcha/challenge → /api/captcha/verify → /api/checkin,
写操作必须带 `X-PMPlan-CSRF` 头;拼图块不含 target_x,缺口由背景暗块求解。
这里覆盖路由判定、登录态分支、今日已签分支、成功落账分支,以及
apply_attribution 的因果证据契约(缺 action 会变 unattributed_success)。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_runner(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "stealth_checkin_runner.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AsyncioTest(unittest.TestCase):
    def test_is_pmcat_site_is_narrow(self):
        m = load_runner("pmcat_route")
        self.assertTrue(m.is_pmcat_site("https://plan.pmcat.top/#/"))
        self.assertTrue(m.is_pmcat_site("https://plan.pmcat.top/"))
        # 同家族不同站必须不被误判(dygyz 的 is_dygyz_site 是宽匹配 pmcat.top)
        self.assertFalse(m.is_pmcat_site("https://dygyz.pmcat.top/dashboard"))
        self.assertFalse(m.is_pmcat_site("https://pap.pmcat.top/#/console/wallet"))
        self.assertFalse(m.is_pmcat_site("https://api.llm.pm/profile"))

    def test_catalog_entry_matches_builtin(self):
        m = load_runner("pmcat_catalog")
        yaml_entry = {a.name: a for a in m.load_sites_from_yaml()}["pmcat"]
        builtin = {a.name: a for a in m._BUILTIN_SITE_ADAPTERS}["pmcat"]
        for adapter in (yaml_entry, builtin):
            self.assertEqual(adapter.kind, "pmcat")
            self.assertEqual(adapter.url, "https://plan.pmcat.top/#/")
            self.assertEqual(adapter.ready_rounds, 30)
            self.assertEqual(
                adapter.sign_selectors[0],
                'button[data-action="checkin"]:not([disabled])',
            )
            self.assertIn(
                'button[data-action="checkin"][disabled]', adapter.already_selectors
            )


class PmcatFlowTest(AsyncioTest):
    def _adapter(self, m):
        return m.SiteAdapter(
            "pmcat",
            "https://plan.pmcat.top/#/",
            kind="pmcat",
            sign_selectors=list(m.PMCAT_CTA_SELECTORS),
            already_selectors=list(m.PMCAT_DONE_SELECTORS),
            ready_rounds=30,
        )

    def _run(self, m, page, api, evaluate=None):
        """跑一次 pmcat_checkin,把页面侧 API/验证码交互替换为桩."""
        adapter = self._adapter(m)
        originals = {
            name: getattr(m, name)
            for name in (
                "wait_text_ready",
                "wait_out_cloudflare",
                "bypass_chrome_interstitial_if_needed",
                "page_text",
                "_pmcat_api",
            )
        }

        async def noop(*a, **k):
            return None

        async def no_cf(*a, **k):
            return False

        async def page_text(*a, **k):
            return "ready"

        async def fake_api(pg, path, method="GET", payload=None):
            return api(path, method, payload)

        m.wait_text_ready = noop
        m.wait_out_cloudflare = no_cf
        m.bypass_chrome_interstitial_if_needed = noop
        m.page_text = page_text
        m._pmcat_api = fake_api
        if evaluate is not None:
            page.evaluate = evaluate
        try:
            return asyncio.run(m.pmcat_checkin(page, adapter))
        finally:
            for name, value in originals.items():
                setattr(m, name, value)

    def test_auth_required_when_session_expired(self):
        m = load_runner("pmcat_auth")

        class Page:
            async def goto(self, *a, **k):
                return None

        result = self._run(m, Page(), lambda path, method, payload: {"_http_status": 401})
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "auth_required")
        self.assertIn("/api/me", result.detail)

    def test_already_when_checked_in_today(self):
        m = load_runner("pmcat_already")

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            return {
                "_http_status": 200,
                "body": {
                    "user": {"username": "mangoqwq"},
                    "checkin": {
                        "checked_in_today": True,
                        "can_checkin": False,
                        "streak": 1,
                        "today": "2026-11-01",
                    },
                },
            }

        result = self._run(m, Page(), api)
        self.assertEqual(result.status, "ALREADY")
        # precheck 分支必须自带 confirming evidence,否则归因门判 unattributed_success
        self.assertTrue(m.evidence_is_confirming(result.confirmation))
        gated = m.apply_attribution(result)
        self.assertEqual(gated.status, "ALREADY")

    def test_not_ready_keeps_pending(self):
        m = load_runner("pmcat_pending")

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            return {
                "_http_status": 200,
                "body": {"checkin": {"checked_in_today": False, "can_checkin": False, "today": "2026-11-01"}},
            }

        result = self._run(m, Page(), api)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "not_ready")
        self.assertTrue(result.keep_pending)

    def test_success_path_is_attributable(self):
        m = load_runner("pmcat_ok")
        state = {"checkin_calls": 0}

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            if path == "/api/me":
                done = state["checkin_calls"] > 0
                return {
                    "_http_status": 200,
                    "body": {
                        "user": {"username": "mangoqwq"},
                        "checkin": {
                            "checked_in_today": done,
                            "can_checkin": not done,
                            "streak": 1 if done else 0,
                            "today": "2026-11-01",
                            "recent": ["2026-11-01"] if done else [],
                        },
                    },
                }
            raise AssertionError(f"unexpected api path {path}")

        async def evaluate(script, arg=None):
            # 页面内滑块流程:challenge → verify → checkin 一次成功
            state["checkin_calls"] += 1
            return {
                "ok": True,
                "tries": [
                    {"stage": "verify", "status": 200, "x": 212, "ok": True},
                    {"stage": "checkin", "status": 200, "message": "签到成功，连续 1 天"},
                ],
            }

        result = self._run(m, Page(), api, evaluate)
        self.assertEqual(result.status, "OK")
        gated = m.apply_attribution(result)
        self.assertEqual(gated.status, "OK", msg=gated.detail)
        self.assertEqual(result.action.kind, "api_post")
        self.assertEqual(result.transition, "PENDING_TO_DONE")
        self.assertTrue(result.confirmation.checked_in_today)

    def test_checkin_200_without_ledger_falls_back_to_failure(self):
        """跟单次响应文案不可信:HTTP 200 但 /api/me 未落账必须判失败."""
        m = load_runner("pmcat_unledgered")

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            return {
                "_http_status": 200,
                "body": {
                    "user": {"username": "mangoqwq"},
                    "checkin": {"checked_in_today": False, "can_checkin": True, "today": "2026-11-01"},
                },
            }

        async def evaluate(script, arg=None):
            return {
                "ok": True,
                "tries": [{"stage": "checkin", "status": 200, "message": "签到成功"}],
            }

        result = self._run(m, Page(), api, evaluate)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "captcha_failed")


if __name__ == "__main__":
    unittest.main()

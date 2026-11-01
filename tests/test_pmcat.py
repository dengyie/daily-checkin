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
        # 裸域不是签到入口:只有 plan 子域跑 pmcat 流程
        self.assertFalse(m.is_pmcat_site("https://pmcat.top/"))
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

    def _run(self, m, page, api, evaluate=None, sso=None, cf=False, open_seq=None):
        """跑一次 pmcat_checkin,把页面侧 API/验证码/SSO 交互替换为桩.

        cf=True 模拟 Cloudflare 挡页(_pmcat_open 返回 False);
        open_seq 按序返回 _pmcat_open 结果(用于「中途自愈后被 CF 挡住」).
        """
        adapter = self._adapter(m)
        originals = {
            name: getattr(m, name)
            for name in (
                "wait_text_ready",
                "wait_out_cloudflare",
                "bypass_chrome_interstitial_if_needed",
                "page_text",
                "page_url",
                "_pmcat_api",
                "_pmcat_ensure_sso",
                "_pmcat_open",
            )
        }

        async def noop(*a, **k):
            return None

        async def no_cf(*a, **k):
            return False

        async def page_text(*a, **k):
            return "ready"

        async def page_url(*a, **k):
            return "https://plan.pmcat.top/#/"

        async def fake_open(*a, **k):
            if open_seq is not None:
                return open_seq.pop(0) if open_seq else True
            return not cf

        async def fake_api(pg, path, method="GET", payload=None):
            return api(path, method, payload)

        async def fake_sso(*a, **k):
            # sso 可以是状态字符串,也可以是计数用的协程函数
            if callable(sso):
                return await sso(*a, **k)
            return sso or "TIMEOUT"

        m.wait_text_ready = noop
        m.wait_out_cloudflare = no_cf
        m.bypass_chrome_interstitial_if_needed = noop
        m.page_text = page_text
        m.page_url = page_url
        m._pmcat_api = fake_api
        m._pmcat_ensure_sso = fake_sso
        m._pmcat_open = fake_open
        if evaluate is not None:
            page.evaluate = evaluate
        try:
            return asyncio.run(m.pmcat_checkin(page, adapter))
        finally:
            for name, value in originals.items():
                setattr(m, name, value)

    def test_auth_required_when_session_expired(self):
        """重授权也拿不回会话(linux.do 自己掉登录)才报 auth_required."""
        m = load_runner("pmcat_auth")

        class Page:
            async def goto(self, *a, **k):
                return None

        result = self._run(
            m, Page(), lambda path, method, payload: {"_http_status": 401}, sso="AUTH_REQUIRED"
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "auth_required")
        self.assertIn("/api/me", result.detail)

    def test_expired_session_self_heals_via_oauth(self):
        """401 后走站内 OAuth 重授权成功:继续签到,不把单次失效当业务失败."""
        m = load_runner("pmcat_sso")
        state = {"me_calls": 0}

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            if path == "/api/me":
                state["me_calls"] += 1
                if state["me_calls"] == 1:
                    return {"_http_status": 401}
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
            raise AssertionError(f"unexpected api path {path}")

        result = self._run(m, Page(), api, sso="OK")
        self.assertEqual(result.status, "ALREADY")
        self.assertEqual(state["me_calls"], 2)
        self.assertEqual(m.apply_attribution(result).status, "ALREADY")

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

    def test_checkin_lands_but_ledger_read_lags_still_already(self):
        """P2-1:checkin 已 200,当轮 /api/me 回读抖动导致 3 轮全过,
        循环退出后兜底复检发现已落账 → ALREADY,而不是 captcha_failed."""
        m = load_runner("pmcat_lagging_ledger")
        state = {"me": 0, "slider": 0}

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            state["me"] += 1
            # 第 1 次是 precheck(可签),2-4 是各轮落账复查(抖动未反映),
            # 第 5 次是循环后的兜底复检 → 已落账
            done = state["me"] >= 5
            return {
                "_http_status": 200,
                "body": {
                    "user": {"username": "mangoqwq"},
                    "checkin": {
                        "checked_in_today": done,
                        "can_checkin": not done,
                        "streak": 1,
                        "today": "2026-11-01",
                    },
                },
            }

        async def evaluate(script, arg=None):
            state["slider"] += 1
            return {
                "ok": True,
                "tries": [
                    {"stage": "verify", "status": 200, "x": 212, "ok": True},
                    {"stage": "checkin", "status": 200, "message": "签到成功"},
                ],
            }

        result = self._run(m, Page(), api, evaluate)
        self.assertEqual(result.status, "ALREADY", msg=result.detail)
        self.assertEqual(state["slider"], m.PMCAT_CAPTCHA_MAX_TRIES)
        self.assertTrue(m.evidence_is_confirming(result.confirmation))
        gated = m.apply_attribution(result)
        self.assertEqual(gated.status, "ALREADY")

    def test_mid_flow_auth_loss_self_heals(self):
        """P3-7:challenge/verify 中途 401 → 补授权后重试并成功签到."""
        m = load_runner("pmcat_mid_auth")
        state = {"me": 0, "slider": 0, "sso": 0}

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            state["me"] += 1
            # 第 1 次 precheck 可签;第 2 次是补授权重试后的落账复查 → 已签
            done = state["me"] >= 2
            return {
                "_http_status": 200,
                "body": {
                    "user": {"username": "mangoqwq"},
                    "checkin": {
                        "checked_in_today": done,
                        "can_checkin": not done,
                        "streak": 1,
                        "today": "2026-11-01",
                    },
                },
            }

        async def evaluate(script, arg=None):
            state["slider"] += 1
            if state["slider"] == 1:
                return {
                    "ok": False,
                    "tries": [{"stage": "verify", "status": 401, "x": 210, "reason": None}],
                }
            return {
                "ok": True,
                "tries": [
                    {"stage": "verify", "status": 200, "x": 212, "ok": True},
                    {"stage": "checkin", "status": 200, "message": "签到成功"},
                ],
            }

        # fake_sso 通过 callable 分支记账,不必再手动临时替换
        async def counting_sso(*a, **k):
            state["sso"] += 1
            return "OK"

        result = self._run(m, Page(), api, evaluate, sso=counting_sso)
        self.assertEqual(result.status, "OK", msg=result.detail)
        self.assertEqual(state["sso"], 1)
        self.assertEqual(state["slider"], 2)
        gated = m.apply_attribution(result)
        self.assertEqual(gated.status, "OK", msg=gated.detail)

    def test_mid_flow_auth_loss_unrecoverable_is_auth_required(self):
        """P3-7:结构化按状态码判会话失效,补授权也失败才落 auth_required."""
        m = load_runner("pmcat_mid_auth_fail")

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            return {
                "_http_status": 200,
                "body": {
                    "checkin": {"checked_in_today": False, "can_checkin": True, "today": "2026-11-01"}
                },
            }

        async def evaluate(script, arg=None):
            return {
                "ok": False,
                "tries": [{"stage": "challenge", "status": 403}],
            }

        result = self._run(m, Page(), api, evaluate, sso="AUTH_REQUIRED")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "auth_required")
        self.assertIn("403", result.detail)

    def test_mid_flow_reauth_then_cloudflare_is_cloudflare(self):
        """中途自愈成功但重载站点被 CF 挡住 → cloudflare,不是 auth_required."""
        m = load_runner("pmcat_mid_cf")

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            return {
                "_http_status": 200,
                "body": {
                    "checkin": {"checked_in_today": False, "can_checkin": True, "today": "2026-11-01"}
                },
            }

        async def evaluate(script, arg=None):
            return {"ok": False, "tries": [{"stage": "challenge", "status": 401}]}

        # 首屏放行 → 401 → 自愈 OK → 重载被 CF 挡住
        result = self._run(
            m, Page(), api, evaluate, sso="OK", open_seq=[True, False]
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "cloudflare")

    def test_transport_error_reports_err_not_http_none(self):
        """P3-1:fetch 抛错时真因(_err)必须进 detail,不能报成 HTTP None."""
        m = load_runner("pmcat_transport_err")

        class Page:
            async def goto(self, *a, **k):
                return None

        result = self._run(
            m, Page(), lambda path, method, payload: {"_err": "TypeError: Failed to fetch"}
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "api_unavailable")
        self.assertIn("Failed to fetch", result.detail)
        self.assertNotIn("HTTP None", result.detail)

    def test_streak_missing_is_not_rendered_as_none(self):
        """P3-4:/api/me 不带 streak 时不能渲染「连续 None 天」."""
        m = load_runner("pmcat_streak_none")

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            return {
                "_http_status": 200,
                "body": {
                    "user": {"username": "mangoqwq"},
                    "checkin": {"checked_in_today": True, "can_checkin": False, "today": "2026-11-01"},
                },
            }

        result = self._run(m, Page(), api)
        self.assertEqual(result.status, "ALREADY")
        self.assertNotIn("None", result.detail)
        self.assertNotIn("None", result.confirmation.summary)

    def test_cloudflare_before_precheck(self):
        """P3-9:Cloudflare 挡页必须归因 cloudflare,不是 api_unavailable."""
        m = load_runner("pmcat_cf")

        class Page:
            async def goto(self, *a, **k):
                return None

        result = self._run(
            m, Page(), lambda path, method, payload: {"_http_status": 200, "body": {}}, cf=True
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "cloudflare")

    def test_cf_edge_error_530_is_cloudflare_not_api_unavailable(self):
        """实测 2026-11-01:站点源站隧道挂了返回 530/Error 1033,
        必须归因 cloudflare(站点故障),不能含混成 api_unavailable."""
        m = load_runner("pmcat_cf_530")

        class Page:
            async def goto(self, *a, **k):
                return None

        def api(path, method, payload):
            return {
                "_http_status": 530,
                "body": {"_raw": "error code: 1033"},
            }

        async def evaluate(script, arg=None):
            raise AssertionError("CF 边缘错误不该进入滑块流程")

        result = self._run(m, Page(), api, evaluate)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason, "cloudflare")
        self.assertIn("530", result.detail)


class EnsureSsoTest(AsyncioTest):
    """P2-3:_pmcat_ensure_sso 本体 —— 唯一会真实点击授权按钮的代码,
    不能只靠一次 smoke 背书。覆盖:approve 只点一次 / 掉登录即人工 /
    会话仍在直接 ALREADY."""

    def _sso(self, m, page, urls, *, approve_results=None, me_status=200):
        """按序返回 page_url,跑一次 _pmcat_ensure_sso.

        urls: 每次 page_url 弹出一个,耗尽后重复最后一个;
        approve_results: _approve_linuxdo_authorize 的按序返回(缺省恒有按钮)。
        """
        state = {"approve_calls": 0, "urls": list(urls), "api_calls": 0}
        approves = list(approve_results) if approve_results is not None else [True]

        async def fake_url(p):
            return state["urls"][-1] if not state["urls"] else state["urls"].pop(0)

        async def fake_approve(p):
            state["approve_calls"] += 1
            return approves[min(state["approve_calls"] - 1, len(approves) - 1)] or None

        async def fake_text(p, limit=300):
            return "使用 Linux DO 登录"

        async def fake_api(pg, path, method="GET", payload=None):
            state["api_calls"] += 1
            return {"_http_status": me_status, "body": {}}

        async def fake_goto(*a, **k):
            return None

        page.goto = fake_goto
        originals = {
            name: getattr(m, name)
            for name in ("page_url", "_approve_linuxdo_authorize", "page_text", "_pmcat_api")
        }
        m.page_url = fake_url
        m._approve_linuxdo_authorize = fake_approve
        m.page_text = fake_text
        m._pmcat_api = fake_api
        try:
            result = asyncio.run(m._pmcat_ensure_sso(page))
        finally:
            for name, value in originals.items():
                setattr(m, name, value)
        return result, state

    def test_approves_authorize_page_only_once(self):
        """授权页可能出现多轮(回调在途),但只点一次「允许」."""
        m = load_runner("pmcat_sso_once")

        class Page:
            pass

        authorize = "https://connect.linux.do/oauth2/authorize?client_id=x"
        result, state = self._sso(
            m, Page(), [authorize, authorize, "https://plan.pmcat.top/#/"]
        )
        self.assertEqual(result, "OK")
        self.assertEqual(state["approve_calls"], 1)

    def test_logged_out_linuxdo_requires_human(self):
        """授权页却要登录 linux.do → 立即 AUTH_REQUIRED,不空转到超时."""
        m = load_runner("pmcat_sso_human")

        class Page:
            pass

        authorize = "https://connect.linux.do/oauth2/authorize?client_id=x"
        result, state = self._sso(
            m, Page(), [authorize], approve_results=[None]
        )
        self.assertEqual(result, "AUTH_REQUIRED")
        self.assertEqual(state["approve_calls"], 1)

    def test_session_still_alive_returns_already(self):
        """没经过授权页就拿到 200 → 会话本来就活着,返回 ALREADY."""
        m = load_runner("pmcat_sso_alive")

        class Page:
            pass

        result, state = self._sso(m, Page(), ["https://plan.pmcat.top/#/"])
        self.assertEqual(result, "ALREADY")
        self.assertEqual(state["approve_calls"], 0)
        self.assertEqual(state["api_calls"], 1)


if __name__ == "__main__":
    unittest.main()

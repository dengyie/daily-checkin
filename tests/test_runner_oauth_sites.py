#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from unittest import mock
import asyncio
import inspect
import pytest
import unittest
import yaml

from tests._support import (
    FakePage,
    load_mod,
)

pytestmark = [pytest.mark.runner]


class TestAgentrouterDualOAuth(unittest.TestCase):
    """agentrouter.org 双账号 OAuth 适配器(dummy 数据,不接真站,不填真账密)。

    覆盖计划要求:
      - is_agentrouter_site 正/反向;
      - 4 组选择器常量非空;
      - 双 OAuth 顺序骨架(登出 → GitHub → 再登出 → LinuxDO)与聚合结果。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_agentrouter_site_positive_negative(self):
        f = self.m.is_agentrouter_site
        self.assertTrue(f("https://agentrouter.org/"))
        self.assertTrue(f("http://agentrouter.org/some/path"))
        self.assertTrue(f("agentrouter.org"))
        self.assertFalse(f("https://anyrouter.top/console"))
        self.assertFalse(f("https://foo.org/"))
        self.assertFalse(f(""))

    def test_agentrouter_selector_constants_nonempty(self):
        for name in (
            "GITHUB_SELECTORS",
            "GITHUB_AUTHORIZE_SELECTORS",
            "AGENTROUTER_LOGOUT_SELECTORS",
            "AGENTROUTER_SUCCESS_SELECTORS",
        ):
            sels = getattr(self.m, name)
            self.assertIsInstance(sels, list, name)
            self.assertTrue(sels, f"{name} must not be empty")
        # 关键 CTA 文案必须覆盖(参考 LINUXDO_SELECTORS / AUTHORIZE_SELECTORS 风格)
        self.assertTrue(any("GitHub" in s for s in self.m.GITHUB_SELECTORS))
        self.assertTrue(
            any(
                "Authorize" in s or "Allow" in s or "授权" in s
                for s in self.m.GITHUB_AUTHORIZE_SELECTORS
            )
        )
        self.assertTrue(
            any("退出" in s or "Logout" in s or "Sign out" in s
                for s in self.m.AGENTROUTER_LOGOUT_SELECTORS)
        )
        self.assertTrue(
            any("已签到" in s or "签到成功" in s or "checked" in s.lower()
                for s in self.m.AGENTROUTER_SUCCESS_SELECTORS)
        )

    def test_github_authorize_url_detector(self):
        f = self.m.is_github_authorize_url
        self.assertTrue(f("https://github.com/login/oauth/authorize?client_id=x"))
        self.assertTrue(f("https://github.com/login/oauth/authorize"))
        self.assertFalse(f("https://github.com/"))
        self.assertFalse(f("https://github.com/user/USERNAME"))
        self.assertFalse(f("https://example.com/login/oauth/authorize"))

    def test_dual_oauth_sequence_logout_first_then_github_then_linux(self):
        """骨架断言:先登出 → GitHub CTA → try_github_oauth → 再登出 → LinuxDO"""

        m = self.m
        calls: list[str] = []

        class FakePage:
            """极简 page 替身;只依赖被 patch 的模块级 helper,不真上网。"""

            async def goto(self, url, **kwargs):
                calls.append("goto")
                return None

        page = FakePage()

        async def fake_wait_out_cf(*a, **k):
            calls.append("wait_out_cloudflare")
            return None

        async def fake_wait_text(*a, **k):
            calls.append("wait_text_ready")
            return True

        async def fake_page_text(*a, **k):
            # 一直表现未登录:「请先登录」是 looks_logged_out 的强信号
            return "请先登录 登录您的账户 使用Linux.do继续使用GitHub继续请登录后再操作"

        async def fake_page_url(*a, **k):
            return "https://agentrouter.org/"

        async def fake_logout(page):
            calls.append("_agentrouter_logout")
            return True

        async def fake_click_first(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "GitHub" in joined:
                calls.append("click_github_cta")
            elif "Linux" in joined:
                calls.append("click_linuxdo_cta")
            elif "退出" in joined or "Logout" in joined or "Sign out" in joined:
                calls.append("click_logout_btn")
            return "fake-sel"

        async def fake_github_oauth(page, timeout=None, browser=None):
            calls.append("try_github_oauth")
            return "OK"

        async def fake_linux_sso(page, origin_host="", browser=None):
            # try_linuxdo_sso 内部会点击 LINUXDO_SELECTORS(第一可见的 Linux.do CTA)
            calls.append("try_linuxdo_sso")
            calls.append("click_linuxdo_cta")
            return "OK"

        async def fake_terms(*a, **k):
            calls.append("check_terms")
            return 0

        async def fake_current_account(*a, **k):
            # 双账号:每一账号 OAuth 后校验一次 handle。第 1 次(GitHub)应验证到
            # github_*,第 2 次(LinuxDO)应验证到 linuxdo_* —— 用调用次数区分。
            calls.append("current_account_check")
            n = calls.count("current_account_check")
            return "github_1" if n == 1 else "linuxdo_1"

        # agentrouter 签到=登录即触发额度(无签到按钮/文字,2026-08-26 inspect 实证)。
        # gate 在 OAuth 返回 OK 后直接返回 OK,不再调用 _agentrouter_sign_once。
        # 只 patch 模块级 helper(global name lookup),不碰 FakePage 方法。
        # page.goto 在 agentrouter_checkin 里直接用 page.goto,需额外 patch self。
        with mock.patch.object(m, "wait_text_ready", fake_wait_text), \
             mock.patch.object(m, "wait_out_cloudflare", fake_wait_out_cf), \
             mock.patch.object(m, "page_text", fake_page_text), \
             mock.patch.object(m, "page_url", fake_page_url), \
             mock.patch.object(m, "click_first_visible", fake_click_first), \
             mock.patch.object(m, "_agentrouter_current_account", fake_current_account), \
             mock.patch.object(m, "try_github_oauth", fake_github_oauth), \
             mock.patch.object(m, "try_linuxdo_sso", fake_linux_sso), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "_agentrouter_logout", fake_logout):

            adapter = m.SiteAdapter(
                name="agentrouter",
                url="https://agentrouter.org/",
                kind="agentrouter",
            )
            res = self.asyncio.run(m.agentrouter_checkin(page, adapter, browser=None))

        # 顺序断言
        self.assertIn("_agentrouter_logout", calls)
        self.assertIn("click_github_cta", calls)
        self.assertIn("try_github_oauth", calls)
        self.assertIn("click_linuxdo_cta", calls)
        self.assertIn("try_linuxdo_sso", calls)

        gh_cta = calls.index("click_github_cta")
        gh_sso = calls.index("try_github_oauth")
        ld_sso = calls.index("try_linuxdo_sso")
        # GitHub 阶段(CTA + OAuth)必须先于 LinuxDO OAuth 阶段
        self.assertLess(gh_cta, gh_sso)
        self.assertLess(gh_sso, ld_sso)
        self.assertLess(gh_cta, ld_sso)
        # 两次登出:初始 + 两账号之间
        self.assertGreaterEqual(calls.count("_agentrouter_logout"), 2)

        self.assertEqual(res.status, "OK")
        self.assertIn("github=OK", res.detail)
        self.assertIn("linuxdo=OK", res.detail)


class TestTabitokenGithubOAuth(unittest.TestCase):
    """tabitoken.com 单账号 GitHub OAuth 适配器(dummy 数据,不接真站,不填真账密)。

    覆盖计划要求:
      - is_tabitoken_site 正/反向;
      - 关键常量非空(登录 URL、session cookie 名、复用 GITHUB_SELECTORS);
      - tabitoken_checkin 骨架:清 session → GitHub CTA → try_github_oauth → OK;
      - _tabitoken_clear_session 只清 tabitoken 域 cookie(断言 CDP deleteCookies
        的 domain/name/path 参数,不全清——共享 profile 安全红线);
      - 走 confirmed_done_result 满足 apply_attribution 因果契约。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_tabitoken_site_positive_negative(self):
        f = self.m.is_tabitoken_site
        self.assertTrue(f("https://tabitoken.com/"))
        self.assertTrue(f("http://tabitoken.com/sign-in"))
        self.assertTrue(f("tabitoken.com"))
        self.assertFalse(f("https://agentrouter.org/"))
        self.assertFalse(f("https://foo.com/"))
        self.assertFalse(f(""))

    def test_github_login_detector_domain_gated(self):
        """_is_github_login_page 必须域门守卫:非 GitHub 域绝不判登录表单。

        2026-08-26 tabitoken 实测踩坑:tabitoken 自身登录页(/sign-in)含「用户名或
        电子邮件」+「密码」文本,旧纯文本匹配把 ta 登录卡误判成 GitHub 登录表单
        → try_github_oauth 首轮 AUTH_REQUIRED(即使 CTA 本可正常跳 OAuth)。
        修复=域守卫:仅 github.com / *.github.com 生效。防回归锚点。
        """
        f = self.m._is_github_login_page
        # 误判源:tabitoken 登录卡(非 GitHub 域)必须 False
        self.assertFalse(f("登录\n使用 GitHub 继续\n用户名或电子邮件\n密码\n忘记密码？", "https://tabitoken.com/sign-in"))
        self.assertFalse(f("用户名或电子邮件\n密码\n登录", "https://agentrouter.org/login"))
        self.assertFalse(f("用户名或电子邮件\n密码\n登录", ""))
        # 真 GitHub 页仍判
        self.assertTrue(f("Sign in to GitHub\nUsername\nPassword", "https://github.com/login"))
        self.assertTrue(f("Sign in to GitHub", "https://github.com/login/oauth/authorize?client_id=x"))
        # GitHub 已登录态(无登录表单文本)不判
        self.assertFalse(f("Authorize\nThis application\nGreen button", "https://github.com/login/oauth/authorize?client_id=x"))
        # GitHub 仓库页(非登录表单)不判
        self.assertFalse(f("Code\nIssues\nPull requests", "https://github.com/your_github/ai-novel"))

    def test_tabitoken_constants_nonempty(self):
        self.assertEqual(self.m.TABITOKEN_SITE_URL, "https://tabitoken.com/")
        self.assertEqual(self.m.TABITOKEN_SIGNIN_URL, "https://tabitoken.com/sign-in")
        self.assertEqual(self.m.TABITOKEN_SESSION_COOKIE, "new_api_refresh")
        # GitHub CTA 文案必须覆盖(tabitoken 复用 GITHUB_SELECTORS)
        self.assertTrue(any("GitHub" in s for s in self.m.GITHUB_SELECTORS))

    def test_clear_session_only_deletes_tabitoken_cookie(self):
        """_tabitoken_clear_session 只清 tabitoken 域 single cookie,不全清。

        关键安全红线:9222 是共享 profile,clear_cookies() 全清会破坏 GitHub + 其他站
        session。断言走 CDP Network.deleteCookies 且 domain/name/path 精确匹配。
        真实 Playwright API:page.context.new_cdp_session(page) 返回独立 CDPSession,
        send/detach 挂在 CDPSession 上(非 BrowserContext)。fake 镜像此形状,避免 API
        误用被测试掩盖(同 page_cdp_target_id line 1054 用法)。
        """
        m = self.m
        cdp_calls: list[dict] = []
        detach_calls: list[int] = []

        class FakeCDPSession:
            async def send(self_inner, method, params=None):
                cdp_calls.append({"method": method, "params": params})
                return {}

            async def detach(self_inner):
                detach_calls.append(1)

        class FakeContext:
            async def new_cdp_session(self_inner, page):
                return FakeCDPSession()

        class FakePage:
            def __init__(self):
                self.context = FakeContext()

        page = FakePage()
        ok = self.asyncio.run(m._tabitoken_clear_session(page, browser=None))
        self.assertTrue(ok)
        # 至少一次 Network.enable + 一次 Network.deleteCookies
        methods = [c["method"] for c in cdp_calls]
        self.assertIn("Network.enable", methods)
        self.assertIn("Network.deleteCookies", methods)
        # 取 deleteCookies 调用,断言参数精确(tabitoken 域、指定 cookie、指定 path)
        del_calls = [c for c in cdp_calls if c["method"] == "Network.deleteCookies"]
        self.assertEqual(len(del_calls), 1)
        params = del_calls[0]["params"] or {}
        self.assertEqual(params.get("name"), m.TABITOKEN_SESSION_COOKIE)
        self.assertEqual(params.get("domain"), "tabitoken.com")
        self.assertEqual(params.get("path"), m.TABITOKEN_SESSION_COOKIE_PATH)
        # 红线:从不清全量 cookie(无 clear_cookies 调用)
        for c in cdp_calls:
            self.assertNotIn("clear", c["method"].lower())
        # session 用完应 detach(不泄漏 CDP 会话)
        self.assertEqual(len(detach_calls), 1)

    def test_tabitoken_checkin_sequence_clear_then_github_oauth(self):
        """骨架断言:先清 session → GitHub CTA → try_github_oauth → OK → confirmed_done."""
        m = self.m
        calls: list[str] = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto")
                return None

        page = FakePage()

        async def fake_wait_out_cf(*a, **k):
            calls.append("wait_out_cloudflare")
            return None

        async def fake_wait_text(*a, **k):
            calls.append("wait_text_ready")
            return True

        async def fake_click_first(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "GitHub" in joined:
                calls.append("click_github_cta")
            return "fake-sel"

        async def fake_clear_session(page, browser):
            calls.append("_tabitoken_clear_session")
            return True

        async def fake_github_oauth(page, timeout=None, browser=None, return_host="agentrouter.org"):
            calls.append("try_github_oauth")
            calls.append(("return_host", return_host))
            return "OK"

        async def fake_terms(*a, **k):
            calls.append("check_terms")
            return 0

        with mock.patch.object(m, "wait_text_ready", fake_wait_text), \
             mock.patch.object(m, "wait_out_cloudflare", fake_wait_out_cf), \
             mock.patch.object(m, "click_first_visible", fake_click_first), \
             mock.patch.object(m, "try_github_oauth", fake_github_oauth), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "_tabitoken_clear_session", fake_clear_session):

            adapter = m.SiteAdapter(
                name="tabitoken",
                url="https://tabitoken.com/",
                kind="tabitoken",
            )
            res = self.asyncio.run(m.tabitoken_checkin(page, adapter, browser=None))

        # 顺序断言:先清 session,再 GitHub CTA,再 OAuth
        self.assertIn("_tabitoken_clear_session", calls)
        self.assertIn("click_github_cta", calls)
        self.assertIn("try_github_oauth", calls)
        clear_idx = calls.index("_tabitoken_clear_session")
        cta_idx = calls.index("click_github_cta")
        sso_idx = calls.index("try_github_oauth")
        self.assertLess(clear_idx, cta_idx)
        self.assertLess(cta_idx, sso_idx)
        # try_github_oauth 须以 return_host=tabitoken.com 调用(回跳 host 收敛)
        self.assertIn(("return_host", "tabitoken.com"), calls)

        # 走 confirmed_done_result → status OK + 有 action evidence
        self.assertEqual(res.status, "OK")
        self.assertIn("github-oauth confirmed", res.detail)


class TestJustwokerGithubOAuth(unittest.TestCase):
    """justwoker(api.justwoker.icu) 单账号 GitHub OAuth 适配器(dummy,不接真站)。

    覆盖:is_justwoker_site 正反向、关键常量、_justwoker_clear_session 只清
    justwoker 域 single cookie(不全清红线)、justwoker_checkin 只走 清 session →
    GitHub CTA → try_github_oauth(return_host 收敛)→ confirmed_done。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_justwoker_site_positive_negative(self):
        f = self.m.is_justwoker_site
        self.assertTrue(f("https://api.justwoker.icu/"))
        self.assertTrue(f("https://api.justwoker.icu/sign-in"))
        self.assertTrue(f("api.justwoker.icu"))
        self.assertFalse(f("https://tabitoken.com/"))
        self.assertFalse(f("https://agentrouter.org/"))
        self.assertFalse(f(""))
        self.assertFalse(f("https://foo.com/"))

    def test_justwoker_constants_nonempty(self):
        self.assertEqual(self.m.JUSTWOKER_SITE_URL, "https://api.justwoker.icu/")
        self.assertEqual(self.m.JUSTWOKER_SIGNIN_URL, "https://api.justwoker.icu/sign-in")
        self.assertEqual(self.m.JUSTWOKER_SESSION_COOKIE, "new_api_refresh")
        # GitHub CTA 文案必须覆盖(justwoker 复用 GITHUB_SELECTORS,首项「使用 GitHub 继续」)
        self.assertTrue(any("GitHub" in s for s in self.m.GITHUB_SELECTORS))
        self.assertIn("使用 GitHub 继续", self.m.GITHUB_SELECTORS[0])

    def test_clear_session_only_deletes_justwoker_cookie(self):
        """_justwoker_clear_session 只清 justwoker 域 single cookie,不全清。

        红线:9222 共享 profile,全清破坏 GitHub + 其他站 session。断言 CDP
        Network.deleteCookies 的 domain/name/path 精确匹配,且无 clear_cookies。
        """
        m = self.m
        cdp_calls: list[dict] = []
        detach_calls: list[int] = []

        class FakeCDPSession:
            async def send(self_inner, method, params=None):
                cdp_calls.append({"method": method, "params": params})
                return {}

            async def detach(self_inner):
                detach_calls.append(1)

        class FakeContext:
            async def new_cdp_session(self_inner, page):
                return FakeCDPSession()

        class FakePage:
            def __init__(self):
                self.context = FakeContext()

        ok = self.asyncio.run(m._justwoker_clear_session(FakePage(), browser=None))
        self.assertTrue(ok)
        methods = [c["method"] for c in cdp_calls]
        self.assertIn("Network.enable", methods)
        self.assertIn("Network.deleteCookies", methods)
        del_calls = [c for c in cdp_calls if c["method"] == "Network.deleteCookies"]
        self.assertEqual(len(del_calls), 1)
        params = del_calls[0]["params"] or {}
        self.assertEqual(params.get("name"), m.JUSTWOKER_SESSION_COOKIE)
        self.assertEqual(params.get("domain"), "api.justwoker.icu")
        self.assertEqual(params.get("path"), m.JUSTWOKER_SESSION_COOKIE_PATH)
        for c in cdp_calls:
            self.assertNotIn("clear", c["method"].lower())
        self.assertEqual(len(detach_calls), 1)

    def test_justwoker_checkin_sequence_clear_then_github_oauth(self):
        """骨架断言:先清 session → GitHub CTA → try_github_oauth(return_host) → OK."""
        m = self.m
        calls: list[str] = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto")
                return None

        page = FakePage()

        async def fake_wait_out_cf(*a, **k):
            calls.append("wait_out_cloudflare")
            return None

        async def fake_wait_text(*a, **k):
            calls.append("wait_text_ready")
            return True

        async def fake_click_first(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "GitHub" in joined:
                calls.append("click_github_cta")
            return "fake-sel"

        async def fake_clear_session(page, browser):
            calls.append("_justwoker_clear_session")
            return True

        async def fake_github_oauth(page, timeout=None, browser=None, return_host="agentrouter.org"):
            calls.append("try_github_oauth")
            calls.append(("return_host", return_host))
            return "OK"

        async def fake_terms(*a, **k):
            return 0

        async def fake_dismiss(page):
            calls.append("dismiss_obstructing_dialogs")
            return 1

        with mock.patch.object(m, "wait_text_ready", fake_wait_text), \
             mock.patch.object(m, "wait_out_cloudflare", fake_wait_out_cf), \
             mock.patch.object(m, "click_first_visible", fake_click_first), \
             mock.patch.object(m, "try_github_oauth", fake_github_oauth), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss), \
             mock.patch.object(m, "_justwoker_clear_session", fake_clear_session):

            adapter = m.SiteAdapter(
                name="justwoker",
                url="https://api.justwoker.icu/",
                kind="justwoker",
            )
            res = self.asyncio.run(m.justwoker_checkin(page, adapter, browser=None))

        self.assertIn("_justwoker_clear_session", calls)
        self.assertIn("dismiss_obstructing_dialogs", calls)
        self.assertIn("click_github_cta", calls)
        self.assertIn("try_github_oauth", calls)
        clear_idx = calls.index("_justwoker_clear_session")
        dismiss_idx = calls.index("dismiss_obstructing_dialogs")
        cta_idx = calls.index("click_github_cta")
        # 先清 session → 关公告弹窗 → GitHub CTA → OAuth
        self.assertLess(clear_idx, dismiss_idx)
        self.assertLess(dismiss_idx, cta_idx)
        self.assertLess(cta_idx, calls.index("try_github_oauth"))
        # try_github_oauth 以 return_host=api.justwoker.icu 调用(回跳 host 收敛)
        self.assertIn(("return_host", "api.justwoker.icu"), calls)

        # 走 confirmed_done_result → status OK + 有 action evidence
        self.assertEqual(res.status, "OK")
        self.assertIn("github-oauth confirmed", res.detail)


class TestGorouterGithubOAuth(unittest.TestCase):
    """gorouter.app 单账号 GitHub OAuth 适配器(dummy,不接真站)。

    镜像 TestJustwokerGithubOAuth:is_gorouter_site 正反向、_clear_newapi_session
    只清 gorouter 域 single cookie(不全清红线)、gorouter_checkin 走 清 session →
    GitHub CTA → try_github_oauth(return_host=gorouter.app)→ confirmed_done。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_gorouter_site_positive_negative(self):
        f = self.m.is_gorouter_site
        self.assertTrue(f("https://gorouter.app/"))
        self.assertTrue(f("https://gorouter.app/sign-in"))
        self.assertTrue(f("gorouter.app"))
        self.assertFalse(f("https://tabitoken.com/"))
        self.assertFalse(f("https://agentrouter.org/"))
        self.assertFalse(f(""))
        self.assertFalse(f("https://foo.com/"))

    def test_gorouter_constants_nonempty(self):
        # GitHub CTA 文案必须覆盖(gorouter 复用 GITHUB_SELECTORS,首项「使用 GitHub 继续」)
        self.assertTrue(any("GitHub" in s for s in self.m.GITHUB_SELECTORS))
        self.assertIn("使用 GitHub 继续", self.m.GITHUB_SELECTORS[0])
        self.assertEqual(self.m._NEWAPI_SESSION_COOKIE, "new_api_refresh")
        self.assertEqual(self.m._NEWAPI_SESSION_COOKIE_PATH, "/api/user/auth")

    def test_clear_session_only_deletes_gorouter_cookie(self):
        """_clear_newapi_session 只删 gorouter 域 single cookie,不全清。

        红线:9222 共享 profile,全清破坏 GitHub + 其他站 session。断言 CDP
        Network.deleteCookies 的 domain/name/path 精确匹配,且无 clear_cookies。
        """
        m = self.m
        cdp_calls: list[dict] = []
        detach_calls: list[int] = []

        class FakeCDPSession:
            async def send(self_inner, method, params=None):
                cdp_calls.append({"method": method, "params": params})
                return {}

            async def detach(self_inner):
                detach_calls.append(1)

        class FakeContext:
            async def new_cdp_session(self_inner, page):
                return FakeCDPSession()

        class FakePage:
            def __init__(self):
                self.context = FakeContext()

        ok = self.asyncio.run(
            m._clear_newapi_session(FakePage(), browser=None, domain="gorouter.app", label="gorouter",
                                    cookie_name="session", cookie_path="/")
        )
        self.assertTrue(ok)
        methods = [c["method"] for c in cdp_calls]
        self.assertIn("Network.enable", methods)
        self.assertIn("Network.deleteCookies", methods)
        del_calls = [c for c in cdp_calls if c["method"] == "Network.deleteCookies"]
        self.assertEqual(len(del_calls), 1)
        params = del_calls[0]["params"] or {}
        # gorouter 实际登录态 cookie 是 session(path=/),不是 New API 通用
        # new_api_refresh。删错 → 登出无效 → /sign-in 跳 dashboard → NO_GITHUB_CTA
        self.assertEqual(params.get("name"), "session")
        self.assertEqual(params.get("domain"), "gorouter.app")
        self.assertEqual(params.get("path"), "/")
        for c in cdp_calls:
            self.assertNotIn("clear", c["method"].lower())
        self.assertEqual(len(detach_calls), 1)

    def test_gorouter_checkin_sequence_clear_then_github_oauth(self):
        """骨架断言:先清 session → GitHub CTA → try_github_oauth(return_host) → OK."""
        m = self.m
        calls: list[str] = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto")
                return None

        page = FakePage()

        async def fake_wait_out_cf(*a, **k):
            calls.append("wait_out_cloudflare")
            return None

        async def fake_wait_text(*a, **k):
            calls.append("wait_text_ready")
            return True

        async def fake_click_first(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "GitHub" in joined:
                calls.append("click_github_cta")
            return "fake-sel"

        async def fake_clear_session(page, browser, domain=None, label=None, **kwargs):
            calls.append("_clear_newapi_session")
            return True

        async def fake_github_oauth(page, timeout=None, browser=None, return_host="agentrouter.org"):
            calls.append("try_github_oauth")
            calls.append(("return_host", return_host))
            return "OK"

        async def fake_terms(*a, **k):
            return 0

        async def fake_dismiss(page):
            calls.append("dismiss_obstructing_dialogs")
            return 1

        with mock.patch.object(m, "wait_text_ready", fake_wait_text), \
             mock.patch.object(m, "wait_out_cloudflare", fake_wait_out_cf), \
             mock.patch.object(m, "click_first_visible", fake_click_first), \
             mock.patch.object(m, "try_github_oauth", fake_github_oauth), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss), \
             mock.patch.object(m, "_clear_newapi_session", fake_clear_session):

            adapter = m.SiteAdapter(
                name="gorouter",
                url="https://gorouter.app/",
                kind="gorouter",
            )
            res = self.asyncio.run(m.gorouter_checkin(page, adapter, browser=None))

        self.assertIn("_clear_newapi_session", calls)
        self.assertIn("dismiss_obstructing_dialogs", calls)
        self.assertIn("click_github_cta", calls)
        self.assertIn("try_github_oauth", calls)
        clear_idx = calls.index("_clear_newapi_session")
        dismiss_idx = calls.index("dismiss_obstructing_dialogs")
        cta_idx = calls.index("click_github_cta")
        # 先清 session → 关公告弹窗 → GitHub CTA → OAuth
        self.assertLess(clear_idx, dismiss_idx)
        self.assertLess(dismiss_idx, cta_idx)
        self.assertLess(cta_idx, calls.index("try_github_oauth"))
        # try_github_oauth 以 return_host=gorouter.app 调用(回跳 host 收敛)
        self.assertIn(("return_host", "gorouter.app"), calls)

        # 走 confirmed_done_result → status OK + 有 action evidence
        self.assertEqual(res.status, "OK")
        self.assertIn("github-oauth confirmed", res.detail)


class TestMzloneLoginPlusSignin(unittest.TestCase):
    """mzlone.top(AIMZ / New API):GitHub OAuth 登录 + 正常签到 两步(dummy,不接真站).

    与 gorouter/justwoker 的「登录即 OK」不同——登录不算签,登录后 /profile 有真
    「立即签到」区。覆盖:is_mzlone_site 正反向、mzlone_checkin 走 GitHub CTA→
    try_github_oauth(return_host=mzlone.top)→ 落 /profile → 点「立即签到」→ OK。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_mzlone_site_positive_negative(self):
        f = self.m.is_mzlone_site
        self.assertTrue(f("https://mzlone.top/sign-in"))
        self.assertTrue(f("https://mzlone.top/profile"))
        self.assertTrue(f("mzlone.top"))
        self.assertFalse(f("https://gorouter.app/"))
        self.assertFalse(f("https://mzlone.com/"))
        self.assertFalse(f(""))
        self.assertFalse(f("https://foo.com/"))

    def test_mzlone_constants_nonempty(self):
        # GitHub CTA 文案必须覆盖(复用 GITHUB_SELECTORS 首项「使用 GitHub 继续」)
        self.assertTrue(any("GitHub" in s for s in self.m.GITHUB_SELECTORS))
        self.assertIn("使用 GitHub 继续", self.m.GITHUB_SELECTORS[0])
        # 双步靠 sign_selectors 点「立即签到」+ already 已签态。
        a = next(ad for ad in self.m._BUILTIN_SITE_ADAPTERS if ad.name == "mzlone")
        self.assertEqual(a.kind, "mzlone")
        self.assertEqual(a.url, "https://mzlone.top/profile")
        self.assertIn("立即签到", " ".join(a.sign_selectors))
        self.assertEqual(a.prefer_catalog_url, True)

    def test_mzlone_checkin_login_then_sign_click(self):
        """骨架:先 GitHub OAuth(return_host=mzlone.top)→ 落 /profile → 点立即签到 → OK."""
        m = self.m
        calls: list[str] = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto:" + (url or ""))
                return None

        page = FakePage()

        async def fake_wait(_a, _b=None, _c=None):
            calls.append("wait_text_ready")
            return True

        async def fake_cf(*a, **k):
            return None

        async def fake_click(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "GitHub" in joined:
                calls.append("click_github_cta")
                return "github-sel"
            if "立即签到" in joined:
                calls.append("click_signin_btn")
                return "button:has-text(\"立即签到\")"
            return None

        async def fake_oauth(page, timeout=None, browser=None, return_host="agentrouter.org"):
            calls.append("try_github_oauth")
            calls.append(("return_host", return_host))
            return "OK"

        async def fake_terms(*a, **k):
            return 0

        async def fake_text(page, n=900):
            return "立即签到 3 累计已签 本月获得"

        async def fake_purl(page):
            return "https://mzlone.top/sign-in?redirect=%2Fprofile"

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "click_first_visible", fake_click), \
             mock.patch.object(m, "try_github_oauth", fake_oauth), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_purl):
            builtin = next(
                ad for ad in m._BUILTIN_SITE_ADAPTERS if ad.name == "mzlone"
            )
            adapter = m.SiteAdapter(
                name=builtin.name,
                url=builtin.url,
                kind=builtin.kind,
                sign_selectors=list(builtin.sign_selectors),
                already_selectors=list(builtin.already_selectors),
                ready_rounds=builtin.ready_rounds,
            )
            res = self.asyncio.run(m.mzlone_checkin(page, adapter, browser=None))

        # 两次 goto:/sign-in 与 /profile
        joined = " ".join(c for c in calls if isinstance(c, str))
        self.assertIn("goto", joined)
        self.assertIn("https://mzlone.top/sign-in", joined)
        self.assertIn("https://mzlone.top/profile", joined)
        self.assertIn("click_github_cta", calls)
        self.assertIn("click_signin_btn", calls)
        self.assertIn("try_github_oauth", calls)
        oauth_idx = calls.index("try_github_oauth")
        sign_idx = calls.index("click_signin_btn")
        # OAuth 在点签到之前
        self.assertLess(oauth_idx, sign_idx)
        # try_github_oauth 以 return_host=mzlone.top 调用(回跳 host 收敛)
        self.assertIn(("return_host", "mzlone.top"), calls)

        # 走 confirmed_done_result → status OK + 有 action evidence
        self.assertEqual(res.status, "OK")
        self.assertIn("mzlone sign-in", res.detail)


class TestLlmroutesDualOAuth(unittest.TestCase):
    """llmroutes.cn(LLM Routes / New API):双账号(GitHub + Linux Do OAuth)登录 + 签到两步.

    已下线(2026-09-01):站点迁至 ai.berf1.cn 后无签到功能,adapter 已从内置列表与
    sites.yaml 移除、DB enabled=0。但 llmroutes_checkin 函数逻辑保留(可逆),以下
    测试手工构造同名 adapter 验证该函数,不再依赖内置 adapter 里的 llmroutes。
    Web 新增站点走 POST /api/tasks。

    """
    def _llmroutes_adapter(self):
        return self.m.SiteAdapter(
            name="llmroutes",
            url="https://llmroutes.cn/",
            kind="llmroutes",
            sign_selectors=[
                'button:has-text("立即签到")',
                'button:has-text("签到领取奖励")',
                'button:has-text("签到"):not(:has-text("每日签到"))',
            ],
            already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
            ready_rounds=15,
        )

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_llmroutes_site_positive_negative(self):
        f = self.m.is_llmroutes_site
        self.assertTrue(f("https://llmroutes.cn/"))
        self.assertTrue(f("https://llmroutes.cn/profile"))
        self.assertTrue(f("llmroutes.cn"))
        self.assertFalse(f("llmroutes"))  # 裸域名非 .cn 不匹配(精确判定)
        self.assertFalse(f("https://mzlone.top/"))
        self.assertFalse(f("https://llmroutes.com/"))
        self.assertFalse(f(""))
        self.assertFalse(f("https://foo.com/"))

    def test_llmroutes_constants_nonempty(self):
        # 登录卡必须同时覆盖 GitHub 与 Linux Do 两个 CTA(用户确认双账号)。
        self.assertTrue(any("GitHub" in s for s in self.m.GITHUB_SELECTORS))
        self.assertIn("使用 GitHub 继续", self.m.GITHUB_SELECTORS[0])
        # LINUXDO_SELECTORS 用 has-text("使用 Linux Do") 子串匹配,必能命中 llmroutes
        # 登录卡的「使用 Linux Do 继续」(has-text 是子串包含)。断言任一选择器能匹配该按钮文案。
        self.assertTrue(any("使用 Linux Do" in s for s in self.m.LINUXDO_SELECTORS))
        a = self._llmroutes_adapter()
        self.assertEqual(a.kind, "llmroutes")
        self.assertEqual(a.url, "https://llmroutes.cn/")
        self.assertIn("立即签到", " ".join(a.sign_selectors))
        # session cookie 名与 New API 系通用一致(实测 llmroutes.cn 域 new_api_refresh)。
        self.assertEqual(self.m.LLMRROUTES_SESSION_COOKIE, "new_api_refresh")

    def test_llmroutes_checkin_dual_oauth_both_ok(self):
        """双账号都登录签到成功 → 聚合 OK."""
        m = self.m
        calls: list[str] = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto:" + (url or ""))
                return None

        page = FakePage()

        async def fake_wait(*a, **k):
            calls.append("wait_text_ready")
            return True

        async def fake_cf(*a, **k):
            return None

        async def fake_clear(*a, **k):
            calls.append("clear_session")
            return True

        async def fake_dismiss(*a, **k):
            return 0

        async def fake_click(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "GitHub" in joined:
                calls.append("click_github_cta")
                return "github-sel"
            if "Linux Do" in joined or "Linux DO" in joined:
                calls.append("click_linuxdo_cta")
                return "linuxdo-sel"
            if "立即签到" in joined:
                calls.append("click_signin_btn")
                return "button:has-text(\"立即签到\")"
            return None

        async def fake_github_oauth(page, timeout=None, browser=None, return_host="agentrouter.org"):
            calls.append("try_github_oauth")
            calls.append(("gh_return_host", return_host))
            return "OK"

        async def fake_linuxdo_sso(page, origin_host="", browser=None):
            calls.append("try_linuxdo_sso")
            calls.append(("ld_origin_host", origin_host))
            return "OK"

        async def fake_terms(*a, **k):
            return 0

        async def fake_text(page, n=1500):
            # 含「立即签到」(触发点击路径) + 「今日已签到」(点击后确认门命中)
            return "用户 立即签到 今日已签到 累计签到 本月获得"

        async def fake_purl(page):
            return "https://llmroutes.cn/profile"

        async def fake_profile_account(page):
            return "github"  # 第一次调用期望切到 github

        # 区分两次 profile 账号读取:第一次 github,第二次 linuxdo(登出后切回)。
        seq = {"n": 0}
        async def fake_account_page(page):
            seq["n"] += 1
            return "github" if seq["n"] == 1 else "linuxdo"

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "click_first_visible", fake_click), \
             mock.patch.object(m, "try_github_oauth", fake_github_oauth), \
             mock.patch.object(m, "try_linuxdo_sso", fake_linuxdo_sso), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_purl), \
             mock.patch.object(m, "_llmroutes_clear_session", fake_clear), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss), \
             mock.patch.object(m, "_llmroutes_profile_account", fake_account_page):
            adapter = self._llmroutes_adapter()
            res = self.asyncio.run(m.llmroutes_checkin(page, adapter, browser=None))

        joined = " ".join(c for c in calls if isinstance(c, str))
        # 清 session 至少两次(登录前 + 切账号前)
        self.assertGreaterEqual(calls.count("clear_session"), 2)
        # GitHub OAuth 走 try_github_oauth;Linux Do 走 try_linuxdo_sso(不再预点 CTA)
        self.assertIn("click_github_cta", calls)
        self.assertNotIn("click_linuxdo_cta", calls)
        self.assertIn("try_github_oauth", calls)
        self.assertIn("try_linuxdo_sso", calls)
        # 两账号都点签到
        self.assertGreaterEqual(calls.count("click_signin_btn"), 2)
        # 聚合结果:双 OK → OK
        self.assertEqual(res.status, "OK")
        self.assertIn("dual-oauth llmroutes confirmed", res.detail)
        self.assertIn("github=OK", res.detail)
        self.assertIn("linuxdo=OK", res.detail)

    def test_llmroutes_checkin_one_account_fails(self):
        """一个账号失败 → 整体 FAIL(单账号成功绝不当 OK)."""
        m = self.m
        calls: list[str] = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto:" + (url or ""))
                return None

        page = FakePage()

        async def fake_wait(*a, **k):
            return True

        async def fake_cf(*a, **k):
            return None

        async def fake_clear(*a, **k):
            calls.append("clear_session")
            return True

        async def fake_dismiss(*a, **k):
            return 0

        async def fake_click(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "GitHub" in joined:
                calls.append("click_github_cta")
                return "github-sel"
            if "Linux Do" in joined:
                calls.append("click_linuxdo_cta")
                return "linuxdo-sel"
            if "立即签到" in joined:
                calls.append("click_signin_btn")
                return "button:has-text(\"立即签到\")"
            return None

        # GitHub 账号签到成功,Linux Do OAuth 失败(TIMEOUT)
        async def fake_github_oauth(page, timeout=None, browser=None, return_host="agentrouter.org"):
            return "OK"

        async def fake_linuxdo_sso(page, origin_host="", browser=None):
            calls.append("linuxdo_fail")
            return "TIMEOUT"

        async def fake_terms(*a, **k):
            return 0

        async def fake_text(page, n=1500):
            return "用户 立即签到"

        async def fake_purl(page):
            return "https://llmroutes.cn/profile"

        seq = {"n": 0}
        async def fake_account_page(page):
            seq["n"] += 1
            return "github"  # 仅第一次走 github

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "click_first_visible", fake_click), \
             mock.patch.object(m, "try_github_oauth", fake_github_oauth), \
             mock.patch.object(m, "try_linuxdo_sso", fake_linuxdo_sso), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_purl), \
             mock.patch.object(m, "_llmroutes_clear_session", fake_clear), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss), \
             mock.patch.object(m, "_llmroutes_profile_account", fake_account_page):
            adapter = self._llmroutes_adapter()
            res = self.asyncio.run(m.llmroutes_checkin(page, adapter, browser=None))

        # GitHub OK 但 LinuxDo 失败 → FAIL,绝不 OK(掩盖少一个账号额度)
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "dual_oauth_incomplete")
        self.assertIn("linuxdo=TIMEOUT", res.detail)


class TestUltrarouterDualOAuth(unittest.TestCase):
    """ultrarouter.org(New API 克隆)签到.

    2026-09-07:新增 ultrarouter 站点,kind='ultrarouter' 原为双账号流程(GitHub→your_github,
    LinuxDo→yourhandle)。
    2026-09-16:站方移除 GitHub OAuth(/sign-in 只剩「使用 LinuxDo 继续」),故改为
    单账号(LinuxDo -> yourhandle)签到。P0 红线:登出/清 session 只用
    _ultrarouter_clear_session 清 ultrarouter.org 域内 cookie,绝不全清。
    """

    def _ultrarouter_adapter(self):
        return self.m.SiteAdapter(
            name="ultrarouter",
            url="https://ultrarouter.org/keys",
            kind="ultrarouter",
            sign_selectors=[
                'button:has-text("立即签到")',
                'button:has-text("签到领取奖励")',
                'button:has-text("签到")',
            ],
            already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
            ready_rounds=15,
        )

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_ultrarouter_site_positive_negative(self):
        f = self.m.is_ultrarouter_site
        self.assertTrue(f("https://ultrarouter.org/profile"))
        self.assertTrue(f("https://ultrarouter.org/keys"))
        self.assertTrue(f("ultrarouter.org"))
        self.assertFalse(f("ultrarouter"))
        self.assertFalse(f("https://mzlone.top/"))
        self.assertFalse(f(""))

    def test_is_ultrarouter_name_positive_negative(self):
        f = self.m.is_ultrarouter_name
        self.assertTrue(f("ultrarouter"))
        self.assertTrue(f("ultrarouter-github"))
        self.assertFalse(f("mzlone"))
        self.assertFalse(f(""))
    def test_ultrarouter_profile_account_detection_live_format(self):
        """实况格式:绑定信息分行展示(@yourhandle 单独一行 + LinuxDO/已绑定)。

        2026-09-07 实跑发现:ultrarouter /profile 显示「@yourhandle」「LinuxDO」「已绑定」
        各占一行,旧检测(连续子串 'LinuxDO 已绑定')匹配不到 → WRONG_ACCOUNT(none)。
        修正后按独立 token 判定。
        """
        f = self.m._ultrarouter_profile_account_page_text
        live_linuxdo = "USERNAME\n用户\n用户 ID 515\n@yourhandle\n总消耗额度\n账户绑定\n未绑定\n绑定\nLinuxDO\n已绑定\n已绑定\n每日签到\n已签到"
        self.assertEqual(f(live_linuxdo), "linuxdo")
        live_github = "your_github\n用户\n用户 ID 88\nGitHub\n已绑定\nGitHub 已绑定"
        self.assertEqual(f(live_github), "github")
        self.assertEqual(f(""), "")
        self.assertEqual(f("普通文本 无账号信息"), "")

    def test_ultrarouter_profile_account_live_mangoqwq_20260921(self):
        """2026-09-21 9222 实况:/profile 是 @mangoqwq + 立即签到,无 @yourhandle/LinuxDO 字样.

        旧检测只认 @yourhandle 或 LinuxDO → SSO 成功后仍 WRONG_ACCOUNT(none)。
        """
        f = self.m._ultrarouter_profile_account_page_text
        live = (
            "M\nmango\n用户\n用户 ID 515\n@mangoqwq\n当前余额\n$247.15\n"
            "每日签到\n每日签到可获得随机额度奖励\n立即签到\n14\n累计签到\n"
            "$392\n本月获得"
        )
        self.assertEqual(f(live), "linuxdo")

    def test_ultrarouter_constants_nonempty(self):
        g = self.m.GITHUB_SELECTORS
        self.assertTrue(any("使用 GitHub 继续" in x for x in g))
        self.assertTrue(any("使用 Linux Do" in x for x in self.m.LINUXDO_SELECTORS))
        # 2026-09-21 9222 实况:登录态 cookie 是 new_api_has_session(path=/)。
        self.assertEqual(self.m.ULTRAROUTER_SESSION_COOKIE, "new_api_has_session")
        a = self._ultrarouter_adapter()
        self.assertEqual(a.kind, "ultrarouter")
        self.assertEqual(a.url, "https://ultrarouter.org/keys")
        self.assertIn("立即签到", " ".join(a.sign_selectors))

    def test_ultrarouter_checkin_linuxdo_ok(self):
        """未登录才走 LinuxDo SSO + 签到成功 → OK;不先清 cookie,不调 GitHub."""
        m = self.m
        calls = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto:" + (url or ""))

        page = FakePage()

        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_clear(*a, **k):
            calls.append("clear_session")
            return True
        async def fake_dismiss(*a, **k):
            return 0
        async def fake_click(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "立即签到" in joined:
                calls.append("click_signin_btn")
                return 'button:has-text("立即签到")'
            return None
        async def fake_linuxdo_sso(page, origin_host="", browser=None):
            calls.append("try_linuxdo_sso"); calls.append(("ld_origin", origin_host)); return "OK"
        async def fake_terms(*a, **k):
            return 0
        async def fake_text(page, n=1500):
            if "try_linuxdo_sso" not in calls:
                return "使用 LinuxDO 继续 登录您的账户"
            return "用户 ID 515 @mangoqwq 立即签到 今日已签到 累计签到 本月获得"
        async def fake_purl(page):
            return "https://ultrarouter.org/sign-in" if "try_linuxdo_sso" not in calls else "https://ultrarouter.org/profile"
        async def fake_account_page(page):
            return "linuxdo"

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "click_first_visible", fake_click), \
             mock.patch.object(m, "try_linuxdo_sso", fake_linuxdo_sso), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_purl), \
             mock.patch.object(m, "_ultrarouter_clear_session", fake_clear), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss), \
             mock.patch.object(m, "_ultrarouter_profile_account", fake_account_page):
            adapter = self._ultrarouter_adapter()
            res = self.asyncio.run(m.ultrarouter_checkin(page, adapter, browser=None))

        self.assertEqual(res.status, "OK")
        self.assertEqual(calls.count("clear_session"), 0)
        self.assertIn("try_linuxdo_sso", calls)
        self.assertNotIn("try_github_oauth", calls)
        self.assertNotIn("click_github_cta", calls)
        self.assertIn("click_signin_btn", calls)

    def test_ultrarouter_checkin_linuxdo_login_fail(self):
        """LinuxDo 登录失败 → 直接 FAIL,不再尝试 GitHub(理由透传 SSO 状态)."""
        m = self.m

        class FakePage:
            async def goto(self, url, **kwargs):
                return None
        page = FakePage()

        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_clear(*a, **k):
            return True
        async def fake_dismiss(*a, **k):
            return 0
        async def fake_linuxdo_sso(page, origin_host="", browser=None):
            return "TIMEOUT"
        async def fake_terms(*a, **k):
            return 0
        async def fake_text(page, n=1500):
            return "使用 LinuxDO 继续 登录您的账户"
        async def fake_purl(page):
            return "https://ultrarouter.org/sign-in"

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "try_linuxdo_sso", fake_linuxdo_sso), \
             mock.patch.object(m, "check_terms", fake_terms), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_purl), \
             mock.patch.object(m, "_ultrarouter_clear_session", fake_clear), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss):
            res = self.asyncio.run(self.m.ultrarouter_checkin(page, self._ultrarouter_adapter(), browser=None))

        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "TIMEOUT")
        self.assertIn("linuxdo", res.detail if res else "")

    def test_ultrarouter_reuses_existing_session_skips_forced_sso(self):
        """2026-09-21:9222 已登录且 /profile 有「立即签到」时,不得清 cookie / 走 SSO.

        今日失败链:专属流程每次 CDP deleteCookies session@ultrarouter.org/ 再
        try_linuxdo_sso → 早班 WRONG_ACCOUNT(none),午后再清后 NO_BUTTON。
        其它 New API 站复用已有登录态直接点签到;ultrarouter 应对齐。
        """
        m = self.m
        calls = []

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto:" + (url or ""))

        page = FakePage()

        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_clear(*a, **k):
            calls.append("clear_session")
            return True
        async def fake_dismiss(*a, **k):
            return 0
        async def fake_click(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "立即签到" in joined:
                calls.append("click_signin_btn")
                return 'button:has-text("立即签到")'
            return None
        async def fake_linuxdo_sso(page, origin_host="", browser=None):
            calls.append("try_linuxdo_sso")
            return "NO_BUTTON"
        async def fake_text(page, n=1500):
            return (
                "mango 用户 ID 515 @mangoqwq 立即签到 今日已签到 累计签到 "
                "本月获得 每日签到可获得随机额度奖励"
            )
        async def fake_purl(page):
            return "https://ultrarouter.org/profile"

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "click_first_visible", fake_click), \
             mock.patch.object(m, "try_linuxdo_sso", fake_linuxdo_sso), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_purl), \
             mock.patch.object(m, "_ultrarouter_clear_session", fake_clear), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss):
            res = self.asyncio.run(m.ultrarouter_checkin(page, self._ultrarouter_adapter(), browser=None))

        self.assertEqual(res.status, "OK")
        self.assertEqual(calls.count("clear_session"), 0)
        self.assertNotIn("try_linuxdo_sso", calls)
        self.assertIn("click_signin_btn", calls)

    def test_ultrarouter_waits_for_profile_spa_hydration(self):
        """/profile SPA 壳先出,约 1s 后才有 @mangoqwq/立即签到;不得因壳页走 SSO.

        2026-09-21 9222:wait_text_ready 在壳页(侧栏+语言偏好,无用户/签到)就返回,
        旧逻辑判未登录去 /sign-in,已登录会话重定向 /dashboard/overview → NO_BUTTON.
        """
        m = self.m
        calls = []
        texts = [
            "跳到主内容 Toggle Sidebar Ultra Router 主页 控制台 语言偏好 设置界面显示语言",
            "mango 用户 ID 515 @mangoqwq 立即签到 今日已签到 累计签到 本月获得",
        ]

        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto:" + (url or ""))

        page = FakePage()

        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_clear(*a, **k):
            calls.append("clear_session")
            return True
        async def fake_dismiss(*a, **k):
            return 0
        async def fake_click(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            if "立即签到" in joined:
                calls.append("click_signin_btn")
                return 'button:has-text("立即签到")'
            return None
        async def fake_linuxdo_sso(page, origin_host="", browser=None):
            calls.append("try_linuxdo_sso")
            return "NO_BUTTON"
        async def fake_text(page, n=1500):
            if texts:
                return texts.pop(0) if len(texts) > 1 else texts[0]
            return texts[0]
        async def fake_purl(page):
            return "https://ultrarouter.org/profile"

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "click_first_visible", fake_click), \
             mock.patch.object(m, "try_linuxdo_sso", fake_linuxdo_sso), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_purl), \
             mock.patch.object(m, "_ultrarouter_clear_session", fake_clear), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dismiss):
            res = self.asyncio.run(m.ultrarouter_checkin(page, self._ultrarouter_adapter(), browser=None))

        self.assertEqual(res.status, "OK")
        self.assertNotIn("try_linuxdo_sso", calls)
        self.assertIn("click_signin_btn", calls)


if __name__ == "__main__":
    unittest.main()

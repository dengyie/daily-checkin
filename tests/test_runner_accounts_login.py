#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from unittest import mock
import asyncio
import unittest

from tests._support import (
    FakeLoc,
    FakePage,
    _FakePage,
    load_mod,
)


class TestParseAccountsDoc(unittest.TestCase):
    """Account+password credential document parser (账密登录)."""

    def setUp(self):
        self.m = load_mod(force=True)

    def _real_doc(self) -> str:
        # mirror the live vault doc format (百倍/魔芋 real; iqach 注释作废; yeelo 占位不足)
        return (
            "## 纯账密登录的签到公益站\n"
            "\n"
            "> runner 自动登录用。格式：站名 / 账号 / 密码，逐站一组。\n"
            "> OAuth 优先的站（yeelo）除非 OAuth 走不通才填这里。\n"
            "\n"
            "百倍（sub.100xlabs.space）\n"
            "your_qq_email@example.com\n"
            "your_id_number\n"
            "\n"
            "魔芋（www.moyu.cn）\n"
            "yourhandle\n"
            "your_id_number\n"
            "\n"
            "iqach（new.iqach.top）\n"
            "（2026-08-21 已从签到系统删除，不用了）\n"
            "\n"
            "yeelo（img.yeelo.fun，OAuth 兜底用）\n"
            "___\n"
            "___\n"
        )

    def test_parses_real_doc_into_site_credential_map(self):
        creds = self.m.parse_accounts_doc(self._real_doc())
        self.assertEqual(creds, {
            "百倍": ("your_qq_email@example.com", "your_id_number"),
            "魔芋": ("yourhandle", "your_id_number"),
        })

    def test_comment_line_after_site_name_voids_group(self):
        # iqach: site-name line then a （…） comment line → group dropped (no real creds)
        creds = self.m.parse_accounts_doc("iqach（new.iqach.top）\n（已删除）\n")
        self.assertNotIn("iqach", creds)

    def test_placeholder_only_group_dropped(self):
        # yeelo: site-name then two ___ placeholders → not enough real values
        creds = self.m.parse_accounts_doc("yeelo（x）\n___\n___\n")
        self.assertNotIn("yeelo", creds)

    def test_empty_and_comment_only_returns_empty(self):
        self.assertEqual(self.m.parse_accounts_doc(""), {})
        self.assertEqual(self.m.parse_accounts_doc("# title\n\n> note\n"), {})

    def test_separator_and_heading_lines_skipped(self):
        creds = self.m.parse_accounts_doc(
            "# heading\n---\n百倍（a.com）\nu1\np1\n"
        )
        self.assertEqual(creds, {"百倍": ("u1", "p1")})

    def test_fullwidth_and_halfwidth_site_parens_both_supported(self):
        fullwidth = self.m.parse_accounts_doc("百倍（a.com）\nu\np\n")
        halfwidth = self.m.parse_accounts_doc("站名(a.com)\nu\np\n")
        self.assertIn("百倍", fullwidth)
        self.assertIn("站名", halfwidth)


class TestAccountLoginFlow(unittest.TestCase):
    """try_account_login_if_needed gating (no real browser)."""

    def setUp(self):
        self.m = load_mod(force=True)

    def test_no_credential_returns_no_credential_without_touching_page(self):
        # site with no registered credential → must short-circuit, never probe the page
        class BoomPage:
            async def locator(self, *a, **k):
                raise AssertionError("page must not be touched without creds")

        def fake_get(name):  # sync, mirrors real get_account_credential
            return None

        adapter = self.m.SiteAdapter(name="some_site_without_creds", url="https://x")
        res = self.m.asyncio.run(
            self.m.try_account_login_if_needed(BoomPage(), adapter, _get_credential=fake_get)
        )
        self.assertEqual(res, "NO_CREDENTIAL")

    def test_no_password_field_returns_no_button(self):
        # credential present but page has no password input → NO_BUTTON (no fill attempted)
        class NoPwPage:
            def locator(self, sel):
                class Loc:
                    async def first(self):
                        return self
                    first = property(lambda self_: self_)
                    async def is_visible(self, timeout=None):
                        return False
                return Loc()

        def fake_get(name):  # sync
            return ("user", "pw")

        async def fake_fill(page, selectors, value, timeout_each=800):
            return None  # nothing visible

        adapter = self.m.SiteAdapter(name="百倍", url="https://sub.100xlabs.space/login")
        res = self.m.asyncio.run(
            self.m.try_account_login_if_needed(
                NoPwPage(), adapter, _get_credential=fake_get, _fill_first_visible=fake_fill
            )
        )
        self.assertEqual(res, "NO_BUTTON")

    def test_password_type_text_name_pw_fallback_proceeds_to_fill(self):
        # columbina 2026-08-26: 密码框 input[name=password] type=text(非 type=password)。
        # 只认 type=password 会漏掉 → NO_BUTTON; add name=password 兜底后应继续到 fill。
        class Loc:
            def __init__(self, visible=False):
                self._v = visible
            @property
            def first(self):
                return self
            async def is_visible(self, timeout=None):
                return self._v
            async def fill(self, value, timeout=None):
                return None
            async def is_disabled(self):
                return False
            async def inner_text(self):
                return ""

        class PwTextPage:
            def locator(self, sel):
                if sel == "input[type='password']":
                    return Loc(visible=False)
                if sel == "input[name='password']":
                    return Loc(visible=True)
                # 其余(fill 用)让它可见但模拟 fill 落
                return Loc(visible=True)

        filled = []
        async def fake_fill(page, selectors, value, timeout_each=800):
            filled.append((selectors, value))
            return selectors[0]

        def fake_get(name):
            return ("USERNAME", "12345678")

        adapter = self.m.SiteAdapter(name="columbina", url="https://newapi.columbina.eu.org/sign-in")
        res = self.m.asyncio.run(
            self.m.try_account_login_if_needed(
                PwTextPage(), adapter, _get_credential=fake_get, _fill_first_visible=fake_fill
            )
        )
        self.assertEqual(res, "FAIL")  # submit 按钮不可见→ FAIL(但已走到 fill 阶段,非 NO_BUTTON)

    def test_verification_polls_through_slow_spa_redirect(self):
        """提交后 SPA 慢跳转(先 /login?expired=true 再 /console)不得误判 FAIL.

        tokenrouter 2026-10-23 实测: 提交后 2.5s 仍在 /login?expired=true,
        数秒后才跳 /console; 原先一次性校验会返回 FAIL 并丢弃一个有效会话.
        """
        class Loc:
            @property
            def first(self):
                return self

            async def is_visible(self, timeout=None):
                return True

            async def click(self, timeout=None, force=False):
                return None

            async def evaluate(self, *a, **k):
                return "checkbox"

        class SlowPage:
            def locator(self, sel):
                return Loc()

        async def fake_fill(page, selectors, value, timeout_each=800):
            return selectors[0]

        state = {"n": 0}

        async def fake_url(page):
            state["n"] += 1
            if state["n"] <= 2:
                return "https://www.tokenrouter.tech/login?expired=true"
            return "https://www.tokenrouter.tech/console"

        async def fake_text(page, n=600):
            return "控制台 当前余额 $16.38"

        adapter = self.m.SiteAdapter(
            name="tokenrouter", url="https://www.tokenrouter.tech/console/personal"
        )
        with mock.patch.object(self.m, "check_terms", mock.AsyncMock(return_value=0)), \
             mock.patch.object(self.m, "page_url", fake_url), \
             mock.patch.object(self.m, "page_text", fake_text), \
             mock.patch.object(self.m, "LOGIN_VERIFY_POLL_S", 0.01):
            res = self.m.asyncio.run(
                self.m.try_account_login_if_needed(
                    SlowPage(), adapter,
                    _get_credential=lambda name: ("USERNAME", "not-a-real-password"),
                    _fill_first_visible=fake_fill,
                )
            )
        self.assertEqual(res, "OK")
        self.assertGreater(state["n"], 2)

    def test_fill_first_visible_fills_first_visible_selector(self):
        filled = {}

        class FakeLoc:
            def __init__(self, visible):
                self._visible = visible

            async def first(self):
                return self
            first = property(lambda self_: self_)

            async def is_visible(self, timeout=None):
                return self._visible

            async def fill(self, value, timeout=None):
                filled["value"] = value
                return None

        class FakePage:
            def locator(self, sel):
                # second selector ('#username') is visible
                return FakeLoc(visible=(sel == "#username"))

        async def run():
            return await self.m.fill_first_visible(
                FakePage(), ["#email", "#username", "#other"], "alice"
            )
        sel = self.m.asyncio.run(run())
        self.assertEqual(sel, "#username")
        self.assertEqual(filled["value"], "alice")


class TestLinuxdoAuthorizeUrl(unittest.TestCase):
    """is_linuxdo_authorize_url must only match real OAuth authorize pages.

    Regression: host=='linux.do' alone returned True, so a profile-resident
    linux.do topic page (https://linux.do/t/topic/.../n) was treated as the
    OAuth authorization page. try_linuxdo_sso's main loop then broke on that
    topic tab before ever reaching the real connect.linux.do/oauth2/authorize
    tab, clicked AUTHORIZE_SELECTORS on a page with no 「允许」 button, and
    every SSO site timed out with saw_linuxdo=True / authorize:0.
    """

    def setUp(self):
        self.m = load_mod(force=True)
        self.f = self.m.is_linuxdo_authorize_url

    def test_real_oauth_authorize_pages_match(self):
        self.assertTrue(self.f("https://connect.linux.do/oauth2/authorize?client_id=x"))
        self.assertTrue(self.f("https://linux.do/oauth/authorize?client_id=x"))
        self.assertTrue(self.f("https://linux.do/oauth2/authorize?response_type=code"))

    def test_linuxdo_topic_and_home_pages_do_not_match(self):
        # the resident tabs that broke the batch
        self.assertFalse(self.f("https://linux.do/t/topic/2666722/3"))
        self.assertFalse(self.f("https://linux.do/t/topic/2785341/6"))
        self.assertFalse(self.f("https://linux.do/"))
        self.assertFalse(self.f("https://linux.do/latest"))
        self.assertFalse(self.f("https://linux.do/u/yourhandle"))

    def test_non_linuxdo_oauth_hosts_do_not_match(self):
        self.assertFalse(self.f("https://free.vipclaude.codes/oauth/linuxdo"))
        self.assertFalse(self.f("https://ai.112102.xyz/profile"))
        self.assertFalse(self.f("https://newapi.sorai.me/profile"))


class TestSigninApi(unittest.TestCase):
    """signin_api field on SiteAdapter, _B pass-through, try_signin_api, and anyrouter config."""

    def setUp(self):
        self.m = load_mod(force=True)

    def test_site_adapter_has_signin_api_field(self):
        # default is "" (no JSON sign-in API); only opted-in sites are non-empty
        a = self.m.SiteAdapter(name="plain", url="https://x")
        self.assertEqual(a.signin_api, "")
        b = self.m.SiteAdapter(name="api", url="https://y", signin_api="/api/user/sign_in")
        self.assertEqual(b.signin_api, "/api/user/sign_in")

    def test_b_passes_signin_api(self):
        via_b = self.m._B("test", "https://x", signin_api="/api/user/sign_in")
        self.assertEqual(via_b.signin_api, "/api/user/sign_in")
        default = self.m._B("test2", "https://y")
        self.assertEqual(default.signin_api, "")

    def test_anyrouter_has_signin_api(self):
        # resolve the anyrouter adapter from the site catalog
        resolved = self.m.resolve_site("anyrouter", "https://anyrouter.top/console")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.signin_api, "/api/user/sign_in")

    def test_try_signin_api_noop_without_api(self):
        adapter = self.m.SiteAdapter(name="plain", url="https://x")
        page = _FakePage()
        import asyncio as _aio
        self.assertEqual(_aio.run(self.m.try_signin_api(page, adapter)), "")

    def test_try_signin_api_ok_on_success_true(self):
        adapter = self.m.SiteAdapter(name="api", url="https://anyrouter.top",
                                     signin_api="/api/user/sign_in")
        page = _FakePage(fetch_result={"status": 200, "text": '{"message":"","success":true}',
                                       "success": True})
        import asyncio as _aio
        self.assertEqual(_aio.run(self.m.try_signin_api(page, adapter)), "OK")

    def test_try_signin_api_no_grant_on_success_false(self):
        adapter = self.m.SiteAdapter(name="api", url="https://x", signin_api="/api/user/sign_in")
        page = _FakePage(fetch_result={"status": 200, "text": '{"message":"","success":false}',
                                       "success": False})
        import asyncio as _aio
        self.assertEqual(_aio.run(self.m.try_signin_api(page, adapter)), "NO_GRANT")

    def test_try_signin_api_fail_on_network_error(self):
        adapter = self.m.SiteAdapter(name="api", url="https://x", signin_api="/api/user/sign_in")
        page = _FakePage(eval_error=RuntimeError("boom"))
        import asyncio as _aio
        self.assertEqual(_aio.run(self.m.try_signin_api(page, adapter)), "FAIL")

    def test_try_signin_api_auth_http_failure(self):
        # 401/403 = not authenticated, NOT a declined sign-in. Must map to AUTH
        # so the guard reports auth_required instead of a misleading no_grant.
        adapter = self.m.SiteAdapter(name="api", url="https://x", signin_api="/api/user/sign_in")
        import asyncio as _aio
        for code in (401, 403):
            page = _FakePage(fetch_result={"status": code, "text": "unauthorized", "success": False})
            self.assertEqual(_aio.run(self.m.try_signin_api(page, adapter)), "AUTH", code)
        # The OK contract: post-action results must carry PENDING→DONE + runner
        # + a kind that is a valid ActionKind (api_post, not api_call), else a
        # stricter consumer/CI would reject them exactly like run validation.
        from checkin_core.models import ActionKind
        adapter = self.m.SiteAdapter(name="api", url="https://anyrouter.top",
                                     signin_api="/api/user/sign_in")
        res = self.m.signin_api_result(adapter, "余额 $1086.89")
        self.assertEqual(res.status, "OK")
        self.assertEqual(res.pre_state, "PENDING")
        self.assertEqual(res.post_state, "DONE")
        self.assertEqual(res.transition, "PENDING_TO_DONE")
        self.assertEqual(res.attribution, "runner")
        self.assertTrue(res.action and res.action.kind in ActionKind.__args__)
        # apply_attribution must NOT rewrite this OK into unattributed_success
        kept = self.m.apply_attribution(res)
        self.assertEqual(kept.status, "OK")


if __name__ == "__main__":
    unittest.main()

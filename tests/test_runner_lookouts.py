#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
import re
import unittest

from tests._support import (
    TARGET,
    load_mod,
)


class TestLookoutsAndSites(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_bohe_nav_login_not_logged_out(self):
        text = "首页\n登录\n今日\n开始转动\n帮助"
        self.assertFalse(self.m.looks_logged_out(text))

    def test_linuxdo_cta_is_logged_out(self):
        text = "使用 Linux DO 继续\n其他"
        self.assertTrue(self.m.looks_logged_out(text))

    def test_linux_dot_do_cta_is_logged_out(self):
        self.assertTrue(self.m.looks_logged_out("使用 Linux.do 登录\n邮箱\n密码"))
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn('使用 Linux.do 登录', src)

    def test_done_via_sign_scan_contract(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("done via sign scan", src)
        self.assertIn("DONE:", src)


    def test_is_bohe_site(self):
        self.assertTrue(self.m.is_bohe_site("https://qd.x666.me/"))
        self.assertFalse(self.m.is_bohe_site("https://example.com"))

    def test_is_lucky_flip_site(self):
        self.assertTrue(self.m.is_lucky_flip_site("https://fuli.lucky0625.qzz.io/"))
        self.assertFalse(self.m.is_lucky_flip_site("https://example.com"))

    def test_resolve_bohe_kind(self):
        a = self.m.resolve_site("薄荷公益站", "")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "bohe")
        b = self.m.resolve_site("HotaruAPI", "")
        self.assertEqual(b.kind, "newapi_profile")
        c = self.m.resolve_site("7倍", "")
        self.assertEqual(c.kind, "newapi_profile")
        self.assertEqual(c.sign_selectors, list(self.m.NEWAPI_SIGN_SELECTORS))

    def test_unknown_profile_url_is_newapi(self):
        a = self.m.resolve_site("未知站", "https://example.com/profile")
        self.assertEqual(a.kind, "newapi_profile")
        b = self.m.resolve_site("未知2", "https://example.com/other")
        self.assertEqual(b.kind, "browser")

    def test_resolve_login_url_override_survives_note_url(self):
        # gemai /login 404,真登录卡在 /sign-in(login_url override)。resolve_site
        # 的 catalog branch 在 url_from_note 存在时会重建 SiteAdapter,必须把
        # login_url 透传,否则 _ensure_account_logged_in 拿不到 override 回落
        # /login → 404 → 账密登录永不触发(2026-08-26 smoke 实证 auth_required)。
        a = self.m.resolve_site("gemai", "https://api.gemai.cc/profile")
        self.assertIsNotNone(a)
        self.assertEqual(a.login_url, "https://api.gemai.cc/sign-in")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.url, "https://api.gemai.cc/profile")


class TestTabBindContract(unittest.TestCase):
    def test_no_last_page_fallback(self):
        src = TARGET.read_text(encoding="utf-8")
        fn = re.search(r"async def find_temp_page\(.*?\n(?=async def |\ndef )", src, re.S)
        self.assertIsNotNone(fn)
        body = fn.group(0)
        self.assertNotIn("return pages[-1]", body)
        self.assertNotIn("blanks[-1]", body)
        self.assertIn("tab_id", body)
        self.assertIn("page_cdp_target_id", body)

    def test_close_retry_and_extra_cleanup(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("async def close_extra_pages", src)
        self.assertIn("close tab failed attempt=", src)
        self.assertIn("close tab GIVE UP", src)
        self.assertIn("owned_root_target_id=tab_id", src)
        self.assertIn("if tab_id:", src)
        self.assertIn("await _sso_cleanup()", src)
        self.assertIn("sso_root_target_id = await page_cdp_target_id(page)", src)
        self.assertIn("owned_root_target_id=sso_root_target_id", src)

    def test_no_site_defs_alias(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertNotIn("SITE_DEFS", src)
        self.assertNotRegex(src, r"(?m)^INTERACTIVE\s*=")
        self.assertIn("async def wait_out_captcha", src)
        self.assertIn("async def wait_out_cloudflare", src)
        self.assertIn("try_click_challenge_widgets", src)
        self.assertIn("wait_out_captcha(page, CAPTCHA_WAIT_S)", src)


if __name__ == "__main__":
    unittest.main()

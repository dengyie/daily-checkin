#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from unittest import mock
import asyncio
import inspect
import re
import pytest
import unittest
import yaml

from tests._support import (
    FakeLoc,
    FakePage,
    ROOT,
    TARGET,
    load_mod,
)

pytestmark = [pytest.mark.runner]


class TestNexaDualAccount(unittest.TestCase):
    """nexavlinks.com(New API 聚合站)—— 2026-09-11 拆分单账号签到。

    拆成两个独立任务:① nexavlinks-linuxdo = LinuxDo OAuth 单账号(复用 9222
    linux.do 会话);② nexavlinks-email = 邮箱+密码单账号(账号密码从 accounts
    文件按站名读取,不硬编码)。每个任务先登出再签到,不再双账号聚合轮换。

    P0 红线:登出只点 NEXA 站内「退出登录」(清 NEXA 域 localStorage),唯一仅
    清本站;绝不用 BrowserContext.clear_cookies()/clear localStorage 全清 9222。
    """

    def _nexa_adapter(self, name="nexavlinks-linuxdo"):
        return self.m.SiteAdapter(
            name=name,
            url="https://www.nexavlinks.com/check-in",
            kind="nexa",
            sign_selectors=['button:has-text("立即签到")'],
            already_selectors=['text=今日已签到', 'text=签到成功'],
            ready_rounds=15,
        )

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def test_is_nexa_site_positive_negative(self):
        f = self.m.is_nexa_site
        self.assertTrue(f("https://www.nexavlinks.com/check-in"))
        self.assertTrue(f("https://www.nexavlinks.com/"))
        self.assertTrue(f("nexavlinks.com"))
        self.assertFalse(f("nexavlinks"))
        self.assertFalse(f("https://mzlone.top/"))
        self.assertFalse(f(""))

    def test_is_nexa_name_positive_negative(self):
        f = self.m.is_nexa_name
        self.assertTrue(f("nexavlinks"))
        self.assertTrue(f("nexavlinks-github"))
        self.assertFalse(f("ultrarouter"))
        self.assertFalse(f(""))

    def test_nexa_constants_nonempty(self):
        # 2026-09-11 拆分单账号后不再有硬编码占位账号常量;关键选择器保留。
        self.assertTrue(any("Linux" in x for x in self.m.LINUXDO_SELECTORS))
        self.assertTrue(any("退出登录" in x for x in self.m.NEXA_LOGOUT_SELECTORS))
        a = self._nexa_adapter()
        self.assertEqual(a.kind, "nexa")
        self.assertIn("立即签到", " ".join(a.sign_selectors))

    def test_nexa_result_both_ok(self):
        r = self.m._nexa_result("nexa", {"linuxdo": "OK", "email": "OK"})
        self.assertEqual(r.status, "OK")
        self.assertIn("dual-account", r.detail)
        r2 = self.m._nexa_result("nexa", {"linuxdo": "ALREADY", "email": "OK"})
        self.assertEqual(r2.status, "OK")

    def test_nexa_result_single_account_fails(self):
        """单账号成功绝不报 OK(会掩盖另一账号未签,同 agentrouter)."""
        r = self.m._nexa_result("nexa", {"linuxdo": "OK", "email": "FAIL"})
        self.assertEqual(r.status, "FAIL")
        self.assertEqual(r.reason, "dual_account_incomplete")
        self.assertIn("linuxdo=OK", r.detail)
        self.assertTrue(any(k in r.detail for k in ("email",)))

    def test_nexa_result_both_fail(self):
        r = self.m._nexa_result("nexa", {"linuxdo": "TIMEOUT", "email": "NO_FORM"})
        self.assertEqual(r.status, "FAIL")
        self.assertEqual(r.reason, "dual_account_incomplete")

    def test_nexa_current_email_parses_localstorage(self):
        m = self.m
        async def fake_eval(expr):
            # 模拟浏览器 evaluate 返回 auth_user 的 JSON 对象(playwright 序列化为 str)
            return '{"email":"your_primary_email@example.com","username":""}'
        class P:
            async def evaluate(self, expr):
                return await fake_eval(expr)
        out = self.asyncio.run(self.m._nexa_current_email(P()))
        self.assertEqual(out, "your_primary_email@example.com")

    def test_nexa_current_email_empty_on_missing(self):
        m = self.m
        class P:
            async def evaluate(self, expr):
                return ""
        self.assertEqual(self.asyncio.run(self.m._nexa_current_email(P())), "")

    def test_nexa_checkin_dual_ok(self):
        """LinuxDO 单账号签到成功 → OK;登出一次,不碰 clear_cookies."""
        m = self.m
        calls = []
        fake_logout = 0
        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_logout_fn(page):
            nonlocal fake_logout
            fake_logout += 1
            calls.append("logout")
            return True
        async def fake_linuxdo_gate(page, adapter, browser=None):
            return "OK"
        async def fake_signin(page, adapter):
            return m.confirmed_done_result("签到成功", adapter="nexa")

        class FakePage:
            async def goto(self, url, **kw):
                return ""
            async def evaluate(self, expr):
                return ""
        page = FakePage()

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "_nexa_logout", fake_logout_fn), \
             mock.patch.object(m, "_nexa_linuxdo_gate", fake_linuxdo_gate), \
             mock.patch.object(m, "_nexa_signin_current", fake_signin):
            res = self.asyncio.run(
                m.nexa_linuxdo_checkin(page, self._nexa_adapter("nexavlinks-linuxdo"), browser=None)
            )

        self.assertIn(res.status, ("OK", "ALREADY"))
        self.assertGreaterEqual(fake_logout, 1)
        self.assertIn("logout", calls)

    def test_nexa_checkin_email_fail_fails(self):
        """邮箱账号登录失败 → FAIL(单账号流程,登录失败直接 FAIL,不再聚合)."""
        m = self.m
        class FakePage:
            async def goto(self, url, **kw):
                return ""
        page = FakePage()
        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_logout(page):
            return True
        async def fake_email_gate(page, adapter):
            return "NO_SUBMIT"
        async def fake_signin(page, adapter):
            return m.confirmed_done_result("签到成功", adapter="nexa")
        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "_nexa_logout", fake_logout), \
             mock.patch.object(m, "_nexa_email_gate", fake_email_gate), \
             mock.patch.object(m, "_nexa_signin_current", fake_signin):
            res = self.asyncio.run(
                m.nexa_email_checkin(page, self._nexa_adapter("nexavlinks-email"), browser=None)
            )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "login_failed")

    def test_nexa_is_subflow_name(self):
        """子流程判定:名字含 email → email 流程;含 linuxdo/默认 → linuxdo."""
        self.assertTrue(self.m.is_nexa_email_name("nexavlinks-email"))
        self.assertTrue(self.m.is_nexa_email_name("nexa-mail"))
        self.assertFalse(self.m.is_nexa_email_name("nexavlinks-linuxdo"))
        self.assertFalse(self.m.is_nexa_email_name("nexavlinks"))

    def test_nexa_sites_yaml_split_entries(self):
        """sites.yaml 拆成两个单账号条目,且都解析成 kind=nexa."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        names = {e.get("name"): e for e in data.get("sites", [])}
        self.assertIn("nexavlinks-linuxdo", names)
        self.assertIn("nexavlinks-email", names)
        from stealth_checkin_runner import _adapter_from_yaml_entry
        for nm in ("nexavlinks-linuxdo", "nexavlinks-email"):
            a = _adapter_from_yaml_entry(names[nm])
            self.assertEqual(a.kind, "nexa")

    def test_legacy_dispatch_has_both_nexa_single_account_branches(self):
        """分发链同时含 nexa_email_checkin 与 nexa_linuxdo_checkin 两个分支."""
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("nexa_email_checkin(page, adapter, browser=browser)", src)
        self.assertIn("nexa_linuxdo_checkin(page, adapter, browser=browser)", src)

    def test_p0_no_clear_cookies_in_nexa_path(self):
        """NEXA 专属流程不得存在 clear_cookies/clear_localStorage 全清调用."""
        import inspect
        src = inspect.getsource(self.m.nexa_linuxdo_checkin)
        src += inspect.getsource(self.m.nexa_email_checkin)
        src += inspect.getsource(self.m._nexa_logout)
        lowercase = src.lower()
        self.assertNotIn("clear_cookies", lowercase)
        self.assertNotIn("clear_localstorage", lowercase)
        self.assertNotIn("clear_all", lowercase)

    def test_nexa_announcement_dismiss_constants(self):
        """公告弹窗关闭选择器包含标记已读与遮罩弹窗选择器."""
        sels = " ".join(self.m.NEXA_ANNOUNCEMENT_DISMISS_SELECTORS)
        self.assertIn("标记已读", sels)
        self.assertIn("z-[120]", sels)

    def test_nexa_has_interactive_captcha_signals(self):
        """NEXA 图形点选签到验证码文本特征被判定为人机验证."""
        sample_text = "签到验证\n按照提示图形的顺序，点击大图中的对应图形。请依次点击"
        self.assertTrue(self.m.has_interactive_captcha(sample_text))

    def test_nexa_dismiss_announcements_calls_locator_and_eval(self):
        """_nexa_dismiss_announcements 正确触发定位器点击与 Pinia 状态清理."""
        eval_called = []
        class FakeLoc:
            @property
            def first(self):
                return self
            async def is_visible(self, timeout=None):
                return True
            async def click(self, timeout=None, force=False):
                eval_called.append("btn_click")
        class FakePage:
            def locator(self, sel):
                return FakeLoc()
            async def evaluate(self, expr):
                eval_called.append("js_eval")
                return 1
        page = FakePage()
        res = self.asyncio.run(self.m._nexa_dismiss_announcements(page))
        self.assertTrue(res)
        self.assertIn("btn_click", eval_called)
        self.assertIn("js_eval", eval_called)

    def test_nexa_signin_current_detects_challenge_and_returns_interactive(self):
        """当签到点击后弹出 CheckInChallengeDialog 且未完成时,返回 interactive 报错."""
        m = self.m
        class FakePage:
            async def goto(self, url, **kw):
                return ""
        page = FakePage()
        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_dismiss(*a, **k):
            return True
        async def fake_page_text(p, limit=4000):
            # 点前为未签 CTA,点后为图形验证码弹窗文本
            if not getattr(fake_page_text, "clicked", False):
                return "立即签到"
            return "签到验证\n按照提示图形的顺序，点击大图中的对应图形。请依次点击"
        fake_page_text.clicked = False

        async def fake_click(p, sels, **kw):
            fake_page_text.clicked = True
            return "button:has-text('立即签到')"
        async def fake_wait_out_captcha(p, max_s=40.0):
            return "INTERACTIVE"

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "_nexa_dismiss_announcements", fake_dismiss), \
             mock.patch.object(m, "page_text", fake_page_text), \
             mock.patch.object(m, "click_first_visible", fake_click), \
             mock.patch.object(m, "wait_out_captcha", fake_wait_out_captcha):
            res = self.asyncio.run(
                m._nexa_signin_current(page, self._nexa_adapter("nexavlinks-email"))
            )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "interactive")
        self.assertIn("interactive click captcha", res.detail)


class TestRelayForLoanCycle(unittest.TestCase):
    """relayfor.xyz(RelayFor / 词元贷借贷站)—— 2026-09-11 接入。

    该站是「词元贷」借贷站,不是普通每日签到:第 1 天点「确认借款」领额度,后续
    每天点「今日签到还款」还贷,4 周期结清。每日运行按 DOM 自判:可借则点借,
    否则有还则点还,两按钮均 disabled / 已还清 → ALREADY。复用 9222 共享登录态
    (不配账密)。

    P0 红线:relayfor 专属流程只点站内借款/还款按钮,绝不调用 clear_cookies /
    clear_localStorage / clear_all(全清 9222 其它域)。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def _adapter(self):
        return self.m.SiteAdapter(
            name="relayfor",
            url="https://relayfor.xyz/console/#benefits",
            kind="relayfor",
            sign_selectors=['button:has-text("确认借款")', 'button:has-text("今日签到还款")'],
            already_selectors=["text=已还清", "text=今日已处理"],
            ready_rounds=10,
        )

    def test_is_relayfor_site_positive_negative(self):
        f = self.m.is_relayfor_site
        self.assertTrue(f("https://relayfor.xyz/console/#benefits"))
        self.assertTrue(f("https://relayfor.xyz/"))
        self.assertTrue(f("relayfor.xyz"))
        self.assertFalse(f("https://mzlone.top/"))
        self.assertFalse(f(""))
        self.assertFalse(f("https://example.com/"))

    def test_kind_in_literal(self):
        self.assertIn("relayfor", self.m.AdapterKind.__args__)

    def test_resolve_relayfor_kind_is_dedicated(self):
        m = self.m
        a = m.resolve_site("relayfor", "https://relayfor.xyz/console/#benefits")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "relayfor")
        self.assertTrue(a.url.startswith("https://relayfor.xyz"))

    def test_yaml_registers_relayfor_entry(self):
        """sites.yaml 的 relayfor 条目解析后 kind=relayfor 且带借/还/活动签名。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "relayfor"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 relayfor 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "relayfor")
        joined = " ".join(a.sign_selectors)
        self.assertIn("确认借款", joined)
        self.assertIn("今日签到还款", joined)
        self.assertIn("立即签到", joined)

    def test_dispatch_contains_relayfor_branch(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn('is_relayfor_site(site_url)', src)
        self.assertIn('return await relayfor_checkin(page, adapter, browser=browser)', src)

    def test_p0_no_clear_cookies_in_relayfor_path(self):
        """relayfor 专属流程不得存在 clear_cookies / clear_localstorage / 全清调用."""
        import inspect
        src = inspect.getsource(self.m.relayfor_checkin)
        src += inspect.getsource(self.m._relayfor_available_button)
        src += inspect.getsource(self.m._relayfor_done_state)
        lower = src.lower()
        self.assertNotIn("clear_cookies", lower)
        self.assertNotIn("clear_localstorage", lower)
        self.assertNotIn("clear_all", lower)

    def _run_handler(self, fake_locators_by, page_text="", page_text_after=None, click_error=None, adapter=None,
                     repay_grace_s=0.0, click_error_after=None):
        """Run relayfor_checkin against a controllable FakePage.

        fake_locators_by: dict mapping selector-substring -> {"is_visible": bool, "disabled": bool}.
        A FakeLocator matches when the selector contains that substring. Clicks are recorded.
        page_text: body text returned by page_text() before the action click.
        page_text_after: body text returned after the action click (to simulate the landed state);
          when None the text stays constant. This lets tests exercise the before/after delta
          confirmation in _relayfor_confirm_after.
        click_error: optional exception to raise from the click, to exercise the no_click path.
        click_error_after: 从第 N 次点击(1-based)开始抛 click_error;None = 每次点击都抛。
          用来测「借款点成功、补点还款失败」这类多步动作的中途失败。
        repay_grace_s: 覆盖「只见借款时等还款」的宽限窗口;默认 0.0 让既有用例保持快跑,
          只有显式测宽限行为的用例才传正值。
        Returns (res, clicks).
        """
        m = self.m
        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_dis(*a, **k):
            return None

        clicks = []
        state = {"text": page_text}
        polls = {}

        class FakeLocator:
            def __init__(self, sel):
                self.sel = sel
                self.sub = None
                self.cfg = None
                for sub, c in fake_locators_by.items():
                    if sub in sel:
                        self.sub = sub
                        self.cfg = c
                        break
            @property
            def first(self):
                return self
            async def is_visible(self, timeout=800):
                if self.cfg is None:
                    return False
                # enable_after_polls: 模拟按钮晚一拍挂载/解禁——第 N 次可见性
                # 探测后才变成可用,用来验证「只见借款时先等还款」的宽限。
                if self.cfg.get("enable_after_polls") is not None:
                    polls[self.sub] = polls.get(self.sub, 0) + 1
                    if polls[self.sub] >= int(self.cfg["enable_after_polls"]):
                        self.cfg = {**self.cfg, "disabled": False}
                return bool(self.cfg.get("is_visible"))
            async def is_disabled(self):
                if self.cfg is None:
                    return False
                # enable_after_click: 借出落账后站点才解禁「今日签到还款」。
                if self.cfg.get("enable_after_click") and clicks:
                    return False
                return bool(self.cfg.get("disabled"))
            async def click(self, **kw):
                if click_error is not None and (
                    click_error_after is None or len(clicks) + 1 >= click_error_after
                ):
                    raise click_error
                clicks.append(self.sel)
                if page_text_after is not None:
                    # list/tuple 支持多步动作:第 N 次点击后用第 N 段文案。
                    if isinstance(page_text_after, (list, tuple)):
                        idx = min(len(clicks) - 1, len(page_text_after) - 1)
                        state["text"] = page_text_after[idx]
                    else:
                        state["text"] = page_text_after

        class FakePage:
            async def goto(self, url, **kw):
                return ""
            async def evaluate(self, expr):
                return ""
            def locator(self, sel):
                return FakeLocator(sel)

        async def fake_page_text(page, n=2000):
            return state["text"]

        page = FakePage()
        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dis), \
             mock.patch.object(m, "page_text", fake_page_text), \
             mock.patch.object(m, "RELAYFOR_REPAY_GRACE_S", repay_grace_s):
            res = self.asyncio.run(m.relayfor_checkin(page, adapter or self._adapter(), browser=None))
        return res, clicks

    def test_borrow_day_clicks_confirm_loan_then_repays_same_day(self):
        """借出当天必须接着点「今日签到还款」:借款只开出周期,还款才是当日签到.

        2026-10-30 事故:08:18 借出 $1.00 后直接返回 OK,页面仍是待还 $1.00 /
        签到进度 0/7,当天实际没有签到。
        """
        fake_locators_by = {
            'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
            'button:has-text("今日签到还款")': {"is_visible": True, "disabled": True, "enable_after_click": True},
        }
        res, clicks = self._run_handler(
            fake_locators_by,
            page_text='已还清\n可再借',
            page_text_after=[
                '当前待还 $1.00\n今日签到还款',            # 借款落账
                '当前待还 $0.72\n签到进度\n1 / 7 次\n今日已签到\n今日签到还款完成',
            ],
        )
        self.assertEqual(res.status, "OK", res.detail)
        self.assertIn("借款", res.detail)
        self.assertIn("还款", res.detail)
        self.assertTrue(any("确认借款" in c for c in clicks), "应先点击 确认借款")
        self.assertTrue(any("今日签到还款" in c for c in clicks), "借出后必须补点 今日签到还款")
        self.assertEqual(clicks[-1].count("今日签到还款"), 1, "最后一步应为还款")

    def test_repay_preferred_when_borrow_also_visible(self):
        """借/还同时可见时先还款:还款才是签到,借款按钮可能只是下周期预告."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": False},
            },
            page_text='当前待还\n$1.00\n今日签到还款',
            page_text_after='当前待还\n$0.72\n今日已签到',
        )
        self.assertEqual(res.status, "OK", res.detail)
        self.assertTrue(any("今日签到还款" in c for c in clicks), "应点击 今日签到还款")
        self.assertFalse(any("确认借款" in c for c in clicks), "不得抢跑点击 确认借款")

    def test_borrow_waits_grace_for_late_repay_before_borrowing(self):
        """只见「确认借款」时先给还款一个短宽限:还款晚一拍挂载也不抢跑借款.

        待还未结清期间站点可能同时/先后放出「确认借款」(下周期预告)与
        「今日签到还款」;若只凭第一眼没看到还款就借款,会多开一个周期。
        第 3 次探测才解禁 → 宽限必须真的「轮询过时间」才等得到,而不是只多探一次。
        """
        m = self.m
        with mock.patch.object(m, "RELAYFOR_REPAY_POLL_S", 0.01):
            res, clicks = self._run_handler(
                {
                    'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
                    # 还款按钮晚两拍解禁:覆盖「首次探测 + 宽限内首次探测」之后才可用。
                    'button:has-text("今日签到还款")': {
                        "is_visible": True, "disabled": True, "enable_after_polls": 3,
                    },
                },
                page_text='当前待还\n$1.00\n今日签到还款',
                page_text_after='当前待还\n$0.72\n今日已签到',
                repay_grace_s=0.05,
            )
        self.assertEqual(res.status, "OK", res.detail)
        self.assertTrue(any("今日签到还款" in c for c in clicks), "宽限内等到还款后应点还款")
        self.assertFalse(any("确认借款" in c for c in clicks), "还款晚到也不得抢跑借款")

    def test_wait_repay_button_honours_timeout_window(self):
        """等还款按钮严格守窗口:window=0 只探一次,window>0 才会轮询到解禁。

        2026-10-30 评审 P3:旧循环在 deadline 检查后才 sleep,window=0 也会多等
        一个轮询周期,宽限测试因此可以「零窗口也通过」。
        """
        calls = {"n": 0}

        async def fake_available(page, selectors):
            calls["n"] += 1
            return "button[data-token-loan-checkin]" if calls["n"] >= 3 else None

        async def run(window):
            calls["n"] = 0
            with mock.patch.object(self.m, "_relayfor_available_button", fake_available):
                return await self.m._wait_relayfor_repay_button(object(), window)

        with mock.patch.object(self.m, "RELAYFOR_REPAY_POLL_S", 0.01):
            self.assertIsNone(self.asyncio.run(run(0.0)), "零窗口不应继续轮询")
            self.assertEqual(
                self.asyncio.run(run(0.05)), "button[data-token-loan-checkin]",
                "窗口内轮询到解禁应返回还款 selector",
            )

    def test_borrow_rechecks_repay_before_clicking_borrow(self):
        """评审 P1:第 2 步锁了借款,更晚才挂载的还款必须在点借款前被复查到.

        活动签到/慢渲染会再耗数秒;若点借款前不复查,待还未结清时点「确认借款」
        (下周期预告)会多开周期,甚至被站点吞掉导致当天漏签。
        """
        m = self.m
        with mock.patch.object(m, "RELAYFOR_REPAY_POLL_S", 0.01):
            res, clicks = self._run_handler(
                {
                    'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
                    # 还款在宽限窗口内始终不可用(超过第 2 步宽限),直到第 7 步复查才解锁。
                    'button:has-text("今日签到还款")': {
                        "is_visible": True, "disabled": True, "enable_after_polls": 6,
                    },
                },
                page_text='当前待还\n$1.00\n今日签到还款',
                page_text_after='当前待还\n$0.72\n今日已签到',
                repay_grace_s=0.02,
            )
        self.assertEqual(res.status, "OK", res.detail)
        self.assertFalse(any("确认借款" in c for c in clicks), "复查命中还款时不得再点借款")
        self.assertTrue(any("今日签到还款" in c for c in clicks), "应直接点还款")

    def test_pending_loan_waits_full_mount_window_before_borrowing(self):
        """待还未结清时「确认借款」只是下周期预告:点借款前按完整挂载窗口等还款.

        短宽限只够覆盖「晚一拍」;页面已有未结清金额时,还款按钮可能挂载更久,
        必须用 RELAYFOR_REPAY_READY_WAIT_S 等满,否则仍会抢跑借款。
        """
        m = self.m
        with mock.patch.object(m, "RELAYFOR_REPAY_POLL_S", 0.01), \
             mock.patch.object(m, "RELAYFOR_REPAY_READY_WAIT_S", 0.1):
            res, clicks = self._run_handler(
                {
                    'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
                    'button:has-text("今日签到还款")': {
                        "is_visible": True, "disabled": True, "enable_after_polls": 8,
                    },
                },
                page_text='当前待还\n$1.00\n今日签到还款',
                page_text_after='当前待还\n$0.72\n今日已签到',
                repay_grace_s=0.02,
            )
        self.assertEqual(res.status, "OK", res.detail)
        self.assertFalse(any("确认借款" in c for c in clicks), "待还未清时不得抢跑借款")
        self.assertTrue(any("今日签到还款" in c for c in clicks))

    def test_borrow_then_repay_click_error_is_fail_no_click(self):
        """借出成功、补点还款时点击抛异常 → FAIL:no_click,detail 指明借款后还款失败."""
        m = self.m
        fake_locators_by = {
            'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
            'button:has-text("今日签到还款")': {
                "is_visible": True, "disabled": True, "enable_after_click": True,
            },
        }
        res, clicks = self._run_handler(
            fake_locators_by,
            page_text='已还清\n可再借',
            page_text_after='当前待还 $1.00\n今日签到还款',
            click_error=RuntimeError("boom"),
            click_error_after=2,  # 第 1 次(借款)成功,第 2 次(还款)抛错
        )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_click")
        self.assertIn("借款后点击还款失败", res.detail)
        self.assertTrue(any("确认借款" in c for c in clicks))

    def test_borrow_then_repay_no_confirm_is_fail(self):
        """借出成功、点还款后待还金额不变 → FAIL:no_confirm,不得假报 OK."""
        m = self.m
        fake_locators_by = {
            'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
            'button:has-text("今日签到还款")': {
                "is_visible": True, "disabled": True, "enable_after_click": True,
            },
        }
        with mock.patch.object(m, "RELAYFOR_REPAY_CONFIRM_WAIT_S", 0.0):
            res, clicks = self._run_handler(
                fake_locators_by,
                page_text='已还清\n可再借',
                page_text_after=[
                    '当前待还 $1.00\n今日签到还款',  # 借款落账
                    '当前待还 $1.00\n今日签到还款',  # 还款点击无效,页面纹丝不动
                ],
            )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_confirm")
        self.assertIn("今日还款未落账", res.detail)
        self.assertTrue(any("今日签到还款" in c for c in clicks))

    def test_borrow_without_repay_button_fails_instead_of_false_ok(self):
        """借出成功但还款按钮始终不出现 → FAIL(不再假报 OK)."""
        m = self.m
        fake_locators_by = {
            'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
            'button:has-text("今日签到还款")': {"is_visible": False},
        }
        with mock.patch.object(m, "RELAYFOR_REPAY_READY_WAIT_S", 0.0):
            res, clicks = self._run_handler(
                fake_locators_by,
                page_text='已还清\n可再借',
                page_text_after='当前待还 $1.00',
            )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_button")
        self.assertTrue(any("确认借款" in c for c in clicks))

    def test_repay_day_clicks_repay_when_borrow_unavailable(self):
        """确认借款不可用、今日签到还款可用 → 点还款,落账为已还清 → OK."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": False},
            },
            page_text='当前待还 $1.00\n今日签到还款',
            page_text_after='已还清\n还款成功',
        )
        self.assertEqual(res.status, "OK")
        self.assertIn("还款", res.detail)
        self.assertTrue(any("今日签到还款" in c for c in clicks), "今日应点击 今日签到还款")

    def test_repay_static_huanqing_copy_never_confirms(self):
        """2026-09-19 事故回归:页面静态说明「…提前还清…」常驻含「还清」,点击被吞/无响应
        时 after 与 before 相同,绝不能因该静态文案假确认 OK(实际待还未变)."""
        before = '公益福利 · 词元贷\n每次签到额度至少 $2.88；每周期有 50% 概率提前还清，第 4 次必定结清\n当前待还\n$0.39\n今日可签到\n今日签到还款'
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": False},
            },
            page_text=before,
            page_text_after=before,  # 点击无效,页面纹丝不动
        )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_confirm")
        self.assertTrue(any("今日签到还款" in c for c in clicks), "仍应尝试点击 今日签到还款")

    def test_repay_confirmed_by_pending_amount_decrease(self):
        """还款落账真判据:待还金额数值下降 → OK."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": False},
            },
            page_text='每周期有 50% 概率提前还清\n当前待还\n$0.39\n今日签到还款',
            page_text_after='每周期有 50% 概率提前还清\n当前待还\n$0.00\n已还清\n本笔签到记录\n2026-09-19 还款 $0.39',
        )
        self.assertEqual(res.status, "OK")
        self.assertIn("还款", res.detail)

    def test_today_done_gate_blocks_borrow_click(self):
        """2026-09-19 二次实证:当日还款完成后站点仍放出「确认借款」按钮,
        完结态(「今日已签到」disabled)在位时必须 ALREADY,绝不点借款."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": True},
                'button:has-text("今日已签到")': {"is_visible": True, "disabled": True},
            },
            page_text='当前待还\n$0.07\n签到进度\n3 / 4 天\n今日已签到',
        )
        self.assertEqual(res.status, "ALREADY")
        self.assertFalse(clicks, "完结态在位时不应点击任何按钮")

    def test_borrow_confirm_requires_amount_delta(self):
        """借款确认以金额为准:点击被吞 + 文本抖动绝不确认(2026-09-19 二次实证)."""
        f = self.m._relayfor_confirm_after
        before = '当前待还\n$0.07\n今日已签到'
        # 点击无效,文本轻微抖动/局部重渲染 → 不得确认
        self.assertFalse(f("借款", before, before + " "))
        self.assertFalse(f("借款", before, '当前待还\n$0.07\n加载中…\n今日已签到'))
        # 待还金额增大 → 确认
        self.assertTrue(f("借款", '当前待还\n$0.07', '当前待还\n$1.07\n今日签到还款'))
        # 借款日新出现待还卡 → 确认
        self.assertTrue(f("借款", '已还清', '当前待还\n$1.00\n今日签到还款'))

    def test_done_gate_walks_button_instances(self):
        """完结态判定必须逐实例检查(SPA 多视图同名按钮),不能只看 .first."""
        src = inspect.getsource(self.m._relayfor_today_done)
        self.assertIn("nth(i)", src)

    def test_pending_amount_parses_comma_and_newline(self):
        """金额解析:换行分隔与千分位逗号."""
        self.assertEqual(self.m._relayfor_pending_amount("当前待还\n$0.39"), 0.39)
        self.assertEqual(self.m._relayfor_pending_amount("待还 $1,234.56"), 1234.56)
        self.assertIsNone(self.m._relayfor_pending_amount("无待还字样 $5.00"))
        self.assertIsNone(self.m._relayfor_pending_amount(""))

    def test_repay_cleared_card_requires_settlement_cue(self):
        """待还卡片消失时必须有结清类文案佐证;无佐证(DOM 重排)不得确认."""
        before = '当前待还\n$0.39\n今日签到还款'
        # 有「已还清」佐证 → 确认
        res, _ = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": False},
            },
            page_text=before,
            page_text_after='已还清\n本笔签到记录',
        )
        self.assertEqual(res.status, "OK")
        # 无任何结清佐证(after 与 before 同窗截断丢掉待还卡)→ no_confirm
        res2, _ = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": False},
            },
            page_text=before,
            page_text_after='进行中的计费活动\n当前没有进行中的计费活动。',
        )
        self.assertEqual(res2.status, "FAIL")
        self.assertEqual(res2.reason, "no_confirm")

    def test_click_no_landed_confirm_returns_fail(self):
        """点击借/还后页面文案无变化(未落账)→ FAIL no_confirm,不是 OK 假阳性."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
                'button:has-text("今日签到还款")': {"is_visible": True, "disabled": True},
            },
            page_text='已还清',  # after 未提供 → 点击后文案不变
        )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_confirm")
        self.assertTrue(any("确认借款" in c for c in clicks), "仍应尝试点击 确认借款")

    def test_no_click_thrown_exception_is_fail_not_misclassified(self):
        """点击抛异常 → FAIL no_click,不可被误判为 ALREADY/OK."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": True, "disabled": False},
            },
            page_text='已还清',
            page_text_after='已还清',
            click_error=RuntimeError("boom"),
        )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_click")

    def test_already_processed_both_buttons_unavailable(self):
        """两按钮均不可见且已还清 → ALREADY 已处理态,不点击."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": False},
            },
            page_text='已还清',
        )
        self.assertEqual(res.status, "ALREADY")
        self.assertFalse(clicks, "已处理态不应点击任何按钮")

    def test_campaign_claim_executes_and_returns_ok(self):
        """中秋等限时签到活动「立即签到」按钮在位时优先执行活动签到."""
        res, clicks = self._run_handler(
            {
                'button[data-checkin-claim]': {"is_visible": True, "disabled": False},
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": False},
            },
            page_text='RelayFor Checkin 中秋签到活动\n已还清',
        )
        self.assertEqual(res.status, "OK")
        self.assertIn("活动签到成功", res.detail)
        self.assertTrue(any("data-checkin-claim" in c for c in clicks), "应点击活动签到按钮")

    def test_no_button_and_no_done_returns_fail_no_button(self):
        """页面未找到借/还/活动按钮且无已还清文案 → FAIL:no_button,杜绝静默假阳性 ALREADY."""
        res, clicks = self._run_handler(
            {
                'button:has-text("确认借款")': {"is_visible": False},
                'button:has-text("今日签到还款")': {"is_visible": False},
                'button[data-checkin-claim]': {"is_visible": False},
            },
            page_text='公益福利 · 词元贷\n加载中...',
        )
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_button")
        self.assertFalse(clicks)

    def test_scoped_done_indicators_exclude_generic_yici(self):
        """RELAYFOR_DONE_INDICATORS 仅限借贷结清相关文案,禁止包含通用已签到文案防活动卡片误判."""
        indicators = self.m.RELAYFOR_DONE_INDICATORS
        self.assertIn("已还清", indicators)
        self.assertNotIn("已签到", indicators)
        self.assertNotIn("签到成功", indicators)

    def test_unified_benefit_selectors_registered(self):
        """RELAYFOR_CAMPAIGN_SELECTORS 覆盖统一福利、套餐福利与助力按钮.

        且活动状态探测 JS 需完整嵌入(带引号的)各 claim 选择器: 缺失任一或丢掉
        引号都会让活动签到按钮探测失效, 导致限时活动漏签。
        """
        selectors = self.m.RELAYFOR_CAMPAIGN_SELECTORS
        self.assertIn("button[data-claim-unified-benefit]", selectors)
        self.assertIn("button[data-claim-benefit]", selectors)
        self.assertIn("button[data-token-loan-loss-pool-cut]", selectors)
        js = self.m._RELAYFOR_CAMPAIGN_STATUS_JS
        # 选择器须作为带引号的 CSS 字符串嵌入(丢引号会让 querySelectorAll 收到非法实参)
        self.assertIn("querySelectorAll('", js, "活动探测 JS 的选择器列表未加引号")
        for s in self.m.RELAYFOR_CLAIM_ATTR_SELECTORS:
            self.assertIn(f"{s}:not([disabled])", js, f"活动探测 JS 未嵌入 {s}")

    def test_token_loan_card_settled_rule_matrix(self):
        """_relayfor_card_settled 真值表: 仅「卡片存在 + 可见 + 明确已还清」才算结清.

        回归目标(2026-10-15 评审 P1): 早期实现把「卡片缺失/隐藏」也当结清,导致真实
        可借/可还站点被假阳性判成 ALREADY。本测试对该判定逐例断言,隐藏/缺失必 False。
        """
        f = self.m._relayfor_card_settled
        # 缺失/异常输入 → 未结清
        self.assertFalse(f(None))
        self.assertFalse(f({}))
        self.assertFalse(f(False))
        self.assertFalse(f({"present": False, "hidden": False, "remaining": "已还清"}))
        # 卡片存在但 hidden(功能下线/隐藏视图),即便残留「已还清」文案也绝不结清
        self.assertFalse(f({"present": True, "hidden": True, "remaining": "已还清"}))
        self.assertFalse(f({"present": True, "hidden": True, "remaining": ""}))
        # 卡片可见但未到「已还清」→ 未结清
        self.assertFalse(f({"present": True, "hidden": False, "remaining": "待还 $1.00"}))
        self.assertFalse(f({"present": True, "hidden": False, "remaining": ""}))
        # 唯一结清态: 可见 + 明确「已还清」
        self.assertTrue(f({"present": True, "hidden": False, "remaining": "已还清"}))

    def test_token_loan_hidden_card_end_to_end_not_already(self):
        """端到端: 词元贷卡片隐藏(功能下线)且无借/还/活动按钮 → FAIL(no_button),绝不假 ALREADY.

        判别力: 若把「hidden 卡片」误判为已结清,本用例会落在 ALREADY 分支 → 断言失败。
        """
        class FakePage:
            async def goto(self, *a, **k):
                return ""
            async def evaluate(self, expr):
                if "status_text" in expr:  # _relayfor_done_state 的卡片信息探测
                    return {"hidden": True, "btn_disabled": False, "btn_text": "",
                            "status_text": "", "remaining_text": ""}
                if "present" in expr and "hidden" in expr:  # 第 9 步结清探测
                    return {"present": True, "hidden": True, "remaining": ""}
                return None
            def locator(self, sel):
                class FakeLoc:
                    async def count(self):
                        return 0
                    @property
                    def first(self):
                        return self
                    async def is_visible(self, **k):
                        return False
                    async def is_disabled(self):
                        return False
                return FakeLoc()

        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_dis(*a, **k):
            return None
        async def fake_text(*a, **k):
            return ""
        async def fake_sleep(*a, **k):
            return None

        class FakeTime:
            """单调时钟快进: 首轮即越过 deadline, 免去 25s 真实轮询等待."""
            def __init__(self):
                self.t = 0.0
            def monotonic(self):
                self.t += 100.0
                return self.t

        page = FakePage()
        with mock.patch.object(self.m, "wait_text_ready", fake_wait), \
             mock.patch.object(self.m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(self.m, "dismiss_obstructing_dialogs", fake_dis), \
             mock.patch.object(self.m, "page_text", fake_text), \
             mock.patch.object(self.m, "time", FakeTime()), \
             mock.patch("asyncio.sleep", fake_sleep):
            res = self.asyncio.run(self.m.relayfor_checkin(page, self._adapter()))
        self.assertNotEqual(res.status, "ALREADY", "隐藏卡片绝不能误判为已处理")
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_button")

    def test_relayfor_balance_extraction_dom(self):
        """_extract_relayfor_balance 成功提取 USD 余额与合理奖励文案."""
        class FakeBalancePage:
            async def evaluate(self, expr):
                return {
                    "raw": "$279.90",
                    "reward": "+$0.10",
                }
        bal = self.asyncio.run(self.m._extract_relayfor_balance(FakeBalancePage()))
        self.assertIsNotNone(bal)
        self.assertEqual(bal.amount, 279.90)
        self.assertEqual(bal.currency, "USD")
        self.assertEqual(bal.reward_text, "+$0.10")
        self.assertEqual(bal.source, "dom")

    def test_relayfor_balance_drops_impossible_reward(self):
        """奖励大于余额必然解析误取 → 丢弃 reward_text(避免污染 account_balances)."""
        class FakeBalancePage:
            async def evaluate(self, expr):
                return {
                    "raw": "$279.90",
                    "reward": "+$280.00",
                }
        bal = self.asyncio.run(self.m._extract_relayfor_balance(FakeBalancePage()))
        self.assertIsNotNone(bal)
        self.assertEqual(bal.amount, 279.90)
        self.assertIsNone(bal.reward_text, "奖励>余额应被丢弃")


class TestRelayForGoCheckin(unittest.TestCase):
    """relayfor-go (RelayFor Go 会员每日签到) 独立任务测试套件."""

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def _adapter(self):
        return self.m.SiteAdapter(
            name="relayfor-go",
            url="https://relayfor.xyz/console/#benefits",
            kind="relayfor_go",
            sign_selectors=['button:has-text("立即签到")'],
            already_selectors=["text=已签到", "text=今日已签到"],
            ready_rounds=10,
        )

    def test_is_relayfor_go_name(self):
        f = self.m.is_relayfor_go_name
        self.assertTrue(f("relayfor-go"))
        self.assertTrue(f("relayfor_go"))
        self.assertTrue(f("RelayFor Go · 签到"))
        self.assertTrue(f("relayfor-会员"))
        self.assertFalse(f("relayfor"))
        self.assertFalse(f("newapi"))
        self.assertFalse(f(""))

    def test_yaml_registers_relayfor_go_entry(self):
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "relayfor-go"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 relayfor-go 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "relayfor_go")
        self.assertEqual(a.url, "https://relayfor.xyz/console/#benefits")

    def test_dispatch_contains_relayfor_go_branch(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("is_relayfor_go_name", src)
        self.assertIn("return await relayfor_go_checkin(page, adapter, browser=browser)", src)

    def test_p0_no_clear_cookies_in_relayfor_go_path(self):
        import inspect
        src = inspect.getsource(self.m.relayfor_go_checkin)
        lower = src.lower()
        self.assertNotIn("clear_cookies", lower)
        self.assertNotIn("clear_localstorage", lower)
        self.assertNotIn("clear_all", lower)

    def test_relayfor_go_already_done(self):
        m = self.m

        class FakePage:
            async def goto(self, url, **kw):
                return ""
            async def evaluate(self, expr):
                if "classList.contains('active')" in expr:
                    return True
                if "aria-busy" in expr:
                    return "false"
                if "checkin-user-card" in expr:
                    return {
                        "found": True,
                        "hasBtn": False,
                        "claimAttr": "",
                        "isDone": True,
                        "tagText": "已签到",
                    }
                return ""

        async def fake_wait(*a, **k):
            return None
        async def fake_cf(*a, **k):
            return False
        async def fake_dis(*a, **k):
            return None

        page = FakePage()
        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dis):
            res = self.asyncio.run(m.relayfor_go_checkin(page, self._adapter()))

        self.assertEqual(res.status, "ALREADY")
        self.assertIn("今日已签到", res.detail)

    def test_relayfor_go_claim_success(self):
        m = self.m
        clicks = []
        eval_call_count = [0]

        class FakeLocator:
            def __init__(self, sel):
                self.sel = sel
            @property
            def first(self):
                return self
            def filter(self, **kw):
                return self
            def locator(self, sel):
                return FakeLocator(sel)
            async def is_visible(self, timeout=800):
                return True
            async def click(self, **kw):
                clicks.append(self.sel)

        class FakePage:
            async def goto(self, url, **kw):
                return ""
            async def evaluate(self, expr):
                if "classList.contains('active')" in expr:
                    return True
                if "aria-busy" in expr:
                    return "false"
                if "checkin-user-card" in expr:
                    eval_call_count[0] += 1
                    if eval_call_count[0] == 1:
                        return {
                            "found": True,
                            "hasBtn": True,
                            "claimAttr": "3|2026-09-26",
                            "isDone": False,
                            "tagText": "",
                        }
                    elif eval_call_count[0] == 2:
                        return {
                            "claimAttr": "3|2026-09-26",
                            "text": "立即签到",
                        }
                    else:
                        return None
                return ""
            def locator(self, sel, **kw):
                return FakeLocator(sel)

        async def fake_wait(*a, **k):
            return None
        async def fake_cf(*a, **k):
            return False
        async def fake_dis(*a, **k):
            return None

        page = FakePage()
        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dis):
            res = self.asyncio.run(m.relayfor_go_checkin(page, self._adapter()))

        self.assertEqual(res.status, "OK")
        self.assertIn("签到成功", res.detail)
        self.assertTrue(len(clicks) >= 1)

    def test_relayfor_go_card_not_found_returns_fail_no_button(self):
        m = self.m

        class FakePage:
            async def goto(self, url, **kw):
                return ""
            async def evaluate(self, expr):
                if "classList.contains('active')" in expr:
                    return True
                if "aria-busy" in expr:
                    return "false"
                if "checkin-user-card" in expr:
                    return {"found": False}
                if "form.login-form" in expr:
                    return False
                return ""

        async def fake_wait(*a, **k):
            return None
        async def fake_cf(*a, **k):
            return False
        async def fake_dis(*a, **k):
            return None

        page = FakePage()
        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "dismiss_obstructing_dialogs", fake_dis):
            res = self.asyncio.run(m.relayfor_go_checkin(page, self._adapter()))

        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_button")


class TestFengwindWindowGuard(unittest.TestCase):
    """fengwind 服务端权威窗口守卫（业务根因修复,2026-09-07）。

    需求根因:2026-09-06 run177 在北京 06:03 跑 fengwind,页面残留上一周期
    「已签到」disabled,通用 already 判据误判当日 ALREADY 并把 daily_tasks
    标 done,使 08:10 cron 跳过真实可签时段(用户只得手动领取)。

    根因修复(仅影响本站,其它站点路径完全不动):不猜本地时间窗口,而是读取
    服务端 /api/checkin/status 的 next_reset_at(该周期按 UTC 0 点=北京 08:00
    切新 biz_date)。now(UTC) < next_reset_at → 今日新周期未开,页面残留不可作
    当日 ALREADY 判据,守卫返回 keep_pending(daily_tasks 保持 pending)让周期
    开启后批次重跑;now(UTC) >= next_reset_at → 新周期已开,放行交给通用
    已签/签到判定。status 缺失/不可解析 → 保守放行(None),绝不整站永久 pending。
    """

    def setUp(self):
        self.m = load_mod()

    def test_is_fengwind_site(self):
        m = self.m
        self.assertTrue(m.is_fengwind_site("https://api-welfalre.fengwind.com/"))
        self.assertTrue(m.is_fengwind_site("https://api-welfalre.fengwind.com/wallet"))
        self.assertTrue(m.is_fengwind_site("https://api.fengwind.com"))
        self.assertFalse(m.is_fengwind_site("https://demo.dev2.mulink.top/wallet"))
        self.assertFalse(m.is_fengwind_site("https://example.com/"))

    def test_guard_returns_pending_before_reset(self):
        """now(UTC) 早于 next_reset_at → 新周期未开,返回 keep_pending 不判 ALREADY。"""
        m = self.m
        from datetime import datetime, timezone

        # 现探实测服务端: next_reset_at="2026-09-07T00:00:00Z"
        status = {"next_reset_at": "2026-09-07T00:00:00Z", "checked_in_today": True}
        now = datetime(2026, 9, 6, 19, 0, 0, tzinfo=timezone.utc)  # 北京 09-07 03:00
        res = m.fengwind_status_guard(status, now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "window_not_open")
        self.assertTrue(res.keep_pending)
        self.assertNotEqual(res.status, "ALREADY")

    def test_guard_passes_through_after_reset(self):
        """now(UTC) >= next_reset_at → 新周期已开,放行 None,不拦截。"""
        m = self.m
        from datetime import datetime, timezone

        status = {"next_reset_at": "2026-09-07T00:00:00Z"}
        at_reset = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)  # 北京 09-07 08:00
        after_reset = datetime(2026, 9, 7, 2, 30, 0, tzinfo=timezone.utc)
        self.assertIsNone(m.fengwind_status_guard(status, now=at_reset))
        self.assertIsNone(m.fengwind_status_guard(status, now=after_reset))

    def test_guard_passes_when_can_checkin_and_next_reset_tomorrow(self):
        """生产实测场景(2026-09-10 08:15 CST):新周期已开,服务端 next_reset_at 已推移到明天,can_check_in=True 必须放行不被误拦。"""
        m = self.m
        from datetime import datetime, timezone

        status = {
            "enabled": True,
            "biz_date": "2026-09-10",
            "next_reset_at": "2026-09-11T00:00:00Z",
            "checked_in_today": False,
            "can_check_in": True,
        }
        now = datetime(2026, 9, 10, 0, 15, 0, tzinfo=timezone.utc)  # 北京 08:15
        self.assertIsNone(m.fengwind_status_guard(status, now=now))

    def test_guard_returns_pending_when_biz_date_behind(self):
        """北京 00:00~08:00 间服务端 biz_date 仍为昨日,不可判 ALREADY,必须返回 keep_pending。"""
        m = self.m
        from datetime import datetime, timezone

        status = {
            "enabled": True,
            "biz_date": "2026-09-09",
            "next_reset_at": "2026-09-10T00:00:00Z",
            "checked_in_today": True,
            "can_check_in": False,
        }
        now = datetime(2026, 9, 9, 22, 0, 0, tzinfo=timezone.utc)  # 北京 09-10 06:00
        res = m.fengwind_status_guard(status, now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "window_not_open")
        self.assertTrue(res.keep_pending)

    def test_guard_passthrough_when_status_missing(self):
        """status 缺失/next_reset_at 缺失/不可解析 → 保守放行 None,绝不永久 pending。"""
        m = self.m
        from datetime import datetime, timezone

        now = datetime(2026, 9, 6, 19, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(m.fengwind_status_guard(None, now=now))
        self.assertIsNone(m.fengwind_status_guard({}, now=now))
        self.assertIsNone(m.fengwind_status_guard({"next_reset_at": "garbage"}, now=now))

    def test_source_wires_fengwind_guard_before_generic(self):
        """源码守卫:fengwind 专属分支在通用流程前接入,且 fetch/guard 只在本站分支内。"""
        src = TARGET.read_text(encoding="utf-8")
        guard_call = re.search(
            r"if is_fengwind_site\(site_url\):.*?_fengwind_fetch_status\(page, site_url\)",
            src,
            re.S,
        )
        self.assertIsNotNone(guard_call, "fengwind 守卫必须在 legacy_checkin_on_page 接入")
        self.assertIn("keep_pending", src)
        self.assertIn("next_reset_at", src)
        self.assertIn("FENGWIND_STATUS_PATH", src)

    def test_pending_write_back_uses_only_if_not_done(self):
        """keep_pending 写回必须 only_if_not_done,绝不把已 done 的账本覆盖回 pending。"""
        src = TARGET.read_text(encoding="utf-8")
        line = 'set_task_result(today_str, name, "pending", only_if_not_done=True)'
        self.assertIn(line, src)
        self.assertIn("result.keep_pending", src)


class TestFengwindLoginAndCheckin(unittest.TestCase):
    """Fengwind API 福利站先登录后签到保障与增量奖励提取回归测试 (2026-10-18)。"""

    def setUp(self):
        self.m = load_mod()

    def test_reward_extraction_fengwind_text(self):
        """验证 _extract_reward_from_text 能正确提取「本次获得 $5.17」等增量。"""
        m = self.m
        self.assertEqual(m._extract_reward_from_text("本次获得\n\n$5.17"), "+$5.17")
        self.assertEqual(m._extract_reward_from_text("本次获得 $3.40"), "+$3.40")
        self.assertEqual(m._extract_reward_from_text("本次奖励 5.00 刀"), "+5.00 刀")
        self.assertEqual(m._extract_reward_from_text("签到成功，获得 $3.40，2天有效，正在入账。"), "+$3.40")

    def test_builtin_adapter_fengwind_selectors(self):
        """验证内置 fengwind 适配器 signs 与 already 包含紧缩选择器，排除 tabs-trigger。"""
        m = self.m
        adapters = {a.name: a for a in m._BUILTIN_SITE_ADAPTERS}
        self.assertIn("fengwind", adapters)
        fw = adapters["fengwind"]
        # signs 必须包含严格限定 data-slot="button" 的精确选择器
        self.assertIn('button[data-slot="button"]:text-is("签到")', fw.sign_selectors)
        self.assertIn('button[data-slot="button"]:has-text("签到"):not(:has-text("登录"))', fw.sign_selectors)
        # already 必须包含今日已签到与已签到按钮
        self.assertIn('text=今日已签到', fw.already_selectors)
        self.assertIn('button[data-slot="button"]:text-is("已签到")', fw.already_selectors)

    def test_ensure_fengwind_logged_in_fast_path(self):
        """若 localStorage 已有有效 token 且无「登录后签到」按钮，快速放行 True。"""
        m = self.m
        import asyncio

        class FakeLocator:
            async def count(self):
                return 0
            @property
            def first(self):
                return self
            async def is_visible(self):
                return False

        class FakePage:
            url = "https://api-welfalre.fengwind.com/"
            async def evaluate(self, expr):
                if "welfare_token" in expr:
                    return "mock_valid_jwt_token_1234567890"
                return None
            def locator(self, selector):
                return FakeLocator()

        result = asyncio.run(m._ensure_fengwind_logged_in(FakePage(), "https://api-welfalre.fengwind.com/"))
        self.assertTrue(result)

    def test_extract_fengwind_balance(self):
        """验证 _extract_fengwind_balance 正确从 /api/checkin/status 提取当日奖励。"""
        m = self.m
        import asyncio

        class FakePage:
            async def evaluate(self, expr):
                return {"rewardText": "+$5.17"}

        res = asyncio.run(m._extract_fengwind_balance(FakePage()))
        self.assertIsNotNone(res)
        self.assertEqual(res.reward_text, "+$5.17")
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.source, "api")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from unittest import mock
import asyncio
import inspect
import json
import os
import re
import pytest
import unittest
import yaml

from tests._support import (
    FakePage,
    ROOT,
    TARGET,
    load_mod,
)

pytestmark = [pytest.mark.runner]


class TestMulinkClaimSelector(unittest.TestCase):
    """Regression: mulink 领取按钮必须精确定位,不能误点卡片折叠头 (2026-09-07)。

    根因:mulink /wallet iframe 额度池卡片头 <button> 文本为
    「额度池 / 本周期还可领取 1 次」,也命中 has-text("领取")(「可领取」子串),
    且 DOM 顺序排在真领取按钮之前;原 `button:has-text("领取")` + `.first`
    点错到卡片折叠头 → 不触发签到 → no_confirm。

    修复:改用精确文本 `button:text-is("领取")` 优先(实测只命中唯一真领取按钮),
    并给模糊命中加折叠头排除(排除含「本周期/额度池」的卡片头按钮)。
    """

    def test_mulink_flow_uses_exact_text_claim_selector(self):
        """mulink_checkin 必须用 text-is(精确文本)而非宽松 has-text 点「领取」。"""
        src = TARGET.read_text(encoding="utf-8")
        # 关键断言:领取按钮主选择器是精确文本 text-is("领取")。
        self.assertIn('button:text-is("领取")', src)
        # 回归兜底断言:仍保留 has-text 兜底,但必须排除卡片折叠头("本周期"/"已领取")。
        self.assertIn('button:has-text("领取"):not(:has-text("已领取"))', src)

    def test_has_text_claim_first_collision_is_guarded(self):
        """mulink 领取按钮 DOM 选择不再允许宽松 has-text 命中先吞卡片头。

        真实现象:has-text("领取") 命中两个按钮——卡片头按钮「额度池/本周期还可领取
        1 次」排在最前、真领取按钮次之。宽松 .first 会误点折叠头。这里断言 runner
        源码对 claim 筛选会跳过含「本周期/额度池」的卡片头按钮。
        """
        src = TARGET.read_text(encoding="utf-8")
        # 模糊命中时,若命中文本是卡片头(含"本周期"/"额度池")则不能被当作接收键。
        self.assertIn('"本周期" not in txt', src)
        self.assertIn('"额度池" not in txt', src)

    def test_mulink_accepts_signin_cta_and_dakao_done_cues(self):
        """2026-09-13 措辞改版回归:CTA「签到」精确命中;已领预判/确认门补「打卡」措辞。

        实测(9222 真机):额度池 CTA 文案从「领取」改为「签到」(enabled),点击成功后
        按钮消失、副标题变「今日已打卡」+ CTA 位变「今天已打卡」+ 日历当日翻 ✓;
        旧确认词「今日已领取/今天 +」均不再出现。旧「领取」选择器与确认词保留兼容。
        """
        src = TARGET.read_text(encoding="utf-8")
        # CTA 精确选择器:领取(旧措辞) + 签到/打卡(新措辞) 都必须在候选列表
        self.assertIn('button:text-is("签到")', src)
        self.assertIn('button:text-is("打卡")', src)
        # 精确文本命中白名单扩到「签到」「打卡」
        self.assertIn('"签到领取", "签到", "打卡"', src)
        # 已领态预判(点击前)与确认门(点击后)都要覆盖打卡措辞
        self.assertIn('"今日已打卡" in wtext', src)
        self.assertIn('"今天已打卡" in wtext', src)
        self.assertIn('"今日已打卡" in wtext2', src)
        self.assertIn('"今天已打卡" in wtext2', src)

    def test_mulink_supports_no_iframe_in_page_modal_and_next_url(self):
        """2026-10-08 前端 SPA 架构改版回归:
        支持 /next?app=wallet 直达与 No-IFrame 内嵌模态卡片 (顶级 document)。
        """
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn('next?app=wallet', src)
        self.assertIn('get_wallet_container', src)
        self.assertIn('"额度池" in ptext', src)


class TestDarkforgerCheckin(unittest.TestCase):
    """welfare.darkforger.com(Darkforger 炼金工坊/公益福利站)—— 2026-09-20 接入.

    React SPA (hash 路由 /#/checkin), LinuxDO OAuth 登录态(复用 9222 共享 profile).
    签到流程依赖前端双重人机校验: Cloudflare Turnstile + Web Worker PoW (SHA-256).
    P0 红线: 绝不调用 clear_cookies / clear_localStorage / clear_all (全清 9222 其它域).
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def _adapter(self):
        return self.m.SiteAdapter(
            name="darkforger",
            url="https://welfare.darkforger.com/#/checkin",
            kind="darkforger",
            sign_selectors=['button:has-text("验证并签到")', 'button:has-text("确认签到")'],
            already_selectors=['button:has-text("今日已签到")', 'text=今日已签到', 'text=今天的探索额度，已到账', 'text=签到成功'],
            ready_rounds=10,
        )

    def test_is_darkforger_site_positive_negative(self):
        f = self.m.is_darkforger_site
        self.assertTrue(f("https://welfare.darkforger.com/#/checkin"))
        self.assertTrue(f("https://welfare.darkforger.com/"))
        self.assertTrue(f("welfare.darkforger.com"))
        self.assertFalse(f("https://mzlone.top/"))
        self.assertFalse(f(""))
        self.assertFalse(f("https://example.com/"))

    def test_kind_in_literal(self):
        self.assertIn("darkforger", self.m.AdapterKind.__args__)

    def test_yaml_registers_darkforger_entry(self):
        """sites.yaml 的 darkforger 条目解析后 kind=darkforger 且包含相应签名。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "darkforger"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 darkforger 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "darkforger")
        self.assertTrue(a.url.startswith("https://welfare.darkforger.com"))
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("验证并签到", joined_signs)
        self.assertIn("确认签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)

    def test_dispatch_contains_darkforger_branch(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("is_darkforger_site(site_url)", src)
        self.assertIn("return await darkforger_checkin(page, adapter, browser=browser)", src)

    def test_p0_no_clear_cookies_in_darkforger_path(self):
        """darkforger 专属流程不得存在 clear_cookies / clear_localstorage / 全清调用."""
        import inspect
        src = inspect.getsource(self.m.darkforger_checkin)
        lower = src.lower()
        self.assertNotIn("clear_cookies", lower)
        self.assertNotIn("clear_localstorage", lower)
        self.assertNotIn("clear_all", lower)

    def test_darkforger_already_checked_in(self):
        """当页面或按钮显示今日已签到时，直接返回 ALREADY。"""
        m = self.m
        clicks = []

        class FakeLocator:
            def __init__(self, sel, visible=True):
                self.sel = sel
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def click(self, **kw):
                clicks.append(self.sel)

        class FakePage:
            url = "https://welfare.darkforger.com/#/checkin"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k): return ""
            def locator(self, sel):
                if "今日已签到" in sel:
                    return FakeLocator(sel, visible=True)
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            return "每日签到\n今日已签到\n今天的探索额度，已到账"

        orig_page_text = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.darkforger_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "ALREADY")
            self.assertIn("今日已签到", res.detail)
            self.assertEqual(len(clicks), 0)
        finally:
            m.page_text = orig_page_text

    def test_darkforger_successful_checkin(self):
        """正常流程：点击验证并签到 -> 弹窗 Turnstile/PoW 就绪 -> 点击确认签到 -> 成功确认。"""
        m = self.m
        clicks = []
        state = {"submitted": False}

        class FakeLocator:
            def __init__(self, sel, visible=True):
                self.sel = sel
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def wait_for(self, state="visible", timeout=6000):
                return True
            async def click(self, **kw):
                clicks.append(self.sel)
                if "确认签到" in self.sel or "verification-submit" in self.sel:
                    state["submitted"] = True

        class FakePage:
            url = "https://welfare.darkforger.com/#/checkin"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                if "verification-submit" in expr:
                    return {"found": True, "disabled": False, "text": "确认签到"}
                if "toast" in expr:
                    return "签到成功！已获得 5.00 额度" if state["submitted"] else ""
                return ""
            def locator(self, sel):
                if "使用 Linux.do" in sel:
                    return FakeLocator(sel, visible=False)
                if "今日已签到" in sel:
                    return FakeLocator(sel, visible=state["submitted"])
                if "验证并签到" in sel:
                    return FakeLocator(sel, visible=not state["submitted"])
                return FakeLocator(sel, visible=True)

        async def fake_page_text(p, limit=2000):
            if state["submitted"]:
                return "每日签到\n今日已签到\n签到成功"
            return "每日签到\n完成轻量验证，领取今天的公益额度。\n验证并签到"

        orig_page_text = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.darkforger_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "OK")
            self.assertIn("签到成功", res.detail)
            self.assertEqual(len(clicks), 2)
            self.assertTrue(any("验证并签到" in c for c in clicks))
            self.assertTrue(any("确认签到" in c or "verification-submit" in c for c in clicks))
        finally:
            m.page_text = orig_page_text


class TestPoolCheckin(unittest.TestCase):
    """pool.983698.xyz —— 2026-09-20 接入,2026-10-01 站点改版适配.

    React SPA,「我的信息」(subscriptions)是 in-app tab;2026-10-01 改版后每日签到
    从「商店」Tab 迁移到「我的信息」Tab(商店面板已变为「兑换邀请码」,绝不可点)。
    必须先点「我的信息」再点 CTA。
    P0 红线: 绝不调用 clear_cookies / clear_localStorage / clear_all (全清 9222 其它域).
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def _adapter(self):
        return self.m.SiteAdapter(
            name="pool",
            url="https://pool.983698.xyz/",
            kind="pool",
            sign_selectors=['button.primary:has-text("立即签到")', 'button:has-text("立即签到")'],
            already_selectors=['button.primary:has-text("今日已签到")', 'button:has-text("今日已签到")', 'text=今日已签到', 'text=签到成功'],
            ready_rounds=6,
        )

    def test_is_pool_site_positive_negative(self):
        f = self.m.is_pool_site
        self.assertTrue(f("https://pool.983698.xyz/"))
        self.assertTrue(f("https://pool.983698.xyz"))
        self.assertTrue(f("https://983698.xyz/"))
        self.assertFalse(f("https://mzlone.top/"))
        self.assertFalse(f(""))
        self.assertFalse(f("https://example.com/"))
        self.assertFalse(f("https://welfare.darkforger.com/"))

    def test_kind_in_literal(self):
        self.assertIn("pool", self.m.AdapterKind.__args__)

    def test_yaml_registers_pool_entry(self):
        """sites.yaml 的 pool 条目解析后 kind=pool 且包含商店签到签名。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "pool"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 pool 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "pool")
        self.assertTrue("pool.983698.xyz" in a.url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertTrue(a.prefer_catalog_url)

    def test_dispatch_contains_pool_branch(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("is_pool_site(site_url)", src)
        self.assertIn("return await pool_checkin(page, adapter, browser=browser)", src)

    def test_p0_no_clear_cookies_in_pool_path(self):
        """pool 专属流程不得存在 clear_cookies / clear_localstorage / 全清调用."""
        import inspect
        src = inspect.getsource(self.m.pool_checkin)
        lower = src.lower()
        self.assertNotIn("clear_cookies", lower)
        self.assertNotIn("clear_localstorage", lower)
        self.assertNotIn("clear_all", lower)

    def test_pool_already_checked_in_on_info_tab(self):
        """2026-10-01 改版:点「我的信息」后按钮显示今日已签到时，直接返回 ALREADY。"""
        m = self.m
        clicks = []

        class FakeLocator:
            def __init__(self, sel, visible=True):
                self.sel = sel
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def click(self, **kw):
                clicks.append(self.sel)

        class FakePage:
            url = "https://pool.983698.xyz/"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k): return ""
            def locator(self, sel):
                if "LinuxDo" in sel:
                    return FakeLocator(sel, visible=False)
                if "我的信息" in sel:
                    return FakeLocator(sel, visible=True)
                if "今日已签到" in sel:
                    return FakeLocator(sel, visible=True)
                if "立即签到" in sel:
                    return FakeLocator(sel, visible=False)
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            return "我的信息\n每日签到\n今日已签到"

        orig_page_text = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.pool_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "ALREADY")
            self.assertIn("今日已签到", res.detail)
            self.assertTrue(any("我的信息" in c for c in clicks))
            self.assertFalse(any("立即签到" in c for c in clicks))
            # 商店 Tab 的「兑换邀请码」绝不可被点击(消耗积分)
            self.assertFalse(any("兑换邀请码" in c for c in clicks))
        finally:
            m.page_text = orig_page_text

    def test_pool_successful_checkin_via_info_tab(self):
        """2026-10-01 改版正常流程：点我的信息 → 点立即签到 → toast 签到成功。"""
        m = self.m
        clicks = []
        state = {"clicked": False, "info": False}

        class FakeLocator:
            def __init__(self, sel, visible=True):
                self.sel = sel
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def click(self, **kw):
                clicks.append(self.sel)
                if "我的信息" in self.sel:
                    state["info"] = True
                if "立即签到" in self.sel:
                    state["clicked"] = True

        class FakePage:
            url = "https://pool.983698.xyz/"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                if "toast" in expr:
                    return "签到成功，获得 10 积分" if state["clicked"] else ""
                return ""
            def locator(self, sel):
                if "LinuxDo" in sel:
                    return FakeLocator(sel, visible=False)
                if "我的信息" in sel:
                    return FakeLocator(sel, visible=True)
                if "立即签到" in sel:
                    return FakeLocator(sel, visible=state["info"] and not state["clicked"])
                if "今日已签到" in sel:
                    return FakeLocator(sel, visible=state["clicked"])
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            if state["clicked"]:
                return "我的信息\n每日签到\n签到成功，获得 10 积分\n今日已签到"
            if state["info"]:
                return "我的信息\n每日签到\n立即签到"
            return "我的信息\n商店\n动漫\nmangoqwq"

        orig_page_text = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.pool_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "OK")
            self.assertIn("签到成功", res.detail)
            self.assertTrue(any("我的信息" in c for c in clicks))
            self.assertTrue(any("立即签到" in c for c in clicks))
            self.assertFalse(any("商店" in c for c in clicks))
            self.assertFalse(any("兑换邀请码" in c for c in clicks))
        finally:
            m.page_text = orig_page_text


class TestXmiaomCheckin(unittest.TestCase):
    """咕嘎咕嘎生图站 (ai.xmiaom.com) —— 2026-09-26 接入.

    React SPA, 签到路径位于 /dashboard/overview (原 /profile 废弃).
    主 CTA 为 button:has-text("立即签到"), 签到后变为 button:has-text("已签到") (disabled).
    2026-10-27 起站点只接受「当天登录」的会话: 过期会话点击被静默拒绝或页面
    根本不渲染 CTA。因此**每次签到前**强制本站 logout + LinuxDO SSO 重新登录。
    P0 红线: 绝不调用 clear_cookies / clear_localStorage / clear_all (全清 9222 其它域).
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio
        # 行为单测不跑真实 OAuth, 也不等真实轮询: 强制重登录路径打桩.
        async def _fake_terms(page):
            return 0
        async def _fake_sso(page, origin_host="", browser=None):
            return "OK"
        async def _fake_ready(page, min_len=40, rounds=15):
            return True
        async def _fake_wait_any(page, selectors, total_s, **kw):
            # 卡片水合等待在单测里不真等; 后续步骤仍走真实扫描逻辑.
            return None, None
        # 桩必须随用例结束还原：直接赋值会残留在共享 runner 模块对象上，
        # xdist 同 worker 后续缓存加载的用例会吃到假选择器/假延迟
        # （同 2026-11-07 nailao 桩泄漏根因）。
        patchers = [
            mock.patch.object(self.m, "check_terms", _fake_terms),
            mock.patch.object(self.m, "try_linuxdo_sso", _fake_sso),
            mock.patch.object(self.m, "wait_text_ready", _fake_ready),
            mock.patch.object(self.m, "wait_for_any_visible", _fake_wait_any),
            mock.patch.object(self.m, "XMIAOM_SSO_RETRY_DELAY_S", 0),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _adapter(self):
        return self.m.SiteAdapter(
            name="咕嘎咕嘎生图站",
            url="https://ai.xmiaom.com/dashboard/overview",
            kind="xmiaom",
            sign_selectors=['button:has-text("立即签到")'],
            already_selectors=[
                'button:has-text("已签到")',
                'text=今日已签到',
                'text="今日已签到"',
                'text=已签到',
                'text="已签到"',
                'text=签到成功',
            ],
            prefer_catalog_url=True,
            login_url="https://ai.xmiaom.com/sign-in",
            ready_rounds=6,
        )

    def test_is_xmiaom_site_positive_negative(self):
        f = self.m.is_xmiaom_site
        self.assertTrue(f("https://ai.xmiaom.com/dashboard/overview"))
        self.assertTrue(f("https://ai.xmiaom.com/profile"))
        self.assertTrue(f("https://xmiaom.com"))
        self.assertFalse(f("https://example.com/"))
        self.assertFalse(f(""))

    def test_is_xmiaom_name_positive_negative(self):
        f = self.m.is_xmiaom_name
        self.assertTrue(f("咕嘎咕嘎生图站"))
        self.assertTrue(f("咕嘎"))
        self.assertTrue(f("xmiaom"))
        self.assertTrue(f("xmiaom_ai"))
        self.assertFalse(f("other_site"))

    def test_kind_in_literal(self):
        self.assertIn("xmiaom", self.m.AdapterKind.__args__)

    def test_yaml_registers_xmiaom_entry(self):
        """sites.yaml 的 咕嘎咕嘎生图站 条目解析为 kind=xmiaom 且主路径为 /dashboard/overview。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "咕嘎咕嘎生图站"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 咕嘎咕嘎生图站 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "xmiaom")
        self.assertEqual(a.url, "https://ai.xmiaom.com/dashboard/overview")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.login_url, "https://ai.xmiaom.com/sign-in")
        # 每次签到强制 logout+SSO, 重试一次需要超过批次默认 60s 的预算
        self.assertEqual(a.site_timeout_s, 150.0)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("已签到", joined_already)

    def test_resolve_site_keeps_site_timeout_override(self):
        """任务行带 URL 时 resolve_site 会重建 adapter: 新字段不能丢。"""
        a = self.m.resolve_site("咕嘎咕嘎生图站", "https://ai.xmiaom.com/dashboard/overview")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "xmiaom")
        self.assertEqual(a.site_timeout_s, 150.0,
                         "带 note URL 重建 adapter 时必须保留站点级 wall clock")
        b = self.m.resolve_site("咕嘎咕嘎生图站", "")
        self.assertEqual(b.site_timeout_s, 150.0)

    def test_builtin_entry_xmiaom(self):
        """_BUILTIN_SITE_ADAPTERS 中的 咕嘎咕嘎生图站 为 xmiaom adapter。"""
        from stealth_checkin_runner import _BUILTIN_SITE_ADAPTERS
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "咕嘎咕嘎生图站"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 咕嘎咕嘎生图站")
        self.assertEqual(builtin_entry.kind, "xmiaom")
        self.assertEqual(builtin_entry.url, "https://ai.xmiaom.com/dashboard/overview")
        self.assertTrue(builtin_entry.prefer_catalog_url)
        self.assertEqual(builtin_entry.login_url, "https://ai.xmiaom.com/sign-in")
        self.assertEqual(builtin_entry.site_timeout_s, 150.0,
                         "内置回退也要带足两轮 SSO 的预算")

    def test_dispatch_contains_xmiaom_branch(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("is_xmiaom_site(site_url)", src)
        self.assertIn("return await xmiaom_checkin(page, adapter, browser=browser)", src)

    def test_p0_no_clear_cookies_in_xmiaom_path(self):
        """xmiaom 专属流程不得调用 clear_cookies / clear_localstorage / 全清接口.

        同时禁掉具体清库 API: 单禁 "localstorage" 会误伤中文说明里的域名描述,
        但要真的清 localStorage 必须落到这三个调用上。
        """
        import inspect
        src = inspect.getsource(self.m.xmiaom_checkin)
        lower = src.lower()
        self.assertNotIn("clear_cookies", lower)
        self.assertNotIn("clear_localstorage", lower)
        self.assertNotIn("clear_all", lower)
        self.assertNotIn("localstorage.clear", lower)
        self.assertNotIn("localstorage.removeitem", lower)
        self.assertNotIn("document.cookie", lower)
        self.assertNotIn("indexeddb", lower)
        self.assertNotIn("deleteallcookies", lower)

    def test_xmiaom_forces_fresh_login_before_cta(self):
        """每次签到前必须先本站 logout + 重新 SSO, 再扫描/点击 CTA。"""
        m = self.m
        order = []
        state = {"checked": False}

        async def fake_terms(page):
            order.append("terms")
            return 0

        async def fake_sso(page, origin_host="", browser=None):
            order.append("sso")
            return "OK"

        m.check_terms = fake_terms
        m.try_linuxdo_sso = fake_sso

        class FakeLocator:
            def __init__(self, sel, visible=True, text=""):
                self.sel = sel
                self._visible = visible
                self._text = text
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def inner_text(self):
                return self._text
            async def click(self, **kw):
                order.append("click:" + self.sel)
                if "立即签到" in self.sel:
                    state["checked"] = True

        class FakePage:
            url = "https://ai.xmiaom.com/dashboard/overview"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                if "/api/user/logout" in expr:
                    order.append("logout")
                    return True
                return ""
            def locator(self, sel):
                if "已签到" in sel:
                    return FakeLocator(sel, visible=state["checked"], text="已签到")
                if "立即签到" in sel:
                    return FakeLocator(sel, visible=not state["checked"], text="立即签到")
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            if state["checked"]:
                return "签到成功\n已签到"
            return "个人中心\n立即签到"

        orig = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
        finally:
            m.page_text = orig

        self.assertEqual(res.status, "OK")
        self.assertIn("logout", order, "必须先调用站内 /api/user/logout")
        self.assertIn("sso", order, "logout 后必须重新走 LinuxDO SSO")
        self.assertLess(order.index("logout"), order.index("sso"))
        cta_clicks = [i for i, o in enumerate(order)
                      if o.startswith("click:") and "立即签到" in o]
        self.assertTrue(cta_clicks, "重新登录后应点击「立即签到」")
        self.assertLess(order.index("sso"), cta_clicks[0], "SSO 必须发生在点击之前")

    def test_xmiaom_fresh_login_failure_returns_auth_failed(self):
        """SSO 刷新失败 → 在扫描 CTA 之前返回 auth_failed, 不再点签到。"""
        m = self.m
        touched = []

        async def fake_sso_timeout(page, origin_host="", browser=None):
            return "TIMEOUT"

        m.try_linuxdo_sso = fake_sso_timeout

        class FakePage:
            url = "https://ai.xmiaom.com/dashboard/overview"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                return True
            def locator(self, sel):
                touched.append(sel)
                raise AssertionError(f"登录失败后不应访问 DOM: {sel}")

        async def fake_page_text(p, limit=2000):
            return "登录\n使用 LinuxDO 继续"

        orig = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
        finally:
            m.page_text = orig

        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_failed")
        self.assertIn("sso=TIMEOUT", res.detail)
        self.assertEqual(touched, [], "登录失败时应立即返回, 不扫描签到按钮")

    def test_xmiaom_logout_returns_to_site_origin_first(self):
        """页面停在别的源(OAuth/CF)时先回站点根再注销, 保证 fetch 打对 origin。"""
        m = self.m
        order = []
        state = {"checked": False, "offsite": True}

        async def fake_sso(page, origin_host="", browser=None):
            order.append("sso")
            return "OK"

        m.try_linuxdo_sso = fake_sso

        class FakeLocator:
            def __init__(self, sel, visible=True, text=""):
                self.sel = sel
                self._visible = visible
                self._text = text
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def inner_text(self):
                return self._text
            async def click(self, **kw):
                order.append("click:" + self.sel)
                if "立即签到" in self.sel:
                    state["checked"] = True

        class FakePage:
            def __init__(self):
                self.url = "about:blank"
            async def goto(self, url, *a, **k):
                order.append("goto:" + url)
                if "/dashboard/overview" in url and state["offsite"]:
                    # 未登录被直送 LinuxDO 授权页: 页面不在站点源上
                    state["offsite"] = False
                    self.url = "https://connect.linux.do/oauth2/authorize"
                else:
                    self.url = url
            async def evaluate(self, expr, *a, **k):
                if "/api/user/logout" in expr:
                    order.append("logout")
                    return True
                return ""
            def locator(self, sel):
                if "已签到" in sel:
                    return FakeLocator(sel, visible=state["checked"], text="已签到")
                if "立即签到" in sel:
                    return FakeLocator(sel, visible=not state["checked"], text="立即签到")
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            if state["checked"]:
                return "签到成功\n已签到"
            return "个人中心\n立即签到"

        orig = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
        finally:
            m.page_text = orig

        self.assertEqual(res.status, "OK")
        root_goto = "goto:" + m.XMIAOM_ORIGIN + "/"
        self.assertIn(root_goto, order, "注销前应先回到站点根")
        self.assertLess(
            order.index(root_goto), order.index("logout"),
            "必须先在站点源上, 再发 /api/user/logout",
        )

    def test_xmiaom_sso_timeout_retries_then_fails_with_logout_state(self):
        """SSO 两次 TIMEOUT → auth_failed, detail 里同时带 sso 与 logout 状态。"""
        m = self.m
        sso_calls = []
        logout_calls = []

        async def fake_sso(page, origin_host="", browser=None):
            sso_calls.append(origin_host)
            return "TIMEOUT"

        m.try_linuxdo_sso = fake_sso

        class FakePage:
            url = "https://ai.xmiaom.com/dashboard/overview"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                if "/api/user/logout" in expr:
                    logout_calls.append(1)
                    return True
                return ""
            def locator(self, sel):
                raise AssertionError("登录失败后不应访问 DOM")

        async def fake_page_text(p, limit=2000):
            return "登录\n使用 LinuxDO 继续"

        orig = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
        finally:
            m.page_text = orig

        self.assertEqual(len(logout_calls), 1, "每次签到只注销一次")
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_failed")
        self.assertIn("sso=TIMEOUT", res.detail)
        self.assertIn("logout=ok", res.detail)
        self.assertEqual(len(sso_calls), 2, "TIMEOUT 应重试一次")

    def test_xmiaom_waits_for_card_hydration_before_scan(self):
        """登录后必须等签到卡片挂载(SPA 外壳先到), 否则会误报 no_button。"""
        m = self.m
        waits = []

        async def fake_wait_any(page, selectors, total_s, **kw):
            waits.append((list(selectors), total_s, kw))
            return None, None

        m.wait_for_any_visible = fake_wait_any

        class FakeLocator:
            def __init__(self, sel, text="已签到", visible=True):
                self.sel = sel
                self._text = text
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def inner_text(self):
                return self._text
            async def click(self, **kw):
                pass

        class FakePage:
            url = "https://ai.xmiaom.com/dashboard/overview"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                return True if "/api/user/logout" in expr else ""
            def locator(self, sel):
                if "已签到" in sel:
                    return FakeLocator(sel, text="已签到", visible=True)
                if "立即签到" in sel:
                    return FakeLocator(sel, text="立即签到", visible=False)
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            return "咕嘎咕嘎生图站\n个人中心\n已签到"

        orig = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
        finally:
            m.page_text = orig

        self.assertEqual(res.status, "ALREADY")
        self.assertTrue(waits, "登录后应等待签到卡片挂载")
        selectors, total_s, kwargs = waits[0]
        self.assertTrue(any("立即签到" in s for s in selectors))
        self.assertTrue(any("已签到" in s for s in selectors))
        self.assertEqual(total_s, m.XMIAOM_CARD_WAIT_S)
        self.assertTrue(kwargs.get("trusted_sign"), "显式选择器应跳过 CTA 推断")

    def test_xmiaom_logout_not_on_site_skips_logout(self):
        """页面回不到站点源时跳过 logout(不误打其它 origin), 但继续走 SSO。"""
        m = self.m
        order = []
        state = {"checked": False}

        async def fake_sso(page, origin_host="", browser=None):
            order.append("sso")
            return "OK"

        m.try_linuxdo_sso = fake_sso

        class FakeLocator:
            def __init__(self, sel, visible=True, text=""):
                self.sel = sel
                self._visible = visible
                self._text = text
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def inner_text(self):
                return self._text
            async def click(self, **kw):
                order.append("click:" + self.sel)
                if "立即签到" in self.sel:
                    state["checked"] = True

        class FakePage:
            url = "https://connect.linux.do/oauth2/authorize"
            async def goto(self, *a, **k):
                # goto 始终留在授权页: 永远回不到站点源
                pass
            async def evaluate(self, expr, *a, **k):
                if "/api/user/logout" in expr:
                    order.append("logout")
                    return True
                return ""
            def locator(self, sel):
                if "已签到" in sel:
                    return FakeLocator(sel, visible=state["checked"], text="已签到")
                if "立即签到" in sel:
                    return FakeLocator(sel, visible=not state["checked"], text="立即签到")
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            if state["checked"]:
                return "签到成功\n已签到"
            return "个人中心\n立即签到"

        orig = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
        finally:
            m.page_text = orig

        self.assertEqual(res.status, "OK")
        self.assertNotIn("logout", order, "不在站点源时绝不能发站点注销请求")
        self.assertIn("sso", order, "logout 跳过不应阻断 SSO")

    def test_xmiaom_sso_timeout_retries_once(self):
        """SSO 偶发 TIMEOUT(授权页不回跳) → 原地重试一次成功。"""
        m = self.m
        state = {"checked": False}
        sso_calls = []

        async def flaky_sso(page, origin_host="", browser=None):
            sso_calls.append(origin_host)
            return "TIMEOUT" if len(sso_calls) == 1 else "OK"

        m.try_linuxdo_sso = flaky_sso

        class FakeLocator:
            def __init__(self, sel, visible=True, text=""):
                self.sel = sel
                self._visible = visible
                self._text = text
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def inner_text(self):
                return self._text
            async def click(self, **kw):
                if "立即签到" in self.sel:
                    state["checked"] = True

        class FakePage:
            url = "https://ai.xmiaom.com/dashboard/overview"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                if "/api/user/logout" in expr:
                    return True
                return ""
            def locator(self, sel):
                if "已签到" in sel:
                    return FakeLocator(sel, visible=state["checked"], text="已签到")
                if "立即签到" in sel:
                    return FakeLocator(sel, visible=not state["checked"], text="立即签到")
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            if state["checked"]:
                return "签到成功\n已签到"
            return "个人中心\n立即签到"

        orig = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
        finally:
            m.page_text = orig

        self.assertEqual(res.status, "OK")
        self.assertEqual(len(sso_calls), 2, "TIMEOUT 后应重试一次 SSO")
        self.assertTrue(all(h == m.XMIAOM_HOST for h in sso_calls))

    def test_xmiaom_already_checked_in(self):
        """页面显示「已签到」时直接返回 ALREADY。"""
        m = self.m
        clicks = []

        class FakeLocator:
            def __init__(self, sel, text="已签到", visible=True):
                self.sel = sel
                self._text = text
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def inner_text(self):
                return self._text
            async def click(self, **kw):
                clicks.append(self.sel)

        class FakePage:
            url = "https://ai.xmiaom.com/dashboard/overview"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k): return ""
            def locator(self, sel):
                if "已签到" in sel:
                    return FakeLocator(sel, text="已签到", visible=True)
                if "立即签到" in sel:
                    return FakeLocator(sel, text="立即签到", visible=False)
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            return "咕嘎咕嘎生图站\n个人中心\n已签到\n今日已签到"

        orig_page_text = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "ALREADY")
            self.assertIn("已签到", res.detail)
            self.assertEqual(len(clicks), 0)
        finally:
            m.page_text = orig_page_text

    def test_xmiaom_successful_checkin(self):
        """点击「立即签到」后按钮转为「已签到」返回 OK。"""
        m = self.m
        clicks = []
        state = {"checked": False}

        class FakeLocator:
            def __init__(self, sel, visible=True, text=""):
                self.sel = sel
                self._visible = visible
                self._text = text
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def inner_text(self):
                return self._text
            async def click(self, **kw):
                clicks.append(self.sel)
                if "立即签到" in self.sel:
                    state["checked"] = True

        class FakePage:
            url = "https://ai.xmiaom.com/dashboard/overview"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k): return ""
            def locator(self, sel):
                if "已签到" in sel:
                    return FakeLocator(sel, visible=state["checked"], text="已签到")
                if "立即签到" in sel:
                    return FakeLocator(sel, visible=not state["checked"], text="立即签到")
                return FakeLocator(sel, visible=False)

        async def fake_page_text(p, limit=2000):
            if state["checked"]:
                return "咕嘎咕嘎生图站\n签到成功\n已签到"
            return "咕嘎咕嘎生图站\n个人中心\n立即签到"

        orig_page_text = m.page_text
        m.page_text = fake_page_text
        try:
            res = self.asyncio.run(m.xmiaom_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "OK")
            self.assertEqual(res.action.kind, "dom_click")
            self.assertTrue(any("立即签到" in c for c in clicks))
        finally:
            m.page_text = orig_page_text


class GladosCheckinTest(unittest.TestCase):
    """glados (https://glados.rocks/console/checkin) 专属签到流程单测 (2026-09-30)."""

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def _adapter(self):
        # 2026-11-06 前端改版: 按钮改为 button.ckb（完成态 .is-done），
        # 旧 button.checkin-cute-btn 保留双兼容（与 runner 内置选择器一致）。
        return self.m.SiteAdapter(
            name="glados",
            url="https://glados.rocks/console/checkin",
            kind="glados",
            sign_selectors=[
                'button.ckb:not(.is-done)',
                'button.checkin-cute-btn:not(.is-done)',
                'button.ckb:has-text("Checkin")',
                'button.checkin-cute-btn:has-text("Checkin")',
            ],
            already_selectors=[
                'button.ckb.is-done',
                'button.checkin-cute-btn.is-done',
                'text=Checkin! Got',
                'text=Please Try Tomorrow',
            ],
            prefer_catalog_url=True,
            login_url="https://glados.rocks/login",
        )

    def test_is_glados_site_and_name(self):
        self.assertTrue(self.m.is_glados_site("https://glados.rocks/console/checkin"))
        self.assertTrue(self.m.is_glados_site("https://glados.network/console/checkin"))
        self.assertFalse(self.m.is_glados_site("https://example.com"))
        self.assertTrue(self.m.is_glados_name("glados"))
        self.assertTrue(self.m.is_glados_name("GLaDOS"))
        self.assertFalse(self.m.is_glados_name("other"))

    def test_kind_in_literal_and_yaml_and_builtin(self):
        self.assertIn("glados", self.m.AdapterKind.__args__)
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "glados"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 glados 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "glados")
        self.assertEqual(a.url, "https://glados.rocks/console/checkin")
        self.assertTrue(a.prefer_catalog_url)
        b = next((x for x in _BUILTIN_SITE_ADAPTERS if x.name == "glados"), None)
        self.assertIsNotNone(b, "内置 adapter 应包含 glados")
        self.assertEqual(b.kind, "glados")

    def test_p0_no_clear_cookies_in_glados_path(self):
        import inspect
        src = inspect.getsource(self.m.glados_checkin)
        lower = src.lower()
        self.assertNotIn("clear_cookies", lower)
        self.assertNotIn("clear_localstorage", lower)

    def test_glados_points_has_today_checkin_helper(self):
        pts_today = {
            "code": 0,
            "history": [
                {"business": "system:checkin", "detail": "2026-09-30", "change": "10.0"},
            ],
        }
        pts_yesterday = {
            "code": 0,
            "history": [
                {"business": "system:checkin", "detail": "2026-09-29", "change": "10.0"},
            ],
        }
        self.assertTrue(self.m._glados_points_has_today_checkin(pts_today, today_str="2026-09-30"))
        self.assertFalse(self.m._glados_points_has_today_checkin(pts_yesterday, today_str="2026-09-30"))
        self.assertFalse(self.m._glados_points_has_today_checkin(None, today_str="2026-09-30"))

    def test_glados_fresh_checkin_returns_ok(self):
        """今日未签时点击签到按钮 (button.ckb)，按钮变为 Checkin! Got 1 Points -> OK."""
        m = self.m
        state = {"clicked": False}

        def _is_done_sel(sel: str) -> bool:
            # 完成态选择器形如 'button.ckb.is-done, ...'；待签态带 ':not(.is-done)'
            # 不能再用子串包含判定（2026-11-06 新选择器双兼容踩坑）。
            return "is-done" in sel and ":not(" not in sel

        class FakeLocator:
            def __init__(self, sel):
                self.sel = sel
            @property
            def first(self):
                return self
            async def count(self):
                if _is_done_sel(self.sel):
                    return 1 if state["clicked"] else 0
                return 1
            async def is_visible(self, timeout=500):
                if _is_done_sel(self.sel):
                    return state["clicked"]
                return True
            async def inner_text(self, timeout=500):
                if _is_done_sel(self.sel) and state["clicked"]:
                    return "Checkin! Got 1 Points"
                return "Checkin"
            async def click(self, **kw):
                state["clicked"] = True

        class FakePage:
            url = "https://glados.rocks/console/checkin"
            async def goto(self, *a, **k): pass
            def locator(self, sel):
                return FakeLocator(sel)

        async def fake_fetch_points(page):
            return {"code": 0, "history": []}

        async def fake_wait_ready(*a, **k): return True
        async def fake_cf(*a, **k): return None
        async def fake_text(page, n=1200): return "GLaDOS Points Daily Punch Checked in Missed"
        async def fake_url(page): return "https://glados.rocks/console/checkin"

        with mock.patch.object(m, "_glados_fetch_points", fake_fetch_points), \
             mock.patch.object(m, "wait_text_ready", fake_wait_ready), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_url):
            res = self.asyncio.run(m.glados_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "OK")
            self.assertIn("Checkin! Got 1 Points", res.detail)
            self.assertEqual(res.transition, "PENDING_TO_DONE")

    def test_glados_repeat_checkin_returns_already(self):
        """今日已签或按钮返回 Please Try Tomorrow 时返回 ALREADY."""
        m = self.m
        state = {"clicked": False}

        def _is_done_sel(sel: str) -> bool:
            return "is-done" in sel and ":not(" not in sel

        class FakeLocator:
            def __init__(self, sel):
                self.sel = sel
            @property
            def first(self):
                return self
            async def count(self):
                if _is_done_sel(self.sel):
                    return 1 if state["clicked"] else 0
                return 1
            async def is_visible(self, timeout=500):
                if _is_done_sel(self.sel):
                    return state["clicked"]
                return True
            async def inner_text(self, timeout=500):
                if _is_done_sel(self.sel) and state["clicked"]:
                    return "Checkin Repeats! Please Try Tomorrow"
                return "Checkin"
            async def click(self, **kw):
                state["clicked"] = True

        class FakePage:
            url = "https://glados.rocks/console/checkin"
            async def goto(self, *a, **k): pass
            def locator(self, sel):
                return FakeLocator(sel)

        async def fake_fetch_points(page):
            return {"code": 0, "history": []}

        async def fake_wait_ready(*a, **k): return True
        async def fake_cf(*a, **k): return None
        async def fake_text(page, n=1200): return "GLaDOS Points Daily Punch Checked in Missed"
        async def fake_url(page): return "https://glados.rocks/console/checkin"

        with mock.patch.object(m, "_glados_fetch_points", fake_fetch_points), \
             mock.patch.object(m, "wait_text_ready", fake_wait_ready), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "page_text", fake_text), \
             mock.patch.object(m, "page_url", fake_url):
            res = self.asyncio.run(m.glados_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "ALREADY")
            self.assertIn("Try Tomorrow", res.detail)


class IloveCatCheckinTest(unittest.TestCase):
    """靴貓公益站 (https://api.ilovecat520.me/playground) 每日摸猫 + 猫咪占卜 专属签到流程单测."""

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def _adapter(self):
        return self.m.SiteAdapter(
            name="靴貓公益站",
            url="https://api.ilovecat520.me/playground",
            kind="ilovecat",
            sign_selectors=[
                "button.cat-pet__cat",
                "button.cat-fortune__drum",
            ],
            already_selectors=[
                "text=明天再来摸摸吧",
                "text=今天已经抽过了，明天再来抽一支吧。",
            ],
            prefer_catalog_url=True,
            login_url="https://api.ilovecat520.me/login",
        )

    def test_is_ilovecat_site_positive_negative(self):
        f = self.m.is_ilovecat_site
        self.assertTrue(f("https://api.ilovecat520.me/playground"))
        self.assertTrue(f("https://api.ilovecat520.me/login"))
        self.assertTrue(f("https://ilovecat520.me"))
        self.assertFalse(f("https://example.com/"))
        self.assertFalse(f(""))

    def test_is_ilovecat_name_positive_negative(self):
        f = self.m.is_ilovecat_name
        self.assertTrue(f("靴貓公益站"))
        self.assertTrue(f("靴貓"))
        self.assertTrue(f("ilovecat"))
        self.assertTrue(f("每日摸猫"))
        self.assertFalse(f("other_site"))

    def test_kind_in_literal(self):
        self.assertIn("ilovecat", self.m.AdapterKind.__args__)

    def test_yaml_registers_ilovecat_entry(self):
        """sites.yaml 的 靴貓公益站 条目解析为 kind=ilovecat 且主路径为 /dashboard/overview。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "靴貓公益站"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 靴貓公益站 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "ilovecat")
        self.assertEqual(a.url, "https://api.ilovecat520.me/dashboard/overview")
        self.assertFalse(a.prefer_catalog_url)
        self.assertEqual(a.login_url, "https://api.ilovecat520.me/login")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("button.cat-pet__cat", joined_signs)
        self.assertIn("button.cat-fortune__drum", joined_signs)

    def test_builtin_entry_ilovecat(self):
        """_BUILTIN_SITE_ADAPTERS 中的 靴貓公益站 为 ilovecat adapter。"""
        from stealth_checkin_runner import _BUILTIN_SITE_ADAPTERS
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "靴貓公益站"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 靴貓公益站")
        self.assertEqual(builtin_entry.kind, "ilovecat")
        self.assertEqual(builtin_entry.url, "https://api.ilovecat520.me/dashboard/overview")
        self.assertFalse(builtin_entry.prefer_catalog_url)
        self.assertEqual(builtin_entry.login_url, "https://api.ilovecat520.me/login")

    def test_dispatch_contains_ilovecat_branch(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("is_ilovecat_site(site_url)", src)
        self.assertIn("return await ilovecat_checkin(page, adapter, browser=browser)", src)

    def test_p0_no_clear_cookies_in_ilovecat_path(self):
        """ilovecat 专属流程不得调用 clear_cookies / clear_localstorage / 全清接口."""
        import inspect
        src = inspect.getsource(self.m.ilovecat_checkin)
        lower = src.lower()
        self.assertNotIn("clear_cookies", lower)
        self.assertNotIn("clear_localstorage", lower)
        self.assertNotIn("clear_all", lower)

    def test_ilovecat_already_checked_in(self):
        """API 与页面均显示今日已摸猫、已占卜时，直接返回 ALREADY。"""
        m = self.m
        clicks = []

        class FakeLocator:
            def __init__(self, sel, visible=True):
                self.sel = sel
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def click(self, **kw):
                clicks.append(self.sel)

        class FakePage:
            url = "https://api.ilovecat520.me/playground"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                if "/api/cat/status" in expr:
                    return {
                        "ok": True,
                        "status": 200,
                        "data": {
                            "pet": {"played": True, "reward": 2500},
                            "fortune": {"played": True, "reward": 3000},
                        }
                    }
                return ""
            def locator(self, sel):
                return FakeLocator(sel, visible=True)

        async def fake_page_text(p, limit=2000):
            return "靴貓公益站\n每日摸猫\n明天再来摸摸吧\n猫咪占卜\n今天已经抽过了"

        async def fake_page_url(p):
            return "https://api.ilovecat520.me/playground"

        async def fake_bypass(*a, **k):
            return False

        orig_page_text = m.page_text
        orig_page_url = m.page_url
        orig_bypass = m.bypass_chrome_interstitial_if_needed
        m.page_text = fake_page_text
        m.page_url = fake_page_url
        m.bypass_chrome_interstitial_if_needed = fake_bypass
        try:
            res = self.asyncio.run(m.ilovecat_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "ALREADY")
            self.assertIn("摸猫", res.detail)
            self.assertIn("占卜", res.detail)
            self.assertEqual(len(clicks), 0)
        finally:
            m.page_text = orig_page_text
            m.page_url = orig_page_url
            m.bypass_chrome_interstitial_if_needed = orig_bypass

    def test_ilovecat_fresh_checkin_both_actions(self):
        """未签到状态下点击摸猫与占卜按钮，完成后均返回 OK 并包含两个动作的详情。"""
        m = self.m
        clicks = []
        status_call_count = [0]

        class FakeLocator:
            def __init__(self, sel, visible=True):
                self.sel = sel
                self._visible = visible
            @property
            def first(self):
                return self
            async def count(self):
                return 1 if self._visible else 0
            async def is_visible(self, timeout=800):
                return self._visible
            async def click(self, **kw):
                clicks.append(self.sel)

        class FakePage:
            url = "https://api.ilovecat520.me/playground"
            async def goto(self, *a, **k): pass
            async def evaluate(self, expr, *a, **k):
                if "/api/cat/status" in expr:
                    status_call_count[0] += 1
                    if status_call_count[0] == 1:
                        # 初始查询：未摸未抽
                        return {
                            "ok": True,
                            "status": 200,
                            "data": {
                                "pet": {"played": False},
                                "fortune": {"played": False},
                            }
                        }
                    else:
                        # 点击后二次验证：已完成
                        return {
                            "ok": True,
                            "status": 200,
                            "data": {
                                "pet": {"played": True, "reward": 2191},
                                "fortune": {"played": True, "reward": 3000},
                            }
                        }
                return ""
            def locator(self, sel):
                return FakeLocator(sel, visible=True)

        async def fake_page_text(p, limit=2000):
            return "靴貓公益站\n每日摸猫\n猫咪占卜"

        async def fake_page_url(p):
            return "https://api.ilovecat520.me/playground"

        async def fake_bypass(*a, **k):
            return False

        orig_page_text = m.page_text
        orig_page_url = m.page_url
        orig_bypass = m.bypass_chrome_interstitial_if_needed
        m.page_text = fake_page_text
        m.page_url = fake_page_url
        m.bypass_chrome_interstitial_if_needed = fake_bypass
        try:
            res = self.asyncio.run(m.ilovecat_checkin(FakePage(), self._adapter()))
            self.assertEqual(res.status, "OK")
            self.assertIn("摸猫成功", res.detail)
            self.assertIn("占卜成功", res.detail)
            self.assertTrue(any("cat-pet__cat" in c for c in clicks))
            self.assertTrue(any("cat-fortune__drum" in c for c in clicks))
        finally:
            m.page_text = orig_page_text
            m.page_url = orig_page_url
            m.bypass_chrome_interstitial_if_needed = orig_bypass


class TestPipiwangcomSpaLogin(unittest.TestCase):
    """皮皮智绘 2026-10-25 改版:登录入口在 SPA 遮罩层,通用 SSO 扫不到。

    当日 11:33 实证:未登录落地页只有「进入控制台」,签到按钮是隐藏 DOM;
    通用流程不识别遮罩层 → no_button。修复:本站专属先登录再走通用 CTA 扫描。
    """

    def setUp(self):
        self.m = load_mod()

    def test_is_pipiwangcom_site(self):
        m = self.m
        self.assertTrue(m.is_pipiwangcom_site("https://img.pipiwangcom.com/"))
        self.assertTrue(m.is_pipiwangcom_site("https://img.pipiwangcom.com/profile"))
        self.assertFalse(m.is_pipiwangcom_site("https://example.com/"))
        self.assertFalse(m.is_pipiwangcom_site(""))

    def test_login_clicks_overlay_then_authorizes(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m

        class FakePage:
            def __init__(self):
                self.url = "https://img.pipiwangcom.com/"
                self.clicks = []
                self.logged_in = False

            async def click(self, selector, timeout=0):
                self.clicks.append(selector)
                if "Linux.do" in selector:
                    self.url = (
                        "https://connect.linux.do/oauth2/authorize"
                        "?response_type=code&client_id=test"
                    )

        page = FakePage()

        async def fake_console_ready(_page):
            return page.logged_in

        async def fake_approve(_page):
            page.logged_in = True
            page.url = "https://img.pipiwangcom.com/"
            return "a:has-text('允许')"

        with patch.object(m, "SSO_TIMEOUT_S", 3), \
             patch.object(m, "_pipiwangcom_console_ready", fake_console_ready), \
             patch.object(m, "_approve_linuxdo_authorize", fake_approve), \
             patch.object(m.asyncio, "sleep", AsyncMock()):
            status = asyncio.run(m._pipiwangcom_login(page))

        self.assertEqual(status, "OK")
        self.assertIn('button.btn-primary:has-text("进入控制台")', page.clicks)
        self.assertIn('button.btn-linux:has-text("Linux.do")', page.clicks)

    def test_login_skips_opener_when_overlay_already_open(self):
        """遮罩层已弹开时不得再点「进入控制台」(否则宽泛「登录」会提交空账密表单)。"""
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m

        class First:
            async def is_visible(self, timeout=0):
                return True

        class Loc:
            first = First()

        class FakePage:
            def __init__(self):
                self.url = "https://img.pipiwangcom.com/"
                self.clicks = []
                self.logged_in = False

            def locator(self, selector):
                return Loc()

            async def click(self, selector, timeout=0):
                self.clicks.append(selector)
                if "Linux.do" in selector:
                    self.url = "https://connect.linux.do/oauth2/authorize?client_id=test"

        page = FakePage()

        async def fake_console_ready(_page):
            return page.logged_in

        async def fake_approve(_page):
            page.logged_in = True
            page.url = "https://img.pipiwangcom.com/"
            return "a:has-text('允许')"

        with patch.object(m, "SSO_TIMEOUT_S", 3), \
             patch.object(m, "_pipiwangcom_console_ready", fake_console_ready), \
             patch.object(m, "_approve_linuxdo_authorize", fake_approve), \
             patch.object(m.asyncio, "sleep", AsyncMock()):
            status = asyncio.run(m._pipiwangcom_login(page))

        self.assertEqual(status, "OK")
        self.assertFalse(any("进入控制台" in c for c in page.clicks))
        self.assertIn('button.btn-linux:has-text("Linux.do")', page.clicks)

    def test_login_returns_no_button_without_overlay_cta(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m

        class FakePage:
            url = "https://img.pipiwangcom.com/"

            async def click(self, selector, timeout=0):
                raise RuntimeError("not found")

        with patch.object(m, "SSO_TIMEOUT_S", 1), \
             patch.object(m.asyncio, "sleep", AsyncMock()):
            status = asyncio.run(m._pipiwangcom_login(FakePage()))

        self.assertEqual(status, "NO_BUTTON")

    def test_dismiss_notice_clicks_visible_button(self):
        """公告遮罩 #noticeModal 会拦住签到按钮点击;先点可见的关闭按钮。"""
        import asyncio

        m = self.m

        class Loc:
            def __init__(self, visible):
                self.visible = visible
                self.clicked = False

            async def is_visible(self, timeout=0):
                return self.visible

            async def click(self, timeout=0):
                self.clicked = True

        class FakePage:
            def __init__(self, visible_selectors):
                self.locs = {sel: Loc(sel in visible_selectors) for sel in visible_selectors}

            def locator(self, selector):
                loc = self.locs.setdefault(selector, Loc(False))
                return type("W", (), {"first": loc})()

        page = FakePage({"#btnNoticeOk"})
        asyncio.run(m._pipiwangcom_dismiss_notice(page))
        self.assertTrue(page.locs["#btnNoticeOk"].clicked)

    def test_dismiss_notice_noop_without_modal(self):
        import asyncio

        m = self.m

        class Loc:
            async def is_visible(self, timeout=0):
                return False

            async def click(self, timeout=0):
                raise AssertionError("must not click an invisible notice button")

        class FakePage:
            def locator(self, selector):
                return type("W", (), {"first": Loc()})()

        asyncio.run(m._pipiwangcom_dismiss_notice(FakePage()))  # 不抛异常即通过

    def test_source_wires_pipiwangcom_login_before_generic(self):
        """源码守卫:本站登录钩子必须在通用 SSO/CTA 扫描之前接入。"""
        src = TARGET.read_text(encoding="utf-8")
        hook = re.search(
            r"if is_pipiwangcom_site\(site_url\).*?_pipiwangcom_console_ready\(page\).*?"
            r"_pipiwangcom_login\(page\)",
            src,
            re.S,
        )
        self.assertIsNotNone(hook, "pipiwangcom 登录钩子必须在 legacy_checkin_on_page 接入")
        self.assertIn('button.btn-linux:has-text("Linux.do")', src)
        self.assertIn("#btnNoticeOk", src)
        body = src[src.index("async def legacy_checkin_on_page"):]
        self.assertLess(
            body.index("await _pipiwangcom_login(page)"),
            body.index("maybe_sso_if_needed("),
        )

    def test_source_orders_pipiwangcom_login_after_block_classification(self):
        """Chrome 错误页/CF 墙必须先被归类,否则会被误报成 auth_required。"""
        src = TARGET.read_text(encoding="utf-8")
        body = src[src.index("async def legacy_checkin_on_page"):]
        login_at = body.index("await _pipiwangcom_login(page)")
        self.assertGreater(
            login_at, body.index("initial_block = classify_page_block("),
            "pipiwangcom 登录钩子必须排在 CF/upstream 分类之后",
        )
        self.assertLess(
            login_at, body.index("maybe_sso_if_needed("),
            "钩子必须在通用 SSO 扫描之前",
        )

    def test_dismiss_obstructing_dialogs_handles_pipiwangcom_notice(self):
        """公告在每个阶段都该被清掉(pre_already/pre_cta/pre_click),放进通用 dismiss。"""
        src = TARGET.read_text(encoding="utf-8")
        block = re.search(
            r"async def dismiss_obstructing_dialogs.*?return total_closed",
            src,
            re.S,
        )
        self.assertIsNotNone(block)
        self.assertIn('if "pipiwangcom.com" in url_str:', block.group(0))
        self.assertIn("_pipiwangcom_dismiss_notice(page)", block.group(0))


class TestJianzhileCheckin(unittest.TestCase):
    """简直了 (jianzhile.vip) 专属图形验证码签到测试."""

    def setUp(self):
        self.m = load_mod(force=True)

    def test_is_jianzhile_site(self):
        self.assertTrue(self.m.is_jianzhile_site("https://jianzhile.vip/console/personal"))
        self.assertTrue(self.m.is_jianzhile_site("https://jianzhile.vip/"))
        self.assertFalse(self.m.is_jianzhile_site("https://example.com/"))
        self.assertFalse(self.m.is_jianzhile_site(""))

    def test_is_jianzhile_name(self):
        self.assertTrue(self.m.is_jianzhile_name("简直了"))
        self.assertTrue(self.m.is_jianzhile_name("jianzhile"))
        self.assertTrue(self.m.is_jianzhile_name("jianzhile_api"))
        self.assertFalse(self.m.is_jianzhile_name("other_site"))

    def test_resolve_jianzhile_adapter(self):
        # 1. 内置 adapter
        builtin = next((x for x in self.m._BUILTIN_SITE_ADAPTERS if x.name == "简直了"), None)
        self.assertIsNotNone(builtin)
        self.assertEqual(builtin.kind, "jianzhile")

        # 2. resolve_site
        resolved = self.m.resolve_site("简直了", "https://jianzhile.vip/console/personal")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.kind, "jianzhile")
        self.assertEqual(resolved.url, "https://jianzhile.vip/console/personal")

        # 3. sites.yaml
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "简直了"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 简直了 条目")
        self.assertEqual(entry.get("kind"), "jianzhile")

    def test_extract_captcha_token(self):
        f = self.m.extract_captcha_token
        # 1. 纯字母数字验证码
        self.assertEqual(f("2GML5"), "2GML5")
        self.assertEqual(f("2wb2s"), "2wb2s")
        # 2. Markdown 代码块与包裹引号
        self.assertEqual(f("```\n2WB2S\n```"), "2WB2S")
        self.assertEqual(f('"5AHA9"'), "5AHA9")
        self.assertEqual(f("'XYZ1'"), "XYZ1")
        # 3. 冗余自然语言说明与标点
        self.assertEqual(f("The CAPTCHA code is 5AHA9."), "5AHA9")
        self.assertEqual(f("Answer: 2GML5\nHope this helps!",), "2GML5")
        # 4. 空值与边界
        self.assertEqual(f(""), "")
        self.assertEqual(f("   "), "")

    def test_solve_captcha_with_vision_logic(self):
        import io
        import json
        from unittest.mock import patch, MagicMock

        # 1. 空图片直接返回空
        self.assertEqual(self.m.solve_captcha_with_vision(b""), "")

        # 2. Mock 接口调用 (默认配置)
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": " 2GML5 \n"}}]
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch.dict(os.environ, {"DAILY_CHECKIN_VISION_API_KEY": "test-key"}), \
                patch("urllib.request.urlopen", return_value=mock_response):
            code = self.m.solve_captcha_with_vision(b"fake_image_bytes")
            self.assertEqual(code, "2GML5")

    def test_solve_captcha_with_vision_env_overrides(self):
        import json
        from unittest.mock import patch, MagicMock

        captured_req = []

        def fake_urlopen(req, context=None, timeout=None):
            captured_req.append(req)
            m = MagicMock()
            m.read.return_value = json.dumps({
                "choices": [{"message": {"content": "XYZ89"}}]
            }).encode("utf-8")
            m.__enter__.return_value = m
            return m

        custom_env = {
            "DAILY_CHECKIN_VISION_API_BASE": "https://custom-gateway.io/v1",
            "DAILY_CHECKIN_VISION_API_KEY": "sk-custom-secret-key-12345",
            "DAILY_CHECKIN_VISION_MODEL": "custom-glm-vision",
        }

        with patch.dict(os.environ, custom_env), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            code = self.m.solve_captcha_with_vision(b"test_bytes")
            self.assertEqual(code, "XYZ89")
            self.assertEqual(len(captured_req), 1)
            req = captured_req[0]
            self.assertEqual(req.full_url, "https://custom-gateway.io/v1/chat/completions")
            self.assertEqual(req.headers.get("Authorization"), "Bearer sk-custom-secret-key-12345")
            data = json.loads(req.data.decode("utf-8"))
            self.assertEqual(data["model"], "custom-glm-vision")


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


if __name__ == "__main__":
    unittest.main()

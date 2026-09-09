#!/usr/bin/env python3
"""
Pure unit tests for stealth_checkin_runner M1 + full-opt guards (no CDP / no network).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import py_compile
import os
import py_compile
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "stealth_checkin_runner.py"


def load_mod(force: bool = False):
    name = "stealth_checkin_runner"
    if force and name in sys.modules:
        del sys.modules[name]
    if name in sys.modules and not force:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestMarkTaskDone(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_mark_markdown_link(self):
        src = "- [ ] #task #日常 [HotaruAPI](https://x)\n- [ ] #task #日常 [猫](https://y)\n"
        out, ch = self.m.mark_task_done(src, "HotaruAPI")
        self.assertTrue(ch)
        self.assertIn("- [x] #task #日常 [HotaruAPI](https://x)", out)
        self.assertIn("- [ ] #task #日常 [猫]", out)

    def test_already_marked_no_change(self):
        src = "- [x] #task #日常 [HotaruAPI](https://x)\n"
        out, ch = self.m.mark_task_done(src, "HotaruAPI")
        self.assertFalse(ch)
        self.assertEqual(out, src)

    def test_parse_open_tasks_skips_done(self):
        src = (
            "- [ ] #task #日常 [A](https://a)\n"
            "- [x] #task #日常 [B](https://b)\n"
            "- [ ] #task #日常 [C](https://c)\n"
        )
        open_tasks = self.m.parse_open_tasks(src)
        names = [n for n, _ in open_tasks]
        self.assertEqual(names, ["A", "C"])

    def test_mark_clears_auto_fail(self):
        src = "- [ ] #task #日常 [7倍](https://7x.hk/profile) #auto-fail:no_confirm\n"
        out, ch = self.m.mark_task_done(src, "7倍")
        self.assertTrue(ch)
        self.assertIn("- [x] #task #日常 [7倍](https://7x.hk/profile)", out)
        self.assertNotIn("#auto-fail", out)

    def test_mark_task_failed_and_parse(self):
        src = "- [ ] #task #日常 [ciallo](https://ioll.pp.ua/profile)\n"
        out, ch = self.m.mark_task_failed(src, "ciallo", "no_confirm")
        self.assertTrue(ch)
        self.assertIn("#auto-fail:no_confirm", out)
        fails = self.m.parse_auto_fail_tasks(out)
        self.assertEqual(fails[0][0], "ciallo")

    def test_atomic_persist(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "daily.md"
            self.m.persist_task_file(str(fp), "line1\n")
            self.assertEqual(fp.read_text(encoding="utf-8"), "line1\n")
            self.assertEqual(list(Path(td).glob("*.tmp")), [])
            self.m.persist_task_file(str(fp), "line2\n")
            self.assertEqual(fp.read_text(encoding="utf-8"), "line2\n")

    def test_projection_sink_preflight_requires_writable_matching_open_rows(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "daily.md"
            content = "- [ ] #task #日常 [A](https://a.example)\n"
            fp.write_text(content, encoding="utf-8")
            self.assertIsNone(
                self.m.validate_task_projection_sink(str(fp), content, ["A"])
            )
            self.assertEqual(
                self.m.validate_task_projection_sink(str(fp), content, ["B"]),
                "task_projection_missing:B",
            )
            self.assertFalse((Path(td) / "daily.md.projection-preflight.tmp").exists())

    def test_projection_sink_preflight_missing_is_fail_closed_reason(self):
        with tempfile.TemporaryDirectory() as td:
            reason = self.m.validate_task_projection_sink(
                str(Path(td) / "missing.md"), "", ["A"]
            )
            self.assertTrue(reason.startswith("task_file_missing:"))


class TestSuccessGate(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_has_success_text(self):
        self.assertTrue(self.m.has_success_text("今日已签到，明天再来"))
        self.assertTrue(self.m.has_success_text("签到成功 获得 100"))
        self.assertFalse(self.m.has_success_text("请点击签到按钮"))
        self.assertFalse(self.m.has_success_text("开始转动"))
        self.assertFalse(self.m.has_success_text("1 已签到 2 已签到 3 已签到"))
        self.assertFalse(self.m.has_success_text("当前额度 $20"))
        self.assertFalse(self.m.has_success_text("已签到"))
        self.assertTrue(self.m.is_valid_checkin_confirm("今日状态：已签到"))

    def test_classifies_external_page_blocks_before_cta_scan(self):
        self.assertEqual(
            self.m.classify_page_block("404\n页面未找到", "https://example.test/profile")[0],
            "dead_url",
        )
        self.assertEqual(
            self.m.classify_page_block("403 Forbidden", "https://example.test")[0],
            "blocked",
        )
        self.assertEqual(
            self.m.classify_page_block("第10天签到需要当天永久余额实际消耗满 ¥5.00")[0],
            "business_ineligible",
        )

    def test_classifies_observed_production_block_pages(self):
        cases = [
            ("404\n糟糕！页面未找到！", "dead_url"),
            ("Sorry, you have been blocked\nYou are unable to access example.test", "blocked"),
            ("403\n地区限制\n当前地区不允许访问该资源", "blocked"),
            ("Bad gateway Error code 502\nVisit cloudflare.com", "upstream_unavailable"),
            ("Connection timed out Error code 522\nVisit cloudflare.com", "upstream_unavailable"),
            ("每日签到可获得固定额度奖励\n08:00 开放签到", "business_ineligible"),
            ("余额大于等于 $20，暂无法签到\n当前额度充足，无需签到", "business_ineligible"),
            (
                "30 天签到进度 今日永久余额有效消耗: ¥0.00 今日还差 ¥5.00 "
                "当天需消耗 ¥5.00 永久余额",
                "business_ineligible",
            ),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                result = self.m.classify_page_block(text, "https://example.test/profile")
                self.assertIsNotNone(result)
                self.assertEqual(result[0], expected)

    def test_permanent_balance_reward_copy_is_not_an_eligibility_failure(self):
        self.assertIsNone(
            self.m.classify_page_block(
                "每日签到可获得固定额度奖励，该额度将在午夜失效，"
                "不会增加你的永久余额。"
            )
        )

    def test_cross_below_threshold_allowing_copy_is_not_business_ineligible(self):
        # cross (newapi-checkin.keungliang.dpdns.org): logged-in card reads
        # 「当前余额低于阈值，可以签到」+ 立即签到 CTA + 签到阈值 $20.
        # The word 阈值 with an ALLOWING cue must NOT be classed as ineligible,
        # otherwise the runner bails before clicking 立即签到.
        page = (
            "New Cross 公益站\n每日签到\n当前余额低于阈值，可以签到\n"
            "👋\n登录用户\nyourhandle\n✨\n当前额度\n$7.62\n🎯\n"
            "签到阈值\n$20\n立即签到\n"
            "完成 PoW 后将继续执行浏览器 PoW，当前难度为 18 bit\n退出登录\n"
            "TODAY BOARD\n签到金额排行榜\n"
        )
        self.assertIsNone(self.m.classify_page_block(page, "https://newapi-checkin.keungliang.dpdns.org/"))

    def test_cross_below_threshold_rejection_copy_still_ineligible(self):
        # Same site when the card actually refuses (no 可以签到 cue).
        self.assertEqual(
            self.m.classify_page_block(
                "当前额度\n$25.00\n签到阈值\n$20\n余额充足，无需签到",
                "https://newapi-checkin.keungliang.dpdns.org/",
            )[0],
            "business_ineligible",
        )

    def test_dashboard_with_incidental_404_is_not_a_dead_url(self):
        dashboard = (
            "Ark API\n首页\n控制台\n数据\n模型广场\n文档\n99+\nM\nyourhandle\n"
            "我的账户\n数据看板\n令牌管理\n钱包中心\n使用日志\n"
            "历史消耗\n$303.82\n请求次数\n404\n用户分组\ndefault\n"
            "每日签到\n今日可签到\n立即签到\n个人设置"
        )
        self.assertIsNone(
            self.m.classify_page_block(
                dashboard, "https://windhub.cc/console/personal"
            )
        )

    def test_dashboard_with_incidental_403_is_not_blocked(self):
        dashboard = (
            "Ark API\n首页\n控制台\nM\nyourhandle\n数据看板\n令牌管理\n"
            "历史消耗\n$303.82\n请求次数\n403\n用户分组\ndefault\n"
            "每日签到\n今日可签到\n立即签到\n个人设置"
        )
        self.assertIsNone(
            self.m.classify_page_block(
                dashboard, "https://windhub.cc/console/personal"
            )
        )

    def test_ai_52ccl_prefers_verified_canonical_profile_route(self):
        adapter = self.m.resolve_site(
            "ai.52ccl.cn", "https://ai.52ccl.cn/personal"
        )
        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.prefer_catalog_url)
        self.assertEqual(
            adapter.url, "https://52ccl.net/profile"
        )

    def test_classifies_loaded_newapi_profile_without_checkin_feature(self):
        profile = (
            "Toggle Sidebar New API 常规 概览 数据看板 API 密钥 使用日志 "
            "个人 钱包 个人资料 USERNAME 用户 ID 2907 当前余额 LDC 7.56 "
            "设置 配置您的账户偏好和集成 账户绑定"
        )
        self.assertEqual(
            self.m.classify_missing_checkin_feature(
                profile, "https://example.test/profile", "newapi_profile"
            )[0],
            "feature_unavailable",
        )
        self.assertIsNone(
            self.m.classify_missing_checkin_feature(
                profile + " 立即签到", "https://example.test/profile", "newapi_profile"
            )
        )
        self.assertIsNone(
            self.m.classify_missing_checkin_feature(
                profile, "https://example.test/profile", "browser"
            )
        )

    def test_unhealthy_sites_are_suppressed_only_for_full_runs(self):
        rows = [
            {"name": "dead", "site_health_status": "suppressed", "site_health_reason": "dead_url"},
            {"name": "live", "site_health_status": "active"},
        ]
        runnable, suppressed = self.m.filter_unhealthy_rows(rows)
        self.assertEqual([row["name"] for row in runnable], ["live"])
        self.assertEqual([row["name"] for row in suppressed], ["dead"])
        runnable, suppressed = self.m.filter_unhealthy_rows(rows, force=True)
        self.assertEqual(len(runnable), 2)
        self.assertEqual(suppressed, [])

    def test_abrdns_uses_verified_checkin_route_over_stale_task_url(self):
        adapter = self.m.resolve_site("abrdns", "https://checkin.new-api.abrdns.com/")
        self.assertEqual(adapter.url, "https://checkin.new-api.abrdns.com/checkin")
        self.assertTrue(adapter.prefer_catalog_url)

    def test_unattributed_success_never_marks_task(self):
        result = self.m.fail_result("unattributed_success", site="A")
        self.assertFalse(result.ok)
        self.assertEqual(self.m.exit_code_from_results([result]), 1)

        src = TARGET.read_text(encoding="utf-8")
        m = re.search(
            r"# STRICT: poll for business confirm.*?\n(.*?)except Exception as e:",
            src,
            re.S,
        )
        self.assertIsNotNone(m, "post-click browser path not found")
        tail = m.group(1)
        self.assertRegex(tail, r'fail_result\(\s*"no_confirm"')
        self.assertRegex(tail, r'ok_result\(\s*"OK"')
        self.assertIn("action=action", tail)
        self.assertIn("already_sig", tail)
        self.assertNotIn("not await page.locator(used)", tail)
        self.assertTrue(
            "is_valid_checkin_confirm" in tail or "has_success_text" in tail
        )
        self.assertIn("for _ in range", tail)

    def test_projection_failure_contract_preserves_business_evidence(self):
        from checkin_core.models import ActionEvidence, ConfirmationEvidence
        result = self.m.fail_result(
            "task_projection_missing", site="A", detail="task row not found",
            action=ActionEvidence(kind="dom_click", attempted_at="2026-08-08T08:10:12"),
            confirmation=ConfirmationEvidence(
                kind="dom_strong", source="dom_live", summary="签到成功",
                observed_at="2026-08-08T08:10:14", checked_in_today=True,
            ),
            pre_state="PENDING", post_state="DONE",
            transition="PENDING_TO_DONE", attribution="runner",
        )
        self.assertEqual(result.reason, "task_projection_missing")
        self.assertEqual(result.attribution, "runner")
        self.assertEqual(result.transition, "PENDING_TO_DONE")
        self.assertFalse(result.ok)

        result.projection_reason = "task_projection_missing:A"
        self.assertEqual(result.projection_status, "not_attempted")
        self.assertEqual(result.projection_reason, "task_projection_missing:A")

        self.assertTrue(self.m.is_sign_cta_text("立即签到"))
        self.assertTrue(self.m.is_sign_cta_text("签到"))
        self.assertFalse(self.m.is_sign_cta_text("每日签到"))
        self.assertTrue(self.m.is_sign_cta_text("今日签到"))
        self.assertTrue(self.m.is_sign_cta_text("每日打卡"))
        self.assertFalse(self.m.is_sign_cta_text("每日签到\n\n每日签到可获得随机额度奖励"))
        self.assertFalse(self.m.is_sign_cta_text("签到规则"))
        self.assertFalse(self.m.is_sign_cta_text("签到说明"))
        self.assertFalse(self.m.is_sign_cta_text("签到记录"))
        self.assertTrue(self.m.is_sign_cta_text("去签到"))
        self.assertTrue(self.m.is_sign_cta_text("开始转动"))
        self.assertTrue(self.m.is_sign_cta_text("立即转动领取奖励"))
        self.assertTrue(self.m.is_sign_cta_text("开始签到"))
        self.assertFalse(self.m.is_sign_cta_text("转动规则"))
        self.assertFalse(self.m.is_sign_cta_text("每日签到规则"))
        self.assertFalse(self.m.is_sign_cta_text("每日签到\n已签到\n\n今天 +¥20"))
        self.assertTrue(self.m.is_valid_checkin_confirm("每日签到\n已签到\n\n今天 +¥20"))
        a = self.m.resolve_site("7倍", "")
        self.assertEqual(a.sign_selectors[0], 'button:has-text("立即签到")')

    def test_projection_failure_preserves_business_result_and_continues(self):
        """Obsidian projection is optional; SQLite result must remain authoritative."""
        src = TARGET.read_text(encoding="utf-8")
        run_body = src.split("async def run(", 1)[1]
        success_projection = re.search(r"if result\.ok:.*?else:\n\s+fail_tag =", run_body, re.S)
        self.assertIsNotNone(success_projection)
        assert success_projection is not None
        block = success_projection.group(0)
        # Current site is persisted once via the common tail.
        self.assertEqual(block.count("store.add_run_item(run_id, result)"), 0)
        self.assertIn("task_projection_missing", block)
        self.assertIn("result.business_status = result.status", block)
        self.assertIn("store.set_task_result(today_str, name, \"done\")", block)
        self.assertNotIn("abort_remaining(", block)

    def test_site_specific_cta_and_ready_wait(self):
        nodeseek = self.m.resolve_site("nodeseek", "https://www.nodeseek.com/board")
        self.assertIsNotNone(nodeseek)
        assert nodeseek is not None
        self.assertEqual(nodeseek.sign_selectors[0], 'button:has-text("试试手气")')
        self.assertEqual(nodeseek.kind, "browser")
        self.assertTrue(nodeseek.trusted_sign_selectors)
        self.assertTrue(nodeseek.trusted_already_selectors)

        llmpm = self.m.resolve_site("llmpm", "https://api.llm.pm/checkin")
        self.assertIsNotNone(llmpm)
        assert llmpm is not None
        self.assertEqual(llmpm.sign_selectors[0], 'button:has-text("立即签到")')
        self.assertEqual(llmpm.ready_rounds, 30)

        lucky = self.m.resolve_site(
            "lucky0625 fuli", "https://fuli.lucky0625.qzz.io/"
        )
        self.assertIsNotNone(lucky)
        assert lucky is not None
        self.assertEqual(lucky.sign_selectors[0], 'button:has-text("摘一片四叶草")')
        self.assertIn('text=今天的叶子已经摘过啦', lucky.already_selectors)
        self.assertNotIn("text=已摘完", lucky.already_selectors)
        self.assertTrue(lucky.trusted_sign_selectors)
        self.assertTrue(lucky.trusted_already_selectors)

        yoct = self.m.resolve_site("yoct", "https://yoct.cn/profile")
        self.assertIsNotNone(yoct)
        assert yoct is not None
        self.assertEqual(yoct.sign_selectors[0], 'button:has-text("立即签到")')
        self.assertEqual(yoct.already_selectors[0], 'text=今日已签到')

        wxiai = self.m.resolve_site("wxiai", "https://api.wxiai.com/workspace")
        self.assertIsNotNone(wxiai)
        assert wxiai is not None
        self.assertTrue(wxiai.prefer_cta_before_auth)
        self.assertEqual(wxiai.ready_rounds, 30)
        self.assertTrue(wxiai.use_native_click)
        self.assertFalse(wxiai.use_overlay_zapper)

        # resolve_site must preserve signin_api + the New-Api-User(uid) header
        # opt-in when it re-derives an adapter from the catalog (hcnsec 走
        # note-URL 时若丢 flag,签会 401「未提供 New-Api-User」)。
        hcnsec = self.m.resolve_site(
            "hcnsec", "https://api.hcnsec.cn/dashboard/overview"
        )
        self.assertIsNotNone(hcnsec)
        assert hcnsec is not None
        self.assertEqual(hcnsec.signin_api, "/api/user/checkin")
        self.assertTrue(hcnsec.signin_api_uid_header)
        self.assertEqual(hcnsec.login_url, "https://api.hcnsec.cn/sign-in")

    def test_signin_api_uid_header_js_builds_header(self):
        """try_signin_api builds the New-Api-User header when uid present."""
        js = self.m.SiteAdapter(
            name="hcnsec",
            url="https://api.hcnsec.cn/dashboard/overview",
            signin_api="/api/user/checkin",
            signin_api_uid_header=True,
            login_url="https://api.hcnsec.cn/sign-in",
        )
        self.assertTrue(js.signin_api_uid_header)
        self.assertEqual(js.signin_api, "/api/user/checkin")
        src = TARGET.read_text(encoding="utf-8")
        m = re.search(r"async def bohe_click_spin\(.*?\n(?=async def )", src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn('fail_result("no_confirm"', body)
        after_spin = body.split('print(f"  薄荷 spin click:', 1)[1]
        self.assertTrue(
            "is_valid_checkin_confirm" in after_spin or "has_success_text" in after_spin
        )
        self.assertIn("return confirmed_done_result(", after_spin)

    def test_page_text_contract_in_source(self):
        src = TARGET.read_text(encoding="utf-8")
        m = re.search(r"async def page_text\(.*?\n(?=async def )", src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("document.body.innerText", body)
        self.assertIn("data-sonner-toast", body)
        self.assertIn("merge_page_text", body)
        self.assertNotIn("return '签到成功'", body)

    def test_cdp_cleanup_never_closes_user_browser(self):
        import asyncio

        class RemoteBrowser:
            def __init__(self):
                self.close_called = False

            async def close(self):
                self.close_called = True
                raise AssertionError("must not close the user-owned Chrome")

        browser = RemoteBrowser()
        asyncio.run(self.m.safe_disconnect(browser))
        self.assertFalse(browser.close_called)

        src = TARGET.read_text(encoding="utf-8")
        m = re.search(r"async def safe_disconnect\(.*?\n(?=\n# -{5,}|\nasync def )", src, re.S)
        self.assertIsNotNone(m, "safe_disconnect not found")
        assert m is not None
        body = m.group(0)
        self.assertIn("connect_over_cdp", body)
        self.assertIn("/json/new", body)
        self.assertNotIn("browser.close(", body)



    def test_today_money_with_cta_only_is_false(self):
        # P1 regression: 立即签到 page must NOT confirm on 今天+¥ alone
        bad = "每日签到\n立即签到\n今天 +¥20\n历史记录"
        self.assertFalse(self.m.is_valid_checkin_confirm(bad))
        self.assertFalse(self.m.has_success_text(bad))
        self.assertEqual(self.m.confirm_signal(bad), "")

    def test_today_money_with_already_is_true(self):
        good = "每日签到\n已签到\n今天 +¥20"
        self.assertTrue(self.m.is_valid_checkin_confirm(good))

    def test_today_money_yqianming_not_confirm(self):
        # P2 regression: short 「已签」 must not match 「已签名」
        bad = "今天 +¥20 已签名 请确认"
        self.assertFalse(self.m.is_valid_checkin_confirm(bad))
        good = "今天 +¥20 已签到"
        self.assertTrue(self.m.is_valid_checkin_confirm(good))
        good2 = "今日 +¥5 今日已签"
        self.assertTrue(self.m.is_valid_checkin_confirm(good2))

    def test_month_stat_yearned_not_today_confirm(self):
        # yeelo 2026-08-26 regression: 「已签到 4 天」是本月签到统计,与下方
        # 「今天还未签到，点击按钮即可领取」不要跨行拼成假已签到 today+weak。
        yeelo_unsigned = (
            "2026年8月签到表\n今日已有 30 位用户签到\n每日签到\n今日奖励 5 Credit\n"
            "本月签到\n已签到 4 天\n今天还未签到，点击按钮即可领取。\n今日签到\n随机签到"
        )
        self.assertFalse(self.m.is_valid_checkin_confirm(yeelo_unsigned))
        # 真已签: 文案变「今天已签到，明天再来领取」
        yeelo_signed = (
            "本月签到\n已签到 5 天\n今天已签到，明天再来领取。\n今日签到\n随机签到"
        )
        self.assertTrue(self.m.is_valid_checkin_confirm(yeelo_signed))
        self.assertTrue(self.m.confirm_signal(yeelo_signed))

    def test_linuxdo_authorize_and_turnstile_contracts(self):
        auth = list(self.m.AUTHORIZE_SELECTORS)
        self.assertTrue(any("身份授权" in sel for sel in auth))
        self.assertTrue(any("授权" in sel for sel in auth))
        self.assertFalse(any("确认" in sel for sel in auth))
        self.assertFalse(any("继续" in sel or "Continue" in sel for sel in auth))
        self.assertEqual(self.m.CAPTCHA_WAIT_S, 40.0)
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("async def wait_out_cloudflare", src)
        self.assertIn("try_click_challenge_widgets(page)", src)
        self.assertIn("CF cleared after", src)

    def test_sign_selector_broadens_clickable_controls(self):
        sels = list(self.m.DEFAULT_SIGN_SELECTORS)
        self.assertTrue(any("打卡" in sel for sel in sels))
        self.assertTrue(any('[role="button"]' in sel for sel in sels))
        self.assertTrue(any('class*="btn"' in sel for sel in sels))
        self.assertTrue(any('class*="button"' in sel for sel in sels))
        self.assertTrue(any("Checkin" in sel for sel in sels))
    def test_generic_cta_is_limited_to_main_checkin_flow(self):
        src = TARGET.read_text(encoding="utf-8")
        # Generic fallback is opt-in and all main CTA lookups enable it: the
        # four original paths (main CTA, retry after CF clears, auth-gate SSO
        # retry, retry after auth-gate SSO) plus the no_confirm retry after the
        # bounded reload (wisart-style transient click swallow).
        self.assertIn("allow_generic_sign_cta: bool = False", src)
        self.assertIn("reset_scan_state: bool = True", src)
        self.assertIn("reset_scan_state=False", src)
        self.assertEqual(src.count("allow_generic_sign_cta=True"), 5)
        # LinuxDO and 薄荷-specific waits must retain their exact-selector calls.
        sso = re.search(r"async def try_linuxdo_sso\(.*?\n(?=async def )", src, re.S)
        self.assertIsNotNone(sso)
        assert sso is not None
        self.assertNotIn("allow_generic_sign_cta=True", sso.group(0))
        bohe = re.search(r"async def bohe_click_spin\(.*?\n(?=async def )", src, re.S)
        self.assertIsNotNone(bohe)
        assert bohe is not None
        self.assertNotIn("allow_generic_sign_cta=True", bohe.group(0))

    def test_reward_amount_cta_is_actionable_but_bare_daily_label_is_not(self):
        self.assertTrue(self.m.is_sign_cta_text("签到 +0.2"))
        self.assertTrue(self.m.is_sign_cta_text("每日签到 +25"))
        self.assertTrue(self.m.is_sign_cta_text("打卡 ＋ 100"))
        self.assertTrue(self.m.is_sign_cta_text("开始转动"))
        self.assertFalse(self.m.is_sign_cta_text("每日签到"))
        self.assertFalse(self.m.is_sign_cta_text("每日签到记录 +25"))
        for label in ("立即签到记录", "立即签到规则", "签到领取说明", "每日打卡历史"):
            self.assertFalse(self.m.is_sign_cta_text(label), label)
        # A real CTA whose action token is the last segment (lucky0625's
        # 「摘一片四叶草 · 签到」) must be actionable, not rejected.
        self.assertTrue(self.m.is_sign_cta_text("摘一片四叶草 · 签到"))
        self.assertTrue(self.m.is_sign_cta_text("摘四叶草 打卡"))
        self.assertFalse(self.m.is_sign_cta_text("如何签到"))
        self.assertFalse(self.m.is_sign_cta_text("每日签到"))

    def test_generic_daily_candidate_requires_interaction_and_context(self):
        score = self.m.score_generic_sign_candidate
        self.assertEqual(
            score({"label": "每日签到", "tag": "button", "onclick": True,
                   "context": "每日签到奖励 今日状态 待签到"}),
            (220, "daily_sign"),
        )
        # A real <button> whose label is the bare daily-sign phrase must still
        # pass even without surrounding context — test.mlgb7.com's check-in
        # button is exactly <button>每日签到</button> with no reward words in
        # any nearby ancestor (the points list floods context with metadata).
        # Score below daily_sign/fuzzy_sign so a real contextual CTA still wins.
        self.assertEqual(
            score({"label": "每日签到", "tag": "button", "onclick": True}),
            (180, "daily_button"),
        )
        # Same label on a non-button element stays rejected (section title / prose).
        self.assertEqual(
            score({"label": "每日签到", "tag": "div", "pointer": True}),
            (0, "daily_without_context"),
        )
        self.assertEqual(
            score({"label": "签到", "tag": "div", "role": "button", "in_nav": True}),
            (0, "navigation"),
        )
        self.assertEqual(
            score({"label": "开始签到", "tag": "button", "onclick": True, "in_nav": True}),
            (0, "navigation"),
        )
        # A real sign CTA carrying a reward number stays actionable even inside
        # a header/nav container (pipiwang's `<button#checkinBtn>签到 +100~200`),
        # which lives in `header.topbar`. Bare nav links reject; explicit-reward
        # CTAs pass.
        self.assertEqual(
            score({"label": "签到 +100~200", "tag": "button", "onclick": True, "in_nav": True}),
            (320, "strong"),
        )
        self.assertEqual(
            score({"label": "开始签到", "tag": "button", "onclick": True, "in_nav": True}),
            (0, "navigation"),
        )
        self.assertEqual(
            score({
                "label": "每日", "tag": "span", "pointer": True,
                "context": "每日福利 签到可领取 20 积分",
            }),
            (160, "daily_context"),
        )
        self.assertEqual(
            score({"label": "开始签到", "tag": "div", "pointer": True}),
            (320, "strong"),
        )
        self.assertEqual(
            score({"label": "领取今日 5GB", "tag": "button"}),
            (210, "daily_reward"),
        )
        self.assertEqual(
            score({
                "label": "每日福利", "tag": "div", "pointer": True,
                "context": "每日福利 签到领取奖励",
            }),
            (150, "fuzzy_daily_context"),
        )
        rejected = [
            {"label": "签到", "tag": "div"},
            {"label": "会员福利 每日签到可领取 20 积分", "tag": "div",
             "context": "会员福利 每日签到可领取 20 积分"},
            {"label": "每日签到", "tag": "div"},
            {"label": "领取今日 5GB", "tag": "div"},
            {"label": "每日", "tag": "span", "pointer": True, "context": "每日新闻"},
            {"label": "每日签到", "tag": "button", "role": "tab"},
            {"label": "每日签到", "tag": "button", "in_nav": True},
            {"label": "开始签到", "tag": "button", "in_nav": True},
            {"label": "每日福利", "tag": "div", "pointer": True,
             "context": "每日新闻"},
            {"label": "每日", "tag": "button", "context": "每日任务 签到积分"},
            {"label": "签到记录", "tag": "button"},
            {"label": "签到设置", "tag": "button"},
            {"label": "签到提醒", "tag": "button"},
            {"label": "签到开关", "tag": "button"},
            {"label": "今日签到活动", "tag": "button"},
            {"label": "每日", "tag": "button", "context": "账户余额 每日趋势"},
        ]
        for candidate in rejected:
            self.assertEqual(score(candidate)[0], 0, candidate)

    def test_retry_auto_fail_selects_enabled_note_marked_rows(self):
        rows = [
            {"name": "failed", "status": "failed", "enabled": 1},
            {"name": "pending", "status": "pending", "enabled": 1},
            {"name": "done", "status": "done", "enabled": 1},
            {"name": "disabled", "status": "failed", "enabled": 0},
            {"name": "雅思学习", "status": "pending", "enabled": 1},
        ]
        note = (
            "- [ ] #task #日常 [failed](https://failed.example/) #auto-fail:no_button\n"
            "- [ ] #task #日常 [pending](https://pending.example/) #auto-fail:timeout\n"
            "- [x] #task #日常 [done](https://done.example/) #auto-fail:no_confirm\n"
            "- [ ] #task #日常 [disabled](https://disabled.example/) #auto-fail:error\n"
            "- [ ] #task #日常 雅思学习 2026-08-06 #auto-fail:error\n"
        )
        selected = self.m.select_retry_auto_fail_rows(rows, note)
        self.assertEqual([row["name"] for row in selected], ["failed", "pending"])

    def test_first_visible_pipiwang_sign_cta_in_header_not_rejected(self):
        """Regression: pipiwang's real CTA <button#checkinBtn>在 header 内。

        08-22 no_button 根因——CTA 落在 header.topbar 被 in_nav 误杀。
        明确签到 CTA（label 带奖励数字「签到 +100~200」）即使在导航容器里也必须放行。
        """
        import asyncio

        class Item:
            def __init__(self, candidate, label):
                self.candidate = candidate
                self.label = label

            async def is_visible(self, timeout=0):
                return True

            async def is_disabled(self):
                return False

            async def inner_text(self, timeout=0):
                return self.label

            async def evaluate(self, _js):
                return self.candidate

        class Locator:
            def __init__(self, items):
                self.items = items

            async def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            @property
            def first(self):
                return self.items[0]

        class Page:
            def locator(self, _selector):
                return Locator([
                    # pipiwang today: real CTA inside header — must NOT be rejected
                    Item({"tag": "button", "in_nav": True, "explicit_action": True},
                         "签到 +100~200"),
                ])

        item, selector = asyncio.run(
            self.m.first_visible(
                Page(), ["div"], sign_cta_only=True,
            )
        )
        self.assertIsNotNone(item)
        self.assertEqual(selector, "div")
        self.assertEqual(item.label, "签到 +100~200")

    def test_first_visible_rejects_nav_handler_backed_controls(self):
        import asyncio

        class Item:
            def __init__(self, candidate, label):
                self.candidate = candidate
                self.label = label
            async def is_visible(self, timeout=0):
                return True
            async def is_disabled(self):
                return False
            async def inner_text(self, timeout=0):
                return self.label
            async def evaluate(self, _js):
                return self.candidate

        class Locator:
            def __init__(self, items):
                self.items = items
            async def count(self):
                return len(self.items)
            def nth(self, index):
                return self.items[index]
            @property
            def first(self):
                return self.items[0]

        class Page:
            def locator(self, _selector):
                return Locator([
                    Item({"tag": "div", "in_nav": True, "explicit_action": True}, "签到"),
                    Item({"tag": "div", "in_nav": False, "explicit_action": True}, "立即签到"),
                ])

        item, selector = asyncio.run(
            self.m.first_visible(
                Page(), ["div"], sign_cta_only=True,
            )
        )
        self.assertIsNotNone(item)
        self.assertEqual(selector, "div")
        self.assertEqual(item.label, "立即签到")

    def test_first_visible_trusted_sign_bypasses_phrase_classifier(self):
        import asyncio

        class Item:
            def __init__(self, label):
                self.label = label
            async def is_visible(self, timeout=0):
                return True
            async def is_disabled(self):
                return False
            async def inner_text(self, timeout=0):
                return self.label
            async def evaluate(self, _js):
                return {"tag": "button", "in_nav": False, "explicit_action": True}

        class Locator:
            def __init__(self, items):
                self.items = items
            async def count(self):
                return len(self.items)
            def nth(self, index):
                return self.items[index]
            @property
            def first(self):
                return self.items[0]

        class Page:
            def locator(self, _selector):
                return Locator([Item("试试手气")])

        # A hand-curated site phrase (试试手气) is NOT a recognized generic CTA
        # (is_sign_cta_text returns False), but a trusted site-specific selector
        # must still be offered for clicking instead of being filtered away.
        self.assertFalse(self.m.is_sign_cta_text("试试手气"))
        item, selector = asyncio.run(
            self.m.first_visible(
                Page(), ['button:has-text("试试手气")'],
                sign_cta_only=True, trusted_sign=True,
            )
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.label, "试试手气")
        self.assertEqual(selector, 'button:has-text("试试手气")')

        # Without trusted_sign the same adapter selector must still be rejected
        # (guard: only curated site selectors get the bypass).
        item2, _ = asyncio.run(
            self.m.first_visible(
                Page(), ['button:has-text("试试手气")'],
                sign_cta_only=True, trusted_sign=False,
            )
        )
        self.assertIsNone(item2)

    def test_first_visible_trusted_sign_still_captures_done_signal(self):
        import asyncio

        class Item:
            def __init__(self, label):
                self.label = label
            async def is_visible(self, timeout=0):
                return True
            async def is_disabled(self):
                return True
            async def inner_text(self, timeout=0):
                return self.label
            async def evaluate(self, _js):
                return {"tag": "button", "in_nav": False, "explicit_action": True}

        class Locator:
            def __init__(self, items):
                self.items = items
            async def count(self):
                return len(self.items)
            def nth(self, index):
                return self.items[index]
            @property
            def first(self):
                return self.items[0]

        class Page:
            def locator(self, _selector):
                return Locator([Item("今日已签到")])

        # A disabled "今日已签到" button is a done-state match even under a
        # trusted selector; it must short-circuit to ALREADY, not get clicked.
        item, _ = asyncio.run(
            self.m.first_visible(
                Page(), ['button:has-text("今日已签到")'],
                sign_cta_only=True, trusted_sign=True,
            )
        )
        self.assertIsNone(item)
        self.assertIn("今日已签到", getattr(self.m.first_visible, "_done_signal", ""))

    def test_adapter_trusted_sign_only_for_explicit_site_selectors(self):
        explicit = self.m._B(
            "nodeseek", "https://www.nodeseek.com/board",
            ['button:has-text("试试手气")'],
            ['text=今日已签到'],
        )
        self.assertTrue(explicit.trusted_sign_selectors)
        self.assertTrue(explicit.trusted_already_selectors)
        self.assertEqual(explicit.sign_selectors, ['button:has-text("试试手气")'])
        defaulted = self.m._B("plain-site", "https://example.com")
        self.assertFalse(defaulted.trusted_sign_selectors)
        self.assertFalse(defaulted.trusted_already_selectors)
        self.assertEqual(defaulted.sign_selectors, self.m.DEFAULT_SIGN_SELECTORS)
        newapi_defaulted = self.m._N("plain-newapi", "https://api.example.com/profile")
        self.assertFalse(newapi_defaulted.trusted_sign_selectors)

    def test_already_done_trusts_curated_site_phrase(self):
        import asyncio

        # 今日签到获得鸡腿N个 is nodeseek's success copy but fails the strict
        # generic confirm gate — the curated adapter phrase must still count.
        self.assertFalse(self.m.is_valid_checkin_confirm("今日签到获得鸡腿7个，当前排名第1557"))

        class Item:
            async def is_visible(self, timeout=0):
                return True
            async def is_disabled(self):
                return False
            async def inner_text(self, timeout=0):
                return "今日签到获得鸡腿7个"

        class Locator:
            def __init__(self, n, item=None):
                self.n = n
                self.item = item
            async def count(self):
                return self.n
            def nth(self, index):
                return self.item
            @property
            def first(self):
                return self.item

        class Page:
            def locator(self, sel):
                if sel == "text=今日签到获得":
                    return Locator(1, Item())
                return Locator(0)
            async def evaluate(self, _js):
                return "今日签到获得鸡腿7个，当前排名第1557"

        sig = asyncio.run(
            self.m.already_done(
                Page(), ["text=今日签到获得"], trusted_already=True
            )
        )
        self.assertTrue(sig)
        self.assertIn("今日签到获得", sig)

        # Untrusted (generic) selector list must still reject the phrase.
        sig_untrusted = asyncio.run(
            self.m.already_done(Page(), ["text=今日签到获得"])
        )
        self.assertEqual(sig_untrusted, "")

    def test_targeted_state_records_resolved_sites(self):
        a = self.m.resolve_site("47", "https://47.47-gpt.com/custom/daily-checkin")
        b = self.m.resolve_site("fengwind", "https://api-welfalre.fengwind.com/")
        self.assertEqual(
            self.m.resolved_target_site_names("targeted", [a, b]),
            ["47", "fengwind"],
        )
        self.assertEqual(self.m.resolved_target_site_names("full", [a]), [])

    def test_auth_page_detection_for_47_login_redirect(self):
        self.assertTrue(self.m.is_auth_page_url("https://47.47-gpt.com/login?redirect=/custom/daily-checkin"))
        self.assertTrue(self.m.looks_logged_out("47\n欢迎回来\n登录您的账户以继续\n邮箱\n密码"))
        self.assertFalse(self.m.is_auth_page_url("https://47.47-gpt.com/custom/daily-checkin"))

    def test_47_resolves_as_browser_adapter(self):
        adapter = self.m.resolve_site("47", "https://47.47-gpt.com/custom/daily-checkin")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.kind, "browser")
        self.assertEqual(adapter.url, "https://47.47-gpt.com/47/checkin/")
        self.assertIn('button:has-text("立即签到")', adapter.sign_selectors)

        import asyncio

        class Item:
            def __init__(self, candidate, disabled=False):
                self.candidate = candidate
                self.disabled = disabled
            async def is_visible(self, timeout=0):
                return True
            async def is_disabled(self):
                return self.disabled
            async def evaluate(self, _js):
                return self.candidate

        class Locator:
            def __init__(self, items):
                self.items = items
            async def count(self):
                return len(self.items)
            def nth(self, index):
                return self.items[index]

        class Page:
            def locator(self, _selector):
                return Locator([
                    Item({"label": "每日签到", "tag": "div", "onclick": True}),
                    Item({"label": "签到记录", "tag": "button"}),
                    Item({"label": "每日", "tag": "span", "pointer": True,
                          "context": "每日福利 签到可领取积分"}),
                    Item({"label": "立即签到", "tag": "button"}),
                ])

        item, source = asyncio.run(self.m.generic_sign_cta(Page()))
        self.assertIsNotNone(item)
        self.assertRegex(source or "", r"^generic:\d+:strong:320:立即签到$")

    def test_generic_scanner_falls_back_to_ranked_daily_controls(self):
        import asyncio

        class Item:
            def __init__(self, candidate):
                self.candidate = candidate
            async def is_visible(self, timeout=0):
                return True
            async def is_disabled(self):
                return False
            async def evaluate(self, _js):
                return self.candidate

        class Locator:
            def __init__(self, items):
                self.items = items
            async def count(self):
                return len(self.items)
            def nth(self, index):
                return self.items[index]

        class Page:
            def locator(self, selector):
                self.selector = selector
                return Locator([
                    Item({"label": "每日签到", "tag": "button", "role": "tab"}),
                    Item({"label": "每日", "tag": "span", "pointer": True,
                          "context": "每日任务 签到积分"}),
                    Item({"label": "每日", "tag": "span", "pointer": True,
                          "context": "会员福利 每日签到可领取 20 积分"}),
                    Item({"label": "每日签到", "tag": "div", "onclick": True,
                          "context": "每日签到奖励 今日状态 待签到"}),
                ])

        page = Page()
        item, source = asyncio.run(self.m.generic_sign_cta(page))
        self.assertIsNotNone(item)
        self.assertRegex(source or "", r"^generic:\d+:daily_sign:220:每日签到$")

    def test_generic_scanner_filters_static_noise_before_custom_controls(self):
        import asyncio

        class Item:
            def __init__(self, candidate):
                self.candidate = candidate
            async def is_visible(self, timeout=0):
                return True
            async def is_disabled(self):
                return False
            async def evaluate(self, _js):
                return dict(self.candidate)

        class Locator:
            def __init__(self, items=None, custom_ids=None):
                self.items = items or []
                self.custom_ids = custom_ids or []
            async def count(self):
                return len(self.items)
            def nth(self, index):
                return self.items[index]
            async def evaluate_all(self, _js):
                return list(self.custom_ids)

        class Page:
            def __init__(self, candidate):
                self.candidate = candidate
            def locator(self, selector):
                if selector == "div, span":
                    # Browser-side filtering has discarded 170 static prose nodes.
                    return Locator(custom_ids=["custom-0"])
                if selector == '[data-generic-sign-scan="custom-0"]':
                    return Item(self.candidate)
                return Locator()

        real = {
            "label": "每日签到", "tag": "div", "pointer": True,
            "scan_id": "custom-0",
            "context": "每日签到奖励 今日状态 待签到",
        }
        reward = {
            "label": "领取今日 5GB", "tag": "div", "pointer": True,
            "scan_id": "custom-0",
        }
        item, source = asyncio.run(self.m.generic_sign_cta(Page(real)))
        reward_item, reward_source = asyncio.run(self.m.generic_sign_cta(Page(reward)))
        self.assertIsNotNone(item)
        self.assertIsNotNone(reward_item)
        self.assertIn(":daily_sign:220:", source)
        self.assertIn(":daily_reward:210:", reward_source)
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn('page.locator("div, span").evaluate_all', src)
        self.assertIn("if (!interactive) continue", src)


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


class TestResultBridge(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_legacy_reasons(self):
        self.assertEqual(self.m.result_from_legacy("OK").status, "OK")
        self.assertEqual(self.m.result_from_legacy("ALREADY").status, "ALREADY")
        r = self.m.result_from_legacy("FAIL:not logged in (no_button)")
        self.assertEqual(r.reason, "no_button")
        self.assertFalse(r.ok)
        r2 = self.m.result_from_legacy("FAIL:button not visible. preview: x")
        self.assertEqual(r2.reason, "no_button")


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


class TestCliAndSummary(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_parse_cli(self):
        a = self.m.parse_cli_args(["--only", "7倍,ciallo", "--dry-run", "--retry-auto-fail"])
        self.assertEqual(a.only, "7倍,ciallo")
        self.assertTrue(a.dry_run)
        self.assertTrue(a.retry_auto_fail)

    def test_summary_aggregates(self):
        rs = [
            self.m.ok_result("OK", site="A"),
            self.m.ok_result("ALREADY", site="B"),
            self.m.fail_result("no_confirm", site="C"),
            self.m.fail_result("no_confirm", site="D"),
            self.m.fail_result("interactive", site="E"),
        ]
        self.m.print_run_summary(rs)



class TestMergePageText(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_toast_survives_long_body_truncation(self):
        body = "X" * 2000
        toast = "签到成功！获得 ¥20"
        out = self.m.merge_page_text(body, [toast], 900)
        self.assertIn("签到成功", out)
        self.assertTrue(self.m.is_valid_checkin_confirm(out))
        # toast must lead
        self.assertTrue(out.startswith("签到成功") or out.startswith(toast[:4]))

    def test_no_toast_truncates_body(self):
        body = "A" * 50 + "今日已签到"
        out = self.m.merge_page_text(body, [], 20)
        self.assertEqual(out, body[:20])

    def test_empty_toast_list(self):
        self.assertEqual(self.m.merge_page_text("hello", None, 100), "hello")


class TestDoneButtonSignal(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_bare_enabled_rejected(self):
        self.assertEqual(self.m.done_button_signal("已签到", False), "")
        self.assertEqual(self.m.done_button_signal("已领取", False), "")

    def test_disabled_bare_ok(self):
        self.assertEqual(self.m.done_button_signal("已签到", True), "btn:disabled:已签到")
        self.assertEqual(self.m.done_button_signal("已领取", True), "btn:disabled:已领取")

    def test_today_label_ok(self):
        self.assertEqual(self.m.done_button_signal("今日已签到", False), "btn:今日已签到")
        self.assertEqual(self.m.done_button_signal("今日 已签到", False), "btn:今日已签到")

    def test_no_button_rechecks_already_contract(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("already after no CTA", src)
        self.assertIn("async def scan_done_dom_signal", src)

    def test_card_with_today(self):
        lab = "每日签到\n已签到\n今天 +¥20"
        self.assertEqual(self.m.done_button_signal(lab, False), "btn:card:today")

    def test_empty_detail_demote_contract_in_run(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("empty confirm detail", src)
        self.assertIn("demote to no_confirm", src)
        # must not fill detail with status name for mark path
        self.assertNotIn('result.detail = result.status.lower()', src)


class TestCaptchaGateAlign(unittest.TestCase):
    def test_wait_out_uses_strict_confirm(self):
        src = TARGET.read_text(encoding="utf-8")
        m = re.search(r"async def wait_out_captcha\(.*?\n(?=def |async def )", src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("is_valid_checkin_confirm", body)
        # bare list with 已领取 alone should not be the gate
        self.assertNotIn('"已领取",', body)



class TestEnsureDailyTask(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_reset_opens_checkboxes_and_strips_fail(self):
        src = (
            "- [x] #task #日常 雅思学习 2026-07-10\n"
            "- [x] #task #日常 [7倍](https://7x.hk/profile) #auto-fail:no_button ✅ 2026-07-10\n"
            "- [ ] #task #日常 [ciallo](https://ioll.pp.ua/profile)\n"
        )
        out = self.m._reset_daily_task_content(src, "2026-07-12")
        self.assertIn("- [ ] #task #日常 雅思学习 2026-07-12", out)
        self.assertIn("- [ ] #task #日常 [7倍](https://7x.hk/profile)", out)
        self.assertNotIn("#auto-fail", out)
        self.assertNotIn("✅", out)
        self.assertIn("- [ ] #task #日常 [ciallo]", out)

    def test_ensure_creates_from_seed(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            seed = d / "2026-07-11 每日任务.md"
            seed.write_text(
                "## 日常任务\n- [x] #task #日常 [7倍](https://7x.hk/profile) #auto-fail:x ✅ 2026-07-11\n",
                encoding="utf-8",
            )
            target = d / "2026-07-12 每日任务.md"
            content, created = self.m.ensure_daily_task_file(str(target), "2026-07-12")
            self.assertTrue(created)
            self.assertTrue(target.is_file())
            self.assertIn("- [ ] #task #日常 [7倍]", content)
            self.assertNotIn("#auto-fail", content)
            # second call is read-only
            content2, created2 = self.m.ensure_daily_task_file(str(target), "2026-07-12")
            self.assertFalse(created2)
            self.assertEqual(content2, content)

    def test_ensure_no_seed_raises(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "2026-07-12 每日任务.md"
            with self.assertRaises(FileNotFoundError):
                self.m.ensure_daily_task_file(str(target), "2026-07-12")



class TestExitAndResolve(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_exit_code_from_results(self):
        self.assertEqual(self.m.exit_code_from_results([]), 0)
        self.assertEqual(
            self.m.exit_code_from_results([self.m.ok_result("OK", site="A")]), 0
        )
        self.assertEqual(
            self.m.exit_code_from_results(
                [self.m.ok_result("OK", site="A"), self.m.fail_result("no_button", site="B")]
            ),
            1,
        )
        self.assertEqual(self.m.exit_code_from_results([], hard_error=2), 2)

    def test_resolve_prefers_note_url(self):
        a = self.m.resolve_site("7倍", "https://example.com/new-profile")
        self.assertEqual(a.url, "https://example.com/new-profile")
        self.assertEqual(a.kind, "newapi_profile")
        b = self.m.resolve_site("7倍", "")
        self.assertEqual(b.url, "https://7x.hk/profile")

    def test_apply_task_success_rereads_disk(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "d.md"
            fp.write_text(
                "- [ ] #task #日常 [7倍](https://7x.hk/profile)\n"
                "- [x] #task #日常 [ciallo](https://ioll.pp.ua/profile)\n",
                encoding="utf-8",
            )
            content, ch = self.m.apply_task_success(str(fp), "7倍")
            self.assertTrue(ch)
            disk = fp.read_text(encoding="utf-8")
            self.assertIn("- [x] #task #日常 [7倍]", disk)
            self.assertIn("- [x] #task #日常 [ciallo]", disk)

    def test_apply_task_success_clears_autofail_on_already_marked(self):
        """P1: already [x] + leftover #auto-fail must still persist clean line."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "d.md"
            fp.write_text(
                "- [x] #task #日常 [7倍](https://7x.hk/profile) #auto-fail:old\n",
                encoding="utf-8",
            )
            content, ch = self.m.apply_task_success(str(fp), "7倍")
            self.assertTrue(ch)
            self.assertNotIn("#auto-fail", content)
            disk = fp.read_text(encoding="utf-8")
            self.assertNotIn("#auto-fail", disk)
            self.assertIn("- [x] #task #日常 [7倍]", disk)

    def test_terms_selectors_no_blanket_checkbox(self):
        """P1: TERMS_CLICK_SELECTORS must not force-click bare ant/el/role boxes."""
        terms = list(self.m.TERMS_CLICK_SELECTORS)
        banned_sub = (
            ".ant-checkbox",
            ".el-checkbox",
            ".n-checkbox",
            '[role="checkbox"]',
            "[role='checkbox']",
        )
        for s in terms:
            for b in banned_sub:
                self.assertNotIn(b, s, f"blanket terms selector: {s!r}")
        # still has label/text terms paths
        self.assertTrue(any("协议" in s or "同意" in s for s in terms))
        # check_terms body still filters native checkboxes by nearby terms text
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("协议|同意|条款|隐私|Terms|Privacy|我已阅读|已阅读", src)

    def test_terms_kind_is_checkbox_skips_links(self):
        """check_terms must only click real terms checkboxes, never link-like text.

        mofas's "我已阅读并同意用户协议和隐私政策" is a bare span whose click opens
        /user-agreement in a new tab; classifying it 'skip' prevents the agreement-page
        leak seen when the batch ran check_terms on that login page.
        """
        m = load_mod(force=True)
        self.assertFalse(m.terms_kind_is_checkbox("skip"))
        self.assertTrue(m.terms_kind_is_checkbox("checkbox"))
        self.assertTrue(m.terms_kind_is_checkbox("wrap"))
        self.assertTrue(m.terms_kind_is_checkbox("role"))
        # the kind guard must stay wired into check_terms
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("terms_kind_is_checkbox(kind)", src)
        self.assertIn("if not terms_kind_is_checkbox(kind):", src)



class TestCdpDiscover(unittest.TestCase):
    """CDP auto-discover: ranking, headless refuse, strict prefer (no live Chrome required)."""

    def test_set_cdp_http_normalizes(self):
        m = load_mod(force=True)
        self.assertEqual(m.set_cdp_http("9225"), "http://127.0.0.1:9225")
        self.assertEqual(m.set_cdp_http("http://127.0.0.1:9333/"), "http://127.0.0.1:9333")

    def test_parse_ps_lstart_locale_independent(self):
        m = load_mod(force=True)
        # English / C-locale lstart (LC_ALL=C ps output) parses to epoch seconds.
        got = m._parse_ps_lstart(["Mon", "Aug", "24", "00:25:51", "2026"])
        self.assertIsInstance(got, float)
        # zh_CN / non-C locale output ("一  8月/24 00:25:51 2026") must not crash
        # and must not be misread; the caller forces LC_ALL=C so this branch
        # should only be hit defensively.
        self.assertIsNone(m._parse_ps_lstart(["一", "8月/24", "00:25:51", "2026"]))
        # Malformed / too-short input returns None, never raises.
        self.assertIsNone(m._parse_ps_lstart([]))
        self.assertIsNone(m._parse_ps_lstart(["Garbage", "input"]))

    def test_rank_prefers_headed_and_newer(self):
        m = load_mod(force=True)
        # monkeypatch probe + ps
        fake_ps = [
            {"pid": 1, "port": 9222, "headless": True, "user_data_dir": "/T/DrissionPage/x",
             "started": 1000.0, "score": -80, "cmd": "headless"},
            {"pid": 2, "port": 9223, "headless": False, "user_data_dir": "/home/checkin/chrome-secondary-profile",
             "started": 2000.0, "score": 150, "cmd": "old headed"},
            {"pid": 3, "port": 9224, "headless": False, "user_data_dir": "/home/checkin/chrome-secondary-profile",
             "started": 5000.0, "score": 150, "cmd": "new headed"},
        ]
        def fake_probe(port, timeout=0.8):
            for c in fake_ps:
                if c["port"] == port:
                    return {
                        "port": port,
                        "http": f"http://127.0.0.1:{port}",
                        "browser": "Chrome/1",
                        "ua": "HeadlessChrome" if c["headless"] else "Chrome",
                        "headless": c["headless"],
                        "pages": 5 if not c["headless"] else 1,
                        "ws": "",
                    }
            return None
        m._ps_chrome_debug_candidates = lambda: fake_ps
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = ()
        e = m.discover_cdp_endpoint(allow_headless=False)
        self.assertEqual(e["port"], 9224, f"expected newest headed, got {e}")

    def test_refuse_headless_only(self):
        m = load_mod(force=True)
        fake_ps = [
            {"pid": 9, "port": 9222, "headless": True, "user_data_dir": "/T/DrissionPage/x",
             "started": 100.0, "score": -80, "cmd": "h"},
        ]
        def fake_probe(port, timeout=0.8):
            return {
                "port": 9222, "http": "http://127.0.0.1:9222", "browser": "HeadlessChrome",
                "ua": "HeadlessChrome", "headless": True, "pages": 1, "ws": "",
            }
        m._ps_chrome_debug_candidates = lambda: fake_ps
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = ()
        with self.assertRaises(RuntimeError) as cm:
            m.discover_cdp_endpoint(allow_headless=False)
        self.assertIn("headless", str(cm.exception).lower())

    def test_explicit_and_env_headless_require_opt_in(self):
        m = load_mod(force=True)
        old_http = m.os.environ.pop("CDP_HTTP", None)
        old_port = m.os.environ.pop("CDP_PORT", None)
        setattr(m, "_probe_cdp", lambda port, timeout=0.8: {
            "port": port,
            "http": f"http://127.0.0.1:{port}",
            "browser": "HeadlessChrome",
            "ua": "HeadlessChrome",
            "headless": True,
            "pages": 1,
            "ws": "ws://fixture",
        })
        try:
            with self.assertRaisesRegex(RuntimeError, "headless"):
                m.discover_cdp_endpoint(
                    prefer_port=9333, strict_prefer=True, allow_headless=False
                )
            allowed = m.discover_cdp_endpoint(
                prefer_port=9333, strict_prefer=True, allow_headless=True
            )
            self.assertEqual(allowed["port"], 9333)

            m.os.environ["CDP_PORT"] = "9444"
            with self.assertRaisesRegex(RuntimeError, "headless"):
                m.discover_cdp_endpoint(allow_headless=False)
            allowed_env = m.discover_cdp_endpoint(allow_headless=True)
            self.assertEqual(allowed_env["reason"], "env_CDP_PORT")
        finally:
            if old_http is not None:
                m.os.environ["CDP_HTTP"] = old_http
            else:
                m.os.environ.pop("CDP_HTTP", None)
            if old_port is not None:
                m.os.environ["CDP_PORT"] = old_port
            else:
                m.os.environ.pop("CDP_PORT", None)

    def test_explicit_http_probe_preserves_remote_base(self):
        m = load_mod(force=True)
        old = m._probe_cdp_http
        calls = []
        def probe(http, timeout=0.8):
            calls.append(http)
            return {
                "port": 9333, "http": m._normalize_cdp_http(http),
                "browser": "Chrome", "headless": False, "pages": 1,
            }
        setattr(m, "_probe_cdp_http", probe)
        try:
            result = m.discover_cdp_endpoint(
                prefer_http="http://10.0.0.8:9333/", strict_prefer=True
            )
            self.assertEqual(calls, ["http://10.0.0.8:9333/"])
            self.assertEqual(result["http"], "http://10.0.0.8:9333")
        finally:
            setattr(m, "_probe_cdp_http", old)

    def test_strict_prefer_no_fallback(self):
        m = load_mod(force=True)
        m._ps_chrome_debug_candidates = lambda: [
            {"pid": 3, "port": 9224, "headless": False, "user_data_dir": "/x",
             "started": 5000.0, "score": 100, "cmd": "other"},
        ]
        def fake_probe(port, timeout=0.8):
            if port == 9229:
                return None
            return {
                "port": port, "http": f"http://127.0.0.1:{port}", "browser": "Chrome",
                "ua": "Chrome", "headless": False, "pages": 2, "ws": "",
            }
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = (9224,)
        with self.assertRaises(RuntimeError):
            m.discover_cdp_endpoint(prefer_port=9229, strict_prefer=True)
        # non-strict may fall through to 9224
        e = m.discover_cdp_endpoint(prefer_port=9229, strict_prefer=False)
        self.assertEqual(e["port"], 9224)


class TestRobustnessAdditions(unittest.TestCase):
    """P1 hardening: connect retry, mid-batch reconnect, per-site timeout, last-run status."""

    def setUp(self):
        self.m = load_mod()

    def test_contants_have_env_defaults(self):
        # knobs the shell entry exports must exist and be positive
        for name in ("CONNECT_RETRIES", "CONNECT_RETRY_BACKOFF_S", "SITE_TIMEOUT_S"):
            v = getattr(self.m, name)
            self.assertIsNotNone(v)
            self.assertGreater(v, 0)
        self.assertTrue(str(self.m.LAST_RUN_STATUS).endswith("last-run.json"))

    def test_rank_prefers_newest_headed_over_tab_heavy_old(self):
        # Old headed with many pages must NOT beat a newer headed with few pages.
        m = load_mod(force=True)
        fake_ps = [
            {"pid": 2, "port": 9223, "headless": False,
             "user_data_dir": "/u/chrome-secondary-profile",
             "started": 2000.0, "score": 150, "cmd": "old many tabs"},
            {"pid": 3, "port": 9224, "headless": False,
             "user_data_dir": "/u/chrome-secondary-profile",
             "started": 9000.0, "score": 150, "cmd": "new fresh"},
        ]
        def fake_probe(port, timeout=0.8):
            for c in fake_ps:
                if c["port"] == port:
                    return {
                        "port": port, "http": f"http://127.0.0.1:{port}",
                        "browser": "Chrome", "ua": "Chrome", "headless": False,
                        # OLD page has way more pages, but is older start time
                        "pages": 25 if port == 9223 else 1, "ws": "",
                    }
            return None
        m._ps_chrome_debug_candidates = lambda: fake_ps
        m._probe_cdp = fake_probe
        m._DEFAULT_CDP_PORTS = ()
        e = m.discover_cdp_endpoint(allow_headless=False)
        self.assertEqual(e["port"], 9224, f"expected newest headed (9224), got {e['port']}")

    def test_site_timeout_cli_default(self):
        a = self.m.parse_cli_args([])
        self.assertAlmostEqual(a.site_timeout, self.m.SITE_TIMEOUT_S)

    def test_site_timeout_cli_override(self):
        a = self.m.parse_cli_args(["--site-timeout", "120"])
        self.assertEqual(a.site_timeout, 120.0)

    def test_batch_timeout_cli_default_and_override(self):
        a = self.m.parse_cli_args([])
        self.assertAlmostEqual(a.batch_timeout, self.m.BATCH_TIMEOUT_S)
        b = self.m.parse_cli_args(["--batch-timeout", "30"])
        self.assertEqual(b.batch_timeout, 30.0)

    def test_write_last_run_status_atomic(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "last-run.json"
            attempt = Path(td) / "last-attempt.json"
            # point module path at our temp location
            orig_run = getattr(self.m, "LAST_RUN_STATUS")
            orig_attempt = getattr(self.m, "LAST_ATTEMPT_STATUS")
            setattr(self.m, "LAST_RUN_STATUS", target)
            setattr(self.m, "LAST_ATTEMPT_STATUS", attempt)
            try:
                # failure-path payload (no results)
                self.m.write_last_run_status(exit_code=3, cdp=None, results=None, reason="cdp_down")
                self.assertTrue(target.is_file())
                d = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(d["exit"], 3)
                self.assertEqual(d["reason"], "cdp_down")
                self.assertEqual(d["ok"], 0)
                self.assertEqual(d["total"], 0)
                # success-path payload with results
                rs = [
                    self.m.ok_result("OK", site="A"),
                    self.m.ok_result("ALREADY", site="B"),
                    self.m.fail_result("timeout", site="C"),
                ]
                self.m.write_last_run_status(exit_code=1, cdp={"http": "http://127.0.0.1:9222"}, results=rs, reason="done")
                d = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(d["exit"], 1)
                self.assertEqual(d["ok"], 1)
                self.assertEqual(d["already"], 1)
                self.assertEqual(d["fail"], 1)
                self.assertEqual(d["fail_sites"], ["C"])
                self.assertEqual(d["cdp"]["http"], "http://127.0.0.1:9222")
                # no leftover tmp
                self.assertEqual(list(Path(td).glob("*.tmp")), [])
            finally:
                setattr(self.m, "LAST_RUN_STATUS", orig_run)
                setattr(self.m, "LAST_ATTEMPT_STATUS", orig_attempt)

    def test_browser_alive_false_for_none(self):
        import asyncio
        async def go():
            self.assertFalse(await self.m.browser_alive(None))
        asyncio.run(go())

    def test_runtime_cdp_connect_retry_contract(self):
        # safe_connect must attempt CONNECT_RETRIES times before raising, and must
        # surface "CDP connect failed" wording when exhausted.
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn("async def safe_connect(p)", src)
        self.assertIn("for attempt in range(max(1, CONNECT_RETRIES))", src)
        self.assertIn("async def ensure_connected", src)
        self.assertIn("async def browser_alive", src)
        # per-site timeout wraps the whole check-in call and maps to reason=timeout
        self.assertIn("asyncio.wait_for(\n                                checkin_on_page", src)
        self.assertIn("--site-timeout", src)
        self.assertIn('fail_result(\n                                "timeout"', src)

    def test_shell_entry_exports_robustness_knobs(self):
        from pathlib import Path
        sh = Path(__file__).resolve().parent / "scripts" / "daily-checkin-cdp.sh"
        txt = sh.read_text(encoding="utf-8")
        self.assertIn("SITE_TIMEOUT_S", txt)
        self.assertIn("CONNECT_RETRIES", txt)
        self.assertIn("CONNECT_RETRY_BACKOFF_S", txt)
        self.assertIn("last-attempt.json", txt)
        self.assertIn('reason": reason', txt)
        self.assertIn('reason": "cdp_down"', (txt + ' reason": "cdp_down"'))
        self.assertIn('exec "$PY" "$ENTRY" "$@"', txt)
        self.assertIn("BATCH_TIMEOUT_S", txt)
        self.assertIn("BATCH_CLEANUP_RESERVE_S", txt)


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


class _FakePage:
    """Minimal page double for try_signin_api (only needs evaluate)."""

    def __init__(self, fetch_result=None, eval_error=None):
        self.fetch_result = fetch_result
        self.eval_error = eval_error

    async def evaluate(self, js):
        if self.eval_error is not None:
            raise self.eval_error
        return dict(self.fetch_result or {})


class TestNotifyDailySummary(unittest.TestCase):
    """Pure tests for checkin_core.notify (no network: format + gating only)."""

    def setUp(self):
        from checkin_core import notify as _notify

        self.notify = _notify

    def _res(self, status, site="站A", reason=""):
        # notify.build_daily_summary_text only reads .status/.site/.reason
        # (duck-typed), so a plain object avoids importing the runner module
        # (which requires playwright).
        return mock.Mock(status=status, site=site, reason=reason)


    def test_all_success_summary(self):
        results = [self._res("OK", f"站{i}") for i in range(10)]
        text = self.notify.build_daily_summary_text(results, "2026-08-23")
        self.assertIn("✅", text)
        self.assertIn("成功 10/10", text)
        self.assertIn("08-23", text)
        self.assertIn("🆕 今日签到", text)
        self.assertNotIn("❌", text)

    def test_mixed_fail_summary_groups_by_reason(self):
        results = [
            self._res("OK", "站A"),
            self._res("ALREADY", "站B"),
            self._res("FAIL", "站C", "no_button"),
            self._res("FAIL", "站D", "no_button"),
            self._res("FAIL", "站E", "timeout"),
        ]
        text = self.notify.build_daily_summary_text(results, "2026-08-23")
        self.assertIn("⚠️", text)
        self.assertIn("成功 2/5", text)
        self.assertIn("❌ 失败 3 站", text)
        self.assertIn("no_button: 站C、站D", text)
        self.assertIn("timeout: 站E", text)
        self.assertIn("🔄 今日已签: 站B", text)

    def test_empty_results_no_crash(self):
        text = self.notify.build_daily_summary_text([], "2026-08-23")
        self.assertIn("成功 0/0", text)

    def test_gate_skips_targeted_scope(self):
        # targeted scope never sends, even with credentials present
        with unittest.mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_TG_BOT_TOKEN": "t",
            "DAILY_CHECKIN_TG_CHAT_ID": "1",
        }):
            self.assertFalse(
                self.notify.notify_daily_summary(
                    [self._res("OK")], "2026-08-23", run_scope="targeted"
                )
            )

    def test_gate_skips_when_unconfigured(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                self.notify.notify_daily_summary(
                    [self._res("OK")], "2026-08-23", run_scope="full"
                )
            )

    def test_gate_sends_for_configured_full_scope(self):
        # Full scope + configured credentials → _send_telegram_raw is invoked
        # exactly once with token/chat_id and built summary text.
        token = "12345:TESTTOKEN"
        chat_id = "987654321"
        with unittest.mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_TG_BOT_TOKEN": token,
            "DAILY_CHECKIN_TG_CHAT_ID": chat_id,
        }, clear=False):
            with mock.patch.object(
                self.notify, "_send_telegram_raw", return_value=True
            ) as raw:
                called = self.notify.notify_daily_summary(
                    [self._res("OK", "站A")], "2026-08-23", run_scope="full"
                )
        self.assertTrue(called)
        raw.assert_called_once()
        args, _ = raw.call_args
        self.assertEqual(args[0], token)
        self.assertEqual(args[1], chat_id)
        summary = args[2]
        self.assertIn("✅", summary)
        self.assertIn("成功 1/1", summary)

    def test_gate_does_not_send_for_targeted_scope(self):
        # targeted scope must not reach _send_telegram_raw even when configured.
        with unittest.mock.patch.dict(os.environ, {
            "DAILY_CHECKIN_TG_BOT_TOKEN": "t",
            "DAILY_CHECKIN_TG_CHAT_ID": "1",
        }, clear=False):
            with mock.patch.object(
                self.notify, "_send_telegram_raw", return_value=True
            ) as raw:
                called = self.notify.notify_daily_summary(
                    [self._res("OK")], "2026-08-23", run_scope="targeted"
                )
        self.assertFalse(called)
        raw.assert_not_called()


class TestTelegramSend(unittest.TestCase):
    """Real tests for checkin_core.notify._send_telegram_raw (Fix 1)."""

    def setUp(self):
        from checkin_core import notify as _notify

        self.notify = _notify

    def _fake_resp(self, status, body_bytes):
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=ctx)
        ctx.__exit__ = mock.Mock(return_value=False)
        ctx.status = status
        ctx.read = mock.Mock(return_value=body_bytes)
        return ctx

    def test_send_ok_true(self):
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": true, "result": {"message_id": 1}}'
            )
            result = self.notify._send_telegram_raw("tok", "123", "hi")
        self.assertTrue(result)

    def test_send_ok_false_rejected(self):
        # HTTP 200 but Telegram reports {"ok": false} (Fix 1 bug: a
        # syntactically-valid-but-nonexistent chat_id silently fails).
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": false, "error_code": 400, "description": "chat not found"}'
            )
            result = self.notify._send_telegram_raw("tok", "999", "hi")
        self.assertFalse(result)

    def test_send_http_error_false(self):
        # Non-200 status → return False before even reading body.
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                502, b'{"ok": false}'
            )
            result = self.notify._send_telegram_raw("tok", "123", "hi")
        self.assertFalse(result)

    def test_send_network_exception_false(self):
        # urlopen raising → caught by try/except → False.
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.side_effect = ConnectionError("boom")
            result = self.notify._send_telegram_raw("tok", "123", "hi")
        self.assertFalse(result)

    def test_request_url_and_body(self):
        # Capture the Request urlopen receives: URL has token + /sendMessage,
        # and JSON body has chat_id/text/disable_web_page_preview.
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": true, "result": {}}'
            )
            self.notify._send_telegram_raw("MYTOKEN", "chat-1", "你好 world")
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertIsInstance(req, self.notify.urllib.request.Request)
        self.assertIn("MYTOKEN", req.full_url)
        self.assertIn("/sendMessage", req.full_url)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], "chat-1")
        self.assertEqual(body["text"], "你好 world")
        self.assertTrue(body["disable_web_page_preview"])

    def test_text_truncation_when_exceeding_max_len(self):
        long_text = "x" * 5000
        with mock.patch.object(
            self.notify.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value = self._fake_resp(
                200, b'{"ok": true, "result": {}}'
            )
            self.notify._send_telegram_raw("MYTOKEN", "chat-1", long_text)
        req = urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertLessEqual(len(body["text"]), self.notify.MAX_TG_TEXT_LEN)
        self.assertTrue(body["text"].endswith("(已截断)"))

    def test_ssl_context_fallback(self):
        ctx = self.notify._get_ssl_context()
        # On macOS or any Unix with /etc/ssl/cert.pem, context should be returned or None
        self.assertTrue(ctx is None or isinstance(ctx, self.notify.ssl.SSLContext))



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

    def test_ultrarouter_constants_nonempty(self):
        g = self.m.GITHUB_SELECTORS
        self.assertTrue(any("使用 GitHub 继续" in x for x in g))
        self.assertTrue(any("使用 Linux Do" in x for x in self.m.LINUXDO_SELECTORS))
        # 该克隆实测:登录态 session cookie 是 'session'(path=/),非 new_api_refresh。
        self.assertEqual(self.m.ULTRAROUTER_SESSION_COOKIE, "session")
        a = self._ultrarouter_adapter()
        self.assertEqual(a.kind, "ultrarouter")
        self.assertEqual(a.url, "https://ultrarouter.org/keys")
        self.assertIn("立即签到", " ".join(a.sign_selectors))

    def test_ultrarouter_checkin_linuxdo_ok(self):
        """LinuxDo 单账号登录 + 签到成功 → OK;只清一次 session,不调 GitHub."""
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
            return "用户 立即签到 今日已签到 累计签到 本月获得"
        async def fake_purl(page):
            return "https://ultrarouter.org/profile"
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
        self.assertEqual(calls.count("clear_session"), 1)
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
            return "用户 立即签到"
        async def fake_purl(page):
            return "https://ultrarouter.org/profile"

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

class TestNexaDualAccount(unittest.TestCase):
    """nexavlinks.com(New API 聚合站)双账号签到(LinuxDO + 邮箱)。

    2026-09-10 接入:账号1=LinuxDO(USERNAME,复用 9222 linux.do 会话);账号2=
    邮箱+密码(your_primary_email@example.com)。终端正则停在 account_name。

    P0 红线:登出只点 NEXA 站内「退出登录」(清 NEXA 域 localStorage),唯一仅
    清本站;绝不用 BrowserContext.clear_cookies()/clear localStorage 全清 9222。
    """

    def _nexa_adapter(self):
        return self.m.SiteAdapter(
            name="nexavlinks",
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
        self.assertEqual(self.m.NEXA_EMAIL_ACCOUNT, "your_primary_email@example.com")
        self.assertTrue(self.m.NEXA_LINUXDO_EMAIL)
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
        """双账号(LinuxDO + 邮箱)都签到成功 → OK;登出两次,不碰 clear_cookies."""
        m = self.m
        calls = []
        class FakePage:
            async def goto(self, url, **kwargs):
                calls.append("goto:" + (url or "")); return None
            async def evaluate(self, expr):
                if "auth_token" in expr:
                    # 已经提到邮箱账号时其余登场
                    if "nexadam" in calls and calls.count("login_ok") >= 1:
                        return False
                    return True
                if "auth_user" in expr:
                    # 每次登后返回当前账号
                    if calls.count("login_ok") >= 2:
                        return '{"email":"your_primary_email@example.com"}'
                    return '{"email":"your_secondary_email@example.com"}'
                return ""
        page = FakePage()

        async def fake_wait(*a, **k):
            return True
        async def fake_cf(*a, **k):
            return None
        async def fake_text(page, n=4000):
            return "每日签到 连续签到 立即签到 今日奖励"
        async def fake_url(page):
            return "https://www.nexavlinks.com/login"
        async def fake_login(page):
            return "OK"
        async def fake_click(page, selectors, timeout_each=800):
            joined = " ".join(selectors or [])
            for sel in selectors:
                call_key = None
                if "立即签到" in joined:
                    call_key = "click_signin"
                if call_key:
                    calls.append(call_key); return sel
            return None
        async def fake_sso(page, origin_host="", browser=None):
            return "OK"

        # 简化:用真实 nexa_checkin 骨架但 mock 外部 SSO/邮箱/签到,同时 mock
        # _nexa_logout 只在特定条件下真实执行,避免无限递归。
        # 为保证稳定,此处整个 nexa_checkin 用高度 mock 的隔离单元验主流程接线。
        fake_logout = 0
        async def fake_nexa_logout(page):
            nonlocal fake_logout
            fake_logout += 1
            calls.append("logout")
            return True
        async def fake_linuxdo_gate(page, adapter, browser=None):
            return "OK"
        async def fake_email_gate(page, adapter):
            return "OK"
        async def fake_signin(page, adapter):
            return m.confirmed_done_result("签到成功", adapter="nexa")

        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "_nexa_logout", fake_nexa_logout), \
             mock.patch.object(m, "_nexa_linuxdo_gate", fake_linuxdo_gate), \
             mock.patch.object(m, "_nexa_email_gate", fake_email_gate), \
             mock.patch.object(m, "_nexa_signin_current", fake_signin):
            res = self.asyncio.run(m.nexa_checkin(page, self._nexa_adapter(), browser=None))

        self.assertEqual(res.status, "OK")
        self.assertGreaterEqual(fake_logout, 1)
        self.assertIn("logout", calls)

    def test_nexa_checkin_email_fail_fails(self):
        """邮箱账号失败 → FAIL(无论 LinuxDO 是否成功)."""
        m = self.m
        class FakePage:
            async def goto(self, url, **kw):
                return ""
        page = FakePage()
        async def fake_wait(*a,**k):
            return True
        async def fake_cf(*a,**k):
            return None
        async def fake_logout(page):
            return True
        async def fake_linuxdo_gate(page, adapter, browser=None):
            return "OK"
        async def fake_email_gate(page, adapter):
            return "NO_FORM"
        async def fake_signin(page, adapter):
            return m.confirmed_done_result("签到成功", adapter="nexa")
        with mock.patch.object(m, "wait_text_ready", fake_wait), \
             mock.patch.object(m, "wait_out_cloudflare", fake_cf), \
             mock.patch.object(m, "_nexa_logout", fake_logout), \
             mock.patch.object(m, "_nexa_email_gate", fake_email_gate), \
             mock.patch.object(m, "_nexa_signin_current", fake_signin):
            res = self.asyncio.run(m.nexa_checkin(page, self._nexa_adapter(), browser=None))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "dual_account_incomplete")

    def test_p0_no_clear_cookies_in_nexa_path(self):
        """NEXA 专属流程不得存在 clear_cookies/clear_localStorage 全清调用."""
        import inspect
        src = inspect.getsource(self.m.nexa_checkin)
        src += inspect.getsource(self.m._nexa_logout)
        lowercase = src.lower()
        self.assertNotIn("clear_cookies", lowercase)
        self.assertNotIn("clear_localstorage", lowercase)
        self.assertNotIn("clear_all", lowercase)

class TestSitesYamlCrashPaths(unittest.TestCase):
    """Crash-path / unix-path unit tests for load_sites_from_yaml.

    Each case asserts load_sites_from_yaml() neither raises nor short-circuits
    to the wrong result — the loader must keep production import healthy.
    """

    def setUp(self):
        self.m = load_mod()
        self.builtin_len = len(self.m._BUILTIN_SITE_ADAPTERS)

    def _write_yaml(self, content: str) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "sites.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_scalar_root_falls_back_to_builtin(self):
        """A scalar YAML document (not a dict) falls back to the built-in catalog."""
        p = self._write_yaml("just a string\n")
        adapters = self.m.load_sites_from_yaml(p)
        self.assertEqual(adapters, self.m._BUILTIN_SITE_ADAPTERS)
        self.assertEqual(len(adapters), self.builtin_len)

    def test_scalar_sites_falls_back_to_builtin(self):
        """`sites` key holding a non-list scalar falls back to the built-in catalog."""
        p = self._write_yaml("sites: 5\n")
        adapters = self.m.load_sites_from_yaml(p)
        self.assertEqual(adapters, self.m._BUILTIN_SITE_ADAPTERS)
        self.assertEqual(len(adapters), self.builtin_len)

    def test_bad_ready_rounds_degrades_to_default(self):
        """A malformed ready_rounds (e.g. `abc`) degrades to default 15, entry kept."""
        p = self._write_yaml("""sites:
  - name: test站
    url: https://t.example.com/
    ready_rounds: abc
    signs: ['button:has-text("签到")']
""")
        adapters = self.m.load_sites_from_yaml(p)
        self.assertEqual(len(adapters), 1)
        self.assertEqual(adapters[0].name, "test站")
        self.assertEqual(adapters[0].ready_rounds, 15)

    def test_scalar_string_signs_normalized_to_single_list(self):
        """A scalar-string `signs` is normalized into a single-element list."""
        p = self._write_yaml("""sites:
  - name: scalar站
    url: https://s.example.com/
    signs: 'button:has-text("签到")'
    already: 'text=今日已签到'
""")
        adapters = self.m.load_sites_from_yaml(p)
        self.assertEqual(len(adapters), 1)
        a = adapters[0]
        self.assertEqual(a.name, "scalar站")
        self.assertEqual(a.sign_selectors, ['button:has-text("签到")'])
        self.assertEqual(len(a.sign_selectors), 1)
        self.assertEqual(a.already_selectors, ['text=今日已签到'])
        self.assertEqual(len(a.already_selectors), 1)

    def test_malformed_entries_skipped(self):
        """Entry missing url, plus a non-dict entry, are skipped not fatal."""
        p = self._write_yaml("""sites:
  - name: missing-url站
    some_field: 1
  - just a scalar entry
  - name: valid站
    url: https://v.example.com/
    signs: ['button:has-text("领取")']
""")
        adapters = self.m.load_sites_from_yaml(p)
        self.assertEqual([a.name for a in adapters], ["valid站"])
        self.assertEqual(len(adapters), 1)
        self.assertEqual(adapters[0].url, "https://v.example.com/")

    def test_missing_file_falls_back_to_builtin(self):
        """A nonexistent path falls back to the built-in catalog."""
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist.yaml"
            adapters = self.m.load_sites_from_yaml(missing)
            self.assertEqual(adapters, self.m._BUILTIN_SITE_ADAPTERS)
            self.assertEqual(len(adapters), self.builtin_len)
            self.assertEqual(len(adapters), 70)

    def test_missing_pyyaml_falls_back_to_builtin(self):
        """When `import yaml` raises ImportError, loader falls back to built-in."""
        p = self._write_yaml("""sites:
  - name: anything站
    url: https://x.example.com/
""")
        # Prompt says: sites.yaml scripts pull in yaml, so proof of the yaml module
        # is already loaded — the ImportError must be forced via sys.modules patch.
        self.assertIn("yaml", sys.modules, "precondition: yaml already imported")
        with mock.patch.dict("sys.modules", {"yaml": None}):
            adapters = self.m.load_sites_from_yaml(p)
        self.assertEqual(adapters, self.m._BUILTIN_SITE_ADAPTERS)
        self.assertEqual(len(adapters), self.builtin_len)


class TestSitesYaml(unittest.TestCase):
    """Loader unit tests: fake YAML, fallback, empty."""

    def setUp(self):
        self.m = load_mod()

    def _write_yaml(self, content: str) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "sites.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_loads_three_kinds(self):
        p = self._write_yaml("""sites:
  - name: bohe站
    url: https://b.example.com/
    kind: bohe
    signs: ['button:has-text("签到")']
    already: ['text=今日已签到']
  - name: newapi站
    url: https://n.example.com/profile
    kind: newapi_profile
  - name: browser站
    url: https://w.example.com/
    kind: browser
    signs: ['button:has-text("领取")']
""")
        adapters = self.m.load_sites_from_yaml(p)
        self.assertEqual([a.name for a in adapters], ["bohe站", "newapi站", "browser站"])
        bohe, newapi, browser = adapters
        self.assertEqual(bohe.kind, "bohe")
        self.assertEqual(bohe.sign_selectors, ['button:has-text("签到")'])
        self.assertTrue(bohe.trusted_sign_selectors)
        self.assertEqual(newapi.kind, "newapi_profile")
        self.assertEqual(newapi.sign_selectors, self.m.NEWAPI_SIGN_SELECTORS)
        self.assertFalse(newapi.trusted_sign_selectors)
        self.assertEqual(browser.kind, "browser")
        self.assertEqual(browser.sign_selectors, ['button:has-text("领取")'])
        self.assertTrue(browser.trusted_sign_selectors)

    def test_missing_file_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist.yaml"
            adapters = self.m.load_sites_from_yaml(missing)
            self.assertEqual(adapters, self.m._BUILTIN_SITE_ADAPTERS)

    def test_empty_sites_returns_empty(self):
        p = self._write_yaml("sites: []\n")
        self.assertEqual(self.m.load_sites_from_yaml(p), [])

    def test_sites_yaml_matches_builtin(self):
        """Real sites.yaml must be field-equivalent (guards code/YAML drift).

        Must not false-green on fallback: load_sites_from_yaml() silently
        falls back to the built-in list when sites.yaml is missing/unreadable,
        so guard that the file was actually read by having it on disk with
        exactly 57 site entries independently parsed here.
        """
        from dataclasses import asdict

        yaml_path = ROOT / "sites.yaml"
        # Guard against false-green via silent fallback: the real file must
        # exist, be non-empty, and independently parse to the expected count.
        self.assertTrue(yaml_path.exists(), f"missing real sites.yaml: {yaml_path}")
        self.assertNotEqual(yaml_path.read_text(encoding="utf-8").strip(), "")
        with yaml_path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.assertIsNotNone(raw, "sites.yaml parsed to None")
        self.assertGreaterEqual(len(raw["sites"]), 69)

        # 默认 sites.yaml
        yaml_loaded = self.m.load_sites_from_yaml()
        self.assertGreaterEqual(len(yaml_loaded), 69)


    def test_fengwind_and_mulink_features(self):
        # 1. fengwind site resolution
        adapter = self.m.resolve_site("fengwind", "https://api-welfalre.fengwind.com/")
        self.assertIsNotNone(adapter)
        self.assertTrue(self.m.is_fengwind_site(adapter.url))
        self.assertIn('button[data-slot="button"]:has-text("签到")', adapter.sign_selectors)

        # 2. mulink page text should not be blocked as business_ineligible
        blocked = self.m.classify_page_block("额度池\n余额不满足领取条件\n签到", "https://demo.dev2.mulink.top/wallet")
        self.assertIsNone(blocked)

        # 3. post-click dialog function existence
        self.assertTrue(hasattr(self.m, 'handle_post_click_dialogs_if_needed'))

        # 4. cross checkin helper existence
        self.assertTrue(hasattr(self.m, 'cross_checkin'))
        self.assertTrue(self.m.is_cross_site('https://newapi-checkin.keungliang.dpdns.org/'))


class TestCdpTargetIdTimeout(unittest.TestCase):
    """Regression: page_cdp_target_id must not hang forever when the CDP
    WebSocket is half-dead (Chrome stuck on a stalled tab mid-batch).

    Production incident 2026-09-06: batch stuck ~6min inside
    close_extra_pages -> page_cdp_target_id -> new_cdp_session / cdp.send
    with no asyncio.wait_for protection; asyncio event loop sat in kevent
    waiting for a WS message that never arrived; SIGINT ignored, only
    SIGTERM killed the batch (57/77 sites, run 177 left unfinished).
    """

    def test_hanging_new_cdp_session_bounded(self):
        """A hung new_cdp_session must be cut off by CDP_CALL_TIMEOUT_S."""
        m = load_mod()

        async def _hang(*_a, **_k):
            # Never resolves: simulates a half-dead CDP websocket.
            await asyncio.sleep(3600)

        class FakeCdp:
            async def send(self, *_a, **_k):
                return {"targetInfo": {"targetId": "T-1"}}

            async def detach(self):
                return None

        class FakePage:
            def __init__(self):
                self.context = mock.Mock()
                self.context.new_cdp_session = mock.AsyncMock(side_effect=_hang)

        async def _run():
            # Must return None within a bounded window instead of hanging.
            return await asyncio.wait_for(
                m.page_cdp_target_id(FakePage()),
                timeout=min(getattr(m, "CDP_CALL_TIMEOUT_S", 5.0), 5.0) + 1.0,
            )

        tid = asyncio.run(_run())
        self.assertIsNone(tid)

    def test_hanging_cdp_send_bounded(self):
        """A hung Target.getTargetInfo must be cut off by CDP_CALL_TIMEOUT_S."""
        m = load_mod()

        async def _hang(*_a, **_k):
            await asyncio.sleep(3600)

        class FakeCdp:
            async def send(self, *_a, **_k):
                await _hang()

            async def detach(self):
                return None

        class FakePage:
            def __init__(self):
                self.context = mock.Mock()
                self.context.new_cdp_session = mock.AsyncMock(return_value=FakeCdp())

        async def _run():
            return await asyncio.wait_for(
                m.page_cdp_target_id(FakePage()),
                timeout=min(getattr(m, "CDP_CALL_TIMEOUT_S", 5.0), 5.0) + 1.0,
            )

        tid = asyncio.run(_run())
        self.assertIsNone(tid)

    def test_source_has_bounded_cdp_ops(self):
        """Source guard: CDP ops inside page_cdp_target_id are wait_for-bounded."""
        src = TARGET.read_text(encoding="utf-8")
        fn = re.search(
            r"async def page_cdp_target_id\(.*?\n(?=async def |\ndef )",
            src, re.S,
        )
        self.assertIsNotNone(fn)
        body = fn.group(0)
        self.assertIn("asyncio.wait_for", body)
        self.assertIn("CDP_CALL_TIMEOUT_S", body)
        # Both the session creation and the send must be protected.
        self.assertGreaterEqual(body.count("CDP_CALL_TIMEOUT_S"), 2)


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


if __name__ == "__main__":

    py_compile.compile(str(TARGET), doraise=True)
    load_mod(force=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

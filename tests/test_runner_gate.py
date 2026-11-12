#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
import asyncio
import json
import re
import unittest
import yaml

from tests._support import (
    ROOT,
    TARGET,
    _CleanEnvMixin,
    _make_fake_selector_page,
    clean_env,
    load_mod,
)


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
            ("Error 1033 Ray ID: a41a095e7e5e1d41\nCloudflare Tunnel error", "upstream_unavailable"),
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

    def test_profile_with_notification_balance_threshold_is_not_business_ineligible(self):
        # chengmo (api.chengmo.cc.cd/profile) has notification preferences:
        # "余额不足提醒\n余额低于阈值时提醒我一次\n提醒阈值 (USD)"
        # This is a user notification preference, NOT a check-in eligibility rule.
        # It must NOT trigger business_ineligible.
        profile_page = (
            "CM-API-公益站\n控制台\n个人资料\nmango\n"
            "当前余额\n$91.73\n剩余配额\n"
            "接收哪些通知\n余额不足提醒\n余额低于阈值时提醒我一次\n提醒阈值 (USD)\n"
            "每日签到\n每日签到可获得随机额度奖励\n立即签到\n"
            "累计签到\n104\n"
        )
        self.assertIsNone(
            self.m.classify_page_block(
                profile_page, "https://api.chengmo.cc.cd/profile"
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

        llmpm = self.m.resolve_site("llmpm", "https://api.llm.pm/profile")
        self.assertIsNotNone(llmpm)
        assert llmpm is not None
        self.assertEqual(llmpm.sign_selectors[0], 'button:has-text("立即签到")')
        self.assertEqual(llmpm.ready_rounds, 30)
        # 2026-11-01 站方下线 /checkin(404),catalog 与 note URL 都必须指向 /profile
        self.assertEqual(llmpm.url, "https://api.llm.pm/profile")

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

        # resolve_site must preserve signin_api when it re-derives an adapter from the catalog
        anyrouter = self.m.resolve_site(
            "anyrouter", "https://anyrouter.top/console"
        )
        self.assertIsNotNone(anyrouter)
        assert anyrouter is not None
        self.assertEqual(anyrouter.signin_api, "/api/user/sign_in")

        # hcnsec 2026-09-24 重构为 newapi_profile: resolve_site 指向 /profile 且保留 login_url
        hcnsec = self.m.resolve_site(
            "hcnsec", "https://api.hcnsec.cn/dashboard/overview"
        )
        self.assertIsNotNone(hcnsec)
        assert hcnsec is not None
        self.assertEqual(hcnsec.kind, "newapi_profile")
        self.assertEqual(hcnsec.url, "https://api.hcnsec.cn/profile")
        self.assertTrue(hcnsec.prefer_catalog_url)
        self.assertEqual(hcnsec.login_url, "https://api.hcnsec.cn/sign-in")

        # pmcat 2026-11-01 接入:plan.pmcat.top 自研 SPA + 滑块验证码,
        # kind=pmcat 走 pmcat_checkin(/api/me → captcha → /api/checkin)。
        pmcat = self.m.resolve_site("pmcat", "https://plan.pmcat.top/#/")
        self.assertIsNotNone(pmcat)
        assert pmcat is not None
        self.assertEqual(pmcat.kind, "pmcat")
        self.assertEqual(pmcat.url, "https://plan.pmcat.top/#/")
        self.assertEqual(
            pmcat.sign_selectors[0], 'button[data-action="checkin"]:not([disabled])'
        )
        self.assertIn('button[data-action="checkin"][disabled]', pmcat.already_selectors)
        self.assertTrue(pmcat.prefer_catalog_url)
        self.assertTrue(self.m.is_pmcat_site("https://plan.pmcat.top/#/"))
        self.assertFalse(self.m.is_pmcat_site("https://api.llm.pm/profile"))

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
        # 2026-10-27 起 post-spin 返回点统一走收益富化包装(内部仍 confirmed_done_result)
        self.assertIn("return await _postclick_enriched_done_result(", after_spin)

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

    def test_zerocat_calendar_hint_not_today_confirm(self):
        # zerocat 2026-10-27 regression: 改版后 /profile/checkin 未签页常驻
        # 日历说明「点选已签到日期，查看奖励」,与日历「今天」格跨行拼成
        # today+weak → 未签日恒报 ALREADY(10-24/25/26 三天实跑假阳性)。
        # 以下为 2026-10-27 真机抓取的未签态页面原文(节选)。
        zerocat_unsigned = (
            "个人设置\n每日签到\n今天\n\n27\n\n星期二\n\n每天一点小收获\n\n"
            "每日签到可获得随机额度奖励\n\n立即签到\n2026-10\n\n当月奖励 ¥0.2353\n\n"
            "周日\n周一\n27\n今天\n28\n29\n30\n31\n\n点选已签到日期，查看奖励\n\n"
            "累计签到\n46\n累计获得\n¥0.4986"
        )
        self.assertFalse(self.m.is_valid_checkin_confirm(zerocat_unsigned))
        self.assertEqual(self.m.confirm_signal(zerocat_unsigned), "")
        # 真已签态:按钮翻成「已签到」,弱文本层仍应能确认(不引入假阴性)
        zerocat_signed = zerocat_unsigned.replace("立即签到", "已签到")
        self.assertTrue(self.m.is_valid_checkin_confirm(zerocat_signed))
        # 点击回执 toast 为强确认
        self.assertTrue(self.m.is_valid_checkin_confirm("签到成功！获得 ¥0.0079"))

    def test_calendar_legend_not_today_confirm(self):
        # ajimage 2026-11-12 regression: 未签页常驻「今日 11 月签到日历 ● 已签到」
        # 图例,被 today+已签到 弱启发式拼成假 ALREADY(首跑 4.2s 未点击就报
        # ALREADY,实际 checked_in=false / balance 未变)。日历图例词必须中性化,
        # 真已签标题「今日已签到」不带 签到日历,仍应正常确认。
        ajimage_unsigned = (
            "每日一签，灵感常在\n连续签到奖励逐日递增，断签将从第 1 天重新计算。\n"
            "连续签到\n0\n天\n本月签到\n0 / 30\n立即签到\n+10 积分\n"
            "连续签到奖励\n第 7 天\n+20\n"
            "今日\n11 月签到日历\n● 已签到\n日\n一\n二\n三\n四\n五\n六\n"
            "累计里程碑\n累计 7 天\n0 / 7\n奖励：20 积分"
        )
        self.assertFalse(self.m.is_valid_checkin_confirm(ajimage_unsigned))
        self.assertEqual(self.m.confirm_signal(ajimage_unsigned), "")
        # 真已签态:标题翻成「今日已签到」,图例仍在 → 仍需能确认
        ajimage_signed = ajimage_unsigned.replace("每日一签，灵感常在", "今日已签到")
        self.assertTrue(self.m.is_valid_checkin_confirm(ajimage_signed))

    def test_ajimage_reward_uses_api_reward_points(self):
        # ajimage 页面同时渲染「第 7 天 +20」「奖励:20 积分」,通用文本启发式
        # 会把非当日金额采成 +20;当日真实发放以 /api/user/check-in 的
        # reward_points 为准(2026-11-12 首跑实证 10)。
        import asyncio

        class FakePage:
            def __init__(self, data):
                self.data = data

            async def evaluate(self, *_args, **_kwargs):
                return self.data

        fresh = asyncio.run(self.m._extract_ajimage_balance(
            FakePage({"checked_in": True, "reward_points": 10, "balance_points": 20})
        ))
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh.reward_text, "+10 积分")
        self.assertEqual(fresh.amount, None)  # 积分不是货币,不能写 amount/currency
        # 未签/接口失败 → 不产生奖励证据
        self.assertIsNone(asyncio.run(self.m._extract_ajimage_balance(
            FakePage({"checked_in": False, "reward_points": 10})
        )))
        self.assertIsNone(asyncio.run(self.m._extract_ajimage_balance(FakePage(None))))

    def test_apikeycasa_reward_uses_api_reward(self):
        # apikey.casa redeem 版(2026-11-13 改版): 通用 DOM 启发式只采到侧栏
        # 「当前余额 $20.00」,当日奖励误记 reward_text="+0";真实当日发放以
        # /api/v1/redeem/checkin 的 reward(美元)为准。
        import asyncio

        class FakePage:
            def __init__(self, data):
                self.data = data

            async def evaluate(self, *_args, **_kwargs):
                return self.data

        fresh = asyncio.run(self.m._extract_apikeycasa_balance(
            FakePage({"code": 0, "message": "success",
                      "data": {"reward": 10, "checked_in": True, "date": "2026-11-13"}})
        ))
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh.reward_text, "+$10")
        self.assertEqual(fresh.amount, 10)
        self.assertEqual(fresh.currency, "USD")
        # 小数奖励不带多余 0
        frac = asyncio.run(self.m._extract_apikeycasa_balance(
            FakePage({"code": 0, "data": {"reward": 0.5, "checked_in": True}})
        ))
        self.assertEqual(frac.reward_text, "+$0.5")
        # reward<=0(签到暂未开启)/未签/接口失败 → 不产生奖励证据
        self.assertIsNone(asyncio.run(self.m._extract_apikeycasa_balance(
            FakePage({"code": 0, "data": {"reward": 0, "checked_in": True}})
        )))
        self.assertIsNone(asyncio.run(self.m._extract_apikeycasa_balance(
            FakePage({"code": 0, "data": {"reward": 0.5, "checked_in": False}})
        )))
        self.assertIsNone(asyncio.run(self.m._extract_apikeycasa_balance(FakePage(None))))

    # ---- 2026-10-27 审计修复:post-click 金额漏采 + 盲回退 CTA 守卫 ----

    def test_postclick_already_enriches_reward_from_wide_text(self):
        """grok-heavy/guxiaomo/hcnsec 漏采根因:强文本门只读 900 字符,长侧栏页把
        ~2.8s toast 挤出窗口;already_done 的 2000 字符窗命中裸「签到成功」后
        组装结果时丢弃收益上下文。修复后 post-click 已签路径必须从宽窗文本
        找回「获得 ¥N」行供 _enrich_result_balance 解析。"""
        import asyncio

        sidebar = "主页\n控制台\n模型广场\n" + "侧栏导航项\n" * 200  # >900 chars
        body = sidebar + "今日已签到\n已签到\n\n签到成功！获得 ¥36.84"
        action = self.m.ActionEvidence(
            kind="dom_click", target='button:has-text("立即签到")',
            attempted_at="2026-10-27T08:31:30",
        )
        page = _make_fake_selector_page(body, {})
        res = asyncio.run(self.m._postclick_enriched_done_result(
            page, "签到成功", kind="newapi_profile", action=action))
        self.assertEqual(res.status, "OK")
        self.assertIn("¥36.84", res.confirmation.summary)
        self.assertEqual(
            self.m._extract_reward_from_text(res.confirmation.summary), "+¥36.84")

    def test_postclick_enrichment_skips_precheck_and_nonparseable(self):
        """pre-click ALREADY(无点击)不得把站内记录/统计金额领成今日收益;
        宽窗里没有任何可解析收益行时保持原 summary。"""
        import asyncio

        action_none = self.m.ActionEvidence(kind="none")
        page = _make_fake_selector_page("已签到\n当前余额 $62.81\n累计获得 $308", {})
        res = asyncio.run(self.m._postclick_enriched_done_result(
            page, "btn:disabled:已签到", kind="newapi_profile", action=action_none))
        self.assertEqual(res.status, "ALREADY")
        self.assertEqual(res.confirmation.summary, "btn:disabled:已签到")
        # post-click 但宽窗无可解析收益行 → 原样保留
        action_click = self.m.ActionEvidence(kind="dom_click")
        res2 = asyncio.run(self.m._postclick_enriched_done_result(
            page, "btn:disabled:已签到", kind="newapi_profile", action=action_click))
        self.assertEqual(res2.confirmation.summary, "btn:disabled:已签到")

    def test_postclick_enrichment_cans_and_date_record_rejection(self):
        """ark717/bohe 同款场景:罐头收益行可被宽窗找回;带日期前缀的历史记录行
        (「10-26 签到成功 +¥2.6」)绝不能被领成今日收益。"""
        import asyncio

        action = self.m.ActionEvidence(kind="dom_click", target="sign")
        # 罐头:fallback 路径(confirm_signal_line 找不到含信号可解析行 → 上下文兜底)
        page_can = _make_fake_selector_page("已签到\n签到成功\n罐头+49.71", {})
        res_can = asyncio.run(self.m._postclick_enriched_done_result(
            page_can, "text-confirm", kind="arkengine", action=action))
        self.assertIn("罐头+49.71", res_can.confirmation.summary)
        self.assertEqual(
            self.m._extract_reward_from_text(res_can.confirmation.summary), "罐头+49.71")
        # 历史记录行:唯一可解析行带日期前缀 → 不富化
        page_rec = _make_fake_selector_page("已签到\n10-26 签到成功 +¥2.6", {})
        res_rec = asyncio.run(self.m._postclick_enriched_done_result(
            page_rec, "text-confirm", kind="newapi_profile", action=action))
        self.assertNotIn("+¥2.6", res_rec.confirmation.summary)

    def test_already_done_blind_sel_kept_when_cta_disabled(self):
        """守卫只压制启用中的 CTA:已签页按钮翻转 disabled 时,信任选择器命中
        仍走盲回退(nodeseek 类真阳性路径保留)。"""
        import asyncio

        page = _make_fake_selector_page(
            "已签到",
            {
                "text=已签到": dict(count=1, visible=True, disabled=False,
                                    label="已签到"),
                'button:has-text("立即签到")': dict(count=1, visible=True, disabled=True),
            },
        )
        self.assertEqual(asyncio.run(self.m.already_done(
            page, ["text=已签到"], trusted_already=True)), "sel:text=已签到")

    def test_abuai_button_guard_short_circuits_when_logged_out(self):
        """登出态(登录页)直接跳过轮询,不在 dismiss 每阶段烧满 12s。"""
        import asyncio

        page = _make_fake_selector_page("请先登录后再签到。\n用户名或密码", {})
        self.assertFalse(asyncio.run(
            self.m._ensure_abuai_checkin_button_ready(page, max_wait_s=12.0)))

    def test_already_done_blind_sel_guarded_by_enabled_cta(self):
        """信任选择器盲回退守卫:页面正文无业务确认 + 启用中的 立即签到 CTA
        在场时,text=已签到 命中常驻说明文案不得判 ALREADY(zerocat 教训);
        无 CTA 时保留盲回退(nodeseek 类站点依赖)。"""
        import asyncio

        zerocat_unsigned = (
            "个人设置\n每日签到\n今天\n\n27\n\n星期二\n\n每天一点小收获\n\n"
            "每日签到可获得随机额度奖励\n\n立即签到\n2026-10\n\n当月奖励 ¥0.2353\n\n"
            "27\n今天\n28\n29\n30\n31\n\n点选已签到日期，查看奖励\n\n累计签到\n46"
        )
        page_cta = _make_fake_selector_page(
            zerocat_unsigned,
            {
                "text=已签到": dict(count=1, visible=True, disabled=False,
                                    label="点选已签到日期，查看奖励"),
                'button:has-text("立即签到")': dict(count=1, visible=True, disabled=False),
            },
        )
        self.assertEqual(asyncio.run(self.m.already_done(
            page_cta, ["text=已签到"], trusted_already=True)), "")
        page_no_cta = _make_fake_selector_page(
            zerocat_unsigned,
            {"text=已签到": dict(count=1, visible=True, disabled=False,
                                 label="点选已签到日期，查看奖励")},
        )
        self.assertEqual(asyncio.run(self.m.already_done(
            page_no_cta, ["text=已签到"], trusted_already=True)), "sel:text=已签到")

    def test_linuxdo_authorize_and_turnstile_contracts(self):
        auth = list(self.m.AUTHORIZE_SELECTORS)
        self.assertTrue(any("身份授权" in sel for sel in auth))
        self.assertTrue(any("授权" in sel for sel in auth))
        self.assertFalse(any("确认" in sel for sel in auth))
        self.assertFalse(any("继续" in sel or "Continue" in sel for sel in auth))
        # CAPTCHA_WAIT_S 默认 40s;生产 env.sh 会压到 30s。断言默认值必须摘掉 env,
        # 否则 source env.sh 后跑全量会假失败(与 _CleanEnvMixin 同一个坑)。
        with clean_env():
            self.assertEqual(load_mod(force=True).CAPTCHA_WAIT_S, 40.0)
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


class TestCaptchaGateAlign(unittest.TestCase):
    def test_wait_out_uses_strict_confirm(self):
        src = TARGET.read_text(encoding="utf-8")
        m = re.search(r"async def wait_out_captcha\(.*?\n(?=def |async def )", src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("is_valid_checkin_confirm", body)
        # bare list with 已领取 alone should not be the gate
        self.assertNotIn('"已领取",', body)


class TestConfirmGateSourceGuards(unittest.TestCase):
    """源码守卫:确认/成功判据不允许放宽成无 before 对比或吞掉 success=false."""

    def setUp(self):
        self.m = __import__("stealth_checkin_runner")
        with open(self.m.__file__, encoding="utf-8") as f:
            self.src = f.read()

    def test_signin_api_success_field_has_priority(self):
        """success 字段存在时必须以其为准;code===200 不得覆盖 success===false."""
        self.assertIn("typeof body.success === 'boolean'", self.src)

    def test_nexa_dismiss_loop_is_bounded(self):
        self.assertIn("count < 10 && ann.currentPopup", self.src)

    def test_nexa_dismiss_never_removes_unknown_modal(self):
        self.assertNotIn("m.remove()", self.src)

    def test_relayfor_confirm_uses_shared_text_window(self):
        self.assertIn("RELAYFOR_TEXT_WINDOW", self.src)
        self.assertNotIn("before = await page_text(page, 1500)", self.src)


class TestEmrankAntiPattern(unittest.TestCase):
    """emrank 与 huaweicloud 同款常驻「已连续签到（天）」标签, 同样严禁作为已签判据.

    2026-10-15 评审 P2-latent 修复: 从内置 adapter 与 sites.yaml 移除 'text=已连续签到'
    (trusted_already 下会假 ALREADY), 防止同类事故在 emrank 复现。
    """

    def setUp(self):
        self.m = load_mod(force=True)

    def test_emrank_already_excludes_continuous_stat_label(self):
        from stealth_checkin_runner import _BUILTIN_SITE_ADAPTERS
        adapter = next(x for x in _BUILTIN_SITE_ADAPTERS if x.name == "emrank")
        self.assertNotIn(
            "text=已连续签到",
            adapter.already_selectors,
            "emrank 内置 adapter 不得以『已连续签到（天）』为已签判据",
        )
        resolved = self.m.resolve_site("emrank", "https://emrank.tech/account")
        self.assertNotIn("text=已连续签到", resolved.already_selectors,
                         "emrank resolve_site 兜底亦不得含『已连续签到（天）』")
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "emrank"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 emrank 条目")
        self.assertNotIn("text=已连续签到", entry.get("already", []),
                         "sites.yaml emrank already 不得含『已连续签到（天）』")


if __name__ == "__main__":
    unittest.main()

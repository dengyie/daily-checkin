#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from pathlib import Path
import tempfile
import unittest

from tests._support import (
    TARGET,
    load_mod,
)


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

    def test_projection_sink_preflight_accepts_already_checked_row(self):
        """已勾选的投影行同样是可用投影:重跑已完成站(--only / Web 面板单站重跑)
        时 apply_task_success 只需清 #auto-fail,不必再翻 checkbox。
        2026-10-29 nailao 实测:该行已 [x](手动确认),预检却只认未勾选行 →
        恒 task_projection_missing → 拒绝运行。"""
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "daily.md"
            content = "- [x] #task #日常 [A](https://a.example)\n"
            fp.write_text(content, encoding="utf-8")
            self.assertIsNone(
                self.m.validate_task_projection_sink(str(fp), content, ["A"])
            )
            self.assertEqual(
                self.m.validate_task_projection_sink(str(fp), content, ["B"]),
                "task_projection_missing:B",
            )

    def test_projection_sink_preflight_missing_is_fail_closed_reason(self):
        with tempfile.TemporaryDirectory() as td:
            reason = self.m.validate_task_projection_sink(
                str(Path(td) / "missing.md"), "", ["A"]
            )
            self.assertTrue(reason.startswith("task_file_missing:"))


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

    def test_resurrect_warning_scenarios(self):
        """Re-seed with prior DB rows must warn; first materialize and
        plain reads must stay silent (iCloud rollback of 2026-11-13)."""
        import contextlib
        import io

        def capture(path, day, prior, created):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.m.warn_if_task_file_resurrected(path, day, prior, created)
            return buf.getvalue()

        # vanished mid-day (DB rows exist) -> loud warning
        out = capture("/x/2026-11-13 每日任务.md", "2026-11-13", 121, True)
        self.assertIn("vanished and was re-seeded", out)
        self.assertIn("121", out)
        # first materialize of the day (no DB rows yet) -> silent
        self.assertEqual(capture("/x/2026-11-14 每日任务.md", "2026-11-14", 0, True), "")
        # file existed (read-only path) -> silent regardless of DB rows
        self.assertEqual(capture("/x/2026-11-13 每日任务.md", "2026-11-13", 121, False), "")


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


if __name__ == "__main__":
    unittest.main()

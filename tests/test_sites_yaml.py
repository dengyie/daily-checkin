#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from pathlib import Path
from unittest import mock
import asyncio
import inspect
import os
import sys
import tempfile
import pytest
import unittest
import yaml

from tests._support import (
    ROOT,
    load_mod,
)

pytestmark = [pytest.mark.runner]


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

    def test_bad_site_timeout_degrades_to_batch_default(self):
        """Malformed or negative site_timeout_s degrades to 0 (use batch default)."""
        for raw in ("abc", "-5"):
            p = self._write_yaml(f"""sites:
  - name: 咕嘎咕嘎生图站
    url: https://ai.xmiaom.com/dashboard/overview
    kind: xmiaom
    site_timeout_s: {raw}
""")
            adapters = self.m.load_sites_from_yaml(p)
            self.assertEqual(len(adapters), 1, raw)
            self.assertEqual(adapters[0].site_timeout_s, 0.0, raw)

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

    def _patch_accounts_fixture(self):
        """用临时账密 fixture 替换真实 vault 文件.

        测试必须与私密凭据文档解耦:CI/他机无 iCloud vault,且公开仓库
        不得断言真实账号值(2026-10-01 CI 修复)。
        """
        fixture_md = (
            "0api（https://sub2api.0api.me）\n"
            "ci-account-0api@example.invalid\n"
            "ci-password-0api\n"
            "\n"
            "hcnsec（https://api.hcnsec.cn）\n"
            "ci-account-hcnsec\n"
            "ci-password-hcnsec\n"
            "\n"
            "sub2api（https://sub2api.0api.me）\n"
            "ci-account-sub2api\n"
            "ci-password-sub2api\n"
            "\n"
            "nailao（https://nailao.biz）\n"
            "ci-account-nailao\n"
            "ci-password-nailao\n"
            "\n"
            "tokenrouter（https://www.tokenrouter.tech）\n"
            "ci-account-tokenrouter\n"
            "ci-password-tokenrouter\n"
            "\n"
            "leileihog（https://fapi.leileihog.top）\n"
            "ci-account-leileihog\n"
            "ci-password-leileihog\n"
        )
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(fixture_md)
        self.addCleanup(lambda: os.unlink(path))
        p_file = mock.patch.object(self.m, "ACCOUNTS_FILE", Path(path))
        p_cache = mock.patch.object(self.m, "_ACCOUNTS_CACHE", None)
        p_read = mock.patch.object(self.m, "read_accounts_file", return_value=fixture_md)
        p_file.start()
        p_cache.start()
        p_read.start()
        self.addCleanup(p_file.stop)
        self.addCleanup(p_cache.stop)
        self.addCleanup(p_read.stop)

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

    def test_yaml_registers_hiyo_entry(self):
        """sites.yaml 的 hiyo 条目解析为 kind=browser,且带立即签到/今日已签到签名。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "hiyo"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 hiyo 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://free.hiyo.top/dashboard")
        joined = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined)
        self.assertTrue(a.already_selectors)

    def test_yaml_registers_zerocat_entry(self):
        """zerocat 2026-10-27 改版守卫:URL 迁到 /profile/checkin,already 必须
        钉死 button:text-is 精确匹配——页面常驻「点选已签到日期」说明文案,
        任何 text= 已签到/签到成功 子串选择器都会在未签日假 ALREADY。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "zerocat"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 zerocat 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://zero.cat/profile/checkin")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn('button:has-text("立即签到")', joined_signs)
        # 子串选择器地雷清除:already 不得含裸 text= 或宽松 button:has-text 已签到
        joined_already = " ".join(a.already_selectors)
        self.assertNotIn("text=已签到", joined_already)
        self.assertNotIn("text=签到成功", joined_already)
        self.assertNotIn('button:has-text("已签到")', joined_already)
        for sel in a.already_selectors:
            self.assertTrue(
                sel.startswith("button:text-is("),
                f"zerocat already 选择器必须精确 button:text-is: {sel}",
            )
        # LinuxDO OAuth 站:不得配置 prefer_cta_before_auth(未登录必须先走 SSO)
        self.assertFalse(a.prefer_cta_before_auth)

    def test_yaml_registers_aihappy_entry(self):
        """sites.yaml 的 aihappy 条目解析为 kind=browser,带 quota bar 精确选择器与已签签名。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "aihappy"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 aihappy 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://images.aihappy.indevs.in/#/profile")
        joined = " ".join(a.sign_selectors)
        self.assertIn("user-quota-checkin", joined)
        self.assertIn("签到", joined)
        self.assertTrue(a.already_selectors)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("已签到", joined_already)
        # LinuxDO OAuth 站:不得配置 prefer_cta_before_auth(未登录必须先走 SSO)
        self.assertFalse(a.prefer_cta_before_auth)

    def test_yaml_registers_qkmss_entry(self):
        """sites.yaml 的 qkmss 条目解析为 kind=browser,URL 为 /user/checkin 且 prefer_catalog_url=True。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "qkmss"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 qkmss 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://qkmss.com/user/checkin")
        self.assertTrue(a.prefer_catalog_url)
        joined = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined)
        # 验证就算传入旧 /profile note URL,prefer_catalog_url 也会自动胜出保持新 URL
        resolved = resolve_site("qkmss", "https://qkmss.com/profile")
        self.assertEqual(resolved.url, "https://qkmss.com/user/checkin")

    def test_yaml_registers_xiadengwang_entry(self):
        """sites.yaml 的 虾蹬王 条目解析为 kind=browser,带 button#btn 立即签到及今日已签到回执，且设置 prefer_cta_before_auth=True。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "虾蹬王"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 虾蹬王 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://checkin.kunyou.asia/")
        self.assertTrue(a.prefer_cta_before_auth)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn('button#btn:has-text("立即签到")', joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn('button#btn:has-text("今日已签到")', joined_already)
        self.assertIn('text=今日已签到', joined_already)
        resolved = resolve_site("虾蹬王", "https://checkin.kunyou.asia/")
        self.assertEqual(resolved.url, "https://checkin.kunyou.asia/")

    def test_yaml_registers_deadlion_entry(self):
        """sites.yaml 的 deadlion 条目解析为 kind=browser,/console 立即签到与今日已签到回执。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "deadlion"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 deadlion 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://free.deadlion.cn/console")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn('button:has-text("立即签到")', joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn('button:has-text("今日已签到")', joined_already)
        self.assertIn("text=今日已签到", joined_already)
        resolved = resolve_site("deadlion", "https://free.deadlion.cn/console")
        self.assertEqual(resolved.url, "https://free.deadlion.cn/console")

    def test_yaml_registers_motomoto_entry(self):
        """sites.yaml 的 motomoto 条目解析为 kind=browser,/profile Check in now 与 Checked in 回执。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "motomoto"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 motomoto 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, done_button_signal, _DIALOG_DISMISS_JS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://motomoto.lol/profile")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn('button:has-text("Check in now")', joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn('button:has-text("Checked in")', joined_already)
        self.assertIn("text=Checked in", joined_already)
        resolved = resolve_site("motomoto", "https://motomoto.lol/profile")
        self.assertEqual(resolved.url, "https://motomoto.lol/profile")
        self.assertEqual(resolved.kind, "browser")

        # 验证 Checked in 的 disabled 与 card 判定
        self.assertEqual(done_button_signal("Checked in", True), "btn:disabled:Checkedin")
        self.assertEqual(done_button_signal("Checked in", False), "")
        self.assertEqual(done_button_signal("Daily Check-in\nChecked in\nToday +$50", False), "btn:card:checkedin")

        # 验证弹窗遮罩排除机制包含「我已知悉」及安全验证/Turnstile/Captcha 保护
        self.assertIn("我已知悉", _DIALOG_DISMISS_JS)
        self.assertIn("安全验证", _DIALOG_DISMISS_JS)
        self.assertIn("cf-turnstile-response", _DIALOG_DISMISS_JS)

    def test_yaml_registers_bxacc_entry(self):
        """sites.yaml 的 bxacc 条目解析为 newapi_profile,/profile,prefer_catalog_url。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "bxacc"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 bxacc 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://api.bxacc.xyz/profile")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertNotIn("每日仅可签到一次", joined_already)
        resolved = resolve_site("bxacc", "https://api.bxacc.xyz/")
        self.assertEqual(resolved.url, "https://api.bxacc.xyz/profile")

    def test_yaml_registers_techmob_entry(self):
        """sites.yaml 的 techmob 条目解析为 newapi_profile,/profile,prefer_catalog_url。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "techmob"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 techmob 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://newapi.do.techmob.net/profile")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertNotIn("每日仅可签到一次", joined_already)
        resolved = resolve_site("techmob", "https://newapi.do.techmob.net/")
        self.assertEqual(resolved.url, "https://newapi.do.techmob.net/profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "techmob"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 techmob")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://newapi.do.techmob.net/profile")
        self.assertTrue(builtin_entry.prefer_catalog_url)

    def test_yaml_registers_si_entry(self):
        """sites.yaml 的 si 条目解析为 newapi_profile,/profile,prefer_catalog_url。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "si"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 si 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://si.99442200.xyz/profile")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertNotIn("每日仅可签到一次", joined_already)
        resolved_si = resolve_site("si", "http://si.99442200.xyz/profile")
        self.assertEqual(resolved_si.url, "https://si.99442200.xyz/profile")
        self.assertEqual(resolved_si.kind, "newapi_profile")
        resolved_furry = resolve_site("furry API", "http://si.99442200.xyz/profile")
        self.assertEqual(resolved_furry.url, "https://si.99442200.xyz/profile")
        self.assertEqual(resolved_furry.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "si"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 si")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://si.99442200.xyz/profile")
        self.assertTrue(builtin_entry.prefer_catalog_url)

    def test_yaml_registers_yuheng_entry(self):
        """sites.yaml 的 yuheng 条目解析为 newapi_profile,/profile,prefer_catalog_url。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "yuheng"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 yuheng 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://yuheng.site/profile")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertNotIn("每日仅可签到一次", joined_already)
        resolved_yuheng = resolve_site("yuheng", "https://yuheng.site/profile")
        self.assertEqual(resolved_yuheng.url, "https://yuheng.site/profile")
        self.assertEqual(resolved_yuheng.kind, "newapi_profile")
        resolved_alias = resolve_site("玉衡中转", "https://yuheng.site/profile")
        self.assertEqual(resolved_alias.url, "https://yuheng.site/profile")
        self.assertEqual(resolved_alias.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "yuheng"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 yuheng")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://yuheng.site/profile")
        self.assertTrue(builtin_entry.prefer_catalog_url)

    def test_yaml_registers_0api_entry(self):
        """sites.yaml 的 0api 条目解析为 sub2api,/custom/activity,prefer_catalog_url,且账密注册有效。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "0api"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 0api 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "sub2api")
        self.assertEqual(a.url, "https://sub2api.0api.me/custom/activity")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("#checkin", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签", joined_already)
        resolved = resolve_site("0api", "https://newapi.0api.cc.cd/profile")
        self.assertEqual(resolved.url, "https://sub2api.0api.me/custom/activity")
        self.assertEqual(resolved.kind, "sub2api")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "0api"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 0api")
        self.assertEqual(builtin_entry.kind, "sub2api")
        self.assertEqual(builtin_entry.url, "https://sub2api.0api.me/custom/activity")
        self.assertTrue(builtin_entry.prefer_catalog_url)
        cred = get_account_credential("0api")
        self.assertIsNotNone(cred)
        self.assertEqual(cred[0], "ci-account-0api@example.invalid")
        self.assertEqual(cred[1], "ci-password-0api")

    def test_yaml_registers_happycoding_entry(self):
        """sites.yaml 的 happycoding 条目解析为 newapi_profile,/profile,prefer_catalog_url。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "happycoding"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 happycoding 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://happycoding.xyz/profile")
        self.assertTrue(a.prefer_catalog_url)
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertNotIn("每日仅可签到一次", joined_already)
        resolved = resolve_site("happycoding", "https://happycoding.xyz/profile")
        self.assertEqual(resolved.url, "https://happycoding.xyz/profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "happycoding"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 happycoding")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://happycoding.xyz/profile")
        self.assertTrue(builtin_entry.prefer_catalog_url)

    def test_yaml_registers_nailao_entry(self):
        """sites.yaml 的 nailao 条目解析为标准 newapi_profile 通道,/profile。

        2026-10-29 实证站点改回标准 New API 形态:/sign-in 会话有效时直接跳
        /dashboard/overview,refresh cookie 恢复有效,/profile 恢复「每日签到」
        卡片(立即签到 → POST /api/user/checkin → 已签到 disabled + 今天 +🧀 N)。
        2026-10-27 版纯 Bearer 专用流程已非必要,回退通用通道;专用 dispatch 仅在
        显式 kind=nailao 时触发(域名/名称劫持已移除,否则标准通道永远进不去)。
        """
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "nailao"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 nailao 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://nailao.biz/profile")
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("nailao", None)
        self.assertEqual(resolved.kind, "newapi_profile")
        self.assertEqual(resolved.url, "https://nailao.biz/profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "nailao"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 nailao")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://nailao.biz/profile")
        self.assertTrue(builtin_entry.prefer_catalog_url)

    def test_nailao_domain_does_not_hijack_standard_flow(self):
        """nailao 回退标准通道的根因回归:legacy dispatch 不得再按域名/名称把
        nailao.biz 劫持进专用 nailao_checkin——2026-10-29 晨批 root cause 即
        「kind=newapi_profile 但 is_nailao_site(site_url) 命中 → 专用流程拦截器
        只盯 /api/user/*,refresh cookie 恢复后 Bearer 出现在 /api/status 等请求
        → 抓不到 token → 恒 auth_required」。只有显式 kind=nailao 才走后备。"""
        import inspect
        import stealth_checkin_runner as m
        src = inspect.getsource(m.legacy_checkin_on_page)
        needle = 'if kind == "nailao":'
        self.assertIn(needle, src)
        idx = src.index(needle)
        window = src[idx:idx + 200]
        self.assertNotIn("is_nailao_site", window)
        self.assertNotIn("is_nailao_name", window)

    def test_yaml_registers_hcnsec_entry(self):
        """sites.yaml 的 hcnsec 条目解析为 newapi_profile,/profile,prefer_catalog_url,且账密注册有效。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "hcnsec"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 hcnsec 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://api.hcnsec.cn/profile")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.login_url, "https://api.hcnsec.cn/sign-in")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertNotIn("每日仅可签到一次", joined_already)
        resolved = resolve_site("hcnsec", "https://api.hcnsec.cn/dashboard/overview")
        self.assertEqual(resolved.url, "https://api.hcnsec.cn/profile")
        self.assertEqual(resolved.login_url, "https://api.hcnsec.cn/sign-in")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "hcnsec"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 hcnsec")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://api.hcnsec.cn/profile")
        self.assertTrue(builtin_entry.prefer_catalog_url)
        self.assertEqual(builtin_entry.login_url, "https://api.hcnsec.cn/sign-in")
        cred = get_account_credential("hcnsec")
        self.assertIsNotNone(cred)
        self.assertEqual(cred[0], "ci-account-hcnsec")
        self.assertTrue(len(cred[1]) > 0)

    def test_yaml_registers_tokenrouter_entry(self):
        """sites.yaml 的 tokenrouter 条目解析为 newapi_profile,/console/personal,/login,且账密注册有效。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "tokenrouter"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 tokenrouter 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://www.tokenrouter.tech/console/personal")
        self.assertEqual(a.login_url, "https://www.tokenrouter.tech/login")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        resolved = resolve_site("tokenrouter", "https://www.tokenrouter.tech/console/personal")
        self.assertEqual(resolved.url, "https://www.tokenrouter.tech/console/personal")
        self.assertEqual(resolved.login_url, "https://www.tokenrouter.tech/login")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "tokenrouter"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 tokenrouter")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://www.tokenrouter.tech/console/personal")
        self.assertEqual(builtin_entry.login_url, "https://www.tokenrouter.tech/login")
        cred = get_account_credential("tokenrouter")
        self.assertIsNotNone(cred)
        self.assertEqual(cred[0], "ci-account-tokenrouter")
        self.assertTrue(len(cred[1]) > 0)

    def test_yaml_registers_ai_facai_entry(self):
        """sites.yaml 的 AI 发财网条目解析为 newapi_profile/dashboard,且无账密(LinuxDO 登录)。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "AI 发财网"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 AI 发财网 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://ai.facai.cloudns.org/dashboard")
        self.assertEqual(a.login_url, "")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("已签到", joined_already)
        resolved = resolve_site("AI 发财网", "https://ai.facai.cloudns.org/dashboard")
        self.assertEqual(resolved.url, "https://ai.facai.cloudns.org/dashboard")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "AI 发财网"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 AI 发财网")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://ai.facai.cloudns.org/dashboard")
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("AI 发财网"))

    def test_yaml_registers_hyxapi_entry(self):
        """sites.yaml 的 HyxAPI 条目解析为 newapi_profile//topup,且无账密(LinuxDO 登录)。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "HyxAPI"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 HyxAPI 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://hyxware.com/topup")
        self.assertEqual(a.login_url, "")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("已签到", joined_already)
        resolved = resolve_site("HyxAPI", "https://hyxware.com/topup")
        self.assertEqual(resolved.url, "https://hyxware.com/topup")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "HyxAPI"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 HyxAPI")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://hyxware.com/topup")
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("HyxAPI"))

    def test_yaml_registers_abuai_entry(self):
        """sites.yaml 的 AbuAI 条目解析为 newapi_profile/welfare,sign 钉死「领取今日签到奖励」,无账密。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "AbuAI"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 AbuAI 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://www.abuai.chat/console/welfare")
        self.assertEqual(a.login_url, "")
        # 福利中心同页有抽奖/补签/每周领取按钮,sign 必须钉死精确文案而非裸「签到」
        self.assertIn("领取今日签到奖励", " ".join(a.sign_selectors))
        self.assertIn("今日已签到", " ".join(a.already_selectors))
        resolved = resolve_site("AbuAI", "https://www.abuai.chat/console/welfare")
        self.assertEqual(resolved.url, "https://www.abuai.chat/console/welfare")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "AbuAI"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 AbuAI")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://www.abuai.chat/console/welfare")
        self.assertIn("领取今日签到奖励", " ".join(builtin_entry.sign_selectors))
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("AbuAI"))

    def test_yaml_registers_superqiyu_entry(self):
        """sites.yaml 的 superqiyu 条目解析为 newapi_profile//profile,通用签到选择器,无账密。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "superqiyu"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 superqiyu 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://www.superqiyu.org/profile")
        self.assertEqual(a.login_url, "")
        # 标准 New API 默认前端:走通用「签到/已签到」判定,无需自定义选择器
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        resolved = resolve_site("superqiyu", "https://www.superqiyu.org/profile")
        self.assertEqual(resolved.url, "https://www.superqiyu.org/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "superqiyu"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 superqiyu")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://www.superqiyu.org/profile")
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("superqiyu"))

    def test_yaml_registers_ssftt_entry(self):
        """sites.yaml 的 ssftt 条目解析为 newapi_profile//profile,login_url 显式,纯账密站。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "ssftt"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 ssftt 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://api.ssftt.vip/profile")
        # 纯账密站:标准登录入口显式钉死,避免派生路径猜错
        self.assertEqual(a.login_url, "https://api.ssftt.vip/login")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        resolved = resolve_site("ssftt", "https://api.ssftt.vip/profile")
        self.assertEqual(resolved.url, "https://api.ssftt.vip/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "ssftt"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 ssftt")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://api.ssftt.vip/profile")
        self.assertEqual(builtin_entry.login_url, "https://api.ssftt.vip/login")

    def test_yaml_registers_azurekiln_entry(self):
        """sites.yaml 的 azurekiln 条目解析为 newapi_profile//profile,LinuxDO OAuth 无账密。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "azurekiln"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 azurekiln 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://newapi.azurekiln.cn/profile")
        self.assertEqual(a.login_url, "")
        # 标准 New API 默认前端:走通用「签到/已签到」判定,无需自定义选择器
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        # yaml 显式 prefer_catalog_url,避免任务行 URL 漂移
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("azurekiln", "https://newapi.azurekiln.cn/profile")
        self.assertEqual(resolved.url, "https://newapi.azurekiln.cn/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "azurekiln"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 azurekiln")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://newapi.azurekiln.cn/profile")
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("azurekiln"))

    def test_yaml_registers_lingjing_entry(self):
        """灵境公益站(6661886.xyz) 2026-10-28 接入:标准 LinuxDO+New API 站,
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "lingjing"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 lingjing 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://6661886.xyz/profile")
        self.assertEqual(a.login_url, "")
        # 标准 New API 默认前端:走通用「签到/已签到」判定,无需自定义选择器
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("lingjing", "https://6661886.xyz/profile")
        self.assertEqual(resolved.url, "https://6661886.xyz/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "lingjing"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 lingjing")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://6661886.xyz/profile")
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("lingjing"))

    def test_yaml_registers_3sbqs_entry(self):
        """3sbqs(api.3sbqs.xyz) 2026-11-04 接入:标准 LinuxDO + New API 站,
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "3sbqs"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 3sbqs 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://api.3sbqs.xyz/profile")
        self.assertEqual(a.login_url, "")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("3sbqs", "https://api.3sbqs.xyz/profile")
        self.assertEqual(resolved.url, "https://api.3sbqs.xyz/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "3sbqs"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 3sbqs")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://api.3sbqs.xyz/profile")
        # LinuxDO OAuth 站:不应登记账密
        self.assertIsNone(get_account_credential("3sbqs"))

    def test_yaml_registers_qingniaocoding_entry(self):
        """qingniaocoding(qingniaocoding.space) 2026-11-09 接入:标准
        LinuxDO + New API 站(version=v1.2.1, checkin_enabled=true),
        newapi_profile 通道,/wallet,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "qingniaocoding"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 qingniaocoding 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://qingniaocoding.space/wallet")
        self.assertEqual(a.login_url, "")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("qingniaocoding", "https://qingniaocoding.space/wallet")
        self.assertEqual(resolved.url, "https://qingniaocoding.space/wallet")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "qingniaocoding"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 qingniaocoding")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://qingniaocoding.space/wallet")
        # LinuxDO OAuth 站:不应登记账密
        self.assertIsNone(get_account_credential("qingniaocoding"))

    def test_yaml_registers_apikey_casa_entry(self):
        """apikey.casa(福利 apikey.casa) 2026-11-10 接入:标准
        LinuxDO + New API 站(version=v1.0.0-rc.49, checkin_enabled=true),
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "apikey.casa"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 apikey.casa 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://apikey.casa/profile")
        self.assertEqual(a.login_url, "")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("apikey.casa", "https://apikey.casa/profile")
        self.assertEqual(resolved.url, "https://apikey.casa/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "apikey.casa"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 apikey.casa")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://apikey.casa/profile")
        # LinuxDO OAuth 站:不应登记账密
        self.assertIsNone(get_account_credential("apikey.casa"))

    def test_yaml_registers_teleai_entry(self):
        """teleai(TeleAgent 星辰超级智能体 agent.teleai.com.cn/teleagent)
        2026-11-12 接入:中国电信自研 SPA(opencowork),非 New API、无
        LinuxDO OAuth,登录态走共享 9222 会话。「积分福利社」每日
        100 积分 button「立即领取」,点击 → /superCowork/sapi/user/portal/
        companyStore/checkTask?type=daily_login,已领显示「已领 N 天
        累计X积分」。kind=browser + prefer_catalog_url,不登记账密。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "teleai"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 teleai 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://agent.teleai.com.cn/teleagent")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.ready_rounds, 20)
        self.assertIn('button:has-text("立即领取")', " ".join(a.sign_selectors))
        joined_already = " ".join(a.already_selectors)
        # 2026-11-12 真机实测:已领态按钮/文案翻成「今日已领」(非「已领取，明天再来」),
        # 首跑 already 未命中落 no_button,补「今日已领」后幂等 ALREADY。
        self.assertIn("text=今日已领", joined_already)
        self.assertIn("text=已领取，明天再来", joined_already)
        resolved = resolve_site("teleai", "https://agent.teleai.com.cn/teleagent")
        self.assertEqual(resolved.url, "https://agent.teleai.com.cn/teleagent")
        self.assertEqual(resolved.kind, "browser")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "teleai"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 teleai(兜底守卫)")
        self.assertEqual(builtin_entry.kind, "browser")
        self.assertEqual(builtin_entry.url, "https://agent.teleai.com.cn/teleagent")
        self.assertEqual(builtin_entry.sign_selectors, a.sign_selectors)
        self.assertEqual(builtin_entry.already_selectors, a.already_selectors)
        # 电信站,无 LinuxDO/账密:不应登记凭据
        self.assertIsNone(get_account_credential("teleai"))

    def test_yaml_registers_fuli_entry(self):
        """伏枥(dash.free-ai.de5.net) 2026-11-05 接入:标准 LinuxDO + New API 站,
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "伏枥"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 伏枥 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://dash.free-ai.de5.net/profile")
        self.assertEqual(a.login_url, "")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("伏枥", "https://dash.free-ai.de5.net/profile")
        self.assertEqual(resolved.url, "https://dash.free-ai.de5.net/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "伏枥"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 伏枥")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://dash.free-ai.de5.net/profile")
        # LinuxDO OAuth 站:不应登记账密
        self.assertIsNone(get_account_credential("伏枥"))

    def test_yaml_registers_newcodex_entry(self):
        """newcodex(cdn.newcodex.cn) 2026-11-06 接入:标准 LinuxDO + New API 站,
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "newcodex"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 newcodex 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://cdn.newcodex.cn/profile")
        self.assertEqual(a.login_url, "")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("newcodex", "https://cdn.newcodex.cn/profile")
        self.assertEqual(resolved.url, "https://cdn.newcodex.cn/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "newcodex"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 newcodex")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://cdn.newcodex.cn/profile")
        # LinuxDO OAuth 站:不应登记账密
        self.assertIsNone(get_account_credential("newcodex"))

    def test_yaml_registers_laisiai_entry(self):
        """laisiai(laisiai.tw.cpolar.io / 莱斯超级API) 2026-11-07 接入:
        标准 New API 账密站(linuxdo_oauth=false),按用户要求不登录、不登记账密,
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "laisiai"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 laisiai 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://laisiai.tw.cpolar.io/profile")
        self.assertEqual(a.login_url, "")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("laisiai", "https://laisiai.tw.cpolar.io/profile")
        self.assertEqual(resolved.url, "https://laisiai.tw.cpolar.io/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "laisiai"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 laisiai")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://laisiai.tw.cpolar.io/profile")
        # 用户明确要求不登录:不应登记账密
        self.assertIsNone(get_account_credential("laisiai"))

    def test_yaml_registers_hyper_entry(self):
        """hyper(ai.hyper.nyc.mn / 澎湃AI网关) 2026-11-07 接入:
        标准 New API 纯 GitHub OAuth 站(linuxdo_oauth=false/github_oauth=true),
        不登记账密、不登录,newapi_profile 通道,/profile,prefer_catalog_url。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "hyper"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 hyper 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://ai.hyper.nyc.mn/profile")
        self.assertEqual(a.login_url, "")
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("hyper", "https://ai.hyper.nyc.mn/profile")
        self.assertEqual(resolved.url, "https://ai.hyper.nyc.mn/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "hyper"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 hyper")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://ai.hyper.nyc.mn/profile")
        # 纯 GitHub OAuth 站:不登记账密(登录态在 9222 profile)
        self.assertIsNone(get_account_credential("hyper"))

    def test_yaml_registers_qulink_entry(self):
        """qulink(qulink.cyou / 链享趣) 2026-11-07 接入:
        自研 Vite+React SPA(checkin_enabled=false,不走标准 newapi_profile),
        签到 /points/check-in,两态锁 button.qpc-claim(已签 disabled 翻「今日已签到」)。
        Custom browser selectors — 已签态不依赖裸 disabled(加载中同样 disabled)。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "qulink"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 qulink 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://qulink.cyou/points/check-in")
        signs = " ".join(a.sign_selectors)
        already = " ".join(a.already_selectors)
        self.assertIn("签到领 10 积分", signs)
        self.assertIn("今日已签到", already)
        self.assertIn("已领取，明天再来", already)
        # 加载中同是 disabled:已签选择器不允许裸 button.qpc-claim[disabled](假 ALREADY 坑)
        self.assertNotIn("qpc-claim[disabled]", already)
        self.assertIn(".qpc-claim:not([disabled])", signs)
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("qulink", "https://qulink.cyou/points/check-in")
        self.assertEqual(resolved.url, "https://qulink.cyou/points/check-in")
        self.assertEqual(resolved.kind, "browser")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "qulink"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 qulink")
        self.assertEqual(builtin_entry.kind, "browser")
        self.assertEqual(builtin_entry.url, "https://qulink.cyou/points/check-in")
        # 纯会话站,无账密凭据
        self.assertIsNone(get_account_credential("qulink"))

    def test_yaml_registers_leileihog_login_url_and_blocks_linuxdo_sso(self):
        """leileihog(fapi.leileihog.top / 一梦五千年) 2026-11-07 用户明确要求:
        登录只用站内用户名/密码,禁止 LinuxDO SSO。

        - sites.yaml 与内置 _B 都要钉死 login_url=/sign-in(/login 会 302 过去)
        - 账密必须已登记(runner 才能走 _ensure_account_logged_in)
        - 站点级判定 is_leileihog_site 命中,且 maybe_sso_if_needed 在
          allow_linuxdo=False 时直接 NO_BUTTON,绝不点 LinuxDO
        """
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "leileihog"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 leileihog 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS,
            get_account_credential, is_leileihog_site,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://fapi.leileihog.top/lottery")
        self.assertEqual(a.login_url, "https://fapi.leileihog.top/sign-in")
        self.assertTrue(a.prefer_catalog_url)
        # 抽奖两态选择器保留
        signs = " ".join(a.sign_selectors)
        already = " ".join(a.already_selectors)
        self.assertIn("lottery-board-center", signs)
        self.assertIn("今日已抽", already)
        # 内置回退与 yaml 必须一致
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "leileihog"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 leileihog")
        self.assertEqual(builtin_entry.login_url, "https://fapi.leileihog.top/sign-in")
        # 账密已登记(否则 _ensure_account_logged_in 直接短路,只剩 SSO)
        self.assertIsNotNone(get_account_credential("leileihog"))
        # 站点级 SSO 屏蔽判定
        self.assertTrue(is_leileihog_site("https://fapi.leileihog.top/lottery"))
        self.assertTrue(is_leileihog_site("https://leileihog.top/"))
        self.assertFalse(is_leileihog_site("https://example.com/lottery"))

    def test_maybe_sso_if_needed_hard_disabled_when_allow_linuxdo_false(self):
        """allow_linuxdo=False 时必须在碰页面之前就返回 NO_BUTTON(不点 LinuxDO)。"""
        import asyncio
        import stealth_checkin_runner as r

        class _BoomPage:
            async def goto(self, *a, **k):
                raise AssertionError("must not navigate")

        async def _run():
            return await r.maybe_sso_if_needed(
                _BoomPage(), force=True, allow_linuxdo=False
            )

        self.assertEqual(asyncio.run(_run()), "NO_BUTTON")

    def test_yaml_registers_loxo_entry(self):
        """loxo(loxo.cc) 2026-10-30 接入:自研 Vue SPA + 纯 LinuxDO OAuth。

        签到两态锁 data-testid(check-in-button / check-in-today-done),
        UI 可切中英文,不能用文案选择器。
        """
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "loxo"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 loxo 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, get_account_credential,
            _BUILTIN_SITE_ADAPTERS,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://loxo.cc/check-in")
        self.assertTrue(a.prefer_catalog_url)
        self.assertIn('button[data-testid="check-in-button"]', a.sign_selectors)
        self.assertIn('[data-testid="check-in-today-done"]', a.already_selectors)
        # 选择器必须语言无关:不得出现中文文案
        joined = " ".join(a.sign_selectors + a.already_selectors)
        self.assertNotIn("立即签到", joined)
        self.assertNotIn("今日已签到", joined)
        # 显式给出 signs/already 才会把选择器标为可信;不可信时 already_done 会按
        # 中文白名单过滤,data-testid 直接失效(英文 UI 下静默漏判已签)。
        self.assertTrue(a.trusted_sign_selectors, "loxo 选择器必须标记为可信")
        self.assertTrue(a.trusted_already_selectors, "loxo 已签选择器必须标记为可信")
        resolved = resolve_site("loxo", "https://loxo.cc/check-in")
        self.assertEqual(resolved.kind, "browser")
        self.assertEqual(resolved.url, "https://loxo.cc/check-in")
        self.assertTrue(resolved.trusted_already_selectors)
        # 纯 LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("loxo"))
        # builtin 兜底也要有:yaml 读不到时不能静默丢站
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "loxo"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 loxo")
        self.assertEqual(builtin_entry.kind, "browser")
        self.assertEqual(builtin_entry.url, "https://loxo.cc/check-in")
        self.assertTrue(builtin_entry.trusted_already_selectors)

    def test_yaml_registers_xkrelay_entry(self):
        """xkrelay(深空 API) 2026-11-04 接入:S 站内 /qq-checkin 取代 QQ 群签到。

        New API 定制分支(checkin_enabled=false,自研 QQ 奖励模块),两态是
        button「今日签到」→「今日已签到」。守卫两个易错点:
        ① already 选择器不能把页面常驻的「签到方法」说明/侧栏「签到领奖」
           命中成已签(「今日已签到」与「今日签到」互不为子串);
        ② 不能登记成 signin_api 站(端点是站方内部实现,未取证前不许硬编码)。
        """
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "xkrelay"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 xkrelay 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://xkrelay.com/qq-checkin")
        self.assertTrue(a.prefer_catalog_url, "钉 yaml URL,防任务行旧地址回灌")
        self.assertEqual(a.signin_api, "", "未取证的签到端点不得硬编码")
        self.assertIn('button:has-text("今日签到")', a.sign_selectors)
        self.assertIn('button:has-text("今日已签到")', a.already_selectors)
        # 已签选择器必须是完整短语:裸「签到」/「已签到」会被日历说明与导航误命中。
        for sel in a.already_selectors:
            self.assertNotEqual(sel.strip(), "text=已签到")
            self.assertNotEqual(sel.strip(), "text=签到")
        self.assertTrue(a.trusted_sign_selectors)
        self.assertTrue(a.trusted_already_selectors)
        resolved = resolve_site("xkrelay", "https://xkrelay.com/qq-checkin")
        self.assertEqual(resolved.kind, "browser")
        self.assertEqual(resolved.url, "https://xkrelay.com/qq-checkin")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "xkrelay"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 xkrelay")
        self.assertEqual(builtin_entry.url, "https://xkrelay.com/qq-checkin")
        self.assertTrue(builtin_entry.trusted_already_selectors)

    def test_yaml_registers_pigeonw_cf_entry(self):
        """pigeonw-cf(cloudflare.pigeonw.com) 2026-10-30 接入:自研 Vue 3 SPA +
        纯 LinuxDO OAuth,签到按钮「签到领取」,浏览器内跑 PoW;builtin 兜底同 loxo。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "pigeonw-cf"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 pigeonw-cf 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://cloudflare.pigeonw.com/#/dashboard")
        self.assertTrue(a.prefer_catalog_url)
        self.assertTrue(a.trusted_sign_selectors)
        self.assertTrue(a.trusted_already_selectors)
        joined = " ".join(a.sign_selectors)
        self.assertIn("签到领取", joined)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "pigeonw-cf"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 pigeonw-cf")
        self.assertEqual(builtin_entry.url, "https://cloudflare.pigeonw.com/#/dashboard")

    def test_yaml_registers_mohua_entry(self):
        """墨华Mo(api.mohuaai.cn) 2026-10-29 接入:标准 LinuxDO+New API 站,
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "mohua"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 mohua 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://api.mohuaai.cn/profile")
        self.assertEqual(a.login_url, "")
        # 标准 New API 默认前端:走通用「签到/已签到」判定,无需自定义选择器
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("mohua", "https://api.mohuaai.cn/profile")
        self.assertEqual(resolved.url, "https://api.mohuaai.cn/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "mohua"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 mohua")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://api.mohuaai.cn/profile")
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("mohua"))


    def test_yaml_registers_akmusi_entry(self):
        """New API(ai.akmusi.online) 2026-10-29 接入:标准 LinuxDO+New API 站,
        newapi_profile 通道,/profile,prefer_catalog_url,无自定义选择器。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "akmusi"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 akmusi 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://ai.akmusi.online/profile")
        self.assertEqual(a.login_url, "")
        # 标准 New API 默认前端:走通用「签到/已签到」判定,无需自定义选择器
        self.assertIn("签到", " ".join(a.sign_selectors))
        self.assertIn("已签到", " ".join(a.already_selectors))
        self.assertTrue(a.prefer_catalog_url)
        resolved = resolve_site("akmusi", "https://ai.akmusi.online/profile")
        self.assertEqual(resolved.url, "https://ai.akmusi.online/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "akmusi"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 akmusi")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://ai.akmusi.online/profile")
        # LinuxDO OAuth 站:不应登记账密(避免误走账密填充)
        self.assertIsNone(get_account_credential("akmusi"))


    def test_yaml_registers_sub2api_entry(self):
        """sites.yaml 的 sub2api 条目解析为 sub2api,/custom/activity,prefer_catalog_url,且账密注册有效。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "sub2api"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 sub2api 条目")
        self._patch_accounts_fixture()
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, get_account_credential, is_sub2api_site, is_sub2api_name
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "sub2api")
        self.assertEqual(a.url, "https://sub2api.0api.me/custom/activity")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.login_url, "https://sub2api.0api.me/login")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("#checkin", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签", joined_already)
        resolved = resolve_site("sub2api", "https://sub2api.0api.me/profile")
        self.assertEqual(resolved.url, "https://sub2api.0api.me/custom/activity")
        self.assertEqual(resolved.kind, "sub2api")
        self.assertEqual(resolved.login_url, "https://sub2api.0api.me/login")
        self.assertTrue(is_sub2api_site("https://sub2api.0api.me/custom/activity"))
        self.assertTrue(is_sub2api_name("sub2api"))
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "sub2api"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 sub2api")
        self.assertEqual(builtin_entry.kind, "sub2api")
        self.assertEqual(builtin_entry.url, "https://sub2api.0api.me/custom/activity")
        self.assertTrue(builtin_entry.prefer_catalog_url)
        self.assertEqual(builtin_entry.login_url, "https://sub2api.0api.me/login")
        cred = get_account_credential("sub2api")
        self.assertIsNotNone(cred)
        self.assertEqual(cred[0], "ci-account-sub2api")
        self.assertTrue(len(cred[1]) > 0)

    def test_yaml_registers_42_entry(self):
        """42 API (api.42x.shop) 2026-11-04 域名迁移:
        原 api.42w.shop 意外断开，新域名 api.42x.shop，启用 prefer_catalog_url 防任务行漂移。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "42"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 42 条目")
        from stealth_checkin_runner import (
            _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS,
        )
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://api.42x.shop/profile")
        self.assertTrue(a.prefer_catalog_url)
        # 即使历史笔记或任务行带旧域名 api.42w.shop，prefer_catalog_url 也会确保实际导航至新域名
        resolved = resolve_site("42", "https://api.42w.shop/profile")
        self.assertEqual(resolved.url, "https://api.42x.shop/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "42"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 42")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        self.assertEqual(builtin_entry.url, "https://api.42x.shop/profile")
        self.assertTrue(builtin_entry.prefer_catalog_url)

    def test_nailao_checkin_api_flow(self):
        """nailao 2026-10-27 改版流程:登录 → 拦截 Bearer → POST /api/user/checkin。
        成功路径 success:true + quota_awarded → OK 且 detail 带奖励;token 不进 detail。"""
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m
        fake_page = AsyncMock()
        # 登录成功后 SPA 离开 /sign-in 落在 dashboard
        fake_page.url = "https://nailao.biz/dashboard/overview"

        def fake_evaluate(js, *args, **kw):
            if "/api/user/checkin" in js:
                return {"status": 200, "success": True, "message": "签到成功", "quota_awarded": 0.897804}
            return None

        fake_page.evaluate = AsyncMock(side_effect=fake_evaluate)
        pw_loc = AsyncMock()
        pw_loc.count = AsyncMock(return_value=1)
        user_loc = AsyncMock()
        btn_loc = AsyncMock()
        btn_loc.count = AsyncMock(return_value=2)
        btn0 = AsyncMock()
        btn0.get_attribute = AsyncMock(return_value="button")
        btn1 = AsyncMock()
        btn1.get_attribute = AsyncMock(return_value="submit")
        btn1.is_disabled = AsyncMock(return_value=False)
        btn_loc.nth = lambda i: [btn0, btn1][i]
        fake_page.locator = lambda sel: {"input[type='password']": pw_loc, "input[name='username']": user_loc, "button:has-text('登录')": btn_loc}[sel]

        def on_request(req):
            # 模拟 SPA 登录后自身请求带 Bearer
            async def _noop():
                return {"authorization": "Bearer ci-token"}
            req.all_headers = _noop
        bearer_req = AsyncMock()
        bearer_req.url = "https://nailao.biz/api/user/models"
        bearer_req.all_headers = AsyncMock(return_value={"authorization": "Bearer ci-token"})
        fake_page.on = lambda ev, cb: cb(bearer_req)

        adapter = m.resolve_site("nailao", "https://nailao.biz/profile")
        with patch.object(m, "get_account_credential", return_value=("ci-account-nailao", "ci-password-nailao")), \
             patch.object(m, "GOTO_TIMEOUT_MS", 5000):
            fake_page.goto = AsyncMock()
            res = asyncio.run(m.nailao_checkin(fake_page, adapter))
        self.assertTrue(res.ok, f"nailao_checkin 应 OK: {res.detail}")
        self.assertIn("签到成功", res.detail)
        self.assertIn("0.89", res.detail)
        self.assertNotIn("ci-token", res.detail, "Bearer token 绝不落结果 detail")
        self.assertNotIn("ci-password-nailao", res.detail)

    def test_nailao_checkin_already(self):
        """幂等响应「今日已签到」→ ALREADY(真 done-state,非失败)。"""
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m
        fake_page = AsyncMock()
        fake_page.url = "https://nailao.biz/sign-in"
        fake_page.evaluate = AsyncMock(return_value={"status": 200, "success": False, "message": "今日已签到", "quota_awarded": None})
        pw_loc = AsyncMock()
        pw_loc.count = AsyncMock(return_value=0)  # 已登录,无密码框
        fake_page.locator = lambda sel: pw_loc
        bearer_req = AsyncMock()
        bearer_req.url = "https://nailao.biz/api/user/models"
        bearer_req.all_headers = AsyncMock(return_value={"authorization": "Bearer ci-token"})
        fake_page.on = lambda ev, cb: cb(bearer_req)

        adapter = m.resolve_site("nailao", "https://nailao.biz/profile")
        with patch.object(m, "get_account_credential", return_value=("ci-account-nailao", "ci-password-nailao")):
            fake_page.goto = AsyncMock()
            res = asyncio.run(m.nailao_checkin(fake_page, adapter))
        self.assertEqual(res.status, "ALREADY")
        self.assertIn("今日已签到", res.detail)

    def test_nailao_checkin_no_token_auth_required(self):
        """未拦截到 Bearer(会话未建立)→ auth_required,不得伪造成功。"""
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m
        fake_page = AsyncMock()
        fake_page.url = "https://nailao.biz/sign-in"
        pw_loc = AsyncMock()
        pw_loc.count = AsyncMock(return_value=0)
        fake_page.locator = lambda sel: pw_loc
        # 页面请求无 authorization 头
        bearer_req = AsyncMock()
        bearer_req.url = "https://nailao.biz/api/user/models"
        bearer_req.all_headers = AsyncMock(return_value={})
        fake_page.on = lambda ev, cb: cb(bearer_req)

        adapter = m.resolve_site("nailao", "https://nailao.biz/profile")
        with patch.object(m, "get_account_credential", return_value=("ci-account-nailao", "ci-password-nailao")):
            fake_page.goto = AsyncMock()
            res = asyncio.run(m.nailao_checkin(fake_page, adapter))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_required")

    def test_yaml_registers_feixingwawa_entry(self):
        """sites.yaml 的 feixingwawa 条目解析为 newapi_profile,/profile(标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "feixingwawa"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 feixingwawa 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://newapi.feixingwawa.cn/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "feixingwawa"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 feixingwawa")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_wududu_entry(self):
        """sites.yaml 的 wududu 条目解析为 newapi_profile,/profile(标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "wududu"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 wududu 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://wududu.edu.kg/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "wududu"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 wududu")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_cngov_entry(self):
        """sites.yaml 的 cngov 条目解析为 newapi_profile,/profile(标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "cngov"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 cngov 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://cngov.cc.cd/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "cngov"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 cngov")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_ssite_entry(self):
        """sites.yaml 的 ssite 条目解析为 newapi_profile,/profile(标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "ssite"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 ssite 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://aapi.ssite.site/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "ssite"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 ssite")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_peiee_entry(self):
        """sites.yaml 的 peiee 条目解析为 newapi_profile,/profile(标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "peiee"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 peiee 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://api.pei.ee/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "peiee"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 peiee")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_yiming_entry(self):
        """sites.yaml 的 yiming 条目解析为 newapi_profile,/profile(一鸣公益站, 标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "yiming"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 yiming 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://newapi.yimingtalk.online/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "yiming"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 yiming")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_maimaidx_entry(self):
        """sites.yaml 的 maimaidx 条目解析为 newapi_profile,/profile(maimai API, 标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "maimaidx"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 maimaidx 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://newapi.maimaidx.icu/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "maimaidx"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 maimaidx")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_chuanapi_entry(self):
        """sites.yaml 的 chuanapi 条目解析为 newapi_profile,/profile(万川智能, 标准 LinuxDO New API 站)."""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "chuanapi"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 chuanapi 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "newapi_profile")
        self.assertEqual(a.url, "https://chuanapi.com/profile")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        # 常驻说明文案绝不能进 already(假 ALREADY 红线)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "chuanapi"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 chuanapi")
        self.assertEqual(builtin_entry.kind, "newapi_profile")
        # sites.yaml 与 _BUILTIN_SITE_ADAPTERS 字段等价
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_mailhub_entry(self):
        """sites.yaml 的 mailhub 条目解析为 browser,/wallet,选择器钉死立即签到。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "mailhub"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 mailhub 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://mailhub.pigeonw.com/wallet")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.login_url, "https://mailhub.pigeonw.com/login")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn('button.primary:has-text("立即签到")', joined_signs)
        # 同页「兑换」不是签到,选择器不能退回裸「签到」
        self.assertNotIn(':has-text("签到")', joined_signs.replace('立即签到', ''))
        joined_already = " ".join(a.already_selectors)
        self.assertIn("今日已签到", joined_already)
        self.assertIn("签到成功", joined_already)
        self.assertNotIn("每日仅可签到一次", joined_already)
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "mailhub"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 mailhub")
        self.assertEqual(builtin_entry.kind, "browser")
        for k in vars(builtin_entry):
            self.assertEqual(getattr(a, k), getattr(builtin_entry, k), f"字段 {k} 不等价")

    def test_yaml_registers_dygyz_entry(self):
        """sites.yaml 的 dygyz 条目解析为 browser,/dashboard,prefer_catalog_url,支持 LinuxDo 登录。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "dygyz"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 dygyz 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, resolve_site, _BUILTIN_SITE_ADAPTERS, is_dygyz_site, is_dygyz_name, LINUXDO_SELECTORS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://dygyz.pmcat.top/dashboard")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.login_url, "https://dygyz.pmcat.top/login")
        joined_signs = " ".join(a.sign_selectors)
        self.assertIn("立即签到领流量", joined_signs)
        joined_already = " ".join(a.already_selectors)
        self.assertIn(".checkin-done", joined_already)
        self.assertIn("今日已签", joined_already)
        resolved = resolve_site("dygyz", "https://dygyz.pmcat.top/dashboard")
        self.assertEqual(resolved.url, "https://dygyz.pmcat.top/dashboard")
        self.assertEqual(resolved.kind, "browser")
        self.assertEqual(resolved.login_url, "https://dygyz.pmcat.top/login")
        self.assertTrue(is_dygyz_site("https://dygyz.pmcat.top/dashboard"))
        self.assertTrue(is_dygyz_name("dygyz"))
        # 2026-11-01 起 "pmcat" 是 plan.pmcat.top(PM-Plan 滑块签到)的名字,
        # 不能再被 dygyz 的名称谓词吞掉;plan 子域也不再算 dygyz 站点。
        self.assertFalse(is_dygyz_name("pmcat"))
        self.assertFalse(is_dygyz_site("https://plan.pmcat.top/#/"))
        self.assertTrue(is_dygyz_site("https://pap.pmcat.top/#/console/wallet"))
        builtin_entry = next((b for b in _BUILTIN_SITE_ADAPTERS if b.name == "dygyz"), None)
        self.assertIsNotNone(builtin_entry, "内置 adapter 应包含 dygyz")
        self.assertEqual(builtin_entry.kind, "browser")
        self.assertEqual(builtin_entry.url, "https://dygyz.pmcat.top/dashboard")
        self.assertTrue(builtin_entry.prefer_catalog_url)
        self.assertEqual(builtin_entry.login_url, "https://dygyz.pmcat.top/login")
        self.assertIn('a:has-text("使用 LinuxDo 登录")', LINUXDO_SELECTORS)


    def test_fengwind_and_mulink_features(self):
        # 1. fengwind site resolution
        adapter = self.m.resolve_site("fengwind", "https://api-welfalre.fengwind.com/")
        self.assertIsNotNone(adapter)
        self.assertTrue(self.m.is_fengwind_site(adapter.url))
        self.assertIn('button[data-slot="button"]:text-is("签到")', adapter.sign_selectors)

        # 2. mulink page text should not be blocked as business_ineligible
        blocked = self.m.classify_page_block("额度池\n余额不满足领取条件\n签到", "https://demo.dev2.mulink.top/wallet")
        self.assertIsNone(blocked)

        # 3. post-click dialog function existence
        self.assertTrue(hasattr(self.m, 'handle_post_click_dialogs_if_needed'))

        # 4. cross checkin helper existence
        self.assertTrue(hasattr(self.m, 'cross_checkin'))
        self.assertTrue(self.m.is_cross_site('https://newapi-checkin.keungliang.dpdns.org/'))


if __name__ == "__main__":
    unittest.main()

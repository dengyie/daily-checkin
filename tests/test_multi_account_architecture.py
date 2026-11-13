"""Unit tests for multi-node multi-account architecture:
1. Dynamic adapter suffix inheritance (resolve_derivative_base_name & resolve_site)
2. Declarative inheritance in sites.yaml (_adapter_from_yaml_entry & load_sites_from_yaml)
3. Convention-based executor node routing (convention_node_for_site & node_for_site)
4. Anti-masking collision detection in web panel (_dedupe_tasks_by_owner)
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checkin_core.dispatch import DispatchStore, convention_node_for_site
from checkin_core.web import _dedupe_tasks_by_owner
from stealth_checkin_runner import (
    SITE_ADAPTERS,
    SiteAdapter,
    _adapter_from_yaml_entry,
    load_sites_from_yaml,
    resolve_derivative_base_name,
    resolve_site,
    _BUILTIN_SITE_ADAPTERS,
)


class TestDerivativeBaseNameResolution(unittest.TestCase):
    """Test extracting base site name from various multi-account / node naming conventions."""

    def test_numeric_account_suffixes(self):
        self.assertEqual(resolve_derivative_base_name("hiyo-2"), "hiyo")
        self.assertEqual(resolve_derivative_base_name("apikey.casa-2"), "apikey.casa")
        self.assertEqual(resolve_derivative_base_name("foo-3"), "foo")
        self.assertEqual(resolve_derivative_base_name("bar_2"), "bar")
        self.assertEqual(resolve_derivative_base_name("baz#2"), "baz")

    def test_node_pinning_suffixes(self):
        self.assertEqual(resolve_derivative_base_name("hiyo-win"), "hiyo")
        self.assertEqual(resolve_derivative_base_name("hiyo-mac"), "hiyo")
        self.assertEqual(resolve_derivative_base_name("91token_win"), "91token")
        self.assertEqual(resolve_derivative_base_name("relayfor-windows"), "relayfor")
        self.assertEqual(resolve_derivative_base_name("relayfor-darwin"), "relayfor")
        self.assertEqual(resolve_derivative_base_name("relayfor-linux"), "relayfor")

    def test_auth_channel_suffixes(self):
        self.assertEqual(resolve_derivative_base_name("nexavlinks-linuxdo"), "nexavlinks")
        self.assertEqual(resolve_derivative_base_name("nexavlinks-email"), "nexavlinks")
        self.assertEqual(resolve_derivative_base_name("account-oauth"), "account")
        self.assertEqual(resolve_derivative_base_name("account-pwd"), "account")

    def test_non_derivative_names_stay_intact(self):
        self.assertIsNone(resolve_derivative_base_name("0api"))
        self.assertIsNone(resolve_derivative_base_name("3sbqs"))
        self.assertIsNone(resolve_derivative_base_name("ai-facai"))
        self.assertIsNone(resolve_derivative_base_name("superqiyu"))
        self.assertIsNone(resolve_derivative_base_name("dygyz"))
        self.assertIsNone(resolve_derivative_base_name("42"))
        self.assertIsNone(resolve_derivative_base_name(""))


class TestDynamicAdapterInheritance(unittest.TestCase):
    """Test that derivative account names automatically inherit base adapter configuration."""

    def test_resolve_site_inherits_custom_browser_selectors(self):
        """hiyo-win or hiyo-3 should inherit hiyo's custom button selectors without extra config."""
        resolved = resolve_site("hiyo-win", "https://free.hiyo.top/dashboard")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "hiyo-win")
        self.assertEqual(resolved.url, "https://free.hiyo.top/dashboard")
        self.assertEqual(resolved.kind, "browser")
        base = next(a for a in SITE_ADAPTERS if a.name == "hiyo")
        self.assertEqual(resolved.prefer_catalog_url, base.prefer_catalog_url)
        self.assertIn('button:has-text("立即签到")', resolved.sign_selectors)
        self.assertIn('button:has-text("今日已签到")', resolved.already_selectors)

    def test_resolve_site_inherits_newapi_profile(self):
        """91token-win should inherit 91token's newapi_profile configuration and url."""
        resolved = resolve_site("91token-win", "")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "91token-win")
        self.assertEqual(resolved.url, "https://91token.com/profile")
        self.assertEqual(resolved.kind, "newapi_profile")
        self.assertTrue(resolved.prefer_catalog_url)

    def test_resolve_site_respects_explicit_catalog_entry_over_inheritance(self):
        """If a derivative site is explicitly declared in catalog, exact match wins."""
        resolved = resolve_site("hiyo-2", "https://free.hiyo.top/dashboard")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "hiyo-2")


class TestDeclarativeYamlInheritance(unittest.TestCase):
    """Test inherit / base field in sites.yaml entries."""

    def test_adapter_from_yaml_entry_with_inherit(self):
        entry = {
            "name": "hiyo-win",
            "inherit": "hiyo",
        }
        adapter = _adapter_from_yaml_entry(entry)
        self.assertEqual(adapter.name, "hiyo-win")
        self.assertEqual(adapter.url, "https://free.hiyo.top/dashboard")
        self.assertEqual(adapter.kind, "browser")
        self.assertIn('button:has-text("立即签到")', adapter.sign_selectors)

    def test_adapter_from_yaml_entry_with_inherit_and_override(self):
        entry = {
            "name": "custom-win",
            "inherit": "hiyo",
            "url": "https://custom.hiyo.top/dashboard",
            "ready_rounds": 25,
        }
        adapter = _adapter_from_yaml_entry(entry)
        self.assertEqual(adapter.name, "custom-win")
        self.assertEqual(adapter.url, "https://custom.hiyo.top/dashboard")
        self.assertEqual(adapter.ready_rounds, 25)
        # Selectors still inherited
        self.assertIn('button:has-text("立即签到")', adapter.sign_selectors)

    def test_load_sites_from_yaml_with_inherit(self):
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "sites.yaml"
            yaml_path.write_text(
                """sites:
  - name: base-site
    url: https://base.example.com/
    kind: browser
    signs:
      - 'button:has-text("打卡")'
    already:
      - 'text=打卡完毕'
  - name: base-site-win
    inherit: base-site
""",
                encoding="utf-8",
            )
            adapters = load_sites_from_yaml(yaml_path)
            self.assertEqual(len(adapters), 2)
            child = next(a for a in adapters if a.name == "base-site-win")
            self.assertEqual(child.url, "https://base.example.com/")
            self.assertEqual(child.sign_selectors, ['button:has-text("打卡")'])
            self.assertEqual(child.already_selectors, ['text=打卡完毕'])


class TestConventionBasedNodeRouting(unittest.TestCase):
    """Test convention-based node routing in checkin_core.dispatch."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "dispatch.db"
        self.store = DispatchStore(self.db_path)
        self.store.add_node("mac", "http://10.144.144.100:9777")
        self.store.add_node("win", "http://10.144.144.101:9778")

    def tearDown(self):
        self.td.cleanup()

    def test_convention_node_for_site_helper(self):
        self.assertEqual(convention_node_for_site("site-win"), "win")
        self.assertEqual(convention_node_for_site("site_win"), "win")
        self.assertEqual(convention_node_for_site("site-windows"), "win")
        self.assertEqual(convention_node_for_site("site-mac"), "mac")
        self.assertEqual(convention_node_for_site("site_mac"), "mac")
        self.assertEqual(convention_node_for_site("site-darwin"), "mac")
        self.assertEqual(convention_node_for_site("site-linux"), "linux")
        self.assertIsNone(convention_node_for_site("site-2"))
        self.assertIsNone(convention_node_for_site("site"))

    def test_node_for_site_pinned_overrides_convention(self):
        # Even if name ends with -win, explicit pin in site_nodes takes precedence
        self.store.set_site_node("custom-win", "mac")
        self.assertEqual(self.store.node_for_site("custom-win"), "mac")

    def test_node_for_site_falls_back_to_convention(self):
        # No explicit pin -> automatically routes by suffix
        self.assertEqual(self.store.node_for_site("91token-win"), "win")
        self.assertEqual(self.store.node_for_site("relayfor-mac"), "mac")
        self.assertIsNone(self.store.node_for_site("unpinned-site"))

    def test_resolve_nodes_for_sites_groups_correctly(self):
        sites = ["hiyo", "91token-win", "relayfor-mac", "custom-windows"]
        groups = self.store.resolve_nodes_for_sites(sites)
        self.assertIn("win", groups)
        self.assertIn("mac", groups)
        self.assertIn("91token-win", groups["win"])
        self.assertIn("custom-windows", groups["win"])
        self.assertIn("relayfor-mac", groups["mac"])
        self.assertIn("hiyo", groups["mac"])  # unpinned falls back to default node (mac)


class TestWebPanelAntiMaskingAndDeduplication(unittest.TestCase):
    """Test that multi-node tasks are deduplicated safely without masking or silent drops."""

    def test_independent_derivative_accounts_both_appear(self):
        """Mac runs 'hiyo' and Win runs 'hiyo-2'; both should appear on dashboard independently."""
        rows = [
            {"name": "hiyo", "node": "mac", "status": "success", "balance": "$35.43"},
            {"name": "hiyo-2", "node": "win", "status": "success", "balance": "$31.13"},
        ]
        pinned = {"hiyo-2": "win"}
        out = _dedupe_tasks_by_owner(rows, pinned)
        self.assertEqual(len(out), 2)
        names = {t["name"]: t["balance"] for t in out}
        self.assertEqual(names["hiyo"], "$35.43")
        self.assertEqual(names["hiyo-2"], "$31.13")
        # Neither has collision
        for t in out:
            self.assertFalse(t.get("cross_node_conflict", False))

    def test_unsuffixed_site_on_multiple_nodes_tags_cross_node_conflict(self):
        """If both Mac and Win report the same un-suffixed site 'hiyo', collision is flagged."""
        rows = [
            {"name": "hiyo", "node": "mac", "status": "pending"},
            {"name": "hiyo", "node": "win", "status": "success"},
        ]
        pinned = {"hiyo": "win"}
        out = _dedupe_tasks_by_owner(rows, pinned)
        self.assertEqual(len(out), 1)
        task = out[0]
        self.assertEqual(task["node"], "win")
        self.assertTrue(task.get("cross_node_conflict"))
        self.assertEqual(task.get("competing_nodes"), ["mac", "win"])

    def test_convention_node_wins_over_last_seen_for_unpinned_site(self):
        """If 'site-win' is unpinned, the win row is preferred over mac row by convention."""
        rows = [
            {"name": "site-win", "node": "win", "status": "done"},
            {"name": "site-win", "node": "mac", "status": "pending"},
        ]
        # unpinned dict
        out = _dedupe_tasks_by_owner(rows, {})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["node"], "win")


class TestYamlInheritIntegrity(unittest.TestCase):
    """sites.yaml inherit resolution must be order-independent and loud."""

    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "sites.yaml"
            yaml_path.write_text(text, encoding="utf-8")
            return load_sites_from_yaml(yaml_path)

    def test_child_declared_before_parent_still_resolves(self):
        """Declaration order must not decide whether a site exists.

        Regression: single-pass resolution looked up only already-parsed
        siblings, so a child placed above its parent raised KeyError('url'),
        which the broad except swallowed — the site silently vanished from the
        roster and simply stopped checking in.
        """
        adapters = self._load(
            """sites:
  - name: demo-win
    inherit: demo
  - name: demo
    url: https://demo.example.com/
    kind: browser
    signs:
      - 'button:has-text("打卡")'
    already:
      - 'text=打卡完毕'
"""
        )
        names = [a.name for a in adapters]
        self.assertIn("demo-win", names, "父站后置时子站不能消失")
        child = next(a for a in adapters if a.name == "demo-win")
        self.assertEqual(child.url, "https://demo.example.com/")
        self.assertEqual(child.sign_selectors, ['button:has-text("打卡")'])
        self.assertEqual(child.already_selectors, ['text=打卡完毕'])

    def test_missing_inherit_target_is_a_hard_error(self):
        """A typo in `inherit` must not degrade into a silently dropped site."""
        with self.assertRaises(ValueError) as ctx:
            self._load(
                """sites:
  - name: typo-win
    inherit: does-not-exist
"""
            )
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_circular_inherit_is_a_hard_error(self):
        with self.assertRaises(ValueError) as ctx:
            self._load(
                """sites:
  - name: a
    inherit: b
  - name: b
    inherit: a
"""
            )
        self.assertIn("circular", str(ctx.exception).lower())

    def test_self_inherit_is_a_hard_error(self):
        with self.assertRaises(ValueError):
            self._load(
                """sites:
  - name: a
    inherit: a
"""
            )

    def test_duplicate_site_name_is_a_hard_error(self):
        with self.assertRaises(ValueError) as ctx:
            self._load(
                """sites:
  - name: dup
    url: https://dup.example.com/
  - name: dup
    url: https://dup2.example.com/
"""
            )
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_entry_without_url_and_without_inherit_still_skipped(self):
        """Structurally incomplete entries keep the old skip-and-warn behaviour."""
        adapters = self._load(
            """sites:
  - name: ok
    url: https://ok.example.com/
  - name: no-url-no-inherit
"""
        )
        self.assertEqual([a.name for a in adapters], ["ok"])

    def test_multi_level_inherit_chain(self):
        adapters = self._load(
            """sites:
  - name: lvl3
    inherit: lvl2
  - name: lvl2
    inherit: lvl1
  - name: lvl1
    url: https://lvl.example.com/
    signs:
      - 'button:has-text("签到")'
"""
        )
        lvl3 = next(a for a in adapters if a.name == "lvl3")
        self.assertEqual(lvl3.url, "https://lvl.example.com/")
        self.assertEqual(lvl3.sign_selectors, ['button:has-text("签到")'])


class TestDerivativeInheritanceGuards(unittest.TestCase):
    """Dynamic suffix inheritance must not capture an independent site."""

    def test_derivative_with_foreign_host_does_not_inherit(self):
        """`foo-3` on another domain is its own site, not foo's second account.

        Regression: inheritance keyed on the name suffix alone, so an
        independent `foo-3` silently took foo's kind and selectors and would
        drive the wrong page. (hiyo-2 is a declared catalog entry, so it exact-
        matches and never reaches this branch; hiyo-3 exercises the undeclared
        derivative path the guard exists for.)
        """
        base = next((a for a in SITE_ADAPTERS if a.name == "hiyo"), None)
        self.assertIsNotNone(base, "precondition: hiyo must be in the catalog")
        resolved = resolve_site("hiyo-3", "https://totally-different.example.org/x")
        self.assertIsNotNone(resolved)
        # 没有继承 hiyo 的专属选择器，而是走了通用 URL 启发式
        # (kind 无从区分: 通用兜底同样是 browser, 差异只在选择器上)
        self.assertNotEqual(resolved.sign_selectors, base.sign_selectors)

    def test_derivative_with_matching_host_inherits(self):
        base = next((a for a in SITE_ADAPTERS if a.name == "hiyo"), None)
        resolved = resolve_site("hiyo-3", "https://free.hiyo.top/dashboard")
        self.assertEqual(resolved.sign_selectors, base.sign_selectors)
        self.assertEqual(resolved.already_selectors, base.already_selectors)

    def test_derivative_without_note_url_still_inherits(self):
        base = next((a for a in SITE_ADAPTERS if a.name == "hiyo"), None)
        resolved = resolve_site("hiyo-win", "")
        self.assertEqual(resolved.sign_selectors, base.sign_selectors)

    def test_independent_site_whose_base_does_not_exist_is_not_invented(self):
        self.assertIsNone(resolve_site("no-such-base-2", ""))


class TestNodeRoutingAgainstRegistry(unittest.TestCase):
    """Convention routing must never target a node that is not registered."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = DispatchStore(Path(self.td.name) / "dispatch.db")
        self.store.add_node("mac", "http://10.144.144.100:9777")
        self.store.add_node("win", "http://10.144.144.101:9778")

    def tearDown(self):
        self.td.cleanup()

    def test_convention_to_unregistered_node_is_refused(self):
        """`*-linux` naming implies a linux node; none is registered here.

        Regression: the inferred name was returned unconditionally, so the job
        was queued to a node that could never claim it, burned every delivery
        attempt and died as "delivery failed" — sending operators after a
        network fault that did not exist.
        """
        self.assertIsNone(self.store.node_for_site("site-linux"))

    def test_convention_to_registered_node_still_applies(self):
        self.assertEqual(self.store.node_for_site("site-win"), "win")
        self.assertEqual(self.store.node_for_site("site-mac"), "mac")

    def test_removed_node_stops_being_targeted(self):
        self.assertEqual(self.store.node_for_site("site-win"), "win")
        self.store.remove_node("win")
        self.assertIsNone(self.store.node_for_site("site-win"))

    def test_unrouted_site_falls_back_to_default_node(self):
        groups = self.store.resolve_nodes_for_sites(["site-linux"])
        self.assertNotIn("linux", groups)
        self.assertIn(self.store.default_node(), groups)

    def test_explicit_pin_beats_convention_even_if_unregistered(self):
        """An explicit operator pin is an instruction, not a guess — keep it."""
        self.store.set_site_node("site-win", "mac")
        self.assertEqual(self.store.node_for_site("site-win"), "mac")


class TestDedupeDoesNotMutateInput(unittest.TestCase):
    def test_conflict_tagging_copies_rows(self):
        rows = [
            {"name": "hiyo", "node": "mac", "status": "pending"},
            {"name": "hiyo", "node": "win", "status": "success"},
        ]
        snapshot = [dict(r) for r in rows]
        out = _dedupe_tasks_by_owner(rows, {"hiyo": "win"})
        self.assertTrue(out[0].get("cross_node_conflict"))
        self.assertEqual(rows, snapshot, "入参行对象不得被写入冲突标记")


class TestCatalogDriftIsBounded(unittest.TestCase):
    """yaml catalog and the built-in fallback must not drift any further.

    The built-in list is a hand-maintained shadow copy of sites.yaml, used only
    when the yaml cannot be read. Divergence there means a fallback run uses
    different selectors than the normal run. The seven known divergences
    predate this change and are tracked here so any NEW drift fails the suite.
    """

    KNOWN_DRIFT = {
        "0api": {"sign_selectors", "already_selectors"},
        "简直了": {"site_timeout_s"},
        "pmcat": {"prefer_catalog_url", "login_url"},
        "hiyo": {"already_selectors", "prefer_catalog_url"},
        "sub2api": {"sign_selectors", "already_selectors"},
        "glados": {"sign_selectors", "already_selectors"},
        "xkrelay": {"login_url"},
    }

    FIELDS = [
        "kind",
        "url",
        "sign_selectors",
        "already_selectors",
        "prefer_catalog_url",
        "ready_rounds",
        "site_timeout_s",
        "login_url",
    ]

    def test_no_new_drift_between_yaml_and_builtin(self):
        yaml_by_name = {a.name: a for a in SITE_ADAPTERS}
        builtin_by_name = {a.name: a for a in _BUILTIN_SITE_ADAPTERS}
        found = {}
        for name, adapter in yaml_by_name.items():
            twin = builtin_by_name.get(name)
            if not twin:
                continue
            bad = {f for f in self.FIELDS if getattr(adapter, f) != getattr(twin, f)}
            if bad:
                found[name] = bad
        self.assertEqual(
            found,
            self.KNOWN_DRIFT,
            "yaml 与 builtin 目录出现新的字段漂移；回落路径行为会与正常路径不一致",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "daily-checkin-cron.py"


def load_entry():
    old = os.environ.get("DAILY_CHECKIN_ROOT")
    os.environ["DAILY_CHECKIN_ROOT"] = str(ROOT)
    try:
        spec = importlib.util.spec_from_file_location("daily_checkin_cron_test", ENTRY)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("DAILY_CHECKIN_ROOT", None)
        else:
            os.environ["DAILY_CHECKIN_ROOT"] = old


class CronEntryTests(unittest.TestCase):
    def test_selected_endpoint_probes_explicit_url_once(self):
        entry = load_entry()
        expected = {
            "http": "http://10.0.0.8:9333", "port": 9333,
            "headless": False,
        }
        with patch.object(entry, "discover_cdp_endpoint", return_value=expected) as discover:
            selected = entry.selected_endpoint(["--cdp", "http://10.0.0.8:9333"])
        self.assertEqual(selected, expected)
        discover.assert_called_once_with(
            prefer_http="http://10.0.0.8:9333",
            strict_prefer=True,
            allow_headless=False,
        )

    def test_fixed_runner_args_reuses_preflight_endpoint(self):
        entry = load_entry()
        args = entry.fixed_runner_args(
            ["--only", "A", "--cdp", "9333", "--allow-headless"],
            {"http": "http://127.0.0.1:9333"},
        )
        self.assertEqual(args.count("--cdp"), 1)
        index = args.index("--cdp")
        self.assertEqual(args[index + 1], "http://127.0.0.1:9333")
        self.assertIn("--allow-headless", args)
        self.assertEqual(args[:2], ["--source", "cron"])

    def test_dry_run_skips_endpoint_discovery(self):
        entry = load_entry()
        with patch.object(entry, "discover_cdp_endpoint") as discover:
            self.assertIsNone(entry.selected_endpoint(["--dry-run"]))
        discover.assert_not_called()

    def test_preflight_scope_matches_runner_filters(self):
        entry = load_entry()
        self.assertEqual(entry.runner_scope([]), ("full", []))
        self.assertEqual(
            entry.runner_scope(["--only", "B,A"]),
            ("targeted", ["A", "B"]),
        )
        self.assertEqual(entry.runner_scope(["--retry-auto-fail"])[0], "targeted")
        self.assertEqual(
            entry.runner_scope(["--task-file", "/tmp/probe.md"])[0],
            "targeted",
        )


if __name__ == "__main__":
    unittest.main()

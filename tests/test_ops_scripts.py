"""Ops-script source-contract tests (morning-batch / sync / dispatch launchers).

These scripts carry the morning-batch 08:10 high-availability chain:
launch-daily-batch.sh -> daily-checkin-cron.py -> sync-db-to-pxed.sh, and the
Windows twin run-checkin-win.ps1 -> sync-db-to-panel.ps1. A regression that
silently drops the post-batch sync or breaks the atomic-rename protocol shows
up here as a failed contract instead of a stale panel replica.

Style: text-contract assertions on the script source (same pattern as
TestDispatchCli.test_launch_agent_script_..., no execution, no network).
"""
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    path = SCRIPTS / name
    assert path.exists(), f"ops script missing: {path}"
    return path.read_text(encoding="utf-8")


class SyncDbToPxedContractTests(unittest.TestCase):
    """Mac -> panel atomic sync (scp to .incoming + remote mv)."""

    def test_atomic_replace_protocol(self):
        text = _read("sync-db-to-pxed.sh")
        self.assertIn("wal_checkpoint(TRUNCATE)", text)
        self.assertIn(".system.db.incoming", text)
        # rename happens on the remote host after scp succeeds
        self.assertIn("mv", text)

    def test_missing_db_env_skips(self):
        text = _read("sync-db-to-pxed.sh")
        self.assertIn("DAILY_CHECKIN_DB", text)
        self.assertIn("skip", text)

    def test_no_zsh_only_parameter_expansions(self):
        """The shebang only governs direct execution; the script must also
        survive `bash script.sh` / `sh script.sh` invocations (an operator
        sourcing env.sh in bash hit "h: unbound variable" on 2026-11-13 via
        `${REMOTE_PATH:h}`). Ban zsh modifiers and keep the POSIX dirname."""
        text = _read("sync-db-to-pxed.sh")
        for bad in (":h}", ":t}", ":r}", ":e}"):
            self.assertNotIn(bad, text)
        self.assertIn('dirname "$REMOTE_PATH"', text)


class SyncDbToPanelContractTests(unittest.TestCase):
    """Windows -> panel atomic sync (twin of sync-db-to-pxed.sh)."""

    def test_atomic_replace_protocol(self):
        text = _read("sync-db-to-panel.ps1")
        self.assertIn("wal_checkpoint(TRUNCATE)", text)
        self.assertIn(".system.db.incoming", text)
        self.assertIn("mv ${RemoteTmp} ${RemotePath}", text)

    def test_env_defaults_and_skip_guard(self):
        text = _read("sync-db-to-panel.ps1")
        self.assertIn("pxed-direct", text)
        self.assertIn("/data/daily-checkin/data/system-win.db", text)
        self.assertIn("DAILY_CHECKIN_DB not set or missing, skip", text)


class LaunchDailyBatchContractTests(unittest.TestCase):
    """launchd 08:10 entry: staging env -> cron batch -> post-batch sync."""

    def test_sources_staging_env_and_venv_python(self):
        text = _read("launch-daily-batch.sh")
        self.assertIn("daily-checkin-staging/env.sh", text)
        self.assertIn("daily-checkin-cron.py", text)
        self.assertIn("--source cron", text)
        self.assertIn(".venv/bin/python", text)

    def test_runs_post_batch_panel_sync(self):
        text = _read("launch-daily-batch.sh")
        self.assertIn("sync-db-to-pxed.sh", text)
        self.assertIn("exit $EXIT_CODE", text)


class RunCheckinWinContractTests(unittest.TestCase):
    """Windows morning batch: runner invocation + post-batch replica sync."""

    def test_invokes_runner_with_cdp_9222(self):
        text = _read("run-checkin-win.ps1")
        self.assertIn("stealth_checkin_runner.py", text)
        self.assertIn('"--cdp", "9222"', text)

    def test_post_batch_sync_targets_win_replica(self):
        text = _read("run-checkin-win.ps1")
        self.assertIn("DAILY_CHECKIN_SYNC_REMOTE_PATH", text)
        self.assertIn("/data/daily-checkin/data/system-win.db", text)
        self.assertIn("sync-db-to-panel.ps1", text)


class WinLauncherContractTests(unittest.TestCase):
    """Windows entry scripts staying non-empty and wired to the ps1 layer."""

    def test_batch_scripts_wired(self):
        run_bat = _read("run-checkin-win.bat")
        self.assertIn("stealth_checkin_runner.py", run_bat)
        self.assertIn("--cdp 9222", run_bat)
        self.assertIn("DAILY_CHECKIN_DB=", run_bat)
        start_bat = _read("start-all-win.bat")
        self.assertIn("start-all-win.ps1", start_bat)

    def test_dispatch_agent_launcher_nonempty_and_env_wired(self):
        text = _read("launch-dispatch-agent-win.ps1")
        self.assertIn("DAILY_CHECKIN_DISPATCH_TOKEN", text)
        self.assertIn("GetEnvironmentVariable", text)  # never hardcodes the token
        # keeps the executor-node DB independent from the Mac node
        self.assertIn("E:\\project\\daily-checkin\\data\\system.db", text)

    def test_hidden_vbs_launcher_targets_agent_ps1(self):
        text = _read("start-dispatch-agent-win-hidden.vbs")
        self.assertIn("launch-dispatch-agent-win.ps1", text)
        self.assertIn("WScript.Quit", text)


if __name__ == "__main__":
    unittest.main()

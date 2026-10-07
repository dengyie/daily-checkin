from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from checkin_core.ai_inspector_models import InspectionReport

logger = logging.getLogger("checkin.remediation")


class RemediationEngine:
    """Decision execution & auto-remediation engine for AI-inspected check-in batches."""

    DEFAULT_MAX_RETRIES: int = 3
    DEFAULT_DELAY_SECONDS: float = 60.0

    @classmethod
    def filter_safe_retry_targets(
        cls,
        targets: Sequence[str],
        today_str: str,
        store: Any,
        max_targets: int = DEFAULT_MAX_RETRIES,
    ) -> list[str]:
        """Enforce strict anti-storm retry budget (max 1 auto-retry per site per day)."""
        safe_targets: list[str] = []
        for raw_site in targets:
            site = str(raw_site).strip()
            if not site:
                continue
            if len(safe_targets) >= max_targets:
                break
            # Check historical retry count today
            current_retries = 0
            if hasattr(store, "get_today_retry_count"):
                try:
                    current_retries = store.get_today_retry_count(site, today_str)
                except Exception:
                    current_retries = 0

            if current_retries < 1:
                safe_targets.append(site)
            else:
                logger.info(
                    "RemediationEngine: skipping %s (already retried %d times today)",
                    site,
                    current_retries,
                )

        return safe_targets

    @classmethod
    def apply_auto_suppression(
        cls,
        report: InspectionReport,
        store: Any,
    ) -> list[str]:
        """Auto-suppress sites confirmed as permanently disabled by site owner (confidence >= 0.90)."""
        suppressed: list[str] = []
        if not hasattr(store, "suppress_site"):
            return suppressed

        for insp in report.inspections:
            if insp.category == "upstream_disabled" and insp.confidence >= 0.90:
                reason = f"ai:disabled:{insp.root_cause[:40]}" if insp.root_cause else "ai:disabled"
                try:
                    store.suppress_site(insp.site, reason=reason)
                    suppressed.append(insp.site)
                    logger.info("RemediationEngine: auto-suppressed %s (%s)", insp.site, reason)
                except Exception as exc:
                    logger.warning("Failed to suppress site %s: %s", insp.site, exc)

        return suppressed

    @classmethod
    def schedule_retries(
        cls,
        retry_sites: Sequence[str],
        today_str: str,
        store: Any,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        runner_path: Path | None = None,
    ) -> list[str]:
        """Schedule isolated background retry subprocess after backoff delay."""
        filtered_targets = cls.filter_safe_retry_targets(retry_sites, today_str, store)
        if not filtered_targets:
            return []

        # Record retry count immediately to avoid race condition
        for s in filtered_targets:
            if hasattr(store, "increment_today_retry_count"):
                try:
                    store.increment_today_retry_count(s, today_str)
                except Exception:
                    pass

        if runner_path is None:
            runner_path = Path(__file__).resolve().parent.parent / "stealth_checkin_runner.py"

        # Spawn detached OS process to outlive exiting batch process and run after lock release
        code_str = (
            f"import time, subprocess, sys; "
            f"time.sleep({float(delay_seconds)}); "
            f"sys.exit(subprocess.run(["
            f"sys.executable, {repr(str(runner_path))}, '--cdp', '9222', '--allow-headless', '--only', {repr(','.join(filtered_targets))}"
            f"]).returncode)"
        )
        cmd = [sys.executable, "-c", code_str]
        try:
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "RemediationEngine: detached retry subprocess scheduled for %s (delay=%.1fs)",
                filtered_targets,
                delay_seconds,
            )
        except Exception as exc:
            logger.warning("RemediationEngine: failed to spawn detached retry subprocess: %s", exc)

        return filtered_targets

    @classmethod
    async def _execute_isolated_retry(
        cls,
        sites: list[str],
        today_str: str,
        runner_path: Path,
        delay_seconds: float,
    ) -> None:
        """Isolated subprocess execution with backoff sleep."""
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        cmd = [
            sys.executable,
            str(runner_path),
            "--cdp", "9222",
            "--allow-headless",
            "--only", ",".join(sites),
        ]
        logger.info("RemediationEngine: executing subprocess retry for: %s", sites)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            logger.info(
                "RemediationEngine: subprocess finished (code=%d, sites=%s)",
                proc.returncode,
                sites,
            )
        except Exception as exc:
            logger.warning("RemediationEngine: subprocess launch failed: %s", exc)

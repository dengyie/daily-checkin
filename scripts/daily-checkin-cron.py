#!/usr/bin/env python3
"""Hermes cron entry that keeps preflight and the runner in one process."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import TextIO

ROOT = Path(os.environ["DAILY_CHECKIN_ROOT"]).resolve()
sys.path.insert(0, str(ROOT))

from stealth_checkin_runner import (  # noqa: E402
    acquire_run_lock,
    classify_run_scope,
    discover_cdp_endpoint,
    parse_cli_args,
    run,
)


class Tee:
    def __init__(self, *streams: TextIO):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def selected_endpoint(argv: list[str]) -> dict | None:
    if "--dry-run" in argv:
        return None
    raw = ""
    if "--cdp" in argv:
        index = argv.index("--cdp")
        if index + 1 >= len(argv):
            raise RuntimeError("--cdp requires a URL or port")
        raw = argv[index + 1].strip()
    allow_headless = "--allow-headless" in argv
    if raw:
        if raw.isdigit():
            return discover_cdp_endpoint(
                prefer_port=int(raw), strict_prefer=True,
                allow_headless=allow_headless,
            )
        return discover_cdp_endpoint(
            prefer_http=raw, strict_prefer=True,
            allow_headless=allow_headless,
        )
    return discover_cdp_endpoint(allow_headless=allow_headless)


def fixed_runner_args(argv: list[str], endpoint: dict | None) -> list[str]:
    out = ["--source", "cron"]
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == "--cdp":
            skip = True
            continue
        out.append(arg)
    if endpoint is not None:
        out.extend(["--cdp", str(endpoint["http"])])
    return out


def runner_scope(argv: list[str]) -> tuple[str, list[str]]:
    parsed = parse_cli_args(fixed_runner_args(argv, None))
    only = {x.strip() for x in parsed.only.split(",") if x.strip()}
    return classify_run_scope(parsed, only)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    log_dir = Path(
        os.environ.get("DAILY_CHECKIN_LOG_DIR", str(Path.home() / ".hermes/checkin"))
    ).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"cron-{datetime.now():%Y-%m-%d}.log"

    with log_path.open("a", encoding="utf-8") as log, redirect_stdout(
        Tee(sys.stdout, log)
    ), redirect_stderr(Tee(sys.stderr, log)):
        print(f"==== {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z} daily-checkin L2 start ====")
        print(f"runner={ROOT / 'stealth_checkin_runner.py'}")
        print("policy=reuse_existing_cdp_fixed_after_preflight (no launch; headless opt-in only)")
        try:
            endpoint = selected_endpoint(argv)
        except Exception as exc:
            print(f"CDP preflight failed: {exc}")
            from stealth_checkin_runner import write_last_run_status

            scope, target_sites = runner_scope(argv)

            write_last_run_status(
                exit_code=3, cdp=None, results=None,
                reason=f"cdp_down:{exc}"[:120], source="cron",
                scope=scope, target_sites=target_sites,
                update_business_run=False,
            )
            print(json.dumps({"wakeAgent": True, "exit": 3, "reason": "cdp_down"}))
            return 3

        if endpoint is None:
            print("CDP preflight skipped (--dry-run)")
        else:
            print("preflight=" + json.dumps(endpoint, ensure_ascii=False, default=str))

        lock_handle = acquire_run_lock()
        if lock_handle is None:
            print("Another daily-checkin batch is already running")
            print(json.dumps({"wakeAgent": True, "exit": 4, "reason": "locked"}))
            return 4
        try:
            ec = asyncio.run(run(fixed_runner_args(argv, endpoint)))
        finally:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()

        print(f"==== {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z} daily-checkin end ec={ec} ====")
        print(f"LOG={log_path}")
        print(json.dumps({"wakeAgent": ec != 0, "exit": ec}))
        return ec


if __name__ == "__main__":
    raise SystemExit(main())

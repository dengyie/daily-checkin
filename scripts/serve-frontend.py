#!/usr/bin/env python3
"""CLI entry for the loopback-only static frontend (see checkin_core.frontend).

    .venv/bin/python scripts/serve-frontend.py --host 127.0.0.1 --port 8766
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkin_core.frontend import main  # noqa: E402

if __name__ == "__main__":
    main()
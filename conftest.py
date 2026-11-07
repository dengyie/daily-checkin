"""Root conftest: make the repo root importable regardless of invocation cwd.

Keeps the README's documented command working from any directory:
    PYTHONPATH=. python -m unittest discover -s tests -p 'test_*.py'
and lets pytest resolve `tests/_support.py` and `checkin_core` imports when
running from an IDE with a different working directory.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

"""Shared test harness — single authoritative implementation.

Everything here used to be copy-pasted across legacy test_stealth_checkin_m1.py
(now split) and individual tests/ files. New tests must import from here
instead of re-implementing:

    from tests._support import (
        clean_env, ensure_runner_loaded, load_mod,
        CleanEnvMixin, FakeLoc, FakePage, make_fake_selector_page,
    )

Conventions kept from the legacy suite (do not change semantics casually):
- `load_mod(force=True)` re-executes stealth_checkin_runner.py so tests that
  assert module-level mutable state (timeout knobs, adapter registries,
  constants) stay isolated from earlier tests in the same worker process.
- `ensure_runner_loaded()` replaces the old `__import__("stealth_checkin_runner")`
  pattern, which silently depended on some earlier test having populated
  sys.modules (ordering coupling — incompatible with xdist).
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock  # noqa: F401  (re-exported for one-line test imports)

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "stealth_checkin_runner.py"

_RUNNER_MODULE_NAME = "stealth_checkin_runner"


def load_mod(force: bool = False):
    """Import stealth_checkin_runner.py by file path, cached in sys.modules."""
    if force and _RUNNER_MODULE_NAME in sys.modules:
        del sys.modules[_RUNNER_MODULE_NAME]
    if _RUNNER_MODULE_NAME in sys.modules and not force:
        return sys.modules[_RUNNER_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_RUNNER_MODULE_NAME, TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_RUNNER_MODULE_NAME] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def ensure_runner_loaded():
    """Guarantee sys.modules["stealth_checkin_runner"] exists, then return it.

    Replacement for bare `__import__("stealth_checkin_runner")` sites that
    relied on test-ordering side effects.
    """
    if _RUNNER_MODULE_NAME not in sys.modules:
        load_mod(force=True)
    return sys.modules[_RUNNER_MODULE_NAME]


# env.sh tuning knobs that short-circuit runner defaults when exported
# (explicit CDP endpoints, timeout overrides, retry tuning).
ENV_KNOBS = (
    "CDP_HTTP", "CDP_PORT",
    "CAPTCHA_WAIT_S", "CF_WAIT_S", "SSO_TIMEOUT_S", "SITE_TIMEOUT_S",
    "CONNECT_RETRIES", "CONNECT_TIMEOUT_S", "CONNECT_RETRY_BACKOFF_S",
    "BATCH_TIMEOUT_S", "BATCH_CLEANUP_RESERVE_S",
)


@contextlib.contextmanager
def clean_env():
    """临时摘掉 env.sh 的调参 knob,退出时原样还原。"""
    saved = {k: os.environ.pop(k, None) for k in ENV_KNOBS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class CleanEnvMixin:
    """用例内屏蔽 env.sh 的调参 knob(见 ENV_KNOBS)。

    典型:CDP_HTTP / CDP_PORT 是「显式端点」,会在自动发现里短路
    (env_CDP_HTTP / env_CDP_PORT);CAPTCHA_WAIT_S 等默认值断言也同理。
    新增断言默认值的用例类直接继承本 mixin,不要各自手写 pop/restore。
    """

    def setUp(self):
        super().setUp()
        self._env_ctx = clean_env()
        self._env_ctx.__enter__()

    def tearDown(self):
        self._env_ctx.__exit__(None, None, None)
        super().tearDown()


class FakePage:
    """Minimal page double for try_signin_api (only needs evaluate)."""

    def __init__(self, fetch_result=None, eval_error=None):
        self.fetch_result = fetch_result
        self.eval_error = eval_error

    async def evaluate(self, js):
        if self.eval_error is not None:
            raise self.eval_error
        return dict(self.fetch_result or {})


class FakeLoc:
    """Locator double configurable per selector (already_done / CTA guard tests)."""

    def __init__(self, *, count=0, visible=False, disabled=False, label=""):
        self._c, self._v, self._d, self._l = count, visible, disabled, label

    @property
    def first(self):
        return self

    def nth(self, i):
        return self

    async def count(self):
        return self._c

    async def is_visible(self, timeout=0):
        return self._v

    async def is_disabled(self):
        return self._d

    async def inner_text(self, timeout=0):
        return self._l


def make_fake_selector_page(body, locators):
    """Page double: evaluate returns `body`; locator(sel) served from `locators`
    (selector -> FakeLoc kwargs), everything else an empty invisible locator."""

    class _FakeSelPage:
        def __init__(self):
            self.url = "https://api.example.com/profile"

        def locator(self, sel):
            return FakeLoc(**(locators.get(sel) or {}))

        async def evaluate(self, js):
            return body

    return _FakeSelPage()


# Legacy in-file names (pre-split one large file). Kept as aliases so the
# mechanically migrated class bodies stay verbatim; new code should use the
# canonical names above.
_CleanEnvMixin = CleanEnvMixin
_FakePage = FakePage
_FakeLoc = FakeLoc
_make_fake_selector_page = make_fake_selector_page
_ENV_KNOBS = ENV_KNOBS

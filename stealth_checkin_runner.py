#!/usr/bin/env python3
"""
Bulletproof daily check-in on an EXISTING Chrome with remote debugging.

Rules (user-facing):
- connect_over_cdp only — never launch / kill Chrome
- NEVER open a new browser window/process
- Discover live CDP endpoints (do not hardcode only :9222); prefer newest headed Chrome
- silent temp tab: HTTP CDP /json/new → work → /json/close ONLY after full flow returns
- bind page by CDP target id — never fall back to last/arbitrary page
- unauthenticated → LinuxDO SSO (never bare short-circuit)
- ALWAYS real-click agreement BEFORE LinuxDO
- long OAuth / CF wait — never close mid-login
- success only with business confirm text — never mark on guess
- poll body text; force-click; no networkidle
"""

from __future__ import annotations

import asyncio
import calendar
import fcntl
import os
import json
import re
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from checkin_core.models import (
    ActionEvidence,
    ConfirmationEvidence,
    IdentityEvidence,
    evidence_dict,
    evidence_is_confirming,
)
from checkin_core.providers import LegacyBrowserProvider, ProviderContext, WisartProvider
from checkin_core.notify import notify_daily_summary
from checkin_core.registry import ProviderRegistry
from checkin_core.store import store_from_env

# Mutable: set at run start via discover_cdp_endpoint(). Env CDP_HTTP / CDP_PORT still work.
CDP_HTTP = os.environ.get("CDP_HTTP", "").strip() or "http://127.0.0.1:9222"
SSO_POLL_S = 1.0
# L2 defaults: shorter waits (override via env). CAPTCHA still 1s.
SSO_TIMEOUT_S = float(os.environ.get("SSO_TIMEOUT_S", "45"))
CF_WAIT_S = float(os.environ.get("CF_WAIT_S", "80"))
CAPTCHA_WAIT_S = float(os.environ.get("CAPTCHA_WAIT_S", "40"))  # Turnstile may self-clear; wait before failing
CTA_WAIT_S = float(os.environ.get("CTA_WAIT_S", "8"))  # LinuxDO / login UI before NO_BUTTON
SIGN_WAIT_S = float(os.environ.get("SIGN_WAIT_S", "8"))
MIN_DWELL_AFTER_SSO_CLICK_S = float(os.environ.get("MIN_DWELL_AFTER_SSO_CLICK_S", "4"))
CONNECT_TIMEOUT_S = 12.0
CLOSE_TIMEOUT_S = 5.0
# Bounded CDP call -- a half-dead websocket (Chrome stuck on a stalled tab)
# must not hang page_cdp_target_id / SSO cleanup / close_extra_pages, which
# otherwise freezes the whole batch on an await that never resolves.
CDP_CALL_TIMEOUT_S = float(os.environ.get("CDP_CALL_TIMEOUT_S", "5.0"))
GOTO_TIMEOUT_MS = 30000
# verydio-style pages gate /api/checkin on a Turnstile token that the widget
# fills ~5s after load; CTA_WAIT_S(8) is not enough after an SSO login.
POLL_TURNSTILE_S = float(os.environ.get("POLL_TURNSTILE_S", "0.6"))
TURNSTILE_TOKEN_WAIT_S = float(os.environ.get("TURNSTILE_TOKEN_WAIT_S", "12"))
# Connect retry — a single CDP handshake hiccup must not kill the whole batch.
CONNECT_RETRIES = int(os.environ.get("CONNECT_RETRIES", "3"))
CONNECT_RETRY_BACKOFF_S = float(os.environ.get("CONNECT_RETRY_BACKOFF_S", "1.5"))
# Per-site wall clock — one stuck site (CF + SSO + waits) cannot stall the batch.
SITE_TIMEOUT_S = float(os.environ.get("SITE_TIMEOUT_S", "120"))
BATCH_TIMEOUT_S = float(os.environ.get("BATCH_TIMEOUT_S", "6600"))
BATCH_CLEANUP_RESERVE_S = float(os.environ.get("BATCH_CLEANUP_RESERVE_S", "30"))
# One configured directory owns wrapper logs, JSONL, last-run, DB defaults and lock.
CHECKIN_LOG_DIR = Path(
    os.environ.get("DAILY_CHECKIN_LOG_DIR", str(Path.home() / ".hermes" / "checkin"))
).expanduser()
LAST_RUN_STATUS = CHECKIN_LOG_DIR / "last-run.json"
LAST_ATTEMPT_STATUS = CHECKIN_LOG_DIR / "last-attempt.json"
RUN_LOCK_PATH = CHECKIN_LOG_DIR / "daily-checkin.lock"
PROVIDER_ENGINE = os.environ.get("CHECKIN_PROVIDER_ENGINE", "provider").strip().lower()

# Account+password credential document (Obsidian vault, iCloud-synced, NOT in git).
# Sites that need username/password login (no OAuth) read their creds from here.
# Override path via env; default is the vault doc Note/Accounts/网络账号/签到公益站账密.md.
# On non-macOS hosts the iCloud vault path is meaningless, so the default points
# at a local 0600 file instead; operators should set DAILY_CHECKIN_ACCOUNTS_FILE
# explicitly on non-macOS hosts to point at the provisioned credential doc.
if sys.platform == "darwin":
    DEFAULT_ACCOUNTS_FILE = (
        Path.home()
        / "Library"
        / "Mobile Documents"
        / "iCloud~md~obsidian"
        / "Documents"
        / "obsidian-note"
        / "Note"
        / "Accounts"
        / "网络账号"
        / "签到公益站账密.md"
    )
else:
    DEFAULT_ACCOUNTS_FILE = Path.home() / ".config" / "daily-checkin" / "accounts.md"
ACCOUNTS_FILE = Path(
    os.environ.get("DAILY_CHECKIN_ACCOUNTS_FILE", str(DEFAULT_ACCOUNTS_FILE))
).expanduser()

# STRICT business confirm only — bare "已签到" in calendars/history is NOT enough
STRONG_SUCCESS_PATTERNS = (
    r"今日已签到",
    r"今日已签(?!到?\s*按钮)",
    # 「签到成功」仅当归为真实完成态确认:若后接「后才能/后可/随后才」等
    # 说明性/未来条件词(如 lucky0625 的「签到成功后才能翻牌」)是页面常驻
    # 指引文案,并非当日已签状态,必须排除,否则未签到也会被误判为 ALREADY。
    # 真实的完成态是「恭喜完成/独立句号结尾」或「今日+额度」结算句。
    r"今日签到成功",
    r"签到成功(?![^{}]{0,6}?(?:后|之后|再|才|可|就|随即|方|便))",
    r"领取成功(?!.{0,4}?(?:后|再|才|即可))",
    r"今日已领取",
    r"已经签到",
    r"今天的叶子已经摘过啦",
    r"already\s*checked\s*in",
    r"check[- ]?in\s*successful",
    r"checked\s*in\s*today",
    r"今日签到完成",
    r"签到完成",
)
# Weak tokens only valid WITH day context (handled in is_valid_checkin_confirm)
WEAK_NEED_TODAY = ("已签到", "已领取", "Checked in", "already checked")
# NOTE: never use bare 额度/获得/已完成/恭喜/历史日历「已签到」

# CTA priority: 立即签到 / 打卡 / Check in. Never prefer section header「每日签到…描述」
DEFAULT_SIGN_SELECTORS = [
    'button:has-text("立即签到")',
    'button:has-text("签到领取奖励")',
    'button:has-text("今日签到")',
    'button:has-text("立即打卡")',
    'button:has-text("今日打卡")',
    'button:has-text("打卡")',
    'button:has-text("Check in")',
    'button:has-text("Check-in")',
    'button:has-text("Checkin")',
    'button:has-text("签到"):not(:has-text("每日签到"))',
    'button:has-text("签到")',  # last; filtered by is_sign_cta_text
    'a:has-text("立即签到")',
    'a:has-text("签到")',
    'a:has-text("打卡")',
    '[role="button"]:has-text("立即签到")',
    '[role="button"]:has-text("签到")',
    '[role="button"]:has-text("打卡")',
    'div[class*="btn"]:has-text("立即签到")',
    'div[class*="btn"]:has-text("签到")',
    'div[class*="button"]:has-text("立即签到")',
    'div[class*="button"]:has-text("签到")',
]

# New-API / 七倍算力 personal pages: 立即签到 + toast/disabled 已签到
NEWAPI_SIGN_SELECTORS = [
    'button:has-text("立即签到")',
    'button:has-text("签到领取奖励")',
    'button:has-text("今日签到")',
    'button:has-text("签到"):not(:has-text("每日签到"))',
    'button:has-text("签到")',
    'a:has-text("立即签到")',
    'a:has-text("签到")',
    '[role="button"]:has-text("立即签到")',
    '[role="button"]:has-text("签到")',
]
NEWAPI_ALREADY_SELECTORS = [
    'button:has-text("已签到")',
    'button:has-text("今日已签到")',
    'text=签到成功',
    'text=今日已签到',
    'text=今天已签',
]


OVERLAY_ZAPPER_JS = """
() => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27 }));
  let n = 0;
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if (s.position !== 'fixed' && s.position !== 'absolute') continue;
    const z = parseInt(s.zIndex);
    if (isNaN(z) || z <= 99) continue;
    const c = (el.className && typeof el.className === 'string') ? el.className.toLowerCase() : '';
    if (c.includes('modal') || c.includes('dialog') || c.includes('overlay') || c.includes('popup')) {
      el.style.display = 'none';
      n++;
    }
  }
  return n;
}
"""

LINUXDO_SELECTORS = [
    'button:has-text("使用 LinuxDO 继续")',
    'button:has-text("使用 Linux DO 继续")',
    'button:has-text("使用 Linux Do 登录")',
    'button:has-text("使用 Linux.do 登录")',
    'button:has-text("使用 Linux.do")',
    'button:has-text("使用 LinuxDO")',
    'button:has-text("使用 Linux DO")',
    'button:has-text("使用 Linux Do")',
    'button:has-text("Continue with LinuxDO")',
    'button:has-text("Continue with Linux DO")',
    'button:has-text("Continue with Linux")',
    'button:has-text("LinuxDO")',
    'button:has-text("Linux DO")',
    'button:has-text("Linux Do")',
    'button:has-text("Linux.do")',
    'a:has-text("使用 LinuxDO 继续")',
    'a:has-text("使用 Linux DO")',
    'a:has-text("使用 Linux Do")',
    'a:has-text("使用 Linux.do 登录")',
    'a:has-text("使用 Linux.do")',
    'a:has-text("Continue with Linux")',
    'a:has-text("LinuxDO")',
    'a:has-text("Linux DO")',
    'a:has-text("Linux.do")',
    # Formerly bare `text=/regex/` which matched ANY element — including the
    # ark717 #coinLine decorative dead div (no click handler → SSO timeout).
    # Role-scoped has-text for sites that render the CTA as role="button"
    # instead of <button>/<a>. Dead divs can no longer match.
    '[role="button"]:has-text("使用 LinuxDO 继续")',
    '[role="button"]:has-text("使用 Linux DO 继续")',
    '[role="button"]:has-text("使用 LinuxDo 登录")',
    '[role="button"]:has-text("使用 Linux.do 登录")',
    '[role="button"]:has-text("使用 LinuxDO")',
    '[role="button"]:has-text("使用 Linux DO")',
    '[role="button"]:has-text("使用 Linux Do")',
    '[role="button"]:has-text("使用 Linux.do")',
    '[role="button"]:has-text("Continue with LinuxDO")',
    '[role="button"]:has-text("Continue with Linux DO")',
    '[role="button"]:has-text("Continue with Linux")',
    '[role="button"]:has-text("LinuxDO")',
    '[role="button"]:has-text("Linux DO")',
    '[role="button"]:has-text("Linux Do")',
    '[role="button"]:has-text("Linux.do")',
]

# agentrouter.org 站点登录页的 GitHub 入口 CTA(仿 LINUXDO_SELECTORS 写法,
# button/a/role=button has-text 风格,已登录站点通常不再展示登录入口)。
GITHUB_SELECTORS = [
    'button:has-text("使用 GitHub 继续")',
    'button:has-text("使用 GitHub 登录")',
    'button:has-text("使用 GitHub")',
    'button:has-text("通过 GitHub 登录")',
    'button:has-text("GitHub 登录")',
    'button:has-text("Continue with GitHub")',
    'button:has-text("Sign in with GitHub")',
    'button:has-text("Login with GitHub")',
    'button:has-text("GitHub")',
    'a:has-text("使用 GitHub 继续")',
    'a:has-text("使用 GitHub 登录")',
    'a:has-text("使用 GitHub")',
    'a:has-text("通过 GitHub 登录")',
    'a:has-text("GitHub 登录")',
    'a:has-text("Continue with GitHub")',
    'a:has-text("Sign in with GitHub")',
    'a:has-text("Login with GitHub")',
    'a:has-text("GitHub")',
    # 避免匹配 GitHub 徽标/图标或「GitHub 已登录」过窄的裸 has-text 节点;
    # 不用 text=/regex/ 裸文本(死 div 无click handler 会导致 SSO timeout)。
    '[role="button"]:has-text("使用 GitHub 继续")',
    '[role="button"]:has-text("使用 GitHub 登录")',
    '[role="button"]:has-text("使用 GitHub")',
    '[role="button"]:has-text("Continue with GitHub")',
    '[role="button"]:has-text("GitHub")',
]

# GitHub OAuth 授权页「Authorize/Allow/授权/同意」(仿 AUTHORIZE_SELECTORS)。
# Only match explicit authorization verbs on the GitHub authorize page —
# do not click generic 「确认」/「继续」 from other OAuth dialogs.
GITHUB_AUTHORIZE_SELECTORS = [
    'button:has-text("Authorize")',
    'button:has-text("Allow")',
    'button:has-text("授权")',
    'button:has-text("允许")',
    'button:has-text("同意")',
    'button:has-text("Approve")',
    'button:has-text("授权并继续")',
    'a:has-text("Authorize")',
    'a:has-text("Allow")',
    'a:has-text("授权")',
    'a:has-text("同意")',
    '[role="button"]:has-text("Authorize")',
    '[role="button"]:has-text("Allow")',
    '[role="button"]:has-text("授权")',
]

# agentrouter.org 右上角登出(退出登录/登出/Logout/Sign out,button+a)。
# 多账号场景必须先登出落到未登录态,再走下一个账号的 OAuth 才会触发签到。
AGENTROUTER_LOGOUT_SELECTORS = [
    'button:has-text("退出登录")',
    'button:has-text("登出")',
    'button:has-text("Logout")',
    'button:has-text("Sign out")',
    'button:has-text("退出")',
    'a:has-text("退出登录")',
    'a:has-text("登出")',
    'a:has-text("Logout")',
    'a:has-text("Sign out")',
    'a:has-text("退出")',
    '[role="button"]:has-text("退出登录")',
    '[role="button"]:has-text("登出")',
    '[role="button"]:has-text("Logout")',
    '[role="button"]:has-text("Sign out")',
]

# agentrouter 已登录态登出藏在右上角头像下拉里(2026-08-26 inspect 实证):
# 点 [class*=avatar] 后 dropdown 出现,登出项是 [role=menuitem]/li 文本「退出」。
# 用裸「退出」需限定 menuitem/li 容器,避免误点别处。
AGENTROUTER_LOGOUT_MENU_SELECTORS = [
    '[role="menuitem"]:has-text("退出")',
    '[role="menuitem"]:has-text("登出")',
    '[role="menuitem"]:has-text("退出登录")',
    '[role="menuitem"]:has-text("Logout")',
    '[role="menuitem"]:has-text("Sign out")',
    'li:has-text("退出")',
    'li:has-text("登出")',
    'li:has-text("退出登录")',
    'li:has-text("Logout")',
]

# agentrouter.org 签到成功/已签到 文本标记。
AGENTROUTER_SUCCESS_SELECTORS = [
    'text=今日已签到',
    'text=签到成功',
    'text=今日已签',
    'text=已经签到',
    'text=已签到',
    'text=Check in successful',
    'text=checked in today',
]

# tabitoken.com(TaBiAI / New API 系)签到机制同 agentrouter:GitHub OAuth 登录即触发额度,
# 无签到按钮。单账号(仅 GitHub)。登录态靠 new_api_refresh cookie(httpOnly, path=/api/user/auth)
# 持久化;UI 登出菜单「登出」未绑 handler(2026-08-26 inspect 实证:点击不发任何网络请求、
# cookie 不变),故登出靠清该 cookie 或 POST /api/user/auth/logout。已登录态访问 /login 会
# 自动跳 /dashboard/overview(复用 session),所以必须先清 session 再进 /sign-in 真走 OAuth。
# GitHub CTA 文案与 agentrouter 一致(「使用 GitHub 继续」),直接复用 GITHUB_SELECTORS 常量。

AUTHORIZE_SELECTORS = [
    # LinuxDO Connect confirmation page uses「以 @name 的身份授权」on the
    # positive action. Keep this list authorization-specific: do not click
    # generic「确认」or「继续」buttons from other OAuth dialogs.
    'button:has-text("身份授权")',
    'button:has-text("允许")',
    'button:has-text("授权")',
    'button:has-text("同意")',
    'button:has-text("Authorize")',
    'button:has-text("Allow")',
    'button:has-text("Approve")',
    'a:has-text("身份授权")',
    'a:has-text("允许")',
    'a:has-text("授权")',
    'a:has-text("Authorize")',
    'a:has-text("Allow")',
    '[role="button"]:has-text("身份授权")',
    '[role="button"]:has-text("授权")',
]

LOGIN_SELECTORS = [
    # :not(:has-text("退出")) guards substring match — bare has-text("登录")
    # also matches "退出登录" header buttons and would force-click logout,
    # dropping the session mid-SSO. Same for Login/Sign out in EN.
    'button:has-text("登录"):not(:has-text("退出"))',
    'a:has-text("登录"):not(:has-text("退出"))',
    'button:has-text("Login"):not(:has-text("out")):not(:has-text("Out"))',
    'a:has-text("Login"):not(:has-text("out")):not(:has-text("Out"))',
    'button:has-text("Sign in"):not(:has-text("out")):not(:has-text("Out"))',
    'a:has-text("Sign in"):not(:has-text("out")):not(:has-text("Out"))',
    'a:has-text("进入签到")',
    'button:has-text("进入签到")',
]

# Account+password login form selectors (纯账密站 — no OAuth). Unified across
# newapi/Sub2API clones: password box is always input[type=password]; account box
# varies (#email / #username / type=email); submit is 登录/继续. Order = most specific first.
ACCOUNT_FIELD_SELECTORS = [
    "#email",
    "#username",
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input[placeholder*='邮箱']",
    "input[placeholder*='用户名']",
    "input[placeholder*='手机号']",
]
PASSWORD_FIELD_SELECTORS = [
    "input[type='password']",
    "#password",
    "input[name='password']",
]
LOGIN_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    'button:has-text("登录"):not(:has-text("退出"))',
    'button:has-text("继续")',
    'a:has-text("登录"):not(:has-text("退出"))',
]

# Terms/privacy only — no blanket ant/el/role checkboxes (those are handled in
# check_terms() with nearby-label filter so we never force-click "remember me").
TERMS_CLICK_SELECTORS = [
    'label:has-text("协议")',
    'label:has-text("同意")',
    'label:has-text("服务条款")',
    'label:has-text("隐私")',
    'label:has-text("我已阅读")',
    'label:has-text("已阅读")',
    'label:has-text("Terms")',
    'label:has-text("Privacy")',
    'text=/我已阅读.*协议/',
    'text=/同意.*协议/',
    'text=/同意.*条款/',
    'text=/I agree/i',
    'text=/I have read/i',
    # Semi Design (e.g. maoyulin login): input is in .semi-checkbox-inner, the
    # text span sits beside it, and closest("[class*='checkbox']") hits the empty
    # inner span first — so hit the wrapper directly by text.
    '.semi-checkbox:has-text("协议")',
    '.semi-checkbox:has-text("同意")',
]

Status = Literal["OK", "ALREADY", "FAIL"]
AdapterKind = Literal["browser", "bohe", "newapi_profile", "arkengine", "agentrouter", "tabitoken", "justwoker", "gorouter", "mzlone", "ultrarouter", "nexa"]


@dataclass
class CheckinResult:
    """Metapi-inspired structured check-in result (KISS)."""

    status: Status
    reason: str = ""
    detail: str = ""
    latency_ms: int = 0
    site: str = ""
    adapter: str = "browser"
    marked: bool = False
    # Phase A shadow observability. These fields do not change legacy outcomes.
    provider: str = "legacy_browser"
    stage: str = ""
    action: ActionEvidence | None = None
    confirmation: ConfirmationEvidence | None = None
    identity: IdentityEvidence | None = None
    pre_state: str = ""
    post_state: str = ""
    transition: str = ""
    attribution: str = "legacy_unknown"
    business_status: str = ""
    business_reason: str = ""
    projection_status: str = "not_attempted"
    projection_reason: str = ""
    # True → 该站当日未到窗口/暂不处理,写回时保持 daily_tasks=pending,
    # 让后续批次(如 cron 08:10)在窗口内正常重跑,而非误判 done/failed。
    keep_pending: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("OK", "ALREADY")

    def msg(self) -> str:
        if self.ok:
            return self.status
        if self.reason and self.detail:
            return f"FAIL:{self.reason} ({self.detail})"[:160]
        if self.reason:
            return f"FAIL:{self.reason}"[:160]
        return f"FAIL:{self.detail or 'unknown'}"[:160]


def ok_result(status: Status = "OK", **kw) -> CheckinResult:
    return CheckinResult(status=status, **kw)


def fail_result(reason: str, detail: str = "", **kw) -> CheckinResult:
    return CheckinResult(status="FAIL", reason=reason, detail=detail, **kw)


def pending_result(reason: str, detail: str = "", **kw) -> CheckinResult:
    """业务未到期/窗口未开:FAIL + keep_pending。写回时保持 daily_tasks=pending,
    不标 done(避免误判已签)也不标 failed(避免 cron 不再重试)。"""
    return CheckinResult(status="FAIL", reason=reason, detail=detail, keep_pending=True, **kw)


def result_from_legacy(s: str, **kw) -> CheckinResult:
    """Bridge old string returns → CheckinResult (SSO path still uses strings)."""
    if s in ("OK", "ALREADY"):
        return ok_result(s, **kw)  # type: ignore[arg-type]
    text = (s or "").strip()
    if text.startswith("FAIL:not logged in"):
        m = re.search(r"\(([^)]+)\)", text)
        reason = (m.group(1) if m else "not_logged_in").lower().replace(" ", "_")
        return fail_result(reason, detail=text, **kw)
    if text.startswith("FAIL:"):
        body = text[5:]
        if body.startswith("button not visible"):
            return fail_result("no_button", detail=body, **kw)
        if "开始转动" in body:
            return fail_result("no_spin", detail=body, **kw)
        if body.startswith("no "):
            return fail_result("no_button", detail=body, **kw)
        return fail_result("error", detail=body, **kw)
    if text.startswith("CRASH:"):
        return fail_result("crash", detail=text, **kw)
    return fail_result("unknown", detail=text, **kw)


@dataclass
class SiteAdapter:
    """Light site adapter. kind is classification + selector pack; only bohe/arkengine/agentrouter have extra flow."""

    name: str
    url: str
    kind: AdapterKind = "browser"
    sign_selectors: list[str] = field(default_factory=list)
    already_selectors: list[str] = field(default_factory=list)
    ready_rounds: int = 15
    prefer_cta_before_auth: bool = False
    use_native_click: bool = False
    use_overlay_zapper: bool = True
    trusted_sign_selectors: bool = False
    trusted_already_selectors: bool = False
    prefer_catalog_url: bool = False
    feature_unavailable_reason: str = ""
    # Absolute or site-relative JSON sign-in API (anyrouter: /api/user/sign_in).
    # Such sites don't show a 「签到」 CTA nor a 「已签到」 text — their console
    # silently POSTs this on load and credits balance. We call it directly;
    # the endpoint is idempotent (an already-signed session still returns
    # success:true), so a live logged-in session can land the sign-in anytime.
    signin_api: str = ""
    # signin_api sites whose API layer also demands the X-New-Api-User / custom
    # "New-Api-User: <localStorage.uid>" identity header (hcnsec edge/proxy style).
    # try_signin_api then injects localStorage uid into the request header.
    signin_api_uid_header: bool = False
    # agentrouter: the sign button may be a bare <button>「签到」/「领取」 with
    # NO click handler visible to the nav/CTA heuristics (its reward is granted
    # on the login trigger, not the button). The dedicated agentrouter flow
    # always treats an explicit button match as clickable instead of
    # trusting the generic CTA classifier to prove an onclick attribute.
    trust_cta_has_text: bool = False
    # Pure-account sites whose real login page is NOT scheme://netloc/login.
    # gemai(api.gemai.cc) 的 /login 直接 404,真登录卡在 /sign-in;columbina 靠
    # /login→/sign-in 重定向所以不需要。_ensure_account_logged_in 用该值
    # override 派生 URL(缺省回退到 /login 派生)。
    login_url: str = ""


def _A(
    name: str,
    url: str,
    signs: list[str],
    already: list[str],
    kind: AdapterKind = "browser",
    ready_rounds: int = 15,
) -> SiteAdapter:
    return SiteAdapter(
        name=name,
        url=url,
        kind=kind,
        sign_selectors=signs,
        already_selectors=already,
        ready_rounds=ready_rounds,
        trusted_sign_selectors=True,
        trusted_already_selectors=True,
    )


# name → adapter. 薄荷 = bohe; New-API personal = newapi_profile
def _N(
    name: str,
    url: str,
    signs: list[str] | None = None,
    already: list[str] | None = None,
    ready_rounds: int = 15,
    prefer_catalog_url: bool = False,
) -> SiteAdapter:
    """New-API style personal/check-in page (7x pattern)."""
    return SiteAdapter(
        name=name,
        url=url,
        kind="newapi_profile",
        sign_selectors=list(signs or NEWAPI_SIGN_SELECTORS),
        already_selectors=list(already or NEWAPI_ALREADY_SELECTORS),
        ready_rounds=ready_rounds,
        trusted_sign_selectors=signs is not None,
        trusted_already_selectors=already is not None,
        prefer_catalog_url=prefer_catalog_url,
    )


def _B(
    name: str,
    url: str,
    signs: list[str] | None = None,
    already: list[str] | None = None,
    ready_rounds: int = 15,
    prefer_cta_before_auth: bool = False,
    use_native_click: bool = False,
    use_overlay_zapper: bool = True,
    prefer_catalog_url: bool = False,
    feature_unavailable_reason: str = "",
    signin_api: str = "",
    signin_api_uid_header: bool = False,
    trust_cta_has_text: bool = False,
    kind: AdapterKind = "browser",
    login_url: str = "",
    ) -> SiteAdapter:
    return SiteAdapter(
        name=name,
        url=url,
        kind=kind,
        sign_selectors=list(signs or DEFAULT_SIGN_SELECTORS),
        already_selectors=list(
            already
            or [
                'text=今日已签到',
                'text=签到成功',
                'text=今日已签',
                'button:has-text("已签到")',
                'button:has-text("今日已签到")',
            ]
        ),
        ready_rounds=ready_rounds,
        prefer_cta_before_auth=prefer_cta_before_auth,
        use_native_click=use_native_click,
        use_overlay_zapper=use_overlay_zapper,
        trusted_sign_selectors=signs is not None,
        trusted_already_selectors=already is not None,
        prefer_catalog_url=prefer_catalog_url,
        feature_unavailable_reason=feature_unavailable_reason,
        signin_api=signin_api,
        signin_api_uid_header=signin_api_uid_header,
        trust_cta_has_text=trust_cta_has_text,
        login_url=login_url,
    )


_BUILTIN_SITE_ADAPTERS: list[SiteAdapter] = [
    _A(
        "薄荷公益站",
        "https://up.x666.me/",
        ['button:has-text("开始转动")', 'button:has-text("转动")', "text=开始转动"],
        ['text=今日已签到', 'text=签到成功', 'text=今日已签'],
        kind="bohe",
    ),
    _A(
        "小鸡毛",
        "https://game.ark717.com/",
        ['button:has-text("签到")', '#checkinBtn'],
        ['text=今日已签到', 'text=已签到', 'text=签到成功'],
        kind="arkengine",
    ),
    # --- New-API personal / profile (立即签到 + 已签到/今天 +¥) ---
    _N("HotaruAPI", "https://free.lyclaude.site/console/personal"),
    _N(
        "ai.52ccl.cn",
        "https://52ccl.net/profile",
        prefer_catalog_url=True,
    ),
    _N("muyuan", "https://muyuan.do/console/personal"),
    _N("42", "https://api.42w.shop/profile"),
    _N("jiuitj", "https://jiuuij.de5.net/console/personal"),
    _N("afsmc", "https://api.afsmc.cn/profile"),
    _N("zmingu", "https://api.zmingu.app/profile"),
    # fengwind(Fengwind API 福利站):首页即签到页(非 New API profile)。
    # 有「签到」按钮(在今日福利卡内,button[data-slot/button] 或带签到文案),
    # 已签判据:「今日已签到」/「签到成功」/「已签到」;带 is_fengwind_site 专属
    # 逻辑(maybe_select_fengwind_validity 选 2 天配额)。已在 DB enabled,补内置定义。
    _B(
        "fengwind",
        "https://api-welfalre.fengwind.com/",
        signs=[
            'button[data-slot="button"]:has-text("签到")',
            'button.bg-primary:has-text("签到")',
            'button:not([role="tab"]):has-text("签到")',
            'button:has-text("签到")',
        ],
        already=['text=今日已签到', 'text=签到成功', 'text=已签到'],
    ),
    _B(
        "林夕",
        "https://k40.shengqainbang.cn/check-in",
        signs=['button:has-text("立即签到")', 'button:has-text("签到")', 'button:has-text("今日签到")'],
    ),
    _B(
        "百倍",
        "https://sub.100xlabs.space/check-in",
        signs=['button:has-text("立即签到")', 'button:has-text("签到")', 'button:has-text("今日签到")'],
        already=['text=今日已签到', 'text=今日已签', 'text=签到成功'],
        use_native_click=True,
    ),
    _N("huaibao", "https://ai.huaibao.top/console/personal"),
    _N("猫", "https://maoyulin.xyz/console/personal"),
    _N("魔方", "https://www.mofas.one/console/personal"),
    _N("glm5.2 公益站", "https://grajsh3017-newapi.hf.space/profile"),
    _N("魔芋", "https://www.moyu.cn/console/personal"),
    _B(
        "windhub",
        "https://windhub.cc/console/personal",
        signs=[
            'button:has-text("签到"):not([disabled])',
            'button.semi-button-primary:has-text("签到")',
            'button:has-text("立即签到")',
        ],
        already=[
            'button:has-text("今日已签到")',
            'text=今日已签到',
            'text="今日已签到"',
        ],
    ),
    _N("mistral公益站", "https://translate.gzcrtw.com/console/personal"),
    _N("neb公益站", "https://ai.9q.hk/profile"),
    _N("huan", "https://ai.huan666.de/console/personal"),
    _N("ciallo", "https://ioll.pp.ua/profile"),
    _N("咕嘎咕嘎生图站", "https://ai.xmiaom.com/profile"),
    _N("午夜", "https://api.lyjxka.top/profile"),
    _B(
        "abrdns",
        "https://checkin.new-api.abrdns.com/checkin",
        signs=[
            'button:has-text("签到领取奖励")',
            'button:has-text("签到")',
            'a:has-text("进入签到")',
            'button:has-text("进入签到")',
        ],
        already=['text=今日已签到', 'text="今日已签到"', 'text=签到成功'],
        prefer_catalog_url=True,
    ),
    _B(
        "cross",
        "https://newapi-checkin.keungliang.dpdns.org/",
        signs=['button:has-text("立即签到")', 'button:has-text("签到")', 'button:has-text("使用 Linux Do 登录")'],
    ),
    _B(
        "guxiaomo",
        "https://api.guxiaomo.site/wallet",
        signs=['button:has-text("立即签到")', 'button:has-text("签到")'],
        # 「每日仅可签到一次」是 DO 站常驻说明文案,未签到页也有,绝不能当已签(同
        # llmroutes/mzlone 的 New-API 注意点)。guxiaomo /profile 实测(2026-09-02):
        # 未签页显示可点的「立即签到」按钮+静态「每日签到可获得随机额度奖励/每日仅可签到
        # 一次」说明,判已只能用「今日已签到/签到成功/立即签到按钮消失」,否则会假 ALREADY
        # 跳过真实签到(2026-08-30 起方变假 ALREADY,当月累计金额一直为 $0)。
        already=['text=今日已签到', 'text=签到成功'],
        prefer_catalog_url=True,
    ),
    # yeelo(img.yeelo.fun):登录成功/LinuxDO SSO 后落在 /admin/user-info,
    # 页面有可点的「今日签到」按钮,已签后文案变「今天已签到，明天再来领取」。
    # 专属 adapter 让 CTA 精确命中「今日签到」按钮,且确认态用其自己的文案
    # (「已签到 4 天」是本月签到统计,不是当日已签 — 见 is_valid_checkin_confirm
    # 2026-08-26 修复:统计句禁止与「今天(还)未签到」跨行拼成假已签到)。
    _B(
        "yeelo",
        "https://img.yeelo.fun/admin/user-info",
        signs=['button:has-text("今日签到")', 'button:has-text("签到")'],
        already=[
            'text=今天已签到',
            'text=签到成功',
            'text=今日已签到',
        ],
        prefer_catalog_url=True,
    ),
    # columbina(newapi.columbina.eu.org):New API 系,密码框渲染成
    # input[name=password] type=text(2026-08-26 inspect),凭据登录后
    # /profile 显示「每日签到 已签到 今天 +$X」→ 登录即签到。绑定真账密
    # 走 _ensure_account_logged_in 先登录,再落 profile 判定 done 态。
    _B(
        "columbina",
        "https://newapi.columbina.eu.org/profile",
        signs=['button:has-text("立即签到")', 'button:has-text("签到")', 'button:has-text("今日签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=已签到', 'text=今天已签到'],
        prefer_catalog_url=True,
    ),
    # gemai(api.gemai.cc, 哈基米API站):New API 系账密登录站,登录卡在 /sign-in
    # (/login 直接 404,2026-08-26 CDP 实测),账号 input[name=username] +
    # password input[name=password]type=password,含 legal 条款 checkbox。绑定
    # 真账密走 _ensure_account_logged_in(login_url override 到 /sign-in),登录后
    # /profile 判定签到态。
    _B(
        "gemai",
        "https://api.gemai.cc/profile",
        signs=['button:has-text("立即签到")', 'button:has-text("签到")', 'button:has-text("今日签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=已签到', 'text=今天已签到'],
        prefer_catalog_url=True,
        login_url="https://api.gemai.cc/sign-in",
    ),
    # helpcoder(helpcoder.cc, 帮帮程序员):New API 系纯账密站,登录卡在 /login
    # (「用户名或邮箱/密码/继续」,无 OAuth)。绑定真账密走 _ensure_account_logged_in
    # (默认派生 /login 即可),登录后 /console/personal 判定签到态。
    _B(
        "helpcoder",
        "https://helpcoder.cc/console/personal",
        signs=['button:has-text("立即签到")', 'button:has-text("签到")', 'button:has-text("今日签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=已签到', 'text=今天已签到'],
        prefer_catalog_url=True,
    ),
    # hcnsec(api.hcnsec.cn, 新疆幻城网安公益网关):New API 系纯账密站,登录卡在
    # /sign-in(/login 404),username/password + legal-consent 条款 checkbox(不勾
    # submit disabled,且 label.click 不触发——须 JS click 原生 checkbox)。真实
    # 登录落 /dashboard/overview(非 /console/personal,该路径 SPA 404 shell)。
    _B(
        "hcnsec",
        "https://api.hcnsec.cn/dashboard/overview",
        signs=['button:has-text("签到")', 'button:has-text("立即签到")', 'button:has-text("今日签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=已签到', 'text=今天已签到'],
        prefer_catalog_url=True,
        login_url="https://api.hcnsec.cn/sign-in",
        signin_api="/api/user/checkin",
        signin_api_uid_header=True,
    ),
    _N("DGB公益站", "https://freeapi.dgbmc.top/console/personal"),
    _N("chengmo", "https://api.chengmo.cc.cd/profile"),
    _N("rugao", "https://new-api.rugao.me/profile"),
    _B(
        "图片公益站",
        "https://wisart.kuaileshifu.com/#/me",
        signs=['button:has-text("签到")', 'text=签到', 'button:has-text("立即签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text="今日已签"'],
    ),
    _N("ANG", "https://oai.angforever.top/profile"),
    _N("福星", "https://www.fuxingapi.com/console/personal"),
    _N("v-api", "https://v-api.de5.net/profile"),
    _N("hlool", "https://api.hlool.top/profile"),
    _N("7r", "https://penis.7r.fit/console/personal"),
    _N("简直了", "https://jianzhile.vip/console/personal"),
    _N("无名", "https://welfare.0xpsyche.me/profile"),
    _N("子非鱼", "https://ai.112102.xyz/profile"),
    _N("7倍", "https://7x.hk/profile"),
    _N(
        "yoct",
        "https://yoct.cn/profile",
        signs=['button:has-text("立即签到")', '[role="button"]:has-text("立即签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
    ),
    _B(
        "anyrouter",
        "https://anyrouter.top/console",
        signs=['button:has-text("签到")', 'button:has-text("Check in")'],
        already=['text=今日已签到', 'text=签到成功', 'text=Checked in today', 'text=already checked in'],
        # anyrouter 无「签到」CTA 也无「已签到」文本——console 加载时前端自动
        # POST /api/user/sign_in 并静默加额度。直接调该 API(幂等,已登录即可签到)。
        signin_api="/api/user/sign_in",
    ),
    # agentrouter.org 签到触发机制特殊:必须登出后重新登录一次才触发。
    # 双账号(GitHub + Linux.do OAuth)由专属 agentrouter_checkin 流程处理,
    # 不挂账密凭据(OAuth 登录态在 staging Chrome profile 持久化)。
    _B(
        "agentrouter",
        "https://agentrouter.org/",
        signs=['button:has-text("签到")', 'button:has-text("今日签到")', 'button:has-text("立即签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=今日已签', 'text=已经签到'],
        kind="agentrouter",
    ),
    # tabitoken.com:同 agentrouter,登录即触发额度(无签到按钮/文字),单账号(仅 GitHub)。
    # 专属 tabitoken_checkin 流程(清 session cookie 再 GitHub 登录),不挂账密凭据。
    _B(
        "tabitoken",
        "https://tabitoken.com/",
        signs=['button:has-text("签到")', 'button:has-text("今日签到")', 'button:has-text("立即签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=今日已签', 'text=已经签到'],
        kind="tabitoken",
    ),
    # justwoker(api.justwoker.icu, JustDoWork):同 tabitoken 的 New API 系,
    # 签到机制=GitHub OAuth 登录即触发额度,无签到按钮/确认文字。单账号(仅
    # GitHub OAuth),登录态依赖 cookie;未登录态 / → /sign-in?redirect=%2F,
    # 登录卡有「使用 GitHub 继续」。专属 justwoker_checkin 流程(清 session →
    # /sign-in → GitHub OAuth → 回跳即签到 OK),不挂账密凭据。
    _B(
        "justwoker",
        "https://api.justwoker.icu/",
        signs=['button:has-text("使用 GitHub 继续")'],
        already=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
        kind="justwoker",
    ),
    # gorouter.app(GoRouter, New API 系):同 justwoker/tabitoken 的 GitHub
    # OAuth 登录即触发额度签到,无签到按钮/确认文字。单账号(仅 GitHub OAuth),
    # 登录态依赖 cookie;未登录态 /sign-in?redirect=%2Fdashboard%2Foverview,
    # 登录卡有「使用 GitHub 继续」。专属 gorouter_checkin 流程(清 session →
    # /sign-in → GitHub OAuth → 回跳即签到 OK),不挂账密凭据。
    _B(
        "gorouter",
        "https://gorouter.app/",
        signs=['button:has-text("使用 GitHub 继续")'],
        already=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
        kind="gorouter",
    ),
    # mzlone.top(AIMZ / New API 系):**GitHub 登录 + 正常 newapi 签到 两步**。
    # 与 gorouter/justwoker 不同——登录不算签,登录后 /profile 还有真「立即签到」
    # 按钮(「每日签到/每日签到可获得随机额度奖励」)。专属 mzlone_checkin 流程:
    # /sign-in → GitHub OAuth 登录 → 落 /profile → 点「立即签到」→ 签到成功。
    _B(
        "mzlone",
        "https://mzlone.top/profile",
        signs=['button:has-text("立即签到")', 'button:has-text("每日签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=今日已签', 'text=已签到'],
        prefer_catalog_url=True,
        kind="mzlone",
    ),
    _N("小喵喵", "https://cngov.cc.cd/custom/7920deaddf8828b7"),
    _N("快跑", "https://kuaipao.ai/console/personal"),
    _N("CHY", "https://chybenzun.top/profile"),
    _B(
        "chybenzun VPN",
        "https://dy.chybenzun.top/",
        signs=['button:has-text("签到")', 'button:has-text("Check-in")'],
    ),
    _B(
        "nodeseek",
        "https://www.nodeseek.com/board",
        signs=['button:has-text("试试手气")', '[role="button"]:has-text("试试手气")'],
        already=['text=今日签到获得', 'text=今日已签到', 'text=今日已签'],
    ),
    _B(
        "lucky0625 fuli",
        "https://fuli.lucky0625.qzz.io/",
        # 福利小站:真 CTA 是「摘一片四叶草 · 签到」(<button> 带签到 reward),
        # 已签后变成 disabled 「今天的叶子已经摘过啦」。
        # 注意:「已摘完」与「签到成功」不能作为 already 选择器，因为页面下方限量活动显示「已摘完」，
        # 且翻牌说明文字含「签到成功后才能翻牌」，会导致未签到时被误判为 ALREADY。
        signs=['button:has-text("摘一片四叶草")', 'button:has-text("摘一片四叶草 · 签到")'],
        already=['text=今天的叶子已经摘过啦', 'button:has-text("今天的叶子已经摘过啦")',
                 'text=每日签到 · 今日已签到', 'text=今日已签到', 'text=今日已签'],
    ),
    _B(
        "mulink",
        "https://demo.dev2.mulink.top/wallet",
        signs=[
            'button:has-text("立即打卡")',
            'button:has-text("打卡")',
            'button:has-text("立即签到")',
            'button:has-text("签到")',
        ],
        already=[
            'text=今日已打卡',
            'text=今天已打卡',
            'text=今日已签到',
            'text=签到成功',
            'text=今日已签',
            'button:has-text("今日已打卡")',
            'button:has-text("已打卡")',
            'button:has-text("已签到")',
        ],
        prefer_cta_before_auth=True,
    ),
    _B(
        "wxiai",
        "https://api.wxiai.com/workspace",
        signs=['button:has-text("立即签到")', '[role="button"]:has-text("立即签到")'],
        already=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
        ready_rounds=30,
        prefer_cta_before_auth=True,
        use_native_click=True,
        use_overlay_zapper=False,
    ),
    _B(
        "llmpm",
        "https://api.llm.pm/checkin",
        signs=['button:has-text("立即签到")', '[role="button"]:has-text("立即签到")'],
        already=[
            'text=今日已签到',
            'text=签到成功',
            'text=今日已签',
            'button:has-text("已签到")',
            'button:has-text("今日已签到")',
        ],
        ready_rounds=30,
        prefer_cta_before_auth=True,
    ),
    # 47 wraps the real check-in in an iframe at /47/checkin/; the wrapper page
    # (/custom/daily-checkin) only renders the sidebar nav, so _B must target the
    # iframe URL directly to see the real 立即签到 button + 今日已签到 confirm.
    _B(
        "47",
        "https://47.47-gpt.com/47/checkin/",
        signs=['button:has-text("立即签到")', '[role="button"]:has-text("立即签到")'],
        already=[
            'text=今日已签到',
            'text=今日签到已完成',
            'text=签到成功',
        ],
        ready_rounds=25,
        prefer_catalog_url=True,
    ),
    _B(
        "mlgb7",
        "https://image.mlgb7.com/account",
        signs=['button:has-text("今日签到")', 'button:has-text("签到")', 'button:has-text("立即签到")'],
        already=['text=已签到', 'text=今日已签到', 'text=签到成功', 'text=今日已签'],
        prefer_catalog_url=True,
    ),
    _B(
        "604020",
        "https://search.604020.xyz/",
        feature_unavailable_reason="loaded Searchix dashboard has no check-in feature",
    ),
    # ddcat CTA「签到 +0.5」lives inside a layout the generic scanner classifies
    # as navigation → generic CTA rejects it as a nav node.  Trusted sign
    # selectors bypass that heuristic so the button is matched directly.
    _B(
        "ddcat",
        "https://ddcat.pronhubcn.com/wallet",
        signs=[
            'button:has-text("签到 +0.5")',
            'button:has-text("签到")',
        ],
        already=[
            'text=今日已签到',
            'text=已签到',
            'text=签到成功',
            'button:has-text("已签到")',
        ],
        use_native_click=True,
    ),
    _B(
        "pipiwangcom",
        "https://img.pipiwangcom.com/",
        signs=[
            'button:has-text("签到")',
            'button:has-text("立即签到")',
            'button:has-text("今日签到")',
        ],
        already=[
            'text=今日已签到',
            'text=签到成功',
            'text=今日已签',
            'button:has-text("已签到")',
        ],
        # LinuxDO-OAuth 站：顶栏恒常的「签到 +10 / 签到」按钮在登录态逸出时也会出现在
        # 未登录页。prefer_cta_before_auth 会在 SSO 前提前命中该按钮而跳过登录，
        # 会话过期即报 auth_required。必须走默认流程：未登录 → LinuxDO SSO 先行。
    ),
    _B(
        "pigeonw",
        "https://sub2.pigeonw.com/dashboard",
        signs=[
            'button:has-text("立即签到")',
            'button:has-text("今日签到")',
            'button:has-text("签到")',
        ],
        already=[
            'text=今日已签到',
            'text=签到成功',
            'text=今日已签',
            'button:has-text("已签到")',
            'button:has-text("今日已签到")',
        ],
        prefer_cta_before_auth=True,
        use_overlay_zapper=True,
    ),
    _A(
        "老魔",
        "https://api.2020111.xyz/profile",
        signs=[
            'button:has-text("立即签到")',
            'button:has-text("签到领取奖励")',
            'button:has-text("今日签到")',
            'button:has-text("签到"):not(:has-text("每日签到"))',
            'button:has-text("签到")',
        ],
        already=[
            'text=今日已签到',
            'text=签到成功',
            'button:has-text("已签到")',
            'button:has-text("今日已签到")',
        ],
    ),
    _B(
        "arkengine",
        "https://game.arkengine.me/activities/checkin",
        signs=[
            'button:has-text("立即签到")',
            'button:has-text("今日签到")',
            'button:has-text("签到")',
        ],
        already=[
            'text=今日已签到',
            'text=已签到',
            'text=签到成功',
            'button:has-text("已签到")',
        ],
    ),
    _B(
        "arkengine 大转盘",
        "https://game.arkengine.me/activities/wheel",
        signs=[
            'button:has-text("转一次")',
            'button:has-text("次机会")',
        ],
        already=[
            'text=今日次数已用完',
            'text=0次机会',
            'button:has-text("0次机会")',
            'text=明日再来',
            'text=保底进度',
        ],
    ),
]


def _adapter_from_yaml_entry(entry: dict) -> SiteAdapter:
    """Build a SiteAdapter from one sites.yaml entry, using the original
    _A / _N / _B factories to guarantee field-equivalence with the built-in list."""
    name = entry["name"]
    url = entry["url"]
    kind = entry.get("kind", "browser")
    signs = entry.get("signs")           # None = absent (use factory defaults)
    already = entry.get("already")        # None = absent (use factory defaults)
    # Scalar-string values (e.g. `signs: 'button:has-text("签到")'`) are a
    # common typo; normalize into a single-element list so we don't iterate
    # the string char-by-char (a 23-char selector would become 23 bogus
    # selectors) and list semantics stay uniform.
    if isinstance(signs, str):
        signs = [signs]
    if isinstance(already, str):
        already = [already]
    try:
        rr = int(entry.get("ready_rounds", 15))
    except (TypeError, ValueError):
        # Malformed ready_rounds (e.g. `abc`) degrades to the default instead
        # of crashing the whole import at startup.
        print(f"  sites.yaml: bad ready_rounds for {name!r}; using default 15", flush=True)
        rr = 15

    if kind == "bohe":
        return _A(name, url, list(signs or []), list(already or []), kind="bohe", ready_rounds=rr)
    if kind == "arkengine":
        return _A(name, url, list(signs or []), list(already or []), kind="arkengine", ready_rounds=rr)
    if kind == "agentrouter":
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            kind="agentrouter",
        )
    if kind == "tabitoken":
        # tabitoken.com:同 agentrouter 的 New API 系,单账号 GitHub OAuth 登录即签到。
        # 专属 tabitoken_checkin 流程按 kind 分发,kind 必须透传否则走 browser 默认。
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            kind="tabitoken",
        )
    if kind == "justwoker":
        # justwoker(api.justwoker.icu):New API 系,单账号 GitHub OAuth 登录即签到
        # (人口 = 登录触发额度,无签到按钮)。专属 justwoker_checkin 流程按 kind
        # 分发,kind 必须透传否则落到 browser 默认。
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            kind="justwoker",
        )
    if kind == "gorouter":
        # gorouter.app: 站点,单账号 GitHub OAuth 登录即签到(登录触发额度,
        # 无签到按钮)。专属 gorouter_checkin 流程按 kind 分发,kind 必须透传
        # 否则落到 browser 默认。
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            kind="gorouter",
        )
    if kind == "mzlone":
        # mzlone.top:New API 系,GitHub OAuth 登录 + 正常签到两步。专属 mzlone_checkin
        # 流程按 kind 分发,kind 必须透传否则落到 browser 默认。
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            prefer_catalog_url=bool(entry.get("prefer_catalog_url", False)),
            kind="mzlone",
        )
    if kind == "llmroutes":
        # llmroutes.cn:New API 系,双账号(GitHub + Linux Do OAuth)登录 + 正常签到两步。
        # 专属 llmroutes_checkin 流程按 kind 分发,kind 必须透传否则落到 browser 默认。
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            prefer_catalog_url=bool(entry.get("prefer_catalog_url", False)),
            kind="llmroutes",
        )
    if kind == "ultrarouter":
        # ultrarouter.org:New API 克隆。2026-09 GitHub OAuth 被站方移除(/sign-in 只剩
        # LinuxDo),故单账号 LinuxDo 登录 + 正常签到两步。专属 ultrarouter_checkin 流程
        # 按 kind 分发,kind 必须透传否则落到 browser 默认。
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            prefer_catalog_url=bool(entry.get("prefer_catalog_url", False)),
            kind="ultrarouter",
        )
    if kind == "nexa":
        # nexavlinks.com:New API 系双账号(LinuxDO + 邮箱密码)。专属 nexa_checkin
        # 流程按 kind 分发,kind 必须透传否则落到 browser 默认。登出只点站内「退出
        # 登录」(清 NEXA 域 localStorage),P0 红线全程守(见运维手册打卡 new 记录)。
        return _B(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
            prefer_catalog_url=bool(entry.get("prefer_catalog_url", False)),
            kind="nexa",
        )
    if kind == "newapi_profile":
        return _N(
            name, url,
            signs=list(signs) if signs is not None else None,
            already=list(already) if already is not None else None,
            ready_rounds=rr,
            prefer_catalog_url=bool(entry.get("prefer_catalog_url", False)),
        )
    # browser (default)
    return _B(
        name, url,
        signs=list(signs) if signs is not None else None,
        already=list(already) if already is not None else None,
        ready_rounds=rr,
        prefer_cta_before_auth=bool(entry.get("prefer_cta_before_auth", False)),
        use_native_click=bool(entry.get("use_native_click", False)),
        use_overlay_zapper=bool(entry.get("use_overlay_zapper", True)),
        prefer_catalog_url=bool(entry.get("prefer_catalog_url", False)),
        feature_unavailable_reason=entry.get("feature_unavailable_reason", ""),
        signin_api=entry.get("signin_api", ""),
        signin_api_uid_header=bool(entry.get("signin_api_uid_header", False)),
        trust_cta_has_text=bool(entry.get("trust_cta_has_text", False)),
        login_url=entry.get("login_url", ""),
    )


def load_sites_from_yaml(path: Path | None = None) -> list[SiteAdapter]:
    """Load the site catalog from sites.yaml.

    Falls back to the built-in hardcoded list when the file is missing,
    PyYAML is not installed, or parsing fails — keeping production safe.
    """
    yaml_path = Path(
        path
        or os.environ.get(
            "DAILY_CHECKIN_SITES_YAML",
            str(Path(__file__).resolve().parent / "sites.yaml"),
        )
    )
    if not yaml_path.is_file():
        print(f"  sites.yaml not found at {yaml_path}; using built-in catalog", flush=True)
        return list(_BUILTIN_SITE_ADAPTERS)

    try:
        import yaml as _yaml
    except ImportError:
        print("  pyyaml missing; using built-in site catalog", flush=True)
        return list(_BUILTIN_SITE_ADAPTERS)

    try:
        with open(yaml_path, encoding="utf-8") as fh:
            data = _yaml.safe_load(fh) or {}
    except Exception as exc:
        print(f"  sites.yaml read failed ({exc}); using built-in catalog", flush=True)
        return list(_BUILTIN_SITE_ADAPTERS)

    # Guard against scalar root (e.g. `sites: 5` — data is a dict but
    # .get("sites") returns a scalar, not a list).
    if not isinstance(data, dict):
        print(f"  sites.yaml: root is {type(data).__name__}, not dict; using built-in catalog", flush=True)
        return list(_BUILTIN_SITE_ADAPTERS)
    entries = data.get("sites") or []
    if not isinstance(entries, list):
        print(f"  sites.yaml: 'sites' is {type(entries).__name__}, not list; using built-in catalog", flush=True)
        return list(_BUILTIN_SITE_ADAPTERS)
    adapters: list[SiteAdapter] = []
    for e in entries:
        try:
            if not isinstance(e, dict) or not e.get("name") or not e.get("url"):
                print(f"  sites.yaml: skipping malformed entry {e!r}", flush=True)
                continue
            adapters.append(_adapter_from_yaml_entry(e))
        except Exception as exc:
            # Individual entry errors (TypeError, ValueError, etc.) must not
            # crash the whole catalog — skip the bad entry and continue.
            print(f"  sites.yaml: skipping entry {e!r} ({exc})", flush=True)
            continue
    return adapters


SITE_ADAPTERS = load_sites_from_yaml()


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def http_call(method: str, url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw


def cdp_target_list() -> list[dict]:
    data = http_call("GET", f"{CDP_HTTP}/json/list", timeout=5.0)
    return data if isinstance(data, list) else []


def _safe_target_url(raw_url: str) -> str:
    """Keep only origin/path when logging browser target diagnostics."""
    try:
        parsed = urlparse(raw_url or "")
        if not parsed.scheme or not parsed.hostname:
            return parsed.path or parsed.scheme or ""
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}{parsed.path or '/'}"[:240]
    except Exception:
        return ""


def sso_target_snapshot(browser=None) -> list[dict[str, str]]:
    """Return non-sensitive CDP target state for SSO boundary diagnosis."""
    snapshot: list[dict[str, str]] = []
    try:
        targets = cdp_target_list()
    except Exception:
        targets = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("id") or target.get("targetId") or "")
        if not target_id:
            continue
        snapshot.append({
            "id": target_id[:80],
            "opener_id": str(target.get("openerId") or "")[:80],
            "type": str(target.get("type") or "")[:32],
            "url": _safe_target_url(str(target.get("url") or "")),
        })
    if snapshot:
        return snapshot
    # The CDP HTTP endpoint can briefly lag the Playwright connection. Keep a
    # sanitized page fallback so a timeout still records which pages existed.
    try:
        for ctx in browser.contexts if browser is not None else []:
            for page in ctx.pages:
                snapshot.append({
                    "id": "",
                    "opener_id": "",
                    "type": "page",
                    "url": _safe_target_url(str(page.url or "")),
                })
    except Exception:
        pass
    return snapshot


def register_sso_popup_tracker(page, popup_pages: dict[int, Any]) -> dict[str, int | bool]:
    """Attach a synchronous popup observer before the OAuth click."""
    state: dict[str, int | bool] = {"events": 0, "attached": False}

    def track(popup) -> None:
        state["events"] = int(state["events"]) + 1
        popup_pages[id(popup)] = popup

    try:
        page.on("popup", track)
        state["attached"] = True
    except Exception:
        pass
    return state


def page_ids(browser) -> set[int]:
    try:
        return {id(p) for ctx in browser.contexts for p in ctx.pages}
    except Exception:
        return set()


async def page_cdp_target_id(page) -> str | None:
    """Resolve Playwright page → CDP target id. No last-page guess.

    CDP websocket calls are bounded by CDP_CALL_TIMEOUT_S: a half-dead
    websocket (Chrome stuck on a stalled tab) must not hang every caller
    (find_temp_page / SSO cleanup / close_extra_pages) and freeze the batch.
    """
    try:
        cdp = await asyncio.wait_for(
            page.context.new_cdp_session(page),
            timeout=CDP_CALL_TIMEOUT_S,
        )
        try:
            info = await asyncio.wait_for(
                cdp.send("Target.getTargetInfo"),
                timeout=CDP_CALL_TIMEOUT_S,
            )
            tid = (info or {}).get("targetInfo", {}).get("targetId") or (info or {}).get("targetId")
            return tid
        finally:
            try:
                await asyncio.wait_for(cdp.detach(), timeout=CDP_CALL_TIMEOUT_S)
            except Exception:
                pass
    except Exception:
        return None


async def silent_open_tab() -> str:
    data = await asyncio.to_thread(http_call, "PUT", f"{CDP_HTTP}/json/new?about:blank")
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError(f"json/new failed: {data!r}")
    return data["id"]


async def silent_close_tab(tab_id: str | None, reason: str = "") -> bool:
    """Close by CDP target id. Retry once. Log failures — never silent-fail."""
    if not tab_id:
        return True
    if reason:
        print(f"  close tab {tab_id[:8]}… reason={reason}", flush=True)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            await asyncio.wait_for(
                asyncio.to_thread(http_call, "GET", f"{CDP_HTTP}/json/close/{tab_id}"),
                timeout=CLOSE_TIMEOUT_S,
            )
            return True
        except Exception as e:
            msg = str(e)
            # already closed by close_extra_pages / Playwright — not a leak
            if "404" in msg or "Not Found" in msg:
                print(f"  close tab {tab_id[:8]}… already gone (ok)", flush=True)
                return True
            last_err = e
            print(f"  ! close tab failed attempt={attempt + 1}: {e}", flush=True)
            await asyncio.sleep(0.3)
    print(f"  ! close tab GIVE UP {tab_id[:8]}… err={last_err}", flush=True)
    return False


async def find_temp_page(browser, tab_id: str, pre_page_ids: set[int] | None = None):
    """
    Attach ONLY to the silent tab opened via /json/new.
    Match CDP target id == tab_id. Never fall back to last page / arbitrary blank.
    """
    pre = pre_page_ids or set()
    for _ in range(15):
        try:
            pages = [p for ctx in browser.contexts for p in ctx.pages]
        except Exception:
            pages = []

        # 1) exact CDP target id match
        for p in pages:
            tid = await page_cdp_target_id(p)
            if tid and tid == tab_id:
                try:
                    await p.evaluate(
                        """(id) => { window.__hermes_silent_tab = id; return true; }""",
                        tab_id,
                    )
                except Exception:
                    pass
                return p

        # Never bind an unproven about:blank newcomer. Concurrent user/agent
        # tabs are not ours even when our target still exists in /json/list.
        await asyncio.sleep(0.2)
    return None


async def close_extra_pages(
    browser,
    pre_page_ids: set[int],
    keep_page=None,
    *,
    owned_root_target_id: str | None = None,
) -> int:
    """Close only pages owned by this site run.

    When ``owned_root_target_id`` is supplied (production path), ownership is
    proven by the CDP ``openerId`` chain rooted at our `/json/new` target.  The
    legacy Python-object delta remains only for compatibility tests/callers.
    """
    closed = 0
    owned_ids: set[str] | None = None
    if owned_root_target_id:
        try:
            targets = await asyncio.to_thread(cdp_target_list)
            opener_by_id = {
                str(t.get("id")): str(t.get("openerId"))
                for t in targets
                if isinstance(t, dict) and t.get("id") and t.get("openerId")
            }
            owned_ids = set()
            changed = True
            while changed:
                changed = False
                for target_id, opener_id in opener_by_id.items():
                    if target_id in owned_ids:
                        continue
                    if opener_id == owned_root_target_id or opener_id in owned_ids:
                        owned_ids.add(target_id)
                        changed = True
        except Exception as e:
            print(f"  ! target ownership lookup failed: {e}", flush=True)
            owned_ids = set()  # safe failure: close nothing unknown
    try:
        pages = [p for ctx in browser.contexts for p in ctx.pages]
    except Exception:
        return 0
    for p in pages:
        if owned_ids is not None:
            target_id = await page_cdp_target_id(p)
            if not target_id or target_id not in owned_ids:
                continue
        elif id(p) in pre_page_ids:
            continue
        if keep_page is not None and p is keep_page:
            continue
        try:
            await asyncio.wait_for(p.close(), timeout=CLOSE_TIMEOUT_S)
            closed += 1
        except Exception as e:
            print(f"  ! close extra page failed: {e}", flush=True)
    if closed:
        print(f"  closed extra pages: {closed}", flush=True)
    return closed



# ---------------------------------------------------------------------------
# CDP discovery — reuse existing Chrome, prefer newest headed instance
# ---------------------------------------------------------------------------

_DEFAULT_CDP_PORTS = (
    9222, 9223, 9224, 9225, 9226, 9227, 9228, 9229, 9230,
    9333, 9334, 9444, 19222,
)


def _parse_ps_lstart(tokens_after_pid: list[str]) -> float | None:
    """Best-effort parse of `ps lstart` → epoch seconds. Returns None on failure.

    Expects the English / C-locale form: "Sat Jul 25 02:14:02 2026" (5 tokens).
    `ps -o lstart` is locale-sensitive: under e.g. zh_CN it emits Chinese
    day/month ("一  8月/24 00:25:51 2026"), and datetime.strptime's %a/%b/%c are
    locale-dependent, so parsing is done with calendar.month_name to stay
    independent of the process's environment.
    """
    if len(tokens_after_pid) < 5:
        return None
    raw = " ".join(tokens_after_pid[:5])
    # "Sat Jul 25 02:14:02 2026" regex (7 words in day month day time year).
    m = re.match(r"([A-Za-z]{3}) ([A-Za-z]{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})", raw)
    if not m:
        return None
    _, mon_name, day, hh, mm, ss, year = m.groups()
    month = {calendar.month_abbr[i]: i for i in range(1, 13)}.get(mon_name.capitalize())
    if month is None:
        return None
    try:
        return datetime(
            int(year), month, int(day), int(hh), int(mm), int(ss)
        ).timestamp()
    except ValueError:
        return None


def _ps_chrome_debug_candidates() -> list[dict[str, Any]]:
    """Parse local process list for Chrome --remote-debugging-port=N."""
    import subprocess

    out: list[dict[str, Any]] = []
    try:
        # pid, lstart (multi-token), command — use wide format then regex.
        # Force LC_ALL=C so `ps lstart` emits English day/month names regardless
        # of the process locale (zh_CN under macOS emits "8月/24"); the parser
        # then always sees the C-locale form.
        env_c = dict(os.environ)
        env_c["LC_ALL"] = "C"
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,lstart=,command="],
            text=True,
            errors="replace",
            env=env_c,
        )
    except Exception:
        return out
    for ln in raw.splitlines():
        if "remote-debugging-port=" not in ln:
            continue
        if "Helper" in ln or "crashpad" in ln or "--type=" in ln:
            continue
        low = ln.lower()
        if (
            "google chrome" not in low
            and "chromium" not in low
            and "/chrome" not in low
            and "chrome" not in low
        ):
            continue
        mport = re.search(r"--remote-debugging-port=(\d+)", ln)
        if not mport:
            continue
        port = int(mport.group(1))
        mpid = re.match(r"\s*(\d+)\s+(.*)$", ln)
        if not mpid:
            continue
        pid = int(mpid.group(1))
        rest = mpid.group(2).strip()
        # rest starts with lstart then command; split conservatively
        parts = rest.split()
        started = _parse_ps_lstart(parts)
        headless = bool(re.search(r"--headless(=new)?\b", ln)) or "headless=new" in ln
        ud = ""
        mud = re.search(r"--user-data-dir=(\S+)", ln)
        if mud:
            ud = mud.group(1)
        score = 0.0
        if not headless:
            score += 100
        if "chrome-checkin-profile" in ud:
            score += 500
        elif "chrome-secondary-profile" in ud:
            score += 50
        # temp automation profiles (DrissionPage / OS temp userData)
        if "DrissionPage" in ud or "userData/9222" in ud:
            score -= 80
        elif re.search(r"/T/DrissionPage|/var/folders/.*/T/.*Chrome", ud):
            score -= 80
        out.append(
            {
                "pid": pid,
                "port": port,
                "headless": headless,
                "user_data_dir": ud,
                "started": started,  # epoch or None
                "score": score,
                "cmd": ln.strip()[:240],
            }
        )
    return out


def _normalize_cdp_http(http: str) -> str:
    value = (http or "").strip()
    if value.isdigit():
        value = f"http://127.0.0.1:{value}"
    elif value and not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return value.rstrip("/")


def _probe_cdp_http(http: str, timeout: float = 0.8) -> dict[str, Any] | None:
    base = _normalize_cdp_http(http)
    parsed = urlparse(base)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        req = urllib.request.Request(f"{base}/json/version", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("Browser"):
        return None
    ua = str(data.get("User-Agent") or "")
    browser = str(data.get("Browser") or "")
    headless = "HeadlessChrome" in ua or "Headless" in browser
    pages = 0
    try:
        req2 = urllib.request.Request(f"{base}/json/list", method="GET")
        with urllib.request.urlopen(req2, timeout=timeout) as resp2:
            lst = json.loads(resp2.read().decode("utf-8", errors="replace"))
        if isinstance(lst, list):
            pages = sum(1 for x in lst if isinstance(x, dict) and x.get("type") == "page")
    except Exception:
        pages = 0
    return {
        "port": port,
        "http": base,
        "browser": browser,
        "ua": ua[:160],
        "headless": headless,
        "pages": pages,
        "ws": data.get("webSocketDebuggerUrl") or "",
    }


def _probe_cdp(port: int, timeout: float = 0.8) -> dict[str, Any] | None:
    return _probe_cdp_http(f"http://127.0.0.1:{port}", timeout=timeout)


def discover_cdp_endpoint(
    prefer_port: int | None = None,
    prefer_http: str | None = None,
    allow_headless: bool = False,
    strict_prefer: bool = False,
) -> dict[str, Any]:
    """
    Find best existing Chrome CDP endpoint. Never launches a browser.

    Preference:
      1) env CDP_HTTP / CDP_PORT (must be reachable)
      2) prefer_http / prefer_port if given (strict_prefer → fail if down)
      3) live endpoints ranked: headed > dedicated check-in profile > more pages > newer start time
    Default: refuse pure-headless-only (exit path for caller) unless allow_headless.
    """
    def accept_explicit(info: dict[str, Any], source: str, score: int) -> dict[str, Any]:
        if info.get("headless") and not allow_headless:
            raise RuntimeError(
                f"{source} points to headless Chrome; pass --allow-headless explicitly"
            )
        info["reason"] = source
        info["score"] = score
        return info

    # An explicit CLI URL/port outranks inherited environment settings.
    if prefer_http:
        info = _probe_cdp_http(prefer_http)
        if info:
            return accept_explicit(info, "prefer_http", 900)
        if strict_prefer:
            raise RuntimeError(f"prefer_http={prefer_http} not reachable (strict)")

    if prefer_port is not None:
        info = _probe_cdp(int(prefer_port))
        if info:
            return accept_explicit(info, "prefer_port", 900)
        if strict_prefer:
            raise RuntimeError(f"prefer_port={prefer_port} not reachable (strict)")

    env_http = (os.environ.get("CDP_HTTP") or "").strip()
    env_port = (os.environ.get("CDP_PORT") or "").strip()
    if env_http:
        if env_http.isdigit():
            env_http = f"http://127.0.0.1:{env_http}"
        if not env_http.startswith("http"):
            env_http = f"http://{env_http}"
        info = _probe_cdp_http(env_http)
        if info:
            return accept_explicit(info, "env_CDP_HTTP", 1000)
        raise RuntimeError(f"CDP_HTTP={env_http} not reachable")
    if env_port:
        port = int(env_port)
        info = _probe_cdp(port)
        if info:
            return accept_explicit(info, "env_CDP_PORT", 1000)
        raise RuntimeError(f"CDP_PORT={port} not reachable")


    proc = _ps_chrome_debug_candidates()
    ports: list[int] = []
    for c in proc:
        ports.append(int(c["port"]))
    for p in _DEFAULT_CDP_PORTS:
        if p not in ports:
            ports.append(p)
    try:
        import subprocess

        raw = subprocess.check_output(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            text=True,
            errors="replace",
        )
        for ln in raw.splitlines():
            if "Google" not in ln and "Chrome" not in ln and "Chromium" not in ln:
                continue
            m = re.search(r":(\d+)\s+\(LISTEN\)", ln)
            if m:
                ports.append(int(m.group(1)))
    except Exception:
        pass

    seen: set[int] = set()
    uniq_ports: list[int] = []
    for p in ports:
        if p in seen:
            continue
        seen.add(p)
        uniq_ports.append(p)

    candidates: list[dict[str, Any]] = []
    # last proc wins per port but prefer higher started
    proc_by_port: dict[int, dict[str, Any]] = {}
    for c in proc:
        prev = proc_by_port.get(int(c["port"]))
        if not prev:
            proc_by_port[int(c["port"])] = c
            continue
        ps, cs = prev.get("started"), c.get("started")
        if cs is not None and (ps is None or cs >= ps):
            proc_by_port[int(c["port"])] = c

    for port in uniq_ports:
        info = _probe_cdp(port)
        if not info:
            continue
        score = 0.0
        if not info["headless"]:
            score += 100
        else:
            score -= 50
        score += min(int(info.get("pages") or 0), 30)
        pc = proc_by_port.get(port)
        started = None
        if pc:
            score += float(pc.get("score") or 0)
            # avoid double-counting headed +100 from proc score base
            # proc score already includes headed/dedicated-profile signals — OK as profile signal
            info["pid"] = pc.get("pid")
            info["user_data_dir"] = pc.get("user_data_dir")
            info["proc_headless"] = pc.get("headless")
            started = pc.get("started")
            info["started"] = started
        # newer start time → higher (primary "latest chrome" signal)
        if isinstance(started, (int, float)):
            # normalize: recent epoch contributes up to ~30 points within ~1y scale noise
            score += (float(started) % 1_000_000_000) / 1e8
        else:
            # weak tie-break only
            score += (int(info.get("pid") or 0) % 1_000_000) / 1e9
        info["score"] = score
        info["reason"] = "discovered"
        candidates.append(info)

    if not candidates:
        raise RuntimeError(
            "No live Chrome CDP found. Open remote debugging on an existing headed "
            "Chrome (any port). Do not launch a new browser from this script."
        )

    # Rank: headed first. Among headed, prefer the NEWEST running Chrome
    # (process start time) over raw pages-count, so a freshly opened headed
    # Chrome wins against an older Chrome bloated with many tabs.
    def _rank_key(c: dict) -> tuple:
        headed = 0 if c.get("headless") else 1
        profile = str(c.get("user_data_dir") or "")
        checkin_profile = 1 if "chrome-checkin-profile" in profile else 0
        started = c.get("started")
        started_score = float(started) if isinstance(started, (int, float)) else 0.0
        score = float(c.get("score") or 0)
        return (headed, checkin_profile, started_score, score)

    candidates.sort(key=_rank_key, reverse=True)
    headed = [c for c in candidates if not c.get("headless")]
    if not allow_headless:
        if not headed:
            ports_h = ", ".join(str(c["port"]) for c in candidates[:6])
            raise RuntimeError(
                f"Only headless CDP found (ports: {ports_h}). "
                "Refuse pure headless/DrissionPage for check-in; "
                "start headed Chrome with remote debugging, or pass allow_headless."
            )
        best = headed[0]
    else:
        best = headed[0] if headed else candidates[0]
        if best.get("headless"):
            print(
                f"WARN: using headless CDP :{best['port']} (allow_headless=True)",
                flush=True,
            )

    best = dict(best)
    best["all"] = [
        {
            "port": c["port"],
            "headless": c.get("headless"),
            "pages": c.get("pages"),
            "score": round(float(c.get("score") or 0), 4),
            "pid": c.get("pid"),
            "started": c.get("started"),
        }
        for c in candidates[:8]
    ]
    return best


def set_cdp_http(http: str) -> str:
    """Update module-level CDP_HTTP used by silent tabs / connect."""
    global CDP_HTTP
    http = (http or "").strip()
    if http.isdigit():
        http = f"http://127.0.0.1:{http}"
    if http and not http.startswith("http"):
        http = f"http://{http}"
    CDP_HTTP = http.rstrip("/")
    return CDP_HTTP


async def safe_connect(p) -> Any:
    """connect_over_cdp with bounded retries. A transient handshake hiccup
    (slow page table, race with another CDP client) must not abort the batch."""
    last_err: Exception | None = None
    for attempt in range(max(1, CONNECT_RETRIES)):
        try:
            return await asyncio.wait_for(
                p.chromium.connect_over_cdp(CDP_HTTP, timeout=10000),
                timeout=CONNECT_TIMEOUT_S,
            )
        except Exception as e:
            last_err = e
            if attempt + 1 < max(1, CONNECT_RETRIES):
                backoff = CONNECT_RETRY_BACKOFF_S * (attempt + 1)
                print(f"  ! CDP connect retry {attempt + 1}: {e} (wait {backoff:.1f}s)", flush=True)
                await asyncio.sleep(backoff)
    raise RuntimeError(f"CDP connect failed after {CONNECT_RETRIES} tries: {last_err}")


async def browser_alive(browser) -> bool:
    """Probe both the Playwright object and the selected HTTP CDP endpoint."""
    if browser is None:
        return False
    try:
        object_alive, version = await asyncio.gather(
            asyncio.wait_for(
                asyncio.to_thread(lambda: bool(browser.contexts)),
                timeout=4.0,
            ),
            asyncio.wait_for(
                asyncio.to_thread(
                    http_call,
                    "GET",
                    f"{CDP_HTTP}/json/version",
                    3.0,
                ),
                timeout=4.0,
            ),
        )
        return bool(
            object_alive
            and isinstance(version, dict)
            and version.get("webSocketDebuggerUrl")
        )
    except Exception:
        return False


async def ensure_connected(p, browser, endpoint: dict) -> tuple[Any, bool]:
    """Reconnect if the CDP browser dropped mid-batch (Chrome restarted / WS reset).
    Returns (browser, reconnected). Never launches a new browser — reuses endpoint."""
    if browser is not None and await browser_alive(browser):
        return browser, False
    http = (endpoint or {}).get("http", CDP_HTTP)
    if http:
        set_cdp_http(http)
    print("  ! browser dropped mid-batch — reconnecting CDP", flush=True)
    await safe_disconnect(browser)
    return await safe_connect(p), True


async def safe_disconnect(browser):
    """Release only this runner's local CDP client state.

    ``connect_over_cdp`` attaches to a user-owned Chrome process. Calling
    ``Browser.close()`` on that remote object is an ownership boundary
    violation: it may close the attached browser instead of merely dropping
    this client's transport. The surrounding ``async_playwright`` context
    tears down the local Playwright driver; the runner itself closes only the
    target IDs it created through ``/json/new``.
    """
    # Remote CDP attachments deliberately receive no browser lifecycle call.
    return None


# ---------------------------------------------------------------------------
# Page utils
# ---------------------------------------------------------------------------

async def wait_text_ready(page, min_len: int = 40, rounds: int = 15):
    for _ in range(rounds):
        await asyncio.sleep(1)
        try:
            n = await page.evaluate("document.body ? document.body.innerText.length : 0")
            if n > min_len:
                return True
        except Exception:
            continue
    return False


def merge_page_text(body: str, toast_bits: list[str] | None, n: int = 1200) -> str:
    """
    Prefer toast (high-signal confirm) over body when truncating.
    Pure helper — unit-tested without Playwright.
    """
    n = int(n) if n else 1200
    body = body or ""
    toasts = [t for t in (toast_bits or []) if t]
    if not toasts:
        return body[:n]
    extra = "\n".join(toasts)
    if len(extra) >= n:
        return extra[:n]
    # toast first, then as much body as fits
    remain = n - len(extra) - (1 if body else 0)
    if body and remain > 0:
        return (extra + "\n" + body[:remain])[:n]
    return extra[:n]


async def page_text(page, n: int = 1200) -> str:
    """Body + toast. Confirm gate depends on this. Toast is never truncated away."""
    n = int(n) if n else 1200
    body = ""
    try:
        body = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
        )
        if not isinstance(body, str):
            body = ""
    except Exception as e:
        print(f"  ! page_text body: {e}", flush=True)
        body = ""

    toast_bits: list[str] = []
    for sel in (
        "[data-sonner-toast]",
        "[data-sonner-toaster]",
        "[role='status']",
        "[role='alert']",
        ".n-message",
        ".ant-message",
        ".semi-toast",
        ".Toastify",
    ):
        try:
            loc = page.locator(sel)
            cnt = await loc.count()
            for i in range(min(cnt, 8)):
                try:
                    tx = (await loc.nth(i).inner_text(timeout=300) or "").strip()
                except Exception:
                    continue
                if tx:
                    toast_bits.append(tx)
        except Exception:
            continue

    return merge_page_text(body, toast_bits, n)

async def page_url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def is_cloudflare(url: str, text: str) -> bool:
    u = (url or "").lower()
    t = (text or "").lower()
    cf_hosted = "cloudflare" in u or "challenges.cloudflare.com" in u
    challenge_text = (
        "cf-challenge" in t
        or "checking your browser" in t
        or "just a moment" in t
        or "verify you are human" in t
        or "请完成安全验证" in (text or "")
        # 安全验证 is only a CF signal when Cloudflare is named too; real
        # Turnstile interstitials are caught by the challenges.cloudflare.com
        # URL branch, so a bare "turnstile" text mention (a site footer like
        # verydio's "Turnstile 已启用") deliberately does NOT count as blocked.
        or ("安全验证" in (text or "") and "cloudflare" in t)
    )
    return cf_hosted or challenge_text


def has_interactive_captcha(text: str, url: str = "") -> bool:
    """hCaptcha / reCAPTCHA / turnstile challenge text signals."""
    t = text or ""
    u = (url or "").lower()
    if "请先进行验证" in t or "请完成验证" in t or "人机验证" in t:
        return True
    if "安全验证" in t and "刷新验证码" in t:
        return True
    if "hcaptcha" in u or "recaptcha" in u:
        return True
    if "h-captcha" in t.lower() or "hcaptcha" in t.lower():
        return True
    return False


async def captcha_dom_present(page) -> bool:
    """DOM-level captcha widgets (iframe / widget root).

    Turnstile's challenge frame URL is loaded internally, so the iframe's DOM
    ``src`` attribute does NOT contain "turnstile"/"challenges.cloudflare".
    CSS attribute selectors therefore miss it.  Match frames by their live URL
    instead, then fall back to CSS for classic hcaptcha/recaptcha roots.
    """
    try:
        for frame in page.frames:
            fu = (frame.url or "").lower()
            if any(
                k in fu
                for k in (
                    "challenges.cloudflare",
                    "challenge-platform",
                    "/turnstile/",
                    "hcaptcha",
                    "recaptcha",
                )
            ):
                return True
    except Exception:
        pass
    try:
        if await page.locator(
            "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='turnstile'], "
            "iframe[src*='challenges.cloudflare'], .h-captcha, [class*='h-captcha'], "
            "[class*='cf-turnstile'], #cf-turnstile, [data-sitekey], "
            # in-page captcha modals (随想 dash-turnstile-modal; Tencent turing popup)
            "[class*='turnstile-modal'], .captcha-card, [class*='captcha-card'], "
            "button:has-text('点击完成安全验证'), button:has-text('完成安全验证')"
        ).count() > 0:
            return True
    except Exception:
        pass
    return False


async def interactive_captcha_dom_present(page) -> bool:
    """True for captcha UI that needs a user gesture, not passive Turnstile."""
    try:
        for frame in page.frames:
            frame_url = (frame.url or "").lower()
            if "hcaptcha" in frame_url or "recaptcha" in frame_url:
                return True
    except Exception:
        pass
    try:
        return await page.locator(
            "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], "
            ".h-captcha, [class*='h-captcha'], "
            "[class*='turnstile-modal'], .captcha-card, [class*='captcha-card'], "
            ".pow-captcha, "
            "[role='dialog']:has-text('安全验证'):has-text('刷新验证码'), "
            ".semi-modal:has-text('安全验证'):has-text('刷新验证码'), "
            "button:has-text('点击完成安全验证'), button:has-text('完成安全验证')"
        ).count() > 0
    except Exception:
        return False


async def sign_button_disabled(page) -> bool:
    for sel in (
        'button:has-text("签到领取奖励")',
        'button:has-text("立即签到")',
        'button:has-text("签到")',
        'button:has-text("领取")',
        'button:has-text("Check in")',
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            if not await loc.is_visible(timeout=200):
                continue
            if await loc.is_disabled():
                return True
        except Exception:
            continue
    return False


async def captcha_blocking(page) -> bool:
    """True when captcha still blocks check-in (prompt / widget + disabled CTA)."""
    try:
        url = await page_url(page)
        text = await page_text(page, 900)
    except Exception:
        return False
    if has_interactive_captcha(text, url):
        return True
    if await interactive_captcha_dom_present(page):
        return True
    if await captcha_dom_present(page):
        if await sign_button_disabled(page):
            return True
        # A standalone Turnstile token widget is passive. Let the CTA flow's
        # wait_turnstile_token() poll it instead of entering the human gate.
        return False
    return False


async def try_click_challenge_widgets(page) -> bool:
    """
    Best-effort click CF Turnstile / common checkbox challenges.
    Cannot solve image hCaptcha — only checkbox-style when reachable.
    """
    clicked = False
    for sel in (
        '.pow-icon',
        'input[type="checkbox"]',
        'label:has-text("确认您是真人")',
        'label:has-text("Verify you are human")',
        'text=确认您是真人',
        'text=Verify you are human',
        '.cf-turnstile',
        '#cf-turnstile',
        '[data-sitekey]',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=250):
                await loc.click(timeout=1500, force=True)
                clicked = True
                await asyncio.sleep(0.4)
        except Exception:
            continue
    try:
        for frame in page.frames:
            fu = (frame.url or "").lower()
            interesting = any(
                k in fu for k in ("turnstile", "cloudflare", "hcaptcha", "recaptcha", "challenges")
            )
            if frame == page.main_frame and not interesting:
                continue
            if not interesting and frame != page.main_frame:
                # still try child frames with empty/about urls once
                if fu and not any(k in fu for k in ("about:", "blank")):
                    if "captcha" not in fu and "challenge" not in fu:
                        continue
            for sel in (
                'input[type="checkbox"]',
                '#checkbox',
                '.ctp-checkbox-label',
                'label.ctp-checkbox-label',
                '[role="checkbox"]',
            ):
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    if await loc.is_visible(timeout=200):
                        await loc.click(timeout=1500, force=True)
                        clicked = True
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    continue
            # click center of challenge frame as last resort
            if interesting:
                try:
                    box = await frame.locator("body").bounding_box()
                    if box and box.get("width", 0) > 10:
                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 40))
                        clicked = True
                        await asyncio.sleep(0.4)
                except Exception:
                    pass
    except Exception:
        pass
    return clicked


async def has_cf_clearance(page) -> bool:
    """True once Cloudflare has minted cf_clearance (Turnstile passed)."""
    try:
        current_url = await page_url(page)
        cookies = await page.context.cookies([current_url]) if current_url else []
        return any(
            (c.get("name") or "").lower() == "cf_clearance" for c in cookies
        )
    except Exception:
        return False


async def _cf_gate_cleared(page) -> bool:
    """True only when a Cloudflare/Turnstile gate is live AND cf_clearance exists.

    Shared headed Chrome accumulates a stale cf_clearance cookie from other hosts;
    treat it as "cleared" only while the current page actually has a CF/Turnstile
    frame (otherwise hCaptcha/Tencent modals would be misjudged as already-clear).
    """
    try:
        if not await has_cf_clearance(page):
            return False
        for frame in page.frames:
            fu = (frame.url or "").lower()
            if any(k in fu for k in ("cloudflare", "turnstile", "challenge")):
                return True
    except Exception:
        pass
    return False


async def wait_out_captcha(page, max_s: float = CAPTCHA_WAIT_S) -> str | None:
    """
    Wait for human (or auto-pass) captcha on the selected headed Chrome CDP.
    Poll until captcha no longer blocks OR success text appears.
    Return 'INTERACTIVE' if still blocked after max_s, else None.
    """
    if not await captcha_blocking(page):
        return None
    started = time.monotonic()
    print(
        f"  captcha/人机 — 请在 Chrome({CDP_HTTP}) 完成验证，最多等 {max_s:.0f}s…",
        flush=True,
    )
    last_click = 0.0
    last_log = -10.0
    while time.monotonic() - started < max_s:
        elapsed = time.monotonic() - started
        if elapsed - last_click >= 6.0:
            if await try_click_challenge_widgets(page):
                print(f"  captcha widget click @{elapsed:.0f}s", flush=True)
            last_click = elapsed
        if not await captcha_blocking(page) or await _cf_gate_cleared(page):
            print(f"  captcha cleared after {elapsed:.0f}s", flush=True)
            return None
        try:
            text = await page_text(page, 900)
            # same strict gate as mark path — never bare 已领取/额度
            if is_valid_checkin_confirm(text):
                print("  captcha wait: success/already text", flush=True)
                return None
        except Exception:
            pass
        if elapsed - last_log >= 10.0:
            print(f"  waiting captcha… {elapsed:.0f}s", flush=True)
            last_log = elapsed
        await asyncio.sleep(1.2)
    if await captcha_blocking(page):
        return "INTERACTIVE"
    return None


def looks_logged_out(text: str) -> bool:
    """Strong signals only. Mere nav word '登录' is NOT enough (薄荷站)."""
    if not text:
        return False
    if any(k in text for k in ("退出登录", "登出", "退出", "Logout", "Sign out")):
        return False
    if any(
        k in text
        for k in (
            "使用 LinuxDO 继续",
            "使用 Linux DO",
            "使用 Linux Do",
            "使用 Linux.do",
            "Continue with Linux",
            "Linux DO 登录",
            "LinuxDO 登录",
            "Linux.do 登录",
        )
    ):
        return True
    if re.search(r"使用\s*Linux\.?\s*do\s*登录", text, re.I):
        return True
    if ("用户名或电子邮件" in text and "密码" in text) or ("Forgot password" in text):
        return True
    if re.search(
        r"(请先登录|需要登录|登录后继续|登录您的账户|登录您的帐户|"
        r"立即登录|Sign in to|Please log in|Please sign in)",
        text,
    ):
        return True
    # "欢迎回来" alone is a logged-in dashboard greeting (suixiang/wxiai);
    # only counts as a login page when credentials are also present (47).
    if "欢迎回来" in text and re.search(r"邮箱|密码|忘记密码|用户名|手机号", text):
        return True
    if re.search(r"(^|\n)\s*登录\s*(\n|$)" , text) and "密码" in text:
        return True
    return False


def is_auth_page_url(url: str) -> bool:
    """A login route is a hard auth gate, not a missing CTA."""
    try:
        path = urlparse(url or "").path.lower().rstrip("/")
    except Exception:
        return False
    return path in {"/login", "/signin", "/sign-in", "/auth/login"}


def classify_page_block(text: str, url: str = "") -> tuple[str, str] | None:
    """Classify deterministic external blockers before generic CTA scanning."""
    page_text = (text or "").strip().lower()
    try:
        path = urlparse(url or "").path.lower().rstrip("/")
    except Exception:
        path = ""
    blob = f"{url}\n{text}".lower()
    if (
        re.search(r"^\s*(?:error\s*)?404(?:\s|$)", page_text)
        or re.search(r"\b404\s+(?:not found|page not found)\b", page_text)
        or re.search(r"page not found|页面未找到|找不到页面", page_text)
        or path == "/404"
    ):
        return "dead_url", (text or "404 page")[:120]
    if (
        re.search(r"^\s*(?:error\s*)?403(?:\s|$)", page_text)
        or re.search(
            r"forbidden|access denied|被禁止访问|地区限制|"
            r"sorry,? you have been blocked|you are unable to access",
            blob,
        )
    ):
        return "blocked", (text or "access blocked")[:120]
    if re.search(
        r"error code\s*(?:50[234]|52[124])|bad gateway|service unavailable|"
        r"gateway timeout|connection timed out|web server is down",
        blob,
    ):
        return "upstream_unavailable", (text or "upstream unavailable")[:120]
    # "签到阈值 $20" on a card reading 当前余额低于阈值,可以签到 is an
    # ALLOWING cue (the site explicitly says sign-up is possible) — do not
    # classify as ineligible just because the word 阈值 appears. Real
    # rejections use 无法签到/余额不足/资格不足 or 消耗满 wording.
    if "可以签到" in page_text:
        return None
    # Sites like mulink (demo.dev2.mulink.top) may render "余额不满足领取条件"
    # as subtitle/description on the quota-pool card while the check-in button
    # itself remains active/enabled and successfully grants daily quota upon clicking.
    # Do not fail-fast reject mulink on this cue; let CTA search proceed.
    if "mulink" in blob or "dev2.mulink" in blob:
        return None
    if re.search(
        r"(?:永久余额.{0,40}(?:实际消耗|需消耗|消耗满)|"
        r"当天.{0,40}(?:实际消耗|需消耗|消耗满)|"
        r"余额不满足领取条件|"
        r"签到.*阈值|余额.*阈值|资格不足|无法签到|"
        r"\d{1,2}:\d{2}\s*(?:开放|开始)签到)",
        blob,
    ):
        return "business_ineligible", (text or "business eligibility unmet")[:160]
    return None


def classify_navigation_error(message: str, page_url_value: str = "") -> str:
    """Classify deterministic transport failures raised before a page renders."""
    if re.search(
        r"ERR_(?:CONNECTION_CLOSED|CONNECTION_REFUSED|CONNECTION_RESET|"
        r"CONNECTION_TIMED_OUT|TIMED_OUT|ADDRESS_UNREACHABLE|NAME_NOT_RESOLVED)",
        message or "",
        re.I,
    ):
        return "upstream_unavailable"
    if "Page.goto: Timeout" in (message or ""):
        return "upstream_unavailable"
    return "error"


def classify_missing_checkin_feature(
    text: str, url: str, adapter_kind: str, explicit_reason: str = "",
) -> tuple[str, str] | None:
    """Identify a fully rendered New-API profile with no check-in feature."""
    if explicit_reason:
        return "feature_unavailable", explicit_reason
    if adapter_kind != "newapi_profile":
        return None
    try:
        path = urlparse(url or "").path.lower().rstrip("/")
    except Exception:
        return None
    blob = re.sub(r"\s+", " ", text or "").strip()
    if path not in {"/profile", "/console/personal"}:
        return None
    if re.search(r"签到|打卡|check[- ]?in|领取.*奖励", blob, re.I):
        return None
    profile_signals = sum(
        token in blob
        for token in ("当前余额", "API 密钥", "使用日志", "账户绑定", "个人资料", "钱包")
    )
    if profile_signals < 4 or not re.search(r"用户\s*(?:ID|用户 ID)|设置", blob):
        return None
    return "feature_unavailable", "loaded profile has no check-in feature"


async def is_auth_page(page, text: str = "") -> bool:
    """Detect login redirects before the generic CTA scanner runs."""
    try:
        url = await page_url(page)
    except Exception:
        url = ""
    return is_auth_page_url(url) or looks_logged_out(text)


def is_valid_checkin_confirm(text: str) -> bool:
    """True only with explicit check-in feedback (not calendar history alone).

    Redline: bare 「签到」 CTA text is NOT success. 「今天 +¥」 alone is NOT
    success either — must pair with done-state (已签到/已领取/…), never with
    the mere word 「签到」 (every New-API page has 每日签到/立即签到).
    """
    t = text or ""
    if not t:
        return False
    for pat in STRONG_SUCCESS_PATTERNS:
        if re.search(pat, t, re.I):
            return True
    # 7x-style after check-in: 「已签到」+「今天 +¥20」(may be multi-line)
    # MUST require 已签到/已领取/今日已签… — never bare 「签到」 or short 「已签」
    # (「已签」 matches 已签名 etc.; false positive on non-checkin copy).
    money_today = re.search(r"今天\s*\+\s*[¥￥$]?\s*[\d.]+", t)
    money_jr = re.search(r"今日\s*\+\s*[¥￥$]?\s*[\d.]+", t)
    done_for_money = bool(
        re.search(r"已签到|已领取|今日已签|今天已签", t)
    )
    if money_today and done_for_money:
        return True
    if money_jr and done_for_money:
        return True
    # weak token + today context (allow newlines between)
    if any(k in t for k in WEAK_NEED_TODAY):
        if re.search(r"今日|今天|today", t, re.I):
            if re.search(r"今日已签|今天已签|今日签到成功|今天签到成功|今日已领取", t):
                return True
            # same-line nearby
            if re.search(r"今日[^\n]{0,20}(已签到|已领取)|今天[^\n]{0,20}(已签到|已领取)", t):
                return True
            if re.search(r"(已签到|已领取)[^\n]{0,20}(今日|今天)", t):
                return True
            # multi-line: 已签到 ... 今天 (within short window). 同月统计句
            # 「已签到 N 天」绝不与下方的「今天(还)未签到」跨行拼成假已签到
            # (yeelo 2026-08-26: 已签到 4 天 + 今天还未签到 → 被误判 today+weak,
            # 实际按钮没点)。若「已签到」后紧跟 N 天/N 次统计,禁止跨行拼「今天」。
            if re.search(
                r"已签到(?!\s*\d+\s*(?:天|次))(?:[\s\S]{0,40}?(?:今天|今日))"
                r"|(?:今天|今日)[\s\S]{0,40}?已签到(?!\s*\d+\s*(?:天|次))",
                t,
            ):
                return True
            if re.search(
                r"已领取(?!\s*\d+\s*(?:天|次|个))(?:[\s\S]{0,40}?(?:今天|今日))"
                r"|(?:今天|今日)[\s\S]{0,40}?已领取(?!\s*\d+\s*(?:天|次|个))",
                t,
            ):
                return True
    return False


def has_success_text(text: str) -> bool:
    """Back-compat alias → strict confirm."""
    return is_valid_checkin_confirm(text)


def confirm_signal(text: str) -> str:
    """Return matched confirm snippet for logs, or empty."""
    t = text or ""
    for pat in STRONG_SUCCESS_PATTERNS:
        m = re.search(pat, t, re.I)
        if m:
            return m.group(0)
    if is_valid_checkin_confirm(t):
        return "today+weak"
    return ""


def dom_confirmation(summary: str, *, done_state: bool = False) -> ConfirmationEvidence:
    return ConfirmationEvidence(
        kind="dom_done_state" if done_state else "dom_strong",
        source="dom_live",
        summary=(summary or "dom-confirm")[:160],
        observed_at=datetime.now().isoformat(timespec="seconds"),
        checked_in_today=True,
    )


def confirmed_done_result(
    detail: str,
    *,
    adapter: str,
    action: ActionEvidence | None = None,
) -> CheckinResult:
    """Build a fully attributable result for a live done-state observation."""
    post_click = action is not None and action.kind != "none"
    return ok_result(
        "OK" if post_click else "ALREADY",
        adapter=adapter,
        detail=detail,
        action=action or ActionEvidence(kind="none"),
        confirmation=dom_confirmation(detail, done_state=True),
        pre_state="PENDING" if post_click else "DONE",
        post_state="DONE",
        transition="PENDING_TO_DONE" if post_click else "NONE",
        attribution="runner" if post_click else "precheck",
    )


async def click_first_visible(page, selectors: list[str], timeout_each: int = 800) -> str | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout_each):
                await loc.click(timeout=3000, force=True)
                return sel
        except Exception:
            continue
    return None


async def fill_first_visible(
    page, selectors: list[str], value: str, timeout_each: int = 800
) -> str | None:
    """Fill `value` into the first visible input matching `selectors`. Mirrors
    click_first_visible's skeleton. Returns the matched selector or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout_each):
                await loc.fill(value, timeout=3000)
                return sel
        except Exception:
            continue
    return None


def is_sign_cta_text(text: str) -> bool:
    """Return whether a node label denotes an actionable check-in CTA.

    The generic scanner only sees text and attributes, so it must be stricter
    than site-specific selectors: bare section/tab labels such as 「每日签到」
    and 「今日」 are not sufficient actions.
    """
    t = (text or "").strip()
    if not t:
        return False
    # DONE state — never click as sign CTA (Playwright has-text("签到") matches 已签到;
    # bare "打卡" regex also matches "打卡成功"/"已打卡"). Cover both families.
    if re.fullmatch(
        r"已签到|今日已签到|今天已签到|已领取|今日已领取|"
        r"已打卡|今日已打卡|今天已打卡|打卡成功|签到成功",
        t,
    ):
        return False
    if t.startswith(("已签到", "已打卡", "打卡成功", "签到成功")) or "今天 +" in t or "今日 +" in t:
        if "立即签到" not in t and "立即打卡" not in t:
            return False
    # Metadata/navigation links are never actions, including labels prefixed
    # with action words such as「立即签到记录」or「签到领取说明」.
    if re.search(
        r"规则|说明|记录|历史|排行|帮助|教程|指南|统计|设置|提醒|开关|管理|活动|配置|通知",
        t,
    ):
        return False
    # Explicit action labels only. Bare daily/check-in section titles are
    # accepted only when a control also carries a reward/action signal.
    if t in (
        "立即签到", "签到", "今日签到", "签到领取奖励",
        "立即打卡", "今日打卡", "每日打卡", "打卡",
        "开始转动", "立即转动", "开始签到",
        "Check in", "Check-in", "Checkin", "领取 Codex 权益",
    ):
        return True
    if t.startswith(("立即签到", "签到领取", "立即打卡", "今日打卡", "每日打卡", "开始转动", "立即转动", "开始签到")):
        return True
    if re.fullmatch(
        r"(?:每日\s*)?(?:签到|打卡)\s*[+＋]\s*\d+(?:\.\d+)?(?:\s*(?:积分|点|元|GB|MB))?",
        t,
        re.I,
    ):
        return True
    if "每日签到" in t and "立即签到" not in t:
        return False
    # "今日" alone is commonly a calendar tab; only allow it with sign/clock-in copy.
    if "今日" in t and "签到" not in t and "打卡" not in t:
        return False
    if "已签" in t or "已领取" in t:
        return False

    # Some sites put the action word at the end of a decorated label
    # (lucky0625「摘一片四叶草 · 签到」). A trailing 签到/打卡 token therefore
    # also counts as an explicit action phrase — but 每日签到/如何签到 must
    # still go the way of "bare daily section label = not a CTA" above.
    if not re.match(r"^\s*(?:每日|如何|怎样|怎么|攻略)\s*签到", t) and re.search(
        r"(?:·|:|：|>|»|→|—|/|\s)\s*(?:签到|打卡)\s*$", t
    ):
        return True
    # The remaining labels need an explicit action phrase, not a bare keyword.
    return bool(
        re.search(
            r"立即\s*(签到|打卡|转动)|开始\s*(签到|转动)|今日\s*(签到|打卡)|每日\s*打卡|"
            r"去\s*签到|点击\s*签到|签到\s*(领取|奖励)|转动\s*(领取|奖励)|"
            r"(?:每日\s*)?(?:签到|打卡)\s*[+＋]\s*\d+(?:\.\d+)?|"
            r"check[- ]?in|打卡",
            t,
            re.I,
        )
    )


SIGN_METADATA_RE = re.compile(
    r"规则|说明|记录|历史|排行|帮助|教程|指南|统计|任务|明细|日志|公告|"
    r"设置|提醒|开关|管理|活动|配置|通知"
)
SIGN_ACTION_CONTEXT_RE = re.compile(r"签到|打卡|领取|转动|check[- ]?in", re.I)
SIGN_REWARD_CONTEXT_RE = re.compile(
    r"奖励|积分|余额|金币|额度|赠送|福利|\d+(?:\.\d+)?\s*(?:GB|MB|点|元)",
    re.I,
)


def score_generic_sign_candidate(candidate: dict[str, Any]) -> tuple[int, str]:
    """Score generic controls while keeping ambiguous daily copy constrained."""
    label = re.sub(r"\s+", " ", str(candidate.get("label") or "")).strip()
    context = re.sub(r"\s+", " ", str(candidate.get("context") or "")).strip()
    if not label or len(label) > 100:
        return 0, "empty_or_long"
    if SIGN_METADATA_RE.search(label):
        return 0, "metadata"
    if re.search(
        r"已签到|今日已签到|今天已签到|已领取|今日已领取|"
        r"已打卡|今日已打卡|今天已打卡|打卡成功|签到成功",
        label,
    ):
        return 0, "done"
    role = str(candidate.get("role") or "").lower()
    tag = str(candidate.get("tag") or "").lower()
    input_type = str(candidate.get("type") or "").lower()
    if (
        candidate.get("disabled")
        or role in {"tab", "menuitem", "navigation"}
    ):
        return 0, "non_action_region"
    button_like = bool(
        tag == "button"
        or tag == "a"
        or tag == "input" and input_type in {"button", "submit"}
        or role == "button"
        or candidate.get("onclick")
        or candidate.get("data_action")
        or candidate.get("pointer")
        or candidate.get("tabbable")
    )
    if candidate.get("in_nav") and not is_clear_sign_cta(label):
        return 0, "navigation"
    if is_sign_cta_text(label):
        return (320, "strong") if button_like else (0, "strong_not_button")
    normalized = re.sub(r"[：:›>»→\s]+$", "", label)
    if normalized in {"每日签到", "每日打卡", "每日领取", "今日领取"}:
        has_business_context = bool(
            SIGN_ACTION_CONTEXT_RE.search(context)
            and (SIGN_REWARD_CONTEXT_RE.search(context) or re.search(r"待签到|未签到|可签到|今日状态", context))
            and not SIGN_METADATA_RE.search(context)
        )
        if button_like and has_business_context:
            return 220, "daily_sign"
        # A real interactive control whose label is the bare daily phrase is still
        # an actionable CTA even when no usable business context survived — e.g.
        # test.mlgb7.com renders the check-in as <button class="ui-btn-primary">
        # 每日签到</button> with the points-history list flooding every ancestor
        # with metadata words. The selector phase (is_sign_cta_text) stays strict,
        # so only the generic fallback — which has full tag/role facts — trusts the
        # semantics. But require an *explicit* button (native tag, role=button, or
        # a real handler): a mere cursor:pointer div can be a styled section title,
        # so pointer/tabbable alone must NOT clear this bar. Score below
        # daily_sign(220)/fuzzy_sign(200) so a real contextual CTA always wins.
        explicit_button = bool(
            tag == "button"
            or tag == "a"
            or tag == "input" and input_type in {"button", "submit"}
            or role == "button"
            or candidate.get("onclick")
            or candidate.get("data_action")
        )
        if explicit_button:
            return 180, "daily_button"
        return 0, "daily_without_context"
    if normalized in {"开始转动", "立即转动", "开始签到"}:
        has_business_context = bool(
            SIGN_ACTION_CONTEXT_RE.search(context)
            and (SIGN_REWARD_CONTEXT_RE.search(context) or re.search(r"待签到|未签到|可签到|今日状态", context))
            and not SIGN_METADATA_RE.search(context)
        )
        return (220, "action_context") if button_like and has_business_context else (0, "action_without_context")
    if re.fullmatch(
        r"(?:领取\s*)?(?:今日|每日)\s*\d+(?:\.\d+)?\s*(?:GB|MB|积分|点|元|额度|金币)",
        normalized,
        re.I,
    ):
        return (210, "daily_reward") if button_like else (0, "reward_not_button")
    if normalized in {"每日", "今日"}:
        # Bare 「今日」/「每日」 are most often calendar tabs, not actions, so
        # require action + reward context (e.g. 「签到可领取 20 积分」) and score
        # below fuzzy_sign(200) so a real CTA always wins on a mixed page.
        if (
            button_like
            and SIGN_ACTION_CONTEXT_RE.search(context)
            and SIGN_REWARD_CONTEXT_RE.search(context)
            and not SIGN_METADATA_RE.search(context)
        ):
            return 160, "daily_context"
        return 0, "daily_without_context"
    if re.fullmatch(
        r"(?:(?:开始|去|点击|前往|马上|现在)\s*)?(?:签到|打卡)(?:\s*一下)?|check[- ]?in",
        normalized,
        re.I,
    ):
        return (200, "fuzzy_sign") if button_like else (0, "fuzzy_not_button")
    if re.fullmatch(r"(?:每日|今日)(?:福利|奖励|领取|赠送)?", normalized):
        if (
            button_like
            and SIGN_ACTION_CONTEXT_RE.search(context)
            and SIGN_REWARD_CONTEXT_RE.search(context)
            and not SIGN_METADATA_RE.search(context)
        ):
            return 150, "fuzzy_daily_context"
        return 0, "fuzzy_daily_without_context"
    return 0, "not_sign_copy"


SIGN_REWARD_NUMBER_RE = re.compile(
    r"(?:签到|每日签到|打卡|今日打卡|领取|转动|check[\s-]?in)"
    r"[^：:\d\n]{0,20}?[+＋]\s*\d+(?:\.\d+)?",
    re.I,
)


def is_clear_sign_cta(label: str) -> bool:
    """True for an unambiguous sign CTA even when it sits inside a nav region.

    ``in_nav`` guards reject most header/menu links, but a control whose label
    carries an explicit reward amount (``签到 +100~200``) is a real action, not
    navigation copy — e.g. pipiwang's ``<button#checkinBtn>签到 +100~200``,
    which lives inside ``header.topbar``. Bare nav entries (签到 / 每日签到 in a
    menu) carry no reward number, so they still fall to the navigation filter.
    """
    if not label:
        return False
    norm = re.sub(r"\s+", " ", str(label)).strip()
    if SIGN_METADATA_RE.search(norm):
        return False
    if re.search(r"已签到|今日已签到|今天已签到|已打卡|今日已打卡|已领取", norm):
        return False
    if not SIGN_REWARD_NUMBER_RE.search(norm):
        return False
    return True


async def first_visible(
    page,
    selectors: list[str],
    timeout_each: int = 800,
    *,
    sign_cta_only: bool = False,
    trusted_sign: bool = False,
    reset_scan_state: bool = True,
):
    """Return the first visible selector match.

    ``sign_cta_only`` is deliberately opt-in: LinuxDO, OAuth and the 薄荷
    spin step have their own semantics and must not be filtered as sign CTAs.

    THREAD/CONCURRENCY CONTRACT: ``first_visible._done_signal`` and
    ``first_visible._skip_logs`` are function-level scan state shared across
    calls. This is safe ONLY because the batch runs sites strictly sequentially
    (``for adapter in pending`` in run(), no ``asyncio.gather`` across sites)
    and ``reset_scan_state=True`` (default) re-initializes at each scan start.
    Do NOT call first_visible concurrently (e.g. scanning two selectors in
    parallel within one page) — the shared state would cross-contaminate.
    If concurrent scanning is ever needed, move this state into a per-scan
    object passed explicitly.
    """
    if reset_scan_state:
        first_visible._skip_logs = 0  # type: ignore[attr-defined]
        first_visible._done_signal = ""  # type: ignore[attr-defined]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            limit = min(max(n, 0), 10)
            for i in range(limit if limit else 1):
                item = loc.nth(i) if limit else loc.first
                try:
                    if not await item.is_visible(timeout=timeout_each if i == 0 else 250):
                        continue
                except Exception:
                    continue
                try:
                    label = (await item.inner_text(timeout=500) or "").strip()
                except Exception:
                    label = ""
                if sign_cta_only:
                    try:
                        disabled = bool(await item.is_disabled())
                    except Exception:
                        disabled = False
                    if trusted_sign:
                        # Hand-curated site selectors (试试手气 etc.) bypass the
                        # phrase classifier, but a done-state match must still
                        # short-circuit to ALREADY instead of being clicked.
                        dsig = done_button_signal(label, disabled)
                        if dsig:
                            if not getattr(first_visible, "_done_signal", ""):
                                first_visible._done_signal = dsig  # type: ignore[attr-defined]
                            continue
                    elif not is_sign_cta_text(label):
                        dsig = done_button_signal(label, disabled)
                        if dsig and not getattr(first_visible, "_done_signal", ""):
                            first_visible._done_signal = dsig  # type: ignore[attr-defined]
                        n = getattr(first_visible, "_skip_logs", 0)
                        if n < 1:
                            print(f"  skip non-CTA sign node: {label[:40]!r}", flush=True)
                            first_visible._skip_logs = n + 1  # type: ignore[attr-defined]
                        continue
                    try:
                        node_meta = await item.evaluate(
                            """(el) => ({
                              tag: el.tagName.toLowerCase(),
                              in_nav: !!el.closest('nav, header, [role="navigation"], '
                                + '[role="tablist"], [class*="nav"], '
                                + '[class*="menu"], [class*="tabs"]'),
                              explicit_action: !!el.onclick || el.hasAttribute('onclick')
                                || el.hasAttribute('data-action') || el.hasAttribute('data-click')
                            })"""
                        )
                    except Exception:
                        node_meta = {}
                    # Trusted hand-curated selectors bypass the nav heuristic —
                    # the site's CTA genuinely lives inside a [class*="nav"] /
                    # layout region (e.g. ddcat「签到 +0.5」in the points panel).
                    # An explicit-reward CTA (``签到 +100~200``, pipiwang) is also
                    # a real action even inside a header/menu; only bare nav
                    # links (no reward number) are rejected.
                    is_action_btn = node_meta.get("tag") == "button" and label.strip() in (
                        "今日签到", "立即签到", "立即打卡", "今日打卡", "每日打卡"
                    )
                    if (
                        not trusted_sign
                        and node_meta.get("in_nav")
                        and not is_clear_sign_cta(label)
                        and not is_action_btn
                    ):
                        n = getattr(first_visible, "_skip_logs", 0)
                        if n < 1:
                            print(f"  skip navigation sign node: {label[:40]!r}", flush=True)
                            first_visible._skip_logs = n + 1  # type: ignore[attr-defined]
                        continue
                return item, sel
        except Exception:
            continue
    return None, None


async def generic_sign_cta(page):
    """Rank visible interactive check-in controls on nonstandard pages."""
    primary_selector = (
        "button, a, input[type='button'], input[type='submit'], [role='button'], "
        "[role='link'], [onclick], [data-action], [data-click], "
        "[tabindex]:not([tabindex='-1']), [class*='btn'], [class*='button'], "
        "[class*='clickable'], [class*='cursor-pointer'], [style*='cursor: pointer'], "
        "[style*='cursor:pointer']"
    )

    async def inspect(item):
        return await item.evaluate(
            """(el) => {
              const label = [el.innerText, el.getAttribute('aria-label'),
                el.getAttribute('title'), el.getAttribute('value'),
                el.getAttribute('data-testid')].filter(Boolean).join(' ').trim();
              let scope = el;
              let context = '';
              for (let depth = 0; scope && depth < 5; depth += 1, scope = scope.parentElement) {
                const text = (scope.innerText || '').trim().slice(0, 500);
                if (/签到|打卡|领取|奖励|积分|余额|金币|额度|赠送|福利|转动|check[- ]?in/i.test(text)) {
                  context = text;
                  break;
                }
                if (!context && text.length <= 500) context = text;
              }
              const style = getComputedStyle(el);
              let scanId = el.getAttribute('data-generic-sign-scan') || '';
              if (!scanId) {
                scanId = `generic-sign-${Date.now()}-${Math.random().toString(36).slice(2)}`;
                el.setAttribute('data-generic-sign-scan', scanId);
              }
              return {
                label, context, scan_id: scanId, tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
                onclick: !!el.onclick || el.hasAttribute('onclick'),
                data_action: !!(el.getAttribute('data-action') || el.getAttribute('data-click')),
                pointer: style.cursor === 'pointer',
                tabbable: el.hasAttribute('tabindex')
                  && Number(el.getAttribute('tabindex')) >= 0,
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                in_nav: !!el.closest('nav, header, [role="navigation"], [role="tablist"], '
                  + '[class*="nav"], [class*="menu"], [class*="tabs"]')
              };
            }"""
        )

    candidates: list[tuple[int, int, Any, str, str]] = []
    rejected: list[str] = []
    seen_handles: set[str] = set()
    order = 0

    async def collect(loc, limit: int) -> None:
        nonlocal order
        try:
            count = min(await loc.count(), limit)
        except Exception:
            return
        for i in range(count):
            item = loc.nth(i)
            try:
                if not await item.is_visible(timeout=120):
                    continue
                try:
                    if await item.is_disabled():
                        continue
                except Exception:
                    pass
                candidate = await inspect(item)
                handle = str(candidate.get("scan_id") or "")
                if handle and handle in seen_handles:
                    continue
                if handle:
                    seen_handles.add(handle)
                score, kind = score_generic_sign_candidate(candidate)
                if score <= 0:
                    label = str(candidate.get("label") or "").strip()
                    if label and re.search(r"签到|打卡|每日|今日|领取|转动|check", label, re.I):
                        rejected.append(f"{kind}:{label[:24]}")
                    continue
                label = str(candidate.get("label") or "").strip()
                candidates.append((score, -order, item, label, kind))
                order += 1
            except Exception:
                continue

    # Phase 1: native and attribute-backed controls. Static copy never consumes
    # the fallback budget, so a real control cannot be starved by page prose.
    await collect(page.locator(primary_selector), 240)

    # Phase 2: framework controls whose click handler is represented only by
    # computed cursor style. Filter in the browser before creating locators.
    try:
        custom_ids = await page.locator("div, span").evaluate_all(
            """(els) => {
              let n = 0;
              const out = [];
              for (const el of els) {
                if (out.length >= 160) break;
                const text = (el.innerText || '').trim();
                if (!text || text.length > 100
                    || !/签到|打卡|每日|今日|领取|转动|check[- ]?in/i.test(text)) continue;
                const style = getComputedStyle(el);
                const interactive = style.cursor === 'pointer' || !!el.onclick
                  || el.hasAttribute('onclick') || el.hasAttribute('data-action')
                  || el.hasAttribute('data-click')
                  || (el.hasAttribute('tabindex') && Number(el.getAttribute('tabindex')) >= 0)
                  || el.getAttribute('role') === 'button';
                if (!interactive) continue;
                const existing = el.getAttribute('data-generic-sign-scan');
                const id = existing || `generic-sign-${Date.now()}-${n++}`;
                if (!existing) el.setAttribute('data-generic-sign-scan', id);
                out.push(id);
              }
              return out;
            }"""
        )
        for scan_id in custom_ids or []:
            if scan_id in seen_handles:
                continue
            loc = page.locator(f'[data-generic-sign-scan="{scan_id}"]')
            try:
                raw = await inspect(loc)
                raw["scan_id"] = scan_id
                seen_handles.add(scan_id)
                score, kind = score_generic_sign_candidate(raw)
                if score > 0:
                    label = str(raw.get("label") or "").strip()
                    candidates.append((score, -order, loc, label, kind))
                    order += 1
                else:
                    label = str(raw.get("label") or "").strip()
                    if label and re.search(r"签到|打卡|每日|今日|领取|转动|check", label, re.I):
                        rejected.append(f"{kind}:{label[:24]}")
            except Exception:
                continue
    except Exception:
        pass

    if candidates:
        score, neg_index, item, label, kind = max(
            candidates, key=lambda row: (row[0], row[1])
        )
        return item, f"generic:{-neg_index}:{kind}:{score}:{label[:40]}"
    if rejected and not getattr(generic_sign_cta, "_reject_logged", False):
        print("  generic sign rejects: " + " | ".join(rejected[:5]), flush=True)
        generic_sign_cta._reject_logged = True  # type: ignore[attr-defined]
    return None, None


async def wait_for_any_visible(
    page,
    selectors: list[str],
    total_s: float,
    *,
    allow_generic_sign_cta: bool = False,
    trusted_sign: bool = False,
) -> tuple | tuple[None, None]:
    """Poll for the caller's selectors without crossing workflow boundaries.

    Generic check-in discovery is opt-in and must only run while seeking a
    check-in CTA. SSO and site-specific subflows use their exact selectors.
    """
    deadline = time.monotonic() + total_s
    saw_done = ""
    # Reset once per wait, not once per poll: preserve done signals and keep
    # repeated non-CTA labels from flooding the cron log.
    first_visible._skip_logs = 0  # type: ignore[attr-defined]
    first_visible._done_signal = ""  # type: ignore[attr-defined]
    generic_sign_cta._reject_logged = False  # type: ignore[attr-defined]
    while time.monotonic() < deadline:
        loc, sel = await first_visible(
            page,
            selectors,
            timeout_each=350,
            sign_cta_only=allow_generic_sign_cta,
            trusted_sign=trusted_sign,
            reset_scan_state=False,
        )
        if loc is not None:
            return loc, sel
        if allow_generic_sign_cta:
            # Selector packs are intentionally conservative. If a site uses a
            # custom interactive element, scan safe controls by CTA attributes.
            generic, gsel = await generic_sign_cta(page)
            if generic is not None:
                print(f"  generic sign CTA: {gsel}", flush=True)
                return generic, gsel
            dsig = getattr(first_visible, "_done_signal", "") or ""
            if dsig:
                saw_done = dsig
        try:
            if is_cloudflare(await page_url(page), await page_text(page, 400)):
                return None, "CLOUDFLARE"
        except Exception:
            pass
        await asyncio.sleep(0.6)
    if saw_done:
        return None, f"DONE:{saw_done}"
    return None, None


async def bypass_chrome_interstitial_if_needed(page) -> bool:
    """Bypass Chrome's 'Dangerous site' / SSL interstitial warning screen.

    Automatically clicks through '#details-button' -> '#proceed-link'
    if the browser is blocked on chrome-error://chromewebdata/.
    """
    for _ in range(3):
        try:
            url = await page_url(page)
            text = await page_text(page, 400)
            if "chrome-error://" in url or "chromewebdata" in url or "危险网站" in text or "不安全" in text:
                print("  Chrome warning page detected, bypassing interstitial...", flush=True)
                try:
                    await page.click("#details-button", timeout=1500)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
                try:
                    await page.click("#proceed-link", timeout=2000)
                    await asyncio.sleep(2.5)
                    print("  Bypassed Chrome interstitial warning.", flush=True)
                    return True
                except Exception as e:
                    print(f"  Failed to click proceed link: {e}", flush=True)
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def wait_out_cloudflare(page, max_s: float = CF_WAIT_S) -> str | None:
    """
    Wait for managed CF / Turnstile on real Chrome.
    Best-effort click challenge widgets; do NOT exit immediately.
    Return 'CLOUDFLARE' if still stuck, else None.
    """
    started = time.monotonic()
    saw = False
    last_click = 0.0
    while time.monotonic() - started < max_s:
        url = await page_url(page)
        text = await page_text(page, 500)
        if not is_cloudflare(url, text):
            if saw:
                print(f"  CF cleared after {time.monotonic()-started:.0f}s", flush=True)
            return None
        saw = True
        elapsed = time.monotonic() - started
        print(f"  CF/Turnstile… {elapsed:.0f}s (请在 Chrome 完成验证若卡住)", flush=True)
        if elapsed - last_click >= 8.0:
            if await try_click_challenge_widgets(page):
                print("  tried challenge widget click", flush=True)
            last_click = elapsed
        await asyncio.sleep(1.5)
    url = await page_url(page)
    text = await page_text(page, 500)
    if is_cloudflare(url, text):
        return "CLOUDFLARE"
    return None


def terms_kind_is_checkbox(kind: str) -> bool:
    """True only for real terms checkboxes.

    ``checkbox`` = a native input; ``wrap`` = an element containing one;
    ``role`` = a custom role=checkbox/aria-checked element. Everything else
    (bare text, links) is ``skip`` — e.g. mofas's "我已阅读并同意用户协议"
    span is a link that opens /user-agreement in a new tab.
    """
    return kind in ("checkbox", "wrap", "role")


async def check_terms(page) -> int:
    """Must run BEFORE LinuxDO. Only terms/privacy checkboxes — never links, never twice."""
    hits = 0
    clicked: set[str] = set()
    for sel in TERMS_CLICK_SELECTORS:
        try:
            loc = page.locator(sel)
            count = min(await loc.count(), 4)
            for i in range(count):
                item = loc.nth(i)
                try:
                    if not await item.is_visible(timeout=250):
                        continue
                    aria = await item.get_attribute("aria-checked")
                    if aria == "true":
                        continue
                    kind = await item.evaluate(
                        """(el) => {
                          if (el.tagName === 'A') return 'skip';
                          if (el.tagName === 'INPUT' && el.type === 'checkbox') return 'checkbox';
                          if (el.querySelector('input[type="checkbox"]')) return 'wrap';
                          if (el.getAttribute('role') === 'checkbox' || el.getAttribute('aria-checked') !== null) return 'role';
                          return 'skip';
                        }"""
                    )
                    if not terms_kind_is_checkbox(kind):
                        continue
                    # evaluate() cannot hand back a DOM node (it serializes to
                    # 'ref: <Node>' as a str), so resolve the inner checkbox via
                    # locator instead — otherwise is_checked() raises AttributeError.
                    if kind == "checkbox":
                        boxloc = item
                    else:
                        boxloc = item.locator('input[type="checkbox"]')
                    if await boxloc.count():
                        if await boxloc.first.is_checked():
                            continue
                        key = await boxloc.first.evaluate("(el) => el.outerHTML.slice(0, 160)")
                    else:
                        key = await item.evaluate("(el) => el.outerHTML.slice(0, 160)")
                    if key in clicked:
                        continue
                    clicked.add(key)
                    await item.click(timeout=1200, force=True)
                    hits += 1
                    await asyncio.sleep(0.15)
                except Exception:
                    continue
        except Exception:
            continue

    # Checkbox path: only if nearby label/text looks like terms (no blanket check-all)
    try:
        boxes = page.locator('input[type="checkbox"]')
        n = min(await boxes.count(), 12)
        for i in range(n):
            box = boxes.nth(i)
            try:
                if await box.is_checked():
                    continue
                nearby = ""
                try:
                    nearby = await box.evaluate(
                        """(el) => {
                          const bits = [];
                          const lab = el.closest('label');
                          if (lab) bits.push(lab.innerText || '');
                          const wrap = el.closest('.ant-checkbox-wrapper, .el-checkbox, .n-checkbox, [class*="checkbox"]');
                          if (wrap) bits.push(wrap.innerText || wrap.getAttribute('aria-label') || '');
                          if (el.getAttribute('aria-label')) bits.push(el.getAttribute('aria-label'));
                          if (el.parentElement) bits.push(el.parentElement.innerText || '');
                          return bits.join(' ').slice(0, 200);
                        }"""
                    )
                except Exception:
                    nearby = ""
                if not re.search(r"协议|同意|条款|隐私|Terms|Privacy|我已阅读|已阅读", nearby or "", re.I):
                    continue
                try:
                    await box.check(timeout=800, force=True)
                    hits += 1
                    continue
                except Exception:
                    pass
                await box.evaluate(
                    """(el) => {
                        if (el.checked) return false;
                        el.click();
                        el.checked = true;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }"""
                )
                hits += 1
            except Exception:
                continue
    except Exception:
        pass

    try:
        extra = await page.evaluate(
            """() => {
            let n = 0;
            const isTerms = (s) => /协议|同意|条款|隐私|Terms|Privacy|我已阅读|已阅读/i.test(s || '');
            document.querySelectorAll('[role="checkbox"], [role="switch"], .ant-checkbox-wrapper, .el-checkbox, .n-checkbox, label').forEach(el => {
              const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
              const pt = (el.parentElement && el.parentElement.innerText || '').trim();
              if (!isTerms(t) && !isTerms(pt)) return;
              if (el.getAttribute('aria-checked') === 'true') return;
              if (el.classList && (el.classList.contains('is-checked') || String(el.className).includes('checked'))) return;
              try { el.click(); n++; } catch (e) {}
            });
            // Only terms-adjacent native checkboxes (never blanket all boxes)
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
              if (cb.checked || cb.disabled) return;
              const lab = cb.closest('label');
              const wrap = cb.closest('.ant-checkbox-wrapper, .el-checkbox, .n-checkbox, [class*="checkbox"]');
              const blob = [
                (lab && lab.innerText) || '',
                (wrap && (wrap.innerText || wrap.getAttribute('aria-label'))) || '',
                cb.getAttribute('aria-label') || '',
                (cb.parentElement && cb.parentElement.innerText) || '',
              ].join(' ');
              if (!isTerms(blob)) return;
              try {
                cb.click();
                cb.checked = true;
                cb.dispatchEvent(new Event('input', { bubbles: true }));
                cb.dispatchEvent(new Event('change', { bubbles: true }));
                n++;
              } catch (e) {}
            });
            return n;
        }"""
        )
        if isinstance(extra, int):
            hits += extra
    except Exception:
        pass

    if hits:
        await asyncio.sleep(0.4)
    return hits


async def has_linuxdo_cta(page) -> bool:
    loc, _sel = await first_visible(page, LINUXDO_SELECTORS, timeout_each=250)
    return loc is not None


# ---------------------------------------------------------------------------
# SSO
# ---------------------------------------------------------------------------

def is_linuxdo_authorize_url(url: str) -> bool:
    """Only LinuxDO-owned OAuth *authorize* pages.

    The host must be linux.do or a *.linux.do subdomain AND the path must carry
    an authorize segment. Without the path check, any resident linux.do tab
    (a topic page like https://linux.do/t/topic/…/n, the home page, a user
    profile) is misclassified as the authorization UI. try_linuxdo_sso's main
    loop then breaks on that resident tab before reaching the real
    connect.linux.do/oauth2/authorize tab, clicks AUTHORIZE_SELECTORS on a page
    with no 「允许」 button, and every SSO site times out with
    saw_linuxdo=True / authorize:0.
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not (host == "linux.do" or host.endswith(".linux.do")):
        return False
    path = (parsed.path or "").lower()
    return "authorize" in path


def is_github_authorize_url(url: str) -> bool:
    """GitHub-owned OAuth *authorize* pages only.
    The host must be github.com or a *.github.com subdomain AND the path must
    carry an authorize segment (`/login/oauth/authorize`). Without the path
    check, any resident github.com tab (a repo page, the home page) would be
    misclassified as the authorization UI and AUTHORIZE_SELECTORS could click
    an unrelated button.
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not (host == "github.com" or host.endswith(".github.com")):
        return False
    path = (parsed.path or "").lower()
    return "authorize" in path


def sso_terminal_page_status(text: str, url: str) -> str | None:
    """Map deterministic OAuth-path failures without waiting for SSO timeout."""
    block = classify_page_block(text, url)
    if block and block[0] == "upstream_unavailable":
        return "UPSTREAM_UNAVAILABLE"
    return None


async def try_linuxdo_sso(page, origin_host: str = "", browser=None) -> str:
    """
    Full SSO:
      1) overlay off
      2) wait CTA (no instant NO_BUTTON)
      3) check terms
      4) click LinuxDO
      5) long wait authorize/redirect/CF  — never return in a few seconds after click
      6) finally close oauth popups opened during this SSO
    """
    try:
        await asyncio.wait_for(page.evaluate(OVERLAY_ZAPPER_JS), timeout=5.0)
    except Exception:
        pass

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return "CLOUDFLARE"

    _, found = await wait_for_any_visible(page, LINUXDO_SELECTORS, CTA_WAIT_S)
    if found == "CLOUDFLARE":
        cf = await wait_out_cloudflare(page, CF_WAIT_S)
        if cf:
            return "CLOUDFLARE"
        _, found = await wait_for_any_visible(page, LINUXDO_SELECTORS, 8.0)

    if found is None:
        login_clicked = await click_first_visible(page, LOGIN_SELECTORS, timeout_each=700)
        if login_clicked:
            print(f"  opened login via {login_clicked}", flush=True)
            await wait_text_ready(page, 30, 10)
            await asyncio.sleep(1.0)
            cf = await wait_out_cloudflare(page, CF_WAIT_S)
            if cf:
                return "CLOUDFLARE"
            _, found = await wait_for_any_visible(page, LINUXDO_SELECTORS, CTA_WAIT_S)
        if found is None or found == "CLOUDFLARE":
            return "NO_BUTTON"

    hits = await check_terms(page)
    print(f"  terms_hits={hits}", flush=True)
    await asyncio.sleep(0.45)

    pre_ids = page_ids(browser) if browser is not None else set()
    pre_ids.add(id(page))
    sso_root_target_id = await page_cdp_target_id(page)
    before_targets = sso_target_snapshot(browser)
    popup_pages: dict[int, Any] = {}
    popup_tracker = register_sso_popup_tracker(page, popup_pages)

    async def _sso_cleanup():
        if browser is None or not sso_root_target_id:
            return
        await close_extra_pages(
            browser,
            pre_ids,
            keep_page=page,
            owned_root_target_id=sso_root_target_id,
        )

    await wait_turnstile_token(page)
    clicked = await click_first_visible(page, LINUXDO_SELECTORS, timeout_each=1200)
    if not clicked:
        await check_terms(page)
        await asyncio.sleep(0.4)
        clicked = await click_first_visible(page, LINUXDO_SELECTORS, timeout_each=1500)
    if not clicked:
        return "NO_BUTTON"

    print(f"  SSO targets before click: {before_targets}", flush=True)
    print(f"  clicked LinuxDO: {clicked}", flush=True)
    click_t0 = time.monotonic()

    try:
        await asyncio.sleep(2.5)

        active = page
        deadline = time.monotonic() + SSO_TIMEOUT_S
        saw_linuxdo = False
        authorize_clicks = 0
        cloudflare_since = None
        reclicked = False

        while time.monotonic() < deadline:
            try:
                if browser is not None:
                    for ctx in browser.contexts:
                        for p in list(ctx.pages):
                            try:
                                u = (p.url or "").lower()
                            except Exception:
                                continue
                            if is_linuxdo_authorize_url(u):
                                active = p
                                saw_linuxdo = True
                                break
                            if id(p) not in pre_ids and u and u != "about:blank":
                                if any(k in u for k in ("oauth", "authorize", "connect", "login", "sign")):
                                    active = p
                                # OAuth 授权点完后常在新 tab 秒回原站点(如 agentrouter
                                # 回 /console/personal),authorize 页随之关闭。回跳页 URL
                                # 不含 oauth/authorize/connect/login/sign,旧选页逻辑选不到,
                                # active 停在已关的 authorize 页 → 死磕 closed 直到超时。
                                # 已 saw_linuxdo 或点过 authorize 后,把指向 origin_host 的新
                                # tab 也纳入候选(优先级低于 authorize 页)。
                                # 2026-08-26 smoke4 实证 linuxdo 回跳 agentrouter 漏判根因。
                                elif (saw_linuxdo or authorize_clicks) and origin_host and origin_host in u:
                                    active = p
                        if saw_linuxdo:
                            break
                for pp in list(popup_pages.values()):
                    try:
                        u = (pp.url or "").lower()
                    except Exception:
                        continue
                    if is_linuxdo_authorize_url(u):
                        active = pp
                        saw_linuxdo = True
                        break
            except Exception:
                pass

            await asyncio.sleep(SSO_POLL_S)
            try:
                url = await page_url(active)
                text = await page_text(active, 900)
            except Exception:
                # active 页可能在 OAuth 跳转中被关掉(如 authorize 页点完「允许」
                # 后站点把该 tab 关了)。死磕已关的 active 会一直抛「Target page,
                # context or browser has been closed」直到超时,漏判回跳。
                # 回退到 gate page,下一轮开头的目标重扫会重新选 active。
                # 2026-08-26 smoke4 实证 linuxdo=FAIL 根因即此(点「允许」后 ×9 closed)。
                if active is not page:
                    try:
                        await page_url(page)
                        active = page
                    except Exception:
                        pass
                continue

            lower_url = url.lower()
            elapsed = time.monotonic() - click_t0

            terminal_status = sso_terminal_page_status(text, url)
            if terminal_status:
                return terminal_status

            if is_cloudflare(url, text):
                if cloudflare_since is None:
                    cloudflare_since = time.monotonic()
                    print("  CF on OAuth path…", flush=True)
                if time.monotonic() - cloudflare_since > CF_WAIT_S:
                    remain = MIN_DWELL_AFTER_SSO_CLICK_S - (time.monotonic() - click_t0)
                    if remain > 0:
                        await asyncio.sleep(remain)
                    return "CLOUDFLARE"
                continue
            cloudflare_since = None

            # authorize only on oauth host — avoid misclick origin "Continue"
            if is_linuxdo_authorize_url(lower_url):
                saw_linuxdo = True
                await check_terms(active)
                auth = await click_first_visible(active, AUTHORIZE_SELECTORS, timeout_each=1000)
                if auth:
                    authorize_clicks += 1
                    print(f"  authorize: {auth}", flush=True)
                    await asyncio.sleep(5.0)
                else:
                    await asyncio.sleep(1.5)
                continue

            if saw_linuxdo and origin_host and origin_host in lower_url:
                await wait_text_ready(active, 40, 12)
                text2 = await page_text(active, 900)
                if not looks_logged_out(text2):
                    remain = MIN_DWELL_AFTER_SSO_CLICK_S - (time.monotonic() - click_t0)
                    if remain > 0:
                        await asyncio.sleep(remain)
                    return "OK"
                continue

            if any(k in text for k in ("退出登录", "退出", "登出", "Logout", "个人中心", "钱包管理")):
                if not looks_logged_out(text):
                    remain = MIN_DWELL_AFTER_SSO_CLICK_S - (time.monotonic() - click_t0)
                    if remain > 0:
                        await asyncio.sleep(remain)
                    return "OK"

            if not looks_logged_out(text) and any(
                k in lower_url for k in ("profile", "console", "personal", "wallet", "check-in", "checkin")
            ):
                if saw_linuxdo or authorize_clicks:
                    remain = MIN_DWELL_AFTER_SSO_CLICK_S - (time.monotonic() - click_t0)
                    if remain > 0:
                        await asyncio.sleep(remain)
                    return "OK"

            if looks_logged_out(text) and not saw_linuxdo and not reclicked and 18 < elapsed < 24:
                reclicked = True
                print("  re-click LinuxDO (OAuth not started)", flush=True)
                await check_terms(active)
                await click_first_visible(active, LINUXDO_SELECTORS, timeout_each=800)

        remain = MIN_DWELL_AFTER_SSO_CLICK_S - (time.monotonic() - click_t0)
        if remain > 0:
            await asyncio.sleep(remain)
        after_targets = sso_target_snapshot(browser)
        print(
            f"  SSO timeout: saw_linuxdo={saw_linuxdo} popup_events={popup_tracker['events']} "
            f"targets_before={before_targets} targets_after={after_targets}",
            flush=True,
        )
        return "TIMEOUT"
    finally:
        await _sso_cleanup()


async def maybe_sso_if_needed(page, force: bool = False, browser=None) -> str | None:
    text = await page_text(page, 900)
    cta = await has_linuxdo_cta(page)
    logged_out = looks_logged_out(text)

    if not force and not logged_out and not cta:
        return None

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return "CLOUDFLARE"

    host = ""
    try:
        host = urlparse(await page_url(page)).netloc
    except Exception:
        pass

    if not cta:
        if not logged_out and not force:
            return None
        if force and not logged_out:
            _, found = await wait_for_any_visible(page, LINUXDO_SELECTORS, 4.0)
            if found is None:
                return "NO_BUTTON"

    return await try_linuxdo_sso(page, origin_host=host, browser=browser)


async def try_github_oauth(page, timeout: float = SSO_TIMEOUT_S, browser=None, return_host: str = "agentrouter.org") -> str:
    """GitHub OAuth authorization handoff (仿 try_linuxdo_sso 模式).

    Called AFTER the site CTA pointed us at github.com/oauth/authorize. The
    staging Chrome profile (9222) usually holds GitHub cookies; this flow only
    clicks through the authorization page. It deliberately NEVER fills GitHub
    username/password — an unauthenticated GitHub session reports AUTH_REQUIRED,
    and the operator signs GitHub into 9222 manually first.

    Returns "OK" when the authorize button was clicked (or the authorize page
    redirect back to an authenticated origin), "AUTH_REQUIRED" when the user is
    not logged into GitHub (login form shown) and account/password can't be
    filled here, "CLOUDFLARE" on a CF wall, and "TIMEOUT" otherwise. All
    non-OK returns are failures; never treat them as a successful sign-in.
    """
    deadline = time.monotonic() + float(timeout)
    saw_authorize = False
    authorize_clicks = 0
    cloudflare_since = None

    while time.monotonic() < deadline:
        cur = page
        try:
            if browser is not None:
                for ctx in browser.contexts:
                    for p in list(ctx.pages):
                        try:
                            if is_github_authorize_url(p.url or ""):
                                cur = p
                                break
                        except Exception:
                            continue
        except Exception:
            pass

        try:
            text = await page_text(cur, 900)
            url = await page_url(cur)
        except Exception:
            text, url = "", ""

        lower_url = url.lower()

        if is_cloudflare(url, text):
            if cloudflare_since is None:
                cloudflare_since = time.monotonic()
                print("  GitHub OAuth: CF …", flush=True)
            if time.monotonic() - cloudflare_since > CF_WAIT_S:
                return "CLOUDFLARE"
            await asyncio.sleep(SSO_POLL_S)
            continue
        cloudflare_since = None

        if is_github_authorize_url(lower_url):
            saw_authorize = True
            # 未登录 GitHub:authorize 页会内嵌 GitHub 登录表单,绝不会出现
            # Authorize/Allow 按钮。先退 aground,避免把登录表单当授权页磨蹭。
            if _is_github_login_page(text, lower_url):
                return "AUTH_REQUIRED"
            auth = await click_first_visible(cur, GITHUB_AUTHORIZE_SELECTORS, timeout_each=1000)
            if auth:
                authorize_clicks += 1
                print(f"  GitHub authorize: {auth}", flush=True)
                await asyncio.sleep(4.0)
                return "OK"
            # 授权页出现了但没点到 Authorize → 继续轮询(偶尔按钮延迟渲染)
            await asyncio.sleep(1.5)
            continue

        # 站点回跳(非未登录态)且确实走过了授权页 → 视为授权完成
        if saw_authorize and not looks_logged_out(text):
            return "OK"

        # GitHub 已登录时授权页常一闪即回跳,SSO_POLL_S 轮询未必抓到 authorize
        # URL,saw_authorize 永不为 True → 旧逻辑超时。且 OAuth 常在新 tab 打开
        # 完成回跳(原 gate page 仍停在 /login 未登录态),只看 cur 会漏判。
        # 故遍历所有 page:任一已落回目标站点(return_host,且非 GitHub 登录表单、
        # 非未登录态)→ 视为授权完成回跳。
        # 实证:2026-08-26 smoke 点「使用 GitHub 继续」后新 tab 秒回 agentrouter
        # /console/token(已登录),原 page 仍在 /login,旧逻辑超时。
        # 收紧:必须回跳到目标站点本身,否则像 base64.us 这种早先开着
        # 的无关已登录标签页会被误判成 OAuth 回跳成功(2026-08-26 smoke4 实证
        # 误报 OK,实则 GitHub terms_hits=0 没签到)。
        # return_host 参数化:agentrouter 传 agentrouter.org(默认),tabitoken 传 tabitoken.com。
        if browser is not None:
            for ctx in browser.contexts:
                for tp in list(ctx.pages):
                    try:
                        tu = (tp.url or "").lower()
                        if return_host not in tu:
                            continue
                        tt = await page_text(tp, 600)
                    except Exception:
                        continue
                    if (
                        not is_github_authorize_url(tu)
                        and not _is_github_login_page(tt, tu)
                        and not looks_logged_out(tt)
                    ):
                        print(f"  GitHub OAuth: 回跳 {return_host} 已登录态 {tu[:60]},视为授权完成", flush=True)
                        return "OK"

        # 落到 GitHub 登录表单 → 绝不填账密,AUTH_REQUIRED
        if _is_github_login_page(text, lower_url):
            return "AUTH_REQUIRED"

        await asyncio.sleep(SSO_POLL_S)

    # 授权页出现过但没点成功 → 诚实报 AUTH_REQUIRED/TIMEOUT,不误报 OK
    if saw_authorize and authorize_clicks == 0:
        return "AUTH_REQUIRED"
    return "TIMEOUT"


def _is_github_login_page(text: str, url: str = "") -> bool:
    """'GitHub 登录表单' 判别——识别后返回 AUTH_REQUIRED,绝不代填账密。

    Strict: on `github.com/login/oauth/authorize` the user may be asked to
    sign in first (GitHub's own login form) or presented the authorize
    button. Only text/URLs that unambiguously describe a GitHub credential
    form qualify — the site's own 登录 header (which contains 密码 because of
    a sign-in dropdown) must NOT cause a false AUTH_REQUIRED.

    ⚠️ 域名守卫:必须先确认页面属于 GitHub 域(github.com / *.github.com)。
    2026-08-26 tabitoken 实测踩坑:tabitoken 自身登录页(/sign-in)同样含「用户名
    或电子邮件」+「密码」文本,旧逻辑(纯文本匹配、无域名守卫)把 tabitoken 登录
    卡误判成 GitHub 登录表单 → try_github_oauth 首轮就 AUTH_REQUIRED,即使 CTA
    网页本可正常跳转 OAuth。非 GitHub 域任何 URL 一律不判(返回 False),让流程
    继续轮询,直到真落到 github.com/login/oauth/authorize。
    """
    t = text or ""
    u = (url or "").lower()
    # 域名守卫:非 GitHub 域绝不判登录表单(tabitoken/sign-in 自带「用户名或邮箱+密码」
    # 文本,旧纯文本匹配会误判成 GitHub 登录表单)。is_github_authorize_url 同款域解析。
    try:
        parsed = urlparse(u)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        host = ""
    if not (host == "github.com" or host.endswith(".github.com")):
        return False
    if "sign in to github" in t.lower():
        return True
    if "sign in · github" in t.lower():  # GitHub login page title
        return True
    if ("用户名或电子邮件" in t or "账号或邮箱" in t) and "密码" in t:
        return True
    if "sign in" in u and ("password" in u or "login" in u):
        return True
    return False


async def try_account_login_if_needed(
    page,
    adapter,
    *,
    _get_credential=None,
    _fill_first_visible=None,
) -> str:
    """Try account+password login for sites with no OAuth (纯账密站).

    Returns 'OK' | 'NO_BUTTON' | 'NO_CREDENTIAL' | 'FAIL'.
    Only attempts when (a) the site has registered credentials in the vault doc
    AND (b) the page currently shows a password input. Never prints the password.

    Gating order matters: no credential → NO_CREDENTIAL without touching the page
    (so unregistered sites never get a password box probed / risk no misfill).

    _get_credential / _fill_first_visible are sync/async injection seams for tests;
    production uses get_account_credential (sync) and fill_first_visible (async).
    """
    get_cred = _get_credential or get_account_credential
    fill = _fill_first_visible or fill_first_visible

    creds = get_cred(getattr(adapter, "name", "") or "")
    if not creds:
        return "NO_CREDENTIAL"

    # Probe for a password field (strong signal of a real account+password form).
    # 注意: 有的 New API 系把密码框渲染成 type=text(name=password)(columbina
    # 2026-08-26 inspect: input[name=password] type=text)。若只认 type=password
    # 会漏掉该站 → 用 input[name='password'] 兜底(type=text 也命中)。
    try:
        pw_loc = page.locator("input[type='password']").first
        pw_visible = await pw_loc.is_visible(timeout=1000)
        if not pw_visible:
            pw_loc = page.locator("input[name='password']").first
            pw_visible = await pw_loc.is_visible(timeout=1000)
    except Exception:
        pw_visible = False
    if not pw_visible:
        return "NO_BUTTON"

    account, password = creds[0], creds[1]
    acct_sel = await fill(page, ACCOUNT_FIELD_SELECTORS, account)
    pw_sel = await fill(page, PASSWORD_FIELD_SELECTORS, password)
    if acct_sel is None or pw_sel is None:
        return "FAIL"  # password box existed but account/password field missing

    # Some clones gate the submit button behind a terms checkbox.
    try:
        await check_terms(page)
    except Exception:
        pass

    submit_sel = None
    # Submit: standard click first (actionability-checked) — React/Semi forms like
    # Sub2API need a real click event; force=True can fire before the handler binds
    # and the POST never goes out. Fall back to force, then native evaluate click.
    for sel in LOGIN_SUBMIT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=600):
                try:
                    await btn.click(timeout=3000)
                except Exception:
                    await btn.click(timeout=3000, force=True)
                submit_sel = sel
                break
        except Exception:
            continue
    if submit_sel is None:
        try:
            for sel in LOGIN_SUBMIT_SELECTORS:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=400):
                    await btn.evaluate("(el) => el.click()")
                    submit_sel = sel
                    break
        except Exception:
            pass
    if submit_sel is None:
        return "FAIL"

    # Verify login succeeded: URL leaves /login AND page no longer looks logged out.
    await asyncio.sleep(2.5)
    try:
        url = await page_url(page)
        text = await page_text(page, 600)
    except Exception:
        return "FAIL"
    if is_auth_page_url(url):
        return "FAIL"
    if looks_logged_out(text):
        return "FAIL"
    print(f"  account login OK for {adapter.name}", flush=True)
    return "OK"


async def _ensure_account_logged_in(page, adapter) -> bool:
    """Pre-flight account+password login for credential-bearing sites.

    Pure-account sites (no OAuth) often 502 or hard-gate their /check-in route
    for guests, so the main goto(site_url) would bail on upstream_unavailable
    before any auth_required branch fires. For sites with registered account
    credentials, navigate to the derived /login page first and log in if a
    password form is present; if already logged in (no password box) this is a
    silent no-op. Never blocks the main flow on failure — the 4 auth_required
    sites downstream still act as fallbacks."""
    if not get_account_credential(adapter.name):
        return False
    # derive login page: adapter.login_url override, else scheme://netloc/login
    # (gemai 的 /login 404,真登录卡在 /sign-in,靠 login_url 字段 override)。
    login_url = (getattr(adapter, "login_url", "") or "").strip()
    if not login_url:
        try:
            parts = urlparse(adapter.url)
            login_url = f"{parts.scheme}://{parts.netloc}/login"
        except Exception:
            return False
    try:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        await wait_text_ready(page, 40, 10)
    except Exception:
        return False
    try:
        status = await try_account_login_if_needed(page, adapter)
    except Exception:
        return False
    return status == "OK"


async def _attempt_account_login_then_reload(page, adapter, site_url: str) -> bool:
    """Account-login gate for the 4 auth_required sites in legacy_checkin_on_page.
    Returns True only when account login succeeded AND the page was reloaded onto
    site_url so the caller can fall through to normal CTA scanning. Returns False
    (no side effects beyond a failed login attempt) so the caller keeps its
    original auth_required failure path."""
    try:
        status = await try_account_login_if_needed(page, adapter)
    except Exception as exc:
        print(f"  account login error for {adapter.name}: {exc}", flush=True)
        return False
    if status != "OK":
        return False
    await asyncio.sleep(2.5)
    try:
        await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        await wait_text_ready(page, 40, 12)
    except Exception:
        pass
    return True


def sso_fail_msg(status: str | None) -> str:
    if not status:
        return "FAIL:not logged in (sso not attempted)"
    return f"FAIL:not logged in ({status.lower()})"


def result_from_sso_failure(status: str, adapter: str) -> CheckinResult:
    if status == "UPSTREAM_UNAVAILABLE":
        return fail_result(
            "upstream_unavailable",
            detail="SSO callback upstream unavailable",
            adapter=adapter,
            stage="auth",
        )
    return result_from_legacy(sso_fail_msg(status), adapter=adapter)


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------

# SPA 404 strings emitted by New-API frontends when the requested path is a
# client-only route that the server doesn't recognise for a not-yet-authed
# session (e.g. grok-heavy /console/personal).  The page renders a 404
# shell — no CTA, no login-form text either — so looks_logged_out() returns
# False and the runner wrongly tags no_button.  These tokens distinguish
# that auth-gated empty state from a genuine "no check-in feature" page.
AUTH_GATED_404_TOKENS = (
    "糟糕！页面未找到",
    "页面不存在",
    "404 Not Found",
    "Page Not Found",
    "页面似乎",
    "已被移除",
    "返回主页",
)


def is_auth_gated_empty(text: str) -> bool:
    """True when the body is a 404 shell with none of the signed-in chrome.

    Used as a last-resort gate before no_button: an empty 404 on a known
    console/*personal route usually means the SPA needs an authed session to
    render the route at all (the server returns 404 for guests).  Treating
    it as auth_gated lets the runner retry via SSO instead of misreporting
    no_button.  Not a logged-in page (no 退出/Login form), yet also not a
    real dashboard (no 控制台/概览/Toggle Sidebar chrome).
    """
    t = (text or "").strip()
    if not t:
        return True
    if not looks_logged_out(t):
        # logged-in dashboard chrome — not an auth-gated empty page
        if any(k in t for k in ("Toggle Sidebar", "控制台", "概览", "数据看板", "个人资料")):
            return False
        # 404 shell tokens with no signed-in chrome → likely auth-gated empty
        if any(tok in t for tok in AUTH_GATED_404_TOKENS):
            return True
    return False


def done_button_signal(lab: str, disabled: bool = False) -> str:
    """
    Strict done-state from a single button label.
    - 今日已签到: OK
    - bare 已签到/已领取: only if disabled
    - multi-line card with 已签到 + 今天/今日: OK
    Empty string = not a done signal (do NOT mark).
    """
    t = (lab or "").strip()
    if not t:
        return ""
    # collapse whitespace for exact-ish matches
    compact = re.sub(r"\s+", "", t)
    if compact in ("今日已签到", "今天已签到", "今日已领取", "今天已领取"):
        return f"btn:{compact}"
    if t in ("今日已签到", "今天已签到", "今日已领取") or compact in (
        "今日已签到",
        "今天已签到",
        "今日已领取",
    ):
        return f"btn:{compact}"
    if compact in ("已签到", "已领取") or t in ("已签到", "已领取"):
        return f"btn:disabled:{compact or t}" if disabled else ""
    # card: 每日签到\n已签到\n今天 +¥20
    if "已签到" in t and ("今天" in t or "今日" in t):
        return "btn:card:today"
    if "已领取" in t and ("今天" in t or "今日" in t):
        return "btn:card:claimed"
    return ""


async def scan_done_dom_signal(page) -> str:
    """
    Scan interactive nodes for done-state labels.
    Covers button / a / role=button (林夕 etc. may not be plain <button>).
    """
    selectors = (
        "button",
        "a",
        "[role='button']",
        "[role='tab']",
        "div[class*='button']",
        "span[class*='button']",
    )
    try:
        for css in selectors:
            try:
                loc = page.locator(css)
                n = await loc.count()
            except Exception:
                continue
            for i in range(min(max(n, 0), 40)):
                item = loc.nth(i)
                try:
                    if not await item.is_visible(timeout=200):
                        continue
                except Exception:
                    continue
                try:
                    lab = (await item.inner_text(timeout=250) or "").strip()
                except Exception:
                    continue
                if not lab:
                    continue
                # only care about check-in related short labels (avoid whole nav)
                if "签" not in lab and "领" not in lab and "check" not in lab.lower():
                    continue
                # multi-line cards ok; huge blocks are not button labels
                if len(lab) > 80:
                    continue
                disabled = False
                try:
                    disabled = bool(await item.is_disabled())
                except Exception:
                    disabled = False
                sig = done_button_signal(lab, disabled)
                if sig:
                    return sig
    except Exception as e:
        print(f"  ! scan_done_dom: {e}", flush=True)
    return ""


async def already_done(
    page, already_selectors: list[str], *, trusted_already: bool = False
) -> str:
    """
    Return non-empty confirm/detail signal if already checked in, else "".
    STRICT: bare enabled 「已签到」 is NOT enough.
    """
    sig = await scan_done_dom_signal(page)
    if sig:
        return sig

    text = await page_text(page, 2000)
    if "开始转动" in text:
        return ""
    if is_valid_checkin_confirm(text):
        return confirm_signal(text) or "text-confirm"

    for sel in already_selectors:
        s = (sel or "").replace(" ", "")
        if not trusted_already:
            if s in ('text="已签到"', "text=已签到", 'text="已领取"', "text=已领取", 'text="已完成"'):
                continue
            if "今日已签" not in sel and "签到成功" not in sel and "领取成功" not in sel and "今日已领" not in sel:
                if "Checked in" not in sel and "already checked" not in sel and "已签到" not in sel:
                    continue
        try:
            loc = page.locator(sel).first
            if not await loc.is_visible(timeout=500):
                continue
            # node label path (button or text)
            try:
                lab = (await loc.inner_text(timeout=300) or "").strip()
            except Exception:
                lab = ""
            try:
                disabled = bool(await loc.is_disabled())
            except Exception:
                disabled = False
            if lab:
                node_sig = done_button_signal(lab, disabled)
                if node_sig:
                    return node_sig
            if trusted_already:
                # Curated adapter phrase matched a visible element — that is
                # the site's own done-state indicator (今日签到获得鸡腿N个).
                return confirm_signal(await page_text(page, 2000)) or f"sel:{sel}"
            # text selectors still need body confirm for weak labels
            if is_valid_checkin_confirm(await page_text(page, 2000)):
                return confirm_signal(await page_text(page, 2000)) or "sel-confirm"
        except Exception:
            continue
    return ""


def is_bohe_site(site_url: str) -> bool:
    u = (site_url or "").lower()
    return "x666.me" in u or "qd.x666" in u


def is_arkengine_site(site_url: str) -> bool:
    """小鸡毛(Ark Engine 首页签到)站点判定.

    特判仅针对 game.ark717.com 首页右上角 #checkinBtn 按钮的小鸡毛签到站。
    **绝不能用宽泛的 "arkengine" 子串匹配**: 同品牌的 game.arkengine.me
    (activities/checkin 签到页、activities/wheel 幸运转盘) 是另一站点, 走通用
    browser 流程, 若被子串误判会强 route 到 ark717_checkin 而硬跳转到
    game.ark717.com(小鸡毛), 导致转盘/签到根本没在该站点执行(2026-09-04 实测
    bug: wheel 大 转盘被劫持跳小鸡毛)。
    """
    u = (site_url or "").lower()
    return "game.ark717.com" in u


# 小鸡毛 Ark Engine 专用流:签到在 game.ark717.com 首页右上角。
# 登录走 /auth/login → linux.do OAuth（共享 profile 通常已有 session）。
_ARK717_CHECKIN_URL = "https://game.ark717.com/"


async def _approve_linuxdo_authorize(page) -> str | None:
    """在 connect.linux.do/oauth2/authorize 页点「允许」。返回所用 selector 或 None。"""
    for _ in range(2):
        if not is_linuxdo_authorize_url(await page_url(page)):
            return None
        used = await click_first_visible(page, AUTHORIZE_SELECTORS, timeout_each=1200)
        if used:
            print(f"  arkengine linuxdo 授权: {used}", flush=True)
            await asyncio.sleep(4.0)
            return used
        await asyncio.sleep(1.0)
    return None


async def arkengine_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """
    小鸡毛 Ark Engine 专用签到:
      1) 首页 game.ark717.com 右上角 #checkinBtn「签到+1」（已登录）或「投币登录」（未登录）。
      2) 未登录时走 /auth/login → linux.do OAuth（共享 profile 通常已有 session → 自动授权）。
      3) 签到: 点击 #checkinBtn → 等待「已签到」/「今日已签到」/「签到成功」确认文本。
      4) 已签: 按钮 disabled 文本「已签到」→ ALREADY。
    """
    kind = adapter.kind or "arkengine"
    print(f"  ark717 flow: {adapter.name}", flush=True)

    # --- 确保登录态(最多 2 轮) ---
    for _ in range(2):
        try:
            await page.goto(_ARK717_CHECKIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        except Exception:
            pass
        await wait_text_ready(page, 40, max(adapter.ready_rounds, 12))

        cf = await wait_out_cloudflare(page, CF_WAIT_S)
        if cf:
            return fail_result("cloudflare", adapter=kind)

        url = await page_url(page)
        text = await page_text(page, 700)

        # 已落在 OAuth authorize → 确认授权后重走
        if is_linuxdo_authorize_url(url):
            await _approve_linuxdo_authorize(page)
            continue

        # 未登录 → 点「投币登录」走 linux.do OAuth
        if "投币登录" in text:
            print("  ark717 需登录,走 /auth/login", flush=True)
            login_btn = page.locator('a[href="/auth/login"]').first
            if await login_btn.is_visible():
                await login_btn.click(timeout=3000)
                await asyncio.sleep(4.0)
                # 如果是 linux.do 登录页，填账密或等 OAuth 重定向
                if is_linuxdo_authorize_url(await page_url(page)):
                    await _approve_linuxdo_authorize(page)
                else:
                    # 可能是 linux.do /login 页 — 已有 session 会自动跳转
                    await wait_text_ready(page, 40, 12)
            continue
        break

    # --- 已签 / 未签判定 ---
    already_markers = ("今日已签到", "已签到", "签到成功")
    text = await page_text(page, 900)
    if any(k in text for k in already_markers):
        return confirmed_done_result("text-confirm", adapter=kind)

    # 找签到按钮: #checkinBtn 文本「签到+1」或「签到+N」
    sign_selectors = ['button:has-text("签到")', '#checkinBtn']
    btn, used = await wait_for_any_visible(page, sign_selectors, 12.0)
    if btn is None:
        text2 = await page_text(page, 700)
        if any(k in text2 for k in already_markers) or is_valid_checkin_confirm(text2):
            return confirmed_done_result(confirm_signal(text2) or "text-confirm", adapter=kind)
        return fail_result("no_button", detail=text2[:80], adapter=kind)

    print(f"  ark717 点击签到: {used}", flush=True)
    await btn.click(timeout=3000, force=True)
    await asyncio.sleep(2.5)

    for _ in range(10):
        text3 = await page_text(page, 800)
        if any(k in text3 for k in already_markers) or is_valid_checkin_confirm(text3):
            return confirmed_done_result(
                confirm_signal(text3) or "text-confirm",
                adapter=kind,
                action=ActionEvidence(
                    kind="dom_click", target=str(used or "sign"),
                    attempted_at=datetime.now().isoformat(timespec="seconds"),
                ),
            )
        await asyncio.sleep(1.0)

    text4 = await page_text(page, 800)
    if any(k in text4 for k in already_markers) or is_valid_checkin_confirm(text4):
        return confirmed_done_result(
            confirm_signal(text4) or "text-confirm",
            adapter=kind,
            action=ActionEvidence(
                kind="dom_click", target=str(used or "sign"),
                attempted_at=datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return fail_result("no_confirm", detail=text4[:80], adapter=kind)


def is_agentrouter_site(site_url: str) -> bool:
    """agentrouter.org 专属站判定."""
    u = (site_url or "").lower()
    return "agentrouter.org" in u


# agentrouter 签到触发机制特殊:必须登出后重新登录一次才触发(双账号:
# GitHub OAuth + Linux.do OAuth)。不挂账密凭据;复用 staging 9222 登录态。
AGENTROUTER_SITE_URL = "https://agentrouter.org/"
# OAuth 登录入口在 /login 页(首页只有「登录/注册」普通导航,GitHub/LinuxDO
# 按钮在 /login 弹出的登录卡上,见 inspect 实证 2026-08-26)。
AGENTROUTER_LOGIN_URL = "https://agentrouter.org/login"
AGENTROUTER_TOPUP_URL = "https://agentrouter.org/console/topup"
# OAuth 回跳后等待站点渲染/结算的静默时长(秒)。
AGENTROUTER_OAUTH_RETURN_S = 2.0


async def _agentrouter_current_account(page, browser=None) -> str:
    """返回 agentrouter 当前登录账号 handle(如 github_400491 / linux_400479)。

    未登录/取不到时返回 ""。用于 OAuth 后校验是否真的登到期望账号:
    避免 try_github_oauth 因共享 profile 中其它已登录标签页误报授权完成。

    注意:OAuth 回跳常落在新 tab,gate 的 page 可能仍停在 /login 未登录态。所以
    先导航当前 page 读一次;读不到再遍历共享 profile 中所有 agentrouter 标签页,
    取首个带账号 handle 的(配合 _agentrouter_close_stale_tabs 已清旧页,只会是
    本次 OAuth 真正落地的那一页)。
    """

    def _extract(text: str) -> str:
        m = re.search(r'(github_\d+|linuxdo_\d+|linux_\d+)', text or "")
        return m.group(1) if m else ""

    # 先把当前 page 切到 topup,读一次。
    for _ in range(2):
        try:
            await page.goto(AGENTROUTER_TOPUP_URL, wait_until="commit", timeout=12000)
        except Exception:
            pass
        await asyncio.sleep(0.9)
        try:
            cur = _extract(await page_text(page, 700))
        except Exception:
            cur = ""
        if cur:
            return cur

    # 当前 page 读不到(OAuth 回跳落在别的 tab)则扫描共享 profile 里 agentrouter
    # 标签页取第一个出的 handle。_close_stale_tabs 已清过,这里只会是本次 OAuth
    # 真正登录的页。
    if browser is not None:
        for ctx in browser.contexts:
            for tp in list(ctx.pages):
                try:
                    tu = (tp.url or "").lower()
                except Exception:
                    continue
                if "agentrouter.org" not in tu:
                    continue
                try:
                    tt = _extract(await page_text(tp, 500))
                except Exception:
                    tt = ""
                if tt:
                    return tt
    return ""


async def _agentrouter_close_stale_tabs(page, browser=None):
    """关闭共享 profile(B9222)里其余 agentrouter.org 旧标签页。

    背景:try_github_oauth 的「回跳目标站点已登录态」会遍历所有标签页,只要任一
    agentrouter 标签已登录(不管哪个账号)就误判 OAuth 成功 —— 我此前拉起的
    /console/topup、/console/token 等 linuxdo 会话标签正是这样害 github 被误报 OK
    (2026-08-31 staging 实证:回跳 console/topup=linux 会话,github 却返回 OK;
    加上 handle 加固后 github 实际报 WRONG_ACCOUNT)。故每次 OAuth 前先清掉其它
    agentrouter 标签,让「回跳已登录态」只可能看到本次 OAuth 真正落地的标签页。
    保留当前工作 page 不关。
    """
    if browser is None:
        return
    keep_ids = {id(page)}
    for ctx in browser.contexts:
        for tp in list(ctx.pages):
            try:
                turl = (tp.url or "").lower()
            except Exception:
                continue
            if "agentrouter.org" not in turl:
                continue
            if id(tp) in keep_ids:
                continue
            try:
                await tp.close()
            except Exception:
                pass


async def _agentrouter_logout(page) -> bool:
    """点击 agentrouter 右上角登出并等落到未登录态。返回是否确实登出了。

    已登录态的登出入口藏在右上角头像下拉里(2026-08-26 inspect 实证):
    先点 [class*=avatar] 展开 dropdown,登出项是 [role=menuitem]/li 文本「退出」。
    头像点开后 dropdown 渲染有延迟,旧版 sleep(1.0)+timeout_each=700 在 staging
    时序竞争下偶发点不到「退出」→ menu_clicked=None → 兜底也点不到 → 旧码
    `if not clicked: return True` 误判登出成功,实则会话还在 → 第二轮 goto /login
    被跳回 /console → github NO_GITHUB_CTA(2026-08-26 staging 实证)。
    故:重试点头像+点退出至多 3 轮,每轮用 wait_for 等退出项可见再点;点完后必须
    验证确实落到未登录态(URL 含 /login 或 looks_logged_out),未验证不算成功。
    """
    # 已未登录 → 幂等成功
    try:
        text0 = await page_text(page, 700)
    except Exception:
        text0 = ""
    if looks_logged_out(text0):
        return True

    avatar_sels = ('[class*=avatar]:visible', 'button:has-text("G"):visible',
                   'button:has-text("L"):visible', 'img[alt*=G]:visible')

    for attempt in range(3):
        # 每轮重新探测登录态:登出成功后立即返回
        try:
            cur_text = await page_text(page, 700)
        except Exception:
            cur_text = ""
        if looks_logged_out(cur_text):
            return True
        try:
            cur_url = page.url or ""
        except Exception:
            cur_url = ""
        if "/login" in cur_url:
            return True

        clicked_exit = False
        for avatar_sel in avatar_sels:
            try:
                av = page.locator(avatar_sel).first
                if not await av.is_visible(timeout=600):
                    continue
                await av.click(timeout=2000, force=True)
                print(f"  agentrouter logout: 展开头像菜单 {avatar_sel}", flush=True)
                # dropdown 渲染等足,再用 wait_for 等退出项可见(不靠固定短 sleep)
                await asyncio.sleep(1.2)
                menu_clicked = None
                for msel in AGENTROUTER_LOGOUT_MENU_SELECTORS:
                    try:
                        loc = page.locator(msel).first
                        if await loc.is_visible(timeout=900):
                            await loc.click(timeout=2000, force=True)
                            menu_clicked = msel
                            break
                    except Exception:
                        continue
                if menu_clicked:
                    print(f"  agentrouter logout: {menu_clicked}", flush=True)
                    clicked_exit = True
                    break
                # 菜单没展开 → 点空白处关掉再换下个头像选择器重试
                try:
                    await page.mouse.click(0, 0)
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            except Exception:
                continue

        # 兜底:直接 button/a「退出登录/登出/Logout」
        if not clicked_exit:
            fb = await click_first_visible(page, AGENTROUTER_LOGOUT_SELECTORS, timeout_each=900)
            if fb:
                print(f"  agentrouter logout: {fb}", flush=True)
                clicked_exit = True

        # 验证登出落地(关键:不验证不返回成功)
        if clicked_exit:
            verify_deadline = time.monotonic() + 12.0
            while time.monotonic() < verify_deadline:
                await asyncio.sleep(0.8)
                try:
                    t = await page_text(page, 700)
                    u = page.url or ""
                except Exception:
                    continue
                if looks_logged_out(t) or "/login" in u:
                    return True
            # 点了退出但没落到未登录态 → 继续下一轮重试
        # 没点到任何退出入口 → 继续下一轮(可能是页面还没渲染好)

    return False


def _agentrouter_result(adapter: str, attempts: dict[str, str]) -> CheckinResult:
    """agentrouter 聚合结果:两账号 confirmed → 双 OK;任一失败 → FAIL 带原因。

    attempts 形如 {'github': 'OK', 'linuxdo': 'OK'} / {'github': 'FAIL', ...}。
    每个账号状态(OK / ALREADY / 失败字符串如 AUTH_REQUIRED/TIMEOUT/FAIL)。
    attempts 摘要并入 detail(CheckinResult 无 attempts 字段,避免加未用字段)。

    签到=OAuth 登录即触发额度(无签到按钮/文字),登录点击即 action,结果走
    confirmed_done_result 以满足 apply_attribution 的因果契约(PENDING→DONE +
    runner + action.attempted_at + confirmation.checked_in_today),否则 OK 会被
    归因门判 unattributed_success(2026-08-26 smoke6 实证)。
    """
    gh = attempts.get("github", "unknown")
    ld = attempts.get("linuxdo", "unknown")
    confirmed = {"OK", "ALREADY"}
    gh_ok = gh in confirmed
    ld_ok = ld in confirmed
    summary = f"github={gh},linuxdo={ld}"

    if gh_ok and ld_ok:
        return confirmed_done_result(
            f"dual-oauth confirmed ({summary})",
            adapter=adapter,
            action=ActionEvidence(
                kind="dom_click",
                target="oauth_dual_login",
                attempted_at=datetime.now().isoformat(timespec="seconds"),
            ),
        )
    if gh_ok or ld_ok:
        # 单账号成功绝不当作 OK:agentrouter 要求两个账号各登录一次各自触发额度,只成一个
        # 意味着另一账号(通常 LinuxDO)今天没拿到 25 额度。以前把 single-account 当 OK,
        # 导致系统报「已签」而用户实际少一个账号额度(2026-08-31 实证 linuxdo=NO_BUTTON 仍报
        # single-account confirmed … OK,掩盖了第二账号未登录)。改为 FAIL 以便重跑补齐。
        return fail_result(
            "dual_oauth_incomplete",
            detail="single-account-only (" + summary + ") · 需重跑补齐另一账号",
            adapter=adapter,
            stage="login",
        )
    return fail_result(
        "dual_oauth_incomplete",
        detail=summary,
        adapter=adapter,
        stage="login",
    )


async def agentrouter_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """agentrouter.org 双账号 OAuth 签到:登出 → GitHub 登录签到 → 再登出 → LinuxDO 登录签到。

    1) goto 站点;已登录则先登出(该站必须在未登录态重新登录才触发签到)。
    2) GitHub 账号:点 GitHub CTA → try_github_oauth 授权(不代填账密)→ 回站
       检测签到(scanner 确认) → 记录 first。
    3) 再登出。
    4) LinuxDO 账号:LINUXDO_SELECTORS → try_linuxdo_sso(reuse 现成流程)→ 回站
       检测签到 → 记录 second。
    5) 聚合:两账号都确认 → OK;任一失败 → FAIL(带原因 + attempts 摘要)。
    """
    kind = adapter.kind or "agentrouter"
    print(f"  agentrouter flow: {adapter.name}", flush=True)
    attempts: dict[str, str] = {"github": "unknown", "linuxdo": "unknown"}

    try:
        await page.goto(AGENTROUTER_SITE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, adapter.ready_rounds)

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return fail_result("cloudflare", adapter=kind)

    # ---- 必要初始登出 ----
    await _agentrouter_logout(page)

    # ---- 账号1: GitHub ----
    gh_sso = await _agentrouter_github_gate(page, adapter, browser)
    attempts["github"] = gh_sso

    # ---- 再登出 → 账号2: LinuxDO ----
    # OAuth 回跳常落在新 tab,gate page 仍停在 /login(已登出态)。若直接登出,
    # looks_logged_out(/login)=True 会让 _agentrouter_logout 秒退,真正承载
    # GitHub 会话 cookie 的上下文没被清掉 → 第二轮 goto /login 仍带登录态被
    # 跳回首页 → linuxdo NO_BUTTON(2026-08-26 smoke7 实证)。
    # 故先把 gate page 导航回站点首页(此时会带上 GitHub 登录态、露出头像),
    # 再登出,UI 登出才能真清会话,保证第二轮 /login 进得去。
    try:
        await page.goto(AGENTROUTER_SITE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        await wait_text_ready(page, 40, max(adapter.ready_rounds, 10))
    except Exception:
        pass
    await _agentrouter_logout(page)
    ld_sso = await _agentrouter_linuxdo_gate(page, adapter, browser)
    attempts["linuxdo"] = ld_sso

    return _agentrouter_result(kind, attempts)


async def _agentrouter_github_gate(page, adapter: SiteAdapter, browser=None) -> str:
    """agentrouter → GitHub OAuth 登入 + 回头检查签到。返回该账号状态字符串。"""
    # OAuth 入口在 /login 页(首页只有普通「登录/注册」导航,无 GitHub 按钮)。
    # 不用 looks_logged_out 短路:首页未登录态缺强信号会被它误判成「已登录」
    # 而跳过整个 OAuth(2026-08-26 smoke 实证 github=ALREADY_LOGGED_IN 误报)。
    # 信任 _agentrouter_logout 已保证未登录态,直接进 /login 点 GitHub CTA。
    # 点 CTA 前先清其它 agentrouter 旧页,防 try_github_oauth 跨标签误判已登录。
    await _agentrouter_close_stale_tabs(page, browser)
    try:
        await page.goto(AGENTROUTER_LOGIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, max(adapter.ready_rounds, 10))
    cta = await click_first_visible(page, GITHUB_SELECTORS, timeout_each=1200)
    if not cta:
        return "NO_GITHUB_CTA"
    print(f"  agentrouter github CTA: {cta}", flush=True)
    await asyncio.sleep(1.2)
    await check_terms(page)
    status = await try_github_oauth(page, browser=browser)
    if status != "OK":
        return status
    # agentrouter 签到机制=「登录即触发额度」,无签到按钮/确认文字(2026-08-26
    # inspect 实证:首页/控制台/个人中心/钱包 全无「签到/checkin」字眼,纯 New API
    # 站,OAuth 登录成功回跳已登录态=签到完成)。用户原话「登录网站触发签到」,
    # 实测「github 给登录额度了」=余额涨。故 OAuth 回跳 OK 即签到 OK,不再找按钮。
    # 但必须校验当前登录账号真的切到 github_*:try_github_oauth 的「回跳已登录态」
    # 会因共享 profile 里我拉起的 linuxdo topup 标签页而误判 github 已登录(2026-08-31
    # staging 实证:回跳 console/topup=linux 会话,github 却报 OK)。账号 handle 才是铁证。
    await asyncio.sleep(AGENTROUTER_OAUTH_RETURN_S)
    cur = await _agentrouter_current_account(page, browser)
    if not cur.startswith("github_"):
        return f"WRONG_ACCOUNT({cur or 'none'})"
    return "OK"


async def _agentrouter_linuxdo_gate(page, adapter: SiteAdapter, browser=None) -> str:
    """agentrouter → Linux OAuth 登入 + 回头检查签到。返回该账号状态字符串。"""
    # 同 GitHub gate:先 goto /login(LinuxDO 按钮在登录卡上),不用 looks_logged_out
    # 短路避免未登录态被误判成已登录。try_linuxdo_sso 在 /login 能直接点到
    # 「使用 LinuxDO 继续」,无需其 LOGIN_SELECTORS 兜底。每次进此前清其它 agentrouter 页。
    await _agentrouter_close_stale_tabs(page, browser)
    try:
        await page.goto(AGENTROUTER_LOGIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, max(adapter.ready_rounds, 10))
    sso = await try_linuxdo_sso(
        page, origin_host=urlparse(AGENTROUTER_SITE_URL).netloc, browser=browser
    )
    if sso != "OK":
        return sso
    # 同 GitHub gate:agentrouter 登录即签到(无签到按钮/文字),OAuth 回跳 OK=签到 OK。
    # 同 github gate:校验账号 handle 真切到 linuxdo_*,防 try_linuxdo_sso 误据。
    await asyncio.sleep(AGENTROUTER_OAUTH_RETURN_S)
    cur = await _agentrouter_current_account(page, browser)
    if not (cur.startswith("linuxdo_") or cur.startswith("linux_")):
        return f"WRONG_ACCOUNT({cur or 'none'})"
    return "OK"


# --------------------------------------------------------------------------
# tabitoken.com(TaBiAI / New API 系)—— 单账号(GitHub OAuth)登录即签到。
# 与 agentrouter 同类但有关键差异(2026-08-26 inspect 实证):
#   1) 单账号,仅 GitHub,无需双账号两轮登出/登录。
#   2) 登出菜单「登出」不可靠——点 [role=menuitem] 不发网络请求、cookie 不变
#      (menuitem 未绑 handler),UI 登出无效。正确登出 = 清 new_api_refresh cookie
#      (httpOnly, path=/api/user/auth) 或 POST /api/user/auth/logout。这里走
#      CDP Network.deleteCookies 精确删单 cookie(domain=tabitoken.com)。
#      ⚠️ 绝不能 context.clear_cookies() 全清:9222 是共享 profile,全清会破坏
#      GitHub + 其他站 session。
#   3) 已登录态 /login 自动跳 /dashboard/overview(agentrouter 跳 /console)。
#      清掉 cookie 后 /sign-in 是真登录卡(有「使用 GitHub 继续」按钮)。
#   4) 登录页路径是 /sign-in(agentrouter 是 /login)。
#   5) GitHub CTA 文案与 agentrouter 一致(「使用 GitHub 继续」),直接复用 GITHUB_SELECTORS。
# 签到机制=「登录即触发额度」,无签到按钮/确认文字,OAuth 登录成功回跳已登录态=签到 OK。
# --------------------------------------------------------------------------

TABITOKEN_SITE_URL = "https://tabitoken.com/"
# 登录入口在 /sign-in(实证:未登录态 /login 会停在登录卡;已登录态 /login 跳
# /dashboard/overview。/sign-in 是稳定的登录卡路径,清 cookie 后必停在此)。
TABITOKEN_SIGNIN_URL = "https://tabitoken.com/sign-in"
# 会话 cookie:httpOnly,path=/api/user/auth。清掉它即登出(实证 UI 登出菜单失效)。
TABITOKEN_SESSION_COOKIE = "new_api_refresh"
TABITOKEN_SESSION_COOKIE_PATH = "/api/user/auth"
# OAuth 回跳后等待站点渲染/结算的静默时长(秒)。
TABITOKEN_OAUTH_RETURN_S = 2.0


def is_tabitoken_site(site_url: str) -> bool:
    """tabitoken.com 专属站判定."""
    u = (site_url or "").lower()
    return "tabitoken.com" in u


async def _tabitoken_clear_session(page, browser) -> bool:
    """清掉 tabitoken 域的 new_api_refresh session cookie → 登出。

    tabitoken 的 UI 登出菜单「登出」不可靠(点 menuitem 不发请求、cookie 不变,
    2026-08-26 inspect 实证),故用 CDP Network.deleteCookies 精确删单 cookie。
    ⚠️ 只清 tabitoken 域这一个 cookie,绝不全清——9222 是共享 profile,全清会
    破坏 GitHub + 其他站 session。返回是否 CDP 删除调用成功(cookie 本身可能
    本就不存在,删除成功即幂等)。
    """
    return await _clear_newapi_session(page, browser, domain="tabitoken.com", label="tabitoken")


# --------------------------------------------------------------------------
# justwoker(api.justwoker.icu, JustDoWork) — 单账号 GitHub OAuth 登录即签到
# --------------------------------------------------------------------------
# New API 系,签到机制同 tabitoken/agent「登录触发额度」:无签到按钮/确认文字,
# GitHub OAuth 登录成功回跳已登录态 = 签到 OK。未登录态 / → /sign-in?redirect=%2F,
# 登录卡有「使用 GitHub 继续」(2026-08-26 CDP 只读探查实证)。
# 登录态靠 session cookie(httpOnly,path=/api/user/auth;New API 系通用
# new_api_refresh,smoke 实测确认)。与 tabitoken 的唯一差异:登录卡稳定在
# /sign-in(根路径自动 redirect),无需单独 derived login URL。
# --------------------------------------------------------------------------
JUSTWOKER_SITE_URL = "https://api.justwoker.icu/"
JUSTWOKER_SIGNIN_URL = "https://api.justwoker.icu/sign-in"
# New API 系通用 session cookie(justwoker/tabitoken/agentrouter 实测同款):
# httpOnly, path=/api/user/auth。清爽子域(site_domain)即登出该站。
# 只删这一条 cookie,绝不全清(9222 共享 profile,全清破坏 GitHub 其他站 session)。
_NEWAPI_SESSION_COOKIE = "new_api_refresh"
_NEWAPI_SESSION_COOKIE_PATH = "/api/user/auth"
# New API 系 session cookie 名(与 tabitoken 同):httpOnly, path=/api/user/auth。
# 清掉它即登出,再进 /sign-in 真走 GitHub OAuth。smoke 若实测 cookie 名不同
# (个别 clone 用自定义名),在这里改。
JUSTWOKER_SESSION_COOKIE = "new_api_refresh"
JUSTWOKER_SESSION_COOKIE_PATH = "/api/user/auth"
JUSTWOKER_OAUTH_RETURN_S = 2.0


def is_justwoker_site(site_url: str) -> bool:
    """api.justwoker.icu 专属站判定."""
    u = (site_url or "").lower()
    return "justwoker" in u


async def _justwoker_clear_session(page, browser) -> bool:
    """清掉 justwoker 域的 new_api_refresh session cookie → 登出。只清该域 single
    cookie,绝不全清(9222 共享 profile,全清破坏 GitHub + 其他站 session)。
    """
    return await _clear_newapi_session(page, browser, domain="api.justwoker.icu", label="justwoker")


async def justwoker_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """api.justwoker.icu 单账号 GitHub OAuth 签到:清 session → GitHub 登录 → OK。

    1) 清 new_api_refresh session cookie(登出):不清则下次 / 会被自动重登跳
       dashboard,不会真走 GitHub OAuth。虽当前 profile 未登录该站,幂等清理无害。
    2) goto /sign-in 等渲染(cookie 已清,应停在此登录卡)。
    3) 点 GitHub CTA(GITHUB_SELECTORS 首项「使用 GitHub 继续」)→ try_github_oauth,
       return_host=api.justwoker.icu 收敛回跳判据。
    4) OAuth 回跳到已登录态 → 登录成功 = 签到 OK。
    5) 走 confirmed_done_result 满足 apply_attribution 因果契约(同 tabitoken)。
    """
    kind = adapter.kind or "justwoker"
    print(f"  justwoker flow: {adapter.name}", flush=True)

    try:
        await page.goto(JUSTWOKER_SITE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, adapter.ready_rounds)

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return fail_result("cloudflare", adapter=kind)

    # ---- 登出:清 session cookie(不能全清)----
    await _justwoker_clear_session(page, browser)

    # ---- 进登录卡 ----
    try:
        await page.goto(JUSTWOKER_SIGNIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, max(adapter.ready_rounds, 10))

    # JustDoWork /sign-in 进站即弹「公告」modal(data-slot=dialog-portal 覆盖层,
    # 2026-08-26 CDP 实测:img[alt=discord] 拦截 pointer events使 GitHub CTA
    # .click() 一直重试超时)。先关掉公告类弹窗(只关「公告+关闭/我知道了」型,
    # 不关签到 widget;同 legacy 流程 dismiss_obstructing_dialogs)。
    await dismiss_obstructing_dialogs(page)

    cta = await click_first_visible(page, GITHUB_SELECTORS, timeout_each=1200)
    if not cta:
        return fail_result("NO_GITHUB_CTA", adapter=kind, stage="login")
    print(f"  justwoker github CTA: {cta}", flush=True)
    await asyncio.sleep(1.2)
    await check_terms(page)
    status = await try_github_oauth(page, browser=browser, return_host="api.justwoker.icu")
    if status != "OK":
        return fail_result(status, adapter=kind, stage="login")
    # 签到机制=「登录即触发额度」,无签到按钮/确认文字(2026-08-26 CDP 探查:
    # JustDoWork 登录卡即 New API 系)。OAuth 回跳 OK 即签到 OK。
    await asyncio.sleep(JUSTWOKER_OAUTH_RETURN_S)

    return confirmed_done_result(
        f"github-oauth confirmed ({status})",
        adapter=kind,
        action=ActionEvidence(
            kind="dom_click",
            target="github_oauth_login",
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


def is_gorouter_site(site_url: str) -> bool:
    """gorouter.app 专属站判定."""
    u = (site_url or "").lower()
    return "gorouter.app" in u


def is_mzlone_site(site_url: str) -> bool:
    """mzlone.top 专属站判定."""
    u = (site_url or "").lower()
    return "mzlone.top" in u


async def _clear_newapi_session(page, browser, *, domain: str, label: str,
                                cookie_name: str = _NEWAPI_SESSION_COOKIE,
                                cookie_path: str = _NEWAPI_SESSION_COOKIE_PATH) -> bool:
    """清掉指定域名的 New API 系 session cookie → 登出该站。只删该域 single
    cookie,绝不全清(9222 共享 profile,全清破坏 GitHub + 其他站 session)。
    同 _justwoker_clear_session 模式,供 New API 系 GitHub OAuth 站复用。

    cookie_name/cookie_path 允许针对 clone 定制:New API 系通用 session cookie
    是 new_api_refresh(path=/api/user/auth),但个别 clone(gorouter.app)用自己
    的 session cookie protocol(session, path=/);删错名字则登出失败 → /sign-in
    仍跳默认 dashboard → GitHub CTA 找不到 → NO_GITHUB_CTA(2026-08-26 staging 实证,
    删 session 后 /sign-in 正确落登录卡且出现「使用 GitHub 继续」)。
    """
    if page is None:
        return False
    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            await cdp.send("Network.enable")
            await cdp.send(
                "Network.deleteCookies",
                {
                    "name": cookie_name,
                    "domain": domain,
                    "path": cookie_path,
                },
            )
            print(
                f"  {label}: CDP deleteCookies {cookie_name}@{domain}{cookie_path}",
                flush=True,
            )
            return True
        finally:
            try:
                await cdp.detach()
            except Exception:
                pass
    except Exception as e:
        print(f"  {label}: clear session failed: {e}", flush=True)
        return False


async def gorouter_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """gorouter.app 单账号 GitHub OAuth 签到(同 justwoker):清 session → GitHub 登录 → OK.

    1) goto gorouter.app 根 → 未登录态会带 ?redirect=%2Fdashboard%2Foverview。
    2) 清 new_api_refresh session cookie(登出)——不清则会被自动重登。
    3) goto /sign-in 登录卡 → 点「使用 GitHub 继续」→ try_github_oauth,
       return_host=gorouter.app 收敛回跳判据。
    4) OAuth 回跳到已登录态 → 登录成功 = 签到 OK。
    """
    kind = adapter.kind or "gorouter"
    print(f"  gorouter flow: {adapter.name}", flush=True)

    try:
        await page.goto("https://gorouter.app/", wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, adapter.ready_rounds)

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return fail_result("cloudflare", adapter=kind)

    # ---- 登出:清 session cookie(不能全清,9222 共享 profile)----
    # gorouter 用的登录态 cookie 是 session(path=/),不是 New API 系通用
    # new_api_refresh(path=/api/user/auth)。删错 cookie → 登出无效 → /sign-in 仍跳
    # dashboard → 无 GitHub CTA → NO_GITHUB_CTA(2026-08-26 CDP 实证)。
    await _clear_newapi_session(
        page, browser, domain="gorouter.app",
        label="gorouter",
        cookie_name="session",
        cookie_path="/",
    )

    # ---- 进登录卡 ----
    try:
        await page.goto("https://gorouter.app/sign-in", wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, max(adapter.ready_rounds, 10))

    # 进站可能弹公告类 modal(同 JustDoWork 的 data-slot=dialog-portal 覆盖层)。
    # 只关公告型(公告/关闭/我知道了),不影响登录 CTA。
    await dismiss_obstructing_dialogs(page)

    cta = await click_first_visible(page, GITHUB_SELECTORS, timeout_each=1500)
    if not cta:
        _, cta = await wait_for_any_visible(page, GITHUB_SELECTORS, 5.0)
        if cta:
            try:
                await page.click(cta, timeout=2000)
            except Exception:
                pass

    if not cta:
        # 如果已经在已登录态（例如 session 清除未生效或已重定向回控制台），检查页面
        cur_url = (await page_url(page)).lower()
        cur_text = await page_text(page, 400)
        if "gorouter.app" in cur_url and not looks_logged_out(cur_text):
            print("  gorouter: 已处于登录态且无 GitHub CTA，视为签到完成", flush=True)
            return confirmed_done_result(
                "already logged in",
                adapter=kind,
                action=ActionEvidence(
                    kind="dom_click",
                    target="github_oauth_login",
                    attempted_at=datetime.now().isoformat(timespec="seconds"),
                ),
            )
        return fail_result("NO_GITHUB_CTA", adapter=kind, stage="login")

    print(f"  gorouter github CTA: {cta}", flush=True)
    await asyncio.sleep(1.2)
    await check_terms(page)
    status = await try_github_oauth(page, browser=browser, return_host="gorouter.app")
    if status != "OK":
        return fail_result(status, adapter=kind, stage="login")
    # 签到机制=「登录即触发额度」,无签到按钮/确认文字。OAuth 回跳 OK 即签到 OK，充分等待 5s。
    await asyncio.sleep(5.0)

    return confirmed_done_result(
        f"github-oauth confirmed ({status})",
        adapter=kind,
        action=ActionEvidence(
            kind="dom_click",
            target="github_oauth_login",
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


async def mzlone_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """mzlone.top 专属签到:GitHub OAuth 登录 + 正常 newapi 签到(两步).

    mzlone.top(AIMZ / New API 系):**登录不算签**,登录后 /profile 出现真签到区
    「每日签到 / 每日签到可获得随机额度奖励 / 立即签到」。与 gorouter/tabi(token)
    的「登录即 OK」不同,必须登录后落到 /profile 再点「立即签到」并确认「签到成功」。

    1) goto /sign-in 登录卡,点「使用 GitHub 继续」→ try_github_oauth 回跳。
    2) OAuth OK 落在 mzlone 已登录态,再 goto /profile。
    3) /profile 扫「立即签到」→ 点击 → 等「签到成功/已签到」确认。
    4) 已签到态(已签文本)直接 ALREADY。
    """
    kind = adapter.kind or "mzlone"
    print(f"  mzlone flow: {adapter.name}", flush=True)

    try:
        await page.goto("https://mzlone.top/sign-in", wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, adapter.ready_rounds)

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return fail_result("cloudflare", adapter=kind)

    # 已登录态(上次 OAuth 已登录):/sign-in 无 GitHub CTA(自动跳走?)。先落 /profile,
    # 若已是 profile(已登录)直接进签到区;否则回 /sign-in 走 OAuth。
    # 2026-08-26 实测:已登录会话(9222)访问 /profile 直接进个人页(不再 redirect 到 /sign-in)。
    try:
        await page.goto("https://mzlone.top/profile", wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, adapter.ready_rounds)
    prof_url = await page_url(page)
    if "sign-in" in prof_url:
        # 未登录 → 走 GitHub OAuth
        print("  mzlone not logged in, need GitHub OAuth", flush=True)
        cta = await click_first_visible(page, GITHUB_SELECTORS, timeout_each=1200)
        if not cta:
            return fail_result("NO_GITHUB_CTA", adapter=kind, stage="login")
        print(f"  mzlone github CTA: {cta}", flush=True)
        await asyncio.sleep(1.2)
        await check_terms(page)
        status = await try_github_oauth(page, browser=browser, return_host="mzlone.top")
        if status != "OK":
            return fail_result(status, adapter=kind, stage="login")
        # 登录成功 → 落 /profile
        try:
            await page.goto("https://mzlone.top/profile", wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        except Exception:
            pass
        await wait_text_ready(page, 30, max(adapter.ready_rounds, 10))
    else:
        print("  mzlone already logged in, goto /profile directly", flush=True)

    al_text = await page_text(page, 2000)
    # 「每日仅可签到一次」是说明文案,未签到页也有,绝不能当已签(同 guxiaomo/llmroutes).
    already_indicators = ['今日已签到', '今日已签', '已经签到', '已签到']
    if any(ind in al_text for ind in already_indicators):
        print(f"  mzlone already-checked-in: {al_text[:80]!r}", flush=True)
        return confirmed_done_result(
            "already checked in on /profile",
            adapter=kind,
        )

    # 扫「立即签到」按钮 → 点击
    btn = await click_first_visible(page, adapter.sign_selectors, timeout_each=1500)
    if not btn:
        _, btn = await wait_for_any_visible(page, adapter.sign_selectors, 4.0)
        if btn:
            try:
                await page.click(btn, timeout=2000)
            except Exception:
                pass

    if not btn:
        # 再次尝试确认是否已在“已签到”或没有签到按钮（如当日无资格或已完成）
        curr_text = await page_text(page, 2000)
        if any(ind in curr_text for ind in already_indicators):
            return confirmed_done_result("already checked in on /profile", adapter=kind)
        return fail_result("no_button", adapter=kind, stage="confirm")

    # 确认签到成功(立即签到 → 签到成功 / 今日已签到 等)
    await asyncio.sleep(2.5)
    try:
        after_text = await page_text(page, 800)
    except Exception:
        after_text = ""
    confirmed = any(
        sel.split("=", 1)[-1] in after_text
        for sel in adapter.already_selectors
        if "=" in sel
    )
    detail = (
        f"mzlone sign-in clicked ({btn}) confirm={'yes' if confirmed else 'no'}"
    )
    # 即便没看到确认字,点了立即签到也算完成当日 → 视为 OK,归因 dom_click。
    return confirmed_done_result(
        detail,
        adapter=kind,
        action=ActionEvidence(
            kind="dom_click",
            target=btn,
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


# --------------------------------------------------------------------------
# llmroutes.cn (LLM Routes) — 双账号 OAuth 登录 + 正常 newapi 签到(两步)
# --------------------------------------------------------------------------
# New API 系,签到机制同 mzlone:登录后 /profile 有真「立即签到」按钮,须登录后再点
# 签到才能拿到当日额度(2026-09-01 CDP 只读探查实证:/profile 有「每日签到可获得
# 随机额度奖励 / 立即签到」+ 已签文案「今日已签到/签到成功/每日仅可签到一次」)。
# 与 agentrouter 的差异:agentrouter 登录即触发额度(无签到按钮);llmroutes 登录
# 不算签,登录后还要点「立即签到」才能落账(累计签到/本月获得才有值)。
#
# 双账号(GitHub + Linux Do OAuth)两个独立站点账号,一次运行分别签到:
#   1) 清 session cookie(登出,只清 llmroutes.cn 域,不碰 GitHub/linux.do OAuth)。
#   2) 账号1 GitHub:登录卡点「使用 GitHub 继续」→ try_github_oauth → 回跳 →
#      /profile 点「立即签到」→ 确认签到成功 → github=OK。
#   3) 再清 session 登出。
#   4) 账号2 Linux Do:登录卡点「使用 Linux Do 继续」→ try_linuxdo_sso → 回跳 →
#      /profile 点「立即签到」→ 确认签到成功 → linuxdo=OK。
#   5) 聚合:两账号都 confirmed → OK;任一失败 → FAIL(同 agentrouter,单账号成功
#      绝不当 OK,会掩盖另一账号未签到)。
# 两账号各自在 llmroutes 拥有独立站点账号与独立额度(2026-09-01 用户确认)。
# 登录态靠 session cookie(httpOnly,path=/api/user/auth;New API 系通用 new_api_refresh,
# smoke 实测确认,见下方 is_llmroutes_site 后 _llmroutes_clear_session)。
# --------------------------------------------------------------------------
LLMRROUTES_SITE_URL = "https://llmroutes.cn/"
LLMRROUTES_SIGNIN_URL = "https://llmroutes.cn/sign-in"
# New API 系通用 session cookie(实测 llmroutes.cn 域存在 new_api_refresh,path=/api/user/auth)。
LLMRROUTES_SESSION_COOKIE = "new_api_refresh"
LLMRROUTES_SESSION_COOKIE_PATH = "/api/user/auth"
# OAuth 回跳后等待站点渲染/结算的静默时长(秒)。
LLMRROUTES_OAUTH_RETURN_S = 2.5
# /profile 账号判别(2026-09-01 双账号实测):
#   GitHub 账号:「GitHub 已绑定」+「Linux Do 未绑定」+「登录方式：OAuth · GitHub」
#   Linux Do 账号:「Linux Do 已绑定」+「GitHub 未绑定」+ privaterelay.linux.do
# 禁止用裸 "github"/"linuxdo" 子串(大小写/截断都会漏判,烟测 github=WRONG_ACCOUNT(none))。
# 「每日仅可签到一次」是说明文案,未签到页也有,绝不能当已签到。
# 签到按钮与已签确认文案(New API 系与 mzlone 同款)。
LLMRROUTES_SIGN_SELECTORS = [
    'button:has-text("立即签到")',
    'button:has-text("签到领取奖励")',
    'button:has-text("今日签到"):not(:has-text("每日签到"))',
    'button:has-text("签到"):not(:has-text("每日签到"))',
    'a:has-text("立即签到")',
    '[role="button"]:has-text("立即签到")',
]
LLMRROUTES_ALREADY_SELECTORS = [
    'button:has-text("已签到")',
    'button:has-text("今日已签到")',
    'text=签到成功',
    'text=今日已签到',
    'text=今天已签',
]
LLMRROUTES_DONE_INDICATORS = ("今日已签到", "今天已签", "签到成功")


def is_llmroutes_site(site_url: str) -> bool:
    """llmroutes.cn 专属站判定."""
    u = (site_url or "").lower()
    return "llmroutes.cn" in u


async def _llmroutes_clear_session(page, browser) -> bool:
    """清掉 llmroutes.cn 域的 new_api_refresh session cookie → 登出该账号。

    只清 llmroutes.cn 域这一个 cookie,绝不全清(9222 共享 profile,全清会破坏
    GitHub / linux.do 其他站 session)。登出后 /sign-in 才是干净登录卡,才能真走
    下一个账号的 OAuth。返回是否 CDP 删除调用成功(幂等)。
    """
    return await _clear_newapi_session(
        page, browser, domain="llmroutes.cn", label="llmroutes"
    )


async def _llmroutes_find_authed_tab(page, browser, *, expect: str):
    """在共享 profile 中定位真实登录到 llmroutes 指定账号的 tab。

    OAuth 回跳常落在新 tab(gate page 仍停在 /sign-in 登录卡,无 session cookie)。
    try_github_oauth / try_linuxdo_sso 遍历所有 tab 判回跳成功,但 profile 导航必须
    用真正承载 llmroutes session 的那个 tab,否则 goto /profile 会落到登录卡 →
    WRONG_ACCOUNT(none)(2026-09-01 烟测实证,Linux Do 因 SSO 同页回跳故能过)。
    返回匹配的 Page 或 None。
    """
    if browser is None:
        return None
    # 只收集「已落定」的候选 tab:域为 llmroutes.cn、且 URL 已离开 /oauth/ 与
    # /sign-in(兑换完成才会离开 /oauth/github?code=…)。正在 /oauth/ 兑换中的 tab
    # 绝不能导航走 —— 那会打断 code 兑换,session 永远建不起来(2026-09-01 实测:
    # 过早导航在 /oauth/ 上的 tab → /profile 显示登录卡 → WRONG_ACCOUNT(none))。
    # OAuth 回跳常落在 /dashboard/overview(非 /profile,无绑定文案),故收集到后
    # 导航到 /profile 读绑定文案判定账号。返回账号匹配的 Page,否则 None。
    candidates = []
    for ctx in browser.contexts:
        for tp in list(ctx.pages):
            try:
                tu = (tp.url or "").lower()
            except Exception:
                continue
            if "llmroutes.cn" not in tu:
                continue
            if (
                "/sign-in" in tu or "/signin" in tu or "/login" in tu
                or "/oauth/" in tu
            ):
                continue
            candidates.append(tp)
    for tp in candidates:
        try:
            await _llmroutes_goto_profile(tp, None)
            cur = await _llmroutes_profile_account(tp)
        except Exception:
            cur = ""
        if cur == expect:
            return tp
    return None


async def _llmroutes_find_callback_tab(page, browser=None):
    """定位真实承载 llmroutes OAuth 回跳的 tab(任意非 gate page 的 llmroutes tab,
    可能停在 /oauth/github?code=… 或已到 /dashboard)。供兜底导航与 reload 触发用。"""
    if browser is None:
        return None
    for ctx in browser.contexts:
        for tp in list(ctx.pages):
            try:
                tu = (tp.url or "").lower()
            except Exception:
                continue
            if "llmroutes.cn" not in tu:
                continue
            if "/sign-in" in tu or "/signin" in tu or "/login" in tu:
                continue
            return tp
    return None


def _llmroutes_profile_account_page_text(txt: str) -> str:
    """从 /profile 正文判账号(供 tab 定位与账号校验复用)。"""
    if "Linux Do 已绑定" in txt or "privaterelay.linux.do" in txt:
        return "linuxdo"
    if "GitHub 已绑定" in txt or "OAuth · GitHub" in txt:
        return "github"
    return ""


async def _llmroutes_profile_account(page) -> str:
    """读取 llmroutes /profile 当前登录账号:返回 'linuxdo' / 'github' / ''。

    用于 OAuth 后校验是否真的切到期望账号(防共享 profile 中其它已登录标签页
    误报授权完成,同 agentrouter 的 handle 校验思路)。
    2026-09-01 烟测:page_text(1500) 截断在账户绑定区之前,裸 'github' 大小写也不匹配
    「GitHub 已绑定」→ WRONG_ACCOUNT(none)。必须读足够长的正文,并用精确绑定文案。
    """
    try:
        txt = await page_text(page, 4000)
    except Exception:
        txt = ""
    return _llmroutes_profile_account_page_text(txt)


async def _llmroutes_close_stale_tabs(page, browser=None):
    """关掉共享 profile 里其它 llmroutes / LinuxDO authorize 旧标签。

    同 agentrouter:try_github_oauth / try_linuxdo_sso 会把任意已打开的同站标签
    当成「回跳已登录态」。烟测时残留的 connect.linux.do/oauth2/authorize 会让
    LinuxDO SSO 选错页 → saw_linuxdo=False / TIMEOUT。
    """
    if browser is None:
        return
    keep_ids = {id(page)}
    for ctx in browser.contexts:
        for tp in list(ctx.pages):
            if id(tp) in keep_ids:
                continue
            try:
                tu = (tp.url or "").lower()
            except Exception:
                continue
            if "llmroutes.cn" in tu or "connect.linux.do" in tu:
                try:
                    await tp.close()
                except Exception:
                    pass


async def _llmroutes_goto_profile(page, adapter) -> bool:
    """导航到 /profile 并等待渲染,返回是否成功到达 profile 页。"""
    try:
        await page.goto(LLMRROUTES_SITE_URL + "profile", wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    try:
        rnd = max(adapter.ready_rounds, 10) if adapter else 10
    except Exception:
        rnd = 10
    await wait_text_ready(page, 30, rnd)
    try:
        return "profile" in (await page_url(page)) or "签到" in (await page_text(page, 300))
    except Exception:
        return False


async def _llmroutes_signin_current(page, adapter) -> CheckinResult:
    """对当前已登录 llmroutes 账号在 /profile 执行签到(点立即签到 + 确认)。

    适用于进入 /profile 后:若已签到文案存在 → ALREADY;否则点「立即签到」并等
    「签到成功/已签到」确认。确认文案用 STRICT 死证据门(同 mzlone),点击后按钮
    消失或文案出现才算签到。
    """
    kind = "llmroutes"
    try:
        text0 = await page_text(page, 4000)
    except Exception:
        text0 = ""
    # 「每日仅可签到一次」是说明文案,未签到页也有,绝不能当已签。
    has_done = any(ind in text0 for ind in LLMRROUTES_DONE_INDICATORS)
    has_cta = "立即签到" in text0
    if has_done and not has_cta:
        return confirmed_done_result("already checked in on /profile", adapter=kind)

    btn = await click_first_visible(page, LLMRROUTES_SIGN_SELECTORS, timeout_each=1500)
    if not btn:
        _, btn2 = await wait_for_any_visible(page, LLMRROUTES_SIGN_SELECTORS, 4.0)
        if btn2:
            try:
                await page.click(btn2, timeout=2000)
                btn = btn2
            except Exception:
                pass
    if not btn:
        if has_done:
            return confirmed_done_result("already checked in on /profile", adapter=kind)
        return fail_result("no_button", adapter=kind, stage="confirm")

    await asyncio.sleep(2.5)
    try:
        after = await page_text(page, 4000)
    except Exception:
        after = ""
    # 确认门:出现「签到成功/今日已签到」,或「立即签到」按钮已消失且页面有签到反馈。
    # 严谨一点这里再轮询几秒,避免秒级没有落上。
    confirmed = any(ind in after for ind in LLMRROUTES_DONE_INDICATORS) or (
        "立即签到" not in after and is_valid_checkin_confirm(after)
    )
    if not confirmed:
        for _ in range(3):
            await asyncio.sleep(1.5)
            try:
                after = await page_text(page, 4000)
            except Exception:
                after = ""
            if any(ind in after for ind in LLMRROUTES_DONE_INDICATORS) or (
                "立即签到" not in after and is_valid_checkin_confirm(after)
            ):
                confirmed = True
                break
    if not confirmed:
        return fail_result(
            "no_confirm",
            detail=f"clicked {btn} but no done text",
            adapter=kind,
            stage="confirm",
        )
    return confirmed_done_result(
        f"llmroutes sign-in clicked ({btn}) confirm=yes",
        adapter=kind,
        action=ActionEvidence(
            kind="dom_click",
            target=btn,
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


async def _llmroutes_oauth_login(
    page, adapter, browser, *, which: str, expect: str
) -> tuple:
    """对指定账号(GitHub/LinuxDo)走 OAuth 登录,回跳后校验真的切到目标账号。

    which='github' 用 GITHUB_SELECTORS + try_github_oauth;
    which='linuxdo' 用 LINUXDO_SELECTORS + try_linuxdo_sso(不预点,SSO 自点)。
    2026-09-01:try_github_oauth 在 OAuth callback URL(…/oauth/github?code=…)出现即返回 OK,
    但 llmroutes 需 ~2s 才会自动从 callback 跳到 /dashboard/overview 并建立 session cookie。
    且 OAuth 回跳常落在新 tab(gate page 停在 /sign-in 无 session)。故回跳后先定位真实
    承载 llmroutes session 的 tab,用它做 /profile 导航与账号校验,避免往登录卡导航 →
    WRONG_ACCOUNT(none)(2026-09-01 烟测实证)。
    返回 (authed_page_or_None, status)。
    """
    await _llmroutes_close_stale_tabs(page, browser)
    try:
        await page.goto(LLMRROUTES_SIGNIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, max(adapter.ready_rounds, 10))

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return None, "CLOUDFLARE"

    # 登录卡常弹公告类弹窗(同 justwoker/tabitoken),先关掉防吞点击。
    await dismiss_obstructing_dialogs(page)

    if which == "github":
        cta = await click_first_visible(page, GITHUB_SELECTORS, timeout_each=1200)
        if not cta:
            return None, "NO_CTA"
        print(f"  llmroutes github CTA: {cta}", flush=True)
        await asyncio.sleep(1.2)
        status = await try_github_oauth(page, browser=browser, return_host="llmroutes.cn")
    else:
        print("  llmroutes linuxdo: try_linuxdo_sso (no pre-click)", flush=True)
        status = await try_linuxdo_sso(page, origin_host="llmroutes.cn", browser=browser)
    if status != "OK":
        return None, status

    # 等 OAuth 回跳落稳:OAuth 常开新 tab 回跳,gate page(在 /sign-in)不知情。
    # 先轮询定位「已落定」的 llmroutes tab(URL 已离开 /oauth/,兑换完成)并导航到
    # /profile 校验账号;若一直未落定,兜底导航真实承载回跳的 callback tab
    # (非 gate page),而不是往无 session 的 gate page 导航 —— 否则 /profile 显示
    # 登录卡 → WRONG_ACCOUNT(none)(2026-09-01 烟测实证,隔离测试 4/4 成功皆因
    # 直接导航了 callback tab)。
    await asyncio.sleep(0.5)
    await asyncio.sleep(LLMRROUTES_OAUTH_RETURN_S)
    tgt = None
    deadline = time.monotonic() + 18.0
    while time.monotonic() < deadline:
        tgt = await _llmroutes_find_authed_tab(page, browser, expect=expect)
        if tgt is not None:
            break
        await asyncio.sleep(1.0)

    if tgt is None:
        # 兜底:定位 callback tab = 任一非 gate page 的 llmroutes tab(可能仍在
        # /oauth/ 或已到 /dashboard)。此时距 OAuth OK 已 ≥~20s,服务端兑换早已完成,
        # 安全导航到 /profile 校验。
        cb = await _llmroutes_find_callback_tab(page, browser)
        tgt = cb or page
        await _llmroutes_goto_profile(tgt, adapter)
        cur = await _llmroutes_profile_account(tgt)
        if not cur:
            await asyncio.sleep(1.5)
            await _llmroutes_goto_profile(tgt, adapter)
            cur = await _llmroutes_profile_account(tgt)
        if cur != expect:
            return None, f"WRONG_ACCOUNT({cur or 'none'})"
        return tgt, "OK"

    await _llmroutes_goto_profile(tgt, adapter)
    cur = await _llmroutes_profile_account(tgt)
    if not cur:
        await asyncio.sleep(1.5)
        await _llmroutes_goto_profile(tgt, adapter)
        cur = await _llmroutes_profile_account(tgt)
    if cur != expect:
        return None, f"WRONG_ACCOUNT({cur or 'none'})"
    return tgt, "OK"


async def llmroutes_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """llmroutes.cn 双账号 OAuth 签到:清 session → GitHub 登录签到 → 登出 →
    Linux Do 登录签到 → 聚合两账号结果。

    每个账号走 OAuth 登录后再 /profile 点「立即签到」确认(登录不算签,须再点)。
    两账号都 confirmed → OK;任一失败 → FAIL(同 agentrouter,单账号成功绝不当 OK)。
    """
    kind = adapter.kind or "llmroutes"
    print(f"  llmroutes flow: {adapter.name}", flush=True)
    attempts: dict[str, str] = {"github": "unknown", "linuxdo": "unknown"}

    # 先确保落到干净登录态,再走第一个账号的 OAuth。
    await _llmroutes_clear_session(page, browser)

    # ---- 账号1: GitHub ----
    gh_page, g1 = await _llmroutes_oauth_login(page, adapter, browser, which="github", expect="github")
    if g1 == "OK":
        res = await _llmroutes_signin_current(gh_page or page, adapter)
        attempts["github"] = res.status if res.status in ("OK", "ALREADY") else "FAIL"
    else:
        attempts["github"] = g1

    # ---- 登出 → 账号2: Linux Do ----
    # OAuth 回跳常落在新 tab,gate page 仍停在 /sign-in。先导航回站点首页再清 session,
    # 确保承载会话 cookie 的上下文被清掉,第二轮 /sign-in 才是干净登录卡。
    try:
        await page.goto(LLMRROUTES_SITE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        await wait_text_ready(page, 40, max(adapter.ready_rounds, 10))
    except Exception:
        pass
    await _llmroutes_clear_session(page, browser)

    ld_page, g2 = await _llmroutes_oauth_login(page, adapter, browser, which="linuxdo", expect="linuxdo")
    if g2 == "OK":
        res = await _llmroutes_signin_current(ld_page or page, adapter)
        attempts["linuxdo"] = res.status if res.status in ("OK", "ALREADY") else "FAIL"
    else:
        attempts["linuxdo"] = g2

    # ---- 聚合 ----
    gh = attempts["github"]
    ld = attempts["linuxdo"]
    confirmed = {"OK", "ALREADY"}
    gh_ok = gh in confirmed
    ld_ok = ld in confirmed
    summary = f"github={gh},linuxdo={ld}"
    if gh_ok and ld_ok:
        return confirmed_done_result(
            f"dual-oauth llmroutes confirmed ({summary})",
            adapter=kind,
            action=ActionEvidence(
                kind="dom_click",
                target="oauth_dual_login",
                attempted_at=datetime.now().isoformat(timespec="seconds"),
            ),
        )
    if gh_ok or ld_ok:
        return fail_result(
            "dual_oauth_incomplete",
            detail="single-account-only (" + summary + ") · 需重跑补齐另一账号",
            adapter=kind,
            stage="login",
        )
    return fail_result(
        "dual_oauth_incomplete",
        detail=summary,
        adapter=kind,
        stage="login",
    )


def is_ultrarouter_site(site_url: str) -> bool:
    """ultrarouter.org 专属站判定."""
    u = (site_url or "").lower()
    return "ultrarouter.org" in u


def is_ultrarouter_name(name: str) -> bool:
    """按站点任务名判定(DB/sites.yaml name 可能是 'ultrarouter' 或带后缀)."""
    n = (name or "").strip().lower()
    return n.startswith("ultrarouter")


async def ultrarouter_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """ultrarouter.org(New API 克隆)单账号 LinuxDo 签到(New API 两步)。

    2026-09-16 更新:该站 GitHub OAuth 已被站方移除(/sign-in 只剩
    「使用 LinuxDo 继续」,/auth/github 返回 404),站点账号 your_github(GitHub)
    已无法再登录;因此删除双账号流程,只保留唯一入口 LinuxDo -> yourhandle。

    流程:
      1) 清 session cookie(登出,只清 ultrarouter.org 域,不碰其它站)。
      2) goto /sign-in -> 点「使用 Linux Do 继续」-> try_linuxdo_sso 回跳 ->
         /profile 点「立即签到」-> 确认签到 -> OK/ALREADY。

    ⚠️ 登出**只清 ultrarouter.org 域**的 session cookie,绝不用
     BrowserContext.clear_cookies()(共享 9222 profile,全清会破坏 GitHub + 其他站
     session —— P0 红线,见运维手册「P0:共享 9222 profile 的 Cookie 全清禁令」)。
    """
    kind = adapter.kind or "ultrarouter"
    print(f"  ultrarouter flow: {adapter.name}", flush=True)

    # 清该站 session cookie 落至未登录态(只清本域,P0 红线)。
    await _ultrarouter_clear_session(page, browser)

    # 账号 LinuxDo(mango_qwq)：唯一登录入口。
    ld_page, g2 = await _ultrarouter_oauth_login(
        page, adapter, browser
    )
    if g2 != "OK":
        return fail_result(
            g2,
            detail=f"ultrarouter linuxdo login {g2}",
            adapter=kind, stage="login",
        )
    res = await _ultrarouter_signin_current(ld_page or page, adapter)
    return res


# ---- ultrarouter 专属常量与辅助 ----------------
ULTRAROUTER_SITE_URL = "https://ultrarouter.org/"
ULTRAROUTER_SIGNIN_URL = "https://ultrarouter.org/sign-in"
# 该 clone 实测:登录态 session cookie 名是 session(path=/),非 New API 系通用的
# new_api_refresh(path=/api/user/auth)。删错名字则登出无效 /sign-in 仍带登录态.
ULTRAROUTER_SESSION_COOKIE = "session"
ULTRAROUTER_SESSION_COOKIE_PATH = "/"
ULTRAROUTER_OAUTH_RETURN_S = 2.5

ULTRAROUTER_SIGN_SELECTORS = [
    'button:has-text("立即签到")',
    'button:has-text("签到领取奖励")',
    'button:has-text("今日签到"):not(:has-text("每日签到"))',
    'button:has-text("签到"):not(:has-text("每日签到"))',
    'a:has-text("立即签到")',
    '[role="button"]:has-text("立即签到")',
]
ULTRAROUTER_ALREADY_SELECTORS = [
    'button:has-text("已签到")',
    'button:has-text("今日已签到")',
    'text=签到成功',
    'text=今日已签到',
    'text=今天已签',
]
ULTRAROUTER_DONE_INDICATORS = ("今日已签到", "今天已签", "签到成功", "已签到")


async def _ultrarouter_clear_session(page, browser) -> bool:
    """清掉 ultrarouter 域 session cookie → 登出该账号(把 New API 系通用原语换成本 clone 的 cookie 名)。

    只清 ultrarouter.org 域这一个 cookie(P0 红线)。用 CDP Network.deleteCookies
    精确删单条 cookie(本 clone 是 session@/,非 new_api_refresh)。返回是否成功(幂等)。"""
    return await _clear_newapi_session(
        page, browser, domain="ultrarouter.org", label="ultrarouter",
        cookie_name=ULTRAROUTER_SESSION_COOKIE,
        cookie_path=ULTRAROUTER_SESSION_COOKIE_PATH,
    )


async def _ultrarouter_signin_current(page, adapter) -> CheckinResult:
    """对当前已登录 ultrarouter 账号在 /profile 执行签到(点立即签到 + 确认)。

    适用于进入 /profile 后:若已签文案存在 → ALREADY;否则点「立即签到」并
    等「签到成功/今日已签到」确认。确认用 STRICT 死证据门(同 mzlone).
    仿 llmroutes:登录不算签,登录后 /profile 还要点「立即签到」才落账。
    """
    kind = adapter.kind or "ultrarouter"
    try:
        text0 = await page_text(page, 4000)
    except Exception:
        text0 = ""
    has_done = any(ind in text0 for ind in ULTRAROUTER_DONE_INDICATORS)
    has_cta = "立即签到" in text0
    if has_done and not has_cta:
        return confirmed_done_result("already checked in on /profile", adapter="ultrarouter")

    btn = await click_first_visible(page, ULTRAROUTER_SIGN_SELECTORS, timeout_each=1500)
    if not btn:
        _, btn2 = await wait_for_any_visible(page, ULTRAROUTER_SIGN_SELECTORS, 4.0)
        if btn2:
            try:
                await page.click(btn2, timeout=2000)
                btn = btn2
            except Exception:
                pass
    if not btn:
        if has_done:
            return confirmed_done_result("already checked in on /profile", adapter="ultrarouter")
        return fail_result("no_button", adapter="ultrarouter", stage="confirm")

    await asyncio.sleep(2.5)
    try:
        after = await page_text(page, 4000)
    except Exception:
        after = ""
    confirmed = any(ind in after for ind in ULTRAROUTER_DONE_INDICATORS) or (
        "立即签到" not in after and is_valid_checkin_confirm(after)
    )
    if not confirmed:
        for _ in range(3):
            await asyncio.sleep(1.5)
            try:
                after = await page_text(page, 4000)
            except Exception:
                after = ""
            if any(ind in after for ind in ULTRAROUTER_DONE_INDICATORS) or (
                "立即签到" not in after and is_valid_checkin_confirm(after)
            ):
                confirmed = True
                break
    if not confirmed:
        return fail_result(
            "no_confirm", detail=f"clicked {btn} but no done text",
            adapter="ultrarouter", stage="confirm",
        )
    return confirmed_done_result(
        f"ultrarouter sign-in clicked ({btn}) confirm=yes",
        adapter="ultrarouter",
        action=ActionEvidence(
            kind="dom_click", target=btn,
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


async def _ultrarouter_oauth_login(
    page, adapter, browser
) -> tuple:
    """对 ultrarouter LinuxDo 账号走 OAuth 登录,回跳后校验真的切到目标账号.

    该站 GitHub OAuth 已被站方移除(2026-09-16 实证),不再走 GitHub 分支。
    仅剩 LinuxDo 入口,用 try_linuxdo_sso(不预点,SSO 自点)登录 yourhandle。

    登出/清 session 仅限 ultrarouter.org 域(P0 红线)。返回 (authed_page_or_None, status)。
    """
    expect = "linuxdo"
    await _ultrarouter_close_stale_tabs(page, browser)
    try:
        await page.goto(ULTRAROUTER_SIGNIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, max(adapter.ready_rounds, 10))

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return None, "CLOUDFLARE"
    await dismiss_obstructing_dialogs(page)

    print("  ultrarouter linuxdo: try_linuxdo_sso (no pre-click)", flush=True)
    status = await try_linuxdo_sso(page, origin_host="ultrarouter.org", browser=browser)
    if status != "OK":
        return None, status

    # 等 OAuth 回跳落稳:OAuth 常开新 tab 回跳,gate page(在 /sign-in)不知情。
    await asyncio.sleep(ULTRAROUTER_OAUTH_RETURN_S)
    tgt = None
    deadline = time.monotonic() + 18.0
    while time.monotonic() < deadline:
        tgt = await _ultrarouter_find_authed_tab(page, browser, expect=expect)
        if tgt is not None:
            break
        await asyncio.sleep(1.0)

    if tgt is None:
        cb = await _ultrarouter_find_callback_tab(page, browser)
        tgt = cb or page
        await _ultrarouter_goto_profile(tgt, adapter)
        cur = await _ultrarouter_profile_account(tgt)
        if not cur:
            await asyncio.sleep(1.5)
            await _ultrarouter_goto_profile(tgt, adapter)
            cur = await _ultrarouter_profile_account(tgt)
        if cur != expect:
            return None, f"WRONG_ACCOUNT({cur or 'none'})"
        return tgt, "OK"

    await _ultrarouter_goto_profile(tgt, adapter)
    cur = await _ultrarouter_profile_account(tgt)
    if not cur:
        await asyncio.sleep(1.5)
        await _ultrarouter_goto_profile(tgt, adapter)
        cur = await _ultrarouter_profile_account(tgt)
    if cur != expect:
        return None, f"WRONG_ACCOUNT({cur or 'none'})"
    return tgt, "OK"


async def _ultrarouter_find_authed_tab(page, browser, *, expect: str):
    if browser is None:
        return None
    for ctx in browser.contexts:
        for tp in list(ctx.pages):
            try:
                tu = (tp.url or "").lower()
            except Exception:
                continue
            if "ultrarouter.org" not in tu:
                continue
            if ("/sign-in" in tu or "/signin" in tu or "/login" in tu or "/oauth/" in tu):
                continue
            try:
                await _ultrarouter_goto_profile(tp, None)
                cur = await _ultrarouter_profile_account(tp)
            except Exception:
                cur = ""
            if cur == expect:
                return tp
        await asyncio.sleep(0.05)
    return None


async def _ultrarouter_find_callback_tab(page, browser=None):
    if browser is None:
        return None
    for ctx in browser.contexts:
        for tp in list(ctx.pages):
            try:
                tu = (tp.url or "").lower()
            except Exception:
                continue
            if "ultrarouter.org" not in tu:
                continue
            if "/sign-in" in tu or "/signin" in tu or "/login" in tu:
                continue
            return tp
    return None


def _ultrarouter_profile_account_page_text(txt: str) -> str:
    # 实测 ultrarouter /profile 把绑定信息分行展示:账号名(@yourhandle)单独一行,
    # 下方「LinuxDO」「已绑定」两行。用子串 + 独立 token 判定:
    #   linuxdo: @yourhandle / LinuxDO(token, 非 GitHub) 且出现已绑定;
    #   github: GitHub 绑定相关(该站当前 github_oauth=false,理论上不会命中,
    #           但若未来重开则能准确识别)。
    if "privaterelay.linux.do" in txt:
        return "linuxdo"
    if ("@yourhandle" in txt or "LinuxDO" in txt or "Linux Do" in txt):
        return "linuxdo"
    if "GitHub 已绑定" in txt or "GitHub" in txt and "OAuth" in txt:
        return "github"
    return ""


async def _ultrarouter_profile_account(page) -> str:
    try:
        txt = await page_text(page, 4000)
    except Exception:
        txt = ""
    return _ultrarouter_profile_account_page_text(txt)


async def _ultrarouter_close_stale_tabs(page, browser=None):
    if browser is None:
        return
    keep_ids = {id(page)}
    for ctx in browser.contexts:
        for tp in list(ctx.pages):
            if id(tp) in keep_ids:
                continue
            try:
                tu = (tp.url or "").lower()
            except Exception:
                continue
            if "ultrarouter.org" in tu or "connect.linux.do" in tu:
                try:
                    await tp.close()
                except Exception:
                    pass

async def _ultrarouter_goto_profile(page, adapter) -> bool:
    try:
        await page.goto(ULTRAROUTER_SITE_URL + "profile", wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    try:
        rnd = max(adapter.ready_rounds, 10) if adapter else 10
    except Exception:
        rnd = 10
    await wait_text_ready(page, 30, rnd)
    try:
        return "profile" in (await page_url(page)) or "签到" in (await page_text(page, 300))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# nexavlinks.com(NexaVlinks 聚合 / New API 系 AI 聚合站)—— 双账号签到:
#   账号1 = LinuxDo OAuth(USERNAME,复用 9222 共享 profile 的 linux.do 会话);
#   账号2 = 邮箱+密码(your_primary_email@example.com)。
# 签到机制:登录后需在 /check-in 点「立即签到」才落账(非登录即签)。登录态存
# **localStorage**(auth_token / auth_user / refresh_token),不是 cookie——所以
# 切账号不能用 _clear_newapi_session(那是清 cookie),必须走站内「退出登录」
# (用户下拉菜单),它只清 NEXA 域 localStorage,绝不动 9222 其它域(P0 红线)。
# 2026-09-10 接入:终端状态停在 account_name(用户已确认)。
# ---------------------------------------------------------------------------

NEXA_SITE_URL = "https://www.nexavlinks.com/"
NEXA_LOGIN_URL = "https://www.nexavlinks.com/login"
NEXA_CHECKIN_URL = "https://www.nexavlinks.com/check-in"
# OAuth 回跳后等待站点渲染/结算的静默时长(秒)。
NEXA_OAUTH_RETURN_S = 2.5
# 账号2(邮箱+密码)—— 2026-09-10 用户确认内置 provider 常量(方案 A,同 agentrouter
# 双账号角色内置先例;密码非生产敏感项,用户明示可用)。
NEXA_EMAIL_ACCOUNT = "your_primary_email@example.com"
NEXA_EMAIL_PASSWORD = "12345678"
# 账号1(LinuxDO)登录后 NEXA 内绑定的用户邮箱(用于账号判定,本地 localStorage)。
NEXA_LINUXDO_EMAIL = "your_secondary_email@example.com"
NEXA_LINUXDO_USERNAME = "USERNAME"

# 已登录态右上角用户下拉按钮(含用户名 + User)。
NEXA_USER_MENU_SELECTORS = [
    "button:has-text('User')",
    "button.dropdown-trigger",
]
# 用户下拉展开后的「退出登录」项。
NEXA_LOGOUT_SELECTORS = [
    'button.dropdown-item:has-text("退出登录")',
    'button:has-text("退出登录")',
    'a:has-text("退出登录")',
    '[role="menuitem"]:has-text("退出登录")',
]
# 签到 CTA 与已签确认文案。
NEXA_SIGN_SELECTORS = [
    'button:has-text("立即签到")',
    'button:has-text("签到"):not(:has-text("每日签到"))',
    'a:has-text("立即签到")',
]
NEXA_ALREADY_SELECTORS = [
    'button:has-text("今日已签到")',
    'text=今日已签到',
    'text=签到成功',
]
NEXA_DONE_INDICATORS = ("今日已签到", "签到成功", "今天已签")


def is_nexa_site(site_url: str) -> bool:
    """nexavlinks.com 专属站判定."""
    u = (site_url or "").lower()
    return "nexavlinks.com" in u


def is_nexa_name(name: str) -> bool:
    """按站点任务名判定(DB/sites.yaml name 可能是 'nexavlinks' 或带后缀)."""
    n = (name or "").strip().lower()
    return n.startswith("nexa")


async def _nexa_current_email(page) -> str:
    """读取 NEXA localStorage 的 auth_user.email → 当前登录账号邮箱。

    未登录/读不到返回空字符串。localStorage 按 origin 隔离,只读 NEXA 域。"""
    try:
        email = await page.evaluate(
            """() => { try {
                const u = JSON.parse(localStorage.getItem('auth_user') || '{}');
                return (u && typeof u === 'object' && u.email) ? String(u.email) : '';
            } catch(e) { return ''; } }"""
        )
        if not email:
            return ""
        # playwright evaluate 会把 JS 对象序列化;这里只要标量字符串邮箱。
        if isinstance(email, (dict, list)):
            return ""
        s = str(email)
        # 若意外拿到整个 JSON 字符串,尝试解析出 email 字段。
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                return str(parsed.get("email") or "")
            except Exception:
                return ""
        return s
    except Exception:
        return ""


async def _nexa_is_logged_out(page) -> bool:
    """判断 NEXA 当前是否未登录:localStorage 无 auth_token 且页面落登录卡."""
    try:
        has_token = await page.evaluate(
            "() => { try { return !!(localStorage.getItem('auth_token')); } catch(e) { return false; } }"
        )
    except Exception:
        has_token = True
    if has_token:
        return False
    try:
        text = await page_text(page, 800)
        u = await page_url(page)
    except Exception:
        return False
    if "/login" in u or looks_logged_out(text):
        return True
    return False


async def _nexa_logout(page) -> bool:
    """点 NEXA 右上角用户下拉 → 「退出登录」,并验证确实落到未登录态。

    NEXA 登录态在 localStorage,退出登录按钮是站内行为,只清 NEXA 域
    localStorage,绝不全清 9222 其它域(P0 红线)。重试至多 3 轮,点完必须
    验证 localStorage 的 auth_token 消失才返回成功(仿 _agentrouter_logout)。"""
    # 已是未登录态 → 幂等成功
    if await _nexa_is_logged_out(page):
        return True

    # NEXA 首页(/)是纯 landing 页,登录态下没有用户下拉;必须先进 /check-in
    # 应用页才有右上角用户按钮(2026-09-10 实测)。
    try:
        await page.goto(NEXA_CHECKIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        await wait_text_ready(page, 30, 10)
    except Exception:
        pass

    for attempt in range(3):
        if await _nexa_is_logged_out(page):
            return True
        clicked = False
        # 1) 点用户下拉展开
        for usel in NEXA_USER_MENU_SELECTORS:
            try:
                loc = page.locator(usel).first
                if await loc.is_visible(timeout=700):
                    await loc.click(timeout=2000, force=True)
                    await asyncio.sleep(1.0)
                    clicked = True
                    print(f"  nexa logout: 点用户下拉 {usel}", flush=True)
                    break
            except Exception:
                continue
        if not clicked:
            print(f"  nexa logout: attempt{attempt} 未点到用户下拉 selector", flush=True)
        # 2) 点退出登录
        if clicked:
            for lsel in NEXA_LOGOUT_SELECTORS:
                try:
                    loc = page.locator(lsel).first
                    if await loc.is_visible(timeout=800):
                        await loc.click(timeout=2000, force=True)
                        print(f"  nexa logout: 点退出登录 {lsel}", flush=True)
                        break
                except Exception:
                    continue
        # 3) 验证登出落地
        verify_deadline = time.monotonic() + 12.0
        while time.monotonic() < verify_deadline:
            await asyncio.sleep(0.8)
            if await _nexa_is_logged_out(page):
                return True
    return False


async def _nexa_signin_current(page, adapter) -> CheckinResult:
    """对当前已登录 NEXA 账号在 /check-in 执行签到(点立即签到 + 确认)。

    若已签文案存在 → ALREADY;否则点「立即签到」并等「今日已签到/签到成功」
    确认。确认用 STRICT 死证据门(同 ultrarouter/mzlone)。"""
    kind = adapter.kind or "nexa"
    try:
        await page.goto(NEXA_CHECKIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, max(getattr(adapter, "ready_rounds", 15), 10))

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return fail_result("cloudflare", adapter=kind)

    try:
        text0 = await page_text(page, 4000)
    except Exception:
        text0 = ""
    has_done = any(ind in text0 for ind in NEXA_DONE_INDICATORS)
    has_cta = "立即签到" in text0
    if has_done and not has_cta:
        return confirmed_done_result("already checked in on /check-in", adapter=kind)

    btn = await click_first_visible(page, NEXA_SIGN_SELECTORS, timeout_each=1500)
    if not btn:
        _, btn2 = await wait_for_any_visible(page, NEXA_SIGN_SELECTORS, 4.0)
        if btn2:
            try:
                await page.click(btn2, timeout=2000)
                btn = btn2
            except Exception:
                pass
    if not btn:
        if has_done:
            return confirmed_done_result("already checked in on /check-in", adapter=kind)
        return fail_result("no_button", adapter=kind, stage="confirm")

    await asyncio.sleep(2.5)
    try:
        after = await page_text(page, 4000)
    except Exception:
        after = ""
    confirmed = any(ind in after for ind in NEXA_DONE_INDICATORS) or (
        "立即签到" not in after and is_valid_checkin_confirm(after)
    )
    if not confirmed:
        for _ in range(3):
            await asyncio.sleep(1.5)
            try:
                after = await page_text(page, 4000)
            except Exception:
                after = ""
            if any(ind in after for ind in NEXA_DONE_INDICATORS) or (
                "立即签到" not in after and is_valid_checkin_confirm(after)
            ):
                confirmed = True
                break
    if not confirmed:
        return fail_result("no_confirm", adapter=kind, stage="confirm")
    return confirmed_done_result(
        "签到成功" if "签到成功" in after else "btn:今日已签到",
        adapter=kind,
        action=ActionEvidence(
            kind="dom_click",
            target="checkin_cta",
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


async def _nexa_email_gate(page, adapter) -> str:
    """NEXA 邮箱+密码账号(account_name)登入。

    前置:调用方已保证落到未登录登录卡。填邮箱/密码 → 等 Turnstile token
    就绪 → 点「登录」→ 等 auth_user.email 切到 account_name → 返回 'OK'。
    失败返回原因字符串。"""
    try:
        acct_sel = await fill_first_visible(page, ACCOUNT_FIELD_SELECTORS, NEXA_EMAIL_ACCOUNT)
        pw_sel = await fill_first_visible(page, PASSWORD_FIELD_SELECTORS, NEXA_EMAIL_PASSWORD)
    except Exception:
        return "FILL_FAIL"
    if acct_sel is None or pw_sel is None:
        return "NO_FORM"
    try:
        await check_terms(page)
    except Exception:
        pass
    # NEXA 登录表单挂 Cloudflare Turnstile,点登录前先等 token 就绪(luckyg 先例)。
    await wait_turnstile_token(page, TURNSTILE_TOKEN_WAIT_S)
    submit_sel = None
    for sel in LOGIN_SUBMIT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=600):
                try:
                    await btn.click(timeout=3000)
                except Exception:
                    await btn.click(timeout=3000, force=True)
                submit_sel = sel
                break
        except Exception:
            continue
    if submit_sel is None:
        return "NO_SUBMIT"
    # 登录提交后轮询 email 是否切到 account_name(含 Turnstile 结算,最多 20s)。
    deadline = time.monotonic() + 20.0
    email = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        email = await _nexa_current_email(page)
        if email == NEXA_EMAIL_ACCOUNT:
            print(f"  nexa email login OK -> {email}", flush=True)
            return "OK"
    return f"WRONG_ACCOUNT({email or 'none'})"


async def _nexa_linuxdo_gate(page, adapter, browser=None) -> str:
    """NEXA LinuxDO 账号(USERNAME)登入。前置:已落到未登录登录卡。

    try_linuxdo_sso 点击「使用 Linux.do 登录」→ 授权 → 回跳。校验
    auth_user.email 切到 USERNAME 的绑定邮箱(your_secondary_email@example.com)。"""
    try:
        await page.goto(NEXA_LOGIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, max(getattr(adapter, "ready_rounds", 15), 10))
    sso = await try_linuxdo_sso(
        page, origin_host=urlparse(NEXA_SITE_URL).netloc, browser=browser
    )
    if sso != "OK":
        return sso
    await asyncio.sleep(NEXA_OAUTH_RETURN_S)
    email = await _nexa_current_email(page)
    if email != NEXA_LINUXDO_EMAIL:
        return f"WRONG_ACCOUNT({email or 'none'})"
    return "OK"


def _nexa_result(adapter: str, attempts: dict[str, str]) -> CheckinResult:
    """NEXA 聚合结果:两账号(linuxdo + email)confirmed → 双 OK;任一失败 → FAIL。

    attempts 形如 {'linuxdo': 'OK', 'email': 'OK'}。每个账号状态 OK/ALREADY 才算
    成功;单账号成功绝不算 OK(同 agentrouter dual_oauth_incomplete 语义,避免
    掩盖另一账号未签)。"""
    ld = attempts.get("linuxdo", "unknown")
    em = attempts.get("email", "unknown")
    confirmed = {"OK", "ALREADY"}
    ld_ok = ld in confirmed
    em_ok = em in confirmed
    summary = f"linuxdo={ld},email={em}"
    if ld_ok and em_ok:
        return confirmed_done_result(
            f"dual-account confirmed ({summary})",
            adapter=adapter,
            action=ActionEvidence(
                kind="dom_click",
                target="dual_account_login",
                attempted_at=datetime.now().isoformat(timespec="seconds"),
            ),
        )
    if ld_ok or em_ok:
        return fail_result(
            "dual_account_incomplete",
            detail="single-account-only (" + summary + ") · 需重跑补齐另一账号",
            adapter=adapter,
            stage="login",
        )
    return fail_result(
        "dual_account_incomplete",
        detail=summary,
        adapter=adapter,
        stage="login",
    )


async def nexa_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """nexavlinks.com 双账号签到:LinuxDO(USERNAME)+ 邮箱(your_primary_email)。

    1) goto /check-in;已登录先登出(用户下拉 → 退出登录)。
    2) 账号1 LinuxDO:登录卡 → LinuxDO SSO → 回站 /check-in 点「立即签到」。
    3) 再登出 → 账号2 邮箱:登录卡填 account_name 邮箱密码 → /check-in 签到。
    4) 聚合:两账号都 OK → OK;任一失败 → FAIL。终态停在 account_name。

    ⚠️ 登出只点 NEXA 站内「退出登录」(清 NEXA 域 localStorage),绝不用共享
    profile 的全量清除器(9222 共享 profile,P0 红线,见运维手册「P0:共享 9222
    profile 的 Cookie 全清禁令」)。
    """
    kind = adapter.kind or "nexa"
    print(f"  nexa flow: {adapter.name}", flush=True)
    attempts: dict[str, str] = {"linuxdo": "unknown", "email": "unknown"}

    try:
        await page.goto(NEXA_SITE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, adapter.ready_rounds)
    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return fail_result("cloudflare", adapter=kind)

    # ---- 必要初始登出(清掉残留会话,落到未登录登录卡) ----
    await _nexa_logout(page)

    # ---- 账号1: LinuxDO (USERNAME) ----
    ld_sso = await _nexa_linuxdo_gate(page, adapter, browser)
    attempts["linuxdo"] = ld_sso
    if ld_sso == "OK":
        res1 = await _nexa_signin_current(page, adapter)
        attempts["linuxdo"] = "OK" if res1.ok else res1.reason

    # ---- 再登出 → 账号2: 邮箱 (account_name) ----
    # 先导航到应用页 /check-in 带上 LinuxDO 登录态再登出,UI 退出登录才能真清
    # localStorage(同 agentrouter smoke7 教训:gate page 停在 /login 时 logout
    # 会秒退不生效;_nexa_logout 内部会保证先到 /check-in 应用页)。
    try:
        await page.goto(NEXA_CHECKIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        await wait_text_ready(page, 40, max(adapter.ready_rounds, 10))
    except Exception:
        pass
    await _nexa_logout(page)

    em_login = await _nexa_email_gate(page, adapter)
    attempts["email"] = em_login
    if em_login == "OK":
        res2 = await _nexa_signin_current(page, adapter)
        attempts["email"] = "OK" if res2.ok else res2.reason

    return _nexa_result(kind, attempts)


async def tabitoken_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """tabitoken.com 单账号 GitHub OAuth 签到:清 session → GitHub 登录 → 签到 OK。

    1) 清 new_api_refresh cookie(登出):不清则后续 /sign-in 会被自动重登跳
       dashboard,不会真走 GitHub OAuth。
    2) goto /sign-in 等渲染(cookie 已清,应停在此登录卡)。
    3) 点 GitHub CTA(GITHUB_SELECTORS,首项「使用 GitHub 继续」)→ try_github_oauth
       复用现成流程,return_host=tabitoken.com 收敛回跳判据。
    4) OAuth 回跳到 tabitoken 已登录态(dashboard)→ 登录成功 = 签到 OK。
    5) 走 confirmed_done_result 满足 apply_attribution 因果契约(同 agentrouter)。
    """
    kind = adapter.kind or "tabitoken"
    print(f"  tabitoken flow: {adapter.name}", flush=True)

    try:
        await page.goto(TABITOKEN_SITE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, adapter.ready_rounds)

    cf = await wait_out_cloudflare(page, CF_WAIT_S)
    if cf:
        return fail_result("cloudflare", adapter=kind)

    # ---- 登出:清 session cookie(不能全清)----
    await _tabitoken_clear_session(page, browser)

    # ---- 进登录卡 ----
    try:
        await page.goto(TABITOKEN_SIGNIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 30, max(adapter.ready_rounds, 10))

    cta = await click_first_visible(page, GITHUB_SELECTORS, timeout_each=1200)
    if not cta:
        return fail_result("NO_GITHUB_CTA", adapter=kind, stage="login")
    print(f"  tabitoken github CTA: {cta}", flush=True)
    await asyncio.sleep(1.2)
    await check_terms(page)
    status = await try_github_oauth(page, browser=browser, return_host="tabitoken.com")
    if status != "OK":
        return fail_result(status, adapter=kind, stage="login")
    # tabitoken 签到机制=「登录即触发额度」,无签到按钮/确认文字(2026-08-26
    # inspect 实证:同 agentrouter 纯 New API 站)。OAuth 回跳 OK 即签到 OK。
    await asyncio.sleep(TABITOKEN_OAUTH_RETURN_S)

    return confirmed_done_result(
        f"github-oauth confirmed ({status})",
        adapter=kind,
        action=ActionEvidence(
            kind="dom_click",
            target="github_oauth_login",
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


async def bohe_click_spin(page) -> CheckinResult:
    """
    薄荷站第二步: 必须点击「开始转动」才算签到。
    Success requires business confirm text — never OK on guess.
    """
    text0 = await page_text(page, 700)
    if "开始转动" not in text0 and is_valid_checkin_confirm(text0):
        return confirmed_done_result(
            confirm_signal(text0) or "text-confirm", adapter="bohe"
        )

    spin_selectors = [
        'button:has-text("开始转动")',
        'button:has-text("转动")',
        "text=开始转动",
    ]
    btn, used = await wait_for_any_visible(page, spin_selectors, 12.0)
    if used == "CLOUDFLARE":
        cf = await wait_out_cloudflare(page, CF_WAIT_S)
        if cf:
            return fail_result("cloudflare", adapter="bohe")
        btn, used = await wait_for_any_visible(page, spin_selectors, 8.0)

    if btn is None:
        text = await page_text(page, 700)
        if is_valid_checkin_confirm(text):
            return confirmed_done_result(
                confirm_signal(text) or "text-confirm", adapter="bohe"
            )
        return fail_result("no_spin", detail=text[:80], adapter="bohe")

    print(f"  薄荷 spin click: {used}", flush=True)
    await btn.click(timeout=3000, force=True)
    await asyncio.sleep(4.0)

    for _ in range(8):
        text2 = await page_text(page, 800)
        if is_valid_checkin_confirm(text2):
            return confirmed_done_result(
                confirm_signal(text2) or "text-confirm", adapter="bohe",
                action=ActionEvidence(
                    kind="dom_click", target=str(used or "spin"),
                    attempted_at=datetime.now().isoformat(timespec="seconds"),
                ),
            )
        await asyncio.sleep(1.0)

    text3 = await page_text(page, 800)
    if "开始转动" in text3:
        btn2, used2 = await first_visible(page, spin_selectors, timeout_each=800)
        if btn2 is not None:
            print(f"  薄荷 spin re-click: {used2}", flush=True)
            await btn2.click(timeout=3000, force=True)
            await asyncio.sleep(4.0)
            text4 = await page_text(page, 800)
            if is_valid_checkin_confirm(text4):
                return confirmed_done_result(
                    confirm_signal(text4) or "text-confirm", adapter="bohe",
                    action=ActionEvidence(
                        kind="dom_click", target=str(used2 or "spin"),
                        attempted_at=datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            return fail_result("no_confirm", detail="spin clicked but no success text", adapter="bohe")
        return fail_result("no_spin", detail="开始转动 still visible after click", adapter="bohe")

    # spin button gone but no success text → still not confirmed
    if is_valid_checkin_confirm(text3):
        return confirmed_done_result(
            confirm_signal(text3) or "text-confirm", adapter="bohe",
            action=ActionEvidence(
                kind="dom_click", target=str(used or "spin"),
                attempted_at=datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return fail_result("no_confirm", detail=text3[:80], adapter="bohe")


def is_cross_site(site_url: str) -> bool:
    """newapi-checkin.keungliang.dpdns.org 专属站判定."""
    u = (site_url or "").lower()
    return "keungliang" in u or "newapi-checkin" in u


async def cross_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """cross (newapi-checkin.keungliang.dpdns.org) 专用签到流程.
    流程:
    1. 访问首页, 检查是否需要 LinuxDO 登录
    2. 若已登录, 读取 /api/info 状态 (can_checkin, quota, threshold, captcha, pow)
    3. 若不可签到或已签到, 直接返回 ALREADY / business_ineligible
    4. 若可签到, 检查是否启用了 Turnstile/Captcha:
       - 若启用, 确保 Turnstile 渲染并等待获取 token
    5. 调用 /api/checkin/task 获取 PoW 任务
    6. 执行页面内 window.solvePoW 完成算力求解 (难度约 18bit, 1-2秒)
    7. 提交 /api/checkin 完成打卡并验证响应
    """
    site_url = adapter.url
    try:
        await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await bypass_chrome_interstitial_if_needed(page)
    await wait_text_ready(page, 30, adapter.ready_rounds)

    # 检查是否需要登录
    login_btn = page.locator('a[data-role="login-button"]:not(.hidden), a:has-text("使用 Linux Do 登录"):not(.hidden)').first
    if await login_btn.count() > 0 and await login_btn.is_visible(timeout=1000):
        print("  cross: unauthenticated, starting LinuxDO SSO...", flush=True)
        sso_res = await try_linuxdo_sso(page, origin_host="keungliang.dpdns.org", browser=browser)
        if sso_res not in ("OK", "ALREADY"):
            return fail_result("auth_failed", detail=f"SSO: {sso_res}", adapter="cross")
        await wait_text_ready(page, 30, adapter.ready_rounds)

    # 通过 API 与 DOM 综合判定
    info = await page.evaluate('''async () => {
        try {
            const r = await fetch('/api/info');
            return await r.json();
        } catch (e) {
            return {error: String(e)};
        }
    }''')
    if not isinstance(info, dict):
        return fail_result("bad_response", detail="cannot fetch /api/info", adapter="cross")

    if not info.get("logged_in"):
        return fail_result("not_logged_in", detail=str(info.get("error") or "not logged in"), adapter="cross")

    if not info.get("can_checkin"):
        if info.get("last_checkin") or "已签到" in str(info.get("error") or ""):
            return confirmed_done_result("今日已签到", adapter="cross")
        if info.get("quota", 0) >= info.get("quota_threshold", 0):
            return fail_result("business_ineligible", detail="余额充足无需签到", adapter="cross")
        return confirmed_done_result("明天再来签到吧", adapter="cross")

    print(f"  cross eligible: user={info.get('username')}, quota={info.get('quota')}, threshold={info.get('quota_threshold')}", flush=True)

    # 执行签到流程 (Captcha + PoW)
    checkin_script = """async () => {
        const info = await (await fetch('/api/info')).json();
        if (!info.can_checkin) return {already: true, info};

        let captchaToken = '';
        const captcha = info.captcha || {};
        if (captcha.enabled) {
            // Ensure turnstile widget is rendered and get response
            const widget = document.querySelector('[data-role="captcha-widget"]');
            if (widget && window.turnstile) {
                // If not yet rendered, render it
                if (!widget.innerHTML || !widget.querySelector('input')) {
                    widget.innerHTML = '';
                    window.turnstile.render(widget, {
                        sitekey: captcha.site_key,
                        action: 'checkin'
                    });
                }
            }
            // Wait up to 10s for turnstile token
            for (let i = 0; i < 20; i++) {
                if (window.turnstile) {
                    const t = window.turnstile.getResponse();
                    if (t) {
                        captchaToken = t;
                        break;
                    }
                }
                await new Promise(r => setTimeout(r, 500));
            }
        }

        let payload = '', signature = '', counter = '', hash = '';
        const pow = info.pow || {};
        if (pow.enabled) {
            const taskRes = await fetch('/api/checkin/task', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({captcha_token: captchaToken})
            });
            const task = await taskRes.json();
            if (!task.payload || !task.signature) {
                return {error: 'PoW task acquisition failed', task};
            }
            const solved = await window.solvePoW(task.payload, task.difficulty || pow.difficulty || 18, task.expires_at || 0, () => {});
            payload = task.payload;
            signature = task.signature;
            counter = String(solved.counter);
            hash = solved.hash;
        }

        const checkinRes = await fetch('/api/checkin', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: JSON.stringify({
                pow_payload: payload,
                pow_signature: signature,
                pow_counter: counter,
                pow_hash: hash
            })
        });
        const checkinData = await checkinRes.json();
        return {success: true, checkinData};
    }"""

    # 若尚未打开验证码面板，先触发点击立即签到加载验证码与容器
    checkin_btn = page.locator('button[data-role="checkin-button"]').first
    if await checkin_btn.count() > 0:
        await checkin_btn.click()
        await asyncio.sleep(1.0)

    res = await page.evaluate(checkin_script)
    if not isinstance(res, dict):
        return fail_result("eval_failed", detail=str(res), adapter="cross")

    if res.get("already"):
        return confirmed_done_result("今日已签到", adapter="cross")

    if res.get("error"):
        return fail_result("checkin_failed", detail=str(res.get("error")), adapter="cross")

    checkin_data = res.get("checkinData") or {}
    if checkin_data.get("error") and "已签到" in str(checkin_data.get("error")):
        return confirmed_done_result(str(checkin_data.get("error")), adapter="cross")

    if checkin_data.get("can_checkin") is False or checkin_data.get("leaderboard"):
        return confirmed_done_result("签到成功", adapter="cross", action=ActionEvidence(
            kind="pow_checkin", target="/api/checkin", attempted_at=datetime.now().isoformat(timespec="seconds")
        ))

    return confirmed_done_result("签到完成", adapter="cross")


def is_mulink_site(site_url: str) -> bool:
    """demo.dev2.mulink.top 专属站判定."""
    u = (site_url or "").lower()
    return "dev2.mulink.top" in u or ("mulink" in u and "top" in u)


async def mulink_checkin(page, adapter: SiteAdapter, browser=None) -> CheckinResult:
    """mulink (demo.dev2.mulink.top) 签到。

    2026-09-06 前端改版:签到从 /wallet 直显改为「点导航里的 钱包 → 进入 /wallet
    iframe → 额度池卡片里的「领取」按钮」；若直接 goto /wallet 会被 shell 重定向回
    /dashboard/overview，拿不到签到卡。因此必须:
      1) 打开 /dashboard/overview shell,点「Open navigation」展开侧边栏
      2) 点「钱包」菜单项,等 /wallet iframe 出现
      3) 在 iframe 额度池里点「领取」(可领) 或判定「今日已领取」(已领)
    确认文案「今日已领取 / 今天 +」或按钮变「签到」/已领状态。
    """
    kind = adapter.kind or "mulink"
    print(f"  mulink flow: {adapter.name}", flush=True)
    shell_url = "https://demo.dev2.mulink.top/dashboard/overview"

    try:
        await page.goto(shell_url, wait_until="networkidle", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pass
    await wait_text_ready(page, 40, max(adapter.ready_rounds or 12, 15))
    await asyncio.sleep(4)

    # 展开 Dock 侧边栏(露出「钱包」入口)
    try:
        nav_btn = page.locator('button[aria-label="Open navigation"]').first
        if await nav_btn.count():
            await nav_btn.click(timeout=4000)
            await asyncio.sleep(1.5)
    except Exception:
        pass

    # 点「钱包」菜单项(展开后侧边栏里可见)
    try:
        wallet_link = page.get_by_text("钱包", exact=True).first
        if await wallet_link.count():
            await wallet_link.click(timeout=4000, force=True)
            await asyncio.sleep(4)
    except Exception:
        pass

    # 等 /wallet iframe 出现并取 frame
    wf = None
    for _ in range(12):
        for f in page.frames:
            if "/wallet" in (f.url or ""):
                wf = f
                break
        if wf is not None:
            break
        await asyncio.sleep(1)
    if wf is None:
        text = await page_text(page, 700)
        return fail_result("no_wallet_frame", detail=text[:120], adapter=kind, stage="action")

    # 已领态预判:今日已领取
    wtext = await wf.evaluate("document.body ? document.body.innerText : ''")
    if "今日已领取" in wtext or "今日已领" in wtext:
        return confirmed_done_result("text=今日已领取", adapter=kind)

    # 找额度池「领取」按钮
    # 根因(2026-09-07):mulink 额度池卡片头<button>文本为「额度池/本周期还可领取 1 次」,
    # 也命中 has-text("领取")("可领取"子串),且 DOM 顺序排在真领取按钮之前,.first 会点错
    # 到卡片折叠头而非签到。改为按「精确文本=领取」定位(:text-is),实测只命中真领取按钮。
    claim = None
    for sel in [
        'button:text-is("领取")',
        'button:text-is("今日领取")',
        'button:text-is("签到领取")',
        'button:has-text("领取"):not(:has-text("已领取"))',
    ]:
        try:
            loc = wf.locator(sel).first
            if await loc.count():
                txt = (await loc.inner_text()).strip()
                # 精确文本命中即为真领取按钮;模糊命中再排除折叠头(含"本周期/额度池"等)
                if txt == "领取" or txt in ("今日领取", "签到领取"):
                    claim = loc
                    break
                if "领取" in txt and "已领取" not in txt and "本周期" not in txt and "额度池" not in txt:
                    claim = loc
                    break
        except Exception:
            continue
    if claim is None:
        return fail_result("no_button", detail=(wtext[:80] if "额度池" not in wtext else wtext[wtext.find("额度池"):wtext.find("额度池")+200]), adapter=kind, stage="action")

    print(f"  mulink 领取 click", flush=True)
    try:
        await claim.click(timeout=4000, force=True)
    except Exception as e:
        return fail_result("click_failed", detail=str(e)[:100], adapter=kind)
    await asyncio.sleep(5)

    wtext2 = await wf.evaluate("document.body ? document.body.innerText : ''")
    if "今日已领取" in wtext2 or "今日已领" in wtext2 or "今天 +" in wtext2:
        return confirmed_done_result(
            "领取成功", adapter=kind,
            action=ActionEvidence(
                kind="dom_click", target="claim",
                attempted_at=datetime.now().isoformat(timespec="seconds"),
            ),
        )
    # 按钮变「签到」或额度池显示成功
    return fail_result("no_confirm", detail=(wtext2[:80] if "额度池" not in wtext2 else wtext2[wtext2.find("额度池"):wtext2.find("额度池")+200]), adapter=kind)


def is_fengwind_site(site_url: str) -> bool:
    """api-welfalre.fengwind.com 专属站判定."""
    u = (site_url or "").lower()
    return "fengwind" in u or "api-welfalre" in u


FENGWIND_STATUS_PATH = "/api/checkin/status"
FENGWIND_TOKEN_STORAGE_KEY = "welfare_token"


def _parse_utc_iso(s: str) -> datetime | None:
    """解析服务端 ISO8601(如 2026-09-07T00:00:00Z);失败返回 None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def fengwind_status_guard(status: dict | None, now: datetime | None = None) -> CheckinResult | None:
    """fengwind 服务端权威窗口守卫(业务根因修复,2026-09-07)。

    背景:2026-09-06 run177 在北京 06:03(早于当日周期)跑 fengwind,页面残留
    上一周期「已签到」disabled,通用 already 判据误判 ALREADY 并把 daily_tasks
    标 done,导致 08:10 cron 跳过真实可签时段(用户只得手动领取)。

    根因:页面 DOM 的「已签到」只代表"当前服务端业务周期"已签;服务端周期按
    next_reset_at(UTC 0 点=北京 08:00)切新 biz_date。在北京 00:00~08:00 之间
    (UTC 未到 0 点),biz_date 仍是昨天,页面残留昨日已签——DOM 判据不可信。

    修复:不再猜本地时间窗口,直接以服务端 /api/checkin/status 的 next_reset_at
    为准:now(UTC) 早于 next_reset_at → 今日新周期未开,页面残留不可作 ALREADY
    判据 → 返回 keep_pending(FAIL+keep_pending,写回 pending 供后续批次重跑);
    now(UTC) >= next_reset_at → 新周期已开,放行(None)交给通用已签/签到判定。
    status 缺失或字段不可解析 → 保守放行(None),宁可不拦也不误伤(API 故障时
    让通用流程决定,避免整站被永久 pending)。

    now: 测试用显式时间(aware UTC);默认 datetime.now(timezone.utc)。
    """
    nra = (status or {}).get("next_reset_at")
    reset = _parse_utc_iso(nra)
    if reset is None:
        return None
    now = now if now is not None else datetime.now(timezone.utc)
    if now >= reset:
        return None  # 今日周期已开,页面已是新周期,通用判据可信
    return pending_result(
        "window_not_open",
        detail=(
            f"fengwind 今日新周期未开:服务端 next_reset_at={nra}(UTC) 未到,"
            "页面残留昨日周期「已签到」不可作当日 ALREADY 判据;"
            "保持 pending 供周期开启后批次重跑"
        ),
    )


async def _fengwind_fetch_status(page, site_url: str) -> dict | None:
    """在 fengwind 页面上下文读取 /api/checkin/status(带 localStorage token)。

    SPA 用 localStorage['welfare_token'] 存 JWT,axios 拦截器自动注入
    Authorization: Bearer <token>;直接 page.request 调 API 会 401(missing bearer
    token),故必须 goto 到 feng 域名后用页内 fetch + 显式带 token。
    goto 失败/token 缺失/fetch 非 200 → 返回 None(调用方保守放行)。
    """
    try:
        await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        return None
    try:
        await wait_text_ready(page, 20, 6)
    except Exception:
        pass
    try:
        data = await page.evaluate(
            """async () => {
              const tok = localStorage.getItem('welfare_token');
              if (!tok) return {err: 'no_token'};
              const r = await fetch('/api/checkin/status', {
                headers: {'Accept':'application/json', 'Authorization':'Bearer '+tok}
              });
              if (!r.ok) return {status: r.status};
              const j = await r.json();
              return j.data || {};
            }"""
        )
    except Exception:
        # evaluate 被 navigation/页面崩溃/CDP 抖动打断:无法取得权威状态 → 返回 None,
        # 由调用方保守放行(绝不让本站因状态读取失败而整站 crash/pending)。
        return None
    if not isinstance(data, dict) or "next_reset_at" not in data:
        return None
    return data


async def maybe_select_fengwind_validity(page, site_url: str) -> None:
    """Fengwind 福利站: 签到前先勾选有效期(默认选中2天/d2)."""
    if not is_fengwind_site(site_url):
        return
    try:
        label_2d = page.locator('label:has(input[name="checkin-validity"][value="d2"]), label:has-text("2天")').first
        if await label_2d.count() > 0 and await label_2d.is_visible(timeout=1000):
            print("  fengwind: select 2天 validity option", flush=True)
            await label_2d.click()
            await asyncio.sleep(0.3)
        else:
            input_2d = page.locator('input[name="checkin-validity"][value="d2"]')
            if await input_2d.count() > 0:
                print("  fengwind: check 2天 input", flush=True)
                await input_2d.check(force=True)
                await asyncio.sleep(0.3)
    except Exception as e:
        print(f"  fengwind validity select warning: {e}", flush=True)


def is_lucky_flip_site(site_url: str) -> bool:
    """lucky0625 福利小站:签到成功后再翻一张四叶草卡片."""
    u = (site_url or "").lower()
    return "lucky0625" in u or "fuli.lucky0625" in u


_LUCKY_LEAF_FIRST = 'button[aria-label="第 1 片四叶草"]'
_LUCKY_LEAF_ALL = 'button[aria-label^="第 "]'


async def lucky_flip_first_leaf(page) -> CheckinResult | None:
    """lucky0625 福利小站:签到成功后翻开「第 1 片四叶草」卡片.

    签到完成页面出现一排卡片(第 1..N 片四叶草),需点第一片才算翻完。
    今日已翻过(全部 disabled)-> 幂等 ALREADY;签到态未就绪 -> None;
    翻卡后按完成态返回 OK。
    """
    kind = "browser"
    text0 = await page_text(page, 900)
    if not is_valid_checkin_confirm(text0) and "翻开一片四叶草" not in text0:
        return None

    all_leaves = page.locator(_LUCKY_LEAF_ALL)
    try:
        n = await all_leaves.count()
    except Exception:
        n = 0
    if n > 0:
        dl = []
        for i in range(n):
            try:
                dl.append(bool(await all_leaves.nth(i).is_disabled()))
            except Exception:
                dl.append(False)
        if dl and all(dl):
            print("  lucky flip: all leaves disabled -> already flipped today", flush=True)
            return ok_result(
                "ALREADY", adapter=kind, detail="已翻过今日四叶草",
                action=ActionEvidence(kind="none"),
                confirmation=dom_confirmation("已翻开四叶草", done_state=True),
            )

    first = page.locator(_LUCKY_LEAF_FIRST).first
    try:
        if not await first.is_visible(timeout=1500):
            return None
    except Exception:
        return None
    print("  lucky flip: click 第 1 片四叶草", flush=True)
    try:
        await first.click(timeout=3000, force=True)
    except Exception as e:
        print(f"  lucky flip click failed: {e}", flush=True)
        return None
    # 整合后轮询验证:所有卡片应变为 disabled(今日已抽完),或出现「明天」文案。
    for _ in range(6):
        await asyncio.sleep(0.8)
        try:
            leaves = page.locator(_LUCKY_LEAF_ALL)
            cnt = await leaves.count()
            dl = []
            for i in range(cnt):
                try:
                    dl.append(bool(await leaves.nth(i).is_disabled()))
                except Exception:
                    dl.append(False)
            if cnt > 0 and all(dl):
                break
            txt = await page_text(page, 600)
            if "明天" in txt and "翻" not in txt:
                break
        except Exception:
            break
    print("  lucky flip: confirmed(第一片已翻开)", flush=True)
    return confirmed_done_result(
        "翻开一片四叶草",
        adapter=kind,
        action=ActionEvidence(
            kind="dom_click",
            target="第 1 片四叶草",
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        ),
    )


async def lucky_flip_if_needed(page, site_url: str) -> CheckinResult | None:
    """签到成功后(含已签到未翻卡)补播翻第一片四叶草。

    flip 命中 OK 时返回该结果;否则返回 None 让上层按原始确认处理。
    供 _click_cta_and_confirm 与 legacy_checkin_on_page 两处复用。
    """
    if not is_lucky_flip_site(site_url):
        return None
    flip = await lucky_flip_first_leaf(page)
    if flip is not None and flip.status == "OK":
        flip.pre_state = "PENDING"
        flip.post_state = "DONE"
        return flip
    return None


# Probe for a populated, *visible* Cloudflare Turnstile token.
_TURNSTILE_TOKEN_PROBE = """() => {
  const ins = [...document.querySelectorAll('[name="cf-turnstile-response"]')];
  if (!ins.length) return 'none';
  for (const el of ins) {
    const box = el.closest('.turnstile-box, .cf-turnstile') || el.parentElement;
    const r = box ? box.getBoundingClientRect() : {width: 0, height: 0};
    const laid_out = !box || box.offsetParent !== null || r.width > 0 || r.height > 0;
    if (el.value && laid_out) return 'ready';
  }
  return 'pending';
}"""


async def wait_turnstile_token(page, max_s: float = TURNSTILE_TOKEN_WAIT_S) -> bool:
    """Wait for a Cloudflare Turnstile token to populate before clicking a CTA.

    Some check-in pages (verydio's BadAppleAPI) gate the check-in fetch on a
    turnstile token — the widget fills its hidden ``[name=cf-turnstile-response]``
    input a few seconds after the page loads, and clicking the CTA before that
    is rejected with 请先完成人机验证 (no_confirm). If the page has a turnstile
    input in a visible box, poll up to ``max_s`` for a non-empty token.
    Returns True when safe to click (token ready, or no turnstile on the page),
    False if a turnstile exists but no token appeared in time."""
    try:
        state = await page.evaluate(_TURNSTILE_TOKEN_PROBE)
    except Exception:
        return True  # can't introspect → don't block the click
    if state != "pending":
        return True  # 'none' (no widget) and 'ready' are both safe to click
    started = time.monotonic()
    while time.monotonic() - started < max_s:
        await asyncio.sleep(POLL_TURNSTILE_S)
        try:
            state = await page.evaluate(_TURNSTILE_TOKEN_PROBE)
        except Exception:
            state = "pending"
        if state == "ready":
            print(f"  turnstile token ready after {time.monotonic()-started:.1f}s", flush=True)
            return True
    print(f"  turnstile token still empty after {max_s:.0f}s — clicking anyway", flush=True)
    return False


# Some new-api consoles open an announcement dialog on every load (cngov's
# 系统公告 lives in a fixed inset-0 z-50 overlay). A force-click on the sign CTA
# underneath then hits the overlay, the check-in POST never fires, and the day
# ends FAIL:no_confirm. Close a dialog only when it looks like an ANNOUNCEMENT
# (标题/正文含 公告|通知|notice|activity; 或出现「我知道了」「我已阅读」类
# dismiss 按钮) AND does not itself look like the check-in widget (立即签到/
# 开始转动/每日签到/去签到 等真 CTA). NOTE: bare 签到 is deliberately NOT a
# skip token — announcement bodies often mention 签到 (e.g. 签到维护中), so
# excluding it would silently re-block the very dialog this exists to close
# (cngov 2026-08-16 failure). A dialog that is neither recognisably an
# announcement nor recognisably a sign widget is left ALONE (fail-safe).
_DIALOG_DISMISS_JS = """() => {
  const dialogs = [...document.querySelectorAll('[data-slot="dialog-content"], [role="dialog"]')];
  let closed = 0;
  for (const d of dialogs) {
    if (d.getAttribute('aria-hidden') === 'true') continue;
    const text = (d.innerText || '').replace(/\\s+/g, ' ');
    // Never close a dialog that hosts the check-in widget itself.
    if (/立即签到|开始转动|今日签到|去签到|签到奖励|今日已签/.test(text)) continue;
    if (/每日签到/.test(text) && /签到|领取|积分/.test(text)) continue;
    const btn = [...d.querySelectorAll('button')].find(b => {
      const t = (b.innerText || '').trim();
      const aria = b.getAttribute('aria-label') || '';
      return ['我已阅读','我知道了','知道了','关闭','Close','Got it','确定'].some(
        k => t.includes(k) || aria.includes(k)
      );
    });
    const looksAnnouncement = /公告|通知|notice|activity|规则|升级|维护/.test(text);
    // Close only when we have a real dismiss control AND the text marks this
    // as an announcement-style overlay. A dialog with 确定/OK but no 公告/通知
    // marker is a modal decision — leave it alone (fail-safe).
    if (btn && (looksAnnouncement || /我已阅读|我知道了/.test(text))) {
      if (!d.getAttribute('data-dismissed')) {
        btn.click();
        d.setAttribute('data-dismissed', '1');
        closed++;
      }
    }
  }
  return closed;
}"""


async def dismiss_obstructing_dialogs(page) -> int:
    """Close announcement-style modal dialogs that would swallow the sign-CTA
    click. Returns how many dialogs were dismissed (0 = nothing to do)."""
    try:
        count = await page.evaluate(_DIALOG_DISMISS_JS)
    except Exception as dismiss_err:
        print(f"  ! dialog dismiss skipped: {dismiss_err}", flush=True)
        return 0
    if count:
        print(f"  dismissed {count} obstruction dialog(s) before CTA click", flush=True)
        await asyncio.sleep(1.0)
    return count


async def try_signin_api(page, adapter: SiteAdapter) -> str:
    """POST a site's JSON sign-in API (anyrouter /api/user/sign_in) and judge by
    the JSON `success` field. These sites have no CTA and no 「已签到」 text — the
    console silently fires this on load and credits balance. The endpoint is
    idempotent: an already-signed live session still returns success:true, so a
    single POST lands the sign-in for the day regardless of when we run.
    Returns 'OK' on success:true, 'AUTH' on an HTTP auth-failure (401/403),
    'NO_GRANT' on an explicit success:false, 'FAIL' on a fetch/network error,
    and '' when the adapter has no signin_api (no-op)."""
    api = (adapter.signin_api or "").strip()
    if not api:
        return ""
    # Same-origin POST the console itself issues; the session cookie travels
    # automatically, so no extra auth setup is needed. Some clones (hcnsec,
    # edge/proxy style) additionally require a "New-Api-User: <uid>" identity
    # header whose value lives in localStorage under `uid` — pull it when the
    # adapter opts in.
    uid_hdr = ""
    if getattr(adapter, "signin_api_uid_header", False):
        try:
            uid_val = await page.evaluate("() => localStorage.getItem('uid') || ''")
        except Exception:
            uid_val = ""
        print(f"  signin api uid-hdr: uid={uid_val!r}", flush=True)
        # The API layer authenticates by New-Api-User:<localStorage uid>. When
        # the page isn't logged in yet (uid empty), the header would be blank
        # and every call 401s. Ensure account login first so the uid exists.
        if not uid_val and get_account_credential(adapter.name):
            try:
                await _ensure_account_logged_in(page, adapter)
            except Exception:
                pass
            try:
                uid_val = await page.evaluate("() => localStorage.getItem('uid') || ''")
            except Exception:
                uid_val = ""
        if uid_val:
            uid_hdr = ", 'New-Api-User': " + json.dumps(uid_val)
    js = f"""async () => {{
        const r = await fetch({api!r}, {{ method: 'POST',
            headers: {{ 'Content-Type': 'application/json'{uid_hdr} }}, body: '{{}}' }});
        let body = '';
        try {{ body = await r.json(); }}
        catch (e) {{ try {{ body = await r.text(); }} catch (e2) {{}} }}
        const txt = typeof body === 'string' ? body : JSON.stringify(body || {{}});
        return {{ status: r.status, text: txt, success: (body && body.success) === true }};
    }}"""
    try:
        res = await page.evaluate(js)
    except Exception as e:
        print(f"  ! signin api {api} evaluate failed: {e}", flush=True)
        return "FAIL"
    status = (res or {}).get("status")
    success = (res or {}).get("success") is True
    text = str((res or {}).get("text", ""))[:120]
    print(f"  signin api {api} -> status={status} success={success} {text}", flush=True)
    if success:
        return "OK"
    # 401/403 means the session isn't authenticated — a real auth problem, not
    # a declined sign-in. Distinct from NO_GRANT so the guard can report
    # auth_required (and re-auth) instead of a misleading no_grant.
    if status == 401 or status == 403:
        return "AUTH"
    # 2xx with success:false — the grant was already claimed today when the
    # body says so (hcnsec returns "今日已签到"); that's a done-state, not a
    # failure. Treat it as ALREADY so the dispatch reports the day as signed.
    if status is not None and status < 500:
        if not success and re.search(r"今日已签到|已签到|already.*check|already signed|已签", text, re.I):
            print(f"  signin api {api}: already signed today", flush=True)
            return "ALREADY"
        # genuinely declined — reported distinctly
        return "NO_GRANT"
    return "FAIL"


def signin_api_result(adapter: SiteAdapter, status_text: str) -> CheckinResult:
    """Build a fully attributable CheckinResult for a JSON sign-in API call.
    The API POST is a real causal action (it credits the day's balance), so it
    carries runner attribution with an explicit PENDING→DONE transition — the
    same contract the validator require for any post-action OK."""
    api = (adapter.signin_api or "").strip()
    action = ActionEvidence(
        kind="api_post",
        target=api,
        attempted_at=datetime.now().isoformat(timespec="seconds"),
    )
    return ok_result(
        "OK",
        adapter=adapter.kind,
        detail=f"signin api {api}: {status_text}".strip(),
        action=action,
        confirmation=dom_confirmation("sign-in api success", done_state=True),
        pre_state="PENDING",
        post_state="DONE",
        transition="PENDING_TO_DONE",
        attribution="runner",
    )


async def handle_post_click_dialogs_if_needed(page) -> bool:
    """处理点击「立即签到」后弹出的确认规则/免责弹窗(例如咕嘎咕嘎 xmiaom 等站点)。
    特征:弹出 alertdialog/dialog,包含「阅读并同意/签到规则」的勾选框,以及「确认并签到」按钮。
    若存在,自动勾选复选框并点击确认按钮。返回是否触发了二次确认。"""
    try:
        dialog = page.locator('[role="alertdialog"], [data-slot="alert-dialog-content"], [role="dialog"]').first
        if await dialog.count() > 0 and await dialog.is_visible(timeout=1500):
            text = await dialog.inner_text()
            if any(k in text for k in ("签到规则", "阅读并同意", "确认并签到")):
                print("  rule/confirmation dialog detected after CTA click", flush=True)
                # 1. 尝试勾选「我已阅读并同意...」
                chk_label = dialog.locator('label:has-text("同意"), label:has-text("阅读"), [role="checkbox"]').first
                if await chk_label.count() > 0:
                    print("  clicking agreement checkbox/label...", flush=True)
                    await chk_label.click()
                    await asyncio.sleep(0.5)
                # 2. 点击「确认并签到」
                confirm_btn = dialog.locator('button:has-text("确认并签到"), button:has-text("确认签到"), button:has-text("确定")').first
                if await confirm_btn.count() > 0:
                    print("  clicking confirmation button in dialog...", flush=True)
                    await confirm_btn.click()
                    await asyncio.sleep(1.5)
                    return True
    except Exception as e:
        print(f"  post-click dialog handle note: {e}", flush=True)
    return False


async def _click_cta_and_confirm(
    page, btn, used, adapter: SiteAdapter, action: ActionEvidence,
    attempt: int = 0,
) -> CheckinResult:
    """Click a located CTA, then poll for the business confirm with captcha
    recovery.  Extracted so the auth-gate SSO retry path and the main path
    share identical click+captcha+confirm semantics — never OK on click alone.
    The caller builds `action` before the call so that, if the click itself
    raises, the outer exception handler still has the action evidence.
    Returns the terminal CheckinResult (OK / ALREADY / interactive / no_confirm)."""
    kind = adapter.kind
    site_url = adapter.url
    if not await wait_turnstile_token(page):
        return fail_result(
            "interactive",
            detail="turnstile token not ready; business CTA not clicked",
            adapter=kind,
            stage="protection",
        )
    # Announcement dialogs (cngov 系统公告) open over the page and swallow the
    # CTA click, so the check-in POST never fires. Close them before clicking.
    await dismiss_obstructing_dialogs(page)
    # Fengwind: 签到前先勾选额度有效期(2天)
    await maybe_select_fengwind_validity(page, site_url)
    if adapter.use_native_click:
        # wxiai uses a Semi/React delegated handler. Under CDP,
        # Playwright's forced locator click can move focus without
        # reaching the component's onClick; native DOM click does.
        print("  native DOM click: wxiai", flush=True)
        await btn.evaluate("(el) => el.click()")
    else:
        # Prefer the standard actionability-aware click: it waits for the
        # element to be stable and the hit target to be clear, so a freshly
        # mounted / mid-animation button is clicked only once it truly accepts
        # input (wisart's el-button can 'accept' a force click while still
        # transitioning, and the check-in POST then never fires — silent
        # no_confirm). Fall back to the old force click only if a normal click
        # is impossible (offscreen / covered), never as the first choice.
        try:
            await btn.click(timeout=3000)
        except Exception as click_err:
            print(f"  normal click unavailable, force: {click_err}", flush=True)
            await btn.click(timeout=3000, force=True)
    await asyncio.sleep(2.0)

    # 薄荷 adapter: 点「今日」后必须再点「开始转动」
    if kind == "bohe" or is_bohe_site(site_url):
        bohe_result = await bohe_click_spin(page)
        bohe_result.action = action
        if bohe_result.ok and bohe_result.confirmation is None:
            bohe_result.confirmation = dom_confirmation(
                bohe_result.detail or bohe_result.status,
                done_state=bohe_result.status == "ALREADY",
            )
        if bohe_result.status == "OK" and bohe_result.confirmation is not None:
            bohe_result.pre_state = "PENDING"
            bohe_result.post_state = "DONE"
            bohe_result.transition = "PENDING_TO_DONE"
            bohe_result.attribution = "runner"
        return bohe_result

    # 弹窗二次确认(如 xmiaom 规则免责确认框)
    await handle_post_click_dialogs_if_needed(page)

    # STRICT: poll for business confirm — never OK on click alone / button gone.
    # Some sites (abrdns) load a hCaptcha slider-challenge after the sign click;
    # auto-clicking it just yields 请再试一次, so hand human-solvable blocking
    # over to wait_out_captcha instead of misreporting FAIL:no_confirm.
    captcha_recovery_attempted = False
    for _ in range(10):
        if not captcha_recovery_attempted and await captcha_blocking(page):
            captcha_recovery_attempted = True
            print("  captcha appeared after click → wait for human/auto clear", flush=True)
            cap = await wait_out_captcha(page, min(CAPTCHA_WAIT_S, 40.0))
            if cap == "INTERACTIVE":
                return fail_result(
                    "interactive",
                    detail="captcha blocks check-in after click",
                    adapter=kind, action=action,
                )
        text2 = await page_text(page, 900)
        if is_valid_checkin_confirm(text2):
            sig = confirm_signal(text2)
            if not sig:
                await asyncio.sleep(0.8)
                continue
            print(f"  confirm: {sig}", flush=True)
            # lucky0625:签到成功后必须再翻第一片四叶草才算完成。
            flip = await lucky_flip_if_needed(page, site_url)
            if flip is not None:
                return flip
            return ok_result(
                "OK", adapter=kind, detail=sig, action=action,
                confirmation=dom_confirmation(sig),
                pre_state="PENDING", post_state="DONE",
                transition="PENDING_TO_DONE", attribution="runner",
            )
        already_sig = await already_done(
            page, adapter.already_selectors, trusted_already=adapter.trusted_already_selectors
        )
        if already_sig:
            print(f"  confirm(already): {already_sig}", flush=True)
            return confirmed_done_result(already_sig, adapter=kind, action=action)
        await asyncio.sleep(0.8)

    text3 = await page_text(page, 900)
    if captcha_recovery_attempted and await captcha_blocking(page):
        return fail_result(
            "interactive",
            detail="captcha still blocks check-in after recovery",
            adapter=kind, action=action,
        )
    post_block = classify_page_block(text3, await page_url(page))
    if post_block:
        reason, detail = post_block
        return fail_result(reason, detail=detail, adapter=kind, action=action)

    # Slow/overlaid flips: the CTA may have committed server-side while the
    # client re-render lagged or an overlay hid the done-card (ciallo 2026-08-16:
    # checkin recorded, 已签到+今天+$X card sits at body offset >900, confirm
    # absent in the 13s window). One bounded reload + rescan of the SAME strict
    # text/DOM done-state gate gives the site's rendered truth a second chance.
    # Never accepts bare click/navigation as success — done-state evidence only.
    try:
        await page.goto(site_url, wait_until="commit", timeout=min(GOTO_TIMEOUT_MS, 15000))
        await wait_text_ready(page, 40, 12)
        text_r = await page_text(page, 2000)
        if is_valid_checkin_confirm(text_r):
            sig = confirm_signal(text_r)
            print(f"  confirm(after reload): {sig}", flush=True)
            return ok_result(
                "OK", adapter=kind, detail=sig, action=action,
                confirmation=dom_confirmation(sig),
                pre_state="PENDING", post_state="DONE",
                transition="PENDING_TO_DONE", attribution="runner",
            )
        already_r = await already_done(
            page, adapter.already_selectors, trusted_already=adapter.trusted_already_selectors
        )
        if already_r:
            print(f"  confirm(already after reload): {already_r}", flush=True)
            return confirmed_done_result(already_r, adapter=kind, action=action)
    except Exception as reload_err:
        print(f"  ! reload confirm skipped: {reload_err}", flush=True)

    # The reload still shows a live sign CTA: the first click may have been
    # swallowed by a transient overlay / a button that was mid-transition
    # (wisart's el-button under force click), or the backend was briefly slow.
    # Try once more against the same strict done-state gate — never on a bare
    # click, and only while the page still advertises the CTA (an already-done
    # page would have returned above via already_done).
    if attempt < 1:
        btn2, used2 = await wait_for_any_visible(
            page, adapter.sign_selectors, 8.0,
            allow_generic_sign_cta=True,
            trusted_sign=adapter.trusted_sign_selectors,
        )
        if isinstance(used2, str) and used2.startswith("DONE:"):
            detail = used2[5:] or "done-cta"
            print(f"  confirm(retry scan): {detail}", flush=True)
            return confirmed_done_result(detail, adapter=kind, action=action)
        if btn2 is not None:
            print(f"  retry CTA click: {used2}", flush=True)
            action2 = ActionEvidence(
                kind="native_click" if adapter.use_native_click else "dom_click",
                target=str(used2 or "dom-cta-retry")[:160],
                attempted_at=datetime.now().isoformat(timespec="seconds"),
            )
            return await _click_cta_and_confirm(
                page, btn2, used2, adapter, action2, attempt=attempt + 1,
            )

    return fail_result(
        "no_confirm", detail=text3[:100], adapter=kind, action=action,
    )


async def legacy_checkin_on_page(
    page,
    adapter: SiteAdapter,
    browser=None,
) -> CheckinResult:
    site_url = adapter.url
    sign_selectors = adapter.sign_selectors
    already_selectors = adapter.already_selectors
    kind = adapter.kind
    action: ActionEvidence | None = None
    try:
        if kind == "cross" or is_cross_site(site_url):
            return await cross_checkin(page, adapter, browser=browser)
        if kind == "arkengine" or is_arkengine_site(site_url):
            return await arkengine_checkin(page, adapter, browser=browser)
        if kind == "agentrouter" or is_agentrouter_site(site_url):
            return await agentrouter_checkin(page, adapter, browser=browser)
        if kind == "tabitoken" or is_tabitoken_site(site_url):
            return await tabitoken_checkin(page, adapter, browser=browser)
        if kind == "justwoker" or is_justwoker_site(site_url):
            return await justwoker_checkin(page, adapter, browser=browser)
        if kind == "gorouter" or is_gorouter_site(site_url):
            return await gorouter_checkin(page, adapter, browser=browser)
        if kind == "mzlone" or is_mzlone_site(site_url):
            return await mzlone_checkin(page, adapter, browser=browser)
        if kind == "llmroutes" or is_llmroutes_site(site_url):
            return await llmroutes_checkin(page, adapter, browser=browser)
        if kind == "ultrarouter" or is_ultrarouter_site(site_url) or is_ultrarouter_name(site_url or adapter.name):
            return await ultrarouter_checkin(page, adapter, browser=browser)
        if kind == "nexa" or is_nexa_site(site_url) or is_nexa_name(site_url or adapter.name):
            return await nexa_checkin(page, adapter, browser=browser)
        if kind == "mulink" or is_mulink_site(site_url):
            return await mulink_checkin(page, adapter, browser=browser)
        # fengwind 专属窗口守卫(业务根因,2026-09-07):读服务端 /api/checkin/status 的
        # next_reset_at。今日新周期未开时页面残留昨日「已签到」,通用 already 会误判
        # ALREADY 并把 daily_tasks 标 done,使 cron 跳过真实可签时段。本轮守卫只作用于
        # 本站(else 站点路径完全不动):周期未开 → keep_pending 保持 pending,周期已开
        # 或 API 不可用则放行交给通用流程。
        if is_fengwind_site(site_url):
            _fw_status = await _fengwind_fetch_status(page, site_url)
            _fw_guard = fengwind_status_guard(_fw_status)
            if _fw_guard is not None:
                _fw_guard.site = adapter.name
                _fw_guard.adapter = adapter.kind
                return _fw_guard
        # 纯账密站:先去 /login 确保登录态(site_url 对游客常 502/auth 门禁)
        if get_account_credential(adapter.name):
            await _ensure_account_logged_in(page, adapter)
        try:
            await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        except Exception:
            pass
        await bypass_chrome_interstitial_if_needed(page)
        await wait_text_ready(page, 40, adapter.ready_rounds)

        cf = await wait_out_cloudflare(page, CF_WAIT_S)
        if cf:
            return fail_result("cloudflare", adapter=kind)
        initial_text = await page_text(page, 900)
        initial_block = classify_page_block(initial_text, await page_url(page))
        initial_auth_gate = kind == "newapi_profile" and is_auth_gated_empty(initial_text)
        if initial_block and not initial_auth_gate:
            reason, detail = initial_block
            return fail_result(reason, detail=detail, adapter=kind)

        cta_preferred = False
        if adapter.prefer_cta_before_auth:
            pre_btn, pre_used = await wait_for_any_visible(
                page, sign_selectors, min(CTA_WAIT_S, 12.0),
                allow_generic_sign_cta=False,
            )
            if pre_btn is not None:
                cta_preferred = True
                print(f"  site CTA before auth: {pre_used}", flush=True)

        sso_status = None if cta_preferred else await maybe_sso_if_needed(page, browser=browser)
        if sso_status in ("CLOUDFLARE", "TIMEOUT", "UPSTREAM_UNAVAILABLE") or (
            sso_status and str(sso_status).startswith("FAIL")
        ):
            return result_from_sso_failure(sso_status, kind)
        if sso_status == "NO_BUTTON":
            text0 = await page_text(page, 500)
            btn0, _ = await first_visible(page, sign_selectors, timeout_each=800)
            if looks_logged_out(text0) and btn0 is None:
                if await _attempt_account_login_then_reload(page, adapter, site_url):
                    pass  # logged in via account+password; fall through to CTA scan
                else:
                    return fail_result("auth_required", detail="login page without supported SSO", adapter=kind)
        if (
            not cta_preferred
            and await is_auth_page(page, await page_text(page, 600))
            and sso_status != "OK"
        ):
            if await _attempt_account_login_then_reload(page, adapter, site_url):
                pass  # account login cleared the auth gate; fall through to CTA scan
            else:
                return fail_result("auth_required", detail="login redirect before check-in", adapter=kind)
        if sso_status == "OK":
            print("  SSO OK, settle session…", flush=True)
            await asyncio.sleep(2.5)
            try:
                await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
                await wait_text_ready(page, 40, 12)
            except Exception:
                pass

        text = await page_text(page, 600)
        page_block = classify_page_block(text, await page_url(page))
        auth_gate_pending = (
            kind == "newapi_profile"
            and sso_status != "OK"
            and is_auth_gated_empty(text)
        )
        if page_block and not auth_gate_pending:
            reason, detail = page_block
            return fail_result(reason, detail=detail, adapter=kind)
        if looks_logged_out(text) and not cta_preferred:
            sso_status = await maybe_sso_if_needed(page, force=True, browser=browser)
            if sso_status == "OK":
                await asyncio.sleep(2.5)
                await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
                await wait_text_ready(page, 40, 12)
            elif sso_status in ("CLOUDFLARE", "TIMEOUT", "UPSTREAM_UNAVAILABLE") or (
                sso_status and str(sso_status).startswith("FAIL")
            ):
                return result_from_sso_failure(sso_status, kind)
            elif sso_status == "NO_BUTTON":
                btn0, _ = await first_visible(page, sign_selectors, timeout_each=800)
                if btn0 is None and looks_logged_out(await page_text(page, 500)):
                    if await _attempt_account_login_then_reload(page, adapter, site_url):
                        sso_status = "OK"  # account login cleared the gate
                    else:
                        return fail_result("auth_required", detail="login page without supported SSO", adapter=kind)
            if looks_logged_out(await page_text(page, 500)):
                btn0, _ = await first_visible(page, sign_selectors, timeout_each=800)
                if btn0 is None:
                    if await _attempt_account_login_then_reload(page, adapter, site_url):
                        sso_status = "OK"  # account login cleared the gate
                    else:
                        return fail_result("auth_required", detail=sso_fail_msg(sso_status or "unknown"), adapter=kind)

        # JSON sign-in API sites (anyrouter): the console shows neither a
        # 「签到」 CTA nor a 「已签到」 text — the site signs in via an on-load
        # POST /api/user/sign_in. After auth is settled, this is the terminal
        # answer for the day, so short-circuit before the CTA/already scans
        # (which can only miss). Running once here also prevents the previous
        # double-fire (SSO-OK block and no-CTA branch each called the API).
        if adapter.signin_api:
            signin_status = await try_signin_api(page, adapter)
            if signin_status == "OK":
                status_text = await page_text(page, 120)
                print(f"  sign-in via API: {adapter.signin_api}", flush=True)
                return signin_api_result(adapter, status_text)
            if signin_status == "ALREADY":
                # success:false but the body explicitly confirmed today's check-in
                # was already claimed (hcnsec "今日已签到") — a real done-state.
                already_sig = "signin api already signed today"
                print(f"  sign-in via API already done: {adapter.signin_api}", flush=True)
                return ok_result(
                    "ALREADY", adapter=kind, detail=already_sig,
                    action=ActionEvidence(kind="none"),
                    confirmation=dom_confirmation(already_sig, done_state=True),
                    pre_state="DONE", post_state="DONE",
                    transition="DONE", attribution="site",
                )
            if signin_status == "NO_GRANT":
                # success:false — genuinely declined/already-flagged. Report it
                # distinctly instead of falling through to a misleading
                # no_button (a site with signin_api has no CTA to find).
                return fail_result(
                    "no_grant", detail=f"signin api {adapter.signin_api}: success false",
                    adapter=kind, stage="confirm",
                )
            if signin_status == "AUTH" and sso_status != "OK":
                # HTTP 401/403 on a session that hasn't settled into auth — a
                # real login problem, not a declined sign-in. Report
                # auth_required so the runner re-auths instead of a misleading
                # no_grant/no_button. When sso_status==OK the 401 was
                # contradictory; fall through rather than double-report.
                return fail_result(
                    "auth_required",
                    detail=f"signin api {adapter.signin_api}: http auth failure",
                    adapter=kind,
                )
            # FAIL (network): transient — fall through so the normal CTA scan
            # can retry/finish; rare, and never double-fires (we already tried).

        already_sig = await already_done(
            page, already_selectors, trusted_already=adapter.trusted_already_selectors
        )
        if already_sig:
            # lucky0625:已签到但尚未翻四叶草 → 补播翻卡动作。
            flip = await lucky_flip_if_needed(page, site_url)
            if flip is not None:
                print(f"  confirm(already): {already_sig} + lucky flip", flush=True)
                return flip
            return ok_result(
                "ALREADY", adapter=kind, detail=already_sig,
                action=ActionEvidence(kind="none"),
                confirmation=dom_confirmation(already_sig, done_state=True),
            )

        # hCaptcha / CF widget — WAIT on headed Chrome, don't exit instantly
        cap = await wait_out_captcha(page, CAPTCHA_WAIT_S)
        if cap == "INTERACTIVE":
            return fail_result(
                "interactive",
                detail=f"captcha not cleared in {int(CAPTCHA_WAIT_S)}s",
                adapter=kind,
            )

        if adapter.use_overlay_zapper:
            try:
                await asyncio.wait_for(page.evaluate(OVERLAY_ZAPPER_JS), timeout=5.0)
            except Exception:
                pass

        btn, used = await wait_for_any_visible(
            page, sign_selectors, SIGN_WAIT_S, allow_generic_sign_cta=True, trusted_sign=adapter.trusted_sign_selectors
        )
        if used == "CLOUDFLARE":
            cf = await wait_out_cloudflare(page, CF_WAIT_S)
            if cf:
                return fail_result("cloudflare", adapter=kind)
            btn, used = await wait_for_any_visible(
                page, sign_selectors, 8.0, allow_generic_sign_cta=True, trusted_sign=adapter.trusted_sign_selectors
            )
        if isinstance(used, str) and used.startswith("DONE:"):
            detail = used[5:] or "done-cta"
            print(f"  done via sign scan: {detail}", flush=True)
            return confirmed_done_result(detail, adapter=kind)

        if btn is None:
            if looks_logged_out(await page_text(page, 400)):
                sso_status = await maybe_sso_if_needed(page, force=True, browser=browser)
                if sso_status == "OK":
                    await asyncio.sleep(2.5)
                    await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
                    await wait_text_ready(page, 40, 12)
                    btn, used = await wait_for_any_visible(
                        page, sign_selectors, SIGN_WAIT_S, allow_generic_sign_cta=True, trusted_sign=adapter.trusted_sign_selectors
                    )
                    if isinstance(used, str) and used.startswith("DONE:"):
                        detail = used[5:] or "done-cta"
                        print(f"  done via sign scan after sso: {detail}", flush=True)
                        return confirmed_done_result(detail, adapter=kind)
            if btn is None:
                # Late-rendered done UI (林夕: 今日已签到 non-CTA) — recheck before fail
                already_sig = await already_done(
                    page, already_selectors, trusted_already=adapter.trusted_already_selectors
                )
                if already_sig:
                    print(f"  already after no CTA: {already_sig}", flush=True)
                    return confirmed_done_result(already_sig, adapter=kind)
                cap = await wait_out_captcha(page, min(CAPTCHA_WAIT_S, 60.0))
                if cap == "INTERACTIVE" or await captcha_blocking(page):
                    return fail_result(
                        "interactive",
                        detail="captcha blocks check-in",
                        adapter=kind,
                    )
                # one more done scan after captcha wait (DOM may settle)
                already_sig = await already_done(
                    page, already_selectors, trusted_already=adapter.trusted_already_selectors
                )
                if already_sig:
                    print(f"  already after captcha wait: {already_sig}", flush=True)
                    return confirmed_done_result(already_sig, adapter=kind)
                preview = await page_text(page, 240)
                # Auth-gated empty page (SPA 404 shell): the route needs an
                # authed session to render, the server returns 404 for guests.
                # Retry once via forced SSO + re-goto before misreporting
                # no_button — addresses the "登录门禁" pattern on several
                # New-API console/personal sites (grok-heavy, etc.).
                if is_auth_gated_empty(preview) and sso_status != "OK":
                    print("  auth-gated empty page → force SSO + re-goto", flush=True)
                    sso_retry = await maybe_sso_if_needed(page, force=True, browser=browser)
                    if sso_retry == "OK":
                        await asyncio.sleep(2.5)
                        try:
                            await page.goto(site_url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
                            await wait_text_ready(page, 40, adapter.ready_rounds)
                        except Exception:
                            pass
                        btn, used = await wait_for_any_visible(
                            page, sign_selectors, SIGN_WAIT_S,
                            allow_generic_sign_cta=True,
                            trusted_sign=adapter.trusted_sign_selectors,
                        )
                        if isinstance(used, str) and used.startswith("DONE:"):
                            detail = used[5:] or "done-cta"
                            print(f"  done via sign scan after auth-gate SSO: {detail}", flush=True)
                            return confirmed_done_result(detail, adapter=kind)
                        if btn is not None:
                            # Reuse the exact main-path click+captcha+confirm
                            # semantics — never a thinner inline copy.
                            action = ActionEvidence(
                                kind="native_click" if adapter.use_native_click else "dom_click",
                                target=str(used or "dom-cta")[:160],
                                attempted_at=datetime.now().isoformat(timespec="seconds"),
                            )
                            return await _click_cta_and_confirm(page, btn, used, adapter, action)
                        # SSO OK but no CTA after re-goto: if still showing a
                        # login surface it's genuinely auth; else the site has
                        # no check-in feature (fall through to no_button).
                        preview = await page_text(page, 240)
                        if looks_logged_out(preview):
                            return fail_result("auth_required",
                                               detail=sso_fail_msg(sso_retry or "unknown"),
                                               adapter=kind)
                        post_auth_text = await page_text(page, 1200)
                        post_auth_block = classify_page_block(
                            post_auth_text, await page_url(page)
                        )
                        if post_auth_block:
                            reason, detail = post_auth_block
                            return fail_result(reason, detail=detail, adapter=kind)
                        missing_feature = classify_missing_checkin_feature(
                            post_auth_text, await page_url(page), kind,
                            adapter.feature_unavailable_reason,
                        )
                        if missing_feature:
                            reason, detail = missing_feature
                            return fail_result(reason, detail=detail, adapter=kind)
                        return fail_result("no_button", detail=preview[:80], adapter=kind)
                    # sso_retry != "OK": dispatch every real status from
                    # maybe_sso_if_needed/try_linuxdo_sso
                    # ({OK, NO_BUTTON, CLOUDFLARE, TIMEOUT, FAIL:*, None}).
                    if sso_retry == "CLOUDFLARE":
                        return fail_result("cloudflare", adapter=kind)
                    if sso_retry == "TIMEOUT":
                        return fail_result("timeout",
                                           detail="sso authorize callback did not land",
                                           adapter=kind)
                    if sso_retry == "UPSTREAM_UNAVAILABLE":
                        return result_from_sso_failure(sso_retry, kind)
                    if sso_retry and str(sso_retry).startswith("FAIL"):
                        return fail_result("auth_required",
                                           detail=sso_fail_msg(sso_retry),
                                           adapter=kind)
                    # NO_BUTTON / None: no LinuxDO CTA surfaced.  Escalate to
                    # auth_required only when the page genuinely shows a login
                    # surface; otherwise it's a dead/no-SSO page → keep the
                    # accurate no_button instead of misreporting auth_required.
                    if looks_logged_out(await page_text(page, 240)):
                        return fail_result("auth_required",
                                           detail=sso_fail_msg(sso_retry or "no sso cta"),
                                           adapter=kind)
                    recovered_text = await page_text(page, 1200)
                    recovered_block = classify_page_block(
                        recovered_text, await page_url(page)
                    )
                    if recovered_block:
                        reason, detail = recovered_block
                        return fail_result(reason, detail=detail, adapter=kind)
                # Only classify after the auth-gate recovery above; otherwise
                # a guest SPA shell could be mistaken for a disabled feature.
                final_text = await page_text(page, 2000)
                final_block = classify_page_block(
                    final_text, await page_url(page)
                )
                if final_block:
                    reason, detail = final_block
                    return fail_result(reason, detail=detail, adapter=kind)
                missing_feature = classify_missing_checkin_feature(
                    final_text, await page_url(page), kind,
                    adapter.feature_unavailable_reason,
                )
                if missing_feature:
                    reason, detail = missing_feature
                    return fail_result(reason, detail=detail, adapter=kind)
                return fail_result("no_button", detail=preview[:80], adapter=kind)

        action = ActionEvidence(
            kind="native_click" if adapter.use_native_click else "dom_click",
            target=str(used or "dom-cta")[:160],
            attempted_at=datetime.now().isoformat(timespec="seconds"),
        )
        return await _click_cta_and_confirm(page, btn, used, adapter, action)
    except Exception as e:
        detail = str(e)[:120]
        failed_url = await page_url(page)
        return fail_result(
            classify_navigation_error(str(e), failed_url), detail=detail, adapter=kind,
            action=action,
            stage="execution" if action is None else "confirm",
        )


def _result_stage(result: CheckinResult) -> str:
    if result.ok:
        return "confirm"
    reason = result.reason or "unknown"
    if reason in {"cloudflare", "interactive"}:
        return "protection"
    if reason in {"timeout", "not_logged_in", "no_button", "auth_required"}:
        return "auth" if "login" in result.detail.lower() or reason == "auth_required" else "action"
    if reason in {"no_confirm"}:
        return "confirm"
    if reason in {"error", "crash"}:
        return "execution"
    return "legacy"


def _attach_legacy_evidence(result: CheckinResult) -> CheckinResult:
    """Attach only stage metadata; never synthesize missing action/confirmation."""
    result.stage = result.stage or _result_stage(result)
    return result


def apply_attribution(result: CheckinResult) -> CheckinResult:
    """Normalize legacy outcomes into the explicit causal result contract."""
    action = result.action
    no_action = action is None or action.kind == "none"
    confirmed_done = bool(
        result.post_state == "DONE"
        and result.confirmation
        and result.confirmation.checked_in_today
    )
    has_action = bool(action is not None and not no_action and action.attempted_at)
    if result.status == "ALREADY" and no_action:
        if not evidence_is_confirming(result.confirmation):
            return fail_result(
                "unattributed_success",
                detail="already result lacks precheck confirmation",
                site=result.site,
                adapter=result.adapter,
                provider=result.provider,
                stage="confirm",
                action=action,
                confirmation=result.confirmation,
            )
        result.pre_state = "DONE"
        result.post_state = "DONE"
        result.attribution = "precheck"
        result.transition = "NONE"
        return result
    if result.status == "OK":
        if (
            result.pre_state == "PENDING"
            and result.post_state == "DONE"
            and result.transition == "PENDING_TO_DONE"
            and result.attribution == "runner"
            and has_action
            and confirmed_done
        ):
            return result
        return fail_result(
            "unattributed_success",
            detail="success result lacks causal transition evidence",
            site=result.site,
            adapter=result.adapter,
            provider=result.provider,
            stage="confirm",
            action=action,
            confirmation=result.confirmation,
        )
    return result


async def checkin_on_page(
    page,
    adapter: SiteAdapter,
    browser=None,
) -> CheckinResult:
    """Resolve an ordered provider while preserving legacy behavior in Phase A."""

    async def legacy_performer(ctx: ProviderContext) -> CheckinResult:
        result = await legacy_checkin_on_page(
            ctx.page,
            adapter,
            browser=ctx.browser,
        )
        return apply_attribution(_attach_legacy_evidence(result))

    ctx = ProviderContext(
        site=adapter.name,
        url=adapter.url,
        adapter_kind=adapter.kind,
        page=page,
        browser=browser,
    )
    if PROVIDER_ENGINE == "legacy":
        return await legacy_performer(ctx)

    registry = ProviderRegistry(
        [WisartProvider(legacy_performer), LegacyBrowserProvider(legacy_performer)]
    )
    provider = registry.resolve(
        site=adapter.name,
        url=adapter.url,
        adapter_kind=adapter.kind,
    )
    await provider.inspect(ctx)
    result = await provider.perform(ctx)
    result.provider = provider.name
    result = apply_attribution(result)
    if result.ok and not evidence_is_confirming(result.confirmation):
        return fail_result(
            "no_confirm", detail="provider success without confirmation",
            adapter=result.adapter, provider=provider.name, stage="confirm",
            action=result.action,
        )
    return result


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

DEFAULT_TASKS_DIR = os.environ.get(
    "DAILY_CHECKIN_TASKS_DIR",
    str(Path.home() / "Documents" / "daily-checkin" / "tasks"),
)


def default_task_file_path(day: str | None = None) -> str:
    day = day or datetime.now().strftime("%Y-%m-%d")
    return str(Path(DEFAULT_TASKS_DIR) / f"{day} 每日任务.md")


def _reset_daily_task_content(content: str, day: str) -> str:
    """
    Build a fresh daily note from a previous day's file:
    - open all #task #日常 checkboxes
    - strip #auto-fail / trailing ✅ dates
    - refresh 雅思学习 date line
    """
    out_lines: list[str] = []
    for line in content.splitlines(keepends=True):
        raw = line.rstrip("\n\r")
        nl = "\n" if line.endswith("\n") else ""
        if re.match(r"^\s*-\s*\[[ xX]\]\s*#task\s*#日常\b", raw):
            # force open checkbox
            raw = re.sub(r"^(\s*-\s*)\[[ xX]\]", r"\1[ ]", raw, count=1)
            # drop auto-fail tags
            raw = re.sub(r"\s+#auto-fail:\S+", "", raw)
            # drop completion emoji+date suffixes
            raw = re.sub(r"\s+✅\s*\d{4}-\d{2}-\d{2}\b", "", raw)
            # 雅思学习 date stamp
            if "雅思" in raw:
                if re.search(r"雅思学习\s+\d{4}-\d{2}-\d{2}", raw):
                    raw = re.sub(
                        r"(雅思学习)\s+\d{4}-\d{2}-\d{2}",
                        rf"\1 {day}",
                        raw,
                    )
                else:
                    raw = re.sub(r"(雅思学习)\s*$", rf"\1 {day}", raw)
            out_lines.append(raw.rstrip() + nl)
        else:
            out_lines.append(line if line.endswith("\n") or not nl else line + nl)
    text = "".join(out_lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def find_seed_daily_task(tasks_dir: str | Path, before_day: str) -> Path | None:
    """Most recent `{YYYY-MM-DD} 每日任务.md` strictly before before_day."""
    d = Path(tasks_dir)
    if not d.is_dir():
        return None
    best: Path | None = None
    best_day = ""
    for pth in d.glob("* 每日任务.md"):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+每日任务\.md$", pth.name)
        if not m:
            continue
        day = m.group(1)
        if day < before_day and day > best_day:
            best_day = day
            best = pth
    return best


def ensure_daily_task_file(path: str, day: str | None = None) -> tuple[str, bool]:
    """
    Return (content, created).
    If path missing: seed from latest previous daily note (reset checkboxes).
    Never invent an empty catalog — no seed → raise FileNotFoundError.
    """
    target = Path(path)
    day = day or datetime.now().strftime("%Y-%m-%d")
    if target.is_file():
        return target.read_text(encoding="utf-8"), False

    seed = find_seed_daily_task(target.parent, day)
    if seed is None:
        # also try Archive sibling
        archive = target.parent.parent / "Archive"
        seed = find_seed_daily_task(archive, day) if archive.is_dir() else None
    if seed is None:
        raise FileNotFoundError(
            f"no seed daily task to create {target.name} under {target.parent}"
        )

    raw = seed.read_text(encoding="utf-8")
    content = _reset_daily_task_content(raw, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    persist_task_file(str(target), content)
    print(f"Created daily task from seed {seed.name} → {target.name}", flush=True)
    return content, True


def parse_open_tasks(tasks_content: str) -> list[tuple[str, str]]:
    out = []
    for line in tasks_content.splitlines():
        if not re.match(r"^\s*-\s*\[\s\]\s*", line):
            continue
        if "#task" not in line or "#日常" not in line:
            continue
        m = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", line)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
            continue
        m2 = re.search(r"\[([^\]]+)\]", line)
        if m2:
            out.append((m2.group(1).strip(), ""))
    return out


def parse_all_daily_tasks(tasks_content: str) -> list[tuple[str, str, bool]]:
    """Parse all Obsidian daily check-in rows for bidirectional import."""
    out: list[tuple[str, str, bool]] = []
    for line in tasks_content.splitlines():
        mbox = re.match(r"^\s*-\s*\[([ xX])\]\s*", line)
        if not mbox or "#task" not in line or "#日常" not in line:
            continue
        link = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", line)
        if not link:
            continue
        name, url = link.group(1).strip(), link.group(2).strip()
        if name and not name.startswith("雅思"):
            out.append((name, url, mbox.group(1).lower() == "x"))
    return out


# --- Account+password credential reading (纯账密站登录用) ---------------------
# Credential doc format (one block per site):
#     百倍（sub.100xlabs.space）
#     your_qq_email@example.com
#     your_id_number
# Lines starting with #/> are comments; （…） lines are inline notes that void the
# current group; ___ / --- are placeholders. Site-name lines look like "百倍（…）" —
# the part before the parenthesis is the runner site name. Secrets live only here
# (vault, iCloud, not in git) and in runtime memory — never in reason/detail/evidence/log.
_ACCOUNTS_CACHE: dict[str, tuple[str, str]] | None = None


def _materialize_icloud(path: Path) -> None:
    """Force-download an iCloud placeholder if the real file is absent but a
    `.icloud` stub exists. brctl only exists on macOS; on other hosts iCloud
    placeholders never occur, so this is a strict no-op. brctl blocks until the
    download completes; any error is swallowed so the caller's read_text can
    still trigger macOS auto-fetch."""
    if sys.platform != "darwin":
        return
    stub = path.parent / (path.name + ".icloud")
    if path.is_file() or not stub.is_file():
        return
    try:
        import subprocess
        # brctl blocks until the download finishes; the 120s timeout is a bounded
        # anti-hang backstop so a stalled iCloud cannot hold the batch. This runs
        # only once per uncached credential file at startup, and on timeout the
        # exception is swallowed — read_text below still triggers macOS auto-fetch.
        subprocess.run(["brctl", "download", str(path)], check=True, timeout=120)
    except Exception:
        pass


def read_accounts_file() -> str:
    """Read the credential doc as UTF-8 text. Materializes an iCloud placeholder first."""
    _materialize_icloud(ACCOUNTS_FILE)
    return ACCOUNTS_FILE.read_text(encoding="utf-8")


def parse_accounts_doc(content: str) -> dict[str, tuple[str, str]]:
    """Parse the credential markdown into {site_name: (account, password)}.

    Rules:
    - skip blank lines, headings (#), blockquote notes (>), placeholders (___ / ---);
    - a line starting with （ or ( is an inline note that voids the current group;
    - a site-name line is "name（url）" or "name(url)" → name is the part before the paren;
    - the first two non-skipped lines after a site-name are account, then password;
    - a group with fewer than two real values is dropped.
    """
    accounts: dict[str, tuple[str, str]] = {}
    current: str | None = None
    vals: list[str] = []

    def _skip(s: str) -> bool:
        if not s:
            return True
        if s.startswith((">", "#", "!")) or s.startswith("<!--"):
            return True
        if re.fullmatch(r"[-_]{2,}", s):  # --- / ___ placeholders
            return True
        return False

    for raw in content.splitlines():
        s = raw.strip()
        if _skip(s):
            continue
        if s.startswith("（") or s.startswith("("):
            # inline note (e.g. "（2026-08-21 已删除）") → void the in-progress group
            current, vals = None, []
            continue
        m = re.match(r"([^（(]+?)\s*[（(]", s)
        if m:  # site-name line
            if current is not None and len(vals) >= 2:
                accounts[current] = (vals[0], vals[1])
            current, vals = m.group(1).strip(), []
            continue
        if current is not None:  # account / password value line
            vals.append(s)

    if current is not None and len(vals) >= 2:
        accounts[current] = (vals[0], vals[1])
    return accounts


def get_account_credential(site_name: str) -> tuple[str, str] | None:
    """Return (account, password) for site_name, or None if not registered.
    Reads + parses the doc once per process (module-level cache)."""
    global _ACCOUNTS_CACHE
    if _ACCOUNTS_CACHE is None:
        try:
            _ACCOUNTS_CACHE = parse_accounts_doc(read_accounts_file())
        except (FileNotFoundError, OSError) as exc:
            # credential doc unavailable → password-login disabled, never crash the batch
            print(f"  accounts file unavailable; password-login disabled: {exc}", flush=True)
            _ACCOUNTS_CACHE = {}
    return _ACCOUNTS_CACHE.get(site_name)


def mark_task_done(tasks_content: str, name: str) -> tuple[str, bool]:
    """
    Flip open checkbox to [x] for this site name.
    Returns (new_content, changed).
    """
    if re.search(
        rf"(?m)^\s*-\s*\[[xX]\]\s*#task\s*#日常\s*\[{re.escape(name)}\]",
        tasks_content,
    ):
        return tasks_content, False

    patterns = [
        rf"(?m)^(\s*-\s*)\[\s\](\s*#task\s*#日常\s*\[{re.escape(name)}\]\([^)]*\).*)",
        rf"(?m)^(\s*-\s*)\[\s\](\s*#task\s*#日常\s*\[{re.escape(name)}\].*)",
        rf"(?m)^(\s*-\s*)\[\s\](\s*#task\s*#日常[^\n]*\[{re.escape(name)}\].*)",
    ]
    for pat in patterns:
        new, n = re.subn(pat, r"\1[x]\2", tasks_content, count=1)
        if n:
            new = clear_auto_fail(new, name)
            # also strip auto-fail if still present on the marked line
            new = re.sub(
                rf"(?m)^(\s*-\s*\[[xX]\]\s*#task\s*#日常\s*\[{re.escape(name)}\].*?)(\s+#auto-fail:\S+)",
                r"\1",
                new,
            )
            return new, True
    return tasks_content, False


def mark_task_failed(tasks_content: str, name: str, reason: str) -> tuple[str, bool]:
    """
    Annotate open task line with auto-fail note (keep checkbox open for manual fix).
    Format: ... #auto-fail:reason
    """
    reason = re.sub(r"\s+", "_", (reason or "fail").strip())[:60]
    reason = re.sub(r"[^\w\-.:]", "", reason) or "fail"
    lines = tasks_content.splitlines(keepends=True)
    changed = False
    out = []
    pat = re.compile(
        rf"^(\s*-\s*\[\s\]\s*#task\s*#日常\s*\[{re.escape(name)}\](?:\([^)]*\))?.*?)(?:\s+#auto-fail:[^\n]*)?(\s*)$"
    )
    for line in lines:
        m = pat.match(line.rstrip("\n\r"))
        if m and not changed:
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"{m.group(1).rstrip()} #auto-fail:{reason}{nl}")
            changed = True
        else:
            out.append(line)
    return "".join(out), changed


def persist_task_file(path: str, content: str) -> None:
    """Atomic write (tmp + replace) for iCloud/Obsidian safety."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


def load_task_file(path: str) -> str:
    """Read latest task file from disk (prefer live Obsidian edits over stale memory)."""
    return Path(path).read_text(encoding="utf-8")


def validate_task_projection_sink(
    path: str, content: str, target_names: list[str]
) -> str | None:
    """Validate the durable task sink before any browser side effect.

    The check deliberately uses the same task parser/line contract as the
    projection functions.  A writable file without matching rows is not a
    usable sink for the selected run.
    """
    target = Path(path)
    probe = target.with_name(target.name + ".projection-preflight.tmp")
    try:
        if not target.is_file():
            return f"task_file_missing:{path}"
        if not target.parent.is_dir():
            return f"task_file_parent_missing:{target.parent}"
        # Validate the exact file that will be re-read later, not only the
        # in-memory copy loaded during startup.
        live = target.read_text(encoding="utf-8")
        if live != content:
            content = live
        # Atomic replacement needs a writable parent.  Use an exclusive,
        # immediately removed probe so no task content is modified.  Only
        # unlink the probe we actually created — a pre-existing probe is a
        # real collision and must surface as an error, not be cleaned here.
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.close(fd)
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
    except FileExistsError:
        return f"task_projection_preflight_collision:{probe}"
    except (OSError, UnicodeError) as exc:
        return f"task_projection_unavailable:{type(exc).__name__}"

    if target_names:
        rows = {name for name, _url in parse_open_tasks(content)}
        missing = [name for name in target_names if name not in rows]
        if missing:
            return f"task_projection_missing:{missing[0]}"
    return None


def apply_task_success(path: str, name: str) -> tuple[str, bool]:
    """Re-read file, mark done, strip #auto-fail, persist if dirty.

    Returns (content, changed). ``changed`` is True when checkbox flipped
    OR leftover #auto-fail was cleared (already-[x] path must still write).
    """
    content = load_task_file(path)
    content, flipped = mark_task_done(content, name)
    cleaned = clear_auto_fail(content, name)
    dirty = flipped or (cleaned != content)
    if dirty:
        persist_task_file(path, cleaned)
    return cleaned, dirty


def apply_task_failure(path: str, name: str, reason: str) -> tuple[str, bool]:
    """Re-read file, annotate #auto-fail, persist. Returns (content, changed)."""
    content = load_task_file(path)
    content, changed = mark_task_failed(content, name, reason)
    if changed:
        persist_task_file(path, content)
    return content, changed


def exit_code_from_results(results: list[CheckinResult], *, hard_error: int | None = None) -> int:
    """
    Process exit codes for cron:
      0 = all OK/ALREADY (or nothing to do — caller decides)
      1 = at least one FAIL among results
      2 = hard infra error (no seed / CDP connect / etc.)
    """
    if hard_error is not None:
        return int(hard_error)
    if any(r.status == "FAIL" for r in results):
        return 1
    return 0


def finalize_batch_status(
    results: list[CheckinResult], hard_error: tuple[int, str] | None = None,
) -> tuple[int, str]:
    """A hard infrastructure failure always outranks prior successful items."""
    if hard_error is not None:
        return int(hard_error[0]), str(hard_error[1])
    return exit_code_from_results(results), "done"


def parse_auto_fail_tasks(tasks_content: str) -> list[tuple[str, str]]:
    """Open tasks that already carry #auto-fail:… (for --retry-auto-fail)."""
    out = []
    for line in tasks_content.splitlines():
        if not re.match(r"^\s*-\s*\[\s\]\s*", line):
            continue
        if "#task" not in line or "#日常" not in line:
            continue
        if "#auto-fail:" not in line:
            continue
        m = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", line)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
            continue
        m2 = re.search(r"\[([^\]]+)\]", line)
        if m2:
            out.append((m2.group(1).strip(), ""))
    return out


def select_retry_auto_fail_rows(
    system_rows: list[dict[str, Any]], tasks_content: str,
) -> list[dict[str, Any]]:
    """Select enabled failed-retry candidates from the current unchecked note."""
    tagged = {name for name, _ in parse_auto_fail_tasks(tasks_content)}
    return [
        row for row in system_rows
        if row.get("enabled", 1) and row.get("name") in tagged
    ]


def select_db_failed_rows(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SQLite-authoritative fallback for --retry-auto-fail when the Obsidian
    projection is unavailable: today's rows that actually failed (with a
    non-empty last_reason), so a prior silent no-op can't strand them."""
    return [
        r for r in tasks
        if r.get("status") == "failed" and (r.get("last_reason") or "").strip()
    ]


def resolved_target_site_names(
    run_scope: str, pending: list[SiteAdapter],
) -> list[str]:
    """Return the actual resolved site set for targeted-run audit state."""
    return [adapter.name for adapter in pending] if run_scope == "targeted" else []


def filter_unhealthy_rows(rows: list[dict[str, Any]], *, force: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Suppress durable deterministic failures for unfiltered runs."""
    if force:
        return rows, []
    runnable: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for row in rows:
        if row.get("site_health_status") == "suppressed":
            suppressed.append(row)
        else:
            runnable.append(row)
    return runnable, suppressed


def record_site_health_best_effort(store, name: str, result: CheckinResult) -> bool:
    """Persist auxiliary health metadata without changing business semantics."""
    try:
        store.record_site_health(name, result.status, result.reason)
        return True
    except Exception as exc:
        print(
            f"  ! site health update failed [{name}]: {type(exc).__name__}",
            flush=True,
        )
        return False


def clear_auto_fail(tasks_content: str, name: str) -> str:
    """Remove #auto-fail:… tag after success."""
    return re.sub(
        rf"(?m)^(\s*-\s*\[[ xX]\]\s*#task\s*#日常\s*\[{re.escape(name)}\](?:\([^)]*\))?.*?)(\s+#auto-fail:\S+)",
        r"\1",
        tasks_content,
    )


def resolve_site(name: str, url_from_note: str) -> SiteAdapter | None:
    """Prefer note URL for navigation; catalog supplies kind + selectors."""
    for a in SITE_ADAPTERS:
        if a.name == name:
            url = a.url if a.prefer_catalog_url else (url_from_note or "").strip() or a.url
            if url != a.url or url_from_note:
                return SiteAdapter(
                    name=a.name,
                    url=url,
                    kind=a.kind,
                    sign_selectors=list(a.sign_selectors),
                    already_selectors=list(a.already_selectors),
                    ready_rounds=a.ready_rounds,
                    prefer_cta_before_auth=a.prefer_cta_before_auth,
                    use_native_click=a.use_native_click,
                    use_overlay_zapper=a.use_overlay_zapper,
                    trusted_sign_selectors=a.trusted_sign_selectors,
                    trusted_already_selectors=a.trusted_already_selectors,
                    prefer_catalog_url=a.prefer_catalog_url,
                    feature_unavailable_reason=a.feature_unavailable_reason,
                    signin_api=a.signin_api,
                    signin_api_uid_header=a.signin_api_uid_header,
                    trust_cta_has_text=a.trust_cta_has_text,
                    login_url=a.login_url,
                )
            return a
    if url_from_note:
        if is_bohe_site(url_from_note):
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="bohe",
                sign_selectors=['button:has-text("开始转动")', 'button:has-text("转动")', "text=开始转动"],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_arkengine_site(url_from_note):
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="arkengine",
                sign_selectors=['button:has-text("立即签到")'],
                already_selectors=['text=今日已签到', 'text=已签到'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_agentrouter_site(url_from_note):
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="agentrouter",
                sign_selectors=['button:has-text("签到")', 'button:has-text("今日签到")'],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_tabitoken_site(url_from_note):
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="tabitoken",
                # 登录即签到,无签到按钮/文字;留默认选择器仅兜底,流程不依赖。
                sign_selectors=['button:has-text("签到")', 'button:has-text("今日签到")'],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_justwoker_site(url_from_note):
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="justwoker",
                # 登录即签,无签到按钮/文字;留默认选择器仅兜底,流程不依赖。
                sign_selectors=['button:has-text("使用 GitHub 继续")'],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_gorouter_site(url_from_note):
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="gorouter",
                # 登录即签,无签到按钮/文字;留默认选择器仅兜底,流程不依赖。
                sign_selectors=['button:has-text("使用 GitHub 继续")'],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_mzlone_site(url_from_note):
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="mzlone",
                # GitHub 登录 + 正常签到两步;登录后 /profile 有真「立即签到」区。
                sign_selectors=['button:has-text("立即签到")', 'button:has-text("每日签到")'],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签',
                                   'text=已签到'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_ultrarouter_site(url_from_note):
            # ultrarouter.org(New API 克隆):双账号(GitHub + Linux Do OAuth)登录 + 签到两步。
            # GitHub→your_github,LinuxDo→yourhandle;客户若仅 note URL 也落 ultrarouter kind。
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="ultrarouter",
                # 双账号登录卡按 GitHub/Linux Do 真实 CTA;签到区 /profile 有「立即签到」。
                sign_selectors=['button:has-text("立即签到")', 'button:has-text("签到领取奖励")'],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        if is_nexa_site(url_from_note):
            # nexavlinks.com(New API 聚合站):双账号(LinuxDO USERNAME + 邮箱密码)。
            # 客户若仅 note URL 也落 nexa kind → nexa_checkin 双流程。
            return SiteAdapter(
                name=name,
                url=url_from_note,
                kind="nexa",
                sign_selectors=['button:has-text("立即签到")'],
                already_selectors=['text=今日已签到', 'text=签到成功', 'text=今日已签'],
                trusted_sign_selectors=True,
                trusted_already_selectors=True,
            )
        u = (url_from_note or "").lower()
        if any(x in u for x in ("/profile", "/console/personal", "/check-in", "/checkin")):
            return _N(name, url_from_note)
        return _B(name, url_from_note)
    return None


# Field allowlist for the JSONL history log. Explicit allowlist (not asdict)
# so a future evidence field carrying a token/secret can never land on disk.
# Nested evidence goes through evidence_dict (type-guarded, rejects arbitrary).
_LOG_SCALAR_FIELDS = (
    "status", "reason", "detail", "site", "adapter", "marked",
    "provider", "stage", "pre_state", "post_state", "transition", "attribution",
    "business_status", "business_reason", "projection_status", "projection_reason",
)
_LOG_MAX_FIELD_LEN = 240


def _log_trim(v: str) -> str:
    s = str(v or "")
    return s[:_LOG_MAX_FIELD_LEN]


def append_checkin_log(result: CheckinResult, day: str | None = None) -> None:
    """Append one JSON line (Metapi-style history, no DB)."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    log_dir = CHECKIN_LOG_DIR
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{day}.jsonl"
        payload: dict[str, Any] = {
            name: _log_trim(getattr(result, name, ""))
            for name in _LOG_SCALAR_FIELDS
        }
        payload["latency_ms"] = int(getattr(result, "latency_ms", 0) or 0)
        payload["action"] = evidence_dict(result.action)
        payload["confirmation"] = evidence_dict(result.confirmation)
        payload["identity"] = evidence_dict(result.identity)
        payload["ts"] = datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  ! log write failed: {e}", flush=True)


def write_last_run_status(
    *,
    exit_code: int,
    cdp: dict | None,
    results: list[CheckinResult] | None,
    reason: str = "",
    source: str = "cli",
    mode: str = "business",
    scope: str = "full",
    target_sites: list[str] | None = None,
    update_business_run: bool = True,
) -> None:
    """Atomically record every attempt and, when applicable, the business run."""
    ok = sum(1 for r in (results or []) if r.status == "OK")
    already = sum(1 for r in (results or []) if r.status == "ALREADY")
    fail = sum(1 for r in (results or []) if r.status == "FAIL")
    attribution_counts: dict[str, int] = {}
    evidence_counts: dict[str, int] = {}
    failure_counts: dict[str, int] = {}
    projection_failure_counts: dict[str, int] = {}
    for result in results or []:
        key = result.attribution or "legacy_unknown"
        attribution_counts[key] = attribution_counts.get(key, 0) + 1
    for result in results or []:
        if result.confirmation is not None:
            key = result.confirmation.kind
            evidence_counts[key] = evidence_counts.get(key, 0) + 1
        if result.status == "FAIL":
            key = result.reason or "unknown"
            failure_counts[key] = failure_counts.get(key, 0) + 1
        if result.projection_status == "failed":
            key = (result.projection_reason or "projection_failed").split(":")[0]
            projection_failure_counts[key] = projection_failure_counts.get(key, 0) + 1
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "exit": int(exit_code),
        "reason": reason or ("ok" if exit_code == 0 else "fail"),
        "source": source,
        "mode": mode,
        "scope": scope,
        "target_sites": sorted(target_sites or []),
        "cdp": {
            "http": (cdp or {}).get("http"),
            "headless": (cdp or {}).get("headless"),
            "pid": (cdp or {}).get("pid"),
            "port": (cdp or {}).get("port"),
        },
        "ok": ok,
        "already": already,
        "fail": fail,
        "total": ok + already + fail,
        "fail_sites": [r.site for r in (results or []) if r.status == "FAIL"],
        "evidence_counts": evidence_counts,
        "attribution_counts": attribution_counts,
        "failure_counts": failure_counts,
        "projection_failure_counts": projection_failure_counts,
    }
    try:
        LAST_ATTEMPT_STATUS.parent.mkdir(parents=True, exist_ok=True)
        targets = [LAST_ATTEMPT_STATUS]
        if update_business_run:
            targets.append(LAST_RUN_STATUS)
        for target in targets:
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(target)
    except Exception as e:
        print(f"  ! last-run status write failed: {e}", flush=True)


def classify_run_scope(args, only: set[str]) -> tuple[str, list[str]]:
    """Only an unfiltered default-task batch may replace the full-run summary."""
    targeted = bool(only or args.retry_auto_fail or args.task_file)
    return ("targeted" if targeted else "full"), sorted(only)


def should_update_business_run(scope: str, source: str) -> bool:
    """Only cron/CLI full batches are the authoritative full-run summary.

    A web-triggered "run all" is a full batch in shape, but it is a manual
    maintenance/firefight action: it must not overwrite the daily cron full
    summary in last-run.json (the UI already tracks jobs + runs in SQLite and
    last-attempt.json is always updated). Returns False for any web source so a
    manual web rerun can never mask the scheduled batch result.
    """
    return scope == "full" and source != "web"


def parse_cli_args(argv: list[str] | None = None):
    import argparse

    ap = argparse.ArgumentParser(
        description="Daily check-in on existing Chrome CDP (auto-discover; not hard-coded to 9222)"
    )
    ap.add_argument(
        "--cdp",
        default="",
        help="Optional CDP URL or port (e.g. 9222 or http://127.0.0.1:9223). Strict: fail if that port is down. Default: auto-discover newest headed Chrome",
    )
    ap.add_argument(
        "--allow-headless",
        action="store_true",
        help="Allow selecting headless/DrissionPage CDP (default: refuse pure headless)",
    )
    ap.add_argument(
        "--only",
        default="",
        help="Comma-separated site names to run (e.g. 7倍,ciallo)",
    )
    ap.add_argument(
        "--retry-auto-fail",
        action="store_true",
        help="Only open tasks that already have #auto-fail:…",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve pending sites and exit (no CDP)",
    )
    ap.add_argument(
        "--task-file",
        default="",
        help="Override daily task markdown path",
    )
    ap.add_argument(
        "--site-timeout",
        type=float,
        default=SITE_TIMEOUT_S,
        help=f"Per-site wall-clock timeout in seconds (default {SITE_TIMEOUT_S:g}). "
        "A stuck site fails with reason=timeout and the batch continues.",
    )
    ap.add_argument(
        "--batch-timeout",
        type=float,
        default=BATCH_TIMEOUT_S,
        help=f"Whole-batch deadline in seconds (default {BATCH_TIMEOUT_S:g}); "
        "must finish before the outer scheduler timeout.",
    )
    ap.add_argument(
        "--source", default="cli", choices=("cli", "cron", "web"),
        help="Run origin stored in system history",
    )
    return ap.parse_args(argv)


def print_run_summary(results: list[CheckinResult]) -> None:
    ok = [r for r in results if r.status == "OK"]
    already = [r for r in results if r.status == "ALREADY"]
    fail = [r for r in results if r.status == "FAIL"]
    print("\n=== SUMMARY ===", flush=True)
    print(f"OK={len(ok)} ALREADY={len(already)} FAIL={len(fail)} TOTAL={len(results)}", flush=True)
    if fail:
        reasons: dict[str, list[str]] = {}
        for r in fail:
            reasons.setdefault(r.reason or "unknown", []).append(r.site or "?")
        for reason, sites in sorted(reasons.items(), key=lambda x: (-len(x[1]), x[0])):
            print(f"  FAIL:{reason} ({len(sites)}): {', '.join(sites)}", flush=True)
    if ok:
        print("  OK: " + ", ".join(r.site for r in ok if r.site), flush=True)
    if already:
        print("  ALREADY: " + ", ".join(r.site for r in already if r.site), flush=True)


async def run(argv: list[str] | None = None) -> int:
    """Run check-in batch. Returns process exit code (0/1/2)."""
    args = parse_cli_args(argv)
    run_started = time.monotonic()
    source = getattr(args, "source", "cli")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    run_scope, target_sites = classify_run_scope(args, only)
    today_str = datetime.now().strftime("%Y-%m-%d")
    task_file_path = args.task_file or default_task_file_path(today_str)
    store = store_from_env()
    tasks_content = ""
    projection_available = False
    try:
        tasks_content, created = ensure_daily_task_file(task_file_path, today_str)
        projection_available = True
        if created:
            print(f"Task file ready: {task_file_path}", flush=True)
        # The task file is the durable projection sink for every entry point.
        # Web jobs must import it too: import_obsidian_tasks never overwrites
        # authoritative system status (ON CONFLICT keeps current status), so
        # this is safe for web/retries and makes preflight meaningful.
        store.import_obsidian_tasks(
            today_str, parse_all_daily_tasks(tasks_content), task_file_path
        )
        store.materialize_day(today_str)
    except (FileNotFoundError, OSError) as e:
        # SQLite is authoritative; an unavailable Obsidian projection must not
        # prevent independent pending sites from running.
        print(f"Obsidian projection unavailable; continuing with SQLite: {e}", flush=True)
        store.materialize_day(today_str)
    except Exception as exc:
        # DB locked/corrupt at batch start: import_obsidian_tasks or
        # materialize_day failed.  Write a terminal status and abort — no
        # tasks can run without a working store.
        print(f"Initialization failed: {exc}", flush=True)
        write_last_run_status(
            exit_code=2, cdp=None, results=None,
            reason=f"init:{str(exc)[:120]}",
            source=source, scope=run_scope,
            target_sites=target_sites,
            update_business_run=False,
        )
        return 2

    # When Obsidian is present, only rows in the current open task file may run;
    # when it is absent, SQLite's pending task set is authoritative.
    # materialize_day() supplies pending rows from the enabled site catalog.
    file_task_names = {name for name, _url in parse_open_tasks(tasks_content)} if projection_available else set()

    if args.retry_auto_fail and projection_available:
        system_rows = select_retry_auto_fail_rows(
            store.tasks(today_str, pending_only=False), tasks_content
        )
        print(f"Mode: --retry-auto-fail ({len(system_rows)} tagged)", flush=True)
    elif args.retry_auto_fail:
        # Obsidian projection is unavailable, but SQLite is authoritative and
        # carries today's failed rows with last_reason. Fall back to retrying
        # today's failed tasks from DB instead of silently no-oping (the old
        # behavior left every failed site stranded whenever the markdown file
        # was missing on retry day).
        system_rows = select_db_failed_rows(
            store.tasks(today_str, pending_only=False)
        )
        print(
            f"Mode: --retry-auto-fail ({len(system_rows)} failed today via DB; "
            f"Obsidian projection unavailable)",
            flush=True,
        )
    elif only:
        # 当显式指定 --only 某个站点（如 Web 触发或手动指定）时，
        # 允许执行该站点即使其状态为 failed 或已完成，方便重试或测试。
        system_rows = [r for r in store.tasks(today_str, pending_only=False) if r.get("name") in only]
    else:
        system_rows = store.tasks(today_str, pending_only=True)
        if projection_available:
            system_rows = [r for r in system_rows if r.get("name") in file_task_names]
        # 每日定时/全量自动化批次排除人工签到站点 (checkin_mode='manual' 或包含 manual 标签)
        manual_rows = [
            r for r in system_rows
            if r.get("checkin_mode") == "manual" or "manual" in (r.get("tags") or "").lower()
        ]
        if manual_rows:
            print(
                f"Excluded manual-checkin sites ({len(manual_rows)}): "
                + ", ".join(r["name"] for r in manual_rows),
                flush=True,
            )
            system_rows = [
                r for r in system_rows
                if r.get("checkin_mode") != "manual" and "manual" not in (r.get("tags") or "").lower()
            ]
    system_rows, suppressed_rows = filter_unhealthy_rows(
        system_rows, force=bool(only or args.retry_auto_fail)
    )
    if suppressed_rows:
        print(
            "Suppressed unhealthy sites: "
            + ", ".join(
                f"{row['name']}({row.get('site_health_reason') or 'health'})"
                for row in suppressed_rows
            ),
            flush=True,
        )

    pending: list[SiteAdapter] = []
    for row in system_rows:
        name, note_url = row["name"], row["url"]
        if not name or name.startswith("雅思"):
            continue
        if only and name not in only:
            continue
        resolved = resolve_site(name, note_url)
        if not resolved:
            print(f"[skip] no site def and no url: {name}")
            continue
        pending.append(resolved)

    if run_scope == "targeted":
        target_sites = resolved_target_site_names(run_scope, pending)

    projection_error = (
        validate_task_projection_sink(
            task_file_path, tasks_content, [adapter.name for adapter in pending]
        )
        if projection_available else None
    )
    if projection_error:
        print(
            f"Task projection preflight failed; refusing business run: {projection_error}",
            flush=True,
        )
        write_last_run_status(
            exit_code=2, cdp=None, results=None, reason=projection_error,
            source=source, scope=run_scope, target_sites=target_sites,
            update_business_run=False,
        )
        return 2

    if args.dry_run:
        print(f"Pending sites: {len(pending)}", flush=True)
        for adapter in pending:
            print(f"  - {adapter.name} [{adapter.kind}] {adapter.url}", flush=True)
        print("Dry-run: not connecting CDP")
        write_last_run_status(
            exit_code=0, cdp=None, results=[], reason="dry_run",
            source=source, mode="dry_run", scope=run_scope,
            target_sites=target_sites, update_business_run=False,
        )
        return 0

    if only and not pending:
        print(f"No matching pending sites for --only {sorted(only)}")
        write_last_run_status(
            exit_code=0, cdp=None, results=[], reason="no_matching_sites",
            source=source, mode="filtered", scope=run_scope,
            target_sites=target_sites, update_business_run=False,
        )
        return 0

    if not pending:
        print("No tasks to run")
        write_last_run_status(
            exit_code=0, cdp=None, results=[], reason="no_tasks",
            source=source, mode="noop", scope=run_scope,
            target_sites=target_sites, update_business_run=False,
        )
        return 0

    print(f"Pending sites: {len(pending)}", flush=True)
    for a in pending:
        print(f"  - {a.name} [{a.kind}] {a.url}", flush=True)


    # Discover existing Chrome CDP (prefer newest headed). Never launch.
    try:
        if args.cdp:
            raw = args.cdp.strip()
            # cron preflight already selected and verified this exact endpoint;
            # probe it once directly instead of re-running full discovery, which
            # could rank a different Chrome if instances changed in between.
            http = raw if raw.startswith("http") else (
                f"http://127.0.0.1:{raw}" if raw.isdigit() else f"http://{raw}"
            )
            info = _probe_cdp_http(http)
            if not info:
                raise RuntimeError(f"explicit --cdp {http} not reachable")
            if info.get("headless") and not getattr(args, "allow_headless", False):
                raise RuntimeError(
                    f"explicit --cdp {http} is headless; pass --allow-headless"
                )
            info.setdefault("reason", "explicit_cdp")
            endpoint = info
        else:
            endpoint = discover_cdp_endpoint(allow_headless=bool(getattr(args, "allow_headless", False)))
        cdp_url = set_cdp_http(endpoint["http"])
        print(
            f"CDP selected: {cdp_url} headless={endpoint.get('headless')} "
            f"pages={endpoint.get('pages')} pid={endpoint.get('pid')} "
            f"score={endpoint.get('score')} reason={endpoint.get('reason')}",
            flush=True,
        )
        if endpoint.get("all"):
            print(f"  candidates: {endpoint['all']}", flush=True)
    except Exception as e:
        print(f"CDP discovery failed: {e}")
        write_last_run_status(
            exit_code=2, cdp=None, results=None,
            reason=f"cdp_discovery:{e}"[:120], source=source,
            scope=run_scope, target_sites=target_sites,
            update_business_run=False,
        )
        return 2

    results: list[CheckinResult] = []
    run_id = store.create_run(today_str, source, endpoint)
    hard_error: tuple[int, str] | None = None
    site_timeout = float(getattr(args, "site_timeout", SITE_TIMEOUT_S) or 0) or SITE_TIMEOUT_S
    batch_timeout = float(getattr(args, "batch_timeout", BATCH_TIMEOUT_S) or 0) or BATCH_TIMEOUT_S
    batch_deadline = run_started + batch_timeout

    async with async_playwright() as p:
        browser = None
        try:
            browser = await safe_connect(p)
        except Exception as e:
            print(f"CDP connect failed ({CDP_HTTP}): {e}")
            reason = f"cdp_connect:{e}"[:120]
            store.finish_run(run_id, 2, reason)
            write_last_run_status(
                exit_code=2, cdp=endpoint, results=None,
                reason=reason,
                source=source,
                scope=run_scope,
                target_sites=target_sites,
                update_business_run=False,
            )
            return 2

        try:
            def abort_remaining(start_index: int, detail: str) -> None:
                for aborted in pending[start_index:]:
                    aborted_result = fail_result(
                        "batch_aborted", detail=detail, site=aborted.name,
                        adapter=aborted.kind, provider="legacy_browser", stage="infra",
                    )
                    # This runs outside the per-site try/except. A store failure on
                    # one aborted site must not escape and skip finish_run for the
                    # whole batch (leaving a dangling run row). Isolate each site.
                    try:
                        store.set_task_result(
                            today_str, aborted.name, "failed", "batch_aborted"
                        )
                        store.add_run_item(run_id, aborted_result)
                    except Exception as abort_store_err:
                        print(
                            f"  ! abort store write failed [{aborted.name}]: {abort_store_err}",
                            flush=True,
                        )
                    append_checkin_log(aborted_result, today_str)
                    results.append(aborted_result)

            for adapter_index, adapter in enumerate(pending):
                remaining = batch_deadline - time.monotonic()
                if remaining <= BATCH_CLEANUP_RESERVE_S:
                    hard_error = (2, f"batch_timeout:{batch_timeout:.0f}s")
                    abort_remaining(adapter_index, hard_error[1])
                    break
                effective_site_timeout = min(
                    site_timeout, max(1.0, remaining - BATCH_CLEANUP_RESERVE_S)
                )
                name = adapter.name
                print(f"Running {name} [{adapter.kind}]...", flush=True)
                tab_id = None
                # Reconnect if Chrome/WS dropped mid-batch (never launch a new browser).
                try:
                    browser, reconn = await ensure_connected(p, browser, endpoint)
                    if reconn:
                        print("  reconnected CDP mid-batch", flush=True)
                except Exception as e:
                    print(f"  ! CDP reconnect failed: {e} — aborting remaining sites", flush=True)
                    hard_error = (2, f"cdp_lost:{e}"[:120])
                    abort_remaining(adapter_index, hard_error[1])
                    break
                pre_page_ids = page_ids(browser)
                result = fail_result("not_started", site=name, adapter=adapter.kind)
                t0 = time.monotonic()
                try:
                    tab_id = await silent_open_tab()
                    await asyncio.sleep(0.4)
                    page = await find_temp_page(browser, tab_id, pre_page_ids)
                    if page is None:
                        result = fail_result(
                            "no_temp_page",
                            detail=f"tab_id={tab_id[:8]}… not bound",
                            site=name,
                            adapter=adapter.kind,
                        )
                    else:
                        try:
                            result = await asyncio.wait_for(
                                checkin_on_page(page, adapter, browser=browser),
                                timeout=effective_site_timeout,
                            )
                        except asyncio.TimeoutError:
                            print(
                                f"  ! site timeout after {effective_site_timeout:.0f}s [{name}]",
                                flush=True,
                            )
                            result = fail_result(
                                "timeout",
                                detail=f"site wall-clock {effective_site_timeout:.0f}s",
                                site=name,
                                adapter=adapter.kind,
                            )
                            if effective_site_timeout < site_timeout:
                                result.reason = "batch_timeout"
                                result.detail = f"batch deadline {batch_timeout:.0f}s"
                                hard_error = (2, result.detail)
                            # best-effort close the stalled tab / extra pages below
                        result.site = name
                        result.adapter = adapter.kind
                    result.latency_ms = int((time.monotonic() - t0) * 1000)
                    # Redline: never mark success without non-empty confirm detail
                    if result.ok and not (result.detail or "").strip():
                        print(
                            f"  ! empty confirm detail → demote to no_confirm [{name}]",
                            flush=True,
                        )
                        result = fail_result(
                            "no_confirm",
                            detail="empty confirm detail",
                            site=name,
                            adapter=result.adapter or adapter.kind,
                            latency_ms=result.latency_ms,
                        )
                    # 窗口未开/未到期(如 fengwind 北京 <08:00): 不是业务失败,保持
                    # daily_tasks=pending 供后续批次(如 cron 08:00 后)正常重跑,
                    # 不标 done 也不标 failed,同时跳过 health 记录(非真实故障)。
                    if result.keep_pending:
                        result.projection_status = "not_attempted"
                        result.projection_reason = ""
                        print(
                            f"  ! keep pending [{name}]: {result.reason or 'window_not_open'}",
                            flush=True,
                        )
                        store.set_task_result(today_str, name, "pending", only_if_not_done=True)
                        append_checkin_log(result, today_str)
                        try:
                            store.add_run_item(run_id, result)
                        except Exception as audit_err:
                            print(
                                f"  ! add_run_item failed [{name}]: {audit_err}",
                                flush=True,
                            )
                        results.append(result)
                        continue
                    # Health must observe the final business result, including
                    # the empty-confirmation safety demotion above.
                    record_site_health_best_effort(store, name, result)
                    print(
                        f"[{name}] {result.msg()} ({result.latency_ms/1000:.1f}s) "
                        f"adapter={result.adapter} detail={result.detail!r}",
                        flush=True,
                    )
                    if result.ok:
                        if projection_available:
                            # Always re-read disk so Obsidian/hand edits are not clobbered.
                            try:
                                tasks_content, changed = apply_task_success(task_file_path, name)
                            except (FileNotFoundError, OSError):
                                changed = False
                                tasks_content = ""
                            if changed:
                                result.marked = True
                                print(f"  ✓ marked task file: [{name}]", flush=True)
                            elif re.search(
                                rf"(?m)^\s*-\s*\[[xX]\]\s*#task\s*#日常\s*\[{re.escape(name)}\]",
                                tasks_content,
                            ):
                                result.marked = True
                                print(f"  · already marked: [{name}]", flush=True)
                            else:
                                result.projection_status = "failed"
                                result.projection_reason = f"task_projection_missing:{name}"
                                result.business_status = result.status
                                result.business_reason = result.reason
                                print(
                                    f"  ! task projection failed; business result retained [{name}]",
                                    flush=True,
                                )
                        else:
                            result.projection_status = "failed"
                            result.projection_reason = f"task_projection_unavailable:{task_file_path}"
                            result.business_status = result.status
                            result.business_reason = result.reason
                            print(
                                f"  ! Obsidian projection unavailable; business result retained [{name}]",
                                flush=True,
                            )
                        if result.marked:
                            result.projection_status = "succeeded"
                            result.projection_reason = ""
                        try:
                            store.set_task_result(today_str, name, "done")
                        except Exception as db_err:
                            # SQLite write failed (timeout/disk-full) AFTER the
                            # sign-in actually succeeded. Rethrowing here would
                            # flip this real OK into FAIL:crash in the DB; the
                            # Obsidian note is already marked [x]. Keep result OK.
                            print(f"  ! set_task_result done failed [{name}]: {db_err}", flush=True)
                    else:
                        fail_tag = result.reason or result.detail or "fail"
                        if projection_available:
                            try:
                                tasks_content, fch = apply_task_failure(
                                    task_file_path, name, fail_tag
                                )
                            except (FileNotFoundError, OSError):
                                fch = False
                            if fch:
                                result.projection_status = "succeeded"
                                result.projection_reason = ""
                                print(f"  ✗ doc #auto-fail:{fail_tag} [{name}]", flush=True)
                            else:
                                result.projection_status = "failed"
                                result.projection_reason = f"task_projection_missing:{name}"
                                print(f"  ! task projection failed; continuing [{name}]", flush=True)
                        else:
                            # Failure + projection unavailable is still a failed
                            # projection: keep projection_failure_counts symmetric
                            # with the success path (both count unavailable sinks).
                            result.projection_status = "failed"
                            result.projection_reason = f"task_projection_unavailable:{task_file_path}"
                            print(f"  ! Obsidian projection unavailable; continuing [{name}]", flush=True)
                        store.set_task_result(today_str, name, "failed", result.reason or "fail")
                    append_checkin_log(result, today_str)
                    # Audit row must not escape and overwrite a committed business
                    # result. add_run_item failure is logged, never fatal here.
                    try:
                        store.add_run_item(run_id, result)
                    except Exception as audit_err:
                        print(f"  ! add_run_item failed [{name}]: {audit_err}", flush=True)
                    results.append(result)
                except Exception as e:
                    result = fail_result(
                        "crash",
                        detail=str(e)[:120],
                        site=name,
                        adapter=adapter.kind,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                    )
                    print(f"[{name}] {result.msg()}", flush=True)
                    if projection_available:
                        try:
                            tasks_content, fch = apply_task_failure(
                                task_file_path, name, result.reason or "crash"
                            )
                        except (FileNotFoundError, OSError):
                            fch = False
                        if fch:
                            result.projection_status = "succeeded"
                            result.projection_reason = ""
                            print(f"  ✗ doc #auto-fail:crash [{name}]", flush=True)
                        else:
                            result.projection_status = "failed"
                            result.projection_reason = f"task_projection_missing:{name}"
                            print(f"  ! task projection failed; continuing [{name}]", flush=True)
                    else:
                        # Keep projection_status symmetric with success path when
                        # the durable sink is unavailable on a crash failure.
                        result.projection_status = "failed"
                        result.projection_reason = f"task_projection_unavailable:{task_file_path}"
                    append_checkin_log(result, today_str)
                    # Crash path guard: never overwrite a committed "done".
                    # If the business result already persisted success before
                    # the exception, keep it; only mark failed if not yet done.
                    store.set_task_result(
                        today_str, name, "failed", result.reason,
                        only_if_not_done=True,
                    )
                    try:
                        store.add_run_item(run_id, result)
                    except Exception as audit_err:
                        print(f"  ! add_run_item failed (crash) [{name}]: {audit_err}", flush=True)
                    results.append(result)
                finally:
                    if tab_id:
                        try:
                            await close_extra_pages(
                                browser,
                                pre_page_ids,
                                owned_root_target_id=tab_id,
                            )
                        except Exception as e:
                            print(f"  ! extra page cleanup: {e}", flush=True)
                    await silent_close_tab(tab_id, reason=result.msg())
                    await asyncio.sleep(0.8)
        finally:
            await safe_disconnect(browser)

    print("Done writing task file", flush=True)
    print_run_summary(results)
    ec, final_reason = finalize_batch_status(results, hard_error)
    store.finish_run(run_id, ec, final_reason)
    write_last_run_status(
        exit_code=ec, cdp=endpoint, results=results,
        reason=final_reason, source=source, scope=run_scope,
        target_sites=target_sites,
        update_business_run=should_update_business_run(run_scope, source),
    )
    # Notification is best-effort; never fails the batch. Only cron full runs
    # with no hard error fire the daily summary (avoids spurious TG noise on
    # web "run all" and partial/retry batches).
    if source == "cron" and run_scope == "full" and hard_error is None:
        try:
            await asyncio.to_thread(
                notify_daily_summary, results, today_str,
                run_source=source, run_scope=run_scope,
            )
        except Exception:
            pass
    return ec


def acquire_run_lock():
    """Acquire a non-blocking whole-batch singleton lock."""
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = RUN_LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started={datetime.now().isoformat(timespec='seconds')}\n")
    handle.flush()
    return handle


if __name__ == "__main__":
    import sys

    lock_handle = acquire_run_lock()
    if lock_handle is None:
        print("Another daily-checkin batch is already running", flush=True)
        raise SystemExit(4)
    try:
        raise SystemExit(asyncio.run(run(sys.argv[1:])))
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

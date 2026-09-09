"""Daily check-in notification: TG daily summary via @tg_bot_handle."""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_TG_TEXT_LEN = 4000


def _get_ssl_context() -> ssl.SSLContext | None:
    """Create SSL context with fallback to common CA cert bundles."""
    ca_files = [
        os.environ.get("SSL_CERT_FILE", ""),
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
        "/opt/homebrew/etc/ca-certificates/cert.pem",
    ]
    for ca_path in ca_files:
        if ca_path and os.path.exists(ca_path):
            try:
                return ssl.create_default_context(cafile=ca_path)
            except Exception:
                continue
    return None


def _send_telegram_raw(token: str, chat_id: str, text: str) -> bool:
    """POST to Telegram sendMessage. Returns True on HTTP 200."""
    if len(text) > MAX_TG_TEXT_LEN:
        text = text[: MAX_TG_TEXT_LEN - 16] + "\n... (已截断)"
    url = TELEGRAM_API.format(token=token)
    body = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        ensure_ascii=False,
    ).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ctx = _get_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok") is True
    except Exception as exc:
        print(f"  notify: TG send failed: {exc}", flush=True)
        return False


def build_daily_summary_text(results, today_str: str) -> str:
    """Build daily summary text from CheckinResult list.

    Returns formatted markdown-safe text ready for Telegram.
    """
    ok = [r for r in results if r.status == "OK"]
    already = [r for r in results if r.status == "ALREADY"]
    fail = [r for r in results if r.status == "FAIL"]
    total = len(results)
    success_count = len(ok) + len(already)
    emoji = "✅" if len(fail) == 0 else "⚠️"
    short_date = today_str[5:] if len(today_str) > 5 else today_str

    lines = [
        f"{emoji} 签到日报 {short_date}   成功 {success_count}/{total}",
    ]
    if ok:
        names = "、".join(r.site for r in ok[:15])
        extra = f" +{len(ok)-15}" if len(ok) > 15 else ""
        lines.append(f"🆕 今日签到: {names}{extra}")
    if already:
        names = "、".join(r.site for r in already[:10])
        extra = f" +{len(already)-10}" if len(already) > 10 else ""
        lines.append(f"🔄 今日已签: {names}{extra}")
    if fail:
        lines.append(f"❌ 失败 {len(fail)} 站:")
        grouped: dict[str, list[str]] = {}
        for r in fail:
            grouped.setdefault(r.reason or "unknown", []).append(r.site or "?")
        for reason, sites in sorted(grouped.items(), key=lambda x: -len(x[1])):
            names = "、".join(sites[:8])
            extra = f" +{len(sites)-8}" if len(sites) > 8 else ""
            lines.append(f"  {reason}: {names}{extra}")
    return "\n".join(lines)


def notify_daily_summary(
    results,
    today_str: str,
    run_source: str = "cli",
    run_scope: str = "targeted",
    store: Any | None = None,
) -> bool:
    """Fire daily summary to TG. Gated: only on full batch (not --only/retry)."""
    if run_scope != "full":
        return False

    enabled_str = "true"
    token = ""
    chat_id = ""
    policy = "all"

    # Check environment first
    env_token = os.environ.get("DAILY_CHECKIN_TG_BOT_TOKEN")
    env_chat_id = os.environ.get("DAILY_CHECKIN_TG_CHAT_ID")

    if store is not None:
        try:
            enabled_str = str(store.get_config("tg_notify_enabled", "true")).lower()
            token = str(store.get_config("tg_bot_token", "")).strip()
            chat_id = str(store.get_config("tg_chat_id", "")).strip()
            policy = str(store.get_config("tg_notify_policy", "all")).lower()
        except Exception:
            pass
    elif "DAILY_CHECKIN_DB" in os.environ:
        try:
            from checkin_core.store import store_from_env
            s = store_from_env()
            enabled_str = str(s.get_config("tg_notify_enabled", "true")).lower()
            token = str(s.get_config("tg_bot_token", "")).strip()
            chat_id = str(s.get_config("tg_chat_id", "")).strip()
            policy = str(s.get_config("tg_notify_policy", "all")).lower()
        except Exception:
            pass

    if env_token is not None:
        token = env_token.strip()
    if env_chat_id is not None:
        chat_id = env_chat_id.strip()

    if enabled_str not in ("1", "true", "yes", "on"):
        print("  notify: TG notification disabled via tg_notify_enabled", flush=True)
        return False

    if not token or not chat_id:
        print(
            "  notify: TG not configured (missing DAILY_CHECKIN_TG_BOT_TOKEN/CHAT_ID)",
            flush=True,
        )
        return False

    fail_count = sum(1 for r in results if getattr(r, "status", None) == "FAIL")
    if policy == "fail_only" and fail_count == 0:
        print("  notify: TG notification skipped (policy=fail_only and 0 failures)", flush=True)
        return False

    text = build_daily_summary_text(results, today_str)
    if not text:
        return False
    return _send_telegram_raw(token, chat_id, text)


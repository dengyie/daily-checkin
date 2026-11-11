"""Daily check-in notification: TG daily summary via @tg_bot_handle."""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_TG_TEXT_LEN = 4000

# AutoNexus Agent 告警委派配置
AUTONEXUS_ALERT_URL = os.environ.get("AUTONEXUS_ALERT_URL", "http://10.144.144.31:9230/api/v1/alerts/ingest")
AUTONEXUS_ALERT_TOKEN = os.environ.get("AUTONEXUS_ALERT_TOKEN") or os.environ.get("FLEET_AUTH_TOKEN") or ""
AUTONEXUS_AGENT_ENABLED = os.environ.get("AUTONEXUS_AGENT_ENABLED", "true").lower() in ("1", "true", "yes", "on")


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


def build_daily_summary_text(results, today_str: str, ai_report: Any | None = None) -> str:
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

    if ai_report and getattr(ai_report, "inspections", None):
        lines.append("")
        lines.append("🤖 AI 智能质检归因:")
        if getattr(ai_report, "summary", ""):
            lines.append(f"💡 结论: {ai_report.summary}")
        for insp in ai_report.inspections[:6]:
            action_hint = f" -> {insp.suggested_action}" if getattr(insp, "suggested_action", "") else ""
            lines.append(f"• {insp.site}: [{insp.category}] {insp.root_cause}{action_hint}")
        if len(ai_report.inspections) > 6:
            lines.append(f"  ...等共 {len(ai_report.inspections)} 项")
        if getattr(ai_report, "auto_retry_targets", None):
            lines.append(f"⚡ 已触发自愈重试: {', '.join(ai_report.auto_retry_targets)}")

    return "\n".join(lines)


def delegate_failures_to_agent(
    results,
    today_str: str,
    ai_report: Any | None = None,
) -> bool:
    """将签到失败异常优先委派给 AutoNexus Agent 自动处置（诊断、自愈重试、凭据探测）。

    成功接收后返回 True，此时抑制即时 Telegram 发送（由 Agent 自愈失败后再报警）；
    未配置、禁用或发送失败返回 False，触发调用方回退直发 Telegram 兜底。
    """
    token = os.environ.get("AUTONEXUS_ALERT_TOKEN") or os.environ.get("FLEET_AUTH_TOKEN") or AUTONEXUS_ALERT_TOKEN
    url = os.environ.get("AUTONEXUS_ALERT_URL") or AUTONEXUS_ALERT_URL
    enabled = os.environ.get("AUTONEXUS_AGENT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    if not enabled or not token or not url:
        return False

    fails = [r for r in results if getattr(r, "status", None) == "FAIL"]
    if not fails:
        return False

    failed_sites = [getattr(r, "site", "unknown") for r in fails]
    target_sites = "、".join(failed_sites[:10])
    summary = f"⚠️ 签到日报 {today_str[5:] if len(today_str) > 5 else today_str} 失败 {len(fails)} 站: {target_sites}"
    if len(failed_sites) > 10:
        summary += f" +{len(failed_sites)-10}"

    fail_details = []
    for r in fails[:20]:
        site_val = getattr(r, "site", "")
        reason_val = getattr(r, "reason", "")
        msg_val = getattr(r, "message", "")
        fail_details.append({
            "site": site_val if isinstance(site_val, str) else (str(site_val) if site_val else ""),
            "reason": reason_val if isinstance(reason_val, str) else (str(reason_val) if reason_val else ""),
            "message": msg_val if isinstance(msg_val, str) else "",
        })

    ai_info = {}
    if ai_report:
        summary_val = getattr(ai_report, "summary", "")
        retry_val = getattr(ai_report, "auto_retry_targets", [])
        ai_info = {
            "summary": summary_val if isinstance(summary_val, str) else "",
            "auto_retry_targets": [str(x) for x in retry_val] if isinstance(retry_val, (list, tuple)) else [],
        }

    payload_dict = {
        "source": "daily-checkin",
        "rule": "checkin_site_failed",
        "target": f"checkin:{target_sites[:80]}",
        "severity": "ERROR",
        "summary": summary[:200],
        "dedup_key": f"daily-checkin:{today_str}:{len(fails)}",
        "details": {
            "date": today_str,
            "failed_count": len(fails),
            "total_count": len(results),
            "failures": fail_details,
            "ai_report": ai_info,
        },
    }
    payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Alert-Source": "daily-checkin",
        },
        method="POST",
    )
    ctx = _get_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            if resp.status in (200, 201, 202):
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success") is not False:
                    print(f"  🤖 签到异常已成功委派给 AutoNexus Agent 自动处置（已暂缓 TG 日报）", flush=True)
                    return True
            print(f"  ⚠️ 委派 AutoNexus Agent 响应异常 (HTTP {resp.status})", flush=True)
            return False
    except Exception as exc:
        print(f"  ⚠️ 委派 AutoNexus Agent 请求失败: {exc}", flush=True)
        return False


def notify_daily_summary(
    results,
    today_str: str,
    run_source: str = "cli",
    run_scope: str = "targeted",
    store: Any | None = None,
    ai_report: Any | None = None,
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

    # 1. 成功通知静默判断：受 NOTIFY_ON_SUCCESS 约束（基本不报，拉活成功不报，缺省静默）
    if fail_count == 0:
        notify_on_success_env = os.environ.get("NOTIFY_ON_SUCCESS", "false").lower() in ("1", "true", "yes", "on")
        if not notify_on_success_env:
            print("  notify: TG notification skipped (0 failures and NOTIFY_ON_SUCCESS=false by default)", flush=True)
            return False
        if policy == "fail_only":
            print("  notify: TG notification skipped (policy=fail_only and 0 failures)", flush=True)
            return False

    # 2. 失败异常：优先委派 AutoNexus Agent 自动处置（自愈、质检与重试）；成功接收后暂缓 TG，失败回退直发 TG
    if fail_count > 0:
        delegated = delegate_failures_to_agent(results, today_str, ai_report=ai_report)
        if delegated:
            return True
        print("  ⚠️ 委派 AutoNexus Agent 失败或未配置，自动回退到 Telegram 直发日报告警。", flush=True)

    text = build_daily_summary_text(results, today_str, ai_report=ai_report)
    if not text:
        return False
    return _send_telegram_raw(token, chat_id, text)


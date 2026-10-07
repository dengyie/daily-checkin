from __future__ import annotations

import re
from typing import Any

# Compiled regex patterns for redaction (re.IGNORECASE)
_RE_BEARER = re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE)
_RE_JWT = re.compile(r"eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]+")
_RE_CRED_KV = re.compile(
    r'(?i)(token|key|secret|password|passwd|pwd|auth|cookie|session)[=:]\s*["\']?[a-zA-Z0-9_\-\.]{6,}["\']?'
)
_RE_EMAIL = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_RE_UID = re.compile(r"(?i)\b(uid|user_?id)\b[=:]\s*\d{4,}")


class EvidenceSanitizer:
    """Zero-leakage evidence sanitizer. Redacts credentials, tokens, PII from error logs."""

    @classmethod
    def sanitize_text(cls, text: str, max_length: int = 400) -> str:
        if not text:
            return ""
        s = text
        s = _RE_BEARER.sub("Bearer [REDACTED]", s)
        s = _RE_JWT.sub("[JWT_REDACTED]", s)
        s = _RE_CRED_KV.sub(r"\1=[REDACTED]", s)
        s = _RE_EMAIL.sub("[EMAIL_REDACTED]", s)
        s = _RE_UID.sub(r"\1=[UID_REDACTED]", s)
        # Bounded truncation to protect prompt token budget
        if len(s) > max_length:
            s = s[:max_length] + "…[truncated]"
        return s

    @classmethod
    def sanitize_task_context(cls, raw_ctx: dict[str, Any]) -> dict[str, Any]:
        """Convert a task failure object or dict into a safe, credential-free prompt payload."""
        site_name = str(raw_ctx.get("name") or raw_ctx.get("site") or "").strip()
        url = str(raw_ctx.get("url") or "").strip()
        adapter = str(raw_ctx.get("adapter") or raw_ctx.get("kind") or "").strip()
        stage = str(raw_ctx.get("stage") or "").strip()
        reason = str(raw_ctx.get("reason") or "").strip()
        raw_detail = str(raw_ctx.get("detail") or raw_ctx.get("error") or "")

        try:
            elapsed_s = round(float(raw_ctx.get("elapsed_s", 0)), 1)
        except (ValueError, TypeError):
            elapsed_s = 0.0

        try:
            historical_failures = int(raw_ctx.get("historical_failures", 0))
        except (ValueError, TypeError):
            historical_failures = 0

        return {
            "site": site_name,
            "url": url,
            "adapter": adapter,
            "stage": stage,
            "reason": reason,
            "detail": cls.sanitize_text(raw_detail),
            "elapsed_s": elapsed_s,
            "historical_failures": historical_failures,
        }

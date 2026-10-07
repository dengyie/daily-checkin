from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

from checkin_core.ai_inspector_models import (
    AIChannelConfig,
    InspectionReport,
    SiteInspectionResult,
)
from checkin_core.ai_sanitizer import EvidenceSanitizer

logger = logging.getLogger("checkin.ai_inspector")


def create_ssl_context() -> ssl.SSLContext:
    """Create a secure SSL context with system CA bundle fallback and opt-in insecure bypass."""
    if os.environ.get("CHECKIN_INSECURE_SKIP_VERIFY") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    for cafile in ("/opt/homebrew/etc/ca-certificates/cert.pem", "/etc/ssl/cert.pem"):
        if os.path.exists(cafile):
            try:
                return ssl.create_default_context(cafile=cafile)
            except Exception:
                pass
    return ssl.create_default_context()

INSPECTOR_SYSTEM_PROMPT = """你是一个高可用自动化签到系统的资深 SRE 质检专家。
你的职责是分析自动化爬虫/签到批次中的失败项，进行根因定性并提供安全自愈决策。

必须输出严格符合如下 JSON 格式的文本，禁止返回任何 Markdown 标记或多余文字：
{
  "summary": "50字以内的今日故障总体评估",
  "inspections": [
    {
      "site": "站点名",
      "category": "transient_flake | auth_expired | upstream_disabled | upstream_offline | selector_drift | unknown",
      "confidence": 0.0到1.0的浮点数,
      "root_cause": "具体根因简述（结合状态码与报错上下文）",
      "can_auto_retry": true或false,
      "suggested_action": "针对该站的具体操作指引"
    }
  ],
  "auto_retry_targets": ["仅当 category 为 transient_flake 且 confidence >= 0.85 时的站点列表"]
}"""


class AIInspector:
    """Post-checkin AI inspection & diagnosis engine with multi-channel waterfall fallback.

    Calls OpenAI-compatible completion endpoints in priority order (waterfall degradation)
    to classify failures and determine if auto-remediation (retry / suppress) is safe.
    """

    def __init__(
        self,
        store: Any,
        base_url: str = "",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout: float = 15.0,
        channels: Sequence[AIChannelConfig | dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.channels: list[AIChannelConfig] = []

        if channels:
            for ch in channels:
                if isinstance(ch, AIChannelConfig):
                    self.channels.append(ch)
                elif isinstance(ch, dict):
                    self.channels.append(AIChannelConfig.from_dict(ch))

        clean_url = (base_url or "").strip().rstrip("/")
        clean_key = (api_key or "").strip()
        clean_model = (model or "gpt-4o-mini").strip()
        req_timeout = max(3.0, float(timeout))

        if clean_url and clean_key:
            primary_ch = AIChannelConfig(
                name="Primary",
                base_url=clean_url,
                api_key=clean_key,
                model=clean_model,
                timeout=req_timeout,
                enabled=True,
            )
            if not self.channels:
                self.channels.append(primary_ch)
            else:
                if not any(c.base_url == primary_ch.base_url and c.model == primary_ch.model for c in self.channels):
                    self.channels.insert(0, primary_ch)

        first = self.channels[0] if self.channels else None
        self.base_url = first.base_url if first else clean_url
        self.api_key = first.api_key if first else clean_key
        self.model = first.model if first else clean_model
        self.timeout = first.timeout if first else req_timeout

    async def inspect_failures(
        self,
        today_str: str,
        failed_results: Sequence[Any],
        source: str = "cron",
    ) -> InspectionReport | None:
        """Run inspection on failed results across prioritized channels. Returns structured report or None."""
        active_channels = [c for c in self.channels if c.enabled and c.base_url and c.api_key]
        if not failed_results or not active_channels:
            return None

        # 1. Collect and sanitize contexts
        raw_items: list[dict[str, Any]] = []
        for r in failed_results:
            site_name = str(getattr(r, "site", None) or getattr(r, "name", None) or "").strip()
            adapter_name = str(getattr(r, "adapter", None) or getattr(r, "kind", None) or "").strip()
            stage_name = str(getattr(r, "stage", None) or "").strip()
            reason_name = str(getattr(r, "reason", None) or "").strip()
            detail_val = str(getattr(r, "detail", None) or "")
            elapsed_val = getattr(r, "elapsed_s", 0)

            hist_failures = 0
            if hasattr(self.store, "get_site_failures") and site_name:
                try:
                    hist_failures = self.store.get_site_failures(site_name)
                except Exception:
                    hist_failures = 0

            raw_items.append({
                "name": site_name,
                "adapter": adapter_name,
                "stage": stage_name,
                "reason": reason_name,
                "detail": detail_val,
                "elapsed_s": elapsed_val,
                "historical_failures": hist_failures,
            })

        sanitized_items = [
            EvidenceSanitizer.sanitize_task_context(item)
            for item in raw_items
        ]

        user_content = (
            f"日期: {today_str}\n"
            f"失败站点总数: {len(sanitized_items)}\n"
            f"站点失败上下文:\n"
            + json.dumps(sanitized_items, ensure_ascii=False, indent=2)
        )

        failed_channel_notes: list[str] = []
        for idx, channel in enumerate(active_channels):
            payload = {
                "model": channel.model,
                "messages": [
                    {"role": "system", "content": INSPECTOR_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }

            t0 = time.monotonic()
            try:
                raw_response, prompt_tokens, completion_tokens = await asyncio.to_thread(
                    self._invoke_llm_api, payload, channel
                )
                latency_ms = int((time.monotonic() - t0) * 1000)

                parsed_report = self._parse_and_validate(raw_response)
                if not parsed_report:
                    raise ValueError(f"Channel '{channel.name}' returned invalid or unparseable report schema")

                report = InspectionReport(
                    summary=parsed_report.summary,
                    total_failures=parsed_report.total_failures,
                    inspections=parsed_report.inspections,
                    auto_retry_targets=parsed_report.auto_retry_targets,
                    channel_name=channel.name,
                    fallback_chain=list(failed_channel_notes),
                )

                # Persist inspection record
                persisted_model = f"{channel.name}:{channel.model}" if len(active_channels) > 1 else channel.model
                if hasattr(self.store, "save_ai_inspection"):
                    try:
                        self.store.save_ai_inspection(
                            day=today_str,
                            model=persisted_model,
                            latency_ms=latency_ms,
                            status="ok",
                            summary=report.summary,
                            structured_json=json.dumps(report.to_dict(), ensure_ascii=False),
                            source=source,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
                    except Exception as exc:
                        logger.warning("save_ai_inspection failed: %s", exc)

                if failed_channel_notes:
                    logger.info(
                        "AIInspector recovered via fallback channel '%s' (%s) after failure(s): %s",
                        channel.name, channel.model, ", ".join(failed_channel_notes),
                    )
                return report

            except Exception as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                err_note = f"[{channel.name} / {channel.model}] failed ({latency_ms}ms): {exc}"
                failed_channel_notes.append(err_note)
                if idx < len(active_channels) - 1:
                    logger.warning(
                        "AIInspector %s; degrading to fallback channel '%s'...",
                        err_note, active_channels[idx + 1].name,
                    )
                else:
                    logger.warning(
                        "AIInspector %s; all %d candidate channels exhausted.",
                        err_note, len(active_channels),
                    )

        return None

    def _invoke_llm_api(
        self,
        payload: dict[str, Any],
        channel: AIChannelConfig | None = None,
    ) -> tuple[dict[str, Any], int, int]:
        """Synchronous HTTP call wrapped in to_thread with channel support."""
        target_ch = channel or (self.channels[0] if self.channels else None)
        base_url = (target_ch.base_url if target_ch else self.base_url).rstrip("/")
        api_key = target_ch.api_key if target_ch else self.api_key
        timeout = target_ch.timeout if target_ch else self.timeout

        url = f"{base_url}/chat/completions"
        data_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "HermesCheckinInspector/1.0",
        }

        ctx = create_ssl_context()

        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp_body = resp.read().decode("utf-8")
            parsed = json.loads(resp_body)
            choices = parsed.get("choices", [])
            if not choices:
                raise ValueError("No choices in LLM response")
            content = choices[0].get("message", {}).get("content", "")

            usage = parsed.get("usage", {})
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)

            cleaned_content = self._strip_markdown_fence(content)
            result_json = json.loads(cleaned_content)
            return result_json, prompt_tokens, completion_tokens

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        s = text.strip()
        if s.startswith("```"):
            lines = s.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            s = "\n".join(lines).strip()
        return s

    def _parse_and_validate(self, raw_dict: dict[str, Any]) -> InspectionReport | None:
        """Enforce strict schema validation and double confidence gating."""
        if not isinstance(raw_dict, dict):
            return None

        raw_inspections = raw_dict.get("inspections", [])
        if not isinstance(raw_inspections, list):
            raw_inspections = []

        validated_inspections: list[SiteInspectionResult] = []
        for item in raw_inspections:
            if isinstance(item, dict):
                validated_inspections.append(SiteInspectionResult.from_dict(item))

        # Double confidence gating: transient_flake and confidence >= 0.85
        safe_retry_targets: list[str] = [
            insp.site
            for insp in validated_inspections
            if (
                insp.site
                and insp.can_auto_retry
                and insp.category == "transient_flake"
                and insp.confidence >= 0.85
            )
        ]

        summary = str(raw_dict.get("summary") or "").strip()
        if not summary and validated_inspections:
            summary = f"检测到 {len(validated_inspections)} 项失败，其中 {len(safe_retry_targets)} 项建议重试。"

        return InspectionReport(
            summary=summary,
            total_failures=len(validated_inspections),
            inspections=validated_inspections,
            auto_retry_targets=safe_retry_targets,
        )

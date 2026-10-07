from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FailureCategory = Literal[
    "transient_flake",
    "auth_expired",
    "upstream_disabled",
    "upstream_offline",
    "selector_drift",
    "unknown",
]

VALID_CATEGORIES: frozenset[str] = frozenset({
    "transient_flake",
    "auth_expired",
    "upstream_disabled",
    "upstream_offline",
    "selector_drift",
    "unknown",
})


@dataclass(slots=True, frozen=True)
class SiteInspectionResult:
    site: str
    category: FailureCategory
    confidence: float
    root_cause: str
    can_auto_retry: bool
    suggested_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteInspectionResult:
        cat = str(data.get("category", "unknown")).strip().lower()
        if cat not in VALID_CATEGORIES:
            cat = "unknown"
        try:
            conf = float(data.get("confidence", 0.0))
            conf = max(0.0, min(1.0, conf))
        except (ValueError, TypeError):
            conf = 0.0

        return cls(
            site=str(data.get("site", "")).strip(),
            category=cat,  # type: ignore[arg-type]
            confidence=conf,
            root_cause=str(data.get("root_cause", "")).strip(),
            can_auto_retry=bool(data.get("can_auto_retry", False)),
            suggested_action=str(data.get("suggested_action", "")).strip(),
        )


@dataclass(slots=True, frozen=True)
class AIChannelConfig:
    """Configuration for a single AI provider channel."""
    name: str
    base_url: str
    api_key: str
    model: str = "gpt-4o-mini"
    timeout: float = 15.0
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AIChannelConfig:
        return cls(
            name=str(data.get("name") or "default").strip(),
            base_url=str(data.get("base_url") or "").strip().rstrip("/"),
            api_key=str(data.get("api_key") or "").strip(),
            model=str(data.get("model") or "gpt-4o-mini").strip(),
            timeout=max(3.0, float(data.get("timeout") or 15.0)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(slots=True, frozen=True)
class InspectionReport:
    summary: str
    total_failures: int
    inspections: list[SiteInspectionResult] = field(default_factory=list)
    auto_retry_targets: list[str] = field(default_factory=list)
    channel_name: str = ""
    fallback_chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "total_failures": self.total_failures,
            "inspections": [i.to_dict() for i in self.inspections],
            "auto_retry_targets": list(self.auto_retry_targets),
            "channel_name": self.channel_name,
            "fallback_chain": list(self.fallback_chain),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InspectionReport:
        raw_inspections = data.get("inspections", [])
        inspections = [
            SiteInspectionResult.from_dict(i)
            for i in raw_inspections
            if isinstance(i, dict)
        ]
        retry_targets = [
            str(t).strip()
            for t in data.get("auto_retry_targets", [])
            if str(t).strip()
        ]
        return cls(
            summary=str(data.get("summary", "")).strip(),
            total_failures=int(data.get("total_failures", len(inspections))),
            inspections=inspections,
            auto_retry_targets=retry_targets,
            channel_name=str(data.get("channel_name", "")).strip(),
            fallback_chain=[str(c).strip() for c in data.get("fallback_chain", []) if str(c).strip()],
        )

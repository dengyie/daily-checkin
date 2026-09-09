"""Provider/evidence contracts for daily-checkin.

The models are intentionally stdlib-only and JSON-serializable.  They can be
introduced beside the legacy runner without changing its result semantics.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EvidenceKind = Literal[
    "api_success",
    "server_status",
    "network_response",
    "dom_strong",
    "dom_done_state",
]
ActionKind = Literal["api_post", "native_click", "dom_click", "none"]


@dataclass
class ActionEvidence:
    kind: ActionKind
    target: str = ""
    request_seen: bool | None = None
    response_status: int | None = None
    attempted_at: str = ""


@dataclass
class ConfirmationEvidence:
    kind: EvidenceKind
    source: str
    summary: str
    observed_at: str
    checked_in_today: bool | None = None
    transition_unknown: bool = False


@dataclass
class IdentityEvidence:
    before: str | None = None
    after: str | None = None
    expected: str | None = None
    matched: bool | None = None
    source: str = ""


@dataclass
class ProviderInspection:
    authenticated: bool | None = None
    identity: IdentityEvidence | None = None
    api_capability: Literal["verified", "unsupported", "unknown"] = "unknown"
    native_action_capability: Literal["available", "missing", "unknown"] = "unknown"
    status_capability: Literal["verified", "unsupported", "unknown"] = "unknown"
    notes: list[str] = field(default_factory=list)


def evidence_dict(value: Any) -> dict[str, Any] | None:
    """Return a JSON-safe dataclass mapping without accepting arbitrary secrets."""
    if value is None:
        return None
    if not isinstance(value, (ActionEvidence, ConfirmationEvidence, IdentityEvidence)):
        raise TypeError(f"unsupported evidence type: {type(value).__name__}")
    return asdict(value)


def evidence_is_confirming(value: ConfirmationEvidence | None) -> bool:
    """Only explicit confirmation objects can authorize a provider success."""
    return value is not None and bool(value.kind and value.source and value.summary)

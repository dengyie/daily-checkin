"""Wisart provider boundary.

No write endpoint is assumed here.  Until a logged-in, redacted protocol probe
proves an API/status contract, this provider delegates to the legacy browser
flow while attaching a stable provider name and inspection notes.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from checkin_core.models import ProviderInspection
from checkin_core.providers.base import ProviderContext

LegacyPerformer = Callable[[ProviderContext], Awaitable[Any]]


class WisartProvider:
    name = "wisart"
    _HOST = "wisart.kuaileshifu.com"

    def __init__(self, performer: LegacyPerformer):
        self._performer = performer

    def matches(self, *, site: str, url: str, adapter_kind: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return site == "图片公益站" or host == self._HOST

    async def inspect(self, ctx: ProviderContext) -> ProviderInspection:
        return ProviderInspection(
            api_capability="unknown",
            status_capability="unknown",
            native_action_capability="available",
            notes=[
                "write API disabled until a logged-in protocol probe verifies it",
                "legacy browser action retained; click is not success",
            ],
        )

    async def perform(self, ctx: ProviderContext) -> Any:
        result = await self._performer(ctx)
        # Shadow-only metadata: this must not change legacy success semantics.
        result.provider = self.name
        if not getattr(result, "stage", ""):
            result.stage = "legacy"
        return result

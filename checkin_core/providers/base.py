"""Provider interfaces and the legacy compatibility provider."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from checkin_core.models import ProviderInspection


@dataclass(frozen=True)
class ProviderContext:
    site: str
    url: str
    adapter_kind: str
    page: Any
    browser: Any = None


class CheckinProvider(Protocol):
    name: str

    def matches(self, *, site: str, url: str, adapter_kind: str) -> bool: ...

    async def inspect(self, ctx: ProviderContext) -> ProviderInspection: ...

    async def perform(self, ctx: ProviderContext) -> Any: ...


LegacyPerformer = Callable[[ProviderContext], Awaitable[Any]]


class LegacyBrowserProvider:
    """Bridge existing behavior into the provider registry unchanged."""

    name = "legacy_browser"

    def __init__(self, performer: LegacyPerformer):
        self._performer = performer

    def matches(self, *, site: str, url: str, adapter_kind: str) -> bool:
        return True

    async def inspect(self, ctx: ProviderContext) -> ProviderInspection:
        return ProviderInspection(notes=["legacy bridge; capabilities not probed"])

    async def perform(self, ctx: ProviderContext) -> Any:
        return await self._performer(ctx)

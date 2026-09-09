"""Ordered provider registry."""
from __future__ import annotations

from typing import Iterable

from checkin_core.providers.base import CheckinProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[CheckinProvider]):
        self._providers = tuple(providers)
        if not self._providers:
            raise ValueError("provider registry cannot be empty")

    def resolve(self, *, site: str, url: str, adapter_kind: str) -> CheckinProvider:
        for provider in self._providers:
            if provider.matches(site=site, url=url, adapter_kind=adapter_kind):
                return provider
        raise LookupError(f"no provider for {site!r} {url!r}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

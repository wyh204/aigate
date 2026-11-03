"""Provider registry — built once at startup, closed once at shutdown."""

from __future__ import annotations

from collections.abc import Iterable

from app.config import ProviderConfig, Settings
from app.provider.base import Provider
from app.provider.mock import MockProvider
from app.provider.openai_compat import OpenAICompatProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider] = ()) -> None:
        self._providers: dict[str, Provider] = {}
        for provider in providers:
            self._providers[provider.name] = provider

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderRegistry:
        providers: list[Provider] = []
        for config in settings.providers:
            providers.append(_build_provider(config, settings))
        return cls(providers)

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"provider {name!r} is not registered") from exc

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers)

    def all(self) -> list[Provider]:
        return [self._providers[name] for name in self.names()]

    async def aclose(self) -> None:
        for provider in self.all():
            await provider.aclose()


def _build_provider(config: ProviderConfig, settings: Settings) -> Provider:
    if config.type == "mock":
        return MockProvider(name=config.name, delay_s=settings.mock_stream_delay_s)
    if config.type == "openai_compat":
        if not config.base_url:
            raise ValueError(f"provider {config.name!r} needs base_url")
        return OpenAICompatProvider(
            name=config.name,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout_s=config.timeout_s or settings.request_timeout_s,
        )
    raise ValueError(f"unknown provider type {config.type!r} for {config.name!r}")

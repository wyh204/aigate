"""Routing: turn the client's ``model`` field into an ordered target chain.

Today the chain is static (alias -> [provider:model, ...]). M2 adds the
interesting part: pick the cheap target and only escalate when needed. Keep
``reason`` on the Route from day one — the cost dashboards and the interview
story both depend on it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.provider.registry import ProviderRegistry


class RouteError(Exception):
    """Configuration is broken — fail at startup, not per request."""


class RouteNotFound(Exception):
    """The client asked for a model/alias we do not serve."""


@dataclass(frozen=True, slots=True)
class RouteTarget:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"

    @classmethod
    def parse(cls, spec: str) -> RouteTarget:
        provider, sep, model = spec.partition(":")
        if not sep or not provider or not model:
            raise RouteError(f"invalid route target {spec!r}, expected 'provider:model'")
        return cls(provider=provider, model=model)


@dataclass(frozen=True, slots=True)
class Route:
    alias: str
    chain: tuple[RouteTarget, ...]
    reason: str = "alias"

    @property
    def primary(self) -> RouteTarget:
        return self.chain[0]


class Router:
    def __init__(
        self,
        routes: Mapping[str, Sequence[str]],
        registry: ProviderRegistry,
        *,
        fallback: str | None = None,
    ) -> None:
        """``fallback`` makes the gateway plug-compatible with agents.

        Most agent clients hardcode model ids (``gpt-4o``, ``claude-3-5-sonnet``).
        Without a fallback every one of them gets a 404 until someone adds an
        alias. With one, an unknown model is served by the fallback alias and the
        route reason says so — visible in ``x-aigate-route`` and the trace log.
        """
        if not routes:
            raise RouteError("no routes configured")
        self._routes: dict[str, tuple[RouteTarget, ...]] = {}
        for alias, specs in routes.items():
            if not specs:
                raise RouteError(f"route {alias!r} has no targets")
            chain = tuple(RouteTarget.parse(spec) for spec in specs)
            for target in chain:
                if not registry.has(target.provider):
                    raise RouteError(
                        f"route {alias!r} references unknown provider "
                        f"{target.provider!r}; registered: {', '.join(registry.names())}"
                    )
            self._routes[alias] = chain
        if fallback is not None and fallback not in self._routes:
            raise RouteError(f"fallback route {fallback!r} is not configured")
        self._registry = registry
        self._fallback = fallback

    def aliases(self) -> list[str]:
        return sorted(self._routes)

    def resolve(self, requested_model: str) -> Route:
        chain = self._routes.get(requested_model)
        if chain is not None:
            return Route(alias=requested_model, chain=chain, reason="alias")

        if ":" in requested_model:
            target = RouteTarget.parse(requested_model)
            if self._registry.has(target.provider):
                return Route(alias=requested_model, chain=(target,), reason="direct")

        if self._fallback is not None:
            return Route(
                alias=requested_model,
                chain=self._routes[self._fallback],
                reason=f"unknown model {requested_model!r}, fell back to {self._fallback!r}",
            )

        raise RouteNotFound(requested_model)

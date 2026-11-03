"""Exact + semantic cache.

Two independent layers behind one lookup:

* **exact** — identical request (tenant, provider, model, full message list and
  sampling params) served from the store. Zero false-hit risk; this is the layer
  that should carry most of the hit rate in production (system prompts, repeated
  questions, retries).
* **semantic** — same tenant + model + system prompt, single-turn user message
  whose embedding is within ``threshold`` of a cached one. This is where the
  money and the risk are: it answers "what is the capital of France" from
  "which city is France's capital". The threshold starts conservative (0.92) and
  is a config knob, not a magic constant.

Design rules encoded here:

* the cache **never** decides whether a request is allowed — quota is checked
  before it, in the API layer;
* a hit is still a metered row (``cache_hit=True``, ``cost_usd=0``,
  ``saved_cost_usd`` = what the call would have cost), otherwise "we saved 40%"
  has no source;
* only non-stream requests are cached (M4): replaying SSE from a stored
  completion needs a chunker, and pretending otherwise would make the demo lie.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from app.cache.embedding import Embedder, cosine
from app.cache.store import CacheStore
from app.config import Settings
from app.logging import get_logger
from app.pricing import PriceBook
from app.protocol.openai import ChatCompletion, ChatCompletionRequest
from app.router.simple import Route, RouteTarget

logger = get_logger(__name__)

#: Parameters that change the answer must change the key.
_KEYED_EXTRA_FIELDS = ("tools", "tool_choice", "functions", "response_format", "seed")

# Human-facing names for the admin endpoint: "MemoryStore" is an implementation
# detail, "memory" is what an operator reasons about.
_BACKEND_NAMES = {"MemoryStore": "memory", "RedisStore": "redis"}


@dataclass(frozen=True, slots=True)
class CacheHit:
    kind: str  # "exact" | "semantic"
    completion: ChatCompletion
    provider: str
    model: str
    similarity: float | None
    saved_usd: float
    age_s: float


def exact_key(tenant: str, target: RouteTarget, payload: ChatCompletionRequest) -> str:
    """Stable across processes: same request -> same key, no local state."""
    body = payload.upstream_payload(target.model)
    body.pop("stream", None)
    body.pop("stream_options", None)
    raw = json.dumps(
        {"t": tenant, "p": target.provider, "m": target.model, "b": body},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _last_user_text(payload: ChatCompletionRequest) -> str | None:
    for message in reversed(payload.messages):
        if message.role == "user":
            return message.content if isinstance(message.content, str) else None
    return None


class CacheService:
    def __init__(
        self,
        settings: Settings,
        store: CacheStore,
        price_book: PriceBook,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self._enabled = settings.cache_enabled
        self._ttl_s = settings.cache_ttl_s
        self._semantic_enabled = settings.semantic_cache
        self._threshold = settings.semantic_threshold
        self._max_per_partition = settings.semantic_max_partition
        self._store = store
        self._price_book = price_book
        self._embedder = embedder
        # partition -> [(key, vector, created_at)], newest last.
        self._semantic: dict[str, list[tuple[str, list[float], float]]] = {}
        self._counters: dict[str, int] = {
            "hits_exact": 0,
            "hits_semantic": 0,
            "misses": 0,
            "stores": 0,
            "skipped": 0,
        }
        self._saved_usd = 0.0

    # ------------------------------------------------------------------ query
    @property
    def enabled(self) -> bool:
        return self._enabled

    def cacheable(self, payload: ChatCompletionRequest) -> bool:
        """Only deterministic-enough requests. A creative call must not be cached."""
        temperature = payload.temperature
        if temperature is None:
            temperature = 1.0
        if temperature > 0.2:
            return False
        extra = payload.model_extra or {}
        if any(extra.get(field) for field in _KEYED_EXTRA_FIELDS):
            return False
        if payload.n not in (None, 1):
            return False
        return all(isinstance(message.content, (str, type(None))) for message in payload.messages)

    async def lookup(
        self, tenant: str, route: Route, payload: ChatCompletionRequest
    ) -> CacheHit | None:
        if not self._enabled:
            return None
        if not self.cacheable(payload):
            self._counters["skipped"] += 1
            return None

        target = route.primary
        key = exact_key(tenant, target, payload)
        entry = await self._store.get(key)
        if entry is not None:
            self._counters["hits_exact"] += 1
            return self._hit("exact", entry, similarity=None)

        hit = await self._lookup_semantic(tenant, target, payload)
        if hit is not None:
            self._counters["hits_semantic"] += 1
            return hit

        self._counters["misses"] += 1
        return None

    async def _lookup_semantic(
        self, tenant: str, target: RouteTarget, payload: ChatCompletionRequest
    ) -> CacheHit | None:
        if not self._semantic_enabled or self._embedder is None:
            return None
        text = _last_user_text(payload)
        if text is None or len(payload.messages) != 1:
            return None

        partition = self._partition(tenant, target, payload)
        candidates = self._semantic.get(partition)
        if not candidates:
            return None

        vector = self._embedder(text)
        best_score = -1.0
        best_key: str | None = None
        cutoff = time.monotonic() - self._ttl_s if self._ttl_s > 0 else 0.0
        for key, cached_vector, created_at in candidates:
            if created_at < cutoff:
                continue
            score = cosine(vector, cached_vector)
            if score > best_score:
                best_score, best_key = score, key

        if best_key is None or best_score < self._threshold:
            return None
        entry = await self._store.get(best_key)
        if entry is None:
            return None
        return self._hit("semantic", entry, similarity=best_score)

    def _hit(self, kind: str, entry: dict[str, Any], *, similarity: float | None) -> CacheHit:
        completion = ChatCompletion.model_validate(entry["completion"])
        usage = completion.usage
        saved = 0.0
        if usage is not None:
            saved = self._price_book.cost_usd(
                entry["provider"],
                entry["model"],
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        self._saved_usd += saved
        age = max(0.0, time.time() - float(entry.get("created_at") or time.time()))
        return CacheHit(
            kind=kind,
            completion=completion,
            provider=entry["provider"],
            model=entry["model"],
            similarity=similarity,
            saved_usd=saved,
            age_s=age,
        )

    # ------------------------------------------------------------------ write
    async def store(
        self,
        tenant: str,
        route: Route,
        payload: ChatCompletionRequest,
        target: RouteTarget,
        completion: ChatCompletion,
    ) -> None:
        if not self._enabled or completion.usage is None:
            return
        key = exact_key(tenant, target, payload)
        entry = {
            "provider": target.provider,
            "model": target.model,
            "completion": completion.model_dump(exclude_none=True),
            "created_at": time.time(),
        }
        await self._store.set(key, entry, self._ttl_s)
        self._counters["stores"] += 1

        if not self._semantic_enabled or self._embedder is None:
            return
        text = _last_user_text(payload)
        if text is None or len(payload.messages) != 1:
            return
        partition = self._partition(tenant, target, payload)
        bucket = self._semantic.setdefault(partition, [])
        bucket.append((key, self._embedder(text), time.monotonic()))
        if len(bucket) > self._max_per_partition:
            del bucket[: len(bucket) - self._max_per_partition]

    @staticmethod
    def _partition(tenant: str, target: RouteTarget, payload: ChatCompletionRequest) -> str:
        # Generation params are part of the partition on purpose: a semantic
        # hit must never answer a request that asked for something else
        # (max_tokens=5 vs 10 are different answers, not paraphrases).
        system = next(
            (
                m.content
                for m in payload.messages
                if m.role == "system" and isinstance(m.content, str)
            ),
            "",
        )
        system_digest = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
        params = json.dumps(
            {
                "temperature": payload.temperature,
                "max_tokens": payload.max_tokens,
                "top_p": payload.top_p,
                "stop": payload.stop,
            },
            sort_keys=True,
            default=str,
        )
        params_digest = hashlib.sha256(params.encode("utf-8")).hexdigest()[:12]
        return f"{tenant}|{target.provider}|{target.model}|{system_digest}|{params_digest}"

    # ------------------------------------------------------------------- ops
    def set_price_book(self, price_book: PriceBook) -> None:
        """Keep hit accounting in sync when prices change under a running gateway."""
        self._price_book = price_book

    def stats(self) -> dict[str, Any]:
        hits = self._counters["hits_exact"] + self._counters["hits_semantic"]
        attempts = hits + self._counters["misses"]
        entries = len(self._store) if hasattr(self._store, "__len__") else None
        store_name = type(self._store).__name__
        return {
            "enabled": self._enabled,
            "semantic_enabled": self._semantic_enabled,
            "semantic_threshold": self._threshold,
            "ttl_s": self._ttl_s,
            "backend": _BACKEND_NAMES.get(store_name, store_name.lower()),
            "entries": entries,
            "semantic_partitions": len(self._semantic),
            "hit_rate": round(hits / attempts, 4) if attempts else 0.0,
            "saved_usd": round(self._saved_usd, 6),
            **self._counters,
        }

    async def purge(self) -> int:
        purge = getattr(self._store, "purge", None)
        removed = int(await purge()) if purge is not None else 0
        self._semantic.clear()
        self._counters = dict.fromkeys(self._counters, 0)
        self._saved_usd = 0.0
        return removed

    async def close(self) -> None:
        await self._store.close()

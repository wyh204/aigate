"""Where cached answers live.

Two backends, one protocol. The in-process store is the default because a demo
should not need Redis to start; Redis is what a real deployment uses so the hit
rate is shared across nodes. Neither knows what a chat completion is — they just
store opaque JSON by key.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Protocol


class CacheStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def close(self) -> None: ...


class MemoryStore:
    """LRU + TTL, bounded by entry count. Single event loop, so no locks."""

    def __init__(self, max_entries: int = 2048) -> None:
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._max_entries = max(1, max_entries)
        self.evictions = 0

    async def get(self, key: str) -> dict[str, Any] | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return value

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        expires_at = time.monotonic() + ttl_s if ttl_s > 0 else float("inf")
        self._items[key] = (expires_at, value)
        self._items.move_to_end(key)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)
            self.evictions += 1

    async def delete(self, key: str) -> None:
        self._items.pop(key, None)

    async def purge(self) -> int:
        count = len(self._items)
        self._items.clear()
        return count

    async def close(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class RedisStore:
    """Shared cache. Requires ``redis`` installed (``pip install redis``)."""

    def __init__(self, url: str, *, prefix: str = "aigate:cache:") -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "AIGATE_REDIS_URL is set but the 'redis' package is not installed; "
                "run `pip install redis` or unset AIGATE_REDIS_URL to use the in-process store"
            ) from exc
        self._redis = Redis.from_url(url, decode_responses=True)
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> dict[str, Any] | None:
        import json

        raw = await self._redis.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        import json

        await self._redis.set(
            self._key(key), json.dumps(value, ensure_ascii=False), ex=ttl_s or None
        )

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._key(key))

    async def purge(self) -> int:
        deleted = 0
        async for key in self._redis.scan_iter(match=f"{self._prefix}*"):
            deleted += int(await self._redis.delete(key))
        return deleted

    async def close(self) -> None:
        await self._redis.aclose()

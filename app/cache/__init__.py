"""M4: two cache layers — exact and semantic — behind one service.

The rule that keeps this honest: a cache hit must be indistinguishable from a
real answer to the client, and the dashboard must show what it saved. Everything
here is written with that in mind.
"""

from app.cache.embedding import cosine, embed
from app.cache.service import CacheHit, CacheService
from app.cache.store import CacheStore, MemoryStore, RedisStore

__all__ = [
    "CacheHit",
    "CacheService",
    "CacheStore",
    "MemoryStore",
    "RedisStore",
    "cosine",
    "embed",
]

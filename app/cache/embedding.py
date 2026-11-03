"""A deterministic, dependency-free text embedding.

Why not call an embedding API? Because the interesting question in a semantic
cache is not "can you call an embedding endpoint", it is "what do you do when a
near-duplicate question has a different answer". A local hashing embedder keeps
the demo runnable offline, costs nothing, and makes the false-hit behaviour
reproducible. Swap :class:`Embedder` for a real model later; the cache only
depends on ``embed`` + ``cosine``.

Tokens are latin words plus CJK characters *and* CJK bigrams: single characters
give recall, bigrams give enough context that "价格" and "价值" do not collapse
into the same vector.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable

DIMENSIONS = 384

_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

Embedder = Callable[[str], list[float]]


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _WORD.findall(lowered)
    cjk = _CJK.findall(lowered)
    tokens.extend(cjk)
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:], strict=False))
    return tokens


def embed(text: str) -> list[float]:
    """Hashed bag of tokens, L2-normalised so cosine is a dot product."""
    vector = [0.0] * DIMENSIONS
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))

"""Token estimation.

Upstream ``usage`` always wins. This is the fallback for providers that do not
return usage on streaming responses, and for the mock provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

_CJK_RANGE = ("\u4e00", "\u9fff")


def estimate_tokens(text: str) -> int:
    """Rough, transparent heuristic: 1 token per CJK char, ~4 latin chars per token."""
    if not text:
        return 0
    cjk = sum(1 for char in text if _CJK_RANGE[0] <= char <= _CJK_RANGE[1])
    latin = len(text) - cjk
    return max(1, cjk + (latin + 3) // 4)


def messages_text(messages: Iterable[Any]) -> str:
    parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append(json.dumps(content, ensure_ascii=False))
    return "\n".join(parts)

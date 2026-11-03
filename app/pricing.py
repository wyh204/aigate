"""Cost accounting.

Prices are USD per 1M tokens, taken from public list prices and used only as a
starting point: they move, and contract prices are lower. Anything in the
``models`` table wins over :data:`DEFAULT_PRICES`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from app.db.models import ModelPrice
from app.db.session import Database
from app.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("deepseek", "deepseek-chat"): (0.27, 1.10),
    ("deepseek", "deepseek-reasoner"): (0.55, 2.19),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("qwen", "qwen-plus"): (0.11, 0.28),
    ("qwen", "qwen-turbo"): (0.05, 0.20),
    ("mock", "mock-echo"): (0.0, 0.0),
}


@dataclass
class PriceBook:
    prices: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    _warned: set[tuple[str, str]] = field(default_factory=set, repr=False)

    @classmethod
    async def load(cls, database: Database) -> PriceBook:
        prices = dict(DEFAULT_PRICES)
        async with database.session() as session:
            for row in await session.scalars(select(ModelPrice)):
                prices[(row.provider, row.model)] = (
                    row.input_usd_per_1m,
                    row.output_usd_per_1m,
                )
        return cls(prices)

    @classmethod
    def builtin(cls) -> PriceBook:
        """Price book with no database attached, for CLI eval runs."""
        return cls(dict(DEFAULT_PRICES))

    def cost_usd(
        self, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> float:
        price = self.prices.get((provider, model))
        if price is None:
            # Unknown model: report zero and warn once. Silently inventing a
            # price would corrupt every cost number downstream.
            if (provider, model) not in self._warned:
                self._warned.add((provider, model))
                logger.warning(
                    "no price registered",
                    extra={"event": "price_missing", "provider": provider, "model": model},
                )
            return 0.0
        input_price, output_price = price
        return round(
            (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000, 8
        )

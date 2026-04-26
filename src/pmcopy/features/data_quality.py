from __future__ import annotations

from collections import Counter
from typing import Iterable


QUALITY_RANKS = {
    "exact_orderbook": 4,
    "price_history_proxy": 3,
    "midpoint_proxy": 2,
    "last_price_proxy": 1,
    "insufficient_data": 0,
}


def quality_rank(level: str | None) -> int:
    return QUALITY_RANKS.get(level or "insufficient_data", 0)


def allowed_quality_set(levels: Iterable[str] | None) -> set[str]:
    if levels is None:
        return {"exact_orderbook", "price_history_proxy"}
    return {level for level in levels if level in QUALITY_RANKS}


def quality_breakdown(levels: Iterable[str]) -> dict[str, float]:
    counts = Counter(levels)
    total = sum(counts.values())
    if total == 0:
        return {level: 0.0 for level in QUALITY_RANKS}
    return {level: counts.get(level, 0) / total for level in QUALITY_RANKS}


def passes_quality_gate(level: str, allowed_levels: Iterable[str] | None) -> bool:
    return level in allowed_quality_set(allowed_levels)

from __future__ import annotations

from typing import Any


def fee_rate_for_category(config: dict[str, Any], category: str | None, override: float | None = None) -> tuple[float, str | None]:
    if override is not None:
        return float(override), None
    fees = config.get("fees", {})
    default_rate = float(fees.get("default_fee_rate", 0.04))
    if not category:
        return default_rate, "category unknown; using default fee rate"
    by_category = fees.get("fee_rates_by_category", {})
    if category in by_category:
        return float(by_category[category]), None
    return default_rate, f"category {category!r} has no configured fee rate; using default"


def polymarket_fee(shares: float, price: float, fee_rate: float) -> float:
    if shares <= 0 or price < 0 or price > 1:
        return 0.0
    return shares * fee_rate * price * (1 - price)


def round_trip_fee(shares: float, entry_price: float, exit_price: float | None, fee_rate: float) -> float:
    fee = polymarket_fee(shares, entry_price, fee_rate)
    if exit_price is not None:
        fee += polymarket_fee(shares, exit_price, fee_rate)
    return fee

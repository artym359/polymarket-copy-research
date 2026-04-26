from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FillResult:
    average_price: float | None
    available_liquidity: float
    slippage: float | None
    fill_possible: bool


def parse_orderbook_levels(levels: list[dict[str, Any]] | None, *, reverse: bool) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    for level in levels or []:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0 or size <= 0:
            continue
        parsed.append((price, size))
    return sorted(parsed, key=lambda item: item[0], reverse=reverse)


def best_bid_ask(book: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    bids = parse_orderbook_levels(book.get("bids"), reverse=True)
    asks = parse_orderbook_levels(book.get("asks"), reverse=False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    midpoint = ((best_bid + best_ask) / 2) if best_bid is not None and best_ask is not None else None
    return best_bid, best_ask, spread, midpoint


def simulate_orderbook_fill(side: str, size_usd: float, book: dict[str, Any]) -> FillResult:
    normalized_side = (side or "").upper()
    if normalized_side == "BUY":
        levels = parse_orderbook_levels(book.get("asks"), reverse=False)
    elif normalized_side == "SELL":
        levels = parse_orderbook_levels(book.get("bids"), reverse=True)
    else:
        return FillResult(None, 0.0, None, False)

    if size_usd <= 0 or not levels:
        return FillResult(None, 0.0, None, False)

    remaining_usd = size_usd
    total_shares = 0.0
    total_usd = 0.0
    available_liquidity = sum(price * shares for price, shares in levels)
    top_price = levels[0][0]

    for price, shares_available in levels:
        level_usd = price * shares_available
        take_usd = min(remaining_usd, level_usd)
        if take_usd <= 0:
            continue
        shares_taken = take_usd / price
        total_shares += shares_taken
        total_usd += take_usd
        remaining_usd -= take_usd
        if remaining_usd <= 1e-9:
            break

    fill_possible = remaining_usd <= 1e-6 and total_shares > 0
    average_price = total_usd / total_shares if total_shares > 0 else None
    slippage = abs(average_price - top_price) if average_price is not None else None
    return FillResult(average_price, available_liquidity, slippage, fill_possible)


def proxy_slippage(price: float, proxy_slippage_bps: float) -> float:
    return price * proxy_slippage_bps / 10_000

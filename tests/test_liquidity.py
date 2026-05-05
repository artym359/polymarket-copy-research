from __future__ import annotations

from pmcopy.features.liquidity import best_bid_ask, simulate_orderbook_fill


def test_orderbook_fill_buy_walks_asks() -> None:
    book = {
        "asks": [{"price": "0.50", "size": "2"}, {"price": "0.60", "size": "5"}],
        "bids": [{"price": "0.49", "size": "10"}],
    }
    fill = simulate_orderbook_fill("BUY", 2.2, book)
    assert fill.fill_possible is True
    assert round(fill.average_price or 0, 6) == round(2.2 / (2 + 1.2 / 0.6), 6)
    assert fill.available_liquidity == 4.0


def test_best_bid_ask_spread() -> None:
    book = {"asks": [{"price": "0.55", "size": "1"}], "bids": [{"price": "0.50", "size": "1"}]}
    assert best_bid_ask(book) == (0.5, 0.55, 0.050000000000000044, 0.525)

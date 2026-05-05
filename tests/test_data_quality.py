from __future__ import annotations

from pmcopy.features.data_quality import passes_quality_gate, quality_breakdown, quality_rank


def test_quality_ranks_and_gate() -> None:
    assert quality_rank("exact_orderbook") == 4
    assert quality_rank("insufficient_data") == 0
    assert passes_quality_gate("price_history_proxy", ["exact_orderbook", "price_history_proxy"]) is True
    assert passes_quality_gate("last_price_proxy", ["exact_orderbook", "price_history_proxy"]) is False


def test_quality_breakdown() -> None:
    breakdown = quality_breakdown(["exact_orderbook", "price_history_proxy", "price_history_proxy"])
    assert breakdown["exact_orderbook"] == 1 / 3
    assert breakdown["price_history_proxy"] == 2 / 3
    assert breakdown["insufficient_data"] == 0

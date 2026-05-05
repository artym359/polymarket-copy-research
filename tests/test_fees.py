from __future__ import annotations

from pmcopy.features.fees import fee_rate_for_category, polymarket_fee


def test_polymarket_fee_formula() -> None:
    assert polymarket_fee(shares=10, price=0.5, fee_rate=0.04) == 0.1


def test_fee_rate_category_fallback() -> None:
    config = {"fees": {"default_fee_rate": 0.04, "fee_rates_by_category": {"sports": 0.03}}}
    assert fee_rate_for_category(config, "sports") == (0.03, None)
    rate, warning = fee_rate_for_category(config, None)
    assert rate == 0.04
    assert warning is not None

from __future__ import annotations

import pandas as pd

from pmcopy.features.wallet_filters import apply_wallet_filters


def test_apply_wallet_filters_excludes_classes_and_categories() -> None:
    df = pd.DataFrame(
        [
            {
                "wallet": "a",
                "total_pnl": 10,
                "roi_on_volume": 0.1,
                "volume": 1000,
                "trade_count": 120,
                "market_count": 25,
                "active_days": 20,
                "max_drawdown": None,
                "top_1_market_pnl_share": 0.2,
                "top_5_market_pnl_share": 0.6,
                "categories": ["sports"],
                "class_label": "likely_directional",
            },
            {
                "wallet": "b",
                "total_pnl": 20,
                "roi_on_volume": 0.01,
                "volume": 2_000_000,
                "trade_count": 6000,
                "market_count": 120,
                "active_days": 90,
                "max_drawdown": None,
                "top_1_market_pnl_share": 0.1,
                "top_5_market_pnl_share": 0.3,
                "categories": ["crypto"],
                "class_label": "likely_market_maker",
            },
        ]
    )

    filtered = apply_wallet_filters(
        df,
        {
            "min_total_pnl": 0,
            "min_roi_on_volume": None,
            "min_total_volume": 500,
            "min_trades": 100,
            "min_markets": 20,
            "min_active_days": 14,
            "max_drawdown": None,
            "max_top_1_market_pnl_share": 0.6,
            "max_top_5_market_pnl_share": 0.85,
            "include_categories": ["sports"],
            "exclude_categories": [],
            "exclude_likely_market_makers": True,
            "exclude_lucky_wallets": True,
            "exclude_insufficient_sample": True,
        },
    )

    assert filtered["wallet"].tolist() == ["a"]


def test_apply_wallet_filters_supports_edge_exposure_and_roi_alias() -> None:
    df = pd.DataFrame(
        [
            {
                "wallet": "a",
                "total_pnl": 10,
                "edge_on_volume": 0.10,
                "roi_on_volume": 0.10,
                "volume": 100,
                "trade_count": 10,
                "market_count": 4,
                "active_days": 3,
                "max_drawdown": None,
                "top_1_market_pnl_share": 0.2,
                "top_5_market_pnl_share": 0.4,
                "return_on_max_capital_at_risk": 0.50,
                "return_on_average_capital_at_risk": 0.40,
                "max_exposure_confidence": "reconstructed_positions",
                "average_exposure_confidence": "reconstructed_positions_time_weighted",
                "categories": ["sports"],
                "class_label": "likely_directional",
            },
            {
                "wallet": "b",
                "total_pnl": 8,
                "edge_on_volume": 0.02,
                "roi_on_volume": 0.02,
                "volume": 400,
                "trade_count": 20,
                "market_count": 5,
                "active_days": 4,
                "max_drawdown": None,
                "top_1_market_pnl_share": 0.2,
                "top_5_market_pnl_share": 0.4,
                "return_on_max_capital_at_risk": 0.10,
                "return_on_average_capital_at_risk": 0.08,
                "max_exposure_confidence": "data_api_proxy",
                "average_exposure_confidence": "unavailable",
                "categories": ["sports"],
                "class_label": "likely_directional",
            },
        ]
    )

    filtered = apply_wallet_filters(
        df,
        {
            "min_edge_on_volume": 0.05,
            "min_return_on_max_capital_at_risk": 0.20,
            "min_return_on_average_capital_at_risk": 0.20,
            "exposure_confidence_filter": ["reconstructed_positions"],
            "exclude_likely_market_makers": True,
            "exclude_lucky_wallets": True,
            "exclude_insufficient_sample": True,
        },
    )
    assert filtered["wallet"].tolist() == ["a"]

    alias_filtered = apply_wallet_filters(df.drop(columns=["edge_on_volume"]), {"min_roi_on_volume": 0.05})
    assert alias_filtered["wallet"].tolist() == ["a"]

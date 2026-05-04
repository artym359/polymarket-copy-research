from __future__ import annotations

from pmcopy.db import WalletMetrics, json_dumps
from pmcopy.features.classification import classify_wallet_metrics


def base_config() -> dict:
    return {
        "wallet_filters": {
            "min_trades": 100,
            "min_markets": 20,
            "min_active_days": 14,
        },
        "classification": {
            "likely_market_maker": {
                "min_volume": 1_000_000,
                "min_trades": 5_000,
                "max_roi_on_volume": 0.01,
                "both_sides_same_market_threshold": 0.25,
            },
            "lucky_wallet": {
                "max_trades": 100,
                "max_markets": 20,
                "min_top_1_market_pnl_share": 0.60,
            },
        },
    }


def test_classification_insufficient_sample_priority() -> None:
    metrics = WalletMetrics(
        wallet_address="0xabc",
        trade_count=5,
        market_count=2,
        active_days=1,
        total_volume=100,
        metrics_json=json_dumps({}),
    )
    result = classify_wallet_metrics(metrics, base_config())
    assert result.class_label == "insufficient_sample"
    assert result.insufficient_sample_flag is True
    assert result.latency_bot_score is None


def test_classification_market_maker() -> None:
    metrics = WalletMetrics(
        wallet_address="0xabc",
        trade_count=6000,
        market_count=120,
        active_days=60,
        total_volume=2_000_000,
        roi_on_volume=0.005,
        metrics_json=json_dumps({"both_side_market_share": 0.4}),
    )
    result = classify_wallet_metrics(metrics, base_config())
    assert result.class_label == "likely_market_maker"
    assert result.market_maker_score >= 0.65


def test_classification_directional() -> None:
    metrics = WalletMetrics(
        wallet_address="0xabc",
        trade_count=200,
        market_count=30,
        active_days=45,
        total_volume=50_000,
        roi_on_volume=0.08,
        top_1_market_pnl_share=0.2,
        metrics_json=json_dumps({"both_side_market_share": 0.0}),
    )
    result = classify_wallet_metrics(metrics, base_config())
    assert result.class_label == "likely_directional"
    assert result.directional_score >= 0.5

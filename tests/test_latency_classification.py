from __future__ import annotations

from datetime import datetime, timezone

from pmcopy.db import AlphaDecayResult, WalletMetrics, json_dumps
from pmcopy.features.classification import classify_wallet_metrics


def test_latency_classification_after_alpha_decay() -> None:
    metrics = WalletMetrics(
        wallet_address="0xabc",
        trade_count=200,
        market_count=30,
        active_days=30,
        total_volume=10_000,
        roi_on_volume=0.02,
        metrics_json=json_dumps({}),
    )
    rows = [
        AlphaDecayResult(
            id=f"a{i}",
            wallet_address="0xabc",
            trade_id=f"t{i}",
            delay_seconds=delay,
            trade_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            copy_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            exit_rule="fixed_24h",
            net_pnl=pnl,
            data_quality="price_history_proxy",
            data_quality_rank=3,
        )
        for i, (delay, pnl) in enumerate([(10, 1.0), (10, 0.5), (60, -0.2), (60, -0.1), (300, -0.1)])
    ]
    config = {
        "wallet_filters": {"min_trades": 100, "min_markets": 20, "min_active_days": 14},
        "classification": {
            "likely_market_maker": {"min_volume": 1_000_000, "min_trades": 5_000, "max_roi_on_volume": 0.01},
            "lucky_wallet": {"max_trades": 100, "max_markets": 20, "min_top_1_market_pnl_share": 0.60},
        },
    }

    result = classify_wallet_metrics(metrics, config, alpha_rows=rows)

    assert result.class_label == "likely_latency_bot"
    assert result.latency_bot_score is not None and result.latency_bot_score >= 0.65

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from pmcopy.backtest.sensitivity import (
    SensitivityGrid,
    apply_parameter_variant,
    enforce_grid_safety,
    generate_parameter_grid,
    prepare_heatmap_data,
    run_sensitivity,
    sensitivity_config_from_values,
    warning_flags,
)
from pmcopy.backtest.simulator import backtest_config_from_values
from pmcopy.db import AlphaDecayResult, BacktestRun, Market, SensitivityResult, SensitivityRun, init_db, json_dumps, session_scope
from pmcopy.features.data_quality import quality_rank


def base_config(tmp_path: Path) -> dict:
    return {
        "app": {"database_url": f"sqlite:///{(tmp_path / 'sensitivity.sqlite3').as_posix()}"},
        "backtest": {
            "mode": "in_sample",
            "initial_capital": 100,
            "position_size_usd": 2,
            "copy_delay_seconds": 60,
            "max_entry_degradation": 0.03,
            "max_spread": 0.03,
            "max_market_exposure_usd": 100,
            "max_wallet_exposure_usd": 100,
            "max_category_exposure_usd": 100,
            "max_daily_loss_usd": 100,
            "duplicate_signal_window_seconds": 0,
            "exit_rule": "latest_available",
            "skip_likely_market_makers": False,
            "skip_likely_latency_bots": False,
            "skip_lucky_wallets": False,
            "skip_insufficient_sample": False,
            "allowed_data_quality_levels": ["price_history_proxy"],
        },
        "sensitivity": {
            "large_grid_threshold": 10,
            "too_few_trades_threshold": 2,
            "high_drawdown_fraction": 0.2,
            "high_skipped_signal_rate": 0.5,
            "high_proxy_data_share": 0.5,
            "concentration_threshold": 0.8,
        },
        "walk_forward": {
            "lookback_days": 10,
            "test_window_days": 2,
            "rebalance_frequency_days": 2,
            "min_trades_in_lookback": 1,
        },
    }


def add_alpha(config: dict, *, delay: int, trade_id: str, net_pnl: float, wallet: str = "0xa") -> None:
    trade_time = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=delay)
    copy_time = trade_time + timedelta(seconds=delay)
    exit_time = copy_time + timedelta(hours=1)
    with session_scope(config["app"]["database_url"]) as session:
        session.merge(Market(market_id="m1", category="sports"))
        session.merge(
            AlphaDecayResult(
                id=f"alpha-{trade_id}-{delay}",
                wallet_address=wallet,
                trade_id=trade_id,
                token_id="token",
                market_id="m1",
                trade_time=trade_time,
                original_side="BUY",
                whale_price=0.5,
                whale_size=10,
                delay_seconds=delay,
                copy_time=copy_time,
                copy_spread=0.01,
                simulated_entry_price=0.5,
                entry_degradation=0.01,
                liquidity_available=10,
                estimated_fee=0.01,
                estimated_slippage=0.0,
                eventual_exit_price=0.6,
                exit_rule="latest_available",
                gross_pnl=net_pnl + 0.01,
                net_pnl=net_pnl,
                data_quality="price_history_proxy",
                data_quality_rank=quality_rank("price_history_proxy"),
                raw_json=json_dumps({"position_size_usd": 2, "debug": {"exit_time": exit_time.isoformat()}}),
            )
        )


def test_sensitivity_grid_generation_and_large_grid_safety() -> None:
    grid = SensitivityGrid([60, 300], [0.02, 0.03], [0.03], [2], [8])
    combinations = generate_parameter_grid(grid)
    assert len(combinations) == 4
    assert combinations[0]["copy_delay_seconds"] == 60

    with pytest.raises(ValueError):
        enforce_grid_safety(combinations, limit_combinations=None, confirm_large_grid=False, large_grid_threshold=2)
    assert len(enforce_grid_safety(combinations, limit_combinations=2, confirm_large_grid=False, large_grid_threshold=2)) == 2


def test_parameter_override_logic(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    base = backtest_config_from_values(config, selected_wallets=["0xa"])
    variant = apply_parameter_variant(
        base,
        {
            "copy_delay_seconds": 300,
            "max_entry_degradation": 0.02,
            "max_spread": 0.05,
            "position_size_usd": 5,
            "max_market_exposure_usd": 10,
        },
    )
    assert variant.copy_delay_seconds == 300
    assert variant.max_entry_degradation == 0.02
    assert variant.max_spread == 0.05
    assert variant.position_size_usd == 5
    assert variant.max_market_exposure_usd == 10


def test_sensitivity_reuses_backtest_engine_and_stores_results(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    init_db(config)
    add_alpha(config, delay=60, trade_id="t60", net_pnl=0.4)
    add_alpha(config, delay=300, trade_id="t300", net_pnl=-0.2)
    sensitivity_config = sensitivity_config_from_values(
        config,
        selected_wallets=["0xa"],
        copy_delays=[60, 300],
        max_entry_degradations=[0.03],
        max_spreads=[0.03],
        position_sizes=[2],
        max_market_exposures=[8],
        allowed_data_quality=["price_history_proxy"],
        exit_rule="latest_available",
    )

    result = run_sensitivity(config, sensitivity_config)

    assert result["tested_combinations"] == 2
    with session_scope(config["app"]["database_url"]) as session:
        assert len(list(session.scalars(select(SensitivityRun)))) == 1
        assert len(list(session.scalars(select(SensitivityResult)))) == 2
        assert len(list(session.scalars(select(BacktestRun)))) == 2


def test_warning_flag_logic_detects_instability(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    sensitivity_config = sensitivity_config_from_values(config, selected_wallets=["0xa"])
    metrics = {
        "total_pnl": -1,
        "roi": -0.01,
        "max_drawdown": 25,
        "trade_count": 1,
        "skipped_signal_reasons": {"duplicate_signal": 10},
        "data_quality_summary": {"percent": {"price_history_proxy": 1.0}},
        "pnl_by_wallet": {"0xa": -1},
        "pnl_by_market": {"m1": -1},
        "pnl_by_category": {"sports": -1},
    }

    flags = warning_flags(metrics, {}, sensitivity_config)

    assert "too_few_trades" in flags
    assert "negative_roi" in flags
    assert "high_drawdown" in flags
    assert "high_skipped_signal_rate" in flags
    assert "high_proxy_data_share" in flags
    assert "result_depends_on_one_wallet" in flags
    assert "result_depends_on_one_market" in flags
    assert "result_depends_on_one_category" in flags


def test_heatmap_data_preparation() -> None:
    rows = [
        {"copy_delay_seconds": 60, "max_entry_degradation": 0.02, "roi": 0.1},
        {"copy_delay_seconds": 60, "max_entry_degradation": 0.02, "roi": 0.3},
        {"copy_delay_seconds": 300, "max_entry_degradation": 0.03, "roi": -0.1},
    ]

    heatmap = prepare_heatmap_data(rows, "roi")

    assert heatmap[0] == {"copy_delay_seconds": 60, "max_entry_degradation": 0.02, "roi": 0.2}
    assert heatmap[1] == {"copy_delay_seconds": 300, "max_entry_degradation": 0.03, "roi": -0.1}

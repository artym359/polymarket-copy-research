from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from pmcopy.backtest.simulator import backtest_config_from_values, run_backtest
from pmcopy.db import AlphaDecayResult, BacktestRun, BacktestTrade, Market, SkippedSignal, init_db, json_dumps, session_scope
from pmcopy.features.data_quality import quality_rank


def base_config(tmp_path: Path) -> dict:
    db_path = tmp_path / "pmcopy_test.sqlite3"
    return {
        "app": {"database_url": f"sqlite:///{db_path.as_posix()}"},
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
            "min_copyability_score": 0,
        },
        "walk_forward": {
            "lookback_days": 10,
            "test_window_days": 2,
            "rebalance_frequency_days": 2,
            "min_trades_in_lookback": 1,
            "selection_metric": "copyability_score",
            "top_wallets": 1,
        },
    }


def seed_market(config: dict, market_id: str, category: str = "sports") -> None:
    with session_scope(config["app"]["database_url"]) as session:
        if session.get(Market, market_id) is None:
            session.add(Market(market_id=market_id, category=category))


def add_alpha(
    config: dict,
    *,
    wallet: str,
    trade_id: str,
    trade_time: datetime,
    net_pnl: float,
    market_id: str = "m1",
    token_id: str = "token1",
    category: str = "sports",
    quality: str = "price_history_proxy",
    side: str = "BUY",
) -> None:
    seed_market(config, market_id, category)
    copy_time = trade_time + timedelta(seconds=60)
    exit_time = copy_time + timedelta(hours=1)
    fee = 0.01
    with session_scope(config["app"]["database_url"]) as session:
        session.merge(
            AlphaDecayResult(
                id=f"alpha-{trade_id}",
                wallet_address=wallet,
                trade_id=trade_id,
                token_id=token_id,
                market_id=market_id,
                trade_time=trade_time,
                original_side=side,
                whale_price=0.5,
                whale_size=10,
                delay_seconds=60,
                copy_time=copy_time,
                copy_spread=0.01,
                simulated_entry_price=0.5,
                entry_degradation=0.01,
                liquidity_available=2,
                estimated_fee=fee,
                estimated_slippage=0.0,
                eventual_exit_price=0.6,
                exit_rule="latest_available",
                gross_pnl=net_pnl + fee,
                net_pnl=net_pnl,
                data_quality=quality,
                data_quality_rank=quality_rank(quality),
                skip_reason=None,
                raw_json=json_dumps({"position_size_usd": 2, "debug": {"exit_time": exit_time.isoformat()}}),
            )
        )


def test_backtest_accounting_equity_drawdown_and_aggregations(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    init_db(config)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    add_alpha(config, wallet="0xa", trade_id="t1", trade_time=start, net_pnl=1.5, market_id="m1", category="sports")
    add_alpha(config, wallet="0xa", trade_id="t2", trade_time=start + timedelta(hours=2), net_pnl=-0.5, market_id="m2", category="crypto")

    bt_config = backtest_config_from_values(config, selected_wallets=["0xa"])
    result = run_backtest(config, bt_config)
    metrics = result["metrics"]

    assert metrics["trade_count"] == 2
    assert metrics["total_pnl"] == 1.0
    assert metrics["roi"] == 0.01
    assert metrics["win_rate"] == 0.5
    assert metrics["max_drawdown"] == 0.5
    assert metrics["pnl_by_wallet"]["0xa"] == 1.0
    assert metrics["pnl_by_category"]["sports"] == 1.5
    assert metrics["pnl_by_market"]["m2"] == -0.5

    with session_scope(config["app"]["database_url"]) as session:
        assert session.scalar(select(BacktestRun).where(BacktestRun.run_id == result["run_id"])) is not None
        assert len(list(session.scalars(select(BacktestTrade)))) == 2


def test_backtest_exposure_limit_and_skipped_signal_logging(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    init_db(config)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    add_alpha(config, wallet="0xa", trade_id="t1", trade_time=start, net_pnl=0.5, market_id="m1")
    add_alpha(config, wallet="0xa", trade_id="t2", trade_time=start + timedelta(minutes=5), net_pnl=0.5, market_id="m2")

    bt_config = backtest_config_from_values(config, selected_wallets=["0xa"], max_wallet_exposure_usd=2)
    result = run_backtest(config, bt_config)

    assert result["metrics"]["trade_count"] == 1
    assert result["metrics"]["skipped_signal_reasons"] == {"max_wallet_exposure_exceeded": 1}
    with session_scope(config["app"]["database_url"]) as session:
        skipped = list(session.scalars(select(SkippedSignal)))
    assert skipped[0].reason == "max_wallet_exposure_exceeded"


def test_duplicate_signal_cluster_skips_later_signal(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    init_db(config)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    add_alpha(config, wallet="0xa", trade_id="t1", trade_time=start, net_pnl=0.5, market_id="m1", token_id="same")
    add_alpha(config, wallet="0xb", trade_id="t2", trade_time=start + timedelta(minutes=3), net_pnl=0.5, market_id="m1", token_id="same")

    bt_config = backtest_config_from_values(
        config,
        selected_wallets=["0xa", "0xb"],
        duplicate_signal_window_seconds=600,
    )
    result = run_backtest(config, bt_config)

    assert result["metrics"]["trade_count"] == 1
    assert result["metrics"]["skipped_signal_reasons"] == {"duplicate_signal": 1}


def test_backtest_data_quality_gate_logs_disallowed_quality(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    init_db(config)
    add_alpha(
        config,
        wallet="0xa",
        trade_id="t1",
        trade_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        net_pnl=0.5,
        quality="last_price_proxy",
    )

    bt_config = backtest_config_from_values(config, selected_wallets=["0xa"], allowed_data_quality=["price_history_proxy"])
    result = run_backtest(config, bt_config)

    assert result["candidate_count"] == 1
    assert result["metrics"]["trade_count"] == 0
    assert result["metrics"]["skipped_signal_reasons"] == {"data_quality_not_allowed": 1}


def test_split_mode_reports_train_validation_test_metrics(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    init_db(config)
    add_alpha(config, wallet="0xa", trade_id="train", trade_time=datetime(2026, 1, 2, tzinfo=timezone.utc), net_pnl=1.0)
    add_alpha(config, wallet="0xa", trade_id="validation", trade_time=datetime(2026, 1, 12, tzinfo=timezone.utc), net_pnl=-1.0)
    add_alpha(config, wallet="0xa", trade_id="test", trade_time=datetime(2026, 1, 22, tzinfo=timezone.utc), net_pnl=0.5)

    bt_config = backtest_config_from_values(
        config,
        mode="split",
        selected_wallets=["0xa"],
        train_start="2026-01-01",
        train_end="2026-01-10",
        validation_start="2026-01-11",
        validation_end="2026-01-20",
        test_start="2026-01-21",
        test_end="2026-01-30",
    )
    result = run_backtest(config, bt_config)

    assert result["periods"]["train"]["total_pnl"] == 1.0
    assert result["periods"]["validation"]["total_pnl"] == -1.0
    assert result["periods"]["test"]["total_pnl"] == 0.5
    assert "strategy positive in train but negative in validation" in result["warnings"]


def test_walk_forward_uses_only_past_rows_for_wallet_selection(tmp_path: Path) -> None:
    config = base_config(tmp_path)
    init_db(config)
    add_alpha(config, wallet="0xa", trade_id="a-lookback", trade_time=datetime(2026, 1, 5, tzinfo=timezone.utc), net_pnl=1.0)
    add_alpha(config, wallet="0xa", trade_id="a-test", trade_time=datetime(2026, 1, 11, tzinfo=timezone.utc), net_pnl=0.5)
    add_alpha(config, wallet="0xb", trade_id="b-future", trade_time=datetime(2026, 1, 11, tzinfo=timezone.utc), net_pnl=100.0)

    bt_config = backtest_config_from_values(
        config,
        mode="walk_forward",
        date_start="2026-01-10",
        date_end="2026-01-12",
    )
    result = run_backtest(config, bt_config)

    assert result["walk_forward_windows"][0]["selected_wallets"] == ["0xa"]
    assert result["metrics"]["trade_count"] == 1
    assert result["metrics"]["total_pnl"] == 0.5

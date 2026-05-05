from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from pmcopy.backtest.lifecycle_copy import LifecycleCopyConfig, run_lifecycle_copy_in_session
from pmcopy.backtest.simulator import backtest_config_from_values, run_backtest
from pmcopy.db import (
    Base,
    LifecycleCopyEvent,
    LifecycleCopyPosition,
    Market,
    PriceHistory,
    ReconstructedPosition,
    Trade,
    init_db,
    json_loads,
    session_scope,
)
from pmcopy.features.position_reconstruction import ReconstructionConfig, reconstruct_wallet_positions_in_session


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


class EmptyClob:
    pass


def config() -> dict:
    return {
        "api": {"clob_base_url": "https://clob.polymarket.com"},
        "alpha_decay": {"max_price_history_distance_seconds": 60, "historical_mode": "price_history_only"},
        "fees": {"default_fee_rate": 0.0},
        "sizing": {
            "default_lifecycle_sizing_mode": "proportional_to_whale_with_cap",
            "copy_ratio": 0.001,
            "max_position_budget_usd": 10,
            "min_trade_usd": 0,
            "execute_small_trades": False,
            "allow_position_cap_partial_fill": True,
        },
        "backtest": {
            "copy_mode": "reconstructed_wallet_lifecycle",
            "mode": "in_sample",
            "initial_capital": 100,
            "copy_delay_seconds": 60,
            "entry_delay_seconds": 60,
            "exit_delay_seconds": 60,
            "allowed_data_quality_levels": ["price_history_proxy"],
            "skip_likely_market_makers": False,
            "skip_likely_latency_bots": False,
            "skip_lucky_wallets": False,
            "skip_insufficient_sample": False,
        },
    }


def tmp_config(tmp_path: Path) -> dict:
    cfg = config()
    cfg["app"] = {"database_url": f"sqlite:///{(tmp_path / 'pmcopy.sqlite3').as_posix()}"}
    return cfg


def add_trade(session: Session, trade_id: str, side: str, timestamp: datetime, *, size: float, price: float = 0.5) -> None:
    session.add(
        Trade(
            id=trade_id,
            wallet_address="0xabc",
            market_id="m1",
            token_id="token",
            side=side,
            price=price,
            size=size,
            usd_value=size * price,
            timestamp=timestamp,
        )
    )


def add_price(session: Session, price_id: str, timestamp: datetime, price: float) -> None:
    session.add(PriceHistory(id=price_id, token_id="token", timestamp=timestamp, price=price))


def seed_two_buys_full_sell(session: Session, *, cap: float = 10, sell_parts: list[float] | None = None) -> LifecycleCopyConfig:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(Market(market_id="m1", category="sports"))
    add_trade(session, "buy-4000", "BUY", now, size=8000, price=0.5)
    add_trade(session, "buy-3000", "BUY", now + timedelta(minutes=10), size=6000, price=0.5)
    sell_sizes = sell_parts or [14000]
    for index, size in enumerate(sell_sizes, start=1):
        add_trade(session, f"sell-{index}", "SELL", now + timedelta(hours=index), size=size, price=0.7)
    add_price(session, "entry-1", now + timedelta(seconds=60), 0.5)
    add_price(session, "entry-2", now + timedelta(minutes=10, seconds=60), 0.5)
    for index in range(1, len(sell_sizes) + 1):
        add_price(session, f"exit-{index}", now + timedelta(hours=index, seconds=60), 0.7)
    session.commit()
    reconstruct_wallet_positions_in_session(session, "0xabc", ReconstructionConfig())
    return LifecycleCopyConfig(
        copy_ratio=0.001,
        max_position_budget_usd=cap,
        min_trade_usd=0,
        entry_delay_seconds=60,
        exit_delay_seconds=60,
        allowed_data_quality=["price_history_proxy"],
    )


def lifecycle_events(session: Session) -> list[LifecycleCopyEvent]:
    return list(session.scalars(select(LifecycleCopyEvent).order_by(LifecycleCopyEvent.whale_time, LifecycleCopyEvent.id)))


def lifecycle_position(session: Session) -> LifecycleCopyPosition:
    return session.scalars(select(LifecycleCopyPosition)).one()


def test_proportional_to_whale_with_cap_uses_each_observed_buy_without_future_allocation() -> None:
    with make_session() as session:
        cfg = seed_two_buys_full_sell(session, cap=10)
        result = run_lifecycle_copy_in_session(session, config(), "run", cfg, ["0xabc"])
        events = lifecycle_events(session)
        position = lifecycle_position(session)

    buys = [event for event in events if event.copied_action == "buy"]
    assert [event.actual_copy_usd for event in buys] == [4, 3]
    assert result["cap_hit_count"] == 0
    assert position.status == "closed"
    assert position.copied_total_buy_usd == 7
    assert position.copied_realized_pnl == 2.8


def test_cap_limits_later_add_and_partial_exits_follow_whale_fraction() -> None:
    with make_session() as session:
        cfg = seed_two_buys_full_sell(session, cap=5, sell_parts=[7000, 7000])
        result = run_lifecycle_copy_in_session(session, config(), "run", cfg, ["0xabc"])
        events = lifecycle_events(session)
        position = lifecycle_position(session)

    buys = [event for event in events if event.copied_action == "buy"]
    sells = [event for event in events if event.copied_action == "sell"]
    assert [event.actual_copy_usd for event in buys] == [4, 1]
    assert result["cap_hit_count"] == 1
    assert [round(event.actual_copy_usd or 0, 4) for event in sells] == [3.5, 3.5]
    assert position.status == "closed"
    assert position.copied_total_buy_usd == 5


def test_min_trade_usd_skip() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "small-buy", "BUY", now, size=800, price=0.5)
        add_price(session, "entry", now + timedelta(seconds=60), 0.5)
        session.commit()
        reconstruct_wallet_positions_in_session(session, "0xabc", ReconstructionConfig())
        cfg = LifecycleCopyConfig(copy_ratio=0.001, max_position_budget_usd=10, min_trade_usd=1, allowed_data_quality=["price_history_proxy"])
        result = run_lifecycle_copy_in_session(session, config(), "run", cfg, ["0xabc"])
        events = lifecycle_events(session)
        position = lifecycle_position(session)

    assert result["below_min_trade_count"] == 1
    assert events[0].skip_reason == "below_min_trade_usd"
    assert position.status == "skipped"


def test_entry_and_exit_delays_are_applied_separately() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy", "BUY", now, size=8000, price=0.5)
        add_trade(session, "sell", "SELL", now + timedelta(hours=1), size=8000, price=0.7)
        add_price(session, "entry", now + timedelta(seconds=30), 0.5)
        add_price(session, "exit", now + timedelta(hours=1, seconds=120), 0.7)
        session.commit()
        reconstruct_wallet_positions_in_session(session, "0xabc", ReconstructionConfig())
        cfg = LifecycleCopyConfig(
            copy_ratio=0.001,
            max_position_budget_usd=10,
            min_trade_usd=0,
            entry_delay_seconds=30,
            exit_delay_seconds=120,
            allowed_data_quality=["price_history_proxy"],
        )
        run_lifecycle_copy_in_session(session, config(), "run", cfg, ["0xabc"])
        events = lifecycle_events(session)

    assert events[0].target_time.replace(tzinfo=timezone.utc) == now + timedelta(seconds=30)
    assert events[1].target_time.replace(tzinfo=timezone.utc) == now + timedelta(hours=1, seconds=120)


def test_missing_exit_price_marks_skip_and_open_lifecycle() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy", "BUY", now, size=8000, price=0.5)
        add_trade(session, "sell", "SELL", now + timedelta(hours=1), size=8000, price=0.7)
        add_price(session, "entry", now + timedelta(seconds=60), 0.5)
        session.commit()
        reconstruct_wallet_positions_in_session(session, "0xabc", ReconstructionConfig())
        cfg = LifecycleCopyConfig(copy_ratio=0.001, max_position_budget_usd=10, min_trade_usd=0, allowed_data_quality=["price_history_proxy"])
        result = run_lifecycle_copy_in_session(session, config(), "run", cfg, ["0xabc"])
        events = lifecycle_events(session)
        position = lifecycle_position(session)

    assert any(event.skip_reason in {"exit_price_missing", "no_price_history"} for event in events)
    assert position.status == "open"
    assert position.skip_reason == "lifecycle_not_closed"
    assert result["skipped_signal_reasons"]


def test_lifecycle_backtest_uses_lifecycle_tables(tmp_path: Path) -> None:
    cfg = tmp_config(tmp_path)
    init_db(cfg)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_scope(cfg["app"]["database_url"]) as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy-4000", "BUY", now, size=8000, price=0.5)
        add_trade(session, "sell-4000", "SELL", now + timedelta(hours=1), size=8000, price=0.7)
        add_price(session, "entry", now + timedelta(seconds=60), 0.5)
        add_price(session, "exit", now + timedelta(hours=1, seconds=60), 0.7)
        session.commit()
        reconstruct_wallet_positions_in_session(session, "0xabc", ReconstructionConfig())

    bt_cfg = backtest_config_from_values(
        cfg,
        copy_mode="reconstructed_wallet_lifecycle",
        selected_wallets=["0xabc"],
        sizing_mode="proportional_to_whale_with_cap",
        copy_ratio=0.001,
        max_position_budget_usd=10,
        min_trade_usd=0,
        entry_delay_seconds=60,
        exit_delay_seconds=60,
        allowed_data_quality=["price_history_proxy"],
    )
    result = run_backtest(cfg, bt_cfg)

    assert result["metrics"]["trade_count"] == 1
    assert result["metrics"]["total_pnl"] == 1.6
    assert result["metrics"]["closed_copied_positions"] == 1
    with session_scope(cfg["app"]["database_url"]) as session:
        assert session.scalar(select(ReconstructedPosition)) is not None
        assert session.scalar(select(LifecycleCopyPosition)) is not None

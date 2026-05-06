from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from pmcopy.db import Base, Market, PriceHistory, ReconstructedPosition, ReconstructedPositionEvent, Trade, json_loads
from pmcopy.features.alpha_decay import alpha_config_from_values, simulate_reconstructed_lifecycles
from pmcopy.features.position_reconstruction import ReconstructionConfig, reconstruct_wallet_positions_in_session


class DummyClob:
    def price_history_url(self, token_id: str, interval: str = "max", fidelity: int | str = 1):
        return f"https://clob.polymarket.com/prices-history?market={token_id}&interval={interval}&fidelity={fidelity}"

    def get_price_history_payload(self, token_id: str, interval: str = "max", fidelity: int | str = 1):
        return {"history": []}

    def get_orderbook(self, token_id: str):
        return None

    def get_midpoint(self, token_id: str):
        return None

    def get_spread(self, token_id: str):
        return None

    def get_last_trade_price(self, token_id: str):
        return None


def base_config() -> dict:
    return {
        "alpha_decay": {
            "max_price_history_distance_seconds": 120,
            "proxy_spread_assumption": 0.01,
            "proxy_slippage_bps": 0,
            "default_position_size_usd": 2,
            "max_spread": 0.03,
            "max_entry_degradation": 0.5,
            "allowed_data_quality_levels": ["price_history_proxy"],
            "sizing_mode": "fixed_usd",
            "copy_ratio": 0.1,
        },
        "fees": {"default_fee_rate": 0.0},
    }


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def add_trade(session: Session, trade_id: str, side: str, timestamp: datetime, *, price: float = 0.5, size: float = 10) -> None:
    session.add(
        Trade(
            id=trade_id,
            wallet_address="0xabc",
            market_id="m1",
            token_id="token",
            side=side,
            price=price,
            size=size,
            timestamp=timestamp,
        )
    )


def add_price(session: Session, price_id: str, timestamp: datetime, price: float) -> None:
    session.add(PriceHistory(id=price_id, token_id="token", timestamp=timestamp, price=price))


def reconstruct(session: Session, **kwargs):
    return reconstruct_wallet_positions_in_session(session, "0xabc", ReconstructionConfig(**kwargs))


def test_reconstruct_simple_buy_then_full_sell() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy", "BUY", now, price=0.4, size=10)
        add_trade(session, "sell", "SELL", now + timedelta(hours=1), price=0.7, size=10)
        session.commit()

        stats = reconstruct(session)
        position = session.scalars(select(ReconstructedPosition)).one()
        events = list(session.scalars(select(ReconstructedPositionEvent).order_by(ReconstructedPositionEvent.timestamp)))

    assert stats["closed_positions"] == 1
    assert position.status == "closed"
    assert position.total_buy_shares == 10
    assert position.total_sell_shares == 10
    assert position.total_buy_usd == 4
    assert position.total_sell_usd == 7
    assert position.realized_pnl == 3
    assert [event.event_type for event in events] == ["open_position", "full_exit"]
    assert events[0].usd_value == 4


def test_reconstruct_partial_then_full_and_multiple_buys() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy-1", "BUY", now, price=0.4, size=10)
        add_trade(session, "buy-2", "BUY", now + timedelta(minutes=10), price=0.5, size=10)
        add_trade(session, "sell-1", "SELL", now + timedelta(hours=1), price=0.6, size=5)
        add_trade(session, "sell-2", "SELL", now + timedelta(hours=2), price=0.7, size=15)
        session.commit()

        stats = reconstruct(session)
        events = list(session.scalars(select(ReconstructedPositionEvent).order_by(ReconstructedPositionEvent.timestamp)))

    assert stats["closed_positions"] == 1
    assert [event.event_type for event in events] == ["open_position", "increase_position", "partial_exit", "full_exit"]
    assert events[2].position_before == 20
    assert events[2].position_after == 15


def test_reconstruct_sell_before_buy_marks_missing_prior_inventory() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "orphan", "SELL", now, price=0.6, size=5)
        add_trade(session, "buy", "BUY", now + timedelta(hours=1), price=0.4, size=10)
        session.commit()

        stats = reconstruct(session)
        statuses = sorted(row.status for row in session.scalars(select(ReconstructedPosition)))

    assert stats["missing_prior_inventory"] == 1
    assert stats["orphan_sell"] == 1
    assert statuses == ["missing_prior_inventory", "open"]


def test_reconstruct_full_close_then_reopen_same_token_creates_new_position() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy-1", "BUY", now, price=0.4, size=10)
        add_trade(session, "sell-1", "SELL", now + timedelta(hours=1), price=0.6, size=10)
        add_trade(session, "buy-2", "BUY", now + timedelta(hours=2), price=0.5, size=4)
        session.commit()

        stats = reconstruct(session)
        positions = list(session.scalars(select(ReconstructedPosition).order_by(ReconstructedPosition.opened_at)))
        events = list(session.scalars(select(ReconstructedPositionEvent).order_by(ReconstructedPositionEvent.timestamp)))

    assert stats["positions"] == 2
    assert [position.status for position in positions] == ["closed", "open"]
    assert [event.event_type for event in events] == ["open_position", "full_exit", "open_position"]


def test_warmup_buy_initializes_inventory_but_is_not_analysis_entry() -> None:
    warmup_buy = datetime(2026, 1, 1, tzinfo=timezone.utc)
    analysis_start = datetime(2026, 1, 10, tzinfo=timezone.utc)
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "warmup-buy", "BUY", warmup_buy, price=0.4, size=10)
        add_trade(session, "analysis-sell", "SELL", analysis_start + timedelta(hours=1), price=0.7, size=10)
        session.commit()

        stats = reconstruct(session, analysis_start=analysis_start, warmup_days=30)
        events = list(session.scalars(select(ReconstructedPositionEvent).order_by(ReconstructedPositionEvent.timestamp)))
        cfg = alpha_config_from_values(base_config(), delays=[60], exit_rule="reconstructed_wallet_lifecycle")
        results = simulate_reconstructed_lifecycles(session, DummyClob(), {}, base_config(), "0xabc", 60, cfg)

    assert stats["closed_positions"] == 1
    assert json_loads(events[0].raw_json, {})["in_analysis_window"] is False
    assert json_loads(events[1].raw_json, {})["in_analysis_window"] is True
    assert results == []


def test_lifecycle_fixed_sizing_entry_exit_delays_and_pnl() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = base_config()
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy", "BUY", now, price=0.4, size=10)
        add_trade(session, "sell", "SELL", now + timedelta(hours=1), price=0.7, size=10)
        add_price(session, "entry", now + timedelta(seconds=60), 0.5)
        add_price(session, "exit", now + timedelta(hours=1, seconds=300), 0.7)
        session.commit()
        reconstruct(session)

        cfg = alpha_config_from_values(
            config,
            delays=[60],
            exit_rule="reconstructed_wallet_lifecycle",
            position_size_usd=2,
            exit_delay_seconds=300,
        )
        result = simulate_reconstructed_lifecycles(session, DummyClob(), {}, config, "0xabc", 60, cfg)[0]

    raw = json_loads(result.raw_json, {})
    assert result.skip_reason is None
    assert result.gross_pnl == 0.8
    assert raw["entry"]["target_time"] == (now + timedelta(seconds=60)).isoformat()
    assert raw["exit"]["exit_time"] == (now + timedelta(hours=1, seconds=300)).isoformat()


def test_lifecycle_proportional_to_whale_sizing() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = base_config()
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy", "BUY", now, price=0.5, size=100)
        add_trade(session, "sell", "SELL", now + timedelta(hours=1), price=0.7, size=100)
        add_price(session, "entry", now + timedelta(seconds=60), 0.5)
        add_price(session, "exit", now + timedelta(hours=1, seconds=60), 0.7)
        session.commit()
        reconstruct(session)

        cfg = alpha_config_from_values(
            config,
            delays=[60],
            exit_rule="reconstructed_wallet_lifecycle",
            sizing_mode="proportional_to_whale",
            copy_ratio=0.1,
        )
        result = simulate_reconstructed_lifecycles(session, DummyClob(), {}, config, "0xabc", 60, cfg)[0]

    raw = json_loads(result.raw_json, {})
    assert result.skip_reason is None
    assert raw["entries"][0]["copied_usd"] == 5
    assert result.gross_pnl == 2


def test_lifecycle_missing_exit_price_skip() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = base_config()
    with make_session() as session:
        session.add(Market(market_id="m1", category="sports"))
        add_trade(session, "buy", "BUY", now, price=0.5, size=10)
        add_trade(session, "sell", "SELL", now + timedelta(hours=1), price=0.7, size=10)
        add_price(session, "entry", now + timedelta(seconds=60), 0.5)
        session.commit()
        reconstruct(session)

        cfg = alpha_config_from_values(config, delays=[60], exit_rule="reconstructed_wallet_lifecycle")
        result = simulate_reconstructed_lifecycles(session, DummyClob(), {}, config, "0xabc", 60, cfg)[0]

    assert result.skip_reason in {"exit_price_missing", "exit_price_too_far"}

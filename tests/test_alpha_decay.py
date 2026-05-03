from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pmcopy.db import Base, Market, OrderbookSnapshot, PriceHistory, Trade, json_dumps, json_loads
from pmcopy.features.alpha_decay import (
    alpha_config_from_values,
    entry_degradation,
    nearest_price_history,
    normalize_price_history_payload,
    normalize_timestamp,
    simulate_trade_delay,
)


class DummyClob:
    def __init__(self, payload=None) -> None:
        self.payload = payload if payload is not None else {"history": []}

    def price_history_url(self, token_id: str, interval: str = "max", fidelity: int | str = 1):
        return f"https://clob.polymarket.com/prices-history?market={token_id}&interval={interval}&fidelity={fidelity}"

    def get_price_history_payload(self, token_id: str, interval: str = "max", fidelity: int | str = 1):
        return self.payload

    def get_orderbook(self, token_id: str):
        return None

    def get_price_history(self, token_id: str, interval: str = "max", fidelity: int | str = 1):
        return []

    def get_midpoint(self, token_id: str):
        return None

    def get_spread(self, token_id: str):
        return None

    def get_last_trade_price(self, token_id: str):
        return None


def test_entry_degradation_buy_and_sell() -> None:
    assert entry_degradation("BUY", 0.4, 0.45) == 0.04999999999999999
    assert entry_degradation("SELL", 0.4, 0.35) == 0.050000000000000044


def test_alpha_decay_price_history_proxy_calculation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    trade_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    copy_time = trade_time + timedelta(seconds=60)
    exit_time = copy_time + timedelta(hours=24)
    config = {
        "alpha_decay": {
            "max_price_history_distance_seconds": 3600,
            "proxy_spread_assumption": 0.01,
            "proxy_slippage_bps": 0,
            "default_position_size_usd": 2,
            "max_spread": 0.03,
            "max_entry_degradation": 0.2,
            "allowed_data_quality_levels": ["price_history_proxy"],
            "default_exit_rule": "fixed_24h",
        },
        "fees": {"default_fee_rate": 0.04, "fee_rates_by_category": {"sports": 0.03}},
    }
    with Session(engine) as session:
        session.add(Market(market_id="m1", category="sports"))
        trade = Trade(
            id="t1",
            wallet_address="0xabc",
            market_id="m1",
            token_id="token",
            side="BUY",
            price=0.4,
            size=10,
            timestamp=trade_time,
        )
        session.add(trade)
        session.add(PriceHistory(id="p1", token_id="token", timestamp=copy_time, price=0.5))
        session.add(PriceHistory(id="p2", token_id="token", timestamp=exit_time, price=0.7))
        session.commit()

        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="fixed_24h", position_size_usd=2)
        result = simulate_trade_delay(session, DummyClob(), {}, config, trade, 60, alpha_cfg)

    assert result.data_quality == "price_history_proxy"
    assert result.skip_reason is None
    assert result.simulated_entry_price == 0.5
    assert result.eventual_exit_price == 0.7
    assert result.gross_pnl == 0.8
    assert result.net_pnl is not None and result.net_pnl < result.gross_pnl


def test_alpha_decay_spread_filter_skips_wide_orderbook() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    trade_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    copy_time = trade_time + timedelta(seconds=60)
    config = {
        "alpha_decay": {
            "exact_orderbook_max_age_seconds": 120,
            "max_price_history_distance_seconds": 3600,
            "default_position_size_usd": 2,
            "max_spread": 0.03,
            "max_entry_degradation": 0.2,
            "allowed_data_quality_levels": ["exact_orderbook"],
            "default_exit_rule": "fixed_24h",
        },
        "fees": {"default_fee_rate": 0.04},
    }
    with Session(engine) as session:
        session.add(Market(market_id="m1", category="sports"))
        trade = Trade(
            id="t1",
            wallet_address="0xabc",
            market_id="m1",
            token_id="token",
            side="BUY",
            price=0.4,
            size=10,
            timestamp=trade_time,
        )
        session.add(trade)
        session.add(
            OrderbookSnapshot(
                id="o1",
                token_id="token",
                timestamp=copy_time,
                best_bid=0.4,
                best_ask=0.5,
                spread=0.1,
                midpoint=0.45,
                bids_json=json_dumps([{"price": "0.4", "size": "100"}]),
                asks_json=json_dumps([{"price": "0.5", "size": "100"}]),
            )
        )
        session.commit()
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="fixed_24h", position_size_usd=2, historical_mode="full")
        result = simulate_trade_delay(session, DummyClob(), {}, config, trade, 60, alpha_cfg)

    assert result.data_quality == "exact_orderbook"
    assert result.skip_reason is not None
    assert "spread" in result.skip_reason


def test_price_history_parser_handles_common_shapes() -> None:
    expected = datetime(2026, 3, 29, 17, 20, 44, tzinfo=timezone.utc)
    points = normalize_price_history_payload({"history": [{"t": 1774804844, "p": 0.7835}]})
    assert points[0].timestamp == expected
    assert points[0].price == 0.7835

    points = normalize_price_history_payload({"prices": [{"timestamp": "2026-01-01T00:00:00Z", "price": "0.42"}]})
    assert points[0].timestamp == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert points[0].price == 0.42

    points = normalize_price_history_payload([["1774804844000", "0.5"]])
    assert points[0].timestamp == expected
    assert points[0].price == 0.5


def test_timestamp_normalization_handles_seconds_millis_iso_and_naive() -> None:
    expected = datetime(2026, 3, 29, 17, 20, 44, tzinfo=timezone.utc)
    assert normalize_timestamp(1774804844) == expected
    assert normalize_timestamp(1774804844000) == expected
    assert normalize_timestamp("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert normalize_timestamp(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_nearest_price_history_enforces_max_distance() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = {"alpha_decay": {"max_price_history_distance_seconds": 30}}
    target = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    payload = {"history": [{"t": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), "p": 0.5}]}
    with Session(engine) as session:
        lookup = nearest_price_history(session, DummyClob(payload), {}, config, "token", target, context="copy")

    assert lookup.price is None
    assert lookup.reason == "copy_time_after_history"
    assert lookup.distance_seconds == 120


def test_fixed_24h_future_exit_skip_reason() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    trade_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    config = {
        "alpha_decay": {
            "max_price_history_distance_seconds": 86400,
            "proxy_spread_assumption": 0.01,
            "proxy_slippage_bps": 0,
            "default_position_size_usd": 2,
            "max_spread": 0.03,
            "max_entry_degradation": 0.2,
        },
        "fees": {"default_fee_rate": 0.04},
    }
    payload = {
        "history": [
            {"t": int((trade_time + timedelta(seconds=60)).timestamp()), "p": 0.5},
            {"t": int(datetime.now(timezone.utc).timestamp()), "p": 0.55},
        ]
    }
    with Session(engine) as session:
        session.add(Market(market_id="m1", category="sports"))
        trade = Trade(id="t1", wallet_address="0xabc", market_id="m1", token_id="token", side="BUY", price=0.4, size=10, timestamp=trade_time)
        session.add(trade)
        session.commit()
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="fixed_24h", position_size_usd=2)
        result = simulate_trade_delay(session, DummyClob(payload), {}, config, trade, 60, alpha_cfg)

    assert result.skip_reason == "exit_time_in_future"
    assert result.data_quality == "insufficient_data"


def test_latest_available_exit_rule_uses_later_history() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    trade_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    copy_time = trade_time + timedelta(seconds=60)
    config = {
        "alpha_decay": {
            "max_price_history_distance_seconds": 3600,
            "proxy_spread_assumption": 0.01,
            "proxy_slippage_bps": 0,
            "default_position_size_usd": 2,
            "max_spread": 0.03,
            "max_entry_degradation": 0.2,
        },
        "fees": {"default_fee_rate": 0.04},
    }
    payload = {
        "history": [
            {"t": int(copy_time.timestamp()), "p": 0.5},
            {"t": int((copy_time + timedelta(hours=2)).timestamp()), "p": 0.8},
        ]
    }
    with Session(engine) as session:
        session.add(Market(market_id="m1", category="sports"))
        trade = Trade(id="t1", wallet_address="0xabc", market_id="m1", token_id="token", side="BUY", price=0.4, size=10, timestamp=trade_time)
        session.add(trade)
        session.commit()
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="latest_available", position_size_usd=2)
        result = simulate_trade_delay(session, DummyClob(payload), {}, config, trade, 60, alpha_cfg)

    assert result.skip_reason is None
    assert result.data_quality == "price_history_proxy"
    assert result.eventual_exit_price == 0.8


def test_detailed_preflight_skip_reason_is_stored() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = {"alpha_decay": {}, "fees": {"default_fee_rate": 0.04}}
    with Session(engine) as session:
        trade = Trade(
            id="t1",
            wallet_address="0xabc",
            market_id="m1",
            token_id=None,
            side="BUY",
            price=0.4,
            size=10,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        session.add(trade)
        session.commit()
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="latest_available", position_size_usd=2)
        result = simulate_trade_delay(session, DummyClob(), {}, config, trade, 60, alpha_cfg)

    assert result.skip_reason == "missing_token_id"
    assert result.data_quality == "insufficient_data"


def follow_exit_config() -> dict:
    return {
        "alpha_decay": {
            "max_price_history_distance_seconds": 3600,
            "proxy_spread_assumption": 0.01,
            "proxy_slippage_bps": 0,
            "default_position_size_usd": 2,
            "max_spread": 0.03,
            "max_entry_degradation": 0.2,
            "min_exit_fraction": 0.5,
            "allow_partial_exits": True,
        },
        "fees": {"default_fee_rate": 0.04},
    }


def add_follow_exit_entry(session: Session, *, sell_sizes: list[float], exit_delay_seconds: int = 60) -> Trade:
    trade_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    session.add(Market(market_id="m1", category="sports"))
    entry = Trade(
        id="entry",
        wallet_address="0xabc",
        market_id="m1",
        token_id="token",
        side="BUY",
        price=0.5,
        size=10,
        timestamp=trade_time,
    )
    session.add(entry)
    session.add(PriceHistory(id="p-entry", token_id="token", timestamp=trade_time + timedelta(seconds=60), price=0.5))
    for index, sell_size in enumerate(sell_sizes, start=1):
        sell_time = trade_time + timedelta(hours=index)
        session.add(
            Trade(
                id=f"sell-{index}",
                wallet_address="0xabc",
                market_id="m1",
                token_id="token",
                side="SELL",
                price=0.7,
                size=sell_size,
                timestamp=sell_time,
            )
        )
        session.add(
            PriceHistory(
                id=f"p-exit-{index}",
                token_id="token",
                timestamp=sell_time + timedelta(seconds=exit_delay_seconds),
                price=0.7 + index / 100,
            )
        )
    session.commit()
    return entry


def test_follow_wallet_exit_full_sell() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    with Session(engine) as session:
        entry = add_follow_exit_entry(session, sell_sizes=[10])
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2, exit_delay_seconds=60)
        result = simulate_trade_delay(session, DummyClob(), {}, config, entry, 60, alpha_cfg)

    raw = json_loads(result.raw_json, {})
    assert result.skip_reason is None
    assert result.eventual_exit_price == 0.71
    assert raw["exit"]["exit_kind"] == "full"
    assert raw["exit"]["exit_delay_seconds"] == 60


def test_follow_wallet_exit_partial_sell() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    with Session(engine) as session:
        entry = add_follow_exit_entry(session, sell_sizes=[5])
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2, exit_delay_seconds=60, min_exit_fraction=0.5)
        result = simulate_trade_delay(session, DummyClob(), {}, config, entry, 60, alpha_cfg)

    raw = json_loads(result.raw_json, {})
    assert result.skip_reason is None
    assert raw["exit"]["exit_kind"] == "partial"
    assert raw["exit"]["exit_fraction"] == 0.5
    assert result.gross_pnl is not None and result.gross_pnl > 0


def test_follow_wallet_exit_multiple_partial_sells_weighted_exit() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    with Session(engine) as session:
        entry = add_follow_exit_entry(session, sell_sizes=[3, 7])
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2, exit_delay_seconds=60)
        result = simulate_trade_delay(session, DummyClob(), {}, config, entry, 60, alpha_cfg)

    raw = json_loads(result.raw_json, {})
    assert result.skip_reason is None
    assert raw["exit"]["exit_kind"] == "full"
    assert len(raw["exit"]["segments"]) == 2
    assert result.eventual_exit_price == 0.717


def test_follow_wallet_exit_no_later_sell() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    with Session(engine) as session:
        entry = add_follow_exit_entry(session, sell_sizes=[])
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2)
        result = simulate_trade_delay(session, DummyClob(), {}, config, entry, 60, alpha_cfg)

    assert result.skip_reason == "no_wallet_exit_found"


def test_follow_wallet_exit_price_missing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    config["alpha_decay"]["max_price_history_distance_seconds"] = 30
    with Session(engine) as session:
        entry = add_follow_exit_entry(session, sell_sizes=[10])
        session.query(PriceHistory).filter(PriceHistory.id.like("p-exit-%")).delete()
        session.commit()
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2)
        result = simulate_trade_delay(session, DummyClob(), {}, config, entry, 60, alpha_cfg)

    assert result.skip_reason in {"exit_price_missing", "exit_price_too_far"}


def test_follow_wallet_exit_delay_differs_from_entry_delay() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    with Session(engine) as session:
        entry = add_follow_exit_entry(session, sell_sizes=[10], exit_delay_seconds=300)
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2, exit_delay_seconds=300)
        result = simulate_trade_delay(session, DummyClob(), {}, config, entry, 60, alpha_cfg)

    raw = json_loads(result.raw_json, {})
    assert result.skip_reason is None
    assert raw["exit"]["exit_delay_seconds"] == 300


def test_follow_wallet_exit_partial_below_threshold() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    with Session(engine) as session:
        entry = add_follow_exit_entry(session, sell_sizes=[2])
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2, min_exit_fraction=0.5)
        result = simulate_trade_delay(session, DummyClob(), {}, config, entry, 60, alpha_cfg)

    assert result.skip_reason == "partial_exit_below_threshold"


def test_follow_wallet_exit_sell_source_is_inconsistent_side() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    config = follow_exit_config()
    trade_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(Market(market_id="m1", category="sports"))
        trade = Trade(id="short", wallet_address="0xabc", market_id="m1", token_id="token", side="SELL", price=0.5, size=10, timestamp=trade_time)
        session.add(trade)
        session.add(PriceHistory(id="p-entry", token_id="token", timestamp=trade_time + timedelta(seconds=60), price=0.5))
        session.commit()
        alpha_cfg = alpha_config_from_values(config, delays=[60], exit_rule="follow_wallet_exit", position_size_usd=2)
        result = simulate_trade_delay(session, DummyClob(), {}, config, trade, 60, alpha_cfg)

    assert result.skip_reason == "inconsistent_trade_side"

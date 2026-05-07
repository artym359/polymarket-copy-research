from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pmcopy.db import Base, Market, ReconstructedPositionEvent, Trade, WalletSnapshot
from pmcopy.features.wallet_metrics import compute_wallet_metrics_values, concentration_shares


def test_wallet_metrics_from_trades_and_snapshots() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Market(market_id="m1", category="sports"))
        session.add(Market(market_id="m2", category="politics"))
        session.add(
            Trade(
                id="t1",
                wallet_address="0xabc",
                market_id="m1",
                side="BUY",
                price=0.5,
                size=10,
                usd_value=5,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.add(
            Trade(
                id="t2",
                wallet_address="0xabc",
                market_id="m2",
                side="SELL",
                price=0.25,
                size=20,
                usd_value=5,
                timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )
        )
        session.add(
            WalletSnapshot(
                id="s1",
                wallet_address="0xabc",
                source_endpoint="data_api:/closed-positions",
                market_id="m1",
                token_id="a1",
                realized_pnl=3,
            )
        )
        session.add(
            WalletSnapshot(
                id="s2",
                wallet_address="0xabc",
                source_endpoint="data_api:/positions",
                market_id="m2",
                token_id="a2",
                initial_value=5,
                current_value=4,
                cash_pnl=-1,
            )
        )
        session.commit()

        values = compute_wallet_metrics_values(session, "0xabc")

    assert values.trade_count == 2
    assert values.market_count == 2
    assert values.active_days == 2
    assert values.total_volume == 10
    assert values.realized_pnl == 3
    assert values.unrealized_pnl == -1
    assert values.total_pnl == 2
    assert values.edge_on_volume == 0.2
    assert values.roi_on_volume == 0.2
    assert values.pnl_per_traded_dollar == 0.2
    assert values.max_capital_at_risk == 5
    assert values.return_on_max_capital_at_risk == 0.4
    assert values.max_exposure_confidence == "data_api_proxy"
    assert values.average_capital_at_risk is None
    assert values.return_on_average_capital_at_risk is None
    assert values.average_exposure_confidence == "unavailable"
    assert values.top_1_market_pnl_share == 0.75
    assert values.top_5_market_pnl_share == 1.0
    assert values.main_category == "sports"
    assert values.max_drawdown_estimate is None


def test_concentration_shares_empty_and_nonzero() -> None:
    assert concentration_shares({}) == (None, None)
    assert concentration_shares({"a": 10, "b": -5, "c": 5}) == (0.5, 1.0)


def test_edge_on_volume_null_when_volume_zero_or_missing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Trade(
                id="zero",
                wallet_address="0xabc",
                market_id="m1",
                side="BUY",
                price=0,
                size=10,
                usd_value=0,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.add(
            WalletSnapshot(
                id="pnl",
                wallet_address="0xabc",
                source_endpoint="data_api:/closed-positions",
                market_id="m1",
                realized_pnl=5,
            )
        )
        session.commit()

        zero_values = compute_wallet_metrics_values(session, "0xabc")
        missing_values = compute_wallet_metrics_values(session, "0xmissing")

    assert zero_values.edge_on_volume is None
    assert zero_values.roi_on_volume is None
    assert missing_values.edge_on_volume is None


def test_reconstructed_exposure_returns_and_confidence_labels() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(
            WalletSnapshot(
                id="pnl",
                wallet_address="0xabc",
                source_endpoint="data_api:/closed-positions",
                market_id="m1",
                realized_pnl=4,
            )
        )
        session.add_all(
            [
                ReconstructedPositionEvent(
                    id="e1",
                    wallet_address="0xabc",
                    token_id="token",
                    market_id="m1",
                    position_id="p1",
                    trade_id="t1",
                    timestamp=start,
                    side="BUY",
                    price=0.5,
                    shares=10,
                    usd_value=5,
                    position_before=0,
                    position_after=10,
                    event_type="open_position",
                ),
                ReconstructedPositionEvent(
                    id="e2",
                    wallet_address="0xabc",
                    token_id="token",
                    market_id="m1",
                    position_id="p1",
                    trade_id="t2",
                    timestamp=start.replace(minute=10),
                    side="BUY",
                    price=1,
                    shares=0,
                    usd_value=0,
                    position_before=10,
                    position_after=10,
                    event_type="increase_position",
                ),
                ReconstructedPositionEvent(
                    id="e3",
                    wallet_address="0xabc",
                    token_id="token",
                    market_id="m1",
                    position_id="p1",
                    trade_id="t3",
                    timestamp=start.replace(minute=20),
                    side="SELL",
                    price=1,
                    shares=10,
                    usd_value=10,
                    position_before=10,
                    position_after=0,
                    event_type="full_exit",
                ),
            ]
        )
        session.commit()

        values = compute_wallet_metrics_values(session, "0xabc")

    assert values.max_capital_at_risk == 10
    assert values.return_on_max_capital_at_risk == 0.4
    assert values.max_exposure_method == "reconstructed_position_events"
    assert values.max_exposure_confidence == "reconstructed_positions"
    assert values.average_capital_at_risk == 7.5
    assert values.return_on_average_capital_at_risk == 0.53333333
    assert values.average_exposure_method == "reconstructed_position_events"
    assert values.average_exposure_confidence == "reconstructed_positions_time_weighted"


def test_exposure_returns_null_when_denominator_zero_or_missing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            WalletSnapshot(
                id="pnl",
                wallet_address="0xabc",
                source_endpoint="data_api:/closed-positions",
                market_id="m1",
                realized_pnl=4,
                initial_value=0,
                current_value=0,
            )
        )
        session.commit()

        values = compute_wallet_metrics_values(session, "0xabc")

    assert values.max_capital_at_risk is None
    assert values.return_on_max_capital_at_risk is None
    assert values.average_capital_at_risk is None
    assert values.return_on_average_capital_at_risk is None
    assert values.max_exposure_confidence == "unavailable"
    assert values.average_exposure_confidence == "unavailable"


def test_snapshot_average_exposure_proxy() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(
            WalletSnapshot(
                id="pnl",
                wallet_address="0xabc",
                source_endpoint="data_api:/closed-positions",
                market_id="m1",
                token_id="closed",
                realized_pnl=2,
            )
        )
        session.add_all(
            [
                WalletSnapshot(
                    id="s1",
                    wallet_address="0xabc",
                    fetched_at=first,
                    source_endpoint="data_api:/positions",
                    market_id="m1",
                    token_id="open",
                    initial_value=8,
                    current_value=4,
                ),
                WalletSnapshot(
                    id="s2",
                    wallet_address="0xabc",
                    fetched_at=second,
                    source_endpoint="data_api:/positions",
                    market_id="m1",
                    token_id="open-later",
                    initial_value=10,
                    current_value=6,
                ),
            ]
        )
        session.commit()

        values = compute_wallet_metrics_values(session, "0xabc")

    assert values.max_capital_at_risk == 18
    assert values.return_on_max_capital_at_risk == 0.11111111
    assert values.max_exposure_confidence == "data_api_proxy"
    assert values.average_capital_at_risk == 4
    assert values.return_on_average_capital_at_risk == 0.5
    assert values.average_exposure_confidence == "snapshots_proxy"

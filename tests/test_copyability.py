from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pmcopy.db import AlphaDecayResult, Base
from pmcopy.features.copyability import compute_wallet_copyability, copyability_trend


def alpha_row(wallet: str, delay: int, pnl: float, trade_time: datetime, quality: str = "price_history_proxy") -> AlphaDecayResult:
    return AlphaDecayResult(
        id=f"{wallet}-{delay}-{trade_time.timestamp()}",
        wallet_address=wallet,
        trade_id=f"trade-{delay}-{trade_time.timestamp()}",
        delay_seconds=delay,
        trade_time=trade_time,
        copy_time=trade_time + timedelta(seconds=delay),
        exit_rule="fixed_24h",
        net_pnl=pnl,
        copy_spread=0.01,
        entry_degradation=0.01,
        data_quality=quality,
        data_quality_rank=3,
    )


def test_copyability_score_and_recent_trend() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    config = {
        "alpha_decay": {
            "allowed_data_quality_levels": ["price_history_proxy"],
            "min_observations_for_copyability": 4,
            "scoring_delay_short_seconds": 60,
            "scoring_delay_medium_seconds": 300,
            "max_spread": 0.03,
            "max_entry_degradation": 0.03,
        }
    }
    with Session(engine) as session:
        for idx in range(4):
            session.add(alpha_row("0xabc", 60, 0.5, now - timedelta(days=idx)))
            session.add(alpha_row("0xabc", 300, 0.4, now - timedelta(days=idx)))
        session.commit()
        values = compute_wallet_copyability(session, config, "0xabc", ["price_history_proxy"])

    assert values.historical_copy_pnl == 3.6
    assert values.copyability_score is not None and values.copyability_score >= 75
    assert values.copyability_trend in {"stable", "insufficient_recent_data"}


def test_copyability_trend_decaying() -> None:
    now = datetime.now(timezone.utc)
    old = [alpha_row("0xabc", 60, 1.0, now - timedelta(days=60 + i)) for i in range(10)]
    recent = [alpha_row("0xabc", 60, -0.5, now - timedelta(days=i)) for i in range(10)]
    assert copyability_trend(old + recent, now, 5.0, -5.0) == "decaying"

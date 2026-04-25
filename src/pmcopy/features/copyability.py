from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmcopy.config import database_url
from pmcopy.db import AlphaDecayResult, WalletClassification, WalletCopyability, init_db, json_dumps, session_scope, utc_now
from pmcopy.features.data_quality import quality_breakdown, quality_rank


@dataclass
class CopyabilityValues:
    wallet_address: str
    historical_copy_pnl: float | None
    recent_7d_copy_pnl: float | None
    recent_30d_copy_pnl: float | None
    recent_90d_copy_pnl: float | None
    copyability_trend: str
    copyability_score: float | None
    reasons: list[str]


def compute_all_copyability(config: dict[str, Any], allowed_data_quality: list[str] | None = None) -> int:
    init_db(config)
    with session_scope(database_url(config)) as session:
        wallets = list(session.scalars(select(AlphaDecayResult.wallet_address).distinct()))
    computed = 0
    for wallet in wallets:
        with session_scope(database_url(config)) as session:
            values = compute_wallet_copyability(session, config, wallet, allowed_data_quality)
            upsert_wallet_copyability(session, values)
            computed += 1
    return computed


def compute_copyability(
    config: dict[str, Any],
    wallet_address: str | None = None,
    allowed_data_quality: list[str] | None = None,
) -> int:
    init_db(config)
    if wallet_address:
        wallets = [wallet_address.lower()]
    else:
        with session_scope(database_url(config)) as session:
            wallets = list(session.scalars(select(AlphaDecayResult.wallet_address).distinct()))
    computed = 0
    for wallet in wallets:
        with session_scope(database_url(config)) as session:
            values = compute_wallet_copyability(session, config, wallet, allowed_data_quality)
            upsert_wallet_copyability(session, values)
            computed += 1
    return computed


def compute_wallet_copyability(
    session: Session,
    config: dict[str, Any],
    wallet_address: str,
    allowed_data_quality: list[str] | None = None,
) -> CopyabilityValues:
    alpha_cfg = config.get("alpha_decay", {})
    allowed = set(allowed_data_quality or alpha_cfg.get("allowed_data_quality_levels", ["exact_orderbook", "price_history_proxy"]))
    rows_all = list(session.scalars(select(AlphaDecayResult).where(AlphaDecayResult.wallet_address == wallet_address)))
    usable = [row for row in rows_all if row.skip_reason is None and row.net_pnl is not None and row.data_quality in allowed]
    now = datetime.now(timezone.utc)
    reasons: list[str] = []

    historical = sum_net(usable)
    recent_7 = sum_net(recent_rows(usable, now, 7))
    recent_30 = sum_net(recent_rows(usable, now, 30))
    recent_90 = sum_net(recent_rows(usable, now, 90))
    trend = copyability_trend(usable, now, historical, recent_30)
    score = copyability_score(session, config, wallet_address, rows_all, usable, trend, reasons)

    if not usable:
        reasons.append("no usable alpha-decay rows after allowed data-quality gate")
    breakdown = quality_breakdown(row.data_quality for row in rows_all)
    reasons.append(
        "data quality mix: "
        + ", ".join(f"{level}={share:.1%}" for level, share in breakdown.items())
    )
    return CopyabilityValues(
        wallet_address=wallet_address,
        historical_copy_pnl=round_float(historical),
        recent_7d_copy_pnl=round_float(recent_7),
        recent_30d_copy_pnl=round_float(recent_30),
        recent_90d_copy_pnl=round_float(recent_90),
        copyability_trend=trend,
        copyability_score=round_float(score),
        reasons=reasons,
    )


def upsert_wallet_copyability(session: Session, values: CopyabilityValues) -> None:
    row = session.get(WalletCopyability, values.wallet_address)
    if row is None:
        row = WalletCopyability(wallet_address=values.wallet_address)
        session.add(row)
    row.computed_at = utc_now()
    row.historical_copy_pnl = values.historical_copy_pnl
    row.recent_7d_copy_pnl = values.recent_7d_copy_pnl
    row.recent_30d_copy_pnl = values.recent_30d_copy_pnl
    row.recent_90d_copy_pnl = values.recent_90d_copy_pnl
    row.copyability_trend = values.copyability_trend
    row.copyability_score = values.copyability_score
    row.copyability_reasons_json = json_dumps(values.reasons)


def copyability_score(
    session: Session,
    config: dict[str, Any],
    wallet_address: str,
    rows_all: list[AlphaDecayResult],
    usable: list[AlphaDecayResult],
    trend: str,
    reasons: list[str],
) -> float | None:
    if not rows_all:
        reasons.append("no alpha-decay observations")
        return None

    alpha_cfg = config.get("alpha_decay", {})
    min_obs = int(alpha_cfg.get("min_observations_for_copyability", 10))
    delay_short = int(alpha_cfg.get("scoring_delay_short_seconds", 60))
    delay_medium = int(alpha_cfg.get("scoring_delay_medium_seconds", 300))
    max_spread = float(alpha_cfg.get("max_spread", 0.03))
    max_degradation = float(alpha_cfg.get("max_entry_degradation", 0.03))
    score = 0.0

    one_min = [row for row in usable if row.delay_seconds == delay_short]
    five_min = [row for row in usable if row.delay_seconds == delay_medium]
    one_min_pnl = sum_net(one_min)
    five_min_pnl = sum_net(five_min)
    if one_min_pnl is not None and one_min_pnl > 0:
        score += 20
        reasons.append(f"positive net copy PnL at {delay_short}s")
    if five_min_pnl is not None and five_min_pnl > 0:
        score += 20
        reasons.append(f"positive net copy PnL at {delay_medium}s")
    if len(usable) >= min_obs:
        score += 15
        reasons.append(f"enough usable alpha observations: {len(usable)} >= {min_obs}")
    else:
        reasons.append(f"not enough usable alpha observations: {len(usable)} < {min_obs}")

    spreads = [row.copy_spread for row in usable if row.copy_spread is not None]
    if spreads and sum(spread <= max_spread for spread in spreads) / len(spreads) >= 0.8:
        score += 10
        reasons.append("acceptable spread on most usable rows")
    degradations = [row.entry_degradation for row in usable if row.entry_degradation is not None]
    if degradations and sum(value <= max_degradation for value in degradations) / len(degradations) >= 0.8:
        score += 10
        reasons.append("acceptable entry degradation on most usable rows")

    high_quality_share = quality_share(usable, min_rank=3)
    if high_quality_share >= 0.75:
        score += 10
        reasons.append(f"acceptable data-quality mix: {high_quality_share:.1%} rank>=3")
    else:
        reasons.append(f"weak data-quality mix: {high_quality_share:.1%} rank>=3")

    classification = session.get(WalletClassification, wallet_address)
    if classification and classification.class_label in {"likely_market_maker", "lucky_wallet"}:
        reasons.append(f"classification penalty: {classification.class_label}")
    else:
        score += 10
        reasons.append("not classified as market maker or lucky wallet")

    if trend in {"improving", "stable"}:
        score += 5
        reasons.append(f"recent copyability trend is {trend}")
    elif trend == "decaying":
        score -= 10
        reasons.append("recent copyability trend is decaying")
    elif trend == "inactive":
        score -= 5
        reasons.append("wallet inactive in recent copyability window")

    return max(0.0, min(100.0, score))


def copyability_trend(
    usable: list[AlphaDecayResult],
    now: datetime,
    historical_copy_pnl: float | None,
    recent_30d_copy_pnl: float | None,
) -> str:
    recent_30 = recent_rows(usable, now, 30)
    if not recent_30:
        recent_90 = recent_rows(usable, now, 90)
        return "inactive" if not recent_90 else "insufficient_recent_data"
    if len(recent_30) < 5:
        return "insufficient_recent_data"
    historical_avg = avg_net(usable)
    recent_avg = avg_net(recent_30)
    if historical_avg is None or recent_avg is None:
        return "insufficient_recent_data"
    margin = max(abs(historical_avg) * 0.25, 0.01)
    if recent_avg > historical_avg + margin:
        return "improving"
    if historical_avg > 0 and recent_avg < 0:
        return "decaying"
    if recent_avg < historical_avg - margin:
        return "decaying"
    return "stable"


def recent_rows(rows: list[AlphaDecayResult], now: datetime, days: int) -> list[AlphaDecayResult]:
    cutoff = now - timedelta(days=days)
    return [row for row in rows if row.trade_time and ensure_aware(row.trade_time) >= cutoff]


def sum_net(rows: list[AlphaDecayResult]) -> float | None:
    values = [row.net_pnl for row in rows if row.net_pnl is not None]
    return sum(values) if values else None


def avg_net(rows: list[AlphaDecayResult]) -> float | None:
    values = [row.net_pnl for row in rows if row.net_pnl is not None]
    return sum(values) / len(values) if values else None


def quality_share(rows: list[AlphaDecayResult], min_rank: int) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if quality_rank(row.data_quality) >= min_rank) / len(rows)


def delay_pnl(rows: list[AlphaDecayResult]) -> dict[int, float | None]:
    delays = sorted({row.delay_seconds for row in rows})
    return {delay: sum_net([row for row in rows if row.delay_seconds == delay]) for delay in delays}


def latency_bot_score_from_alpha(rows: list[AlphaDecayResult]) -> tuple[float | None, list[str]]:
    usable = [row for row in rows if row.skip_reason is None and row.net_pnl is not None]
    if len(usable) < 5:
        return None, ["not enough alpha-decay rows for latency classification"]
    pnl_by_delay = delay_pnl(usable)
    pnl_10 = pnl_by_delay.get(10)
    pnl_60 = pnl_by_delay.get(60)
    pnl_300 = pnl_by_delay.get(300)
    if pnl_10 is None or (pnl_60 is None and pnl_300 is None):
        return None, ["missing 10s and 1m/5m alpha-decay delays for latency classification"]
    score = 0.0
    reasons: list[str] = []
    if pnl_10 > 0:
        score += 0.45
        reasons.append("positive copy PnL at 10s")
    if pnl_60 is not None and pnl_60 <= 0:
        score += 0.30
        reasons.append("copy PnL non-positive by 1m")
    if pnl_300 is not None and pnl_300 <= 0:
        score += 0.25
        reasons.append("copy PnL non-positive by 5m")
    return min(score, 1.0), reasons


def copyability_ranking_dataframe(session: Session):
    import pandas as pd

    rows = []
    for row in session.scalars(select(WalletCopyability).order_by(WalletCopyability.copyability_score.desc().nullslast())):
        rows.append(
            {
                "wallet_address": row.wallet_address,
                "historical_copy_pnl": row.historical_copy_pnl,
                "recent_7d_copy_pnl": row.recent_7d_copy_pnl,
                "recent_30d_copy_pnl": row.recent_30d_copy_pnl,
                "recent_90d_copy_pnl": row.recent_90d_copy_pnl,
                "copyability_trend": row.copyability_trend,
                "copyability_score": row.copyability_score,
                "reasons": "; ".join(__import__("json").loads(row.copyability_reasons_json or "[]")[:8]),
            }
        )
    return pd.DataFrame(rows)


def ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def round_float(value: Any, digits: int = 8) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)

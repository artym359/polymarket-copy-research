from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmcopy.config import database_url
from pmcopy.db import AlphaDecayResult, WalletClassification, WalletMetrics, init_db, json_dumps, json_loads, session_scope, utc_now
from pmcopy.features.copyability import latency_bot_score_from_alpha


@dataclass
class ClassificationValues:
    wallet_address: str
    class_label: str
    market_maker_score: float
    latency_bot_score: float | None
    lucky_wallet_score: float
    directional_score: float
    insufficient_sample_flag: bool
    reasons: list[str]


def classify_all_wallets(
    config: dict[str, Any],
    limit: int | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    init_db(config)
    with session_scope(database_url(config)) as session:
        stmt = select(WalletMetrics.wallet_address).order_by(WalletMetrics.wallet_address)
        if limit:
            stmt = stmt.limit(limit)
        wallets = list(session.scalars(stmt))

    classified = 0
    total = len(wallets)
    for index, wallet_address in enumerate(wallets, start=1):
        if progress_callback:
            progress_callback(index, total, wallet_address)
        with session_scope(database_url(config)) as session:
            metrics = session.get(WalletMetrics, wallet_address)
            if metrics is None:
                continue
            alpha_rows = list(session.scalars(select(AlphaDecayResult).where(AlphaDecayResult.wallet_address == wallet_address)))
            values = classify_wallet_metrics(metrics, config, alpha_rows=alpha_rows)
            upsert_wallet_classification(session, values)
            classified += 1
    return classified


def classify_wallet_metrics(
    metrics: WalletMetrics,
    config: dict[str, Any],
    alpha_rows: list[AlphaDecayResult] | None = None,
) -> ClassificationValues:
    reasons: list[str] = []
    filters = config.get("wallet_filters", {})
    mm_cfg = config.get("classification", {}).get("likely_market_maker", {})
    lucky_cfg = config.get("classification", {}).get("lucky_wallet", {})
    metrics_extra = json_loads(metrics.metrics_json, {}) or {}

    insufficient_reasons = []
    if metrics.trade_count < int(filters.get("min_trades", 100) or 0):
        insufficient_reasons.append(f"trade_count {metrics.trade_count} below threshold {filters.get('min_trades')}")
    if metrics.market_count < int(filters.get("min_markets", 20) or 0):
        insufficient_reasons.append(f"market_count {metrics.market_count} below threshold {filters.get('min_markets')}")
    if metrics.active_days is None or metrics.active_days < int(filters.get("min_active_days", 14) or 0):
        insufficient_reasons.append(f"active_days {metrics.active_days} below threshold {filters.get('min_active_days')}")
    if metrics.total_volume is None:
        insufficient_reasons.append("total_volume missing")

    insufficient = bool(insufficient_reasons)
    reasons.extend(insufficient_reasons)

    mm_score = compute_market_maker_score(metrics, metrics_extra, mm_cfg, reasons)
    lucky_score = lucky_wallet_score(metrics, lucky_cfg, reasons)
    latency_score, latency_reasons = latency_bot_score_from_alpha(alpha_rows or [])
    reasons.extend(f"latency bot signal: {reason}" for reason in latency_reasons)
    directional_score = directional_score_from_scores(metrics, insufficient, mm_score, lucky_score)

    if insufficient:
        class_label = "insufficient_sample"
    elif mm_score >= 0.65:
        class_label = "likely_market_maker"
    elif latency_score is not None and latency_score >= 0.65:
        class_label = "likely_latency_bot"
    elif lucky_score >= 0.65:
        class_label = "lucky_wallet"
    elif directional_score >= 0.50:
        class_label = "likely_directional"
    else:
        class_label = "unknown"

    if latency_score is None:
        reasons.append("latency_bot_score not computed: not_enough_alpha_data")
    return ClassificationValues(
        wallet_address=metrics.wallet_address,
        class_label=class_label,
        market_maker_score=round(mm_score, 4),
        latency_bot_score=round(latency_score, 4) if latency_score is not None else None,
        lucky_wallet_score=round(lucky_score, 4),
        directional_score=round(directional_score, 4),
        insufficient_sample_flag=insufficient,
        reasons=reasons,
    )


def compute_market_maker_score(metrics: WalletMetrics, metrics_extra: dict[str, Any], mm_cfg: dict[str, Any], reasons: list[str]) -> float:
    score = 0.0
    min_volume = float(mm_cfg.get("min_volume", 1_000_000) or 0)
    min_trades = int(mm_cfg.get("min_trades", 5_000) or 0)
    max_roi = float(mm_cfg.get("max_roi_on_volume", 0.01) or 0.0)
    both_side_threshold = float(mm_cfg.get("both_sides_same_market_threshold", 0.25) or 0.0)

    if metrics.total_volume is not None and metrics.total_volume >= min_volume:
        score += 0.30
        reasons.append(f"market maker signal: volume {metrics.total_volume:.2f} >= {min_volume:.2f}")
    if metrics.trade_count >= min_trades:
        score += 0.25
        reasons.append(f"market maker signal: trade_count {metrics.trade_count} >= {min_trades}")
    if metrics.roi_on_volume is not None and abs(metrics.roi_on_volume) <= max_roi:
        score += 0.20
        reasons.append(f"market maker signal: low absolute ROI on volume {metrics.roi_on_volume:.4f}")
    both_side_share = metrics_extra.get("both_side_market_share")
    if both_side_share is not None and float(both_side_share) >= both_side_threshold:
        score += 0.20
        reasons.append(f"market maker signal: both-side market share {float(both_side_share):.2f}")
    if metrics.market_count >= 100:
        score += 0.05
        reasons.append("market maker signal: broad market coverage")
    return min(score, 1.0)


def lucky_wallet_score(metrics: WalletMetrics, lucky_cfg: dict[str, Any], reasons: list[str]) -> float:
    score = 0.0
    max_trades = int(lucky_cfg.get("max_trades", 100) or 0)
    max_markets = int(lucky_cfg.get("max_markets", 20) or 0)
    concentration_threshold = float(lucky_cfg.get("min_top_1_market_pnl_share", 0.60) or 0.0)

    if metrics.trade_count <= max_trades:
        score += 0.25
        reasons.append(f"lucky wallet signal: trade_count {metrics.trade_count} <= {max_trades}")
    if metrics.market_count <= max_markets:
        score += 0.25
        reasons.append(f"lucky wallet signal: market_count {metrics.market_count} <= {max_markets}")
    if metrics.active_days is not None and metrics.active_days <= 14:
        score += 0.20
        reasons.append(f"lucky wallet signal: short active history {metrics.active_days} days")
    if metrics.top_1_market_pnl_share is not None and metrics.top_1_market_pnl_share >= concentration_threshold:
        score += 0.30
        reasons.append(f"lucky wallet signal: top market PnL share {metrics.top_1_market_pnl_share:.2f}")
    return min(score, 1.0)


def directional_score_from_scores(
    metrics: WalletMetrics,
    insufficient: bool,
    market_maker_score_value: float,
    lucky_score_value: float,
) -> float:
    if insufficient:
        return 0.0
    score = 0.50
    if metrics.roi_on_volume is not None and metrics.roi_on_volume > 0:
        score += 0.20
    if metrics.market_count >= 20:
        score += 0.15
    if metrics.active_days is not None and metrics.active_days >= 14:
        score += 0.15
    score -= max(market_maker_score_value, lucky_score_value) * 0.50
    return max(0.0, min(score, 1.0))


def upsert_wallet_classification(session: Session, values: ClassificationValues) -> None:
    row = session.get(WalletClassification, values.wallet_address)
    if row is None:
        row = WalletClassification(wallet_address=values.wallet_address, class_label=values.class_label)
        session.add(row)
    row.computed_at = utc_now()
    row.class_label = values.class_label
    row.market_maker_score = values.market_maker_score
    row.latency_bot_score = values.latency_bot_score
    row.lucky_wallet_score = values.lucky_wallet_score
    row.directional_score = values.directional_score
    row.insufficient_sample_flag = values.insufficient_sample_flag
    row.reasons_json = json_dumps(values.reasons)

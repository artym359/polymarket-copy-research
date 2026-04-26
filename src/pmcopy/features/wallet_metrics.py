from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from statistics import mean, median
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmcopy.config import database_url
from pmcopy.db import (
    Market,
    ReconstructedPositionEvent,
    Trade,
    Wallet,
    WalletMetrics,
    WalletSnapshot,
    init_db,
    json_dumps,
    json_loads,
    session_scope,
    utc_now,
)


@dataclass
class WalletMetricValues:
    wallet_address: str
    total_pnl: float | None
    realized_pnl: float | None
    unrealized_pnl: float | None
    total_volume: float | None
    edge_on_volume: float | None
    roi_on_volume: float | None
    pnl_per_traded_dollar: float | None
    max_capital_at_risk: float | None
    return_on_max_capital_at_risk: float | None
    max_exposure_method: str | None
    max_exposure_confidence: str | None
    average_capital_at_risk: float | None
    return_on_average_capital_at_risk: float | None
    average_exposure_method: str | None
    average_exposure_confidence: str | None
    trade_count: int
    market_count: int
    active_days: int | None
    avg_trade_size: float | None
    median_trade_size: float | None
    win_rate_estimate: float | None
    max_drawdown_estimate: float | None
    top_1_market_pnl_share: float | None
    top_5_market_pnl_share: float | None
    main_category: str | None
    category_breakdown: dict[str, dict[str, float | int]]
    metrics: dict[str, Any]


def compute_all_wallet_metrics(
    config: dict[str, Any],
    limit: int | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    init_db(config)
    with session_scope(database_url(config)) as session:
        stmt = select(Wallet.wallet_address).order_by(Wallet.wallet_address)
        if limit:
            stmt = stmt.limit(limit)
        wallets = list(session.scalars(stmt))

    computed = 0
    total = len(wallets)
    for index, wallet_address in enumerate(wallets, start=1):
        if progress_callback:
            progress_callback(index, total, wallet_address)
        with session_scope(database_url(config)) as session:
            values = compute_wallet_metrics_values(session, wallet_address)
            upsert_wallet_metrics(session, values)
            computed += 1
    return computed


def compute_wallet_metrics_values(session: Session, wallet_address: str) -> WalletMetricValues:
    trades = list(session.scalars(select(Trade).where(Trade.wallet_address == wallet_address)))
    snapshots = list(session.scalars(select(WalletSnapshot).where(WalletSnapshot.wallet_address == wallet_address)))
    reconstructed_events = list(
        session.scalars(
            select(ReconstructedPositionEvent)
            .where(ReconstructedPositionEvent.wallet_address == wallet_address)
            .order_by(ReconstructedPositionEvent.timestamp, ReconstructedPositionEvent.id)
        )
    )

    trade_values = [trade.usd_value for trade in trades if trade.usd_value is not None]
    total_volume = sum(trade_values) if trade_values else None
    trade_count = len(trades)
    market_ids = {trade.market_id for trade in trades if trade.market_id}
    market_count = len(market_ids)
    active_dates: set[date] = {trade.timestamp.date() for trade in trades if trade.timestamp}
    active_days = len(active_dates) if active_dates else None
    avg_trade_size = mean(trade_values) if trade_values else None
    median_trade_size = median(trade_values) if trade_values else None

    category_breakdown, main_category = category_metrics(session, trades)
    pnl_by_market, realized_pnl, unrealized_pnl = pnl_metrics_from_snapshots(snapshots)
    total_pnl = none_sum(realized_pnl, unrealized_pnl)
    edge_on_volume = safe_div(total_pnl, total_volume)
    top_1_share, top_5_share = concentration_shares(pnl_by_market)
    win_rate = win_rate_from_market_pnl(pnl_by_market)
    both_side_share = both_side_market_share(trades)
    exposure = exposure_metrics(reconstructed_events, snapshots, total_pnl)

    warnings: list[str] = []
    if total_pnl is None:
        warnings.append("PnL unavailable: no usable Data API position or closed-position PnL fields were ingested.")
    else:
        warnings.append("PnL uses Data API position/closed-position fields and is not reconstructed from fills.")
    warnings.append("edge_on_volume is PnL divided by traded volume; it is not return on capital.")
    warnings.append("Max drawdown unavailable: no historical equity curve or timestamped portfolio valuation is ingested in Phase 3.")
    if top_1_share is None:
        warnings.append("Market PnL concentration unavailable because per-market PnL is missing or zero.")
    if total_volume is None:
        warnings.append("Volume unavailable because ingested trades had no price/size or USD value fields.")
    warnings.extend(exposure["warnings"])

    metrics = {
        "warnings": warnings,
        "pnl_by_market": pnl_by_market,
        "both_side_market_share": both_side_share,
        "source_trade_count": trade_count,
        "snapshot_count": len(snapshots),
        "reconstructed_position_event_count": len(reconstructed_events),
        "edge_on_volume": round_float(edge_on_volume),
        "pnl_per_traded_dollar": round_float(edge_on_volume),
        "exposure_warnings": exposure["warnings"],
    }

    return WalletMetricValues(
        wallet_address=wallet_address,
        total_pnl=round_float(total_pnl),
        realized_pnl=round_float(realized_pnl),
        unrealized_pnl=round_float(unrealized_pnl),
        total_volume=round_float(total_volume),
        edge_on_volume=round_float(edge_on_volume),
        roi_on_volume=round_float(edge_on_volume),
        pnl_per_traded_dollar=round_float(edge_on_volume),
        max_capital_at_risk=round_float(exposure["max_capital_at_risk"]),
        return_on_max_capital_at_risk=round_float(exposure["return_on_max_capital_at_risk"]),
        max_exposure_method=exposure["max_exposure_method"],
        max_exposure_confidence=exposure["max_exposure_confidence"],
        average_capital_at_risk=round_float(exposure["average_capital_at_risk"]),
        return_on_average_capital_at_risk=round_float(exposure["return_on_average_capital_at_risk"]),
        average_exposure_method=exposure["average_exposure_method"],
        average_exposure_confidence=exposure["average_exposure_confidence"],
        trade_count=trade_count,
        market_count=market_count,
        active_days=active_days,
        avg_trade_size=round_float(avg_trade_size),
        median_trade_size=round_float(median_trade_size),
        win_rate_estimate=round_float(win_rate),
        max_drawdown_estimate=None,
        top_1_market_pnl_share=round_float(top_1_share),
        top_5_market_pnl_share=round_float(top_5_share),
        main_category=main_category,
        category_breakdown=category_breakdown,
        metrics=metrics,
    )


def upsert_wallet_metrics(session: Session, values: WalletMetricValues) -> None:
    row = session.get(WalletMetrics, values.wallet_address)
    if row is None:
        row = WalletMetrics(wallet_address=values.wallet_address)
        session.add(row)
    row.computed_at = utc_now()
    row.total_pnl = values.total_pnl
    row.realized_pnl = values.realized_pnl
    row.unrealized_pnl = values.unrealized_pnl
    row.total_volume = values.total_volume
    row.roi_on_volume = values.roi_on_volume
    row.max_capital_at_risk = values.max_capital_at_risk
    row.return_on_max_capital_at_risk = values.return_on_max_capital_at_risk
    row.max_exposure_method = values.max_exposure_method
    row.max_exposure_confidence = values.max_exposure_confidence
    row.average_capital_at_risk = values.average_capital_at_risk
    row.return_on_average_capital_at_risk = values.return_on_average_capital_at_risk
    row.average_exposure_method = values.average_exposure_method
    row.average_exposure_confidence = values.average_exposure_confidence
    row.trade_count = values.trade_count
    row.market_count = values.market_count
    row.active_days = values.active_days
    row.avg_trade_size = values.avg_trade_size
    row.median_trade_size = values.median_trade_size
    row.win_rate_estimate = values.win_rate_estimate
    row.max_drawdown_estimate = values.max_drawdown_estimate
    row.top_1_market_pnl_share = values.top_1_market_pnl_share
    row.top_5_market_pnl_share = values.top_5_market_pnl_share
    row.main_category = values.main_category
    row.category_breakdown_json = json_dumps(values.category_breakdown)
    row.metrics_json = json_dumps(values.metrics)


def category_metrics(session: Session, trades: list[Trade]) -> tuple[dict[str, dict[str, float | int]], str | None]:
    by_market = {market.market_id: market for market in session.scalars(select(Market))}
    breakdown: dict[str, dict[str, float | int]] = defaultdict(lambda: {"volume": 0.0, "trade_count": 0})
    for trade in trades:
        market = by_market.get(trade.market_id or "")
        category = market.category if market and market.category else "unknown"
        breakdown[category]["trade_count"] = int(breakdown[category]["trade_count"]) + 1
        if trade.usd_value is not None:
            breakdown[category]["volume"] = float(breakdown[category]["volume"]) + trade.usd_value
    if not breakdown:
        return {}, None
    normalized = {key: {"volume": round_float(value["volume"]) or 0.0, "trade_count": int(value["trade_count"])} for key, value in breakdown.items()}
    main_category = max(normalized.items(), key=lambda item: (item[1]["volume"], item[1]["trade_count"]))[0]
    return normalized, main_category


def pnl_metrics_from_snapshots(snapshots: list[WalletSnapshot]) -> tuple[dict[str, float], float | None, float | None]:
    latest = latest_snapshots_by_position(snapshots)
    realized_values: list[float] = []
    unrealized_values: list[float] = []
    pnl_by_market: Counter[str] = Counter()

    for snapshot in latest:
        market_key = snapshot.market_id or "unknown"
        if snapshot.source_endpoint.endswith("/closed-positions") and snapshot.realized_pnl is not None:
            realized_values.append(snapshot.realized_pnl)
            pnl_by_market[market_key] += snapshot.realized_pnl
        elif snapshot.source_endpoint.endswith("/positions") and snapshot.cash_pnl is not None:
            unrealized_values.append(snapshot.cash_pnl)
            pnl_by_market[market_key] += snapshot.cash_pnl

    realized = sum(realized_values) if realized_values else None
    unrealized = sum(unrealized_values) if unrealized_values else None
    return {key: round_float(value) or 0.0 for key, value in pnl_by_market.items()}, realized, unrealized


def latest_snapshots_by_position(snapshots: list[WalletSnapshot]) -> list[WalletSnapshot]:
    selected: dict[tuple[str, str], WalletSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.source_endpoint.endswith("/value"):
            continue
        key = (snapshot.source_endpoint, snapshot.token_id or snapshot.market_id or snapshot.id)
        existing = selected.get(key)
        if existing is None or snapshot.fetched_at >= existing.fetched_at:
            selected[key] = snapshot
    return list(selected.values())


def exposure_metrics(
    reconstructed_events: list[ReconstructedPositionEvent],
    snapshots: list[WalletSnapshot],
    total_pnl: float | None,
) -> dict[str, Any]:
    reconstructed = exposure_from_reconstructed_events(reconstructed_events)
    if reconstructed["max_capital_at_risk"] is not None:
        max_capital = reconstructed["max_capital_at_risk"]
        average_capital = reconstructed["average_capital_at_risk"]
        confidence = "reconstructed_positions"
        average_confidence = (
            "reconstructed_positions_time_weighted" if average_capital is not None else "unavailable"
        )
        warnings = list(reconstructed["warnings"])
        if average_capital is None:
            warnings.append("exposure_metrics_unavailable: average exposure needs at least two timestamped reconstructed events.")
        return {
            "max_capital_at_risk": max_capital,
            "return_on_max_capital_at_risk": safe_div(total_pnl, max_capital),
            "max_exposure_method": "reconstructed_position_events",
            "max_exposure_confidence": confidence,
            "average_capital_at_risk": average_capital,
            "return_on_average_capital_at_risk": safe_div(total_pnl, average_capital),
            "average_exposure_method": "reconstructed_position_events" if average_capital is not None else None,
            "average_exposure_confidence": average_confidence,
            "warnings": warnings,
        }

    proxy = exposure_from_snapshots(snapshots)
    warnings = list(reconstructed["warnings"]) + list(proxy["warnings"])
    if proxy["max_capital_at_risk"] is None:
        warnings.append("exposure_metrics_unavailable: no reconstructed exposure or usable Data API position values.")
    elif proxy["average_capital_at_risk"] is None:
        warnings.append("exposure_metrics_proxy_only: max exposure uses Data API position values; average exposure is unavailable.")
    else:
        warnings.append("exposure_metrics_proxy_only: exposure metrics use Data API position snapshots.")

    return {
        "max_capital_at_risk": proxy["max_capital_at_risk"],
        "return_on_max_capital_at_risk": safe_div(total_pnl, proxy["max_capital_at_risk"]),
        "max_exposure_method": proxy["max_exposure_method"],
        "max_exposure_confidence": proxy["max_exposure_confidence"],
        "average_capital_at_risk": proxy["average_capital_at_risk"],
        "return_on_average_capital_at_risk": safe_div(total_pnl, proxy["average_capital_at_risk"]),
        "average_exposure_method": proxy["average_exposure_method"],
        "average_exposure_confidence": proxy["average_exposure_confidence"],
        "warnings": warnings,
    }


def exposure_from_reconstructed_events(events: list[ReconstructedPositionEvent]) -> dict[str, Any]:
    usable = [
        event
        for event in events
        if event.timestamp is not None
        and event.position_id
        and event.position_after is not None
        and event.price is not None
        and event.event_type != "missing_prior_inventory"
    ]
    if not usable:
        return {
            "max_capital_at_risk": None,
            "average_capital_at_risk": None,
            "warnings": ["No usable reconstructed position events found for exposure metrics."],
        }

    exposure_by_position: dict[str, float] = {}
    exposure_points: list[tuple[datetime, float]] = []
    for event in usable:
        position_after = max(float(event.position_after or 0.0), 0.0)
        price = max(float(event.price or 0.0), 0.0)
        exposure_by_position[event.position_id or ""] = position_after * price
        exposure_points.append((event.timestamp, sum(exposure_by_position.values())))

    max_exposure = max((value for _, value in exposure_points), default=None)
    average_exposure = time_weighted_average_exposure(exposure_points)
    warnings: list[str] = []
    if max_exposure is None or max_exposure <= 0:
        warnings.append("Reconstructed positions exist, but reconstructed exposure is zero or unavailable.")
        max_exposure = None
    if average_exposure is None:
        warnings.append("Average exposure unavailable because reconstructed events do not span a positive duration.")
    return {
        "max_capital_at_risk": max_exposure,
        "average_capital_at_risk": average_exposure,
        "warnings": warnings,
    }


def exposure_from_snapshots(snapshots: list[WalletSnapshot]) -> dict[str, Any]:
    latest = latest_snapshots_by_position(snapshots)
    max_values = [snapshot_max_exposure_value(snapshot) for snapshot in latest]
    max_values = [value for value in max_values if value is not None and value > 0]
    max_capital = sum(max_values) if max_values else None

    samples: dict[datetime, float] = defaultdict(float)
    for snapshot in snapshots:
        value = snapshot_current_exposure_value(snapshot)
        if value is not None and value > 0 and snapshot.fetched_at is not None:
            samples[snapshot.fetched_at] += value
    average_capital = time_weighted_average_exposure(sorted(samples.items()))

    return {
        "max_capital_at_risk": max_capital,
        "max_exposure_method": "wallet_snapshot_position_values" if max_capital is not None else None,
        "max_exposure_confidence": "data_api_proxy" if max_capital is not None else "unavailable",
        "average_capital_at_risk": average_capital,
        "average_exposure_method": "wallet_snapshot_position_values" if average_capital is not None else None,
        "average_exposure_confidence": "snapshots_proxy" if average_capital is not None else "unavailable",
        "warnings": [],
    }


def snapshot_max_exposure_value(snapshot: WalletSnapshot) -> float | None:
    raw = json_loads(snapshot.raw_json, {}) or {}
    candidates = [
        snapshot.initial_value,
        snapshot.current_value,
        snapshot.value,
        first_float(raw, ("initialValue", "initial_value", "totalBought", "total_bought")),
        multiply_or_none(snapshot.size, snapshot.avg_price),
    ]
    usable = [abs(float(value)) for value in candidates if value is not None]
    return max(usable) if usable else None


def snapshot_current_exposure_value(snapshot: WalletSnapshot) -> float | None:
    raw = json_loads(snapshot.raw_json, {}) or {}
    candidates = [
        snapshot.current_value,
        snapshot.value,
        first_float(raw, ("currentValue", "current_value")),
        multiply_or_none(snapshot.size, snapshot.avg_price),
    ]
    usable = [abs(float(value)) for value in candidates if value is not None]
    return max(usable) if usable else None


def time_weighted_average_exposure(points: list[tuple[datetime, float]]) -> float | None:
    if len(points) < 2:
        return None
    ordered = sorted(points, key=lambda item: item[0])
    weighted = 0.0
    total_seconds = 0.0
    for (timestamp, exposure), (next_timestamp, _) in zip(ordered, ordered[1:]):
        duration = (next_timestamp - timestamp).total_seconds()
        if duration <= 0:
            continue
        weighted += float(exposure or 0.0) * duration
        total_seconds += duration
    if total_seconds <= 0:
        return None
    return weighted / total_seconds


def concentration_shares(pnl_by_market: dict[str, float]) -> tuple[float | None, float | None]:
    if not pnl_by_market:
        return None, None
    values = sorted((abs(value) for value in pnl_by_market.values()), reverse=True)
    denominator = sum(values)
    if denominator <= 0:
        return None, None
    return values[0] / denominator, sum(values[:5]) / denominator


def win_rate_from_market_pnl(pnl_by_market: dict[str, float]) -> float | None:
    non_zero = [value for value in pnl_by_market.values() if value != 0]
    if not non_zero:
        return None
    return sum(1 for value in non_zero if value > 0) / len(non_zero)


def both_side_market_share(trades: list[Trade]) -> float | None:
    sides_by_market: dict[str, set[str]] = defaultdict(set)
    for trade in trades:
        if trade.market_id and trade.side:
            sides_by_market[trade.market_id].add(trade.side.upper())
    if not sides_by_market:
        return None
    both_side_count = sum(1 for sides in sides_by_market.values() if {"BUY", "SELL"}.issubset(sides))
    return both_side_count / len(sides_by_market)


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def multiply_or_none(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return float(left) * float(right)


def none_sum(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def round_float(value: Any, digits: int = 8) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)

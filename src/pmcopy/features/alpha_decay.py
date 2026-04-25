from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pmcopy.api.clob import ClobClient
from pmcopy.config import database_url
from pmcopy.db import (
    AlphaDecayResult,
    Market,
    OrderbookSnapshot,
    PriceHistory,
    ReconstructedPosition,
    ReconstructedPositionEvent,
    Trade,
    Wallet,
    init_db,
    json_dumps,
    json_loads,
    session_scope,
)
from pmcopy.features.data_quality import quality_rank
from pmcopy.features.fees import fee_rate_for_category, round_trip_fee
from pmcopy.features.liquidity import best_bid_ask, proxy_slippage, simulate_orderbook_fill
from pmcopy.features.position_reconstruction import (
    ReconstructionConfig,
    reconstruct_wallet_positions_in_session,
)


@dataclass
class AlphaDecayConfig:
    delays_seconds: list[int]
    position_size_usd: float
    max_spread: float | None
    max_entry_degradation: float | None
    allowed_data_quality: list[str]
    exit_rule: str
    limit: int | None = None
    categories: list[str] | None = None
    date_start: datetime | None = None
    date_end: datetime | None = None
    historical_mode: str = "price_history_only"
    exit_delay_seconds: int | None = None
    min_exit_fraction: float = 0.5
    max_holding_hours: float | None = None
    allow_partial_exits: bool = True
    sizing_mode: str = "fixed_usd"
    copy_ratio: float = 0.001
    warmup_days: int = 90
    debug_alpha: bool = False
    debug_alpha_limit: int = 10


@dataclass
class PricePoint:
    timestamp: datetime
    price: float
    raw: Any = None


@dataclass
class PriceHistoryLoad:
    token_id: str
    endpoint_url: str
    raw_payload: Any
    points: list[PricePoint]
    parse_failed: bool = False

    @property
    def first_timestamp(self) -> datetime | None:
        return self.points[0].timestamp if self.points else None

    @property
    def last_timestamp(self) -> datetime | None:
        return self.points[-1].timestamp if self.points else None


@dataclass
class PriceLookup:
    price: float | None
    point: PricePoint | None
    distance_seconds: float | None
    reason: str | None
    load: PriceHistoryLoad


@dataclass
class EntryEstimate:
    price: float | None
    data_quality: str
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    liquidity_available: float | None = None
    slippage: float | None = None
    skip_reason: str | None = None
    lookup: PriceLookup | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitEstimate:
    price: float | None
    data_quality: str
    exit_time: datetime | None = None
    skip_reason: str | None = None
    lookup: PriceLookup | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WalletExitMatch:
    exit_trades: list[Trade]
    exit_fraction: float
    exit_kind: str
    whale_exit_time: datetime


@dataclass
class CopiedLot:
    shares: float
    remaining_shares: float
    entry_price: float
    entry_time: datetime
    entry_event_id: str


def alpha_config_from_values(
    config: dict[str, Any],
    *,
    delays: list[int] | None = None,
    position_size_usd: float | None = None,
    max_spread: float | None = None,
    max_entry_degradation: float | None = None,
    allowed_data_quality: list[str] | None = None,
    exit_rule: str | None = None,
    limit: int | None = None,
    categories: list[str] | None = None,
    date_start: datetime | None = None,
    date_end: datetime | None = None,
    historical_mode: str | None = None,
    exit_delay_seconds: int | None = None,
    min_exit_fraction: float | None = None,
    max_holding_hours: float | None = None,
    allow_partial_exits: bool | None = None,
    sizing_mode: str | None = None,
    copy_ratio: float | None = None,
    warmup_days: int | None = None,
    debug_alpha: bool = False,
    debug_alpha_limit: int = 10,
) -> AlphaDecayConfig:
    cfg = config.get("alpha_decay", {})
    return AlphaDecayConfig(
        delays_seconds=delays or list(cfg.get("delays_seconds", [10, 30, 60, 300, 900, 3600, 21600, 86400])),
        position_size_usd=float(position_size_usd or cfg.get("default_position_size_usd", 2)),
        max_spread=max_spread if max_spread is not None else cfg.get("max_spread"),
        max_entry_degradation=max_entry_degradation if max_entry_degradation is not None else cfg.get("max_entry_degradation"),
        allowed_data_quality=allowed_data_quality or list(cfg.get("allowed_data_quality_levels", ["exact_orderbook", "price_history_proxy"])),
        exit_rule=exit_rule or str(cfg.get("default_exit_rule", "hold_to_resolution")),
        limit=limit,
        categories=categories,
        date_start=to_utc(date_start) if date_start else None,
        date_end=to_utc(date_end) if date_end else None,
        historical_mode=historical_mode or str(cfg.get("historical_mode", "price_history_only")),
        exit_delay_seconds=exit_delay_seconds if exit_delay_seconds is not None else cfg.get("exit_delay_seconds"),
        min_exit_fraction=float(min_exit_fraction if min_exit_fraction is not None else cfg.get("min_exit_fraction", 0.5)),
        max_holding_hours=max_holding_hours if max_holding_hours is not None else cfg.get("max_holding_hours"),
        allow_partial_exits=bool(allow_partial_exits if allow_partial_exits is not None else cfg.get("allow_partial_exits", True)),
        sizing_mode=sizing_mode or str(cfg.get("sizing_mode", "fixed_usd")),
        copy_ratio=float(copy_ratio if copy_ratio is not None else cfg.get("copy_ratio", 0.001)),
        warmup_days=int(warmup_days if warmup_days is not None else cfg.get("warmup_days", 90)),
        debug_alpha=debug_alpha,
        debug_alpha_limit=debug_alpha_limit,
    )


def compute_alpha_decay(
    config: dict[str, Any],
    *,
    wallet_address: str | None = None,
    alpha_config: AlphaDecayConfig | None = None,
) -> dict[str, Any]:
    init_db(config)
    alpha_config = alpha_config or alpha_config_from_values(config)
    with session_scope(database_url(config)) as session:
        wallets = selected_wallets(session, wallet_address)
        clob = ClobClient(config, session=session)
        cache: dict[str, Any] = {}
        total_rows = 0
        skipped_trades = 0
        quality_levels: list[str] = []
        skip_reasons: dict[str, int] = {}
        follow_exit_stats = {"matched_wallet_exits": 0, "no_wallet_exit_found": 0, "partial_exits": 0, "full_exits": 0}
        lifecycle_stats = {
            "reconstructed_positions": 0,
            "closed_positions": 0,
            "missing_prior_inventory": 0,
            "orphan_sell": 0,
            "usable_lifecycle_copy_trades": 0,
            "partial_exits": 0,
            "full_exits": 0,
            "lifecycle_copy_pnl": 0.0,
        }
        debug: list[dict[str, Any]] = []
        debug_trade_ids: set[str] = set()
        for wallet in wallets:
            if alpha_config.exit_rule == "reconstructed_wallet_lifecycle":
                recon_config = ReconstructionConfig(
                    wallet_address=wallet,
                    analysis_start=alpha_config.date_start,
                    analysis_end=alpha_config.date_end,
                    warmup_days=alpha_config.warmup_days,
                )
                recon_stats = reconstruct_wallet_positions_in_session(session, wallet, recon_config)
                lifecycle_stats["reconstructed_positions"] += int(recon_stats.get("positions", 0))
                lifecycle_stats["closed_positions"] += int(recon_stats.get("closed_positions", 0))
                lifecycle_stats["missing_prior_inventory"] += int(recon_stats.get("missing_prior_inventory", 0))
                lifecycle_stats["orphan_sell"] += int(recon_stats.get("orphan_sell", 0))
                for delay in alpha_config.delays_seconds:
                    rows = simulate_reconstructed_lifecycles(session, clob, cache, config, wallet, delay, alpha_config)
                    for result in rows:
                        upsert_alpha_result(session, result)
                        total_rows += 1
                        quality_levels.append(result.data_quality)
                        if result.skip_reason:
                            skip_reasons[result.skip_reason] = skip_reasons.get(result.skip_reason, 0) + 1
                        else:
                            lifecycle_stats["usable_lifecycle_copy_trades"] += 1
                            lifecycle_stats["lifecycle_copy_pnl"] += float(result.net_pnl or 0.0)
                            raw = json_loads(result.raw_json, {}) or {}
                            lifecycle = raw.get("lifecycle", {}) if isinstance(raw, dict) else {}
                            if lifecycle.get("full_exit_count", 0):
                                lifecycle_stats["full_exits"] += 1
                            if lifecycle.get("partial_exit_count", 0):
                                lifecycle_stats["partial_exits"] += 1
                        if alpha_config.debug_alpha and len(debug) < alpha_config.debug_alpha_limit:
                            debug.append(debug_from_result(result))
                continue
            trades = wallet_trades(session, wallet, alpha_config.limit, alpha_config.date_start, alpha_config.date_end, alpha_config.categories)
            for trade in trades:
                if not usable_trade(trade):
                    skipped_trades += 1
                for delay in alpha_config.delays_seconds:
                    result = simulate_trade_delay(session, clob, cache, config, trade, delay, alpha_config)
                    upsert_alpha_result(session, result)
                    total_rows += 1
                    quality_levels.append(result.data_quality)
                    if result.skip_reason:
                        skip_reasons[result.skip_reason] = skip_reasons.get(result.skip_reason, 0) + 1
                    update_follow_exit_stats(result, follow_exit_stats)
                    if alpha_config.debug_alpha and trade.id not in debug_trade_ids and len(debug_trade_ids) < alpha_config.debug_alpha_limit:
                        debug_trade_ids.add(trade.id)
                        debug.append(debug_from_result(result))
        return {
            "wallets": len(wallets),
            "rows": total_rows,
            "skipped_trades": skipped_trades,
            "data_quality_levels": quality_levels,
            "skip_reasons": skip_reasons,
            "follow_wallet_exit": follow_exit_stats,
            "reconstructed_wallet_lifecycle": lifecycle_stats,
            "debug": debug,
        }


def update_follow_exit_stats(result: AlphaDecayResult, stats: dict[str, int]) -> None:
    if result.exit_rule != "follow_wallet_exit":
        return
    if result.skip_reason == "no_wallet_exit_found":
        stats["no_wallet_exit_found"] += 1
        return
    if result.skip_reason:
        return
    raw = json_loads(result.raw_json, {}) or {}
    exit_payload = raw.get("exit", {}) if isinstance(raw, dict) else {}
    kind = exit_payload.get("exit_kind") if isinstance(exit_payload, dict) else None
    stats["matched_wallet_exits"] += 1
    if kind == "partial":
        stats["partial_exits"] += 1
    elif kind == "full":
        stats["full_exits"] += 1


def selected_wallets(session: Session, wallet_address: str | None) -> list[str]:
    if wallet_address:
        return [wallet_address.lower()]
    trade_wallets = set(session.scalars(select(Trade.wallet_address).distinct()))
    if trade_wallets:
        return sorted(trade_wallets)
    return list(session.scalars(select(Wallet.wallet_address).order_by(Wallet.wallet_address)))


def wallet_trades(
    session: Session,
    wallet_address: str,
    limit: int | None,
    date_start: datetime | None = None,
    date_end: datetime | None = None,
    categories: list[str] | None = None,
) -> list[Trade]:
    stmt = (
        select(Trade)
        .where(Trade.wallet_address == wallet_address)
        .order_by(Trade.timestamp.desc().nullslast())
    )
    if date_start is not None:
        stmt = stmt.where(Trade.timestamp >= date_start)
    if date_end is not None:
        stmt = stmt.where(Trade.timestamp <= date_end)
    if categories:
        market_ids = list(session.scalars(select(Market.market_id).where(Market.category.in_(categories))))
        if not market_ids:
            return []
        stmt = stmt.where(Trade.market_id.in_(market_ids))
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def usable_trade(trade: Trade) -> bool:
    return bool(trade.token_id and trade.timestamp and trade.price is not None and trade.side)


def simulate_trade_delay(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    trade: Trade,
    delay_seconds: int,
    alpha_config: AlphaDecayConfig,
) -> AlphaDecayResult:
    trade_time = to_utc(trade.timestamp) if trade.timestamp else None
    copy_time = trade_time + timedelta(seconds=delay_seconds) if trade_time else None
    market = session.get(Market, trade.market_id) if trade.market_id else None
    category = market.category if market else None
    source_side = (trade.side or "").upper()
    whale_price = trade.price
    result = base_result(trade, delay_seconds, copy_time, alpha_config.exit_rule)

    preflight_reason = preflight_skip_reason(trade, source_side)
    if preflight_reason:
        mark_skip(result, "insufficient_data", preflight_reason, debug_payload(trade, copy_time, None, None, preflight_reason))
        return result

    entry = estimate_entry(session, clob, cache, config, trade.token_id or "", source_side, copy_time, alpha_config.position_size_usd, alpha_config.historical_mode)
    apply_entry(result, entry)
    if entry.price is None:
        mark_skip(result, entry.data_quality, entry.skip_reason or "missing_entry_price", debug_payload(trade, copy_time, None, entry.lookup, entry.skip_reason))
        return result

    result.entry_degradation = entry_degradation(source_side, whale_price, entry.price)
    if alpha_config.max_spread is not None and entry.spread is not None and entry.spread > alpha_config.max_spread:
        mark_skip(result, entry.data_quality, "max_spread_exceeded", debug_payload(trade, copy_time, None, entry.lookup, "max_spread_exceeded"))
        return result
    if (
        alpha_config.max_entry_degradation is not None
        and result.entry_degradation is not None
        and result.entry_degradation > alpha_config.max_entry_degradation
    ):
        mark_skip(result, entry.data_quality, "max_entry_degradation_exceeded", debug_payload(trade, copy_time, None, entry.lookup, "max_entry_degradation_exceeded"))
        return result

    if alpha_config.exit_rule == "follow_wallet_exit":
        return simulate_follow_wallet_exit(session, clob, cache, config, trade, market, source_side, copy_time, entry, result, alpha_config)

    exit_estimate = estimate_exit(session, clob, cache, config, trade.token_id or "", market, copy_time, alpha_config.exit_rule)
    if exit_estimate.price is None:
        mark_skip(
            result,
            min_quality(entry.data_quality, exit_estimate.data_quality),
            exit_estimate.skip_reason or "missing_exit_price",
            debug_payload(trade, copy_time, exit_estimate, entry.lookup, exit_estimate.skip_reason),
        )
        return result

    shares = alpha_config.position_size_usd / entry.price if entry.price > 0 else 0.0
    fee_rate, fee_warning = fee_rate_for_category(config, category)
    estimated_fee = round_trip_fee(shares, entry.price, exit_estimate.price, fee_rate)
    gross_pnl = signed_gross_pnl(source_side, shares, entry.price, exit_estimate.price)
    result.eventual_exit_price = round_float(exit_estimate.price)
    result.estimated_fee = round_float(estimated_fee)
    result.gross_pnl = round_float(gross_pnl)
    result.net_pnl = round_float(gross_pnl - estimated_fee - (entry.slippage or 0.0) * shares)
    result.data_quality = min_quality(entry.data_quality, exit_estimate.data_quality)
    result.data_quality_rank = quality_rank(result.data_quality)
    result.skip_reason = None
    result.raw_json = json_dumps(
        {
            "entry": entry.raw,
            "exit": exit_estimate.raw,
            "debug": debug_payload(trade, copy_time, exit_estimate, entry.lookup, None),
            "category": category,
            "fee_rate": fee_rate,
            "fee_warning": fee_warning,
            "position_size_usd": alpha_config.position_size_usd,
            "allowed_data_quality": alpha_config.allowed_data_quality,
            "historical_mode": alpha_config.historical_mode,
        }
    )
    return result


def simulate_reconstructed_lifecycles(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    wallet_address: str,
    delay_seconds: int,
    alpha_config: AlphaDecayConfig,
) -> list[AlphaDecayResult]:
    stmt = (
        select(ReconstructedPosition)
        .where(ReconstructedPosition.wallet_address == wallet_address)
        .where(ReconstructedPosition.status != "missing_prior_inventory")
        .order_by(ReconstructedPosition.opened_at.asc(), ReconstructedPosition.position_id.asc())
    )
    if alpha_config.categories:
        market_ids = list(session.scalars(select(Market.market_id).where(Market.category.in_(alpha_config.categories))))
        if not market_ids:
            return []
        stmt = stmt.where(ReconstructedPosition.market_id.in_(market_ids))
    if alpha_config.limit:
        stmt = stmt.limit(alpha_config.limit)

    rows: list[AlphaDecayResult] = []
    for position in session.scalars(stmt):
        result = simulate_reconstructed_lifecycle(session, clob, cache, config, position, delay_seconds, alpha_config)
        if result is not None:
            rows.append(result)
    return rows


def simulate_reconstructed_lifecycle(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    position: ReconstructedPosition,
    delay_seconds: int,
    alpha_config: AlphaDecayConfig,
) -> AlphaDecayResult | None:
    events = reconstructed_events(session, position)
    entry_events = [row for row in events if row.event_type in {"open_position", "increase_position"} and event_in_analysis(row)]
    if not entry_events:
        return None

    first_event = entry_events[0]
    result = lifecycle_base_result(position, first_event, delay_seconds, alpha_config)
    market = session.get(Market, position.market_id) if position.market_id else None
    category = market.category if market else None
    fee_rate, fee_warning = fee_rate_for_category(config, category)
    exit_delay = alpha_config.exit_delay_seconds if alpha_config.exit_delay_seconds is not None else delay_seconds

    lots: list[CopiedLot] = []
    copied_inventory = 0.0
    position_ratio: float | None = None
    worst_quality = "exact_orderbook"
    entry_segments: list[dict[str, Any]] = []
    exit_segments: list[dict[str, Any]] = []
    entry_fees = 0.0
    exit_fees = 0.0
    entry_slippage_cost = 0.0
    gross_pnl = 0.0
    weighted_entry_price = 0.0
    weighted_whale_entry_price = 0.0
    total_entry_shares = 0.0
    weighted_exit_price = 0.0
    total_exit_shares = 0.0
    full_exit_count = 0
    partial_exit_count = 0
    first_entry_lookup: PriceLookup | None = None
    first_entry_estimate: EntryEstimate | None = None
    last_exit_estimate: ExitEstimate | None = None

    for event in events:
        if event.timestamp is None:
            continue
        if event.event_type in {"open_position", "increase_position"}:
            if not event_in_analysis(event):
                continue
            whale_delta = max(event_delta_shares(event), 0.0)
            if whale_delta <= 0:
                continue
            target_time = to_utc(event.timestamp) + timedelta(seconds=delay_seconds)
            copied_usd = copied_entry_usd(event, whale_delta, alpha_config)
            if copied_usd <= 0:
                continue
            entry = estimate_entry(session, clob, cache, config, position.token_id, "BUY", target_time, copied_usd, alpha_config.historical_mode)
            first_entry_estimate = first_entry_estimate or entry
            first_entry_lookup = first_entry_lookup or entry.lookup
            if entry.price is None:
                reason = entry_price_skip_reason(entry.skip_reason or (entry.lookup.reason if entry.lookup else None))
                return lifecycle_skip_result(result, entry.data_quality, reason, position, entry_segments, exit_segments, entry.lookup, None)
            degradation = entry_degradation("BUY", float(event.price or 0.0), entry.price)
            if alpha_config.max_spread is not None and entry.spread is not None and entry.spread > alpha_config.max_spread:
                return lifecycle_skip_result(result, entry.data_quality, "max_spread_exceeded", position, entry_segments, exit_segments, entry.lookup, None)
            if alpha_config.max_entry_degradation is not None and degradation > alpha_config.max_entry_degradation:
                return lifecycle_skip_result(result, entry.data_quality, "max_entry_degradation_exceeded", position, entry_segments, exit_segments, entry.lookup, None)

            copied_shares = copied_usd / entry.price if entry.price > 0 else 0.0
            if alpha_config.sizing_mode == "proportional_to_position":
                if position_ratio is None:
                    position_after = float(event.position_after or whale_delta)
                    position_ratio = copied_shares / position_after if position_after > 0 else None
                elif position_ratio is not None:
                    target_inventory = max(0.0, float(event.position_after or 0.0) * position_ratio)
                    copied_shares = max(0.0, target_inventory - copied_inventory)
                    copied_usd = copied_shares * entry.price
            if copied_shares <= 0:
                continue

            lot = CopiedLot(
                shares=copied_shares,
                remaining_shares=copied_shares,
                entry_price=entry.price,
                entry_time=target_time,
                entry_event_id=event.id,
            )
            lots.append(lot)
            copied_inventory += copied_shares
            total_entry_shares += copied_shares
            weighted_entry_price += copied_shares * entry.price
            weighted_whale_entry_price += copied_shares * float(event.price or 0.0)
            entry_fees += copied_shares * fee_rate * entry.price * (1 - entry.price)
            entry_slippage_cost += copied_shares * float(entry.slippage or 0.0)
            worst_quality = min_quality(worst_quality, entry.data_quality)
            entry_segments.append(
                {
                    "event_id": event.id,
                    "trade_id": event.trade_id,
                    "event_type": event.event_type,
                    "whale_time": to_utc(event.timestamp).isoformat(),
                    "target_time": target_time.isoformat(),
                    "nearest_price_time": entry.lookup.point.timestamp.isoformat() if entry.lookup and entry.lookup.point else None,
                    "price_distance_seconds": entry.lookup.distance_seconds if entry.lookup else None,
                    "data_quality": entry.data_quality,
                    "skip_reason": None,
                    "whale_shares": round_float(whale_delta),
                    "copied_usd": round_float(copied_usd),
                    "copied_shares": round_float(copied_shares),
                    "entry_price": round_float(entry.price),
                    "whale_price": round_float(event.price),
                    "entry_degradation": round_float(degradation),
                }
            )
            continue

        if event.event_type not in {"partial_exit", "full_exit", "reduce_position"}:
            continue
        if copied_inventory <= 1e-9 or not lots:
            continue
        target_time = to_utc(event.timestamp) + timedelta(seconds=exit_delay)
        if result.copy_time and target_time <= to_utc(result.copy_time):
            return lifecycle_skip_result(result, worst_quality, "exit_before_copy_entry", position, entry_segments, exit_segments, first_entry_lookup, None)
        lookup = nearest_price_history(session, clob, cache, config, position.token_id, target_time, context="exit", require_history_extends=True)
        if lookup.price is None:
            reason = exit_price_skip_reason(lookup.reason)
            return lifecycle_skip_result(
                result,
                min_quality(worst_quality, "insufficient_data"),
                reason,
                position,
                entry_segments,
                exit_segments,
                first_entry_lookup,
                lookup,
            )

        shares_to_exit = copied_exit_shares(event, copied_inventory, position_ratio, alpha_config.sizing_mode)
        shares_to_exit = min(shares_to_exit, copied_inventory)
        if shares_to_exit <= 1e-9:
            continue
        realized = consume_lots(lots, shares_to_exit, lookup.price)
        copied_inventory -= realized["shares"]
        gross_pnl += realized["gross_pnl"]
        exit_fee = realized["shares"] * fee_rate * lookup.price * (1 - lookup.price)
        exit_fees += exit_fee
        total_exit_shares += realized["shares"]
        weighted_exit_price += realized["shares"] * lookup.price
        worst_quality = min_quality(worst_quality, "price_history_proxy")
        if event.event_type == "full_exit" or copied_inventory <= 1e-9:
            full_exit_count += 1
            copied_inventory = max(0.0, copied_inventory)
            exit_kind = "full"
        else:
            partial_exit_count += 1
            exit_kind = "partial"
        last_exit_estimate = ExitEstimate(
            lookup.price,
            "price_history_proxy",
            target_time,
            lookup=lookup,
            raw={"source": "reconstructed_wallet_lifecycle", "exit_time": target_time.isoformat()},
        )
        exit_segments.append(
            {
                "event_id": event.id,
                "trade_id": event.trade_id,
                "event_type": event.event_type,
                "exit_kind": exit_kind,
                "whale_time": to_utc(event.timestamp).isoformat(),
                "target_time": target_time.isoformat(),
                "nearest_price_time": lookup.point.timestamp.isoformat() if lookup.point else None,
                "price_distance_seconds": lookup.distance_seconds,
                "data_quality": "price_history_proxy",
                "skip_reason": None,
                "whale_reduced_shares": round_float(event_delta_shares(event)),
                "copied_shares": round_float(realized["shares"]),
                "exit_price": round_float(lookup.price),
                "gross_pnl": round_float(realized["gross_pnl"]),
                "fee": round_float(exit_fee),
            }
        )

    if not entry_segments:
        return lifecycle_skip_result(result, "insufficient_data", "no_reconstructed_position", position, entry_segments, exit_segments, None, None)
    if not exit_segments:
        return lifecycle_skip_result(result, worst_quality, "no_exit_event", position, entry_segments, exit_segments, first_entry_lookup, None)
    if copied_inventory > 1e-9 and not alpha_config.allow_partial_exits:
        return lifecycle_skip_result(result, worst_quality, "partial_exit_unmatched", position, entry_segments, exit_segments, first_entry_lookup, None)

    avg_entry = weighted_entry_price / total_entry_shares if total_entry_shares else None
    avg_whale_entry = weighted_whale_entry_price / total_entry_shares if total_entry_shares else None
    avg_exit = weighted_exit_price / total_exit_shares if total_exit_shares else None
    estimated_fee = entry_fees + exit_fees
    net_pnl = gross_pnl - estimated_fee - entry_slippage_cost

    result.copy_best_bid = round_float(first_entry_estimate.best_bid if first_entry_estimate else None)
    result.copy_best_ask = round_float(first_entry_estimate.best_ask if first_entry_estimate else None)
    result.copy_spread = round_float(first_entry_estimate.spread if first_entry_estimate else None)
    result.simulated_entry_price = round_float(avg_entry)
    result.entry_degradation = round_float((avg_entry - avg_whale_entry) if avg_entry is not None and avg_whale_entry is not None else None)
    result.liquidity_available = round_float(sum(float(item.get("copied_usd") or 0.0) for item in entry_segments))
    result.estimated_slippage = round_float(entry_slippage_cost)
    result.estimated_fee = round_float(estimated_fee)
    result.eventual_exit_price = round_float(avg_exit)
    result.gross_pnl = round_float(gross_pnl)
    result.net_pnl = round_float(net_pnl)
    result.data_quality = worst_quality
    result.data_quality_rank = quality_rank(worst_quality)
    result.skip_reason = None
    result.raw_json = json_dumps(
        {
            "entry": entry_segments[0],
            "exit": {
                "source": "reconstructed_wallet_lifecycle",
                "exit_kind": "full" if copied_inventory <= 1e-9 else "partial",
                "exit_time": last_exit_estimate.exit_time.isoformat() if last_exit_estimate and last_exit_estimate.exit_time else None,
                "entry_delay_seconds": delay_seconds,
                "exit_delay_seconds": exit_delay,
                "segments": exit_segments,
            },
            "lifecycle": {
                "position_id": position.position_id,
                "position_status": position.status,
                "sizing_mode": alpha_config.sizing_mode,
                "copy_ratio": alpha_config.copy_ratio,
                "entry_count": len(entry_segments),
                "exit_count": len(exit_segments),
                "partial_exit_count": partial_exit_count,
                "full_exit_count": full_exit_count,
                "remaining_copied_shares": round_float(copied_inventory),
                "copied_entry_shares": round_float(total_entry_shares),
                "copied_exit_shares": round_float(total_exit_shares),
            },
            "entries": entry_segments,
            "exits": exit_segments,
            "debug": lifecycle_debug_payload(position, result, first_entry_lookup, last_exit_estimate, None),
            "category": category,
            "fee_rate": fee_rate,
            "fee_warning": fee_warning,
            "position_size_usd": alpha_config.position_size_usd,
            "allowed_data_quality": alpha_config.allowed_data_quality,
            "historical_mode": alpha_config.historical_mode,
        }
    )
    return result


def reconstructed_events(session: Session, position: ReconstructedPosition) -> list[ReconstructedPositionEvent]:
    return list(
        session.scalars(
            select(ReconstructedPositionEvent)
            .where(ReconstructedPositionEvent.position_id == position.position_id)
            .order_by(ReconstructedPositionEvent.timestamp.asc(), ReconstructedPositionEvent.trade_id.asc(), ReconstructedPositionEvent.id.asc())
        )
    )


def lifecycle_base_result(
    position: ReconstructedPosition,
    first_event: ReconstructedPositionEvent,
    delay_seconds: int,
    alpha_config: AlphaDecayConfig,
) -> AlphaDecayResult:
    trade_time = to_utc(first_event.timestamp) if first_event.timestamp else None
    copy_time = trade_time + timedelta(seconds=delay_seconds) if trade_time else None
    result_id = stable_id(
        "alpha_lifecycle",
        position.position_id,
        first_event.trade_id,
        delay_seconds,
        alpha_config.exit_delay_seconds,
        alpha_config.sizing_mode,
        alpha_config.position_size_usd,
        alpha_config.copy_ratio,
    )
    return AlphaDecayResult(
        id=result_id,
        wallet_address=position.wallet_address,
        trade_id=first_event.trade_id,
        token_id=position.token_id,
        market_id=position.market_id,
        trade_time=trade_time,
        original_side="BUY",
        whale_price=first_event.price,
        whale_size=first_event.shares,
        delay_seconds=delay_seconds,
        copy_time=copy_time,
        exit_rule="reconstructed_wallet_lifecycle",
        data_quality="insufficient_data",
        data_quality_rank=0,
        raw_json="{}",
    )


def lifecycle_skip_result(
    result: AlphaDecayResult,
    data_quality: str,
    reason: str,
    position: ReconstructedPosition,
    entries: list[dict[str, Any]],
    exits: list[dict[str, Any]],
    entry_lookup: PriceLookup | None,
    exit_lookup: PriceLookup | None,
) -> AlphaDecayResult:
    result.data_quality = data_quality
    result.data_quality_rank = quality_rank(data_quality)
    result.skip_reason = reason
    result.raw_json = json_dumps(
        {
            "skip_reason": reason,
            "entry": entries[0] if entries else {},
            "exit": {
                "source": "reconstructed_wallet_lifecycle",
                "exit_time": exits[-1].get("target_time") if exits else None,
                "segments": exits,
            },
            "lifecycle": {
                "position_id": position.position_id,
                "position_status": position.status,
                "entry_count": len(entries),
                "exit_count": len(exits),
            },
            "entries": entries,
            "exits": exits,
            "debug": lifecycle_debug_payload(position, result, entry_lookup, ExitEstimate(None, "insufficient_data", lookup=exit_lookup) if exit_lookup else None, reason),
        }
    )
    return result


def event_in_analysis(event: ReconstructedPositionEvent) -> bool:
    raw = json_loads(event.raw_json, {}) or {}
    if not isinstance(raw, dict):
        return True
    return bool(raw.get("in_analysis_window", True))


def event_delta_shares(event: ReconstructedPositionEvent) -> float:
    raw = json_loads(event.raw_json, {}) or {}
    if isinstance(raw, dict) and raw.get("matched_shares") is not None:
        parsed = parse_float(raw.get("matched_shares"))
        if parsed is not None:
            return parsed
    if event.event_type in {"partial_exit", "full_exit", "reduce_position"}:
        return max(0.0, float(event.position_before or 0.0) - float(event.position_after or 0.0))
    return float(event.shares or 0.0)


def copied_entry_usd(event: ReconstructedPositionEvent, whale_delta: float, alpha_config: AlphaDecayConfig) -> float:
    if alpha_config.sizing_mode == "proportional_to_whale":
        whale_usd = whale_delta * float(event.price or 0.0)
        return whale_usd * alpha_config.copy_ratio
    return alpha_config.position_size_usd


def copied_exit_shares(
    event: ReconstructedPositionEvent,
    copied_inventory: float,
    position_ratio: float | None,
    sizing_mode: str,
) -> float:
    if sizing_mode == "proportional_to_position" and position_ratio is not None:
        target_inventory = max(0.0, float(event.position_after or 0.0) * position_ratio)
        return max(0.0, copied_inventory - target_inventory)
    if event.event_type == "full_exit":
        return copied_inventory
    before = float(event.position_before or 0.0)
    after = float(event.position_after or 0.0)
    fraction = max(0.0, min(1.0, (before - after) / before)) if before > 0 else 0.0
    return copied_inventory * fraction


def consume_lots(lots: list[CopiedLot], shares_to_exit: float, exit_price: float) -> dict[str, float]:
    remaining = shares_to_exit
    realized_shares = 0.0
    gross_pnl = 0.0
    while remaining > 1e-9 and lots:
        lot = lots[0]
        matched = min(lot.remaining_shares, remaining)
        lot.remaining_shares -= matched
        remaining -= matched
        realized_shares += matched
        gross_pnl += matched * (exit_price - lot.entry_price)
        if lot.remaining_shares <= 1e-9:
            lots.pop(0)
    return {"shares": realized_shares, "gross_pnl": gross_pnl}


def entry_price_skip_reason(reason: str | None) -> str:
    if reason in {"copy_price_too_far", "copy_time_before_history", "copy_time_after_history"}:
        return "entry_price_too_far"
    return "entry_price_missing"


def exit_price_skip_reason(reason: str | None) -> str:
    if reason == "exit_price_too_far":
        return "exit_price_too_far"
    return "exit_price_missing"


def lifecycle_debug_payload(
    position: ReconstructedPosition,
    result: AlphaDecayResult,
    entry_lookup: PriceLookup | None,
    exit_estimate: ExitEstimate | None,
    reason: str | None,
) -> dict[str, Any]:
    exit_lookup = exit_estimate.lookup if exit_estimate else None
    return {
        "wallet_address": position.wallet_address,
        "position_id": position.position_id,
        "token_id": position.token_id,
        "market_id": position.market_id,
        "trade_id": result.trade_id,
        "copy_time": result.copy_time.isoformat() if result.copy_time else None,
        "exit_time": exit_estimate.exit_time.isoformat() if exit_estimate and exit_estimate.exit_time else None,
        "nearest_copy_timestamp": entry_lookup.point.timestamp.isoformat() if entry_lookup and entry_lookup.point else None,
        "nearest_exit_timestamp": exit_lookup.point.timestamp.isoformat() if exit_lookup and exit_lookup.point else None,
        "copy_distance_seconds": entry_lookup.distance_seconds if entry_lookup else None,
        "exit_distance_seconds": exit_lookup.distance_seconds if exit_lookup else None,
        "parsed_copy_price": entry_lookup.price if entry_lookup else None,
        "parsed_exit_price": exit_lookup.price if exit_lookup else None,
        "insufficient_reason": reason,
    }


def simulate_follow_wallet_exit(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    trade: Trade,
    market: Market | None,
    source_side: str,
    copy_time: datetime,
    entry: EntryEstimate,
    result: AlphaDecayResult,
    alpha_config: AlphaDecayConfig,
) -> AlphaDecayResult:
    if source_side != "BUY":
        mark_skip(result, entry.data_quality, "inconsistent_trade_side", debug_payload(trade, copy_time, None, entry.lookup, "inconsistent_trade_side"))
        return result
    if not trade.size or trade.size <= 0:
        mark_skip(result, entry.data_quality, "cannot_reconstruct_position", debug_payload(trade, copy_time, None, entry.lookup, "cannot_reconstruct_position"))
        return result

    exit_match = find_wallet_exit(session, trade, alpha_config)
    if exit_match is None:
        mark_skip(result, entry.data_quality, "no_wallet_exit_found", debug_payload(trade, copy_time, None, entry.lookup, "no_wallet_exit_found"))
        return result
    if exit_match.exit_fraction < alpha_config.min_exit_fraction:
        mark_skip(result, entry.data_quality, "partial_exit_below_threshold", debug_payload(trade, copy_time, None, entry.lookup, "partial_exit_below_threshold"))
        return result
    if not alpha_config.allow_partial_exits and exit_match.exit_kind != "full":
        mark_skip(result, entry.data_quality, "partial_exit_below_threshold", debug_payload(trade, copy_time, None, entry.lookup, "partial_exit_below_threshold"))
        return result

    exit_delay = alpha_config.exit_delay_seconds if alpha_config.exit_delay_seconds is not None else result.delay_seconds
    if copy_time >= exit_match.whale_exit_time + timedelta(seconds=exit_delay):
        mark_skip(result, entry.data_quality, "exit_before_copy_entry", debug_payload(trade, copy_time, None, entry.lookup, "exit_before_copy_entry"))
        return result

    category = market.category if market else None
    entry_price = float(entry.price or 0.0)
    copied_shares = alpha_config.position_size_usd / entry_price if entry_price > 0 else 0.0
    fee_rate, fee_warning = fee_rate_for_category(config, category)
    entry_fee = copied_shares * fee_rate * entry_price * (1 - entry_price)
    remaining_fraction = 1.0
    gross_pnl = 0.0
    exit_fees = 0.0
    weighted_exit_price = 0.0
    realized_fraction = 0.0
    worst_quality = entry.data_quality
    exit_segments: list[dict[str, Any]] = []
    first_exit_lookup: PriceLookup | None = None
    last_exit_estimate: ExitEstimate | None = None

    for exit_trade in exit_match.exit_trades:
        whale_exit_time = to_utc(exit_trade.timestamp)
        copy_exit_time = whale_exit_time + timedelta(seconds=exit_delay)
        if copy_exit_time <= copy_time:
            mark_skip(result, entry.data_quality, "exit_before_copy_entry", debug_payload(trade, copy_time, None, entry.lookup, "exit_before_copy_entry"))
            return result
        lookup = nearest_price_history(session, clob, cache, config, trade.token_id or "", copy_exit_time, context="exit", require_history_extends=True)
        if lookup.price is None:
            reason = lookup.reason or "exit_price_missing"
            mark_skip(
                result,
                min_quality(entry.data_quality, "insufficient_data"),
                "exit_price_too_far" if reason == "exit_price_too_far" else "exit_price_missing",
                debug_payload(trade, copy_time, ExitEstimate(None, "insufficient_data", copy_exit_time, reason, lookup=lookup), entry.lookup, reason),
            )
            return result
        first_exit_lookup = first_exit_lookup or lookup
        segment_fraction = min(remaining_fraction, float(exit_trade.size or 0.0) / float(trade.size))
        if segment_fraction <= 0:
            continue
        segment_shares = copied_shares * segment_fraction
        gross_pnl += signed_gross_pnl("BUY", segment_shares, entry_price, lookup.price)
        exit_fees += segment_shares * fee_rate * lookup.price * (1 - lookup.price)
        weighted_exit_price += lookup.price * segment_fraction
        realized_fraction += segment_fraction
        remaining_fraction -= segment_fraction
        worst_quality = min_quality(worst_quality, "price_history_proxy")
        last_exit_estimate = ExitEstimate(
            lookup.price,
            "price_history_proxy",
            copy_exit_time,
            lookup=lookup,
            raw={"source": "follow_wallet_exit", "exit_trade_id": exit_trade.id, "exit_time": copy_exit_time.isoformat()},
        )
        exit_segments.append(
            {
                "exit_trade_id": exit_trade.id,
                "whale_exit_time": whale_exit_time.isoformat(),
                "copy_exit_time": copy_exit_time.isoformat(),
                "whale_exit_size": exit_trade.size,
                "segment_fraction": round_float(segment_fraction),
                "exit_price": round_float(lookup.price),
                "exit_price_timestamp": lookup.point.timestamp.isoformat() if lookup.point else None,
                "exit_price_distance_seconds": lookup.distance_seconds,
            }
        )
        if remaining_fraction <= 1e-9:
            break

    if realized_fraction < alpha_config.min_exit_fraction:
        mark_skip(result, entry.data_quality, "partial_exit_below_threshold", debug_payload(trade, copy_time, last_exit_estimate, entry.lookup, "partial_exit_below_threshold"))
        return result

    estimated_fee = entry_fee + exit_fees
    entry_slippage_cost = (entry.slippage or 0.0) * copied_shares * realized_fraction
    result.eventual_exit_price = round_float(weighted_exit_price / realized_fraction) if realized_fraction else None
    result.estimated_fee = round_float(estimated_fee)
    result.gross_pnl = round_float(gross_pnl)
    result.net_pnl = round_float(gross_pnl - estimated_fee - entry_slippage_cost)
    result.data_quality = worst_quality
    result.data_quality_rank = quality_rank(result.data_quality)
    result.skip_reason = None
    result.raw_json = json_dumps(
        {
            "entry": {
                **entry.raw,
                "entry_price_timestamp": entry.lookup.point.timestamp.isoformat() if entry.lookup and entry.lookup.point else None,
                "entry_price_distance_seconds": entry.lookup.distance_seconds if entry.lookup else None,
            },
            "exit": {
                "source": "follow_wallet_exit",
                "exit_kind": "full" if realized_fraction >= 1.0 - 1e-9 else "partial",
                "exit_fraction": round_float(realized_fraction),
                "entry_delay_seconds": result.delay_seconds,
                "exit_delay_seconds": exit_delay,
                "whale_entry_size": trade.size,
                "whale_exit_time": exit_match.whale_exit_time.isoformat(),
                "exit_time": last_exit_estimate.exit_time.isoformat() if last_exit_estimate and last_exit_estimate.exit_time else None,
                "exit_price_timestamp": first_exit_lookup.point.timestamp.isoformat() if first_exit_lookup and first_exit_lookup.point else None,
                "exit_price_distance_seconds": first_exit_lookup.distance_seconds if first_exit_lookup else None,
                "segments": exit_segments,
            },
            "debug": debug_payload(trade, copy_time, last_exit_estimate, entry.lookup, None),
            "category": category,
            "fee_rate": fee_rate,
            "fee_warning": fee_warning,
            "position_size_usd": alpha_config.position_size_usd,
            "allowed_data_quality": alpha_config.allowed_data_quality,
            "historical_mode": alpha_config.historical_mode,
        }
    )
    return result


def find_wallet_exit(session: Session, trade: Trade, alpha_config: AlphaDecayConfig) -> WalletExitMatch | None:
    if not trade.timestamp or not trade.size or trade.size <= 0:
        return None
    trade_time = to_utc(trade.timestamp)
    stmt = (
        select(Trade)
        .where(Trade.wallet_address == trade.wallet_address)
        .where(Trade.token_id == trade.token_id)
        .where(Trade.timestamp > trade_time)
        .order_by(Trade.timestamp.asc())
    )
    if trade.market_id:
        stmt = stmt.where(Trade.market_id == trade.market_id)
    if alpha_config.max_holding_hours is not None:
        stmt = stmt.where(Trade.timestamp <= trade_time + timedelta(hours=float(alpha_config.max_holding_hours)))
    exits: list[Trade] = []
    cumulative = 0.0
    for candidate in session.scalars(stmt):
        side = (candidate.side or "").upper()
        if side == "BUY":
            continue
        if side != "SELL":
            continue
        if not candidate.size or candidate.size <= 0 or not candidate.timestamp:
            continue
        exits.append(candidate)
        cumulative += float(candidate.size)
        if cumulative >= float(trade.size):
            break
    if not exits:
        return None
    fraction = min(cumulative / float(trade.size), 1.0)
    return WalletExitMatch(
        exit_trades=exits,
        exit_fraction=fraction,
        exit_kind="full" if fraction >= 1.0 - 1e-9 else "partial",
        whale_exit_time=to_utc(exits[-1].timestamp),
    )


def preflight_skip_reason(trade: Trade, side: str) -> str | None:
    if not trade.token_id:
        return "missing_token_id"
    if not trade.timestamp:
        return "missing_trade_time"
    if trade.price is None:
        return "missing_whale_price"
    if side not in {"BUY", "SELL"}:
        return "invalid_side"
    if trade.price < 0 or trade.price > 1:
        return "invalid_price"
    return None


def estimate_entry(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    token_id: str,
    side: str,
    copy_time: datetime,
    position_size_usd: float,
    historical_mode: str,
) -> EntryEstimate:
    alpha_cfg = config.get("alpha_decay", {})
    if historical_mode != "price_history_only":
        exact_max_age = int(alpha_cfg.get("exact_orderbook_max_age_seconds", 30))
        snapshot = nearest_orderbook(session, token_id, copy_time, exact_max_age)
        if snapshot:
            book = {"bids": json_loads_safe(snapshot.bids_json), "asks": json_loads_safe(snapshot.asks_json)}
            fill = simulate_orderbook_fill(side, position_size_usd, book)
            if fill.fill_possible and fill.average_price is not None:
                return EntryEstimate(
                    price=fill.average_price,
                    data_quality="exact_orderbook",
                    best_bid=snapshot.best_bid,
                    best_ask=snapshot.best_ask,
                    spread=snapshot.spread,
                    liquidity_available=fill.available_liquidity,
                    slippage=fill.slippage,
                    raw={"source": "stored_orderbook_snapshot", "snapshot_id": snapshot.id},
                )

    lookup = nearest_price_history(session, clob, cache, config, token_id, copy_time, context="copy")
    if lookup.price is not None:
        proxy_spread = float(alpha_cfg.get("proxy_spread_assumption", 0.01))
        slippage = proxy_slippage(lookup.price, float(alpha_cfg.get("proxy_slippage_bps", 50)))
        return EntryEstimate(
            price=adjust_proxy_entry(side, lookup.price, slippage),
            data_quality="price_history_proxy",
            spread=proxy_spread,
            liquidity_available=position_size_usd,
            slippage=slippage,
            lookup=lookup,
            raw={"source": "clob_price_history", "base_price": lookup.price, "distance_seconds": lookup.distance_seconds},
        )

    if historical_mode == "price_history_only":
        return EntryEstimate(None, "insufficient_data", skip_reason=lookup.reason or "missing_entry_price", lookup=lookup)

    live_snapshot = fetch_and_store_live_orderbook(session, clob, token_id)
    exact_max_age = int(alpha_cfg.get("exact_orderbook_max_age_seconds", 30))
    if live_snapshot and abs((to_utc(live_snapshot.timestamp) - copy_time).total_seconds()) <= exact_max_age:
        book = {"bids": json_loads_safe(live_snapshot.bids_json), "asks": json_loads_safe(live_snapshot.asks_json)}
        fill = simulate_orderbook_fill(side, position_size_usd, book)
        if fill.fill_possible and fill.average_price is not None:
            return EntryEstimate(
                price=fill.average_price,
                data_quality="exact_orderbook",
                best_bid=live_snapshot.best_bid,
                best_ask=live_snapshot.best_ask,
                spread=live_snapshot.spread,
                liquidity_available=fill.available_liquidity,
                slippage=fill.slippage,
                raw={"source": "live_orderbook_snapshot", "snapshot_id": live_snapshot.id},
            )

    midpoint = clob.get_midpoint(token_id)
    mid_value = first_float(midpoint or {}, ("mid", "midpoint"))
    spread_payload = clob.get_spread(token_id)
    spread = first_float(spread_payload or {}, ("spread",))
    if mid_value is not None:
        slippage = proxy_slippage(mid_value, float(alpha_cfg.get("proxy_slippage_bps", 50)))
        return EntryEstimate(
            price=adjust_proxy_entry(side, mid_value, slippage),
            data_quality="midpoint_proxy",
            spread=spread if spread is not None else float(alpha_cfg.get("proxy_spread_assumption", 0.01)),
            liquidity_available=position_size_usd,
            slippage=slippage,
            raw={"source": "clob_midpoint", "payload": midpoint, "spread_payload": spread_payload},
        )

    last = clob.get_last_trade_price(token_id)
    last_value = first_float(last or {}, ("price",))
    if last_value is not None:
        slippage = proxy_slippage(last_value, float(alpha_cfg.get("proxy_slippage_bps", 50)))
        return EntryEstimate(
            price=adjust_proxy_entry(side, last_value, slippage),
            data_quality="last_price_proxy",
            spread=float(alpha_cfg.get("proxy_spread_assumption", 0.01)),
            liquidity_available=position_size_usd,
            slippage=slippage,
            raw={"source": "clob_last_trade_price", "payload": last},
        )
    return EntryEstimate(None, "insufficient_data", skip_reason=lookup.reason or "missing_entry_price", lookup=lookup)


def estimate_exit(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    token_id: str,
    market: Market | None,
    copy_time: datetime,
    exit_rule: str,
) -> ExitEstimate:
    if exit_rule == "fixed_24h":
        target = copy_time + timedelta(hours=24)
        if target > datetime.now(timezone.utc):
            load = price_history_load(session, clob, cache, token_id)
            return ExitEstimate(None, "insufficient_data", target, "exit_time_in_future", lookup=empty_lookup(token_id, load, "exit_time_in_future"))
        lookup = nearest_price_history(session, clob, cache, config, token_id, target, context="exit", require_history_extends=True)
        if lookup.price is None:
            return ExitEstimate(None, "insufficient_data", target, lookup.reason or "missing_exit_price", lookup=lookup)
        return ExitEstimate(lookup.price, "price_history_proxy", target, lookup=lookup, raw={"source": "fixed_24h_price_history", "target": target.isoformat()})

    if exit_rule == "latest_available":
        load = price_history_load(session, clob, cache, token_id)
        if not load.points:
            reason = "price_history_parse_failed" if load.parse_failed else "no_price_history"
            return ExitEstimate(None, "insufficient_data", None, reason, lookup=empty_lookup(token_id, load, reason))
        after_copy = [point for point in load.points if point.timestamp >= copy_time]
        if after_copy:
            point = max(after_copy, key=lambda item: item.timestamp)
            distance = abs((point.timestamp - copy_time).total_seconds())
            lookup = PriceLookup(point.price, point, distance, None, load)
            return ExitEstimate(point.price, "price_history_proxy", point.timestamp, lookup=lookup, raw={"source": "latest_available_after_copy"})
        nearest = min(load.points, key=lambda point: abs((point.timestamp - copy_time).total_seconds()))
        lookup = PriceLookup(nearest.price, nearest, abs((nearest.timestamp - copy_time).total_seconds()), None, load)
        return ExitEstimate(nearest.price, "price_history_proxy", nearest.timestamp, lookup=lookup, raw={"source": "latest_available_nearest_fallback"})

    if exit_rule == "hold_to_resolution":
        if not market or not market.closed:
            load = price_history_load(session, clob, cache, token_id)
            return ExitEstimate(None, "insufficient_data", None, "missing_exit_price", lookup=empty_lookup(token_id, load, "missing_exit_price"))
        load = price_history_load(session, clob, cache, token_id)
        after_copy = [point for point in load.points if point.timestamp >= copy_time]
        if not after_copy:
            return ExitEstimate(None, "insufficient_data", None, "no_exit_price", lookup=empty_lookup(token_id, load, "no_exit_price"))
        point = max(after_copy, key=lambda item: item.timestamp)
        lookup = PriceLookup(point.price, point, abs((point.timestamp - copy_time).total_seconds()), None, load)
        return ExitEstimate(point.price, "price_history_proxy", point.timestamp, lookup=lookup, raw={"source": "closed_market_latest_price_history"})

    load = price_history_load(session, clob, cache, token_id)
    return ExitEstimate(None, "insufficient_data", None, "missing_exit_price", lookup=empty_lookup(token_id, load, "missing_exit_price"))


def nearest_orderbook(session: Session, token_id: str, target: datetime, max_age_seconds: int) -> OrderbookSnapshot | None:
    rows = list(session.scalars(select(OrderbookSnapshot).where(OrderbookSnapshot.token_id == token_id)))
    if not rows:
        return None
    nearest = min(rows, key=lambda row: abs((to_utc(row.timestamp) - target).total_seconds()))
    if abs((to_utc(nearest.timestamp) - target).total_seconds()) <= max_age_seconds:
        return nearest
    return None


def fetch_and_store_live_orderbook(session: Session, clob: ClobClient, token_id: str) -> OrderbookSnapshot | None:
    payload = clob.get_orderbook(token_id)
    if not payload:
        return None
    timestamp = normalize_timestamp(payload.get("timestamp")) or datetime.now(timezone.utc)
    best_bid, best_ask, spread, midpoint = best_bid_ask(payload)
    snapshot_id = stable_id("orderbook", token_id, timestamp.isoformat(), payload.get("hash"))
    snapshot = session.get(OrderbookSnapshot, snapshot_id)
    if snapshot is None:
        snapshot = OrderbookSnapshot(id=snapshot_id, token_id=token_id, timestamp=timestamp)
        session.add(snapshot)
    snapshot.best_bid = best_bid
    snapshot.best_ask = best_ask
    snapshot.spread = spread
    snapshot.midpoint = midpoint
    snapshot.bids_json = json_dumps(payload.get("bids") or [])
    snapshot.asks_json = json_dumps(payload.get("asks") or [])
    snapshot.min_order_size = first_float(payload, ("min_order_size", "minOrderSize"))
    snapshot.tick_size = first_float(payload, ("tick_size", "tickSize"))
    snapshot.neg_risk = bool_or_none(payload.get("neg_risk") or payload.get("negRisk"))
    snapshot.raw_json = json_dumps(payload)
    session.flush()
    return snapshot


def price_history_load(session: Session, clob: ClobClient, cache: dict[str, Any], token_id: str) -> PriceHistoryLoad:
    cached = cache.setdefault("price_history_load", {})
    if token_id in cached:
        return cached[token_id]

    existing = list(session.scalars(select(PriceHistory).where(PriceHistory.token_id == token_id).order_by(PriceHistory.timestamp)))
    endpoint_url = clob.price_history_url(token_id)
    if existing:
        load = PriceHistoryLoad(
            token_id=token_id,
            endpoint_url=endpoint_url,
            raw_payload={"source": "sqlite_price_history"},
            points=[PricePoint(to_utc(row.timestamp), row.price, row.raw_json) for row in existing],
            parse_failed=False,
        )
        cached[token_id] = load
        return load

    payload = clob.get_price_history_payload(token_id, interval="max", fidelity=1)
    rows = extract_price_history_rows(payload)
    points = normalize_price_history_rows(rows)
    parse_failed = bool(rows) and not points
    for point in points:
        history_id = stable_id("price_history", token_id, int(point.timestamp.timestamp()), point.price)
        history = session.get(PriceHistory, history_id)
        if history is None:
            history = PriceHistory(id=history_id, token_id=token_id, timestamp=point.timestamp, price=point.price)
            session.add(history)
        history.source = "clob:/prices-history"
        history.fidelity = "1"
        history.raw_json = json_dumps(point.raw)
    session.flush()
    load = PriceHistoryLoad(token_id=token_id, endpoint_url=endpoint_url, raw_payload=payload, points=points, parse_failed=parse_failed)
    cached[token_id] = load
    return load


def normalize_price_history_payload(payload: Any) -> list[PricePoint]:
    return normalize_price_history_rows(extract_price_history_rows(payload))


def normalize_price_history_rows(rows: list[Any]) -> list[PricePoint]:
    points: list[PricePoint] = []
    for row in rows:
        timestamp_value, price_value = extract_price_history_values(row)
        timestamp = normalize_timestamp(timestamp_value)
        price = parse_float(price_value)
        if timestamp is None or price is None:
            continue
        points.append(PricePoint(timestamp=timestamp, price=price, raw=row))
    return sorted(points, key=lambda point: point.timestamp)


def extract_price_history_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("history", "prices", "data", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def extract_price_history_values(row: Any) -> tuple[Any, Any]:
    if isinstance(row, dict):
        timestamp = first_present(row, ("t", "timestamp", "time", "ts", "date", "datetime"))
        price = first_present(row, ("p", "price", "value", "mid", "midpoint"))
        return timestamp, price
    if isinstance(row, (list, tuple)) and len(row) >= 2:
        return row[0], row[1]
    return None, None


def nearest_price_history(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    token_id: str,
    target: datetime,
    *,
    context: str,
    require_history_extends: bool = False,
) -> PriceLookup:
    load = price_history_load(session, clob, cache, token_id)
    if not load.points:
        reason = "price_history_parse_failed" if load.parse_failed else "no_price_history"
        return empty_lookup(token_id, load, reason)

    target = to_utc(target)
    if require_history_extends and load.last_timestamp and load.last_timestamp < target:
        return PriceLookup(None, None, None, "no_exit_price", load)

    nearest = min(load.points, key=lambda point: abs((point.timestamp - target).total_seconds()))
    distance = abs((nearest.timestamp - target).total_seconds())
    max_distance = int(config.get("alpha_decay", {}).get("max_price_history_distance_seconds", config.get("alpha_decay", {}).get("price_history_max_distance_seconds", 86400)))
    if distance > max_distance:
        if context == "copy":
            if load.first_timestamp and target < load.first_timestamp:
                reason = "copy_time_before_history"
            elif load.last_timestamp and target > load.last_timestamp:
                reason = "copy_time_after_history"
            else:
                reason = "copy_price_too_far"
        else:
            reason = "exit_price_too_far"
        return PriceLookup(None, nearest, distance, reason, load)
    return PriceLookup(nearest.price, nearest, distance, None, load)


def empty_lookup(token_id: str, load: PriceHistoryLoad | None, reason: str) -> PriceLookup:
    if load is None:
        load = PriceHistoryLoad(token_id=token_id, endpoint_url="", raw_payload=None, points=[])
    return PriceLookup(None, None, None, reason, load)


def inspect_price_history(config: dict[str, Any], token_id: str) -> dict[str, Any]:
    init_db(config)
    with session_scope(database_url(config)) as session:
        clob = ClobClient(config, session=session)
        payload = clob.get_price_history_payload(token_id, interval="max", fidelity=1)
        points = normalize_price_history_payload(payload)
        return {
            "endpoint_url": clob.price_history_url(token_id),
            "point_count": len(points),
            "first_timestamp": points[0].timestamp.isoformat() if points else None,
            "last_timestamp": points[-1].timestamp.isoformat() if points else None,
            "first_points": [{"timestamp": point.timestamp.isoformat(), "price": point.price} for point in points[:5]],
            "last_points": [{"timestamp": point.timestamp.isoformat(), "price": point.price} for point in points[-5:]],
            "raw_sample": raw_sample(payload),
        }


def upsert_alpha_result(session: Session, result: AlphaDecayResult) -> None:
    session.merge(result)


def delete_alpha_results_for_wallets(config: dict[str, Any], wallet_address: str | None = None) -> int:
    init_db(config)
    with session_scope(database_url(config)) as session:
        stmt = delete(AlphaDecayResult)
        if wallet_address:
            stmt = stmt.where(AlphaDecayResult.wallet_address == wallet_address.lower())
        result = session.execute(stmt)
        return int(result.rowcount or 0)


def base_result(trade: Trade, delay_seconds: int, copy_time: datetime | None, exit_rule: str) -> AlphaDecayResult:
    result_id = stable_id("alpha", trade.id, delay_seconds, exit_rule)
    return AlphaDecayResult(
        id=result_id,
        wallet_address=trade.wallet_address,
        trade_id=trade.id,
        token_id=trade.token_id,
        market_id=trade.market_id,
        trade_time=to_utc(trade.timestamp) if trade.timestamp else None,
        original_side=trade.side,
        whale_price=trade.price,
        whale_size=trade.size,
        delay_seconds=delay_seconds,
        copy_time=copy_time,
        exit_rule=exit_rule,
        data_quality="insufficient_data",
        data_quality_rank=0,
        raw_json="{}",
    )


def apply_entry(result: AlphaDecayResult, entry: EntryEstimate) -> None:
    result.copy_best_bid = round_float(entry.best_bid)
    result.copy_best_ask = round_float(entry.best_ask)
    result.copy_spread = round_float(entry.spread)
    result.simulated_entry_price = round_float(entry.price)
    result.liquidity_available = round_float(entry.liquidity_available)
    result.estimated_slippage = round_float(entry.slippage)
    result.data_quality = entry.data_quality
    result.data_quality_rank = quality_rank(entry.data_quality)


def mark_skip(result: AlphaDecayResult, data_quality: str, reason: str, debug: dict[str, Any] | None = None) -> None:
    result.data_quality = data_quality
    result.data_quality_rank = quality_rank(data_quality)
    result.skip_reason = reason
    result.raw_json = json_dumps({"skip_reason": reason, "debug": debug or {}})


def debug_payload(
    trade: Trade,
    copy_time: datetime | None,
    exit_estimate: ExitEstimate | None,
    copy_lookup: PriceLookup | None,
    reason: str | None,
) -> dict[str, Any]:
    exit_lookup = exit_estimate.lookup if exit_estimate else None
    load = (copy_lookup or exit_lookup).load if (copy_lookup or exit_lookup) else None
    return {
        "wallet_address": trade.wallet_address,
        "trade_id": trade.id,
        "token_id": trade.token_id,
        "market_id": trade.market_id,
        "side": trade.side,
        "whale_price": trade.price,
        "trade_time_raw": str(trade.timestamp),
        "trade_time_parsed_utc": to_utc(trade.timestamp).isoformat() if trade.timestamp else None,
        "copy_time": copy_time.isoformat() if copy_time else None,
        "exit_time": exit_estimate.exit_time.isoformat() if exit_estimate and exit_estimate.exit_time else None,
        "prices_history_returned_points": bool(load and load.points),
        "price_history_point_count": len(load.points) if load else 0,
        "first_price_timestamp": load.first_timestamp.isoformat() if load and load.first_timestamp else None,
        "last_price_timestamp": load.last_timestamp.isoformat() if load and load.last_timestamp else None,
        "nearest_copy_timestamp": copy_lookup.point.timestamp.isoformat() if copy_lookup and copy_lookup.point else None,
        "nearest_exit_timestamp": exit_lookup.point.timestamp.isoformat() if exit_lookup and exit_lookup.point else None,
        "copy_distance_seconds": copy_lookup.distance_seconds if copy_lookup else None,
        "exit_distance_seconds": exit_lookup.distance_seconds if exit_lookup else None,
        "parsed_copy_price": copy_lookup.price if copy_lookup else None,
        "parsed_exit_price": exit_lookup.price if exit_lookup else None,
        "insufficient_reason": reason,
        "endpoint_url": load.endpoint_url if load else None,
    }


def debug_from_result(result: AlphaDecayResult) -> dict[str, Any]:
    import json

    payload = {}
    try:
        payload = json.loads(result.raw_json or "{}")
    except json.JSONDecodeError:
        pass
    debug = payload.get("debug") if isinstance(payload, dict) else {}
    return debug or {
        "wallet_address": result.wallet_address,
        "trade_id": result.trade_id,
        "token_id": result.token_id,
        "market_id": result.market_id,
        "side": result.original_side,
        "whale_price": result.whale_price,
        "trade_time_parsed_utc": result.trade_time.isoformat() if result.trade_time else None,
        "copy_time": result.copy_time.isoformat() if result.copy_time else None,
        "insufficient_reason": result.skip_reason,
    }


def debug_for_unusable_trade(trade: Trade) -> dict[str, Any]:
    return debug_payload(trade, None, None, None, preflight_skip_reason(trade, (trade.side or "").upper()))


def entry_degradation(side: str, whale_price: float, simulated_entry_price: float) -> float:
    if side.upper() == "SELL":
        return whale_price - simulated_entry_price
    return simulated_entry_price - whale_price


def signed_gross_pnl(side: str, shares: float, entry_price: float, exit_price: float) -> float:
    if side.upper() == "SELL":
        return shares * (entry_price - exit_price)
    return shares * (exit_price - entry_price)


def adjust_proxy_entry(side: str, price: float, slippage: float) -> float:
    if side.upper() == "SELL":
        return max(0.0, price - slippage)
    return min(1.0, price + slippage)


def min_quality(left: str, right: str) -> str:
    return left if quality_rank(left) <= quality_rank(right) else right


def normalize_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        numeric = parse_float(cleaned)
        if numeric is not None:
            return normalize_timestamp(numeric)
        try:
            return to_utc(datetime.fromisoformat(cleaned.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = row.get(key)
        parsed = parse_float(value)
        if parsed is not None:
            return parsed
    return None


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return bool(value)


def json_loads_safe(value: str | None) -> list[dict[str, Any]]:
    import json

    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def raw_sample(payload: Any) -> Any:
    if isinstance(payload, dict):
        sample = {}
        for key, value in payload.items():
            if isinstance(value, list):
                sample[key] = value[:2]
            else:
                sample[key] = value
        return sample
    if isinstance(payload, list):
        return payload[:2]
    return payload


def stable_id(*parts: Any) -> str:
    digest = hashlib.sha256(json_dumps(parts).encode("utf-8")).hexdigest()
    return f"{parts[0]}:{digest}"


def round_float(value: Any, digits: int = 8) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pmcopy.api.clob import ClobClient
from pmcopy.config import database_url
from pmcopy.db import (
    LifecycleCopyEvent,
    LifecycleCopyPosition,
    LifecycleCopyRun,
    Market,
    ReconstructedPosition,
    ReconstructedPositionEvent,
    init_db,
    json_dumps,
    json_loads,
    session_scope,
    utc_now,
)
from pmcopy.features.alpha_decay import PriceLookup, nearest_price_history
from pmcopy.features.data_quality import quality_rank
from pmcopy.features.fees import fee_rate_for_category


EPSILON = 1e-9
ADD_EVENTS = {"open_position", "increase_position"}
EXIT_EVENTS = {"partial_exit", "reduce_position", "full_exit"}
INVALID_EVENTS = {"orphan_sell", "missing_prior_inventory"}


@dataclass
class LifecycleCopyConfig:
    copy_mode: str = "reconstructed_wallet_lifecycle"
    sizing_mode: str = "proportional_to_whale_with_cap"
    copy_ratio: float = 0.001
    max_position_budget_usd: float = 10.0
    min_trade_usd: float = 1.0
    execute_small_trades: bool = False
    allow_position_cap_partial_fill: bool = True
    entry_delay_seconds: int = 60
    exit_delay_seconds: int = 60
    allowed_data_quality: list[str] = field(default_factory=lambda: ["price_history_proxy"])
    historical_mode: str = "price_history_only"
    date_start: datetime | None = None
    date_end: datetime | None = None


@dataclass
class CopyLot:
    shares: float
    remaining_shares: float
    entry_price: float
    remaining_cost_usd: float


@dataclass
class CopyState:
    lots: list[CopyLot] = field(default_factory=list)
    copied_shares: float = 0.0
    copied_exposure_usd: float = 0.0
    total_buy_usd: float = 0.0
    total_sell_usd: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    worst_quality: str | None = None
    cap_hit_count: int = 0
    below_min_trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    skip_reasons: Counter = field(default_factory=Counter)


@dataclass
class PriceExecution:
    price: float | None
    timestamp: datetime | None
    distance_seconds: float | None
    data_quality: str
    skip_reason: str | None
    lookup: PriceLookup | None


def lifecycle_config_from_values(
    config: dict[str, Any],
    *,
    copy_mode: str | None = None,
    sizing_mode: str | None = None,
    copy_ratio: float | None = None,
    max_position_budget_usd: float | None = None,
    min_trade_usd: float | None = None,
    execute_small_trades: bool | None = None,
    allow_position_cap_partial_fill: bool | None = None,
    entry_delay_seconds: int | None = None,
    exit_delay_seconds: int | None = None,
    allowed_data_quality: list[str] | None = None,
    historical_mode: str | None = None,
    date_start: datetime | None = None,
    date_end: datetime | None = None,
) -> LifecycleCopyConfig:
    sizing = config.get("sizing", {})
    alpha = config.get("alpha_decay", {})
    backtest = config.get("backtest", {})
    entry_delay = entry_delay_seconds if entry_delay_seconds is not None else backtest.get("entry_delay_seconds", backtest.get("copy_delay_seconds", 60))
    exit_delay = exit_delay_seconds if exit_delay_seconds is not None else backtest.get("exit_delay_seconds", entry_delay)
    return LifecycleCopyConfig(
        copy_mode=copy_mode or str(backtest.get("copy_mode", "diagnostic_trade_level")),
        sizing_mode=sizing_mode or str(sizing.get("default_lifecycle_sizing_mode", "proportional_to_whale_with_cap")),
        copy_ratio=float(copy_ratio if copy_ratio is not None else sizing.get("copy_ratio", 0.001)),
        max_position_budget_usd=float(
            max_position_budget_usd if max_position_budget_usd is not None else sizing.get("max_position_budget_usd", 10)
        ),
        min_trade_usd=float(min_trade_usd if min_trade_usd is not None else sizing.get("min_trade_usd", 1)),
        execute_small_trades=bool(
            execute_small_trades if execute_small_trades is not None else sizing.get("execute_small_trades", False)
        ),
        allow_position_cap_partial_fill=bool(
            allow_position_cap_partial_fill
            if allow_position_cap_partial_fill is not None
            else sizing.get("allow_position_cap_partial_fill", True)
        ),
        entry_delay_seconds=int(entry_delay),
        exit_delay_seconds=int(exit_delay),
        allowed_data_quality=allowed_data_quality
        or list(backtest.get("allowed_data_quality_levels", alpha.get("allowed_data_quality_levels", ["price_history_proxy"]))),
        historical_mode=historical_mode or str(alpha.get("historical_mode", "price_history_only")),
        date_start=date_start,
        date_end=date_end,
    )


def run_lifecycle_copy(
    config: dict[str, Any],
    *,
    run_id: str | None = None,
    wallet_address: str | None = None,
    selected_wallets: list[str] | None = None,
    lifecycle_config: LifecycleCopyConfig | None = None,
) -> dict[str, Any]:
    init_db(config)
    lifecycle_config = lifecycle_config or lifecycle_config_from_values(config)
    run_id = run_id or stable_id("lifecycle_copy", utc_now().isoformat())
    wallets = normalize_wallets(selected_wallets or ([wallet_address] if wallet_address else None))
    with session_scope(database_url(config)) as session:
        return run_lifecycle_copy_in_session(session, config, run_id, lifecycle_config, wallets)


def run_lifecycle_copy_in_session(
    session: Session,
    config: dict[str, Any],
    run_id: str,
    lifecycle_config: LifecycleCopyConfig,
    selected_wallets: list[str] | None = None,
    *,
    period_label: str = "in_sample",
    reset_run: bool = False,
) -> dict[str, Any]:
    if reset_run:
        clear_lifecycle_run(session, run_id)
    ensure_lifecycle_run(session, run_id, lifecycle_config, selected_wallets)
    wallets = selected_wallets or lifecycle_wallets(session)
    clob = ClobClient(config, session=session)
    cache: dict[str, Any] = {}
    positions = lifecycle_positions(session, wallets)
    categories = market_category_map(session)
    copied_positions: list[dict[str, Any]] = []
    copied_events: list[dict[str, Any]] = []
    skipped = Counter()
    data_qualities: list[str] = []
    cap_hit_count = 0
    below_min_trade_count = 0

    for position in positions:
        position_result = simulate_position_lifecycle(
            session,
            config,
            clob,
            cache,
            run_id,
            period_label,
            position,
            lifecycle_config,
            categories.get(position.market_id or "", "unknown"),
        )
        copied_positions.append(position_result["position"])
        copied_events.extend(position_result["events"])
        skipped.update(position_result["skipped"])
        data_qualities.extend(position_result["data_qualities"])
        cap_hit_count += int(position_result["cap_hit_count"])
        below_min_trade_count += int(position_result["below_min_trade_count"])

    closed = [row for row in copied_positions if row.get("status") == "closed" and row.get("skip_reason") is None]
    open_positions = [row for row in copied_positions if row.get("status") == "open"]
    skipped_positions = [row for row in copied_positions if row.get("skip_reason") or row.get("status") in {"skipped", "invalid"}]
    result = {
        "run_id": run_id,
        "period_label": period_label,
        "selected_wallets": wallets,
        "candidate_count": len(copied_positions),
        "positions": copied_positions,
        "events": copied_events,
        "closed_positions": len(closed),
        "open_positions": len(open_positions),
        "skipped_positions": len(skipped_positions),
        "skipped_signal_reasons": dict(sorted(skipped.items())),
        "data_quality_summary": data_quality_summary(data_qualities),
        "cap_hit_count": cap_hit_count,
        "below_min_trade_count": below_min_trade_count,
        "total_pnl": round_float(sum(float(row.get("net_pnl") or 0.0) for row in closed)),
    }
    run_row = session.get(LifecycleCopyRun, run_id)
    if run_row:
        run_row.result_json = json_dumps(result_summary(result))
    return result


def simulate_position_lifecycle(
    session: Session,
    config: dict[str, Any],
    clob: ClobClient,
    cache: dict[str, Any],
    run_id: str,
    period_label: str,
    position: ReconstructedPosition,
    lifecycle_config: LifecycleCopyConfig,
    category: str,
) -> dict[str, Any]:
    events = reconstructed_events(session, position)
    state = CopyState()
    copied_events: list[dict[str, Any]] = []
    data_qualities: list[str] = []
    skipped = Counter()
    fee_rate, fee_warning = fee_rate_for_category(config, category)

    for event in events:
        if event.event_type in INVALID_EVENTS:
            row = store_lifecycle_event(
                session,
                run_id,
                period_label,
                position,
                event,
                copied_action="skip",
                target_time=None,
                execution=None,
                whale_trade_usd=event_usd(event),
                desired_copy_usd=None,
                actual_copy_usd=None,
                copied_position_before=state.copied_exposure_usd,
                copied_position_after=state.copied_exposure_usd,
                data_quality="insufficient_data",
                skip_reason=event.event_type,
                raw={"source": "position_reconstruction"},
            )
            copied_events.append(event_to_dict(row))
            state.skip_reasons[event.event_type] += 1
            skipped[event.event_type] += 1
            continue

        if event.event_type in ADD_EVENTS:
            if not event_is_copy_entry_signal(event, lifecycle_config):
                continue
            row = handle_add_event(
                session,
                config,
                clob,
                cache,
                run_id,
                period_label,
                position,
                event,
                lifecycle_config,
                state,
                fee_rate,
            )
        elif event.event_type in EXIT_EVENTS:
            if state.copied_shares <= EPSILON and not event_is_inside_period(event, lifecycle_config):
                continue
            row = handle_exit_event(
                session,
                config,
                clob,
                cache,
                run_id,
                period_label,
                position,
                event,
                lifecycle_config,
                state,
                fee_rate,
            )
        else:
            continue

        copied_events.append(event_to_dict(row))
        if row.skip_reason:
            skipped[row.skip_reason] += 1
            state.skip_reasons[row.skip_reason] += 1
        if row.data_quality and row.data_quality != "insufficient_data":
            data_qualities.append(row.data_quality)

    position_row = store_lifecycle_position(session, run_id, period_label, position, state, category, fee_warning)
    return {
        "position": position_to_dict(position_row, category),
        "events": copied_events,
        "skipped": skipped,
        "data_qualities": data_qualities,
        "cap_hit_count": state.cap_hit_count,
        "below_min_trade_count": state.below_min_trade_count,
    }


def handle_add_event(
    session: Session,
    config: dict[str, Any],
    clob: ClobClient,
    cache: dict[str, Any],
    run_id: str,
    period_label: str,
    position: ReconstructedPosition,
    event: ReconstructedPositionEvent,
    lifecycle_config: LifecycleCopyConfig,
    state: CopyState,
    fee_rate: float,
) -> LifecycleCopyEvent:
    whale_usd = event_usd(event) or 0.0
    desired_copy_usd = whale_usd * lifecycle_config.copy_ratio
    remaining_cap = max(0.0, lifecycle_config.max_position_budget_usd - state.copied_exposure_usd)
    actual_copy_usd = min(desired_copy_usd, remaining_cap)
    skip_reason = None
    if remaining_cap <= EPSILON:
        actual_copy_usd = 0.0
        skip_reason = "position_cap_reached"
        state.cap_hit_count += 1
    elif desired_copy_usd > remaining_cap + EPSILON:
        state.cap_hit_count += 1
        if not lifecycle_config.allow_position_cap_partial_fill:
            actual_copy_usd = 0.0
            skip_reason = "position_cap_reached"
    if skip_reason is None and actual_copy_usd < lifecycle_config.min_trade_usd and not lifecycle_config.execute_small_trades:
        skip_reason = "below_min_trade_usd"
        state.below_min_trade_count += 1

    target_time = to_utc(event.timestamp) + timedelta(seconds=lifecycle_config.entry_delay_seconds) if event.timestamp else None
    execution: PriceExecution | None = None
    before = state.copied_exposure_usd
    if skip_reason is None:
        execution = lookup_execution_price(session, clob, cache, config, position.token_id, target_time, "entry", lifecycle_config)
        skip_reason = execution.skip_reason
    if skip_reason is not None:
        return store_lifecycle_event(
            session,
            run_id,
            period_label,
            position,
            event,
            copied_action="skip",
            target_time=target_time,
            execution=execution,
            whale_trade_usd=whale_usd,
            desired_copy_usd=desired_copy_usd,
            actual_copy_usd=0.0,
            copied_position_before=before,
            copied_position_after=before,
            data_quality=execution.data_quality if execution else "insufficient_data",
            skip_reason=skip_reason,
            raw={"remaining_cap": remaining_cap, "copy_ratio": lifecycle_config.copy_ratio},
        )

    assert execution is not None and execution.price is not None
    copied_shares = actual_copy_usd / execution.price if execution.price > 0 else 0.0
    state.lots.append(CopyLot(copied_shares, copied_shares, execution.price, actual_copy_usd))
    state.copied_shares += copied_shares
    state.copied_exposure_usd += actual_copy_usd
    state.total_buy_usd += actual_copy_usd
    state.fees += actual_copy_usd * fee_rate * execution.price * (1 - execution.price) / execution.price if execution.price > 0 else 0.0
    state.opened_at = state.opened_at or target_time
    state.worst_quality = min_quality(state.worst_quality, execution.data_quality)
    state.buy_count += 1
    return store_lifecycle_event(
        session,
        run_id,
        period_label,
        position,
        event,
        copied_action="buy",
        target_time=target_time,
        execution=execution,
        whale_trade_usd=whale_usd,
        desired_copy_usd=desired_copy_usd,
        actual_copy_usd=actual_copy_usd,
        copied_position_before=before,
        copied_position_after=state.copied_exposure_usd,
        data_quality=execution.data_quality,
        skip_reason=None,
        raw={
            "remaining_cap_before": remaining_cap,
            "copied_shares": copied_shares,
            "copy_ratio": lifecycle_config.copy_ratio,
            "entry_delay_seconds": lifecycle_config.entry_delay_seconds,
        },
    )


def handle_exit_event(
    session: Session,
    config: dict[str, Any],
    clob: ClobClient,
    cache: dict[str, Any],
    run_id: str,
    period_label: str,
    position: ReconstructedPosition,
    event: ReconstructedPositionEvent,
    lifecycle_config: LifecycleCopyConfig,
    state: CopyState,
    fee_rate: float,
) -> LifecycleCopyEvent:
    before = state.copied_exposure_usd
    whale_fraction = whale_exit_fraction(event)
    target_time = to_utc(event.timestamp) + timedelta(seconds=lifecycle_config.exit_delay_seconds) if event.timestamp else None
    if whale_fraction is None:
        return store_lifecycle_event(
            session,
            run_id,
            period_label,
            position,
            event,
            copied_action="skip",
            target_time=target_time,
            execution=None,
            whale_trade_usd=event_usd(event),
            desired_copy_usd=None,
            actual_copy_usd=None,
            copied_position_before=before,
            copied_position_after=before,
            data_quality="insufficient_data",
            skip_reason="invalid_exit_fraction",
            raw={},
        )
    if state.copied_shares <= EPSILON:
        return store_lifecycle_event(
            session,
            run_id,
            period_label,
            position,
            event,
            copied_action="skip",
            target_time=target_time,
            execution=None,
            whale_trade_usd=event_usd(event),
            desired_copy_usd=None,
            actual_copy_usd=0.0,
            copied_position_before=before,
            copied_position_after=before,
            data_quality="insufficient_data",
            skip_reason="insufficient_copied_inventory",
            raw={"whale_exit_fraction": whale_fraction},
        )

    execution = lookup_execution_price(session, clob, cache, config, position.token_id, target_time, "exit", lifecycle_config)
    if execution.skip_reason:
        return store_lifecycle_event(
            session,
            run_id,
            period_label,
            position,
            event,
            copied_action="skip",
            target_time=target_time,
            execution=execution,
            whale_trade_usd=event_usd(event),
            desired_copy_usd=None,
            actual_copy_usd=0.0,
            copied_position_before=before,
            copied_position_after=before,
            data_quality=execution.data_quality,
            skip_reason=execution.skip_reason,
            raw={"whale_exit_fraction": whale_fraction},
        )

    assert execution.price is not None
    shares_to_sell = state.copied_shares if event.event_type == "full_exit" else state.copied_shares * whale_fraction
    closed = close_lots(state, shares_to_sell, execution.price)
    sell_usd = closed["shares"] * execution.price
    state.copied_shares = max(0.0, state.copied_shares - closed["shares"])
    state.copied_exposure_usd = max(0.0, state.copied_exposure_usd - closed["cost_basis"])
    state.total_sell_usd += sell_usd
    state.gross_pnl += closed["gross_pnl"]
    state.fees += closed["shares"] * fee_rate * execution.price * (1 - execution.price)
    state.closed_at = target_time if state.copied_shares <= EPSILON else state.closed_at
    state.worst_quality = min_quality(state.worst_quality, execution.data_quality)
    state.sell_count += 1
    return store_lifecycle_event(
        session,
        run_id,
        period_label,
        position,
        event,
        copied_action="sell",
        target_time=target_time,
        execution=execution,
        whale_trade_usd=event_usd(event),
        desired_copy_usd=None,
        actual_copy_usd=sell_usd,
        copied_position_before=before,
        copied_position_after=state.copied_exposure_usd,
        data_quality=execution.data_quality,
        skip_reason=None,
        raw={
            "whale_exit_fraction": whale_fraction,
            "sold_copied_shares": closed["shares"],
            "sold_cost_basis_usd": closed["cost_basis"],
            "gross_pnl": closed["gross_pnl"],
            "exit_delay_seconds": lifecycle_config.exit_delay_seconds,
        },
    )


def lookup_execution_price(
    session: Session,
    clob: ClobClient,
    cache: dict[str, Any],
    config: dict[str, Any],
    token_id: str,
    target_time: datetime | None,
    context: str,
    lifecycle_config: LifecycleCopyConfig,
) -> PriceExecution:
    if target_time is None:
        return PriceExecution(None, None, None, "insufficient_data", f"{context}_price_missing", None)
    lookup = nearest_price_history(
        session,
        clob,
        cache,
        config,
        token_id,
        target_time,
        context="copy" if context == "entry" else "exit",
        require_history_extends=context == "exit",
    )
    if lookup.price is None:
        return PriceExecution(None, lookup.point.timestamp if lookup.point else None, lookup.distance_seconds, "insufficient_data", price_skip_reason(context, lookup.reason), lookup)
    data_quality = "price_history_proxy"
    if data_quality not in set(lifecycle_config.allowed_data_quality):
        return PriceExecution(lookup.price, lookup.point.timestamp if lookup.point else None, lookup.distance_seconds, data_quality, "data_quality_not_allowed", lookup)
    return PriceExecution(lookup.price, lookup.point.timestamp if lookup.point else None, lookup.distance_seconds, data_quality, None, lookup)


def store_lifecycle_event(
    session: Session,
    run_id: str,
    period_label: str,
    position: ReconstructedPosition,
    event: ReconstructedPositionEvent,
    *,
    copied_action: str,
    target_time: datetime | None,
    execution: PriceExecution | None,
    whale_trade_usd: float | None,
    desired_copy_usd: float | None,
    actual_copy_usd: float | None,
    copied_position_before: float | None,
    copied_position_after: float | None,
    data_quality: str,
    skip_reason: str | None,
    raw: dict[str, Any],
) -> LifecycleCopyEvent:
    row_id = stable_id("lifecycle_event", run_id, period_label, position.position_id, event.id, copied_action, skip_reason)
    row = LifecycleCopyEvent(
        id=row_id,
        run_id=run_id,
        wallet_address=position.wallet_address,
        token_id=position.token_id,
        market_id=position.market_id,
        position_id=position.position_id,
        whale_event_id=event.id,
        whale_event_type=event.event_type,
        whale_time=to_utc(event.timestamp) if event.timestamp else None,
        copied_action=copied_action,
        target_time=target_time,
        execution_price=round_float(execution.price) if execution else None,
        execution_price_timestamp=execution.timestamp if execution else None,
        price_distance_seconds=round_float(execution.distance_seconds) if execution else None,
        whale_trade_usd=round_float(whale_trade_usd),
        desired_copy_usd=round_float(desired_copy_usd),
        actual_copy_usd=round_float(actual_copy_usd),
        copied_position_before=round_float(copied_position_before),
        copied_position_after=round_float(copied_position_after),
        data_quality=data_quality,
        skip_reason=skip_reason,
        raw_json=json_dumps({"period_label": period_label, **raw}),
    )
    session.merge(row)
    return row


def store_lifecycle_position(
    session: Session,
    run_id: str,
    period_label: str,
    position: ReconstructedPosition,
    state: CopyState,
    category: str,
    fee_warning: str | None,
) -> LifecycleCopyPosition:
    net_pnl = state.gross_pnl - state.fees
    if state.buy_count == 0:
        status = "skipped"
        skip_reason = most_common_skip(state.skip_reasons) or "no_reconstructed_position"
    elif state.copied_shares > EPSILON:
        status = "open"
        skip_reason = "lifecycle_not_closed"
    else:
        status = "closed"
        skip_reason = None
    row_id = stable_id("lifecycle_position", run_id, period_label, position.position_id)
    row = LifecycleCopyPosition(
        id=row_id,
        run_id=run_id,
        wallet_address=position.wallet_address,
        token_id=position.token_id,
        market_id=position.market_id,
        position_id=position.position_id,
        opened_at=state.opened_at,
        closed_at=state.closed_at,
        copied_total_buy_usd=round_float(state.total_buy_usd),
        copied_total_sell_usd=round_float(state.total_sell_usd),
        copied_realized_pnl=round_float(net_pnl) if state.sell_count else None,
        status=status,
        skip_reason=skip_reason,
        raw_json=json_dumps(
            {
                "period_label": period_label,
                "category": category,
                "gross_pnl": round_float(state.gross_pnl),
                "fees": round_float(state.fees),
                "net_pnl": round_float(net_pnl),
                "copied_open_shares": round_float(state.copied_shares),
                "copied_open_exposure_usd": round_float(state.copied_exposure_usd),
                "data_quality": state.worst_quality,
                "cap_hit_count": state.cap_hit_count,
                "below_min_trade_count": state.below_min_trade_count,
                "buy_count": state.buy_count,
                "sell_count": state.sell_count,
                "skip_reasons": dict(state.skip_reasons),
                "fee_warning": fee_warning,
            }
        ),
    )
    session.merge(row)
    return row


def reconstructed_events(session: Session, position: ReconstructedPosition) -> list[ReconstructedPositionEvent]:
    return list(
        session.scalars(
            select(ReconstructedPositionEvent)
            .where(ReconstructedPositionEvent.position_id == position.position_id)
            .order_by(ReconstructedPositionEvent.timestamp.asc(), ReconstructedPositionEvent.trade_id.asc(), ReconstructedPositionEvent.id.asc())
        )
    )


def lifecycle_positions(session: Session, wallets: list[str] | None) -> list[ReconstructedPosition]:
    stmt = (
        select(ReconstructedPosition)
        .where(ReconstructedPosition.status != "missing_prior_inventory")
        .order_by(ReconstructedPosition.opened_at.asc(), ReconstructedPosition.position_id.asc())
    )
    if wallets:
        stmt = stmt.where(ReconstructedPosition.wallet_address.in_(wallets))
    return list(session.scalars(stmt))


def lifecycle_wallets(session: Session) -> list[str]:
    return sorted(set(session.scalars(select(ReconstructedPosition.wallet_address).distinct())))


def event_is_copy_entry_signal(event: ReconstructedPositionEvent, lifecycle_config: LifecycleCopyConfig) -> bool:
    return event_is_inside_period(event, lifecycle_config) and event_raw_in_analysis(event)


def event_is_inside_period(event: ReconstructedPositionEvent, lifecycle_config: LifecycleCopyConfig) -> bool:
    if event.timestamp is None:
        return False
    timestamp = to_utc(event.timestamp)
    if lifecycle_config.date_start and timestamp < lifecycle_config.date_start:
        return False
    if lifecycle_config.date_end and timestamp > lifecycle_config.date_end:
        return False
    return True


def event_raw_in_analysis(event: ReconstructedPositionEvent) -> bool:
    raw = json_loads(event.raw_json, {}) or {}
    return bool(raw.get("in_analysis_window", True)) if isinstance(raw, dict) else True


def event_usd(event: ReconstructedPositionEvent) -> float | None:
    if event.usd_value is not None:
        return float(event.usd_value)
    if event.price is None or event.shares is None:
        return None
    return float(event.price) * float(event.shares)


def whale_exit_fraction(event: ReconstructedPositionEvent) -> float | None:
    before = float(event.position_before or 0.0)
    if before <= EPSILON:
        return None
    raw = json_loads(event.raw_json, {}) or {}
    sold = parse_float(raw.get("matched_shares")) if isinstance(raw, dict) else None
    if sold is None:
        sold = max(0.0, before - float(event.position_after or 0.0))
    if sold <= EPSILON:
        return None
    return max(0.0, min(1.0, sold / before))


def close_lots(state: CopyState, shares_to_sell: float, exit_price: float) -> dict[str, float]:
    remaining = min(shares_to_sell, state.copied_shares)
    sold_shares = 0.0
    cost_basis = 0.0
    gross_pnl = 0.0
    while remaining > EPSILON and state.lots:
        lot = state.lots[0]
        matched = min(lot.remaining_shares, remaining)
        fraction = matched / lot.remaining_shares if lot.remaining_shares > 0 else 0.0
        matched_cost = lot.remaining_cost_usd * fraction
        lot.remaining_shares -= matched
        lot.remaining_cost_usd -= matched_cost
        remaining -= matched
        sold_shares += matched
        cost_basis += matched_cost
        gross_pnl += matched * (exit_price - lot.entry_price)
        if lot.remaining_shares <= EPSILON:
            state.lots.pop(0)
    return {"shares": sold_shares, "cost_basis": cost_basis, "gross_pnl": gross_pnl}


def clear_lifecycle_run(session: Session, run_id: str) -> None:
    session.execute(delete(LifecycleCopyEvent).where(LifecycleCopyEvent.run_id == run_id))
    session.execute(delete(LifecycleCopyPosition).where(LifecycleCopyPosition.run_id == run_id))
    session.execute(delete(LifecycleCopyRun).where(LifecycleCopyRun.run_id == run_id))
    session.flush()


def ensure_lifecycle_run(
    session: Session,
    run_id: str,
    lifecycle_config: LifecycleCopyConfig,
    selected_wallets: list[str] | None,
) -> None:
    row = session.get(LifecycleCopyRun, run_id)
    if row is None:
        row = LifecycleCopyRun(run_id=run_id, created_at=utc_now())
        session.add(row)
    row.config_json = json_dumps(config_to_json(lifecycle_config, selected_wallets))
    row.wallet_address = selected_wallets[0] if selected_wallets and len(selected_wallets) == 1 else None


def config_to_json(lifecycle_config: LifecycleCopyConfig, selected_wallets: list[str] | None) -> dict[str, Any]:
    payload = lifecycle_config.__dict__.copy()
    payload["selected_wallets"] = selected_wallets or []
    for key, value in list(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
    return payload


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "period_label": result.get("period_label"),
        "selected_wallets": result.get("selected_wallets", []),
        "candidate_count": result.get("candidate_count", 0),
        "closed_positions": result.get("closed_positions", 0),
        "open_positions": result.get("open_positions", 0),
        "skipped_positions": result.get("skipped_positions", 0),
        "total_pnl": result.get("total_pnl", 0.0),
        "cap_hit_count": result.get("cap_hit_count", 0),
        "below_min_trade_count": result.get("below_min_trade_count", 0),
        "skipped_signal_reasons": result.get("skipped_signal_reasons", {}),
        "data_quality_summary": result.get("data_quality_summary", {}),
    }


def event_to_dict(row: LifecycleCopyEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "wallet_address": row.wallet_address,
        "token_id": row.token_id,
        "market_id": row.market_id,
        "position_id": row.position_id,
        "whale_event_id": row.whale_event_id,
        "whale_event_type": row.whale_event_type,
        "whale_time": row.whale_time.isoformat() if row.whale_time else None,
        "copied_action": row.copied_action,
        "target_time": row.target_time.isoformat() if row.target_time else None,
        "execution_price": row.execution_price,
        "execution_price_timestamp": row.execution_price_timestamp.isoformat() if row.execution_price_timestamp else None,
        "price_distance_seconds": row.price_distance_seconds,
        "whale_trade_usd": row.whale_trade_usd,
        "desired_copy_usd": row.desired_copy_usd,
        "actual_copy_usd": row.actual_copy_usd,
        "copied_position_before": row.copied_position_before,
        "copied_position_after": row.copied_position_after,
        "data_quality": row.data_quality,
        "skip_reason": row.skip_reason,
        "raw": json_loads(row.raw_json, {}),
    }


def position_to_dict(row: LifecycleCopyPosition, category: str | None = None) -> dict[str, Any]:
    raw = json_loads(row.raw_json, {}) or {}
    return {
        "id": row.id,
        "run_id": row.run_id,
        "wallet_address": row.wallet_address,
        "token_id": row.token_id,
        "market_id": row.market_id,
        "category": category or raw.get("category", "unknown"),
        "position_id": row.position_id,
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "copied_total_buy_usd": row.copied_total_buy_usd,
        "copied_total_sell_usd": row.copied_total_sell_usd,
        "copied_realized_pnl": row.copied_realized_pnl,
        "net_pnl": row.copied_realized_pnl,
        "status": row.status,
        "skip_reason": row.skip_reason,
        "data_quality": raw.get("data_quality"),
        "cap_hit_count": raw.get("cap_hit_count", 0),
        "below_min_trade_count": raw.get("below_min_trade_count", 0),
        "raw": raw,
    }


def lifecycle_events_dataframe(session: Session, run_id: str | None = None):
    import pandas as pd

    stmt = select(LifecycleCopyEvent).order_by(LifecycleCopyEvent.whale_time.desc().nullslast())
    if run_id:
        stmt = stmt.where(LifecycleCopyEvent.run_id == run_id)
    return pd.DataFrame([event_to_dict(row) for row in session.scalars(stmt)])


def lifecycle_positions_dataframe(session: Session, run_id: str | None = None):
    import pandas as pd

    stmt = select(LifecycleCopyPosition).order_by(LifecycleCopyPosition.opened_at.desc().nullslast())
    if run_id:
        stmt = stmt.where(LifecycleCopyPosition.run_id == run_id)
    return pd.DataFrame([position_to_dict(row) for row in session.scalars(stmt)])


def market_category_map(session: Session) -> dict[str, str]:
    return {market_id: category or "unknown" for market_id, category in session.execute(select(Market.market_id, Market.category))}


def data_quality_summary(data_qualities: list[str]) -> dict[str, Any]:
    counts = dict(Counter(data_qualities))
    total = sum(counts.values())
    return {
        "counts": counts,
        "percent": {level: round_float(count / total) for level, count in counts.items()} if total else {},
    }


def price_skip_reason(context: str, reason: str | None) -> str:
    if reason == "no_price_history":
        return "no_price_history"
    if context == "entry":
        if reason in {"copy_price_too_far", "copy_time_before_history", "copy_time_after_history"}:
            return "entry_price_too_far"
        return "entry_price_missing"
    if reason == "exit_price_too_far":
        return "exit_price_too_far"
    return "exit_price_missing"


def min_quality(left: str | None, right: str) -> str:
    if left is None:
        return right
    return left if quality_rank(left) <= quality_rank(right) else right


def most_common_skip(counter: Counter) -> str | None:
    return counter.most_common(1)[0][0] if counter else None


def normalize_wallets(wallets: list[str] | None) -> list[str] | None:
    if not wallets:
        return None
    return sorted({wallet.strip().lower() for wallet in wallets if wallet and wallet.strip()})


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_utc(value: datetime | None) -> datetime:
    if value is None:
        return utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def round_float(value: Any, digits: int = 8) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def stable_id(*parts: Any) -> str:
    digest = hashlib.sha256(json_dumps(parts).encode("utf-8")).hexdigest()
    return f"{parts[0]}:{digest}"

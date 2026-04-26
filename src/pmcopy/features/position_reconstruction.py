from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pmcopy.config import database_url
from pmcopy.db import (
    ReconstructedPosition,
    ReconstructedPositionEvent,
    Trade,
    Wallet,
    init_db,
    json_dumps,
    json_loads,
    session_scope,
)


EPSILON = 1e-9


@dataclass
class ReconstructionConfig:
    wallet_address: str | None = None
    analysis_start: datetime | None = None
    analysis_end: datetime | None = None
    warmup_days: int = 90


@dataclass
class PositionState:
    wallet_address: str
    token_id: str
    market_id: str | None
    position_id: str
    opened_at: datetime
    inventory: float = 0.0
    total_buy_shares: float = 0.0
    total_buy_cost: float = 0.0
    total_sell_shares: float = 0.0
    total_sell_proceeds: float = 0.0
    realized_pnl: float = 0.0
    had_exit: bool = False
    closed_at: datetime | None = None

    @property
    def avg_buy_price(self) -> float | None:
        return self.total_buy_cost / self.total_buy_shares if self.total_buy_shares > 0 else None

    @property
    def avg_sell_price(self) -> float | None:
        return self.total_sell_proceeds / self.total_sell_shares if self.total_sell_shares > 0 else None

    @property
    def status(self) -> str:
        if self.inventory <= EPSILON:
            return "closed"
        return "partial" if self.had_exit else "open"


def reconstruction_config_from_values(
    *,
    wallet_address: str | None = None,
    analysis_start: Any = None,
    analysis_end: Any = None,
    warmup_days: int | None = None,
) -> ReconstructionConfig:
    return ReconstructionConfig(
        wallet_address=wallet_address.lower() if wallet_address else None,
        analysis_start=parse_datetime(analysis_start),
        analysis_end=parse_datetime(analysis_end, end_of_day=True),
        warmup_days=int(warmup_days if warmup_days is not None else 90),
    )


def reconstruct_wallet_positions(
    config: dict[str, Any],
    wallet_address: str,
    recon_config: ReconstructionConfig | None = None,
) -> dict[str, Any]:
    init_db(config)
    recon_config = recon_config or reconstruction_config_from_values(wallet_address=wallet_address)
    with session_scope(database_url(config)) as session:
        return reconstruct_wallet_positions_in_session(session, wallet_address.lower(), recon_config)


def reconstruct_promoted_positions(
    config: dict[str, Any],
    recon_config: ReconstructionConfig | None = None,
    limit: int | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    init_db(config)
    recon_config = recon_config or reconstruction_config_from_values()
    with session_scope(database_url(config)) as session:
        stmt = select(Wallet.wallet_address).order_by(Wallet.wallet_address)
        if limit:
            stmt = stmt.limit(limit)
        wallets = list(session.scalars(stmt))
        totals = empty_stats()
        totals["wallets"] = len(wallets)
        for index, wallet in enumerate(wallets, start=1):
            if progress_callback:
                progress_callback(index, len(wallets), wallet, None)
            stats = reconstruct_wallet_positions_in_session(session, wallet, recon_config)
            if progress_callback:
                progress_callback(index, len(wallets), wallet, stats)
            merge_stats(totals, stats)
        return totals


def reconstruct_wallet_positions_in_session(
    session: Session,
    wallet_address: str,
    recon_config: ReconstructionConfig,
) -> dict[str, Any]:
    clear_reconstruction(session, wallet_address)
    trades = reconstruction_trades(session, wallet_address, recon_config)
    states: dict[str, PositionState] = {}
    all_states: list[PositionState] = []
    stats = empty_stats()

    for trade in trades:
        stats["trades"] += 1
        if not usable_trade(trade):
            continue
        token_id = trade.token_id or ""
        state = states.get(token_id)
        side = (trade.side or "").upper()
        if side == "BUY":
            state = handle_buy(session, trade, state, all_states, recon_config)
            states[token_id] = state
            stats["events"] += 1
        elif side == "SELL":
            state = handle_sell(session, trade, state, all_states, recon_config, stats)
            if state is not None and state.inventory > EPSILON:
                states[token_id] = state
            elif token_id in states:
                states.pop(token_id, None)
            stats["events"] += 1

    for state in all_states:
        upsert_position(session, state)

    stats.update(reconstruction_counts(session, wallet_address))
    return stats


def clear_reconstruction(session: Session, wallet_address: str) -> None:
    position_ids = list(
        session.scalars(select(ReconstructedPosition.position_id).where(ReconstructedPosition.wallet_address == wallet_address))
    )
    session.execute(delete(ReconstructedPositionEvent).where(ReconstructedPositionEvent.wallet_address == wallet_address))
    if position_ids:
        session.execute(delete(ReconstructedPosition).where(ReconstructedPosition.position_id.in_(position_ids)))
    session.flush()


def reconstruction_trades(session: Session, wallet_address: str, recon_config: ReconstructionConfig) -> list[Trade]:
    stmt = (
        select(Trade)
        .where(Trade.wallet_address == wallet_address)
        .where(Trade.timestamp.is_not(None))
        .order_by(Trade.token_id, Trade.timestamp, Trade.id)
    )
    start = warmup_start(recon_config)
    if start is not None:
        stmt = stmt.where(Trade.timestamp >= start)
    if recon_config.analysis_end is not None:
        stmt = stmt.where(Trade.timestamp <= recon_config.analysis_end)
    return list(session.scalars(stmt))


def warmup_start(recon_config: ReconstructionConfig) -> datetime | None:
    if recon_config.analysis_start is None:
        return None
    return recon_config.analysis_start - timedelta(days=recon_config.warmup_days)


def usable_trade(trade: Trade) -> bool:
    return bool(trade.token_id and trade.timestamp and trade.side and trade.price is not None and trade.size and trade.size > 0)


def handle_buy(
    session: Session,
    trade: Trade,
    state: PositionState | None,
    all_states: list[PositionState],
    recon_config: ReconstructionConfig,
) -> PositionState:
    timestamp = to_utc(trade.timestamp)
    if state is None or state.inventory <= EPSILON:
        state = PositionState(
            wallet_address=trade.wallet_address,
            token_id=trade.token_id or "",
            market_id=trade.market_id,
            position_id=stable_id("position", trade.wallet_address, trade.token_id, trade.id),
            opened_at=timestamp,
        )
        all_states.append(state)
        event_type = "open_position"
    else:
        event_type = "increase_position"
    before = state.inventory
    shares = float(trade.size or 0.0)
    state.inventory += shares
    state.total_buy_shares += shares
    state.total_buy_cost += shares * float(trade.price or 0.0)
    store_event(session, trade, state.position_id, before, state.inventory, event_type, recon_config)
    return state


def handle_sell(
    session: Session,
    trade: Trade,
    state: PositionState | None,
    all_states: list[PositionState],
    recon_config: ReconstructionConfig,
    stats: dict[str, Any],
) -> PositionState | None:
    timestamp = to_utc(trade.timestamp)
    shares = float(trade.size or 0.0)
    if state is None or state.inventory <= EPSILON:
        stats["missing_prior_inventory"] += 1
        stats["orphan_sell"] += 1
        position_id = stable_id("missing_position", trade.wallet_address, trade.token_id, trade.id)
        missing = PositionState(
            wallet_address=trade.wallet_address,
            token_id=trade.token_id or "",
            market_id=trade.market_id,
            position_id=position_id,
            opened_at=timestamp,
            total_sell_shares=shares,
            total_sell_proceeds=shares * float(trade.price or 0.0),
            closed_at=timestamp,
        )
        all_states.append(missing)
        store_invalid_position(session, missing, "missing_prior_inventory")
        store_event(session, trade, position_id, 0.0, 0.0, "missing_prior_inventory", recon_config, {"orphan_sell": True})
        return None

    before = state.inventory
    matched_shares = min(shares, state.inventory)
    state.inventory -= matched_shares
    state.total_sell_shares += matched_shares
    state.total_sell_proceeds += matched_shares * float(trade.price or 0.0)
    state.had_exit = True
    if state.avg_buy_price is not None:
        state.realized_pnl += matched_shares * (float(trade.price or 0.0) - state.avg_buy_price)
    event_type = "full_exit" if state.inventory <= EPSILON else "partial_exit"
    if event_type == "full_exit":
        state.closed_at = timestamp
        state.inventory = 0.0
    extra = {"matched_shares": matched_shares, "unmatched_sell_shares": max(0.0, shares - matched_shares)}
    store_event(session, trade, state.position_id, before, state.inventory, event_type, recon_config, extra)
    return state


def store_invalid_position(session: Session, state: PositionState, status: str) -> None:
    row = session.get(ReconstructedPosition, state.position_id)
    if row is None:
        row = ReconstructedPosition(id=state.position_id, position_id=state.position_id)
        session.add(row)
    row.wallet_address = state.wallet_address
    row.token_id = state.token_id
    row.market_id = state.market_id
    row.opened_at = state.opened_at
    row.closed_at = state.closed_at
    row.total_buy_shares = 0.0
    row.total_sell_shares = state.total_sell_shares
    row.total_buy_usd = 0.0
    row.total_sell_usd = round_float(state.total_sell_proceeds)
    row.avg_buy_price = None
    row.avg_sell_price = state.avg_sell_price
    row.realized_pnl = None
    row.status = status
    row.raw_json = json_dumps({"status": status})


def upsert_position(session: Session, state: PositionState) -> None:
    existing = session.get(ReconstructedPosition, state.position_id)
    if existing is not None and existing.status == "missing_prior_inventory":
        return
    row = existing or ReconstructedPosition(id=state.position_id, position_id=state.position_id)
    if existing is None:
        session.add(row)
    row.wallet_address = state.wallet_address
    row.token_id = state.token_id
    row.market_id = state.market_id
    row.opened_at = state.opened_at
    row.closed_at = state.closed_at
    row.total_buy_shares = round_float(state.total_buy_shares)
    row.total_sell_shares = round_float(state.total_sell_shares)
    row.total_buy_usd = round_float(state.total_buy_cost)
    row.total_sell_usd = round_float(state.total_sell_proceeds)
    row.avg_buy_price = round_float(state.avg_buy_price)
    row.avg_sell_price = round_float(state.avg_sell_price)
    row.realized_pnl = round_float(state.realized_pnl) if state.total_sell_shares > 0 else None
    row.status = state.status
    row.raw_json = json_dumps({"ending_inventory": round_float(state.inventory)})


def store_event(
    session: Session,
    trade: Trade,
    position_id: str | None,
    before: float,
    after: float,
    event_type: str,
    recon_config: ReconstructionConfig,
    extra: dict[str, Any] | None = None,
) -> None:
    row_id = stable_id("position_event", trade.id, position_id, event_type)
    row = ReconstructedPositionEvent(
        id=row_id,
        wallet_address=trade.wallet_address,
        token_id=trade.token_id or "",
        market_id=trade.market_id,
        position_id=position_id,
        trade_id=trade.id,
        timestamp=to_utc(trade.timestamp) if trade.timestamp else None,
        side=trade.side,
        price=trade.price,
        shares=trade.size,
        usd_value=round_float(trade_usd_value(trade)),
        position_before=round_float(before),
        position_after=round_float(after),
        event_type=event_type,
        raw_json=json_dumps(
            {
                "in_analysis_window": in_analysis_window(to_utc(trade.timestamp), recon_config) if trade.timestamp else False,
                "analysis_start": recon_config.analysis_start.isoformat() if recon_config.analysis_start else None,
                "analysis_end": recon_config.analysis_end.isoformat() if recon_config.analysis_end else None,
                **(extra or {}),
            }
        ),
    )
    session.add(row)


def reconstruction_counts(session: Session, wallet_address: str | None = None) -> dict[str, Any]:
    positions_stmt = select(ReconstructedPosition)
    events_stmt = select(ReconstructedPositionEvent)
    if wallet_address:
        positions_stmt = positions_stmt.where(ReconstructedPosition.wallet_address == wallet_address)
        events_stmt = events_stmt.where(ReconstructedPositionEvent.wallet_address == wallet_address)
    positions = list(session.scalars(positions_stmt))
    events = list(session.scalars(events_stmt))
    status_counts: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    for row in positions:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    for row in events:
        event_counts[row.event_type] = event_counts.get(row.event_type, 0) + 1
    return {
        "positions": len(positions),
        "events": len(events),
        "closed_positions": status_counts.get("closed", 0),
        "open_positions": status_counts.get("open", 0),
        "partial_positions": status_counts.get("partial", 0),
        "missing_prior_inventory": status_counts.get("missing_prior_inventory", 0),
        "orphan_sell": event_counts.get("missing_prior_inventory", 0) + event_counts.get("orphan_sell", 0),
        "status_counts": status_counts,
        "event_counts": event_counts,
    }


def inspect_position(config: dict[str, Any], wallet_address: str, token_id: str) -> dict[str, Any]:
    init_db(config)
    wallet = wallet_address.lower()
    with session_scope(database_url(config)) as session:
        positions = list(
            session.scalars(
                select(ReconstructedPosition)
                .where(ReconstructedPosition.wallet_address == wallet)
                .where(ReconstructedPosition.token_id == token_id)
                .order_by(ReconstructedPosition.opened_at)
            )
        )
        events = list(
            session.scalars(
                select(ReconstructedPositionEvent)
                .where(ReconstructedPositionEvent.wallet_address == wallet)
                .where(ReconstructedPositionEvent.token_id == token_id)
                .order_by(ReconstructedPositionEvent.timestamp, ReconstructedPositionEvent.trade_id)
            )
        )
        return {
            "positions": [position_to_dict(row) for row in positions],
            "events": [event_to_dict(row) for row in events],
        }


def positions_dataframe(session: Session, wallet_address: str | None = None) -> pd.DataFrame:
    stmt = select(ReconstructedPosition).order_by(ReconstructedPosition.opened_at.desc())
    if wallet_address:
        stmt = stmt.where(ReconstructedPosition.wallet_address == wallet_address.lower())
    return pd.DataFrame([position_to_dict(row) for row in session.scalars(stmt)])


def events_dataframe(session: Session, wallet_address: str | None = None) -> pd.DataFrame:
    stmt = select(ReconstructedPositionEvent).order_by(ReconstructedPositionEvent.timestamp.desc())
    if wallet_address:
        stmt = stmt.where(ReconstructedPositionEvent.wallet_address == wallet_address.lower())
    return pd.DataFrame([event_to_dict(row) for row in session.scalars(stmt)])


def position_to_dict(row: ReconstructedPosition) -> dict[str, Any]:
    return {
        "wallet_address": row.wallet_address,
        "token_id": row.token_id,
        "market_id": row.market_id,
        "position_id": row.position_id,
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "total_buy_shares": row.total_buy_shares,
        "total_sell_shares": row.total_sell_shares,
        "total_buy_usd": row.total_buy_usd,
        "total_sell_usd": row.total_sell_usd,
        "avg_buy_price": row.avg_buy_price,
        "avg_sell_price": row.avg_sell_price,
        "realized_pnl": row.realized_pnl,
        "status": row.status,
        "raw": json_loads(row.raw_json, {}),
    }


def event_to_dict(row: ReconstructedPositionEvent) -> dict[str, Any]:
    return {
        "wallet_address": row.wallet_address,
        "token_id": row.token_id,
        "market_id": row.market_id,
        "position_id": row.position_id,
        "trade_id": row.trade_id,
        "timestamp": row.timestamp,
        "side": row.side,
        "price": row.price,
        "shares": row.shares,
        "usd_value": row.usd_value,
        "position_before": row.position_before,
        "position_after": row.position_after,
        "event_type": row.event_type,
        "raw": json_loads(row.raw_json, {}),
    }


def empty_stats() -> dict[str, Any]:
    return {
        "wallets": 0,
        "trades": 0,
        "events": 0,
        "positions": 0,
        "closed_positions": 0,
        "open_positions": 0,
        "partial_positions": 0,
        "missing_prior_inventory": 0,
        "orphan_sell": 0,
    }


def merge_stats(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, int) and key != "wallets":
            target[key] = int(target.get(key, 0)) + value


def in_analysis_window(timestamp: datetime, recon_config: ReconstructionConfig) -> bool:
    if recon_config.analysis_start and timestamp < recon_config.analysis_start:
        return False
    if recon_config.analysis_end and timestamp > recon_config.analysis_end:
        return False
    return True


def parse_datetime(value: Any, *, end_of_day: bool = False) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
                parsed = datetime.fromisoformat(cleaned)
                return parsed.replace(
                    hour=23 if end_of_day else 0,
                    minute=59 if end_of_day else 0,
                    second=59 if end_of_day else 0,
                    microsecond=999999 if end_of_day else 0,
                    tzinfo=timezone.utc,
                )
            return to_utc(datetime.fromisoformat(cleaned.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def round_float(value: Any, digits: int = 8) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def trade_usd_value(trade: Trade) -> float | None:
    if trade.usd_value is not None:
        return float(trade.usd_value)
    if trade.price is None or trade.size is None:
        return None
    return float(trade.price) * float(trade.size)


def stable_id(*parts: Any) -> str:
    digest = hashlib.sha256(json_dumps(parts).encode("utf-8")).hexdigest()
    return f"{parts[0]}:{digest}"

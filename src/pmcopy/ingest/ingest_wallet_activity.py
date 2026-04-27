from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmcopy.api.data_api import DataAPIClient
from pmcopy.config import database_url
from pmcopy.db import (
    Activity,
    Market,
    Token,
    Trade,
    Wallet,
    WalletSnapshot,
    init_db,
    json_dumps,
    session_scope,
    utc_now,
)
from pmcopy.ingest.discover_wallets import extract_username, normalize_wallet, parse_timestamp
from pmcopy.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass
class WalletIngestionResult:
    wallet_address: str
    trades: int = 0
    activity: int = 0
    positions: int = 0
    closed_positions: int = 0
    value_snapshots: int = 0
    warnings: list[str] = field(default_factory=list)


def ingest_promoted_wallets(
    config: dict[str, Any],
    limit: int | None = None,
    progress_callback: Callable[[int, int, str, WalletIngestionResult | None], None] | None = None,
) -> list[WalletIngestionResult]:
    init_db(config)
    with session_scope(database_url(config)) as session:
        stmt = select(Wallet.wallet_address).order_by(Wallet.wallet_address)
        if limit:
            stmt = stmt.limit(limit)
        wallets = list(session.scalars(stmt))

    results: list[WalletIngestionResult] = []
    total = len(wallets)
    for index, wallet in enumerate(wallets, start=1):
        if progress_callback:
            progress_callback(index, total, wallet, None)
        result = ingest_wallet(config, wallet)
        results.append(result)
        if progress_callback:
            progress_callback(index, total, wallet, result)
    return results


def ingest_wallet(config: dict[str, Any], wallet_address: str) -> WalletIngestionResult:
    normalized = normalize_wallet(wallet_address)
    if not normalized:
        return WalletIngestionResult(wallet_address=wallet_address, warnings=["invalid wallet address"])

    init_db(config)
    result = WalletIngestionResult(wallet_address=normalized)
    ingestion_cfg = config.get("wallet_ingestion", {})
    with session_scope(database_url(config)) as session:
        wallet = session.get(Wallet, normalized)
        if wallet is None:
            wallet = Wallet(
                wallet_address=normalized,
                source="manual_ingest",
                first_seen_at=utc_now(),
                notes="Created by ingest-wallet command.",
            )
            session.add(wallet)

        client = DataAPIClient(config, session=session)
        last_seen: datetime | None = wallet.last_seen_at

        if ingestion_cfg.get("trades", {}).get("enabled", True):
            rows = client.get_wallet_trades(
                normalized,
                page_size=int(ingestion_cfg.get("trades", {}).get("page_size", 500)),
                max_pages=int(ingestion_cfg.get("trades", {}).get("max_pages", 20)),
            )
            result.trades = upsert_trades(session, normalized, rows)
            last_seen = max_datetime(last_seen, max_timestamp(rows))
            update_wallet_identity(wallet, rows)
            if not rows:
                result.warnings.append("no trades returned from Data API /trades?user=wallet")

        if ingestion_cfg.get("activity", {}).get("enabled", True):
            rows = client.get_wallet_activity(
                normalized,
                page_size=int(ingestion_cfg.get("activity", {}).get("page_size", 500)),
                max_pages=int(ingestion_cfg.get("activity", {}).get("max_pages", 5)),
            )
            result.activity = upsert_activity(session, normalized, rows)
            last_seen = max_datetime(last_seen, max_timestamp(rows))
            update_wallet_identity(wallet, rows)
            if not rows:
                result.warnings.append("no activity returned from Data API /activity?user=wallet")

        if ingestion_cfg.get("positions", {}).get("enabled", True):
            rows = client.get_wallet_positions(
                normalized,
                page_size=int(ingestion_cfg.get("positions", {}).get("page_size", 500)),
                max_pages=int(ingestion_cfg.get("positions", {}).get("max_pages", 20)),
            )
            result.positions = upsert_wallet_snapshots(session, normalized, rows, "data_api:/positions")
            update_wallet_identity(wallet, rows)
            if not rows:
                result.warnings.append("no open positions returned from Data API /positions?user=wallet")

        if ingestion_cfg.get("closed_positions", {}).get("enabled", True):
            rows = client.get_wallet_closed_positions(
                normalized,
                page_size=int(ingestion_cfg.get("closed_positions", {}).get("page_size", 500)),
                max_pages=int(ingestion_cfg.get("closed_positions", {}).get("max_pages", 20)),
            )
            result.closed_positions = upsert_wallet_snapshots(session, normalized, rows, "data_api:/closed-positions")
            update_wallet_identity(wallet, rows)
            if not rows:
                result.warnings.append("no closed positions returned from Data API /closed-positions?user=wallet")

        if ingestion_cfg.get("value", {}).get("enabled", True):
            rows = client.get_wallet_value(normalized)
            result.value_snapshots = upsert_wallet_snapshots(session, normalized, rows, "data_api:/value")
            if not rows:
                result.warnings.append("no value returned from Data API /value?user=wallet")

        wallet.last_seen_at = last_seen or wallet.last_seen_at

    return result


def upsert_trades(session: Session, wallet_address: str, rows: list[dict[str, Any]]) -> int:
    upserted = 0
    for row in rows:
        if normalize_wallet(row.get("proxyWallet")) and normalize_wallet(row.get("proxyWallet")) != wallet_address:
            continue
        market_id, token_id = upsert_market_token_from_row(session, row)
        timestamp = parse_timestamp(row.get("timestamp"))
        price = first_float(row, ("price",))
        size = first_float(row, ("size", "shares"))
        trade_id = row_id("trade", wallet_address, row)
        trade = session.get(Trade, trade_id)
        if trade is None:
            trade = Trade(id=trade_id, wallet_address=wallet_address)
            session.add(trade)
            upserted += 1
        trade.market_id = market_id
        trade.token_id = token_id
        trade.side = string_or_none(row.get("side"))
        trade.price = price
        trade.size = size
        trade.usd_value = first_float(row, ("usdValue", "usd_value", "usdcSize")) or multiply_or_none(price, size)
        trade.timestamp = timestamp
        trade.tx_hash = string_or_none(row.get("transactionHash") or row.get("tx_hash"))
        trade.outcome = string_or_none(row.get("outcome"))
        trade.title = string_or_none(row.get("title"))
        trade.source_endpoint = "data_api:/trades"
        trade.raw_json = json_dumps(row)
    return upserted


def upsert_activity(session: Session, wallet_address: str, rows: list[dict[str, Any]]) -> int:
    upserted = 0
    for row in rows:
        if normalize_wallet(row.get("proxyWallet")) and normalize_wallet(row.get("proxyWallet")) != wallet_address:
            continue
        market_id, token_id = upsert_market_token_from_row(session, row)
        activity_id = row_id("activity", wallet_address, row)
        activity = session.get(Activity, activity_id)
        if activity is None:
            activity = Activity(id=activity_id, wallet_address=wallet_address)
            session.add(activity)
            upserted += 1
        activity.market_id = market_id
        activity.token_id = token_id
        activity.activity_type = string_or_none(row.get("type") or row.get("activityType"))
        activity.side = string_or_none(row.get("side"))
        activity.price = first_float(row, ("price",))
        activity.size = first_float(row, ("size", "shares"))
        activity.timestamp = parse_timestamp(row.get("timestamp"))
        activity.raw_json = json_dumps(row)
    return upserted


def upsert_wallet_snapshots(session: Session, wallet_address: str, rows: list[dict[str, Any]], source_endpoint: str) -> int:
    upserted = 0
    fetched_at = utc_now()
    for row in rows:
        market_id, token_id = upsert_market_token_from_row(session, row)
        snapshot_id = row_id(source_endpoint, wallet_address, row)
        snapshot = session.get(WalletSnapshot, snapshot_id)
        if snapshot is None:
            snapshot = WalletSnapshot(id=snapshot_id, wallet_address=wallet_address)
            session.add(snapshot)
            upserted += 1
        snapshot.fetched_at = fetched_at
        snapshot.source_endpoint = source_endpoint
        snapshot.market_id = market_id
        snapshot.token_id = token_id
        snapshot.value = first_float(row, ("value",))
        snapshot.initial_value = first_float(row, ("initialValue", "initial_value"))
        snapshot.current_value = first_float(row, ("currentValue", "current_value"))
        snapshot.cash_pnl = first_float(row, ("cashPnl", "cash_pnl"))
        snapshot.realized_pnl = first_float(row, ("realizedPnl", "realized_pnl"))
        snapshot.percent_pnl = first_float(row, ("percentPnl", "percent_pnl"))
        snapshot.size = first_float(row, ("size", "shares"))
        snapshot.avg_price = first_float(row, ("avgPrice", "avg_price"))
        snapshot.raw_json = json_dumps(row)
    return upserted


def upsert_market_token_from_row(session: Session, row: dict[str, Any]) -> tuple[str | None, str | None]:
    condition_id = string_or_none(row.get("conditionId") or row.get("condition_id") or row.get("market"))
    slug = string_or_none(row.get("slug"))
    title = string_or_none(row.get("title"))
    market_id = condition_id or slug
    if market_id:
        market = session.get(Market, market_id)
        if market is None and condition_id:
            market = session.scalar(select(Market).where(Market.condition_id == condition_id).limit(1))
        if market is None:
            market = Market(market_id=market_id)
            session.add(market)
        market.condition_id = market.condition_id or condition_id
        market.question = market.question or title
        market.slug = market.slug or slug
        market.event_slug = market.event_slug or string_or_none(row.get("eventSlug") or row.get("event_slug"))
        market.raw_json = market.raw_json if market.raw_json != "{}" else json_dumps({"source": "wallet_ingestion", **row})
        market_id = market.market_id

    token_id = string_or_none(row.get("asset") or row.get("token_id") or row.get("tokenId"))
    if token_id and market_id:
        token = session.get(Token, token_id)
        if token is None:
            token = Token(token_id=token_id, market_id=market_id)
            session.add(token)
        token.market_id = market_id
        token.outcome_name = token.outcome_name or string_or_none(row.get("outcome"))
        token.outcome_index = token.outcome_index if token.outcome_index is not None else int_or_none(row.get("outcomeIndex"))
        token.raw_json = token.raw_json if token.raw_json != "{}" else json_dumps({"source": "wallet_ingestion", **row})
    return market_id, token_id


def update_wallet_identity(wallet: Wallet, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        username = extract_username(row)
        if username and not wallet.username:
            wallet.username = username
        if wallet.first_seen_at is None:
            timestamp = parse_timestamp(row.get("timestamp"))
            if timestamp:
                wallet.first_seen_at = timestamp


def max_timestamp(rows: list[dict[str, Any]]) -> datetime | None:
    current: datetime | None = None
    for row in rows:
        current = max_datetime(current, parse_timestamp(row.get("timestamp")))
    return current


def max_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return max(left, right)


def row_id(prefix: str, wallet_address: str, row: dict[str, Any]) -> str:
    stable = {
        "wallet": wallet_address,
        "tx": row.get("transactionHash") or row.get("tx_hash"),
        "asset": row.get("asset") or row.get("token_id") or row.get("tokenId"),
        "condition": row.get("conditionId") or row.get("condition_id") or row.get("market"),
        "timestamp": row.get("timestamp"),
        "side": row.get("side"),
        "type": row.get("type"),
        "price": row.get("price") or row.get("avgPrice"),
        "size": row.get("size") or row.get("totalBought"),
        "value": row.get("value") or row.get("currentValue") or row.get("realizedPnl"),
        "raw": row if not (row.get("transactionHash") or row.get("asset") or row.get("conditionId")) else None,
    }
    digest = hashlib.sha256(json_dumps(stable).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


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
    return left * right


def string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

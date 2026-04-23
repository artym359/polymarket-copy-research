from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    event,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from pmcopy.config import database_url as config_database_url
from pmcopy.config import project_root

_ENGINES: dict[str, Engine] = {}
_INITIALIZED_DATABASE_URLS: set[str] = set()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def json_loads(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class Base(DeclarativeBase):
    pass


class CandidateWallet(Base):
    __tablename__ = "candidate_wallets"

    wallet_address: Mapped[str] = mapped_column(String(128), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    first_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    categories_json: Mapped[str] = mapped_column(Text, default="[]")
    raw_refs_json: Mapped[str] = mapped_column(Text, default="{}")
    discovery_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False)


class Wallet(Base):
    __tablename__ = "wallets"

    wallet_address: Mapped[str] = mapped_column(String(128), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Market(Base):
    __tablename__ = "markets"

    market_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    condition_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    slug: Mapped[str | None] = mapped_column(String(512), nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_slug: Mapped[str | None] = mapped_column(String(512), nullable=True)
    active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    closed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    archived: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    enable_order_book: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    start_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class Token(Base):
    __tablename__ = "tokens"

    token_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(128), ForeignKey("markets.market_id"))
    outcome_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yes_no_side: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    side: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_endpoint: Mapped[str] = mapped_column(String(255), default="data_api:/trades")
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class Activity(Base):
    __tablename__ = "activity"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    activity_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    side: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class WalletSnapshot(Base):
    __tablename__ = "wallet_snapshots"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    source_endpoint: Mapped[str] = mapped_column(String(255))
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class ReconstructedPosition(Base):
    __tablename__ = "reconstructed_positions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(255), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    position_id: Mapped[str] = mapped_column(String(255), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    total_buy_shares: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0)
    total_sell_shares: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0)
    total_buy_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_sell_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_buy_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_sell_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class ReconstructedPositionEvent(Base):
    __tablename__ = "reconstructed_position_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(255), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    position_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    trade_id: Mapped[str] = mapped_column(String(255), index=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    side: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class LifecycleCopyRun(Base):
    __tablename__ = "lifecycle_copy_runs"

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    wallet_address: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")


class LifecycleCopyEvent(Base):
    __tablename__ = "lifecycle_copy_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), ForeignKey("lifecycle_copy_runs.run_id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(255), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    position_id: Mapped[str] = mapped_column(String(255), index=True)
    whale_event_id: Mapped[str] = mapped_column(String(255), index=True)
    whale_event_type: Mapped[str] = mapped_column(String(128), index=True)
    whale_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    copied_action: Mapped[str] = mapped_column(String(32), index=True)
    target_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    execution_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_price_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_distance_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    whale_trade_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    desired_copy_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_copy_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    copied_position_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    copied_position_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality: Mapped[str] = mapped_column(String(64), index=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class LifecycleCopyPosition(Base):
    __tablename__ = "lifecycle_copy_positions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), ForeignKey("lifecycle_copy_runs.run_id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(255), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    position_id: Mapped[str] = mapped_column(String(255), index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    copied_total_buy_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    copied_total_sell_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    copied_realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class WalletMetrics(Base):
    __tablename__ = "wallet_metrics"

    wallet_address: Mapped[str] = mapped_column(String(128), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    total_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_on_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_capital_at_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_on_max_capital_at_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_exposure_method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_exposure_confidence: Mapped[str | None] = mapped_column(String(128), nullable=True)
    average_capital_at_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_on_average_capital_at_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_exposure_method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    average_exposure_confidence: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    market_count: Mapped[int] = mapped_column(Integer, default=0)
    active_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_trade_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_trade_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    win_rate_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_1_market_pnl_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_5_market_pnl_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    main_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category_breakdown_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")


class WalletClassification(Base):
    __tablename__ = "wallet_classification"

    wallet_address: Mapped[str] = mapped_column(String(128), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    class_label: Mapped[str] = mapped_column(String(128))
    market_maker_score: Mapped[float] = mapped_column(Float, default=0.0)
    latency_bot_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    lucky_wallet_score: Mapped[float] = mapped_column(Float, default=0.0)
    directional_score: Mapped[float] = mapped_column(Float, default=0.0)
    insufficient_sample_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    token_id: Mapped[str] = mapped_column(String(255), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(128), default="clob:/prices-history")
    fidelity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderbookSnapshot(Base):
    __tablename__ = "orderbook_snapshots"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    token_id: Mapped[str] = mapped_column(String(255), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    midpoint: Mapped[float | None] = mapped_column(Float, nullable=True)
    bids_json: Mapped[str] = mapped_column(Text, default="[]")
    asks_json: Mapped[str] = mapped_column(Text, default="[]")
    min_order_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    neg_risk: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class AlphaDecayResult(Base):
    __tablename__ = "alpha_decay_results"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    trade_id: Mapped[str] = mapped_column(String(255), index=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    trade_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    original_side: Mapped[str | None] = mapped_column(String(32), nullable=True)
    whale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    whale_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    delay_seconds: Mapped[int] = mapped_column(Integer, index=True)
    copy_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    copy_best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    copy_best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    copy_spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    simulated_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_degradation: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_available: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    eventual_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_rule: Mapped[str] = mapped_column(String(128))
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality: Mapped[str] = mapped_column(String(64), index=True)
    data_quality_rank: Mapped[int] = mapped_column(Integer, default=0)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class WalletCopyability(Base):
    __tablename__ = "wallet_copyability"

    wallet_address: Mapped[str] = mapped_column(String(128), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    historical_copy_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_7d_copy_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_30d_copy_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_90d_copy_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    copyability_trend: Mapped[str | None] = mapped_column(String(128), nullable=True)
    copyability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    copyability_reasons_json: Mapped[str] = mapped_column(Text, default="[]")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    mode: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    selected_wallets_json: Mapped[str] = mapped_column(Text, default="[]")
    date_split_json: Mapped[str] = mapped_column(Text, default="{}")
    train_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_trade_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_holding_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), ForeignKey("backtest_runs.run_id"), index=True)
    period_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    source_trade_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    side: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    entry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SkippedSignal(Base):
    __tablename__ = "skipped_signals"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("backtest_runs.run_id"), nullable=True, index=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    source_trade_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(128), index=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")


class SensitivityRun(Base):
    __tablename__ = "sensitivity_runs"

    sensitivity_run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    base_config_json: Mapped[str] = mapped_column(Text, default="{}")
    parameter_grid_json: Mapped[str] = mapped_column(Text, default="{}")
    selected_wallets_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")


class SensitivityResult(Base):
    __tablename__ = "sensitivity_results"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    sensitivity_run_id: Mapped[str] = mapped_column(String(255), ForeignKey("sensitivity_runs.sensitivity_run_id"), index=True)
    config_variant_json: Mapped[str] = mapped_column(Text, default="{}")
    copy_delay_seconds: Mapped[int] = mapped_column(Integer, index=True)
    max_entry_degradation: Mapped[float] = mapped_column(Float)
    max_spread: Mapped[float] = mapped_column(Float)
    position_size_usd: Mapped[float] = mapped_column(Float)
    max_market_exposure_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    skipped_signal_count: Mapped[int] = mapped_column(Integer, default=0)
    data_quality_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    train_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_flags_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class RawResponse(Base):
    __tablename__ = "raw_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(16), default="GET")
    url: Mapped[str] = mapped_column(Text)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)


def _resolve_sqlite_url(url: str) -> str:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url
    path_part = url.removeprefix(prefix)
    db_path = Path(path_part)
    if not db_path.is_absolute():
        db_path = project_root() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"{prefix}{db_path.as_posix()}"


def get_engine(database_url: str) -> Engine:
    resolved_url = _resolve_sqlite_url(database_url)
    engine = _ENGINES.get(resolved_url)
    if engine is not None:
        return engine
    connect_args = {"timeout": 60} if resolved_url.startswith("sqlite:///") else {}
    engine = create_engine(resolved_url, future=True, connect_args=connect_args)
    if resolved_url.startswith("sqlite:///"):
        configure_sqlite_engine(engine)
    _ENGINES[resolved_url] = engine
    return engine


def configure_sqlite_engine(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def get_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(database_url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(database_url: str) -> Iterator[Session]:
    factory = get_session_factory(database_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(config: dict[str, Any]) -> None:
    engine = get_engine(config_database_url(config))
    resolved_url = str(engine.url)
    if resolved_url in _INITIALIZED_DATABASE_URLS:
        return
    for attempt in range(3):
        try:
            Base.metadata.create_all(engine)
            ensure_sqlite_schema(engine)
            _INITIALIZED_DATABASE_URLS.add(resolved_url)
            return
        except OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def ensure_sqlite_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    additive_columns = {
        "reconstructed_positions": {
            "total_buy_usd": "FLOAT",
            "total_sell_usd": "FLOAT",
        },
        "reconstructed_position_events": {
            "usd_value": "FLOAT",
        },
        "wallet_metrics": {
            "max_capital_at_risk": "FLOAT",
            "return_on_max_capital_at_risk": "FLOAT",
            "max_exposure_method": "VARCHAR(128)",
            "max_exposure_confidence": "VARCHAR(128)",
            "average_capital_at_risk": "FLOAT",
            "return_on_average_capital_at_risk": "FLOAT",
            "average_exposure_method": "VARCHAR(128)",
            "average_exposure_confidence": "VARCHAR(128)",
        },
    }
    with engine.begin() as connection:
        for table_name, columns in additive_columns.items():
            existing = {row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")')}
            if not existing:
                continue
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    connection.exec_driver_sql(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_type}')


def cleanup_database(session: Session, scope: str) -> dict[str, int]:
    if scope == "classifications":
        return delete_models(session, [WalletClassification])
    if scope == "metrics":
        return delete_models(session, [WalletClassification, WalletMetrics])
    if scope == "ingested":
        return delete_models(session, INGESTED_AND_DOWNSTREAM_MODELS)
    if scope == "promoted":
        counts = delete_models(session, [*INGESTED_AND_DOWNSTREAM_MODELS, Wallet])
        result = session.execute(update(CandidateWallet).values(promoted=False))
        counts["candidate_wallets_promoted_reset"] = int(result.rowcount or 0)
        return counts
    if scope == "all":
        return delete_models(session, ALL_DATA_MODELS)
    raise ValueError(f"Unknown cleanup scope: {scope}")


def delete_models(session: Session, models: list[type[Base]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in models:
        result = session.execute(delete(model))
        counts[model.__tablename__] = int(result.rowcount or 0)
    return counts


INGESTED_AND_DOWNSTREAM_MODELS: list[type[Base]] = [
    SensitivityResult,
    SensitivityRun,
    SkippedSignal,
    BacktestTrade,
    BacktestRun,
    LifecycleCopyEvent,
    LifecycleCopyPosition,
    LifecycleCopyRun,
    WalletCopyability,
    AlphaDecayResult,
    WalletClassification,
    WalletMetrics,
    ReconstructedPositionEvent,
    ReconstructedPosition,
    WalletSnapshot,
    Activity,
    Trade,
]


ALL_DATA_MODELS: list[type[Base]] = [
    SensitivityResult,
    SensitivityRun,
    SkippedSignal,
    BacktestTrade,
    BacktestRun,
    LifecycleCopyEvent,
    LifecycleCopyPosition,
    LifecycleCopyRun,
    WalletCopyability,
    AlphaDecayResult,
    WalletClassification,
    WalletMetrics,
    ReconstructedPositionEvent,
    ReconstructedPosition,
    WalletSnapshot,
    Activity,
    Trade,
    OrderbookSnapshot,
    PriceHistory,
    Token,
    Market,
    Wallet,
    CandidateWallet,
    RawResponse,
]


def promote_top_candidates(session: Session, top: int) -> int:
    stmt = (
        select(CandidateWallet)
        .where(CandidateWallet.promoted.is_(False))
        .order_by(CandidateWallet.discovery_score.desc().nullslast(), CandidateWallet.source_count.desc())
        .limit(top)
    )
    promoted = 0
    for candidate in session.scalars(stmt):
        promote_candidate(session, candidate)
        promoted += 1
    return promoted


def promote_candidate_addresses(session: Session, wallet_addresses: list[str]) -> int:
    promoted = 0
    for wallet_address in wallet_addresses:
        candidate = session.get(CandidateWallet, wallet_address.lower())
        if candidate is None:
            candidate = session.get(CandidateWallet, wallet_address)
        if candidate is None:
            continue
        promote_candidate(session, candidate)
        promoted += 1
    return promoted


def promote_candidate(session: Session, candidate: CandidateWallet) -> None:
    candidate.promoted = True
    wallet = session.get(Wallet, candidate.wallet_address)
    if wallet is None:
        wallet = Wallet(
            wallet_address=candidate.wallet_address,
            username=candidate.username,
            source="candidate_discovery",
            first_seen_at=candidate.discovered_at,
            last_seen_at=candidate.last_seen_at,
            notes="Promoted from candidate discovery.",
        )
        session.add(wallet)
    else:
        wallet.username = wallet.username or candidate.username
        wallet.last_seen_at = candidate.last_seen_at or wallet.last_seen_at

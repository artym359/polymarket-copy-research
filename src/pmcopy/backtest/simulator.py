from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmcopy.backtest.lifecycle_copy import (
    LifecycleCopyConfig,
    lifecycle_config_from_values,
    run_lifecycle_copy_in_session,
)
from pmcopy.config import database_url
from pmcopy.db import (
    AlphaDecayResult,
    BacktestRun,
    BacktestTrade,
    LifecycleCopyPosition,
    Market,
    ReconstructedPosition,
    SkippedSignal,
    WalletClassification,
    WalletCopyability,
    init_db,
    json_dumps,
    json_loads,
    session_scope,
    utc_now,
)
from pmcopy.features.data_quality import quality_breakdown, quality_rank


@dataclass
class BacktestConfig:
    copy_mode: str
    mode: str
    selected_wallets: list[str] | None
    date_start: datetime | None
    date_end: datetime | None
    train_start: datetime | None
    train_end: datetime | None
    validation_start: datetime | None
    validation_end: datetime | None
    test_start: datetime | None
    test_end: datetime | None
    initial_capital: float
    position_size_usd: float
    copy_delay_seconds: int
    max_spread: float | None
    max_entry_degradation: float | None
    allowed_data_quality: list[str]
    max_market_exposure_usd: float | None
    max_wallet_exposure_usd: float | None
    max_category_exposure_usd: float | None
    max_daily_loss_usd: float | None
    duplicate_signal_window_seconds: int
    exit_rule: str
    sizing_mode: str
    copy_ratio: float
    max_position_budget_usd: float
    min_trade_usd: float
    execute_small_trades: bool
    allow_position_cap_partial_fill: bool
    entry_delay_seconds: int
    exit_delay_seconds: int
    include_categories: list[str]
    exclude_categories: list[str]
    skip_likely_market_makers: bool
    skip_likely_latency_bots: bool
    skip_lucky_wallets: bool
    skip_insufficient_sample: bool
    min_copyability_score: float | None
    diagnostics_mode: bool = False
    walk_forward_lookback_days: int = 60
    walk_forward_test_window_days: int = 7
    walk_forward_rebalance_frequency_days: int = 7
    walk_forward_min_trades_in_lookback: int = 30
    walk_forward_selection_metric: str = "copyability_score"
    walk_forward_top_wallets: int = 10


@dataclass
class OpenPosition:
    wallet_address: str
    market_id: str | None
    token_id: str | None
    category: str
    size_usd: float
    exit_time: datetime
    net_pnl: float
    trade_dict: dict[str, Any]


def backtest_config_from_values(
    config: dict[str, Any],
    *,
    copy_mode: str | None = None,
    mode: str | None = None,
    selected_wallets: list[str] | None = None,
    date_start: Any = None,
    date_end: Any = None,
    train_start: Any = None,
    train_end: Any = None,
    validation_start: Any = None,
    validation_end: Any = None,
    test_start: Any = None,
    test_end: Any = None,
    initial_capital: float | None = None,
    position_size_usd: float | None = None,
    copy_delay_seconds: int | None = None,
    max_spread: float | None = None,
    max_entry_degradation: float | None = None,
    allowed_data_quality: list[str] | None = None,
    max_market_exposure_usd: float | None = None,
    max_wallet_exposure_usd: float | None = None,
    max_category_exposure_usd: float | None = None,
    max_daily_loss_usd: float | None = None,
    duplicate_signal_window_seconds: int | None = None,
    exit_rule: str | None = None,
    sizing_mode: str | None = None,
    copy_ratio: float | None = None,
    max_position_budget_usd: float | None = None,
    min_trade_usd: float | None = None,
    execute_small_trades: bool | None = None,
    allow_position_cap_partial_fill: bool | None = None,
    entry_delay_seconds: int | None = None,
    exit_delay_seconds: int | None = None,
    include_categories: list[str] | None = None,
    exclude_categories: list[str] | None = None,
    skip_likely_market_makers: bool | None = None,
    skip_likely_latency_bots: bool | None = None,
    skip_lucky_wallets: bool | None = None,
    skip_insufficient_sample: bool | None = None,
    min_copyability_score: float | None = None,
) -> BacktestConfig:
    cfg = config.get("backtest", {})
    sizing = config.get("sizing", {})
    splits = config.get("date_splits", {})
    walk = config.get("walk_forward", {})
    wallet_filters = config.get("wallet_filters", {})
    return BacktestConfig(
        copy_mode=copy_mode or str(cfg.get("copy_mode", "diagnostic_trade_level")),
        mode=mode or str(cfg.get("mode", "in_sample")),
        selected_wallets=normalize_wallets(selected_wallets),
        date_start=parse_datetime_start(date_start),
        date_end=parse_datetime_end(date_end),
        train_start=parse_datetime_start(train_start or splits.get("train_start")),
        train_end=parse_datetime_end(train_end or splits.get("train_end")),
        validation_start=parse_datetime_start(validation_start or splits.get("validation_start")),
        validation_end=parse_datetime_end(validation_end or splits.get("validation_end")),
        test_start=parse_datetime_start(test_start or splits.get("test_start")),
        test_end=parse_datetime_end(test_end or splits.get("test_end")),
        initial_capital=float(initial_capital if initial_capital is not None else cfg.get("initial_capital", 100)),
        position_size_usd=float(position_size_usd if position_size_usd is not None else cfg.get("position_size_usd", 2)),
        copy_delay_seconds=int(copy_delay_seconds if copy_delay_seconds is not None else cfg.get("copy_delay_seconds", 60)),
        max_spread=max_spread if max_spread is not None else cfg.get("max_spread"),
        max_entry_degradation=(
            max_entry_degradation if max_entry_degradation is not None else cfg.get("max_entry_degradation")
        ),
        allowed_data_quality=allowed_data_quality
        or list(cfg.get("allowed_data_quality_levels", config.get("data_quality", {}).get("default_allowed_levels", ["exact_orderbook", "price_history_proxy"]))),
        max_market_exposure_usd=(
            max_market_exposure_usd if max_market_exposure_usd is not None else cfg.get("max_market_exposure_usd")
        ),
        max_wallet_exposure_usd=(
            max_wallet_exposure_usd if max_wallet_exposure_usd is not None else cfg.get("max_wallet_exposure_usd")
        ),
        max_category_exposure_usd=(
            max_category_exposure_usd if max_category_exposure_usd is not None else cfg.get("max_category_exposure_usd")
        ),
        max_daily_loss_usd=max_daily_loss_usd if max_daily_loss_usd is not None else cfg.get("max_daily_loss_usd"),
        duplicate_signal_window_seconds=int(
            duplicate_signal_window_seconds
            if duplicate_signal_window_seconds is not None
            else cfg.get("duplicate_signal_window_seconds", 600)
        ),
        exit_rule=exit_rule or str(cfg.get("exit_rule", "hold_to_resolution")),
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
        entry_delay_seconds=int(entry_delay_seconds if entry_delay_seconds is not None else cfg.get("entry_delay_seconds", cfg.get("copy_delay_seconds", 60))),
        exit_delay_seconds=int(
            exit_delay_seconds
            if exit_delay_seconds is not None
            else cfg.get("exit_delay_seconds", entry_delay_seconds if entry_delay_seconds is not None else cfg.get("copy_delay_seconds", 60))
        ),
        include_categories=include_categories if include_categories is not None else list(cfg.get("include_categories", [])),
        exclude_categories=exclude_categories if exclude_categories is not None else list(cfg.get("exclude_categories", [])),
        skip_likely_market_makers=bool(
            skip_likely_market_makers
            if skip_likely_market_makers is not None
            else cfg.get("skip_likely_market_makers", True)
        ),
        skip_likely_latency_bots=bool(
            skip_likely_latency_bots
            if skip_likely_latency_bots is not None
            else cfg.get("skip_likely_latency_bots", True)
        ),
        skip_lucky_wallets=bool(
            skip_lucky_wallets if skip_lucky_wallets is not None else cfg.get("skip_lucky_wallets", True)
        ),
        skip_insufficient_sample=bool(
            skip_insufficient_sample
            if skip_insufficient_sample is not None
            else cfg.get("skip_insufficient_sample", wallet_filters.get("exclude_insufficient_sample", False))
        ),
        min_copyability_score=(
            min_copyability_score if min_copyability_score is not None else cfg.get("min_copyability_score", 0)
        ),
        diagnostics_mode=bool(cfg.get("diagnostics_mode", False)),
        walk_forward_lookback_days=int(walk.get("lookback_days", 60)),
        walk_forward_test_window_days=int(walk.get("test_window_days", 7)),
        walk_forward_rebalance_frequency_days=int(walk.get("rebalance_frequency_days", 7)),
        walk_forward_min_trades_in_lookback=int(walk.get("min_trades_in_lookback", 30)),
        walk_forward_selection_metric=str(walk.get("selection_metric", "copyability_score")),
        walk_forward_top_wallets=int(walk.get("top_wallets", 10)),
    )


def run_backtest(config: dict[str, Any], backtest_config: BacktestConfig | None = None) -> dict[str, Any]:
    init_db(config)
    bt_config = backtest_config or backtest_config_from_values(config)
    run_id = stable_id("backtest", utc_now().isoformat(), bt_config.mode, bt_config.copy_mode, bt_config.copy_delay_seconds, bt_config.exit_rule)
    with session_scope(database_url(config)) as session:
        run_row = BacktestRun(
            run_id=run_id,
            created_at=utc_now(),
            mode=bt_config.mode,
            config_json=json_dumps(config_to_json(bt_config)),
            selected_wallets_json="[]",
            date_split_json=json_dumps(date_split_json(bt_config)),
        )
        session.add(run_row)
        session.flush()

        if bt_config.copy_mode == "reconstructed_wallet_lifecycle":
            result = run_lifecycle_backtest_mode(session, config, run_id, bt_config)
        elif bt_config.mode == "split":
            result = run_split_mode(session, run_id, bt_config)
        elif bt_config.mode == "walk_forward":
            result = run_walk_forward_mode(session, run_id, bt_config)
        else:
            selected = resolve_selected_wallets(session, bt_config)
            period = simulate_period(
                session,
                run_id,
                bt_config,
                selected,
                bt_config.date_start,
                bt_config.date_end,
                "in_sample",
                bt_config.initial_capital,
            )
            result = result_from_periods(bt_config, selected, {"in_sample": period})

        result.setdefault("warnings", [])
        result["warnings"].extend(follow_wallet_exit_warnings(session, bt_config, result.get("selected_wallets", [])))
        update_backtest_run(run_row, result)
        return result


def run_lifecycle_backtest_mode(session: Session, config: dict[str, Any], run_id: str, bt_config: BacktestConfig) -> dict[str, Any]:
    selected = resolve_selected_wallets(session, bt_config)
    if bt_config.mode == "split":
        split_ranges = {
            "train": (bt_config.train_start, bt_config.train_end),
            "validation": (bt_config.validation_start, bt_config.validation_end),
            "test": (bt_config.test_start, bt_config.test_end),
        }
        periods = {
            label: simulate_lifecycle_period(session, config, run_id, bt_config, selected, start, end, label, bt_config.initial_capital)
            for label, (start, end) in split_ranges.items()
            if start is not None or end is not None
        }
        if not periods:
            periods["in_sample"] = simulate_lifecycle_period(
                session,
                config,
                run_id,
                bt_config,
                selected,
                bt_config.date_start,
                bt_config.date_end,
                "in_sample",
                bt_config.initial_capital,
            )
        result = result_from_periods(bt_config, selected, periods)
        result["warnings"] = split_warnings(periods)
    elif bt_config.mode == "walk_forward":
        period = simulate_lifecycle_period(
            session,
            config,
            run_id,
            bt_config,
            selected,
            bt_config.date_start,
            bt_config.date_end,
            "in_sample",
            bt_config.initial_capital,
        )
        result = result_from_periods(bt_config, selected, {"in_sample": period})
        result["warnings"] = ["walk_forward wallet reselection is not implemented for reconstructed lifecycle mode; ran in_sample lifecycle simulation"]
    else:
        period = simulate_lifecycle_period(
            session,
            config,
            run_id,
            bt_config,
            selected,
            bt_config.date_start,
            bt_config.date_end,
            "in_sample",
            bt_config.initial_capital,
        )
        result = result_from_periods(bt_config, selected, {"in_sample": period})
    result["copy_mode"] = bt_config.copy_mode
    return result


def simulate_lifecycle_period(
    session: Session,
    config: dict[str, Any],
    run_id: str,
    bt_config: BacktestConfig,
    selected_wallets: list[str],
    date_start: datetime | None,
    date_end: datetime | None,
    period_label: str,
    initial_capital: float,
) -> dict[str, Any]:
    lifecycle_config = lifecycle_config_from_backtest(config, bt_config, date_start, date_end)
    lifecycle_result = run_lifecycle_copy_in_session(
        session,
        config,
        run_id,
        lifecycle_config,
        selected_wallets,
        period_label=period_label,
        reset_run=False,
    )
    accepted = []
    skipped = Counter(lifecycle_result.get("skipped_signal_reasons", {}))
    data_qualities = []
    categories = market_category_map(session)
    for row in closed_lifecycle_positions(session, run_id, period_label):
        trade = store_lifecycle_trade(session, run_id, period_label, row, categories.get(row.market_id or "", "unknown"))
        accepted.append(trade)
        raw = json_loads(row.raw_json, {}) or {}
        if raw.get("data_quality"):
            data_qualities.append(raw["data_quality"])
    for row in non_closed_lifecycle_positions(session, run_id, period_label):
        if row.skip_reason:
            skipped[row.skip_reason] += 1
    equity_curve = equity_curve_from_trades(accepted, initial_capital)
    metrics = metrics_from_trades(accepted, skipped, data_qualities, equity_curve, initial_capital)
    metrics["closed_copied_positions"] = lifecycle_result.get("closed_positions", 0)
    metrics["open_copied_positions"] = lifecycle_result.get("open_positions", 0)
    metrics["skipped_positions"] = lifecycle_result.get("skipped_positions", 0)
    metrics["cap_hit_count"] = lifecycle_result.get("cap_hit_count", 0)
    metrics["below_min_trade_count"] = lifecycle_result.get("below_min_trade_count", 0)
    metrics["lifecycle_data_quality_summary"] = lifecycle_result.get("data_quality_summary", {})
    return {
        "period_label": period_label,
        "selected_wallets": selected_wallets,
        "candidate_count": lifecycle_result.get("candidate_count", 0),
        "trades": accepted,
        "metrics": metrics,
    }


def lifecycle_config_from_backtest(
    config: dict[str, Any],
    bt_config: BacktestConfig,
    date_start: datetime | None,
    date_end: datetime | None,
) -> LifecycleCopyConfig:
    return lifecycle_config_from_values(
        config,
        copy_mode=bt_config.copy_mode,
        sizing_mode=bt_config.sizing_mode,
        copy_ratio=bt_config.copy_ratio,
        max_position_budget_usd=bt_config.max_position_budget_usd,
        min_trade_usd=bt_config.min_trade_usd,
        execute_small_trades=bt_config.execute_small_trades,
        allow_position_cap_partial_fill=bt_config.allow_position_cap_partial_fill,
        entry_delay_seconds=bt_config.entry_delay_seconds,
        exit_delay_seconds=bt_config.exit_delay_seconds,
        allowed_data_quality=bt_config.allowed_data_quality,
        date_start=date_start,
        date_end=date_end,
    )


def run_split_mode(session: Session, run_id: str, bt_config: BacktestConfig) -> dict[str, Any]:
    selected = resolve_selected_wallets(session, bt_config)
    periods: dict[str, dict[str, Any]] = {}
    split_ranges = {
        "train": (bt_config.train_start, bt_config.train_end),
        "validation": (bt_config.validation_start, bt_config.validation_end),
        "test": (bt_config.test_start, bt_config.test_end),
    }
    for label, (start, end) in split_ranges.items():
        if start is None and end is None:
            continue
        periods[label] = simulate_period(session, run_id, bt_config, selected, start, end, label, bt_config.initial_capital)
    if not periods:
        periods["in_sample"] = simulate_period(
            session,
            run_id,
            bt_config,
            selected,
            bt_config.date_start,
            bt_config.date_end,
            "in_sample",
            bt_config.initial_capital,
        )
    result = result_from_periods(bt_config, selected, periods)
    result["warnings"] = split_warnings(periods)
    return result


def run_walk_forward_mode(session: Session, run_id: str, bt_config: BacktestConfig) -> dict[str, Any]:
    start, end = infer_walk_forward_range(session, bt_config)
    if start is None or end is None or start >= end:
        metrics = empty_metrics(bt_config.initial_capital)
        return {
            "run_id": run_id,
            "mode": "walk_forward",
            "selected_wallets": [],
            "metrics": metrics,
            "periods": {},
            "walk_forward_windows": [],
            "candidate_count": 0,
            "warnings": ["walk_forward range has no alpha-decay data"],
        }

    periods: dict[str, dict[str, Any]] = {}
    windows: list[dict[str, Any]] = []
    unique_wallets: set[str] = set()
    current_capital = bt_config.initial_capital
    decision_time = start
    index = 1
    while decision_time < end:
        window_end = min(decision_time + timedelta(days=bt_config.walk_forward_test_window_days), end)
        selected = select_walk_forward_wallets(session, bt_config, decision_time)
        unique_wallets.update(selected)
        label = f"wf_{index:03d}_{decision_time.date().isoformat()}"
        period = simulate_period(session, run_id, bt_config, selected, decision_time, window_end, label, current_capital)
        periods[label] = period
        current_capital += float(period["metrics"]["total_pnl"])
        windows.append(
            {
                "period_label": label,
                "decision_time": decision_time.isoformat(),
                "window_end": window_end.isoformat(),
                "selected_wallets": selected,
                "total_pnl": period["metrics"]["total_pnl"],
                "trade_count": period["metrics"]["trade_count"],
                "skipped_signal_reasons": period["metrics"]["skipped_signal_reasons"],
                "data_quality_summary": period["metrics"]["data_quality_summary"],
            }
        )
        decision_time += timedelta(days=bt_config.walk_forward_rebalance_frequency_days)
        index += 1

    result = result_from_periods(bt_config, sorted(unique_wallets), periods)
    result["walk_forward_windows"] = windows
    return result


def simulate_period(
    session: Session,
    run_id: str,
    bt_config: BacktestConfig,
    selected_wallets: list[str],
    date_start: datetime | None,
    date_end: datetime | None,
    period_label: str,
    initial_capital: float,
) -> dict[str, Any]:
    rows = load_candidate_alpha_rows(session, bt_config, selected_wallets, date_start, date_end)
    categories = market_category_map(session)
    classifications = classification_map(session)
    equity = float(initial_capital)
    equity_curve: list[dict[str, Any]] = [{"timestamp": (date_start or utc_now()).isoformat(), "equity": round_float(equity)}]
    open_positions: list[OpenPosition] = []
    accepted: list[dict[str, Any]] = []
    skipped = Counter()
    data_qualities: list[str] = []
    recent_signals: list[tuple[datetime, str | None, str | None, str | None]] = []
    daily_pnl: dict[date, float] = defaultdict(float)

    for row in rows:
        entry_time = to_utc(row.copy_time or row.trade_time)
        release_closed_positions(open_positions, entry_time, daily_pnl, equity_curve)
        equity = float(equity_curve[-1]["equity"])
        category = categories.get(row.market_id or "", "unknown")
        candidate = candidate_from_alpha(row, bt_config, category)

        reason = classification_skip_reason(classifications.get(row.wallet_address), bt_config)
        if reason:
            store_skip(session, run_id, period_label, row, reason, {"class_label": class_label(classifications.get(row.wallet_address))})
            skipped[reason] += 1
            continue

        reason = category_skip_reason(category, bt_config)
        if reason:
            store_skip(session, run_id, period_label, row, reason, {"category": category})
            skipped[reason] += 1
            continue

        reason = execution_skip_reason(row, candidate, bt_config)
        if reason:
            store_skip(session, run_id, period_label, row, reason, {"category": category})
            skipped[reason] += 1
            continue

        reason = risk_skip_reason(open_positions, candidate, equity, daily_pnl, bt_config)
        if reason:
            store_skip(session, run_id, period_label, row, reason, exposure_details(open_positions, candidate, equity))
            skipped[reason] += 1
            continue

        reason = duplicate_skip_reason(recent_signals, row, entry_time, bt_config)
        if reason:
            store_skip(session, run_id, period_label, row, reason, {"duplicate_window_seconds": bt_config.duplicate_signal_window_seconds})
            skipped[reason] += 1
            continue

        trade = store_trade(session, run_id, period_label, row, candidate)
        accepted.append(trade)
        data_qualities.append(row.data_quality)
        open_positions.append(
            OpenPosition(
                wallet_address=row.wallet_address,
                market_id=row.market_id,
                token_id=row.token_id,
                category=category,
                size_usd=candidate["size_usd"],
                exit_time=candidate["exit_time"],
                net_pnl=candidate["net_pnl"],
                trade_dict=trade,
            )
        )
        recent_signals.append((entry_time, row.market_id, row.token_id, normalized_side(row.original_side)))

    release_all_positions(open_positions, daily_pnl, equity_curve)
    metrics = metrics_from_trades(accepted, skipped, data_qualities, equity_curve, initial_capital)
    return {
        "period_label": period_label,
        "selected_wallets": selected_wallets,
        "candidate_count": len(rows),
        "trades": accepted,
        "metrics": metrics,
    }

def release_closed_positions(
    open_positions: list[OpenPosition],
    entry_time: datetime,
    daily_pnl: dict[date, float],
    equity_curve: list[dict[str, Any]],
) -> None:
    closed = sorted([position for position in open_positions if position.exit_time <= entry_time], key=lambda item: item.exit_time)
    if not closed:
        return
    remaining = [position for position in open_positions if position.exit_time > entry_time]
    equity = float(equity_curve[-1]["equity"])
    for position in closed:
        equity += position.net_pnl
        daily_pnl[position.exit_time.date()] += position.net_pnl
        equity_curve.append({"timestamp": position.exit_time.isoformat(), "equity": round_float(equity)})
    open_positions[:] = remaining


def release_all_positions(
    open_positions: list[OpenPosition],
    daily_pnl: dict[date, float],
    equity_curve: list[dict[str, Any]],
) -> None:
    equity = float(equity_curve[-1]["equity"])
    for position in sorted(open_positions, key=lambda item: item.exit_time):
        equity += position.net_pnl
        daily_pnl[position.exit_time.date()] += position.net_pnl
        equity_curve.append({"timestamp": position.exit_time.isoformat(), "equity": round_float(equity)})
    open_positions.clear()


def candidate_from_alpha(row: AlphaDecayResult, bt_config: BacktestConfig, category: str) -> dict[str, Any]:
    raw = json_loads(row.raw_json, {}) or {}
    alpha_size = parse_float(raw.get("position_size_usd")) or bt_config.position_size_usd
    scale = bt_config.position_size_usd / alpha_size if alpha_size > 0 else 1.0
    entry_time = to_utc(row.copy_time or row.trade_time)
    exit_time = infer_exit_time(row)
    return {
        "wallet_address": row.wallet_address,
        "market_id": row.market_id,
        "token_id": row.token_id,
        "category": category,
        "side": normalized_side(row.original_side),
        "signal_time": to_utc(row.trade_time or row.copy_time),
        "entry_time": entry_time,
        "entry_price": row.simulated_entry_price,
        "size_usd": bt_config.position_size_usd,
        "exit_time": exit_time,
        "exit_price": row.eventual_exit_price,
        "gross_pnl": scale_value(row.gross_pnl, scale),
        "fee": scale_value(row.estimated_fee, scale),
        "slippage": scale_value(row.estimated_slippage, scale),
        "net_pnl": scale_value(row.net_pnl, scale) or 0.0,
        "data_quality": row.data_quality,
    }


def load_candidate_alpha_rows(
    session: Session,
    bt_config: BacktestConfig,
    selected_wallets: list[str],
    date_start: datetime | None,
    date_end: datetime | None,
) -> list[AlphaDecayResult]:
    if not selected_wallets:
        return []
    stmt = (
        select(AlphaDecayResult)
        .where(AlphaDecayResult.wallet_address.in_(selected_wallets))
        .where(AlphaDecayResult.delay_seconds == bt_config.copy_delay_seconds)
        .where(AlphaDecayResult.exit_rule == bt_config.exit_rule)
        .where(AlphaDecayResult.simulated_entry_price.is_not(None))
        .where(AlphaDecayResult.eventual_exit_price.is_not(None))
        .where(AlphaDecayResult.net_pnl.is_not(None))
    )
    if not bt_config.diagnostics_mode:
        stmt = stmt.where(AlphaDecayResult.skip_reason.is_(None)).where(AlphaDecayResult.data_quality != "insufficient_data")
    if date_start is not None:
        stmt = stmt.where(AlphaDecayResult.trade_time >= date_start)
    if date_end is not None:
        stmt = stmt.where(AlphaDecayResult.trade_time <= date_end)
    rows = list(session.scalars(stmt.order_by(AlphaDecayResult.trade_time, AlphaDecayResult.copy_time, AlphaDecayResult.id)))
    return dedupe_alpha_rows(rows)


def dedupe_alpha_rows(rows: list[AlphaDecayResult]) -> list[AlphaDecayResult]:
    grouped: dict[tuple[Any, ...], list[AlphaDecayResult]] = defaultdict(list)
    for row in rows:
        grouped[(row.wallet_address, row.trade_id, row.token_id, row.market_id, row.delay_seconds, row.exit_rule)].append(row)
    selected = []
    for group in grouped.values():
        selected.append(
            sorted(
                group,
                key=lambda row: (
                    1 if row.skip_reason else 0,
                    0 if row.simulated_entry_price is not None else 1,
                    0 if row.net_pnl is not None else 1,
                    -quality_rank(row.data_quality),
                    row.id,
                ),
            )[0]
        )
    return sorted(selected, key=lambda row: (to_utc(row.trade_time or row.copy_time), to_utc(row.copy_time or row.trade_time), row.id))


def resolve_selected_wallets(session: Session, bt_config: BacktestConfig) -> list[str]:
    if bt_config.selected_wallets:
        return bt_config.selected_wallets
    if bt_config.copy_mode == "reconstructed_wallet_lifecycle":
        wallets = normalize_wallets(list(session.scalars(select(ReconstructedPosition.wallet_address).distinct()))) or []
        if wallets:
            return wallets
    stmt = select(WalletCopyability).order_by(WalletCopyability.copyability_score.desc().nullslast())
    rows = list(session.scalars(stmt))
    if rows:
        threshold = bt_config.min_copyability_score
        wallets = [
            row.wallet_address
            for row in rows
            if row.copyability_score is not None and (threshold is None or row.copyability_score >= threshold)
        ]
        if wallets:
            return normalize_wallets(wallets) or []
    return normalize_wallets(list(session.scalars(select(AlphaDecayResult.wallet_address).distinct()))) or []


def classification_skip_reason(classification: WalletClassification | None, bt_config: BacktestConfig) -> str | None:
    if classification is None:
        return None
    if bt_config.skip_likely_market_makers and classification.class_label == "likely_market_maker":
        return "likely_market_maker"
    if bt_config.skip_likely_latency_bots and classification.class_label == "likely_latency_bot":
        return "likely_latency_bot"
    if bt_config.skip_lucky_wallets and classification.class_label == "lucky_wallet":
        return "lucky_wallet"
    if bt_config.skip_insufficient_sample and classification.insufficient_sample_flag:
        return "insufficient_sample"
    return None


def category_skip_reason(category: str, bt_config: BacktestConfig) -> str | None:
    if bt_config.include_categories and category not in bt_config.include_categories:
        return "category_not_included"
    if bt_config.exclude_categories and category in bt_config.exclude_categories:
        return "category_excluded"
    return None


def execution_skip_reason(row: AlphaDecayResult, candidate: dict[str, Any], bt_config: BacktestConfig) -> str | None:
    if row.data_quality not in set(bt_config.allowed_data_quality):
        return "data_quality_not_allowed"
    if row.skip_reason and not bt_config.diagnostics_mode:
        return row.skip_reason
    if row.simulated_entry_price is None:
        return "missing_entry_price"
    if row.eventual_exit_price is None:
        return "missing_exit_price"
    if row.net_pnl is None:
        return "missing_net_pnl"
    if candidate["exit_time"] is None:
        return "missing_exit_time"
    if bt_config.max_spread is not None and row.copy_spread is not None and row.copy_spread > bt_config.max_spread:
        return "max_spread_exceeded"
    if (
        bt_config.max_entry_degradation is not None
        and row.entry_degradation is not None
        and row.entry_degradation > bt_config.max_entry_degradation
    ):
        return "max_entry_degradation_exceeded"
    if row.data_quality == "exact_orderbook" and row.liquidity_available is not None and row.liquidity_available < bt_config.position_size_usd:
        return "insufficient_liquidity"
    return None


def risk_skip_reason(
    open_positions: list[OpenPosition],
    candidate: dict[str, Any],
    equity: float,
    daily_pnl: dict[date, float],
    bt_config: BacktestConfig,
) -> str | None:
    size = float(candidate["size_usd"])
    if bt_config.max_daily_loss_usd is not None and daily_pnl[candidate["entry_time"].date()] <= -abs(bt_config.max_daily_loss_usd):
        return "daily_loss_limit_reached"
    if total_exposure(open_positions) + size > max(equity, 0.0) + 1e-9:
        return "insufficient_capital"
    if bt_config.max_wallet_exposure_usd is not None and exposure_by(open_positions, "wallet_address", candidate["wallet_address"]) + size > bt_config.max_wallet_exposure_usd + 1e-9:
        return "max_wallet_exposure_exceeded"
    if bt_config.max_market_exposure_usd is not None and exposure_by(open_positions, "market_id", candidate["market_id"]) + size > bt_config.max_market_exposure_usd + 1e-9:
        return "max_market_exposure_exceeded"
    if bt_config.max_category_exposure_usd is not None and exposure_by(open_positions, "category", candidate["category"]) + size > bt_config.max_category_exposure_usd + 1e-9:
        return "max_category_exposure_exceeded"
    return None


def duplicate_skip_reason(
    recent_signals: list[tuple[datetime, str | None, str | None, str | None]],
    row: AlphaDecayResult,
    entry_time: datetime,
    bt_config: BacktestConfig,
) -> str | None:
    window = bt_config.duplicate_signal_window_seconds
    if window <= 0:
        return None
    side = normalized_side(row.original_side)
    for seen_time, seen_market, seen_token, seen_side in recent_signals:
        if abs((entry_time - seen_time).total_seconds()) > window:
            continue
        if row.market_id == seen_market and (row.token_id == seen_token or side == seen_side):
            return "duplicate_signal"
    return None


def store_trade(
    session: Session,
    run_id: str,
    period_label: str,
    row: AlphaDecayResult,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    trade_id = stable_id("backtest_trade", run_id, period_label, row.id)
    db_row = BacktestTrade(
        id=trade_id,
        run_id=run_id,
        period_label=period_label,
        wallet_address=row.wallet_address,
        source_trade_id=row.trade_id,
        market_id=row.market_id,
        token_id=row.token_id,
        side=candidate["side"],
        signal_time=candidate["signal_time"],
        entry_time=candidate["entry_time"],
        entry_price=candidate["entry_price"],
        size_usd=candidate["size_usd"],
        exit_time=candidate["exit_time"],
        exit_price=candidate["exit_price"],
        gross_pnl=candidate["gross_pnl"],
        fee=candidate["fee"],
        slippage=candidate["slippage"],
        net_pnl=candidate["net_pnl"],
        data_quality=row.data_quality,
        skip_reason=None,
    )
    session.add(db_row)
    return {
        "id": trade_id,
        "period_label": period_label,
        "wallet_address": row.wallet_address,
        "source_trade_id": row.trade_id,
        "market_id": row.market_id,
        "token_id": row.token_id,
        "category": candidate["category"],
        "side": candidate["side"],
        "signal_time": candidate["signal_time"].isoformat() if candidate["signal_time"] else None,
        "entry_time": candidate["entry_time"].isoformat() if candidate["entry_time"] else None,
        "entry_price": candidate["entry_price"],
        "size_usd": candidate["size_usd"],
        "exit_time": candidate["exit_time"].isoformat() if candidate["exit_time"] else None,
        "exit_price": candidate["exit_price"],
        "gross_pnl": candidate["gross_pnl"],
        "fee": candidate["fee"],
        "slippage": candidate["slippage"],
        "net_pnl": candidate["net_pnl"],
        "data_quality": row.data_quality,
    }


def closed_lifecycle_positions(session: Session, run_id: str, period_label: str) -> list[LifecycleCopyPosition]:
    rows = list(
        session.scalars(
            select(LifecycleCopyPosition)
            .where(LifecycleCopyPosition.run_id == run_id)
            .where(LifecycleCopyPosition.status == "closed")
            .where(LifecycleCopyPosition.skip_reason.is_(None))
            .order_by(LifecycleCopyPosition.opened_at, LifecycleCopyPosition.closed_at, LifecycleCopyPosition.id)
        )
    )
    return [row for row in rows if lifecycle_period_label(row) == period_label]


def non_closed_lifecycle_positions(session: Session, run_id: str, period_label: str) -> list[LifecycleCopyPosition]:
    rows = list(
        session.scalars(
            select(LifecycleCopyPosition)
            .where(LifecycleCopyPosition.run_id == run_id)
            .where((LifecycleCopyPosition.status != "closed") | LifecycleCopyPosition.skip_reason.is_not(None))
            .order_by(LifecycleCopyPosition.opened_at, LifecycleCopyPosition.id)
        )
    )
    return [row for row in rows if lifecycle_period_label(row) == period_label]


def lifecycle_period_label(row: LifecycleCopyPosition) -> str | None:
    raw = json_loads(row.raw_json, {}) or {}
    return raw.get("period_label") if isinstance(raw, dict) else None


def store_lifecycle_trade(
    session: Session,
    run_id: str,
    period_label: str,
    row: LifecycleCopyPosition,
    category: str,
) -> dict[str, Any]:
    raw = json_loads(row.raw_json, {}) or {}
    trade_id = stable_id("backtest_lifecycle_trade", run_id, period_label, row.id)
    net_pnl = row.copied_realized_pnl or 0.0
    gross_pnl = parse_float(raw.get("gross_pnl"))
    fee = parse_float(raw.get("fees"))
    db_row = BacktestTrade(
        id=trade_id,
        run_id=run_id,
        period_label=period_label,
        wallet_address=row.wallet_address,
        source_trade_id=row.position_id,
        market_id=row.market_id,
        token_id=row.token_id,
        side="BUY",
        signal_time=row.opened_at,
        entry_time=row.opened_at,
        entry_price=None,
        size_usd=row.copied_total_buy_usd,
        exit_time=row.closed_at,
        exit_price=None,
        gross_pnl=round_float(gross_pnl) if gross_pnl is not None else None,
        fee=round_float(fee) if fee is not None else None,
        slippage=None,
        net_pnl=net_pnl,
        data_quality=raw.get("data_quality"),
        skip_reason=None,
    )
    session.merge(db_row)
    return {
        "id": trade_id,
        "period_label": period_label,
        "wallet_address": row.wallet_address,
        "source_trade_id": row.position_id,
        "market_id": row.market_id,
        "token_id": row.token_id,
        "category": category,
        "side": "BUY",
        "signal_time": row.opened_at.isoformat() if row.opened_at else None,
        "entry_time": row.opened_at.isoformat() if row.opened_at else None,
        "entry_price": None,
        "size_usd": row.copied_total_buy_usd,
        "exit_time": row.closed_at.isoformat() if row.closed_at else None,
        "exit_price": None,
        "gross_pnl": round_float(gross_pnl) if gross_pnl is not None else None,
        "fee": round_float(fee) if fee is not None else None,
        "slippage": None,
        "net_pnl": net_pnl,
        "data_quality": raw.get("data_quality"),
        "cap_hit_count": raw.get("cap_hit_count", 0),
        "below_min_trade_count": raw.get("below_min_trade_count", 0),
    }


def store_skip(
    session: Session,
    run_id: str,
    period_label: str,
    row: AlphaDecayResult,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    skip_id = stable_id("skipped_signal", run_id, period_label, row.id, reason)
    session.add(
        SkippedSignal(
            id=skip_id,
            run_id=run_id,
            wallet_address=row.wallet_address,
            source_trade_id=row.trade_id,
            market_id=row.market_id,
            token_id=row.token_id,
            timestamp=row.trade_time,
            reason=reason,
            details_json=json_dumps(details or {}),
        )
    )


def metrics_from_trades(
    trades: list[dict[str, Any]],
    skipped: Counter,
    data_qualities: list[str],
    equity_curve: list[dict[str, Any]],
    initial_capital: float,
) -> dict[str, Any]:
    pnl_values = [float(trade["net_pnl"] or 0.0) for trade in trades]
    total_pnl = sum(pnl_values)
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    avg_holding = average_holding_seconds(trades)
    metrics = {
        "total_pnl": round_float(total_pnl),
        "roi": round_float(total_pnl / initial_capital if initial_capital else 0.0),
        "max_drawdown": round_float(max_drawdown(equity_curve)),
        "trade_count": len(trades),
        "win_rate": round_float(len(wins) / len(trades) if trades else 0.0),
        "profit_factor": round_float(sum(wins) / abs(sum(losses))) if losses else None,
        "avg_trade_size": round_float(sum(float(trade["size_usd"] or 0.0) for trade in trades) / len(trades)) if trades else None,
        "avg_holding_time": round_float(avg_holding) if avg_holding is not None else None,
        "best_trade": round_float(max(pnl_values)) if pnl_values else None,
        "worst_trade": round_float(min(pnl_values)) if pnl_values else None,
        "pnl_by_wallet": grouped_pnl(trades, "wallet_address"),
        "pnl_by_category": grouped_pnl(trades, "category"),
        "pnl_by_market": grouped_pnl(trades, "market_id"),
        "skipped_signal_reasons": dict(sorted(skipped.items())),
        "data_quality_summary": data_quality_summary(data_qualities),
        "equity_curve": equity_curve,
    }
    return metrics


def result_from_periods(bt_config: BacktestConfig, selected_wallets: list[str], periods: dict[str, dict[str, Any]]) -> dict[str, Any]:
    all_trades = [trade for period in periods.values() for trade in period["trades"]]
    all_skips = Counter()
    all_quality = []
    extra_counts = Counter()
    for period in periods.values():
        metrics = period["metrics"]
        all_skips.update(metrics["skipped_signal_reasons"])
        all_quality.extend([trade["data_quality"] for trade in period["trades"] if trade.get("data_quality")])
        for key in ("closed_copied_positions", "open_copied_positions", "skipped_positions", "cap_hit_count", "below_min_trade_count"):
            extra_counts[key] += int(metrics.get(key, 0) or 0)
    equity_curve = equity_curve_from_trades(all_trades, bt_config.initial_capital)
    metrics = metrics_from_trades(all_trades, all_skips, all_quality, equity_curve, bt_config.initial_capital)
    metrics.update({key: value for key, value in extra_counts.items() if value})
    return {
        "run_id": None,
        "mode": bt_config.mode,
        "selected_wallets": selected_wallets,
        "metrics": metrics,
        "periods": {label: period["metrics"] for label, period in periods.items()},
        "candidate_count": sum(period["candidate_count"] for period in periods.values()),
        "warnings": [],
    }


def update_backtest_run(run_row: BacktestRun, result: dict[str, Any]) -> None:
    result["run_id"] = run_row.run_id
    metrics = result["metrics"]
    run_row.selected_wallets_json = json_dumps(result.get("selected_wallets", []))
    periods = result.get("periods", {})
    run_row.train_metrics_json = json_dumps(periods.get("train")) if periods.get("train") else None
    run_row.validation_metrics_json = json_dumps(periods.get("validation")) if periods.get("validation") else None
    run_row.test_metrics_json = json_dumps(periods.get("test")) if periods.get("test") else None
    run_row.total_pnl = float(metrics.get("total_pnl") or 0.0)
    run_row.roi = float(metrics.get("roi") or 0.0)
    run_row.max_drawdown = float(metrics.get("max_drawdown") or 0.0)
    run_row.trade_count = int(metrics.get("trade_count") or 0)
    run_row.win_rate = float(metrics.get("win_rate") or 0.0)
    run_row.profit_factor = metrics.get("profit_factor")
    run_row.avg_trade_size = metrics.get("avg_trade_size")
    run_row.avg_holding_time = metrics.get("avg_holding_time")
    run_row.data_quality_summary_json = json_dumps(metrics.get("data_quality_summary", {}))
    run_row.result_json = json_dumps(result)


def select_walk_forward_wallets(session: Session, bt_config: BacktestConfig, decision_time: datetime) -> list[str]:
    lookback_start = decision_time - timedelta(days=bt_config.walk_forward_lookback_days)
    stmt = (
        select(AlphaDecayResult)
        .where(AlphaDecayResult.trade_time >= lookback_start)
        .where(AlphaDecayResult.trade_time < decision_time)
        .where(AlphaDecayResult.delay_seconds == bt_config.copy_delay_seconds)
        .where(AlphaDecayResult.exit_rule == bt_config.exit_rule)
        .where(AlphaDecayResult.data_quality.in_(bt_config.allowed_data_quality))
        .where(AlphaDecayResult.skip_reason.is_(None))
        .where(AlphaDecayResult.net_pnl.is_not(None))
    )
    if bt_config.selected_wallets:
        stmt = stmt.where(AlphaDecayResult.wallet_address.in_(bt_config.selected_wallets))
    rows = list(session.scalars(stmt))
    grouped: dict[str, list[AlphaDecayResult]] = defaultdict(list)
    for row in rows:
        grouped[row.wallet_address].append(row)
    scored = []
    for wallet, wallet_rows in grouped.items():
        if len(wallet_rows) < bt_config.walk_forward_min_trades_in_lookback:
            continue
        net = sum(float(row.net_pnl or 0.0) for row in wallet_rows)
        avg = net / len(wallet_rows)
        score = avg if bt_config.walk_forward_selection_metric == "avg_net_pnl" else net
        scored.append((score, net, len(wallet_rows), wallet))
    scored.sort(reverse=True)
    return [wallet for _, _, _, wallet in scored[: bt_config.walk_forward_top_wallets]]


def infer_walk_forward_range(session: Session, bt_config: BacktestConfig) -> tuple[datetime | None, datetime | None]:
    start = bt_config.date_start
    end = bt_config.date_end
    if start is None or end is None:
        rows = list(
            session.scalars(
                select(AlphaDecayResult)
                .where(AlphaDecayResult.delay_seconds == bt_config.copy_delay_seconds)
                .where(AlphaDecayResult.exit_rule == bt_config.exit_rule)
                .where(AlphaDecayResult.trade_time.is_not(None))
                .order_by(AlphaDecayResult.trade_time)
            )
        )
        if not rows:
            return None, None
        if start is None:
            start = to_utc(rows[0].trade_time) + timedelta(days=bt_config.walk_forward_lookback_days)
        if end is None:
            end = to_utc(rows[-1].trade_time) + timedelta(seconds=1)
    return start, end


def split_warnings(periods: dict[str, dict[str, Any]]) -> list[str]:
    warnings = []
    train = periods.get("train", {}).get("metrics", {})
    validation = periods.get("validation", {}).get("metrics", {})
    test = periods.get("test", {}).get("metrics", {})
    if train.get("total_pnl", 0) > 0 and validation and validation.get("total_pnl", 0) < 0:
        warnings.append("strategy positive in train but negative in validation")
    if train.get("total_pnl", 0) > 0 and validation.get("total_pnl", 0) > 0 and test and test.get("total_pnl", 0) < 0:
        warnings.append("strategy positive in train/validation but negative in test")
    for label, metrics in (("validation", validation), ("test", test)):
        if metrics and metrics.get("trade_count", 0) < 5:
            warnings.append(f"too few trades in {label}")
        if metrics and metrics.get("max_drawdown", 0) > abs(metrics.get("total_pnl", 0)) and metrics.get("trade_count", 0):
            warnings.append(f"high drawdown in {label}")
    return warnings


def follow_wallet_exit_warnings(session: Session, bt_config: BacktestConfig, selected_wallets: list[str]) -> list[str]:
    if bt_config.copy_mode == "reconstructed_wallet_lifecycle":
        return []
    if bt_config.exit_rule not in {"follow_wallet_exit", "reconstructed_wallet_lifecycle"} or not selected_wallets:
        return []
    stmt = (
        select(AlphaDecayResult.skip_reason)
        .where(AlphaDecayResult.wallet_address.in_(selected_wallets))
        .where(AlphaDecayResult.delay_seconds == bt_config.copy_delay_seconds)
        .where(AlphaDecayResult.exit_rule == bt_config.exit_rule)
    )
    reasons = [reason for reason in session.scalars(stmt)]
    total = len(reasons)
    if total == 0:
        return [f"no {bt_config.exit_rule} alpha rows found for the selected wallets and delay"]
    no_exit_reason = "no_exit_event" if bt_config.exit_rule == "reconstructed_wallet_lifecycle" else "no_wallet_exit_found"
    no_exit = sum(1 for reason in reasons if reason == no_exit_reason)
    if no_exit / total >= 0.5:
        return [f"many {bt_config.exit_rule} rows have no matched wallet exit: {no_exit}/{total}"]
    return []


def empty_metrics(initial_capital: float) -> dict[str, Any]:
    return metrics_from_trades([], Counter(), [], [{"timestamp": utc_now().isoformat(), "equity": initial_capital}], initial_capital)


def market_category_map(session: Session) -> dict[str, str]:
    return {market_id: category or "unknown" for market_id, category in session.execute(select(Market.market_id, Market.category))}


def classification_map(session: Session) -> dict[str, WalletClassification]:
    return {row.wallet_address: row for row in session.scalars(select(WalletClassification))}


def exposure_by(open_positions: Iterable[OpenPosition], field: str, value: Any) -> float:
    return sum(position.size_usd for position in open_positions if getattr(position, field) == value)


def total_exposure(open_positions: Iterable[OpenPosition]) -> float:
    return sum(position.size_usd for position in open_positions)


def exposure_details(open_positions: list[OpenPosition], candidate: dict[str, Any], equity: float) -> dict[str, Any]:
    return {
        "wallet_exposure": exposure_by(open_positions, "wallet_address", candidate["wallet_address"]),
        "market_exposure": exposure_by(open_positions, "market_id", candidate["market_id"]),
        "category_exposure": exposure_by(open_positions, "category", candidate["category"]),
        "total_exposure": total_exposure(open_positions),
        "equity": equity,
        "candidate_size_usd": candidate["size_usd"],
    }


def grouped_pnl(trades: list[dict[str, Any]], key: str) -> dict[str, float]:
    grouped: dict[str, float] = defaultdict(float)
    for trade in trades:
        grouped[str(trade.get(key) or "unknown")] += float(trade.get("net_pnl") or 0.0)
    return {item: round_float(value) for item, value in sorted(grouped.items(), key=lambda pair: pair[1], reverse=True)}


def data_quality_summary(data_qualities: list[str]) -> dict[str, Any]:
    counts = dict(Counter(data_qualities))
    breakdown = quality_breakdown(data_qualities)
    return {
        "counts": counts,
        "percent": {level: round_float(share) for level, share in breakdown.items()},
    }


def equity_curve_from_trades(trades: list[dict[str, Any]], initial_capital: float) -> list[dict[str, Any]]:
    curve = [{"timestamp": utc_now().isoformat(), "equity": round_float(initial_capital)}]
    equity = initial_capital
    for trade in sorted(trades, key=lambda item: item.get("exit_time") or ""):
        equity += float(trade.get("net_pnl") or 0.0)
        curve.append({"timestamp": trade.get("exit_time"), "equity": round_float(equity)})
    return curve


def max_drawdown(equity_curve: list[dict[str, Any]]) -> float:
    peak = None
    drawdown = 0.0
    for point in equity_curve:
        equity = float(point.get("equity") or 0.0)
        peak = equity if peak is None else max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def average_holding_seconds(trades: list[dict[str, Any]]) -> float | None:
    values = []
    for trade in trades:
        entry = parse_datetime_start(trade.get("entry_time"))
        exit_time = parse_datetime_start(trade.get("exit_time"))
        if entry and exit_time:
            values.append(max(0.0, (exit_time - entry).total_seconds()))
    return sum(values) / len(values) if values else None


def infer_exit_time(row: AlphaDecayResult) -> datetime | None:
    raw = json_loads(row.raw_json, {}) or {}
    debug = raw.get("debug") if isinstance(raw, dict) else None
    if isinstance(debug, dict):
        parsed = parse_datetime_start(debug.get("exit_time"))
        if parsed is not None:
            return parsed
    exit_payload = raw.get("exit") if isinstance(raw, dict) else None
    if isinstance(exit_payload, dict):
        parsed = parse_datetime_start(exit_payload.get("target") or exit_payload.get("exit_time"))
        if parsed is not None:
            return parsed
    copy_time = to_utc(row.copy_time or row.trade_time)
    if row.exit_rule == "fixed_24h":
        return copy_time + timedelta(hours=24)
    return copy_time


def config_to_json(bt_config: BacktestConfig) -> dict[str, Any]:
    value = bt_config.__dict__.copy()
    for key, item in list(value.items()):
        if isinstance(item, datetime):
            value[key] = item.isoformat()
    return value


def date_split_json(bt_config: BacktestConfig) -> dict[str, Any]:
    return {
        "start": bt_config.date_start.isoformat() if bt_config.date_start else None,
        "end": bt_config.date_end.isoformat() if bt_config.date_end else None,
        "train_start": bt_config.train_start.isoformat() if bt_config.train_start else None,
        "train_end": bt_config.train_end.isoformat() if bt_config.train_end else None,
        "validation_start": bt_config.validation_start.isoformat() if bt_config.validation_start else None,
        "validation_end": bt_config.validation_end.isoformat() if bt_config.validation_end else None,
        "test_start": bt_config.test_start.isoformat() if bt_config.test_start else None,
        "test_end": bt_config.test_end.isoformat() if bt_config.test_end else None,
    }


def normalize_wallets(wallets: list[str] | None) -> list[str] | None:
    if not wallets:
        return None
    return sorted({wallet.strip().lower() for wallet in wallets if wallet and wallet.strip()})


def parse_datetime_start(value: Any) -> datetime | None:
    return parse_datetime(value, end_of_day=False)


def parse_datetime_end(value: Any) -> datetime | None:
    return parse_datetime(value, end_of_day=True)


def parse_datetime(value: Any, *, end_of_day: bool) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min, tzinfo=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
                return datetime.combine(date.fromisoformat(cleaned), time.max if end_of_day else time.min, tzinfo=timezone.utc)
            return to_utc(datetime.fromisoformat(cleaned.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def to_utc(value: datetime | None) -> datetime:
    if value is None:
        return utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def scale_value(value: Any, scale: float) -> float | None:
    parsed = parse_float(value)
    return round_float(parsed * scale) if parsed is not None else None


def normalized_side(value: str | None) -> str | None:
    return value.upper() if value else None


def class_label(classification: WalletClassification | None) -> str | None:
    return classification.class_label if classification else None


def round_float(value: Any, digits: int = 8) -> float:
    return round(float(value), digits)


def stable_id(*parts: Any) -> str:
    digest = hashlib.sha256(json_dumps(parts).encode("utf-8")).hexdigest()
    return f"{parts[0]}:{digest}"

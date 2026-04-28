from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from itertools import product
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmcopy.backtest.simulator import BacktestConfig, backtest_config_from_values, run_backtest
from pmcopy.config import database_url
from pmcopy.db import SensitivityResult, SensitivityRun, init_db, json_dumps, json_loads, session_scope, utc_now


@dataclass
class SensitivityGrid:
    copy_delay_seconds: list[int]
    max_entry_degradation: list[float]
    max_spread: list[float]
    position_size_usd: list[float]
    max_market_exposure_usd: list[float | None]


@dataclass
class SensitivityConfig:
    base_backtest_config: BacktestConfig
    grid: SensitivityGrid
    limit_combinations: int | None = None
    confirm_large_grid: bool = False
    large_grid_threshold: int = 100
    too_few_trades_threshold: int = 5
    high_drawdown_fraction: float = 0.20
    high_skipped_signal_rate: float = 0.75
    high_proxy_data_share: float = 0.50
    concentration_threshold: float = 0.80


def sensitivity_config_from_values(
    config: dict[str, Any],
    *,
    mode: str | None = None,
    selected_wallets: list[str] | None = None,
    copy_delays: list[int] | None = None,
    max_entry_degradations: list[float] | None = None,
    max_spreads: list[float] | None = None,
    position_sizes: list[float] | None = None,
    max_market_exposures: list[float | None] | None = None,
    allowed_data_quality: list[str] | None = None,
    exit_rule: str | None = None,
    limit_combinations: int | None = None,
    confirm_large_grid: bool = False,
    **backtest_overrides: Any,
) -> SensitivityConfig:
    cfg = config.get("sensitivity", {})
    base_backtest = backtest_config_from_values(
        config,
        mode=mode,
        selected_wallets=selected_wallets,
        allowed_data_quality=allowed_data_quality,
        exit_rule=exit_rule,
        **backtest_overrides,
    )
    grid = SensitivityGrid(
        copy_delay_seconds=copy_delays or [int(value) for value in cfg.get("copy_delay_seconds", [10, 60, 300, 900])],
        max_entry_degradation=max_entry_degradations
        or [float(value) for value in cfg.get("max_entry_degradation", [0.01, 0.02, 0.03])],
        max_spread=max_spreads or [float(value) for value in cfg.get("max_spread", [0.01, 0.02, 0.03, 0.05])],
        position_size_usd=position_sizes or [float(value) for value in cfg.get("position_size_usd", [1, 2, 5])],
        max_market_exposure_usd=max_market_exposures
        or [float(value) for value in cfg.get("max_market_exposure_usd", [5, 8, 10])],
    )
    return SensitivityConfig(
        base_backtest_config=base_backtest,
        grid=grid,
        limit_combinations=limit_combinations,
        confirm_large_grid=confirm_large_grid,
        large_grid_threshold=int(cfg.get("large_grid_threshold", 100)),
        too_few_trades_threshold=int(cfg.get("too_few_trades_threshold", 5)),
        high_drawdown_fraction=float(cfg.get("high_drawdown_fraction", 0.20)),
        high_skipped_signal_rate=float(cfg.get("high_skipped_signal_rate", 0.75)),
        high_proxy_data_share=float(cfg.get("high_proxy_data_share", 0.50)),
        concentration_threshold=float(cfg.get("concentration_threshold", 0.80)),
    )


def run_sensitivity(config: dict[str, Any], sensitivity_config: SensitivityConfig | None = None) -> dict[str, Any]:
    init_db(config)
    sens_config = sensitivity_config or sensitivity_config_from_values(config)
    combinations = generate_parameter_grid(sens_config.grid)
    total_combinations = len(combinations)
    combinations = enforce_grid_safety(
        combinations,
        limit_combinations=sens_config.limit_combinations,
        confirm_large_grid=sens_config.confirm_large_grid,
        large_grid_threshold=sens_config.large_grid_threshold,
    )
    sensitivity_run_id = stable_id("sensitivity", utc_now().isoformat(), total_combinations, len(combinations))
    base_config_json = backtest_config_to_json(sens_config.base_backtest_config)
    grid_json = grid_to_json(sens_config.grid)
    results: list[dict[str, Any]] = []

    with session_scope(database_url(config)) as session:
        session.add(
            SensitivityRun(
                sensitivity_run_id=sensitivity_run_id,
                created_at=utc_now(),
                base_config_json=json_dumps(base_config_json),
                parameter_grid_json=json_dumps(grid_json),
                selected_wallets_json=json_dumps(sens_config.base_backtest_config.selected_wallets or []),
            )
        )

    for index, variant in enumerate(combinations, start=1):
        bt_config = apply_parameter_variant(sens_config.base_backtest_config, variant)
        backtest_result = run_backtest(config, bt_config)
        row = sensitivity_row_from_backtest(sensitivity_run_id, index, variant, backtest_result, sens_config)
        results.append(row)
        with session_scope(database_url(config)) as session:
            store_sensitivity_result(session, row)

    robustness = robustness_diagnostics(results)
    summary = {
        "sensitivity_run_id": sensitivity_run_id,
        "estimated_combinations": total_combinations,
        "tested_combinations": len(combinations),
        "mode": sens_config.base_backtest_config.mode,
        "results": results,
        "robustness": robustness,
    }
    with session_scope(database_url(config)) as session:
        run_row = session.get(SensitivityRun, sensitivity_run_id)
        if run_row:
            run_row.result_json = json_dumps({"robustness": robustness, "tested_combinations": len(combinations)})
    return summary


def generate_parameter_grid(grid: SensitivityGrid) -> list[dict[str, Any]]:
    return [
        {
            "copy_delay_seconds": copy_delay,
            "max_entry_degradation": max_degradation,
            "max_spread": max_spread,
            "position_size_usd": position_size,
            "max_market_exposure_usd": market_exposure,
        }
        for copy_delay, max_degradation, max_spread, position_size, market_exposure in product(
            grid.copy_delay_seconds,
            grid.max_entry_degradation,
            grid.max_spread,
            grid.position_size_usd,
            grid.max_market_exposure_usd,
        )
    ]


def enforce_grid_safety(
    combinations: list[dict[str, Any]],
    *,
    limit_combinations: int | None,
    confirm_large_grid: bool,
    large_grid_threshold: int,
) -> list[dict[str, Any]]:
    if limit_combinations is not None:
        return combinations[: max(0, int(limit_combinations))]
    if len(combinations) > large_grid_threshold and not confirm_large_grid:
        raise ValueError(
            f"sensitivity grid has {len(combinations)} combinations; pass --limit-combinations N "
            "or --confirm-large-grid to run the full grid"
        )
    return combinations


def apply_parameter_variant(base_config: BacktestConfig, variant: dict[str, Any]) -> BacktestConfig:
    return replace(
        base_config,
        copy_delay_seconds=int(variant["copy_delay_seconds"]),
        max_entry_degradation=float(variant["max_entry_degradation"]),
        max_spread=float(variant["max_spread"]),
        position_size_usd=float(variant["position_size_usd"]),
        max_market_exposure_usd=(
            None if variant.get("max_market_exposure_usd") is None else float(variant["max_market_exposure_usd"])
        ),
    )


def sensitivity_row_from_backtest(
    sensitivity_run_id: str,
    index: int,
    variant: dict[str, Any],
    backtest_result: dict[str, Any],
    sens_config: SensitivityConfig,
) -> dict[str, Any]:
    metrics = backtest_result.get("metrics", {})
    periods = backtest_result.get("periods", {})
    skipped_count = sum(int(value) for value in metrics.get("skipped_signal_reasons", {}).values())
    warnings = warning_flags(metrics, periods, sens_config)
    return {
        "id": stable_id("sensitivity_result", sensitivity_run_id, index, variant),
        "sensitivity_run_id": sensitivity_run_id,
        "config_variant": variant,
        "copy_delay_seconds": int(variant["copy_delay_seconds"]),
        "max_entry_degradation": float(variant["max_entry_degradation"]),
        "max_spread": float(variant["max_spread"]),
        "position_size_usd": float(variant["position_size_usd"]),
        "max_market_exposure_usd": variant.get("max_market_exposure_usd"),
        "total_pnl": float(metrics.get("total_pnl") or 0.0),
        "roi": float(metrics.get("roi") or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown") or 0.0),
        "trade_count": int(metrics.get("trade_count") or 0),
        "win_rate": float(metrics.get("win_rate") or 0.0),
        "profit_factor": metrics.get("profit_factor"),
        "skipped_signal_count": skipped_count,
        "data_quality_summary": metrics.get("data_quality_summary", {}),
        "train_metrics": periods.get("train"),
        "validation_metrics": periods.get("validation"),
        "test_metrics": periods.get("test"),
        "warning_flags": warnings,
        "backtest_run_id": backtest_result.get("run_id"),
    }


def store_sensitivity_result(session: Session, row: dict[str, Any]) -> None:
    session.add(
        SensitivityResult(
            id=row["id"],
            sensitivity_run_id=row["sensitivity_run_id"],
            config_variant_json=json_dumps(row["config_variant"]),
            copy_delay_seconds=row["copy_delay_seconds"],
            max_entry_degradation=row["max_entry_degradation"],
            max_spread=row["max_spread"],
            position_size_usd=row["position_size_usd"],
            max_market_exposure_usd=row["max_market_exposure_usd"],
            total_pnl=row["total_pnl"],
            roi=row["roi"],
            max_drawdown=row["max_drawdown"],
            trade_count=row["trade_count"],
            win_rate=row["win_rate"],
            profit_factor=row["profit_factor"],
            skipped_signal_count=row["skipped_signal_count"],
            data_quality_summary_json=json_dumps(row["data_quality_summary"]),
            train_metrics_json=json_dumps(row["train_metrics"]) if row["train_metrics"] else None,
            validation_metrics_json=json_dumps(row["validation_metrics"]) if row["validation_metrics"] else None,
            test_metrics_json=json_dumps(row["test_metrics"]) if row["test_metrics"] else None,
            warning_flags_json=json_dumps(row["warning_flags"]),
        )
    )


def warning_flags(metrics: dict[str, Any], periods: dict[str, Any], sens_config: SensitivityConfig) -> list[str]:
    flags: list[str] = []
    trade_count = int(metrics.get("trade_count") or 0)
    skipped_count = sum(int(value) for value in metrics.get("skipped_signal_reasons", {}).values())
    initial_capital = max(float(sens_config.base_backtest_config.initial_capital or 0.0), 1e-9)
    if trade_count == 0:
        flags.append("zero_trades")
    if trade_count < sens_config.too_few_trades_threshold:
        flags.append("too_few_trades")
    if float(metrics.get("roi") or 0.0) < 0:
        flags.append("negative_roi")
    if float(metrics.get("max_drawdown") or 0.0) / initial_capital >= sens_config.high_drawdown_fraction:
        flags.append("high_drawdown")
    if skipped_count + trade_count > 0 and skipped_count / (skipped_count + trade_count) >= sens_config.high_skipped_signal_rate:
        flags.append("high_skipped_signal_rate")
    if proxy_data_share(metrics.get("data_quality_summary", {})) >= sens_config.high_proxy_data_share:
        flags.append("high_proxy_data_share")
    if pnl_concentration(metrics.get("pnl_by_wallet", {})) >= sens_config.concentration_threshold:
        flags.append("result_depends_on_one_wallet")
    if pnl_concentration(metrics.get("pnl_by_market", {})) >= sens_config.concentration_threshold:
        flags.append("result_depends_on_one_market")
    if pnl_concentration(metrics.get("pnl_by_category", {})) >= sens_config.concentration_threshold:
        flags.append("result_depends_on_one_category")
    train = periods.get("train") or {}
    validation = periods.get("validation") or {}
    test = periods.get("test") or {}
    if float(train.get("total_pnl") or 0.0) > 0 and float(validation.get("total_pnl") or 0.0) < 0:
        flags.append("train_positive_validation_negative")
    if float(validation.get("total_pnl") or 0.0) > 0 and float(test.get("total_pnl") or 0.0) < 0:
        flags.append("validation_positive_test_negative")
    return flags


def robustness_diagnostics(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"label": "no_results", "reasons": ["no parameter combinations were run"]}
    positive = [row for row in results if row["roi"] > 0 and row["trade_count"] >= 1]
    meaningful_positive = [row for row in results if row["roi"] > 0 and row["trade_count"] >= 5]
    negative = [row for row in results if row["roi"] < 0]
    warning_counts: dict[str, int] = {}
    for row in results:
        for flag in row.get("warning_flags", []):
            warning_counts[flag] = warning_counts.get(flag, 0) + 1
    neighboring_positive = neighboring_positive_pairs(results)
    reasons: list[str] = []
    label = "unstable"
    if len(meaningful_positive) >= 2 and neighboring_positive >= 1 and warning_counts.get("high_drawdown", 0) < len(results) / 2:
        label = "stable_candidate"
        reasons.append("multiple neighboring configurations have positive ROI and meaningful trade counts")
    else:
        if len(positive) <= 1:
            reasons.append("only one or zero parameter combinations are positive")
        if negative:
            reasons.append(f"{len(negative)} parameter combinations are negative")
        if warning_counts.get("too_few_trades", 0):
            reasons.append("some configurations have too few trades")
        if warning_counts.get("high_proxy_data_share", 0):
            reasons.append("results rely heavily on proxy data")
    return {
        "label": label,
        "positive_combinations": len(positive),
        "meaningful_positive_combinations": len(meaningful_positive),
        "negative_combinations": len(negative),
        "neighboring_positive_pairs": neighboring_positive,
        "warning_counts": warning_counts,
        "reasons": reasons,
    }


def neighboring_positive_pairs(results: list[dict[str, Any]]) -> int:
    positive_keys = {
        (
            row["copy_delay_seconds"],
            row["max_entry_degradation"],
            row["max_spread"],
            row["position_size_usd"],
            row["max_market_exposure_usd"],
        )
        for row in results
        if row["roi"] > 0 and row["trade_count"] >= 1
    }
    pairs = 0
    for row in results:
        key = (
            row["copy_delay_seconds"],
            row["max_entry_degradation"],
            row["max_spread"],
            row["position_size_usd"],
            row["max_market_exposure_usd"],
        )
        if key not in positive_keys:
            continue
        for other in positive_keys:
            if key == other:
                continue
            same_other_params = key[2:] == other[2:]
            one_axis_neighbor = key[0] == other[0] or key[1] == other[1]
            if same_other_params and one_axis_neighbor:
                pairs += 1
                break
    return pairs


def proxy_data_share(data_quality_summary: dict[str, Any]) -> float:
    percent = data_quality_summary.get("percent", {}) if isinstance(data_quality_summary, dict) else {}
    return float(percent.get("price_history_proxy", 0.0) or 0.0) + float(percent.get("midpoint_proxy", 0.0) or 0.0) + float(percent.get("last_price_proxy", 0.0) or 0.0)


def pnl_concentration(grouped_pnl: dict[str, Any]) -> float:
    values = [abs(float(value or 0.0)) for value in grouped_pnl.values()]
    total = sum(values)
    return max(values) / total if total else 0.0


def prepare_heatmap_data(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, float], list[float]] = {}
    for row in rows:
        key = (int(row["copy_delay_seconds"]), float(row["max_entry_degradation"]))
        grouped.setdefault(key, []).append(float(row.get(metric) or 0.0))
    return [
        {
            "copy_delay_seconds": key[0],
            "max_entry_degradation": key[1],
            metric: sum(values) / len(values),
        }
        for key, values in sorted(grouped.items())
    ]


def sensitivity_results_dataframe(session: Session, sensitivity_run_id: str | None = None):
    import pandas as pd

    stmt = select(SensitivityResult)
    if sensitivity_run_id:
        stmt = stmt.where(SensitivityResult.sensitivity_run_id == sensitivity_run_id)
    rows = []
    for row in session.scalars(stmt.order_by(SensitivityResult.sensitivity_run_id.desc(), SensitivityResult.copy_delay_seconds)):
        rows.append(
            {
                "sensitivity_run_id": row.sensitivity_run_id,
                "copy_delay_seconds": row.copy_delay_seconds,
                "max_entry_degradation": row.max_entry_degradation,
                "max_spread": row.max_spread,
                "position_size_usd": row.position_size_usd,
                "max_market_exposure_usd": row.max_market_exposure_usd,
                "total_pnl": row.total_pnl,
                "roi": row.roi,
                "max_drawdown": row.max_drawdown,
                "trade_count": row.trade_count,
                "win_rate": row.win_rate,
                "profit_factor": row.profit_factor,
                "skipped_signal_count": row.skipped_signal_count,
                "data_quality_summary": json_loads(row.data_quality_summary_json, {}),
                "warning_flags": ", ".join(json_loads(row.warning_flags_json, []) or []),
            }
        )
    return pd.DataFrame(rows)


def grid_to_json(grid: SensitivityGrid) -> dict[str, Any]:
    return {
        "copy_delay_seconds": grid.copy_delay_seconds,
        "max_entry_degradation": grid.max_entry_degradation,
        "max_spread": grid.max_spread,
        "position_size_usd": grid.position_size_usd,
        "max_market_exposure_usd": grid.max_market_exposure_usd,
    }


def backtest_config_to_json(config: BacktestConfig) -> dict[str, Any]:
    payload = config.__dict__.copy()
    for key, value in list(payload.items()):
        if hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
    return payload


def stable_id(*parts: Any) -> str:
    digest = hashlib.sha256(json_dumps(parts).encode("utf-8")).hexdigest()
    return f"{parts[0]}:{digest}"

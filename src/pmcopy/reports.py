from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from pmcopy.config import database_url, project_root, resolve_project_path
from pmcopy.db import (
    Activity,
    AlphaDecayResult,
    BacktestRun,
    BacktestTrade,
    CandidateWallet,
    LifecycleCopyEvent,
    LifecycleCopyPosition,
    LifecycleCopyRun,
    Market,
    RawResponse,
    ReconstructedPosition,
    ReconstructedPositionEvent,
    SensitivityResult,
    SensitivityRun,
    SkippedSignal,
    Token,
    Trade,
    Wallet,
    WalletClassification,
    WalletCopyability,
    WalletMetrics,
    init_db,
    json_dumps,
    json_loads,
    session_scope,
)


ALLOWED_EXPORT_TABLES = {
    "candidate_wallets": CandidateWallet,
    "wallets": Wallet,
    "wallet_metrics": WalletMetrics,
    "wallet_classification": WalletClassification,
    "alpha_decay_results": AlphaDecayResult,
    "reconstructed_positions": ReconstructedPosition,
    "reconstructed_position_events": ReconstructedPositionEvent,
    "wallet_copyability": WalletCopyability,
    "backtest_runs": BacktestRun,
    "backtest_trades": BacktestTrade,
    "lifecycle_copy_runs": LifecycleCopyRun,
    "lifecycle_copy_events": LifecycleCopyEvent,
    "lifecycle_copy_positions": LifecycleCopyPosition,
    "skipped_signals": SkippedSignal,
    "sensitivity_results": SensitivityResult,
}


def export_table(config: dict[str, Any], table_name: str, output: str | Path | None = None) -> dict[str, Any]:
    if table_name not in ALLOWED_EXPORT_TABLES:
        raise ValueError(f"Unsupported export table: {table_name}")
    init_db(config)
    path = export_path(output or f"data/exports/{table_name}.csv")
    with session_scope(database_url(config)) as session:
        df = table_dataframe(session, ALLOWED_EXPORT_TABLES[table_name])
    df.to_csv(path, index=False)
    return {"table": table_name, "path": str(path), "rows": len(df)}


def export_report(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    init_db(config)
    safe_run = safe_filename(run_id)
    directory = export_path(f"data/exports/report_{safe_run}")
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    with session_scope(database_url(config)) as session:
        run = session.get(BacktestRun, run_id)
        if run is None:
            raise ValueError(f"Backtest run not found: {run_id}")

        run_df = pd.DataFrame([record_from_row(run)])
        trades_df = table_dataframe(session, BacktestTrade, BacktestTrade.run_id == run_id)
        skipped_df = table_dataframe(session, SkippedSignal, SkippedSignal.run_id == run_id)
        verdict = verdict_for_run(run, config)
        verdict_df = pd.DataFrame([verdict])
        sensitivity_df = sensitivity_results_dataframe(session)

    for name, df in (
        ("backtest_run", run_df),
        ("backtest_trades", trades_df),
        ("skipped_signals", skipped_df),
        ("verdict", verdict_df),
        ("sensitivity_results", sensitivity_df),
    ):
        path = directory / f"{name}.csv"
        df.to_csv(path, index=False)
        paths[name] = str(path)
    return {"run_id": run_id, "directory": str(directory), "paths": paths, "verdict": verdict}


def table_dataframe(session: Session, model: type[Any], *filters: Any) -> pd.DataFrame:
    stmt = select(model)
    for item in filters:
        stmt = stmt.where(item)
    return pd.DataFrame([record_from_row(row) for row in session.scalars(stmt)])


def record_from_row(row: Any) -> dict[str, Any]:
    record = {column.name: serialize_value(getattr(row, column.name)) for column in row.__table__.columns}
    if isinstance(row, WalletMetrics):
        record["edge_on_volume"] = row.roi_on_volume
        record["pnl_per_traded_dollar"] = row.roi_on_volume
    return record


def serialize_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json_dumps(value)
    return value


def backtest_runs_dataframe(session: Session, config: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for run in session.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc())):
        result = json_loads(run.result_json, {}) or {}
        metrics = result.get("metrics", {})
        run_config = json_loads(run.config_json, {}) or {}
        verdict = verdict_for_run(run, config)
        rows.append(
            {
                "run_id": run.run_id,
                "created_at": run.created_at,
                "mode": run.mode,
                "selected_wallets": ", ".join(json_loads(run.selected_wallets_json, []) or []),
                "config_summary": config_summary(run_config),
                "date_split_summary": date_split_summary(json_loads(run.date_split_json, {}) or {}),
                "total_pnl": run.total_pnl,
                "roi": run.roi,
                "max_drawdown": run.max_drawdown,
                "trade_count": run.trade_count,
                "win_rate": run.win_rate,
                "profit_factor": run.profit_factor,
                "data_quality_warning_count": data_quality_warning_count(verdict),
                "verdict": verdict["verdict"],
                "verdict_reasons": "; ".join(verdict["reasons"]),
                "exit_rule": run_config.get("exit_rule"),
                "copy_delay_seconds": run_config.get("copy_delay_seconds"),
                "skipped_signal_count": sum(int(value) for value in metrics.get("skipped_signal_reasons", {}).values()),
            }
        )
    return pd.DataFrame(rows)


def verdict_for_run(run: BacktestRun, config: dict[str, Any]) -> dict[str, Any]:
    result = json_loads(run.result_json, {}) or {}
    metrics = result.get("metrics", {})
    periods = result.get("periods", {})
    run_config = json_loads(run.config_json, {}) or {}
    reasons: list[str] = []
    warnings: list[str] = []
    trade_count = int(metrics.get("trade_count") or run.trade_count or 0)
    roi = float(metrics.get("roi") if metrics.get("roi") is not None else run.roi or 0.0)
    max_drawdown = float(metrics.get("max_drawdown") if metrics.get("max_drawdown") is not None else run.max_drawdown or 0.0)
    initial_capital = max(float(run_config.get("initial_capital") or 100), 1e-9)
    skipped_count = sum(int(value) for value in metrics.get("skipped_signal_reasons", {}).values())
    data_quality = metrics.get("data_quality_summary", {}).get("percent", {})
    proxy_share = proxy_share_from_percent(data_quality)
    insufficient_share = float(data_quality.get("insufficient_data", 0.0) or 0.0)
    exact_share = float(data_quality.get("exact_orderbook", 0.0) or 0.0)
    wallet_conc = pnl_concentration(metrics.get("pnl_by_wallet", {}))
    market_conc = pnl_concentration(metrics.get("pnl_by_market", {}))
    category_conc = pnl_concentration(metrics.get("pnl_by_category", {}))

    if run_config.get("exit_rule") == "latest_available":
        warnings.append("latest_available exit rule used; diagnostic only and can introduce bias")
    if trade_count == 0:
        reasons.append("zero accepted trades")
    if trade_count < int(config.get("reports", {}).get("min_trades_for_verdict", 20)):
        reasons.append("too few trades for a reliable verdict")
    if skipped_count and skipped_count / max(skipped_count + trade_count, 1) >= 0.75:
        reasons.append("too many skipped signals")
    if roi < 0:
        reasons.append("negative ROI")
    if max_drawdown / initial_capital >= float(config.get("reports", {}).get("high_drawdown_fraction", 0.20)):
        reasons.append("high drawdown relative to initial capital")
    if proxy_share >= float(config.get("data_quality", {}).get("warn_if_proxy_share_above", 0.50)):
        reasons.append("strategy relies heavily on proxy data")
    if insufficient_share >= 0.25:
        reasons.append("insufficient_data share is high")
    if exact_share == 0 and proxy_share > 0:
        warnings.append("no exact_orderbook rows in accepted trades")
    warnings.extend(exposure_metric_warnings_for_run(run))
    if wallet_conc >= 0.80:
        reasons.append("result depends on one wallet")
    if market_conc >= 0.80:
        reasons.append("result depends on one market")
    if category_conc >= 0.80:
        reasons.append("result depends on one category")
    if split_train_positive_validation_negative(periods):
        reasons.append("strategy works in train but fails validation")
    if split_validation_positive_test_negative(periods):
        reasons.append("strategy works in validation but fails test")

    verdict = "needs more data"
    if proxy_share >= 0.75 or insufficient_share >= 0.25:
        verdict = "poor data quality"
    if wallet_conc >= 0.80 or market_conc >= 0.80 or category_conc >= 0.80:
        verdict = "overfit / too concentrated"
    if roi < 0 or max_drawdown / initial_capital >= 0.30 or split_train_positive_validation_negative(periods):
        verdict = "reject"
    if trade_count < 5 and roi >= 0:
        verdict = "needs more data"
    if (
        roi > 0
        and trade_count >= int(config.get("reports", {}).get("min_trades_for_verdict", 20))
        and max_drawdown / initial_capital < 0.10
        and max(wallet_conc, market_conc, category_conc) < 0.60
        and proxy_share < float(config.get("data_quality", {}).get("warn_if_proxy_share_above", 0.50))
    ):
        verdict = "promising for paper trading"
        reasons.append("positive result with controlled drawdown and acceptable concentration in this backtest")

    if not reasons:
        reasons.append("not enough evidence for a stronger verdict")
    return {
        "run_id": run.run_id,
        "verdict": verdict,
        "reasons": reasons,
        "warnings": warnings,
        "trade_count": trade_count,
        "roi": roi,
        "max_drawdown": max_drawdown,
        "proxy_share": proxy_share,
        "insufficient_share": insufficient_share,
        "wallet_concentration": wallet_conc,
        "market_concentration": market_conc,
        "category_concentration": category_conc,
    }


def exposure_metric_warnings_for_run(run: BacktestRun) -> list[str]:
    session = object_session(run)
    selected_wallets = json_loads(run.selected_wallets_json, []) or []
    if session is None or not selected_wallets:
        return []
    rows = list(session.scalars(select(WalletMetrics).where(WalletMetrics.wallet_address.in_(selected_wallets))))
    if not rows:
        return ["exposure_metrics_unavailable"]
    max_conf = [(row.max_exposure_confidence or "unavailable") for row in rows]
    avg_conf = [(row.average_exposure_confidence or "unavailable") for row in rows]
    combined = max_conf + avg_conf
    if all(confidence == "unavailable" for confidence in combined):
        return ["exposure_metrics_unavailable"]
    reconstructed_labels = {"reconstructed_positions", "reconstructed_positions_time_weighted"}
    proxy_labels = {"data_api_proxy", "snapshots_proxy"}
    if not any(confidence in reconstructed_labels for confidence in combined) and any(
        confidence in proxy_labels for confidence in combined
    ):
        return ["exposure_metrics_proxy_only"]
    return []


def data_quality_summary(session: Session) -> dict[str, Any]:
    raw_total = session.scalar(select(func.count()).select_from(RawResponse)) or 0
    raw_failed = session.scalar(select(func.count()).select_from(RawResponse).where(RawResponse.success.is_(False))) or 0
    alpha_total = session.scalar(select(func.count()).select_from(AlphaDecayResult)) or 0
    alpha_quality_counts = dict(session.execute(select(AlphaDecayResult.data_quality, func.count()).group_by(AlphaDecayResult.data_quality)).all())
    skipped_total = session.scalar(select(func.count()).select_from(SkippedSignal)) or 0
    backtest_trades = session.scalar(select(func.count()).select_from(BacktestTrade)) or 0
    skip_counts = dict(session.execute(select(SkippedSignal.reason, func.count()).group_by(SkippedSignal.reason)).all())
    missing_market_metadata = session.scalar(select(func.count()).select_from(Market).where(Market.category.is_(None))) or 0
    missing_token_ids = session.scalar(select(func.count()).select_from(Trade).where(Trade.token_id.is_(None))) or 0
    missing_exit_prices = session.scalar(
        select(func.count()).select_from(AlphaDecayResult).where(AlphaDecayResult.eventual_exit_price.is_(None))
    ) or 0
    missing_price_history = session.scalar(
        select(func.count()).select_from(AlphaDecayResult).where(AlphaDecayResult.skip_reason.in_(["no_price_history", "price_history_parse_failed"]))
    ) or 0
    insufficient = alpha_quality_counts.get("insufficient_data", 0)
    price_proxy = alpha_quality_counts.get("price_history_proxy", 0)
    exact = alpha_quality_counts.get("exact_orderbook", 0)
    return {
        "raw_total": raw_total,
        "raw_failed": raw_failed,
        "alpha_total": alpha_total,
        "alpha_quality_counts": alpha_quality_counts,
        "insufficient_data_share": insufficient / alpha_total if alpha_total else 0.0,
        "price_history_proxy_share": price_proxy / alpha_total if alpha_total else 0.0,
        "exact_orderbook_share": exact / alpha_total if alpha_total else 0.0,
        "backtest_trades": backtest_trades,
        "skipped_signals": skipped_total,
        "most_common_skip_reason": max(skip_counts.items(), key=lambda item: item[1])[0] if skip_counts else None,
        "skip_counts": skip_counts,
        "missing_market_metadata": missing_market_metadata,
        "missing_token_ids": missing_token_ids,
        "missing_price_history": missing_price_history,
        "missing_exit_prices": missing_exit_prices,
    }


def raw_response_counts_dataframe(session: Session) -> pd.DataFrame:
    rows = [
        {"source": source, "endpoint": endpoint, "success": success, "count": count}
        for source, endpoint, success, count in session.execute(
            select(RawResponse.source, RawResponse.endpoint, RawResponse.success, func.count()).group_by(
                RawResponse.source, RawResponse.endpoint, RawResponse.success
            )
        )
    ]
    return pd.DataFrame(rows)


def affected_alpha_dataframe(session: Session, limit: int = 500) -> pd.DataFrame:
    rows = []
    stmt = (
        select(AlphaDecayResult)
        .where((AlphaDecayResult.data_quality == "insufficient_data") | AlphaDecayResult.skip_reason.is_not(None))
        .order_by(AlphaDecayResult.trade_time.desc().nullslast())
        .limit(limit)
    )
    for row in session.scalars(stmt):
        rows.append(
            {
                "wallet_address": row.wallet_address,
                "trade_id": row.trade_id,
                "market_id": row.market_id,
                "token_id": row.token_id,
                "trade_time": row.trade_time,
                "data_quality": row.data_quality,
                "skip_reason": row.skip_reason,
            }
        )
    return pd.DataFrame(rows)


def copyability_dataframe(session: Session) -> pd.DataFrame:
    rows = []
    for row in session.scalars(select(WalletCopyability).order_by(WalletCopyability.copyability_score.desc().nullslast())):
        rows.append(record_from_row(row))
    return pd.DataFrame(rows)


def sensitivity_results_dataframe(session: Session) -> pd.DataFrame:
    rows = []
    for row in session.scalars(select(SensitivityResult).order_by(SensitivityResult.sensitivity_run_id.desc())):
        record = record_from_row(row)
        record["warning_flags"] = ", ".join(json_loads(row.warning_flags_json, []) or [])
        rows.append(record)
    return pd.DataFrame(rows)


def sensitivity_summary_dataframe(session: Session) -> pd.DataFrame:
    rows = []
    for run in session.scalars(select(SensitivityRun).order_by(SensitivityRun.created_at.desc())):
        result = json_loads(run.result_json, {}) or {}
        robustness = result.get("robustness", {})
        rows.append(
            {
                "sensitivity_run_id": run.sensitivity_run_id,
                "created_at": run.created_at,
                "tested_combinations": result.get("tested_combinations"),
                "label": robustness.get("label"),
                "positive_combinations": robustness.get("positive_combinations"),
                "negative_combinations": robustness.get("negative_combinations"),
                "warning_counts": json_dumps(robustness.get("warning_counts", {})),
            }
        )
    return pd.DataFrame(rows)


def best_worst_from_group(metrics: dict[str, Any], key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    values = metrics.get(key, {}) or {}
    if not values:
        return {}, {}
    items = sorted(values.items(), key=lambda item: item[1])
    return {items[-1][0]: items[-1][1]}, {items[0][0]: items[0][1]}


def config_summary(run_config: dict[str, Any]) -> str:
    keys = ["copy_delay_seconds", "position_size_usd", "max_spread", "max_entry_degradation", "exit_rule"]
    return ", ".join(f"{key}={run_config.get(key)}" for key in keys if key in run_config)


def date_split_summary(split: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in split.items() if value)


def data_quality_warning_count(verdict: dict[str, Any]) -> int:
    text = " ".join(verdict.get("reasons", []) + verdict.get("warnings", [])).lower()
    return sum(1 for token in ("proxy", "insufficient", "data quality", "exact_orderbook") if token in text)


def proxy_share_from_percent(percent: dict[str, Any]) -> float:
    return (
        float(percent.get("price_history_proxy", 0.0) or 0.0)
        + float(percent.get("midpoint_proxy", 0.0) or 0.0)
        + float(percent.get("last_price_proxy", 0.0) or 0.0)
    )


def pnl_concentration(grouped_pnl: dict[str, Any]) -> float:
    values = [abs(float(value or 0.0)) for value in grouped_pnl.values()]
    total = sum(values)
    return max(values) / total if total else 0.0


def split_train_positive_validation_negative(periods: dict[str, Any]) -> bool:
    train = periods.get("train", {})
    validation = periods.get("validation", {})
    return float(train.get("total_pnl") or 0.0) > 0 and float(validation.get("total_pnl") or 0.0) < 0


def split_validation_positive_test_negative(periods: dict[str, Any]) -> bool:
    validation = periods.get("validation", {})
    test = periods.get("test", {})
    return float(validation.get("total_pnl") or 0.0) > 0 and float(test.get("total_pnl") or 0.0) < 0


def export_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = resolve_project_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:160]

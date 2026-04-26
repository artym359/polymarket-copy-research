from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmcopy.db import Trade, Wallet, WalletClassification, WalletMetrics, json_loads


def wallet_screener_dataframe(session: Session) -> pd.DataFrame:
    stmt = (
        select(Wallet, WalletMetrics, WalletClassification)
        .outerjoin(WalletMetrics, Wallet.wallet_address == WalletMetrics.wallet_address)
        .outerjoin(WalletClassification, Wallet.wallet_address == WalletClassification.wallet_address)
        .order_by(Wallet.wallet_address)
    )
    rows: list[dict[str, Any]] = []
    for wallet, metrics, classification in session.execute(stmt):
        category_breakdown = json_loads(metrics.category_breakdown_json, {}) if metrics else {}
        reasons = json_loads(classification.reasons_json, []) if classification else []
        rows.append(
            {
                "wallet": wallet.wallet_address,
                "username": wallet.username,
                "class_label": classification.class_label if classification else None,
                "total_pnl": metrics.total_pnl if metrics else None,
                "volume": metrics.total_volume if metrics else None,
                "edge_on_volume": metrics.roi_on_volume if metrics else None,
                "roi_on_volume": metrics.roi_on_volume if metrics else None,
                "pnl_per_traded_dollar": metrics.roi_on_volume if metrics else None,
                "max_capital_at_risk": metrics.max_capital_at_risk if metrics else None,
                "return_on_max_capital_at_risk": metrics.return_on_max_capital_at_risk if metrics else None,
                "max_exposure_method": metrics.max_exposure_method if metrics else None,
                "max_exposure_confidence": metrics.max_exposure_confidence if metrics else "unavailable",
                "average_capital_at_risk": metrics.average_capital_at_risk if metrics else None,
                "return_on_average_capital_at_risk": metrics.return_on_average_capital_at_risk if metrics else None,
                "average_exposure_method": metrics.average_exposure_method if metrics else None,
                "average_exposure_confidence": metrics.average_exposure_confidence if metrics else "unavailable",
                "trade_count": metrics.trade_count if metrics else 0,
                "market_count": metrics.market_count if metrics else 0,
                "active_days": metrics.active_days if metrics else None,
                "max_drawdown": metrics.max_drawdown_estimate if metrics else None,
                "top_1_market_pnl_share": metrics.top_1_market_pnl_share if metrics else None,
                "top_5_market_pnl_share": metrics.top_5_market_pnl_share if metrics else None,
                "main_category": metrics.main_category if metrics else None,
                "categories": sorted(category_breakdown.keys()) if category_breakdown else [],
                "classification_reasons": "; ".join(reasons[:8]),
                "has_metrics": metrics is not None,
                "has_classification": classification is not None,
            }
        )
    return pd.DataFrame(rows)


def apply_wallet_filters(df: pd.DataFrame, filters: dict[str, Any]) -> pd.DataFrame:
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)

    mask &= numeric_min(df, "total_pnl", filters.get("min_total_pnl"))
    min_edge = filters.get("min_edge_on_volume", filters.get("min_roi_on_volume"))
    edge_column = "edge_on_volume" if "edge_on_volume" in df.columns else "roi_on_volume"
    mask &= numeric_min(df, edge_column, min_edge)
    mask &= numeric_min(
        df,
        "return_on_max_capital_at_risk",
        filters.get("min_return_on_max_capital_at_risk"),
    )
    mask &= numeric_min(
        df,
        "return_on_average_capital_at_risk",
        filters.get("min_return_on_average_capital_at_risk"),
    )
    mask &= numeric_min(df, "volume", filters.get("min_total_volume"))
    mask &= numeric_min(df, "trade_count", filters.get("min_trades"))
    mask &= numeric_min(df, "market_count", filters.get("min_markets"))
    mask &= numeric_min(df, "active_days", filters.get("min_active_days"))
    mask &= numeric_max(df, "max_drawdown", filters.get("max_drawdown"))
    mask &= numeric_max(df, "top_1_market_pnl_share", filters.get("max_top_1_market_pnl_share"))
    mask &= numeric_max(df, "top_5_market_pnl_share", filters.get("max_top_5_market_pnl_share"))

    include_categories = set(filters.get("include_categories") or [])
    exclude_categories = set(filters.get("exclude_categories") or [])
    if include_categories:
        mask &= df["categories"].apply(lambda values: bool(include_categories.intersection(set(values or []))))
    if exclude_categories:
        mask &= ~df["categories"].apply(lambda values: bool(exclude_categories.intersection(set(values or []))))

    confidence_filter = set(filters.get("exposure_confidence_filter") or [])
    if confidence_filter:
        max_conf = df.get("max_exposure_confidence", pd.Series("unavailable", index=df.index)).fillna("unavailable")
        avg_conf = df.get("average_exposure_confidence", pd.Series("unavailable", index=df.index)).fillna("unavailable")
        mask &= max_conf.isin(confidence_filter) | avg_conf.isin(confidence_filter)

    if filters.get("exclude_likely_market_makers", True):
        mask &= df["class_label"].fillna("") != "likely_market_maker"
    if filters.get("exclude_lucky_wallets", True):
        mask &= df["class_label"].fillna("") != "lucky_wallet"
    if filters.get("exclude_insufficient_sample", True):
        mask &= df["class_label"].fillna("") != "insufficient_sample"

    return df[mask].copy()


def screener_counts(session: Session) -> dict[str, Any]:
    promoted = session.scalar(select(func.count()).select_from(Wallet)) or 0
    ingested = session.scalar(select(func.count(func.distinct(Trade.wallet_address)))) or 0
    metrics = session.scalar(select(func.count()).select_from(WalletMetrics)) or 0
    class_rows = session.execute(
        select(WalletClassification.class_label, func.count()).group_by(WalletClassification.class_label)
    ).all()
    return {
        "promoted_wallets": promoted,
        "wallets_with_ingested_trades": ingested,
        "wallets_with_metrics": metrics,
        "by_class_label": {label: count for label, count in class_rows},
    }


def numeric_min(df: pd.DataFrame, column: str, threshold: Any) -> pd.Series:
    if threshold is None:
        return pd.Series(True, index=df.index)
    values = pd.to_numeric(df[column], errors="coerce")
    return values.notna() & (values >= float(threshold))


def numeric_max(df: pd.DataFrame, column: str, threshold: Any) -> pd.Series:
    if threshold is None:
        return pd.Series(True, index=df.index)
    values = pd.to_numeric(df[column], errors="coerce")
    return values.isna() | (values <= float(threshold))

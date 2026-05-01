from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import func, select

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import AlphaDecayResult, RawResponse, SkippedSignal, Trade, init_db, json_loads, session_scope  # noqa: E402
from pmcopy.reports import affected_alpha_dataframe, data_quality_summary, raw_response_counts_dataframe  # noqa: E402


def raw_failures_dataframe(session, limit: int = 200) -> pd.DataFrame:
    rows = []
    stmt = select(RawResponse).where(RawResponse.success.is_(False)).order_by(RawResponse.fetched_at.desc()).limit(limit)
    for row in session.scalars(stmt):
        rows.append(
            {
                "fetched_at": row.fetched_at,
                "source": row.source,
                "endpoint": row.endpoint,
                "status_code": row.status_code,
                "error": row.error,
                "url": row.url,
                "params": row.params_json,
            }
        )
    return pd.DataFrame(rows)


def raw_samples_dataframe(session, limit: int = 20) -> pd.DataFrame:
    rows = []
    stmt = select(RawResponse).order_by(RawResponse.fetched_at.desc()).limit(limit)
    for row in session.scalars(stmt):
        payload = json_loads(row.response_json, None)
        rows.append(
            {
                "fetched_at": row.fetched_at,
                "source": row.source,
                "endpoint": row.endpoint,
                "success": row.success,
                "status_code": row.status_code,
                "sample": str(payload)[:500] if payload is not None else (row.response_text or "")[:500],
            }
        )
    return pd.DataFrame(rows)


def missing_token_trades_dataframe(session, limit: int = 200) -> pd.DataFrame:
    rows = []
    stmt = select(Trade).where(Trade.token_id.is_(None)).order_by(Trade.timestamp.desc().nullslast()).limit(limit)
    for row in session.scalars(stmt):
        rows.append(
            {
                "wallet_address": row.wallet_address,
                "trade_id": row.id,
                "market_id": row.market_id,
                "timestamp": row.timestamp,
                "side": row.side,
                "price": row.price,
            }
        )
    return pd.DataFrame(rows)


st.set_page_config(page_title="Data Quality", layout="wide")

config = load_config()
init_db(config)

st.title("Data Quality")
st.caption("Make hidden data problems visible before trusting any alpha-decay, backtest, or sensitivity output.")

with session_scope(database_url(config)) as session:
    summary = data_quality_summary(session)
    raw_counts = raw_response_counts_dataframe(session)
    failures = raw_failures_dataframe(session)
    affected_alpha = affected_alpha_dataframe(session)
    missing_tokens = missing_token_trades_dataframe(session)
    raw_samples = raw_samples_dataframe(session)
    skip_rows = [
        {"reason": reason, "count": count}
        for reason, count in session.execute(select(SkippedSignal.reason, func.count()).group_by(SkippedSignal.reason))
    ]
    alpha_skip_rows = [
        {"skip_reason": reason or "", "count": count}
        for reason, count in session.execute(select(AlphaDecayResult.skip_reason, func.count()).where(AlphaDecayResult.skip_reason.is_not(None)).group_by(AlphaDecayResult.skip_reason))
    ]

cards = st.columns(8)
cards[0].metric("raw responses", summary["raw_total"])
cards[1].metric("failed raw", summary["raw_failed"])
cards[2].metric("alpha rows", summary["alpha_total"])
cards[3].metric("insufficient", f"{summary['insufficient_data_share']:.1%}")
cards[4].metric("price proxy", f"{summary['price_history_proxy_share']:.1%}")
cards[5].metric("exact book", f"{summary['exact_orderbook_share']:.1%}")
cards[6].metric("bt trades", summary["backtest_trades"])
cards[7].metric("skipped", summary["skipped_signals"])

if summary["most_common_skip_reason"]:
    st.warning(f"Most common skipped-signal reason: {summary['most_common_skip_reason']}")

st.subheader("Alpha Data-Quality Mix")
quality_df = pd.DataFrame(
    [{"data_quality": key, "count": value} for key, value in summary["alpha_quality_counts"].items()]
)
if quality_df.empty:
    st.info("No alpha-decay rows yet.")
else:
    st.plotly_chart(px.bar(quality_df, x="data_quality", y="count"), width="stretch")

st.subheader("Raw Response Counts By Endpoint")
if raw_counts.empty:
    st.info("No raw API responses stored yet.")
else:
    st.dataframe(raw_counts, width="stretch", hide_index=True)

st.subheader("API Failures")
if failures.empty:
    st.info("No failed raw responses recorded.")
else:
    st.dataframe(failures, width="stretch", hide_index=True)

st.subheader("Missing / Weak Data")
weak_cols = st.columns(4)
weak_cols[0].metric("missing market category", summary["missing_market_metadata"])
weak_cols[1].metric("missing token IDs", summary["missing_token_ids"])
weak_cols[2].metric("missing price history", summary["missing_price_history"])
weak_cols[3].metric("missing exit prices", summary["missing_exit_prices"])

st.subheader("Alpha Rows With Data Problems")
if affected_alpha.empty:
    st.info("No insufficient/skipped alpha rows found.")
else:
    st.dataframe(affected_alpha, width="stretch", hide_index=True)

st.subheader("Backtest Skipped Signal Reasons")
skip_df = pd.DataFrame(skip_rows)
if skip_df.empty:
    st.info("No skipped backtest signals yet.")
else:
    st.dataframe(skip_df.sort_values("count", ascending=False), width="stretch", hide_index=True)

st.subheader("Alpha Skip Reasons")
alpha_skip_df = pd.DataFrame(alpha_skip_rows)
if alpha_skip_df.empty:
    st.info("No alpha skip reasons recorded.")
else:
    st.dataframe(alpha_skip_df.sort_values("count", ascending=False), width="stretch", hide_index=True)

st.subheader("Trades Missing Token IDs")
if missing_tokens.empty:
    st.info("No ingested trades with missing token_id in the current sample.")
else:
    st.dataframe(missing_tokens, width="stretch", hide_index=True)

st.subheader("Raw JSON Samples")
if raw_samples.empty:
    st.info("No raw samples yet.")
else:
    st.dataframe(raw_samples, width="stretch", hide_index=True)

st.subheader("Data Quality Export")
st.code("python -m pmcopy.cli export-table --table alpha_decay_results --output data/exports/alpha_decay_results.csv", language="powershell")

render_formula_reference(PAGE_SECTIONS["data_quality"])

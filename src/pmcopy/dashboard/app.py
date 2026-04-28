from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import render_formula_reference  # noqa: E402
from pmcopy.db import init_db, session_scope  # noqa: E402
from pmcopy.ingest.discover_wallets import list_candidates  # noqa: E402


st.set_page_config(page_title="Polymarket Copy Research", layout="wide")

config = load_config()
init_db(config)

st.title("Polymarket Copy Research")
st.caption("Public-data research dashboard for discovery, alpha decay, copyability, local backtesting, sensitivity analysis, reports, and data quality. No trading endpoints are used.")

with session_scope(database_url(config)) as session:
    candidates = list_candidates(session, limit=10)

st.subheader("Current Candidate Snapshot")
if candidates.empty:
    st.info("No candidates yet. Open the Candidate Discovery page to run discovery.")
else:
    st.dataframe(candidates, use_container_width=True, hide_index=True)

st.markdown(
    """
    Use the sidebar page navigation to open **Candidate Discovery**, **Wallet Screener**, **Alpha Decay**, **Backtest Lab**, **Sensitivity Analysis**, **Results / Reports**, **Data Quality**, or **Position Reconstruction**.

    This MVP stores all API responses in the local SQLite `raw_responses` table and only uses public read-only endpoints.
    """
)

render_formula_reference(expanded=False)

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import init_db, promote_top_candidates, session_scope  # noqa: E402
from pmcopy.ingest.discover_wallets import (  # noqa: E402
    discover_wallets,
    list_candidates,
    promote_candidates_by_address,
)


st.set_page_config(page_title="Candidate Discovery", layout="wide")

config = load_config()
init_db(config)
discovery_cfg = config.get("wallet_discovery", {})
market_cfg = discovery_cfg.get("markets", {})
category_cfg = discovery_cfg.get("categories", {})

st.title("Candidate Discovery")
st.caption("Discover raw candidate wallets from public data. These are not ranked for profitability or copyability in Phase 1/2.")

with st.form("discovery_form"):
    left, middle, right = st.columns(3)

    with left:
        st.subheader("Categories")
        selected_categories = []
        for category in ["sports", "politics", "crypto", "finance", "culture", "economics", "other"]:
            if st.checkbox(category, value=bool(category_cfg.get(category, False)), key=f"cat_{category}"):
                selected_categories.append(category)

    with middle:
        st.subheader("Sources")
        include_manual_seeds = st.checkbox("manual seed wallets", value=bool(discovery_cfg.get("include_manual_seeds", True)))
        include_leaderboards = st.checkbox("leaderboards if available", value=bool(discovery_cfg.get("include_leaderboards", True)))
        include_market_holders = st.checkbox("holders of selected markets", value=bool(discovery_cfg.get("include_market_holders", True)))
        include_market_activity = st.checkbox("trades/activity of selected markets", value=bool(discovery_cfg.get("include_market_activity", True)))

    with right:
        st.subheader("Limits")
        max_markets_per_category = st.number_input(
            "max markets per category",
            min_value=1,
            max_value=2000,
            value=int(market_cfg.get("max_markets_per_category", 300)),
            step=10,
        )
        min_market_volume = st.number_input(
            "min market volume",
            min_value=0.0,
            value=float(market_cfg.get("min_volume", 10000) or 0),
            step=1000.0,
        )
        min_market_liquidity = st.number_input(
            "min market liquidity",
            min_value=0.0,
            value=float(market_cfg.get("min_liquidity", 1000) or 0),
            step=500.0,
        )
        include_active = st.checkbox("include active markets", value=bool(market_cfg.get("include_active", True)))
        include_closed = st.checkbox("include closed markets", value=bool(market_cfg.get("include_closed", True)))
        max_wallets_total = st.number_input(
            "max wallets total",
            min_value=1,
            max_value=100000,
            value=int(discovery_cfg.get("max_wallets_total", 5000)),
            step=100,
        )

    run_discovery = st.form_submit_button("Run discovery", type="primary")

if run_discovery:
    overrides = {
        "categories": selected_categories,
        "include_manual_seeds": include_manual_seeds,
        "include_leaderboards": include_leaderboards,
        "include_market_holders": include_market_holders,
        "include_market_activity": include_market_activity,
        "max_markets_per_category": int(max_markets_per_category),
        "min_volume": float(min_market_volume),
        "min_liquidity": float(min_market_liquidity),
        "include_active": include_active,
        "include_closed": include_closed,
        "max_wallets_total": int(max_wallets_total),
    }
    with st.spinner("Running public-data discovery..."):
        result = discover_wallets(config, overrides)
    st.success(
        f"Discovery complete: {result.candidates_found} candidates, "
        f"{result.markets_scanned} markets scanned, {result.tokens_upserted} tokens upserted."
    )
    if result.warnings:
        with st.expander("Data-quality and endpoint warnings", expanded=True):
            for warning in result.warnings:
                detail = f" Detail: {warning.detail}" if warning.detail else ""
                st.warning(f"{warning.source}: {warning.message}{detail}")

st.divider()

table_limit = st.slider("candidate table limit", min_value=50, max_value=5000, value=500, step=50)
with session_scope(database_url(config)) as session:
    candidates = list_candidates(session, limit=table_limit)

if candidates.empty:
    st.info("No candidate wallets found yet.")
    st.stop()

metric_left, metric_middle, metric_right = st.columns(3)
metric_left.metric("shown candidates", len(candidates))
metric_middle.metric("promoted shown", int(candidates["promoted"].sum()))
metric_right.metric("avg source count", round(float(candidates["source_count"].mean()), 2))

st.subheader("Candidate Wallets")
st.dataframe(
    candidates[
        [
            "wallet_address",
            "username",
            "sources",
            "source_count",
            "categories",
            "discovery_score",
            "first_source",
            "last_seen_at",
            "promoted",
        ]
    ],
    use_container_width=True,
    hide_index=True,
)

st.subheader("Promote Candidates")
selected_wallets = st.multiselect(
    "Promote selected wallet addresses",
    options=candidates["wallet_address"].tolist(),
)
promote_left, promote_right = st.columns([1, 1])
with promote_left:
    if st.button("Promote selected", disabled=not selected_wallets):
        promoted = promote_candidates_by_address(config, selected_wallets)
        st.success(f"Promoted {promoted} selected candidates.")
        st.rerun()
with promote_right:
    top_n = st.number_input("Top N by discovery score", min_value=1, max_value=10000, value=100, step=50)
    if st.button("Promote top N"):
        with session_scope(database_url(config)) as session:
            promoted = promote_top_candidates(session, int(top_n))
        st.success(f"Promoted {promoted} top candidates.")
        st.rerun()

render_formula_reference(PAGE_SECTIONS["candidate"])

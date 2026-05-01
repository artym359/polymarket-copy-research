from __future__ import annotations

from datetime import datetime, time, timezone
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.backtest.lifecycle_copy import lifecycle_events_dataframe, lifecycle_positions_dataframe  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import LifecycleCopyPosition, Wallet, init_db, json_loads, session_scope  # noqa: E402
from pmcopy.features.position_reconstruction import (  # noqa: E402
    events_dataframe,
    positions_dataframe,
    reconstruction_config_from_values,
    reconstruction_counts,
    reconstruct_promoted_positions,
    reconstruct_wallet_positions,
)


def parse_date(value, *, end: bool = False) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.max if end else time.min, tzinfo=timezone.utc)


def progress_value(index: int, total: int) -> float:
    return min(index / total, 1.0) if total else 1.0


def short_wallet(wallet: str) -> str:
    return wallet if len(wallet) <= 18 else f"{wallet[:8]}...{wallet[-6:]}"


def lifecycle_alpha_summary(session) -> dict[str, object]:
    rows = list(session.scalars(select(LifecycleCopyPosition)))
    closed = [row for row in rows if row.status == "closed" and row.skip_reason is None]
    raw_payloads = [json_loads(row.raw_json, {}) or {} for row in rows]
    return {
        "rows": len(rows),
        "usable": len(closed),
        "pnl": sum(float(row.copied_realized_pnl or 0.0) for row in closed),
        "open": sum(1 for row in rows if row.status == "open"),
        "skipped": sum(1 for row in rows if row.status in {"skipped", "invalid"} or row.skip_reason),
        "cap_hit_count": sum(int(raw.get("cap_hit_count") or 0) for raw in raw_payloads if isinstance(raw, dict)),
        "below_min_trade_count": sum(int(raw.get("below_min_trade_count") or 0) for raw in raw_payloads if isinstance(raw, dict)),
        "skips": {reason: count for reason, count in session.execute(
            select(LifecycleCopyPosition.skip_reason, func.count())
            .where(LifecycleCopyPosition.skip_reason.is_not(None))
            .group_by(LifecycleCopyPosition.skip_reason)
        )},
    }


st.set_page_config(page_title="Position Reconstruction", layout="wide")

config = load_config()
init_db(config)
alpha_defaults = config.get("alpha_decay", {})

st.title("Position Reconstruction")
st.caption("Event-sourced wallet inventory reconstruction from ingested public trade rows.")

with session_scope(database_url(config)) as session:
    wallet_options = sorted(set(session.scalars(select(Wallet.wallet_address).order_by(Wallet.wallet_address))))
    counts = reconstruction_counts(session)
    alpha_summary = lifecycle_alpha_summary(session)

metric_cols = st.columns(6)
metric_cols[0].metric("positions", counts["positions"])
metric_cols[1].metric("closed", counts["closed_positions"])
metric_cols[2].metric("open", counts["open_positions"])
metric_cols[3].metric("partial", counts["partial_positions"])
metric_cols[4].metric("missing inventory", counts["missing_prior_inventory"])
metric_cols[5].metric("orphan sells", counts["orphan_sell"])

with st.form("reconstruction_controls"):
    left, middle, right = st.columns(3)
    with left:
        selected_wallet = st.selectbox("wallet", [""] + wallet_options)
        limit = st.number_input("wallet limit", min_value=1, value=25, step=25)
    with middle:
        analysis_start = st.date_input("analysis start", value=None)
        analysis_end = st.date_input("analysis end", value=None)
    with right:
        warmup_days = st.number_input("warmup days", min_value=0, value=int(alpha_defaults.get("warmup_days", 90)), step=10)
        run_one = st.form_submit_button("Reconstruct selected wallet", type="primary", disabled=not selected_wallet)
        run_many = st.form_submit_button("Reconstruct promoted wallets")

if run_one or run_many:
    recon_config = reconstruction_config_from_values(
        wallet_address=selected_wallet or None,
        analysis_start=parse_date(analysis_start),
        analysis_end=parse_date(analysis_end, end=True),
        warmup_days=int(warmup_days),
    )
    if run_one:
        with st.spinner("Reconstructing wallet positions..."):
            result = reconstruct_wallet_positions(config, selected_wallet, recon_config)
        st.success(
            f"Reconstructed {result.get('positions', 0)} positions: "
            f"closed={result.get('closed_positions', 0)}, "
            f"missing_prior_inventory={result.get('missing_prior_inventory', 0)}, "
            f"orphan_sell={result.get('orphan_sell', 0)}."
        )
    else:
        progress = st.progress(0.0, text="Starting reconstruction...")
        status = st.empty()

        def update(index, total, wallet, result):
            pct = progress_value(index, total)
            progress.progress(pct, text=f"Reconstructing {index}/{total} ({pct:.0%}): {short_wallet(wallet)}")
            if result:
                status.write(
                    f"{short_wallet(wallet)}: {result.get('positions', 0)} positions, "
                    f"{result.get('closed_positions', 0)} closed, "
                    f"{result.get('missing_prior_inventory', 0)} missing inventory"
                )

        result = reconstruct_promoted_positions(config, recon_config=recon_config, limit=int(limit), progress_callback=update)
        st.success(
            f"Reconstructed {result.get('positions', 0)} positions across {result.get('wallets', 0)} wallets."
        )
    st.rerun()

alpha_cols = st.columns(4)
alpha_cols[0].metric("lifecycle copied positions", alpha_summary["rows"])
alpha_cols[1].metric("closed copied positions", alpha_summary["usable"])
alpha_cols[2].metric("lifecycle copy PnL", f"{float(alpha_summary['pnl']):.4f}")
alpha_cols[3].metric("open / skipped", f"{alpha_summary['open']} / {alpha_summary['skipped']}")

cap_cols = st.columns(2)
cap_cols[0].metric("cap hits", alpha_summary["cap_hit_count"])
cap_cols[1].metric("below min trade", alpha_summary["below_min_trade_count"])

with session_scope(database_url(config)) as session:
    positions_df = positions_dataframe(session, selected_wallet or None if "selected_wallet" in locals() else None)
    events_df = events_dataframe(session, selected_wallet or None if "selected_wallet" in locals() else None)
    lifecycle_positions_df = lifecycle_positions_dataframe(session)
    lifecycle_events_df = lifecycle_events_dataframe(session)

st.subheader("Position Lifecycle")
if positions_df.empty:
    st.info("No reconstructed positions yet.")
else:
    st.dataframe(positions_df.head(1000), use_container_width=True, hide_index=True)

st.subheader("Position Events")
if events_df.empty:
    st.info("No reconstructed position events yet.")
else:
    st.dataframe(events_df.head(1000), use_container_width=True, hide_index=True)

st.subheader("Lifecycle Copy Positions")
if lifecycle_positions_df.empty:
    st.info("No lifecycle copy positions yet. Run Backtest Lab with reconstructed_wallet_lifecycle copy mode.")
else:
    st.dataframe(lifecycle_positions_df.head(1000), use_container_width=True, hide_index=True)

st.subheader("Lifecycle Copy Events")
if lifecycle_events_df.empty:
    st.info("No lifecycle copy events yet.")
else:
    st.dataframe(lifecycle_events_df.head(1000), use_container_width=True, hide_index=True)

st.subheader("Skipped Lifecycle Copy Reasons")
skips = pd.DataFrame(
    [{"skip_reason": reason, "count": count} for reason, count in alpha_summary.get("skips", {}).items()]
)
if skips.empty:
    st.info("No reconstructed lifecycle alpha skips yet.")
else:
    st.dataframe(skips.sort_values("count", ascending=False), use_container_width=True, hide_index=True)

render_formula_reference(PAGE_SECTIONS["positions"])

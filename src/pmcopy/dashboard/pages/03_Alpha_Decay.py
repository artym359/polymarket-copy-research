from __future__ import annotations

from datetime import datetime, time, timezone
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import select

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import AlphaDecayResult, Market, Trade, Wallet, init_db, json_loads, session_scope  # noqa: E402
from pmcopy.features.alpha_decay import alpha_config_from_values, compute_alpha_decay  # noqa: E402
from pmcopy.features.classification import classify_all_wallets  # noqa: E402
from pmcopy.features.copyability import compute_copyability, copyability_ranking_dataframe  # noqa: E402
from pmcopy.features.data_quality import QUALITY_RANKS, quality_breakdown  # noqa: E402

EXIT_RULES = ["hold_to_resolution", "fixed_24h", "latest_available", "reconstructed_wallet_lifecycle"]
SIZING_MODES = ["fixed_usd", "proportional_to_whale", "proportional_to_position"]


def parse_date_range(value) -> tuple[datetime | None, datetime | None]:
    if not value:
        return None, None
    if isinstance(value, tuple) and len(value) == 2:
        start, end = value
    elif isinstance(value, list) and len(value) == 2:
        start, end = value
    else:
        return None, None
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end, time.max, tzinfo=timezone.utc),
    )


def alpha_results_dataframe(session) -> pd.DataFrame:
    rows = []
    for row in session.scalars(select(AlphaDecayResult).order_by(AlphaDecayResult.trade_time.desc().nullslast())):
        rows.append(
            {
                "wallet_address": row.wallet_address,
                "trade_id": row.trade_id,
                "token_id": row.token_id,
                "market_id": row.market_id,
                "trade_time": row.trade_time,
                "original_side": row.original_side,
                "whale_price": row.whale_price,
                "whale_size": row.whale_size,
                "delay_seconds": row.delay_seconds,
                "copy_time": row.copy_time,
                "copy_best_bid": row.copy_best_bid,
                "copy_best_ask": row.copy_best_ask,
                "copy_spread": row.copy_spread,
                "simulated_entry_price": row.simulated_entry_price,
                "entry_degradation": row.entry_degradation,
                "liquidity_available": row.liquidity_available,
                "estimated_fee": row.estimated_fee,
                "estimated_slippage": row.estimated_slippage,
                "eventual_exit_price": row.eventual_exit_price,
                "exit_rule": row.exit_rule,
                "gross_pnl": row.gross_pnl,
                "net_pnl": row.net_pnl,
                "data_quality": row.data_quality,
                "data_quality_rank": row.data_quality_rank,
                "skip_reason": row.skip_reason,
                "raw": json_loads(row.raw_json, {}),
            }
        )
    return pd.DataFrame(rows)


def filter_display(df: pd.DataFrame, wallets: list[str], allowed_levels: list[str]) -> pd.DataFrame:
    result = df.copy()
    if wallets:
        result = result[result["wallet_address"].isin(wallets)]
    if allowed_levels:
        result = result[result["data_quality"].isin(allowed_levels)]
    return result


def follow_exit_summary(df: pd.DataFrame) -> dict[str, object]:
    follow = df[df["exit_rule"].isin(["follow_wallet_exit", "reconstructed_wallet_lifecycle"])].copy()
    if follow.empty:
        return {}
    matched = follow[(follow["skip_reason"].isna()) & (follow["net_pnl"].notna())]
    partial = 0
    full = 0
    for raw in matched["raw"]:
        if not isinstance(raw, dict):
            continue
        lifecycle = raw.get("lifecycle", {})
        if isinstance(lifecycle, dict) and lifecycle:
            partial += 1 if lifecycle.get("partial_exit_count", 0) else 0
            full += 1 if lifecycle.get("full_exit_count", 0) else 0
            continue
        exit_payload = raw.get("exit", {})
        if isinstance(exit_payload, dict) and exit_payload.get("exit_kind") == "partial":
            partial += 1
        elif isinstance(exit_payload, dict) and exit_payload.get("exit_kind") == "full":
            full += 1
    holding_seconds = []
    for _, row in matched.iterrows():
        raw = row.get("raw") or {}
        exit_payload = raw.get("exit", {}) if isinstance(raw, dict) else {}
        exit_time = pd.to_datetime(exit_payload.get("exit_time"), utc=True, errors="coerce")
        entry_time = pd.to_datetime(row.get("copy_time"), utc=True, errors="coerce")
        if pd.notna(exit_time) and pd.notna(entry_time):
            holding_seconds.append(max(0.0, (exit_time - entry_time).total_seconds()))
    return {
        "matched": len(matched),
        "no_exit": int(follow["skip_reason"].isin(["no_wallet_exit_found", "no_exit_event"]).sum()),
        "partial": partial,
        "full": full,
        "median_holding_hours": (pd.Series(holding_seconds).median() / 3600) if holding_seconds else None,
    }


st.set_page_config(page_title="Alpha Decay", layout="wide")

config = load_config()
init_db(config)
alpha_defaults = config.get("alpha_decay", {})
quality_options = list(QUALITY_RANKS.keys())

st.title("Alpha Decay")
st.caption("Phase 4 diagnostic simulation. This is not a backtest and does not execute trades.")
st.warning(
    "latest_available, fixed_24h, and hold_to_resolution are diagnostic trade-level modes. "
    "For copy-trading analysis, use Backtest Lab copy mode reconstructed_wallet_lifecycle."
)

with session_scope(database_url(config)) as session:
    wallet_options = list(session.scalars(select(Wallet.wallet_address).order_by(Wallet.wallet_address)))
    category_options = sorted({value for value in session.scalars(select(Market.category).where(Market.category.is_not(None))) if value})

with st.form("alpha_controls"):
    left, middle, right = st.columns(3)
    with left:
        selected_wallets = st.multiselect("select wallets", wallet_options, default=wallet_options[:3])
        selected_delays = st.multiselect(
            "select delays",
            options=[10, 30, 60, 300, 900, 3600, 21600, 86400],
            default=list(alpha_defaults.get("delays_seconds", [10, 30, 60, 300, 900])),
        )
        selected_categories = st.multiselect("select categories", category_options)
    with middle:
        date_range = st.date_input("date range", value=[])
        limit = st.number_input("max source trades per wallet", min_value=1, value=50, step=50)
        position_size = st.number_input(
            "position size USD",
            min_value=0.1,
            value=float(alpha_defaults.get("default_position_size_usd", 2)),
            step=0.5,
        )
    with right:
        max_spread = st.number_input("max spread", min_value=0.0, value=float(alpha_defaults.get("max_spread", 0.03)), step=0.005)
        max_degradation = st.number_input(
            "max entry degradation",
            min_value=0.0,
            value=float(alpha_defaults.get("max_entry_degradation", 0.03)),
            step=0.005,
        )
        allowed_quality = st.multiselect(
            "allowed data-quality levels",
            quality_options,
            default=list(alpha_defaults.get("allowed_data_quality_levels", ["exact_orderbook", "price_history_proxy"])),
        )
        exit_rule = st.selectbox("exit rule", EXIT_RULES, index=0)
        exit_delay_seconds = st.number_input(
            "exit delay seconds",
            min_value=0,
            value=int(alpha_defaults.get("exit_delay_seconds") or 60),
            step=10,
            help="Used by follow_wallet_exit and reconstructed_wallet_lifecycle. Defaults to entry delay in CLI if omitted.",
        )
        min_exit_fraction = st.number_input(
            "min exit fraction",
            min_value=0.01,
            max_value=1.0,
            value=float(alpha_defaults.get("min_exit_fraction", 0.5)),
            step=0.05,
        )
        allow_partial_exits = st.checkbox("allow partial exits", value=bool(alpha_defaults.get("allow_partial_exits", True)))
        sizing_mode = st.selectbox("sizing mode", SIZING_MODES, index=SIZING_MODES.index(str(alpha_defaults.get("sizing_mode", "fixed_usd"))))
        copy_ratio = st.number_input("copy ratio", min_value=0.0, value=float(alpha_defaults.get("copy_ratio", 0.001)), step=0.001, format="%.6f")
        warmup_days = st.number_input("warmup days", min_value=0, value=int(alpha_defaults.get("warmup_days", 90)), step=10)
        historical_mode = st.selectbox(
            "historical mode",
            ["price_history_only", "full"],
            index=0 if alpha_defaults.get("historical_mode", "price_history_only") == "price_history_only" else 1,
        )

    run_compute = st.form_submit_button("Compute Alpha Decay", type="primary")

if run_compute:
    date_start, date_end = parse_date_range(date_range)
    with st.spinner("Computing alpha decay diagnostics..."):
        total = {"wallets": 0, "rows": 0, "skipped_trades": 0}
        wallets_to_run = selected_wallets or [None]
        for wallet in wallets_to_run:
            alpha_cfg = alpha_config_from_values(
                config,
                delays=selected_delays,
                position_size_usd=float(position_size),
                max_spread=float(max_spread),
                max_entry_degradation=float(max_degradation),
                allowed_data_quality=allowed_quality,
                exit_rule=exit_rule,
                limit=int(limit),
                categories=selected_categories or None,
                date_start=date_start,
                date_end=date_end,
                historical_mode=historical_mode,
                exit_delay_seconds=int(exit_delay_seconds) if exit_rule in {"follow_wallet_exit", "reconstructed_wallet_lifecycle"} else None,
                min_exit_fraction=float(min_exit_fraction),
                allow_partial_exits=allow_partial_exits,
                sizing_mode=sizing_mode,
                copy_ratio=float(copy_ratio),
                warmup_days=int(warmup_days),
            )
            result = compute_alpha_decay(config, wallet_address=wallet, alpha_config=alpha_cfg)
            total["wallets"] += result["wallets"]
            total["rows"] += result["rows"]
            total["skipped_trades"] += result["skipped_trades"]
        compute_copyability(config, allowed_data_quality=allowed_quality)
        classify_all_wallets(config)
    st.success(f"Computed {total['rows']} alpha-decay rows across {total['wallets']} wallet selections.")

with session_scope(database_url(config)) as session:
    alpha_df = alpha_results_dataframe(session)
    ranking_df = copyability_ranking_dataframe(session)

if alpha_df.empty:
    st.info("No alpha-decay rows yet. Ingest wallets first, then compute alpha decay.")
    st.stop()

selected_wallet_filter = selected_wallets if "selected_wallets" in locals() else []
quality_filter = allowed_quality if "allowed_quality" in locals() else quality_options
quality_source_df = filter_display(alpha_df, selected_wallet_filter, quality_options)
display_df = filter_display(alpha_df, selected_wallet_filter, quality_filter)

st.subheader("Data Quality Breakdown")
breakdown = quality_breakdown(quality_source_df["data_quality"].tolist())
quality_cols = st.columns(len(breakdown))
for col, (level, share) in zip(quality_cols, breakdown.items()):
    col.metric(level, f"{share:.1%}")

follow_summary = follow_exit_summary(display_df)
if follow_summary:
    st.subheader("Follow Wallet Exit Summary")
    follow_cols = st.columns(5)
    follow_cols[0].metric("matched exits", follow_summary["matched"])
    follow_cols[1].metric("no wallet exit", follow_summary["no_exit"])
    follow_cols[2].metric("full exits", follow_summary["full"])
    follow_cols[3].metric("partial exits", follow_summary["partial"])
    median_holding = follow_summary["median_holding_hours"]
    follow_cols[4].metric("median holding", "" if median_holding is None else f"{float(median_holding):.2f}h")

usable = display_df[(display_df["skip_reason"].isna()) & (display_df["net_pnl"].notna())]
if usable.empty:
    st.warning("No usable alpha-decay rows after filters. Inspect skipped signal reasons below.")
else:
    avg_by_delay = usable.groupby(["wallet_address", "delay_seconds"], as_index=False)["net_pnl"].mean()
    st.subheader("Alpha Decay Curves")
    st.plotly_chart(
        px.line(avg_by_delay, x="delay_seconds", y="net_pnl", color="wallet_address", markers=True, log_x=True),
        use_container_width=True,
    )

    summary_cols = st.columns(3)
    one_min = usable[usable["delay_seconds"] == 60]["net_pnl"].sum()
    five_min = usable[usable["delay_seconds"] == 300]["net_pnl"].sum()
    summary_cols[0].metric("net PnL at 1m", f"{one_min:.4f}")
    summary_cols[1].metric("net PnL at 5m", f"{five_min:.4f}")
    summary_cols[2].metric("usable rows", len(usable))

    chart_left, chart_right = st.columns(2)
    chart_left.plotly_chart(px.histogram(usable, x="entry_degradation", nbins=50, title="Entry Degradation"), use_container_width=True)
    chart_right.plotly_chart(px.histogram(usable, x="copy_spread", nbins=50, title="Spread Distribution"), use_container_width=True)
    st.plotly_chart(px.bar(usable.groupby("delay_seconds", as_index=False)["net_pnl"].sum(), x="delay_seconds", y="net_pnl", title="Net PnL After Fees by Delay"), use_container_width=True)

st.subheader("Wallet Copyability Ranking")
if ranking_df.empty:
    st.info("No wallet copyability rows yet.")
else:
    st.dataframe(ranking_df, use_container_width=True, hide_index=True)

st.subheader("Recent vs Historical Copyability")
if not ranking_df.empty:
    st.dataframe(
        ranking_df[
            [
                "wallet_address",
                "historical_copy_pnl",
                "recent_7d_copy_pnl",
                "recent_30d_copy_pnl",
                "recent_90d_copy_pnl",
                "copyability_trend",
                "copyability_score",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

st.subheader("Simulated Copy Trades")
st.dataframe(display_df.sort_values(["trade_time", "wallet_address", "delay_seconds"], ascending=[False, True, True]).head(1000), use_container_width=True, hide_index=True)

st.subheader("Skipped Signal Reasons")
skips = display_df[display_df["skip_reason"].notna()]
if skips.empty:
    st.info("No skipped alpha-decay rows in the current filter.")
else:
    st.dataframe(skips.groupby(["skip_reason", "data_quality"], as_index=False).size().sort_values("size", ascending=False), use_container_width=True, hide_index=True)

render_formula_reference(PAGE_SECTIONS["alpha"])

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import cleanup_database, init_db, session_scope  # noqa: E402
from pmcopy.features.classification import classify_all_wallets  # noqa: E402
from pmcopy.features.wallet_filters import apply_wallet_filters, screener_counts, wallet_screener_dataframe  # noqa: E402
from pmcopy.features.wallet_metrics import compute_all_wallet_metrics  # noqa: E402
from pmcopy.ingest.ingest_wallet_activity import ingest_promoted_wallets  # noqa: E402


st.set_page_config(page_title="Wallet Screener", layout="wide")

config = load_config()
init_db(config)
filter_defaults = config.get("wallet_filters", {})

st.title("Wallet Screener")
st.caption("Phase 3: promoted wallet ingestion, basic metrics, manual filtering, and initial rule-based classification.")
st.warning("edge_on_volume = PnL / traded volume. It is not true ROI on invested capital.")

with session_scope(database_url(config)) as session:
    counts = screener_counts(session)
    df = wallet_screener_dataframe(session)

metric_cols = st.columns(4)
metric_cols[0].metric("promoted wallets", counts["promoted_wallets"])
metric_cols[1].metric("wallets with trades", counts["wallets_with_ingested_trades"])
metric_cols[2].metric("wallets with metrics", counts["wallets_with_metrics"])
metric_cols[3].metric("class labels", len(counts["by_class_label"]))

if counts["by_class_label"]:
    st.write("Class distribution:", counts["by_class_label"])

pipeline_message = st.session_state.pop("screener_pipeline_result", None)
if pipeline_message:
    st.success(pipeline_message)


def progress_value(index: int, total: int) -> float:
    return min(index / total, 1.0) if total else 1.0


def short_wallet(wallet: str) -> str:
    return wallet if len(wallet) <= 18 else f"{wallet[:8]}...{wallet[-6:]}"


def ingest_with_progress(limit: int | None):
    progress = st.progress(0.0, text="Starting ingest...")
    status = st.empty()

    def update(index, total, wallet, result):
        pct = progress_value(index, total)
        if result is None:
            progress.progress(pct, text=f"Ingesting wallet {index}/{total} ({pct:.0%}): {short_wallet(wallet)}")
            status.info(f"Fetching public Data API rows for {short_wallet(wallet)}")
        else:
            progress.progress(pct, text=f"Ingested wallet {index}/{total} ({pct:.0%}): {short_wallet(wallet)}")
            status.write(
                f"{short_wallet(wallet)}: {result.trades} trades, {result.activity} activity rows, "
                f"{result.positions + result.closed_positions + result.value_snapshots} snapshots"
            )

    return ingest_promoted_wallets(config, limit=limit, progress_callback=update)


def compute_metrics_with_progress(limit: int | None) -> int:
    progress = st.progress(0.0, text="Starting metric computation...")
    status = st.empty()

    def update(index: int, total: int, wallet: str) -> None:
        pct = progress_value(index, total)
        progress.progress(pct, text=f"Computing metrics {index}/{total} ({pct:.0%}): {short_wallet(wallet)}")
        status.info(f"Computing wallet metrics for {short_wallet(wallet)}")

    return compute_all_wallet_metrics(config, limit=limit, progress_callback=update)


def classify_with_progress(limit: int | None) -> int:
    progress = st.progress(0.0, text="Starting classification...")
    status = st.empty()

    def update(index: int, total: int, wallet: str) -> None:
        pct = progress_value(index, total)
        progress.progress(pct, text=f"Classifying wallets {index}/{total} ({pct:.0%}): {short_wallet(wallet)}")
        status.info(f"Classifying {short_wallet(wallet)}")

    return classify_all_wallets(config, limit=limit, progress_callback=update)


def summarize_cleanup(counts: dict[str, int]) -> str:
    changed = {name: count for name, count in counts.items() if count}
    if not changed:
        return "Cleanup completed: no rows were changed."
    return "Cleanup completed: " + ", ".join(f"{name}={count}" for name, count in changed.items())


with st.expander("Run Phase 3 pipeline actions", expanded=False):
    st.warning("These actions call public Data API endpoints and may take time for many promoted wallets.")
    pipeline_cols = st.columns(2)
    if pipeline_cols[0].button(
        "Run ingest + metrics",
        type="primary",
        disabled=counts["promoted_wallets"] == 0,
        help="Ingest all promoted wallets, then compute metrics for every wallet. Does not run classification.",
    ):
        with st.spinner("Ingesting all promoted wallets..."):
            results = ingest_with_progress(limit=None)
        total_trades = sum(result.trades for result in results)
        total_activity = sum(result.activity for result in results)
        total_snapshots = sum(result.positions + result.closed_positions + result.value_snapshots for result in results)
        with st.spinner("Computing wallet metrics for all wallets..."):
            computed = compute_metrics_with_progress(limit=None)
        st.session_state["screener_pipeline_result"] = (
            "Ingest + metrics pipeline completed: "
            f"ingested {len(results)} wallets "
            f"({total_trades} trades, {total_activity} activity rows, {total_snapshots} snapshots), "
            f"computed metrics for {computed}. Classification was not run."
        )
        st.rerun()
    if pipeline_cols[1].button(
        "Run full screener pipeline",
        disabled=counts["promoted_wallets"] == 0,
        help="Ingest all promoted wallets, compute metrics, then classify every wallet.",
    ):
        with st.spinner("Ingesting all promoted wallets..."):
            results = ingest_with_progress(limit=None)
        total_trades = sum(result.trades for result in results)
        total_activity = sum(result.activity for result in results)
        total_snapshots = sum(result.positions + result.closed_positions + result.value_snapshots for result in results)
        with st.spinner("Computing wallet metrics for all wallets..."):
            computed = compute_metrics_with_progress(limit=None)
        with st.spinner("Classifying all wallets..."):
            classified = classify_with_progress(limit=None)
        st.session_state["screener_pipeline_result"] = (
            "Full screener pipeline completed: "
            f"ingested {len(results)} wallets "
            f"({total_trades} trades, {total_activity} activity rows, {total_snapshots} snapshots), "
            f"computed metrics for {computed}, classified {classified}."
        )
        st.rerun()
    st.caption("Pipeline buttons ignore the per-step limits below and process every promoted wallet.")
    st.divider()
    action_cols = st.columns(3)
    ingest_limit = action_cols[0].number_input("ingestion wallet limit", min_value=1, max_value=10000, value=10, step=10)
    if action_cols[0].button("Ingest promoted wallets"):
        with st.spinner("Ingesting promoted wallets..."):
            results = ingest_with_progress(limit=int(ingest_limit))
        st.success(f"Ingested {len(results)} wallets.")
        st.rerun()
    metric_limit = action_cols[1].number_input("metrics wallet limit", min_value=1, max_value=10000, value=100, step=50)
    if action_cols[1].button("Compute wallet metrics"):
        with st.spinner("Computing metrics..."):
            computed = compute_metrics_with_progress(limit=int(metric_limit))
        st.success(f"Computed metrics for {computed} wallets.")
        st.rerun()
    class_limit = action_cols[2].number_input("classification wallet limit", min_value=1, max_value=10000, value=100, step=50)
    if action_cols[2].button("Classify wallets"):
        with st.spinner("Classifying wallets..."):
            classified = classify_with_progress(limit=int(class_limit))
        st.success(f"Classified {classified} wallets.")
        st.rerun()

with st.expander("Reset / Cleanup", expanded=False):
    st.warning("Cleanup actions modify the local SQLite database. They do not affect Polymarket or any external account.")
    confirm_cleanup = st.checkbox("I understand this will delete local research data", key="confirm_screener_cleanup")
    cleanup_cols = st.columns(5)
    cleanup_actions = [
        (
            "Clear classifications",
            "classifications",
            "Deletes wallet_classification only. Metrics and ingested data remain.",
        ),
        (
            "Clear metrics + classifications",
            "metrics",
            "Deletes wallet_metrics and wallet_classification. Ingested trades remain.",
        ),
        (
            "Clear ingested data",
            "ingested",
            "Deletes trades, activity, wallet snapshots, metrics, classifications, alpha decay, backtests, and sensitivity results. Promoted wallets remain.",
        ),
        (
            "Clear promoted wallets",
            "promoted",
            "Deletes promoted wallets and downstream data, and resets candidate promoted flags. Candidate discovery rows remain.",
        ),
        (
            "Reset all local data",
            "all",
            "Deletes every local research table row, including candidates, markets, tokens, raw responses, and reports.",
        ),
    ]
    for col, (label, scope, help_text) in zip(cleanup_cols, cleanup_actions):
        if col.button(label, disabled=not confirm_cleanup, help=help_text):
            with st.spinner(f"Running cleanup: {label}..."):
                with session_scope(database_url(config)) as session:
                    counts_deleted = cleanup_database(session, scope)
            st.session_state["screener_pipeline_result"] = summarize_cleanup(counts_deleted)
            st.rerun()

if df.empty:
    st.info("No promoted wallets found. Promote candidates on the Candidate Discovery page first.")
    st.stop()

if not df["has_metrics"].all():
    missing = int((~df["has_metrics"]).sum())
    st.warning(f"{missing} promoted wallets do not have computed metrics yet.")
if not df["has_classification"].all():
    missing = int((~df["has_classification"]).sum())
    st.warning(f"{missing} promoted wallets do not have classifications yet.")
if "max_exposure_confidence" in df.columns:
    unavailable_share = (df["max_exposure_confidence"].fillna("unavailable") == "unavailable").mean()
    if unavailable_share >= 0.5:
        st.warning("Exposure metrics are unavailable for many wallets. Reconstruct positions or ingest position snapshots to improve coverage.")

st.subheader("Filters")
left, middle, right = st.columns(3)

with left:
    min_pnl = st.number_input("min PnL", value=float(filter_defaults.get("min_total_pnl") or 0.0), step=100.0)
    min_edge = st.number_input("min edge_on_volume", value=0.0, step=0.01, format="%.4f")
    use_min_edge = st.checkbox(
        "apply min edge_on_volume filter",
        value=filter_defaults.get("min_edge_on_volume", filter_defaults.get("min_roi_on_volume")) is not None,
    )
    min_volume = st.number_input("min volume", value=float(filter_defaults.get("min_total_volume") or 0.0), step=1000.0)
    min_trades = st.number_input("min trades", value=int(filter_defaults.get("min_trades", 100) or 0), step=10)

with middle:
    min_return_max = st.number_input("min return_on_max_capital_at_risk", value=0.0, step=0.01, format="%.4f")
    use_min_return_max = st.checkbox(
        "apply return_on_max_capital_at_risk filter",
        value=filter_defaults.get("min_return_on_max_capital_at_risk") is not None,
    )
    min_return_average = st.number_input("min return_on_average_capital_at_risk", value=0.0, step=0.01, format="%.4f")
    use_min_return_average = st.checkbox(
        "apply return_on_average_capital_at_risk filter",
        value=filter_defaults.get("min_return_on_average_capital_at_risk") is not None,
    )
    min_markets = st.number_input("min markets", value=int(filter_defaults.get("min_markets", 20) or 0), step=5)
    min_active_days = st.number_input("min active days", value=int(filter_defaults.get("min_active_days", 14) or 0), step=1)
    max_drawdown = st.number_input("max drawdown", value=0.0, step=0.05, format="%.4f")
    use_max_drawdown = st.checkbox("apply max drawdown filter", value=filter_defaults.get("max_drawdown") is not None)

exposure_filter_options = [
    "reconstructed_positions",
    "reconstructed_positions_time_weighted",
    "data_api_proxy",
    "snapshots_proxy",
    "unavailable",
]
with right:
    exposure_confidence_filter = st.multiselect(
        "exposure confidence filter",
        exposure_filter_options,
        default=[value for value in filter_defaults.get("exposure_confidence_filter", []) if value in exposure_filter_options],
    )

concentration_left, concentration_right = st.columns(2)
with concentration_left:
    max_top_1 = st.slider(
        "max top 1 market concentration",
        min_value=0.0,
        max_value=1.0,
        value=float(filter_defaults.get("max_top_1_market_pnl_share", 0.60) or 1.0),
        step=0.05,
    )
with concentration_right:
    max_top_5 = st.slider(
        "max top 5 market concentration",
        min_value=0.0,
        max_value=1.0,
        value=float(filter_defaults.get("max_top_5_market_pnl_share", 0.85) or 1.0),
        step=0.05,
    )

with right:
    category_options = sorted({category for values in df["categories"] for category in (values or [])})
    include_default = [category for category in filter_defaults.get("include_categories", []) if category in category_options]
    exclude_default = [category for category in filter_defaults.get("exclude_categories", []) if category in category_options]
    include_categories = st.multiselect("include categories", category_options, default=include_default)
    exclude_categories = st.multiselect("exclude categories", category_options, default=exclude_default)
    exclude_market_makers = st.checkbox(
        "exclude likely market makers",
        value=bool(filter_defaults.get("exclude_likely_market_makers", True)),
    )
    exclude_lucky = st.checkbox("exclude lucky wallets", value=bool(filter_defaults.get("exclude_lucky_wallets", True)))
    exclude_insufficient = st.checkbox(
        "exclude insufficient sample",
        value=bool(filter_defaults.get("exclude_insufficient_sample", True)),
    )

filters = {
    "min_total_pnl": min_pnl,
    "min_edge_on_volume": min_edge if use_min_edge else None,
    "min_roi_on_volume": min_edge if use_min_edge else None,
    "min_return_on_max_capital_at_risk": min_return_max if use_min_return_max else None,
    "min_return_on_average_capital_at_risk": min_return_average if use_min_return_average else None,
    "min_total_volume": min_volume,
    "min_trades": int(min_trades),
    "min_markets": int(min_markets),
    "min_active_days": int(min_active_days),
    "max_drawdown": max_drawdown if use_max_drawdown else None,
    "max_top_1_market_pnl_share": max_top_1,
    "max_top_5_market_pnl_share": max_top_5,
    "include_categories": include_categories,
    "exclude_categories": exclude_categories,
    "exposure_confidence_filter": exposure_confidence_filter,
    "exclude_likely_market_makers": exclude_market_makers,
    "exclude_lucky_wallets": exclude_lucky,
    "exclude_insufficient_sample": exclude_insufficient,
}

filtered = apply_wallet_filters(df, filters)
st.subheader("Filtered Wallets")
st.caption(f"{len(filtered)} of {len(df)} promoted wallets pass current filters.")

table = filtered[
    [
        "wallet",
        "username",
        "class_label",
        "total_pnl",
        "volume",
        "edge_on_volume",
        "pnl_per_traded_dollar",
        "max_capital_at_risk",
        "return_on_max_capital_at_risk",
        "max_exposure_confidence",
        "average_capital_at_risk",
        "return_on_average_capital_at_risk",
        "average_exposure_confidence",
        "trade_count",
        "market_count",
        "active_days",
        "max_drawdown",
        "top_1_market_pnl_share",
        "top_5_market_pnl_share",
        "main_category",
        "classification_reasons",
    ]
].copy()
table.insert(0, "selected", False)
st.data_editor(table, use_container_width=True, hide_index=True, disabled=[column for column in table.columns if column != "selected"])

with st.expander("Missing and approximate data notes", expanded=False):
    st.markdown(
        """
        - `total_pnl`, `realized_pnl`, and `unrealized_pnl` come from Data API position fields when available.
        - `edge_on_volume` and `pnl_per_traded_dollar` are aliases for PnL divided by traded volume.
        - Exposure metrics use reconstructed positions when available, then Data API position-value proxies, otherwise null.
        - `max_drawdown` is null in Phase 3 because no historical equity curve is ingested.
        - Concentration metrics are null when per-market PnL is unavailable or sums to zero.
        - `likely_latency_bot` is not classified until alpha decay exists in Phase 4.
        """
    )

render_formula_reference(PAGE_SECTIONS["wallet"])

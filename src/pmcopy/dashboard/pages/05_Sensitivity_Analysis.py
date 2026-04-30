from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import select

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.backtest.sensitivity import (  # noqa: E402
    prepare_heatmap_data,
    run_sensitivity,
    sensitivity_config_from_values,
    sensitivity_results_dataframe,
)
from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import AlphaDecayResult, BacktestRun, SensitivityRun, init_db, json_loads, session_scope  # noqa: E402
from pmcopy.features.data_quality import QUALITY_RANKS  # noqa: E402

EXIT_RULES = ["hold_to_resolution", "fixed_24h", "latest_available", "reconstructed_wallet_lifecycle"]


def parse_int_list(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def latest_sensitivity_run(session) -> str | None:
    row = session.scalars(select(SensitivityRun).order_by(SensitivityRun.created_at.desc())).first()
    return row.sensitivity_run_id if row else None


def backtest_run_options(session) -> list[str]:
    rows = list(session.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(25)))
    return ["config/default.yaml"] + [row.run_id for row in rows]


def backtest_overrides_from_run(config: dict, run_id: str) -> dict:
    if run_id == "config/default.yaml":
        return {}
    allowed = {
        "date_start",
        "date_end",
        "train_start",
        "train_end",
        "validation_start",
        "validation_end",
        "test_start",
        "test_end",
        "initial_capital",
        "max_wallet_exposure_usd",
        "max_category_exposure_usd",
        "max_daily_loss_usd",
        "duplicate_signal_window_seconds",
        "include_categories",
        "exclude_categories",
        "skip_likely_market_makers",
        "skip_likely_latency_bots",
        "skip_lucky_wallets",
        "skip_insufficient_sample",
        "min_copyability_score",
    }
    with session_scope(database_url(config)) as session:
        row = session.get(BacktestRun, run_id)
        payload = json_loads(row.config_json, {}) if row else {}
    return {key: value for key, value in payload.items() if key in allowed}


st.set_page_config(page_title="Sensitivity Analysis", layout="wide")

config = load_config()
init_db(config)
defaults = config.get("sensitivity", {})
quality_options = list(QUALITY_RANKS.keys())

st.title("Sensitivity Analysis")
st.caption("Phase 6 diagnostic grid. This is not an optimizer and does not select a best configuration.")

with session_scope(database_url(config)) as session:
    wallet_options = sorted(set(session.scalars(select(AlphaDecayResult.wallet_address).distinct())))
    base_options = backtest_run_options(session)

with st.form("sensitivity_controls"):
    left, middle, right = st.columns(3)
    with left:
        base_source = st.selectbox("base backtest config", base_options)
        selected_wallets = st.multiselect("wallets", wallet_options)
        mode = st.selectbox("mode", ["in_sample", "split", "walk_forward"], index=0)
        allowed_quality = st.multiselect("allowed data-quality levels", quality_options, default=["price_history_proxy"])
        exit_rule = st.selectbox("exit rule", EXIT_RULES, index=2)
    with middle:
        copy_delays = st.text_input("copy delays", value=",".join(str(value) for value in defaults.get("copy_delay_seconds", [10, 60, 300, 900])))
        max_degradations = st.text_input(
            "max entry degradations",
            value=",".join(str(value) for value in defaults.get("max_entry_degradation", [0.01, 0.02, 0.03])),
        )
        max_spreads = st.text_input("max spreads", value=",".join(str(value) for value in defaults.get("max_spread", [0.01, 0.02, 0.03, 0.05])))
    with right:
        position_sizes = st.text_input("position sizes", value=",".join(str(value) for value in defaults.get("position_size_usd", [1, 2, 5])))
        market_exposures = st.text_input(
            "max market exposures",
            value=",".join(str(value) for value in defaults.get("max_market_exposure_usd", [5, 8, 10])),
        )
        max_combinations = st.number_input("max combinations", min_value=1, max_value=10000, value=24, step=1)
        confirm_large_grid = st.checkbox("confirm large grid", value=False)

    run_clicked = st.form_submit_button("Run Sensitivity", type="primary")

result = None
if run_clicked:
    try:
        sensitivity_config = sensitivity_config_from_values(
            config,
            mode=mode,
            selected_wallets=selected_wallets or None,
            copy_delays=parse_int_list(copy_delays),
            max_entry_degradations=parse_float_list(max_degradations),
            max_spreads=parse_float_list(max_spreads),
            position_sizes=parse_float_list(position_sizes),
            max_market_exposures=parse_float_list(market_exposures),
            allowed_data_quality=allowed_quality,
            exit_rule=exit_rule,
            limit_combinations=int(max_combinations),
            confirm_large_grid=confirm_large_grid,
            **backtest_overrides_from_run(config, base_source),
        )
        with st.spinner("Running sensitivity grid..."):
            result = run_sensitivity(config, sensitivity_config)
        st.success(f"Completed {result['tested_combinations']} combinations.")
    except ValueError as exc:
        st.warning(str(exc))
    except Exception as exc:
        st.error(f"Sensitivity run failed: {exc}")

with session_scope(database_url(config)) as session:
    active_run_id = result["sensitivity_run_id"] if result else latest_sensitivity_run(session)
    df = sensitivity_results_dataframe(session, active_run_id)
    run_row = session.get(SensitivityRun, active_run_id) if active_run_id else None

if df.empty:
    st.info("No sensitivity results yet. Run a small grid after alpha decay and backtests are available.")
    st.stop()

robustness = json_loads(run_row.result_json, {}).get("robustness", {}) if run_row else result.get("robustness", {})
st.subheader("Robustness Diagnostics")
cols = st.columns(4)
cols[0].metric("label", robustness.get("label", "unknown"))
cols[1].metric("positive combos", robustness.get("positive_combinations", 0))
cols[2].metric("negative combos", robustness.get("negative_combinations", 0))
cols[3].metric("neighbor pairs", robustness.get("neighboring_positive_pairs", 0))
for reason in robustness.get("reasons", []):
    st.warning(reason)

st.subheader("Parameter Combinations")
st.dataframe(df, use_container_width=True, hide_index=True)
st.download_button(
    "Export sensitivity CSV",
    df.to_csv(index=False).encode("utf-8"),
    file_name=f"{active_run_id}_sensitivity.csv",
    mime="text/csv",
)

rows = df.to_dict("records")
heat_cols = st.columns(3)
for col, metric, title in (
    (heat_cols[0], "roi", "ROI by Delay x Degradation"),
    (heat_cols[1], "max_drawdown", "Max Drawdown by Delay x Degradation"),
    (heat_cols[2], "trade_count", "Trade Count by Delay x Degradation"),
):
    heat_df = pd.DataFrame(prepare_heatmap_data(rows, metric))
    if not heat_df.empty:
        pivot = heat_df.pivot(index="max_entry_degradation", columns="copy_delay_seconds", values=metric)
        col.plotly_chart(px.imshow(pivot, text_auto=True, aspect="auto", title=title), use_container_width=True)

chart_left, chart_right = st.columns(2)
chart_left.plotly_chart(px.scatter(df, x="max_spread", y="roi", color="copy_delay_seconds", size="trade_count", title="ROI vs Max Spread"), use_container_width=True)
chart_right.plotly_chart(px.scatter(df, x="position_size_usd", y="roi", color="max_market_exposure_usd", size="trade_count", title="ROI vs Position Size"), use_container_width=True)

st.subheader("Warning Flags")
flags = []
for _, row in df.iterrows():
    for flag in str(row.get("warning_flags") or "").split(","):
        cleaned = flag.strip()
        if cleaned:
            flags.append(cleaned)
if flags:
    counts = pd.Series(flags).value_counts().reset_index()
    counts.columns = ["warning_flag", "count"]
    st.dataframe(counts, use_container_width=True, hide_index=True)
else:
    st.info("No warning flags in this sensitivity run.")

render_formula_reference(PAGE_SECTIONS["sensitivity"])

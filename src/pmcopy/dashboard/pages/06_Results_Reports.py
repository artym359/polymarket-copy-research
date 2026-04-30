from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import BacktestRun, init_db, json_loads, session_scope  # noqa: E402
from pmcopy.reports import (  # noqa: E402
    backtest_runs_dataframe,
    best_worst_from_group,
    copyability_dataframe,
    sensitivity_results_dataframe,
    sensitivity_summary_dataframe,
    verdict_for_run,
)


def equity_curves_dataframe(runs: list[BacktestRun]) -> pd.DataFrame:
    rows = []
    for run in runs:
        result = json_loads(run.result_json, {}) or {}
        for point in result.get("metrics", {}).get("equity_curve", []):
            rows.append({"run_id": run.run_id, "timestamp": point.get("timestamp"), "equity": point.get("equity")})
    return pd.DataFrame(rows)


def periods_dataframe(run: BacktestRun) -> pd.DataFrame:
    result = json_loads(run.result_json, {}) or {}
    rows = []
    for label, metrics in result.get("periods", {}).items():
        rows.append(
            {
                "period": label,
                "total_pnl": metrics.get("total_pnl"),
                "roi": metrics.get("roi"),
                "max_drawdown": metrics.get("max_drawdown"),
                "trade_count": metrics.get("trade_count"),
                "win_rate": metrics.get("win_rate"),
            }
        )
    return pd.DataFrame(rows)


def walk_forward_dataframe(run: BacktestRun) -> pd.DataFrame:
    result = json_loads(run.result_json, {}) or {}
    return pd.DataFrame(result.get("walk_forward_windows", []))


st.set_page_config(page_title="Results / Reports", layout="wide")

config = load_config()
init_db(config)

st.title("Results / Reports")
st.caption("Compare saved research runs, inspect warnings, and export cautious report artifacts. Diagnostic only, not financial advice.")

with session_scope(database_url(config)) as session:
    runs_df = backtest_runs_dataframe(session, config)
    run_map = {row.run_id: row for row in session.query(BacktestRun).order_by(BacktestRun.created_at.desc()).all()}
    copy_df = copyability_dataframe(session)
    sens_summary = sensitivity_summary_dataframe(session)
    sens_results = sensitivity_results_dataframe(session)

if runs_df.empty:
    st.info("No backtest runs yet. Run Backtest Lab or `python -m pmcopy.cli run-backtest` first.")
    st.stop()

st.subheader("Backtest Runs")
st.dataframe(runs_df, width="stretch", hide_index=True)

run_options = runs_df["run_id"].tolist()
selected_runs = st.multiselect("compare runs", run_options, default=run_options[: min(3, len(run_options))])
selected_detail = st.selectbox("inspect run", run_options)

selected_run_rows = [run_map[run_id] for run_id in selected_runs if run_id in run_map]
equity_df = equity_curves_dataframe(selected_run_rows)
if not equity_df.empty:
    st.subheader("Equity Curves")
    st.plotly_chart(px.line(equity_df, x="timestamp", y="equity", color="run_id"), width="stretch")

detail_run = run_map[selected_detail]
verdict = verdict_for_run(detail_run, config)
st.subheader("Transparent Verdict")
verdict_cols = st.columns(4)
verdict_cols[0].metric("verdict", verdict["verdict"])
verdict_cols[1].metric("ROI", f"{verdict['roi']:.2%}")
verdict_cols[2].metric("trades", verdict["trade_count"])
verdict_cols[3].metric("proxy share", f"{verdict['proxy_share']:.1%}")
for reason in verdict["reasons"]:
    st.warning(reason)
for warning in verdict["warnings"]:
    st.info(warning)

period_df = periods_dataframe(detail_run)
if not period_df.empty:
    st.subheader("Train / Validation / Test")
    st.dataframe(period_df, width="stretch", hide_index=True)

walk_df = walk_forward_dataframe(detail_run)
if not walk_df.empty:
    st.subheader("Walk-Forward Windows")
    st.dataframe(walk_df, width="stretch", hide_index=True)

result = json_loads(detail_run.result_json, {}) or {}
metrics = result.get("metrics", {})
best_wallet, worst_wallet = best_worst_from_group(metrics, "pnl_by_wallet")
best_category, worst_category = best_worst_from_group(metrics, "pnl_by_category")
best_market, worst_market = best_worst_from_group(metrics, "pnl_by_market")

st.subheader("Drivers")
driver_cols = st.columns(3)
driver_cols[0].write("Best / worst wallets")
driver_cols[0].dataframe(pd.DataFrame([{"side": "best", **best_wallet}, {"side": "worst", **worst_wallet}]), width="stretch", hide_index=True)
driver_cols[1].write("Best / worst categories")
driver_cols[1].dataframe(pd.DataFrame([{"side": "best", **best_category}, {"side": "worst", **worst_category}]), width="stretch", hide_index=True)
driver_cols[2].write("Best / worst markets")
driver_cols[2].dataframe(pd.DataFrame([{"side": "best", **best_market}, {"side": "worst", **worst_market}]), width="stretch", hide_index=True)

st.subheader("Sensitivity Summary")
if sens_summary.empty:
    st.info("No sensitivity runs yet.")
else:
    st.dataframe(sens_summary, width="stretch", hide_index=True)
    if not sens_results.empty:
        best = sens_results.sort_values("roi", ascending=False).head(5)
        worst = sens_results.sort_values("roi", ascending=True).head(5)
        sens_cols = st.columns(2)
        sens_cols[0].write("Best configs by ROI")
        sens_cols[0].dataframe(best, width="stretch", hide_index=True)
        sens_cols[1].write("Worst configs by ROI")
        sens_cols[1].dataframe(worst, width="stretch", hide_index=True)

st.subheader("Recent vs Historical Copyability")
if copy_df.empty:
    st.info("No copyability rows yet.")
else:
    columns = [
        "wallet_address",
        "historical_copy_pnl",
        "recent_7d_copy_pnl",
        "recent_30d_copy_pnl",
        "recent_90d_copy_pnl",
        "copyability_trend",
        "copyability_score",
    ]
    st.dataframe(copy_df[[column for column in columns if column in copy_df.columns]], width="stretch", hide_index=True)
    decaying = copy_df[copy_df.get("copyability_trend", "") == "decaying"] if "copyability_trend" in copy_df else pd.DataFrame()
    if not decaying.empty:
        st.warning("Some wallets show good historical alpha but decaying recent copyability.")

st.subheader("Report Exports")
st.code(f"python -m pmcopy.cli export-report --run-id {selected_detail}", language="powershell")

render_formula_reference(PAGE_SECTIONS["reports"])

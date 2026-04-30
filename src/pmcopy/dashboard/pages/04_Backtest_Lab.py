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

from pmcopy.backtest.simulator import backtest_config_from_values, run_backtest  # noqa: E402
from pmcopy.config import database_url, load_config  # noqa: E402
from pmcopy.dashboard.formulas import PAGE_SECTIONS, render_formula_reference  # noqa: E402
from pmcopy.db import (  # noqa: E402
    AlphaDecayResult,
    BacktestRun,
    BacktestTrade,
    Market,
    ReconstructedPosition,
    ReconstructedPositionEvent,
    SkippedSignal,
    init_db,
    json_loads,
    session_scope,
)
from pmcopy.features.data_quality import QUALITY_RANKS  # noqa: E402

EXIT_RULES = ["hold_to_resolution", "fixed_24h", "latest_available"]
COPY_MODES = ["diagnostic_trade_level", "reconstructed_wallet_lifecycle"]
SIZING_MODES = ["proportional_to_whale_with_cap"]
WHALE_EXIT_EVENTS = {"partial_exit", "reduce_position", "full_exit"}


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


def parse_one_date(value, *, end: bool = False) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.max if end else time.min, tzinfo=timezone.utc)


def to_utc_datetime(value) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def trades_dataframe(session, run_id: str) -> pd.DataFrame:
    rows = []
    for row in session.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_time)):
        rows.append(
            {
                "period_label": row.period_label,
                "wallet_address": row.wallet_address,
                "source_trade_id": row.source_trade_id,
                "market_id": row.market_id,
                "token_id": row.token_id,
                "side": row.side,
                "signal_time": row.signal_time,
                "entry_time": row.entry_time,
                "entry_price": row.entry_price,
                "size_usd": row.size_usd,
                "exit_time": row.exit_time,
                "exit_price": row.exit_price,
                "gross_pnl": row.gross_pnl,
                "fee": row.fee,
                "slippage": row.slippage,
                "net_pnl": row.net_pnl,
                "data_quality": row.data_quality,
                "skip_reason": row.skip_reason,
            }
        )
    return pd.DataFrame(rows)


def skipped_dataframe(session, run_id: str) -> pd.DataFrame:
    rows = []
    for row in session.scalars(select(SkippedSignal).where(SkippedSignal.run_id == run_id).order_by(SkippedSignal.timestamp)):
        rows.append(
            {
                "wallet_address": row.wallet_address,
                "source_trade_id": row.source_trade_id,
                "market_id": row.market_id,
                "token_id": row.token_id,
                "timestamp": row.timestamp,
                "reason": row.reason,
                "details": json_loads(row.details_json, {}),
            }
        )
    return pd.DataFrame(rows)


def latest_run(session) -> BacktestRun | None:
    return session.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc())).first()


def whale_strategy_dataframe(
    session,
    wallets: list[str],
    date_start: datetime | None,
    date_end: datetime | None,
) -> pd.DataFrame:
    stmt = (
        select(ReconstructedPosition)
        .where(ReconstructedPosition.realized_pnl.is_not(None))
        .where(ReconstructedPosition.status != "missing_prior_inventory")
        .order_by(ReconstructedPosition.opened_at, ReconstructedPosition.position_id)
    )
    if wallets:
        stmt = stmt.where(ReconstructedPosition.wallet_address.in_(wallets))
    positions = list(session.scalars(stmt))
    if not positions:
        return pd.DataFrame()

    position_ids = [row.position_id for row in positions]
    event_rows = session.execute(
        select(ReconstructedPositionEvent.position_id, ReconstructedPositionEvent.timestamp)
        .where(ReconstructedPositionEvent.position_id.in_(position_ids))
        .where(ReconstructedPositionEvent.event_type.in_(WHALE_EXIT_EVENTS))
    )
    last_exit_by_position: dict[str, datetime] = {}
    for position_id, timestamp in event_rows:
        timestamp = to_utc_datetime(timestamp)
        if timestamp is None:
            continue
        current = last_exit_by_position.get(position_id)
        if current is None or timestamp > current:
            last_exit_by_position[position_id] = timestamp

    realized_rows = []
    for position in positions:
        timestamp = to_utc_datetime(position.closed_at) or last_exit_by_position.get(position.position_id) or to_utc_datetime(position.opened_at)
        if timestamp is None:
            continue
        if date_start and timestamp < date_start:
            continue
        if date_end and timestamp > date_end:
            continue
        realized_rows.append(
            {
                "timestamp": timestamp,
                "wallet_address": position.wallet_address,
                "position_id": position.position_id,
                "market_id": position.market_id,
                "token_id": position.token_id,
                "position_status": position.status,
                "realized_pnl": float(position.realized_pnl or 0.0),
            }
        )
    if not realized_rows:
        return pd.DataFrame()

    realized_rows.sort(key=lambda item: (item["timestamp"], item["position_id"]))
    cumulative = 0.0
    curve = [
        {
            "timestamp": date_start or realized_rows[0]["timestamp"],
            "cumulative_realized_pnl": 0.0,
            "realized_pnl": 0.0,
            "wallet_address": "",
            "position_id": "",
            "market_id": "",
            "token_id": "",
            "position_status": "",
        }
    ]
    for row in realized_rows:
        cumulative += row["realized_pnl"]
        curve.append({**row, "cumulative_realized_pnl": cumulative})
    return pd.DataFrame(curve)


st.set_page_config(page_title="Backtest Lab", layout="wide")

config = load_config()
init_db(config)
backtest_defaults = config.get("backtest", {})
sizing_defaults = config.get("sizing", {})
quality_options = list(QUALITY_RANKS.keys())

st.title("Backtest Lab")
st.caption("Phase 5 local simulation from stored alpha-decay rows. No trading endpoints or order placement are used.")

with session_scope(database_url(config)) as session:
    wallet_options = sorted(
        set(session.scalars(select(AlphaDecayResult.wallet_address).distinct()))
        | set(session.scalars(select(ReconstructedPosition.wallet_address).distinct()))
    )
    category_options = sorted({value for value in session.scalars(select(Market.category).where(Market.category.is_not(None))) if value})

copy_mode = st.selectbox(
    "copy mode",
    COPY_MODES,
    index=COPY_MODES.index(str(backtest_defaults.get("copy_mode", "diagnostic_trade_level"))),
)

with st.form("backtest_controls"):
    left, middle, right = st.columns(3)
    with left:
        selected_wallets = st.multiselect("wallet selection", wallet_options)
        mode = st.selectbox("backtest mode", ["in_sample", "split", "walk_forward"], index=0)
        date_range = st.date_input("date range", value=[])
        if copy_mode == "diagnostic_trade_level":
            selected_categories = st.multiselect("include categories", category_options)
            excluded_categories = st.multiselect("exclude categories", category_options)
        else:
            selected_categories = []
            excluded_categories = []
    with middle:
        initial_capital = st.number_input("initial capital", min_value=1.0, value=float(backtest_defaults.get("initial_capital", 100)), step=10.0)
        if copy_mode == "diagnostic_trade_level":
            position_size = st.number_input("position size USD", min_value=0.1, value=float(backtest_defaults.get("position_size_usd", 2)), step=0.5)
            copy_delay = st.number_input("copy delay seconds", min_value=1, value=int(backtest_defaults.get("copy_delay_seconds", 60)), step=10)
            entry_delay = copy_delay
            exit_delay = copy_delay
            max_spread = st.number_input("max spread", min_value=0.0, value=float(backtest_defaults.get("max_spread", 0.03)), step=0.005)
            max_degradation = st.number_input(
                "max entry degradation",
                min_value=0.0,
                value=float(backtest_defaults.get("max_entry_degradation", 0.03)),
                step=0.005,
            )
            exit_rule = st.selectbox("exit rule", EXIT_RULES, index=2)
        else:
            position_size = float(backtest_defaults.get("position_size_usd", 2))
            entry_delay = st.number_input(
                "entry delay seconds",
                min_value=1,
                value=int(backtest_defaults.get("entry_delay_seconds", backtest_defaults.get("copy_delay_seconds", 60))),
                step=10,
            )
            exit_delay = st.number_input(
                "exit delay seconds",
                min_value=1,
                value=int(backtest_defaults.get("exit_delay_seconds", backtest_defaults.get("copy_delay_seconds", 60))),
                step=10,
            )
            copy_delay = entry_delay
            max_spread = float(backtest_defaults.get("max_spread", 0.03))
            max_degradation = float(backtest_defaults.get("max_entry_degradation", 0.03))
            exit_rule = "reconstructed_wallet_lifecycle"
        allowed_quality = st.multiselect(
            "allowed data-quality levels",
            quality_options,
            default=list(backtest_defaults.get("allowed_data_quality_levels", ["exact_orderbook", "price_history_proxy"])),
        )
    with right:
        if copy_mode == "reconstructed_wallet_lifecycle":
            sizing_mode = st.selectbox("sizing mode", SIZING_MODES, index=0)
            copy_ratio = st.number_input("copy ratio", min_value=0.0, value=float(sizing_defaults.get("copy_ratio", 0.001)), step=0.001, format="%.6f")
            max_position_budget = st.number_input(
                "max position budget USD",
                min_value=0.0,
                value=float(sizing_defaults.get("max_position_budget_usd", 10)),
                step=1.0,
            )
            min_trade_usd = st.number_input("min trade USD", min_value=0.0, value=float(sizing_defaults.get("min_trade_usd", 1)), step=0.5)
            allow_cap_partial = st.checkbox(
                "allow cap partial fill",
                value=bool(sizing_defaults.get("allow_position_cap_partial_fill", True)),
            )
            execute_small = st.checkbox("execute small trades", value=bool(sizing_defaults.get("execute_small_trades", False)))
        else:
            sizing_mode = None
            copy_ratio = None
            max_position_budget = None
            min_trade_usd = None
            allow_cap_partial = None
            execute_small = None
        max_wallet_exposure = st.number_input("max wallet exposure", min_value=0.0, value=float(backtest_defaults.get("max_wallet_exposure_usd", 20)), step=1.0)
        max_market_exposure = st.number_input("max market exposure", min_value=0.0, value=float(backtest_defaults.get("max_market_exposure_usd", 8)), step=1.0)
        max_category_exposure = st.number_input("max category exposure", min_value=0.0, value=float(backtest_defaults.get("max_category_exposure_usd", 30)), step=1.0)
        max_daily_loss = st.number_input("max daily loss", min_value=0.0, value=float(backtest_defaults.get("max_daily_loss_usd", 10)), step=1.0)
        duplicate_window = st.number_input("duplicate signal window seconds", min_value=0, value=int(backtest_defaults.get("duplicate_signal_window_seconds", 600)), step=60)
        skip_market_makers = st.checkbox("skip likely market makers", value=bool(backtest_defaults.get("skip_likely_market_makers", True)))
        skip_latency_bots = st.checkbox("skip likely latency bots", value=bool(backtest_defaults.get("skip_likely_latency_bots", True)))
        skip_lucky_wallets = st.checkbox("skip lucky wallets", value=bool(backtest_defaults.get("skip_lucky_wallets", True)))
        skip_insufficient_sample = st.checkbox("skip insufficient sample", value=bool(backtest_defaults.get("skip_insufficient_sample", False)))

    st.markdown("Split periods")
    split_cols = st.columns(6)
    train_start = split_cols[0].date_input("train start", value=None)
    train_end = split_cols[1].date_input("train end", value=None)
    validation_start = split_cols[2].date_input("validation start", value=None)
    validation_end = split_cols[3].date_input("validation end", value=None)
    test_start = split_cols[4].date_input("test start", value=None)
    test_end = split_cols[5].date_input("test end", value=None)

    run_clicked = st.form_submit_button("Run Backtest", type="primary")

result = None
if run_clicked:
    start, end = parse_date_range(date_range)
    bt_config = backtest_config_from_values(
        config,
        copy_mode=copy_mode,
        mode=mode,
        selected_wallets=selected_wallets or None,
        date_start=start,
        date_end=end,
        train_start=parse_one_date(train_start),
        train_end=parse_one_date(train_end, end=True),
        validation_start=parse_one_date(validation_start),
        validation_end=parse_one_date(validation_end, end=True),
        test_start=parse_one_date(test_start),
        test_end=parse_one_date(test_end, end=True),
        initial_capital=float(initial_capital),
        position_size_usd=float(position_size),
        copy_delay_seconds=int(copy_delay),
        entry_delay_seconds=int(entry_delay),
        exit_delay_seconds=int(exit_delay),
        max_spread=float(max_spread),
        max_entry_degradation=float(max_degradation),
        allowed_data_quality=allowed_quality,
        max_wallet_exposure_usd=float(max_wallet_exposure),
        max_market_exposure_usd=float(max_market_exposure),
        max_category_exposure_usd=float(max_category_exposure),
        max_daily_loss_usd=float(max_daily_loss),
        duplicate_signal_window_seconds=int(duplicate_window),
        exit_rule=exit_rule,
        sizing_mode=sizing_mode,
        copy_ratio=float(copy_ratio) if copy_ratio is not None else None,
        max_position_budget_usd=float(max_position_budget) if max_position_budget is not None else None,
        min_trade_usd=float(min_trade_usd) if min_trade_usd is not None else None,
        execute_small_trades=execute_small,
        allow_position_cap_partial_fill=allow_cap_partial,
        include_categories=selected_categories,
        exclude_categories=excluded_categories,
        skip_likely_market_makers=skip_market_makers,
        skip_likely_latency_bots=skip_latency_bots,
        skip_lucky_wallets=skip_lucky_wallets,
        skip_insufficient_sample=skip_insufficient_sample,
    )
    with st.spinner("Running backtest..."):
        result = run_backtest(config, bt_config)
    st.success(f"Backtest complete: {result['run_id']}")

if result is None:
    with session_scope(database_url(config)) as session:
        run = latest_run(session)
        if run is not None:
            result = json_loads(run.result_json, {})

if not result:
    st.info("No backtest runs yet. Compute alpha decay first, then run a backtest.")
    st.stop()

metrics = result.get("metrics", {})
run_id = result.get("run_id")

st.subheader("Summary")
summary_cols = st.columns(6)
summary_cols[0].metric("total PnL", f"{float(metrics.get('total_pnl') or 0):.4f}")
summary_cols[1].metric("ROI", f"{float(metrics.get('roi') or 0):.2%}")
summary_cols[2].metric("max drawdown", f"{float(metrics.get('max_drawdown') or 0):.4f}")
summary_cols[3].metric("trades", int(metrics.get("trade_count") or 0))
summary_cols[4].metric("win rate", f"{float(metrics.get('win_rate') or 0):.1%}")
summary_cols[5].metric("profit factor", "" if metrics.get("profit_factor") is None else f"{float(metrics.get('profit_factor')):.2f}")

if result.get("copy_mode") == "reconstructed_wallet_lifecycle" or metrics.get("closed_copied_positions") is not None:
    lifecycle_cols = st.columns(5)
    lifecycle_cols[0].metric("closed copied positions", int(metrics.get("closed_copied_positions") or 0))
    lifecycle_cols[1].metric("open copied positions", int(metrics.get("open_copied_positions") or 0))
    lifecycle_cols[2].metric("skipped positions", int(metrics.get("skipped_positions") or 0))
    lifecycle_cols[3].metric("cap hits", int(metrics.get("cap_hit_count") or 0))
    lifecycle_cols[4].metric("below min trade", int(metrics.get("below_min_trade_count") or 0))

equity_df = pd.DataFrame(metrics.get("equity_curve", []))
if not equity_df.empty:
    st.plotly_chart(px.line(equity_df, x="timestamp", y="equity", title="Equity Curve"), use_container_width=True)

if run_id:
    with session_scope(database_url(config)) as session:
        run_row = session.get(BacktestRun, run_id)
        run_wallets = result.get("selected_wallets") or json_loads(run_row.selected_wallets_json if run_row else None, []) or []
        whale_df = whale_strategy_dataframe(session, run_wallets, None, None)
else:
    whale_df = pd.DataFrame()

st.subheader("Whale Strategy Curve")
if whale_df.empty:
    st.info("No reconstructed whale realized PnL for this run scope.")
else:
    whale_fig = px.line(
        whale_df,
        x="timestamp",
        y="cumulative_realized_pnl",
        title="Whale Strategy Realized PnL",
        hover_data=["realized_pnl", "wallet_address", "market_id", "position_status"],
    )
    whale_fig.add_hline(y=0)
    st.plotly_chart(whale_fig, use_container_width=True)

periods = result.get("periods", {})
if periods:
    period_df = pd.DataFrame(
        [
            {
                "period": label,
                "total_pnl": values.get("total_pnl"),
                "roi": values.get("roi"),
                "max_drawdown": values.get("max_drawdown"),
                "trade_count": values.get("trade_count"),
                "win_rate": values.get("win_rate"),
            }
            for label, values in periods.items()
        ]
    )
    st.subheader("Period Comparison")
    st.dataframe(period_df, use_container_width=True, hide_index=True)

if result.get("walk_forward_windows"):
    st.subheader("Walk-Forward Windows")
    st.dataframe(pd.DataFrame(result["walk_forward_windows"]), use_container_width=True, hide_index=True)

left_chart, right_chart = st.columns(2)
pnl_wallet = pd.DataFrame([{"wallet": key, "net_pnl": value} for key, value in metrics.get("pnl_by_wallet", {}).items()])
if not pnl_wallet.empty:
    left_chart.plotly_chart(px.bar(pnl_wallet, x="wallet", y="net_pnl", title="PnL by Wallet"), use_container_width=True)
pnl_category = pd.DataFrame([{"category": key, "net_pnl": value} for key, value in metrics.get("pnl_by_category", {}).items()])
if not pnl_category.empty:
    right_chart.plotly_chart(px.bar(pnl_category, x="category", y="net_pnl", title="PnL by Category"), use_container_width=True)

st.subheader("PnL by Market")
pnl_market = pd.DataFrame([{"market_id": key, "net_pnl": value} for key, value in metrics.get("pnl_by_market", {}).items()])
if pnl_market.empty:
    st.info("No accepted trades in this run.")
else:
    st.dataframe(pnl_market, use_container_width=True, hide_index=True)

skip_reasons = pd.DataFrame([{"reason": key, "count": value} for key, value in metrics.get("skipped_signal_reasons", {}).items()])
st.subheader("Skipped Signal Reasons")
if skip_reasons.empty:
    st.info("No skipped signals recorded.")
else:
    st.dataframe(skip_reasons.sort_values("count", ascending=False), use_container_width=True, hide_index=True)

quality = pd.DataFrame(
    [{"data_quality": key, "share": value} for key, value in metrics.get("data_quality_summary", {}).get("percent", {}).items()]
)
st.subheader("Data Quality")
if quality.empty:
    st.info("No accepted-trade data-quality mix yet.")
else:
    st.dataframe(quality, use_container_width=True, hide_index=True)

if run_id:
    with session_scope(database_url(config)) as session:
        trades_df = trades_dataframe(session, run_id)
        skipped_df = skipped_dataframe(session, run_id)
else:
    trades_df = pd.DataFrame()
    skipped_df = pd.DataFrame()

st.subheader("Backtest Trades")
if trades_df.empty:
    st.info("No accepted backtest trades.")
else:
    st.dataframe(trades_df, use_container_width=True, hide_index=True)
    st.download_button("Export trades CSV", trades_df.to_csv(index=False).encode("utf-8"), file_name=f"{run_id}_trades.csv", mime="text/csv")

st.subheader("Skipped Signals")
if skipped_df.empty:
    st.info("No stored skipped signals.")
else:
    st.dataframe(skipped_df, use_container_width=True, hide_index=True)

render_formula_reference(PAGE_SECTIONS["backtest"])

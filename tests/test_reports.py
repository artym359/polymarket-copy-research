from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pmcopy.db import (
    AlphaDecayResult,
    BacktestRun,
    Base,
    RawResponse,
    SkippedSignal,
    Trade,
    WalletMetrics,
    json_dumps,
)
from pmcopy.reports import (
    backtest_runs_dataframe,
    data_quality_summary,
    export_report,
    export_table,
    pnl_concentration,
    verdict_for_run,
)


def config(tmp_path: Path) -> dict:
    return {
        "app": {"database_url": f"sqlite:///{(tmp_path / 'reports.sqlite3').as_posix()}"},
        "data_quality": {"warn_if_proxy_share_above": 0.50},
        "reports": {"min_trades_for_verdict": 20, "high_drawdown_fraction": 0.20},
    }


def run_row(run_id: str, *, roi: float, trade_count: int, proxy_share: float = 0.0, exit_rule: str = "fixed_24h") -> BacktestRun:
    total_pnl = roi * 100
    metrics = {
        "total_pnl": total_pnl,
        "roi": roi,
        "max_drawdown": 1.0,
        "trade_count": trade_count,
        "win_rate": 0.5,
        "profit_factor": 1.2,
        "pnl_by_wallet": {"0xa": total_pnl / 2, "0xb": total_pnl / 2},
        "pnl_by_market": {"m1": total_pnl / 2, "m2": total_pnl / 2},
        "pnl_by_category": {"sports": total_pnl / 2, "crypto": total_pnl / 2},
        "skipped_signal_reasons": {},
        "data_quality_summary": {
            "percent": {
                "exact_orderbook": 1.0 - proxy_share,
                "price_history_proxy": proxy_share,
                "midpoint_proxy": 0.0,
                "last_price_proxy": 0.0,
                "insufficient_data": 0.0,
            }
        },
        "equity_curve": [{"timestamp": "2026-01-01T00:00:00+00:00", "equity": 100 + total_pnl}],
    }
    return BacktestRun(
        run_id=run_id,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        mode="in_sample",
        config_json=json_dumps({"exit_rule": exit_rule, "initial_capital": 100, "copy_delay_seconds": 60}),
        selected_wallets_json=json_dumps(["0xa", "0xb"]),
        date_split_json=json_dumps({}),
        total_pnl=total_pnl,
        roi=roi,
        max_drawdown=1.0,
        trade_count=trade_count,
        win_rate=0.5,
        profit_factor=1.2,
        data_quality_summary_json=json_dumps(metrics["data_quality_summary"]),
        result_json=json_dumps({"metrics": metrics, "periods": {}}),
    )


def test_verdict_logic_rejects_negative_and_warns_latest_available(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    run = run_row("r1", roi=-0.01, trade_count=30, proxy_share=0.0, exit_rule="latest_available")

    verdict = verdict_for_run(run, cfg)

    assert verdict["verdict"] == "reject"
    assert "negative ROI" in verdict["reasons"]
    assert any("latest_available" in warning for warning in verdict["warnings"])


def test_verdict_detects_poor_data_quality_and_concentration(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    poor = run_row("poor", roi=0.05, trade_count=30, proxy_share=1.0)
    poor_verdict = verdict_for_run(poor, cfg)
    assert poor_verdict["verdict"] == "poor data quality"
    assert "strategy relies heavily on proxy data" in poor_verdict["reasons"]

    concentrated = run_row("conc", roi=0.05, trade_count=30, proxy_share=0.0)
    payload = json_dumps(
        {
            "metrics": {
                **(pd.Series({}).to_dict()),
                "total_pnl": 5,
                "roi": 0.05,
                "max_drawdown": 1,
                "trade_count": 30,
                "pnl_by_wallet": {"0xa": 5},
                "pnl_by_market": {"m1": 3, "m2": 2},
                "pnl_by_category": {"sports": 3, "crypto": 2},
                "data_quality_summary": {"percent": {"exact_orderbook": 1.0}},
                "skipped_signal_reasons": {},
            },
            "periods": {},
        }
    )
    concentrated.result_json = payload
    verdict = verdict_for_run(concentrated, cfg)
    assert verdict["verdict"] == "overfit / too concentrated"
    assert "result depends on one wallet" in verdict["reasons"]


def test_data_quality_summary_counts_missing_and_skips() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(RawResponse(source="clob", endpoint="/prices-history", url="u", success=False, error="boom"))
        session.add(Trade(id="t1", wallet_address="0xa", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), token_id=None))
        session.add(
            AlphaDecayResult(
                id="a1",
                wallet_address="0xa",
                trade_id="t1",
                delay_seconds=60,
                exit_rule="fixed_24h",
                data_quality="insufficient_data",
                data_quality_rank=0,
                skip_reason="no_price_history",
            )
        )
        session.add(SkippedSignal(id="s1", wallet_address="0xa", reason="missing_exit_price"))
        session.commit()

        summary = data_quality_summary(session)

    assert summary["raw_total"] == 1
    assert summary["raw_failed"] == 1
    assert summary["alpha_total"] == 1
    assert summary["insufficient_data_share"] == 1.0
    assert summary["missing_token_ids"] == 1
    assert summary["missing_price_history"] == 1
    assert summary["most_common_skip_reason"] == "missing_exit_price"


def test_verdict_adds_exposure_metric_warnings() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    cfg = {"data_quality": {"warn_if_proxy_share_above": 0.50}, "reports": {"min_trades_for_verdict": 20}}
    with Session(engine) as session:
        run = run_row("r-proxy", roi=0.02, trade_count=25)
        session.add(run)
        session.add(WalletMetrics(wallet_address="0xa", max_exposure_confidence="data_api_proxy", average_exposure_confidence="unavailable"))
        session.add(WalletMetrics(wallet_address="0xb", max_exposure_confidence="data_api_proxy", average_exposure_confidence="snapshots_proxy"))
        session.commit()

        verdict = verdict_for_run(run, cfg)

    assert "exposure_metrics_proxy_only" in verdict["warnings"]


def test_exports_and_run_comparison_helpers(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    engine = create_engine(cfg["app"]["database_url"], future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(run_row("r-export", roi=0.02, trade_count=25))
        session.add(
            WalletMetrics(
                wallet_address="0xa",
                roi_on_volume=0.12,
                max_capital_at_risk=20,
                return_on_max_capital_at_risk=0.2,
                max_exposure_confidence="data_api_proxy",
            )
        )
        session.add(
            AlphaDecayResult(
                id="a1",
                wallet_address="0xa",
                trade_id="t1",
                delay_seconds=60,
                exit_rule="latest_available",
                data_quality="price_history_proxy",
                data_quality_rank=3,
            )
        )
        session.commit()

    table_result = export_table(cfg, "alpha_decay_results", tmp_path / "alpha.csv")
    assert table_result["rows"] == 1
    assert Path(table_result["path"]).exists()
    metrics_result = export_table(cfg, "wallet_metrics", tmp_path / "wallet_metrics.csv")
    metrics_df = pd.read_csv(metrics_result["path"])
    assert metrics_df.iloc[0]["edge_on_volume"] == 0.12
    assert metrics_df.iloc[0]["pnl_per_traded_dollar"] == 0.12
    assert "return_on_max_capital_at_risk" in metrics_df.columns

    report = export_report(cfg, "r-export")
    assert Path(report["directory"]).exists()
    assert Path(report["paths"]["verdict"]).exists()

    with Session(engine) as session:
        df = backtest_runs_dataframe(session, cfg)
    assert df.iloc[0]["run_id"] == "r-export"
    assert "verdict" in df.columns


def test_pnl_concentration() -> None:
    assert pnl_concentration({"a": 8, "b": 2}) == 0.8
    assert pnl_concentration({}) == 0.0

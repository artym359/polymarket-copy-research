from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.table import Table

from pmcopy.backtest.sensitivity import sensitivity_config_from_values, run_sensitivity
from pmcopy.backtest.simulator import backtest_config_from_values, run_backtest
from pmcopy.config import database_url, load_config, project_root
from pmcopy.db import init_db, promote_top_candidates, session_scope
from pmcopy.features.alpha_decay import alpha_config_from_values, compute_alpha_decay, inspect_price_history
from pmcopy.features.classification import classify_all_wallets
from pmcopy.features.copyability import compute_copyability
from pmcopy.features.data_quality import quality_breakdown
from pmcopy.features.position_reconstruction import (
    inspect_position,
    reconstruction_config_from_values,
    reconstruct_promoted_positions,
    reconstruct_wallet_positions,
)
from pmcopy.features.wallet_metrics import compute_all_wallet_metrics
from pmcopy.ingest.discover_wallets import discover_wallets, list_candidates
from pmcopy.ingest.ingest_wallet_activity import ingest_promoted_wallets, ingest_wallet
from pmcopy.logging import setup_logging
from pmcopy.reports import ALLOWED_EXPORT_TABLES, export_report, export_table

console = Console()

EXIT_RULE_CHOICES = ["hold_to_resolution", "fixed_24h", "latest_available", "reconstructed_wallet_lifecycle"]
COPY_MODE_CHOICES = ["diagnostic_trade_level", "reconstructed_wallet_lifecycle"]
SIZING_MODE_CHOICES = ["fixed_usd", "proportional_to_whale", "proportional_to_position", "proportional_to_whale_with_cap"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    setup_logging(str(config.get("app", {}).get("log_level", "INFO")))

    if args.command == "init-db":
        init_db(config)
        console.print(f"Initialized database: {database_url(config)}")
        return 0

    if args.command == "discover-wallets":
        overrides = discovery_overrides_from_args(args)
        result = discover_wallets(config, overrides)
        console.print(
            f"Discovery complete: {result.candidates_found} candidate wallets, "
            f"{result.markets_scanned} markets scanned, {result.tokens_upserted} tokens upserted."
        )
        if result.warnings:
            console.print("[yellow]Warnings[/yellow]")
            for warning in result.warnings:
                detail = f" ({warning.detail})" if warning.detail else ""
                console.print(f"- {warning.source}: {warning.message}{detail}")
        return 0

    if args.command == "list-candidates":
        init_db(config)
        with session_scope(database_url(config)) as session:
            promoted_filter = parse_promoted_filter(args.promoted)
            df = list_candidates(session, limit=args.limit, promoted=promoted_filter)
        print_dataframe(df, title="Candidate Wallets")
        return 0

    if args.command == "promote-candidates":
        init_db(config)
        with session_scope(database_url(config)) as session:
            promoted = promote_top_candidates(session, args.top)
        console.print(f"Promoted {promoted} candidates into wallets table.")
        return 0

    if args.command == "ingest-wallet":
        result = ingest_wallet(config, args.wallet)
        print_ingestion_result(result)
        return 0

    if args.command == "ingest-promoted-wallets":
        results = ingest_promoted_wallets(config, limit=args.limit)
        total_trades = sum(result.trades for result in results)
        total_activity = sum(result.activity for result in results)
        total_positions = sum(result.positions + result.closed_positions for result in results)
        console.print(
            f"Ingested {len(results)} wallets: {total_trades} trades, "
            f"{total_activity} activity rows, {total_positions} position rows."
        )
        warn_count = sum(len(result.warnings) for result in results)
        if warn_count:
            console.print(f"[yellow]{warn_count} ingestion warnings recorded in command output.[/yellow]")
            for result in results[:10]:
                for warning in result.warnings:
                    console.print(f"- {result.wallet_address}: {warning}")
        return 0

    if args.command == "compute-wallet-metrics":
        computed = compute_all_wallet_metrics(config, limit=args.limit)
        console.print(f"Computed wallet metrics for {computed} wallets.")
        return 0

    if args.command == "classify-wallets":
        classified = classify_all_wallets(config, limit=args.limit)
        console.print(f"Classified {classified} wallets.")
        return 0

    if args.command == "reconstruct-positions":
        recon_config = reconstruction_config_from_values(
            wallet_address=args.wallet,
            analysis_start=args.analysis_start,
            analysis_end=args.analysis_end,
            warmup_days=args.warmup_days,
        )
        result = reconstruct_wallet_positions(config, args.wallet, recon_config)
        console.print(
            f"Reconstructed {result.get('positions', 0)} positions for {args.wallet}: "
            f"closed={result.get('closed_positions', 0)}, open={result.get('open_positions', 0)}, "
            f"partial={result.get('partial_positions', 0)}, "
            f"missing_prior_inventory={result.get('missing_prior_inventory', 0)}, "
            f"orphan_sell={result.get('orphan_sell', 0)}."
        )
        return 0

    if args.command == "reconstruct-promoted-positions":
        recon_config = reconstruction_config_from_values(
            analysis_start=args.analysis_start,
            analysis_end=args.analysis_end,
            warmup_days=args.warmup_days,
        )
        result = reconstruct_promoted_positions(config, recon_config=recon_config, limit=args.limit)
        console.print(
            f"Reconstructed {result.get('positions', 0)} positions across {result.get('wallets', 0)} wallets: "
            f"closed={result.get('closed_positions', 0)}, open={result.get('open_positions', 0)}, "
            f"partial={result.get('partial_positions', 0)}, "
            f"missing_prior_inventory={result.get('missing_prior_inventory', 0)}, "
            f"orphan_sell={result.get('orphan_sell', 0)}."
        )
        return 0

    if args.command == "inspect-position":
        console.print_json(data=inspect_position(config, args.wallet, args.token_id))
        return 0

    if args.command == "compute-alpha-decay":
        entry_delays = [args.entry_delay_seconds] if args.entry_delay_seconds is not None else (parse_int_csv(args.delays) if args.delays else None)
        alpha_config = alpha_config_from_values(
            config,
            delays=entry_delays,
            position_size_usd=args.position_size_usd,
            max_spread=args.max_spread,
            max_entry_degradation=args.max_entry_degradation,
            allowed_data_quality=parse_str_csv(args.allowed_data_quality) if args.allowed_data_quality else None,
            exit_rule=args.exit_rule,
            limit=args.limit,
            historical_mode=args.historical_mode,
            exit_delay_seconds=args.exit_delay_seconds,
            min_exit_fraction=args.min_exit_fraction,
            max_holding_hours=args.max_holding_hours,
            allow_partial_exits=args.allow_partial_exits,
            sizing_mode=args.sizing_mode,
            copy_ratio=args.copy_ratio,
            warmup_days=args.warmup_days,
            debug_alpha=args.debug_alpha,
            debug_alpha_limit=args.debug_alpha_limit,
        )
        result = compute_alpha_decay(config, wallet_address=args.wallet, alpha_config=alpha_config)
        copyability_count = compute_copyability(config, wallet_address=args.wallet, allowed_data_quality=alpha_config.allowed_data_quality)
        classified = classify_all_wallets(config)
        console.print(
            f"Computed alpha decay: {result['rows']} rows across {result['wallets']} wallets "
            f"({result['skipped_trades']} unusable source trades skipped)."
        )
        breakdown = quality_breakdown(result.get("data_quality_levels", []))
        console.print("Data-quality breakdown: " + ", ".join(f"{level}={share:.1%}" for level, share in breakdown.items()))
        skip_reasons = result.get("skip_reasons", {})
        if skip_reasons:
            console.print("Skip reasons: " + ", ".join(f"{reason}={count}" for reason, count in sorted(skip_reasons.items())))
        follow_stats = result.get("follow_wallet_exit", {})
        if args.exit_rule == "follow_wallet_exit" and follow_stats:
            console.print(
                "Follow-wallet exits: "
                f"matched={follow_stats.get('matched_wallet_exits', 0)}, "
                f"no_exit={follow_stats.get('no_wallet_exit_found', 0)}, "
                f"partial={follow_stats.get('partial_exits', 0)}, "
                f"full={follow_stats.get('full_exits', 0)}"
            )
        lifecycle_stats = result.get("reconstructed_wallet_lifecycle", {})
        if args.exit_rule == "reconstructed_wallet_lifecycle" and lifecycle_stats:
            console.print(
                "Reconstructed lifecycle: "
                f"positions={lifecycle_stats.get('reconstructed_positions', 0)}, "
                f"closed={lifecycle_stats.get('closed_positions', 0)}, "
                f"missing_prior_inventory={lifecycle_stats.get('missing_prior_inventory', 0)}, "
                f"orphan_sell={lifecycle_stats.get('orphan_sell', 0)}, "
                f"usable_copy_trades={lifecycle_stats.get('usable_lifecycle_copy_trades', 0)}, "
                f"pnl={float(lifecycle_stats.get('lifecycle_copy_pnl', 0.0)):.4f}."
            )
        if args.debug_alpha:
            console.print_json(data=result.get("debug", []))
        console.print(f"Updated copyability for {copyability_count} wallets and refreshed {classified} classifications.")
        return 0

    if args.command == "inspect-price-history":
        console.print_json(data=inspect_price_history(config, args.token_id))
        return 0

    if args.command == "compute-copyability":
        count = compute_copyability(
            config,
            wallet_address=args.wallet,
            allowed_data_quality=parse_str_csv(args.allowed_data_quality) if args.allowed_data_quality else None,
        )
        console.print(f"Computed copyability for {count} wallets.")
        return 0

    if args.command == "run-backtest":
        wallets = list(args.wallet or [])
        if args.wallets_file:
            wallets.extend(read_wallets_file(args.wallets_file))
        bt_config = backtest_config_from_values(
            config,
            copy_mode=args.copy_mode,
            mode=args.mode,
            selected_wallets=wallets or None,
            date_start=args.start_date,
            date_end=args.end_date,
            train_start=args.train_start,
            train_end=args.train_end,
            validation_start=args.validation_start,
            validation_end=args.validation_end,
            test_start=args.test_start,
            test_end=args.test_end,
            copy_delay_seconds=args.copy_delay_seconds,
            position_size_usd=args.position_size_usd,
            initial_capital=args.initial_capital,
            max_spread=args.max_spread,
            max_entry_degradation=args.max_entry_degradation,
            allowed_data_quality=parse_str_csv(args.allowed_data_quality) if args.allowed_data_quality else None,
            exit_rule=args.exit_rule,
            sizing_mode=args.sizing_mode,
            copy_ratio=args.copy_ratio,
            max_position_budget_usd=args.max_position_budget_usd,
            min_trade_usd=args.min_trade_usd,
            execute_small_trades=args.execute_small_trades,
            allow_position_cap_partial_fill=args.allow_position_cap_partial_fill,
            entry_delay_seconds=args.entry_delay_seconds,
            exit_delay_seconds=args.exit_delay_seconds,
            include_categories=parse_str_csv(args.include_categories) if args.include_categories else None,
            exclude_categories=parse_str_csv(args.exclude_categories) if args.exclude_categories else None,
            max_market_exposure_usd=args.max_market_exposure,
            max_wallet_exposure_usd=args.max_wallet_exposure,
            max_category_exposure_usd=args.max_category_exposure,
            max_daily_loss_usd=args.max_daily_loss,
            duplicate_signal_window_seconds=args.duplicate_signal_window,
            skip_likely_market_makers=args.skip_market_makers,
            skip_likely_latency_bots=args.skip_latency_bots,
            skip_lucky_wallets=args.skip_lucky_wallets,
            skip_insufficient_sample=args.skip_insufficient_sample,
            min_copyability_score=args.min_copyability_score,
        )
        result = run_backtest(config, bt_config)
        metrics = result["metrics"]
        console.print(
            f"Backtest {result['run_id']} ({result['mode']}): "
            f"{metrics['trade_count']} accepted trades, "
            f"total_pnl={metrics['total_pnl']:.4f}, roi={metrics['roi']:.2%}, "
            f"max_drawdown={metrics['max_drawdown']:.4f}."
        )
        console.print(f"Candidate alpha rows considered: {result.get('candidate_count', 0)}")
        if args.copy_mode == "reconstructed_wallet_lifecycle":
            console.print(
                "Lifecycle copy: "
                f"closed={metrics.get('closed_copied_positions', 0)}, "
                f"open={metrics.get('open_copied_positions', 0)}, "
                f"skipped={metrics.get('skipped_positions', 0)}, "
                f"cap_hits={metrics.get('cap_hit_count', 0)}, "
                f"below_min={metrics.get('below_min_trade_count', 0)}"
            )
        skipped = metrics.get("skipped_signal_reasons", {})
        if skipped:
            console.print("Skipped signal reasons: " + ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items())))
        data_quality = metrics.get("data_quality_summary", {}).get("percent", {})
        if data_quality:
            console.print("Data-quality breakdown: " + ", ".join(f"{level}={share:.1%}" for level, share in data_quality.items()))
        for warning in result.get("warnings", []):
            console.print(f"[yellow]Warning: {warning}[/yellow]")
        return 0

    if args.command == "run-sensitivity":
        try:
            sensitivity_config = sensitivity_config_from_values(
                config,
                mode=args.mode,
                selected_wallets=list(args.wallet or []) or None,
                copy_delays=parse_int_csv(args.copy_delays) if args.copy_delays else None,
                max_entry_degradations=parse_float_csv(args.max_entry_degradations) if args.max_entry_degradations else None,
                max_spreads=parse_float_csv(args.max_spreads) if args.max_spreads else None,
                position_sizes=parse_float_csv(args.position_sizes) if args.position_sizes else None,
                max_market_exposures=parse_optional_float_csv(args.max_market_exposures) if args.max_market_exposures else None,
                allowed_data_quality=parse_str_csv(args.allowed_data_quality) if args.allowed_data_quality else None,
                exit_rule=args.exit_rule,
                limit_combinations=args.limit_combinations,
                confirm_large_grid=args.confirm_large_grid,
            )
            result = run_sensitivity(config, sensitivity_config)
        except ValueError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return 2
        robustness = result.get("robustness", {})
        console.print(
            f"Sensitivity {result['sensitivity_run_id']}: "
            f"tested {result['tested_combinations']} of {result['estimated_combinations']} combinations."
        )
        console.print(
            f"Robustness: {robustness.get('label', 'unknown')} "
            f"({robustness.get('positive_combinations', 0)} positive, "
            f"{robustness.get('negative_combinations', 0)} negative)."
        )
        warning_counts = robustness.get("warning_counts", {})
        if warning_counts:
            console.print("Warning flags: " + ", ".join(f"{flag}={count}" for flag, count in sorted(warning_counts.items())))
        return 0

    if args.command == "export-table":
        try:
            result = export_table(config, args.table, args.output)
        except ValueError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return 2
        console.print(f"Exported {result['rows']} rows from {result['table']} to {result['path']}")
        return 0

    if args.command == "export-report":
        try:
            result = export_report(config, args.run_id)
        except ValueError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return 2
        console.print(f"Exported report for {result['run_id']} to {result['directory']}")
        console.print(f"Verdict: {result['verdict']['verdict']}")
        return 0

    if args.command == "dashboard":
        return run_dashboard(args.config)

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket copy-research MVP CLI.")
    parser.add_argument("--config", default=None, help="Path to YAML config. Defaults to config/default.yaml.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create SQLite tables.")

    discover = subparsers.add_parser("discover-wallets", help="Run Phase 2 candidate wallet discovery.")
    discover.add_argument("--categories", default=None, help="Comma-separated categories to enable for this run.")
    discover.add_argument("--max-wallets-total", type=int, default=None)
    discover.add_argument("--max-markets-per-category", type=int, default=None)
    discover.add_argument("--min-volume", type=float, default=None)
    discover.add_argument("--min-liquidity", type=float, default=None)
    discover.add_argument("--include-active", action=argparse.BooleanOptionalAction, default=None)
    discover.add_argument("--include-closed", action=argparse.BooleanOptionalAction, default=None)
    discover.add_argument("--manual-seeds", action=argparse.BooleanOptionalAction, default=None)
    discover.add_argument("--leaderboards", action=argparse.BooleanOptionalAction, default=None)
    discover.add_argument("--holders", action=argparse.BooleanOptionalAction, default=None)
    discover.add_argument("--activity", action=argparse.BooleanOptionalAction, default=None)

    list_cmd = subparsers.add_parser("list-candidates", help="List candidate wallets.")
    list_cmd.add_argument("--limit", type=int, default=100)
    list_cmd.add_argument("--promoted", choices=["all", "true", "false"], default="all")

    promote = subparsers.add_parser("promote-candidates", help="Promote top candidates into wallets table.")
    promote.add_argument("--top", type=int, required=True)

    ingest_one = subparsers.add_parser("ingest-wallet", help="Ingest public Data API rows for one wallet.")
    ingest_one.add_argument("--wallet", required=True, help="Wallet address to ingest.")

    ingest_promoted = subparsers.add_parser("ingest-promoted-wallets", help="Ingest public Data API rows for promoted wallets.")
    ingest_promoted.add_argument("--limit", type=int, default=None, help="Optional max number of promoted wallets to ingest.")

    metrics = subparsers.add_parser("compute-wallet-metrics", help="Compute basic metrics for ingested wallets.")
    metrics.add_argument("--limit", type=int, default=None, help="Optional max number of wallets to compute.")

    classify = subparsers.add_parser("classify-wallets", help="Classify wallets from computed metrics.")
    classify.add_argument("--limit", type=int, default=None, help="Optional max number of wallets to classify.")

    reconstruct_one = subparsers.add_parser("reconstruct-positions", help="Reconstruct event-sourced positions for one wallet.")
    reconstruct_one.add_argument("--wallet", required=True, help="Wallet address to reconstruct.")
    reconstruct_one.add_argument("--analysis-start", default=None, help="YYYY-MM-DD or ISO datetime. Warmup starts before this date.")
    reconstruct_one.add_argument("--analysis-end", default=None, help="YYYY-MM-DD or ISO datetime.")
    reconstruct_one.add_argument("--warmup-days", type=int, default=90)

    reconstruct_promoted = subparsers.add_parser("reconstruct-promoted-positions", help="Reconstruct positions for promoted / ingested wallets.")
    reconstruct_promoted.add_argument("--limit", type=int, default=None, help="Optional max wallets to reconstruct.")
    reconstruct_promoted.add_argument("--analysis-start", default=None, help="YYYY-MM-DD or ISO datetime. Warmup starts before this date.")
    reconstruct_promoted.add_argument("--analysis-end", default=None, help="YYYY-MM-DD or ISO datetime.")
    reconstruct_promoted.add_argument("--warmup-days", type=int, default=90)

    inspect_position_cmd = subparsers.add_parser("inspect-position", help="Inspect reconstructed lifecycle for one wallet and token.")
    inspect_position_cmd.add_argument("--wallet", required=True)
    inspect_position_cmd.add_argument("--token-id", required=True)

    alpha = subparsers.add_parser("compute-alpha-decay", help="Compute Phase 4 alpha-decay diagnostics.")
    alpha.add_argument("--wallet", default=None, help="Optional wallet address. Defaults to all wallets with ingested trades.")
    alpha.add_argument("--limit", type=int, default=None, help="Optional max source trades per wallet.")
    alpha.add_argument("--delays", default=None, help="Comma-separated delays in seconds.")
    alpha.add_argument("--entry-delay-seconds", type=int, default=None, help="Single entry delay. Overrides --delays when provided.")
    alpha.add_argument("--position-size-usd", type=float, default=None)
    alpha.add_argument("--max-spread", type=float, default=None)
    alpha.add_argument("--max-entry-degradation", type=float, default=None)
    alpha.add_argument("--allowed-data-quality", default=None, help="Comma-separated allowed quality levels for copyability.")
    alpha.add_argument("--exit-rule", default=None, choices=EXIT_RULE_CHOICES)
    alpha.add_argument("--exit-delay-seconds", type=int, default=None, help="Delay after the source wallet exit before simulated copy exit. Defaults to entry delay.")
    alpha.add_argument("--min-exit-fraction", type=float, default=None, help="Minimum source entry size fraction that must be exited.")
    alpha.add_argument("--max-holding-hours", type=float, default=None, help="Optional max time to search for source wallet exits.")
    alpha.add_argument("--allow-partial-exits", action=argparse.BooleanOptionalAction, default=None)
    alpha.add_argument("--sizing-mode", default=None, choices=SIZING_MODE_CHOICES)
    alpha.add_argument("--copy-ratio", type=float, default=None)
    alpha.add_argument("--warmup-days", type=int, default=None)
    alpha.add_argument("--historical-mode", default=None, choices=["price_history_only", "full"])
    alpha.add_argument("--debug-alpha", action="store_true", help="Print first source-trade alpha diagnostics.")
    alpha.add_argument("--debug-alpha-limit", type=int, default=10, help="Number of source trades to include in debug output.")

    copyability = subparsers.add_parser("compute-copyability", help="Recompute wallet copyability from stored alpha-decay rows.")
    copyability.add_argument("--wallet", default=None)
    copyability.add_argument("--allowed-data-quality", default=None)

    backtest = subparsers.add_parser("run-backtest", help="Run Phase 5 copy-trading backtest from stored alpha-decay rows.")
    backtest.add_argument("--copy-mode", default=None, choices=COPY_MODE_CHOICES)
    backtest.add_argument("--wallet", action="append", default=None, help="Wallet address. Can be repeated.")
    backtest.add_argument("--wallets-file", default=None, help="Optional file with one wallet address per line.")
    backtest.add_argument("--mode", choices=["in_sample", "split", "walk_forward"], default=None)
    backtest.add_argument("--start-date", default=None, help="YYYY-MM-DD or ISO datetime.")
    backtest.add_argument("--end-date", default=None, help="YYYY-MM-DD or ISO datetime.")
    backtest.add_argument("--train-start", default=None)
    backtest.add_argument("--train-end", default=None)
    backtest.add_argument("--validation-start", default=None)
    backtest.add_argument("--validation-end", default=None)
    backtest.add_argument("--test-start", default=None)
    backtest.add_argument("--test-end", default=None)
    backtest.add_argument("--copy-delay-seconds", type=int, default=None)
    backtest.add_argument("--entry-delay-seconds", type=int, default=None)
    backtest.add_argument("--exit-delay-seconds", type=int, default=None)
    backtest.add_argument("--position-size-usd", type=float, default=None)
    backtest.add_argument("--initial-capital", type=float, default=None)
    backtest.add_argument("--max-spread", type=float, default=None)
    backtest.add_argument("--max-entry-degradation", type=float, default=None)
    backtest.add_argument("--allowed-data-quality", default=None)
    backtest.add_argument("--exit-rule", default=None, choices=EXIT_RULE_CHOICES)
    backtest.add_argument("--sizing-mode", default=None, choices=SIZING_MODE_CHOICES)
    backtest.add_argument("--copy-ratio", type=float, default=None)
    backtest.add_argument("--max-position-budget-usd", type=float, default=None)
    backtest.add_argument("--min-trade-usd", type=float, default=None)
    backtest.add_argument("--execute-small-trades", action=argparse.BooleanOptionalAction, default=None)
    backtest.add_argument("--allow-position-cap-partial-fill", action=argparse.BooleanOptionalAction, default=None)
    backtest.add_argument("--include-categories", default=None)
    backtest.add_argument("--exclude-categories", default=None)
    backtest.add_argument("--max-market-exposure", type=float, default=None)
    backtest.add_argument("--max-wallet-exposure", type=float, default=None)
    backtest.add_argument("--max-category-exposure", type=float, default=None)
    backtest.add_argument("--max-daily-loss", type=float, default=None)
    backtest.add_argument("--duplicate-signal-window", type=int, default=None)
    backtest.add_argument("--min-copyability-score", type=float, default=None)
    add_skip_pair(backtest, "market-makers", "skip_market_makers")
    add_skip_pair(backtest, "latency-bots", "skip_latency_bots")
    add_skip_pair(backtest, "lucky-wallets", "skip_lucky_wallets")
    add_skip_pair(backtest, "insufficient-sample", "skip_insufficient_sample")

    sensitivity = subparsers.add_parser("run-sensitivity", help="Run Phase 6 sensitivity grid over backtest parameters.")
    sensitivity.add_argument("--mode", choices=["in_sample", "split", "walk_forward"], default=None)
    sensitivity.add_argument("--wallet", action="append", default=None, help="Wallet address. Can be repeated.")
    sensitivity.add_argument("--copy-delays", default=None, help="Comma-separated copy delays.")
    sensitivity.add_argument("--max-entry-degradations", default=None, help="Comma-separated degradation limits.")
    sensitivity.add_argument("--max-spreads", default=None, help="Comma-separated spread limits.")
    sensitivity.add_argument("--position-sizes", default=None, help="Comma-separated position sizes.")
    sensitivity.add_argument("--max-market-exposures", default=None, help="Comma-separated max market exposures.")
    sensitivity.add_argument("--allowed-data-quality", default=None)
    sensitivity.add_argument("--exit-rule", default=None, choices=EXIT_RULE_CHOICES)
    sensitivity.add_argument("--limit-combinations", type=int, default=None)
    sensitivity.add_argument("--confirm-large-grid", action="store_true")

    export_table_cmd = subparsers.add_parser("export-table", help="Export an allowed SQLite table to CSV.")
    export_table_cmd.add_argument("--table", required=True, choices=sorted(ALLOWED_EXPORT_TABLES))
    export_table_cmd.add_argument("--output", default=None, help="Output CSV path. Defaults to data/exports/<table>.csv.")

    export_report_cmd = subparsers.add_parser("export-report", help="Export a backtest run report bundle to data/exports.")
    export_report_cmd.add_argument("--run-id", required=True)

    inspect = subparsers.add_parser("inspect-price-history", help="Inspect normalized CLOB /prices-history data for one token.")
    inspect.add_argument("--token-id", required=True)

    subparsers.add_parser("dashboard", help="Launch local Streamlit dashboard.")
    return parser


def discovery_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    categories = [item.strip() for item in args.categories.split(",")] if args.categories else None
    return {
        "categories": categories,
        "max_wallets_total": args.max_wallets_total,
        "max_markets_per_category": args.max_markets_per_category,
        "min_volume": args.min_volume,
        "min_liquidity": args.min_liquidity,
        "include_active": args.include_active,
        "include_closed": args.include_closed,
        "include_manual_seeds": args.manual_seeds,
        "include_leaderboards": args.leaderboards,
        "include_market_holders": args.holders,
        "include_market_activity": args.activity,
    }


def parse_promoted_filter(value: str) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def parse_int_csv(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_float_csv(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def parse_optional_float_csv(value: str) -> list[float | None]:
    result: list[float | None] = []
    for item in value.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        result.append(None if cleaned.lower() in {"none", "null"} else float(cleaned))
    return result


def parse_str_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def read_wallets_file(path: str) -> list[str]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip() and not line.strip().startswith("#")]


def add_skip_pair(parser: argparse.ArgumentParser, label: str, dest: str) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--skip-{label}", dest=dest, action="store_true", default=None)
    group.add_argument(f"--include-{label}", dest=dest, action="store_false")


def print_dataframe(df: pd.DataFrame, title: str) -> None:
    if df.empty:
        console.print(f"{title}: no rows.")
        return
    table = Table(title=title)
    columns = list(df.columns)
    for column in columns:
        table.add_column(column)
    for _, row in df.iterrows():
        table.add_row(*[shorten(row[column]) for column in columns])
    console.print(table)


def shorten(value: Any, width: int = 72) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def print_ingestion_result(result: Any) -> None:
    console.print(
        f"Ingested {result.wallet_address}: {result.trades} trades, "
        f"{result.activity} activity rows, {result.positions} open positions, "
        f"{result.closed_positions} closed positions, {result.value_snapshots} value rows."
    )
    for warning in result.warnings:
        console.print(f"[yellow]- {warning}[/yellow]")


def run_dashboard(config_path: str | None) -> int:
    app_path = project_root() / "src" / "pmcopy" / "dashboard" / "app.py"
    env = os.environ.copy()
    if config_path:
        env["PMCOPY_CONFIG"] = str(Path(config_path).resolve())
    command = [sys.executable, "-m", "streamlit", "run", str(app_path)]
    return subprocess.run(command, cwd=project_root(), env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

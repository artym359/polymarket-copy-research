# Polymarket Copy Research

Public portfolio snapshot of a local research dashboard for discovering and analyzing Polymarket wallet copy-trading candidates from public read-only data.

> This repository contains research and simulation code only. It does not place orders, connect to authenticated endpoints or provide investment advice. Raw API responses, databases and wallet seed lists are deliberately excluded from the public snapshot.

This repository currently implements Phase 1 through Phase 7: repo scaffold, SQLite persistence, public API clients, candidate discovery, promoted wallet ingestion, wallet metrics, classification, alpha-decay diagnostics, position reconstruction, causal lifecycle-copy simulation, copyability scoring, local backtesting, sensitivity analysis, reports, data-quality inspection, and CSV exports.

## What This Project Does

- Discovers raw candidate wallets from selected market categories.
- Supports optional local seed wallets from an ignored `config/wallets_seed.txt` file.
- Discovers markets from Gamma API and holders/trades from Data API where available.
- Ingests promoted wallet trades, activity, positions, closed positions, and value snapshots.
- Computes basic wallet metrics and initial rule-based wallet classifications.
- Computes alpha-decay diagnostics after copy delays.
- Reconstructs wallet token positions from public trade history.
- Simulates lifecycle copy trading from reconstructed position events with causal sizing.
- Computes wallet copyability score and recent vs historical copyability.
- Runs local backtests from either diagnostic alpha-decay rows or reconstructed lifecycle-copy rows.
- Runs sensitivity grids over key backtest parameters to diagnose robustness.
- Shows rule-based report verdicts and data-quality diagnostics.
- Exports core research tables and run reports to CSV.
- Stores raw API responses for reproducibility and schema inspection.

## What This Project Does Not Do

- No live trading.
- No paper trading.
- No authenticated Polymarket endpoints.
- No order placement.
- No geographic restriction, KYC, Terms of Service, or platform restriction bypassing.
- No baseline comparison yet.
- Not financial advice.

## Public-repository boundary

The public version contains source code, tests and a safe default configuration. It intentionally does not contain:

- SQLite databases, raw API responses, CSV exports or logs;
- manually selected wallet lists or private research notes;
- `.env` files, credentials, browser sessions or authenticated trading code;
- generated Streamlit state and local virtual environments.

If you use manual seeds locally, create `config/wallets_seed.txt` yourself. Never commit wallet lists, credentials or private identifiers without a clear reason and permission.

## Setup

```powershell
cd path\to\polymarket-copy-research
python -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8="1"
$env:PIP_PROGRESS_BAR="off"
pip install -e ".[dev]" --progress-bar off
```

Optional environment overrides can be copied from `.env.example` into `.env`.

## CLI Commands

```powershell
python -m pmcopy.cli init-db
python -m pmcopy.cli discover-wallets
python -m pmcopy.cli list-candidates --limit 100
python -m pmcopy.cli promote-candidates --top 500
python -m pmcopy.cli ingest-promoted-wallets --limit 100
python -m pmcopy.cli compute-wallet-metrics
python -m pmcopy.cli classify-wallets
```

Ingest one wallet:

```powershell
python -m pmcopy.cli ingest-wallet --wallet 0x...
```

Compute alpha decay:

```powershell
python -m pmcopy.cli compute-alpha-decay --limit 100 --delays 10,30,60,300,900 --position-size-usd 2 --max-spread 0.03 --max-entry-degradation 0.03 --allowed-data-quality exact_orderbook,price_history_proxy --exit-rule fixed_24h --historical-mode price_history_only
```

Compute alpha decay for one wallet:

```powershell
python -m pmcopy.cli compute-alpha-decay --wallet 0x... --limit 250 --delays 10,60,300 --exit-rule fixed_24h
```

Debug alpha-decay data quality for the first source trades:

```powershell
python -m pmcopy.cli compute-alpha-decay --limit 10 --delays 60 --position-size-usd 2 --exit-rule latest_available --historical-mode price_history_only --debug-alpha
```

Inspect normalized CLOB price history for one token:

```powershell
python -m pmcopy.cli inspect-price-history --token-id TOKEN_ID
```

Reconstruct wallet position lifecycles:

```powershell
python -m pmcopy.cli reconstruct-positions --wallet 0x... --warmup-days 90
python -m pmcopy.cli reconstruct-promoted-positions --warmup-days 90
python -m pmcopy.cli inspect-position --wallet 0x... --token-id TOKEN_ID
```

Recompute copyability from stored alpha-decay rows:

```powershell
python -m pmcopy.cli compute-copyability --allowed-data-quality exact_orderbook,price_history_proxy
```

Run a basic backtest from stored alpha-decay rows:

```powershell
python -m pmcopy.cli run-backtest --mode in_sample --copy-delay-seconds 60 --position-size-usd 2 --initial-capital 100 --allowed-data-quality price_history_proxy --exit-rule latest_available --include-market-makers --include-latency-bots --include-lucky-wallets
```

Run a lifecycle-copy backtest from reconstructed position events:

```powershell
python -m pmcopy.cli run-backtest --mode in_sample --copy-mode reconstructed_wallet_lifecycle --sizing-mode proportional_to_whale_with_cap --copy-ratio 0.001 --max-position-budget-usd 10 --entry-delay-seconds 60 --exit-delay-seconds 60 --allowed-data-quality price_history_proxy
```

Run split mode:

```powershell
python -m pmcopy.cli run-backtest --mode split --train-start 2026-01-01 --train-end 2026-01-31 --validation-start 2026-02-01 --validation-end 2026-02-28 --test-start 2026-03-01 --test-end 2026-03-31 --copy-delay-seconds 60 --exit-rule latest_available
```

Run walk-forward mode:

```powershell
python -m pmcopy.cli run-backtest --mode walk_forward --start-date 2026-01-01 --end-date 2026-04-01 --copy-delay-seconds 60 --exit-rule latest_available
```

Run sensitivity analysis:

```powershell
python -m pmcopy.cli run-sensitivity --mode in_sample --copy-delays 60,300 --max-entry-degradations 0.02,0.03 --max-spreads 0.03 --position-sizes 2 --max-market-exposures 8 --allowed-data-quality price_history_proxy --exit-rule latest_available
```

Limit a larger grid:

```powershell
python -m pmcopy.cli run-sensitivity --mode in_sample --limit-combinations 24 --allowed-data-quality price_history_proxy --exit-rule latest_available
```

Export a table:

```powershell
python -m pmcopy.cli export-table --table alpha_decay_results --output data/exports/alpha_decay_results.csv
```

Export a backtest report bundle:

```powershell
python -m pmcopy.cli export-report --run-id RUN_ID
```

Launch dashboard:

```powershell
python -m pmcopy.cli dashboard
```

## Dashboard Pages

Candidate Discovery:

- Choose categories and discovery sources.
- Run public-data discovery.
- View candidate wallets.
- Promote selected or top N candidates.

Wallet Screener:

- Ingest promoted wallets.
- Compute wallet metrics and classifications.
- Filter by PnL, edge on volume, exposure returns, volume, trades, markets, active days, drawdown, concentration, categories, exposure confidence, and class labels.

Alpha Decay:

- Select wallets, categories, delays, date range, position size, spread/degradation limits, allowed data-quality levels, and diagnostic exit rule.
- Compute trade-level alpha-decay diagnostics.
- View alpha-decay curves, net PnL by delay, entry degradation, spread distribution, data-quality breakdown, copyability ranking, recent vs historical copyability, simulated rows, and skipped reasons.
- The page warns that `latest_available`, `fixed_24h`, and `hold_to_resolution` are diagnostic; lifecycle copy analysis belongs in Backtest Lab.

Backtest Lab:

- Select wallets, date ranges, mode, capital, copy mode, sizing, delay, exit rule, quality gates, category filters, classification filters, exposure limits, daily loss limits, and duplicate-signal windows.
- Run in-sample, train/validation/test split, or walk-forward simulations.
- View equity curve, summary metrics, split comparison, walk-forward windows, PnL by wallet/category/market, skipped signal reasons, data-quality breakdown, accepted trades or copied lifecycle positions, and CSV export.

Sensitivity Analysis:

- Choose a base backtest configuration and run a bounded parameter grid.
- Vary copy delay, max entry degradation, max spread, position size, and max market exposure.
- View ROI, drawdown, trade count, skipped signal count, data-quality mix, warning flags, heatmaps, and CSV export.
- The page highlights instability but does not optimize or choose a winner.

Results / Reports:

- Compare previous backtest runs, equity curves, train/validation/test behavior, walk-forward windows, sensitivity summaries, and copyability trends.
- Shows transparent rule-based verdicts: `reject`, `needs more data`, `promising for paper trading`, `overfit / too concentrated`, or `poor data quality`.
- Surfaces warnings such as negative ROI, proxy-heavy data, concentration, high drawdown, too many skipped signals, and diagnostic `latest_available` exits.

Data Quality:

- Shows API failures, raw response counts, missing tokens/metadata, missing price history, missing exit prices, alpha data-quality mix, skipped-signal reasons, affected wallets/markets/tokens, and raw JSON samples.

Position Reconstruction:

- Shows reconstructed wallet positions, position events, missing-prior-inventory counts, orphan sells, warmup coverage, lifecycle copy events, lifecycle copy positions, PnL, cap hits, below-min-trade skips, and data-quality mix.

## Public Endpoints Used

- Gamma API: `https://gamma-api.polymarket.com`
- Data API: `https://data-api.polymarket.com`
- CLOB API: `https://clob.polymarket.com`

Observed working endpoints:

- Gamma `/markets`
- Data API `/holders?market=...`
- Data API `/trades?market=...`
- Data API `/trades?user=...`
- Data API `/activity?user=...`
- Data API `/positions?user=...`
- Data API `/closed-positions?user=...`
- Data API `/value?user=...`
- CLOB `/book?token_id=...`
- CLOB `/midpoint?token_id=...`
- CLOB `/last-trade-price?token_id=...`
- CLOB `/price?token_id=...&side=BUY|SELL`
- CLOB `/spread?token_id=...`
- CLOB `/prices-history?market=<asset_id>&interval=max&fidelity=1`

Observed caveats:

- Data API leaderboard variants returned `404`; do not rely on them.
- Market-level Data API activity variants may return `400`.
- Data API `/trades?user=...` can return `400` at high offsets after many successful pages; this is treated as an API/pagination limit, not a fatal error.
- CLOB `/prices-history` requires `market=<asset_id>`, not `token_id=<asset_id>`.

## Database Tables

- `candidate_wallets`
- `wallets`
- `markets`
- `tokens`
- `trades`
- `activity`
- `wallet_snapshots`
- `wallet_metrics`
- `wallet_classification`
- `price_history`
- `orderbook_snapshots`
- `alpha_decay_results`
- `reconstructed_positions`
- `reconstructed_position_events`
- `wallet_copyability`
- `backtest_runs`
- `backtest_trades`
- `lifecycle_copy_runs`
- `lifecycle_copy_events`
- `lifecycle_copy_positions`
- `skipped_signals`
- `sensitivity_runs`
- `sensitivity_results`
- `raw_responses`

## Key Concepts

- Candidate wallet: a discovered public wallet that may or may not be worth deeper analysis.
- Promoted wallet: a candidate selected for ingestion, metrics, alpha decay, and backtesting.
- Alpha decay: how simulated copy PnL changes as copy delay increases.
- Position reconstruction: event-sourced replay of `wallet_address + token_id` inventory from public trades.
- Lifecycle copy: copy simulation driven by reconstructed open/increase and partial/full-exit events.
- Causal sizing: sizing that uses only the current observed whale event and previous copied state.
- Copyable alpha: net simulated copy PnL after delay, spread, fees, slippage, liquidity limits, and data-quality gates.
- Copyability score: an interpretable wallet ranking focused on copy usefulness, not source-wallet PnL alone.
- Data-quality levels: `exact_orderbook`, `price_history_proxy`, `midpoint_proxy`, `last_price_proxy`, and `insufficient_data`.
- Price-history proxy: nearest CLOB price-history point; useful for diagnostics but not the same as executable orderbook liquidity.
- `latest_available`: diagnostic exit rule that can introduce bias.
- `fixed_24h`: exit rule requiring trades old enough to have a price 24 hours after copy time.
- Skipped signal reasons: explicit reasons a simulated copy trade was rejected.
- Duplicate signal filter: avoids multiplying exposure when several wallets emit the same market/side signal close together.
- Exposure limits: wallet, market, category, daily-loss, and capital constraints.
- Train / validation / test split: period split for checking whether a configuration generalizes.
- Walk-forward mode: past-only wallet selection tested on the next forward window.
- Sensitivity analysis: parameter grid diagnostics, not automatic optimization.
- Report verdicts: cautious rule-based labels that explain why a result is rejected, data-limited, overfit, or only paper-trading-worthy.

## Alpha Decay

Alpha decay asks: if a source wallet trades at time `T`, what happens if we copy the same side after a delay such as 10 seconds, 1 minute, or 5 minutes?

For each source trade and delay:

- `copy_time = trade_time + delay`
- copy entry is estimated using the fallback hierarchy below
- entry degradation, spread, slippage, fee, exit price, gross PnL, and net PnL are stored
- skipped rows are still stored with `skip_reason`

This is diagnostic, not final proof of copy-trading profitability. Trade-level exit rules such as `latest_available`, `fixed_24h`, and `hold_to_resolution` are useful for screening, but they do not copy the wallet's real lifecycle.

## Position Reconstruction And Lifecycle Copy

Position reconstruction replays public trades by `wallet_address + token_id`:

- BUY increases token inventory.
- SELL reduces known token inventory.
- SELL with no known inventory becomes `missing_prior_inventory` / `orphan_sell`.
- Buying the opposite binary outcome is a separate token position, not a short of this token.

Lifecycle copy uses `reconstructed_position_events` instead of isolated source trades:

- `open_position` / `increase_position`: copier buys after `entry_delay_seconds`.
- `partial_exit` / `reduce_position` / `full_exit`: copier sells the same fraction after `exit_delay_seconds`.
- `missing_prior_inventory` and `orphan_sell` are skipped instead of guessed.

The preferred lifecycle sizing mode is `proportional_to_whale_with_cap`:

```text
whale_trade_usd = reconstructed event usd_value or shares * price
desired_copy_usd = whale_trade_usd * copy_ratio
remaining_cap = max_position_budget_usd - current_copied_exposure_for_position
actual_copy_usd = min(desired_copy_usd, remaining_cap)
```

If `actual_copy_usd < min_trade_usd` and `execute_small_trades` is false, the add event is skipped with `below_min_trade_usd`. If the cap is reached, the event is skipped with `position_cap_reached` unless partial cap fills are enabled.

This is causal: every copied buy is sized from the current whale trade only. It does not allocate a fixed budget across future whale buys, because those buys are not known at the first entry timestamp. A fixed amount per future entry can overstate or distort lifecycle copy behavior, and a fixed position budget split across all future buys is look-ahead bias.

Lifecycle exits are proportional to the whale's current reduction:

```text
whale_exit_fraction = sold_shares / position_before
copier_exit_fraction = whale_exit_fraction
full_exit closes remaining copied inventory
```

Lifecycle limitations:

- It requires sufficiently complete wallet trade history.
- Missing prior inventory makes the lifecycle invalid rather than guessed.
- `price_history_proxy` is not real executable liquidity.
- This project still does not implement live trading, authenticated order placement, or real paper-trading execution.

## Data-Quality Levels

Every alpha-decay row has exactly one data-quality flag:

- `exact_orderbook`, rank 4: stored/live orderbook snapshot matched the copy timestamp closely enough.
- `price_history_proxy`, rank 3: nearest CLOB price-history point was used.
- `midpoint_proxy`, rank 2: live midpoint was used as fallback.
- `last_price_proxy`, rank 1: live last trade price was used as fallback.
- `insufficient_data`, rank 0: entry or exit could not be estimated.

Default allowed levels for copyability:

- `exact_orderbook`
- `price_history_proxy`

Proxy-based alpha decay is visible but should not be treated as equally reliable as exact orderbook data.

## Historical Fallback Hierarchy

Entry price estimation uses:

1. CLOB price history nearest `copy_time` by default in `price_history_only` mode.
2. Stored orderbook snapshot if `--historical-mode full` is used and a close snapshot exists.
3. CLOB midpoint proxy if `--historical-mode full` is used.
4. CLOB last trade price proxy if `--historical-mode full` is used.
5. `insufficient_data` with a precise skip reason.

Historical orderbooks are usually unavailable unless captured by earlier runs. The code does not mark price-history, midpoint, or last-price data as exact orderbook data.

`alpha_decay.max_price_history_distance_seconds` controls how far the nearest price-history point may be from the target time. The MVP default is 86400 seconds to make diagnostics usable with sparse public history.

Detailed skip reasons include `no_price_history`, `price_history_parse_failed`, `copy_time_before_history`, `copy_time_after_history`, `copy_price_too_far`, `exit_time_in_future`, `no_exit_price`, `exit_price_too_far`, `missing_token_id`, `missing_trade_time`, `missing_whale_price`, `invalid_side`, `invalid_price`, `max_spread_exceeded`, and `max_entry_degradation_exceeded`.

## Fee And Liquidity Model

Fee formula:

```text
fee = shares * fee_rate * p * (1 - p)
```

Category-specific fee rates come from `config/default.yaml`; unknown categories use `default_fee_rate` and record a reason.

For orderbooks:

- BUY walks asks until the configured USD size fills.
- SELL walks bids.
- The fill model returns average fill price, available liquidity, slippage, and fill possibility.

For price-history and midpoint proxies, configured proxy slippage is applied and the row is marked as proxy quality.

## Copyability Score

`copyability_score` is an interpretable 0-100 score based on:

- positive net copy PnL at 1 minute
- positive net copy PnL at 5 minutes
- enough usable alpha-decay observations
- acceptable spread
- acceptable entry degradation
- acceptable data-quality mix
- not classified as market maker or lucky wallet
- recent copyability not decaying

Reasons are stored in `wallet_copyability.copyability_reasons_json`.

## Recent Vs Historical Copyability

For each wallet the system stores:

- `historical_copy_pnl`
- `recent_7d_copy_pnl`
- `recent_30d_copy_pnl`
- `recent_90d_copy_pnl`
- `copyability_trend`

Trend labels:

- `improving`
- `stable`
- `decaying`
- `inactive`
- `insufficient_recent_data`

Trend logic is intentionally simple and should be treated as a screening signal.

## Backtesting

Backtests support two copy modes:

- `diagnostic_trade_level`: uses `alpha_decay_results` as the execution source.
- `reconstructed_wallet_lifecycle`: uses `lifecycle_copy_positions` and `lifecycle_copy_events` created from reconstructed position events.

In diagnostic mode, a candidate row must match the configured wallet set, copy delay, exit rule, date range, and data-quality policy. By default, rows with `insufficient_data`, `skip_reason`, missing entry/exit price, or missing net PnL are excluded from accepted trades.

In lifecycle mode, copied positions must close to become accepted backtest trades. Open, skipped, invalid, cap-hit, below-min-trade, and data-quality failures remain visible in lifecycle metrics and skip summaries.

Supported modes:

- `in_sample`: runs one selected period.
- `split`: runs train, validation, and test periods separately and stores each period's metrics.
- `walk_forward`: selects wallets from past-only lookback windows and tests the next forward window.

The duplicate signal filter clusters accepted signals with the same market and same token or side within `duplicate_signal_window_seconds`. Later duplicates are skipped with `duplicate_signal` instead of multiplying exposure blindly.

Exposure limits are tracked while simulated positions are open:

- `max_wallet_exposure_usd`
- `max_market_exposure_usd`
- `max_category_exposure_usd`
- available capital
- daily realized loss limit

Backtest outputs include total PnL, ROI, max drawdown, win rate, profit factor, average trade size, average holding time, best/worst trade, PnL by wallet/category/market, skipped signal reasons, data-quality mix, and an equity curve. Lifecycle mode also reports closed/open/skipped copied positions, cap-hit count, below-min-trade count, and lifecycle data-quality mix.

`latest_available` is supported for development and diagnostics when fresh trades make `fixed_24h` unusable. It can introduce exit-time bias and should not be treated as final unbiased evidence.

## Sensitivity Analysis

Sensitivity analysis is diagnostic, not optimization. It runs the existing backtest engine over a grid of parameter combinations and stores one summary row per combination.

Supported grid parameters:

- `copy_delay_seconds`
- `max_entry_degradation`
- `max_spread`
- `position_size_usd`
- `max_market_exposure_usd`

Warning flags include:

- `zero_trades`
- `too_few_trades`
- `negative_roi`
- `high_drawdown`
- `high_skipped_signal_rate`
- `high_proxy_data_share`
- `train_positive_validation_negative`
- `validation_positive_test_negative`
- `result_depends_on_one_wallet`
- `result_depends_on_one_market`
- `result_depends_on_one_category`

Stable-looking results should have multiple neighboring parameter combinations with positive ROI, meaningful trade counts, and controlled drawdown. Unstable results tend to work in only one narrow configuration, flip negative nearby, have too few trades, depend on one wallet/market/category, or rely heavily on proxy data.

Large grids are capped for safety. Use `--limit-combinations N` for a bounded run or `--confirm-large-grid` when intentionally running the full grid.

Do not read sensitivity output as proof of profitability. It is a robustness screen and can still overfit historical alpha-decay data.

## Report Verdicts

Report verdicts are rule-based and transparent. They are not financial claims.

- `reject`: negative ROI, high drawdown, train/validation/test failure, or too many skipped signals.
- `needs more data`: too few trades, too few wallets/markets, or validation/test windows too small.
- `promising for paper trading`: positive validation/test or walk-forward behavior, controlled drawdown, enough trades, acceptable data quality, and no extreme concentration.
- `overfit / too concentrated`: one wallet, market, category, or narrow sensitivity configuration drives the result.
- `poor data quality`: proxy or insufficient-data share is too high, or key exit/price data is missing.

Careful wording matters: reports say "promising in this backtest", "diagnostic only", or "requires paper trading"; they do not call any strategy proven profitable.

## Exports

Allowed table exports:

- `candidate_wallets`
- `wallets`
- `wallet_metrics`
- `wallet_classification`
- `alpha_decay_results`
- `reconstructed_positions`
- `reconstructed_position_events`
- `wallet_copyability`
- `backtest_runs`
- `backtest_trades`
- `lifecycle_copy_runs`
- `lifecycle_copy_events`
- `lifecycle_copy_positions`
- `skipped_signals`
- `sensitivity_results`

Exports are written under `data/exports/` unless an explicit output path is provided.

## Wallet Metrics Reliability

Reliable from ingested trade rows when endpoint fields are present:

- `trade_count`
- `market_count`
- `active_days`
- `total_volume`
- `avg_trade_size`
- `median_trade_size`
- category breakdown

Available but not independently reconstructed:

- `realized_pnl` comes from Data API closed-position fields when available.
- `unrealized_pnl` comes from Data API open-position `cashPnl` fields when available.
- `total_pnl` is `realized_pnl + unrealized_pnl` only when those public fields are present.
- `edge_on_volume = total_pnl / total_volume` when both PnL and volume are available.
- `pnl_per_traded_dollar` is the same value as `edge_on_volume`.
- `roi_on_volume` remains in the database for backward compatibility, but the screener labels the value as `edge_on_volume`.
- `edge_on_volume` is not true ROI on capital.
- `return_on_max_capital_at_risk = total_pnl / max_capital_at_risk`.
- `return_on_average_capital_at_risk = total_pnl / average_capital_at_risk`.

Approximate or intentionally null:

- market concentration uses available per-market position PnL and is null if unavailable or zero
- `max_capital_at_risk` prefers reconstructed position exposure over time; otherwise it uses Data API position values as a proxy when available
- `average_capital_at_risk` prefers time-weighted reconstructed exposure; otherwise it uses timestamped position snapshots only when there is enough time information
- exposure confidence is `reconstructed_positions`, `reconstructed_positions_time_weighted`, `data_api_proxy`, `snapshots_proxy`, or `unavailable`
- exposure metrics are null with `unavailable` confidence when data is insufficient
- `win_rate_estimate` is estimated from per-market PnL when available
- `max_drawdown_estimate` is null because no historical equity curve is ingested
- `latency_bot_score` is only computed after alpha-decay rows exist

## Known Limitations

- Public endpoint schemas are not guaranteed stable.
- Wallet PnL is not reconstructed from fills.
- Historical orderbooks may be unavailable.
- Price-history proxy can distort fills.
- Exit price may be missing, especially for recent trades with `fixed_24h` or open markets with `hold_to_resolution`.
- Alpha-decay rows with `insufficient_data` are excluded from backtests by default.
- `fixed_24h` requires exit price availability.
- `latest_available` can introduce bias and is for development/diagnostics.
- Backtest quality depends on alpha-decay data quality.
- Lifecycle copy quality depends on reconstructed position quality.
- Missing prior inventory invalidates lifecycle copy for that position.
- Causal lifecycle sizing avoids look-ahead bias, but still relies on proxy prices unless exact historical execution data is available.
- Walk-forward mode is more honest but may have fewer trades.
- Backtesting is still a local research simulation, not live or paper trading.
- Sensitivity analysis can still overfit if the same wallets and periods are repeatedly inspected.
- Small sample sizes are unreliable.
- Concentrated PnL is dangerous.
- Backtest results require paper trading before any real consideration.

## Suggested Workflow

1. Setup the environment with `pip install -e ".[dev]"`.
2. Initialize DB: `python -m pmcopy.cli init-db`.
3. Discover wallets: `python -m pmcopy.cli discover-wallets --categories sports --max-markets-per-category 30`.
4. Promote candidates: `python -m pmcopy.cli promote-candidates --top 100`.
5. Ingest promoted wallets: `python -m pmcopy.cli ingest-promoted-wallets --limit 100`.
6. Compute metrics: `python -m pmcopy.cli compute-wallet-metrics`.
7. Classify wallets: `python -m pmcopy.cli classify-wallets`.
8. Reconstruct positions: `python -m pmcopy.cli reconstruct-promoted-positions --warmup-days 90`.
9. Compute diagnostic alpha decay: `python -m pmcopy.cli compute-alpha-decay --limit 100 --delays 60,300 --position-size-usd 2 --exit-rule latest_available --historical-mode price_history_only`.
10. Compute copyability: `python -m pmcopy.cli compute-copyability`.
11. Run diagnostic backtest: `python -m pmcopy.cli run-backtest --mode in_sample --copy-delay-seconds 60 --position-size-usd 2 --initial-capital 100 --allowed-data-quality price_history_proxy --exit-rule latest_available`.
12. Run lifecycle backtest: `python -m pmcopy.cli run-backtest --mode in_sample --copy-mode reconstructed_wallet_lifecycle --sizing-mode proportional_to_whale_with_cap --copy-ratio 0.001 --max-position-budget-usd 10 --entry-delay-seconds 60 --exit-delay-seconds 60 --allowed-data-quality price_history_proxy`.
13. Run sensitivity: `python -m pmcopy.cli run-sensitivity --mode in_sample --copy-delays 60,300 --max-entry-degradations 0.02,0.03 --max-spreads 0.03 --position-sizes 2 --max-market-exposures 8 --allowed-data-quality price_history_proxy --exit-rule latest_available`.
14. Open dashboard: `python -m pmcopy.cli dashboard`.
15. Inspect Position Reconstruction.
16. Inspect Results / Reports.
17. Inspect Data Quality.
18. Export CSVs with `export-table` or `export-report`.

## Next Phase

Post-MVP work should focus on paper-trading-style validation outside this research app, better historical execution data, and careful review of data-provider schema changes. Baseline comparison remains intentionally out of scope for now.

## License

MIT. See [LICENSE](LICENSE).

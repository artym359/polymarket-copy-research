from __future__ import annotations

from collections.abc import Iterable

import streamlit as st


CANDIDATE_DISCOVERY = """
### Candidate discovery

`discovery_score` is a discovery-priority score, not a profitability score.

```text
score = 0
+ 3.0  if any source starts with "leaderboard"
+ 0.5  if source includes "manual_seed"
+ 1.5 * (source_count - 1) if source_count > 1
+ 1.0  if last_seen_at is within the last 30 days
+ 0.5 * min(high_volume_market_count, 5)
+ 0.75 * (category_count - 1) if category_count > 1
+ 1.0  if the wallet has a public profile / username

discovery_score = round(score, 4)
```

Candidate table columns:

- `wallet_address`: wallet identifier used for ingestion and later analysis.
- `username`: public profile name if the API returned one.
- `sources`: discovery sources that found the wallet.
- `source_count`: number of distinct discovery sources.
- `categories`: market categories where the wallet was observed.
- `first_source`: first discovery source stored for this wallet.
- `last_seen_at`: latest activity timestamp found during discovery.
- `promoted`: whether the candidate was added to the deeper-analysis `wallets` table.
"""


WALLET_METRICS = """
### Wallet metrics

```text
trade_count = count(ingested trades)
market_count = count(distinct trade.market_id)
active_days = count(distinct date(trade.timestamp))
total_volume = sum(trade.usd_value)
avg_trade_size = mean(trade.usd_value)
median_trade_size = median(trade.usd_value)
```

PnL is read from public Data API position/closed-position fields; it is not reconstructed from fills.

```text
realized_pnl = sum(latest closed-position realized_pnl)
unrealized_pnl = sum(latest open-position cash_pnl)
total_pnl = realized_pnl + unrealized_pnl, using only present values
roi_on_volume = total_pnl / total_volume
edge_on_volume = total_pnl / total_volume
pnl_per_traded_dollar = edge_on_volume
return_on_max_capital_at_risk = total_pnl / max_capital_at_risk
return_on_average_capital_at_risk = total_pnl / average_capital_at_risk
```

Market concentration and win-rate estimates:

```text
pnl_by_market = realized + unrealized PnL grouped by market
top_1_market_pnl_share = max(abs(market_pnl)) / sum(abs(market_pnl))
top_5_market_pnl_share = sum(top 5 abs(market_pnl)) / sum(abs(market_pnl))
win_rate_estimate = count(markets with pnl > 0) / count(markets with non-zero pnl)
both_side_market_share = count(markets with both BUY and SELL trades) / count(markets)
```

`max_drawdown_estimate` is intentionally null at wallet-metrics stage because no historical wallet equity curve is ingested.
"""


CLASSIFICATION = """
### Wallet classification

Insufficient sample flag:

```text
insufficient_sample =
  trade_count < wallet_filters.min_trades
  OR market_count < wallet_filters.min_markets
  OR active_days < wallet_filters.min_active_days
  OR total_volume is missing
```

Market-maker score:

```text
market_maker_score = min(1.0,
    0.30 if total_volume >= classification.likely_market_maker.min_volume else 0
  + 0.25 if trade_count >= classification.likely_market_maker.min_trades else 0
  + 0.20 if abs(roi_on_volume) <= classification.likely_market_maker.max_roi_on_volume else 0
  + 0.20 if both_side_market_share >= classification.likely_market_maker.both_sides_same_market_threshold else 0
  + 0.05 if market_count >= 100 else 0
)
```

Lucky-wallet score:

```text
lucky_wallet_score = min(1.0,
    0.25 if trade_count <= classification.lucky_wallet.max_trades else 0
  + 0.25 if market_count <= classification.lucky_wallet.max_markets else 0
  + 0.20 if active_days <= 14 else 0
  + 0.30 if top_1_market_pnl_share >= classification.lucky_wallet.min_top_1_market_pnl_share else 0
)
```

Latency-bot score, from usable alpha-decay rows:

```text
latency_bot_score = min(1.0,
    0.45 if net copy PnL at 10s > 0 else 0
  + 0.30 if net copy PnL at 60s <= 0 else 0
  + 0.25 if net copy PnL at 300s <= 0 else 0
)
```

Directional score:

```text
if insufficient_sample:
    directional_score = 0
else:
    directional_score = clamp(
        0.50
      + 0.20 if roi_on_volume > 0 else 0
      + 0.15 if market_count >= 20 else 0
      + 0.15 if active_days >= 14 else 0
      - 0.50 * max(market_maker_score, lucky_wallet_score),
      0,
      1
    )
```

Class label priority:

```text
insufficient_sample
else likely_market_maker if market_maker_score >= 0.65
else likely_latency_bot if latency_bot_score >= 0.65
else lucky_wallet if lucky_wallet_score >= 0.65
else likely_directional if directional_score >= 0.50
else unknown
```
"""


POSITION_RECONSTRUCTION = """
### Position reconstruction

For each `wallet_address + token_id`, ingested trades are sorted by timestamp and replayed as inventory events.
Opposite binary-market outcomes are separate `token_id` positions; buying the opposite token is not treated as shorting this token.

```text
BUY:
  position_before = current_inventory
  position_after = current_inventory + shares
  event_type = open_position if inventory was 0 else increase_position

SELL with known inventory:
  matched_shares = min(sell_shares, current_inventory)
  position_after = current_inventory - matched_shares
  event_type = full_exit if position_after == 0 else partial_exit

SELL with zero/unknown inventory:
  event_type = missing_prior_inventory
  status = missing_prior_inventory
```

Position aggregates:

```text
avg_buy_price = sum(buy_shares * buy_price) / total_buy_shares
avg_sell_price = sum(sell_shares * sell_price) / total_sell_shares
realized_pnl = sum(matched_sell_shares * (sell_price - avg_buy_price_at_exit))
status = closed if ending_inventory == 0
       = partial if ending_inventory > 0 and any sell matched
       = open if ending_inventory > 0 and no sell matched
```

Warmup:

```text
warmup_start = analysis_start - warmup_days
warmup trades initialize inventory
only events marked in_analysis_window are copy-entry signals
```

Lifecycle copy:

```text
open_position / increase_position:
  target_entry_time = whale_event_time + entry_delay_seconds
  copier buys after price lookup at target_entry_time

partial_exit / reduce_position / full_exit:
  target_exit_time = whale_event_time + exit_delay_seconds
  whale_exit_fraction = sold_shares / position_before
  copier sells the same fraction of copied inventory
```

Preferred causal sizing:

```text
whale_trade_usd = event.usd_value or event.shares * event.price
desired_copy_usd = whale_trade_usd * copy_ratio
remaining_cap = max_position_budget_usd - current_copied_exposure_for_position
actual_copy_usd = min(desired_copy_usd, remaining_cap)

if actual_copy_usd < min_trade_usd and execute_small_trades is false:
  skip_reason = below_min_trade_usd

if remaining_cap <= 0:
  skip_reason = position_cap_reached
```

This does not split a fixed budget across future whale buys. Future buys are unknown at the current event timestamp, so allocating against them would be look-ahead bias.
"""


ALPHA_DECAY = """
### Alpha decay and simulated copy PnL

For each source trade and each configured delay:

```text
copy_time = trade_time + delay_seconds
```

Entry price lookup order:

```text
1. stored/live orderbook snapshot, if historical_mode allows it and snapshot is close enough
2. nearest CLOB price-history point
3. live midpoint proxy, if full historical mode is enabled
4. live last-trade-price proxy, if full historical mode is enabled
5. insufficient_data
```

Orderbook fill:

```text
BUY walks asks from lowest ask upward
SELL walks bids from highest bid downward
average_price = total_usd_filled / total_shares_filled
available_liquidity = sum(price * shares at usable levels)
slippage = abs(average_price - top_price)
spread = best_ask - best_bid
midpoint = (best_bid + best_ask) / 2
```

Proxy entry adjustment:

```text
proxy_slippage = price * proxy_slippage_bps / 10000
BUY proxy entry = min(1.0, price + proxy_slippage)
SELL proxy entry = max(0.0, price - proxy_slippage)
```

Entry degradation:

```text
BUY  entry_degradation = simulated_entry_price - whale_price
SELL entry_degradation = whale_price - simulated_entry_price
```

Position, fees, and PnL:

```text
shares = position_size_usd / simulated_entry_price
single_side_fee = shares * fee_rate * price * (1 - price)
estimated_fee = entry_fee + exit_fee

BUY  gross_pnl = shares * (exit_price - entry_price)
SELL gross_pnl = shares * (entry_price - exit_price)

net_pnl = gross_pnl - estimated_fee - entry_slippage * shares
```

Legacy `follow_wallet_exit` mode for old rows:

```text
Only BUY source trades are treated as entry events in this first version.
Later SELL trades by the same wallet on the same token/market are treated as exits.

copy_entry_time = whale_entry_time + entry_delay_seconds
copy_exit_time = whale_exit_time + exit_delay_seconds

For partial exits:
segment_fraction = min(remaining_fraction, whale_sell_size / whale_entry_size)
segment_copy_shares = copied_shares * segment_fraction
segment_gross_pnl = segment_copy_shares * (segment_exit_price - copy_entry_price)

realized_exit_fraction = sum(segment_fraction)
skip if realized_exit_fraction < min_exit_fraction
weighted_exit_price = sum(segment_exit_price * segment_fraction) / realized_exit_fraction
```

`reconstructed_wallet_lifecycle` mode:

```text
Use reconstructed_position_events instead of matching a BUY to later SELL heuristically.
copy_entry_time = wallet open/increase event time + entry_delay_seconds
copy_exit_time = wallet partial/full exit event time + exit_delay_seconds

fixed_usd:
  each copied open/increase uses position_size_usd

proportional_to_whale:
  copied_usd = whale_trade_usd * copy_ratio

proportional_to_position:
  first copied entry sets copied_shares / whale_position_after ratio
  later copied inventory follows whale position_after by that ratio

proportional_to_whale_with_cap:
  desired_copy_usd = current_whale_trade_usd * copy_ratio
  actual_copy_usd = min(desired_copy_usd, remaining_position_cap)
  exits reduce copied inventory by the whale exit fraction

gross_pnl = sum(closed_copied_shares * (copy_exit_price - matched_copy_entry_price))
net_pnl = gross_pnl - entry_fees - exit_fees - entry_slippage_cost
```

For copy-trading analysis, Backtest Lab's `copy_mode = reconstructed_wallet_lifecycle` uses dedicated lifecycle-copy tables and the causal `proportional_to_whale_with_cap` sizing mode. Trade-level alpha-decay rows remain useful as diagnostics and screening signals.

Current reconstruction limits:

```text
Diagnostic modes latest_available, fixed_24h, and hold_to_resolution do not follow wallet exits.
The older follow_wallet_exit mode was heuristic and is not exposed in the default CLI/UI choices.
Use reconstructed_wallet_lifecycle for historical copy lifecycle analysis.
This is historical research only; it does not place or monitor live orders.
```

Data-quality rank:

```text
exact_orderbook = 4
price_history_proxy = 3
midpoint_proxy = 2
last_price_proxy = 1
insufficient_data = 0

final row quality = lower-ranked quality of entry and exit estimates
```
"""


COPYABILITY = """
### Copyability

Usable alpha rows:

```text
usable = rows where skip_reason is null
         AND net_pnl is not null
         AND data_quality is in allowed_data_quality_levels
```

PnL windows:

```text
historical_copy_pnl = sum(net_pnl over usable rows)
recent_7d_copy_pnl = sum(net_pnl where trade_time is within 7 days)
recent_30d_copy_pnl = sum(net_pnl where trade_time is within 30 days)
recent_90d_copy_pnl = sum(net_pnl where trade_time is within 90 days)
```

Trend:

```text
if no rows in last 30d and no rows in last 90d: inactive
if no rows in last 30d but some rows in last 90d: insufficient_recent_data
if rows in last 30d < 5: insufficient_recent_data

historical_avg = average(net_pnl over usable rows)
recent_avg = average(net_pnl over recent 30d rows)
margin = max(abs(historical_avg) * 0.25, 0.01)

if recent_avg > historical_avg + margin: improving
elif historical_avg > 0 and recent_avg < 0: decaying
elif recent_avg < historical_avg - margin: decaying
else: stable
```

Copyability score:

```text
score = 0
+ 20 if net PnL at scoring_delay_short_seconds is positive
+ 20 if net PnL at scoring_delay_medium_seconds is positive
+ 15 if usable rows >= min_observations_for_copyability
+ 10 if at least 80% of rows with spread have spread <= max_spread
+ 10 if at least 80% of rows with degradation have degradation <= max_entry_degradation
+ 10 if at least 75% of usable rows have data_quality_rank >= 3
+ 10 if not classified as likely_market_maker or lucky_wallet
+ 5  if trend is improving or stable
- 10 if trend is decaying
- 5  if trend is inactive

copyability_score = clamp(score, 0, 100)
```
"""


BACKTEST = """
### Backtest

Backtests use stored `alpha_decay_results`; they do not fetch live execution prices.

Candidate rows must match wallet selection, delay, exit rule, date range, data quality policy, entry/exit price availability, and configured filters.

Position rescaling:

```text
alpha_size = alpha row raw position_size_usd, or current backtest position_size_usd
scale = backtest.position_size_usd / alpha_size
backtest gross_pnl = alpha gross_pnl * scale
backtest fee = alpha estimated_fee * scale
backtest net_pnl = alpha net_pnl * scale
```

Risk filters:

```text
skip if total_open_exposure + size > current_equity
skip if wallet exposure would exceed max_wallet_exposure_usd
skip if market exposure would exceed max_market_exposure_usd
skip if category exposure would exceed max_category_exposure_usd
skip if daily realized PnL <= -abs(max_daily_loss_usd)
skip duplicate signal if same market and same token/side appears inside duplicate_signal_window_seconds
```

Backtest metrics:

```text
total_pnl = sum(accepted_trade.net_pnl)
roi = total_pnl / initial_capital
win_rate = count(net_pnl > 0) / trade_count
profit_factor = sum(winning net_pnl) / abs(sum(losing net_pnl))
avg_trade_size = average(size_usd)
avg_holding_time = average(exit_time - entry_time)
best_trade = max(net_pnl)
worst_trade = min(net_pnl)

equity starts at initial_capital
equity changes when positions close
max_drawdown = max(previous_equity_peak - later_equity)
```
"""


SENSITIVITY = """
### Sensitivity analysis

Sensitivity runs the same backtest over a parameter grid:

```text
copy_delay_seconds
max_entry_degradation
max_spread
position_size_usd
max_market_exposure_usd
```

Warning flags:

```text
zero_trades: trade_count == 0
too_few_trades: trade_count < sensitivity.too_few_trades_threshold
negative_roi: roi < 0
high_drawdown: max_drawdown / initial_capital >= sensitivity.high_drawdown_fraction
high_skipped_signal_rate: skipped / (skipped + accepted) >= sensitivity.high_skipped_signal_rate
high_proxy_data_share: proxy share >= sensitivity.high_proxy_data_share
result_depends_on_one_wallet/market/category: max(abs(group_pnl)) / sum(abs(group_pnl)) >= concentration_threshold
train_positive_validation_negative: train pnl > 0 and validation pnl < 0
validation_positive_test_negative: validation pnl > 0 and test pnl < 0
```

Robustness label:

```text
stable_candidate if:
  at least 2 configurations have roi > 0 and trade_count >= 5
  AND at least 1 neighboring positive pair exists
  AND fewer than half of configurations have high_drawdown
else unstable
```
"""


REPORTS = """
### Reports and verdicts

Report diagnostics:

```text
proxy_share = price_history_proxy + midpoint_proxy + last_price_proxy shares
insufficient_share = insufficient_data share
wallet/market/category concentration = max(abs(group_pnl)) / sum(abs(group_pnl))
skipped_signal_rate = skipped / (skipped + accepted)
```

Verdict priority:

```text
default: needs more data
poor data quality if proxy_share >= 0.75 OR insufficient_share >= 0.25
overfit / too concentrated if wallet, market, or category concentration >= 0.80
reject if roi < 0 OR max_drawdown / initial_capital >= 0.30 OR train positive but validation negative
needs more data if trade_count < 5 and roi >= 0
promising for paper trading if:
  roi > 0
  trade_count >= reports.min_trades_for_verdict
  max_drawdown / initial_capital < 0.10
  max concentration < 0.60
  proxy_share < data_quality.warn_if_proxy_share_above
```
"""


DATA_QUALITY = """
### Data quality

Quality counters are direct counts over stored tables:

```text
raw API failures = count(raw_responses where success is false)
alpha quality mix = count(alpha_decay_results grouped by data_quality)
missing token ids = count(trades where token_id is null)
missing exit prices = count(alpha_decay_results where eventual_exit_price is null)
missing price history = count(alpha rows skipped for no_price_history or price_history_parse_failed)
skipped signals = count(skipped_signals grouped by reason)
```

Accepted backtest rows should be inspected together with their data-quality mix. Proxy-heavy results are diagnostics, not proof of executable edge.
"""


ALL_SECTIONS = (
    CANDIDATE_DISCOVERY,
    WALLET_METRICS,
    CLASSIFICATION,
    POSITION_RECONSTRUCTION,
    ALPHA_DECAY,
    COPYABILITY,
    BACKTEST,
    SENSITIVITY,
    REPORTS,
    DATA_QUALITY,
)


PAGE_SECTIONS = {
    "candidate": (CANDIDATE_DISCOVERY,),
    "wallet": (WALLET_METRICS, CLASSIFICATION),
    "positions": (POSITION_RECONSTRUCTION, ALPHA_DECAY),
    "alpha": (ALPHA_DECAY, COPYABILITY),
    "backtest": (BACKTEST, ALPHA_DECAY),
    "sensitivity": (SENSITIVITY, BACKTEST),
    "reports": (REPORTS, BACKTEST, COPYABILITY),
    "data_quality": (DATA_QUALITY, ALPHA_DECAY),
}


def render_formula_reference(sections: Iterable[str] = ALL_SECTIONS, *, expanded: bool = False) -> None:
    with st.expander("Formula Reference", expanded=expanded):
        st.markdown(
            "These formulas mirror the current Python implementation. "
            "Config thresholds come from `config/default.yaml` unless overridden in the page controls."
        )
        for section in sections:
            st.markdown(section)

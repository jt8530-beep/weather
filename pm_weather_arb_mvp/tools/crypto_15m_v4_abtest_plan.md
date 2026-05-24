# Crypto15 V4 AB Test Plan

## Current conclusion

V2 and V3 both failed in paper settlement.

V3 result:

- first_dedup = 20
- first_win_rate = 35.0%
- first_pnl = -11.69 USD
- first_max_dd = -24.00 USD
- avg_edge = 0.2013
- assets_positive = 0

This is a NO_LIVE result.

The key failure is not lack of signal count. The key failure is that high model edge did not convert into positive PnL. Edge >= 0.18 had worse performance, so simply raising the edge threshold is not a valid fix.

## What is dead

Do not live trade any of the following:

- V2 model
- V3 model
- max_elapsed 720
- max_elapsed 180
- edge-only filtering
- high-edge-only filtering
- BTC/ETH all-signals

## What remains worth testing

Only two research hypotheses remain:

1. Inverse signal hypothesis
   - If model direction is systematically wrong, the opposite side may have value.
   - Existing CSV cannot test this cleanly because it does not record both YES and NO asks at signal time.

2. Narrow subgroup hypothesis
   - SOL BUY_YES showed a positive slice, but only 5 trades.
   - This is not tradeable yet. Needs 50+ first-dedup samples before any conclusion.

## Required V4 logging change

The scanner must record both legs for every candidate row:

- yes_bid
- yes_ask
- yes_bid_size
- yes_ask_size
- no_bid
- no_ask
- no_bid_size
- no_ask_size
- p_up
- p_down
- selected_action
- inverse_action
- selected_edge
- inverse_edge_at_same_time

Without both legs, inverse testing is contaminated by missing historical order book data.

## V4 test design

Run all three modes in paper only:

A. ORIGINAL
- Buy side selected by model_prob - ask.

B. INVERSE
- Buy opposite side when original edge is high.
- Only if opposite ask at the same timestamp exists and spread/depth pass filters.

C. SOL_BUY_YES_ONLY
- Only SOL BUY_YES.
- Same filters, but no BTC/ETH.

## V4 gates

No live trading unless all are true:

- first_dedup >= 100
- win_rate >= 55%
- pnl > 0
- max_drawdown > -15 USD per 100 trades at 2 USD stake
- at least 2 independent market windows positive
- no single asset contributes more than 70% of profit unless explicitly running a single-asset strategy

## Operational decision

Current status: NO_LIVE.

Allowed action:

- continue paper-only research
- add V4 both-leg logging
- test inverse and SOL-only slices

Forbidden action:

- no deposit
- no wallet
- no real order
- no parameter relaxation to force signals

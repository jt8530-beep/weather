# Wallet Alpha Radar V1

Phase 1 only. Paper/data mode. No wallet, no signing, no live orders.

## Goal

Find Polymarket wallets that may have copyable alpha. The system does **not** assume that a profitable wallet is copyable. The key future metric is delayed follow PnL, but precise delayed follow backtests require recorded order book history. Therefore Phase 1 starts order book recording immediately.

## Phase 1 scope

1. `wallet_candidate_discovery.py`
   - Pull recent public Polymarket trades.
   - Aggregate candidate wallets.
   - Output `paper_logs/wallet_alpha/candidate_wallets.csv`.

2. `wallet_history_builder.py`
   - Pull recent trade history for candidate wallets when the public API supports it.
   - Output `paper_logs/wallet_alpha/wallet_trade_history.csv`.
   - This is best-effort because public API response fields may vary.

3. `wallet_score.py`
   - Score wallets using metrics that do not require historical order book snapshots:
     - trade count
     - active days
     - entry price distribution
     - high-price entry ratio
     - hedge ratio approximation
     - market concentration
     - category concentration
   - Output `paper_logs/wallet_alpha/wallet_scores.csv`.

4. `orderbook_recorder.py`
   - Record top-of-book snapshots for markets touched by candidate wallets.
   - Output SQLite database `paper_logs/wallet_alpha/orderbook_snapshots.sqlite`.
   - This must run for 3-4 weeks before serious delayed follow backtesting.

## What this does not do

- No real wallet connection.
- No private key.
- No order placement.
- No automatic copy trading.
- No claim that any wallet is profitable after delay.

## Recommended commands

```bash
cd /home/ubuntu/weather/pm_weather_arb_mvp
mkdir -p paper_logs/wallet_alpha logs

PYTHONPATH=src .venv/bin/python wallet_alpha_radar/wallet_candidate_discovery.py \
  --limit 1000 \
  --output paper_logs/wallet_alpha/candidate_wallets.csv

PYTHONPATH=src .venv/bin/python wallet_alpha_radar/wallet_history_builder.py \
  --candidates paper_logs/wallet_alpha/candidate_wallets.csv \
  --top-wallets 100 \
  --output paper_logs/wallet_alpha/wallet_trade_history.csv

PYTHONPATH=src .venv/bin/python wallet_alpha_radar/wallet_score.py \
  --history paper_logs/wallet_alpha/wallet_trade_history.csv \
  --candidates paper_logs/wallet_alpha/candidate_wallets.csv \
  --output paper_logs/wallet_alpha/wallet_scores.csv
```

Start recorder:

```bash
nohup bash -lc '
cd /home/ubuntu/weather/pm_weather_arb_mvp
while true; do
  PYTHONPATH=src .venv/bin/python wallet_alpha_radar/orderbook_recorder.py \
    --candidates paper_logs/wallet_alpha/candidate_wallets.csv \
    --db paper_logs/wallet_alpha/orderbook_snapshots.sqlite \
    --top-markets 200 \
    --once
  sleep 30
done
' > logs/wallet_alpha_orderbook_recorder.log 2>&1 &
```

## Phase 1 interpretation

Good Phase 1 result:

- `candidate_wallets.csv` has at least 200 wallets.
- `wallet_trade_history.csv` has enough rows for at least 50 wallets.
- `wallet_scores.csv` produces 10-50 B/A watchlist wallets.
- `orderbook_snapshots.sqlite` grows steadily.

Bad result:

- Trade API fields do not expose wallets reliably.
- Candidate set is tiny.
- Most wallets fail concentration/hedge/high-entry filters.
- Recorder cannot map candidate markets to token IDs.

If Phase 1 is bad, stop. Do not force copy trading.

## Future Phase 2

After 3-4 weeks of order book recording:

- Implement delayed follow backtest.
- Test T+60s / T+180s / T+300s / T+900s / T+1800s.
- Use T+300s as the realistic baseline.
- Only paper-follow wallets whose delayed PnL remains positive.

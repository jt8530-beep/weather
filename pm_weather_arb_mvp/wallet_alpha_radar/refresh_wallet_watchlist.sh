#!/usr/bin/env bash
set -euo pipefail

# Rolling refresh for Wallet Alpha Radar watchlist.
# Data-only: no wallet, no signing, no orders.

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/weather/pm_weather_arb_mvp}"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
MIN_TRACKED="${MIN_TRACKED:-30}"
LIMIT="${LIMIT:-3000}"
TOP_WALLETS="${TOP_WALLETS:-150}"
LIMIT_PER_WALLET="${LIMIT_PER_WALLET:-500}"
MAX_HIGH90="${MAX_HIGH90:-0.40}"
MAX_HEDGE="${MAX_HEDGE:-0.35}"
MIN_TRADES="${MIN_TRADES:-30}"

cd "$PROJECT_DIR"
mkdir -p paper_logs/wallet_alpha logs paper_logs/wallet_alpha/archive

TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_DIR="paper_logs/wallet_alpha/tmp_refresh_$TS"
mkdir -p "$TMP_DIR"

log(){ echo "[$(date -Is)] $*"; }

log "WALLET_WATCHLIST_REFRESH_START ts=$TS limit=$LIMIT top_wallets=$TOP_WALLETS"

# 1) Candidate discovery from recent public trades.
PYTHONPATH=src "$PY" wallet_alpha_radar/wallet_candidate_discovery.py \
  --limit "$LIMIT" \
  --output "$TMP_DIR/candidate_wallets.csv" \
  --raw-output "$TMP_DIR/recent_trades_raw_sample.json" \
  --min-trades 2

# 2) Pull per-wallet history.
PYTHONPATH=src "$PY" wallet_alpha_radar/wallet_history_builder.py \
  --candidates "$TMP_DIR/candidate_wallets.csv" \
  --top-wallets "$TOP_WALLETS" \
  --limit-per-wallet "$LIMIT_PER_WALLET" \
  --output "$TMP_DIR/wallet_trade_history.csv" \
  --errors-output "$TMP_DIR/wallet_history_errors.csv"

# 3) Score wallets.
PYTHONPATH=src "$PY" wallet_alpha_radar/wallet_score.py \
  --history "$TMP_DIR/wallet_trade_history.csv" \
  --candidates "$TMP_DIR/candidate_wallets.csv" \
  --output "$TMP_DIR/wallet_scores.csv"

# 4) Build watchlist and market watchlist.
PYTHONPATH=src "$PY" wallet_alpha_radar/wallet_watchlist_builder.py \
  --scores "$TMP_DIR/wallet_scores.csv" \
  --history "$TMP_DIR/wallet_trade_history.csv" \
  --wallet-output "$TMP_DIR/wallet_watchlist.csv" \
  --market-output "$TMP_DIR/market_watchlist.csv" \
  --top-wallets 80 \
  --max-high90 "$MAX_HIGH90" \
  --max-hedge "$MAX_HEDGE" \
  --min-trades "$MIN_TRADES"

TRACKED=$(python3 - <<PY
import csv
p="$TMP_DIR/wallet_watchlist.csv"
rows=list(csv.DictReader(open(p, encoding='utf-8')))
print(sum(1 for r in rows if r.get('gate') in ('PROVISIONAL_A','PROVISIONAL_B')))
PY
)

if [ "$TRACKED" -lt "$MIN_TRACKED" ]; then
  log "WALLET_WATCHLIST_REFRESH_REJECT tracked=$TRACKED min=$MIN_TRACKED reason=too_few_tracked"
  exit 2
fi

# 5) Enrich markets. This may still leave many unknowns, but it improves recorder prioritization.
if [ -f wallet_alpha_radar/market_metadata_enricher.py ]; then
  PYTHONPATH=src "$PY" wallet_alpha_radar/market_metadata_enricher.py \
    --market-watchlist "$TMP_DIR/market_watchlist.csv" \
    --output "$TMP_DIR/market_watchlist_enriched.csv" \
    --pages 100 \
    --limit 100 \
    --order volume_24hr || cp "$TMP_DIR/market_watchlist.csv" "$TMP_DIR/market_watchlist_enriched.csv"
else
  cp "$TMP_DIR/market_watchlist.csv" "$TMP_DIR/market_watchlist_enriched.csv"
fi

# 6) Archive old files then atomically replace main files.
for f in candidate_wallets.csv wallet_trade_history.csv wallet_scores.csv wallet_watchlist.csv market_watchlist.csv market_watchlist_enriched.csv; do
  if [ -f "paper_logs/wallet_alpha/$f" ]; then
    cp "paper_logs/wallet_alpha/$f" "paper_logs/wallet_alpha/archive/${f%.csv}_$TS.csv"
  fi
done

cp "$TMP_DIR/candidate_wallets.csv" paper_logs/wallet_alpha/candidate_wallets.csv
cp "$TMP_DIR/wallet_trade_history.csv" paper_logs/wallet_alpha/wallet_trade_history.csv
cp "$TMP_DIR/wallet_scores.csv" paper_logs/wallet_alpha/wallet_scores.csv
cp "$TMP_DIR/wallet_watchlist.csv" paper_logs/wallet_alpha/wallet_watchlist.csv
cp "$TMP_DIR/market_watchlist.csv" paper_logs/wallet_alpha/market_watchlist.csv
cp "$TMP_DIR/market_watchlist_enriched.csv" paper_logs/wallet_alpha/market_watchlist_enriched.csv

log "WALLET_WATCHLIST_REFRESH_OK tracked=$TRACKED tmp=$TMP_DIR"
PYTHONPATH=src "$PY" wallet_alpha_radar/wallet_live_activity_report.py \
  --db paper_logs/wallet_alpha/wallet_live_trades.sqlite \
  --output paper_logs/wallet_alpha/wallet_live_activity_report.csv \
  --top 20 || true

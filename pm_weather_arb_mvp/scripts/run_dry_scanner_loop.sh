#!/usr/bin/env bash
set -euo pipefail

SLEEP_SECONDS="${SLEEP_SECONDS:-3}"
LOG_DIR="${LOG_DIR:-logs}"
OUT_CSV="${OUT_CSV:-opportunities.csv}"
mkdir -p "$LOG_DIR"

while true; do
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  echo "[$ts] scan_start"
  PYTHONPATH=src python -m pm_weather_arb scan \
    --pages "${PM_PAGES:-5}" \
    --limit "${PM_LIMIT:-100}" \
    --max-shares "${PM_MAX_SHARES:-100}" \
    --min-shares "${PM_MIN_SHARES:-5}" \
    --min-edge "${PM_MIN_EDGE:-0.005}" \
    --fee-rate "${PM_FEE_RATE:-0.05}" \
    --output "$OUT_CSV" 2>&1 | tee -a "$LOG_DIR/dry_scanner_stdout.log"
  sleep "$SLEEP_SECONDS"
done

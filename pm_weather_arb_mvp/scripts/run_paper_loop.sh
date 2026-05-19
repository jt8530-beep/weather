#!/usr/bin/env bash
set -euo pipefail

SLEEP_SECONDS="${SLEEP_SECONDS:-3}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR" paper_logs
WEATHER_SCOPE_ARGS=()
if [[ "${PM_WEATHER_ONLY:-false}" == "true" ]]; then
  WEATHER_SCOPE_ARGS+=(--weather-only)
fi

while true; do
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  echo "[$ts] paper_start"
  PYTHONPATH=src python -m pm_weather_arb paper \
    "${WEATHER_SCOPE_ARGS[@]}" \
    --pages "${PM_PAGES:-5}" \
    --limit "${PM_LIMIT:-100}" \
    --max-shares "${PM_MAX_SHARES:-20}" \
    --min-shares "${PM_MIN_SHARES:-5}" \
    --min-edge "${PM_SCAN_MIN_EDGE:-0.005}" \
    --paper-min-edge "${PM_PAPER_MIN_EDGE:-0.02}" \
    --fee-rate "${PM_FEE_RATE:-0.05}" \
    --max-notional "${PM_MAX_NOTIONAL_PER_TRADE:-10}" \
    --output opportunities.csv \
    --near-miss-output near_misses.csv \
    --near-miss-top "${PM_NEAR_MISS_TOP:-50}" \
    --paper-csv paper_logs/paper_executions.csv \
    --paper-jsonl paper_logs/paper_executions.jsonl 2>&1 | tee -a "$LOG_DIR/paper_stdout.log"
  sleep "$SLEEP_SECONDS"
done

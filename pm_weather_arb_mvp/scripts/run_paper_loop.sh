#!/usr/bin/env bash
set -euo pipefail

SLEEP_SECONDS="${SLEEP_SECONDS:-3}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR" paper_logs

while true; do
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  echo "[$ts] paper_start"

  args=(
    --pages "${PM_PAGES:-5}"
    --limit "${PM_LIMIT:-100}"
    --max-shares "${PM_MAX_SHARES:-20}"
    --min-shares "${PM_MIN_SHARES:-5}"
    --min-edge "${PM_SCAN_MIN_EDGE:-0.005}"
    --paper-min-edge "${PM_PAPER_MIN_EDGE:-0.02}"
    --paper-min-edge-by-kind "${PM_PAPER_MIN_EDGE_BY_KIND:-YES_NO_BUY_BOTH=0.005,YES_NO_SPLIT_SELL_BOTH=0.005}"
    --paper-kind-min-edge "${PM_KIND_MIN_EDGE:-}"
    --fee-rate "${PM_FEE_RATE:-0.05}"
    --max-notional "${PM_MAX_NOTIONAL_PER_TRADE:-10}"
    --output opportunities.csv
    --near-miss-output near_misses.csv
    --near-miss-top "${PM_NEAR_MISS_TOP:-50}"
    --paper-seen-keys "${PM_PAPER_SEEN_KEYS:-paper_logs/paper_seen_keys.txt}"
    --suspicious-negrisk-output paper_logs/suspicious_negrisk.csv
    --paper-csv paper_logs/paper_executions.csv
    --paper-jsonl paper_logs/paper_executions.jsonl
  )

  if [[ "${PM_ENABLE_NEGRISK_PAPER:-false}" == "true" || "${PM_ENABLE_NEGRISK_PAPER:-false}" == "1" ]]; then
    args+=(--enable-negrisk-paper)
  fi

  if [[ "${PM_TARGET_TEMPERATURE_EVENTS:-true}" == "true" ]]; then
    args+=(--target-temperature-events)
  fi
  if [[ -n "${PM_TEMPERATURE_SEARCH_TERMS:-}" ]]; then
    args+=(--temperature-search-terms "$PM_TEMPERATURE_SEARCH_TERMS")
  fi
  if [[ -n "${PM_TEMPERATURE_SEARCH_LIMIT:-}" ]]; then
    args+=(--temperature-search-limit "$PM_TEMPERATURE_SEARCH_LIMIT")
  fi
  if [[ -n "${PM_TARGET_EVENT_SLUGS:-}" ]]; then
    args+=(--target-event-slugs "$PM_TARGET_EVENT_SLUGS")
  fi

  PM_ALLOW_KINDS="${PM_ALLOW_KINDS:-YES_NO_BUY_BOTH,YES_NO_SPLIT_SELL_BOTH}" \
    PYTHONPATH=src python -m pm_weather_arb paper "${args[@]}" 2>&1 | tee -a "$LOG_DIR/paper_stdout.log"
  sleep "$SLEEP_SECONDS"
done

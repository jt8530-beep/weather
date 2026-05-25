#!/usr/bin/env bash
set -euo pipefail

# Wallet Alpha Radar automation installer.
# Data-only automation: no wallet, no signing, no orders.

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/weather/pm_weather_arb_mvp}"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
UNIT_DIR="$HOME/.config/systemd/user"
LOG_DIR="$PROJECT_DIR/logs"
WA_DIR="$PROJECT_DIR/wallet_alpha_radar"
PAPER_DIR="$PROJECT_DIR/paper_logs/wallet_alpha"

mkdir -p "$UNIT_DIR" "$LOG_DIR" "$PAPER_DIR"

cat > "$UNIT_DIR/wallet-alpha-orderbook.service" <<EOF
[Unit]
Description=Wallet Alpha Radar Orderbook Recorder
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONPATH=src
ExecStart=/bin/bash -lc 'while true; do $PY wallet_alpha_radar/orderbook_recorder.py --db paper_logs/wallet_alpha/orderbook_snapshots.sqlite --market-watchlist paper_logs/wallet_alpha/market_watchlist_enriched.csv --live-trades-db paper_logs/wallet_alpha/wallet_live_trades.sqlite --live-lookback-sec 7200 --live-max-keys 3000 --top-markets 500 --pages 80 --limit 100 --once; sleep 30; done'
Restart=always
RestartSec=10
StandardOutput=append:$LOG_DIR/wallet_alpha_orderbook_recorder.log
StandardError=append:$LOG_DIR/wallet_alpha_orderbook_recorder.log

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/wallet-alpha-live-trades.service" <<EOF
[Unit]
Description=Wallet Alpha Radar Live Trade Recorder
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONPATH=src
ExecStart=/bin/bash -lc 'while true; do $PY wallet_alpha_radar/wallet_live_trade_recorder.py --wallet-watchlist paper_logs/wallet_alpha/wallet_watchlist.csv --gates PROVISIONAL_A,PROVISIONAL_B --db paper_logs/wallet_alpha/wallet_live_trades.sqlite --csv-output paper_logs/wallet_alpha/wallet_live_trades.csv --limit 1000 --gamma-pages 20 --gamma-limit 100 --once; sleep 30; done'
Restart=always
RestartSec=10
StandardOutput=append:$LOG_DIR/wallet_alpha_live_trade_recorder.log
StandardError=append:$LOG_DIR/wallet_alpha_live_trade_recorder.log

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/wallet-alpha-backfill.service" <<EOF
[Unit]
Description=Wallet Alpha Radar Key Backfill

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONPATH=src
ExecStart=/bin/bash -lc '$PY wallet_alpha_radar/wallet_key_backfill.py --db paper_logs/wallet_alpha/wallet_live_trades.sqlite --gamma-pages 100 --gamma-limit 100 --limit 500000 >> logs/wallet_alpha_backfill.log 2>&1'
EOF

cat > "$UNIT_DIR/wallet-alpha-backfill.timer" <<EOF
[Unit]
Description=Run Wallet Alpha key backfill every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true
Unit=wallet-alpha-backfill.service

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/wallet-alpha-health.service" <<EOF
[Unit]
Description=Wallet Alpha Radar Healthcheck

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONPATH=src
ExecStart=/bin/bash -lc 'date -Is >> logs/wallet_alpha_healthcheck.log; $PY wallet_alpha_radar/wallet_alpha_healthcheck.py --orderbook-db paper_logs/wallet_alpha/orderbook_snapshots.sqlite --live-db paper_logs/wallet_alpha/wallet_live_trades.sqlite --stale-sec 600 >> logs/wallet_alpha_healthcheck.log 2>&1'
EOF

cat > "$UNIT_DIR/wallet-alpha-health.timer" <<EOF
[Unit]
Description=Run Wallet Alpha healthcheck every 15 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
Persistent=true
Unit=wallet-alpha-health.service

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/wallet-alpha-decay.service" <<EOF
[Unit]
Description=Wallet Alpha Radar Decay Probe

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONPATH=src
ExecStart=/bin/bash -lc '$PY wallet_alpha_radar/wallet_key_backfill.py --db paper_logs/wallet_alpha/wallet_live_trades.sqlite --gamma-pages 100 --gamma-limit 100 --limit 500000 >> logs/wallet_alpha_decay.log 2>&1; date -Is >> logs/wallet_alpha_decay.log; $PY wallet_alpha_radar/wallet_alpha_decay_probe.py --live-db paper_logs/wallet_alpha/wallet_live_trades.sqlite --orderbook-db paper_logs/wallet_alpha/orderbook_snapshots.sqlite --wallets 0x6088,0xd9de,0x6748,0x18d3,0xb55f,0xce25,0x25f4 --delays 60,300,900 --tolerance-sec 45 --output paper_logs/wallet_alpha/wallet_alpha_decay_rows_top_clean.csv --summary-output paper_logs/wallet_alpha/wallet_alpha_decay_summary_top_clean.csv --limit 500000 >> logs/wallet_alpha_decay.log 2>&1'
EOF

cat > "$UNIT_DIR/wallet-alpha-decay.timer" <<EOF
[Unit]
Description=Run Wallet Alpha decay probe every 4 hours

[Timer]
OnBootSec=30min
OnUnitActiveSec=4h
Persistent=true
Unit=wallet-alpha-decay.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now wallet-alpha-orderbook.service
systemctl --user enable --now wallet-alpha-live-trades.service
systemctl --user enable --now wallet-alpha-backfill.timer
systemctl --user enable --now wallet-alpha-health.timer
systemctl --user enable --now wallet-alpha-decay.timer

# Run first maintenance jobs once so status files/logs appear quickly.
systemctl --user start wallet-alpha-backfill.service || true
systemctl --user start wallet-alpha-health.service || true

echo "WALLET_ALPHA_SYSTEMD_INSTALLED project=$PROJECT_DIR"
systemctl --user --no-pager list-units 'wallet-alpha-*'
systemctl --user --no-pager list-timers 'wallet-alpha-*'

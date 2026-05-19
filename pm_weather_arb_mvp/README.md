# Polymarket Weather Arbitrage MVP

这是一个**只读 dry-run 扫描器**，用于发现 Polymarket 天气相关市场中的三类机会：

1. YES/NO 互补套利：`ask_yes + ask_no + taker_fees < 1`
2. NegRisk / 多结果全套套利：`sum(ask_yes_i + fees_i) < 1`
3. 阈值嵌套套利：例如 `T >= 85` 蕴含 `T >= 80`，扫描 `buy YES(>=80) + buy NO(>=85)`

默认不下单、不读取私钥。先把扫描、日志、机会过滤跑稳定，再接 live execution。

## 安装

```bash
cd pm_weather_arb_mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 运行 dry-run 扫描

```bash
python -m pm_weather_arb scan \
  --pages 5 \
  --limit 100 \
  --max-shares 100 \
  --min-shares 5 \
  --min-edge 0.005 \
  --fee-rate 0.05 \
  --output opportunities.csv
```

参数含义：

- `--pages`：从 Gamma API 拉取多少页活跃事件。
- `--limit`：每页事件数量。
- `--max-shares`：单次扫描每条组合最多按多少份额计算。
- `--min-shares`：机会最小可交易份额。
- `--min-edge`：每份最小利润，0.005 表示每份 0.5 美分。
- `--fee-rate`：天气市场 taker fee rate 默认按 0.05 估算。
- `--output`：保存机会 CSV。

## 本地测试

```bash
python -m unittest discover -s tests
```

## 接 live trading 的顺序

第一阶段只跑 scanner。第二阶段开启 paper executor。第三阶段才接官方 SDK：

```bash
pip install py_clob_client_v2
```

然后按 `src/pm_weather_arb/live_executor.py` 中的接口实现 FOK/FAK 多腿提交。不要跳过 dry-run，因为三类套利都必须按深度、费用、tick size 和部分成交风险过滤。

## 生产运行建议

```bash
# systemd / tmux / docker 均可；先每 2-5 秒 scan 一次，不要高频轮询公开 REST。
while true; do
  python -m pm_weather_arb scan --pages 5 --limit 100 --max-shares 100 --output opportunities.csv
  sleep 3
done
```

真正上线时建议改用 WebSocket market channel 维护本地 order book，REST 只用于启动快照和失败恢复。

---

## Added execution-oriented modules

This version also includes:

```text
src/pm_weather_arb/ws_market.py        WebSocket market-channel book cache
src/pm_weather_arb/paper_executor.py   Paper FOK-style execution simulator
```

### Run paper execution simulation

```bash
PYTHONPATH=src python -m pm_weather_arb paper \
  --pages 5 \
  --limit 100 \
  --max-shares 20 \
  --min-shares 5 \
  --min-edge 0.005 \
  --paper-min-edge 0.02 \
  --fee-rate 0.05 \
  --max-notional 10 \
  --output opportunities.csv \
  --paper-csv paper_logs/paper_executions.csv \
  --paper-jsonl paper_logs/paper_executions.jsonl
```

### First live feature gates

```bash
export PM_LIVE_TRADING=false
export PM_ALLOW_KINDS=YES_NO_BUY_BOTH
export PM_MIN_EDGE=0.02
export PM_MAX_SHARES=20
export PM_MAX_NOTIONAL_PER_TRADE=10
export PM_MAX_BOOK_AGE_MS=500
```

Keep live trading disabled until dry-run and paper logs show stable candidate parsing, low stale-book rates, and reliable residual handling.

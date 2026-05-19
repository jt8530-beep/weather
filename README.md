# Polymarket Weather Live Scanner

这是一个只读 weather range scanner。它的目标是发现天气温度区间市场里的潜在 mispricing，并输出 paper candidates。

安全边界：

```text
不连接钱包
不需要私钥
不签名
不下单
只读取公开数据
只输出 CSV
```

## 当前结构

```text
live_scanner.py                    实时只读扫描入口
polymarket_public_client.py         公开市场与公开深度快照读取
weather_market_parser.py            天气市场解析、城市/日期/温度区间识别
forecast_tools.py                   Open-Meteo 预测与区间概率计算
station_map.json                    城市到官方/近似观测站映射
alternative_data_ocr.py             原始 OCR 模块
alternative_data_ocr_ascii.py       ASCII 兼容 OCR 模块，推荐服务器使用
paper_settlement_tracker.py         paper 结果回填工具
weather_range_scanner_local.py      本地 CSV 快照研究版
weather_scanner_config_v2_ocr.json  主配置文件
```

## 已修正的 6 个缺陷

### 1. 不再只是本地快照

新增 `live_scanner.py` 和 `polymarket_public_client.py`，可读取公开市场数据和公开深度快照。

### 2. 增加实时市场发现与公开深度读取

`polymarket_public_client.py` 负责读取 active markets，并按 token 读取公开 book stats。

### 3. 增加 station mapping

`station_map.json` 提供城市到观测站的初始映射。注意：这只是初始映射，实盘前必须逐个核对 Polymarket resolution rules，不能盲信默认机场。

### 4. 增加 paper settlement 回填

`paper_settlement_tracker.py` 可以把 scanner 输出和最终温度 CSV 合并，计算每 1 美元 paper unit 的胜负与 PnL。

### 5. README 命名一致

当前主入口是：

```bash
python live_scanner.py --config weather_scanner_config_v2_ocr.json --top 20
```

本地快照版是：

```bash
python weather_range_scanner_local.py --config weather_scanner_config_v2_ocr.json --snapshot market_snapshot_example.csv
```

### 6. OCR 兼容修正

新增 `alternative_data_ocr_ascii.py`，不依赖 degree symbol，更适合服务器环境。

## 安装

Ubuntu/Debian：

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_weather_scanner_v2_ocr.txt
playwright install chromium
```

## 运行 live scanner

单次运行：

```bash
python live_scanner.py --config weather_scanner_config_v2_ocr.json --top 20
```

循环运行，每 5 分钟一次：

```bash
python live_scanner.py --config weather_scanner_config_v2_ocr.json --loop --sleep 300 --top 20
```

输出：

```text
paper_logs/live_candidates.csv
```

## OCR 测试

默认 OCR 关闭：

```json
"ocr_enabled": false
```

单独测试 ASCII OCR：

```bash
python alternative_data_ocr_ascii.py --config weather_scanner_config_v2_ocr.json
```

测试稳定后，再把配置改成：

```json
"ocr_enabled": true
```

OCR 只用于修正 forecast：

```text
forecast_adjusted = forecast_raw + ocr_bias * ocr_weight
```

OCR 不直接决定 candidate。

## Paper settlement 回填

准备最终结果 CSV：

```text
city,target_date,temp_type,final_temp_f,source,notes
Seattle,2026-05-06,high,61,official_station,verified manually
```

运行：

```bash
python paper_settlement_tracker.py \
  --candidates paper_logs/live_candidates.csv \
  --results final_results_example.csv \
  --out paper_logs/settled_candidates.csv
```

## 实盘前硬规则

这仍然不是自动交易程序。至少满足以下条件后，才考虑把信号接到任何执行系统：

```text
跑满 30 天
累计 300+ candidates
人工抽查 station mapping
人工核对 resolution rules
统计 YES/NO 或 IN_RANGE/OUT_RANGE 的兑现率
确认利润不是集中在少数极端单
确认 spread 和 depth 在真实可成交层面有效
```

一句话：先证明 edge 存在，再考虑执行。别让 bot 从研究员变成败家子。

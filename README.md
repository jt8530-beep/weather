# Polymarket Weather Paper Scanner V2 + OCR Bias

只读版天气市场 paper scanner。V2 增加了 OCR alternative data 模块，但仍然：

```text
不连接钱包
不需要私钥
不签名
不下单
只输出 paper signals
```

## V2 新增内容

- `alternative_data_ocr.py`
  - 使用 Playwright 打开公开网页
  - 对指定 CSS selector 截图
  - 使用 Pillow 做灰度化、二值化、放大、锐化
  - 使用 pytesseract 做本地 OCR
  - 提取温度、置信度、截图路径、原始 OCR 文本

- `weather_paper_scanner_v2.py`
  - 读取 Polymarket 天气市场
  - 读取 Open-Meteo daily forecast
  - 可选读取 OCR 当前温度
  - 用 `OCR 当前温度 - Open-Meteo 当前温度` 计算 bias
  - 按距离结算日期给 forecast 做小权重修正
  - 再计算每个 YES/NO 的 edge

## 核心原则

OCR 不直接触发交易。OCR 只允许做一件事：

```text
forecast_adjusted = forecast_raw + ocr_bias * ocr_weight
```

默认权重：

```text
当天市场：0.55
明天市场：0.30
更远市场：0.10
最低温市场：默认不使用 OCR 修正
```

如果 OCR 与 Open-Meteo 当前温度差异超过 `ocr_max_abs_bias_f`，默认 8°F，直接拒绝该 OCR 修正，避免识别错误。

## 安装

Ubuntu/Debian：

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr
mkdir -p ~/weather_scanner_v2
cd ~/weather_scanner_v2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_weather_scanner_v2_ocr.txt
playwright install chromium
```

Mac：

```bash
brew install tesseract
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_weather_scanner_v2_ocr.txt
playwright install chromium
```

## 先不开 OCR，正常跑 scanner

默认配置里 `ocr_enabled=false`，先确认基础 scanner 能跑：

```bash
python weather_paper_scanner_v2.py --config weather_scanner_config_v2_ocr.json --once --top 20
```

循环跑：

```bash
python weather_paper_scanner_v2.py --config weather_scanner_config_v2_ocr.json --loop --sleep 300 --top 20
```

## 单独测试 OCR

先在配置里添加真实 URL 和 selector，并把该 source 的 `enabled` 改成 `true`。

```json
{
  "enabled": true,
  "name": "austin_public_dashboard",
  "city": "Austin",
  "url": "https://真实网页地址",
  "selector": "真实CSS选择器",
  "unit": "F"
}
```

然后测试：

```bash
python alternative_data_ocr.py --config weather_scanner_config_v2_ocr.json
```

输出里重点看：

```text
ok
confidence
temp_f
raw_text
screenshot_path
```

如果 `ok=false`，不要开主 scanner 的 OCR 修正。

## 开启 OCR 修正

确认 OCR 单独测试稳定后，再改配置：

```json
"ocr_enabled": true
```

再运行：

```bash
python weather_paper_scanner_v2.py --config weather_scanner_config_v2_ocr.json --once --top 20
```

输出新增字段：

- `forecast_raw_f`: Open-Meteo daily forecast + 固定城市 bias
- `forecast_value_f`: OCR 修正后的 forecast
- `ocr_temp_f`: OCR 当前温度
- `public_current_f`: Open-Meteo 当前温度
- `ocr_bias_f`: OCR 当前温度 - Open-Meteo 当前温度
- `ocr_weight`: 实际应用权重

## 目录输出

```text
paper_logs/signals.csv       paper signal 记录
paper_logs/ocr/*.png         OCR 原图和预处理图
```

## 实盘前硬规则

这版仍然只用于 paper：

```text
至少跑满 30 天
至少记录 300 个以上候选 outcome
人工抽查 OCR 截图
检查 Polymarket resolution rules
统计 YES/NO 哪边 edge 兑现更好
确认利润不是集中在少数极端单
```

在这之前，不接私钥，不自动下单。

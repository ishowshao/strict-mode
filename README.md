# StrictMode - 股票纪律交易工具

> 设定规则、严格执行、及时复盘

StrictMode 是一个**无界面的命令行工具**，帮助你严格执行股票交易纪律，特别是止损管理。它通过自动化止损价计算和更新，确保你按照预设规则执行交易，避免情绪化决策。

## ✨ 核心功能

### 🎯 自动化止损管理

- **Chandelier（吊灯）止损策略**：基于 ATR（平均真实波幅）自动计算和更新止损价
- **只上提不下放**：止损价只会上移保护利润，不会下调增加风险
- **每日自动更新**：收盘后自动拉取最新数据，更新止损价并调整订单
- **多次加仓支持**：同一标的可以多次买入，每次买入都有独立的止损单，但会统一上调到全局 Chandelier 水平

### 📊 交易执行

- **建仓即设止损**：买入时自动设置初始止损单（基于固定百分比）
- **智能清仓**：卖出时自动取消止损单
- **支持限价/市价订单**：灵活的交易方式
- **订单管理**：查看、取消、对账订单，支持按标的或订单ID筛选

### 📈 数据管理与验证

- **复权数据支持**：自动处理复权价格，确保计算准确
- **本地数据缓存**：减少 API 调用，提高效率
- **历史数据查看**：方便查看和验证数据
- **多数据源支持**：默认使用 yfinance（免费），可切换至 Alpha Vantage
- **止损计算验证**：离线验证工具展示 ATR/Chandelier 完整计算过程，支持参数调优和回测分析

### 🔔 通知提醒

- **Telegram 通知**：交易执行、止损更新、触发提醒
- **每日摘要报告**：收盘后自动发送持仓和止损更新摘要

## 🚀 快速开始

### 安装

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -e .[development]
```

### 配置

创建 `.env` 文件（或设置环境变量，程序启动时会自动加载项目根目录的 `.env`）：

```bash
# 数据源配置（默认使用 yfinance，无需额外变量）
# 如需切换回 Alpha Vantage，请取消注释下方两行
# STRICTMODE_DATA_SOURCE=alphavantage
# STRICTMODE_DATA_API_KEY=your_alphavantage_api_key

# Interactive Brokers 配置
STRICTMODE_IB_HOST=127.0.0.1
STRICTMODE_IB_PORT=7497              # 纸面账户默认 7497，实盘账户 7496
STRICTMODE_IB_CLIENT_ID=1

# Telegram 通知（可选）
STRICTMODE_TELEGRAM_BOT_TOKEN=your_bot_token
STRICTMODE_TELEGRAM_CHAT_ID=your_chat_id

# 策略参数（可选，有默认值）
STRICTMODE_ATR_N=22                  # ATR 计算周期
STRICTMODE_ATR_K=3.0                 # ATR 乘数
STRICTMODE_INITIAL_STOP_PCT=0.05     # 初始止损百分比（5%）
STRICTMODE_AUTO_LIQUIDATE=false      # 触发止损是否自动清仓
STRICTMODE_DRAWDOWN_PCT=0.10         # 可选：固定回撤百分比（10%），作为止损的兜底选项
```

### 前置准备

1. **启动 IBKR TWS/Gateway**
   - 纸面交易：启动 Paper Trading 模式（端口 7497）
   - 实盘交易：启动 Live Trading 模式（端口 7496）

2. **准备数据源**
   - 默认使用 yfinance，无需注册或提供密钥
   - 如需切换至 Alpha Vantage，请到 [Alpha Vantage](https://www.alphavantage.co/support/#api-key) 申请 API Key，并在 `.env` 中设置 `STRICTMODE_DATA_SOURCE=alphavantage` 与 `STRICTMODE_DATA_API_KEY`

3. **设置 Telegram Bot（可选）**
   - 创建 Bot：[@BotFather](https://t.me/botfather)
   - 获取 Chat ID：[@userinfobot](https://t.me/userinfobot)

## 📖 使用指南

### 市场与代码格式（重要）

- 当前版本仅支持美股与港股。
- 港股代码必须以 `.HK` 结尾，格式为“4位数字 + .HK”，例如：`0700.HK`（腾讯）、`9988.HK`（阿里巴巴）。
- 美股代码为 1–5 位英文字母（如 `AAPL`、`TSLA`、`MSFT`）。
- 币种默认：美股默认 `USD`；`.HK` 结尾的港股默认 `HKD`。可通过 `--currency` 显式覆盖。
- 如果输入了纯数字但未加 `.HK`，系统会按美股处理；若 yfinance 无数据，CLI 将给出友好提示，而不是异常退出。

### 1. 建仓（买入并设置止损）

```bash
# 基本用法：市价单买入 10 股 AAPL
strictmode buy AAPL 10 --mkt

# 限价单买入
strictmode buy AAPL 10 --limit 150.50

# 自定义初始止损百分比（默认 5%）
strictmode buy AAPL 10 --mkt --initial-stop-pct 0.03

# 自定义 ATR 参数
strictmode buy AAPL 10 --mkt --atr-n 14 --atr-k 2.5

# 多次加仓同一标的（允许）
strictmode buy AAPL 10 --mkt --initial-stop-pct 0.05
strictmode buy AAPL 5 --mkt --initial-stop-pct 0.05  # 第二次买入，会创建新的止损单

# 干跑模式（不实际下单，仅测试）
strictmode buy AAPL 10 --mkt --dry-run

# 港股示例：无需显式 --currency，默认 HKD
strictmode buy 9988.HK 10 --mkt --dry-run
```

**执行流程**：
1. 获取历史价格数据
2. 计算初始止损价（买入价 × (1 - 止损百分比)）
3. 验证数据是否足够计算 Chandelier 止损
4. 下达买入订单和止损单到 IBKR（使用括号单，父单为买入，子单为止损）
5. 更新持仓记录（支持多次加仓，自动计算加权平均成本）
6. 记录到数据库并发送通知

**多次加仓说明**：
- 每次买入都会创建独立的止损单，初始止损价基于该笔买入价格计算
- 数据库中的持仓会合并为一条记录（数量累加，成本加权平均）
- 每日任务会将所有止损单统一上调到 Chandelier 水平（只上提，不下放）

### 2. 清仓（卖出并取消止损）

```bash
# 市价单卖出全部持仓
strictmode sell-all AAPL --mkt

# 限价单卖出
strictmode sell-all AAPL --limit 160.00

# 指定数量卖出（默认卖出全部）
strictmode sell-all AAPL --mkt --qty 5

# 干跑模式
strictmode sell-all AAPL --mkt --dry-run

# 港股示例：无需显式 --currency，默认 HKD
strictmode sell-all 9988.HK --mkt --dry-run
```

**执行流程**：
1. 检查持仓是否存在
2. 取消 IBKR 上的所有止损单（查找该标的的所有 `SM:{symbol}` 标记的止损单）
3. 下达卖出订单
4. 删除数据库中的持仓和止损记录
5. 发送通知

### 3. 订单管理

#### 查看订单

```bash
# 查看所有活跃订单（默认）
strictmode show-orders

# 查看所有订单（包括已完成和已取消）
strictmode show-orders --state all

# 查看已完成的订单
strictmode show-orders --state completed

# 查看已取消的订单
strictmode show-orders --state cancelled

# 实盘账户
strictmode show-orders --paper false
```

#### 取消订单

```bash
# 取消指定订单ID（可重复使用 --id 指定多个）
strictmode cancel --id 123 --id 456 --apply

# 取消某标的的所有订单（包括子订单）
strictmode cancel --symbol AAPL --apply

# 预览取消计划（不实际执行）
strictmode cancel --symbol AAPL

# 实盘账户
strictmode cancel --symbol AAPL --apply --paper false
```

#### 对账止损单

当手动减仓或订单状态不一致时，可以使用 `reconcile-stops` 命令：

```bash
# 查看止损单数量与持仓的对比（预览）
strictmode reconcile-stops AAPL

# 自动取消多余的止损单
strictmode reconcile-stops AAPL --apply

# 实盘账户
strictmode reconcile-stops AAPL --apply --paper false
```

该命令会：
- 对比当前持仓数量与所有止损单的总数量
- 如果止损单数量 > 持仓数量，自动取消多余的止损单（优先取消止损价较高的）
- 避免触发止损时超卖的风险

### 4. 数据管理

```bash
# 同步最近 30 天的数据（默认）
strictmode sync-data AAPL

# 同步指定天数的数据（最多 90 天）
strictmode sync-data AAPL --days 60

# 清空缓存后重新同步
strictmode sync-data AAPL --days 30 --truncate

# 查看缓存的数据
strictmode show-data AAPL

# 港股示例
strictmode sync-data 9988.HK --days 30
strictmode show-data 9988.HK --limit 5

# 查看最近 20 条数据（时间倒序）
strictmode show-data AAPL --limit 20

# 查看指定日期范围的数据
strictmode show-data AAPL --start 2024-01-01 --end 2024-01-31 --ascending
```

### 5. 验证止损计算

使用 `chandelier-table` 命令可以离线验证 ATR/Chandelier 止损计算逻辑，无需连接 IBKR：

```bash
# 基本用法：显示 AAPL 的止损计算表
strictmode chandelier-table AAPL

# 指定入场日期和展示天数
strictmode chandelier-table AAPL --entry 2024-01-15 --days 30

# 自定义 ATR 参数测试不同策略
strictmode chandelier-table AAPL --atr-n 14 --atr-k 2.5

# 导出 CSV 供外部分析
strictmode chandelier-table AAPL --entry 2024-01-15 --csv output.csv

# 倒序查看（从最新到最旧）
strictmode chandelier-table AAPL --ascending false

# 港股示例
strictmode chandelier-table 9988.HK --entry 2024-06-01 --days 60
```

**输出说明**：
- **date**：交易日期
- **adj_close**：复权收盘价
- **ATR**：平均真实波幅
- **Chandelier**：吊灯止损价（最高价 - K × ATR）
- **Stop(trailing)**：追踪止损价（只上提不下放）
- **ΔStop**：相对前一日的止损价变动量
- **n_from_entry**：相对入场日期的天数偏移（0 = 入场日，负数 = 入场前，正数 = 入场后）

**使用场景**：
- **验证计算准确性**：对比手工计算和系统计算结果
- **策略参数调优**：测试不同 ATR 周期和乘数的效果
- **回测分析**：查看历史止损轨迹，评估策略表现
- **数据质量检查**：确认 ATR 预热期数据是否充足

**注意事项**：
- 仅使用本地缓存数据，请先运行 `sync-data` 确保数据最新
- 入场前的数据（n_from_entry < 0）用于 ATR 计算预热，止损值显示为 `-`
- 入场当日（n_from_entry = 0）使用初始百分比止损
- 入场后（n_from_entry >= 1）使用 Chandelier 追踪止损

#### 价格跳动单位验证

使用 `tick-size` 命令可以离线查询和验证价格舍入逻辑，避免 IBKR 错误 110：

```bash
# 查询港股价格的最小跳动单位和舍入结果
strictmode tick-size 9988.HK 154.85

# 指定舍入模式
strictmode tick-size 9988.HK 154.85 --mode nearest  # 最接近（默认）
strictmode tick-size 9988.HK 154.85 --mode down     # 向下舍入
strictmode tick-size 9988.HK 154.85 --mode up       # 向上舍入

# 美股示例
strictmode tick-size AAPL 256.857 --mode down
```

**输出示例**：
```
exchange=SEHK inc=0.1 price=154.85 -> rounded=154.9 (mode=nearest)
exchange=US/SMART inc=0.01 price=256.857 -> rounded=256.85 (mode=down)
```

**使用场景**：
- 调试订单价格问题
- 验证限价单价格是否合规
- 了解不同交易所的价格规则

### 6. 每日自动更新止损

启动后台服务，按市场时区在收盘后（默认 16:15）自动更新所有持仓的止损价：

```bash
strictmode-service
```

**服务功能**：
- 自动获取所有持仓的最新收盘数据
- 计算最新的 Chandelier 止损价
- 对于每个标的的所有止损单，统一上调到 Chandelier 水平（只上提，不下放）
- 如果止损价上移，自动修改 IBKR 止损单
- 如果触发止损，发送通知（或自动清仓）
- 发送每日摘要报告到 Telegram

**服务日志**：
- 启动时会显示时区配置
- 每日更新时会记录每个标的的更新情况
- 错误和警告会记录到数据库的 `audit_log` 表

## 🛡️ 安全特性

### 纸面交易优先

- **默认使用纸面账户**：所有命令默认 `--paper true`
- **明确切换实盘**：需要显式指定 `--paper false` 才能使用实盘账户

### 干跑模式

所有下单命令都支持 `--dry-run` 参数：
- 不会实际下单到 IBKR
- 会执行所有计算和数据库操作
- 适合测试和验证配置

### 数据验证

- **数据新鲜度检查**：确保使用最新的收盘数据
- **数据量检查**：确保有足够的历史数据计算 ATR
- **价格对齐**：所有价格会自动对齐到最小跳动单位，避免 IBKR 错误 110

### 订单标识

- 所有订单使用 `orderRef` 前缀 `SM:{symbol}` 标记，便于识别和管理
- 止损单与买入单通过括号单关联，在 TWS 中清晰可见

## 📊 止损策略说明

### Chandelier（吊灯）止损

基于 ATR 的动态止损策略：

```
止损价 = 最高价(N日) - K × ATR(N日)
```

- **N（ATR 周期）**：默认 22 天，可配置
- **K（ATR 乘数）**：默认 3.0，可配置
- **跟踪逻辑**：新止损价 = max(旧止损价, 新计算的吊灯止损价)
- **只上提不下放**：止损价只会上移保护利润，不会下调增加风险

### 初始止损

建仓时使用固定百分比计算初始止损：

```
初始止损价 = 买入价 × (1 - 止损百分比)
```

默认止损百分比为 5%，可在配置或命令行中修改。

### 可选：固定回撤止损

如果配置了 `STRICTMODE_DRAWDOWN_PCT`，系统会同时计算固定回撤止损：

```
回撤止损价 = 最高价 × (1 - 回撤百分比)
```

最终止损价取两者中的较高者，提供双重保护。

### 多次加仓的止损管理

- **逐笔兜底**：每次买入都有独立的止损单，初始止损价 = 买入价 × (1 - 初始止损百分比)
- **全局统一**：每日任务会将所有止损单上调到 Chandelier 水平（只上提，不下放）
- **触发逻辑**：当价格触发任何一张止损单时，该止损单会被执行，但其他止损单保持不变

**示例**：
- 第 1 笔：100 买，兜底 5% → `STOP1 = 95.00`
- 第 2 笔：110 买，兜底 5% → `STOP2 = 104.50`
- 当日 Chandelier = 98：
  - `STOP1 = max(95.00, 98.00) = 98.00`
  - `STOP2 = max(104.50, 98.00) = 104.50`
- 随后 Chandelier = 107：
  - `STOP1 = max(98.00, 107.00) = 107.00`
  - `STOP2 = max(104.50, 107.00) = 107.00`

## 📁 数据存储

所有数据存储在 SQLite 数据库中（默认：`strictmode.db`），包括：

- **positions**：持仓信息（symbol, qty, avg_price, opened_at, paper）
- **stops**：止损配置和当前价格（symbol, stop_price, method, atr_n, atr_k, updated_at）
- **orders**：订单历史（id, symbol, side, qty, type, limit_price, stop_price, tif, status, placed_at）
- **price_cache**：缓存的日线数据（symbol, date, open, high, low, close, adj_close）
- **audit_log**：操作日志（id, ts, level, msg, ctx）

可通过 `STRICTMODE_DATABASE_URL` 环境变量自定义数据库路径。

## 🔧 高级配置

### 时区设置

```bash
STRICTMODE_TZ_LOCAL=Asia/Singapore      # 本地时区
STRICTMODE_TZ_MARKET=America/New_York   # 市场时区
STRICTMODE_TZ_MARKET2=Asia/Hong_Kong    # 第二个市场时区（可选；启用后同时在该时区16:15再跑一次）

当同时设置两个市场时区时：
- 主时区任务（如美股 ET）只处理“非 .HK”标的。
- 次时区任务（如港股 HKT）只处理“.HK”标的。
- 两次任务都会生成各自的日报摘要；若有 Telegram 配置，会在各自任务结束后发送消息。
```

### 交易时段

```bash
STRICTMODE_RTH_ONLY=true                # 仅常规交易时段
```

### 自动清仓

```bash
STRICTMODE_AUTO_LIQUIDATE=true          # 触发止损时自动清仓（仅实盘）
```

**注意**：自动清仓功能仅对实盘账户生效，纸面账户不会自动清仓。

### IBKR 调试

在买入命令中添加 `--ib-debug` 参数可以打印 IB API 的调试事件：

```bash
strictmode buy AAPL 10 --mkt --ib-debug
```

## 📝 常见问题

### Q: 如何确认配置是否正确？

A: 使用 `--dry-run` 参数测试命令，检查输出和数据库记录。

```bash
strictmode buy AAPL 10 --mkt --dry-run
strictmode show-data AAPL
```

### Q: 止损单没有更新怎么办？

A: 检查：
1. IBKR TWS/Gateway 是否正常运行
2. 每日任务是否正常运行（`strictmode-service`）
3. 数据是否最新（查看日志或使用 `show-data`）
4. 止损价是否实际上移（查看数据库 `stops` 表）

### Q: 如何处理 API 调用限制？

A: 
- 使用 `sync-data` 命令缓存数据到本地
- 每日任务会自动缓存最新数据
- 避免频繁调用 `buy` 命令（每次都会拉取全部历史数据）

### Q: 多次加仓后如何查看止损单？

A: 使用 `show-orders` 命令查看所有活跃订单：

```bash
strictmode show-orders
```

每条止损单都会显示订单ID、父订单ID、止损价等信息。

### Q: 手动减仓后止损单数量不匹配怎么办？

A: 使用 `reconcile-stops` 命令对账：

```bash
strictmode reconcile-stops AAPL --apply
```

### Q: 如何查看操作日志？

A: 数据库中的 `audit_log` 表记录了所有操作。可以使用 SQLite 客户端查看：

```bash
sqlite3 strictmode.db "SELECT * FROM audit_log ORDER BY ts DESC LIMIT 20;"
```

### Q: 为什么买入时会出现错误 110？

A: 错误 110 表示价格不符合最小跳动单位。StrictMode 会自动对齐价格，但如果仍然出现，请检查：
1. 合约是否正确资格化（系统会自动处理）
2. 市场规则是否可用（系统会使用兜底值 0.01）

### Q: 如何切换数据源？

A: 在 `.env` 文件中设置：

```bash
# 使用 yfinance（默认，免费）
STRICTMODE_DATA_SOURCE=yfinance

# 切换到 Alpha Vantage
STRICTMODE_DATA_SOURCE=alphavantage
STRICTMODE_DATA_API_KEY=your_api_key
```

## 🧪 测试

```bash
# 运行所有测试
pytest

# 运行特定测试文件
pytest tests/test_cli.py

# 运行特定测试用例
pytest tests/test_cli.py::test_buy_and_sell_cli
```

## 📄 许可证

本项目为个人使用工具，请根据实际情况使用。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request，但请注意：
- 本项目优先考虑个人使用场景
- 提交前请确保测试通过
- 保持代码风格一致

---

**免责声明**：本工具仅供学习和个人使用。使用本工具进行实盘交易的所有风险由用户自行承担。请在使用前充分测试，并确保理解所有功能。

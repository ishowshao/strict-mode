# StrictMode - 股票纪律交易工具

> 设定规则、严格执行、及时复盘

StrictMode 是一个**无界面的命令行工具**，帮助你严格执行股票交易纪律，特别是止损管理。它通过自动化止损价计算和更新，确保你按照预设规则执行交易，避免情绪化决策。

## ✨ 核心功能

### 🎯 自动化止损管理

- **Chandelier（吊灯）止损策略**：基于 ATR（平均真实波幅）自动计算和更新止损价
- **只上提不下放**：止损价只会上移保护利润，不会下调增加风险
- **每日自动更新**：收盘后自动拉取最新数据，更新止损价并调整订单

### 📊 交易执行

- **建仓即设止损**：买入时自动设置初始止损单
- **智能清仓**：卖出时自动取消止损单
- **支持限价/市价订单**：灵活的交易方式

### 📈 数据管理

- **复权数据支持**：自动处理复权价格，确保计算准确
- **本地数据缓存**：减少 API 调用，提高效率
- **历史数据查看**：方便查看和验证数据

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
STRICTMODE_ATR_K=3.0                  # ATR 乘数
STRICTMODE_INITIAL_STOP_PCT=0.05     # 初始止损百分比（5%）
STRICTMODE_AUTO_LIQUIDATE=false      # 触发止损是否自动清仓
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

# 干跑模式（不实际下单，仅测试）
strictmode buy AAPL 10 --mkt --dry-run
```

**执行流程**：
1. 检查是否已有持仓（防止重复建仓）
2. 获取历史价格数据
3. 计算初始止损价（买入价 × (1 - 止损百分比)）
4. 验证数据是否足够计算 Chandelier 止损
5. 下达买入订单和止损单到 IBKR
6. 记录到数据库并发送通知

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
```

**执行流程**：
1. 检查持仓是否存在
2. 取消 IBKR 上的止损单
3. 下达卖出订单
4. 删除数据库中的持仓和止损记录
5. 发送通知

### 3. 数据管理

```bash
# 同步最近 30 天的数据（默认）
strictmode sync-data AAPL

# 同步指定天数的数据（最多 90 天）
strictmode sync-data AAPL --days 60

# 清空缓存后重新同步
strictmode sync-data AAPL --days 30 --truncate

# 查看缓存的数据
strictmode show-data AAPL

# 查看最近 20 条数据（时间倒序）
strictmode show-data AAPL --limit 20

# 查看指定日期范围的数据
strictmode show-data AAPL --start 2024-01-01 --end 2024-01-31 --ascending
```

### 4. 每日自动更新止损

启动后台服务，每日收盘后（美东时间 16:15）自动更新所有持仓的止损价：

```bash
strictmode-service
```

**服务功能**：
- 自动获取所有持仓的最新收盘数据
- 计算最新的 Chandelier 止损价
- 如果止损价上移，自动修改 IBKR 止损单
- 如果触发止损，发送通知（或自动清仓）
- 发送每日摘要报告到 Telegram

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
- **幂等性保护**：防止重复建仓

## 📊 止损策略说明

### Chandelier（吊灯）止损

基于 ATR 的动态止损策略：

```
止损价 = 最高价(N日) - K × ATR(N日)
```

- **N（ATR 周期）**：默认 22 天，可配置
- **K（ATR 乘数）**：默认 3.0，可配置
- **跟踪逻辑**：新止损价 = max(旧止损价, 新计算的吊灯止损价)

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

## 📁 数据存储

所有数据存储在 SQLite 数据库中（默认：`strictmode.db`），包括：

- **positions**：持仓信息
- **stops**：止损配置和当前价格
- **orders**：订单历史
- **price_cache**：缓存的日线数据
- **audit_log**：操作日志

可通过 `STRICTMODE_DATABASE_URL` 环境变量自定义数据库路径。

## 🔧 高级配置

### 时区设置

```bash
STRICTMODE_TZ_LOCAL=Asia/Singapore      # 本地时区
STRICTMODE_TZ_MARKET=America/New_York   # 市场时区
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

## 📝 常见问题

### Q: 如何确认配置是否正确？

A: 使用 `--dry-run` 参数测试命令，检查输出和数据库记录。

### Q: 止损单没有更新怎么办？

A: 检查：
1. IBKR TWS/Gateway 是否正常运行
2. 每日任务是否正常运行
3. 数据是否最新（查看日志）
4. 止损价是否实际上移（查看数据库）

### Q: 如何处理 API 调用限制？

A: 
- 使用 `sync-data` 命令缓存数据到本地
- 每日任务会自动缓存最新数据
- 避免频繁调用 `buy` 命令（每次都会拉取全部历史数据）

### Q: 如何查看操作日志？

A: 数据库中的 `audit_log` 表记录了所有操作。可以使用 SQLite 客户端查看：

```bash
sqlite3 strictmode.db "SELECT * FROM audit_log ORDER BY ts DESC LIMIT 20;"
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

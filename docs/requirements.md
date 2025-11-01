# StrictMode — Backend-only Trading Discipline Engine (Python)

## 目标

* 我个人使用的**无 UI 后端**程序。
* **两类能力**：

  1. **指令（CLI）**：`建仓` 与 `清仓`。建仓时**同时下达初始止损单**。
  2. **服务（Scheduler）**：**每日**拉取 EOD（日线）OHLC（复权口径），计算/更新**回撤卖出（吊灯/ATR）**的**跟踪止损价**，必要时通过 IBKR 调整止损单，并发送通知。

---

## 技术栈

* Python 3.11+
* 核心依赖：`ib-insync`, `pandas`, `httpx`, `APScheduler`, `SQLModel`(或 `sqlite3`), `pydantic-settings`, `pandas-ta`（或自写 ATR）
* 通知：Telegram Bot（HTTP 直发）
* 数据源：Alpha Vantage（只需日线，带 `adjClose`），按比例生成复权 O/H/L。

---

## 复权口径（强制）

* 对于每个交易日，计算比例 `r = adjClose / close`。
* 生成：

  * `adjOpen = open * r`
  * `adjHigh = high * r`
  * `adjLow  = low  * r`
  * `adjClose` 直接用源数据
* 全部纪律与止损**仅基于** `adjOpen/adjHigh/adjLow/adjClose`。

---

## 算法：回撤卖出（吊灯/ATR）

* 参数（可配置，给默认）：

  * **ATR 窗口** `N = 22`
  * **乘数** `K = 3.0`
* **多头策略**的当日候选止损价：

  * `chandelier = HighestHigh(N) - K * ATR(N)`，其中 `HighestHigh(N)` 用**复权** `adjHigh` 计算。
* **跟踪逻辑**：

  * 实际使用的 `stop_today = max(stop_yesterday, chandelier_today)`（只上提，不下放）。
  * 若收盘后 `adjClose_today <= stop_today`，在**次一交易时段**触发卖出（或立即发出强提醒/市价清仓，取决于配置）。
* 可选：若用户配置了**固定回撤百分比**，可作为备选或兜底：`stop_drawdown = peak_price * (1 - dd%)`，实际止损取两者更高者 `max(chandelier, stop_drawdown)`。
* **仓位建立时的初始止损**：直接按交易成本下方 `initial_stop_pct`（默认 5%）设定，同步写入数据库，并在次日开始套用上述吊灯跟踪逻辑。

---

## 指令层（CLI）

### 1. 建仓（同时下止损）

> 程序启动时会自动加载项目根目录的 `.env`，也可以直接设置系统环境变量。

```
strictmode buy <SYMBOL> <QTY>
  [--limit <price> | --mkt]
  [--tif DAY|GTC]               # 默认 GTC
  [--sl-type chandelier]        # 目前固定为 chandelier
  [--atr-n 22] [--atr-k 3.0]
  [--initial-stop-pct 0.05]     # 初始止损固定百分比
  [--rth true|false]            # 仅常规交易时段，默认 true
  [--paper true|false]          # 默认 true（强制先走 paper）
  [--currency USD]
```

**行为**：

1. 连接 IBKR（TWS/IB Gateway，优先 paper）。
2. 下**开仓单**（限价/市价）。
3. 使用下单价与配置的**固定百分比**计算初始止损价（例如 95%）。
   * 该初始值立即写入止损单，并作为后续吊灯止损的起点。
   * 为确保后续跟踪顺利，在入场时仍会验证当日数据足够计算 Chandelier（但初始值本身不取用 Chandelier）。
4. 同步下达**止损单**（`STP`/`STP LMT`），TIF 采用 `GTC`。
5. 入库：`positions`, `orders`, `stops`，并发 Telegram 通知。
6. **幂等**：若发现已有未平仓同符号仓位，拒绝或提示使用 `scale-in`（本期不支持）。

### 2. 清仓

```
strictmode sell-all <SYMBOL>
  [--mkt | --limit <price>]
  [--tif DAY|GTC]
  [--paper true|false]
```

**行为**：

1. 取消该 SYMBOL 现有**止损单**。
2. 执行**平仓单**。
3. 入库并通知。

> 备注：为简化，**本期不实现**分批减仓/加仓、OCO/Bracket 的多腿联动；保留接口位。

---

### 3. 数据缓存与检视

```
strictmode sync-data <SYMBOL>
  [--days 30]                   # 最大 90 天
  [--truncate]                  # 同步前清空该 symbol 的缓存

strictmode show-data <SYMBOL>
  [--limit 10]
  [--start YYYY-MM-DD]
  [--end YYYY-MM-DD]
  [--ascending]                 # 按时间顺序输出
```

**行为**：

1. `sync-data` 会调用 Alpha Vantage 拉取最近 `days` 天（上限 90）的复权日线数据，并写入本地 `price_cache` 表（同一天数据会覆盖旧值）。
2. `show-data` 从 SQLite 中读取缓存记录，根据过滤条件打印 OHLC/adj close，供人工校验数据正确性。
3. 若需要重置缓存，可加 `--truncate` 先删除后重新写入；日常使用建议保留历史，避免触发 Alpha Vantage 频率限制。

---

## 服务层（每日任务）

**调度**：使用 `APScheduler` 在**美东收盘后**（例如 16:15 America/New_York）运行：

1. 拉取目标标的的**当日 EOD 复权 OHLC**（若当日非交易日则跳过）。
2. 计算最新 `stop_today`（按“只上提不下放”）。
3. 若 `stop_today` **上移** → 调整 IBKR 上的止损单（修改价格）；
4. 若 **触发**（`adjClose <= stop_today`）→ 按配置：

   * a) **仅提醒**：Telegram 强通知；
   * b) **自动清仓**：取消止损单并下达市价平仓（仅当 `--auto-liquidate=true` 且非纸面限制）。
5. 入库：`stops`（历史轨迹）、`logs`，并推送日报摘要。

**数据新鲜度防线**：

* 若最新 EOD 数据日期 < 目标交易日 → **拒绝调整单**并发出“数据滞后”错误通知。
* 若 ATR 计算窗口不足 → 暂停策略/仅提醒。

---

## 数据模型（SQLite）

* `symbols(symbol TEXT PK)`
* `positions(symbol TEXT PK, qty REAL, avg_price REAL, opened_at DATETIME, paper INTEGER)`
* `stops(symbol TEXT PK, stop_price REAL, method TEXT, atr_n INT, atr_k REAL, updated_at DATETIME)`
* `price_cache(symbol TEXT, date DATE, open REAL, high REAL, low REAL, close REAL, adj_close REAL, PRIMARY KEY(symbol,date))`
* `orders(id TEXT PK, symbol TEXT, side TEXT, qty REAL, type TEXT, limit_price REAL, stop_price REAL, tif TEXT, status TEXT, placed_at DATETIME)`
* `audit_log(id INTEGER PK, ts DATETIME, level TEXT, msg TEXT, ctx JSON)`

---

## 配置（.env / pydantic-settings）

```
STRICTMODE_TZ_LOCAL=Asia/Singapore
STRICTMODE_TZ_MARKET=America/New_York

STRICTMODE_DATA_SOURCE=alphavantage         
STRICTMODE_DATA_API_KEY=xxxx

STRICTMODE_ATR_N=22
STRICTMODE_ATR_K=3.0
STRICTMODE_INITIAL_STOP_PCT=0.05           # 初始止损按成本下方 5%

STRICTMODE_IB_HOST=127.0.0.1
STRICTMODE_IB_PORT=7497                     # paper 默认 7497
STRICTMODE_IB_CLIENT_ID=1

STRICTMODE_TELEGRAM_BOT_TOKEN=xxx
STRICTMODE_TELEGRAM_CHAT_ID=123456

STRICTMODE_AUTO_LIQUIDATE=false             # 触发止损是否自动清仓
STRICTMODE_RTH_ONLY=true
STRICTMODE_DRAWDOWN_PCT=0.10                # 可选：固定回撤百分比（如 0.10 表示 10%），作为止损的兜底选项
```

---

## 目录结构

```
strictmode/
  config.py
  datasrc/
    base.py
    av.py            # 拉日线 + 复权 O/H/L
  rules/
    chandelier.py    # 计算 stop_today
  engine/
    broker_ib.py     # ib-insync 封装（下单、改单、撤单）
    scheduler.py     # APScheduler 定时任务
    notifier.py      # Telegram
    journal.py       # SQLite I/O
  cli.py             # click/typer 实现 buy / sell-all
tests/
Dockerfile
pyproject.toml
```

---

## 错误与安全边界

* **干跑模式（dry-run）**：任何下单类操作可加 `--dry-run`，仅打印计划与写库，不触发 IBKR。
* **纸面账户优先**：默认 `paper=true`，非显式切换不允许实盘。
* **幂等性**：对“重复 buy/sell-all”的保护；对“已存在止损单”的更新而非重复创建。
* **时区与交易日**：统一以 `TZ_MARKET` 判定“当日收盘后”；假日自动跳过。
* **网络与 API 失败**：重试（指数退避）；失败则发告警并不改单。

---

## 测试（最小集）

1. **ATR/吊灯计算**（纯函数）：

   * 给出固定样本 K 线 → 验证 `ATR(N)`、`HighestHigh(N)`、`stop_today`。
2. **复权比例**：校验 `adjOpen/High/Low` 等比缩放正确性。
3. **CLI 幂等**：重复 `buy` 不产生第二个止损单；`sell-all` 会撤止损再平仓。
4. **服务迭代**：前一日 `stop` < 当日 `chandelier` 时上移；否则不动。
5. **触发清仓**：当 `adjClose <= stop` 时按配置行为正确（仅提醒或下单）。
6. **数据滞后保护**：当日无 EOD、不足 `N` 根 → 不更新止损并告警。


---

## 交付验收（Definition of Done）

* `strictmode buy` 能在 **paper 环境**下单并创建对应的止损单（可在 IBKR 客户端看到）。
* 每日任务能拉到最新 EOD，成功**上提**止损并写入 SQLite，Telegram 能收到摘要。
* 当价格触发止损阈值时，至少能**发出清仓通知**（`AUTO_LIQUIDATE=false` 情况）。
* 单元/集成测试通过（含计算与幂等用例），Docker 镜像可启动并在日志中看到定时任务注册。

# StrictMode 项目完成情况检查报告

## ✅ 已完成部分

### 1. CLI命令层
- ✅ `buy` 命令：基本实现
  - 支持限价/市价订单
  - 幂等性检查（防止重复建仓）
  - 自动计算并下达止损单
  - 数据库记录和通知
  
- ⚠️ `sell-all` 命令：部分实现
  - ✅ 删除数据库记录
  - ❌ **缺失**：未调用IBKR取消实际止损单

### 2. 数据源
- ✅ Alpha Vantage实现：`datasrc/av.py`
- ✅ 复权口径计算：正确实现 `adjOpen/adjHigh/adjLow` 的等比缩放

### 3. 算法
- ✅ ATR计算：`rules/chandelier.py`
- ✅ 吊灯止损计算：`chandelier_exit()`
- ✅ 跟踪止损逻辑：`trailing_stop()` 实现"只上提不下放"
- ✅ 固定回撤百分比支持：可选 `drawdown_pct`

### 4. Broker集成
- ✅ IBKR下单：`place_order()` 实现
- ⚠️ 取消订单：`cancel_order()` 存在但实现不完整
- ❌ **缺失**：修改订单功能 `modify_order()`

### 5. 通知
- ✅ Telegram通知：`engine/notifier.py` 完整实现

### 6. 数据库
- ✅ `positions` 表：完整实现
- ✅ `stops` 表：完整实现
- ✅ `orders` 表：完整实现
- ✅ `audit_log` 表：完整实现
- ❌ **缺失**：`symbols` 表
- ❌ **缺失**：`price_cache` 表

### 7. 配置
- ✅ 环境变量支持：`config.py` 支持所有必需配置项
- ✅ 配置项包括：IB设置、Telegram、数据源、策略参数

### 8. 测试
- ✅ CLI测试：`test_cli.py`
- ✅ 算法测试：`test_chandelier.py`
- ✅ 数据源测试：`test_datasource.py`

---

## ❌ 缺失的关键部分

### 1. 每日任务实现（⚠️ 最重要）

**文件**：`strictmode/engine/scheduler.py`

**现状**：只有调度框架，缺少实际任务逻辑

**需要实现的功能**：
```python
def daily_update_task():
    """
    1. 获取所有持仓（从positions表）
    2. 对每个持仓：
       a. 拉取最新EOD复权数据（从Alpha Vantage）
       b. 检查数据新鲜度（最新日期是否匹配今日）
       c. 计算最新 stop_today（使用trailing_stop）
       d. 获取当前止损价（从stops表）
       e. 如果 stop_today > 当前止损价：
          - 修改IBKR止损单（需要modify_order方法）
          - 更新stops表
       f. 如果 adjClose <= stop_today（触发）：
          - 如果 AUTO_LIQUIDATE=true：
             * 取消止损单
             * 下达市价平仓单
          - 否则：
             * 发送Telegram强通知
       g. 缓存价格数据到price_cache表
    3. 发送日报摘要（Telegram）
    4. 记录audit_log
    """
```

### 2. Broker修改订单功能

**文件**：`strictmode/engine/broker_ib.py`

**需要添加**：
```python
def modify_order(self, order_id: int, stop_price: float | None = None, limit_price: float | None = None) -> OrderResponse:
    """修改现有订单的价格"""
    # 需要实现IBKR的订单修改逻辑
```

### 3. CLI sell-all命令完善

**文件**：`strictmode/cli.py` - `sell_all()` 函数

**问题**：第219-221行只删除了数据库记录，没有取消IBKR实际订单

**需要添加**：
```python
# 在删除数据库记录之前，需要：
# 1. 查询IBKR上该symbol的止损单
# 2. 取消这些止损单
```

### 4. 数据库Schema补充

**文件**：`strictmode/engine/journal.py`

**需要添加两个表**：

```python
# symbols表
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY
)

# price_cache表
CREATE TABLE IF NOT EXISTS price_cache (
    symbol TEXT,
    date DATE,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    PRIMARY KEY(symbol, date)
)
```

**需要添加的方法**：
- `get_all_positions()` - 获取所有持仓
- `cache_price_data()` - 缓存价格数据
- `get_cached_price()` - 获取缓存的价格数据

### 5. 主入口文件

**需要创建**：`strictmode/main.py` 或 `strictmode/service.py`

**功能**：
```python
def main():
    """启动每日任务调度器"""
    container = DependencyContainer(settings)
    scheduler = DailyScheduler(timezone=settings.tz_market)
    
    # 注册每日任务
    scheduler.add_daily_job(
        lambda: daily_update_task(container),
        hour=16,
        minute=15
    )
    
    scheduler.start()
    # 保持运行
```

### 6. 数据新鲜度检查

**需要实现**：
- 检查最新EOD数据日期是否匹配目标交易日
- 如果数据滞后，拒绝调整订单并发送告警

### 7. 测试补充

**需要添加的测试**：
- `test_scheduler.py` - 每日任务测试
- `test_data_freshness.py` - 数据滞后保护测试
- `test_trigger_liquidation.py` - 触发清仓测试
- `test_order_modification.py` - 订单修改测试

---

## 📊 完成度统计

| 模块 | 完成度 | 状态 |
|------|--------|------|
| CLI命令 | 85% | ⚠️ 需完善sell-all |
| 数据源 | 100% | ✅ |
| 算法 | 100% | ✅ |
| Broker集成 | 60% | ⚠️ 缺修改订单 |
| 通知 | 100% | ✅ |
| 数据库 | 67% | ⚠️ 缺2个表 |
| 配置 | 100% | ✅ |
| 每日任务 | 20% | ❌ 只有框架 |
| 测试 | 50% | ⚠️ 缺关键测试 |

**总体完成度：约 70%**

---

## 🎯 优先级建议

### P0（必须完成）
1. ✅ 实现每日任务核心逻辑
2. ✅ 添加Broker修改订单功能
3. ✅ 完善sell-all命令（取消IBKR订单）
4. ✅ 补充数据库schema（symbols, price_cache）

### P1（重要）
5. ✅ 实现数据新鲜度检查
6. ✅ 创建主入口文件启动scheduler
7. ✅ 添加关键测试用例

### P2（优化）
8. ✅ 错误重试机制（指数退避）
9. ✅ 交易日判断（跳过假日）
10. ✅ 性能优化和日志完善

---

## 🔍 代码位置参考

- CLI命令：`strictmode/cli.py:70-257`
- 每日任务框架：`strictmode/engine/scheduler.py:10-22`
- Broker下单：`strictmode/engine/broker_ib.py:62-80`
- 算法计算：`strictmode/rules/chandelier.py:49-64`
- 数据库：`strictmode/engine/journal.py:42-199`


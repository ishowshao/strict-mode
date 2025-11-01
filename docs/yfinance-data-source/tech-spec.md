# 数据源切换技术方案：Alpha Vantage → yfinance

## 1. 背景与目标

### 1.1 背景
- Alpha Vantage 已将 `TIME_SERIES_DAILY_ADJUSTED`、实时/15分钟延迟美股数据等核心功能划入收费套餐
- 需要切换为免费替代方案以降低运营成本
- yfinance 是基于 Yahoo Finance 的免费 Python 库，提供类似的历史数据功能

### 1.2 目标
- 将数据源从 Alpha Vantage 切换为 yfinance
- 保持现有功能接口不变，确保向后兼容
- 最小化代码改动，降低切换风险
- 保持数据格式和计算逻辑的一致性

---

## 2. 当前实现分析

### 2.1 数据源架构

项目采用抽象数据源模式，核心接口定义在 `strictmode/datasrc/base.py`：

```python
class AbstractDataSource(abc.ABC):
    @abc.abstractmethod
    def get_adjusted_daily(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> AdjustedDailyBar:
        raise NotImplementedError
```

### 2.2 Alpha Vantage 实现特点

**文件位置**：`strictmode/datasrc/av.py`

**关键特性**：
- 使用 `httpx` 进行 HTTP 请求
- API 端点：`https://www.alphavantage.co/query`
- 使用 `TIME_SERIES_DAILY_ADJUSTED` 函数
- 需要 API Key（通过环境变量 `STRICTMODE_DATA_API_KEY` 配置）
- 返回格式：JSON，包含 `Time Series (Daily)` 字段
- 数据字段：`1. open`, `2. high`, `3. low`, `4. close`, `5. adjusted close`, `6. volume`

**数据处理逻辑**：
1. 解析 JSON 响应，提取日线数据
2. 根据 `start` 和 `end` 参数过滤日期范围
3. 计算复权比例：`ratio = adjusted_close / close`
4. 生成复权价格：`adj_open/high/low = open/high/low * ratio`
5. 构建 DataFrame，列包括：`date`, `open`, `high`, `low`, `close`, `adj_open`, `adj_high`, `adj_low`, `adj_close`, `volume`, `ratio`
6. 返回 `AdjustedDailyBar` 对象

### 2.3 数据源使用位置

1. **CLI 命令** (`strictmode/cli.py`)：
   - `buy` 命令：获取完整历史数据用于计算初始止损
   - `sync-data` 命令：同步指定天数的数据到本地缓存

2. **每日任务** (`strictmode/engine/daily_task.py`)：
   - `daily_update_task`：获取最新 EOD 数据，更新止损价

3. **配置管理** (`strictmode/config.py`)：
   - `DataSettings` 类：存储 `api_key` 和 `source` 配置
   - `DependencyContainer.data_source()`：工厂方法，返回数据源实例

4. **测试** (`tests/test_datasource.py`)：
   - Mock 测试：`MockAlphaVantage` 类用于测试复权计算逻辑

### 2.4 数据格式要求

**返回类型**：`AdjustedDailyBar`

**DataFrame 结构**：
- **索引**：`date` (date 类型)
- **必需列**：
  - `open`, `high`, `low`, `close`：原始价格
  - `adj_open`, `adj_high`, `adj_low`, `adj_close`：复权价格
  - `volume`：成交量
  - `ratio`：复权比例（可选，用于调试）

**数据质量要求**：
- 数据按日期升序排列（`sort_index()`）
- 包含至少 1 条记录
- 必须支持 `start` 和 `end` 日期过滤
- 所有价格字段为 `float` 类型

---

## 3. yfinance 能力分析

### 3.1 基本能力

**yfinance 库特点**：
- 免费、无需 API Key
- 基于 Yahoo Finance 数据源
- 支持全球主要股票市场
- 提供历史数据、实时数据、公司信息等

**核心方法**：
```python
import yfinance as yf

ticker = yf.Ticker(symbol)
# 获取历史数据
hist = ticker.history(start=start_date, end=end_date, auto_adjust=True)
# 或使用便捷方法
data = yf.download(symbol, start=start_date, end=end_date)
```

### 3.2 数据格式

**yfinance 返回的 DataFrame**：
- **索引**：`DatetimeIndex`（日期时间）
- **列**：
  - `Open`, `High`, `Low`, `Close`：原始价格
  - `Adj Close`：调整后收盘价（已复权）
  - `Volume`：成交量
  - `Dividends`、`Stock Splits`：分红和拆股信息（如果启用）

**关键差异**：
1. **列名大小写**：yfinance 使用首字母大写（`Open`, `Close`），而当前代码使用小写（`open`, `close`）
2. **索引类型**：yfinance 返回 `DatetimeIndex`，需要转换为 `date` 索引
3. **复权处理**：
   - yfinance 的 `history(auto_adjust=True)` 会自动复权，但只提供 `Adj Close`
   - 需要手动计算 `adj_open/high/low`，或使用 `auto_adjust=False` 自行计算
4. **日期范围**：
   - `start` 和 `end` 参数支持 `str`（如 `"2023-01-01"`）或 `datetime` 对象
   - 如果不指定，默认返回最近 1 年的数据

### 3.3 优势与限制

**优势**：
- ✅ 免费，无需 API Key
- ✅ 支持全球市场（美股、港股、A股等）
- ✅ 数据更新及时（通常延迟 15-20 分钟）
- ✅ 支持批量下载多个标的
- ✅ 自动处理分红、拆股等公司行为

**限制与注意事项**：
- ⚠️ **数据延迟**：非实时数据，通常延迟 15-20 分钟
- ⚠️ **请求频率限制**：虽然没有官方限制，但过于频繁可能被限制
- ⚠️ **网络依赖**：依赖 Yahoo Finance 的可用性
- ⚠️ **数据源变更**：Yahoo Finance 可能随时调整 API，需要保持库更新
- ⚠️ **历史数据范围**：部分标的可能没有完整的长期历史数据

---

## 4. 技术改造方案

### 4.1 总体策略

采用**策略模式**和**工厂模式**结合的方式：

1. **保留抽象接口**：`AbstractDataSource` 接口保持不变
2. **新增 yfinance 实现**：创建 `YFinanceDataSource` 类
3. **配置驱动切换**：通过 `STRICTMODE_DATA_SOURCE` 环境变量选择数据源
4. **保持向后兼容**：Alpha Vantage 实现保留，便于回滚

### 4.2 实现方案

#### 4.2.1 创建 YFinanceDataSource 类

**文件**：`strictmode/datasrc/yfinance.py`（新建）

**核心实现逻辑**：

```python
class YFinanceDataSource(AbstractDataSource):
    def __init__(self, session: yf.Ticker | None = None):
        # yfinance 不需要 session，但保留接口兼容性
        pass
    
    def get_adjusted_daily(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> AdjustedDailyBar:
        # 1. 调用 yfinance 获取数据
        # 2. 转换日期格式
        # 3. 处理复权逻辑
        # 4. 构建 AdjustedDailyBar
        pass
```

**关键实现细节**：

1. **数据获取**：
   ```python
   ticker = yf.Ticker(symbol)
   # 使用 auto_adjust=False 获取原始数据，自行计算复权
   hist = ticker.history(start=start_str, end=end_str, auto_adjust=False)
   ```

2. **列名标准化与必需列**：
   ```python
   rename_map = {
       "Open": "open",
       "High": "high",
       "Low": "low",
       "Close": "close",
       "Adj Close": "adj_close",
       "Volume": "volume",
   }
   hist = hist.rename(columns=rename_map)
   hist = hist[list(rename_map.values())]
   ```

3. **复权比例计算**：
   - 使用 `auto_adjust=False` 获取原始数据后，通过 `adj_close` 计算复权比例
   - 在计算 `ratio = adj_close / close` 前，将 `close == 0` 的行替换为缺失值，避免除零
   - 如果计算得到的复权比例全为缺失，直接抛出 `RuntimeError("No usable close prices returned from yfinance")`
   - 生成 `adj_open/high/low = open/high/low * ratio`，必要时对缺失值进行前向填充或丢弃

4. **日期索引转换**：
   ```python
   hist.index = pd.to_datetime(hist.index).normalize()
   hist.index.name = "date"
   hist.index = hist.index.date
   ```

5. **构建返回对象**：
   - 检查 DataFrame 是否为空，若无数据则抛出 `RuntimeError("No data returned for symbol")`
   - 确认所有必需列齐全后，创建 `AdjustedDailyBar`
   - 调用 `set_symbol()` 设置符号

#### 4.2.2 更新工厂方法

**文件**：`strictmode/cli.py`

**修改位置**：`DependencyContainer.data_source()` 方法

**原代码**：
```python
def data_source(self) -> AlphaVantageDataSource:
    return AlphaVantageDataSource(api_key=self.settings.data.api_key)
```

**新代码**：
```python
from .datasrc.base import AbstractDataSource


def data_source(self) -> AbstractDataSource:
    source = (self.settings.data.source or "yfinance").lower()
    if source == "yfinance":
        from .datasrc.yfinance import YFinanceDataSource

        return YFinanceDataSource()
    if source == "alphavantage":
        from .datasrc.av import AlphaVantageDataSource

        if not self.settings.data.api_key:
            raise RuntimeError("Alpha Vantage data source requires an API key")
        return AlphaVantageDataSource(api_key=self.settings.data.api_key)
    raise ValueError(f"Unknown data source: {source}")
```

#### 4.2.3 更新配置处理

**文件**：`strictmode/config.py`

**修改位置**：`DataSettings` 类

**变更**：
- `api_key` 字段改为可选（yfinance 不需要），Alpha Vantage 路径由工厂显式校验
- 默认值改为 `"yfinance"`，确保系统在无额外配置时即使用新数据源

**代码**：
```python
@dataclass
class DataSettings:
    api_key: str | None = None  # 仅 Alpha Vantage 需要，启用时需显式配置
    source: str = "yfinance"  # 默认启用 yfinance，可通过环境变量切换
```

#### 4.2.4 更新依赖配置

**文件**：`pyproject.toml`

**添加依赖**：
```toml
dependencies = [
    # ... 现有依赖 ...
    "yfinance>=0.2.0",
]
```

#### 4.2.5 更新测试

**文件**：`tests/test_datasource.py`

**新增测试**：
- 使用 `pytest` fixture 构建一个伪造的 yfinance 响应（DataFrame），覆盖 `close == 0`、`start/end` 过滤、空数据场景
- 验证 `YFinanceDataSource.get_adjusted_daily()` 会抛出 `RuntimeError` 当 DataFrame 为空或复权比例不可用
- 断言 `adj_open/adj_high/adj_low` 与 `adj_close / close` 的比率一致

**更新现有测试**：
- 使用参数化在 `MockAlphaVantage` 与 yfinance mock 之间复用断言
- 为 `DependencyContainer.data_source()` 添加集成测试，验证根据 `STRICTMODE_DATA_SOURCE` 选择不同实现

---

## 5. 详细实施步骤

### 5.1 阶段一：准备工作

1. **安装 yfinance 库**：
   ```bash
   pip install yfinance>=0.2.0
   ```

2. **验证 yfinance 可用性**：
   ```python
   import yfinance as yf
   ticker = yf.Ticker("AAPL")
   hist = ticker.history(period="1mo")
   print(hist.head())
   ```

3. **对比数据格式**：
   - 使用相同的 symbol 和日期范围
   - 对比 Alpha Vantage 和 yfinance 返回的数据
   - 验证复权价格的一致性

### 5.2 阶段二：实现 YFinanceDataSource

1. **创建 `strictmode/datasrc/yfinance.py`**
   - 实现 `YFinanceDataSource` 类
   - 实现 `get_adjusted_daily()` 方法
   - 确保返回格式与 `AlphaVantageDataSource` 一致

2. **处理边界情况**：
   - 若返回空 DataFrame 或复权比率全为缺失，抛出 `RuntimeError`，与 Alpha Vantage 行为一致
   - 对 `close == 0` 或缺失的行进行过滤/填补，保证最终 DataFrame 不含 `inf/NaN`
   - 日期范围超出可用数据时允许返回子集数据；若最终数据为空仍抛异常并记录警告
   - 捕获 yfinance 抛出的网络异常并转换为统一的运行时错误，便于日志监控

3. **性能优化**：
   - 考虑添加本地缓存（可选）
   - 批量下载多个标的（如需要）

### 5.3 阶段三：集成与测试

1. **更新工厂方法**：
   - 修改 `DependencyContainer.data_source()`
   - 添加数据源选择逻辑

2. **更新配置**：
   - 将 `DataSettings` 改为可选 `api_key`，默认直接启用 yfinance
   - 在文档中说明如需回退到 Alpha Vantage，应显式设置 `STRICTMODE_DATA_SOURCE=alphavantage` 并提供 API Key

3. **单元测试**：
   - 参数化复用 `MockAlphaVantage` 与 yfinance mock，覆盖 `adj_*` 字段比率校验
   - 针对空 DataFrame、`close == 0`、`start/end` 过滤分别断言抛出/返回结果
   - 验证 `AdjustedDailyBar` 保持日期索引与列名一致

4. **集成测试**：
   - 使用 `monkeypatch` 替换 `DependencyContainer.data_source`，复用 CLI 测试路径验证 `buy`、`sync-data`
   - 在每日任务测试中提供 yfinance mock DataFrame，确认止损计算与缓存逻辑正常

### 5.4 阶段四：部署与验证

1. **更新文档**：
   - 更新 `README.md` 中的数据源说明
   - 更新环境变量配置示例
   - 添加数据源切换指南

2. **环境变量配置**：
   ```bash
   STRICTMODE_DATA_SOURCE=yfinance
   # STRICTMODE_DATA_API_KEY 保留供回滚使用
   ```

3. **灰度验证**：
   - 先在测试环境验证
   - 对比新旧数据源的数据一致性
   - 验证所有 CLI 命令正常工作

4. **生产部署**：
   - 更新生产环境配置
   - 监控日志，确保无异常
   - 保留 Alpha Vantage 配置作为备份

---

## 6. 代码变更清单

### 6.1 新增文件

- `strictmode/datasrc/yfinance.py`：yfinance 数据源实现

### 6.2 修改文件

1. **`strictmode/cli.py`**：
   - 修改 `DependencyContainer.data_source()` 方法
   - 更新导入语句（添加 `AbstractDataSource`）

2. **`strictmode/config.py`**：
   - 将 `DataSettings.api_key` 调整为可选，并将默认数据源设置为 yfinance
   - 新增对 `STRICTMODE_DATA_SOURCE` 的说明文档，指引如何回退至 Alpha Vantage

3. **`pyproject.toml`**：
   - 添加 `yfinance>=0.2.0` 依赖

4. **`tests/test_datasource.py`**：
   - 添加 `YFinanceDataSource` 测试
   - 更新 Mock 类

5. **`README.md`**（如需要）：
   - 更新数据源配置说明

### 6.3 保留文件（不修改）

- `strictmode/datasrc/av.py`：保留 Alpha Vantage 实现，便于回滚
- `strictmode/datasrc/base.py`：接口定义保持不变

---

## 7. 风险与应对措施

### 7.1 数据一致性风险

**风险**：yfinance 和 Alpha Vantage 的数据可能存在细微差异（如复权计算方式、数据更新时间等）

**应对措施**：
- 在切换前进行充分的数据对比测试
- 保留 Alpha Vantage 配置，支持快速回滚
- 添加数据验证逻辑，检查异常值

### 7.2 网络可用性风险

**风险**：Yahoo Finance 可能不稳定或变更 API

**应对措施**：
- 添加重试机制（yfinance 内部已实现）
- 监控数据获取失败率
- 准备备用数据源（如保留 Alpha Vantage 作为备选）

### 7.3 数据延迟风险

**风险**：yfinance 的数据延迟可能影响实时交易决策

**应对措施**：
- 本系统主要使用 EOD（日线）数据，延迟影响较小
- 每日任务在收盘后执行，数据延迟不影响决策
- 如需实时数据，可考虑集成其他数据源

### 7.4 API 变更风险

**风险**：yfinance 库或 Yahoo Finance API 可能变更

**应对措施**：
- 锁定 yfinance 版本（如 `yfinance>=0.2.0,<1.0.0`）
- 定期更新库版本，关注变更日志
- 添加健康检查，监控数据获取成功率

### 7.5 向后兼容性风险

**风险**：修改可能影响现有功能

**应对措施**：
- 保持接口不变（`AbstractDataSource`）
- 充分测试所有 CLI 命令
- 保留 Alpha Vantage 代码，支持配置切换

---

## 8. 回滚方案

### 8.1 快速回滚步骤

1. **修改环境变量**：
   ```bash
   STRICTMODE_DATA_SOURCE=alphavantage
   STRICTMODE_DATA_API_KEY=<your_api_key>
   ```

2. **重启服务**：
   ```bash
   # 如果是服务模式
   systemctl restart strictmode-service
   ```

3. **验证回滚**：
   ```bash
   strictmode sync-data AAPL --days 7
   ```

### 8.2 回滚检查清单

- [ ] 环境变量已更新
- [ ] 服务已重启
- [ ] 数据获取正常
- [ ] CLI 命令工作正常
- [ ] 每日任务正常运行

---

## 9. 验收标准

### 9.1 功能验收

- [ ] `strictmode buy` 命令能正常获取数据并下单
- [ ] `strictmode sync-data` 能正常同步数据
- [ ] `strictmode show-data` 能正常显示缓存数据
- [ ] 每日任务能正常获取最新数据并更新止损
- [ ] 复权价格计算正确
- [ ] ATR 和吊灯止损计算正确

### 9.2 数据质量验收

- [ ] 数据格式与 Alpha Vantage 一致
- [ ] 复权价格计算准确
- [ ] 日期范围过滤正确
- [ ] 数据完整性良好（无缺失值）

### 9.3 性能验收

- [ ] 数据获取速度可接受（< 5秒）
- [ ] 内存使用正常
- [ ] 无明显的性能退化

### 9.4 稳定性验收

- [ ] 连续运行 7 天无异常
- [ ] 错误处理正确
- [ ] 日志记录完整

---

## 10. 后续优化建议

### 10.1 短期优化

1. **添加数据缓存**：
   - 在本地缓存已获取的数据，减少 API 调用
   - 实现基于文件或数据库的缓存机制

2. **批量下载优化**：
   - 如果同时需要多个标的的数据，使用 yfinance 的批量下载功能
   - 实现并行下载，提高效率

3. **错误处理增强**：
   - 添加更详细的错误日志
   - 实现自动重试机制（yfinance 已有，但可增强）

### 10.2 长期优化

1. **多数据源支持**：
   - 实现数据源优先级机制
   - 支持主备数据源自动切换

2. **数据验证**：
   - 实现数据质量检查
   - 对比多个数据源，识别异常数据

3. **性能监控**：
   - 添加数据获取耗时监控
   - 添加数据源可用性监控

---

## 11. 附录

### 11.1 yfinance 使用示例

```python
import yfinance as yf
from datetime import date, timedelta

# 方式一：使用 Ticker 对象
ticker = yf.Ticker("AAPL")
hist = ticker.history(start="2023-01-01", end="2023-12-31", auto_adjust=False)

# 方式二：使用 download 函数
data = yf.download("AAPL", start="2023-01-01", end="2023-12-31", auto_adjust=False)

# 处理数据
hist.index = pd.to_datetime(hist.index).normalize()
rename_map = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}
hist = hist.rename(columns=rename_map)
hist = hist[list(rename_map.values())]
close = hist["close"].replace(0, pd.NA)
ratio = hist["adj_close"] / close
hist["adj_open"] = hist["open"] * ratio
hist["adj_high"] = hist["high"] * ratio
hist["adj_low"] = hist["low"] * ratio
hist = hist.dropna(subset=["adj_close"])
hist.index = hist.index.date
```

### 11.2 环境变量配置示例

```bash
# .env 文件

# 数据源配置（切换为 yfinance）
STRICTMODE_DATA_SOURCE=yfinance

# Alpha Vantage 配置（可选，用于回滚）
# STRICTMODE_DATA_API_KEY=your_api_key_here
```

### 11.3 相关文档链接

- yfinance GitHub: https://github.com/ranaroussi/yfinance
- yfinance 文档: https://pypi.org/project/yfinance/
- Yahoo Finance: https://finance.yahoo.com/

---

**文档版本**：v1.0  
**创建日期**：2024-12-19  
**最后更新**：2025-11-01

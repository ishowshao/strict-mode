# 多次加仓与止损管理

负责人：StrictMode CLI/Engine
最近更新：2025-11-02

## 摘要

同一标的允许多次 BUY。每一笔 BUY 都配置一张“硬兜底”子止损（百分比法），同时维护一个“全局 Chandelier 止损”作为只上不下的棘轮。每日将所有子止损上调到不低于 Chandelier 水平，但绝不下调。

对任意一张子止损 j，在时间 t 的更新规则：

    new_stop_j = max(current_stop_j, chandelier_stop_t)

该规则确保：每笔买入都保留自己的下行兜底；当趋势上涨后，回撤由全局 Chandelier 统一触发。

## 目标
- 允许同一标的多次 BUY，不再阻塞第二次买入。
- 每次 BUY 下达“父 BUY + 子 SELL STP”的括号单；子止损初始价按“百分比兜底”计算（来自该笔的成交价或限价）。
- 计算全局 Chandelier 值，仅向上抬升所有子止损到该水平，不下调。
- 在 TWS 中保持清晰：每笔 BUY 对应一个子止损；不同笔之间不做 OCA。
- 通过合约资格化与最小跳动对齐，避免交易所拒单或 PendingSubmit。

## 非目标
- 本迭代不加入止盈 LMT。
- 不改变现有 `daily_task` 的通知/自动清仓策略之外的行为。
- 暂不把“每笔子止损”的元数据落库；DB 仍仅保存“按标的的分析用 Stop 记录（Chandelier）”。

## 背景

当前实现当标的已有持仓时阻止再次 BUY；DB 只有一条 Stop 记录用于计算并更新一张 STOP 订单。实际加仓场景需要：每笔独立兜底（百分比）+ 全局趋势回撤（Chandelier）。

已在运行中验证的要点：
- 先对合约 `qualifyContracts`，可避免 `PendingSubmit` 与歧义合约。
- 所有价格按最小跳动对齐，避免 IB 错误 110（不符合最小报价单位）。
- 用父/子（括号）关联让 STOP 在 TWS 作为子项可见，并在父单成交后激活。

## 拟议行为

### 每次 BUY（逐笔）
- 下达父 BUY（MKT 或 LMT）+ 子 STOP（SELL STP，`parentId` 指向父单）。
- 子 STOP 初始价：`fill_or_limit_price * (1 - initial_stop_pct)`。
- 下单前对所有价格做最小跳动对齐；两条腿复用同一个已资格化的 `Contract`。

### 每日棘轮（全局 Chandelier）
- 继续按现有方法计算当日 Chandelier。
- 找到该标的在 IB 的所有（属于 StrictMode 的）子 STOP，将其价位更新为 `max(当前价, Chandelier)`；从不下调。
- 更新前同样做最小跳动对齐。
- DB 的 `Stop` 记录更新为当日 Chandelier 值（分析用地板），IB 订单 ID 继续由 IB 管理。

### 持仓核算
- DB 仍保持一条 Position（qty 为合计，avg_price 为加权平均）。每次 BUY 更新该记录；逐笔子止损直到被 Chandelier 超越前，保持各自独立的兜底。

## 数据/模型
- 保留现有 `Stop` 表，代表“标的级的分析用地板（Chandelier）”，不对应单个 IB 订单。
- 本迭代不新增表（未来可选：增加 `stop_orders` 表记录每笔 STOP 的 orderId）。

## IBKR/TWS 集成细节
- 使用 `ib.qualifyContracts(Stock(symbol, 'SMART', currency))`，并在父/子两腿复用该 `Contract`。
- 括号提交与可见性：父 BUY `transmit=False`，子 STOP `parentId=<父单ID>` 且 `transmit=True`（由子腿传输整条链）。
- 价格对齐（避免 110 错误）：优先 `marketRuleIds + reqMarketRule`，否则 `minTick`，最终兜底 0.01（美股）。
- RTH 提示：2109 为信息级，表示某些路由/类型下 RTH 参数被忽略，不影响提交。

## CLI 变更

### buy
- 放开“已有持仓禁止再买”的限制，允许多次 BUY。
- 现有参数保持；仍记录两条订单到 journal；Position 按新加权平均与新总量更新。
- 对 IB 订单设置 `orderRef` 前缀，如 `SM:{symbol}`，便于后续筛选。

### show-orders（诊断）
- 已提供：打印当前 open orders 的 id/parentId/type/status/lmt/aux。

### reconcile-stops（新增）
- 对比 `SM:{symbol}` 的所有 STOP 数量与当前持仓数量；若合计 > 持仓，提示或执行缩减/取消，避免潜在超卖。
- 支持 dry‑run 预览；`--apply` 确认执行。

## 引擎变更

### broker_ib
- `OrderRequest` 保留 `parent_id`、`transmit`；新增 `order_ref` 并透传到 `ib_insync.Order(orderRef=...)`。
- 父/子两腿统一资格化与最小跳动对齐。
- 扩展 `find_stop_orders(symbol, order_ref_prefix='SM:')`，在按 symbol 与 orderType 过滤的基础上，再按 `orderRef` 前缀过滤。
- `modify_order` 继续做价格对齐，保证更新合法。

### daily_task
- 对单个 symbol：
  1) 计算当日 `chandelier_stop`；
  2) 获取 `orderRef` 以 `SM:` 开头的所有子 STOP；
  3) 对每张 STOP_j：`new = max(current, chandelier)`，若上调则提交修改；
  4) 将 DB 的 `Stop` 记录更新为 `chandelier_stop`（沿用 method、atr 参数等）。

## 算法说明

逐笔兜底：

    floor_j = fill_or_limit_price_j * (1 - initial_stop_pct)

每日棘轮上调：

    new_stop_j = max(current_stop_j, chandelier_stop_t)

可选：若希望“分批退出”更平滑，上调到 Chandelier 时可对不同 STOP 引入 ±1 个 tick 的微小差异。默认完全对齐。

### 数值示例

- 第 1 笔：100 买，兜底 5% → `STOP1_floor = 95.00`。
- 第 2 笔：110 买，兜底 5% → `STOP2_floor = 104.50`。

当日 Chandelier = 98：
- `STOP1 = max(95.00, 98.00) = 98.00`
- `STOP2 = max(104.50, 98.00) = 104.50`

含义：两张 STOP 不同价位，第二笔更靠近现价，先触发以更早降低新增仓位风险。

随后 Chandelier = 107：
- `STOP1 = max(98.00, 107.00) = 107.00`
- `STOP2 = max(104.50, 107.00) = 107.00`

含义：两张 STOP 被统一抬到 107；若发生回撤，将一次性平掉全部仓位（符合“全局回撤退出”的思路）。

实现备注：所有价格在提交给 IB 前都会按最小跳动对齐（如股票 0.01），例如 `104.5` 会以 `104.50` 发送，避免错误 110。

## 边界情况
- 休市 MKT 且仅 RTH：可能维持 `PreSubmitted`；可用 `--no-rth` 或改 LMT。2109 为信息级。
- 在 TWS 手动减仓：可能导致 STOP 合计数量 > 当前持仓；用 `reconcile-stops` 整顿。
- 市场规则不可用时的跳动：兜底 0.01，适用于美股大多数标的。

## 测试计划
- 单元
  - 价格对齐：给定任意价验证按市场增量对齐。
  - 上调规则：多张 STOP 的 `max(current, chandelier)` 行为。
- 集成（pytest CLI）
  - 同一标的多次 BUY（dry‑run broker）：应记录两次 STOP 意向。
  - `daily_task` 配合桩 broker 返回 N 张 STOP：逐张上调到期望值。
  - `reconcile-stops` 的 dry‑run：能识别盈余并产出计划。
  - 真实路径：开启对齐后，不出现 110 错误。

## 可观测性
- 继续记录 BUY/STOP 的 journal。
- `daily_task` 上调时，增加简洁日志（每标的：张数、最小/最大上调幅度）。
- 诊断工具：`--ib-debug`、`show-orders` 保持。

## 附录：IB 注意事项
- 父/子关联：用 `parentId`，子单 `transmit=True` 推动链路提交。
- 合约资格化：下单前 `ib.qualifyContracts()`，避免 `PendingSubmit`。
- 最小跳动：优先 `marketRuleIds` + `reqMarketRule`；否则 `minTick`；兜底 0.01。
- 常见错误码：
  - 110：价格不符合最小跳动 → 通过对齐解决；
  - 2109：RTH 属性被路由忽略 → 信息级，可忽略。

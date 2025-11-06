# StrictMode 迁移至 IBKR Web API 技术方案

本文档面向工程与运维，给出从 TWS/IB Gateway Socket API（ib_insync）迁移到 IBKR Web API（HTTP + WebSocket，经本地 Gateway 转发）的落地方案、改造清单与风险控制。内容结合 StrictMode 当前工程结构与“日驱动”批处理形态（每日挂/上提止损、订单查询/撤单/对账）。

最后更新：2025-11-06（美国东部时区）。

---

## 关键结论（给决策者）

- 可行性：IBKR Web API 在功能上覆盖现有需求（下单、括号/父子单、修改止损、订单/持仓/合约、必要行情），可运行于无头 Linux 服务器（本地 Java Gateway + 应用通过 HTTP/WS 调用）。
- 会话管理：零交互“永不掉线”不现实；采用“任务前自愈 + 心跳保活 + 失败重建”可将人工干预降到低频。
- 单会话互斥：同一用户名仅能有一个经纪（可交易）会话。若手机 App/TWS 进入可交易在线，会把服务器会话顶掉；任务开始前需自动“夺回”。
- 纸面/实盘：通过独立凭证或登录后基于账户列表选择（实盘 U**** / 纸面 DU****）。流程清晰可控。
- 迁移成本：核心改造集中在“Socket 调用 → /iserver/ 路由的 HTTP/WS 调用”，并引入“状态探测 + 再认证（reauth）”中间件；策略与业务层可复用。

参考依据：

- Web API 认证与会话（含只读/经纪双阶段、tickle 心跳、互斥会话、午夜重置等）与端点：官方 Web API 文档与参考。见“附录 · 参考资料”。

---

## 目标与范围

- 目标：用 IBKR Web API 替换现有 ib_insync（Socket API）在 StrictMode 中的经纪交互，实现同等或更优的稳定性与可观测性，并保持业务语义不变。
- 范围：
  - 下单：市场/限价/止损、括号单/父子单（cOID/parentId）、订单修改（replace）与撤单（cancel）。
  - 账户：账户列表、持仓、现金、开仓/委托查询。
  - 合约与最小跳动：基于 `conId + exchange`，下单前价格对齐与规则校验。
  - 行情：仅覆盖轻量实时/快照（用于下单校验/探针）；日线行情仍由 yfinance 提供，避免迁移复杂度。
  - 会话管理：只读会话校验、经纪会话拉起、心跳保活、自愈重连、与手机/TWS 的互斥处理。

不在范围：

- 期权策略路由、复杂条件单、算法单、深度行情等高级特性（后续阶段评估）。

---

## 总体架构

```
             +---------------------------+          +-----------------------+
             |  StrictMode (Ubuntu)      |          |   IBKR Backend        |
             |  - CLI/Service (Typer)    |  HTTPS   |   api.ibkr.com        |
             |  - IBKR WebAPI Client     +--------->|   (官方服务)          |
             |  - Session Manager        |          +-----------------------+
             |                           |
             |  localhost:5000/v1/api    |          +-----------------------+
             |  (Client Portal Gateway)  | <--------+  IBKR Mobile/TWS/TWA  |
             +---------------------------+    互斥      (可能占用会话)       |
```

- 应用只与本机 Gateway 通信（默认 `https://127.0.0.1:5000/v1/api`）。Gateway 首次登录需要一次浏览器 + 2FA（可通过 SSH 端口转发完成）。
- 会话模型（二段式）：
  - 登录成功获得“只读”会话（可查但不可下单）。
  - 需调用“brokerage session init”端点拉起“可交易”会话；与手机/TWS 互斥。
- 保活：定期 HTTP `tickle` 与/或 WebSocket 心跳；按探针状态自动 reauth。

---

## 端点与对象模型（核心映射）

以下端点名称、方法与关键字段基于官方 Web API 文档。请以“附录 · 参考资料”为准，关注后续变更（官方有端点整合与命名演进）。

### 1) 认证与会话（只读/经纪）

- 只读会话有效性：`GET /sso/validate`（有效返回 `1`，失效返回 `0`）。
- 登录状态（只读）：`POST /iserver/auth/status`（返回 `COMPLETE/FAILED` 等）。
- 拉起经纪会话（可交易）：`POST /iserver/auth/ssodh/init`
  - 常用参数：
    - `publish: true`（允许 Gateway 推送事件）
    - `compete: true`（必要时踢出其他活跃会话，夺回交易会话）
- 心跳：
  - HTTP：`GET /tickle`（建议 ≤ 1 次/秒；实践 30–60 秒一次）。
  - WebSocket：发送 `tic` 帧维持活性。
- 会话时效：只读会话在长时间无交互会过期；认证在“午夜”发生重置（需在每日任务前确保有效）。

### 2) 账户、持仓与订单

- 账户列表：`GET /iserver/accounts`（含实盘 `U****` 与纸面 `DU****`）。
- 切换交易账户（部分端点需要显式账户上下文）：`POST /iserver/account/{accountId}/switch`。
- 持仓：`GET /iserver/portfolio/{accountId}/positions`。
- 订单列表（含开放订单）：`GET /iserver/account/orders`。
- 下单：`POST /iserver/account/{accountId}/orders`
  - 重要字段（示例）：
    - `conid`、`side`（`BUY/SELL`）、`orderType`（`MKT/LMT/STP/STOP_LIMIT` 等）、
      `price`（限价或止损价，视 `orderType`）、`tif`、`outsideRth`、
      `cOID`（客户端订单 ID，用于父子/括号单链路）、`parentId`（子单指向父单）。
  - 括号/父子单：先发父单（可 `transmit=false` 预挂），再发子单（`parentId=父单ID`，`transmit=true`）。
- 改单（Replace）：`POST /iserver/account/{accountId}/order/{ibOrderId}`（修改价格/数量等）。
- 撤单（Cancel）：`DELETE /iserver/account/{accountId}/order/{ibOrderId}`。
- 合规字段（期货）：对“美国期货”订单，需包含 `manualIndicator`（2025-05-01 生效，取消/改单同样要求）。

### 3) 合约与规则（最小跳动/路由）

- 合约搜索：`GET /iserver/secdef/search?symbol=...`（拿 `conId`）。
- 合约详情与规则：`GET /iserver/contract/{conid}/info-and-rules`
  - 返回最小跳动、有效价格区间、交易时段、默认路由等。
- 行情快照（轻量探针）：`GET /iserver/marketdata/snapshot`（校验合约可订阅/价格可用）。

### 4) WebSocket（可选增强：订单状态/行情流）

- 连接：`wss://127.0.0.1:5000/v1/api/ws`（复用 HTTP Cookie）。
- 主题：
  - `sor`（订单/执行状态流）
  - `smd`（市场数据流）
  - `str`（账户相关流，如净值/PNL 等）
- 心跳：发送 `tic`；断线自动重连并做状态比对。

> 注：上列端点与主题名称以官方 Reference 为准；部分文档仍包含旧称，迁移时以最新版为权威。

---

## 与 StrictMode 代码库的映射与改造点

### 新增模块与类

- `strictmode/engine/broker_ib_webapi.py`
  - `class WebAPISessionManager`：
    - `validate()`: `GET /sso/validate` 与 `POST /iserver/auth/status`，只读会话有效性；失败时抛出需要人工登录的异常（并触发告警）。
    - `ensure_brokerage(compete: bool = True)`: `POST /iserver/auth/ssodh/init`；必要时 `compete=true` 抢占会话；随后以 `GET /iserver/accounts`、`GET /iserver/account/orders` 做轻量探针验证。
    - `heartbeat()`: 定时 `GET /tickle`；若失败或返回失效，则走 `validate() → ensure_brokerage()`。
  - `class IBKRWebAPIBroker`（对齐现有 `engine/broker_ib.py` 的方法签名）：
    - `place_order(request: OrderRequest) -> OrderResponse`
    - `place_bracket(parent: OrderRequest, stop: OrderRequest) -> tuple[OrderResponse, OrderResponse]`
    - `cancel_order(order_id: int) -> None`
    - `replace_order(order_id: int, **fields) -> OrderResponse`
    - `find_stop_orders(symbol: str) -> list[tuple[int, float]]`（返回 stop 的 `order_id` 与 stop 价）
    - 内部统一：
      - 合约解析：`secdef/search → info-and-rules`，以 `conId` 为主键；缓存 24h。
      - 价格对齐：根据 `info-and-rules` 中的最小跳动做 rounding（沿用现有 `_round_to_increment` 逻辑）。
      - 账户上下文：根据配置或运行时参数选择 `accountId`（优先 `DU****`/`U****`）。
      - 合规：美国期货下单/改单/撤单添加 `manualIndicator`。

### 现有文件的最小改动

- `strictmode/config.py`
  - 新增配置段 `WebAPISettings`：
    - `base_url`（默认 `https://127.0.0.1:5000/v1/api`）
    - `verify_tls`（默认 `False`，因本地 Gateway 为自签证书；生产可改为指定 CA/证书路径）
    - `heartbeat_sec`（默认 `45`）
    - `account_hint`（可选：`DU`/`U` 前缀偏好或固定账户 ID）
    - `mode`：`socket`（默认）或 `webapi` 用于切换经纪实现
  - 环境变量（均带 `STRICTMODE_` 前缀）：`IB_MODE`、`IB_WEBAPI_BASE_URL`、`IB_WEBAPI_VERIFY_TLS`、`IB_WEBAPI_HEARTBEAT_SEC`、`IB_WEBAPI_ACCOUNT_HINT`。

- `strictmode/cli.py`
  - `DependencyContainer.broker(...)` 中根据 `settings.ib.mode` 返回 `IBKRWebAPIBroker` 或 `IBBroker`。
  - 新增维护类命令（便于运维与测试）：
    - `strictmode ibkr session status`（打印 validate/status/brokerage 状态）
    - `strictmode ibkr session ensure --compete/--no-compete`（拉起/夺回交易会话）
    - `strictmode ibkr accounts`（列出账户并显示当前选择）

- `pyproject.toml`
  - 运行时已具备 `httpx`。若启用 WS 可流式监听，建议新增：`websocket-client` 或 `websockets`（二选一）。

### 测试

- 在 `tests/test_cli.py` 增加 WebAPI 模式的 smoke 测试（加 `-k webapi` 选择性运行）：
  - `session ensure` 命令在无 Gateway 时不失败，但提示需要首次登录。
  - `buy --dry-run` 在 WebAPI 模式绕过真实下单，仅校验价格对齐与请求体构造。
- 针对 `engine/broker_ib_webapi.py`：
  - 使用 `respx`（httpx mock）覆盖：`/sso/validate`、`/iserver/auth/status`、`/iserver/auth/ssodh/init`、`/iserver/accounts`、`/iserver/secdef/search`、`/iserver/contract/*/info-and-rules`、`/iserver/account/*/orders` 等。
  - 回归测试保证与 `broker_ib.py` 的行为等价（括号单/止损改价/撤单）。

---

## 部署与运维

### 服务器安装 Gateway（无头 Linux）

1) 安装 OpenJDK 17+：`apt-get install -y openjdk-17-jre`

2) 部署 IBKR Web API Gateway：

- 方案 A（推荐）Docker：拉取官方镜像，映射 5000 端口到回环或仅本机网络命名空间。
- 方案 B（本地进程）：解压官方发布包（通常名为 `clientportal.gw`），以 `bin/run.sh` 启动，默认监听 `127.0.0.1:5000`。

3) 首次登录（必需一次交互）：

- 在服务器上启动 Gateway 后，SSH 端口转发到本机浏览器完成用户名/密码 + 2FA：

  ```bash
  # 本地终端：
  ssh -L 5000:127.0.0.1:5000 user@server
  # 浏览器打开 https://127.0.0.1:5000/ 依指引完成登录与 2FA
  ```

4) systemd 常驻（本地进程示例）：

```ini
[Unit]
Description=IBKR Web API Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ibkr
WorkingDirectory=/opt/ibkr/clientportal.gw
ExecStart=/opt/ibkr/clientportal.gw/bin/run.sh root/conf.yaml
Restart=always
RestartSec=5
Environment=JAVA_TOOL_OPTIONS=-Xms256m -Xmx512m

[Install]
WantedBy=multi-user.target
```

安全建议：

- 仅监听回环（默认）；若需容器网络，务必通过防火墙/安全组限制访问；HTTPS 为自签证书，客户端可设置 `verify=false` 或配置受信 CA。
- 监控 Gateway 进程与 5000 端口；如异常退出，systemd 自动拉起。

### StrictMode 配置

`.env` 示例：

```
# 选择 Web API 模式
STRICTMODE_IB_MODE=webapi

# Gateway 基址
STRICTMODE_IB_WEBAPI_BASE_URL=https://127.0.0.1:5000/v1/api
STRICTMODE_IB_WEBAPI_VERIFY_TLS=false
STRICTMODE_IB_WEBAPI_HEARTBEAT_SEC=45

# 账户选择偏好（可选）
STRICTMODE_IB_WEBAPI_ACCOUNT_HINT=DU  # 优先纸面；或 U
```

---

## 会话管理与“前置自愈”流程

### 原语定义

- `validate()`：
  - `GET /sso/validate` 返回 1 → 只读会话有效。
  - 失败或返回 0 → 标记需要人工重新登录（触发告警）；若仍可调用 `POST /iserver/auth/status` 获取状态信息。

- `ensure_brokerage(compete=true)`：
  - 若“经纪会话未就绪”，调用 `POST /iserver/auth/ssodh/init { publish:true, compete:true }`。
  - 成功后以 `GET /iserver/accounts` 和一次轻量查询（如 `GET /iserver/account/orders` 或快照行情）作为探针确认就绪。

- `heartbeat()`：
  - 每 30–60s `GET /tickle`；若异常 → 执行 `validate() → ensure_brokerage()`。

### 每日任务入口（批处理）

1. `validate()`：仅只读失效时才需人工干预（SSH + 浏览器完成登录）。
2. `ensure_brokerage(compete=true)`：在任务开始前主动夺回交易会话，避免被手机/TWS 顶掉。
3. 进入业务：下单/止损上提/对账等。

### 与手机 App/TWS 的互斥策略

- 生产服务器长期占用“经纪会话”。
- 团队规范：移动端/桌面端仅做查询或短时操作，用毕及时退出“可交易”态。
- 任务前 `compete=true` 重新初始化，自动夺回会话，降低人工协调成本。

---

## 交易与合约细节

### 合约与价格对齐

- 按 `symbol → secdef/search → 选 conId + exchange`；**不**仅凭 symbol，避免跳动单位与路由歧义。
- 下单前调用 `info-and-rules` 取最小跳动、交易时段、涨跌幅/价格带等；
  - 价格按最小跳动做 rounding（买单向下/向最近、止损按最近），避免因无效价格被拒（等价于当前 `broker_ib.py._round_to_increment`）。

### 括号/父子单与止损修改

- 首发父单（可 `transmit=false`），取得 `ibOrderId` 后，提交子单（`parentId=父单ID`，`transmit=true`）。
- 止损修改：对止损子单调用 `POST /iserver/account/{accountId}/order/{orderId}` 替换价格。
- 统一使用 `cOID` 便于幂等与链路追踪（父/子共享前缀，或父为 `{prefix}:P`，子为 `{prefix}:SL`）。

### 订单/持仓/对账

- 开放订单：`GET /iserver/account/orders`（含工作中与部成）。
- 持仓：`GET /iserver/portfolio/{accountId}/positions`。
- 对账可在每日结束时拉取当日执行回报与成交，或通过 WS `sor` 流增量消费（后续阶段）。

### 行情（最小化接入）

- 仍以 yfinance 提供日线数据（EOD），降低 Web API 行情限流/订阅复杂度。
- 仅在下单前进行轻量快照/探针，验证合约可用与价格带。

---

## 观测性与告警

- 运行指标：
  - Gateway 进程/端口存活；HTTP `health`/`tickle` 成功率；`validate/status` 状态；`ensure_brokerage` 成功率与重试次数。
  - 每日任务入口的自愈耗时；被顶掉→重新夺回的次数；人工登录间隔。
- 日志分层：
  - `INFO`：会话状态切换、账户选择、订单提交/替换/撤单。
  - `WARN`：只读会话失效、经纪会话丢失后自动恢复。
  - `ERROR`：需要人工重新登录（附触发时间与操作手册链接）。
- 告警：
  - Telegram：只读会话失效/人工登录请求、`ensure_brokerage` 连续失败、订单提交 4xx/5xx。

---

## 安全与合规

- Gateway 仅对回环开放；若容器部署，限制到主机网络且由防火墙保护。
- 本地自签证书：客户端可配置 `verify=false`，但生产建议引入内部 CA 或证书绑定。
- 凭证安全：严禁在代码/配置中保存 IBKR 用户密码与 2FA 秘密。首次登录仅通过人工交互完成。
- 合规字段：美国期货订单自 2025-05-01 起下单/改单/撤单均需 `manualIndicator` 字段。

---

## 迁移计划（两周节奏，含回退预案）

### 第 1 周：接入与联调

1. 模块骨架：新增 `engine/broker_ib_webapi.py` 与 `WebAPISessionManager`，提供 `validate/ensure_brokerage/heartbeat` 原语（HTTP mock 覆盖）。
2. 合约解析与缓存：`secdef/search → info-and-rules`；落地最小跳动 rounding。
3. 下单/括号单：以 `cOID/parentId` 建链，`replace/cancel` 路径贯通。
4. CLI 运维命令：`ibkr session status/ensure`、`ibkr accounts`。
5. 无头首次登录演练：SSH 端口转发 + 2FA；记录 SOP。

验收：`--dry-run` 路径全绿；在 paper 环境成功下单 + 修改止损 + 撤单。

### 第 2 周：稳定性与切流

1. 心跳与自愈：按 45s 定时 `tickle`，异常转 `validate → ensure_brokerage`；增加重试/退避与度量。
2. 只读会话过期与午夜重置演练：凌晨前后跑一轮日任务，验证自动恢复。
3. 切流：在 paper 环境跑完整一日批处理；比对订单与本地账目；确认告警阈值。
4. 实盘灰度：单账户短窗口灰度（避开交易繁忙时段），确认“手机/TWS 顶掉”时的自动夺回能力。

回退：保留 `STRICTMODE_IB_MODE=socket`；遇严重回归可秒级回退至 ib_insync。

---

## 风险与缓解

- 端点演进：Web API 文档与端点有合并与更名历史。缓解：封装最小 API 层，定期跟进官方 Changelog 与 Reference；集成测试覆盖关键路径。
- 网络与 2FA 风控：偶发需要人工重新登录。缓解：完善告警与运行手册；设置计划性低频人工登录窗口。
- 多端抢占：手机/TWS 进入可交易态会挤掉服务器会话。缓解：任务前 `compete=true` 夺回，团队规范限制个人端在线时长。
- 自签证书与 TLS 校验：`verify=false` 易被误用。缓解：在生产使用受信 CA 或明确仅绑定到回环访问。

---

## 附录 · 端点速查（常用）

- 认证/会话：
  - `GET /sso/validate`（只读会话有效性）
  - `POST /iserver/auth/status`（登录状态）
  - `POST /iserver/auth/ssodh/init`（经纪会话；`publish/compete`）
  - `GET /tickle`（心跳）
- 账户/订单/持仓：
  - `GET /iserver/accounts`
  - `POST /iserver/account/{accountId}/switch`
  - `GET /iserver/account/orders`
  - `POST /iserver/account/{accountId}/orders`
  - `POST /iserver/account/{accountId}/order/{orderId}`（改单）
  - `DELETE /iserver/account/{accountId}/order/{orderId}`（撤单）
- 合约与行情：
  - `GET /iserver/secdef/search?symbol=...`
  - `GET /iserver/contract/{conid}/info-and-rules`
  - `GET /iserver/marketdata/snapshot`
- WebSocket：`wss://127.0.0.1:5000/v1/api/ws`；主题 `sor`（订单）、`smd`（行情）、`str`（账户），心跳 `tic`。

---

## 附录 · 参考资料（官方）

- IBKR Web API – 总览、认证、只读/经纪会话、心跳与互斥说明（含 `ssodh/init`、`sso/validate`、`tickle`、“午夜重置”）：
  - IBKRCampus: Web API v1.0 Documentation（Authentication & Session Management / Order Management / Contracts / Reference）
- IBKR Web API 参考（端点说明与示例）：
  - IBKRCampus: Web API Reference
- WebSocket（主题 `sor/smd/str`、心跳 `tic`、Cookie 复用）：
  - IBKRCampus: Websocket | IBKR API
- 变更日志（`manualIndicator` 要求等）：
  - IBKRCampus: Web API Changelog — IBKR Guides

以上资料需随官方更新而校准细节与命名；生产以官方最新版为准。


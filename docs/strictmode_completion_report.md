# StrictMode 项目完成情况检查报告

- **总体结论**：需求文档定义的 CLI 与每日调度双能力尚未齐备，目前仅能通过 CLI 手工下单与记录，距离 Definition of Done 所述的每日上提止损与通知流程仍有显著差距（docs/requirements.md:3; docs/requirements.md:200; docs/requirements.md:201）。
- **CLI 与基础数据**：`buy`/`sell-all` 命令已实现数据拉取、吊灯止损计算、下单请求、SQLite 记账与 Telegram 通知，基本覆盖建仓流程；复权口径按比例生成，符合强制数据要求（docs/requirements.md:54; strictmode/cli.py:70; strictmode/rules/chandelier.py:49; strictmode/engine/journal.py:122; strictmode/engine/notifier.py:18; strictmode/datasrc/av.py:42）。
- **交易执行缺口**：清仓命令仅删除本地止损记录，未通过 IBBroker 撤销券商侧止损单，违背“先取消止损单”的约束；同时 `outside_rth` 逻辑取反，导致默认 `RTH=true` 时订单仍允许盘外成交，存在实际执行风险（docs/requirements.md:85; strictmode/cli.py:219; strictmode/engine/broker_ib.py:68）。
- **服务与风控**：调度层仅保留 APScheduler 外壳，缺少每日 EOD 拉取、止损上提、触发通知/自动清仓与数据新鲜度防线等关键流程，无法满足服务端能力与风控要求（docs/requirements.md:8; docs/requirements.md:97; docs/requirements.md:108; strictmode/engine/scheduler.py:10）。
- **数据模型与配置**：SQLite 仅建 `positions`/`stops`/`orders`/`audit_log` 表，缺失 `symbols` 与 `price_cache` 等结构；配置系统仍是 dataclass + 环境变量解析，未采用 `pydantic-settings`，仓库也未提供需求中的 Dockerfile（docs/requirements.md:13; docs/requirements.md:115; docs/requirements.md:151; strictmode/engine/journal.py:56; strictmode/config.py:9）。
- **测试与验收**：现有测试涵盖 ATR 计算、复权比例与 CLI 幂等，但缺少服务迭代、触发清仓、数据滞后等关键场景，无法支撑 DoD 要求的全链路回归（docs/requirements.md:184; docs/requirements.md:200; tests/test_chandelier.py:5; tests/test_datasource.py:1; tests/test_cli.py:68）。

## 后续建议

1. **补齐每日调度流程**：落地数据源缓存、止损上提、触发/自动清仓及数据新鲜度校验，提供可运行的 scheduler 入口与 Docker 化部署脚本。
2. **修复 CLI 执行缺口**：完善券商撤单/改单接口、修正 RTH 参数传递，并串联 `auto_liquidate` 配置支撑触发后的执行分支。
3. **扩展基础设施**：补齐 SQLite 表结构与迁移策略，引入 `pydantic-settings` 管理配置，补充服务迭代与异常场景的测试用例，以满足验收标准。

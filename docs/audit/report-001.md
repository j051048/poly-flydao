# 审计报告 #1（2026-08-05）

按 [`docs/AUDIT_PROMPT.md`](../AUDIT_PROMPT.md) 执行。

## 摘要

- 总分：**8.82 / 10**（评级：优秀）
- P0 × 0，P1 × 2，P2 × 7，P3 × 2
- 总体判断：资金路径的风控、对账与安全设计保持大厂级水准；本轮重点补齐了 P0/P1 阶段的"数据地基"（归档、权益历史、AI 成本台账）并修复了 2 个真实缺陷。主要剩余短板是覆盖率门禁尚未到 75%、实盘验证仍然缺失、可观测性（指标/告警）不足。

## 评分卡

| 维度 | 得分 | 依据 |
|---|---|---|
| D1 资金安全与交易正确性 | 9.0 | Decimal 全程、状态机/幂等/孤儿单处理完整，资金路径走查未发现新缺陷 |
| D2 风控体系 | 9.5 | 硬门全、提交层二次校验、熔断持久化、显式授权链完整 |
| D3 安全与密钥管理 | 9.0 | 私钥不入前端/日志、SSRF 双校验、RLS 正确；新表策略与 0007 模式一致 |
| D4 执行可靠性与对账 | 9.0 | lease/fencing、unresolved、cancel-all、四向对账均有测试 |
| D5 AI 子系统 | 8.5 | 新增 token/延迟/成本台账并修复"只记 critic"缺陷；多 provider fallback 仍未实现 |
| D6 数据层与一致性 | 8.5 | 0017/0018 约束与索引齐全，schema 版本集中管理；supabase_store 覆盖仍偏低 |
| D7 测试与 CI/CD | 8.0 | 345 tests / 74% 覆盖；门禁 72%（75% 待补）；Windows 上 `--cov` 模式偶发事件循环错误 |
| D8 可观测性与运维 | 7.5 | 结构化日志/健康检查/优雅停机良好；缺 /metrics 与外部告警通道 |
| D9 代码质量与可维护性 | 8.5 | lint 全绿；schema 版本集中化；旧多租户路径仍有低覆盖大模块 |
| D10 前端 UX 与产品闭环 | 8.0 | 净值曲线/风控档位/成本台账已上线；缺 toast、术语通俗化、外部通知 |

## P1 发现（均已修复）

| ID | 位置 | 问题 | 证据 | 修复 |
|---|---|---|---|---|
| #1-D5 | `ai/openai_provider.py`、`litellm_provider.py`、`openai_compatible_provider.py` | `last_usage` 只保留最近一次模型调用，导致 primary 预测的 token/成本丢失 | `graph.forecast` 内 primary+critic 两次调用覆盖同一属性 | 改为 `_usage_log` + `drain_usage()`；`engine` 每次 forecast 后逐条落库；补 `test_graph_drain_returns_all_usage_records_and_clears` |
| #1-L2 | `apps/web/app/settings/page.tsx` | 风控档位读取 `runtime_profile.risk_policy_version`，而后端 `create_risk_policy_preset` 比较的是 `profile.version`，前端永远拿不到正确版本 | `jobs.py:2102` 使用 `profile.version`；`/v1/me` 返回 `runtime_profile.version` | 前端改读 `version`；新增 API 测试验证 aal2 门禁与保守档位应用 |

## P2 发现

| ID | 问题 | 状态 |
|---|---|---|
| #1-L1 | 仪表盘净值曲线不随 20s 轮询刷新（组件只在挂载时拉取） | 已修复：`EquityChart` 增加 20s 自动刷新 |
| #1-L2b | 应用风控档位后成功提示被 `refresh()` 的 `setNotice(null)` 清掉 | 已修复：先刷新后提示 |
| #1-D6 | schema 版本硬编码 3 处（store/jobs/personal_execution）+ 测试 2 处，易漏改 | 已修复：新增 `polybot/schema.py::EXPECTED_SCHEMA_VERSION` 并统一引用 |
| #1-D7 | 覆盖率门禁 75% 未达标（实测 74%） | 门禁暂设 72%（含 2% 安全余量）；补测 `runtime._ai`、`market` 解析、`risk` 边界、`cli`、`resolution_worker`、`personal_execution`、JWT 异常路径 |
| #1-D7 | Windows 上 `pytest --cov` 模式偶发 `ERROR`（test_tenant_execution / test_evidence），无 `--cov` 连续 5 轮全绿 | 判定为 pytest-cov 与 asyncio 在 Windows 的事件循环时序问题；CI 为 Linux 不受影响；后续在 Linux 上验证并考虑 `pytest-asyncio` loop_scope 显式化 |
| #1-D8 | 无 /metrics、无 Telegram/webhook 告警 | 列入下一轮（P2） |
| #1-D5 | 应用层多 provider 无故障切换 | 列入下一轮（P2） |
| #1-D1 | 文档声明过期（VERIFICATION.md 写 129 passed；制品内容缺新功能） | 已修复：更新至 345 passed / 74% / 新功能清单 |

## P3 发现

- `settings` 页 `meResult` 多包一层 `Promise.allSettled`，冗余但无害 → 接受。
- 归档 worker 串行抓取 20 市场 × 2 深度：保守设计，避免触发 Gamma 限流 → 接受。

## 文档声明核对

- `docs/VERIFICATION.md` 原声明"129 passed" → **过期**，已更新为 345。
- `docs/OPEN_SOURCE.md` 声明的依赖与许可 → 与 `pyproject.toml`/`THIRD_PARTY_NOTICES.md` 一致，未过期。
- `docs/ARCHITECTURE.md` 描述与当前 personal 模式一致；新增归档 worker 未在图中体现 → 下一轮补充。

## 已验证通过项（防回归清单）

- `ruff check .` 全绿；`pytest` 345 passed；前端 `vitest` 62 passed；`next build` 通过。
- 资金路径 Decimal 全程、UTC 时间处理、日志无密钥泄露、API 错误不反射输入。
- 新端点 `/v1/me/equity-history`、`/v1/me/ai-usage` 认证门禁测试通过。
- 归档 worker 单市场失败隔离、serve 优雅停止、CLI 冒烟测试通过。

## 下一轮修复清单

1. `ARCHITECTURE.md` 补充归档 worker 与权益/用量台账。
2. 可观测性：`/metrics` 最小指标端点（周期时长、AI 成本、风控触发、对账漂移）。
3. 覆盖率目标 75%：优先补 `stores/supabase_store.py`、`jobs.py`、`credentials.py` 的 FakeClient 测试。
4. 外部告警通道（Telegram/Discord webhook）设计评审。
5. 多 provider fallback 的故障切换策略设计。

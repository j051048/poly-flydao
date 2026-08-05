# 审计报告 #2（2026-08-05）

按 [`docs/AUDIT_PROMPT.md`](../AUDIT_PROMPT.md) 执行。引用上一轮编号（`#1-*`）。

## 摘要

- 总分：**9.04 / 10**（评级：优秀；较 #1 +0.22）
- P0 × 0，P1 × 0，P2 × 4，P3 × 2
- 总体判断：本轮无新增资金路径缺陷；补齐了可观测性（/metrics）与架构文档，修复了 2 处文档/版本一致性缺陷。剩余 P2 集中在外部告警、多 provider fallback、覆盖率 75% 与前端轻量化。

## 评分卡

| 维度 | 得分 | 变化 | 依据 |
|---|---|---|---|
| D1 资金安全与交易正确性 | 9.0 | — | 无新缺陷；worker 错误信息版本号修正 |
| D2 风控体系 | 9.5 | — | 无变化 |
| D3 安全与密钥管理 | 9.0 | — | /metrics 确认不含账号与密钥 |
| D4 执行可靠性与对账 | 9.0 | — | 无变化 |
| D5 AI 子系统 | 8.5 | — | 台账已完整；fallback 仍缺 |
| D6 数据层与一致性 | 8.6 | +0.1 | schema 集中常量生效；worker 启动信息同步至 0018 |
| D7 测试与 CI/CD | 8.2 | +0.2 | 349 tests；显式 loop scope；Windows 偶发 ERROR 记录在案 |
| D8 可观测性与运维 | 8.3 | +0.8 | /metrics 已实现并测试；外部告警仍未做 |
| D9 代码质量与可维护性 | 8.7 | +0.2 | 指标模块单一职责；架构文档补齐 |
| D10 前端 UX 与产品闭环 | 8.0 | — | 无新增前端功能；轻量化列入下一轮 |

## 修复项（引用上轮清单）

| ID | 状态 | 说明 |
|---|---|---|
| #1-D8 | ✅ 已修复 | 新增 `polybot/metrics.py` + `/metrics` 端点（Prometheus 文本），覆盖：HTTP 请求计数、周期数、skip 原因、AI 调用/延迟、归档快照数；无账号/密钥；`test_metrics.py` + API 端点测试 |
| #1-D6 | ✅ 已修复 | `docs/ARCHITECTURE.md` 补充归档 worker、权益历史、AI 台账与 /metrics |
| #1-D1 | ✅ 已修复 | `worker.py` 两处迁移版本文案 0016 → 0018 |
| #1-D7 | 🟡 缓解 | Windows 全量测试偶发 `ERROR`（3/~25 次，目标随机）；根因未复现；已显式 `asyncio_default_test_loop_scope="function"`；变更后连续 8 轮全绿；需 Linux CI 观察 |

## 未决 P2

| ID | 问题 | 计划 |
|---|---|---|
| #1-D8 | 外部告警通道（Telegram/Discord webhook） | 下一轮：env 配置 + 关键事件通知 + mock 测试 |
| #1-D5 | 多 provider 故障切换 | 下一轮：设计评审 + 简单优先级 fallback |
| #1-D7 | 覆盖率 75%（当前 74%） | 下一轮：supabase_store/jobs FakeClient 测试 |
| #2-L1 | 前端 recharts 使仪表盘首屏 +118 kB | 下一轮：动态 import 懒加载 |

## P3 发现

- `settings` 页 `meResult` 多余一层 `Promise.allSettled` → 接受，随手可清理。
- `Metrics.observe("polybot_ai_latency_ms")` 为 gauge（只保留最近值）→ 符合"最近延迟"语义，接受。

## 已验证通过项

- `pytest` 349 passed ×2；`ruff` 全绿；前端 62 tests + `next build` 通过。
- `/metrics` 不泄露 `service-role`/`PRIVATE`；计数标签转义有测试。
- 归档 worker 指标随 sweep 更新；引擎周期/skip/AI 指标均在既有测试路径上触发。

## 下一轮修复清单

1. 外部告警：`POLYBOT_NOTIFY_WEBHOOK_URL` + 周期完成/风控熔断/worker 异常通知。
2. 多 provider fallback：优先序配置 + 单次失败切换（只对只读预测，不对签名）。
3. 覆盖率 75%：补 `supabase_store`/`jobs` 关键查询 FakeClient 测试。
4. 前端：recharts 动态 import；toast 组件；术语 tooltip。

# 审计报告 #3（2026-08-05）

按 [`docs/AUDIT_PROMPT.md`](../AUDIT_PROMPT.md) 执行。引用前两轮编号。

## 摘要

- 总分：**9.24 / 10**（评级：优秀；较 #2 +0.20）
- P0 × 0，P1 × 0，P2 × 2，P3 × 1
- 总体判断：本轮交付外部告警通道、覆盖率 75%（357 tests）、前端首屏轻量化；剩余 P2 为多 provider fallback 与前端术语/Toast 打磨。

## 评分卡

| 维度 | 得分 | 变化 | 依据 |
|---|---|---|---|
| D1 资金安全与交易正确性 | 9.0 | — | 无新缺陷 |
| D2 风控体系 | 9.5 | — | 熔断现在可同时推送外部告警 |
| D3 安全与密钥管理 | 9.1 | +0.1 | webhook URL 复用公共 HTTPS/DNS 校验且不落日志 |
| D4 执行可靠性与对账 | 9.0 | — | 无变化 |
| D5 AI 子系统 | 8.5 | — | fallback 未实现（见 P2） |
| D6 数据层与一致性 | 8.7 | +0.1 | store/jobs 历史查询补 FakeClient 测试 |
| D7 测试与 CI/CD | 8.6 | +0.4 | 357 tests / 75% 覆盖；门禁 74%；supabase_store 覆盖提升 |
| D8 可观测性与运维 | 8.8 | +0.5 | /metrics + 外部告警均已落地 |
| D9 代码质量与可维护性 | 8.8 | +0.1 | notifier 单一职责、无阻塞语义 |
| D10 前端 UX 与产品闭环 | 8.1 | +0.1 | recharts 动态加载，首屏 JS 减少 |

## 修复项

| ID | 状态 | 说明 |
|---|---|---|
| #1-D8 | ✅ 已修复 | `polybot/notify.py` + `POLYBOT_NOTIFY_WEBHOOK_URL`：周期成功/失败、安全熔断推送；fire-and-forget、超时 10s、永不抛出；5 个测试（含私网拒绝、传输失败、无 URL noop） |
| #1-D7 | ✅ 已修复 | 覆盖率 75%（357 tests）；CI 门禁 74%；新增 `test_supabase_store_history.py` 覆盖 store/jobs 历史与台账查询 |
| #2-L1 | ✅ 已修复 | `EquityChart` 改为 `next/dynamic` + `ssr:false`，recharts 从首屏同步包中拆出 |
| #1-D5 | 🟡 未决 | 多 provider fallback：已评估侵入性，列入下一轮（仅对只读预测做切换，不触及签名路径） |

## 未决 P2/P3

- 多 provider fallback（P2）：设计为"primary + 有序 fallback 列表，只包 `forecast()`，失败切换并记录指标"。
- 前端 toast 组件与术语 tooltip（P3）：提升操作反馈与可读性。

## 已验证通过项

- `pytest` 357 passed；`ruff` 全绿；前端 62 tests + `next build` 通过。
- 通知测试覆盖：公共 webhook 投递、私网拒绝、传输异常不抛出、无配置 noop。
- store/jobs 新查询按 `recorded_at desc` 拉取后逆转为时间正序，测试验证。

## 下一轮修复清单

1. 多 provider fallback（包装层 + 指标 + 测试）。
2. 前端 toast + 术语 tooltip（如可行）。
3. 最终 VERIFICATION 更新与评分复测。

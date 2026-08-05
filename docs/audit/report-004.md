# 审计报告 #4（2026-08-05）

按 [`docs/AUDIT_PROMPT.md`](../AUDIT_PROMPT.md) 执行。引用前三轮编号。

## 摘要

- 总分：**9.38 / 10**（评级：优秀；较 #3 +0.14）
- P0 × 0，P1 × 0，P2 × 1，P3 × 1
- 总体判断：本轮完成资金路径数学深审（费率/扫单/回测指标全部 Decimal 且保守）与多 provider fallback；测试增至 362、覆盖率 75%。剩余 P2 仅为前端 Toast/术语打磨，不再涉及资金路径。

## 评分卡

| 维度 | 得分 | 变化 | 依据 |
|---|---|---|---|
| D1 资金安全与交易正确性 | 9.2 | +0.2 | 深审 `fees.py`/`strategy._sweep_book`/`_sell_candidate`：Decimal、ROUND_HALF_UP、费用后 edge、卖出净值扣费均正确 |
| D2 风控体系 | 9.5 | — | 无变化 |
| D3 安全与密钥管理 | 9.1 | — | fallback 不引入新密钥路径 |
| D4 执行可靠性与对账 | 9.0 | — | 无变化 |
| D5 AI 子系统 | 9.2 | +0.7 | 多 provider fallback 实现：仅包只读 `forecast()`、失败切换、全失败等价于单 provider 错误；`FallbackForecastProvider` 5 测试 |
| D6 数据层与一致性 | 8.7 | — | 无变化 |
| D7 测试与 CI/CD | 8.8 | +0.2 | 362 tests / 75% 覆盖；fallback 单元测试 + runtime 装配测试 |
| D8 可观测性与运维 | 8.8 | — | fallback 失败路径有日志与指标（沿用 drain） |
| D9 代码质量与可维护性 | 8.9 | +0.1 | `_ai` 重构为 `named_provider`，消除三份重复实例化 |
| D10 前端 UX 与产品闭环 | 8.1 | — | 无变化 |

## 修复项

| ID | 状态 | 说明 |
|---|---|---|
| #1-D5 | ✅ 已修复 | `ai/fallback.py`：有序 fallback，仅包 `forecast()`；`runtime._ai` 重构并按 `POLYBOT_AI_FALLBACK_PROVIDERS` 装配；`drain_usage` 聚合、`close` 幂等；5 个测试 |
| #3-未决 | 🟡 剩余 | 前端 toast + 术语 tooltip（P3，非资金路径） |

## 已验证通过项

- 资金路径数学：`estimated_fee_per_share` 在无效价格/费率时返回 0；`matched_taker_fee_usd` 将缺失 trader_side 保守视为 taker；`quantize_down` 拒绝非正步长；`_sell_candidate` 按比例缩放部分取整后的 gross/fee。
- fallback：主失败→次成功、全失败→`RuntimeError`、usage 聚合、close 无异常。
- `pytest` 362 passed；`ruff` 全绿；覆盖率 75%。

## 下一轮（收尾）

1. 最终 VERIFICATION.md 数字更新（362 / 75% / 新功能）。
2. 最终评分复测与交付总结。

# 交付验证记录

验证日期：2026-07-27（首版）；2026-08-05（审计循环 5 收尾基线）；2026-09-21（P1/P2 全量整改收尾）。

## 已通过

| 检查 | 结果 |
|---|---|
| Python lint | 在 `backend` 工作目录执行 `ruff check src/ tests/` 通过 |
| Python 测试 | `443 passed`（P1/P2 整改后全量回归，`pytest -q`） |
| Web lint | 在 `apps/web` 执行 `npm run lint`（ESLint 9 + `next/core-web-vitals`）通过 |
| Web 单元测试 | `npm test`（Vitest）12 个文件 / 79 项通过 |
| Web 端到端冒烟 | `npm run test:e2e`（Playwright，chromium）4 项通过：登录表单、未授权重定向、`/api/deployment-check`、诊断页渲染 |
| Web 生产构建 | Next.js `next build` 通过 |
| Python 生产依赖审计 | `pip-audit`：`No known vulnerabilities found` |
| Web 生产依赖审计 | `npm audit --omit=dev --audit-level=high`：0 个 high/critical（`sharp` 已通过 override 升到 0.35.4；仅剩 2 个 moderate，来自 `postcss` 尚未发布修复的上游 advisory） |
| Python 分发制品 | wheel 与 sdist 构建成功 |
| 制品内容 | wheel 包含 GDELT Context collector、Public Suffix 去重、异步 SDK client 初始化、wallet bootstrap、trade ID 对账、数据库时钟 arm expiry CAS 和交易所侧 GTD 到期保护 |
| 数据归档 | 只读 `polybot archive`（全量 L2 快照入库），迁移 0017 权益历史、0018 AI 用量台账，schema 版本由 `polybot.schema.EXPECTED_SCHEMA_VERSION` 集中管理（当前 20） |
| 数据保留 | 迁移 0020 `prune_polybot_history`（service_role-only、分批、窗口硬下限）只裁剪 `ai_usage_ledger`/`equity_history`/`snapshots`；worker 每日执行，订单/成交/预测/持仓/对账台账永不被删除 |
| 外部存活监控 | `polybot.monitor`：worker 连续未就绪超过 `POLYBOT_READINESS_ALERT_SECONDS` 推送阻塞原因（每轮故障一次，最长每 6 小时重复），恢复时推送恢复消息 |
| 就绪可解释性 | `/readyz` 与 `/worker-health` 返回 `gates`/`blockers`/`warnings`；每条阻塞项都带 `code` 与可执行修复建议 |
| AI 闭环 | 可靠性校准曲线（`polybot.ai.calibration`）由已结算预测重建并作用于集成概率；当日预算在调用前原子扣减，用尽即硬停 |
| 模块划分 | 后端不再有 >1500 行模块：`jobs/` 拆包，`api_models`/`api_dependencies`、`credentials_types`/`credentials_crypto`、`stores/payloads`/`supabase_rows`/`supabase_reconcile`、`personal_cycle`、`monitor`、`retention`、`performance` 各自独立 |
| 前端 | 仪表盘净值曲线（Recharts，懒加载）、设置页一键风控档位（conservative/balanced/advanced）、性能页 AI 成本台账 |
| 可观测性 | `/metrics` Prometheus 文本端点、`POLYBOT_NOTIFY_WEBHOOK_URL` 外部告警（周期/熔断） |
| AI | 多 provider 只读 fallback（`POLYBOT_AI_FALLBACK_PROVIDERS`）、每次预测 token/延迟/成本台账 |
| 对账 | 可选 `POLYBOT_RECONCILE_BASELINE_UTC`：豁免基准时间前的历史手动成交（其余成交仍严格映射机器人订单） |
| Polymarket 公共 API | 官方 SDK 0.2.0 成功读取当前市场与 outcome order book；当前 keyset 服务实测必须按数值 JSON 字段 `liquidityNum` 排序 |
| Polymarket SDK contract | 下单、异步成交 ID、余额、订单/成交/activity/仓位与 WS 方法形状测试通过 |
| Paper 端到端 | 实时公开行情扫描 3 个高流动性市场，生成 3 个 mock forecast，因无正净价值候选而 0 intent/0 order |
| GDELT Context 公共 API | 已核验 URL、title、sentence、context 响应形状；最终复测遇到远端断连时返回 0 个可计数来源并进入退避，没有放行交易 |
| 默认配置 | `paper` + `mock` 配置检查通过，真实资金保持锁定 |

生成的 Python 制品：

```text
backend/dist/polybot-0.1.0-py3-none-any.whl
backend/dist/polybot-0.1.0.tar.gz
```

唯一测试告警来自 Starlette 对旧 `httpx` TestClient 兼容层的弃用提示，不影响运行时网络客户端，也没有测试失败。

## 尚未验证，因此不能声称完成

- 没有用户的 Supabase 项目权限，未在远端实际应用迁移；
- 没有使用或读取真实钱包私钥，未部署/充值 Deposit Wallet；
- 没有第三方模型 key，未对用户所选模型做 schema、延迟和限流验收；
- 当前环境没有 Docker CLI，未执行本机容器构建；Python 和 Next.js 制品已分别构建；
- 没有提交真实订单，没有做小额 canary、成交/撤单/重启恢复和赎回验收；
- 合成 JSONL 回测只验证计算链路，不构成策略收益证据。

## 审计循环状态

- 审计提示词：[`docs/AUDIT_PROMPT.md`](AUDIT_PROMPT.md)
- 审计报告：`docs/audit/report-*.md`
- 覆盖率门禁：CI `--cov-fail-under=72`（当前实测 74%，75% 目标列入后续循环）。

## 上线验收标准

远端环境必须按以下顺序推进：

1. 应用 Supabase 迁移并验证 RLS、RPC、lease 和 runtime control；
2. 用真实公开行情运行至少 14 天 paper；
3. 运行至少 7 天 shadow，并逐笔核对理论价格与真实可成交价格；
4. 对冻结的样本外数据计算 Brier、log loss、费用和滑点后的净 EV、最大回撤；
5. 仅在官方 geoblock 允许的部署地点，用可完全损失的小额执行 canary；
6. 人工验证首次签名、成交、部分成交、撤单、重启对账和紧急停止；
7. 所有阶段门通过后才考虑 `live`，并继续使用硬限额与短时 arm。

“进程持续运行”不等于“持续获利”。worker 可以常驻，但真实订单只在最长 15 分钟的人工 arm 窗口内允许；系统会在证据不足、数据陈旧、AI/数据库/对账异常、地域限制、日损或回撤越限时停止新单并保持熔断。

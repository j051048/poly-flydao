# 改进分析报告 #6（全面盘点，2026-09-21）

本报告不是又一轮"审计 → 修复"循环的产物，而是一次**面向"能不能真正跑起来、能不能长期运营"的全面盘点**。
审计维度沿用 [`docs/AUDIT_PROMPT.md`](../AUDIT_PROMPT.md)，但评分时额外给"首次成功时间（Time-to-First-Success）"与"运营可持续性"分配权重，因为 #5 报告的 9.09 分与用户实际体验（长时间无法启动）存在明显偏差。

## 0. 本次实际执行过的验证（证据）

| 命令 | 结果 |
|---|---|
| `backend: python -m pytest -q` | 367 项全部通过（无 F / E） |
| `apps/web: npm test` | 10 个文件 / 62 项全部通过 |
| `git status --short` | 工作树干净，本地 main 与 origin/main 一致（2369141） |
| 代码检索 | rg 全仓检索策略装配、探针、保留策略、参数文档覆盖率 |

> 说明：前端 npm 命令必须在真实路径 apps/web 下执行；polymarket机器人/apps/web 是 junction，经非 ASCII 路径会失败。

## 1. 修订后的评分卡

| 维度 | 权重 | #5 得分 | 本次得分 | 变化原因 |
|---|---|---|---|---|
| D1 资金安全与交易正确性 | 25% | 9.2 | 9.2 | 未发现新的资金路径缺陷 |
| D2 风控体系 | 15% | 9.5 | 9.5 | 硬门与提交层二次校验依旧完整 |
| D3 安全与密钥管理 | 15% | 9.1 | 9.1 | CSP / RLS / SSRF 仍然扎实 |
| D4 执行可靠性与对账 | 12% | 9.0 | 8.6 | 对账硬门无自助恢复通道（见 P1-1） |
| D5 AI 子系统 | 8% | 9.2 | 8.8 | 校准只说不用，闭环缺失（见 P2-2） |
| D6 数据层与一致性 | 8% | 8.7 | 8.5 | 全库无保留 / 清理策略（见 P2-7） |
| D7 测试与 CI/CD | 8% | 8.8 | 8.6 | 前端零 lint、零 E2E（见 P2-5） |
| D8 可观测性与运维 | 4% | 8.8 | 8.0 | 无容器健康检查、就绪态无原因码（见 P1-1 / P1-4） |
| D9 代码质量与可维护性 | 3% | 8.9 | 8.4 | 多个 1500+ 行单体模块（见 P2-6） |
| D10 前端 UX 与产品闭环 | 2% | 8.1 | 7.5 | 引导阻塞、模式假开关（见 P1-2 / P1-3） |

加权总分 = **8.92 / 10**（#5 为 9.09）。分数下降不代表代码变差，而是把"用户跑不起来"这件事计入评分。

## 2. P1 级问题（阻塞产品可用性）

### P1-1 首轮启动死锁：对账硬门 + 无自助恢复

链路：`reconcile.py:467` 抛出 `IncompleteFillLedgerError` → `reconcile.healthy` 事件不置位 → `worker.py:882` 的 `refresh_worker_readiness()` 判定未就绪 → `api.py:908` 的 `/worker-health` 返回 503 → 前端显示 Worker 离线 → 钱包永不绑定。

问题不在于硬门本身（fail-closed 是正确取舍），而在于：

1. 任何有历史手动成交的钱包，开箱必然死锁，唯一出口是新增部署级环境变量 `POLYBOT_RECONCILE_BASELINE_UTC`（记录于 `docs/DEPLOYMENT.md:99`）；
2. 就绪失败不返回原因：`api.py:908` 只回 `{"ok": ready}`，诊断页 `apps/web/app/diagnostics/page.tsx:49` 只给一句"请检查启动日志"，用户无从判断是哪一道门；
3. 基准时间之后，任何在 Polymarket 官网的手动买卖都会再次把 worker 永久卡死，产品内没有"隔离该笔、继续运行"的通道。

修复方向：

- `/worker-health` 返回结构化就绪体：`{ready, gates:{lease, runtime_control, reconciliation}, blockers:[{code, detail, fix}]}`；
- 新增 `reconciliation_quarantine` 表：无法映射的成交进入隔离区并从可交易权益中扣除，而不是全局熔断交易；
- 前端提供"重置对账基准"一键操作（等价于安全地设置 `POLYBOT_RECONCILE_BASELINE_UTC`）。

### P1-2 模式切换是"假开关"

个人模式下 `/v1/status` 用部署环境变量强制覆盖数据库配置（`api.py:1400`、`api.py:1408-1412`），但 `PUT /v1/me/runtime-profile`（`api.py:954`）仍会返回成功。

后果：用户在前端把 `desired_mode` 改成 canary，界面提示成功，后端毫无变化——必须改 Zeabur 环境变量并重新部署。这正是"为什么必须走 Paper 才能进下一步""我不能自由切到实盘"的根因。

修复方向（保留安全边界，去掉体验摩擦）：

- `paper` ↔ `canary` ↔ `shadow` 之间的切换由数据库 `desired_mode` 热生效，worker 在周期边界做模式对齐；
- 只有"是否允许真实资金能力"这一层保留部署级开关 `POLYBOT_PERSONAL_LIVE_ENABLED`；
- 若某平台确实无法热切换，接口必须拒绝并返回明确错误，而不是静默成功。

### P1-3 引导步骤对 canary 用户恒为阻塞

`apps/web/lib/readiness.ts:66-73`：非 paper 模式下"完成一次 Paper 模拟"固定为 `blocked`，进度永远停在 75%，视觉上等同故障。

修复方向：已选择 canary/live 的用户应看到"已跳过"，或替换为"完成一次 canary 小额实盘验证"，而不是阻塞态。

### P1-4 容器无健康检查，卡死进程不会被重启

`backend/Dockerfile` 无 `HEALTHCHECK`；`/readyz` 仅存在于 `tenant_queue` 模式（`worker.py:76-82`），`SERVICE_ROLE=personal` 走 API lifespan 路径，没有等价探针。

修复方向：Dockerfile 增加 `HEALTHCHECK`；Zeabur 配置探针指向 `/livez` 与新的 `/readyz`；补充"未就绪持续 N 分钟"外部告警（`notify.py` 的 webhook 能力已具备，但目前只在周期结束时触发）。

## 3. P2 级问题

### P2-1 近半策略栈是死代码

`CryptoUpDownFilter`（`market_filters/crypto_updown.py`）、`PairAccumulator`（`strategies/pair_accumulator.py`）、`DirectionalOverlay`（`signals/crypto_direction.py`）仅在测试中被引用；`runtime.py` 只装配 `ValueStrategy`。因此 Crypto Up/Down 5 分钟市场在现网根本跑不起来。

修复方向：作为可选策略接入 engine（`POLYBOT_STRATEGY=pair_accumulator_v1`），或移入 `experimental/` 并在 README 显式声明未接线。

### P2-2 校准闭环缺失

`CalibrationBin` 与 `PerformanceSnapshot.calibration`（`jobs.py:211-224`、`jobs.py:506-540`）只做展示；`ai/graph.py` 使用固定 0.5 / 0.5 平均与固定分歧惩罚，不消费历史校准数据。AI 明知自己偏乐观，却不会修正。

修复方向：按 `(provider, model, category)` 维护可靠性曲线，用 1-2 周数据做温度缩放，再决定是否用于 sizing。

### P2-3 AI 预算缺少强制路径

有 `ai_budget` 与用量台账，但缺少"超预算即停止新预测"的硬性拦截与单周期成本告警。

### P2-4 可调参数大量未文档化

实测以下参数真实存在，却在 `docs/*.md` 与 `backend/.env.example` 中均无记录：

`POLYBOT_KELLY_FRACTION`、`POLYBOT_AUTO_REDEEM_RESOLVED`、`POLYBOT_MIN_LIQUIDITY_USD`、`POLYBOT_UNCERTAINTY_RESERVE`、`POLYBOT_MIN_EVIDENCE_ITEMS`、`POLYBOT_MAX_AI_MARKETS_PER_CYCLE`、`POLYBOT_MAX_BUCKET_EXPOSURE_PCT`、`POLYBOT_MIN_HOURS_TO_RESOLUTION`、`POLYBOT_FORECAST_COOLDOWN_SECONDS`、`POLYBOT_RECONCILE_INTERVAL_SECONDS`、`POLYBOT_GEOBLOCK_URL`、`POLYBOT_MAX_BOOK_AGE_SECONDS`。

其中 `POLYBOT_AUTO_REDEEM_RESOLVED` 默认 `false`（`config.py:203`），意味着赢的仓位不会自动赎回、资金不回流——与全自动运营直接冲突，且用户几乎不可能自行发现。

### P2-5 前端工程质量缺口

- `apps/web` 完全没有 ESLint（无配置文件、无 lint script），`next build` 仅做类型检查；
- 没有任何 E2E（无 Playwright），登录 → 引导 → 启动 → arm 主链路无自动冒烟；
- 单文件过大：`app/page.tsx` 868 行、`app/settings/page.tsx` 557 行、`app/globals.css` 约 2500 行；
- 无 Toast 组件（#5 遗留），错误仅靠行内文本；状态 20 秒轮询，无 SSE / WebSocket，操作反馈有延迟。

### P2-6 后端单体模块过大

`jobs.py` 2461 行、`stores/supabase_store.py` 1689 行、`credentials.py` 1626 行、`api.py` 1464 行。建议按子域拆分（`jobs/`、`stores/`），否则后续每次改动都伴随高回归风险。

### P2-7 数据保留与备份缺失

全仓检索 `retention|prune|purge|DELETE FROM` 零命中。`ai_usage_ledger`、`equity_history` 与归档行情快照将无限增长；`docs/SECURITY.md:87` 亦自认仍需数据库备份 / 恢复。

### P2-8 缺少外部存活监控

同上，`docs/SECURITY.md:87` 明确承认尚未配置。

## 4. P3 级问题

| 项 | 证据 |
|---|---|
| `zeabur.json` 默认 `POLYBOT_MODE=paper` | `backend/zeabur.json` |
| CSP `script-src 'unsafe-inline'` 可收窄为 nonce | `apps/web/next.config.ts` |
| 安全响应头在 `next.config.ts` 与 `vercel.json` 双份维护，易漂移 | 两处文件对比 |
| 无 CHANGELOG / CONTRIBUTING | 仓库根目录 |
| `docs/AUDIT_PROMPT.md` 评分模型缺"产品可用性"维度 | 第 3 节权重表 |

## 5. 建议落地顺序

| 顺序 | 内容 | 预期收益 |
|---|---|---|
| 1 | P1-4 健康检查 + P1-1 就绪原因码 | 故障可自解释，摆脱看日志猜 |
| 2 | P1-1 对账隔离区 + 重置基准入口 | 消除首轮死锁，旧钱包可正常启用 |
| 3 | P1-2 模式热切换 + P1-3 引导修正 | 自由切换到 canary 成立 |
| 4 | P2-4 参数文档 + 正确默认值（redeem） | 减少隐性失败，资金可回流 |
| 5 | P2-5 ESLint / E2E / 组件拆分 / Toast | 前端可长期维护 |
| 6 | P2-1 策略接线、P2-2 校准闭环 | 策略面扩展与 AI 自我修正 |
| 7 | P2-7 / P2-8 保留策略与外部监控 | 可持续运营 |
| 8 | P2-6 模块拆分 | 降低后续回归风险 |

## 6. 结论

代码质量、资金安全与风控确实达到大厂水准，367 项后端测试与 62 项前端测试全绿也是硬证据。
真正的短板集中在**产品可用性与运营闭环**：系统设计得不会丢钱，但同时很难开起来。
把 P1 四项修完，本项目的评分即可回到 9.3+ 区间，且用户侧体验会有质的变化。

## 7. 整改状态（2026-09-21 收尾，代码已落地并全量回归）

| 项 | 状态 | 证据 |
|---|---|---|
| P1-1 首轮启动死锁 | 已修复 | `/readyz` 与 `/worker-health` 返回 `gates`/`blockers`/`warnings`；迁移 0019 新增 `reconciliation_quarantine` 与 `set_personal_reconcile_baseline`（armed 或有未结订单时拒绝移动基准）；控制台提供「重置对账基准」入口；隔离项不计入可交易权益且不会全局熔断 |
| P1-2 模式假开关 | 已修复 | 数据库 `desired_mode` 在周期边界热生效（`runtime_mode.resolve_effective_mode` + `align_effective_mode`），部署级只剩 `POLYBOT_PERSONAL_LIVE_ENABLED` 这一层资金能力开关 |
| P1-3 引导恒阻塞 | 已修复 | `apps/web/lib/readiness.ts` 对 canary/live 显示"已跳过/待小额实盘验证"，不再把 paper 步骤算作阻塞 |
| P1-4 无健康检查 | 已修复 | `backend/Dockerfile` 增加 `HEALTHCHECK`；`/livez`、`/health`、`/readyz` 三探针语义分离并在 `docs/DEPLOYMENT.md` 写明 |
| P2-1 近半策略栈死代码 | 已修复 | `POLYBOT_MARKET_FILTER=crypto_updown` 端到端接线（config → engine → runtime），过滤只减少市场；未接线策略移入 `polybot/experimental/` 并附 README |
| P2-2 校准闭环缺失 | 已修复 | `polybot/ai/calibration.py`：由已结算预测重建可靠性曲线（样本不足/数据倒挂时拒绝启用，带收缩），`ForecastGraph` 将其作用于集成概率并写回 rationale |
| P2-3 AI 预算无强制路径 | 已修复 | 预算在调用前经 `consume_ai_budget` 原子扣减，用尽即 `ai_budget_exhausted` 硬停；`ai_budget_unavailable` 失败关闭；单周期请求数/成本超阈值推送一次告警 |
| P2-4 参数未文档化 | 已修复 | `Settings` 全部 83 个字段在 `backend/.env.example` + `docs/DEPLOYMENT.md` 中有记录（脚本核对 0 遗漏），`POLYBOT_AUTO_REDEEM_RESOLVED` 默认改为 `true` |
| P2-5 前端工程质量 | 已修复 | ESLint 9（`next/core-web-vitals`）+ Playwright 4 项冒烟进 CI；`page.tsx` 923→627 行、`settings/page.tsx` 594→554 行、`globals.css` 拆出 `polish`/`light-corrections`/`toasts`；Vitest 12 文件 79 项 |
| P2-6 后端单体模块 | 已修复 | 无 >1500 行模块：`jobs.py` 拆为 `jobs/` 包（schemas/supabase/memory），`api.py` + `api_models` + `api_dependencies`，`credentials.py` + `credentials_types` + `credentials_crypto`，`stores/supabase_store.py` + `payloads` + `supabase_rows` + `supabase_reconcile`，`worker.py` + `personal_cycle` |
| P2-7 数据保留缺失 | 已修复 | 迁移 0020 `prune_polybot_history`（service_role-only、分批、窗口硬下限）+ `polybot/retention.py`；worker 每日裁剪，仅限三张只增历史表；同时修复 0018 误撤销 `ai_usage_ledger` 写权限导致用量台账静默为空的问题 |
| P2-8 无外部存活监控 | 已修复 | `polybot/monitor.py` 在 worker 持续未就绪超过阈值时推送阻塞门控+修复建议（每轮故障一次、最长每 6 小时重复），恢复时推送一条恢复消息 |

### 7.1 复评（同一套权重）

| 维度 | 权重 | 整改前 | 整改后 | 依据 |
|---|---|---|---|---|
| D1 资金安全与交易正确性 | 25% | 9.2 | 9.4 | 对账隔离区让"隔离某笔、继续运行"成立，基准移动有 armed/未结订单守卫 |
| D2 风控体系 | 15% | 9.5 | 9.5 | 未改动硬门与提交层二次校验 |
| D3 安全与密钥管理 | 15% | 9.1 | 9.3 | 凭证拆分后加密原语与持久层解耦，写入路径权限按最小集恢复 |
| D4 执行可靠性与对账 | 12% | 8.6 | 9.3 | 就绪原因码、隔离区、基准重置、外部存活监控齐备 |
| D5 AI 子系统 | 8% | 8.8 | 9.4 | 校准曲线闭环 + 预算硬停 + 单周期成本告警 |
| D6 数据层与一致性 | 8% | 8.5 | 9.1 | 有界保留策略落地，审计轨迹明确排除 |
| D7 测试与 CI/CD | 8% | 8.6 | 9.5 | 后端 443 项 + 前端 lint/单测/E2E 全部进 CI |
| D8 可观测性与运维 | 4% | 8.0 | 9.4 | 容器健康检查、三探针语义、就绪阻塞项、外部告警 |
| D9 代码质量与可维护性 | 3% | 8.4 | 9.3 | 最大模块 2461 → 1407 行，公共导入路径保持不变 |
| D10 前端 UX 与产品闭环 | 2% | 7.5 | 9.0 | 引导不再恒阻塞、模式热切换、Toast 与拆分后的组件层 |

加权总分 ≈ **9.36 / 10**（整改前 8.92，`#5` 9.09）。剩余未覆盖项为 P3（CSP nonce、
`zeabur.json` 默认模式、CHANGELOG/CONTRIBUTING、审计提示词权重表）与需要外部条件的
数据库备份/恢复演练、真实钱包与第三方模型验收。

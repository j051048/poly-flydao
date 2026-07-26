# 安全模型与当前实现状态

> 结论：本仓库是 paper-first、fail-closed 的交易系统骨架，不是“保证高胜率”的产品。真实资金仍需完成 canary、账户实测和运维验收。

## 信任边界

- AI 只能生成结构化概率、区间、证据引用和反方意见；没有 signer、CLOB 下单工具或运行控制权限。
- Vercel 只承载控制台，不保存钱包私钥、service-role key、签名 payload key 或长期管理员令牌。
- `SERVICE_ROLE=api` 自动设置 `POLYBOT_COMPONENT=api`，构建无 signer 的控制面 broker。
- `SERVICE_ROLE=worker` 自动设置 `POLYBOT_COMPONENT=worker`；只有该单副本进程能解密/创建签名单。
- Supabase service role 只存在于 Zeabur；浏览器受 RLS owner-read 限制且没有写策略。

## 已实现并有测试覆盖

1. 默认 `paper + mock AI`；canary/live 需要显式风险确认、Supabase、固定官方 geoblock URL 和对应组件秘密。
2. canary 单笔硬上限 5 pUSD；确定性风控限制单笔、事件、相关 bucket、gross、日损和回撤。
3. 行情使用官方 SDK WebSocket 缓存，并至少每 30 秒用 REST 全量快照校验；数据过期拒绝交易。
4. 账户使用 authenticated user WebSocket，并以 REST 修复 open orders、带时间水位的 trades 和 positions；消失仓位归零，maker fill 按实际子单数量记账。
5. live 提交必须同时满足：短时 arm、无待确认撤单、官方 geoblock、最新价格上限/下限、余额与 allowance、健康对账、有效数据库 lease 和相同 fencing token。
6. CLI、Prefect 和 HTTP cycle 不能执行 canary/live；live broker 未安装 worker lease guard 时直接拒单。
7. intent hash 不包含随机 run ID，同一 forecast/方向/价格/数量在重启后仍可去重。
8. signed payload 用 Fernet 加密，在 POST 前写入 Supabase；order row 记录 fencing token。`mark_order_submitting` RPC 在一次数据库语句中校验未过期 lease 与相同 token，随后还会最终复检 lease 和 runtime-control version。
9. POST 超时不会重新签一张新单。无 CLOB ID 的 `signed/submitting/unknown` 会阻断后续下单、触发 cancel-all 并等待人工核对；已有 CLOB ID 的 `unknown` 也先阻断，但会逐 ID 查询、完整回放成交，并在两次 404 缺失确认后安全终态化。
10. cancel-all 必须回查开放订单为零；失败不会被吞掉。disarm 原子锁存 `cancellation_pending`，worker 确认零挂单后才用版本 CAS 清除；未清除时数据库拒绝重新 arm。
11. 挂单 BUY 预占现金并计入 gross；挂单 SELL 预占 shares。无法分类的 live exposure 以保守 bucket 计入限制。历史 peak equity 持久化。
12. canary/live 在周期开始、AI 前或 AI 后任一组合检查发现硬日损/回撤，都会先持久化 kill/disarm，再撤销并验证全部开放订单，并立即停止剩余市场；组合/权益不可用也执行同一流程。二元运行控制不安全地表达“只许 SELL”，所以真实资金模式不自动发风险退出单；paper/shadow 才保留自动 `risk_exit_v1`。
13. canary/live 至少要求两个独立可注册发布域名（eTLD+1）的 URL 证据，最终 forecast 必须实际引用对应 URL-backed evidence ID；同一出版商子域不能凑数。市场 resolution source、title-only lead 和未出现在 Web Search citation/tool metadata 的模型 URL 均不计数；数量门仍不等于来源真实性或结论正确性。
14. live 当日风险 PnL 取“完整 bot fills 的平均成本 realized PnL”和“当前权益减 UTC 日初权益”二者更差值；后者覆盖败方归零。CLOB `fee_rate_bps` 按官方价格对称公式换算，maker 费用为零。
15. 每次冷启动从 epoch 0 回放账户成交，positions 使用 `size_threshold=0`，并和 fills + REDEEM activity 重放数量核对。无法建立前序成本、出现 orphan trade、外部 token 仓位或 SPLIT/MERGE/CONVERSION 时停止交易。

## 仍是生产阻断项

- 尚未用真实 pUSD/deposit wallet 完成小额下单、部分成交、拒单、取消、重启和结算演练。
- 官方 unified Python SDK 当前仍是早期版本；升级必须重新跑 SDK shape 与 canary 测试。
- WS 没有可用的单调 sequence 校验；现在依赖短周期 REST 全量重校。连接健康与服务端 order heartbeat 还应继续加强。
- “POST 已成功但进程在保存 response 前崩溃”无法跨 CLOB/Supabase 原子提交。当前选择 fail closed + cancel + 人工对账，而不是未经官方保证的签名重放。
- fills/activity 账本只覆盖该机器人可归因的交易与 REDEEM，仍不是税务账本；奖励、存取款和人工 token transfer 不纳入成本归因。外部入金可能抬高权益并掩盖结算亏损，当前接口无法可靠自动识别，所以系统要求专用钱包运行时确认、首次启动前一次性入金、之后零外部现金/token 流。日初权益与 drawdown 不是对违反该约束的可靠补救。
- canary/live 硬熔断采用全撤单 + 持久 disarm，UTC 换日不会自动恢复。当前没有 reduce-only 运行状态；持仓处置和账本恢复需要人工审核，完成后必须显式重新 arm，不能把该过程描述成自动安全清仓。
- category/bucket 无法从账户 API 为旧仓位完整恢复时，按未分类总敞口保守处理。
- 控制 API 目前使用静态 Bearer token；尚无 Supabase Auth/MFA、CSRF token、细粒度角色、速率限制或外部告警。应先置于 SSO/VPN/访问代理后。
- paper 仓位仍按成本而非连续 mark-to-market；不能用 sample backtest 或 paper PnL 宣称实盘收益。
- 自动赎回默认关闭；虽已写入 REDEEM activity ledger 并刷新权益，仍缺告警和真实链上故障演练。
- 没有完成 SBOM/镜像签名、灾难恢复演练和 24/7 告警值班。

## 秘密与签名数据

- 钱包私钥只进入 worker 环境，不写日志、数据库、浏览器或 AI prompt。
- `POLYBOT_SIGNED_PAYLOAD_KEY` 与 Supabase service-role key 分开管理、分开轮换。
- 数据库中的 `signed_payload_ciphertext` 是可重放敏感材料；RLS 之外还应使用项目级备份加密、最小 service-role 暴露和审计。
- Fernet 提供认证加密，但当前没有把 intent/order ID 作为独立 associated data；轮换 key 时保留 `payload_key_version` 并完成旧单处置。
- 任何日志、告警和错误消息禁止包含私钥、Bearer token、service-role key、完整签名 payload 或 AI API key。

## 失败语义

- geoblock 失败、响应畸形或非官方 URL：拒绝真实资金启动/提交。
- lease、store、reconciler、余额、allowance、book freshness 任一异常：不开新仓。
- runtime control 变更：签名后也要复检；持久化 cancellation latch 确保 kill watcher 完成撤单前不能重新 arm。
- 组合/权益不可用或任一周期检查触发硬日损/回撤：持久化 kill/disarm、验证 cancel-all 并停止后续市场；任何一步无法确认都会抛出高危错误并让 worker 进入安全 shutdown，而不是只记录 skip。
- 模糊 POST：不盲目重签/重发；持久化 unknown，验证撤单并停止新单。
- cancel-all 无法验证：返回/记录高危错误，不报告“已安全撤单”。
- 账户状态与本地账本不一致：REST 修复；无法自动归因的 order 保留 unknown/audit，不猜测 terminal state。

## 上线审核

只有以下条件全部满足才讨论扩大真实资金：真实 canary 样本足够、样本外净 EV 为正且置信区间合理、校准优于市场基线、压力成本下仍可接受、所有 unknown 已清零、取消/重启/断网演练通过、控制面身份认证升级完成，并由人类明确批准新的资金上限。

官方协议事实应以 [CLOB V2 migration](https://docs.polymarket.com/v2-migration)、[order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)、[authentication](https://docs.polymarket.com/api-reference/authentication) 和 [geoblock](https://docs.polymarket.com/api-reference/geoblock) 为准。

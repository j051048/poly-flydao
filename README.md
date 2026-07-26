# Polybot

Polybot 是一套面向 Polymarket CLOB V2 的 AI 辅助自动交易工程：市场数据和外部证据进入结构化概率预测，普通代码完成盘口、费用、仓位和风险判断，只有隔离的 signer worker 可以提交订单。

> 本项目不保证“高胜率”，也没有真实收益承诺。胜率可以被高概率、低赔率交易人为抬高，却掩盖尾部损失。正确的验收指标是样本外净期望值、概率校准/Brier score、费用与滑点后的 PnL、最大回撤和真实成交质量。
>
> 当前交付应从 `paper` 开始。仓库尚未完成真实资金小额 canary 验证，不能据此直接放大资金。

## 当前状态

| 状态 | 能力 |
|---|---|
| 已实现 | 官方 `polymarket-client==0.2.0` 的市场、账户、下单和对账适配 |
| 已实现 | 行情 WebSocket 缓存，失效或未验证时回退 REST；市场发现仍使用 REST |
| 已实现 | 认证用户 WebSocket，加周期 REST 的 open orders、trades、positions 修复 |
| 已实现 | OpenAI Responses Structured Outputs，或 LiteLLM JSON Schema provider |
| 已实现 | LiteLLM 单 key + 无 key的 GDELT Context 同句证据；真实资金按可注册发布域名与实际引用双重校验 |
| 已实现 | LangGraph 主预测、独立 skeptic 和确定性区间聚合 |
| 已实现 | BUY、基于预测的 SELL；paper/shadow 自动 risk exit；canary/live 硬风险全撤单并持久 disarm |
| 已实现 | 深度 VWAP、动态费用、保守概率、1/4 Kelly 和确定性风险上限 |
| 已实现 | 签名 payload 加密后先落 Supabase，再单次广播；模糊结果不重签、不盲重试 |
| 已实现 | worker lease、递增 fencing token、提交前复验、持久化撤单锁存和安全 shutdown handoff |
| 已实现 | `api`/`worker` 组件分离：公开控制面不需要私钥，worker 才能持有 signer |
| 已实现 | 账户成交从 epoch 0 回放、dust 仓位核对、REDEEM activity ledger、UTC 日初权益与结算亏损熔断 |
| 部分实现 | Supabase orders/fills/activities/positions/risk/audit ledger 与 RLS；paper broker 状态仍在进程内 |
| 部分实现 | JSONL 回测和指标；它不是完整事件级历史重放 |
| 尚未完成 | 真实小额 canary、基于已结算样本的概率校准和漂移监控 |
| 尚未完成 | 显式 WS heartbeat/sequence-gap 检测、Polymarket order heartbeat，以及无 CLOB order ID 的模糊广播完整恢复 |
| 尚未完成 | Supabase Auth、MFA、server-side dashboard proxy 和生产告警体系 |
| 尚未完成 | NautilusTrader adapter/research runner；目前只是隔离的可选依赖 |

完整边界见 [架构说明](docs/ARCHITECTURE.md)。

## 仓库布局

```text
backend/    Zeabur API + worker、Python 源码、测试、Supabase migrations
apps/web/   Vercel 控制台
docs/       架构、验证、安全与部署说明
.github/    CI、Dependabot 与协作模板
```

Zeabur 的 API 和 worker 服务都应把 Root Directory 设为 `backend`；Vercel 的 Root Directory 保持 `apps/web`。后端目录只有一份源码和锁文件，不需要在仓库根目录保留代理副本。

## 数据与执行流

```text
Polymarket REST 市场发现
          │
CLOB 行情 WebSocket ── stale/未验证 ──► REST order book
          │
          ▼
   市场与盘口快照 ───────────────────► Supabase
          │
OpenAI 已验证搜索引用或 GDELT Context 同句 snippet（不可信数据）
          │
          ▼
LangGraph: forecaster → skeptic → deterministic aggregate
          │
          ▼
BUY / SELL / risk exit + 费用 / 深度 / Kelly / 硬风控
          │
          ▼
唯一 intent_hash → Paper / Shadow / leased signer worker
          │
          ▼
用户 WebSocket + REST 对账
orders / fills / positions：MATCHED → MINED → CONFIRMED
```

AI 只产生结构化概率和论据。它拿不到钱包、签名器、Supabase service role，也不能修改订单大小、价格、arm 状态或风险门。

## 本地安全启动

需要 Python 3.11–3.14 和 [uv](https://docs.astral.sh/uv/)。

```powershell
Set-Location backend
Copy-Item .env.example .env
uv sync --frozen --extra dev --no-editable
uv run --no-editable polybot config-check
uv run --no-editable uvicorn polybot.api:app --reload
```

这里显式使用 `--no-editable`，以避免 Windows + Python 3.11 在中文项目路径下误读 editable `.pth`；修改源码后用 `uv sync --frozen --extra dev --no-editable --reinstall-package polybot` 刷新已安装包。

另一个终端运行 paper worker：

```powershell
Set-Location backend
uv run --no-editable python -m polybot.worker
```

默认配置为：

```dotenv
POLYBOT_MODE=paper
POLYBOT_COMPONENT=all
POLYBOT_AI_PROVIDER=mock
POLYBOT_AUTO_REDEEM_RESOLVED=false
```

mock provider 返回低置信度预测，默认不会形成交易。

接入 OpenAI：

```dotenv
POLYBOT_AI_PROVIDER=openai
OPENAI_API_KEY=...
POLYBOT_FORECAST_MODEL=gpt-5.6-terra
POLYBOT_CRITIC_MODEL=gpt-5.6-sol
POLYBOT_MIN_EVIDENCE_ITEMS=2
```

也可只给一个第三方模型/gateway key：

```dotenv
POLYBOT_AI_PROVIDER=litellm
POLYBOT_EVIDENCE_PROVIDER=auto
LITELLM_API_KEY=...
POLYBOT_LITELLM_BASE_URL=https://your-openai-compatible-endpoint/v1
POLYBOT_FORECAST_MODEL=<gateway-model-name>
POLYBOT_CRITIC_MODEL=<gateway-model-name>
```

`auto` 在 LiteLLM 路径使用无 key 的 [GDELT Context 2.0 API](https://blog.gdeltproject.org/announcing-the-gdelt-context-2-0-api/)。只有非空 context、且查询词确实同句命中的近 72 小时文章才保留可计数 URL；DOC 标题线索和市场指定 resolution source 只作为元数据，不计入真实资金证据门。GDELT 限流、5xx、断连或畸形响应会缓存退避并关闭证据门，不会终止常驻 worker。原生 `json_schema` 不可用时，provider 只对 LiteLLM 明确返回的 capability mismatch 退回 JSON-only 提示并按模型缓存该能力，输出仍由同一 Pydantic schema 严格校验；鉴权、限流、超时和内容策略错误不会重试。若没有至少两个独立可注册发布域名、接口限流或模型没有实际引用这些 URL，系统会跳过交易。OpenAI 路径也只接受实际出现在 Web Search citation/tool metadata 中的 URL。

首次真实钱包初始化可生成系统内部 secret，并让官方 SDK 自动派生/部署 Deposit Wallet：

```powershell
Set-Location backend
uv run --no-editable polybot generate-secrets
$env:POLYMARKET_PRIVATE_KEY="<dedicated-wallet-private-key>"
uv run --no-editable polybot wallet-info
# 向上一条命令输出的 trading_wallet 转入唯一一笔启动资金后：
uv run --no-editable polybot wallet-bootstrap --confirm-standard-allowances
```

`wallet-info` 会让官方 SDK 派生凭据，并在需要时部署 Deposit Wallet，因此不是纯离线命令。把 Polygon pUSD 转到它输出的 `trading_wallet`，再显式运行一次 `wallet-bootstrap` 建立标准交易授权；该操作可能提交并等待链上/relayer 交易。`POLYMARKET_DEPOSIT_WALLET` 只是可选覆盖值，留空时由 SDK 从 signer 自动派生。两条命令都不会输出私钥或 CLOB 凭据。

## API 与 signer worker 分离

同一镜像可以部署为两个进程，但 `SERVICE_ROLE` 和 `POLYBOT_COMPONENT` 是两个独立设置，必须匹配：

```text
公开控制 API:
  SERVICE_ROLE=api
  POLYBOT_COMPONENT=api
  不配置 POLYMARKET_PRIVATE_KEY / POLYBOT_SIGNED_PAYLOAD_KEY

私有执行 worker:
  SERVICE_ROLE=worker
  POLYBOT_COMPONENT=worker
  不公开域名，只在 canary/live 时配置 signer secrets
```

`component=api` 在 canary/live 模式使用无签名能力的 `ControlPlaneBroker`。它可以修改 Supabase 中的短时 arm/kill switch，但不能读取交易组合或提交订单。`disarm` 会原子写入 `cancellation_pending`；私有 worker 每两秒观察该锁存，请求并验证 `cancel_all`，再用版本 CAS 确认完成。在确认开放订单为零之前，数据库拒绝重新 arm，因此即使 worker 跳过了中间 control 版本也不会漏掉撤单义务。

当前 dashboard 仍是浏览器直接向 FastAPI 发送 admin bearer 的原型。它没有 Supabase Auth、MFA 或 server-side proxy，因此不能视为成熟的真实资金控制面。

## 实盘执行保护

Canary/live 启动和每次提交需要同时满足：

1. `POLYBOT_LIVE_ACK=I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST`；
2. `POLYBOT_BETA_SDK_ACK=I_ACCEPT_BETA_SDK_CANARY_ONLY`；
3. signer worker 设置 `POLYBOT_DEDICATED_WALLET_ACK=I_CONFIRM_DEDICATED_WALLET_NO_EXTERNAL_FLOWS`，启动资金在首次 worker 启动前一次性转入，此后没有外部存取款或 token transfer；
4. API 与 worker 都使用 Supabase，进程 mode 与数据库 control mode 一致；
5. API 组件有 admin token；worker 组件有全新专用低余额 signer 和 Fernet payload key；deposit wallet 可由 SDK 自动派生；
6. 数据库 arm 未过期、kill switch 与 `cancellation_pending` 均为 false，且提交前 control version 没有变化；
7. 官方 geoblock 端点明确允许；canary/live 不接受自定义 geoblock URL；
8. worker 持有有效 lease 和 fencing token，用户对账处于 healthy；
9. 冷启动从 epoch 0 完整回放账户成交，dust 仓位与本地 fill 数量完全一致，且没有未支持的 SPLIT/MERGE/CONVERSION；
10. 当前盘口、余额、allowance、open orders、仓位和全部确定性风险上限通过。

Canary/live 还硬性要求至少两个独立可注册发布域名（eTLD+1）的 URL 证据，且最终 forecast 必须实际引用其中至少两个 URL-backed evidence ID；同一出版商的不同子域、URL、标题或摘要不能重复计数。市场 resolution source 与 title-only lead 不计数。

签名后按以下顺序执行：

```text
sign once
  → 加密 exact payload + signed hash + fencing token 持久化
  → 再验证 lease/fencing 和 control version
  → 标记 submitting
  → 单次 post_order
```

如果广播结果含糊，系统不会重新签名或自动重发，而是请求并验证 `cancel_all`，把订单保留为 durable unresolved state，并阻止 worker 创建后续订单。已知 CLOB order ID 的 `unknown` 会逐 ID 查询、回放完整成交历史，并在两次 404 缺失确认后安全终态化；没有 order ID 的 `signed/submitting/unknown` 仍需人工核对。这是 fail-closed 行为，不代表所有模糊订单都能自动解决。

SDK 0.2.0 会返回即时成交的 `trade_ids`，而结算交易哈希可能稍后才可用。系统把两者写入响应审计，但仍以 user WebSocket 和 REST 成交对账到 `CONFIRMED`/`FAILED` 为准，不把 `POST /order` 成功当成已结算成交。

Canary 还会强制 `POLYBOT_MAX_ORDER_USD <= 5`。官方统一 SDK 仍按 beta 依赖对待，单元测试不能替代真实小额 canary。

## SELL 与风险退出

策略不再只有买入：

- 当已持有 YES/NO 且可执行 bid 高于保守概率上界时，可以生成普通 SELL 退出；
- 成交实现损失、UTC 日初到当前的权益损失二者取更差值；paper/shadow 触发硬阈值时先撤单再生成 `risk_exit_v1`；
- canary/live 在周期开始、AI 前或 AI 后发现硬阈值，或无法取得可信组合/权益时，会持久化 kill/disarm、撤销并验证全部旧挂单并停止剩余市场；UTC 换日不会自动 re-arm；
- 当日已实现 PnL 按 UTC 日界从完整 bot fill 历史重建平均成本；持久化日初权益同时覆盖败方归零和赎回造成的结算损失；
- 账户冷启动从 epoch 0 对账，`size_threshold=0` 包含 dust；实时仓位数量必须与 fills + REDEEM activity 重放一致，无法证明成本基础时 fail closed；
- 已有持仓即使不在热门市场扫描结果中，paper/shadow 也会按 `condition_id` 精确反查并优先处理；硬熔断期间不再调用 AI 或评估新入场；
- paper/shadow risk exit 仍检查市场可交易、盘口新鲜、持仓数量、最小订单和限价，但不会被最低 edge 或最低流动性门阻止；
- 当前二元运行控制无法安全表达 reduce-only。真实资金硬熔断选择全撤单 + disarm，不声称自动清仓；持仓与账本需人工审核后显式重新 arm。

## 自动赎回

自动赎回默认关闭：

```dotenv
POLYBOT_AUTO_REDEEM_RESOLVED=false
```

只有显式设为 `true`，且 worker lease、短时 arm 和 geoblock 同时有效时，worker 才会尝试赎回最多 5 个 resolved condition 并等待 SDK handle。完成后立即刷新权益；周期对账把 `REDEEM` 写入 `account_activities`，在平均成本账中关闭整个 condition。该路径已有 fail-closed 账本，但仍未经过真实小额 canary，因此首期保持关闭。

## 回测

```powershell
Set-Location backend
uv run --no-editable polybot backtest --input examples/backtest_sample.jsonl
```

示例数据只是合成格式样例，不是收益证据。当前回测器计算 Brier score、log loss、费用/延迟后的 PnL、ROI、胜率和最大回撤，但不模拟完整订单队列、WS 延迟序列、撤单、链上结算或 point-in-time prompt 重建。

真实验收必须使用按时间冻结的规则、证据、预测、L2 深度、费用、延迟和最终结算，按 walk-forward 切分训练、校准和测试集。详见 [策略验证说明](docs/STRATEGY_VALIDATION.md)。

## 文档

- [两篇教程的事实核验](docs/ARTICLE_REVIEW.md)
- [八个开源仓库与实际接线状态](docs/OPEN_SOURCE.md)
- [架构、实现状态和实盘阻塞项](docs/ARCHITECTURE.md)
- [策略验证与上线 Gate](docs/STRATEGY_VALIDATION.md)
- [Vercel / Zeabur / Supabase 部署](docs/DEPLOYMENT.md)
- [安全模型](docs/SECURITY.md)
- [交付验证记录](docs/VERIFICATION.md)

## 许可证

本仓库代码采用 MIT。NautilusTrader 为 `LGPL-3.0-or-later`，只存在于 `research` optional dependency，不进入默认生产镜像或 signer 路径；详见 [开源组件说明](docs/OPEN_SOURCE.md)。

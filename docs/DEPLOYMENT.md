# 部署手册：Vercel + Supabase + 一个 Zeabur 服务

个人版的推荐拓扑是：Vercel 前端、Supabase Auth/数据库、一个 Zeabur `personal` 服务。AI Key 和 EVM 私钥不经过浏览器，全部由 Zeabur Secret 环境变量注入。

## 0. 先清理旧的两服务变量

如果 Zeabur 服务是从旧多租户模板升级，请删除：

```text
所有 NEXT_PUBLIC_*
PASSWORD
POLYBOT_ADMIN_TOKEN
POLYBOT_CREDENTIAL_PUBLIC_KEY_PEM
POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM
POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON
POLYBOT_CREDENTIAL_FINGERPRINT_KEY
POLYBOT_SIGNED_PAYLOAD_KEY
POLYBOT_PAYLOAD_KEY_VERSION
POLYBOT_TENANT_WORKER_MAX_CONCURRENCY
POLYBOT_TENANT_JOB_LEASE_SECONDS
POLYBOT_TENANT_JOB_POLL_SECONDS
```

这些值不属于个人单服务模式。截图、聊天或历史日志中曾显示过的 API Key、EVM 私钥或 Supabase service-role key 必须先轮换，不能继续使用。

## 1. 配置 Supabase

1. 创建 Supabase 项目。
2. 在 SQL Editor 按文件名顺序执行 `backend/supabase/migrations/` 中的全部迁移，直至最新编号 `0016`。不要只执行最后一份；个人模式仍复用之前的订单、风控和租约表。
3. 在 **Authentication → Users** 创建或确认唯一 owner 用户，复制其 User UUID。这个 UUID 将作为 `POLYBOT_ACCOUNT_ID`；不要填邮箱、项目 ref 或钱包地址。
4. 在 Auth 设置中关闭公开注册。控制台只是 owner 登录入口，不是多人注册产品。
5. 配置站点 URL 与回调白名单：

```text
http://localhost:3000/auth/callback
https://YOUR_VERCEL_DOMAIN/auth/callback
```

6. 复制 Project URL、publishable key 和 service-role key。publishable key 只给 Vercel；service-role key 只给 Zeabur。

个人模式不要求 TOTP MFA、RSA credential envelope 或 credential fingerprint。后端仍用 JWT `sub` 校验当前登录者必须等于 `POLYBOT_ACCOUNT_ID`。

## 2. 部署一个 Zeabur Personal Service

从 GitHub 仓库创建一个服务：

- Root Directory：`backend`
- Health Check Path：`/livez`
- Port：`8080`
- Replicas：`1`
- 公网域名：需要，用于 Vercel 调用 API

逐项复制 [`backend/deploy/personal.env.example`](../backend/deploy/personal.env.example)。最小配置如下：

```dotenv
PORT=8080
SERVICE_ROLE=personal
WEB_CONCURRENCY=1
POLYBOT_MODE=paper
POLYBOT_PERSONAL_AUTO_RUN=true
POLYBOT_PERSONAL_LIVE_ENABLED=false
POLYBOT_LOG_LEVEL=INFO

SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=REPLACE_ME
POLYBOT_ACCOUNT_ID=OWNER_AUTH_USER_UUID

POLYBOT_DASHBOARD_ORIGINS=https://YOUR_VERCEL_DOMAIN

POLYBOT_AI_API_KEY=REPLACE_ME
POLYBOT_AI_BASE_URL=https://YOUR_AI_PROVIDER.example/v1
POLYBOT_AI_MODEL=YOUR_MODEL_ID

POLYMARKET_PRIVATE_KEY=0x64_HEX_CHARACTERS
POLYBOT_BANKROLL_USD=100
POLYBOT_MAX_ORDER_USD=2

# 可选：数据归档（默认开启）、外部告警、AI fallback
POLYBOT_ARCHIVE_ENABLED=true
POLYBOT_ARCHIVE_MARKET_LIMIT=20
POLYBOT_ARCHIVE_INTERVAL_SECONDS=300
# POLYBOT_NOTIFY_WEBHOOK_URL=https://hooks.example.com/your-endpoint
# POLYBOT_AI_FALLBACK_PROVIDERS=litellm,openai_compatible
# 可选：忽略该 UTC 时间之前的账户历史成交（仅用于"专用钱包历史手动交易"豁免，
# 之后的任何成交仍必须映射机器人订单）。格式：2026-08-05T00:00:00Z
# 建议改用控制台「重置对账基准」：无需重新部署，且在 armed 或有未结订单时会被拒绝。
# POLYBOT_RECONCILE_BASELINE_UTC=2026-08-05T00:00:00Z
```

说明：

- `PORT` 必须是纯数字 `8080`，不要填写 `${WEB_PORT}`；平台会把该字符串原样传给应用，Pydantic 会因此启动失败。
- `SERVICE_ROLE=personal` 会自动设置 `POLYBOT_COMPONENT=all`、`POLYBOT_PERSONAL_MODE=true` 和 `POLYBOT_WORKER_EXECUTION_MODEL=single_account`，无需重复填写。
- `POLYBOT_DASHBOARD_ORIGINS` 是 Vercel 的完整 origin；多个域名用英文逗号分隔，不带路径、通配路径或末尾 `/`。
- 自定义 AI 中转站必须是公开 HTTPS、OpenAI Chat Completions 兼容接口。Base URL 通常填到 `/v1`；模型 ID 必须使用中转站实际接受的名称。
- 提供 Base URL 时，后端自动使用 OpenAI-compatible 模式并把该 hostname 锁为 AI Key 的唯一目标，无需再填写 provider 或 allowlist。
- 使用官方 OpenAI 时删除 `POLYBOT_AI_BASE_URL`，只填写 `POLYBOT_AI_API_KEY` 与明确的 `POLYBOT_AI_MODEL`；后端会自动识别。
- `POLYBOT_AI_FALLBACK_PROVIDERS`：逗号分隔的只读预测 fallback 顺序（`openai` / `litellm` / `openai_compatible`），只影响 AI 预测，不影响签名与下单。
- `POLYBOT_NOTIFY_WEBHOOK_URL`：Telegram/Discord 兼容 webhook，周期完成、失败与安全熔断会推送；不配置则完全静默。
- `POLYBOT_RECONCILE_BASELINE_UTC`：仅在你确认旧钱包上存在"机器人之外的历史手动成交"且已清仓时使用。设置后，该时间之前的账户成交会在对账时被忽略（不落库、不告警），该时间之后的新成交仍然严格校验必须来自机器人订单。**使用前请确认钱包当前无任何 token 持仓，只保留 USDC。**
- `POLYBOT_MARKET_FILTER`：默认 `none`（按流动性取 top-N 市场）。设为 `crypto_updown` 后，每轮只保留短周期加密 Up/Down 窗口（默认 BTC/ETH 5 分钟市场），`POLYBOT_CRYPTO_UPDOWN_ASSETS` 支持 `BTC`/`ETH`/`SOL`/`XRP`。该过滤器只会**减少**市场，永远不会扩大风险敞口；过滤掉但仍有持仓的市场保持"只减仓"退出。
- `POLYBOT_CALIBRATION_REFRESH_SECONDS`：AI 可靠性校准曲线从已结算预测中重建的间隔（默认 900 秒）。样本不足时曲线保持关闭，AI 不会用噪声修正自己。
- `POLYBOT_AI_CYCLE_UNITS_ALERT` / `POLYBOT_AI_CYCLE_COST_ALERT_USD`：单周期 AI 请求数或预估成本超过阈值时推送一次告警（每小时最多一次）。当日预算本身在每次调用前原子扣减，用尽即**硬停**新预测，已持仓仍按风控退出，UTC 零点自动恢复。
- `POLYBOT_RETENTION_*`：`ai_usage_ledger`、`equity_history`、`snapshots` 三张只增历史表的保留窗口（默认 90/365/30 天）与执行间隔（默认 86400 秒）。裁剪走数据库 `prune_polybot_history`（service_role-only、分批、窗口有硬下限），订单、成交、预测、持仓和对账台账属于真实资金审计轨迹，永不被删除。关闭裁剪可设 `POLYBOT_RETENTION_ENABLED=false`。
- `POLYBOT_READINESS_ALERT_SECONDS`：worker 连续未就绪超过该秒数后，通过 `POLYBOT_NOTIFY_WEBHOOK_URL` 推送一次阻塞原因（含门控名与修复建议），同一轮故障最多每 6 小时重复一次，恢复时再推送一条恢复消息。进程存活但永远不交易时，这是唯一的对外信号。
- 高级/遗留参数（个人模板刻意不写入 `.env.example`，因为个人模式会自动推断 provider，写入反而会造成误配）：`POLYBOT_MARKET_LIMIT`（每轮扫描市场数，默认 20）、`POLYBOT_RESOLUTION_POLL_SECONDS`（默认 300）、`POLYBOT_AI_TIMEOUT_SECONDS`（默认 45）、`POLYBOT_MIN_EDGE`（默认 0.04）、`POLYBOT_MIN_FORECAST_CONFIDENCE`（默认 0.55）、`POLYBOT_MAX_TRADE_RISK_PCT` / `POLYBOT_MAX_EVENT_EXPOSURE_PCT` / `POLYBOT_MAX_GROSS_EXPOSURE_PCT` / `POLYBOT_DAILY_LOSS_LIMIT_PCT` / `POLYBOT_MAX_DRAWDOWN_PCT`（默认 0.005 / 0.02 / 0.10 / 0.02 / 0.08，控制台风控档位会批量覆盖）、`POLYBOT_SUPABASE_TIMEOUT_SECONDS`（默认 15，避免数据库卡死拖住周期）、`POLYBOT_API_HOST`（默认 0.0.0.0）、`POLYBOT_EVIDENCE_PROVIDER`（`auto|openai_web|gdelt|none`）、`POLYBOT_CRITIC_MODEL`。
- 非个人（多租户）实盘路径额外要求三个确认串，personal 模式不需要：`POLYBOT_LIVE_ACK=I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST`、`POLYBOT_BETA_SDK_ACK=I_ACCEPT_BETA_SDK_CANARY_ONLY`、`POLYBOT_DEDICATED_WALLET_ACK=I_CONFIRM_DEDICATED_WALLET_NO_EXTERNAL_FLOWS`，以及 `POLYBOT_SIGNED_PAYLOAD_KEY` 与凭证解密密钥。该路径的 AI provider 需显式给出 `POLYBOT_AI_PROVIDER`（`mock|openai|litellm|openai_compatible`）、`POLYBOT_OPENAI_API_KEY`、`POLYBOT_LITELLM_BASE_URL`、`POLYBOT_LITELLM_API_KEY`、`POLYBOT_CUSTOM_AI_ALLOWED_HOSTS`。**个人部署不要设置这些变量。**
- 只使用全新、低余额、专门给机器人使用的钱包。不要使用主钱包或助记词。
- 如果 Polymarket 账户使用独立 proxy/funder 地址，额外设置 `POLYMARKET_DEPOSIT_WALLET=0x...`；直接 EOA 可不填。

重新部署后检查：

```text
GET https://YOUR_ZEABUR_DOMAIN/livez
GET https://YOUR_ZEABUR_DOMAIN/health
GET https://YOUR_ZEABUR_DOMAIN/worker-health
GET https://YOUR_ZEABUR_DOMAIN/readyz
```

`/livez` 仅表示进程存在；`/health` 检查 Supabase 控制面；`/worker-health` 与 `/readyz`
返回同一份结构化就绪体（含 `gates` / `blockers` / `warnings`），任何角色都可用。

## 3. 部署 Vercel

Root Directory 设置为 `apps/web`。只配置三个浏览器可公开值：

```dotenv
NEXT_PUBLIC_API_BASE_URL=https://YOUR_ZEABUR_DOMAIN
NEXT_PUBLIC_SUPABASE_URL=https://PROJECT_REF.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=sb_publishable_REPLACE_ME
```

旧项目可继续使用 `NEXT_PUBLIC_SUPABASE_ANON_KEY` 代替 publishable key，但不要同时误放 `SUPABASE_SERVICE_ROLE_KEY`。Vercel 中绝不能出现 AI Key、EVM 私钥、seed phrase、service-role key、CLOB credential 或 shared admin token。

Vercel 重新部署后，用唯一 owner 登录。配置页只显示 Zeabur 的脱敏就绪状态，不再要求在浏览器粘贴 AI/EVM 秘密或绑定 MFA。

## 4. 先跑 Paper

保持：

```dotenv
POLYBOT_MODE=paper
POLYBOT_PERSONAL_LIVE_ENABLED=false
```

确认以下项目后再考虑真实资金：

1. `/diagnostics` 的 Vercel → Zeabur、Vercel → Supabase、Zeabur → Supabase 和 Worker 状态均正常；
2. 配置页显示 AI、钱包和自动周期已识别，但不回显秘密；
3. 手动“运行一次”能完成，自动周期能持续刷新；
4. Paper 订单、资产、拒绝原因和预测记录能在重启后恢复；
5. 风险参数、时区、最大单笔、日亏损和回撤阈值符合自己的承受能力。

## 5. 显式开启 Canary/Live

真实资金能力仍由 Zeabur 上的单一开关授权，但**模式本身可以热切换**：

```dotenv
POLYBOT_PERSONAL_LIVE_ENABLED=true
POLYBOT_MODE=paper
```

重启一次后，`paper` / `shadow` / `canary` / `live` 全部在控制台切换（写入
`runtime_profiles.desired_mode`），常驻 Worker 在下一个周期边界自动对齐，**不需要再改环境变量、也不需要重新部署**。

- `POLYBOT_PERSONAL_LIVE_ENABLED=false` 时请求 `canary`/`live` 会被 API 以 409 明确拒绝
  （不再是"界面提示成功但什么都没变"）；若数据库里残留了实盘模式，实际运行会被降级为 `paper`，
  并在 `/v1/status` 的 `mode_note` 与向导里写明原因。
- 建议始终保持 `POLYBOT_MODE=paper`：进程永远以安全模式启动，实盘只能由控制台显式切换。
- 从实盘切回 `paper`/`shadow` 时，Worker 会先撤单、确认零开放单并写入 disarm 后字才继续模拟周期。

稳定验证后，若确实接受更高风险，在控制台切到 `live` 即可。AI 不能修改任何环境变量，也不能修改模式。

Canary/Live 仍会检查数据库运行控制、Worker 租约、控制版本、钱包余额/allowance 就绪时间、风险限制、未解决订单、官方地理限制和盘口新鲜度。`POLYBOT_PERSONAL_LIVE_ENABLED=true` 是 owner 的部署级授权，不是盈利证明，也不会解除 P2 research-only 闸门。

启动资金应使用可以完全承受损失的小额资金。先旁观首笔 Canary，核对链、collateral、限价、GTD/post-only、成交、撤单、仓位和数据库账本。任何异常先在控制台点击“停用并撤单”；若随后切回 `paper` 并设置 `POLYBOT_PERSONAL_LIVE_ENABLED=false`，暂时保留原 `POLYMARKET_PRIVATE_KEY`。新 Paper Worker 会在持有租约后再次执行全撤单并验证零开放单，只有 `/worker-health` 恢复 200 才会开始模拟周期。

## 6. 常见故障

### Zeabur 反复重启：`ValidationError for Settings`

逐项检查：

- `PORT=8080`，不是 `${WEB_PORT}`；
- `SERVICE_ROLE=personal`；
- `POLYBOT_ACCOUNT_ID` 是合法且真实存在的 Supabase Auth UUID，不是默认全零值；
- `SUPABASE_URL` 使用 HTTPS，service-role key 已轮换且完整；
- EVM 私钥是 `0x` 加 64 个十六进制字符；
- AI Base URL 是公开 HTTPS；个人模式会自动把该 hostname 设为唯一允许的 AI Key 目标；
- `POLYBOT_MODE=canary/live` 时同时设置了 `POLYBOT_PERSONAL_LIVE_ENABLED=true`。

### 容器健康检查与就绪探针

- Docker 镜像自带 `HEALTHCHECK`（`python -m polybot.healthcheck`），只探测 `/livez`。
  **故意不探测 `/readyz`**：一个"没丢钱但还没就绪"的部署（钱包未绑定、未充值、存在隔离成交）
  必须继续运行并把原因报出来，而不是被平台判定为不健康后重启。
- Zeabur 服务的 Health Check Path 建议填 `/livez`（存活）；把 `/readyz` 用于人工/外部监控：

```text
GET https://YOUR_ZEABUR_DOMAIN/livez    # 200 = 进程存活（平台探针用这个）
GET https://YOUR_ZEABUR_DOMAIN/readyz   # 200 = Worker 全部就绪；503 = 见 blockers
```

`/readyz`（与 `/worker-health` 同源）在任何 `SERVICE_ROLE` 下都可用，返回结构化原因：

```json
{
  "ready": false,
  "gates": { "store": true, "lease": true, "runtime_control": true, "reconciliation": false },
  "blockers": [
    {
      "code": "unmapped_account_trade",
      "gate": "reconciliation",
      "message": "账户里存在无法映射到机器人订单的成交。",
      "fix": "在官网清仓后于「诊断」页重置对账基准。"
    }
  ],
  "warnings": [{"code": "reconciliation_quarantine", "message": "1 笔成交处于隔离区"}],
  "not_ready_seconds": 180
}
```

建议对 `/readyz` 配置外部监控（UptimeRobot / Better Stack 等）：连续返回 503 超过
`POLYBOT_NOTIFY_WEBHOOK_URL` 告警阈值时人工介入，而不是重启容器。

### 对账隔离区与基准重置

- 机器人无法证明属于自己、且**不在机器人自身 token 足迹内**的成交/持仓会进入
  `reconciliation_quarantine`：不计入机器人权益、不参与成本基础，也不会阻塞你的第一次运行。
- 一旦成交落在机器人交易过的 token 上，仍按原样 fail closed（隔离区不能掩盖真实分歧）。
- 控制台「重置对账基准」（`POST /v1/personal/reconciliation/baseline`）等价于设置
  `POLYBOT_RECONCILE_BASELINE_UTC`，但无需重新部署，且被数据库函数拒绝于
  armed / `accept_new_intents` / 撤单进行中 / 存在非终态订单的状态。

新镜像会在启动 Uvicorn 前运行脱敏配置检查。优先查找 Zeabur 日志中的
`POLYBOT_CONFIG_ERROR field=... message=...`；它会指出规则但不会打印 Key。不要只复制
Kubernetes 的 BackOff 摘要。也可以在本地以同一组环境变量运行：

```powershell
Set-Location backend
$env:POLYBOT_COMPONENT="all"
$env:POLYBOT_WORKER_EXECUTION_MODEL="single_account"
$env:POLYBOT_PERSONAL_MODE="true"
uv run python -m polybot.config_check
```

### 前端显示 `Failed to fetch`

1. 直接访问 Zeabur `/livez` 与 `/health`；502 表示后端未启动，不是前端问题。
2. 检查 Vercel 的 `NEXT_PUBLIC_API_BASE_URL` 是 Zeabur 公网 HTTPS origin，而不是 AI 中转站 URL。
3. 检查 Zeabur 的 `POLYBOT_DASHBOARD_ORIGINS` 精确包含当前 Vercel Preview/Production origin，并重新部署。
4. 若用了自定义域名，Vercel 环境变量和 CORS origin 都要改成实际访问域名。

### 登录成功但 API 返回 403

对比当前 Supabase 用户的 Auth User UUID 与 Zeabur `POLYBOT_ACCOUNT_ID`。个人模式只允许这一个 `sub`；邮箱相同不代表 UUID 相同。

### `/health` 500 或数据库 RPC 不存在

确认 Zeabur 使用的是 `SUPABASE_SERVICE_ROLE_KEY`，并从 `0001` 到最新 `0016` 顺序应用全部 migration。不要把 publishable/anon key 当作 service role。

### Worker 一直未就绪

不要再靠读日志猜：直接看 `/readyz` 的 `blockers`，每一项都带 `code`、中文原因和 `fix`。

- `store_unhealthy`：Supabase 不可达、service-role key 错误或 migration 未跑完；
- `lease_unavailable`：有第二个副本在抢租约，确认只有一个副本且 `WEB_CONCURRENCY=1`；
- `runtime_control_blocked`：暂停中或撤单未确认，在控制台解除暂停后自动恢复；
- `unmapped_account_trade` / `fill_ledger_position_mismatch` / `fill_ledger_incomplete`：
  钱包里有机器人之外的历史成交。已清仓时点「诊断 → 重置对账基准」即可，无需重新部署；
  未清仓请先在官网卖出。落在机器人自身 token 上的分歧仍会 fail closed，不会被隔离掩盖。

`warnings` 里的 `reconciliation_quarantine` 表示部分成交/持仓被隔离：机器人会继续运行，
但这些仓位不计入权益、相关市场也不会被交易。

## 7. 部署前验证

```powershell
Set-Location backend
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest

Set-Location ..\apps\web
npm ci
npm test
npm run build
```

仓库 CI 的 Supabase migration reset 应在全新本地数据库应用全部迁移并执行 reset。静态字符串测试不能替代 PostgreSQL 对 SQL、权限和事务的实际验证。

## 8. 高级/旧多租户拓扑

若以后恢复对外 SaaS，再使用两个 Zeabur 服务：公开 `SERVICE_ROLE=api` 与私有 `SERVICE_ROLE=worker`，配套 tenant queue、RSA credential envelope 和独立 payload key。参考 `backend/deploy/api.env.example` 与 `backend/deploy/worker.env.example`。个人部署不要混用这两套变量。

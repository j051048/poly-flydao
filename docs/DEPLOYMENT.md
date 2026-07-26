# 部署手册：Vercel + Zeabur + Supabase

> 当前交付可直接用于本地回测、paper 和 shadow。`canary/live` 代码路径具备多重硬锁，但尚未完成真实资金 canary 验收，因此不得直接扩大资金。

## 1. 组件边界

生产建议部署三个服务：

1. Vercel：`apps/web` 控制台，只保存公开 API 地址；不放私钥、Supabase service-role key 或管理员令牌。
2. Zeabur API：同一 Docker 镜像，`SERVICE_ROLE=api`。这是无 signer 的控制面，只读状态并写入短时 arm/kill control。
3. Zeabur worker：同一镜像，`SERVICE_ROLE=worker`，单副本。只有这里保存钱包私钥，并且只有持有 Supabase fencing lease、健康账户对账流和短时 arm 时才能提交订单。

Supabase 保存市场快照、证据、预测、意图、加密签名单、订单、成交、账户 activity、仓位、日初权益、风险事件、运行控制和 worker lease。新项目依次执行 [0001](../backend/supabase/migrations/0001_initial.sql)、[0002](../backend/supabase/migrations/0002_daily_equity_risk.sql)、[0003](../backend/supabase/migrations/0003_account_activity_ledger.sql)、[0004](../backend/supabase/migrations/0004_reconciliation_state.sql)、[0005](../backend/supabase/migrations/0005_runtime_control_expiry.sql) 和 [0006](../backend/supabase/migrations/0006_order_expiry.sql)；已有项目只执行尚未应用的后续迁移。

## 2. 本地验证

需要 Python 3.11–3.14、uv 和 Node.js 20.9+：

```powershell
Set-Location backend
uv sync --frozen --extra dev --no-editable
uv run --no-editable ruff check .
uv run --no-editable pytest
uv run --no-editable polybot config-check
uv run --no-editable polybot backtest --input examples/backtest_sample.jsonl
uv run --no-editable polybot generate-secrets
```

`--no-editable` 是刻意的：它兼容 Windows/Python 3.11 的非 ASCII 项目路径；源码变化后用 `uv sync --frozen --extra dev --no-editable --reinstall-package polybot` 刷新已安装包。

可选 Nautilus 离线研究环境：

```powershell
Set-Location backend
uv sync --frozen --extra research --no-editable
uv run --no-editable polybot research-runtime
```

启动本地 API 和 worker：

```powershell
Set-Location backend
uv run --no-editable uvicorn polybot.api:app --host 127.0.0.1 --port 8080
uv run --no-editable python -m polybot.worker
```

安全默认值为 `POLYBOT_MODE=paper`、`POLYBOT_AI_PROVIDER=mock`，不会提交真实订单。

## 3. Supabase

1. 新建项目和一个对应 `POLYBOT_ACCOUNT_ID` 的 Auth 用户。
2. 按文件名顺序运行 `backend/supabase/migrations/0001`–`0006`；`0004` 为消失订单的两次确认保存持久状态，`0005` 用数据库时钟和 CAS 保证 arm 到期必须先撤单再重新授权，`0006` 保存交易所侧 GTD 到期时间。
3. 仅把 `SUPABASE_URL` 和 `SUPABASE_SERVICE_ROLE_KEY` 放进 Zeabur API/worker；绝不放入 Vercel 的 `NEXT_PUBLIC_*`。
4. 迁移启用了 RLS：认证用户只有 owner-read，浏览器没有写策略；service role 执行可信服务写入。
5. signer worker 的 `POLYBOT_SIGNED_PAYLOAD_KEY` 必须是独立 Fernet key；数据库只保存 ciphertext，密钥不进入数据库/API/Vercel。

首次应用迁移后，用 service-role 身份验证 `/health` 返回 200。启动预检会验证目标 Auth user、`0002`–`0004`/`0006` 的关键列和 `0005` 的 expiry RPC；账户 UUID、迁移或 service-role 权限错误会返回 503，worker 会在三次有界重试后非零退出，而不是无限伪健康重试。

## 4. Zeabur：两个服务

在 Zeabur 分别创建 API 和 worker 两个 Git 服务，并把两者的 **Root Directory 都设为 `backend`**。Zeabur 随后会在该目录发现 [Dockerfile](../backend/Dockerfile) 与 `zeabur.json`；不要使用仓库根目录作为 Docker build context，也不要在根目录保留第二份后端配置。镜像以非 root 用户运行，`backend/uv.lock` 是冻结依赖来源。

### API 服务（无 signer）

```dotenv
SERVICE_ROLE=api
POLYBOT_MODE=canary
POLYBOT_ACCOUNT_ID=<supabase-auth-user-uuid>
POLYBOT_LIVE_ACK=I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST
POLYBOT_BETA_SDK_ACK=I_ACCEPT_BETA_SDK_CANARY_ONLY
POLYBOT_ADMIN_TOKEN=<long-random-secret>
POLYBOT_DASHBOARD_ORIGINS=https://your-dashboard.vercel.app
POLYBOT_AI_PROVIDER=mock
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
```

API 服务不需要、也不应拥有 `POLYMARKET_PRIVATE_KEY`、deposit wallet 或 signed-payload key。

### worker 服务（唯一 signer）

```dotenv
SERVICE_ROLE=worker
POLYBOT_MODE=canary
POLYBOT_ACCOUNT_ID=<same-uuid>
POLYBOT_LIVE_ACK=I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST
POLYBOT_BETA_SDK_ACK=I_ACCEPT_BETA_SDK_CANARY_ONLY
POLYBOT_DEDICATED_WALLET_ACK=I_CONFIRM_DEDICATED_WALLET_NO_EXTERNAL_FLOWS
POLYBOT_MAX_ORDER_USD=5
POLYBOT_AI_PROVIDER=litellm
POLYBOT_EVIDENCE_PROVIDER=auto
LITELLM_API_KEY=<one-third-party-ai-key>
POLYBOT_LITELLM_BASE_URL=https://your-provider.example/v1
POLYBOT_FORECAST_MODEL=<provider-model>
POLYBOT_CRITIC_MODEL=<provider-model>
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
POLYMARKET_PRIVATE_KEY=...
# 可选；留空时官方 SDK 自动派生/部署默认 Deposit Wallet
POLYMARKET_DEPOSIT_WALLET=
POLYBOT_SIGNED_PAYLOAD_KEY=<fernet-key>
POLYBOT_PAYLOAD_KEY_VERSION=1
POLYBOT_AUTO_REDEEM_RESOLVED=false
```

worker 必须保持单副本；数据库 lease 是第二道保护，而不是允许随意水平扩容。worker 每 2 秒观察持久化的 `cancellation_pending`，验证 cancel-all 后用版本 CAS 确认完成；确认前数据库拒绝重新 arm。失去 lease、对账健康或数据库健康时也会尝试 cancel-all 并验证开放订单为零。

`POLYBOT_DEDICATED_WALLET_ACK` 是运行时硬门，不是检测能力：Polymarket 账户接口不能可靠归因 pUSD 存取款、奖励和任意 token transfer。必须在**首次 worker 启动前**完成唯一一次启动入金，之后禁止外部存取款、人工 token transfer、split/merge/conversion 和人工交易。系统会对可见的成交、仓位与 REDEEM activity 做核对，但不能声称识别所有外部现金流；违反约束会使日初权益/结算损失指标失真。

`POLYBOT_EVIDENCE_PROVIDER=auto` 在 LiteLLM 模式使用 GDELT Context 2.0 的近 72 小时同句 snippet，因此不需要第二个搜索 key。只有 snippet 非空且查询词确实出现在返回的 matching sentence 中才保留可计数 URL；market resolution source 与 title-only lead 不计数。不足两个独立可注册发布域名时不会交易。GDELT 的 429、5xx、断连和畸形响应会缓存退避并关闭证据门。若第三方 endpoint 只支持普通 Chat Completions 而不支持原生 `json_schema`，provider 只对 LiteLLM 明确的 400/422 capability mismatch 回退一次并缓存该模型能力，随后仍用本地 schema 严格校验；鉴权、限流、超时、内容策略和网络错误不重试。

## 5. Vercel

项目 Root Directory 设为 `apps/web`，唯一必需变量：

```dotenv
NEXT_PUBLIC_API_BASE_URL=https://your-api.zeabur.app
```

当前控制台是运维原型：管理员令牌只保存在当前页面 React state，但仍由浏览器直接发送 Bearer token。正式资金前应把 API 放在 SSO/VPN/访问代理后，或实现 Supabase Auth + MFA 的服务端会话代理；不要把管理员令牌写入 Vercel 环境变量或 localStorage。

## 6. 健康检查与控制

- `GET /livez`：进程 liveness。
- `GET /health`：Supabase readiness，失败返回 503。
- `GET /v1/status`：需要管理员 Bearer token。
- `POST /v1/cycles/run`：只允许 paper/shadow；真实资金周期只能由 leased worker 执行。
- `POST /v1/control/arm`：canary/live，最长 15 分钟。
- `POST /v1/control/disarm`：原子写 kill switch 与持久化撤单锁存。无 signer API 会返回 `cancellation_pending_worker=true`，由 worker watcher 撤单并确认；同进程 signer 能立即验证和确认。撤单无法验证时返回 503；锁存清除前 `/arm` 返回 409。

示例：

```bash
curl -X POST https://your-api.example/v1/control/arm \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"canary","minutes":5}'
```

## 7. 上线顺序

1. 用 `polybot generate-secrets` 生成 Fernet/admin secret；不要把输出提交到 Git。
2. 在只临时设置 `POLYMARKET_PRIVATE_KEY` 的安全终端运行 `polybot wallet-info`。官方 SDK 会派生凭据，并在需要时部署默认 Deposit Wallet；该命令不是纯离线查询。
3. 在首次 worker 启动前，把唯一一笔启动资金转到输出的 `trading_wallet`，再运行 `polybot wallet-bootstrap --confirm-standard-allowances`。这个显式一次性命令调用官方 SDK 建立标准交易授权、等待交易完成并检查 CLOB allowance；不要把 approval 隐式塞进每次下单。
4. 钱包必须是全新专用钱包：worker 首次启动后不再入金/出金，不做人工交易、token transfer、split/merge/conversion。冷启动会从 epoch 0 重放成交并核对所有 dust 仓位；不一致会停机。若必须追加资金，先 disarm、清零挂单并建立新的人工审计基线；当前版本不提供自动安全重基线。
5. 本地单测、lint、构建全部通过。
6. 至少 14 天 paper，使用真实实时行情；修正成本、深度、延迟和数据缺口。
7. 至少 7 天 shadow；逐笔比较预期成交与真实可成交路径。
8. 完成冻结样本外与 walk-forward 报告，重点看 Brier、log loss、净 EV、最大回撤，不以胜率单指标决策。
9. 人工审阅未解决 `unknown/signed/submitting` 订单；这些状态会 fail closed。
10. canary 仅用可完全损失的小额，单笔硬上限 5 pUSD；首次真实订单人工旁观并核对订单、成交、仓位和取消。
11. 未达到 [策略验证阶段门](STRATEGY_VALIDATION.md) 前，不切换 `live`，不提高限额。

自动赎回默认关闭。代码会把 `REDEEM` 写入 activity ledger、关闭 condition 成本并刷新权益，但启用前仍必须用小额单独验证 relayer/EOA 交易、wait 结果、审计和失败恢复。

真实资金 arm 最长 15 分钟。`0005` 禁止直接续期一个已经过期、尚未完成撤单确认的 arm；worker 用数据库时钟原子转换为 kill/cancellation latch，验证零挂单并完成 CAS acknowledgement 后才允许再次 arm。真实订单同时使用不晚于 `armed_until` 的 GTD 到期时间；剩余授权少于 3.5 分钟时拒绝新签名，因此 worker 离线时交易所仍有第二道到期保护。该安全契约允许进程持续扫描和对账，但不声称支持无人值守、永久授权的真实下单。

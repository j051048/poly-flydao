# 部署手册：Vercel + Zeabur + Supabase

生产拓扑是一个 Vercel 前端、一个公开 Zeabur API、一个私有 Zeabur Worker，以及一个 Supabase 项目。API 与 Worker 使用同一镜像，但秘密权限完全不同。

## 1. 生成内部密钥

在可信本地终端运行：

```powershell
Set-Location backend
uv sync --frozen
uv run --frozen polybot generate-secrets
```

输出不会自动写文件。分别保存：

- API：`POLYBOT_CREDENTIAL_PUBLIC_KEY_PEM`、`POLYBOT_CREDENTIAL_FINGERPRINT_KEY`
- Worker：`POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM`、`POLYBOT_SIGNED_PAYLOAD_KEY`
- Worker 实盘硬锁确认值：三个 `*_ACK`

公钥和私钥必须来自同一次生成。私钥、签名 payload key、Supabase service role 不能进入 Vercel、Git、日志或公开 API。

## 2. Supabase

1. 创建 Supabase 项目。
2. 确保 Auth access token 使用后端支持的非对称签名算法：RS256 或 ES256，并有可访问的 JWKS；本项目拒绝 HS256 共享密钥 token。
3. 按顺序执行：

```text
backend/supabase/migrations/0001_initial.sql
...
backend/supabase/migrations/0014_custom_ai_credential.sql
```

4. 在 Auth 中配置站点 URL、Vercel 登录回调 URL和邮件验证回调：

```text
https://YOUR_VERCEL_DOMAIN/auth/callback
```

5. 为真实资金账户启用 TOTP MFA。前端写入/撤销 AI Key、导入/撤销钱包、启用自动实盘和 arm 都要求 AAL2。

迁移启用了 RLS/FORCE RLS。浏览器只有 owner-read 元数据；密文写入、任务领取、钱包生命周期和订单状态变更只能通过受限 RPC。

## 3. Zeabur Control API

从 GitHub 仓库创建服务，Root Directory 设置为 `backend`，公开 HTTPS 域名。环境变量：

最不容易配错的方法是逐项复制
[`backend/deploy/api.env.example`](../backend/deploy/api.env.example)，不要复用 Worker
的变量清单。健康检查 Path 设置为 `/health`。

```dotenv
SERVICE_ROLE=api
POLYBOT_MODE=paper
POLYBOT_DASHBOARD_ORIGINS=https://YOUR_VERCEL_DOMAIN

SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...

POLYBOT_CREDENTIAL_PUBLIC_KEY_PEM=-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----
POLYBOT_CREDENTIAL_KEY_VERSION=1
POLYBOT_CREDENTIAL_FINGERPRINT_KEY=...

# 多租户 SaaS 建议设置；API 与 Worker 必须完全一致
POLYBOT_CUSTOM_AI_ALLOWED_HOSTS=api.trusted-relay.example,*.ai-gateway.example
```

可选显式 JWT 配置：

```dotenv
POLYBOT_SUPABASE_JWT_ISSUER=https://PROJECT_REF.supabase.co/auth/v1
POLYBOT_SUPABASE_JWT_AUDIENCE=authenticated
POLYBOT_SUPABASE_JWKS_URL=https://PROJECT_REF.supabase.co/auth/v1/.well-known/jwks.json
```

API 服务禁止出现：

```text
POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM
POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON
POLYBOT_SIGNED_PAYLOAD_KEY
POLYMARKET_PRIVATE_KEY
OPENAI_API_KEY
LITELLM_API_KEY
```

健康检查：

```text
GET /livez
GET /health
```

`/health` 必须返回 200。生产缺少 Supabase 时控制面会 fail closed，不会回退到内存状态。

## 4. Zeabur Tenant Worker

从同一仓库再创建一个服务，Root Directory 同样为 `backend`。不要绑定公网域名，初期保持单副本：

逐项复制 [`backend/deploy/worker.env.example`](../backend/deploy/worker.env.example)。
Worker 会在 `PORT` 上提供固定、无账户数据的 `/livez`，供 Zeabur 判断常驻进程是否存活；
健康检查 Path 设置为 `/livez`，但仍不要为该服务绑定公网域名。

```dotenv
SERVICE_ROLE=worker
POLYBOT_COMPONENT=worker
POLYBOT_WORKER_EXECUTION_MODEL=tenant_queue
POLYBOT_MODE=canary

SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...

POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM=-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----
POLYBOT_CREDENTIAL_KEY_VERSION=1
POLYBOT_SIGNED_PAYLOAD_KEY=...
POLYBOT_PAYLOAD_KEY_VERSION=1

POLYBOT_TENANT_WORKER_MAX_CONCURRENCY=4
POLYBOT_TENANT_JOB_LEASE_SECONDS=60
POLYBOT_TENANT_JOB_POLL_SECONDS=1

# 与 Control API 保持一致
POLYBOT_CUSTOM_AI_ALLOWED_HOSTS=api.trusted-relay.example,*.ai-gateway.example

POLYBOT_LIVE_ACK=I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST
POLYBOT_BETA_SDK_ACK=I_ACCEPT_BETA_SDK_CANARY_ONLY
POLYBOT_DEDICATED_WALLET_ACK=I_CONFIRM_DEDICATED_WALLET_NO_EXTERNAL_FLOWS
POLYBOT_AUTO_REDEEM_RESOLVED=false
```

不要配置全局 AI key 或全局钱包私钥。Tenant Worker 会根据任务中的账户引用解密该账户自己的凭证，并创建一次性运行时。

Worker 与任务都有数据库 fencing lease。即使滚动部署短暂出现两个副本，旧副本也不能通过最终订单闸门；但仍建议单副本，确认稳定后再评估水平扩展。

## 5. Vercel

Root Directory 设置为 `apps/web`。只配置浏览器可公开值：

```dotenv
NEXT_PUBLIC_API_BASE_URL=https://YOUR_ZEABUR_API_DOMAIN
NEXT_PUBLIC_SUPABASE_URL=https://PROJECT_REF.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=...
```

也可使用旧的 `NEXT_PUBLIC_SUPABASE_ANON_KEY`，但不要同时误放 `SUPABASE_SERVICE_ROLE_KEY`。

生产构建漏配 API 或 Supabase 时仍能成功构建，但界面会禁用认证/操作并 fail closed；不会把 Bearer JWT、邮箱或密码发送到 localhost。

## 6. 首个租户的启用顺序

1. 注册并完成邮箱验证。
2. 登录，在“系统配置”绑定并验证 TOTP，使当前会话达到 AAL2。
3. 输入第三方 AI provider、模型 ID 和 API key。使用 OpenAI 兼容中转站时选择“自定义”，Base URL 填写到 API 版本根路径，例如 `https://relay.example.com/v1`。密钥只提交一次，之后不回显。
4. 导入一个全新、低余额、只给机器人使用的 EVM 私钥。绝不能使用主钱包。
5. 等待 Worker 将钱包从 `pending_verification` 变为 `active`，前端会显示入金地址、chain ID 和 collateral token。
6. 按显示的网络与 collateral 资产转入一笔可完全承受损失的小额启动资金。
7. 先选择 `paper`，启用自动周期并观察任务、预测、风险拒绝和账本。
8. 完成策略验证门槛后切换 `canary`。每次真实运行还需要在控制台以 AAL2 短时 arm；Worker 检测到资金后才执行已明确授权的标准 trading approvals。
9. 首笔真实订单旁观核对：限价、post-only、GTD 到期、成交、撤单、持仓和 Supabase 账本。
10. 任何异常立即 disarm。系统会持久化 kill/cancellation latch，并要求 Worker 验证零开放订单后才能重新 arm。

自动周期不等于永久授权。Canary/live 只有在短时 arm、任务和账户租约、配置/风险快照、钱包/凭证状态、对账、余额、allowance、地理限制和盘口新鲜度全部有效时才可能提交订单。

### 自定义 AI 中转站的边界

- 仅支持 OpenAI Chat Completions 兼容接口和公开 HTTPS 域名，不支持 HTTP、localhost、私网/链路本地/云元数据地址、URL 内账号密码、查询参数或重定向。
- API 保存配置时与 Worker 解密 Key 前都会解析 DNS；真正发送 HTTP 前还会再次检查全部 DNS 地址。任何一个地址不是公网地址都会 fail closed。
- `POLYBOT_CUSTOM_AI_ALLOWED_HOSTS` 为空时允许任意通过上述检查的公网域名；商业多租户部署应配置可信中转站白名单。支持逗号分隔的精确域名和 `*.example.com`。
- 中转站能够读取发给模型的市场证据和 API Key。只使用可信服务、专用低额度 Key，并在 Zeabur/云网络层额外禁止访问私网和 metadata 网段。应用层 DNS 检查不能替代基础设施 egress firewall。
- 中转站若不支持 JSON Schema，系统只会在明确的“不支持 structured output”错误下退回普通 JSON，并继续使用 Pydantic 严格校验；认证、限流、超时和内容策略错误不会自动重复付费请求。

## 7. 部署前验证

```powershell
Set-Location backend
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest
uv run --frozen polybot pair-replay --input examples/pair_replay_sample.json

Set-Location ..\apps\web
npm ci
npm test
npm run build
```

还必须在临时 Supabase 项目实际执行一次完整 migration reset；纯字符串单测不能替代 PostgreSQL 解析、权限和事务验证。

## 8. P2 上线门槛

P2 配对策略当前 `research_only=true` 且 `execution_enabled=false`。不能通过环境变量直接解除。只有满足 [策略验证门槛](STRATEGY_VALIDATION.md)，完成单独代码审查和小额 canary 后，才能设计后续 live migration；目前交付不声明可持续优势或保证盈利。

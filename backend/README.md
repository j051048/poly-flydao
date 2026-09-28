# Polybot backend

`backend/` 可直接作为 Zeabur 的构建根目录。个人版推荐只创建一个 `SERVICE_ROLE=personal` 服务：同一个 Uvicorn 进程提供控制 API，并由应用 lifespan 管理一个单账户 Worker。部署必须保持 `WEB_CONCURRENCY=1`，避免在同一服务中创建多个签名运行时。

## 个人版配置

复制 [`deploy/personal.env.example`](deploy/personal.env.example) 到 Zeabur 环境变量。关键规则：

- `POLYBOT_ACCOUNT_ID` 必须是唯一 owner 的 Supabase Auth User UUID，不是邮箱、项目 ref 或钱包地址；
- `POLYBOT_AI_API_KEY`、`POLYBOT_AI_BASE_URL`、`POLYBOT_AI_MODEL` 和 `POLYMARKET_PRIVATE_KEY` 只放 Zeabur；
- `PORT` 直接填写 `8080`，不要填写 `${WEB_PORT}`；
- `POLYBOT_DASHBOARD_ORIGINS` 填 Vercel 的完整 HTTPS origin，多个域名用英文逗号分隔且不带路径；
- 默认 `POLYBOT_MODE=paper`。只有显式设置 `POLYBOT_PERSONAL_LIVE_ENABLED=true` 且模式为 `canary` 或 `live`，才允许构建真实资金运行时。

个人版不需要以下多租户密钥：

```text
POLYBOT_CREDENTIAL_PUBLIC_KEY_PEM
POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM
POLYBOT_CREDENTIAL_FINGERPRINT_KEY
POLYBOT_SIGNED_PAYLOAD_KEY
```

也不要把任何 `NEXT_PUBLIC_*`、`PASSWORD` 或 `POLYBOT_ADMIN_TOKEN` 放在 Zeabur。

## Supabase

按文件名顺序应用 `supabase/migrations/` 中全部迁移，目前要求 schema 版本 **22**。已有 0020 的部署需依次应用 `0021_reconcile_activity_scope.sql` 和 `0022_order_intent_commitment.sql`。前者将外部账户活动与机器人账本分开并保留审计原文；后者保存订单允许的最大支出与真实市场标识。缺少最新迁移时 Worker 会停止执行。

Supabase Auth 只保留一个 owner。先在 Supabase Dashboard 创建/确认该用户，复制 User UUID 到 `POLYBOT_ACCOUNT_ID`，再关闭公开注册。后端会拒绝其他有效用户访问个人运行时。

## 健康检查

- `GET /livez`：进程已启动；
- `GET /health`：API 可以访问 Supabase 控制面；
- `GET /worker-health`：内嵌 Worker 已完成初始化并持有健康租约。

Zeabur 平台 Health Check Path 推荐 `/livez`，避免 Supabase 短暂故障触发容器重启；控制台仍以 `/health` 和 `/worker-health` 判断是否可以交易。服务应为一个副本。

从 Canary/Live 降级到 Paper/Shadow 时先保留原钱包私钥。新 Worker 会在有效租约下原子停机、撤销交易所挂单并验证零开放单；清场失败时 `/worker-health` 保持 503，模拟周期不会在不确定状态下启动。

模式和风控档位保存在数据库，由 Worker 在周期边界读取。界面分别显示已请求和实际生效的配置；撤单和版本确认完成前仍显示待生效。风控金额和比例取数据库请求与启动时部署上限的较小值，最小净优势取较大值；Canary 另有不可放宽的 5 美元上限。修改风控需要在设置页完成 TOTP 验证（AAL2）。配置读取失败时阻止新下单。

个人部署同时运行市场结算扫描器，为绩效与预测校准补充结果。手动周期使用 `POST /v1/personal/cycles/run` 返回的 request ID 查询 `GET /v1/personal/cycles/{request_id}`；该跟踪记录属于当前进程，重启后需重新读取实际运行和订单状态。权益历史按 `scope=paper|shadow|real` 分开，真实范围合并 Canary 与 Live。

## 本地验证

```powershell
Set-Location backend
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest
uv run --frozen polybot pair-replay --input examples/pair_replay_sample.json
```

无需连接 Supabase 的迁移验证（在仓库根目录执行）：

```powershell
npm install --prefix .audit-migrations --ignore-scripts @electric-sql/pglite@0.5.8
node backend/scripts/verify_migrations.mjs .audit-migrations
```

脚本执行完整迁移链，并检查升级、金额约束和 RLS；使用最小 Supabase Auth 夹具，不能替代部署环境中的迁移与 PostgREST 验证。

## 高级：旧多租户部署

仓库仍保留 `SERVICE_ROLE=api` 与 `SERVICE_ROLE=worker`、tenant queue、账户凭证加密和 API/Worker 密钥分离，模板位于 [`deploy/api.env.example`](deploy/api.env.example) 与 [`deploy/worker.env.example`](deploy/worker.env.example)。这条路径适合对外 SaaS，不是个人版的推荐配置。不要把两套模板混在同一个服务里。

P2 的 YES/NO 配对微结构策略仍是 research/paper 流水线，不能把历史传闻当成可复制收益证明，也不会因启用个人 Live 自动解除研究闸门。

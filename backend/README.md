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

按文件名顺序应用 `supabase/migrations/` 中全部迁移，包括个人模式的最新 `0016` 迁移。个人模式仍依赖 Supabase 保存订单、运行控制、风险快照、租约、Paper 状态与审计记录；缺少数据库或最新迁移时会 fail closed。

Supabase Auth 只保留一个 owner。先在 Supabase Dashboard 创建/确认该用户，复制 User UUID 到 `POLYBOT_ACCOUNT_ID`，再关闭公开注册。后端会拒绝其他有效用户访问个人运行时。

## 健康检查

- `GET /livez`：进程已启动；
- `GET /health`：API 可以访问 Supabase 控制面；
- `GET /worker-health`：内嵌 Worker 已完成初始化并持有健康租约。

Zeabur 平台 Health Check Path 推荐 `/livez`，避免 Supabase 短暂故障触发容器重启；控制台仍以 `/health` 和 `/worker-health` 判断是否可以交易。服务应为一个副本。

从 Canary/Live 降级到 Paper/Shadow 时先保留原钱包私钥。新 Worker 会在有效租约下原子停机、撤销交易所挂单并验证零开放单；清场失败时 `/worker-health` 保持 503，模拟周期不会在不确定状态下启动。

## 本地验证

```powershell
Set-Location backend
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest
uv run --frozen polybot pair-replay --input examples/pair_replay_sample.json
```

## 高级：旧多租户部署

仓库仍保留 `SERVICE_ROLE=api` 与 `SERVICE_ROLE=worker`、tenant queue、账户凭证加密和 API/Worker 密钥分离，模板位于 [`deploy/api.env.example`](deploy/api.env.example) 与 [`deploy/worker.env.example`](deploy/worker.env.example)。这条路径适合对外 SaaS，不是个人版的推荐配置。不要把两套模板混在同一个服务里。

P2 的 YES/NO 配对微结构策略仍是 research/paper 流水线，不能把历史传闻当成可复制收益证明，也不会因启用个人 Live 自动解除研究闸门。

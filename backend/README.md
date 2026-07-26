# Polybot backend

这是 Polybot 的独立后端构建根目录，包含 Zeabur API/worker 镜像、Python 源码、测试、Supabase migrations 和冻结依赖。

Zeabur 中为 API 与 worker 各创建一个服务，并把两者的 Root Directory 都设为 `backend`。两个服务使用同一镜像，通过 `SERVICE_ROLE=api` 或 `SERVICE_ROLE=worker` 选择进程；`zeabur.json` 的安全默认值是 API，因此 worker 服务必须显式覆盖为 `SERVICE_ROLE=worker` 并保持单副本。

部署前，在 Supabase SQL Editor 按文件名顺序应用
`supabase/migrations/0001_initial.sql` 到 `0006_order_expiry.sql`，并创建一个 Auth 用户；其 UUID 用作两个服务共同的 `POLYBOT_ACCOUNT_ID`。

| Zeabur 服务 | 网络与健康检查 | 必需的服务变量 | 必须隔离的变量 |
| --- | --- | --- | --- |
| API | 可绑定公开域名；`GET /livez` 与 `GET /health` | `SERVICE_ROLE=api`、`POLYBOT_ACCOUNT_ID`、`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`、`POLYBOT_ADMIN_TOKEN`、`POLYBOT_DASHBOARD_ORIGINS` 及对应模式的确认值 | 不得配置钱包私钥、deposit wallet 或 `POLYBOT_SIGNED_PAYLOAD_KEY` |
| Worker | 不绑定公开域名；单副本 | `SERVICE_ROLE=worker`、同一 account/Supabase 配置、AI provider/endpoint/model/key、钱包私钥、`POLYBOT_SIGNED_PAYLOAD_KEY` 及对应模式的确认值 | 私钥、AI key 和 Fernet key 只放在此服务 |

API 与 worker 的完整变量示例见仓库根目录的 [部署手册](../docs/DEPLOYMENT.md)。先以 `paper` 验证；`canary/live` 还需要文档规定的短时数据库 arm，不能仅凭部署变量开始交易。

本地验证：

```powershell
Copy-Item .env.example .env
uv sync --frozen --extra dev --no-editable
uv run --no-editable ruff check .
uv run --no-editable python -m pytest
uv run --no-editable polybot config-check
```

首次钱包准备：

```powershell
$env:POLYMARKET_PRIVATE_KEY="<dedicated-wallet-private-key>"
uv run --no-editable polybot wallet-info
# 向输出的 trading_wallet 转入唯一一笔启动资金后：
uv run --no-editable polybot wallet-bootstrap --confirm-standard-allowances
```

真实资金 worker 除 AI key 和钱包私钥外，还必须获得 Supabase URL/service-role key、对应的 Auth user UUID、AI endpoint/model、持久 Fernet key 和显式风险确认值。这些不能从两个 key 安全推断；`polybot generate-secrets` 会生成内部 secret/确认值，外部基础设施值仍需在 Zeabur 中配置。

完整的安全边界、Supabase 迁移与上线阶段门见部署手册。默认 `paper` + `mock` 不会提交真实订单；切换真实资金前必须完成文档中的 paper、shadow 和小额 canary 验收。

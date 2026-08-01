# Polybot backend

`backend/` 是可独立部署到 Zeabur 的构建根目录，包含 FastAPI 控制面、常驻多租户 Worker、Supabase migrations、策略研究组件和测试。

生产环境必须从同一镜像创建两个服务：

| 服务 | `SERVICE_ROLE` | 是否公开 | 持有的密钥 |
|---|---|---|---|
| Control API | `api` | 是 | Supabase service role、凭证 RSA 公钥、指纹 HMAC key |
| Tenant Worker | `worker` | 否 | Supabase service role、凭证 RSA 私钥、签名 payload key |

API 不持有钱包私钥或 AI key。用户登录后一次性提交这些值；API 只用公钥生成账户绑定的密文。Worker 按任务租约取出并瞬时解密，任务结束后销毁运行时。浏览器后续请求只携带 Supabase Bearer JWT，不再通过 `X-*` 请求头传秘密。

## 本地验证

```powershell
Set-Location backend
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest
uv run --frozen --extra dev polybot pair-replay --input examples/pair_replay_sample.json
```

默认配置是 `paper + mock`，不会发送真实订单。本地无 Supabase 的控制面必须显式设置 `POLYBOT_ALLOW_INMEMORY_CONTROL=true`；生产环境缺少持久控制面会直接返回 503。

## Supabase

按文件名顺序应用 `supabase/migrations/0001_initial.sql` 至 `0015_product_operations.sql`。`0007` 引入多租户凭证、钱包生命周期、风险快照和任务队列；`0008`–`0009` 是默认关闭的配对微结构研究账本；`0010` 是真实订单提交前的原子授权闸门；`0011`–`0012` 加固自定义 AI 中转站；`0013`–`0014` 保存任务摘要并接通自定义中转站；`0015` 增加持久 Paper 账户、Worker 心跳、钱包就绪闸门、AI 诊断/预算、通知和预测校准闭环。

GitHub CI 使用固定版本的官方 Supabase CLI，在全新本地数据库应用所有迁移并执行一次
完整 reset。新增或修改 SQL 后，`Supabase / migration-reset` 是必需检查，不要只依赖
`tests/test_schema.py` 的静态约束断言。

上线配置与操作顺序见 [部署手册](../docs/DEPLOYMENT.md)，安全边界见 [安全说明](../docs/SECURITY.md)。

为避免把 Worker 私钥误放进公开 API，Zeabur 两个服务分别从
[`deploy/api.env.example`](deploy/api.env.example) 和
[`deploy/worker.env.example`](deploy/worker.env.example) 复制变量。API 健康检查使用
`/health`；私有 Worker 使用只返回固定进程状态的 `/livez`，且不要绑定公网域名。

## P2 策略状态

限价 maker、分时收集 YES/NO、完整成本低于 1 的配对逻辑、方向覆盖、非原子双腿状态机、幂等成交和 L2 队列回放均已实现。它仍被硬编码为 `research_only`，Supabase 中 `execution_enabled=false`，不能作为实盘盈利证明。完成事件级数据、样本外 walk-forward、shadow 和小额 canary 门槛前，不应解除该限制。

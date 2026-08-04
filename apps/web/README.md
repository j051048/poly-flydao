# Polybot Web Console

Next.js owner console for the personal single-account backend. The browser logs in with Supabase Auth and reads sanitized runtime status; it never asks for, stores or forwards an AI API Key or EVM private key.

## Vercel environment

Copy `.env.example` and configure only:

- `NEXT_PUBLIC_API_BASE_URL`：一个 Zeabur `SERVICE_ROLE=personal` 服务的 HTTPS origin；
- `NEXT_PUBLIC_SUPABASE_URL`：Supabase project URL；
- `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`：公开 publishable key。旧项目也支持 `NEXT_PUBLIC_SUPABASE_ANON_KEY`。

不要把 Supabase service-role key、AI Key、EVM 私钥、seed phrase、CLOB credential、`PASSWORD`、admin token 或 credential RSA/fingerprint key 放入 Vercel。

Supabase Auth 回调白名单应包含：

```text
http://localhost:3000/auth/callback
https://YOUR_VERCEL_DOMAIN/auth/callback
```

在 Supabase Dashboard 创建唯一 owner 后关闭公开注册，并把该用户的 Auth UUID 配置为 Zeabur 的 `POLYBOT_ACCOUNT_ID`。个人版不要求在前端绑定 MFA，因为前端没有保存 AI/EVM 秘密的接口。

缺少 Supabase 公共变量时，生产构建仍可成功，但登录会显示“Auth 未配置”并 fail closed。`/diagnostics` 只返回安全的连通性/就绪状态，不返回任何环境变量值或秘密。

## 会话与 API 边界

Middleware 通过 `supabase.auth.getUser()` 校验并刷新会话。受保护页面调用 Zeabur 时只发送：

```text
Authorization: Bearer <Supabase JWT>
Content-Type: application/json
Idempotency-Key: <random UUID>  # 状态变更操作
```

后端还会校验 JWT `sub` 必须等于 Zeabur 的 `POLYBOT_ACCOUNT_ID`。浏览器不能选择或覆盖账户 ID。

配置中心只显示：AI provider/model/Base URL、AI 是否就绪、钱包公开地址、模式、自动周期和 Worker 状态。更换 Key、模型、Base URL 或私钥必须在 Zeabur 环境变量中完成并重新部署。

## 验证

```bash
npm ci
npm test
npm run build
```

建议分别在无 Supabase 变量和合法公开测试变量下运行生产构建，确保配置缺失只禁用认证，不会阻断 Vercel 构建。

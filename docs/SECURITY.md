# 安全模型

## 目标与非目标

系统目标是限制单个租户、单个任务或单个组件失陷后的影响，并在授权、数据或对账不确定时停止新订单。它不能消除私钥托管风险、第三方依赖风险、交易所风险、智能合约风险、模型错误、网络中断或市场亏损。

## 秘密分层

| 位置 | 允许 | 禁止 |
|---|---|---|
| Vercel | API origin、Supabase URL、publishable/anon key、用户短期会话 | service role、AI key、钱包私钥、RSA 私钥、payload key |
| Control API | Supabase service role、RSA 公钥、指纹 HMAC key | RSA 私钥、钱包私钥、AI key、签名 payload key |
| Tenant Worker | Supabase service role、RSA 私钥或轮换 keyring、签名 payload key | 公开域名、浏览器会话 |
| Supabase | 账户绑定密文、任务/风险快照、订单/成交账本 | 凭证明文、Worker 私钥 |

API 接收秘密时：

1. JWT `sub` 决定账户，不接受客户端传 account ID。
2. 请求需要 HTTPS；敏感操作需要 TOTP AAL2。
3. API 使用随机 AES-256-GCM 数据密钥加密明文，再用 3072-bit RSA-OAEP-256 包装数据密钥。
4. AAD 包含账户、秘密类型、provider、版本和随机版本号，阻止跨租户/跨用途替换。
5. 数据库保存 HMAC 指纹用于同账户冲突检测，不向浏览器暴露指纹或完整尾号。
6. 验证错误、日志与 API 响应不反射输入。

Python 字符串无法保证内存安全擦除；代码会尽力清零可变 bytearray，但生产上仍应使用短生命周期进程、最小权限和平台 secret 管理。

## 自定义 AI 出站请求

租户可以配置 OpenAI 兼容 HTTPS 中转站，但不能把 Worker 当作任意 URL 请求器：

- Runtime Profile 只保存规范化 HTTPS Base URL，数据库约束拒绝 HTTP；
- Control API 保存时解析 DNS，Worker 在解密租户 AI Key 前重新解析；
- 自定义 Provider 的 HTTP transport 在每次携带 Key 的请求前再次验证全部 A/AAAA 地址；
- 私网、loopback、link-local、CGNAT、保留、组播、未指定地址和本地域名全部拒绝；
- 禁止 URL credentials/query/fragment、环境代理和 HTTP redirect；
- 可通过 `POLYBOT_CUSTOM_AI_ALLOWED_HOSTS` 将域名进一步限制为管理员批准列表；
- 自定义 Provider 持有 Key 的 SDK client 会随单次租户 Runtime 关闭，不复用到其他租户。

DNS 验证与实际 TCP 建连之间仍存在很小的解析竞态。生产必须同时使用云平台 egress policy/firewall 阻止 RFC1918、loopback、link-local 与 metadata 网段，不能把应用层检查描述为对 DNS rebinding 的绝对证明。

## 身份与会话

- 后端只接受 Supabase 非对称 RS256/ES256 JWT，并验证签名、issuer、audience、exp、nbf/iat、UUID subject、role 和 AAL。
- JWKS 仅允许 HTTPS 或精确 loopback 开发地址，限制响应大小/密钥数量，并对未知 `kid` 做并发合并和负缓存。
- 浏览器配置会拒绝 `sb_secret_*`、可解码为 `service_role`/`supabase_admin` 的 JWT，以及生产 loopback URL。
- Next.js middleware 在重定向时保留刷新后的 Supabase cookie；受保护页面在 Auth 未配置或不可用时 fail closed。
- URL 回跳只允许规范化后的站内绝对路径，拒绝 `//`、反斜线和编码斜线。

## 多租户数据库边界

Supabase service role 会绕过 RLS，因此仅靠 policy 不够。系统同时使用：

- API repository 的每个写操作显式携带 JWT-derived account ID；
- Worker 的 `SupabaseStore` 和 `SupabasePairExecutionStore` 创建后永久绑定一个 account ID；
- 账户复合外键，防止配置引用别人的凭证、钱包或风险策略；
- FORCE RLS 与受限表权限；
- `SECURITY DEFINER SET search_path=''` RPC；
- 默认撤销 `public/anon/authenticated` EXECUTE，只授予 service role；
- audit 与 pair inventory event append-only。

## 任务和订单 fencing

真实订单需通过两层租约：

- cycle job：`claimed_by + fencing_token + lease_expires_at`
- account worker：`owner_id + fencing_token + expires_at`

订单签名后先加密持久化。进入 `submitting` 的同一 SQL 事务再次检查：

- 两层租约仍有效；
- job 为 running，任务风险版本未变化；
- runtime control 的版本、模式、arm、到期、kill/cancellation 状态；
- runtime profile 版本及 AI/钱包/风险引用；
- 风险快照仍 active；
- 钱包、AI credential、signer credential 仍 active；
- 订单仍是该账户、该模式、该 fencing token 的 signed 状态。

数据库事务不能与外部 CLOB POST 做分布式原子提交，因此 SQL 后仍有不可消除的极短网络竞态。Broker 会在 POST 前再次同步检查，使用 GTD 到期、模糊结果不盲重发、cancel-all、持久 unresolved 状态和对账来降低影响；这不是“绝对不会出现孤儿订单”的保证。

## 钱包生命周期

- 只允许导入全新低余额专用钱包，服务端生成钱包默认关闭。
- 导入请求明确确认标准 allowance 权限；Worker 先派生公开地址，不在未入金时耗尽重试。
- 资金到位、任务租约和短时 arm 有效时才初始化并复核 allowance。
- 撤销先停止新任务、cancel-all，并从交易所复核开放订单为零；无法验证时保持 `revocation_pending`。
- 钱包私钥永不回显。前端输入仅在 React 内存短暂存在，提交后清空，并使用 `autocomplete=off` 尽量避免密码管理器保存，但浏览器扩展仍可能读取页面内容。

## 运行响应

以下情况停止新订单并尽力撤单：

- disarm、arm 到期或 control version 变化；
- job/account lease 丢失；
- profile、risk、wallet 或 credential 变化；
- Supabase、余额、allowance、盘口或对账不可用；
- 日亏损/回撤硬阈值；
- 地理限制拒绝；
- CLOB 响应模糊、成交账本不完整或存在 unresolved order。

所有真实资金部署仍需要外部监控、异常告警、数据库备份、密钥轮换、撤单演练和人工 runbook。

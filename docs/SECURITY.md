# 安全模型：个人单账户部署

## 目标与明确取舍

个人模式优先降低部署和使用门槛：AI Key 与 EVM 私钥直接由 Zeabur Secret 环境变量注入，同一个 `personal` 进程同时运行 API 和 Worker。这样不再需要浏览器录入秘密、TOTP、RSA credential envelope、fingerprint key 或第二个私有 Worker 服务。

代价也必须说清楚：Zeabur personal service 同时持有 Supabase service-role key、AI Key 和钱包私钥。若该容器、Zeabur 账户或部署供应链被攻破，攻击者可能取得全部权限。这是个人自托管接受的单一信任边界，不具备原两服务多租户架构的最小爆炸半径。

系统不能消除私钥托管、第三方依赖、智能合约、模型错误、网络中断、Polymarket/CLOB 故障、地理合规或市场亏损风险。

## 秘密放置

| 位置 | 允许 | 禁止 |
|---|---|---|
| Vercel | Zeabur API origin、Supabase URL、publishable/anon key、短期用户会话 | service role、AI Key、EVM 私钥、助记词、CLOB credential、admin token |
| Zeabur personal | Supabase service role、AI Key、专用 EVM 私钥、允许的 AI hostname | 主钱包/助记词、任何 `NEXT_PUBLIC_*`、共享密码 |
| Supabase | 公开钱包地址、配置绑定、租约、风险/订单/成交/审计状态 | AI Key 明文、EVM 私钥明文 |
| Git/日志/聊天/截图 | 脱敏示例与公开地址 | 任何真实秘密 |

Zeabur 环境变量应标记为私有，限制项目成员，并开启平台账户 MFA。任何曾出现在截图、聊天、构建日志或 Git 历史中的 key 都应立即轮换。

个人模式可从高熵 EVM 私钥派生内部 signed-payload 加密材料，因此无需额外配置 `POLYBOT_SIGNED_PAYLOAD_KEY`。更换钱包会改变派生材料；轮换前必须切回 Paper、停止服务、确认没有开放或 unresolved 订单，再重新部署。不要在存在待对账订单时直接替换私钥。

## Owner 登录

- 后端只接受 Supabase 支持的非对称 JWT，并校验签名、issuer、audience、有效期和 UUID subject；
- 个人模式进一步要求 JWT `sub` 精确等于 `POLYBOT_ACCOUNT_ID`；
- Supabase 应关闭公开注册，只保留唯一 owner；
- Vercel middleware 恢复/刷新会话，未登录用户不能进入受保护控制台；
- 浏览器配置拒绝 service-role/secret key 和生产 loopback URL。

不要求“保存秘密”的 AAL2，是因为浏览器没有保存秘密的接口，不代表应取消 Supabase 登录或把 API 公开成共享 admin token。

## 自定义 AI 出站请求

OpenAI 兼容中转站能够读取发给模型的市场证据和 AI Key。只使用可信、专用、低额度 Key；个人模式会从 `POLYBOT_AI_BASE_URL` 自动提取并锁定唯一允许的 hostname。

应用继续执行以下限制：

- 仅允许公开 HTTPS Base URL；
- 拒绝 URL credentials、query/fragment、HTTP redirect 和环境代理；
- 拒绝 localhost、RFC1918、link-local、CGNAT、保留、组播、云 metadata 等地址；
- 在配置和携带 Key 发请求前重新解析 DNS；
- 限制超时、响应大小，并对模型输出做结构化校验。

DNS 检查与实际 TCP 建连之间仍存在解析竞态。高价值部署还应使用云 egress firewall 阻止私网、loopback、link-local 和 metadata 网段。

## 真实资金授权

默认配置是：

```dotenv
POLYBOT_MODE=paper
POLYBOT_PERSONAL_LIVE_ENABLED=false
```

Canary/Live 必须同时显式设置 `POLYBOT_PERSONAL_LIVE_ENABLED=true` 和对应模式，并重新部署。这个环境变量是部署级 owner 授权；AI 和浏览器不能修改它。

即使开启，提交前仍保留：

- 单副本 Worker lease 与 fencing token；
- runtime control/config binding 版本与取消状态；
- 钱包地址、余额和 allowance 新鲜度；
- unresolved order 与对账健康检查；
- 单笔、事件、桶、总敞口、日亏损和最大回撤限制；
- 官方 geoblock 与盘口新鲜度；
- 签名订单的幂等状态转换。

真实模式只使用专用、低余额钱包。不要配置主钱包私钥或助记词。启动资金应是可以完全损失的小额资金；先用 Canary 旁观首单与撤单流程。

## 单进程与可用性

`SERVICE_ROLE=personal` 必须使用一个 Zeabur 副本和 `WEB_CONCURRENCY=1`。数据库租约仍是最终闸门，但随意扩容会增加签名、轮询和恢复路径复杂度。滚动部署、数据库不可用、租约丢失、对账失败、模式/配置变化或 geoblock 拒绝时，应停止新订单并尽力撤单。

外部 CLOB POST 与数据库事务无法形成真正的分布式原子提交。模糊网络结果必须记录为 unresolved 并对账，不能盲目重发；系统不能承诺绝对不会出现孤儿订单。

## 运行响应清单

发生异常时：

1. 在 Zeabur 设置 `POLYBOT_PERSONAL_LIVE_ENABLED=false`、`POLYBOT_MODE=paper` 并重新部署；
2. 从 Polymarket 官方界面确认并撤销开放订单；
3. 检查 Supabase unresolved orders、持仓、成交和 runtime control；
4. 轮换可能泄漏的 AI Key、service-role key 和钱包；
5. 保存 request ID 与脱敏日志，复盘后再恢复 Canary。

外部存活监控与异常告警已内置：worker 连续未就绪超过 `POLYBOT_READINESS_ALERT_SECONDS`
会通过 `POLYBOT_NOTIFY_WEBHOOK_URL` 推送阻塞门控与修复建议（同一轮故障最多每 6 小时
重复一次，恢复时再推送一条），周期失败与安全熔断仍照常推送；只增历史表
（`ai_usage_ledger` / `equity_history` / `snapshots`）由迁移 0020 的有界裁剪按天清理。
仍需为真实资金配置数据库备份/恢复、密钥轮换和撤单演练。

## 高级/旧多租户安全边界

对外 SaaS 应恢复公开 Control API 与私有 Tenant Worker 分离、RSA-OAEP + AES-GCM 账户凭证加密、fingerprint HMAC、tenant queue 和 AAL2 敏感操作。个人模式的简化不应直接复制到多人托管产品。

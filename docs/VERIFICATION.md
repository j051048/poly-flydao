# 交付验证记录

验证日期：2026-07-26。

## 已通过

| 检查 | 结果 |
|---|---|
| Python lint | `ruff check .` 通过 |
| Python 测试 | `114 passed` |
| Web 生产构建 | Next.js `next build` 通过 |
| Python 生产依赖审计 | `pip-audit`：`No known vulnerabilities found` |
| Web 生产依赖审计 | `npm audit --audit-level=high`：0 个漏洞 |
| Python 分发制品 | wheel 与 sdist 构建成功 |
| 制品内容 | wheel 包含 GDELT Context collector、内置 Public Suffix 去重、429/网络故障退避、异步 trade ID 对账和最新风控代码 |
| Polymarket 公共 API | 官方 SDK 0.2.0 成功读取当前市场与 outcome order book；当前 keyset 服务实测必须按数值 JSON 字段 `liquidityNum` 排序 |
| Polymarket SDK contract | 下单、异步成交 ID、余额、订单/成交/activity/仓位与 WS 方法形状测试通过 |
| Paper 端到端 | 实时公开行情扫描 3 个高流动性市场，生成 3 个 mock forecast，因无正净价值候选而 0 intent/0 order |
| GDELT Context 公共 API | 已核验 URL、title、sentence、context 响应形状；最终复测遇到远端断连时返回 0 个可计数来源并进入退避，没有放行交易 |
| 默认配置 | `paper` + `mock` 配置检查通过，真实资金保持锁定 |

生成的 Python 制品：

```text
dist/polybot-0.1.0-py3-none-any.whl
dist/polybot-0.1.0.tar.gz
```

唯一测试告警来自 Starlette 对旧 `httpx` TestClient 兼容层的弃用提示，不影响运行时网络客户端，也没有测试失败。

## 尚未验证，因此不能声称完成

- 没有用户的 Supabase 项目权限，未在远端实际应用迁移；
- 没有使用或读取真实钱包私钥，未部署/充值 Deposit Wallet；
- 没有第三方模型 key，未对用户所选模型做 schema、延迟和限流验收；
- 当前环境没有 Docker CLI，未执行本机容器构建；Python 和 Next.js 制品已分别构建；
- 没有提交真实订单，没有做小额 canary、成交/撤单/重启恢复和赎回验收；
- 合成 JSONL 回测只验证计算链路，不构成策略收益证据。

## 上线验收标准

远端环境必须按以下顺序推进：

1. 应用 Supabase 迁移并验证 RLS、RPC、lease 和 runtime control；
2. 用真实公开行情运行至少 14 天 paper；
3. 运行至少 7 天 shadow，并逐笔核对理论价格与真实可成交价格；
4. 对冻结的样本外数据计算 Brier、log loss、费用和滑点后的净 EV、最大回撤；
5. 仅在官方 geoblock 允许的部署地点，用可完全损失的小额执行 canary；
6. 人工验证首次签名、成交、部分成交、撤单、重启对账和紧急停止；
7. 所有阶段门通过后才考虑 `live`，并继续使用硬限额与短时 arm。

“进程持续运行”不等于“持续获利”。worker 可以常驻，但真实订单只在最长 15 分钟的人工 arm 窗口内允许；系统会在证据不足、数据陈旧、AI/数据库/对账异常、地域限制、日损或回撤越限时停止新单并保持熔断。

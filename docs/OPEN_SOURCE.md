# 开源组件选择、版本与实际接线状态

> 审计基准日：2026-07-26。本文区分“运行时已接线”“有限接线”和“仅可选依赖”，不把选择了一个仓库等同于已经完成集成。

## 结论

项目选择了 8 个上游仓库。六个组件直接参与核心运行时，Prefect 只有一个有限的离线入口，NautilusTrader 目前只是隔离的研究依赖。

| # | 上游仓库 | 当前依赖基线 | 许可证 | 当前实际角色 | 接线状态 |
|---|---|---|---|---|---|
| 1 | [Polymarket/py-sdk](https://github.com/Polymarket/py-sdk) | `polymarket-client==0.2.0` | MIT | 市场 REST/WS、账户、签名、下单、取消、对账、可选赎回 | 核心运行时已接线 |
| 2 | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | `research` extra：`>=1.220,<2`；锁文件按 Python 分支解析 | LGPL-3.0-or-later | 未来的隔离高保真研究/重放引擎 | 仅可选依赖，尚无 adapter/runner |
| 3 | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | `langgraph==1.2.9` | MIT | primary、skeptic、确定性聚合的预测图 | 核心运行时已接线 |
| 4 | [BerriAI/litellm](https://github.com/BerriAI/litellm) | `litellm==1.93.0` | 核心 MIT；enterprise 另有许可 | OpenAI-compatible JSON Schema 预测 provider | 可选运行时 provider 已接线 |
| 5 | [openai/openai-python](https://github.com/openai/openai-python) | `openai==2.46.0` | Apache-2.0 | Responses Structured Outputs 与 Web Search evidence | 核心/可选 AI 路径已接线 |
| 6 | [supabase/supabase-py](https://github.com/supabase/supabase-py) | `supabase==2.31.0` | MIT | ledger、runtime control、lease/fencing、RLS 后端访问 | 核心持久化已接线 |
| 7 | [PrefectHQ/prefect](https://github.com/PrefectHQ/prefect) | `prefect==3.7.8` | Apache-2.0 | 单个 `retries=0` 的周期 flow | 有限接线，仅 paper/shadow 离线入口 |
| 8 | [john-kurkowski/tldextract](https://github.com/john-kurkowski/tldextract) | `>=5.3,<6` | BSD-3-Clause | 使用内置 Public Suffix List 归一化可注册发布域名；关闭运行时 PSL 下载 | 核心 evidence gate 已接线 |

核心依赖在 `backend/pyproject.toml` 中精确固定，并由 `backend/uv.lock` 记录解析版本与哈希。NautilusTrader 是例外：它是范围约束的 optional dependency，不进入默认生产镜像。

## NautilusTrader 的 Python 版本分支

当前 `backend/uv.lock` 对 `research` extra 的解析是：

| Python | 锁定版本 |
|---|---|
| Python 3.11 | `nautilus_trader==1.221.0` |
| Python 3.12–3.14 | `nautilus_trader==1.230.0` |

因此不能笼统声称仓库固定使用 `1.230.0`。默认 Docker 使用 Python 3.12，但 Docker 构建没有安装 `research` extra，所以 Nautilus 不在 API/worker 镜像中。

## 1. Polymarket/py-sdk

仓库：[Polymarket/py-sdk](https://github.com/Polymarket/py-sdk)；许可证：[MIT](https://github.com/Polymarket/py-sdk/blob/main/LICENSE)。

### 当前已接线

- `market.py` 使用 `PublicClient` 做 REST 市场发现和 order-book fallback；
- `StreamingPolymarketMarketData` 使用 `AsyncPublicClient` 订阅 market WebSocket；
- `brokers/polymarket.py` 使用 `SecureClient` 创建、签名、提交、取消订单和可选赎回；
- `reconcile.py` 使用 `AsyncSecureClient` 订阅 user WebSocket，并读取完整 trades、lifecycle activity 和 `size_threshold=0` positions；
- live broker 查询真实 balance、allowance、open orders 和 positions；fills + REDEEM activity 重建成本，持久化 UTC 日初权益覆盖结算归零；
- `wallet-info` 依赖官方 SDK 自动派生/部署默认 Deposit Wallet；显式一次性的 `wallet-bootstrap` 调用 SDK `setup_trading_approvals()` 并检查 CLOB allowance；
- 所有真实资金写路径只使用这一官方 SDK，没有第二套 CLOB signer；
- 0.2.0 的异步成交响应 `trade_ids` 和当前可用的 transaction hashes 会进入订单响应审计，最终成交状态仍由 user WebSocket 和 REST 对账确认。

这些适配分别位于市场、broker 和 reconciler 模块；仓库中没有名为 `PolymarketGateway` 的统一类。

### 边界

`polymarket-client==0.2.0` 是当前稳定发布，但官方说明 0.x 的 minor release 仍可能包含 breaking change，因此资金路径继续按 beta 接口处理。Canary/live 需要：

```text
POLYBOT_BETA_SDK_ACK=I_ACCEPT_BETA_SDK_CANARY_ONLY
```

本地 mock/shape tests 只能发现接口形状回归，不能替代官方环境和真实小额 canary。

## 2. NautilusTrader

仓库：[nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)；许可证：[LGPL-3.0-or-later](https://github.com/nautechsystems/nautilus_trader/blob/master/LICENSE)。

选择它作为未来研究边界，是因为它提供事件驱动内核、订单/组合模型和 Python 控制面；这不代表当前已经有可运行的 Polymarket replay。

### 当前事实

- 只存在于 `[project.optional-dependencies].research`；
- 默认 `uv sync` 和生产 Docker 镜像不安装它；
- 没有 Nautilus adapter、Parquet exporter、research-runner、结果 importer 或 Supabase 回写；
- live signer、私钥和 service-role 不会进入 Nautilus 进程；
- 当前 JSONL 回测器是本项目自己的简化实现，不是 Nautilus。

### 许可证边界

若未来分发包含 Nautilus wheel 或独立 research 镜像，需要保留其版权和 LGPL/GPL 许可证文本，并确保用户能够替换兼容的动态库版本。对 Nautilus 自身的修改必须单独追踪并按 LGPL-3.0-or-later 履行义务。

本项目代码采用 MIT，不会复制、vendor、静态链接或合并 Nautilus 的 Rust/Cython/Python 源码。

## 3. LangGraph

仓库：[langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)；许可证：[MIT](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)。

`ai/graph.py` 当前实现固定的无状态图：

```text
primary forecast
  → independent skeptic forecast
  → deterministic aggregate
```

聚合逻辑取概率均值、区间并集，并按模型分歧降低置信度。图只返回 `Forecast`，不能导入 broker、读取 signer 或绕过风险层。

当前没有 LangGraph checkpointer、人工中断、跨周期 memory、任务恢复或可回放 checkpoint。表格中的角色不能描述为“已实现可恢复状态机”。

## 4. LiteLLM

仓库：[BerriAI/litellm](https://github.com/BerriAI/litellm)；许可证范围：[core MIT](https://github.com/BerriAI/litellm/blob/main/LICENSE)。

`LiteLLMForecastProvider` 当前直接调用 `acompletion`，要求 strict JSON Schema，并把结果解析为 `ForecastPayload`。

准确边界：

- LiteLLM 和直接 OpenAI 是两个可选 provider，不是 `OpenAI Python → LiteLLM` 的固定串联；
- 仓库没有附带 LiteLLM gateway 服务、路由表或私网部署；
- 供应商预算、rate limit、timeout 和 fallback 需要在用户实际部署的 gateway 中配置；
- 应用层只提供每周期 AI 市场数量、forecast cooldown 和输出 token 上限；
- LiteLLM `auto` evidence 不要求第二个模型/搜索 key，而是使用 GDELT Context 2.0 的近 72 小时同句 snippet；resolution source 与 title-only lead 不计数；
- GDELT 结果按市场缓存；接口 429 按 `Retry-After` 有界退避，5xx、断连或畸形响应同样进入短时退避，真实资金模式因不足两个独立 eTLD+1 来源而跳过交易；
- 不复制或使用 LiteLLM `enterprise/` 内容。

## 5. OpenAI Python

仓库：[openai/openai-python](https://github.com/openai/openai-python)；许可证：[Apache-2.0](https://github.com/openai/openai-python/blob/main/LICENSE)。

当前有两条直接用途：

1. `OpenAIWebEvidenceCollector` 使用 Responses Web Search 收集结构化 evidence，并只接受实际 citation/tool source metadata 中出现的 URL；
2. `OpenAIForecastProvider` 使用 `responses.parse(..., text_format=ForecastPayload, store=False)`。

外部网页、市场描述和规则会被截断并作为不可信数据送入模型。解析失败或概率区间非法会终止该市场的预测，不会进入下单。

尚未实现：

- provider 响应 ID、token 用量、货币成本和 latency 的完整 ledger（已实现：`ai_usage_ledger` + `/v1/me/ai-usage`）；
- 应用层多供应商 fallback（已实现：只包裹 `forecast()` 只读路径，配置 `POLYBOT_AI_FALLBACK_PROVIDERS`）；
- 证据已按规范化 URL、Public Suffix eTLD+1 和 forecast 实际引用设门；GDELT Context 有近 72 小时时效与同句 snippet，但尚无任意网页正文抓取校验或权威域名 allowlist；
- 基于真实结算结果的模型校准和漂移反馈。

## 6. Supabase Python

仓库：[supabase/supabase-py](https://github.com/supabase/supabase-py)；许可证：[MIT](https://github.com/supabase/supabase-py/blob/main/LICENSE)。

`SupabaseStore` 当前维护：

- markets、snapshots、evidence、forecasts；
- risk events、唯一 order intents；
- encrypted signed order、order status；
- fills、account activities 和 positions reconciliation；
- runtime controls；
- worker lease claim/validate/release 与 fencing token；
- durable UTC 日初、latest 与 peak equity；
- 部分 reconciliation audit。

Service-role 只应位于后端 API/worker。浏览器没有直接写策略或交易表的权限。加密 signed payload 会进入 Supabase，但 payload key、钱包私钥、AI/admin/CLOB secret 不进入数据库。

当前 dashboard 尚未接入 Supabase Auth；“RLS schema 已就绪”和“前端认证已经完成”是两件不同的事。

## 7. Prefect

仓库：[PrefectHQ/prefect](https://github.com/PrefectHQ/prefect)；许可证：[Apache-2.0](https://github.com/PrefectHQ/prefect/blob/main/LICENSE)。

当前只实现：

```python
@flow(name="polybot-trading-cycle", retries=0)
```

它创建 runtime 并运行一次 engine cycle。它没有：

- worker lease/fencing guard；
- 常驻市场归档；
- Nautilus 调度；
- 校准、结算或对账 flow；
- 生产 worker 的恢复/报警能力。

因此当前 Prefect flow 只应运行 paper/shadow 的人工或离线任务。Canary/live 常驻循环由 `polybot.worker` 负责。

## 八个仓库当前如何连接

实际运行链路是：

```text
Polymarket py-sdk
  → REST 市场 + WS/REST order book
  → Supabase market/snapshot
  → OpenAI evidence
  → OpenAI provider 或 LiteLLM provider
  → LangGraph primary/skeptic/aggregate
  → 本项目策略与确定性风险层
  → Supabase intent
  → leased/fenced worker 使用 py-sdk 单次提交
  → py-sdk user WS + REST reconciliation
  → Supabase orders/fills/positions
```

旁路状态：

```text
Prefect → 仅一次 paper/shadow cycle
NautilusTrader → 仅 research optional dependency，尚未连接数据流
```

这种表述比“八个仓库已经组成完整闭环”更准确。核心资金闭环不依赖 Nautilus 或 Prefect。

## 没有进入资金路径的仓库

| 仓库 | 处理 | 原因 |
|---|---|---|
| [Polymarket/py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2) | 不直接依赖 | 避免第二套认证、签名和重复订单路径 |
| [Polymarket/agents](https://github.com/Polymarket/agents) | 仅参考，不进入运行时 | 已归档且依赖/示例边界不适合作为生产 signer |
| [Polymarket/real-time-data-client](https://github.com/Polymarket/real-time-data-client) | 不采用 | 官方统一 Python SDK 已提供所需 market stream |
| [Polymarket/polymarket-subgraph](https://github.com/Polymarket/polymarket-subgraph) | 不采用 | 增加另一套派生数据和运维面，当前不需要 |
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | 不采用 | 需要额外 C# 服务和自定义 Polymarket adapter |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | 不采用 | 许可证和 bar 模型都不适合本项目的 CLOB/部分成交目标 |

## 许可证与升级原则

- 本项目自身代码使用 MIT；
- 保留上游仓库链接、版本和许可证记录；
- LiteLLM 只使用 core MIT 范围；
- Nautilus 保持 optional、独立、可替换，不进入默认 signer 镜像；
- 依赖升级先更新 `backend/uv.lock`，再运行单元测试、SDK shape tests、paper/shadow 和小额 canary；
- `0.x` Polymarket SDK 和任何签名/订单 schema 变化都按 breaking change 处理；
- 许可证记录不是 SBOM。仓库当前没有自动生成 SBOM 或完整 `THIRD_PARTY_NOTICES`，正式分发前仍需补齐。

无论使用多少开源仓库，都不能据此推导出高胜率、正收益或生产就绪。可验证的优势只能来自无泄漏的历史数据、真实费用/成交建模、样本外校准和受控 canary。

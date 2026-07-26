# Polymarket 自动交易机器人：教程文章审查与需求提炼

> 审查日期：2026-07-26  
> 文档性质：产品与技术输入审查，不构成投资、法律或税务建议  
> 结论优先：两篇文章可用于提取功能模块和策略类别，但均不能作为当前生产实现规范，也不能支持“高胜率”承诺。

## 1. 审查对象

1. PredictEngine，《How to Make a Polymarket Bot: Complete 2026 Guide》
   - 原文：https://www.predictengine.ai/blog/how-to-make-polymarket-bot
   - 页面日期：2026-02-28
   - 定位：PredictEngine 自家无代码产品教程，同时给出一段 DIY Python 示例

2. CryptoManiaks，《Best Polymarket Trading Bots in 2026》
   - 原文：https://cryptomaniaks.com/trading/best-polymarket-trading-bots
   - 页面标注最后更新：2026-03-19 16:21
   - 定位：4 分钟产品榜单，介绍 AI、新闻、复制交易、做市及开发框架

两文均早于 Polymarket 2026 年 4 月的 CLOB V2 切换。凡涉及 SDK、抵押物、钱包类型、费用、订单签名和交易所合约的内容，必须以当前官方资料为准。

## 2. 总体可信度结论

| 维度 | PredictEngine 教程 | CryptoManiaks 榜单 |
|---|---|---|
| 可用价值 | 给出从策略描述到托管运行的功能清单；指出生产机器人需要成交、持仓、余额和错误处理 | 提供五类机器人/策略的概念分类；NautilusTrader 是有效的开源线索 |
| 技术深度 | 示例仅演示轮询与下单，不是可恢复的交易系统 | 几乎无代码、接口、版本、许可证或回测方法 |
| 时效性 | 核心 Python 示例已被 CLOB V2 迁移淘汰 | 产品名称、链接和当前能力出现错配 |
| 业绩证据 | 没有可复现实盘或样本外证据 | 没有统一评测、实盘样本或风险调整后收益 |
| 利益冲突 | 文章主要推广作者自有 SaaS | 产品榜单未提供足够的测试和安全尽调 |
| 是否可支持“高胜率” | 否 | 否 |

项目应吸收两文描述的能力边界，不应复制旧代码，也不应把闭源第三方机器人直接拼装进资金执行链路。

## 3. PredictEngine 教程审查

### 3.1 可提炼的功能需求

文章给出两条路线：

- 无代码路线：自然语言生成策略，自动配置入场、出场、止损、仓位、滑点和市场筛选；支持模拟盘与托管运行。
- Python 路线：读取订单簿、按阈值生成买卖信号、提交 GTC 订单，并部署到常驻服务器。

由此可提炼的有效需求：

1. 市场数据采集必须 24×7 运行，并区分市场元数据、订单簿、成交、用户订单和持仓。
2. 策略参数必须结构化、可版本化，而不是只保存一段自然语言。
3. 上线前必须有模拟盘。
4. 生产系统必须具备：
   - 成交与部分成交确认；
   - 持仓和余额核对；
   - 订单生命周期管理；
   - 网络重试与断线恢复；
   - 运行日志、告警与紧急停止。
5. AI 可以协助形成研究结论和候选参数，但不能绕过确定性的风险与执行校验。

### 3.2 已过时的技术内容

文章使用以下旧栈：

- Python 包 py-clob-client；
- USDC.e 作为交易抵押物；
- 旧版 ClobClient 构造与订单结构；
- 旧的钱包/签名假设。

当前 Polymarket 官方要求使用 CLOB V2；迁移期提供过 `py-clob-client-v2`，本项目按当前统一官方 Python SDK `polymarket-client==0.2.0` 实现：

- Python 使用当前官方统一 SDK，而不是旧 `py-clob-client`；
- 抵押物为 pUSD；
- 新 EIP-712 域与新交易所合约；
- 新 API 用户主要使用 deposit wallet 和 POLY_1271 签名类型；
- 订单需携带当前市场对应的 tick size 与 neg-risk 参数；
- 费用由协议在成交时按市场设置计算。

官方明确说明旧 py-clob-client 仅适用于 V1，已不能对生产 CLOB 工作。

权威来源：

- CLOB V2 迁移：https://docs.polymarket.com/v2-migration
- 当前交易概览：https://docs.polymarket.com/trading/overview
- 当前下单接口：https://docs.polymarket.com/trading/orders/create
- 官方 GitHub 组织：https://github.com/Polymarket

### 3.3 示例代码的生产级缺陷

文章的示例不能进入实盘，主要原因如下：

1. **错误的仓位状态**  
   提交 GTC 订单后立即把 holding 设为 true，没有确认订单是否 live、matched、rejected 或部分成交。随后可能尝试卖出并不存在的份额。

2. **无法恢复**  
   holding 只在内存中。进程重启后状态归零，可能重复开仓；也没有从 CLOB、Data API 和链上余额做启动对账。

3. **信号价格不可成交**  
   使用 midpoint 判断入场，而真实成本由可成交 best ask、订单簿深度、费用、滑点和排队位置决定。

4. **空订单簿处理错误**  
   无 bid 时取 0、无 ask 时取 1，会人为生成 0.5 的中间价，掩盖流动性缺失。

5. **订单参数不完整**  
   未处理 tick size、最小订单、neg-risk、pUSD、allowance、deposit wallet、签名类型和动态费率。

6. **未处理部分成交与撤单**  
   没有订单 ID 持久化、超时、cancel/replace、幂等键和剩余数量管理。

7. **没有实时用户事件**  
   每 10 秒轮询不能替代用户 WebSocket 的订单与成交状态流。

8. **市场标识来源不可靠**  
   普通市场 URL 通常提供 event slug，不直接提供 outcome token ID。token ID 应由 Gamma API 的 clobTokenIds 与 outcome 数组一一映射。

9. **密钥保护不足**  
   普通 .env 只适合本地开发。生产 signer 必须与 Web 应用和数据库隔离，并限制热钱包余额、权限和可调用合约。

### 3.4 策略主张的审查

#### 单边低买高卖

价格阈值只是交易规则，不是预测优势。若没有独立的公平概率估计、催化剂和流动性判断，“跌了就买”可能持续接住错误定价。

#### YES + NO 套利

只有在以下条件同时成立时，才接近锁定收益：

- 两侧使用可成交 ask，而非 midpoint；
- 目标数量在两侧均有足够深度；
- 两腿能够原子执行，或系统能严格限制单腿暴露；
- 扣除 taker fee、滑点、延迟、失败重试成本后，组合成本仍小于 1；
- 两个 outcome 的结算关系确实互补，且不是错误映射或特殊 neg-risk 结构。

所以系统只能称其为“可执行套利候选”，不能默认称为“无风险套利”。

#### 临近结算买入

0.95 买入并最终兑付 1 的毛收益计算成立，但仍有：

- 市场规则与标题不一致；
- 结算源异常或数据延迟；
- UMA 提议与争议；
- 规则澄清；
- 薄流动性与无法退出；
- 低概率但接近 100% 的本金损失。

机器人必须先解析并保存完整结算规则、来源和边界条件。官方说明：https://docs.polymarket.com/concepts/resolution

#### 复制交易

公开钱包历史可作为信号，但复制者会遭遇：

- 幸存者偏差和挑选期偏差；
- 源钱包可能在其他账户或平台对冲；
- 发现源交易时价格已经变化；
- 源交易的小额试单可能被误当成主观点；
- 退出交易的流动性可能比进入更差。

复制交易必须有独立的源钱包评分、价格追踪上限、延迟统计和组合风险限制。

#### 体育赔率比较

不能直接把博彩公司展示概率与 Polymarket 价格相减。必须：

- 对同一赛事所有结果去除 bookmaker vig；
- 对齐是否包含加时、取消/延期、比赛完成条件；
- 对齐时间点、数据源和 Polymarket 结算规则；
- 将 in-play 延迟和暂停交易纳入模型。

#### 止损

Polymarket 原生订单类型为 GTC、GTD、FOK、FAK，没有原生 stop order。所谓止损是机器人监控到条件后再提交订单，跳价和薄流动性下不能保证价格或成交。

### 3.5 费用与产品宣传冲突

文章将 crypto taker fee 简化成约 2%，并称 event 市场无费。当前官方费用按类别和价格曲线计算：

fee = shares × feeRate × price × (1 − price)

费用不是统一的名义百分比；maker 不收 maker fee，但返点资格、比例和最低支付额依市场而异。

来源：

- 官方费用：https://docs.polymarket.com/trading/fees
- Maker Rebates：https://docs.polymarket.com/market-makers/maker-rebates

文章对 PredictEngine 自身的价格和费用描述也已过时：

- 教程称 Pro 为 $19.99；当前首页列出 Starter $19、Pro $39。
- 教程称没有 Polymarket 原生费用之外的交易费；2026-06-08 更新的条款列出入金、出金、bot、套利和 Discord 等平台费用。
- 条款一处称 custodial wallet creation，另一处称 self-custody，同时说明私钥由平台代生成并加密保存；这不等于用户自行控制的隔离 signer。
- 条款承认可能存在不完全基于已实现链上 P&L 的 phantom P&L 或游戏化展示，因此首页统计与排行榜不能作为策略业绩证据。

来源：

- 当前首页：https://www.predictengine.ai/
- 当前条款：https://www.predictengine.ai/terms

## 4. CryptoManiaks 榜单审查

### 4.1 可提炼的产品类别

文章将机器人概括为：

- 规则/执行机器人；
- 做市机器人；
- AI 决策代理；
- 新闻与情绪机器人；
- 复制交易机器人；
- 研究、回测与实盘一体的开发框架。

这是合理的能力分类，可映射成项目中的独立模块，但不应把榜单中的闭源服务直接作为资金执行依赖。

### 4.2 五项推荐的当前核验

#### Polystrat

当前产品页称其通过 Pearl 在本地运行，资金使用 Safe，并允许配置风险模式。可借鉴“本地 agent + 智能账户 + 有界授权”的设计。

限制：

- 文章没有提供对应源码仓库；
- 聚合交易次数不是收益或安全证据；
- 没有可复现的概率模型、回测集和独立审计资料。

产品页：https://www.pearl.you/polystrat

#### NautilusTrader

这是榜单中最具工程价值的推荐：

- 开源事件驱动交易内核；
- 研究、回测与实盘共享事件模型；
- 当前具有 Polymarket CLOB V2、pUSD、市场/用户 WebSocket 与订单执行适配；
- 可用于状态机、重放、对账和执行仿真。

采用条件：

- 固定包含 CLOB V2 迁移的近期版本；
- 跑完 deposit wallet、签名、成交、撤单、重启对账的集成测试；
- 注意 Python 与 Rust adapter 的行为差异；
- 不启用文档标为实验性的订单历史恢复能力来承担关键资金状态。

来源：

- GitHub：https://github.com/nautechsystems/nautilus_trader
- Polymarket adapter：https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/polymarket.md

#### PolyBro

文章称 PolyBro 扫描新闻和社交媒体后自动交易，但文章链接实际指向 https://polybot.me/ 。当前该站是付费、自托管的 PolyBot 复制交易和 15 分钟 ML 产品，不是文章描述的新闻机器人。

目前可找到的 PolyBro Chrome 商店条目又明确称只做分析、不执行交易：

https://chromewebstore.google.com/detail/polybro-polymarket-analys/mechndbekkmmhjldfgjkkhilmnbglofp

因此名称、链接和能力无法相互验证，不进入技术选型。

#### PolyCop 与 PolyGun

文章只提供 PolyCop 链接，没有分别核验 PolyGun。

- https://polycop.trade/ 当前会跳转到另一个域名；
- 产品为闭源 Telegram bot；
- “私钥只在 Telegram session 内生成、从不上传服务器”是厂商自述，页面未链接可验证代码或安全审计；
- 当前网络上存在多个 PolyGun 近似域名，且费率、账号和产品描述冲突。

结论：

- 不将此类 bot 作为代码依赖；
- 不向它们输入用户现有主钱包私钥；
- 不依据同名域名或社交账号判断真实性；
- 只吸收复制交易、追价限制、限仓和通知等功能概念。

#### Custom Market-Making Bot

这是策略类别，不是可安装产品。“同时挂买卖单”并不等于两侧都会成交。系统必须处理：

- 信息型订单专门成交陈旧报价的逆向选择；
- 单边成交后的库存风险；
- 报价更新和 cancel/replace；
- YES/NO 拆分、合并和赎回；
- 返点评分规则与最低支付额；
- 跨市场和相关事件的总风险。

官方做市说明：https://docs.polymarket.com/market-makers/overview

### 4.3 榜单遗漏的关键风险

文章只简要提到费用、安全和流动性，还遗漏：

- 钱包与 signer 的真实控制权；
- CLOB V2 版本兼容；
- 订单部分成交、重启和对账；
- AI 幻觉、过度自信和 prompt injection；
- 新闻来源冲突与发布时间错误；
- 结算规则和 UMA 争议；
- 复制交易的幸存者偏差和时延；
- 做市逆向选择与库存；
- 地域和法律限制；
- 回测的数据泄漏、过拟合与不现实成交假设。

## 5. 对项目的正式需求

### 5.1 当前协议与数据

1. 仅采用当前 CLOB V2 SDK/adapter。
2. 运行时读取 pUSD、signature type、tick size、minimum size、neg-risk 和 fee 参数。
3. 数据分层：
   - Gamma API：市场、事件、规则、结算源、token IDs；
   - CLOB REST/WebSocket：订单簿、价格、成交和订单；
   - Data API：仓位、活动、历史交易和公开钱包画像。
4. 所有外部字符串均视为不可信内容，只作为数据，不能成为代理指令。

官方 API 划分：https://docs.polymarket.com/api-reference/introduction

### 5.2 AI 决策边界

AI 层只允许输出结构化研究结果：

- 估计概率及区间；
- 置信度；
- 证据 ID、来源和时间；
- 支持与反对证据；
- 失效条件；
- 建议观察期。

AI 不得直接持有 signer，不得自行扩大仓位，不得绕过风险检查。确定性 edge engine 使用可成交价格、深度、费用、滑点和延迟缓冲做最终判断。

### 5.3 执行与状态

必须实现：

- 持久化订单状态机；
- user WebSocket；
- 部分成交和剩余量管理；
- 幂等下单；
- cancel/replace；
- 心跳和数据新鲜度检查；
- 启动、重连和定期 reconciliation；
- 紧急 cancel-all 与关闭新仓；
- 链上交易和 CLOB 订单的统一审计链。

### 5.4 风险与安全

- 模拟盘为默认模式。
- 实盘必须经过 docs/STRATEGY_VALIDATION.md 定义的阶段门。
- signer 与 Vercel、浏览器和 Supabase 表隔离。
- 使用小额专用热钱包、合约 allowlist、最小权限和额度上限。
- 设置单笔、单市场、单事件、相关主题、全组合和单日损失限制。
- API 或模型失效、数据陈旧、异常成交、回撤越限时自动 fail closed。
- 启动及下单前检查官方 geoblock，不规避地域限制：
  https://docs.polymarket.com/api-reference/geoblock

### 5.5 基础设施映射

- **Zeabur**：常驻 WebSocket、市场数据 worker、策略 worker、OMS 与 signer/executor。
- **Supabase**：配置、事件日志、订单镜像、仓位快照、实验与回测结果；开启 RLS，不保存明文私钥。
- **Vercel**：控制台、只读监控、策略审批和紧急停止入口；不承载长期交易循环或 signer。

## 6. 明确不采纳的内容

- 不复制旧 py-clob-client 示例。
- 不使用 midpoint 代替可成交价格计算收益。
- 不把提交成功当成成交成功。
- 不把 stop loss 表述成保证成交。
- 不把 YES+NO 小于 1 的截图表述成无风险收益。
- 不以历史排行榜直接决定复制钱包。
- 不让 LLM 单独签名或下单。
- 不将闭源 Telegram bot、同名域名或厂商自报收益作为安全/业绩证明。
- 不宣传或承诺“高胜率”“稳定收益”“无风险套利”。

## 7. 可对外表述

项目可表述为：

> 这是一个 AI 辅助研究、确定性风控和自动化执行的 Polymarket 交易系统。系统会通过历史回放、样本外验证、模拟盘和小额实盘逐级验证。任何策略都可能亏损，历史胜率不代表未来结果。

不得表述为：

- 保证高胜率；
- 保证盈利；
- 无风险套利；
- AI 能准确预测所有事件；
- 止损一定能按指定价格成交。

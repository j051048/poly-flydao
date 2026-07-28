# 策略验证与 P2 上线门槛

“胜率高”不是上线标准。频繁赚小额、偶尔承受一条未配对腿的大损失，可以同时表现为高胜率和负期望。决策必须看费用后期望值、尾部风险、成交质量和样本外稳定性。

## P2 策略假设

研究策略包含三层：

1. 两边都只挂 post-only maker 限价，减少主动吃单费用和滑点。
2. 在不同时间收集等量 Up/Down（YES/NO）份额；只有总成本加 maker fee、潜在 taker hedge fee、单腿风险缓冲和资金成本后仍低于 1，才把差额视为候选锁定收益。
3. 方向模型足够强且已校准时，允许小额多余方向仓位；它必须单独记账、单独限额，不能计入 paired edge。

批量发送两条腿不等于原子成交。任何回测都必须模拟排队、部分成交、撤单延迟和对冲最坏价格。

## 数据门槛

用于 live gate 的数据必须：

- 是 point-in-time 数据，不能使用当时不可见的最终结果、修订信息或未来盘口；
- 包含完整 L2 深度、单调且无缺口的交易所 sequence、成交方向、精确 tick/min size；
- 保存当时 fee/rebate 规则、市场 token/outcome 映射、起止时间与结算规则；
- 覆盖高/低波动、单边趋势、跳空、流动性枯竭和 API 异常阶段；
- 明确记录缺失区间。缺失数据不能按“无成交”处理。

仓库的普通 JSONL backtest 不能作为 P2 live 证据。只有 `L2ReplayResult.live_gate_eligible=true` 才能进入下一阶段，它也只是必要条件之一。

## 回放模型

事件级 replay 至少要包括：

- 提交和撤单延迟；
- 订单到达时的 post-only rejection；
- 同价位外部 queue ahead；
- 部分成交与 cancel race；
- 双腿独立接受/成交；
- GTD 到期；
- maker/taker fee、rebate、资金占用；
- 未配对腿在 deadline 后的有界对冲或冻结；
- 对冲深度不足和价格跳跃压力测试。

运行示例：

```powershell
Set-Location backend
uv run --frozen polybot pair-replay --input examples/pair_replay_sample.json
```

示例文件是格式演示，不是收益证据。

## 统计评估

时间顺序划分训练、校准、验证和最终 holdout，禁止随机打散。至少报告：

- 配对后净 EV/share 与净 PnL；
- 95% bootstrap 置信区间；
- 最大回撤、Expected Shortfall、最坏单腿损失；
- 两腿成交率、配对完成时间、孤腿率、强制对冲率；
- maker 成交偏差、post-only rejection、cancel race；
- 资金利用率和持仓时间；
- 方向模型的 Brier score、log loss、校准曲线和分桶漂移；
- 与“不交易”、固定价差、只做配对不做方向等基线比较。

必须同时做压力测试：费用上调、rebate 归零、延迟倍增、queue ahead 增大、成交率下降、对冲滑点扩大以及最优市场选择偏差。

## 阶段 Gate

### Gate A：研究

- 完成数据泄漏审计和 token 方向人工抽样。
- 至少三个非重叠 walk-forward 窗口均未出现费用后负期望。
- 最终 holdout 的净 EV 95% 下界大于 0。
- 压力场景下损失没有突破预设账户风险预算。

未满足时 P2 保持 `research_only=true`、`execution_enabled=false`。

### Gate B：Paper

- 连续至少 14 天使用实时数据。
- 无跨租户、重复 fill、版本跳跃或无法解释的 inventory drift。
- 所有孤腿都在 deadline 内进入确定的 paired/hedged/frozen 状态。

### Gate C：Shadow

- 连续至少 7 天把模拟下单与当时真实可成交路径比较。
- 记录而不是忽略 API 失败、撤单竞态和盘口缺口。
- 实际可成交估计在费用后仍通过 Gate A 的下界要求。

### Gate D：Canary

- 使用可完全损失的小额专用资金，单笔不超过代码的 canary 硬上限。
- 每次 arm 很短，首批订单人工旁观。
- 核对 CLOB、user stream、REST、Supabase order/fill/inventory 四方一致。
- 任一模糊订单、账实差异、撤单未确认或风险超限都回到研究阶段。

### Gate E：Live

当前仓库没有自动解锁 P2 live 的开关。只有完成上述 Gate、独立安全/策略审查并新增显式数据库 migration 与发布审批后，才讨论接入 live runtime。

历史账户截图、用户名、社交媒体收益数字或“某人三个月赚了多少”都不能替代这些证据。没有策略能保证持续赚钱。

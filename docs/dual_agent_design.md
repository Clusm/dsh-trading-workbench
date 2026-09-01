# 双交易通道架构设计（策略通道 + AI 自决策通道）

版本：v1（2025-08）
状态：设计已确认，P0 开始落地

## 1. 核心目标

把原主 Agent 拆成两条独立决策通道：

| 通道 | Agent | 订单来源 | 职责 |
|---|---|---|---|
| 策略通道 | strategy_trader | `strategy` | 执行 strategy_lab 验证过的 final 策略卡 |
| AI 自决策通道 | ai_trader | `ai` | 完全自主判断、自主下单 |

两个通道共用统一风控与执行层，但**资金、统计、熔断完全分开**。

---

## 2. 策略交易 AI

### 2.1 信号发现：确定性扫描器

- 由 `SignalScanner` 读取 `knowledge/strategy_cards.json` 中的 `signal_fn/params/exits`。
- 扫描 BTC/ETH 的 30m/1h/2h/4h/8h K 线，计算指标并调用信号函数。
- 只有信号函数在当前已完成 K 线收盘后返回信号，才生成一张 ticket。
- LLM 不做指标计算，避免幻觉与未来函数。

### 2.2 Signal Ticket

小时级信号只负责“发牌”，AI 负责在牌局内做分钟级进场决策：

```json
{
  "ticket_id": "t-ETH-8h-20250831-0800",
  "strategy_id": "F_OLD_MACD_RSI_8H",
  "symbol": "ETHUSDT",
  "side": "SHORT",
  "signal_time": "2025-08-31T08:00:00Z",
  "signal_price": 3350.0,
  "entry_zone_low": 3310.0,
  "entry_zone_high": 3390.0,
  "expires_at": "2025-08-31T12:00:00Z",
  "stop_loss_pct": 0.07,
  "take_profit_pct": 0.09,
  "time_stop_hours": 96
}
```

### 2.3 策略 AI 的权限边界

策略 AI 可以决定：

- 是否执行这张 ticket；
- 名义金额与杠杆（不再固定 80U/1x）：名义金额在 min/max 区间内按策略账本风险预算自适应，杠杆 1x~20x 动态选择；
- 市价立即进场，或挂限价/条件单等待分钟级更好的进场点；
- 分一批或两批进场；
- 主动放弃 ticket。

策略 AI 不能改变：

- 品种、方向、周期；
- 卡片规定的 SL/TP 比例与 time stop；
- entry_zone 范围（由 ATR 计算）；
- ticket 过期后继续追单。

### 2.4 执行质量计量（影子基准）

每张 ticket 同时记录：

1. **实际单**：AI 选择的分钟级进场点。
2. **影子基准**：同一信号按“下一根 K 线开盘”模拟成交，不占资金。

统计字段：`entry_delay_minutes`、`entry_deviation_pct`、`actual_return`、`baseline_return`、`entry_edge`。

自动升降级规则：

- AI 进场连续 20 张优于基准 → 保持分钟级自主权限；
- 连续 20 张劣于基准 → 降级为 `next_open`；
- 结果不稳定 → 只允许在基准价 ±0.3 ATR 内微调。

---

## 3. AI 自决策 Agent

- 独立判断，可主动使用 `query_symbol_detail / query_top_symbols / query_review_library`。
- `strategy_refs` 只作背景参考，不是下单依据。
- **AI 可以自己下限价单**：`order_type=LIMIT`，必须带 `limit_price` 和 `limit_expires_at`。
- **AI 决策单默认逐仓**：`margin_mode=ISOLATED`。
- 每个 AI 开仓单必须给出交易逻辑标签 `ai_reason_type`。

---

## 4. 订单与数据模型

`TradeIntent` 增加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `order_source` | `strategy / ai` | 订单来源，必须显式 |
| `strategy_id` | `string/null` | 策略单才填 |
| `signal_id` | `string/null` | 策略单的信号事件 |
| `ticket_id` | `string/null` | 策略单的 ticket |
| `entry_mode` | `string/null` | `next_open/retest_limit/breakout_confirm/ai_limit` |
| `limit_price` | `float/null` | AI 限价单必填 |
| `limit_expires_at` | `string/null` | AI 限价单必填 |
| `sl_pct` | `float/null` | 策略单按百分比止损 |
| `tp_pct` | `float/null` | 策略单按百分比止盈 |
| `margin_mode` | `ISOLATED/CROSSED` | AI 默认 ISOLATED |
| `ai_reason_type` | `string/null` | AI 单逻辑标签 |

决策拆分：

```text
strategy_decision → ai_decision → merge → risk → execution
```

---

## 5. 虚拟账本：总权益 6:4，每日重算

### 5.1 分配比例

- 策略账本 = 总权益 × 60%
- AI 账本 = 总权益 × 40%

### 5.2 每日重算（UTC 00:00）

```text
total_equity = strategy_book.equity + ai_book.equity
target_strategy = total_equity * 0.60
target_ai       = total_equity * 0.40
transfer_ai = target_ai - ai_book.equity
```

- 只调整两个账本的现金，**不强制平仓**；
- 调账属于“资金划转”，不算盈亏，写 `daily_rebalances`；
- 调账后各自记录新的 `day_start_equity`，当日收益据此计算。

### 5.3 分别的仓位保证金上限

```text
strategy_margin_cap = strategy_book.equity × channel_max_margin_usage
ai_margin_cap       = ai_book.equity       × channel_max_margin_usage
```

- 总账本仍受全局 `max_margin_usage` 约束；
- 任一通道保证金超上限：该通道禁止新开仓；超限超过 5% 时自动减仓/平仓直到合规；
- 一个通道触限，不影响另一个通道。

---

## 6. 风控硬规则

### 6.1 所有开仓必须有初始止盈和止损

- `action=open` 时，`stop_loss` 与 `take_profit` **必须同时存在且 > 0**；
- 用实际参考入场价校验方向：
  - LONG：`stop_loss < entry_ref < take_profit`
  - SHORT：`take_profit < entry_ref < stop_loss`
- `entry_ref` 优先取 `limit_price`（限价单），市价单取最新价；
- 策略单可用 `sl_pct/tp_pct`，成交后按实际成交价换算绝对 SL/TP；
- 实盘执行层若挂不上止盈/止损，必须重试；仍失败则立即平掉该仓位，不允许裸仓。

### 6.2 AI 账户当日 -30% 熔断

- 按 **UTC 日** 计算；
- `ai_equity <= ai_day_start_equity × 0.70` → AI 通道当日进入 `close_only`；
- 次日 UTC 00:00 重算并解除熔断；
- AI 熔断不影响策略通道。

### 6.3 其他分通道规则

策略通道：

- 只能交易卡片中的 symbol/timeframe/direction；
- SL/TP 与卡片 exits 一致，不得放宽；
- 每张策略最多同时 1 个仓位；
- ticket 过期后不再执行。

AI 通道：

- 默认逐仓；
- 强制初始 SL/TP；
- 单笔风险 ≤ AI 账本权益 × 2%；
- 连续 3 笔亏损或当日 -30% → 当日只平仓。

冲突规则：

- 同一 symbol，策略仓与 AI 仓**互斥**；
- 策略优先：AI 想开冲突仓位时拒绝；策略信号想开冲突仓位时跳过并记 `skipped_ai_conflict`。

---

## 7. 执行层

### 7.1 纸面

- 每个账本独立 `cash / positions / pending_orders`；
- AI 限价单进入 `pending_orders`，用后续 1m K 线高低点按时间顺序判断成交，不允许未来函数；
- 订单与持仓均带 `order_source / strategy_id / ticket_id / margin_mode`。

### 7.2 Binance Demo/实盘

- AI 限价单走原生 `LIMIT + timeInForce=GTC`，到期未成交按 `clientOrderId` 撤单；
- AI 单默认 `marginType=ISOLATED`；
- SL/TP 走 Algo Order，必须两个都挂成功，否则立即平仓；
- 实盘暂不做物理分账，用本地台账按来源归因，并保持同 symbol 通道互斥；资金足够后再考虑子账户。

---

## 8. Graph

```text
START
  ├─ market ─┐
  ├─ macro ──┼─ signal_scanner ─ strategy_trader ─┐
  └─ onchain ┘                                    ├─ merge ─ risk ─ execution ─ END
                                                  └─ ai_trader ────────┘
```

错误隔离：任一通道异常只影响该通道，本轮另一通道照常。

---

## 9. 统计与仪表盘

- `channel_daily_stats`：date/channel/start_equity/end_equity/realized_pnl/fees/closed_trades/wins/win_rate/max_drawdown...
- AI 自决策日报独立显示，不与总账户混合；
- 策略通道显示 signal/ticket/影子基准对比；
- 时间戳一律 UTC，界面同时显示 UTC 与 UTC+8。

---

## 10. 落地阶段

- **P0**：模型、数据库、配置、全部开仓强制 SL/TP（已完成）
- **P1**：SignalScanner + strategy_trader，原 orchestrator 改为 ai_trader（已完成：扫描/去重/ticket/双通道并行/merge）
- **P2**：双虚拟账本 6:4、每日重算、通道保证金上限、AI 限价单队列（已完成：双账本/权益修复/UTC重算/熔断/纸面限价队列/每日胜率收益统计）
- **P3**：ticket 影子基准 + AI -30% 熔断 + 统计报表
- **P4**：Dashboard 分通道展示与复盘分离（已完成：首页双通道卡片 + /channels 页面）
- **P5**：Binance Demo/实盘订单来源台账（已完成：futures_source_ledger + 本地 LIMIT 监控/补保护）

# 观察门禁状态（observed_status）

> **结论：三项观察门禁全部「未开始 / 未完成」。**
> **不得写成通过。** 观察窗尚未启动（`observation_window.state = NOT_STARTED`）。

- 适用 RC：`unattended-rc`（DRAFT）
- 状态文件（机器可读）：[`observed_status.json`](observed_status.json)
- 门禁口径来源（集成树**实际代码**）：`src/core/release_evidence.py:107`
  `DEFAULT_THRESHOLDS`，由 `build_release_readiness_report()` 消费。

## 1. 三项观察门禁

| 门禁 | 必需（冻结值） | 实测值 | 状态 | 说明 |
|---|---|---|---|---|
| **7 天观察跨度** | `min_observation_days = 7.0` | **无**（`null`） | **NOT_STARTED** | 观察窗未启动；窗口边界必须**由声明**给出（`resolve_release_window`），**禁止**把数据首末当边界（规格 §12） |
| **200 方向候选** | `min_directional_candidates = 200` | **无**（`null`） | **NOT_STARTED** | 同版本真实前瞻样本不可由 Agent 生成；影子 B/C 样本**不得**进入分母（`S08` 已闭合） |
| **30 完整闭合** | `min_eligible_closed_trades = 30` | **无**（`null`） | **NOT_STARTED** | 需完整 lifecycle（`CLOSED` + `ATTRIBUTED` + `cost_status=COMPLETE` + 有效价格/数量），按 `lifecycle_id` 去重 |

## 2. 同时冻结、同样未开始的观察类门禁（不得遗漏登记）

| 门禁 | 必需（冻结值） | 实测值 | 状态 |
|---|---|---|---|
| 观察覆盖率 | `min_observation_coverage = 0.999` | 无（`null`） | **NOT_STARTED** |
| 最大观察间隔 | `max_observation_gap_seconds = 120.0` | 无（`null`） | **NOT_STARTED** |
| 未收口 critical incident | `0` | 无（`null`） | **NOT_STARTED** |
| 未知订单 / 在途提交订单 | `0` / `0` | 无（`null`） | **NOT_STARTED** |
| 新开仓就绪门禁 `entry_readiness` | `True` | 无（`null`） | **NOT_STARTED**（`DEPLOY_DEMO` 未开始） |
| 网络 418/429 = 0 且协调可用 | `http_418=0, http_429=0` | 无（`null`） | **NOT_STARTED** |

## 3. 门禁数值**未被降低**（逐字确认）

`DEFAULT_THRESHOLDS`（`src/core/release_evidence.py:107`）在本 RC 中**逐字未改**：

```
min_directional_candidates = 200
min_eligible_closed_trades = 30
min_observation_days       = 7.0
min_observation_coverage   = 0.999
max_observation_gap_seconds= 120.0
```

- 本 RC 交付物**不含** `src/**` 变更 → 阈值不可能被本次交付改动。
- **未新增**任何 `thresholds=` 调用把 `required` 调低；台账 `U09-P3-CLUSTER` 记录的
  "`thresholds=` 入参可显式调低 `required`"属**待裁决 A5**（本 RC 不自行决定）。
- **未**通过删除异常样本、改小样本门槛或发起无必要交易来"凑"达标。

## 4. 不构成发布通过的声明

- `K06` 五组全绿 **≠** 发布通过（台账 `U09-K06-GREEN-BUT-NO_GO`）：权威判定仍是
  `build_release_readiness_report()` 输出的 `release_readiness`。
- 本 RC 的交付状态是 **DRAFT**：`BUILD` 之后的 `DEPLOY_DEMO` / `OBSERVE` / `OPS` /
  `RELEASE` 检查点**均未开始**。
- 对外待决项 `D05`（运维 SLO 测量口径与目标冻结）`blocks_checkpoint = RC_FREEZE`
  → **RC 冻结前必须关闭**；`D03`（账户必要风险覆盖）阻断 `RELEASE`。
- 实盘 `live_enabled = false`，**未启用**，需用户另行明确指令。

## 5. 何时可以更新本文件

只有**同版本候选**在 Demo 真实采证、且观察窗按**声明**的起点推进后，由编排者 I / Q
按 `report_release_readiness.py` 的只读输出更新本文件。**不得**用 Agent 生成的合成样本、
影子样本或历史回放样本填充上述分母（规格 §3：历史记录可修复账本、用于回放，
但不假造最终版本的前瞻观察）。

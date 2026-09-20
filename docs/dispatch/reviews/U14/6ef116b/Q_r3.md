# U14 修复轮 r2 独立复验（Q 角色）r3

- 复验对象：`6ef116b`（分支 `task/U14`；父 `68903f8`；修复提交 `a89eaa5`，补丁包提交 `6ef116b`）
- 上一轮报告：`mat-wt/I/docs/dispatch/reviews/U14/68903f8/F_r1.md`（CHANGES_REQUESTED，P1×3）
- 补丁目录：`mat-wt/EF/docs/dispatch/patches/U14/a89eaa5/`（4 条 diff + README）
- 我的工作区：`mat-wt/reviews/U14/R3/r1/`（**非 git worktree，普通目录**，detached 语义由 `git archive` 沙箱实现）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`（3.13.12，`PYTHONUTF8=1`）；无 pytest
- 说明：本报告全部结论来自**本轮独立复现**，不采信作者/编排者的对照。探针 `_scratch_Q/probes/`、
  原始输出 `_scratch_Q/logs/`、结构化结果 `_scratch_Q/q*/`、沙箱 `_scratch_Q/sb/`。

> 环境注记：本会话 Bash 无法把命令行里字面写出的中文路径透传给子进程（`ls`/`python -c` 均报
> "No such file or directory"），故所有命令改用 `$(cd <glob>/mat-wt && pwd)` 动态取值或相对路径。
> 这不影响任何结论（`Read`/`Write`/`Grep` 工具对同一路径正常）。

---

## 0. 结论

**CHANGES_REQUESTED**

- **P1-2、P1-3 真闭合**（前后对照 + 反例均独立复现）。
- **P1-1 只闭合了一半**（新增 **P1-4**，详见 §1.1.4 与 §2）：F 点名的
  `AlertLadderViolation` 路径确实被单条隔离；但"单条隔离"的 `except` 只收
  `(AlertLadderViolation, AlertReceiptRejected)` 两个类型，**捕获集合小于它需要防的异常集合**：
  1. **`transport.submit()` 自身抛出的任何异常**（`TimeoutError`/`OSError`/`RuntimeError`…）
     直接穿出 `dispatch_due` ⇒ 整批中断 + 该行无退避 + **每轮复发**。
     真实网关接线（U12）后一次网络抖动即可让整条告警通路永久停摆。
  2. 本模块**自己**为"空白回执"抛的**裸 `ValueError`**（不是 `AlertReceiptRejected`），
     以及 `int(outcome.delivered_at_ms)` 对非数值抛的 `ValueError`/`TypeError`，同样漏网。
  两种向量的后果与 F 原报的 P1-1 **逐条相同**，且向量 1 在 `sb/68903f8` 上结果**完全一致**
  ⇒ 属 r2 未触及的残留，不是新引入的回归。
- 其余 P2/P3 抽验 **8/8 通过**；P2-2/P2-3 登记文字**属实**（仅一处行号引用不准，P3）。
- 新发现：**P1-4（未受单条隔离保护的畸形回执 ⇒ 整批中断 + 每轮复发）**、P3-6（`_record_failure`
  对阶梯拒绝写 `TRANSPORT_FAILED` 审计标签，语义不符）、P3-7（README §5.2 与 §2 的两处引用/适用范围问题）。

---

## 1. 逐项判定（对应任务 1–7）

### 1.1 第 1 项 P1-1 — **部分闭合（新增 P1-4）**

#### 1.1.1 F 原场景前后对照（独立复现，探针 `probe_q1_p11.py` §C1）

输入：4 条告警同批，`RecordingSink(provider_receipt="r", delivered_at_ms=now-1, device_receipt="dev-r")`，
连续 3 轮 `dispatch_due(now, now+1, now+2)`，再推进到 `now+300s` 派发一次。

| 树 | 结果 |
|---|---|
| `sb/68903f8`（修复前） | 三轮**全部抛** `AlertLadderViolation: alert ladder violated: TIME_ORDER_VIOLATION`；队列快照 `{'ACCEPTED': 3, 'UNKNOWN': 1}`（第 4 条一条都没投递 ⇒ **整批中断**）；触发告警 `state=ACCEPTED, next_attempt_ms=now+300s`；`t+300s` **再次抛**（复发） |
| `sb/after`（`6ef116b`） | `round0 return submitted=4/confirmed=4/rejected=0, reason_codes=['PROVIDER_CLOCK_SKEW_TOLERATED']`；`round1/round2` 无事发生；队列 `{'DELIVERED': 4}`；触发行 `state=DELIVERED, delivered_at_ms=1700000000000 == submitted_at_ms, next_attempt_ms=NULL`；`t+300s` `submitted=0 confirmed=0 rejected=0 reasons=[]`（**不复发**） |

原始输出：`_scratch_Q/logs/q1_before.log`、`_scratch_Q/logs/q1_after.log`。

**结论：F 点名的这条路径确已闭合。**

#### 1.1.2 容差边界（探针 §C2 / §C5d）

`RECEIPT_TIMESTAMP_TOLERANCE_MS = 120_000`（独立读出）。

| 场景 | 结果 |
|---|---|
| 偏斜 **恰好 = 120000ms** | 归一（`PROVIDER_CLOCK_SKEW_TOLERATED` + 审计行），不拒绝 |
| 偏斜 **= 119999ms** | 归一 |
| 偏斜 **= 120001ms（差 1ms）** | **不归一**（`reason_codes` 无 skew 码），走阶梯拒绝 |
| 偏斜 = 0 | 不归一（不需要） |
| 直接 API：`delivered_at_ms = now + 120000`（恰好上界） | 接受 → `DELIVERED` |
| 直接 API：`delivered_at_ms = now + 120001`（上界 +1ms） | `AlertReceiptRejected: RECEIPT_TIMESTAMP_FUTURE`，行停在 `ACCEPTED` |

边界方向为 `skew_ms > tolerance` 与 `delivered_at_ms > now + tolerance`，
**恰好相等即放行、差 1ms 即拒绝**，与 README §5.1/§7.6 的措辞一致。

#### 1.1.3 归一后是否仍早于提交 / 是否掩盖真实时钟问题

- `_normalize_receipt_time` 返回 `int(submitted_at_ms)`，**归一后 `delivered - submitted == 0`**
  （探针 §C4 实测），不再存在"回执早于提交"的残留。
- **掩蔽性评估（这是本轮重点）**：实现与 README §7.6 的措辞**一致**，理由三条，均已独立验证：
  1. 容差**有界**（120 s），超界不归一而是交回 K05 拒绝（§1.1.2 已实测差 1ms 即拒）；
  2. 归一**留痕**：写 `alert_audit` 行（`transition='PROVIDER_CLOCK_SKEW_TOLERATED'`，
     note 里**保留供应商原始 `delivered_at_ms`** 与偏斜毫秒数），并在返回结构追加同名原因码；
  3. 归一只改**审计记录用的时间戳**，`occurrence_at_ms`（SLO 延迟的分母）不受影响。
  → 因此"有界容忍 ≠ 供应商时钟无问题的证据"这句措辞与实现相符，**不是**静默掩盖。
- 但**必须点名一条残留**：归一是"无害化"而非"诊断"。若真实网关长期以同一偏斜量偏早（例如恒定 −90 s），
  每轮都会归一 + 记同一原因码，**没有任何聚合/计数阈值**把它升级成"该通道时钟可疑"。当前
  `reason_codes` 是去重后的集合，运维侧无法从 `ops_status` 看到偏斜频次。属**观察项**（不列入 P 级，
  因规格未要求；但接线（U12）时应考虑累计偏斜的暴露面）。

#### 1.1.4 绕过尝试（**发现未被隔离的一类输入** ⇒ 新增 P1-4）

探针 §BYPASS 用自建 `RawSink` 直接返回畸形 `TransportOutcome`，队列里第一条为畸形条、其后 2 条正常条，
观察异常是否穿出 `dispatch_due`、以及退避 3600 s 到期后是否复发。

**（A）`transport.submit` 自身抛异常 —— 最现实的向量**（探针 `probe_q4_transport_raises.py`，
`_scratch_Q/logs/` + `q4/`；**before / after 两树结果逐字相同**）：

| 通道抛出的异常 | 是否穿出 `dispatch_due` | 队列终态 | 该行退避 | +3600s 复发 |
|---|---|---|---|---|
| `TimeoutError("gateway timeout")` | **是** | `{'UNKNOWN': 3}`（后两条一条都没投递） | `attempts=0, next_attempt_ms=0`（**没有退避**） | **是** |
| `RuntimeError("connection reset by peer")` | **是** | `{'UNKNOWN': 3}` | 同上 | **是** |
| `OSError("network unreachable")` | **是** | `{'UNKNOWN': 3}` | 同上 | **是** |

`outcome = transport.submit(...)` 虽然被搬进了 `try`（r2 改动），但 `except` 只收
`AlertLadderViolation` / `AlertReceiptRejected`，**任何通道侧异常（超时、连接重置、
DNS、TLS、SDK 的自定义异常）都会穿出**，于是：整批停摆 + 该行无退避 + 每轮复发。
**这是 `dispatch_due` 里最可能真实发生的一条中断路径**（U12 接线真实网关后，
一次网络抖动即可让告警通路永久卡死），且 r2 修复**完全未触及**（before/after 行为一致）。

**（B）畸形回执（不抛异常的通道返回）**：

| 畸形输入 | 异常是否穿出 | 异常型 | 未处理条数 | 该行 `next_attempt_ms` / `last_error` | 到期后复发 |
|---|---|---|---|---|---|
| `provider_receipt="   "`（**空白但 truthy**） | **是** | `ValueError: provider_receipt required: 回执不得为空` | 2 条 UNKNOWN 未投递 | `NULL` / `''` | **是**（同一 `ValueError`） |
| `device_receipt="   "` | **是** | `ValueError: device_receipt required: 回执不得为空` | 2 条 UNKNOWN | `NULL` / `''` | **是** |
| `delivered_at_ms="not-a-number"` | **是** | `ValueError: invalid literal for int()` | 2 条 UNKNOWN | `NULL` / `''` | **是** |
| `delivered_at_ms=[1,2]` | **是** | `TypeError: int() argument must be…` | 2 条 UNKNOWN | `NULL` / `''` | **是** |
| `receipt_alert_id="alert-someone-else"` | 否 | — | 0 | `now+30s` / `TRANSITION_REJECTED:RECEIPT_BINDING_MISMATCH` | 否 |
| `receipt_alert_id=None` | 否 | — | 0 | `now+30s` / `TRANSITION_REJECTED:RECEIPT_UNBOUND` | 否 |
| `device_receipt=12345` | 否 | — | 0 | `RECEIPT_TYPE_INVALID`（见下） | 否 |
| `delivered_at_ms=now+10**12` | 否 | — | 0 | `RECEIPT_TIMESTAMP_FUTURE`（见下） | 否 |

另有混批测试（§C3）：坏条（回执早于提交 600 s，超容差）排在**最前**、其后 3 条正常 ⇒
`submitted=4, confirmed=3, rejected=1, reason_codes=['TIME_ORDER_VIOLATION']`，
坏条 `state=ACCEPTED, next_attempt_ms=now+30s, last_error=TRANSITION_REJECTED:TIME_ORDER_VIOLATION`，
3 条正常条全部 `DELIVERED`；退避窗口内再派 3 次 **额外提交 0 次**（§C3b）。
**这就是"超容差按单条隔离 + 退避"应当的行为，已成立。**

**另注（P1-4 的机理）**：`_bind_receipt` 对"空白回执"刻意抛**裸 `ValueError`**
（docstring 明说"既有负例（空回执）仍拿到含 field 名字的 ValueError"），
而 `dispatch_due` 的 except 只写 `except (AlertLadderViolation, AlertReceiptRejected)`。
`AlertReceiptRejected` 是 `ValueError` 的**子类**，但**裸 `ValueError` 不是 `AlertReceiptRejected`**，
故 `isinstance` 匹配失败、异常穿出。同理 `int(outcome.delivered_at_ms)`（`dispatch_due` 内、
`try` 之内）对非数值抛出的 `ValueError/TypeError` 也不在捕获集合里。

详见 §2 问题清单 P1-4。

### 1.2 第 2 项 P1-2（ops_status fail-open）— **闭合**

探针 `probe_q2_ops_p2p3.py`，前后两树逐场景对照（`_scratch_Q/logs/q2_before.log` / `q2_after.log`）：

| 场景 | 修复前 `68903f8` | 修复后 `6ef116b` | 判定 |
|---|---|---|---|
| 空队列 + 仅配置 `verified=true` | **`OPS_OK`**，`reasons=[]`（fail-open） | `OPS_DEGRADED`，`['NO_CONFIRMED_DELIVERY']` | ✅ 不再 fail-open |
| 真健康（1 条 `DELIVERED`，`pending=0`） | `OPS_OK` | **`OPS_OK`** | ✅ **不误报** |
| 历史 `DELIVERED` + 当前 `UNKNOWN` 卡住 @ t+10s | **`OPS_OK`** | `OPS_DEGRADED` + `NO_CONFIRMED_DELIVERY` | ✅ 不再被历史行掩盖 |
| 同上 @ t+299s | **`OPS_OK`** | `OPS_DEGRADED` | ✅ |
| 同上 @ **t+300s（边界）** | **`OPS_OK`** | `OPS_DEGRADED`（`backlog=300.0`，未超期） | ✅ 边界同口径 |
| 同上 @ t+301s | `OPS_BLOCKED`(BACKLOG) | `OPS_BLOCKED`(`BACKLOG_OVERDUE`+`NO_CONFIRMED_DELIVERY`) | ✅ |
| 历史 `DELIVERED` + 当前卡住的 `ACCEPTED` | **`OPS_OK`** | `OPS_DEGRADED` + `NO_CONFIRMED_DELIVERY` | ✅ |
| 仅人工确认（`ACKNOWLEDGED`，`pending=0`） | `OPS_DEGRADED` `MANUAL_ACK_ONLY` | 同 | ✅ 无回归 |
| 未验证通道 + 已送达 | `OPS_BLOCKED` `CHANNEL_UNVERIFIED` | 同 | ✅ 无回归 |

判据实现（独立读出 `alert_delivery.py:1774`）确为 `if pending > 0 or not confirmed_states:`，
其中 `pending = UNKNOWN+SUBMITTED+ACCEPTED`（**只看当前未闭合项**），
`confirmed_states` 由全表 `states()` 聚合但仅在 `pending == 0` 时才可能不触发——语义与 §5.1 一致。

**残留观察（不列为问题）**：稳态"从未发生任何告警"的空队列现在恒为 `OPS_DEGRADED`。
这是规格明确要求的口径（配置不是送达证据），README §5.1「修复后 4a」已如实写出，
且 `ops_status` 无生产消费者，故不构成缺陷；但 U12 接线时需注意这会产生持续的 DEGRADED 噪声。

### 1.3 第 3 项 P1-3（回执未绑定 / 无上界 / 类型不符）— **闭合**

探针 §C5b / §C5c / §C5d / §C6，全部经**直接 API**与**经 transport** 两条路各验一遍：

| 构造 | 期望 | 实测 | 行终态 |
|---|---|---|---|
| 他消息回执（`bound_alert_id="alert-deadbeef…"`） | 拒绝 | `AlertReceiptRejected: RECEIPT_BINDING_MISMATCH` | `UNKNOWN`（未提升） |
| 跨 incident 回执（经 transport `receipt_alert_id="alert-someone-else"`） | 拒绝 | `rejected=3, reasons=['RECEIPT_BINDING_MISMATCH']` | `SUBMITTED` |
| `bound_alert_id=None`（缺省） | fail-closed | `RECEIPT_UNBOUND` | `UNKNOWN` |
| `bound_alert_id=""` | fail-closed | `RECEIPT_UNBOUND` | `UNKNOWN` |
| `bound_alert_id=12345`（非字符串） | fail-closed | `RECEIPT_UNBOUND` | `UNKNOWN` |
| **大小写变体** `ALERT-C890…` | 拒绝 | `RECEIPT_BINDING_MISMATCH`（**严格相等，不做大小写归一**） | `UNKNOWN` |
| **空白变体** `" "+aid+" "` / `aid+"\n"` | 拒绝 | `RECEIPT_BINDING_MISMATCH`（**不 strip**） | `UNKNOWN` |
| 完全一致 | 接受 | `DELIVERED`，`receipt_bound_alert_id` **落库**为该 id | `DELIVERED` |
| `device_receipt=12345` | 拒绝 | `AlertReceiptRejected: RECEIPT_TYPE_INVALID`（**无 `str()` 强转**） | `ACCEPTED` |
| `device_receipt=None` / `[]` / `""` | 不伪造送达 | `reason_codes=['DELIVERED_WITHOUT_DEVICE_RECEIPT']`，`rejected=0` | `ACCEPTED` |
| `delivered_at_ms=now+10**12` | 拒绝 | `RECEIPT_TIMESTAMP_FUTURE` | `ACCEPTED` |
| `delivered_at_ms=now-10**12` | 拒绝（下界交 K05） | `TIME_ORDER_VIOLATION` | `ACCEPTED` |

- 全部拒绝路径**行都不进 `DELIVERED`**（停在 `UNKNOWN` 或 `ACCEPTED`/`SUBMITTED`）✅
- `receipt_bound_alert_id` 确实写进 **U14 自己的队列文件**（`_ensure_columns` 幂等
  `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`），不是共享列、不进 `schema_migrations` ✅
- **补丁 03（审计镜像）同步**（README §1.1 声称）：独立实测 `record_alert_delivery`：
  正确绑定 → 插入成功（`rows=2`）；缺 `bound_incident_key` → `RECEIPT_UNBOUND`；
  `bound_incident_key="inc-OTHER"` → `RECEIPT_BINDING_MISMATCH`；`provider_receipt=12345` →
  `RECEIPT_TYPE_INVALID`；仅 `submitted_at_ms`（无回执）→ 正常插入。✅ 与队列侧同口径。

### 1.4 第 4 项 P2/P3 抽验 — **8/8 通过**；登记文字属实

| 项 | README 声称 | 独立实测（`68903f8` → `6ef116b`） | 判定 |
|---|---|---|---|
| **P2-1** | `process_alive is None` 追加 `PROCESS_UNVERIFIED`，`kind` 不变 | `None` → `kind=PROCESS_MISSING, codes=['PROCESS_MISSING','PROCESS_UNVERIFIED']`（前：只有 `PROCESS_MISSING`）；`False` → 仍只有 `PROCESS_MISSING`；`True` → `TRADING_STALLED` | ✅ |
| **P3-1** | 常量名==值 + 旧名同值别名 | `MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED == "MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED"` = True；`MANUAL_ACK_REQUIRED` 存在且同值（前：值不符、名字是 `MANUAL_ACK_CONFIRM_REQUIRED`） | ✅ |
| **P3-2** | `record_manual_ack` 收敛为元组 | 对已 `DELIVERED` 用 `now-60s` 确认 → 返回 `(False, ('TRANSITION_REJECTED','TIME_ORDER_VIOLATION'))`，状态仍 `DELIVERED`（前：**抛** `AlertLadderViolation`） | ✅ |
| **P3-3** | 证据时间晚于 `now` 即 `TRADING_EVIDENCE_FROM_FUTURE` | `+4s` → `healthy=False, codes=['TRADING_EVIDENCE_FROM_FUTURE']`；`+1h` 同；`-1s`（正常过去）→ `HEARTBEAT_OK` | ✅ 已修（但见下注） |
| **P3-4** | `SUBMITTED` 也排复查 | 第一次派发 `submissions=1, next_attempt_ms=now+300000`；`t+1ms` 再派发 → `submissions` 仍 1、返回全 0（前：`submissions=2, next_attempt_ms=NULL`） | ✅ |
| **P3-5** | 负 uptime 归一为不可得 | `-5.0` → `kind=HOST_CONTINUITY_UNKNOWN, codes=[…,'PROCESS_UNVERIFIED','HOST_CONTINUITY_UNKNOWN']`（前：`HOST_JUST_BOOTED`）；`0.0`/`30.0` 仍 `HOST_JUST_BOOTED`（宽限语义未变） | ✅ |
| **P3-5b** | 回执 `str()` 强转被类型校验取代 | 见 §1.3（`RECEIPT_TYPE_INVALID`，无强转） | ✅ |
| **P2-2** | **如实登记，本包不改** | 实测 `sb/after` 的 `scripts/run_offline_regression.py` 与 `sb/before` **逐字节相同**（sha256 一致，见 §4），确未越界改 I 独占文件 | ✅ 登记属实 |
| **P2-3** | **如实登记**：全树无生产消费者 | 独立 grep：`AlertQueue(` 构造点 **全部**在 `tests/acceptance/test_alert_delivery.py`(14)、`scripts/smoke_u14_alert_delivery.py`(7)、README 文本(1)；`core.ops.alert_delivery` 的生产导入方只有 `src/core/health_watch.py`（只 import 只读探针） | ✅ 登记属实 |

> **P3-3 的残留口径差**：README §5.2 写「证据时间晚于**本地 `now`**（或晚于 `emitted`）」。
> 实现是 `if evidence > emitted or evidence > int(now_ms)`。实测 `evaluate_heartbeat(payload, now_ms=…)`
> 的 `now_ms` 缺省会取 `_now_ms(None)`（真实时钟）。**我在探针里必须显式传 `now_ms`**；
> 若调用方不传（用真实墙钟），"未来 4 s"的证据在真实时间里自然进不了未来分支——
> 这是**预期**的（真实时钟下证据确实不晚于 now），不算缺陷，但消费者需知道该函数对 `now_ms`
> 的取值敏感。属**观察项**。

### 1.5 第 5 项 补丁与 delta — **通过**

见 §4。`apply --check`：`59660b2` 与 `e2376f7` 两树 × 两种 git 配置 × 4 条补丁 **全 rc=0**；
当前集成 HEAD `ab58277` 上 01/02/03 **rc=0**，**04 rc=1**（因 I 已提前落地该登记行，属预期，见 P3-7）。
全量回归 delta：`before`(68903f8+7c430c8) `80/80 FAIL=0`，`after`(6ef116b+a89eaa5) `80/80 FAIL=0`，
**失败集合差集 = ∅**，基线全绿、**新增失败 0**。§1.1 记录的一类畸形回执**不在既有回归覆盖面内**，
故不会体现为 delta 失败（这正是它需要靠人工反例发现的原因）。

### 1.6 第 6 项 门禁 — 见 §6

### 1.7 第 7 项 回归抽样 — 见 §5

---

## 2. 问题清单

### 【新增 P1-4】单条隔离的 except 集合不完整：通道异常与一类畸形回执仍会中断整批并每轮复发

> **P1-1 未完全闭合。** F r1 P1-1 的根因是"异常从 `dispatch_due` 穿出（未收敛为返回字典，
> 也未 `continue`），for 循环被中断"。r2 把 `transport.submit(...)` 搬进了 `try`，却只把
> `except` 写成 `(AlertLadderViolation, AlertReceiptRejected)`——**捕获集合小于该函数自身与
> 其唯一外部依赖（transport）能抛出的异常集合**，根因仍在。

- 位置：`src/core/ops/alert_delivery.py:1626`（`except (AlertLadderViolation, AlertReceiptRejected)`）
  - `:1581`（`outcome = transport.submit(...)`，在 `try` 内但异常不被捕获）
  - `:1400`（`raise ValueError(f"{field} required: 回执不得为空")`，裸 `ValueError`）
  - `:1604`（`int(outcome.delivered_at_ms)`，非数值时 `ValueError`/`TypeError`）

**向量 A（最现实，且 r2 完全未改）：通道抛异常 ⇒ 整批中断 + 无退避 + 每轮复发**

```
class Sink:  # 第 2 条起正常返回 DELIVERED 回执，只有第 1 条抛网络异常
    def submit(...):
        if alert.incident_key == "bad": raise TimeoutError("gateway timeout")
队列 3 条（bad/ok1/ok2），channel=verified
dispatch_due(..., now_ms=NOW)
-> TimeoutError 穿出 dispatch_due
   队列：{'UNKNOWN': 3}                 # ok1/ok2 一条都没投递（transport 只被调用 1 次）
   bad 行：attempts=0, next_attempt_ms=0, last_error=NULL   # _record_failure 从未执行
   +3600s 再派发 -> 再次 TimeoutError（同一异常，永久卡死）
```

`TimeoutError` / `RuntimeError` / `OSError` 三种**逐字相同**（`q4/after/q4_result.json`），
且 `sb/68903f8` 上结果**完全一致** ⇒ 这不是 r2 引入的回归，而是 r2 **声称修好的那类缺陷
（整批中断 + 每轮复发）在 transport 侧的残留**。真实网关一旦接入（U12），一次超时/连接重置/
DNS 抖动就会让**整条告警投递通路永久停摆**——比 F r1 收到的"供应商时钟早 1ms"更常见。

**向量 B：畸形回执（transport 正常返回但字段畸形）**

```
provider_receipt="   "（三个空格，truthy）-> ValueError: provider_receipt required: 回执不得为空
device_receipt="   "                      -> ValueError: device_receipt required: 回执不得为空
delivered_at_ms="not-a-number"            -> ValueError: invalid literal for int()
delivered_at_ms=[1,2]                     -> TypeError: int() argument must be ...
四种情形均：异常穿出 -> 队列 {'SUBMITTED': 1, 'UNKNOWN': 2} -> 该行
attempts=1, next_attempt_ms=NULL, last_error='' -> +3600s 再派发 -> 再次抛（每轮复发）
```

机理：`_bind_receipt` 对**空白回执**刻意抛**裸 `ValueError`**（`:1399-1400`，
docstring 明说"既有负例（空回执）仍拿到含 field 名字的 ValueError"）。
`AlertReceiptRejected` 虽继承 `ValueError`，但 `except AlertReceiptRejected`
**不匹配裸 `ValueError`**；`int(...)` 的非数值 `ValueError/TypeError` 同理会漏网。
异常穿出后 `_record_failure` 不执行 ⇒ `next_attempt_ms` 保持 `NULL`/`0` ⇒ 该行下一轮
**立即到期**，而通道行为不变 ⇒ **每轮派发每轮抛、每轮中断整批**。

- 影响：与 F r1 的 P1-1 **逐条等价**（整批中断 + 该告警永远送不达 + 每轮复发），且**未登记**。
- 期望处置（任一即可，建议 1+3）：
  1. `dispatch_due` 的单条隔离改为**兜底式**：`except (AlertLadderViolation,
     AlertReceiptRejected)` 之外再 `except Exception as exc:` → 记
     `INTERNAL_ERROR`/`TRANSPORT_FAILED` + `_record_failure` + `continue`；
     "单条异常不得中断整批"必须对**任意**异常成立，而不是对白名单成立。
  2. `transport.submit(...)` 单独包一层 try，把通道异常显式转成
     `outcome = TransportOutcome(ok=False, error=f"TRANSPORT_EXCEPTION:{type(exc).__name__}")`。
  3. 把 `_bind_receipt` 的空白回执改成 `AlertReceiptRejected((RECEIPT_TYPE_INVALID,))`
     （或新增 `RECEIPT_EMPTY`），并让 `int(outcome.delivered_at_ms)` 走类型校验而非裸 `int()`。

### 【新增 P3-6】`_record_failure` 对阶梯/回执拒绝写 `TRANSPORT_FAILED` 审计标签

- 位置：`src/core/ops/alert_delivery.py:1674`（`self._audit(alert_id, TRANSPORT_FAILED, …)`）
- 实测：混批拒绝时该行 `alert_audit` 出现 `transition='TRANSPORT_FAILED'`，
  而 note 是 `error=TRANSITION_REJECTED:TIME_ORDER_VIOLATION`、`last_error` 也是
  `TRANSITION_REJECTED:*`。按 `transition` 列过滤日志的消费者会把"阶梯拒绝"误读成"传输失败"。
- 建议：`_record_failure` 增加 `transition` 形参，阶梯/回执拒绝传 `TRANSITION_REJECTED`。

### 【新增 P3-7】补丁包 README 两处引用不准

1. §5.2 P2-2 行引用 `scripts/smoke_u14_alert_delivery.py:69-70`，实际清空
   `MTA_STATE_ROOT`/`MTA_DATA_ROOT` 的语句在 **:72-75**（:69-70 是 `check()` 里的失败分支打印）。
   引用名称正确、行号差 3–5 行。
2. §1 的应用示例写 `BASE=…/mat-wt/I  # 集成 HEAD e2376f7`。实测当前集成 HEAD
   `ab58277` **已包含** U14 登记行，故补丁 04 在那里 **`apply --check` 失败**
   （`patch does not apply`，见 §4.1）；补丁 01/02/03 仍 rc=0。建议把示例改成
   "在补丁基线 `59660b2` / `e2376f7` 上应用；已登记 U14 的集成树上只需 01/02/03"。

### 【观察，不列 P 级】`src/audit/database.py` 反向依赖 `core.ops.alert_delivery`

打补丁后 `AuditDatabase.record_alert_delivery` 在函数体内
`from core.ops.alert_delivery import RECEIPT_BINDING_MISMATCH, RECEIPT_TYPE_INVALID, RECEIPT_UNBOUND`。
延迟导入避免了模块级循环，功能正确；但审计层因此依赖 ops 层的**字符串常量**（跨层重复定义
原因码字面量）。建议后续把这三个原因码提到一个中立的契约/常量模块。

---

## 3. 补丁审读与 `apply --check`（第 5 项上半）

### 3.1 补丁内容核对（r1 → r2 是否真如 README §1 所写）

对 `sb/after`（`6ef116b` + `a89eaa5`）与 `sb/before`（`68903f8` + `7c430c8`）打补丁后的共享文件做
sha256 比对：

```
SAME  src/core/health_watch.py            (493e4fa48b4b66c4)
SAME  scripts/health_watch.py             (97524d9ae25046a9)
SAME  scripts/run_offline_regression.py   (39893aff24759605)
DIFF  src/audit/database.py               after=3f6953228135f642  before=56781af2a5876bda
```

⇒ README §1「补丁 01/02/04 逐字节未变，03 同步更新」**属实**。

### 3.2 `apply --check`（两配置 × 三树 × 4 条）

`git -C <tree> [-c core.autocrlf=false -c core.eol=lf] apply --check ../../_patches/a89eaa5/<p>.diff`
（补丁在仓库根执行，避免子目录静默 no-op；`_scratch_Q/logs/applycheck.log`）：

| 树 | 配置 | 01 | 02 | 03 | 04 |
|---|---|---|---|---|---|
| `59660b2`（补丁基线） | strict（`autocrlf=false,eol=lf`） | OK | OK | OK | OK |
| `59660b2` | 默认（本机 `core.autocrlf=true`） | OK | OK | OK | OK |
| `e2376f7`（README 声称的集成 HEAD） | strict | OK | OK | OK | OK |
| `e2376f7` | 默认 | OK | OK | OK | OK |
| `ab58277`（**当前**集成 HEAD） | strict | OK | OK | OK | **rc=1: patch does not apply** |
| `ab58277` | 默认 | OK | OK | OK | **rc=1: patch does not apply** |

`ab58277` 上 04 失败的**原因已定位且属预期**：该树 `scripts/run_offline_regression.py:101-102`
**已经含有** U14 登记行（注释 + `"scripts/smoke_u14_alert_delivery.py",`），I 已把 r2 落地。
即"补丁已应用后再 apply"必然失败，不是补丁缺陷。→ 记为文档问题 **P3-7.2**。

**落盘面**：`01/02/03` 在 `59660b2`/`e2376f7` 上 `git apply` 成功；`04` 追加 2 行（1 行注释 + 1 行登记）。
（注：`git diff --stat` 在 `-c core.autocrlf=false` 下会把 `src/audit/database.py` 的既有 CRLF 行
全部算成改动（显示 1605 行），是**本仓库混合换行**下的测量伪影，非真实落盘面；
真实增量由 §3.1 的字节比对与 §3.3 的 sha256 确认。）

### 3.3 打补丁后的共享文件是否等于作者工作树

无提交引用（作者未把共享改动提交进 `task/U14`），故改以**行为等价**验证：
`src/audit/database.py` 的 `alert_deliveries` 表结构、K05 派生、以及 §1.3 的镜像绑定负例
全部按 README §1.1 所述工作（实测见 §1.3 末表）。`scripts/health_watch.py --state-root`
的 fail-closed 行为在 r2 未变（补丁 02 逐字节相同）。

### 3.4 补丁 03 的检查顺序（README §1.1 兼容性声明的核对）

`sb/after/src/audit/database.py:1368-1381`：`DELIVERED requires a non-empty device receipt`
的检查（:1368）**先于**回执绑定检查（:1372-1381）。⇒ README §1.1「负例仍先命中'缺 device_receipt'
那条，错误串不变」**准确**。

**观察**：镜像侧用 `bound.strip() != incident`（**做 strip**），队列侧用
`bound_alert_id != alert_id`（**不 strip**）。两层都自称"同口径"，实际归一策略不同：
队列侧把 `" <id> "` 判为 `RECEIPT_BINDING_MISMATCH`，镜像侧会判为**通过**。
建议统一（任一方向都可，但要显式写清）。属 P3 观察项，不单列编号。

---

## 4. 全量回归 delta（第 5 项下半，强制项）

### 4.1 沙箱与 harness

- `sb/before` = `git archive 68903f8` + `7c430c8` 的 01–04（= F r1 的 s2 态）
- `sb/after`  = `git archive 6ef116b` + `a89eaa5` 的 01–04（= 本 r2 候选态）
- harness `probes/harness_q.py`（**Q 自建**，非 fail-fast）：用 `importlib` 读被侧树的
  `scripts/run_offline_regression.py` 取 `SCRIPTS` 清单，逐行复制其 `main()` 的 env 构造
  （`EXECUTION_MODE=paper`、`pop MTA_PROFILE`、`MTA_NO_PROXY=1`、UTF-8、按名清 `*KEY*/*SECRET*/*TOKEN*`），
  并对**两侧统一**额外 `env.pop("MTA_STATE_ROOT"/"MTA_DATA_ROOT")`，逐脚本 `subprocess.run` 记返回码。

### 4.2 结果

| 树 | 官方入口 | 逐脚本 |
|---|---|---|
| `sb/before` | `rc=0  OFFLINE REGRESSION OK scripts=80` | `TOTAL=80 PASS=80 FAIL=0`，`failed set: []` |
| `sb/after`  | `rc=0  OFFLINE REGRESSION OK scripts=80` | `TOTAL=80 PASS=80 FAIL=0`，`failed set: []` |

- **失败集合差集 = ∅**（`fail→pass` 与 `pass→fail` 皆空）；**基线全绿**、**新增失败 0**。
- 两轮各 192.7 s / 192.5 s，**并行**执行（两侧同时跑，负载约为单轮 2 倍）；
  `scripts/smoke_llm_deadline.py` 在两轮里 **rc=0**（F r1 未复现的抖动本轮亦未复现）。
- 原始输出：`_scratch_Q/logs/delta_before.log`、`delta_after.log`；结构化
  `_scratch_Q/delta/before_result.json`、`after_result.json`。

### 4.3 关于 I 的 `env.pop` 硬化是否影响本对照（任务点名）

**不影响**，三条独立证据：
1. 本会话环境本身**未设置** `MTA_STATE_ROOT`/`MTA_DATA_ROOT`/`MTA_PROFILE`
   （`env | grep -i MTA_` → 空；`MTA_STATE_ROOT=[<unset>]`）。
2. 两侧沙箱的 `scripts/run_offline_regression.py` **逐字节相同**（sha256 `39893aff…`），
   harness 又对两侧施加**同一份** env 构造 ⇒ 对照是同 env 对照。
3. 直接用**未改环境的官方入口**跑两棵树（`logs/entry_before.log` / `entry_after.log`）
   同为 `rc=0 / scripts=80`，与 harness 结论一致。

（附：I 在 `ab58277` 的改动是 `main()` 里 4 行，见 `git -C ../I show ab58277 --
scripts/run_offline_regression.py`；方向正确性本报告不评价，属 I 归属。）

---

## 5. 第 7 项 回归抽样（≥5，Q 自建探针 `probe_q3_sample.py`）

6 项主题全部独立复现，**全部符合规格**（原始输出 `logs/q3_after.log` / `q3_result.json`）：

| # | 主题 | 独立构造 | 实测 |
|---|---|---|---|
| 1 | **三级阶梯分开（R14）** | HTTP200 单独出现；空 `provider_receipt`；无 `provider_receipt` 直接 DELIVERED；再走正规三级 | `http200_only→SUBMITTED`；`empty_provider_receipt→ValueError`；`skip_to_delivered→AlertLadderViolation` 且状态**保持 SUBMITTED**（未落库）；正规路径 `SUBMITTED→ACCEPTED→DELIVERED` |
| 2 | **两成因分开（R13）** | 6 组注入 | `PROCESS_MISSING`（uptime≫age）/`HOST_DOWN`（uptime≪age）/`HOST_CONTINUITY_UNKNOWN`（uptime 不可得）/`TRADING_STALLED`（存活 + age=121s）/`HEALTHY`（age=**120s 边界不陈旧**）/证据缺失也判 `STALLED`（绝不判健康） |
| 3 | **OPS_BLOCKED 不伪造送达** | 未验证通道派发；仅 HTTP200；`drill=True` | 未验证：`transport_calls=0`、状态留 `UNKNOWN`、`delivered=0`、`OPS_BLOCKED`；仅 HTTP200：`OPS_DEGRADED`+`NO_CONFIRMED_DELIVERY`、`delivered=0`；drill：提交 1 次但状态停在 `SUBMITTED`、`delivered=0` |
| 4 | **心跳带交易证据时间** | 7 组 | 缺证据→不健康；`emitted` 回退→`HEARTBEAT_REPLAY_OUT_OF_ORDER`（拒）；序号持平→`HEARTBEAT_SEQUENCE_REPLAY`（拒）；证据回退→`TRADING_EVIDENCE_REPLAY`（拒）；**无界**维护窗口→`MAINTENANCE_WINDOW_UNBOUNDED`（`accepted=False`）；**有界**窗口→`muted=True`、`accepted=True` 但 `healthy=False`；基线 `HEARTBEAT_OK` |
| 5 | **队列独立于交易库** | 交易目录快照（mtime+size+sha256）；容量 50 灌 200 条；队列文件损坏；状态根不可写 | 交易目录快照**不变**；`count=50, dropped=150`（有界+可审计）；损坏队列→`sqlite3.DatabaseError`（响亮失败）且交易目录**仍不变**；父路径是文件→`FileExistsError` |
| 6 | **人工确认可审计** | 缺 note / `confirm=False` / `auto` actor / 未提交先确认 / 合法 / 重复 | 前两者→`MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED`（**常量名==值**）；`"  AUTO "`→`MANUAL_ACK_ACTOR_INVALID`；未提交→`MANUAL_ACK_WITHOUT_SUBMISSION` 且**只写审计不改状态**（state 仍 `UNKNOWN`，trail 含该行+actor）；合法→`ACKNOWLEDGED` 且 `delivered_at_ms=NULL`/`provider_receipt=NULL`（**不伪造送达**），审计含 `(ACKNOWLEDGED, human-op, "end-to-end drill ok")`；重复确认幂等返回 `(True, [])` |

---

## 6. 第 6 项 门禁（在已打补丁的沙箱里跑）

| 脚本 | 树 | 结果 |
|---|---|---|
| `scripts/check_quality.py` | `sb/after` | `rc=0` — `SECRET SCAN OK files=440 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0` / `QUALITY CHECK OK files=264 untracked=0` |
| `scripts/scan_tracked_secrets.py` | `sb/after` | `rc=0` — `SECRET SCAN OK files=440 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0` |
| `scripts/check_quality.py` | `sb/before`（对照） | `rc=0` — 同上（files=435） |

**两门禁全绿**，且 before/after 一致（无新增未跟踪/未登记文件）。
原始输出：`logs/gate_quality.log`、`logs/gate_secrets.log`、`logs/gate_quality_before.log`。

---

## 7. 未覆盖 / 无法离线验证（明说）

1. **真实通道送达**：`verified` 通道只存在于夹具与内存 `RecordingSink`；真实回执、真实升级路径、
   宿主断电/失联演练属 **U12 OPS（D02）**。README §7.1 已如实声明，我未也无法验证"真的能联系到人"。
2. **`AlertQueue` 无生产消费者（P2-3，已登记）**：独立 grep 确认 `AlertQueue(` 的全部构造点都在
   验收/冒烟/README 里，生产导入方只有 `src/core/health_watch.py`（只 import 只读探针）。
   ⇒ **不得因为本包测试全绿就认为"无人值守外部告警送达"已可用**；§1.1.4 的 P1-4 正是在
   **真实 transport 接入后**才会被触发的输入面，U12 接线时必须带故障注入复验。
3. **`host_uptime_seconds` 的平台性质**：Linux 读 `/proc/uptime`，其他平台退回 `time.monotonic()`；
   离线只用注入值，未在真实宿主做断电演练（README §7.2 已登记）。
4. **多进程并发写队列 / 队列写阻塞压力模型**：未做（README §7.5 已登记）。
5. **`smoke_llm_deadline` 抖动**：本轮两轮并行（≈2× 负载）下均 rc=0，未复现；F r1 已证明该脚本
   与本包三个目标文件无执行路径重叠。若后续轮次复现且能定位到本包路径，应升级。
6. **未设置 `MTA_STATE_ROOT` 的机器上的 delta 复跑**：本机未设置（见 §4.3），故未复跑
   "变量已设置"场景；P2-2 的影响面已由 F r1 用直接探针证明，本包未改该文件（§3.1 sha256 相同）。
7. **`_bind_receipt` 空白回执的裸 `ValueError` 是有意设计还是疏漏**：作者 docstring 明说是
   "刻意保留"（为了不破坏既有空回执负例的错误串），但**未意识到**它与 `dispatch_due` 的
   `except` 集合不匹配（P1-4）。我按"后果定级"，未替作者推断意图。

---

## 8. 命令清单（可复现）

所有命令在 `mat-wt/reviews/U14/R3/r1/` 下执行；`MW=$(cd /c/Users/Tao/Desktop/*/mat-wt && pwd)`，
`SC="$MW/reviews/U14/R3/r1/_scratch_Q"`，`PY="<venv>/Scripts/python.exe"`。

```bash
# 沙箱
mkdir -p $SC/sb/after $SC/sb/before
(cd $MW/EF && git archive 6ef116b) | tar -x -C $SC/sb/after
(cd $MW/EF && git archive 68903f8) | tar -x -C $SC/sb/before
cp -r $MW/EF/docs/dispatch/patches/U14/a89eaa5 $SC/sb/_patches/a89eaa5
cp -r $MW/EF/docs/dispatch/patches/U14/7c430c8 $SC/sb/_patches/7c430c8
for t in after:6ef116b:a89eaa5 before:68903f8:7c430c8; do
  T=${t%%:*}; rest=${t#*:}; P=${rest##*:}
  (cd $SC/sb/$T && git init -q . && git add -A)
  for f in 01_core_health_watch 02_scripts_health_watch \
           03_audit_database_alert_deliveries 04_run_offline_regression_registration; do
    git -C $SC/sb/$T -c core.autocrlf=false -c core.eol=lf apply "../_patches/$P/$f.diff"
  done
done

# 1) P1-1 / 绕过尝试
cd $SC/probes
PYTHONUTF8=1 $PY probe_q1_p11.py $SC/sb/after    $SC/q1/after  > $SC/logs/q1_after.log  2>&1
PYTHONUTF8=1 $PY probe_q1_p11.py $SC/sb/68903f8  $SC/q1/before > $SC/logs/q1_before.log 2>&1
# 2) P1-2 / P2 / P3 / 补丁 03 镜像
PYTHONUTF8=1 $PY probe_q2_ops_p2p3.py $SC/sb/after   $SC/q2/after  > $SC/logs/q2_after.log  2>&1
PYTHONUTF8=1 $PY probe_q2_ops_p2p3.py $SC/sb/68903f8 $SC/q2/before > $SC/logs/q2_before.log 2>&1
# 3) 回归抽样
PYTHONUTF8=1 $PY probe_q3_sample.py $SC/sb/after $SC/q3/after > $SC/logs/q3_after.log 2>&1
# 4) 全量 delta（两侧同 env，非 fail-fast）
PYTHONUTF8=1 $PY harness_q.py $SC/sb/before $SC/delta/before_result.json > $SC/logs/delta_before.log 2>&1
PYTHONUTF8=1 $PY harness_q.py $SC/sb/after  $SC/delta/after_result.json  > $SC/logs/delta_after.log  2>&1
# 5) 官方入口 / 两态套件 / 门禁（见 probes/run_rest.sh）
bash $SC/probes/run_rest.sh > $SC/logs/run_rest.log 2>&1
# 6) apply --check 两配置 × 三树（见 logs/applycheck.log）
```

---

## 9. 标尺自评

- 残留项为 **P1-4（未登记的整批中断 + 每轮复发）**，属"未登记的崩溃/停摆" ⇒ 触发
  `CHANGES_REQUESTED` 判据。触发面不只是构造性输入：**通道抛任何异常**（超时/连接重置/
  DNS/TLS）都会命中，这是 U12 接线真实网关后的常态风险。另有 **P3-6**（审计标签语义）与
  **P3-7**（README 两处引用）为新增未登记项。
- 除上述外：P1-2/P1-3 真闭合，P2-2/P2-3 登记文字准确，无伪造送达，无 fail-open，
  全量 delta 无新增失败，门禁全绿。
- 结论：**CHANGES_REQUESTED**（P1-4 修完即可转 GO；建议一并处理 P3-6/P3-7）。


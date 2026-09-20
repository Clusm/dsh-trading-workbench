# U04 初审报告 — A 角色（账本：数量 / 费用守恒）

- 审查对象 head：`8eb0480c6bb78c4e527abfd301209fdaf11ef6e5`（分支 `task/U04`），基线 `ffdab44`
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U04\A\r1`（detached，独立会话，只读）
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 探针与原始输出：`_scratch_A/probe1..4*.py` + `_scratch_A/probe1..4*.txt`、`author_acceptance.txt`、`smoke_u04.txt`
- 我自行复核（不采信作者的"92 通过"）：`tests/acceptance/test_execution_faults.py` exit=0（92/0）；`scripts/smoke_u04_execution_faults.py` exit=0（含篡改 golden 的负向对照）

## 结论：**CHANGES_REQUESTED**

模块本体（`protection_commands.py`）质量高，**三分量守恒在我能构造的全部路径上真成立、全程 Decimal**，未发现 P0。
但保护层有两处真实缺口（P1-A1/P1-A2），且共享补丁有 3 处会在真实路径上失效（其中 2 处直接导致"裸仓被保留"或"成交无法入账"）。
若补丁 P1 全部按 I 的独立项跟踪，模块本体修掉 P1-A1/P1-A2 后可降级为"有条件 GO"。

## 需求复核要点（现读，非采信转述）
- K04 `OrderQuantity`：`accepted = filled + working + terminal`；`NEW→filled=0`、`PARTIALLY_FILLED→0<filled<accepted`、`FILLED→filled=accepted`；`FILLED` 不可撤；`REJECTED` 的 accepted/filled 必须为 0；`apply_fill` 的 fill 级幂等**由调用方传 `applied_fill_ids`**（`k04_management.py:438-487`）。→ 本模块把"重复 fill_id"的判定外包给调用方，是契约允许的，但下游必须真的持久化该集合（见 P2-A9/P2-P1）。
- `docs/contracts/README.md` §6 明确：**授权过期只限制新提交，不阻断按原 client ID 的 UNKNOWN 查单与对账**（§10.2）→ 本模块"只查不重发"符合该口径。

---

## 问题清单

### P1-A1（模块）| `can_cancel_protection` 把"未确认归零"当"已确认归零"，允许撤掉最后一道保护
`src/exchange/protection_commands.py:1153-1154`
```python
if flat_confirmed or position.is_flat:
    return True, "FLAT_CONFIRMED_CLEANUP"
```
`position.is_flat` 即 `qty == 0`，仅来自快照数值；**与 `flat_confirmed` 无关**。同模块 `plan_protection_refresh:994-1001` 对同一情形明确拒绝（`FLAT_NOT_CONFIRMED`，不产生任何 CANCEL）。两处口径互相矛盾，且 `can_cancel_protection` 返回的 reason 文本是 `FLAT_CONFIRMED_CLEANUP`——**在未确认时谎称已确认**。
反例（probe4 §3，原始输出 `probe4.txt`）：
```
positionAmt=0, flat_confirmed=False, 仅剩一张保护单: (True, 'FLAT_CONFIRMED_CLEANUP')
positionAmt='0.00000000', 同上:                    (True, 'FLAT_CONFIRMED_CLEANUP')
positionAmt=2.0（有仓）:                            (False, 'CANNOT_REMOVE_LAST_PROTECTION')
```
危害：`positionAmt==0` 可能是快照竞态/挂单未成交/镜像滞后。此时撤掉最后一道 SL 后若有成交落地，仓位即**裸奔**（正是 `flat_confirmed` 这个参数存在的理由）。角色卡"归零未确认不清理"未成立。
建议：删掉 `or position.is_flat`，只认 `flat_confirmed`；若确有"确认归零"的另一种证明来源，作为显式入参传入并改名 reason。

### P1-A2（模块+补丁）| 健康/传输故障与保护失败的边界**不可靠可分**：未登记的健康故障会被判为 UNKNOWN 且**允许全平**
`src/exchange/protection_commands.py:1197-1200`（`_HEALTH_LIKE` 只有 4 个类）、`1244`（未识别→`UNKNOWN`）、`1266`（只拦 `_HEALTH_LIKE`）。
分类是**按名字**的有限表 + 子串匹配，默认落到 `UNKNOWN`；而 `UNKNOWN` **不在** `_HEALTH_LIKE` 里 → `guard_fault_action` 允许 `CLOSE_ALL/EMERGENCY_CLOSE`。补丁又把"非健康类即兜底平仓"写死在 `binance_futures._ensure_protection_from_order`。于是**只要健康故障的类名不在表内，就会触发全平**，直接违反角色卡"健康类不得触发全平"。
反例（probe4 §6/§8，`probe4.txt`）：
```
guard_fault_action(name, "CLOSE_ALL"):
  HEALTH_DEGRADED / WS_DISCONNECT / QUERY_FAILED / RATE_LIMITED -> BLOCKED   (符合宣称)
  TIMEOUT / RESPONSE_LOST / HTTP_503 (SUBMIT_UNCERTAIN)          -> ALLOWED
  PROTECTION_FAILED / PLACE_FAILED / UNKNOWN_FAULT / REJECTED    -> ALLOWED
分类 fuzz:
  'PROTECTION_MIRROR_STALE'    -> UNKNOWN  flat=ALLOWED
  'ALGO_ORDER_QUERY_FAILED'    -> UNKNOWN  flat=ALLOWED   (不是 QUERY_UNCERTAIN!)
  'ProtectionOrderHealthCheck' -> UNKNOWN  flat=ALLOWED
  'HEALTH_PROTECTION_FAILED'   -> UNKNOWN  flat=ALLOWED
```
另：`SUBMIT_UNCERTAIN` 的 `action=QUERY_BY_ORIGINAL_ID` 且 `blocks_new_open=True`，说明作者自己承认"POST 结果未知、订单可能已在交易所存活"；**这种状态下允许全平会二次平仓（可能反手开出反向仓）**，与"观测故障不得驱动仓位动作"的理由完全同构，却被放行。
`REASON_HEALTH_NO_FLAT_CLOSE` 只挂在 4 个类上（`1245`），所以 reason 也不能提示"这是观测故障、不该平仓"。
建议：把 `SUBMIT_UNCERTAIN | UNKNOWN` 一并纳入"不得由观测故障触发全平"的集合（或对 `UNKNOWN` 强制 `MANUAL_REVIEW` + 禁止 `FLAT_CLOSE_ACTIONS`），并把分类改为白名单（未登记即拒绝风险动作），而不是"未登记=可以平仓"。

### P1-P1（补丁 `binance_futures`）| `last_protection_sync_reason` 是跨流程、跨标的、成功后不复位的进程级残余状态 → 会**抑制该有的兜底平仓（裸仓被保留）**
`last_protection_sync_reason` 只被写入 3 处：`__init__` 置 `""`；`_resync_protection_impl` 查询失败分支置 `"QUERY_FAILED"`；`_resync_protection_impl` 末尾置 `"PROTECTION_FAILED" if failed else ""`。
读取者是**另一个**函数 `_ensure_protection_from_order`（限价单成交后的 P0 兜底路径），它用这个字段决定"是否推迟平仓"。
`_resync_protection_impl` 在这两者之间有一个**提前返回**：`if stop_ok and take_ok: return True`（源文件 `binance_futures.py:1755-1756`）——**成功路径不复位该字段**。于是：
```
周期 N   : 保护核对因查询失败 -> 字段 = "QUERY_FAILED"
周期 N+1 : 查询成功且保护匹配 -> 提前 return True -> 字段仍为 "QUERY_FAILED"
周期 N+2 : 某限价单成交，_ensure_protection_from_order 挂保护失败
           -> 读到残余 "QUERY_FAILED" -> 判为观测故障 -> 只告警、不平仓
           => 仓位无保护且不平仓（P0 裸仓）
```
同一字段还**不分标的**：A 标的的 `QUERY_FAILED` 会抑制 B 标的的兜底平仓。
作者测试被掩盖：`tests/acceptance/test_execution_faults.py:781` 直接 `broker.last_protection_sync_reason = reason` 逐次赋新值，等价于假设"每次调用前该字段都新鲜"，**恰好绕过了这个缺陷**。
建议：入参化（把 reason 作为 `_ensure_protection_from_order` 的显式参数），或至少在每次 resync 入口先置 `""`，并按 symbol 存字典。

### P1-P2（补丁 `order_outbox`）| `apply_fill_revision` 对任何"尚未落数量的行"都抛 `STATUS_FILL_MISMATCH` → **第一笔成交永远无法入账**
`patches/order_outbox.patch` 中新方法：`status = stored_status if stored_status in {NEW,PARTIALLY_FILLED,FILLED,CANCELED,EXPIRED,REJECTED} else "PARTIALLY_FILLED"`，随后 `OrderQuantity.checked(...)`。
而 `prepare()` 落库时是 `accepted_qty_text=accepted, cumulative_filled="0", working_leaves=accepted, terminal="0"`，状态 `SUBMITTING`；`acknowledge()` 也只改状态列。
复制该表达式实测（probe2 §E，`probe2.txt`）：
```
stored_status=SUBMITTING       -> status used=PARTIALLY_FILLED -> RAISED QuantityError: STATUS_FILL_MISMATCH
stored_status=ACKNOWLEDGED     -> status used=PARTIALLY_FILLED -> RAISED QuantityError: STATUS_FILL_MISMATCH
stored_status=UNKNOWN          -> status used=PARTIALLY_FILLED -> RAISED QuantityError: STATUS_FILL_MISMATCH
stored_status=PARTIALLY_FILLED (filled 仍为 "0") -> RAISED QuantityError: STATUS_FILL_MISMATCH
stored_status=NEW              -> OK filled=0.0047 status=PARTIALLY_FILLED
```
即 `PARTIALLY_FILLED` 要求 `0<filled<accepted`，而 `SUBMITTING/ACKNOWLEDGED/UNKNOWN` 这些**真实的前置状态**都被强行映射成 `PARTIALLY_FILLED`。只有"已经被 `apply_exchange_quantities` 写过非零部分成交"的行才能用；而 `apply_exchange_quantities` 与 `apply_fill_revision` **在补丁里都没有任何调用点**（全仓 grep 无调用者）→ DC04 的"重启后重放守恒"整条链路未接线、不可验证。
建议：状态由分量推导（`filled==0 → NEW`，`0<filled<accepted → PARTIALLY_FILLED`，`filled==accepted → FILLED`，终态保留原状态），而不是靠白名单 else 兜底；并补上调用点与"重启后重放同一 fill"的端到端用例。

### P2-A3（模块）| `read_submission_result` 把"已确认的部分成交 + CANCELED/EXPIRED"归为 `TERMINAL_UNFILLED`，且**不给任何 reason**
`src/exchange/protection_commands.py:799-800`
反例（probe1 §8，`probe1.txt`）：
```
CANCELED with partial fill: outcome=TERMINAL_UNFILLED filled=0.0053 confirmed=True reasons=()
EXPIRED  with partial fill: outcome=TERMINAL_UNFILLED filled=0.0053 confirmed=True reasons=()
```
`outcome` 是主分类字段，`reason_codes` 为空 → 只读 `outcome`/`reasons` 的消费者会把一笔**真实的 0.0053 成交**当作"什么都没有"，与"请求成功≠成交"是**反方向**的错账（成交被当未成交）。数据本身在 `filled_qty`/`fill_confirmed` 里没丢，但主字段误导。
建议：新增 `TERMINAL_PARTIAL`（或在 `TERMINAL_UNFILLED` 上加 `PARTIAL_FILL_PRESENT` reason），让终态带成交与终态无成交可区分。

### P2-A4（模块）| `plan_protection_refresh` 的 `CANNOT_REMOVE_LAST_PROTECTION` 分支**不可达**（声称的"撤最后保护被拒"只在另一函数成立，而那个函数有 P1-A1）
`src/exchange/protection_commands.py:1065-1074`。构造上：`cancel_candidates` 只在 `price is not None` 的循环迭代内追加，而同一迭代末尾必然追加 `PLACE`（`1053-1055`）或已追加 `KEEP`（`1045-1048`）→ `surviving` 永不为空。
穷举验证（probe4 §1）：`combos searched=513648, REASON_LAST_PROTECTION hits=0`。
所以模块内"拒绝撤最后一道保护"的唯一实际入口是 `can_cancel_protection`，而它带 P1-A1 的漏洞。补丁侧的 `execute_protection_plan`（PLACE 先于 CANCEL、PLACE 失败则 `cancel_skipped=True`）是**真实有效**的时序保证（probe4 §4 已验证：`place-fail events: [('place','STOP_MARKET')] -> canceled=(), cancel_skipped=True`），但那是"先挂后撤"，不是"拒撤最后"。
建议：要么把该分支改成可达的真实判据，要么删除并在文档中说明"最后一道保护由 PLACE-先于-CANCEL + can_cancel_protection 保证"。

### P2-A5（模块）| `KindCoverage.covered` 忽略 `active`（含 `SUBMITTING`）→ 可对"结果未知的保护挂单"宣称 Algo 已覆盖
`src/exchange/protection_commands.py:657-660`（docstring 说"没有未决命令才算已覆盖"，实现只排除 `UNKNOWN`）。
反例（probe2 §D）：
```
assert_kind_coverage([{kind=ALGO, status=SUBMITTING}], kind=ALGO, claimed_covered=True)
  -> {'total':1,'unknown':0,'active':1,'terminal':0,'covered':True}
assert_kind_coverage([{kind=ALGO, status=PARTIAL_UNKNOWN}], ...)  # 词表外状态
  -> {'unknown':0,'active':0,'terminal':1,'covered':True}
```
`SUBMITTING` 是"已 POST、结果未知"，此时保护**是否存在并不确定**，却能被 R07 覆盖声明采纳；词表外状态被计入 terminal 也是 fail-open 方向（当前 outbox 状态词表是封闭的，属潜在项）。
交叉种类本身是可靠的：仅普通记录声明 Algo 覆盖 → `CrossKindCoverageClaim`（probe2 §D）；端点/显式种类矛盾 → `CommandKindConflict`（probe2 §A）。

### P2-A6（模块+补丁）| `execute_protection_plan` 把"回调什么也没做"记成"已撤销"
`src/exchange/protection_commands.py:1142-1144` 无条件 `canceled.append(...)`；补丁的 `_cancel_action` 在 `algo_id is None`（只知 `clientAlgoId`、匹配不到 `existing` 行）时**静默 return**。
后果：账本记"旧保护已撤"，交易所侧旧保护仍在 → 重复保护 + 过期的 trigger/qty 仍会触发，且 R07 覆盖报告据此认为该单已终结。
建议：`cancel` 回调返回值语义化（返回 `False`/`None` 表示未执行），未执行的记入 `cancel_skipped`/`cancel_failed` 而不是 `canceled`。

### P2-A7（模块）| 声称的"保护确证失败仍按 P0 兜底平仓"在本模块**未实现**，`triggers_flat_close` 恒为 False
`FaultActionPlan` 的 4 个硬不变量字段全部是默认 False 且**无任何赋值点**；`_FAULT_TABLE` 全部条目实测 `distinct triggers_flat_close values = {False}`（probe4 §9）。
`guard_fault_action("PROTECTION_FAILED","CLOSE_ALL")` 返回 `triggers_flat_close=False, action="ALERT_AND_RETRY_PROTECTION"`（probe4 §7）→ **放行了全平，却在审计计划里否认发生了全平**。该能力实际只存在于未应用的补丁里（作者测试也如实打印 `PENDING ...健康故障守护未安装`）。属可审计性问题：计划对象不能作为"是否平仓"的证据。

### P2-A8（补丁 `binance_futures`）| 保护对账的**逐条容错被移除**，浮动数值会让整轮保护刷新抛错
被替换的原实现逐条 `try/except (TypeError, ValueError): continue`（`binance_futures.py:1743-1744`）；补丁改为在列表推导里直接 `protection_view_from_algo(...)` / `real_position_from_snapshot(...)`，二者对 float 一律硬拒（probe4 §10）：
```
protection_view_from_algo({'quantity': 2.0, 'triggerPrice': 100.5})
  -> RAISED QuantizationInputError: quantity 禁止浮点直传
real_position_from_snapshot({'positionAmt': -0.001})
  -> RAISED QuantizationInputError: positionAmt 禁止浮点直传
（字符串形态均正常解析）
```
`normalize_algo_orders_payload`（`src/exchange/protocol.py:42-61`）不做字符串化，原样透传 JSON 值。**离线无法判定** `openAlgoOrders`/`positionAmt` 到底返回字符串还是 JSON 数字——若为数字，保护刷新整轮异常。补丁自身对 `desired_sl/tp` 用了 `decimal_from_float`，却对交易所来源字段不用，口径不一致。
建议：对交易所来源字段统一 `decimal_from_float`（或恢复逐条容错 + 计数上报告警）。

### P3 列表
- **P3-1** `plan_unknown_reconcile:536` `int(_row_get(row,"query_attempt_count",0) or 0)` 对脏文本抛裸 `ValueError`（`'abc'`），非 fail-closed（probe2 §B）。同类 `int(...)` 对 `"3.0"` 也会炸。
- **P3-2** `ensure_execution_schema:222` 在"并发进程已加列"时仍把该列计入返回值（自称"本次新增"），且 `conn.commit()`（`223`）在调用方连接上无条件提交，可能替调用方提交未完成事务。
- **P3-3** `read_exchange_order:331` 对非文本 `origQty` 抛 `QuantizationInputError`，而 `executedQty` 被转成 `QuantityStateConflict`（`335-336`）——同函数内两种异常口径（`read_submission_result` 两者都捕获，影响仅限直接调用者）。
- **P3-4** `quantities_equal` 用相对容差 `max(1e-9, 1e-6*max)`（`252-259`）；被替换的旧实现用 `qty_eps = max(_min_qty(symbol), 1e-8)`（步长口径）。容差被放宽到 1e-6 相对 → 2.0 仓位下"差 2e-6 的保护数量"也算 `MATCHES_REAL_POSITION`。离线无 symbol filters，影响不可判。
- **P3-5** 镜像新增的 `*_text` 列**只写不读**（全仓 grep 仅测试断言其存在）：`protection_view_from_algo` 仍读 REAL 浮点列 → "所有数量路径都是 Decimal"在镜像读路径上**未成立**。建议加读取方或删列。
- **P3-6** `release_evidence.py:398` 仍全局统计 UNKNOWN（不分种类），`service.py:713` 仍读全局 `unknown_orders`；补丁新增的 `status_counts_by_kind`/`unknown_orders_by_kind` 无消费者。方向是保守的（全局 ≥ 分种类），但 R07 分账未传导到 GO 门。
- **P3-7** 测试强度：`section_dc13_unknown` 的"重启"用的是**新库文件**且从未用新连接重开同一文件；我在 probe2 §F 用"关闭 + 重开同一文件 + 新 `OrderOutbox`"复验，结论一致（见下）。另 test_execution_faults.py:781 直接赋值 `last_protection_sync_reason`，掩盖 P1-P1。
- **P3-8** `_assert_no_cancel_before_place`（`1087-1099`）当前也结构性不可达（所有 PLACE 都排在 CANCEL 之前）。它是合理的"未来回归护栏"，但不构成运行期保护。

---

## 5 个补丁的逐条意见

### 1) `tests/fixtures/execution/patches/order_outbox.patch`
- ✅ 方向正确：7 列随 INSERT 显式写入；`ensure_execution_schema` 在 `__init__` 幂等补齐；`reconcile_unknown` 改用 `classify_row` 分派查单入口（R07 落地）；`status()` 增加分种类计数与 `algo_covered_by_standard_outbox=False`。
- ❌ **P1-P2**：`apply_fill_revision` 的状态兜底使第一笔成交必抛 `STATUS_FILL_MISMATCH`（见上）。
- ❌ `apply_exchange_quantities` / `apply_fill_revision` **无任何调用者** → DC04 的"落库 + 重启重放"未接线，守恒只在内存对象上被证明过。
- ⚠ **幂等性**：fill 级幂等**是**正确接上的——读取 `applied_fill_ids_json`、`apply_late_fill` 抛 `DuplicateFillError`、并把 `applied + [fill_id]` 与数量在同一次 UPDATE 内提交（原子）。但：(a) 重复投递以**异常**表达，at-least-once 流下调用方若把异常当故障会无限重试/误告警，建议改为返回未变更行 + `DUPLICATE_FILL_IGNORED`；(b) 数量列与 `_update(status=...)` **不在同一事务**（`_update` 在 `commit()` 之后、锁之外）→ 崩溃窗口内 `quantity_revision` 前进而 `status` 滞后，且下次重放会因重复 fill_id 抛错而**永远修不回状态**。
- ⚠ 精度：`qty_text` 缺省时用 `str(qty)`（float 的 repr）写进"精确"文本列；`binance_futures` 侧 `qty_text=str(request["quantity"])` 同样是浮点转文本 → 文本列不会比上游 float 更准（`0.0047` 这类"短表示"没问题，累加/乘积型 float 会带尾）。建议上游用 `decimal_from_float` 后传文本。
- ⚠ `reconcile_unknown` 新增 SELECT `command_kind`：此处安全（`ensure_execution_schema` 先在 `__init__` 跑过），但把 SELECT 与 schema 补齐绑定成隐式顺序依赖；任何绕过 `OrderOutbox.__init__` 的同表查询都会炸（异常被循环 catch → 退避重试，表现为静默不收敛）。

### 2) `tests/fixtures/execution/patches/binance_futures.u04_adaptation.patch`
- ✅ **"先挂新后撤旧"成立且无窗口期裸奔**：`plan_protection_refresh` 产出的 `actions` 天然"全部 PLACE 在所有 CANCEL 之前"，`ordered()` 再按 PLACE<KEEP<CANCEL 排序，`execute_protection_plan` 遇到 PLACE 异常立即 return 且不再执行任何 CANCEL（`cancel_skipped=True`）。probe4 §4 用真实回调验证：PLACE 失败时事件序列只有一次 place、`canceled=()`。**这是我本轮唯一确认"完全达成"的保护承诺。**
- ✅ 两端点显式传 `command_kind`（STANDARD/ALGO），`qty_text` 取自请求文本；保护失败时 `failed_place` 分类为 `futures_sl_failed`/`futures_tp_failed`，语义未弱化。
- ❌ **P1-P1** 残余状态 `last_protection_sync_reason`（跨流程/跨标的/成功不复位）→ 抑制兜底平仓。
- ❌ **P2-A8** 交易所来源字段丢掉逐条容错 + 硬拒 float。
- ❌ **P2-A6** `_cancel_action` 在 `algo_id is None` 时静默 return，但 `execute_protection_plan` 仍记 `canceled`。
- ⚠ `float(action.trigger_price)` 在线上把 Decimal 再退回 float；仅当交易所会按 tick 复核才可接受。
- ⚠ `flat_confirmed=False` 恒传（安全侧），但与 `can_cancel_protection` 的 flat 口径不一致，建议统一。
- ⚠ 健康分支复用 `event_type="futures_sync_error"` + `action_taken="alert_only_no_flat_close"`：说是 dashboard 事件映射无需新增键，但把一个 warn 级事件类型用于 critical 决策，需确认 dashboard 的 severity 过滤不会把它降噪掉（离线无法验证）。

### 3) `tests/fixtures/execution/patches/protection_mirror.decimal_text.patch`
- ✅ `_text_decimal` 语义正确：缺失/非法/非有限 → `None`（**绝不补 0 冒充数量**），零统一 `"0"`，其余 `format(normalize(),"f")` 去指数去尾零；REAL 列保留仅供展示的注释也符合现状。
- ❌ **P3-5** 三个文本列**只写不读**（grep 全仓无读取方），REAL 浮点列仍是唯一读取来源 → 该补丁目前**不产生**"精确勾稽"效果，属未完成的半成品。要么让 `protection_view_from_algo` 优先读 `*_text`，要么不加这三列。
- ⚠ `on conflict do update` 里三个新列已正确加入 `excluded` 分支（不会漏更新）；`_float()` 仍写 REAL 列（预期）。
- ⚠ `ensure_execution_schema(self.db.conn)` 在 `__init__` 重复调用（幂等，无害），但让镜像模块反向依赖执行模块；`protection_order_mirror` 不存在时静默跳过（符合"运行期表由各自迁移创建"）。

### 4) `tests/fixtures/execution/patches/run_offline_regression.register_u04.patch`
- ✅ 纯登记，正确。当前 `run_offline_regression` exit=1 是 I 的原子登记项，**不记为作者缺陷**（按预核验口径）。

### 5) 迁移编号 / schema_version（作者称"未动 schema_version，靠 `ensure_execution_schema` 幂等补齐"）
- ✅ **可接受，且理由成立**：`src/core/release_evidence.py:448-451` 的 GO 门是 `PASS if manifest_schema and schema_version == manifest_schema else FAIL`。只加向后兼容可空列却抬升迁移号，会让"数据库迁移版本与发布清单一致"这一项直接 FAIL。作者在模块 docstring（commit `8eb0480`）明确记录理由，测试也前后断言 `schema_migrations == 1..16` 不变（`test_execution_faults.py:723-739`）。我复核 `ensure_execution_schema` 真实生效且持久：`PRAGMA table_info(order_outbox)` 29 → 36 列，同连接/`fetch_all`/**关闭重开同一文件**均可见（probe3.txt）。
- ⚠ **P3（一致性）**：迁移 14 `order_outbox_reconciliation_metadata`（`src/audit/database.py:816-842`）做的**正是同一类**幂等 `ALTER TABLE ADD COLUMN`，但它**带版本号**。现在同一类变更存在两套机制。建议把这条策略写进 `docs/contracts/README.md` 的 §10.2 扩展登记处，避免后续审查者"顺手"补一个迁移 15 而把 GO 门打 FAIL。

---

## 我构造的反例与结论（A 角色核心：守恒 / 精度 / 不重发）

### 守恒（probe1，`probe1.txt`）— **未发现破坏守恒的路径**
测过的：并发式连续 fill、同一 `fill_id` 二次投递、超出 accepted、累计超量、`0`、负数、float、bool、`NaN`/`Infinity`、终态后再 fill、REJECTED 后 fill、FILLED 后 fill、1000 次 `0.001` 累加、大数+小数、`to_components()` 序列化往返。
摘要：
```
after cancel: filled=0 working=0 terminal=0.01 cons=True
late fill 0.0047: filled=0.0047 terminal=0.0053 status=CANCELED revision_only=True cons=True
  terminal exact==Decimal('0.0053') ? True      # 不是 0.0052999...
0.0047 后再 0.0006: filled=0.0053 terminal=0.0047 cons=True
over-fill 0.0101 -> QuantityError: fill exceeds accepted quantity
zero -> QuantityStateConflict；negative/NaN/Inf/float/bool -> QuantizationInputError
fill on REJECTED -> QuantityError；late fill on FILLED -> QuantityError
1000x0.001 filled=1.000 expected=1.000 equal=True cons=True
big+small (1.2345678901234567890e8 + 1e-20) cons=True；往返相等 True
dup fill_id（传 applied 集合）-> DuplicateFillError；不传集合 -> 二次入账（契约允许，见下）
```
**唯一需要提醒**：`apply_late_fill` 的幂等完全依赖调用方传入 `applied_fill_ids`；默认 `()` 会**静默二次计账**（probe1 §2 第 2 行）。K04 契约如此规定，但 `cancel_then_late_fill_probe` 自身也传 `()`（`459-462`），补丁的 `apply_fill_revision` 是唯一正确持久化该集合的地方（且它受 P1-P2 阻断）。

### 精度（probe1 §9）
所有入参入口（`parse_qty`/`apply_late_fill`/`read_exchange_order`/`real_position_from_snapshot`/`protection_view_from_algo`/`desired_sl|tp`）都经 Decimal，float 一律拒绝；`MoneyDecimal` 的 `_coerce_money` 也拒 float（`core/contracts/common.py:165-173`）。`0.01 → 0.0047` 得 `Decimal('0.0053')` / `Decimal('0.0006')` 精确，未见 float 泄漏（float 泄漏只在**补丁的** `str(float)` 落文本列与 `float(action.trigger_price)` 两处，见 P2-A8/P3 相关条目）。

### UNKNOWN 不重发 / 重启（probe2 §B–C, §F）— 靠**持久化**，不靠内存；但新模块未接线
- 真实 sqlite：`prepare` → `mark_unknown` → **关闭连接、重开同一文件、新建 `OrderOutbox`** → 再次 `prepare` 同 `clientOrderId` 抛 `DuplicateOrderSubmission`；新 `ENTRY` 在存在 UNKNOWN 时抛 `PendingOrderConflict`。二者都是 DB 查询驱动（`order_outbox.py:63-90`），故跨进程/重启成立 ✅（这正是"重启后同 ID 再提交被阻断"的**真实**依据，而不是新模块的 `repost_allowed` 字段）。
- `repost_allowed` / `new_client_id_allowed` 只是 dataclass 默认值，**无任何函数读取**；"不重发"的实际保证是 `execute_unknown_reconcile` 结构上**不存在提交路径**（只有 `query_*` 与 `acknowledge`），这一点我认可其可靠性。
- ❗ `plan_unknown_reconcile` / `execute_unknown_reconcile` **没有生产调用者**（全仓仅测试 import 本模块）；`execute_unknown_reconcile` 也**不回写** `query_attempt_count`/`next_retry_at`（`co_names` 中无该名字），预算纯靠外部写好的行状态 → 与既有的 `OrderOutbox.reconcile_unknown`（会 `+1` 并写退避）形成**两套并行实现**，存在漂移风险。R10 的"预算/时效耗尽转 MANUAL"目前只在库层面可用，未接入运行路径。

### 普通/Algo 分账（probe2 §A, §D）
端点→种类、显式种类↔端点矛盾、缺端点回推、历史未知端点 fail-closed 全部符合宣称；仅普通记录声明 Algo 覆盖被 `CrossKindCoverageClaim` 拒绝 ✅。扣分项是 P2-A5（`covered` 忽略 SUBMITTING）。

### 请求成功≠成交（probe1 §8）
```
http200 NEW 无成交        -> ACKNOWLEDGED, filled=0, FILL_UNCONFIRMED
FILLED 但 avgPrice=0      -> ACKNOWLEDGED, FILL_UNCONFIRMED          ✅
FILLED + 成交价           -> FILLED, confirmed=True                   ✅
空响应                    -> UNKNOWN, EMPTY_RESPONSE
回报自相矛盾(REJECTED带成交) -> UNKNOWN, RESPONSE_INCONSISTENT          ✅
```
反向漏点是 P2-A3（CANCELED/EXPIRED 已确认部分成交 → `TERMINAL_UNFILLED` 且无 reason）。

### 健康故障不触发全平（probe4 §6–§9）
已登记的健康类被拦 ✅；未登记的健康类落到 UNKNOWN 后**被放行全平** ❌（P1-A2）。"保护确证失败→P0 兜底平仓"仅存在于补丁，本模块 `triggers_flat_close` 恒 False（P2-A7）。

---

## 未覆盖 / 离线无法验证（不写"通过"）
1. `openAlgoOrders` / `positionAmt` / algo `quantity`·`triggerPrice` 的**真实报文类型**（字符串 vs JSON 数字）→ P2-A8 是否真实触发；需抓一份真实/回放报文。
2. **打过补丁的集成树**：我全程只读，未应用 4 个补丁，因此作者"打补丁的集成树已验证通过"这一说法我**无法复核**；补丁相关结论均来自 diff 阅读 + 对被替换代码的复现/受控复制。
3. `run_offline_regression.py` 全量（本 head 上 exit=1 属预期登记项）。
4. 交易所 tick/step-size 与 `quantities_equal` 相对容差的相互作用（离线无 symbol filters）。
5. 补丁的取消路径在真交易所上的行为（`DELETE /fapi/v1/algoOrder` 的错误码分支、`-2011` 兜底）未被覆盖。
6. `last_protection_sync_reason` 残余状态在真实 sync 循环中的触发频率（需时序/多标的运行），但缺陷本身由控制流证明成立。
7. 未读 `.env`/`data/`，未联网，未下单/撤单，未启停服务；所有产物仅落在本工作区 `_scratch_A/`。

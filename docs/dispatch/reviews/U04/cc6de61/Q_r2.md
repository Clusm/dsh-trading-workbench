# U04 修复轮 r2 独立复验报告 — Q 角色（独立会话）

- 复验对象：`cc6de619554f6256be952feaaedbf27974765f3e`（分支 `task/U04`，detached）
- 父链：`8eb0480`（A 判 CHANGES_REQUESTED）→ `083c16d`/`da3327f`/`cc6de61`
- 工作区（独立、detached、只读）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U04\R2\r1`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 上一轮报告（必读，已读）：`...\I\docs\dispatch\reviews\U04\8eb0480\A_r1.md`
- 本报告所有结论均来自**我自己写的探针**（`_scratch_Q/q*.py` + `q*_out.txt`），未采信作者的前后对照。
- 预核验（编排者已跑，我不重跑）：作者验收套件 exit 0；两个 smoke exit 0；`smoke_contracts_v3.py` PASS 173/FAIL 0；
  `run_offline_regression.py` 在本 head 仍 exit=1（`smoke_u04_*` 未登记，**I 的原子登记项，不记为作者缺陷**）。

**结论：CHANGES_REQUESTED**（唯一阻塞项为新发现 N1，详见 §4b 与文末）。

---

## 复现环境（我自己搭的两棵打过补丁的树）

作者的 4 个补丁在 head 上 `git apply --check` **全部 rc=0**。为了做真实集成路径的前后对照，
我在自己的 scratch 里复制了源码并分别应用补丁，构造两棵树，探针用 `sys.path` 指向对应树：

| 树 | 内容 |
|---|---|
| `_scratch_Q/tree_v1/` | `8eb0480` 版的 4 个补丁（V1） |
| `_scratch_Q/tree_r2/` | `cc6de61` 版的 4 个补丁（r2） |

命令（全部为 `git apply --directory=... <patch>`，只写我自己的 scratch）：

```
git apply --check tests/fixtures/execution/patches/binance_futures.u04_adaptation.patch   -> rc=0
git apply --check tests/fixtures/execution/patches/order_outbox.patch                     -> rc=0
git apply --check tests/fixtures/execution/patches/protection_mirror.decimal_text.patch   -> rc=0
git apply --check tests/fixtures/execution/patches/run_offline_regression.register_u04.patch -> rc=0
```

---

## 1. `can_cancel_protection`（A 的头号 P1 / MUST FIX 1）→ **CLOSED**

探针：`_scratch_Q/q1_cancel_protection.py`，原始输出 `q1_out.txt`。

重跑 A 的原始场景（`positionAmt='0.0'` / `'0.00000000'`，仅剩一道 SL，`flat_confirmed=False`）：

```
positionAmt='0.0', flat_confirmed=False, 1 SL        -> (False, 'FLAT_NOT_CONFIRMED')   is_flat=True
positionAmt='0.00000000', flat_confirmed=False, 1 SL -> (False, 'FLAT_NOT_CONFIRMED')   is_flat=True
positionAmt=2.0（有仓）                              -> (False, 'CANNOT_REMOVE_LAST_PROTECTION')
对照 V1：前两行是 (True, 'FLAT_CONFIRMED_CLEANUP')
```

**真翻转**，且反向未失效：`flat_confirmed=True` 三行全部 `(True, 'FLAT_CONFIRMED_CLEANUP')`。

绕过尝试（全部被挡）：

```
-0.0 / '0' / '+0' / '0.0E0' / '0E-8' / '00.000' / '  0.0  ' / Decimal(0) / Decimal('-0')
   -> 一律 (False, 'FLAT_NOT_CONFIRMED')
positionAmt 缺失 / None / ''（其他字段暗示归零） -> qty=0 但 (False, 'FLAT_NOT_CONFIRMED')   ✅ 不再靠快照数值
极小非零 '0.0000000001' / '1E-20' / '-1E-13'     -> (False, 'CANNOT_REMOVE_LAST_PROTECTION') ✅
```

**唯一残留（新发现，P3）**：`flat_confirmed` 未做严格布尔化，只做真值判断。
传非布尔真值（`'false'`/`'False'`/`[0]`）会被当成"已确认归零"：

```
flat_confirmed='false' -> (True, 'FLAT_CONFIRMED_CLEANUP')
flat_confirmed='False' -> (True, 'FLAT_CONFIRMED_CLEANUP')
flat_confirmed=[0]     -> (True, 'FLAT_CONFIRMED_CLEANUP')
```

签名是 `flat_confirmed: bool = False`，传入字符串是调用方违约；且上线唯一调用点（补丁的
`plan_protection_refresh(..., flat_confirmed=False)`）恒传常量 `False`，**不可达**。
判 P3，建议 `if flat_confirmed is True:`，但**不阻塞**。

---

## 2. 健康/未知故障不得放行全平（A 的 P1-A2 / MUST FIX 2）→ **CLOSED（白名单彻底）**

探针：`_scratch_Q/q2_fault_guard.py`，原始输出 `q2_out.txt`。
我**扩表枚举**了 35 个故障名（含 A 未列的、以及"看起来更像保护失败"的名字），
对每个 `FLAT_CLOSE_ACTIONS` 成员调用 `guard_fault_action`。

```
FLAT_CLOSE_ACTIONS    = ['CANCEL_ALL_PROTECTION', 'CLOSE_ALL', 'EMERGENCY_CLOSE', 'FLATTEN']
FLAT_CLOSE_AUTHORIZED = ['PROTECTION']
LEAKS (allowed flat-close but class != PROTECTION): []
```

- 4 个已登记健康类 → BLOCKED（`HEALTH_FAULT_MUST_NOT_TRIGGER_FLAT_CLOSE`）
- A 未登记的观测类：`PROTECTION_MIRROR_STALE` / `ALGO_ORDER_QUERY_FAILED` /
  `ProtectionOrderHealthCheck` / `HEALTH_PROTECTION_FAILED` → **全部 BLOCKED**
  （`FLAT_CLOSE_REQUIRES_CONFIRMED_PROTECTION_FAILURE`）
- 我新造的 15 个未登记名（`MIRROR_STALE` / `SNAPSHOT_STALE` / `POSITION_VIEW_LAG` /
  `API_LATENCY_HIGH` / `DNS_FAILURE` / `TLS_HANDSHAKE_FAILED` / `SOCKET_CLOSED` /
  `CLOCK_SKEW_DETECTED` / `DB_WRITE_SLOW` / `STREAM_GAP_DETECTED` …）→ **全部 BLOCKED**
- `SUBMIT_UNCERTAIN`（`TIMEOUT`/`RESPONSE_LOST`/`HTTP_503`/`HTTP_500`）→ **BLOCKED**（A 的关切已修）
- 词表外/空/异常类名（`SOMETHING_NEW` / `Q` / `''` / `Exception` / `RuntimeError`）→ **BLOCKED**
- 子串陷阱名（`ALGO_ORDER_HEALTHCHECK_TIMEOUT` → SUBMIT_UNCERTAIN、
  `PROTECTION_HEALTH_WS_DISCONNECT` → TRANSPORT）→ 均 BLOCKED
- 全部 4 个 flat-close 动作 × 5 类代表名：只有 `PROTECTION_FAILED` 放行

**反向未失效**：真正的保护确证失败仍能触发兜底平仓，且审计计划如实记录：

```
guard_fault_action('PROTECTION_FAILED','CLOSE_ALL') -> ALLOWED class=PROTECTION triggers_flat_close=True  ✅
guard_fault_action('PLACE_FAILED','CLOSE_ALL')      -> ALLOWED class=PROTECTION triggers_flat_close=True  ✅
（对照 V1：triggers_flat_close 恒 False —— A 的 P2-A7 也已修）
```

非全平提议不受影响：`HEALTH_DEGRADED + ALERT_ONLY -> ALERT_ONLY, triggers=False`。

**观察（不算缺陷）**：`defers_flat_close("TIMEOUT") == False`（`SUBMIT_UNCERTAIN` 不在
`_OBSERVATION_FAULTS`）→ 补偿平仓不被 TIMEOUT 抑制。方向是"宁可平仓不留裸仓"，
与 `guard_fault_action` 的"不得由观测故障驱动仓位动作"是两个不同判断点，作者 docstring
已写明取舍（"证据缺失变成保留裸仓"被明令禁止）。我判**设计选择、非缺陷**。

---

## 3. `ProtectionSyncReasons`（A 的 P1-P1）→ 作者抗辩**成立**，且修复有效 → **CLOSED**

探针：`_scratch_Q/q3_sync_reasons.py`（single / race / ast 三个模式）+ `_scratch_Q/q3_race2.py`。
两棵树分别加载（探针打印已加载的 `bf.__file__`，确认打的是哪棵树）。

### 3a. A 的 `N→N+1→N+2` 顺序场景：**我自己枚举返回路径，确认不可复现**

我用**自己的 AST 遍历**（不采信作者结论）列出 `_resync_protection_impl` 的每条 return，
并判断该 return 之前是否写过 sync-reason 字段：

```
tree_v1（4 条 return）:
  return False          wrote=True   via existing is None / intent,sl,tp 全 None   <- 查询失败分支
  return True           wrote=False  via not pos or |positionAmt| <= 0             <- ok=True
  return True           wrote=False  via stop_ok and take_ok                        <- ok=True
  return not failed     wrote=True   via entry（末尾统一写）
tree_r2（5 条 return）: 全部 wrote=True（入口 begin() + 各分支 mark()）
```

关键点：V1 里**唯一**的 `return False` 就是查询失败分支，而它**在返回前就写了** `QUERY_FAILED`；
两条"不写"的路径都返回 `True`，而 `_ensure_protection_from_order` 在 `ok=True` 时**直接 return**、
根本不会读该字段。因此"读到的值"永远等于"本次调用自己写的值"，不存在单线程可见的残余。

实测同一序列（`q3_sync_reasons.py <tree> single`）：

```
tree_v1:
  cycle N   : resync(A) query-fail       -> ok=False  reason='QUERY_FAILED'
  cycle N+1 : resync(A) match, early ret -> ok=True   reason='QUERY_FAILED'   <- 残余确实留下
  cycle N+2 : _ensure_protection_from_order(A) place-fail -> closes=['A']  reason_now='PROTECTION_FAILED'
  RESULT: CLOSED            (不是 A 说的裸仓)
tree_r2:
  cycle N+1 : reason=''（已复位）;  cycle N+2 -> closes=['A']  RESULT: CLOSED
```

即：**A 描述的"残余导致 N+2 放行不平仓"不成立**——N+2 的 resync 自己会在返回前把字段
改写成 `PROTECTION_FAILED`，读者看到的是新鲜值。A 说的"成功路径不复位导致残留"这半句
在 V1 里是**事实**（`reason` 在 N+1 后仍是 `QUERY_FAILED`），但它**不可达**到 A 声称的后果。

### 3b. 作者主张的"真正可达窗口"（同实例跨标的/跨线程）：**我独立复现了 V1 裸仓，r2 已修**

`q3_race2.py`：thread1 反复走 `_ensure_protection_from_order(B)`（其自身 resync 因 PLACE 失败
→ V1 写 `PROTECTION_FAILED` 这个**进程级标量**）；thread2 反复 `resync(A)` 且查询无法核验
→ 写 `QUERY_FAILED`。两者都走真实 `_resync_lock`；我只替换交易所 I/O 边界
（`_open_algos`/`_place_algo_protection`/`_close_or_reduce`），未注入该字段。
`sys.setswitchinterval(1e-6)` 放大小窗口，20000 轮：

```
tree_v1: closes=19984  bare(no close)=16  total=20000   <- 裸仓复现（PLACE 失败却不平仓）
tree_r2: closes=20000  bare(no close)=0   total=20000   <- 已修
```

### 裁定

**作者抗辩成立**：A 的 `N→N+1→N+2` 单线程因果链**不可复现**（我自己的 AST 枚举 + 实测双证），
A 把"字段有残留"（真）错推成"该残留会被读到"（假）。但**状态原语确实是真缺陷**——只是可达
路径是并发/跨标的窗口，作者已用 `ProtectionSyncReasons`（per-symbol + 每轮 `begin()` 复位）修掉，
我也独立复现了前后对照（16→0）。这一项按"修复已落地且我复现了收益"记为 **CLOSED**，
但保留一条 P3 提示：`ProtectionSyncReasons` 是**进程内**字典，多进程/多 worker 部署下
每个进程各持一份，跨进程不共享（当前部署是单进程 broker，暂不可达）。

---

## 4. `apply_fill_revision`（A 的 P1-P2 / MUST FIX 4）→ **第一笔成交：CLOSED；终态后再修订：NOT CLOSED（新发现 N1）**

探针：`q4_fill_revision.py`（`q4_out.txt`）、`q5_component_status.py`（`q5_out.txt`）、
`q7_terminal_regression.py`。全部跑在 `tree_r2`（真实打过补丁的集成树）上。

### 4a. "第一笔成交不再抛 STATUS_FILL_MISMATCH" → **成立**

对 5 个真实前置状态各建一行、各投一笔 `0.0047`：

```
seeded=SUBMITTING       -> OK status=PARTIALLY_FILLED filled=0.0047 working=0.0053
seeded=ACKNOWLEDGED     -> OK status=PARTIALLY_FILLED filled=0.0047 working=0.0053
seeded=UNKNOWN          -> OK status=PARTIALLY_FILLED filled=0.0047 working=0.0053
seeded=NEW              -> OK ...      seeded=PARTIALLY_FILLED -> OK ...
（对照 V1：这 5 行全部抛 STATUS_FILL_MISMATCH）
```

守恒、Decimal 精确、幂等、接线：

```
0.01 -> 0.0047 -> 0.0053：accepted==filled+working+terminal ? True；filled==Decimal('0.0053') ? True
重复 fill_id 'f1' -> DuplicateFillError: fill already applied: f1；filled 不变（无二次计账）
未知 client_order_id -> None（不炸）
reject() 后：accepted=filled=working=terminal=0（K04: REJECTED 必须全 0）OK；再 fill -> QuantityError（fail-closed）OK
FILLED 后再 fill -> QuantityError OK
apply_event 私有流（ORDER_TRADE_UPDATE, l=0.0047, t=12345）-> 落库 filled=0.0047, fills=["12345"] OK
  同一 trade_id 重放 -> 不二次计账 OK；关闭连接、重开同一文件、新 OrderOutbox 后重放同一 fill_id -> DuplicateFillError OK
结构接线：apply_fill_revision 出现在 apply_event 源码中 ? True；apply_exchange_quantities 出现在 reconcile_unknown 源码中 ? True
```

### 4b. **新发现 N1（P2，未登记）：终态行被 `_component_status` 去终态化 → 终态桶归零、违反 K04 不变量、返回值说谎**

`tests/fixtures/execution/patches/order_outbox.patch` 的 `_component_status`：

```python
stored = _STATUS_ALIASES.get(str(stored_status or "").upper(), "")   # 默认值是 ""，不是 x
if stored in _QUANTITY_TERMINAL:
    return stored
```

`_STATUS_ALIASES = {"EXPIRED_IN_MATCH": "EXPIRED"}`，`.get(k, "")` 意味着**除 `EXPIRED_IN_MATCH`
以外的任何状态都变成 `""`**，"终态保留原状态"这条分支对 `CANCELED/EXPIRED/REJECTED/FILLED`
**永不成立**（docstring 明写"终态保留原状态，迟到成交不复活订单"）。实测：

```
_component_status(accepted=0.01, filled=0.0047, stored_status=X)
  SUBMITTING/ACKNOWLEDGED/UNKNOWN/NEW -> PARTIALLY_FILLED    （推导，符合预期）
  CANCELED  -> PARTIALLY_FILLED   <<< 应当 CANCELED
  EXPIRED   -> PARTIALLY_FILLED   <<< 应当 EXPIRED
  REJECTED  -> PARTIALLY_FILLED   <<< 应当 REJECTED
  FILLED    -> PARTIALLY_FILLED   <<< 应当 FILLED
```

后果（走**生产私有流路径**，我只替换交易所 I/O 边界）：

```
私有流: PARTIALLY_FILLED(0.0047) -> CANCELED -> 迟到成交帧(l=0.001)
  行 after CANCELED : status=CANCELED accepted=0.02 filled=0.0047 working=0 terminal=0.0153  OK（作者测试[11]到此为止）
  行 after 迟到成交 : status=CANCELED accepted=0.02 filled=0.0057 working=0.0143 terminal=0   WRONG
     - K04 终态不变量 working==0 ? False   <<< VIOLATED
     - 终态余量应进 terminal(0.0143)，实际被记进 working，terminal 恒 0
     - 该行已无法再被契约自身接受：
       OrderQuantity.checked(..., status='CANCELED', working=0.0143) -> QuantityError: WORKING_NONZERO_WHILE_TERMINAL
     - 返回值同样说谎：status='PARTIALLY_FILLED'、revision_only=False（一个 CANCELED 单被报成"工作中"）
EXPIRED 行同理。行状态列之所以还停在 CANCELED，是因为 _update 的 _RANK 降级拦截挡住了
PARTIALLY_FILLED(30) < CANCELED(40)——即"状态"与"分量"此刻来自两套互相矛盾的来源。
```

**这是 r2 修复引入的"半修"**：V1 在同一场景下**抛** `WORKING_NONZERO_WHILE_TERMINAL`（成交根本
入不了账），r2 让成交能入账了，却把行写成非法态。V1 vs r2 同场景对照：

```
tree_v1: 迟到成交 RAISED QuantityError: WORKING_NONZERO_WHILE_TERMINAL（且 ack 不同步分量，working=0.02）
tree_r2: 迟到成交被接受，行 = status=CANCELED / working=0.0143 / terminal=0   -> WRONG
```

作者测试为何 133/0：`test_execution_faults.py` 的 `[11]` 在断言"CANCELED 三分量同步"之后
**正好停住**，从未在 CANCELED 之后再投一笔迟到成交——恰好避开这条路径。

**候选最小修复（我已实测）**：把默认值从 `""` 改为该状态本身

```python
stored = _STATUS_ALIASES.get(str(stored_status or "").upper(),
                             str(stored_status or "").upper())
```

在 `tree_r2fix` 上验证：

```
canceled_partial: reported status=CANCELED rev_only=True filled=0.0057 terminal=0.0143
  ROW: status=CANCELED accepted=0.02 filled=0.0057 working=0 terminal=0.0143 -> OK
expired_partial : ROW: status=EXPIRED ... terminal=0.0143 -> OK
canceled_zero_fill: ROW: status=CANCELED ... terminal=0.019 -> OK
回归：第一笔成交 5/5 前置状态仍然 OK；作者验收套件在修复树上仍 133 通过 / 0 失败
```

即修复是一行、无副作用、且不改任何现有断言。

**定级理由（P2）**：不动用资金、总量仍守恒、当前无读取方会据此崩溃（该行今天只被
`apply_fill_revision` 自己读，且它再次按分量推导故不会炸）；但 (a) 账本留下违反 K04 终态
不变量的行，(b) `terminal_unfilled` 桶被静默清零（对账/报告口径），(c) 返回值把一个终态单
报成 `PARTIALLY_FILLED` 且 `revision_only=False`（消费者可能据此把已撤单当活跃单）。
**未登记** ⇒ 按标尺判 CHANGES_REQUESTED。

---

## 5. 补丁：`git apply --check` + 打补丁树上的验收套件 → **CLOSED**

```
git apply --check tests/fixtures/execution/patches/binance_futures.u04_adaptation.patch     rc=0
git apply --check tests/fixtures/execution/patches/order_outbox.patch                       rc=0
git apply --check tests/fixtures/execution/patches/protection_mirror.decimal_text.patch     rc=0
git apply --check tests/fixtures/execution/patches/run_offline_regression.register_u04.patch rc=0
```

4 个补丁**实打应用**到我的临时树后跑作者验收套件（原始输出 `author_suite_patched_r2.txt`）：

```
PATCHED_TREE_EXIT=0      结果: 133 通过 / 0 失败      U04 EXECUTION FAULTS OK
HEAD_TREE_EXIT=0         结果: 122 通过 / 0 失败      （[11] 打印 PENDING，符合预期）
```

MUST FIX 3/4 的严格断言在**真实集成路径**上成立（我另行用 `q3_race2.py` / `q4_fill_revision.py`
独立复现，结论一致）。

**P3（测试强度，新发现 N2）**：MUST FIX 3 的"回归护栏"是**空转的**。
`test_execution_faults.py` 的 `[10]`：跨标的与"成功即复位"两项断言里，两次
`make_broker()` 各自 `__new__` 出一个新实例，`ProtectionSyncReasons` 是**每实例惰性新建**的，
所以状态根本没有跨调用共享——按标的隔离在"每实例新鲜"下自动成立。
我把 r2 的测试套件原样跑到 **V1 树**（`_scratch_Q/r2suite_on_v1tree.txt`）验证：

```
[10] 全部 PASS，包括
  PASS 成功的一轮同步不留下残余失败类别（成功即复位）
  PASS 另一标的的查询失败不抑制本标的的兜底平仓（按标的隔离）
  （section [11] 的 4 条 FAIL 是 V1 order_outbox 的真实缺陷，属预期）
```

即这两条断言**无法区分 V1 与 r2**（V1 也全过）。修复本身我已独立证实有效（3b 的 16→0），
所以这是**护栏无效**而非修复无效。建议：把跨标的回归改成**同一个 broker 实例**上
先 `resync(A)` 失败、再 `_ensure_protection_from_order(B)` 保护挂单失败。

---

## 6. 回归抽样（6 项，全部 PASS）

探针 `q6_regression.py`（`q6_out.txt`，`checks failed: none`）：

| # | 项 | 结果 |
|---|---|---|
| R1 | 三分量守恒 `accepted == filled + working + terminal`（NEW / PARTIAL / CANCELED+部分 / FILLED） | PASS x4，`violations=()` |
| R1b | 非法态确实被拒（REJECTED 带 working、和不等、REJECTED 且 accepted>0） | PASS x3（均 `QuantityError`） |
| R2 | Decimal 精确 `0.01->0.0047->0.0053`，无 float 尾；float 入参被拒 | PASS x3 |
| R3 | UNKNOWN 不重发：关闭重开同一 DB 文件后同 `clientOrderId` -> `DuplicateOrderSubmission`；新 ENTRY -> `PendingOrderConflict`；`execute_unknown_reconcile`/`OrderOutbox.reconcile_unknown` 源码无提交调用；脏计数 `'abc'` -> `MANUAL_RECOVERY` 不抛 | PASS x5 |
| R4 | PLACE 严格先于 CANCEL（`ordered()` = PLACE,PLACE,CANCEL；PLACE 失败 -> 只发生 1 次 place、`canceled=()`、`cancel_skipped=True`）；撤单回调返回假值 -> `cancel_failed=('sl-old',)`、`canceled=()`、reason=`CANCEL_CALLBACK_NOT_EXECUTED` | PASS x3 |
| R5 | 端点->种类（`/fapi/v1/order`->STANDARD、`/algoOrder`->ALGO）；分桶不合并；`SUBMITTING` 不算已覆盖；词表外状态按未决（fail-closed） | PASS x5 |
| R6 | 请求成功≠成交：200 NEW 无成交 -> `ACKNOWLEDGED`/`FILL_UNCONFIRMED`；FILLED 但 avgPrice=0 -> `ACKNOWLEDGED`；空响应 -> `UNKNOWN`；回报自相矛盾 -> `UNKNOWN`；CANCELED 带成交 -> `TERMINAL_PARTIAL`（A 的 P2-A3 已修）；CANCELED 无成交 -> `TERMINAL_UNFILLED` | PASS x3 |

---

## 新发现汇总

| ID | 级别 | 内容 | 是否已登记 |
|---|---|---|---|
| **N1** | **P2** | `order_outbox` 补丁 `_component_status` 的 `_STATUS_ALIASES.get(x, "")` 令"终态保留原状态"分支永久失效；CANCELED/EXPIRED 行收到迟到成交后 `working != 0`、`terminal` 归零、返回值报 `PARTIALLY_FILLED`/`revision_only=False`，该行被 `OrderQuantity.checked` 判 `WORKING_NONZERO_WHILE_TERMINAL`。一行可修（4b 已验证）。 | **否** |
| N2 | P3 | MUST FIX 3 的跨标的/复位回归断言空转（每次 `make_broker()` 都是新实例，V1 树也全过） | 否（测试强度问题） |
| N3 | P3 | `can_cancel_protection(flat_confirmed=<非布尔真值>)` 仍按真值放行（`'false'`/`[0]`）；签名是 `bool`，上线唯一调用点恒传常量 `False`，不可达 | 否（建议 `is True`） |
| N4 | P3 | `mirrored_protection_views()`（P3-5 新增的文本列读取方）在补丁树里**仍无生产调用者**（全仓仅注释提及）；作者已自述"两个无消费者方法"，方向保守 | 作者自述 known_limit |
| N5 | 观察 | `defers_flat_close('TIMEOUT') == False`（`SUBMIT_UNCERTAIN` 不在 `_OBSERVATION_FAULTS`）-> 不推迟兜底平仓；方向是"宁可平仓不留裸仓"，docstring 已写明取舍 | 设计选择，非缺陷 |

## 命令清单

```
# 补丁可应用性
git apply --check tests/fixtures/execution/patches/{binance_futures.u04_adaptation,order_outbox,protection_mirror.decimal_text,run_offline_regression.register_u04}.patch
# 造两棵打过补丁的树（只写我自己的 scratch）
git apply --directory=_scratch_Q/tree_v1 _scratch_Q/v1_bf.patch          # 8eb0480 版
git apply --directory=_scratch_Q/tree_r2 tests/fixtures/execution/patches/*.patch
# 探针（输出全部重定向到同目录 txt）
python _scratch_Q/q1_cancel_protection.py                     > q1_out.txt
python _scratch_Q/q2_fault_guard.py                           > q2_out.txt
python _scratch_Q/q3_sync_reasons.py {tree_v1|tree_r2} {single|race|ast}
python _scratch_Q/q3_race2.py {tree_v1|tree_r2} 20000 4
python _scratch_Q/q4_fill_revision.py tree_r2                 > q4_out.txt
python _scratch_Q/q5_component_status.py                      > q5_out.txt
python _scratch_Q/q6_regression.py                            > q6_out.txt
python _scratch_Q/q7_terminal_regression.py {tree_v1|tree_r2|tree_r2fix}
# 验收套件（三棵树）
python tests/acceptance/test_execution_faults.py                       # head  122/0
python _scratch_Q/tree_r2/tests/acceptance/test_execution_faults.py    # 补丁  133/0
python _scratch_Q/tree_v1/tests/acceptance/test_execution_faults.py    # V1 树（护栏检验）
```

## 未覆盖 / 离线无法验证（不写"通过"）

1. 作者自述 4 条 known_limits：step-size 容差（`quantities_equal` 1e-6 相对，离线无 symbol filters）、
   两个无消费者方法（其中 `mirrored_protection_views` 我已复核**确实无调用者**）、结构性护栏
   （`CANNOT_REMOVE_LAST_PROTECTION` / `_assert_no_cancel_before_place` 不可达）、真实报文类型无回放数据。
2. `openAlgoOrders` / `positionAmt` / algo `quantity`/`triggerPrice` 的真实报文类型（字符串 vs JSON 数字）——
   r2 已加 `_exchange_decimal` + 逐行容错，但我**无真实报文可验**。
3. 真交易所上的取消路径（`DELETE /fapi/v1/algoOrder` 错误码分支、`-2011` 兜底）未覆盖。
4. `run_offline_regression.py` 全量（本 head exit=1，属 I 的原子登记项，按口径不记为作者缺陷）。
5. 我未读过 `.env`/`data/`/密钥；未联网；未下单/撤单；未启停服务；未触碰 `mat-wt/I`、`mat-wt/B`；
   所有产物仅落在 `reviews/U04/R2/r1/_scratch_Q/`。

## 结论：**CHANGES_REQUESTED**

- 上一轮 5 个 P1 里，**4 个已 CLOSED 且我独立复现了收益**（P1-A1 真翻转、P1-A2 白名单零泄漏、
  P1-P1 抗辩成立且跨标的/跨线程窗口 16->0、P1-P2 第一笔成交 5/5 前置状态不再抛错），
  P1-P2 的**终态后再修订**子路径**未闭合**并引入新缺陷 N1。
- 上一轮 P2/P3 的修复我抽样复核：P2-A3（TERMINAL_PARTIAL）、P2-A5（SUBMITTING 不算覆盖）、
  P2-A6（未执行撤单进 `cancel_failed`）、P2-A7（`triggers_flat_close=True`）、P3-1（脏计数
  fail-closed）、P3-2（并发补列不计入返回值/不替调用方 commit）均已落地；P2-A4/P3-3/P3-4/P3-8
  已**如实登记**在模块 docstring/注释里。
- 唯一阻塞项是 **N1（未登记的 P2）**：一个一行可修、且直接命中 DC04 "撤单后迟到成交" 主场景的
  账本一致性缺陷，会让终态行违反 K04 不变量并把终态单报成工作中。
- 另附 4 条 P3/观察（N2 护栏空转、N3 非布尔 `flat_confirmed`、N4 无消费者方法、N5 设计取舍）。

**Go/No-Go**：修掉 N1（4b 的一行）+ 建议补 N2 护栏后可转 GO；其余均为 P3 及以下且已如实登记，
不阻塞。

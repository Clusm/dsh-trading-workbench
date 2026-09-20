# U04 修复轮 r3 独立复验报告 — Q 角色（独立会话）

- 复验对象：`f4e466328fcd87b162fbfd89799aad485c7f9b15`（分支 `task/U04`，detached）
- 父链：`cc6de61`（Q_r2 判 CHANGES_REQUESTED，唯一阻断项 N1）→ `531d2c8` / `a602211` / `f4e4663`
- 工作区（独立、detached、只读）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U04\R3\r1`
- 上一轮报告（**已读**）：`...\I\docs\dispatch\reviews\U04\cc6de61\Q_r2.md`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 所有结论来自**我自己写的探针**（`_scratch_Q/q*.py` + `q*_out.txt`），未采信作者的 `_scratch_B/*` 对照。
- 预核验（编排者已跑，我不重跑）：`run_offline_regression.py` 在本 head 因 `smoke_u04_*` 未登记 exit=1 —— **I 的原子登记项**。

**结论：CHANGES_REQUESTED**

- N1 修复**语义正确**（我独立复现：`working=0.0143/terminal=0/WORKING_NONZERO_WHILE_TERMINAL` → `working=0/terminal=0.0143/守恒/CORRECT_TERMINAL`），**但引入了同等严重的 P1 回归**：`_component_status` 现在把"行状态已 FILLED、成交量尚未入账"的中间态认成 FILLED，导致 **`FILLED` 事件帧的成交被静默丢弃、行落成非法态**。这是订单生命周期里**最常走**的一条路（见 §1b/§9-N6）。
- 另有 **N7（P2，未登记）**：`EXPIRED_IN_MATCH` 经生产私有流落库后 `status` 列保留交易所别名，`OrderQuantity` 词表不接受，且**三分量不同步**（终态行 `working != 0`）。

---

## 0. 复现环境（我自己搭的 6 棵打过补丁的树）

补丁在 head 上 `git apply --check` 全部 rc=0（**在 repo 根**执行，非子目录）：

```
binance_futures.u04_adaptation.patch          rc=0
order_outbox.patch                            rc=0
protection_mirror.decimal_text.patch          rc=0
run_offline_regression.register_u04.patch     rc=0
```

（原始输出：`_scratch_Q/apply_check_r3.txt`）

| 树 | 内容 | 用途 |
|---|---|---|
| `_scratch_Q/tree_pristine/` | `f4e4663` 源，补丁**未**应用 | 全量 delta 基线 |
| `_scratch_Q/tree_r3/` | `f4e4663` 源 + 当前 4 补丁（**r3 集成态**） | 主复验对象 |
| `_scratch_Q/tree_r2code/` | `cc6de61` 源 + `cc6de61` 版 4 补丁（**未修树**） | N1 前后对照 / 断言非空转 |
| `_scratch_Q/tree_v1code/` | `8eb0480` 源 + `8eb0480` 版 4 补丁 | N2 的 V1 对照 |
| `_scratch_Q/tree_r3_fix2/` | r3 + 我实测的候选最小修复 | 给出可落地修法 |
| `_scratch_Q/tree_r3_mut/` | r3 + **受控回归注入**（读侧退回进程级标量） | N2 护栏能否被绕过 |

树构造命令（只写我自己的 scratch）：

```
git archive <commit> | tar -x -C _scratch_Q/tree_<x>
git apply --directory=_scratch_Q/tree_<x> <该 commit 版的 patch>
```

集成态验收（我自己跑，`_scratch_Q/acc_tree_r3.txt`）：

```
PATCHED (tree_r3) EXIT=0   结果: 154 通过 / 0 失败   U04 EXECUTION FAULTS OK
HEAD    (未打补丁) EXIT=0   结果: 130 通过 / 0 失败
```

与作者自述一致。

---

## 1. N1（唯一阻断项）—— **修复本身成立；但把另一条主路径打坏了**

### 1a. 原 N1 场景：**真闭合**（探针 `q1_n1_terminal.py`，输出 `q1_out.txt`）

`_component_status` 直调矩阵（`accepted=0.01, filled=0.0047`）：

```
tree_r3（修后）:
  CANCELED -> 'CANCELED'    EXPIRED -> 'EXPIRED'    REJECTED -> 'REJECTED'
  FILLED   -> 'FILLED'      EXPIRED_IN_MATCH -> 'EXPIRED'
  canceled/Canceled -> 'CANCELED'（大小写归一）
  未映射 SOMETHING_NEW/''/None/123/'CANCELLED' -> 'PARTIALLY_FILLED'（落到分量推导，不外泄）
  [edge] CANCELED filled=0 -> 'CANCELED'；CANCELED filled=accepted -> 'CANCELED'
tree_r2code（未修）:
  CANCELED/EXPIRED/REJECTED/FILLED -> 'PARTIALLY_FILLED'（终态分支永久失效，与 Q_r2 一致）
  [edge] CANCELED filled=0 -> 'NEW'；CANCELED filled=accepted -> 'FILLED'
```

生产私有流路径（`PARTIALLY_FILLED(0.0047)` → 终态帧 → 迟到成交帧 `l=0.001`）：

```
tree_r2code:  CANCELED  after-late: filled=0.0057 working=0.0143 terminal=0
              row_violations = QuantityError: WORKING_NONZERO_WHILE_TERMINAL
              EXPIRED   同上
tree_r3:      CANCELED  after-late: status=CANCELED filled=0.0057 working=0 terminal=0.0143
              row_violations = ()   守恒 True
              EXPIRED   同上
```

作者矩阵（`CANCELED`/`EXPIRED`/`REJECTED`/`FILLED` 各自 + 迟到成交）**我自己跑了**：
`CANCELED`/`EXPIRED` 在 r3 上保留原终态且守恒；`REJECTED`/`FILLED` 两行的**迟到成交前**就已经非法
（见 1b：`_update` 在 `REJECTED`/`FILLED` 上把余量写进 terminal，而 REJECTED 要求 accepted==0、
FILLED 要求 terminal==0）——**该非法态在 r2code 上完全相同，不是本轮引入**，且这两个前置序列
（已部分成交再被 REJECTED）物理上不可能，故不计入 N1。

**绕过尝试（r3，全部被挡）**：

```
C1 终态 + 超额成交（经 apply_event）：行不变、legal；真正超额由 apply_fill 的
   "fill exceeds accepted quantity" 拒绝（详 §7-R7）
C2 终态 + 负数量：行不变、legal（QuantityStateConflict 被 apply_event 吞掉，fail-closed）
C3 行 status 被改成 'SOMETHING_NEW'/''/'   '/123 后投成交：返回值恒为词表内状态
   （PARTIALLY_FILLED），**不外泄**；行 status 列仍为脏值——但该脏值在生产上不可达：
   apply_event 对 `X not in _ACTIVE|_TERMINAL` 一律回落到行内既有状态，从不写未知串
C4 model_copy 篡改：violations() 立刻报 WORKING_NONZERO_WHILE_TERMINAL，重水化抛 QuantityError
```

→ **N1 语义闭合，绕过尝试无一成功。**

### 1b. **新阻断项 N6（P1，未登记）：`FILLED` 私有流事件的成交被静默丢弃、行落成非法态**

探针 `q2_fill_sequence.py`（输出 `q2_r3.txt` / `q2_r2_v1.txt`）。

`apply_event` 的顺序是 **先 `_update(status=...)` 再 `apply_fill_revision(...)`**（`order_outbox.py`
L375-410）。当帧是 `X="FILLED"` 时：

1. `_update(status='FILLED')` 先把行改成 `status=FILLED`，并按终态口径写 `working=0`、
   `terminal=accepted-filled_old`；
2. 随后 `apply_fill_revision` 读到的 `stored_status` 已经是 `"FILLED"` → 修后的
   `_component_status` 返回 `"FILLED"` → 走 `elif status == "FILLED": working, terminal = 0, 0`；
3. `OrderQuantity.checked(accepted=0.02, filled=0.0105, working=0, terminal=0)` →
   **THREE_COMPONENT_CONSERVATION_VIOLATION** → `QuantityError`；
4. `apply_event` 用 `except Exception` 吞掉（只写一条 `warn` 风险事件），**本帧成交永远不入账**。

实测（生产私有流路径，正常生命周期）：

```
S1 部分成交 -> 完全成交（真实 USER_STREAM 形状：z 推进到 q，l>0）
  tree_r2code: after FILLED  status=FILLED  accepted=0.02 filled=0.02   working=0 terminal=0.00 viol=()
  tree_r3    : after FILLED  status=FILLED  accepted=0.02 filled=0.0105 working=0 terminal=0.0095
               viol='QuantityError: FILLED_STATUS_WITH_RESIDUAL; STATUS_FILL_MISMATCH'
               risk_events: "s1: 私有流成交未能落库 QuantityError
                             THREE_COMPONENT_CONSERVATION_VIOLATION; STATUS_FILL_MISMATCH"
                             action_taken='fill revision skipped'   severity='warn'
               last_error=None     <<< 末尾被 apply_event 复位，健康监控看不到

S3 从零一笔全成（X=FILLED, z==q, l==q）
  tree_r2code: filled=0.02  working=0 terminal=0.00 viol=()
  tree_r3    : filled=0     working=0 terminal=0.02 viol='FILLED_STATUS_WITH_RESIDUAL; STATUS_FILL_MISMATCH'
               <<< 成交事实**整笔丢失**

S4 部分成交 -> FILLED 帧不带 l（物理上不可能）
  r2code 与 r3 **同样**非法 -> 既有缺陷，非本轮引入
```

**为什么严重**：

- **可达性最高**：任何"部分成交后最终全成"的限价单都会走这条帧序；这不是边界，是主路径。
- **后果**：`cumulative_filled_qty_text` 少记最后一档成交（S3 是**整笔**丢失）。DC04 要求重启后按
  `fill_id` 重放/续算，落库的累计成交是唯一事实来源 → 重启对账必然对不上。
- 行**永久性违反 K04**：`status=FILLED` 且 `terminal != 0` / `filled != accepted`；此后对同一行的
  任何 `apply_fill_revision` 都会再次命中同一分支抛错，**再也修不回来**。
- **静默**：只落一条 `warn` 风险事件；`apply_event` 末尾 `self.last_error = None` 无条件复位，
  以 `last_error` 为健康信号的监控看不到。
- 作者验收 154/0 为何没抓到：`section_fill_revision_wiring`（[11]）**从未发送 `X="FILLED"` 的
  ORDER_TRADE_UPDATE 帧**（`grep '"X": "FILLED"'` 无命中），夹具里只有 `PARTIALLY_FILLED` 与
  `CANCELED`/`EXPIRED`。

**候选最小修复（我已实测，`tree_r3_fix2`）**：终态分支不能对 `filled < accepted` 的 `FILLED` 生效。

```python
    if stored in _QUANTITY_TERMINAL and not (stored == "FILLED" and filled < accepted):
        return stored
```

在 `tree_r3_fix2` 上：

```
S1 -> filled=0.02 working=0 terminal=0.00 viol=()        （恢复）
S3 -> filled=0.02 working=0 terminal=0.00 viol=()        （恢复）
S5 -> 第一笔成交路径仍 OK
N1 场景（CANCELED/EXPIRED + 迟到成交）-> 仍 working=0 terminal=0.0143 viol=()   （不回退）
作者验收套件 -> 154 通过 / 0 失败，exit 0
```

即一行、无副作用、且不改任何现有断言。（**注意**：这只是候选，需作者自测后再验。）

---

## 2. 回归断言是否非空转 —— **非空转（4/4 真 FAIL）**

我自建未修树 `tree_r2code`（`cc6de61` 源 + `cc6de61` 版补丁），把 r3 的测试文件原样覆盖进去后跑
（`_scratch_Q/acc_r3tests_on_r2code.txt`，EXIT=1）：

```
FAIL  非布尔 flat_confirmed='false'/'true'/'0'/1/1.0/[0]/(0,)/{'a':0} 不得授权清理   x8
      （allowed=True reason=FLAT_CONFIRMED_CLEANUP —— 正是 N3 要挡的）
FAIL  CANCELED 后的迟到成交：working 归零、余量进 terminal
      actual working='0.0092' terminal='0'
FAIL  CANCELED 后的迟到成交：落库行仍被 OrderQuantity 契约接受
      violations=QuantityError: WORKING_NONZERO_WHILE_TERMINAL
FAIL  CANCELED 后第二笔迟到成交继续推进且不破坏终态
FAIL  EXPIRED 后的迟到成交保留终态且行仍被契约接受
```

→ 作者称"第 [11] 节断言在未修的 r2 树上 4/4 FAIL"**成立**，且失败原因与我 Q_r2 判定的
N1 完全一致（`WORKING_NONZERO_WHILE_TERMINAL`）。断言**不是恒真**。

**但**：新增断言仍然没有覆盖 `X="FILLED"` 帧（见 1b），因此**未覆盖本轮新引入的 N6**。

---

## 3. N3（`flat_confirmed` 严格布尔）—— **CLOSED（附 1 条 P3 观察）**

探针 `q6_n3_bool.py`（输出 `q6_out.txt`），`position.is_flat=True`、仅剩一道 SL：

| `flat_confirmed` | r2code | r3 |
|---|---|---|
| `True` | allowed=True / FLAT_CONFIRMED_CLEANUP | **allowed=True / FLAT_CONFIRMED_CLEANUP**（无假阳性 ✅） |
| `False` | allowed=False / FLAT_NOT_CONFIRMED | allowed=False / FLAT_NOT_CONFIRMED |
| `1` / `1.0` / `'true'` / `'false'` / `[0]` / `{'a':0}` | allowed=True / FLAT_CONFIRMED_CLEANUP | **allowed=False / FLAT_NOT_CONFIRMED** ✅ |
| `np.True_` / `np.False_` | — | allowed=False / FLAT_NOT_CONFIRMED |

`plan_protection_refresh` 同口径：`flat_confirmed=True` → `blocked='', cancels=1`；
其余全部 `blocked='FLAT_NOT_CONFIRMED', cancels=0`。
**任何入参都不抛异常**，返回契约 `(bool, reason)` 保持。

- (a) ✅ 非布尔真值不再被当成 True（r2 上 `'false'`/`1`/`1.0` 都会放行清理最后一道保护，r3 全部挡住）。
- (b) ✅ 合法布尔 `True` 仍被接受，无假阳性。
- (c) **P3 观察（不阻塞）**：降级是**静默**的——`flat_confirmed=1` 与 `flat_confirmed=False`
  返回完全相同的 `(False, 'FLAT_NOT_CONFIRMED')`，调用方无法区分"类型违约"与"确实未确认"。
  方向是 fail-closed（不清最后一道保护 = 不放行危险动作），且全仓**无任何生产调用点**传非布尔
  （`grep flat_confirmed` 只有声明、测试与我）；但 `np.True_` 这类"看起来是 bool"的值也会被拒，
  若将来有调用方用 numpy/pandas 布尔会**静默跳过清理**（留下多余保护单，不产生裸仓）。
  建议：非布尔入参至少 `log_risk_event` 或加一个 `FLAT_CONFIRMED_TYPE_INVALID` reason。

---

## 4. N2（顺序断言无法区分 V1/r2）—— **作者补的结构断言有效但可绕过（反例已实测）**

### 4a. 结构断言对 V1 **非空转** ✅

```python
production_reasons = getattr(shared, "protection_sync", None)
check(..., production_reasons is not None and production_reasons.get(symbol) == "QUERY_FAILED")
```

我逐树探针（`q4_out.txt`）：

```
tree_v1code  getattr(broker,'protection_sync',None) = None   hasattr(type,..)=False  -> 断言 FAIL
tree_r2code  = ProtectionSyncReasons object                  -> 断言可过
tree_r3      = ProtectionSyncReasons object                  -> 断言可过
```

V1 只有进程级标量 `last_protection_sync_reason`（`grep` 4 处），无该对象 ⇒ 断言在 V1 上**必 FAIL**。
（注：完整的 r3 测试文件无法在 V1 树上跑到底——V1 的 `protection_commands` 缺
`REASON_CANCEL_NOT_EXECUTED`，`ImportError`；故我用单点探针验证该断言的核心。）

### 4b. 三条行为断言改单实例后确实不再空转

`q4_out.txt` 基线（正确 r3 代码，7/7 PASS）。我另注入"读侧退回进程级标量"的受控回归（对象/写侧
保持 r3 正确），**7 条里 2 条抓住**：

```
FAIL (S-b) A 标的失败在 B 标的键下不可见   get(B)='QUERY_FAILED'
FAIL (2b)  轮次入口复位失败类别            get(sym)='QUERY_FAILED'
PASS (3)   跨标的隔离：B 的失败不被 A 抑制   <<< 行为断言反而没抓住
```

值得注意：**行为断言 (3) 抓不住这类泄漏**（B 自己那一轮会在读之前把自己的失败类别写进去，
读到的永远是新鲜值——与 V1 当初骗过 A/P1-P1 的原因相同）。所以结构断言**确实带来了行为断言
没有的区分力**，方向是对的。

### 4c. **但它仍可绕过 —— 反例（我实测）**

反例构造：保留 `protection_sync`（按标的、每轮 `begin()` 复位）**完全正确**，只把
`_ensure_protection_from_order` 的**读**改用进程级标量 `_MUT_GLOBAL_LAST_FAILURE`（树
`tree_r3_mut`，22 行改动）：

```
q4_n2_guard.py tree_r3_mut -> [baseline-r3] 7/7 PASS   <<< [10] 全部断言（含结构断言）都过
q5_race_mut.py（双线程，sys.setswitchinterval(1e-6)，仅替换交易所 I/O）:
  tree_r3       20000 轮 bare_no_flat_close=0                    （正确实现）
  tree_r3_mut   20000 轮 bare_no_flat_close=6 / 16 / 20 / 5 / 17 （5 次运行；同一竞态）
  tree_v1code   20000 轮 bare_no_flat_close=26 / 33             （V1 基准）
```

即：**同一个能产生 16/20000 裸仓的缺陷实现，能通过 [10] 的全部断言（含新增结构断言）**。
原因是结构断言只检查"对象里存了什么"，不检查"读路径是否从该对象按标的取"。

**裁定**：N2 的处理方向正确、比 r2 强（我能证伪 V1、能抓到"对象也坏了"的回归），但**不构成对
跨标的窗口缺陷的完整护栏**。属 **P3 测试强度残余**，建议（非阻塞）：把回归改成对**读路径**的可观
测行为断言 —— 例如在同一个 broker 实例上让 B 的一轮"保护失败"与 A 的一轮"查询失败"真正并发
（`setswitchinterval` + 线程），或以"决策依据"为断言对象（断言 `_ensure_protection_from_order`
在 A 的 QUERY_FAILED 存活期间对 B 仍会调用 `_close_or_reduce`）。

---

## 5. 补丁可应用性 + 集成态验收 —— **CLOSED**

见 §0：4 个补丁在 repo 根 `git apply --check` 全部 rc=0（`apply_check_r3.txt`）；
`tree_r3` 上实打应用后集成态验收 **154 通过 / 0 失败，exit 0**。

---

## 6. 全量回归 delta（强制）—— **无新增失败**

作者自述 `run_offline_regression.py` 用 `check=True` **在第一个失败处中止**，只能给出"有/无失败"，
给不出失败集合。我因此写了 `q7_delta.py`：读出**同一份清单**（`tree_r3/scripts/run_offline_regression.py`
的 `SCRIPTS`），用**同一 env**（`EXECUTION_MODE=paper`、删 `MTA_PROFILE`、`MTA_NO_PROXY=1`、
`PYTHONUTF8=1`、剥离 `*BINANCE*/*SENSENOVA*` 的 KEY/SECRET/TOKEN），在**临时沙箱树**里逐脚本
跑并记录 rc。

| 树 | 清单条数 | 失败集合 |
|---|---|---|
| `tree_pristine`（补丁未应用） | 70（与 r3 同一份清单） | `{scripts/smoke_windows_service_plan.py}` |
| `tree_r3`（补丁已应用） | 70 | `{scripts/smoke_windows_service_plan.py}` |

**失败集合差集 = ∅**（两侧完全一致）。唯一的失败是既有 KI02
（`smoke_windows_service_plan.py`，`ValueError: ... 不存在: ...`——本机缺 Windows 服务配置文件，
两侧报错文本逐字相同），与 U04 无关。

补充：pristine 用**它自己的**（未打补丁的）清单是 69 条（少 `smoke_u04_execution_faults.py`，
即 `run_offline_regression.register_u04.patch` 新登记的那条），r3 是 70 条；我按标尺改用
**同一份 70 条清单** 跑 pristine，以排除"清单不同"造成的假 delta。

→ **打补丁树未新增任何失败。**（原始输出：`delta_r3.json` / `delta_pristine_full.json` +
`delta_r3.log` / `delta_pristine_full.log`）

---

## 7. 回归抽样（6 类 15 项，全部 PASS）

探针 `q9_regression.py`（输出 `q9_out.txt`，跑在 `tree_r3`，`failures=[]`）：

| # | 项 | 结果 |
|---|---|---|
| R1 | 三分量守恒：NEW / PARTIALLY_FILLED / CANCELED+部分 / FILLED 四态 `violations()` 均为空 | PASS |
| R1b | 非法态确实被拒：REJECTED 带量、和不等、REJECTED 且 accepted>0 三项均抛 `QuantityError` | PASS x3 |
| R2 | Decimal 精确 `0.01 -> 0.0047 -> 0.0053`，无 float 尾差；float 入参被拒 | PASS x2 |
| R3 | UNKNOWN 不重发：`repost_allowed`/`new_client_id_allowed` 恒 False；同 `clientOrderId` 抛 `DuplicateOrderSubmission`；未决 ENTRY 抛 `PendingOrderConflict`；`reconcile_unknown` 源码无提交调用；脏计数 `'abc'` fail-closed 不抛 | PASS x5 |
| R4 | PLACE 先于 CANCEL：`ordered()` 实测 `[PLACE, CANCEL]`；PLACE 抛错 -> 只发生 1 次 PLACE、`cancel_skipped=True` | PASS x2 |
| R5 | Algo/普通分账：`/fapi/v1/order`->STANDARD、`/fapi/v1/algoOrder`->ALGO；请求成功≠成交：200 NEW 无成交 -> `ACKNOWLEDGED`；空响应 -> `UNKNOWN`；CANCELED 带成交 -> `TERMINAL_PARTIAL` | PASS x4 |

---

## 8. known_limits ②（`EXPIRED_IN_MATCH`）—— **真实可达的未登记 P2（N7）**

探针 `q8_expired_in_match.py`（输出 `q8_out.txt`）。

事实：

- `EXPIRED_IN_MATCH` 在 `order_outbox._TERMINAL` 里、在 `order_mirror` 里、在 `_STATUS_ALIASES`
  里 —— 它是**Binance USDⓈ-M 真实订单状态**（STP 撮合中过期），不是假想值；`_ACTIVE|_TERMINAL`
  判定通过，因此 `apply_event` 会把它**原样写进 status 列**（该列无 CHECK 约束，`TEXT NOT NULL`）。
- **但 `_QUANTITY_TERMINAL` 不含它**，所以 `_update` 的"终态同步三分量"分支**不执行**。

实测（r3 与 r2code **完全相同** ⇒ 既有缺陷，非本轮引入）：

```
B1 无成交（挂单在撮合中过期，最常见）
   ROW status='EXPIRED_IN_MATCH' accepted=0.02 filled=0 working=0.02 terminal=0
       <<< 终态行 working=0.02、terminal=0 —— 既违反词表，又违反 K04 终态不变量
   OrderQuantity 重水化 -> ValidationError（status 不在 Literal 内）
   迟到成交后 ROW filled=0.001 working=0 terminal=0.019（分量被"救"回来了，状态列仍是别名）
   apply_fill_revision 返回 status='EXPIRED'（返回值是词表内的，与落库行不一致）
B2 部分成交后过期：同上，status='EXPIRED_IN_MATCH'，重水化 ValidationError
```

**判定：真实可达、未登记。** 我检查了 `docs/contracts/README.md` §12 的 L1–L20，以及全仓
`grep EXPIRED_IN_MATCH`，**没有任何一处把它登记为已知限制**；作者"非本次范围"的说法在
"非本轮引入"这层成立，但不改变"未登记"这一事实。

**严重度 P2**（不动用资金、写出方不崩、`terminal` 桶少记、行可被后续迟到成交部分纠正），但它
(a) 让持久化行**永久无法重水化成契约对象**（任何按行读取的消费者/对账/审计重放都会抛
`ValidationError`），(b) 在 B1 情形留下 `working != 0` 的终态行（K04 不变量），(c) 与 N1/N6 属
同一缺陷族（`status` 列与分量来源不一致）。**按标尺，未登记的 P2 ⇒ 必须处置**（登记或修复）。
最小修法建议：`_QUANTITY_TERMINAL` 用别名后的集合判定，并让落库 status 也走
`_STATUS_ALIASES`（即 `_update` 前先归一 `status = _STATUS_ALIASES.get(status, status)`）。

---

## 9. 新发现汇总

| ID | 级别 | 内容 | 是否已登记 |
|---|---|---|---|
| **N6** | **P1** | `_component_status` 修后，`apply_event` 先写 `status=FILLED` 再入账成交，导致 `FILLED` 帧走 `working,terminal=0,0` 分支 → 守恒违规 → **成交被静默丢弃、行成非法态且不可自愈**。覆盖"部分成交→全成"与"一笔全成"两条主路径。作者验收未覆盖（无 `X=FILLED` 帧）。 | **否** |
| **N7** | **P2** | `EXPIRED_IN_MATCH` 经私有流落库后 `status` 列保留交易所别名，`OrderQuantity` 词表不接受（行无法重水化）；且不在 `_QUANTITY_TERMINAL` 故三分量不同步（B1 留下 `working=0.02` 的终态行）。r2code 同样，属既有缺陷。 | **否**（作者称"非本次范围"，但我未在任何登记面找到） |
| N2' | P3 | §4c：N2 结构断言可被"对象正确、读路径退化"的回归绕过（反例实测 16/20000 裸仓且 [10] 7/7 全过）。 | 否（测试强度） |
| N3' | P3 | §3(c)：非布尔 `flat_confirmed` 的降级是静默的（与 `False` 不可区分）；`np.True_` 也被拒。 | 否（建议加 reason/log） |
| N8 | 观察 | `apply_event` 末尾无条件 `self.last_error = None` 会抹掉本次入账失败的痕迹（N6 因此完全静默）。既有代码，但被 N6 放大。 | 否 |
| N9 | 观察 | `_update` 对 `REJECTED`/`FILLED`（`filled < accepted`）也按"终态语义"写 `terminal=accepted-filled`，本身就会造出非法行（`REJECTED_WITH_ACCEPTED` / `FILLED_STATUS_WITH_RESIDUAL`）。r2code 相同 ⇒ 既有；前置序列物理上不可能（部分成交后被 REJECTED / FILLED 帧无成交），故不阻塞。 | 否 |

---

## 命令清单

```
# 补丁可应用性（repo 根）
git apply --check tests/fixtures/execution/patches/{binance_futures.u04_adaptation,order_outbox,protection_mirror.decimal_text,run_offline_regression.register_u04}.patch
# 造树
git archive <commit> | tar -x -C _scratch_Q/tree_<x>
git apply --directory=_scratch_Q/tree_<x> <该 commit 版 patch>
# 探针
python _scratch_Q/q1_n1_terminal.py tree_r3 tree_r2code        > q1_out.txt
python _scratch_Q/q2_fill_sequence.py tree_r3 tree_r2code tree_v1code   # 见 q2_r3.txt / q2_r2_v1.txt
python _scratch_Q/q4_n2_guard.py tree_r3                        > q4_out.txt
python _scratch_Q/q4_n2_guard.py tree_r3_mut                    > q4_mut_out.txt
python _scratch_Q/q5_race_mut.py tree_r3 20000                  # 0 bare
python _scratch_Q/q5_race_mut.py tree_r3_mut 20000              # 6~20 bare（多次）
python _scratch_Q/q5_race_mut.py tree_v1code 20000              # 26~33 bare
python _scratch_Q/q6_n3_bool.py tree_r3 tree_r2code             > q6_out.txt
python _scratch_Q/q8_expired_in_match.py tree_r3 tree_r2code    > q8_out.txt
python _scratch_Q/q7_delta.py tree_pristine delta_pristine.json
python _scratch_Q/q7_delta.py tree_r3       delta_r3.json
# 验收套件
python _scratch_Q/tree_r3/tests/acceptance/test_execution_faults.py               # 154/0 exit 0
python tests/acceptance/test_execution_faults.py                                  # 130/0 exit 0（head）
python _scratch_Q/tree_r2code/tests/acceptance/test_execution_faults.py           # r3 测试跑未修树 -> exit 1
python _scratch_Q/tree_v1code/tests/acceptance/test_execution_faults.py           # ImportError（V1 缺常量），改用 q4 的单点结构探针
python _scratch_Q/q9_regression.py tree_r3                                        # 15/15 PASS
# 全量 delta（同一 70 条清单 + 同一 env，逐脚本）
python _scratch_Q/q7_delta.py tree_r3       delta_r3.json
python _scratch_Q/q7_delta.py tree_pristine delta_pristine_full.json _scratch_Q/tree_r3/scripts/run_offline_regression.py
```

原始输出（全部在本目录）：`apply_check_r3.txt` `acc_tree_r3.txt` `acc_head.txt`
`acc_r3tests_on_r2code.txt` `acc_tree_r3fix2.txt` `q1_out.txt` `q2_r3.txt` `q2_r2_v1.txt`
`q4_out.txt` `q4_mut_out.txt` `q5_out.txt` `q6_out.txt` `q6_r2code.txt` `q8_out.txt`
`q9_out.txt` `delta_r3.json` `delta_pristine_full.json` `delta_summary.txt` `diff_tests.txt`。

---

## 未覆盖 / 离线无法验证（不写"通过"）

1. 真实 Binance USER_STREAM 报文回放：`X`/`z`/`l`/`t` 的字段类型与帧序（我用符号级推演 + 官方语义
   构造，**无真实报文可验**）。N6 的触发只依赖"`X=FILLED` 帧带 `l>0`"这一帧序，我有较高把握，但
   真机回放会更有力。
2. `EXPIRED_IN_MATCH` 的触发频率（STP 场景）无法离线度量。
3. `apply_exchange_quantities` / `reconcile_unknown` 是否会在生产时序上"顺带修好" N6/N7 的非法行
   —— 我只验证了 `status='UNKNOWN'` 的行才会进 `reconcile_unknown`，FILLED/EXPIRED_IN_MATCH 行
   不在此列，故判"不可自愈"；但未穷举所有对账入口。
4. 未读 `.env`/`data/`/密钥/账户标识；未联网；未下单/撤单；未启停服务；未写 `mat-wt/I`、
   `mat-wt/B` 或任何其它 worktree；所有产物只落在 `reviews/U04/R3/r1/_scratch_Q/`。

## 结论：**CHANGES_REQUESTED**

1. **N1 真闭合**（我已独立复现前后对照，绕过尝试 6 类全部失败）—— 但**同为 P1 的 N6 是本轮新引入
   的回归**，命中"部分成交→全成"主路径，静默丢成交 + 落非法行，必须修（§1b 给了实测过的一行修法）。
2. **N3 闭合**（附 P3 静默降级观察）。
3. **N2 方向正确、非空转，但可绕过**（反例实测），P3 残余。
4. **N7（EXPIRED_IN_MATCH）真实可达且未登记**，P2，需登记或修复。
5. **全量 delta = ∅**（pristine 与打补丁树失败集合逐条相同，唯一失败为既有 KI02
   `smoke_windows_service_plan.py`，两侧报错文本逐字一致）——本任务**未给既有脚本引入新失败**。
6. 上一轮已 CLOSED 项本轮**未见回退**：`git diff cc6de61 f4e4663 -- src/` **只**动了
   `protection_commands.py` 的 2 处 `flat_confirmed` 判断（+docstring），
   `guard_fault_action`（P1-A2）与 `ProtectionSyncReasons`/`_resync_protection_impl`（P1-P1）
   **逐字节未变**（`binance_futures.u04_adaptation.patch` 在 r2→r3 之间也无 diff），故上轮判定仍成立
   —— 但**我本轮未重跑** A2 的 35 名枚举与 P1-P1 的 20000 轮竞态原探针，此处是"diff 推定"而非"重测"。
   P1-P2 第一笔成交我**重测**了（`q2` S5 + `q6` R1/R2 + 验收 [11]）；其"终态后修订"子路径本轮
   **修好，但另开了一个更宽的口子（N6）**。
7. 回归抽样 6 类 15 项全 PASS，未见其它回退。

**Go/No-Go**：修掉 N6（§1b 已给一行、实测过、不破坏 154/0 与 N1 的修法）+ 处置 N7（登记或修）
后可转 GO；N2'/N3'/N8/N9 均为 P3/观察，不阻塞。

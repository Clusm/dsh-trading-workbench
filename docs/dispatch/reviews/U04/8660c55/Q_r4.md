# U04 修复轮 r4 独立复验报告 — Q 角色（独立会话）

- 复验对象：`8660c558c40198bf3cc73c194b7283d88b382272`（分支 `task/U04`，detached）
- 父链：`f4e4663`（Q_r3 判 CHANGES_REQUESTED：N6 P1 + N7 P2 未登记）→ `33bbb43`（修复）→ `8660c55`（仅追加 `_scratch_B/r4/delta_final.json`）
- 真正改生产的提交是 **`33bbb43`**（`8660c55` 只加了一个证据 json）
- 工作区（独立、detached、只读）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U04\R4\r1`
- 上一轮报告（**已读**）：`...\I\docs\dispatch\reviews\U04\f4e4663\Q_r3.md`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 所有结论来自**我自己写的探针**（`_scratch_Q/q*.py` + `q*_out.txt`），未采信作者的 `_scratch_B/*`。

**结论：PASS-with-notes**（详见 §10/§11；唯一前置条件：把 N8 登记进已知限制面或按 §10 的一行改法修掉）

- N6（r3 引入的 P1）**真闭合** —— 前后对照 + 未修树 155/12（非空转）+ 帧序 fuzz（r3=237 非法 → r4=0）三重独立证据；我设计的 6 类绕过全部被挡。
- N7 **私有流路径真修**（归一 + 三分量同步 + `acknowledge` 连带修好），但**归一不完整**：N8 —— 同一列的第二个写入者 `manual_resolve_unknown` 绕过 `_update`，其白名单自带 `EXPIRED_IN_MATCH`。
- N1 未回退；N2 残余登记诚实（我实测"读路径退化"下全套件 167/0、并发 7/8/11 per 20000）；**全量 delta = ∅**；回归抽样 6 类 22 项全 PASS。

---

## 0. r3 → r4 的生产代码 delta（我自己 diff 出来的，全量）

`diff -r _scratch_Q/tree_r3/src _scratch_Q/tree_r4/src` 只有 **1 个文件、2 处逻辑改动**：

1. `src/exchange/order_outbox.py` L76（`_component_status`）：

```python
-    if stored in _QUANTITY_TERMINAL:
+    if stored in _QUANTITY_TERMINAL and not (stored == "FILLED" and filled < accepted):
```

2. `src/exchange/order_outbox.py` L340-344（`_update`）：

```python
+            alias = _STATUS_ALIASES.get(str(status).upper())
+            if alias:
+                status = alias
+            previous_raw = str(previous["status"] or "SUBMITTING")
+            previous_status = _STATUS_ALIASES.get(previous_raw.upper(), previous_raw)
```

（其余全是 docstring / 注释；`src/` 下**没有**任何其它文件变动。）

---

## 1. N6 前后对照（自建探针 `q1_n6_compare.py`，走生产私有流路径 `apply_event`）

原始输出：`q1_r3.txt` / `q1_r4.txt`。

| 场景 | r3（未修，`f4e4663`） | r4（`8660c55`） |
|---|---|---|
| **S1 部分成交→全成** | `filled=0.0105 working=0 terminal=0.0095` **不可重水化** `FILLED_STATUS_WITH_RESIDUAL; STATUS_FILL_MISMATCH` + `fill revision skipped` 风险事件（warn）+ `last_error=None` | `filled=0.02 working=0 terminal=0.00` **可重水化 OK**，无风险事件 |
| **S3 一笔全成** | `filled=0`（整笔丢失）`terminal=0.02` 非法 + 风险事件 | `filled=0.02 working=0 terminal=0.00` OK，无风险事件 |
| S4 全成帧不带 `l`（物理不可能） | 非法 | **同样非法**（`FILLED_STATUS_WITH_RESIDUAL; STATUS_FILL_MISMATCH`）→ 既有，非本轮 |
| S5 第一笔成交（直调修订） | `PARTIALLY_FILLED 0.0047/0.0153` OK | **同**（无回退） |
| S6 CANCELED/EXPIRED + 迟到成交 | `filled=0.0057 working=0 terminal=0.0143` OK，终态保留 | **同**（N1 未回退） |
| S6 REJECTED + 迟到成交 | 非法 `REJECTED_WITH_ACCEPTED_QTY` | **同**（既有，物理不可达） |

→ **N6 真闭合**：两条主路径的成交都真正入账、行合法、无静默风险事件；且未回退 N1。
`S4`/`S6-REJECTED` 的非法态在 r3 上**逐字相同**，是本轮之前就有的（作者已在测试里登记为已知限制）。

（原始输出见文件尾部附录。）

---

## 2. N6 是否非空转（未修树跑同一套件）—— **非空转（12 条真 FAIL）**

我自己造未修树 `_scratch_Q/tree_r3`（`f4e4663` 源 + `f4e4663` 版补丁），把 **r4 的测试文件原样覆盖**进去后跑：

```
tree_r4（r4 源码 + r4 测试）  EXIT=0   结果: 167 通过 / 0 失败
tree_r3（r3 源码 + r4 测试）  EXIT=1   结果: 155 通过 / 12 失败
tree_r4src（未打补丁 HEAD + r4 测试）  EXIT=0  结果: 130 通过 / 0 失败（[11] 走 PENDING 分支）
```

12 条 FAIL 全部落在本轮新增的用例上（`_scratch_Q/acc_r4tests_on_r3.txt` L200-212）：

| 新增用例 | r3 树上的实际值 |
|---|---|
| N6-a 1/5 `FILLED 帧的成交量真正入账` | `filled=0.0105`（应为 0.02） |
| N6-a 2/5 `行合法且守恒` | `FILLED / working=0 / terminal=0.0095` |
| N6-a 3/5 `成交 id 被持久化` | `applied=["88801"]`（缺 88802） |
| N6-a 4/5 `未产生风险事件` | 有 `fill revision skipped` |
| N6-a 5/5 `重复投递不二次计账` | 行非法 |
| N6-b 1-3 | `filled=0`（整笔丢失）+ 非法 + 风险事件 |
| N6-c | `booking_error=QuantityError: THREE_COMPONENT_CONSERVATION_VIOLATION; STATUS_FILL_MISMATCH` |
| N7 x3 | `status='EXPIRED_IN_MATCH'`、`working=0.02`、重水化 `ValidationError` |

→ **作者"154→167、未修树 155/12"逐字复现成立**（154 + 13 条新断言 = 167；未修树上 13 条里 12 条 FAIL，
只有 N6-a 的前置检查"前档部分成交已入账"在两侧都过）。**断言不是恒真**。

---

## 3. N6 绕过尝试（自建 `q2_n6_bypass.py`，`q2_r4.txt`）—— **全部被挡**

| 绕过 | 结果 |
|---|---|
| `_component_status` 直调 `accepted=0.02/stored=FILLED` | `filled=0→NEW`；`0.0047/0.0105/0.0199→PARTIALLY_FILLED`；`0.02→FILLED`；**返回值恒在词表内** |
| **`filled > accepted`（超额）** | `_component_status` 返回 `FILLED`，但 `OrderQuantity.checked` 抛 `FILLED_EXCEEDS_ACCEPTED; THREE_COMPONENT_CONSERVATION_VIOLATION` → `apply_fill_revision` 抛错 → **fail-closed**（未落库、未静默） |
| **`filled == accepted` 但 `terminal > 0`** | 只能靠**直接篡改落库行**构造；`apply_fill_revision` 重算三分量并抛 `fill exceeds accepted quantity`，**不会把非法态洗白**。代码路径本身无法产出该组合（`_update` 写 `terminal=accepted-filled`，相等即 0） |
| **分数边界 `0.0047`** | `stored=FILLED, filled=0.0047 < accepted=0.02` → 退回分量推导 → `PARTIALLY_FILLED`，成交入账成功（`filled=0.0097`） |
| 其它终态带残量 `CANCELED/EXPIRED/REJECTED + working!=0` | CANCELED/EXPIRED → 保留终态、`working=0`、`terminal=accepted-filled`、**可重水化**；REJECTED 仍抛 `REJECTED_WITH_ACCEPTED_QTY`（**r3 逐字相同 ⇒ 既有，且前置序列物理不可达**） |
| `model_copy` 篡改 | `violations()` 立即报 `THREE_COMPONENT_CONSERVATION_VIOLATION` / `WORKING_NONZERO_WHILE_TERMINAL` / `FILLED_EXCEEDS_ACCEPTED`；`OrderQuantity.checked(**dump)` 全部抛错。（`model_validate(dump)` 不复跑 `violations()` —— 但这是**已登记**的 L11："构造器不内建校验，仅 `checked()` 严格"；生产读路径只用 `checked`） |
| 重复投递同一 FILLED 帧 x3 | `filled=0.02` 不二次计账、行合法、无风险事件、`applied=["99901"]` 只有一个 id |
| 同一 FILLED 帧换新 `t` 再投 | 抛 `fill exceeds accepted quantity` → 行保持合法、只记一条 warn |
| `accepted=0`（REJECTED 清零行）收 FILLED | `_update` 终态守卫挡住（REJECTED→FILLED 被拒），行不变 |

**结论：N6 真闭合，我设计的 6 类绕过无一成功。**唯一能造出非法态的方式是**直接改写 SQLite 行**（进程外篡改），
且写入方的读取路径也会拒绝它（fail-closed）。

---

## 4. N1 未回退 + 帧序 fuzz（自建 `q4_n1_bypass.py` / `q8_fuzz.py`）

### 4a. `q4_r3.txt` vs `q4_r4.txt` 全量 diff 只有 3 处（**全部是预期修复**）

```
7c7   'FILLED' -> 'FILLED'          |  'FILLED' -> 'PARTIALLY_FILLED'      (N6 守卫)
22c22 FILLED filled=0 -> FILLED     |  FILLED filled=0 -> NEW              (N6 守卫)
52c52 EXPIRED_IN_MATCH 行 status 保留别名、不可重水化
      |  EXPIRED_IN_MATCH 行 status=EXPIRED、可重水化 OK                    (N7 归一)
```

其余**逐字节相同** ⇒ N1（CANCELED/EXPIRED 迟到成交保留终态）**未回退**：

```
C5/C6 CANCELED          filled=0.0077 working=0 terminal=0.0123 可重水化 OK 无风险事件
      EXPIRED          同上
      EXPIRED_IN_MATCH 同上（status=EXPIRED）
      REJECTED         REJECTED_WITH_ACCEPTED_QTY——r3 逐字相同，物理不可达，既有
C1 终态+超额成交  -> 行不变、合法；成交抛 "fill exceeds accepted quantity"（fail-closed）
C2 终态+负量      -> 行不变、合法；抛 QuantizationInputError
C3 行 status 脏值(SOMETHING_NEW/''/'   '/123) 后投成交 -> 返回值恒在词表内，且行被改写成 PARTIALLY_FILLED
C4 model_copy 篡改 -> checked(**dump) 全部抛错（见 §3）
```

### 4b. 帧序 fuzz：**物理可达扫描 400 条生命周期 · r3=237 非法 · r4=0**

`q8_fuzz.py <tree> 20260921 400`（固定种子，随机 0~3 笔单调部分成交 + 合法终态帧
`FILLED/CANCELED/EXPIRED/EXPIRED_IN_MATCH`，每步重水化落库行）：

```
tree_r3 : [A 物理可达] 非法步数 = 237 / 400   （FILLED_STATUS_WITH_RESIDUAL、
                                               EXPIRED_IN_MATCH ValidationError …）
tree_r4 : [A 物理可达] 非法步数 =   0 / 400   <<< 强证据：N6+N7 闭合，且扫描非空转
[B 敌意扫描] 705 -> 475；r4 侧**不再出现 EXPIRED_IN_MATCH 行**，
  剩余全部是"准备态行(SUBMITTING/None 本就不在 OrderQuantity 词表)"与
  "物理不可达序列(REJECTED 后再来成交、FILLED 帧不带 l、带残量 REJECTED)"
```

r4 剩余非法类的 key 集合 ⊂ r3 的 key 集合（**未引入新类别**）。

---

## 5. N7 归一是否完整（自建 `q3_n7.py`）

### 5a. 私有流路径：**真修**

```
B1  EXPIRED_IN_MATCH 无成交      -> status=EXPIRED filled=0  working=0 terminal=0.02  可重水化 OK
B2  EXPIRED_IN_MATCH 后迟到成交  -> status=EXPIRED filled=0.001 working=0 terminal=0.019 可重水化 OK 无风险事件
D   acknowledge(status=EXPIRED_IN_MATCH) -> EXPIRED（连带修好 REST 路径）
```

作者测试里"落库为词表内终态 / 无成交时三分量同步 / 迟到成交保留终态"3 条我逐条复现成立。

### 5b. **历史遗留行自愈 —— 作者的 docstring 说法过宽（P3）**

`_update` 的注释称"顺带把历史遗留的别名行也归一，使旧行在下一个事件到达时自愈"。实测（`q3_r4.txt` B3）：

| 遗留行（raw SQL 造 `status='EXPIRED_IN_MATCH'`、`working=0.02`）收到 | 结果 |
|---|---|
| 下一个事件 X=`EXPIRED_IN_MATCH` | **自愈**：`status=EXPIRED working=0 terminal=0.02` 可重水化 |
| 下一个事件 X=`CANCELED` | **不自愈**：仍在 `EXPIRED_IN_MATCH`、`working=0.02`、不可重水化（终态→其它终态被不变量守卫挡住，符合预期） |
| 只来一笔成交修订（无 `_update`） | **状态列不自愈**（分量被修正为 `working=0/terminal=0.019`，但 status 列仍是别名，仍不可重水化） |

即：**归一仅对"再次到达同语义状态"的事件生效**。方向是 forward-only（不再新增毒行），但
"下一个事件到达即自愈"对绝大多数后续事件不成立。

### 5c. **反向枚举：还有哪些别名/写入者能落库 —— 找到 1 个漏网点（N8）**

`_STATUS_ALIASES` 只映射 `EXPIRED_IN_MATCH -> EXPIRED`。已枚举的交易所状态串：

```
NEW/PARTIALLY_FILLED/FILLED/CANCELED/EXPIRED/REJECTED -> 本就是词表值
EXPIRED_IN_MATCH -> 归一为 EXPIRED ✓（`_update` / `_component_status` / `acknowledge` 三处生效）
FINISHED / CANCELLED / PARTIALLYFILLED / PENDING_NEW / TRIGGERED / NEW_INSURANCE / NEW_ADL / UNKNOWN
   -> 不在 _ACTIVE|_TERMINAL，`apply_event` 一律回落到行内既有状态、`acknowledge` 一律回落
      到 ACKNOWLEDGED ⇒ **不会落库**（fuzz 实测：X=CANCELLED 行 status 仍是 SUBMITTING）
```

**但同一列有第二个写入者绕过 `_update`：`manual_resolve_unknown`（`order_outbox.py` L506/L522）**

```python
terminal = str(status).upper()
if terminal not in {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:   # ← 白名单里就有别名
    raise ValueError("人工收口只允许无成交类终态")
...
"UPDATE order_outbox SET status=?,resolution_note=?,resolved_by=?,resolved_at=?,updated_at=?,"
"next_retry_at=NULL WHERE client_order_id=?"      # ← 直接 SET，不经 _update，不做别名归一
```

实测（`q3_r4.txt` [C]，**r3 与 r4 逐字相同 ⇒ 既有、且本轮未覆盖**）：

```
manual_resolve_unknown(status='EXPIRED_IN_MATCH')
  -> status='EXPIRED_IN_MATCH'  working='0.02'  terminal='0'  重水化 ValidationError
manual_resolve_unknown(status='CANCELED')
  -> status='CANCELED'  working='0.02'  terminal='0'  QuantityError: WORKING_NONZERO_WHILE_TERMINAL
manual_resolve_unknown(status='CANCELLED'/'SOMETHING_NEW') -> ValueError（白名单挡住）
```

即：(i) **`EXPIRED_IN_MATCH` 仍然能落进同一列**（N7 的同一症状：行永久不可重水化）；
(ii) 该方法**完全不做三分量同步**，连合法的 `CANCELED` 也会留下终态行 `working != 0`（K04 终态不变量）。

**可达性**：全仓 grep **没有任何生产调用点**（只有 `scripts/smoke_order_outbox.py` 传 `"CANCELED"`）；
操作者需显式传别名才会命中。无任何消费者把该列重水化成 `OrderQuantity`
（`release_evidence` 只数 `UNKNOWN`/`SUBMITTING`，`order_outbox.status()` 只 GROUP BY）。
⇒ 后果与 N7 同族、但**不生产可达、不 fail-open、不崩溃**。

---

## 6. N2 残余登记 —— **登记文字存在且实质准确**（自建 `q5_race.py` / 受控回归树）

登记文字（`tests/acceptance/test_execution_faults.py` L1099-1108）逐字存在，声称三点：
(a) 结构断言只查"对象里存了什么"、不查读路径；
(b) 受控反例（对象正确、仅读路径退化为进程级标量）**本节 7 条断言全过**；
(c) 该反例并发实测仍有 **6~20/20000 次裸仓**。

我自建了**受控回归树 `tree_r4_mut`**（12 行改动：`_mut_mark` 包一层写进程级标量 + 把
`_ensure_protection_from_order` 的**读**换成该标量，`ProtectionSyncReasons` 对象侧与
`begin()/mark()/get()` 语义**完全正确**）：

```
tree_r4_mut + r4 全套件 -> EXIT=0  结果: 167 通过 / 0 失败
   [10] 节 11 条断言（共享实例 8 条 + 单元 3 条）全部 PASS
```

我另行自建的并发探针 `q5_race.py`（A 标的查询失败 / B 标的真实保护缺口 + 兜底平仓计数，
`sys.setswitchinterval(1e-6)`，只替换交易所 I/O 边界）：

| 树 | 20000 轮 | 裸仓 `bare_B` | 反向 fail-open `close(A)` |
|---|---|---|---|
| `tree_r4`（正确读路径） | 3 次运行 | **0 / 0 / 0** | 0 |
| `tree_r4_mut`（读路径退化） | 3 次运行 | **7 / 8 / 11** | 0 / 0 / **3** |

**(b)、(c) 均实测成立**（7/8/11 落在登记的 6~20 区间内）。→ **登记诚实、不空转**。

偏差（P3，不影响结论）：
1. "本节 **7** 条断言" 计数不准 —— [10] 节共享实例部分实为 **8** 条，全节共 **11** 条（均 PASS）。数字偏小，不构成夸大。
2. 登记只提"裸仓"，未提**反方向**的 fail-open（我的反例同时产生了 3/20000 次"A 标的确证观测故障被误平仓"）。
3. 该反例的实际危害是"读路径退化"，与 V1 的进程级标量同类但触发窗口更窄（只在 `_resync_protection`
   释放锁之后、读之前），登记文字对此描述准确。

---

## 7. 补丁可应用性 + 集成态验收

```
git apply --check（repo 根，逐个）:
  binance_futures.u04_adaptation.patch   rc=0
  order_outbox.patch                     rc=0
  protection_mirror.decimal_text.patch   rc=0
  run_offline_regression.register_u04.patch rc=0      （_scratch_Q/apply_check_r4.txt）

实打应用（git apply --directory=_scratch_Q/tree_r4 ...）4/4 OK
tree_r4 验收套件       EXIT=0   167 通过 / 0 失败   U04 EXECUTION FAULTS OK
未打补丁 HEAD 验收套件 EXIT=0   130 通过 / 0 失败（[11] PENDING）
```

---

## 8. 全量回归 delta（强制）—— **差集 = ∅**

自建 `q6_delta.py`：用 `ast` 读出 **同一份 70 条清单**（`tree_r4/scripts/run_offline_regression.py` 的
`SCRIPTS`，含 U04 新登记的 `smoke_u04_execution_faults.py`），用**同一 env**
（`EXECUTION_MODE=paper`、删 `MTA_PROFILE`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、
剥离 `*BINANCE*/*SENSENOVA*` 的 KEY/SECRET/TOKEN），逐脚本在各自树根跑并记录 rc：

| 树 | 清单条数 | 失败集合 |
|---|---|---|
| `tree_r4src`（补丁**未**应用） | 70 | `{scripts/smoke_windows_service_plan.py}` |
| `tree_r4`（补丁**已**应用） | 70 | `{scripts/smoke_windows_service_plan.py}` |

```
set diff (r4 - pristine) = []
set diff (pristine - r4) = []
70 个脚本 rc 全部逐条相同
```

唯一失败是**既有 KI02**（`load_windows_service_config` 找不到本机 `.venv\Scripts\python.exe`），
两侧 `src/core/windows_service.py:190` 同一行、同一异常，仅路径里 `tree_r4`/`tree_r4src` 不同。

⇒ **本任务未给任何既有脚本引入新失败。**（`delta_r4.json` / `delta_pristine.json` + 两份 .log）

---

## 9. 回归抽样（6 类 22 项，全部 PASS）

自建 `q7_regression.py`（`q7_r4.txt`，`结果: 0 项失败`；r3 树输出**逐行相同** ⇒ 无回退）：

| 类 | 项 | 结果 |
|---|---|---|
| 三分量守恒 | NEW/PARTIALLY_FILLED/CANCELED/FILLED/EXPIRED/REJECTED 六态守恒 + 4 类非法态被 `checked` 拒 | PASS x10 |
| Decimal 精确 | `0.01 = 0.0047 + 0.0053` 无尾差；`0.1+0.2 == 0.3` 文本；Decimal 入参 | PASS x3 |
| UNKNOWN 不重发 | 重复 `clientOrderId` 拒；`mark_unknown` 落 UNKNOWN；决策 `QUERY_DUE` 且 `repost/new_client_id=False`；`reconcile_unknown` 源码无提交调用；同业务键未决冲突拒 | PASS x5 |
| PLACE 先于 CANCEL | `ordered()` PLACE 全在 CANCEL 前；实测 PLACE 先于 CANCEL 且未跳撤；PLACE 抛错→只 1 次 PLACE + `cancel_skipped`；`defers_flat_close` 只对观测故障 | PASS x4 |
| Algo/普通分账 | `/fapi/v1/order`→STANDARD；`/fapi/v1/algoOrder`→ALGO | PASS x2 |
| 请求成功≠成交 | 200+NEW→ACKNOWLEDGED；空响应→UNKNOWN；CANCELED 带成交→TERMINAL_PARTIAL | PASS x3 |

---

## 10. 新发现汇总

| ID | 级别 | 内容 | 是否登记 |
|---|---|---|---|
| **N8** | **P3（从严口径可判 P2）** | `manual_resolve_unknown` 是 `order_outbox.status` 的**第二个写入者**，直接 `UPDATE ... SET status=?`，**绕过 `_update` 的别名归一**，且其白名单里**就写着 `EXPIRED_IN_MATCH`** ⇒ 该别名仍能落进同一列（行永久不可重水化，N7 同症状）；该方法**也不做三分量同步**，连合法 `CANCELED` 都留下终态行 `working=0.02`（K04 终态不变量）。r3 逐字相同（既有、非本轮引入），全仓 grep **无生产调用点**，操作者需显式传别名才命中，且无消费者重水化该列。 | **否** |
| N7' | P3 | `_update` docstring 称历史遗留别名行"下一个事件到达时自愈"，实测仅当后续事件**再次归一为同一终态**时自愈；`CANCELED` 等其它事件或仅成交修订都**不自愈**（分量可修正，status 列仍是别名）。 | 否（注释过宽，无断言依据它） |
| N10 | 观察 | `apply_event` 的帧序 `_update(status=X)` → `apply_fill_revision(l)` 意味着"全成帧不带 `l`/`t`"会留下 `FILLED + filled<accepted` 的非法行且**无法自愈**。作者已在测试内登记为已知限制（要求"全成帧必带 `l`"，交易所语义成立）；我 fuzz 复核：只要 `l>0 && t` 存在就自愈，否则长期非法。 | 是（测试内已登记） |
| N11 | 观察 | r4 新增用例 N6-c 的 `h2` 帧 `dict(event, **{"X":"FILLED","l":"0","t":0})` 会**继承夹具基帧的 `z="0.0047"`**，构成 `X=FILLED` 但 `z(0.0047) < q(0.02)` 的自相矛盾帧（`apply_event` 不读 `z`，故不影响断言结果）。属夹具卫生问题。 | 否（不影响判定） |
| L11 复核 | 已登记 | `OrderQuantity.model_validate(model_dump())` 不复跑 `violations()`（篡改态可"重水化成功"）；但生产读路径只用 `OrderQuantity.checked`，且 `docs/contracts/README.md` §12 **L11** 已登记"构造器不内建校验，仅 `checked()` 严格"。**非新发现**。 | 是（L11） |

**N8 为何不判 CHANGES_REQUESTED（我的判断依据，供编排者复核）**：
(a) **不生产可达**——全仓无任何调用点（对比 N7 的 `apply_event` 由 `src/dashboard/app.py:1232` 接线），
操作者需显式传别名；(b) **不 fail-open**——不放行危险动作、不影响保护/平仓路由，只是数据卫生；
(c) 无任何消费者把该列重水化成 `OrderQuantity`；(d) 与 r3 逐字相同 ⇒ 非本轮回归，且本轮 `_update`
归一对其只有改善没有恶化。Q_r3 把 N7 升为阻塞的核心依据正是"**真实可达**"，按同一把尺子，此处应对称降级。
**但**：本轮 N7 选的是"修"而非"登记"，而修法只覆盖了三个写入者中的两个；同一列的第四个
（`recover_stale_submitting`/`mark_unknown` 写 `UNKNOWN`，在词表内）无需处理。
**建议**（一行，零风险）：`manual_resolve_unknown` 入口也走 `_STATUS_ALIASES` 归一 + 复用
`_update(..., status=<归一后>)`（可同时修好三分量不同步）。**建议在合入前登记或修掉。**

---

## 命令清单

```bash
# 工作区（detached，独立）
cd C:\Users\Tao\Desktop\币安\mat-wt\reviews\U04\R4\r1     # HEAD = 8660c55
# 补丁可应用性（repo 根，逐个）
git apply --check tests/fixtures/execution/patches/{binance_futures.u04_adaptation,order_outbox,protection_mirror.decimal_text,run_offline_regression.register_u04}.patch
# 造树（只写 _scratch_Q）
git archive 8660c55 | tar -x -C _scratch_Q/tree_r4src        # 未打补丁 HEAD
git archive 8660c55 | tar -x -C _scratch_Q/tree_r4 ; git apply --directory=_scratch_Q/tree_r4 <4 patches>
git archive f4e4663 | tar -x -C _scratch_Q/tree_r3   ; git apply --directory=_scratch_Q/tree_r3 <f4e4663 版 4 patches>
cp -r _scratch_Q/tree_r4 _scratch_Q/tree_r4_mut     # 读路径退化注入（12 行）
# 探针（PYTHONUTF8=1，解释器 multi-agent-trader\.venv\Scripts\python.exe）
python _scratch_Q/q1_n6_compare.py r3|r4            > q1_r3.txt / q1_r4.txt
python _scratch_Q/q2_n6_bypass.py  r3|r4            > q2_r3.txt / q2_r4.txt
python _scratch_Q/q3_n7.py         r3|r4            > q3_r3.txt / q3_r4.txt
python _scratch_Q/q4_n1_bypass.py  r3|r4            > q4_r3.txt / q4_r4.txt
python _scratch_Q/q5_race.py r4 20000 1e-6  (x3)    # bare=0/0/0   spurious=0
python _scratch_Q/q5_race.py r4_mut 20000 1e-6 (x3) # bare=7/8/11  spurious=0/0/3
python _scratch_Q/q7_regression.py r3|r4            > q7_r3.txt / q7_r4.txt
python _scratch_Q/q8_fuzz.py r3|r4 20260921 400     > q8_r3.txt / q8_r4.txt
# 验收套件
python _scratch_Q/tree_r4/tests/acceptance/test_execution_faults.py        # 167/0 exit0
python _scratch_Q/tree_r3/tests/acceptance/test_execution_faults.py        # 155/12 exit1（r4 测试跑未修树）
python _scratch_Q/tree_r4src/tests/acceptance/test_execution_faults.py     # 130/0 exit0
python _scratch_Q/tree_r4_mut/tests/acceptance/test_execution_faults.py    # 167/0 exit0（读路径退化也全过）
# 全量 delta（同一 70 条清单 + 同一 env）
python _scratch_Q/q6_delta.py r4    delta_r4.json
python _scratch_Q/q6_delta.py r4src delta_pristine.json r4
```

原始输出（全部在本目录）：`apply_check_r4.txt` `acc_tree_r4.txt` `acc_r4tests_on_r3.txt`
`acc_head_r4.txt` `acc_r4_mut.txt` `q1_r3.txt` `q1_r4.txt` `q2_r3.txt` `q2_r4.txt`
`q3_r3.txt` `q3_r4.txt` `q4_r3.txt` `q4_r4.txt` `q7_r3.txt` `q7_r4.txt` `q8_r3.txt`
`q8_r4.txt` `delta_r4.json` `delta_r4.log` `delta_pristine.json` `delta_pristine.log`。

---

## 未覆盖 / 离线无法验证（不写"通过"）

1. **无真实 USER_STREAM 报文回放**：N6/N7 的触发依赖"`X=FILLED`/`EXPIRED_IN_MATCH` 帧带 `l`/`t`"
   这一帧序，我用官方语义 + 符号级推演构造，**无真实报文可验**。
2. **物理不可达的帧序**（`X=FILLED` 不带 `l`、部分成交后被 `REJECTED`）**离线判为不可达**，
   因此 N6-c 的已知限制与 N9 的既有非法态我**没有推翻**，也没有声称它们已被修复。
3. `EXPIRED_IN_MATCH` 的**触发频率**（STP 撮合场景）无法离线度量。
4. **N8 的操作面**：`manual_resolve_unknown` 是否有**本仓库之外**的运维脚本/手册调用（若有，
   且传交易所原始状态串，可达性会升到生产级）。我只查了本仓库。
5. `order_mirror.py` 自己的 `exchange_order_mirror` 表也接受 `EXPIRED_IN_MATCH`（其 `_TERMINAL`
   与 `_RANK` 含它，且该表 status 无 `OrderQuantity` 词表约束）——**不在 U04 范围**（U04 只改
   `order_outbox`），我未展开评价。
6. 未读 `.env`/`data/`/密钥/账户标识；未联网；未下单/撤单；未启停服务；
   未写 `mat-wt/I`、`mat-wt/B` 或任何其它 worktree；所有产物只落在
   `reviews/U04/R4/r1/_scratch_Q/`。

---

## 结论：**PASS-with-notes**

1. **N6 真闭合**（P1 已清）。三条独立证据：①自建前后对照（`q1`）——r3 的
   `filled=0.0105/terminal=0.0095` 守恒违规 + 静默风险事件、`一笔全成 filled=0` 全部消失，
   r4 为 `filled=0.02/working=0/terminal=0.00` 且无风险事件；②未修树跑同一套件 **155/12 真 FAIL**
   （不是恒真），作者"154→167"逐字成立；③**帧序 fuzz 物理可达扫描 400 条生命周期 r3=237 非法 → r4=0**。
   我设计的 6 类绕过（超额 / `filled==accepted` 但 `terminal>0` / 分数边界 / 其它终态带残量 /
   `model_copy` / 重复投递，另加换新 `t` 的重复 FILLED、`accepted=0` 收 FILLED）**无一成功**。
2. **N1 未回退**：`q4` 逐行 diff r3↔r4 只有 3 处差异，全部是预期修复；CANCELED/EXPIRED
   迟到成交仍保留终态且可重水化。
3. **N7 私有流路径真修**（归一 + 三分量同步 + `acknowledge` 连带修好）；**但归一不完整**——
   同一列的第二个写入者 `manual_resolve_unknown` 绕过 `_update`，白名单里就含 `EXPIRED_IN_MATCH`，
   该别名仍能落库（**N8**）。另：docstring 的"下一个事件到达即自愈"过宽（**N7'**）。
4. **N2 残余登记诚实**：登记文字存在；我自建受控回归树实测"读路径退化为进程级标量"时
   全套件 **167/0**（含 [10] 节 11 条断言）；自建并发探针实测裸仓 **7/8/11 per 20000**（登记称 6~20）；
   偏差仅为"7 条"计数不准（实为 8/11）与未提反向 fail-open（我实测 3/20000）。
5. **全量 delta = ∅**：同一 70 条清单 + 同一 env 下 pristine 与打补丁树失败集合逐条相同，
   唯一失败为既有 KI02（`smoke_windows_service_plan.py`，同一行同一异常，仅树路径不同）。
   **本任务未引入新失败。**
6. 回归抽样 6 类 22 项全 PASS，r3/r4 逐行相同。
7. **残留（均不阻塞）**：N8（P3，建议合入前登记或一行修）、N7'（P3 注释）、N10（已登记限制）、
   N11（夹具卫生）、N2'/N3'/N8(上轮)/N9（上轮 P3/观察，本轮未见回退）。
8. **唯一前置条件**：建议在合入前把 N8 登记进已知限制面（或按 §10 的一行改法修掉）；
   若编排者按"任何残留必须已登记"的从严口径，则本项即为该口径下的唯一未达标点。

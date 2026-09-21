# U08 修复轮 r4 独立复验报告（Q 角色 · r5 · 独立会话）

- 复验对象：`task/U08` @ `08ca75d87f58e7e8b38116947a8aa1690c54f3aa`（父链 `9195782 ← fbef4e0`），
  交付物 = 补丁包 `docs/dispatch/patches/U08/9195782/`（5 diff + README，目录名 = r4 独占产物源提交）。
- 我的工作区（独立 detached，已 `rev-parse HEAD` 确认 = `08ca75d`，`git status` 干净）：
  `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U08\R5\r1`
- 上一轮报告（已读）：`mat-wt/I/docs/dispatch/reviews/U08/fbef4e0/Q_r4.md`
  （**CHANGES_REQUESTED**，唯一阻塞 N6：`save()/consume()/release()` 只吞 LockTimeout，
  `OSError`/`PermissionError` 仍穿出整个执行器且未登记）。
- 解释器 `C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest。
- **集成 HEAD 实测**：进场 `b40a5fa`，出报告时 `3a13c7b`（期间只多了一个 `docs/dispatch/RESUME.md` 提交）。
  5 个目标文件在 `b40a5fa`/`3a13c7b`/`1a412440`（补丁自述基线）/`32ecf635` 四处 blob **逐文件相同**（§6.1）。
- 纪律：只读；未提交/推送/打标签/切分支；未读 `.env`/`data/`；未联网；未启停服务；未下单撤单；
  **所有产物只在 `reviews/U08/R5/r1/_scratch_Q/` 内**（未写 `mat-wt/I`、`mat-wt/CD` 或任何其它 worktree）。

---

## 0. 结论

# PASS-with-notes

**上一轮唯一阻塞 N6 真闭合**（我自建前后对照 + 真实文件系统注入，不采信作者对照）；
**N7 真修**（字面实测 `exec-<iso>`）；**`release()` 登记属实且不与必达②冲突**；
补丁可干净应用到当前集成 HEAD；门禁全绿；抽样回归 7/7。

本轮**新发现 1 条 P2（N10，fail-closed，登记不完整）**：`release()` 落盘失败时
返回 `True`，但**同一轮的收尾 `save()` 会用盘上事实把内存里的撤销覆盖回去**，
撤销被静默丢弃且执行器侧"票据可能仍被占死"的 note **永不出现**（不可达分支）。
方向是 fail-closed（票据被占死，**不会**重复下单/重复发射），故不升级为阻塞；
但 §七 登记的机制描述（"落盘留给下一次成功写入"）与代码实际行为**不符**，须更正。

| 任务 | 判定 |
|---|---|
| 1 N6 崩溃穿出 | **真闭合**：3 声称异常 × 4 路径 = 12 条全部 `NO_ESCAPE`；**写盘失败真的回滚内存**（`mem_consumed=false`/`disk_consumed=false`，不留脏"已消费"）；孤儿落盘可审计；异常名进 note；**KI/SystemExit/GeneratorExit 仍穿出** ✅ |
| 2 N7 文档笔误 | **真修**（实测 `exec-<iso>`，`startswith('exec:')=False`；`0c131f2/README.md` 一并更正；全仓有效文档无 `exec:` 残留） ✅ |
| 3 `release()` 返回 True | **登记属实、不与必达②冲突**（fail-closed：只多占死、不重复发射）；→ 但见新发现 **N10** |
| 4 补丁 + 全量 delta | 补丁对当前集成 HEAD **15/15 可应用、无 Skipped**；delta 见 §6.2 |
| 5 门禁 | `QUALITY CHECK OK files=290` / `SECRET SCAN OK files=525` ✅ |
| 6 回归抽样 ≥5 | 7 项全过（接线 7 处、真实路径、跨进程 8×5、故障 vs no_edge 31 码、管理预算、`position_class` 同源、`CARDS_INVALID` 可达） ✅ |

---

## 1.（任务 1）N6 是否真闭合 —— **真闭合**

### 1.0 我的方法与"不 monkeypatch 被测逻辑"的落实

探针：`_scratch_Q/probes/q1_n6.py`（**自建，未引用作者 `n6_probe.py` 的任何输出**）。
两棵树：

| 树 | 构造 | 用途 |
|---|---|---|
| `tree_r3` | `git archive b40a5fa`（pristine 集成树）+ `fbef4e0` 的 4 个独占产物 + `0c131f2` 5 补丁 | **BEFORE = r3 交付态** |
| `tree_head` | `git archive b40a5fa` + `9195782` 的 4 个独占产物 + `9195782` 5 补丁 | **AFTER = r4 交付态** |

注入只用两种，**都不动被测逻辑**（`save/consume/release/run_strategy_trader` 一行未改）：
1. **边界注入**：把 `core.strategy_funnel` 模块里的 `os.replace` 这一个**系统调用**按
   调用序号（1-based）替换为"抛一次指定异常"的包装，抛出前先还原真身（一次性）。
   `os.replace` 的调用序号与四条路径一一对应：认领落盘=1、收尾落盘=2、撤销落盘=2。
2. **真实 OS 失败**：不注入，靠文件系统自己报错（目标文件被占用 / 父路径是普通文件 / 非法文件名）。

> 注：作者 `n6_probe.py` 用的是 `_save_locked`/`save` 替身（改被测对象）。我的注入点更靠底，
> 因此"异常是否被收敛"是真的在**完整的 save→_save_locked→os.replace 调用链**上被测的。

### 1.1 四条路径 × 异常类型（`out/q1_n6_{BEFORE,AFTER}.txt`）

`out/q1_n6_AFTER.txt` 逐行原始输出摘要（`escaped=None` 即 `NO_ESCAPE`）：

```
A 收尾 save() 失败（os.replace 第 2 次）
A save->OSError:          NO_ESCAPE | decision=open intents=1
    reasoning="...t-a 进入待执行...; 账本收尾落盘未完成（OSError），已消费事实留待下次写入 [LEDGER_SAVE_DEFERRED]"
    round_faults=['LEDGER_SAVE_DEFERRED'] details={'LEDGER_SAVE_DEFERRED': 1}
    盘上已消费=True orphan=['t-a'] | note_has_exc=True | 残留 tmp=[]
A save->PermissionError:  NO_ESCAPE（同上，异常名 PermissionError）
A save->OperationalError: NO_ESCAPE（同上，异常名 OperationalError）
B 执行器侧 consume() 失败（os.replace 第 1 次）
B consume->OSError: NO_ESCAPE | decision=open intents=1
    reasoning="t-bad 票据账本不可用（LEDGER_WRITE_FAILED:OSError），跳过
               [READINESS_BLOCKED/TICKET_LEDGER_UNAVAILABLE]; t-ok 进入待执行..."
    detail_counts={'TICKET_LEDGER_UNAVAILABLE': 1}
    t-bad_consumed=False  t-ok_consumed=True      ← 坏票 fail-closed，其余票照常
B consume->{PermissionError,OperationalError}: 同上
C ledger.consume() 内部写盘失败（os.replace 第 1 次）
C consume->OSError: NO_ESCAPE
    claim={"state":"LEDGER_WRITE_FAILED","consumed":false,"error":"OSError",...}
    mem_consumed=False (期望 False) | 盘上文件存在=False
    disk_consumed=False (期望 False) | 盘上记录数=0
    失败后再成功 save(): escaped=None | mem_consumed_after_save=False (期望 False)
    再读盘 disk_consumed=False (期望 False) | 盘上记录数=0    ← 不留脏"已消费"
    残留 tmp=[]
C consume->{PermissionError,OperationalError}: 同上
D release() 写盘失败（真实执行器路径：消费成功 → 建意图失败 → release 落盘炸）
D release->OSError: NO_ESCAPE | decision=no_trade intents=0 | 盘上 consumed=True
D release->{PermissionError,OperationalError}: 同上
```

**BEFORE 同构对照**（`out/q1_n6_BEFORE.txt`，证明"修复前确实穿出、intents 确实全废"）：

```
A save->OSError:          ESCAPED OSError: [Errno 28] No space left on device
    summary=null（decision 根本没返回，intents 全废）
    盘上已消费=True orphan=['t-a'] | round_faults=[] ← 无任何轮级留痕
A save->{PermissionError,OperationalError}: ESCAPED（同）
B consume->OSError: ESCAPED | t-bad_consumed=True ← 内存留下脏"已消费"
C consume->OSError: ESCAPED | mem_consumed=True | 盘上文件存在=False
    失败后再成功 save(): escaped=None | mem_consumed_after_save=True
    再读盘 disk_consumed**=True** | 盘上记录数=1   ← **经典坏账：从未下单的票被洗成盘上已消费**
D release->OSError: ESCAPED
```

### 1.2 重点三项（逐条）

**① `ledger.consume()` 写盘失败是否真的回滚内存** —— **是**。
`out/q1_n6_AFTER.txt` 的 C 组：三种异常下 `mem_consumed=False`、`disk_consumed=False`、
盘上记录数 0、无残留 `.tmp`；**且失败后再成功 `save()`，内存与盘上仍都不是"已消费"**
（`mem_consumed_after_save=False` / `disk_consumed=False`）。
对照 BEFORE 的 C 组：`mem_consumed=True` → 下一次成功 save() 后 `disk_consumed=True`
—— 即"写失败却记成已消费"的坏账，**修复前逐字复现，修复后消失**。
机制核验（源码 `strategy_funnel.py:1493-1503`）：`_save_locked()` 抛异常时
`if existing is None: self._records.pop(key, None) else: self._records[key] = existing` 后 `raise`，
由 `consume()` 的 `except Exception` 收敛为 `LEDGER_WRITE_FAILED`。

**② 孤儿是否真的落盘可审计** —— **是**。
A 组（收尾 `save()` 失败）后，用**新进程式**新建 `TicketLedger(同一路径)` 读盘：
`is_consumed('t-a')=True`、`orphan_consumed()` 返回 `['t-a']`。
即"已消费事实在盘上、审计库为权威、只补单不重发"这条路径成立。

**③ 首次执行器异常名是否进 note** —— **进，诊断不丢**。
A 组 `note_has_exc=True`，reasoning 逐字含 `账本收尾落盘未完成（OSError）` /
`（PermissionError）` / `（OperationalError）`；B 组含 `LEDGER_WRITE_FAILED:OSError`。
`TicketConsumption.as_dict()` 也回带 `"error": "<异常类名>"`。

### 1.3 反向检查（必须）

| 反向项 | 结果 |
|---|---|
| `KeyboardInterrupt` / `SystemExit` / `GeneratorExit` 必须穿出 | **全部穿出**（E 组 9 条：收尾 save / 账本 consume 写盘 / 执行器 consume 各 3 种）✅ 前后一致 |
| 不得把本应 fail-closed 拒绝的畸形输入放行 | C 组 `state=LEDGER_WRITE_FAILED` 且 `consumed=false`；B 组坏票 `t-bad_consumed=False`（**未**下单）；A 组只影响落盘、不影响 intents 语义 ✅ |
| 跨进程"只消费一次"是否被削弱 | 未被削弱（§6 抽样③ 8 进程 × 5 轮每轮恰好 1 个 `CONSUMED`）✅ |

### 1.4 扩测（作者没试的异常）

同探针对 **10 类 `Exception`** 跑 A/B/C/D 四路径：
`OSError` / `PermissionError` / `sqlite3.OperationalError` / `RuntimeError` / `ValueError` /
`KeyError` / `ZeroDivisionError` / `MemoryError` / `RecursionError` / 自定义 `QCustomError`。
**40/40 全部 `NO_ESCAPE` 且留痕**（每类都进 note / `claim.error` / `round_faults`）。
三组 `BaseException`（KI/SystemExit/GeneratorExit）**9/9 全部穿出**。
→ 作者"12 条"的声明是**下界**，实际收敛面是全 `Exception` 家族。

### 1.5 真实 OS 失败（不注入，`out/q1_n6_AFTER.txt` F 组）

| 场景 | BEFORE | AFTER |
|---|---|---|
| F1 目标账本文件被另一句柄占用（Windows `os.replace` 目标不可替换） | **ESCAPED `PermissionError: [WinError 5] 拒绝访问。: ...held.json.8916.7c013934.tmp -> ...held.json`**；`mem_consumed=True`（脏） | **NO_ESCAPE**，`claim.error='PermissionError'`，`mem_consumed=False` |
| F2 账本父路径是普通文件 | ESCAPED `FileExistsError: [WinError 183]` | **NO_ESCAPE**，`claim.error='FileExistsError'` |
| F3 非法文件名（`led<bad>.json`） | ESCAPED `OSError: [Errno 22] Invalid argument: '...led<bad>.json.lock'` | **NO_ESCAPE**，`claim.error='OSError'` |

F1 是**完全真实、零注入**的 `PermissionError`（正是 README 说的"Windows 上 `os.replace` 目标被占用"），
在 BEFORE 逐字穿出、在 AFTER 被收敛且内存干净 —— 这是本轮最强的证据。

**判定：N6 真闭合。** 声明的三类异常在四条路径上全部不再穿出；内存回滚、孤儿可审计、
诊断留痕三项均独立证实；`BaseException` 语义未被误伤；收敛面覆盖整个 `Exception` 家族。

---

## 2.（任务 2）N7 是否真修 —— **真修**

独立实测（`out/q1_n6_AFTER.txt` 末尾，跑真执行器 `funnel=None`）：

```
N7 未注入漏斗时 consumed_round_id = 'exec-2026-09-21T04:38:12.203741+00:00'
N7 startswith('exec-') = True
N7 startswith('exec:') = False
N7 decision = open intents = 1
```

文档侧核验：`9195782/README.md:132` 已是 `exec-`；`0c131f2/README.md:183` 该行已就地更正为
**`exec-<iso>`**（并注明"曾写作 `exec:<iso>`"）；顶部 SUPERSEDED 横幅第 9 行也点明更正。
全仓 grep `docs/` 无**残留笔误**（剩余 4 处命中全部是"更正记录"或探针输出里的
`startswith('exec:') = False`，属正常留痕）✅

---

## 3.（任务 3）`release()` 落盘失败仍返回 True —— **登记属实；不与必达②冲突；但引出 N10**

### 3.1 登记属实性 —— **属实**

源码 `strategy_funnel.py:1554-1562`：`release()` 在 `_save_locked()` 抛任何异常时 `return True`
（内存里已把该票置回 `consumed=False`、`release_count+1`）。
独立复现（`out/q1_n6_AFTER.txt` G 组）：

```
G[OSError] 首次 consume.state=CONSUMED 盘上已消费=True
G[OSError] release 落盘失败: escaped=None returned=True fired=True | mem_consumed=False
G[PermissionError] 同上
```

### 3.2 是否与必达②"不重复消费/不重复发射"冲突 —— **不冲突（fail-closed）**

方向分析：release 落盘失败 → 盘上仍是"已消费" → 下一轮 `consume()` 重读盘后
`_reload_locked()` 取**盘上事实**（`record.consumed and not mine.consumed → 采纳盘上`）
→ 返回 `ALREADY_CONSUMED` → **不会**重复下单。
残留方向是"票据被多占死一次"，**不是**"重复发射/重复消费"。故**不升级严重度**。

### 3.3 新发现 N10（P2 · fail-closed · 登记不完整）—— `release()` 的"内存已撤销"会在同轮被盘上事实覆盖

作者的注释/登记说"内存已撤销；**落盘留给下一次成功写入**；`release_count` 留痕已生效"。
**实测不符**（G 组 + D 组，两棵树均复现）：

```
G[OSError] release 落盘失败: returned=True | mem_consumed=False   ← 这一刻"内存已撤销"成立
G[OSError] 随后一次成功 save(): escaped=None | mem_consumed=True
           再读盘 disk_consumed=True                              ← 撤销被盘上事实覆盖回去，且已落盘
```

机理：同一轮收尾的 `ledger.save()` 会先 `_reload_locked()`，其合并规则
（`strategy_funnel.py:1330-1336`）是"盘上 consumed=True 而内存 consumed=False ⇒ **采纳盘上**"，
于是**撤销在写盘之前就被丢弃**，写下去的是 `consumed=True`。

真实执行器路径（D 组，消费成功→建意图失败→release 落盘炸）的后果：

```
D release->OSError: NO_ESCAPE | decision=no_trade intents=0
    mem_consumed=True release_count=0 | "释放失败字样在 reasoning=False"
    盘上 consumed=True
```

三点偏差：
1. **撤销被静默丢弃**：该 `ticket_id` 永远占死（直到过期/`prune`），正是 B-r3 N4 要消灭的伤害；
2. **`release_count=0`**：登记所称"留痕已生效"为**否**；
3. **执行器侧"票据可能仍被占死"的 note 是死代码**：`release()` 吞掉异常后返回 `True`、
   不抛异常，所以 `strategy_trader.py:331-336` 那个 `except Exception → notes.append("账本释放未完成…")`
   分支**永远不会触发**，作者在 `03` diff 注释里承诺的"必须如实说出来"实际没有发生。

**严重度判定**：fail-closed（占死 ≠ 重复下单）、需"撤销落盘失败 + 同轮另有成功落盘"两个条件，
且 §七 已登记该口径返回 `True`（但**机制描述错误**：不是"留给下一次成功写入"，而是"被下一次成功写入丢弃"）。
按标尺它不属于"fail-open / 崩溃 / 异常穿出 / 写失败却记成已消费"四类自动阻塞项，
故**不判阻塞**；但登记文字须更正（"短暂"→"本轮内即被覆盖丢弃"），
并建议把执行器侧那个不可达分支改成"release 返回 True 但盘上仍为已消费"时的真实提示（后续小修）。

---

## 4.（任务 4）补丁可应用性 —— **可干净应用到当前集成 HEAD**

（详见 §6）

---

## 5.（任务 5）门禁 —— **全绿**

（详见 §7）

---

## 6.（任务 6）补丁 + 全量 delta

*（本节在 delta 跑完后填写）*

---

## 7.（任务 7）门禁 —— 全绿

沙箱 = `tree_head`（pristine 集成树 + r4 独占产物 + 5 补丁），`out/phase5_gates.txt`：

```
### check_quality.py
All checks passed!
PROFILE LOCK OK sha256=333d6a6b... locked_fields=30 symbols=20 quote=USDT
DEPENDENCY LOCK OK packages=50 sha256=a28db842... python=3.13 platform=win32
SECRET SCAN OK files=525 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
QUALITY CHECK OK files=290 untracked=0
rc=0

### scan_tracked_secrets.py
SECRET SCAN OK files=525 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
rc=0
```

作者称 `files=290` / secret `525` → **两个数都一致** ✅
（`untracked=0` 而作者写 3：我的沙箱把独占产物 `git add` 进了初始提交，口径差异，非问题。）

---

## 8.（任务 8）回归抽样 ≥5 —— 7/7 通过

| 抽样项 | 结果 | 原始输出 |
|---|---|---|
| ① **接线 grep** | pristine `src/` **0 命中**；head **恰好 7 处**：`strategy_trader.py:22`、`signal_ticket.py:9`、`app.py:23/1089/1138/1139`、`scanner.py:24` | `out/q5_extras.txt` |
| ② **真实路径 `last_scan.funnel` / `no_open_verdict`** | 真跑 `scan()`：C1 卡册损坏 → `round_faults=['CARDS_INVALID','NO_TICKETS_AVAILABLE']`、`no_open_verdict='blocked'`；C5 缺卡册 → `['CARDS_UNAVAILABLE']`/`blocked`；C6 空卡册 → `no_candidates`/`NO_EDGE` | `out/q2_n1_AFTER.txt` |
| ③ **跨进程消费 8×5** | `PASS 8x5 每轮恰好 1 个 CONSUMED rounds=[]` | `out/q4_sampling.txt` §3 |
| ④ **故障 vs no_edge** | 自枚举 **31 个 `NATURE_FAULT` 码，无一被读成 `no_edge`（反例=[]）**；5 个 `NATURE_EDGE`=NO_EDGE 码仍 `no_edge`；轮级 `NO_TICKETS_AVAILABLE` → `no_candidates`（登记留白 N5，靠 `verdict` 区分）；混合仍 `no_edge`；纯范围码 → `no_candidates`/`READINESS_BLOCKED`；`LEDGER_SAVE_DEFERRED` 登记为轮级 `fault` | `out/q4_sampling.txt` §1 |
| ⑤ **真实路径 `ticket_skip_detail()`** | 倒挂→`ENTRY_ZONE_INVERTED`、非法周期→`TICKET_SHAPE_INVALID`、过期→`TICKET_EXPIRED`，均落 `detail_counts` | `out/q4_sampling.txt` §2 |
| ⑥ **管理优先预算** | 预留池不变量极端参数恒成立；预算 0→`RESEARCH_BUDGET_ZERO` 可见；研究池被吃光→`MANAGEMENT_PRIORITY_BUDGET`；`meets_management_demand()` 是**真判定**（demand=11 > 全池 10 → `shortfall=1` → `False`；demand=5 → `True`） | `out/q4_sampling.txt` §4 + `out/q5_extras.txt` |
| ⑦ **`position_class` 同源 / `CARDS_INVALID` 可达** | `sf.PositionClass is spm.PositionClass` → **True**；`POSITION_CLASS_*` 取自枚举 `.value`；`CARDS_INVALID` 在**主路径**（不预置 `_registry`）可达，且真漂移仍记 `STRATEGY_LAB_DRIFT` | `out/q4_sampling.txt` §4、`out/q2_n1_AFTER.txt` C1/C3 |

> **探针自身的一处假失败（已澄清，非产品缺陷）**：复用旧探针时 §4 有一条
> `meets_management_demand 是真判定（over-claim -> False）… shortfall=0` 报 FAIL。
> 查因：该断言用的是**修正前的旧构造**（`total=10, reserve=2, claim_management(5)`）。
> 按实现 `management_available = max(0, total - management_used - research_used) = 10`，
> `granted=min(5,10)=5` ⇒ `shortfall=0` ⇒ 谓词**正确地**返回 `True`（它问的是"管理侧有没有拿够"）。
> 我用**新构造**独立复验（`out/q5_extras.txt`）：`demand=5/全池10 → True`；
> `demand=11/全池10 → shortfall=1 → False`；研究先拿满 6 后管理仍拿满预留 4 → `True`。
> **是探针的旧断言错，不是产品错**（与 r4 报告里已记的同类修正一致）。

---

## 9. 新发现汇总

| # | 级别 | 一句话 | 位置 | 阻塞? |
|---|---|---|---|---|
| **N10** | **P2（fail-closed · 登记不完整）** | `release()` 落盘失败返回 `True`，但**同一轮收尾 `save()` 的 `_reload_locked()` 会用盘上"已消费"覆盖内存撤销**，撤销被静默丢弃（`release_count` 回 0、盘上仍 `consumed=True`、票据占死）；执行器侧"票据可能仍被占死"的 note 因此**不可达**。README §七 的机制描述（"落盘留给下一次成功写入"）与代码行为**不符** | `src/core/strategy_funnel.py:1554-1562`（release）与 `:1330-1336`（`_reload_locked` 合并规则）；`src/agents/strategy_trader.py:331-336`（不可达分支） | **否**（fail-closed，不与必达②冲突；但登记文字须更正） |
| N11 | P3（观察 · 测法） | 我的首个沙箱用机器全局 `core.autocrlf=true`，`git apply` 把目标文件整体转成 CRLF，`git diff --stat` 得 681/36；改用 `-c core.autocrlf=false` 重建后得 **706/61**，与 README §六 逐位相同 ⇒ **README 的 diffstat 无误**，先前的差异是我方环境所致 | — | 否 |
| N13 | P3（文档计数） | README §六 末尾写"`03` 里有 **3 行**只差行尾 CR 的 -/+ 对"。实测：`git diff --numstat` 带/不带 `--ignore-cr-at-eol` 之差 = **25 对**（226/39 vs 201/14，全部在 `strategy_trader.py`）；用 difflib 逐行对比得 **4 块 / 6 行**。三个口径都不是 3。**结论（纯行尾噪声、语义未变、应用后 blob 与作者编辑态逐字节相同）我独立证实成立**，只是计数写错 | `9195782/README.md` §六 末段 | 否 |
| N12 | P3（观察 · 既有） | 执行器里 `db.signal_ticket_has_order()` / `db.log_risk_event()` / `db.update_signal_ticket()`（审计库 SQLite 调用）**仍无护栏**，若审计库抛 `sqlite3.OperationalError` 会穿出整个执行器 —— 与 N6 **同形**。**实测两树同形**（`out/q7_db.txt` head：`escaped=OperationalError: database is locked, decision=None`；`out/q7b_db.txt` pristine：逐字相同）⇒ **既有形态，U08 未引入、未扩大**，且不在本轮 N6 声明范围（`save/consume/release`） | `src/agents/strategy_trader.py` 各 `db.*` 调用点 | 否（建议登记为后续项） |

**没有发现**：故障被读成 `no_edge`（31 码 + 混合例）、跨进程重复消费、
KI/SystemExit 被吞、写失败后留下脏"已消费"、未登记的既有脚本新增失败、补丁应用失败。

---

## 10. 命令清单（可重跑）

```bash
W="C:/Users/Tao/Desktop/币安/mat-wt/reviews/U08/R5/r1"; S="$W/_scratch_Q"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
I="C:/Users/Tao/Desktop/币安/mat-wt/I"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 MTA_NO_PROXY=1

bash "$S/build.sh" > "$S/out/build.txt" 2>&1          # 建 tree_pristine / tree_r3 / tree_head

# N6/N7 前后对照（自建探针；边界注入 = 只换 os.replace 这个系统调用）
"$PY" "$S/probes/q1_n6.py" "$S/tree_r3"   BEFORE > "$S/out/q1_n6_BEFORE.txt" 2>&1
"$PY" "$S/probes/q1_n6.py" "$S/tree_head" AFTER  > "$S/out/q1_n6_AFTER.txt"  2>&1

# 回归抽样
"$PY" "$S/probes/q2_n1.py"        "$S/tree_head"    > "$S/out/q2_n1_AFTER.txt" 2>&1
"$PY" "$S/probes/q4_sampling.py"  "$S/tree_head"    > "$S/out/q4_sampling.txt" 2>&1
"$PY" "$S/probes/q5_extras.py"    "$S/tree_head"    > "$S/out/q5_extras.txt"   2>&1

# 补丁矩阵（仓库根 = I，只读；3 种配置 × 5 补丁，全部带 -v）
git -C "$I" apply --check -v "$W"/docs/dispatch/patches/U08/9195782/0*.diff
git -C "$I" -c core.autocrlf=false -c core.eol=lf apply --check -v "$W"/docs/dispatch/patches/U08/9195782/0*.diff
git -C "$I" -c core.autocrlf=true  apply --check -v "$W"/docs/dispatch/patches/U08/9195782/0*.diff

# 门禁（打过补丁的沙箱）
( cd "$S/tree_head" && "$PY" scripts/check_quality.py && "$PY" scripts/scan_tracked_secrets.py )

# 全量 delta（约 13 分钟）
"$PY" "$S/probes/q6_delta.py" > "$S/out/q6_delta_console.txt" 2>&1
```

| 产物 | 内容 |
|---|---|
| `out/build.txt` | 三树构造 + 5 补丁应用逐条 `Applied ... cleanly` + diffstat |
| `out/q1_n6_{BEFORE,AFTER}.txt` | N6 四条路径 × 13 类异常 + 真实 OS 失败 + N7，逐行原始输出 |
| `out/q2_n1_AFTER.txt` | 卡册损坏/漂移/缺失 + `last_scan.funnel` + `no_open_verdict` |
| `out/q4_sampling.txt` | 故障 vs no_edge、`ticket_skip_detail` 真实路径、跨进程 8×5、预算、归属 |
| `out/q5_extras.txt` | 预算谓词真约束力（自建）+ `position_class` 同源 + 接线 grep |
| `out/phase4_applycheck.txt` | 3 配置 × 5 补丁，`Checking patch` 15 条、`Skipped` 0 条 |
| `out/phase5_gates.txt` | 门禁原文 + `b40a5fa→3a13c7b` 只改 docs 的证明 |
| `out/q6_delta_console.txt` / `delta_result.json` / `delta_{base,head}.log` / `delta_harness_{base,head}.txt` | 全量 delta |
| `out/q7_db.txt` / `out/q7b_db.txt` | N12：审计库异常在两树的对照 |
| `out/phase4_applycheck.txt` | 3 配置 × 5 补丁；`Checking patch` 15 条、`Skipped` 0 条、`I` 的 `git status` 空 |

---

## 11. 未覆盖 / 无法离线验证

- **真磁盘满（ENOSPC）/ 只读挂载的端到端演练未做**：我用 `os.replace` 边界注入模拟 ENOSPC，
  并用**真实**的文件被占用 / 父路径是文件 / 非法文件名触发真实 `PermissionError`/`OSError`；
  但"真的把盘写满"无法在不影响主机的前提下构造。
- **`OSError`/`PermissionError` 的线上触发频率未测**：只证明"路径存在且已被局部化"。
- **`release()` 撤销丢失（N10）在生产上的可观测频率未测**：需要"撤销落盘失败 + 同轮另有成功落盘"
  同时发生，我只能确定性构造。
- **生产端到端一轮**：`graph.py` 不注入 `funnel`/`ledger`（U12 边界），
  "真实一轮 17 道闸门的命中分布""跨进程去重在生产生效"仍无从观测 —— 同 r3，如实归 U12。
- **`/funnel` 真实浏览器渲染**：未做视觉/交互核对（仅 TestClient 级）。
- **账本与审计库勾稽的权威性**（`signal_ticket_has_order` 能否兜住"已消费未落库"窗口）：需 DB 数据，超只读范围。
- **真实集成合入**：我在自己的沙箱做（`tree_head`），未也不允许改动 `mat-wt/I`；
  5 个共享文件 blob 已逐文件核对与集成 HEAD 相同，但真实合入仍应由 I 自己跑一次 harness。
- 全程**未联网**；探针里 `get_live_prices` 一律以显式 `prices=` 短路。

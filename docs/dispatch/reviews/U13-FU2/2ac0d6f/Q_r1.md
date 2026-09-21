# Q 独立验收报告 — U13-FU2 (R1)

- 会话: Q (独立验收, 推翻式)
- 工作树: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU2\R1\q` (detached HEAD @ 2ac0d6f)
- pristine 基线: `7885e6a`
- patched: `7885e6a` + 2ac0d6f 分支内文件 + 补丁 01–03
- 状态: **FINAL**（全部 8 项验证已完成；无未跑项）

## 0. 结论：**CHANGES_REQUESTED**（1 个阻塞项；N1/N3 通过，N2 的"无行为差异"自述被推翻）

| 项 | 裁决 |
|---|---|
| **N1** 裸形态 fail-open | **PASS**（自有探针独立复现前后翻转 8/3→11/0；方向正确，未误伤 `GATE_NOT_INSTALLED` 与干净账本） |
| **N2** service 接线 | **CHANGES_REQUESTED**：接线**形态**正确（spy 两态对照确认），但作者"N2 无可达行为翻转"的自述**不成立**；新接线在**放松**方向有可达行为差异 |
| **N3** demo 口径 | **PASS**（`assert_environment_bound('demo')` 实测抛 `STATE_ROOT_MISSING`；README/JSON 更正与代码一致） |
| **N4** 补丁 05 | **确认不必要**（base 已 ruff-clean；05 是一条新的 ruff exclude 且完全失效，不应采用） |
| 补丁矩阵 / 等价性 / check_quality | **PASS**（9/9 全绿；三文件与 head 逐字节等价；两棵树 check_quality 绿、无新增 exclude） |
| 回归 full delta | 见 §6 |
| `test_supervision.py` | **不算放宽断言**（+21/−0，断言零改动；新夹具是前提修正） |

### 阻塞项 B-1：N2 的接线把启动/set_control 的门禁判据**放松**了

- **最小复现**（`_scratch_Q/probe_n2.py`，case `unknown_exit`；两棵树同一份合成状态根）：
  1. 用真实 `AuditDatabase` 建账本 → 真实 `require_reconcile`/`complete_reconcile`
     把门禁推到 `CLEARED`；
  2. 之后往 `order_outbox` 插一颗 `purpose='EXIT'`、`status='UNKNOWN'` 的单并入主文件；
  3. 构造 `TradingService`。
- **结果**：

  | | pristine `7885e6a` | patched `2ac0d6f` |
  |---|---|---|
  | 启动期 `before_entry` | 阻断 `RECONCILE_REQUIRED_BEFORE_ENTRY` | **放行 `GATE_CLEARED`** |
  | `control.state` | `close_only` | **`normal`** |
  | `set_control("normal")` | `ok=False` 被拒 | **`ok=True` 成功** |

- **根因**：`src/core/service.py:891` `_recovery_gate_ledger_facts(facts=None)` 的
  `unknown_orders`/`submitting_orders` **直接取自 `facts` 形参，缺省 0**；两条新接的
  路径都是**无参**调用（`self._recovery_gate_ledger_facts()`）→ 这两列**恒为 0**，
  等于绕过门禁对这两列的账本读取。门禁自己的 `ledger_reconcile_facts`
  对这两列是**不分 purpose 全表计数**，且 `_facts_block_unresolved` 把任一列非零
  视为阻断。于是"账本里唯一未收口的是非 ENTRY 单"时，**裸形态阻断、注入形态放行**。
- **为什么是阻塞**：U13/F14 的整改点正是"恢复门禁未收口时不允许把控制面拨回
  `normal`，否则可以被控制台一步绕过"；本补丁在该场景下让 `set_control('normal')`
  **重新成功**，即对既有 P1 整改的**回退**（限非 ENTRY 未收口单）。同时启动期不再
  强制 `close_only`，丢掉了运维可见的失败信号。（注：**进场链**在 live 下仍会拦
  ——`_collect_entry_readiness_facts` 用 `Broker.order_outbox.status()` 全 purpose
  计数——所以阻断力只在控制面与启动期被削弱。）
- **建议最小修法**：两条新路径改为把**账本真实的两列**传进去（例如
  `self._recovery_gate_ledger_facts(self.db.outbox_counts())` 之类，或让
  `_recovery_gate_ledger_facts` 在 `facts` 缺该键时**自行查账本**而不是默认 0），
  并让验证器补一条**行为断言**（当前 4 条 N2 check 全部只是
  `bare == 0` 的接线形态 spy，结构上**不可能**发现本项）。
- **附带**：作者 §2「如实说明」与 §7.1「没有构造出行为翻转」的结论需要更正；
  §4.2「注入形态更精确」的论证依赖"账本可证干净"，而无参调用时它并未**证明**，
  只是默认 0。

## 0.1 复现清单（全部在 `_scratch_Q/`，均只写系统临时目录）

| 文件 | 用途 | 结果 |
|---|---|---|
| `probe_n1.py` | N1 10 场景 | pristine 8/3 exit1 → patched 11/0 exit0 |
| `probe_n2.py` | N2 接线 spy + A/B/C 三场景 | pristine 7/3 exit1（失败=接线×2+C1）→ patched 7/3 exit1（失败=**C1/C2/C3 全部三条**）——失败集合不同即行为翻转 |
| `probe_n3.py` | N3 doc vs code | pristine 4/2 exit1 → patched 6/0 exit0 |
| `probe_bypass.py` | 9 组绕过 | pristine 11/2 → patched 13/0 |
| `run_reg_tsv.py` | 逐脚本官方回归 | 见 §6 |
| 日志 | `_scratch_Q/logs/*.txt`、`logs/reg_*.tsv` | — |

## 1. N1 前后翻转 (自建探针) — 已实测

自建 `_scratch_Q/probe_n1.py`（不抄作者探针）。两棵树 `git archive` 导出后**立刻**
`git init && git add -A -f` 提交（pristine=`3dd60ee` 内容=7885e6a；patched=`b1bead3`
内容=2ac0d6f）。探针手写**自洽摘要**的 `CLEARED` 门禁（复验 Q1/Q2 绕过形态），
唯一变量 = 状态根里的账本形态。

```
python probe_n1.py <tree>
```

| # | 场景 | pristine | patched |
|---|---|---|---|
| S1 | 状态根**没有账本文件** | **allowed=True / GATE_CLEARED  fail-open** | allowed=False / EVIDENCE_UNVERIFIED |
| S2 | 账本在、**无 `order_outbox` 表** | **allowed=True / GATE_CLEARED  fail-open** | allowed=False / EVIDENCE_UNVERIFIED |
| S3 | 有未收口 `SUBMITTING/ENTRY` | blocked / RECONCILE_REQUIRED_BEFORE_ENTRY | 同左（不回归） |
| S4 | 账本干净（outbox 空） | allowed / GATE_CLEARED | allowed / GATE_CLEARED（**不误伤**） |
| S5 | 注入干净 facts | allowed | allowed |
| S6 | 注入脏 facts | blocked | blocked |
| S7 | 注入 `source="not_applicable"` | **allowed=True  fail-open** | allowed=False / EVIDENCE_UNVERIFIED |
| S8 | 门禁从未安装（无文件无戳） | allowed / GATE_NOT_INSTALLED | allowed / GATE_NOT_INSTALLED（**不误伤**） |
| S9 | 删门禁文件、留安装戳 | blocked / GATE_MISSING | blocked / GATE_MISSING |
| S10 | 门禁+安装戳同删 | allowed / GATE_NOT_INSTALLED（已登记口子） | 同左 |

```
pristine: PROBE N1: 8 通过 / 3 失败   exit=1   （S1/S2/S7 即 fail-open 三条）
patched : PROBE N1: 11 通过 / 0 失败  exit=0
```

**方向核查（重点）**：作者称"未恢复过的部署在 `GATE_NOT_INSTALLED` 前置分支就放行"——
**实测成立**（S8：`before_entry` 在 `self.state == GATE_NOT_INSTALLED` 处 return
`allowed=True`，早于任何账本读取）。恢复过且账本干净的部署也不误伤（S4）。
`assert S4/S8 在 patched 上仍放行` → 没有把正常启动一并挡死。**N1 通过。**

## 2. N2 独立观测 + 价值判定 — 已实测，**发现可达行为翻转（与作者相反）**

自建 `_scratch_Q/probe_n2.py`（独立 spy + 自建合成状态根；真实 `AuditDatabase`
建账本、真实 `RecoveryGate.require_reconcile/complete_reconcile` 到 CLEARED，
真实 `TradingService` 构造；全程 `EXECUTION_MODE=paper`、隔离 `project_root`、
不联网/不下单/不读真实 `.env`/`data`）。

### 2a. 接线形态（spy）— 作者说法属实

| 观察 | pristine | patched |
|---|---|---|
| 启动期 `before_entry` 调用 | 2 次，**其中 1 次裸** | 2 次，**0 次裸**，注入
`{'unknown_orders':0,'submitting_orders':0,'unresolved_entry_orders':1}` |
| `set_control("normal")` 期调用 | 1 次，**裸**（facts=None） | 1 次，注入 |
| 服务账本 == 门禁账本 | True | True |

`service.py` 两条路径（`:319`、`:1426`）在 patched 上确实注入 facts、pristine 上
确实裸 —— 与作者一致。

### 2b. 价值判定：**作者"无可达行为差异"的结论不成立**

作者只测了"未收口 **ENTRY** 单落在 `-wal`"一种场景（我复现：两态均阻断，
`[A]` 通过），并据此在 §7.1 断言"没有构造出行为翻转"。**我构造出来了**：

> 门禁 CLEARED 之后，账本里出现一颗**非 ENTRY**（`purpose='EXIT'`）的
> `UNKNOWN` 单（已 checkpoint 落在主文件）。此时"门禁自己读账本"与
> "服务注入的账本事实"结论**相反**：

| | 门禁自己的 `ledger_reconcile_facts` | 服务注入的 `_recovery_gate_ledger_facts()` |
|---|---|---|
| `unknown_orders` | `status='UNKNOWN'` 计数，**不分 purpose** → **1** | 取自 `facts`，无参调用时 **硬编码 0** |
| `submitting_orders` | 同上 → 0 | **硬编码 0** |
| `unresolved_entry_orders` | `status IN (...) AND purpose='ENTRY'` → 0 | 同口径 → 0 |
| `_facts_block_unresolved` | **True → 阻断** | False → 放行 |

实测（我自己的探针 `[C]`，两棵树同一份合成账本）：

| | pristine（裸） | patched（注入） |
|---|---|---|
| 启动期 `before_entry` | **阻断 `RECONCILE_REQUIRED_BEFORE_ENTRY`** | **放行 `GATE_CLEARED`** |
| 启动后 `control.state` | **`close_only`** | **`normal`** |
| `set_control("normal")` | **`ok=False` 被拒** | **`ok=True` 成功** |

```
pristine [C]: gate_allowed=True(readiness) control=close_only set_control={'ok': False, 'error': 'recovery gate blocked: RECONCILE_REQUIRED_BEFORE_ENTRY...'}
patched  [C]: gate_allowed=True control=normal     set_control={'ok': True, 'state': 'normal', ...}
```

**根因**（结构性，不是边角）：`src/core/service.py:891` 的
`_recovery_gate_ledger_facts(facts=None)` 把 `unknown_orders`/`submitting_orders`
**直接取自调用方传入的 `facts`，缺省 0**；只有 `unresolved_entry_orders` 真的查账本。
而新接的两条路径 `self._recovery_gate_ledger_facts()` **不传 `facts`** →
这两列**恒为 0**，等于把门禁对这**两列**的账本读取整个绕过。门禁自己的
`ledger_reconcile_facts` 对这两列是**不分 purpose 全表计数**（上游契约），
所以只要账本里唯一未收口的单是 `EXIT/PROTECTIVE` 之类非 ENTRY 单，
**裸形态阻断、注入形态放行**。方向是**放松**，与本任务"启动路径不得退回裸
`before_entry()`"的加固意图相反。（同类但不新增的第三处：冷启动就绪快照
`:411` 与 paper 就绪链 `:986` 同样无参调用；live 就绪链因
`Broker.order_outbox.status()` 全 purpose 计数而对齐门禁口径。）

### 2c. 作者验证器为什么没发现（结构性问题）

独立跑扩展后的验证器（`scripts/ops/verify_u13f1_recovery_gate.py`）：

```
扩展验证器 on pristine recovery.py : 45 通过 / 7 失败  exit=1   （与作者口径一致）
扩展验证器 on patched  recovery.py : 52 通过 / 0 失败  exit=0   （与作者口径一致）
7 条失败 = N1×3 ([none]/[no_outbox]/[injected:not_applicable])
          + N2×4 ([dirty_wal]/[clean] × 启动/set_control)
```

但 N2 的 4 条 check **全部只是 `bare == 0` 的接线形态 spy**，从不读
`control.state` / `set_control()` 返回值；且它用的 `[dirty_wal]` 场景恰是
**两态结论相同**的情形（未收口 ENTRY 在 `-wal` 里）。因此该验证器
**在结构上不可能**发现 §2b 的放松面。这也是为什么"验证器 52/0 全绿"不能作为
N2 无回归的证据。

**裁决**：N2 不是"无可达行为差异的防御性/一致性改进"。它是**可达的、方向为放松的
行为变更**。作者 §2/§7.1 的自述**不成立**。建议列 **CHANGES_REQUESTED**（细则见结论）。

## 3. 绕过尝试 — 已实测（`_scratch_Q/probe_bypass.py`）

| # | 尝试 | pristine | patched | 说明 |
|---|---|---|---|---|
| B1 | 空证据 `evidence={}` | blocked EVIDENCE_UNVERIFIED | 同左 | — |
| B2 | **自洽摘要 + 无账本** | **allowed=True 绕过成功** | blocked EVIDENCE_UNVERIFIED | N1 |
| B3 | 自算摘要、账本脏 | blocked RECONCILE_REQUIRED_BEFORE_ENTRY | 同左 | — |
| B4 | 先 CLEARED 再塞未收口单 | 重新阻断 | 同左 | — |
| B5 | 只删门禁文件（留安装戳） | blocked GATE_MISSING | 同左 | — |
| B6 | 只删安装戳（留门禁文件） | allowed（**非绕过**：戳只在文件缺失时才用） | 同左 | — |
| B7 | 门禁 + 安装戳**同时删**，账本脏 | **allowed=True** | **allowed=True** | 已登记口子，未收紧 |
| B8 | `complete_reconcile` 在账本**不可读**时自声明 CLEARED | **allowed=True 且 `before_entry` 也放行** | `complete_reconcile` 仍 `allowed=True`，但 `before_entry` **blocked EVIDENCE_UNVERIFIED** | 见下 |
| B9 | 账本里唯一未收口单是**非 ENTRY**（`UNKNOWN/EXIT`） | 裸形态阻断 / 注入形态放行 | 同左 | N2 的放松面在门禁层的表现 |

```
pristine: PROBE BYPASS: 11 通过 / 2 失败 (B2/B8)
patched : PROBE BYPASS: 13 通过 / 0 失败
```

**B8 逐条核对作者自述**（"该 CLEARED 不产生放行能力，只做到状态位被写脏"）：

- ✅ 自声明 CLEARED 后 `before_entry` **确实不能放行**（patched：`EVIDENCE_UNVERIFIED`）。
  pristine 上这条是**真放行**（fail-open）→ 作者这句话在 patched 上**成立**。
- ✅ 状态位**确实被写脏**：落盘 `state="CLEARED"`，
  `evidence.facts.source == "evidence"`（`unresolved_entry_orders: 0`）。
- ⚠️ 但"不可读时自声明"这个口子本身**没收紧**：`complete_reconcile` 仍返回
  `allowed=True`（作者已如实登记）。补注：若调用方在注入形态下递自算 zeros，
  同一份脏状态仍放行（`_facts_unreadable` 只认 `None`/`not_applicable`，
  不认"注入值未经账本证明"）——这是 doc 明示的**调用方信任边界**，
  `TradingService` 走 `_recovery_gate_ledger_facts` 时读失败即返回
  `unresolved=1`，是 fail-closed，故生产路径不受此影响。

## 4. `tests/acceptance/test_supervision.py` 改动是否放宽断言 — 逐行裁决

`diff -u pristine patched` 全文 = **+21 行，0 删 0 改**：

1. 新增 `_make_gate_ledger(state_root)`（+20 行，含 docstring）：在门禁所在状态根
   造一份只有空 `order_outbox` 表的 `agent_trader.sqlite`。
2. `check_reconcile_before_entry()`（`[7]`）加一行 `_make_gate_ledger(Path(tmp))`。
3. `check_gate_fail_closed()`（`[7b]`）加一行 `_make_gate_ledger(Path(tmp))`。

**断言文本逐条核对：0 处改动，不存在任何从"应阻断"改成"应放行"的断言。**
`[7]` 里所有 check（拒绝缺字段/类型混淆/未完成/过期/未来/时钟不可信；解除后允许）
与 `[7b]` 的 (a)–(f) 全部逐字未动。

**是否削弱？**做了一组对照：把 **patched 的 recovery.py** 与 **pristine 的
`test_supervision.py`** 组合跑 —— `exit=1`，恰好 2 条失败：

```
U13 SUPERVISION ACCEPTANCE FAILED: 2 -> 解除后允许开仓, 损坏门禁经结构化对账可显式收口
```

两条都是"**合法对账后应当放行**"的断言，在**合成夹具（状态根无账本）**下因
N1 的一律阻断而失败。补上账本夹具后即恢复通过（patched 全套 `166 checks` exit 0）。
⇒ 这两条断言**原本是靠 N1 的 fail-open 才通过的**，即旧夹具恰好覆盖了缺陷形态；
新夹具把前提修成生产形态（门禁所在状态根总是有账本），**断言强度未降**
（现在走的是 `source="ledger"` 的正当放行路径，而不是 fail-open）。

**裁决：不算放宽断言。**唯一代价：夹具账本用的是**桩表**
（`status/purpose/environment` 三列，非迁移真实 schema），够用但脆弱；
且 `[7c] check_gate_runtime_binding` 未加该夹具（它只碰 `complete_reconcile`，
不需要），两处不一致但无害。

## 5. N3 — 已实测（`_scratch_Q/probe_n3.py`）

| 观察 | pristine | patched |
|---|---|---|
| `RuntimePaths.validate('demo')`（未声明状态根） | `('STATE_ROOT_MISSING',)` | 同左（**代码未改**） |
| `assert_environment_bound('demo')` | **抛** `RuntimePathsError: ... reasons=STATE_ROOT_MISSING` | 同左 |
| U13-FU1 补丁包 README 登记"会抛" | **否（FAIL）** | **是（PASS）** |
| 任务清单 U13-FU1 tradeoff 更正 | **否（FAIL）** | **是（PASS）** |
| 生产调用点 | 全树仅 `src/core/runtime_paths.py` 自身（`TradingService`/`run_web.py` 零调用） | 同左 |

```
pristine: PROBE N3: 4 通过 / 2 失败   exit=1
patched : PROBE N3: 6 通过 / 0 失败   exit=0
```

**N3 属实**：该 API 对 demo 确实会抛；README/JSON 的更正与代码一致，且更正文案
明确了"默认部署仍能启动 = 该 API 零调用点"，而不是"该 API 不拦"。**N3 通过。**

## 6. 回归 full delta — 已实测

方法：两棵**解包后立刻 `git init && git add -A -f` 提交**的沙箱
（`trees/pristine`=`3dd60ee`，`trees/patched`=`b1bead3`），用
`_scratch_Q/run_reg_tsv.py` **逐脚本**跑官方清单（清单从各树自己的
`scripts/run_offline_regression.py` 的 `SCRIPTS` AST 读入，不另抄），环境与官方
入口同口径（`EXECUTION_MODE=paper`、清 `MTA_PROFILE`/`MTA_STATE_ROOT`/
`MTA_DATA_ROOT`、清凭据变量、`PYTHONUTF8=1`）。

```
pristine: TOTAL 85 scripts, nonzero=0, wall=310s
patched : TOTAL 85 scripts, nonzero=0, wall=325s
```

| 项 | 结果 |
|---|---|
| 脚本集合 | 85 / 85，**两棵树同一集合**（`set==set` True） |
| pristine 非零 | **0 / 85** |
| patched 非零 | **0 / 85** |
| **NEW_FAIL**（0→≠0） | **∅** |
| **FIXED**（≠0→0） | **∅** |
| BOTH_NONZERO | **∅** |

`raw` 见 `_scratch_Q/logs/reg_pristine.tsv` / `reg_patched.tsv`。

### 6.1 官方入口（真实工作树）

在 `reviews/U13-FU2/R1/q`（detached HEAD `2ac0d6f`、无 `.diff` 应用、无本地改动）
直接跑官方入口：

```
python scripts/run_offline_regression.py        # exit 0
...
OFFLINE REGRESSION OK scripts=85
```

跑完后 `git status --short` 仍只有 `?? _scratch_Q/`，**未污染工作树**。
（完整日志：`_scratch_Q/logs/official_worktree.log`。）

（作者口径为"逐脚本 full delta 85/85、NEW_FAIL=∅"——**独立复现一致**。）

## 7. 补丁矩阵 + check_quality — 已完成

**补丁矩阵**（3 补丁 × 3 种 git 配置，均在 **pristine `7885e6a`** 树上
`git apply --check --verbose`）：

```
cfg=[<default>] 01 service rc=0 Checking=1 Skipped=0
cfg=[<default>] 02 acceptance rc=0 Checking=1 Skipped=0
cfg=[<default>] 03 n3 docs rc=0 Checking=2 Skipped=0
cfg=[core.autocrlf=false core.eol=lf] 01/02/03 rc=0 Checking=1/1/2 Skipped=0
cfg=[core.autocrlf=true]               01/02/03 rc=0 Checking=1/1/2 Skipped=0
```

9/9 全绿，0 Skipped。**注**：本机 `core.autocrlf=true` 是 `--system` 级设置
（`git config --global` 未设），所以"默认"档实际也走 CRLF 语义；三档结果一致。

**补丁等价性**：pristine + 补丁 01–03 得到的 `src/core/service.py`、
`tests/acceptance/test_supervision.py`、`docs/.../bc112fe/README.md` 与
`git archive 2ac0d6f` 的对应文件**逐字节一致**（`diff --strip-trailing-cr`；
唯一差异是 `git apply` 在 `core.autocrlf=true` 下写出的 CR，属本机配置产物，
非补丁缺陷）。即"分支自洽"与"集成树单独 apply"两条路等价，作者说法成立。

**check_quality**：`scripts/check_quality.py` 在 pristine 与 patched 两棵树
**逐字节相同**（未新增任何 exclude / per-file-ignores）：

```
SECRET SCAN OK files=579 (pristine) / 602 (patched)
QUALITY CHECK OK files=295 untracked=0      exit 0   (两棵树都绿)
```

**补丁 05 确认不必要（N4）**：base `7885e6a` 上 `python -m ruff check .`
= `All checks passed!`，`check_quality` exit 0。把归档的演练探针
`.py.txt` 改回 `.py` 后 ruff 立刻报 **30 个错**（全部落在
`docs/dispatch/reviews/U12/drill_F14_F18/raw/` 与 U13-FU2 的 `evidence/`），
即 base 红灯的真正来源就是这些**归档证据**，已被"改名 `.py.txt`"修好。
补丁 `05_optional_docs_probe_lint.diff` 想加的是
`docs/**/*.py = ["E401","E402","E702","F401"]` —— 一条**新的 ruff exclude**，
且因 `docs/**/*.py` 现在只剩 17 个 ruff-clean 的 U02 证据文件而**完全失效**
（实测：apply 05 后 `check_quality` 结果不变）。**补丁 05 不必要，且不应采用。**

## 8. 未能验证 / 未覆盖的部分（如实登记）

1. **未在真实交易所侧验证**：全部证据来自进程内假 broker 与系统临时目录里的合成
   状态根；没有真实网络、真实凭据、真实下单/撤单、真实告警。N2 的"放松面"是在
   合成的 `order_outbox` 行上演示的，未在真实生产账本上观测到同类行（真实账本
   只读约束下不可读）。
2. **未做真实断电撕裂写 / 真实多账户并发**：`-wal` 无 `-shm` 的"停写快照"形态是
   手工拷文件造出来的（与作者同法），不是真实断电产物。
3. **未验证 demo/testnet 档下 broker 的 `order_outbox` 归属**：只确认 `PaperBroker`
   无 `order_outbox`；`demo` 档走哪个 broker、其 `order_outbox.status()` 是否跨
   purpose 计数，未逐一实测（这会影响 N2 放松面在 demo 档的可达性）。
4. **未验证 `_recovery_gate_ledger_facts` 的 `unknown_orders`/`submitting_orders`
   在 live 就绪链下的真实取值**：只做了代码级推断
   （`Broker.order_outbox.status()` 全 purpose 计数 → 与门禁口径一致）。
5. **未在真实工作树以外的集成树（`mat-wt/I` 等）上验证**：按硬约束不动别人
   worktree；也没有跑 `mat-wt/I` 的集成态。
6. **未评估 N2 放松面在"账本被篡改"场景下的叠加效应**：`order_outbox` 行被同时
   篡改本来就挡不住（作者已登记为 U13-FU1 遗留边界）。
7. **未做安装/部署验证**：不涉及 SCM 双 profile 注册、Windows 服务安装、发布包。

## 9. 报告文件路径

- 本报告：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU2\R1\q\_scratch_Q\Q_U13-FU2_r1.md`
- 探针与日志：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU2\R1\q\_scratch_Q\`
  （`probe_n1.py` / `probe_n2.py` / `probe_n3.py` / `probe_bypass.py` /
  `run_reg_tsv.py` / `logs/`）

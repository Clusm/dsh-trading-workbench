# U13-FU3 补丁包（目录名 `5651531`）

> 目录名 = **打包时（`.diff` 生成时）的分支 head** `5651531`。`.diff` 内容 =
> `2ac0d6f..5651531`（base `2ac0d6f` = U13-FU2 的收尾 head）。本目录自身若在本
> head 之后落库，不影响 `.diff`。

本目录是 **U13-FU3（E 线修复轮）** 的补丁包：修 U13-FU2 独立复验
（`docs/dispatch/reviews/U13-FU2/R1/q/_scratch_Q/Q_U13-FU2_r1.md`，`reviews/`
下同路径）**唯一阻塞项 B-1** —— U13-FU2 的 N2 接线**不是在修，而是把门禁放松了**。

| 文件 | 交付方式 | 说明 |
|---|---|---|
| `src/core/service.py` | `01_service_ledger_outbox_counts.diff` | N2 回归：门禁的 `unknown_orders`/`submitting_orders` 两列改为**从本进程账本实读**（与门禁同一口径），不再"取自形参、缺省 0"。 |
| `scripts/ops/verify_u13f1_recovery_gate.py` | `02_verify_gate_n2_unknown_exit.diff` | 用复验者原场景补**行为断言**（不只 `bare == 0` 的接线 spy）。**分支内独占文件**（同 U13-FU2 约定：集成树不带，它由 E 线分支提供）；`.diff` 只为"分支自洽 + 复核可重现"提供。 |

`src/core/ops/recovery.py` **本轮未改**（N1/N3 保持原样，见 §4）。

---

## 1. N2 回归的前后对照（核心）

**复验者原场景**（`_scratch_Q/probe_n2.py` 的 `unknown_exit`，本轮原样复用）：

1. 用真实 `AuditDatabase` 建账本，真实 `require_reconcile`/`complete_reconcile`
   把门禁推到 `CLEARED`；
2. **门禁 CLEARED 之后**往 `order_outbox` 插一颗 `purpose='EXIT'`、
   `status='UNKNOWN'` 的单并 checkpoint 进主文件；
3. 构造 `TradingService`，观察启动期判定 / `control.state` /
   `set_control("normal")`。

```bash
python <Q>/_scratch_Q/probe_n2.py <tree>          # Q 原探针，未改一行
```

| 观察（`unknown_exit`） | pristine `7885e6a` | **U13-FU2 `2ac0d6f`** | **U13-FU3 `5651531`（本修复）** |
|---|---|---|---|
| 启动期 `before_entry` | 阻断 `RECONCILE_REQUIRED_BEFORE_ENTRY`（裸调用） | **放行 `GATE_CLEARED`（放松）** | 阻断 `RECONCILE_REQUIRED_BEFORE_ENTRY` |
| `control.state` | `close_only` | **`normal`** | `close_only` |
| `set_control("normal")` | `ok=False` 被拒 | **`ok=True` 成功** | `ok=False` 被拒 |
| 启动/`set_control` 注入的 `unknown_orders` | 0（冷启动快照仍取形参缺省） | **0** | **1（账本实读）** |
| 冷启动就绪快照 `recovery_gate_allowed` | `True` | `True` | **`False`** |

```
pristine: PROBE N2: 7 通过 / 3 失败  exit=1   （3 条 = 接线形态×2 + C1）
U13-FU2 : PROBE N2: 7 通过 / 3 失败  exit=1   （3 条 = C1/C2/C3 —— 失败集合不同即行为翻转）
U13-FU3 : PROBE N2: 10 通过 / 0 失败 exit=0
```

逐条日志：`evidence/n2_probe_{pristine,fu2,fixed}.log`（`Q` 探针副本见
`evidence/probe_n2_Q.py.txt`）。

**关于 pristine 的 C1**：探针 C1 断的是就绪快照字段 `recovery_gate_allowed`，
pristine 上它是 `True`（`C` 行的 `gate_allowed=True codes=['GATE_CLEARED']`）——
因为**冷启动就绪快照**（`service.py:411`）本来就是"无参调用"的第三处，在 pristine
上同样取形参缺省 0。pristine 的**有效**阻断走的是启动路径那条**裸** `before_entry()`
（`control.state` 被收紧为 `close_only`、`set_control` 被拒，C2/C3 在 pristine 上
PASS）。FU3 之后**三处**（启动 / `set_control` / 冷启动快照）都走账本实读，C1/C2/C3
全 PASS。

### 根因（结构性，不是边角）

`src/core/service.py` 的 `_recovery_gate_ledger_facts(facts=None)`：

- `unknown_orders`/`submitting_orders` **只**取自 `facts` 形参、缺省硬编码 `0`；
- 只有 `unresolved_entry_orders` 真的查账本（且带 `purpose='ENTRY'` 过滤）。

U13-FU2 新接线的两条路径（启动 `:319`、`set_control` `:1426`）都是**无参调用**
`self._recovery_gate_ledger_facts()` → 这两列**恒为 0**。而门禁自己的
`ledger_reconcile_facts` 对这两列是**不分 purpose 全表计数**，`_facts_block_unresolved`
把任一列非零视为阻断。于是"账本里唯一未收口的是非 ENTRY 单"时，
**裸形态阻断、注入形态放行**——方向是**放松**，对 U13/F14"恢复未收口时不允许把
控制面拨回 `normal`"的既有整改构成回退。

U13-FU2 的作者验证器为什么没发现：N2 的 4 条 check 全是 `bare == 0` 的**接线 spy**，
从不读 `control.state` / `set_control()` 返回值；且 `[dirty_wal]` 场景恰是两态同结论
的那种。**结构上不可能**发现放松面。

---

## 2. 修的是"这两列根本没读账本"这个结构问题

```python
# 新增：三列一律从本进程 order_outbox 实读，复用门禁自己的常量保证口径同源
def _ledger_outbox_counts(self) -> dict[str, int] | None:
    statuses = ",".join("?" for _ in UNRESOLVED_ORDER_STATUSES)
    row = self.db.fetch_one(
        "SELECT "
        " SUM(CASE WHEN status='UNKNOWN' THEN 1 ELSE 0 END) AS unknown_orders, "
        " SUM(CASE WHEN status='SUBMITTING' THEN 1 ELSE 0 END) AS submitting_orders, "
        f" SUM(CASE WHEN status IN ({statuses}) "
        "          AND UPPER(COALESCE(purpose,''))='ENTRY' THEN 1 ELSE 0 END) "
        "   AS unresolved_entry_orders "
        "FROM order_outbox", tuple(UNRESOLVED_ORDER_STATUSES))
```

要点：

1. **不是针对 `purpose='EXIT'` 的特判**。判定表达式就是门禁
   `ledger_reconcile_facts` 的那三条 `SUM(CASE ...)`，且 `FACTS_KEYS` /
   `UNRESOLVED_ORDER_STATUSES` 直接从 `core.ops.recovery` **导入**（`service.py`
   顶部 import 改动）：口径只存在一份，门禁改口径时这里跟着改，不会漂移。
2. **为什么不会漏掉其它 purpose/status 组合**：`unknown_orders`/`submitting_orders`
   的 `WHERE` 里**没有任何 purpose 条件**，`unresolved_entry_orders` 的 purpose
   条件与门禁逐字相同。因此"门禁会数到的行"与"这里会数到的行"是**同一个谓词的
   同一结果**：任何 `status` 取值（`UNKNOWN`/`SUBMITTING` 两列覆盖门禁真正会阻断的
   两列；其余状态由第三列按 `UNRESOLVED_ORDER_STATUSES` 覆盖）与任何 `purpose`
   取值（`ENTRY`/`EXIT`/`PROTECTIVE`/`NULL`/任意新值）都按门禁的同一规则计数。
   `COALESCE(purpose,'')` 保证了 `NULL` purpose 不会漏。
3. **读不出账本不再等于"干净"**：`_ledger_outbox_counts()` 返回 `None` 时退回
   门禁自己的账本读取（与裸形态同源，`gate.live_reconcile_facts()`）；仍读不出则
   返回三元组全 `1`（fail-closed，与 U13-FU1/P1-3 的既有约定一致）。若退回得到
   `source="not_applicable"`（账本没有 `order_outbox` 表），该来源标记原样透传给
   门禁，由 `_facts_unreadable` 判阻断——与裸形态同结论，**不把"不适用"当"干净"**。
4. **`facts` 形参降级为"额外下界"**（逐列取 `max`）：调用方多报的未收口单照样生效
   （就绪链注入的 broker `outbox.status()` 取值不受影响），少报 / 漏报 / 不传都不再
   能放宽判定。这样**任何**调用形态（含未被接线的 `service.py:987` 就绪链）都不会
   比门禁自身更松。
5. **单向性**：修改只可能让三元组**变大或不变**，不会变小 → 判定只可能更严。
   干净账本仍然 `0/0/0`（`[clean]` 对照不被误伤）。

---

## 3. 验收扩展：用复验者原场景补行为断言（两态可跑）

`scripts/ops/verify_u13f1_recovery_gate.py` 新增：

- 夹具 `_build_service_root(..., mode="unknown_exit")`：门禁 CLEARED **之后**插入
  `purpose='EXIT'`/`status='UNKNOWN'` 单并 checkpoint 落主文件（= 复验者场景）；
- `[unknown_exit] 注入事实里的 unknown_orders 来自账本（>=1，非 ENTRY 未收口单也
  计入）` —— 直接钉住"这两列读了账本"（U13-FU2 的验证器缺的正是这条）；
- `[unknown_exit] 启动收紧为 close_only`、`[unknown_exit] 冷启动就绪快照的门禁判定
  也阻断`、`[unknown_exit] set_control('normal') 被门禁拒绝` —— **行为断言**；
- **保留** `[clean]`（账本干净不误伤）与 `[dirty_wal]`（未收口 ENTRY）对照。

```bash
python scripts/ops/verify_u13f1_recovery_gate.py      # 三态同一份脚本
```

| 树 | 结果 | exit |
|---|---|---|
| pristine `7885e6a` | **47 通过 / 11 失败** | 1 |
| U13-FU2 `2ac0d6f` | **54 通过 / 4 失败**（4 条全在 `[unknown_exit]`） | 1 |
| U13-FU3 `5651531` | **58 通过 / 0 失败** | 0 |

`[unknown_exit]` 三条**可区分**断言（pristine PASS / FU2 FAIL / 修复 PASS）：
`unknown_orders>=1`、`启动收紧为 close_only`、`set_control('normal') 被拒`。
日志：`evidence/verify_{pristine,fu2,fixed}.log`。

**未放宽任何断言**：本轮的验证器改动是**纯新增**（+99 / −12，删掉的 12 行是
`check_n2_service_paths` 里被重构的循环与旧 `dirty` 分支，语义更严不变松），
`[clean]`/`[dirty_wal]` 的既有断言逐条保留；`tests/acceptance/test_supervision.py`
**本轮零改动**。

---

## 4. N1 / N3 不回归

`src/core/ops/recovery.py` 与 N3 文档**本轮未改**（`git diff 2ac0d6f 5651531`
只含上表两个文件），N1 的三处 fail-open 翻转逐字保留：

```bash
python <Q>/_scratch_Q/probe_n1.py <tree>       # N1 10 场景
python <Q>/_scratch_Q/probe_bypass.py <tree>   # 9 组绕过
python <Q>/_scratch_Q/probe_n3.py <tree>       # N3 doc vs code
```

| 探针 | pristine | U13-FU2 | U13-FU3 |
|---|---|---|---|
| `probe_n1.py` | 8 通过 / 3 失败 exit=1（S1/S2/S7 fail-open） | 11 / 0 exit=0 | **11 / 0 exit=0** |
| `probe_bypass.py` | 11 / 2 exit=1 | 13 / 0 exit=0 | **13 / 0 exit=0** |
| `probe_n3.py` | 4 / 2 exit=1 | 6 / 0 exit=0 | **6 / 0 exit=0** |

N1 的 3 条翻转（`[none]`/`[no_outbox]`/`[injected:not_applicable]` →
`EVIDENCE_UNVERIFIED`）在修复态仍为阻断（`evidence/n1_probe_fixed.log`）。

---

## 5. 沙箱 full delta + 受影响套件 + check_quality

方法（与复验者 Q 同口径）：两棵树 `git archive` 导出后**立刻**
`git init && git add -A -f` 提交，逐脚本跑各树自己 `scripts/run_offline_regression.py`
的 `SCRIPTS`（清单 AST 读入，不另抄；`EXECUTION_MODE=paper`、清
`MTA_PROFILE`/`MTA_STATE_ROOT`/`MTA_DATA_ROOT`、清凭据变量、`PYTHONUTF8=1`）。

```
pristine: TOTAL 85 scripts, nonzero=0, wall=271s
patched : TOTAL 85 scripts, nonzero=0, wall=270s
```

| 项 | 结果 |
|---|---|
| 脚本集合 | 85 / 85，两棵树**同一集合**（`set==set` True） |
| pristine（`7885e6a`）非零 | **0 / 85** |
| patched（`5651531`）非零 | **0 / 85** |
| **NEW_FAIL**（0→≠0） | **∅** |
| **FIXED**（≠0→0） | **∅** |
| BOTH_NONZERO | **∅** |

逐脚本 rc 原文：`evidence/reg_pristine.tsv` / `evidence/reg_patched.tsv`，
汇总结论：`evidence/full_delta_summary.txt`。

受影响套件（在 patched 沙箱里单跑，均 exit 0）：

| 脚本 | pristine | patched |
|---|---|---|
| `scripts/check_quality.py` | 0（`QUALITY CHECK OK files=295 untracked=0`） | 0（同左） |
| `scripts/smoke_u13_supervision.py`（内含**扩展后的** `ops/verify_u13f1_recovery_gate.py`） | 0 | 0（`U13 SMOKE OK steps=4`，内含 `58 通过 / 0 失败`） |
| `scripts/smoke_contracts_v3.py` | 0（`PASS 173 / FAIL 0`） | 0（同左） |
| `scripts/smoke_recovery_bundle.py` | 0（`11 通过 / 0 失败`） | 0（同左） |

（`02` 是**分支内独占文件**：集成树不带该文件，由 E 线分支提供；它在本沙箱里由
`git archive 5651531` 直接带入，因此 `verify_u13f1_recovery_gate.py` 已是扩展后的版本。）

## 6. 补丁包与 `git apply --check`

| 补丁 | `ap_pristine`（`7885e6a`） | `ap_fu2`（`2ac0d6f`） |
|---|---|---|
| `01_service_ledger_outbox_counts.diff` | rc=0 `Checking=1 Skipped=0` | rc=0 `Checking=1 Skipped=0` |
| `02_verify_gate_n2_unknown_exit.diff` | rc=1（缺 U13-FU2 的验证器小节，**预期**） | rc=0 `Checking=1 Skipped=0` |

两档均在 **3 种 git 配置**（默认 / `-c core.autocrlf=false -c core.eol=lf` /
`-c core.autocrlf=true`，`-c` 置于子命令前）下各跑一次，结论一致：
**`01` 在两种 base × 三配置 = 6/6 `rc=0 / Checking=1 / Skipped=0`**；
`02` 在 `ap_fu2` 上 3/3 同样干净。**集成顺序 = U13-FU2 补丁 01–03 → 本目录 `01`**
（`02` 是 E 线分支独占文件，集成树不带）。

等价性：`ap_fu2` + `01` + `02` 得到的 `src/core/service.py`、
`scripts/ops/verify_u13f1_recovery_gate.py` 与 `git archive 5651531` 对应文件
**逐字节一致**（`diff --strip-trailing-cr`）。

`scripts/check_quality.py`（含 ruff / secret scan）两棵树**逐字节相同**（未新增任何
ruff exclude / per-file-ignores），`QUALITY CHECK OK files=295 untracked=0`，exit 0
（日志：`evidence/check_quality_{pristine,fixed}.log`）。

---

## 7. 未尽事项（如实登记）

1. **pristine 的 N2 场景只有"启动路径"阻断**：pristine 的**冷启动就绪快照**
   （`service.py:411`）在 `unknown_exit` 场景下本来就放行（`recovery_gate_allowed=True`），
   这是 U13-FU2 之前就存在的同源缺陷（Q 报告 §2b 末段登记的"同类但不新增的第三处"）。
   本修复顺带把它一起收严（三处都走账本实读），因此 `[unknown_exit] 冷启动就绪快照`
   这条断言在 pristine 上 FAIL、在修复态 PASS——**该断言的 pristine 基线是"不阻断"**，
   登记以免被读成"修复态更严于要求"。
2. 未评估 `unknown_orders`/`submitting_orders` 采用**全表（不按 `environment`
   过滤）**计数在多环境共用同一 `agent_trader.sqlite` 时的额外阻断面。本修复刻意
   与门禁口径一致（门禁也不按 environment 过滤）；一个状态根对应一个环境是现行
   部署约定，但未在真实多环境库上实测。
3. `_ledger_outbox_counts()` 读不出时退回 `gate.live_reconcile_facts()`
   （多开一次只读连接 + 一次 immutable 快照读）。该退路只在"本进程账本读不出"的
   异常窗口触发，未做高频路径的实测性能计量。
4. 未修任何其它 P2/P3（含 U13-FU2 复验 N4：补丁 05 不必要、作者对 base 红灯的
   归因有误），按约定留给 U12。
5. 未做真实交易所侧 / 真实断电撕裂写 / 真实多账户凭据 / SCM 双 profile 注册验证；
   全部证据来自进程内假 broker 与系统临时目录里的合成状态根。N2 的放松面与本次
   修复是在合成的 `order_outbox` 行上演示/验证的（真实生产账本只读约束下不可读）。
6. 与 U13-FU1/FU2 相同的边界仍然存在且未处理：门禁文件 + 安装戳**同时**删除 →
   `GATE_NOT_INSTALLED` 放行；同时篡改账本 `order_outbox` 行不可挡；
   `complete_reconcile` 在账本不可读时仍可按证据自声明 `CLEARED`（不产生放行能力，
   但状态位会被写脏）。

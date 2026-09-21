# A 角色交叉审查 · U09 发布证据门禁（R1，接续会话）

- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U09\R1\a`（detached HEAD @ `b675118`）
- 审查对象：U09 代码 head `7cb6a71`（`b675118` 只多一份 docs README）；基线 `448a4e9`
- 本会话为**接续会话**：只读上一会话 `_scratch_A/*` 证据 + 最小补跑（`verify1.py`），不从零重做。
- 运行目录 `multi-agent-trader` 只读；demo 服务未停；未联网/未下单/未改产品代码。

---

## 1. 结论

**PASS-with-notes**（无 P0/P1 阻塞项）

- U09 的**来源口径（S08）、窗口口径（F24/K06）、ACK≠送达（F12）**方向正确且 fail-closed，实测可复现。
- 三条悬念**全部查清**：① 是"诚实披露 + 残余未设门槛"（既有口径，非 U09 回归）；② U09 **确实读错了表**（读 outcome 快照而非 lifecycle 权威态），方向 fail-open，但该读法**继承自基线、非 U09 新引入**，且离线无法证明真写路径会产生两表分歧 ⇒ 记为 **P2 需修不阻塞**；③ `REVISION_CONFLICT:outcome_id=` 子分支在真 schema 上**不可达**，其测试理由不成立 ⇒ **P3**。
- **[S3] 悬念已定论：S3 不是 fail-open 证据，而是一个自相矛盾的夹具**——它的"冲突插入"复用了同一 `outcome_id` 做 `INSERT OR REPLACE`，把第二条修订**覆盖**掉了，因此 S3 实际是"30 条合法、0 冲突"，PASS 30 是**正确**答案。它与 T5a 不矛盾。

### 阻塞项
无。

### 建议的最小修复（非阻塞，按优先级）
1. **P2**：`eligible_closed_lifecycles` 的成本/归因判据应同时要求 `position_lifecycles.cost_status` 与 `attribution_status` 为 COMPLETE（或用两表一致性校验），不能只信 `trade_outcomes` 快照（见 §2②）。
2. **P2**：`net_realized_unknown > 0` 时至少给 blocker/原因码，或把"COMPLETE 但净收益不可算"判为数据不一致（见 §2①）。
3. **P3**：把验收套件里"重复 outcome_id 不同内容"的断言改述为"纯函数防御性覆盖（DB 约束保证不可达）"，并删掉"历史库可能无 UNIQUE"的理由（该 UNIQUE 自首个基线 commit `186f266` 即存在）。

---

## 2. 三条悬念裁决

### ① 净收益未知的生命周期仍计入"30 完整闭合"

**判定：这是"诚实披露"，但不构成一个通过意义上的门槛 —— 残余 fail-open，P2。**
理由如下（**重要限定**：该口径**继承自基线**，不是 U09 回归；U09 新增的是披露字段）：

**最小构造（实测，`verify1.out [V1]`）**：30 条 lifecycle，`cost_status='COMPLETE'` 而 `funding_cashflow=NULL` → 门禁 `eligible_closed_trades` = **PASS / actual=30**，同时 `net_realized_unknown=30`、`eligible_net_realized_total=0.0`、`eligible_closed_rows` 中 **净收益可算的样本数 = 0**、`cost_recomputed` 全为 `False`。
⇒ **最少 1 个**净收益未知的样本即可把"29 条真凭证 + 1 条不可算"凑成 30 并放行。

命令 / 输出 / exit：
```
$ python _scratch_A/verify1.py   # exit=0
[V1] 30 条 funding=NULL（净收益全未知）-> {'state':'PASS','actual':30,'net_unknown':30,'net_total':0.0,...}
     eligible_closed_rows 的 cost_recomputed: [False]
     -> 净收益可算的样本数 = 0
```
佐证（上一会话证据，已重跑 exit=0）：`q1.out` `funding=None（净收益未知） eligible=True` / `eligible = True net_realized = None`；`q234.out [S5]` `net_unknown=1` 而 `current=31` 仍 PASS。

**为什么仍判 P2 而非 P0/P1**：U01 自己的契约使 `cost_status=COMPLETE ⟹ 净收益可算`。`trade_reconciliation.py:610` 的 `cost_complete` 要求 `entry_fee / exit_fee_value / funding` **三者非 None** 且费用资产 ⊆{USDT} 才给 COMPLETE；`k03_income.finalize_costs` 也只在"有归因决策"或"合法零费证明"时才 COMPLETE（`q1.out` 七种场景逐条对上）。真写路径（`binance_futures.py:2185` 旧路径不传 `cost_status` ⇒ 默认 `UNKNOWN`）不会产出"COMPLETE 但成本项缺失"。所以 **V1 这种数据本身违反 U01 不变量**，不是现实攻击面；U09 的问题在于（a）不校验该不变量、（b）对 `net_realized_unknown` **没有任何门槛或 blocker**，只在 `release_evidence` 里披露，且 README §8 九条"未尽事项"**未登记这一条**。
⇒ 判定：披露是诚实的，但门禁 reason 自述"成本证据"却对"成本不可算"零门槛，属**欠严**；应把"COMPLETE 却净收益不可算"当数据不一致上报（或对 unknown 设阈值）。

### ② `cost_status` 权威表：lifecycle 行 vs outcome 行

**判定：U09 读的是 outcome 行（`trade_outcomes.cost_status`），而 U01 的权威态是 lifecycle 行（`position_lifecycles.cost_status`）。S4 是 fail-open —— 但该读法继承自基线，非 U09 新引入，P2。**

证据一（U09 只读 outcome 行）：
`src/core/release_evidence.py:1097-1106` 的查询 `SELECT l.status AS lifecycle_status, o.status, o.attribution_status, o.cost_status, ... FROM position_lifecycles l LEFT JOIN trade_outcomes o ...` —— lifecycle 侧的 `cost_status` **根本没被 SELECT**；`_eligible_lifecycle`（:782）判的是传入行（即 `latest`，outcome 行）的 `attribution_status/cost_status`。

证据二（布尔双向实测，`verify1.out [V2a]/[V2b]`，exit=0）：
```
[V2a] lifecycle.cost=INCOMPLETE / outcome.cost=COMPLETE -> state=PASS actual=30   ← 与 S4 一致
[V2b] lifecycle.cost=COMPLETE   / outcome.cost=INCOMPLETE -> state=FAIL actual=29 incomplete=1
```
⇒ 门禁的"成本完整"**完全由 outcome 行决定**，lifecycle 行的 `INCOMPLETE` 被无视。

证据三（哪张表才是权威）：
- `src/exchange/trade_reconciliation.py:625-637` 是**唯一**做成本重算的写方，它 `UPDATE position_lifecycles SET ... cost_status=?`；`binance_futures.py:2652-2657` 与 `:3120-3122` 两处在拿到开仓手续费后 **只把 lifecycle 降级为 `cost_status="INCOMPLETE"`，不碰 outcome**。即 U01 把"成本尚未闭合"写在 lifecycle 行。
- `src/audit/database.py:2082+` 的 `log_trade_outcome` 是 `INSERT OR IGNORE`，docstring 明说"outcome_id 由成交命令/交易所订单号稳定派生…使用 INSERT OR IGNORE 让补记天然幂等"；`q234.out [S6]` 实证：同一 `outcome_id` 二次写入不同内容（`exit_fee 0.1→0.9`、`gross 1.0→0.5`）**被静默丢弃**，落库仍是 `0.1 / 1.0`，行数 `1→1`。
⇒ **lifecycle 行是可变、当前的权威聚合态；outcome 行是首次写入即冻结的快照。** 一旦二者分歧，U09 读到的是**过期快照**，把"成本未闭合"读成"已闭合"（fail-open 方向）。

证据四（"继承 vs 新引入"）：基线 `_scratch_A/base_release_evidence.py:357,373` 同样是 `SELECT ... o.cost_status` + 判 `cost_status=='COMPLETE'`。⇒ **既有口径，U09 未修**。U09 的 F23 工作（按 `lifecycle_id` 去重、取最新修订、冲突隔离）反而使"以 outcome 行为准"更显式。

**未确认项（如实登记）**：我**未能离线端到端复现**真写路径造出 `l.cost=INCOMPLETE / o.cost=COMPLETE`。静态上该分歧可构造（如迟到 EXIT 成交以非 USDT 计费 → `trade_reconciliation:625` 把已 CLOSED 的 lifecycle 置 INCOMPLETE，而 outcome 已在上一轮冻结为 COMPLETE），但需要联网/下单才能跑真路径，超出本次约束。按"未知即未知"，此项记 **P2 + 待真机复核**，不升为阻塞。

### ③ U09 的 `REVISION_CONFLICT:outcome_id=` 分支在真 schema 上是否可达

**判定：不可达。该断言确实在检验"数据库产生不出来的状态"。定级 P3。**

证据：
- `src/audit/database.py:309-311` `CREATE TABLE ... trade_outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT, outcome_id TEXT NOT NULL UNIQUE, ...)` ⇒ `outcome_id` **有 UNIQUE**。
- `git log -S"outcome_id TEXT NOT NULL UNIQUE" -- src/audit/database.py` 最早出现在**首个基线 commit `186f266`**；`git show 448a4e9:src/audit/database.py` 同样含该行 ⇒ **不存在"历史库无 UNIQUE"的可达状态**（CREATE TABLE IF NOT EXISTS 也不会移除既有约束）。
- `q2.out [T1]`（exit=0）：同 `outcome_id` 二次 **plain INSERT** → `UNIQUE constraint failed: trade_outcomes.outcome_id`。
- 真写入路径 `log_trade_outcome` 是 `INSERT OR IGNORE`（`q234.out [S6]` 实证行数 1→1）⇒ 连"冲突行"都进不去。
- `select_lifecycle_revisions` 的该分支判据是 `by_outcome[outcome_id]` 的签名集合 `>1`（`release_evidence.py:750-756`）；DB 路径上同一 `outcome_id` 至多一行 ⇒ `len(signatures)>1` 恒假 ⇒ **死分支**。`q2.out [T2]` 是用**内存行**直接调纯函数才触发的。

定级依据与背景：
- 该分支本身 fail-closed（冲突⇒排除），**对门禁结果无影响**，故不是 P2。
- 但 U09 验收套件 `tests/acceptance/test_release_evidence_v2.py:573-578` 用 `check("重复 outcome_id 不同内容 → REVISION_CONFLICT（不静默挑一条）")` 断言了它，注释理由写作"**DB 层 OR IGNORE 挡不住的历史库**也要隔离（纯函数级）"。**该理由不成立**（UNIQUE 自首 commit 起就在）⇒ 属"不可达分支被当成已覆盖 + 理由站不住"，是**测试/声明卫生问题**，P3（新引入、fail-closed）。
- 注：`REVISION_CONFLICT` **机制整体仍可达且已被真夹具覆盖**——可达的是另一子分支 `same_recorded_at`（`release_evidence.py:757-762`），`q2.out [T3]`/`[T5a]` 与 `accept.out §4` 均用真库写入实证（`conflicted=1`、`eligible=29`、门禁 FAIL）。所以"冲突隔离"这一能力不是空头支票，只有 `outcome_id=` 这一条子分支是死的。

### [S3] vs [T5a] 差异定论（本次关键实验）

**定论：S3 与 T5a 不矛盾。S3 的"冲突"根本不是冲突 —— 它的插入用了与 lifecycle 写入时相同的 `outcome_id` 且是 `INSERT OR REPLACE`，第二条修订被就地覆盖，因此 S3 实际是 "30 条合法 / 0 冲突"，`PASS 30` 是正确结果。S3 夹具的标签（"29合法+1冲突"）是错的。**

最小复现（`verify1.out [V3a]/[V3b]/[V3c]`，exit=0；两条夹具只差插入方式）：
```
[V3a] S3 式 INSERT OR REPLACE 同 outcome_id: 行数 1 -> 1 （只是覆盖，不产生第二条修订）
[V3b] S3 式(同 outcome_id 覆盖)      30 条 -> state=PASS actual=30 conflicted=0  current=30
[V3c] T5a 式(异 outcome_id 同 recorded_at) 30 条 -> state=FAIL actual=29 conflicted=1  incomplete=1
      S3 式冲突行数(诊断)=0   T5a 式冲突行数(诊断)=1
```
- **S3 的构造**（`_scratch_A/q234_db.py:134-143`）：`range(29)` + `lifecycle(29)` = **30 条 lifecycle**，然后 `fx.raw(CONFLICT_INSERT, conflict_params(..., "lc-29", ...))`，而 `conflict_params` 的 `outcome_id` 是 `f"out-{lid}"` = `out-lc-29`，**与 `fx.lifecycle` 刚写的 outcome_id 完全相同**，且 SQL 是 `INSERT OR REPLACE`（`q234_db.py:15-20`）⇒ 覆盖，1 条行，签名一致 ⇒ `by_outcome` 无 `>1`、`same_stamp` 只有一个签名 ⇒ 无冲突。总数 30 条全部合法 ⇒ PASS 30。**这是正确答案，不是 fail-open。**
- **T5a 的构造**（`_scratch_A/q2_conflict.py:159-166`）：同样 30 条 lifecycle，再调 `fx.outcome("lc-29", outcome_id="out2-lc-29", recorded_at=iso(START+39min), exit_fee=0.9, gross=99.0)` —— **不同的 `outcome_id`**，且 `recorded_at` 与 lc-29 原 outcome **同一时间戳**、内容不同 ⇒ 两条真修订、同 stamp、异签名 ⇒ `REVISION_CONFLICT:same_recorded_at=...` ⇒ lc-29 被排除 ⇒ 29 ⇒ FAIL。**这是 fail-closed，正确。**

⇒ 推论与影响：
1. S3 的 PASS 30 **不能**被读作"门禁能被凑数"的证据；同理 `q234.out [S2]`（"30合法+1冲突(共31)" → PASS 31、`conflicted=0`）也因复用 `CONFLICT_INSERT` 而**不是冲突**，只证明"31 条合法 ⇒ 31"。**上一会话 q234 里的 S2/S3 两个"冲突"场景均未真正构造出冲突**，任何基于它们的推论作废。
2. 真正的"29 合法 + 1 冲突"只由 T5a 表达，结论是 **FAIL 29 / NO_GO**（fail-closed），与 U09 验收套件 `accept.out §4` 的断言一致（"冲突生命周期不进 30（29 笔 + 门禁 FAIL，拒绝统计伪通过）"）。
3. 因此**没有**发现"用冲突样本凑数"的 fail-open；S3 的问题属**夹具标签错误**（A 侧实验记录问题），不是 U09 产品缺陷。

---

## 3. 其余发现（P0–P3）

| # | 级别 | 新/既有 | 方向 | 说明 |
|---|---|---|---|---|
| N1 | P3 | 既有 | fail-closed | `same_recorded_at` 冲突判据可能**误伤**：`binance_futures.py` 存在两个派生 outcome_id 的写路径（`bn-<cid>` 见 `:2160/:2185`，`lifecycle-<lid>` 见 `:3224`）。同一 lifecycle 若被两条路径在同一毫秒写入且成本签名略有差异，会被判 `REVISION_CONFLICT` 并从分母**逐出** ⇒ 虚假 FAIL。方向 fail-closed（安全），离线未复现。 |
| N2 | P3 | 新引入 | — | `_eligible_lifecycle` 对 `cost_status/attribution_status` 用 `.upper()` 比较（`q1.out` 小写 `'complete'`/`'attributed'` 判 eligible=True），而 U01 枚举是大写；宽容读法无安全影响，但与 U01 严格枚举不一致，登记即可。 |
| N3 | P3 | 新引入 | — | 门禁 reason 自述"仅计入完整生命周期、归因和成本证据"（`release_evidence.py:1274`），但实现只看 outcome 行、且不校验 net 可算 ⇒ **声明与实现口径不符**（与 §2①②同源），建议连修。 |
| N4 | 信息 | 既有 | — | `window_cost_finalization` / `allocate_funding_or_incomplete` / `reconcile_session` 在 `src/` 内**没有生产调用方**（`IncomeReconciliationBook.persist` 只写 income 三表，不写 lifecycle/outcome 的 `cost_status`）。即 U01 的"窗口成本闭合→生命周期状态"接线在本仓库未见落地，属 U01 侧既有缺口；这正是 §2② 分歧能存在的土壤。 |
| N5 | 信息 | 既有 | — | `current_closed_lifecycles` 计数**不带窗口过滤**（`release_evidence.py:1149-1153`），而 eligible 分母带窗口；两者差值只作诊断，不影响门禁判定，但报告读者易误读为"同一分母"。 |

**未发现 P0/P1。** 特别地，**未发现 U09 用错误证据让门禁虚过**的**新引入**缺陷：S08 影子来源（分母只计 A 组，`smoke.out` Δ=0）、F24 窗口（首尾缺失计入分母、未声明窗口 `NO_DATA+WINDOW_MISSING` 而非按 0 冒充）、NO_EDGE 排除、只读性（DB 字节一致）均实测 fail-closed。

---

## 4. U09 读账本口径：确认对 / 无法确认

**确认对（实测复现）**
- 净收益重算公式 `gross - entry_fee - exit_fee + funding`，且任一成本项缺失即 `None`（不拿 0 冒充）——`release_evidence.py:687-702` 与 `q1.out`/`accept.out §4` 一致。
- 迟到修订：取 `recorded_at` 最新一条、旧行只列 `superseded`、不重复计数（`q2.out [T4]`：仍 30、`net_realized 0.8→0.5`、`evidence_revision_hash` 变化）；同 `recorded_at` 内容冲突 → 排除且门禁 FAIL（`[T3]/[T5a]`）。
- 分母按 `lifecycle_id` 去重、双向仓 LONG/SHORT/BOTH 各计一次；`cost_status!=COMPLETE`/`attribution_status!=ATTRIBUTED`/`qty=0`/`entry_price=0` 一律不计入（`q1.out` 边界probe 全 False）。
- 与 U01 `finalize_costs` 七种真实输出对照，`_eligible` 判据**同向**（INCOMPLETE/UNKNOWN 全 False，仅真 COMPLETE 为 True）——`q1.out` 表。

**无法确认（如实登记）**
- U01 真写路径**端到端**能否造出 `l.cost_status=INCOMPLETE` 与 `o.cost_status=COMPLETE` 的分歧（§2②；静态可构造，离线未复现，需真机）。
- 生产侧是否会写入发布窗口声明（README §8 第 2 条自认"窗口声明尚无生产写入方"）。若 U12/U15 不写，真实报告只会得到 `NO_DATA + WINDOW_MISSING`、分母为 `None` —— 这会让 §2① 的"30 分母可被凑数"在真实发布里**根本走不到**（fail-closed）。这是 U09 的既有登记限制，我未独立核实 service 侧。
- 未独立复算 U09 自证的其他块（K06 五组、S08 影子来源计数、观察窗口/ACK/重启语义）：本次按"交叉审查门禁 fail-open"聚焦，这些项以 `accept.out`（65/0，exit=0）与 `smoke.out`（9/0，exit=0）为 U09 自证，未逐项独立推导。

---

## 5. 证据文件与命令

| 用途 | 命令（cwd = 工作树根） | exit | 结果 |
|---|---|---|---|
| ①最小构造 + ②双向 + ③/S3 定论 | `PY=.../.venv/Scripts/python.exe; export PYTHONUTF8=1 PYTHONPATH=$PWD/src; $PY _scratch_A/verify1.py` | 0 | `_scratch_A/verify1.out` |
| U01 语义 vs U09 判据 | `$PY _scratch_A/q1_semantics.py` | 0 | `_scratch_A/q1.out` |
| 冲突/修订真路径 | `$PY _scratch_A/q2_conflict.py` | 0 | `_scratch_A/q2.out` |
| 构造矩阵 S1–S7 | `$PY _scratch_A/q234_db.py` | 0 | `_scratch_A/q234.out` |
| U09 验收套件 | `$PY tests/acceptance/test_release_evidence_v2.py` | 0 | `_scratch_A/accept.out`：`65 通过 / 0 失败`（本树 shared 补丁未应用态） |
| U09 冒烟 | `$PY scripts/smoke_u09_release_evidence.py` | 0 | `_scratch_A/smoke.out`：`9 通过 / 0 失败` |

- 本报告：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U09\R1\a\_scratch_A\A_U09_r1.md`
- 本次新增：`_scratch_A/verify1.py`、`_scratch_A/verify1.out`
- 只读性：本次未写工作树 `_scratch_A/` 以外任何路径；未动产品代码、未 push/合并/打 tag。

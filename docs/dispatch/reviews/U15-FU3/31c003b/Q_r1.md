# Q / U15-FU3 独立验收报告 (R1)

工作树: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15-FU3\R1\q` @ detached `31c003b` (code head `7dd1053`)
角色: 独立验收 Q — 只验证，不改产品代码。
日期: 2026-09-22

## 0. 结论

**PASS-with-notes**（无阻塞项）。第 1–9 条全部独立复现通过。
关键判定：**两处 fail-closed 生效**、**合法路径端到端仍放行（未过度修正）**、
**口径单一（零第二名单/零哨兵字面量）**、**lookalike 不误杀**、**既有语义逐条两态字节相同**。

非阻塞 notes：
1. `ReadinessSnapshot.runtime_instance_id` 仍原样回显入参（哨兵时仍 `"unknown"`）。
   经核查**无消费方**读该字段放行（见第 6 条），故不构成未闭合路径——与作者自认一致。
2. **[本轮新发现，pre-existing，两态完全相同]** 纯空白实例号（`"   "`）在两个入口
   **仍被放行**（`reasons=[]`）。原因：`evaluate_entry_readiness` 只做真值判断
   `not effective_runtime_id` 且不 `strip()`；`is_placeholder_runtime_instance_id("   ")`
   经 `strip()` 得 `""` 不在名单 → `False`。这是**本轮补丁未覆盖的同族残留**，
   但 base/patched **逐字相同**（非回归），且真实 `TradingService` 传的是
   `uuid4().hex` 或运行代，不会产生纯空白，故**不判阻塞**。仅记录供后续收口参考。

---

## 1. 【最重要】两处前后对照（自家探针，base 048033b vs patched 31c003b）

自家探针（`_scratch_Q/probe_q.py`，注入 `MTA_TREE_SRC` 决定读哪棵树，probe 内 assert
`core.entry_readiness.__file__` 落在注入树内）两态输出 diff，**语义差异恰好 3 条**，
与作者声称逐字一致：

| 探针编号 | base | patched |
|---|---|---|
| A1 `evaluate_entry_readiness(unknown)`（显式参数） | `allowed=true, reasons=[]` | `allowed=false, reasons=["RUNTIME_INSTANCE_UNBOUND"]` |
| A2 同上，哨兵经 `facts["runtime_instance_id"]` | `allowed=true, reasons=[]` | `allowed=false, reasons=["RUNTIME_INSTANCE_UNBOUND"]` |
| A3 `authorization_matches_context`（两侧哨兵） | `true` | `false` |

佐证：
- A4（expected=哨兵 / actual=真值）与 A5（expected=真值 / actual=哨兵）两态**均为 `false`**
  —— 已被既有 `actual != expected` 分支拦住，本轮未动 ⇒ 无回归。
- 全 16 个哨兵字面量（`RUNTIME_INSTANCE_PLACEHOLDERS`）：base 全部放行
  （`C_sentinel_any_passed` = 全名单）→ patched 全部拦截（`[]`）。
- 唯一字符串差异（B3b `authorization_context` 的 `authorization_id`）来自 `uuid.uuid4()`，
  非语义；`runtime_instance_id` 回显在 B3b 两态均为真实值。

## 2. 【最重要】合法路径仍放行（含端到端到实际门禁）

构造**完整且合法**的入参（真实例号 `rt-2026-09-22-a1b2c3` + 15 项事实齐备 +
有效 scope/environment + TTL 未过）：

- `B1/B2 evaluate_entry_readiness(真实例号)`（显式参数 与 经 facts）两态均
  **`allowed=true`、`reasons=[]`、无新增阻断码**（diff 无此行）。
- `B3 authorization_matches_context`（两侧真实同值）两态均 **`true`**。
- **端到端（`_scratch_Q/probe_e2e.py`）**：走真实门禁
  `sign_execution` → `filter_unauthorized_opens`（broker 边界，内部经
  `_has_valid_signature` → `authorization_matches_context`）：
  - `E2E_legal_real_instance`：**两态逐字相同** —
    `readiness_allowed=true, opens_kept=["BTCUSDT"], bypass_blocked=0`。
    合法开仓**照常放行**，未被新闸门堵死。
  - `E2E_lookalike_unknownverse`：两态相同，放行。
  - `E2E_empty_instance`：两态相同，`reasons=["RUNTIME_INSTANCE_UNPROVEN"]`，
    仍被拦（既有语义，非本轮引出）。
  - 对照：`E2E_sentinel_unknown` base `opens_kept=["BTCUSDT"],bypass_blocked=0`
    → patched `opens_kept=[],bypass_blocked=1`（哨兵在**真实门禁**被拦，缺陷复现并修复）；
    `E2E_sentinel_signed_context`（用哨兵快照上下文签发）同样 base 放行 → patched 拦。

**⇒ 合法路径未被堵死，判「非过度修正」。**

## 3. 口径单一 / 谓词调用点计数

AST 扫描全 `src/`：
- 运行代哨兵**名单只有一处定义**：`src/core/runtime_identity.py:1621`
  （`RUNTIME_INSTANCE_PLACEHOLDERS`，16 成员）。其余疑似「>=3 字符串且含 unknown/none/null」
  的元组均为无关枚举（market_regime / order status / income 等），**无第二份运行代名单**。
- `entry_readiness.py` 代码中（去注释/docstring，AST 常量扫描）
  **哨兵字符串字面量 = `[]`**（`"unknown"` 仅出现在注释）。无量第二套归一化。
- 原因码 `RUNTIME_INSTANCE_UNBOUND` 为 `from core.runtime_identity import` 的**常量**（非硬编码串）。
- `is_placeholder_runtime_instance_id` 调用点：语法级 **8 次调用**，归并为 **6 个逻辑调用点**
  （与作者一致）：
  1. `entry_readiness.py:179`（本轮新增）
  2. `entry_readiness.py:258+259`（同一 `if` 条件，2 次调用；本轮新增）
  3. `runtime_identity.py:1646`（`resolve_runtime_instance_id`，FU1）
  4. `runtime_identity.py:1846+1847`（`verify_account_binding`，FU2）
  5. `runtime_identity.py:2010`（`collect_runtime_identity`，FU1）
  6. `ops/supervision.py:266`（RunRegistry，FU2）

## 4. 不误杀（lookalike 真值）

两态在**两个入口**上均照常通过（diff 无对应行）：
`unknownverse` / `known` / `n/a-x` / `UNKNOWN-X` / `unknown-ish` / `tbd-2`
→ `readiness.allowed=true, reasons=[]`；`authorization_matches_context=True`。
端到端 `E2E_lookalike_unknownverse` 亦两态放行。
（注：我额外探测的 `none` / `NULL` / ` n/a ` 被拦是**正确**行为——它们经
`casefold()+strip()` 后确实等于名单成员，非误杀。）

## 5. 既有语义逐条未变

`D`/`E`/`F` 三组探针输出**两态逐字节相同**（`diff` 无输出）：
- **三态事实**：`order_mirror_ok` / `protection_mirror_ok` 的
  `None→*_UNPROVEN`、`False→*_MISMATCH`、`True→[]` 逐条未变；
  「缺证据的既有码未被替换、也未叠加哨兵码」。
- **空/缺失实例号**（`""` / `None`）→ 仍为 **`["RUNTIME_INSTANCE_UNPROVEN"]`**，
  **未**被改成 `UNBOUND`（与作者/任务书一致）。
- **`authorization_matches_context` 比较项**逐条未变：`expired`、到期边界（`<=now`）、
  `missing_expiry(None/bogus)`、`env_mismatch`、`scope_mismatch`、`runtime_mismatch`、
  `empty_context(None/{})`、`ctx 缺 runtime 键`、`decision 缺属性`。

## 6. 独立判定：是否有消费方直接读 ReadinessSnapshot.runtime_instance_id 放行

**结论：没有。** 全 `src/` 检索 `.runtime_instance_id` 属性读取，`ReadinessSnapshot`
的该字段仅被自身 `as_dict()`（`entry_readiness.py:43`）与
`authorization_context()`（`:62`）读取；**无外部消费方**把它当放行依据。所有真实门禁读的是：
- `src/core/graph.py:580` `if getattr(gate,"new_entry_allowed",False): authorization_context=gate.authorization_context(...)`
- `src/core/graph.py:635` `gate_allowed = bool(getattr(gate,"new_entry_allowed",False))` + `blocking_reasons`
- `src/core/service.py:1188` `allow_new = readiness.new_entry_allowed`
- `src/core/release_evidence.py:1266` `bool(latest_entry.get("new_entry_allowed"))`
- dashboard 只读 `blocking_reasons` / 透传 dict（模板中**零** `runtime_instance_id` 引用）

且 `authorization_context()`（会回显该字段）在 `graph.py` 中**仅当 `new_entry_allowed` 为真**
才构造；patched 下哨兵令其为假 ⇒ 不构造。即便旁路构造，`authorization_matches_context`
两侧哨兵已返回 False。**⇒ 回显字段不构成授权凭据，无未闭合路径。**
（残留：`as_dict()["runtime_instance_id"]` 仍写 `"unknown"`，仅作观测展示，不是门禁输入。）

## 7. 门禁数字 / 无新增字面量 / 无新增 try-except

- `release_evidence.py` 两态 sha256 **相同** =
  `b550fc2ce8994d814cde86e1188ad565e562edae451193098245eed627e46c56`。
  门禁数字 `min_directional_candidates=200 / min_eligible_closed_trades=30 /
  min_observation_days=7.0 / min_observation_coverage=0.999 / max_observation_gap_seconds=120.0`
  一字未改（且该文件本轮未触碰）。
- AST 对比 `entry_readiness.py` 两态数值常量：**新增数值常量 = `[]`**；
  `Try` 节点数 base=3 / patched=3 ⇒ **无新增 `try`/`except`**。

## 8. 回归（全量 94 脚本两态 delta + quality + smoke）

沙箱按 L19：`git archive` 解包 → `git init && git add -A`（只暂存、不 commit）。
- base 沙箱（048033b）暂存 **918** 文件；patched 沙箱（31c003b）暂存 **938** 文件
  （+20 = FU3 补丁包 docs，见第 9 条）。
- **代码 head `7dd1053` 的 `git ls-files` 条数 = 918**（`31c003b` = 938）。
- 两态均 `OFFLINE REGRESSION OK scripts=94`，rc=0，**94/94 全部通过**。

逐脚本 delta（自家解析）：

| 集合 | 结果 |
|---|---|
| **NEW_FAIL** | `[]`（无） |
| **FIXED** | `[]`（无） |
| **CHANGED**（rc 同、摘要不同） | 2 项，均非语义 |

CHANGED 两项逐条解释（**均非代码行为变化**）：
1. `scripts/smoke_u10a_release_package.py`：摘要 `沙箱已跟踪文件 918 个,
   release_id=rel-cd1f800a…`（base）→ `938 个, release_id=rel-ae04b32c…`（patched）。
   差异纯粹来自**沙箱组成**（我的 patched 沙箱含 FU3 补丁包 docs 的 +20 文件），
   release_id 是包内容哈希，随之变化。**已确认该脚本两态都真跑到且 rc=0**。
2. `tests/integration/test_strategy_funnel.py`：仅因它是**最后一个**脚本，
   我追加到日志的 `BASE_RC=0` / `PATCHED_RC=0` 行被解析成其摘要，属解析产物。

聚焦检查（两态）：

| 检查 | base | patched |
|---|---|---|
| `scripts/run_offline_regression.py` | rc=0, scripts=94 | rc=0, scripts=94 |
| `scripts/check_quality.py` | `QUALITY CHECK OK files=304 untracked=0` rc=0 | 同左 rc=0 |
| `tests/acceptance/test_runtime_identity.py` | `U15 RUNTIME IDENTITY ACCEPTANCE PASS` rc=0 | 同左 rc=0 |
| `scripts/smoke_entry_readiness.py`（本轮最相关冒烟） | `PASS entry readiness: shared gate, missing evidence, expiry and context binding` rc=0 | 同左 rc=0 |
| `scripts/smoke_contracts_v3.py` | `PASS 173 / FAIL 0` rc=0 | 同左 rc=0 |

- `check_quality.py` 两态**逐字节相同** ⇒ **无新增 exclude**。
  `SECRET SCAN` 两态 `registered_false_positives=2`（未新增登记）。
- **两态守卫测试有效**：把 patched 版 `test_runtime_identity.py` 放到 **base src** 上跑，
  亦 `PASS`（走 guard 缺失分支，断言「复现缺陷」）⇒ 该测试确实是两态判定，不是恒真。
  （跑完已把 base 沙箱该文件还原为 tree_base 版本，且还原发生在回归跑到该脚本之前。）

## 9. 补丁矩阵 + 自足性

- 补丁包 `docs/dispatch/patches/U15-FU3/7dd1053/` 内 **无 `.py`**（仅 2 个 `.diff` + README + evidence）。
- `git apply --check` × 3 配置（DEFAULT / `core.autocrlf=false core.eol=lf` / `core.autocrlf=true`）
  均输出 `Checking patch src/core/entry_readiness.py...` 与
  `Checking patch tests/acceptance/test_runtime_identity.py...`，**rc=0，0 Skipped**。
- 实际 `git apply` 到 base 后与 patched 树逐文件比对（`tr -d '\r'` 归一化行尾）：
  - `src/core/entry_readiness.py` **SAME**（sha256 `300301c9…`）
  - `tests/acceptance/test_runtime_identity.py` **SAME**（sha256 `840df6b4…`）

## 10. 未能验证的部分

1. **真实 `TradingService` 全链端到端未跑**：受硬约束（demo 服务不得停、运行目录只读、
   不读 `.env`/`data/`），未启动/连接真实服务验证 `graph.py` 的
   `execution_node`/`authorize` 在实际运行中的放行。改为：代码级确认两处消费点均只读
   `new_entry_allowed`/`blocking_reasons`（`graph.py:580,635`），并以
   `decision_pipeline.filter_unauthorized_opens`（真实 broker 边界门禁）做端到端探针。
2. **`graph.py:635` 的 `_drop_opens` 执行前拦截**未动态触发（需重图运行），仅读代码确认。
3. **patch 包内 evidence 的其它日志**未逐字复核（只复核了 diff、apply 矩阵、
   ls-files、两态 tsv；probe/reg 日志为作者产物，我用自家探针与自家回归独立覆盖）。
4. **未验证** demo 服务当前仍在线（无端口探测，遵守只读与不打扰约束，未执行任何探测）。
5. 回归 CHANGED 项 `smoke_u10a` 的 `release_id` 语义未深究（判定为非语义：packaged
   文件数差异直接导致内容哈希不同，脚本 rc 仍 0）。

---

## 命令日志 (逐步追加)

- `git archive 048033b` → `_scratch_Q/tree_base`；`git archive 31c003b` → `_scratch_Q/tree_patched`
- `probe_q.py`（A/B/C/D/E/F 六组，注入 `MTA_TREE_SRC`，assert 模块归属树）两态 diff → 语义差异恰 3 条
- `probe_e2e.py`（`sign_execution` → `filter_unauthorized_opens`）两态 diff → 合法/误杀两态相同；仅哨兵行变化
- AST 扫描：第二名单=0、`entry_readiness.py` 代码哨兵字面量=0、调用点 8 语法/6 逻辑、新增数值常量=0、Try 3=3
- `sha256sum release_evidence.py` 两态相同
- 3 配置 `git apply --check -v` → 均 `Checking patch` ×2、0 Skipped、rc=0；实际 apply 后逐文件 SAME
- L19 沙箱两套全量 `run_offline_regression.py` → 94/94 rc=0；NEW_FAIL/FIXED 空；CHANGED 2（非语义）
- `check_quality.py` / `test_runtime_identity.py` / `smoke_entry_readiness.py` / `smoke_contracts_v3.py` 两态 rc=0

## 报告文件
`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15-FU3\R1\q\_scratch_Q\Q_U15-FU3_r1.md`

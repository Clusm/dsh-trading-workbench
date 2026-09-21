# U11 R1 — Q 独立验收报告

- 工作区：`C:/Users/Tao/Desktop/币安/mat-wt/reviews/U11/R1/q`（detached HEAD @ `00a49a8`，作者 head；已确认）
- 运行根 = 同目录（= `03967e9` + 2 篇 .md；`git diff 03967e9..00a49a8` 仅 2 个 docs 文件，`src/`、`scripts/`、`pyproject.toml` 0 行变更）
- 解释器：`C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`；`export PYTHONUTF8=1`
- 只读约束遵守：未写 `mat-wt/I` 或他人 worktree；未触碰 `multi-agent-trader` 运行目录；未读 `.env`/`data/`；未联网；未下单；产物仅在 `_scratch_Q/`

---

## 0. 结论

**`CHANGES_REQUESTED`**（2 项阻塞；另有 6 条非阻塞备注）

先讲清楚：**这份文档里所有"可实跑"的断言都是真的** —— §7 的 10 条命令我逐条重跑，exit code 与关键数字**全部逐字对得上**；§7 V1 的公开面基线我用自己的实现独立复现，**4 个模块的项数与哈希全部一致**。就"作者有没有跑过、有没有编数字"而言，答案是**没有编**。

但 U11 交付的是**接口映射**，它的价值取决于"映射是否完整、约束是否列全"。在这两条上文档有实质缺口：

- **阻塞 1**：首选候选 `core/decision_trace.py`（1799 行）的块划分**只覆盖文件前半**。§3.1 声称"内部已经天然分成**三块**互不依赖的逻辑"，实际文件有 ≥6 个块；**第 961–1799 行（839 行，占 46.6%）整块未被 §3.1 表格、也未被 §4.1 映射表提及**，涉及 **28 个公开名**（`Lease`/`LeaseRegistry`/`shared_quota_lease`/`ModelRouteGuard`/`SourceCache`/`background_refresh`/`background_refresh`/`EvidenceLink`/`run_protection_first`… 全部在 V1 的 86 名公开面里）。`strategy_funnel.py` 同样漏掉第 1804–2138 行（4 个公开名：`ManagementPriorityPlan`/`round_explanation`/`source_error_reason`/`DEFAULT_MANAGEMENT_RESERVE_RATIO`，均在 `__all__` 里）。**关键**：文档自己给的出口 V1（dir() 名集合不变）**检测不出这个漏项**——只搬前半、后半留在门面，V1 照样逐字通过。一个"漏了 47% 文件"的映射能通过它自己的验收。
- **阻塞 2**：§5"不动清单"（16 项）**没有任何一行是 `tests/`**。而本仓 `tests/integration/*`（12 个套件）才是行为断言的**真身**：12 个 `scripts/smoke_*.py` 只是**透传退出码的薄壳**（如 `scripts/smoke_u05_decision_trace.py:4` 明写"真正执行的是 `tests/integration/test_decision_trace.py`"）。§7 V2 的全部"行为回放"证据链落在这些文件上，§5 却只保护 `scripts/` 顶层外壳。先例就在仓里：`tests/integration/test_research_evidence.py:447` 的模块集断言用的是**包含**（`imported <= allowed_modules`）而非相等——正是 U06b/U07 那类"从相等改成包含"的弱化形态，改它不会让任何 smoke 变红。**同时**：文档对 `strategy_funnel`（U08 产物）**通篇未提 U08**，也没写"拆分后由谁的套件复跑谁签字"；对 `runtime_identity` 只要求 U15 作者确认**边界**（§3.3/§6），未点明其既有套件与复跑责任人。

> 即：**"作者跑过没有"= 通过；"映射完不完整 / 约束列全没有"= 不通过。**

---

## 1. §7"每阶段可验证出口"逐条重跑对照（任务第 1 条）

全部在 q 工作树（= 基线代码）实跑，`export PYTHONUTF8=1`。原始日志见 `_scratch_Q/<script>.log`。

| # | 命令 | 文档所写 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | `scripts/run_offline_regression.py` | exit **0**；`SMOKE MANIFEST OK included=81 excluded=1`；`OFFLINE REGRESSION OK scripts=85` | exit **0**；`SMOKE MANIFEST OK included=81 excluded=1`(L1)；`OFFLINE REGRESSION OK scripts=85`(L4698) | ✓ 逐字符合 |
| 2 | `scripts/check_quality.py` | exit **0**；`QUALITY CHECK OK files=301 untracked=0` | exit **0**；`QUALITY CHECK OK files=301 untracked=0`（另 `SECRET SCAN OK files=675 …`） | ✓ |
| 3 | `scripts/smoke_contracts_v3.py` | exit **0**；`PASS 173 / FAIL 0`；golden `160 (positive=45, negative=115)` | exit **0**；`结果：PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)` | ✓ |
| 4 | `scripts/check_execution_boundaries.py` | exit **0**；`PASS src 中 Broker.execute 仅由 graph/service 调用` | exit **0**；`PASS  src 中 Broker.execute 仅由 graph/service 调用` | ✓ |
| 5 | `scripts/smoke_u05_decision_trace.py` | exit **0** | exit **0**（PASS 115 / FAIL 0） | ✓ |
| 6 | `scripts/smoke_u05_cross_process_admission.py` | exit **0** | exit **0**（PASS 45 / FAIL 0） | ✓ |
| 7 | `scripts/smoke_u08_strategy_funnel.py` | exit **0**；`PASS 91 / FAIL 0` | exit **0**；`结果：PASS 91 / FAIL 0` | ✓ |
| 8 | `scripts/smoke_u02_strategy_position_manager.py` | exit **0** | exit **0**；末行 `U02 SMOKE OK` | ✓ |
| 9 | `scripts/smoke_u06a_shadow_isolation.py` | exit **0** | exit **0**（PASS 52 / FAIL 0） | ✓ |
| 10 | `scripts/smoke_u10a_release_package.py` | exit **0**；无 `BROKEN_LINK` | exit **0**；`BROKEN_LINK` 计数 **0**；`PASS 所有相对文档链接可达（独立解析，43 篇 md）` | ✓ |

**不符项：无。** 10/10 条 exit code 与关键输出全部与文档一致。

附带说明（非发现）：
- 第 8 条 `smoke_u02` 内部三个子段打印 `PASS=158/FAIL=0`、`PASS=156/FAIL=2`、`PASS=150/FAIL=8`；后两段标题是 `[2/3] 负向对照 N1`、`[3/3] 负向对照 N2`，即"断言某些检查**必须失败**"，故 FAIL 计数是设计值，整体 `U02 SMOKE OK` 且 exit 0。文档只断言 exit code，成立。
- 第 10 条顺带证实新文档 `docs/重构接口映射.md` 的 Markdown 相对链接在构建期**可达**（43 篇 md 全解析，0 断链）。

---

## 2. §7 V1 公开面快照的独立复现（任务第 2 条）

### 2.1 按文档原样重跑 —— 完全一致

```
core.decision_trace: names=86 surface_sha=1e96c448b313
core.strategy_funnel: names=158 surface_sha=9ec72c643e4b
core.runtime_identity: names=117 surface_sha=9616373d6147
core.contracts: names=118 surface_sha=8172c604e1b2
```

**4/4 项数与哈希与文档逐字相同，exit 0。** 算法（`sorted(n for n in dir(m) if not n.startswith('_'))` 后 `sha256('\n'.join(...)).hexdigest()[:12]`）与文档给定的一致。

### 2.2 稳健性自测（我另做的，文档未做）

- **导入顺序无关**：把 4 个模块按**逆序** import，4 个哈希不变。
- **隔离无关**：每个模块在**独立新进程**单独 import，4 个哈希不变。
→ 该快照**可复现、可用于后续比对**，作者没有算错。

### 2.3 口径判定：**基线可复现，但"公开面"的定义比文档说的更宽**

我进一步把 `dir()` 结果与模块**自己声明的 `__all__`** 对照：

| 模块 | `__all__` 项数 | `dir()` 公开名 | 差值来源 |
|---|---|---|---|
| `core.decision_trace` | **61** | **86** | 25 个是**偶然导入**：`Any/Callable/Iterable/Iterator/Mapping/Path/Sequence/StrEnum/annotations/contextmanager/ctypes/dataclass/field/hashlib/json/math/os/time` + 从 `core.contracts` **透传**的 `DecisionCounters/DecisionTraceSpan/SpanValidationError` + `DEADLINE_TOLERANCE_MS` |
| `core.strategy_funnel` | **71** | **158** | 87 个是偶然导入 + 未进 `__all__` 的 `DETAIL_*` 等常量 |
| `core.contracts` | **111** | **118** | 7 个子模块名（`common`、`k01_identity`…） |

结论与判定：
1. **作者没算错**，基线真实、可复现 —— 该工具**能**当回归探测器用。
2. 但它**不等于"公开面"**：`dir()` 把"顺手 import 进来的 stdlib 名"也算进"公开面"。文档 §4.1 写"`core.decision_trace` 的 `dir()` 公开名集合逐字不变"，**字面上**就要求连未使用的 `ctypes/math/json` 也留在门面里——**拆分时若实施者按 ruff 清掉门面里已不再使用的 import，V1 会假红**。
3. **反向盲区**：`__all__` 以 `_` 开头被过滤，V1 **完全不检查 `__all__`**。若拆分后门面忘了维护 `__all__`（或把 `__all__` 搬去了子模块），`from core.decision_trace import *` 的语义已变，而 **V1 逐字通过**。
4. 口径差异结论：**"作者错了"不成立，属"口径不同且文档未言明"**。建议实施者在 V1 里补一行 `__all__` 对比，或明确写死"本快照口径 = dir() 可见名（含偶然导入），实施时门面须保持 import 集合不变"。

---

## 3. §5"不动清单"的独立枚举与漏项（任务第 3 条）

### 3.1 文档 §5 项数与任务清单项逐条核对

`sed -n '206,224p' | grep -c '^| '` = 17 = 1 表头 + **16 数据行** ✓，与"16 项"一致。任务列举的应核对项：

| 任务点名的应保护项 | 文档是否覆盖 | 位置 |
|---|---|---|
| `src/core/contracts/*` + tag `contract-v3.0` | ✓ | §5 行1、§2 C1 |
| `docs/contracts/golden/*`（160 条） | ✓ | §5 行2、§2 C1 |
| `CONTRACT-ERRATUM-1` 先例 / 路径键守卫 | ✓（**实质覆盖，未点名文件**）| §2 C2；`k01_identity.py` 由 §5 行1 的 `k01`–`k06` 通配覆盖 |
| `release_evidence.py` + `shadow_status.release_gate_role` 200 分母单一口径 | ✓ | §5 行3/行4、§2 C4 |
| `ops/recovery.py` + `service.py` 账本事实注入、`before_entry(facts=)` **替换**语义 | ✓ | §5 行5/行6、§2 C6 |
| `U13FU3-INJECTION-TRUST` 纵深防御 | **△ 实质覆盖、未点名** | 简称未出现；C6 已写"纵深防御项" |
| `scripts/` 顶层**所有** `smoke_*.py`（glob 强制） | ✓ | §5 行9、§2 C3 |
| `run_offline_regression.py` 的 `SCRIPTS` 清单 | ✓ | §5 行8 |
| `pyproject.toml` ruff `per-file-ignores` / 禁新增 exclude | ✓ | §5 行10、§2 C7 |
| `docs/archive/*` + `archive/README.md` 规则3 已被证伪 | ✓ | §5 行15、§2 C8 |
| `docs/dispatch/*` | ✓ | §5 行16 |
| **`tests/acceptance/*`、`tests/integration/*` 跨任务共享断言** | **✗ 完全未列** | — |

### 3.2 我独立枚举出的**漏项**（文档 §5 未写、但我认为必须写）

**M1（阻塞级）——`tests/` 整体缺席 §5。** 文档全文 `grep "tests/"` **0 命中**。事实：
- `tests/integration/test_strategy_funnel.py`（881 行）是 `core.strategy_funnel` 的**主要消费者**（`import core.strategy_funnel as funnel_mod` + 直接 `from core.strategy_funnel import (...)` 多名导入）；
- 12 个 `scripts/smoke_*.py` 是**透传退出码的薄壳**（`scripts/smoke_u05_decision_trace.py:4`、`scripts/smoke_u02_strategy_position_manager.py:4` 明写真身在 `tests/`）；
- §7 V2 的"行为套件"实际跑的就是这些 `tests/` 文件。

**为什么这是实质缺项**：只要实施者把 `tests/integration/test_*.py` 的 import 改指向新子模块（"看起来更整齐"），**smoke 外壳仍 exit 0，V2 静默通过**，但"门面等价"这一被验证对象已被实施者改掉——自证陷阱。仓内**已有先例**：`tests/integration/test_research_evidence.py:447` 用 `imported <= allowed_modules`（**包含**，非相等）做模块集断言，正是"从相等放宽为包含"那类不报错的弱化。**必须**补一行"`tests/integration/*`、`tests/acceptance/*` 不动；既有测试的 import 与断言一行不改"。

**M2（阻塞级）——责任归属未写清。**
- 文档**通篇未提 U08**。`strategy_funnel.py` 是 U08 产物（任务清单 JSON L1977–1979 登记其交付面含 `tests/integration/test_strategy_funnel.py`、`src/core/strategy_funnel.py`），文档 §3.2 只说"保留兼容门面让既有消费者一行不改"，**没写**拆分后由 U08 作者复核/复跑其 881 行套件。
- `runtime_identity`（U15 产物）**较好**：§3.3/§4.3/§6 三处要求"落地前必须由 U15 作者书面确认边界""没有 U15 作者现场确认，不动"。**但仍未点名** U15 的既有套件是哪个、拆分后谁复跑签字。
- §6 只有笼统的"I 单独派审查（A、B、C、Q 中最少两位）"，**没有把"原任务作者对其套件签字"写成前置条件**。

**M3（低）——`docs/策略扫描原因码.md` 未列。** 该文档是 U08 交付面的一部分（任务清单 JSON L2164 与 `strategy_funnel.py` 同批冻结），且 P1/P2 要搬的正是原因码/明细码表；它不进"不动清单"，搬到一半容易被"顺手同步"改写。

**M4（低）——`knowledge/*.json` 未列。** §4.3 自己写"策略卡来源…与 `knowledge/*.json` 对账"，但 §5 无此条。

**M5（低）——`scripts/check_quality.py`、`scripts/create_release_package.py` 未列。** 它们是 C7/C8 所述行为的**宿主**（ruff 豁免、`doc_links_resolve`、`EXCLUDED_PATH_PREFIXES`），属"改了就动门禁语义"的文件。

**M6（信息）——`U13FU3-INJECTION-TRUST` 未点名。** 该项在任务清单 JSON L4512/L4758 真实登记，内容正是"`before_entry(facts=...)` 会替换自身账本读取，故安全性依赖注入诚实；唯一生产注入点 `service.py:988`；建议改 `max(injected, live)`"。文档 §2 C6 覆盖了同一实质，但未引用该编号。**另需提醒**：该未决项未来若要加固 `recovery.py`，将与本文档"`recovery.py` 绝对不动"产生张力，建议在 §5 或 §8 注明。

### 3.3 一个反向核对（§5 里的数字）

§5 行9 写"`scripts/` 顶层 `smoke_*.py`（**81 个**）"。实测顶层 `ls scripts/smoke_*.py | wc -l` = **82**。"81"是**清单纳入数**（`included=81`），另有 `scripts/smoke_p0c.py` 被 `EXCLUDED_SMOKE_SCRIPTS` 排除。保护意图（"不移动、不改名"作用于全部顶层 smoke）是对的，但**计数口径写成了 included 却表述为存在总数**，差 1。§2 C3 写的 `included=81 excluded=1` 是对的。

---

## 4. 事实性抽查（任务第 4 条）

抽了 6 组共 15 处具体数字，逐一自查：

| 文档断言 | 实测 | 判定 |
|---|---|---|
| `decision_trace.py` 1799 行 / `strategy_funnel.py` 2138 / `runtime_identity.py` 2464 / `service.py` 1826 | 1799 / 2138 / 2464 / 1826 | ✓ 全对 |
| `contracts/__init__.py` 公开 118 个符号 | V1 实测 118 | ✓ |
| §3.2 "`from core.decision_trace import` 15 个符号，第 60 行" | `strategy_funnel.py:60` 起 15 名（9 `REASON_*` + `REASON_CODES` + `REASON_ORDER` + 4 名） | ✓ |
| §3.2/G "`from core.strategy_position_manager import PositionClass`，第 81 行" | 第 **81** 行 | ✓ |
| §3.1 "`decision_trace.py` 第 64 行整块导入 contracts" | 第 **64** 行 | ✓ |
| §3.4 "`service.py` `build_execution_feedback`，第 65 行" | 第 **65** 行 | ✓ |
| §2 C6 "`recovery.py:970` `before_entry`" | 第 **970** 行 `def before_entry` | ✓ |
| §2 C6 "`service.py:988` 调用点" | 第 **988** 行 `gate.before_entry(facts=ledger_facts)` | ✓ |
| §3.1 decision_trace 扇出 8 模块 | `grep` src 命中**恰好这 8 个** | ✓ |
| §3.4 "service.py 20+ 直接依赖" | 46 条顶层 import，跨 7 个顶层包 | ✓（保守） |
| §3.1 块边界 107/210/446/546/586 | 分别 = `UnknownReasonCode` / `_status_code_of` / `DeadlineExceeded` / `span_absolute_deadline_ms` / `Stage` | ✓ |
| §3.1 `SCHEMA_VERSION="u05-decision-trace-1"`、`PERCENTILE_METHOD` | 第 72/76 行，值一致 | ✓ |
| §4.1/§4.2 契约透传 5 名（`DecisionTraceSpan` 等）确在 `decision_trace` 公开面 | ✓ 全在 | ✓ |
| `docs/dispatch/patches/`、`reviews/` 在 `EXCLUDED_PATH_PREFIXES` | `create_release_package.py:137-138` 确有 | ✓ |
| §5 表 16 项 | 16 数据行 | ✓ |

**发现 2 处行号/区间不准（均为非阻塞，但会误导实施者按行搬移）：**

1. **§3.1 "107–208 原因码" 区间起点错。** 该块真身从**第 78 行**的 `# --- 原因码` 分隔开始：9 个 `REASON_*` 字面量在 **80–88**，`REASON_ORDER` 在 **93–104**（`REASON_CODES` 104），`UnknownReasonCode` 才在 107。文档把块的起点写成 107，**恰好把它紧接着声明为"冻结语义"的 `REASON_ORDER` 顺序字面量漏在区间外**。§4.1 映射表里这 11 个名字是有的（用 `REASON_NO_EDGE…REASON_CHANNEL_CONFLICT（9 码）` 概括），所以约束没丢；但 §3.1 的行区间表若被当搬移清单用，会漏搬 REASON 常量。
2. **§3.1/§4.1 未覆盖 `decision_trace.py` 第 961–1799 行**（与阻塞 1 同一件事的另一面，此处的具体数字）：该段含顶层定义 `run_protection_first`(978)、`RouteRisk`(1052)、`RouteViolation`(1062)、`RouteExpectation`(1073)、`ModelRouteGuard`(1089)、`RouteViolationError`(1240)、`credential_alias`(1252)、`shared_admission_scope`(1257)、`coordination_dir`(1262)、`lock_path_for`(1270)、`owner_alive`(1275)、`Lease`(1311)、`LeaseRegistry`(1333)、`shared_quota_lease`(1457)、`SourceName`(1471)、`SOURCE_NAMES`/`SOURCE_TTL_MS`、`RefreshBudget`(1489)、`SourceCache`(1537)、`BackgroundRefreshOutcome`(1590)、`background_refresh`(1615)、`EvidenceLink`(1696)、`evidence_links`(1721)，另有 `__all__`(≈1730 起)。**28 个均在我实测的 86 名公开面内**（我用 `dir()` 逐一验证：28/28 命中，0 缺失）。
   对应地 **§3.2 未覆盖 `strategy_funnel.py` 第 1804–2138 行**：`DEFAULT_MANAGEMENT_RESERVE_RATIO`(1807)、`ManagementPriorityPlan`(1811)、`source_error_reason`(1966)、`round_explanation`(1978)，**4/4 在 158 名公开面内且均在 `__all__`**。

---

## 5. 文档自身链接与门禁（任务第 5 条）

| 检查 | 结果 |
|---|---|
| `smoke_u10a_release_package.py`（构建期链接可达） | exit **0**；`BROKEN_LINK` 计数 **0**；`PASS 所有相对文档链接可达（独立解析，43 篇 md）`。新文档的 `contracts/README.md`、`../pyproject.toml`、`archive/README.md` 三处相对链接全部可达 ✓ |
| `check_quality.py` 是否新增 exclude | ✗ 未新增：`pyproject.toml` 在 `03967e9..00a49a8` **0 行变更**，`extend-exclude` 仍为 `.venv/data/logs`；`per-file-ignores` 仍只有 `"scripts/*.py" = ["E402"]` ✓ |
| `run_offline_regression.py` 一行未改 | `git diff 03967e9..00a49a8 -- scripts/run_offline_regression.py` = **空** ✓（该提交 `-- scripts/check_quality.py pyproject.toml src/` 亦为空） |
| 全量变更面 | 仅 `docs/重构接口映射.md`、`docs/dispatch/patches/U11/00706d5/README.md`；**0 个共享文件补丁** ✓ |
| 未跟踪 Python 文件（会不会给 check_quality 添乱） | `_scratch_Q/` 只有 `.md`/`.log`，无 `.py`，故 `untracked=0` 未被我的验收污染 ✓ |
| 新文档未进 `docs/文档索引.md` | `grep -c` = **0**，与补丁包 README 的"有意不加"自述一致；`create_release_package.py` 内**无** "孤儿文档/必须被索引" 类机器校验，故不构成门禁违规 —— 仅**可发现性**变差（低） |

---

## 6. 我未能验证的部分（如实登记）

1. **V3（磁盘/持久化格式不变）未实跑**：文档 §8.2 已自认。我同样无法跑——它要求"新旧两份实现同输入对比 `TicketLedger` 落盘 JSON"，而拆分尚未发生，没有"新实现"可比。
2. **拆分后的 V1/V2 等价性未验证**：文档 §8.1 已自认。本文只能证明**基线可复现**，不能证明"拆分后照旧"。
3. **动态引用（`getattr`/字符串反射）未清查**：文档 §8.3 已自认仅做静态 import 扫描。我延续其口径，**未**对 `decision_trace`/`strategy_funnel` 做反射用法全清查（这属实施前动作）。
4. **`runtime_identity.py` 的块边界**：文档 §4.3 自称不完整映射、需 U15 作者确认；我**未**独立判定其内部分块是否内聚（超出 U11 R1 范围，且需 U15 领域知识）。
5. **`CONTRACT-ERRATUM-1` 的完整语义**：我只核实了 `_structured_path_mapping()` 位于 `src/core/contracts/k01_identity.py:117`（而非文档 C2 措辞所指的 `common.py`），**未**重演该事故、也未核验 `k01`–`k06` 内其他守卫是否同样敏感。
6. **`service.py:988` 的 `except TypeError → before_entry()` 回退分支**：我注意到该回退（注释"门禁尚未支持 facts 注入（补丁未合入）"）与 C6"替换语义"的精确边界值得讨论，但**未**追查它是否构成 fail-open 路径（属 U13FU3 范围，非 U11）。
7. **`tests/` 各套件内部断言的详尽清单**：我只抽查到 `test_research_evidence.py:447` 的"包含式"模块集断言作为先例，**未**逐一审计 12 个 `tests/integration/*` 的断言强度。
8. **U08/U15 的套件所有权**：来自任务清单 JSON 的登记字段（L1977–1979 等），**未**交叉核对 `docs/dispatch/RESUME.md` 的集成台账（该文件不在我工作树可读范围内被要求）。

---

## 7. 建议的最小修改（供作者选择）

**必须（阻塞）：**
1. 在 §3.1/§4.1 补齐 `decision_trace.py` 的**第二半**（961–1799）分块与归属，或至少**显式声明**"该段留在 `decision_trace` 门面不动、不属 P1 拆分范围"——二者任一都比现在的沉默好，因为沉默 + V1 通过 = 实施者可以合法地只搬前半。同时补 `strategy_funnel.py` 1804–2138 段。
2. §5 增列一行：**`tests/integration/*`、`tests/acceptance/*` 不动；既有测试的 import 与断言一行不改**（并在 §11 或 §6 写明：`strategy_funnel` 的复跑责任归 **U08** 作者，`runtime_identity` 归 **U15** 作者，须对各自既有套件签字）。

**建议（非阻塞）：**
3. 在 §7 V1 旁注明口径：`dir()` 可见名（含偶然导入与契约透传），并**补 `__all__` 对比**（61/71/111 三种口径已在 §2.3 量化）。
4. 修正 §3.1 的"107–208"为真实起点（≈78），避免漏搬 `REASON_ORDER`。
5. 修正 §5 行9 "81 个"为"顶层 82 个（清单纳入 81 + 排除 1）"。
6. 在 §8 未验证清单补一条"§3 块划分是否覆盖全文件"，并在 C2 里点名 `k01_identity.py`。
7. 在 C6 或 §5 点名 `U13FU3-INJECTION-TRUST`，并注明与"`recovery.py` 不动"的潜在张力。

---

## 8. 报告文件路径

- 本报告：`C:/Users/Tao/Desktop/币安/mat-wt/reviews/U11/R1/q/_scratch_Q/Q_U11_r1.md`
- 原始日志：`_scratch_Q/offline.log`、`_scratch_Q/check_quality.log`、`_scratch_Q/smoke_contracts_v3.log`、`_scratch_Q/check_execution_boundaries.log`、`_scratch_Q/smoke_u05_decision_trace.log`、`_scratch_Q/smoke_u05_cross_process_admission.log`、`_scratch_Q/smoke_u08_strategy_funnel.log`、`_scratch_Q/smoke_u02_strategy_position_manager.log`、`_scratch_Q/smoke_u06a_shadow_isolation.log`、`_scratch_Q/smoke_u10a_release_package.log`、`_scratch_Q/v1_doc_algo.log`

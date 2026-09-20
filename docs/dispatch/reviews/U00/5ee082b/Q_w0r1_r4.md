# U00 契约候选 r4 独立复审报告（Q / w0r1）

- 会话：Q-w0r1（独立 QA 会话，r4 对抗性复验）
- 复审对象：候选提交 `5ee082b`（`fix(contracts): close r3 P2 findings and complete golden changelog (U00 r4; I recovery commit after tool abort; k02/k03 schemas regenerated; smoke PASS 173/0)`）
- 对照基线：r3 候选 `b36d8a9` 及三份 r3 报告 `docs/dispatch/reviews/U00/b36d8a9/{Q,C,B}_w0r1_r3.md`（作为"必须关闭项"的权威清单）
- 规格基准：`docs/contracts/README.md`（§3–§8 字段/判定规则、§10 版本与兼容规则、§11 golden changelog、§12 已知限制 L1–L15 + 下游缺口索引）
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1`（detached @ `5ee082b`）
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`）
- 边界：未修改任何 tracked 文件（`git status --porcelain` 仅 `?? U00_review_Q_w0r1*.md`、`?? _review_scratch/`）；未读写运行目录 `multi-agent-trader/` 的 `data/`、`.env`、凭证、账户标识；未联网；未启停服务；未提交/推送/打标签/切换分支；未改动 `mat-wt/I`。全部产物落在 `_review_scratch/`
- 本轮脚本/原始输出：`r4_boot.py`、`r4_probe_q_p2.py`(+`_out.txt`)、`r4_probe_q_p3_cb.py`(+`_out.txt`)、`r4_probe_new.py`(+`_out.txt`)、`r4_schema_recheck.py`(+`_out.txt`)、`r4_golden_diff.py`(+`_out.txt`)、`r4_smoke_out.txt`、`r4_manifest_out.txt`、`r4_rules_crash.txt`、`r4_more_edge.txt`、`r4_dropped_edge.txt`、`r4_new_cases.txt`

## 0. 结论

**CHANGES_REQUESTED**（P0 = 0，P1 = 0；r3 遗留 P2 ×1 未完全关闭 + r4 新引入 P2 ×2，**均未在 README §12 登记**；另有 P3 ×4）

- r3 逐条处置：**CLOSED 12 / PARTIAL 1 / NOT CLOSED 3**（16 行判定，见 §2；Q P3-3 与 B N14 为同一缺陷的两次报告）
- r4 修得**扎实**：Q P2-2/3/4/5/6、C NR-1/2/3、B N12 全部可独立复现为结构化拒绝或结构化抛错，无一是"崩溃冒充拒绝"；`rules` 由"应传"变为"必传"（`RULES_MISSING`）、`dropped_input_ids` 落地、`ZeroFeeProof.coverage_hash` 窗口绑定、K04 空串按 strip 后判定、`ManagementIntent.environment` 空串直接 `ValidationError`，方向与 r3 建议逐字对应
- 独立复算：smoke `PASS 173 / FAIL 0`、golden `160 (positive=45, negative=115)`、13 条新用例全部实际执行；6 个 schema 由 `model_json_schema()` 独立重生成 **6/6 EQUAL**；期望值变更 **0 条**（无静默改期望）；smoke manifest `OK included=65 excluded=1`
- 阻断理由：① **新回归（P2，崩溃未登记）**：`rules` 为 Mapping 但含非 JSON 值或非 str 键时，`build_bundle`/`validate_bundle` 抛裸 `TypeError`（r3 同类项 K01 `AttributeError` 被判 P2 并已修复，标尺一致）；② **r3 P2-1 的残留未关闭（P2，fail-open 未登记）**：手工构造 `ZeroFeeProof(provable=True, coverage_hash=<正确哈希>)` 仍可跳过 TerminationProof 等全部零费用条件并得 `COST_COMPLETE`
- 次要：README 在 r4 **一字未改**，但 r4 新增了 reason code（`RULES_MISSING`/`ZERO_FEE_PROOF_MISMATCH`）、新模型字段（`EvidenceBundle.dropped_input_ids`）、新覆盖口径（`组.unknown_field:<键>`）并把 `rules` 变为必传（§10 要求"收紧判定必须登记为小版本行为变更"）；§11 changelog 对 36 条 K02 既有用例 + `K03-P5` 的 inputs 变更零登记（Q P3-3 / B N14 未关闭且范围扩大）

## 1. 复算基线（真实输出）

| 项目 | 命令 | 原始结果 |
|---|---|---|
| 契约冒烟 | `PYTHONUTF8=1 <venv>/python.exe scripts/smoke_contracts_v3.py` | `结果：PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)`；`grep -c "^  PASS" → 173`、`"^  FAIL" → 0`；退出码 0（`r4_smoke_out.txt`） |
| 13 条新用例是否真跑 | `grep "PASS  <case_id> "` 逐条 | K01-N6、K02-N31..N35/P8、K03-N20..N22、K04-N28..N30 **各命中 1 次** |
| schema 独立重生成 | `_review_scratch/r4_schema_recheck.py`（`model_json_schema()` ×6 `==` 仓库文件） | `k01..k06: EQUAL` ×6；`ALL_EQUAL`（`r4_schema_recheck_out.txt`） |
| smoke manifest | 导入 `scripts/run_offline_regression.py` 调 `_validate_smoke_manifest()` | `SMOKE MANIFEST OK included=65 excluded=1` |
| golden 逐用例比对 | `_review_scratch/r4_golden_diff.py`（`b36d8a9` → `5ee082b`，六文件） | `added` 13 条、`removed` 0、**`expect_changed` []**、`inputs_changed` 36 条（K02 ×35 + K03-P5）、`total=160 (45/115)` |
| 工作区完整性 | `git rev-parse HEAD` / `git status --porcelain` | `5ee082b...`；仅 3 个未跟踪审查报告 + `_review_scratch/` |

## 2. r3 发现逐条判定

判定口径：**CLOSED = 我方探针得到结构化拒绝/结构化抛错**；PARTIAL = 命名缺陷已闭合但同类面仍可达；NOT CLOSED = 仍可复现或登记未做。

### 2.1 Q r3 P2（6 项）

| 编号 | 问题 | 判定 | 原始证据（探针 + 逐字输出） | 实现位置 |
|---|---|---|---|---|
| NEW-R3-P2-1 | K03 `finalize_costs` 接受自报 Mapping 作零费用证明；证明未绑定窗口可跨窗口复用 | **PARTIAL** | 命名缺陷两条均已闭合：`r4_probe_q_p2_out.txt` → `P2-1a dict self-report {'provable':True}: ... reason_codes=('ZERO_FEE_NOT_PROVABLE',)`；`P2-1b dict self-report WITH correct coverage_hash: ... ('ZERO_FEE_NOT_PROVABLE',)`；`P2-1d proof minted for window B reused on window A: ... ('ZERO_FEE_PROOF_MISMATCH',)`；`P2-1f ZeroFeeProof(provable=True) without coverage_hash: ('ZERO_FEE_PROOF_MISMATCH',)`；正向对照 `P2-1e ... : ('COST_COMPLETE',)`。**残留（fail-open）**：`P2-1c hand-built ZeroFeeProof(provable=True, hash=hash(cov_a)): cost_status='COMPLETE' ... ('COST_COMPLETE',)` —— 见 §3 NEW-R4-P2-2 | `k03_income.py:619`（`proof = zero_fee if isinstance(zero_fee, ZeroFeeProof) else None`）、`:649-650`（`coverage_hash` 比对）、`:261-265`、`:323` |
| NEW-R3-P2-2 | K02 `bound_inputs=[]` 整体禁用越界检查 | **CLOSED** | `r4_probe_q_p2_out.txt`：`P2-2 CLOSE bound_inputs=[] + auth + claim refs in-01/in-02: ('EVIDENCE_INVALID', ['CLAIM_OUTSIDE_BOUND_INPUTS', 'MISSING_REQUIRED_CLAIM'])`；REDUCE/ADJUST 同码；`CLOSE bound_inputs=[' ']` → 同码；`OPEN bound_inputs=[]` → `MISSING_EVIDENCE_BINDING`（兜底仍在） | `k02_evidence.py:998-1002` |
| NEW-R3-P2-3 | K02 允许 `symbol=None` 幽灵输入并被 claim 引用 | **CLOSED** | `P2-3 validate_bundle with symbol=None input: (False, ['INPUT_SYMBOL_MISMATCH', 'QUALITY_MISMATCH'])`；`P2-3 OPEN intent bound_inputs=['ghost'] ... : ('EVIDENCE_INVALID', ['INPUT_SYMBOL_MISMATCH', 'QUALITY_MISMATCH', ...])` | `k02_evidence.py:561-562`（`if item.symbol is None`）、`:388-389`、`:478-479` |
| NEW-R3-P2-4 | K04 空串 `runtime_instance_id`/`environment` 被当作已绑定 | **CLOSED** | `P2-4 auth(rid='', env='') + request w/o bindings -> (False, ['RUN_GENERATION_UNBOUND', 'ENVIRONMENT_UNBOUND'])`；`rid='   ', env='  '` 同码；`rid='', env='demo'` → `['RUN_GENERATION_UNBOUND','ENVIRONMENT_MISMATCH']`；请求携带 `''` → 同样拒绝；控制组 `rid='inst-1',env='demo'` + 匹配请求 → `(True, [])`；`ManagementIntent(environment=''/'   ')` → `ValidationError`（`environment must be non-empty when provided`） | `k04_management.py:258-272`、`:107` |
| NEW-R3-P2-5 | K05 `UNKNOWN -> ACCEPTED` 跳过 SUBMITTED 事实 | **CLOSED** | `P2-5a UNKNOWN + receipt -> transition ACCEPTED: RAISED TransitionError: ACCEPTED requires prior SUBMITTED fact`（`k05_trace.py:385`）；`P2-5e UNKNOWN no receipt -> ... TransitionError: ACCEPTED requires provider_receipt`；`P2-5f UNKNOWN+receipt -> DELIVERED ... requires prior SUBMITTED fact`；控制组 `P2-5c`（带 submitted）→ `('ACCEPTED', 1789862399000)` | `k05_trace.py:383-385` |
| NEW-R3-P2-6 | K01 `build_identity` 对非 Mapping 覆盖输入抛裸 `AttributeError` | **CLOSED** | `r4_probe_q_p2_out.txt`：`model_route=list/str/int`、`effective_config=list/str/int/None`、`model_route=None` → 全部 `IdentityCoverageError`，`groups=['model_route.*']` / `['effective_config.*']`；`model_route blank model/effort` → `groups=['model_route.model','model_route.effort']`；`junk extra key` → `groups=['model_route.unknown_field:junk_field']`；`ModelRoute(model='', effort='')` → `ValidationError` | `k01_identity.py:150-157`（`_field_gap`）、`:180-204`（`_coverage_gaps`）、`:69-74` |

### 2.2 Q r3 P3（3 项）

| 编号 | 问题 | 判定 | 原始证据 | 实现/文档位置 |
|---|---|---|---|---|
| NEW-R3-P3-1 | K05 `value`/`satisfied` int↔bool 混淆（`True == 1`） | **CLOSED** | `r4_probe_q_p3_cb_out.txt`：`value=True, satisfied=1: recovered=False reasons=['EVIDENCE_TYPE_INVALID']`；反向 `value=1, satisfied=True` 同；`value=False, satisfied=0` 同；`value='true'`、`satisfied=2` 同；纯粹单字段 `value=True only`/`satisfied=True only` → `recovered=True`（与 README §7"显式 `{"observed_at_ms": int, "value": bool}`"的单字段宽容语义一致，非本轮问题） | `k05_trace.py:242-247` |
| NEW-R3-P3-2 | K02 `quality.dedup_removed` 不在重算范围，可任意改写 | **CLOSED** | `P3-2 dedup_removed=['in-dup'] / ['in-01'] / ['not-a-real-input']: valid=False reasons=['QUALITY_MISMATCH']`；控制组 `dedup_removed=[]: valid=True`；`dropped_input_ids=('in-01',)`（真实保留输入）→ `QUALITY_MISMATCH`；`('',)`/`('x','x')` → `QUALITY_MISMATCH`；重复内容 bar 建包 → `dropped_input_ids: ['in-dup']`、`quality.dedup_removed: []`、包仍 valid | `k02_evidence.py:499`（`_expected_quality` 产出 `dedup_removed: []`）、`:606`（纳入比对循环）、`:575-585`（`dropped_input_ids` 形态校验）、`:860` |
| NEW-R3-P3-3 | golden changelog 漏登 inputs-only 变更（K01-P3/P4/P5、K03-N5） | **NOT CLOSED** | `P3-3 README contains 'K01-P3': False`、`'K01-P4': False`、`'K01-P5': False`、`'K01 全部': False`；`git diff --stat b36d8a9 5ee082b -- docs/contracts/README.md` → **空（README 未改）**；`git diff b36d8a9 5ee082b --name-only` 不含 README.md。且 r4 自身把 **36 条既有用例** inputs 改了（`r4_golden_diff_out.txt: inputs_changed = K02-N1..N30,N31 族共 35 条 + K03-P5`，均为新增 `rules` 键），登记为零 | `docs/contracts/README.md:262-276`（changelog 表，r4 未追加行） |

### 2.3 C r3 P2（3 项 + 1 项 r1 残余）

| 编号 | 问题 | 判定 | 原始证据 | 实现位置 |
|---|---|---|---|---|
| C NR-1 | `dedup_removed` 不在重算/比对范围，可任意改写 | **CLOSED** | 同 §2.2 P3-2；另 `dropped_input_ids` 走 `QUALITY_MISMATCH`、重复键/空串/与保留输入相交均拒绝（`r4_probe_q_p3_cb_out.txt`） | `k02_evidence.py:499/575-585/606/860` |
| C NR-2 | `rules=None` 默认值下策略字段可洗白 | **CLOSED** | `r4_probe_q_p3_cb_out.txt`：`NR-2 validate_bundle(rules=None (default)): valid=False reasons=['RULES_MISSING']`；`rules=[]`/`'x'`/`{}` → `['RULES_MISMATCH']`；`validate_intent(rules=None): status=EVIDENCE_INVALID reasons=['RULES_MISSING', ...]`；`rules=RULES` → `['RULES_MISMATCH', ...]`（篡改被拦） | `k02_evidence.py:648-651`、`:86` |
| C NR-3 | `build_identity` 强制覆盖与 `validate_coverage` 空值语义不一致（空串/额外键可绕 ModelRoute） | **CLOSED** | `NR-3 model_route all-blank strings: IdentityCoverageError groups=['model_route.model', 'model_route.effort']`；`junk extra key` → `['model_route.unknown_field:junk_field']`；`effective_config extra key` → `['effective_config.unknown_field:junk']`；`missing group` → `['effective_config.profile_overrides','effective_config.environment_overrides']`。**残余（P3，仅记录）**：`validate_coverage` 仍不报 unknown 键（`NEW-D validate_coverage(payload with junk key): ['dependency.requirements_lock','dependency.comparison_status','strategy.strategy_cards']`），即 `build_identity` 比 `validate_coverage` 严，方向 fail-closed | `k01_identity.py:180-204` |
| C P2-6（r1 残余） | K01 常量 vs 文件清单：`STRATEGY_SOURCE_FILES` 仅作标签 | **NOT CLOSED** | `STRATEGY_SOURCE_FILES`（`k01_identity.py:23`）在 `_coverage_gaps` 中**零引用**（`:172` 只用 `DEPENDENCY_SOURCE_FILES[0]`）；r3→r4 该处无 diff（`git diff b36d8a9 5ee082b -- src/core/contracts/k01_identity.py` 不含该段） | `k01_identity.py:23,166-176`；README §12 无对应行 |

### 2.4 B r3 P2 / 相关 P3

| 编号 | 问题 | 判定 | 原始证据 | 实现位置 |
|---|---|---|---|---|
| B N12（P2） | 空串绑定使 UNBOUND 检查落空 | **CLOSED** | 同 §2.1 P2-4（授权侧 + 意图侧双闭合） | `k04_management.py:258-272`、`:107` |
| B N13（P3） | `validate_authorization` docstring 与实现矛盾 | **CLOSED** | docstring 已改为"运行代/环境绑定按 strip 后非空判定…`reconcile_allowed` 仅当账户/生命周期/方向域匹配才为真"（r4 diff `k04_management.py:234-239`） | `k04_management.py:234-239` |
| B N14（P3） | changelog 漏登 4 例 inputs-only 变更 | **NOT CLOSED** | 同 §2.2 P3-3（README 零改动；且新增 36 条既有用例 inputs 变更也未登记） | `docs/contracts/README.md:262-276` |
| B N15（P3） | `ManagementIntent` 不强制环境域 | **NOT CLOSED（未变，已登记 B-O4）** | r4 未改该 validator；README §12 `B-O4`（`:329`）已登记 → 属已登记项 | `k04_management.py:104-124` |
| B N16（P3） | K04 状态机残余（终态迟到 fill / 手工构造 confirm） | **NOT CLOSED（未变，已登记 B-O1/L11）** | r4 未改；README `B-O1`（`:326`）与 `L11`（`:308`）已登记 → 属已登记项 | `k04_management.py`（未改段） |

### 2.5 判定计数

| 分类 | 数量 |
|---|---|
| CLOSED | 12（Q P2-2/3/4/5/6、Q P3-1/2、C NR-1/2/3、B N12、B N13） |
| PARTIAL | 1（Q P2-1：命名缺陷闭合，对象自报残留） |
| NOT CLOSED | 3 行 / 2 个缺陷（Q P3-3 ≡ B N14：changelog 登记；C P2-6 r1 残余：K01 常量）＋ 2 行"未变但已登记"（B N15/N16） |

## 3. r4 新发现

### NEW-R4-P2-1（P2，崩溃，未登记）`rules` 为 Mapping 但含非 JSON 值/非 str 键时抛裸 `TypeError`

- 位置：`src/core/contracts/k02_evidence.py:523-524`（`def _rules_hash(rules): return canonical_hash(dict(rules))`），被 `:648-653`（`validate_bundle`）与 `:849`（`build_bundle` 的 `quality["rules_hash"]`）调用；崩溃点 `src/core/contracts/common.py:41-56`（`_json_default` 抛 `TypeError("not JSON serialisable: ...")`、`json.dumps(sort_keys=True)` 在混合键型上抛 `TypeError: '<' not supported between instances of 'str' and 'int'`）
- 复现（`_review_scratch/r4_rules_crash.txt`、`r4_more_edge.txt`，逐字）：
  - `build_bundle(SNAP, BARS, {1:'x','required_intervals':['1h']})` → `RAISED TypeError '<' not supported between instances of 'str' and 'int'`（`return _iterencode(o, 0)`）
  - `build_bundle(SNAP, BARS, {'required_intervals':['1h'],'x':object()})` → `RAISED TypeError not JSON serialisable: object`
  - `validate_bundle(bundle, rules={'required_intervals':['1h'],'x':{'a':{1}}})` → `RAISED TypeError not JSON serialisable: set`
  - `validate_bundle(bundle, rules={'required_intervals':['1h'],'x':b'z'})` → `RAISED TypeError not JSON serialisable: bytes`
  - 对照（无崩溃）：`rules` 含 `float('nan')` / `Decimal('1')` → `RULES_MISMATCH`；纯 str 键 JSON 值 → 正常
- 为什么是**新回归**：r3（`b36d8a9`）的 `validate_bundle` 用 `rules.get("required_intervals")` 等逐字段比对，`{1:'x'}` 只会得到 `RULES_MISMATCH`（我已按 r3 代码逻辑复算：`{1:'x','required_intervals':['1h']}.get('required_intervals') == ['1h']`，不触碰哈希）；`_rules_hash` 是 r4 新增，`build_bundle` 侧同样新增了 `quality["rules_hash"]`，两条路径同时引入崩溃面。
- 期望依据：契约 fail-closed 原则（README §3–§6 与本任务标尺）；`r4` 已专门为 `rules` 增加 `isinstance(rules, Mapping)` 分支（`:651`），说明作者认领了"畸形 rules 必须结构化拒绝"的责任，但对 **Mapping 内部内容**未做校验。r3 把同类"畸形内部参数致崩溃"（K01 `AttributeError`）定为 **P2**，故此处按 P2 定级。
- 现实可达性：`rules` 由调用方（U03）从配置装载；YAML 未加引号的日期/时间戳会产出 `datetime`/`date` 对象，`json.dumps` 无 default 分支即 `TypeError`；`{1:'x'}` 这类混合键型则来自代码拼接配置。
- 建议：`_rules_hash` 前先 `try: return canonical_hash(dict(rules)) except (TypeError, ValueError): raise/reject`，或让 `validate_bundle`/`build_bundle` 对 `canonical_json` 失败一律转 `RULES_MISMATCH`/`ValidationError`；或登记 §12。

### NEW-R4-P2-2（P2，fail-open，未登记）手工构造 `ZeroFeeProof(provable=True, coverage_hash=<正确哈希>)` 可跳过全部零费用条件

- 位置：`src/core/contracts/k03_income.py:619`（`proof = zero_fee if isinstance(zero_fee, ZeroFeeProof) else None`）与 `:642-668`（只要 `proof.provable` 为真即返回 `COST_COMPLETE/ATTRIBUTED`，**不重算** `zero_fee_proof` 的任何条件）
- 复现（`_review_scratch/r4_probe_new_out.txt`，逐字）：
  - `zero_fee_proof(bad_cov) = provable=False reason_codes=('TERMINATION_PROOF_MISSING',) coverage_hash='46f6265db62f...'`（`bad_cov` = COMPLETE、无 gaps、但 `termination_proof=None`）
  - `NEW-A-1 finalize_costs(bad_cov, forged ZeroFeeProof(provable=True)): cost_status='COMPLETE' coverage_status='COMPLETE' attribution_status='ATTRIBUTED' reason_codes=('COST_COMPLETE',)`
  - 对照 `NEW-A-2`（`zero_fee=None`）→ `('ZERO_FEE_NOT_PROVABLE',)`；`NEW-A-3`（真证明）→ `('TERMINATION_PROOF_MISSING','SOURCE_UNAVAILABLE','PAGINATION_INCOMPLETE','LATE_RECHECK_MISSING')`
- 期望依据：README §5 `finalize_costs` 行「无事件时必须传入 `ZeroFeeProof` **结果对象**（不得自报布尔）」、§5 `zero_fee_proof` 行「分页终止（**TerminationProof 且与窗口绑定**：… `last_page_size<page_limit`、`terminated_at>=end`）」；r4 docstring（`:613-614`）自称「字典/布尔自报一律不构成证明」。`coverage_hash` 是公开可算的（`model_content_hash` 已导出于 `core.contracts`），伪造成本仅一次函数调用，故 r3 P2-1 的"自报即通过"在对象形态下**依然成立**：无 TerminationProof 的空窗口可被判"确证零费用"。
- 与 r3 建议的关系：r3 建议的两点（拒绝 Mapping、`ZeroFeeProof` 回带 coverage 哈希并与入参比对）**均已实现**，所以 §2.1 记为 PARTIAL 而非 NOT CLOSED；但"证明条件不得由调用方自报"这一**根因**未解——`finalize_costs` 应自行重算条件或接收原始布尔而非信任 `provable`。
- 建议：`finalize_costs` 增加原始条件参数并内部调用 `zero_fee_proof`；或把"对象可伪造"显式登记（与 `L11 OrderQuantity 构造器不内建校验` 同类）。

### NEW-R4-P3-1（P3，文档/规格失同步，未登记）README 完全未随 r4 更新

- `git diff --stat b36d8a9 5ee082b -- docs/contracts/README.md` → **空**。以下 r4 行为/结构变更在 README 中**零记载**（逐项 grep 计数）：
  - `RULES_MISSING`：README 命中 **0**；README `:88` 仍写签名 `validate_bundle(bundle, rules=None, identity=None)`，`:92` 仍写"**验证方应显式传 `rules`**"（"应"，非"必须"），`:95-97` 的 reason 清单亦无 `RULES_MISSING`。实现已把它变为**必传**（`rules=None → RULES_MISSING → EVIDENCE_INVALID`），按 README §10（`:215-221`）"收紧判定必须登记为小版本行为变更"应登记，未登记。照 README 实现的 U03 会对**每一个 intent** 得到 `EVIDENCE_INVALID`（方向 fail-closed，但契约文档失真）
  - `ZERO_FEE_PROOF_MISMATCH`：README 命中 **0**（§5 `finalize_costs` 行未提窗口哈希绑定）
  - `EvidenceBundle.dropped_input_ids`：README 命中 **0**；§10 的 3.1 新增字段清单（`:215-218`）未列入该新模型字段（schema 已相应重生成为 `k02_evidence_bundle.schema.json`）
  - `unknown_field`：README 命中 **0**；§3 未记载"Mapping 载荷未知键按 `组.unknown_field:<键>` 拒绝"与"非 Mapping 按 `组.*` 拒绝"
- 影响：README 是本包唯一冻结规格；下游 U01–U05 按 README 编码。属文档缺陷（P3），不改判定语义
- 建议：补 §4 reason 清单（`RULES_MISSING`）、§5 reason（`ZERO_FEE_PROOF_MISMATCH`）、§10 3.1 字段新增、§3 覆盖口径，并在 §10 登记 `rules` 必传为小版本收紧

### NEW-R4-P3-2（P3，审计面，未登记）`dropped_input_ids` 的去重集合不可重算，可凭空申报

- 位置：`src/core/contracts/k02_evidence.py:575-585`（只校验非空 str / 唯一 / 与保留输入不相交，**不比对实际重复内容**）
- 复现（`r4_probe_new_out.txt`）：`NEW-B-2 fabricated dropped_input_ids=('never-existed',) -> valid=True reasons=[]`（该 id 在 inputs 中不存在）；`NEW-B-3 dropped_input_ids=('in-02',) [retained input] -> valid=False ['QUALITY_MISMATCH']`；`NEW-B-1 duplicate-content inputs kept, dropped=() -> valid=False ['DERIVED_FACT_MISMATCH']`（重复内容本身能被事实重建拦下）
- 影响：`dropped_input_ids` 是审计字段，可凭空申报"丢弃过 N 个输入"。无判定消费者，且 §4 明示去重是 build 侧行为，故 P3
- 建议：按 `(symbol, interval, close_time, values)` 从 inputs 反查可重算的去重集合，或 README 明写"该字段为审计提示、不可重算"

### NEW-R4-P3-3（P3，边缘）`model_copy` 绕过校验后 `dropped_input_ids` 迭代崩溃

- 复现（`r4_dropped_edge.txt`）：`EvidenceBundle(model_copy).dropped_input_ids = 7` 后 `validate_bundle` → `RAISED TypeError : 'int' object is not iterable`（`None` 同）；字符串 `'abc'` → `valid=True`（按字符迭代当作 3 个被丢弃 id）
- 对照：直接构造 `EvidenceBundle(**{... 'dropped_input_ids': 7})` → `ValidationError`（`tuple_type`），故仅 `model_copy` 绕过可达；pydantic `model_copy(update=)` 不重校验是通用行为，`L11`（构造器不内建校验）已覆盖同类
- 建议：`:575` 循环前加 `isinstance(bundle.dropped_input_ids, (tuple, list))` 守卫

## 4. 新增负例 golden 抽查（8 条，仅读 inputs/expect/derivation 手推，未看实现）

方法：只读 golden 的 `inputs/expect/derivation` + README 条款独立推导，再与 `expect` 比对（smoke 已实测全 PASS）。**期望值与我的手推全部一致**，无"复制实现输出"痕迹；`derivation` 均引用了规格条款或 r3 审查编号。

| 用例 | 手推（规格依据） | 与 expect |
|---|---|---|
| K01-N6 | §3:66-71 字段级覆盖 + 空串计缺口（§3:75）→ `route_blank_model_effort` 报 `model_route.model/effort`；非 Mapping 无字段可查 → 整组 `model_route.*`；未知键不在 §3 字段表内 → `unknown_field:`（新增口径，见 NEW-R4-P3-1）；`control_all_present` → `[]` | 一致 |
| K02-N31 | §4:105「claim 引用必须 ⊆ `bound_inputs`」；`∅` 下任何引用必越界 → `CLAIM_OUTSIDE_BOUND_INPUTS`，claim 本身仍 `PASS` | 一致 |
| K02-N32 | §4:92「验证方应显式传 rules」→ 实现升级为必传，缺省 `RULES_MISSING`；其余 reason 清单对照 | 一致（口径见 NEW-R4-P3-1） |
| K02-N33 | §4:83「跨 symbol 输入…被引用时给 `INPUT_SYMBOL_MISMATCH`」+ §4:91 quality 字段全量重算 → `symbol_mismatch_inputs` 未声明该输入 → 追加 `QUALITY_MISMATCH`；引用它的 claim → `UNRESOLVED` | 一致 |
| K02-N34 | 被丢弃 ID 只应出现在 `dropped_input_ids`（§4:84-86 去重语义）；`quality.dedup_removed` 不可重算 → 非空自报 → `QUALITY_MISMATCH`，同时 `dropped_input_ids=['in-dup']` 合法保留 | 一致 |
| K03-N20 | §5「不得自报布尔」→ dict 非结果对象 → `ZERO_FEE_NOT_PROVABLE`，`cost_status=INCOMPLETE`、`attribution_status=UNKNOWN` | 一致 |
| K03-N21 | §5「TerminationProof 且与窗口绑定」→ 证明回带 coverage 哈希，窗口不同 → `ZERO_FEE_PROOF_MISMATCH` | 一致 |
| K04-N28 | §6:143-144「授权缺 `runtime_instance_id`→`RUN_GENERATION_UNBOUND`、缺 `environment`→`ENVIRONMENT_UNBOUND`」；`''` 视同缺 → 两码同时给出 | 一致 |

（另：K04-N30 `ManagementIntent(environment='')` → `ValidationError` 与 §6:138-140「validator 强制」形状一致；`L10` 子集断言仍在（`scripts/smoke_contracts_v3.py:833-854` 只查 expected 的键），故 golden 的 `expect` 若漏写新输出字段不会被冒烟发现——已登记限制，未变。）

## 5. 新对抗批次汇总（超出 r3 清单的尝试）

| 批次 | 目标 | 结果 |
|---|---|---|
| NEW-A | K03 对象自报零费用证明 | `ZeroFeeProof(provable=True, 正确哈希)` → `COMPLETE`（NEW-R4-P2-2）；`nan`/`Decimal` 等正常路径不受影响 |
| NEW-B | K02 去重集合可重算性 | 重复内容输入被抓（`DERIVED_FACT_MISMATCH`）；凭空 `dropped_input_ids` 通过（NEW-R4-P3-2）；丢弃真实保留输入被拒 |
| NEW-C | K04 绑定字段非 str 类型 | `rid=123/[]/0/True`、`env=456/{}/0/False` → 全部 `ValidationError(string_type)`（fail-closed，无崩溃） |
| NEW-D | K01 `build_identity` vs `validate_coverage` 口径 | `build_identity` 报 `unknown_field`，`validate_coverage` 不报（build 侧更严，方向安全，仅记录） |
| NEW-E | K02 `bound_inputs` 类型滥用 | 裸字符串按字符迭代 → 必然 `CLAIM_OUTSIDE_BOUND_INPUTS`；dict → 取键集合；`None` → 越界；均 fail-closed 无崩溃 |
| NEW-F | K02 `rules` 载荷形态 | NaN/Decimal → `RULES_MISMATCH`；tuple → `RULES_MISMATCH`；**set/bytes/object/混合键型 → 裸 `TypeError`**（NEW-R4-P2-1） |

## 6. 复现命令清单

```bash
cd "C:/Users/Tao/Desktop/币安/mat-wt/reviews/U00/Q/w0r1"
git checkout 5ee082b
git status --porcelain
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
export PYTHONUTF8=1

# 1) 冒烟 + 计数
"$PY" scripts/smoke_contracts_v3.py | tee _review_scratch/r4_smoke_out.txt
grep -c '^  PASS' _review_scratch/r4_smoke_out.txt   # 173
grep -c '^  FAIL' _review_scratch/r4_smoke_out.txt   # 0

# 2) schema 独立重生成
"$PY" _review_scratch/r4_schema_recheck.py | tee _review_scratch/r4_schema_recheck_out.txt   # ALL_EQUAL

# 3) smoke 清单
"$PY" -c "import importlib.util,sys;s=importlib.util.spec_from_file_location('r','scripts/run_offline_regression.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m._validate_smoke_manifest())"

# 4) 逐条复验（cwd = _review_scratch，脚本内 sys.path 用 parents[1]/src）
cd _review_scratch
"$PY" r4_probe_q_p2.py   | tee r4_probe_q_p2_out.txt    # Q P2-1..6
"$PY" r4_probe_q_p3_cb.py| tee r4_probe_q_p3_cb_out.txt # Q P3-1..3 / C NR-1..3 / B N12,N13
"$PY" r4_probe_new.py    | tee r4_probe_new_out.txt     # 新发现对抗批次
cd ..

# 5) golden 变更比对
"$PY" _review_scratch/r4_golden_diff.py | tee _review_scratch/r4_golden_diff_out.txt

# 6) 新发现最小复现（rules 崩溃）
"$PY" -c "
import sys;sys.path.insert(0,'src')
from core.contracts import build_bundle
ID={'schema_version':'3.1','environment':'d','account_ref':'a','artifact_hash':'a','git_commit':'c','dependency_hash':'d','effective_config_hash':'e','strategy_hash':'s','model_route_hash':'m','runtime_instance_id':'i'}
for r in [{1:'x','required_intervals':['1h']},{'required_intervals':['1h'],'x':object()}]:
    try: build_bundle({'evidence_id':'e','symbol':'S','as_of_ms':1,'identity':ID},[],r); print('  no crash')
    except Exception as e: print('  RAISED',type(e).__name__,':',e)
"
# → RAISED TypeError : '<' not supported between instances of 'str' and 'int'
# → RAISED TypeError : not JSON serialisable: object
```

## 7. 模型 / 独立性声明与边界

- **同模型偏差**：本轮 Q 复审、被审实现的作者会话、以及 r3 的 C/B/Q 报告均由 `deepseek-v4.1-flash` 生成（会话隔离，非跨模型独立审计）。共同盲区无法排除，尤其对"自报型对象证明"（NEW-R4-P2-2）与"内部参数畸形"（NEW-R4-P2-1）的恐慌阈值可能偏低或偏高。
- **独立性保障**：所有判定均来自 (a) 我方自建探针（不复用作者/前轮脚本）的原始 stdout、(b) `git show`/`git diff` 对 `b36d8a9`、`5ee082b` 两版源码与 golden 的直接读取、(c) 对 README 条款的手工推导。作者的黄金文件、smoke 输出与提交信息仅作交叉核对对象，未作为结论依据。`adv_04` 类"断言非空转"检验由 `r4_golden_diff.py` 的"`expect_changed == []`"与手推抽查替代。
- **未完成/未覆盖项**：① 未做真实 Binance 语义下的分页排序/边界包含性验证（无网络，超出契约层）；② 未逐条枚举全部 reason_code 的 golden 触达率（r3 §6 已列清单，r4 仅新增 13 例，覆盖缺口仍在）；③ `pydantic` 版本行为（`model_copy` 不重校验）按文档推定，未做矩阵验证；④ 未审计 `scripts/` 之外的调用方（U01–U05 尚不存在）。
- **只读边界**：仅以 `multi-agent-trader/.venv/Scripts/python.exe` 作为解释器调用；未进入/读取该服务的 `data/`、`.env`、凭证、账户标识、运行状态；未启停其任何服务；未在 `mat-wt/I` 产生任何写入（复核 `git status --porcelain` 为空）。

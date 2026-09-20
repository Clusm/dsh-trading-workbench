# U00 契约层 v3 — Q 独立复审 r5（被审 `71f8109`）

- 复审对象：`71f8109`（`2feccaa` ← `523db59` ← `cca5f27` ← `3de51d9`）—— U00 r5 修复轮，4 个提交叠在 `3de51d9` 之上
- 上一轮：`5ee082b`（r4 候选），Q 结论 CHANGES_REQUESTED（`_review_scratch/Q_w0r1_r4.md`）
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1`（detached `71f8109`），全部产物落 `_review_scratch/`，未改任何 tracked 文件
- 边界声明：未读 `.env`/`data/`/凭证/账户标识；未启停服务；未联网；未 commit/push/tag/branch-switch；未写 `mat-wt/I`；未动 `docs/dispatch/RESUME.md`

## 0. 结论

**VERDICT: PASS-with-notes**

六项修复全部独立复现为"已闭合"；12 项回归项全部仍闭合；README 同步逐条 grep 通过；golden/schema/脚本零改动并已独立重生成比对。残留问题按严重度排序：

| 类型 | 编号 | 严重度 | 是否已登记 | 阻塞？ |
|---|---|---|---|---|
| fail-open（三外部标志仍可伪造） | §12 `L17` | **P2** | **是**（README:381，描述诚实） | 否（已登记） |
| 崩溃（深嵌套 rules → 裸 `RecursionError`） | **NEW-R5-P3-1** | **P3** | **否** | 否（P3，但建议补登记 §12 L21） |
| 文档理由失实 + 自动化覆盖缺口 | §12 `L20` | **P3** | 登记了结论、**理由失实** | 否（建议改写 + 补 op 驱动用例） |
| 崩溃（敌对 Mapping 子类抛任意异常穿透） | NEW-R5-P3-2 | P3（构造性极强） | 否 | 否 |

按标尺："Any unregistered P2 fail-open or crash ⇒ CHANGES_REQUESTED"。本轮**无**未登记的 P2 fail-open/崩溃：L17 是 P2 但**已诚实登记**；我新发现的崩溃是 P3（需 ≥5000 层嵌套，现实配置不可达，且方向仍是 fail-closed 中止）。故给 PASS-with-notes，但把 NEW-R5-P3-1 与 L20 理由失实列为**必须跟进的登记项**。

---

## 1. 基础项（全部自测）

| 项 | 证据 | 逐字输出 |
|---|---|---|
| 冒烟 | `_review_scratch/r5_smoke_out.txt` | `结果：PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)`，exit=0；`grep -c '^  PASS'`=173，`'^  FAIL'`=0 |
| 6 schema 独立重生成 | `_review_scratch/r5_schema_regen.py` / `_out` | `EQUAL ×6`，`SCHEMA_FAILS = 0`（`model_json_schema()` 逐字节 `==` 仓库文件；冒烟同款断言 `scripts/smoke_contracts_v3.py:881`） |
| 冒烟清单 | `_validate_smoke_manifest()`（`scripts/run_offline_regression.py`） | `SMOKE MANIFEST OK included=65 excluded=1`，无异常 |
| `mat-wt/I` 无脏改动 | `git -C .../I status --porcelain` | 空；`git log --oneline -6` 顶部 `71f8109`，其下 `2feccaa/523db59/cca5f27/3de51d9` |
| golden/schema/脚本零改动 | `git diff 5ee082b..71f8109 -- docs/contracts/golden "docs/contracts/*.schema.json" scripts/` | **空**（diff --stat 仅 `README.md`+`src/core/contracts/{__init__,k01_identity,k02_evidence,k03_income}.py`+dispatch 文档+任务清单 json） |
| golden 逐用例零改动 | `_review_scratch/r5_golden_diff_r5_out.txt`（`5ee082b`→`71f8109`） | `added: []`、`removed: []`、**`expect_changed: []`**、`inputs_changed: []`、`total=160 positive=45 negative=115` |

> 注：`_review_scratch/r5_golden_diff_r5.py` 由 r4 脚本改 rev 生成，比对含 `expect` 块逐键比对，非仅文件 diff。

---

## 2. 六项修复逐项核验

### 2.1 NEW-R4-P2-1（`rules` 非 JSON 值/混合键型裸 `TypeError`）→ **CLOSED**

实现：`src/core/contracts/k02_evidence.py:524`（`class RulesInputError(ValueError)`）、`:532`（`_rules_payload`，`except (TypeError, ValueError)` → 结构化抛错）、`:550`（`_rules_hash`）、`:693`（validate 侧 `try/except RulesInputError` → `RULES_MISMATCH`）、`:753`（build 侧先行守卫）、`:904`。

r4 复现逐字重跑（`_review_scratch/r5_probe_rules_out.txt`，A/B 节）：

```
build_bundle rules={1:'x','required_intervals':['1h']}: RAISED RulesInputError: rules payload is not canonical JSON: '<' not supported between instances of 'str' and 'int'
build_bundle rules={'required_intervals':['1h'],'x':object()}: RAISED RulesInputError: rules payload is not canonical JSON: not JSON serialisable: object
build_bundle rules={'x':{1,2}}:  RAISED RulesInputError: ... not JSON serialisable: set
build_bundle rules={'x':b'z'}:   RAISED RulesInputError: ... not JSON serialisable: bytes
build_bundle rules={'x':datetime.datetime(2026,9,21)}: RAISED RulesInputError: ... not JSON serialisable: datetime
build_bundle rules=['not','a','mapping']: RAISED RulesInputError: rules must be a mapping, got list
build_bundle rules={1:'x'} (single non-str key): RAISED RulesInputError: ... '<' not supported ...
RulesInputError is ValueError subclass: True
```

`validate_bundle` 侧（B 节）：`bytes/nested set/object/int-key/datetime` → 全部 `valid=False reasons=['RULES_MISMATCH']`，**零崩溃**。

**新崩溃路径搜寻（作者未覆盖）**：

- `NEW-R5-P3-1`（**P3，新发现，未登记**）：**深嵌套无环 dict** 使 `canonical_json` 抛 `RecursionError`，而守卫只捕 `(TypeError, ValueError)`；`RecursionError` 是 `RuntimeError` 子类 → **裸穿透**。
  `_review_scratch/r5_probe_dropped2_out.txt`：
  ```
  depth=1000: OK    depth=2000: OK    depth=5000: RecursionError (UNCAUGHT): maximum recursion depth exceeded while encoding a JSON object
  ```
  阈值约 5000 层。realism：`rules` 为 `Mapping[str,Any]`、现实来自配置装载，5000 层嵌套仅可蓄意生成；且失败方向是**响亮中止**而非静默放行（fail-closed 未破）。故 P3。
- 自引用结构（作者文档明确点名）：`rec['self']=rec` → `RulesInputError: ... Circular reference detected`（`ValueError` 分支命中）✓。
- `NEW-R5-P3-2`（P3，构造性）：自定义 `Mapping` 子类在 `__iter__` 抛 `KeyError`/`RuntimeError`、或自定义键 `__lt__` 抛异常（`sort_keys=True`），异常原样穿透（C2/C2b/C6）。需调用方自备敌对对象，现实不可达，仅记录。
- 反向：`dict` 子类覆写 `keys()` 抛 `OSError` **不影响** build（CPython `dict(m)` 快路径不调覆写 `keys()`，C3 成功建包）——非缺陷。

### 2.2 NEW-R4-P2-2（伪造 `ZeroFeeProof` 跳过零费用条件）→ **CLOSED（coverage 侧）/ 残留 L17 已登记**

实现：`src/core/contracts/k03_income.py:268`（`_coverage_provable_reasons`，铸证/复算共用）、`:671`（`finalize_costs` 由入参 coverage **重算**）。

r4 逐字复现重跑（`_review_scratch/r5_probe_l17_out.txt`、`r5_rerun_probe_new.txt`）：

```
L17-E forged provable=True on BAD coverage(无 TerminationProof): cost_status=INCOMPLETE reasons=['TERMINATION_PROOF_MISSING']   ← r4 曾为 COST_COMPLETE，已闭合
L17-D zero_fee_proof(GOOD, all True) + GOOD coverage            : cost_status=COMPLETE   reasons=['COST_COMPLETE']              ← 诚实路径仍通
L17-F replay proof_B on coverage A                              : cost_status=INCOMPLETE reasons=['ZERO_FEE_PROOF_MISMATCH']   ← 跨窗口复用仍拒
K03-N21 golden golden 仍 PASS（金标 expect 未变，冒烟实测通过）
```

`finalize_costs` 现在对 status/GAP/gaps/TerminationProof（窗口一致、短页、时间）/watermark/半开区间全部重算，`provable` 布尔不再被单独采信。

**L17 残留 = 真实 fail-open（P2）**：`_review_scratch/r5_probe_l17_out.txt` A/B/C 节：

```
L17-A zero_fee_proof(GOOD, source_available=False, pages_complete=False, late_recheck_done=False) = provable=False reasons=['SOURCE_UNAVAILABLE','PAGINATION_INCOMPLETE','LATE_RECHECK_MISSING']
      honest proof object -> finalize_costs: INCOMPLETE reasons=['SOURCE_UNAVAILABLE','PAGINATION_INCOMPLETE','LATE_RECHECK_MISSING']
L17-B forged ZeroFeeProof(provable=True, correct hash)  -> finalize_costs: cost_status=COMPLETE reasons=['COST_COMPLETE']
L17-C forged reasons=()                                  -> finalize_costs: cost_status=COMPLETE reasons=['COST_COMPLETE']
```

即：**同一份内容合规 coverage**，诚实铸证（三标志 False）判 `INCOMPLETE`，而手工 `ZeroFeeProof(provable=True, coverage_hash=model_content_hash(coverage))`（`model_content_hash` 已公开导出）判 `COST_COMPLETE`。三标志无签名、无原始入参，故 `COST_COMPLETE` 仍可在"来源不可用"的实况下取得。

**L17 登记诚实性：判定为诚实。** README:381 逐字：「具备正确 coverage 哈希的伪证仍可对**这三个标志**说谎（coverage 侧条件已不可伪造）；要彻底关闭需给 `finalize_costs` 增加原始条件入参（签名变更）」——准确描述了我复现的机制、范围与修法。**我给的严重度：P2**（确为 fail-open，但已登记且属"构造器不内建校验"同族 L11；彻底闭需小版本签名变更，下游 U01 已登记）。

### 2.3 README 同步 → **CLOSED（逐条 grep 实测）**

`_review_scratch/r5_readme_grep.py` / `_out`：23 项检查**全部 OK**，含：

| 要求 | 命中行 |
|---|---|
| §3 `unknown_field` 口径 | 21/83（§3 覆盖口径段） |
| §4 `rules` 必传 + `RULES_MISSING` | 115（「`rules` 为必传参数」）、116/121/255/261 |
| §4 畸形内容语义 + `RulesInputError` | 108/256/263 |
| §4 `dropped_input_ids` 形态口径 | 124（新增专段） |
| §5 `ZERO_FEE_PROOF_MISMATCH` + 重算 | 163/255/264（§5 行「不信任对象自报的 `provable`」） |
| §10 3.1 新字段/常量 | 252 |
| §10 T1–T5 收紧表 | 261–265（T1 r4 / T2 r4 / T3 r5 / T4 r5 / T5 r5） |
| §10「收紧判定必须登记为小版本行为变更」+ 表头「均为小版本行为变更」 | 251 / 258 |

**T1 确实把 `rules`-必传登记为小版本行为变更**（README:261）：影响列写「未显式传 `rules` 的调用方（U03）从'跳过规则比对'变为一律 `EVIDENCE_INVALID`（方向 fail-closed）」，golden 列 `K02-N32/N35`——满足 §10:251 的强制登记要求。

**§11 changelog 是否真逐条枚举 r4 输入变更**：独立复算（`_review_scratch/r5_golden_diff_r4.txt`，`b36d8a9`→`5ee082b`）：
```
inputs_changed: ['K02-N1',...,'K02-N21', 'K02-N23',...,'K02-N30', 'K02-P1'..'K02-P6', 'K03-P5']  (共 36)
expect_changed: []     added: 13     removed: 0
```
README:321 写「`K02-N1..N21、N23..N30、P1..P6`（r4，共 35 条）」+ README:322「`K03-P5`（r4）」→ **35+1=36，集合与实测逐条吻合**（区间写法压缩但边界精确，`N22` 被显式排除且实测确未变）。r3 的 `K01-P3/P4/P5`（:319）与 `K03-N5`（:320）已补登。

### 2.4 NEW-R4-P3-2 → §12 `L18`（登记而非修）→ **判断：论证成立，合法范围决策**

`_review_scratch/r5_rerun_probe_new.txt`：`NEW-B-2 fabricated dropped_input_ids=('never-existed',) -> valid=True reasons=[]`（凭空申报仍通过）；`NEW-B-3 ... =('in-02',) [保留输入] -> valid=False ['QUALITY_MISMATCH']`；`NEW-B-1 重复内容保留 -> valid=False ['DERIVED_FACT_MISMATCH']`。

论证 `contents absent → 去重集合不可重算` **成立**：被丢弃输入的载荷确不在 `inputs` 内（`dedup` 是 build 侧行为，只保留幸存者），故无任何可复核数据能反推"应丢弃集合"。可复核的部分（幸存输入互相不重复）已被事实重建拦下。L18 文本（README:382）如实标注"仅形态受校验、无判定消费者、要强制需新增被丢弃载荷字段=小版本新增字段"。**非合理化**。

### 2.5 NEW-R4-P3-3（`dropped_input_ids` 非序列形态）→ **CLOSED**

实现：`src/core/contracts/k02_evidence.py:608`（`isinstance(..., (tuple, list))` 守卫）。

复现（`_review_scratch/r5_probe_rules_out.txt` D 节 + `r5_probe_dropped2_out.txt`）：

```
str 'abc'            -> QUALITY_MISMATCH   (r4 曾 valid=True，已闭合)
int 7 / None / 1.5 / dict / frozenset / bytes -> QUALITY_MISMATCH   (r4 int/None 曾裸 TypeError)
list-of-bytes [b'x'] -> QUALITY_MISMATCH
list ['x', 5] / tuple ('x', 5) 非字符串元素 -> QUALITY_MISMATCH
list [[]] / [''] / ['  '] -> QUALITY_MISMATCH
list/tuple 子类 MyList/MyTuple -> 走 isinstance 正常路径（符合语义）
```

无裸异常。**旁注**：`['x']`、`MyTuple(('x',))` 等指向**不存在**的 id 仍 `valid=True` —— 这是 L18（不可重算）而非本修复面，方向一致。

### 2.6 C P2-6 → §12 `L16`（登记而非修）→ **判断：论证成立，合法范围决策**

实现：`src/core/contracts/k01_identity.py:28`（`STRATEGY_SOURCE_FILES` 声明标签）、`:181`（`strategy_cards` 仅"非空"校验；`:184` `DEPENDENCY_SOURCE_FILES` 才有**文件名级**校验）。

读码确认论证属实：`hash_strategy`（`k01_identity.py:132-147`）接受 `Mapping`（如 `{"cards":[...]}`）或 Mapping 序列，即**卡片内容本身**；而 `hash_dependencies` 收的是 `路径 -> 内容` 映射（`_structured_mapping`），故能校验"锁文件名出现"。内容侧无法反查文件来源，文件级检查**在本层不适用**。L16（README:380）与 §3 口径段均明写"仅声明标签、不参与 `_coverage_gaps` 文件级校验、下游 U15"。**非合理化**。

### 2.7 "无 golden 期望变更 / 未触碰 golden·schema·脚本" → **VERIFIED**

见 §1 两行：文件级 `git diff` 空 + 用例级 `expect_changed: []`、`inputs_changed: []`（`5ee082b`→`71f8109`）。6 schema 独立重生成 6/6 `EQUAL`。

### 2.8 `L20`（"JSON fixture 无法表达 `set`/`object`/混合键型，故不加 golden 用例"）→ **判断：理由失实，属合理化**

**该论证为假。** 冒烟**不是**纯数据驱动：`scripts/smoke_contracts_v3.py:856-866` 的 `run_case` 只从 JSON 取 `operation`（字符串）+ `inputs`（JSON），但 `OPERATIONS`（:797）把字符串派发到**任意 Python 函数**——操作函数内可自由构造非 JSON 载荷。既有先例两处，均在本次被审代码内：

- `op_k05_incident_recovery`（:687-711）：`inputs.get("circular_evidence")` 为 JSON 布尔，函数内 `circular["..."] ["self"] = circular` 构造**自引用 dict**（JSON 绝对无法表达），并据此断言 `EVIDENCE_NOT_SERIALISABLE`。对应 golden `K05-N14`（`k05_trace.golden.json:502`）实测 PASS。
- `op_k02_bundle_tamper`（:280-337）：`tamper["kind"]` 开关内用 `model_copy(update=...)` 构造绕过类型校验的形态（含 `add_symbolless_input` 置 `symbol=None`、`quality_field` 任意值、`hash_only`）。

**直接可行性证明**（`_review_scratch/r5_l20_feasibility.py` / `_out`，全部落在 `_review_scratch/`，未改 tracked 脚本）：用**严格 JSON** fixture（`{"required_intervals":["1h"],"x":{"__py__":"set","items":[1,2]}}`）+ 一个约 10 行的 op 侧 tag 解码器（拷贝 `K05-N14` 既有模式），即可复现 T3 的 `set` rules 拒绝：

```
fixture is pure JSON: True
op result on the JSON-only fixture: {'rules_mismatch': True}
expect match: True
```

解码器同法支持 `bytes`/`object`/`datetime`。因此 L20 的"**无法写成 JSON golden fixture**"**作为 harness 能力论断是错的**——真实情况是"本轮未新增 op/分支"。后果不只是文档措辞：**T3（P2 级修复）与 T5 在本项目主回归门（冒烟 173 项）中零自动化覆盖**，只能靠一次性探针；下一次改动可静默回退。**判定：合理化，非合法范围决策**（README 已登记该限制本身，故严重度仍记 P3；但理由须改写，并建议补 1–2 条 op 驱动用例）。

---

## 3. 回归项（12 项，全部仍闭合，逐字重跑）

| 编号 | 复现脚本 | 逐字观测 | 状态 |
|---|---|---|---|
| Q P2-2 `bound_inputs=[]` | `r5_rerun_q_p2.txt` | `P2-2 CLOSE bound_inputs=[] ...: ('EVIDENCE_INVALID', ['CLAIM_OUTSIDE_BOUND_INPUTS','MISSING_REQUIRED_CLAIM'])` | CLOSED |
| Q P2-3 `symbol=None` | 同上 | `P2-3 validate_bundle with symbol=None input: (False, ['INPUT_SYMBOL_MISMATCH','QUALITY_MISMATCH'])` | CLOSED |
| Q P2-4 空串绑定 | 同上 | `P2-4 auth(rid='', env='') -> (False, ['RUN_GENERATION_UNBOUND','ENVIRONMENT_UNBOUND'])`；`rid='   '` 同 | CLOSED |
| Q P2-5 `UNKNOWN→ACCEPTED` | 同上 | `P2-5a ...: RAISED TransitionError: ACCEPTED requires prior SUBMITTED fact` | CLOSED |
| Q P2-6 非 Mapping coverage | 同上 | `P2-6 model_route=list: IdentityCoverageError groups=['model_route.*']`（str/int/None 同） | CLOSED |
| Q P3-1 int≠bool 证据 | `r5_rerun_q_p3_cb.txt` | `P3-1 value=True, satisfied=1: recovered=False reasons=['EVIDENCE_TYPE_INVALID']`（1/True、False/0 等 5 组同） | CLOSED |
| Q P3-2 `dedup_removed` 非空 | 同上 | `P3-2 dedup_removed=['in-dup']: valid=False reasons=['QUALITY_MISMATCH']`（'in-01'/伪造 id 同） | CLOSED |
| C NR-1 dedup 重算面 | 同上 | 同上 P3-2 区块（同源） | CLOSED |
| C NR-2 `rules=None` 洗白 | 同上 | `NR-2 validate_bundle(rules=None): valid=False reasons=['RULES_MISSING']`；`rules=[]`/`'x'`/`{}` → `RULES_MISMATCH` | CLOSED |
| C NR-3 K01 空串/未知键 | 同上 | `NR-3 model_route junk extra key: IdentityCoverageError groups=['model_route.unknown_field:junk_field']`；`strategy_cards 空: ACCEPTED`（合法空组） | CLOSED |
| B N12 空串绑定 | `r5_rerun_q_p2.txt`（P2-4 同源，授权侧+意图侧双闭合） | 同上 P2-4 | CLOSED |
| B N13 docstring 与实现一致 | 读码 `src/core/contracts/k04_management.py:234-239` | docstring「运行代/环境绑定按 strip 后非空判定…`reconcile_allowed` 仅当账户/生命周期/方向域匹配才为真」与 `:259-272` 实现一致 | CLOSED |

---

## 4. 新发现汇总

| 编号 | 严重度 | 位置 | 现象 | 建议 |
|---|---|---|---|---|
| **NEW-R5-P3-1** | **P3**（未登记） | `src/core/contracts/k02_evidence.py:546`（`except (TypeError, ValueError)`） | ≥约 5000 层**无环深嵌套** rules → `canonical_json` 抛 `RecursionError`（`RuntimeError` 子类）**穿透**守卫，`build_bundle` 裸崩 | 守卫扩到 `RecursionError`，或先做深度/`RecursionError` 捕获转 `RulesInputError`；并在 §12 补登一条（建议 L21） |
| NEW-R5-P3-2 | P3（构造性） | 同上 | 敌对 `Mapping` 子类 `__iter__` 抛 `KeyError/RuntimeError`、或键 `__lt__` 抛异常 → 原样穿透 | 可选：`except Exception` 收敛；现实不可达，登记即可 |
| NEW-R5-P3-3 | P3（文档） | README:384（L20） | L20 理由失实（见 §2.8）；T3/T5 主回归门零覆盖 | 改写 L20 理由为"本轮未新增 op 分支"；补 op 驱动的 `set`/`model_copy` golden 用例 |

**无**未登记的 P2 fail-open 或 P2 崩溃。

## 5. 复现命令清单

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
cd "C:/Users/Tao/Desktop/币安/mat-wt/reviews/U00/Q/w0r1"
git status --porcelain; git log --oneline -5                       # 71f8109
git diff 5ee082b..71f8109 -- docs/contracts/golden "docs/contracts/*.schema.json" scripts/   # 空
PYTHONUTF8=1 "$PY" scripts/smoke_contracts_v3.py | tee _review_scratch/r5_smoke_out.txt      # PASS 173/FAIL 0
PYTHONUTF8=1 "$PY" _review_scratch/r5_schema_regen.py | tee _review_scratch/r5_schema_regen_out.txt   # EQUAL ×6
PYTHONUTF8=1 "$PY" -c "import sys;sys.path.insert(0,'scripts');import run_offline_regression as R;R._validate_smoke_manifest()"
cd _review_scratch
PYTHONUTF8=1 "$PY" r4_probe_new.py      | tee r5_rerun_probe_new.txt      # NEW-A/B/C/D/E/F
PYTHONUTF8=1 "$PY" r4_probe_q_p2.py     | tee r5_rerun_q_p2.txt           # P2-2..P2-6
PYTHONUTF8=1 "$PY" r4_probe_q_p3_cb.py  | tee r5_rerun_q_p3_cb.txt        # P3-1/P3-2/NR-1..3
PYTHONUTF8=1 "$PY" r5_probe_rules.py    | tee r5_probe_rules_out.txt      # NEW-R4-P2-1 + 新崩溃搜寻
PYTHONUTF8=1 "$PY" r5_probe_dropped2.py | tee r5_probe_dropped2_out.txt   # P3-3 bypass + RecursionError 阈值
PYTHONUTF8=1 "$PY" r5_probe_l17.py      | tee r5_probe_l17_out.txt        # L17 fail-open + 诚实路径
PYTHONUTF8=1 "$PY" r5_l20_feasibility.py| tee r5_l20_feasibility_out.txt  # L20 可表达性证明
PYTHONUTF8=1 "$PY" r5_golden_diff_r4.py | tee r5_golden_diff_r4.txt       # b36d8a9→5ee082b 36 条
PYTHONUTF8=1 "$PY" r5_golden_diff_r5.py | tee r5_golden_diff_r5.txt       # 5ee082b→71f8109 全 0
PYTHONUTF8=1 "$PY" r5_readme_grep.py    | tee r5_readme_grep_out.txt      # README 23 项
```

## 6. 模型与独立性声明

- 本轮 Q 复审由 **`deepseek-v4.1-flash[1m]`** 执行（会话隔离的独立复审者角色）。
- 被审实现（`71f8109` 及 `cca5f27`/`523db59`/`2feccaa`）由 **I（作者）会话**产出；据 r4 报告 §6，作者会话与 r2 的 Q/C/B、r3 的 Q/C/B、r4 的 Q 亦均为 **`deepseek-v4.1-flash`** 系列。
- **故非跨模型独立审计**：同模型共同盲区无法排除，尤其对"自报型对象证明"（L17）与"内部参数畸形"（NEW-R5-P3-1 的恐慌阈值、L20 的 harness 能力判断）可能偏高或偏低。建议下一轮引入异构模型或人工抽检。
- 本报告所有结论均由 `_review_scratch/` 下脚本在本工作区 `71f8109` 上现场重跑得出，未采信作者 golden/冒烟输出作为唯一证据。

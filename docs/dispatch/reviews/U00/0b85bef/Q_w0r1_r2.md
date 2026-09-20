# U00 契约候选 r2 复审报告（Q 独立 QA）

- 会话：Q-w0r1（独立会话，r2 复审）
- 复审对象：候选提交 `0b85bef`（`fix(contracts): close P0/P1 review findings, add regressions (U00 r2)`；同轮 `9d7a100`）
- 对照基线：r1 候选 `db7e0d7` 与本报告作者 r1 报告 `U00_review_Q_w0r1.md`；C r1 报告 `docs/dispatch/reviews/U00/db7e0d7/C_w0r1_r1.md`；B r1 报告 `docs/dispatch/reviews/U00/db7e0d7/B_w0r1_r1.md`
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1`（detached @ `0b85bef`）
- 审查日期：2026-09-21
- 只读约束：未修改任何 tracked 文件（`git status --porcelain` 仅 `?? U00_review_Q_w0r1.md`、`?? _review_scratch/`）；未进入/读取运行目录 `multi-agent-trader\`、`data\`、`.env`；未联网；未启动服务；未提交/推送/打标签
- 复审脚本（未跟踪，均在 `_review_scratch/`）：`r2_adv_verify.py`（新增，含 C/B P0 自建复现 + 10 项新对抗）、`r2_k06_json_inf.py`（K06 JSON `Infinity/1e400` 复现）、`adv_01..04`（r1 原探针原样重跑）
- 真实输出：`_review_scratch/r2_smoke_out.txt`、`r2_r1probes_rerun.txt`、`r2_adv_verify_out.txt`、`r2_golden_detail.txt`

## 0. 结论

**CHANGES_REQUESTED**（窄口修复：1 项 P1 + 3 项建议同批处置的 P2 + 1 项流程登记）

- r1 问题处置：**CLOSED 8 / PARTIAL 3 / NOT CLOSED 1**（共 12 项；r1 的 P0 为 0，C/B 的 3 个 P0 亦已独立复现关闭）
- 新发现：**P0 = 0，P1 = 1，P2 = 10**
- 主体质量：r2 修复方向正确且覆盖面大（golden 56 → 113，positive 26→40 / negative 30→73；冒烟 69 → 126 断言），C P0-1/P0-2 与 B P0 均可用独立探针证伪为“已关闭”，新增负例期望经手工/规格推导可独立复算，未发现实现回放式自证。
- 阻断理由（仅 1 条硬伤）：`k06_release._as_int` 未捕获 `OverflowError`，窗口字段为 JSON `1e400`（Python `json.loads` 直接得到 `inf`）时 `build_release_report` 整体崩溃——正是 r2 自称已修（README §8「报告不崩溃」）的同类缺陷未修尽。
- 若 I 决定把该条与 NEW-P2-1/2/3 一并登记为“下游 U05/U09 自建 fail-closed”并接受，则其余部分具备冻结条件；Q 不建议在未登记的情况下直接打标签。

## 1. 实测汇总（真实输出）

| 项目 | 命令 | 结果 |
|---|---|---|
| 契约冒烟 | `& <venv>\python.exe scripts\smoke_contracts_v3.py` | `结果：PASS 126 / FAIL 0；golden cases=113 (positive=40, negative=73)`；6 个 schema 同步断言全 PASS；退出码 0 |
| smoke 清单校验 | 导入 `scripts/run_offline_regression.py` 调 `_validate_smoke_manifest()` | `SMOKE MANIFEST OK included=65 excluded=1` |
| schema 独立重生成 | `model_json_schema()` ×6 与仓库文件 `==` | 6/6 `EQUAL`（独立于冒烟脚本复现） |
| golden 计数 | 独立解析 6 个 golden JSON | total=113, positive=40, negative=73 |
| r1 探针重跑 | `adv_01..adv_04`（原样，未改） | 见 §2 逐条判定；`adv_04` 的翻转/篡改敏感性仍 FAIL（断言非空转） |
| r2 新探针 | `_review_scratch\r2_adv_verify.py` | 全项输出见 `r2_adv_verify_out.txt`；见 §3/§4 |

r1 探针重跑的关键翻转（对照 r1 报告）：

- `F.garbage_qty/nan_qty/missing_qty/missing_position_qty` → 全部 `allowed=False` 且带具体 reason（r1 为 `allowed=True, reasons=[]`）
- `K.no_timestamp_true / K.string_false / K.str_yes` → `recovered=False, EVIDENCE_TYPE_INVALID`；`K.dict_no_value` → `EVIDENCE_TIMESTAMP_MISSING`（r1 三者为 `RESOLVED`）
- `E.unknown_to_ack` → `TransitionError: ACKNOWLEDGED requires prior SUBMITTED fact`（r1 放行）
- `J.k06_str_window` → `FAIL`（r1 崩溃）；`K06.pass_padded/pass_lower` → `PASS`（r1 误判 UNKNOWN）
- `I.negative_weight` → `ValueError: weights must be non-negative`；`I.zero_weights` → `weights must sum to a positive value`（r1 产生负分摊）
- `B.ingest.false_conflict_scale` → `duplicates=1`（r1 隔离）；`D.tran/D.mgmt` 键已归一化
- `C.contradiction.complete_with_gaps` → `COVERAGE_GAPS_DECLARED` / `provable=False`（r1 无问题/可证明）
- `A.nonmapping.strategy_string_with_secret` → `SensitiveInputError`（r1 静默哈希）
- `L.compare.extra_in_actual` → `[]`（**仍未变**，见 r1 P2-5 判定 PARTIAL）

## 2. r1 逐条判定表

| r1 编号 | 问题 | 判定 | 证据（r2 实测） |
|---|---|---|---|
| P1-1 | K04 授权校验对畸形/缺失数量静默放行 | **CLOSED** | 自建复现（`r2_adv_verify` §3）：`reduce_missing_qty→REDUCE_QTY_MISSING`；`reduce_negative_qty→NON_POSITIVE_QUANTITY`；`reduce_qty_bool→REDUCE_QTY_INVALID`；`net_delta_missing→NET_DELTA_MISSING`；`net_delta_positive→NET_EXPOSURE_INCREASE`；`close_over_position(100 vs 0.01)→CLOSE_EXCEEDS_AUTHORIZATION+CLOSE_EXCEEDS_POSITION`；`close_empty_position→CLOSE_EXCEEDS_POSITION`；`nonce_replay→NONCE_REPLAY`。golden `K04-N8..N17`；实现 `k04_management.py:196-313` |
| P1-2 | K05 恢复证据缺时间戳/类型混淆仍 RESOLVED | **CLOSED** | `{"heartbeat": True}→EVIDENCE_TYPE_INVALID`；`"false"/"yes"→EVIDENCE_TYPE_INVALID`；`{note}`→`EVIDENCE_TIMESTAMP_MISSING`；`observed_at_ms=True→EVIDENCE_TYPE_INVALID`；新鲜度边界 `now-observed==max_age` 判新鲜（`5g` 正确）；ACK 前证据→`EVIDENCE_STALE+EVIDENCE_PREDATES_ACK`。golden `K05-N5..N8`；实现 `k05_trace.py:208-251` |
| P1-3 | K03 schema 缺 `ZeroFeeProof/PaginationDecision` | **CLOSED** | `K03SchemaEnvelope` 已含 `zero_fee_proof/pagination_decision`（`k03_income.py:682-697`）；6 schema 独立重生成 `EQUAL`；冒烟 schema 断言 PASS |
| P2-1 | K03 COMPLETE+gaps 自相矛盾未被发现 | **CLOSED** | `coverage_problems→['COVERAGE_GAPS_DECLARED']`；`zero_fee_proof→provable=False`；golden `K03-N5/N6/N7`；`k03_income.py:224-232, 260-264` |
| P2-2 | K03 Decimal scale 误判冲突 | **CLOSED** | `income_content_hash` 改用 `decimal_text`（`k03_income.py:109-121`）；`100 vs "100.00"`、`1E+2 vs "100"` 均 `duplicates`；golden `K03-P9` |
| P2-3 | K06 窗口字段类型错误崩溃；状态未 strip | **PARTIAL** | strip 已修（`" PASS "/"pass"→PASS`，`k06_release.py:95`）；`"not-a-number"→FAIL+WINDOW_INVALID`；**但 `float('inf')`（JSON `1e400`/`Infinity`）仍抛 `OverflowError` 崩溃** → 见 NEW-P1-1（同类未修尽） |
| P2-4 | K05 `UNKNOWN→ACKNOWLEDGED` 跳级 | **CLOSED** | `TransitionError: ACKNOWLEDGED requires prior SUBMITTED fact`（`k05_trace.py:362-365`）；golden `K05-N9` |
| P2-5 | 冒烟 `compare()` 只做期望子集比较 | **PARTIAL** | 行为未变（`smoke_contracts_v3.py:721-742`；`L.compare.extra_in_actual→[]`）；已在 README §12 `L10` 显式登记并由 I/Q 决策，属“已登记未修” |
| P2-6 | `allocate_weighted` 负权重/负末腿 | **CLOSED** | 负权重 `ValueError`；`0.000000016` 三等分→`AllocationError: negative share for non-negative source amount`（独立复现，与 golden `K03-N10/N11` 一致）；`k03_income.py:510-528` |
| P2-7 | 幂等键大小写/空白敏感 | **CLOSED** | `normalize_key_part`（strip+casefold）+ `join_key_parts` 转义已接入 K03/K04 键；`"T1" vs " T1"`、`"L\|SL" vs "L\|sl"` 均归一；golden `K03-P12`、`K04-P3/P8` |
| P2-8 | 敏感守卫残余面（值内容/同形字） | **PARTIAL** | 非 Mapping 载荷已拒绝（`ensure_structured_payload`），但全角同形字键（`ｓｅｃｒｅｔ`）与“秘密仅在 value”仍通过；README §12 `L1` 已登记为下游 U15 边界，属“已登记未修” |
| P2-9 | 新脚本 `scripts/smoke_contracts_v3.py` 未登记 | **NOT CLOSED** | `docs/无人值守升级任务清单_2026-09-19.json` 全文仍无 `smoke_contracts_v3`（独立全文检索 0 命中）；U00 `owned_paths` 仍仅 `docs/contracts/`、`scripts/run_offline_regression.py`、`src/core/contracts/`，无 `shared_files` 键；r2 提交未触碰该 JSON |

计数：CLOSED 8 / PARTIAL 3 / NOT CLOSED 1。

## 3. C/B 的 P0 在 `0b85bef` 上的独立复现判定

以下均为**自建探针**（不经作者 golden/冒烟结论），全部在 `_review_scratch/r2_adv_verify.py`。

### 3.1 C P0-1「K02 逐意图完整检查不可达 / 未知 action 绕过」→ CLOSED（保留 1 项 P2 残余）

| 复现 | 输入 | 实测 |
|---|---|---|
| 1a 恒真 claim | 完整 bundle + `1 < 2` | `EVIDENCE_INVALID`，`CLAIM_WITHOUT_EVIDENCE + MISSING_REQUIRED_CLAIM`（r1 CE1 为 `VALID`） |
| 1b 未知 action | `action="OPENX"` | `EVIDENCE_INVALID / UNKNOWN_ACTION`（r1 CE1b 为 `VALID`） |
| 1c 缺必需 kind | `rules.required_claim_kinds=[direction,rr]`，只声明 `direction` | `EVIDENCE_INVALID / MISSING_REQUIRED_CLAIM` |
| 1d 无 claims | `claims=[]` | `MISSING_REQUIRED_CLAIM + MISSING_CLAIMS` |
| 1e 缺绑定 | `bound_inputs=[]` | `MISSING_EVIDENCE_BINDING` |

残余（P2，见 NEW-P2-7）：`kind` 为调用方自证标签，`1f` 中把 `1h:prior_high >= 0` 贴上 `rr` 标签即满足必需 kind → `VALID`。属 README §12 `L2`「intent 仍是自由 dict」的设计边界，但建议在 README 明写“kind 只做标签存在性校验，表达式语义由 U03 模板绑定”。

### 3.2 C P0-2「content_hash 无校验入口 / 事实溯源」→ CLOSED

| 复现 | 实测 |
|---|---|
| 2a 篡改 `in-02.close→999`，保留旧 hash | `validate_bundle → valid=False, (CONTENT_HASH_MISMATCH, DERIVED_FACT_MISMATCH)`；`validate_intent → EVIDENCE_INVALID`（r1 CE2 曾翻 `VALID`） |
| 2b 篡改后再自算 `content_hash` | `valid=False, (DERIVED_FACT_MISMATCH)`（事实重算兜住） |
| 2c 伪造 `DerivedFact(closed_close).value=999` | `(CONTENT_HASH_MISMATCH, DERIVED_FACT_MISMATCH)`（r1 CE12 曾 `VALID`） |
| 2d `content_hash=""` | `CONTENT_HASH_MISSING` |
| 2e `evidence_id` 值绑定 | 同一输入 id 不同取值 → 不同 `evidence_id`（`eb-3a4d51… vs eb-37081a…`），r1 P2「种子不含值」已修 |

实现：`k02_evidence.py:358-446`（`validate_bundle` + `_compute_fact_value`），`validate_intent:667-669` 首过 `validate_bundle`。

## 4. 新发现（P0/P1/P2，含 file:line 与复现）

### NEW-P1-1  K06 窗口字段 `float('inf')` 仍使整份报告崩溃（r2 自称已修的同类未修尽）

- 位置：`src/core/contracts/k06_release.py:60-69`（`_as_int` 的 `except (TypeError, ValueError)` 未覆盖 `OverflowError`）
- 复现（`_review_scratch/r2_k06_json_inf.py`，输入来自普通 JSON 数字，无需构造特殊类型）：

```
json.loads('1e400') -> inf            # Python json 默认把溢出数字解析为 inf
{"window_end_ms": Infinity} -> raised OverflowError cannot convert float infinity to integer
{"window_start_ms": 1e400, "window_end_ms": 2} -> raised OverflowError cannot convert float infinity to integer
```

- 期望依据：README §8「畸形窗口字段（不可解析）→ `FAIL`+`WINDOW_INVALID`，报告不崩溃」；r2 变更原则「任何缺失/畸形/未知/自相矛盾输入都必须拒绝并给出具体 reason_code」；规格 §4 K06「缺证据必须列出具体阻断项」。当前既无 reason_code 也无报告输出，整份 `ReleaseEvidenceReport` 构建失败。
- 修复：`except (TypeError, ValueError, OverflowError)`；建议同时把 `_as_int` 的 `float` 分支限制为有限值（`math.isfinite`），并对 `abs` 超出 `int64` 毫秒范围的值给 `WINDOW_INVALID`。
- 严重度说明：与 r1 P2-3 同类，r1 定级 P2；本报告因“作者已声明该类关闭但实测未关闭、且可由普通 JSON 数字触发”上调为 P1，供 I 复核时按同一标尺裁定。

### NEW-P2-1  K05 未 ACK 的 Incident 可直接 RESOLVED（`EVIDENCE_PREDATES_ACK` 前置被跳过）

- 位置：`src/core/contracts/k05_trace.py:222`（`ack_at = incident.ack_at_ms if incident.status == "ACK" else None`）、`:258-272`（`resolve_incident` 不要求 `status=="ACK"`）
- 复现：`[5a.open_without_ack.resolve] RESOLVED`（`Incident(status="OPEN", ack_at_ms=None)` + 新鲜显式证据 → `RESOLVED`）
- 期望依据：模块 docstring「Incident 的 ACK 只是确认看到，RESOLVED 必须由 ACK 之后新观察到的、带时间戳且类型明确的证据满足」；README §7 同一语义。当 `status != "ACK"` 时“ACK 之后”这一约束整体消失。
- 建议：`resolve_incident` 增加 `status=="ACK"`（或 `ack_at_ms is not None`）前置，否则 `EVIDENCE_PREDATES_ACK`（或新增 `ACK_REQUIRED`）；golden 增“OPEN 直接 resolve 被拒”。

### NEW-P2-2  K05 恢复证据含引用循环/不可序列化结构时抛裸 `ValueError`

- 位置：`src/core/contracts/k05_trace.py:271`（`canonical_hash(dict(recovery_evidence or {}))` 对整份证据做 JSON 哈希）
- 复现：`[5b.circular_evidence] raised ValueError: Circular reference detected`（谓词内条目合法，仅额外键自引用；`check_recovery` 已通过，随后哈希阶段崩溃）
- 期望依据：r2 fail-closed 原则要求结构化拒绝；且异常发生在“已判定可恢复”之后，`resolution_evidence_hash` 无法生成。
- 建议：只哈希谓词内条目（或对 `canonical_json` 的 `ValueError/TypeError` 转 `ResolutionNotAllowedError`）。

### NEW-P2-3  `acknowledge` 可把已 RESOLVED 的 Incident 降级为 ACK

- 位置：`src/core/contracts/k05_trace.py:254-255`（无状态守卫）
- 复现：`[5d.ack_downgrades_resolved] {'before': 'RESOLVED', 'after': 'ACK', 'resolved_at_ms': 1000000}`（状态与残留 `resolved_at_ms` 自相矛盾）
- 建议：仅允许 `OPEN→ACK`；已 `ACK/RESOLVED` 保持幂等或抛 `TransitionError`。

### NEW-P2-4  `AlertDelivery` 允许 UNKNOWN→DELIVERED 且 `submitted_at_ms` 为空

- 位置：`k05_trace.py:358-361`（DELIVERED 只要求 `provider_receipt` + `delivered_at_ms`）、`:320-327`（`SUBMITTED_WITHOUT_TIMESTAMP` 只对 `state=="SUBMITTED"` 生效）
- 复现：`[6a] {'state': 'DELIVERED', 'violations': [], 'submitted': None}`（带凭证与送达时间即可从 UNKNOWN 直达 DELIVERED，且无违规码）
- 期望依据：README §7 阶梯 `UNKNOWN→SUBMITTED→ACCEPTED→DELIVERED→ACKNOWLEDGED`；“前置事实缺失直接 TransitionError”。当前 SUBMITTED 这一级事实可被永久跳过。
- 建议：DELIVERED 亦要求 `submitted_at_ms` 非空（或在跃迁时补记并对“补记”给出显式码）。

### NEW-P2-5  K05 恢复证据 `value` 与 `satisfied` 矛盾时静默取 `value`

- 位置：`k05_trace.py:238`
- 复现：`[5e] {"observed_at_ms": 999000, "value": True, "satisfied": False} → recovered=True`
- 建议：两字段同时存在且不等 → `EVIDENCE_TYPE_INVALID`（自相矛盾输入）。

### NEW-P2-6  K02 claim `expected` 非布尔被静默当作“断言为真”

- 位置：`k02_evidence.py:283`（`expected if isinstance(expected, bool) else None`）、`:791`（`passed = actual == expected if expected is not None else actual`）
- 复现（`expected` 字段逐值）：`True→PASS`；`False→CLAIM_FALSE`；**`"false"→PASS`、`"true"→PASS`、`0→PASS`、`1→PASS`、`None→PASS`**
- 期望依据：r2 变更原则“类型混淆一律拒绝”。调用方写 `"expected": "false"`（意图取反）会被解释为“断言表达式为真”，语义静默翻转。
- 建议：非 `bool` 的 `expected` → `CLAIM_UNRESOLVED` 或新增 `INVALID_CLAIM_EXPECTED`。

### NEW-P2-7  K02 `required_claim_kinds` 仅校验标签存在性（C P0-1 残余）

- 位置：`k02_evidence.py:738-746`
- 复现：`[1f] VALID`（`kind="rr"` 的 claim 实际断言 `1h:prior_high >= 0`）
- 建议：README §4 明写“kind 为调用方声明标签，语义绑定由 U03 模板保证”，或要求 kind 与表达式模板哈希绑定（小版本追加）。

### NEW-P2-8  K03 `finalize_costs(zero_fee_provable=<bool>)` 可绕过 `zero_fee_proof` 的全部条件

- 位置：`k03_income.py:550-600`（`zero_fee_provable: bool`，`:585-593`）
- 复现：`[7a] zero_fee_proof(source=False, pages=False, late=False) → provable=False`；`[7b] finalize_costs(..., zero_fee_provable=True) → cost_status=COMPLETE, ATTRIBUTED`（同一 coverage）
- 期望依据：README §5「无事件时需零费用证明」；`zero_fee_proof` 的 5 个条件（来源/分页/覆盖/无缺口/迟到复查）是唯一判定源。当前调用方可用一个自由布尔跳过 3 个事实条件。
- 建议：参数改为 `zero_fee_proof: ZeroFeeProof`（或三个事实布尔），并在 verdict 中回带其 reason_codes。

### NEW-P2-9  K03 `plan_next_page(page_limit="3")` 类型混淆抛 `TypeError`

- 位置：`k03_income.py:306`（`if page_limit <= 0` 未做类型校验）
- 复现：`[7c] raised TypeError: '<=' not supported between instances of 'str' and 'int'`
- 建议：显式类型/数值边界校验（`INVALID_PAGE_LIMIT`）。

### NEW-P2-10  `content_hash` 是自哈希而非签名（文档缺口）

- 位置：`common.py:210-219`（`model_content_hash`）、`k02_evidence.py:369-373`
- 复现：篡改不影响派生事实的字段（`source/value_ref`）并重算哈希 → `validate_bundle valid=True, reasons=()`；不重算 → `CONTENT_HASH_MISMATCH`。
- 判断：这是自哈希的固有边界（`validate_bundle` 只能证明“内部一致”，不能证明“未被改写”），不构成实现缺陷；但 README §4 未声明该边界，而 DC07「签名后改 qty/SL」需要的是签名/封条。
- 建议：README §4/§12 增一条 `L13`：「`content_hash` 为完整性自校验，非签名；跨进程/持久化可信需 U03/U15 的签名封条（含精确意图集合）」。

### 新发现计数

P0 ×0；P1 ×1（NEW-P1-1）；P2 ×10（NEW-P2-1..10）。

## 5. 新增负例 golden 抽查（≥5 条，独立推导）

方法：只读 golden 的 `inputs/expect/derivation`，**不看实现**，用规格条款 + 手算复算期望；再与实测对照。抽查 8 条：

| 用例 | 期望 | 独立推导 | 结论 |
|---|---|---|---|
| K02-N16 | `EVIDENCE_INVALID / [MISSING_REQUIRED_CLAIM]`，claim c1 `PASS actual=true` | README §4「`rules.required_claim_kinds` 列出的 kind 必须出现」；手算 `744.13 > 743.5` 为真 | 一致；探针 1c 同向 |
| K02-N17 | `CONTENT_HASH_MISMATCH + DERIVED_FACT_MISMATCH` | `content_hash` 覆盖全模型 → 改输入必失配；`1h:closed_close` 必须等于引用输入 close（999≠744.13） | 一致；探针 2a 顺序与集合完全相同 |
| K03-N7 | `INCOMPLETE / [COVERAGE_GAPS_DECLARED]` | README §5「覆盖/缺口问题不 COMPLETE」；watermark=end 但 gaps 非空 | 一致 |
| K03-N11 | `AllocationError` | 手算：1.6e-8÷3=5.33e-9 → HALF_UP 到 1e-8；余 1.6e-8−2e-8=−4e-9 <0 → 与源金额符号矛盾 | 一致（独立脚本复算得 `AllocationError: negative share…`） |
| K04-N8 | `[CLOSE_EXCEEDS_AUTHORIZATION, CLOSE_EXCEEDS_POSITION]` | README §6「CLOSE 受 max_reduce_qty 与当前仓位双重上限」；100>0.01 同时命中两条 | 一致；探针 `3.close_over_position` 同向 |
| K04-N12 | `[NET_DELTA_MISSING]` | README §6「`requested_net_delta` 必填、不得为正」 | 一致；探针 `3.net_delta_missing` 同向 |
| K05-N6 | `EVIDENCE_TIMESTAMP_MISSING` 且 resolve 被拒、`evidence_hash_present=false` | README §7「无时间戳→EVIDENCE_TIMESTAMP_MISSING」 | 一致；探针 `K.dict_no_value` 同向 |
| K05-N8 | `EVIDENCE_TYPE_INVALID` | README §7「裸布尔→EVIDENCE_TYPE_INVALID」 | 一致；探针 `K.no_timestamp_true` 同向 |
| K06-N5 | `FAIL` + `WINDOW_INVALID` 阻断项 | README §8「不可解析窗口→FAIL+WINDOW_INVALID」 | 一致；探针 `4.str_overflow` 同向 |
| K01-N3 | 四组任一为空 → `IdentityCoverageError(组名)` | README §3「`enforce_coverage=True` 下四组任一缺失抛错」 | 一致（独立脚本复现 4/4 组名与 control 通过） |

未发现“复制实现输出”的证据：`derivation` 均引用规格条款/README/公式/交易所语义，且多条期望可由手算复现；`adv_04` 证明翻转期望会立即 FAIL（断言非空转）。

## 6. 未覆盖项（golden/冒烟维度，供冻结前取舍）

以下 reason_code/分支在实现中存在但 113 条 golden 未触达（独立全文检索 0 命中）：

- K02：`BAR_RECEIVED_BEFORE_CLOSE`、`CONTENT_HASH_MISSING`、`CROSS_SYMBOL_FACT`、`CYCLE_MISMATCH`、`DERIVED_FACT_INVALID`、`DUPLICATE_INPUT_ID`、`FINALITY_MISMATCH`、`FUTURE_INPUT`、`INPUT_TIME_ORDER`、`INVALID_SIDE`、`MISSING_SIDE`、`MISSING_CLAIMS`、`QUALITY_MISMATCH`、`QUOTE_EXPIRED`、`QUOTE_NON_FINITE`、`EVIDENCE_BUNDLE_INVALID`
- K03：`AMBIGUOUS_LIFECYCLE`、`COMPLETE_WITHOUT_COVERAGE`、`COMPLETE_WITHOUT_PAGINATION_EVIDENCE`、`GAP_INVALID_RANGE`、`INVALID_RANGE`、`LATE_RECHECK_MISSING`、`NO_MATCHING_LIFECYCLE`、`NO_SOURCE_EVENTS`、`PAGINATION_INCOMPLETE`、`SOURCE_UNAVAILABLE`、`UNATTRIBUTED_EXPOSURE`、`ZERO_FEE_NOT_PROVABLE`
- K04：`ACCOUNT_MISMATCH`、`ACTION_NOT_MANAGEMENT`、`AUTHORIZATION_FROM_FUTURE`、`CLOSE_QTY_INVALID`、`CLOSE_QTY_MISSING`、`ENVIRONMENT_MISMATCH`、`LIFECYCLE_MISMATCH`、`SIDE_MISMATCH`、`POSITION_QTY_INVALID`、`POSITION_QTY_MISSING`、`REDUCE_QTY_INVALID`、`NEGATIVE_QUANTITY`、`FILLED_EXCEEDS_ACCEPTED`、`FILLED_STATUS_WITH_RESIDUAL`、`STATUS_FILL_MISMATCH`、`TERMINAL_NONZERO_WHILE_WORKING`、`WORKING_NONZERO_WHILE_TERMINAL`、`THREE_COMPONENT_CONSERVATION_VIOLATION`
- K05：`ACK_WITHOUT_SUBMISSION`、`ENDED_BEFORE_STARTED`、`EVIDENCE_FROM_FUTURE`、`NEGATIVE_DEADLINE_REMAINING`、`RECOVERY_PREDICATE_FALSE`、`SUBMITTED_WITHOUT_TIMESTAMP`、`TIME_ORDER_VIOLATION`
- K06：`EVIDENCE_HASH_MISSING`
- 其他：`check_conservation` 的 `tolerance` 路径；`apply_late_income` 的正常路径未独立复核（仅 `K03-N13` 多义）；K05 `Incident.ack_at_ms` 与 `status` 组合的一致性无用例。

说明：上述多为“实现已写但无用例”，不构成本次阻断项；但 `MISSING_SIDE/INVALID_SIDE`、`QUOTE_EXPIRED`、`THREE_COMPONENT_CONSERVATION_VIOLATION`、`EVIDENCE_FROM_FUTURE` 属安全相关判定，建议 r3 或 I 冻结前补 5–8 条负例。

## 7. 同模型偏差声明

- 本次 Q 复审与编排者（I）及 r2 作者运行在同一底层模型（`deepseek-v4.1-flash`），属**同模型会话隔离**而非跨模型独立审计；共同盲区无法完全排除（例如对“标签自证”“自哈希≠签名”这类语义边界的敏感度、对 pydantic v2 惯用法的偏好）。
- 本报告全部判定以可独立复算的证据支撑（`0b85bef` 源码行号、真实运行输出、自建探针输入/输出、手算复算），不依赖作者 golden/冒烟结论；但仍建议 I 在集成后用独立夹具复核 NEW-P1-1 与 NEW-P2-1。
- 标尺校准：r1 把 K06 窗口崩溃类缺陷定为 P2，本报告把同类残余上调为 P1（理由见 NEW-P1-1），若 I 认为应维持 P2，则结论可降为“PASS（附 P2 清单）”，但 `_as_int` 的 `OverflowError` 仍应修复。

## 8. 复现命令清单

```powershell
$env:PYTHONUTF8=1
$py = "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe"
git -C "C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1" checkout 0b85bef
& $py scripts\smoke_contracts_v3.py                                   # PASS 126 / FAIL 0；golden 113 (40/73)
& $py _review_scratch\r2_adv_verify.py                                # C/B P0 自建复现 + 10 项新对抗
& $py _review_scratch\r2_k06_json_inf.py                              # NEW-P1-1：JSON 1e400 -> OverflowError
& $py _review_scratch\adv_01_guard_decimal_range.py                   # r1 探针原样重跑
& $py _review_scratch\adv_02_k04_k05_k06_k02.py                       # r1 探针原样重跑
& $py _review_scratch\adv_03_misc.py                                  # r1 探针原样重跑
& $py _review_scratch\adv_04_harness_sensitivity.py                   # 断言敏感性（应 FAIL/False）
& $py -c "import importlib.util; s=importlib.util.spec_from_file_location('r','scripts/run_offline_regression.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m._validate_smoke_manifest()"
```

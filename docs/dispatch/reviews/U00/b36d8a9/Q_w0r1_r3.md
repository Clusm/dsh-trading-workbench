# U00 契约候选 r3 复审报告（Q 独立 QA）

- 会话：Q-w0r1（独立会话，r3 复审）
- 复审对象：候选提交 `b36d8a9`（`fix(contracts): close r2 P0/P1 findings, update gap index and golden changelog (U00 r3; I recovery commit after author session cancellation; smoke PASS 160/0 verified)`）
- 对照基线：r2 候选 `0b85bef` 与作者 r2 报告 `docs/dispatch/reviews/U00/0b85bef/Q_w0r1_r2.md`（本报告作者）；C/B r2 报告同目录
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1`（detached @ `b36d8a9`；已由 `0b85bef` checkout 至此）
- 审查日期：2026-09-21
- 只读约束：未修改任何 tracked 文件（`git status --porcelain` 仅 `?? U00_review_Q_w0r1.md`、`?? U00_review_Q_w0r1_r2.md`、`?? _review_scratch/`）；未进入/读取 `multi-agent-trader\`、运行目录、`data\`、`.env`；未联网；未启动服务；未提交/推送/打标签。临时脚本/输出全部落在 `_review_scratch/`
- 本轮脚本：`r3_adv_verify.py`（r2 问题逐条复跑 + 新对抗）、`r3_adv_extra.py`（K01 畸形覆盖输入 / 幽灵输入 claim / K06 宽松解析）、`r3_golden_case_diff.py`、`r3_golden_expect_history.py`、`r3_golden_input_detail.py`、`r3_k06_json_inf.py`（重跑）；真实输出 `r3_smoke_out.txt`、`r3_adv_verify_out.txt`、`r3_adv_extra_out.txt`、`r3_r1probes_rerun.txt`、`r3_golden_*_out.txt`、`r3_schema_recheck.txt`

## 0. 结论

**CHANGES_REQUESTED（窄口：P0 = 0，P1 = 0，P2 ×6，P3 ×3；无一在 README §12 登记）**

- r2 报告问题处置：**CLOSED 11 / PARTIAL 4 / NOT CLOSED 0**（共 15 项 = r2 新发现 ×11 + r2 遗留的 r1 问题 ×4；另附 r1 已闭合项复验）
- 主体质量：r3 方向正确、覆盖面大。`K06` 非有限/超界窗口完全结构化拒绝；`K05` ACK 前置、降级、循环证据、`value/satisfied` 布尔矛盾全部关闭；`K02` rules/quality 重算、kind 注册表、越界引用、畸形 claims、非 OPEN 授权引用等新校验经独立探针可复现；`K04` 空 `fill_id`/FILLED 撤单/绑定缺失均关闭；golden 147 条（44 正/103 负，+34）与 6 schema 独立重生成全部通过；smoke `PASS 160 / FAIL 0` 与提交声明一致；`adv_04` 证明断言非空转
- 阻断理由：剩余 6 条 P2 均为可独立复现的窄口 fail-open/崩溃（见 §4），其中 `NEW-R3-P2-6` 是 r3 **新引入的回归**（r2 为非 Mapping 覆盖输入抛结构化 `IdentityCoverageError`，r3 变为 `AttributeError` 崩溃）；`NEW-R3-P2-1`（零费用证明可跨窗口复用/字典自报）与 `NEW-R3-P2-4`（空字符串绑定被当已绑定）直接削弱本轮新增的 fail-closed 断言。按任务标尺（仅 P2 且已在 README 已知限制登记才可 PASS-with-notes），当前未达 PASS-with-notes。
- 若 I 选择将上述 6 条登记为 §12 已知限制 + 下游任务（或接受窄口修复后复审），本候选其余部分具备冻结条件；Q 不建议在未处置前直接打标签。

## 1. 实测汇总（真实输出）

| 项目 | 命令 | 结果 |
|---|---|---|
| 契约冒烟 | `& <venv>\python.exe scripts\smoke_contracts_v3.py` | `PASS 160 / FAIL 0；golden cases=147 (positive=44, negative=103)`；6 schema 断言全 PASS；退出码 0（`r3_smoke_out.txt`） |
| schema 独立重生成 | `model_json_schema()` ×6 与仓库文件 `==` | 6/6 `EQUAL`（`r3_schema_recheck.txt`） |
| smoke 清单校验 | 导入 `scripts/run_offline_regression.py` 调 `_validate_smoke_manifest()` | `SMOKE MANIFEST OK included=65 excluded=1` |
| r2 问题复跑 | `_review_scratch\r3_adv_verify.py` | 全项输出见 `r3_adv_verify_out.txt`；重点见 §2/§4 |
| r2 K06 复现重跑 | `_review_scratch\r2_k06_json_inf.py`（未改） | `Infinity→FAIL`、`1e400→FAIL`（r2 为 `OverflowError` 崩溃） |
| r1 四探针原样重跑 | `adv_01..adv_04` | 见 §2；`adv_04` 翻转/篡改敏感性仍 FAIL（断言非空转） |
| golden 期望历史对照 | `r3_golden_expect_history.py`（语义级，忽略 JSON 重排） | r2 改期望 5 例（4 行登记）；r3 改期望 2 例（2 行登记）；增 34 例、删 0 例 |
| 候选提交完整性 | `git rev-parse HEAD` / `git status --porcelain` | `b36d8a9`；工作区仅 3 个未跟踪审查产物，无 tracked 改动 |

## 2. r2 发现逐条判定（CLOSED / PARTIAL / NOT CLOSED）

### 2.1 r2 新发现（Q r2 报告 §4）

| r2 编号 | 问题 | 判定 | 证据（r3 实测） |
|---|---|---|---|
| NEW-P1-1 | K06 `float('inf')`（JSON `1e400`）整报崩溃 | **CLOSED** | `inf/nan/1e400/"1e999999"/10**40/bool` 全部 `FAIL + WINDOW_INVALID` 不崩溃；int64 边界 `-(2**63)` 通过、`2**63` 拒绝；golden `K06-N7`；实现 `k06_release.py:64-78` |
| NEW-P2-1 | 未 ACK 的 Incident 可直接 RESOLVED | **CLOSED** | `OPEN+新鲜证据 resolve → ResolutionNotAllowedError(ACK_REQUIRED)`；ACK 后成功 `RESOLVED` 且写证据哈希；golden `K05-N11`；`k05_trace.py:277-278` |
| NEW-P2-2 | 循环引用证据抛裸 `ValueError` | **CLOSED** | `EVIDENCE_NOT_SERIALISABLE` 结构化拒绝；golden `K05-N14`；`k05_trace.py:282-285` |
| NEW-P2-3 | `acknowledge` 可把 RESOLVED 降级为 ACK | **CLOSED** | `TransitionError: cannot acknowledge resolved incident`；ACK 幂等；golden `K05-N12`；`k05_trace.py:261-267` |
| NEW-P2-4 | UNKNOWN→DELIVERED 且 `submitted_at_ms` 为空 | **PARTIAL** | `transition_state(d,"DELIVERED")` 已 `TransitionError`（`k05_trace.py:378-379`）；但同类跳级在 **ACCEPTED** 仍存在：UNKNOWN+回执 `→ACCEPTED` 直接放行、仅由 `delivery_violations` 报 `SUBMITTED_WITHOUT_TIMESTAMP`（探针 3b）→ 见 NEW-R3-P2-5 |
| NEW-P2-5 | 恢复证据 `value` 与 `satisfied` 矛盾时静默取 `value` | **CLOSED** | `{"value": True, "satisfied": False} → EVIDENCE_TYPE_INVALID`；golden `K05-N13`；`k05_trace.py:240-244`。残留类型混淆见 NEW-R3-P3-1（`satisfied=1` 因 `True==1` 未被识别） |
| NEW-P2-6 | K02 `expected` 非布尔被静默当“断言为真” | **CLOSED** | `"false"/0/None → CLAIM_EXPECTED_INVALID + UNRESOLVED`；golden `K02-N28`；`k02_evidence.py:993-999` |
| NEW-P2-7 | `required_claim_kinds` 仅校验标签存在性 | **CLOSED** | `CLAIM_KIND_REGISTRY` + `claim_kind_consistent`：未知 kind→`CLAIM_KIND_UNKNOWN`，标签与操作符/操作数语义矛盾→`CLAIM_KIND_CONTRADICTION`；golden `K02-N23/N30`；探针 6.kind_label_spoof/kind_unknown |
| NEW-P2-8 | K03 `finalize_costs(zero_fee_provable=<bool>)` 可绕过全部零费用条件 | **PARTIAL** | 自由布尔参数已移除，非 `ZeroFeeProof`/`Mapping` 一律 `ZERO_FEE_NOT_PROVABLE`（探针 5e）；但 `Mapping` 分支仍接受 `{"provable": true}` 自报，且证明与 `coverage` 无绑定、可跨窗口复用（探针 5b/5c）→ 见 NEW-R3-P2-1 |
| NEW-P2-9 | `plan_next_page(page_limit="3")` 抛 `TypeError` | **CLOSED** | `str/True/0/-1 → INVALID_PAGE_LIMIT`；golden `K03-N16`；`k03_income.py:338-345` |
| NEW-P2-10 | `content_hash` 自哈希≠签名，README 未声明 | **CLOSED** | README §12 `L13` 已登记（含下游 U03/U15 签名封条） |
| NEW-P2-参（附加复验，不计入 15 项） | r2 §3 残余：C P0-1 kind 标签自证、C P0-2 事实溯源 | **CLOSED** | 探针 1f 恒真贴标签→`CLAIM_KIND_CONTRADICTION`；`2a/2b/2c` 篡改/伪造/repoint 全部 `EVIDENCE_INVALID`；事实集合唯一且须与重建一致（`k02_evidence.py:591-618`），探针 7d/7e |

### 2.2 r2 报告中判 PARTIAL / NOT CLOSED 的 r1 问题复核

| r1 编号 | r2 判定 | r3 判定 | 证据 |
|---|---|---|---|
| P2-3 | PARTIAL（K06 非有限窗口崩溃） | **CLOSED** | 同 NEW-P1-1；`_as_int` 捕获 `OverflowError` 并加 `math.isfinite` + int64 范围 |
| P2-5 | PARTIAL（smoke `compare()` 期望子集断言） | **PARTIAL（未变，已登记）** | `adv_03 [L.compare.extra_in_actual] → []`；`smoke_contracts_v3.py:812-833` 仍为子集比较；README `L10` 仍登记 |
| P2-8 | PARTIAL（敏感守卫残余面：全角同形字键、秘密仅在 value） | **PARTIAL（未变，已登记）** | `adv_01`：`tuple_homoglyph_fullwidth` 仍 HASHED、`secret_in_value_only` 仍 HASHED；README `L1` 登记 |
| P2-9 | NOT CLOSED（`smoke_contracts_v3.py` 未登记） | **CLOSED** | `docs/无人值守升级任务清单_2026-09-19.json` 全文 1 处命中 `smoke_contracts_v3`，位于 U00 `owned_paths`；manifest 仍 `OK 65/1` |
| P2-1/2/4/6/7（r2 已闭合；附加复验，不计入 15 项） | CLOSED | **CLOSED（复验）** | `coverage_problems→COVERAGE_GAPS_DECLARED`（探针 9a）；`attribute_income` COMPLETE+gaps→`UNKNOWN`（9b，需事件存在）；scale/hash 归一（adv_01 B.*）；K05 跳级/类型（§2.1）；负权重/末腿（adv_03 I.*）；键规范化（adv_01 D.*） |

计数（15 项）：**CLOSED 11 / PARTIAL 4 / NOT CLOSED 0**。PARTIAL = NEW-P2-4、NEW-P2-8、r1-P2-5、r1-P2-8。

## 3. 任务重点项验证

### 3.1 K06 1e400/inf 不崩溃 —— 通过
`r2_k06_json_inf.py` 原样重跑：`json.loads("Infinity")/("1e400")` 均为 `inf`，`build_release_report` 返回 `FAIL`（含 `WINDOW_INVALID` 阻断项），不再抛异常；`r3_adv_verify` §1 覆盖 `inf/nan/1e400/字符串溢出/超大整数/bool/int64 边界`。实现 `k06_release.py:64-78`。

### 3.2 K05 状态跳级 / ACK 前置 / value-satisfied 矛盾 —— 通过（1 项同类残留）
`OPEN→resolve` 拒绝（ACK_REQUIRED）；`OPEN→ACK` 幂等、`RESOLVED→ack` 拒绝；ACK 前证据 `EVIDENCE_PREDATES_ACK`；`{"value": true, "satisfied": false}` 拒绝；循环证据 `EVIDENCE_NOT_SERIALISABLE`。残留：UNKNOWN→ACCEPTED 无 submitted 事实（NEW-R3-P2-5）。

### 3.3 K02 新校验 —— 通过（2 处边界见 §4）
可复现点：`rules` 与 `bundle.quality` 逐项比对（stale/kinds/max_bar_age 抹除→`RULES_MISMATCH`，探针 7f/7g）；质量字段全量重算（undated/unclosed/symbol/stale/bar_times/finality）；事实集合唯一 + 重建一致（重复 id→`DUPLICATE_FACT_ID`，换指/删集合→`DERIVED_FACT_MISMATCH`）；kind 注册表与语义一致；`expected` 显式布尔；畸形 claims 结构化拒绝；非 OPEN 缺 `management_authorization_id`→`NEEDS_AUTH`；claim 越界引用→`CLAIM_OUTSIDE_BOUND_INPUTS`。边界：`bound_inputs=[]` 时越界检查整体跳过（NEW-R3-P2-2）；`symbol=None` 幽灵输入通过自洽校验（NEW-R3-P2-3）。

### 3.4 K04 空 fill_id —— 通过
`""/"   "/5` → `QuantityError`；重复非空 ID → `DuplicateFillError`；`None` 仍为“无 ID 幂等”路径（可选语义，与 README“必须是非空字符串”表述略有张力，仅记为文字口径）；FILLED 撤单/撤单确认均 `QuantityError`，`FILLED_WITH_CANCEL_CONFIRMED` 违规码落地（golden `K04-P9/N22..N27`）。绑定：`runtime_instance_id/environment` 缺失→`RUN_GENERATION_UNBOUND/ENVIRONMENT_UNBOUND`；但空字符串被当已绑定（NEW-R3-P2-4）。

### 3.5 golden changelog 是否逐条登记 —— 期望变更全部登记；输入变更登记不完整
- r2→r3 期望变更实测 **2 条**：`K01-N3`（组级标签→字段级缺口）、`K03-N1`（`PAGINATION_NOT_TERMINATED`→`TERMINATION_PROOF_MISSING`）——§11 changelog 均逐条登记且变更前后/依据一致。
- r1→r2 期望变更实测 5 条（4 行）：`K03-P1/P6`、`K04-N6`、`K04-P2`、`K05-N4`——r3 §11 恢复表全部登记（任务所述“4 条被改期望”即此 4 行；K03-P1/P6 合并一行）。
- 输入变更登记不完整（P3，见 NEW-R3-P3-3）：`K01-P3/P4/P5`（`model_route.fallback_routes/profile_overrides`、`effective_config.*` 由 `null`→`[]/{}`）与 `K03-N5`（新增 `termination_proof`）未逐条出现；`K04` 行以“全部非 OPEN 授权用例”泛化覆盖（实测 16 例）。
- 用例总量：113 → 147（+34：K01 +2、K02 +13、K03 +6、K04 +7、K05 +5、K06 +1）。

## 4. 新发现（r3 引入或残留；P2 ×6 / P3 ×3）

### NEW-R3-P2-1 K03 `finalize_costs` 的零费用证明可字典自报且可跨窗口复用（r2 NEW-P2-8 残留）
- 位置：`src/core/contracts/k03_income.py:605-606`（`elif isinstance(zero_fee, Mapping): proof = ZeroFeeProof.model_validate(zero_fee)`）、`:641-646`
- 复现（`r3_adv_verify` §5）：
  - `[5c] finalize_costs("lc-1", coverage=cov_a, zero_fee={"provable": True, "reason_codes": []}) → cost_status=COMPLETE, attribution_status=ATTRIBUTED`（无需任何事实）
  - `[5b] proof_b 为 cov_b（5000–6000）的合法证明，却用于 cov_a（0–1000）→ COMPLETE`
- 期望依据：README §5「无事件时必须传 `ZeroFeeProof` 结果对象（不得自报布尔）」、r3 变更原则「零费用证明必须结构化并与窗口绑定」。当前事件证明与 `coverage` 参数零绑定。
- 建议：`finalize_costs` 拒绝 Mapping（仅收对象）并在 `ZeroFeeProof` 内加 `coverage_hash/start_ms/end_ms` 回带、与入参 `coverage` 比对；或登记 §12 `L16`（证明新鲜度/窗口绑定由 U01 持久化保证）。

### NEW-R3-P2-2 K02 `bound_inputs` 为空时越界引用检查整体禁用
- 位置：`src/core/contracts/k02_evidence.py:951-954`（`if bound_inputs:` 才做 `claim_refs ⊆ bound_set`）
- 复现（`r3_adv_verify` §6）：CLOSE 意图 `bound_inputs=[]` + `management_authorization_id="auth-1"` + 引用 `in-01/in-02` 的 claim → `status=VALID`（未报 `CLAIM_OUTSIDE_BOUND_INPUTS`）
- 期望依据：README §4「claim 引用必须 ⊆ `bound_inputs`（`CLAIM_OUTSIDE_BOUND_INPUTS`）」未以“非空”为前提；OPEN 空绑定另有 `MISSING_EVIDENCE_BINDING`，但 CLOSE/REDUCE/ADJUST 无此兜底。
- 建议：无条件执行子集检查（`ref not in bound_set` 即报），空绑定下任何引用必然越界。

### NEW-R3-P2-3 K02 允许 `symbol=None` 的输入存在并被 claim 引用（与 build_bundle 输出不可对照）
- 位置：`src/core/contracts/k02_evidence.py:408-418`（`_closed_inputs_by_interval` 要求 `symbol == bundle.symbol`，None 被排除）、`:475-478`（`symbol_mismatch` 仅查 `symbol is not None`）
- 复现（`r3_adv_extra` §2）：在合法 bundle 上追加 `input_id="ghost", symbol=None, finality=closed, interval="1h", bar_close_time_ms=2500, values={"close":"9.5"}` 并重算 `content_hash` → `validate_bundle → valid=True`；OPEN 意图 `bound_inputs=["ghost"]`、claim `ghost.close > 5` → `VALID`
- 期望依据：`validate_bundle` 文档/README §4「与 `build_bundle` 的输出逐项对照」；`build_bundle` 只会产出 `symbol == bundle.symbol` 的输入（`:691,704`），None 是不可达形态。该输入绕过 `INPUT_SYMBOL_MISMATCH` 与 quality `symbol_mismatch_inputs`，却可作 claim 证据。
- 建议：`validate_bundle` 对 `item.symbol != bundle.symbol`（含 None）一律报 `INPUT_SYMBOL_MISMATCH`/`QUALITY_MISMATCH`；或在 `EvidenceInput` 上要求 closed 输入必须带 symbol。
- 说明：content_hash 为自哈希（`L13`），伪造整包本就可自洽；此条是“内部一致性证明”的完整性缺口，非新增攻击面，故列 P2 末位。

### NEW-R3-P2-4 K04 空字符串 `runtime_instance_id/environment` 被当作已绑定
- 位置：`src/core/contracts/k04_management.py:254-259`（`is None` 判定）、`:264-269`（请求侧 `str(... or "")`）
- 复现（`r3_adv_verify` §4）：`ManagementAuthorization(runtime_instance_id="", environment="")` + 请求缺两字段 → `allowed=True, reasons=[]`
- 期望依据：README §6「授权缺 `runtime_instance_id`→`RUN_GENERATION_UNBOUND`、缺 `environment`→`ENVIRONMENT_UNBOUND`；请求不符→MISMATCH」；r3 变更原则“绑定缺失即拒绝”。空串是“事实未绑定”的常见配置默认值。
- 建议：判定改为 `not authorization.runtime_instance_id` / `not authorization.environment`（空串视同缺失）。

### NEW-R3-P2-5 K05 `UNKNOWN→ACCEPTED` 仍可跳过 SUBMITTED 事实（NEW-P2-4 同类残留）
- 位置：`src/core/contracts/k05_trace.py:376-377`（ACCEPTED 只校验 `provider_receipt`）
- 复现（`r3_adv_verify` §3b）：`AlertDelivery(UNKNOWN, provider_receipt="rcpt")` → `transition_state(..., "ACCEPTED")` 成功，`delivery_violations → ("SUBMITTED_WITHOUT_TIMESTAMP",)`（可见但不抛错）
- 期望依据：README §7 阶梯 `UNKNOWN→SUBMITTED→ACCEPTED→DELIVERED…`、`transition_state` 前置事实缺失直接 `TransitionError`（不得先提升后补证）。
- 建议：ACCEPTED 亦要求 `submitted_at_ms` 非空（或在跃迁时补记并对“补记”给显式码）。

### NEW-R3-P2-6 K01 `build_identity` 对非 Mapping 覆盖输入抛 `AttributeError`（r3 回归）
- 位置：`src/core/contracts/k01_identity.py:169-178`（`route_payload.get(field)` / `effective_config.get(field)` 无 Mapping 形态校验）
- 复现（`r3_adv_extra` §1）：
  - `build_identity(..., model_route=["model","effort","fallback_routes","profile_overrides"])` → `AttributeError: 'list' object has no attribute 'get'`
  - `model_route="model_effort_fallback_routes_profile_overrides"`、`effective_config=["settings","profile_overrides","environment_overrides"]`、对应字符串 → 同样 `AttributeError`
  - 对照 r2：非 Mapping 路径由 `hash_model_route → _structured_mapping` 抛 `IdentityCoverageError(["model_route"])`（结构化拒绝）；r3 在 `_coverage_gaps` 提前崩溃。
- 期望依据：契约 fail-closed 原则「畸形输入必须结构化拒绝并给出具体 reason/字段，不得崩溃」；README §3 字段级覆盖。
- 建议：`_coverage_gaps` 先校验 `isinstance(payload, Mapping)`，否则直接计入缺口（如 `model_route.*`）而不调用 `.get`。

### NEW-R3-P3-1 K05 `value/satisfied` 的 int↔bool 类型混淆未拒绝
- 位置：`k05_trace.py:242`（`entry["value"] != entry["satisfied"]`，`True == 1` 故不判矛盾）
- 复现：`{"observed_at_ms": 999000, "value": True, "satisfied": 1} → recovered=True`（`satisfied` 非布尔被静默忽略）；`value=False, satisfied=0` → `RECOVERY_PREDICATE_FALSE`（底线正确）
- 建议：两字段均须 `isinstance(..., bool)`；或任一非布尔即 `EVIDENCE_TYPE_INVALID`。影响低（真值仍须显式布尔），P3。

### NEW-R3-P3-2 K02 `quality.dedup_removed` 不参与重算，可任意改写
- 位置：`k02_evidence.py:559-585`（必需键只查存在；重算列表不含 `dedup_removed`）
- 复现：`quality.dedup_removed=["not-a-real-input"]` + 重哈希 → `valid=True`
- 判断：`dedup_removed` 是审计字段，未参与判定，无直接安全影响；但与 README §4“质量字段全量重算”表述不完全一致。建议 README 明写“dedup_removed 为审计记录、不重算”，或按其与 inputs 去重语义重算。P3。

### NEW-R3-P3-3 golden changelog 对“输入变更”登记不完整
- `K01-P3/P4/P5`（`model_route.fallback_routes/profile_overrides`、`effective_config.*` 由 `null`→`[]/{}`）与 `K03-N5`（新增 `termination_proof`）未逐条登记；`K03` 行列举 7 例但实测 8 例（缺 N5）。期望值均未变（不违反 §10 硬规则），仅登记口径不完整。P3。

### 新发现计数
P0 ×0；P1 ×0；P2 ×6（NEW-R3-P2-1..6）；P3 ×3（NEW-R3-P3-1..3）。

## 5. 新增负例 golden 抽查（≥5 条，独立推导）

方法：只读 golden 的 `inputs/expect/derivation`，**不看实现**，按 README/规格手算复算期望；再与实测对照（smoke 已全过）。抽查 9 条新增负例：

| 用例 | 独立推导（手算/规格） | 与实测 |
|---|---|---|
| K02-N23 | 两 bars：prior_high=743.5、closed_close=744.13。c1 kind=`mystery` 不在注册表→`CLAIM_KIND_UNKNOWN`；c2 kind=`close_above_prior_high` 但表达式用 `<`（注册表要求 `>`）→`CLAIM_KIND_CONTRADICTION`；两者均 UNRESOLVED | 一致 |
| K02-N25 | 把 `1h:closed_close` 换成 in-01（close=742.8）并自洽 value；重建规则要求取最新 bar（in-02=744.13）→`DERIVED_FACT_MISMATCH`；claim 742.8 > 743.5 为假→`CLAIM_FALSE` | 一致 |
| K02-N29 | 追加 closed 但 `bar_close_time_ms=None` 的输入→`UNDATED_BAR`；其不在声明 `undated_inputs`→`QUALITY_MISMATCH` | 一致 |
| K03-N15 | COMPLETE、watermark=end、无 gaps、三布尔全 true，但只有自由文本 `pagination_terminated_evidence`（无 `termination_proof`）→`TERMINATION_PROOF_MISSING` | 一致 |
| K03-N18 | proof 窗口 end=1789865999000 ≠ coverage end=1789866000000，short page 与 terminated_at 均满足→仅 `TERMINATION_PROOF_MISMATCH` | 一致 |
| K03-N19 | proof 窗口一致，`last_page_size=1000 >= page_limit=1000`→`TERMINATION_PROOF_INVALID`；terminated_at≥end 满足 | 一致 |
| K04-N24 | `fill_id=""` 在旧实现 `if fill_id` 短路；非空校验后必抛 `QuantityError`（0.4≤1 不触发其他错） | 一致 |
| K05-N15 | DELIVERED 有回执/送达时间、无 submitted：推导状态 DELIVERED；`SUBMITTED_WITHOUT_TIMESTAMP` 违规；`transition→DELIVERED` 拒绝 | 一致 |
| K06-N7 | JSON `1e400`→`inf`；窗口须有限 int64→`FAIL`+`WINDOW_INVALID`（不崩溃） | 一致 |

未发现“复制实现输出”证据：推导均可由 README §3–§8 条款与手算复现；`adv_04` 翻转期望立即 FAIL。

## 6. 未覆盖项与遗留（供冻结前取舍）

- r2 §6 所列 reason_code 分支仍有相当部分未由 golden 触达（r3 新 golden 已补 `ACCOUNT_MISMATCH/ENVIRONMENT_MISMATCH/INSUFFICIENT_POSITION/INVALID_PAGE_LIMIT` 等；其余如 K02 `BAR_RECEIVED_BEFORE_CLOSE/INPUT_TIME_ORDER/FUTURE_INPUT/QUOTE_EXPIRED`、K04 `AUTHORIZATION_FROM_FUTURE/CLOSE_QTY_*/POSITION_QTY_*/ACTION_NOT_MANAGEMENT`、K05 `ACK_WITHOUT_SUBMISSION/ENDED_BEFORE_STARTED`、K06 `EVIDENCE_HASH_MISSING` 仍无 golden；`THREE_COMPONENT_CONSERVATION_VIOLATION` 已由 Q 探针触达但未收 golden），多属“实现已写但无用例”，不构成本轮阻断。
- 本轮新增建议补例：`bound_inputs=[]` + claim（P2-2）、`symbol=None` 输入（P2-3）、空串绑定（P2-4）、ACCEPTED 跳级（P2-5）、零费用证明跨窗口（P2-1）、K01 非 Mapping 覆盖输入（P2-6）。
- README `L10`（子集断言）与 `L1`（敏感守卫）维持 PARTIAL，属已登记未修；`L13/L14/L15` 为本轮新增的诚实登记，认可。
- `K06._as_int` 对 `1.5` 截断为 1（宽松解析）、`"+5"/" 5 "` 接受——行为宽松但 README 已声明“宽松解析”，未列为问题（如需严格化属小版本行为变更）。

## 7. 同模型偏差声明

- 本轮 Q 复审与编排者（I）及 r3 恢复提交涉及同一底层模型（`deepseek-v4.1-flash`），属**同模型会话隔离**而非跨模型独立审计；共同盲区无法完全排除（如对“自报证明”类符号学边界、pydantic 惯用法、畸形输入的恐慌阈值）。
- 全部判定以可独立复算证据支撑（`b36d8a9` 源码行号、真实运行输出、自建探针输入/输出、手算复算），不依赖作者 golden/冒烟结论；建议 I 在集成后用独立夹具复核 NEW-R3-P2-1/2/4/6。
- 标尺一致性：r2 将 K06 崩溃类定为 P1（“已声明修复但未修复”）；本轮 K01 `AttributeError` 为同类回归，但因输入形态为内部调用方畸形参数（非数据面可达），按 P2 裁定并显式提请 I 复核定级。

## 8. 复现命令清单

```powershell
$env:PYTHONUTF8=1
$py = "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe"
git -C "C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1" checkout b36d8a9
& $py scripts\smoke_contracts_v3.py                 # PASS 160 / FAIL 0；golden 147 (44/103)
& $py _review_scratch\r3_adv_verify.py              # r2 问题逐条复跑 + 新对抗
& $py _review_scratch\r3_adv_extra.py               # K01 畸形覆盖 / 幽灵输入 / K06 宽松解析
& $py _review_scratch\r2_k06_json_inf.py            # K06 JSON 1e400/inf（不崩溃）
& $py _review_scratch\adv_01_guard_decimal_range.py # r1 探针原样重跑
& $py _review_scratch\adv_02_k04_k05_k06_k02.py
& $py _review_scratch\adv_03_misc.py
& $py _review_scratch\adv_04_harness_sensitivity.py
& $py _review_scratch\r3_golden_case_diff.py        # 0b85bef..b36d8a9 逐用例 expect/inputs 变化
& $py _review_scratch\r3_golden_expect_history.py   # r1→r2→r3 期望变更历史
& $py -c "import importlib.util; s=importlib.util.spec_from_file_location('r','scripts/run_offline_regression.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m._validate_smoke_manifest()"
```

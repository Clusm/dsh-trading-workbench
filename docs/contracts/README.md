# K01–K06 冻结契约 v3.1（U00 r5）

状态：FROZEN 候选（本版为 r5 修复轮，关闭 r4 复审的 2 项 P2 与 4 项 P3；r2 复审
Q/C 窄口 CHANGES_REQUESTED、B 有条件 GO；待 Q/C/B/I 复审后按流程打 `contract-v3.x` 标签）。
复审报告：`docs/dispatch/reviews/U00/0b85bef/{Q,C,B}_w0r1_r2.md`（r2）、
`docs/dispatch/reviews/U00/b36d8a9/{Q,C,B}_w0r1_r3.md`（r3）、
`docs/dispatch/reviews/U00/5ee082b/Q_w0r1_r4.md`（r4）。
来源：[无人值守交付与多 Agent 执行规格 v3](../无人值守交付与多Agent执行规格_2026-09-19.md) §4/§10/§11.1/§12/§13；
[决策链路专项评估与优化报告](../决策链路专项评估与优化报告_2026-09-19.md) §3/§5/§7/§9；U00 任务卡与 W0 指纹差异清单
（`docs/dispatch/reviews/U00/w0/F_fingerprint_r1.md`）。

r3 变更原则：继续 fail-closed。K06 非有限窗口不得崩溃；K02 规则/质量字段必须由验证方持有并可比对；
非 OPEN 意图必须携带管理授权引用；K04 运行代/环境绑定缺失即拒绝；K05 必须先 ACK 才能 RESOLVED；
零费用证明必须结构化并与窗口绑定。既有 golden 期望的每处修改都在 §11 changelog 逐条登记。

r4/r5 变更原则（同一 fail-closed 方向，r4 引入、r5 收口）：
`rules` 对 `validate_bundle`/`validate_intent` 为**必传**（`RULES_MISSING`），且其**内容**必须可
规范化为 JSON（非 JSON 值/混合键型 → `RULES_MISMATCH`，不抛裸 `TypeError`）；
`finalize_costs` **不信任**调用方 `ZeroFeeProof.provable`，一律由入参 coverage 重算可复核条件；
`EvidenceBundle.dropped_input_ids` 落地为审计字段（形态受校验、集合不可重算，§12 L18）；
K01 未知键按 `组.unknown_field:<键>` 报告。本节所有收紧均在 §10/§11 登记。

## 1. 内容与边界

| 路径 | 内容 |
|---|---|
| `src/core/contracts/common.py` | 规范化 JSON 哈希、Decimal 边界、敏感键名守卫、结构化载荷守卫、键归一化/转义、`[start,end)` 工具 |
| `src/core/contracts/k01_identity.py` | RuntimeIdentity、ModelRoute、字段级覆盖清单强制、身份不符判定 |
| `src/core/contracts/k02_evidence.py` | EvidenceBundle/EvidenceInput/DerivedFact、`build_bundle`、`validate_bundle`、`validate_intent`、claim kind 注册表 |
| `src/core/contracts/k03_income.py` | 收入幂等、覆盖证明、TerminationProof、分页决策、归因、守恒、迟到修订 |
| `src/core/contracts/k04_management.py` | ManagementIntent/ManagementAuthorization/OrderQuantity 三分量 |
| `src/core/contracts/k05_trace.py` | DecisionTraceSpan/DecisionCounters、Incident 恢复、AlertDelivery 阶梯 |
| `src/core/contracts/k06_release.py` | 五组 ReleaseEvidence 状态与阻断项清单 |
| `docs/contracts/*.schema.json` | 六个 JSON schema（含 `BundleVerdict`/`ModelRoute`/`TerminationProof`/`ZeroFeeProof`/`PaginationDecision`），冒烟校验与模型同步 |
| `docs/contracts/golden/*.golden.json` | 160 条正反 golden（45 正 / 115 反），每条含规格推导 `derivation` |
| `scripts/smoke_contracts_v3.py` | 离线执行 golden 与 schema 同步断言；失败退出码非 0 |
| `scripts/run_offline_regression.py` | 统一回归入口，已登记本冒烟 |

契约包为纯函数 + pydantic v2 模型：不读取 `.env`/`data`/运行状态，不构造 Broker，不发网络请求，
不写入运行目录。真实数据由运行时模块通过显式适配器转换为“有效值字典”后传入。

## 2. 通用约定

- UTC 毫秒整数用于持久化事件时间；本进程期限使用调用方提供的 `now_ms`，契约不读墙钟。
  **K02 `validate_intent` 必须显式传 `now_ms`**（缺省回退 `bundle.as_of_ms` 只用于离线重放；
  真实校验时刻晚于 `as_of_ms` 时缺省会漏判 EXPIRED/STALE，由 U03 调用约定强制）。
- 新增成本/数量计算一律 `Decimal`，边界以十进制字符串序列化；`float` 直传金额抛
  `NumericInputError`，需显式使用 `decimal_from_float`。现有 `TradeIntent`/SQLite REAL 保持兼容。
- 模型均为 `frozen=True, extra="forbid"`；未知字段报错而不是静默丢弃。
- 哈希输入必须是结构化 Mapping（或 Mapping 序列）；原始字符串（如 `.env` 原文）与字符串列表
  （行列表）抛 `SensitiveInputError`。键名含 `key/secret/token/password/credential/authorization/cookie`
  等子串（大小写不敏感）同样拒绝；守卫是保守子串匹配，**值内容不做扫描**（见 §12）。
- 区间语义固定 `[start, end)`；对官方包含端点的接口，适配层用 `endTime = end - 1ms`。
- 幂等键组件统一 `strip + casefold`，并以转义后的 `|` 连接（组件含 `|` 不会碰撞）。

## 3. K01 RuntimeIdentity

字段：`schema_version, environment, account_ref, artifact_hash, git_commit, dependency_hash,
effective_config_hash, strategy_hash, model_route_hash, runtime_instance_id`。

| 字段 | 冻结语义 |
|---|---|
| `artifact_hash` | 不可变代码发布目录内容哈希；输入 `相对路径 -> 内容`，**不含 git_commit** |
| `git_commit` | 独立身份字段；不参与文件内容哈希，但参与身份比较 |
| `dependency_hash` | 必须覆盖实际锁文件（`requirements-lock.txt`）；比对结果另存 `comparison_status` |
| `strategy_hash` | 实际策略卡内容（`knowledge/strategy_cards.json`）；改卡即改身份 |
| `model_route_hash` | 模型名/推理档/备用路由/profile 覆盖值（解析路由后的有效值，非原始 yaml） |
| `effective_config_hash` | settings + profile + 环境覆盖后的有效非敏感值 |
| `runtime_instance_id` | 进程级运行代；身份比较默认忽略（`IDENTITY_MISMATCH_IGNORED_FIELDS`） |

接口：`hash_artifact/hash_dependencies/hash_strategy/hash_model_route/hash_effective_config`、
`build_identity(...)`、`identity_mismatches(expected, actual, ignore=...)`、`validate_coverage(payload)`、
`RuntimeIdentity.identity_hash()`（全字段）/`deployment_hash()`（忽略运行代）。

- **字段级覆盖强制**：`build_identity(enforce_coverage=True)`（默认）下逐字段校验——
  `dependency.requirements_lock`（锁文件名必须出现）、`strategy.strategy_cards`、
  `model_route.{model,effort,fallback_routes,profile_overrides}`、
  `effective_config.{settings,profile_overrides,environment_overrides}`；缺失逐项列入
  `IdentityCoverageError`（空组是合法声明，字段缺失仍拒绝）。离线迁移可用 `enforce_coverage=False`
  但需在审计中说明。
- **覆盖口径（r4 冻结，Q NEW-D / C NR-3 残余）**：非 Mapping 载荷（列表/字符串/数值）不调用
  `.get`，直接按 `组.*` 缺口结构化拒绝；Mapping 载荷中的**未知键**按
  `组.unknown_field:<键>` 报告（如 `model_route.unknown_field:junk_field`）；字段为 `None`、
  空串/空白串按 `组.<字段>` 报告，`model/effort` 另要求非空字符串。同一口径由
  `build_identity` 强制；`validate_coverage` 只报缺失/空串、不报未知键（build 侧更严，
  方向 fail-closed，见 §12 L19）。
- **`STRATEGY_SOURCE_FILES`**：仅是“策略卡来源文件”的声明标签（`knowledge/strategy_cards.json`），
  **不参与** `_coverage_gaps` 文件级校验——`strategy_cards` 载荷是卡片内容而非 `路径 -> 内容`
  映射（对比 `dependency_lock_files`），无法从内容复核文件来源。见 §12 L16。
- **ModelRoute**：类型化解析路由（`model/effort/fallback_routes/profile_overrides`，前两者非空、
  后两者可为显式空）；`hash_model_route` 接受 ModelRoute 或结构化 Mapping；capability/mock 标志
  属 U15/DL01 扩展面（§12 C8）。
- `validate_coverage` 只把字段缺失（None）或空字符串算缺口；显式空集合/空字典表示“已声明且当前为空”。
- 小版本兼容比较可把 `schema_version` 显式加入 `ignore`。

## 4. K02 EvidenceBundle 与逐意图校验

- `build_bundle(snapshot, closed_bars, rules)`：只从已收盘 K 线生成事实；
  - 收盘时间晚于 `as_of_ms` 或缺失 `close_time_ms` 的 bar 一律不判 closed（缺时间记 `UNDATED_BAR`）；
  - 跨 symbol 输入被隔离：不进入事实、`bar_times` 与最终性计算，并记入
    `quality.symbol_mismatch_inputs`，被引用时给 `INPUT_SYMBOL_MISMATCH`；
  - 重复内容按 `(symbol, interval, close_time, values)` 去重；被丢弃的 input_id 记入
    `dropped_input_ids`（审计字段，纳入 `content_hash`）；`quality` 记录
    `missing_intervals / stale_inputs / dedup_removed / unclosed_inputs / undated_inputs /
    symbol_mismatch_inputs / required_intervals / required_claim_kinds / max_bar_age_ms /
    as_of_ms / rules_hash`；`quality.dedup_removed` 恒为空（不可重算，§12 L18）；
  - 畸形 `rules`（非 Mapping，或 Mapping 内容含 `set/bytes/object/datetime` 等非 JSON 值、
    `{1:'x'}` 这类混合键型）抛 `RulesInputError`（结构化拒绝），不会退化成裸
    `TypeError`/`AttributeError`（r5，NEW-R4-P2-1）；
  - `evidence_id` 未显式提供时由 cycle/symbol/as_of/输入 id 与值摘要派生（值变化即 id 变化）。
- `validate_bundle(bundle, rules=None, identity=None) -> BundleVerdict`：**K02 完整性入口**。
  与 `build_bundle` 输出逐项对照：`content_hash` 重算、按最新/次新 bar 规则重建全部事实
  （id/kind/inputs/value，重复 id→`DUPLICATE_FACT_ID`、换指→`DERIVED_FACT_MISMATCH`）、
  missing/stale/unclosed/undated/symbol/bar_times/finality 全部质量字段重算、
  quality 必需键缺失→`QUALITY_MISSING_FIELD`。**`rules` 为必传参数**（r4 收紧，见 §10）：
  缺省 `None` → `RULES_MISSING`；非 Mapping → `RULES_MISMATCH`；Mapping 内容不可规范化为
  JSON（非 JSON 值/混合键型）→ 同样 `RULES_MISMATCH` 且不再逐字段比对；
  可规范化时 `required_intervals/required_claim_kinds/max_bar_age_ms` 与 bundle.quality
  逐项比对、整体哈希与 `quality.rules_hash` 比对，不一致→`RULES_MISMATCH`
  （阻止“改 rules/quality 重哈希即 VALID”）。其余 reason：
  `RULES_MISSING/CONTENT_HASH_MISMATCH/CONTENT_HASH_MISSING/DERIVED_FACT_INVALID/
  CROSS_SYMBOL_FACT/DUPLICATE_INPUT_ID/QUALITY_MISMATCH/FINALITY_MISMATCH/INPUT_TIME_ORDER/
  BAR_RECEIVED_BEFORE_CLOSE/UNDATED_BAR/IDENTITY_MISMATCH`。
- `dropped_input_ids` 的形态校验（r4/r5）：必须是 `tuple`/`list`（非序列形态如
  `model_copy` 写入的 `int`、或裸字符串 → `QUALITY_MISMATCH`，不得逐字符迭代）、元素为
  非空 `str`、唯一、与保留输入 ID 不相交，否则 `QUALITY_MISMATCH`。该字段是**审计提示**：
  被丢弃输入的内容已不在包内，其集合无法由 `inputs` 重算（§12 L18）。
- `validate_intent(intent, bundle, execution_quote, now_ms=None, rules=None)`：先过 `validate_bundle`
  （失败直接 EVIDENCE_INVALID），再逐 intent 校验：
  - `intent_id` 必填；`action ∈ {OPEN,CLOSE,REDUCE,ADJUST}`、`side ∈ {LONG,SHORT}` 否则
    `UNKNOWN_ACTION/MISSING_SIDE/INVALID_SIDE`；
  - **OPEN** 必须声明 `bound_inputs`（`MISSING_EVIDENCE_BINDING`）与非空 claims（`MISSING_CLAIMS`）；
    **CLOSE/REDUCE/ADJUST** 必须携带 `management_authorization_id`（K04 授权引用），否则 `NEEDS_AUTH`；
  - claim 只能是 `lhs op rhs` 单比较（op 有限集），操作数必须引用事实/输入值；两个字面量
    （如 `1 < 2`）判 `CLAIM_WITHOUT_EVIDENCE`；claim 引用必须 ⊆ `bound_inputs`
    （`CLAIM_OUTSIDE_BOUND_INPUTS`）；`expected` 必须是显式布尔（`CLAIM_EXPECTED_INVALID`）；
    非 Mapping 的 claim 元素→`CLAIM_MALFORMED`（结构化拒绝，不崩溃）；
  - **claim kind 有限注册表**（`CLAIM_KIND_REGISTRY`：`close_above_prior_high/close_above_price/
    close_below_price`）：kind 必须与操作符及操作数语义一致，未知→`CLAIM_KIND_UNKNOWN`，
    标签与表达式矛盾→`CLAIM_KIND_CONTRADICTION`；`required_claim_kinds` 必须出现且已注册
    （`MISSING_REQUIRED_CLAIM`）；
  - 报价必须显式携带非空 symbol（`QUOTE_SYMBOL_MISMATCH`）、有限价格、未过期；
  - 状态优先级：`SYMBOL_MISMATCH > EXPIRED > STALE > EVIDENCE_INVALID > VALID`；`reason_codes`
    汇总全部检出原因（去重、按检出顺序）。
- 其余 reason：`EVIDENCE_BUNDLE_MISMATCH`、`IDENTITY_MISMATCH`、`CYCLE_MISMATCH`、
  `MISSING_REQUIRED_INTERVAL`、`BUNDLE_NOT_CLOSED`、`BUNDLE_EXPIRED`、`UNCLOSED_BAR`、
  `FUTURE_BAR`、`FUTURE_INPUT`、`INPUT_STALE`、`INPUT_SYMBOL_MISMATCH`、`NON_FINITE_VALUE`、
  `CLAIM_FALSE`、`CLAIM_UNRESOLVED`、`MISSING_EXECUTION_QUOTE`、`QUOTE_NON_FINITE`、`QUOTE_EXPIRED`。

## 5. K03 收入、覆盖与成本

| 模型/接口 | 关键规则 |
|---|---|
| `IncomeEvent` / `idempotency_key_text` | 键 `(environment, account_ref, income_type, tran_id)` 经 strip+casefold，symbol/asset 是被校验内容不进键 |
| `income_content_hash` | 金额先 `decimal_text` 归一化；同值不同 scale 不再误判冲突 |
| `ingest_income(page, existing)` | 元组/列表主键按组件规范化，文本键去空白折叠大小写；同键同内容→duplicates，异内容→QUARANTINE |
| `IncomeCoverage` | `[start,end)`、watermark、page_limit、**结构化 `termination_proof`**、`PENDING/COMPLETE/GAP`、gaps（每条校验半开区间） |
| `plan_next_page` | 短页→TERMINATED；满页要求页内非降序（`PAGE_NOT_SORTED`）；`next_start=max(event_time)` 且必须 > 当前 start，否则 `PAGINATION_STALLED`；落在窗口末尾→`WINDOW_EXHAUSTED`；`page_limit` 非正整数→`INVALID_PAGE_LIMIT`（不崩溃） |
| `zero_fee_proof` | 来源正常 + 分页终止（**TerminationProof 且与窗口绑定**：start/end 一致、last_page_size<page_limit、terminated_at≥end）+ watermark 覆盖 + **无已声明缺口** + 迟到复查；仅自由文本→`TERMINATION_PROOF_MISSING`，窗口不符→`TERMINATION_PROOF_MISMATCH`，页未读完→`TERMINATION_PROOF_INVALID`。返回对象回带 coverage 整体哈希；**“仅凭 coverage 可复核的条件”由 `finalize_costs` 重算**（r5） |
| `attribute_income` | 事件时点可证明仓位；双向仓/多生命周期歧义→UNATTRIBUTED；**覆盖缺失/非 COMPLETE/有已声明缺口→UNKNOWN** |
| `check_conservation` | Σallocated == 源金额（默认精确相等），不符抛 `ConservationViolation` |
| `allocate_weighted` | 权重必须非负；末腿承接余数；任何与源金额符号矛盾的分量抛 `AllocationError` |
| `finalize_costs` | cost_status 与 attribution_status 独立；无事件时必须传入 `ZeroFeeProof` 结果对象（不得自报布尔，否则 `ZERO_FEE_NOT_PROVABLE`），未证明则回带其 reason_codes。**（r5）不信任对象自报的 `provable`**：始终用入参 coverage 重算可复核条件（状态非 COMPLETE / 已声明缺口 / 缺 TerminationProof 或窗口·短页·时间不符 / watermark 未达窗口末尾 / 区间非法），任一不成立即以对应 reason code 拒绝；证明与入参 coverage 哈希不一致 → `ZERO_FEE_PROOF_MISMATCH`（跨窗口复用、手工构造对象均被拦）。签名不变。三个外部标志（`source_available`/`pages_complete`/`late_recheck_done`）仍由铸证方提供，见 §12 L17 |
| `apply_late_income` | 只产生 `OutcomeRevision{revision_id, requires_recalculation=true, new_closed_outcome_created=false}`；闭合结果缺失或多义抛 `LookupError` |

## 6. K04 管理意图、授权与数量（fail-closed）

- `ManagementIntent`：`model_validator` 强制 `management_id` 等于 `derive_management_id(...)`
  （四组件或带可选 `environment` 环境域两种派生都接受）；`max_reduce_qty > 0`、`expires_at_ms > 0`、
  `reason_code/policy_version/evidence_id/trigger_*` 非空。
- `validate_authorization(authorization, request, now_ms, current_revision, consumed_nonces=())`：
  - 账户/生命周期/方向/revision/时效/nonce 逐项拒绝；`issued_at_ms > now` 亦拒绝；
  - **运行代/环境绑定为强制**：授权缺 `runtime_instance_id`→`RUN_GENERATION_UNBOUND`、
    缺 `environment`→`ENVIRONMENT_UNBOUND`；请求不符→`RUN_GENERATION_MISMATCH/ENVIRONMENT_MISMATCH`；
  - REDUCE 必须携带可解析的 `reduce_qty` 且 `0 < qty ≤ min(max_reduce_qty, current_position_qty)`；
  - CLOSE 必须携带可解析的 `close_qty` 且同样受双重上限约束（空仓拒绝）；
  - `requested_net_delta` 必填、可解析且不得为正；`remove_protection` 必须指明授权集合内的
    `target_client_id`；
  - 返回 `AuthorizationVerdict{allowed, submit_allowed(计算属性=allowed), reconcile_allowed, reason_codes}`：
    `reconcile_allowed` 仅当账户/生命周期/方向域匹配才为真；授权过期只限制新提交，不阻断按原
    client ID 的 UNKNOWN 查单与对账（§10.2）。
  - reason：`REDUCE_QTY_MISSING/INVALID`、`CLOSE_QTY_MISSING/INVALID`、`POSITION_QTY_MISSING/INVALID`、
    `NON_POSITIVE_QUANTITY`、`CLOSE_EXCEEDS_AUTHORIZATION/CLOSE_EXCEEDS_POSITION`、
    `NET_DELTA_MISSING/INVALID`、`RUN_GENERATION_UNBOUND/MISMATCH`、`ENVIRONMENT_UNBOUND/MISMATCH`、
    `NONCE_REPLAY`、`PROTECTION_TARGET_MISSING/MISMATCH`、`AUTHORIZATION_FROM_FUTURE`。
- `OrderQuantity`：三分量守恒；状态一致性（NEW→filled=0，PARTIALLY_FILLED→0<filled<accepted，
  FILLED→filled=accepted，违规 `STATUS_FILL_MISMATCH`）；**FILLED 属不可撤状态**：`request_cancel`
  拒绝，`cancel_confirmed=true` 违规 `FILLED_WITH_CANCEL_CONFIRMED`；`REJECTED` 表示未接受
  （accepted/filled 必须为 0，违规 `REJECTED_WITH_ACCEPTED_QTY`）；`requested_qty` 保留原始请求量。
- `apply_fill(..., fill_id, applied_fill_ids)`：`fill_id` 必须是非空字符串（空串/非串→`QuantityError`），
  重复 ID 抛 `DuplicateFillError`；REJECTED 不得接受 fill；working 状态推进到
  PARTIALLY_FILLED/FILLED；终态迟到 fill 只修订事实不改状态。
- `to_components()` 无损导出（含 cancel 两标志与 requested_qty）；`OrderQuantity.checked()` 为严格构造工厂。

## 7. K05 轨迹、事件与告警

- `DecisionTraceSpan` + `tally`：先跑 `span_violations`，违规 span 抛 `SpanValidationError`；
  轮次按 `cycle_id` 去重；api_attempt（含 tool_call）/decision 按 span 计数；`validation` 不计数；
  未登记 purpose 抛 `UnclassifiedPurposeError`。
- `Incident`：稳定 key = `profile|object_ref|reason_code`；`acknowledge` 仅允许 OPEN→ACK（ACK 幂等、
  RESOLVED 拒绝降级）；`resolve_incident` **要求 status=ACK**（否则 `ACK_REQUIRED`），并要求每条恢复
  证据是显式 Mapping `{"observed_at_ms": int, "value": bool}`：缺失→`EVIDENCE_MISSING`，
  无时间戳→`EVIDENCE_TIMESTAMP_MISSING`，类型混淆（字符串真值/裸布尔/`value` 与 `satisfied` 矛盾）
  →`EVIDENCE_TYPE_INVALID`，陈旧/来自未来→`EVIDENCE_STALE/EVIDENCE_FROM_FUTURE`，
  早于 ACK 时刻→`EVIDENCE_PREDATES_ACK`；证据不可序列化（循环引用）→`EVIDENCE_NOT_SERIALISABLE`；
  全部通过才置 `RESOLVED` 并写证据哈希。
- `AlertDelivery` 阶梯：`UNKNOWN→SUBMITTED（submitted_at）→ACCEPTED（provider_receipt）→
  DELIVERED（另需 delivered_at_ms，且必须有 submitted_at 前件）→ACKNOWLEDGED（ack_at_ms）`；
  HTTP200 只到 SUBMITTED；`transition_state` 前置事实缺失直接 `TransitionError`（不得先提升后补证）；
  高层状态缺 `submitted_at` 时 `delivery_violations` 报 `SUBMITTED_WITHOUT_TIMESTAMP`；
  无设备回执只能以人确认标 `ACKNOWLEDGED`。

## 8. K06 ReleaseEvidence

- 五组固定；`PASS` 必须同时具备证据 hash、有效窗口与匹配身份；缺证据→`NO_DATA`+阻断项；声明 PASS
  但证据不完整→`FAIL`+`PASS_WITHOUT_EVIDENCE/WINDOW_MISSING/IDENTITY_REF_MISSING`；畸形窗口字段
  （不可解析、JSON `Infinity/1e400`、非有限或超出 int64 毫秒）→`FAIL`+`WINDOW_INVALID`，
  报告不崩溃；`status` 文本先 strip 再大写。
- `NO_DATA != PASS`；组与组独立评估；**不建立第二套 GO 判断器**（无整体 GO 字段）。

## 9. 与既有 ReleaseManifest 字段的显式映射（U15/I 集成依据）

现有 `src/core/release_manifest.py` 的 `ReleaseManifest` 与 K01 的对应关系如下；命名差异是
F 指纹清单的已知项（`F_fingerprint_r1.md` 第 12/5/3/4 条），U15 集成时按此映射生成身份，
**U00 不修改 `release_manifest.py`**。

| K01 字段 | 现有来源 | 差异与要求 |
|---|---|---|
| `artifact_hash` | `content_hash` | 语义一致，仅命名不同 |
| `git_commit` | `git_commit` | 一致；无 git 时显式 UNKNOWN，身份比较不忽略 |
| `dependency_hash` | `dependency_hash` | **现缺 `requirements-lock.txt`**；契约要求锁文件纳入，比对结果另存 |
| `strategy_hash` | `strategy_config_hash` | **现缺 `knowledge/strategy_cards.json`**；契约要求覆盖实际策略卡 |
| `model_route_hash` | `model_config_hash` | 契约要求**解析后的有效路由**（含环境/备用/profile 覆盖） |
| `effective_config_hash` | 无 | 契约要求 profile/环境合并后的有效非敏感值 |
| `account_ref` | `account_ref` | 一致；environment 与状态根一致性校验由 U15 补 |
| `runtime_instance_id` | 无 | 新增；U15 持久化运行代并与实例锁绑定 |
| `environment` | `environment` | 一致；`release_id` 种子缺陷由 U15 处理 |
| —（K01 不含） | `release_id/egress_id/threshold_version/manifest_schema` | 保留在 ReleaseManifest |

K06 与既有 `src/core/release_evidence.py`：K06 只冻结输出结构，U09 扩展现有计算器，不得新建平行 GO 逻辑。

## 10. 版本与兼容规则

- `SCHEMA_VERSION = "3.1"`；`RuntimeIdentity.schema_version` 是字符串，允许 `3.x` 小版本。
- **3.0 → 3.1（r2 + r3）**：新增 reason_code、可选/计算字段（`OrderQuantity.requested_qty`、
  `ManagementAuthorization.environment/runtime_instance_id`、`AuthorizationVerdict.submit_allowed`
  计算属性、`OutcomeRevision.revision_id/previous_revision_id`、`IncomeCoverage.termination_proof`、
  `ModelRoute`、`ManagementIntent.environment`），以及 `validate_bundle(rules=)`/`validate_intent(rules=)`/
  `validate_authorization`/`zero_fee_proof`/`finalize_costs` 的 fail-closed 收紧。契约尚未被
  U01–U05 集成、未打标签，因此不承担旧调用兼容；集成后新增字段仍按“带默认值可选”约束，
  收紧判定必须登记为小版本行为变更。
- **3.1 新增字段/常量（r4 + r5，带默认值，可选语义）**：
  - `EvidenceBundle.dropped_input_ids: tuple[str, ...] = ()`（默认空，纳入 `content_hash`，
    随 `k02_evidence_bundle.schema.json` 同步；r4 引入，r5 补形态守卫）；
  - 新增 reason_code：`RULES_MISSING`（r4）、`ZERO_FEE_PROOF_MISMATCH`（r4）、
    `RulesInputError`（r5 结构化异常，`build_bundle` 侧专用）；
  - K01 覆盖缺口新标签：`<组>.unknown_field:<键>`（r4）。
- **3.1 判定收紧登记（r4/r5，均为小版本行为变更，不留旧调用兼容）**：
  | # | 收紧 | 影响 | golden |
  |---|---|---|---|
  | T1（r4） | `rules` 由“应传”变为**必传**：`validate_bundle`/`validate_intent` 缺省 `rules=None` → `RULES_MISSING` → `EVIDENCE_INVALID` | 未显式传 `rules` 的调用方（U03）从“跳过规则比对”变为一律 `EVIDENCE_INVALID`（方向 fail-closed） | K02-N32/N35 |
  | T2（r4） | 对 `symbol=None` 的输入、越界 `bound_inputs`、非空 `dedup_removed` 一律结构化拒绝（此前可部分通过） | 幽灵输入/越界引用/不可重算字段自报不再放行 | K02-N31/N33/N34/P8 |
  | T3（r5） | `rules` 的 **Mapping 内容**必须可规范化为 JSON：非 JSON 值/混合键型 → `RULES_MISMATCH`（validate 侧）/ `RulesInputError`（build 侧），不再抛裸 `TypeError` | 新拒绝面（原先崩溃） | 直接探针（JSON golden 无法表达 `set`/`object`/混合键型，见 §12 L20） |
  | T4（r5） | `finalize_costs` 由入参 coverage **重算**零费用可复核条件，并新增 `ZERO_FEE_PROOF_MISMATCH`（证明哈希与入参 coverage 不一致）；不再采信对象自报 `provable` | 手工构造 `ZeroFeeProof(provable=True, coverage_hash=<正确哈希>)` 不再能跳过 TerminationProof 等条件（原先 `COST_COMPLETE`） | K03-N20/N21/P5（期望不变） |
  | T5（r5） | `dropped_input_ids` 非 `tuple`/`list`（含裸字符串）→ `QUALITY_MISMATCH`，不迭代 | `model_copy` 绕过类型校验的形态不再崩溃/静默通过 | 直接探针（同 T3） |
- **golden 期望修改规则（恢复）**：golden 追加新用例；**既有用例不得修改期望值**。如契约语义
  修复必须变更既有期望，须在 §11「golden changelog」逐条登记：用例号、变更前后、规格/审查依据，
  不得静默改期望。
- **小版本**：只允许新增带默认值的可选字段、新增 reason_code、新增枚举值（消费方须容忍未知值）；
  不得改变既有字段类型/语义/区间/状态优先级。golden 追加新用例。
- **大版本（3.x → 4.0）**：删除/重命名/改类型字段、改变状态优先级、改变守恒或时间语义、
  改变幂等键组件，均为大版本；需新契约提交与 I 重新冻结。
- 现有 `TradeIntent`/SQLite 字段不动；展示四舍五入不回流执行计算。

## 11. Golden、冒烟与 P0/P1 回归映射

- golden 位于 `docs/contracts/golden/*.golden.json`，每条含 `case_id/kind/operation/inputs/expect/
  derivation`；`derivation` 必须写明期望来自规格条款、公式或交易所语义，禁止用被测实现输出自证。
- 运行（PowerShell，仓库根）：

```powershell
$env:PYTHONUTF8=1
& "..\multi-agent-trader\.venv\Scripts\python.exe" scripts\smoke_contracts_v3.py
```

| 初审问题 | 修复/回归用例 |
|---|---|
| B/Q P0 validate_authorization fail-open | K04-N8..N17（CLOSE/REDUCE 缺失、负值、超量、净敞口、目标、nonce、运行代） |
| C P0-1 逐意图完整检查不可达 | K02-N10..N12、N16、K02-P4（字面量 claim、未知 action、缺绑定、必需 claim kind） |
| C P0-2 content_hash 无校验入口 | K02-N17..N19、K02-P5（篡改输入/伪造事实/身份不符/正常包） |
| K05 恢复证据无时间戳/真值化；tally 不去重 | K05-N5..N8、K05-P6 |
| K01 覆盖不强制；原始文本可哈希 | K01-N3、K01-N4、K01-P6 |
| K02 跨 symbol/无时间 bar/空报价 symbol | K02-N13..N15 |
| K03 gaps/分页进度/scale/键规范化/元组键 | K03-N5..N9、K03-P9..P12 |
| K04 fill 状态/幂等/REJECTED/无损导出/意图绑定 | K04-P6..P8、K04-N18..N21 |
| K06 畸形窗口/状态空白 | K06-P2、K06-N5、K06-N6 |
| **r2 复审 K06 非有限窗口崩溃（Q P1）** | K06-N7（JSON `1e400`→`FAIL/WINDOW_INVALID`，不崩溃） |
| **r2 复审 K02 非 OPEN 无授权引用（C P0-1 残余）** | K02-P6、K02-N20（`NEEDS_AUTH`） |
| **r2 复审 K02 quality/rules 可抹除（C N1）** | K02-N21（stale 重哈希→`QUALITY_MISMATCH`）、N22（kinds 抹除+验证方 rules→`RULES_MISMATCH`）、P7 |
| **r2 复审 K02 kind 标签/越权/畸形（C N2/N3/N4/N6/N7）** | K02-N23（未知/矛盾 kind）、N24（重复 fact_id）、N25（事实换指）、N26（越界引用）、N27（畸形 claims）、N28（expected 非布尔）、N29（非 K 线 closed 无时间戳）、N30（未注册必需 kind） |
| **r2 复审 K04 绑定可选/空 fill_id/FILLED（B N1..N5）** | K04-N22..N27、K04-P9（FILLED 撤单、空 fill_id、未绑定/跨环境、查单许可；环境域意图 id） |
| **r2 复审 K05 ACK 前置/跳级/序列化（Q NEW-P2-1..5）** | K05-N11..N15（OPEN 直接 resolve、RESOLVED 降级、value/satisfied 矛盾、循环证据、缺 SUBMITTED 直达 DELIVERED） |
| **r2 复审 K03 缺口归因/自由文本/类型（B N6/N7、Q NEW-P2-8/9）** | K03-N14..N19（gaps 归因、自由文本终止、page_limit 类型、signed zero、证据窗口/页大小） |
| **r2 复审 K01 字段级覆盖/ModelRoute（C P1-3 残余）** | K01-P7、K01-N5、K01-N3（字段级期望） |

**golden changelog（既有用例期望/输入变更登记）**

| 用例 | 变更 | 依据 |
|---|---|---|
| K03-P1/P6（r2） | 键文本 `SYNTH_ACCT/FUNDING_FEE` → 小写 | 键归一化 strip+casefold（B P1-11/Q P2-7；期望值随语义修复变更） |
| K04-N6（r2） | expect 追加 `INSUFFICIENT_POSITION` | 新增必填 `current_position_qty` 后双重上限同时命中（B P0-1 修复） |
| K04-P2（r2） | `revision` `"5"` → `5` | 修复有损导出，revision 恢复为 int（B P1-2） |
| K05-N4（r2） | expect 追加 `EVIDENCE_PREDATES_ACK` | ACK 后必须重新观察（C P1-1/Q NEW-P2-1 同族收紧） |
| K01-N3（r3） | 期望从组级标签改为字段级缺口（如 `model_route.model`） | C P1-3 残余：字段级覆盖强制 |
| K03-N1（r3） | `PAGINATION_NOT_TERMINATED` → `TERMINATION_PROOF_MISSING` | B N7：自由文本不再作为终止证据 |
| K03-P4/P5/P8/N2/N3/N6/N7（r3） | inputs 增加 `termination_proof`；P5/N7 的 `zero_fee_provable` 布尔改为 `zero_fee` 对象 | B N7/Q NEW-P2-8：证明必须结构化且不得自报布尔 |
| K04 全部非 OPEN 授权用例（r3） | inputs 的 authorization/request 增加 `environment/runtime_instance_id`；N15 保持跨代请求 | B N1：绑定缺失即拒绝（语义收紧，期望不变） |
| K02-N16（r3） | inputs 移除自由标签 `kind:"other_kind"`（期望不变） | kind 注册表落地后标签必须已注册，用例聚焦缺必需 kind |
| K01-P3/P4/P5（r3） | inputs 增加显式空组：`model_route.fallback_routes/profile_overrides`、`effective_config.profile_overrides/environment_overrides`（期望不变） | C P1-3 残余：字段级覆盖强制后空组必须显式声明（Q P3-3 ≡ B N14 漏登，r5 补登） |
| K03-N5（r3） | inputs 的 coverage 增加 `termination_proof`、顶层增加 `rules` 键（期望不变） | B N7：终止证据必须结构化；rules 由验证方持有（Q P3-3 ≡ B N14 漏登，r5 补登） |
| K02-N1..N21、N23..N30、P1..P6（r4，共 35 条） | bundle 规格新增 `rules` 键（与顶层 rules 同源；期望不变） | Q NEW-R4-P3-1 ≡ B N14：`rules` 变为必传后 build 侧与 validate 侧规则必须同源（r5 补登；K02-N22 由 verify 路径构造，未受影响） |
| K03-P5（r4） | 顶层增加 `rules` 键；`zero_fee` 由自报 dict `{"provable": true}` 改为 `{"from_coverage": true, source_available/pages_complete/late_recheck_done: true}`（期望不变） | G/Q P3-3：零费用证明不得自报布尔，必须由 `zero_fee_proof` 铸证（r5 补登） |
| K02 全部用例（r2/r3） | identity `schema_version` 3.0→3.1 | 契约小版本升级 |

**r5 轮登记**：本轮修复（T3/T4/T5）**未改动任何既有期望、也未新增 golden 用例**——r4 的
“0 条期望变更”性质保持（`git diff` 上 golden 六文件零改动）。原因：新增拒绝面依赖 JSON
无法表达的载荷形态（`set`/`bytes`/`object`/混合键型、`model_copy` 绕过类型校验），
无法写成 golden fixture，改以直接探针复现（见 §12 L20 与复审报告 §3 的复现命令）。

## 12. 已知限制、P2 处置与下游缺口索引

**已修（r2）**：K03 COMPLETE+gaps 矛盾、Decimal scale 误冲突、负权重/负末腿分摊、`coverage=None`
归因 fail-closed、`finalize_costs(coverage=None)` 保留归因状态、迟到修订 revision_id 与多义拒绝、
元组主键与键规范化、K05 非法 span 拒绝与 cycle 去重、K06 畸形窗口/状态空白、K01 空集合语义与
死常量、K03 schema 补齐 `ZeroFeeProof/PaginationDecision`、幂等键转义防碰撞。

**已修（r3）**：K06 非有限/超界窗口不再崩溃（`WINDOW_INVALID`）；K02 质量字段全量重算 +
验证方 `rules` 比对（stale/required_claim_kinds 不可抹除）、事实集合唯一与选取规则固定、
kind 注册表与语义一致、bound_inputs 覆盖 claim 引用、畸形 claims/expected 结构化拒绝、
非 OPEN 必须 `management_id`/授权引用（`NEEDS_AUTH`）；K04 运行代/环境绑定缺失即拒绝、
FILLED 不可撤、空串 fill_id 拒绝、`submit_allowed` 计算属性与 `revision_id` 默认值兼容、
查单许可限定身份域；K05 必须先 ACK（`ACK_REQUIRED`）、循环证据结构化拒绝、`value/satisfied`
矛盾拒绝、DELIVERED 需 SUBMITTED 前件；K03 结构化 TerminationProof（窗口绑定/短页/时间）、
`finalize_costs` 只接受 ZeroFeeProof 对象、gaps 归因 fail-closed、signed zero、page_limit 类型；
K01 ModelRoute + 字段级覆盖。

**已修（r4）**：`rules` 变为必传（`RULES_MISSING`）并新增整体哈希比对 `quality.rules_hash`；
非 OPEN 意图必须携带管理授权引用；`bound_inputs` 覆盖 claim 引用、`symbol=None` 幽灵输入拒绝；
`quality.dedup_removed` 不可自报（必须为空）且 `dropped_input_ids` 落地为审计字段；
K01 `build_identity` 对非 Mapping 覆盖输入结构化拒绝（不再 `AttributeError`）与未知键
`unknown_field` 口径；K04 运行代/环境绑定按 strip 后非空判定（空串视同缺）；
K05 `UNKNOWN→ACCEPTED` 必须有 SUBMITTED 前件、恢复证据 int/bool 混淆拒绝；
K03 `ZeroFeeProof` 回带 coverage 哈希并要求窗口绑定一致（`ZERO_FEE_PROOF_MISMATCH`）。

**已修（r5）**：`rules` 的 Mapping **内容**不可规范化为 JSON 时结构化拒绝（`RULES_MISMATCH` /
`RulesInputError`，不再抛裸 `TypeError`，NEW-R4-P2-1）；`finalize_costs` 由入参 coverage
**重算**零费用可复核条件，不再采信对象自报 `provable`（NEW-R4-P2-2）；`dropped_input_ids`
迭代前加 `tuple`/`list` 守卫（`model_copy` 绕过类型校验不再崩溃/静默通过，NEW-R4-P3-3）；
README 与 r4 实现同步（§3/§4/§5/§10/§11/§12）。

**已文档化（不在本包实现，按失败关闭约定由下游承担）**：

| # | 限制 | 影响 | 下游任务 |
|---|---|---|---|
| L1 | 敏感守卫只查键名/载荷形态，不扫描值内容 | 值中夹带秘密不会拦截 | U15（哈希输入白名单 + 适配验收） |
| L2 | K02 intent 仍是自由 dict，无 `side/order_type/entry/SL/TP/RR` 类型化模型 | 策略几何与 RR 校验需在调用侧实现 | U03（DL02），U00 不预建 |
| L3 | `validate_intent` 的 `now_ms` 缺省为 `as_of_ms` | 漏传会低估过期/陈旧 | U03（调用约定 + 验收） |
| L4 | K05 无 `cycle_deadline_ms` 绝对期限字段与跨 span 一致性谓词 | 无法仅凭契约证明“同 deadline” | U05（DL07），需小版本追加 |
| L5 | `AlertDelivery` 无通道能力/dedup/退避/冷却字段；`Incident` 无结构化 reason_code | 投递缓存语义需自建 | U14（§11.2） |
| L6 | K04 无“交易所权威累计对账 setter/只向上修正审计”、无保护命令模型、无 UNKNOWN 查单计划 | fill/保护/未知订单恢复需 U04 自建 | U04（§10.2） |
| L7 | 授权 nonce 消费集合由调用方传入；无持久化预留/释放原子接口 | 重放与在途预留由协调器保证 | U02（§10.2） |
| L8 | K03 无“分页与水位同事务提交”结构及 `pending_since/next_sync/last_source_success` | 处理时效字段需自建 | U01（§10.1） |
| L9 | K01 无 `credential_epoch/key_fingerprint`，`account_ref` 派生规则未冻结 | 轮换与账本连续归集由 U15 落地 | U15（§10.1） |
| L10 | 冒烟 `compare()` 是期望子集断言（actual 多字段不报错） | 冻结后字段漂移可能漏检 | I/Q（冻结前补键集合校验或接受） |
| L11 | `OrderQuantity` 构造器不内建校验，仅 `checked()` 严格 | 审计重放允许构造“问题样本” | U04（写入路径统一用 checked/apply_fill） |
| L12 | K05 深调用超时/分阶段 p95 等指标口径未冻结数值 | 阈值待 RC 前冻结 | U05/U12（§12 operations_slo） |
| L13 | `content_hash` 是完整性自校验（无密钥自哈希），不是签名/封条 | 篡改后重算哈希仍可通过自洽；跨进程可信需签名 | U03/U15（精确意图集合签名 `ValidationBatch`） |
| L14 | K02 无 bundle 级字段 `market_venue/account_profile/missing_fields/fresh_until` 与类型化 quality | 主网/Demo 同时间戳合并风险由调用侧标注 | U03（报告 §5.3） |
| L15 | K03 `coverage_problems` 与 `zero_fee_proof` 为两个职责面（自洽性 vs 可证明），watermark 命名仍有两套 | 需同时消费两函数的报告注意口径 | U01（可收敛为单一判定源） |
| L16 | `STRATEGY_SOURCE_FILES`（`knowledge/strategy_cards.json`）仅作来源标签，`_coverage_gaps` **不**做策略卡文件级校验 | 身份只证明“策略卡内容已声明且非空”，不证明该内容来自 `knowledge/strategy_cards.json`；改卡仍改身份（`strategy_hash`） | U15（C r1 P2-6 残余；已在 §3 明写口径，r5 登记） |
| L17 | `finalize_costs` 由 coverage 重算零费用条件，但 `source_available`/`pages_complete`/`late_recheck_done` 三个外部标志仍由铸证方提供（`ZeroFeeProof` 不携带原始布尔） | 具备正确 coverage 哈希的伪证仍可对这**三个标志**说谎（coverage 侧条件已不可伪造）；要彻底关闭需给 `finalize_costs` 增加原始条件入参（签名变更） | U01（Q NEW-R4-P2-2 残余，r5 登记；与 L11 同类“构造器不内建校验”） |
| L18 | `dropped_input_ids` 是**审计提示**，其去重集合无法由包内 `inputs` 重算（被丢弃输入的内容已不在包内） | 可凭空申报“丢弃过 N 个输入”（仅形态受校验：`tuple`/`list`、非空 str、唯一、与保留输入不相交）；无判定消费者 | U01/U03（Q NEW-R4-P3-2，r5 登记；若要强制需新增“被丢弃输入载荷”字段 = 小版本新增字段） |
| L19 | `validate_coverage` 不报未知键（`build_identity` 报 `组.unknown_field:<键>`） | 两个入口口径不同：build 侧更严，方向 fail-closed（不会放行）；手工构造身份文本时未知键不被 `validate_coverage` 检出 | U15（C NR-3 残余 / Q NEW-D，r5 登记） |
| L20 | 非 JSON 载荷形态（`set`/`bytes`/`object`/`datetime`/混合键型）与 `model_copy` 绕过类型校验的形态**无法写成 JSON golden fixture** | 冒烟（golden 驱动）覆盖不到这些拒绝面，只由直接探针验证；冒烟不得因此被当成“全覆盖” | I/Q（r5 登记：T3/T5 的新拒绝面，见 §10、§11） |

**下游缺口索引（r2 复审 C §4 八项 + B §5 八项，按任务分列）**：

| 来源 | 缺口 | 下游任务 |
|---|---|---|
| C#5 | `ValidationBatch` 精确意图集合签名/封条（含 bundle_hash/intent_ids/verdict_ids/config_version/expires_at） | U03/U15 |
| C#6 | bundle 级字段（`schema_version/environment/market_venue/account_profile/missing_fields/fresh_until`、类型化 quality） | U03 |
| C#8 | 非 K 线输入证据结构（funding/OI/mark/basis/账户快照的 kind/finality/TTL/missing） | U03/U05 |
| C#10 | 工具出参 schema 裁剪与“工具结果→证据输入”适配契约 | U03/U05 |
| C#11 | 外部文本 trust/taint 通道（DC12：文本只能当数据） | U03（U00 不预建） |
| C#12 | span→intent/order/evidence 关联字段（链路闭合） | U05 |
| C#13 | funnel 状态枚举、no_trade 一轮/一单解释、分桶耗时 helper | U05/U08 |
| C#16 | `ModelRoute` capability/mock 标志与参数来源（yaml/env/CLI/profile） | U15（DL01） |
| B-O1 | `FILLED` 撤单语义（r3 已收紧 request/confirm + 违规码；剩余“与交易所终态对账”） | U04 |
| B-O2 | fill 来源（user stream/REST）、可选幂等集合由调用方维护且接口不回带 | U04 |
| B-O3 | 运行代/环境绑定（r3 已强制校验；剩余 mint 侧持久化与实例锁绑定） | U02/U15 |
| B-O4 | environment 域 `management_id` 在意图中的统一约定（r3 已支持两种派生） | U02 |
| B-O5 | 零费用终止证据与窗口/`synced_at` 的持久化绑定与新鲜度（r3 已结构化） | U01 |
| B-O6 | `attribute_income` 对 COMPLETE+gaps fail-closed（r3 已修）；剩余按 income_type/asset 的决策域 | U01 |
| B-O7 | 改量（amend）语义：accepted revision、`client_amend_id`、“被拒改量不冒充已接受” | U04 |
| B-O8 | 授权→意图→命令（Outbox）关联 ID 与重启后只读恢复形态 | U02/U04 |
| 补充 | `income_type` 枚举/大小写治理与覆盖分裂（r3 仅做键归一化） | U01 |
| 补充 | `AlertDelivery` 去重/退避/冷却与 `Incident` 结构化 reason_code | U14 |
| 补充 | 策略卡**文件级**来源校验（`STRATEGY_SOURCE_FILES` 落地为覆盖检查，L16） | U15 |
| 补充 | `finalize_costs` 接收原始零费用条件入参以关闭“三标志自报”（L17，小版本签名新增） | U01 |
| 补充 | `dropped_input_ids` 可重算（新增“被丢弃输入载荷”字段，L18） | U01/U03 |

其余接口缺口（C r1 报告 §5 其余项、B r1 报告 §5 清单）不改本契约冻结面，
由 I 在分派时按 `implementation_requires` 对应任务登记；新增字段仍按 §10 规则小版本演进。

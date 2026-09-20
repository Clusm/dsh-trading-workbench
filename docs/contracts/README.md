# K01–K06 冻结契约 v3.1（U00 r2）

状态：FROZEN 候选（`db7e0d7` 初审 CHANGES_REQUESTED，本版为 r2 修复轮；待 Q/C/B/I 复审后按流程打
`contract-v3.x` 标签）。初审报告：`docs/dispatch/reviews/U00/db7e0d7/{Q,C,B}_w0r1_r1.md`。
来源：[无人值守交付与多 Agent 执行规格 v3](../无人值守交付与多Agent执行规格_2026-09-19.md) §4/§10/§11.1/§12/§13；
[决策链路专项评估与优化报告](../决策链路专项评估与优化报告_2026-09-19.md) §3/§5/§7/§9；U00 任务卡与 W0 指纹差异清单
（`docs/dispatch/reviews/U00/w0/F_fingerprint_r1.md`）。

r2 变更原则：fail-closed。任何缺失/畸形/未知/自相矛盾输入都必须拒绝并给出具体 reason_code，
不得按“无值即跳过”放行；新增字段全部带默认值，新增 reason_code 属小版本允许面。

## 1. 内容与边界

| 路径 | 内容 |
|---|---|
| `src/core/contracts/common.py` | 规范化 JSON 哈希、Decimal 边界、敏感键名守卫、结构化载荷守卫、键归一化/转义、`[start,end)` 工具 |
| `src/core/contracts/k01_identity.py` | RuntimeIdentity、覆盖清单强制、身份不符判定 |
| `src/core/contracts/k02_evidence.py` | EvidenceBundle/EvidenceInput/DerivedFact、`build_bundle`、`validate_bundle`、`validate_intent` |
| `src/core/contracts/k03_income.py` | 收入幂等、覆盖证明、分页决策、归因、守恒、迟到修订 |
| `src/core/contracts/k04_management.py` | ManagementIntent/ManagementAuthorization/OrderQuantity 三分量 |
| `src/core/contracts/k05_trace.py` | DecisionTraceSpan/DecisionCounters、Incident 恢复、AlertDelivery 阶梯 |
| `src/core/contracts/k06_release.py` | 五组 ReleaseEvidence 状态与阻断项清单 |
| `docs/contracts/*.schema.json` | 六个 JSON schema（含 `BundleVerdict`/`ZeroFeeProof`/`PaginationDecision` 等），冒烟校验与模型同步 |
| `docs/contracts/golden/*.golden.json` | 113 条正反 golden（40 正 / 73 反），每条含规格推导 `derivation` |
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

- **覆盖强制**：`build_identity(enforce_coverage=True)`（默认）下 dependency/strategy/model_route/
  effective_config 四组任一缺失抛 `IdentityCoverageError`，禁止生成不完整身份；离线迁移工具可用
  `enforce_coverage=False` 但需在审计中说明。
- `validate_coverage` 只把字段缺失（None）或空字符串算缺口；显式空集合/空字典表示“已声明且当前为空”。
- 小版本兼容比较可把 `schema_version` 显式加入 `ignore`。

## 4. K02 EvidenceBundle 与逐意图校验

- `build_bundle(snapshot, closed_bars, rules)`：只从已收盘 K 线生成事实；
  - 收盘时间晚于 `as_of_ms` 或缺失 `close_time_ms` 的 bar 一律不判 closed（缺时间记 `UNDATED_BAR`）；
  - 跨 symbol 输入被隔离：不进入事实、`bar_times` 与最终性计算，并记入
    `quality.symbol_mismatch_inputs`，被引用时给 `INPUT_SYMBOL_MISMATCH`；
  - 重复内容按 `(symbol, interval, close_time, values)` 去重；`quality` 记录
    `missing_intervals / stale_inputs / dedup_removed / unclosed_inputs / undated_inputs /
    symbol_mismatch_inputs / required_intervals / required_claim_kinds / max_bar_age_ms`；
  - `evidence_id` 未显式提供时由 cycle/symbol/as_of/输入 id 与值摘要派生（值变化即 id 变化）。
- `validate_bundle(bundle, identity=None) -> BundleVerdict`：**K02 完整性入口**。重算并比对
  `content_hash`，按 kind 从引用输入重算每个 `DerivedFact.value`，校验输入去重、时间顺序、
  fact 引用与符号完整性、quality/finality/bar_times 自洽、可选身份一致性。返回 reason：
  `CONTENT_HASH_MISMATCH/CONTENT_HASH_MISSING/DERIVED_FACT_MISMATCH/DERIVED_FACT_INVALID/
  CROSS_SYMBOL_FACT/DUPLICATE_INPUT_ID/QUALITY_MISMATCH/FINALITY_MISMATCH/INPUT_TIME_ORDER/
  BAR_RECEIVED_BEFORE_CLOSE/IDENTITY_MISMATCH`。
- `validate_intent(intent, bundle, execution_quote, now_ms=None)`：先过 `validate_bundle`（失败直接
  EVIDENCE_INVALID），再逐 intent 校验：
  - `intent_id` 必填；`action ∈ {OPEN,CLOSE,REDUCE,ADJUST}`、`side ∈ {LONG,SHORT}` 否则
    `UNKNOWN_ACTION/MISSING_SIDE/INVALID_SIDE`；
  - OPEN 必须声明 `bound_inputs`（`MISSING_EVIDENCE_BINDING`）与非空 claims（`MISSING_CLAIMS`）；
  - claim 只能是 `lhs op rhs` 单比较（op 有限集），操作数必须引用事实/输入值；两个字面量
    （如 `1 < 2`）判 `CLAIM_WITHOUT_EVIDENCE`；`rules.required_claim_kinds` 列出的 kind 必须出现
    （`MISSING_REQUIRED_CLAIM`）；
  - 报价必须显式携带非空 symbol（`QUOTE_SYMBOL_MISMATCH`）、有限价格、未过期；
  - 状态优先级：`SYMBOL_MISMATCH > EXPIRED > STALE > EVIDENCE_INVALID > VALID`；`reason_codes`
    汇总全部检出原因（去重、按检出顺序）。
- 其余 reason：`EVIDENCE_BUNDLE_MISMATCH`、`IDENTITY_MISMATCH`、`CYCLE_MISMATCH`、
  `MISSING_REQUIRED_INTERVAL`、`BUNDLE_NOT_CLOSED`、`BUNDLE_EXPIRED`、`UNCLOSED_BAR`、`UNDATED_BAR`、
  `FUTURE_BAR`、`FUTURE_INPUT`、`INPUT_STALE`、`INPUT_SYMBOL_MISMATCH`、`NON_FINITE_VALUE`、
  `CLAIM_FALSE`、`CLAIM_UNRESOLVED`、`MISSING_EXECUTION_QUOTE`、`QUOTE_NON_FINITE`、`QUOTE_EXPIRED`。

## 5. K03 收入、覆盖与成本

| 模型/接口 | 关键规则 |
|---|---|
| `IncomeEvent` / `idempotency_key_text` | 键 `(environment, account_ref, income_type, tran_id)` 经 strip+casefold，symbol/asset 是被校验内容不进键 |
| `income_content_hash` | 金额先 `decimal_text` 归一化；同值不同 scale 不再误判冲突 |
| `ingest_income(page, existing)` | 元组/列表主键按组件规范化，文本键去空白折叠大小写；同键同内容→duplicates，异内容→QUARANTINE |
| `IncomeCoverage` | `[start,end)`、watermark、page_limit、终止证据、`PENDING/COMPLETE/GAP`、gaps（每条校验半开区间） |
| `plan_next_page` | 短页→TERMINATED；满页要求页内非降序（`PAGE_NOT_SORTED`）；`next_start=max(event_time)` 且必须 > 当前 start，否则 `PAGINATION_STALLED`；落在窗口末尾→`WINDOW_EXHAUSTED` |
| `zero_fee_proof` | 来源正常 + 分页终止 + watermark 覆盖 + **无已声明缺口** + 迟到复查；缺口存在时 COMPLETE 亦不可证明（`COVERAGE_GAPS_DECLARED`） |
| `attribute_income` | 事件时点可证明仓位；双向仓/多生命周期歧义→UNATTRIBUTED；**覆盖缺失或非 COMPLETE→UNKNOWN** |
| `check_conservation` | Σallocated == 源金额（默认精确相等），不符抛 `ConservationViolation` |
| `allocate_weighted` | 权重必须非负；末腿承接余数；任何与源金额符号矛盾的分量抛 `AllocationError` |
| `finalize_costs` | cost_status 与 attribution_status 独立；覆盖/缺口问题不 COMPLETE；无事件时需零费用证明 |
| `apply_late_income` | 只产生 `OutcomeRevision{revision_id, requires_recalculation=true, new_closed_outcome_created=false}`；闭合结果缺失或多义抛 `LookupError` |

## 6. K04 管理意图、授权与数量（fail-closed）

- `ManagementIntent`：`model_validator` 强制 `management_id == derive_management_id(...)`；
  `max_reduce_qty > 0`、`expires_at_ms > 0`、`reason_code/policy_version/evidence_id/trigger_*` 非空。
- `management_idempotency_key/derive_management_id` 支持可选 `environment` 环境域；
  intent 校验使用 3.0 兼容的四组件绑定（环境域由 U02 在调用侧统一约定）。
- `validate_authorization(authorization, request, now_ms, current_revision, consumed_nonces=())`：
  - 账户/生命周期/方向/revision/时效/nonce/运行代/环境不符逐项拒绝；`issued_at_ms > now` 亦拒绝；
  - REDUCE 必须携带可解析的 `reduce_qty` 且 `0 < qty ≤ min(max_reduce_qty, current_position_qty)`；
  - CLOSE 必须携带可解析的 `close_qty` 且同样受双重上限约束（空仓拒绝）；
  - `requested_net_delta` 必填、可解析且不得为正；`remove_protection` 必须指明授权集合内的
    `target_client_id`；
  - 返回 `AuthorizationVerdict{allowed, submit_allowed, reconcile_allowed=True, reason_codes}`：
    **授权过期只限制新提交，不阻断按原 client ID 的 UNKNOWN 查单与对账**（§10.2）。
  - 新 reason：`REDUCE_QTY_MISSING/INVALID`、`CLOSE_QTY_MISSING/INVALID`、`POSITION_QTY_MISSING/INVALID`、
    `NON_POSITIVE_QUANTITY`、`CLOSE_EXCEEDS_AUTHORIZATION/CLOSE_EXCEEDS_POSITION`、
    `NET_DELTA_MISSING/INVALID`、`RUN_GENERATION_MISMATCH`、`ENVIRONMENT_MISMATCH`、`NONCE_REPLAY`、
    `PROTECTION_TARGET_MISSING/MISMATCH`、`AUTHORIZATION_FROM_FUTURE`。
- `OrderQuantity`：三分量守恒；状态一致性（NEW→filled=0，PARTIALLY_FILLED→0<filled<accepted，
  FILLED→filled=accepted，违规 `STATUS_FILL_MISMATCH`）；`REJECTED` 表示未接受（accepted/filled 必须为 0，
  违规 `REJECTED_WITH_ACCEPTED_QTY`）；`requested_qty` 保留原始请求量。
- `apply_fill(..., fill_id, applied_fill_ids)`：重复 fill ID 抛 `DuplicateFillError`；REJECTED 不得接受
  fill；working 状态推进到 PARTIALLY_FILLED/FILLED；终态迟到 fill 只修订事实不改状态。
- `to_components()` 无损导出（含 cancel 两标志与 requested_qty）；`OrderQuantity.checked()` 为严格构造工厂。

## 7. K05 轨迹、事件与告警

- `DecisionTraceSpan` + `tally`：先跑 `span_violations`，违规 span 抛 `SpanValidationError`；
  轮次按 `cycle_id` 去重；api_attempt（含 tool_call）/decision 按 span 计数；`validation` 不计数；
  未登记 purpose 抛 `UnclassifiedPurposeError`。
- `Incident`：稳定 key = `profile|object_ref|reason_code`；`ACK` 只记录 `ack_at_ms`；
  `resolve_incident` 要求每条恢复证据是显式 Mapping `{"observed_at_ms": int, "value": bool}`：
  缺失→`EVIDENCE_MISSING`，无时间戳→`EVIDENCE_TIMESTAMP_MISSING`，类型混淆（字符串真值/裸布尔）
  →`EVIDENCE_TYPE_INVALID`，陈旧/来自未来→`EVIDENCE_STALE/EVIDENCE_FROM_FUTURE`，
  早于 ACK 时刻→`EVIDENCE_PREDATES_ACK`；全部通过才置 `RESOLVED` 并写证据哈希。
- `AlertDelivery` 阶梯：`UNKNOWN→SUBMITTED（submitted_at）→ACCEPTED（provider_receipt）→
  DELIVERED（另需 delivered_at_ms）→ACKNOWLEDGED（ack_at_ms）`；HTTP200 只到 SUBMITTED；
  `transition_state` 前置事实缺失直接 `TransitionError`（不得先提升后补证）；无设备回执只能
  以人确认标 `ACKNOWLEDGED`。

## 8. K06 ReleaseEvidence

- 五组固定；`PASS` 必须同时具备证据 hash、有效窗口与匹配身份；缺证据→`NO_DATA`+阻断项；声明 PASS
  但证据不完整→`FAIL`+`PASS_WITHOUT_EVIDENCE/WINDOW_MISSING/IDENTITY_REF_MISSING`；畸形窗口字段
  （不可解析）→`FAIL`+`WINDOW_INVALID`，报告不崩溃；`status` 文本先 strip 再大写。
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
- **3.0 → 3.1（本 r2）**：新增 reason_code、可选字段（`OrderQuantity.requested_qty`、
  `ManagementAuthorization.environment/runtime_instance_id`、`AuthorizationVerdict.submit_allowed/
  reconcile_allowed`、`OutcomeRevision.revision_id/previous_revision_id`）、`validate_bundle` 与
  `validate_authorization` 的 fail-closed 收紧。契约尚未被 U01–U05 集成、未打标签，因此不承担旧调用兼容；
  集成后新增字段仍按“带默认值可选”约束，收紧判定必须登记为小版本行为变更。
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

## 12. 已知限制、P2 处置与下游缺口索引

**已修（r2）**：K03 COMPLETE+gaps 矛盾、Decimal scale 误冲突、负权重/负末腿分摊、`coverage=None`
归因 fail-closed、`finalize_costs(coverage=None)` 保留归因状态、迟到修订 revision_id 与多义拒绝、
元组主键与键规范化、K05 非法 span 拒绝与 cycle 去重、K06 畸形窗口/状态空白、K01 空集合语义与
死常量、K03 schema 补齐 `ZeroFeeProof/PaginationDecision`、幂等键转义防碰撞。

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

其余接口缺口（C 报告 §5 的 18 项、B 报告 §5 的 U01/U02/U04 清单）不改本契约冻结面，
由 I 在分派时按 `implementation_requires` 对应任务登记；新增字段仍按 §10 规则小版本演进。

# K01–K06 冻结契约 v3.0（U00）

状态：FROZEN 候选（待 I/Q/C/B 对完整候选审查后按流程打 `contract-v3.0`）。
来源：[无人值守交付与多 Agent 执行规格 v3](../无人值守交付与多Agent执行规格_2026-09-19.md) §4/§10/§11.1/§12/§13；
[决策链路专项评估与优化报告](../决策链路专项评估与优化报告_2026-09-19.md) §3/§5/§7/§9；U00 任务卡与 WL 指纹差异清单
（`docs/dispatch/reviews/U00/w0/F_fingerprint_r1.md`）。

## 1. 内容与边界

| 路径 | 内容 |
|---|---|
| `src/core/contracts/common.py` | 规范化 JSON 哈希（sorted keys/UTF-8/无空格）、Decimal 边界、敏感键名守卫、`[start,end)` 区间工具 |
| `src/core/contracts/k01_identity.py` | RuntimeIdentity、覆盖清单、身份不符判定 |
| `src/core/contracts/k02_evidence.py` | EvidenceBundle/EvidenceInput/DerivedFact、`build_bundle`、`validate_intent`、EvidenceVerdict |
| `src/core/contracts/k03_income.py` | IncomeEvent/IncomeCoverage/CostAttribution、幂等吸收、分页决策、零费用证明、归因、守恒、迟到修订 |
| `src/core/contracts/k04_management.py` | ManagementIntent/ManagementAuthorization/OrderQuantity 三分量 |
| `src/core/contracts/k05_trace.py` | DecisionTraceSpan/DecisionCounters、Incident/恢复谓词、AlertDelivery 状态阶梯 |
| `src/core/contracts/k06_release.py` | 五组 ReleaseEvidence 状态与阻断项清单 |
| `docs/contracts/*.schema.json` | 由 `model_json_schema()` 生成的六个 JSON schema（冒烟脚本校验其与模型同步） |
| `docs/contracts/golden/*.golden.json` | 56 条正反 golden（26 正 / 30 反），每条含规格推导 `derivation` |
| `scripts/smoke_contracts_v3.py` | 离线执行 golden 与 schema 同步断言；失败退出码非 0 |
| `scripts/run_offline_regression.py` | 统一回归入口，已登记本冒烟 |

契约包为纯函数 + pydantic v2 模型：不读取 `.env`/`data`/运行状态，不构造 Broker，不发网络请求，
不写入运行目录。真实数据由运行时模块通过显式适配器转换为“有效值字典”后传入。

## 2. 通用约定

- UTC 毫秒整数用于持久化事件时间；本进程期限使用调用方提供的 `now_ms`，契约不读墙钟。
- 新增成本/数量计算一律 `Decimal`，边界以十进制字符串序列化；`float` 直传金额抛
  `NumericInputError`，需显式使用 `decimal_from_float`。现有 `TradeIntent`/SQLite REAL 保持兼容，
  由适配器转换（R16：不得借契约触发全仓数值重构）。
- 模型均为 `frozen=True, extra="forbid"`；未知字段报错而不是静默丢弃。
- 哈希输入含 `key/secret/token/password/credential/authorization/cookie` 等子串键名（大小写不敏感）
  时抛 `SensitiveInputError`，报错只含字段路径。守卫是保守的子串匹配。
- 区间语义固定 `[start, end)`；对官方包含端点的接口，适配层用 `endTime = end - 1ms`。

## 3. K01 RuntimeIdentity

字段：`schema_version, environment, account_ref, artifact_hash, git_commit, dependency_hash,
effective_config_hash, strategy_hash, model_route_hash, runtime_instance_id`。

| 字段 | 冻结语义 |
|---|---|
| `artifact_hash` | 不可变代码发布目录内容哈希；输入 `相对路径 -> 内容`，**不含 git_commit** |
| `git_commit` | 独立身份字段；不参与文件内容哈希，但参与身份比较 |
| `dependency_hash` | 必须覆盖实际锁文件（`requirements-lock.txt`）；依赖环境与锁的比对结果另存 `comparison_status` |
| `strategy_hash` | 实际策略卡内容（`knowledge/strategy_cards.json`）；改卡即改身份 |
| `model_route_hash` | 模型名/推理档/备用路由/profile 覆盖值（解析路由后的有效值，非原始 yaml） |
| `effective_config_hash` | settings + profile + 环境覆盖后的有效非敏感值 |
| `runtime_instance_id` | 进程级运行代；身份比较默认忽略（`IDENTITY_MISMATCH_IGNORED_FIELDS`） |

接口：`hash_artifact/hash_dependencies/hash_strategy/hash_model_route/hash_effective_config`、
`build_identity(...)`、`identity_mismatches(expected, actual, ignore=...)`、`validate_coverage(payload)`。
`IDENTITY_COVERAGE` 定义四组必备覆盖字段；`validate_coverage` 返回 `组.字段` 缺口列表。

## 4. K02 EvidenceBundle 与逐意图校验

`EvidenceInput{input_id, source, event_time_ms, received_time_ms, valid_until_ms, value_ref, symbol,
interval, finality, bar_close_time_ms, values}`；`DerivedFact{fact_id, kind, inputs[], value, computed_by=code}`；
`EvidenceBundle{evidence_id, cycle_id, identity, symbol, as_of_ms, bar_times, finality, inputs[],
derived_facts, quality, expires_at_ms, content_hash}`。

- `build_bundle(snapshot, closed_bars, rules)`：只从已收盘 K 线生成事实；收盘时间晚于 `as_of_ms` 的
  bar 一律降级为 `live`；重复内容按 `(symbol, interval, close_time, values)` 去重并记入
  `quality.dedup_removed`；缺必需周期记 `quality.missing_intervals`，bundle `finality != closed`。
- `validate_intent(intent, bundle, execution_quote, now_ms=None)`：逐 intent 返回
  `EvidenceVerdict{verdict_id, checked_intent_id, intent_symbol, evidence_id, status, reason_codes,
  claim_results, checked_at_ms}`。不同 intent 必须分别调用；verdict 绑定自身 intent_id/evidence_id。
- claim 表达式：`lhs op rhs`，操作数支持字面量、`<interval>:<fact_kind>`（如 `1h:closed_close`）、
  `<input_id>.<value_key>`、唯一 fact kind。数值一律由代码从输入/事实计算，自由文本不参与执行。
- 状态优先级：`SYMBOL_MISMATCH > EXPIRED > STALE > EVIDENCE_INVALID > VALID`；`reason_codes` 汇总
  全部检出原因（去重、按检出顺序）。

主要 reason_code：`EVIDENCE_BUNDLE_MISMATCH`、`IDENTITY_MISMATCH`、`CYCLE_MISMATCH`、
`SYMBOL_MISMATCH`、`MISSING_REQUIRED_INTERVAL`、`BUNDLE_NOT_CLOSED`、`BUNDLE_EXPIRED`、
`UNCLOSED_BAR`、`FUTURE_BAR`、`FUTURE_INPUT`、`INPUT_STALE`、`NON_FINITE_VALUE`、`CLAIM_FALSE`、
`CLAIM_UNRESOLVED`、`MISSING_CLAIMS`、`MISSING_EXECUTION_QUOTE`、`QUOTE_SYMBOL_MISMATCH`、
`QUOTE_NON_FINITE`、`QUOTE_EXPIRED`。报价绑定 `intent.symbol`（OPEN 意图必须携带有限、未过期报价）。

## 5. K03 收入、覆盖与成本

| 模型 | 关键规则 |
|---|---|
| `IncomeEvent` | 幂等键 `(environment, account_ref, income_type, tran_id)`；**symbol/asset 是被校验内容**，不进键；金额 Decimal |
| `ingest_income(page, existing)` | 同键同内容→`duplicates`；同键异内容→`quarantined`（`INCOME_ID_CONFLICT`），不覆盖既有记录 |
| `IncomeCoverage` | `[start,end)`、watermark、page_limit、`pagination_terminated_evidence`、`PENDING/COMPLETE/GAP`、gaps |
| `plan_next_page` | 短页终止（`PAGE_SHORT`）；满页时 `next_start_ms = max(event_time_ms)`（同毫秒重叠重查，**不是 +1ms**），`overlap_recheck=true` |
| `zero_fee_proof` | 零费用需同时：来源正常 + 分页终止 + watermark 覆盖 + 无缺口 + 迟到复查；否则列出具体 reason |
| `attribute_income` | 按事件时点可证明仓位；双向仓/多生命周期歧义→`UNATTRIBUTED`；覆盖非 COMPLETE→`UNKNOWN` |
| `check_conservation` | Σallocated == 源金额（默认精确相等），不符抛 `ConservationViolation` |
| `allocate_weighted` | 明示权重分摊，末腿承接余数，保证精确守恒并可重放 |
| `finalize_costs` | `cost_status` 与 `attribution_status` 独立；coverage 非 COMPLETE 时 cost 不得 COMPLETE；空窗口需零费用证明 |
| `apply_late_income` | 迟到收入只产生 `OutcomeRevision{requires_recalculation=true, new_closed_outcome_created=false}` |

## 6. K04 管理意图、授权与数量

- `ManagementIntent`：`MY` 动作只有 `CLOSE/REDUCE/MAINTAIN_PROTECTION`；幂等键 =
  `lifecycle_id|trigger_type|policy_version|trigger_event_id`，`derive_management_id` 由其规范化哈希派生，
  跨轮稳定、不随机。
- `validate_authorization`：仅管理动作；拒绝 `OPEN_NOT_ALLOWED`、`NET_EXPOSURE_INCREASE`、
  `CANNOT_REMOVE_LAST_PROTECTION`、`STALE_POSITION_REVISION`（旧授权失效）、`AUTHORIZATION_EXPIRED`、
  `REDUCE_EXCEEDS_AUTHORIZATION`、`INSUFFICIENT_POSITION`、账户/生命周期/方向不符。
- `OrderQuantity`：`accepted = cumulative_filled + working_leaves + terminal_unfilled`；
  NEW/PARTIALLY_FILLED→`terminal=0`；终态→`working=0`；撤单未确认→`terminal=0`
  （`CANCEL_UNCONFIRMED_TERMINAL_COUNTED`）；`apply_fill` 允许终态迟到 fill 只修订事实不复活订单，
  并递增 `revision`。

## 7. K05 轨迹、事件与告警

- `DecisionTraceSpan` + `DecisionCounters`：`purpose` 经 `purpose_kind` 映射，cycle/api_attempt
  （含 tool_call）/decision 分别计数；`validation` 明确不计数；未登记 purpose 抛
  `UnclassifiedPurposeError`，禁止猜测归类。
- `Incident`：稳定 key = `profile|object_ref|reason_code`；`ACK` 只记录 `ack_at_ms`，
  `resolve_incident` 只有在恢复证据齐全、新鲜（`max_age_ms`）且满足谓词时才置 `RESOLVED`，
  并写入 `resolution_evidence_hash`；证据缺失/陈旧/来自未来/为假分别返回 reason code。
- `AlertDelivery` 阶梯：`UNKNOWN → SUBMITTED（submitted_at）→ ACCEPTED（provider_receipt）→
  DELIVERED（delivered_at_ms）→ ACKNOWLEDGED（ack_at_ms）`；HTTP200 只到 `SUBMITTED`；
  无设备回执不得 `DELIVERED`，只能以人确认 `ACKNOWLEDGED`；声明状态与事实不符时
  `delivery_violations` 列出 `STATE_MISMATCH` 等违规。

## 8. K06 ReleaseEvidence

- 五组固定：`engineering, accounting, runtime_observation, operations, strategy_evaluation`；
  每组 `{status∈{PASS,FAIL,NO_DATA}, evidence_hash, window_start_ms, window_end_ms, identity_ref}`。
- `PASS` 必须同时具备证据 hash、有效窗口（`start < end`）与匹配身份；缺证据→`NO_DATA`+阻断项，
  声明 PASS 但证据不完整→`FAIL` 并列出 `PASS_WITHOUT_EVIDENCE/WINDOW_MISSING/IDENTITY_REF_MISSING`。
- `NO_DATA != PASS`；组与组独立评估。
- **不建立第二套 GO 判断器**：报告模型没有整体 GO/NO_GO 字段，只输出五组状态与阻断项清单；
  最终门禁仍由既有 `release_evidence` 计算器与发布审查决定（U09/U12）。

## 9. 与既有 ReleaseManifest 字段的显式映射（U15/I 集成依据）

现有 `src/core/release_manifest.py` 的 `ReleaseManifest` 与 K01 的对应关系如下；命名差异是
F 指纹清单的已知项（`F_fingerprint_r1.md` 第 12/5/3/4 条），U15 集成时按此映射生成身份，
**U00 不修改 `release_manifest.py`**（不在本任务独占路径）。

| K01 字段 | 现有来源 | 差异与要求 |
|---|---|---|
| `artifact_hash` | `content_hash`（`src` + 已跟踪 scripts 的内容指纹） | 语义一致，仅命名不同 |
| `git_commit` | `git_commit` | 一致；契约要求无 git 时显式 UNKNOWN 处理，身份比较不忽略 |
| `dependency_hash` | `dependency_hash`（`DEPENDENCY_FILES`） | **现缺 `requirements-lock.txt`**；契约要求锁文件纳入，比对结果另存 |
| `strategy_hash` | `strategy_config_hash`（decision_policy + risk_learning） | **现缺 `knowledge/strategy_cards.json`**；契约要求覆盖实际策略卡 |
| `model_route_hash` | `model_config_hash`（原始 agents.yaml + settings.yaml） | 契约要求**解析后的有效路由**（含环境变量/MTA_AI_DECISION_PROVIDER/备用路由/profile 覆盖） |
| `effective_config_hash` | 无（`config_hashes` 仅逐文件哈希且无消费方） | 契约要求 profile/环境合并后的有效非敏感值 |
| `account_ref` | `account_ref`（已脱敏短哈希） | 一致；契约要求 environment 与状态根一致性校验由 U15 补 |
| `runtime_instance_id` | 无（仅进程内存 uuid） | 新增字段；U15 需持久化运行代并与实例锁绑定时使用 |
| `environment` | `environment` | 一致；不在 `release_id` 种子的缺陷由 U15 处理 |
| —（K01 不含） | `release_id / egress_id / threshold_version / manifest_schema / schema_version(int)` | 保留在 ReleaseManifest；K01 的 `schema_version` 是契约版本字符串 |

K06 与既有 `src/core/release_evidence.py` 的关系：K06 只冻结“五组状态 + 阻断项”的**输出结构**，
U09 扩展现有计算器与报告契约，不得新建平行 GO 逻辑；`NO_DATA`/`FAIL` 的判定输入来自现有证据表。

## 10. 版本与兼容规则

- `SCHEMA_VERSION = "3.0"`；`RuntimeIdentity.schema_version` 是字符串，允许 `3.x` 小版本。
- **小版本（3.0 → 3.1）**：只允许新增带默认值的可选字段、新增 reason_code、新增枚举值（消费方必须
  容忍未知值）；不得改变既有字段类型、语义、`[start,end)` 区间或状态优先级顺序。golden 追加新用例，
  既有用例不得修改期望值。
- **大版本（3.x → 4.0）**：删除/重命名/改类型字段、改变状态优先级、收紧必填、改变守恒或时间语义、
  改变幂等键组成，均为大版本；需新契约提交、I 重新冻结并在任务清单登记。
- 冻结后修复只允许“补 golden 或补文档”，代码修复须走大/小版本规则并保留旧版兼容读取。
- 现有 `TradeIntent`/SQLite 字段不动；新增 Decimal 经适配器转换，展示四舍五入不回流执行计算。

## 11. Golden 与冒烟

- golden 位于 `docs/contracts/golden/*.golden.json`，每条用例含 `case_id/kind/operation/inputs/
  expect/derivation`；`derivation` 必须写明期望值来自规格条款、公式或交易所语义，禁止用被测实现输出自证。
- 运行（PowerShell，仓库根）：

```powershell
$env:PYTHONUTF8=1
& "..\multi-agent-trader\.venv\Scripts\python.exe" scripts\smoke_contracts_v3.py
```

- 该脚本也被 `scripts/run_offline_regression.py` 登记；冒烟只读 `docs/contracts` 与内存模型，不写任何
  运行目录。新增用例时：在对应 golden 追加用例并选择既有 `operation`；若需新 operation，在冒烟脚本
  增加 handler 并保持“期望全部来自 fixture”的原则。

## 12. 已知限制与未决项

- 契约只冻结结构与判定函数；与真实运行数据的接线（RuntimePaths、配置解析、Outbox/Broker 适配、
  release_evidence 改造）属 U15/U13/U04/U09/U12，U00 不实现运行门禁。
- K01 敏感键名守卫是保守子串匹配（例如 `monkey` 也会被拒绝）；如运行配置存在类似键名，需在 U15 用
  显式白名单适配。
- K02 claim 语法限定为单比较表达式；不承诺识别自然语言矛盾（R05），复杂策略判断应拆成多个 claim。
- K03 的“迟到复查”与来源可用性必须由 U01 提供显式布尔证据，契约不自行探测来源。
- K05 的 `max_age_ms`、K06 的窗口阈值等具体数值仍在 U12/U13 冻结；本契约只定义判定结构。
- 本包未做性能基准；均为纯函数与 pydantic 校验，预计不进入快循环热点。

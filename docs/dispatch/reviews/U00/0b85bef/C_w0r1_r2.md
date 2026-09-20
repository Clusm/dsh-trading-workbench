# U00 契约候选复审报告 — C / 决策证据（w0r1 r2）

- 复审对象：`0b85bef`（`fix(contracts): close P0/P1 review findings, add regressions (U00 r2)`）；r1 候选：`db7e0d7`；detached，与指令一致。
- 阅读范围：`git diff db7e0d7..0b85bef -- src/core/contracts docs/contracts scripts/smoke_contracts_v3.py` 全量阅读。K01/K02/K05 做逐条复跑 + 新绕过对抗；K03/K04/K06 只做 diff 级阅读（不在 C r1 范围，未做对抗性验证，避免与 B/Q/E 域重复）。
- 反例脚本（临时，未进 tracked）：`_review_scratch\ce_r2_k02_k05_k01.py`、`ce_r2_probe2.py`、`ce_r2_probe3.py`、`ce_r2_probe4.py`；原始输出 `_review_scratch\ce_r2*_output.txt` + 控制台运行记录。
- 边界声明：未触碰运行目录 `multi-agent-trader\`、`data/`、`.env`；未联网；未启动服务；未修改任何 tracked 文件（`git status --porcelain` 仅 `?? _review_scratch/`、`?? U00_review_C_w0r1*.md`）。
- 环境：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，`PYTHONUTF8=1`。
- 说明：r1 脚本 `ce_k02_k05_k01.py` 已无法整体重跑（CE8 现在按设计抛 `SpanValidationError`，脚本在调用处中断）；r2 使用逐用例捕获异常的适配脚本重跑全部 r1 反例。

## 0. 结论

r2 是一次实质性的 fail-closed 收紧。实测确认：`validate_bundle`（content_hash 重算 + fact 溯源 + 输入去重/时序/符号/最终性）生效；K02 字面量 claim、未知 action、缺 side、OPEN 缺绑定、必需 claim kind 均被拒；K05 恢复证据的类型/时间戳/ACK 先后、span 拒绝与 cycle 去重、DELIVERED 前置回执均生效；K01 原始文本/行列表载荷被拒、`build_identity` 默认强制四组覆盖。

**r1 判定汇总：7 项 P0/P1 中 5 CLOSED、2 PARTIAL、0 NOT CLOSED**（P0-2/P1-1/P1-2/P1-4/P1-5 CLOSED；P0-1、P1-3 PARTIAL）。

**新发现 7 项：P1 × 1、P2 × 6。** 最重的是 N1：`validate_bundle` 只重算 `missing_intervals / bar_times / finality` 三个 quality 面，其余字段（含 `stale_inputs` 与 `required_claim_kinds`）不参与自洽校验；篡改后重算无密钥的 `content_hash` 即得 `valid=true`，可把 STALE 输入洗成 VALID、把 `MISSING_REQUIRED_CLAIM` 抹除。这与 r2 自身 docstring/README 的“手工删改 quality/finality 都会在此失败”不符（k02_evidence.py:363-367、README §4:80-85）。

**README §12 缺口索引核对**：L1–L12 与 r1 §5 的 18 项相比，**明确遗漏 8 项、部分覆盖 2 项**（详见 §4）。

**smoke**：`PASS 126 / FAIL 0；golden cases=113 (positive=40, negative=73)`，schema 6/6 同步。smoke 全绿仅证明 golden 期望与实现一致，覆盖不到下述绕过（L10 亦自认 `compare()` 为期望子集断言，scripts/smoke_contracts_v3.py:721-742，已核实）。

## 1. r1 P0/P1 逐条判定

| r1 编号 | r2 证据（本轮实测） | 判定 |
|---|---|---|
| P0-1 逐意图完整检查不可达；未知 action 绕过 OPEN 门禁 | CE1 `1<2` → `EVIDENCE_INVALID/CLAIM_WITHOUT_EVIDENCE`；CE1b `action=OPENX` → `UNKNOWN_ACTION`；缺 side/缺绑定 → `MISSING_SIDE`/`MISSING_EVIDENCE_BINDING`；`required_claim_kinds` 缺项 → `MISSING_REQUIRED_CLAIM`（b1、b3；golden K02-N10..N12/N16/P4）。**残余 A**：强制杠杆存放在被校验对象 `bundle.quality.required_claim_kinds`，抹除+重哈希后 `MISSING_REQUIRED_CLAIM` 消失（EVIDENCE_INVALID→VALID，b3 `required_kinds_erase`）→ 见 N1。**残余 B**：kind 只是自由标签，与表达式/证据无绑定，三个 trivial `*.close>0` claim 即可满足三种必需 kind（b1 `new_kind_label_only` → VALID）→ 见 N4。**残余 C**：`CLOSE/REDUCE/ADJUST` 默认零 claim、零绑定即 VALID（b4，6/6 用例），“逐意图完整检查”在非 OPEN 动作上仍无载体。 | **PARTIAL** |
| P0-2 content_hash 无校验入口；事实无溯源反向校验 | CE2 改输入不重哈希 → `CONTENT_HASH_MISMATCH`（原翻转被阻断）；CE12 伪造 `DerivedFact.value`：不重哈希 → `CONTENT_HASH_MISMATCH+DERIVED_FACT_MISMATCH`，**重哈希后仍 `DERIVED_FACT_MISMATCH`**（b1）；fact 引用不存在输入/fact id/空 inputs → `DERIVED_FACT_MISMATCH`/`DERIVED_FACT_INVALID`（b2）。`validate_bundle` 已导出并先于 intent 判定执行（k02_evidence.py:358、:667-669）。残余：content_hash 无密钥、任何人可重算（改值+重哈希 → `valid=true`、claim 翻转 VALID，b1 `ce2b`），属 r1 §5 第 5 项“精确意图集合签名/封条”仍未实现（§12 未索引）；quality 校验不完整见 N1。 | **CLOSED**（r1 两条攻击） |
| P1-1 K05 恢复谓词无时间戳绕过 | 裸 bool → `EVIDENCE_TYPE_INVALID`；`{value:true}`、`{observed_at_ms:null}` → `EVIDENCE_TIMESTAMP_MISSING`；float/bool 时间戳、"false"/"0"/1 值 → `EVIDENCE_TYPE_INVALID`；future/stale/假值分别给码；`now-max_age` 恰好=新鲜、再早 1ms=STALE（b1）。新残余：OPEN（从未 ACK）可直接 RESOLVED → 见 N5。 | **CLOSED** |
| P1-2 K05 span 不设防、周期不去重 | 违规 span → `SpanValidationError(ENDED_BEFORE_STARTED)`；同 cycle 两 span → `cycles_total=1`；负 deadline → `NEGATIVE_DEADLINE_REMAINING`；`span_violations` 已从 `__init__` 导出且被 `tally` 调用（k05_trace.py:145-167）。巨值 `deadline_remaining_ms` 仍无违规码（L4 已文档化）。 | **CLOSED**（delta） |
| P1-3 K01 覆盖不强制、无 ModelRoute | 空四组 → `IdentityCoverageError` 列出 4 组；`enforce_coverage=False` 为显式离线通道（README §3 已声明）。**残余**：`model_route={"x":1}`、`effective_config={"y":2}` 可通过 `build_identity`，同 payload 经 `validate_coverage` 报 7 项字段缺口；字段级 `IDENTITY_COVERAGE` 无强制入口；仍无 `ModelRoute` 类型（k01_identity.py:125-159、:201-217；b1 `ce10`）。README L9 未索引 ModelRoute/字段级覆盖。 | **PARTIAL** |
| P1-4 K01 非 Mapping 输入可静默哈希 .env 原文 | `hash_strategy(<原文>)`/`hash_strategy(<行列表>)`/`hash_artifact(<原文>)`/`hash_effective_config(<行列表>)` 全部 `SensitiveInputError`；`hash_model_route([{...}])` → `IdentityCoverageError`（不再静默）；`{"note":"API_KEY=..."}`（值内容）仍通过 → L1 已登记为已知边界（b1 `ce11`）。 | **CLOSED** |
| P1-5 K02 输入级币种污染；无时间 bar | CE3：`symbol_mismatch_inputs=["in-x"]`，引用 → `INPUT_SYMBOL_MISMATCH`，fact 引用 → `CROSS_SYMBOL_FACT`；CE4：无 `close_time` → `finality=live/bar_times={}`，引用 → `UNDATED_BAR`，两种 rules 下均 `EVIDENCE_INVALID`。时间边界全部保守：bar_close==as_of→closed、valid_until==now→STALE、bundle expires==now→EXPIRED、quote expires==now→`QUOTE_EXPIRED`（b1、b2）。 | **CLOSED** |

r1 P2 状态（简）：P2-1 CLOSED（evidence_id seed 含输入值/最终性摘要，改值即改 id）；P2-2 CLOSED（空 symbol → `QUOTE_SYMBOL_MISMATCH`）；P2-3 文档化（README §2/§3 + L3）；P2-4 CLOSED（DELIVERED 需回执、ACK 需先 SUBMITTED，`TransitionError`）；P2-5 文档化（L5）；P2-6 部分（常量已用于错误标签，但未与真实文件清单绑定校验）。

## 2. 新发现（含 file:line + 复现）

### N1（P1）`validate_bundle` quality 自洽校验不完整；stale/required_claim_kinds 可洗白并重哈希通过

- 位置：`src/core/contracts/k02_evidence.py:358-446`（重算范围仅 :409-431 的 `missing_intervals`、:432-441 的 `bar_times`、:430 的 `finality`），docstring 宣称 :363-367；`stale_inputs` 的消费点 :297、:316-317；`required_claim_kinds` 读取点 :738-746。
- 复现：
  - b1 `new_quality_stale_tamper_rehash`：`max_bar_age_ms=1000` 建包（`stale_inputs=["in-01"]`）→ 原 verdict `STALE/INPUT_STALE`；把 `quality["stale_inputs"]` 改成 `[]` 并 `content_hash=model_content_hash(...)` → `validate_bundle.valid=true`，verdict **VALID**。
  - b3 `required_kinds_erase`：`rules.required_claim_kinds=["entry_direction","rr_min"]`，intent 不声明 kind → `EVIDENCE_INVALID/MISSING_REQUIRED_CLAIM`；抹除 `quality["required_claim_kinds"]` + 重哈希 → `valid=true`、verdict **VALID**。
  - b2 `quality_field_tamper_rehash`：`stale_inputs / undated_inputs / symbol_mismatch_inputs / dedup_removed / required_claim_kinds / max_bar_age_ms / quality.as_of_ms` 七项分别篡改+重哈希后 `valid` 全为 `true`；仅 `missing_intervals`、`bar_times` 被拦截（`QUALITY_MISMATCH`）。
- 影响：DC07 式“持久化回读后用契约 API 验证”场景下，唯一完整性入口对关键质量字段失明；“超龄输入”目前**只有** `quality.stale_inputs` 一个无 `valid_until` 的传导体（:297、:316），因此该字段可改写即等于 K02-N6 语义可被本地抹除。r2 为 P0-1 新增的强制杠杆同样落在可篡改对象里。
- 建议：`validate_bundle` 重算所有由 `inputs` 可推导的 quality 字段并逐项比对（`stale_inputs` 需 `max_bar_age_ms`，`undated/unclosed/symbol_mismatch/dedup` 可直接推导）；或把 `required_claim_kinds/max_bar_age_ms` 移出 `bundle.quality`，改为 `validate_intent(..., rules)` 参数（由验证方提供而非被验证对象自带）；README/docstring 措辞同步修正。

### N2（P2）`fact_id` 重复不校验；claim 解析取首个匹配 → verdict 顺序依赖

- 位置：`k02_evidence.py:389-407`（只逐 fact 自洽，无去重）、`EvidenceBundle.fact_by_id` :142-146、按 kind 唯一匹配 :248-252。
- 复现：b1 `new_duplicate_fact_id`：在同一 bundle 追加一条 fact_id 相同、inputs/值各自自洽的 fact → `validate_bundle.valid=true`；claim `1h:closed_close == 744.13` 在“重复条在后”时 VALID、在“重复条在前”时 `EVIDENCE_INVALID(CLAIM_FALSE)`。
- 影响：持久化数据/多来源合并出现重复 fact_id 时，结论取决于序列顺序，审计不可重放。建议增加 `DUPLICATE_FACT_ID` reason（与 `DUPLICATE_INPUT_ID` 对称）。

### N3（P2）fact kind 语义不校验：`closed_close/prior_high` 只需与所指输入数值一致

- 位置：`k02_evidence.py:323-342`（`_compute_fact_value` 取 `fact.inputs[0]` 对应字段比对）。
- 复现：b2 `fact_kind_input_swap_fixed`：把 `1h:closed_close` 的输入换成旧 bar（742.8）、`1h:prior_high` 换成新 bar（745）并让 value 匹配 → `validate_bundle.valid=true`，方向性 claim 由 VALID 翻为 `CLAIM_FALSE`。`build_bundle` 的“closed_close=最新 bar / prior_high=次新 bar”算法未被 `validate_bundle` 固定。
- 影响：跨进程重载的 bundle 可在不违反任何 reason 的情况下换指事实语义。建议校验 kind 对应的 bar 选取规则（最新/次新、同 interval 唯一）。

### N4（P2）`bound_inputs` 非授权范围、kind 无绑定

- 位置：`k02_evidence.py:696-698`（仅 OPEN 非空检查）、:716-731（claim 引用集合独立于 `bound_inputs`）、:738-746（kind 集合仅按标签比对）。
- 复现：b1 `new_claims_outside_bound_inputs`（`bound_inputs=["in-01"]`，claim 引用 `in-02` → VALID）；b1 `new_kind_label_only`（三种必需 kind 均配 `*.close > 0` trivial 表达式 → VALID）。
- 影响：OPEN 的“证据绑定”声明与真正使用的证据可以不一致；`required_claim_kinds` 只保证“有名无实”的标签存在。建议：claim 引用必须 ⊆ `bound_inputs`（或反向全覆盖校验）；README §4 明确 kind→表达式语义由 U03 规则表提供并纳入验收。

### N5（P2）OPEN（从未 ACK）可直接 RESOLVED，“ACK 之后”仅在已 ACK 时成立

- 位置：`k05_trace.py:222`（`ack_at` 仅 `status=="ACK"` 时读取）、:258-272（`resolve_incident` 无 ACK 前置）。
- 复现：b1 `new_open_incident_direct_resolve`：`status=OPEN` + 新鲜证据 → `recovered=true`、`resolve_incident` 置 `RESOLVED`；同一证据在已 ACK 且早于 `ack_at_ms` 时被 `EVIDENCE_PREDATES_ACK` 拒绝。
- 影响：模块/README §7 自述“RESOLVED 必须由 ACK 之后新观察到的证据满足”在 OPEN 路径不成立。建议 `resolve_incident` 要求 `status=="ACK"`（或显式允许 direct-resolve 并写入文档/新状态）。

### N6（P2）畸形 claims 元素抛 `AttributeError`，非 fail-closed verdict

- 位置：`k02_evidence.py:708-714`（`claim.get`）、:274-287（`_parse_claim`）。
- 复现：b2 `malformed_claims`：`claims=[None]`、`[1]`、字符串 `"abc"`（被 `list()` 拆成字符）均抛 `AttributeError: '...' object has no attribute 'get'`；`{"claim_id":...}` 缺 expr 与非法 op 则正常给 `CLAIM_UNRESOLVED`。
- 影响：r1 §5 第 2 项“非空但无效 claim 的拒绝路径”残余：U03 直接喂外部 JSON 时拿到崩溃而非可审计 reason。建议 `_parse_claim` 对非 Mapping 元素返回 None（或新增 `CLAIM_MALFORMED`）。

### N7（P2，索引遗漏）非 K 线“closed”输入无时间戳可被接受并参与 claim

- 位置：`k02_evidence.py:306-309`（`finality=="closed"` 且 `bar_close_time_ms is None` 时既不判 `UNDATED_BAR` 也不判 `UNCLOSED_BAR`）。
- 复现：b1 `new_non_bar_closed_input`：`model_copy` 追加 `in-acc`（`interval=None`、`bar_close_time_ms=None`、`finality="closed"`、symbol 匹配、`valid_until_ms` 未来）→ `validate_bundle.valid=true`，claim `in-acc.equity > 0` → VALID。
- 影响：“非 K 线证据（funding/OI/mark/basis/账户快照）的 kind/finality/TTL/missing 表示”在本包不可表达，且 `validate_bundle` 不会替该缺口兜底（r1 §5 第 8 项，§12 未索引）。建议为非 bar 输入引入 `kind`/`observed_at_ms` 的显式判定，或在 README 明确 U03 自建并纳入验收。

## 3. 复跑与反例增量（原始输出）

| 批次 | 脚本 | 关键实测 |
|---|---|---|
| b1 | `ce_r2_k02_k05_k01.py` | CE1/CE1b 关闭；CE2/CE12 关闭（含重哈希后仍拒）；CE3/CE4/CE5/CE7/CE9 关闭；CE8 违规 span 抛错、cycle 去重=1；CE10 空组拒绝/字段级仍缺；CE11 原文与行列表全拒；新：kind 标签绕过、bound_inputs 越权、stale/kinds 抹除、duplicate fact_id、非 bar closed 输入、OPEN→RESOLVED |
| b2 | `ce_r2_probe2.py` | 时间边界全保守；fact 坏引用全拒；kind 换指通过（bundle valid）；quality 7 字段可篡改；claims 畸形崩溃 |
| b3 | `ce_r2_probe3.py` | `MISSING_REQUIRED_CLAIM` 抹除后 VALID；STALE 抹除后 VALID（均 `valid=true`） |
| b4 | `ce_r2_probe4.py` | CLOSE/REDUCE/ADJUST 零 claim/零绑定 6/6 VALID |

## 4. README §12（L1–L12）与 r1 §5（18 项）核对

**已覆盖/已消解**：#1、#3 → L2；#4、#7、#14、#15 → r2 已实现（不再需要索引）；#17 → 已实现 + L1；#18 → L5；#12 的 deadline 子项 → L4。

**部分覆盖 2 项**：
- #2（按 action/side/order_type 的必需 claim 策略）：实现了 bundle 级 `required_claim_kinds`，但 per-action 映射、kind 语义绑定、以及杠杆可被抹除（N1）均未索引；
- #9（采集 deadline/尝试次数/降级原因）：仅 deadline 进 L4，attempts/降级原因未索引。

**明确遗漏 8 项**（README 全文无对应 L 条目，grep “签名/seal/market_venue/fresh_until/account_profile/taint/funnel/工具出参/non-K” 无命中）：
- #5 `ValidationBatch` 精确意图集合签名/封条；bundle 无签名字段（N1→content_hash 可自算）；对应 r1 P0-2 残余；
- #6 bundle 级字段（`schema_version/environment/market_venue/account_profile/missing_fields/fresh_until`、类型化 `quality`）；
- #8 非 K 线输入证据结构（N7 实测可通过）；
- #10 工具出参 schema 裁剪与“工具结果→证据输入”适配；
- #11 外部文本 trust/taint 通道（DC12；与 L1 的值内容扫描不是同一项）；
- #12 span→intent/order/evidence 关联字段（链路不可闭合）；
- #13 funnel 状态枚举、no_trade 一轮/一单解释、分桶耗时 helper；
- #16 `ModelRoute` 类型与 capability/mock 标志（L9 只覆盖 credential/account_ref）。

L 表现有条目复核：L1（值内容不扫描）、L3、L4、L5、L10（`compare()` 子集断言——已读 scripts/smoke_contracts_v3.py:721-742 核实准确）、L2 均与实现一致；L6/L7/L8/L11 属 B/U01/U04 域，未逐项复核。

## 5. smoke 实测输出（2026-09-21 本次运行）

```
结果：PASS 126 / FAIL 0；golden cases=113 (positive=40, negative=73)
schema 6/6 PASS（k01..k06 与冻结模型一致）
K02: P1..P5 + N1..N19 全 PASS（含新增 N10..N19、P4、P5）
K05: P1..P7 + N1..N10 全 PASS（含 N5..N10、P6、P7）
```

命令：`& <venv-python> scripts\smoke_contracts_v3.py`（`$env:PYTHONUTF8=1`）。

## 6. 同模型偏差声明

- 本报告由 opencode 子代理执行，模型 `deepseek-v4.1-flash`；被审契约与专项报告可能出自同族模型，存在共同盲区（例如对“quality 自洽”措辞的习惯性信任、对 golden 自证循环不敏感）。本报告结论均给出可复算证据（`0b85bef` 行号、b1–b4 脚本与实测输出），建议 Q/E 用独立夹具复验 N1 与 P0-1 残余。
- 边界复核：本会话未进入/读取运行目录 `multi-agent-trader\`、`data/`、`.env`；未联网；未启动服务；未修改任何 tracked 文件（HEAD=0b85bef，`git status --porcelain` 仅 `_review_scratch/` 与报告文件为 `??`）。

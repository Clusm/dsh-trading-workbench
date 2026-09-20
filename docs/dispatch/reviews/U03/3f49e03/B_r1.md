# U03 初审（B 角色：持仓与执行绑定）— r1

- 审查对象：`task/U03` head `3f49e03a6a1d8c90b7abea018e51f4994cca160b`（基线 `ffdab44`）
- 工作区（独立 detached）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U03\B\r1`（`git status` 干净，HEAD 与送审一致）
- 解释器：`multi-agent-trader/.venv` python 3.13.12，`PYTHONUTF8=1`；无 pytest，全部为自写探针 + 作者脚本复跑
- 纪律遵守：只读；未提交/推送/切分支；未读 `.env`/`data/`/密钥；未联网；未启停服务；未写 `mat-wt/I`、`mat-wt/CD`

## 结论：**CHANGES_REQUESTED**

执行绑定（B 域）方向有一处 P0（本 head 让仓库统一离线回归入口直接红）+ 三处 P1（身份未固定 fail-open、畸形 claim 元素崩溃、证据→订单无落地绑定）。修复量都很小，但都落在"执行侧能不能真的证明这一单的证据是哪个"这条主线上。

已独立确认（与编排者一致，属"复核"不含于结论依据）：
`tests/integration/test_research_evidence.py` → `PASS 79 / FAIL 0`，exit 0；`tests/integration/test_market_evidence_quality.py` → `PASS 36 / FAIL 0`，exit 0（原始输出 `_scratch_B/r1_test_research.txt`、`_scratch_B/r1_test_market.txt`）。
作者"全 PASS"不作为结论依据；以下每条都有独立反例与原始输出。

---

## 问题清单

### P0-1 本 head 使 `scripts/run_offline_regression.py` 在清单门禁处直接失败（共享回归变红）

- 证据：`_scratch_B/probe_manifest.txt`
  ```
  MANIFEST FAILED: RuntimeError 未登记冒烟脚本: scripts/smoke_u03_market_evidence_quality.py, scripts/smoke_u03_research_evidence.py
  ```
- 复现：以模块方式加载 `scripts/run_offline_regression.py` 并调用 `_validate_smoke_manifest()`（`scripts/run_offline_regression.py:95-121`）。该函数把 `scripts/smoke_*.py` 的全集与 `SCRIPTS ∪ EXCLUDED_SMOKE_SCRIPTS` 求差，差集非空即 `RuntimeError`。
- 成因：commit `ccfd7d9` **新增** `scripts/smoke_u03_research_evidence.py`、`scripts/smoke_u03_market_evidence_quality.py`，但登记补丁 `tests/fixtures/evidence/u03_offline_regression_registration.patch` 只是**提案**，共享文件 `scripts/run_offline_regression.py` 未改（作者声明"未改共享文件"，属实，但代价就是这个）。
- 附带的边界问题：任务卡 `docs/无人值守升级任务清单_2026-09-19.json` 中 U03 的 `owned_paths` 只有 `src/agents/research_evidence.py`、`tests/integration/test_research_evidence.py`、`tests/fixtures/evidence/`、`tests/integration/test_market_evidence_quality.py`，**不含 `scripts/`**，且 U03 无 `shared_files` 键。新增两个 `scripts/smoke_u03_*.py` 越界。
- 要求：要么把两个脚本移出 `scripts/`（例如并入 `tests/integration/` 由已有入口驱动），要么由 I 在合入前应用登记补丁并补 `owned_paths/shared_files`。当前状态下"新增冒烟可运行"这一自证本身成立（两个脚本直接跑 exit 0），但统一回归入口已被打红。

### P1-1 丢 id / 缺证据的身份未固定（UNPINNED）对执行是 fail-open

- 位置：`src/agents/research_evidence.py:1008-1010`（`identity is None → UNPINNED_IDENTITY`）、`:903-911`（`decision_gate()`）、`:1200-1209`（报告字段）。
- 反例（`_scratch_B/probe_b.txt` P3）：不传 `identity`，其余全部合法时
  ```
  identity_pinned = False
  report.ok       = True
  accepted_ids    = ('intent-A',)
  decision_gate   = {'ok': True, 'identity_pinned': False, 'drop_intent_ids': [], 'reasons': {}}
  identity_hash   = 5ee087180384443493bc25c3a594334127ba4895958e8f9049c02be897f2a73d
  ```
  即：**未固定身份的证据包会被判 VALID 并进入"可开仓"集合**，而 `decision_gate()` 给出的 `ok=True` / `drop_intent_ids=[]` 与固定身份时**逐字段相同**，唯一差别是一个布尔 `identity_pinned`。消费方只要按 `ok`/`drop_intent_ids` 写逻辑就会放行。
- 更关键：`report.identity_hash` 在 UNPINNED 下**照常有值**（是 unpinned 身份的哈希），把"身份未核对"伪装成一个可用的身份摘要。
- 接线印证：`tests/fixtures/evidence/u03_orchestrator_integration.patch:45` 写死 `identity=None`，且补丁只在 **blocked 路径** 记录（`raw={"identity_pinned": report.identity_pinned}`），accepted 路径只往 `reasoning` 追加一段中文说明（patch:68-69），**不阻断**。也就是说该补丁一旦照抄合入，运行期就是"未固定身份下放行开仓"。
- 要求（择一，按 fail-closed 排序）：① `identity is None` 时 OPEN 一律判 `IDENTITY_UNPINNED` 并进 `drop_intent_ids`；② `decision_gate()` 在 `identity_pinned=False` 时返回 `ok=False` 且把全部 OPEN 列入 `drop_intent_ids`；③ 至少让 `identity=None` 成为显式调用方声明（例如必须传 `allow_unpinned=True`），缺省拒绝。作者自述"U15 需传真实身份"是**约定**，不是**门禁**。

### P1-2 模型提供的畸形 claim 元素 → 门禁崩溃（不是结构化拒绝）

- 位置：`src/agents/research_evidence.py:1095-1097`
  ```python
  declared_claims = item.get("claims")
  claims = [dict(claim) for claim in declared_claims] if isinstance(declared_claims, (list, tuple)) else []
  ```
  在把 claims 交给 K02 之前先无条件 `dict(claim)`。
- 反例（`_scratch_B/probe_b.txt` P4）：
  ```
  claims=[None]                             -> RAISED TypeError: 'NoneType' object is not iterable
  claims=['abc']                            -> RAISED ValueError: dictionary update sequence element #0 has length 1; 2 is required
  claims=[123]                              -> RAISED TypeError: 'int' object is not iterable
  claims=[[]]                               -> RAISED ValueError: argument must be a sequence of length 3
  ```
- 与契约/自述矛盾：README §4 明写"非 Mapping 的 claim 元素→`CLAIM_MALFORMED`（结构化拒绝，不崩溃）"，且 `src/core/contracts/k02_evidence.py:1014-1033` 确实把非 Mapping 元素结构化记为 `CLAIM_MALFORMED`——但 U03 在到达 K02 之前就抛了。模块 docstring（`:15-19`）"claim 只从 `CLAIM_KIND_REGISTRY` 的三种 kind 生成"也因此不成立：模型自报 claims 是公开入口且未做形态守卫。
- 可达性：经由公开 API `evaluate_open_intents` 可达；**经提议的 orchestrator 接线不可达**（`src/agents/models.py:85-119` 的 `TradeIntent` 无 `claims` 字段，`model_dump` 不会产出该键）。因此这是"U03 作为门禁模块的契约缺口"，不是当前运行链的即时崩溃。K02 README §12 L2 明确 K02 intent 仍是自由 dict、DL02 的几何/claim 面归 U03，所以该入口会长期存在。
- 要求：`dict(claim)` 前加 `isinstance(claim, Mapping)` 守卫，非 Mapping 的元素要么原样透传给 K02（由其记 `CLAIM_MALFORMED`），要么由 U03 记一个自有 reason code 拒绝。

### P1-3 证据 → 订单没有落地绑定，"这一单的证据是哪个"事后不可证

- 模块侧：`binding_id` 在 `:1180-1183` 计算，`evidence_id` 在 `:1186` 取自 `bundle.evidence_id`；两者**只进内存报告**，不落盘、不回传执行侧（探针 R2-E：`research_evidence.py` 内 `log_risk_event/db./sqlite/json.dump/write_text` 全为 False）。
- 接线侧：`tests/fixtures/evidence/u03_orchestrator_integration.patch` 全文**不出现** `binding_id` / `evidence_id`；补丁只在被拒路径写一条 `db.log_risk_event`（patch:53-62）。放行路径**零审计**。
- `evidence_id` 本身不具"逐意图"区分度（探针 P1）：
  ```
  A status VALID accepted True  evid eb-714f52bca5f0bc2d
  B status EVIDENCE_INVALID accepted False evid eb-714f52bca5f0bc2d
  SAME evidence_id ? True    binding_id A/B differ ? True
  ```
  同一 `(symbol, interval, cycle, as_of, 输入)` 派生出**同一个** `evidence_id`（`build_bundle` 的 seed 见 `k02_evidence.py:876-890`）。同轮同币同周期的两笔单会携带同一 `evidence_id`，所以 `evidence_id` 不能作为"这一单的证据"。逐意图区分只靠 `binding_id`（含 `intent_id`+`status`），而它不落地。
- 与契约登记一致：K02 README §12 下游缺口索引 C#12"span→intent/order/evidence 关联字段（链路闭合）"本就登记为 U05 缺口，且 §12 L13 明写 `content_hash`"不是签名/封条"。所以作者的"`binding_id`/`evidence_id` 足以让执行侧证明这一单的证据是哪个"**在交付物内无任何支撑**。
- 要求：要么把 `binding_id`/`verdict_id`/`evidence_id` 随 accepted intent 落到审计/订单侧并要求执行链携带；要么在提交包 `known_limits` 里明确写"本模块不提供执行侧证据溯源，归 U05/C#12"，不要以"已绑定"表述。

### P2-1 周期标签由调用方给定且从不回校 → 1h K 线可被判为"已收盘 15m"证据

- 位置：`snapshot_from_tool_result(..., interval=...)`（`:407-497`）与 `bars_from_packed_candles` 的 `span = INTERVAL_MS.get(str(interval))`（`:266`）。模块只用**调用方传入的 interval 字符串**，从不与出参自身的 `interval` 交叉核对。
- 反例（`_scratch_B/probe_b.txt` P12，用作者自己的夹具）：
  ```
  labelled 1h  : bars=2 intervals={'1h'}  finality={'closed'}
  labelled 4h  : bars=2 intervals={'4h'}  finality={'live'}
  labelled 15m : bars=2 intervals={'15m'} finality={'closed'}
  1h-data-as-15m -> VALID accepted True ()
  ```
  把 1h 出参标成 `15m`：收盘时间被算成 `开盘+15m-1ms`（早于真实收盘），于是**通过 `finality=closed` 检查**，产出 `15m:closed_close`/`15m:prior_high` 事实，声明 15m 的 OPEN 意图拿到 VALID。反向（1h 数据标 4h）收盘时间落到未来 → `live` → 拒绝，属于"歪打正着"的 fail-closed。
- 接线隐患：`u03_orchestrator_integration.patch:104` `_interval = str(result.get("interval") or "15m")`。当前 `query_symbol_detail` 的成功路径与 stale 错误路径都带 `interval`（`src/data/market_query.py:224`、`:203`），所以 `or "15m"` 目前不可达为害；但任何一次出参缺 `interval` 就会把任意周期洗成 15m。这是"15m 不得冒充 4h"承诺的唯一支点，却是调用方的默认值。
- 要求：`result.get("interval")` 存在且与入参不等时**拒绝**建快照；去掉 `or "15m"` 兜底（缺标签即 fail closed）。

### P2-2 `required_sources` 是被门禁者自报，缺源可"自己声明不依赖"绕过

- 位置：`:818-820`（`raw.get("required_sources")`）、`:968-986`（`_source_gate`）、`:1065`（调用点）。
- 反例（`_scratch_B/probe_b.txt` P15）：用作者自己的 `funding_missing_not_zero` 场景，仅把 `required_sources` 改成 `[]`
  ```
  required_sources=[] -> VALID accepted True ()
  missing_fields recorded: ('mark_funding.mark_price','mark_funding.funding_rate','open_interest.open_interest_usd')
  ```
  即 funding/OI 全缺仍判 VALID；缺失只在 `missing_fields` 留痕，不阻断。
- 这正是本模块存在的理由（DC11："funding 缺失被读成费率中性"）所对应的失败模式，而"是否设防"由**被门禁的模型**决定。作者在 `test_market_evidence_quality.py` 的 `optional_source_missing_not_blocking` 里把它断言为预期行为，故这是设计选择而非缺陷，但 B 域不同意其默认方向：`required_sources` 应由策略卡/thesis 侧给定，或在未声明时默认为全部 `SOURCE_NAMES`。
- 要求：至少默认全源必需；若坚持自报，需在提交包 `known_limits` 明写"缺源门禁为自报口径，可被意图自身关闭"。

### P2-3 `intent_id` 取位置序号，且丢了 `client_order_id`；重复/合成 id 碰撞无守卫

- 模块侧：`:1023-1028` 缺 id 时合成 `f"open[{position}]"`；`normalise_intent:805` 优先级是 `intent_id > client_order_id`。
- 接线侧：`u03_orchestrator_integration.patch:38` 强制 `payload["intent_id"] = f"open[{position}]"`，**丢弃真实 `client_order_id`**——而实际被执行的集合是 `verdict.adjusted_intents`（`src/agents/execution.py:282`、`src/core/decision_pipeline.py:14`），风险层会 `model_copy(update={"qty": ...})` 缩减数量（`src/agents/risk.py:241-243`）并跳过失败项，位置序号在执行集合里不再可靠；`client_order_id` 会被 `model_copy` 原样保留，是天然可用的稳定键。
- 反例（`_scratch_B/probe_b.txt` P5）：
  ```
  intents -> [('open[0]','VALID',True), ('open[0]','VALID',True)]
  accepted_ids ('open[0]','open[0]') rejected_ids ()
  dup-id gate: {'ok': False, 'identity_pinned': True, 'drop_intent_ids': ['dup'], 'reasons': {...}}
  ```
  无 id 的意图与显式 id 为 `open[0]` 的意图碰撞 → 两条同名、`by_intent` 只返回第一条；显式重复 id 且一条被拒时 `drop_intent_ids` 会把两条一起丢（方向 fail-closed，但报告失去逐意图分辨力）。
- 要求：接线优先用 `client_order_id` 作绑定键（缺则退回位置序号并在报告标注）；对重复/空 id 做结构化拒绝。

### P2-4 时效窗口全部是调用方参数，无冻结上限（过期证据可支撑新开仓）

- 位置：`:996-999`（`quotes/max_bar_age_ms/max_snapshot_age_ms/expires_in_ms`）、`:413`（`snapshot_from_tool_result(max_bar_age_ms=)`）、`:1129`（`quote_map` 覆盖快照派生报价）。
- 反例（`_scratch_B/probe_b4.txt`）：把快照按 `max_bar_age_ms=1e9` 建包，再传 `expires_in_ms=1e9`、`max_snapshot_age_ms=1e9`、并自供 `quotes`：
  ```
  status VALID accepted True codes []
  bar valid_until: 1790862400000 now: 1790726400000   # 快照 10 天前的 1h bar
  ```
  即 10 天前的 K 线可支撑一次新开仓。默认值（7.2e6/6e5/6e4）+ 现有接线用默认值 ⇒ 现状安全；但模块内部**没有任何上界**，且模块常量级的唯一硬约束 `SOURCE_TTL_MS`（30s/60s）只在源被列入 `required_sources` 时才生效（`:984`）。
- 附带确认（正例）：不改参数时，`now+61s` 即 `EXPIRED`（`_scratch_B/probe_b.txt` P8），`now_ms` 为必传关键字（缺省 → `TypeError`，`:1002`）。K02 README §12 L3 要求的"调用约定强制 `now_ms`"已满足。
- 要求：给三个窗口加与配置冻结值绑定的上界；OPEN 路径拒收调用方自供 `quotes`（或要求其 `quote_time_ms >= snapshot.as_of_ms` 且落在模块 TTL 内）。

### P2-5 提议接线：局部拒绝会把**所有**存活开仓一起打掉，并误报 critical 事件

- 位置：`u03_orchestrator_integration.patch:70-72`
  ```python
  return decision.model_copy(update={"intents": kept, "decision": action, "pipeline_authorized": False, ...})
  ```
- 下游语义：`src/core/decision_pipeline.py:69-85` 的 `filter_unauthorized_opens` 在 `pipeline_authorized` 为假时，对 `raw + adjusted` 里**任何** open 一律丢弃，并写 `db.log_risk_event(event_type="pipeline_bypass_blocked", severity="critical", details="Broker 收到未授权开仓")`。因此只拒一个意图 ⇒ 其余合法开仓也被丢，且每轮报一条"未授权开仓"critical 事件（告警噪音/可能触发健康门禁）。方向 fail-closed，但与补丁自述"只丢证据不足的开仓意图、退出不受影响"不符（退出确实不受影响：`kept` 无 open 后 `filter_unauthorized_opens` 提前返回）。
- 要求：局部拒绝时不要动 `pipeline_authorized`（改为只过滤 `intsents`/`adjusted_intents`），或同步把 `adjusted_intents` 也过滤并保留签名，避免误报。

### P3-1 退出路径根本没有证据绑定（"不误伤"是靠不评估实现的）

- 位置：`:1035-1045` → 非 OPEN 一律 `NOT_APPLICABLE`、`accepted=True`、`evidence_id=""`、`binding_id=""`。
- 反例（`_scratch_B/probe_b2.txt` R2-C）：三个退出意图配**空快照列表**（连该 symbol 的快照都没有）
  ```
  c1 CLOSE  status=NOT_APPLICABLE accepted=True evid='' bind=''
  r1 REDUCE status=NOT_APPLICABLE accepted=True evid='' bind=''
  a1 ADJUST status=NOT_APPLICABLE accepted=True evid='' bind=''
  gate: {'ok': True, 'identity_pinned': True, 'drop_intent_ids': [], 'reasons': {}}
  ```
- 结论：作者"CLOSE/REDUCE 路径的证据绑定正确（自验）"**不成立**——本模块对退出**不产出任何绑定**。"不误伤退出"这一条成立，但它是"不评估"的同义反复。README §6 要求 `ManagementIntent.evidence_id` 非空，退出证据需另处提供。
- 要求：把这条写成显式下游缺口（K04 侧提供），不要在交付说明里表述为"绑定正确"。

### P3-2 `MODEL_FACT_OVERRIDE` 只扫顶层键

- 位置：`:1032-1033` `overrides = [key for key in MODEL_FACT_KEYS if key in item]`（`item` 是摊平后的顶层 dict）。
- 反例（`_scratch_B/probe_b4.txt`）：
  ```
  nested evidence facts -> status VALID codes ()
  normalised intent keys: [... 'evidence' ...]
  MODEL_FACT_KEYS hit on normalised: []
  ```
  `intent["evidence"] = {..., "close":900, "prior_high":900, "facts":{...}}` 不触发任何 reason code。
- 影响有限：嵌套值不被消费为事实（`evidence` 里只有 `trigger_price`/`risk_reward_ratio`/`confirmation_interval`/`data_observed_at_ms` 被 `normalise_intent` 读取），所以是"被忽略"而非"被覆盖"；但 docstring `:20-23`"intent 里出现任何行情事实字段一律 `MODEL_FACT_OVERRIDE`"表述过宽。
- 备注（自我纠错）：首轮探针曾误判"注入值 900 出现在摘要"，实为时间戳 `1789862399000` 含子串 `900`；已复核摘要文本，注入值**未**进入摘要（`_scratch_B/probe_b4.txt` R2-A）。本项不构成泄漏。

### P3-3 非 Mapping 的 intent 元素使整批判定崩溃

- 位置：`_as_mapping` `:790-796` 显式 `raise TypeError`。
- 反例（`_scratch_B/probe_b.txt` P10）：`None` / `"not-a-mapping"` / `123` / `["a"]` 全部 RAISED，整批不产出结论。
- 经提议接线不可达（intent 来自 pydantic 校验过的 `TradeIntent`）。要求：批量入口对非 Mapping 元素按"结构化拒绝该条"处理，或至少在 docstring 里写明该入口要求 Mapping 序列。

### P3-4 显式 `bound_inputs=[]` / `claims=[]` 被静默改写

- 位置：`:1098-1107`。反例（`_scratch_B/probe_b.txt` P7）：显式 `bound_inputs=[]` → `VALID ()`（被自动派生覆盖）。自动派生出的引用是真实 bundle 引用，无泄漏；但 K02 对"OPEN 空绑定"判 `MISSING_EVIDENCE_BINDING`，U03 却替调用方补上了——与"尊重调用方显式声明"的口径不一致。

---

## 共享适配补丁审读

### `tests/fixtures/evidence/s01_market_query_adaptation.patch`

- **可应用**：用作者自己的严格 applier（`test_market_evidence_quality.py:68-125`）对当前基线重放，`patched != original` 且未抛错（`_scratch_B/probe_b2.txt` R2-F）。基线漂移会立刻失败，这点做得对。
- **出参键名保留，但值类型改变**：`funding_rate`/`mark_price`/`open_interest_usd` 由 `float`（缺失时 `0.0`）改为可能为 `None`（patch:120-125、patch:134）。作者的断言只检查**键存在**（`test_market_evidence_quality.py:156-158`），不检查类型，"兼容既有消费端"这一条**未被测试覆盖**。B 域复核：
  - 仓库内对这三个键做算术的消费者：**未发现**（`grep` 命中均为无关项：`src/audit/database.py` 的 `mark_price_samples`、`src/intel/*` 的另一数据源、`src/core/portfolio_policy.py:87` 读的是持仓的 `mark_price`；`smoke_p0c.py` 只断言结构且被 `EXCLUDED_SMOKE_SCRIPTS` 排除）。
  - `data_quality["sources"]` 的唯一仓内消费者是 `src/core/position_guardian.py:79-81`，只读 `reason_code`，不受新增字段影响。
  - 结论：仓内未见破坏，但"兼容"结论**不能外推到仓外消费者**（dashboard/其它进程/历史落库行）——这部分离线无法验证，明说。
- **`live_price_available = bool(ticker_price or mark_price)`**（patch 未改此上下文行）在改成 `_source_number` 之后，真实价格 `0.0` 会被当作"无实时价"而整体置 `data_quality.ok=False`。价格不为 0，影响可忽略，但与本补丁要修的"缺失≠零"是同族口径问题。
- **源时钟未被夹紧**：补丁只把 `fresh_until_ms` 夹到 `min(source_at, observed)`，`source_at_ms` 原样保留；`research_evidence._source_inputs`（`:603-626`）随后令 `event_time_ms = source_at_ms`，而 `validate_bundle`（`k02_evidence.py:596-597`）在 `event_time_ms > received_time_ms` 时报 `INPUT_TIME_ORDER` 使整包无效。正常路径下 `observed_at_ms` 在三路取数之后才计算（`src/data/market_query.py:166`），所以 `source_at < observed` 恒成立；但"源时钟略快于本地 ⇒ 所有开仓被拒"这条耦合没有显式设计说明，建议登记。
- `data_quality` 新增 `missing_fields`/`fresh_until_ms`、`sources[*]` 新增 `available/source_at_ms/observed_at_ms/fresh_until_ms/missing_fields/values`：纯增量，未见仓内破坏。

### `tests/fixtures/evidence/u03_orchestrator_integration.patch`

- 接线方向正确：按 `(symbol, interval)` 存快照（patch:102-107）、逐 intent 独立建包、按 `entry.accepted` 过滤（patch:47）而非用 `report.ok`，退出保持不进本门禁。`intent_id` 的生成与模块合成规则一致（都是 `open[position]`）。
- 但存在上文 P1-1（`identity=None` 且不阻断）、P1-3（不留绑定/审计）、P2-2（丢 `client_order_id`）、P2-3（位置序号）、P2-5（`pipeline_authorized=False` 导致整体拒开 + 误报 critical）五处，需同批修。
- `blocked_symbols`/`kept` 里用 `open_indexes.index(index)` 反查位置，逻辑正确但 O(n²)；`decision.pipeline_authorized` 与 `verdict.adjusted_intents` 未同步处理，是最主要的语义风险点。

### `tests/fixtures/evidence/u03_offline_regression_registration.patch`

- 内容正确（把两个 U03 冒烟追加进 `SCRIPTS`，含理由注释），与 `_validate_smoke_manifest` 的要求一致，应用后即可消除 P0-1。问题是它只是提案（见 P0-1 的边界说明）。

---

## K02 必传 `rules` 的满足情况（正向确认）

模块对每个 intent 独立 `build_rules`（`:583-600`），并在 `:1130`、`:1133-1135` 把同一份 `rules` 同时传给 `validate_bundle` 与 `validate_intent`，`build_bundle`（`:1082-1093`）也用同一份。探针全程未出现 `RULES_MISSING/RULES_MISMATCH`（`_scratch_B/probe_b.txt` 多例），且 `required_claim_kinds` 由 side/breakout 派生、不由模型声明（`:1071-1073`）——模型无法用"少声明必需 kind"降低门槛。这一条（K02 README §10 T1 的下游义务）**已满足**。

确定性：同输入两次运行报告逐字段一致（作者 test 10；`_scratch_B/probe_b2.txt` R2-D 另确认 `binding_id` 随 `cycle_id` 变化，即绑定到轮次）。`binding_id` 不可被跨轮复用。

---

## 未覆盖 / 无法离线验证

1. `scripts/run_offline_regression.py` 的**整体执行**未跑（只跑了 `_validate_smoke_manifest()`）：清单失败是确定性的、先于任何子脚本执行，故无需跑完整套；其余子脚本在本 head 上是否受 S01/边界影响未逐个验证。
2. orchestrator 接线补丁**未应用**，只做静态审读；其运行期行为（含 `_research_evidence_gate` 与风险层/`filter_unauthorized_opens` 的交互顺序）未做实跑验证。
3. 仓外消费者（dashboard 进程、其它仓库、历史库表）是否依赖 `query_symbol_detail` 出参的 `funding_rate/mark_price/open_interest_usd` 数值类型、或 `data_quality["sources"]` 的旧形状——`grep` 只能覆盖本仓，外部不可验证。
4. S01 补丁对**真实交易所返回体**的行为（网络）：`smoke_p0c.py` 在 `EXCLUDED_SMOKE_SCRIPTS` 中，本次未联网、未跑；`live_check_query_detail.py` 同理。
5. U15 身份铸造未接线，"真实 RuntimeIdentity 到位后 P1-1 是否自然关闭"无法验证（当前代码路径下不会自动关闭——`identity=None` 仍是放行）。
6. `now_ms` 由调用方给出，模块不读墙钟；"证据 `as_of` 与被授权执行时刻"的**跨模块**约束（例如执行侧是否比对订单时间）不在本模块范围，未验证。

---

## 原始输出索引（`_scratch_B/`）

| 文件 | 内容 |
|---|---|
| `probe_b.py` / `probe_b.txt` | P1–P16：同周期共享 `evidence_id`、跨周期隔离、UNPINNED fail-open、畸形 claim 崩溃、id 缺/重/碰撞、`now_ms`、`bound_inputs`、时效、越界操作数、非 Mapping intent、嵌套覆盖、周期重标、退出绑定、报价不匹配、`required_sources` 自报、时序 |
| `probe_b2.py` / `probe_b2.txt` | R2-A..F：摘要 `900` 溯源、时效参数、退出空证据、`binding_id` 与轮次、落地绑定/接线静态核、S01 补丁值类型漂移与仓内消费者 |
| `probe_b3.py` / `probe_b3.txt` | R3-A..C：嵌套覆盖复现、放宽窗口后的 `INPUT_STALE` 边界 |
| `probe_b4.py` / `probe_b4.txt` | R4-A/B：`900` 为时间戳子串（自我纠错）、放宽全部窗口 + 自供报价 ⇒ 10 天前 bar 判 VALID |
| `probe_manifest.py` / `probe_manifest.txt` | P0-1 复现：`RuntimeError 未登记冒烟脚本: scripts/smoke_u03_market_evidence_quality.py, scripts/smoke_u03_research_evidence.py` |
| `r1_test_research.txt` | `test_research_evidence.py` 独立复跑 `PASS 79 / FAIL 0`，exit 0 |
| `r1_test_market.txt` | `test_market_evidence_quality.py` 独立复跑 `PASS 36 / FAIL 0`，exit 0 |

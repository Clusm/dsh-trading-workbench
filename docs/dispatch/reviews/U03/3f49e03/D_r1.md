# U03 证据层初审（D 角色 / 影子研究：时点口径与前瞻偏差）

- 审查对象：`task/U03` head `3f49e03a6a1d8c90b7abea018e51f4994cca160b`，基线 `ffdab44`
- 工作区（独立 detached）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U03\D\r1`
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`（`PYTHONUTF8=1`），全程离线，仅读 tracked 文件；探针与原始 stdout 在 `_scratch_D/`（未修改任何 tracked 文件，`git status` 只有 `?? _scratch_D/`）
- 结论：**CHANGES_REQUESTED**

## 0. 结论摘要

作者的时点口径主链路（未闭合 bar 不入事实、未来/过期快照拒绝、`now_ms` 必传、TTL 边界保守、K02 前瞻包拦截）经独立反例验证 **成立**。但存在 2 个 **P1 fail-open**，都指向同一根因：**门禁的"必需项"集合由模型自报数据决定，而验证方没有把自报项与程序事实绑定**——

1. 模型自报 `claims` 完全覆盖程序生成的 claim，使"数值矛盾确定性拒绝"与"开仓必须给出入场价"在自报 claims 路径下全部失效；
2. `required_sources` 是**源级**粒度，`mark_funding` 只取到 `markPrice`、`funding_rate` 全缺时仍算 available，依赖资金费的意图仍被放行。

另有 6 项 P2、4 项 P3；其中 P2-1（`status` 可为 `VALID` 而 `accepted=False`）是报告字段自相矛盾，建议一并修。

## 1. 命令与原始输出索引（`_scratch_D/`）

| 文件 | 内容 |
|---|---|
| `p00_env.txt` | 解释器 3.13.12，模块导入 OK |
| `p01_out.txt` | 反例 1：模型自报 claims（P1-1） |
| `p02_out.txt` | 反例 2：前瞻 bar / 手工包 / interval 混用 |
| `p03_out.txt` | 反例 3：源新鲜度、缺失当 0、TTL 边界 |
| `p04_out.txt` | 反例 4：源级门禁、status 一致性、覆盖键范围、未固定身份 |
| `p05_out.txt` | 反例 5：funding 字段级放行、缺 trigger_price、自洽前瞻包 |
| `p06_out.txt` | 反例 6：`BundleVerdict.valid=True` 的前瞻包（K02 层观察） |
| `p07_patchcheck2.txt` | 三个补丁 `git apply --check` 全部 APPLIES_CLEAN |
| `p08_authortest1.txt` / `p09_authortest2.txt` | 作者两个测试脚本 exit 0（PASS 79 / PASS 36，FAIL 0） |
| `p10_out.txt` | 反例 7：真实 0 保留、patched 形态往返、missing_fields 命名 |
| `p11_interval_mix.txt` | 4h 包混入 15m bar 的事实摘要 |
| `p12_unclosed.txt` / `p13` / `p14_livebar2.txt` | 未闭合 bar 不入事实（哨兵值复现） |
| `p15_nowms.txt` | `now_ms` 必传（缺省 TypeError） |

作者的两个回归（`test_research_evidence.py`、`test_market_evidence_quality.py`）我独立重跑确认 exit 0 / FAIL 0，与编排者记录一致。

## 2. 已证伪 / 已确认成立项（供对照，不计问题）

- **未闭合 bar 不入事实（verified）**：p14 忠实按 `_pack_candles` 列式构造 4 根 1h K 线（第 4 根 `close_time_ms = as_of + 1h - 1 > as_of`，即进行中 bar，哨兵价 8888.88）→ `bars_from_packed_candles` 标 `finality=live`，摘要 `facts` 只含 `1h:closed_close=744.9@…`，**哨兵值不出现**，包 `finality` 仍为 `closed`，意图 VALID。即：进行中 bar 既不污染事实，也不会把整个包打成未闭合。
- **未来/无时间 bar 的误标被拦（verified）**：把一根 `close_time_ms = as_of + 10h` 的 bar 显式标 `finality=closed` 塞进快照 → `build_bundle` 仍按 `close_time > as_of` 强制改判 `live`；`snapshot_from_records` 派生 `event_time = close_time - span + 1 > received` → K02 `INPUT_TIME_ORDER`，整包 `EVIDENCE_INVALID`（p03/p12）。**我未能构造出经 `evaluate_open_intents` 采信的前瞻事实。**
- **interval 不互相顶替（verified）**：15m 快照服务 4h 意图 → `INTERVAL_MISMATCH` + `MISSING_EVIDENCE_BINDING`/`MISSING_REQUIRED_INTERVAL`/`BUNDLE_NOT_CLOSED`/`CLAIM_KIND_CONTRADICTION`/`MISSING_EXECUTION_QUOTE`，全部拒绝（p02 D）。`_locate_snapshot` 要求 `str(requested) == str(snapshot.interval)`。
- **`now_ms` 必传（verified）**：keyword-only 无缺省；省略 → `TypeError: missing 1 required keyword-only argument: 'now_ms'`；定位传参也被拒（p15）。
- **TTL 边界保守且与 K02 口径一致（verified）**：`fresh_at` 用严格 `<`，`now == fresh_until` → 不 fresh → `SOURCE_STALE`；K02 `_referenced_input_issues` 用 `now >= valid_until` → 同样在等号处判 stale；同时 `QUOTE_EXPIRED` 一并命中（p03 G）。
- **源时钟晚于本地观测（verified）**：`fresh_until = min(source_at, observed) + ttl`（不取 max），且 `_source_inputs` 固定 `received_time = observed`，使 `source_at > observed` 触发 K02 `INPUT_TIME_ORDER` 整包拒绝（p03 B）。
- **required source 无时点即拒（verified）**：`available=True` 但无 `source_at_ms` → `SOURCE_TIME_UNVERIFIED:open_interest`（p03 D）。
- **未知/拼错的 required source 即拒（verified）**：`["funding"]` → `SOURCE_UNAVAILABLE:funding`（p03 F）。
- **真实 0 保留为 `"0"`、缺失不写 0（verified，仅限 patched 形态）**：`ETHUSDT_1h_patched_genuine_zero` → `funding_rate` 在 `values` 中为 `'0'` 且不在 `missing_fields`（p10 A）；`_patched_sources_missing` → `available=False`、`values` 无该键（p10 B）。
- **三个共享适配补丁可干净应用（verified）**：`git apply --check` 三者均 APPLIES_CLEAN，作者未改共享文件（p07）。

## 3. 问题清单

### P1-1 模型自报 `claims` 完全覆盖程序生成 claim，绕开"数值矛盾确定性拒绝"与"入场价必填"
`src/agents/research_evidence.py:1095-1102`（`declared_claims` 优先）、`:715-729`（`generate_claims`）、`:1137-1145`（geometry 依赖 `trigger_price`）

复现（`_scratch_D/p01_out.txt`、`p05_out.txt`）：最新已闭合 bar `close = 744.13`。

| 用例 | 模型输入 | 结果 |
|---|---|---|
| A 对照 | `trigger_price=900`，不自报 claims | `CLAIM_FALSE`，`accepted=False`，摘要 claim 为 `1h:closed_close > 900` |
| B 反例 | 同上 + `claims=[{kind:"close_above_price", expr:"1h:closed_close > 0.0001"}]` | **`status=VALID accepted=True`，`reason_codes=[]`，无丢单** |
| C 反例 | 完全不给 `trigger_price`，只给 claims | **`VALID accepted=True`，`geometry.entry=None`** |
| B' 对照 | 不给 `trigger_price`，不给 claims | `MISSING_EVIDENCE_BINDING`/`MISSING_REQUIRED_CLAIM`/`MISSING_CLAIMS`，全部拒绝 |

即：`if not claims: claims = generate_claims(...)` 让模型可以替换掉程序生成的比较基准，claim 的 RHS 与意图自报的 `trigger_price` 脱钩；且 `trigger_price`（开仓入场价）缺失这一必需项，仅在"模型不自报 claims"时才会被 K02 的 `MISSING_CLAIMS`/`MISSING_EVIDENCE_BINDING` 兜住。

影响：模块 docstring 第 3 条"claim 只从 `CLAIM_KIND_REGISTRY` 的三种 kind 生成，比较对象是真实快照数值"与验收列"数值矛盾确定性拒绝"只在"模型不自报 claims"路径成立。风控门禁的自报项（`side`/`breakout`）本就来自模型，再加上 `claims` 与 `trigger_price`，实际形成了"模型自定基准自证"的闭环——门禁退化为止损（只保证模型自己写的谓词为真），而非对模型叙述的独立校验。可审计性同时受损：报告里的 claim 与 `geometry.entry` 可以互相矛盾而无人发现。

建议：优先**忽略** `intent.claims`（始终用 `generate_claims` 结果），或要求自报 claims 与程序生成结果逐字段一致（`expr`/`kind`/`expected` 全等），不一致即拒；OPEN 动作强制要求 `trigger_price` 可解析为有限十进数。

### P1-2 `required_sources` 为源级粒度：源"部分可用"时关键字段缺失不阻断
`src/agents/research_evidence.py:968-986`（`_source_gate` 只查 `available`/`source_at_ms`/`fresh_at`）、`:344-364`（`missing_fields` 只记录不门禁）；S01 补丁 `available = any(value is not None for value in values.values())`

复现（`_scratch_D/p05_out.txt` A、`p04_out.txt` A）：`mark_funding` 载荷含 `markPrice`、不含 `lastFundingRate`（模拟交易所只返回标记价）→

```
mark_funding available= True values={'mark_price': '744.2'} missing=('funding_rate',)
意图 required_sources=["mark_funding"] → status=VALID accepted=True reason_codes=[]
摘要 sources: ticker=…; mark_funding=mark_price=744.2@…; open_interest=MISSING
```

即 `missing_fields` 如实记录了 `mark_funding.funding_rate`，但 `_source_gate` 只看源级 `available`，因此**声明依赖资金费的意图在拿不到任何 funding 数据时仍然放行**。这是 S01 "缺失 funding/OI 不得当 0"在**门禁层**的缺口（values 层是诚实的，没有折算成 0）。

建议：`required_sources` 支持字段级名（`mark_funding.funding_rate`）并与 `SourceQuality.missing_fields` 求交；或当声明了某源时，把该源 `missing_fields` 中的**声明字段**（可在 intent 里给 `required_source_fields`）升级为拒绝。

### P2-1 `IntentEvidence.status` 可为 `"VALID"` 同时 `accepted=False`（同一结论自相矛盾）
`src/agents/research_evidence.py:1147-1151`（`reason_codes` 合并 U03 理由）、`:1184-1197`（`status=verdict.status` 仅来自 K02）

复现（`_scratch_D/p04_out.txt` B，直接使用**作者自己的夹具** `funding_missing_not_zero`）：

```
intent-eth-funding status=VALID accepted=False reasons=['SOURCE_UNAVAILABLE:mark_funding']
decision_gate: drop_intent_ids=['intent-eth-funding']
```

U03 层拒绝（`SOURCE_*`、`INTERVAL_*`、`MODEL_FACT_OVERRIDE`、`GEOMETRY_*`、`RR_CONTRADICTION`）不反映到 `status`。`decision_gate()` 的 `drop_intent_ids` 是对的（门禁行为正确），但任何按 K02 习惯读 `status == "VALID"` 的下游会把这条被丢弃的意图当成通过。建议 U03 层有拒绝理由时输出独立的 U03 状态（如 `REJECTED`），或把字段拆成 `k02_status` + `u03_status`。

### P2-2 源自报 `fresh_until_ms` 被无条件采信，缺 clamp
`src/agents/research_evidence.py:358-364`

```python
declared_until = decimal_or_none(payload.get("fresh_until_ms"))
if declared_until is not None:
    fresh_until = int(declared_until)          # 无上界
elif available and source_at is not None:
    fresh_until = min(int(source_at), int(observed_at_ms)) + int(ttl_ms)   # 有 clamp
```

复现（`_scratch_D/p03_out.txt` A）：载荷声明 `fresh_until_ms = now + 100h` → `fresh_at(NOW)=True`、`fresh_at(NOW+99h)=True`，`required_sources=["ticker"]` 永不命中 `SOURCE_STALE`，意图 `VALID`。

同函数在派生分支已明确写了"取 max 会把未来时间洗白成'仍在有效期'"的安全原则，declared 分支没有同等约束，属不一致。现状：S01 补丁产出的 `fresh_until_ms` 已按 `min(source_at, observed)+ttl` clamp，所以**生产路径当前不可利用**；但验证方把"新鲜度"的最终裁定权交给数据生产者，是 fail-open 的委托，且一旦有其它生产者（或未来引入的 `fresh_until_ms` 字段）就会直接生效。建议统一为 `fresh_until = min(declared_until, min(source_at, observed) + ttl)`。

### P2-3 `MODEL_FACT_OVERRIDE` 只扫 intent 顶层键，`evidence.*` 内的行情事实全部漏检
`src/agents/research_evidence.py:1032-1033`（`overrides = [key for key in MODEL_FACT_KEYS if key in item]`，`item` 是 `normalise_intent` 的顶层字典）、`:108-122`

复现（`_scratch_D/p04_out.txt` C/D）：

```python
"evidence": {"confirmation_interval": "1h", "trigger_price": 744.0,
             "last_price": "1", "close": "1",
             "facts": {"1h:closed_close": "1"}, "market_facts": {"last_price": "1"}}
→ status=VALID accepted=True reason_codes=[]   # 无 MODEL_FACT_OVERRIDE
```

docstring 第 4 条与验收列声称"intent 里出现任何行情事实字段（`last_price/close/high/facts/…`）一律 `MODEL_FACT_OVERRIDE`"，在嵌套形态下不成立。实际危害有界（U03 只从 `evidence` 取 `confirmation_interval`/`trigger_price`/`risk_reward_ratio`/`data_observed_at_ms`，被夹带的 `last_price` 不进事实、不进摘要），但属"声明了未实现的保证"，且使"模型自报事实被接受"这一反问成立。建议递归扫描或对 `raw["evidence"]` 应用同一键表。

### P2-4 快照内 `bar.interval` 不受快照 interval 约束，异周期 bar 可混入同一证据包
`src/agents/research_evidence.py:536`（`"interval": bar.get("interval", interval)`）；K02 `_closed_inputs_by_interval` 按 `bar.interval` 分组

复现（`_scratch_D/p11_interval_mix.txt`）：4h 快照夹一根 `interval="15m"`、`close=7777` 的 bar →

```
status=VALID accepted=True ok=True bundle_reasons=()
facts: 15m:bar_close_time_ms=…; 15m:closed_close=7777@…; 4h:bar_close_time_ms=…; 4h:closed_close=744.13@…
```

4h claim 本身仍取真实 4h bar（决策未被污染），但**事实摘要把异周期数值混进了同一证据包**，正是 C 角色禁止的"用 15m 结论冒充 4h 证据"的可审计性形态。可达路径为 `snapshot_from_records`（离线重放入口，bar 可自带 `interval`）；工具路径所有 bar 由传入 interval 标注，生产不可达。建议在 `snapshot_from_records` 过滤或断言 `bar.interval == interval`。

### P2-5 未固定身份仍可 `VALID`/`accepted`（fail-open 委托，U15 延后）
`src/agents/research_evidence.py:1008-1010`、`:1200-1209`；orchestrator 补丁硬编码 `identity=None  # TODO(U15)`

复现（`_scratch_D/p04_out.txt` E）：缺省 identity →

```
status=VALID accepted=True identity_pinned=False ok=True
decision_gate={'ok': True, 'identity_pinned': False, 'drop_intent_ids': [], 'reasons': {}}
```

报告字段是诚实的（`identity_pinned=False`，不冒充已验证身份），用户文档也标注了 U15 待接线；但门禁**不因未固定身份丢单**，且 orchestrator 补丁每轮都传 `identity=None`，因此生产运行的每个 OPEN 都是在未固定身份下放行的。唯一信号是 `identity_pinned` 布尔与 risk event 的 `raw` 字段；只看 `ok`/`accepted` 的消费方看不到差别。属已披露的延后项而非隐瞒，建议在 U15 接线前于调用侧明确把 unpinned 视为降级（例如仅允许 paper / 需人工确认），并在 `decision_gate()` 的语义上说明该字段必须被检查。

### P2-6 旧口径出参回带的 0 无法与真实 0 区分（"缺失当 0"只在适配器显式声明时成立）
`src/agents/research_evidence.py:463-467`（顶层字段回填）、`:329-331`（`available = bool(payload)`）

复现（`_scratch_D/p10_out.txt` D）：`{"funding_rate": 0.0, "mark_price": 0.0, "open_interest_usd": 0.0}` 且无 `data_quality.sources` →

```
mark_funding  available=True values={'mark_price': '0', 'funding_rate': '0'} missing=()
open_interest available=True values={'open_interest_usd': '0'} missing=()
```

作者测试 B5 断言"适配层不信任回带的 0"，但该保证实际由适配器的 `available: False` 承担；U03 侧没有任何独立手段识别"工具把缺失填成 0"。当前 S01 补丁已把 `funding_rate`/`mark_price`/`oi_usd` 改为 `None`，所以生产路径安全；但这是单点依赖，无纵深。建议要求源必须显式声明 `available`（缺声明时按"未验证"→ 仅在 `required_sources` 时拒绝，其余降级并标注），或把"无 `source_at_ms` 却 `available`"从仅 required 路径扩展到全部源。

### P3-1 `missing_fields` 双命名口径并存
`src/agents/research_evidence.py:482-485`（把 `data_quality.missing_fields` 原样过户）

复现（`p10_out.txt` B）：`snapshot.missing_fields` 同时含 U03 规范名与适配器交易所原名：

```
('mark_funding.mark_price', 'mark_funding.funding_rate', 'open_interest.open_interest_usd',
 'mark_funding.lastFundingRate', 'mark_funding.markPrice', 'open_interest.openInterest')
```

同一缺失记两次、名字不同，下游按名匹配需容忍别名。建议统一为规范名（或明确声明 `data_quality.missing_fields` 为交易所原名域）。

### P3-2 `interval` 与 `evidence.confirmation_interval` 冲突静默取 `interval`
`src/agents/research_evidence.py:809-810`

复现（`p02_out.txt` E）：`interval="1h"` + `evidence.confirmation_interval="4h"` → 静默按 1h 绑定，`VALID`，无任何提示。建议至少记录一条不一致标记。

### P3-3 调用方注入 `quotes` 不与快照交叉校验
`src/agents/research_evidence.py:996`、`:1129-1132`：`quotes` 内的报价只过 symbol、有限性、`expires_at_ms` 检查，不与快照 ticker 的价格/时点交叉核对。生产 orchestrator 未传 `quotes`，风险低，但 U00 若启用该入参会引入未校验的报价来源。

### P3-4 K02 层观察（非作者文件，记录备查）
`src/core/contracts/k02_evidence.py:596-604`

构造 `received_time_ms` 远晚于 `as_of_ms`、`bar_close_time_ms = as_of + 10h` 且 `finality="closed"` 的自洽包 → `BundleVerdict.valid=True`，并派生出 `1h:closed_close=9999`（`p06_out.txt`）。拦截发生在 `validate_intent` 的 `FUTURE_BAR`（因 fact 必然引用该 bar）。U03 走 `validate_intent`，**无实际利用路径**；但任何只调 `validate_bundle`（README 称其为"K02 完整性入口"）的下游会把前瞻包判为完整性 OK。属冻结契约层残余，作者不可改，仅记录。

## 4. 未覆盖项与理由

- **未做真实交易所/联网验证**：`_pack_candles` 的 `t0_epoch_ms`/`t_sec` 是否可能被上游填错（例如 `n` 大于实际收盘根数）无法离线判定；我只验证了"给定列式载荷 → finality/事实"这一段。
- **未验证 orchestrator 运行时行为**：只静态读了 `u03_orchestrator_integration.patch` 与其干净应用性；未执行 orchestrator（需 LLM/网络）。
- **未跑 `smoke_contracts_v3.py` 与两个 `smoke_u03_*.py`**：编排者已预先核验，我按纪律未重跑；作者两个 integration 测试我重跑确认 exit 0。
- **SOUL/时钟回拨**：`now_ms` 由调用方传入，无法离线验证其是否来自真实时钟；"时钟回拨"只验证了 `now < as_of`（`SNAPSHOT_FROM_FUTURE`）与 TTL 等号边界（P2/verified），未验证跨进程 NTP 跳变。
- **`UNPINNED_IDENTITY` 的 `identity_hash` 稳定性**：未验证与真实 identity 命名空间是否可能碰撞。

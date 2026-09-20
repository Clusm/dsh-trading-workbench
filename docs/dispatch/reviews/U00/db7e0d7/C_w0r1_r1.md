# U00 契约候选审查报告 — C / 决策证据（w0r1）

- 审查提交：`db7e0d7`（`feat(contracts): freeze K01-K06 schemas, goldens and contract smoke (U00)`），detached，与任务指定一致。
- 审查对象：`src/core/contracts/k02_evidence.py`、`k05_trace.py`、`k01_identity.py`（`model_route_hash` 部分）、`common.py`、`__init__.py`、`docs/contracts/README.md`、`docs/contracts/golden/k01|k02|k05`、`scripts/smoke_contracts_v3.py`。
- 权威需求：规格 §4 K01/K02/K05、§10.2、§12；专项报告 §3（模型路由）、§5（数据语义）、§9（DL01/DL02/DL04/DL07）、§10（DC06/07/08/11/12）；`docs/contracts/README.md` §2–§7、§12。
- 边界声明：未读运行目录 `multi-agent-trader\`、`data/`、`.env`；未联网；未启动服务；未修改任何 tracked 文件（`git status --porcelain` 仅新增 `?? _review_scratch/` 与本报告 `?? U00_review_C_w0r1.md`）。
- 运行环境：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（Python 3.13.12，Windows 11），`PYTHONUTF8=1`。
- smoke 汇总（2026-09-20 本次运行）：`结果：PASS 69 / FAIL 0；golden cases=56 (positive=26, negative=30)`；6 个 schema 与模型同步、6 个 golden 均含正反用例。smoke 全部通过只能证明"golden 所写期望 = 实现输出"，不能证明 golden 覆盖了接口的真实边界（见 §3）。

## 0. 结论

契约骨架方向正确：K05 三类计数器分离、告警状态阶梯的正向语义清楚；K01 的敏感键名守卫与覆盖清单结构可用；K02 的 finality/未来 bar/NaN/有效期判定在常规路径上失败关闭。但以当前冻结形态直接支撑 U03/U05 会有系统性偏差：

1. **K02 "逐意图完整检查"在实现层不可达**：`validate_intent` 只校验意图自己声明的 claim，不校验 claim 集合是否覆盖必要检查；同一完整 bundle 上"缺证据"的意图可以用恒真 claim 或未知 action 字符串通过。golden K02-P2 的 B 失败实际来自"空 bundle + evidence_id 不符"，不是"缺证据必须失败"这一语义。
2. **K02 完整性（content_hash/事实溯源）没有校验入口**：篡改输入值或伪造 `DerivedFact.value` 后 verdict 可由 `EVIDENCE_INVALID` 翻转为 `VALID`，且包内不存在任何重算/比对 `content_hash` 的函数。
3. **K05 恢复谓词可被"无时间戳证据"绕过**：已 ACK 的 Incident 用裸 `true` 即可 RESOLVED，违反 §10.3"恢复谓词依赖新鲜证据"。
4. **K01 覆盖清单不强制、非 Mapping 输入可静默哈希 `.env` 原文**：`build_identity` 接受空的 `model_route`/`effective_config`；`hash_strategy` 对字符串/字符串列表不做敏感键名守卫。

计数：**P0 × 2、P1 × 5、P2 × 6；给 U03/U05/U15 的接口缺口 18 项**。建议：在打 `contract-v3.0` 标签前，以"小版本追加"方式修订 K02/K05 的判定函数语义（不改既有字段类型/优先级），或明确把这 7 条列为 U03/U05 必须自行补齐并在验收中签收的边界。

## 1. 证据与复现

- 反例脚本：`_review_scratch\ce_k02_k05_k01.py`（只读契约包、内存构造，不写运行目录）。

```powershell
$env:PYTHONUTF8=1
& "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe" "_review_scratch\ce_k02_k05_k01.py"
```

- smoke 复现：

```powershell
$env:PYTHONUTF8=1
& "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe" scripts\smoke_contracts_v3.py
```

以下引用行号均为 `db7e0d7` 版本。

## 2. 审查要点逐项结论

### 2.1 K02 `build_bundle` / `validate_intent`

**(a) 签名与规格一致。** 规格 §4 K02 要求 `build_bundle(snapshot, closed_bars, rules) -> EvidenceBundle`、`validate_intent(intent, bundle, execution_quote) -> EvidenceVerdict`；实现分别见 `k02_evidence.py:280`、`:448`，位置参数一致；`build_bundle` 增加 keyword-only `cycle_id/identity`、`validate_intent` 增加 keyword-only `now_ms` 是向后兼容扩展（README §4 已声明）。注意 `now_ms` 缺省为 `bundle.as_of_ms`（`:459`），真实校验时刻晚于 `as_of_ms` 时过期/STALE 判定会失效，必须由调用方显式传参——README 未把这一点写成强制。

**(b) 逐意图隔离：只做到"每次调用独立"，没做到"缺证据必须失败"。**
正向：`EvidenceVerdict` 绑定 `checked_intent_id/intent_symbol/evidence_id`（`:145-159`）；声明 `evidence_id` 与 bundle 不符给 `EVIDENCE_BUNDLE_MISMATCH`（`:470-472`）。golden K02-P2 确认 A 的 VALID 不泄漏给 B。
反例（CE1）：对与 K02-P1 完全相同的完整 bundle，意图只写一条恒真 claim `1 < 2`，其他必要检查（entry/SL/TP/RR、事件方向、必要数据源）全缺，`status=VALID`。golden P2 的 B 之所以失败，是因为它的 bundle 为空且声明了 `eb-A`，并非实现对"同一 bundle 上缺证据的意图"做了判定。`MISSING_CLAIMS` 只覆盖 `claims==[]` 且 `action=="OPEN"` 的情形（`:564-565`）。
另一个绕过（CE1b）：`action` 取任意非 `"OPEN"` 字符串（如拼写错误 `"OPENX"`、大小写变体经 `.upper()` 后不等）即跳过 OPEN 的 claim/报价门禁（`:461`、`:564`、`:568`），无报价、无 claim 也 `VALID`。

**(c) claim 数值由代码计算，但"事实-输入"链条无反向校验，存在自证路径。**
正向：三种操作数（字面量、`<input_id>.<key>`、`<interval>:<fact_kind>`/唯一 kind）都经 `Decimal` 解析（`resolve_operand`:183-214），NaN/Infinity 在 `_value_to_decimal` 被拒（`:162-171`）；claim 运算 `_compare` 全十进制（`:217-230`）。
反例（CE2）：`model_copy` 把 `in-01.close` 从 `742.8` 改为 `744.13` 后，同一 claim 从 `EVIDENCE_INVALID` 变 `VALID`，而 `content_hash` 仍是旧值（`hash_stale_after_tamper=true`）——`validate_intent` 从不重算/比对 `content_hash`（全包无 `verify_*`，grep 确认；`:108` 定义、`:435` 生成、`common.py:174` 提供 `model_content_hash` 但无消费方）。
反例（CE12）：用 `model_copy` 把 `DerivedFact("1h:closed_close").value` 从 `744.13` 改为 `999`（输入仍为 `742.8/744.13`），claim `1h:closed_close > 1h:prior_high` 变 `VALID`。`resolve_operand` 直接采信 `fact.value`（`:196-201`），`_referenced_input_issues`（`:251-277`）只检查 fact 引用的输入是否存在/已收盘/未过期，从不校验 `fact.value` 是否由这些输入按 `kind` 计算得出。

**(d) finality/过期/未来/NaN 的判定边界。**
正向：未来 bar 强制降级 `live`（`:317-319`）、未闭合 bar 引用给 `UNCLOSED_BAR`、`bar_close_time_ms > as_of` 给 `FUTURE_BAR`、`valid_until_ms <= now` 给 `INPUT_STALE`、NaN/Infinity 给 `NON_FINITE_VALUE`（CE6 与 golden K02-N2/N3/N4/N6 一致）。
可绕过点：
- **无时间 bar 被当成已收盘**（CE4）：`close_time_ms` 缺省 0（`:312`），`0 <= as_of` → `finality="closed"`（`:317`），`bar_close_time_ms=None`（`:348`）时连 `FUTURE_BAR` 检查都跳过；`rules` 不含 `max_bar_age_ms` 时该 bar 不被标记 stale，bundle `finality=closed`、`bar_times={"1h":0}`、缺周期为空，claim 引用由它派生的事实得到 `VALID`。只有默认 `max_bar_age_ms=7200000` 才会因 `as_of-0` 超龄而变 `STALE`——即失败关闭依赖规则里恰好有 max_age。
- **输入级币种污染**（CE3）：`build_bundle` 允许每根 bar 自带 `symbol`（`:345`），引用检查不比较 `item.symbol` 与 `bundle.symbol`，claim 可引用 bundle 内另一币种输入并 `VALID`。DC07"无借用另一个 symbol 检查结果"在输入级无对应判定；`validate_intent:473` 只比较意图 symbol 与包 symbol。
- **报价校验留口**（CE5）：`quote.symbol==""` 时跳过币种比对（`:572-574`）；OPEN 意图报价 `expires_at_ms=None` 时无过期判定。
- `evidence_id` 种子只含 `cycle_id/symbol/as_of_ms/input_id 列表`（`:405-411`），同一组输入 id 不同取值会得到同一 `evidence_id`（内容绑定只靠 `content_hash`，而它恰好无人校验——见 P0-2）。

### 2.2 K05

**(a) 计数分离正确，但不设防、不去重。** `DecisionCounters` 三类字段独立（`:107-115`），`purpose_kind` 未登记 purpose 抛 `UnclassifiedPurposeError`（`:63-69`），`validation` 明确不计数，golden K05-P4 与 N3 覆盖。
缺口（CE8）：`tally`（`:133-140`）对 span 不做任何合法性校验、也不按 `cycle_id` 去重——同一周期两枚 `purpose="cycle"` span 计出 `cycles_total=2`，与 DL07 验收"同一周期不重复计数"直接冲突；`span_violations`（`:98-104`）甚至未从 `__init__.py` 导出，没有任何强制调用点。

**(b) `deadline_remaining` 语义只有"负值提示"。** 字段注释声明由调用方在单调时钟上计算（`:72-86`），`span_violations` 只查 `deadline_remaining_ms < 0`（`:102-103`）。跨字段一致性（与 `started/ended`、与周期绝对 deadline 的单调性）无谓词可写，因为契约里没有绝对 deadline 字段：CE8 中 `started=5000, ended=5001, deadline_remaining_ms=1e9` 无任何违规码。U05 无法仅靠现有字段证明"每次外部调用都服从同一绝对剩余时限"（规格 §4 K05、报告 §3.1）。

**(c) Incident ACK≠RESOLVED 方向正确，但恢复谓词可被"无时间戳"绕过。** `acknowledge` 只写 `ack_at_ms`（`:214-215`）；`resolve_incident` 必须先过 `check_recovery`（`:218-232`），缺失/陈旧/来自未来/为假分别给 reason code。
绕过（CE7）：`_evidence_entry`（`:181-186`）在证据不是 Mapping，或 Mapping 里没有 `observed_at_ms` 时返回 `observed=None`，`:202-207` 随即跳过 `max_age_ms` 新鲜度检查。实测 `{"fresh_account_snapshot": true}`、`{"...": {"value": true}}`、`{"...": {"observed_at_ms": null, "value": true}}` 三种形态均 `recovered=true` 且 `resolve_incident` 成功置 `RESOLVED`。这正是 §10.3/report §5.2 禁止的"用缓存里的绿色状态恢复"。谓词表达力还缺：证据类型→来源绑定、ACK 之后必须重新观察、`resolution_evidence_hash` 不保留逐项引用。

**(d) AlertDelivery 阶梯正向正确，但 DELIVERED 可在缺少供应商回执时进入。** `evaluate_state`（`:251-267`）与 `delivery_violations`（`:270-297`）实现"HTTP200 只到 SUBMITTED、无设备回执不得 DELIVERED"，golden P2/P3/N2 覆盖。
缺口（CE9）：`transition_state`（`:300-322`）升 `DELIVERED` 时只要求 `delivered_at_ms` 已存在，不要求 `provider_receipt`；实测先构造 `submitted_at_ms+delivered_at_ms`、再 `transition_state(..., "DELIVERED")` 成功，违规只在事后 `delivery_violations` 里出现 `PROVIDER_RECEIPT_MISSING`。另外 `AlertDelivery` 没有通道能力标志、去重键、退避/冷却/next_retry 字段（§11.2/U14 需要的投递缓存语义）。

### 2.3 K01 `model_route_hash` 与 `effective_config_hash`

**(a) 哈希覆盖由调用方字典决定，漂移敏感但无结构强制。** `hash_model_route`（`:90-93`）对传入 Mapping 全量规范化哈希，golden K01-P2 证明"模型名/effort/备用路由/profile 覆盖"任一变化都会变哈希。但 `RuntimeIdentity` 只存哈希、不存脱敏后的 `ModelRoute`，`IDENTITY_COVERAGE`（`:34-39`）声明的四组字段没有任何运行时强制：`build_identity`（`:96-121`）不调用 `validate_coverage`，CE10 用 `model_route={}`、`effective_config={}` 正常构造身份，`model_route_hash == hash({}) == 44136fa3…`，而 `validate_coverage` 会报 7 项缺口。DL01 要求"解析路由后生成脱敏 ModelRoute；声明参数来源、capability、mock 标志"没有对应结构。

**(b) 敏感键名守卫强，但只作用于"Mapping 的键"。** `ensure_no_sensitive_fields`/`sensitive_paths`（`common.py:68-89`）递归键名匹配；`hash_model_route({"api_key": ...})` 正确拒绝（CE11 对照）。
误用路径（CE11）：
- `hash_strategy("OPENAI_API_KEY=sk-…\nBINANCE_API_SECRET=…")` **返回哈希，不报错**——`hash_strategy` 仅在 `isinstance(strategy_cards, Mapping)` 时才做守卫（`k01_identity.py:83-87`），非 Mapping 直接进 `canonical_hash`。行列表同理。
- `hash_effective_config(["A=1", "BINANCE_API_SECRET=…"])` 会走 `dict(route)` 抛 `ValueError`（形式非法但未按敏感输入处理；好在未静默哈希）。
- 值为秘密（键名无害）的情况不做扫描，如 `hash_strategy({"note": "API_KEY=sk-…"})` 会通过——键名守卫本身的设计边界，但 README §2 未向 U15 声明"必须经结构化解包，禁止整文本回退"。
另：模块顶部 `DEPENDENCY_SOURCE_FILES`/`STRATEGY_SOURCE_FILES`（`:20-21`）声明后未被任何函数使用，覆盖清单与实际传入文件之间没有绑定校验（README §9 表格已承认命名差异，但常量本身是死代码）。

## 3. 反例增量（golden 未覆盖，全部已运行复现）

| 编号 | 场景 | 关键实测输出 | 对应问题 |
|---|---|---|---|
| CE1 | 完整 bundle + 意图只有恒真 claim `1 < 2` | `status=VALID`，`reasons=[]` | P0-1 |
| CE1b | `action="OPENX"`、无 claim、无报价 | `status=VALID` | P0-1 |
| CE2 | `model_copy` 改 `in-01.close` 742.8→744.13，保留旧 `content_hash` | 原 `EVIDENCE_INVALID` → `VALID`；`hash_stale_after_tamper=true` | P0-2 |
| CE12 | 伪造 `DerivedFact(closed_close)=999`（输入仍 742.8/744.13） | `status=VALID` | P0-2 |
| CE7 | 恢复证据无 `observed_at_ms`（裸 bool / `{value:true}` / `{observed_at_ms:null}`） | 三种均 `recovered=true`、`RESOLVED` | P1-1 |
| CE8 | 两枚同 cycle `purpose=cycle` span；负 duration；巨值 deadline_remaining | `cycles_total=2`；`duration_ms=-1000`；`violations=[]`（deadline 例） | P1-2 |
| CE10 | `build_identity(model_route={}, effective_config={})` | 构造成功；`model_route_hash=hash({})`；`validate_coverage` 报 7 缺口 | P1-3 |
| CE11 | `hash_strategy(<.env 原文/行列表>)` | 返回哈希 `806e68d3…` / `0444505d…` | P1-4 |
| CE3 | BNBUSDT bundle 内含 BTCUSDT 输入，claim 引用之 | `status=VALID` | P1-5 |
| CE4 | bar 无 `close_time_ms` 且 rules 无 `max_bar_age_ms` | `finality=closed`、`bar_times={"1h":0}`、`VALID` | P1-5 |
| CE5 | 报价 `symbol=""` | `status=VALID` | P2-2 |
| CE9 | 无 `provider_receipt` 时 `transition_state(...,"DELIVERED")` | `state=DELIVERED`，仅事后 `PROVIDER_RECEIPT_MISSING` | P2-4 |
| CE6（对照） | 引用 NaN bar | `EVIDENCE_INVALID` / `NON_FINITE_VALUE`（与 golden N3 一致，未发现绕过） | — |

原始输出保存在本报告生成时的运行记录中，重跑命令见 §1。

## 4. P0/P1/P2 清单

### P0（阻断 DL02 正确性/完整性）

**P0-1 K02 逐意图"完整检查"不可达，未知 action 绕过 OPEN 门禁**
- 位置：`src/core/contracts/k02_evidence.py:496-519`（claims 仅按声明运算）、`:564-565`（MISSING_CLAIMS 仅空 claims）、`:461`（action 仅字符串大写）、`:567-580`（报价门禁仅 OPEN）。
- 复现：CE1 / CE1b（§3）。
- 影响：规格 §4 K02"对每个 intent 分别核验全部必要检查"、报告 §9 DL02"每条 open 绑定 symbol/interval/snapshot、程序计算 entry/SL/TP 方向和 RR"在契约层无对应判定；U03 若按现状接线，每个意图的检查完整性完全由调用方自觉决定，DC06/DC07 的"缺证据拒绝新仓"无契约保证。
- 建议：`rules` 增加按 `action/side/order_type` 的 `required_claim_kinds`（或引入 intent 结构化字段），缺失即 `EVIDENCE_INVALID`（新 reason code，小版本允许）；未知 `action` 值显式拒绝而非按非 OPEN 处理；golden 增"同 bundle 两意图，缺必需 claim 的那个必须 INVALID"。

**P0-2 K02 `content_hash` 无校验入口、派生事实与输入无溯源反向校验**
- 位置：`k02_evidence.py:108`（字段）、`:405-435`（生成）、`:196-201`（直接采信 fact.value）、`:251-277`（只查输入存在/时间/finality）；`common.py:174-183`（`model_content_hash` 无消费方）。
- 复现：CE2 / CE12（§3）。
- 影响：DC07"签名后改 qty/SL"式的证据篡改在契约层不可检测；持久化回读/跨进程传递后无法用契约 API 验证证据未被改。U03 被迫自造验证逻辑，易出现各自口径的"等效哈希"。
- 建议：新增 `verify_bundle(bundle) -> list[reason]`（重算并比对 `content_hash`）；`resolve_operand`/新增 `verify_fact` 对 `fact.value` 做输入一致性校验（按 kind 的确定性重算）；golden 补"篡改输入/伪造事实必须被拒"。

### P1（语义坑，会在 U03/U05 集成处放大）

**P1-1 K05 恢复谓词可被无时间戳证据绕过**（CE7）
- 位置：`k05_trace.py:181-186`、`:202-207`；`resolve_incident:218-232`。
- 影响：规格 §10.3、§11.2；ACK 后的缓存绿色状态可冒充新鲜恢复证据。
- 建议：缺 `observed_at_ms` 视为不可恢复（reason `EVIDENCE_TIMESTAMP_MISSING`）；golden 增该负例。

**P1-2 K05 span 完整性不设防、每周期不去重**（CE8）
- 位置：`k05_trace.py:133-140`（tally）、`:88-91`（负 duration 允许）、`:98-104`（violations 无人调用）、无绝对 deadline 字段。
- 影响：DL07 验收"同一周期不重复计数"、报告 §3.1"服从同一绝对剩余时限"均无实现载体。
- 建议：tally/持久化入口强制 `span_violations` 并拒绝/隔离违规 span；按 `cycle_id` 去重计数；补 `cycle_deadline_ms` 支持一致性校验；补 golden 负 duration、重复 cycle 用例。

**P1-3 K01 覆盖清单不强制、无 ModelRoute 类型**（CE10）
- 位置：`k01_identity.py:96-121`（build_identity 不调 validate_coverage）、`:148-159`、`:34-39`。
- 影响：DL01 的"参数来源/capability/mock 标志"无处承载；身份可静默缺少路由覆盖。
- 建议：`ModelRoute` 模型 + `build_identity` 强制覆盖校验（返回缺口或抛错）。

**P1-4 K01 非 Mapping 输入可静默哈希 `.env` 原文**（CE11）
- 位置：`k01_identity.py:83-87`（守卫仅 Mapping）、`common.py:68-82`（只扫键名路径）。
- 影响：规格 §4 K01"禁止对原始 .env 整文件导出哈希"存在执行面缺口。
- 建议：所有 `hash_*` 强制 Mapping 输入（或对 str/序列走显式拒绝），并把该约束写进 README §2 与测试。

**P1-5 K02 输入级币种污染与无时间 bar 判定**（CE3 / CE4）
- 位置：`k02_evidence.py:345`（bar.symbol 覆盖）、`:251-277`（不查输入 symbol）、`:311-319`（close_time 缺省 0 → closed）、`:348`。
- 影响：DC07"不借用另一个 symbol 检查结果"在输入级失效；未收盘/无时间数据可被当作已收盘事实进入 claim。
- 建议：build_bundle 拒绝/隔离 `bar.symbol != bundle.symbol`；引用检查比较输入 symbol；`close_time_ms` 缺失判 `unknown` 并必须缺省拒绝；补 golden。

### P2（文档/一致性/轻度误用）

**P2-1** `evidence_id` 种子不含内容哈希（`k02_evidence.py:405-411`），同 id 不同值生成同一 evidence_id，内容绑定实际落在无人校验的 `content_hash` 上。
**P2-2** 报价 `symbol` 为空字符串时跳过币种比对（`k02_evidence.py:572-574`）；CE5。
**P2-3** `validate_intent` 的 `now_ms` 默认 `as_of_ms`（`:459`、`:493`、`:515`），调用方忘记传真实校验时刻会漏掉 EXPIRED/STALE；README 未标注"传入 now_ms 是硬性要求"。
**P2-4** `transition_state` 允许无 `provider_receipt` 升 `DELIVERED`（`k05_trace.py:311-321`），仅由 `delivery_violations` 事后标记；CE9。
**P2-5** K05 缺运维字段：`Incident` 无结构化 `reason_code`（`stable_incident_key:170-171` 需外部再传）、`AlertDelivery` 无通道能力/去重/退避/冷却；`tool_call` 直接计入 `api_attempts`（`:54-59`）需 U05 明确是否混淆 DL07 工具与 API 口径。
**P2-6** `DEPENDENCY_SOURCE_FILES`/`STRATEGY_SOURCE_FILES` 为死常量（`k01_identity.py:20-21`），未参与任何哈希或校验。

## 5. 给 U03/U05/U15 的接口缺口清单（仅列，不实现）

**DL02 / U03（逐意图事实验证）**
1. 结构化 intent/proposal 契约：现无模型承载 `side / order_type / entry / SL / TP / RR / lifecycle_id / expires_at`；`validate_intent` 只认自由键 dict，`side` 字段被完全忽略（grep 确认仅 k04 使用）。
2. 按 `action/side/order_type` 的必需 claim 策略（`required_claim_kinds`）与非空但无效 claim 的拒绝路径。
3. entry/SL/TP 方向与 RR 的程序计算/校验纯函数（含订单类型对应的候选入场价、RR 阈值）。
4. `verify_bundle`（content_hash）与 fact↔input 溯源校验函数。
5. "精确意图集合"签名/封条：`ValidationBatch{bundle_hash, intent_ids, verdict_ids, config_version, expires_at}`（报告 §9.1 明确"签名需绑定精确意图集合"；现只有单意图 verdict）。
6. 报告 §5.3 的 bundle 级字段：`schema_version / environment / market_venue / account_profile / missing_fields / fresh_until` 语义与类型化 `quality`（现 `quality: dict[str, Any]`）。
7. `evidence_id` 内容绑定（种子加入 `content_hash` 或值摘要）。

**DL04 / U03+U05（数据质量与有界刷新）**
8. 非 K 线输入（funding/OI/mark/basis/账户快照）的 `kind/finality/TTL/missing` 表示与"缺失≠0"的显式证据结构（现 `build_bundle` 只处理 bar）。
9. 每次采集的整体 deadline/尝试次数/降级原因字段（K05 span 无绝对 deadline，bundle 无采集 deadline）。
10. 工具出参 schema 裁剪 + "工具结果→证据输入"的适配契约（U00 未登记字段，U03 无法按统一 schema 落库）。
11. 外部文本的 trust/taint 标记通道（DC12"仅当文本数据"；现证据输入仅数值 bar 通道，无文本证据的合法承载与隔离位）。

**DL07 / U05（漏斗、时延、原因可观测）**
12. span→intent/order/evidence 关联字段（现仅 cycle/stage/request/purpose），cycle→request→intent→order 链路不可闭合。
13. funnel 状态枚举（无信号/跳过/异常/冲突/拒绝/成交）+ no_trade 一轮/一单的解释结构 + 分桶耗时 helper。
14. 每周期去重计数（`CycleAccounting`/`count_unique_cycles`）与"异常不报全成功"的断言入口。
15. span 校验管线入口（`span_violations` 接入 tally/持久化；现函数未导出、无调用点）。

**DL01 / U05+U15（模型身份与 Mock 门禁）**
16. `ModelRoute` 模型：provider/model/effort/fallback/来源（yaml/env/CLI/profile）/capability/mock 标志；`build_identity` 强制覆盖清单。
17. profile 合并"有效非敏感值"的构造契约（防 `.env` 原文/行列表误传）与输入类型守卫。
18. `AlertDelivery` 通道能力/dedup/backoff/cooldown 与 `Incident` 结构化 `reason_code`（U14 告警真实性所需，落在 K05 结构上）。

## 6. 同模型偏差声明

- 本审查由 opencode 子代理执行，模型为 `deepseek-v4.1-flash`（当前会话模型）。被审 U00 契约与"专项报告"可能出自同族模型（仓库文档显示主链路使用 WorkBuddy/DeepSeek V4.1），因此存在**共同盲区风险**：例如对"claim 集合充分性"的习惯性信任、对无类型 dict 输入约定、对 golden 自证循环的敏感度不足。
- 本报告结论尽量以可独立复算的证据支撑（`db7e0d7` 源码行号、smoke 输出、CE1–CE12 可执行反例的实测输出），而非文风或"另一 Agent 口头通过"；但同源模型偏差无法完全排除，建议 Q/E 用独立夹具对 P0-1/P0-2/P1-1 复验。
- 边界复核：本会话未进入/读取运行目录 `multi-agent-trader\`、`data/`、`.env`；未联网；未启动服务；未修改任何 tracked 文件（`git status` 仅新增 `_review_scratch/` 与本报告）。

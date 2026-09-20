# U00 契约候选复审报告 — C / 决策证据（w0r1 r3）

- 复审对象：`b36d8a9`（`fix(contracts): close r2 P0/P1 findings, update gap index and golden changelog (U00 r3; I recovery commit after author session cancellation; smoke PASS 160/0 verified)`）；r2 候选 `0b85bef`；detached HEAD，与指令一致。
- 阅读范围：`git diff 0b85bef..b36d8a9 -- src/core/contracts docs/contracts scripts/smoke_contracts_v3.py` 全量（19 文件）。
  K02/K01/K05 做逐条复跑 + 新对抗；K03/K04/K06 仅 diff 级阅读（不在 C 域，未做对抗性验证，避免与 B/Q/E 域重复）。
- 反例脚本（临时，未进 tracked）：`_review_scratch\ce_r3_adversarial.py`、`ce_r3_probe2.py`；原始输出 `_review_scratch\ce_r3_adversarial_output.txt`、`ce_r3_probe2_output.txt`、`ce_r3_k05_boundary_output.txt`、`ce_r3_smoke_output.txt`。
- 边界声明：未触碰运行目录 `multi-agent-trader\`、`data/`、`.env`；未联网；未启动服务；未修改任何 tracked 文件（`git status --porcelain` 仅 `?? _review_scratch/` 与 `?? U00_review_C_w0r1*.md`）。
- 环境：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，`PYTHONUTF8=1`。

## 0. 结论

**PASS-with-notes。** 无新增 P0/P1；新增 P2 × 3、P3 × 2（均登记于 §2），另有 2 项索引残留与 2 项 K05 观察（不阻塞）。

- **r1 两项 PARTIAL 本轮均 CLOSED**：P0-1（quality 字段参与判定 / kind 标签 / 非 OPEN 门禁）、P1-3（字段级覆盖 / ModelRoute）。
- **r2 新发现 P1 N1 CLOSED（以验证方显式传 `rules` 为前提）**：保留策略字段被改写后，无 `rules` 仍可洗白（残余 NR-2）。
- r2 P2 N2–N7 全部 CLOSED（N7 对本复现关闭，非 K 线结构残余已索引 C#8）；r1 P2-6 仍 PARTIAL。
- **smoke**：`PASS 160 / FAIL 0；golden cases=147 (positive=44, negative=103)`，schema 6/6 PASS。
- **README §12 缺口索引**：r2 指出的 8 项遗漏 **8/8 已补齐**（C#5/C#6/C#8/C#10/C#11/C#12/C#13/C#16，README:318-325）；抽样发现 1 处交叉引用错误 + 1 处 golden derivation 未同步 + 2 项 r2 “部分覆盖”仍缺索引。
- 逐条判定与证据见 §1–§4。

## 1. r2 发现逐条判定

| r2 编号 | r3 证据（本轮实测） | 判定 |
|---|---|---|
| P0-1a quality 字段参与判定 | A1：只抹 `stale_inputs`+重哈希（**不传 rules**）→ `QUALITY_MISMATCH`（较 r2 本质提升）；A6：10 个必需键逐项删除全部 `QUALITY_MISSING_FIELD`；A7：`bar_times`→`QUALITY_MISMATCH`、`finality`→`FINALITY_MISMATCH`。重算范围 `_expected_quality` k02_evidence.py:461-503，比对 k02_evidence.py:576-589。残余：`dedup_removed` 不在重算/比对（NR-1）。 | **CLOSED** |
| P0-1b kind 标签 | `CLAIM_KIND_REGISTRY`（k02_evidence.py:94-98）+ `claim_kind_consistent`（:349-369）；D 组 5/5：合法 kind→VALID；字面量 RHS/错误 op/输入值 LHS→`CLAIM_KIND_CONTRADICTION`；未注册→`CLAIM_KIND_UNKNOWN`；golden K02-N23/N30。 | **CLOSED** |
| P0-1c 非 OPEN 零 claim | 非 OPEN 缺 `management_authorization_id`→`NEEDS_AUTH`（k02_evidence.py:899-902；E no-auth；golden K02-P6/N20）；带授权引用且零 claim→VALID 为设计语义（README §4:103，授权范围由 K04 承担）。残余：引用只校验非空串，K02 无法解析 K04 授权（跨契约引用，属设计）。 | **CLOSED**（设计已文档化） |
| P0-2 content_hash/事实溯源 | r2 已判 CLOSED；本轮复核事实集合唯一与选取规则仍生效：F3 重复 `fact_id`→`DUPLICATE_FACT_ID`（k02_evidence.py:592-595），缺失期望事实→`DERIVED_FACT_MISMATCH`（:617-618）。无密钥自哈希残余属 L13/C#5，已索引。 | **CLOSED** |
| P1-1 K05 恢复谓词 | r2 已判 CLOSED；本轮边界再验（C1）：`now-observed == max_age` 恰好新鲜、再老 1ms→`EVIDENCE_STALE`；`observed=now` 通过、`now+1`→`EVIDENCE_FROM_FUTURE`；`observed==ack_at` 不判 PREDATES（严格 `<`，k05_trace.py:253-254）。 | **CLOSED** |
| P1-2 K05 span/去重 | r2 已判 CLOSED（本轮未重复对抗）。 | **CLOSED** |
| P1-3 K01 覆盖/ModelRoute | `ModelRoute` 落地（k01_identity.py:46-66，`model/effort` 非空，`fallback_routes/profile_overrides` 显式空合法）并导出（`__init__.py:29,152`）；`build_identity(enforce_coverage=True)` 字段级强制 `_coverage_gaps`（k01_identity.py:150-179、203-211；B1 空 `model` 的 ModelRoute→ValidationError，B6 `model=None`→拒绝）；golden K01-N5 字段级缺口、K01-P7。与 r2 相比“无强制入口”与“无 ModelRoute”均消解。残余：Mapping 路径空串/额外键绕过类型约束（NR-3）、§3 交叉引用错（NR-4）。 | **CLOSED** |
| P1-4 K01 原始文本载荷 | r2 已判 CLOSED（本轮未重复对抗）。 | **CLOSED** |
| P1-5 K02 跨币种/无时间 bar | r2 已判 CLOSED（本轮未重复对抗）。 | **CLOSED** |
| N1（P1）quality 洗白+重哈希 | A1 无 rules 即拦（`stale_inputs` 单独抹除已不可）；A2 `required_claim_kinds` 抹除、A4 `max_bar_age_ms` 改写、A5 `required_intervals` 抹除：**传 rules 后全部 `RULES_MISMATCH`**（k02_evidence.py:620-634），golden K02-N21/N22/P7。**残余**：`validate_intent(..., rules=None)`（默认值）时 A2/A4 仍得 `VALID`（probe2 P1/P2）→ 见 NR-2。 | **CLOSED**（条件：验证方传 `rules`；残余已登记 NR-2） |
| N2（P2）重复 fact_id | `REASON_DUPLICATE_FACT_ID` 落地；F3 `validate_bundle.valid=False`；golden K02-N24。重复非法后“claim 取首个匹配”的顺序依赖不再可达（`fact_by_id` 仍取首个，但仅在非法包中触发）。 | **CLOSED** |
| N3（P2）事实换指 | `_expected_facts` 按“最新/次新 bar”重建（k02_evidence.py:421-458），逐条比对 id/kind/inputs/value（:606-616）；golden K02-N25。 | **CLOSED** |
| N4（P2）bound_inputs/kind | claim 引用越界→`CLAIM_OUTSIDE_BOUND_INPUTS`（k02_evidence.py:951-954；F1、golden K02-N26）；kind 语义绑定见 P0-1b。残余：注册表固定“形态”不固定“阈值”（`close_above_price` 配 `0.01` 仍可通过），策略几何/RR 由 L2 登记为 U03 职责。 | **CLOSED** |
| N5（P2）OPEN 直接 RESOLVED | `resolve_incident` 要求 `status=="ACK"`（k05_trace.py:277-278）；C3/C4/C5 三种路径均 `ResolutionNotAllowedError(ACK_REQUIRED)`；golden K05-N11。 | **CLOSED** |
| N6（P2）畸形 claims 崩溃 | `_parse_claim` 非 Mapping 返回 None（k02_evidence.py:302-319）+ `CLAIM_MALFORMED`（:912-931）；F2 `claims="abc"`→`CLAIM_MALFORMED+MISSING_CLAIMS`、`[None,7]`→`CLAIM_MALFORMED`，无异常；golden K02-N27/N28。 | **CLOSED** |
| N7（P2）非 K 线 closed 输入 | 无 `bar_close_time_ms` 的 closed 输入→`UNDATED_BAR`（k02_evidence.py:388-394、556-557）+ `QUALITY_MISMATCH`（`undated_inputs` 重算）；golden K02-N29。**残余**：非 K 线结构仍不可表达——`interval=None`、`bar_close_time_ms` 有值、`finality="closed"` 的伪造账户快照仍 `valid=true` 且 claim `in-acc.equity > 0`→`VALID`（probe2 P3）；该项已作为 C#8 索引（README:320），属登记过的下游缺口。 | **CLOSED**（残余已索引 C#8） |
| P2-6（r1 残余）K01 常量 vs 文件清单 | `_coverage_gaps` 现校验锁文件名 `requirements-lock.txt` 出现（k01_identity.py:159-162），但 `STRATEGY_SOURCE_FILES` 仍只作标签、不与实际文件清单绑定；低影响、未索引。 | **PARTIAL**（无升级） |

## 2. 新发现（含 file:line + 复现）

### NR-1（P2）`dedup_removed` 不在重算/比对范围，可任意改写且规则比对也拦不住
- 位置：`QUALITY_REQUIRED_KEYS` 含 `dedup_removed`（k02_evidence.py:99-110），但 `_expected_quality`（:461-503）不产出该键、`validate_bundle` 的逐项比对循环（:576-585）不含该键，`rules` 比对（:620-634）也不涉及；写点在 :800。
- 复现（A3）：两条同内容 bar 建包，`quality.dedup_removed=["in-dup"]`；分别改写为 `[]` 与 `["in-01"]` 并重哈希 → `validate_bundle(rules=正确规则)` 两次均 `valid=True, reason_codes=[]`。
- 影响：审计字段可被随意抹除/嫁祸（谎报某个真实输入被去重丢弃），且 §12“质量字段**全量**重算”（README:284）表述过强；实际重算范围是“由 inputs 可推导”的字段。因该字段无判定消费者，定级 P2。
- 建议：`rules` 增加 `dedup_enabled` 或按 `(symbol,interval,close_time,values)` 反查重复（或明确声明“不可重算、仅审计提示”并修正 README 措辞）。

### NR-2（P2）`rules=None` 默认值下，策略字段仍可洗白（r2 N1 的配置面残余）
- 位置：`validate_bundle(rules=None)`/`validate_intent(rules=None)` 默认（k02_evidence.py:519-524、838-845）；策略读取点 :961-973（`required_claim_kinds` 来自 bundle.quality）。
- 复现（probe2 P1/P2）：① `max_bar_age_ms` 1000→7200000 + `stale_inputs=[]` + 重哈希，`validate_intent(..., rules=None)` → **`VALID`**；传 rules → `RULES_MISMATCH`。② `required_claim_kinds` 抹除 + 重哈希，`rules=None` → **`VALID`**；传 rules → `EVIDENCE_INVALID/RULES_MISMATCH`。另 A5 显示 `required_intervals` 抹除 + `finality="live"` 在 `validate_bundle(rules=None)` 下 `valid=True`（intent 层会被 `BUNDLE_NOT_CLOSED` 兜住）。
- 影响：r3 的封堵完全依赖“验证方显式传 rules”，而 README §4:92 用“**应**显式传”、docstring 用“应”，不是强制；与 L3（`now_ms` 缺省风险）同类却无索引条目。U03 一旦漏传 `rules`，即回到 r2 N1 的 STALE/MISSING_REQUIRED_CLAIM 洗白。
- 建议：把 `rules` 改为必填（或提供 `validate_intent_with_rules` 门面），至少补一条 L 条目并纳入 U03 验收（与 L3 同列）。

### NR-3（P2）`build_identity` 的强制覆盖与 `validate_coverage` 空值语义不一致（Mapping 路径可绕开 ModelRoute）
- 位置：`_coverage_gaps` 只判 `field not in payload or payload.get(field) is None`（k01_identity.py:173-178）；`validate_coverage` 把 `None` **与空字符串**都算缺口（:265-268）；`build_identity` 只在 `enforce_coverage` 时用前者（:203-211），Mapping 路由不经 `ModelRoute` 类型校验（:191、:221）。
- 复现（B2/B4）：`build_identity(model_route={"model":"","effort":"","fallback_routes":[],"profile_overrides":{}}, ...)` → **接受**（`model_route_hash=74e68a021ce9`），而同一 payload 经 `validate_coverage` → `["model_route.model","model_route.effort",...]`；`{"model":"m","effort":"e","fallback_routes":[],"profile_overrides":{},"junk_field":1}` 亦被接受且 `junk_field` 进入哈希。
- 影响：README §3:66-70 声称“字段级覆盖强制…缺失逐项列入 `IdentityCoverageError`”，但字符串空值与额外键在强制路径下不拦；U15 可用“全键位空值”构造出通过覆盖校验、却无有效模型名的身份。
- 建议：`_coverage_gaps` 与 `validate_coverage` 统一（`""` 计缺口）；Mapping 路径先 `ModelRoute.model_validate` 或拒绝额外键。

### NR-4（P3，文档）README §3 交叉引用错误
- README:73-74：“capability/mock 标志属 U15/DL01 扩展面（**§12 C8**）”，但对应行是 `C#16`（README:325）；C#8 是“非 K 线输入证据结构”。r3 新增文本，指向错行。

### NR-5（P3，文档/golden）K02-N16 的 `derivation` 未随 inputs 同步
- README §11 changelog 登记“K02-N16（r3）inputs 移除自由标签 `kind:"other_kind"`”（README:274），但 golden 的 `derivation` 仍写“意图只声明 `other_kind`”（`docs/contracts/golden/k02_evidence.golden.json:1638`），实际 intent 已无 `kind` 字段（已核对）。违反 §11“`derivation` 必须写明期望来自规格条款…不得自证”的准确性要求（用例本身 PASS，仅文档失真）。

### NR-6（P3，索引）r2 两项“部分覆盖”仍未补行
- #2（按 action/side/order_type 的必需 claim 策略）：r3 已实现 kind 注册表与 bundle 级 `required_claim_kinds`，但 per-action 映射仍未索引（L2 只覆盖“策略几何/RR 在调用侧”）；
- #9（采集 deadline/尝试次数/降级原因）：仍只有 deadline 进 L4，attempts/降级原因无条目。
- 建议：在 §12 表尾补两行或在 L2/L4 的“下游任务”列里显式纳入。

### 观察（不阻塞）
- O1：恢复证据 entry 允许携带未声明键（如 `{"observed_at_ms","value","junk"}`）→ C2 仍 `recovered=true`；整个 evidence dict 会进 `resolution_evidence_hash`，影响限于“结构不严”，建议明确“多余键忽略”写入 §7。
- O2：`Incident(status="OPEN", ack_at_ms=非 None)` 时 `check_recovery` 忽略 `ack_at`（k05_trace.py:224 仅 `status=="ACK"` 时读取），可报 `recovered=true`，但 `resolve_incident` 仍 `ACK_REQUIRED`（C3），fail-closed 形状不一致、无判定危害。

## 3. 对抗批次与实测

| 批次 | 脚本 | 关键实测 |
|---|---|---|
| A（K02 rules 绑定/quality） | `ce_r3_adversarial.py` | stale 抹除无 rules 即拒（A1）；10 键缺失全拒（A6）；`bar_times/finality` 篡改拒（A7）；`dedup_removed` 改写+正确 rules 仍 `valid=True`（A3，NR-1）；rules 传参下 A2/A4/A5 全 `RULES_MISMATCH` |
| B（ModelRoute） | 同上 | 空 `model`：ModelRoute 拒 / Mapping 路径收（B2, NR-3）；`model=None` 拒（B6）；重复 fallback 条目改变哈希（B5，符合预期）；额外键收（B4） |
| C（K05 新鲜度） | 同上 + `ce_r3_k05_boundary_output.txt` | `age==max_age` 恰好新鲜、+1ms `EVIDENCE_STALE`；`observed==now` 过、`+1ms` `EVIDENCE_FROM_FUTURE`；`observed==ack_at` 过、`-1ms` `EVIDENCE_PREDATES_ACK`；OPEN 直接 resolve/never-ACK/二次 resolve 全 `ACK_REQUIRED` |
| D（kind 注册表） | 同上 | 合法 kind `PASS`；字面量 RHS、错误 op、输入值 LHS→`CLAIM_KIND_CONTRADICTION`；未注册→`CLAIM_KIND_UNKNOWN`；跨 interval→矛盾 |
| E/F（非 OPEN、杂项） | 同上 | 无授权→`NEEDS_AUTH`；`claims="abc"/[None,7]` 结构化拒绝不崩溃；越界引用→`CLAIM_OUTSIDE_BOUND_INPUTS`；重复 fact_id→`valid=False` |
| P（残余复现） | `ce_r3_probe2.py` | `rules=None` 下 A2/A4 洗白→`VALID`（NR-2）；伪造账户快照（非 K 线、无 interval、closed+时间戳）claim→`VALID`（C#8 已索引残余） |

## 4. README §12 缺口索引核对

- **8/8 补齐**（r2 §4 列举）：C#5→README:318、C#6→:319、C#8→:320、C#10→:321、C#11→:322、C#12→:323、C#13→:324、C#16→:325；表头声明“C §4 八项 + B §5 八项”（:314），B 侧 B-O1..B-O8 八行（:326-333）+ 2 行“补充”（:334-335），计数一致。
- **表述抽样**：C#5/C#8/C#10/C#12/C#13 描述与 r2 原文一致；C#6 合并了 `market_venue`（L14 呼应）；C#11 标注 DC12；C#16 含“capability/mock 标志与参数来源”，但 §3:74 的交叉引用错指 C8（NR-4）。L13/L14 与 C#5/C#6 对应关系准确；L10 复核仍准确（`compare()` 确为期望子集断言，scripts/smoke_contracts_v3.py:812-833）。
- **“已修（r3）”抽样**（README:284-292）：K05 先 ACK、K02 事实集合/kind/畸形 claims、K01 ModelRoute 均与实现一致；唯“质量字段**全量**重算”对 `dedup_removed` 不成立（NR-1）。
- 仍缺索引：#2 per-action claim 映射、#9 attempts/降级原因（NR-6）。

## 5. smoke 实测（2026-09-21 本次运行）

```
结果：PASS 160 / FAIL 0；golden cases=147 (positive=44, negative=103)
schema 6/6 PASS（k01..k06 与冻结模型一致）
新增：K01-P7/N5、K02-P6/P7/N20..N30、K03-N14..N19、K04-P9/N22..N27、K05-N11..N15、K06-N7 全 PASS
```

命令：`& <venv-python> scripts\smoke_contracts_v3.py`（`$env:PYTHONUTF8=1`）；原始输出 `_review_scratch\ce_r3_smoke_output.txt`。
与 r2 对比：PASS +34（126→160）、golden +34（113→147），无回归、无既有期望静默改写（changelog 已登记 K01-N3/K02-N16/K03/K04 各项）。

## 6. 同模型偏差声明与边界

- 本报告由 opencode 子代理执行，模型 `deepseek-v4.1-flash`；被审契约与部分修复可能出自同族模型，存在共同盲区（例如把“应传 rules”当作已封堵、对 `dedup_removed` 这类无消费者的审计字段不敏感）。全部结论给出可复算证据（`b36d8a9` 行号、脚本与输出），建议 Q/E 用独立夹具复验 NR-1/NR-2。
- 边界复核：本会话未进入/读取运行目录 `multi-agent-trader\`、`data/`、`.env`；未联网；未启动服务；未修改任何 tracked 文件（HEAD=b36d8a9，`git status --porcelain` 仅 `_review_scratch/` 与 `U00_review_C_w0r1*.md` 为 `??`）。

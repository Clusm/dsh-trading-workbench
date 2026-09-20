# U03 修复轮 r2 独立复验（Q 角色）

- 复验对象：`5ffc6c0`（分支 `task/U03`，父 `6b841ff`；上一版 `3f49e03` 被 D/B 判 CHANGES_REQUESTED）
- 工作区（独立 detached）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U03\R2\r1`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest，全程离线
- 纪律：只读，未修改任何 tracked 文件（`git status` 仅 `?? _scratch_Q/`）；未提交/推送/切分支；未读 `.env`/`data/`/密钥；未联网；未启停服务；全部产物只在 `reviews/U03/R2/r1/_scratch_Q/`
- 本会话接续上一中止会话：复用其 `probe_q1/q2/q2b` 与原始 stdout（未重写），修复 `probe_q4.py` 的自身 bug（`base_intent()` 重复关键字）后补跑，并新增 `probe_q3/q5/q6/q7` 与若干静态核验

## 结论

**CHANGES_REQUESTED**

7 个指派项中 **6 项 CLOSED、1 项 PARTIAL**；9 项回归检查**全部仍成立**；接线补丁 `git apply --check` CLEAN（纯插入 112 行/0 删除）。r2 的主线修法（claim 权威化、字段级分源、逐意图绑定、status 一致化、缺 id/畸形输入结构化拒绝）经独立反例验证**确实成立**，且未发现 r2 引入的崩溃（我的探针在 ~70 组畸形输入上 CRASH COUNT = 0）。

但存在 **1 个未登记的 fail-open（P2）**，正好落在 r2 自己宣称修好的那条控制上：`identity_pinned = identity is not None`（`research_evidence.py:1299`）把模块**自带的**未固定身份哨兵 `UNPINNED_IDENTITY` 也判为"身份已固定"，于是 `accepted=True`、`report.ok=True`、`decision_gate()["ok"]=True`，且报告里 `identity_pinned=True` 是对审计字段的**虚假背书**——与本模块 docstring 第 86-88 行"不允许把未固定身份当作已验证身份"直接冲突（Q-NEW-1）。按标尺（未登记的 P2 fail-open ⇒ CHANGES_REQUESTED），故判 CHANGES_REQUESTED。

另有 4 项必须处理/登记的残留（详见"新发现"）：

- **Q-NEW-3（P2，政策未启用）**：接线侧 `RESEARCH_EVIDENCE_REQUIRED_SOURCES = ()`，且 `TradeIntent`/`DecisionEvidence` **没有** `required_sources`、`claims`、`bound_inputs` 字段 ⇒ 字段级分源门禁在**生产路径完全不生效**，D-P1-2 的原始风险（funding 缺失被读成费率中性）在生产仍存在。模块级修复正确，但没接线、也没有 known_limits 登记。
- **Q-NEW-7（P0 级残留，r1 起未修）**：`scripts/smoke_u03_*.py` 存在但未登记进 `scripts/run_offline_regression.py`，我独立复现 `_validate_smoke_manifest()` → `RuntimeError` ⇒ 共享离线回归入口在该 head 上是**红的**（`main()` 第一行就抛）。
- **Q-NEW-2（P3）**、**Q-NEW-4/5/6/8/9（P3）**：见下。

登记状态说明（影响判尺）：`verification_policy.required_artifacts` 要求提交包含 `known_limits`，产物落在 `mat-wt/I/docs/dispatch/reviews/U03/<commit>/`。该目录下**只有 `3f49e03`**（且其中只有 D/B 报告，无作者提交包）；仓库 `README.md` 无任何 U03 / known-limits 条目；作者 r2 工作区 `CD/_scratch_author_r2/` 也无 known_limits 文件。**我未能找到 r2 的 known_limits 登记，因此"已登记"这一条按事实判为不成立/无法核验**。

---

## 逐项判定

| # | 项 | 判定 | 依据 |
|---|---|---|---|
| 1 | D-P1-1 模型自报 claim 不得覆盖程序 claim | **CLOSED** | q1_out.txt（194 行，14 组反例） |
| 2 | B-P1-1 identity=None fail-closed | **PARTIAL** | q2_out.txt / q2b_out.txt / q7_out.txt（`identity=None` 路径 CLOSED；`UNPINNED_IDENTITY` 显式传入仍 fail-open） |
| 3 | B-P1-2 畸形 claim 元素不崩溃 | **CLOSED** | q2_out.txt Q2-G/H（CRASH COUNT = 0） |
| 4 | D-P1-2 字段级 required_sources | **CLOSED**（模块级；接线未启用见 Q-NEW-3） | q4_out.txt |
| 5 | B-P1-3 evidence_id / binding_id 唯一性 | **CLOSED**（模块级；轮次分量未生效见 Q-NEW-2） | q5_out.txt |
| 6 | D-P2-1 status 与 accepted 自相矛盾 | **CLOSED** | q4_out.txt Q4-G（自相矛盾用例数 = 0） |
| 7 | 其余作者声称已修项抽查（9 项） | **CLOSED** | q6_out.txt |
| 回归 | 9 项回归检查 | **全部仍成立** | q3_out.txt + q3_finality.txt |
| 补丁 | `u03_orchestrator_integration.patch` 审读 | **可应用；引入 1 项 P3 + 2 项待登记** | q_patchcheck.txt + 静态审读 |

### 1. D-P1-1 模型自报 claim 不得覆盖程序 claim — **CLOSED**

程序 claim 一律并入，同 kind 但 `expr`/`expected` 不一致即点名 `CLAIM_DECLARATION_REJECTED:override:<kind>`；无 `trigger_price` 即 `MISSING_TRIGGER_PRICE`；自报 claim 引用幽灵事实/跨周期事实 → `UNRESOLVED` 并拒绝。14 组反例全部拒绝，无一放行（原始输出 `q1_out.txt`）：

```
Q1-A 对照 trigger_price=900（真实 close=744.13）  reasons=['K02:CLAIM_FALSE']
Q1-B 自报 claims=[{kind:close_above_price, expr:'1h:closed_close > 0.0001'}]
      reasons=['CLAIM_DECLARATION_REJECTED:override:close_above_price', 'K02:CLAIM_FALSE']
      摘要 claim 仍为 '1h:closed_close > 900'（程序基准未被替换）
Q1-C 不给 trigger_price 只给 claims  reasons=['MISSING_TRIGGER_PRICE']
Q1-D 同 claim_id 省略 kind（两条 claim 并存）  reasons=['K02:CLAIM_FALSE']
Q1-E 换成恒真 kind                reasons=['K02:CLAIM_FALSE']（程序 claim 仍在）
Q1-F 同 kind 声称相反值            reasons=['CLAIM_DECLARATION_REJECTED:override:close_above_price',  'K02:CLAIM_FALSE']
Q1-G 引用不存在事实                reasons=['K02:CLAIM_FALSE','K02:CLAIM_UNRESOLVED']
Q1-H 引用跨周期事实                reasons=['K02:CLAIM_FALSE','K02:CLAIM_UNRESOLVED']
Q1-I/J 越界/别币 bound_inputs      reasons=[...,'K02:CLAIM_OUTSIDE_BOUND_INPUTS',...]
Q1-K 篡改 evidence_id              reasons=['K02:EVIDENCE_BUNDLE_MISMATCH',...]
Q1-L 自报 cycle_id 冲突            reasons=['K02:CYCLE_MISMATCH',...]
Q1-M 伪造 identity                 reasons=['K02:IDENTITY_MISMATCH',...]
Q1-N trigger_price='NaN'/'Infinity'/''/'abc' -> ['MISSING_TRIGGER_PRICE']
```

### 2. B-P1-1 identity=None fail-closed — **PARTIAL**

`identity=None`（默认，即接线补丁的传法）**已 fail-closed**，且 `unpinned_allowed` 默认 False：

```
J1 identity=None, allow_unpinned 缺省
    status=REJECTED accepted=False reasons=['IDENTITY_UNPINNED']
    report.ok=False identity_pinned=False unpinned_allowed=False
    gate.ok=False drop=['intent-X'] bindings={}
J7 非 OPEN 不误伤：c1 NOT_APPLICABLE accepted=True / o1 REJECTED，gate drop=['o1']
```

`allow_unpinned=True` 时才放行（且仅在显式开关下）——见"第 6 项判定"。**PARTIAL 的原因**是同一控制的同族逃逸：显式传入模块自带的哨兵 `UNPINNED_IDENTITY` 会被判为"身份已固定"（Q-NEW-1）。

### 3. B-P1-2 畸形 claim 元素不崩溃 — **CLOSED**

上一版的 `dict(claim)` 无条件转换已换成 `declared_claims_or_issues` 形态守卫。Q2-G 的 ~30 组畸形 claims + Q2-H 的 5 组非 Mapping intent，**`>>> CRASH COUNT = 0`**，全部结构化拒绝且不炸整批：

```
[None]/['abc']/[123]/[[]]/[[1,2]]/[set()]/[b'abc']/[object()]
        -> CLAIM_DECLARATION_REJECTED:element[0]
[{}]/[{'a':1}]/[{'expr':None}]/[{'expr':123}]/[{'expr':'no operator'}]/[{'expr':True}]
        -> CLAIM_DECLARATION_REJECTED:expr[0]
[{'kind':123|'nope'|{...}}]        -> K02:CLAIM_KIND_UNKNOWN（不崩）
[{'expr':..,'expected':'yes'|1}]   -> K02:CLAIM_EXPECTED_INVALID
claims='abc' / {} / 123            -> CLAIM_DECLARATION_REJECTED:not_a_sequence
claims=None / () / []              -> 合法（无附加项）
10^6 长 expr                       -> K02:CLAIM_FALSE（不崩）
intent=None|'str'|123|['a']|object() -> INTENT_MALFORMED:TypeError，该条拒绝，GOOD 条不受影响
```

（补充：`q6_out.txt` I 段复核同一条 5 种类型，`GOOD` 条仍 VALID，drop 只含 `invalid[0]`。）

### 4. D-P1-2 字段级 required_sources — **CLOSED**（模块级）

修好上一会话探针的自身 bug（`base_intent()` 被 `**OK` 重复传 `intent_id`）后，改用**作者自己的夹具出参**建快照（bar 完整 ⇒ K02 干净），单变量验证分源门禁（原始输出 `q4_out.txt`）：

```
PARTIAL 快照（源 available=True、只有 markPrice，funding_rate 缺、未折算 0）：
  mark_funding = {available: True, source_at_ms: ..., missing_fields: ['funding_rate'],
                  values: {'mark_price': '744.1'}}      # 没有 funding_rate 键
  snapshot.missing_fields = ('mark_funding.funding_rate',)

Q4-A required=['mark_funding']（源级）  PARTIAL -> accepted=False
     src_reasons=['SOURCE_FIELD_MISSING:mark_funding.funding_rate']     ← D-P1-2 的原缺口已关闭
                                        FULL    -> accepted=True
Q4-B required=['mark_funding.funding_rate']（字段级）
     PARTIAL -> accepted=False ['SOURCE_FIELD_MISSING:mark_funding.funding_rate']
     FULL    -> accepted=True
Q4-C 六个字段逐一在 FULL 上声明 -> 全部 accepted=True
     open_interest 源在、字段缺（源级与字段级写法）-> 均 accepted=False
     ['SOURCE_FIELD_MISSING:open_interest.open_interest_usd']
Q4-D 拼错名一律 fail-closed：
     'mark_funding.fundingrate'/'Funding_Rate'/'FundingRate'/'ticker.lastprice'
        -> SOURCE_FIELD_UNKNOWN:<原名>
     'funding_rate'/'nonexistent'/'nonexistent.field'/'.funding_rate'/'Ticker.last_price'
        -> SOURCE_UNAVAILABLE:<源名>
     'mark_funding.funding_rate.extra' -> SOURCE_FIELD_UNKNOWN:<原名>
     '  mark_funding.funding_rate  '   -> accepted=True（正确 strip）
Q4-E default_required_sources 真生效（意图不自报时）：
     default=()                                 -> accepted=True
     default=('ticker',)                        -> accepted=True（FULL 字段齐全）
     default=('mark_funding.funding_rate',)     -> accepted=False  ['SOURCE_FIELD_MISSING:...']
     default=('mark_funding',)                  -> accepted=False  ['SOURCE_FIELD_MISSING:...']
     default=('nonexistent',)                   -> accepted=False  ['SOURCE_UNAVAILABLE:nonexistent']
Q4-F 源不可用/无时点/过期 的字段级路径：
     ticker 无 source_at_ms -> SOURCE_TIME_UNVERIFIED:ticker
     ticker 时点 120s 前(TTL=30s) -> SOURCE_STALE:ticker + K02:QUOTE_EXPIRED
     mark_funding 源不可用（作者 patched_sources_missing 夹具）-> SOURCE_UNAVAILABLE:mark_funding
```

两处 P3 形态问题见 Q-NEW-4（declared 覆盖 default）、Q-NEW-5（`mark_funding.`/空串静默降级）。

### 5. B-P1-3 evidence_id / binding_id 唯一性 + 绑定面 — **CLOSED**（模块级）

```
Q5-A 同周期同币两单
  intent-A: evid=eb-adb780dee3a1a073 bind=binding-4204e88b5640b594
  intent-B: evid=eb-62f4a580d425edab bind=binding-fd0ed7475e7cc39f
  互异? True / True        ← 上一版"同轮两单共享同一 evidence_id"已修
Q5-B 同 symbol 不同 interval（ETHUSDT 1h vs 15m）: eb-daff3df82180f755 != eb-9bea82fbac5bbb05
Q5-C 同 interval 不同 as_of: as_of A=1789862400000 B=1789862460000 -> evid 互异 True
Q5-D 多轮循环: cyc-1/cyc-2/cyc-3/cyc-1 -> 3 组唯一对，cyc-1 可复现（确定性）
Q5-E identity 变 -> evidence_id 相同(True) 但 binding_id 互异(True)
     （binding 种子含 identity_hash；evidence 种子不含，符合设计）
Q5-F 40 单同轮同币同周期、字段组合各异 -> 唯一 evidence_id=40 / 唯一 binding_id=40
Q5-G 无 id 两条 -> open[0]/open[1]，evidence_id 互异；显式同 id 两条 ->
     ['DUPLICATE_INTENT_ID:dup'] 两条皆 REJECTED（fail-closed）
Q5-H report.bindings() 2 条（含 rejected 一条，字段齐全：intent/symbol/interval/
     evidence_id/binding_id/status/k02_status/accepted/applies_to_open/cycle_id/identity_pinned）
     decision_gate()['bindings'] = 只含 accepted∧applies_to_open 键集合
Q5-I 非 OPEN -> evidence_id='' / binding_id='' ，gate.bindings={}（本模块不为退出产出绑定）
Q5-J interval=None/'7m' -> 仍生成 id 且结构化拒绝（INTERVAL_MISSING / INTERVAL_UNSUPPORTED），不崩
```

**注（Q-NEW-2）**：`binding_id` 的"轮次"分量在接线路径上恒为空串，见"新发现"。

### 6. D-P2-1 status 与 accepted 自相矛盾 — **CLOSED**

`IntentEvidence` 拆出 `k02_status`，U03 层有任一拒绝理由即 `status=REJECTED`。全量扫描 12 组用例（含 U03 独有拒绝、K02 独有拒绝、重复 id、畸形 claim）**自相矛盾用例数 = 0**（`q4_out.txt` Q4-G）：

```
全合法            detail=[('intent-OK', 'VALID', True)]
源字段缺失         detail=[('intent-OK', 'REJECTED', False)]
无快照/interval 缺/周期不符/几何非法/自报事实(顶层)/自报事实(嵌套 evidence)/
未固定身份/模型重叠 claim/畸形 claim/重复 id
                  detail=[(..., 'REJECTED', False), ...]
Q4-H 非 OPEN -> NOT_APPLICABLE accepted=True applies_open=False evid='' （探针崩溃已修）
```

### 7. 其余作者声称已修项抽查 — **CLOSED**（9 项，`q6_out.txt`）

| 抽查项 | 结果 |
|---|---|
| a `fresh_until` 只可缩短 | 自报 `now+100h` → 实际 = `min(source_at,observed)+TTL`=1789862429500，被夹住；`fresh_at(now+99h)=False`。自报更短（now-100）→ 采信更短值，`fresh_at(now)=False` |
| b 异周期 bar 剔除 | 4h/1h 快照夹 15m bar → `ok=False bars=['c1'] issues=('INTERVAL_MISLABELED:1h<-15m',)`，意图 `REJECTED ... ['SNAPSHOT_UNUSABLE', ...]`，摘要不含 8888 |
| c interval 冲突点名 | `interval='1h'` + `evidence.confirmation_interval='4h'` → `['INTERVAL_DECLARATION_CONFLICT:1h!=4h']` |
| d 重复/缺 id | `['DUPLICATE_INTENT_ID:dup']` 两条皆拒、`gate.ok=False`；缺 id 两条 → `open[0]/open[1]` 各自 VALID 且 evidence_id 互异 |
| e `bound_inputs=[]` 被尊重 | `['K02:MISSING_EVIDENCE_BINDING']`（不替调用方补）；不给该键 → 自动派生 VALID |
| f 报价优先快照派生 | 注入 999.99 → 摘要 `quote: price=744.13@1789862399500`；注入 120s 过期报价不改变结果 |
| g MODEL_FACT_OVERRIDE 深入 `evidence` | `evidence={'last_price','close','facts'}` → `['MODEL_FACT_OVERRIDE:facts','...:last_price','...:close']` |
| h 时效窗口冻结上界 | 三个窗口 1e9/负值 → `ValueError: ... 超出冻结上界`（含 `snapshot_from_tool_result` 入口） |
| i 非 Mapping intent 不炸整批 | 5 种类型 → 该条 `invalid[0]` 拒绝，`GOOD` 条仍 VALID |

---

## 回归检查（9 项，全部仍成立）

原始输出 `q3_out.txt`、`q3_finality.txt`、`q9_idcycle.txt`。

| 回归项 | 结果 | 关键原始输出 |
|---|---|---|
| 进行中 bar 哨兵不入事实 | 成立 | 3 根 1h 列式（第 3 根 `finality=live`，close=8888.88）→ 意图 VALID，`facts: ...1h:closed_close=744.13...`，`哨兵出现在 facts? False`，`摘要含 8888? False` |
| 未来 bar 改判 live | 成立 | 显式 `finality="closed"` + `close_time=as_of+10h` → `build_bundle` 输入 `finality=live`，derived facts 只有 `1h:closed_close=744.13`，哨兵被剔除 |
| 15m 不冒充 4h | 成立 | 4h 意图 vs 1h/15m 快照 → `INTERVAL_MISMATCH`；1h 意图 vs 15m 快照 → `SNAPSHOT_MISSING`；15m 出参标 1h → `snapshot.ok=False issues=('INTERVAL_MISLABELED:15m!=1h',)` |
| `now_ms` 必传 | 成立 | 省略 → `TypeError: missing 1 required keyword-only argument: 'now_ms'`；位置传 → `TypeError: takes 2 positional arguments but 3 were given` |
| TTL 等号保守 | 成立 | `now=source+29999` → VALID；`+30000`（等号）→ `SOURCE_STALE:ticker` + `K02:QUOTE_EXPIRED`；`+30001` 同 |
| 真实 0 保留 `"0"` | 成立 | `mark_funding.values={'mark_price':'2410.4','funding_rate':'0'} missing=()`，`required funding_rate` 仍 VALID；`patched_sources_missing` → `values={} missing=('mark_price','funding_rate') available=False` |
| 跨周期隔离 | 成立 | 同币 1h/15m 两单（只有 1h 快照）→ 1h VALID、15m `INTERVAL_MISMATCH` 拒绝，证据 id 互异 |
| `rules` 必传 | 成立 | `validate_bundle(bundle)` 不传 rules → `RULES_MISSING`；正常一轮 reasons 无 `RULES_*` |
| 时效过期 `+61s` → EXPIRED | 成立 | `now=as_of+61000` → `k02=EXPIRED reasons=['K02:BUNDLE_EXPIRED','K02:QUOTE_EXPIRED']`；`+59s` → 仅 `QUOTE_EXPIRED`；`+601s` → `SNAPSHOT_STALE`+`BUNDLE_EXPIRED` |

补充：作者两个 integration 测试我**独立复跑**（`q8_test_research.txt` / `q8_test_market.txt`）：`PASS 119 / FAIL 0`、`PASS 38 / FAIL 0`，均 exit 0（与作者自述一致，但不作为结论依据）。

---

## `u03_orchestrator_integration.patch` 审读（第 5 项）

`q_patchcheck.txt`（本会话自行执行，未采信作者转述）：

```
### git apply --check -v (HEAD=5ffc6c0)
Checking patch src/agents/orchestrator.py...
REAL_EXIT=0
### git apply --stat / --numstat
 src/agents/orchestrator.py | 112 ++++++++++++  /  1 file changed, 112 insertions(+)  (112  0)
### blob 对照：期望补丁 index 行 7ac28fb
 HEAD:src/agents/orchestrator.py   = 7ac28fb
 ffdab44:src/agents/orchestrator.py = 7ac28fb     ← 与基线一致，补丁上下文未漂移
### 另两个补丁
 s01_market_query_adaptation.patch        S01_EXIT=0
 u03_offline_regression_registration.patch REG_EXIT=0
```

结论：**三重验证通过**——HEAD/基线两处 blob 与补丁 index 行一致、`--check` CLEAN、`--stat` 为纯插入 112/0，与作者自述（112 行纯插入、CLEAN、去掉 `or "15m"`、改用 `client_order_id`、`identity=None` + `ALLOW_UNPINNED_RESEARCH_EVIDENCE=False`）**逐条相符**。上一轮 D/B 指出的接线缺陷已按方向修掉：

- 去掉 `or "15m"` 兜底（现为 `_interval = str(result.get("interval") or "").strip()`，空则**不建快照** ⇒ 后续 `SNAPSHOT_MISSING` fail-closed）✔
- `payload["intent_id"] = str(client_order_id or f"open[{index}]")`，不再丢弃 `client_order_id`、不再用 `open_indexes.index()` O(n²) 反查 ✔
- 不再改写 `pipeline_authorized`（避免 `filter_unauthorized_opens` 误报 `pipeline_bypass_blocked` critical 并连带丢掉全部存活开仓）✔
- 放行路径写 `research_evidence_binding` 事件（含 `bindings`/`identity_pinned`/`unpinned_allowed`），被拒路径补记 `unpinned_allowed` ✔

`identity=None` 默认值与第 2 项 fail-closed 的**自洽性判定：自洽**。`ALLOW_UNPINNED_RESEARCH_EVIDENCE = False` 是模块级常量、无配置/环境变量入口（`grep` 全仓只命中补丁本身），`identity=None` 时模块对每个 OPEN 判 `IDENTITY_UNPINNED` 并入丢单集合 ⇒ 与"缺省拒绝"一致（J1 实测）。**代价**（需登记，非缺陷）：U15 接线前**每个开仓意图都会被丢**，系统只能退出不能开仓，且每轮写一条 `severity="critical"` 的 `research_evidence_blocked`（见 Q-NEW-8）。

补丁新引入/未覆盖的风险：
- `cycle_id=str(getattr(decision,"pipeline_decision_id","") or "")` —— 该字段由 `core.decision_pipeline.sign_execution` 在 RiskGuard **之后**写入（`decision_pipeline.py:40`），orchestrator 内无任何赋值点（`grep pipeline_ src/agents/orchestrator.py` 无命中；`models.py:144` 默认 `None`）⇒ 门禁处恒为 `""`（Q-NEW-2，P3）。
- `snapshot_from_tool_result(result, interval=result["interval"])` 使模块内的"周期自证"在该调用点成为同义反复（Q-NEW-6，P3）。

---

## 第 6 项判定：`q2b_out.txt` 里 `gate.ok=True` 的性质

**判定：受控降级（controlled degradation），不是新的 fail-open**——但成立条件必须写清。

依据（`q7_out.txt` J2 复现 + 静态）：

1. **只能由调用方显式声明触发**：`allow_unpinned` 是 `evaluate_open_intents` 的 keyword-only 参数，默认 `False`；接线补丁用模块级常量 `ALLOW_UNPINNED_RESEARCH_EVIDENCE = False` 传入，全仓无配置/环境变量/数据库读取路径（`grep` 只有模块 docstring 与补丁）。不构成"外部输入可打开"的开关。
2. **降级被如实记录、不冒充**：`report.identity_pinned=False`、`report.unpinned_allowed=True`，`as_dict()`/`decision_gate()` 都带这两个字段，接线补丁在**放行路径**（`research_evidence_binding.raw`）与**拒绝路径**（`research_evidence_blocked.raw`）都写入 ⇒ 审计可区分"未固定身份下放行"。
3. **不是无条件放行**：`gate.ok` 仍要求 `self.ok`（即没有意图被拒）；J2 里几何/claim 全合法才 True，`q2_out` Q2-B 中 `CLAIM_FALSE` 时 `gate.ok=False`。
4. **身份哈希不冒充**：`UNPINNED_IDENTITY.identity_hash()` 与真实 `Identity` 哈希不同（J6：`5ee087…` vs `9658d2…`），`binding_id` 因含 `identity_hash` 而与固定身份下的绑定可区分（J5 对照）。

**但同一控制存在真正的逃逸（Q-NEW-1）**：把哨兵当参数传回去（或传等值裸 dict）即得 `identity_pinned=True` ⇒ 此时"降级"不再被记录，审计字段变成虚假背书。这才是 fail-open，且与 `allow_unpinned` 的存在性无关。

---

## 新发现

### Q-NEW-1（P2，fail-open）显式传入 `UNPINNED_IDENTITY`（或等值 dict）⇒ `identity_pinned=True`，身份门禁整体失效
位置：`src/agents/research_evidence.py:1299` `identity_pinned = identity is not None`
复现（`q7_out.txt` J3/J4，一行命令即可）：

```
identity=UNPINNED_IDENTITY, allow_unpinned 缺省
  status=VALID accepted=True reasons=[]
  report.ok=True identity_pinned=True unpinned_allowed=False identity_hash=5ee0871803844434...
  gate.ok=True drop=[] bindings={'intent-X': {'evidence_id': 'eb-52758d5379aedd9d', ...}}
identity=<unpinned 同值裸 dict>   → 同上（identity_pinned=True）
对照 J1 identity=None, allow_unpinned 缺省 → REJECTED ['IDENTITY_UNPINNED'], gate.ok=False
```

- 危害：`identity_pinned` 的语义是"本轮身份已核对"（docstring 第 87 行），而 `UNPINNED_IDENTITY` 的语义是"未固定身份，**不允许**当作已验证身份"（第 86-88 行）。二者相撞使报告输出**虚假的已验证背书**，且 `decision_gate()` 全绿。这是 r2 自己主打的 B-P1-1 修复的旁路。
- 可达性：**当前接线不可达**（补丁传 `None`）。但哨兵是模块级公开常量（无下划线），且 U15 接线的自然写法 `identity = build_identity() or UNPINNED_IDENTITY` 会一行恢复 fail-open；对模块的**公开 API 调用方**（K02 intent 是自由 dict，该入口会长期存在）则是可达的。
- 建议一行修：`identity_pinned = identity is not None and resolution != UNPINNED_IDENTITY`（或把哨兵与 `allow_unpinned=True` 绑定，`allow_unpinned` 缺省时哨兵同样判 `IDENTITY_UNPINNED`）。
- 登记状态：模块 docstring 只写了 `identity=None` 的情形，**未登记该路径**。

### Q-NEW-2（P3）接线路径上 `binding_id` 的"轮次"分量恒为空串
`decision.pipeline_decision_id` 在门禁执行时尚未写入（`decision_pipeline.py:37-47` 在 RiskGuard 之后），接线传 `cycle_id=""`。复现（`q9_idcycle.txt`）：

```
cycle_id=""  run1: eb-3c9aa8883b37968a binding-ea5a3e67d7a9ef74
cycle_id=""  run2: eb-3c9aa8883b37968a binding-ea5a3e67d7a9ef74   ← 完全相同的绑定
同 cycle_id="" 两轮 id 完全相同? True
cycle_id="round-2": eb-e015f7042b0b9095 binding-bd141f1029cb837a
```

同一快照被两轮复用（`query_snapshots` 在 `run_orchestrator` 顶层创建、跨轮不清理）时逐意图绑定不可区分。不影响任何门禁判定，但"binding_id 绑定轮次"在**接线层面**不成立（模块层面对 `cycle_id` 变敏感，Q5-D 已验证）。建议接线改传 `pipeline_decision_id or decision_id or 本轮 run_id/trace_id`。

### Q-NEW-3（P2，政策未启用/未登记）字段级分源门禁在生产路径完全不生效
`TradeIntent`/`DecisionEvidence`（`src/agents/models.py`）**均无** `required_sources` 字段；补丁 `RESEARCH_EVIDENCE_REQUIRED_SOURCES = ()`。⇒ `declared=()` 且 `default=()` ⇒ `_source_gate` 直接 `return []`（`research_evidence.py:1238`）。实测（`q4_out.txt` Q4-0）：

```
FULL,    required 未声明 -> accepted=True
PARTIAL, required 未声明 -> accepted=True      ← funding_rate 缺失仍放行
```

即 D-P1-2 描述的原始失败模式（"声明依赖资金费却拿不到 funding 数据仍放行"）在**生产路径依旧存在**；r2 修好的是模块能力，不是生效口径。补丁注释写"策略卡就绪后在此收紧口径"、模块 docstring 写"缺省即空集合"，属代码注释级说明，**未见 r2 known_limits 登记**。建议：要么在接线处给非空缺省（至少 `("ticker",)`），要么在提交包 `known_limits` 明写"分源门禁缺省关闭，由策略卡开启"。

### Q-NEW-4（P3）`required_sources` 由意图自报可**覆盖**策略侧 default（自选更弱集合）
`_resolve_required_sources`（`:1216-1223`）在 `declared` 非空时**完全丢弃** `default`。复现（`q4_out.txt` Q4-E 末两行）：

```
declared=('ticker.last_price',) default=('mark_funding.funding_rate',) -> accepted=True   ← default 被绕过
declared=[]                     default=('mark_funding.funding_rate',) -> accepted=False  ← 空列表不覆盖（保守，好）
```

当前 `TradeIntent` 无该字段 ⇒ 不可达；但一旦模型可写该键（K02 intent 是自由 dict），被门禁者就能自行降低分源门槛。建议 `declared | default`（并集）而非覆盖。

### Q-NEW-5（P3）畸形 `required_sources` 条目静默降级/忽略
`'mark_funding.'`（尾点）→ `field=""` ⇒ 静默当成**源级**声明（本例 FULL 下放行）；`''`/`'   '` → `continue` 静默忽略；`required_sources="mark_funding"`（字符串非序列）→ `normalise_intent` 判非 list/tuple ⇒ `()` **完全无门禁**（`q4_out.txt` Q4-D、`q10_newevidence.txt`）。均非放宽（尾点是收紧），但"拼错即 fail-closed"的承诺在这些形态上不成立，建议对空条目/非序列声明记一条 reason code。

### Q-NEW-6（P3）接线的"周期自证"是同义反复
补丁传 `interval=result["interval"]`，而模块用同一值交叉核对出参标签（`snapshot_from_tool_result:513-520`）⇒ 该检查在接线处永不触发（恒等）。防"15m 冒充 4h"实际由 `_locate_snapshot` 的 `(symbol, interval)` 精确匹配承担（R3 已验证成立）。残留：若工具出参自身把 15m 数据标成 4h，模块无从发现（`bars_from_packed_candles` 按传入 span 反推收盘时间，自洽）。离线不可进一步验证（需真实市场数据）。

### Q-NEW-7（P0 级残留，r1 起未修）共享离线回归入口在该 head 上仍然打红
`q_manifest.txt`（我独立执行）：

```
MANIFEST FAILED: RuntimeError 未登记冒烟脚本: scripts/smoke_u03_market_evidence_quality.py, scripts/smoke_u03_research_evidence.py
```

`scripts/run_offline_regression.py:123` 的 `main()` **第一行**即 `_validate_smoke_manifest()` ⇒ 整个离线回归入口抛错，无法运行。`5ffc6c0` 只改了 orchestrator 补丁，未处理此项；`u03_offline_regression_registration.patch` 仍只是提案（`REG_EXIT=0` 可应用）。属 B_r1 P0-1 的原样残留。

### Q-NEW-8（P3，需登记）U15 接线前系统无法开仓 + 每轮 critical 事件
`identity=None` + `ALLOW_UNPINNED=False` ⇒ 每个 OPEN 都被 `IDENTITY_UNPINNED` 丢弃；补丁对"有 blocked"的每一轮写 `db.log_risk_event(event_type="research_evidence_blocked", severity="critical")`。方向 fail-closed（安全），但运营上表现为"开仓功能整体下线 + 每轮 critical 告警"，必须写进 known_limits/上线说明，否则会被当成故障。另：`blocked_symbols` 只用于 note 文本，`decision` 会被改写为 `no_trade`/`adjust`，与 `_build_decision` 的原 decision 语义不冲突（静态审读，未实跑）。

### Q-NEW-9（P3）`evidence.data_observed_at_ms` 必须与快照 `observed_at_ms` 精确相等
复现（`q10_newevidence.txt`）：

```
evidence.data_observed_at_ms 对齐          -> VALID accepted=True
evidence.data_observed_at_ms 差 1ms        -> REJECTED ['EVIDENCE_TIME_MISMATCH']
evidence.data_observed_at_ms 秒级(截断)     -> REJECTED ['EVIDENCE_TIME_MISMATCH']
```

fail-closed（好），但模型只要抄错/取整该时间戳就整轮丢单；`DecisionEvidence` 是模型填写字段。属耦合脆弱性，建议登记（或允许毫秒级容差并落审计）。

---

## 命令清单

解释器统一为 `C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，前置 `PYTHONUTF8=1`，cwd 为工作区根。全部输出重定向到 `_scratch_Q/` 文件。

```bash
# 探针（只写 _scratch_Q/）
$PY _scratch_Q/probe_q4.py  > _scratch_Q/q4_out.txt       # 修复重复关键字 bug 后重跑（第 4/6 项）
$PY _scratch_Q/probe_q5.py  > _scratch_Q/q5_out.txt       # 第 5 项 唯一性/绑定面
$PY _scratch_Q/probe_q3.py  > _scratch_Q/q3_out.txt       # 回归 9 项
$PY _scratch_Q/probe_q6.py  > _scratch_Q/q6_out.txt       # 第 7 项 抽查 9 项
$PY _scratch_Q/probe_q7.py  > _scratch_Q/q7_out.txt       # 身份面 + allow_unpinned 判定
# 复用上一会话（未重写）：probe_q1.py/q1_out.txt、probe_q2.py/q2_out.txt、probe_q2b.py/q2b_out.txt
# 崩溃原件留档：q4_out_crash.txt

# 内联核验
$PY -c "...build_bundle 未来 bar finality..."          > _scratch_Q/q3_finality.txt
$PY -c "...cycle_id='' 绑定碰撞..."                     > _scratch_Q/q9_idcycle.txt
$PY -c "...evidence_at_ms 耦合 / required_sources 字符串..." > _scratch_Q/q10_newevidence.txt
$PY -c "..._validate_smoke_manifest()..."               > _scratch_Q/q_manifest.txt

# 补丁
git apply --check -v tests/fixtures/evidence/u03_orchestrator_integration.patch   # 真实 EXIT=0
git apply --stat/--numstat 同上；git rev-parse --short HEAD:src/agents/orchestrator.py  # 7ac28fb
git apply --check -v tests/fixtures/evidence/s01_market_query_adaptation.patch            # EXIT=0
git apply --check -v tests/fixtures/evidence/u03_offline_regression_registration.patch    # EXIT=0
                                                                        > _scratch_Q/q_patchcheck.txt

# 作者测试独立复跑
$PY tests/integration/test_research_evidence.py       > _scratch_Q/q8_test_research.txt  # PASS 119/FAIL 0, exit 0
$PY tests/integration/test_market_evidence_quality.py > _scratch_Q/q8_test_market.txt    # PASS 38/FAIL 0, exit 0
```

## 未覆盖项

1. **orchestrator 接线补丁未应用、未实跑**：只做 `git apply --check` + 静态审读。运行期行为（`_research_evidence_gate` 与风险层/`filter_unauthorized_opens` 的交互、`decision` 被改写为 `no_trade` 后下游如何呈现、critical 事件频率）无法离线验证，需 LLM/网络。
2. **`cycle_id` 恒为 `""` 是静态推断**（orchestrator 内无 `pipeline_*` 赋值点、`decision_pipeline` 在更晚阶段写入）；未实跑端到端确认。
3. **`interval` 自证同义反复的利用面**：工具出参若自身标错周期，模块无从发现——需要真实交易所出参/联网才能判定，明说未验证。
4. **`smoke_contracts_v3.py` 与两个 `smoke_u03_*.py` 未重跑**（作者自述 PASS 173 等）；我只独立跑了两个 integration 测试与 manifest 校验。共享回归入口因 Q-NEW-7 无法运行，故整套回归未执行。
5. **真实身份到位后的行为**（U15 未接线）：无法验证 `identity` 传入真实 `RuntimeIdentity` 后 fail-closed 是否按预期关闭；Q-NEW-1 说明它**不会**因 U15 接线而自动消失。
6. **跨进程/仓外消费者**：`data_quality.sources` 形态变化、`funding_rate/mark_price/open_interest_usd` 值类型（float→None）对仓外（dashboard/历史库）的影响不可离线验证。
7. **`now_ms` 的真实性**：模块不读墙钟，`now_ms` 来自调用方（接线用 `time.time()`）；时钟回拨/NTP 跳变未验证（仅验证了 `now < as_of` 与 TTL 等号边界）。

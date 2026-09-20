# U03 修复轮 r3 独立复验（Q 角色，聚焦轮）

- 复验对象：`c0e14c4`（分支 `task/U03`；代码修复 `e381eca`，其后 4 个 docs 提交）
- 上一轮：`5ffc6c0` 判 CHANGES_REQUESTED（Q-NEW-1 / Q-NEW-3 未处理）
- 工作区（独立 detached）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U03\R3\r1`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest，全程离线
- 纪律：只读；未提交/推送/打标签/切分支；未读 `.env`/`data/`/密钥/账户标识；未联网；未启停服务；所有产物只在 `_scratch_Q/`
  - 过程中一处越界并已当场纠正：`_scratch_Q/apply_patch.py` 曾把重建的 post-image 写到
    `tests/fixtures/evidence/u03_orchestrator_integration.patch.postimage.txt`（相对路径落在仓库内），
    发现后立即 `mv` 进 `_scratch_Q/postimage_rebuilt.txt`；`git status --porcelain` 现仅 `?? _scratch_Q/`，
    工作区无任何 tracked/untracked 残留。
- 范围：只复验 r3 的改动 + 回归抽样（不重做 r2 已 CLOSED 的 6 项验收）

## 结论

**PASS-with-notes**

5 个指派项**全部 CLOSED**；10 项回归抽样**全部仍成立**（畸形输入 CRASH COUNT = 0）。
r3 的两条主线修法经**独立构造的反例**验证确实成立，且未发现任何 fail-open 逃逸：
`is_unpinned_identity()` 把所有哨兵形态（含自造等价裸 dict、大小写/空白变体、
`model_copy` 近等值副本）一律判未固定，同时**真实身份仍 `pinned=True`（无假阳性）**；
接线缺省 `RESEARCH_EVIDENCE_REQUIRED_SOURCES = ("ticker",)` 使字段级分源门禁在
**缺省路径真实生效**；`required_sources` 改为**并集**（自报只能加严）；畸形声明
`SOURCE_DECLARATION_REJECTED`；`cycle_id` 改为 `pipeline_decision_id or run_id`，
`binding_id` 的轮次分量不再恒空。

作者对 Q-NEW-2 / Q-NEW-3 / Q-NEW-6 / Q-NEW-8 / Q-NEW-9 / Q-NEW-7 的**残余登记经逐条独立核验，全部如实**
（尤其 Q-NEW-3：`TradeIntent`/`DecisionEvidence` 确无 `required_sources` 字段，
"未声明的资金费依赖仍不会自动拒绝"我一比一复现到）。r2 遗留的"known_limits 无法核验"
在 r3 已被解决：`docs/dispatch/reviews/U03/r3/known_limits.md` 随分支提交（76 行）。

新增 3 项 **P3**（均已带位置与最小复现，建议登记；**均非 fail-open，也非可达崩溃**）：
① 补丁 `index` 行的后像 blob `890c08f` **不可复现**（我按前像逐 hunk 重放得 `8499377`，
`git apply` 落盘后 `git hash-object` 得 `280e1ea`）——纯证据卫生问题；
② `evaluate_open_intents(identity=<残缺/越界 Mapping>)` 抛未捕获异常（`r2` 起既有、当前接线不可达）；
③ 公开导出的 `is_unpinned_identity()` 传 Mapping 会 `AttributeError`（与 `evaluate_open_intents`
显式接受 Mapping 的入参口径不对称）。

按标尺（未登记的 **P2** fail-open 或崩溃才判 CHANGES_REQUESTED；本轮无此类项），
判 **PASS-with-notes**，并把上述 P3 列为"合入前登记"事项。

---

## 逐项判定

| # | 项 | 判定 | 依据 |
|---|---|---|---|
| 1 | Q-NEW-1 未固定身份哨兵不得洗白 | **CLOSED** | `q1_identity_out.txt`（E1–E8 + 绕过 B1–B4 + 鲁棒性 T） |
| 2 | Q-NEW-3 分源门禁缺省真实生效 | **CLOSED**（残余登记如实） | `q2_sources_out.txt`（Z0–Z5）+ `q16_ttl_undeclared.txt`（E1/E2） |
| 3 | Q-NEW-2 `cycle_id` 轮次分量非空 | **CLOSED**（残余登记如实） | `q3_union_cycle_out.txt` + `q4/q5`（补丁静态） |
| 4 | Q-NEW-4/5 并集 + 畸形声明 | **CLOSED** | `q2_sources_out.txt`（U/M/D）+ `q3_union_cycle_out.txt`（V1–V6） |
| 5 | 接线补丁审读 | **可应用；+8 行未引入新风险；1 项 P3 证据卫生** | `q4_patchcheck.txt` / `q6_postimage.txt` / `q7_hashvalidate.txt` |
| 回归 | 10 项抽样 | **全部仍成立** | `q12_regression_out.txt` |

### 1. Q-NEW-1 — CLOSED（含绕过尝试，全部被堵）

`is_unpinned_identity` 判据：字段全等 `UNPINNED_IDENTITY`，或 `environment.strip().lower() == "unpinned"`。

```
is_unpinned(哨兵本体)                       = True
is_unpinned(model_copy 全等副本)            = True
is_unpinned(手工等价构造 RuntimeIdentity)    = True
is_unpinned(model_copy 改 runtime_instance_id) = True
is_unpinned(model_copy 改 artifact_hash)    = True
is_unpinned(environment='UNPINNED')        = True
is_unpinned(environment='unpinned ')       = True
is_unpinned(environment=' Unpinned ')      = True
is_unpinned(environment='unpinned\t')      = True
is_unpinned(真实身份)                       = False
is_unpinned(真实身份 + environment='unpinned') = True   ← 刻意副作用（known_limits#2 已登记）
```

门禁端到端（原始输出 `q1_identity_out.txt`，干净意图，K02 全绿以排除干扰）：

```
E1 哨兵本体          ok=False pinned=False accepted=() drop=['A'] gate.ok=False  reasons=['IDENTITY_UNPINNED']
E2 哨兵 model_copy   ok=False pinned=False accepted=() drop=['A'] gate.ok=False  reasons=['IDENTITY_UNPINNED']
E3 自造结构等价裸 dict ok=False pinned=False accepted=() drop=['A'] gate.ok=False  reasons=['IDENTITY_UNPINNED']
E4 哨兵改实例号副本    ok=False pinned=False accepted=() drop=['A'] gate.ok=False  reasons=['IDENTITY_UNPINNED']
E5 environment='UNPINNED'  ok=False pinned=False drop=['A'] gate.ok=False reasons=['IDENTITY_UNPINNED']
E6 environment='unpinned ' ok=False pinned=False drop=['A'] gate.ok=False reasons=['IDENTITY_UNPINNED']
E7 identity=None     ok=False pinned=False accepted=() drop=['A'] gate.ok=False  reasons=['IDENTITY_UNPINNED']
E8 真实身份           ok=True  pinned=True  accepted=('A',) drop=[] gate.ok=True bindings=['A'] reasons=[]
```

E1–E7 与派单要求的 `ok=False/pinned=False/gate.ok=False/drop=['A']/reasons=['IDENTITY_UNPINNED']`
**逐字段相符**；E8 证明**无假阳性**（真实身份仍 `pinned=True` 且 `gate.ok=True`、
`accepted=('A',)`、`bindings=['A']`）。

绕过尝试结果：

- B1/B2 顶层=哨兵 + `intent.identity`=哨兵/等价 dict：仍 `pinned=False`、丢单（F13 别名路径不可洗白）。
- B3 哨兵 + `allow_unpinned=True`：放行但 `pinned=False`、`gate.ok=True`（= `unpinned_allowed=True`
  的受控降级，与 r2 判定一致；缺省 `False`、无配置入口）。
- B4 真实身份 + `allow_unpinned=True`：`pinned=True`（降级开关不污染固定身份路径）。
- 类型鲁棒性 T：残缺/越界 Mapping、`str`/`int`/`object()` 抛异常但不产生 `pinned=True`
  （见"新发现 Q-R3-2"，属崩溃而非 fail-open）。

残余边界（已登记，不构成缺陷）：本模块只保证"未固定身份不得被当成已固定"，
不验证身份**真实性**（`identity` 内容全填 "unpinned" 但 `environment="prod"` 会判 `False`）——
known_limits#2 明写"真实身份的内容仍需 U15/DL01 注入"。另 `is_unpinned_identity(真实身份+environment='unpinned')=True`
的刻意副作用也已登记。

### 2. Q-NEW-3 — CLOSED（缺省路径真实生效 + 残余登记如实）

**接线常量（从补丁原文读取，不采信转述）**：`q2_sources_out.txt` 末段

```
PATCH| +ALLOW_UNPINNED_RESEARCH_EVIDENCE = False
PATCH| +RESEARCH_EVIDENCE_REQUIRED_SOURCES: tuple[str, ...] = ("ticker",)
PATCH| +        allow_unpinned=ALLOW_UNPINNED_RESEARCH_EVIDENCE,
PATCH| +        default_required_sources=RESEARCH_EVIDENCE_REQUIRED_SOURCES,
```

**缺省路径独立复现**（`default_required_sources=("ticker",)`，即接线缺省）：

```
Z1 default=('ticker',) + ticker 字段齐全   accepted=('A',) ok=True  reasons=[]            ← 不误伤
Z2 default=('ticker',) + ticker 缺 pct_24h/quote_volume_24h
                                           accepted=() ok=False
                                           reasons=['SOURCE_FIELD_MISSING:ticker.pct_24h',
                                                    'SOURCE_FIELD_MISSING:ticker.quote_volume_24h']  ← 真实生效
Z3 default=('ticker',) + ticker 全缺        accepted=() reasons=[SOURCE_FIELD_MISSING:ticker.{last_price,pct_24h,quote_volume_24h}, ...]
Z0 default=()  + 同一 PARTIAL 快照          accepted=('A',) ok=True                        ← 对照：差异确由非空缺省带来
Z5 default=('ticker.pct_24h',) + PARTIAL   accepted=()  reasons=['SOURCE_FIELD_MISSING:ticker.pct_24h']
```

Z2 与派单期望（`accepted=()` 且 reasons 含 `SOURCE_FIELD_MISSING:ticker.pct_24h`，而非放行）
**逐字相符**；Z1 证明字段齐全的正常意图仍放行（**无假阳性**），且真实身份下同样成立
（`identity=真实身份 + default=('ticker',)` → `accepted=('A',) VALID`）。

**残余登记如实性独立核验**（`q16_ttl_undeclared.txt`）：

```
E1 未声明资金费依赖 + mark_funding 不可用 + 缺省 (ticker,)  -> accepted=True  VALID  reasons=[]        ← known_limits#1 所述形态
E2 同一快照但显式声明 mark_funding.funding_rate            -> accepted=False reasons=['SOURCE_UNAVAILABLE:mark_funding']
静态：TradeIntent / DecisionEvidence 字段清单里 **没有** required_sources / claims / bound_inputs（q14_crossref.txt）
```

即：known_limits#1 的三句话——"缺省是 `("ticker",)`"、"资金费/OI 不在缺省集合内"、
"未声明时不会自动拒绝（原始形态仍在）"、"逐意图声明字段本任务未提供"——**逐条与实测一致**，
未出现"声称已修但实际未修"。补丁 `RESEARCH_EVIDENCE_REQUIRED_SOURCES` 的缺省值也与登记值一致。

### 3. Q-NEW-2 — CLOSED（模块敏感）+ 残余登记如实

`q3_union_cycle_out.txt`：

```
cycle_id=''      evid=eb-7367fa72231d6fcf bind=binding-8565b85ce332fec1   report.cycle_id=''
cycle_id='run-1' evid=eb-90dc30b4504e1d68 bind=binding-a60182a2871aab61
cycle_id='run-2' evid=eb-ad1665d4f7fe2fc7 bind=binding-5c204dcd44ef389d
cycle_id='run-1' 重复 -> 与 run-1 完全相同（确定性可复现）
同一 cycle_id 两轮: bind1 == bind2 == binding-011844166bab3025（碰撞） evid 亦相同
```

轮次分量**确实随轮次变化**（不再恒 `""`）。接线侧取值来源经静态核验：

- `service.py:777` `run_id = str(uuid.uuid4())`（每轮新 uuid4）——补丁注明的来源真实存在；
- `_research_evidence_gate(..., run_id: str)` 是补丁新增函数的**形参**（补丁第 32-33 行），
  两处调用点传 `run_id`（补丁第 135/161 行），而 `run_id` 是 `run_orchestrator` 的
  keyword-only 形参（`src/agents/orchestrator.py:789`）⇒ **不会 NameError**；
- 表达式 `str(getattr(decision, "pipeline_decision_id", "") or run_id or "")` 与登记一致
  （`pipeline_decision_id` 在 RiskGuard 之后才写入，门禁处恒为 None）。

**"同 run_id 复用"残余登记如实**：known_limits#3 明写"同一 run_id 被两轮复用时会重新出现绑定碰撞
（离线可复现）"——我复现到该碰撞；且 operational 只发生在 run_id 被复用时，而 `_run_once_inner`
每轮都重新 `uuid4()`，故为条件性残余，非当前缺陷。登记与实测一致。

### 4. Q-NEW-4 / Q-NEW-5 — CLOSED

**并集语义**（单元级 + 判别性 e2e）。单元级（`q3_union_cycle_out.txt`）：

```
declared=('ticker.last_price',) default=('mark_funding.funding_rate',)
   -> ('ticker.last_price', 'mark_funding.funding_rate')     # 并集，不再"自报覆盖"
declared=()                     default=('mark_funding.funding_rate',) -> ('mark_funding.funding_rate',)
declared=['ticker'] default=['mark_funding.funding_rate','ticker'] -> ('ticker','mark_funding.funding_rate')  # 去重保序
```

判別性 e2e（用 mark_funding **不可用** 的快照，否则两种语义同结果、测不出）：

```
V1 declared=('ticker.last_price',) default=('mark_funding.funding_rate',) -> REJECTED ['SOURCE_UNAVAILABLE:mark_funding', ...]
V2 declared=[]  default=('mark_funding.funding_rate',)                  -> REJECTED ['SOURCE_UNAVAILABLE:mark_funding', ...]
V3 declared=('mark_funding.funding_rate',) default=('ticker',)          -> REJECTED（ticker 亦被查）
```

r2 的 Q-NEW-4 反例（`declared` 更窄即可绕过 `default`）在 r3 **不再成立**：V1 里更窄的自报
集合无法顶掉策略侧的 `mark_funding.funding_rate`。

**畸形声明**（`q2_sources_out.txt`，全部结构化拒绝，无崩溃）：

```
M2 ''/M3 'mark_funding'/M4 123/M5 {'a':1}/M6 set()/M7 b'..'  -> SOURCE_DECLARATION_REJECTED:not_a_sequence
M9 ['mark_funding.']  -> SOURCE_DECLARATION_REJECTED:mark_funding.
M10 ['.funding_rate'] -> SOURCE_DECLARATION_REJECTED:.funding_rate
M11 [''] / M12 ['   '] -> SOURCE_DECLARATION_REJECTED:empty
M13 ['  ticker  '] -> accepted（正确 strip，不误伤）
M16 ['ticker','bogus'] -> SOURCE_UNAVAILABLE:bogus（fail-closed）
M17 ['ticker.last_price.extra'] -> SOURCE_FIELD_UNKNOWN:...
M18 [['ticker']] -> SOURCE_UNAVAILABLE:['ticker']  M1 None -> 合法未声明（accepted）
D1 default='ticker'（裸字符串） -> 逐字符 SOURCE_UNAVAILABLE:t/i/c/k/e/r（fail-closed，丑陋但安全）
```

`M14 ['mark_funding','mark_funding']` → 通过（不会重复计入），`U5 declared=(None,)` →
`SOURCE_UNAVAILABLE:None`（fail-closed）。

### 5. 接线补丁 `u03_orchestrator_integration.patch` — 可应用；+8 行未引入新风险；1 项 P3

`q4_patchcheck.txt`（HEAD=`c0e14c4`，我自己执行）：

```
git apply --check -v  -> Checking patch src/agents/orchestrator.py...  REAL_EXIT=0
--stat/--numstat      -> 120  0   src/agents/orchestrator.py | 120 +++++  (1 file changed, 120 insertions(+))
blob 对照             -> HEAD/ffdab44/5ffc6c0:src/agents/orchestrator.py = 7ac28fb（与补丁 index 前像一致，上下文未漂移）
s01_market_query_adaptation.patch          S01_EXIT=0
u03_offline_regression_registration.patch  REG_EXIT=0
```

与作者自述一致：`git apply --check` EXIT=0、相对 r2 的 **+8 行**、**120/0 纯插入**。
新增 8 行 = `RESEARCH_EVIDENCE_REQUIRED_SOURCES` 由 `()` 改 `("ticker",)`（含注释改写）
+ `cycle_id` 增加 `or run_id or ""`。审读结论：

- **无新风险（静态）**：① `run_id` 是该新增函数的形参且在 `run_orchestrator` 作用域内
  （见第 3 项）；② `AuditDatabase` 已在 `orchestrator.py` 顶部 import，且该文件有
  `from __future__ import annotations`（注解惰性求值，无 NameError 风险）；
  ③ `report.bindings()` 的每一项确实含 `"accepted"`/`"intent_id"`/`"evidence_id"` 键
  （`research_evidence.py:1129-1146`），补丁的 `item["accepted"]`/`item['evidence_id']` 不会 KeyError；
  ④ `TradeIntent.action` 是小写字面量而模块 `_OPEN_ACTION = "OPEN"`，看似不一致，但
  `normalise_intent` 第 1013 行 `str(raw.get("action") or "OPEN").upper()` 做了归一
  ⇒ 补丁按小写 `== "open"` 过滤与模块口径自洽，**不是**"门禁空转"缺陷（我专门查证过这一条）；
  ⑤ `TradeIntent.evidence: DecisionEvidence | None` 存在 ⇒ 补丁的
  `payload["interval"] = getattr(evidence, "confirmation_interval", None)` 有来源。
- **唯一发现（P3，证据卫生，非功能缺陷）**：补丁 `index` 行的后像短哈希 `890c08f`
  **不可复现**。我把 6 个 hunk 逐行重放到前像 blob（先验证前像重放能精确复现 `7ac28fb`，
  且 6 个 hunk 的 old/new 行数全部吻合、上下文全部匹配），得到后像
  `849937706255cac4f672e8986ebbc37ed755e61f`；把补丁真正 apply 到副本后 `git hash-object`
  得 `280e1ea7c06dbce03361f576194d20427818fef5`。两者都 ≠ `890c08f`。
  根因是 EOL：目标文件是**混合行尾的已提交 blob**（内容里 755 个 CR），补丁里有 6 行
  以 CR 结尾的上下文行，插入行的行尾处理依实现而异（我的重放保留 LF，`git apply` 落盘为 CRLF）。
  known_limits#8 声称该值"由作者可复现重建脚本复算为 890c08f"，**这一声称我无法独立复现**
  （r2 的 `4c340da` 同样不可复现，已知）。功能无影响（`git apply` 不校验该值），故 P3。
  附带确认：`git apply --3way` 因后像 blob 不在对象库而退化为直接应用（`q8_3way.txt`），
  已应用状态下不会重复插入（计数仍为 1 份），无重复应用风险。
- 已知的两项设计性残留（Q-NEW-6 周期自证同义反复、Q-NEW-9 `data_observed_at_ms` 精确相等）
  在 known_limits#4/#6 已登记，本轮**未重做**（属 r2 已核验项）。

---

## 回归抽样（10 项，全部仍成立；原始输出 `q12_regression_out.txt`）

| # | 回归项 | 结果 | 关键原始输出 |
|---|---|---|---|
| R1 | claim 权威化（自报不得覆盖程序 claim） | 成立 | `trigger_price=900`（真实 close 744.13）→ `CLAIM_FALSE`，摘要 claim 仍为 `1h:closed_close > 900`；自报真值 claim → `CLAIM_DECLARATION_REJECTED:override:close_above_price`，摘要仍是程序基准 `1h:closed_close > 744` |
| R2 | 畸形 claim 不崩溃 | 成立 | 21 组畸形 claims（含 `set()`/`bytes`/`object()`/嵌套/超长 expr）+ 非序列 → **`>>> R2 CRASH COUNT = 0`**，其中 `None`/`()`/`[]` 视为"无附加项"合法 |
| R3 | `evidence_id`/`binding_id` 唯一性 | 成立 | 同轮同币同周期两单 id 互异；**40 单 → 唯一 evidence_id=40/40、唯一 binding_id=40/40**；重复 id → 两条皆 `DUPLICATE_INTENT_ID:dup` |
| R4 | `status`/`accepted` 一致 | 成立 | 9 组用例全扫 → **自相矛盾用例数 = 0**（含未固定身份、畸形 claim、无快照、周期不符） |
| R5 | 字段级分源 | 成立 | 源级/字段级声明 + 真实 0 → 放行；`mark_funding` 源不可用 → `SOURCE_UNAVAILABLE:mark_funding` |
| R6 | 跨周期隔离 | 成立 | 只给 1h 快照：1h 单 VALID、15m 单 `INTERVAL_MISMATCH`(+`INTERVAL_DECLARATION_CONFLICT:15m!=1h`) 拒；evid 互异；15m 快照喂 1h 意图 → `INTERVAL_MISMATCH` |
| R7 | `rules` 必传 | 成立 | 不传 → `RULES_MISSING`；同源 → `valid=True`；**异源 → `RULES_MISMATCH`**（我追加的异源反例） |
| R8 | 进行中 bar 哨兵不入事实 | 成立 | `finality=live`/`unknown` 的 bar（close 8888.88/high 9999.99）→ 摘要与 facts **均不含 8888/9999**；列式三根第 3 根 `finality=live`、close_time 落在未来 |
| R9 | TTL 等号保守 | 成立 | 快照 `ticker.fresh_until=1789862429500`：`-1ms` 放行；**等号即拒**（`K02:QUOTE_EXPIRED`）；`+1ms` 同 |
| R10 | 真实 0 保留 `"0"` | 成立 | `genuine_zero`：`values={'mark_price':'2410.4','funding_rate':'0'} missing=()`，声明 `funding_rate` → 放行；对照 `patched_sources_missing`：`available=False values={} missing=('mark_price','funding_rate')` |

补充（不作为结论依据）：作者两个 integration 测试我独立复跑（`q13_tests.txt`）——
`test_research_evidence.py` **PASS 138 / FAIL 0**、`test_market_evidence_quality.py`
**PASS 46 / FAIL 0**，均 exit 0，与作者自述一致。r3 新增的两个测试（`test_unpinned_sentinel_cannot_be_laundered`、
`test_required_sources_policy_floor`）与接线常量测试（`test_orchestrator_wiring_policy`）
经审读**不是空转断言**：前两者打的是行为（含"对照组真实身份 pinned=True"与
"并集双向 + 畸形声明"），后者用 AST 求值接线常量字面量并断言补丁改变了源文件。

范围确认（`q18_manifest.txt`）：`5ffc6c0..HEAD` 只动 5 个路径
（`src/agents/research_evidence.py`、两个 integration 测试、接线补丁、known_limits.md），
**未碰**任何共享源文件（`orchestrator.py`/`market_query.py`/`models.py`）、契约层、
`run_offline_regression.py`。`scripts/run_offline_regression.py` 在本 head 仍 exit=1
（两个 `smoke_u03_*` 未登记，`_validate_smoke_manifest()` → `RuntimeError`）——
按派单说明属 **I 侧原子登记项**，known_limits#7 已如实登记，**不作为作者缺陷计**。

---

## 新发现（均为 P3；无 fail-open、无可达崩溃）

### Q-R3-1（P3，证据卫生）补丁 `index` 行后像哈希 `890c08f` 不可复现
位置：`tests/fixtures/evidence/u03_orchestrator_integration.patch:2`。
复现见第 5 项。我按前像逐 hunk 重放得 `8499377`（前像重放自身能精确复现 `7ac28fb`，
可证明方法保真），`git apply` + `hash-object` 得 `280e1ea`，均 ≠ `890c08f`。
影响：无功能影响（`git apply` 不校验该值），但 known_limits#8 的"可复现"声称不成立，
且错误的 index 后像会让 `git apply --3way` 因缺少后像 blob 而退化为直接应用
（实测：已应用状态下不会重复插入，仅报 `patch does not apply`）。
建议：把 index 后像改为真实值（或直接去掉该哈希），并把 known_limits#8 的措辞改为
"后像哈希不可复现、仅作占位"。

### Q-R3-2（P3，崩溃；r2 起既有、当前接线不可达）畸形 `identity` 参数炸整轮
位置：`src/agents/research_evidence.py:1353` `RuntimeIdentity.model_validate(dict(identity))`。
复现（`q1_identity_out.txt` 末段）：

```
identity='abc'          -> <CRASH ValueError: dictionary update sequence element #0 has length 1; 2 is required>
identity=123            -> <CRASH TypeError: 'int' object is not iterable>
identity=object()        -> <CRASH TypeError>
identity=等价 dict+额外键 -> <CRASH ValidationError: Extra inputs are not permitted>
identity=等价 dict 缺字段 -> <CRASH ValidationError: Field required>
```

反例要点：残缺/越界的 Mapping 会让**整次门禁调用**抛异常，而不是像 intent 那样结构化拒绝
（同一入口对 intents 有 `INTENT_MALFORMED` 兜底，对 `identity` 没有）。
可达性：当前接线传 `identity=None`，U15 计划传 `build_identity()` 的模型对象 ⇒ **不可达**；
但该参数是公开 API 的一部分且显式接受 `Mapping`，建议补兜底（异常 → `IDENTITY_UNPINNED` 或
`IDENTITY_MALFORMED` 结构化理由）并登记。

### Q-R3-3（P3）导出的 `is_unpinned_identity()` 传 Mapping 会 AttributeError
`__all__` 新增了 `is_unpinned_identity`（`research_evidence.py:1663`），而
`evaluate_open_intents` 的 `identity` 口径是 `RuntimeIdentity | Mapping | None`；
直接对着 Mapping 调用该公开函数 → `AttributeError: 'dict' object has no attribute 'environment'`
（我的 `q1_identity_out.txt` 第一段第 4 行即为该形态）。建议 `_as_mapping`-风格的守卫，
或在 docstring 明写"仅接受 RuntimeIdentity"。

### 观察 O-1（非缺陷，供 U15 参考）新缺省与 30s ticker TTL 的时序叠加
`default=("ticker",)` 下，快照观测时刻起约 29.5s 后开仓会新增一条 `SOURCE_STALE:ticker`
（`q16_ttl_undeclared.txt` E3）。但对照 E4（`default=()`）显示同一时刻**本来就会**因
`K02:QUOTE_EXPIRED` 被拒 ⇒ 该时序约束**不是 r3 引入**，r3 的"行为增量"实际只限于
**字段齐全性**（known_limits#9① 已登记该增量）。故仅作运营提示：U15 接线时需知
"查询→门禁"的延迟上限约为报价/源 TTL 量级，否则开仓会被时效类理由拒绝。

### 观察 O-2（非缺陷）known_limits 现在可核验
r2 复验判"登记无法核验"（当时 `docs/dispatch/reviews/U03/` 下只有 `3f49e03`）。
r3 已把 `docs/dispatch/reviews/U03/r3/known_limits.md`（76 行）随分支提交，
六条残余登记（Q-NEW-1 边界/2/3/6/7/8/9）逐条可对照。位置是轮次目录
`U03/r3/` 而非 head-commit 目录，作者备注"由 I 归档到 head 目录" ⇒ 归档动作在 I 侧。

---

## 命令清单

解释器统一为 `C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，
前置 `PYTHONUTF8=1`，cwd 为工作区根，全部输出重定向到 `_scratch_Q/`。

```bash
# 探针（只写 _scratch_Q/）
$PY _scratch_Q/probe_q1_identity.py        > _scratch_Q/q1_identity_out.txt      # 第 1 项（哨兵/绕过/鲁棒性）
$PY _scratch_Q/probe_q2_sources.py         > _scratch_Q/q2_sources_out.txt       # 第 2/4 项（缺省/并集/畸形）
$PY _scratch_Q/probe_q3_union_cycle.py     > _scratch_Q/q3_union_cycle_out.txt   # 第 3/4 项（并集判别式 + cycle_id）
$PY _scratch_Q/probe_q4_regression.py      > _scratch_Q/q12_regression_out.txt   # 回归 10 项
$PY _scratch_Q/probe_q5_ttl_undeclared.py  > _scratch_Q/q16_ttl_undeclared.txt   # 残余登记如实性 + TTL 叠加

# 补丁核验
git diff/--numstat 5ffc6c0..HEAD                                  > _scratch_Q/diff_*.txt
git apply --check -v / --stat / --numstat（三个补丁）+ blob 对照   > _scratch_Q/q4_patchcheck.txt
$PY _scratch_Q/apply_patch.py <patch> HEAD:src/agents/orchestrator.py > _scratch_Q/q6_postimage.txt
$PY -c "manual sha1 公式校验 pre/post"                             > _scratch_Q/q7_hashvalidate.txt
git hash-object _scratch_Q/postimage_rebuilt.txt                   # 84993770...
git init 副本 + git apply + git apply --3way                        > _scratch_Q/q8_3way.txt / q9_3way_fixed.txt
$PY -c "补丁 CR/EOL 统计"                                          > _scratch_Q/q10_crlf.txt
git config/CR 计数/blob 大小                                       > _scratch_Q/q11_eol.txt

# 静态交叉核验
grep required_sources / TradeIntent / DecisionEvidence 字段         > _scratch_Q/q14_crossref.txt
grep bindings() / 模块导出面                                        > _scratch_Q/q15_bindings.txt
grep identity_pinned 消费方 / environment 取值                      > _scratch_Q/q17_consumers.txt
$PY -c "_validate_smoke_manifest() + run_offline_regression"       > _scratch_Q/q18_manifest.txt

# 作者测试独立复跑（不作为结论依据）
$PY tests/integration/test_research_evidence.py                     > _scratch_Q/q13_tests.txt   # PASS 138/FAIL 0, exit 0
$PY tests/integration/test_market_evidence_quality.py               >> _scratch_Q/q13_tests.txt  # PASS 46/FAIL 0, exit 0
```

## 未覆盖项

1. **接线补丁未应用、未跑端到端**：只做 `git apply --check`（EXIT=0）+ 副本落盘哈希 + 静态审读
   （形参作用域、`bindings()` 键、action 归一、import 齐全）。运行期行为（`_research_evidence_gate`
   与风险层/`filter_unauthorized_opens` 的交互、`decision` 改写为 `no_trade` 后下游呈现、
   `research_evidence_blocked` critical 事件频率）无法离线验证，需 LLM/网络。
2. **Q-NEW-6（周期自证同义反复）未重做**：r2 已核验并在 known_limits#4 登记；其利用面
   （工具出参自身标错周期）需真实交易所出参才能判定，明说未验证。
3. **Q-NEW-9（`data_observed_at_ms` 精确相等）未重做**：r2 已核验、known_limits#6 已登记。
4. **r2 已 CLOSED 的 6 项不做全量重验**：本轮按派单只抽样 10 项；`smoke_contracts_v3.py`
   与两个 `smoke_u03_*.py` 未由我重跑（编排者已预核 PASS 173/0 等）。
5. **`run_offline_regression.py` 整套回归仍无法运行**（exit=1，两个 `smoke_u03_*` 未登记，
   属 I 侧原子登记项）；故"共享离线回归入口"的绿灯状态需 I 登记后才能确认。
6. **真实身份到位后的行为**（U15）：`is_unpinned_identity` 已保证哨兵不得被洗白，
   但真实身份内容真实性、`environment` 命名是否会出现 `"unpinned"` 之外的近义值，无法离线判定。
7. **30s ticker TTL 与真实工具出参的字段齐全率**：真实 `query_symbol_detail` 是否稳定提供
   `last_price/pct_24h/quote_volume_24h` 三字段无法离线验证；若缺失率高，`("ticker",)` 缺省会
   大面积拒开仓（fail-closed，方向安全）——U15 接线前建议用真实出参确认（O-1）。

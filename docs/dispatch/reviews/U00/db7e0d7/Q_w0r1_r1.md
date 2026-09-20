# U00 契约候选独立审查报告（Q / 初审）

- 会话：Q-w0r1（独立会话，初审；不替代 I 集成后的最终验收）
- 审查对象：候选提交 `db7e0d7`（含 `d8b2555`、`3272b98`；W0 证据提交 `8774126/1c695b1/212b296/fd5e8af/ea08edf` 亦在候选范围内）
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1`（detached @ db7e0d7）
- 审查日期：2026-09-20
- 只读约束：未修改任何 tracked 文件（`git status` 仅 `?? _review_scratch/`）；未进入运行目录、未读 `data/`、`.env`；未联网、未启动服务；未推送/合并/打标签
- 反例脚本（未跟踪）：`_review_scratch/adv_01_guard_decimal_range.py`、`adv_02_k04_k05_k06_k02.py`、`adv_03_misc.py`、`adv_04_harness_sensitivity.py`

## 0. 结论

**CHANGES_REQUESTED**

- P0：0 项
- P1：3 项（两个契约判定函数 fail-open + 一个 K03 schema 冻结面缺口）
- P2：9 项（含 1 项流程登记缺口）
- 候选主体质量良好：冒烟 69/0 通过，6 个 schema 与模型逐字节同步，golden 期望为手工推导（非实现回放），负例断言具体 reason code 而非笼统失败；P1 均为小改动可修（补校验/补 reason code、补 golden、补 schema 定义）。

## 1. 实测汇总（真实输出）

| 项目 | 命令 | 结果 |
|---|---|---|
| 契约冒烟 | `& <venv>\python.exe scripts\smoke_contracts_v3.py` | `结果：PASS 69 / FAIL 0；golden cases=56 (positive=26, negative=30)`；6 个 schema 同步断言全 PASS；退出码 0 |
| smoke 清单校验 | 导入 `scripts/run_offline_regression.py` 调 `_validate_smoke_manifest()` | `SMOKE MANIFEST OK included=65 excluded=1`；`smoke_contracts_v3.py` 已登记（`scripts/run_offline_regression.py:82`）；`smoke_p0c.py` 有明确排除理由（同文件 `:84-88`） |
| schema 独立重生成 | `model_json_schema()` ×6 与仓库文件 `==` | 6/6 `EQUAL`（独立于冒烟脚本节点复现） |
| 依赖锁校验 | `& <venv>\python.exe scripts\verify_dependency_lock.py` | `DEPENDENCY LOCK OK packages=50 sha256=a28db842...`，退出码 0（该脚本由 `check_quality.py` 调用，不在 offline_regression 内） |
| 离线纯度抽查 | grep `src/core/contracts/**` | 仅 stdlib + pydantic；无 socket/requests/os.environ/open() 等 IO/网络/凭据调用 |
| 候选范围 | `git diff --name-only 5295632..db7e0d7` + `git status` | 契约代码/文档/登记均在 U00 相关路径；无 tracked 脏改 |

反例增量观察（详见 §3）：

- `adv_01`：大小写/嵌套键（`APIKEY`、`Binance-Secret`、`a[0].accessToken`）均被拒；非 Mapping 载荷/秘密仅在 value 中会通过（README §12 已声明键名守卫）；`Decimal` 文本 scale 影响哈希；`[start,end)` 边界正确（start 含、end 不含）。
- `adv_02`：K04 授权对畸形/缺失数量 fail-open（P1-1）；K05 允许 UNKNOWN→ACKNOWLEDGED 跳级（P2-4）；K06 非数值窗口崩溃（P2-3）。
- `adv_03`：K05 恢复证据缺时间戳或字符串 `"false"` 仍 RESOLVED（P1-2）；`allocate_weighted` 负权重产生负分摊（P2-6）；冒烟 `compare()` 只做期望子集比较（P2-5）。
- `adv_04`：翻转 golden 期望（status/claim actual）→ 断言立即 FAIL；篡改 schema 字段 → 不等。证明现有断言有效、非空转。

## 2. P0/P1/P2 问题清单

### P0（0）

无。

### P1-1  K04 授权校验对畸形/缺失数量静默放行（fail-open）

- 位置：`src/core/contracts/k04_management.py:156-162`（`_optional_decimal(...) is not None` 才比对）、`:175-181`（`_optional_decimal` 吞掉 `NumericInputError/ValueError` 返回 None）
- 复现：

```powershell
$env:PYTHONUTF8=1
& "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe" _review_scratch\adv_02_k04_k05_k06_k02.py
# F.garbage_qty   -> {'allowed': True, 'reason_codes': []}   # reduce_qty="not-a-number"
# F.nan_qty       -> {'allowed': True, 'reason_codes': []}   # reduce_qty=float('nan')
# F.missing_qty   -> {'allowed': True, 'reason_codes': []}   # request 无 reduce_qty
# F.garbage_net_delta -> {'allowed': True, 'reason_codes': []} # requested_net_delta="huge-buy"
```

- 观察：`REDUCE` 请求携带不可解析/缺失的 `reduce_qty`、或不可解析的 `requested_net_delta` 时，均按“无值即不校验”放行；无任何 reason code。
- 期望依据：规格 §4 K04「仅允许 close/reduce 或批准范围内的保护维护」；管理凭证绑定 `max_reduce_qty`；README §6 声称拒绝 `REDUCE_EXCEEDS_AUTHORIZATION`/`INSUFFICIENT_POSITION`。畸形数量不应被当作缺失跳过。
- 建议：对非法数值新增显式 reason（如 `INVALID_REDUCE_QTY`/`INVALID_NET_DELTA`，小版本允许新增 reason_code），或要求请求先过 `ManagementIntent` 验证；golden 补对应 negative。

### P1-2  K05 恢复证据缺时间戳/类型混淆仍 RESOLVED（fail-open）

- 位置：`src/core/contracts/k05_trace.py:181-186`（非 Mapping 直接 `bool(entry)`；字符串 `"false"` 为真）、`:202-207`（`observed is None` 时完全跳过新鲜度检查）
- 复现：

```powershell
& <venv>\python.exe _review_scratch\adv_03_misc.py
# K.no_timestamp_true -> {'recovered': True, 'reasons': [], 'resolve': 'RESOLVED'}   # {"heartbeat": True}
# K.string_false      -> {'recovered': True, 'reasons': [], 'resolve': 'RESOLVED'}   # {"heartbeat": "false"}
# K.dict_no_value     -> {'recovered': True, 'reasons': [], 'resolve': 'RESOLVED'}   # {"heartbeat": {"note": "looks fine"}}
```

- 观察：谓词证据没有 `observed_at_ms` 时新鲜度校验被跳过；字符串 `"false"`/任意非空字符串按满足处理；含 note 的字典（无 value/satisfied）默认 satisfied=True。三者均可把 Incident 置 `RESOLVED` 并写入 `resolution_evidence_hash`。
- 期望依据：规格 §4 K05「RESOLVED 必须满足恢复证据」、README §7「只有在恢复证据齐全、新鲜（`max_age_ms`）且满足谓词时才置 `RESOLVED`」；注释亦声明“必须由新鲜证据满足，而不是缓存里的绿色状态”。
- 建议：Mapping 证据要求显式的布尔/数值字段与 `observed_at_ms`，否则记 `EVIDENCE_MISSING`/`EVIDENCE_STALE`/类型错误；golden 补“无时间戳”“字符串假值”负例。

### P1-3  K03 schema 缺失两个公开返回模型（冻结面不完整）

- 位置：`docs/contracts/k03_income_coverage.schema.json`（`$defs` 无 `ZeroFeeProof`、`PaginationDecision`）；模型定义 `src/core/contracts/k03_income.py:204-208, 245-252`；`K03SchemaEnvelope` 未纳入二者（`:597-610`）
- 复现（独立逐模型对照）：

```powershell
& <venv>\python.exe -c "<脚本见 §1 表；遍历模块内 BaseModel 与 `$defs` 差集>"
# k03_income_coverage.schema.json
#   models not in schema: ['K03SchemaEnvelope', 'PaginationDecision', 'ZeroFeeProof']
# 其余 5 个 schema 仅缺各自 Envelope（Envelope 即根 schema，正常）
```

- 观察：`zero_fee_proof()` 与 `plan_next_page()` 是 README §5 明确列出的契约接口，其返回模型（`ZeroFeeProof`/`PaginationDecision`）被 golden（K03-N1/N2、K03-P2/P3）断言字段，却没有进入冻结 schema；`__init__.py` 也未导出这两个类（仅导出函数）。
- 期望依据：任务卡 U00「契约：K01–K06（实现 schema/JSON 及可执行正反 golden）」；README §1 将 `docs/contracts/*.schema.json` 定义为冻结结构件；K05 的同类辅助模型 `RecoveryCheck` 已纳入 schema，K03 口径不一致。
- 建议：把两个模型加入 `K03SchemaEnvelope` 并重生成 schema（或明确登记排除理由并在 README 注明）。

### P2-1  K03 覆盖状态与缺口清单自相矛盾未被发现

- 位置：`src/core/contracts/k03_income.py:187-201`（`coverage_problems`）、`:211-242`（`zero_fee_proof`）
- 复现：`adv_01` → `C.contradiction.complete_with_gaps.problems -> []`、`.zerofee -> {'provable': True}`
- 观察：`coverage_status="COMPLETE"` 且 `gaps` 非空（`RangeGap(120,130)`）时，`coverage_problems` 返回空、零费用证明 `provable=True`；缺口被静默忽略。
- 期望依据：K03 规格「完整分页、时间范围覆盖」；缺口语义与 COMPLETE 互斥。
- 建议：`coverage_status=="COMPLETE" and gaps` 时报 `COVERAGE_GAP`（或新增矛盾码），并补 golden。

### P2-2  K03 Decimal scale 未规范化导致“同值异形”误判冲突

- 位置：`src/core/contracts/k03_income.py:85-97`（`income_content_hash` 用 `str(item.amount)`）；对比 `src/core/contracts/common.py:142-146`（`decimal_text` 会 `normalize()`）
- 复现：`adv_01` → `B.ingest.false_conflict_scale -> quarantined ['INCOME_ID_CONFLICT']`（existing `100` vs incoming `"100.00"`）；同样 `1E+2` vs `"100"` 冲突；仅逐字相同文本才是 duplicate。
- 观察：数值相等、文本 scale 不同即被判“同键异内容”进入隔离，可能造成合法重复事件误隔离累计。
- 期望依据：通用约定「边界以十进制字符串序列化」；`decimal_text` 提供规范化能力但未用于内容指纹。
- 建议：`income_content_hash` 改用 `decimal_text(item.amount)`，或明确接受 scale 敏感并在 README 声明（属语义变更需走版本规则）。

### P2-3  K06 窗口字段类型错误直接崩溃；声明状态未去除空白

- 位置：`src/core/contracts/k06_release.py:115`（`int(window_end)`）、`:79`（`str(...).upper()` 不 strip）
- 复现：`adv_03` → `J.k06_str_window -> raised ValueError: invalid literal for int(): 'not-a-number'`；`adv_02` → `" PASS "` 得 `DECLARED_STATUS_UNKNOWN`+`NO_DATA`
- 观察：畸形证据导致整份报告构建抛异常（fail-closed 但非结构化阻断项）；带空白的合法状态被误判为未知。
- 期望依据：规格 §4 K06「缺证据必须列出具体阻断项」。
- 建议：窗口字段解析失败 → `FAIL`+`WINDOW_INVALID`（或新增码）；`status` 先 `strip()`。

### P2-4  K05 `transition_state` 允许 UNKNOWN→ACKNOWLEDGED 跳级

- 位置：`src/core/contracts/k05_trace.py:300-322`（`:311` 对 ACKNOWLEDGED 豁免回退检查，且不校验前置事实）
- 复现：`adv_02` → `E.unknown_to_ack -> {'state': 'ACKNOWLEDGED', ..., 'violations': ['ACK_WITHOUT_SUBMISSION']}`
- 观察：转换本身成功；随后 `delivery_violations` 仍会报违规，但阶梯约束在转换处不生效，与 docstring「按阶梯提升状态」不一致。
- 建议：明确 ACK 是否允许无 SUBMITTED 前件（若允许，在 README 明示；若不允许，转换即拒绝）。

### P2-5  冒烟 `compare()` 只做期望子集比较

- 位置：`scripts/smoke_contracts_v3.py:502-523`
- 复现：`adv_03` → `L.compare.extra_in_actual -> []`（actual 多出 `reason_codes:["HACK"]`、`hidden:1` 不报错）
- 观察：实现若新增字段不会被冒烟发现；部分 op 输出有意裁剪（如 `op_k03_ingest` 未断言 quarantine 的两个 content_hash）。本身不构成错误，但冻结后字段漂移可能漏检。
- 建议（可选）：对关键结构加“键集合相等”断言，或在 README 声明现有为子集断言。

### P2-6  `allocate_weighted` 允许负权重产生负分摊

- 位置：`src/core/contracts/k03_income.py:447-459`
- 复现：`adv_03` → `I.negative_weight -> [('a','-50.00000000'), ('b','150.00000000')]`（Σ=100 仍守恒）
- 观察：权重单个为负时产生负的分摊金额，守恒校验通不过事实约束（守恒只查总和）。
- 建议：权重非负校验；golden 补 negative。

### P2-7  幂等键大小写/空白敏感（观察项，建议在 README 明示语义）

- 位置：`src/core/contracts/k03_income.py:75-82`、`src/core/contracts/k04_management.py:69-77`
- 复现：`adv_01` → `D.ingest.space_variant -> {'inserted': 1, 'duplicates': 1}`（`"T1"` 与 `" T1"` 视为两条）；`D.mgmt.case -> ['L|SL|v1|E', 'L|sl|v1|E']`
- 观察：不做 trim/大小写归一；`tran_id`/`trigger_type` 若上游清洗不一致将重复入账或重复管理。
- 依据/建议：规格未承诺归一化（交易所 ID 语义为不透明原始值）。建议 README 明确“原样语义，清洗归适配层”，由 U01/U04 在适配器统一。

### P2-8  敏感守卫残余面（README 已声明键名守卫）

- 位置：`src/core/contracts/common.py:63-89`、`src/core/contracts/k01_identity.py:83-87`
- 复现：`adv_01` → 全角 `ｓｅｃｒｅｔ` 键、`{"endpoint":..., "value":"sk-live-..."}`（秘密仅在 value）、`hash_strategy("BINANCE_API_KEY=...")`（字符串载荷）均可哈希成功。
- 观察：守卫只查键名、不查内容；非 Mapping 载荷（含 `.env` 原文）不被扫描。
- 依据：README §12 已声明“保守子串匹配、需 U15 白名单适配”。属已知限制，建议 U15 集成时把“哈希输入必须来自显式白名单键集”纳入适配验收。

### P2-9（流程）新脚本 `scripts/smoke_contracts_v3.py` 未在任何登记中

- 位置：`docs/无人值守升级任务清单_2026-09-19.json` U00 `owned_paths`/`shared_files`（全文无 `smoke_contracts_v3`）
- 观察：U00 独占路径仅 `docs/contracts/`、`scripts/run_offline_regression.py`、`src/core/contracts/`；新冒烟脚本落在独占路径之外也未列入共享文件，与 R17「所有权精确到路径」及任务卡独占路径清单不一致（文件本身必要且已登记进回归入口）。
- 建议：I 更新 JSON 将 `scripts/smoke_contracts_v3.py` 纳入 U00 owned_paths 或 shared_files 登记。

## 3. 对抗检查明细（与任务清单逐项对应）

1. **反例增量（≥3，全部独立运行）**：见 P1/P2 及 `adv_01/02/03`。关键反例：
   - 守卫：`APIKEY`/`Binance-Secret`/`passwd`/`a[0].accessToken` 全部被拒 → 守卫基本有效；残余面见 P2-8。
   - Decimal round-trip：`Decimal("1.0")` vs `Decimal("1")` 哈希不等；`ingest` 同值异形 → 误隔离（P2-2）；`decimal_text` 对 `0.00000000`/`1E+2`/`1.10` 归一正确（`'0'/'100'/'1.1'`，`-0` 输出 `'-0'`）。
   - `[start,end)`：`start` 含、`end` 不含、跨点正确；`start==end` → `INVALID_RANGE`；`watermark==end` 正常计覆盖。
   - 幂等键：大小写/空白差异产生不同键（P2-7）。
   - delivery 非法跃迁：`SUBMITTED→ACCEPTED` 无回执、`UNKNOWN→DELIVERED`、`ACKNOWLEDGED→SUBMITTED` 均抛 `TransitionError`；`UNKNOWN→ACKNOWLEDGED` 放行（P2-4）；时间倒序 → `TIME_ORDER_VIOLATION`。
2. **断言有效性**：负例全部断言具体 reason code（K02-N1 `CLAIM_FALSE`、K05-N2 `STATE_MISMATCH/DELIVERED_WITHOUT_DEVICE_RECEIPT/PROVIDER_RECEIPT_MISSING`、K06-N4 `IDENTITY_MISMATCH` 等），非笼统 False；`derivation` 为手工推导（抽查 K02-P1、K03-P2/P7、K04-P2、K05-P2/P4 与规格条款/公式一致，无实现回放）。`adv_04` 证明翻转期望/篡改 schema 会正确失败。子集断言弱点见 P2-5。
3. **schema 同步**：`model_json_schema()` 重生成 6/6 `EQUAL`；K03 缺 2 个模型（P1-3）。
4. **登记完整性**：manifest 校验通过（included=65/excluded=1）；offline_acceptance 逐条证据见 §4；依赖锁校验实测通过；DL/RV/DC 登记齐全（`increment_registration`、`dc_test_cases` REGISTERED by Q-w0r1、F25 扩展）。
5. **契约与规格逐字段对照（K01–K06）**：字段名/枚举/语义核对结果——K01 十字段、K02 twelve 字段与 `inputs/derived_facts` 语义、K03 三类模型与三状态枚举、K04 十一字段+三分量等式、K05 Trace/Incident/AlertDelivery 阶梯、K06 五组+PASS/FAIL/NO_DATA 均与规格一致；新增字段均有 README 说明（K04 `trigger_type/trigger_event_id` 为幂等键所需、K02 `now_ms` 为可选参数）；未发现未说明的改名/缺失。`ReleaseEvidenceReport` 无整体 GO 字段，符合“不建第二套 GO 判断器”。

## 4. U00 offline_acceptance 逐条证据映射

| # | 验收条目 | 证据 | 判定 |
|---|---|---|---|
| 1 | 现有与新增 smoke 均登记或有排除理由 | `SMOKE MANIFEST OK included=65 excluded=1`；新冒烟登记于 `run_offline_regression.py:82`；排除理由注释 `:84-88` | 通过（登记缺口见 P2-9） |
| 2 | 实际基线、契约版本、共享文件负责人已登记 | `baseline_commit_observed=05d28e0`、`planning_commit=5295632`（JSON+git log 一致）；`SCHEMA_VERSION/README=3.0`；`shared_file_owner=I` | 通过（`contract_commit` 依 `artifact_stage_policy` 允许候选阶段为 null） |
| 3 | DL/RV/DC 增量与 review/lessons 范围登记且不改风险档 | `increment_registration`（DL01–DL10、RV01–03→U01、DC01–16 REGISTERED by I from Q-w0r1）；U01 `owned_paths` 含 review/calibration/lessons；F25 扩展 | 通过 |
| 4 | 运行根/数据根/进程/锁路径只读盘点；集成与运行根隔离 | `docs/dispatch/w0_runtime_inventory.md`（PID 12984/25928、WAL/锁文件、隔离结论）；本审查在 mat-wt worktree 进行 | 通过 |
| 5 | K01–K06 实际 schema、正反 golden 及契约测试通过；占位不得标 FROZEN | 6 schema 与模型同步 EQUAL；56 golden（26/30）；冒烟 69/0；README 仅标“FROZEN 候选（待审查）”，JSON 状态仍 PLANNED | 通过（schema 面缺陷见 P1-3） |
| 6 | 编排器/模型/并发/权限可用；禁网+依赖锁校验；基线→W0→契约顺序无循环 | U00 `orchestrator_check` 证据（Task(general) 可用、2 路并行、同模型偏差声明）；`verify_dependency_lock.py` OK；契约包无 IO/网络导入；`git log --graph` 顺序 05d28e0→5295632→W0 证据→契约提交 | 通过 |

## 5. 未覆盖项（golden/冒烟维度，供冻结前取舍）

- K01：整组缺失（`coverage={}` → `group.*`）与组非 Mapping 的行为正确但无 golden 用例。
- K02：DL02 明列的 `78550<78443` 同类反例未收入（`744.13>744.9` 已覆盖）；“伪造高周期/错时间”无专门用例。
- K03：`plan_next_page` 的 `INVALID_PAGE_LIMIT`；`AMBIGUOUS_LIFECYCLE`（多生命周期歧义，仅覆盖双向仓歧义）；`check_conservation` 的 tolerance 路径。
- K04：`ACTION_NOT_MANAGEMENT`；畸形/缺失 `reduce_qty`（与 P1-1 同修）；`OrderQuantity` 没有“accepted revision”更新 API/用例（订单改量仅靠调用方重建 frozen 模型），建议在 README/U04 明确责任边界。
- K05：`EVIDENCE_FROM_FUTURE`；`span_violations`/`duration_ms` 完全未被 golden 触达；`attempt=0` 校验。
- K06：`DECLARED_STATUS_UNKNOWN`（声明非法状态）无 golden；`evaluate_group` 对非数值窗口的崩溃（P2-3）。

## 6. 同模型偏差声明

本次 Q 审查与编排者（orchestrator/I）运行在同一底层模型（deepseek-v4.1-flash）上，属同模型会话隔离而非跨模型独立审计；可能存在共同的盲区与语料偏差。本报告不构成独立第三方交叉审计，PASS/FAIL 判定应由 I 在集成后另行复核。

## 7. 复现命令清单

```powershell
$env:PYTHONUTF8=1
$py = "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe"
git -C "C:\Users\Tao\Desktop\币安\mat-wt\reviews\U00\Q\w0r1" checkout db7e0d7
& $py scripts\smoke_contracts_v3.py
& $py _review_scratch\adv_01_guard_decimal_range.py
& $py _review_scratch\adv_02_k04_k05_k06_k02.py
& $py _review_scratch\adv_03_misc.py
& $py _review_scratch\adv_04_harness_sensitivity.py
& $py scripts\verify_dependency_lock.py
& $py -c "import importlib.util; s=importlib.util.spec_from_file_location('r','scripts/run_offline_regression.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m._validate_smoke_manifest()"
```

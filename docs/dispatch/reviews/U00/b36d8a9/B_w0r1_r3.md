# U00 契约候选 r3 复审报告 — B（持仓与执行 / 账本语义）

- 审查人：B 子代理（deepseek-v4.1-flash，全新会话）
- 工作区：`mat-wt/reviews/U00/B/w0r1` @ `b36d8a9`（`fix(contracts): close r2 P0/P1 findings, update gap index and golden changelog (U00 r3; I recovery commit)`）
- 复审对象：`git diff 0b85bef..b36d8a9 -- src/core/contracts docs/contracts scripts/smoke_contracts_v3.py`（作者会话被取消，I 恢复提交；本次按"审内容"执行）
- 方法：只读静态审查 + `_review_scratch/` 纯内存探针（`r3_probe.py`、`r3_envkey.py`、`r3_cmp_golden.py`）；未改任何 tracked 文件；未读运行目录 `multi-agent-trader/`、`data/`、`.env`；未联网；未启动服务；未执行 git 写操作
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，`PYTHONUTF8=1`
- 前置报告：`docs/dispatch/reviews/U00/0b85bef/B_w0r1_r2.md`（本报告逐条判定其结论）

## 0. smoke 基线（真实输出）

```
$env:PYTHONUTF8=1
& <venv-python> scripts\smoke_contracts_v3.py
结果：PASS 160 / FAIL 0；golden cases=147 (positive=44, negative=103)   # exit 0
```

完整输出：`_review_scratch/r3_smoke_out.txt`（160 PASS 行 / 0 FAIL）。smoke 与 golden 同提交、属"自证"，本报告判定主要来自独立探针（§5、附录 A）。
计数自洽：r2 报告 113 = db7e0d7(56) + r2 新增 57；r3 新增 34 → 147；README §1 的 147（44/103）一致。

## 1. 结论

**CHANGES_REQUESTED（单一 P2 残余，且未登记）**。

- r2 的新 P1「运行代/环境未绑定→跨代跨环境重放」**已强制 fail-closed（CLOSED）**：`None` 绑定即拒 `RUN_GENERATION_UNBOUND/ENVIRONMENT_UNBOUND`（`k04_management.py:254-261`）；跨代/跨环境 → `*_MISMATCH`；golden K04-N25/N26/N15/N27 覆盖。
- r2 两个 PARTIAL：**运行代绑定可选 → CLOSED**；**fill 对账 setter → 仍 PARTIAL，但已登记**（README L6 + B-O2，属 U04 下游面）。
- r2 P2 关键项：**全部 CLOSED**（FILLED 终态/撤单确认、空串 fill_id、submit_allowed/revision_id 兼容、environment 域幂等、COMPLETE+gaps 归因、零费用结构化证据）；随带 N8/N9/N10 亦关闭或登记（N9→L15）。
- golden changelog：**4 条被改期望逐条登记（用例号/前后/依据齐全）；"既有用例不得修改期望值"规则已恢复**（README:222-224）；r3 仅 2 处 expect 变更（K01-N3、K03-N1），均已登记。
- README §12：**B 的 8 项遗漏（B-O1..B-O8）全部补齐并映射到 U04/U02/U01**。
- 新问题：**P2×1、P3×4**，无新 P0/P1。唯一 P2 是 r3 修复留下的输入边界漏洞：**空串绑定** `runtime_instance_id=""`/`environment=""` 使 UNBOUND 检查落空 → 未携带任何请求绑定的合法 REDUCE 被 `allowed=true`（探针实测）。
- 处置建议：在 `ManagementAuthorization` 对两个绑定字段按 strip 后非空校验（或 `""` 归一化为 `None`）；同时 `ManagementIntent.environment=""` 一并收紧。做上述任一修复（或把空串绑定残余写入 README §12），本报告可转 PASS-with-notes；其余无阻塞项。

## 2. r2 逐条判定

判定依据：`_review_scratch/r3_probe_out.txt`（48 OK / 1 unexpected，唯一 unexpected 即新 P2）；`r3_cmp_golden_out.txt`（r3 golden 逐用例比对）；`r3_cmp_golden_r2.txt`（r2 逐用例比对）。

### 2.1 新 P1（N1）与两个 PARTIAL

| r2 项 | r3 判定 | 证据 |
|---|---|---|
| **N1（P1）** 运行代/环境为可选 → 未绑定即放行 | **CLOSED**（主路径） | `authorization.runtime_instance_id is None → RUN_GENERATION_UNBOUND`、`environment is None → ENVIRONMENT_UNBOUND`，`allowed = not reasons`（`k04_management.py:254-261`、:329）；探针：未绑定 `allowed=False`；请求缺 bound 值→`*_MISMATCH`；跨环境→`ENVIRONMENT_MISMATCH`；golden K04-N25/N26（README §11 映射）；残余空串见 §6-N12 |
| P1-3 fill 对账 setter（PARTIAL） | **PARTIAL（未变，已登记）** | `k04_management.py` 无交易所权威累计 setter/只向上修正审计；调用方自维护 `applied_fill_ids`；README L6、§12 B-O2 登记为 U04 下游 |
| P1-5 运行代绑定可选（PARTIAL） | **CLOSED** | 同 N1；README §6「运行代/环境绑定为强制」与实现一致；K04-P4/P5/N15 全部数据已绑 |

### 2.2 P2 关键项

| r2 项 | r3 判定 | 证据 |
|---|---|---|
| **N2** FILLED 不在终态 → 可"撤单确认" | **CLOSED** | `request_cancel` 拒 `CANCEL_FORBIDDEN_STATUSES = TERMINAL ∪ {FILLED}`（:478-481）；`confirm_cancel` 显式拒 FILLED（:490-491）；新增违规 `FILLED_WITH_CANCEL_CONFIRMED`（:400-401、:67）；探针：FILLED request/confirm 均 `QuantityError`，全成交后 request 亦拒；golden K04-N22/N23 |
| **N3** 空串 fill_id 绕过幂等 | **CLOSED** | `if fill_id is not None` + 非空字符串校验（:441-445）；探针：`""`/`"   "`/`"\t"`/非 str 全部 `QuantityError`；真重复→`DuplicateFillError`；golden K04-N24 |
| **N4** submit_allowed/revision_id 新必填 | **CLOSED** | `submit_allowed` 改 `@computed_field`（:202-206）：3.0 形态 `{authorization_id, allowed, reason_codes}` 可直接 validate，dump 含计算值；显式传 `submit_allowed=` 因 `extra=forbid` 报 `ValidationError`（单来源派生，已在 README §10 登记为 3.1 变更）；`OutcomeRevision.revision_id` 默认 `""`（k03:682）；探针通过 |
| **N5** environment 域 id 两条派生路径互斥 | **CLOSED** | `ManagementIntent.environment` + validator 接受四组件/环境域两种派生（:101-119）；`derive_management_id(env)` 三域 id 互异；`" Demo "` 归一化；错配 env id → `ValidationError`；golden K04-P9；探针通过。残余（四组件 id + env 字段并存）已登记 B-O4 |
| **N6** COMPLETE+gaps 仍 ATTRIBUTED | **CLOSED** | `attribute_income` gaps 非空 → `UNKNOWN`+`COVERAGE_GAPS_DECLARED`（k03:427-428）；探针通过；golden K03-N14；与 `zero_fee_proof`/`finalize_costs` 三方一致 |
| **N7** 零费用自由文本可证明 | **CLOSED** | 新增 `TerminationProof`（k03:197-206）；`zero_fee_proof` 仅认结构化 proof，自由文本→`TERMINATION_PROOF_MISSING`，窗口不符→`_MISMATCH`，整页/早于窗口末→`_INVALID`（k03:285-298）；`finalize_costs` 只接受 `ZeroFeeProof` 对象，自报布尔 `True` → `INCOMPLETE`（k03:593、:603-608、:642-648）；探针 6 项全过；golden K03-N15/N18/N19 |

### 2.3 其余 r2 项（含 P3）

| r2 项 | r3 判定 | 证据 |
|---|---|---|
| **N8** 带符号零指纹分歧 | **CLOSED** | `decimal_text`/`_json_default` 零值统一 `"0"`（`common.py:43-46`、:181-182）；探针：`decimal_text(-0)=="0"`、`-0` 与 `0` 哈希相等；golden K03-N17 |
| **N9** `coverage_problems` 双语义 | **PARTIAL（改善并登记）** | PENDING 现报 `COVERAGE_NOT_COMPLETE`（k03:240-241），与 `zero_fee_proof` 对齐；watermark 命名仍两套 → README **L15** 登记（U01 可收敛为单一判定源） |
| **N10** `reconcile_allowed` 恒 True | **CLOSED** | 改为 `account_ok and lifecycle_ok and side_ok`（:333）；过期授权仍 `reconcile_allowed=true`（规格 §10.2 语义保留）；golden K04-N27；探针通过。文档尾注残留见 §6-N13 |
| **N11** 冻结纪律（改期望未登记/规则被删） | **CLOSED** | 4 条 r2 期望变更逐条登记（README:266-269）；规则原句恢复（README:222-224）；r3 全部 expect 变更仅 K01-N3/K03-N1 且均登记（§3） |
| P1-14 `account_ref` 与轮换解耦 | **PARTIAL（未变，已登记）** | 仍为 L9（U15），r3 未改，属已知限制 |

## 3. golden changelog 与冻结纪律核对

**4 条被改期望逐条登记（含用例号/前后/规格依据）— 全部通过**：

| 用例 | changelog 登记 | 实测（db7e0d7→0b85bef） | 判定 |
|---|---|---|---|
| K03-P1/P6（r2） | `SYNTH_ACCT/FUNDING_FEE` → 小写；键归一化（B P1-11/Q P2-7） | expect 键文本确为大小写变更（`demo|synth_acct|funding_fee|t-001`） | 登记准确 |
| K04-N6（r2） | expect 追加 `INSUFFICIENT_POSITION`；新增必填 `current_position_qty` 双重上限（B P0-1） | 前后一致 | 登记准确 |
| K04-P2（r2） | `revision` `"5"` → `5`；修复有损导出（B P1-2） | 前后一致 | 登记准确 |
| K05-N4（r2） | expect 追加 `EVIDENCE_PREDATES_ACK`；ACK 后须重新观察（C P1-1/Q NEW-P2-1） | 前后一致 | 登记准确 |

**r3 期望变更全集核对**：逐用例比对 `0b85bef..b36d8a9` 六个 golden，expect 变更 **仅 2 条**（K01-N3 组级→字段级缺口、K03-N1 `PAGINATION_NOT_TERMINATED`→`TERMINATION_PROOF_MISSING`），changelog 第 5/6 行逐条登记；无静默改期望。新增用例 34 条（K01-N5/P7、K02-N20..N30/P6/P7、K03-N14..N19、K04-N22..N27/P9、K05-N11..N15、K06-N7）全部在 §11 映射表，且 §12「已修（r3）」逐条复述修复语义。**规则恢复**：README:222-224 明确"既有用例不得修改期望值…须在 §11 changelog 逐条登记"。

**登记瑕疵（P3，见 §6-N14）**：changelog 表头声明登记"期望/输入变更"，但 inputs-only 变更漏登 4 例：K01-P3/P4/P5（model_route 增 `fallback_routes/profile_overrides`、effective_config 增显式空覆盖）、K03-N5（补 `termination_proof`）。K03 行写"P4/P5/P8/N2/N3/N6/N7"、实际输入变更为同 7 例 + K03-N5；K04 行以"全部非 OPEN 授权用例"合并覆盖 16 例（我已逐例核对 21 条 K04 授权用例，除新增 N25 外全部已加绑定，声明成立）。

## 4. README §12 中 U01/U02/U04 的 8 项遗漏核对

r2 报告 §4 的 O1–O8 **全部补齐**（README:326-333，按任务分列，并注明 r3 状态）：

| r2 遗漏 | r3 索引 | 补齐判定 |
|---|---|---|
| O1 FILLED 撤单语义 | B-O1（r3 已收紧 request/confirm + 违规码；剩余交易所终态对账→U04） | ✔ |
| O2 fill 来源/幂等集合/无 setter | B-O2（user stream/REST、调用方维护、接口不回带→U04） | ✔ |
| O3 运行代/环境可选绑定 | B-O3（r3 已强制校验；剩余 mint 持久化与实例锁→U02/U15） | ✔ |
| O4 environment 域意图 id 约定 | B-O4（r3 已支持两种派生→U02） | ✔ |
| O5 零费用证据窗口/synced_at 绑定 | B-O5（r3 已结构化；剩余持久化/新鲜度→U01） | ✔ |
| O6 COMPLETE+gaps 归因 | B-O6（r3 已修；剩余 income_type/asset 决策域→U01） | ✔ |
| O7 改量（amend）语义 | B-O7（accepted revision、client_amend_id→U04） | ✔ |
| O8 授权→意图→命令（Outbox）关联 | B-O8（关联 ID 与重启只读恢复→U02/U04） | ✔ |

附带：新增 L13–L15 承载 r2 C 域与 K03 双语义残余；C 的 8 项遗漏（C#5/6/8/10/11/12/13/16）与 C r2 §4 清单一致（已对照 C 报告）；C 两处"部分覆盖"（per-action claim 策略、采集 attempts/降级原因）未单列，由"其余接口缺口…由 I 登记"承接（P3 观察，非 B 域阻塞）。

## 5. 新对抗尝试（≥2，均独立于 golden）

**对抗 1 — K04 状态机（超越 K04-N22..N27）**：FILLED 撤单/确认双拒；NEW→fill→FILLED 后再 `request_cancel` 拒绝；PARTIALLY_FILLED 撤单确认后 `terminal_unfilled=余量`、完好；`confirm_cancel(status=REJECTED)` 对有成交订单抛 `REJECTED_WITH_ACCEPTED_QTY`；CANCELED 超量迟到 fill 拒绝；手工构造 `REJECTED+cancel_requested` 可被 confirm 置旗（API 路径不可达，§6-N16）；CANCELED 迟到 fill 填满后状态仍 CANCELED、无违规（规格明示"终态迟到 fill 只修订事实"，§6-N16 备注）。

**对抗 2 — 幂等键环境域（`r3_envkey_out.txt`）**：`demo/live/plain` 三键互异；`" Demo "`≡`"demo"`（strip+casefold）；`env=""` 是独立域（与四组件键不同）；构造 `trigger_event_id="e|env=x"`、`environment="x|"`、`"e|x"` 等碰撞尝试全部互异（`join_key_parts` 转义生效，10 例 8 唯一仅因大小写归一，符合预期）。结论：键域无碰撞；但 `env=""` 的"域"无绑定语义（→N12）。

## 6. 新发现（r3）

### N12（P2，未登记）空串绑定使 UNBOUND 检查落空

- 位置：`k04_management.py:190-191`（`environment/runtime_instance_id` 为 `str | None`，无 min_length）、:254-261（仅 `is None` 触发 UNBOUND）。
- 复现（`r3_probe_out.txt`）：`ManagementAuthorization(runtime_instance_id="", environment="")` + 未携带这两个键的合法 REDUCE 请求 → `allowed=True, reason_codes=()`（`[!! ] EMPTY-STRING binding must be rejected (fail-closed)`）。等价地，空串绑定授权可跨进程代、跨环境重放，README §6"绑定缺失即拒绝"的实际保证只在 `None`/非空不符时成立。
- 对照：探针中 `" "` 因请求侧取 `""` 对比而报 `*_MISMATCH`，说明只有精确 `""` 绕过；`ManagementIntent(environment="")` 也被 validator 接受（同源边界）。
- 影响：U02 mint 若把未配置项（如 env 变量默认 `""`）写入绑定，fail-closed 保证静默失效；属"畸形输入必须拒绝"原则的缺口。定为 P2（需 mint 侧误传才可利用，但属低成本高后果）。
- 建议（任一）：① `ManagementAuthorization` 增加"strip 后非空"校验（空/空白→`ValidationError`）或 `""` 归一为 `None`；② `ManagementIntent.environment` 同样收紧；③ 若暂不修，写入 README §12 L7/B-O3 并标注重放边界。建议 ①+②。

### N13（P3）`validate_authorization` 文档字符串与实现矛盾

- `k04_management.py:234-235` 仍写"``reconcile_allowed`` 恒为 True：授权过期只限制新提交…"，与 :333 的 `account_ok and lifecycle_ok and side_ok` 及 README §6 相矛盾（r2 N10 修复后未同步）。建议删除该句或改为"仅身份域匹配时为真"。

### N14（P3）changelog 漏登 4 例 inputs-only 变更

- 见 §3 末尾：K01-P3/P4/P5、K03-N5。修复建议：changelog 增补一行"K01 全部 identity 用例（r3）model_route/effective_config 补显式空字段"与"K03-N5（r3）补 termination_proof"。（expect 未变，不影响语义，仅登记完整性。）

### N15（P3）`ManagementIntent` 不强制环境域

- 带 `environment="demo"` 的意图仍接受四组件派生的 `management_id`（:112-119，为 3.0 兼容有意为之）。若 U02 统一用四组件派生，则 demo/live 同生命周期意图仍共用 id；已登记 B-O4，此处仅确认残余与测试覆盖（K04-P9 只正测环境域 id，未负测"env 字段 + 四组件 id"组合）。

### N16（P3）状态机残余（备注，非阻塞）

- CANCELED/EXPIRED 订单迟到 fill 填满后状态不变、`violations=()` —— 与 README §6"终态迟到 fill 只修订事实"一致，但"已全成交却显示 CANCELED"仍可能误导对账；B-O1 已把"与交易所终态对账"登记 U04。
- 手工构造 `REJECTED`（或 EXPIRED）+ `cancel_requested=True` 可被 `confirm_cancel` 置 `cancel_confirmed=True`（API 路径不可达，因 `request_cancel` 拒绝终态）；L11（构造器不内建校验）已覆盖。

## 7. 正面确认（不计缺口）

- K04：绑定强制、FILLED 不可撤、空串 fill_id、意图 env 双派生、`submit_allowed` 单来源、`reconcile_allowed` 身份域限定，正反 golden 齐（K04-P4/P5/P9、N15、N22..N27）。
- K03：TerminationProof 窗口/短页/时间三重校验、`finalize_costs` 只认对象、gaps 归因 fail-closed、signed zero、`coverage_problems(PENDING)` 对齐、`page_limit` 非 int/布尔结构化拒绝（K03-N14..N19）。
- README：143-163（§6 K04 语义）与实现逐句一致；§10 小版本规则与"不得改期望"纪律恢复；§11 映射含全部 34 条新用例。
- smoke 160/0；K01/K02/K05/K06 的 r3 变更经冒烟与索引核对未见与 B 判定冲突。

## 8. 同模型偏差声明与审查边界

- **同模型偏差**：本报告作者与候选实现/编排者同为 `deepseek-v4.1-flash`（U00 evidence 已登记）。① 本报告不构成独立交叉审计；② 对 pydantic 惯用法（computed_field、`str|None` 边界）与作者偏好可能共享盲区；③ smoke/golden 同源，本报告结论来自独立探针；④ r3 的 K02/K05/K06 大改动非我域，仅按索引需要抽查（未逐行核验）。
- **边界**：未读运行目录 `multi-agent-trader/`、`data/`、`.env`；未联网；未启动服务；未改任何 tracked 文件（HEAD=`b36d8a9`，`git status --short` 仅 `?? U00_review_B_w0r1.md`、`?? U00_review_B_w0r1_r2.md`、`?? _review_scratch/`）；未执行 git 提交/推送/标签；未在真实 Binance 语义下验证分页排序与边界包含性。

## 附录 A 复现清单（全部在 `_review_scratch/`，纯内存/只读）

| 文件 | 内容 |
|---|---|
| `r3_smoke_out.txt` | r3 smoke 真实输出（PASS 160 / FAIL 0；147 golden，exit 0） |
| `r3_probe.py` / `r3_probe_out.txt` | 全量探针（48 OK / 1 unexpected = N12），覆盖 r2 N1..N11 复跑与两组新对抗 |
| `r3_envkey.py` / `r3_envkey_out.txt` | 幂等键环境域碰撞/归一化 10 例 |
| `r3_cmp_golden.py` / `r3_cmp_golden_out.txt` | 0b85bef→b36d8a9 逐用例比对（expect 变更 2、inputs 变更 29、新增 34、删除 0） |
| `r3_cmp_golden_r2.txt` | db7e0d7→0b85bef 逐用例比对（expect 变更 5 例=4 项，验证 changelog 4 条） |
| `r3_k04_new_cases.txt`、`r3_show_case.py`、`r3_show_out.txt` | K04-N22..N27/P9、K03-N14..N19、K04-N15 等用例原文 |
| `r3_audit_k04_auth.py` / `r3_audit_k04_auth_out.txt` | 21 条 K04 授权用例绑定字段覆盖审计 |
| `r3_k02n16_out.txt` | K02-N16 输入差异核验（`kind:"other_kind"` 移除） |
| `r3_c_r2_s4.txt` | C r2 §4 八项原文摘录（用于索引对照） |

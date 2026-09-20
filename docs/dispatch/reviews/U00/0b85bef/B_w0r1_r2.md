# U00 契约候选 r2 复审报告 — B（持仓与执行 / 账本语义）

- 审查人：B 子代理（deepseek-v4.1-flash）
- 工作区：`mat-wt/reviews/U00/B/w0r1` @ `0b85bef`（`fix(contracts): close P0/P1 review findings, add regressions (U00 r2)`，基线 `db7e0d7`）
- 复审对象：`git diff db7e0d7..0b85bef -- src/core/contracts docs/contracts scripts/smoke_contracts_v3.py`；重点 K04/K03/K01/README §10–§12
- 方法：只读静态审查 + `_review_scratch/` 纯内存探针；未改任何 tracked 文件（`git status --short` 仅 `?? U00_review_B_w0r1.md`、`?? _review_scratch/`）；未读运行目录 `multi-agent-trader/`、`data/`、`.env`；未联网；未启动服务
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，`PYTHONUTF8=1`

## 0. smoke 基线（真实输出）

```
$env:PYTHONUTF8=1
& <venv-python> scripts\smoke_contracts_v3.py
结果：PASS 126 / FAIL 0；golden cases=113 (positive=40, negative=73)   # exit 0
```

完整输出：`_review_scratch/r2_smoke_out.txt`。smoke/golden 与候选同一提交，属"自证"；本报告结论来自独立探针（附录 A）。

## 1. 结论

- **r1 P0-1（管理授权 fail-open）已 CLOSED**：CLOSE/REDUCE 缺失、负值、超限、超仓、缺净敞口字段、保护目标不符全部有具体 reason（探针 R1-K04-2a/2b/2c/2d/2e/2f/7a/7b）。
- **r1 P1：11/14 CLOSED，2 PARTIAL，1 NOT CLOSED（已降级登记 L9/U15）**。状态推进、无损导出、REJECTED、nonce、保护目标、对账许可、零费用 gaps、分页停滞/乱序、scale 归一化、元组键、hash_strategy list 守卫均已修复并有 golden 回归（K03-N5..N13/P9..P12、K04-N8..N21/P6..P8、K01-N3/N4/P6，与 README §11 映射一致）。
- **新增问题 11 项：P1×1、P2×7、P3×3**（§4）。最高优先级是「运行代/环境绑定仍是可选字段 → 未绑定时跨进程代/跨环境重放 allowed」的 fail-open 默认；其余集中在 FILLED 未入终态、空串 fill_id、必填新字段与版本纪律、gaps 归因一致性、自由文本终止证据。
- **README §12 索引（L1–L12）本身准确**，但 U01/U02/U04 相关有 **8 项遗漏**（§5：U04×3、U02×3、U01×2）。
- **冻结纪律**：r2 修改了 4 条既有 golden 的 `expect`（K03-P1/P6、K04-N6、K04-P2、K05-N4）并删除了 README §10「既有用例不得修改期望值」原句（README:195），未逐条登记；另有 2 个新增必填字段违反 README:10「新增字段全部带默认值」。
- 处置建议：**有条件 GO**——在 §9 条件（P1 残余 + 2 个 P2 安全项 + 索引/纪律登记）在合并前由作者确认或写入 README §12 后再由 I 汇总冻结；不建议在登记前直接打 `contract-v3.x`。

## 2. r1 P0/P1 逐条判定

判定依据探针：`_review_scratch/r2_probe_r2_out.txt`（下称 R2-x）、`_review_scratch/r2_probe_out.txt`（r1 probe_contracts 复跑）、`_review_scratch/r2_probe_extra_out.txt`（r1 probe_extra 复跑）。

| r1 项 | 判定 | 证据（复现点） |
|---|---|---|
| **P0-1** REDUCE/CLOSE 缺省即放行、CLOSE 无数量校验 | **CLOSED** | R1-K04-2a `REDUCE_QTY_MISSING`；2b `NON_POSITIVE_QUANTITY`；2c `POSITION_QTY_MISSING`；2d/2e `NET_DELTA_MISSING`；7a `CLOSE_EXCEEDS_AUTHORIZATION+CLOSE_EXCEEDS_POSITION`；7b `CLOSE_EXCEEDS_POSITION`（`k04_management.py:255-298`）；golden K04-N8..N13 |
| P1-1 `apply_fill` 不推进 status / 假撤单 | **CLOSED** | R1-K04-1a `PARTIALLY_FILLED`、1d `FILLED`（`k04_management.py:433-452`）；1e 满成交订单不再变 CANCELED（另有残余 N2）；`violations()` 新增 `STATUS_FILL_MISMATCH`（:375-382）；golden K04-P6/N20 |
| P1-2 `to_components` 有损 | **CLOSED** | R1-K04-4c/4e：round-trip `equal=true`、N4 违规往返保留（`k04_management.py:395-408`）；golden K04-P7 |
| P1-3 无 fill 级幂等/无对账修复 | **PARTIAL** | fill_id+applied_fill_ids 已提供且重复 ID 抛 `DuplicateFillError`（R2-K04-3a；`k04_management.py:415-426`；golden K04-N19）；但无来源标记、无交易所权威累计 setter/只向上修正审计（README L6 降级 U04），且空串 fill_id 可绕过（新 N3），`applied_fill_ids` 需调用方自维护 |
| P1-4 REJECTED 语义 | **CLOSED** | `apply_fill(REJECTED)` 抛 `QuantityError`（R2-K04-3b；:423-424）；`REJECTED_WITH_ACCEPTED_QTY` 违规（R1-K04-4a；:383-386）；新增 `requested_qty`（:339）；构造期强制仍靠 `checked()`（残余 L11） |
| P1-5 授权不绑定运行代 | **PARTIAL** | `runtime_instance_id/environment` 已增字段并在不符时拒绝（R2-K04-A9 `RUN_GENERATION_MISMATCH`；:182-183、:238-247；golden K04-N15/P5）；但二者为可选：未绑定的授权在新进程代/其他环境仍 `allowed=true`（R2-K04-A10/A10b）→ 见新 N1 |
| P1-6 nonce 无防重放 | **CLOSED** | `consumed_nonces` 参数 + `NONCE_REPLAY`（R2-K04-2h；:217、:248-249；golden K04-N14）；消费点由 README L7 明示 |
| P1-7 保护目标不可对照 | **CLOSED** | 请求须带 `target_client_id` 且须在授权 sl/tp 集合内（R1-K04-2f `PROTECTION_TARGET_MISMATCH`；:300-313；golden K04-N16/N17）；残余：授权集合内两个目标可互换（N10 相关，非绕过） |
| P1-8 过期授权 vs UNKNOWN 查单 | **CLOSED** | `AuthorizationVerdict{submit_allowed, reconcile_allowed=True}`（:186-193、:315-322；golden K04-N7/P5） |
| P1-9 零费用证明忽略 gaps | **CLOSED** | COMPLETE+gaps → `provable=false`、cost `INCOMPLETE`（R1-K03-1a/1g；`k03_income.py:260-264`、:570-576；`coverage_problems` 增 `COVERAGE_GAPS_DECLARED` :229-230；golden K03-N5/N6/N7） |
| P1-10 分页无进度/排序保护 | **CLOSED** | 乱序 → `PAGE_NOT_SORTED`；满页无进展 → `PAGINATION_STALLED`（R1-K03-2d/2a；`k03_income.py:315-330`；golden K03-N8/N9）；窗口耗尽不再混报 `PAGE_SHORT`（R2-K03-B16） |
| P1-11 Decimal scale 敏感 | **CLOSED** | `-0.123456` vs `-0.1234560` → `duplicates`（R1-K03-3a；`k03_income.py:119`、`common.py:41-46/178-182`；golden K03-P9）；残余带符号零见 N8 |
| P1-12 existing 元组键静默失配 | **CLOSED** | 元组键按组件规范化后命中（R1-K03-3c；`k03_income.py:102-106`；golden K03-P11）；`income_content_hash` 已导出（`__init__.py`） |
| P1-13 `hash_strategy` 非 Mapping 绕过守卫 | **CLOSED** | list/字符串/字符串列表/嵌套 list 均 `SensitiveInputError`（R1-K01-1a、R2-K01 raw/string list/nested；`k01_identity.py:104-117`、`common.py:94-110`；golden K01-N4） |
| P1-14 `account_ref` 与密钥轮换解耦 | **NOT CLOSED**（已登记） | `api_id/signature/hmac` 仍不被守卫（`_review_scratch/r2_k01_extra_out.txt`）；轮换仍改变 effective_config_hash；无 `credential_epoch/key_fingerprint/account_ref` 派生契约 —— 已降级为 README §12 **L9（U15）**，契约层未实现 |

**r1 §6 反例 12 条复跑**（golden 外增量）：

| # | 契约 | 判定 | 实际 |
|---|---|---|---|
| 1 | K03 COMPLETE+gaps | CLOSED | `provable=false`，cost `INCOMPLETE`（R1-K03-1a/1g） |
| 2 | K03 同毫秒满页无进展 | CLOSED | `PAGINATION_STALLED`（R1-K03-2a） |
| 3 | K03 乱序满页 | CLOSED | `PAGE_NOT_SORTED`（R1-K03-2d） |
| 4 | K03 同值不同 scale | CLOSED | `duplicates`（R1-K03-3a） |
| 5 | K03 元组主键 | CLOSED | 键命中（探针用假 digest 故 `INCOME_ID_CONFLICT`；golden K03-P11 用真 digest→duplicates） |
| 6 | K03 `coverage_problems(PENDING)` 报 COVERAGE_NOT_COMPLETE | **NOT CLOSED（P2 级）** | 仍 `[]`（R1-K03-1d/R2-K03-B19）→ 见 N9 |
| 7 | K03 微额分摊负末腿 | CLOSED | `AllocationError`（R1-K03-5；`k03_income.py:525-528`；golden K03-N11） |
| 8 | K04 REDUCE 缺省/负值 | CLOSED | 见 P0-1 |
| 9 | K04 CLOSE 超仓/空仓 | CLOSED | 见 P0-1 |
| 10 | K04 REJECTED 接受 fill | CLOSED | 抛错 + 违规码（R2-K04-3b/4a） |
| 11 | K04 同一 fill 双计 | **PARTIAL** | 传 fill_id+集合即拒；缺省仍双计（R1-K04-3a）——按"调用方提供集合"的接口约定，README 已写明但残余空串绕过（N3） |
| 12 | K01 list 敏感键绕过 | CLOSED | `SensitiveInputError`（R1-K01-1a） |

## 3. 新发现（对抗性绕过 / 回归）

### N1（P1）运行代/环境绑定可选 → 未绑定时跨进程代、跨环境重放 allowed

- 位置：`k04_management.py:182-183`（字段 `None` 默认）、`:238-247`（`is not None` 才校验）。
- 复现（R2-K04-A10）：`ManagementAuthorization(runtime_instance_id=None)` 同一凭证连续两次 `allowed=true`；A10b：无 environment 的授权接受 `environment="live"` 请求。
- 影响：规格 §10.2「重启后旧进程签名失效」在**默认路径**下不可保证；r1 P1-5 的修复是 opt-in。README L7 只登记了 nonce 消费，未登记该默认。
- 建议：U02 mint 时强制携带；或在 README §12 增条目并把 `validate_authorization` 改为"要求显式 `require_run_binding=True` 或凭证缺 run id 即拒"。

### N2（P2）`FILLED` 不在 `TERMINAL_STATUSES` → 可对满成交订单"撤单确认"

- 位置：`k04_management.py:68`（终态集合缺 FILLED）、`:459-462`（`request_cancel` 只拒 CANCELED/EXPIRED/REJECTED）、`:482-486`（非 working 分支仅写 `cancel_confirmed`）。
- 复现（R2-K04-A3）：FILLED 订单 `request_cancel` 不抛错，`confirm_cancel` 后 `status=FILLED, cancel_requested=true, cancel_confirmed=true, violations=[]`。
- 影响：产生"已完全成交但撤单已确认"的自相矛盾记录；golden K04-N20 只覆盖 CANCELED。与 P1-1「假撤单」同类，收敛不彻底。
- 建议：FILLED 归入终态（拒绝撤单）；或对 `cancel_confirmed ∧ status==FILLED` 增违规码并补 golden。

### N3（P2）空串 `fill_id` 使幂等短路

- 位置：`k04_management.py:425`（`if fill_id and fill_id in set(...)`）。
- 复现（R2-K04-A1）：`fill_id=""`, `applied_fill_ids=("",)` 连续两次 → `filled=0.8`（双计），`revision=2`。
- 建议：`if fill_id is not None`（且空串拒绝或视为缺失并告警）；补 golden。

### N4（P2）新增必填字段与版本纪律冲突

- 位置：`k04_management.py:191`（`submit_allowed: bool` 无默认）、`k03_income.py:633`（`revision_id: str` 无默认）。
- 复现（R2-VER）：`AuthorizationVerdict.model_validate({"authorization_id","allowed","reason_codes"})` → `ValidationError: submit_allowed Field required`。
- 对照：README:10「新增字段全部带默认值」、§10 小版本规则（`docs/contracts/README.md:190-196`）。虽"未集成不承担兼容"，但文档自述不实。
- 建议：给默认值 + 派生一致性校验（`submit_allowed` 默认 False/由 `allowed` 校验；`revision_id` 改为内部派生或注默认），或明确把两处登记为 3.1 破坏性变更。

### N5（P2）environment 域幂等 id 在 `ManagementIntent` 中不可用

- 位置：`k04_management.py:99-111`（validator 固定四组件派生）vs `:114-154`（函数支持 `environment`）、README:123（"环境域由 U02 在调用侧统一约定"）。
- 复现（R2-K04-A11）：`environment="demo"` 派生的 id → `ValidationError`；四组件 id → 通过。
- 影响：若 U02 采用环境域防 demo/live 碰撞，意图构造即失败；两条派生路径只有一条可用。
- 建议：`ManagementIntent` 增 `environment` 字段并将 validator 接受两种派生，或删除函数上的 environment 参数并在 README 说明环境域由外层键承担。

### N6（P2）`attribute_income` 对 COMPLETE+已声明 gaps 仍 ATTRIBUTED

- 位置：`k03_income.py:387-392`（仅按 `coverage_status` 分支）。
- 复现（R2-K03-B11）：`coverage=COMPLETE, gaps=[1100,1200)` → `ATTRIBUTED`，同参数 `zero_fee_proof=false`、`finalize_costs=INCOMPLETE`。
- 影响：同一覆盖证据下"归因完成"与"成本不完整"并存，下游可能引用 ATTRIBUTED 结论。
- 建议：gaps 非空时归因判 UNKNOWN（`COVERAGE_GAPS_DECLARED`），保持三方一致；或 README 明示归因不受 gaps 约束。

### N7（P2）零费用终止证据/布尔与窗口无绑定（自由文本可证明）

- 位置：`k03_income.py:265-268`（仅检查非空）、`:272-277`（三个裸布尔入参）。
- 复现（R2-K03-B13）：`pagination_terminated_evidence="x"`, 三布尔 true → `provable=true`（含 R1-K03-1b/1c 的旧场景）。
- 影响：§10.1「不能靠空返回证明不存在收入」的完整性依赖调用方诚实；README L8 只登记了事务结构，此项无索引。
- 建议：索引增补 U01 项（证据哈希/窗口绑定/新鲜度），或契约侧把三个布尔并入 coverage/流水线状态。

### N8（P3）带符号零导致内容指纹分歧

- 位置：`common.py:178-182`（`normalize()` 不消除 `-0`）、`k03_income.py:119`。
- 复现（R2-K03-B15）：`amount="-0"` 与 `"0"` 内容哈希不同（`c2795b…` vs `c2becf…`），同键零值事件会被隔离为 `INCOME_ID_CONFLICT`。
- 影响：仅噪声（隔离偏保守），非漏账。建议 `decimal_text` 统一 signed zero。

### N9（P3）`coverage_problems` 与 `zero_fee_proof` 双语义仍在

- 位置：`k03_income.py:224-232` vs `:258-259`。
- 复现：PENDING → `coverage_problems=[]` 而 `zero_fee_proof=["COVERAGE_NOT_COMPLETE"]`（R2-K03-B19）；COMPLETE+watermark<end → `COMPLETE_WITHOUT_COVERAGE` vs `WATERMARK_BEFORE_WINDOW_END`（r1 K03-1e 未改）。
- 建议：单一判定源，或 README 说明两函数职责边界（前者只查自洽性）。

### N10（P3）`reconcile_allowed` 恒 True 且不携带账户/生命周期含义

- 位置：`k04_management.py:320`（无条件 True）。
- 复现：账户/生命周期不符的 verdict 里 `reconcile_allowed=true`（R2 探针任意 mismatch 行）。
- 影响：读单许可被表述为全局常量；实现者若把它当作"本请求对应订单可查"会越过账户绑定。建议仅当账户域匹配时置 True，或 README 注明其不承担账户校验。

### N11（P2，冻结纪律）既有 golden 期望被修改且 §10 原规则被删除

- 变更（`_review_scratch/r2_cmp_golden_out.txt`、`r2_golden_changes_out.txt`）：
  - K03-P1/P6：键文本 `SYNTH_ACCT/FUNDING_FEE` → 小写（键归一化的语义面变更）；
  - K04-N6：expect 追加 `INSUFFICIENT_POSITION`（新增必填 `current_position_qty` 所致）；
  - K04-P2：`revision "5"` → `5`（修复有损导出）；
  - K05-N4：expect 追加 `EVIDENCE_PREDATES_ACK`；K02 全部用例 identity `schema_version 3.0→3.1`（inputs only）。
- 同时 README §10 原句「golden 追加新用例，**既有用例不得修改期望值**」在 r2 中被删除（现 `README.md:195` 仅剩"golden 追加新用例"）。
- 建议：在 §11/§12 逐条登记这 4 处期望变更与理由（其中 K03 键文本、K04-P2 属 3.1 行为变更），并对"未集成不兼容"的豁免范围显式说明。

## 4. README §12 缺口索引核对（L1–L12）

**准确性**：L1（值内容不扫描）、L2（K02 自由 dict：`validate_intent(intent: Mapping…)`）、L3（`k02_evidence.py:649-655` `now_ms=None→as_of_ms`）、L4（无 `cycle_deadline_ms`）、L5（`AlertDelivery` 无通道/dedup/退避；Incident `reason_code` 为自由串）、L6（无对账 setter/保护命令模型/UNKNOWN 查单计划）、L7（nonce 消费由调用方）、L8（无分页-水位同事务与 `pending_since` 等）、L9（无 `credential_epoch` 等）、L10（`compare()` 期望子集断言）、L11（构造器不内建校验）、L12（指标阈值未冻结）均与代码一致；L1/L2/L3/L5/L6/L7/L8/L10/L11 我已逐点抽查。

**遗漏 8 项（U01/U02/U04 相关）**：

| # | 任务 | 遗漏内容 | 关联 |
|---|---|---|---|
| O1 | U04 | `FILLED` 未入终态，可对满成交订单发起/确认撤单；无对应 golden | N2 |
| O2 | U04 | `fill_id` 空串绕过、可选幂等、`applied_fill_ids` 调用方自维护且接口不返回；无来源（user stream/REST）字段 | N3、P1-3 残余 |
| O3 | U02 | 运行代/环境为可选绑定，未绑定时跨代/跨环境重放；L7 只写 nonce | N1 |
| O4 | U02 | environment 域 `management_id` 与 `ManagementIntent` validator 互斥（README:123 与 :99-111 矛盾） | N5 |
| O5 | U01 | 零费用终止证据/三布尔与窗口、`synced_at` 无绑定（自由文本可通过）；L8 未覆盖证据绑定 | N7 |
| O6 | U01 | `attribute_income` 对 COMPLETE+gaps 不 fail-closed，与成本/零费用判定冲突 | N6 |
| O7 | U04 | 改量（amend）语义（accepted revision、`client_amend_id`、"被拒改量不冒充已接受"）无任何接口亦无索引（r1 §5-U04-6） | — |
| O8 | U02 | 授权→意图→命令（Outbox）关联 ID 与重启后只读恢复形态未索引（r1 §5-U02-7；L7 仅覆盖在途预留） | — |

## 5. 正面确认（不计缺口）

- K04 状态推进（NEW→PARTIALLY_FILLED→FILLED）、REJECTED 全零语义、`checked()` 严格工厂、`to_components` 无损往返、`management_id` 绑定、键转义防碰撞、`MAX|-1` 归一化后跨轮稳定，均与规格 §4/§10.2 一致且正反 golden 齐（K04-P5..P8、N8..N21）。
- K03 分页三态（短页终止/乱序拒绝/无进展停滞）、窗口耗尽单码、gaps 半开校验、归因 fail-closed（None/PENDING/GAP）、`finalize_costs` 保留归因状态、迟到修订 `revision_id` 稳定且多义拒绝、`AllocationError` 负分摊拒绝，均有 golden（K03-P9..P12、N5..N13）。
- K01 四组覆盖强制（`IdentityCoverageError` 列全缺口）、原始文本/行列表拒绝、空集合语义、`deployment_hash` 与 `identity_hash` 分离，均有 golden（K01-P6、N3、N4）。
- r1 反例 1–5、7–10、12 关闭；r1 P0 关闭；smoke 126 PASS / 0 FAIL。

## 6. 同模型偏差声明与审查边界

- **同模型偏差**：本报告作者与候选实现/编排者同为 `deepseek-v4.1-flash`（U00 evidence 已登记该偏差）。① 本报告不构成独立交叉审计；② 对 pydantic 惯用法与"可选绑定/默认值"这类作者偏好，我可能共享盲区（例如 N1 是否应判 P0 而非 P1 值得 Q/C/I 复核）；③ smoke 与 golden 同源自证，本报告结论来自独立探针；④ r2 修复代码量较大，我未逐行核验 K02/K05/K06 全部修复（仅按索引需要抽查）。
- **边界**：未读运行目录 `multi-agent-trader/`、未读 `.env/.env.local/data`、未联网、未启动服务；未改任何 tracked 文件（`git status --short` 仅两项 untracked，HEAD=`0b85bef`）；未执行 git 提交/推送/标签；未在真实 Binance 语义下验证分页排序与边界包含性（仍按规格假定升序 + 适配层 `endTime=end-1ms`）。

## 附录 A 复现清单（全部在 `_review_scratch/`，纯内存/只读）

| 文件 | 内容 |
|---|---|
| `r2_smoke_out.txt` | r2 smoke 真实输出（PASS 126 / FAIL 0；113 golden，exit 0） |
| `r2_probe_out.txt` | r1 `probe_contracts.py` 复跑：K04-3b 处按新语义抛 `QuantityError`（exit 1，预期内） |
| `r2_probe_extra_out.txt` | r1 `probe_extra.py` 复跑：`ManagementIntent(max_reduce_qty=-5)` 处 `ValidationError`（exit 1，预期内），其余已改判 |
| `r2_probe_r2_out.txt` | r2 全量探针（74 行）：P0/P1 复跑 + 新对抗 A1–A12 + K03 B11–B19 + K01 + 版本纪律 |
| `r2_k01_extra_out.txt` | 守卫漏名（`api_id/signature/hmac/binance_uid`）与 `api_key` 对照 |
| `r2_cmp_golden_out.txt` / `r2_golden_changes_out.txt` | 既有 golden expect/inputs 变更比对 |
| `probe_r2.py` / `cmp_golden.py` / `show_golden_changes.py` | 本次新增的只读探针脚本 |

# U00 契约候选审查报告 — B（持仓与执行 / 账本语义）

- 审查人：B 子代理（deepseek-v4.1-flash）
- 工作区：`mat-wt/reviews/U00/B/w0r1` @ `db7e0d7`（detached）
- 审查对象：`src/core/contracts/k04_management.py`、`k03_income.py`、`k01_identity.py`（account_ref 部分）、`common.py`（共享序列化/守卫）、`docs/contracts/README.md`、`docs/contracts/golden/{k03,k04}*.json`、`docs/contracts/{k03,k04}*.schema.json`、`scripts/smoke_contracts_v3.py`
- 权威依据：规格 §4 K01/K03/K04、§10.1、§10.2、§12；专项报告 §7（RV/复盘）、§9 DL03/DL05；DC04/DC05/DC13；任务卡 U01/U02/U04 的 offline_acceptance
- 方法：只读静态审查 + `_review_scratch/` 纯内存探针（不改任何 tracked 文件；未读运行目录 `multi-agent-trader/`、`data/`、`.env`；未联网；未启动服务）

## 0. smoke 基线

```
$env:PYTHONUTF8=1
& "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe" scripts\smoke_contracts_v3.py
结果：PASS 69 / FAIL 0；golden cases=56 (positive=26, negative=30)   # exit 0
```

完整输出：`_review_scratch/smoke_out.txt`。注意：smoke 与 golden 由同一候选提交提供，属"自证"；本报告结论不依赖其 PASS，独立探针见附录 A。

## 1. 结论

- 主干规则与规格一致且正反两面都有 golden：K04 的三分量守恒、撤单未确认不得入 terminal、终态迟到 fill 修订累计成交、旧 revision 授权失效、OPEN 拒绝；K03 的幂等隔离、同毫秒满页重叠重查、短页终止、零费用四条件、守恒精确相等、迟到收入不新增闭合。
- 但有两类系统性问题：
  1. **K04 管理授权门 fail-open**（P0）：`CLOSE` 分支没有任何数量/仓位校验；`REDUCE` 与净敞口校验在字段缺省时整段跳过。冻结契约层无法保证"不反向开仓/不提净敞口/不超减"。
  2. **订单事实模型不完整**（P1 群）：`apply_fill` 不推进 status 且无一致性校验、`to_components` 有损（丢撤销确认标志）、无 fill 级幂等、无权威累计对账修复、`REJECTED` 无法表达"请求未被接受"、授权不绑定运行代、nonce 不防重放、保护目标不可对照。
  3. **K03 证明链可被自相矛盾输入洗白**（P1 群）：`zero_fee_proof` 不校验已声明缺口（与 README §5 矛盾且直通 `cost_status=COMPLETE`）；分页无进度/排序保护（死循环或静默漏收入）；内容指纹对 Decimal scale 敏感（同值被判冲突隔离）；`existing` 映射用 `str(key)` 导致元组主键静默失配重复入库。
- 计数：**P0 = 1，P1 = 14，P2 = 17**；接口缺口 **U04 = 8，U02 = 9，U01 = 9**；反例增量 **12 条**（其中 9 条当前实现即失败）。
- 处置建议：现状不宜直接打 `contract-v3.0` 冻结。要么按 README §10 走小版本（3.1）补齐 P0 + P1 中与 U01/U02/U04 验收直接相关项并追加反例 golden；要么在 README §12 显式登记"以下安全判定由 U02/U04/U01 自建 fail-closed 补齐"，否则验收矩阵无契约依据。

## 2. P0

### P0-1 管理授权校验 fail-open：CLOSE 无数量校验；REDUCE/净敞口检查在字段缺省时跳过

- 依据：`src/core/contracts/k04_management.py:152-155` 只挡 `OPEN`；`:156-162` 仅在 `action=="REDUCE"` 且 `reduce_qty` 非 None 时比较上限与仓位；`:163-165` 仅在 `requested_net_delta` 非 None 时拒绝正值。
- 复现（`_review_scratch/probe_contracts.py`，输出 `probe_out.txt`）：

| 探针 | 请求要点 | 现状 |
|---|---|---|
| K04-7a | `action=CLOSE, close_qty=100, current_position_qty=0.01, requested_net_delta=-100` | `allowed=true, reasons=[]`（反向开仓路径） |
| K04-7b | `action=CLOSE` 且 `current_position_qty=0`（空仓平仓） | `allowed=true` |
| K04-2a | `action=REDUCE` 缺 `reduce_qty` | `allowed=true` |
| K04-2b | `action=REDUCE, reduce_qty=-0.005` | `allowed=true`（负值不被拒） |
| K04-2d/2e | `action=REDUCE` 缺 `requested_net_delta` | `allowed=true`（提高净敞口的唯一检查被省略） |

- 影响：规格 §4「不允许 OPEN、提高净敞口」、§10.2「多轮累计不得超减」、U02 验收「重复退出不反向开仓」在冻结契约层不可保证。门禁的安全属性依赖调用方自觉传全字段（fail-open）。
- 建议（不实现）：REDUCE 必须要求 `reduce_qty > 0` 且 `≤ min(max_reduce_qty, current_position_qty)`；CLOSE 必须要求 `close_qty > 0` 且 `≤ current_position_qty`（或显式 `close_all` 语义 + 仓位为 0 时拒绝）；缺省字段一律拒绝而非跳过；新增 reason code（`MISSING_REDUCE_QTY` / `NON_POSITIVE_QUANTITY` / `CLOSE_EXCEEDS_POSITION`）并补正反 golden。

## 3. P1

### P1-1 `apply_fill` 不推进 status，`violations()` 不校验 status/分量一致性 → 可产出"满成交的 NEW"与"假撤单"

- 位置：`k04_management.py:255-268`（working 分支只重算 working/terminal，不改 status）、`:199-230`（无 status 一致性检查）。
- 复现：
  - K04-1a：`NEW(0.01)` + fill 0.0047 → `status=NEW, filled=0.0047, working=0.0053, violations=[]`（应为 `PARTIALLY_FILLED`）
  - K04-1d：`NEW(1)` + fill 1 → `status=NEW, filled=1, working=0, violations=[]`（应为 `FILLED`）
  - K04-1e：承上再 `confirm_cancel` → `status=CANCELED, filled=1, terminal=0`（满成交订单被记为"已撤且无残量"，与真实已撤不可区分）
- golden 已固化该语义：`docs/contracts/golden/k04_management.golden.json:16`（ord-2 `status=NEW, cumulative_filled_qty=0.0047` 期望无违规）。
- 影响：DL05「订单状态与模型陈述分离」、DC13「以账户对账为准」、F21「不漏仓、不假覆盖」。U04/UI/保护清理若以 `status` 推断"是否成交/是否已撤"会误判。
- 建议：fill 后按分量推导/校验 status（`NEW ∧ filled>0` 违规；working 状态且 `working==0 ∧ filled==accepted` 应转 FILLED 或新增违规码）；golden ord-2 需修正或显式声明 status 仅为交易所快照、禁止用于推断。

### P1-2 `to_components` 有损：丢 `cancel_requested/cancel_confirmed`，重水化后 N4 违规消失

- 位置：`k04_management.py:232-241` 只导出 `order_id/status/revision/四个分量`；而 K04-N4 的违规完全依赖 cancel 两标志（`:224-229`）。
- 复现：K04-4c/4e —— `OrderQuantity.model_validate(to_components())` 后 `ok=false`，`cancel_requested: true→false`、`cancel_confirmed: true→false`；K04-N4 样本重水化后 `violations` 由 `["CANCEL_UNCONFIRMED_TERMINAL_COUNTED"]` 变 `[]`。
- 影响：契约给出的唯一"分量导出"接口有损；U04 若用它落库/回放会丢撤销确认状态，负例可被洗白。
- 建议：`to_components` 增补 cancel 两字段，或另给无损 `to_record/from_record`，并加 round-trip golden。

### P1-3 无 fill 级幂等与对账修复接口：重复/乱序 fill 双计且只能加不能减

- 位置：`k04_management.py:244-272`，`apply_fill(quantity, fill_qty)` 无 fill/trade id、无来源序号、无"交易所权威累计"入参。
- 复现：K04-3a —— 同一笔 0.5 连续 apply 两次 → `filled=1.0, working=0.0, revision=2, violations=[]`，无任何提示。
- 反向缺口：本地多计（漏/重放/离线后补）需要以交易所 `executedQty` 校正，契约没有 setter、没有只向上修正规则、没有修正审计（只能重新构造，而构造不校验，见 P2-13）。
- 影响：DL05「迟到增量幂等补算」、§10.2「以账户对账为准」、F21「迟到部分 fill 事实能修订」、U04 验收「0.01 到 0.0047 部分成交数量可勾稽」缺幂等键。
- 建议：`apply_fill(..., fill_id=..., source="user_stream|rest", exchange_cumulative_filled=...)`；fill_id 去重与单调序号；对账 setter 带"只允许向上修正 + 审计原因"的显式规则。

### P1-4 `REJECTED` 无法表达"请求未被接受"，且 `apply_fill` 静默接受 REJECTED 的成交

- 位置：`k04_management.py:184-197` 无 `requested_qty`（客户端原始量）与 `accepted_qty`（交易所已接受量）之分；`:244-272` 对 status 不做限制。
- 复现：K04-3b —— `REJECTED, accepted=0.01, terminal=0.01` + fill 0.005 → `filled=0.005, terminal=0.005, violations=[]`。
- 规格 §4「拒绝请求不冒充交易所已接受订单」目前只能用全零表达（量信息丢失）；非全零则与"已接受后撤销"不可区分。
- 影响：U04「未知提交查单且不重复发单」/F01 语义与 DL05 审计。
- 建议：新增 `requested_qty`（或 `client_orig_qty`）；`REJECTED` 只允许 `terminal=requested ∧ filled=0`；`apply_fill` 对 REJECTED 报错（`REJECTED_CANNOT_FILL`）。

### P1-5 管理授权不绑定运行代：重启后旧签名失效不可表达

- 位置：`k04_management.py:107-121`（无 identity/environment/runtime_instance_id）、`:132-172`（不比较）。
- 对照：`docs/dispatch/reviews/U00/w0/F_fingerprint_r1.md:121` 记录现有开仓授权绑定 `environment/scope_hash/runtime_instance_id` 且有 HMAC —— K04 相对现有实现是回退。
- 复现：同一 authorization 对象在"新进程代"下重放 → `allowed=true`（无任何字段区分运行代）。
- 影响：§10.2「重启后旧进程签名失效」、§4「新增管理能力凭证绑定账户、仓位…」。
- 建议：凭证绑定 K01 identity（至少 environment + run generation），verdict 增 `RUN_GENERATION_MISMATCH`。

### P1-6 nonce 无消费/防重放接口

- 位置：`k04_management.py:119` 字段存在，`:132-172` 从未读取。
- 复现：K04-2h —— 同一 authorization 连续两次 validate → 两次 `allowed=true`。
- 依据：§10.2「授权的 nonce 与 deadline 用于防止重新提交」。
- 建议：`validate_authorization(..., consumed_nonces=...)` 或 verdict 增 replay 结果，并在 README 明示消费点。

### P1-7 保护目标不可对照：`remove_protection` 可指向任意保护单

- 位置：`k04_management.py:166-167` 只看 `authorization.target_protection.is_last_protection`；请求侧无保护单标识，`target_protection.sl_client_id/tp_client_id`（`:102-103`）从未被比较。
- 复现：K04-2f —— 授权 `target=sl-1`，请求 `remove_protection=true, sl_client_id="sl-OTHER"` → `allowed=true`。
- 依据：F19「不误撤未知挂单/其他仓保护」、§10.2「清理已证明归属的遗留保护，不误撤其他仓的保护」。
- 建议：请求侧带 `target_client_id`（或 kind+id），与授权目标严格比对，不匹配 → `PROTECTION_TARGET_MISMATCH`。

### P1-8 过期授权与 UNKNOWN 查单/对账的关系无接口表达

- 位置：全文件无"对账/查单许可"概念；`AuthorizationVerdict`（`k04_management.py:124-130`）只有 allowed + reason_codes。
- 依据：§10.2「未知请求仍按原 client ID 查单，不因授权过期停止对账，也不产生新ID重复提交」、U02 验收「过期授权不阻断UNKNOWN查单」。
- 影响：实现者容易把 `AUTHORIZATION_EXPIRED` 当作"不得再触达该订单"，或在过期后重发新 ID。
- 建议：显式区分"新提交授权"与"既有命令对账许可"（如 `reconcile_allowed=true` 恒成立 + 只读约束），或 README 写明该边界由 U04 查单计划承担并给出接口位置。

### P1-9 零费用证明不校验已声明缺口（与 README §5 矛盾，直通 cost COMPLETE）

- 位置：`k03_income.py:224-231` 只按 `coverage_status` 分支，从不读 `gaps`；`k03_income.py:187-201`（`coverage_problems`）同样忽略 gaps（仅 GAP 状态时报告）。
- README §5 明确："零费用需同时：来源正常 + 分页终止 + watermark 覆盖 + **无缺口** + 迟到复查"。
- 复现：K03-1a/1g —— `coverage_status=COMPLETE` 且 `gaps=[{1100,1200})` → `zero_fee_proof.provable=true`；`finalize_costs(..., zero_fee_provable=true)` → `cost_status=COMPLETE`。
- 影响：§10.1「不能靠空返回证明不存在收入」、§12「资金账本缺证据」不得 RELEASE_READY —— 这是把已声明缺口洗成 COMPLETE 的路径。
- 建议：provable 需 `not coverage.gaps`（新增 `COVERAGE_GAPS_DECLARED`）；gaps 内每条做 half-open 校验。

### P1-10 分页无进度保护与排序前置条件 → 死循环或静默漏收入

- 位置：`k03_income.py:270-284`，满页时 `next_start_ms=max(event_time_ms)`；未检查 `next_start_ms > start_ms`，未检查页内有序。
- 复现 K03-2a：`start_ms=1000`、3 条同毫秒=1000、`page_limit=3` → `CONTINUE, next_start_ms=1000`（无进展；下一次调用输入完全相同 → 永久循环）。资金费整点同毫秒结算，>1000 行同毫秒是现实场景。
- 复现 K03-2d：乱序满页 `[5000,1000,4000]`（窗口 `[1000,6000)`）→ `CONTINUE, next_start_ms=5000`，1000..4999 被跳过；调用方随后可把窗口标 COMPLETE 并证明零费用（静默漏收入）。
- 另：`k03_income.py:272-275` 满页且 `boundary>=end` 时同时返回 `PAGE_SHORT`（页并不短），reason 语义失真。
- 建议：新增无进展信号（`PAGINATION_STALLED`：满页且 `next_start<=start` → 非终止 + 告警）；要求/校验页内非降序（`PAGE_NOT_SORTED`）；对"无进展且满页"给出有界升级语义而非 CONTINUE。

### P1-11 内容指纹对 Decimal scale 敏感 → 同值同键被判冲突隔离

- 位置：`k03_income.py:95`（`"amount": str(item.amount)`）；`common.py:41-44`（`_json_default` 同样 `str(Decimal)`）；`common.py:142-146` 已有 `decimal_text`（normalize）却未用于任何序列化路径（仅 smoke 输出使用）。
- 复现 K03-3a：existing `-0.123456` vs page `-0.1234560` → 两个不同 hash，结果 `quarantined=["INCOME_ID_CONFLICT"]`（而非 duplicates）。
- 影响：§10.1「重复ID但内容矛盾进入隔离表和告警」——现在"相同值不同尾零"也进隔离：若以首次记录为准，账本保留错误精度；若告警，U01 产生噪声。
- 建议：内容指纹与规范化 JSON 统一用 `decimal_text`（或显式 quantize 政策），补 golden（同值不同 scale → duplicates）。

### P1-12 `ingest_income` 的 `existing` 映射用 `str(key)` → 元组主键静默失配、重复插入

- 位置：`k03_income.py:123-131`（`known[str(key)] = digest`）；`idempotency_key()`（`:75-78`）返回元组，文本键由 `idempotency_key_text`（`:81-82`）生成，两者互不相等。
- 复现 K03-3c：`existing={("demo","ACCT","FUNDING_FEE","t-001"): "deadbeef"}` + 同事件 → `inserted=[...]`（重复入库）；K03-3d 用文本键 → `duplicates`（对照）。
- 附：`income_content_hash` 未从 `core.contracts` 导出（`__init__.py` 的 `__all__` 无此项），调用方需从子模块导入。
- 建议：`existing` 只接受文本键（类型注解/校验），或对元组键做规范化；导出 `income_content_hash`。

### P1-13 K01 `hash_strategy` 对非 Mapping 输入绕过敏感键守卫

- 位置：`k01_identity.py:83-87`（`if isinstance(strategy_cards, Mapping): ensure_no_sensitive_fields(...)`）；`common.py:68-82` 的 `sensitive_paths` 本身支持 list，但被条件跳过。
- 复现 K01-1a/1-6：`hash_strategy([{"api_key":"sk-live-secret"}])` → 正常返回哈希；同内容放 dict（`{"card": {...}}`）→ 抛 `SensitiveInputError`。
- 对照 README §2「哈希输入含 key/secret/token… 时抛 SensitiveInputError」。
- 影响：策略卡若以 list 传入（`payload["cards"]`）即绕过；与规格「禁止对原始 .env 导出哈希」同类风险。
- 建议：`hash_strategy` 无条件 `ensure_no_sensitive_fields(strategy_cards)`。

### P1-14 `account_ref` 与 API key 轮换解耦无接口表达（K01 只给了字段名）

- 位置：`k01_identity.py:51/99` 仅 `account_ref: str`；无派生规则、无 `credential_epoch`/`key_fingerprint`、无"轮换不得改变 account_ref"的断言函数。
- 对照：`F_fingerprint_r1.md:120` 记录现网 `account_ref` 是 API key 的不可逆短哈希（`trade_reconciliation.py:66-69`）→ 轮换 API key 即改变账本身份，历史 lifecycle/收入/发布证据无法按账户连续归集。
- 复现 K01-5：名称不匹配的凭据字段（`api_id`/`signature`/`hmac`，K01-4 均未被守卫拦截）一旦进入 effective_config 即改变 `effective_config_hash` → 轮换改变身份。
- 建议：K01 增补 `credential_epoch`/`key_fingerprint`（非秘密、与 identity_hash 的关系显式化）+ `account_ref` 派生契约（禁止用 key 材料派生）；README 明示轮换流程由 U15 落地。

## 4. P2

1. **K03 两套判定/编码不一致**：`coverage_problems` 与 `zero_fee_proof` 对同一条件给不同 code —— COMPLETE+watermark<end 时前者报 `COMPLETE_WITHOUT_COVERAGE`、后者报 `WATERMARK_BEFORE_WINDOW_END`（K03-1e）；`watermark<start` 也报 `WATERMARK_BEFORE_END`（`k03_income.py:192-193`）命名失真。建议单一判定源。
2. **K03 gaps 元组不校验**：倒置区间 `[1900,1500)` 仅报 `COVERAGE_GAP`（K03-1f；`k03_income.py:199-200` 不遍历 gaps）。
3. **K03 `attribute_income(coverage=None)` → ATTRIBUTED/coverage_status=null**（K03-4a；`k03_income.py:331-333`），README §5 只写"非 COMPLETE→UNKNOWN"，None 被当作完整。建议 fail-closed。
4. **K03 PENDING/GAP 归因 → UNKNOWN 已实现但无 golden**（K03-4d；golden 只有 P8/N3）。
5. **K03 `finalize_costs(coverage=None, attribution=ATTRIBUTED)` 强制 `attribution_status=UNKNOWN`**（K03-4b；`k03_income.py:492-501`），已知归因状态丢失。
6. **K03 `allocate_weighted` 末腿可为负**：`allocate_weighted("0.000000016", {a,b,c:1})` → `["1e-8","1e-8","-4e-9"]`（Σ 守恒但出现负分摊，`k03_income.py:451-467`）；建议拒绝或提升 quantum。
7. **K03 幂等键无大小写/空白规范化，`income_type` 为自由字符串**（`k03_income.py:56-58/75-82`）：`" t-001"`、`"T-001"`、`"funding_fee"` 均生成独立键与独立覆盖（K03-3b）；§10.1 按 income_type 分范围，大小写变体会分裂覆盖证明。
8. **K03 `apply_late_income` 无 revision 身份**：`OutcomeRevision` 无 `revision_id`/`previous_revision_id`，重复调用只能靠 `(outcome_id, source_event_id)` 外部去重；`revision_version` 默认 `SCHEMA_VERSION`（`k03_income.py:567`）易与 §12"计算器版本"混淆；多个匹配闭合 outcome 时静默取第一个（`:578-588`）。
9. **K03 缺"分页结果与水位同一事务提交"的结构**（§10.1）：无 confirmed_end/pending_watermark 表达；也缺 `pending_since/next_sync/last_source_success`（§10.1 明示）。
10. **K04 `management_id` 与派生键无绑定**：`ManagementIntent.management_id` 为自由字符串（`k04_management.py:53`），无 model_validator 校验 `== derive_management_id(...)`，"不得每轮随机换ID"不可执行。
11. **K04 幂等键 `"|".join` 可碰撞且无环境域**（`k04_management.py:69-77`）：`lifecycle="a|b",trigger="c"` 与 `lifecycle="a",trigger="b|c"` 同键同 id（K04-5）；空组件 → `"|||"`（K04-7d）；demo/live 同 lifecycle 同键。
12. **K04 `request_cancel` 对终态订单也置位**（`k04_management.py:275-276`），与 `:224-229` 组合可制造假 `CANCEL_UNCONFIRMED_TERMINAL_COUNTED` 违规；建议终态拒绝。
13. **K04 `OrderQuantity` 构造不校验**（`k04_management.py:184-197`）：`filled>accepted`、负 accepted 均可构造，仅 `violations()` 事后报告（K04-4a/4b）；无 `from_components`。建议 model_validator 或 `checked()` 工厂 + 运行时强制。
14. **K04 `ManagementIntent` 无边界**：`max_reduce_qty="-5"`、`expires_at_ms=1`（过去）可构造（K04-7c；`k04_management.py:58-62`）。
15. **K01 `identity_hash()` 含 runtime_instance_id**（`k01_identity.py:60-62`）而 `identity_mismatches` 默认忽略它（`:41`）：同名"身份"两义（K01-2：matches=true 但 hash 不等）；建议区分 deployment_hash/run_hash 或注明用途。
16. **K01 `validate_coverage` 把空集合当缺失**（`k01_identity.py:157`）→ 合法的"无备用路由/无 profile 覆盖"被报缺口。
17. **K01 `identity_mismatches` 把 schema_version 计入不匹配**（`k01_identity.py:124-136`）：3.0 记录 vs 3.1 期望即 IDENTITY_MISMATCH，与 README §10 小版本容忍表述有张力；建议显式可忽略项。

## 5. 接口缺口清单（只列缺口，不实现）

### U04（执行 / 部分成交 / 保护）

1. fill 级幂等：fill/trade id、来源（user stream / REST）、去重表接口（P1-3）。
2. 以交易所权威累计对账的 setter + 只向上修正规则 + 修正审计（P1-3）。
3. status 推进/校验（NEW→PARTIALLY_FILLED→FILLED；working 状态与分量一致性；终态与 filled 一致性）（P1-1）。
4. 无损持久化接口（cancel 两标志及后续字段；`to_components` 目前有损）（P1-2）。
5. `requested_qty` vs `accepted_qty`（REJECTED/未接受语义）（P1-4）。
6. 改量（amend）表示：accepted revision 记录、client_amend_id、"被拒改量不冒充已接受"（规格 §4；当前无任何接口）。
7. 保护单身份与端点映射：普通 `clientOrderId/orderId` vs `clientAlgoId/algoId`、查单/撤单端点、`closePosition` 型保护禁 quantity/reduceOnly（§10.2）；K04 无保护命令模型（U04 的 `protection_commands.py` 需自建）。
8. UNKNOWN 订单的有界查单计划与状态（UNKNOWN→MANUAL_RECOVERY、禁删 Outbox、保持不确定敞口禁新开）（§10.2；K04 无表达）。

### U02（策略仓管理 / 授权）

1. 授权绑定运行代（identity / runtime_instance_id / environment）（P1-5）。
2. nonce 消费/防重放（P1-6）。
3. 既有命令对账许可与授权过期的关系（P1-8）。
4. CLOSE 数量语义与反向/空仓拒绝（P0-1）。
5. 保护目标对照与"最后保护"判定输入（P1-7）。
6. `available_reduce_qty = 最新确认仓量 − 在途退出预留`：预留/释放/对账的原子接口（§10.2；K04 只有静态 `max_reduce_qty`）。
7. 授权→意图→命令（Outbox）关联 ID 与持久化形态：`management_id/authorization_id/nonce/client_order_id` 绑定与重启后只读恢复（§10.2）。
8. 保护替换（先建后撤/有界重复/最终收口）与"不宣称原子性"的表达（§10.2）。
9. 请求形状校验（`reason_code/policy_version/evidence_id` 非空、字段必填、类型）（P0-1/P2-14）。

### U01（收入 / 成本）

1. 分页+水位同事务的提交结构（confirmed_end / pending_watermark / page 结果）（§10.1）。
2. `pending_since / next_sync / last_source_success` 字段（§10.1 明示）。
3. 幂等键规范化（大小写/空白）与 `income_type` 枚举（P2-7）。
4. 内容指纹的十进制规范化（P1-11）与 `income_content_hash` 导出（P1-12）。
5. `existing` 映射键类型契约（P1-12）。
6. 零费用证明的证据绑定：gaps 非空即不可证明（P1-9）、`pagination_terminated_evidence` 与窗口/`synced_at_ms` 的绑定与新鲜度（当前为自由文本，K03-1b/1c）。
7. 分页排序前置条件与无进展检测（P1-10）。
8. 归因接口的覆盖前置（None 也需 UNKNOWN）（P2-3）与按 income_type/asset 的决策域（当前 decisions 只带 event_id）。
9. 账户级勾稽公式（期末−期初 = 已实现盈亏 − 净手续费 + 资金费 + 净划转 + 其他已分类现金流 + 未解释差额，§10.1）与"同一手续费来源不重复相加"的结构：K03 无任何表达（U01 offline_acceptance 项）。

## 6. 反例增量（golden 未覆盖；要求 ≥2，此处 12 条）

| # | 契约 | 输入要点 | 期望（依据） | 现状 |
|---|---|---|---|---|
| 1 | K03 | COMPLETE + `gaps=[1100,1200)` + watermark=end + 终止证据 + 三布尔 true | `provable=false`（README §5"无缺口"） | `true`（K03-1a） |
| 2 | K03 | 满页全部 `event_time==start_ms`（`page_limit==len`） | 非 CONTINUE，或 reason 含 `PAGINATION_STALLED`（§10.1） | `CONTINUE/next=start`（K03-2a） |
| 3 | K03 | 满页乱序 `[5000,1000,4000]`，窗口 `[1000,6000)` | 拒绝或 `PAGE_NOT_SORTED`（否则静默漏 1000..4999） | `CONTINUE/next=5000`（K03-2d） |
| 4 | K03 | existing `-0.123456` vs page `-0.1234560`（同值不同 scale） | `duplicates`（§10.1 同键同内容幂等） | `quarantined INCOME_ID_CONFLICT`（K03-3a） |
| 5 | K03 | existing 用 `idempotency_key()` 元组为键 | `duplicates`（或显式拒绝非文本键） | `inserted`（重复入库）（K03-3c） |
| 6 | K03 | PENDING + watermark=end + 终止证据 | `coverage_problems` 报 `COVERAGE_NOT_COMPLETE`（与 zero_fee_proof 一致） | `[]`（K03-1d） |
| 7 | K03 | `allocate_weighted(0.000000016, 三腿等权)` | 拒绝或末腿 ≥0（不得负分摊） | `[-4e-9]`（K03-5） |
| 8 | K04 | REDUCE 缺 `reduce_qty` / `reduce_qty=-0.005` / 缺 `requested_net_delta` | `allowed=false` + 具体 reason（§4） | `allowed=true`（K04-2a/2b/2e） |
| 9 | K04 | CLOSE `close_qty=100` vs 仓 0.01；CLOSE 于空仓 | `allowed=false`（U02"不反向开仓"） | `allowed=true`（K04-7a/7b） |
| 10 | K04 | REJECTED + fill 0.005；REJECTED 携带非零 accepted | 拒绝（`REJECTED_CANNOT_FILL`）或 requested/accepted 分离（§4） | `filled=0.005` 无违规（K04-3b） |
| 11 | K04 | 同一 fill 事件重放两次 | 幂等（需 fill_id）或明确拒绝 | `filled` 双计（K04-3a） |
| 12 | K01 | `hash_strategy([{"api_key":...}])`（list 顶层） | `SensitiveInputError`（README §2） | 返回哈希（K01-1a） |

## 7. 正面确认（不计缺口）

- K04 三分量守恒与状态边界（NEW/PARTIALLY_FILLED→terminal=0；终态→working=0；撤单未确认→terminal≠0 违规；满成交→无残量）与规格 §4 一致，golden P1/N4 手工可核。
- K04 终态迟到 fill 修订累计成交、revision 递增、幂等键四元组与跨轮稳定（P3 覆盖 `derive_management_id`）。
- K04 旧 revision 授权失效、授权过期边界（`now>=expires` 即拒）、OPEN 拒绝、REDUCE 超上限拒绝。
- K03 幂等主键不含 symbol/asset、同键异内容隔离不覆盖、同毫秒满页重叠重查（不加 1ms）、短页终止、GAP 不填零、双向仓/多生命周期歧义 UNATTRIBUTED、守恒精确相等（默认 `tolerance=0`）、迟到收入不新增闭合、末腿承接余数可重放（常规量级）。
- K01 `git_commit` 不参与文件内容哈希但进入身份比较、敏感键守卫对 dict 生效、依赖锁/策略卡/模型路由/有效配置四组覆盖清单与 `validate_coverage`。
- smoke：69 PASS / 0 FAIL，schema 与模型同步，56 golden（26 正 / 30 反）。

## 8. 同模型偏差声明与审查边界

- **同模型偏差**：本报告作者与候选/编排者同为 `deepseek-v4.1-flash`（U00 evidence 亦登记"同模型偏差需声明"）。因此：①本报告不构成独立交叉审计；②对 pydantic v2 惯用法与"validity 显式调用"这类作者偏好，我可能共享盲区（例如我把 `OrderQuantity` 无内建校验判为 P2 而非 P0，可能低估）；③smoke 与 golden 由同一提交提供，其 PASS 不作为独立证据，本报告结论来自独立探针（附录 A）。
- **边界**：未读运行目录 `multi-agent-trader/`、未读 `.env/.env.local/data`、未联网、未启动服务；未改任何 tracked 文件（`git status --short` 仅 `?? _review_scratch/`）；未执行 git 提交/推送/标签。
- **未做**：未在真实 Binance 语义下验证（分页排序与边界包含性按规格 §10.1/README 假定"升序 + 适配层 `endTime=end-1ms`"；K03-2d 表明该假定未被契约声明或校验）；未做性能基准；未审查 K02/K05/K06（除 `common.py` 共享序列化）。

## 附录 A 复现

- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，`PYTHONUTF8=1`；全部探针纯内存、无网络、只写 `_review_scratch/`。
- `_review_scratch/probe_contracts.py` → `_review_scratch/probe_out.txt`（K04-1..6、K03-1..5、K01-1..3）
- `_review_scratch/probe_extra.py` → `_review_scratch/probe_extra_out.txt`（K03-2d/3c/1g/4d、K04-7、K01-4..6）
- `_review_scratch/smoke_out.txt`（smoke 全量输出，退出码 0）

## 附录 B 建议的冻结处置

- 若按现状冻结：必须在 README §12 显式登记降级声明 —— "管理授权门的字段缺省/CLOSE 分支由 U02 自行 fail-closed 补齐"、"fill 幂等与对账修复由 U04 自建"、"零费用 gaps 校验与分页进度检测由 U01 自建" —— 否则 U01/U02/U04 的 offline_acceptance 无契约依据。
- 若修版：建议按 README §10 的小版本（3.1）规则增补可选字段与 reason code（`requested_qty`、`run_generation`、`fill_id`、`credential_epoch`、`revision_id`、`PAGINATION_STALLED`、`PAGE_NOT_SORTED`、`COVERAGE_GAPS_DECLARED` 等），既有 golden 期望保持不变，新增本报告 §6 的反例为 golden。

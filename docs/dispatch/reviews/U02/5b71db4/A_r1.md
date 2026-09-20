# A（账本/数量守恒）· U02 R1 初审报告

- 审查对象：`task/U02` @ `5b71db4`（base `45ebdff`，含 U00/U01/U03/U04/U15）
- 工作区（独立 detached worktree）：`reviews/U02/A/r1`（HEAD = `5b71db4`，`git status` clean）
- 沙箱：`_scratch_A/{pristine,sb01,sb02,sb,sba,sbb,sbc,sb_noreg}`（均为 `git archive 8c66f65` 的独立 `git init` 副本）
  - `pristine` = 补丁前（**已含作者独占文件**，见 §4 口径说明）；`sb` = 01..06 全应用；`sb01/sb02/sba/sbb/sbc` = 部分应用
- 解释器：`multi-agent-trader\.venv\Scripts\python.exe`（3.13.12，`PYTHONUTF8=1`）；无 pytest（测试文件本身可直接运行）
- 全部命令输出留在 `_scratch_A/out_*.txt`；探针在 `_scratch_A/probes/p0*.py`

## 结论：**CHANGES_REQUESTED**

- 结构、契约绑定（K04/U15）、R11 故障矩阵、重复退出幂等、旧仓不隐式退出：**均能独立复现，判定正确**。
- 但**退出预留有一个 P0 级活性缺陷**（预留泄漏 → 该生命周期永久无法自动退出，且回报的 reason 是误导性的"无仓可平"），另有 1 个 P1 代码缺陷（`_open` 引用未定义变量）、1 个 P1 纸面路径缺陷（同名标的第二次退出永久被幂等吃掉）、1 处 README 风险方向说反、1 份**不可执行**的账本迁移建议。
- 结论性判断：**01+02 的"原子单位"要求成立，但其理由与事实相反**；**第 8 项账本口径的迁移建议不充分，且"声明账户后旧数据不可幂等"是真实存在的**（已用真实 schema/索引复现）。

---

## 1. 问题清单

### P0-1 退出预留泄漏 → 该生命周期永久无法自动退出（F03/R09 直接场景）
`src/exchange/binance_futures.py:2018`（`_reserve_exit` 占量）…`:2202/:2204`（`_release_exit` / `_settle_exit` 结算）

- 占量之后、结算之前有**大量可抛出点**，其中最典型的是 `_submit_standard_order(...)`：`binance_futures.py:730/736` 在"确定性拒绝"或"POST 异常后按 clientOrderId 对账也失败"时**重新 raise**（HTTP 429/418、reduceOnly 被拒、4xx 均算确定性拒绝）。异常被逐 intent 调度循环的 `except Exception`（`binance_futures.py:1192-1194`）捕获并只记 `futures_order_error`，**`_release_exit`/`_settle_exit` 都不会执行** → 预留以全量 `ACTIVE` 泄漏。
- 泄漏后：`request_exit` 每轮先 `reconcile_position(交易所当前量)`，因 `reserved == position` → `available == 0` → 之后**每一次**退出请求（新 key、老 key 都一样）返回 `NOOP`，`reason_codes = (EXIT_EXCEEDS_POSITION, NO_POSITION_TO_EXIT)`。
- 文件内 `_release_exit` 仅 3 次出现（1430 调用 / 2202 调用 / 2253 定义），**没有任何清扫器**（探针断言 `count == 3` 通过）；且 1430 那次调用本身是死代码（见 P1-2），实际只有 `FILL_UNCONFIRMED` 一条释放路径。
- **稳定 key 会让问题更早发生**：`position_guardian.py:34 _stable_close_id()` 给出的退出 key 跨周期/跨重启稳定（`mta-g-<sha24>`）；首次失败后 `_exit_keys` 已记忆（`request_exit` 接受即记忆），后续每轮直接 `duplicate_skipped` 返回，**连占量检查都不再走**，永不重发。
- 这**正是 F03 的场景**（TP 与退出同时触发/重复用户流）：并发 TP/保护单造成的 `reduceOnly` 拒绝会把单笔失败升级成永久不可退出。

复现（真实 `_close_or_reduce`/`_reserve_exit`/`_settle_exit`/`_release_exit`，只桩掉网络）：
```
_scratch_A/out_p05.txt
PASS submit_error_propagates RuntimeError: Binance HTTP 400 reduceOnly rejected
PASS leak_reservation_still_ACTIVE status=ACTIVE remaining=1.0
PASS leak_available_is_zero 0
PASS retry_after_leak_is_blocked {'action': 'exit_noop', 'reason_codes': ['EXIT_EXCEEDS_POSITION','NO_POSITION_TO_EXIT']}
PASS permanent_block_across_rounds ['exit_noop','exit_noop','exit_noop']
PASS stable_key_retry_is_duplicate_skipped {'action':'duplicate_skipped','reason_codes':['DUPLICATE_EXIT_IGNORED']}
PASS no_release_sweeper occurrences=3
```
- 未到"完全锁死"的程度：`close_all()`（`binance_futures.py:2289`，实测其函数体内**完全不出现** `strategy_position_manager`/`_reserve_exit`/`_release_exit`），人工 emergency 全平仍可用；但自动退出通道对该 (env,account,symbol,side) 域已废，且操作员看到的 reason 是"无仓可平"而实际满仓 —— 会误导人工响应。
- 建议：`_reserve_exit` 之后所有退出路径统一 `try/finally`（`finally` 中按 `close_unconfirmed` 决定 settle/release），或给账本加"在途预留超时回收"（带上限与审计）。

### P1-2 patch 02 在 `_open()` 里引用了不存在的 `reservation_key` → NameError
`binance_futures.py:1430`（`_open` 定义于 `:1342`，开仓路径）
- 该行由 patch 02 新增（`if not chunks: self._release_exit(reservation_key, reason="NO_CHUNKS")`），但 `reservation_key` 在 `_open` 内既非局部也非自由变量，而是**全局名**（`symtable` 证明 `is_global=True`），模块内也没有同名全局 → 运行时 `NameError`。
- 静态证明：`_scratch_A/probes/p02_nameerr.py` → `UNBOUND_RELEASE_EXIT: [('_open', 1342, 'reservation_key', 1430)]`；`symtable` 输出 `reservation_key in symbol table of _open: True(global) False False`。
- 动态证明（桩掉前置网络调用，qty=0 走 `chunks` 为空的分支）：`_scratch_A/out_p03.txt`
```
===== pristine =====   NO_ERROR result=None
===== sb =====         NAMEERROR name 'reservation_key' is not defined
  File "...binance_futures.py", line 1430, in _open
```
- 影响：`_open` 原本对 qty≈0 是**有意的优雅跳过**（`return None`），补丁把它变成异常；被 `execute()` 逐 intent 的 `except Exception` 兜住，只多记一条 severity=error 的 `futures_order_error`。功能等价（都不下单），但这是**明确的代码错误 + 噪声回归**，且说明 patch 02 的对账/退出拼接未在这条路径上跑过。
- 建议：删掉该行（开仓路径没有预留可释放），或改成 `getattr` 探测。

### P1-3 patch 05 纸面退出键污染：同 symbol/side 第二次退出永久被吞
`src/agents/execution.py:327`（`_exit_idempotency_guard`），key 回退 `paper-exit:<symbol>:<side>`（`:341`），`:354` 立即 release
- 纸面路径在**没有** `client_order_id` 时用一个**稳定**回退键 `paper-exit:{symbol}:{side}`；`StrategyPositionManager._exit_keys` 只增不减（无 eviction）。
- 结果：同一 symbol/side 的**第一次**退出执行并把预留 release；此后**任何**第二次退出（含"先部分平、再平剩余"）都返回 `duplicate_skipped` 且**不再执行**。
- 复现 `_scratch_A/out_p06.txt`：
```
PASS second_exit_on_same_symbol_blocked {'action': 'duplicate_skipped', 'reason_codes': ['DUPLICATE_EXIT_IGNORED']}
PASS partial_exit_also_blocked        {'action': 'duplicate_skipped', ...}   # qty=0.3 也被吞
PASS explicit_cid_escapes_poison      None                                   # 只有显式 cid 能绕过
PASS no_key_eviction_in_manager       _exit_keys is append-only
```
- 与 live 路径**方向相反**：live 无 cid 时用 `uuid4()[:16]`（永远不重复 → 幂等检测形同虚设），纸面用稳定回退键（过度抑制 → 平不掉）。两条路径都偏离"同一语义的重复退出应被识别"的意图。
- 影响面：纸面/离线验证（`smoke_*`、影子运行）中退出链路会在第二次退出后静默失效，掩盖真实缺陷。建议纸面回退键加入"本轮意图/时间片"维度，或在该 guard 内 release 后清理 `_exit_keys`。

### P1-4 README「约束 1」的原子性**理由与事实相反**：只上 01 **并非** no-op
`docs/dispatch/patches/U02/8c66f65/README.md:18-22`
- README 称"只上 01 … 两者不会崩，但账本域仍停在密钥摘要形态，**等于补丁无效**"。
- 实测（`_scratch_A/out_p13.txt`，`account_reference` 的 `_declared_account_ref` 会读 `os.getenv("MTA_ACCOUNT_REF")`）：
```
pristine  + MTA_ACCOUNT_REF=acct-live : book.account_ref='demo:fd8e5f5989dff875'  (无变化)
sb01(仅01)+ MTA_ACCOUNT_REF=acct-live : book.account_ref='demo:acct-live'          (已改变!)
sb       + MTA_ACCOUNT_REF=acct-live : book.account_ref='demo:acct-live'  broker.account_ref='demo:acct-live' source='declared'
sb01     : broker.account_ref=<ABSENT>  account_ref_source=<ABSENT>  _exit_domain_key=<ABSENT>
```
- 即：**只上 01 就已经把账本分区切换成 `<env>:<declared>`**（因为 01 自己读环境变量，不依赖 02 传参）。真正的差别是：只上 01 时**没有** `broker.account_ref` 单一派生点、**没有** `account_ref_source` 可见标记、**没有** 03 的 `account_domain_mismatch` 启动自检 —— 也就是"改数据分区但不留任何可见性"。
- 结论：01+02 同批**仍然应该坚持**（要求本身正确），但 README 给出的理由（"无效"）与风险方向（"无影响" vs 实际"静默改分区"）都写反了，需要改写，否则 I/Q 会按错误的风险模型决定应用顺序。
- 另：`account_ref_source()` 只有在 02 里才有属性出口；01 单独存在时该函数无消费者（唯一的消费点 `binance_futures.__init__` 属 02）。

### P1-5 账本迁移建议（第 8 项）**不可执行且不完整** —— 硬结论见 §3
`README.md:47-77`

### P2-6 patch 04 的授权门**没有任何调用方**，且 `enforce=True` 是"关掉全部退出"的开关
- `filter_unbound_management` / `management_intents` 在整个仓库**只有定义、无调用**（`grep -rn` 覆盖 `sb/` 全树；patch 03 也没有接）。因此"非 OPEN 意图必须携带已绑定管理授权引用"当前只是一段可观测代码，不在执行路径上。
- `TradeIntent`（`src/agents/models.py:85`）**没有 `model_config`** → pydantic 默认 `extra='ignore'` → 调用方即使传 `management_authorization_id` 也会被**静默丢弃**（不报错），`intent_authorization_ref()` 恒为 `None`。
- 复现 `_scratch_A/out_p12.txt`：`enforce=True` 下，一个合法 `close` 意图被直接摘除（只剩 `open`），决策降级为 `open`，**该退出永远无法提交**；`enforce=False`（默认）下行为不变（仅记事件）。
- 结论：默认值安全，但这是一个"翻一个布尔就废掉所有退出"的雷；README/接口注释应显式写"在 `TradeIntent` 具备该字段前禁止 `enforce=True`"，或直接对 `enforce=True` + 无法绑定 的情形 fail-closed 抛错。

### P2-7 显式迁移与硬退出流水在生产上不可达/不可持续
- **`plan_restart` 是每轮调用**（`src/core/service.py:873`，位于 `_run_once_inner`，非一次性启动），且对任何缺 `strategy_id/ticket_id` 的仓位无条件重新登记 `self._legacy` → 操作员 `migrate_legacy()` 的结果**在下一轮被撤销**。复现：
```
after migration, pending_legacy = []
after next cycle,   pending_legacy = ['L9']      # 迁移被 plan_restart 覆盖
migrations recorded = 1
```
- `set_expectation` **无生产调用方** → `_expectations` 恒空：`plan_restart` 对有归属仓位永远报 `ALIGN_EXPECTATION`，`HardExitRecord.expected_qty_at_trigger` 恒为 0，"预期持仓"这一半命名空间在生产上没有写入者（分离在结构上成立，功能上是空的）。
- `record_hard_exit` 唯一生产调用点是人工 emergency（`service.py:1228`）→ **SL/TP 触发的硬退出不会进流水**；`migrate_legacy` 无生产调用方（无运营入口）。
- `_plan_position_restart` 每轮在 `plan.legacy` 非空时记 `legacy_position_requires_migration`（warn）→ 旧仓存续期间**每轮一条** `risk_events`（用的是 `log_risk_event` 而非 `..._once_while_open`）→ 表格膨胀与看板噪声。
- 建议：`plan_restart` 尊重已迁移记录（或迁移写回归属），旧仓告警改用 once-while-open 语义。

### P2-8 "全平护栏"是恒真式（不能作为证据）
- `strategy_position_manager.py:478` 与 `service.py:349` 两处调用都是 `assert_no_implicit_bulk_exit(context="restart", actions=())` —— **传的是字面空元组**；`RestartPlan.exit_actions` 恒返回 `()`，`as_dict()["exit_action_count"]` 是字面 `0`。
- 函数本身正确（给定真实动作会抛 `IMPLICIT_BULK_EXIT_BLOCKED`，已验），但生产路径永远不会触发它，因此"重启不产生退出动作"是**代码构造**保证（`plan_restart` 只生成 MIGRATE_EXPLICIT/ALIGN_EXPECTATION/NOOP）而非护栏保证；`exit_action_count==0` 不能当作独立证据。
- 建议：把 `plan_restart` 真实产出的 actions 传进护栏，或删除该调用以诚实表达"由构造保证"。

### P2-9 U02 的全部状态都是进程内易失
`StrategyPositionManager` 在 `TradingService.__init__` 重建（patch 03）→ `_expectations`/`_hard_exits`/`_legacy`/`_migrations`/`_exit_keys`/`_ledgers` 全部丢失。已复现迁移不可持续（P2-7）；同一机制意味着**跨重启的重复退出识别与 R09 预留不成立**（作者已自述"R09 仅在单进程锁内证明"，此处给出具体后果）。

### P2-10 冒烟注册行不在补丁里 → 按 01..06 机械应用会让统一回归**直接抛错**
- 6 个补丁都不含 `scripts/run_offline_regression.py`，实测（`_scratch_A/out_manifest.txt`）：
```
MANIFEST_RAISES: 未登记冒烟脚本: scripts/smoke_u02_strategy_position_manager.py
```
- README §SCRIPTS 已披露该登记行，但它是集成清单的一部分，I 必须一并落，否则 `_validate_smoke_manifest()` 先抛错，整条统一回归一条都跑不了。

### P3（观察项，不阻塞）
1. `account_reference(env='demo', declared_account_ref='other:acct')` → `'demo:other:acct'`（异环境前缀被再拼一次）。与 U15 `derive_account_ref` 行为一致，属口径一致而非缺陷；但无"declared 与环境不符"的拒绝。
2. `_exit_domain_key` = `demo:demo:<ref>:SYMBOL:SIDE`（env 出现两次）；与 `position_lifecycles` 唯一索引口径一致，纯外观。
3. `migrate_legacy` 对**未知** lifecycle_id 静默写入 `qty=0` 的迁移记录（无告警）→ 运营笔误不会被发现。
4. `record_hard_exit` 对 `exit_id` 无去重（append-only 语义下重复调用产生重复记录）；当前只有 emergency 调用方。
5. `FlatCloseDecision` 的 `changes_risk_profile/touches_pause_state/touches_emergency_state` 是**字面 `False` 默认值**，不构成"未改控制位"的证据；本报告改用"调用前后 `manager.as_dict()` 不变 + 纯函数性"验证（通过）。
6. `evaluate_management_request` 的 U15 绑定是**可选**的：`generation`/`expected_generation` 都不传时 `binding_reason_codes == ()`，`submit_allowed` 直接取决于 K04 契约（契约本身仍强制 env/runtime/account，故不是漏洞，但"跨账号校验"会因调用方忘记传参而静默消失）。
7. 声明账户后，同一 `release_id` + 同一分钟的 `release_observations` 新样本会被 `INSERT OR IGNORE` **静默丢弃**（`UNIQUE(release_id,environment,account_ref,observed_minute)` 更宽），`fetch_release_observations(account_ref=NEW)` 看不到（见 §3 证据）。
8. 纸面与实盘的 `lifecycle_id` 域键形状不同（`env:symbol:side` vs `env:account:symbol:side`）；因每进程一管理器，当前无串域风险。
9. `reconcile_position` 溢出时：**完全**被裁掉的预留置 `RELEASED`；**部分**被裁的仍为 `ACTIVE` 且把差额计入 `consumed_qty`（即使那部分是别人的成交）——"溢出转 RELEASED"的说法只对完全裁剪成立。

---

## 2. 独立复现：作者声称的行为（均通过）

探针：`probes/p04_ledger_core.py`（账本）、`p07_manager_semantics.py`（管理器）、`p08_r11_fault_matrix.py`（R11）、`p09_auth_binding.py` / `p10_misc.py`（契约）、`p06`（纸面）。原始输出 `out_p04/06/07/08/09/10.txt`，`PROBE_EXIT fails=[]`。

- **R09（预留守恒）**：8 线程抢 1.0（每个 0.25）→ **恰 4 成功**，`reserved=1.0`、`available=0`；所有被接受者 `reduce_only=True`、`opens_position=False`、`action=SUBMIT_REDUCE`。
- **守恒式**：`Σ(ACTIVE.remaining) + available == position_qty` 与 `available >= 0` 在 8 线程 × 4000 次随机交错的 `reserve/consume/release/reconcile` 混沌测试中**始终成立**；超量 reserve 被拒且不改状态；重复 reserve（同 id）幂等；重复 consume 不二次扣减；`consume > remaining` 被拒；release 后 consume 被拒；重复 release 幂等；未知 id 的 release/consume 均拒绝且不炸。
- **注意守恒式的**口径**：`available` 是派生值，`position_qty` 只由 `reconcile_position` 更新；`consume` **不**减少 `position_qty`，所以"某笔退出已成交"之后、下一次 reconcile 之前，`available` 会**高估**真实可减量（实测：position=1.0、consume 1.0 后仍 `available=1.0`，而交易所真值已 0.5）。集成的 `request_exit` 每轮先 reconcile，故在其调用形态下自我修正；但 `reservation_conserved()` 只证明"账本自洽"，不证明"账本 == 交易所"。若调用方传入**陈旧** `current_qty`，账本会照样为同一份仓量再开一笔预留（R09 的本质边界）。
- **reconcile**：缩小 → 溢出裁剪并返回 `RESERVATION_RELEASED_POSITION_SHRUNK`；放大 → 不释放、不扩张预留（`reserved` 不变，`available` 变大）；归零 → 全部 `RELEASED`。
- **重复退出不反向开仓**：同 key 二次 → `duplicate=True / accepted=False / submit_qty=0 / action=NOOP / opens_position=False`，且不二次占量；空仓退出 LONG→`SELL`、SHORT→`BUY`、`action=NOOP`；未知持仓侧按 `SELL`（保守）。**NOOP 结果不记忆**（同 key 在仓位出现后可以转为 `SUBMIT_REDUCE`）。
- **硬退出与预期分离**：`record_hard_exit` 不改预期（`HELD`/数量不变），写预期不抹硬退出；6 线程 × 50 次并发下 300 条硬退出**无丢失**、revision 单调（299）。反向写不抹硬退出 ✓。无回滚 API（append-only）✓。
- **旧仓显式迁移**：混合新旧仓 → 旧仓 `MIGRATE_EXPLICIT`（reason `LEGACY_POSITION_REQUIRES_EXPLICIT_MIGRATION`）、有归属无预期 → `ALIGN_EXPECTATION`；`exit_action_count == 0`；`implicit_bulk_exit is False`；`migrate_legacy` 缺 target/actor/reason 抛 `ValueError`；迁移后 `pending_legacy` 清空。**迁移标记丢失后重启不会隐式批量退出**（新管理器只重新标记 `MIGRATE_EXPLICIT`，`exit_action_count` 仍为 0）——"重启不隐式批量退出"**成立**。
- **过期授权不阻断 UNKNOWN 收口**：`now_ms=issued+ttl+5s` → `submit_allowed=False`（`AUTHORIZATION_EXPIRED`）而 `reconcile_allowed=True`；接 U04 `execute_unknown_reconcile` → `queried=1 resolved=1 pending=0 submit_calls=0`，`outbox.acknowledge` 1 次，`reason_codes=(NO_REPOST_BY_ORIGINAL_CLIENT_ID,)`。未来时间戳 → `AUTHORIZATION_FROM_FUTURE` 且提交被拒。
- **契约逐条拒绝**（`out_p09/p10`）：OPEN / 跨环境 / 异账户 / 异生命周期 / 反向持仓侧 / 运行代不符 / 超授权上限 / 超当前仓位 / 净敞口 +0.5 / 缺 `requested_net_delta` / 非正数量 / 缺数量 / 陈旧 revision / nonce 重放 / 撤最后保护（`CANNOT_REMOVE_LAST_PROTECTION`）/ 保护目标不符 / 畸形授权（三条许可全 False）—— 全部拒绝且 reason 正确。签发侧对上界/时效/运行代缺失 fail-closed（`MAX_REDUCE_QTY_NON_POSITIVE`/`AUTHORIZATION_TTL_NON_POSITIVE`/`AUTHORIZATION_ISSUED_AT_INVALID`/`MANAGEMENT_AUTHORIZATION_UNBOUND`）。
- **R11 故障矩阵**：自建 60+ 名称（登记表 16 个 + 代码实际产出的 `PROTECTION_FAILED/PLACE_FAILED/QUERY_FAILED/空串` + 角色卡未列但"看似保护类"的 `STOP_LOSS_FAILED/SL_FAILED/TP_FAILED/PROTECTION_SYNC_FAILED/PROTECTION_MISSING/MIRROR_FAILED/PROTECTION_MIRROR_STALE/ALGO_ORDER_QUERY_FAILED/NAKED_POSITION/POSITION_UNPROTECTED/LIQUIDATION_IMMINENT/MARGIN_CALL` + 健康/传输/未归类 + 垃圾名/异常对象/None）→ **除 `PROTECTION_FAILED`/`PLACE_FAILED` 外无一放行**；这两个真保护失败**仍能兜底**（`allowed=True, action=CLOSE_ALL`）；大小写/空白归一化不能把健康故障偷渡进来，含 `PROTECTION_FAILED` 子串的未登记名**不被**授权；`operator_emergency=True` 恒允许且**不改控制位**（调用前后 `as_dict()` 逐字相同，纯函数）。授权类只有 `{PROTECTION}`（白名单，非黑名单）。
- **作者自测**：`tests/integration/test_strategy_position_manager.py` → `PASS=115 FAIL=0`（独立复跑确认）。`scripts/smoke_u02_strategy_position_manager.py` 结构正确：正向 115/0，两个**负向对照**（N1 打回 sha16 漂移 → 2 失败；N2 让重复退出变成开仓 → 4 失败）都必须失败，脚本才返回 0 —— 输出的 `FAIL=` 是**变异检出证据**，不是缺陷（我最初误读，特此更正）。

---

## 3. 第 8 项：账本口径 / 迁移建议 —— **硬结论**

### 3.1 未声明账户时与 base 逐字一致 —— **成立**
`probes/p01_account_ref.py`（`out_p01.txt`）：`account_reference(api_key, env)` 在未声明时 == base 实现（`<env>:sha256(api_key)[:16]`，含空串/纯空白 `declared_account_ref` 回退）；`pristine` 与 `sb` 对同一 key 给出**逐字相同**的值；未声明时密钥轮换会漂移（`demo:46df…` vs `demo:df21…`），声明后轮换不变（`demo:acct-live`）。`account_ref_source()` → `legacy-key-digest` / `declared`。**这条没有问题。**

### 3.2 声明 `MTA_ACCOUNT_REF` 后，旧数据**不幂等** —— **风险成立，且已复现**
用真实 `AuditDatabase`（真实 schema + 真实唯一索引）复现（`probes/p11_account_ref_migration.py`，输出 `out_p11.txt`）：
```
INFO post-declare fills by domain: [('demo:acct-declared', 2), ('demo:fd8e5f5989dff875', 3)]
PASS K03_key_splits_into_two_domains        # 同一账户历史被切成两个域
PASS duplicate_rows_across_domains [('t0', 2), ('t1', 2)]   # 同一笔成交在两个域各一行
```
- K03 幂等键是 `(environment, account_ref, income_type, tran_id)`，**分区维度里含 account_ref**；声明账户 = 换一个分区 → 同一个 `trade_id`/`tran_id` 在库里成为**两行**，唯一索引拦不住（它们本来就"合法地"分布在不同分区）。
- 后果：成交/生命周期/收入会被**重复统计**；`income_coverage` 在新域里显示"未覆盖"→ 回补再把同一笔收入入账一次；`position_lifecycles` 的 `idx_active_position_lifecycle` 允许"同标的同侧"在新旧域各有一个 ACTIVE 生命周期 → 该开的仓/该收的口会被做两遍。
- 因此："**声明账户后旧数据不可幂等**" 是**真实且已被 schema 层证明的**结论，不是理论风险；正确做法是"**先迁移、再声明**"，而不是 README 暗示的"声明后按需迁移"。

### 3.3 README 的迁移 SQL **按字面执行会失败，且会把库留在半迁移状态** —— **不充分**
README 第 3 步建议逐表 `UPDATE ... SET account_ref=new WHERE environment=env AND account_ref=old`。逐字执行（同一 `probes/p11`）：
```
execution_fills       → IntegrityError: UNIQUE constraint failed: execution_fills.(environment,account_ref,trade_id)
position_lifecycles   → IntegrityError: UNIQUE constraint failed: position_lifecycles.(environment,account_ref,symbol,position_side)
trade_reconciliation_jobs → ok
release_observations  → ok（仅因新域那一分钟样本已被丢弃，见 3.5）
INFO after abort: fills by domain = [('demo:acct-declared', 2), ('demo:fd8e5f5989dff875', 3)]
```
- 触发条件就是"声明后先跑了一阵"或"迁移中途失败后重跑"——即**新域已存在同 key 行**。此时 `UPDATE` 撞唯一索引 → 事务回滚，**前两张表没迁，后两张表已迁**：`非原子、不可幂等重入、且失败后状态更难判断**。README 没有给冲突检测/去重步骤，也没有给"迁移事务边界/校验和/回滚"。
- 建议（把这条登记给 U13 时必须补上）：① 先按新 key 查重，冲突行做**语义去重/合并**（保留哪一行要显式裁决，不能靠 UPDATE）；② 迁移放在单一事务内 + 迁移前后行数/关键列校验和；③ 幂等：可重复执行（`UPDATE` 与去重都写成可重入）；④ 迁移**先于**声明执行，或声明与迁移同批发布。

### 3.4 表清单**不完整，且含一张不存在的列**（schema 逐表核对）
以真实 schema 反查（`PRAGMA table_info` 交集）：
```
tables_with_account_ref = [execution_fills, income_conflicts, income_coverage, income_events,
                           position_lifecycles, release_observations, trade_reconciliation_jobs]
READMNE_LIST_VERDICT MISSING_FROM_README=['trade_reconciliation_jobs']
                     NOT_A_TABLE=['lifecycle_fill_allocations']
```
- **漏了 `trade_reconciliation_jobs`**：它有 `account_ref NOT NULL`，且 `binance_futures.py:2926` 的"同开仓已收口"护栏查询 `WHERE environment=? AND account_ref=? AND symbol=? AND position_side=? AND status='CLOSED'` **按 account_ref 过滤** → 未迁移的 job/历史行在新域里**查不到**，该护栏失效 → 会为同一笔开仓**重复生成生命周期/outcome**。这正是 A 关心的账本重复记账路径。
- **`lifecycle_fill_allocations` 根本没有 `account_ref` 列**（靠 `lifecycle_id` FK 关联）→ README 把它列为待 `UPDATE` 的表是**无效指令**，会让迁移脚本报错或静默空转。
- README 自称"本包不新增迁移、不改表结构"——**这一条我在 §4 独立确认成立**；但它与"迁移清单"是两件事：清单漏表不是 schema 变更问题，是迁移方案完整性问题。
- 另外 `income_*` 表确实另有 `account_ref_norm` 归一化列（由 K03 `strip+casefold` 口径决定，`income_reconciliation.py:1385/1470/1484/1494` 用 `normalize_key_part(self.account_ref)`）→ README 提醒"必须同批更新 `account_ref_norm`"**正确且必要**；`release_manifest.json`（磁盘工件）里的 `account_ref` 与 `db.set_release_context(account_ref=...)` 的进程内值也不在表清单里，历史发布清单会与新域分叉（K01 身份对照会看到不一致）。

### 3.5 额外发现：声明账户会让**发布观察证据静默丢失**
`release_observations` 的幂等键含 `account_ref`，且 `log_release_observation` 用 `INSERT OR IGNORE`：
```
INFO release_observations under NEW before=1 write_accepted=False
PASS release_observation_silently_dropped_after_declare
```
同一 `release_id` 批次内跨域声明时，已观察过的那一分钟在新域**写不进去且返回 False 被忽略**（`fetch_release_observations(account_ref=NEW)` 也就取不到）→ 发布门禁的"观察新鲜度"证据会出现空洞。建议：把 account_ref 从该唯一键中移除，或在声明切换时显式开新 `release_id`。

### 3.6 对 README 给出的"备用方案"的评价
README 末尾给了退路：把"声明优先"改成**仅显式传参**时生效（不读 `MTA_ACCOUNT_REF`）。**这是更安全的默认**，理由是 §3.2/§3.5：环境变量一旦被运维/`.env`/部署脚本设置，01 就会在**无人决策**的时刻切换数据分区（且如 P1-4 所示，仅上 01 也已经生效）。建议采纳该退路，或至少在 `account_ref_source()=='declared'` 时于启动自检里把"声明是否已伴随迁移完成"也做成一条**阻断级**（而非 warn/critical 事件）的检查。

---

## 4. 全量回归 delta（独立确认）

方法：`_scratch_A/run_delta.py <tree>` —— 直接 `ast` 解析**该树自己的** `scripts/run_offline_regression.py` 的 `SCRIPTS` 清单，用与 `main()` **相同**的 env（`EXECUTION_MODE=paper`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、剔除 `MTA_PROFILE` 与 `*BINANCE/SENSENOVA*KEY/SECRET/TOKEN*`）逐条**非 fail-fast** 执行并记录退出码。

- 两棵树都先补上 README §SCRIPTS 的登记行（否则 `_validate_smoke_manifest()` 直接抛错，见 P2-10），清单 **75** 条（74 + U02）。
- 结果（`out_pristine.txt` / `out_sb.txt`）：
```
pristine : manifest_entries=75  FAILED=1  ['scripts/smoke_windows_service_plan.py']
sb (01..06): manifest_entries=75  FAILED=1  ['scripts/smoke_windows_service_plan.py']
新增失败集合差集 = {}          # 与作者声称一致
```
- 该唯一失败是**既有 KI02 且为环境性**：`windows_service.py:190` 要求 `<项目根>/.venv/Scripts/python.exe` 存在，沙箱里没有真的 venv（两棵树报同一错误，路径只差树名）。与 U02 无关。
- 受影响的既有冒烟单跑复核：`smoke_trade_reconciliation / smoke_decision_pipeline / smoke_service_lifecycle / smoke_close_reconcile / smoke_fault_matrix / smoke_contracts_v3 / smoke_entry_readiness / smoke_ui` 在 `sb` 下均 PASS（`smoke_ui` 的通过**必须**以 06 已应用为前提，见 §5）。
- **口径声明（对 delta 的效力边界）**：基线 `8c66f65` **已包含**作者的独占文件（`strategy_position_manager.py`/`management_authorization.py`/独占测试/冒烟包装器），所以本 delta 只隔离 **01..06 六个共享适配补丁**的集成回归，**不覆盖** U02 独占模块本身的新增行为。独占模块的行为证据是本报告 §2/§3 的探针，外加作者自带 `PASS=115 FAIL=0`（我已独立复跑）。

---

## 5. 六个补丁逐条意见

| 补丁 | 结论 | 依据 / 意见 |
|---|---|---|
| **01** `trade_reconciliation.py` | **有条件通过** | 未声明时与 base 逐字一致（实测）；`account_ref_source` 标记可用；`derive_account_ref` 口径一致。**问题**：它自己读 `os.getenv("MTA_ACCOUNT_REF")`，所以"只上 01 无效"是错的（P1-4）；此补丁是**唯一**能在无人决策时切换账本分区的地方，应把"仅显式传参"作为默认。 |
| **02** `binance_futures.py` | **CHANGES_REQUESTED** | 含 **P0-1**（预留泄漏 → 永久不可退出）与 **P1-2**（`_open` 里 `reservation_key` NameError）。另外：无 `client_order_id` 时用 `uuid4()[:16]` 作 exit_key → 幂等检测对非 guardian 的所有退出**形同虚设**（guardian 的 `mta-g-<sha24>` 才有效）；`_settle_exit` 对 LIMIT/未确认也按 `submit_qty` 全额 consume（把"订单被接受"当"仓位已减"），叠加 §P0-1 的裁剪会把预留留在 ACTIVE。 |
| **03** `service.py` | **通过（附意见）** | 账户域绑定/自检、管理器注入、重启只分类不退出、emergency 走同一判定入口，均与 README 描述一致且实测成立；`_bind_account_domain` 不改 broker 的 `account_ref`（避免第二身份），正确。**问题**：`_plan_position_restart()` 放在**每轮** `_run_once_inner` → 撤销 `migrate_legacy`、每轮记旧仓告警（P2-7）；`assert_no_implicit_bulk_exit(actions=())` 是恒真式（P2-8）。 |
| **04** `decision_pipeline.py` | **通过（附意见）** | `enforce=False` 默认保持既有行为（实测）；`priority_order` 稳定、只改次序；摘除不降级为 open（实测 `action` 落到 `open`/`no_trade` 而非把管理意图变开仓）。**问题**：整段门**没有调用方**；`enforce=True` 在当前 `TradeIntent` 下等于关掉全部退出（P2-6）。 |
| **05** `agents/execution.py` | **CHANGED（P1-3）** | 管理优先 + 纸面同序的意图正确；但回退 exit_key 用稳定键且 `_exit_keys` 无淘汰 → 同名标的第二次退出永久 `duplicate_skipped`、不再执行（P1-3）。纸面"注册后立即 release"本身合理（同步成交），问题只在键的生命周期。 |
| **06** `dashboard/app.py` | **必要伴随** | 实测：只上 01+02（无 06）→ `smoke_ui` `FAIL 全部事件类型都有中文映射 缺映射：['futures_duplicate_exit_skipped']`；01+02+03（无 06）→ 缺 `['account_domain_mismatch','futures_duplicate_exit_skipped','legacy_position_requires_migration']`。README"06 是必要条件"**成立**。属纯展示映射（缺映射只影响该断言与看板可读性，不影响交易语义）。 |

**"01+02 原子单位"核实**：**要求成立，理由需改写**。
- 只上 **02**：`ImportError: cannot import name 'account_ref_source' from 'exchange.trade_reconciliation'`（`out_sb02_import.txt`）→ 直接崩，README 描述**正确**。
- 只上 **01**：**不崩，而且不是"无效"** —— 声明环境下账本分区**已经切换**（`out_p13.txt`），但没有 02 的单一派生点/`account_ref_source`、没有 03 的启动自检 → **静默改分区**。README 的理由与风险方向需按此改写。

**"无 schema 变更"核实**：**成立**。6 个补丁内无 `CREATE TABLE/ALTER TABLE/INDEX/_apply_migration/schema_migrations` 任何字样；两棵树 `current_schema_version()` 均为 **17**，`account_ref` 表集合逐字相同（§3.4 输出）。因此 U13 那条"迁移版本断言"确实无需伴随补丁。

---

## 6. 未覆盖 / 无法离线验证（明说）

1. **无真实交易所**：所有 Broker 相关探针都用桩（`__new__` + 方法替换），P0-1/P1-2 是**代码路径级**证明，未在 demo 环境实盘验证；未下单、未撤单、未联网。
2. **P0-1 的触发概率**未量化：只证明了"任一次占量后的异常都会泄漏"，没有统计真实 `_submit_standard_order` 抛错频率。
3. **P1-2 的可达性**：qty≈0 的 open 意图是否能走到 `_open` 取决于上游过滤（未穷举 `decision_pipeline`/`risk_guard` 的产出），我证明的是"该分支在补丁前是优雅跳过、补丁后是 NameError"。
4. **R09 仅单进程证明**（与作者自述一致）：未做多进程/多实例争抢；`_release_exit`/`_settle_exit`/`_reserve_exit` 无实盘路径覆盖（作者自述），本报告只证明了它们的账本副作用。
5. **`TradeIntent.management_authorization_id` 的端到端集成未验**（该字段当前不存在，见 P2-6）。
6. **U15/K01 本体**未审：只审 U02 使用的接口（`derive_account_ref`/`mint_runtime_generation`/`verify_account_binding`/`RuntimeGeneration.account_ref`）；`account_ref_stable` 等语义未展开核。
7. **迁移建议的修复版**未实现/未验证（属 U13），本报告只证明现方案不可执行与不完整。
8. 未读 `.env`/`data/`/密钥/账户标识；未启动真实服务；未修改任何 tracked 文件（全部产物在 `reviews/U02/A/r1/_scratch_A/`）。

---

## 7. 证据索引

| 文件 | 内容 |
|---|---|
| `_scratch_A/out_p01.txt` | account_ref：未声明逐字一致 / 声明后域切换 / 轮换解耦 |
| `_scratch_A/out_p03.txt` | `_open` NameError（pristine 优雅跳过 vs 补丁抛错） |
| `_scratch_A/out_p04.txt` | 账本守恒：8 线程恰 4 成功、混沌不变量、consume/release/reconcile 边界 |
| `_scratch_A/out_p05.txt` | **P0-1 预留泄漏 → 永久不可退出**（含稳定 key 变体） |
| `_scratch_A/out_p06.txt` | **P1-3 纸面退出键污染** |
| `_scratch_A/out_p07.txt` | 管理器语义：重复退出/空仓/硬退出并发/重启计划/迁移不可持续 |
| `_scratch_A/out_p08.txt` | **R11 故障矩阵 60+ 名称** |
| `_scratch_A/out_p09.txt` `out_p10.txt` | K04 契约逐条拒绝 / 过期与收口分离 / U04 联测 / U15 域绑定 |
| `_scratch_A/out_p11.txt` | **第 8 项：域切裂、重复行、迁移 UPDATE 撞唯一索引、清单漏表** |
| `_scratch_A/out_p12.txt` | `enforce=True` 关掉全部退出 |
| `_scratch_A/out_p13.txt` | **只上 01 已切换账本分区**（README 约束 1 反证） |
| `_scratch_A/out_manifest.txt` | 缺登记行 → 统一回归入口抛错 |
| `_scratch_A/out_ui2.txt` | 缺 06 → `smoke_ui` 缺中文映射（逐条缺哪个） |
| `_scratch_A/out_pristine.txt` `out_sb.txt` | **全量回归 delta**（75 条逐条退出码 + 失败输出尾巴） |
| `_scratch_A/out_test.txt` `out_smoke_u02*.txt` | 作者自测 115/0；冒烟含两个负向对照 |
| `_scratch_A/probes/p01..p13*.py` | 全部探针源码（可复跑） |

# U01 初审报告（B 角色 / 数量·费用边界 / r1）

- 审查对象 head: `c290c50639291a23462e95bdfd0667f268848a82`（分支 `task/U01`），基线 `ffdab44`
- 审查工作区: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U01\B\r1`（独立 detached，全程只读）
- 解释器: `C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`，无 pytest）
- 探针目录: `_scratch_B/`（全部原始 stdout 重定向保留）；沙箱（**未 tracked**，用于施加作者补丁）: `_scratch_B/sandbox`
- 本报告不采信作者"全 PASS"结论；所有判定均附自建反例与原始输出文件

## 0. 结论

**CHANGES_REQUESTED**

理由：U01 的**契约侧**主张（三状态独立、资金费守恒、Decimal 精度、空窗口≠零、"未知不是零"）经独立复现**成立**；
但**执行侧**存在 2 处 P0（离线回归门禁红 + 真实存储路径的幂等重放不可用，且修好它会引发静默丢数）
与 9 处 P1（含任务卡明列的"跨 profile 覆盖"关键风险、`fetch_income_coverage` 无法按窗口查询导致的"缺失当零"、
分页"零丢失"不可证、迟到修订被吞、重复计费守卫形同虚设、手续费交叉核对误报与不核对 REALIZED_PNL、
隔离无持久化审计、`income_type` 枚举治理这一 U01 自有缺口未闭合）。
作者"全 PASS"的 78/44 中，唯一覆盖真实存储层的 `[9]` 段在本次交付中是 **SKIP**。

## 1. 审查与证据索引

| 文件 | 内容 |
|---|---|
| `_scratch_B/p01_pagination.txt` | 分页/端点/跨页重复/越窗/limit 边界 |
| `_scratch_B/p02_idem_revision.txt` | content_hash 双口径、幂等、迟到修订、守恒、精度 |
| `_scratch_B/p05_sandbox_store.txt` `p06_sandbox_store2.txt` | **沙箱=已应用 01..04** 的真实 `AuditDatabase` 端到端 |
| `_scratch_B/p07_attr_alloc.txt` | 守恒/分摊/迟到/会话/精度 反例 |
| `_scratch_B/p08_readonly_rv.txt` | `query_review_library` 只读性（8 种写原语拦截）+ RV02 触发来源矩阵 |
| `_scratch_B/p09_tx_env.txt` | persist 事务边界/环境域绑定/隔离持久化/income_type 枚举 |
| `_scratch_B/p10_session_edges.txt` `p11_crosscheck.txt` `p12.txt` | 会话勾稽边界（误报/矛盾/分歧不核对） |
| `_scratch_B/r01..r04.txt` | 作者两份 test + 两个 smoke：`PASS=78/44`，其中 `[9]` SKIP |
| `_scratch_B/r05_offline_regression.txt` | **`scripts/run_offline_regression.py` 在 head 上直接失败** |
| `_scratch_B/r06_policy_02only.txt` | 只应用补丁 02 会让 `check_binance_request_policy.py` 失败 |
| `_scratch_B/collateral_*.txt` | 10 个既有冒烟独立复跑，全部 exit 0（补丁未应用状态下无回归） |

补丁可应用性：四个补丁在仓库根 `git apply -p1 --directory=<沙箱>` **全部干净应用**（`--check` 通过）；
r1 工作区 tracked 文件始终保持 `git status --short` 仅 `?? _scratch_B/`。

## 2. 问题清单

### P0-1 新增冒烟未登记 → 统一离线回归入口在 head 上直接失败

- 证据：`_scratch_B/r05_offline_regression.txt`
  ```
  RuntimeError: 未登记冒烟脚本: scripts/smoke_u01_income.py, scripts/smoke_u01_review_statistics.py
  ```
- 位置：`scripts/smoke_u01_income.py`（新增）、`scripts/smoke_u01_review_statistics.py`（新增）；
  校验逻辑 `scripts/run_offline_regression.py:95-118`（`smoke_*.py` 全量发现集必须 ⊆ `SCRIPTS` ∪ `EXCLUDED_SMOKE_SCRIPTS`）
- 我的复现：`python scripts/run_offline_regression.py` → exit 1（在跑任何用例之前就 raise）
- 加重情节：两个 smoke 的 docstring 明确声称"保证统一回归 `scripts/run_offline_regression.py` 能执行到同一份断言"，
  与事实相反；且 `scripts/run_offline_regression.py` 是 README §1 登记的统一回归入口（U00 独占路径）。
- 处置建议：作者必须提交第 5 个补丁（把它俩登记进 `SCRIPTS`，或在 `EXCLUDED_SMOKE_SCRIPTS` 写明排除理由），
  交 I 应用；在此之前 head 不可作为集成候选。

### P0-2 `sync_income_window(existing=<Mapping>)` + 真实存储回读：崩溃，且修好后会静默丢数

两个面必须一起修（只修前一个会让后一个立即变成数据丢失）。

**(a) 崩溃**：`src/exchange/income_reconciliation.py:301-304`
```python
if isinstance(existing, Mapping):
    existing_events: list[Any] = list(existing.values())   # key->content_hash 的 hash 字符串被当事件
```
- `IncomeReconciliationBook.load_existing()`（同文件 `:910-920`）返回的正是 `dict[键文本, content_hash]`，
  这是文档化/唯一可用的"真实库重放"入参；`values()` 是 `str`，随后 `ingest_income` → `_as_event(str)` → pydantic
  `ValidationError`。
- 复现 `_scratch_B/p05_sandbox_store.txt` `[A]`/`[5]`（沙箱=已应用 01..04，真实 `AuditDatabase`）。

**(b) content_hash 双口径 → 重放被判"内容冲突"并清空事件集**：
`docs/dispatch/patches/U01/author-2c73c9e/01_audit_database_income_store.diff:67-76` 在适配层**重算**了一套指纹
（`environment` 用 `.lower()`、字段集/大小写规则与 K03 不同），而契约侧是 `k03_income.py:114-126 income_content_hash`。
两者对同一条事件产出**不同**摘要（实测 `d829e493…` vs `0ea6ce24…`，`_scratch_B/p02_idem_revision.txt [1]`；沙箱端到端 `p05 [4]`）。
- 后果链（`p02 [2]` / `p06 [B]`）：`ingest_income(page, book.load_existing())` 把**真重复**判为
  `INCOME_ID_CONFLICT` 隔离；而 `sync_income_window:366-375` 的政策是"冲突键整体退出 `all_events` 与 `inserted`"，
  于是**重放会把上一轮已确认的事件从窗口事件集里抹掉**——"重复拉取不重复入账"变成"重复拉取抹掉已入账"，
  且窗口金额真值随之丢失。
- 反证（幂等键路径本身是对的）：`existing=list(上轮 sync.events)` → `duplicates=(a,b) quarantined=0`（`p06 [C]`）。
  即缺陷只在"真实库回读"这一条链路上，而契约侧代码是正确的，问题出在补丁 01 自造指纹。
- 处置：适配层必须直接调用 `core.contracts.income_content_hash`（单一真值源），不得自行派生；`existing` 若是
  `key->hash` 映射应原样透传给 `ingest_income`，不得取 `values()` 当事件。
- 加重情节：作者测试 `[9] 真实存储层` 在本次交付中 **SKIP**（`_scratch_B/r01_test_income.txt` 末尾），
  故 `PASS=78` 完全没有覆盖上述路径；`book.load_existing()` 只被用于 `len(...)==4`，从未回灌。

### P1-1 跨 environment / profile 的覆盖证据错配（任务卡明列关键风险"跨 profile 覆盖"）

- `IncomeReconciliationBook.persist`（`income_reconciliation.py:873-908`）用 `event.environment` 写事件行，
  却用 `str(self.environment).lower()` 写 `income_coverage`。
- 复现 `_scratch_B/p09_tx_env.txt [2]`：book 以 `environment="live"` 构造、sync 以 `environment="demo"` 运行时，
  demo 窗口的覆盖证据被写成 **live 的**覆盖证据（并因 `UNIQUE(env,acct,type,start,end)` 覆盖同窗口 live 记录）。
- 危害：Demo/Shadow 窗口的"确认为空"可以把 Live 同窗口标成 `COMPLETE + 0 事件`，直接通向"缺失当零"；
  反之 live 覆盖被 demo 结果覆盖后，审计链上无法分辨证据来自哪个环境域。
- 修复方向：coverage 行必须用 `sync` 的（或显式校验一致后的）环境域，并在不一致时结构化拒绝。

### P1-2 `fetch_income_coverage` 无法按窗口查询 → 消费者必然"缺失当零"

- 补丁 01 的 `fetch_income_coverage`（diff `:125-135`）签名只有 `(environment, account_ref, income_type)`，
  SQL 是 `ORDER BY end_ms DESC LIMIT 1`。
- 复现 `_scratch_B/p06_sandbox_store2.txt [F]`：两个窗口（W1 有 1 笔 / W2 确认空）落库后，
  接口只返回 `end_ms` 最大的 W2（`COMPLETE, watermark=end`）。
- 危害：任何"我这个区间到底有没有资金费/费用"的判定都会拿到**任意一个**（最新的）窗口证据；
  9 个 GAP 窗口 + 1 个最新 COMPLETE 空窗口 → 读成"全期已证明为零"。这正是否定项要求的"缺失当零杜绝"。
- 修复方向：提供按 `[start,end)` 或按区间列表读取覆盖（并允许聚合为"区间内全部窗口均 COMPLETE 且无缺口"）。

### P1-3 分页"零丢失"不可证明：缺跨页单调/完整性交叉校验，`overlap_recheck` 从未被消费

- `sync_income_window` 的分页只做三件事：短页终止、`len(rows)==page_limit` 时页内非降序校验、
  `seen_page_ids` 完全相同页去重（`income_reconciliation.py:338-361`）。`plan_next_page` 返回的
  `overlap_recheck=True` / `next_start_ms` **从未被使用**，循环只用 `fetch_page(page_no)`。
- 复现 `_scratch_B/p01_pagination.txt [H]`：满页 `limit=2`、三行同毫秒，服务端第 2 页重排导致一行被跳过 →
  结果 `coverage=COMPLETE`、`reason_codes=()`，**静默丢行**。
- 对照 `[I]`（页内乱序）能正确判 `PAGE_NOT_SORTED`+GAP，`[J]`（max_pages 触顶）能判 `PAGE_CAP_EXCEEDED`+GAP：
  说明防线只覆盖"页内乱序/无进展/触顶"，**不覆盖"页间丢行"**——而官方 `page` 翻页在并发写入下并不保证稳定。
- 处置：至少补一条跨页单调性/连续性交叉校验（相邻页 `min(time)` 不得小于上一页 `max(time)`-重叠区除外），
  或真正实现 `overlap_recheck` 的重叠复读；否则不得宣称"零丢失"。

### P1-4 迟到修订：同 `tran_id` 金额被改高时被吞掉

- `k03_income.py:734-766 apply_late_income` 的 `revision_id` 只哈希 `(outcome_id, source_event_id, closed_at_ms)`，
  而 `source_event_id = idempotency_key_text(event)` 不含金额/内容。
- 复现 `_scratch_B/p07_attr_alloc.txt [6]`：同 `tran_id` 从 `-0.40` 修订到 `-9.90` → 同一个 `revision_id`、
  `first_application=False`、`ledger.revisions` 仍只有 1 条 → **修订被当成重放吞掉**。
- 同时 `sync_income_window` 的默认政策把"同键不同内容"整键移出事件集（`:366-375`，`p01 [B]`），
  所以金额真值在窗口聚合里也一起消失。
- 结论：`new_closed_outcome_created=False` 成立（已验证），但"能修订"不成立——实现只能在
  "重放去重"和"保留修订"之间二选一，当前选了前者且没有给后者留出口。
  需在 `OutcomeRevision` 中区分 `LATE_REVISION_ALREADY_APPLIED`（同内容重放）与
  "同键内容变更"（应新修订 + `source_hash` 变化），并把策略登记到 K03（新增 reason code，属小版本演进）。
- 附带：`REASON_LATE_REVISION_DUPLICATE`（`income_reconciliation.py:84`）是**死常量**，全模块无引用。

### P1-5 重复计费守卫形同虚设（文档声称"结构化拒绝"，实测基本不拦）

- `assert_no_double_count`（`:745-762`）：① **未被 `reconcile_session` 调用**
  （`"assert_no_double_count" in reconcile_session.__code__.co_names` → `False`，`p07 [8]`）；
  ② 判定是 `kind.lower() in additive`，即只拦**字面名为** `commission`/`realized_pnl` 的分量键。
- 复现 `p07 [8]`：`commission` → 拦截；`commission_income_usdt` / `income_fees` / `net_fees_from_fills_usdt`
  → **全部放行**。而 `reconcile_session` 根本不接收 `components` 入参（`TypeError`），
  所以该守卫只能由调用方自愿调用，且用真实字段名时无效。
- 作者测试只用了 `{"commission": …}` 这一个恰好被拦的键名，属于"测试贴合实现"。
- 建议：把守卫改为"凡名称含 commission/realized_pnl 的分量键一律拒绝"（白名单式反向判定），
  并在 `reconcile_session` 入口对 `fills`/`income_events` 的重复来源做一次显式核对。

### P1-6 手续费交叉核对：误报 MISMATCH、`balanced` 与 `status` 自相矛盾、REALIZED_PNL 不核对

- 位置：`income_reconciliation.py:704-726`。
- (a) **误报**：触发条件是 `net_fees is not None and (commissions_income or realized_income)`，
  但比较对象只有 `commissions_income`。当收入侧只有非零 `REALIZED_PNL`、没有 `COMMISSION` 行时，
  会用**空的**手续费收入(0)去比 fill 手续费(-0.70)。复现 `_scratch_B/p11_crosscheck.txt [1b]`：
  `status=MISMATCH`（`FILL_INCOME_FEE_MISMATCH`）——这是极常见配置（按类型/窗口取收入时很容易没有 COMMISSION 行）。
- (b) **自相矛盾**：`balanced` 在 `:709-710` 就算完（`unexplained==0 and not reason_codes`），
  而交叉核对的 reason 在 `:718-725` 才追加 → 实测可同时得到 `status=MISMATCH` 与
  `balanced=True` + `WALLET_IDENTITY_BALANCED`（`p12 [1c]`）。
- (c) **REALIZED_PNL 不核对**：docstring 写"`REALIZED_PNL` 同理"，但 `income_realized_usdt` 只被塞进
  `fee_cross_check` 字典、**从未与 `realized_trade_pnl_usdt` 比较**。复现 `p12 [2d]`：
  fill 侧 realized=0、income 侧 realized=+5.00（差 5.00），`balanced=True`、`status=MATCH`、无任何 reason code。
- 说明：作者"COMMISSION 收入只做交叉核对、不重复计入"这一**主结论成立**
  （`components` 无 `income_commission_usdt`；把 income 手续费重复塞进 `fills` 会让 `unexplained=+0.70`、`balanced=False`，`p07 [8]`）；
  但"`unexplained=0` 是真对齐"只在**没有 REALIZED_PNL 分歧**时成立。

### P1-7 隔离（quarantine）无持久化审计，`persist()` 不报告丢弃

- 补丁 01 只建 `income_events` / `income_coverage` 两表，无冲突/隔离表；
  `IncomeReconciliationBook.persist`（`:873-908`）只写 `sync.events`，隔离行既不落库也不进返回值。
- 复现 `_scratch_B/p09_tx_env.txt [3]`：含 1 条内容冲突的窗口 → `persist` 返回 1（静默少了 1 条），
  `income*` 表只有两张，`quarantine` 只存在于内存 `sync` 对象。
- 危害：与"同键不同内容必须隔离并告警"（fixture `expected_invariants`）相比，**告警没有落点**；
  进程重启后无法审计曾发生过冲突；由于冲突键被整体移出事件集，窗口金额少算且**库内无痕迹**。
- 建议：新增 `income_conflicts`（或 `income_events.status='QUARANTINED'` 行）并让 `persist` 返回/落库隔离清单。

### P1-8 `income_type` 枚举与大小写治理（README §12 明确归 U01）未闭合

- README §12 下游缺口表两处点名 U01：`income_type 枚举/大小写治理与覆盖分裂（r3 仅做键归一化）`；
  任务卡 U01 关键风险亦含"跨 profile 覆盖"。
- 实测（`_scratch_B/p09_tx_env.txt [4]`）：
  - `canonical_income_type`（`:158-160`）只做 strip+upper，**无枚举校验**：`'FUNDINGFEE'`、`'未知类型'`
    都会作为**独立收入族**入库/建覆盖率；模块定义 5 个类型常量却从不用于校验。
  - 补丁 01 的 `insert_income_events` 只 `.lower()` 了 environment，**未归一化 `income_type`/`tran_id`**；
    实测 `("DEMO","funding_fee","a")` 与 `("demo","FUNDING_FEE","A")` 在库里成为**两行**
    （`p06 [D]`）——DB 层唯一约束与 K03 的 `strip+casefold` 键**不等价**，即"覆盖分裂"未治理。
- 建议：入库前落归一化键列（或直接存 `idempotency_key_text`），并对 `income_type` 设显式枚举 + 未知类型
  显式降级策略（拒绝或隔离），把结论写进 K03 小版本。

### P2-1 越窗事件未校验：`t == end` 的行被收进本窗口

- `window_contains` 在 `income_reconciliation.py:148-150` 与 `k03:769-771` 都定义了 `[start,end)` 语义，
  但**摄入路径从不调用**；`normalize_income_row` 也不校验 `event_time_ms ∈ [start,end)`。
- 复现 `_scratch_B/p01_pagination.txt [C2]`：`time == end` 的行进入 `sync.events` 且窗口仍 `COMPLETE`。
- 危害：该行同时属于下一个窗口 → 相邻窗口双计（`income_events` 的 UNIQUE 只防库内重复，不防窗口聚合双计）。
- 建议：摄入时按半开区间过滤或对越窗行给 `ROWS_OUTSIDE_WINDOW` 结构化 reason code（不得静默收下）。

### P2-2 跨页重复行让 `WindowSync.events` 不再键唯一（金额重复求和）

- `sync_income_window:329-336`：`ingest_income` 已把重复行记为 `duplicates`，但
  `all_events.extend(item for item in page_events if key not in conflicted)` **只排除冲突键、不排除重复键**。
- 复现 `_scratch_B/p01_pagination.txt [A]`：跨页重复 B → `events len=4 / unique=3`，
  `sum(events) = -2.00`（正确 `-1.50`）；`insert.inserted` 只有 3 个键，与实际 4 条事件、与
  `persist()` 的返回行数三方不一致。
- 缓解：库内 `UNIQUE` + `INSERT OR IGNORE` 让**落库数据**不重复（`p05 [3]` `persist#2: 0`），
  但内存产物（被文档当作"本窗口事件集"交给上层求和/归因）会双计。
- 建议：`all_events` 只追加 `result.inserted` 对应的行（或按幂等键去重后再 extend）。

### P2-3 极小金额 + 多腿让 `allocate_weighted` 抛 `AllocationError`，成本链整链中断

- `k03_income.py:557-601`：`ROUND_HALF_UP` 量化后末腿承接余数，余数为负即抛 `AllocationError`；
  `allocate_funding_to_lifecycles`（`income_reconciliation.py:557-572`）**不捕获**。
- 复现 `_scratch_B/p07_attr_alloc.txt [2]`：`amount=0.00000003, 5 等权腿` → `AllocationError: negative share…`；
  `0.00000001`/3 腿与 2 腿正常，`0.01`/3 腿精确守恒（`[3]`）。
- 影响：资金费单腿低至 1e-8 在真实账户并不罕见；5 个以上生命周期同时持仓时应触发。
- 建议：把该异常在窗口/生命周期层捕获并降级为 `INCOMPLETE + 明确 reason code`（如
  `ALLOCATION_QUANTUM_CONFLICT`），或改成"量化误差记在确定性单腿并审计"。

### P2-4 `persist` 并非"写入与水位同事务"

- 类 docstring（`:866`）与 `IncomeStore` Protocol（`:849`）都称"分页结果与水位同事务提交"，
  但补丁 01 的 `insert_income_events` 内部 `self.conn.commit()`（diff `:90`），
  `upsert_income_coverage` 走 `_insert`（`src/audit/database.py:1346-1351`）**各自提交**。
- 复现 `_scratch_B/p09_tx_env.txt [1]`：让 `upsert_income_coverage` 抛错 → 事件已提交、覆盖未落库。
- 当前顺序（事件先、覆盖后）偏向 fail-closed，可接受；但 docstring 与实现不符，且 README §12 **L8**
  明确把"分页与水位同事务提交"及 `pending_since/next_sync/last_source_success` 时效字段判给 U01 ——本交付未闭合。
- 建议：二选一：真正单事务提交（`persist` 内一次 commit），或把 docstring/Protocol 改成事实描述并登记未闭合。

### P2-5 归因侧不可换算金额被静默丢弃，且 `unknown_usdt=None` 语义混淆

- `attribute_funding:529-534` 对 `_usdt_amount(...) is None` 的事件 `continue`，既不计入任何桶，也不进
  `attributed/unattributed/unknown_keys`；`IncomeAttribution`（`:466-480`）**没有** `unconverted` 字段
  （会话侧 `SessionReconciliation` 有）。
- 复现 `_scratch_B/p07_attr_alloc.txt [9]` / `p09 [6]`：KZ 无汇率 → `unknown_usdt=None`（读作"无未知"）、
  `attributed=unattributed=0`、`unknown_keys=()`；叠加真实 UNKNOWN 状态时该事件同样消失
  （`unknown=-0.5` 只来自那条 USDT 事件）。
- 危害：金额被剔除却无任何可查询痕迹——与"已知的未知必须显式"相冲突（会话侧做到了，归因侧没有）。
- 建议：`IncomeAttribution` 增 `unconverted` 与 reason code，并让 `unknown_usdt=None` 仅表示"无 UNKNOWN 状态事件"。

### P2-6 未知分量在 `components` 里被折算成 0

- `reconcile_session:696-703`：`realized if realized is not None else Decimal(0)`、
  `net_fees if net_fees is not None else Decimal(0)`。
- 复现 `_scratch_B/p10_session_edges.txt [3]`：`realized_pnl=None` 或 fill 缺 `commission` →
  分量写作 `0`，`identity_rhs()` 把未知当 0 加进等式；仅靠 reason code（复用 `NO_SOURCED_FX_RATE`）提示。
- 与同模块"非 USDT 无汇率 → `None` 而不是 0"的口径不一致。
- 建议：未知分量用 `None`（或 `UNKNOWN` 标记分量）并让 `identity_rhs()` 显式回带未决项。

### P2-7 非空窗口 + `attribution=None` 被报成"零费用不可证明"

- `window_cost_finalization:443-450`：`sync.events` 非空时仍把 `attribution` 传 `None` 给 `finalize_costs`，
  而 `finalize_costs:663` 把"无 attribution.decisions"当**空窗口**分支处理 → 要求 `zero_fee_proof`。
- 复现 `_scratch_B/p07_attr_alloc.txt [4]`：2 条事件的窗口 → `cost=INCOMPLETE`，`reason_codes=('ZERO_FEE_NOT_PROVABLE',)`。
- 状态结果正确（INCOMPLETE），但 reason code 误导（真正缺的是归因，不是零费用证明）；排障会走错方向。
- 建议：非空窗口缺归因时给 `ATTRIBUTION_MISSING`/`NO_ATTRIBUTION_DECISIONS`。

### P2-8 作者测试的证据强度：真实存储层被 SKIP，`PASS=78` 未覆盖补丁路径

- `_scratch_B/r01_test_income.txt`：`[9] 真实存储层 → SKIP database.py 收入存储适配尚未由 I 应用`，
  最终仍打印 `PASS=78 FAIL=0` 且 exit 0。
- 编排者与作者据此得出的"PASS=78"**不覆盖** `insert_income_events/upsert_income_coverage/fetch_*` 与
  `load_existing` 回灌路径——正好是 P0-2 与 P1-7 所在处。
- 建议：把"补丁未应用"视为**未验收**而非 SKIP（或要求作者在沙箱内自证并附输出）。

### P3 级（不影响结论，但应登记/修）

| # | 位置 | 说明 |
|---|---|---|
| P3-1 | `src/agents/calibration.py:16-22`（`BUCKETS`） | 桶中点用 `(lo+hi)/2`（如 `>=0.85` → 0.93、`0.40-0.55` → 0.475），口径偏乐观/偏保守不一；应显式登记为约定 |
| P3-2 | `review.py:177` vs `calibration.py:68-73` | **胜率定义跨模块不一致**：`_build_outcome_stats` 把 `pnl>=0` 算赢，`compute_calibration` 把 `pnl==0` 记为 `flat` 且不入 wins（同一批 `trade_outcomes`，两个胜率） |
| P3-3 | `review.py:137-141` | 触发事件 `symbol` 为空时 `if item["symbol"] and …` 跳过匹配 → **命中所有仓位**（fail-open，会把与某标的无关的证伪算到每个亏损单头上）。当前唯一生产端 `position_guardian.py:209-212` 总带 symbol，故暂不可达 |
| P3-4 | `review.py:100` | **RV02 触发来源窄**（只认 `event_type='guardian_invalidation'`）：`p08 [D]` 矩阵显示 `thesis_invalidation` 等来源 → `invalidated_losses=0`、落入 `invalidation_text_only`。因唯一生产端就是 `guardian_invalidation`+`THESIS_INVALIDATED`（`position_guardian.py:204-212`），**缺口严重性：低（当前 fail-closed 偏低而非误报）**，但必须按作者自述登记，否则将来新增证伪来源会静默不计 |
| P3-5 | `income_reconciliation.py:78`（`REASON_UNCONVERTED_FX`）、`:84`（`REASON_LATE_REVISION_DUPLICATE`） | `NO_SOURCED_FX_RATE` 一码多用（fill 缺 pnl / 非 USDT 无汇率 / 归因 UNKNOWN 状态），排障粒度不足；`REASON_LATE_REVISION_DUPLICATE` 为死常量 |
| P3-6 | `income_reconciliation.py:647-664` | `reconcile_session(income_events=[dict,…])` → `AttributeError: 'dict' object has no attribute 'income_type'`（非结构化拒绝）；同模块其他入口都是结构化拒收 |
| P3-7 | `_sum_commissions:601-624` | 对 fill 手续费取 `abs()` 强制记成本，而 `realized_pnl` 不取绝对值 → 返佣/负手续费场景符号被静默翻转；口径应写明或改为按符号处理 |
| P3-8 | `patches/…/02_…diff` | `fetch_income_page` 全仓库**无调用方**（死代码），且 `_request_purpose` 无 `/fapi/v1/income` 分支 → 该端点被判 `normal` 而非 `account_reconcile`（`binance_futures.py:44-71`）。补丁 02 的授权路径就含 `binance_futures.py`，建议同批补上用途分支 |

## 3. 作者 9 项自述主张的独立判定

| # | 主张 | 判定 | 证据 |
|---|---|---|---|
| 1 | 同毫秒满页零丢失、终止证据窗口绑定、`watermark==end`、`gaps==()` | **部分成立** | 窗口绑定/水位/短页证明正确（`p01 [C]`）；但"零丢失"不可证（P1-3），且跨页重复会使事件集双计（P2-2） |
| 2 | 幂等键四元组唯一 | **契约侧成立，存储侧不成立** | K03 键含 environment 不撞键（`p01 [K]`，`p06 [C]` 重放判 duplicates）；但补丁 01 未归一化 `income_type`/`tran_id` → DB 出现同键两行（P1-8） |
| 3 | `coverage=COMPLETE & attribution=UNATTRIBUTED & cost=INCOMPLETE` 同帧 | **成立** | 独立复现 `p09 [5]`：`COMPLETE / UNATTRIBUTED / INCOMPLETE ('UNATTRIBUTED_EXPOSURE')` |
| 3b | 守恒：归因 + 未归因 = 总额 | **成立（但"总额"仅指 FUNDING_FEE 的可换算部分）** | `p07 [1]`：`conservation_source_amount == -2.20 == Σ FUNDING_FEE`；类型混杂（COMMISSION/REALIZED_PNL/TRANSFER）不计入；不可换算事件被静默剔除（P2-5） |
| 3c | 负权重/末腿反例 | **发现真实崩溃路径** | 负权重正确拒绝；极小金额多腿抛 `AllocationError` 未捕获（P2-3） |
| 4 | `new_closed_outcome_created=False` 真成立 | **成立**，但"可修订"不成立 | `p07 [6]`：全 `False`、重复投递不重复闭合；同键改额被吞（P1-4） |
| 5 | COMMISSION 只交叉核对，不重复计费 | **主结论成立，核对实现有缺陷** | `p07 [8]`：`components` 无 `income_commission_usdt`，重复塞入即 `unexplained=+0.70`；但误报 MISMATCH / `balanced` 与 `status` 矛盾 / REALIZED_PNL 不核对（P1-6），守卫基本无效（P1-5） |
| 6 | Decimal 而非 float；KZ 缺汇率 `None` 而非 0 | **成立（会话侧）** | `p07 [9]`：`Decimal('0.01')`、`0.01-0.0047==0.0053` 精确；KZ 无汇率进 `unconverted` 且不折算。**例外**：归因侧不可换算被静默丢弃（P2-5）、未知分量在 `components` 里写 0（P2-6） |
| 7 | RV01–RV03 | **成立**（独立复跑 44 PASS）；RV02 缺口严重性低 | `r02/r04`；`p08 [D]` 矩阵；P3-2/P3-3/P3-4 |
| 8 | `query_review_library` 无写入副作用 | **成立（强验证）** | `p08 [B]`：拦截 `write_text/write_bytes/mkdir/touch/unlink/rename/os.replace/os.remove` 共 8 种原语 → 0 次写尝试；SQLite `total_changes` 增量 0；`LESSONS_FILE` 不存在→不存在；返回值 `read_only=True` 且不含 `library_file` |

## 4. 对 4 个补丁的逐条意见

- **01 `audit/database.py`（139 行）—— CHANGES_REQUESTED（P0 关联）**
  - 迁移 17 建表 + `UNIQUE(environment, account_ref, income_type, tran_id)` + `income_coverage` 的
    `UNIQUE(env,acct,type,start,end)` 语义**方向正确**；`INSERT OR IGNORE` + `ON CONFLICT DO UPDATE` 幂等、
    `fetch_income_events` 回 dict、`fetch_income_coverage` 不存在返回 `None`（不折算为零）均正确；
    沙箱内 `schema_version=17`、首写 2/重写 0（`p05 [1][3]`）。
  - 必须修：①`content_hash` 自造指纹（P0-2b）——应直接调用 `core.contracts.income_content_hash`；
    ②`income_type/tran_id` 未归一化（P1-8）；③`fetch_income_coverage` 无法按窗口查询（P1-2）；
    ④无隔离/冲突持久化（P1-7）；⑤`persist` 两段提交与"同事务"声明不符（P2-4）。
  - **可回滚性**：`CREATE TABLE IF NOT EXISTS` + `_apply_migration` 版本号推进；表为新增，无 ALTER/DROP，
    对既有表零影响 → 数据层可回滚（回退代码即可，遗留空表无害）。`_apply_migration(17,…)` 需确认
    `src/audit/migrations.py` 的版本状态机允许（沙箱实测 17 生效、`smoke_db_migrations` 13/13 PASS）。
  - `with self.lock` / `_insert` 复用既有原语，写指标计数正常。
- **02 `binance_futures.py`（35 行）—— 有条件接受**
  - `fetch_income_page` 正确做 `endTime=end-1ms` 映射、`limit` 1..1000 校验、不排序/不去重/不写库，符合半开区间契约。
  - 但**必须与 04 同批应用**：实测只应用 02 会让 `scripts/check_binance_request_policy.py`（在离线回归清单内）
    以 `未登记 Binance 端点: [('GET', '/fapi/v1/income')]` 失败（`_scratch_B/r06_policy_02only.txt`）。
  - 建议同批补 `_request_purpose` 的 `/fapi/v1/income` 分支（该文件在 U01 集成目标内，属授权范围，P3-8）。
- **03 `trade_reconciliation.py`（39 行）—— 接受**
  - `FUNDING_CASHFLOW_TYPES = {FUNDING_FEE, INSURANCE_CLEAR}` + `aggregate_funding_cashflow` 把
    `COMMISSION/REALIZED_PNL` 排除并**回带被排除类型**（不静默丢弃），与"fill 是唯一费用入口"一致；
    `Iterable/Mapping/Decimal/ZERO/_decimal/InvalidExecutionFill` 均已在文件内存在（无新 import 缺失）；
    纯函数、无调用方改动 → 不破坏既有消费端（`smoke_trade_reconciliation` 23/23 PASS）。
  - 意见：建议把 `realized_pnl` 的等价排除也显式写出（当前只按类型名排除，若上游把 REALIZED_PNL 折算进
    `realizedPnl` 字段仍有双计入口），并在 docstring 注明与 `reconcile_session` 的配合方式。
- **04 `data/binance_request_policy.py`（12 行 / 1 插入）—— 风险处置意见**
  - 事实：`_request`（`binance_futures.py:264-271`）以 `strict="binance" in hostname` 调用 `policy_for`，
    而默认 `base_url="https://demo-fapi.binance.com"`、生产 `fapi.binance.com` **都含 "binance"** →
    未登记即抛 `UnknownBinanceEndpoint`。实测未登记时 `policy_for('GET','/fapi/v1/income',{})` 抛错，
    登记后 `ip_weight=30 / endpoint=/fapi/v1/income`（`p05 [1]`）。**风险为真**。
  - **处置建议（不应用它并非可接受状态）**：该文件不在 U01 独占路径也不在 U01 集成目标（仅
    `src/data/`），因此**不应由作者自行应用**；应作为**必需的共享适配项登记给 I**，与 01/02/03 同批应用，
    并绑定 02（02↔04 耦合，见上）。理由：1 行、纯新增登记、对既有端点零行为变化，且它落实的是仓库既有不变量
    （L03 发布检查 `check_binance_request_policy.py`）。
  - 附带：`30` 与官方 Get Income History 的 IP 权重一致，量级合理；但"未应用即抛 `UnknownBinanceEndpoint`"
    目前只影响 `fetch_income_page`，而该方法**当前无任何调用方**（P3-8），故真实运行期风险要等 §10.1 接线；
    建议在 04 的登记条目里写明"依赖方 = U01 收入同步接线"，避免 I 漏合。
  - 备选（不推荐）：若 I 坚持不动该路径，则补丁 02 也不得合入（否则 CI 门禁必红），即两条一起推迟。

## 5. 未覆盖项 / 无法离线验证

1. **真实 Binance `page`/`limit` 翻页稳定性**：离线只能用替身 fetch。P1-3 的"页间丢行"是机制层面证明
   （无校验 + `overlap_recheck` 未用），其真实发生率需联网观测，本次**未验证**。
2. 补丁 01..04 已应用后的**真实交易/网络路径**：未联网、不启服务，未验证 `fetch_income_page` 的真实响应
   形状（`incomeType`/`tranId` 字段名与大小写）与真实修订行为（同 `tranId` 改额是否真的发生）。
3. **真实账户/密钥/`.env`/`data/`**：按纪律未读取，未验证 `account_ref` 派生与多账户场景下的键唯一性。
4. `IncomeReconciliationBook` 与 §10.1 实际调用方的接线（谁提供 `existing`、谁聚合多窗口覆盖）**未验证**——
   P1-2/P1-8 的实际触发面取决于接线方式，建议 I 在集成时补一条"按区间聚合覆盖"的验收。
5. 未验证 `src/audit/migrations.py` 的版本状态机在**已升级到 17 后又回退代码**时的行为（仅确认前向 17 可用）。
6. 报告的"U01 越界"结论沿用编排者预先核验（我复核了 `git diff --stat` 与任务卡独占路径，一致）；
   未逐字节比对契约文件哈希。

## 6. 建议的放行条件（供 Q/I 参考）

必须（P0）：
1. 登记两个 `smoke_u01_*`（新补丁）→ `scripts/run_offline_regression.py` 必须 exit 0；
2. `sync_income_window` 正确处理 `key->content_hash` 映射；补丁 01 改用 `income_content_hash`（单一真值源），
   并补一条"真实库落库 → `load_existing()` → 重放 → `duplicates`（非 quarantine）且事件集不减"的测试，不得 SKIP。

应修（P1，至少给出登记或修复二选一）：P1-1..P1-8，其中 P1-1（跨 profile 覆盖）、P1-2（按窗口查覆盖）、
P1-4（迟到修订）、P1-6（交叉核对误报/不核对）、P1-8（income_type 治理）建议本批修；
P1-3/P1-5/P1-7 若本批不修，必须写进 README §12 的 U01 归属行并给出下游任务号。

可延后（P2/P3）：按上表登记即可，但 P2-2（事件集非键唯一）与 P2-3（极小金额分摊崩溃）建议本批顺手修，
因为二者都会在正常运行期（非边界）出现。

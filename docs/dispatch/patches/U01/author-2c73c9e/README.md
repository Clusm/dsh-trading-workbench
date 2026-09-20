# U01 共享适配补丁包（作者 A / 交 I 应用）

补丁基线：`ffdab44`；作者分支 `task/U01`；本包由 I 在集成时应用，**不适用的补丁不得单独合入**。

| 补丁 | 目标文件 | 独占/共享 | 用途 |
|---|---|---|---|
| `01_audit_database_income_store.diff` | `src/audit/database.py` | U01 集成目标（I 应用） | 收入流水/覆盖证据/冲突隔离三表（迁移 17）+ 读写入适配 |
| `02_exchange_binance_futures_income_endpoint.diff` | `src/exchange/binance_futures.py` | U01 集成目标（I 应用） | 官方 `GET /fapi/v1/income` 单页只读读取 + 请求用途分类 |
| `03_exchange_trade_reconciliation_funding_cashflow.diff` | `src/exchange/trade_reconciliation.py` | U01 集成目标（I 应用） | 资金费现金流聚合（排除 fill 已计入的 COMMISSION/REALIZED_PNL） |
| `04_data_binance_request_policy_income_weight.diff` | `src/data/binance_request_policy.py` | 共享适配（I 应用） | 登记 `/fapi/v1/income` 的 IP 权重 30 |
| `05_migration17_assertions.diff` | `scripts/smoke_{account_mirror,db_migrations,exchange_event_inbox,exchange_safety_monitor,fault_matrix,order_mirror,order_outbox,protection_mirror,release_acceptance}.py` | 共享适配（I 应用） | **迁移 17 的伴随修改**：9 个既有共享冒烟把「迁移集合/最新 schema」写死为 16，必须随 01 同批改 |

## 批次约束（必须遵守）

1. **02 必须与 04 同批应用**。只上 02 会让发布检查
   `scripts/check_binance_request_policy.py`（在 `scripts/run_offline_regression.py` 清单内）
   以 `未登记 Binance 端点: [('GET', '/fapi/v1/income')]` 失败（本批复现：
   `_scratch_A/sb` 沙箱内 02-only → exit 1；补上 04 → `PASS request policy coverage: endpoints=35`）。
   若 I 坚持不动 `src/data/binance_request_policy.py`，则 02 也不得合入（两条一起推迟）。
2. **01 必须与 05 同批应用**（顺序：**先 01、后 05**；05 只改 `scripts/`、不改 `src/`，
   两者无文本冲突，但 05 的断言只有在 01 已应用时才成立）。
   r2 独立复验已实测：**只应用 01 会让统一离线回归新增 9 处失败**（见下节），
   即 I 一应用 01、发布门禁立刻变红。05 就是这 9 处的必需伴随修改。
   **不得只合 01 不合 05**；若 05 被拒，01 也必须一起推迟。
3. 01/02/03 三者互不依赖，可与 04 任意顺序应用；五个补丁各自 `git apply --check -p1` 通过，
   任意组合（含 `01+02+03+04+05`）在沙箱中顺序应用全部成功。
4. **「对既有表零影响」的口径修正（撤回原表述）**：01 的迁移 17 在 **DDL 层面**确为纯新增表
   （`CREATE TABLE IF NOT EXISTS`，无 ALTER/DROP，不改既有表列）；但**版本号从 16 推进到 17
   这一事实本身**会改变 `schema_migrations` 的可观测内容，而仓库内 9 个既有共享冒烟把
   「迁移集合恰为 1..16」/「最新 schema 版本 = 16」写死在断言与 fixture 里。
   因此原 r2 README 的「**对既有表零影响**」作为**结论**不成立（它只对 DDL 成立，对
   「版本号推进」不成立），本版撤回，改为如实描述：**迁移 17 = DDL 纯新增 + 必须伴随 05 的
   断言/fixture 更新**。回退方式不变：回退代码即回滚（遗留空表无害）。
5. 04 的 `ip_weight=30` 与官方 Get Income History 一致；其依赖方 = U01 收入同步接线
   （`fetch_income_page` 在 `src/exchange/income_reconciliation.py` 之外暂无调用方，
   接线属下游任务）。

## 补丁 05：迁移 17 的 9 处伴随修改（逐条）

`scripts/` 不在 U01 独占路径内（`run_offline_regression.py` 属 U00），故作者**不直接改仓库**，
只交付本 diff，由 I 与 01 同批应用。9 个文件全部在
`scripts/run_offline_regression.py` 的 `SCRIPTS` 清单内，且**均只在补丁 01 单独应用时才失败**
（`combo_02`/`03`/`04` 单独应用不触发 → 归因收敛到 01）。

| # | 文件 | 原内容（错） | 改为 | 为什么是必需的 |
|---|---|---|---|---|
| 1 | `smoke_db_migrations.py:37` | `check("记录迁移版本1到16", … == list(range(1, 17)))` | `…1到17… == list(range(1, 18))` | 旧库首次升级后 `schema_migrations` 实际含版本 1..17；断言把「集合恰为 1..16」写死 |
| 2 | `smoke_db_migrations.py:78` | `check("版本记录保持唯一", … == list(range(1, 17)))` | `… == list(range(1, 18))` | 同上（重启后迁移不重复用例） |
| 3 | `smoke_account_mirror.py:103` | `check("数据库迁移版本1到16完整", … == list(range(1, 17)))` | `…1到17完整… == list(range(1, 18))` | 直连真实 `AuditDatabase`，版本集合含 17 |
| 4 | `smoke_exchange_event_inbox.py:61` | `check("数据库已记录迁移1到16", … == list(range(1, 17)))` | `…迁移1到17… == list(range(1, 18))` | 同上 |
| 5 | `smoke_exchange_safety_monitor.py:95` | `check("数据库迁移版本1到16完整", … == list(range(1, 17)))` | 同上 | 同上 |
| 6 | `smoke_order_mirror.py:188` | 同上 | 同上 | 同上 |
| 7 | `smoke_order_outbox.py:212` | 同上 | 同上 | 同上 |
| 8 | `smoke_protection_mirror.py:119` | 同上 | 同上 | 同上 |
| 9 | `smoke_fault_matrix.py:559,567,571,573,589,592,594-600` | 场景 10 手工 `INSERT INTO schema_migrations … VALUES(17,…)` 模拟「更高 schema」；注释/fixture 里旧代码期望 `16` | 手工插入版本改 **18**；`current_schema_version() == 18`；manifest/正控断言改 18；「旧代码期望」改 **17**；局部变量 `manifest16` → `manifest_legacy` | 01 已应用后版本 17 已存在，再手工插入 17 直接 `sqlite3.IntegrityError: UNIQUE constraint failed: schema_migrations.version`（**不是**断言失败，是崩溃）。场景语义是「新代码更高版本 vs 旧代码期望更低版本」，故整体上移 1 |
| 10 | `smoke_release_acceptance.py:112` | fixture manifest `"schema_version": 16` | `17` | 该 fixture 用真实 `AuditDatabase` 建库（迁移到 17），而 `src/core/release_evidence.py:448-451` 的 `schema_version` 门禁比较「DB 版本 == manifest 版本」；16≠17 → 门禁 FAIL → `严格发布报告可GO` 失败 |

> 说明：`smoke_fault_matrix.py` 的第 9 行同时把局部变量 `manifest16` 重命名为
> `manifest_legacy`——该变量在改动后装的是 `schema_version=17`，沿用旧名会让「名字说谎」，
> 属必需的命名修正（同一逻辑块内 5 处引用）。
> 计数：`smoke_db_migrations` 2 处 + 其余 6 个版本断言各 1 处 = 8 处版本断言，
> 加 `smoke_fault_matrix` 手工插入、加 `smoke_release_acceptance` fixture = 10 个改动点 / 9 个文件。

### 后续维护提示（登记，不阻断）

版本断言的正确形态是「**至少包含 1..N**」而非「恰为 1..N」。本批沿用「恰为 `range(1, 18)`」
以保持与既有断言同构、最小改动；下一次迁移（18）会再次触发同一类失败。
建议 I/U00 在后续小批把 8 处断言改为 `set(range(1, N)) <= set(versions)` 的包含式，
`smoke_release_acceptance` 的 fixture manifest 改为从 `db.current_schema_version()` 派生。

## 补丁 01 的关键口径（审查 B_r1 §4 的 5 处修改）

1. **`content_hash` 与 K03 对齐**：直接调用 `core.contracts.income_content_hash`，
   删除适配层自造指纹。这是 P0-B 第二半——自造指纹会让「真实库回读 → 重放」被判成
   同键不同内容而隔离，而窗口政策会把冲突键整体移出事件集，于是**重放抹掉上一轮已入账事件**。
2. **键归一化**：新增 `environment_norm/account_ref_norm/income_type_norm/tran_id_norm`
   列，唯一约束建在归一化列上，与 K03 幂等键（`normalize_key_part`：strip+casefold）**等价**（P1-8）。
3. **按窗口读覆盖**：`fetch_income_coverage(..., start_ms=None, end_ms=None)` +
   `fetch_income_coverage_range(...)`。旧签名只返回 `end_ms` 最大的一条，
   会把任意一个（最新）窗口当全期证据 ⇒ 缺口被读成「已证明为零」（P1-2）。
4. **隔离可审计**：新增 `income_conflicts` 表 + `insert/fetch_income_conflicts`，
   同键不同内容不再只存在于内存（P1-7）。
5. **事务语义改为事实描述**：`IncomeStore` Protocol 与 docstring 不再声称「写入与水位同事务」；
   事件先、覆盖后，各自提交（fail-closed）（P2-4）。

## 未修项登记（本批不修，理由与去向）

| 项 | 位置 | 处置 |
|---|---|---|
| P3-1 桶中点 `(lo+hi)/2`（0.85→0.93） | `src/agents/calibration.py:16-22` | **登记**：属口径约定，改动会移动 RV03 的 miscalibration 数值与既有 44 PASS 语义；需 I/Q 决定是否重定基准后单独小批 |
| P3-2 胜率定义跨模块不一致（`pnl>=0` vs `pnl==0` 记 flat） | `src/agents/review.py:177` / `calibration.py:68-73` | **登记**：同一批 `trade_outcomes` 产出两个胜率；统一会改变 RV01/RV03 既有断言，需契约侧确认口径 |
| P3-4 RV02 触发来源窄（只认 `guardian_invalidation`） | `src/agents/review.py:100` | **登记**：当前唯一生产端即此来源，缺口为 fail-closed 偏低（不误报）；新增证伪来源时须同步扩表 |
| P3-5 `NO_SOURCED_FX_RATE` 一码多用 | `src/exchange/income_reconciliation.py:78` | **部分修**：新增 `FILL_INCOME_REALIZED_PNL_*`、`NO_ATTRIBUTION_DECISIONS` 等细分码；fill 缺 pnl / 非 USDT 无汇率仍共用一码，登记待 K03 小版本演进 |
| P3-7 fill 手续费取 `abs()` 强制记成本 | `src/exchange/income_reconciliation.py:_sum_commissions` | **登记**：docstring 已写明口径（币安 userTrades commission 恒正、返佣走独立字段）；改按符号处理会翻转返佣场景，需契约确认 |
| P3-8 `fetch_income_page` 暂无调用方 | `src/exchange/binance_futures.py` | **部分修**：`_request_purpose` 已补 `/fapi/v1/income → account_reconcile` 分支；调用方接线属 §10.1 后续任务 |
| **P3-N1** 守卫在 `income_events` 为空时对任意分量键放行 | `src/exchange/income_reconciliation.py:assert_no_double_count` | **登记（确认非本批回归）**：守卫以 `for event in income_events:` 为外层循环，事件为空则 marker 判定体从不执行 → 任意分量键 ALLOWED。已核对基线 `c290c50` 的旧实现在 `:757` 是同一外层结构（`for event in income_events:`），故**非本批引入**。不修的理由：`_FILL_OWNED_KEY_MARKERS` 是「收入类型 → 键名 marker」映射，无事件时无法确定适用哪些 marker；改为无条件拒绝是一处**策略变更**（会影响仓库外未知调用方），需 I/契约侧确认后再单独小批。 |
| **P3-N2** `_income_row` 对 falsy 金额强制转 `""` → 校验崩溃 | 补丁 01 新增的 `src/audit/database.py:_income_row` | **登记**：`str(row.get("amount") or "")` 会把 `Decimal("0")`/`Decimal("0.00")` 当 falsy 转成 `""`，`IncomeEvent` 校验抛 `ValidationError`（`symbol` 同法）。**fail-loud**（不是静默错账），且仅在调用方绕过 `persist` 直接传 `Decimal` 时可达（正常路径入参为 JSON 字符串）。不修的理由：该函数体只存在于**已被 r2 独立复验通过的补丁 01**内，改它必须重生成 01 并重跑 r2 的全部验证链（apply 矩阵 / `content_hash` 与 K03 同一真值源 / `[9]` 真实落库），风险大于收益。**建议在 01 的下一小版本**把 `or ""` 换成显式 `None`/缺省判定（`row.get("amount")` 为 `None` 才取默认），并加 Decimal 入参用例。 |
| **P3-N3** `attribution=None` 时 `INCOME_ID_CONFLICT` 被提前 return 掩盖 | `src/exchange/income_reconciliation.py:window_cost_finalization` | **已修**：`skip-events + attribution is None` 分支现在在 `sync.quarantine` 非空时一并回带 `REASON_ID_CONFLICT`（状态仍 INCOMPLETE）。新增回归断言「缺归因 + 有隔离 -> 两个 reason 同时回带」 |
| **P3-N4** 测试模式串硬编码「已应用」 | `tests/integration/test_income_reconciliation.py:_patched_database_dir` | **已修**：模式串改为由实跑证据派生（`rc={applied.returncode}` + 由 returncode 决定的状态词），并补 `hasattr(AuditDatabase, "insert_income_events")` 断言，使「补丁已生效」有实证而非声明 |
| P1-4 的契约侧出口（`LATE_REVISION_CONTENT_CHANGED` 升格为新修订） | `src/core/contracts/k03_income.py` | **登记给 I**：契约侧 `revision_id` 不含金额，本层已改为结构化拒绝（`LateIncomeContentChanged`）兜底；把「同键内容变更」升格为独立修订需 K03 新增 reason code（小版本演进），作者不越权改契约 |
| P1-8 的未知类型降级策略（拒绝 vs 隔离）写入 K03 | `src/core/contracts/k03_income.py` | **登记给 I**：本层已对窗口声明类型做枚举校验、对返回行做未知/异类型显式回带；枚举本身的治理条款需落到 K03 小版本 |
| 冲突键整体退出窗口聚合（`reasons=('INCOME_ID_CONFLICT',)` 时该键金额不进 `events`） | `src/exchange/income_reconciliation.py` | **登记**：库层不丢（`income_conflicts` 可审计），但聚合层少算该键金额；带真实 attribution 时 `window_cost_finalization` 会补 `INCOME_ID_CONFLICT` + INCOMPLETE，缺 attribution 时本批已由 P3-N3 补齐 reason |
| 两个 `scripts/smoke_u01_*.py` 未登记进 `run_offline_regression.SCRIPTS` | `scripts/run_offline_regression.py`（U00 独占） | **登记给 I**（作者不改）：`_validate_smoke_manifest()` 会以 `未登记冒烟脚本: …` 直接抛错，统一入口在任何补丁应用前就 exit≠0；属 I 的原子登记项 |
| README §12 下游缺口表 | `docs/contracts/README.md` | **登记给 I**：`docs/contracts/` 属 U00 独占，作者不改；请 I 把上表 P3-1/2/4/5/7/8 与 P1-4/P1-8 契约项补入 U01 归属行 |

## 验证（本批实跑，原始输出见交付报告）

```
git apply --check -p1 <01|02|03|04|05>          # 五个补丁各自 OK（独立 git repo 沙箱，非 repo 子目录）
沙箱顺序应用 01 / 02 / 03 / 04 / 05 / 01+05 / 01+02+03+04+05   # 全部 OK
python tests/integration/test_income_reconciliation.py   # PASS=140 FAIL=0, exit 0
python tests/integration/test_review_statistics.py       # PASS=44  FAIL=0, exit 0
python scripts/smoke_u01_income.py                       # PASS=140 FAIL=0, exit 0
python scripts/smoke_u01_review_statistics.py            # PASS=44  FAIL=0, exit 0
python scripts/smoke_contracts_v3.py                     # PASS 173 / FAIL 0, exit 0

# 迁移 17 的 9 处伴随修改对照（沙箱 = git archive HEAD + 独立 git init）
pristine            : 9/9 exit 0
仅 01               : 9/9 exit 1  ← 未登记时 I 一应用 01 门禁即红
01 + 05             : 9/9 exit 0
01+02+03+04 + 05    : 9/9 exit 0
# 统一离线回归全清单（非 fail-fast 逐条）
pristine vs 01+02+03+04+05 : 失败集合无新增（仅既有 KI02 环境性失败）
```

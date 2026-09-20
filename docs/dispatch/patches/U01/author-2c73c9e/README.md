# U01 共享适配补丁包（作者 A / 交 I 应用）

补丁基线：`ffdab44`；作者分支 `task/U01`；本包由 I 在集成时应用，**不适用的补丁不得单独合入**。

| 补丁 | 目标文件 | 独占/共享 | 用途 |
|---|---|---|---|
| `01_audit_database_income_store.diff` | `src/audit/database.py` | U01 集成目标（I 应用） | 收入流水/覆盖证据/冲突隔离三表（迁移 17）+ 读写入适配 |
| `02_exchange_binance_futures_income_endpoint.diff` | `src/exchange/binance_futures.py` | U01 集成目标（I 应用） | 官方 `GET /fapi/v1/income` 单页只读读取 + 请求用途分类 |
| `03_exchange_trade_reconciliation_funding_cashflow.diff` | `src/exchange/trade_reconciliation.py` | U01 集成目标（I 应用） | 资金费现金流聚合（排除 fill 已计入的 COMMISSION/REALIZED_PNL） |
| `04_data_binance_request_policy_income_weight.diff` | `src/data/binance_request_policy.py` | 共享适配（I 应用） | 登记 `/fapi/v1/income` 的 IP 权重 30 |

## 批次约束（必须遵守）

1. **02 必须与 04 同批应用**。只上 02 会让发布检查
   `scripts/check_binance_request_policy.py`（在 `scripts/run_offline_regression.py` 清单内）
   以 `未登记 Binance 端点: [('GET', '/fapi/v1/income')]` 失败（本批复现：
   `_scratch_A/sb` 沙箱内 02-only → exit 1；补上 04 → `PASS request policy coverage: endpoints=35`）。
   若 I 坚持不动 `src/data/binance_request_policy.py`，则 02 也不得合入（两条一起推迟）。
2. 01/02/03 三者互不依赖，可与 04 任意顺序应用；四个补丁各自 `git apply --check -p1` 通过，
   任意组合（含 01+02+03+04）在沙箱中顺序应用全部成功。
3. 01 的迁移版本 17 为**纯新增表**（`CREATE TABLE IF NOT EXISTS`，无 ALTER/DROP），
   对既有表零影响，回退代码即回滚（遗留空表无害）。
4. 04 的 `ip_weight=30` 与官方 Get Income History 一致；其依赖方 = U01 收入同步接线
   （`fetch_income_page` 在 `src/exchange/income_reconciliation.py` 之外暂无调用方，
   接线属下游任务）。

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
| P1-4 的契约侧出口（`LATE_REVISION_CONTENT_CHANGED` 升格为新修订） | `src/core/contracts/k03_income.py` | **登记给 I**：契约侧 `revision_id` 不含金额，本层已改为结构化拒绝（`LateIncomeContentChanged`）兜底；把「同键内容变更」升格为独立修订需 K03 新增 reason code（小版本演进），作者不越权改契约 |
| P1-8 的未知类型降级策略（拒绝 vs 隔离）写入 K03 | `src/core/contracts/k03_income.py` | **登记给 I**：本层已对窗口声明类型做枚举校验、对返回行做未知/异类型显式回带；枚举本身的治理条款需落到 K03 小版本 |
| README §12 下游缺口表 | `docs/contracts/README.md` | **登记给 I**：`docs/contracts/` 属 U00 独占，作者不改；请 I 把上表 P3-1/2/4/5/7/8 与 P1-4/P1-8 契约项补入 U01 归属行 |

## 验证（本批实跑，原始输出见交付报告）

```
git apply --check -p1 <01|02|03|04>             # 四个补丁各自 OK
沙箱顺序应用 01 / 02 / 03 / 04 / 01+02 / 01+03 / 02+04 / 01+02+03+04   # 全部 OK
python tests/integration/test_income_reconciliation.py   # PASS=139 FAIL=0, exit 0
python tests/integration/test_review_statistics.py       # PASS=44  FAIL=0, exit 0
python scripts/smoke_u01_income.py                       # PASS=139 FAIL=0, exit 0
python scripts/smoke_u01_review_statistics.py            # PASS=44  FAIL=0, exit 0
python scripts/smoke_contracts_v3.py                     # PASS 173 / FAIL 0, exit 0
```

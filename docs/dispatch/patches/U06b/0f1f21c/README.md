# U06b 共享适配补丁包（作者 head `0f1f21c`）

**后置线交付。** 本包**不**把 v2 标签接入执行路径、**不**改变首批记录
`F11:v2_labels=DEFERRED`、**不**让 v2 结果计入发布门禁（200 方向候选 / 30 完整闭合）。
v2 是 research-only 的第二套口径，与 legacy v1（轮转刷新取价）分表存放。

## 补丁清单

| # | 文件 | 作用 | 行数 |
|---|---|---|---|
| 01 | `src/audit/database.py` | 新增只读研究表 `shadow_v2_labels` + `log_shadow_v2_labels` / `fetch_shadow_v2_labels` / `shadow_v2_coverage` | +168 / -0 |
| 02 | `src/intel/service.py` | 新增只读方法 `ReadOnlyIntelService.shadow_v2_status()`；导入 `intel.shadow_v2` | +20 / -1 |

两个补丁**纯增量**：不改任何既有 SQL、不改任何既有方法体、不新增
`schema_migrations` 版本号（见下），也不向 `snapshot()` 增删任何键。

## 关键设计决定

1. **不占用迁移版本号。** `shadow_v2_labels` 随 `SCHEMA` 每次打开幂等
   `CREATE TABLE IF NOT EXISTS` 落地，**不**写入 `schema_migrations`。
   原因：`scripts/smoke_db_migrations.py` 与既有套件用
   `list(range(1, N))` 断言迁移版本集合，新增版本号会直接把已集成门禁跑红
   （U01 教训）。先例：同文件里的 U14 `alert_deliveries`。
   本任务已实测 `current_schema_version()` 仍为 **17**，迁移集合在 v2 写入前后不变。

2. **门禁角色只引用、不复制。** 本任务**未触碰** `src/intel/shadow_status.py`：
   `COHORTS` / `SOURCE_A|B|C` / `GATE_ROLE_*` / `release_gate_role` 语义逐字不变。
   `shadow_v2` 通过 `intel.shadow_status` 现算门禁角色与来源集合。

3. **落库强制归零。** `log_shadow_v2_labels` 把 `counts_toward_release_gate`
   写死为 `0`：即便调用方传 `True` 也不生效（已在测试中反证）。

4. **标签不可回填。** 主键 `(decision_id, horizon_minutes, experiment_key)` +
   `INSERT OR IGNORE`：同一实验版本内标签一经生成不可被后续轮次改写；改口径必须
   换 `experiment_key`，从而杜绝「后来才知道的价回填历史标签」。

5. **只读状态无副作用。** `shadow_v2_status()` 不写库、不采集、不触碰 Broker；
   缺库表时返回 `UNAVAILABLE` 而不是用 0 冒充「无缺失」。

## 应用方式

```bash
git apply 01_audit_database_shadow_v2_labels.diff
git apply 02_intel_service_shadow_v2_readonly_status.diff
```

`git apply --check` 在 3 种 git 配置下均 `exit=0`、`Skipped=0`：
默认 / `-c core.autocrlf=false -c core.eol=lf` / `-c core.autocrlf=true`。

## 集成说明（给 I）

* 必须先合入 `src/intel/shadow_v2/`（作者分支已提交，见 `git diff 0706db1..0f1f21c -- src/intel/shadow_v2 tests`），
  否则补丁 02 的 `from intel import shadow_status, shadow_v2` 会 ImportError。
* 两态探测标记：`audit.database.U06B_INTEGRATION == "u06b.shadow_v2.v1"`，
  `hasattr(ReadOnlyIntelService, "shadow_v2_status")`。
* `tests/integration/test_shadow_labels.py` **两态均可跑**（未打补丁 89 PASS，
  打完 100 PASS），不假定补丁是否已应用。

## 验证结果（隔离沙箱，`git archive 0706db1 | tar -x` + `git init`）

| 项 | 结果 |
|---|---|
| `git apply --check`（3 种 git 配置） | 全部 `exit=0`、`Skipped=0`、`Checking patch` 覆盖全部文件 |
| 本套件 `tests/integration/test_shadow_labels.py` | 未打补丁 **89 PASS / 0 FAIL**；打完 **100 PASS / 0 FAIL** |
| `scripts/smoke_contracts_v3.py` | PASS 173 / FAIL 0（exit 0） |
| `scripts/smoke_u06a_shadow_isolation.py` | PASS 52 / FAIL 0（exit 0） |
| `scripts/smoke_u09_release_evidence.py` | 60 通过 / 0 失败（exit 0），pristine 与 patched 同值 |
| `tests/acceptance/test_release_evidence_v2.py` | 78 通过 / 0 失败（exit 0），pristine 与 patched 同值 |
| `scripts/run_offline_regression.py`（全量） | pristine `OK scripts=85`；patched `OK scripts=85`；**失败脚本集合 = ∅（两者皆空）→ NEW_FAIL = ∅** |
| `tests/**/*.py` 全量扫（acceptance + integration） | pristine 16/16 exit 0；patched 17/17 exit 0（差值即本任务新增套件），无新失败 |
| `scripts/check_quality.py` | `QUALITY CHECK OK files=301`（含 ruff，未新增 exclude） |
| 门禁不变性 | 见 `evidence/release_gate_invariance.txt`：灌入 50 条 v2 标签前后，`directional_candidates` 3→3，报告（除墙钟 `generated_at`）逐字相同 |

## 未做（如实登记）

* 未把 v2 标签接入采集循环（`_refresh` 不调用 v2 标签）：默认关闭，后置线不改变
  已部署 RC 的运行行为。
* 未提供 v2 标签的采集调度/取价来源实现（`mark_price_samples` 已是唯一价格源，
  复用即可，调度属运行接线）。
* 未做配对实验与独立样本统计（U07 范围）。
* 未登记 `scripts/smoke_u06b_*.py`：本任务验收全部放在
  `tests/integration/test_shadow_labels.py`，故**不触发**
  `run_offline_regression._validate_smoke_manifest()`，无需在
  `scripts/run_offline_regression.py` 里登记。

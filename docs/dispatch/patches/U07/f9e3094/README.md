# U07 共享适配补丁包（作者 head `f9e3094`）

> 目录名 = **生成补丁时的作者 head**。该 head 之后作者分支只有
> `statistics.py` 的 ruff F401 修复（`aec8e2a`）与文档提交，**两个 `.diff` 内容未再变动**。
> 作者分支工作区已把共享文件还原（本包是共享改动的唯一载体，**未提交进分支**）。

**后置线交付。** 本包**不**把统计产物接入执行路径、**不**改变首批记录
`F11:v2_labels=DEFERRED`、**不**让统计产物计入发布门禁（200 方向候选 / 30 完整闭合）。
统计是 research-only 的第三套口径，与 legacy v1（轮转刷新取价）和 U06b 的
v2 标签分表/分口径存放。

## 补丁清单

| # | 文件 | 作用 | 行数 |
|---|---|---|---|
| 01 | `src/audit/database.py` | 新增只读研究表 `shadow_v2_statistics` + `log_shadow_v2_statistics` / `fetch_shadow_v2_statistics` / `shadow_v2_statistics_summary`；新增接线标记 `U07_INTEGRATION` | +145 / -0 |
| 02 | `tests/integration/test_shadow_labels.py` | U06b 套件的「独占路径四个模块齐备」由**集合相等**改为**包含**（其他任务在同包新增模块不视为 U06b 缺失） | +8 / -4 |

两个补丁**纯增量**：不改任何既有 SQL、不改任何既有方法体、不新增
`schema_migrations` 版本号（见下），也不向 `snapshot()` 增删任何键。

## 逐条证据

### 1. 为什么必须出补丁 02（否则 U07 会引入一个 NEW_FAIL）

`tests/integration/test_shadow_labels.py::test_module_boundaries` 原本断言
`src/intel/shadow_v2/` 下的文件集合**等于** U06b 的四个模块：

```
FAIL  独占路径四个模块齐备  ['__init__.py', 'labels.py', 'models.py', 'sampler.py', 'statistics.py']
PASS=99 FAIL=1
```

U07 的独占路径就在同一个包里（`statistics.py`），因此**任何**在该包内分批交付的
后置线任务都会把 U06b 套件跑红。修法是把「集合相等」换成「包含」：U06b 四模块仍被
逐字断言存在（缺失即 FAIL），其他任务的模块不参与 U06b 的完整性判定。
补丁 02 只改这一处判定，**不**放宽 U06b 的任何其它断言（禁止项扫描、
「不得硬编码真实日期」扫描仍覆盖同包**全部** `.py`，包括新增的 `statistics.py`）。

### 2. 数据库适配做了什么 / 没做什么

* 只新增一张表与三个方法；`counts_toward_release_gate` / `execution_weight` /
  `auto_promotion` 三列由 `log_shadow_v2_statistics` **强制**写死为 `0 / 0.0 / 0`：
  即便调用方传 `True` / `9.9` / `True` 也不生效（套件内有反证断言）。
* 主键 `(experiment_key, report_kind, config_fingerprint, digest)` + `INSERT OR IGNORE`：
  同一份报告重放 → **0 新增**（套件内断言 `inserted == 1` 后重放为 `0`）。
* 不占用迁移版本号：随 `SCHEMA` 每次打开幂等 `CREATE TABLE IF NOT EXISTS` 落地。
  与 U14 `alert_deliveries`、U06b `shadow_v2_labels` 同款策略。
  套件内断言「迁移版本集合在统计写入前后不变」（**相对比较**，不硬编码版本集合）。
* `shadow_v2_statistics_summary` 只给计数与隔离声明，刻意不产出
  `promotion_ready`/`go_verdict`/`verdict`，避免建立第二套门禁判断器。
* **未做**：没有向 `docs/dashboard/templates/decisions.html` 增加任何渲染
  （本包只有 2 个 `.diff`，dashboard 侧零改动）；没有把统计接进 `src/intel/service.py`；
  没有把统计接进采集循环。

### 3. 门禁结果未被影响（前后同值）

| 命令 | pristine（`c2f1bab`，未打补丁） | patched（作者 head + 本包 2 个补丁） |
|---|---|---|
| `scripts/smoke_u09_release_evidence.py` | `U09 冒烟: 60 通过 / 0 失败` exit=0 | `U09 冒烟: 60 通过 / 0 失败` exit=0 |
| `tests/acceptance/test_release_evidence_v2.py` | `U09 验收: 78 通过 / 0 失败` exit=0 | `U09 验收: 78 通过 / 0 失败` exit=0 |
| `tests/integration/test_shadow_labels.py` | `PASS=100 FAIL=0` exit=0 | `PASS=100 FAIL=0` exit=0 |
| `scripts/smoke_contracts_v3.py` | `PASS 173 / FAIL 0` exit=0 | `PASS 173 / FAIL 0` exit=0 |
| `scripts/smoke_u06a_shadow_isolation.py` | `PASS 52 / FAIL 0` exit=0 | `PASS 52 / FAIL 0` exit=0 |

套件内另有一条**逐字节**不变性断言（`tests/integration/test_shadow_statistics.py`
第 13 节）：在同一个库上先算一次 `release_evidence.build_release_readiness_report`，
灌入统计快照（30 条配对样本生成 1 份报告行）后再算一次，去掉墙钟
`generated_at` 后 **报告逐字段完全相同**；`directional_candidates` 前后同为 `3`，
`release_readiness` 结论字段相同，`candidate_source_filter.sources == [SOURCE_A]`
（口径仍逐字来自 `intel.shadow_status`）。证据见 `evidence/release_gate_invariance.txt`。

**没有触碰** `src/intel/shadow_status.py`（`COHORTS` / `SOURCE_A|B|C` /
`GATE_ROLE_*` / `release_gate_role` 一个字符未动），也没有触碰
`src/core/release_evidence.py`。

### 4. 隔离沙箱（`git archive <base> | tar -x` + 立刻 `git init`）

沙箱：`sandbox_pristine`（`c2f1bab`）/ `sandbox_patched`（作者 head + 本包 2 补丁）/
`sandbox_branch`（作者 head，不打补丁，用于两态自证）。

| 项 | pristine | patched |
|---|---|---|
| `scripts/run_offline_regression.py`（全量 85 脚本，各约 13 分钟） | `SMOKE MANIFEST OK included=81 excluded=1` + **`OFFLINE REGRESSION OK scripts=85`**，exit=0 | 同左，**`OFFLINE REGRESSION OK scripts=85`**，exit=0 |
| **失败脚本集合** | **∅** | **∅** |
| **NEW_FAIL = patched ∖ pristine** | — | **∅** |
| 本任务新增失败 | — | **0**（本包未新增任何 `scripts/smoke_*.py`，故 `_validate_smoke_manifest()` 不触发登记要求） |
| `tests/integration/test_shadow_statistics.py` | 该文件不存在（U07 独占路径） | `PASS=127 FAIL=0` exit=0 |
| `tests/integration/test_shadow_labels.py` | `PASS=100 FAIL=0` exit=0 | `PASS=100 FAIL=0` exit=0 |
| `scripts/smoke_contracts_v3.py` | `PASS 173 / FAIL 0` exit=0 | `PASS 173 / FAIL 0` exit=0 |
| `scripts/smoke_u06a_shadow_isolation.py` | `PASS 52 / FAIL 0` exit=0 | `PASS 52 / FAIL 0` exit=0 |
| `scripts/smoke_u09_release_evidence.py` | `60 通过 / 0 失败` exit=0 | `60 通过 / 0 失败` exit=0 |
| `tests/acceptance/test_release_evidence_v2.py` | `78 通过 / 0 失败` exit=0 | `78 通过 / 0 失败` exit=0 |
| `scripts/check_quality.py`（含 ruff） | — | `QUALITY CHECK OK files=303 untracked=0`，**未新增任何 ruff exclude** |

> **补丁 02 未应用时的对照**：在 `sandbox_branch`（作者 head，不打补丁）里，
> `tests/integration/test_shadow_labels.py` 为 `PASS=99 FAIL=1`
> （唯一失败即「独占路径四个模块齐备」）；加上补丁 02 后回到 `PASS=100 FAIL=0`。
> 这就是补丁 02 存在的**唯一理由**，也是本任务唯一的跨任务耦合点。

## 应用方式

```bash
git apply 01_audit_database_shadow_v2_statistics.diff
git apply 02_test_shadow_labels_module_boundary.diff
```

`git apply --check` 在 3 种 git 配置下均 `exit=0`、`Skipped=0`、`Checking patch` 覆盖全部文件：
默认 / `-c core.autocrlf=false -c core.eol=lf` / `-c core.autocrlf=true`。

## 集成说明（给 I）

* 必须先合入 `src/intel/shadow_v2/statistics.py` 与
  `tests/integration/test_shadow_statistics.py`、`tests/fixtures/shadow/statistics/`
  （作者分支已提交），否则补丁 01 的落库方法没有消费者、补丁 02 也没有必要。
* 两态探测标记：`audit.database.U07_INTEGRATION == "u07.shadow_v2.statistics.v1"`。
* `tests/integration/test_shadow_statistics.py` **两态均可跑**（已在沙箱实测）：
  `sandbox_branch`（不打补丁）**120 PASS / 0 FAIL**，`sandbox_patched`
  **127 PASS / 0 FAIL**（差值即 7 条落库/幂等/摘要断言），不假定补丁是否已应用。
* 补丁 01 的插入点紧邻 U06b 的 `shadow_v2_labels`（SCHEMA 段）与
  `shadow_v2_coverage`（方法段）：若其他任务也在这两处附近落改动，请按
  「集成版本 → 目标内容」重出 diff，不要手工猜冲突解法（RESUME lesson 8）。

## 未做（如实登记）

* 未做统计快照的 UI/看板渲染（`decisions.html` 零改动）。
* 未把统计接进 `src/intel/service.py` 的只读状态，也未接进采集循环。
* 未做跨 horizon 的配对（horizon 是「同机会键」的一段，不同 horizon 不可配对）。
* 费用压力点的区间用的是配对级（iid）自举，**没有**对压力点做分块自举；
  分块（cluster）区间只出现在 `paired_statistics` 的配对差上——这一点在报告的
  `assumption` 字段里如实标注（`iid_pairs` vs `independent_blocks`）。
* 不做影子权重晋升、不改首批行为、不改风险档/白名单/live 开关。

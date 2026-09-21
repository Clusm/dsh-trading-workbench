# U13-FU2 补丁包（目录名 `ce412fa`）

> 目录名 = **打包时（`.diff` 生成时）的分支 head** `ce412fa`。`.diff` 内容 =
> `7885e6a..ce412fa`（base `7885e6a` 已含 U13-FU1 的四条 P1 修复与补丁 01–04 的
> 集成提交 `acf9ced`）。本目录自身若在本 head 之后落库，不影响 `.diff`。

本目录是 **U13-FU2（E 线收尾）** 的补丁包：修 U13-FU1 独立复验
（`docs/dispatch/reviews/U13-FU1/af76443/Q_r1.md`，PASS-with-notes）新发现的两条
阻塞收口项 + 一条文档不一致：

| # | 事项 | 复验原始定级 | 本次处置 |
|---|---|---|---|
| **N1** | `before_entry()` **裸形态**（未注入 facts）在"状态根没有账本文件 / 账本没有 `order_outbox` 表"时 **fail-open** | P2（有界） | **修**：读不出账本事实一律阻断 |
| **N2** | `src/core/service.py:314`（启动）与 `:1416`（`set_control`）仍是**裸 `before_entry()`**，且验证器完全不触及 `service.py` | P3 | **修**：两处改走"注入账本事实"；验证器扩展覆盖两条路径 |
| **N3** | "demo 只被检出并报告、不阻止启动"与代码行为不符 | P3 | **修**：口径按实测更正 |

分支内**独占提交**的文件（不需要 `.diff`）：

| 文件 | 改动 | 提交 |
|---|---|---|
| `src/core/ops/recovery.py` | +37 / −8 | `79931e0` |
| `scripts/ops/verify_u13f1_recovery_gate.py` | +230 / −2 | `ce412fa` |
| `docs/dispatch/patches/U13-FU1/bc112fe/README.md`、`docs/无人值守升级任务清单_2026-09-19.json` | +18 / −0、+1 / −1 | `66ce106`、`93e7038` |

**共享/他线文件**的改动一律只以本目录的 `.diff` 交付（同时也在分支内提交，
便于"分支自洽 + 集成树可单独应用"两条路都成立）：

| 文件 | 补丁 | 说明 |
|---|---|---|
| `src/core/service.py` | `01_service_startup_and_set_control_ledger_facts.diff` | N2：启动 + `set_control` 两处改走 `_recovery_gate_facts(_recovery_gate_ledger_facts())`（与就绪链同一个入口）。 |
| `tests/acceptance/test_supervision.py` | `02_acceptance_gate_fixture_ledger.diff` | N1 的**夹具前提**：门禁夹具补一份真实账本（空 `order_outbox`）。断言原文一行未改。 |
| `docs/dispatch/patches/U13-FU1/bc112fe/README.md` + 任务清单 JSON | `03_n3_docs_demo_wording.diff` | N3：更正"demo 只被检出并报告"的口径。 |

---

## 1. N1 前后对照：裸形态不得 fail-open

**命令**（两棵树：`before` = `git archive 7885e6a`，`after` = `git archive ce412fa`；
两棵都在解包后**立刻** `git init && git add -A -f` 提交一次）：

```bash
python probe_n1_bare_before_entry.py <tree>      # 探针源码见 evidence/*.py.txt
```

探针给门禁手写一份**自洽摘要**的 `CLEARED` 门禁（= 复验 Q1/Q2 的绕过形态），
唯一变量是"门禁所在状态根里的账本形态"：

| # | 状态根里的账本 | before（`7885e6a`） | after（`ce412fa`） |
|---|---|---|---|
| S1 | **没有账本文件** | `allowed=True codes=('GATE_CLEARED',)` **fail-open** | `allowed=False codes=('EVIDENCE_UNVERIFIED',)` |
| S2 | 账本在、**没有 `order_outbox` 表** | `ledger_reconcile_facts={...,'source':'not_applicable'}` → **`allowed=True` fail-open** | `allowed=False codes=('EVIDENCE_UNVERIFIED',)` |
| S3 | 账本在、有未收口 ENTRY 单 | `allowed=False / RECONCILE_REQUIRED_BEFORE_ENTRY` | 同左（不回归） |
| S4 | 账本在、`order_outbox` 为空 | `allowed=True / GATE_CLEARED` | 同左（**不误伤**） |
| S5 | 注入 `source="not_applicable"` 的事实 | `allowed=True` fail-open | `allowed=False / EVIDENCE_UNVERIFIED` |
| S6 | 注入干净事实 | `allowed=True` | 同左 |
| S7 | 注入 `unresolved_entry_orders=1` | `allowed=False` | 同左 |

```
before: PROBE N1: 4 通过 / 3 失败   exit=1   （3 条失败 = S1/S2/S5 的 fail-open）
after : PROBE N1: 7 通过 / 0 失败   exit=0
```

实现要点（`src/core/ops/recovery.py`）：

- 新增判据 `_facts_unreadable(facts)`：`None`（读不出）或
  `source="not_applicable"`（**不适用 != 干净**，正是 `ledger_reconcile_facts`
  自己写的契约）→ 一律 `EVIDENCE_UNVERIFIED`（**复用既有原因码，无新码**）。
- 删掉"按 `ledger_snapshot_present()` 决定要不要复验"这条分支——它就是
  "活动库读不出事实就放行"的口子。
- `ledger_snapshot_present()` **保留**（未删），退为诊断/报告用；docstring 已写明
  判定不再依赖它。

## 2. N2 前后对照：启动 / `set_control` 真的受门禁约束

```bash
python probe_n2_service_paths.py <tree>
```

探针在隔离临时状态根里**真的构造一次 `TradingService`**（paper、隔离
`project_root`、不联网、不下单、不读真实 `data`/`.env`），并 spy
`RecoveryGate.before_entry` 记录两条路径传入的 `facts`。关键场景（同一份账本、
同一时刻，**门禁自己的只读快照**与**本进程账本事实**结论相反）：

> 账本用真实 `AuditDatabase` 建（迁移齐全 → 与生产同形），最后一颗 SUBMITTING
> ENTRY **只提交进 `-wal`**，且**不带 `-shm`** 地拷回。门禁自己的
> `immutable=1` 只读快照看不到它；服务本进程正常打开的连接会重放 `-wal` 看到它。

| 观察 | before（`7885e6a`） | after（`ce412fa`） |
|---|---|---|
| 启动期 `before_entry` 调用 | 2 次（1 次**裸**、1 次注入） | 2 次，**0 次裸**，注入
`{'unknown_orders':0,'submitting_orders':0,'unresolved_entry_orders':1}` |
| `set_control("normal")` 期调用 | 1 次，**裸**（`facts=[]`） | 1 次，注入账本事实 |
| 启动后控制状态 | `close_only` | `close_only` |
| `set_control("normal")` 结果 | `{'ok': False, 'recovery gate blocked: RECONCILE_REQUIRED_BEFORE_ENTRY...'}` | 同左 |
| 干净账本对照：控制状态 / `set_control` | `normal` / `ok=True` | `normal` / `ok=True`（**不误伤**） |

```
before: PROBE N2: 7 通过 / 2 失败   exit=1   （2 条失败 = 两条路径"未注入事实"）
after : PROBE N2: 9 通过 / 0 失败   exit=0
```

**如实说明（重要）**：在**同一个服务实例**里，"门禁自己读的账本"与"服务本进程
读的账本"是**同一个文件**（探针里也逐字校验了这一点）；服务开库时会创建 `-shm`，
从而把门禁的读取从 `immutable=1` 切到 WAL 感知，于是**行为结果**在这个场景下
两态相同（都阻断）。所以 N2 的可验证增量是**接线形态**（`facts` 是否为本进程
账本事实，spy 直接观测）与"不再依赖门禁的 sidecar/快照启发式"，而不是一个新的
可达行为翻转。裸形态与注入形态的**行为**差异只出现在"门禁自己读不出账本事实"
的窗口——那正是 N1 已在**门禁自身**堵住的那一类，且 N1 的堵法更钝（一律阻断），
注入形态则更精确（进程账本可证干净时仍放行，见上表干净账本一行）。

## 3. N3 前后对照：口径按实测更正

```bash
python probe_n3_doc_vs_code.py <tree>
```

| 观察 | before | after |
|---|---|---|
| `validate('demo')` | `('STATE_ROOT_MISSING',)` | 同左 |
| `assert_environment_bound('demo')`（未声明状态根） | **抛** `RuntimePathsError: ... reasons=STATE_ROOT_MISSING` | 同左 |
| U13-FU1 补丁包 README 是否登记"会抛" | **否**（FAIL） | **是**（PASS） |
| 任务清单里 U13-FU1 的 tradeoff 是否仍称 demo 只被检出/仅报告 | **是**（FAIL） | **否**（PASS） |

```
before: PROBE N3: 1 通过 / 2 失败   exit=1
after : PROBE N3: 3 通过 / 0 失败   exit=0
```

更正后的话术（README 新增小节 + JSON tradeoff）：**会抛**（`STATE_ROOT_MISSING`，
不限 live）；默认部署**仍能启动**是因为该 API 在生产**零调用点**
（`TradingService` / `run_web.py` 都不调它，`assert_environment_matches_state_root`
对非 live 早退），不是因为该 API 不拦；**接线前必须先声明状态根**，否则未声明的
demo/paper 部署会被启动期挡死。

## 4. 取舍说明（N1 的方向选择）

1. **缺账本怎么判**：判为**未 CLEARED**（`EVIDENCE_UNVERIFIED` 阻断），不采用
   "要求调用方必须注入事实"的写法——后者会让所有独立巡检脚本/`RecoveryGate` 的
   自带判定直接失效，反而更容易被绕过（调用方只要不传 facts 就回到裸形态）。
   宁可多阻断。
2. **正常启动有没有被误伤**：**没有**，两条理由都是实测的：
   - 从未恢复过的部署走 `GATE_NOT_INSTALLED` **在到达账本判定之前**就返回放行
     （`before_entry` 前置分支）；默认部署不受影响。
   - 恢复过的部署里，账本由服务自己在 `AuditDatabase` 迁移时创建，服务启动时
     走**注入**路径 → 进程账本可读且干净就放行（探针 `[clean]` 一行：
     控制状态保持 `normal`、`set_control('normal')` 成功）。
3. **唯一的"副作用"**：**合成夹具**（门禁放在一个没有账本的临时目录）现在会被
   阻断。`tests/acceptance/test_supervision.py` 的 `[7]`/`[7b]` 正是这种夹具，
   所以补了 `_make_gate_ledger()`（一份空 `order_outbox` 的账本）。
   **断言原文一行未改**——改的是夹具前提（生产里门禁所在状态根总是带账本），
   不是把断言放宽。这是本次唯一的共享测试文件改动，见补丁 `02`。
4. **登记（未收口的相邻口子）**：`complete_reconcile` 在账本**不可读**时仍可
   按证据自声明把状态置成 `CLEARED`（合成夹具兼容，U13-FU1 原有取舍）。N1 之后
   这种 `CLEARED` **不产生任何放行能力**（读取时一律阻断），但状态位会被写脏；
   完全收紧需要在 `complete_reconcile` 侧也拒绝，属下一轮范围，**此处如实登记**。
5. `ledger_snapshot_present()` 变成事实上的诊断接口（判定不再调用它）。保留而非
   删除：它是公开方法，删掉属 API 变更，不在本任务范围内。

## 5. 沙箱 full delta（pristine vs patched）

方法：两棵树都是 `git archive` 导出后**立刻** `git init && git add -A -f` 提交一次
（避免无 `.git` 让 `test_secret_scanner.py` / `smoke_u10a_release_package.py` 两边
同错、得出假 delta），再**逐脚本**跑官方清单（清单从
`scripts/run_offline_regression.py` 的 `SCRIPTS` 读入，不另抄一份；环境与官方入口
同口径：`EXECUTION_MODE=paper`、清 `MTA_PROFILE`/`MTA_STATE_ROOT`/`MTA_DATA_ROOT`、
清凭据变量、`PYTHONUTF8=1`）。逐脚本 rc 原文见 `evidence/reg_before.tsv` /
`evidence/reg_after.tsv`。

| 项 | 结果 |
|---|---|
| 脚本数 | **85 / 85**（两棵树同一集合） |
| pristine（`7885e6a`）非零脚本 | **0 / 85** |
| patched（`ce412fa`）非零脚本 | **0 / 85** |
| **NEW_FAIL**（pristine rc=0 → patched rc≠0） | **∅** |
| **FIXED**（pristine rc≠0 → patched rc=0） | **∅** |
| 两边同非零 | **∅** |

受影响冒烟（全在 official 清单内，逐条 rc 均 0）：

| 脚本 | pristine | patched |
|---|---|---|
| `scripts/smoke_u13_supervision.py`（内含 `tests/acceptance/test_supervision.py`、
`ops/verify_runtime_paths.py`、`ops/verify_recovery_plan.py`、
**扩展后的** `ops/verify_u13f1_recovery_gate.py`） | 0 | 0 |
| `scripts/smoke_contracts_v3.py` | 0 | 0 |
| `scripts/smoke_recovery_bundle.py` | 0 | 0 |
| `scripts/smoke_windows_service_plan.py` | 0 | 0 |
| 扩展后的验证器（单跑） | 45 通过 / 7 失败（exit 1） | **52 通过 / 0 失败（exit 0）** |

## 6. 复验纪律

- 三个补丁在 **3 种 git 配置**下 `git apply --check --verbose` 全部
  `rc=0 / Checking patch≥1 / Skipped=0`（在 **pristine `7885e6a`** 树上执行）。
- `scripts/check_quality.py`（含 ruff）：`QUALITY CHECK OK files=295 untracked=0`，
  exit 0；**未新增任何 ruff exclude**。
- 验证器两态：pristine 树跑**扩展后**的验证器 = 45 通过 / 7 失败（exit 1，7 条即
  N1×3 + N2 接线×4），修复后 = 52 通过 / 0 失败（exit 0）。

## 7. 未尽事项（如实登记）

1. **N2 的行为增量不可达**（见 §2 的如实说明）：本任务只证明了"接线形态"与
   "不误伤"，没有构造出"裸形态放行 / 注入形态阻断"的同一文件行为翻转——
   因为服务一开库就会创建 `-shm`，把门禁的读取变成 WAL 感知。若后续要证明
   行为差异，需要在"门禁读不到账本"的窗口里做，而那已被 N1 一刀切阻断。
2. `complete_reconcile` 侧的同类口子未收紧（§4.4）。
3. 与 U13-FU1 相同的边界仍然存在且未处理：门禁文件 + 安装戳**同时**删除 →
   `GATE_NOT_INSTALLED` 放行；同时篡改账本 `order_outbox` 行不可挡。
4. 未做真实交易所侧 / 真实断电撕裂写 / 真实多账户凭据 / SCM 双 profile 注册验证；
   全部证据来自进程内假 broker 与合成状态根。
5. 未修任何其它 P2/P3（含复验 N4：补丁 05 不必要、作者对 base 红灯的归因有误），
   按约定留给 U12。

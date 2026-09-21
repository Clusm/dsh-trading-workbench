# 无人值守首批 RC 交付物（DRAFT）

> **状态：DRAFT — 未冻结。**
> 本目录是 U12 BUILD 检查点的交付物。**RC 冻结**与**7 天观察窗启动**由编排者 I 决定，
> 本文档**不**宣布 RC 已冻结、不宣布任何观察门禁已通过。

## 0. 一分钟看懂

| 项 | 值 |
|---|---|
| RC 名称 | `unattended-rc`（首批必交范围） |
| 交付状态 | **DRAFT / BUILD 候选**（未冻结，未部署，未开始观察） |
| 源码候选 commit | `7885e6a`（分支 `task/U12` 的 base；= U13-FU1 集成后的集成树 HEAD） |
| 集成契约 | `contract-v3.0`（标签 `ffdab44`，冻结产物） |
| 交付物范围 | 仅 `docs/releases/unattended_rc/**`、`tests/acceptance/test_unattended_release.py`、`.github/workflows/ci.yml`（+ 登记补丁） |
| 是否改产品逻辑 | **否**（本 RC 交付物不含任何 `src/**` / `scripts/**` 产品逻辑变更） |
| 观察门禁 | **未开始 / 未完成**（见 `observed_status.md`） |
| 实盘 | **未启用**（`live_enabled=false`，需用户另行明确指令） |

## 1. 纳入范围

首批必交任务（全部 `INTEGRATED`，见台账 `docs/无人值守升级任务清单_2026-09-19.json`）：

| 任务 | 标题 | 状态 |
|---|---|---|
| U00 | 冻结契约、基线和 CI 清单 | INTEGRATED |
| U01 | 资金费流水与成本闭环 | INTEGRATED |
| U02 | 策略仓管理和有界退出授权 | INTEGRATED |
| U03 | 逐意图可验证研究证据 | INTEGRATED |
| U04 | 部分成交、保护和重放验收 | INTEGRATED |
| U05 | 真实耗时、模型降级和决策原因 | INTEGRATED |
| U06a | 影子最小隔离与口径修正 | INTEGRATED |
| U08 | 策略漏斗与管理责任展示 | INTEGRATED |
| U09 | 发布证据与事件恢复门禁 | INTEGRATED |
| U10a | 可运行发布包与准确运维说明 | INTEGRATED |
| U13 | 托管、单实例及备份恢复 | INTEGRATED |
| U14 | 外部告警送达与主机失联检测 | INTEGRATED |
| U15 | 运行身份、有效配置和控制权限 | INTEGRATED |
| **U12** | **RC 集成、故障验收和观察交付** | **IN_PROGRESS（本交付物）** |

**不在本 RC 范围**（后置，`PLANNED`）：U06b、U07、U10b、U11。
其中 U06b/U07 是影子 v2 标签与配对统计——正是 `F11:v2_labels=DEFERRED` 的来源。

## 2. `release_id` 口径

本 RC 的 `release_id` **由代码铸造，不由人手写**，口径如下（`src/core/release_manifest.py:build_release_manifest`）：

```
release_seed = "|".join((
    environment, content_hash, dependency_hash, strategy_hash, model_hash,
    schema_version, threshold_version, egress_id,
    git_commit, account_ref, strategy_cards_hash,
))
release_id = "rel-" + sha256(release_seed)[:20]
```

- `content_hash`：`src/**/*.py` 的按相对路径归一化集合哈希（`_safe_source_files`）。
  **本 RC 交付物不新增/修改 `src/` 文件，故 `content_hash` 与候选 `7885e6a` 一致。**
- `config_hashes`：`config/{settings,decision_policy,agents,risk_learning}.yaml` +
  `config/risk_profile.lock.json` + `config/windows_service.json`。
- `dependency_hash`：`DEPENDENCY_FILES`（含 `requirements*.txt`、`pyproject.toml` 等）。
- `strategy_config_hash` / `model_config_hash` / `strategy_cards_hash`：决策/模型配置与策略卡。
- `threshold_version`：发布门禁阈值版本（当前 `DEFAULT_THRESHOLDS` = 200/30/7/99.9%，**未改动**）。
- `account_ref`：由 `account_reference(environment, account_ref)` 脱敏派生，**不含账户标识原文**。

铸造命令（离线、只读、不读 `.env`）：

```powershell
$py = "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
cd C:\Users\Tao\Desktop\币安\mat-wt\W3-rc
& $py -c "import sys; sys.path.insert(0,'src'); from pathlib import Path; from core.release_manifest import build_release_manifest; m=build_release_manifest(Path('.'), environment='demo', db_file=None); print(m.release_id); print(m.content_hash)"
```

铸造结果逐字记录在同目录 `release_manifest.json`（**全部为实测值，非手写**）。

### 2.1 铸造实测值（口径与限制）

| 字段 | 实测值 | 说明 |
|---|---|---|
| `release_id` | 见 `release_manifest.json` | 由上述种子哈希派生；种子含 `git_commit`，故**每提交一次即变** |
| `artifact_hash` = `content_hash` | 见清单 | `src/**/*.py` 的归一化集合哈希；**本 RC 交付物不碰 `src/`，故与候选 `7885e6a` 一致** |
| `dependency_hash` | 见清单 | `DEPENDENCY_FILES` 集合哈希（锁文件被改即变） |
| `config_hashes` | 6 个配置文件的 sha256 | 含 `config/risk_profile.lock.json`（与 `check_quality` 的 `PROFILE LOCK OK` 同源） |
| `schema_version` | **0** | **未提供账本**（`db_file=None`）——本交付物**不读运行目录真实 `data/`**，故无法取真实迁移版本。**部署时必须由真实账本重铸** |
| `environment` / `account_ref` | `demo` / `demo:unknown` | 脱敏派生，**不含账户标识原文** |
| `runtime_instance_id` / `effective_config_hash` / `model_route_hash` | **空** | **`U12-BUILD-F1`（§5.1）**：`identity_blockers = ["IDENTITY_COLLECT_FAILED"]` |
| `dependency_comparison_status` | `UNAVAILABLE` | 上述降级的连带结果 |

**限制（必须如实登记）**：
1. `release_id` 的种子含 `git_commit` ⇒ **只在铸造提交上稳定**；后续任何提交都会改变它。
   本清单记录的是**铸造时的 `git_commit`**，其后仅追加该清单文件本身（不改 `content_hash`/`artifact_hash`）。
2. `schema_version=0` 表示"未提供账本"，**不是**真实迁移版本；部署侧必须重铸。
3. 身份字段为空是**缺陷后果**，不是"无需身份"：见 §5.1。

## 3. 故障矩阵

`F01–F25` 逐条的反例 → 必须结果 → 证据 → 覆盖状态：见
[`FAULT_MATRIX_F01_F25.md`](FAULT_MATRIX_F01_F25.md)。

- 定义来源：`docs/无人值守交付与多Agent执行规格_2026-09-19.md` §6（F01–F16）、
  §14（F17–F24、F11 的 DEFERRED 规则）；F25 由台账 `fault_matrix_extension` 登记。
- **`F11` 逐字记录 `F11:v2_labels=DEFERRED`**，首批只验旧口径标识 / C 禁用 /
  零执行权重及隔离，**不计作已验收功能**。
- 缺失必需能力的条目**写明缺什么、为什么现在不能验**，**不写 N/A**。

## 4. 观察门禁

`observed_status`：**7 天 / 200 方向候选 / 30 完整闭合 三项均"未开始/未完成"**
（观察窗尚未启动）。逐字见 [`observed_status.md`](observed_status.md) 与
[`observed_status.json`](observed_status.json)。

## 5. 跨任务挂账消费（不得静默忽略）

台账 `dispatch.known_issues` 与 `dispatch.w3_followups` 中，**影响本 RC 或由 U12 负责**的条目。
逐条给出：本 RC 的处置 / 影响 / 状态。**凡标 OPEN 的，本 RC 不声称已解决。**

| 挂账 ID | 摘要 | 归属 | 本 RC 处置 |
|---|---|---|---|
| `W1-JOINT-WIRING` | U03 接线 × U15 身份必须联合落地；单独启用会让缺 pinned 身份时管线 fail-closed 拒绝**全部**开仓 | I (U12 BUILD) | **未执行联合接线**（本交付物不改 `src/`）。手册 S05 联合候选验收（缺 key 阻断新仓而保留管理）**尚未做** → 见矩阵 F13/F15 适用性说明 |
| `U08-U12-WIRING` | `graph.py` 未注入 funnel/ledger（grep 0 命中）→ 执行侧逐票原因与跨进程去重**生产不可达** | U12 | **未接线**（同上，属产品逻辑）。U08 README §五 已如实记"生产不可达（待 U12）"。影响 F19/F24 的生产可达性 |
| `U08-R5-FU1` | `release()` 撤销被同轮成功 `save()` 丢弃（N10）；`release_count=0` 且执行器 "账本释放未完成" 分支为死代码 | I/U12 | **未修**（属 `src/`）。编排失效风险如实登记；不阻断集成（fail-closed） |
| `U08-R5-FU2` | 执行器内 `db.*` 调用无护栏；审计库 `OperationalError` 穿出整轮、`decision=None` | U12/I | **未修**（属 `src/`）。既有形态，与 F05 的"DB 锁/满"直接相关 |
| `U02-LEDGER-PARTITION` | 声明 `MTA_ACCOUNT_REF` 会使历史账本行不可幂等（同 `trade_id` 两域各一行）；先迁移后声明 | U02/U13/I | **本 RC 不得在生产启用 `MTA_ACCOUNT_REF`**（迁移脚本未落地）。影响 F18 |
| `U02` `_consumed_ids` 纯内存 | 重启后同一 `exit_key` 会再次 `SUBMIT_REDUCE` | U02/U13 | **未修**。登记为 F04/F03 的重启面已知缺口 |
| `AlertQueue` 无生产消费者 | U14：监控 `errors`，且 `PERSIST_FAILED` 不在其中 | U14 | **未修**。告警积压/弃投的监控口径缺口，影响 F12/F22 |
| `U09-OUTCOME-VS-LIFECYCLE-COST` | 「30 完整闭合」读 outcome 成本快照而非 lifecycle 权威态（fail-open，继承自基线） | **U12 须裁决是否 RC 前修** | **待裁决**（见矩阵 F23 + 第 8 节）。发布门禁权威性缺口 |
| `U09-NET-UNKNOWN-PADDING` | 净收益未知的样本仍计入 30 闭合且**零门槛**；最少 1 个未知样本即可把 29 条真凭证凑成 30 | U12/U09 后续 | **待裁决**（见矩阵 F23）。发布门禁口径缺口 |
| `U13FU1-BARE-BEFORE_ENTRY-FAILOPEN` | 裸 `before_entry()` 在缺账本时 fail-open；`service.py:314/:1416` 未注入事实（N1+N2） | U12 须裁决；**正在由 `U13-FU2` 修** | **在修（IN FLIGHT）**。本 RC **不声称已修**；注册为 F14 待裁决项 |
| `U13FU1-README-VS-CODE-N3` | README 称 demo 不阻止启动，但 `assert_environment_bound('demo')` 实测会抛（P3 文档卫生） | U12 | **待裁决**（见矩阵 F18 待裁决项） |
| `S08-SHADOW-GATE-LEAK` | 影子样本进发布门槛分母（未按 source 过滤；50B+50C 使 200→300） | **U09** | **已闭合（FIXED）**。`src/core/release_evidence.py` 只计入 `intel.shadow_status` 判定的 `execution_decision_copy` 来源；见矩阵 F16/F24 |
| `KI02` | `run_offline_regression` 在无 `.venv` 的 worktree 中中止 | U13/U10a/I | **已闭合**：本树离线回归实测 `exit 0`（见第 6 节） |
| `KI03` / `KI04` | `scan_tracked_secrets` 误报使 `check_quality` 变红 | I/U05/U12 | **已闭合**：本树 `QUALITY CHECK OK files=295 untracked=0`、`SECRET SCAN OK ... registered_false_positives=2`。**未新增 ruff exclude** |
| `U00-R5-FU1` | T3/T5 拒绝面缺自动化 golden 覆盖 | I | **未补**。登记为 smokes 门禁未尽事项 |
| `U00-R5-FU2` | rules 深嵌套(>=5000) 触发裸 `RecursionError`（P3） | I | **未修**。方向仍 fail-closed，登记 |
| `U01-R3-FU1` | 文档内嵌验收断言写死 `current_schema_version()==16`（迁移 17 后应为 17） | U10a/U10b/I | **未修**（文档准确性，fail-closed 方向），登记 |
| `U04-R4-FU1` | `manual_resolve_unknown` 是状态列第二写入者，绕过 `_update` 守卫 | I/U12 | **未修**（当前无生产调用点）。U12 接线前必须处置 |
| `U05-COUPLING-REVIEW` | U05 patch 09 污染 `agent_runs.latency_ms`，经 U01 `AVG(latency_ms)` 影响每日复盘 | U05 | **未修**。复盘统计正确性缺口，登记 |
| `RECOVERY-BUNDLE-P2-CLUSTER` | 两套恢复包单向不互通 / 损坏锁静默 / 0 字节锁单方接管 / 同根两账户覆盖基线 / 恢复丢运行代 | E线/U12 | **未修**。已列入第 7 节运维风险清单 |
| `U09-K06-GREEN-BUT-NO_GO` | 伪造完整身份+空证据可把 K06 五组点绿，权威 `release_readiness` 仍 `NO_GO` | U12 | **待处置**：本 RC 明确 **K06 全绿 ≠ 发布通过**（见第 4 节） |
| `U09-REVISION-CONFLICT-DEAD-BRANCH` | `REVISION_CONFLICT:outcome_id=` 子分支在真 schema 上不可达（P3 测试卫生） | U09 后续/U12 | 登记（对门禁无影响，fail-closed） |
| `U09-P3-CLUSTER` | `thresholds=` 入参可显式调低 `required`；`smoke_u02` 自计数瑕疵；U01 契约函数无生产调用方 | U12 | 登记。见第 8 节待裁决 |
| `CONTRACT-FREEZE-BOUNDARY` | 残余限制不得直接写入已冻结的 `docs/contracts/README.md` §12 | I | 遵循：本 RC 未改冻结契约 |
| `W1-PATCH-BATCHING` / `W1-MANIFEST-TRANSIENT*` | I 侧原子登记/同批应用事项 | I | 本 RC 不涉及（属集成动作） |
| **`U12-BUILD-F1`**（**U12 BUILD 新发现**，台账未登记） | **K01 身份采集在真实 `src/` 树上必然失败**：`collect_artifact_files(root)` 默认扫描 `src/**/*.py`，对路径分量做 `is_sensitive_name()` 子串匹配，而 `SENSITIVE_KEY_MARKERS` 含 `"authorization"` → 文件名 `src/core/management_authorization.py` 命中 → `CredentialLeakError`。`_identity_evidence` 单点降级为 `IDENTITY_COLLECT_FAILED`，于是 `runtime_instance_id`/`effective_config_hash`/`model_route_hash` 全空 | **U15（守卫口径）/U02（文件名）/I** | **未修**（属 `src/`，超出 U12 硬约束）。**属既有缺陷**（`management_authorization.py` 来自 U02，`SENSITIVE_KEY_MARKERS` 来自 U00），**非 U12 引入**。后果链见 §5.1。**RC 不得据此宣称身份已验** |

### 5.1 U12 BUILD 新发现：K01 身份采集在真实 `src/` 树上必然失败

> **本条是 U12 BUILD 会话在**运行集成树代码**（而非引用任何作者报告）时发现的。**
> 台账 `known_issues` **未登记**此条。

**事实链（全部为本树实测）**：

1. `src/core/contracts/common.py:19` `SENSITIVE_KEY_MARKERS` 含 `"authorization"`。
2. `src/core/runtime_identity.py:320` `is_sensitive_name()` 只做 `lower()` + **子串匹配**。
3. `src/core/runtime_identity.py:557` `collect_artifact_files()` 对每个路径**分量**调用它；
   默认 `relatives=None` 即扫描 `src/**/*.py`。
4. `src/core/management_authorization.py`（U02 交付物）的**文件名**含 `authorization`
   → 命中 → `CredentialLeakError: credential-bearing fields rejected:
   artifacts.src/core/management_authorization.py`（实测异常原文）。
5. `src/core/service.py:184` **生产启动路径**调用 `build_release_manifest(...)`，
   **未传** `artifact_relatives` → 走默认扫描 → 必然抛错 → `_identity_evidence`
   单点降级 → `identity_blockers = ('IDENTITY_COLLECT_FAILED',)`。
6. 实测铸造结果（`release_manifest.json`，本目录）：
   `artifact_hash = 544d176a…`（**非空**，来自 `content_hash`），但
   `runtime_instance_id = ""`、`effective_config_hash = ""`、`model_route_hash = ""`。
7. `_identity_block()`（`release_evidence.py:812`）对同一清单的实测输出：
   `state = NO_DATA`，`reason_codes = ['IDENTITY_REF_MISSING']`，`runtime_instance_id = None`。
8. ⇒ K06 `engineering` 组为 **NO_DATA** ⇒ `release_readiness` **不可能为 GO**。

**为什么此前没被发现**：`tests/acceptance/test_runtime_identity.py`（本树 exit 0，
`RUNTIME IDENTITY ACCEPTANCE PASS`）使用**合成临时根**或**显式 `artifact_relatives`**，
**从不**对真实 `src/**/*.py` 跑默认扫描 ⇒ U15 的验收套件对本缺陷**盲**。

**本 RC 的处置**：**不修**（`src/` 只读约束）。已在本 RC 登记为：
- 待裁决 **A13**（§8）；
- 矩阵 `F13` 行的 ⚠；
- 本 RC 验收套件 `tests/acceptance/test_unattended_release.py` 第 [8] 节
  **两态不变量**："身份要么完整，要么**显式**带 `IDENTITY_COLLECT_FAILED`，**绝不静默空身份**"
  —— 该不变量在修复前后都成立，且把本缺陷**钉在证据里**。

**修复方向（供 I 裁决，本 RC 不代行）**：① 让 `is_sensitive_name()` 对**路径分量**改用
更精确的口径（如按 `_`/`.` 切分后的 token 精确匹配，或对 `*.py` 源文件名豁免）；
② 给 `collect_artifact_files()` 的 `src/**/*.py` 默认扫描加**有文档依据的文件名白名单/豁免**；
③ 重命名 `management_authorization.py`（会改 U02 契约路径，**不可轻动**）。
**无论选哪种，都不得降低凭据守卫强度、不得新增 ruff exclude。**

## 6. 如何复现（离线，不联网，不读真实 `data/`/`.env`，不下单）

```powershell
$py = "C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
cd C:\Users\Tao\Desktop\币安\mat-wt\W3-rc

# [1] 静态门禁（含 ruff / 依赖锁 / 指纹锁 / 密钥扫描）——必须 exit 0
& $py scripts/check_quality.py

# [2] 官方离线行为回归入口（全部冒烟脚本）——必须 exit 0
& $py scripts/run_offline_regression.py

# [3] 本 RC 验收套件（两态可跑，独立运行）——必须 exit 0
& $py tests/acceptance/test_unattended_release.py
```

实测证据（本轮 U12 BUILD 会话，全部为**本树自跑**，非引用）：

| 命令 | exit | 摘要 |
|---|---|---|
| `scripts/check_quality.py` | 0 | `All checks passed!` / `QUALITY CHECK OK files=295 untracked=0` |
| `scripts/run_offline_regression.py` | 0 | `OFFLINE REGRESSION OK scripts=85`（含 81 个 `smoke_*` + 4 个非 smoke 入口） |
| `tests/acceptance/test_unattended_release.py` | 0 | 见 `evidence/` 日志 |

> 余下 13 个 `tests/acceptance/*.py` 与各 `scripts/smoke_*.py` 由入口 [2] 覆盖；本 RC
> **不**另建第二套回归入口。

## 7. 回滚

本 RC 交付物**不含产品逻辑变更**（无 `src/**` 改动），因此回滚面极小：

- 交付物本身：删除 `docs/releases/unattended_rc/` 与
  `tests/acceptance/test_unattended_release.py`，`git revert` 对应提交即可。
- CI：`.github/workflows/ci.yml` 的增量步可单独 `git revert`；回滚后原有
  `check_quality` + `run_offline_regression` 步保持不变。
- 源码候选 `7885e6a` 未被本 RC 修改，故不需要回滚源码。
- **未部署、未启用 live、未动风险档/白名单**，无运行状态需回滚。

## 8. 待裁决项（本 RC 标出、**不自行决定**）

以下条目由本交付物标出并留 **OPEN**，交由编排者 I / 用户裁决。**本 RC 不替其决定。**

| # | 待裁决项 | 出处 | 选项 |
|---|---|---|---|
| A1 | 裸 `before_entry()` fail-open + `service.py` 两处未接线（N1+N2） | `U13FU1-BARE-BEFORE_ENTRY-FAILOPEN` | RC 前补接线 / 明确接受并登记（**U13-FU2 正在修**） |
| A2 | U13-FU1 README 与代码行为不一致（N3） | `U13FU1-README-VS-CODE-N3` | 改 README / 改代码 |
| A3 | 发布门禁读 outcome 成本快照而非 lifecycle 权威态 | `U09-OUTCOME-VS-LIFECYCLE-COST` | RC 前修 / 接受（既有口径） |
| A4 | 净收益未知样本计入 30 闭合且零门槛 | `U09-NET-UNKNOWN-PADDING` | RC 前加门槛 / 接受 |
| A5 | `thresholds=` 入参可显式调低 `required`（无配置路径） | `U09-P3-CLUSTER` | 封死入参 / 接受 |
| A6 | U03 接线 × U15 身份的联合启用（S05 联合候选验收） | `W1-JOINT-WIRING` | 联合落地并验收 / 继续 fail-closed |
| A7 | U08 整轮聚合接线（`graph.py` 注入 funnel/ledger） | `U08-U12-WIRING` | 接线 / 继续登记不可达 |
| A8 | `U08-R5-FU1` / `U08-R5-FU2` 同处收口修复 | 台账 | RC 前 / RC 后 |
| A9 | `U04-R4-FU1` 接线前处置 | 台账 | 接线前必须 |
| A10 | `MTA_ACCOUNT_REF` 启用前置迁移 | `U02-LEDGER-PARTITION` | 迁移落地前不得启用 |
| A11 | U09-P3 `smoke_u02` 自计数瑕疵、`current_closed` 无窗口过滤 | 台账 | 修 / 登记 |
| A12 | 运维 SLO 口径与目标冻结（D05） | 台账 `external_decisions` | **RC 冻结前必须关闭**（`blocks_checkpoint=RC_FREEZE`） |
| **A13** | **K01 身份采集在真实 `src/` 树上必然失败**（文件名 `management_authorization.py` 命中 `"authorization"` 敏感标记 → `IDENTITY_COLLECT_FAILED` → identity `NO_DATA` → `release_readiness` 不可 GO） | **本 RC §5.1 新发现**（台账未登记） | 收窄敏感名口径 / 加源文件名豁免 / 登记接受并说明为何不阻断；**RC 冻结前必须裁决**（阻断 RELEASE） |

## 9. 未尽事项（如实登记）

1. **本 RC 未冻结、未部署、未开始观察。** `BUILD` 之后的 `DEPLOY_DEMO` / `OBSERVE` /
   `OPS` / `RELEASE` 检查点**均未开始**。
2. **未做真实交易所、真实断电、真实多账户、真实告警送达验证**（受硬约束限制，
   且这些属 U12 的 DEPLOY_DEMO/OPS 检查点）。
3. **未执行** U03×U15 联合接线（S05）、U08 整轮接线；因此执行侧逐票原因与跨进程去重
   在**生产上仍不可达**。
4. **未修**任何 P2/P3（`src/` 只读约束）。
5. `F11:v2_labels=DEFERRED`——v2 固定时点标签属 U06b 后置，**首批不验**。
6. 外部待决项 `D01`–`D05` 全部 `PENDING`；其中 **D05（SLO 冻结）阻断 RC_FREEZE**，
   `D03`（账户风险模式覆盖）阻断 RELEASE。
7. 本交付物由 `deepseek-v4.1-flash` 同模型会话产出；矩阵中**引用他人证据**的条目已在
   矩阵"证据来源"列逐条标注（`引用`/`自跑`），未把引用当自验。
8. **`U12-BUILD-F1`（§5.1）未修**：K01 身份采集在真实 `src/` 树上必然失败，K06
   `engineering` 组为 `NO_DATA`，`release_readiness` 不可能为 GO。本 RC **不**声称
   身份已验。**本会话首轮官方离线回归（`smoke_u10a_release_package.py`）也因此曾红**——
   详见 §6.1；那是本 RC 交付物自身文档链接尚未落盘所致的**门禁真实生效**，非缺陷。
9. **`F11:v2_labels=DEFERRED` 的 v2 面**属 U06b 后置；影子权重在 U06b/U07 完成前
   **不得**讨论收益。

## 6.1 官方离线回归的两次实测（含首轮红的根因）

| 轮次 | 结果 | 根因 |
|---|---|---|
| 第 1 轮（矩阵/README 已提交、`observed_status.*` **未**提交时） | `run_offline_regression.py` **FAIL**（`smoke_u10a_release_package.py` 5 项失败） | `docs/releases/unattended_rc/README.md` 的 3 个相对链接指向**尚未落盘**的文件 → `doc_links_resolve` 报 3 条 `BROKEN_LINK`，并连带 `run_web.py --verify-package rc=2`。**门禁真实生效**（U10a 的"不漏包/链接可达"闸门抓到了本 RC 的在制品）。 |
| 第 2 轮（全部交付物落盘后） | `OFFLINE REGRESSION OK scripts=85`，**exit 0** | 修复方式为**补齐文件**（非豁免/非跳过），门禁范围未收窄；第 1 轮的 5 项失败**全部消失** |


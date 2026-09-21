# U13-FU1 补丁包（目录名 `bc112fe`）

> 目录名 = **打包时（`.diff` 生成时）的分支 head** `bc112fe`。补丁内容随后在
> `777c22c` 落库，其后只有对 `service.py` 补丁的加宽（`a12805b`）与本目录
> 正文/`evidence/` 的追加（`d260607`）。`.diff` 的**当前**内容 = `a12805b`
> 之后、`d260607` 之前的状态（正文与证据不改变 `.diff`）。
> `git log --oneline -- docs/dispatch/patches/U13-FU1/` 可回溯全部落库点。

本目录是 **U13-FU1（E 线托管/恢复修复）** 的补丁包。四条 P1 的**核心修复**已在
分支 `task/U13-FU1` 内直接提交（独占路径：`src/core/ops/recovery.py`、
`src/core/runtime_paths.py`、`scripts/ops/verify_recovery_plan.py`、
`scripts/restore_recovery_bundle.py`，另加回归脚本
`scripts/ops/verify_u13f1_recovery_gate.py` 与
`scripts/smoke_u13_supervision.py` 的接线）。

**共享/他线文件**的改动一律只以本目录的 `.diff` 交付，**没有**提交进分支。

## 补丁清单

| 文件 | 性质 | 说明 |
|---|---|---|
| `01_service_recovery_gate_ledger_facts.diff` | 必选 | P1-3 生产接线：`_recovery_gate_facts()` 接受注入的账本事实并传给 `before_entry(facts=...)`；`_collect_entry_readiness_facts()` 把门禁判定挪到 `order_outbox` 读取之后，用**同一批**刚采集的事实复验。 |
| `02_create_recovery_bundle_profile.diff` | 必选 | P1-2 建包侧：`create_recovery_bundle(..., profile=...)` 按 `RuntimePaths` 解析状态根（live → `<root>/data/live`）、锁文件随之对齐、`epoch.profile` 写真实档位，并加 `--profile` 参数。 |
| `03_run_web_state_root_drift_banner.diff` | 建议 | P1-4 可观测：启动横幅打印 `state_base_explicit`，未声明状态根时额外打一条 `WARNING state_root_drift`（默认部署仍可启动）。 |
| `04_verify_runtime_paths_drift_report.diff` | 建议 | P1-4 可观测：`scripts/ops/verify_runtime_paths.py` 报告里增加 `state_root_drift` 并打印。 |
| `05_optional_docs_probe_lint.diff` | **可选、非本任务缺陷** | 见下节。 |

### 05 的说明（请 owner 决定）

`check_quality.py` 的覆盖范围是「Git 已跟踪 + 未跟踪但未被 .gitignore 排除的
`*.py`」，因此它会连带扫描 `docs/dispatch/**` 下**归档的评审证据脚本**。
base `943d839`（归档 W3 F14/F18 演练 + U02 补丁证据）把这些文件带进仓库时，
它们就已经让 `scripts/check_quality.py` 变红：在 base 上
`ruff check docs/dispatch/` 报 **25 条**（E401×3 / E402×8 / E702×9 / F401×5），
与 U13-FU1 的改动无关（本任务改的 4 个源文件 + 2 个测试文件 ruff 全绿）。

`05` 选择在 `pyproject.toml` 里给 `docs/**/*.py` 加**按码放行**
（E401/E402/E702/F401，都是"改写别人归档原文"才会消失的形态），而不是去改
归档证据原文，也不是放宽 `select`。应用 `05` 后 `check_quality.py` 变绿
（`QUALITY CHECK OK files=298 untracked=0`，exit 0）。

**这是一个取舍**：它确实缩小了 lint 对 `docs/**` 归档件的强制面。若不接受，
另一条路是改写归档探针原文（会破坏"归档即当时原文"的性质），第三条路是让
`check_quality.py` 只扫描发布白名单。三条都请 owner 拍板；**U13-FU1 的核心
修复不依赖 05**。

### N3 口径更正（U13-FU2）：`assert_environment_bound` 对 demo 也会抛

本任务登记 P1-4 取舍时写的"默认部署 demo/paper 仍可启动，**只是被检出并报告**"
只对**部署入口**成立，对 `RuntimePaths.assert_environment_bound()` **本身**
不成立，此处更正（原文留在 `docs/无人值守升级任务清单_2026-09-19.json` 的
U13-FU1 `tradeoffs` 里，已同步改口径）：

- **该 API 会抛**：`assert_environment_bound("demo")` 在"未声明状态根"时抛
  `RuntimePathsError`，原因码 `STATE_ROOT_MISSING`（来自 P1-4 让 `validate()`
  可达）。它的语义已从"只拦 live"变成"拦**一切**未声明状态根"。
- **默认部署仍能启动**，原因是该 API 在生产**零调用点**：`TradingService` 与
  `scripts/run_web.py` 都不调它，`assert_environment_matches_state_root` 对
  非 live 早退（`config_loader.py`）。所以"只被报告"描述的是**入口现状**，
  不是这个 API 的行为。
- **接线前必须先声明状态根**：若将来把它接到启动期（P1-4 的可观测化方向），
  未声明状态根的 demo/paper 部署会被它**挡死**——这是"宁可多阻断"的取向，
  但不该在不打招呼的情况下变成启动期硬拦截。

## 应用与复验

```bash
# 三种 git 配置都必须干净应用（均 Checking patch=1 / Skipped=0，rc=0）
git apply --check --verbose <patch>                                   # default
git -c core.autocrlf=false -c core.eol=lf apply --check --verbose <patch>
git -c core.autocrlf=true apply --check --verbose <patch>
```

## 与四条 P1 的对应

- **P1-1**（core 演练不装门禁）→ 只改分支内独占文件，无需补丁。
- **P1-2**（live 档门禁装错位置）→ 分支内改还原布局；**建包侧**需要 `02`
  （否则 live 恢复包永远带 `profile=""`）。
- **P1-3**（证据是自证裸布尔）→ 分支内改 `RecoveryGate`；**生产热路径**用 `01`
  把已采集的账本事实注入，避免依赖活动 WAL 库的只读快照语义。
- **P1-4**（`STATE_ROOT_MISSING` 不可达）→ 分支内改 `validate()`；`03`/`04`
  提供启动期/部署前的**可观测**消费者。

## 复验证据（`evidence/`）

| 文件 | 内容 |
|---|---|
| `before_drill_f14.log` / `after_drill_f14.log` | 评审方原始探针 `drill_f14.py`：修复前 19 通过 / 0 失败（exit 0，即缺陷全在），修复后 **16 / 3**（exit 1），新失败的三条**正是缺陷断言**（P1-1 未装门禁、两条恢复路径行为不一致、P1-3 手写 CLEARED 放行）。 |
| `before_drill_f18.log` / `after_drill_f18.log` | 评审方原始探针 `drill_f18.py`：修复前 31 / 0（exit 0），修复后 **29 / 2**（exit 1），两条新失败即 P1-4 的 `STATE_ROOT_MISSING` 不可达断言。 |
| `before_probe_live_layout.log` / `after_probe_live_layout.log` | 评审方原始探针 `probe_live_restore_layout.py`：修复前 live 档"状态 JSON 平铺 `data/`、门禁在 `data/live/ops/`"；修复后 live 档四个状态 JSON + 库 + 门禁**全部**在 `data/live/` 之下，空 profile 档全部在 `data/` 之下。 |
| `before_u13f1_verify.log` / `after_u13f1_verify.log` | 本任务新增的 `scripts/ops/verify_u13f1_recovery_gate.py` 在 **pristine base（`git archive 943d839`）**= 44 失败 / exit 1；在修复后 = **39 通过 / 0 失败 / exit 0**。两态可跑：符号缺失时逐条判失败，不 ImportError。 |
| `after_drill_f14_e2e.log` | 评审方原始端到端探针 `drill_f14_e2e.py`（真实 `TradingService`，paper）在**打上全部补丁**的树上：11 通过 / 4 失败。4 条失败**同源**——根因是 [3] 段仍按旧语义预期"门禁可在账本还有未收口 ENTRY 时被证据收口"；[3] 的另外 1 条与 [4] 的 2 条都是它的下游。见下一节。 |
| `delta_pristine.log` / `delta_patched.log` | 全量离线回归（84 脚本，逐脚本 rc）pristine vs patched 的失败集合增量对比：两边非零都是同 2 个（`smoke_u10a_release_package.py`、`test_secret_scanner.py`），**均为 `git archive` 导出无 `.git` 导致的 `git ls-files` 失败**，与本次改动无关。**NEW_FAIL = ∅**。 |
| `after_official_regression.tail.log` | 官方入口 `scripts/run_offline_regression.py` 在工作树内实测：`OFFLINE REGRESSION OK scripts=84`，exit 0。 |
| `quality_evidence.log` | `check_quality.py` 的 ruff 现状（见 05 一节）。 |

## 预期行为变更（F14 端到端）：先"真对账"，再收口

`drill_f14_e2e.py` 的 [3] 段在**旧**实现下可以"operator + 二次确认 + 新鲜结构化
证据"直接把门禁从 `RECONCILE_REQUIRED` 拨到 `CLEARED`——**即使账本里那颗
"交易所已成交、本地未收口"的 ENTRY 单还在**。P1-3 之后这条路被有意关掉：
`complete_reconcile` 先读账本真实事实，未收口 ENTRY 仍在时返回
`RECONCILE_REQUIRED_BEFORE_ENTRY`。

正确顺序因此变成 **先让对账真的发生**（快循环的 `reconcile_unknown` 把
UNKNOWN 拉成终态 / 或人工收口）**→ 再提交证据**。这与 F14 必达原话
"先对账再开仓"一致，且是**收紧**而非放宽。探针里那 2 条失败是这个收紧的
直接证据，不是回归。

## 未尽事项（如实登记）

1. **`scripts/create_recovery_bundle.py` 的 profile 支持只在 `02` 里**。未应用
   `02` 时，生产建包脚本仍写 `epoch.profile=""`，live 档 DR 只能靠 core 的
   `create_epoch_bundle` 建包。
2. **恢复目标只按"旧布局 `<target>/data`"落地**。若 live 部署用绝对
   `MTA_STATE_ROOT`（例如 `D:/mta-state`），运维必须把恢复目标放在状态基座的
   父目录，或另行搬迁；`restore_to_new_directory` 未提供 `state_base` 覆盖参数。
3. **live 服务热路径的实时事实**靠 `01` 注入。未应用 `01` 时门禁会退化为
   "自己按只读快照读一次账本"（活动 WAL 库走 `mode=ro`，能看见最新已提交事实，
   但每轮多一次连接）。行为仍 fail-closed，只是开销更高。
4. **未做真实交易所侧验证**：全部证据来自进程内假 broker / 合成状态根；真实
   `query_order` 回执、真实断电/撕裂写、真实多账户凭据、SCM 双 profile 注册、
   真实告警均未涉及。
5. **P1-3 的证据仍由"进程内账本"复验**。能挡住"手写 JSON"和"删/改门禁文件"，
   挡不住"同时篡改账本 `order_outbox` 行"——那已超出"手写门禁 JSON 即放行"
   这一缺陷范围（账本篡改属另一类威胁模型）。已在此显式登记。
6. **`check_quality.py` 在 base 即为红**（见 05 一节）；本任务未把 `05` 提交进
   分支，因此本分支上它仍然红。是否接受 `05` 由 owner 决定。

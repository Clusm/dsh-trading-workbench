# U13-FU1 补丁包（head `bc112fe`）

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

# U13 共享适配补丁（head = `b55c4fb`，修复轮 r2）

基线：`task_base_commit = 0e522b13472cdff3757547752089f5a8d97ca8f8`（= 已审核集成 HEAD）。
本目录的 **12 个** `.diff` 是该基线 → 本 head 工作树内容的 **完整 unified diff**，逐文件一份。
（上一轮目录 `../a27fc17/` 的 11 个补丁已被本目录**取代**：它们只针对 base → `0c0f701`，
本轮 r2 的共享改动不在其中。**二选一**，不要两套都打。）

> **r2 与分支的关系**：本轮共享改动**只以补丁交付**，没有再提交进 `task/U13` 分支。
> `git diff --name-only 0e522b1..b55c4fb` 里的共享文件来自上一轮（已审核、逐字节一致）；
> 本轮的 app.py / service.py / windows_service.py / config_loader.py / entry_readiness.py /
> windows_service_host.py / smoke_windows_service_plan.py 改动仅存在于本目录的 `.diff`。

## 1. 应用方式（I 在集成树上执行；二选一）

```bash
cd <integration-worktree>          # mat-wt/I
for p in docs/dispatch/patches/U13/b55c4fb/*.diff; do
  git -c core.autocrlf=false -c core.eol=lf apply --check "$p" && \
  git -c core.autocrlf=false -c core.eol=lf apply "$p"
done
```

- 必须带 `-c core.autocrlf=false -c core.eol=lf`：本机**全局** `core.autocrlf=true`；
  在非仓库目录/无该覆盖时 `git apply` 会把 LF 文件写成 CRLF，产生整文件噪声 diff
  （`src/dashboard/app.py` 是**混合行尾**文件，尤其不能整文件重写）。
- 在仓库**子目录**里直接 `git apply` 会静默 Skip（退出码 0），必须在仓库根执行。

## 2. 补丁清单（12 个）

| 补丁 | 文件 | r2 变更 |
|---|---|---|
| `src_core_windows_service.py.diff` | `src/core/windows_service.py` | **KI02 收口**：显式 `MTA_PYTHON` 指向不存在路径 → `ValueError`（不再静默忽略）；新增 `allow_fallback`，安装期拒绝 `project-venv`/`virtualenv`/`sys.executable` 三类**猜测性**来源；新增 `EXPLICIT_PYTHON_SOURCES` 与 `python_executable_is_explicit` |
| `scripts_windows_service_host.py.diff` | `scripts/windows_service_host.py` | **KI02 收口**：安装期与运行期同一入口强制 `allow_fallback=False`；解析不出显式解释器即**失败**（原来只 `print` 一条 warn） |
| `scripts_smoke_windows_service_plan.py.diff` | `scripts/smoke_windows_service_plan.py` | 新增第 [5] 节：显式无效 `MTA_PYTHON` 被拒 / 有效仍接受 / 安装期缺失失败而非回退 / 宿主脚本接线文本断言（19 → 24 项） |
| `src_core_entry_readiness.py.diff` | `src/core/entry_readiness.py` | **新增文件**：存在性触发的 `recovery_gate_allowed` 事实 → 门禁阻断原因码并入 `blocking_reasons`（P1-3 判定链接线） |
| `src_core_service.py.diff` | `src/core/service.py` | 启动改用 `RecoveryGate.before_entry()`（损坏/被删/未对账都收紧）；`bind_runtime` 绑定运行代；`_recovery_gate_facts()` 接入开仓判定链；**新增 `submit_recovery_reconcile()`（complete_reconcile 的唯一生产调用点，operator + 二次确认 + 进程身份）**；`set_control("normal")` 在门禁未收口时拒绝 |
| `src_dashboard_app.py.diff` | `src/dashboard/app.py` | **新增 `POST /api/recovery/reconcile`**（对账生产入口）；新增 `recovery_reconcile_submitted` 中文映射（否则 `smoke_ui` 变红） |
| `src_core_config_loader.py.diff` | `src/core/config_loader.py` | **P2-3 回归修复**：`runtime_path` 拒绝绝对路径逃逸（与 `RuntimePaths.path()` 同强度；修复前 `C:/Windows/Temp/x` 会被放行） |
| `src_core_instance_lock.py.diff` | `src/core/instance_lock.py` | 无变化（与上一轮一致，随包重发以保证"单一目录、单一应用顺序"） |
| `src_core_db_backup.py.diff` | `src/core/db_backup.py` | 无变化（同上） |
| `scripts_run_web.py.diff` | `scripts/run_web.py` | 无变化（同上） |
| `scripts_create_recovery_bundle.py.diff` | `scripts/create_recovery_bundle.py` | 无变化（同上） |
| `scripts_restore_recovery_bundle.py.diff` | `scripts/restore_recovery_bundle.py` | 无变化（同上） |

## 3. 未随补丁交付的已知残余（r2 如实登记）

- **P2-5 / P2-7 live 恢复门禁装错位置**：`create_recovery_bundle.py` 的
  `epoch.profile` 仍是 `""`（脚本不知道 profile），且恢复载荷是**扁平**落在
  `stage/data/`，而 live 状态根是 `<data>/live`。两者叠加 → live 恢复时门禁装在
  `<data>/ops/`，运行中的 live 服务读 `<data>/live/ops/`。**修正它需要先定义 live
  恢复的目录布局**（属 U12 OPS 的部署决策），本轮只登记、不做半套修正。
- **P3-7** `validate_windows_service_plan.py` 未接线 `python_executable_source`
  （安装**计划**校验走旧口径；真正的安装/运行入口 `windows_service_host.py` 已收口）。
- **P3-9** `src/core/ops/__init__.py`、`scripts/smoke_u13_supervision.py` 不在任务卡
  独占清单内（前者是包必需；后者触发 P1-1，登记行见交付报告）。
- **P3-1** 建议 I 在本目录 README 与本说明**二选一**应用，不要"先合分支再打补丁"
  （重复应用会静默 Skip 或被拒）。

## 4. 校验（本轮实测）

```text
git -c core.autocrlf=false -c core.eol=lf apply --check  <12 个补丁>   → 12/12 CHECK-OK
应用后与 task/U13 工作树逐字节比对                                    → 12/12 SAME
```

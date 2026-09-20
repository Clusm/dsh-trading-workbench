# U13 共享适配补丁（head = `a27fc17`）

基线：`task_base_commit = 0e522b13472cdff3757547752089f5a8d97ca8f8`（= 已审核集成 HEAD）。
本目录的 11 个 `.diff` 是该基线 → `0c0f701` 的 **完整 unified diff**，逐文件一份。

## 1. 应用方式（I 在集成树上执行）

```bash
cd <integration-worktree>          # mat-wt/I
for p in docs/dispatch/patches/U13/a27fc17/*.diff; do
  git -c core.autocrlf=false -c core.eol=lf apply --check "$p" && \
  git -c core.autocrlf=false -c core.eol=lf apply "$p"
done
```

- 必须带 `-c core.autocrlf=false -c core.eol=lf`：本机**全局** `core.autocrlf=true`，
  在非仓库目录/无该覆盖时 `git apply` 会把 LF 文件写成 CRLF，产生整文件噪声 diff。
- `core.autocrlf=false` 对 `mat-wt/I` 是既有仓库配置，正常 `git apply` 即可；
  上面的写法是**显式**声明，避免依赖本机配置。

## 2. 补丁清单（11 个文件，+700/-48 行）

| 补丁 | 文件 | 内容 | 关联 |
|---|---|---|---|
| `src_core_windows_service.py.diff` | `src/core/windows_service.py` | `resolve_python_executable`（注入/回退/来源记录，`strict_python` 可关闭回退）；`profile`（demo/live）+ 端口与 `config_loader.console_port` 对齐 + live 日志限 `data/live` + 服务名 `<base>-<Profile>`；`run_arguments()` 追加 `--profile` | **KI02**、decl-6/fu-4 |
| `scripts_smoke_windows_service_plan.py.diff` | `scripts/smoke_windows_service_plan.py` | 第 [3] 节改为两态断言（回退成功且来源被记录 / 显式注入 / strict 仍 fail-closed / `require_files=False` 语义不变 / 入口缺失仍失败） | **KI02** |
| `scripts_windows_service_host.py.diff` | `scripts/windows_service_host.py` | 运行期与安装期同一解析口径；回退时打印 warn；把 `profile` 传给统一入口 | KI02、decl-6 |
| `src_core_config_loader.py.diff` | `src/core/config_loader.py` | `Settings.state_base` / `runtime_paths` / `runtime_path` 由状态根派生；限流冷却移入 `shared_egress_root`（旧文件优先读）；`assert_environment_matches_state_root` 走 `RuntimePaths.validate` | R12、F08、fu-paths |
| `src_core_instance_lock.py.diff` | `src/core/instance_lock.py` | 可选 `metadata`（敏感键名/复杂对象丢弃）+ `describe()` + `update_metadata()`；冲突消息带持有者摘要 | diff-6、F06/F18 |
| `src_core_service.py.diff` | `src/core/service.py` | `RuntimePaths` 单一事实源；锁路径=状态根+profile；启动期 environment↔状态根拦截；运行代持久化并回填锁元数据；恢复门禁 `RECONCILE_REQUIRED` → 强制 `close_only` + critical 事件 | diff-6、diff-7、F14 |
| `src_core_db_backup.py.diff` | `src/core/db_backup.py` | epoch 边车（DB 哈希/迁移版本/当时 JSON 哈希 + 显式标注同盘运行期备份≠灾难恢复）；同秒不覆盖；文件数上界 | F05、F20、R21 |
| `scripts_create_recovery_bundle.py.diff` | `scripts/create_recovery_bundle.py` | manifest 增加 `epoch` 块与逐成员 `snapshot_at_ms`、`writer_stopped`；状态 JSON 损坏时拒绝建包 | 必达要点 4 |
| `scripts_restore_recovery_bundle.py.diff` | `scripts/restore_recovery_bundle.py` | 同 epoch 窗口校验（有 epoch 才校验，老包兼容）；回执 `entry_gate`；在目标状态根安装 `RECONCILE_REQUIRED` 门禁 | 必达要点 4/6、F14 |
| `scripts_run_web.py.diff` | `scripts/run_web.py` | `--state-root` 注入点（= `MTA_STATE_ROOT`）；启动横幅打印状态根/共享出口根/锁名 | R12、fu-paths |
| `src_dashboard_app.py.diff` | `src/dashboard/app.py` | 新增 `recovery_reconcile_required` / `run_generation_error` 中文映射（否则操作员看不到"为什么停新仓"，`smoke_ui` 也会因缺映射失败） | F14、smoke_ui |

> `src/dashboard/app.py` **不在** U13 原始集成目标清单里，但 `service.py` 新增的两个
> 事件类型必须能在控制台显示（`scripts/smoke_ui.py` 的"全部事件类型都有中文映射"
> 会因此变红），故作为附带共享适配一并交付，供 I 决定是否合并。

## 3. 自证：补丁 == 提交

在 `mat-wt/I`（pristine 基线）的**逐字节拷贝**上应用全部 11 个补丁后，与
`mat-wt/EF @ 0c0f701` 的同名文件逐字节相同：

```
ALL 11 PATCHED FILES BYTE-IDENTICAL TO EF HEAD
```

## 4. 为什么不需要改 `config/windows_service.json`

配置里 `"python_executable": ".venv/Scripts/python.exe"` 在**部署根**（存在 `.venv`）
是正确的；缺陷在于"解析这一步把相对路径当成硬校验"。修在解析层后：

- 部署根：声明路径存在 → `python_executable_source=config`，行为与升级前逐字节一致；
- 无 `.venv` 的目录（worktree / 只读发布目录 / CI 检出）：回退到受信来源并**记录来源**，
  不再让整条离线回归硬失败。

因此 `config/windows_service.json` 无需改动（也避免了两份配置漂移）。

## 5. 已知残余（显式登记，未静默忽略）

| 项 | 说明 | 归属 |
|---|---|---|
| `validate_windows_service_plan.py` | 未在本任务集成目标内，`--require-files` 不暴露 `python_executable_source`；建议 I 顺带把来源打印进 JSON | I / U10a |
| 真实双 profile SCM 实例 | 代码侧已参数化（端口/服务名/日志/profile），真正注册两个服务与宿主验收需 D01 | U12 OPS |
| `state_root` 未纳入 K01 身份哈希 | `SETTINGS_WHITELIST` / `ENVIRONMENT_OVERRIDE_WHITELIST` 在 U15 独占路径（`src/core/runtime_identity.py`），本任务不可改 | U15 / I 小版本追加 |

> 目录名 `a27fc17` = 本补丁集冻结时的**代码提交** HEAD；其后仅有一个仅含本目录的
> 文档提交，补丁内容不变。

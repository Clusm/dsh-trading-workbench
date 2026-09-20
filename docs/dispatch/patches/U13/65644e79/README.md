# U13 共享适配补丁（重生成，目标 = 集成 HEAD `65644e79`）

> **本目录取代 `../a27fc17/` 与 `../b55c4fb/`。以本次目录为准，其余作废。**
> 目录名 = 本组补丁的**目标（前像）集成 HEAD**
> `65644e79ee84db40917f82af9e3a0b9b2c1f69d4`。不是 EF 分支的 head
> （EF head 仍只承载独占产物 + 本说明；共享改动按约定只交补丁、不进分支）。

## 0. 为什么需要重生成（上一轮 11/12 失败的根因）

上一轮的两组补丁都是 **`0e522b1`（base）→ 本 head 工作树**的完整 diff。
但 `0e522b1..65644e79` 之间，这些文件已经被 **U13 自己的既有提交**
（`22e1529` / `0c0f701`，随后经 `65644e7` merge(task/U13) 进入集成树）改过。
于是集成树上的"前像"不是 `0e522b1`，而是 `0e522b1 + U13 已集成改动`，
凡是与那次改动重叠的 hunk 全部对不上 → 11/12 冲突。

实测（本轮，在集成 HEAD 沙箱里）：

```text
旧 b55c4fb/*.diff 对集成 HEAD：CHECK-OK 1/12（仅 entry_readiness，该文件集成树 == base）；FAIL 11/12
```

> 补充更正：任务书推测根因是"U15 改过 `config_loader.py` / `app.py`"。
> 实测并非如此——U15 的集成提交 `2954dce` 是 `0e522b1` 的**祖先**，
> 其改动（`authorize_control_request` / `verify_control_write` /
> `LiveWithoutProfileStateRootError` / `identity_blockers` / `model_route_ok`）
> 在 base 与集成树里**都在**，从未丢过。真正的错配来自 U13 自身的已集成改动。
> 本轮用 `git log --oneline 0e522b1..65644e79 -- <12 个文件>` 核过：
> 只有 `0c0f701`、`22e1529` 两条，**均属 U13**；
> U00/U01/U03/U04/U15 在这些文件上**没有再改一行**。

## 1. 本轮怎么生成的

1. 取集成 HEAD 版本：`git -C mat-wt/I show 65644e79:<路径>`；
2. 逐文件与 EF 工作树目标内容做 diff（7 个文件有差异）；
3. 逐文件落成 `git diff` 格式的 unified diff（`-c core.autocrlf=false -c core.eol=lf`）。

因为集成 HEAD 上这 12 个文件与本分支 head `c216af6` **逐字节相同**
（已用 blob 哈希核对），"其它任务改动 + 我的改动"在生成时就是恒等合并：
目标内容 = 集成 HEAD 内容 + 我的改动，没有任何一行被覆盖或丢弃。

## 2. 补丁清单（12 项，其中 **7 个有差异 / 5 个零差异**）

有差异的 7 个（**需要应用**，共 284 插入 / 24 删除）：

| 补丁 | 文件 | 改动 |
|---|---|---|
| `src_core_config_loader.py.diff` | `src/core/config_loader.py` | **P2-3**：`runtime_path` 拒绝绝对路径逃逸（`C:/Windows/Temp/x` 修复前被放行），与 `RuntimePaths.path()` 同强度 |
| `src_core_entry_readiness.py.diff` | `src/core/entry_readiness.py` | **存在性触发**的 `recovery_gate_allowed` 事实 → 门禁原因码并入 `blocking_reasons`（P1-3 接线） |
| `src_core_service.py.diff` | `src/core/service.py` | 启动改用 `RecoveryGate.before_entry()`（损坏/被删/未对账一律收紧，fail-closed）；`bind_runtime` 绑定运行代；`_recovery_gate_facts()` 接入开仓判定链；**新增 `submit_recovery_reconcile()`**（`complete_reconcile` 唯一生产调用点，operator + 二次确认 + 身份取自本进程）；`set_control("normal")` 在门禁未收口时拒绝 |
| `src_core_windows_service.py.diff` | `src/core/windows_service.py` | **KI02 收口**：显式 `MTA_PYTHON` 指向不存在路径 → `ValueError`（不再静默忽略）；新增 `allow_fallback`，安装期拒绝 `project-venv`/`virtualenv`/`sys.executable` 三类**猜测性**来源；新增 `EXPLICIT_PYTHON_SOURCES` / `FALLBACK_PYTHON_SOURCES` / `python_executable_is_explicit` |
| `src_dashboard_app.py.diff` | `src/dashboard/app.py` | **新增 `POST /api/recovery/reconcile`**（对账生产入口）；新增 `recovery_reconcile_submitted` 中文映射（否则 `smoke_ui` 变红） |
| `scripts_smoke_windows_service_plan.py.diff` | `scripts/smoke_windows_service_plan.py` | 新增第 [5] 节：显式无效 `MTA_PYTHON` 被拒 / 有效仍接受 / 安装期缺失失败而非回退 / 宿主脚本接线文本断言（19 → 24 项） |
| `scripts_windows_service_host.py.diff` | `scripts/windows_service_host.py` | **KI02 收口**：安装期与运行期同一入口强制 `allow_fallback=False`；解析不出显式解释器即 `RuntimeError`（原来只 `print` 一条 warn） |

零差异的 5 个（**不要再应用**——集成 HEAD 上已是最终内容，逐字节相同）：

| 文件 | 状态 |
|---|---|
| `scripts/create_recovery_bundle.py` | 零差异：U13 改动已经 `22e1529` + `65644e7` 进入集成树 |
| `scripts/restore_recovery_bundle.py` | 零差异（同上） |
| `scripts/run_web.py` | 零差异（同上） |
| `src/core/db_backup.py` | 零差异（同上） |
| `src/core/instance_lock.py` | 零差异（同上） |

> 复核命令（应全部输出 `SAME`）：
> ```bash
> for f in scripts/create_recovery_bundle.py scripts/restore_recovery_bundle.py \
>          scripts/run_web.py src/core/db_backup.py src/core/instance_lock.py; do
>   a=$(git -C mat-wt/I show 65644e79:$f | git hash-object --stdin)
>   b=$(git hash-object "$f")
>   [ "$a" = "$b" ] && echo "SAME $f" || echo "DIFF $f"
> done
> ```
> 对应 blob：`9c270c84` / `e23d8e4d` / `0ffd7139` / `a4ab32cd` / `5b460e6a`。

## 3. 应用方式（I 在集成树上执行）

```bash
cd mat-wt/I     # 仓库根；子目录里 git apply 会静默 Skip（退出码 0）
for p in docs/dispatch/patches/U13/65644e79/*.diff; do
  git -c core.autocrlf=false -c core.eol=lf apply --check "$p" || { echo "FAIL $p"; break; }
  git -c core.autocrlf=false -c core.eol=lf apply "$p"
done
```

- **必须带** `-c core.autocrlf=false -c core.eol=lf`：本机全局 `core.autocrlf=true`。
  `src/dashboard/app.py` 是**混合行尾**文件（1389 CRLF + 858 LF），
  整文件重写会产生数千行噪声 diff；本轮补丁里 49 行是 CRLF（含新增端点 41 行，
  与本地区域一致）、其余为 LF，是逐字节保真的。
- 应用顺序无关（7 个文件两两不相交），目录名排序即上表顺序。
- 不要"先合分支再打补丁"（重复应用会被拒或静默 Skip）。

## 4. 本轮实测（集成 HEAD 沙箱 `git archive 65644e79 | tar -x`）

```text
7/7 apply --check → CHECK-OK；依序实打应用 → 7/7 APPLIED
应用后 sha 比对 EF 目标内容 → 7/7 SAME（cmp 逐字节）
对真实集成树 mat-wt/I 做只读 --check → 7/7 CHECK-OK，工作树无改动
```

验收（全绿，详见交付报告）：`test_supervision` 166/0、`smoke_u13_supervision` OK、
`verify_runtime_paths` OK、`verify_recovery_plan` OK、`smoke_windows_service_plan` 24/0、
`test_runtime_identity` PASS、`test_control_boundary` PASS、`test_execution_faults` 168/0、
`test_income_reconciliation` 140/0、`smoke_entry_readiness` PASS、`smoke_release_package` 6/0、
`smoke_ui` 52/0、`smoke_contracts_v3` 173/0。

全量离线回归（**逐脚本口径**，74 个脚本）：基线失败集合 = ∅，打补丁后失败集合 = ∅，
**新增失败 0**。注：本轮 `smoke_windows_service_plan.py` 在**未打补丁的基线**上也已是
19/0（本机未复现 KI02 的环境条件），因此 KI02 本轮是"不出现"而非"被修好"。

## 5. 未随补丁交付的已知残余（如实登记）

- **P2-5 / P2-7 live 恢复门禁装错位置**：`create_recovery_bundle.py` 的
  `epoch.profile` 仍是 `""`（脚本不知道 profile），且恢复载荷**扁平**落在
  `stage/data/`，而 live 状态根是 `<data>/live`。两者叠加 → live 恢复时门禁装在
  `<data>/ops/`，运行中的 live 服务读 `<data>/live/ops/`。修正它需要先定义 live
  恢复的目录布局（属 U12 OPS 的部署决策），本轮只登记、不做半套修正。
- **P3-7** `validate_windows_service_plan.py` 未接线 `python_executable_source`
  （安装**计划**校验走旧口径；真正的安装/运行入口 `windows_service_host.py` 已收口）。
- **P3-9** `src/core/ops/__init__.py`、`scripts/smoke_u13_supervision.py` 不在任务卡
  独占清单内（前者是包必需；后者触发 P1-1，登记行见交付报告）。
- **登记行**（**I 负责改**，本目录不动 `scripts/run_offline_regression.py`）：
  在 `SCRIPTS` 元组末尾 `"scripts/smoke_u04_execution_faults.py",` 之后、
  收尾 `)` 之前插入：
  ```python
      # U13（E 监督与恢复）：恢复门禁/运行代/有界托管。全程离线，只写临时目录。
      "scripts/smoke_u13_supervision.py",
  ```
  不加这一行，`_validate_smoke_manifest()` 会以
  `RuntimeError: 未登记冒烟脚本: scripts/smoke_u13_supervision.py` **首错即停**，
  整条官方单命令入口在基线与补丁态都是 EXIT=1（已实测）。

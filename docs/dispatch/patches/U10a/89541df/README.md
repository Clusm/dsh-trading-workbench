# U10a 共享适配补丁包

- 作者线/任务：EF / U10a（可运行发布包与准确运维说明）
- 补丁生成时作者 head：`89541df`（`task/U10a`）
- 集成 base：`f5e4790`（当前集成 HEAD；已含 U00/U01/U02/U03/U04/U05/U06a/U13/U14/U15）
- 集成目标：`scripts/run_web.py`、`src/core/windows_service.py`
- 伴随登记：`scripts/run_offline_regression.py`（I 独占，登记行随补丁 03 交付）
- 上下文一致性：`89541df` 与 `f5e4790` 在这三个文件上**逐字节相同**（作者分支未改共享文件），
  因此补丁同时适用于集成 HEAD（已实测，见下）。

## 补丁清单

| # | 文件 | 变更 | 为什么不改会让本任务"不可复核" |
|---|---|---|---|
| 01 | `scripts/run_web.py` | 新增 `verify_release_package()` 与 `--verify-package [DIR]`；缺省为当前项目根 | 发布包解压后需要一个**在该包内**、只读、不加载配置、不绑定端口、不读 `.env` 的自检入口。"解压后自检通过"否则只能由打包机代跑，目标机上无法复核 |
| 02 | `src/core/windows_service.py` | 新增 `is_release_package_root()` 与 `RELEASE_PACKAGE_MANIFEST`；`resolve_python_executable` 新增 `require_in_root`；`load_windows_service_config` 新增 `require_package_interpreter`（默认按目录形态判定） | 解压目录里配置声明的相对 `.venv` 不存在时，旧实现会**静默回退**到进程的 `sys.executable`。实测：在解压目录执行 `validate_windows_service_plan.py --require-files --print-install` 会打印出**构建机**的 venv 路径作为 `sc.exe create` 的 `binPath`——"这次部署的是哪个工件"随即不可复核。发布包内必须 fail-closed |
| 03 | `scripts/run_offline_regression.py` | `SCRIPTS` 元组追加 `scripts/smoke_u10a_release_package.py` | `_validate_smoke_manifest()` 要求 `scripts/smoke_*.py` 必须登记；不登记则整条离线回归在第一个断言处中止（与 U01/U03/U04 同类，见台账 `W1-MANIFEST-TRANSIENT`） |

三个补丁**无相互依赖**，可单独应用；但 03 必须与 `scripts/smoke_u10a_release_package.py`
同批落地（只应用 03 会翻转为"清单引用不存在"）。

## 应用方式（在仓库根执行）

```bash
cd <repo-root>
for p in docs/dispatch/patches/U10a/89541df/0*.diff; do git apply -p1 "$p"; done
```

## 应用校验（实际执行）

在仓库根：三种 git 配置下 `git apply --check -p1` 全部通过。

```
=== default ===
  01_run_web_verify_package.diff                             OK
  02_core_windows_service_package_interpreter.diff           OK
  03_run_offline_regression_registration.diff                OK
=== -c core.autocrlf=false -c core.eol=lf ===
  01 / 02 / 03                                               OK
=== -c core.autocrlf=true ===
  01 / 02 / 03                                               OK
```

针对**集成 HEAD**（干净检出 `f5e4790`，`core.autocrlf=false`）：

```
$ git checkout -q f5e4790 && for p in .../0*.diff; do git apply --check -p1 "$p"; done
OK  01_run_web_verify_package.diff
OK  02_core_windows_service_package_interpreter.diff
OK  03_run_offline_regression_registration.diff
```

> 注意（给 I）：补丁 02/03 的上下文是 **LF** 行尾。若在 `core.autocrlf=true` 的仓库里
> **重新检出**整个工作区（而非在本仓直接应用），这两个文件会变成 CRLF，`git apply`
> 会失败。本仓 `core.autocrlf=false`（仓库级），故不受影响；实测在三种配置下均通过。
> 另：`scripts/run_web.py` 自身是**混合行尾**（41 行 CRLF / 75 行 LF，U13 遗留），
> 补丁 01 只在该文件的 LF 段落内加行，未触碰任何行尾。

## 沙箱 delta（独立检出 + 三补丁应用后的实测）

沙箱 = `git clone --no-hardlinks --no-checkout` 作者 head → `core.autocrlf=false` →
`git checkout` → 应用 01/02/03（**未改任何其他文件**）。

| 运行 | 结果 |
|---|---|
| 全量离线回归（补丁态） | `SMOKE MANIFEST OK included=79 excluded=1`；`OFFLINE REGRESSION OK scripts=83`；exit 0 |
| 全量离线回归（作者 head，未打补丁） | 在 `_validate_smoke_manifest()` 中止：`RuntimeError: 未登记冒烟脚本: scripts/smoke_u10a_release_package.py`（= 补丁 03 的登记项） |
| 基线（集成 HEAD `f5e4790`，作者改动之前） | `OFFLINE REGRESSION OK scripts=82`；exit 0 |

失败集合差集：**新增失败 0 项**。唯一差异是"未登记冒烟脚本"这一条清单门禁，由补丁 03
原子解决；除此之外无任何脚本由绿转红。

受两个共享文件影响的既有入口在补丁态**逐个复跑**（原始输出见作者报告）：

| 入口 | 结果 |
|---|---|
| `scripts/smoke_windows_service_plan.py` | 24 通过 / 0 失败，exit 0 |
| `scripts/smoke_windows_service_lifecycle.py` | 5 / 0，exit 0 |
| `scripts/smoke_service_lifecycle.py` | 12 / 0，exit 0 |
| `scripts/smoke_release_package.py` | 6 / 0，exit 0 |
| `scripts/smoke_u13_supervision.py` | `U13 SUPERVISION ACCEPTANCE PASS: 166 checks`，exit 0 |
| `tests/acceptance/test_supervision.py` | 166 checks，exit 0 |
| `tests/acceptance/test_runtime_identity.py` | PASS，exit 0 |
| `tests/acceptance/test_control_boundary.py` | PASS，exit 0 |
| `tests/acceptance/test_alert_delivery.py` | PASS=262 FAIL=0，exit 0 |
| `scripts/smoke_contracts_v3.py` | PASS 173 / FAIL 0，exit 0 |

## 补丁行为的三态验证（双态可运行）

`scripts/smoke_u10a_release_package.py` 第 [5] 节用**运行时行为探测**（真实调用 CLI 与
`inspect.signature`，不读源码文本、不注入内部字段）判定接线状态：

- **未应用**：`run_web.py --verify-package` 报 `unrecognized arguments`；
  `resolve_python_executable` 无 `require_in_root` 形参 → 如实打印"未接线"，其余 26 项
  断言照常通过（exit 0）。作者 head 实测：`26 通过 / 0 失败`。
- **已应用**：追加 5 项断言并通过（exit 0，总数 31）——`--verify-package` 对解压包自检退出 0；
  发布包目录缺解释器时 fail-closed；开发检出行为不变（仍回退，证明没有引入默认闸门）；
  显式 `require_package_interpreter=False` 可覆盖；包内存在配置声明的解释器时来源为
  `config`。沙箱实测（最终补丁）：`31 通过 / 0 失败`。

## 语义与消费方核对（写入共享字段前先查清）

- `--verify-package` 复用打包器的 `verify_release_package`，**不新增第二套判定**；
  退出码沿用 `report["ok"]` 的 0/2，与 `create_release_package.py` 主入口一致。
- `require_in_root` / `require_package_interpreter` 均为**新增关键字参数且默认关闭**，
  现有调用方（`scripts/validate_windows_service_plan.py`、`scripts/windows_service_host.py`、
  `scripts/smoke_windows_service_plan.py`、`tests/acceptance/*`）行为逐字节不变；
  `WindowsServiceConfig` 字段未增删改。
- `is_release_package_root` 的判据是 `package_manifest.json` 存在，与
  `scripts/create_release_package.py:PACKAGE_SCHEMA` 同一来源，不引入新的"是否发布包"口径。

## 回滚

- 补丁未合入前：删除本目录即可，作者分支不含共享文件改动。
- 已合入后：`git revert <集成提交>`，或按上表逐文件应用反向补丁
  （`git apply -R -p1 <patch>`）。三个补丁都没有数据迁移、没有配置默认值变更、
  没有风险档/白名单/live 开关改动。

## 未做与已知限制

1. `--verify-package` 位于 `scripts/run_web.py` **模块级重依赖导入之后**（uvicorn/
   langchain/pandas），因此它要求"与运行相同的解释器与依赖环境"。这与"发布包部署必须
   先安装 `requirements-lock.txt`"一致，但意味着**尚未安装依赖时不能先跑自检**；
   故手册 §3.2 第 1 步先建虚拟环境再自检。改为在导入前分支需要重构模块级导入，
   会波及 `windows_service_host.py` 等既有消费方，本轮不做。
2. 未新增"启动时强制校验发布包"的自动闸门：该路径只能靠真正启动服务来验证，而
   "启动真实服务"不在本任务权限内（无法给出离线证据）。因此自检是**操作者显式调用**
   的入口，不是启动前置。这一取舍写入手册 §3.2 与作者报告 known_limits。
3. 发布包内**没有** `.venv`（本机虚拟环境不入包），因此 `require_in_root` 在"解压后
   尚未建 venv"时会拒绝解析——这是刻意的 fail-closed，不是缺陷。
4. 本目录补丁 03 触及 `scripts/run_offline_regression.py`（I 独占）：作者**未**在分支内
   提交该文件的任何改动，登记行仅以补丁形式交付。

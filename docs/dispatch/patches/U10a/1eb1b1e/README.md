# U10a 共享适配补丁包（r2）

- 作者线/任务：EF / U10a（可运行发布包与准确运维说明）
- 补丁发布时作者 head：`1eb1b1e`（`task/U10a`）。01–03 与 r1 在 `89541df` 生成时逐字节
  相同，04 生成于 `ff98566`；三者内容都不随 head 推进而变，故本目录以最终 head 命名
- 集成 base：`8a96609`（本报告核对时的集成 HEAD）。本轮窗口内集成 HEAD 持续推进
  （`f5e4790` → `c7783d9` → `8a96609`），但 4 个补丁触及的**全部文件**在
  `f5e4790..8a96609` 之间**逐字节相同**（`git diff --stat` 为空，已核），因此补丁对
  任意含这些文件的集成 HEAD 都适用。
- 集成目标：`scripts/run_web.py`、`src/core/windows_service.py`（补丁 01/02）；
  文档卫生 3 篇（补丁 04）；伴随登记 `scripts/run_offline_regression.py`（补丁 03）
- 上下文一致性：作者分支**未改任何共享文件**（`git diff --stat f5e4790 1eb1b1e -- <共享文件>` 为空）

## 补丁清单

| # | 文件 | 变更 | 为什么不改会让本任务"不可复核" |
|---|---|---|---|
| 01 | `scripts/run_web.py` | 新增 `verify_release_package()` 与 `--verify-package [DIR]`；缺省为当前项目根 | 发布包解压后需要一个**在该包内**、只读、不加载配置、不绑定端口、不读 `.env` 的自检入口。"解压后自检通过"否则只能由打包机代跑，目标机上无法复核 |
| 02 | `src/core/windows_service.py` | 新增 `is_release_package_root()` 与 `RELEASE_PACKAGE_MANIFEST`；`resolve_python_executable` 新增 `require_in_root`；`load_windows_service_config` 新增 `require_package_interpreter`（默认按目录形态判定） | 解压目录里配置声明的相对 `.venv` 不存在时，旧实现会**静默回退**到进程的 `sys.executable`。实测：在解压目录执行 `validate_windows_service_plan.py --require-files --print-install` 会打印出**构建机**的 venv 路径作为 `sc.exe create` 的 `binPath`——"这次部署的是哪个工件"随即不可复核。发布包内必须 fail-closed |
| 03 | `scripts/run_offline_regression.py` | `SCRIPTS` 元组追加 `scripts/smoke_u10a_release_package.py` | `_validate_smoke_manifest()` 要求 `scripts/smoke_*.py` 必须登记；不登记则整条离线回归在第一个断言处中止 |
| 04 | `docs/开发规格书_v2.md`、`docs/项目说明书.md`、`docs/多Agent执行分派手册_2026-09-20.md` | 3 处**构建机绝对路径/用户名**替换为占位符（`<项目根目录>` / `<工作区根>`），纯文档、各 1 行 | 这 3 篇都在 runtime 包内；包内携带构建机身份属"部署工件卫生"缺陷（E r1 的 P3-4）。作者无这 3 篇的独占权，故以补丁交付 |

补丁 01–03 **无相互依赖**，可单独应用；但 03 必须与 `scripts/smoke_u10a_release_package.py`
同批落地（只应用 03 会翻转为"清单引用不存在"）。04 与其余补丁无交集。

## 应用方式（在仓库根执行）

```bash
cd <repo-root>
for p in docs/dispatch/patches/U10a/1eb1b1e/0*.diff; do git apply -p1 "$p"; done
```

## 应用校验（实际执行）

在仓库根：三种 git 配置下 `git apply --check -p1` 全部通过；针对**集成 HEAD `8a96609`**
亦全部通过。

```
=== default ===
  01_run_web_verify_package.diff                             OK
  02_core_windows_service_package_interpreter.diff           OK
  03_run_offline_regression_registration.diff                OK
  04_docs_placeholder_absolute_paths.diff                    OK
=== -c core.autocrlf=false -c core.eol=lf ===
  01 / 02 / 03 / 04                                          OK
=== -c core.autocrlf=true ===
  01 / 02 / 03 / 04                                          OK
```

> **行尾说明（已复测，更正 r1 的保守表述）**：`scripts/run_web.py` 自身是**混合行尾**
> （41 行 CRLF / 75 行 LF，U13 遗留），补丁 01 只在该文件的 LF 段落内加行，未触碰任何
> 行尾。补丁 04 的上下文含 CRLF 行，生成时按**字节**写出（`text=True` 的 universal-newline
> 翻译会把 CRLF 上下文改成 LF，从而在 `core.autocrlf=false` 的仓库里 apply 失败——本轮
> 踩过并已修）。
> r1 曾写"`core.autocrlf=true` 下重新检出会让 02/03 失败"，**在 git 2.53.0.windows.1 上
> 不可复现**：`core.autocrlf=true` + 重新检出后三/四个补丁 `--check` 与实际 apply 均成功
> （git 会做 CRLF 归一）。该限制属**偏保守的旧表述**，保留此更正而非删除。

## 沙箱 delta（独立检出 + 补丁应用后的实测）

沙箱 = `git archive <rev>` → `git init` → `git add -A`（`core.autocrlf=false`），
pristine = `f5e4790`（不含 U10a），patched = 作者 head `1eb1b1e` + 补丁 01–04。

| 运行 | 结果 |
|---|---|
| 全量离线回归（pristine `f5e4790`） | `OFFLINE REGRESSION OK scripts=82`；exit 0 |
| 全量离线回归（head + 补丁 01–04） | `OFFLINE REGRESSION OK scripts=83`；exit 0 |
| 失败集合差集 | **新增失败 0 项**；无脚本由绿转红 |

唯一差异是补丁 03 登记的新冒烟 `scripts/smoke_u10a_release_package.py`（本身 exit 0）。
原始输出见作者 r2 报告。

受两个共享文件影响的既有入口在补丁态逐个复跑（原始输出见作者 r1 报告，本轮未改动
01–03，故结论沿用）：`smoke_windows_service_plan` 24/0、`smoke_windows_service_lifecycle`
5/0、`smoke_service_lifecycle` 12/0、`smoke_release_package` 6/0、`smoke_u13_supervision`
166 checks、`test_supervision` 166 checks、`test_runtime_identity` PASS、
`test_control_boundary` PASS、`test_alert_delivery` PASS=262、`smoke_contracts_v3` PASS 173。

## 补丁行为的三态验证（双态可运行）

`scripts/smoke_u10a_release_package.py` 第 [5] 节用**运行时行为探测**（真实调用 CLI 与
`inspect.signature`，不读源码文本、不注入内部字段）判定接线状态：

- **未应用**：`run_web.py --verify-package` 报 `unrecognized arguments`；
  `resolve_python_executable` 无 `require_in_root` 形参 → 如实打印"未接线"，其余断言照常
  通过（作者 head 实测：`32 通过 / 0 失败`）。
- **已应用**：追加 5 项断言并通过——`--verify-package` 对解压包自检退出 0；发布包目录缺
  解释器时 fail-closed；开发检出行为不变（仍回退，证明没有引入默认闸门）；显式
  `require_package_interpreter=False` 可覆盖；包内存在配置声明的解释器时来源为 `config`。

## 语义与消费方核对（写入共享字段前先查清）

- `--verify-package` 复用打包器的 `verify_release_package`，**不新增第二套判定**；
  退出码沿用 `report["ok"]` 的 0/2，与 `create_release_package.py` 主入口一致。
- `require_in_root` / `require_package_interpreter` 均为**新增关键字参数且默认关闭**，
  现有调用方（`scripts/validate_windows_service_plan.py`、`scripts/windows_service_host.py`、
  `scripts/smoke_windows_service_plan.py`、`tests/acceptance/*`）行为逐字节不变；
  `WindowsServiceConfig` 字段未增删改。
- `is_release_package_root` 的判据是 `package_manifest.json` 存在，与
  `scripts/create_release_package.py:PACKAGE_SCHEMA` 同一来源，不引入新的"是否发布包"口径。
- 补丁 04 只改 3 行文档文本，不含任何风险参数、白名单、live 开关或接口语义变更。

## 回滚

- 补丁未合入前：删除本目录即可，作者分支不含共享文件改动。
- 已合入后：`git revert <集成提交>`，或按上表逐文件应用反向补丁
  （`git apply -R -p1 <patch>`）。四个补丁都没有数据迁移、没有配置默认值变更、
  没有风险档/白名单/live 开关改动。

## 未做与已知限制

1. `--verify-package` 位于 `scripts/run_web.py` **模块级重依赖导入之后**（uvicorn/
   langchain/pandas），因此它要求"与运行相同的解释器与依赖环境"。这与"发布包部署必须
   先安装 `requirements-lock.txt`"一致，但意味着**尚未安装依赖时不能先跑自检**；
   故手册 §3.2 第 1 步先建虚拟环境再自检。改为在导入前分支需要重构模块级导入，
   会波及 `windows_service_host.py` 等既有消费方，本轮不做。
2. 未新增"启动时强制校验发布包"的自动闸门：该路径只能靠真正启动服务来验证，而
   "启动真实服务"不在本任务权限内（无法给出离线证据）。因此自检是**操作者显式调用**
   的入口，不是启动前置。这一取舍写入手册 §3.2。
3. 发布包内**没有** `.venv`（本机虚拟环境不入包），因此 `require_in_root` 在"解压后
   尚未建 venv"时会拒绝解析——这是刻意的 fail-closed，不是缺陷。
4. 补丁 03 触及 `scripts/run_offline_regression.py`（I 独占）：作者**未**在分支内提交
   该文件的任何改动，登记行仅以补丁形式交付。
5. 补丁 04 未覆盖 `docs/无人值守升级任务清单_2026-09-19.json` 内的构建机绝对路径：
   该文件是 I 独占维护的**活台账**（每轮编排都会改动），对其做行级补丁必然冲突；
   登记为遗留项（见作者 r2 报告 known_limits）。

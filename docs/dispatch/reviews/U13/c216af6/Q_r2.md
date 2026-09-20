# U13 修复轮 r2 · Q 独立聚焦复验

- 复验对象：`c216af6`（`task/U13`；父 `bf4c0cc`；提交 `b55c4fb` / `c64435e` / `c216af6`）
- 审查者：Q（**独立会话**，与作者不同会话）
- 工作区：`mat-wt/reviews/U13/R2/r1`（detached @ `c216af6`，工作树 clean，只读）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`，`PYTHONUTF8=1`，**无 pytest**
- 补丁：`docs/dispatch/patches/U13/b55c4fb/`（12 个）；旧目录 `a27fc17/` 未使用
- 探针与原始输出：`_scratch_Q/`（`probe_gate*.py`、`probe_e2e.py`、`probe_ki02.py`、
  `probe_sample.py`、`lock_probe.py`、`driver.py` 及对应 `*.out`/`*.json`/`*.log`）
- 沙箱树：`_scratch_Q/{pristine,base,repo,after,full}`（pristine=`0e522b1` archive；
  `base`=`c216af6` archive；`after`=pristine+12 补丁；`full`=`base` 覆盖 12 补丁文件）
- 同模型偏差声明：本报告作者与编排者使用同一底层模型，**不构成独立交叉审计**。
  未读 `.env`/`data/`/密钥/账户标识；未联网；未启停服务；未下单撤单；
  未写 `mat-wt/I`、`mat-wt/EF` 或任何其它 worktree。

## 结论：**PASS-with-notes**

- **P1-2（门禁 fail-open）：真闭合。** 独立 23 例反例矩阵，无一条"应拒绝却 `allowed=True`"。
- **P1-3（门禁未接线）：真闭合。** 真实 `TradingService` 端到端 40/40 通过（含重启第二实例）。
- **KI02 的 `strict_python` 偏离：裁定可接受**（白名单确实收口了 F 指出的 A1/A2 两个面）。
- **P2-4 / P2-5 登记：P2-5 如实；P2-4 的"已修部分"与"未修部分"描述基本准确，
  但"反向未修"只写在代码注释里、未进 README §3 残余清单**（见 §4，非"把未修写成已修"）。
- **全量 delta 独立确认**：pristine 73 脚本 1 失败 → 打补丁树 73 脚本 **0 失败**；
  **新失败 0**；差集纯单向 `{smoke_windows_service_plan.py}: fail→pass`。
- **唯一未闭合的 P1 是 r1 的 P1-1**（官方离线入口 `run_offline_regression.py` 因
  `scripts/smoke_u13_supervision.py` 未登记而在第一动作即中止）。它**已如实登记**、
  **非本轮补丁引入**、修法在 **I 侧**（在 `SCRIPTS` 登记该脚本）或改名，
  且**不在本轮 7 项复验范围内** —— 故不计入本轮阻断，但**必须在 I 集成前闭合**。
  ⚠ 若把 P1-1 视为本轮在范围内，则按"残留须 ≤P2"的标尺应判 CHANGES_REQUESTED（仅此 1 项）。

新发现 4 条，**全部为 P3（无未登记 P2 fail-open、无崩溃、无未登记的新增脚本失败）**。

---

## 1. 补丁与"补丁 vs 分支"一致性（复验项 5）

独立 git repo 根（`_scratch_Q/repo`，`git init` + pristine `0e522b1`）逐补丁
`git -c core.autocrlf=false -c core.eol=lf apply --check -p1`：

```
12/12 CHECK-OK      （check_all.txt）
```

应用后（`_scratch_Q/after`）与作者分支 `c216af6` 对应文件逐字节比对：

| 结果 | 文件 | 与 README 声明 |
|---|---|---|
| SAME-AS-BRANCH ×5 | `src/core/instance_lock.py`、`src/core/db_backup.py`、`scripts/run_web.py`、`scripts/create_recovery_bundle.py`、`scripts/restore_recovery_bundle.py` | 一致（README §2 标"无变化"）✅ |
| DIFFERS ×7 | `windows_service.py`、`windows_service_host.py`、`smoke_windows_service_plan.py`、`entry_readiness.py`、`service.py`、`dashboard/app.py`、`config_loader.py` | 一致（README §2 标 r2 有变更）✅ |

**这是对 README §2 逐文件声明的独立交叉验证**：标"无变化"的 5 个补丁，其
pristine+补丁 结果确实**等于**分支版本；标"有 r2 变更"的 7 个确实**不等于**。

> **对作者"SAME×12"表述的限定**：那 7 个共享文件的 r2 改动**只存在于补丁**，
> 未提交进分支（`git diff --name-only bf4c0cc..c216af6` 不含它们）。
> 因此"SAME×12"无法对任何 commit 验证，只能对作者当时的工作树验证。
> 可验证的部分（5/5）成立；7/7 与 README 的"仅补丁交付"自述自洽。**不构成造假**。

### 1b. `a27fc17/` vs `b55c4fb/` 二选一（作者提示复核）

先打 `a27fc17` 11 个补丁、再对 `b55c4fb` 12 个做 `--check`：**11/12 报错**
（`patch does not apply`），仅新文件 `src_core_entry_readiness.py.diff` 通过
（`double_apply_check.txt`）。即**重复应用会大声失败（非零退出），不会静默 skip**。
F_r1 的 P3-1（"子目录内 `git apply` 静默 skip 退出码 0"）已由 README §1 显式文档化
（"必须在仓库根执行" + 必须带 `-c` 覆盖）。**残余风险已降为文档级**。

### 1c. `-c core.autocrlf=false -c core.eol=lf` 是否真必要（本机）

在 `_scratch_Q/repo_noov` 上**不带** `-c` 覆盖应用 `src_dashboard_app.py.diff`：
`rc=0`，产物与带覆盖的结果 **逐字节相同**（CRLF 计数均 1430，bytes 均 114965）。
即**在本机该覆盖是 no-op，无法复现作者所称的 CRLF 噪声**；但该指令无害且更显式，
不构成缺陷。另：`pristine → after` 的 `src/dashboard/app.py` 仅 **45 行**变更
（文件 114965 B），**确认无整文件噪声 diff**。

---

## 2. P1-2 门禁 fail-closed（复验项 1）→ **真闭合**

探针 `probe_gate.py`（pristine 语义，独立设计反例）+ `probe_gate2.py`（Windows 真实不可读）。

### 2a. 行为矩阵（`probe_gate_full.out`，全部 `before_entry(now_ms=NOW)`）

| 磁盘事实 | 状态 | allowed | 原因码 | 期望 | 判定 |
|---|---|---|---|---|---|
| 损坏 JSON `{corrupt` | CORRUPT | False | GATE_CORRUPT | 拒 | ✅ |
| 只删文件（戳在） | MISSING | False | GATE_MISSING | 拒 | ✅ |
| `state` 未知值 `BOGUS` | CORRUPT | False | GATE_CORRUPT | 拒 | ✅ |
| **无文件无戳（从未 restore）** | GATE_NOT_INSTALLED | **True** | GATE_NOT_INSTALLED | 放行 | ✅ 设计内 |
| CLEARED + 新鲜 | CLEARED | **True** | GATE_CLEARED | 放行 | ✅ 无误伤 |
| **只删戳留文件**（=RECONCILE_REQUIRED） | RECONCILE_REQUIRED | False | RECONCILE_REQUIRED_BEFORE_ENTRY | 拒 | ✅ |
| 戳内容 `{"installed": false}` + 删文件 | MISSING | False | GATE_MISSING | 拒 | ✅ |
| 戳内容 `null` + 删文件 | MISSING | False | GATE_MISSING | 拒 | ✅ |
| 戳内容 `false` + 删文件 | MISSING | False | GATE_MISSING | 拒 | ✅ |
| 戳内容 `{"state":"CLEARED"}` + 删文件 | MISSING | False | GATE_MISSING | 拒 | ✅ |
| 合法 JSON 但缺 `state` 字段 | CORRUPT | False | GATE_CORRUPT | 拒 | ✅ |
| 合法 JSON 但是数组 | CORRUPT | False | GATE_CORRUPT | 拒 | ✅ |
| gate 路径是**目录** | CORRUPT | False | GATE_CORRUPT | 拒 | ✅ |
| CLEARED 但 `reconciled_at_ms=null` | CLEARED | False | RECONCILE_REQUIRED_BEFORE_ENTRY | 拒 | ✅ |
| CLEARED 但时间戳过期 | CLEARED | False | EVIDENCE_STALE | 拒 | ✅ |
| 持久化字面 `"CORRUPT"` / `"MISSING"` | CORRUPT / MISSING | False | GATE_CORRUPT / GATE_MISSING | 拒 | ✅ |

### 2b. 权限/写入类（`probe_gate2_full.out`）

| 反例 | 结果 | 判定 |
|---|---|---|
| **真实不可读**（`CreateFileW` share=0 独占锁住 gate 文件） | `CORRUPT` → `GATE_CORRUPT` 拒绝 | ✅ |
| 目录不可写（gate 父路径是普通文件） | `load` → `GATE_NOT_INSTALLED`（allowed）；`save()` **抛 `FileExistsError` 大声失败** | ⚠ 见 N3 |

> 首发我把 `os.chmod(0o000)` 当"不可读"，Windows 忽略 POSIX 位 → 假阳性
> （`cleared-but-unreadable` 得 `allowed=True`）。**已改用 Win32 共享锁复测**，
> 结论为 `GATE_CORRUPT` 阻断。**该假阳性是我的探针缺陷，不是产品 fail-open。**

### 2c. 额外（本轮新测，非 F 清单）

| 反例 | 结果 |
|---|---|
| CLEARED 且 `reconciled_at_ms` 为**未来**（now+10¹²） | `allowed=True`（见 N2） |
| 持久化字面 `"GATE_NOT_INSTALLED"` | `GATE_NOT_INSTALLED` → `allowed=True`（见 N2） |

**判定：P1-2 真闭合。**"损坏/被删/未知状态 ⇒ 拒绝"三条独立复现成立；
唯一 `allowed=True` 的两种情形是**设计内的"从未恢复"**与**新鲜 CLEARED**，
未发现任何"应拒绝却放行"的路径。

---

## 3. P1-3 门禁接线端到端（复验项 2）→ **真闭合**

探针 `probe_e2e.py`：**真实 `TradingService` + 隔离 root**（`MTA_STATE_ROOT` 指向
临时目录，`project_root`/状态根全部隔离，`EXECUTION_MODE=paper`，paper broker）。
**40/40 通过**（`probe_e2e.out`）。

| 场景 | 关键断言 | 结果 |
|---|---|---|
| **A 启动被拦** | ①启动即 `close_only` ②落 `recovery_reconcile_required` **critical** 事件 ③冷启动 `_recovery_gate_facts()=False` ④`entry_readiness` 含 `RECONCILE_REQUIRED_BEFORE_ENTRY` ⑤`set_control('normal')` 返回 `ok=False` 且控制状态仍 `close_only` | ✅ ×5 |
| **A 绕过尝试** | `actor="auto"`→`OPERATOR_CONFIRM_REQUIRED`；`confirm=False`→拒；陈旧证据→`EVIDENCE_STALE`；未来证据→`EVIDENCE_STALE`；`open_orders_verified=False`→拒；`observed_at_ms=True`(bool)→`EVIDENCE_TYPE_INVALID`；空证据→`EVIDENCE_MISSING`；**请求体伪造 `account_ref/runtime_instance_id` 不被采纳**（门禁绑定仍为真实 `paper:local` + 真实运行代） | ✅ ×8 |
| **B 其他账户证据** | 门禁预绑 `OTHER-ACCOUNT` → 提交 → **`EVIDENCE_ACCOUNT_MISMATCH`** 拒绝，门禁仍 `RECONCILE_REQUIRED` | ✅ ×3 |
| **C 正常收口** | `operator`+`confirm=True`+新鲜证据 → `CLEARED` **落盘**；绑定真实 `account_ref`/`runtime_instance_id`；`set_control('normal')` 放行 | ✅ ×6 |
| **C 重启第二实例** | 新 `TradingService` 读同 root → gate=`CLEARED`、`control.state=normal`（**不再强制 close_only**）、开仓事实放行 | ✅ ×2 |
| **D 损坏门禁启动** | `close_only` + 事实 `GATE_CORRUPT` | ✅ ×2 |
| **E 从未恢复** | 不强制 `close_only`，事实放行 | ✅ ×2 |
| **F HTTP 入口** | `POST /api/recovery/reconcile` 已注册；`confirm=False`→**409 `MANUAL_REENABLE_NOT_CONFIRMED`**（门禁仍阻断）；`confirm=True`+证据→**200 CLEARED**；重复提交不 5xx | ✅ ×5 |

**"运营死路"确已解除**：`recovery_gate.json` 不再永久停在 `RECONCILE_REQUIRED` ——
`submit_recovery_reconcile()` 是 `complete_reconcile` 的唯一生产调用点，
且 `before_entry()` 已同时进入 (a) 冷启动快照 `_entry_readiness_facts`
(b) 每次 `_collect_entry_readiness_facts()`（`_recovery_gate_facts()` 存在性触发）
(c) `set_control("normal")` 前置闸门。三条路径**都由真实服务端到端复现**。

**判定：P1-3 真闭合。**

---

## 4. KI02 收口与 `strict_python` 偏离裁定（复验项 3）

探针 `probe_ki02.py`：**24/24 通过**（`probe_ki02.out`）。

### 4a. 事实

| 用例 | 结果 |
|---|---|
| A2 **显式 `MTA_PYTHON` 指向不存在路径** | **`ValueError: MTA_PYTHON 显式指定的解释器不存在`** ✅（F 的 A2 已闭合） |
| A1 全部候选缺失（默认 `allow_fallback=True`） | 仍回退 `sys.executable`（`source=interpreter`） |
| A3 `MTA_PYTHON` 指向根外**存在**文件 | 接受（`source=env`），**不受根边界约束** |
| A4/A5/A10 配置值 `../../`、根外绝对路径、`require_files=False` 短路 | 一律 `ValueError` ✅ |
| A7–A9 `strict_python=True` | 声明缺失/不存在 → `ValueError`；忽略 override ✅ |
| **B1 `allow_fallback=False` + 无显式可用** | **`ValueError`（安装期失败，不再 print warn）** ✅ |
| B2/B3 `allow_fallback=False` + `MTA_PYTHON` 有效 / 配置声明存在 | 接受（`env` / `config`）✅ |
| B5 `allow_fallback=False` + 仅 `VIRTUAL_ENV` | `ValueError` ✅ |
| C1–C4 宿主脚本 `_load_config` | 传 `allow_fallback=False` + `require_files=True`；对非显式来源 `raise RuntimeError`；`_load_config` 函数体内**已无** `print()` 的 warn-and-continue ✅ |

### 4b. 独立裁定：**偏离可接受（ACCEPT）**

判据是"白名单是否收口了 F 指出的 A1（回退 `sys.executable`）与 A2（静默忽略）两个面"：

- **A2 面：全局闭合。** 不依赖 `strict`/`allow_fallback`，任何调用方显式设了无效
  `MTA_PYTHON` 都 `ValueError`。
- **A1 面：在真正的安装/运行入口闭合。** `windows_service_host.py`
  （安装期与运行期**同一入口**）以 `allow_fallback=False` 调用，猜测性来源
  （`project-venv`/`virtualenv`/`interpreter`）**不在候选集内**，全不可用即
  `ValueError`，并有 `python_executable_is_explicit` 兜底断言 —— 从"可见的 warn"
  提升为"可控的拒绝"。`EXPLICIT_PYTHON_SOURCES={argument,env,config}` 与
  `FALLBACK_PYTHON_SOURCES` **互不相交**（B6c）。
- **未强制 `strict_python=True` 的理由成立**：F 的 A7–A9 语义（"只认配置声明"）
  会与官方推荐的 `MTA_PYTHON` 写法冲突（A9 明确"忽略 override"，
  而 `MTA_PYTHON` 优先级高于配置）。用"显式来源白名单"能在**不改变 A7–A9 语义**的
  前提下收口 A1/A2 —— 这是**更精确**的修法，不是回避。
- **残余（已登记）**：`scripts/validate_windows_service_plan.py:52` 仍以默认
  `allow_fallback=True` 调用、且不读 `python_executable_source`
  （独立复核：无 `allow_fallback=False` / `strict_python=True` /
  `python_executable_source` 任一字样）。即**安装"计划"校验器**仍走旧口径。
  这正是 README §3 登记的 **P3-7**，且真实安装/运行入口已收口。**如实，可接受。**

**裁定：该有意偏离可接受，理由与登记如实。** 唯一附带问题是 N1。

---

## 5. P2-4 / P2-5 登记是否如实（复验项 4）

| 条目 | 作者登记 | 独立核验 | 判定 |
|---|---|---|---|
| **P2-5 / P2-7 live 恢复门禁装错位置** | README §3：`epoch.profile` 仍硬编码 `""`、载荷扁平落在 `stage/data/`、live 状态根是 `<data>/live` → 门禁装在 `<data>/ops/` 而 live 服务读 `<data>/live/ops/`；"修正它需要先定义 live 恢复的目录布局（属 U12 OPS 的部署决策），本轮只登记、不做半套修正" | 代码复核：`scripts/create_recovery_bundle.py:161-164` `bundle_schema` 保持 2、`epoch.profile` 未改为可用值；`scripts/restore_recovery_bundle.py` 门禁目录仍按 `epoch_profile=="live"` 分支，而该值来自硬编码 `""` | **如实**（残余、影响面、不修理由、归属 U12 均准确）✅ |
| **P2-4 schema 兼容** | 代码注释（`recovery.py:81-82,409-413`）与验收 `[7d]`：**已修** —— `SUPPORTED_BUNDLE_SCHEMAS=(1,2,3)`，core 现可校验/演练**生产脚本的 schema-2 包**，老包显式标 `legacy_bundle`、缺 epoch 标 `None`（不假装校验过）。"反向未修"**仅**此一处隐含 | 独立复现：`SUPPORTED_BUNDLE_SCHEMAS=(1,2,3)`；`restore_recovery_bundle.py:122-123` 仍 `schema not in {1,2}` → 生产恢复脚本**仍拒绝** core 的 schema-3 包 | **部分如实**：把**已修的方向（正向）**说得准确且可验证；**"反向未修"未进 README §3 残余清单**，只在代码注释里 |

**关键结论：未发现"把未修写成已修"。** 已修的部分（core 兼容读 schema 1/2/3、
`legacy_bundle` 不假装校验）**独立复现为真**；未修的部分（脚本读 schema 3）
**确实仍未修**，且**未声称已修**。
→ 唯一登记瑕疵：P2-4 的"反向未修"应像 P2-5 那样写进 README §3 残余清单
（影响面：仅"用 core 建的 schema-3 演练包喂生产恢复脚本"会被拒；生产流程
`create → restore` 全程 schema 2，**不在生产路径上**）。**P3 级登记完整性问题。**

---

## 6. 全量回归 delta（复验项 6，强制）

### 6a. 方法（独立实现，不采信作者对照）

`_scratch_Q/driver.py`：从**每棵树自己的** `scripts/run_offline_regression.py`
用 `ast` 解析同一 `SCRIPTS`（73 项）与同一 env 变换
（`EXECUTION_MODE=paper`、pop `MTA_PROFILE`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、
`PYTHONIOENCODING=utf-8`、剔除含 KEY/SECRET/TOKEN 的 `BINANCE*`/`SENSENOVA*`），
**逐脚本独立执行**（一个失败不中止后续），记录失败集合后取差集。

### 6b. 结果（`delta.txt`）

| 树 | 运行 | 失败 | 失败项 |
|---|---|---|---|
| `pristine`（`0e522b1`） | 73 | **1** | `scripts/smoke_windows_service_plan.py`（rc=1，KI02） |
| `full`（r2 交付态） | 73 | **0** | — |

```
fixed by task   : ['scripts/smoke_windows_service_plan.py']
NEW failures    : []
persisting      : []
status changes  : [('scripts/smoke_windows_service_plan.py','fail','pass')]
```

**独立确认作者声明成立**：差集**纯单向** `fail→pass`，**本任务引入的新失败 0**。

### 6c. 关于作者"另清了 `MTA_STATE_ROOT/MTA_DATA_ROOT`"（与官方入口的差异）

**不影响结论。** 我在两种模式下运行时本机 shell **均未设置** `MTA_STATE_ROOT` /
`MTA_DATA_ROOT`（`env | grep MTA_` 无输出；`res_pristine.json`/`res_full.json`
里的 `env_MTA_STATE_ROOT=None`、`env_MTA_DATA_ROOT=None`）。因此
"清"与"不清"在本环境**完全等价**，作者的 env 差异不可能造成 delta 偏差。
（正式口径下 `run_offline_regression.py` 只 `copy` 环境、不清这两个变量；
若真实集成机设了它们，**两棵树会同样受影响**，差集仍成立。）

### 6d. ⚠ 官方单命令入口在 r2 树上**仍然中止**（= r1 的 P1-1，未修）

```
cd full && python scripts/run_offline_regression.py
→ RuntimeError: 未登记冒烟脚本: scripts/smoke_u13_supervision.py   rc=1   （official_full.out）
```

`full/scripts/smoke_*.py` 比 `pristine` 多出 `scripts/smoke_u13_supervision.py`
（62→63 个），而 `run_offline_regression.py`（I 独占、非 U13 补丁范围）
的 `_validate_smoke_manifest()` 要求全部登记 → 第一动作即抛。
故作者的"0 失败"**只在逐脚本口径成立；官方单命令口径下 0 脚本可运行**。
**该项已在 README §3（P3-9 行）与 r1/F 报告中如实登记，非本轮引入**，
修法为 **I 侧登记一行**（或把脚本改名去 `smoke_` 前缀）。**必须在 I 集成前闭合。**

---

## 7. 回归抽样（复验项 7，≥5）

`probe_sample.py`：**15/15 通过**（`probe_sample.out`）；`lock_probe.py` 独立通过。

| # | 抽样点 | 独立结果 |
|---|---|---|
| 1 | **退避 `30/60/120`** | 实测 `[30.0, 60.0, 120.0]`，前三次均 `RESTART_BACKOFF`/`RESTART_ALLOWED` ✅ |
| 2 | **窗口内第 4 次 → `MANUAL_RECOVERY` 持久化** | `RESTART_LIMIT_EXCEEDED`+`MANUAL_RECOVERY_REQUIRED`；`ManualRecovery=True` **已落盘**；**重新 load 后仍 `MANUAL_RECOVERY`**（非仅内存）✅ |
| 3 | **账本损坏 fail-closed（r2 新修）** | 损坏 JSON / 非对象 → `BLOCKED`/`LEDGER_CORRUPT`，**不回落空账本**（F_r1"损坏→空账本→额度重置"的 fail-open 已闭合）✅ |
| 4 | **人工禁用优先 + `auto` 不能解除** | 禁用 → `MANUAL_RECOVERY`/`MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY`；`actor=auto` 拒；未确认拒；`operator`+确认 可解除 ✅ |
| 5 | **`profile` 未声明 → `BLOCKED`** | `PROFILE_NOT_DECLARED` ✅ |
| 6 | **消费者清单 22 项 + 共享出口根唯一 + 换代码根不改锁** | `len(CONSUMER_INVENTORY)==22` ✅（F_r1 为 19，作者"补到 22 项"属实）；demo/live `shared_egress_root` 相同 ✅；两个不同 `code_root` → 同一 `lock_file` ✅；`../../etc/passwd` → `STATE_ROOT_ESCAPE` ✅ |
| 7 | **单实例锁双进程互斥** | `lock_probe.py`：子进程持锁时父进程 `RuntimeError: 交易服务已在运行（PID=22456，锁文件=trading_service.lock）` ✅ |
| 8 | **epoch 包 / 损坏状态守卫** | 建包+校验通过（`files=5`, `schema=3`）✅；状态 JSON 损坏 → `STATE_CORRUPT` 拒建包 ✅ |

**U13 自带套件复跑**（同一 r2 树）：
- `tests/acceptance/test_supervision.py` → `U13 SUPERVISION ACCEPTANCE PASS: 166 checks` ✅（作者称 166，r1 为 131）
- `scripts/smoke_u13_supervision.py` → `U13 SMOKE OK steps=3` ✅

**消费者清单新增项**（独立读取 `CONSUMER_INVENTORY`）：F_r1 指出的 3 个遗漏已补入，
均标 `planned` 并写明归属：
`binance_public_cache`(`src/data/binance_public.py:20`)、
`env_file_read`(`src/core/net_env.py:65`)、
`legacy_phase1_entry`(`scripts/run_phase1.py:41`)。
`dashboard_signal_state` / `health_watch_state` / `llm_coordination` /
`review_lessons_write` / `futures_source_ledger` 保持 `planned`。
→ F_r1 的 P2-9（清单不完整）**已登记补齐**；"shared_egress_root 唯一"的真实消费者
覆盖度仍以 `planned` 如实标注，**未夸大**。

---

## 8. 新发现（全部 P3；无未登记 P2 fail-open / 崩溃 / 新增脚本失败）

### N1【P3】解释器来源的两个属性**互相矛盾**（r2 白名单改造的副作用）

`src/core/windows_service.py:212-223`：

```python
@property
def python_executable_from_fallback(self) -> bool:
    return self.python_executable_source != PYTHON_SOURCE_CONFIG   # 仍只认 config
```

实测：`MTA_PYTHON` 有效时 `source="env"` →
`python_executable_is_explicit=True` **且** `python_executable_from_fallback=True`
（`probe_ki02.out` B6d/B6f）。
后者 docstring 仍写"非 `config` 即发生了回退（KI02）"，与 r2 的
"`MTA_PYTHON` 不是猜出来的回退"**直接冲突**。
**影响**：任何按 `from_fallback` 报警/拒绝的消费者（最可能的就是尚未接线的
`validate_windows_service_plan.py`，P3-7）会把**正确使用 `MTA_PYTHON` 的部署**
误报为"回退到猜测解释器"。建议 `from_fallback` 改为
`source in FALLBACK_PYTHON_SOURCES` 或直接退役该属性。

### N2【P3】`RecoveryGate.load` 接受两种"无证据即放行"的持久化形态

- 持久化 `state="GATE_NOT_INSTALLED"` 在 `KNOWN_GATE_STATES` 内 → `before_entry`
  返回 `allowed=True`（`probe_gate.out` 的 `persisted-state-*` 组）。
- `CLEARED` 且 `reconciled_at_ms` 为**未来** → `allowed=True`，且**永不过期**
  （`before_entry` 只查"过旧"，不查"来自未来"；`complete_reconcile` 查未来，
  但 `load` 不查）→ 系统时钟**回拨**会让一份 CLEARED 无限期"新鲜"。
**影响面有限**：两种都需**写权限**改 `ops/` 目录，而该权限等价于直接伪造
`{"state":"CLEARED","reconciled_at_ms":<now>}`。属纵深防御缺口（建议 `load()`
也拒绝 `NOT_INSTALLED` 持久化形态，并对未来时间戳按 `EVIDENCE_STALE` 处理），
**不是可达的绕过**。

### N3【P3】"安装戳"与被保护物**同目录**，整目录删除仍回到"从未恢复"

戳路径 = `recovery_gate.json.installed`（同目录）。只删戳→`GATE_MISSING` 阻断、
只删文件→`GATE_MISSING` 阻断，但**两者都删**（或删整个 `ops/`）→
`GATE_NOT_INSTALLED` → `allowed=True`。
这是 README/docstring **明文登记**的设计规则（"确实从未 restore"），故**不算未登记**；
但"见证物不独立于被保护物"值得在 README 写清。

**缓和因素（独立复现的新事实）**：`bind_runtime()` 在**每次**服务启动都会写盘
（`account_ref` 从空补齐 → `changed=True` → `save()`），因此**任何一次启动之后**
`ops/recovery_gate.json` + `.installed` 就已存在；此后删掉 gate 文件一定是
`GATE_MISSING`（阻断）。实测（`e2e_tmp/E_never/ops/`）：
```
recovery_gate.json            state="GATE_NOT_INSTALLED", account_ref="paper:local"
recovery_gate.json.installed  state="GATE_NOT_INSTALLED"
```
即 r2 实际上**把"删门禁"的 fail-open 面进一步收紧了**（"从未恢复"只存在于首次启动之前）。
副作用：**任何全新部署首次启动都会新建 `ops/recovery_gate.json`**（新增磁盘产物），
建议在 README 提一句。

### N4【P3 · 承接 F_r1，未在本轮 README §3 重登】

以下 r1 已登记的残余**仍存在**且**未随 r2 残余清单重述**（均不影响本轮 P1-2/P1-3 结论）：

- **`ClockVerdict` 不持久化**：`SupervisionLedger.as_dict()`（`:444-452`）**不含
  `clock`**。实测：内存中 `trusted=False, reasons=(CLOCK_ROLLBACK,)` → `save()`
  后文件无 `clock` 键 → **重新 load 后 `clock is None` → `plan_supervision`
  返回 `RESTART_BACKOFF`/`RESTART_ALLOWED`**（时钟不可信判定丢失）；只有显式传
  `clock=` 才 `BLOCKED/CLOCK_UNTRUSTED`。
- **墙钟前跳可重置重启窗口**：`now` 前跳 700s（> 600s 窗口）后
  `restarts_in_window` 3 → 0 → `RESTART_BACKOFF`。
- **账本读-改-写仍非跨进程原子**（r2 只修了"损坏→空账本"这一面）。
- `ClockGuard` / `plan_supervision` / `operator_clear_manual_recovery` 仍**无生产消费者**
  （库级保证）。
- **P3-3 已修**（附注）：`set_manual_disable` 现在**立即 `save()`**（`:481`），
  r1 的"人工禁用不落盘"已闭合。✅

---

## 9. 逐项判定汇总（复验项 1–7）

| 项 | 内容 | 判定 |
|---|---|---|
| 1 | **P1-2 门禁 fail-closed** | **CLOSED**（23 例反例；无"应拒却放行"） |
| 2 | **P1-3 门禁接线端到端** | **CLOSED**（真实 `TradingService` 40/40，含重启第二实例） |
| 3 | **KI02 收口 + `strict_python` 偏离** | **ACCEPT**（A2 全局闭合；A1 在安装/运行入口闭合；偏离理由成立） |
| 4 | **P2-4 / P2-5 登记** | **基本如实**（P2-5 准确；P2-4 未修部分未进 README §3 残余清单 = P3 完整性瑕疵；**无"未修写成已修"**） |
| 5 | **12 补丁** | **12/12 CHECK-OK**；5/5 标"无变化"的与分支逐字节 SAME；7/7 标"有变更"的确认仅存于补丁；重复应用**大声失败** |
| 6 | **全量 delta** | **单向 `{smoke_windows_service_plan.py}: fail→pass`，新失败 0**；作者"清 `MTA_STATE_ROOT`"与官方入口的差异**不影响结论**（本机两变量均未设置）。⚠ 官方单命令入口**仍中止**（P1-1，已登记） |
| 7 | **抽样 ≥5** | **15/15 + 166 checks + smoke steps=3**，8 个抽样点全绿 |

**残留项**：P3 ×4（N1–N4）；P1 ×1（P1-1，**已如实登记、非本轮引入、修法在 I 侧**）。
**未发现**：未登记的 P2 fail-open、崩溃、或未登记的既有脚本新增失败。

---

## 10. 命令清单（可复现）

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
Q="C:/Users/Tao/Desktop/币安/mat-wt/reviews/U13/R2/r1/_scratch_Q"
cd "C:/Users/Tao/Desktop/币安/mat-wt/reviews/U13/R2/r1"

# 树
mkdir -p $Q/{pristine,base,repo,after,full}
git archive 0e522b1 | tar -x -C $Q/pristine
git archive c216af6 | tar -x -C $Q/base
cp -r $Q/pristine/. $Q/repo/ && (cd $Q/repo && git init -q . && git add -A && git commit -qm base)
cp "C:/Users/Tao/Desktop/币安/mat-wt/EF/docs/dispatch/patches/U13/b55c4fb/"*.diff $Q/patches_b55/

# 12 补丁 check + 应用（仓库根）
cd $Q/repo && for f in ../patches_b55/*.diff; do \
  git -c core.autocrlf=false -c core.eol=lf apply --check -p1 "$f"; done
cp -r $Q/repo $Q/after && cd $Q/after && for f in ../patches_b55/*.diff; do \
  git -c core.autocrlf=false -c core.eol=lf apply -p1 "$f"; done
# full = base(c216af6) 覆盖 12 个补丁产物
cp -r $Q/base $Q/full && for f in <12 files>; do cp "$Q/after/$f" "$Q/full/$f"; done

# 全量 delta（逐脚本，同清单同 env）
cd $Q && PYTHONUTF8=1 "$PY" driver.py pristine res_pristine.json official
        PYTHONUTF8=1 "$PY" driver.py full     res_full.json     official
        PYTHONUTF8=1 "$PY" -c "<delta 汇总见 delta.txt>"

# 官方入口（预期中止 = P1-1）
cd $Q/full && PYTHONUTF8=1 "$PY" scripts/run_offline_regression.py

# 反例探针
cd $Q && PYTHONUTF8=1 "$PY" probe_gate.py   full > probe_gate_full.out
        PYTHONUTF8=1 "$PY" probe_gate2.py  full > probe_gate2_full.out
        PYTHONUTF8=1 "$PY" probe_ki02.py   full > probe_ki02.out
        PYTHONUTF8=1 "$PY" probe_sample.py full > probe_sample.out
        PYTHONUTF8=1 "$PY" lock_probe.py   full > probe_lock.out
        PYTHONUTF8=1 "$PY" probe_e2e.py    full "$Q/e2e_tmp" > probe_e2e.out

# 自带套件
cd $Q/full && PYTHONUTF8=1 "$PY" tests/acceptance/test_supervision.py
             PYTHONUTF8=1 "$PY" scripts/smoke_u13_supervision.py
```

---

## 11. 未覆盖 / 无法离线验证项

- **真实 SCM 双 profile 服务注册**：端口/服务名/日志落地的实际生效、
  `windows_service_host.py` 在真实 SCM 下的行为 → 需 **U12 / D01-D02**。
  本报告只做代码级与本地离线验证。
- **真实独立卷灾难恢复**：`require_independent_volume=True` 的真实第二块卷演练 →
  需 U12。本报告只验证核心建包/校验/损坏守卫。
- **真实告警演练 / 主机失联检测**：属 **U14**，本任务未涉及。
- **`smoke_ui.py` 完整 UI 断言**：本报告只确认 `dashboard/app.py` 补丁新增了
  `recovery_reconcile_required` / `run_generation_error` / `recovery_reconcile_submitted`
  三个中文映射（`smoke_ui` 的"全事件类型有中文映射"因此保持通过），未逐页核对渲染。
- **`.env` / 运行期真实取值**：受硬约束未读，故"生产 profile 实际解析到哪个状态根"
  只能做代码级断言。
- **`MTA_STATE_ROOT` 在真实部署中的落地**：R12 修复**依赖显式声明**，
  未声明时锁仍随代码根漂移（= 升级前既有行为）。需在部署手册中作为必做动作。
- **本机 `core.autocrlf` 差异**：无法复现作者所称的 CRLF 噪声（本机无差异），
  该指令的"必要性"未在本环境证实（但无害）。
- **同模型偏差**：本报告**非独立交叉审计**（见文首声明）。

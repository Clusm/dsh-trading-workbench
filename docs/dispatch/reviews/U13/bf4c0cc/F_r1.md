# U13 初审（F 角色 · 发布与可维护性）· r1

- 审查对象：`task/U13` head = `bf4c0cc`（base `0e522b1`）
- 审查者：F（独立会话，与作者不同会话）
- 工作区：`mat-wt/reviews/U13/F/r1`（detached，只读，未修改任何 tracked 文件）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`，`PYTHONUTF8=1`，无 pytest
- 探针与原始输出：`_scratch_F/`（`probe_ki02.out`、`probe_ops.out`、`probe_r3.out`、
  `probe_lock.py`、`probe_interop.py`、`drv_*.txt`、`res_*.json`、`log_*.txt`）
- 同模型偏差声明：本报告作者与编排者使用同一底层模型，**不构成独立交叉审计**。
  未读 `.env`/`data/`/密钥/账户标识；未联网；未启停服务；未写任何其它 worktree。

## 结论：**有条件 GO（CHANGES_REQUESTED 一项集成阻断 + 若干 P1）**

任务核心断言经独立复现**属实**：KI02 回退链**未削弱根边界与 strict fail-closed**；
R12 单实例锁**真实互斥**且换代码根不改锁；重启退避 `30/60/120`、窗口内第 4 次 →
`MANUAL_RECOVERY` 且**已持久化**；恢复包 epoch/坏包/损坏状态守卫**有效**；
门禁证据的类型/时效/未来/未验证**全部被拒**；人工禁用**优先于**自动恢复且 `auto` 无法解除。
**11 个补丁与作者分支逐字节一致**（无"补丁 vs 分支"漂移）。

但存在 1 项集成阻断、3 项 P1 与若干 P2/P3（详见清单），故不能无条件 GO。

---

## 一、基线构建与"补丁 vs 分支"一致性（审查重点 9）

`git apply` 在仓库子目录内**按仓库根解析路径**（本次首发即踩坑：在 `_scratch_F/patched`
内 `git apply` 全部 "Skipped patch" 且退出码 0，**静默 no-op**）。改用
`git apply -p1 --directory=_scratch_F/patched` 从仓库根应用后核验：

```
pristine(0e522b1) + 11 补丁  ==  bf4c0cc 的同名 11 个文件   逐字节 SAME  ×11
```

| 文件 | 结果 |
|---|---|
| scripts/create_recovery_bundle.py, scripts/restore_recovery_bundle.py,
  scripts/run_web.py, scripts/smoke_windows_service_plan.py, scripts/windows_service_host.py |
  SAME |
| src/core/config_loader.py, src/core/db_backup.py, src/core/instance_lock.py,
  src/core/service.py, src/core/windows_service.py, src/dashboard/app.py | SAME |

**结论：作者"把共享文件同时提交进自己分支"与"补丁"不一致的行径 —— 未造成漂移。**
`git diff --name-only 0e522b1..bf4c0cc` 恰为 11 个共享文件 + 8 个独占交付物 +
`docs/dispatch/patches/U13/**`（无其它 tracked 文件被顺带改动）。
`0c0f701`（"还原行尾去噪"）确实只影响 `dashboard/app.py`(14 行) 与 `run_web.py`(46 行)，
无整文件噪声 diff。

> **P3**：同一改动以"分支提交"与"补丁"两种形式同时交付。I 只能择一：合分支 **或**
> 在 pristine 上打补丁。若先合分支再打补丁，`git apply` 会静默 skip（见上），
> 集成方可能误判为"补丁已应用"。建议 README 显式写死集成方式二选一。

---

## 二、全量回归 delta（强制项，独立确认）

方法：从 pristine 与打补丁树各自的 `run_offline_regression.py` 读出**同一 SCRIPTS 清单**
（73 项）与同一 env（`EXECUTION_MODE=paper`、清 `MTA_PROFILE`、`MTA_NO_PROXY=1`、
`PYTHONUTF8=1`、剔除含 KEY/SECRET/TOKEN 的 BINANCE*/SENSENOVA* 变量），逐脚本执行取
失败集合差集（脚本：`_scratch_F/driver.py`）。

| 树 | 运行数 | 失败数 | 失败项 |
|---|---|---|---|
| pristine (`0e522b1`) | 73 | **1** | `scripts/smoke_windows_service_plan.py`（KI02） |
| 打补丁树 | 73 | **0** | — |

- **本任务引入的新失败：0（空集）**
- **被本任务修复的既有失败：1（KI02）**
- 差集为纯单向：`{smoke_windows_service_plan.py}` 从 fail → pass。

U13 自带脚本独立复跑：`tests/acceptance/test_supervision.py` → **131 PASS / 0 FAIL**；
`scripts/smoke_u13_supervision.py` → `U13 SMOKE OK steps=3`；
`scripts/ops/verify_runtime_paths.py` → `RUNTIME PATHS VERIFY OK`；
`scripts/ops/verify_recovery_plan.py` → `RECOVERY PLAN VERIFY OK`。

### ⚠ 但官方入口在打补丁树上**根本跑不起来**（P1，见清单 P1-1）
直接执行 `scripts/run_offline_regression.py`（打补丁树）在第一个动作即中止：

```
RuntimeError: 未登记冒烟脚本: scripts/smoke_u13_supervision.py
```

`_validate_smoke_manifest()`（`scripts/run_offline_regression.py:105-126`，I 独占）要求
所有 `scripts/smoke_*.py` 必须登记进 `SCRIPTS`。U13 新增了
`scripts/smoke_u13_supervision.py` 却**未登记、也未在交付说明中登记该风险**
（README §5"已知残余"只提了 `validate_windows_service_plan.py`）。
故：作者"补丁已应用态 0 失败"仅在**逐脚本**口径下成立；**官方单命令口径下为 0 脚本可运行**。

---

## 三、KI02 回退链安全性专项结论（**本任务额外承担**）

探针：`_scratch_F/probe_ki02.py` → `probe_ki02.out`（打补丁树，真实 import）。

回退链实测（`src/core/windows_service.py:52-115`）：

| 用例 | 结果 | 判定 |
|---|---|---|
| A1 全部候选缺失 | `source=interpreter`，路径 == `sys.executable` | ⚠ 见下 |
| A2 `MTA_PYTHON` 指向**不存在**路径 | **静默忽略**，继续回退到 `sys.executable` | ⚠ P2 |
| A3 `MTA_PYTHON` 指向**根外**攻击者可控路径 | **接受**（`source=env`，`inside_root=False`） | ⚠ 见下 |
| A4 配置值 `../../attacker/python.exe` | `ValueError: 必须位于项目根目录内` | ✅ |
| A5 配置值**绝对路径（根外）** | `ValueError` | ✅ |
| A6 配置值绝对路径（根内，合法部署） | 接受，`source=config` | ✅ |
| A7 `strict=True` + 声明不存在 | `ValueError: 不存在（strict）` | ✅ **真 fail-closed** |
| A8 `strict=True` + 声明存在 | 返回 config | ✅ |
| A9 `strict=True` + `override` | 忽略 override，只认配置 | ✅ |
| A10 `require_files=False` + 逃逸声明 | 仍 `ValueError`（边界未被 require_files 短路） | ✅ |
| A11 `VIRTUAL_ENV` 根外 | 接受（`source=virtualenv`） | 同 A3 |

**专项结论：KI02 修复是"基本安全、带有未收口的回退面"。**

1. **配置值仍受根边界校验（关键肯定项）**：`declared` 一律经 `_inside(root, ...)`
   解析，`..` 逃逸与根外绝对路径均 `ValueError`，且该校验**先于** `require_files`
   短路，`require_files=False` 也不能绕过（A4/A5/A10）。
2. **`strict_python=True` 确实 fail-closed（肯定项）**：不注册任何回退候选，
   声明缺失或不存在即 `ValueError`，且忽略注入（A7–A9）。
3. **会静默选到"错误"解释器（成立，但非完全无痕）**：默认 `strict_python=False`
   时，`.venv` 缺失的部署根会**回退到 `sys.executable`**——即"拉起安装/宿主脚本的
   那个解释器"（A1）。来源被记录为 `interpreter`，`python_executable_from_fallback`
   为真；但 `scripts/windows_service_host.py`（安装期与运行期同一入口）**只 `print` 一条
   warn，不失败、不改用 strict**（`scripts_windows_service_host.py.diff:32-43`）。
   SCM 场景下该 warn 通常只落进服务日志。→ **建议：安装期强制 `strict_python=True`
   （或至少 `require_files=True` 且 `source=="interpreter"` 时拒绝），把"回退"从
   "可见"提升为"可控"。**
4. **显式声明被静默忽略（P2）**：`MTA_PYTHON` 被显式设成一个**不存在的路径**时，
   解析层不报错而是继续回退（A2）。显式声明应当 fail-closed（"我指定的解释器
   不存在"与"我没指定"是两件事）。当前行为会让一次配错的部署静默落到 `sys.executable`。
5. **`MTA_PYTHON`/`VIRTUAL_ENV` 不受根边界约束**（A3/A11）：这是**有意且已文档化**的
   （"进程级受信来源"），在 SCM 场景需管理员权限才能设置机器级环境变量，**不构成提权**，
   但意味着"进程环境可决定服务运行时"。建议在 README 显式写"部署时必须锁定 `MTA_PYTHON`
   或使用 strict"，以免运维误以为配置即事实源。

同目录另有 P2-4（`validate_windows_service_plan.py` 未接线 `python_executable_source`），
见清单。

---

## 四、RuntimePaths（审查重点 2）

探针：`probe_ops.py`(R1–R7)、`probe_r3.py`（R2/R5/R7 修正）、`probe_r5_pristine.py`。

- **R1 `shared_egress_root` 跨 profile 唯一**：`shared_egress_root_is_unique()` → `True`；
  且**跨不同 code_root 也相同** → `True`。✅
- **R2 换代码根不改锁（R12 核心）**：同 profile + 同 `state_base` + **两个不同 `code_root`**
  → `lock_file` 相同、`lock_scope()` 相同（demo 与 live 各测一次均 `True`）。
  实测锁路径：`<state>/trading_service.lock`（demo）、`<state>/live/trading_service.lock`（live）。✅
  （注：我首发探针错用了 demo vs live 两个 profile，得到"不同锁"是**设计预期**
  ——锁按"状态根+profile"隔离；修正后结论如上。）
- **R3 未声明状态根时**：锁随代码根变化（`state_base_source=fallback`,
  `declared='data'`）→ 这正是升级前的既有行为，**R12 修复需要显式声明
  `MTA_STATE_ROOT`/`paths.state_root` 才生效**。README/模块文档已如实说明，
  但**部署侧必须显式声明才算修好**，建议在交付说明中作为"部署必做动作"置顶。
- **R7 迁移只复制不删除、可回滚**：`compatibility_migration_plan` 给出
  `copy`；`dry_run=True` → `dry-run`；`dry_run=False` → `yes`（真复制）；
  旧文件**原地保留**（可回滚）✅；重跑 → `no`（**不覆盖**）✅；复制后读取切换到规范位置 ✅。
- **R4 路径逃逸**：`RuntimePaths.path()` 对 `../../etc/passwd` 与 `C:/Windows/Temp/x.json`
  均抛 `STATE_ROOT_ESCAPE` ✅。
- **R5 ⚠ 回归：`Settings.runtime_path` 的绝对路径逃逸守卫被削弱（P2）**

  | 调用 | pristine | 打补丁树 |
  |---|---|---|
  | `profile=live`, `"C:/Windows/Temp/escape.json"` | `ValueError: 必须位于 data 内` | **`RETURNED C:\Windows\Temp\escape.json`** |
  | `profile=live`, `"data/../../../escape.json"` | `ValueError` | `ValueError`（保留） |
  | `profile=None`, `"..\..\escape.json"` | `RETURNED`（旧行为） | `ValueError`（**改善**） |

  即 `src/core/config_loader.py:78-103` 用 `if ".." in parts` 取代了旧的
  `is_relative_to(base)` 判定，**绝对路径不再被拦**。当前调用点均为字面量，
  实际可利用性低，但该函数 docstring 仍称"禁止路径逃逸"，且 `RuntimePaths.path()`
  仍严格拦截——**两个路径 API 的守卫强度已不一致**，建议把 `runtime_path` 直接
  委托 `RuntimePaths.path()`。

- **消费者清单完整性（P2）**：`CONSUMER_INVENTORY` 实为 **19 项**（`runtime_paths.py:544-581`），
  含 5 项 `planned`：`llm_coordination`、`review_lessons_write`、`futures_source_ledger`、
  `dashboard_signal_state`、`health_watch_state`。**遗漏**（对照 U00 F 指纹报告 §2 漂移清单）：
  - `src/data/binance_public.py:20` `CACHE_DIR = parents[2]/"data"/"cache"` —— **公共行情
    快照缓存**（跨 profile 共享出口性质），清单里**没有**该消费者；`cache_dir` 行的 owner
    写的是 `src/core/service.py:98`（K 线缓存），**并非同一消费者**，容易误读为已对齐。
  - `src/core/net_env.py:65`（`.env` 读取）与 `scripts/run_phase1.py`（旧入口，`settings.project_root`
    直用）均未登记。
  - `SHARED_EGRESS_CONSUMERS` 只声明 2 项，且其中 `llm_coordination` 状态是 `planned`
    （`request_coordinator.py:34` 仍按代码根派生）。即 **"shared_egress_root 唯一"目前
    只在"限流冷却"一个真实消费者上成立**；`binance_public.CACHE_DIR` 是第三个未登记的
    共享出口消费者。
- **P3 文档计数错**：`U13_FINGERPRINT_DISPOSITION["fu-paths"]` 写"CONSUMER_INVENTORY 18 项"，
  实际 19 项（`runtime_paths.py:643`）。

---

## 五、重启有界 / 时钟 / 并发（审查重点 3）

探针：`probe_ops.py`(S1–S6)、`probe_lock.py`(N)。

- **S1 退避表实测 = `30.0 / 60.0 / 120.0`**，状态 `RESTART_BACKOFF`/`RESTART_ALLOWED` ✅
- **S2 窗口内第 4 次** → `state=MANUAL_RECOVERY`、`RESTART_LIMIT_EXCEEDED` +
  `MANUAL_RECOVERY_REQUIRED` ✅；**S2b 已落盘**（`SupervisionLedger.load(path).manual_recovery is True`）✅；
  **S2c 重新加载后仍为 `MANUAL_RECOVERY`**（非仅内存）✅
- **S3 人工禁用优先且不放宽**：`manual_disable` 置位时，即使重启次数已用尽，仍返回
  `MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY`；且 `record_restart` 在禁用期**抛错拒绝** ✅
- **S4 `auto` 不能解除**：`actor="auto"` → 拒绝；`operator` + 未确认 → 拒绝；
  `operator` + 确认 → 解除 ✅（与 U15 控制面二次确认同向）
- **S5 ⚠ `ClockVerdict` 不持久化（P2）**：`SupervisionLedger.as_dict()`（`supervision.py:423-430`）
  **不含 `clock`**。实测：内存中 `trusted=False, reasons=(CLOCK_ROLLBACK,)`，
  写盘后文件里无 `clock` 键；**重新加载后 `plan_supervision` 返回 `RESTART_BACKOFF / RESTART_ALLOWED`**
  ——时钟不可信判定在重启后**丢失**。`ClockGuard` 也无任何生产消费者。
  即"时钟不可信 → BLOCKED"只在**同一次进程内且调用方显式传 `clock=`** 时成立。
- **S5c 墙钟前跳可重置重启窗口**：`now` 前跳 700s（> 窗口 600s）后
  `restarts_in_window=0` → `RESTART_ALLOWED`。若同时未传 `clock=`（见 S5），
  存在"快进时钟换取无限重启额度"的路径。`ClockGuard` 能识别该形态
  （`CLOCK_FORWARD_JUMP`，前跳 >120s 且单调钟几乎不动），但**必须由调用方提供**。
- **S6 ⚠ 账本读-改-写非原子（P2）**：两个从同一路径 `load()` 的账本各自决策，
  `max_restarts=1` 时**双方均 `allowed=True`**（丢更新）。`_atomic_write_json`
  只保证单次写原子，没有跨进程锁（不像 `InstanceLock`）。单实例锁在正常情况下
  限定了只有一个进程，但**监督者与被监督服务是不同进程**，故该窗口真实存在。
- 反向构造：更严重的是 `SupervisionLedger.load()` 对**损坏文件返回空账本**
  （`supervision.py:404-405`）→ 重启时间线被清空 → **重启额度重置**。
  与门禁同类的 fail-open（对照 P1-2）。

---

## 六、单实例锁 R12（审查重点 4）

`probe_lock.py`（真实双进程）：

- **L1 真实互斥成立**：子进程持锁期间，父进程 `acquire()` 抛
  `RuntimeError: 交易服务已在运行（PID=35560，锁文件=trading_service.lock），持有者={'profile': 'demo'}`
  —— 互斥来自 `O_EXCL` 文件创建 + PID 存活判定，**不是"仅路径相同"** ✅
  （新增持有者摘要确为可审计增益）
- **L2 陈旧锁**：`pid=999999999`（进程已死）→ 成功接管，锁内 PID 更新 ✅
- **L3 损坏锁文件** → 视为无锁并接管。因 `acquire` 直写（非临时文件+rename），
  崩溃可能留下半截文件；此时原持有者已死，接管**语义正确** ✅
- **L4 元数据脱敏有效**：`{"api_key": "SECRET123", ...}` → `api_key` 被丢弃；
  嵌套 dict 丢弃；超长字符串截断到 200 ✅（复用 U15 `is_sensitive_name`，未另立口径）

---

## 七、同 epoch 恢复包（审查重点 5）

`probe_ops.py`(G1–G6)、`probe_interop.py`。

- **G1** 建包成功，`verify_epoch_bundle` 通过（`files=5`，`span_ms=21`，`schema=5`）✅
- **G2 越界 → `EPOCH_MISMATCH`**：篡改某成员 `snapshot_at_ms=1`（并同步重算
  `manifest.sha256` 以排除哈希拦截）→ 仍被 epoch 窗口拒 ✅（**这是真的窗口校验，
  不只是哈希**）
- **G3 部分写入/缺成员 → `RECOVERY_INCOMPLETE`**（"恢复包成员缺失: state/capital_state.json"）✅
- **G3b 白名单外成员 → `RECOVERY_INCOMPLETE`** ✅
- **G4 坏包不覆盖好包**：`drill_epoch_bundle` 目标已存在 → `TARGET_EXISTS` 拒绝；
  建包目标已存在 → `BACKUP_TARGET_EXISTS`；发布用 `os.replace(stage, final)` 原子 ✅
- **G4b 恢复中断不留残骸**：`except` 分支 `shutil.rmtree(stage)` ✅
- **G5/G5b R21 同盘**：同卷时 `notices=['SAME_VOLUME_NOT_DISASTER_RECOVERY']`；
  `require_independent_volume=True` → 抛 `SAME_VOLUME_NOT_DISASTER_RECOVERY` ✅
- **G6 状态 JSON 损坏 → `STATE_CORRUPT` 拒绝建包** ✅
- **G6b 写者仍在运行 → `RECOVERY_WRITER_RUNNING`**（复用 `InstanceLock._pid_alive`）✅
- **P2-5 ⚠ 两套恢复包实现互不兼容（可维护性）**：
  `core.ops.recovery` 产出 `bundle_schema: 3` + `writer_stopped`；
  `scripts/restore_recovery_bundle.py` 只接受 `{1,2}` 且要求 `service_was_stopped`。
  实测：**用 core 建的包喂给 restore 脚本 → `ValueError: 不支持的恢复包版本`**；
  反向（脚本建的 schema-2 包）会被 `verify_epoch_bundle` 以
  `BUNDLE_SCHEMA_UNSUPPORTED` 拒。而任务卡的集成目标是**脚本那一对**，
  core 模块只被验收测试/`scripts/ops/verify_recovery_plan.py` 使用。
  结果：**"同 epoch 演练"证明的是一套实现，实际灾难恢复走的是另一套**，
  两者无法互相验证。

---

## 八、恢复后先对账再开仓（审查重点 6）

`probe_ops.py`(E1–E5)。

**证据校验不可绕过（肯定项）** —— `complete_reconcile` 逐项判定：

| 反例 | 结果 |
|---|---|
| 缺 `observed_at_ms` | `EVIDENCE_MISSING` |
| `observed_at_ms=True`（bool 混入 int） | `EVIDENCE_TYPE_INVALID` |
| `observed_at_ms=1000000.0`（float） | `EVIDENCE_TYPE_INVALID` |
| 缺 `positions_verified`/`open_orders_verified` | `EVIDENCE_MISSING` |
| `positions_verified="true"`（字符串真值） | `EVIDENCE_TYPE_INVALID` |
| `positions_verified=False` | `RECONCILE_REQUIRED_BEFORE_ENTRY` |
| 过期（now−obs > TTL 15min） | `EVIDENCE_STALE` |
| 未来（obs > now+1s） | `EVIDENCE_STALE` |
| 非 Mapping（裸 `True`） | `EVIDENCE_MISSING` |
| `untrusted_clock=True` | `CLOCK_UNTRUSTED` |

有效证据 → `GATE_CLEARED`；落盘后重新加载仍 `CLEARED`；TTL 过期后回到
`EVIDENCE_STALE`（`CLEARED` 状态 + 过期时间戳 → 拒）✅

**但有三处实质缺口：**

- **P1-2 ⚠ 门禁文件损坏/缺失 → fail-**open**（`GATE_NOT_INSTALLED`，`allowed=True`）**
  实测：`recovery_gate.json` 写入 `"{corrupt"` 或直接删除后，
  `RecoveryGate.load()` 回落默认态 `GATE_NOT_INSTALLED`，
  `before_entry()` 返回 **`allowed=True`**。
  "从未恢复过"与"门禁被损坏/被删"在实现上**不可区分**——损坏的写入（磁盘满、
  断电、篡改）会**静默解除**"恢复后先对账"约束。模块 docstring 自称 fail-closed，
  此处相反。建议：文件存在但不可解析 → `RECONCILE_REQUIRED`（或独立
  `GATE_CORRUPT` 阻断），并把"未安装"限定为"文件确实不存在且从未 restore"。
- **P1-3 ⚠ `before_entry`/`complete_reconcile` 无生产消费者：门禁在运行时收不了口**
  全仓检索：`before_entry`/`complete_reconcile` 仅出现在
  `tests/acceptance/test_supervision.py`、`scripts/ops/verify_recovery_plan.py`；
  **服务端只做一件事**（`src/core/service.py:278-290`）：启动时若
  `state == RECONCILE_REQUIRED` → `control.set_state("close_only")` + critical 事件。
  由此产生两个后果：
  1. 生产代码里**没有任何 `complete_reconcile` 调用点**，门禁状态**永远不会**从
     `RECONCILE_REQUIRED` 变成 `CLEARED`；`recovery_gate.json` 将长期停在
     `RECONCILE_REQUIRED`，于是**每次重启都会强制 `close_only`**，
     运营侧只能手工删除该文件（或绕过门禁）才能恢复开仓 —— 这与"结构化对账
     证据齐备后自动收口"的设计声明不符，是一条**运营死路**。
  2. "证据 TTL / 结构化证据 / 未来时间 / 过期"等全部精细判定**不在生产路径上**；
     上线后真正生效的只有"启动一次、置 close_only"，且该判断**只在启动时读一次**。
  → 必须在 I 合入前补齐：(a) 对账完成的**生产入口**（operator 经控制面提交
  结构化证据 → `complete_reconcile`），(b) 把 `before_entry` 接进开仓前判定链
  （`entry_readiness`/`decision_pipeline`/下单前），否则 F14 只是"测试里成立"。
- **P2-6 证据未与账户/运行代绑定**：`RecoveryGate` docstring（`recovery.py:605-614`）
  把 `account_ref`、`runtime_instance_id` 列为必需证据字段，但
  `complete_reconcile` （`:684-723`）只校验 `observed_at_ms` + 两个 verified 布尔，
  `account_ref`/`runtime_instance_id` 取自**形参**而非证据本身。
  实测：仅提交 `{"observed_at_ms":…, "positions_verified":True, "open_orders_verified":True}`
  （**无任何账户/运行代绑定**）即 `allowed=True, GATE_CLEARED`。
  → 文档与实现不一致；"这份对账属于哪个账户/哪个运行代"未强制。

- **P2-7 live 恢复的门禁装错位置（F14 对 live 失效）**：
  `scripts/restore_recovery_bundle.py:247-252` 用 `epoch_profile == "live"`
  决定门禁目录（`stage/data/live/ops/`）。但
  (a) 树内的生产者 `scripts/create_recovery_bundle.py:176-183` 把 `epoch.profile`
  **硬编码为 `""`**，restore 端永远拿不到 live；
  (b) 且恢复载荷是**扁平**落在 `stage/data/`，而 live 的状态根是 `<data>/live`。
  两者叠加 → live 恢复时门禁被装在 `<data>/ops/`，而运行中的 live 服务读
  `<data>/live/ops/recovery_gate.json` → **门禁不存在 → 开仓不被阻断**。
  （`close_only` 兜底也因此失效，因为该分支根本不会进入。）

---

## 九、人工禁用 live 优先于自动恢复（审查重点 7）

- 判定顺序（`plan_supervision`, `supervision.py:486-532`）：
  ① profile 未声明 → `BLOCKED`；② **人工禁用 → `MANUAL_RECOVERY`**；
  ③ 已人工恢复态 → `MANUAL_RECOVERY`；④ 时钟不可信 → `BLOCKED`；
  ⑤ 窗口内用尽 → `MANUAL_RECOVERY`；⑥ 否则退避。
  人工禁用**不是字面第一**（profile 检查在其前），但①是更严格的拒绝，
  因此"人工禁用永不被放宽"成立 ✅
- `auto` 无法解除（S4）✅；重启用尽**不放宽**（S3，禁用态优先返回禁用原因码）✅；
  禁用期 `record_restart` 抛错，无法用记录动作绕过 ✅
- **P3 提醒**：`set_manual_disable`（`:445-452`）**不落盘**（无 `save()`），
  而 `plan_supervision` 的 `RESTART_LIMIT_EXCEEDED` 分支却显式 `save()`。
  API 不一致，调用方若忘记 `save()` 会**丢失人工禁用**——正是最不该丢的状态。
- **P3 未接线**：`set_manual_disable` / `ManualDisable` / `operator_clear_manual_recovery`
  **无任何生产消费者**（仅测试）。"人工禁用优先"目前是库级保证。

---

## 十、指纹报告 diff-6 / decl-6 处置是否如实（审查重点 8）

| 条目 | 声明 | 独立核验 | 判定 |
|---|---|---|---|
| **diff-6** | `fixed`：运行代持久化 + 与实例锁绑定 + 换代码根不改锁 | `RunRegistry` 落 `RuntimePaths.run_registry_file`；`service.py:150-166` 铸造/回填锁元数据（`runtime_instance_id`/`generation_id`/`account_ref`/`environment`）；实测 N2/N2b 重启**同 id 同 generation**；N3 凭据纪元推进**不换 id**；锁绑定见 L1 | **基本如实** |
| **diff-6 残余** | note 称"凭据轮换不改变运行代，账本连续归集不被轮换打断" | 生产路径 `account_ref = sha256(api_key)[:16]`（key 派生）。实测 **N4：key 轮换使 `account_ref` 变化 → 铸造全新 `runtime_instance_id`**。只有无消费者的 `RunRegistry.rotate_credential()` 能保住运行代 | **P3 声明过强**（"轮换不打断归集"在生产接线不可达） |
| **decl-6** | `fixed`：显式 profile + 端口对齐 + live 日志限 `data/live` + 服务名 `<base>-<Profile>` + `run_arguments()` 追加 `--profile`；残余为真实双 SCM 实例归 U12 | 逐行核对 `src_core_windows_service.py.diff`：`ALLOWED_PROFILES`、`CONSOLE_PORT_BY_PROFILE={demo:8000,live:8001}`、端口一致性强校验、`log_path` 必须位于 `data/live`、`service_name` 加 `-{Profile}` 后缀、`run_arguments` 追加 `--profile`；`config/windows_service.json` 未改（`profile` 缺省 `None` = 升级前语义） | **如实**（残余归属也如实） |
| **fu-1/fu-4/fu-paths** | `fixed` | fu-1 见 R2；fu-4 同 decl-6；fu-paths 见第四节（**清单不完整 + "18 项"计数错 + `llm_coordination` 实为 planned**） | **部分如实**：`status:"fixed"` 对 fu-paths 偏乐观，残余项（含未登记的 `binance_public`）未全部登记 |
| **fu-2** | `registered`（归 U09/I） | 如实，未被本任务改动 | **如实** |
| 作者"131 PASS" | 已复现 | 131 PASS / 0 FAIL | **如实** |
| README §3"补丁 == 提交" | 11/11 逐字节 | 已复现（用 `--directory` 正确应用后） | **如实** |

**未发现"声称已修但实际未修"的条目。** 唯一偏乐观的是 `fu-paths` 的
`fixed` 与 diff-6 note 的"凭据轮换"表述。

---

## 十一、逐条补丁意见（11 个）

| # | 补丁 | 意见 |
|---|---|---|
| 1 | `src_core_windows_service.py.diff` | **语义正确**。KI02 修复见第三节；`profile`/端口/日志/服务名参数化正确；**未放宽**既有 `_inside`/`entrypoint`/`working_directory` 校验（仅移除 `python_executable` 一项，其边界改由 `resolve_python_executable` 承担）。**P2**：安装期未用 strict（见 KI02-3/4） |
| 2 | `scripts_smoke_windows_service_plan.py.diff` | **确有断言替换，但未被削弱**。原断言"解释器缺失时 `require-files` 失败"被**删除**，替换为"`strict` 模式下仍失败（fail-closed 未被削弱）"+ 回退成功/来源记录/显式注入/`require_files=False` 语义/入口缺失 5 条新断言。这是 KI02 的**意图性**放宽（该断言正是 KI02 的成因），且 fail-closed 属性由 strict 分支重新断言 → 可接受。**P3**：安装期宿主脚本（`windows_service_host.py`）的回退路径**没有**任何断言，建议补一条"回退时 `python_executable_from_fallback is True`" |
| 3 | `scripts_windows_service_host.py.diff` | 同一解析口径 ✅。**但见 KI02-3**：回退只 `print` warn、不失败 |
| 4 | `src_core_config_loader.py.diff` | `state_base` 派生正确；`rate_limit_state` 旧文件优先读取（升级不丢冷却）✅；`runtime_path` **守卫回归**（P2，第四节 R5）；`assert_environment_matches_state_root` 改走 `RuntimePaths.validate` → 对随附配置（`paths.data_dir: data`、无 `paths.state_root`）**与 pristine 同结论**（均因 `data` 为共享根而拒 live），**未引入新的启动阻断**；但判定源改用 `state_base_declared`（未声明恒为字面量 `"data"`）后**不再读 `paths.data_dir`**，两把旋钮解耦 → P3 建议文档化 |
| 5 | `src_core_instance_lock.py.diff` | **语义正确**。元数据脱敏有效（L4）；`update_metadata` 仅在持有锁且 pid 匹配时写 ✅；冲突消息带持有者摘要（可审计增益）✅；未改动 `O_EXCL` 互斥语义 |
| 6 | `src_core_service.py.diff` | 路径单一事实源接线正确；`assert_environment_matches_state_root` 先于交易链路、失败即拒启动（live 收紧、demo 不变）✅；运行代落盘 + 回填锁元数据 ✅。**P1-3**：恢复门禁只做"启动置 close_only"，无 `complete_reconcile` 生产入口（死路）；**P3**：`run_generation` 失败分支只记 warning（可接受，但 `RunRegistry.load_or_mint` 对**损坏**运行代文件是"静默重铸"、无事件） |
| 7 | `src_core_db_backup.py.diff` | epoch 边车 + `keep_max_files` 上界 + 同秒不覆盖 + 显式标注同盘≠灾难恢复；`state_dir=settings.runtime_path("data")` 与 `RuntimePaths.state_base` **在声明状态根时可能取到不同根**（`runtime_path("data")` → `state_base`，实为同值；已验证一致）→ 可接受 |
| 8 | `scripts_create_recovery_bundle.py.diff` | epoch 块为**追加字段**、`bundle_schema` 保持 2（向后兼容）✅；`snapshot_at_ms` 逐成员 ✅；`STATE_CORRUPT` 拒建包 ✅。**P2-5**（两套实现不兼容）；**P2-7**（`epoch.profile` 硬编码 `""` → live 门禁装错位置） |
| 9 | `scripts_restore_recovery_bundle.py.diff` | 同 epoch 逐成员校验（有 epoch 才校验，老包兼容）✅；回执 `entry_gate` ✅；**在目标状态根安装 `RECONCILE_REQUIRED`** ✅。**P2-7** 见上 |
| 10 | `scripts_run_web.py.diff` | `--state-root` 在 `load_settings` **之前**写 `os.environ["MTA_STATE_ROOT"]`（状态根一次定型，不可中途切换）✅；横幅打印状态根/共享出口根/锁名 ✅；未放宽回环地址限制 ✅ |
| 11 | `src_dashboard_app.py.diff` | 纯新增两个事件中文映射，使 `smoke_ui` 的"全部事件类型都有中文映射"保持通过 ✅（不属原集成目标，README 已说明并交 I 决定）。**P3**：若 I 不合并此补丁，`smoke_ui` 会因 `service.py` 新增的两个事件类型而变红 |

**补丁普遍未破坏既有消费端**：73 项离线回归在打补丁树上 **0 失败**（第二节），
且本任务**修复**了唯一既有失败（KI02）。

---

## 十二、问题清单

### P0
（无）

### P1
1. **P1-1 集成阻断：官方离线回归入口在打补丁树上直接中止**
   `scripts/run_offline_regression.py:131` → `RuntimeError: 未登记冒烟脚本: scripts/smoke_u13_supervision.py`。
   复现：`cd <打补丁树> && python scripts/run_offline_regression.py`（0 脚本运行）。
   交付说明未登记该项风险。**修法**：I 在 `SCRIPTS` 登记该脚本（及后续 `smoke_u13_*`），
   或作者改名为非 `smoke_` 前缀；同时 README §5 必须补登。
2. **P1-2 恢复门禁 fail-open**：`RecoveryGate.load()` 对**损坏**的 `recovery_gate.json`
   回落 `GATE_NOT_INSTALLED` → `before_entry()` = `allowed=True`（`recovery.py:626-643, 668-682`）。
   复现：`probe_ops.py` E5/E5b。**修法**：文件存在但不可解析 → 阻断（`GATE_CORRUPT`）。
3. **P1-3 门禁无生产收口路径（F14 运行时不可达）**：`complete_reconcile` 无生产调用点，
   `before_entry` 未接入开仓判定链；`recovery_gate.json` 将永久停留
   `RECONCILE_REQUIRED` → 每次重启强制 `close_only`（`service.py:278-290` + 全仓检索）。
   **修法**：补 operator 提交结构化证据的生产入口 + 在下单前链路上调用 `before_entry`。

### P2
4. **P2-1 KI02 回退可能静默选错解释器**：默认非 strict 时落到 `sys.executable`，
   安装期仅 `print` warn、不失败（`windows_service.py:52-115`、`windows_service_host.py:32-43`）。
   复现：`probe_ki02.out` A1/B1。**修法**：安装期强制 `strict_python=True`。
5. **P2-2 显式 `MTA_PYTHON` 指向不存在路径被静默忽略**（同上 A2）。应 fail-closed。
6. **P2-3 `Settings.runtime_path` 绝对路径逃逸守卫回归**（`config_loader.py:78-103`）；
   pristine `ValueError` → 现在 `RETURNED`（live）。复现：`probe_r5_pristine.py` /
   `probe_r3.py` R5。**修法**：委托 `RuntimePaths.path()`。
7. **P2-4 两套恢复包实现互不兼容**（schema 3 ↔ {1,2}），互喂即拒；
   演练证明的实现 ≠ 运维恢复的实现。复现：`probe_interop.py`。
8. **P2-5 live 恢复门禁装错位置**（`epoch.profile` 硬编码 `""` + 扁平恢复布局）
   → live 服务找不到门禁 → 开仓不被阻断。`create_recovery_bundle.py:176-183` /
   `restore_recovery_bundle.py:247-252`。
9. **P2-6 门禁证据未绑定账户/运行代**（docstring 列为必需，实现不校验）。
   复现：`probe_ops.py` E3 "no account binding" → `GATE_CLEARED`。
10. **P2-7 `ClockVerdict` 不持久化** → 重启后时钟不可信判定丢失，+ 快进时钟可重置
    重启窗口（`supervision.py:423-430`；`probe_ops.py` S5/S5c）。
11. **P2-8 重启账本读-改-写非原子**（无跨进程锁）→ 并发下额度可被绕过；
    且**损坏账本 → 空账本 → 额度重置**（`supervision.py:404-405`）。复现：S6。
12. **P2-9 消费者清单不完整**：缺 `binance_public.CACHE_DIR`（共享出口性质）、
    `net_env.py` `.env`、`run_phase1.py`；`SHARED_EGRESS_CONSUMERS` 2 项中 1 项未接线
    → "shared_egress_root 唯一"目前只覆盖 1 个真实消费者。

### P3
13. P3-1 分支与补丁双形式交付，集成方式未写死（`git apply` 会静默 skip）。
14. P3-2 `U13_FINGERPRINT_DISPOSITION["fu-paths"]` 写"18 项"，实际 19 项。
15. P3-3 `set_manual_disable` 不落盘（与 `plan_supervision` 的 save 不一致）。
16. P3-4 `set_manual_disable`/`operator_clear_manual_recovery`/`rotate_credential`/
    `ClockGuard`/`plan_supervision` 均无生产消费者（库级保证）。
17. P3-5 diff-6 note 的"凭据轮换不打断账本归集"在生产接线（key 派生 `account_ref`）
    不可达，`rotate_credential()` 无消费者。
18. P3-6 运行代文件损坏时静默重铸、无事件（`supervision.py:244-247`）。
19. P3-7 `validate_windows_service_plan.py` 未接线 `python_executable_source`
    （README §5 已如实登记，归 I/U10a）。
20. P3-8 `assert_environment_matches_state_root` 不再读 `paths.data_dir`（两把旋钮解耦）。
21. P3-9 `src/core/ops/__init__.py`、`scripts/smoke_u13_supervision.py` 不在任务卡
    独占清单内（前者为包必需，后者触发 P1-1）。

---

## 十三、未覆盖 / 无法离线验证项

- **真实宿主托管**：双 profile SCM 服务注册、端口/服务名/日志落地的实际生效、
  `windows_service_host.py` 在真实 SCM 下的行为 → 需 **D01/D02（U12 OPS）**。
- **真实告警演练 / 主机失联检测**：属 U14，本任务未涉及。
- **跨卷灾难恢复**：`require_independent_volume=True` 的**真实独立卷**演练需第二块卷；
  本报告只验证了同卷告警与拒绝路径（G5/G5b）。
- **`smoke_ui.py` 的完整 UI 断言**：本报告仅确认 `dashboard/app.py` 补丁补上了两个
  事件中文映射；未逐个核对看板渲染。
- **`.env` / 运行期真实取值**：受硬约束未读，故"生产 profile 实际解析到哪个状态根"
  无法断言（仅验证代码语义）。
- **`git_commit` / 发布包清单**：属 U15/U09，本任务未改。
- **`MTA_STATE_ROOT` 在真实部署中的落地**：R12 修复**依赖显式声明**；未声明时锁仍随
  代码根漂移（除性能差异外与升级前一致）——需在部署手册中作为必做动作。
- 同模型偏差：本报告非独立交叉审计（见文首声明）。

---

## 附：复现命令

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
cd mat-wt/reviews/U13/F/r1

# 建树（注意：必须在仓库根用 --directory，否则 git apply 静默 skip）
mkdir -p _scratch_F/pristine _scratch_F/patched
git archive 0e522b1 | tar -x -C _scratch_F/pristine
cp -r _scratch_F/pristine _scratch_F/patched
for p in docs/dispatch/patches/U13/a27fc17/*.diff; do
  git apply -p1 --directory=_scratch_F/patched "$p"; done
# 打补丁树 = patched + 8 个独占交付物（见 changed_all.txt）

# 全量 delta（逐脚本，同清单同 env）
PYTHONUTF8=1 "$PY" _scratch_F/driver.py _scratch_F/pristine _scratch_F/res_pristine.json
PYTHONUTF8=1 "$PY" _scratch_F/driver.py _scratch_F/full     _scratch_F/res_full.json

# 反例探针
PYTHONUTF8=1 "$PY" _scratch_F/probe_ki02.py        # KI02 回退链
PYTHONUTF8=1 "$PY" _scratch_F/probe_ops.py         # RuntimePaths/重启/恢复包/门禁
PYTHONUTF8=1 "$PY" _scratch_F/probe_r3.py          # R2/R5/R7 修正
PYTHONUTF8=1 "$PY" _scratch_F/probe_r5_pristine.py # pristine 逃逸守卫对比
PYTHONUTF8=1 "$PY" _scratch_F/probe_lock.py        # 双进程互斥 + 运行代
PYTHONUTF8=1 "$PY" _scratch_F/probe_interop.py     # 两套恢复包兼容性
```

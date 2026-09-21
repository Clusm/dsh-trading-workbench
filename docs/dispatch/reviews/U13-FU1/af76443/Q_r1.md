# Q 独立验收报告 — U13-FU1（R1）

- 验收对象：`task/U13-FU1` head `af76443`（base `943d839`），作者 head。
- 性质：**独立验收（Q 角色）**，只验证、只写报告；未改任何产品代码，未 push / 合并 / 打 tag / 提交。
- 工作树：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU1\R1\q`（detached HEAD @ `af76443`）
- 产物：本文件 + `_scratch_Q/`（两棵对照树、Q 自建探针、全部原始输出 `out_*.txt` / `reg_*.tsv`）
- 解释器：`C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`（`PYTHONUTF8=1`；无 pytest）
- 硬约束遵守：运行目录只读、demo 服务未动、未读/未复制 `.env`/`data/`/密钥/账户标识、未联网、未下单撤单、未发真实告警、未动风险档/白名单/live 开关。

---

## 1. 结论

**`PASS-with-notes`** —— 四条 P1 的**缺陷断言在两棵树上确实逐条翻转**；Q 自建的 12 项绕过尝试全部被挡（含"先 CLEARED 再塞未收口单必须重新阻断"）；`drill_f14_e2e` 的 4 条失败经独立裁决为**正当收紧**（门禁非死锁、非一次性）；全量离线回归 **`NEW_FAIL=∅` / `FIXED=∅`**。
**无阻塞项。**

Notes（均**不阻塞**，按重要性排序）：

| # | 事项 | 级别 |
|---|---|---|
| **N1** | `before_entry()`（**未注入 facts** 的调用形态）在"门禁所在状态根**没有账本文件**"或"账本无 `order_outbox` 表"时 **fail-open**：只要证据自带自洽的 `facts`+`facts_digest` 就放行。与 `ledger_reconcile_facts` 自称的"账本不可读时不假定干净"相悖。生产**开仓判定链**不受影响（总是注入账本事实）；受影响的是启动、`set_control` 两个未注入的调用点与所有独立巡检脚本。**未能构造出生产链上的利用。** | P2（有界） |
| **N2** | 补丁 `01` 的表述"把门禁判定挪到 `order_outbox` 读取之后、用同一批事实复验"**只覆盖了就绪链**：`src/core/service.py:314`（启动）与 `:1416`（`set_control`）仍是裸 `before_entry()`，未注入。 | P3 |
| **N3** | 作者 README 称"demo/paper 只是被检出并报告、不阻止启动"——**实测 `assert_environment_bound("demo")` 现在会抛 `RuntimePathsError`**。默认部署仍能启动（该 API 在生产零调用点；`assert_environment_matches_state_root` 对非 live 早退），但该 API 的语义已从"仅拦 live"变为"拦一切未声明状态根"。 | P3 |
| **N4** | 补丁 `05` **不必要**（§6.2 实测）；且作者对 base 红灯的归因"来自 U02 归档脚本"**实测为误**——25 条**全部**来自 U12 演练探针。 | P3（非代码） |
| **N5** | 作者已自认的两条威胁模型边界**已独立确认存在**：门禁文件 + 安装戳**同时**删除 → `GATE_NOT_INSTALLED` 放行；同时篡改账本 `order_outbox` 行不可挡。 | 边界 |

---

## 2. 四条 P1 逐条前后对照（pristine vs patched）

两棵树：`pristine` = `git archive 943d839`；`patched` = `943d839` + `af76443` 的 6 个分支内文件 + 补丁 `01`–`04`。
探针 = 集成树归档的**原始探针**逐字节还原为 `.py`。

### P1-1 core 演练还原后不装门禁 → **已修**

`drill_f14.py`：

| 观察点 | pristine | patched |
|---|---|---|
| `gate_path.exists` / 安装戳 | `False` / `False` | `True` / `True` |
| `before_entry()` | `state=GATE_NOT_INSTALLED allowed=True codes=('GATE_NOT_INSTALLED',)` | `state=RECONCILE_REQUIRED allowed=False codes=('RECONCILE_REQUIRED_BEFORE_ENTRY',)` |
| 该缺陷断言 | **PASS**（缺陷成立） | **FAIL**（缺陷已修） |
| `[B]` 两条恢复路径行为 | core 放行 vs 生产阻断 → 断言 PASS | 两边**一致地阻断** → 断言 FAIL（= 修复目标） |
| 总计 | **19 通过 / 0 失败 exit 0** | **16 通过 / 3 失败 exit 1** |

patched 的 3 条失败 = `[A] 未装门禁` + `[B] 两条路径不一致` + `[C] C1 手写 CLEARED 放行` —— **恰为缺陷断言**。

循环证据已消除（`scripts/ops/verify_recovery_plan.py` 实测 exit 0）：

```
restored layout = {'profile':'', 'state_root':'data', 'gate_file':'data/ops/recovery_gate.json',
                   'receipt_gate':'data/ops/recovery_gate.json', 'gate_installed_on_disk':True,
                   'install_marker_on_disk':True, 'state_files_in_state_root':[4 个]}
gate before recon = {'allowed': False, 'reason_codes': ['RECONCILE_REQUIRED_BEFORE_ENTRY']}
gate after recon  = {'allowed': True,  'reason_codes': ['GATE_CLEARED']}
gate after unlink = {'allowed': False, 'reason_codes': ['GATE_MISSING']}   <- 非循环性证明
```

### P1-2 live 档门禁装错位置 → **已修**

`probe_live_restore_layout.py`（逐文件比对）：

| `epoch.profile` | pristine | patched |
|---|---|---|
| `live` | 4 个状态 JSON + 库平铺 `data/`；门禁在 `data/live/ops/` | **全部**（4 JSON + 库 + 门禁 + 安装戳）在 `data/live/` 之下 |
| `""` | 全部在 `data/` 之下 | 全部在 `data/` 之下 |

Q 自建 `q_bypass.py` [E] 段（原始探针**不做** `before_entry` 与口径比对，Q 补上）：

| 检查 | demo | live |
|---|---|---|
| `restored_state_root()` 与 `RuntimePaths.state_root` **同口径** | PASS `data` | PASS `data/live` |
| 门禁落在 `RuntimePaths.recovery_gate_file` 读的位置 | PASS | PASS |
| 4 个必需 JSON + 库都在状态根 | PASS | PASS |
| **`before_entry().allowed`** | **False** | **False** |
| `receipt.recovery_gate_file` 指向真实门禁 | PASS | PASS |

**共用性（grep 真实调用点，非只看声明）**：`restored_state_root` / `restored_gate_path` 的调用点仅
`src/core/ops/recovery.py:706/723/750/751`（`drill_epoch_bundle`）、
`scripts/restore_recovery_bundle.py:230/232/251/263/300/301`（生产恢复）、
`scripts/ops/verify_recovery_plan.py:96/97`。演练与生产恢复**确实共用同一对函数**，无各写一套。

**建包侧（补丁 02）实测**：

```
profile=None  data_dir_rel='data'       epoch.profile=''
profile='live' data_dir_rel='data/live' epoch.profile='live'
```

### P1-3 证据是自证裸布尔、读取不复验 → **已修**

`drill_f14.py` [C]：`C1 forged CLEARED` pristine `allowed=True codes=('GATE_CLEARED',)` →
patched `allowed=False codes=('EVIDENCE_UNVERIFIED',)`。Q 自建完整绕过矩阵见 §3。

### P1-4 `STATE_ROOT_MISSING` 不可达 → **已修（判据改为 `state_base_source`）**

`drill_f18.py`：

| 断言 | pristine | patched |
|---|---|---|
| `legacy_a.state_base_declared / source / explicit` | `'data' / fallback / False` | 同（**返回值未改**） |
| `legacy_a.validate('demo')` | `()` | `('STATE_ROOT_MISSING',)` |
| `[缺陷] validate() 为空` | **PASS**（缺陷成立） | **FAIL**（已修） |
| `[缺陷] assert_environment_bound('demo') 不抛错` | **PASS** | **FAIL**（现在抛，见 N3） |
| 总计 | **31 通过 / 0 失败 exit 0** | **29 通过 / 2 失败 exit 1** |

- `resolve_state_base` 返回值**逐字节未改**，既有消费者（`tests/acceptance/test_supervision.py:426`
  断言 `state_base_declared == "data"`）不受影响 —— acceptance 套件全绿（§5 全量回归）。
- **无新造/重叠原因码**：全仓 `STATE_ROOT_MISSING = "STATE_ROOT_MISSING"` 只有一处
  （`runtime_paths.py:99`），`state_root_drift_reasons()` 复用它；无 `STATE_ROOT_DRIFT` 等新码。
- **新消费者确有真实接线**：
  - 补丁 04 实测：`python scripts/ops/verify_runtime_paths.py` → `exit 0`，
    `state_root_drift = code=STATE_ROOT_MISSING lock_follows_code_root=True` + `remediation=...`。
  - 补丁 03 代码核对：`scripts/run_web.py:89-92` 在 `paths` 非空时打印
    `WARNING state_root_drift ...`。**未实际启动 web 服务**（见 §7）。
- **默认部署仍能启动（实测）**：`drill_f14_e2e.py` 在 patched 树上成功构造并启动 `TradingService`
  （隔离 `project_root`、paper、**未设 `MTA_STATE_ROOT`**）→ `close_only` + 开仓判定链阻断。
  硬拦截只在 `environment=live` 生效（`config_loader.py:126-138`：非 `live` 直接 `return`）。

---

## 3. 绕过尝试结果清单（Q 自建 `_scratch_Q/q_bypass.py`，patched 树，**23 通过 / 0 失败**）

| # | 尝试 | 期望 | 实测 | 判定 |
|---|---|---|---|---|
| B1 | 手写 `{"state":"CLEARED","evidence":{}}` | 拒 | `allowed=False codes=('EVIDENCE_UNVERIFIED',)` | **挡住** |
| B2 | 手写 + **自算正确摘要**，账本仍有未收口 ENTRY | 拒 | `allowed=False codes=('RECONCILE_REQUIRED_BEFORE_ENTRY',)`；`live_facts={'submitting_orders':1,'unresolved_entry_orders':1}` | **挡住** |
| B3 | 摘要对不上 | 拒 | `allowed=False codes=('EVIDENCE_MISMATCH',)` | **挡住** |
| B4 | **先真 CLEARED，再往账本塞未收口 ENTRY** | 重新阻断 | 塞入前 `allowed=True`；塞入后 `allowed=False` | **挡住（非一次性）** |
| B5a | 只删门禁文件（安装戳在） | 拒 | `GATE_MISSING`，`allowed=False` | **挡住** |
| B5b | 门禁 + 安装戳**都删** | — | `GATE_NOT_INSTALLED`，**`allowed=True`** | 已知 P3-2 边界，**确认存在** |
| B6 | 同时篡改账本 `order_outbox` | — | 改成终态即放行 | 作者已登记，**确认边界存在**（非缺陷） |
| Q1 | 自洽摘要 + **状态根无账本文件** | — | **`allowed=True codes=('GATE_CLEARED',)`** | **fail-open（N1）** |
| Q2 | 账本在但**无 `order_outbox` 表** | — | `live_facts={...,'source':'not_applicable'}` → **`allowed=True`** | **fail-open（N1）** |
| Q3 | 未收口的是**非 ENTRY** 单（`unknown_orders>0`） | 拒 | `allowed=False` | **挡住** |
| Q4 | facts 用 bool/字符串（`False`/`"0"`） | 按 0 处理 | `allowed=True`（canonical 归一为 0） | 语义正确 |
| M1 | **只删安装戳**（门禁 `RECONCILE_REQUIRED`） | 拒 | `allowed=False codes=('RECONCILE_REQUIRED_BEFORE_ENTRY',)` | **挡住** |
| M2 | 只删安装戳（门禁 `CLEARED`） | 跟随门禁状态 | `allowed=True codes=('GATE_CLEARED',)`（删戳不新增权限） | 无变化 |
| M3 | 先删戳、再删门禁文件 | — | `GATE_NOT_INSTALLED`，`allowed=True` | = B5b 的同一路径，**确认 P3-2 边界** |

> 补充：单独删安装戳**不会**把已阻断的门禁变成放行；只有"文件 + 安装戳同时不存在"才回到
> `GATE_NOT_INSTALLED` 放行（B5b/M3）。这正是 `gate_install_marker` 设计的取舍，作者已登记。

**N1 机制**（`src/core/ops/recovery.py` `before_entry` 尾部）：

```python
live          = self.live_reconcile_facts()        # 账本文件不存在 → None
live_required = self.ledger_snapshot_present()     # 账本文件不存在 → False
if live is None and live_required:  -> EVIDENCE_UNVERIFIED   # 不触发
-> 放行
```

**为何不判 P0**：生产就绪链 `_collect_entry_readiness_facts()` → `_recovery_gate_ledger_facts()`
**总是注入**事实（账本查询异常 → `unresolved=1` → 阻断），且服务用的就是状态根下的真实账本。
Q1/Q2 需要"门禁所在状态根没有账本"这一前置，与已修复的 P1-2 布局冲突。**属"未证不可利用"，非"已证安全"。**

---

## 4. `drill_f14_e2e` 4 条失败的独立裁决：**正当收紧（非修坏）**

patched 树实测 **11 通过 / 4 失败 exit 1**（与作者自述一致）。4 条失败：
`[3] operator+确认+新鲜证据→收口`、`[3] 落盘为 CLEARED`、`[4] 快照看不到未收口订单`、`[4] RECONCILE_REQUIRED 消失`。

同源根因：探针预埋了一颗"交易所已成交、本地 SUBMITTING"的 `ENTRY-E2E-1`（**从未收口**），却在 [3] 段
直接提交"我已核对持仓与挂单"的证据并预期 `CLEARED`。P1-3 之后 `complete_reconcile` 先读账本真实事实 →
`unresolved_entry_orders=1` → 返回 `RECONCILE_REQUIRED_BEFORE_ENTRY`；第 2 条是第 1 条的下游，
第 3/4 条是同一 state 在快照里的投影。

**决定性独立判据**（Q 自建 `_scratch_Q/q_e2e_variant.py`，**6 通过 / 0 失败**）：复刻同一场景，
**先让对账真的发生**（该 ENTRY 收敛为终态），再提交**同一份**证据：

```
[A] 账本未收口时提交证据被拒        -> ok=False, RECONCILE_REQUIRED_BEFORE_ENTRY    PASS
[B] 真对账后同一份证据可以收口      -> ok=True, state=CLEARED, GATE_CLEARED         PASS
    落盘证据带可核 facts 块         -> {'source':'ledger','unknown_orders':0,...}    PASS
[B] 读取时复验通过（账本仍干净）    -> allowed=True                                  PASS
[C] 已 CLEARED 后再塞未收口 ENTRY   -> allowed=False（重新阻断）                     PASS
[C] set_control('normal') 同步被拦  -> ok=False                                      PASS
```

**裁决 = 收紧**：门禁**不是死锁**（真对账后可达 CLEARED）、**不是一次性**（再出现未收口即重新阻断），
符合 F14 必达原话"先对账再开仓"。探针 [3] 的预期是**旧语义**（把"操作者自证"当作可收口）。
附带：探针 [1] 的既有缺陷观察 `unknown_orders 硬编码 0`（P3-1）仍在，本次未修（作者未声称修）。

---

## 5. 正确的全量 delta 表

**作者方法的缺陷已独立确认**：`git archive` 导出无 `.git`，那两个脚本里的 `git ls-files` 失败 →
两边都**没真正跑起来**，"两边非零相同"是假象。

Q 用正确方法重做：两棵树**干净解包后立刻 `git init` + `git add -A -f` 提交**，且**跑完后不再 `git add`**
（避免运行期 `__pycache__` 被 `-f` 强制入库）。逐脚本跑 `run_offline_regression.py` 的 **84 个脚本**，
环境与官方入口同口径（`EXECUTION_MODE=paper`、清 `MTA_*`、清凭据、UTF-8）。

| 项 | 结果 |
|---|---|
| pristine 非零脚本 | **0 / 84** |
| patched 非零脚本 | **0 / 84** |
| **NEW_FAIL**（pristine rc=0 → patched rc≠0） | **∅** |
| **FIXED**（pristine rc≠0 → patched rc=0） | **∅** |
| 两边同非零 | **∅** |

即 `test_secret_scanner.py`、`smoke_u10a_release_package.py` **在正确的 git 树里两边都通过** ——
作者的"两边同为 2 个非零"完全是导出法造成的伪影，**不是真实 delta**。

> **踩坑记录（可复现，供后续会话避雷）**：Q 第一版做过一次 `git add -A -f` 在**跑过探针之后**，
> 于是 `__pycache__/*.pyc` 被强制入库（pristine 33 个 / patched 100 个），
> `smoke_u10a_release_package.py` 立刻误报 2 条失败（`应有 182 个` / `未归类=0`）。
> 清理索引中的 `.pyc` 并重跑后归零。→ **`-f` 只能在解包后立刻用一次。**

---

## 6. 补丁矩阵 + check_quality

### 6.1 补丁可应用性 —— 15/15 通过

`git apply --check --verbose`，3 种 git 配置 × 5 个补丁 = **全 `rc=0`，`Checking patch=1`，`Skipped=0`**：

| 补丁 | default | `core.autocrlf=false core.eol=lf` | `core.autocrlf=true` |
|---|---|---|---|
| `01_service_recovery_gate_ledger_facts` | ok | ok | ok |
| `02_create_recovery_bundle_profile` | ok | ok | ok |
| `03_run_web_state_root_drift_banner` | ok | ok | ok |
| `04_verify_runtime_paths_drift_report` | ok | ok | ok |
| `05_optional_docs_probe_lint` | ok | ok | ok |

### 6.2 check_quality —— **补丁 05 不必要（建议弃用）**

| 树 | `05` | 归档探针名 | 结果 |
|---|---|---|---|
| `basecheck`（`943d839` 原样） | 无 | `.py` | **`exit 1`**，ruff 25 条 |
| `checkq`（`patched` + 探针改名 = 集成树现状） | **未应用** | `.py.txt` | **`QUALITY CHECK OK files=291 untracked=0`，`exit 0`** |
| 6 个分支内文件 | — | — | `ruff check` → **`All checks passed!`** |

**归因核对（独立复现，作者归因错误）**：

- `ruff check docs/dispatch/reviews/U12/drill_F14_F18/raw/` → **Found 25 errors**
  （`E702×9 / E402×8 / F401×5 / E401×3`，与作者给的码分布逐字一致）
- `ruff check docs/dispatch/patches/`（U02 归档全部） → **`All checks passed!`**

结论：base 的 25 条**全部**来自我归档的 U12 演练探针，**与 U02 归档脚本无关**（作者的归因是错的）。
既然探针已改名 `.py.txt`，**不应用 `05` 时 `check_quality` 即为绿 → `05` 无必要，建议弃用**。
（`05` 所触及的"lint 是否该覆盖 `docs/**` 归档件"仍是一个需要 owner 拍板的取舍，但不是本任务的必要件。）

### 6.3 作者自带验证器的两态性（Q 独立复核）

`scripts/ops/verify_u13f1_recovery_gate.py`：pristine 树 → **44 失败 / exit 1**，且
`NOTE: missing symbols: facts_digest, ledger_reconcile_facts, restored_gate_path, restored_state_root`
（**无 ImportError / 无 Traceback**，逐条判失败）；patched 树 → **39 通过 / 0 失败 / exit 0**。
**"两态可跑"属实**，验证器内容非空转（含"删真实门禁必须翻转"的非循环断言、真实账本读取、
真对账后收口、再污染后重新阻断）。
**覆盖缺口**：该验证器**完全不触及 `src/core/service.py`**（grep 零命中）→ 补丁 01 的生产接线
**不在**作者自带验证器的覆盖范围内（只能靠 `drill_f14_e2e` 与 Q 的人工核对）。

---

## 7. 未能验证的部分

1. **补丁 03 的实际横幅打印**：`scripts/run_web.py` 的 `WARNING state_root_drift` **未实际启动 web 服务验证**
   （会占端口/碰真实配置）。只做了代码路径核对 + 实测其调用的 `state_root_drift()['code']=='STATE_ROOT_MISSING'`。
2. **N1（Q1/Q2）在生产开仓链上的可利用性**：已证明判定函数 fail-open，但**未能构造**出"服务进程内经
   `_entry_readiness_snapshot` 放行"的路径。属**未证不可利用**，不等于已证安全。
3. **live 档端到端**：`drill_f14_e2e` 只在 paper 档跑；live 档真实 `TradingService` 未起。
4. **真实交易所侧**：全程进程内假 broker / 合成状态根；真实 `query_order` 回执、真实断电/撕裂写、
   真实多账户凭据、SCM 双 profile 注册、真实告警均未涉及。
5. **`scripts/create_recovery_bundle.py --profile live` 的真实 CLI**：只验证了底层
   `create_recovery_bundle(..., profile=...)`，未跑 argparse 入口。
6. **未在 U13-FU1 范围内的旧缺陷**：`drill_f18` 的 B3 空锁接管、B4/B5 损坏锁静默放行、
   `[C]` 账户覆盖运行代基线、`probe_bundle_identity` 恢复即换运行代等（P2/P3 残留）本次**未逐条复核**。
7. **`_ACTIVE` 与 `UNRESOLVED_ORDER_STATUSES` 是两份重复字面量**（`src/exchange/order_outbox.py:29`
   vs `src/core/ops/recovery.py`）：Q 静态比对**逐一相同**，但未做运行期等价性测试。

---

## 8. 官方入口（真实工作树）—— 通过

在 Q 工作树（detached HEAD @ `af76443`，即分支 head 的真实检出）跑官方入口
`scripts/run_offline_regression.py`：

```
=== OFFLINE ...（84 个脚本，逐条打印）===
备注: 沙箱已跟踪文件 644 个，release_id=rel-3a721dac95fb28cc7db2 ...
OFFLINE REGRESSION OK scripts=84
exit code 0
```

- **`scripts=84`，exit 0** —— 与作者证据一致。
- 跑完后 `git status` 仍只有 `?? _scratch_Q/`（Q 自己的产物目录），HEAD 仍 `af76443` —— **未污染工作树**
  （`__pycache__/`、`*.pyc` 在 `.gitignore` 内，且 Q 全程未再 `git add`）。
- 注意：Q 工作树检出的**只有 6 个分支内文件**，补丁 `01`–`04` 未合入（共享文件按约定不提交进分支）。
  因此本次官方入口**未覆盖补丁 01 的生产接线**；补丁 01 的验证来自 `patched` 沙箱（§4 的 `drill_f14_e2e`
  与本报告的 Q1/Q2 分析）。

## 9. 报告与原始证据路径

- 报告：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU1\R1\q\_scratch_Q\Q_U13-FU1_r1.md`
- 对照树：`_scratch_Q/pristine`（`943d839`，624 文件）、`_scratch_Q/patched`（+6 文件 +补丁 01–04，625 文件）、
  `_scratch_Q/checkq`（+ 探针改名）、`_scratch_Q/basecheck`（`943d839` 原样）
- Q 自建探针：`_scratch_Q/q_bypass.py`、`q_e2e_variant.py`、`q_regression.py`
- 原始输出：`out_pristine_f14.txt`、`out_patched_f14.txt`、`out_pristine_f18.txt`、`out_patched_f18.txt`、
  `out_pristine_live.txt`、`out_patched_live.txt`、`out_patched_e2e.txt`、`out_q_bypass_patched.txt`、
  `out_q_e2e_patched.txt`、`out_verify_recovery_plan.txt`、`out_verify_runtime_paths.txt`、
  `out_verifier_basecheck.txt`、`out_verifier_patched.txt`、`out_create_bundle_profile.txt`、
  `out_marker_tests.txt`、`out_checkq_no05.txt`、`out_basecheck_quality.txt`、
  `reg_pristine.tsv`、`reg_patched.tsv`、`official_entry_q.txt`

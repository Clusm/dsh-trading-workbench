# U15-FU2 补丁包（目录名 `6a24fb5`）

> 目录名 = **打包时（`.diff` 生成时）的分支 head** `6a24fb5`。`.diff` 内容 =
> `85722e2..6a24fb5`（base `85722e2` = 本轮起点，已确认干净；`U15-FU1` 已在 base 里）。
> 本目录自身在本 head 之后落库，不影响 `.diff`。

关闭的登记项：**`U15-FU1` 自认的两条残留**（`85722e2` 提交信息「register K04 sentinel
residual」）—— 运行代**哨兵值**（`"unknown"`）仍能从**另外两条路径**被当成合法身份。

| 文件 | 交付方式 | 说明 |
|---|---|---|
| `src/core/ops/supervision.py` | `01_core_ops_supervision_registry_sentinel_heal.diff` | **修复 A（源头）**：`RunRegistry.load()` 把哨兵实例号与"文件损坏"同判 → 视为**无记录** → `load_or_mint` 重铸真实实例号。 |
| `src/core/runtime_identity.py` | `02_core_runtime_identity_sentinel_binding_gate.diff` | **修复 B（授权路径）**：`verify_account_binding` 识别哨兵（任一侧）→ `RUNTIME_INSTANCE_UNBOUND`；新增**唯一**原因码常量与 U15-FU2 两态探测标记。 |
| `src/core/management_authorization.py` | `03_core_management_authorization_gate_reuses_resolver.diff` | **修复 B（K04 门禁）**：`resolve_account_domain` 的门禁与 `issue_management_authorization` 的签发门禁改用同一个 `resolve_runtime_instance_id`，不再用「串非空即绑定」。 |
| `tests/acceptance/test_runtime_identity.py` | `04_acceptance_runtime_identity_u15_fu2.diff` | U15 新增 3 节：授权路径口径 / registry 源头 / **端到端（污染 registry → 授权结论）**。两态可跑。 |

改动量：`4 files changed, 338 insertions(+), 7 deletions(-)`。

---

## 0. 缺陷（两条路径，均 fail-open）

`U15-FU1` 让**发布清单**不再携带占位符，但它只保证哨兵**会被记成 `RUNTIME_GENERATION_UNPROVEN`**，
没有阻止哨兵**产生**，也没有让**授权路径**认识它。

**残留 A（源头）**：`RunRegistry.load()`（`src/core/ops/supervision.py`）把
`runtime_generation.json` 里的 `runtime_instance_id` 原样 `str()` 读入，只 reject
**形状错误**（`KeyError/TypeError/ValueError`）。文件里写着 `"unknown"` → 读出一个
**实例号为 `"unknown"` 的合法运行代**；`load_or_mint` 见 `existing.runtime_instance_id`
非空即**继承**它（连 `restarts` 一起），于是"一次重启"被误判成"同一代"。

**残留 B（授权路径）**：两处判据都只问"串非空"：

* `management_authorization.py:177`（修前）：`if not str(generation.runtime_instance_id or "").strip(): ... RUNTIME_INSTANCE_UNBOUND` —— `"unknown"` 非空，**直接过**；
* `runtime_identity.py verify_account_binding`：关键比较是 `actual.generation_id != expected.generation_id`，而 `generation_id` 的 seed **包含实例号** —— 被污染的 registry 铸出的哨兵运行代，与授权记录里绑定的那个哨兵运行代**逐字相等**，于是 `allowed=True`、`reason_codes=()`。

结论：**被污染的 registry 可让一份"绑定到某个运行代"的授权，实际绑定在一个占位符上**
——授权本想提供的"已证明的运行代"保证落空（与 U09-FU1 同族的"读数即值"缺陷）。

---

## 1. 前后对照（实测，命令 + 输出 + exit code）

探针：`evidence/probe_e2e.py.txt`（`argv[1]` = 被测树根，**一份脚本跑两态**，
`E2E` 段即"从被污染的 registry 到授权结论"）。

```
PYTHONUTF8=1 <venv>/python.exe evidence/probe_e2e.py <tree_root>
```

| 命令 | exit code |
|---|---|
| `probe_e2e.py <pristine 85722e2 树>` → `evidence/probe_e2e_base.log` | `0` |
| `probe_e2e.py <本 head 树>` → `evidence/probe_e2e_patch.log` | `0` |

### 1.1 残留 A（源头）

fixture：`runtime_generation.json` 里 `runtime_instance_id="unknown"`、`restarts=3`、
`generation_id` 由**真实铸造**算出（即"文件自洽、只有实例号是哨兵"）。

| 观测点 | 修前（base `85722e2`） | 修后（本 head） |
|---|---|---|
| `RunRegistry.load()` | `RunRecord(instance='unknown', restarts=3)` ← **哨兵被读成合法运行代** | `None`（**视为无记录**） |
| `load_or_mint(env, acct)` | `instance='unknown' created=False restarts=3` ← **哨兵被当成「同一代」继承** | `instance='355b1d7d4cee46e28167da889e2258b6' created=True restarts=0` ← **重铸出真实实例号** |
| 哨兵是否已消除 | `False` | `True` |

方向说明：这里"重铸"是**恢复正确行为**（拿回真值），不是放宽 —— 修后 `restarts` 归零
是因为旧记录整体不可信（与"文件损坏即视为无记录"**同一既有口径**），且**服务仍照常启动**
（`load()` 不抛错，只返回 `None`）。

### 1.2 残留 B（授权路径）

| 观测点 | 修前 | 修后 |
|---|---|---|
| `verify_account_binding(哨兵, 哨兵)` | `allowed=True reasons=()` | `allowed=False reasons=('RUNTIME_INSTANCE_UNBOUND',)` |
| `resolve_account_domain(哨兵)` | `bound=True reasons=()` | `bound=False reasons=('RUNTIME_INSTANCE_UNBOUND',)` |
| `issue_management_authorization(哨兵运行代)` | **成功**，`runtime_instance_id='unknown'` | **拒绝** `('RUNTIME_INSTANCE_UNBOUND',)` |

### 1.3 端到端（被污染的 registry → 授权结论）

同一条链路：registry 文件（实例号 = 哨兵）→ 哨兵运行代 → 管理授权 → 授权结论。

| 观测点 | 修前 | 修后 |
|---|---|---|
| `evaluate_management_request(...)` | `submit_allowed=True`、`binding_reason_codes=()`、`reasons=()` ← **授权实际绑在占位符上，却判"提交许可"** | `submit_allowed=False`、`binding_reason_codes=('RUNTIME_INSTANCE_UNBOUND',)` |
| `reconcile_allowed` | `True` | `True`（**既有边界未变**：按原 id 查单/收口不被身份绑定阻断） |

修后那一条之所以仍能走到结论，是因为**签发侧已经先把哨兵挡住了**；探针按"修前已签发的
在途授权"形态重放（`model_copy(update={"runtime_instance_id": "unknown"})`），验证**消费侧
也拦得住** —— 即"哨兵从别的路径进来"这条要求。

---

## 2. 两处修复的论证

### 修复 A：`RunRegistry.load()` —— 与"文件损坏"同口径

```python
        if is_placeholder_runtime_instance_id(record.runtime_instance_id):
            self.record = None
            return None
```

* **为什么是"视为无记录"而不是抛错**：既有口径已经把"运行代文件损坏"定义为
  "视为不存在（重新铸造），但绝不静默当作同代"（原注释逐字）。哨兵实例号正是
  "读起来有值、实际不是一个运行代"，与损坏**同类**；复用同一出口，不新增启动期拒绝 →
  满足"不得把缺运行代变成服务起不来"。
* **为什么不是放宽**：修前该分支把哨兵**保留**下来；修后把它**丢掉并重铸**。判定集合
  只增不减（哨兵从"合法继承"变成"不可继承"）。
* `load_or_mint` 里额外把**调用方显式传入**的 `runtime_instance_id` 也过同一口径
  （`resolve_runtime_instance_id(...) or uuid4().hex`）—— 否则"传入哨兵"会绕过 `load()`。

### 修复 B：`verify_account_binding` + K04 门禁 —— 同一口径，一个函数

```python
# runtime_identity.verify_account_binding
    if (is_placeholder_runtime_instance_id(expected.runtime_instance_id)
            or is_placeholder_runtime_instance_id(actual.runtime_instance_id)):
        reasons.append(RUNTIME_INSTANCE_UNBOUND)

# management_authorization.resolve_account_domain / issue_management_authorization
    if resolve_runtime_instance_id(generation.runtime_instance_id) is None:
        reasons.append(RUNTIME_INSTANCE_UNBOUND)
```

* **两侧都查**（不只查 `actual`）：缺陷的触发条件是"两侧都带同一个哨兵 → 逐字相等"，
  只查一侧会漏掉"expected 带哨兵、actual 是真值且恰好相等不可能"之外的组合；
  两侧都查是 fail-closed 且不误杀真值（`unknownverse` 不是哨兵）。
* **`reconcile_allowed` 不受影响**：`evaluate_management_request` 只把绑定原因并入
  `binding_reason_codes` / `submit_allowed`，查单收口那条既有边界**逐字未动**（实测
  `reconcile_allowed=True` 两态一致）。
* **签发侧同口径**：不把哨兵写进授权（否则等于把"未绑定"盖成"看起来绑定好了"，
  留待运行期发现）——修后 `issue_management_authorization` 对哨兵运行代**拒绝签发**。

---

## 3. 复用 U15-FU1 的哨兵口径（**未造第二份名单**）

复用清单（`src/core/runtime_identity.py`，U15-FU1 交付）：

| 复用物 | 谁用 |
|---|---|
| `RUNTIME_INSTANCE_PLACEHOLDERS`（16 项名单，唯一的**数据源**） | 下面两个函数的判定基础 |
| `is_placeholder_runtime_instance_id()` | `RunRegistry.load()`、`verify_account_binding` |
| `resolve_runtime_instance_id()` | `RunRegistry.load_or_mint()`、`resolve_account_domain`、`issue_management_authorization` |

**本轮没有新增任何判定函数、没有新增名单**：`resolve_runtime_instance_id` 已是 U15-FU1
抽好的**单一归一化函数**（"哨兵/空/非字符串 → `None`；真值 → 去空白原值"），本轮三处
门禁全部改调它。之所以能做到，是因为 U15-FU1 已经把口径收敛到一个函数；本轮只需
**把它的调用点补到真正做判定的地方**（哨兵从"消费侧可见"变成"判定侧不可通过"）。

原因码：复用既有字面量 `"RUNTIME_INSTANCE_UNBOUND"`（修前由
`management_authorization` 在"串为空"这一层产出）。本轮**未新造任何原因码**；在
`runtime_identity.py` 里为它加了一个**同名常量**（值不变），使两处调用不再各写一遍
字符串 —— 这是**去重**，不是新码。

---

## 4. 反向证据（"不得误杀真值 / 不得放宽既有判定"）

`evidence/probe_e2e_*.log` 的 `[REV]` 段（**两态都成立**，逐字相同）：

| 观测点 | 两态结果 |
|---|---|
| 真值 `verify_account_binding(rt-real-1, rt-real-1)` | `allowed=True reasons=()` |
| 真值端到端 `submit_allowed` | `True reasons=()`（**没有新增阻断**） |
| `"unknownverse"` / `"known"` / `"n/a-x"` / `"UNKNOWN-X"` | `allowed=True reasons=()`（**只是长得像，不被误杀**） |
| 缺 `actual` → `RUNTIME_GENERATION_UNPROVEN` | 逐字不变 |

registry 侧反向证据（`evidence/u15_acceptance_*.log`，**两态都成立**）：

* 真实实例号（`rt-persisted-1`、`restarts=4`）照常读入，`load_or_mint` **不重铸**
  （`created=False`、`restarts=4` 保留）；
* `"unknownverse"` 照常读入且**不重铸**。

既有语义未变（两态都成立）：两边都未声明 `scope_hash` 时不比较；轮换凭据后绑定照常通过；
畸形入参仍是 `RUNTIME_GENERATION_MALFORMED`；`reconcile_allowed` 不受绑定失败影响。

**阈值未动**：`7 天 / 200 / 30 / 0.999 / 120` 一个数字未碰（本轮 diff 不含任何常量数值改动，
见 §1 的四份 `.diff`；`git diff 85722e2..6a24fb5` 的 `+` 行里没有数字字面量改动）。

---

## 5. 测试改动自证（两态可跑）

新增 3 个 `check_*` 函数（`tests/acceptance/test_runtime_identity.py`），沿用 U15-FU1 的
**两态探测标记**方式（不猜共享文件是否已打补丁）：

| 标记 | 位置 | 语义 |
|---|---|---|
| `U15_FU2_RUNTIME_INSTANCE_SENTINEL_GUARD` | `runtime_identity.py` | 授权路径修复在位 |
| `U15_FU2_RUN_REGISTRY_SENTINEL_GUARD` | `ops/supervision.py` | 源头修复在位 |

* 文件顶部**不** `import RUNTIME_INSTANCE_UNBOUND`，改用本地字面量 `_RUNTIME_INSTANCE_UNBOUND`
  —— 该码**两态都是既有码**，因此本套件在**未打补丁的 src** 上也能跑（两态可跑所必需）。
* 基线分支断言的是**缺陷形态**（哨兵被继承 / 哨兵×哨兵判通过），不是放宽：修后态走 guard
  分支，基线分支若被执行即失败。
* **未硬编码迁移版本集合**：本套件与迁移版本无关，未新增任何版本清单。

| 运行 | 结果 |
|---|---|
| 修后全量（`evidence/u15_acceptance_patched.log`） | `PASS=298 FAIL=0`，exit `0` |
| 新测试 × **未打补丁 src**（`evidence/u15_acceptance_hybrid_patched_tests_on_pristine_src.log`） | `PASS=291 FAIL=0`，exit `0` |

（`298 - 291 = 7`：guard-only 断言在基线态被"复现缺陷"断言替换，两态各自自洽。）

同批回归（同一工作树，修后）：

| 套件 | 结果 |
|---|---|
| `tests/acceptance/test_unattended_release.py`（U12） | `PASS=50 FAIL=0` |
| `tests/acceptance/test_supervision.py`（U13） | `166 checks`，exit `0` |
| `tests/acceptance/test_release_evidence_v2.py`（U09） | `86 通过 / 0 失败` |
| `tests/integration/test_strategy_position_manager.py`（U02） | `PASS=158 FAIL=0` |

---

## 6. full delta（L19：沙箱 `git init && git add -A`，只暂存不 commit）

两棵树：`git archive 85722e2`（pristine）与 `git archive 6a24fb5`（patched）解包到独立目录，
各 `git init -q && git add -A`，**只暂存不 commit**，再跑同一把驱动
`evidence/run_delta.py.txt`（94 脚本，逐条 `rc` + 尾部摘要入 TSV）。

L19 校验：**两态各打印 `沙箱已跟踪文件 892 个`** ⇒ `git ls-files` 真返回 **892** 条，
L19 生效。`smoke_u10a_release_package.py` **确实被执行到**（其尾部摘要出现在两态 TSV 里）——
这正是 L19 要防的"空列表同崩"。

| 指标 | 值 |
|---|---|
| 脚本数 | `94` |
| pristine 非零退出 | `0` |
| patched 非零退出 | `0` |
| **NEW_FAIL** | `∅` |
| **FIXED** | `∅` |
| **CHANGED** | `1` —— `smoke_u10a_release_package.py`，`rc` 均为 `0`，**仅尾部摘要里的 `release_id` 哈希不同**（`rel-b072c1c3f28fb277befd` → `rel-cd1f800ab8a593ac9c52`；该值由树内容派生，逐轮天然不同）。**不是行为变化**。 |

`git ls-files` 条数（两态同）：**892**。

---

## 7. 补丁应用性（3 种 git 配置）

在 base 树（`85722e2` 解包目录）上逐个 `.diff` 做 `git apply --check -v`
（`evidence/apply_check_3configs.log`）：

| 配置 | `Checking patch` | `Skipped` |
|---|---|---|
| 默认 | `4`（每份 diff 各 1） | `0` |
| `-c core.autocrlf=false -c core.eol=lf` | `4` | `0` |
| `-c core.autocrlf=true` | `4` | `0` |

三配置均逐份 `Checking patch`、**0 `Skipped`**，无 `error:`。

## 8. 质量门禁

`PYTHONUTF8=1 <venv>/python.exe scripts/check_quality.py` → `QUALITY CHECK OK files=304 untracked=0`
（`evidence/check_quality_patched.log`）。**未新增任何 exclude**。

## 9. 未吞 `KeyboardInterrupt` / `SystemExit`

本轮 diff **没有新增任何 `except` 子句**（`git diff 85722e2..6a24fb5` 中新增行不含
`except`/`try:`）；既有的 `except (KeyError, TypeError, ValueError)` 范围**未扩大**。
新增路径上唯一可能抛出的异常都会被正常传播，不吞任何 `BaseException` 子类。

---

## 10. 如实登记未尽事项

1. **`mint_runtime_generation(runtime_instance_id="unknown")` 仍能铸出一个哨兵运行代对象**
   （构造函数只 reject 空/形状错误，未 reject 哨兵）。本轮按任务要求只改**读取侧**
   （`RunRegistry.load()`）与**判定侧**（K04 绑定），没有改这个纯构造函数 —— 改了会与
   "重铸是恢复正确行为"的方向冲突，也会影响 `generation_id` 的确定性语义。
   影响面：**任何消费该对象的判定入口（发布清单 / K04 绑定 / 授权签发）现在都会拒绝或
   标记它**，因此不再有"被当成合法身份"的出口。若后续要求"连构造都不许产出哨兵"，
   需要单独一轮（会动 `mint_runtime_generation` 的公开契约）。
2. **`verify_account_binding` 对"实例号为空串"的运行代仍返回 `allowed=True`**（哨兵判定
   函数按 U15-FU1 设计只认非空字符串）。可达性极低：`mint_runtime_generation` 对空串抛
   `MissingRuntimeInputError`，`RunRegistry.load_or_mint` 对空实例号走重铸；只有**直接
   构造 `RuntimeGeneration`**（frozen dataclass，非受支持路径）才能造出。K04 门禁侧
   （`resolve_account_domain` / 签发）已由 `resolve_runtime_instance_id is None` 覆盖空串。
   本轮**未**顺手扩大 `verify_account_binding` 的判定面（避免越出登记范围）。
3. **`load()` 的"哨兵即无记录"会连带丢弃 `restarts` 计数**（旧记录整体不可信）。这是与
   "文件损坏"一致的既有口径，但意味着被污染过的状态根会**重新计数**。未做"只换实例号、
   保留 restarts"的折中 —— 那需要单独论证（会引入"部分可信"的中间态）。
4. 本轮**未**触碰任何已登记项之外的代码。

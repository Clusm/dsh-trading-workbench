# U15-FU3 — 运行代哨兵缺陷族的最后两个消费入口收口

| 项 | 值 |
| --- | --- |
| 工作区 | `C:\Users\Tao\Desktop\币安\mat-wt\U15F3` |
| 分支 | `task/U15-FU3` |
| base | `048033b`（U15-FU1 / U15-FU2 已集成） |
| 代码 head | `7dd1053` |
| 缺陷来源 | 复验者 Q 在 U15-FU2 复验时**实测**（非作者自评） |

## 1. 缺陷与修法

U15-FU2 修好了**源头**（`RunRegistry.load()` 把哨兵视为无记录并重铸）与 **K04 授权绑定**
（`verify_account_binding` / `management_authorization`）。Q 遍历消费入口时发现**还有两个入口
本轮未触碰，仍接受非空哨兵**：

| 入口 | 修前行为 |
| --- | --- |
| `src/core/entry_readiness.py:85 evaluate_entry_readiness(...)` | `runtime_instance_id='unknown'` → `allowed=True`、`reasons=()`（哨兵被当成合法身份） |
| `src/core/entry_readiness.py:216 authorization_matches_context(...)` | 两侧都是 `'unknown'` → `True`（"逐字相等"被读成同一个运行代） |

Q 判定这两处**暂不可达**（真正的开仓门禁 `authorize_new_entry` 已被 FU2 堵住，
且 `mint_runtime_generation` 是 FU2 自认的边界①，仍能构造哨兵运行代对象）。
**本轮按"同一缺陷族必须收口"处理**，不留"当前不可达所以先不管"的口子：任何把构造出来的
哨兵对象直接喂进这两个入口的调用方，从此也拿不到"通过"。

### 改动（`src/core/entry_readiness.py`，+27 行）

```python
from core.runtime_identity import (
    RUNTIME_INSTANCE_UNBOUND,
    is_placeholder_runtime_instance_id,
)

U15_FU3_ENTRY_READINESS_SENTINEL_GUARD = "U15-FU3"   # 两态探测标记
```

```python
    if not effective_runtime_id:
        reasons.append("RUNTIME_INSTANCE_UNPROVEN")          # 既有：空/缺失
    elif is_placeholder_runtime_instance_id(effective_runtime_id):
        reasons.append(RUNTIME_INSTANCE_UNBOUND)             # U15-FU3：哨兵
```

```python
    for expected_key, decision_attr in checks.items():
        expected = context.get(expected_key)
        actual = getattr(decision, decision_attr, None)
        if expected_key == "runtime_instance_id" and (
                is_placeholder_runtime_instance_id(expected)
                or is_placeholder_runtime_instance_id(actual)):
            return False                                     # U15-FU3：哨兵不构成绑定
        if not expected or actual != expected:
            return False
    return True
```

`evaluate_entry_readiness` 的 `allowed = not unique_reasons` 未动 —— 新增的阻断项自然把它
压成 `False`，`authorization_expires_at` 随之 `None`（该函数**既有**语义）。原因码复用
**既有** `RUNTIME_INSTANCE_UNBOUND`，**未新造**任何码。

## 2. 口径复用论证（全仓仍只有一份哨兵判定）

实测（`evidence/single_caliber.log`）：

```
== RUNTIME_INSTANCE_PLACEHOLDERS definitions ==
src/core/runtime_identity.py:1621:RUNTIME_INSTANCE_PLACEHOLDERS: tuple[str, ...] = (
== membership tests against the one list ==
src/core/runtime_identity.py:1636:    return value.strip().casefold() in RUNTIME_INSTANCE_PLACEHOLDERS
== is_placeholder_runtime_instance_id definitions ==
src/core/runtime_identity.py:1627:def is_placeholder_runtime_instance_id(value: object) -> bool:
```

* **名单只有一处定义**（`runtime_identity.py:1621`，16 项）。
* **判定只有一处实现**：`is_placeholder_runtime_instance_id`，其唯一真值判断是
  `value.strip().casefold() in RUNTIME_INSTANCE_PLACEHOLDERS`（:1636）。
  `resolve_runtime_instance_id`（:1639）是它的上层归一化（`resolve` 先问
  `is_placeholder`，再判空/非字符串），**不是**第二套判定。
* 本轮的**两个新判定点**（`entry_readiness.py:179` 与 `:258`）都**只调**这个谓词，
  文件内**没有**任何哨兵字面量、没有第二份名单、没有第二套 `casefold/strip` 归一化。
  `RUNTIME_INSTANCE_UNBOUND` 也是 **import 常量**（不是本地字面量副本）。
* 谓词调用点全集：`ops/supervision.py:266`（FU2 源头）、`runtime_identity.py:1646`
  （resolve 自身）、`:1846/:1847`（FU2 授权绑定）、`:2010`（FU1 mint 守卫）、
  `entry_readiness.py:179/:258/:259`（本轮）。FU1/FU2/FU3 是**同一个谓词的 6 个调用点**。

为什么两处用不同的**拼写**却仍是同一口径：`evaluate_entry_readiness` 问的是
"这个串是不是哨兵"（谓词），`authorization_matches_context` 问的也是同一件事
（对两侧各问一次）—— 两处都是谓词，没有用到归一化返回值，因此无需引入 `resolve`。
若改用 `resolve(...) is None`，结果**逐字相同**（`resolve` 只在哨兵/空/非字符串时返回
`None`），只是多绕一层；选择谓词是为了让"这是哨兵吗"这一问在代码里显式可读。

## 3. 两处前后对照（实测，非推断）

两棵树：base = `git archive 048033b` 解包；patched = 本 head 工作区。
探针：`evidence/probe.py.txt`（`python probe.py <tree_root>`）。

```bash
PYTHONUTF8=1 <venv>/python.exe probe.py <base_tree>    > evidence/probe_base.log    2>&1 ; echo $?
PYTHONUTF8=1 <venv>/python.exe probe.py <U15F3_worktree> > evidence/probe_patched.log 2>&1 ; echo $?
diff evidence/probe_base.log evidence/probe_patched.log
```

| 入口 | base | patched |
| --- | --- | --- |
| `evaluate_entry_readiness(runtime_instance_id='unknown')` | `[A:sentinel] {"allowed": true, "reasons": [], "runtime_instance_id": "unknown"}` | `[A:sentinel] {"allowed": false, "reasons": ["RUNTIME_INSTANCE_UNBOUND"], "runtime_instance_id": "unknown"}` |
| 同上，哨兵只经 `facts["runtime_instance_id"]` 通道 | `[A:facts_channel] {"allowed": true, "reasons": [], ...}` | `[A:facts_channel] {"allowed": false, "reasons": ["RUNTIME_INSTANCE_UNBOUND"], ...}` |
| `authorization_matches_context`（两侧哨兵） | `[B:sentinel_both] true` | `[B:sentinel_both] false` |

两态探针 `exit=0`（脚本自身无断言失败，仅打印事实）。`diff` 退出码 `1`，**只有 3 行不同**
（上面三行），其余每一行**逐字相同**（第 4、5 节的逐行 diff 输出）。

## 4. 反向证据（两态逐字相同 = 没修坏）

`diff probe_base.log probe_patched.log` 的**全部**差异只有 3 行（上表 `[A:sentinel]`、
`[A:facts_channel]`、`[B:sentinel_both]`）。以下各行两态**完全相同**：

* `[A:real]` `rt-2026-09-22-a1b2c3` → `allowed: true, reasons: []`（真值照常通过，**不新增阻断码**）。
* `[A:lookalike:*]` `unknownverse` / `known` / `n/a-x` / `UNKNOWN-X` → 全部 `allowed: true, reasons: []`
  （**像哨兵的真值不被误杀**）。
* `[A:tri_state]` 三态事实语义逐条未变：
  `order_mirror_ok/protection_mirror_ok` 的 `True/False/None` →
  `[]` / `ORDER_MIRROR_MISMATCH`（`PROTECTION_MIRROR_MISMATCH`） / `ORDER_MIRROR_UNPROVEN`
  （`PROTECTION_MIRROR_UNPROVEN`）；`service_accepting=True/False/None` → `[]` / `SERVICE_NOT_ACCEPTING` / `SERVICE_NOT_ACCEPTING`。
* `[B:real_both]` → `true`（真值仍构成绑定）。
* `[B:comparisons]` 逐条未变：`expired=false`、`missing_expiry=false`、`env_mismatch=false`、
  `scope_mismatch=false`、`runtime_mismatch=false`、`empty_context=false`、`none_context=false`、
  `lookalike:*=true`（TTL/环境/scope/进程绑定比较项**一条未改**）。
* `[B:sentinel_context_real_decision]` → `false`（两态都 false，未放宽）。

**未新增阻断码**：`_RUNTIME_INSTANCE_UNBOUND` 只在哨兵时出现；空实例号仍走**原有**
`RUNTIME_INSTANCE_UNPROVEN`（见 `[A:tri_state]` 上下文与验收套件中的
"空实例号仍产出原有 RUNTIME_INSTANCE_UNPROVEN（未换码）"）。

## 5. 测试改动自证（`tests/acceptance/test_runtime_identity.py`，+173 行）

新增 `check_sentinel_never_passes_entry_readiness_entries()`，登记进 `main()`。
**未改动任何既有断言**（本 diff 是纯新增：`git diff --stat` = 2 files, 200 insertions, 0 deletions）。

* **两态可跑**：用 `getattr(entry_readiness_module, "U15_FU3_ENTRY_READINESS_SENTINEL_GUARD", "")`
  探测交付物。标记缺席（base）走"复现缺陷"分支，标记在位（patched）走 fail-closed 分支。
  原因码用**本地字面量副本** `_RUNTIME_INSTANCE_UNBOUND`（测试夹具惯例，与 FU2 一致），
  因此**在未打补丁的 src 上也能跑**。
* **无硬编码迁移版本集合**：本轮不涉及迁移。
* 断言清单（patched 态实际输出见 `evidence/u15_acceptance_patched.log`）：

| 断言 | patched | base |
| --- | --- | --- |
| 哨兵实例号不再被读成合法身份（`allowed=False`） | PASS | — |
| 哨兵产出**可区分**的既有码 `RUNTIME_INSTANCE_UNBOUND`（且**只出现一次**） | PASS | — |
| 哨兵经 `facts` 通道同样被拒 | PASS | — |
| 授权上下文两侧哨兵 → `False` | PASS | — |
| 基线态：哨兵被判"就绪"且无原因码（复现缺陷） | — | PASS |
| 基线态：两侧哨兵被读成"同一个运行代"（复现缺陷） | — | PASS |
| 反向：真实运行代照常就绪且不新增阻断码 | PASS | PASS |
| 反向：像哨兵的真值不被误杀 / 仍可构成绑定 | PASS | PASS |
| 反向：三态事实语义逐条未变 | PASS | PASS |
| 反向：缺证据的既有码未被替换、也未叠加哨兵码 | PASS | PASS |
| 反向：空实例号仍产出原有 `RUNTIME_INSTANCE_UNPROVEN`（未换码） | PASS | PASS |
| 反向：TTL/环境/scope/运行代比较项未变 | PASS | PASS |

两态运行（把**新测试文件**放到 base 树上跑，证明它不依赖补丁）：

```bash
PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_runtime_identity.py   # base 树  -> exit 0
PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_runtime_identity.py   # patched  -> exit 0
```

## 6. 补丁可应用性

`git apply --check`（base = `git archive 048033b`，`git init && git add -A`）在 3 种配置下
均 `Checking patch`、**0 Skipped**、`rc=0`（`evidence/apply_check_3configs.log`）：

* default
* `-c core.autocrlf=false -c core.eol=lf`
* `-c core.autocrlf=true`

把两个 `.diff` **实际应用到 base** 后，与 patched 树逐文件比对（归一化行尾）→
`SAME src/core/entry_readiness.py`、`SAME tests/acceptance/test_runtime_identity.py`。

## 7. 全量离线回归 delta（94 脚本）

驱动（**按 L19**：base 树 `git archive 048033b` 解包后 `git init && git add -A`，**只暂存不
commit**）：`evidence/run_delta.py.txt`；逐脚本 rc + 尾部摘要落 TSV
（`evidence/reg_base.tsv` / `evidence/reg_patched.tsv`，各 94 行，驱动日志
`evidence/reg_*_driver.log`，均 `*_DONE=0`）。

```
scripts=94 pristine_nonzero=0 patched_nonzero=0
NEW_FAIL: ∅
FIXED: ∅
CHANGED: 1
   scripts/smoke_u10a_release_package.py (rc 0 -> 0)
     tail: ... release_id=rel-cd1f800ab8a593ac9c52 ...
        -> ... release_id=rel-ae04b32cc4af3c7d37ee ...
```

* **NEW_FAIL = ∅**（补丁没有让任何脚本由绿转红）。
* **FIXED = ∅**（本轮不指望修好任何既有红脚本；两态 `nonzero` 都是 0）。
* **CHANGED = 1，且只是发布包内容哈希**：`smoke_u10a_release_package.py` 每轮按**当前文件内容**
  算 `release_id`，改了 `entry_readiness.py` / 测试就必然变号。rc 两态同为 0，
  "沙箱已跟踪文件 918 个" 两态一致，属**内容指纹**而非行为差异（与 U15-FU2 的 CHANGED 同族）。

代码 head 的 `git ls-files` 条数（L19）：`7dd1053` → **918**（base `048033b` 亦为 918；
本 head 只改 2 个已跟踪文件，未新增代码文件）。补丁包自身的 20 个 `docs/**` 文件在
docs 提交 `b305345` 后使分支 head 增至 **938**（见 `evidence/git_ls_files_head.log`）——
**补丁包已被 head 跟踪**（`git ls-files` 列出 20 个文件，非 FU5 那种"未跟踪"状态）。

`scripts/check_quality.py`：**绿**（`exit=0`，`QUALITY CHECK OK files=304 untracked=0`，
`evidence/check_quality_patched.log`）。**未新增任何 exclude**：`pyproject.toml` 的
`extend-exclude` / `per-file-ignores` 本轮**零改动**。

## 8. 未尽事项（如实登记）

1. **`ReadinessSnapshot.runtime_instance_id` 仍原样回显入参**（哨兵时依然写 `"unknown"`）。
   本轮**只**加 fail-closed 判定（`allowed=False` + 原因码），**不改**该字段的回显，
   以免超出"只针对哨兵实例号加 fail-closed"的边界、动到既有消费者读数。该字段此刻
   不可能被当成授权凭据（`authorization_context()` 的消费方需要 `allowed`），
   但若后续要求快照也不回显哨兵，应另开一轮（FU1 已修的"读数即值"在清单侧）。
2. **`mint_runtime_generation` 仍能构造哨兵运行代对象**（FU2 自认边界①，本轮未动）。
   本轮的结果是：该对象喂进**任何**已收口入口（FU1/FU2/FU3 共 6 个谓词调用点）都拿不到通过。
3. 本轮**未**触碰 `7 天 / 200 / 30 / 0.999 / 120` 任何数值，`pyproject.toml`
   `extend-exclude` / `per-file-ignores` **未改**。

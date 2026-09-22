# Q 独立验收 — U15-FU2（运行代哨兵最后两条路径：源头 A + K04 授权绑定 B）

- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15-FU2\R1\q`（detached HEAD @ `8c67af8`）
- base = `85722e2`（`git archive`）／patched = `8c67af8`
- 解释器：`C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`，`PYTHONUTF8=1`
- 角色：Q（只验证、只写报告，不改产品代码）

---

## 1. 结论

**`PASS-with-notes`**（无阻塞项）。

- 第 1/2/3 条（A 源头、B 端到端、反向证据）**逐条独立复现，全部与作者声称一致**；
  A 的方向是"恢复真值"不是洗白；B 的端到端在补丁态 `submit_allowed=False` 且带
  `RUNTIME_INSTANCE_UNBOUND`，既有 `reconcile_allowed=True` 未变。
- 第 4 条：口径**确为单一**（全仓无第二份名单 / 第二套归一化）。
- **notes（非阻塞，须登记）**：
  1. 第 5① 条：author 称"不再有任何被当成合法身份的出口"**严格不成立** —— 另有两条
     **本轮未触碰**的消费入口仍接受非空哨兵：`evaluate_entry_readiness`
     （`inst='unknown'` → `allowed=True, reasons=()`）、`authorization_matches_context`
     （两侧 `'unknown'` → `True`）。可达性由已治愈的 registry 挡住，真正的开仓门禁
     `authorize_new_entry` 已堵，判**非阻塞**。
  2. §9 "本轮 diff 的 `+` 行不含 `except`/`try:`" **不准确**：新增行含
     `tests/acceptance/test_runtime_identity.py` 的 `try:` / `except
     ManagementAuthorizationError`（**仅测试**、只捕获具体异常，不吞
     `KeyboardInterrupt`/`SystemExit`）。实质结论（不吞 BaseException）成立。
  3. 两态 `git ls-files` 不等（base 892 / `8c67af8` 917），因 `8c67af8` 比代码 head
     `6a24fb5` 多 25 个补丁包文档；**代码头 `6a24fb5` 两态同为 892**。回归用
     `6a24fb5`（src/tests 与 `8c67af8` 逐字节相同）跑，结论与作者一致。
  4. 第 5② 条：`RunRegistry.load()` 会读入**空串**实例号的记录（实测 `instance=''`），
     但唯一消费入口 `load_or_mint` 会重铸；author 的"可达性极低"判定成立。

## 2. 第 1 条 — 残留 A（源头 registry）前后对照

我自建 fixture（`_scratch_Q/probe_q.py`，一份脚本两态）：`runtime_generation.json` 写入
`runtime_instance_id="unknown"`、`restarts=3`、`generation_id` 由**真实铸造**算出
（文件自洽，只有实例号是哨兵）。`load_or_mint` 传入派生账户引用 `demo:q-verify-acct`
（否则 `same_scope` 恒假，测不到"继承"分支）。

| 观测点 | base `85722e2` | patched `8c67af8` |
|---|---|---|
| `RunRegistry.load()` | `RunRecord(instance='unknown', restarts=3)` | `None` |
| `load_or_mint().created` | `False` | `True` |
| `load_or_mint().instance` | `'unknown'` | `'252ac7ed…'`（32 位 uuid hex，非哨兵） |
| `load_or_mint().restarts` | `3` | `0` |
| sentinel_eliminated | `False` | `True` |

**方向核查**：patched 的"重铸"是**丢弃不可信记录 + 取回真值**，不是把损坏状态"洗白"：
`load()` 返回 `None`（不抛错，服务照常启动），`restarts` 归零 = 旧记录整体不可信
（与"文件损坏视为无记录"同一既有口径）；判定集合只增不减（哨兵从"可继承"变成"不可继承"）。

## 3. 第 2 条 — 残留 B 端到端（污染 registry -> 授权结论）

`probe_q.py` 的 `[E2E]` 段。路径：污染 registry 文件 → 运行代 → 管理授权 → 授权结论
（`evaluate_management_request`）。因 patched 的源头会把哨兵**治愈**，`(a)` 记录源头效果，
`(b)` 用"哨兵运行代 + 修前已签发的在途授权"（`model_copy`）这一形态，两态可比。

| 观测点 | base | patched |
|---|---|---|
| `(a)` registry→generation.instance | `'unknown'` | `'5c33afcc…'`（真值） |
| `(a)` registry→generation.created | `False` | `True` |
| **(b) `submit_allowed`** | **`True`** | **`False`** |
| **(b) `binding_reason_codes`** | **`()`** | **`('RUNTIME_INSTANCE_UNBOUND',)`** |
| `(b)` `reason_codes` | `()` | `('RUNTIME_INSTANCE_UNBOUND',)` |
| `(b)` `reconcile_allowed` | `True` | `True`（既有边界未变） |
| `(c)` 治愈后 submit_allowed | `True` | `True`（不新增阻断） |

单函数口径（`[B]` 段）：

| 观测点 | base | patched |
|---|---|---|
| `verify_account_binding(哨兵,哨兵)` | `allowed=True reasons=()` | `allowed=False ('RUNTIME_INSTANCE_UNBOUND',)` |
| `resolve_account_domain(哨兵).bound` | `True / ()` | `False / ('RUNTIME_INSTANCE_UNBOUND',)` |
| `issue_management_authorization(哨兵)` | `OK runtime_instance_id='unknown'` | `REJECTED ('RUNTIME_INSTANCE_UNBOUND',)` |

## 4. 第 3 条 — 反向证据逐条（两态实测）

| 条目 | base | patched |
|---|---|---|
| 真值绑定 `verify('rt-real-1')` | `True ()` | `True ()` |
| 真值端到端 `submit_allowed` | `True ()` | `True ()`（**不新增阻断**） |
| `unknownverse` / `known` / `n/a-x` / `UNKNOWN-X` | `True ()` | `True ()`（**不误杀**） |
| `actual is None` | `RUNTIME_GENERATION_UNPROVEN` | 同 |
| 未声明 scope_hash 不比较 | `allowed=True` | 同 |
| 声明 scope 而 actual 无 | `SCOPE_HASH_UNBOUND` | 同 |
| 轮换 credential_epoch 后 | `allowed=True` | 同 |
| 畸形入参 | `RUNTIME_GENERATION_MALFORMED` | 同 |
| 真值 registry（`rt-persisted-1`, restarts=4）`load_or_mint` | `created=False instance='rt-persisted-1' restarts=4` | 同（**不重铸**） |
| `unknownverse` registry | `created=False` | `created=False` |
| `reconcile_allowed` | `True` | `True` |

结论：**反向 12 条逐条实测全通过，无一条被修坏。**

## 5. 第 4 条 — 口径单一性核查

全仓（`src` + `scripts`）grep：

- 哨兵**名单**只有一处定义：`runtime_identity.py:1621 RUNTIME_INSTANCE_PLACEHOLDERS`
  （16 项）；无第二份字面元组（正则 `"unknown","none"…` 仅命中该行）。
- **判定函数**只有 `is_placeholder_runtime_instance_id`（唯一 `casefold() in RUNTIME_INSTANCE_PLACEHOLDERS`）。
- **归一化函数**只有 `resolve_runtime_instance_id`。
- `resolve_runtime_instance_id` 调用点：`management_authorization.py:182,247`（本轮新增）、
  `ops/supervision.py:304`（本轮新增）、`release_manifest.py:213`（FU1）。
- `is_placeholder_runtime_instance_id` 调用点：`runtime_identity.py:1646`（resolve 内部）、
  `1846`（verify_account_binding，本轮新增）、`2010`（collect_runtime_identity，FU1）、
  `ops/supervision.py:266`（load，本轮新增）。

**无第二份名单、无第二套归一化。** 5 个门禁调用点（load / load_or_mint / verify_account_binding /
resolve_account_domain / issue_management_authorization）全部走同一函数。**未发现成因复现。**

## 6. 第 5 条 — 两条未尽事项的独立严重度判定

**① `mint_runtime_generation("unknown")` 仍能构造哨兵对象（未闭合的消费入口排查）**

实测（`probe_consumers.py`，两态）：

| 消费入口 | base | patched | 判定 |
|---|---|---|---|
| `verify_account_binding` | 放行 | **拒绝** `RUNTIME_INSTANCE_UNBOUND` | 已堵 |
| `resolve_account_domain` / `issue_management_authorization` | 放行/签发 | **拒绝** | 已堵 |
| `authorize_new_entry`（开仓身份门禁） | `IDENTITY_UNPROVEN` | **+`RUNTIME_INSTANCE_UNBOUND`** | 已堵（继承） |
| `collect_runtime_identity` / `release_manifest` | 标记 UNPROVEN | 同（FU1） | 已堵 |
| **`evaluate_entry_readiness`** | `inst='unknown'` → **`allowed=True, reasons=()`** | **同**（未变） | **未堵（残留）** |
| **`authorization_matches_context`** | 两侧同为 `'unknown'` → `True` | **同**（未变） | **未堵（残留）** |

严重度判定：`evaluate_entry_readiness` 的 `if not effective_runtime_id` 与
`authorization_matches_context` 的 `not expected or actual != expected` 都是**同一族**的
"非空/相等即值"判据，确实**仍接受非空哨兵**，author 的"不再有任何被当成合法身份的出口"
严格说来**不成立**（存在两个消费入口）。但：
- 这两处**本轮 diff 未触碰**（base/patched 逐字相同），**不是本轮引入的回归**；
- **可达性受损**：`service.py:165` 用 `RunRegistry.load_or_mint`（本轮已治愈）取
  `self.runtime_instance_id`，另一分支是 `uuid.uuid4().hex`；即服务持有的实例号
  已不可能经注册表变成哨兵。要触达这两个入口需要**另外的注入路径**（例如
  `mint_runtime_generation("unknown")` 的产物被外部直接喂进 readiness facts），
  本轮范围内**未发现**这样的调用点。
- 二者也**不构成"授权绑定"**：`authorize_new_entry`（真正的开仓身份门禁）已堵。

**结论**：本族登记的**两条路径（A 源头 / B K04 授权绑定）已闭合**；
"连构造都不许产出哨兵"未做（author 已登记，非本轮范围）。**另登记两条更外层的
消费入口残留**（`evaluate_entry_readiness` / `authorization_matches_context`）——
判为**非阻塞**（本轮未触碰、可达性由治愈后的 registry 挡住、且真正的开仓门禁已堵），
但应在下一轮登记，不能声称"任何出口都已拒绝"。

**② `verify_account_binding` 对空串实例号仍 `allowed=True`**

实测两态一致：`verify(empty,empty) → allowed=True, reasons=()`；`resolve_account_domain(empty).bound=False`。
- 受支持构造器：`mint_runtime_generation("")` → `MissingRuntimeInputError`（实测），
  `RunRegistry.load_or_mint` 对空实例号走重铸（实测 `empty.load_or_mint().created=True`）；
  `collect_runtime_identity` 对空串抛 `MissingRuntimeInputError`。
- **`RunRegistry.load()` 会读入空串实例号的记录**（实测 `empty.load() = instance='' restarts=5`），
  但仅 `load_or_mint` 是服务使用的入口且会重铸；`load()` 无其它调用点
  （全仓仅 `service.py:165` + `supervision.py:377`，都走 `load_or_mint`）。
- 唯一能构造空串 `RuntimeGeneration` 的方式是**直接实例化 frozen dataclass**（非受支持路径）。

**结论**：与 author 判定一致 —— **可达性极低，非阻塞**。K04 门禁侧（`resolve_account_domain` /
签发）对空串已 `bound=False`（两态都如此）。

## 7. 第 6/7/8 条 — 门禁数字 / 回归 / 补丁矩阵与自足性

### 7.1 门禁数字与既有语义（第 6 条，自核不采信作者）

- `git diff 85722e2 6a24fb5 --stat` = **仅 4 个文件**（3 src + 1 test），
  `+338 / −7`，与声称一致。
- 阈值 **`7.0 / 200 / 30 / 0.999 / 120.0`**：所在文件
  `src/core/release_evidence.py` 两态 **sha256 逐字节相同**
  （`b550fc2c…`），`DEFAULT_THRESHOLDS` 与 `gates` 键集合 **未动**。
- 本轮 diff 的 `+` 行中**无新增数值字面量**（含数字的行只有 `U15-FU2` 标记与注释）。
- `+` 行**无新增 `except`/`try:` 于 src**（src 部分 0 处）；
  **但测试文件新增了 1 处 `try:` + 1 处 `except ManagementAuthorizationError:`**
  （`tests/acceptance/test_runtime_identity.py`，见 §1 note 2）。

### 7.2 回归（第 7 条）— L19 沙箱

`git archive` 解包 → `git init -q && git add -A`（**只暂存不 commit**，L19）。

- `git ls-files`：base=**892**，代码头 patched(`6a24fb5`)=**892**（同集合，4 文件被改）；
  `8c67af8` 自身=917（+25 补丁包文档，`git diff 6a24fb5 8c67af8 -- src tests` 为空）。
- 官方入口 `scripts/run_offline_regression.py`（`check=True` 逐脚本）：
  base `OFFLINE REGRESSION OK scripts=94` rc=0；patched 同 rc=0。
- 逐脚本驱动（`run_delta_q.py`）两态 TSV 比对：

| 指标 | 值 |
|---|---|
| 脚本数 | `94`（两态） |
| 非零退出 | base `0` / patched `0` |
| **NEW_FAIL** | `∅` |
| **FIXED** | `∅` |
| **CHANGED** | `1` —— `scripts/smoke_u10a_release_package.py`，`rc 0→0`，仅尾部摘要 `release_id` 哈希不同（`rel-b072c1c3f28fb277befd` → `rel-cd1f800ab8a593ac9c52`，与作者报告**逐字相同**；该值由树内容派生）。 |
| `smoke_u10a_release_package.py` 真跑到 | **是**（两态摘要行均在 TSV 内，`沙箱已跟踪文件 892 个`） |

- `scripts/check_quality.py`：patched `QUALITY CHECK OK files=304 untracked=0` rc=0；
  base 同为 `files=304` ⇒ **未新增 exclude**（`check_quality.py` 两态未改）。
- `tests/acceptance/test_runtime_identity.py`（patched）：`PASS 298` rc=0；
  C 标签含"fail-closed"的 3 行为测试名，非失败。
- 混合（**patched 测试 × 未打补丁 src**）：`PASS 291` rc=0（`298−291=7` guard-only 断言）。
- `tests/acceptance/test_unattended_release.py`：`PASS=50 FAIL=0`。
- `scripts/smoke_contracts_v3.py`：`PASS 173 / FAIL 0`（golden cases=160）。

### 7.3 补丁矩阵 + 自足性（第 8 条）

- 4 份 `.diff` × 3 种 git 配置（默认 / `core.autocrlf=false core.eol=lf` /
  `core.autocrlf=true`）`git apply --check -v`：**每份各 1 次 `Checking patch`、
  0 `Skipped`、0 `error:`**（共 12 组全绿）。
- 自足性：base 全新解包树上依次 `git apply` 4 份 diff（**全部 OK，无 rejected**），
  再与 `6a24fb5` 的 4 个 blob **归一化行尾后 sha256 比对**：**`SAME`×4**。

## 8. 未能验证的部分

- 未运行 `multi-agent-trader` 运行目录内的真实/demo 服务，也未触碰 `.env`/`data/`/
  密钥/账户标识/风险档/白名单/live 开关（只读约束）。
- 未采信也未打开作者的 `evidence/*.log`，全部证据为本轮**独立重跑**所得
  （唯一例外：两态 `release_id` 哈希与作者报告**独立吻合**，可互相印证）。
- 哨兵消费入口排查范围 = `src/` + `scripts/`；**未穷举 `tests/` 内的名单/用法**
  （测试内另有 `_RUNTIME_INSTANCE_UNBOUND` 字面量副本，属测试夹具，非产品路径）。
- 未做并发/多进程下 registry 重铸的时序验证（仅单进程两态）。
- 未验证 `8c67af8` 那 25 个补丁包文档对 `smoke_release_package` 类脚本的影响
  （回归在代码头 `6a24fb5` 上跑，见 §7.2）。

## 9. 报告路径

`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15-FU2\R1\q\_scratch_Q\Q_U15-FU2_r1.md`

原始证据（同目录）：`probe_q.py` / `probe_patched.log` / `probe_base.log` /
`probe_consumers.py` / `delta_base.tsv` / `delta_patched.tsv` / `delta_compare.log` /
`official_base.log` / `official_patched.log` / `u15_patched.log` / `hybrid.log`。

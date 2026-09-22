# Q 独立验收报告 — U15-FU1 (R1)

工作树: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15-FU1\R1\q`
HEAD: detached `bf9b304` (= code `ad0d2ea` + 补丁包/证据)
pristine 对照: `02b93da`
解释器: `C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe` (无 pytest)

## 0. 结论

**`PASS-with-notes`** —— 未发现阻塞项。缺陷链的每一环都在**我自建的隔离夹具**上独立
复现并闭环；未能推翻作者的任何一条声称。

**阻塞项：无。**

两条 note（均不阻塞发布，建议顺手处理）：
1. `tests/acceptance/test_runtime_identity.py:8` docstring 仍写"**不启动服务**"，而新增的
   `check_service_manifest_registers_missing_run_generation` 确实构造真实 `TradingService`；
   补丁包 README §4 声称该 docstring 已随之更新，**与实际不符**（numstat 171/0，无删行）。
   实测安全性无问题（paper + 隔离 `MTA_STATE_ROOT`，`state_base_isolated=true`，不碰真实 `data/`）。
2. K04 残留（README §7.2 **已自认**）：`management_authorization.py` /
   `verify_account_binding` 对本轮新识别的哨兵**不做识别**（仍是"非空即通过"）。
   经**损坏 registry** 混入的哨兵在 K04 路径上仍可能被当成有效运行代 —— 已在 §7b 实测。
   属范围外残留，非本缺陷活路径。

**最关键的独立结论**（全为我自建夹具产出，未采信作者 evidence）：
- 服务链路：修后正常态 `manifest == service.runtime_instance_id`（真实值透传、不加阻断）；
  降级态 **服务照常启动**、`manifest=''`、`blockers=['RUNTIME_GENERATION_UNPROVEN']`。
  **不存在"没有运行代 → 启动失败"的过度修正。**
- U09 门禁：占位符清单 `PASS/[]`（修前）→ **`NO_DATA/['IDENTITY_REF_MISSING']`（修后）**；
  真实值两态 `PASS`。
- 哨兵 25/25 命中、14 项"像哨兵的真值"零误杀、空串/空白/非字符串**仍抛**
  `MissingRuntimeInputError`。
- 新断言强制扣旧实现时 **7/7 FAIL**（非空转）。

## 1. 服务链路前后对照 (真实 TradingService, 隔离 MTA_STATE_ROOT)

夹具: `_scratch_Q/probe_service.py`（自建；paper；`MTA_STATE_ROOT` 指向自建 tempdir，
`state_base_isolated=true` 已核实；清空 `MTA_PROFILE`/`MTA_RUNTIME_INSTANCE_ID`）。
pristine 树用 `git archive 02b93da` 解到 `_scratch_Q/trees/pristine`（未动任何 worktree）。

| 树 | 态 | 服务启动 | service.runtime_instance_id | manifest.runtime_instance_id | identity_blockers |
|---|---|---|---|---|---|
| pristine | normal | ✅ | `bacf19ef…` (真实32hex) | **`'unknown'`** | **`[]`** |
| pristine | degraded | ✅ | `b87ac65a…`(临时值) | **`'unknown'`** | **`[]`** |
| patched | normal | ✅ | `32432633…` | **`32432633…`（==service，透传）** | `[]`（不新增） |
| patched | degraded | ✅ | `63abcb44…`(临时值) | **`''`** | **`['RUNTIME_GENERATION_UNPROVEN']`** |

- 正常态修后 manifest **等于** service 手上的真实 32-hex 运行代；blockers 未新增。✅
- 降级态修后**服务照常启动**（`started=true`，`run_generation_is_none=true`），
  manifest 留空（且**不等于** service 那个没持久化的临时值——未被冒充），
  blockers 含 `RUNTIME_GENERATION_UNPROVEN`。✅ **未过度修正**。
- 修前两态都是 `'unknown'` + `[]` —— 缺陷形态与缺陷链描述一致。✅
- 清单键集合：两态都只有 `runtime_instance_id` 命中 `identity`/`instance` 关键字，
  **无** `identity_ref`/`instance_id` 别名键去覆盖 U09 的 `_IDENTITY_KEYS`。

## 2. U09 发布门禁读数独立复现

夹具: `_scratch_Q/probe_u09_gate.py`（自建合成库 `AuditDatabase` 于系统 tempdir；
不读真实 data/.env）。走完整 `build_release_readiness_report`，读 `report["identity"]`。

| 清单 runtime_instance_id 来源 | pristine 树 | patched 树 |
|---|---|---|
| 不传参（旧 `'unknown'` / 新 `''`） | `PASS` `[]` | **`NO_DATA` `['IDENTITY_REF_MISSING']`** |
| 显式 `"unknown"` 哨兵 | `PASS` `[]` | **`NO_DATA` `['IDENTITY_REF_MISSING']`** |
| env `MTA_RUNTIME_INSTANCE_ID="unknown"` | `PASS` `[]` | **`NO_DATA` `['IDENTITY_REF_MISSING']`** |
| 真实值 `"rt-probe-1"` | `PASS` `[]` | `PASS` `[]` |

作者声称与我的独立复现**逐格一致**。机制（未改 `release_evidence.py`）:
`_identity_block` → `_first_present(manifest, ("runtime_instance_id","identity_ref","instance_id"))`
跳过 `(None, "")`；`"unknown"` 非空 → identity_key 命中 → 无码 PASS；`""` → 跳过
→ `CODE_IDENTITY_REF_MISSING` → NO_DATA。artifact_hash 仍 64 位，故不触发
`ARTIFACT_HASH_MISSING`（诊断准确指向缺的是身份引用）。

## 3. 哨兵正/反面 + 空值仍抛

夹具: `_scratch_Q/probe_sentinels.py` + `probe_collect.py`（自建最小状态根）。

- **正面**：16 项哨兵（unknown/none/null/nil/n/a/na/undefined/unset/missing/
  not-set/notset/-/--/?/placeholder/tbd）**逐一无漏**；另加 9 项大小写/空白变形
  （`UNKNOWN` `Unknown` `" unknown "` `"\tNone\n"` `N/A` `TBD` `"  ?  "` `Not-Set`
  `UnDeFiNeD`）合计 **25/25** 全部识别为缺失 → `positive_misses == []`。
- **反面（误杀）**：14 项"长得像"的真值（`unknownverse` `known` `none2` `tbd2`
  `n/a-x` `unknownish` `placeholder1` `missing_data` `nulll` `n-a` `??` `---`
  `real-rt-1` `rt-probe-1`）**无一被误判**，`resolve` 原样返回（去空白）。
- **显式 None**：patched 不再抛，`identity.runtime_instance_id==""`、
  blockers 含 `RUNTIME_GENERATION_UNPROVEN`；pristine 则抛
  `MissingRuntimeInputError: runtime_instance_id` —— 语义确已分化。
- **未放宽**：空串 / 纯空白（空格、制表）/ 非字符串（`7`）在三态下**仍抛**
  `MissingRuntimeInputError(["runtime_instance_id"])`。
- 占位符/None 时 identity 其余证据**照常采集**（artifact_hash 64、dependency_hash 64），
  **没有**整份降级成 `IDENTITY_COLLECT_FAILED` —— 口径 B（不抛错）达成。
- 清单链路：`build_release_manifest(arg='unknown'|'none'|'null'|'nil'|'UNKNOWN'|'Unknown')`
  → 字段 `''` + 含阻断码；`arg='unknownverse'|'known'|'none2'` → 原样透传 + 无阻断。

## 4. U12/U15 测试改动逐 hunk 裁决 + 反向测试

### 4.1 逐 hunk：有无删除/放松既有断言
`git diff --numstat 02b93da ad0d2ea -- tests/`:
`test_runtime_identity.py` **171 / 0**（纯新增，0 行删除）；
`test_unattended_release.py` **38 / 8**。

U12 删除的 8 行**全部**落在 `test_identity_is_never_silently_empty` 内，逐行核对：
- `complete = bool(artifact_hash) and bool(runtime_instance_id)` → 改为
  `collected = ... and "IDENTITY_COLLECT_FAILED" not in blockers`。**这是修的不是删的**：
  旧第二合取项在无运行代时取哨兵 `"unknown"`（恒真），正是缺陷本体。新口径把
  "证据已采集"与"运行代已证明"拆成两件事，后者由新增的两条断言**单独且强制**断言。
- 断言名/文案 2 处重命名（`身份完整…` → `身份证据已采集…` / `身份证据完整时…`）——
  判定条件逐字未变（`True`；`"IDENTITY_COLLECT_FAILED" not in blockers`）。
- `scan_ok == complete or (not scan_ok and not complete)` → `scan_ok == collected or (…)`，
  条件形状逐字不变，只换被比较的变量名。
- **无任何旧断言被删除或条件被放松**。

`check()` 计数（`test_identity_is_never_silently_empty` 内）:
pristine 正常路径 **3** 条 → patched 正常(guard)路径 **6** 条（+3），与作者声称一致。
`test_runtime_identity.py` 的 diff **无一行删除**，`main()` 只追加 2 个调用 →
**未改任何既有断言**。

### 4.2 两态探测机制
`tests/acceptance/test_runtime_identity.py` 用
`_u15_fu1_guard() = getattr(runtime_identity_module, "U15_FU1_RUNTIME_INSTANCE_GUARD", "")`；
U12 用 `getattr(identity_module, "U15_FU1_RUNTIME_INSTANCE_GUARD", "")`。
**无硬编码迁移版本集合**（未新增任何形如 `{"v1","v2",…}` 的版本名单；
新增的唯一常量是字符串标记 `"U15-FU1"`）。✅

### 4.3 反向测试（新断言扣旧实现）
- **套件级**（`_scratch_Q/trees/hybrid` = pristine src + patched tests）：
  两套都 **全绿**。但这是**设计使然**——旧实现的共享文件里没有
  `U15_FU1_RUNTIME_INSTANCE_GUARD`，测试走**基线分支**（断言缺陷形态
  `instance == "unknown"` 且无 `RUNTIME_GENERATION_UNPROVEN`）。这不是"新断言空转",
  而是本项目"两态可跑、不假定补丁已应用"的既定要求。日志:
  `_scratch_Q/hybrid_ri.log`、`hybrid_u12.log`。
- **断言级**（`_scratch_Q/probe_reverse.py`，绕开 guard 分支，**强制求值新断言本体**）：

| 新断言本质 | pristine | patched |
|---|---|---|
| `manifest.runtime_instance_id == ""`（无运行代） | **FAIL**（实测 `'unknown'`） | PASS |
| `RUNTIME_GENERATION_UNPROVEN in manifest.identity_blockers` | **FAIL**（`[]`） | PASS |
| `collect('unknown').identity.runtime_instance_id == ""` | **FAIL** | PASS |
| `collect('unknown')` 带 `RUNTIME_GENERATION_UNPROVEN` | **FAIL** | PASS |
| 服务降级态 `manifest.runtime_instance_id == ""` | **FAIL**（`'unknown'`） | PASS |
| 服务降级态 带 `RUNTIME_GENERATION_UNPROVEN` | **FAIL**（`[]`） | PASS |
| 反向：真实值 `rt-rev` 透传且不带该码 | PASS（两态都成立） | PASS |

`NEW_ASSERTIONS_ALL_PASS`：pristine = **false**，patched = **true**。
→ 新增断言**确在检验新口径**，非空转；反向证据断言两态均成立，未把正常路径误标阻断。

## 5. 门禁数字与既有语义未动

- `src/core/release_evidence.py` **不在补丁集**（`git diff --stat` 仅 5 文件），因此
  `DEFAULT_THRESHOLDS`、`gates` 键集合、`_identity_block` 判定**物理未改**。
- 逐值核对两态：`min_observation_days=7.0`、`min_directional_candidates=200`、
  `min_eligible_closed_trades=30`、`min_observation_coverage=0.999`、
  `max_observation_gap_seconds=120.0` —— **一字未改**。
- `gates` 键集合两态一致（10 个）:
  `critical_open_incidents, directional_candidates, eligible_closed_trades,
  entry_readiness, network_rate_limit, observation_coverage, observation_days,
  schema_version, submitting_orders, unknown_orders`。报告顶层键集合亦一致
  （唯一差异是 `identity.state`：`PASS` → `NO_DATA`，即本修复**意图**）。
- `RUNTIME_GENERATION_UNPROVEN` 在 **pristine 已存在**（`runtime_identity.py:196`）
  → 复用既有码，**未新造**。补丁新增的 `+` 行里只出现
  `RUNTIME_INSTANCE_PLACEHOLDERS` / `U15_FU1_RUNTIME_INSTANCE_GUARD` /
  `MTA_RUNTIME_INSTANCE_ID` 三个新字符串，**无新原因码**。
- `IDENTITY_COLLECT_FAILED` 未被挪作他用：补丁的 `+` 行未引入该串；它仍是
  `_identity_evidence` 兜底分支的码（占位符路径**不**走它 —— 见 §3：
  artifact_hash/dependency_hash 仍 64 位、未整份降级）。

## 6. 回归 (offline 94 + quality + acceptance + smoke)

沙箱（L19）：`git archive 02b93da / ad0d2ea` 解包 → `git init && git add -A`（**只暂存不 commit**）。
`git ls-files` 条数：**pristine 857 / patched 857**（与作者一致；`smoke_u10a` 自报
"沙箱已跟踪文件 857 个" 交叉印证）。

### 逐脚本 94 项（自建 harness，不 fail-fast；两态各一遍）
`scripts=94`（`SMOKE MANIFEST OK included=81 excluded=1` + 9 个 tests/ + 4 个其它）。

```
NEW_FAIL = []        FIXED = []        BOTH_FAIL = []
patched fails = []   pristine fails = []
CHANGED(输出尾差) = 3 个，且**恰是**本补丁触及的文件：
  - scripts/smoke_u10a_release_package.py   : 37 通过/0 失败（两态同），仅 release_id 变
  - tests/acceptance/test_runtime_identity.py : PASS 262 → 276（+14，含新节）
  - tests/acceptance/test_unattended_release.py: PASS=47 → PASS=50（+3）
```
未出现任何"由绿转红"（NEW_FAIL 空）。日志: `_scratch_Q/reg_{pristine,patched}.json/.log`。

**`smoke_u10a_release_package.py` 确认真跑到**：单次 89.17s、`结果: 37 通过 / 0 失败`、
自报沙箱 857 跟踪文件；官方入口日志里也出现 `=== OFFLINE scripts/smoke_u10a_release_package.py ===`。

### 官方入口 `scripts/run_offline_regression.py`（两态各一遍）
```
pristine: SMOKE MANIFEST OK included=81 excluded=1 … OFFLINE REGRESSION OK scripts=94   exit=0
patched : 同上                                                                        exit=0
```
日志: `_scratch_Q/official_{pristine,patched}.log`。

### `check_quality.py`（两态）
两态均 `QUALITY CHECK OK files=304 untracked=0`，`SECRET SCAN OK`，退出 0。
**`check_quality.py` 在 `git diff --name-only` 里不存在**（未被本补丁改动）→ 不存在
"为变绿新增 exclude"。且两态 files/untracked 完全相同。

### `scripts/smoke_contracts_v3.py`（两态）
两态均 `结果：PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)`，退出 0。

### 验收套件（三树）
| 树 | test_runtime_identity | test_unattended_release |
|---|---|---|
| reg_pristine（旧码+旧测） | PASS 262 / FAIL 0 | PASS=47 FAIL=0 |
| reg_patched（新码+新测） | **PASS 276 / FAIL 0** | **PASS=50 FAIL=0** |
| hybrid（旧码+新测） | PASS 271 / FAIL 0（走基线分支） | PASS=49 FAIL=0 |

与作者 evidence 逐项吻合（276 / 271 / 47 / 50 / 49）。

## 7. 补丁矩阵 (5 patch x 3 git config)

自建 3 份 pristine 解包树（各 `git init`），对 5 个 `.diff` 逐个
`git apply --check --verbose`：

| git 配置 | Checking patch | Skipped |
|---|---|---|
| default（无 `-c`） | **5/5**，每 patch rc=0 | 0 |
| `core.autocrlf=false core.eol=lf` | **5/5**，每 patch rc=0 | 0 |
| `core.autocrlf=true` | **5/5**，每 patch rc=0 | 0 |

与作者 `evidence/apply_check_3configs.log` 一致。日志: `_scratch_Q/patchmatrix_Q.log`。

## 7b. 对抗性补测（我额外加的）
**遗留/损坏 registry**：把 `runtime_generation.json` 的 `runtime_instance_id` 直接改成
`"unknown"`（README §7.1 声称"端到端可见"）：

| 树 | service.runtime_instance_id | manifest.runtime_instance_id | identity_blockers |
|---|---|---|---|
| pristine | `"unknown"` | **`'unknown'`** | **`[]`** |
| patched | `"unknown"`（mint 未动，如 §7.1 所述） | **`''`** | **`['RUNTIME_GENERATION_UNPROVEN']`** |

→ README §7.1 的"端到端可见"**属实**：哨兵即使从 registry 混进来，也会在清单侧被
归一化并显式登记。夹具: `_scratch_Q/probe_corrupt_registry.py`。

## 8. 未能验证的部分
1. **未运行作者自己的探针脚本**（`probe_manifest_identity.py.txt` 等以 `.txt` 内嵌）。
   我用**自建**夹具独立复现，读数逐格一致；但若作者的探针里还有我未覆盖的分支，
   本次未核对。
2. **未触碰正在运行的 demo 服务**（硬约束要求不停）。因此"在真实常驻 demo 进程上
   清单落盘变成 `''`+阻断码"这一条**未在活进程上观测**；我只在隔离 `MTA_STATE_ROOT`
   的新起进程里验证。
3. **K04 授权残留未做端到端验证**（README §7.2 自认）：我读到
   `management_authorization.py:165-190` / `verify_account_binding`（`runtime_identity.py:1801`）
   均**未被本补丁改动**，其中
   `if not str(generation.runtime_instance_id or "").strip(): reasons.append("RUNTIME_INSTANCE_UNBOUND")`
   仍是"非空即通过"，`verify_account_binding` 也不做占位符识别。
   → 由**损坏 registry** 混入的哨兵（见 §7b）在 K04 路径上仍可能被当成有效运行代。
   这是**已登记的范围外残留**，不是本缺陷的活路径，但对"发布门禁对身份的判定"一族
   而言仍是敞口。**未知其可利用性**（需要先污染 registry）。
4. **U09 下游脚本未端到端跑**（`scripts/report_release_readiness.py`、
   `report_demo_acceptance.py`）：我直接调 `build_release_readiness_report`（含合成库 +
   真实清单），未走这两个 CLI 入口的完整参数路径。
5. **无 pytest**（环境未装），全部按设计走各文件 `main()` 独立入口。
6. **单平台**：win32 / Python 3.13 / 本机 git。补丁矩阵第 3 种配置只做
   `git apply --check`，**未真正 apply 后再跑测试**（`core.autocrlf=true` 下的行为面未测）。
7. **文档不一致（非功能，low）**：补丁包 README §4 声称
   "该文件 docstring 的'不启动服务'边界随本轮更新为…"，但
   `tests/acceptance/test_runtime_identity.py:8` 仍是
   `不联网、不启动服务、不下单。`（`git diff --numstat` = 171/0，**无一删除**，
   故 docstring 实际未被更新）。而新节 `check_service_manifest_registers_missing_run_generation`
   确实构造了真实 `TradingService`。→ **docstring 已失准**，建议作者补一句
   （paper + 隔离 `MTA_STATE_ROOT`，不联网不下单）。实际安全性我已实测：
   `state_base_isolated=true`，未碰真实 `data/`。

## 9. 报告文件路径
`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15-FU1\R1\q\_scratch_Q\Q_U15-FU1_r1.md`

### 附件（同目录 `_scratch_Q/`）
`probe_service.py` / `probe_u09_gate.py` / `probe_sentinels.py` / `probe_collect.py` /
`probe_reverse.py` / `probe_corrupt_registry.py` / `run_all_scripts.py`
日志: `service_chain.txt` `sentinels_patched.json` `sentinels_pristine.json`
`hybrid_ri.log` `hybrid_u12.log` `u15_{reg_pristine,reg_patched,hybrid}.log`
`reg_{pristine,patched}.json/.log` `official_{pristine,patched}.log`
`patchmatrix_Q.log`

---

### 验证范围确认
- 运行目录 `C:\Users\Tao\Desktop\币安\multi-agent-trader` **只读**：全程未 `cd` 进去执行、
  未修改任何文件（仅用其 `.venv` 解释器）。
- 未读/未复制 `.env` / `data/` / 密钥 / 账户标识；未联网；未下单撤单；未发真实告警；
  未动风险档/白名单/live 开关；未 stop demo 服务。
- 未写 `mat-wt/I` 或他人 worktree；未 push/合并/tag/commit。
- 工作树最终状态: HEAD 仍为 `bf9b304`，`git status --porcelain` 仅 `?? _scratch_Q/`，
  `git diff` 为空（**产品代码零改动**）。
- 全部产物只在 `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15-FU1\R1\q\_scratch_Q\`。

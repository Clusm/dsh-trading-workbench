# U12-BF1 补丁包 — head `178f4fc`

**任务**：修复发布阻断缺陷 `U12-BUILD-F1`（`collect_artifact_files(真实根)` 必抛
`CredentialLeakError` → `IDENTITY_COLLECT_FAILED` → 身份三字段全空 →
`release_readiness` 永远不可能 GO）。

**base**：`7885e6a`　**branch**：`task/U12-BF1`　**worktree**：`mat-wt/W3-bfix`

> 目录名 `178f4fc` = **代码补丁提交**（`git diff 7885e6a..178f4fc` 即本包 01/02）。
> 其后仅追加本补丁包文档（README/evidence），不改动 01/02 的内容。

---

## 0. 结论摘要（先读这段）

本缺陷有**两跳**。本包修第 1 跳并交付两态验收；**第 2 跳在冻结契约
（`src/core/contracts/k01_identity.py`，I 独占范围），本包按硬约束不修**，
但已精确定位、附**可应用提案 diff** 与**"只差这一跳"的端到端证明**。

| 跳 | 位置 | 归属 | 本包状态 |
|---|---|---|---|
| hop 1 | `src/core/runtime_identity.py` `_safe_artifact_name` | U15 | **已修**（`2555f1f`） |
| hop 2 | `src/core/contracts/k01_identity.py:113` `_structured_mapping` → `ensure_no_sensitive_fields` | I | **未修**（越权）；提案见 §5 |

- hop 1 修复后 `collect_artifact_files(<真实根>)` **成功**（125 个文件，含
  `src/core/management_authorization.py`）—— **本任务的核心不变式已达成**。
- `build_release_manifest` 仍记 `IDENTITY_COLLECT_FAILED`：**原因已变**，不再是 U15 的
  守卫，而是契约层把 `artifact_files` 的**路径键**当**字段名**做键名匹配。
- 仅需再应用 §5 的 34 行契约提案（**已实测**），身份三字段即非空、
  `IDENTITY_COLLECT_FAILED` 消失（见 §4.3）。

---

## 1. 改动（2 个文件，+210 / −3）

| 文件 | 行数 | 说明 |
|---|---:|---|
| `src/core/runtime_identity.py` | +68 −3 | 新增独立**文件名**守卫 `_looks_like_credential_file` + 常量；`_safe_artifact_name` 改用之，**不再**用 `SENSITIVE_KEY_MARKERS` 做文件名子串匹配 |
| `tests/acceptance/test_runtime_identity.py` | +145 −0 | 新增 `check_artifact_filename_guard_is_separate`（真实根不变式 + 反向证据 + 非静默登记 hop 2），接入 `main()`；新增 `SENSITIVE_KEY_MARKERS` 导入 |

**`src/core/contracts/common.py` 一字未改**（`SENSITIVE_KEY_MARKERS` 逐字相同）：
`git diff 7885e6a..178f4fc -- src/core/contracts/` 为空。

**取向（语义正确性）**：契约 L1 登记"敏感守卫只查**键名/载荷形态**"
（`docs/contracts/README.md:365`）。把键名标记表当**文件名子串**表用是**过度匹配**
（`"authorization"` 命中普通代码模块 `management_authorization.py`）。修法是给
**文件名**一条**独立**口径，而非放宽共用表。键名口径 `is_sensitive_name` 及其余
4 处调用点（`runtime_identity.py:1220/1312/1403/1428` 附近的配置键名处理）**未动**。

---

## 2. 前后对照：`collect_artifact_files(真实项目根)`

命令：`PYTHONUTF8=1 <venv>/python.exe verify2.py <tree>`
（完整输出 `evidence/01_collect_before_after.txt`）

| | pristine `7885e6a` | patched `178f4fc` |
|---|---|---|
| 默认扫描 `src/**/*.py` | **RAISED** `CredentialLeakError: credential-bearing fields rejected: artifacts.src/core/management_authorization.py` | **OK，125 个文件** |
| 含 `src/core/management_authorization.py` | 否（抛错） | **是** |
| 凭据文件名仍被拒 | 12/15 | **15/15** |
| 普通代码模块被接受 | 0/4 | **4/4** |

> 修复**同时加强**了守卫：pristine 下 `id_rsa`、`id_rsa.pub`、`cert.p12` **根本没被拒**
> （旧守卫只认键名标记），patched 下被新文件名口径拒绝。

---

## 3. 反向证据：凭据文件形态**仍然被拒**（不得修成空转）

`collect_artifact_files(<真实根>, relatives=[...])` 逐条实测，
**15/15 全部 `CredentialLeakError`**（`evidence/01_collect_before_after.txt`）：

`.env`、`.env.local`、`config/.env.production`、`credentials.json`、`id_rsa`、
`id_rsa.pub`、`api_secret.pem`、`secrets.yaml`、`server.key`、`cert.p12`、
`app.keystore`、`db_password.txt`、`tls/private_key.pem`、`auth/passwd`、
`src/api_key_store.py`（既有用例，必须继续成立）。

**独立文件名口径**（`_looks_like_credential_file`）只认三类：
① 凭据容器词 `secret / credential / apikey / api_key / private_key / password /
passwd / passphrase`；② 凭据后缀 `.pem/.key/.p12/.pfx/.ppk/.jks/.keystore/.env`；
③ 前缀 `.env*` 与 `id_rsa/id_dsa/id_ecdsa/id_ed25519`。

---

## 4. 核心验收（特别验证：真实根身份字段）

完整输出：`evidence/02_identity_manifest.txt`

### 4.1 pristine `7885e6a` — 失败在第 1 跳
```
build_release_manifest(真实根) -> identity_blockers = ('IDENTITY_COLLECT_FAILED',)
  runtime_instance_id = ''   effective_config_hash = ''   model_route_hash = ''
collect_runtime_identity RAISED:
  runtime_identity.py:531 _safe_artifact_name -> CredentialLeakError:
  credential-bearing fields rejected: artifacts.src/core/management_authorization.py
```

### 4.2 patched `178f4fc` — **第 1 跳已消解**，失败前进到第 2 跳
```
collect_artifact_files(真实根) OK (125)
collect_runtime_identity RAISED（原因已变）:
  k01_identity.py:250 build_identity -> :119 hash_artifact -> :113 _structured_mapping
  -> common.py:93 ensure_no_sensitive_fields
  -> SensitiveInputError: sensitive keys rejected: src/core/management_authorization.py
build_release_manifest -> identity_blockers = ('IDENTITY_COLLECT_FAILED',)
```
> 注意：U15 的守卫**已经不再**抛错。此处的 `SensitiveInputError` 来自
> **冻结契约**把 `artifact_files` 的**路径键**当**字段名**匹配（§5）。

### 4.3 端到端证明：再补 §5 提案 ⇒ 核心验收通过
在一次性沙箱（`sb_hop2`，**未提交、不进本包**）应用 §5 提案后：

```
build_release_manifest(真实根) ->
  runtime_instance_id   = 'unknown'
  effective_config_hash = '3cdc7708b90952b9179bc10e86d8794f6509ff2425b88ffa7b91c64e9285183e'
  model_route_hash      = 'e3704987bc41a03b4faa0d833856748909d7c03d99b6003b93560c943bf0a0f5'
  identity_blockers     = ('MODEL_ROUTE_MOCK',)      <-- IDENTITY_COLLECT_FAILED 已消失
  IDENTITY_COLLECT_FAILED present: False
  three fields all non-empty: True
```
残余 `MODEL_ROUTE_MOCK` 是 demo 环境模型路由走 mock 的**既有独立阻断项**，
与身份采集缺陷无关。

---

## 5. 第 2 跳（归属 I）— 定位、根因、提案

**根因**：`hash_artifact(files)` 文档即"**相对路径** → 内容"
（`k01_identity.py:118`），却经 `_structured_mapping` 对**路径键**调用
`ensure_no_sensitive_fields` —— 与 hop 1 **完全同类**的过度匹配
（键名标记表被当文件名子串表用）。同一问题也潜在存在于
`hash_dependencies`（同为路径键映射，当前锁文件名恰好不含标记词而未暴露）。

**为何 U15 修不到**：该函数在 `src/core/contracts/` 内，属 I 独占范围
（`docs/dispatch/角色卡.md:17`）；且 `build_identity` 是唯一入口，无绕过参数。
经核，**不存在**仅改 `runtime_identity.py` 即可让身份采集成功的路径
（`hash_artifact` 的哈希值就是 `canonical_hash(dict(files))`，任何键改写都会
改变契约语义 —— 属"绕过"，故不做）。

**提案**（`03_PROPOSED_contract_hop2_NOT_APPLIED.diff`，34 行，**未应用**）：
为文档化为"路径 → 内容"的两个入口引入 `_structured_path_mapping`
（仅做 `ensure_structured_payload` 形态校验 + Mapping 校验，**不**跑键名口径），
`hash_artifact` / `hash_dependencies` 改调它。文件名级凭据守卫**已由 U15
`_safe_artifact_name` 承担**（本包 §3 的 15/15 反向证据），故不产生防护缺口。

> 备选（若 I 希望契约层自带文件名守卫）：把 U15 的凭据容器口径**同源**上提到
> `common.py` 并由两处共用；该口径变更属契约小版本，需 I 决定。

---

## 6. 沙箱复验（解包 base → **立刻 `git init`** → `git apply` 01/02）

`evidence/06_sandbox_suites_and_applycheck.txt`

| 套件（sandbox, patched） | exit |
|---|---:|
| `scripts/check_quality.py`（含 ruff，**未新增 exclude**） | 0 |
| `scripts/smoke_contracts_v3.py` | 0 |
| `tests/acceptance/test_runtime_identity.py` | 0（262 PASS / 0 FAIL） |
| `tests/acceptance/test_control_boundary.py` | 0 |
| `scripts/smoke_entry_readiness.py` | 0 |

**两态可跑**（`evidence/03`、`evidence/04`）：同一个新增用例，
pristine 沙箱 **10 FAIL**（含"真实根默认工件扫描不抛错"、"真实根工件集合包含
`management_authorization.py`"），patched 沙箱 **0 FAIL**。
**未硬编码迁移版本集合**。

---

## 7. 补丁应用（3 种 git 配置）

| 配置 | 01_runtime_identity.diff | 02_acceptance_artifact_guard.diff |
|---|---|---|
| 默认 | exit=0 `Checking patch`=1 `Skipped`=0 | exit=0 `Checking patch`=1 `Skipped`=0 |
| `-c core.autocrlf=false -c core.eol=lf` | exit=0 `Checking patch`=1 `Skipped`=0 | exit=0 `Checking patch`=1 `Skipped`=0 |
| `-c core.autocrlf=true` | exit=0 `Checking patch`=1 `Skipped`=0 | exit=0 `Checking patch`=1 `Skipped`=0 |

---

## 8. 全量离线回归 pristine vs patched（失败集合增量）

`scripts/run_offline_regression.py`，pristine 沙箱 vs patched 沙箱，
完整记录：`evidence/07_offline_regression_delta.txt`。

| | 退出码 | 总判定 |
|---|---|---|
| pristine `7885e6a` | **0** | `OFFLINE REGRESSION OK scripts=85` |
| patched `178f4fc` | **0** | `OFFLINE REGRESSION OK scripts=85` |

失败集合增量（`grep FAIL` 归一化后 `comm`）：

- patched 新增失败（本包引入的新失败）：**∅**
- pristine 独有失败：**∅**

两侧各匹配到 10 行 `FAIL`，**逐字相同**，来源是
`scripts/smoke_u02_strategy_position_manager.py` 的**负向对照 N2**
（故意喂入"重复退出反向开仓"损坏场景并逐条列出 detectors；脚本自报
`PASS=156 FAIL=2`，既有，与本题无关）。

---

## 9. 未尽事项（如实登记）

1. **hop 2 未修**（§5）：`src/core/contracts/k01_identity.py` 属 I 独占范围，
   超出本会话授权（任务硬约束"冻结契约不改"、`角色卡.md:17`）。
   ⇒ **`build_release_manifest` 仍 `IDENTITY_COLLECT_FAILED`，`release_readiness`
   仍不可能 GO**。缺口只差 §5 的 34 行提案，已实测可闭合。
2. **保守残差**：文件名含 `secret` **子串**者一律拒绝（如
   `secretary_roster.py`）。方向 fail-closed，真实 `src/` 树无此名。
   若改"整词匹配"会放过 `mysecret.txt` 这类真凭据名 —— 属削弱防护，故不做。
   已作为用例 ⑧ 显式登记（断言其**被拒**），并写入用例 docstring。
3. 未跑真实运行目录（`multi-agent-trader`）与 demo 服务路径；本包全部证据为离线。
4. 未验证 `service.py:184` 生产启动路径的端到端行为（需 hop 2 落地后另行联调）。

---

## 10. 硬约束遵守声明

- 运行目录 `multi-agent-trader` **只读**；未停 demo 服务；未读/未复制
  `.env`/`data/`/密钥/账户标识。
- **未联网、未下单/撤单、未发真实告警、未动风险档/白名单/live 开关。**
- 未写 `mat-wt/I`；未 push / 未合并 / 未打 tag；全部产物在本工作树内，增量提交
  （`2555f1f` → `203c968` → `178f4fc`）。
- **未改冻结契约**；**未新增 ruff exclude**；U15 验收套件**未削断言**
  （只新增，`main()` 追加一项）。

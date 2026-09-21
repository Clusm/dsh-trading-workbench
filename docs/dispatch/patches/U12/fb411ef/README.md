# U12 补丁包（RC 交付物 + CI 接线）

- 任务：**U12 · RC 集成、故障验收和观察交付**（BUILD 检查点）
- 角色：I / 主会话（U12 BUILD 会话）
- 分支：`task/U12`（worktree `mat-wt/W3-rc`）
- **base：`7885e6a`**（= U13-FU1 集成后的集成树 HEAD）
- **candidate head：`fb411ef`**（本目录名即该短 hash）；其后仅追加本补丁包目录本身
  （`524815c`，**不含产品逻辑、不含 RC 正文变更**），故交付物正文与 `fb411ef` 逐字节一致。
- 契约：`contract-v3.0`
- 性质：**不含任何 `src/**` / `scripts/**` 产品逻辑改动**；只含 RC 交付物文档、独立验收套件、CI 接线。
- 状态：**DRAFT**（未冻结、未部署、未开始观察）

## 1. 内容与 I 的动作

| 文件 | 说明 | I 的动作 |
|---|---|---|
| `01_ci_wire_official_regression.diff` | `.github/workflows/ci.yml`：工作流级 UTF-8 + **新增 U12 RC 验收步** | **apply**（CI 属 I 的集成目标） |
| `docs/releases/unattended_rc/**` | RC 清单/说明 + F01–F25 矩阵 + `observed_status`（md+json）+ 铸造的 `release_manifest.json` | 随分支合入 |
| `tests/acceptance/test_unattended_release.py` | RC 验收套件（两态可跑、独立运行） | 随分支合入 |
| **无** `run_offline_regression.py` 登记补丁 | **本任务不需要**（理由见 §3） | 无 |

## 2. 应用方式

```powershell
# 在 I 的集成树上（已含 fb411ef 之前的 U12 提交）
git apply docs/dispatch/patches/U12/fb411ef/01_ci_wire_official_regression.diff
python scripts/check_quality.py                 # 期望 exit 0
python scripts/run_offline_regression.py        # 期望 exit 0（scripts=85）
python tests/acceptance/test_unattended_release.py  # 期望 exit 0
```

## 3. 为什么**不**需要 `run_offline_regression.py` 的登记补丁

- 本任务新增的验收脚本命名为 **`tests/acceptance/test_unattended_release.py`**
  （U12 独占路径），**未**新建 `scripts/smoke_u12_*.py`。
- `_validate_smoke_manifest()` 只 `glob("scripts/smoke_*.py")`；清单校验的
  `listed` 集合也用 `Path(name).name.startswith("smoke_")` 过滤，故 `tests/` 路径
  **不参与**该门禁。实测本树打印 `SMOKE MANIFEST OK included=81 excluded=1` 并 `exit 0`。
- 因此**不触碰** I 独占的 `scripts/run_offline_regression.py`，避免 `W1-MANIFEST-TRANSIENT`
  那类"脚本先落库而清单未加"的原子性风险。
- RC 验收脚本改由 **CI 显式步骤**运行（见 `01_...diff`）。
  **若 I 希望它并入官方离线入口**，可在 `SCRIPTS` 追加一行
  `"tests/acceptance/test_unattended_release.py"`（非 `smoke_` 前缀，不影响清单校验）；
  该动作**归 I**，本包不代行。

## 4. 证据（全部为 U12 BUILD 会话在**本树自跑**）

| 文件 | 命令 | 结果 |
|---|---|---|
| `evidence/check_quality.log` | `scripts/check_quality.py` | `All checks passed!` / `SECRET SCAN OK ... registered_false_positives=2` / `QUALITY CHECK OK files=295 untracked=0`，**exit 0** |
| `evidence/offline_regression_round2.tail.txt` | `scripts/run_offline_regression.py` | `OFFLINE REGRESSION OK scripts=85`，**exit 0** |
| `evidence/offline_regression_round1_FAIL.tail.txt` | 同上（**首轮**） | **FAIL**：`smoke_u10a_release_package.py` 5 项失败——本 RC 文档的 3 个相对链接指向**尚未落盘**的文件。**门禁真实生效**（详见交付物 README §6.1） |
| `evidence/acceptance_u12_release.log` | `tests/acceptance/test_unattended_release.py` | `U12 UNATTENDED RELEASE ACCEPTANCE PASS=46 FAIL=0`，**exit 0** |
| `evidence/acceptance_all_suites.exitcodes.txt` | 6 个既有 `tests/acceptance/*.py` 逐个运行 | 全部 `EXIT=0`（U04 168/0、U09 78/0、U13 166 checks、U14 262/0、U15 identity/control PASS） |
| `evidence/u12_build_f1_identity.log` | 见 §5 | `U12-BUILD-F1` 的可复跑证据 |

**引用（非本会话自跑）**：F14/F18 的原始演练与修复复验分别引用
`mat-wt/I/docs/dispatch/reviews/U12/drill_F14_F18/REPORT.md` 与
`mat-wt/I/docs/dispatch/reviews/U13-FU1/af76443/Q_r1.md`；矩阵中逐条标注了 `引用`。

## 5. `U12-BUILD-F1`（U12 BUILD 新发现，**台账未登记**，阻断 RELEASE）

**K01 身份采集在真实 `src/` 树上必然失败**：

- `src/core/runtime_identity.py:557` `collect_artifact_files(root)` 默认扫描
  `src/**/*.py`，对每个路径**分量**调用 `is_sensitive_name()`
  （`runtime_identity.py:320`，只做 `lower()` + **子串匹配**）。
- `src/core/contracts/common.py:19` `SENSITIVE_KEY_MARKERS` 含 **`"authorization"`**。
- 文件名 `src/core/management_authorization.py`（U02 交付物）命中 → `CredentialLeakError`。
- `src/core/service.py:184` **生产启动路径**调用 `build_release_manifest(...)` 未传
  `artifact_relatives` → 走默认扫描 → 必然抛错 → 单点降级 `IDENTITY_COLLECT_FAILED`。
- ⇒ `runtime_instance_id`/`effective_config_hash`/`model_route_hash` 全空 ⇒
  `_identity_block()` → `state=NO_DATA / IDENTITY_REF_MISSING` ⇒ K06 `engineering`
  组 `NO_DATA` ⇒ **`release_readiness` 不可能为 GO**。

**为何此前未被发现**：`tests/acceptance/test_runtime_identity.py`（本树 exit 0）
只用**合成临时根**或**显式 `artifact_relatives`**，**从不**对真实 `src/**/*.py`
跑默认扫描 ⇒ U15 验收套件对本缺陷**盲**。

**归属**：既有缺陷（U02 的文件名 + U00 的敏感标记），**非 U12 引入**。
**本包不修**（`src/` 只读约束），已登记为交付物 `README.md` §5.1 + 待裁决 **A13**，
并在 `tests/acceptance/test_unattended_release.py` 第 [8] 节以**两态不变量**钉住
（"身份要么完整，要么**显式**带 `IDENTITY_COLLECT_FAILED`，绝不静默空身份"）。

## 6. 硬约束遵守声明

- 运行目录 `multi-agent-trader` **只读**；未停止 demo 服务；未读/未复制 `.env`/`data/`/密钥/账户标识。
- **未联网、未下单/撤单、未发真实告警、未动风险档/白名单/live 开关。**
- 未写 `mat-wt/I`；未 push/未合并/未打 tag；全部产物在本工作树内，增量提交。
- 发布门禁**未降低**：`DEFAULT_THRESHOLDS` 逐字未改（200/30/7/0.999/120）；
  **未新增 ruff exclude**。

## 7. 未尽事项

1. `U12-BUILD-F1` 未修（§5），阻断 `release_readiness`。
2. 观察门禁 7 天 / 200 候选 / 30 闭合 **未开始**（`observed_status`）。
3. `W1-JOINT-WIRING`（U03×U15 联合接线，S05 验收）与 `U08-U12-WIRING`（`graph.py`
   注入 funnel/ledger）**未执行** ⇒ 执行侧逐票原因/跨进程去重生产不可达。
4. `U09-OUTCOME-VS-LIFECYCLE-COST`、`U09-NET-UNKNOWN-PADDING`、`U13FU1 N1/N2/N3`、
   `U04-R4-FU1`、`U02 _consumed_ids`、`AlertQueue` 无消费者等**待裁决/未修**（见交付物 README §5/§8）。
5. `release_manifest.json` 的 `schema_version=0`（**未提供账本**，不读真实 `data/`），
   部署侧必须重铸。

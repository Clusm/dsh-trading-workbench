# U06a 共享适配补丁（目标 = 集成 HEAD `59660b26`）

> ⚠️ **本目录为 r1 版本，已被 `../a01d088/` 取代（superseded）**：r2 修复了独立审查
> 指出的 P2-1（聚合失败仍报 REFRESHED / 陈旧当新值）与 P2-2（`release_gate.note`
> 只存 JSON 未渲染），并补了 P3-8/P3-9/P3-10/P3-11。**请只应用 `a01d088/` 下的补丁。**
> 保留本目录仅为如实留痕（r1 交付物与审查对象 `ebbe1e9` 对应）。

> 目录名 = 生成这组补丁时 `task/U06a` 的 **head 提交 `cd9c2a4`**（即"导出共享文件
> 改动并还原"那一步的提交）。**前像 = 集成 HEAD** `59660b2`，不是本分支 head：
> 本分支 head 的 `src/intel/service.py` 已按约定还原为集成版本，共享改动只存在于
> 本目录的 `.diff` 中。

## 0. 为什么共享改动不进分支

`src/intel/service.py` 与 `src/dashboard/templates/decisions.html` 都是**共享适配**
文件（任务卡「集成目标（I 合入）」），必须由 I 在集成树上应用，不得随任务分支提交。
上一会话把 `src/intel/service.py` 的改动带进了 WIP 提交 `f0c0e48`；本会话：

1. 以集成 HEAD 版本为前像导出 `src_intel_service.py.diff`；
2. `git checkout 59660b2 -- src/intel/service.py` 还原，提交 `cd9c2a4`。
   还原后 `git diff 59660b2 -- src/intel/service.py` 为 **0 行**（分支不含该文件改动）。

`decisions.html` 在 WIP 提交里**未被改过**，其前像与原像都是集成 HEAD 版本，
因此本补丁是在集成版本上**叠加**我的改动生成的（符合派发规则第 8 条）。

## 1. 补丁清单

| 文件 | 目标文件 | 内容 |
|---|---|---|
| `src_intel_service.py.diff` | `src/intel/service.py` | 影子披露接线：零权重常量、C 未配置隔离、有界研究负载、汇总指标缓存、v2=DEFERRED 披露 |
| `src_dashboard_templates_decisions.html.diff` | `src/dashboard/templates/decisions.html` | 旧口径明确展示（口径 ID/权重恒 0/v2 DEFERRED/不充当执行候选）、A/B/C 组状态、已知缺陷列表；样本表改为研究参考标注 |
| `scripts_smoke_intel_sidecar.py.diff` | `scripts/smoke_intel_sidecar.py` | **必要**：既有共享冒烟断言的是**旧口径**（"B/C 影子组同时记录"、C 组 `signal='unknown'` 计 2 行）。U06a 明确要求"C 未配置不产生无效绩效样本"，该断言必然随口径一起改。不改则本任务在集成树上会让 `run_offline_regression` 红（实测 `AssertionError: B/C影子组同时记录`） |

`src/core/service.py` **无需补丁**：侧车已由既有代码在独立线程
`readonly-intel-sidecar` 上运行（`_intel_loop`），不接 Broker、不调用 LLM，
必达要点③由既有实现满足，本次未改一行（见报告）。

> ⚠️ **偏差声明（请审查者注意）**：`scripts/smoke_intel_sidecar.py` **不在**任务卡
> 「集成目标（I 合入）」的三个文件之列。把它做成补丁是因为：它是**既有共享门禁
> 的一部分**，其断言与 U06a 的必达要点②（C 未配置不产生无效绩效样本）**直接冲突**；
> 不处理就会在集成树上引入一条新失败。除该文件的两处断言外，其 17 条断言逐字未改
> （仍 19 通过 / 0 失败），不削弱任何既有覆盖。

## 2. 应用方式（I 在集成树上执行）

```bash
cd <integration-worktree>
for p in docs/dispatch/patches/U06a/cd9c2a4/*.diff; do
  git -c core.autocrlf=false -c core.eol=lf apply --check "$p" && \
  git -c core.autocrlf=false -c core.eol=lf apply "$p"
done
```

本组补丁**两种 git 配置下均可干净应用**（实测，见 §3）：

- `-c core.autocrlf=false -c core.eol=lf`（集成工作区 `mat-wt/I` 的既有仓库配置）
- 默认配置（本机 system gitconfig `core.autocrlf=true`）

生成时统一用 `git -c core.autocrlf=false -c core.eol=lf diff`，补丁正文全为 LF
（`grep -c $'\r'` 均为 0），不依赖本机行尾配置。

## 3. 可应用性实测（原始输出）

在**沙箱 git 仓库**（前像 = `git show 59660b2:<path>`）中逐条 `--check`：

```text
--- src_intel_service.py.diff ---
cfg A (autocrlf=false,eol=lf) : exit=0
cfg B (default)               : exit=0
--- src_dashboard_templates_decisions.html.diff ---
cfg A (autocrlf=false,eol=lf) : exit=0
cfg B (default)               : exit=0
--- scripts_smoke_intel_sidecar.py.diff ---
cfg A (autocrlf=false,eol=lf) : exit=0
cfg B (default)               : exit=0
```

另在**沙箱**（system `core.autocrlf=true` 的默认配置下工作树为 CRLF）实测：
用 `-c core.autocrlf=false -c core.eol=lf` 应用后，`src/intel/service.py` 与
本分支 WIP 目标内容**逐字节相同**（41628 字节、CRLF 计数 0）。

行尾无损性：把补丁应用到前像后，目标文件 CRLF 计数 = 0（与前像一致），
不产生"整文件噪声 diff"。

## 4. 与既有消费者的兼容性（共享字段先查语义）

| 共享字段 | 既有消费者 | 本补丁的处理 |
|---|---|---|
| `snapshot()["shadow_evaluation"]["events_24h"]` | `decisions.html:139` | **保留键**：改为读 `self._metrics_cache`（= `intel_observability()` 的完整副本），键集不变 |
| `snapshot()["shadow_evaluation"]["evaluations"]` | `decisions.html:168` | **保留键**；新增的 `shadow_metrics.evaluations` 是在其之上加标注的视图（`legacy_metric_view` 用 `**item` 保留全部原键），模板优先取新视图、缺失时回落旧视图 |
| `shadow_decisions.source` | `normalize_shadow_cohort_names`（迁移 10）、`evaluate_shadow_decisions` | **取值不变**（`A_current_baseline` / `B_contract_market` / `C_contract_plus_web3`），本次只是把字面量收敛为常量 |
| `shadow_decisions.execution_weight` | 无聚合消费者 | 恒 `0.0`（原本也写 `0.0`，本次改为常量并加配置冲突登记） |
| `feature_json` | `evaluate_shadow_decisions` 读 `contract_market`/`funding_rate`；`release_evidence` 读 `opportunity_id`/`opportunity_key` | **只追加** `shadow_cohort` 诊断键，不改既有键、不删既有键 |
| `shadow_decisions` 行数 | `release_evidence.py` 的 `directional_candidates`（`signal IN ('bullish','bearish')` 去重） | 本补丁**只减少**行数（未配置/非方向不写），不会凭空增加；且 B/C 的门禁角色在披露中标为 `research_only`，200 候选业务口径由 U09 固定 |

**无 schema 变更**：不新增列、不新增迁移版本，故不涉及"迁移版本集合被硬编码"的既有断言
（派发规则第 5 条）。

## 5. 已知限制

- 汇总指标改为「到期才算」的缓存后，**首次刷新若已超预算**，`shadow_evaluation`
  会是空字典（`events_24h` 显示 0），此时披露对象的 `metrics.state` 为 `UNAVAILABLE`
  以便区分"无数据"与"未计算"。
- `decisions.html` 的改动是**纯展示层**：不改变任何门禁判定、不改变执行权重、
  不新增数据源；所有新字段都在缺失时回落到原有展示。

## 6. 应用后的实测（沙箱，前像 = 集成 HEAD）

在 `git archive <本任务 head>` 的沙箱里应用本目录 3 个补丁后实测：

```text
tests/integration/test_shadow_status.py   exit=0  PASS 239 / FAIL 0   （未应用时 200 / 0）
scripts/smoke_u06a_shadow_isolation.py    exit=0  PASS 46  / FAIL 0   （未应用时 10 / 0）
scripts/smoke_contracts_v3.py             exit=0  PASS 173 / FAIL 0
scripts/smoke_intel_sidecar.py            exit=0  19 通过 / 0 失败    （未改则失败）
scripts/smoke_ui.py                       exit=0  通过 52 / 失败 0
scripts/check_quality.py                  exit=0  QUALITY CHECK OK files=264 untracked=0
scripts/run_offline_regression.py         exit=0  OFFLINE REGRESSION OK scripts=80
```

`run_offline_regression` 的 pristine 基线（同一沙箱、不打补丁）同样是
`OFFLINE REGRESSION OK scripts=80` → **失败集合差集 = ∅**（`scripts=80` 而非 79，
是因为沙箱里把本任务新增的 `scripts/smoke_u06a_shadow_isolation.py` 按清单要求
临时登记了一次；**该登记行必须由 I 加入**，见该脚本 docstring）。

**`/decisions` 真实渲染**（沙箱 TestClient，status 200）命中：
`影子口径` / `LEGACY_ACTIVE` / `配置声明不可抬升` / `v2 固定时点标签 DEFERRED` /
`A/B/C 研究隔离` / `C=unconfigured` / `旧口径已知缺陷（6 条）`；
`可人工评审` 已消失。

> ⚠️ **集成注意**：新增冒烟脚本会让 `run_offline_regression` 的清单校验
> （`_validate_smoke_manifest`）在**登记前**直接 raise
> `RuntimeError: 未登记冒烟脚本: scripts/smoke_u06a_shadow_isolation.py`。
> 必须与登记行**同批原子落地**（`W1-MANIFEST-TRANSIENT`）。

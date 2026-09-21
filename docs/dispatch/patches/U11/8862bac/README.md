# U11-FU1 补丁包（R1 复验 CHANGES_REQUESTED 的修复会话）

- 任务：U11 核心模块渐进拆分（F 线 / EF 线 / 后置 W4+）——**修复轮 FU1**
- 作者分支：`task/U11`，base `03967e9`，R1 head `00a49a8`
- 承载提交（内容）：`8862bac`（`docs(U11): add tests/ to the do-not-touch list, …`）
  - 前置提交 `2561b7b`（`docs(U11): map decision_trace 961-1799 and strategy_funnel 1804-2138 blocks`）
    是**同一轮修复的第一半**（阻塞 1），与本包同属 FU1。
- 目录名 `8862bac` = 承载**内容**的提交，而非最终 HEAD。与 U10b
  （`docs/dispatch/patches/U10b/273892c/`）、U11 R1（`docs/dispatch/patches/U11/00706d5/`）
  同一约定：**按内容提交命名目录**，目录名不随 README/证据刷新而漂移。

> **与 `00706d5/README.md` 的关系**：那一篇是 **R1 提交轮的记录**（当时判
> `CHANGES_REQUESTED`），其"补丁数量 0 / 只写独占路径"的结论**依然成立**；
> 但其中"§3.1 三块""§5 十六项"等描述已被本包取代。**以本包与在产文档为准。**

## 补丁数量：**0**（无需任何共享文件补丁）

本轮**只写独占路径** `docs/重构接口映射.md`，没有触碰任何 `src/`、`scripts/`、
`pyproject.toml`、`docs/文档索引.md` 或其它共享文件。**不存在需要 I 应用的 diff。**

`git diff 03967e9..8862bac -- src/ scripts/ pyproject.toml tests/` = **空**。

## 本包修了什么（对照 R1 的裁决）

### 阻塞 1 —— 映射漏掉半个文件，且自己的验收测不出

| | R1 版 | FU1 版 |
|---|---|---|
| `§3.1` 块数 | 3 块（实际 ≥6），表止于 960 行 | **B1–B13 + B14 门面行**，覆盖 72–1799 全文 |
| `decision_trace` 未映射段 | 961–1799（839 行 = 46.6%，24 个顶层公开定义） | 归入 **B8**（`run_protection_first`）、**B9**（`ModelRouteGuard` 族）、**B10/B11**（`shared_admission`：`Lease`/`LeaseRegistry`/`shared_quota_lease`/`owner_alive`/`lock_path_for`/`credential_alias`）、**B12**（`SourceCache`/`background_refresh`/`RefreshBudget`）、**B13**（`EvidenceLink`/`evidence_links`） |
| `§4.1` 映射表 | 7 行，覆盖 **28/63** 名（V1b 实测 44.4%） | **14 行（M1–M14）**，覆盖 **63/63 = 100%** |
| `strategy_funnel` 未映射段 | 1804–2138（4 名，均在 `__all__`） | **F6** → `core/funnel_explanation.py` |
| `§4.2` 映射表 | 5 行，覆盖 **27/122** 名（V1b 实测 22.1%） | **6 行（F1–F6）**，覆盖 **122/122 = 100%** |
| 出口能否发现漏块 | **不能**（`dir()` 名集合不变，只搬前半照过） | **能**：新增 `§7 V1b`，见下 |

### 阻塞 2 —— `§5` 不动清单里没有任何 `tests/`

- `§5` 新增两行：`tests/integration/*`（12 套）、`tests/acceptance/*`（7 套 + `independent/`），
  写明"**既有测试的 import 与断言一行不改**"。
- 新增 `§5.1`：逐一点名 12 个 integration 套件及其归属任务；
  写明 6+ 个 `smoke_*.py` 是**透传退出码的薄壳**（`smoke_u05_decision_trace.py:4` 原文），
  以及"改 `tests/` 的 import → smoke 仍 exit 0 → V2 静默通过"的自证陷阱；
  引用了仓内先例 `tests/integration/test_research_evidence.py:447`（包含式断言）。
- 新增 `§5.2`：**责任归属** —— `decision_trace` → **U05**、`strategy_funnel` → **U08**、
  `runtime_identity` → **U15**，均须在拆分落地前复跑其既有套件并**书面签字**。
  **R1 通篇未提 U08**，此处补上。
- 新增 `§5.3`：点名 **`U13FU3-INJECTION-TRUST`**（台账 `status: OPEN`、`owner: U12`），
  写清它与"`recovery.py` 绝对不动"的张力及**相容顺序**（先 U12 裁决落地、再 U11 拆分，不得并行）。
- `§6` 把"原任务作者签字"写成**硬前置**（第 4 条）。

### 6 条备注

| 备注 | 落点 |
|---|---|
| C1（V1 口径） | `§7 C1` 专节：双口径（`__all__` 主 + `dir()` 辅，含 61/86、71/158 量化），写明**假红边界**（清门面未用 import → `dir_sha` 变但非回归）与**漏报边界**（`__all__` 以 `_` 开头被 `dir()` 过滤，R1 完全不检查 `__all__`） |
| C2（`107–208` 起点错） | `§3.1` B2 行 + 灰框勘误：真实起点 **78**，`REASON_*` 在 81–89、`REASON_ORDER` 93–103、`REASON_CODES` 104 |
| C3（"81 个"= included 数） | `§5` 改为"顶层共 82 个（清单纳入 81 + 排除 1：`smoke_p0c.py`）" |
| C4（M4） | `§5` 新增 `knowledge/*.json` 行 |
| C5（M5） | `§5` 新增 `scripts/check_quality.py`、`scripts/create_release_package.py` 行 |
| C6（M6） | `§5.3` 专节（见上） |
| 另 M1/M2/M3 | `§5.1`/`§5.2`/`§5` 新增 `docs/策略扫描原因码.md` 行 |

## 新增的出口：`§7 V1b` 映射覆盖率自检（FU1 自证）

R1 的 V1 只回答"拆分后名集合变没变"，**不回答"映射本身覆盖了全文没有"**。
V1b 对每个候选模块取**全部顶层公开定义**（AST）与**文档中反引号点名过的符号**求差，
差额非空 → 退出码 1。可归档版本见本目录 `v1b_mapping_coverage.py.txt`
（后缀 `.txt` 是有意的：**不把可运行 `.py` 放进被 lint 的树**）。

**在 R1 版文档上回放（`git show 00a49a8:docs/重构接口映射.md`）——报红**：

```
core.decision_trace: 顶层公开定义 63，覆盖率 44.4%，漏项 35
core.strategy_funnel: 顶层公开定义 122，覆盖率 22.1%，漏项 95
V1B FAIL 2 个模块有漏项          # 退出码 1
```

**在 FU1 版上实跑——通过**：

```
core.decision_trace: 顶层公开定义 63，覆盖率 100.0%，漏项 0
core.strategy_funnel: 顶层公开定义 122，覆盖率 100.0%，漏项 0
V1B OK 映射覆盖完整              # 退出码 0
```

## 门禁复核（作者实跑，base `03967e9` + FU1）

| 命令 | 结果 |
|---|---|
| `python scripts/run_offline_regression.py` | 退出码 **0**；`SMOKE MANIFEST OK included=81 excluded=1`；`OFFLINE REGRESSION OK scripts=85` |
| `python scripts/check_quality.py` | 退出码 **0**；`QUALITY CHECK OK files=301 untracked=0`（与 base 同值：FU1 只改 `.md` + 一个 `.py.txt`，不增 Python 文件）；**未新增任何 exclude** |
| `python scripts/smoke_u10a_release_package.py` | 退出码 **0**；`BROKEN_LINK` 计数 **0**；`所有相对文档链接可达（独立解析，43 篇 md）` |
| `python scripts/smoke_contracts_v3.py` | 退出码 **0**；`PASS 173 / FAIL 0`，golden 160 逐字未变 |
| `§7 V1`（双口径快照） | 退出码 **0**；4 行与 R1 基线逐字相同，另增 `all_sha` 一列 |
| `§7 V1b`（覆盖率自检） | 退出码 **0**；63/63、122/122 |

**FU1 新引用的三条命令也已实跑**（R1 未跑过它们）：

| 命令 | 结果 |
|---|---|
| `python scripts/smoke_u05_background_refresh_budget.py` | 退出码 **0** |
| `python scripts/smoke_u05_model_route_safety.py` | 退出码 **0** |
| `python tests/integration/test_strategy_funnel.py`（**直跑，不经 smoke**） | 退出码 **0**；`U08 STRATEGY FUNNEL: PASS 208 / FAIL 0` |

**未登记 `scripts/run_offline_regression.py`**：FU1 没有新增任何 `scripts/smoke_u11_*.py`，
也没有移动/改名任何 `smoke_*.py`，故 `SCRIPTS`/`EXCLUDED_SMOKE_SCRIPTS` **一行未改**。

## 应用方式

无补丁可应用。若 I 只想带走在产文档：

```bash
git checkout task/U11 -- docs/重构接口映射.md
```

## 未尽的（如实登记）

1. **本文档仍是映射/设计交付**，`src/` 一行未拆；`§7 V1/V2` 的"拆分后逐行相同"
   仍是**待实施者断言**，非本包已证事实。
2. **V3（磁盘格式不变）仍未实跑**（§8.2），与 R1 相同。
3. **V1b 只验"名字在不在"，不验"块划得对不对"**（§8.8）。
4. **`§5.1` 报出的离线回归缺口**（`test_strategy_funnel.py` 等 3 个 integration 与
   5 个 acceptance 套件不被任何 smoke 调起）是**既有事实**，本包只登记、不修——
   是否把它们接入 `run_offline_regression.py` 属 I 动作（改 `SCRIPTS` 需 I 合入）。

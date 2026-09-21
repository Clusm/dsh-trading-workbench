# Q 独立验收报告 — U10b（旧脚本与历史文档整理，P2 后置线）

- 角色：Q（独立验收，只读验证，未改任何产品代码）
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U10b\R1\q`（detached HEAD @ `0416408`，已验证 == 作者 head）
- base `0706db1` → head `0416408`（8 提交，`git rev-list --count` = 8）
- 解释器：`C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`（无 pytest），全程 `PYTHONUTF8=1`，无联网
- 沙箱（`_scratch_Q/`）：
  - `base/`    = `git archive 0706db1 | tar -x` + `git init` + `git config core.autocrlf false` + `git add -A`（tracked=734）
  - `patched/` = `git archive 0416408 | tar -x` + 同上 + 应用补丁 `01`（tracked=738，补丁已生效）
  - `unpatched/` = `git archive 0416408 | tar -x` + 同上，**不**应用补丁（tracked=738）

> 复现提示（避免误判）：沙箱若只 `git init` 不 `git add -A`，`git ls-files` 为空，
> `smoke_u10a_release_package.py` 会在 `build_release_manifest` 处 `ModuleNotFoundError: core`
> 失败（base 与 patched 同样失败，属环境构造问题，非被测缺陷）。本报告全部结论取自
> `git add -A` 之后的正确构造。

---

## 0. 结论

**PASS-with-notes**（无阻塞项；4 条 note 见 §7）

推翻性尝试的结果：N/M/K 覆盖、删除清单、根深度、断链、发布包、质量门禁、补丁矩阵
**七条结构性主张全部独立复现通过**。唯一实质发现是作者**归档规则 3 的表述与事实不符**
（有现行文档以"内联路径"而非"相对链接"引用被归档文档，补丁未覆盖）—— 详见 §4 与 §7 N1。

---

## 1. 覆盖未减少（N/M/K 前后值）—— 硬证据

命令（两树同一入口，全量跑完）：
`<sandbox>/{base,patched}$ python scripts/run_offline_regression.py`

| 指标 | base (0706db1) | patched (0416408+补丁) | 判定 |
|---|---|---|---|
| `SMOKE MANIFEST OK included=` | **81** | **81** | 未减少 |
| `… excluded=` | **1** | **1** | 未减少 |
| `OFFLINE REGRESSION OK scripts=`（K） | **85** | **85** | 未减少 |
| 进程退出码 | **0** | **0** | 通过 |

输出文件：`_scratch_Q/base_regress2.txt`、`_scratch_Q/patched_regress2.txt`

**逐条核对 `scripts/` 顶层 `smoke_*.py`**：

- base 顶层 `smoke_*.py` = 82 个；patched = 82 个；`diff` 集合**完全相同**（`SMOKE SET IDENTICAL`）。
- 82 = manifest `discovered`；81 登记入 `SCRIPTS` + 1 条 `EXCLUDED_SMOKE_SCRIPTS`
  （`scripts/smoke_p0c.py`，本就排除）→ `_validate_smoke_manifest()` 无 `missing`/`stale`/`overlap`。
- 被移走的 18 个脚本**无一**以 `smoke_` 开头 → 不触碰 manifest 的 `glob("scripts/smoke_*.py")`（非递归）。
- `scripts/run_offline_regression.py` 的 `SCRIPTS` / `EXCLUDED_SMOKE_SCRIPTS` 两树**逐字节相同**（该文件不在 `git diff 0706db1..0416408` 名单内）。
- **结论：无任何 `smoke_*` 被移走；无静默削减回归覆盖。** 作者自述规则被遵守。

---

## 2. 「没有任何能力被静默丢弃」核查

### 2.1 删除清单（`--no-renames`）
```
git diff --no-renames --name-status 0706db1..0416408
```
→ `D=26`、`A=30`；**每个 D 的 basename 都能在 A 里找到同名 counterpart → `UNPAIRED_DELETES: NONE`（26/26 配对，孤儿删除 = 0）**。
多出的 4 个 A = `docs/archive/README.md` + 补丁目录 3 个文本（无对应删除）。与作者"空删除清单"一致。
（脚本 `_scratch_Q/check_moves2.py`、`check_moves3.py`，输出 `check_moves3.txt`）

### 2.2 被移走的 8 篇文档：正文逐字节未改
`docs/*.md` → `docs/archive/*.md`，`diff_lines=0`、CR 计数均为 0 → **R100 成立，正文逐字节相同**。

### 2.3 被移走的 18 个脚本：内容差异**仅**为根深度那一行
用 difflib 在 **EOL 归一化后**逐行比对（`check_moves3.txt`）：
- 16 个脚本：唯一语义差异 = 新增 1 行注释 `# U10b 归档：…` + 根深度表达式由 `parents[1]`→`parents[2]`
  （`cleanup_ghost_orders.py` 两处：L25 sys.path、L36 data 路径）。
- `probe_intel_with_proxy.py`：**零**语义差异（正文里没有 `__file__`）。
- `_js_check.py`：**零**语义差异（正文里没有 `__file__`）。
- 例外（note N3）：3 个脚本同时被 CRLF→LF 归一，故"逐字节相同"对它们**不字面成立**：
  `_js_check.py`(CR 189→0)、`probe_exchange_positions.py`(133→0)、`run_phase0.py`(68→0)。
  EOL 归一化后内容除上述根深度行外完全一致；`pyproject.toml` 有 `[tool.ruff.format] line-ending = "lf"`，属格式归一，无功能影响。

### 2.4 「已核实但不归档」保留清单逐项在位（patched 树实测，22/22 `OK`）
`scripts/diag_algo_orphans.py`、`scripts/run_phase1.py`、`docs/策略扫描原因码.md`、
`export_usdm_incidents.py`、`benchmark_demo_endpoints.py`、`benchmark_runtime_baseline.py`、
`verify_llm_provider.py`、`verify_close_reconcile_live.py`、`start_web_detached.py`、
`run_web_service.py`、6 个 `scripts/test_*.py`、`docs/CHANGES_2026-09-01.md`、
`docs/dual_agent_design.md`、`docs/项目问题盘点…`、`docs/USD-M…`、`docs/WorkBuddy…`、
`docs/决策链路…`、`docs/模拟盘复审…`、`docs/无人值守交付…`、`docs/无人值守方案细审…`、
`docs/测试与验收矩阵…`、`docs/当前状态与剩余工作矩阵…`、`docs/上线前人工检查单…` —— **全部仍在原位**。

保留理由也逐一独立核验：
- `docs/策略扫描原因码.md`：`tests/integration/test_strategy_funnel.py:239` 确实断言该路径存在 ✓
- `diag_algo_orphans.py` 的 lint 契约：`pyproject.toml:12-13` 确为 `"scripts/*.py" = ["E402"]`（`*` 不跨 `/`，移入子目录即失去豁免）✓
- `run_phase1.py` 的字面路径登记：`src/core/runtime_paths.py:640` `ConsumerEntry("legacy_phase1_entry", "scripts/run_phase1.py:41", …)` ✓

### 2.5 规则 1（无运行/质量入口引用）复核
在 patched 树对 18 个脚本名做全仓 grep（`src/ tests/ .github/ scripts/`）→ **0 命中**
（排除脚本自身与 archive 索引）。归档不会从任何入口/import 里"带走"能力。

---

## 3. 根深度修正独立复算（不采信作者 `bad=0`）

方法：逐文件正则取出模块级 `Path(__file__).resolve()…` 表达式，以**新路径**作为 `__file__` 求值，
断言 == 仓库根（`_scratch_Q/patched`）。脚本 `_scratch_Q/rootprobe.py`，输出 `rootprobe.txt`。

- 覆盖：`scripts/diagnostics/*.py`(13) + `scripts/maintenance/*.py`(5) = 18 个文件；
  共 **17 处** `__file__` 表达式（`cleanup_ghost_orders.py` 两处）。
- 结果：**BAD COUNT: 0**（17/17 解析到仓库根，非 `scripts/<子目录>/`）。
- `git grep __file__` 全枚举确认：18 个脚本里的 `__file__` **只有上述 17 处**，无遗漏。

**2 个未改的脚本为什么不用改（独立确认，非采信）**：
- `_js_check.py`：全文**无** `__file__`（只有一行相对路径 `tmp = "scripts/_js_check_run.js"`，与根深度无关）。
- `probe_intel_with_proxy.py`：用 `sys.path.insert(0, "src")`（CWD 相对），依赖"从仓库根执行"，与 `__file__` 无关。
→ **不存在"本该改而没改"的脚本。**

---

## 4. 断链核查

### 4.1 Markdown 相对链接（解析出相对路径 → 检查目标是否存在）
脚本 `_scratch_Q/linkcheck2.py`（剥掉 `:行号` 后缀；扫描 `docs/**/*.md` + `README.md` + `CONTRIBUTING.md`）。

| 树 | md 文件 | 链接 | BROKEN | 说明 |
|---|---|---|---|---|
| base (0706db1) | 123 | 167 | **3** | 3 条为既有断链（见下） |
| head **未应用补丁** | 125 | 192 | **11** | = 既有 3 + **`docs/文档索引.md` 新增 8 条** |
| head **应用补丁** | 125 | 197 | **3** | 与 base 完全一致 |

- 未应用补丁时 `docs/文档索引.md` 的 8 条断链：`双进程双账本改造设计_2026-09-15.md`、
  `上线前核查报告_2026-09-02.md`、`风险事件排查与改善计划_2026-09-03.md`、
  `实盘上线评估与整改计划_2026-09-14.md`、`实盘账户并行接入评估与上线整改清单_2026-09-15.md`、
  `实盘接管阻断诊断_2026-09-16.md`、`架空机制排查_2026-09-02.md`、`UI重构方案_整合版.md` —— **8 条，与作者自述一致**。
- 应用补丁后 **= base 的 3 条既有断链**，U10b 引入的断链 **0 条**。
  3 条既有断链与 U10b 无关：`docs/dispatch/reviews/U02/4856aec/Q_r2.md -> partial`、
  `docs/dispatch/reviews/U10a/10bc4d2/Q_r3.md -> 开发规格书_v2.md`、`-> ../knowledge/策略报告_整理版.md`。
- `docs/contracts/README.md`（冻结目录）：仅 2 条相对链接（`../无人值守交付与多Agent执行规格_2026-09-19.md`、
  `../决策链路专项评估与优化报告_2026-09-19.md`），两目标**均未被归档**、均存在 → **无断链**。
- `src/core/runtime_paths.py`：引用的 `scripts/run_phase1.py`、`scripts/create_recovery_bundle.py`
  **均未被移动** → **无断链**。

### 4.2 补丁后仍存在的「旧路径残留引用」（**N1，本次唯一实质发现**）
全仓扫描（`_scratch_Q/residual.py`）—— 这些是**反引号内联路径**，不是 markdown 相对链接，
因此 `doc_links_resolve` 不会报，但**指向已不存在的路径**：

| 引用方（性质） | 行 | 指向的旧路径 |
|---|---|---|
| `docs/上线前人工检查单_2026-09-16.md`（**现行**） | 132 | `docs/双进程双账本改造设计_2026-09-15.md` §4.5 |
| `docs/上线前人工检查单_2026-09-16.md`（**现行**） | 156 | `docs/实盘账户并行接入评估与上线整改清单_2026-09-15.md` §3.9 |
| `docs/运行与故障恢复手册_2026-09-06.md`（**现行**） | 387 | `docs/双进程双账本改造设计_2026-09-15.md` |
| `docs/无人值守升级任务清单_2026-09-19.json`（**现行**） | 4160 | 同上两篇 |
| `docs/Binance行情接入与418治理_2026-09-14.md`（现行，仅 basename） | 45 | `live_check_query_detail.py` |
| `docs/模拟盘复审与上线优化总方案_2026-09-19.md`（现行，仅 basename） | 209 | `run_phase0.py` |
| `docs/dispatch/patches/U14/{7c430c8,a89eaa5,ad2c57d}/README.md`（历史流程记录） | 77/78 等 | 两篇被归档设计文档 |
| `docs/dispatch/reviews/U01/fb12a38/Q_r3.md`（历史流程记录） | 200-202 | 同上 |
| `docs/dispatch/patches/U02/890bb7c/evidence/…txt`（历史证据） | 4 | `scripts/_js_check.py` |

**这同时证伪了 `docs/archive/README.md` 归档规则 3 的措辞**："只归档出链为 0、且**只被文档索引引用**的文档"。
实测 `docs/双进程双账本改造设计_2026-09-15.md` 与 `docs/实盘账户并行接入评估与上线整改清单_2026-09-15.md`
**仍被至少 3 篇现行文档以路径引用**（规则 3 只对"markdown 相对链接"成立，对内联路径不成立）。
影响面：可在一跳内经 `docs/文档索引.md` / `docs/archive/README.md` 找回，属**文档可导航性**问题，非构建失败。

### 4.3 被归档脚本自身 docstring 的用法行仍写旧路径（N2）
如 `scripts/diagnostics/diag_tool_errors.py:4` 写 `python scripts/diag_tool_errors.py`、
`scripts/maintenance/cleanup_positions_ledger.py:20-22` 等（共 12 个脚本的 docstring）。
这是"正文逐字节不改"的必然结果，属低危可用性提示。

---

## 5. 发布打包器 —— 归档脚本是否还在发布包内（关键怀疑点，已实测）

### 5.1 `scripts/smoke_u10a_release_package.py`（应用补丁后）
```
patched$ python scripts/smoke_u10a_release_package.py
```
- 结果：**37 通过 / 0 失败**，退出码 **0**（沙箱已跟踪文件 738 个）。
- `PASS 所有相对文档链接可达（独立解析，42 篇 md）`（base 为 41 篇；+1 = `docs/archive/README.md`）。
- `PASS 归类清单自证完整（… 未归类=0）` → **无未归类文件、无断链**。
- 对照实验（未应用补丁）：**32 通过 / 5 失败，退出码 1**，报
  `doc_links_resolve: {… BROKEN_LINK: docs/文档索引.md -> … , 'count': 8}`。
  → **独立复现了作者"未应用补丁时分支本身是红的、且红点仅在共享文件"的主张。**
  （输出：`_scratch_Q/u10a_patched.txt`、`u10a_unpatched.txt`）

### 5.2 归档物是否还在发布包内 —— **在，且类别未变（不是静默削减交付物）**
用打包器 CLI 在 base / patched 沙箱**各自真实出包**（runtime + source）后逐成员比对
（`_scratch_Q/pkgprobe.py`、`pkgdiff.py`，输出 `pkgdiff.txt`）：

| 包 | base 成员数 | patched 成员数 | 变化 |
|---|---|---|---|
| `runtime` | 223 | 224 | **+1**（`docs/archive/README.md`） |
| `source`  | 391 | 392 | **+1**（同上） |

- **18 个被归档脚本**：base/patched 在 **runtime 包中均为 0**（`diag_`/`probe_`/`cleanup_`/`verify_`/`repair_`/`live_check`/`run_phase`/`_` 前缀本就被 `SOURCE_ONLY_SCRIPT_PREFIXES` 归为 source-only），
  在 **source 包中 18/18 全部存在**，只是路径由 `scripts/x.py` → `scripts/diagnostics|x.py`。
  → **交付物集合逐项等价，零能力丢失**；移动未改变任何文件的入包类别。
  （因为 `_source_only_script_reason()` 用 `Path(relative).name.startswith(prefix)` 判定**文件名前缀**，与目录无关。）
- **8 篇被归档文档**：`docs/` 属 runtime 类，base/patched 均在 **runtime 包内**，只是路径带 `archive/`；
  patched 的 `docs/archive/README.md` 也随包。→ 文档也未丢。
- runtime/source 两个包的差集只有上述路径重命名与 `+docs/archive/README.md`，**GONE 集合 = 被移动文件的旧路径**，一一对应新路径。

**结论：不存在"归档后脚本不再进发布包"的静默削减。** 作者关于 `SOURCE_ONLY_SCRIPT_PREFIXES`
按文件名前缀归类的说明被证实，且"文件名一字不改"这一约束是真实必要的。

---

## 6. 回归与质量

### 6.1 full delta（失败签名集合，两树全量跑完）
```
FAIL sigs: base=12  patched=12  common=12
NEW_FAIL (only patched): EMPTY
FIXED    (only base)   : EMPTY
```
- 12 条全部来自 `scripts/smoke_u02_strategy_position_manager.py` 的**双态快照**打印段
  （`U02 strategy position manager: PASS=158 FAIL=0 / PASS=156 FAIL=2 / PASS=150 FAIL=8`，
  该冒烟自身输出 `U02 SMOKE OK` 且退出码 0），两树逐行相同 → **既有基线现象，与 U10b 无关**。
- 与作者自述（10 条）差异仅因我把两行 `PASS=… FAIL=8/2` 汇总行也计为签名；核心结论 `NEW_FAIL = ∅` 一致。

### 6.2 `scripts/check_quality.py`
| 树 | 退出码 | 结果 |
|---|---|---|
| base | 0 | `QUALITY CHECK OK files=296 untracked=0`；`SECRET SCAN OK files=655 registered_false_positives=2` |
| patched | 0 | `QUALITY CHECK OK files=296 untracked=0`；`SECRET SCAN OK files=659 registered_false_positives=2` |

- 两树 **均绿**；`files=296` 前后不变。
- **未新增任何 ruff exclude**：`pyproject.toml` 在 `0706db1..0416408` 上 `git diff` 为空、
  两版本 blob **逐字节相同**（`diff` 无输出）；`extend-exclude` 仍为 `[".venv","data","logs"]`。
- 归档的 18 个脚本**仍在质量门禁内**：`git ls-files -- '*.py'` 在两树均为 296 个，
  其中 `scripts/{diagnostics,maintenance}/*.py` = **18**；直接 `ruff check` 两例 → `All checks passed!`。
  （`check_quality.py` 用 `git ls-files -- '*.py'` 全仓匹配，不按目录，故子目录不逃逸。）

### 6.3 补丁矩阵（3 种 git 配置，在 `unpatched/` 上跑 `--check -v`）
| 配置 | 命令 | 输出 | Skipped | rc |
|---|---|---|---|---|
| 默认 | `git apply --check -v` | `Checking patch "docs/\346\226\207…md"...` | **0** | 0 |
| LF | `git -c core.autocrlf=false -c core.eol=lf apply --check -v` | 同上 | **0** | 0 |
| CRLF | `git -c core.autocrlf=true apply --check -v` | 同上 | **0** | 0 |

补充：`docs/文档索引.md` 在 base 与 head **完全相同**（`git diff --quiet` 通过），
故补丁在 base 或 head 上均可 apply（base 树上 `--check` 亦为 `Checking patch`，rc=0）。

---

## 7. Note 清单（无阻塞项；建议但非必须）

- **N1（唯一实质项，建议处理）**：§4.2 列出的**现行文档内联旧路径**未被补丁覆盖
  （`上线前人工检查单_2026-09-16.md:132,156`、`运行与故障恢复手册_2026-09-06.md:387`、
  `无人值守升级任务清单_2026-09-19.json:4160`）。同时 `docs/archive/README.md` **规则 3**
  "只被文档索引引用" 的措辞与实测不符（至少 2 篇被归档文档仍被现行文档以路径引用）。
  最小复现：`grep -rn "docs/双进程双账本改造设计_2026-09-15.md" docs/ --include="*.md" | grep -v archive/`。
  **不阻塞**：markdown 相对链接 0 断链、发布包自检绿、可经归档索引一跳找回；
  但若 R1 判据把"任何旧路径残留"都算断链，则这是唯一候选阻塞项。建议改法：补丁追加这 3 处内联路径
  （或把规则 3 措辞收窄为"markdown 相对链接只被文档索引引用"）。
- **N2**：12 个被归档脚本的 docstring 用法行仍写旧路径（§4.3）。低危。
- **N3**：3 个被移动脚本（`_js_check.py`/`probe_exchange_positions.py`/`run_phase0.py`）
  同时发生 CRLF→LF 归一，故作者"正文逐字节相同（除那一行）"对它们不字面成立；语义为零改动。
- **N4**：`docs/dispatch/**` 历史流程记录里的旧路径引用（U14 patch README、U01 review、U02 evidence）
  属冻结的流程证据，预期保留，不计缺陷。

---

## 8. 我未能验证 / 明确未做的部分

1. **未在真实运行目录 `multi-agent-trader` 上跑任何东西**（只读约束）：所有结论基于
   `git archive` 沙箱。沙箱 `git add -A` 后的 tracked 数与真实检出可能不同
   （真实 `multi-agent-trader` 是一个 275 tracked 的检出，与本任务 base 树的 734 无关）。
2. **未验证 `data/`、`.env`、密钥、账户标识、demo 服务状态**（按硬约束不读/不碰）。
3. **未做联网/下单/撤单/告警/风险档/live 开关**相关任何操作。
4. **未跑 `tests/` 全量**（无 pytest）；`tests/` 只做了"是否引用被移动路径"的静态 grep（0 命中）。
5. **未验证 CI (`ci.yml`) 在 GitHub 上的真实执行**，只静态确认它不引用被移动的 18 个脚本
   与 8 篇文档。
6. **未覆盖 U10b 之外的其它任务/补丁**（如 U14/U01 历史记录里的旧路径）—— 这些被明确视为范畴外。
7. 沙箱 `ruff/py_compile` 会生成 `__pycache__`；这不影响本报告结论（`.gitignore` 已排除，
   打包器只按 `git ls-files` 取件）。

## 9. 证据清单（均在 `_scratch_Q/`）

`base_regress2.txt`、`patched_regress2.txt`、`base_quality.txt`、`patched_quality.txt`、
`u10a_patched.txt`、`u10a_unpatched.txt`、`check_moves2.txt`、`check_moves3.txt`、
`rootprobe.txt`、`linkcheck_patched.txt`/`lc_patched.txt`/`lc_unpatched.txt`/`lc_base.txt`、
`residual.txt`、`pkgdiff.txt`、`failsig3.txt`，以及脚本
`check_moves2.py` `check_moves3.py` `rootprobe.py` `linkcheck2.py` `residual.py`
`pkgprobe.py` `pkgdiff.py` `failsig3.py` `mksb.py`。

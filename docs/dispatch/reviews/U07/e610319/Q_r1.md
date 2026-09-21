# U07 R1 独立验收报告（Q 角色）

- 工作区：`C:/Users/Tao/Desktop/币安/mat-wt/reviews/U07/R1/q`，detached HEAD @ `e610319`（已确认）。
- base `c2f1bab`。作者交付：纯增量 +2497/-0，仅 9 个文件（全部 `A`，无共享文件改动）。
- 沙箱（全部在我自己工作树 `_scratch_Q/` 内，解包后立刻 `git init`）：
  - `sb_pristine` = `git archive c2f1bab`
  - `sb_branch` = `git archive e610319`（作者 head，**不打补丁**）
  - `sb_patched` = 作者 head + 补丁 01 + 补丁 02
- 解释器 `C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`（Python 3.13.12，无 pytest，无 scipy；numpy 2.5.2）。

## 结论：`PASS-with-notes`

**无阻塞项。** 7 项验收要求全部独立复现通过：

1. 门禁不倒性 —— 自建夹具 + 裸 SQL 强塞门禁语义，门禁逐字段不变，两态 60/0、78/0 同值。
2. 补丁 02 **不是削弱**（反向测试：删 U06b 模块 → 断言立刻变红），且改动本身合理（原断言过度约束）；该文件其它断言逐字未动。
3. 统计口径**正确**（91 项自建探针 BAD=0）：`naive_understates` 为真且非空话、独立块手算全吻合、二项检验三方比对一致、自举可重放、费用压力区间外如实拒绝且 `assumption` 披露属实。
4. 配对键按四段配对；7 类剔除逐条命中且披露；总数守恒无静默丢弃。
5. 越权全拦；`decisions.html`/`service.py`/`shadow_status.py` diff 均 0 字节。
6. 全量离线回归两态 `scripts=85`、失败集 ∅、`NEW_FAIL=∅`；质量检查未新增 exclude。
7. 补丁矩阵 2×3 全 `Checking patch`/0 `Skipped`；01 与 02 **顺序无关**。

非阻塞观察 5 条（N1–N5，见 §7），均不改变任何结论。

---

## 1. 发布门禁不倒性（自建夹具，不采信作者 evidence）—— 通过

**自建夹具** `_scratch_Q/probe_gate.py`：自己播 4 条 A 组候选 + 2 条 B + 1 条 C + 61 分钟观测（**与作者夹具数目不同**，避免「同数巧合」)，先算一次 `build_release_readiness_report`，再走产品路径灌统计快照、再算一次；并**额外**用裸 SQL 绕过写入层，把 `counts_toward_release_gate=1 / execution_weight=9.9 / auto_promotion=1` 强行塞进 `shadow_v2_statistics` 再算第三次。

| 状态 | directional_candidates | release_readiness | candidate_source_filter.sources | 报告(去 generated_at)逐字段相同 |
|---|---|---|---|---|
| before | 4 | NO_GO | `['A_current_baseline']` | — |
| after 产品路径写入 | 4 | NO_GO | `['A_current_baseline']` | True |
| after 裸 SQL 强塞门禁语义 | 4 | NO_GO | `['A_current_baseline']` | True |

- 裸 SQL 强塞 `counts=1/weight=9.9` **仍然改不动门禁** → 门禁根本不读这张表（不是靠写入层拦）。
- `sb_patched`：产品写入 `rows=1`、重放 `rows=0`（幂等成立）；`sb_pristine`/`sb_branch`：表不存在（`OperationalError`），报告同样逐字段不变。
- 静态检查：`git diff c2f1bab..e610319 -- src/intel/shadow_status.py src/core/release_evidence.py` = **0 字节**；`grep -rn shadow_v2 src/core/` **零命中**（`src/core/` 下的 `statistics` 命中只有 `portfolio_policy.py` / `strategy_evaluation.py` 的 stdlib `import statistics`，与 U07 无关）。
- 复跑两态同值：`smoke_u09_release_evidence.py` = **60 通过 / 0 失败**、`tests/acceptance/test_release_evidence_v2.py` = **78 通过 / 0 失败**，在 `sb_pristine` / `sb_branch` / `sb_patched` **三态完全同值**。

## 2. 补丁 02 反向测试与裁决 —— **不是削弱**，改动合理

**反向测试（A：字面删除）**：`sb_lit` = patched 树删掉 `src/intel/shadow_v2/labels.py` → 跑 `tests/integration/test_shadow_labels.py` **exit=1（红）**。但红在 `ModuleNotFoundError`（`__init__.py` 顶部 import 链断了），未跑到断言，证据强度不足。

**反向测试（B：隔离断言本人）**：`sb_iso` 把 `labels.py` 改名 `labels_impl.py` 并把 `__init__.py` / `statistics.py` 的导入路径同步改指，使 import 链保持可用而 `glob("*.py")` 确实少一个 U06b 模块；再用驱动只调用 `test_module_boundaries()`：

```
=== 14. 模块边界（纯函数、无网络/DB/LLM） ===
  FAIL  独占路径四个模块齐备（其他任务在同包新增的模块不视为缺失）  ['labels.py']
TARGETED RESULT: PASS=3 FAIL=1 FAILURES=['独占路径四个模块齐备…']
```

**裁决：删掉任一 U06b 模块断言立刻变红 → 补丁 02 不是「削弱」**。而且新写法还在失败信息里点出**缺哪个模块**（`['labels.py']`），比原「集合相等」只回显全量列表更有诊断力。

**其它断言一字未改**：`git apply` 补丁 02 后对该文件的完整 diff = **+8/-4，唯一 hunk 只动了 `check("独占路径四个模块齐备"…)` 这一处调用**（`forbidden` 扫描、真实日期扫描、`U06b 四模块` 语义等**逐字未动**）。该文件在 `c2f1bab..e610319` 分支内**根本不在变更清单里**（补丁是分支外未提交载体）。

**改动本身合理性：合理。** 原 `==` 断言把「同包内由其它任务新增的合法模块」也判红——属**过度约束**（谁后加文件谁跑红）。新 `expected <= present` 保留了「U06b 四模块必须存在」的全部约束力（见上反向测试），只去掉了「不得有别人模块」这一跨任务耦合。实测旁证：`sb_branch`（不打补丁）`PASS=99 FAIL=1`，唯一失败即此条；`sb_patched` `PASS=100 FAIL=0`。

## 3. 统计口径核验（自建探针 `_scratch_Q/probe_stats.py`，91 项，**BAD=0**）

### 3.1 `naive_understates` 为真不是空话 —— 通过
构造同一标的相邻目标时刻（间隔 60s、窗口 300s，强重叠）2 块 × 3 对，块内净差同号：
```
diffs = [0.5,0.5,0.5,-0.5,-0.5,-0.5]  block_means = [0.5,-0.5]
naive_se = 0.223607   cluster_se = 0.500000   (cluster/naive = 2.236)
朴素区间半宽 = 0.4383   独立块自举区间全宽 = 1.0000
naive_understates = True ; independent_blocks = 2 ; naive n=6 / cluster n=2
```
朴素 iid 区间**确实显著窄于**独立块自举区间（半宽 0.438 vs 全宽 1.0）。
近似独立样本（6 对各自成块）：`naive_understates = False`，`|naive_se - cluster_se| = 0.0`（两者完全一致，因为块规模全为 1 时块均值 == 配对差本身）。**两者接近的预期成立。**

### 3.2 独立块切分：手算 vs 实现 —— 全部吻合
手算依据：窗口 `[k*I, k*I+300]`（horizon=5min=300s），合并判据 `next.start <= prev.end + gap`，**按标的各自计算**。

| 用例 | 手算 (blocks, largest, state) | 实现 |
|---|---|---|
| I=600>300 分离 ×4 | (4, 1, INDEPENDENT) | 同 |
| I=300==300 相接 ×4 | (1, 4, OVERLAPPING) | 同 |
| I=200<300 链式 ×4 | (1, 4, OVERLAPPING) | 同 |
| I=400>300 ×5 | (5, 1, INDEPENDENT) | 同 |
| 跨标的 ETH/SOL 各 ×3 @300 | (2, 3, OVERLAPPING) | 同 |
| I=600 但 gap=300 | (1, 3, OVERLAPPING) | 同 |
| I=600 但 gap=299 | (3, 1, INDEPENDENT) | 同 |
| overlap_ratio（4 行 1 块） | 0.75 | 0.75 |
| 空集 | INSUFFICIENT 且 ratio=None | 同 |

`by_symbol` 计数、`observations` 守恒亦逐项吻合。

### 3.3 精确二项符号检验：独立复算 —— 通过
用 `math.comb` 精确整数独立实现 `min(1, 2*Σ_{k≥tail} C(n,k)/2^n)`，并对 n≤16 做 **2^n 暴力枚举**（枚举全部等概率符号串，统计偏离度 ≥ 观测偏离度的比例）三方比对：

| (正,负) | 实现 p | 独立公式 | 暴力枚举 |
|---|---|---|---|
| 6/0 | 0.03125 | 0.03125 | 0.03125 |
| 5/1 | 0.21875 | 0.21875 | 0.21875 |
| 8/2 | 0.109375 | 0.109375 | 0.109375 |
| 10/0 | 0.001953125 | 同 | 同 |
| 7/3 | 0.34375 | 同 | 同 |
| 9/1 | 0.021484375 | 同 | 同 |
| 15/5 | 0.0413894653 | 同 | —（n>16 未枚举） |
| 3/3 | 1.0 | 1.0 | 1.0 |

**边界行为合理且不崩**：`n=0`（全 0 差 / 空输入）→ `p_value=None` + `zeros` 如实披露（不拿 1.0 冒充）；全正/全负 → `2*(1/2^n)`；`n=1` → `p=1.0`（不可能显著）；`n=2` 1/1 分裂 → 加倍法得 1.5 被 `min(1.0,·)` 正确截断为 1.0（与精确检验一致）。

### 3.4 自举可重放 —— 通过
同种子两次 `bootstrap_interval` **逐字段相等**（含 `digest` 64 位十六进制完全相同）；换种子、换输入、换 `config_fingerprint` → `digest` 均变化；报告同输入 `digest()` 相同、输入变化则变化。

### 3.5 费用压力 —— 通过
- **插值只在区间内有效**：因 `net(m) = gross - (fee+slip)*m - extra - funding` 对 m **线性**，相邻点线性插值与解析解**精确相等**：gross=0.3→2.142857143、0.16→1.142857143、0.14→1.0、0.42→3.0，全部与手算解析解 `gross/(fee+slip)` 一致到 1e-9。
- **区间外如实拒绝（不外推）**：gross=0.3 但只给 multipliers=(2.5,3)（解析解 2.1428 在下界之外）→ `None`；gross=0.5 只给 (2.5,3)（解析解 3.571 在上界之外）→ `None`；厚利 1.0% 在 (1,2,3) 内无跨越 → `None`。
- **`assumption` 如实**：压力点 `paired_interval.assumption == "iid_pairs"`（3 个点全部），确实**未做分块自举**；而 `paired_statistics` 的 `cluster.assumption == "independent_blocks"`。`cost_assumption_note` 明写「不是真实成交成本」。

## 4. 配对与剔除 —— 通过
- **配对键**：`opportunity_key` = 实验身份 + 标的 + 目标时刻 + horizon；实测**换 symbol / horizon / version / target_at 任一即换键**，同参数必同键（键串逐段可见）。
- **7 类剔除逐条构造，全部命中且被披露**（每类恰好 1 条，均 0 配对）：

| 原因 | 我的构造 | `drop_counts()` |
|---|---|---|
| `MISSING_TREATMENT_ARM` | 处理臂无行 | `{该原因: 1}` |
| `MISSING_CONTROL_ARM` | 对照臂无行 | `{该原因: 1}` |
| `TREATMENT_NOT_LABELED` | 处理臂窗口无价 | `{该原因: 1}` |
| `CONTROL_NOT_LABELED` | 对照臂窗口无价 | `{该原因: 1}` |
| `SIGNAL_MISMATCH` | bullish vs bearish | `{该原因: 1}` |
| `ENTRY_DIVERGENT` | 参考价 100.0 vs 100.5（≈49.75bps>0 容忍） | `{该原因: 1}` |
| `DUPLICATE_OPPORTUNITY` | 同机会处理臂两行 | `{该原因: 1}` |

- **总数守恒**：混合 4 类剔除 + 2 对成功，`len(pairs)+len(dropped) == len(唯一机会键)`（2+4==6），且**每条 dropped 都带 `reason` 与 `opportunity`**，无静默丢弃。
- **入场参考价反推**：`implied_entry_price` 在 ref∈{100.0, 1.005, 0.00012345, 43210.5} × {bullish,bearish} 8 组下**精确还原参考价**（相对误差 <1e-9），方向符号与量级均正确。

## 5. 越权 —— 通过
- `assert_gate_neutral` 对 3 类污染样本**全部拒绝**（`counts_toward_release_gate=True`、`execution_weight=0.5`、`caliber=LEGACY_CALIBER_ID`），且 `build_statistics_report` 入口同样拒绝（不是只在末端拦）。
- `promotion_guard()` 三项归零：`auto_promotion=False, execution_promotion_allowed=False, counts_toward_release_gate=False, execution_weight=0.0`；`to_rows()` 落库行同样 `(False, 0.0, False)`。
- 补丁 01 的写入层是**强制**写死 0/0.0/0（不看调用方传参）——我用裸 SQL 绕过时门禁仍不变（见 §1），双保险成立。
- `decisions.html` / `src/intel/service.py` / `src/intel/shadow_status.py` 的 `c2f1bab..e610319` diff 均 **0 字节**。

## 6. 回归 / full delta 表 / 门禁 / 补丁矩阵

### 6.1 full delta 表（沙箱 `git archive` + 立刻 `git init`）

| 项 | `sb_pristine`(c2f1bab) | `sb_patched`(head+01+02) | 备注 |
|---|---|---|---|
| `scripts/run_offline_regression.py` | `OFFLINE REGRESSION OK scripts=85`，exit=0 | **同左**，exit=0 | 执行脚本数均 = 85 |
| `SMOKE MANIFEST` | `included=81 excluded=1` | `included=81 excluded=1` | 同 |
| **失败脚本集合** | **∅** | **∅** | 两态输出归一化后残差仅时间戳/临时路径 |
| **NEW_FAIL = patched ∖ pristine** | — | **∅** | 无新增失败 |
| `scripts/check_quality.py` | `QUALITY CHECK OK files=301 untracked=0`，exit=0 | `OK files=303`，exit=0 | +2 = 新增 2 个 .py；**未新增任何 exclude**（脚本本身 0 字节 diff，exclude 行逐字相同，`pyproject.toml` 0 字节 diff） |
| `scripts/smoke_contracts_v3.py` | `PASS 173 / FAIL 0` | `PASS 173 / FAIL 0` | 同 |
| `scripts/smoke_u06a_shadow_isolation.py` | `PASS 52 / FAIL 0` | `PASS 52 / FAIL 0` | 同 |
| `scripts/smoke_u09_release_evidence.py` | `60 通过 / 0 失败` | `60 通过 / 0 失败` | 同 |
| `tests/acceptance/test_release_evidence_v2.py` | `78 通过 / 0 失败` | `78 通过 / 0 失败` | 同 |
| `tests/integration/test_shadow_labels.py` | `PASS=100 FAIL=0` | `PASS=100 FAIL=0` | 见 `sb_branch`：不打补丁 = `PASS=99 FAIL=1` |
| `tests/integration/test_shadow_statistics.py` | 不存在 | `PASS=127 FAIL=0` | `sb_branch`（无补丁）= `PASS=120 FAIL=0` |

补充（作者 head `sb_branch`，不打补丁）：labels `PASS=99 FAIL=1`、statistics `PASS=120 FAIL=0`、contracts 173/0、u06a 52/0、u09 60/0、u09 验收 78/0。

**回归日志中唯一两态不同的实质内容**（均非失败）：打包冒烟的 `沙箱已跟踪文件 746 → 755`（+9，恰好等于本任务新增的 9 个文件）与 `运行必需文件全部入包（应有 155 → 156 个）`（+1 = `src/intel/shadow_v2/statistics.py` 属 `src/` 必需项）。该自检在**两态均 PASS**（156 与 155 都通过），属预期增量。

另注：`smoke_u02_strategy_position_manager.py` 内部打印 `PASS=156 FAIL=2` / `PASS=150 FAIL=8` —— 那是脚本**自带的负向对照 N1/N2**（故意喂坏夹具证明验收器会拒绝），**两态逐字相同**，随后 `N1 OK / N2 OK / U02 SMOKE OK`，不是失败脚本。

### 6.2 补丁矩阵（每个 `.diff` × 3 种 git 配置）

| 配置 | 01 | 02 | `Checking patch` 行数 | `Skipped` |
|---|---|---|---|---|
| 默认 | `CHECK OK` | `CHECK OK` | 1 / 1 | 0 / 0 |
| `-c core.autocrlf=false -c core.eol=lf` | OK | OK | 1 / 1 | 0 / 0 |
| `-c core.autocrlf=true` | OK | OK | 1 / 1 | 0 / 0 |

实际 `git apply -v` 均 `Applied patch … cleanly.`

### 6.3 集成顺序

**01 与 02 可任意顺序应用**（实测：`sb_patched` 先 01 后 02、`sb_patched_rev` 先 02 后 01，两者都 `Applied … cleanly`）。原因：两个补丁文件集**不相交**（01 → `src/audit/database.py`；02 → `tests/integration/test_shadow_labels.py`），无 hunk 上下文重叠、无先后依赖。**结论：顺序无关，无需声明依赖。**

> 注意（给 I）：补丁 01 的 hunk 插入点紧邻 U06b 的 `shadow_v2_labels` / `shadow_v2_coverage`，02 的 hunk 上下文是 `test_module_boundaries` 的模块集断言。若别的任务也动这两处，需按集成版本重出 diff。

## 7. 未能验证的部分 / 非阻塞观察

### 未能验证（如实登记）
1. **真实市场数据下的统计有效性**：全部核验基于合成夹具与解析构造。未验证真实影子样本上 `naive_understates` / 分块 / 费用压力的**经济含义**（本任务也要求不联网、不读 `data/`，无法做）。
2. **统计快照表在长时运行/并发下的行为**：只做了单进程内的幂等与重放；未做多进程并发写同一 digest 的争用测试。
3. **`decisions.html` 渲染**：`0` 字节 diff 已确认未改，但因此也**没有**可验证的 UI 呈现（作者 README 已如实登记「未做 UI」）。
4. **作者沙箱的等价性**：我没有复现作者的沙箱路径与文件名，只复现了结论（数值同值）。作者 evidence 文本本身未被我采信为证据。
5. **scipy 交叉验证**：环境无 scipy，二项检验改用「`math.comb` 精确独立实现 + 2^n 暴力枚举」三方比对（n≤16）；n>16 只有精确公式比对，无第三方库。
6. `check_quality.py` 的 `untracked=0` 是在我自己的沙箱内（已 `git init` 并 commit）成立；未在原工作区跑。

### 非阻塞观察（不构成阻塞项）
- **N1. `ENTRY_DIVERGENT` 的原因标签在一种情形下不够精确。** 当两臂信号不是 `bullish`/`bearish`（反推入场价返回 `None`）时，即使调用方显式传 `require_matching_entry=False`，该对**仍会被剔除**并记为 `ENTRY_DIVERGENT`。行为是 fail-closed（可接受，且被披露、计入守恒），但「算不出入场价」与「入场价确实分了歧」是两回事，原因码可再细分（如 `ENTRY_UNVERIFIABLE`）。实测：`signal="neutral"` → `pairs=0, drops={'ENTRY_DIVERGENT': 1}`（`require_matching_entry=False` 与默认**同结果**）。
- **N2. 费用压力点的区间用的是配对级 iid 自举，未做分块。** 作者已在 `assumption="iid_pairs"` 与 README「未做」中如实标注（我逐点核对 3 个压力点全部为 `iid_pairs`），**披露属实**。但同一批配对在 `paired_statistics` 里已识别出重叠块（`independent_blocks`），压力点却退回 iid —— 若重叠严重，压力点的区间会像 naive 一样偏窄。属可改进项，非缺陷（因为已显式标注）。
- **N3. `paired_statistics` 里 `block_means` 的成员查找是 O(n·|keys|)**（`item.opportunity in set(block.keys())` 在内层每次重建集合）。样本量大时是性能问题，不影响正确性。
- **N4. `naive_understates` 是「朴素 SE < 块 SE」的严格不等式**，而非「是否存在重叠块」。语义正确（它断言的是「低估」而不是「有重叠」），但**块存在而块内负相关/块均值方差偏大时该标志会为 False**；下游若把它当「无重叠」用会误读。文档已说明以 `cluster` 为准。
- **N5. 补丁 01 末尾 `}` 与 `def intel_observability` 之间无空行** —— 已核实这是**仓库既有风格**（`sb_pristine` 同一位置同样无空行），非本补丁引入；`ruff check` 通过，`check_quality.py` 通过。

## 8. 报告文件路径与可复现产物

- 本报告：`C:/Users/Tao/Desktop/币安/mat-wt/reviews/U07/R1/q/_scratch_Q/Q_U07_r1.md`
- 自建探针（可复跑）：
  - `_scratch_Q/probe_gate.py` —— 门禁不变性（4 条 A + 2 B + 1 C，产品路径写入 + 裸 SQL 强塞）
  - `_scratch_Q/probe_stats.py` —— 统计口径 91 项（§3/§4/§5），`BAD=0`
  - `_scratch_Q/probe_keys.py` —— 配对键敏感性与反推入场价
  - `_scratch_Q/driver_boundary.py` —— 补丁 02 隔离反向测试驱动
- 沙箱：`_scratch_Q/{sb_pristine,sb_branch,sb_patched,sb_patched_rev,sb_lit,sb_iso,sb_diffchk}`
- 原始输出：`_scratch_Q/logs/`（`reg_pristine.out` / `reg_patched.out` / `probe_stats.out` / `gate_*.json` / `quality_*.out` / `iso_boundary.out` / `lit_del.out`）

复跑命令（解释器固定）：

```bash
export PYTHONUTF8=1; PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
S="C:/Users/Tao/Desktop/币安/mat-wt/reviews/U07/R1/q/_scratch_Q"
"$PY" "$S/probe_stats.py" "$S/sb_patched"     # 期望 TOTAL CASES=91 BAD=0
"$PY" "$S/probe_gate.py"  "$S/sb_patched"     # 期望三态 full_stripped identical=True
"$PY" "$S/driver_boundary.py" "$S/sb_iso"     # 期望 FAIL=1（缺 labels.py）
```

### 硬约束遵守声明
未读/未复制 `.env`/`data/`/密钥/账户标识；未联网；未下单撤单；未发真实告警；未改风险档/白名单/live 开关；`C:/Users/Tao/Desktop/币安/multi-agent-trader` 只读未改；未写 `mat-wt/I` 或他人 worktree；未 commit/push/合并/打 tag。所有产物只写在 `reviews/U07/R1/q/_scratch_Q/` 内。

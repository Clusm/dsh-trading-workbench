# Q_U06b_r1 — U06b 独立验收（Q 角色，只验证/只报告）

- 工作区: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U06b\R1\q` @ **8ae0f65**（detached，作者 head；已确认）
- base: **0706db1**
- 解释器: `C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`（无 pytest），全程 `PYTHONUTF8=1`
- 沙箱: 我的工作树内 `_scratch_Q/sandbox/{pristine,author,patched,pristine_p02only}`，均由
  `git archive <rev> | tar -x` + 立即 `git init` 得到。未触碰 `mat-wt/I` 或他人 worktree，未提交/push。
- 作者 `evidence/release_gate_invariance.txt` **未被采信**；第 1 条全部自建夹具复现（脚本
  `_scratch_Q/t1_gate_invariance.py`，作者脚本从未执行）。

## 0. 结论

**PASS-with-notes**

无阻塞项。8 条核心主张全部独立复现通过；3 条非阻塞发现（纯风格 1、环境侧 CRLF 1、
死代码 1）已在 §8.4 记录，均不影响门禁与运行行为。**没有任何一条推翻成功**——
包括最关键的"发布门禁未被影响"。

集成顺序声明（必须先合 `shadow_v2/` 再打补丁）经独立复现**为真且必要**（§8.3）。

| # | 主张 | 结论 |
|---|---|---|
| 1 | 发布门禁未被影响（v2 不进分母、shadow_status 口径未动、结论不变） | **PASS**（独立复现） |
| 2 | F11：晚 480s → OVER_TOLERANCE，不退回 legacy、不静默丢弃 | PASS |
| 3 | 无未来信息回填 / LeakageError | PASS |
| 4 | 版本隔离：同版本标签不可改写 | PASS |
| 5 | 时钟可信度 F17（相对增量、整批 UNTRUSTED） | PASS |
| 6 | 不越界（shadow_status/schema 版本/ruff exclude/pyproject） | PASS |
| 7 | v2 未接入采集循环，补丁只加只读方法 | PASS |
| 8 | 回归 NEW_FAIL=∅ / check_quality / 补丁矩阵 / 集成顺序 | PASS（见 §8 表） |

## 1. 门禁不倒性独立复现（重点）

自建夹具 `_scratch_Q/t1_gate_invariance.py`：A/B/C 三类 source 的 `shadow_decisions`
（A 3 条方向 + A 2 条 non-directional + A 1 条 NO_EDGE + B 5 条 + C 4 条）+ 3 条完整
闭合生命周期 + 61 分钟 healthy 观察窗口，跑 `build_release_readiness_report` 两次
（灌 v2 标签前后）：

```
命令: python t1_gate_invariance.py <sandbox>/patched        exit=0
基线   directional_candidates = 3   release_readiness = NO_GO
       excluded_by_reason = {'non_directional_signal': 2, 'research_only_source': 9, 'no_edge_declared': 1}
       shadow_source_rows = {'A_current_baseline': 6, 'B_contract_market': 5, 'C_contract_plus_web3': 4}
写入 50 条 v2 标签 -> 实际新增 50 行
灌满后 directional_candidates = 3   release_readiness = NO_GO
```

- ① **v2 标签不进任何门禁分母**：3 → 3 不变；`shadow_excluded_by_reason` 无 v2 贡献；
  额外再灌 500 条 v2 缺失行后 `directional_candidates` 仍为 3、`release_readiness` 仍 NO_GO。
  根因已核实：`build_release_readiness_report` 只查 `shadow_decisions` /
  `position_lifecycles` / `trade_outcomes` / `release_observations` / `risk_incidents` /
  `order_outbox`；`grep -rn shadow_v2 src/core/` = **0 命中**（门禁模块根本不引 v2）。
- ② **`intel.shadow_status.release_gate_role` 口径未被改动**（实测）：
  ```
  git diff 0706db1..8ae0f65 -- src/intel/shadow_status.py   exit=0, 输出 0 字节
  blob: 0706db1:src/intel/shadow_status.py = afa53766be
        8ae0f65:src/intel/shadow_status.py = afa53766be   -> IDENTICAL
  src/core/release_evidence.py 同样 IDENTICAL (c146b89c75)
  ```
- ③ 门禁结论不变：
  ```
  PASS  门禁报告（除 generated_at）逐字相同
  PASS  gates 逐字相同
  PASS  blocking_item_count 不变
  PASS  release_readiness NO_GO -> NO_GO 不变
  ```
- 附加反证（我自己加的越权测试）：把 `counts_toward_release_gate=True` 传给
  `log_shadow_v2_labels`，落库仍为 0（50/50 全 0）——**落库层强制归零生效**。

## 2. F11 反例语义（晚 480s）

`decision = T`，`target = T+300s`，唯一价格样本在 `T+780s`（晚 8 分钟），
容差默认 90s，`now = T+840s`：

```
PASS  判定为 MISSING（不是 LABELED，即未退回 legacy 口径）
PASS  缺失原因 = OVER_TOLERANCE
PASS  该行未被静默丢弃：结果对象字段齐备且带 note
PASS  coverage_report.unexplained == 0
PASS  逐原因计数可见 OVER_TOLERANCE=1
PASS  变体（目标两侧都有样本 + 780s 长空洞）= DATA_GAP，仍是 MISSING
PASS  对照：偏差 +30s（容差内）-> LABELED      # 证明不是"一律判超容差"
```

说明：目标两侧都有样本时按设计落 `DATA_GAP`（断流）而非 `OVER_TOLERANCE`，
两者都是 `MISSING` + 逐原因留痕 + `unexplained==0`，符合"不静默丢弃"。

## 3. 无未来信息回填

```
PASS  assert_no_future 抛 LeakageError（异常类型正确）
      LeakageError: 1 条样本晚于截止时刻 2026-05-04T12:05:00+00:00，违反『无未来信息回填』
PASS  now < window_close 且存在"目标时刻之后"的样本 -> PENDING（不用未来价回填）
PASS  PENDING 行 exit_price 为 None
PASS  退出价用容差内样本
PASS  path_samples == 3（entry + [decided,target) 内 1 点 + exit），未来点未混入路径
PASS  MFE 只反映容差内退出价：=400，不含目标之后的更晚尖峰
PASS  追加目标+600s 的极端尖峰(10000)后 path_samples/MFE 不变
PASS  samples_before(target) 排除 target 之后的样本
```

取价路径严格 `[decided_at, target)`：`_path_metrics` 先 `samples_before(cutoff)` 再按
`decided_at <= t < target` 过滤，并对路径再跑一次 `assert_no_future`。

## 4. 版本隔离

```
PASS  首次写入新增 1 行
PASS  同键再次写入（内容不同）新增 0 行（INSERT OR IGNORE）
PASS  表内仍只有 1 行
PASS  旧值未被覆盖：state 仍为 LABELED, gross 仍 11.0
PASS  换 experiment_key 才允许新增（改口径必须换键）
PASS  四段隔离键逐字段参与：ExperimentIdentity.key()
PASS  不同 horizon 产生不同键
```

主键 `(decision_id, horizon_minutes, experiment_key)`；`experiment_key` =
`version|release_id|config_fingerprint|horizon_model`(四段 `key()` 拼成)。

## 5. 时钟可信度 F17

```
PASS  首次快照（previous=None）-> STABLE
PASS  墙钟回拨 1h -> UNTRUSTED (wall_rolled_back)
PASS  monotonic 倒退 100s -> UNTRUSTED (monotonic_regressed)
PASS  boot_id 变化 -> UNTRUSTED (boot_id_changed)
PASS  墙钟跑在 monotonic 前 60s -> UNTRUSTED (wall_ahead_of_monotonic)
PASS  正常推进 30s -> STABLE（不误报）
PASS  同一相对回拨在 1970/2026/2099 判定一致（不含绝对日期）
PASS  时区表示（+08:00）不影响相对判定：正常推进 30s -> STABLE
PASS  跨 UTC 日切（23:59:50->00:00:30，跨度 40s）-> STABLE（不因日切误报）
PASS  sampler.py 无硬编码绝对日期字面量（正则 \d{4}-\d{2}-\d{2} 0 命中）
PASS  时钟不可信 -> 整批落 CLOCK_UNTRUSTED（无一 LABELED）
PASS  整批 CLOCK_UNTRUSTED 的覆盖报告 unexplained == 0
PASS  时钟正常 -> 同一批全 LABELED（对照）
```

判定完全基于两次快照的 `monotonic`/`wall` **差值**与 `boot_id` 比较，无绝对日期。

## 6. 不越界（逐项实测）

```
git diff 0706db1..8ae0f65 -- src/intel/shadow_status.py       0 字节  (exit=0)
git diff 0706db1..8ae0f65 -- pyproject.toml                   0 字节  (exit=0)
git diff 0706db1..8ae0f65 -- src/core/release_evidence.py     0 字节  (exit=0)
git diff --name-status 0706db1..8ae0f65  -> 11 个条目全部为 "A"（新增），0 删除 0 修改
```

| 项 | 结果 |
|---|---|
| `shadow_status.py` 未被改 | 是（blob `afa53766be` 前后相同） |
| 新增 `schema_migrations` 版本号 | 否；`current_schema_version()` pristine=**17** / patched=**17** |
| 写 v2 前后迁移集合 | 不变（`[1..17]`，n=17，`v0 == v1` True） |
| 新增 ruff exclude | 否（`pyproject.toml` 未动；`extend-exclude = [".venv","data","logs"]` 原样） |
| `pyproject.toml` | 未动（blob `7480aef224` 相同） |

`shadow_v2_labels` 表按 `CREATE TABLE IF NOT EXISTS` 随 SCHEMA 落地，不占版本号
（实测：pristine 无该表 / patched 有该表，两者 `current_schema_version()` 都是 17）。

## 7. 只读接线

`src/intel/service.py` 补丁实测 diff（`diff --strip-trailing-cr pristine patched`）只有两处：

1. `from intel import shadow_status` → `from intel import shadow_status, shadow_v2`（第 24 行）
2. 新增方法 `shadow_v2_status()`（第 152-170 行）

全文 `grep -n shadow_v2 src/intel/service.py` 只命中上述 4 行（import + def + 两处调用）。
`shadow_v2` 在补丁后全仓（除 `src/intel/shadow_v2/` 包自身）只被
`audit/database.py` 与 `intel/service.py` 引用：

- `database.py`：建表 + `log/fetch/coverage` 三个方法（**不触** `shadow_decisions`/`shadow_outcomes`）
- `service.py`：`shadow_v2_status()` 只读

`_refresh` / `snapshot()` / 采集热路径**无** v2 调用；`SHADOW_V2_ENABLED_BY_DEFAULT = False`，
`runtime_status()` 的 `enabled` 只有在配置节显式 `shadow_v2.enabled=true` 时才为真，
且 `counts_toward_release_gate`/`execution_weight` 恒为 `False`/`0.0`。

## 8. 回归 / 门禁 / 补丁矩阵 / 集成顺序

### 8.1 全量离线回归 pristine vs patched

沙箱：`_scratch_Q/sandbox/pristine`（`git archive 0706db1`）与 `_scratch_Q/sandbox/patched`
（`git archive 8ae0f65` + 两个补丁），两者都解包后**立刻 `git init`**。
runner 用 `subprocess.run(..., check=True)`，任一脚本非零退出即中止并返回非零。

| 项 | pristine | patched |
|---|---|---|
| `run_offline_regression.py` 末行 | `OFFLINE REGRESSION OK scripts=85` | `OFFLINE REGRESSION OK scripts=85` |
| 退出码 | **0** | **0** |
| **失败脚本集合** | **∅** | **∅** |
| `SMOKE MANIFEST OK` | included=81 excluded=1 | included=81 excluded=1 |
| `smoke_contracts_v3.py` | PASS 173 / FAIL 0 | PASS 173 / FAIL 0 |
| `smoke_u06a_shadow_isolation.py` | PASS 52 / FAIL 0 | PASS 52 / FAIL 0 |
| `scripts/smoke_u09_release_evidence.py` | 60 通过 / 0 失败 | 60 通过 / 0 失败 |
| `tests/acceptance/test_release_evidence_v2.py` | 78 通过 / 0 失败 (exit 0) | 78 通过 / 0 失败 (exit 0) |
| `tests/integration/test_shadow_labels.py`（作者套件） | 未打共享补丁 **89 PASS / 0 FAIL** (exit 0) | 打完 **100 PASS / 0 FAIL** (exit 0) |
| `scripts/check_quality.py` | `QUALITY CHECK OK files=296 untracked=0` (exit 0) | `QUALITY CHECK OK files=301 untracked=0` (exit 0) |

**NEW_FAIL = ∅**（两树失败脚本集合均为空集，非"都有同一批失败"）。

补充核验（避免把"两树同样失败"读成"没新增失败"）：

- 两树的 `smoke_contracts_v3` / `smoke_u06a_shadow_isolation` / `smoke_u09_release_evidence`
  计数**逐字相同**（上方表内值取自各自日志实际输出）。
- `tests/acceptance/test_release_evidence_v2.py` 两树日志 `diff` **仅第 1 行临时目录名不同**
  （`u09-acceptance-gcf2thzr` vs `u09-acceptance-vctnrg5t`），其余逐字节相同 → 证明"同值"。
- `smoke_u02_strategy_position_manager.py` 内部有 12 行以 `FAIL ` 开头的行（负向用例自述），
  两树均为 `PASS=156 FAIL=2`，且该脚本段落**逐字节相同**（13452 字节 vs 13452 字节），
  脚本自身 exit=0 → 属既有负向用例输出，**不是**新增失败。
- patched 的 `files=301` = pristine `296` + 本任务新增 5 个 Python 文件
  （`shadow_v2/{__init__,models,sampler,labels}.py` + `tests/integration/test_shadow_labels.py`），
  数量自洽。

### 8.2 补丁矩阵（2 补丁 × 3 种 git 配置）

命令：`git -c ... apply --check --verbose <patch>`（在 `git archive 8ae0f65` 解包 + `git init` 的树上）

| 配置 | 01 database.diff | 02 service.diff |
|---|---|---|
| 默认 | exit=0, Checking=1, **Skipped=0**, error=0 | exit=0, Checking=1, **Skipped=0**, error=0 |
| `-c core.autocrlf=false -c core.eol=lf` | exit=0, Checking=1, **Skipped=0**, error=0 | exit=0, Checking=1, **Skipped=0**, error=0 |
| `-c core.autocrlf=true` | exit=0, Checking=1, **Skipped=0**, error=0 | exit=0, Checking=1, **Skipped=0**, error=0 |

6/6 全通过。

### 8.3 集成顺序核对（作者称"必须先合 shadow_v2 再打补丁"）

独立复现：把 `02_intel_service_shadow_v2_readonly_status.diff` **单独**打到 pristine 树：

```
ImportError: cannot import name 'shadow_v2' from 'intel'
  File ".../src/intel/__init__.py", line 3, in <module>
    from .service import ReadOnlyIntelService
  File ".../src/intel/service.py", line 24, in <module>
    from intel import shadow_status, shadow_v2
exit=1
```

同树先合入 `src/intel/shadow_v2/` 后再打两个补丁：`import intel.service` -> `IMPORT OK`（exit=0）。

**结论：作者的集成顺序声明为真且必要**，`service.py` 是包级 `__init__` 传递导入，
所以补丁 02 早于 `shadow_v2/` 落地会让整个 `intel` 包 import 失败。

### 8.4 非阻塞发现（notes）

- **N1（纯风格）**：补丁 02 在 `shadow_v2_status()` 的 `return status` 与
  `def record_liquidation` 之间**没有空行**（类内方法应隔一空行）。因
  `pyproject.toml` 的 ruff `select = ["E4","E7","E9","F"]` 不含 E3xx，`check_quality.py`
  不会报。不阻塞，建议合并前顺手补一行。
- **N3（死代码）**：`sampler.clock_verdict_for_window()` 已定义但**全仓无调用方、也未导出**
  （`grep -rn clock_verdict_for_window src tests scripts` 只命中定义行本身）。
  `build_batch` 走的是 `clock_guard()`。不影响行为，但属未接线代码，建议删除或接线。
  （顺带：正因为走 `clock_guard`，时钟不可信时**整批**落 `CLOCK_UNTRUSTED`，含尚未到期
  的请求——与"整批"要求一致，但与 `clock_verdict_for_window` 的"只看窗口覆盖"语义不同。）
- **N2（环境侧）**：在 `core.autocrlf=true` 的 Windows 环境下 `git apply` 会把被打补丁的
  **整个文件**转成 CRLF（worktree 侧；提交时 git 会归一化回 LF）。本次沙箱即出现该现象
  （`service.py` 813 → 813+19 行，全文件 CRLF）。**不影响** `--check` 通过、不影响导入与
  测试；但集成时若直接拿 worktree 产物做字节比较会看到全文件差异，需知悉。

### 8.5 只读接线运行期实测（补 §7）

构造 `ReadOnlyIntelService(db=patched_db, config={"symbols":["BTCUSDT"]}, symbols=["BTCUSDT"])`
并调用 `shadow_v2_status()`（脚本 `_scratch_Q/t7_service.py`，exit=0）：

```
shadow_v2_status.enabled = False | enabled_by_default = False
counts_toward_release_gate = False | execution_weight = 0.0
first_release_v2_labels = "DEFERRED"
stored_coverage = {schema, decided=0, labeled=0, missing=0, coverage=None, by_state={}, ...}
snapshot() 键集合因调用 shadow_v2_status 而改变: False
snapshot() 内容完全相同: True          # 调用前后逐字节一致
```

即：默认关闭、只读、不污染 `snapshot()`、缺数据时不编造 0（覆盖率给 `None`）。

## 9. 未能验证的部分

- 未做**真实数据**上的门禁不倒性验证：全程合成 SQLite，未读 `data/`、未碰运行库
  （硬约束）。因此"在真实 demo 库上灌 v2 标签结论亦不变"这一点只由代码路径
  （门禁不引用 v2 表）与合成夹具支持，未在真库上观察。
- 未验证**运行期真实时钟**行为（墙钟回拨/monotonic 跳变只在纯函数层构造快照复现，
  未在真实主机上调时钟，也不应这么做）。`clock_verdict_for_window()` 有实现但
  `build_batch` 走的是 `clock_guard()`；本任务未验证二者的接线差异是否有意为之，
  仅确认 `build_batch` 的整批 CLOCK_UNTRUSTED 行为正确。
- 未验证 v2 **采集调度**是否存在（作者已如实登记"未接入采集循环"），因此
  "长期运行下 v2 覆盖率是否会退化"不在本次范围。
- 未验证 U07 配对统计消费 v2 表的行为（后续任务）。
- 未做跨平台（Linux/macOS）验证，仅在 Windows + 指定 venv 下运行。
- 未验证 `scripts/smoke_u06b_*.py`：作者未登记该脚本（验收全在
  `tests/integration/test_shadow_labels.py`），故无此项可跑。

## 附录：Q 侧复现命令（全部在 Q 工作树内，可重跑）

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
Q="C:/Users/Tao/Desktop/币安/mat-wt/reviews/U06b/R1/q"
export PYTHONUTF8=1
cd "$Q"

# 沙箱
git archive 0706db1 | tar -x -C "$Q/_scratch_Q/sandbox/pristine" && (cd "$Q/_scratch_Q/sandbox/pristine" && git init -q . && git add -A)
git archive 8ae0f65 | tar -x -C "$Q/_scratch_Q/sandbox/author"   && (cd "$Q/_scratch_Q/sandbox/author"   && git init -q . && git add -A)
cp -r "$Q/_scratch_Q/sandbox/author" "$Q/_scratch_Q/sandbox/patched"
(cd "$Q/_scratch_Q/sandbox/patched" && git apply "$Q/docs/dispatch/patches/U06b/0f1f21c/01_audit_database_shadow_v2_labels.diff" \
                                        && git apply "$Q/docs/dispatch/patches/U06b/0f1f21c/02_intel_service_shadow_v2_readonly_status.diff")

# 第 1 条（门禁不倒性，自建夹具）—— exit 0
"$PY" _scratch_Q/t1_gate_invariance.py "$Q/_scratch_Q/sandbox/patched"
# 第 2-5 条（F11 / 未来信息 / 版本隔离 / 时钟）—— exit 0
"$PY" _scratch_Q/t2_semantics.py      "$Q/_scratch_Q/sandbox/patched"
# 第 6 条（schema 版本）—— pristine=17(无表) / patched=17(有表)
"$PY" _scratch_Q/t6_schema.py "$Q/_scratch_Q/sandbox/pristine" pristine
"$PY" _scratch_Q/t6_schema.py "$Q/_scratch_Q/sandbox/patched"  patched
# 第 7 条（只读接线运行期）
"$PY" _scratch_Q/t7_service.py "$Q/_scratch_Q/sandbox/patched"

# 第 8 条（两树全量回归 + 质量门禁）
(cd "$Q/_scratch_Q/sandbox/pristine" && "$PY" scripts/run_offline_regression.py) | tail -2
(cd "$Q/_scratch_Q/sandbox/patched"  && "$PY" scripts/run_offline_regression.py) | tail -2
(cd "$Q/_scratch_Q/sandbox/pristine" && "$PY" scripts/check_quality.py) | tail -1
(cd "$Q/_scratch_Q/sandbox/patched"  && "$PY" scripts/check_quality.py) | tail -1
```

产物：`_scratch_Q/{t1_out,t2_out,t6,t7,t1_gate_invariance,t2_semantics,parse_reg}.py`
与 `_scratch_Q/{reg_pristine,reg_patched,qc_pristine,qc_patched,sl_*,rev2_*}.txt`。

---
报告文件：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U06b\R1\q\_scratch_Q\Q_U06b_r1.md`

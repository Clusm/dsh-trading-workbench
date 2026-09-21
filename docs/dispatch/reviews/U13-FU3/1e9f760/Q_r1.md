# Q 独立复验报告 — U13-FU3 (R1)  【独立复验，未改任何产品代码】

- 工作区: `C:/Users/Tao/Desktop/币安/mat-wt/reviews/U13-FU3/R1/q` @ `1e9f760` (detached)
- 解释器: `C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`（PYTHONUTF8=1，无 pytest）
- 三棵树（`git archive` 解包到 `_scratch_Q/trees/`）:
  `pristine=7885e6a`、`fu2=2ac0d6f`、`fix=5651531`
- 全部产物只在本工作树 `_scratch_Q/`；未 push/合并/tag/提交；未读 `.env`/`data`/密钥；未联网/下单/告警。

---

## 1. 结论

**`PASS-with-notes`** —— **无阻塞项**。U13-FU2 的可达放松面（非 ENTRY 未收口单绕过门禁）
在本轮修复态**已收掉**，且方向为**只可能更严**；N1/N3/绕过/`complete_reconcile` 全部不回归；
两棵树 full delta `NEW_FAIL = ∅`。

Notes（均为"不影响本轮放行、但应登记"的边界，非缺陷）:

- **N-1（设计面，非回归）**：门禁 `before_entry(facts=...)` 把**注入事实当作权威**、
  **跳过自己那次账本读取**（`src/core/ops/recovery.py:1024-1028`）。因此"安全"完全依赖
  **所有调用方都注入诚实事实**。修复树里唯一的生产注入点是 `service.py:988`
  （经 `_recovery_gate_ledger_facts`，已验证正确），所以**当前无可达放松**；但这是
  一条**脆弱信任边界**：任何未来的注入方少报即放松。建议（非本轮要求）门禁改为
  `max(注入, 自读)` 做纵深防御。`probe_bypass.py` B9 用手写 zeros 字典演示了这条边界。
- **N-2（登记边界①裁决：安全）**：三列计数**不按 `environment` 过滤** —— 与门禁
  `ledger_reconcile_facts` **同口径**（门禁也无 environment 谓词）。实测：本进程
  `environment=paper` 时，`live`/`demo` 环境的未收口单**同样计入** → **过阻断**
  （更严），方向安全。见 `logs/env_fix.txt`。
- **N-3（登记边界②裁决：安全）**：`_ledger_outbox_counts()` 读不出时退回
  `gate.live_reconcile_facts()`（多开一次只读连接）。实测该退路方向安全：
  退回后若门禁读得"有未收口"→阻断；读得干净→放行（此时账本确为空）；读 `None`
  /`not_applicable`→三元组全 1 / 原样透传 → 门禁 `EVIDENCE_UNVERIFIED` 阻断。
  另测**长开连接不产生陈旧读**（外部连接提交的 UNKNOWN 行被本进程实读看见）。
  见 `logs/stale_fix.txt`。
- **N-4（pre-existing，非 FU3 引入）**："账本没有 `order_outbox` 表"的保护（`not_applicable`
  → `EVIDENCE_UNVERIFIED`）在**服务打开该库后失效**：`AuditDatabase` 会物化空表，
  此后裸形态与注入形态都放行（空表=确为干净）。pristine/FU2/fix 三态一致，无相对回退。
  见 `logs/cr_{pristine,fu2,fix}.txt` 的 (a)/(b)。
- **N-5（口径观察，实践不可达）**：`status` 匹配是**大小写敏感**（`status='UNKNOWN'`），
  而 `purpose` 是 `UPPER(...)='ENTRY'`。即 `status='unknown'`(小写) 两列都不数。
  门禁与 `_ledger_outbox_counts` **完全一致**（矩阵已验证），故非 FU3 问题；且
  `OrderOutbox` 只写大写状态（`prepare`/`_update`/`fill revision`），实践不可达。

---

## 2. 【必做】三方对照 —— Q 原探针 `[C] unknown_exit`

命令（cwd = `_scratch_Q/`；探针 = 上一轮 Q 原文件，**未改一行**）:

```bash
python probe_n2.py trees/pristine     # exit=1
python probe_n2.py trees/fu2          # exit=1
python probe_n2.py trees/fix          # exit=0
```

| 树 | `[C]` 启动期 `gate_allowed` | `control.state` | `set_control('normal')` | 探针 | exit |
|---|---|---|---|---|---|
| pristine `7885e6a` | `True`（就绪快照字段；见下） | **`close_only`** | **`ok=False` 被拒** | 7/3 | 1 |
| FU2 `2ac0d6f` | `True`（`GATE_CLEARED`） | **`normal`（放松）** | **`ok=True` 成功（放松）** | 7/3 | 1 |
| **fix `5651531`** | **`False`（`RECONCILE_REQUIRED_BEFORE_ENTRY`）** | **`close_only`** | **`ok=False` 被拒** | **10/0** | **0** |

原始行（`logs/n2_*.txt`）:
- `fu2`: `C 结果: gate_allowed=True codes=['GATE_CLEARED'] control=normal set_control={'ok': True, ...}`
- `fix` : `C 结果: gate_allowed=False codes=['RECONCILE_REQUIRED_BEFORE_ENTRY'] control=close_only set_control={'ok': False, ...}`

**裁决**：修复态与 pristine 的**有效阻断结论一致**（`close_only` + 拒绝回 `normal`），
且在**第三条通道**（冷启动就绪快照 `recovery_gate_allowed`）上比 pristine **更严**
（pristine `True` → fix `False`）。C1 是 pristine 上"同源但此前未修的第三处"（作者已在
§7.1 登记），故 pristine C1 的 FAIL 属**基线如此**，不是修复态引入。`[clean]` 对照两态均放行、不误伤。

---

## 3. 【最重要】还剩哪些放松面 —— purpose × status 矩阵

### 3a. 逐格比对（110 格）：门禁 vs 新计数
探针 `_scratch_Q/probe_matrix.py`（自建）。对每个 `(purpose,status)` 单行组合，比较
`ledger_reconcile_facts(db)`（门禁自己会数到的行）与 `TradingService._ledger_outbox_counts(shim)`
（修复新增的实读）三列**是否逐格一致**。

- purpose 覆盖: `ENTRY/entry/Entry/EXIT/exit/PROTECTION/PROTECTIVE/''/'ENTRY '/' ENTRY'/Other`（11）
- status 覆盖: `UNKNOWN/unknown/SUBMITTING/ACKNOWLEDGED/NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/''`（10）
- 结果: **矩阵格数=110，不一致=0，放松格=0**（`logs/matrix_fix.txt`）。

补充探针 `probe_ledgerfacts.py` 直接跑**接线处的真实无参调用** `_recovery_gate_ledger_facts(None)`：
- pristine: **8 个放松格**（`unknown_orders`/`submitting_orders` 恒 0）
- FU2     : **8 个放松格**（同上，且被接线 → 可达）
- **fix   : 0 个放松格**（`logs/lf_{pristine,fu2,fix}.txt`）

### 3b. 全链路（注入 vs 裸）
`probe_matrix.py` 建模门禁 `CLEARED`+全零声明事实，比 `before_entry(facts=None)` 与
`before_entry(facts=svc)`：**24/24 组合两态结论相同，0 处"注入放行/裸阻断"**。
（`ENTRY×(NEW/ACKNOWLEDGED/PARTIALLY_FILLED)` 与 `非ENTRY×(UNKNOWN/SUBMITTING)` 均两态一致阻断；
`非ENTRY/FILLED` 等真干净格两态一致放行。）

**结论：没有找到任何一格修复态比 pristine 松。** 唯一"注入可放松"的形态是**手写事实
绕过服务**（B9），属门禁的信任边界（N-1），非接线放松。

---

## 4. 下界"只增不减" + 两条已登记边界的裁决

`probe_bounds.py`（`logs/bounds_fix.txt`）：**13 通过 / 0 失败**。
- 少报（全 0）/漏报（缺键）/不传（`None`）/负数/非整数 → 结果**一律取真实账本**（更严）。
- 超报（99）/部分超报 → 结果**取形参**（更严）。即 `result = max(live, declared) >= live`。
- 读不出 → 退回 `gate.live_reconcile_facts()`：有未收口→阻断；干净→放行；`not_applicable`
  →**原样透传**（门禁判 `EVIDENCE_UNVERIFIED` 阻断）；`None`→三元组全 1 阻断。

边界①（不按 environment 过滤）：**同口径 + 方向安全（过阻断）**，见 §1 N-2。
边界②（读不出退回门禁读取）：**方向安全**，见 §1 N-3。另验证
`settings.db_file == runtime_paths.db_file == gate.ledger_file()` 在 **5 种配置**
（baseline / `MTA_STATE_ROOT` / `MTA_PROFILE=demo` / 二者组合 / `MTA_PROFILE=live`）
下**全部同文件**（`logs/pathsync_fix.txt`）→ 注入事实取自门禁同一账本，无"读错库"放松。

---

## 5. N1 / N3 / 绕过 / `complete_reconcile` 回归

| 探针 | fix 结果 | exit | 日志 |
|---|---|---|---|
| `probe_n1.py`（10 场景，3 处 fail-open 翻转） | **11 通过 / 0 失败** | 0 | `logs/probe_n1_fix.txt` |
| `probe_n3.py`（doc vs code） | **6 通过 / 0 失败** | 0 | `logs/probe_n3_fix.txt` |
| `probe_bypass.py`（9 组绕过） | **13 通过 / 0 失败** | 0 | `logs/probe_bypass_fix.txt` |

N1 三处翻转（`[none]`/`[no_outbox]`/`[injected:not_applicable]` → `EVIDENCE_UNVERIFIED`）逐字保留
（`logs/probe_n1_fix.txt` S1/S2/S7）。

`src/core/ops/recovery.py` **FU3 未改**：`git diff 2ac0d6f 5651531 -- src/core/ops/recovery.py`
**为空**（FU3 只改 `src/core/service.py` + `scripts/ops/verify_u13f1_recovery_gate.py`）。
（注意：`7885e6a..5651531` 下该文件有 +37/−8，但那是 **FU2** 的改动，非 FU3。）

**`complete_reconcile` 口子复测（`probe_cr.py`）**：自声明 `CLEARED`（账本 `not_applicable`）后
- (a) 裸 `before_entry()` → **阻断 `EVIDENCE_UNVERIFIED`**（fix 成立；pristine 放行=fail-open 旧态）
- (b) 服务物化 schema 后裸读 → 放行（空表=干净，**非注入特有**，pristine/FU2/fix 一致）
- (c) FU3 事实路径注入 → 与 (b) 相同（**不比裸松**）
- 状态位确实被写脏（`state=CLEARED`）—— 与作者自述一致。`logs/cr_fix.txt`

---

## 6. full delta + 门禁 + 补丁矩阵 / 集成顺序

### 6a. 沙箱 full delta（pristine vs patched，git init 后逐脚本）
（清单 AST 读入各树自己的 `scripts/run_offline_regression.py`；`EXECUTION_MODE=paper`，
清 `MTA_PROFILE`/`MTA_STATE_ROOT`/`MTA_DATA_ROOT`，`PYTHONUTF8=1`）

| 项 | 结果 |
|---|---|
| 脚本集合 | 85 / 85，两棵树同一集合（`set==set` True） |
| pristine 非零 | 0 / 85（wall 321s） |
| patched 非零 | 0 / 85（wall 324s） |
| **NEW_FAIL（0→≠0）** | **∅** |
| **FIXED（≠0→0）** | **∅** |
| BOTH_NONZERO | **∅** |

TSV: `logs/reg_pristine.tsv` / `logs/reg_patched.tsv`。

### 6b. 受影响套件（pristine / patched 均 exit 0）
| 脚本 | 结果 |
|---|---|
| `check_quality.py` | 两树均 `QUALITY CHECK OK files=295 untracked=0`；`pyproject.toml` 无改动、**未新增任何 ruff exclude / per-file-ignores** |
| `smoke_contracts_v3.py` | 两树均 `PASS 173 / FAIL 0` |
| `tests/acceptance/test_supervision.py` | 两树 exit 0；`git diff 2ac0d6f 5651531 -- tests/...` 为空（本轮零改动） |
| `smoke_u13_supervision.py` | pristine `39/0`；patched `58/0`，`U13 SMOKE OK steps=4` |

### 6c. 官方验证器（扩展脚本）三方
同一份**扩展**验证器（fix 树脚本）跑三态：pristine **47/11** exit=1、FU2 **54/4** exit=1、
fix **58/0** exit=0 —— **与作者自述逐字一致**。FU2 的 4 条失败**全在 `[unknown_exit]`**
（含"启动收紧 close_only"、"set_control 被拒"、"冷启动快照阻断"、"unknown_orders>=1"）。
日志 `logs/verifier_ext_{pristine,fu2}.log`、`logs/verifier_fix.log`。

### 6d. 补丁矩阵 与 集成顺序（自建 `patchtest/`，每 base 独立 `git init` 避免误用外层仓库）
`git apply --check --verbose`，3 种 git 配置（默认 / `-c core.autocrlf=false -c core.eol=lf` /
`-c core.autocrlf=true`）：

| 补丁 | base `7885e6a` | base `2ac0d6f` |
|---|---|---|
| FU3 `01_service_ledger_outbox_counts.diff` | **3/3 `rc=0 Checking=1 Skipped=0`** | **3/3 `rc=0 Checking=1 Skipped=0`** |
| FU3 `02_verify_gate_n2_unknown_exit.diff` | 3/3 `rc=1`（缺 FU2 验证器小节，**预期**） | 3/3 `rc=0 Checking=1 Skipped=0` |

→ **`01` 在两种 base × 三配置 = 6/6 干净**，与作者自述一致。

**集成顺序**（独立复现）：
1. `7885e6a` + FU2 `01/02/03` → `src/core/service.py` 与 `2ac0d6f` **逐字节一致**；
2. 再 + FU3 `01` → `service.py` 与 `5651531` **逐字节一致**（`diff --strip-trailing-cr`）；
3. `2ac0d6f` + FU3 `01` + `02` → `service.py` 与验证器脚本均与 `5651531` **逐字节一致**。
→ 集成顺序 = **U13-FU2 补丁 01–03 → FU3 `01`**，结果逐字节等于 `5651531`。`02` 为分支独占文件，
集成树不带（本轮由 `git archive 5651531` 带入，故 patched 沙箱验证器已是扩展版）。

---

## 7. 我未能验证 / 未覆盖的部分

1. **真实生产账本**：Q 硬约束禁止读真实 `data/`；全部证据来自系统临时目录的**合成状态根**
   与假 broker。真实 `order_outbox` 行上的放松面未实测。
2. **真实交易所 / 真实断电撕裂写 / 真实多账户凭据 / SCM 双 profile 注册**：未做。
3. **真实多环境共用同一 `agent_trader.sqlite`**：边界①只在合成行上判定方向（同口径+过阻断），
   未在真实多环境库上实测额外阻断面。
4. **边界②退路的性能**：只验方向安全与正确性，未做高频路径的性能计量。
5. **`02` 补丁在 `7885e6a` 上的 rc=1** 我按"缺 FU2 验证器小节"归因（与 diff hunk 上下文一致），
   未逐 hunk 展开证明其唯一性。
6. **门禁信任边界（N-1）的"应否改成 max(注入,自读)"** 属设计决策，超出本轮"验证"范围，未评估。
7. 未验证任何 P2/P3（含 U13-FU2 复验 N4）。

---

## 8. 证据文件（全在本工作树内）

- 探针: `_scratch_Q/{probe_n1,probe_n2,probe_n3,probe_bypass,probe_matrix,probe_ledgerfacts,probe_bounds,probe_pathsync,probe_env,probe_stale,probe_cr}.py`
- 日志: `_scratch_Q/logs/`（`n2_*`、`matrix_fix`、`lf_*`、`bounds_fix`、`pathsync_fix`、
  `env_fix`、`stale_fix`、`cr_*`、`probe_{n1,n3,bypass}_fix`、`verifier_ext_*`、`verifier_fix`、
  `reg_{pristine,patched}.tsv`、`suite_*`）
- 补丁矩阵: `_scratch_Q/patchtest/`

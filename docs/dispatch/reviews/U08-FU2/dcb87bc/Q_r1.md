# Q · U08-FU2 独立验收报告（R1）

- 验收人：Q（独立会话，只验证不修码；全程未改产品代码）
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U08-FU2\R1\q`，detached HEAD @ `dcb87bc`
  （= 代码 head `7746605` + 补丁包提交）
- 解释器：`C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe`（无 pytest），
  一律 `PYTHONUTF8=1`
- 产物：仅本工作树 `_scratch_Q/`
- 自建两棵树：
  - `_scratch_Q/pristine` = `git archive b0c61ba | tar -x` + `git init`（未 commit）
  - `_scratch_Q/patched` = `git archive 7746605 | tar -x` + `git init`
  - `_scratch_Q/patched_apply` = pristine + `git apply` 3 个补丁（验证 diff 忠实性）

## 1. 结论

**PASS-with-notes**（无阻塞项）。

- 核心判据「去重失败时不得重复下单」**成立且方向确为 fail-closed**：审计库读不出答案时
  该票被**跳过**（0 intent），**不烧账本**（`consume()` 调用数 = 0、`is_consumed` 仍 False、
  账本文件甚至未被创建），披露码是 `AUDIT_UNAVAILABLE` / `READINESS_BLOCKED` / `fault`，
  `verdict=blocked`，**不是** `TICKET_ALREADY_CONSUMED`（后者 verdict 会落 `handled`）。
- 关键对抗场景**「票据其实已下过单、但审计库恰好读不出」实测仍为跳过**（用真实
  `AuditDatabase` 先证明权威说"已下单"=True，再让读侧抛 `OperationalError`）——
  **无重复下单风险**。
- `KeyboardInterrupt` / `SystemExit` 在读、写两条路径上**均仍穿出**（实测）。
- 正常路径逐字不变：`tests/integration/test_strategy_funnel.py` 两态 **208/0**；
  旧冒烟脚本两态 **91/0**；新冒烟脚本 patched **101/0**、旧实现 **94 PASS/7 FAIL**
  且 7 条失败**全部**来自新增第 [10] 节。
- 全量离线回归（94 脚本）NEW_FAIL/FIXED/CHANGED **均为空集**。
- 3 补丁 × 3 git 配置：`Checking patch` 齐全，**0 Skipped / 0 Error**。

Notes（非阻塞，详见 §7）：
1. 场景 F（账本**已**消费 + 审计读失败）披露为 `AUDIT_UNAVAILABLE`（`blocked`）而非
   `TICKET_ALREADY_CONSUMED`（`handled`）。语义上偏保守（不撒谎、不重复下单），
   但"这张票其实已经被消费"这一事实在本轮被 `AUDIT_UNAVAILABLE` 覆盖。
2. 纯审计写入失败只进 note、不进漏斗（作者已在 README §8.3 主动登记为未尽事项）。
3. 作者 README §8.5 称 `:231` 过期分支不可达 —— Q 未独立复核该"死代码"判定
   （见 §7）。

## 2. 第 1 条：四场景前后对照（**Q 自建探针**，未复用作者 `*_probe.txt`）

探针：`_scratch_Q/probe_q.py`（只对 `db` 打桩注入 `sqlite3.OperationalError` /
`KeyboardInterrupt` / `SystemExit`，**不改被测逻辑**）。
命令（两棵树同一份探针）：

```
PYTHONUTF8=1 <venv>/python.exe _scratch_Q/probe_q.py _scratch_Q/pristine pristine
PYTHONUTF8=1 <venv>/python.exe _scratch_Q/probe_q.py _scratch_Q/patched  patched
```

输出：`out_probe_pristine.json` / `out_probe_patched.json`（rc=0/0）。

| 场景 | pristine（修前） | patched（修后） |
|---|---|---|
| **A** 去重读取抛 `OperationalError`（1 张票） | `escaped=sqlite3.OperationalError: database is locked`；`decision=<未返回>`；`snap=null`（intents 随栈丢失）；funnel **空** | `escaped=None`；`decision=no_trade`；`n_intents=0`；`AUDIT_UNAVAILABLE=1`；`verdict=blocked` / `primary=READINESS_BLOCKED`；note 含 `OperationalError` |
| **A2** 只坏一张票（t-bad + t-good） | `escaped=OperationalError`；`decision=<未返回>`；整轮作废 | `escaped=None`；`decision=open`；`n_intents=1`；`intent_tickets=['t-good']`；`AUDIT_UNAVAILABLE=1`（坏票不牵连好票） |
| **B** 纯审计写抛错（前有已构造 intent） | `escaped=OperationalError`；`decision=<未返回>`；**已构造的 intent 随栈丢失** | `escaped=None`；`decision=open`；`n_intents=1`；`intent_tickets=['t-good']`；note = `审计写入失败（log_risk_event(strategy_conflict_skip,t-hold)，OperationalError: database is locked），本轮继续` |
| **C** `KeyboardInterrupt`（读路径） | `escaped=builtins.KeyboardInterrupt`；未返回 | `escaped=builtins.KeyboardInterrupt`；未返回（**仍穿出**） |
| **C** `SystemExit(7)`（读路径，Q 追加） | `escaped=builtins.SystemExit: 7` | `escaped=builtins.SystemExit: 7`（**仍穿出**） |
| **H** 无 funnel、无 ledger 时读失败（Q 追加） | `escaped=OperationalError` | `escaped=None`；`decision=no_trade`；`n_intents=0`（护栏不依赖 funnel 接线） |
| **G** 正常路径（db 健康、无持仓） | `open` / 1 intent | `open` / 1 intent（逐字相同） |

结论：四场景（A/A2/B/C）**独立复现**，与作者声称一致；修前异常穿出、decision 未返回、
intents 丢失；修后不穿出。

## 3. 第 2 条：方向验证（**真 fail-closed，不是"看起来像"**）

### 3.1 去重读取失败 -> 该票被**跳过**而非放行

场景 A/A2/D 均显示 `n_intents` 不含故障票：A = 0、A2 只含 `t-good`、D = 0。
若写成 fail-open（读不出当"没下过"），A 会得到 `open` + 1 intent；实测不是。

### 3.2 该票**未被账本消费**（不烧票）

用 `SpyLedger(TicketLedger)`（只加 `consume()` 计数器，不改行为）实测：

| 场景 | `consume_calls` | `is_consumed` | 账本文件 |
|---|---|---|---|
| A（读失败，票已 register） | **0** | **False** | — |
| D（读失败，票已 register） | **0** | **False** | **未被创建** |

即"少开一笔"不会退化成"永久不再开"。

### 3.3 披露码**不是** `TICKET_ALREADY_CONSUMED`；verdict = `blocked`

patched 场景 A/D/F：`detail_counts={'AUDIT_UNAVAILABLE':1}`，
`verdict_pair()=('blocked','READINESS_BLOCKED')`；
`funnel_mod.DETAIL_TICKET_ALREADY_CONSUMED` 计数为 `None`（未出现）。
对照（场景 E，权威可读且说已下单）：`detail_counts={'TICKET_ALREADY_CONSUMED':1}`、
`verdict=('handled','HOLD_EXISTING')` —— 两种事实确实**不互相冒充**。

注册表程序化核对（patched 树）：

```
code: AUDIT_UNAVAILABLE
reason: READINESS_BLOCKED == READINESS_BLOCKED? True
nature: fault == fault? True
stages: ('ticket_fresh',) == ticket_fresh? True
in ROUND_ONLY_DETAILS? False
validate_detail_table: OK
classify_no_open(details={AUDIT_UNAVAILABLE:1}, opened=0, evaluated=1) -> ('blocked','READINESS_BLOCKED')
```

### 3.4 **关键对抗**：票据"确实已下过单"但审计库读不出 -> 仍是**跳过**

构造（探针 D）：
1. 建真实 `AuditDatabase`（临时 sqlite），向 `orders` 表插入一行 `ticket_id='t-ordered'`
   的订单；
2. `real.signal_ticket_has_order('t-ordered')` 直接调用 -> **True**（证明权威真的说"已下单"）；
3. 用 `UnreadableProxy(real)` 包一层（读侧抛 `OperationalError`，写侧透传），
   作为 `db` 传入执行器。

实测（patched）：`escaped=None`、`decision=no_trade`、`n_intents=0`、
`consume_calls=0`、`is_consumed=False`、账本文件未创建、
`AUDIT_UNAVAILABLE=1`、`verdict=blocked`。
**没有放行 —— 无重复下单风险。**

顺带验证恢复后仍不重复：审计库恢复可读后（同一真实权威）再跑一轮（探针 D2）：
`TICKET_ALREADY_CONSUMED=1`、`verdict=handled`、`n_intents=0`。即抖动窗口内既没重复开、
也没把票据记成消费掉。

## 4. 第 3 条：正常路径逐字不变 + 新增断言"扣旧实现应变红"

| 套件（cwd=树根） | pristine（旧码） | patched（新码） | exit |
|---|---|---|---|
| `tests/integration/test_strategy_funnel.py` | **208 / 0** | **208 / 0** | 0 / 0 |
| `scripts/smoke_u08_strategy_funnel.py`（**旧脚本** b0c61ba） | **91 / 0** | **91 / 0** | 0 / 0 |
| `scripts/smoke_u08_strategy_funnel.py`（**新脚本**，含第 [10] 节） | **94 / 7** | **101 / 0** | 1 / 0 |

- 91 -> 101 的 +10 **全部**是新增第 [10] 节断言；既有 91 条在两态、两个脚本口径下
  均 PASS，语义未变。
- 旧实现下失败的 7 条，**逐条比对**（失败清单取自 `smoke_pristine_newscript.txt`
  结尾 FAILURES 段，全部以 `FU2 ` 开头、全部位于第 [10] 节）：

  1. FU2 去重读取抛 OperationalError 不再穿出执行器（decision 仍返回）
  2. FU2 读取不可用 -> fail-closed 跳过：0 笔 intent 且披露 AUDIT_UNAVAILABLE
  3. FU2 跳过不等于'没下过单'放行，也不谎报已消费（三种事实不混）
  4. FU2 未注册自由文本不得冒充原因码（细分码走注册表）
  5. FU2 坏票跳过、同轮其余票照常开仓（不因一张票读不出而整轮停摆）
  6. FU2 纯审计写入抛 OperationalError 不再穿出，已构造的 intent 不丢
  7. FU2 审计写入失败如实披露（note 带操作名与底层异常名，不静默吞掉）

  → 7 条**全部来自第 [10] 节**，无一条是别处被改坏。
- 第 [10] 节另 3 条在两态都 PASS（确认是**不变量**，不是新口径）：
  「跳过不烧票」「只吞 Exception：KeyboardInterrupt 仍穿出」「写入失败不改既有跳过语义
  （仍记 SYMBOL_ALREADY_HELD）」。

## 5. 第 4/5/6 条

**第 4 条（`KeyboardInterrupt` / `SystemExit` 仍穿出）**——实测**穿出**，非阻塞：

| 注入点 | 结果 |
|---|---|
| 读路径 `signal_ticket_has_order` 抛 `KeyboardInterrupt` | `escaped=builtins.KeyboardInterrupt`，decision 未返回 |
| 读路径抛 `SystemExit(7)` | `escaped=builtins.SystemExit: 7`，decision 未返回 |
| 写路径 `log_risk_event` 抛 `KeyboardInterrupt`（构造同 symbol 已有仓位走到写入分支） | `escaped=builtins.KeyboardInterrupt`，decision 未返回 |
| 写路径抛 `SystemExit(9)` | `escaped=builtins.SystemExit: 9`，decision 未返回 |

**第 5 条（注册与语义、文档一致性）**——通过：

- `src/core/strategy_funnel.py` 相对 pristine 为 **+7/−0**（`--numstat` = `7 0`），
  逐行核对为 **4 处纯新增**（1 段注释 + 1 个常量 + 3 张映射表各 1 行），
  **既有行一个字符未改**。
- `docs/策略扫描原因码.md` 相对 pristine 为 **+1/−0**，`difflib` 逐行核对为
  **唯一一处新增行**，既有原因码表**一字未动**。
- 文档登记与实现**一致**：文档称 `AUDIT_UNAVAILABLE | READINESS_BLOCKED | fault |
  ticket_fresh`；实现 `DETAIL_REASON=READINESS_BLOCKED`、`DETAIL_NATURE=fault`、
  `DETAIL_STAGES=(ticket_fresh,)`（程序化断言全 True）。
- `validate_detail_table()` OK；`classify_no_open` 对该码返回 `blocked`。
- 该码**不在** `ROUND_ONLY_DETAILS`（正确：走 `record()` 逐票登记，不是轮级故障）。

**第 6 条（越权）**——通过，0 改动：

`git diff --name-only b0c61ba 7746605` 只有 4 个文件：
`docs/策略扫描原因码.md`、`scripts/smoke_u08_strategy_funnel.py`、
`src/agents/strategy_trader.py`、`src/core/strategy_funnel.py`。

| 文件 | 改动行数 |
|---|---|
| `src/core/signal_ticket.py` | 0 |
| `src/core/release_evidence.py` | 0 |
| `src/core/ops/recovery.py` | 0 |
| `pyproject.toml` | 0 |
| `scripts/run_offline_regression.py` | 0 |
| `scripts/check_quality.py` | 0 |

`U08-R5-FU1` / `U13FU3-INJECTION-TRUST` / `U09-K06-*` 在 3 个补丁 diff 中**0 提及**；
`strategy_trader.py` 的 `release()` 撤销路径与账本 `save()` 路径的既有代码**未被删除或改写**
（diff 的删除行仅为 4 处 `db.*` 调用、1 行 import、1 行去重闸门）。

## 6. full delta 表 + check_quality + 补丁矩阵

### 6.1 全量离线回归 `scripts/run_offline_regression.py`（两态，94 脚本）

```
PYTHONUTF8=1 <venv>/python.exe scripts/run_offline_regression.py   # cwd=树根
```

| 树 | 退出码 | 结尾 |
|---|---|---|
| `_scratch_Q/pristine` | **0** | `OFFLINE REGRESSION OK scripts=94` |
| `_scratch_Q/patched` | **0** | `OFFLINE REGRESSION OK scripts=94` |

**环境偏差（如实登记）**：runner 用 `subprocess.run(..., check=True)`，首个子进程非零即
`raise`。按规格"只 `git init`"（**无 index**）时，两态都会在第 85 个脚本
`scripts/smoke_u10a_release_package.py` **同样地**崩掉——该脚本靠
`git ls-files` 建沙箱，无 index 时沙箱为空。故我在**两棵树各做一次 `git add -A`（只暂存、
不 commit、`core.autocrlf=false`）**后再跑；两态处理完全一致，且**补丁矩阵（§6.3）
在独立的全新未暂存树上另测**，不受影响。
（作者 `evidence/offline_regression_*.txt` 的 94 脚本全绿可复现。）

逐脚本摘要（每块最后一个 `PASS x / FAIL y` 形式的行；两态 94 块一一对齐）：

| 集合 | 结果 |
|---|---|
| **NEW_FAIL**（pristine 过 -> patched 挂） | **∅** |
| **FIXED** | **∅** |
| **CHANGED**（摘要数字变化） | **1 个**：`scripts/smoke_u08_strategy_funnel.py` `PASS 91 / FAIL 0` -> `PASS 101 / FAIL 0` |
| 其余 93 个脚本 | 摘要逐字相同（其中 `smoke_u10a_release_package.py` 末行仅 `release_id` 哈希随机，归一化后相同） |

> 注：pristine 树里我用的是**原始**（b0c61ba）冒烟脚本，patched 树用的是新脚本，
> 因此 91->101 反映的是"脚本新增断言"，而非代码行为变化；两态 FAIL 均为 0。
> 31 个脚本没有 `PASS/FAIL` 形式的摘要行，但 runner `check=True` 保证它们在两态
> **退出码均为 0**（否则到不了 OK 行）。

### 6.2 质量门禁与契约冒烟（patched 沙箱）

| 检查 | 结果 | rc |
|---|---|---|
| `scripts/check_quality.py` | `QUALITY CHECK OK files=303 untracked=303`（我未 commit，故 untracked 非 0；`files=303` 与作者一致） | 0 |
| `scripts/smoke_contracts_v3.py` | `结果：PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)` | 0 |

`scripts/check_quality.py` 相对 pristine **0 改动** ⇒ **未新增 exclude**。

### 6.3 补丁矩阵：3 补丁 × 3 git 配置

在**全新** `git archive b0c61ba | tar -x` + `git init`（未 stage/未 commit）的树上，
对 3 个 diff 各跑 `git apply --check --verbose`：

| git 配置 | 01（2 文件） | 02（1 文件） | 03（1 文件） |
|---|---|---|---|
| 默认 | 2 Checking / 0 Skipped / 0 Error | 1 / 0 / 0 | 1 / 0 / 0 |
| `-c core.autocrlf=false -c core.eol=lf` | 2 / 0 / 0 | 1 / 0 / 0 | 1 / 0 / 0 |
| `-c core.autocrlf=true` | 2 / 0 / 0 | 1 / 0 / 0 | 1 / 0 / 0 |

（共 4 个文件命中 patch；与"三者文件不相交"一致。）

**diff 忠实性**：`_scratch_Q/patched`（`git archive 7746605`）
vs `_scratch_Q/patched_apply`（pristine + 3 补丁）逐文件比对，
**内容逐字节相同（忽略 CRLF/LF）**，4 个文件全部 `SAME`。

**踩坑（Q 实测复现）**：直接 `cp -r` 一个由 `git init`（继承全局 `core.autocrlf=true`）
创建的树并 `git apply`，会把新增/改动文件写成 CRLF（本机 `gitconfig` 全局
`core.autocrlf=true`、仓库级 `false`）；这是**沙箱自造环境**，不是补丁缺陷
（用 `-c core.autocrlf=false` 应用即与 `7746605` 归档逐字节一致）。

## 7. 未能验证的部分

1. **作者 README §8.5 的"死代码"判定未独立复核**：作者称 `strategy_trader.py`
   过期分支（`_not_expired` 为假处的 `log_risk_event` / `update_signal_ticket`）
   在当前 `ticket_skip_detail` 真值表下**不可达**。Q **未**做该可达性证明，
   也未验证它被保留是否真的不改变任何路径（只验证了该分支被**加了护栏**，
   且 `git diff` 里既有语义未被放宽）。
2. **跨进程/并发未覆盖**：全部验证为单进程、离线、桩注入。未构造多进程同时
   `consume()` 或真实 `database is locked` 竞争窗口；`orphan_consumed()` 人工补单路径
   未实测。
3. **未跑真实服务/未对真实审计库施压**：未启停 demo 服务、未触碰任何真实 `data/`/`.env`/
   密钥/账户标识（按硬约束禁止），因此"审计库整体长时间不可用"的轮级退避压力
   未观测（作者 README §8.2 已登记为该口子的未尽事项）。
4. **未核对作者 `evidence/*.txt` 内容本身**（只核对了由这些证据支撑的可复现结论）。
5. **`check_quality.py` 的 EXCLUDE 元组内容未逐一审计**，只依据"该文件相对 pristine
   0 改动"推断"未新增 exclude"。
6. **`U08-R5-FU1` / `U13FU3-INJECTION-TRUST` / `U09-K06-*` 的正确性未评估**，
   只验证了这三个任务在本次补丁中**0 触及**。
7. **未验证** `AUDIT_UNAVAILABLE` 在展示层/看板侧（`dashboard/app.py`）的呈现是否
   正确渲染（该文件 0 改动，新码未被展示层显式处理，走的是通用
   `detail_counts`/`verdict` 通路）。

## 8. 报告路径

`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U08-FU2\R1\q\_scratch_Q\Q_U08-FU2_r1.md`

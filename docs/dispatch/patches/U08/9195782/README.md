# U08 共享适配补丁包（r4 · 作者 C-U08 / 交 I 应用）

- **补丁基线 = 集成 HEAD `1a412440`（`mat-wt/I`）**，5 个目标文件的 `index` blob
  与 `git rev-parse 1a412440:<path>` 逐文件相同（§四）。5 个 diff 的上下文取自该
  提交的 **pristine 共享文件**。
- **本目录名 `9195782` = 本轮（r4）U08 独占产物的源提交**，即 `task/U08` 上
  "只触碰独占文件"的那个 tip 提交（`9195782 fix(u08): contain all ledger write
  failures (r4 N6)`）。**它不是"创建本目录的提交"** —— 目录命名一律跟"被投递内容
  的源提交"（B-r3 D1 澄清，r4 沿用）。
- **取代关系**：本包取代 `../0c131f2/`（r3），`../0c131f2/` 取代 `../ec8fd1c/`（r2），
  `../ec8fd1c/` 取代 `../c147895/`（r1）。**三个旧包顶部都已标注 SUPERSEDED 并
  指回本包**（一到两跳内可达）——**四包并存时请只用本包**。
- **批次约束**（同 r3，未变）：本包**必须与 U08 独占产物同批合入**，且 `05` 必须与
  `scripts/smoke_u08_strategy_funnel.py` 同批（见 §二）。
- 说明：本包由 **I 在集成时应用**；`task/U08` 分支**不携带任何共享文件**。

## 一、补丁清单（5 个）

| 补丁 | 目标文件 | 用途 |
|---|---|---|
| `01_signal_engine_scanner_funnel.diff` | `src/signal_engine/scanner.py` | 同 r3（逐闸门原因 + 轮级故障 + `last_scan.funnel`；r3 的 N1：先读卡册再校验运行时）。**r4 逐字节未改** |
| `02_core_signal_ticket_skip_detail.diff` | `src/core/signal_ticket.py` | 同 r3（`ticket_skip_detail()` 与 `valid_signal_ticket()` 同一真值表）。**r4 逐字节未改** |
| `03_agents_strategy_trader_reasons.diff` | `src/agents/strategy_trader.py` | 同 r3（逐票原因、`funnel=`/`ledger=`、删除静默批量过滤、预算早退留痕、N3 收尾落盘局部化、N4 同轮 release）。**r4 新增 N6**：收尾 `save()` 由"只吞 `LedgerLockTimeout`/`LedgerCorrupt`"扩为 **全异常局部化**（`except Exception`），认领/撤销各自加兜底 `try` |
| `04_dashboard_app_funnel_page.diff` | `src/dashboard/app.py` | 同 r3（`/funnel`、`/api/funnel`、读侧重重建与对账）。**r4 逐字节未改** |
| `05_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | 同 r3（登记 `scripts/smoke_u08_strategy_funnel.py`）。**r4 逐字节未改** |

## 二、批次约束（必须遵守）

1. **先合入 U08 独占产物，再应用 `01`–`04`**：四者都会 `import core.strategy_funnel`。
   只应用补丁而不合入 `src/core/strategy_funnel.py` ⇒ `ModuleNotFoundError`。
2. **`05` 必须与 `scripts/smoke_u08_strategy_funnel.py` 同批**：脚本落库而 `05` 未应用，
   `_validate_smoke_manifest()` 立即以 `RuntimeError: 未登记冒烟脚本: ...` 失败。
3. **r4 的 `01`–`05` 与 r3 的 `01`,`02`,`04`,`05` 逐字节相同**（sha256 前 12 位：
   `4d369c6647de` / `7c55acfe4bb6` / `a997ceaca16c` / `d97ec0a057d8`），只有 `03` 变了
   （`8b3f383f068e` → `9a71cd7ef4bd`）。**不要混用两包的 `03`。**

## 三、r4 相对 r3 的变更（只有 2 项）

| 项 | 内容 |
|---|---|
| **N6（P2 · 上轮唯一阻塞项）** | `run_strategy_trader` 收尾 `ledger.save()` 原先只吞 `(LedgerLockTimeout, LedgerCorrupt)`，`TicketLedger.consume()`/`release()` 只吞 `LedgerLockTimeout` ⇒ **`OSError`（磁盘满/只读挂载）、`PermissionError`（`os.replace` 目标被占用）、`sqlite3.OperationalError` 等写盘异常仍穿出整个执行器**：已消费落盘的票据被丢掉、本轮已构造的 intents 全部作废、调用方只得到一句泛泛的"策略交易AI异常" —— 与 r3 已修的 N3 完全同形。改法：<br>① `strategy_trader` 收尾 `save()` → `except Exception`（**只**吞 `Exception`，`KeyboardInterrupt`/`SystemExit` 仍穿出），失败照样登记轮级故障 `LEDGER_SAVE_DEFERRED` + 保留 intents + 孤儿可由 `orphan_consumed()` 审计；<br>② 认领 `ledger.consume(...)` 加兜底 `try`：账本异常 → 该票 fail-closed 跳过（`TICKET_LEDGER_UNAVAILABLE`），**异常名进 note**，同轮其余票照常；<br>③ 撤销 `ledger.release(...)` 加兜底 `try`：不穿出，并如实写"票据可能仍被占死"；<br>④ `TicketLedger.consume()` 内部把写盘异常收敛为新的 fail-closed 状态 `LEDGER_WRITE_FAILED`（底层异常类名经 `TicketConsumption.error` 回带），**并在落盘处回滚内存里预置的"已消费"**（没落盘就不算消费，否则会被下一次成功 `save()` 洗成已消费、把从未下单的票永久占死）；`release()` 的写盘异常同样局部化。 |
| **N7（P3 · 文档笔误）** | r3 包 README §六 N4 行把未注入漏斗时的 `round_id` 字面写成 `exec:<iso>`（冒号），**实际是 `exec-<iso>`（连字符）**。本包 §六 已改正；`../0c131f2/README.md` 也一并就地更正。**全仓核对**：有效文档里再无 `exec:<iso>` 笔误（`docs/无人值守升级任务清单_2026-09-19.json` 里的同款描述属**派发台账**、非本任务独占路径，且其语境是"记录该发现"，未改）。 |
| 其余 | r3 的 **N1/N2/N3/N4/N5** 本轮**未改动**（仅 N6 触及 `03`，且只加不删既有行为）。 |

## 四、基线不变性（复核）

`git rev-parse 1a412440:<path>` = 本包 5 个 diff 的 `index` a 侧 blob（同时等于复验
报告里 `32ecf635`/`509246f` 的值 ⇒ 集成 HEAD 前移只动了 `docs/`，5 个共享文件未变）：

```
3cee84747c029f3460380680a92398e739e12479  src/signal_engine/scanner.py
956c2e9a82be0d6f10418b035077fe4b83a8aa61  src/core/signal_ticket.py
96a035fd28a3e2debc9fdf1db92995384266be5e  src/agents/strategy_trader.py
1f77be25861f2bc97b9596686ecb8e9d9ae33957  src/dashboard/app.py
2113d652c871830b5e0ae0246a0ddff865504cde  scripts/run_offline_regression.py
```

## 五、N6 前后对照（确定性构造，原始输出）

探针 `n6_probe.py`（本次沙箱自建，不引用作者对照）。BEFORE = 集成树 + r3 独占产物 +
`0c131f2` 5 补丁；AFTER = 集成树 + r4 独占产物 + 本包 5 补丁。三种异常**各自**在
"收尾落盘 / 执行器侧认领 / 账本内部认领写盘 / 撤销"四条路径上对照；
A/B/C/D 各自的第一行是探针原始打印（逐字），长 JSON 未截断。

```
### BEFORE（r3 交付态）
    strategy_funnel=C:\Users\Tao\Desktop\币安\mat-wt\CD\_scratch_u08_r4\tree_r3\src\core\strategy_funnel.py
  A save()->OSError: ESCAPED OSError: [Errno 28] No space left on device
  A save()->PermissionError: ESCAPED PermissionError: [Errno 13] Permission denied
  A save()->sqlite3.OperationalError: ESCAPED OperationalError: database is locked
  B executor consume()->OSError: ESCAPED OSError: [Errno 28] No space left on device
  B executor consume()->PermissionError: ESCAPED PermissionError: [Errno 13] Permission denied
  B executor consume()->sqlite3.OperationalError: ESCAPED OperationalError: database is locked
  C ledger.consume() 写盘->OSError: ESCAPED OSError: [Errno 28] No space left on device
  C ledger.consume() 写盘->PermissionError: ESCAPED PermissionError: [Errno 13] Permission denied
  C ledger.consume() 写盘->sqlite3.OperationalError: ESCAPED OperationalError: database is locked
  D release()->OSError: ESCAPED OSError: [Errno 28] No space left on device
  D release()->PermissionError: ESCAPED PermissionError: [Errno 13] Permission denied
  D release()->sqlite3.OperationalError: ESCAPED OperationalError: database is locked
  E KeyboardInterrupt: ESCAPED(正确) KeyboardInterrupt: 构造：操作员 Ctrl-C
  E SystemExit: ESCAPED(正确) SystemExit: 3
  E ledger.consume() 遇 KeyboardInterrupt: ESCAPED(正确) 构造：账本写盘时 Ctrl-C

### AFTER（本包）
    strategy_funnel=C:\Users\Tao\Desktop\币安\mat-wt\CD\_scratch_u08_r4\tree_new\src\core\strategy_funnel.py
  A save()->OSError: NO_ESCAPE {"decision": "open", "details": {"LEDGER_SAVE_DEFERRED": 1}, "disk_consumed": true, "intents": 1, "note_has_code": true, "orphan": ["t-a"], "round_faults": ["LEDGER_SAVE_DEFERRED"]}
  A save()->PermissionError: NO_ESCAPE {"decision": "open", "details": {"LEDGER_SAVE_DEFERRED": 1}, "disk_consumed": true, "intents": 1, "note_has_code": true, "orphan": ["t-a"], "round_faults": ["LEDGER_SAVE_DEFERRED"]}
  A save()->sqlite3.OperationalError: NO_ESCAPE {"decision": "open", "details": {"LEDGER_SAVE_DEFERRED": 1}, "disk_consumed": true, "intents": 1, "note_has_code": true, "orphan": ["t-a"], "round_faults": ["LEDGER_SAVE_DEFERRED"]}
  B executor consume()->OSError: NO_ESCAPE {"decision": "open", "intents": 1, "ledger_unavailable": 1, "note": "t-bad 票据账本不可用（OSError: [Errno 28] No space left on device），跳过 [READINESS_BLOCKED/TICKET_LEDGER_UNAVAILABLE]; t-ok 进入待执行: BTCUSDT LONG @ 100.0 名义=20.00", "other_ticket_ok": true, "skipped": ["TICKET_LEDGER_UNAVAILABLE"]}
  B executor consume()->PermissionError: NO_ESCAPE {"decision": "open", "intents": 1, "ledger_unavailable": 1, "note": "t-bad 票据账本不可用（PermissionError: [Errno 13] Permission denied），跳过 [READINESS_BLOCKED/TICKET_LEDGER_UNAVAILABLE]; t-ok 进入待执行: BTCUSDT LONG @ 100.0 名义=20.", "other_ticket_ok": true, "skipped": ["TICKET_LEDGER_UNAVAILABLE"]}
  B executor consume()->sqlite3.OperationalError: NO_ESCAPE {"decision": "open", "intents": 1, "ledger_unavailable": 1, "note": "t-bad 票据账本不可用（OperationalError: database is locked），跳过 [READINESS_BLOCKED/TICKET_LEDGER_UNAVAILABLE]; t-ok 进入待执行: BTCUSDT LONG @ 100.0 名义=20.00U 杠杆=1x", "other_ticket_ok": true, "skipped": ["TICKET_LEDGER_UNAVAILABLE"]}
  C ledger.consume() 写盘->OSError: NO_ESCAPE {"as_dict": {"consumed": false, "duplicate": false, "error": "OSError", "first_at_ms": 0, "first_round_id": "", "round_id": "r1", "state": "LEDGER_WRITE_FAILED", "ticket_id": "t-c"}, "consumed": false, "disk_consumed": false, "error": "OSError", "mem_consumed": false, "state": "LEDGER_WRITE_FAILED"}
  C ledger.consume() 写盘->PermissionError: NO_ESCAPE {"as_dict": {"consumed": false, "duplicate": false, "error": "PermissionError", "first_at_ms": 0, "first_round_id": "", "round_id": "r1", "state": "LEDGER_WRITE_FAILED", "ticket_id": "t-c"}, "consumed": false, "disk_consumed": false, "error": "PermissionError", "mem_consumed": false, "state": "LEDGER_WRITE_FAILED"}
  C ledger.consume() 写盘->sqlite3.OperationalError: NO_ESCAPE {"as_dict": {"consumed": false, "duplicate": false, "error": "OperationalError", "first_at_ms": 0, "first_round_id": "", "round_id": "r1", "state": "LEDGER_WRITE_FAILED", "ticket_id": "t-c"}, "consumed": false, "disk_consumed": false, "error": "OperationalError", "mem_consumed": false, "state": "LEDGER_WRITE_FAILED"}
  D release()->OSError: NO_ESCAPE {"decision": "no_trade", "intents": 0, "mem_consumed": true, "note_mentions": "t-d 账本释放未完成（OSError: [Errno 28] No space left on device），票据可能仍被占死; t-d 建意图失败（ValueError），跳过 [VALIDATION_REJECTED/STRATEGY_TRADER_EXCEPTION]", "release_count": 0}
  D release()->PermissionError: NO_ESCAPE {"decision": "no_trade", "intents": 0, "mem_consumed": true, "note_mentions": "t-d 账本释放未完成（PermissionError: [Errno 13] Permission denied），票据可能仍被占死; t-d 建意图失败（ValueError），跳过 [VALIDATION_REJECTED/STRATEGY_TRADER_EXCEPTION]", "release_count": 0}
  D release()->sqlite3.OperationalError: NO_ESCAPE {"decision": "no_trade", "intents": 0, "mem_consumed": true, "note_mentions": "t-d 账本释放未完成（OperationalError: database is locked），票据可能仍被占死; t-d 建意图失败（ValueError），跳过 [VALIDATION_REJECTED/STRATEGY_TRADER_EXCEPTION]", "release_count": 0}
  E KeyboardInterrupt: ESCAPED(正确) KeyboardInterrupt: 构造：操作员 Ctrl-C
  E SystemExit: ESCAPED(正确) SystemExit: 3
  E ledger.consume() 遇 KeyboardInterrupt: ESCAPED(正确) 构造：账本写盘时 Ctrl-C
```

一句话读法：**12 条"写盘异常穿出执行器"的场景全部变为"本轮有明确结论、其余票据照常、
孤儿可审计"；3 条 `BaseException` 场景前后一致地仍然穿出**（`except Exception` 只收敛
`Exception`，`KeyboardInterrupt`/`SystemExit` 不受影响）。

## 六、N6 / N7 自证（原始输出摘要）

| 项 | 结果 |
|---|---|
| `tests/integration/test_strategy_funnel.py`（**独占产物态**：无共享补丁） | `PASS 135 / FAIL 0`，exit 0 |
| 同上（**打过本包**） | `PASS 208 / FAIL 0`，exit 0 |
| `scripts/smoke_u08_strategy_funnel.py`（独占产物态） | `PASS 58 / FAIL 0`，exit 0 |
| 同上（打过本包） | `PASS 91 / FAIL 0`，exit 0 |
| `scripts/smoke_contracts_v3.py`（打过本包） | `结果：PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)`，exit 0 |
| `scripts/check_quality.py`（打过本包） | `QUALITY CHECK OK files=290 untracked=0`，exit 0 |
| `scripts/scan_tracked_secrets.py`（打过本包） | `SECRET SCAN OK files=525 registered_false_positives=2 …`，exit 0 |
| `git apply --check -v` × 3 种 git 配置（仓库根 = `mat-wt/I`，HEAD `1a412440`） | **15/15**：`default` / `-c core.autocrlf=false -c core.eol=lf` / `-c core.autocrlf=true`，均 `5/5 rc=0`、每份都有 `Checking patch`、无 `Skipped`；`mat-wt/I` 的 `git status` 仍为空 |
| 真实应用（沙箱：集成树 + r4 独占产物 + 本包 5 补丁） | `git diff --stat` = `scripts/run_offline_regression.py +3`、`src/agents/strategy_trader.py`、`src/core/signal_ticket.py`、`src/dashboard/app.py`、`src/signal_engine/scanner.py`；`5 files changed, 706 insertions(+), 61 deletions(-)`；应用后的 `strategy_trader.py` blob = 作者编辑态（逐字节相同） |
| **全量离线回归 delta**（base = pristine 集成树 `1a412440`，head = 打过本包） | `base_scripts=83  head_scripts=84  union=84`；`added=head-base=['scripts/smoke_u08_strategy_funnel.py']`；`base_fail_n=0  head_fail_n=0`；`NEW_FAIL=[]  FIXED=[]`；逐条 rc 直方图：**167 次执行全部 rc=0** |
| 统一入口 `scripts/run_offline_regression.py`（打过本包，端到端） | `SMOKE MANIFEST OK included=80 excluded=1` → `OFFLINE REGRESSION OK scripts=84`，exit 0 |

> **已知的观感噪声（不阻塞）**：`03` 的差异里有 **3 行"只差行尾 CR"** 的
> `-`/`+` 对（pristine 那几行是 CRLF，r3 打完后成了 LF）。这是 r3 包就有的同类
> 现象（r3 打完的文件 169→160 个 CRLF）在 r4 的 diff 里被**如实显式记录**，语义
> 未变；应用后 `strategy_trader.py` 的 blob 与作者编辑态**逐字节相同**
> （`c58b7b2570fb2219f6141ad197c8cba84a86027c`），3 种 git 配置 `--check` 全过。

N7：更正后的字面与代码一致（`n7_literal.py` 在打过本包的树上真跑一轮，原始输出）：

```
未注入漏斗时 round_id 字面 = 'exec-2026-09-21T04:25:38.296028+00:00'
decision = open intents = 1
startswith('exec-') = True
startswith('exec:') = False
```

## 七、未覆盖 / 已知边界（如实登记）

- **`OSError`/`PermissionError` 的真实触发概率未测**：本轮用确定性构造（monkeypatch
  `_save_locked`/`save`）证明"路径存在且现在被局部化"，**不**等于实测线上频率。
- **磁盘满/只读挂载的端到端演练未做**（无法在不影响主机的前提下造真磁盘满）。
- **`save()` 与 `consume()` 的落盘语义**：`consume()` 写盘失败时**回滚内存**（本轮
  **不**产生孤儿）；收尾 `save()` 失败时**已消费事实在盘上**（孤儿走
  `orphan_consumed()` 审计，审计库为权威，只补单不重发）。
- **`release()` 写盘失败**返回 `True`（内存已撤销，落盘留给下一次成功写入）——与 r3
  处理锁超时的口径一致，**盘上可能短暂仍记"已消费"**；该口径 r3 复验已接受，本轮未改。
- **生产端到端一轮**仍不可观测：`graph.py` 不注入 `funnel`/`ledger`（U12 边界），
  "跨进程去重在生产生效"仍无从观测 —— 同 r3，如实归 U12。
- 本轮**未联网**、未读 `.env`/`data/`、未启停服务、未下单撤单、未动风险档/live 开关；
  所有产物只落在 `mat-wt/CD` 内的沙箱目录。

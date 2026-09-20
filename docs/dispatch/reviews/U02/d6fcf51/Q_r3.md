# Q 独立复验 · U02 修复轮 r3（聚焦复验）

- 复验对象：`task/U02` @ **`d6fcf51`**（base `45ebdff`；上一轮审 `4856aec`）
- 我的工作区（独立 detached）：`mat-wt/reviews/U02/R3/r1`（HEAD=`d6fcf51`）
- 上一轮报告（必读）：`mat-wt/I/docs/dispatch/reviews/U02/4856aec/Q_r2.md`
- 补丁目录（交付物，在**我的工作区**内取用）：`<ws>/docs/dispatch/patches/U02/890bb7c/`（6 条）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`；`PYTHONUTF8=1`；**无 pytest**
- 沙箱与探针全部落在 `_scratch_Q/`（独立 git 仓，`git apply` 才生效）：
  - `pristine_int` = `git archive I@b4eeda9` + U02 独占文件（`d6fcf51` 版本）+ README 的 SCRIPTS 登记行（未打补丁）
  - `sb_int` = `pristine_int` + r3 补丁 01..06（`-c core.autocrlf=false -c core.eol=lf`）
  - `sb_r2` = `pristine_int` + `4856aec` 独占文件 + r2 补丁 01..06（**③ 的前后对照**）
- 探针：`probes/probe_q*.py`（全部自写）；原始 stdout：`out_*.txt`

**基线核对**：6 个补丁目标文件在 `b4eeda9` / I 当前 HEAD 上 blob 逐字相同 → 补丁对当前 I 有效。
`I` 会话期间 HEAD 由 `3ec7802` 前移到 `57b720a`，我**固定 `IBASE=b4eeda9`** 保证两棵沙箱可比。

---

## 1. ①（原 P2 NEW-1）已 CONSUMED 的键被账本淘汰后 TTL 回收并重发 —— **真闭合（进程内）**

探针 `probes/probe_q1_consumed.py`（自写，无 monkeypatch；真实 `MAX_TERMINAL_RESERVATIONS=2000`）。

### 1.1 前后对照（同一探针跑两棵树）

```
== sb_r2（BEFORE，4856aec）  Q_CONSUMED_PROBE FAIL=22
FAIL A2_is_consumed_true                                  # 无 is_consumed 事实判据
FAIL A3/A4/A5 release_on_consumed   accepted=True -> 状态 CONSUMED 被改回 RELEASED
FAIL A6/A7 reserve_after_consumed   accepted=True reserved_qty=10 available=0  # 同键二次占量
FAIL B5_pruned_consumed_reserve_rejected   accepted=True
FAIL B6_pruned_consumed_release_rejected  accepted=True
FAIL C1_consumed_key_not_resubmitted_after_prune
     {'action': 'SUBMIT_REDUCE', 'duplicate': False, 'submit_qty': '1',
      'reason_codes': ['STALE_EXIT_RESERVATION_RECLAIMED']}          # <<< 正是 NEW-1
FAIL F1/F2/F3 release_exit 对 CONSUMED 返回 True 并撤销键，随后同键 SUBMIT_REDUCE
         （F3 {'action': 'SUBMIT_REDUCE', 'duplicate': False, 'submit_qty': '1'}）
FAIL F5/F7 记录被淘汰后 release_exit 仍 True，同键再次 SUBMIT_REDUCE
FAIL E_consumed_still_consumed
== sb_int（AFTER，d6fcf51）  Q_CONSUMED_PROBE FAIL=1
FAIL D_restart_fact_survives     # 见 §1.3：内存态，注册在 P2-9
```
（作者报告 BEFORE `FAIL=5` / AFTER `FAIL=0`；我的探针断言更宽，同一现象的**计数**不同，
但关键用例 `C1` 与作者 `B2` 逐字一致：`SUBMIT_REDUCE … STALE_EXIT_RESERVATION_RECLAIMED`。）

**结论 ①：真修。** 我自建的 22 条反例里，BEFORE 全中、AFTER 只剩"重启"这一条（见 1.3）。

### 1.2 淘汰**恰好边界**（2000/2001）与"淘汰后重新 reserve 同一 id"

```
INFO MAX_TERMINAL_RESERVATIONS=2000
PASS B1_exactly_at_limit_record_present   n=2000 present=True     # 恰好 2000：记录仍在
PASS B2_at_limit_is_consumed
PASS B3_over_limit_record_pruned          n=2000 present=False    # 2001+ 触发淘汰，kc 记录被挤掉
PASS B4_pruned_but_is_consumed_true       # 记录没了，成交事实仍在
PASS B5_pruned_consumed_reserve_rejected  accepted=False reason=['RESERVATION_ALREADY_CONSUMED']
PASS B6_pruned_consumed_release_rejected  accepted=False reason=['RESERVATION_ALREADY_CONSUMED']
PASS B7_consumed_id_count_visible         1
```
→ 淘汰**边界语义正确**：`_prune_locked` 在 `len > 2000` 时才淘汰；被淘汰的 CONSUMED 键
**仍被 `_consumed_ids` 拦住**（reserve / release 都返回 `RESERVATION_ALREADY_CONSUMED`，
且 `available_qty` 未被二次占用）。

### 1.3 **① 的"重启语义"专项结论（关键）**

`_consumed_ids` 是 **纯内存 set**（`ManagementReservationLedger.__init__` 里 `set()`；
全仓无任何把预留/成交事实落库的语句）。实测：

```
INFO D_restart_same_key={'exit_key': 'kr', 'accepted': True, 'duplicate': False,
                         'action': 'SUBMIT_REDUCE', 'submit_qty': '1', 'reduce_only': True}
FAIL D_restart_fact_survives
```

**结论（明确）：r3 的 "已成交的退出键永不可重开" 只在单进程生命周期内成立。**
- 进程重启后 `TradingService` 重建管理器 → 新账本 → `_consumed_ids` 为空 → 同一 `exit_key`
  **可以再次 `reserve` 并 `SUBMIT_REDUCE`**（实测）。
- 因此 §1.1 的 AFTER 并不是"永不重发"，而是"**在内存事实不丢的前提下**不重发"。
- 这条**已被作者如实登记**：README「如实登记 P2-9」明写
  「U02 的全部状态（`_expectations`/`_hard_exits`/`_ledgers`/`_exit_keys`）都是**进程内易失**，
  跨重启的重复退出识别与在途预留**不成立**」；`_consumed_ids` 属于 `_ledgers`。
  README §已知限制 r3 新增条目另加了 `_consumed_ids` 只增不淘汰的代价说明。
  → **属已登记项，不是未登记缺陷**；但建议 README 在 r3-1 的"已真修"措辞后**显式补一句
  "仅在进程内"**，避免读者把"永不重开"读成跨重启保证（这是**文字口径**建议，非代码缺陷）。
- 影响界：跨重启重发仍是 `reduce_only` 减仓单、提交量被 `min(请求量, 可用量)` 夹逼、
  显式 cid 的生产路径还会被 `orders` 表/exchange 同 cid 拒单拦住（实测 §3 用例 7b）；
  不会反向开仓、不会超仓。**与 P2-11 登记的代价同源。**

### 1.4 绕过尝试（我独立设计的反例）

| 绕过尝试 | 结果 |
|---|---|
| 淘汰恰好边界 2000 / 2001（§1.2） | **未绕过**：2000 保留记录、2001 淘汰记录但事实仍在，两种情形都拒绝重发 |
| 淘汰后重新 `reserve` 同一 id | **未绕过**：`RESERVATION_ALREADY_CONSUMED` |
| 记录被淘汰后 `release()` 同一 id | **未绕过**：`RESERVATION_ALREADY_CONSUMED`，状态不变 |
| `exit_key` 记忆（`_exit_keys`）被 `MAX_EXIT_KEYS` 淘汰后再 `request_exit` | **未绕过**：`INFO C2 exit_keys_has_kq=False`，但 `request_exit` 回到 `ledger.is_consumed` → `NOOP/DUPLICATE_EXIT_IGNORED` |
| 换一个 `lifecycle_id` 传同一个 `exit_key`（域漂移） | **未绕过**：`NOOP/DUPLICATE_EXIT_IGNORED`（`_reclaim_if_stale_locked` 走 `_exit_ledgers` 记的原账本 → `is_consumed=True`） |
| **进程重启（事实丢失）** | **绕过了** —— 见 §1.3（已登记 P2-9） |
| 并发 reserve+release+consume+reclaim 混沌（6 线程 × 500 轮） | **未绕过**：`E_concurrent_no_error`、`E_consumed_still_consumed`、`E_no_over_reservation`、`E_conservation` 全 PASS |

---

## 2. ② `release()` 幂等性 —— **真修**

探针同上（`probes/probe_q1_consumed.py` §A/§B/§F）。各类状态组合实测：

| 起始状态 | 操作 | AFTER（`sb_int`） | BEFORE（`sb_r2`） |
|---|---|---|---|
| ACTIVE | `release` | `accepted=True`，状态→RELEASED，`available` 恢复（A12/A13/A14） | 同（一致） |
| RELEASED | `release` 再来一次 | `accepted=False` + `RESERVATION_ALREADY_CONSUMED`，**不改状态**（A9） | 同（r2 已幂等） |
| CONSUMED | `release` | `accepted=False` + `RESERVATION_ALREADY_CONSUMED`，状态**保持 CONSUMED**（A3/A4/A5） | **`accepted=True` → 状态被改回 RELEASED**、可再 `reserve`（A3/A5/A6/A7 FAIL） |
| CONSUMED 且**记录被淘汰** | `release` | `accepted=False` + `RESERVATION_ALREADY_CONSUMED`（B6） | `accepted=True`（B6 FAIL） |
| 未知 id | `release` | `accepted=False` + `RESERVATION_UNKNOWN`（A11） | 同 |
| CONSUMED（管理器层） | `release_exit` | 返回 `False` 且**保留**幂等记忆（F1/F2/F3） | 返回 `True`、撤销键，随后同键 `SUBMIT_REDUCE`（F1/F3 FAIL） |
| CONSUMED+记录淘汰（管理器层） | `release_exit` | `False`（F5/F6/F7） | `True`，同键 `SUBMIT_REDUCE`（F5/F7 FAIL） |

**二次扣减 / 二次预留**：`A7`（`reserved_qty=0`）、`A14`（`position_qty` 不变）、
`E_no_over_reservation`、`E_conservation` 均 PASS → **状态不会倒退、不产生二次扣减/二次预留**。

**结论 ②：真修。** 与它自己的 docstring「对已终结预留幂等」一致。

---

## 3. ③ 纸面"同轮抑制"声明与事实 —— **行为已与声明一致（两侧都验）**

探针 `probes/probe_q3_paper_round.py`（自写，驱动**真实 `PaperBroker.execute()`**，
临时 state 文件 + 临时 `AuditDatabase`，落在 `_scratch_Q/tmp/`；不联网、不下单）。

```
== sb_int（AFTER）  Q_PAPER_ROUND_PROBE FAIL=0
INFO 1_same_round=['close', 'duplicate_skipped']            # 同轮同名同量第二次被抑制 ✓
INFO 2_cross_round=['close'] ['close']                      # 跨轮仍执行 ✓
INFO 2b_cross_round_same_fingerprint=['close'] ['close']    # 跨轮同指纹仍执行 ✓
INFO 3_five_rounds=[['close']×5]                            # 5 轮全部执行 ✓
INFO 4_same_round_partial_then_rest=['close', 'close']      # 同轮 0.3+0.7 都执行 ✓
INFO 5_same_round_same_partial=['close', 'duplicate_skipped']
INFO 6_cross_symbol=['close', 'close']                      # 跨标的独立 ✓
INFO 7_explicit_cid_same_round=['close', 'duplicate_skipped']
INFO 7b_explicit_cid_next_round=['duplicate_skipped']       # 由 orders 表拦（生产同口径）
INFO 8_same_round_diff_action=['close', 'close']            # close 与 reduce 不互相抑制
INFO 9_no_position=['exit_noop']                            # 无仓不反向开仓
INFO 11_same_round_diff_ticket=['close', 'close']           # 不同 ticket 的同量退出都执行
== sb_r2（BEFORE）  Q_PAPER_ROUND_PROBE FAIL=2
INFO 1_same_round=['close', 'exit_noop']                    # r2 不抑制：第二次重新占量后无仓可减
INFO 5_same_round_same_partial=['close', 'close']           # r2 **两笔 0.3 都执行**（声明失真）
```

**两侧都验**：同轮确实抑制（用例 1/5/7）**且**跨轮确实仍能执行（用例 2/2b/3/4/6/11）
——**没有"修一侧坏另一侧"**。r2 的失真声明（README r2-3「保持抑制」）已由 r3-3 改成事实。

**新观察（②，P3 级，非阻塞，未在 README 单列）**：用例 10 显示同轮"两笔**相同**的部分平"
会被合并成一笔：

```
INFO 10_same_round_identical_partials=['close', 'duplicate_skipped'] remaining=[0.3]
INFO 10b_next_round_completes=['close'] remaining=[]
```
即"同一轮内两笔同量 0.3 想平完 0.6"这一轮只平了 0.3，**剩 0.3 要到下一轮才平**。
不会反向开仓、不会超量，且下一轮自愈；只在"同轮发出两笔指纹完全相同（同 action+同 qty+
同 token+同 signal/ticket/strategy 或都缺省）"的意图集合上出现。r2 会两笔都执行。
**这是 r3-3 抑制语义的必然代价，属可接受的取舍**，但 README 未单列该后果（仅写"同轮抑制"），
建议在 r3-3 补一句已知后果。

---

## 4. 迁移 README 两处引证 —— **两处都改对了（源码 + 真实 schema 双重核实）**

探针 `probes/probe_q4_migrate.py`（自写；真实 `AuditDatabase` schema + 临时 DB 在 `_scratch_Q/tmp/`；
不读 `.env`/`data/`/密钥）。原始输出 `out_q4.txt`（`Q_MIGRATE_PROBE FAIL=0`）。

### 4.1 `:2926` 到底查什么表 —— **README r3 的说法正确**

```
INFO guard_query_starts_at_line=2925 (未打补丁树 pristine_int)
  ...FROM position_lifecycles
     WHERE environment=? AND account_ref=? AND symbol=? AND position_side=?
       AND status='CLOSED' ORDER BY closed_at_ms DESC,recorded_at DESC LIMIT 1
```
（语句首行在 `:2925`，`FROM position_lifecycles` 落在 `:2926` —— r2 的 `:2926` 引号与 r3 的更正
指向同一条语句。）→ **该护栏查的是 `position_lifecycles`，而该表本来就在清单里**；
r3 的更正与源码逐字一致：把 `trade_reconciliation_jobs` 的迁移理由挂到这条护栏上确实是**引错了表**。

### 4.2 `trade_reconciliation_jobs` 有无按 `account_ref` 的读取 —— **确认没有**

全仓（`src/`）对该表的读取只有 4 处，全部与账户无关：
```
src/audit/database.py            SELECT status,COUNT(*) n FROM trade_reconciliation_jobs {prefix} GROUP BY status
                                 # prefix 只可能是 "WHERE environment=?"
src/exchange/binance_futures.py  SELECT DISTINCT symbol ... WHERE status IN ('PENDING','RETRY','RUNNING')
                                 AND (next_retry_at IS NULL OR next_retry_at<=?)   # 不按账户
src/exchange/binance_futures.py  SELECT attempt_count ... WHERE job_id=?            （两处）
```
`PASS 1_no_jobs_read_filters_account_ref`。→ 迁它属**一致性/整洁**、**不是**功能必需，
README r3 改写的口径与事实一致。

### 4.3 `*_norm` 唯一键与"同一域"推理 —— **r3 改的方向正确（实测）**

```
2_with_account_ref      = ['execution_fills','income_conflicts','income_coverage','income_events',
                           'position_lifecycles','release_observations','trade_reconciliation_jobs']  # 47 表里恰好 7 张
2_with_account_ref_norm = ['income_conflicts','income_coverage','income_events']                       # 仅 3 张 income 表
2_lifecycle_fill_allocations_has_no_account_ref = True
```
income 三表的 `UNIQUE` 逐字为 `UNIQUE(environment_norm, account_ref_norm, income_type_norm, tran_id_norm)`
（coverage 另加 `start_ms,end_ms`；conflicts 另加 `incoming_content_hash`）——**只建在 `*_norm` 列上**。
真实 DB 上的方向实验：
```
INFO 3_after_partial_update account_ref=demo:new account_ref_norm=demo:old     # 只改非归一列
INFO 3_partial_update_insert_same_norm=True      # 再插旧 *_norm 值 -> 撞 UNIQUE
INFO 3b_both_columns_updated_new_row_old_domain=True   # 两列同批更新后旧域可再插
```
→ **"只改 `account_ref` 不改 `account_ref_norm` → 旧域与新域仍被视作同一分区"成立**
（r2 的"视作两个域"确实写反了）。r3 的方向改正**事实正确**，且"两列必须同批更新"的结论必要。

### 4.4 冲突键表逐字一致（旁证）

```
uniq_execution_fills      = [idx_execution_fills_trade (environment,account_ref,trade_id)]        ✓
uniq_position_lifecycles  = [idx_active_position_lifecycle (environment,account_ref,symbol,position_side)] ✓
uniq_release_observations = [(release_id,environment,account_ref,observed_minute)]                ✓
uniq_trade_reconciliation_jobs = [idx_active_trade_reconcile_job (lifecycle_id), PK job_id]       # 无账户相关唯一键 ✓
```
→ README §A 表格的冲突键与真实索引**逐字一致**；`trade_reconciliation_jobs` 在 r3 里也不再声称
"冲突键含账户"。**④ 判定：两处引证与方向都已改对。**

---

## 5. 新增告警 `futures_exit_reservation_sweep_failed` + 06 映射 —— **真闭合**

探针 `probes/probe_q5_sweep.py`（自写；真实 `BinanceFuturesBroker`（`__new__` 桩，不联网/不下单）
+ 真实 `AuditDatabase`）。原始输出 `out_q5_sb_int.txt` / `out_q5_sb_r2.txt`。

```
AFTER（sb_int）  Q_SWEEP_PROBE FAIL=0
INFO 1_res=() events=[('futures_exit_reservation_sweep_failed', 'warn')]
PASS 1_details_has_exception_type          # details 含 'RuntimeError'
PASS 1_returns_empty_fail_closed           # 巡检失败 -> 返回 ()，不放行
PASS 2_audit_failure_does_not_propagate    # 告警自身 db 抛错也不回抛到退出路径
PASS 3_reclaim_event_logged                # futures_stale_exit_reservation_reclaimed
PASS 3_p0_not_regressed_resubmit_after_reclaim   # 正常回收后同键仍可重发
INFO 5_scanned_types=112 missing=[]        # 复刻 smoke_ui 口径，无缺映射
BEFORE（sb_r2）  Q_SWEEP_PROBE FAIL=4
INFO 1_res=() events=[]                    # r2 巡检失败完全静默（无告警）
FAIL 5_map_futures_exit_reservation_sweep_failed   # r2 无该事件
```
- **构造的巡检失败用例**：`manager.reclaim_stale_exits` 抛 `RuntimeError` → 告警真的触发（warn + 异常类型）。
- **告警自身失败**：`db.log_risk_event` 抛错 → 被吞掉，退出路径不受影响（实测 `r2==()`，无 raise）。
- **06 中文映射完整**：`_event_type_cn` 对 7 个新增事件全部有中文；复刻 `smoke_ui` 的
  `event_type="…"` 扫描口径得到 112 个事件类型、**缺映射 `[]`**。**⑤ 判定：闭合。**

---

## 6. 补丁与 delta

### 6.1 两种 git 配置下 `git apply --check`（自跑，`out_applycheck_Q.txt`）
```
cfg = -c core.autocrlf=false -c core.eol=lf  : 01 OK 02 OK 03 OK 04 OK 05 OK 06 OK   (6/6)
cfg = 默认（本机 system autocrlf=true）        : 01 OK 02 OK 03 OK 04 OK 05 OK 06 OK   (6/6)
顺序应用：两种配置都 6/6 成功
补丁涉及文件（grep '^+++ b/'）恰好 6 个目标文件，无越界
```
**EOL 字节精确性（自测，逐字节计数）**：
```
                              pristine_int        seq_lf(=sb_int)      seq_default(autocrlf=true)
trade_reconciliation.py       CRLF 0 / LF 611     CRLF 0 / LF 657      CRLF 657 / LF 0   (bytes 29429 -> 30086)
service.py                    CRLF 0 / LF 1560    CRLF 0 / LF 1722     CRLF 1722 / LF 0  (bytes 95299 -> 97021)
decision_pipeline.py          CRLF 0 / LF 188     CRLF 0 / LF 323      CRLF 323 / LF 0   (bytes 16273 -> 16596)
binance_futures.py            CRLF 1488/LF 1780   CRLF 1712/LF 1707    CRLF 1712/LF 1707  (md5 相同)
execution.py                  CRLF 625 /LF 38     CRLF 703 /LF 38      CRLF 703 /LF 38    (md5 相同)
app.py                        CRLF 1430/LF 859    CRLF 1437/LF 859     CRLF 1437/LF 859   (md5 相同)
```
- `seq_lf` 与 `sb_int` **md5 完全相同** → 按推荐配置应用后补丁**字节精确**。
- 默认配置只把**三条纯 LF 文件整文件转 CRLF**，三条 mixed 文件一字不变 → 与 README 顶部 ⑥ 声明**逐字一致**。
- 两种配置下 `git diff --stat` 都是 `6 files changed, 756 insertions(+), 177 deletions(-)`（只出现真实改动行）
  → README 的"应用后 `git diff --stat` 自检"建议**有效**。属**已登记的操作提示（P3）**。

### 6.2 全量回归 delta（强制项，自写 runner `run_delta_Q.py`）
方法：`ast` 解析**该树自己的** `scripts/run_offline_regression.py` 的 `SCRIPTS`，用与 `main()` 同一套 env
（`EXECUTION_MODE=paper`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、剔除 `MTA_PROFILE`
与 `*BINANCE/SENSENOVA*KEY/SECRET/TOKEN*`），逐条**非 fail-fast**。原始输出 `out_delta_pristine_Q.txt` / `out_delta_sb_Q.txt`。
```
pristine_int  : manifest_entries=80  FAILED=0 []   elapsed=198.7s
sb_int        : manifest_entries=80  FAILED=0 []   elapsed=197.5s
逐条状态行 diff -> IDENTICAL_STATUS_LINES（差集 = {}）
```
- **基线全绿**（80 条 = 79 + U02 登记行，与作者口径一致）；**无任何新增失败**。
- U02 的登记脚本 `scripts/smoke_u02_strategy_position_manager.py` 在 80 条内且 OK。
- 另在 `sb_cur` 单跑官方件：`tests/integration/test_strategy_position_manager.py` → **PASS=158 FAIL=0**；
  `scripts/smoke_u02_strategy_position_manager.py` → **U02 SMOKE OK**（与作者自述一致）。

---

## 7. 门禁（在打过补丁的沙箱里跑 `check_quality.py` + `scan_tracked_secrets.py`）

我建了**两个**打了相同 6 补丁的沙箱，因为基线决定结论：
- `sb_cur` = **当前 I HEAD**（`57b720a`）+ U02 独占文件 + 6 补丁
- `sb_int` = **`I@b4eeda9`**（作者声明的生成基线）+ U02 独占文件 + 6 补丁

```
########## sb_cur（当前 I HEAD） ##########
ruff: All checks passed!
PROFILE LOCK OK / DEPENDENCY LOCK OK (python=3.13 platform=win32)
SECRET SCAN OK files=424 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
QUALITY CHECK OK files=265 untracked=0
  -> check_quality rc=0 ; scan_tracked_secrets rc=0      【全绿】
基线对照 pristine_cur（未打补丁，同一 I HEAD）：check_quality rc=0   【基线亦全绿】

########## sb_int（旧基线 b4eeda9） ##########
ruff: All checks passed! / PROFILE LOCK OK / DEPENDENCY LOCK OK
SECRET SCAN FAIL docs\dispatch\RESUME.md:36 rule=secret-assignment
  -> check_quality rc=1 ; scan_tracked_secrets rc=1
```
**逐条列出与归属判断**（唯一红点）：
1. 命中文件/规则：`docs/dispatch/RESUME.md:36`，规则 `secret-assignment`；
2. 字面量定位：旧基线为 `api_key=<旧基线字面量>`（KI04：此处不复制原文），当前 I HEAD 已改写为 `api_key=<已绑定变量名>`；
3. 6 个补丁只触及 6 个 `src/` 文件，**没有任何补丁触及 `RESUME.md`**（`grep -l RESUME patch890bb7c/*.diff` = 无命中）；
4. `git diff b4eeda9..57b720a` 只改了 3 个 docs 文件（`RESUME.md`、一份 review、任务清单 json）；
5. 同一套补丁打在**当前** I 树上 → 两个门禁**都 rc=0**。
→ **该红点与本任务无关，是旧基线的历史残留**；在 I 当前 HEAD 的补丁沙箱里**门禁全绿**。
（任务书"门禁现已全绿"成立。作者 README 表里说"沙箱仍为红 1 处 RESUME.md"是**旧基线**口径，
现已因 I 的 redaction 提交而消失——建议作者别再把这条列为"沙箱红"。）

---

## 8. 回归抽样 ≥5（我独立断言，`probes/probe_q8_regression.py`）

`sb_int` 与 `sb_r2` 都 `Q_REGRESSION_PROBE FAIL=0`（r3 未引入回归；抽样不变量在交付树成立）。

| # | 不变量 | 我的独立结论（原始输出 `out_q8_sb_int.txt`） |
|---|---|---|
| 1 | **P0 四个异常阶段释放** | A 占量前（无预留）→ 无泄漏；B `_ledger_source` 抛错 → 预留 `RELEASED` + 审计 `futures_exit_reservation_released`；C `_submit_standard_order` 确定性拒绝 → `RELEASED` + 审计；D 成交后 `_resync_protection` 抛错 → `RELEASED`。四阶段之后用**新 key** 均仍 `SUBMIT_REDUCE`（域未被占死）。E 正常成交 → `CONSUMED`（`consumed_id_count=1`）。 |
| 2 | **三分量守恒 / 预留** | 守恒式 `Σ(ACTIVE.remaining)+available == position_qty` 在 reserve / 超额拒绝 / consume / 重复 consume / 仓位缩小溢出释放 每个边界都成立；`available>=0`；重复 consume 不二次扣减。 |
| 3 | **重复退出不反向开仓** | 同 key 二次 = `NOOP/submit_qty=0/reduce_only=True/opens_position=False`；LONG→`SELL` 且请求 9 被夹到 `3`；SHORT→`BUY`；空仓 = `NOOP`（`EXIT_EXCEEDS_POSITION,NO_POSITION_TO_EXIT`）。 |
| 4 | **旧仓显式迁移** | 混合仓 → `MIGRATE_EXPLICIT` + `ALIGN_EXPECTATION`、`exit_action_count=0`、`exit_actions=()`；`migrate_legacy` 缺任一字段抛 `ValueError`；迁移后**下一轮不再被撤销**（`ALREADY_MIGRATED`）；未知 lifecycle 留 `LEGACY_MIGRATION_UNKNOWN_LIFECYCLE`；护栏能检出真实 `close_all` 动作。 |
| 5 | **过期授权不阻断 UNKNOWN 查单** | `now=1e9 > expires_at=1000` → `submit_allowed=False`（`AUTHORIZATION_EXPIRED`）而 `reconcile_allowed=True`；**未过期**时两路都 True（证明不是恒假式）。 |
| 6 | **健康故障不触发全平** | `STOP_LOSS_FAILED/NAKED_POSITION/MARGIN_CALL/HTTP_5XX/TIMEOUT/UNKNOWN_FAULT/垃圾名/空串/None/对象` 全部 `ALERT_ONLY`；`PROTECTION_FAILED/PLACE_FAILED`（含大小写/空白变体）放行；`operator_emergency=True` 恒允许；调用前后 `as_dict()` 逐字不变（纯函数）。 |
| 7 | **R09 并发不重复占量** | 8 线程抢 1.0（各 0.25）→ **恰 4 个 accepted**，`reserved=1.00 available=0`，全部 `reduce_only=True` 且 `opens_position=False`。 |

---

## 9. 新发现（本轮 r3 引入 / 遗留，按严重度）

> 标尺：r2 的 3 条未登记 P2 是本轮要闭的核心。**这 3 条我全部独立复现为"已闭合"**（§1/§2/§3）。
> 本轮**没有发现任何新的未登记 P2**，也没有 fail-open / 崩溃 / 重复提交 / 预留泄漏。
> 以下 4 条都是 **P3（文档口径 / 潜在无调用方 API / 观察）**。

- **NEW-A（P3，文档口径，建议改一行）**：README r3-1 写"已真修"、模块 docstring 写
  "已 `CONSUMED` 的键永不回收"，但两处都没写"**仅在进程内**"。实测跨重启同键会再次
  `SUBMIT_REDUCE`（§1.3）。事实本身**已登记在 P2-9**（"全部状态进程内易失，跨重启的重复
  退出识别与在途预留不成立"），所以**不是未登记缺陷**；但 r3-1 的措辞与 P2-9 并列阅读时
  容易被读成矛盾。建议在 r3-1 末尾加一句"（进程内；跨重启见 P2-9）"。

- **NEW-B（P3，潜在缺陷，**当前无调用方**）**：`StrategyPositionManager.open_ledger()`
  （`src/core/strategy_position_manager.py:576-583`）**无条件**用新账本覆盖 `self._ledgers[key]`。
  实测：一个 CONSUMED 键在该调用后 `is_consumed` 变回 `False`、同键再次 `SUBMIT_REDUCE`
  （`INFO I_open_ledger before_consumed=True after_consumed=False same_key=SUBMIT_REDUCE`）。
  这与 r3-1 刚建立的"成交事实只增不淘汰"直接冲突。**缓解事实**：全仓（`src/`/`scripts/`/`tests/`）
  `open_ledger` **零调用方**（r2 report 也未见调用方），所以当前不可达。
  **建议**：要么删掉这个死方法，要么改成"已存在即返回既有账本"。

- **NEW-C（P3，观察，可达性极窄）**：域漂移（`lifecycle_id` 变化）**叠加** `_exit_keys`
  被 `MAX_EXIT_KEYS` 淘汰时，同一个 `exit_key` 会被判为新请求并重新占量：
  ```
  INFO H_exit_keys_has_kh=False
  INFO H_drift_plus_evicted={'action': 'SUBMIT_REDUCE', ...}     # 重新提交
  ```
  生产上 `lifecycle_id` = `_exit_domain_key()` = `env:account_ref:SYMBOL:SIDE`，只有
  `account_ref` 在进程内变化才会漂移（即"中途声明 `MTA_ACCOUNT_REF`"这种已登记为阻断级的
  操作），且还需同域累计 >2000 个 exit key。**两者同时成立才可达**，后果仍是
  `reduce_only` 减仓 + 可用量夹逼。属**纵深防御的边界**，与 P2-9/P2-11 同源，建议并入其说明。

- **NEW-D（P3，观察，已在 §3 记录）**：r3-3 的"同轮抑制"使**同轮两笔指纹完全相同**的部分平
  被合并为一笔（0.6 仓、两笔 0.3 → 本轮只平 0.3，剩 0.3 下一轮平）。这是抑制语义的**必然代价**，
  不反向、不超量、下一轮自愈，但 README 只写了"同轮抑制"、没写该后果。建议补一句。

- 说明：r2 的 P3 项（NEW-4 引证、NEW-5 无告警、NEW-6 EOL 提示）本轮**都已真修**：
  NEW-4 → §4 已改对；NEW-5 → §5 已加告警且告警自身失败不回抛；NEW-6 → README 顶部 ⑥ 已写明。

---

## 10. 命令清单（可复跑；全部落在 `_scratch_Q/`）

```bash
W="C:/Users/Tao/Desktop/币安/mat-wt/reviews/U02/R3/r1"
cd "$W/_scratch_Q"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"

# 0) 沙箱（pristine_int / sb_int / sb_r2；6 补丁两种配置 apply --check）
bash setup.sh                 # IBASE=b4eeda9 + U02 独占文件(d6fcf51) + README 登记行；建 pristine/sb_int/sb_r2
bash chk_applies.sh           # 两配置 6/6 apply --check + 顺序应用 6/6（-> out_applycheck_Q.txt）
bash mksbcur.sh               # 当前 I HEAD 的 pristine_cur；再手工建 sb_cur（git init + 6 补丁）

# 1) ①/② 成交事实 / release 幂等 / 淘汰边界 / 重启语义 / 绕过
"$PY" probes/probe_q1_consumed.py sb_r2   > out_q1_sb_r2.txt   # BEFORE FAIL=22
"$PY" probes/probe_q1_consumed.py sb_int  > out_q1_sb_int.txt  # AFTER  FAIL=1（仅 D_restart，已登记）

# 2) ③ 纸面同轮抑制 + 跨轮仍执行（真实 PaperBroker.execute）
"$PY" probes/probe_q3_paper_round.py sb_r2  > out_q3_sb_r2.txt   # BEFORE FAIL=2
"$PY" probes/probe_q3_paper_round.py sb_int > out_q3_sb_int.txt  # AFTER  FAIL=0

# 3) ④ 迁移 README 引证（源码 + 真实 schema）
"$PY" probes/probe_q4_migrate.py sb_int > out_q4.txt             # FAIL=0

# 4) ⑤ 巡检失败告警 + 06 映射
"$PY" probes/probe_q5_sweep.py sb_r2  > out_q5_sb_r2.txt   # BEFORE FAIL=4
"$PY" probes/probe_q5_sweep.py sb_int > out_q5_sb_int.txt  # AFTER  FAIL=0

# 5) ⑥ 全量 delta（非 fail-fast，逐条退出码）
"$PY" run_delta_Q.py pristine_int out_delta_pristine_Q.txt     # FAILED=0，80 条
"$PY" run_delta_Q.py sb_int       out_delta_sb_Q.txt           # FAILED=0，80 条；状态行 diff = {}

# 6) ⑦ 门禁（两个基线各跑一遍）
(cd sb_cur && PYTHONUTF8=1 EXECUTION_MODE=paper MTA_NO_PROXY=1 "$PY" scripts/check_quality.py)      # rc=0
(cd sb_cur && PYTHONUTF8=1 EXECUTION_MODE=paper MTA_NO_PROXY=1 "$PY" scripts/scan_tracked_secrets.py) # rc=0
(cd sb_int && ... scripts/check_quality.py)   # rc=1，唯一红点 docs/dispatch/RESUME.md:36（旧基线残留）

# 7) ⑧ 回归抽样
"$PY" probes/probe_q8_regression.py sb_int > out_q8_sb_int.txt     # FAIL=0
"$PY" probes/probe_q8_regression.py sb_r2  > out_q8_sb_r2.txt      # FAIL=0

# 8) 官方件（在 sb_cur）
(cd sb_cur && "$PY" tests/integration/test_strategy_position_manager.py)   # PASS=158 FAIL=0
(cd sb_cur && "$PY" scripts/smoke_u02_strategy_position_manager.py)        # U02 SMOKE OK
```

## 11. 未覆盖 / 无法离线验证（明说）

1. **无实盘**：所有 Broker 路径用 `__new__` + 方法替换桩掉网络/账本副作用；**未下单、未撤单、未联网**。
   探针 ①/③/⑤/⑧ 是**代码路径级**证明。
2. **`_consumed_ids` 的"重启语义"只能在进程内证明，且已证明为"不跨重启"**（§1.3）；I 没有
   多进程/多实例的共享账本，R09 只在单进程成立（P2-9 固定）。
3. **P0/P1 的触发频率未量化**；`_open` 的 qty≈0 分支可达性仍取决于上游过滤（与作者登记一致）。
4. **迁移未在生产库规模验证**：§4 只在真实 schema 的临时空库上跑，锁时长/耗时不适用；**未读
   `.env`/`data/`/密钥/账户标识**。
5. **I 是移动靶**：会话期间 I HEAD 由 `3ec7802` → `57b720a`。delta/gate 我固定了基线
   （delta 用 `b4eeda9`，门禁另跑 `57b720a`），并核对 6 个补丁目标文件 blob 三次 HEAD 逐字相同。
6. **新增观察 3 条（NEW-A~D）未在任何门禁里**（都是文档/潜在 API），未做端到端可达性穷举。
7. **未审 U15/K01/K04 本体**；只审 U02 使用到的接口与行为。
8. r3 的 `d6fcf51` 相对 `890bb7c` 还有 docs/lint 追加提交；我按任务指定用 `890bb7c` 目录的 6 条补丁，
   未对 `890bb7c..d6fcf51` 的文档改动做逐字审（那些不在补丁内、不进门禁行为面）。

---

## 12. 结论：**PASS-with-notes**

### 逐项判定（1–8）

| # | 项 | 判定 | 我的证据 |
|---|---|---|---|
| 1 | **① CONSUMED 被淘汰后 TTL 回收并重发** | **真闭合（进程内）** | BEFORE `FAIL=22`（含 `C1 … STALE_EXIT_RESERVATION_RECLAIMED`）→ AFTER `FAIL=1`（仅 D_restart 内存态，P2-9 已登记）；淘汰边界 2000/2001、淘汰后 reserve/release、`_exit_keys` 淘汰、域漂移、6×500 并发混沌全部**未绕过** |
| 2 | **② `release()` 幂等性** | **真修** | RELEASED/CONSUMED/已淘汰但成交过/未知/ACTIVE 五类状态全测：CONSUMED 与"已淘汰+成交过"一律 `ALREADY_CLOSED` 且**状态不倒退**、**无二次扣减/二次预留** |
| 3 | **③ 纸面同轮抑制声明** | **行为已与声明一致（两侧都验）** | 同轮 `duplicate_skipped`（用例 1/5/7）**且**跨轮/5 轮/部分平+剩余/跨标的全部 `EXECUTED`（2/2b/3/4/6/11）——**没修一侧坏另一侧** |
| 4 | **迁移 README 两处引证** | **两处都改对** | `:2925-2928` 查 `position_lifecycles`（源码逐字）；4 处 `trade_reconciliation_jobs` 读取**无一按 `account_ref`**；`*_norm` 唯一键实测"只改非归一列 → 仍视作同一域"（r3 方向正确）；冲突键表逐字一致 |
| 5 | **新增告警 + 06 映射** | **闭合** | 巡检失败真的记 `futures_exit_reservation_sweep_failed`（warn + 异常类型）；告警自身 db 失败不回抛；112 个事件类型**缺映射 `[]`** |
| 6 | **补丁与 delta** | **通过** | 两配置 6/6 `apply --check` + 6/6 顺序应用；`seq_lf == sb_int` 字节（md5）相同；EOL 行为与 README 声明逐字一致；**全量 delta 两树 `FAILED=0`、状态行差集 `{}`** |
| 7 | **门禁** | **全绿**（在打过补丁的当前 I 树上） | `sb_cur`：`check_quality` rc=0、`scan_tracked_secrets` rc=0、基线 `pristine_cur` 亦 rc=0。`sb_int`（旧基线）唯一红点 `docs/dispatch/RESUME.md:36` = **旧基线历史残留**（补丁不触及该文件，当前 I 已 redaction） |
| 8 | **回归抽样 ≥5** | **通过** | 7 组独立断言全 PASS：四阶段释放 / 三分量守恒 / 不反向开仓 / 显式迁移（不撤销 + 护栏有效）/ 过期授权不阻断 UNKNOWN / 健康故障不全平 / R09 并发恰 4 个 |

### 为什么是 PASS-with-notes 而不是 PASS / 也不是 CHANGES_REQUESTED
- **不是 CHANGES_REQUESTED**：r2 的 3 条**未登记 P2 全部真闭合**（§1/§2/§3 前后对照），
  迁移的两处引证/方向**都改对**（§4），巡检告警从无到有（§5），全量 delta 无新增失败（§6），
  门禁在补丁沙箱**全绿**（§7），回归抽样无回归（§8）。
  **本轮没有发现任何未登记的 P2、fail-open、崩溃、重复提交/预留泄漏、或既有脚本新增失败** ——
  这正是 r2 判 CHANGES_REQUESTED 的四条触发条件，本轮**无一成立**。
- **不是 PASS**：有 **4 条 P3 残留**需要处理口径（NEW-A 文档措辞、NEW-B 死 API `open_ledger`
  会抹掉成交事实、NEW-C 域漂移+键淘汰的极窄边界、NEW-D 同轮同指纹部分平被合并的后果未登记）。
  它们都不阻塞、后果有界、且**其中 3 条已在 README 有对应登记或可由一行文档补齐**，
  但严格按"如实登记"标尺，仍有 2 处未在 README 显式写出（NEW-B / NEW-D），故取
  **PASS-with-notes**。

### 建议（非阻塞，按优先级）
1. **NEW-B**：删除或修正 `open_ledger()`（唯一会抹掉成交事实的入口，零调用方）。
2. **NEW-A**：r3-1 末尾补"（进程内；跨重启见 P2-9）"。
3. **NEW-D**：r3-3 补一句"同轮两笔指纹完全相同的部分平会被合并为一笔，剩余在下一轮处理"。
4. **NEW-C**：并入 P2-9/P2-11 的说明（域漂移 + 键淘汰的联合边界）。
5. README 顶部 ⑥ 建议保留（实测有效），且作者不必再把"沙箱红 1 处 RESUME.md"列为遗留 —— 那
   是旧基线残留，当前 I 树上已消失。

# U14 共享适配补丁包（作者 E，线 EF，任务 U14）— 修复轮 r3

- **补丁基线（= 将被应用的那棵树）**：`task_base_commit = 59660b26f71f5c7057847bfb0caced5d88776218`。
- **集成 HEAD 兼容面**：本包交付时的当前集成 HEAD 是
  `fdd208c76f6c40b9f8fc4df5a3fd4b4097f3cd0c`（`mat-wt/I`，分支 `integration/unattended-v3`）。
  四个目标文件里 **三个**在 `59660b2` 与 `fdd208c` 上**逐字节相同**（blob id 已核对：
  `scripts/health_watch.py` = `cafa7497`、`src/audit/database.py` = `c9ccf6a0`、
  `src/core/health_watch.py` = `06ba9773`）；**只有**
  `scripts/run_offline_regression.py` 漂移了（`8dcaed98` → `47d16691`，因为 U02 的登记行被
  追加在 U13 之后）。因此 **01/02/03 的 diff 对两棵树都有效**，而 **04 本轮特意改成
  "尾部锚定" hunk**（0 行前导上下文 + 4 行后随上下文），使**同一个** 04 在
  补丁基线 `59660b2` 与当前集成 HEAD `fdd208c` 上**都能干净应用**（见 §1.1、§2）。
- **本包由补丁基线直接生成**：`git diff` 于 `mat-wt/EF`（分支 `task/U14`）。
- 共享文件**未**提交进 `task/U14`；工作树里的改动只用于生成 diff。
- **不涉及**契约目录与 `scripts/run_offline_regression.py` 的代码归属（后者只给登记行补丁，见 04）。
- 本轮（r3）针对独立复验 `docs/dispatch/reviews/U14/6ef116b/Q_r3.md` 的
  **1 处 P1（P1-4）+ 2 处 P3（P3-6/P3-7）** 做了修复。其中 **P1-4/P3-6 落在 U14
  独占文件**（`src/core/ops/alert_delivery.py` 等，不在本补丁包内）；本包**只更新 04**
  （P3-7.2 的适用范围问题），**01/02/03 逐字节未变**。

## 1. 补丁清单

| # | 文件 | 目标文件 | 作用 | r2→r3 |
|---|---|---|---|---|
| 01 | `01_core_health_watch.diff` | `src/core/health_watch.py` | RuntimePaths 单一事实源接线 + 交易证据时间只读探针 + 两类失联成因分开 | **未变**（sha256 同 r2） |
| 02 | `02_scripts_health_watch.diff` | `scripts/health_watch.py` | `--state-root` 显式注入点；核心补丁缺失时**显式拒绝**而非静默忽略 | **未变**（sha256 同 r2） |
| 03 | `03_audit_database_alert_deliveries.diff` | `src/audit/database.py` | 告警投递**审计镜像**表 + `record_alert_delivery`/`fetch_alert_deliveries` | **未变**（r2 已同步 P1-3） |
| 04 | `04_run_offline_regression_registration.diff` | `scripts/run_offline_regression.py` | **登记行**（I 独占文件，仅一行 + 注释，无逻辑改动） | **改为尾部锚定 hunk**（P3-7.2） |

### 1.1 r3 对补丁 04 的改动（P3-7.2：同一份 04 要能落在两个不同的集成状态上）

r2 的 04 是常规 3 行上下文 hunk，锚在 `"scripts/smoke_u13_supervision.py",` 之后、
元组闭合括号 `)` 之前。集成树在 U13 之后又追加了 **U02 的登记行**后，该位置的
**后随上下文**变了 ⇒ `git apply --check` 报 `patch does not apply`
（Q r3 §4.1 观测到的现象；本轮在 `fdd208c` 上独立复现，rc=1）。

改法：把 04 改成**尾部锚定**的统一 diff——**0 行前导上下文 + 4 行后随上下文**
（`)` / 空行 / `EXCLUDED_SMOKE_SCRIPTS = {` / 其下第一行注释）：

```
diff --git a/scripts/run_offline_regression.py b/scripts/run_offline_regression.py
@@ -101,4 +101,6 @@ SCRIPTS = (
+    # U14（E 运维）：外部告警送达/心跳交易证据/主机失联检测离线冒烟。
+    "scripts/smoke_u14_alert_delivery.py",
 )
 
 EXCLUDED_SMOKE_SCRIPTS = {
```

语义：**把 U14 登记为 `SCRIPTS` 元组的最后一项**。两棵树的元组**尾部锚点
（`)` + `EXCLUDED_SMOKE_SCRIPTS = {`，及其后注释）逐字节相同，故同一个 hunk 在
`59660b2` 上落在 U13 之后、在 `fdd208c` 上落在 U02 之后，**两处都正确**
（已实跑核对落盘位置，见 §2）。这仍属"一行 + 注释、无逻辑改动"。
（`git apply` 默认拒绝 **0 上下文**的 hunk；本 hunk **有** 4 行上下文，只是全部位于
插入点之后，故不需要 `--unidiff-zero`。）

### 1.1 r2 对补丁 03 的同步（F r1 §5「若 P1-3 的处置引入绑定字段，此表需同步」）

`core.ops.alert_delivery` 的 P1-3 引入了**回执身份绑定**。审计镜像是投递事实的
副本，必须同口径，故 `record_alert_delivery` 增加：

- 形参 `bound_incident_key: str | None = None`：本镜像的身份是 `incident_key`，
  凡提供 `provider_receipt` 或 `device_receipt` 就必须同时给出该绑定且与本行
  `incident_key` 相等；缺省/不一致分别拒绝为 `RECEIPT_UNBOUND` /
  `RECEIPT_BINDING_MISMATCH`（`ValueError`，带原因码）。
- 回执**类型**校验：非字符串（如 `12345`）一律 `RECEIPT_TYPE_INVALID` 拒绝，
  **不做** `str()` 强转（与队列侧同口径）。
- 绑定一致后，本行主键 `(environment, incident_key, attempt)` 即“回执归属”的落库证据；
  **不新增列、不新增迁移**（`alert_deliveries` 仍是随 `SCHEMA` 幂等落地的新表）。

**兼容性**：该 API 的唯一消费者是 U14 自己的验收/冒烟；两者已同步（负例仍先命中
“缺 device_receipt”那条，错误串不变）。生产消费者为止（见 §7.3）。

应用（**必须在仓库根**，`git -C <树> apply <绝对路径>/<补丁>`；补丁路径按 `<树>` 解析，
写相对路径会找不到文件）：

```bash
BASE=C:/Users/Tao/Desktop/币安/mat-wt/I      # 集成树（本包交付时的集成 HEAD：fdd208c）
P=C:/Users/Tao/Desktop/币安/mat-wt/EF/docs/dispatch/patches/U14/ad2c57d
for f in 01_core_health_watch 02_scripts_health_watch \
         03_audit_database_alert_deliveries 04_run_offline_regression_registration; do
  git -C "$BASE" -c core.autocrlf=false -c core.eol=lf apply "$P/$f.diff"
done
```

**适用范围（P3-7.2 改正）**：这套 diff 对**补丁基线 `59660b2`** 与**本包交付时的集成
HEAD `fdd208c`** 都有效（§2 已逐条实测）。若集成树**已经**含有 U14 的登记行（例如 I
已手工落地过 r2 的登记），则 **04 会因"改动已存在"而 `apply` 失败**，此时只需应用
**01/02/03**，或用 `git apply --check` 先探测；这不是补丁缺陷。

## 2. `git apply --check`（两种 git 配置 × 两棵树）

验证方式：`git archive <commit> | tar -x` 导出目标树 + `git init` + `git add -A`，
**在仓库根**逐个补丁 `apply --check`（子目录执行会静默 no-op 造成假绿，故在根执行并
另用 `git diff --stat` 核对真实落盘面）。本机全局 `core.autocrlf=true`
（`git config --show-origin --get core.autocrlf` → `file:C:/Program Files/Git/etc/gitconfig  true`），
“默认”一列即该更严口径。

| 补丁 | 基线 `59660b2`：`-c core.autocrlf=false -c core.eol=lf` | 基线 `59660b2`：默认（autocrlf=true） | 集成 HEAD `fdd208c`：前者 | 集成 HEAD `fdd208c`：默认 |
|---|---|---|---|---|
| 01_core_health_watch | **OK** | **OK** | **OK** | **OK** |
| 02_scripts_health_watch | **OK** | **OK** | **OK** | **OK** |
| 03_audit_database_alert_deliveries | **OK** | **OK** | **OK** | **OK** |
| 04_run_offline_regression_registration | **OK** | **OK** | **OK** | **OK** |

（r2 曾记录 04 在当时的集成 HEAD 上 `rc=1: patch does not apply`；本轮改为尾部锚定后
两棵树 × 两配置 **16/16 rc=0**。落盘位置已实跑核对：`59660b2` 上 U14 紧接 U13、
`fdd208c` 上 U14 紧接 U02，两处都在 `SCRIPTS` 元组内、`)` 之前，且
`ast.parse` 均通过。）

落盘面核对（根目录，`git -c core.autocrlf=false diff --stat`）：

```
 scripts/health_watch.py           |  41 ++++++-
 scripts/run_offline_regression.py |   2 +
 src/audit/database.py             | 223 ++++++++++++++++++++++++++++++++++++++
 src/core/health_watch.py          | 170 ++++++++++++++++++++++++++++-
 4 files changed, 424 insertions(+), 12 deletions(-)
```

## 3. 迁移版本：**没有**新增迁移 ⇒ **没有**需要伴随补丁的版本断言

`03` 里新增的 `alert_deliveries` 表放在**模块级 `SCHEMA`**（`AuditDatabase.__init__`
每次打开都 `executescript(SCHEMA)`，`CREATE TABLE IF NOT EXISTS` 幂等），
**不写 `schema_migrations` 行**，因此：

- `current_schema_version()` 仍为 **17**（已在验收与冒烟中直接断言，见 §5）；
- 全仓 `versions == list(range(1, 18))` 形式的 9 处既有断言**全部照旧通过**。

逐条列出（`git grep` 全仓搜索结果，含路径与行号）：

| # | 文件:行 | 断言 | 迁移 18 会不会打破 | 本包处置 |
|---|---|---|---|---|
| 1 | `scripts/smoke_db_migrations.py:37` | `== list(range(1, 18))` | 会 | **不触发**（无迁移） |
| 2 | `scripts/smoke_db_migrations.py:78` | `== list(range(1, 18))` | 会 | **不触发** |
| 3 | `scripts/smoke_account_mirror.py:103` | `== list(range(1, 18))` | 会 | **不触发** |
| 4 | `scripts/smoke_exchange_event_inbox.py:61` | `== list(range(1, 18))` | 会 | **不触发** |
| 5 | `scripts/smoke_exchange_safety_monitor.py:95` | `== list(range(1, 18))` | 会 | **不触发** |
| 6 | `scripts/smoke_order_mirror.py:188` | `== list(range(1, 18))` | 会 | **不触发** |
| 7 | `scripts/smoke_order_outbox.py:212` | `== list(range(1, 18))` | 会 | **不触发** |
| 8 | `scripts/smoke_protection_mirror.py:119` | `== list(range(1, 18))` | 会 | **不触发** |
| 9 | `scripts/smoke_fault_matrix.py:565-573` | 手工 `INSERT ... VALUES(18,…)` + `current_schema_version() == 18` | 会（UNIQUE 冲突） | **不触发** |

> 该表是 U01（迁移 17 打破上述 9 处）的**主动规避**：两个方案（新增迁移 18 + 9 份伴随补丁；
> 或版本中性的幂等建表）中选了后者，因为本表**没有**需要被迁移顺序约束的历史数据形态。
> r2 只在该**新表所属方法**上加参数与校验，**没有**触碰表结构版本策略。
>
> 其余扫描结果：`tests/acceptance/test_supervision.py:121/785/878`、
> `scripts/ops/verify_recovery_plan.py:49-50` 是**自建 raw sqlite fixture**（手工插 17），
> 与迁移集合无关；`tests/integration/test_income_reconciliation.py:1043` 是 `>= 17`（单调，安全）；
> `scripts/report_demo_acceptance.py:58`、`scripts/create_recovery_bundle.py:128`、
> `scripts/restore_recovery_bundle.py:169` 是 `MAX(version)` 形式（安全）；
> `docs/*.md` 里写死的 `current_schema_version() == 16` 是 U01 已登记的**文档级**陈旧值
> （KI：`docs/双进程双账本改造设计_2026-09-15.md:222,224`、
> `docs/实盘账户并行接入评估与上线整改清单_2026-09-15.md:202,363`），归 I/U01，本任务不改。

## 4. 共享列 / 字段的既有语义与消费者（写入前三方核对）

**写入面**：本包**不写任何既有表/既有列**。`03` 新增的 `alert_deliveries` 是**新表**，
新列语义在 `record_alert_delivery` docstring 里逐条声明（新表无既有消费者，故不存在
“同一列被赋予第二语义”的风险）。要点：`first_occurred_at`（ISO-8601 UTC 文本，与
`release_observations.observed_at` 同口径，供人读/报表）与 `first_occurred_at_ms`
（epoch 毫秒整数，与 `execution_fills.trade_time_ms` 同类口径，供计算/跨表比较）
**指同一个时刻**；`state_rank` 只是 `ALERT_STATE_ORDER` 下标，用于 SQL 侧状态单调守卫，
**不是**新的状态枚举（枚举只认 K05）。

r2 新增的队列列 `alert_queue.receipt_bound_alert_id`（回执绑定落库）位于 **U14 自己的
队列文件** `<state_root>/ops/alert_queue.sqlite`，**不是**共享列、不在交易库、不进
`schema_migrations`；对既存队列文件用幂等 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`
补列（无历史数据需迁移顺序约束），库文件损坏时仍是响亮的 `sqlite3.DatabaseError`。

**读取面**（`core.health_watch.probe_trading_evidence` → `TRADING_EVIDENCE_SOURCES`）：

| 读取的既有列 | 既有语义 | 既有消费者（已核实） |
|---|---|---|
| `release_observations.observed_at` | 交易服务每个快循环写入的**观察证据时刻**（ISO-8601 UTC，`audit.database.utc_now()` 口径，同一 release/环境/账户/UTC 分钟幂等） | `core.health_watch.probe_latest_observation`（证据流时效）、`core.release_evidence`（观察窗口覆盖与门禁分母） |
| `execution_fills.received_at` | 本地**收到**成交事实的时刻（ISO-8601 UTC 文本） | `core.release_evidence`（成交完整性/去重）、`core.exchange.trade_reconciliation` |
| `protection_reconciliations.checked_at` | 保护单对账**完成**时刻（ISO-8601 UTC 文本） | `core.health_watch.probe_protection`（保护对账时效/不匹配） |

**未纳入**（诚实登记，避免口径混用）：`orders.updated_at`、`trade_decisions.created_at`
—— 它们的写方时钟来源（`update_order_status(..., updated_at=?)` 的调用方）不在本任务
已核实范围，按“不猜语义”原则**不**计入交易证据时间。因此交易证据时间可能**晚于**
真实成交发生时刻的最大偏差 = 该两次写入间隔（见 §7 known_limits）。

另外 `health_watch` 新增的 `HealthResult` 字段（`trading_evidence_at` /
`outage_kind` / `state_root_source` / …）与新增原因码（`host_down` / `trading_stalled` /
`host_continuity_unknown` / `host_booted`）都是**加法**：既有原因码
（`process_missing` / `process_unverified` / `disk_unavailable` / `observation_stale` /
`fast_cycle_*` / `protection_reconcile_*` / `protection_mismatch`）的**产生条件与命名未变**，
`scripts/smoke_health_watch.py`（既有消费者）在打补丁前后都是 `ok=28 fail=0`。

r2 在 `classify_outage` 里新增原因码 `PROCESS_UNVERIFIED`（`process_alive is None` 时
**追加**）：`kind` 不变，`OUTAGE_CATEGORY_NAMES` 只按 `kind` 取值，故 `HealthResult.reasons`
（既有消费者口径）不受影响；它只出现在新增字段 `outage_reason_codes` 里。

## 5. r2/r3 修复对照与两态/delta 自证

### 5.0 r3：P1-4 的修复前后对照（原始输出，任务强制项）

探针：`_u14r3_out/probe_isolation.py <tree> <out.json>`（U14 独占，离线、内存 raw sink；
原始输出 `_u14r3_out/logs/probe_before.log`（`6ef116b`）/ `probe_after.log`（`ad2c57d`）、
结构化 `_u14r3_out/logs/probe_*.json`）。场景 = 3 条同批（第 1 条坏、后 2 条正常），
时间线 `0s → +1s → +31s → +300.001s → +3600s`。

**向量 1：通道自身抛异常（真实网关最可能的中断路径）**

```
修复前（6ef116b：except 只收 AlertLadderViolation/AlertReceiptRejected）
  transport_raises_TimeoutError
    轮次: 每轮('escaped': True, exception: TimeoutError: synthetic gateway fault)   # 0/1/31/300.001/3600s 全部穿出
    round0 后的队列状态: {'UNKNOWN': 3}          # 后两条一条都没被提交 ⇒ 整批中断
    坏行: {'attempts': 0, 'next_attempt_ms': 0, 'last_error': None, 'state': 'UNKNOWN'}  # 无退避
    坏行审计: ['ALERT_ENQUEUED']                 # _record_failure 从未执行
    后两条送达: False
  transport_raises_RuntimeError / transport_raises_OSError：逐字相同

修复后（ad2c57d：通道异常就地收敛 + 兜底隔离）
  transport_raises_TimeoutError
    round0(0s) : {'submitted': 2, 'confirmed': 2, 'failed': 1, 'rejected': 0, 'errors': 0,
                  'reason_codes': ['TRANSPORT_EXCEPTION:TimeoutError']}
                 队列状态: {'bad': 'UNKNOWN', 'ok1': 'DELIVERED', 'ok2': 'DELIVERED'}
    +1s        : submitted=0 failed=0（退避窗口内不重派 ⇒ 不再每轮复发）
    坏行: {'attempts': 4, 'next_attempt_ms': <now+3840s>, 'last_error': 'TRANSPORT_EXCEPTION:TimeoutError'}
    坏行审计: ['ALERT_ENQUEUED', 'TRANSPORT_FAILED', 'TRANSPORT_FAILED', …]   # P3-6：标签=传输失败
    后两条送达: True（delivered_at_ms = now）
  transport_raises_RuntimeError / transport_raises_OSError：同上（类名不同）
```

**向量 2：空白回执（本模块自己抛的畸形回执）**

```
修复前
  provider_receipt='   ' -> 穿出 ValueError: provider_receipt required: 回执不得为空
                            （裸 ValueError 不是 AlertReceiptRejected ⇒ 不匹配 except）
                            round0 后 {'SUBMITTED':1,'UNKNOWN':2}（整批中断）
                            坏行 attempts 递增但 next_attempt_ms=NULL ⇒ 每轮复发
  device_receipt='   '   -> 穿出 ValueError: device_receipt required: 回执不得为空；
                            且 +300.001s / +3600s **复发同一异常**
  delivered_at_ms='not-a-number' -> 穿出 ValueError: invalid literal for int()
  delivered_at_ms=[1,2]          -> 穿出 TypeError: int() argument must be …

修复后
  provider_receipt='   ' -> round0 {'submitted': 3, 'confirmed': 2, 'rejected': 1, 'errors': 0,
                                    'reason_codes': ['RECEIPT_EMPTY']}
                            坏行 {'state': 'SUBMITTED', 'next_attempt_ms': <now+30s>,
                                  'last_error': 'TRANSITION_REJECTED:RECEIPT_EMPTY'}
                            审计含 TRANSITION_REJECTED（P3-6）
  device_receipt='   '   -> 同上（bad 行停在 ACCEPTED，未伪造送达）
  delivered_at_ms='not-a-number' -> rejected=1, reason_codes=['RECEIPT_TYPE_INVALID']
  delivered_at_ms=[1,2]          -> rejected=1, reason_codes=['RECEIPT_TYPE_INVALID']
```

**兜底桶与"不过度捕获"**（同一探针）

```
  通道返回 None（非 TransportOutcome）:
      修复前 -> 每轮穿出 AttributeError: 'NoneType' object has no attribute 'ok'
      修复后 -> errors=1, reason_codes=['INTERNAL_ERROR:AttributeError'], 坏行退避、后两条照常送达
  KeyboardInterrupt（BaseException）: 修复前后都**穿出**（隔离层只收 Exception，不吞 Ctrl-C/SystemExit）
  审计表被 DROP（连"记失败+退避"都写不进去）:
      修复后 -> dispatch_due 仍返回（不中断整批），reason_codes 含 PERSIST_FAILED:OperationalError
```

处置（对应 Q r3 §2 的三条建议，采 1+3）：

1. `transport.submit(...)` 单独包一层 `try/except Exception`，收敛为
   `TransportOutcome(False, error="TRANSPORT_EXCEPTION:<类名>")` ⇒ 计入 `failed`；
2. `_bind_receipt` 的空白回执改成 `AlertReceiptRejected((RECEIPT_EMPTY,), field=…)`
   （**仍是 `ValueError` 子类、错误串仍含字段名**，既有负例契约不变）；
3. `int(outcome.delivered_at_ms)` 改成先做类型校验 ⇒ `RECEIPT_TYPE_INVALID`（结构化拒绝）；
4. 兜底：`except Exception`（**不含** `BaseException`）⇒ `INTERNAL_ERROR:<类名>`，计入
   返回结构**新增**的 `errors` 计数；`_isolate_failure()` 保证连"记失败 + 退避"本身
   失败也不抛出。

### 5.1 r2：3 处 P1 的修复前后对照（原始输出）

探针：`_u14r2_probe.py <tree>`（离线、内存 fake sink；原始输出
`_u14r2_out/probe_before.txt` / `probe_after.txt`）。

**P1-1 回执时间略早于本地提交（真实网关常态）**

```
修复前（head 68903f8）
  连续三轮 dispatch_due（now, now+1, now+2）:
      round0: AlertLadderViolation ('TIME_ORDER_VIOLATION',)
      round1: AlertLadderViolation ('TIME_ORDER_VIOLATION',)
      round2: AlertLadderViolation ('TIME_ORDER_VIOLATION',)
     队列快照: {'ACCEPTED': 3, 'UNKNOWN': 1}          # 整批中断，只推进了一条
     触发告警行: {'state': 'ACCEPTED', 'next_attempt_ms': <now+300s>, 'last_error': '',
                  'delivered_at_ms': None, 'attempts': 1}
     t+300s 再派发: AlertLadderViolation（复发）
     各告警终态: ['ACCEPTED','ACCEPTED','ACCEPTED','UNKNOWN']

修复后（head a89eaa5）
  连续三轮 dispatch_due（now, now+1, now+2）:
      round0: return 4/4
      round1: return 0/0
      round2: return 0/0
     队列快照: {'DELIVERED': 4}                        # 整批继续且全部有确定归宿
     触发告警行: {'state': 'DELIVERED', 'next_attempt_ms': None, 'last_error': '',
                  'delivered_at_ms': <submitted_at_ms>, 'attempts': 1}
     t+300s 再派发: return submitted=0 confirmed=0 reasons=[]   # 不复发
```

处置：`[submitted-容差, submitted)` 内的回执按**有界容差**（`RECEIPT_TIMESTAMP_TOLERANCE_MS`
=120 s）归一到 `submitted_at_ms`，并写 `alert_audit` 行 + 返回码
`PROVIDER_CLOCK_SKEW_TOLERATED`（**如实标注，不静默掩盖**）；超出容差或任何阶梯/回执异常
按**单条**隔离：`TRANSITION_REJECTED` + `last_error` + 指数退避重排 + `continue`
（返回结构新增 `rejected` 计数），其余 due 行照常投递。验收套件另用
`_PerAlertSink` 锁住“一条坏回执不拖垮整批”（submitted=4 / confirmed=3 / rejected=1，
且退避窗口内不重派）。

**P1-2 `ops_status` fail-open**

```
修复前
  4a 空队列 + 仅配置 verified=true ⇒ {'status': 'OPS_OK', 'reasons': [], 'counts': {}}
  4b GOOD=DELIVERED, STUCK=SUBMITTED 在 t+10s / t+299s ⇒ OPS_OK（confirmed_states=['DELIVERED']）
  4b-3 t+301s（积压超期）⇒ OPS_BLOCKED（BACKLOG_OVERDUE）

修复后
  4a 空队列 + 仅配置 verified=true ⇒ {'status': 'OPS_DEGRADED',
                                      'reasons': ['NO_CONFIRMED_DELIVERY'], 'counts': {}}
  4b t+10s / t+299s ⇒ OPS_DEGRADED + NO_CONFIRMED_DELIVERY
  4b-3 t+301s ⇒ OPS_BLOCKED（BACKLOG_OVERDUE, NO_CONFIRMED_DELIVERY）
```

判据改为**只看当前**：`if pending > 0 or not confirmed_states: NO_CONFIRMED_DELIVERY`。
即（a）只要还有未闭合的待投递项就报缺确认送达（队列别处的历史 `DELIVERED` **不能**
当健康证据）；（b）当前没有任何 `DELIVERED`/`ACKNOWLEDGED` 证据（含空队列）也**不得**
报 `OPS_OK`。与本模块 docstring「仅将 verified=true 写入配置不通过 OPS」一致。

**P1-3 回执未绑定消息身份 / 无时间戳上界 / 类型不符**

```
修复前
  伪造1 他消息回执(provider_receipt='receipt-of-Y')      -> 接受 'ACCEPTED'
  伪造1b 他消息回执(device_receipt='device-receipt-of-Y') -> 接受 'DELIVERED'
  伪造2 非字符串 device_receipt=12345                     -> 接受 'DELIVERED'（str() 强转）
  伪造3 未来时间戳 delivered_at_ms=now+10^12              -> 接受 'DELIVERED'
  经 transport 的跨消息回执 -> confirmed=1，行 state=DELIVERED

修复后
  伪造1  -> 拒绝 AlertReceiptRejected: RECEIPT_BINDING_MISMATCH
  伪造1b -> 拒绝 AlertReceiptRejected: RECEIPT_BINDING_MISMATCH
  伪造2  -> 拒绝 AlertReceiptRejected: RECEIPT_TYPE_INVALID
  伪造3  -> 拒绝 AlertReceiptRejected: RECEIPT_TIMESTAMP_FUTURE
  最终行 state='ACCEPTED'（未被标成送达）
  经 transport 的跨消息回执 -> submitted=1 confirmed=0 rejected=1
                               reasons=['RECEIPT_BINDING_MISMATCH']，行 state=SUBMITTED
```

处置：`record_accepted`/`record_delivered` 增加 `bound_alert_id`（**缺省 fail-closed**：
未声明绑定即 `RECEIPT_UNBOUND`，绑定他消息即 `RECEIPT_BINDING_MISMATCH`）；回执必须是
字符串（非字符串 `RECEIPT_TYPE_INVALID`，不再 `str()` 强转）；`delivered_at_ms` 必须是
非布尔整数且不晚于 `now + 容差`（否则 `RECEIPT_TIMESTAMP_FUTURE`）；下界仍由 K05
`delivery_violations` 的 `TIME_ORDER_VIOLATION` 严格把关（**不**在此放宽）。绑定值落库到
`alert_queue.receipt_bound_alert_id` 供事后核对。`TransportOutcome`/`RecordingSink` 新增
`receipt_alert_id` 身份声明（fake sink 默认回带本条消息 id）。

### 5.2 SHOULD FIX（P2/P3）逐条状态

| 编号 | 处置 | 位置 |
|---|---|---|
| P2-1 `classify_outage` 不可证也断言 `PROCESS_MISSING` | **已修**：`process_alive is None` 时追加原因码 `PROCESS_UNVERIFIED`（`kind` 不变，与"确证消失"可区分） | `alert_delivery.py` `classify_outage` |
| P2-2 `from_settings` 环境敏感 + 回归入口不清 `MTA_STATE_ROOT` | **如实登记，本包不改**（F 认可）。建议 I 在 `run_offline_regression.py` 的 `main()` 补 `env.pop("MTA_STATE_ROOT"/"MTA_DATA_ROOT")`；U14 自己的冒烟已自行清空两者 | §7.7 + `scripts/smoke_u14_alert_delivery.py:81-82`（`_ENV_STATE_ROOT_BACKUP`；**P3-7.1 已按实际行号改正**，r2 写的 :69-70 是 `check()` 的失败分支打印） |
| P2-3 `ops_status`/`dispatch_due` 尚无生产消费者 | **如实登记**：全树仍无生产消费者，"用户可用的外部告警送达"在本任务范围内**未成立**，须 U12/D02 接线时带故障注入复验；**不得**据此认为 U14 的告警已可用 | §7.3 |
| P3-1 常量名≠值 | **已修**：`MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED == "MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED"`；旧名 `MANUAL_ACK_REQUIRED` 保留为同值别名（不破坏既有导入） | `alert_delivery.py` 常量区 |
| P3-2 已送达后再确认抛异常，违反 `(bool, reasons)` 契约 | **已修**：`record_manual_ack` 捕获阶梯违规并返回 `(False, (TRANSITION_REJECTED, …))`，状态不降级 | `alert_delivery.py` `record_manual_ack` |
| P3-3 未来证据时间被夹成 `age=0` ⇒ 永不 stale | **已修**：证据时间晚于**本地 `now`**（或晚于 `emitted`）即 `TRADING_EVIDENCE_FROM_FUTURE` | `alert_delivery.py` `evaluate_heartbeat` |
| P3-4 只有 SUBMITTED 时 `next_attempt_ms=NULL` ⇒ 每轮重提交 | **已修**：`state == "SUBMITTED"` 也按 `accepted_poll_seconds` 排复查（与 ACCEPTED 同口径） | `alert_delivery.py` `dispatch_due` |
| P3-5 负 uptime 落入启动宽限 / 回执 `str()` 强转 | **已修**：负 uptime 归一为"不可得"⇒ `HOST_CONTINUITY_UNKNOWN`（不猜）；`str()` 强转已由 P1-3 的类型校验取代 | `alert_delivery.py` `classify_outage` / `record_*` |
| **P3-6**（r3 新增）`_record_failure` 对阶梯/回执拒绝也写 `TRANSPORT_FAILED` 审计标签 | **已修**：`_record_failure` 增加 `transition` 形参——传输失败写 `TRANSPORT_FAILED`、阶梯/回执拒绝写 `TRANSITION_REJECTED`、内部兜底写 `INTERNAL_ERROR`；`detail_json` 同时落 `reason/error/attempt/backoff_seconds` | `alert_delivery.py` `_record_failure` / `_isolate_failure` |
| **P3-7.1**（r3 新增）README §5.2 引用 `smoke_u14_alert_delivery.py:69-70` 行号不准 | **已改正**为 `:81-82`（`_ENV_STATE_ROOT_BACKUP` 定义处，`MTA_STATE_ROOT`/`MTA_DATA_ROOT` 的 `pop` 就在这两行） | 本 README §5.2 |
| **P3-7.2**（r3 新增）README §1 的应用示例只指向旧集成 HEAD；04 在漂移后的集成树上 `apply` 失败 | **已改正**：新增 §1.1（04 改尾部锚定）+ §2 增加当前集成 HEAD `fdd208c` 两配置实测（16/16 rc=0）+ 示例改为绝对路径并写明"已含 U14 登记行时只应用 01/02/03" | 本 README §1/§1.1/§2 |

### 5.3 两态与 delta 自证

独立沙箱（`git archive` + `git init`，均在 `mat-wt/EF` 内的临时目录，交付前删除）：

- **s0** = 官方基线 `59660b2`：`OFFLINE REGRESSION OK scripts=79`，逐脚本 **79/79 通过**。
- **s1** = 作者 head（含 U14 独占文件）+ 仅补丁 **04**（登记行）：**80/80 通过**。
- **s2** = 作者 head + 补丁 **01–04**：`OFFLINE REGRESSION OK scripts=80`，逐脚本 **80/80 通过**。

失败集合差集：`s0.FAIL = ∅`，`s1.FAIL = ∅`，`s2.FAIL = ∅` ⇒ **新增失败 0**
（`fail->pass` 与 `pass->fail` 都为空）。本轮**未**出现 `smoke_llm_deadline` 抖动。

两态验收套件（`tests/acceptance/test_alert_delivery.py`）：

| 树 | r2（`a89eaa5`） | **r3（`ad2c57d`）** |
|---|---|---|
| s1（共享补丁**未**应用） | `PASS=194 FAIL=0` | **`PASS=244 FAIL=0`** |
| s2（共享补丁**已**应用） | `PASS=212 FAIL=0` | **`PASS=262 FAIL=0`** |

差异来自 7b 段按两态**各自应有的语义**分别断言（补丁在 → 断言新事实与 `--state-root`
生效、镜像绑定拒绝三类伪造；补丁不在 → 断言旧行为未被伪造、CLI 无 `--state-root`），
**没有 SKIP**。r3 的 +50 项两态一致，全部来自新增的第 10 节（P1-4 隔离回归锁）。

U14 冒烟两态：s1 `SMOKE_U14_ALERT_DELIVERY ok=34 fail=0` → r3 **`ok=51 fail=0`**；
s2 `ok=38 fail=0` → r3 **`ok=55 fail=0`**（新增 [4c] 段 17 项）。

打补丁后既有 `scripts/smoke_health_watch.py`：**ok=28 fail=0**（与打补丁前相同）。
`scripts/smoke_contracts_v3.py`：**PASS 173 / FAIL 0**（golden cases=160，r3 复跑一致）。
打过补丁的沙箱里 `scripts/check_quality.py`：**`QUALITY CHECK OK files=264 untracked=0`**
（`SECRET SCAN OK files=440 registered_false_positives=2 skipped_bare_identifiers=10`）。

### 5.4 delta 复现命令与原始输出（供 F/Q 复核）

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
P=<EF>/docs/dispatch/patches/U14/ad2c57d
# s0 = 官方基线 59660b2（pristine，无补丁）
mkdir -p <T>/s0 && (cd <EF> && git archive 59660b2) | tar -x -C <T>/s0
# s_r2 = r2 候选态（6ef116b + a89eaa5 的 01..04）
mkdir -p <T>/s_r2 && (cd <EF> && git archive 6ef116b) | tar -x -C <T>/s_r2
# s_r3 = r3 候选态（ad2c57d + ad2c57d 的 01..04）
mkdir -p <T>/s_r3 && (cd <EF> && git archive ad2c57d) | tar -x -C <T>/s_r3
for t in s_r2 s_r3; do (cd <T>/$t && git init -q . && git add -A); done
# 逐脚本收集失败集合（官方入口 check=True 首错即停，给不出集合）
for T in s0 s_r2 s_r3; do PYTHONUTF8=1 "$PY" _u14r3_out/harness_r3.py <T>/<T> <out.json>; done
```

`_u14r3_out/harness_r3.py` 用 `importlib` 加载**被侧树自己**的
`scripts/run_offline_regression.py`，逐行复制其 `main()` 的环境构造
（`EXECUTION_MODE=paper`、清 `MTA_PROFILE`、`MTA_NO_PROXY=1`、UTF-8、按名清
`*KEY*/*SECRET*/*TOKEN*`），并**对三棵树统一** `pop` `MTA_STATE_ROOT`/`MTA_DATA_ROOT`
（本机未设置，见 §7.7），对 `SCRIPTS` 逐个 `subprocess.run` 记录返回码。
原始输出（`_u14r3_out/logs/delta_s0.log` / `delta_s_r2.log` / `delta_s_r3.log`，
结构化 `_u14r3_out/delta/*.json`；三棵树**顺序**执行，各 ≈ 8–10 min）：

```
=== s0（pristine 基线 59660b2，79 脚本）===
TOTAL=79 PASS=79 FAIL=0
failed set: []
=== s_r2（r2 候选 6ef116b + a89eaa5 的 01..04，80 脚本）===
TOTAL=80 PASS=80 FAIL=0
failed set: []
=== s_r3（r3 候选 ad2c57d + ad2c57d 的 01..04，80 脚本）===
TOTAL=80 PASS=80 FAIL=0
failed set: []
=== s_r3（官方入口 / 门禁 / 契约）===
OFFLINE REGRESSION OK scripts=80
QUALITY CHECK OK files=264 untracked=0
结果：PASS 173 / FAIL 0；golden cases=160
```

失败集合差集：`s0.FAIL = s_r2.FAIL = s_r3.FAIL = ∅` ⇒ **新增失败 0**（`fail->pass` 与
`pass->fail` 都为空）。本轮**未**出现 `smoke_llm_deadline` 抖动。
注：harness 按**返回码**判定（与官方入口 `check=True` 同口径），因此
`smoke_u02_strategy_position_manager.py` 里的**负向对照 FAIL 行**（脚本自带、exit 0）
不会被误报成新增失败。

作者树**未**应用 04 时，官方入口 fail-closed（设计如此，与 U01/U05/U13 同）：

```
RuntimeError: 未登记冒烟脚本: scripts/smoke_u14_alert_delivery.py
```

## 6. 补丁目录命名

目录名 = **本轮作者交付 head `ad2c57d`**（分支 `task/U14`，r3 修复提交）。历史目录
`7c430c8/`（r1 交付）与 `a89eaa5/`（r2 交付）保留不动。若 I 之后在 `task/U14` 上再追加
提交，请以“补丁基线 `59660b2`”或“本包交付时的集成 HEAD `fdd208c`”为准钉住应用目标；
`apply --check` 已在两棵树 × 两配置上分别验证（§2，16/16 rc=0）。

## 7. known_limits（如实登记，未验证项必须明说）

1. **真实通道未验证**：本任务全程离线，`verified` 通道只出现在夹具/内存 fake sink 中；
   真实送达回执与主通道演练属 U12 OPS（D02）。`OPS_BLOCKED` 是当前真实配置下的**正确**结果。
2. **`host_uptime_seconds` 的平台假设**：Linux 读 `/proc/uptime`，其余平台退回
   `time.monotonic()`（Windows QPC / macOS / Linux 均为“自宿主启动”语义，但该性质
   由平台保证而非 Python 文档保证）。返回 `None` 或**可疑值（含负值，r2 新增）**时一律按
   “不可证明”处理（`HOST_CONTINUITY_UNKNOWN`），**不**猜。离线测试通过注入覆盖，
   **未**在真实宿主上做断电演练。
3. **`ops_status`/`dispatch_due` 无生产消费者（P2-3）**：全树构造 `AlertQueue(` 的点
   全部在验收/冒烟里；真实投递路径尚未接线。因此
   **不得认为 U14 的“无人值守外部告警送达”已可用**；U12/D02 接线时必须带故障注入
   复验（尤其是回执绑定：真实 transport 必须回带 `receipt_alert_id`，否则会被
   fail-closed 拒绝——这是有意的，避免他消息回执伪造送达）。
4. **维护窗口/升级路径只做结构化校验**：`expires_at_ms` 有界性、`escalation_path`
   非空是配置面检查；“真的能联系到人”只能由 U12 OPS 演练证明。
5. **交易证据时间的最小可证粒度**是 §4 三张表的 `MAX(时间列)`；某类证据长期不写
   （例如无成交）时它由其余来源兜底，极端情况下可能晚于真实交易时刻（§4 未纳入项）。
6. **回执时间容差是**“有界”而不是“精确”：`RECEIPT_TIMESTAMP_TOLERANCE_MS = 120 s`
   覆盖供应商/本地时钟偏斜；容差内的偏斜会被**归一并留审计行**（
   `PROVIDER_CLOCK_SKEW_TOLERATED`），因此它**不**等于“供应商时钟无问题”的证据。
   超容差的倒序回执按单条隔离并退避重排（最多 3600 s 一次），**不**伪造送达。
7. **入队是同步 SQLite 写**：队列在独立文件上，`busy_timeout=5000`；本包未做
   “队列写阻塞”压力模型（无并发多进程写队列的验收）。
8. **`alert_deliveries` 不进迁移序列**（§3），因此它没有“迁移回滚”语义；表结构变更
   需要人工处理（与 `SCHEMA` 里其他基础表一致）。队列文件新增列 `receipt_bound_alert_id`
   用幂等 `ALTER TABLE ADD COLUMN` 补，无版本号语义。
9. **`--state-root` 与 `MTA_STATE_ROOT` 共同生效（P2-2）**：`from_settings` 会（与其他
   RuntimePaths 消费者一致地）读取 `MTA_STATE_ROOT`/`MTA_DATA_ROOT`。官方回归入口
   `run_offline_regression.py` **未**清除这两个变量（只清 `MTA_PROFILE`）；本机未设置
   故无影响，本任务新增的冒烟脚本自行清空它们。建议 I 在回归入口补两行
   `env.pop(...)`（本包**不改**该文件的环境构造逻辑，避免超出登记行范围）。
10. **r3 新增：单条隔离的边界（P1-4 的残留，如实登记）**
    - **"兜底桶"会掩盖代码缺陷**：`except Exception` 把"非通道、非阶梯/回执"的异常
      也算成单条失败（`errors` 计数 + `INTERNAL_ERROR:<类名>`）。这是刻意的
      fail-closed（"单条异常不得中断整批"必须对任意异常成立），但**新写的 bug 会表现为
      "该条一直退避失败"而不是响亮的崩溃**。接线（U12）时必须监控 `errors` 计数。
    - **持久化彻底不可用时无法退避**：`_isolate_failure` 保证不抛出，但若队列库连
      `UPDATE alert_queue`/`INSERT alert_audit` 都写不进去（如审计表被破坏），该条
      `next_attempt_ms` 可能保持原值 ⇒ 下一轮仍会到期重试。此时 `reason_codes` 里会出现
      `PERSIST_FAILED:<异常类名>`（探针实测）。整批仍不中断，但"退避"这个保证在
      **库不可用**这一前提下失效——这是无法用同一把库修的，如实登记。
    - **仅覆盖 `Exception`**：`KeyboardInterrupt`/`SystemExit`（`BaseException`）**不**被
      吞（探针实测其穿出 `dispatch_due`），符合"不要过度捕获"的要求。
    - `dispatch_due` 返回结构**新增 `errors` 键**（加法）：`rejected` 只计阶梯/回执拒绝，
      `failed` 只计传输失败；按旧字段消费的调用方不受影响（本模块当前无生产消费者，P2-3）。

11. **r3 登记（Q r3 §2 观察项，不单列编号）：回执绑定的空白归一策略两层不同**
    - 队列侧（`core.ops.alert_delivery._bind_receipt`）：**严格相等**，不做 `strip()`
      ⇒ `" <id> "` / `<id>\n` 判 `RECEIPT_BINDING_MISMATCH`；
    - 审计镜像侧（`src/audit/database.record_alert_delivery`）：行自身的
      `incident_key` 与 `bound_incident_key` **都**先 `strip()` 再比较 ⇒ 上述输入**通过**。
    - **处置：本包有意保持两侧各自现状**（队列侧是 fail-closed 权威口径，镜像侧是"对人可读的
      审计副本"，其归一策略在函数内自洽），**但在此显式写清**，避免被误读成"同口径"。
      两侧的 `RECEIPT_UNBOUND` / `RECEIPT_BINDING_MISMATCH` / `RECEIPT_TYPE_INVALID`
      **拒绝语义**一致，差异只在空白归一。
12. **r3 登记（Q r3 §2 观察项）：`src/audit/database.py` 反向依赖 `core.ops.alert_delivery`**
    的**字符串常量**（函数体内延迟导入 `RECEIPT_BINDING_MISMATCH` / `RECEIPT_TYPE_INVALID` /
    `RECEIPT_UNBOUND`）。延迟导入避免了模块级循环，功能正确；但审计层因此依赖 ops 层的
    常量定义。建议后续把这三个原因码提到中立的契约/常量模块——**超出本补丁包范围，不改**。

## 8. rollback

- 逆向应用同一组 diff 即可：`git apply -R <p>`（顺序 04→01，或直接 `git checkout --`
  四个文件到基线）；`alert_deliveries` 表是新增表，回滚后残留该表不影响任何既有消费者
  （无既有代码引用它）。若要彻底清除：`DROP TABLE alert_deliveries;`
- U14 独占文件的 r3 修复回滚：`git revert ad2c57d`（等价 `git checkout a89eaa5 --
  src/core/ops/alert_delivery.py tests/acceptance/test_alert_delivery.py
  scripts/smoke_u14_alert_delivery.py`）；r2 修复回滚见上一轮包
  `docs/dispatch/patches/U14/a89eaa5/README.md` §8。
- 因为**没有**新增迁移版本，回滚不需要处理 `schema_migrations`。

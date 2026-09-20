# U14 初审（F 角色：发布与可维护性）r1

- 审查对象 head：`68903f8`（分支 `task/U14`）；base `59660b2`（含 U00/U01/U03/U04/U05/U13/U15 的集成 HEAD）
- 独立工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U14\F\r1`（detached，`.git/worktrees/r124`）
- 作者交付物：`src/core/ops/alert_delivery.py`(1620)、`tests/acceptance/test_alert_delivery.py`(933)、`tests/fixtures/alerts/`(6)、`scripts/smoke_u14_alert_delivery.py`(344)
- 共享适配：`docs/dispatch/patches/U14/7c430c8/`（4 diff + README）
- 说明：本报告的一切结论均来自**本轮独立复现**，不采信编排者/作者预先核验结论。所有命令原始输出在 `_scratch_F/*.log`、`_scratch_F/*_result.json`，探针在 `_scratch_F/probe_f*.py`、`_scratch_F/harness_f.py`。

## 0. 结论

**CHANGES_REQUESTED**（有条件：P1 三处修完即可转 GO；无需改契约，无需新增迁移）

发现 11 项：**P1 × 3、P2 × 3、P3 × 5**。没有 P0。
三项 P1 全部集中在**尚未接线**的投递队列库（`AlertQueue`/`dispatch_due`/`ops_status` 在整棵树里只有验收/冒烟构造，无生产消费者），所以是"接线前必修"而非"线上已出故障"；但 U12/D02 一旦接线，第一项会直接卡住告警通路，第二项会让 `ops_status` 谎报 `OPS_OK`。

## 1. 独立复现的全量回归 delta（强制项）

原始输出：`_scratch_F/s0_harness.log`、`s1_harness.log`、`s2_harness.log`、`s0/s1/s2_result.json`。

| 树 | 内容 | 官方入口 | 逐脚本（同一 harness、同一 env、非 fail-fast） |
|---|---|---|---|
| s0 | `git archive 59660b2` | — | `TOTAL=79 PASS=79 FAIL=0`，失败集合 `∅` |
| s1 | `git archive 68903f8` + 仅补丁 04 | — | `TOTAL=80 PASS=80 FAIL=0`，失败集合 `∅` |
| s2 | `git archive 68903f8` + 补丁 01–04 | `OFFLINE REGRESSION OK scripts=80` | `TOTAL=80 PASS=80 FAIL=0`，失败集合 `∅` |

- **失败集合差集 = ∅（fail→pass 与 pass→fail 都为空）**；基线 s0 确为全绿 79/79，**新增失败 0**。
- 补丁 04 之外未登记补丁时官方入口 fail-closed，独立复现成功：
  `RuntimeError: 未登记冒烟脚本: scripts/smoke_u14_alert_delivery.py`（s3 = clean head）。
- 验收两态独立复现：s1 `U14 ALERT_DELIVERY PASS=164 FAIL=0`；s2 `PASS=179 FAIL=0`。
- 冒烟两态独立复现：s1 `SMOKE_U14_ALERT_DELIVERY ok=28 fail=0`；s2 `ok=32 fail=0`（编排者简报写的 28/0 = 未打补丁态；补丁态为 32/0，两者都全绿）。
- 迁移版本 9 处断言：见 §3。

### 1.1 `smoke_llm_deadline` 抖动：**本轮未能复现**

尝试过的复现路径（全部 0 失败）：
1. s2 上连跑 10 次（单进程顺序）→ 10/10 rc=0；s0 上 5 次 → 5/5 rc=0。
2. s2 上 **12 路并发**同时跑 → 12/12 rc=0。
3. 8 个 CPU 占满进程 + s2 上连跑 15 次 → 15/15 rc=0。
4. s2 全量 80 脚本回归 1 轮 → `smoke_llm_deadline` rc=0。

**独立判定（比"不可复现"更强的一条证据）**：该脚本的失败断言是 `SlowHttpClient.calls == 1`（`scripts/smoke_llm_deadline.py:80`），是纯时序断言（0.01s 绝对 deadline vs 0.08s 假 HTTP 延迟）。
- 用 `runpy` 复跑并检查 `sys.modules`：`core.health_watch` / `core.ops.alert_delivery` / `core.runtime_paths` **全程未被导入**；`audit.database` 虽被间接导入，但 **`AuditDatabase` 构造 0 次**（探针 `probe_f` 实测 `AuditDatabase constructed times: 0`），即补丁 03 唯一会执行的那段代码（模块级 `SCHEMA` 的 `executescript`）**从未执行**。
- 结论：即使该抖动在更差的机器上复现，**与本包三个目标文件不存在可执行路径重叠**，只能判定为 U05 该冒烟的时序抖动。**不升级为问题**；但也不把"零影响"写成结论 —— 只写"未能复现 + 无执行路径重叠"。

## 2. 问题清单

### P1-1 `dispatch_due` 未捕获 `AlertLadderViolation`：一条回退时间戳的回执会中断整批投递并反复复发

- 位置：`src/core/ops/alert_delivery.py:1444-1448`（`record_delivered` 调用）→ `:1334`（`_persist`）→ `:1268-1270`（`raise AlertLadderViolation`）
- 复现（`probe_f2.py` §1h / `probe_f3.py` §1h，原始输出在 `_scratch_F/probe_f2.log`、`probe_f3.log`）：
  ```
  sink = RecordingSink(provider_receipt="r", delivered_at_ms=now - 1, device_receipt="d")
  q.dispatch_due(sink, verified_channel(), now_ms=now)
  -> AlertLadderViolation: alert ladder violated: TIME_ORDER_VIOLATION   （异常穿出 dispatch_due）
  队列快照：{'ACCEPTED': 1, 'UNKNOWN': 3}      # 触发告警停在 ACCEPTED，其余 3 条一条都没投递
  三轮派发三轮都抛：raises == 3
  ```
- 机理：`dispatch_due` 先 `record_submitted(now)`（落库 SUBMITTED）再 `record_accepted`，最后用 `delivered_at_ms=int(outcome.delivered_at_ms)` 调 `record_delivered`；K05 的 `delivery_violations` 要求时间戳非降序，因此**任何早于本次本地提交时刻的供应商回执时间**都会触发 `TIME_ORDER_VIOLATION`。`_persist` 在违规时抛异常，异常从 `dispatch_due` 穿出（未收敛为返回字典，也未 `continue`），**for 循环被中断**。
- 可达性：真实网关普遍上报**供应商自己的时间戳**（通常略早于本地接收时刻），故这不是构造性输入。触发后该告警已是 ACCEPTED（`_schedule_recheck` 排到 +300s），每次重排到期后在同一 transport 上**再次抛**，并再次中断整批。
- 影响：告警通路整批停摆 + 该告警永远送不达；与 U14"外部告警送达"的目的直接冲突。
- 期望处置：`dispatch_due` 内对单条告警的阶梯异常做收敛（记录 `TRANSITION_REJECTED`/`last_error` + 退避重排 + 继续处理其余 due 行），或把回执时间归一化（`delivered_at_ms = max(delivered_at_ms, submitted_at_ms)`）——但后者会掩盖供应商时钟问题，建议前者 + 显式原因码。

### P1-2 `ops_status` fail-open：仅凭配置即可报 `OPS_OK`；且任意一条已送达会永久掩盖卡住的待投递告警

- 位置：`src/core/ops/alert_delivery.py:1565-1575`
- 实测（`probe_f.log` §4a、`probe_f2.log` §4b）：
  ```
  4a 空队列 + ChannelPolicy(status="verified", escalation_path=...) ⇒
     {'status': 'OPS_OK', 'blocked': False, 'reason_codes': [], 'confirmed_states': [], 'counts': {}}
  4b GOOD=DELIVERED, STUCK=UNKNOWN(未投递) 在 t+10s / t+299s ⇒
     ('OPS_OK', ())                       # 无 NO_CONFIRMED_DELIVERY
  4b-3 积压 >300s 才 (BACKLOG_OVERDUE) ⇒ OPS_BLOCKED
  ```
- 两个独立缺陷：
  1. **队列为空时 `reasons` 为空 ⇒ `OPS_OK`**。判定门只有 `CHANNEL_UNVERIFIED`/`NO_ESCALATION_PATH`/`BACKLOG_OVERDUE`，全部来自**配置**。因此"仅把 `verified=true` 写进配置"**确实**能把 OPS 变成 `OPS_OK`（空队列时），与本模块 docstring `:1335-1336`「仅将 verified=true 写入配置不通过 OPS」以及 README §4 的表述**直接矛盾**。规格要的是"有真实送达证据才通过 OPS"。
  2. `if pending > 0 and not confirmed_states:` —— `confirmed_states` 是**全队列聚合**，一旦历史上出现过任何一行 DELIVERED，`NO_CONFIRMED_DELIVERY` **再也不会触发**。实测一条永久 UNKNOWN 的告警在 300 秒内一直报 `OPS_OK`。SLO 是"≤60s 提交、≤120s 送达"（`:147-148`），300 秒的盲窗远超 SLO。
- 期望处置：`OPS_OK` 应以"本队列存在满足 SLO 的 `DELIVERED`/`ACKNOWLEDGED` **证据**，且无超期 pending"为条件；pending 与 confirmed 必须按**同一批待投递项**而非全表聚合比较。

### P1-3 送达回执不绑定消息身份：他消息回执 / 非字符串回执都能把本告警标成 DELIVERED

- 位置：`src/core/ops/alert_delivery.py:1319-1341`（`record_delivered`）、`:1305-1317`（`record_accepted`）
- 实测（`probe_f2.log` §回执绑定 / §delivered_at_ms 无上界）：
  ```
  用"属于 Y 的回执"把 X 标成送达：
    q.record_accepted(x, provider_receipt="receipt-of-Y")
    q.record_delivered(x, device_receipt="device-receipt-of-Y")
    -> ('DELIVERED', 'device-receipt-of-Y')          # 接受
  device_receipt=12345（int）-> ('DELIVERED', '12345')  # str() 强转后接受
  未来 10^12 ms 的 delivered_at_ms -> ('DELIVERED', 2789945240610)  # 无上界/合理性检查
  ```
- 唯一门槛是 `str(device_receipt).strip()` 非空（`:1322-1323`）。K05 的 `AlertDelivery` 不含回执字段（`docs/contracts/README.md` L5 明示"无通道能力/dedup/退避/冷却字段；投递缓存语义需自建"），即**回执绑定与去重属于 U14 自建范围**，本包只实现了 enqueue 侧的冷却聚合，未实现回执侧的身份绑定/格式校验。
- 影响：任一 transport 实现（或脱敏后的 fake sink）只要返回非空字符串即可产生 `DELIVERED` 与 `OPS_OK`，是 R14"供应商接受≠送达"之外的第二条伪造送达路径。
- 期望处置：把 `provider_receipt`/`device_receipt` 与 `alert_id`/`incident_key`/`attempt` 绑定后落库并校验（例如回执必须回带 `alert_id`，或记录 `receipt_bound_alert_id` 并在不一致时拒绝），并给 `delivered_at_ms` 加未来上界（如 `now + tolerance`）与下界（`>= submitted_at_ms`）。

### P2-1 `classify_outage` 在进程不可证时仍断言 `PROCESS_MISSING`，与自身 docstring 的"原因码不同"承诺不符

- 位置：`src/core/ops/alert_delivery.py:373-381`；docstring 承诺在 `:346-348`
- 实测（`probe_f.log` §2j）：`process_alive=None, uptime=259200, age=600` ⇒ `{'kind': 'PROCESS_MISSING', 'reason_codes': ['PROCESS_MISSING']}`（**只有** PROCESS_MISSING，无"进程侧不可证"标记）。
- 到达该分支的输入包括"锁文件缺失"（`health_watch` 调用点把缺锁也算成 `None`，见补丁 01），因此实际语义有好有坏；但**消费者无法区分"确证进程消失"与"进程状态不可读"**，而 docstring 明确承诺"原因码不同"。属诚实性/可维护性缺陷。
- 建议：`process_alive is None` 时至少追加一个独立原因码（如 `PROCESS_UNVERIFIED`），或在 docstring 里把口径改成与实现一致。

### P2-2 `HealthWatchConfig.from_settings` 变成环境敏感，而 I 独占的回归入口不清 `MTA_STATE_ROOT`

- 位置：补丁 01 `_core_health_watch.diff`（`from_settings` 里 `resolve_state_base(root, declared=..., env=os.environ)`）；`scripts/run_offline_regression.py` 的 `main()` 只 `env.pop("MTA_PROFILE")`
- 实测（`probe_f3.log` §01）：
  ```
  未声明状态根：s1 与 s2 的 data_root/db 完全一致（== <project>/data/live）  ✅ 兼容
  s2 + MTA_STATE_ROOT=<X> ⇒ data_root = <X>/live ，state_root_source='declared'
  s1 + MTA_STATE_ROOT=<X> ⇒ data_root 仍 = <project>/data/live   （不敏感）
  ⇒ 设置了该变量的机器上，两态/回归结论不同
  ```
- 作者已在 README §7.7 如实登记并建议 I 在回归入口补 `env.pop("MTA_STATE_ROOT"/"MTA_DATA_ROOT")`。**本机未设置该变量，故本轮 delta 不受影响**；但这是可复现性缺口，须由 I 在登记行之外处理（本包不越界，处理方式我认可）。

### P2-3 `ops_status`/`dispatch_due` 目前无生产消费者 —— 须在 U12 接线时带故障注入复验

- 事实（全树 grep）：`core.ops.alert_delivery` 的导入方只有 `tests/acceptance/test_alert_delivery.py`、`scripts/smoke_u14_alert_delivery.py`、以及补丁后的 `src/core/health_watch.py`（只 import 只读探针 `classify_outage`/`trading_evidence_timestamp`/`host_uptime_seconds`）。`AlertQueue(` 的构造点**全部**在测试/冒烟里。
- 影响：P1-1/P1-2/P1-3 目前是"库内缺陷、尚未接线"；同时意味着**没有任何生产代码路径消费 `AlertQueue`**，"用户可用的外部告警送达"在本任务范围内**未成立**（作者 README §7.1 已如实登记，真实通道属 U12/D02）。这不是隐瞒，但 F 角色必须点名：**不得据此认为 U14 的告警已可用**。

### P3 清单

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| P3-1 | `MANUAL_ACK_REQUIRED` 常量名 ≠ 值（`"MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED"`），名字与值不一致会误导以名字过滤日志的消费者 | `alert_delivery.py:191` | `probe_f2.log` §6a |
| P3-2 | 已 DELIVERED 的告警再以更早 `now_ms` 人工确认 ⇒ 抛 `AlertLadderViolation`，而该函数契约是返回 `(bool, reasons)`（调用方只处理元组会崩） | `:1343-1369` | `probe_f.log` §6h（`('AlertLadderViolation:...TIME_ORDER_VIOLATION', 'DELIVERED', None)`） |
| P3-3 | 证据时间在未来时 `age` 被 `max(0.0, ...)` 夹为 0 ⇒ **永不 stale**（时钟异常可长期掩盖停摆）；`evaluate_heartbeat` 只校验 `evidence > emitted`，不校验 `> now`（`now+4s` 判定为健康、age=0.0） | `:352`、`:558-564` | `probe_f.log` §2f、`probe_f2.log` §3f |
| P3-4 | 只有 SUBMITTED（无回执）的告警 `next_attempt_ms=NULL` ⇒ 每次 `dispatch_due` 都重新提交，无退避（与 ACCEPTED 的 +300s 排程、失败退避不一致） | `:1264-1283`、`:1293-1303` | 定向复现：同一个 http200-only sink 连派 3 次 ⇒ `submissions=3`，`state=SUBMITTED`，`next_attempt_ms=None` |
| P3-5 | 负 uptime 落入启动宽限 ⇒ `HOST_JUST_BOOTED`（正解可能是 HOST_DOWN）；`record_delivered` 对非字符串回执做 `str()` 强转 | `:353-359`、`:1322` | `probe_f.log` §2i、`probe_f2.log` §delivered 无上界 |

（另：主库损坏（F22）时 `trading_evidence_timestamp` 明确返回 `available=False` + `SOURCE_UNAVAILABLE:...:DatabaseError`，`evaluate_health` 追加 `disk_unavailable`；但 `TRADING_DB_UNAVAILABLE` 未进入 `HealthResult.reasons`/`outage_reason_codes`，运维侧只能从 `disk.detail` 看到。属已知边界，不单列。）

## 3. 第 7 项独立结论：**"未写任何既有表/既有列"成立**（独立验证，非引用作者）

四条独立证据：

1. **全库 schema 差集**（会话内直接构造 s0/s2 的 `AuditDatabase` 并比对 `sqlite_master` + `PRAGMA table_info` + `PRAGMA index_list`）：
   ```
   s0 tables: 46 ; s2 tables: 47 ; delta: {'alert_deliveries'}
   existing tables whose COLUMNS changed: NONE
   existing-table indexes changed: NONE
   s0 ver: 17 ; s2 ver: 17
   new-table indexes: {'alert_deliveries': ('idx_alert_deliveries_state',)}
   ```
2. **动态行数不变**：向新表写入 SUBMITTED/DELIVERED 审计行前后，**所有既有表行数逐一相同**（`probe_f2.log` §A8 `all-same`）。
3. **静态**：`src/core/ops/alert_delivery.py` 全部写语句（3 × INSERT / 7 × UPDATE / 1 × DELETE / 3 × CREATE TABLE）目标**只有** `alert_queue` / `alert_audit` / `alert_queue_meta`（均在自己的队列文件上，`self.conn`）；`trading_evidence_timestamp`（`:701-744`）只发 `PRAGMA table_info` + `SELECT MAX(...)`，`inspect.getsource` 无任何写关键字。
4. **读面口径**：读的既有列只有 `release_observations.observed_at` / `execution_fills.received_at` / `protection_reconciliations.checked_at`（`:670-674`），三者的既有语义与消费者与 README §4 声明一致（我另行核对了 `docs/contracts/README.md` 与补丁 README §4 的口径描述，未发现"同一列被赋予第二语义"）。补丁 03 的写入目标是**新表** `alert_deliveries`。另：`evaluate_health` 打开交易库用 `mode=ro` + `PRAGMA query_only=ON`（`src/core/health_watch.py:167-169`），不会在交易库目录产生 WAL 写入。

结论：**写面确实只有新表**；U05 曾栽的"悄悄改既有列"这一类问题在 U14 未出现。

## 4. 第 8 项独立结论：**9 处版本断言确实均不触发**

- 全仓 `range(1, 18)` 实为 **8 处**（`grep` 独立复核：smoke_db_migrations 2 处 + account_mirror / exchange_event_inbox / exchange_safety_monitor / order_mirror / order_outbox / protection_mirror 各 1 处），README 表格第 9 行是 `smoke_fault_matrix.py:571` 的手工 `VALUES(18,...)` + `:573 == 18`，合计 9 条，**清单与全仓实况一致，无遗漏项**。
- 打补丁树（s2）逐脚本独立复跑这 8 个脚本：**全部 rc=0**（`probe_f2.log` §9 处断言；例：`smoke_db_migrations` 13/0、`smoke_account_mirror` 14/0、`smoke_order_mirror` 26/0、`smoke_fault_matrix` 37/0）。
- 根因独立确认：`alert_deliveries` 建在模块级 `SCHEMA`（每次打开幂等 `CREATE TABLE IF NOT EXISTS`），**不写 `schema_migrations`**；实测 `current_schema_version()==17`、`SELECT COUNT(*) FROM schema_migrations WHERE version>=18 == 0`、`MAX(version)==17`。
- 因此"均不触发"**成立**；且这是对 U01（迁移 17 曾打破这 9 处）的正确规避。README §3 的两个方案权衡（版本中性幂等建表 vs 迁移 18 + 9 份伴随补丁）我认可前者，理由：该表无需要迁移顺序约束的历史数据。

## 5. 对 4 个补丁的逐条意见

| 补丁 | 意见 |
|---|---|
| **01 `core/health_watch`** | **认可（1 项 P2）**。三条接线都是加法：既有原因码产生条件与命名未变（`smoke_health_watch` **独立复现 s1 与 s2 均为 `ok=28 fail=0`**）；未声明状态根时 `data_root`/`db_file` 与升级前逐字节一致（`probe_f3` 实测一致）。只读性正确（`mode=ro`+`query_only`）。`OUTAGE_CATEGORY_NAMES` 把 `PROCESS_MISSING`/`OUTAGE_HEALTHY` 映射为空串，避免新增原因码冲击既有 smoke 期望集合 —— 方向正确。P2-2：`from_settings` 读 `os.environ` 使行为随机器环境变化，而 I 的回归入口不清 `MTA_STATE_ROOT`（作者已登记）。另 P2-1 的 `process_alive=None` 口径在此调用点被赋予"缺锁=进程缺失"的第二语义，建议补一个 `PROCESS_UNVERIFIED` 类原因码。 |
| **02 `scripts/health_watch`** | **认可，且"缺核心补丁则显式拒绝"经独立构造混合态验证为真**。我特意构造 s4 = head + 补丁 04 + 补丁 02（**不含** 01/03）：`--state-root X` ⇒ rc=1 且 stderr `--state-root 需要 src/core/health_watch.py 的 U14 适配补丁同时应用`；不带 `--state-root` ⇒ 正常输出旧 JSON（无 `trading_evidence_at` 等新字段），旧行为完整保留。`inspect.signature` 探测的 fail-closed 方向正确（不会静默盯住错误状态根）。 |
| **03 `audit_database` 新表** | **认可**。纯新增表 + 两个方法；`current_schema_version()` 仍 17、无 version=18 行、既有表列/索引/行数零变化（§3 证据）。审计镜像复用 K05 的 `evaluate_state`/`delivery_violations`（**未另立第二套阶梯**），并额外要求 `DELIVERED` 必须有非空 `device_receipt`、拒绝调用方自报状态（`state` 与派生值不符即 `ValueError`）；`state_rank` 单调守卫实测"同 attempt 低阶重报不降级"（返回 `False`，状态保持 DELIVERED）。`first_occurred_at` / `first_occurred_at_ms` 的同刻双表示在 docstring 里显式声明为同一时刻，未与既有列混用 —— 符合"写入前三方核对"。**注意**：镜像表同样不绑定回执与消息身份（与 P1-3 同源），若 P1-3 的处置引入绑定字段，此表需同步。 |
| **04 `run_offline_regression` 登记行** | **认可**。仅 1 行 + 1 行注释，无逻辑改动；登记后官方入口 `scripts=80`。未登记时 fail-closed 经独立复现（s3）。归属 I 独占文件，作者只给登记行补丁，未越界改 env 构造 —— 正确（P2-2 的处置留给 I）。 |

## 6. 审查重点逐条的独立结论

1. **三级分开 / 唯一阶梯**：成立。唯一阶梯是 K05 `transition_state`；`HTTP200→SUBMITTED`、`provider_receipt→ACCEPTED`、`delivered_at_ms+device_receipt→DELIVERED`。跳级（无 `provider_receipt` 直接 DELIVERED）、空回执、时间倒序、`model_copy` 伪造 `state=DELIVERED` 而 `delivered_at_ms=None` 全部被拒且**不落库**（`probe_f` §1b/1c/1d/1g）。**但**：时间戳**相等**被接受（非降序，符合 K05）；**未来**时间戳被接受（P1-3）；回执不绑定消息（P1-3）。
2. **两成因分开**：判别式独立复核成立 —— `uptime >= age ⇒ PROCESS_MISSING`（含 `==` 边界，方向保守）、`uptime < age ⇒ HOST_DOWN`、`uptime` 不可得 ⇒ `HOST_CONTINUITY_UNKNOWN`、**存活但证据陈旧 ⇒ `TRADING_STALLED`（=120s 边界不陈旧，=121s 即陈旧，证据缺失也归 STALLED，绝不判健康）**（`probe_f` §2a–2e）。对抗面：uptime 被伪造极大 → PROCESS_MISSING（不可辨，属上游信任问题）；uptime 负值 → 落入启动宽限（P3-5）；证据时间在未来 → 不判异常（P3-3）；`process_alive=None` → 断言 PROCESS_MISSING（P2-1）。
3. **OPS_BLOCKED 不得伪造送达**：未验证通道 `dispatch_due` **不调用 transport**（`liar.submissions == []`）、状态留 UNKNOWN、恒 `OPS_BLOCKED`；仅 HTTP200 → `OPS_DEGRADED` + `NO_CONFIRMED_DELIVERY`；有设备回执才 `OPS_OK`；缺升级路径仍 BLOCKED —— 这些**都成立**（`probe_f` §1a、`probe_f2` §4）。但存在 P1-2 的两条 fail-open（空队列 + 配置 verified ⇒ `OPS_OK`；任一已送达掩盖卡住项）。
4. **心跳带交易证据时间**：缺 `trading_evidence_at_ms` ⇒ 不健康；emitted 回退/序号持平或回退/证据时间回退/超容差的未来时间戳一律拒绝且**时间线不推进**（`probe_f` §3a–3e）；无界维护窗口被拒、有界窗口 muted 不健康（§3g/3h）；`emitted` 为 bool/0/负数 → `HEARTBEAT_TIMESTAMP_MISSING`（§3i）。缺口：证据时间"未来但 ≤ emitted"被判健康且 age=0（P3-3）。
5. **队列独立于交易库**：独立复现成立 —— 200 条积压后交易库目录快照（mtime+size+sha256）完全不变、容量有界 50/淘汰 150 计入 `dropped_total`；**队列文件损坏** ⇒ `sqlite3.DatabaseError`（响亮失败，不静默）且交易库不变；**状态根不可写**（父路径是文件）⇒ `FileExistsError`，交易库不变；**主库损坏** ⇒ 只读探针 `available=False` + 三个 `SOURCE_UNAVAILABLE:...` 原因码，不抛异常（`probe_f` §5、`probe_f2` §5e）。未做：多进程并发写队列的压力模型（作者 README §7.5 已登记为未验证）。
6. **人工确认可审计**：缺 note / `confirm=False` → `MANUAL_ACK_REQUIRED`；`auto`/`  AUTO ` → `MANUAL_ACK_ACTOR_INVALID`；未提交先确认 → `MANUAL_ACK_WITHOUT_SUBMISSION` 且**写入 alert_audit、状态不改**；合法确认 → `ACKNOWLEDGED` 且 `delivered_at_ms`/`device_receipt` 保持 NULL，`alert_audit` 记录 actor+note；重复确认幂等（`probe_f2` §6、`probe_f` §6）。缺口：无操作者身份白名单（超出规格最低要求，"伪造确认"只需任意非 auto 字符串）；已送达后以更早时间确认会抛异常（P3-2）。
7. **共享列语义与写面**：见 §3，**成立**。
8. **迁移影响**：见 §4，**9 条均不触发成立**。
9. **补丁审读**：见 §5。
10. **全量 delta**：见 §1（差集 ∅，抖动未复现）。

## 7. 未覆盖 / 无法离线验证（明说）

1. **真实通道送达**：`verified` 通道只存在于夹具与内存 `RecordingSink`；真实回执、真实升级路径、宿主断电/失联演练属 **U12 OPS（D02）**。当前真实配置下 `OPS_BLOCKED` 是正确结果（README §7.1 已声明），我未也无法验证"真的能联系到人"。
2. **`host_uptime_seconds` 平台性质**：Linux 读 `/proc/uptime`，其他平台退回 `time.monotonic()`（由平台保证"自宿主启动"语义，非 Python 文档保证）；未在真实宿主做断电演练，离线只用注入值。属 README §7.2 已登记项。
3. **多进程并发写队列 / 队列写阻塞压力模型**：未做（README §7.5 已登记）。
4. **`AlertQueue` 的生产接线**：本任务无生产消费者（P2-3），"无人值守告警可用"未成立，须 U12 复验。
5. **`smoke_llm_deadline` 抖动**：在更差负载的机器上可能复现；本轮 4 类负载场景均未复现，且已证明补丁代码路径未被该脚本执行（§1.1）。若 Q 轮次复现且能定位到与本包相关的路径，应升级。
6. 未做：真实 `MTA_STATE_ROOT` 已设置的机器上的 delta 复跑（本机未设置；P2-2 已用直接探针证明其影响）。

## 8. 复现资产索引（全部在 `_scratch_F/`）

| 文件 | 用途 |
|---|---|
| `harness_f.py` | 逐脚本非 fail-fast 回归 harness（env 逐行复制官方 `main()`） |
| `s0_result.json` / `s1_result.json` / `s2_result.json` | 三树逐脚本返回码 + 失败集合 |
| `s0_harness.log` / `s1_harness.log` / `s2_harness.log` | 同上原始输出 |
| `probe_f.py` / `probe_f.log` | 阶梯绕过、失联边界、心跳、OPS、队列独立、人工确认、写面静态（48 项） |
| `probe_f2.py` / `probe_f2.log` | 追补：dispatch_due 卡死、回执绑定、OPS 掩盖、6e 修正、主库损坏、补丁态审计镜像 A1–A9、8 个迁移断言脚本 |
| `probe_f3.py` / `probe_f3.log` | 补丁层：02 显式拒绝（混合态 s4）、01 兼容/环境敏感、1h 复发 |
| `sandbox/s0 s1 s2 s3 s4` | 临时双树沙箱（s3=clean head，s4=head+04+02） |

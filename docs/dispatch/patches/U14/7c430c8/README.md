# U14 共享适配补丁包（作者 E，线 EF，任务 U14）

- **补丁基线（= 将被应用的那棵树）**：`task_base_commit = 59660b26f71f5c7057847bfb0caced5d88776218`
  （集成 HEAD，已含 U00/U01/U03/U04/U05/U13/U15）。
- **本包由该基线直接生成**：`git diff` 于 `mat-wt/EF`（分支 `task/U14`），
  补丁目录名 = 作者交付 head（若后续再追加提交，I 以“基线提交”为准钉住，见 §6）。
- 共享文件**未**提交进 `task/U14`；工作树里的改动只用于生成 diff，生成后已 `git checkout --` 还原。
- **不涉及**契约目录与 `scripts/run_offline_regression.py` 的代码归属（后者只给登记行补丁，见 04）。

## 1. 补丁清单

| # | 文件 | 目标文件 | 作用 |
|---|---|---|---|
| 01 | `01_core_health_watch.diff` | `src/core/health_watch.py` | RuntimePaths 单一事实源接线 + 交易证据时间只读探针 + 两类失联成因分开 |
| 02 | `02_scripts_health_watch.diff` | `scripts/health_watch.py` | `--state-root` 显式注入点；核心补丁缺失时**显式拒绝**而非静默忽略 |
| 03 | `03_audit_database_alert_deliveries.diff` | `src/audit/database.py` | 告警投递**审计镜像**表 + `record_alert_delivery`/`fetch_alert_deliveries` |
| 04 | `04_run_offline_regression_registration.diff` | `scripts/run_offline_regression.py` | **登记行**（I 独占文件，仅一行 + 注释，无逻辑改动） |

应用（仓库根，集成树）：

```bash
BASE=C:/Users/Tao/Desktop/币安/mat-wt/I      # 基线 59660b2
P=docs/dispatch/patches/U14/7c430c8
for f in 01_core_health_watch 02_scripts_health_watch \
         03_audit_database_alert_deliveries 04_run_offline_regression_registration; do
  git -C "$BASE" apply "$P/$f.diff"
done
```

## 2. `git apply --check`（两种 git 配置，均在**补丁基线那棵树**上验证）

验证方式：`git archive` 导出作者树（head `7c430c8`） + `git init`（使 `core.autocrlf` 生效），
逐个补丁检查。本机 main 仓库 `.git/config` 里 `core.autocrlf=false`（故“默认”一列为
autocrlf=true 的更严口径，来源 `git config --show-origin`）。

| 补丁 | `-c core.autocrlf=false -c core.eol=lf` | 默认（无 `-c`，本机 `core.autocrlf=true`） | 附加：`-c core.autocrlf=true -c core.eol=crlf` |
|---|---|---|---|
| 01_core_health_watch | **OK** | **OK** | **OK** |
| 02_scripts_health_watch | **OK** | **OK** | **OK** |
| 03_audit_database_alert_deliveries | **OK** | **OK** | **OK** |
| 04_run_offline_regression_registration | **OK** | **OK** | **OK** |

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
> 若 I/F/Q 认为新表也必须进迁移序列，则必须同时交付上表 9 条伴随补丁（含 9 号的
> `VALUES(18)`→`(19)` 与 `== 19`），本包不默认这么做。
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

## 5. 两态与 delta 自证

独立沙箱（`git archive` + `git init`，均在 `mat-wt/EF` 内的临时目录，交付前已删除）：

- **s0** = 官方基线 `59660b2`：`OFFLINE REGRESSION OK scripts=79`，逐脚本 **79/79 通过**。
- **s1** = 作者 head（含 U14 独占文件）+ 仅补丁 **04**（登记行）：**80/80 通过**。
- **s2** = 作者 head + 补丁 **01–04**：`OFFLINE REGRESSION OK scripts=80`，逐脚本 **80/80 通过**。

失败集合差集：`s1.FAIL = ∅`，`s2.FAIL = ∅` ⇒ **新增失败 0**（`fail->pass` 与 `pass->fail` 都为空）。

**一处一次性抖动（如实登记）**：s2 首轮（三套回归连续跑、机器负载高）出现
`scripts/smoke_llm_deadline.py` 失败一次，断言 `超时请求只发送一次且不外置遗留线程`。
证据与判定：该脚本单独复跑 s2 **3/3 通过**、s1 **2/2 通过**，s2 全量复跑 **80/0 通过**；
且其 import 面只有 `httpx` / `llm.*` / `agents.orchestrator`，与本包三个目标文件
（`core.health_watch` / `audit.database` / 回归登记）**无依赖关系** ⇒ 判定为 U05 该冒烟的
时序抖动，非本包引入。**不声称“零影响”，只声称“复跑不可复现且无依赖路径”**。

两态验收套件（本任务交付的 `tests/acceptance/test_alert_delivery.py`）：

| 树 | 结果 |
|---|---|
| s1（共享补丁**未**应用） | `U14 ALERT_DELIVERY PASS=164 FAIL=0` |
| s2（共享补丁**已**应用） | `U14 ALERT_DELIVERY PASS=179 FAIL=0` |

差异来自 7b 段按两态**各自应有的语义**分别断言（补丁在 → 断言新事实与 `--state-root`
生效；补丁不在 → 断言旧行为未被伪造、CLI 无 `--state-root`），**没有 SKIP**。

打补丁后既有 `scripts/smoke_health_watch.py`：**ok=28 fail=0**（与打补丁前相同）。

## 6. 补丁目录命名

目录名 = **作者交付 head `7c430c8`**（`task/U14`）。diff 在更早的 head `5249004` 生成，
此后仅追加了 docs/test 提交（**不含**任何共享文件），故 diff 内容与基线
`59660b2` 的对应关系不变；`git apply --check` 在最终树上重新验证通过（§2）。
若 I 之后在 `task/U14` 上再追加提交，请以“补丁基线 59660b2”为准钉住应用目标。

## 7. known_limits（如实登记，未验证项必须明说）

1. **真实通道未验证**：本任务全程离线，`verified` 通道只出现在夹具/内存 fake sink 中；
   真实送达回执与主通道演练属 U12 OPS（D02）。`OPS_BLOCKED` 是当前真实配置下的**正确**结果。
2. **`host_uptime_seconds` 的平台假设**：Linux 读 `/proc/uptime`，其余平台退回
   `time.monotonic()`（Windows QPC / macOS / Linux 均为“自宿主启动”语义，但该性质
   由平台保证而非 Python 文档保证）。返回 `None` 或可疑值时一律按“不可证明”处理
   （`HOST_CONTINUITY_UNKNOWN`），**不**猜。离线测试通过注入 `monotonic_value` 覆盖，
   **未**在真实宿主上做断电演练。
3. **维护窗口/升级路径只做结构化校验**：`expires_at_ms` 有界性、`escalation_path`
   非空是配置面检查；“真的能联系到人”只能由 U12 OPS 演练证明。
4. **交易证据时间的最小可证粒度**是上述三张表的 `MAX(时间列)`；某类证据长期不写
   （例如无成交）时它由其余来源兜底，极端情况下可能晚于真实交易时刻（§4 未纳入项）。
5. **入队是同步 SQLite 写**：队列在独立文件上，`busy_timeout=5000`；本包未做
   “队列写阻塞”压力模型（无并发多进程写队列的验收）。
6. **`alert_deliveries` 不进迁移序列**（§3），因此它没有“迁移回滚”语义；表结构变更
   需要人工处理（与 `SCHEMA` 里其他基础表一致）。
7. **`--state-root` 与 `MTA_STATE_ROOT` 共同生效**：`from_settings` 会（与其他
   RuntimePaths 消费者一致地）读取 `MTA_STATE_ROOT`/`MTA_DATA_ROOT`。官方回归入口
   `run_offline_regression.py` **未**清除这两个变量（只清 `MTA_PROFILE`）；本机未设置
   故无影响，本任务新增的冒烟脚本自行清空它们。建议 I 在回归入口补两行 `env.pop(...)`
   以获得与“干净环境”等价的结论（本包**不改**该文件的环境构造逻辑，避免超出登记行范围）。

## 8. rollback

- 逆向应用同一组 diff 即可：`git apply -R <p>`（顺序 04→01，或直接 `git checkout --`
  四个文件到基线）；`alert_deliveries` 表是新增表，回滚后残留该表不影响任何既有消费者
  （无既有代码引用它）。若要彻底清除：`DROP TABLE alert_deliveries;`
- 因为**没有**新增迁移版本，回滚不需要处理 `schema_migrations`。

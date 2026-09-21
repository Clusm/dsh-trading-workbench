# F01–F25 故障矩阵（U12 BUILD，DRAFT）

- 定义来源：`docs/无人值守交付与多Agent执行规格_2026-09-19.md` §6（F01–F16）、
  §14（F17–F24 + `F11` 的 DEFERRED 规则）；`F25` 由台账 `fault_matrix_extension` 登记。
- 本矩阵对象：集成树 `7885e6a`（本项目工作树 `mat-wt/W3-rc`，分支 `task/U12`）+ 本 RC 交付物。
- **状态：DRAFT。** 本矩阵**不**宣布 RC 冻结、**不**宣布任何观察门禁通过。

## 0. 状态词表与证据口径

| 覆盖状态 | 含义 |
|---|---|
| `COVERED_OFFLINE` | 该条在首批范围内的必须结果已由本树离线证据覆盖，无已知缺口 |
| `PARTIAL_OFFLINE` | 离线核心不变量**已有本树证据**；剩余必须结果依赖**当前缺失的能力**（逐条在 §D 列明），须由 U12 后续检查点承接 |
| `DEFERRED` | 明示后置，**不得算作已验收功能**（仅 F11） |
| `NOT_VERIFIABLE_NOW` | 整条必须结果均依赖缺失能力，当前**无法**验收（本矩阵**无**此类条目，但**不等于**全部达标） |

**⚠ 标记**：该条离线范围内已有**已知未修缺陷**（挂账 ID 给出），必须结果的一部分
在集成树上**不成立**。

**证据来源标注**（严格遵守"区分引用与自跑"）：

| 代号 | 证据 | 来源 |
|---|---|---|
| `E1` | `scripts/run_offline_regression.py` → **exit 0**，`OFFLINE REGRESSION OK scripts=85`（本树实测；Q 在 U13-FU1 候选树上曾记 84，**以本树实测为准**） | **自跑**（本树） |
| `E2` | `scripts/check_quality.py` → **exit 0**，`QUALITY CHECK OK files=295 untracked=0` | **自跑**（本树） |
| `E3` | `tests/acceptance/test_execution_faults.py` → **exit 0**，`168 通过 / 0 失败` | **自跑**（本树） |
| `E4` | `tests/acceptance/test_release_evidence_v2.py` → **exit 0**，`78 通过 / 0 失败` | **自跑**（本树） |
| `E5` | `tests/acceptance/test_runtime_identity.py` → **exit 0** | **自跑**（本树） |
| `E6` | `tests/acceptance/test_supervision.py` → **exit 0**，`166 checks` | **自跑**（本树） |
| `E7` | `tests/acceptance/test_alert_delivery.py` → **exit 0**，`PASS=262 FAIL=0` | **自跑**（本树） |
| `E8` | `tests/acceptance/test_control_boundary.py` → **exit 0** | **自跑**（本树） |
| `E9` | `docs/dispatch/reviews/U12/drill_F14_F18/REPORT.md`（隔离恢复演练） | **引用**（他人会话） |
| `E10` | `docs/dispatch/reviews/U13-FU1/af76443/Q_r1.md`（修复独立复验） | **引用**（他人会话） |
| `E11` | `scripts/smoke_*.py` / `tests/integration/*.py` —— 全部由 `E1` 一次性运行 | **自跑**（经 E1） |

> **证据口径声明（重要）**：`E11` 的具体脚本名按**台账 `fault_tests` 归属** +
> **代码内显式 `Fxx` 引用** + **场景匹配**归类，且这些脚本在**本树由 E1 实际执行、exit 0**。
> 本 U12 会话**未逐条复核每个脚本内部断言的覆盖完备性**——断言的独立性与充分性由各任务
> 的 Q 独立审查负责（审查报告归档在 `docs/dispatch/reviews/<ID>/<candidate>/`）。

---

## 1. 汇总

| 覆盖状态 | 条目数 | 条目 |
|---|---|---|
| `COVERED_OFFLINE` | **4** | F08、F09、F10、F16 |
| `PARTIAL_OFFLINE` | **20** | F01、F02、F03、F04⚠、F05⚠、F06⚠、F07、F12⚠、F13⚠、F14⚠、F15⚠、F17、F18⚠、F19⚠、F20⚠、F21、F22⚠、F23⚠、F24⚠、F25⚠ |
| `DEFERRED` | **1** | F11（`F11:v2_labels=DEFERRED`） |
| `NOT_VERIFIABLE_NOW` | 0 | —（**无条目被判"完全可验"**；见 §D 的 9 类缺失能力） |
| 合计 | **25** | F01–F25 |

- **有离线证据**：25/25（经 `E1`/`E3`–`E8`）。
- **含已知未修缺陷（⚠）**：15 条。
- **明示后置**：1 条（F11）。
- **必须由 DEPLOY_DEMO/OPS/OBSERVE 承接的必须结果**：见 §D（9 类缺失能力）。

### 1.1 U12 BUILD 新发现（`U12-BUILD-F1`，台账未登记）

**K01 身份采集在真实 `src/` 树上必然失败** → identity `NO_DATA` →
`release_readiness` **不可能为 GO**。该缺陷使上表 **F13** 行再加一个 ⚠，
并**阻断 RELEASE**。完整事实链、证据与修复方向见
`README.md` §5.1（同目录），待裁决编号 **A13**。**本 RC 不声称身份已验。**

---

## 2. 主表

| ID | 反例 | 必须结果 | 证据（脚本/验收；来源） | 覆盖状态 | 适用性说明（缺什么能力） |
|---|---|---|---|---|---|
| **F01** | 请求已到交易所但响应丢失 / 503 未知 | 查单对账，无第二笔同意图开仓 | `E3`[3][4]（DC13：UNKNOWN 只查单不重发、有界升级）；`E1`→`smoke_u04_execution_faults.py`、`smoke_order_outbox.py`（自跑） | `PARTIAL_OFFLINE` | 本地不变量（不重发/不双开）已验；**缺**真实交易所 `query_order` 时序与私有流（§D-1），离线用**进程内假 broker** |
| **F02** | BTC 0.01 仅成交 0.0047，随后撤单 / 迟到成交 | 真实数量守恒，保护随成交更新，剩余订单可收口 | `E3`[1][2]（DC04：三分量守恒 + 迟到成交修订）；`E1`→`smoke_u04_execution_faults.py`（自跑） | `PARTIAL_OFFLINE` | 守恒/修订已验；**缺**真实部分成交回报（§D-1） |
| **F03** | TP 与时间退出同时触发、重复用户流 | 一次净减仓，不反向开仓，不先撤最后保护 | `E3`[5]（F03/F02：保护随真实仓位、先挂新后撤旧）；`E1`→`smoke_u02_strategy_position_manager.py`、`tests/integration/test_strategy_position_manager.py`、`test_strategy_funnel.py`（自跑） | `PARTIAL_OFFLINE` | 离线时序已验；**缺**真实并发触发（§D-1）。⚠ 另见 `U02 _consumed_ids` 纯内存（F04 行） |
| **F04** | 下单前后各持久化断点崩溃 | 重启可恢复意图/查单，未知状态不重发 | `E3`[4]（SUBMITTING→UNKNOWN 不重发）；`E1`→`smoke_order_outbox.py`、`smoke_db_migrations.py`（自跑） | `PARTIAL_OFFLINE` ⚠ | **缺**真实崩溃/断电重启（§D-6，离线为**断点重放**非真崩溃）。⚠ **已知未修**：`U02` 的 `_consumed_ids` **纯内存**，重启后同 `exit_key` 会**再次 `SUBMIT_REDUCE`** → "重启不重复动作"这一面**不成立** |
| **F05** | DB 满、锁超时、状态 JSON 损坏、备份中断 | 停新仓，无静默 normal，无坏备份覆盖好备份 | `E1`→`smoke_db_backup.py`、`smoke_recovery_bundle.py`、`smoke_fault_matrix.py`（自跑）；`E6`；`E9` | `PARTIAL_OFFLINE` ⚠ | **缺**真实磁盘 ENOSPC 与真实 SQLite 跨进程锁竞争（§D-6）。⚠ **已知未修**：`U08-R5-FU2` 执行器内 `db.*` 调用**无护栏**，审计库 `OperationalError: database is locked` 会**穿出整轮**（`decision=None`）→ "停新仓/不静默"的一部分不成立 |
| **F06** | 主进程退出、挂起、PID 复用、第二实例、宿主重启 | 单账户单执行者；用户关闭 live 不自动启动 | `E6`[4]（锁由状态根决定，换代码根不换锁）；`E1`→`smoke_service_lifecycle.py`、`smoke_windows_service_lifecycle.py`、`smoke_profile_isolation.py`、`smoke_takeover_compatibility.py`、`smoke_detached_profiles.py`（自跑）；`E9` | `PARTIAL_OFFLINE` ⚠ | **缺**真实宿主重启与"进程挂起 ≤60s 外部探针发现"（§D-2，D01 未决）。⚠ **已知缺陷**：`U13 P2-3` 0 字节锁文件**单方接管，实测双持锁**（既有，`0cbdb20` 起）；P3-4 "单账户互斥"实为 `(state_base, profile)` 互斥 |
| **F07** | 模型 429/401/坏 JSON/240 秒耗尽/备用也失败 | 总 deadline 有界、原因码准确，保护与管理继续 | `E1`→`smoke_llm_coordinator.py`、`smoke_llm_deadline.py`、`smoke_llm_failover.py`、`smoke_u05_decision_trace.py`、`smoke_u05_model_route_safety.py`、`tests/integration/test_decision_trace.py`（自跑） | `PARTIAL_OFFLINE` | 离线故障注入已验；**缺**真实供应商 401/429（§D-1）。"保护与管理继续"的**生产可达性**受 `U08-U12-WIRING` 影响（逐票原因生产不可达） |
| **F08** | 418/429、失效权重状态、DNS 断网 | 共享持久化退避，恢复前不继续轰炸接口 | `E1`→`smoke_public_rate_limit.py`、`smoke_rate_limit_multiprocess.py`、`smoke_request_policy.py`、`check_binance_request_policy.py`、`smoke_binance_transport.py`（自跑） | `COVERED_OFFLINE` | 持久化退避与跨进程共享已在离线（含多进程）验证。真实 DNS 断网未验（§D-1），但该条的必须结果**本地可判** |
| **F09** | 缺 K 线、重复/乱序、未闭合伪确认、未来事件、两意图借证据 | 每个意图独立核验，错误事实不入执行 | `E1`→`smoke_u03_market_evidence_quality.py`、`smoke_u03_research_evidence.py`、`tests/integration/test_market_evidence_quality.py`、`test_research_evidence.py`、`test_strategy_funnel.py`（自跑） | `COVERED_OFFLINE` | 合成夹具驱动，必须结果完全离线可判 |
| **F10** | 收入重复、同毫秒满页、迟到、双向仓、零资金费、非 USDT 费用 | 幂等守恒；COMPLETE 可证明；未知仍未知 | `E1`→`smoke_u01_income.py`、`tests/integration/test_income_reconciliation.py`（自跑）；`E4` | `COVERED_OFFLINE` | 合成夹具驱动，必须结果完全离线可判 |
| **F11** | 5 分钟标签晚 8 分钟、轮转币缺价、历史版本混合 | 旧口径可见，v2 缺失明确，不选择性丢负样本 | 见 §3（F11 专条）；`E1`→`smoke_u06a_shadow_isolation.py`、`tests/integration/test_shadow_status.py`（自跑） | **`DEFERRED`** | **`F11:v2_labels=DEFERRED`** —— 见 §3 |
| **F12** | 告警供应商接受但不送达、断网、主机断电、配置假 verified | 按真实回执展示状态；外部能发现失联 | `E4`[5]（ACK ≠ 送达 ≠ 恢复）；`E7`；`E1`→`smoke_u14_alert_delivery.py`、`smoke_health_watch.py`（自跑） | `PARTIAL_OFFLINE` ⚠ | **缺**真实告警通道送达回执 + 异地 dead-man 探针（§D-3，D02 PENDING）→ 端到端 ≤120s、失联 ≤180s 的**外部发现不可验**。⚠ **已知未修**：`AlertQueue` **无生产消费者**，监控只看 `errors` 且 `PERSIST_FAILED` **不在其中** |
| **F13** | 改依赖锁/模型覆盖/profile/策略卡/参数 | 指纹必变，身份不符不发单，凭据不外泄 | `E5`；`E1`→`smoke_contracts_v3.py`、`smoke_u10a_release_package.py`、`smoke_detached_profiles.py`（自跑）；`E2`（密钥扫描） | `PARTIAL_OFFLINE` ⚠⚠ | 指纹必变与凭据不外泄已验。⚠⚠ **U12 BUILD 新发现 `U12-BUILD-F1`**：K01 身份采集在真实 `src/` 树上**必然失败**（`management_authorization.py` 命中 `"authorization"` 敏感标记 → `IDENTITY_COLLECT_FAILED` → identity `NO_DATA` → `release_readiness` 不可 GO）；`E5` 用合成根/显式 relatives **对本缺陷盲**。见 `README.md` §5.1。⚠ **缺**"身份不符不发单"的**联合链路**：`W1-JOINT-WIRING` **未执行**（U03 接线 × U15 身份须同批），手册 S05 联合候选验收**尚未做** |
| **F14** | 从恢复包启动，期间有交易所已成交订单 | 先全量对账/增量回拉再开仓，不重复下单 | 见 §4（F14/F18 专条）；`E9` + `E10`（**引用**）；`E1`→`smoke_recovery_bundle.py`、`smoke_takeover_compatibility.py`（自跑）；`E6`[7] | `PARTIAL_OFFLINE` ⚠ | 4 条 P1 已 FIXED 并集成（`acf9ced`），但**仍有两条 U12 待裁决项**；**不得记作"完全通过"**。**缺**真实交易所已成交订单 + `live` 档端到端（§D-1/§D-2） |
| **F15** | 无凭据控制请求、错误 Origin/token、改 profile 路径 | 控制面拒绝未授权写入，demo/live 状态互不污染 | `E8`（控制面纯策略函数）；`E1`→`smoke_profile_isolation.py`、`smoke_detached_profiles.py`（自跑）；`E9` | `PARTIAL_OFFLINE` ⚠ | **缺**真实 HTTP 控制面（回环/Origin/token 判定**只做代码核对/纯策略函数**，未发请求，§D-2）。⚠ S05 联合链路未做（同 F13）。⚠ `U13FU1 README vs 代码`（N3）待裁决 |
| **F16** | B 影子样本充当 AI 候选、同单多 fill、资金费晚到 | 候选/闭合不膨胀，门禁可撤销并可重算 | `E4`[2][2b]（S08/F16：B/C 影子行不得进方向候选分母）；`E1`→`smoke_u09_release_evidence.py`、`smoke_u06a_shadow_isolation.py`（自跑） | `COVERED_OFFLINE` | **`S08-SHADOW-GATE-LEAK` 已闭合**：`release_evidence.py` 只计入 `intel.shadow_status` 判定的 `execution_decision_copy` 来源，影子行只进诊断字段 |
| **F17** | NTP 回拨/快进、UTC 日切、monotonic 期限与重启 | 不延长授权、不重复日重置；时钟不可信停新仓 | `E6`[8]（时钟可信度）；`E1`→`tests/integration/test_research_evidence.py`、`smoke_u01_income.py`（日切）（自跑） | `PARTIAL_OFFLINE` | **缺**真实 NTP 回拨/宿主时钟变更（§D-2） |
| **F18** | 新代码目录、旧状态根、PID 复用、两个账户同 profile/同账户不同 profile | 状态路径/身份一致，单账户互斥，不新建错误基线；迁移可回滚 | 见 §4（F14/F18 专条）；`E9` + `E10`（**引用**）；`E6`[4]；`E1`→`smoke_u13_supervision.py`、`smoke_profile_isolation.py`、`smoke_takeover_compatibility.py`（自跑） | `PARTIAL_OFFLINE` ⚠ | **缺**同根两账户真实凭据与 SCM 双 profile 注册（§D-4）。⚠ **已知缺陷/待裁决**：未声明状态根漂移、单方接管、同根两账户覆盖基线、恢复丢运行代、`N3` README vs 代码 |
| **F19** | 人工 pause/emergency 与健康自动恢复重叠，外部挂单成交 | 不解除人工/风险锁存；不误撤未知挂单；不把普通故障当主动全平 | `E1`→`smoke_u08_strategy_funnel.py`、`smoke_u02_strategy_position_manager.py`、`tests/integration/test_strategy_funnel.py`、`test_strategy_position_manager.py`（自跑）；`E6` | `PARTIAL_OFFLINE` ⚠ | ⚠ **生产可达性缺口**：`U08-U12-WIRING` 未接线（`graph.py` 未注入 funnel/ledger），`classify_cycle`/`round_explanation` 生产可达性为 0 |
| **F20** | 补账/影子长查询、LLM 悬挂、日志/WAL 增长、备份停写 | 管理链符合已冻结时限，资源有界，可降级研究负载 | `E1`→`smoke_u05_background_refresh_budget.py`、`tests/integration/test_background_refresh_budget.py`、`smoke_db_backup.py`（自跑）；`E4` | `PARTIAL_OFFLINE` ⚠ | **缺** SLO 时限冻结（§D-7，**D05 阻断 `RC_FREEZE`**）→ "符合已冻结时限"**当前无冻结值可判**；日志/WAL 增长的真实上界未压测（§D-6） |
| **F21** | 普通单已 CANCELED 但迟到部分 fill、Algo 提交结果丢失、保护替换双单 | 事实能修订；用对应端点查单；不漏仓、不双减、不假覆盖 | `E3`[6]（普通与 Algo 命令分账、分派查单、覆盖声明）；`E1`→`smoke_u04_execution_faults.py`、`tests/integration/test_strategy_position_manager.py`（自跑） | `PARTIAL_OFFLINE` | **缺**"用**对应端点**查单"的真实交易所端点验证（§D-1） |
| **F22** | 心跳进程正常但交易进程停摆、告警 HTTP200 未送达、主库损坏 | 外部检测陈旧交易证据，投递独立，未确认不假送达 | `E7`（心跳活着但交易证据陈旧 ≠ 健康；主库损坏仍投递严重告警）；`E1`→`smoke_u14_alert_delivery.py`、`smoke_health_watch.py`（自跑） | `PARTIAL_OFFLINE` ⚠ | **缺**真实告警通道 + 真实主库损坏（§D-3/§D-6）。⚠ `AlertQueue` 无生产消费者（同 F12） |
| **F23** | 收入满页中插入迟到记录、重复 ID 不同内容、已 COMPLETE 后费用修订 | 覆盖不虚报，冲突隔离，净收益与发布证据可修订 | `E4`[4]（lifecycle 去重、迟到修订重算、冲突隔离、未知仍未知）；`E1`→`smoke_u09_release_evidence.py`、`smoke_u01_income.py`（自跑） | `PARTIAL_OFFLINE` ⚠ | ⚠ **待裁决 A3/A4**：`U09-OUTCOME-VS-LIFECYCLE-COST`（门禁读 **outcome 快照**而非 lifecycle **权威态**）；`U09-NET-UNKNOWN-PADDING`（净收益未知样本计入 30 且**零门槛**）。`U09-REVISION-CONFLICT-DEAD-BRANCH`（P3 测试卫生） |
| **F24** | 窗口首尾断档、不同 RC/配置混样、无确认事件被排除、纯 NO_EDGE 凑 200 | 分母/身份/候选定义正确，拒绝统计伪通过 | `E4`[2b][3][6]（NO_EDGE 不计 200；窗口来自声明、首尾缺失计入；重启 ≠ 换版）；`E1`→`smoke_u09_release_evidence.py`（自跑） | `PARTIAL_OFFLINE` ⚠ | 口径正确性已验；**缺** 200/30/7 的**真实分母样本**（§D-5，观察窗未启动）。⚠ `U09-NET-UNKNOWN-PADDING` 零门槛（A4）；`U09-P3-CLUSTER` 的 `current_closed` 无窗口过滤 |
| **F25** | 复盘统计、归因与校准（RV01–RV03） | NULL 不计赢、持平独立、失效需触发事件、校准区分高低估与小样本、重算幂等 | `E1`→`smoke_u01_review_statistics.py`、`tests/integration/test_review_statistics.py`（自跑）；台账 `dc_test_cases` DC01–DC03 `REGISTERED` | `PARTIAL_OFFLINE` ⚠ | ⚠ **已知未修**：`U05-COUPLING-REVIEW` —— U05 决策 trace 写入 `agent_runs.latency_ms`，而 U01 `review.py` 用 `AVG(latency_ms)` 聚合 → **复盘统计正确性缺口** |

---

## 3. F11 专条（逐字要求）

> **`F11:v2_labels=DEFERRED`**

- **首批只验**：旧口径标识 / C 禁用 / 零执行权重及隔离。**不把它算作已验收功能。**
- 机制依据（集成树**实际代码**，非作者自述）：
  - `src/intel/shadow_status.py:36` → `V2_LABELS_STATE = "DEFERRED"`
  - `src/intel/shadow_status.py:39` → `FIRST_RELEASE_FAULT_SCOPE = "FIRST_RELEASE_LABEL_DISCLOSURE_C_UNCONFIGURED_ISOLATION_ONLY"`
  - `src/intel/shadow_status.py:32` → 旧口径标识 `LEGACY_CALIBER_ID = "legacy-v1-rolling-mark-price"`
  - `src/intel/shadow_status.py:42` → `EXECUTION_WEIGHT = 0.0`（**常量而非配置**，杜绝配置面抬起）
  - `src/intel/shadow_status.py:38` 注释逐字：**"F11 首批验收范围（U12 必须逐字记录 `F11:v2_labels=DEFERRED`）"**
  - `execution_weight_guard()`：配置声明非零权重时**只登记、不生效**（`forced_fields`）
- **证据**：`E1` → `scripts/smoke_u06a_shadow_isolation.py`（两态兼容 + 旧口径字段语义）、
  `tests/integration/test_shadow_status.py`（自跑，exit 0）。
- **覆盖状态**：`DEFERRED`。v2 固定时点标签属 **U06b 后置**（`PLANNED`），首批**不验**。
- **不得**：把 F11 写成"通过"、把 DEFERRED 写成 N/A、或在影子权重未讨论前声称收益已证明。

---

## 4. F14 / F18 专条（引用两份报告 + 待裁决）

**两份被引用的报告（均为他人会话产物，本会话未复跑其探针）**：

- 演练（原始发现）：`docs/dispatch/reviews/U12/drill_F14_F18/REPORT.md`（`E9`）
- 修复复验：`docs/dispatch/reviews/U13-FU1/af76443/Q_r1.md`（`E10`）

### 4.1 已修复并集成（4 条 P1）

`U13-FU1` 的 4 条 P1 已 **FIXED 并集成**（集成提交 **`acf9ced`**，候选 `af76443`，
Q r1 判 **PASS-with-notes，无阻塞项**）：

| P1 | 演练发现 | 复验结论（`E10`） |
|---|---|---|
| P1-1 | core 恢复演练不安装恢复门禁（`drill_epoch_bundle` 只写回执字符串，`before_entry()` → `GATE_NOT_INSTALLED`/`allowed=True`）；`verify_recovery_plan.py` 的断言是**循环证据** | 门禁**真落盘**（文件 + 安装戳）；循环证据已删并做**非循环性证明**（`gate after unlink = GATE_MISSING`） |
| P1-2 | live 档门禁装错位置 → fail-open（`epoch.profile=""` → 门禁落 `data/ops/`，live 读 `data/live/ops/`） | `restored_state_root()`/`restored_gate_path()` 被演练与生产恢复**共用**；两档 `before_entry()` 均 `allowed=False` |
| P1-3 | 门禁证据是操作者自证裸布尔，读取时不复验（手写 `{"state":"CLEARED",...}` 即放行） | 三档 `EVIDENCE_UNVERIFIED` / `EVIDENCE_MISMATCH` / `RECONCILE_REQUIRED_BEFORE_ENTRY` 全生效；真收口 → `GATE_CLEARED`，之后账本再现未收口 → **重新阻断**（非一次性） |
| P1-4 | 未声明状态根时 `STATE_ROOT_MISSING` 不可达 → 静默新建错误基线 | `validate('demo')` 现返回 `('STATE_ROOT_MISSING',)`；`state_base_explicit` 有真实消费者（`state_root_drift`） |

Q 另用**正确方法重做** delta（解包后 `git init`，84 脚本逐条 rc）：`NEW_FAIL=∅ / FIXED=∅`；
真实工作树官方入口 `OFFLINE REGRESSION OK scripts=84 exit 0`（**引用**该报告的数字；本树实测为 **85**，见 E1）。I 已弃用作者的补丁 05
（作者对 `check_quality` 变红的归因经 I/Q 实测**不成立**：25 条 ruff 全部来自 I 自己归档的
演练探针 `docs/dispatch/reviews/U12/.../raw/*.py`，已改名 `.py.txt` 修复）。

### 4.2 仍有两条 U12 待裁决项 —— **F14/F18 不得记作"完全通过"**

| # | 待裁决项 | 出处 | 现状 |
|---|---|---|---|
| **A1** | 裸 `before_entry()` 在缺账本时 **fail-open**（自洽摘要即放行，与其文档契约相悖，N1）；`src/core/service.py:314`（启动）与 `:1416`（`set_control`）仍是**裸调用**，补丁 01 的接线**只覆盖就绪链**，作者自带验证器**完全不触及 `service.py`**（N2） | 台账 `U13FU1-BARE-BEFORE_ENTRY-FAILOPEN`；`E10` `new_findings.N1/N2` | **正在由 `U13-FU2` 修**。本 RC **不声称已修**。Q 判 P2 有界、**未能构造生产链利用**（真实部署里账本存在），但生产代码里仍有**两处未接线的调用点** |
| **A2** | `U13-FU1` README 称"demo 只是被检出并报告、不阻止启动"，但 `assert_environment_bound('demo')` **实测会抛**（P3 交付物卫生；该 API 生产零调用点，默认部署仍可启动） | 台账 `U13FU1-README-VS-CODE-N3`；`E10` `new_findings.N3` | **待裁决**：改 README / 改代码 |

**F14 / F18 覆盖状态：`PARTIAL_OFFLINE` ⚠**（4 条 P1 已闭合，但上述两条**仍待裁决**，
且真实交易所/真实断电/真实多账户面**未验**）。

### 4.3 F14/F18 相关的其余已知缺口（`E9`，未修，已列入运维风险清单）

恢复包 P2 簇：①两套恢复包**单向不互通**（生产脚本读不了 core 的 schema-3 包）；
②`check_writer_stopped` 对**损坏锁静默 return** → 停写前提不可证；③`InstanceLock` 对
**0 字节锁单方接管**（实测双持锁，**既有缺陷**）；④同一状态根两账户 → 运行代基线被静默覆盖；
⑤恢复包不含 `runtime_generation.json` → **DR 后运行代必变**。
另有 P3：门禁文件 + 安装戳**同时删除** → `GATE_NOT_INSTALLED` 放行；
冷启动 readiness 硬编码 `unknown_orders/submitting_orders = 0`；
"单账户互斥"实为 `(state_base, profile)` 互斥。

---

## 5. §D 缺失能力清单（"缺什么、为什么现在不能验"）

以下 9 类能力**当前缺失**，使上表 20 条 `PARTIAL_OFFLINE` 的**剩余必须结果**无法在
BUILD 阶段验收。**必须由 U12 后续检查点承接；本矩阵不把未完成能力写成 N/A 或通过。**

| # | 缺失能力 | 为什么现在不能验 | 承接检查点 |
|---|---|---|---|
| D-1 | 真实交易所交互（下单/撤单/**对应端点查单**/私有流/真实部分成交/真实 418·429/真实费用） | 硬约束：**不联网、不下单撤单**；离线证据一律用**进程内假 broker** | `DEPLOY_DEMO` |
| D-2 | 真实宿主（进程挂起/断电/宿主重启/PID 复用跨重启/外部探针/真实 HTTP 控制面） | 需专用演练宿主；`D01`（宿主托管）`PENDING` | `OPS`（D01） |
| D-3 | 真实告警通道与异地 dead-man 探针（送达回执、端到端 ≤120s、失联 ≤180s） | `D02`（告警接收人/升级路径/异地探针）`PENDING`；硬约束：**不发真实告警** | `OPS`（D02） |
| D-4 | 真实账号身份与风险约束（账户必要风险 `audit_only`→`enforce`、存量仓退出规则） | `D03`/`D04` `PENDING`；硬约束：**不动风险档/白名单/live 开关** | `DEPLOY_DEMO`/`RELEASE` |
| D-5 | 真实观察窗样本（**7 天 / 200 方向候选 / 30 完整闭合** / 99.9% 覆盖率） | **观察窗尚未启动**；同版本真实样本无法由 Agent 生成 | `OBSERVE` |
| D-6 | 真实断电 / 撕裂写 / 独立卷灾难恢复 / 真实磁盘满 / 真实库损坏 | 演练为**同卷**（`SAME_VOLUME_NOT_DISASTER_RECOVERY`）；竞态窗口未压测 | `OPS` |
| D-7 | 运维 SLO 时限冻结（快循环 p95≤30s、保护 p95≤5s、本机告警 ≤60s 等） | `D05`（SLO 测量口径与目标冻结）`PENDING`，**`blocks_checkpoint = RC_FREEZE`** | `RC_FREEZE`（D05） |
| D-8 | U03 接线 × U15 身份的**联合**启用与 S05 联合候选验收 | 单独启用会让缺 pinned 身份时管线 **fail-closed 拒绝全部开仓**（既有冒烟红）；须同批落地 | `BUILD`（`W1-JOINT-WIRING`，**未执行**） |
| D-9 | U08 整轮聚合生产接线（`graph.py` 注入 funnel/ledger） | `graph.py` 未注入（grep 0 命中）；`round_explanation()` 唯一调用方是作者测试 | `BUILD`（`U08-U12-WIRING`，**未执行**） |

---

## 6. 与观察门禁的关系（不得混淆）

- 上表 `F24`/`F23`/`F10`/`F16` 的**离线口径正确性**已由 `E4`/`E1` 验收；
  但**发布门禁的三项数值**（7 天 / 200 方向候选 / 30 完整闭合）**仍未开始/未完成**
  —— 见 [`observed_status.md`](observed_status.md)。
- **`K06` 五组全绿 ≠ 发布通过**（台账 `U09-K06-GREEN-BUT-NO_GO`）：权威判定仍是
  `build_release_readiness_report` 的 `release_readiness`（`NO_GO` 直到观察门禁达标）。
- 本矩阵**不**构成 `RELEASE_READY` 或 `BUILD_VERIFIED` 的宣称——状态为 **DRAFT**。

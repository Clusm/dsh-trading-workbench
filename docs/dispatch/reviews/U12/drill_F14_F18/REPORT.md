# W3 隔离恢复演练报告（F14 / F18）—— 独立验收会话

- 对象：集成树 `448a4e9`（工作树 `mat-wt/W3-drill`，detached HEAD）。
- 性质：**独立验收**，只做验证、只写报告，未改任何产品代码。
- 原始证据：同目录 `raw/`（7 个可复跑探针脚本 + 22 份原始命令输出；全量离线回归日志只保留尾部 40 行）。
- 说明：本报告正文由验收会话在消息中给出，I 逐字归档；`raw/` 内为可复跑的脚本与原始输出。

---

## 1. 结论

| 项 | 判定 | 依据 |
|---|---|---|
| **F14** | **PASS-with-notes（有条件的）** | 生产恢复路径（`scripts/restore_recovery_bundle.py`）+ 真实 `TradingService` 端到端：先对账再开仓**成立**，不重复下单**成立**；但 core 演练路径（`drill_epoch_bundle`）**不装门禁**、live 档门禁**装错位置**、门禁证据是操作者自证且读取时不复验 |
| **F18** | **PASS-with-notes** | 显式状态根下"换代码根不换锁/不换身份"、PID 复用拒绝、死 PID 接管、迁移事务回滚 + 回退再推进不损坏 全部复现成功；但"未声明状态根"这一默认部署下漂移**零拦截**，且存在单方接管、同根两账户覆盖基线、恢复丢失运行代 |

本工作树内既有冒烟全绿（含官方入口 `OFFLINE REGRESSION OK scripts=84`，exit 0）。

---

## 2. 发现（按严重度）

### P1

**P1-1 ｜ F14：core 演练恢复后不安装恢复门禁，"恢复完直接开仓"可达（新引入，U13 起，未登记）**
- 现象：`drill_epoch_bundle()` 只在目标写 `restore_receipt.json`（其中 `entry_gate: RECONCILE_REQUIRED_BEFORE_ENTRY` **只是一个字符串声明**），从不写 `ops/recovery_gate.json`。还原后的状态根 `before_entry()` 返回 `allowed=True / GATE_NOT_INSTALLED`。全仓 `require_reconcile()` 的生产调用点只有 `scripts/restore_recovery_bundle.py`。
- 命令：`python docs/dispatch/reviews/U12/drill_F14_F18/_scratch/drill_f14.py` → exit 0（19 通过/0 失败）
- 输出摘要：`gate_path ... exists=False` / `install_marker exists=False` / `state=GATE_NOT_INSTALLED allowed=True`
- 佐证：`scripts/ops/verify_recovery_plan.py:90` 的"先对账再开仓"验收是**循环证据**——它把门禁装在自己另建的 `base/"drill"/gate.json`（与还原根 `<基>/drill/data/ops/...` 不是同一文件），再断言它阻断。
- 溯源：`git log --diff-filter=A -- src/core/ops/recovery.py` → `ef28b2b`（U13 首交，HEAD 的祖先）；`git show ef28b2b:...recovery.py | grep require_reconcile` 无该调用。跨 `a27fc17`/`b55c4fb` 至今未变。**新引入、无任何审查登记**。
- 对 U12 影响：F14 必达结果在 core 路径上**未达成**；必须钉住生产恢复脚本为唯一入口，或给 `drill_epoch_bundle` 补装门禁。

**P1-2 ｜ F14：live 档恢复门禁装错位置 → fail-open（已登记 P2-5 未修，实证）**
- 现象：`create_recovery_bundle` 恒写 `epoch.profile=""`，故门禁落 `data/ops/`；而 live 服务 `RuntimePaths.state_root = state_base/live`，读 `data/live/ops/` → 找不到门禁 → `GATE_NOT_INSTALLED` → 允许开仓。反过来若把 `epoch.profile` 设成 `live`，门禁去 `data/live/ops/` 但**四个关键状态 JSON 仍平铺在 `data/`**，live 状态根缺全部必需状态文件。
- 命令：`python .../_scratch/probe_live_restore_layout.py`
- 输出摘要：`epoch.profile=live` → `data/{4 个 json}` + `data/live/ops/recovery_gate.json`；`epoch.profile=(empty)` → `data/{4 个 json}` + `data/ops/recovery_gate.json`
- 溯源：生产脚本 `restore_recovery_bundle.py`（U13，`b55c4fb` 补丁）；`docs/.../U13/c216af6/Q_r2.md` 已登记 P2-5"需 U12 定义 live 恢复布局"。**已登记未修**。
- 对 U12 影响：实盘档 F14 未达成，属 U12 必须在集成前定案项。

**P1-3 ｜ F14：门禁证据是操作者自证的裸布尔，且 `before_entry` 读取时不复验 → 手写 JSON 即放行（新引入）**
- 现象：`positions_verified/open_orders_verified` 只在 `recovery.py` 与 HTTP 载荷出现，**从未由 `TradeReconciliationBook`/`OrderOutbox` 的真实对账结果推导**；`before_entry()` 只看 `state` + `reconciled_at_ms`。手写 `{"state":"CLEARED","reconciled_at_ms":<now>,"evidence":{}}` → `allowed=True`。
- 命令：同上 `drill_f14.py`（[C] 段）
- 输出摘要：`C1 forged CLEARED -> state=CLEARED allowed=True codes=('GATE_CLEARED',)`
- 溯源：`recovery.py` 引入即如此（`ef28b2b`），`b55c4fb` 的 P1-2 修复只补了损坏/删除两条路。
- 对 U12 影响：F14 的"先对账"是**流程约束**而非"对账已发生"的 enforced 事实；若 U12 验收口径要求"对账证据可核"，此项不达标。

**P1-4 ｜ F18：未声明状态根时 `validate()` 恒空，代码根漂移零原因码静默新建基线（新引入）**
- 现象：`resolve_state_base` 在 fallback 分支返回字面量 `"data"` 作为 `state_base_declared`，`validate()` 因此判为"已声明"→ 永不产出 `STATE_ROOT_MISSING`。正常构造路径下该原因码**不可达**。唯一探测口 `state_base_explicit`（False）在 `src/` 内**零消费者**。
- 命令：`python .../_scratch/drill_f18.py` → exit 0（31 通过/0 失败）
- 输出摘要：`state_base_declared='data' source=fallback explicit=False` / `validate('demo') = ()` / 两个代码根 legacy 各得**不同** lock 与**不同** runtime_instance_id
- 溯源：`runtime_paths.py` 引入即如此（`ef28b2b`）。`grep -rn STATE_ROOT_MISSING docs/` → **零命中，从未被报告**。
- 对 U12 影响：F18 必达"不得当成全新环境而新建错误基线"在**默认（未设 MTA_STATE_ROOT）部署**下无任何拦截信号；`U13` 声称的"启动期 fail-closed 前置检查"证据不成立。

### P2

**P2-1 ｜ 两套恢复包实现单向不互通（已登记 P2-4 的反向未修）** — 生产恢复脚本对 core 的 schema-3 包直接 `ValueError: 不支持的恢复包版本`（`drill_f14.py` [B0]）。与 P1-1 合成闭环缺口：core 建的包生产恢复**读不了**，退路（core 演练）**没门禁**。

**P2-2 ｜ `check_writer_stopped` 对损坏锁静默判"写者已停"** — `except (FileNotFoundError, OSError, JSONDecodeError): return`。损坏锁下 `create_epoch_bundle` 仍**成功建包**，于是"停写一致性/同 epoch"前提不可证（`drill_f18.py` B4/B5：`B5 ... -> CREATED`）。溯源 `ef28b2b`，新引入。

**P2-3 ｜ `InstanceLock.acquire` 对空/不可解析锁文件单方接管 → 实测双持锁（既有缺陷）** — 锁文件被截断为 0 字节后，持有者 PID 30004 仍存活，第二个进程 PID 34808 **接管成功**，两活进程同时认为持锁。确定性重放在 `drill_f18.py` B3；跨进程复现用 `race_child2.py`。溯源：该 `pid=0 → 视为已死 → unlink` 逻辑在 `0cbdb20`（U13 之前）已存在，U13 仅加了 metadata。**既有缺陷，非 U13 引入**。

```
holder   : {"role":"holder","pid":30004,"owned":true}
truncated: size=0
intruder : {"role":"intruder","owned":true,"content":"{\"pid\": 34808, ...}"}
```

**P2-4 ｜ 同一状态根上两个账户：运行代基线被静默覆盖** — `RunRegistry.load_or_mint` 在账户不符时直接 `persist` 新记录，先启动账户的 `runtime_instance_id` 被后启动账户覆盖，前者再启动会拿到**新的**运行代（`drill_f18.py` [C]）。溯源 `ef28b2b`，新引入。

**P2-5 ｜ 恢复包不含 `runtime_generation.json` → 恢复即换运行代** — bundle 成员仅 `database + 4 个必需 JSON`；`runtime_generation.json` 既不在 `REQUIRED_STATE_FILES` 也不在 `OPTIONAL_STATE_FILES`，且校验器拒绝白名单外文件，故 DR 后 `runtime_instance_id`/`generation_id` 必变（`probe_bundle_identity.py`：`identity_continuous: False`）。"运行代跨重启稳定（diff-6）"在**恢复**这一场景断裂。

### P3

- **P3-1 ｜ 冷启动 readiness 硬编码 `unknown_orders/submitting_orders = 0`**（`service.py:396/397`，溯源 `3a54188`，**既有**）→ 已还原根里的未收口 ENTRY 单在首次循环前不出现在冷启动快照（`drill_f14_e2e.py`：`codes=['RECONCILE_REQUIRED_BEFORE_ENTRY'] unknown=0`）。当次不影响安全（门禁+未核验事实仍阻断），但 U13 特意给冷启动注入了 `_recovery_gate_facts` 却没补这条。
- **P3-2 ｜ 门禁文件 + 安装戳同时删除 → `GATE_NOT_INSTALLED` 放行**（`drill_f14.py` C2）。属 P1-2 修复的已知设计取舍，但"删两个文件重置门禁"应进 U12 的运维风险清单。
- **P3-3 ｜ Windows 下建文件→写入窗口内竞争 `acquire` 抛未捕获 `PermissionError`**（非文档化的 `RuntimeError`）→ fail-closed 但错误形态不在契约内（`race_child.py` 实测 `PermissionError [WinError 32]`）。
- **P3-4 ｜ "单账户互斥"实为 `(state_base, profile)` 互斥**：同账户不同 profile 得到不同锁文件（`data/trading_service.lock` vs `data/live/trading_service.lock`），可并行；账户级互斥完全依赖"每账户一个状态根"的部署约定，运行期无校验。

---

## 3. 独立复现的必达结果与反例构造

**F14（`drill_f14.py` 19/0；`drill_f14_e2e.py` 15/0）**
- 反例构造：在状态根预埋"已 POST、交易所已成交（假 broker `query_order→FILLED`）、本地 SUBMITTING"的 ENTRY 单 → 建同 epoch 包 → 隔离演练还原 → 模拟重启。
- 必达结果复现：`recover_stale_submitting` 把 SUBMITTING 收敛为 UNKNOWN（不重发）→ 未收口订单进入开仓阻断（`ORDER_OUTBOX_UNKNOWN`）→ `reconcile_unknown` 回拉为 FILLED 且**全程 `broker.posts` 未增加**（不重复下单）→ 同一 `clientOrderId` 二次 `prepare` 抛 `DuplicateOrderSubmission` → 存在未决 ENTRY 时新 ENTRY 抛 `PendingOrderConflict`。
- 端到端（真实 `TradingService`，隔离 root，paper）：启动即 `close_only`；冷启动快照含 `RECONCILE_REQUIRED_BEFORE_ENTRY`；`set_control('normal')` 返回 `ok=False`；`auto`/未二次确认/缺字段证据全拒；`operator + confirm + 新鲜证据` → `CLEARED` 落盘且 HTTP 层要求环回 + operator。

**F18（`drill_f18.py` 31/0）**
- 换代码根：两个 `code_root` + 同一显式状态根 → **同一 lock_file / 同一 lock_scope / 同一 runtime_instance_id / 同一 generation_id**（`new=False`，未新建基线）。
- PID 复用：锁文件 PID = 当前进程 PID → **仍拒绝启动**且不删他人锁（不把自己误认成持有者）；死 PID → 安全接管。
- 环境绑定：live 无 profile → `STATE_ROOT_PROFILE_UNLOCKED`；`environment=demo` + `profile=live` → `ENVIRONMENT_STATE_ROOT_MISMATCH`。
- 迁移回滚：动作失败整笔回滚且不登记版本；库回退到 v9 后重开 → 版本序列仍唯一到顶、`quick_check=ok`、业务行不丢；在线备份独立可打开且版本一致。

**KI02（天然检验点）**：本工作树**无 `.venv`**，`smoke_windows_service_plan.py` 仍 **24 通过 / 0 失败**，无 `ValueError`；`smoke_u13_supervision.py` 已被登记进官方入口（`run_offline_regression.py:100`），全量 **84 脚本 0 失败 exit 0** → U13 声称的 KI02 修复与 P1-1 残留**在本树已闭合**。

---

## 4. 未能离线验证的部分

1. **真实交易所侧成交/回执**：全程用进程内假 broker（只记账、不发请求）；真实 `query_order`/私有流时序未验。
2. **真实断电/撕裂写/独立卷灾难恢复**：演练为同卷（`SAME_VOLUME_NOT_DISASTER_RECOVERY`）；P3-3 的自然竞态窗口宽度未压测（只做了确定性重放 + 人为放大窗口的跨进程复现）。
3. **真实多账户凭据、SCM 双 profile 注册、真实宿主托管、真实告警**：未涉及。
4. **台账声称的"真实 TradingService 端到端 40/40"在提交的测试集中不存在**（`test_supervision.py` 内 0 处 `TradingService`，`check_gate_production_wiring` 仅是**源码文本匹配**）。本会话用自建隔离-root 端到端（paper，15/0）替代；**live 档端到端未做**。
5. `authorize_control_request` 的环回/角色判定只做代码核对（未发 HTTP）。

---

## 5. 产物路径

- 本报告：`docs/dispatch/reviews/U12/drill_F14_F18/REPORT.md`
- 可复跑探针与原始输出：`docs/dispatch/reviews/U12/drill_F14_F18/raw/`
  - 探针（**以 `.py.txt` 后缀归档**，内容逐字节不变）：`drill_f14.py.txt`、`drill_f14_e2e.py.txt`、`drill_f18.py.txt`、`probe_bundle_identity.py.txt`、`probe_live_restore_layout.py.txt`、`race_child.py.txt`、`race_child2.py.txt`
  - 原始输出：`raw_f14_drill.log`、`raw_f14_e2e.log`、`raw_f18_drill.log`、`raw_probe_bundle_identity.log`、`raw_probe_live_restore_layout.log`、`raw_smoke_*.log` 等（全量离线回归日志另存 `raw_offline_regression.tail.txt`）
  - **为什么用 `.py.txt`**：I 最初以 `.py` 归档，结果这 7 个探针被 `check_quality.py` 的 ruff 扫到（25 条 E401/E402/E702/F401），**把 RC 门禁 `check_quality` 弄红**。改用 `.py.txt` 使 ruff 的**扫描范围逐字不变**（不新增任何 exclude、不降低门禁），同时保留证据逐字节原样。要复跑：复制到沙箱后把后缀改回 `.py`。

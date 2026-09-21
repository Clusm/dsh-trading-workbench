# 执行恢复点（RESUME）

本文件是中断恢复的**唯一入口**。任何新会话（包括被 20 分钟定时任务唤醒的会话）先读本文件，再决定动作。

规则：**每完成一个步骤，I 必须立即更新本文件并 commit。** 禁止长时间持有未提交状态——交接说明 §5 记录的三次会话中断，全部损失都来自长会话未提交。

---

更新时间：2026-09-21 13:4x（北京时间）
更新者：I（claude-code 编排会话，接自 opencode 交接）

## 1. 当前状态

- **U00 已完成并 INTEGRATED**；`contract-v3.0` 已冻结
- **contract_commit = `ffdab443cae45c1451b5cc13b24e3e73c22038ff`**（tag `contract-v3.0`）
- 已验收候选：`71f8109`（Q r5 复验 PASS-with-notes；与 tag 的差异仅为 dispatch 文档，`src/core/contracts` 与 `docs/contracts` 逐字节相同）
- 集成区分支：`integration/unattended-v3` @ `ffdab44`（契约冻结后又有 1 个台账提交 `4fdc6bf`）
- 四个实现线 worktree **已创建**（均从 `ffdab44`）：`mat-wt/A`(task/U01)、`mat-wt/B`(task/U04)、`mat-wt/CD`(task/U03)、`mat-wt/EF`(task/U15)
- 当前步骤：**17/18 任务已 INTEGRATED（仅 U11 在修复轮）；W1/W2/W3 工程侧全部完成** —— **首批 14 项任务全部 INTEGRATED**（含 U09、U12、U13-FU1/FU2/FU3、U12-BF1+契约勘误 `CONTRACT-ERRATUM-1`）。**RC 仍未冻结**（DRAFT）；**W4 结构性不可在本窗口完成**。
- **后置线（P2，非首批必需）进行中**：`U06b` **已 INTEGRATED**；`U10b` 作者已交付→Q 复验中；`U07` 已派发；`U11` 等 U10b。
- 最新门禁：**`OFFLINE REGRESSION OK scripts=85`**、**`QUALITY CHECK OK files=301 untracked=0`**、契约 `PASS 173 / FAIL 0`。
- **恢复门禁三条（N1/N2/N3）已全部闭合并复验**：Q 的 110 格 `purpose×status` 矩阵 **0 处放松格**（pristine/FU2 各 8 处 → 修复 0），集成提交 `57a4e97`。
- **RC 冻结仍待四项**：`D05`（SLO 时限冻结，需外部决策）、观察窗三项 `NOT_STARTED`（7天/200/30，结构性不可压缩）、`MODEL_ROUTE_MOCK`（须配真实模型路由后重铸 manifest）、`U12-INTEGRATION-RUNTIME-INSTANCE-PLACEHOLDER`。

### W1 进度（每轮更新）

| 线 | 任务 | 作者 head | 状态 | 备注 |
|---|---|---|---|---|
| EF | U15 | `abf2eda` | **✅ INTEGRATED** | r1 E/B/C → r2 闭两 P0 → r2 复验剩 NEW-A → r3 修（`IDENTITY_GENERATION_MISMATCH`/409，无假阳性）→ **Q r3 = PASS-with-notes** → 合并 `5ffa54d` + 补丁 `2954dce`。**代码与已验证 head 逐字节一致（git diff 为空）**；69/69 非基线脚本 PASS。**U02 阻塞项**：账本 `account_ref=sha256(api_key)[:16]` 仍漂移 |
| A | U01 | `fb12a38` | **✅ INTEGRATED** | r3 复验 PASS-with-notes：**05 补丁必需且正确**（168 行/9 文件逐条核对）；**全量 delta = ∅**（独立确认 `NEW=0 GONE=0`，两态唯一失败均 KI02）。I 应用 5 个补丁 + 原子登记两个 `smoke_u01_*` → 全量 **TOTAL=73 FAILED=1（仅 KI02）**。修复轮各关键项（`[9]` 真跑、content_hash 对 K03、重放不抹账）回归保持。**遗留 P3**：4 处文档内嵌断言仍写死 `current_schema_version()==16`（不参与 smoke 执行，故 delta 为空）→ `U01-R3-FU1` |
| CD | U03 | `fb8ae5a` | **✅ INTEGRATED（接线暂缓）** | r3 复验 PASS → 集成时暴露"仅集成态可见"缺陷（测试读现网文件二次打补丁）→ r4 修好（两态均通过 + 行尾归一化、不削弱漂移检测），**真实集成树绿灯**。**但 U03 的 orchestrator 接线补丁使管线缺 pinned 身份时 fail-closed 拒绝全部开仓 → 既有 `smoke_p0b_loop.py` 与 `smoke_ui.py` 新增失败**（仅 U15 集成时两者均 PASS）。决策：**暂缓接线补丁**（保留模块 + S01 + 登记），撤销后两者恢复（28/0、52/0）。接线依赖 U15 身份接入管线 → 归 **U12 BUILD 联合候选**。 |
| B | U04 | `8660c55` | **r4 修复完成 → 聚焦复验中** | r4 已修 **N6**（`FILLED` 必须由分量背书，否则退回推导）→ 两条路径 `filled=0.02/working=0/terminal=0.00/viol=()`；**补齐 `X=FILLED` 覆盖**（154→**167 通过/0 失败**，未修树 155/12 失败=非空转）；**N7 选择修**（`EXPIRED_IN_MATCH` 归一为词表内终态、可重水化、历史遗留行自愈）；**N2 残余如实写入测试注释**（结构断言挡不住"对象正确、读路径退化"，并发实测 6~20/20000 裸仓）。全量 delta = ∅ |

**W1 出口状态：✅ 完成** —— **U00 / U01 / U03 / U04 / U15 全部 INTEGRATED**。U03 的 orchestrator 接线暂缓（`W1-JOINT-WIRING`，归 U12）。W1 共经历：U01（r2+r3）、U03（r2+r3+r4）、U04（r2+r3+r4+r5）、U15（r2+r3）多轮修复 + 每轮独立复验；每条任务的集成都做了**代码与已验证 head 的逐字节一致性证明**，并在补丁应用后跑**全量离线回归与 pristine 的失败集合差集**。

### W2 进度

| 线 | 任务 | 分支/base | 状态 | 备注 |
|---|---|---|---|---|
| CD | U05 真实耗时/模型降级/决策原因 | `task/U05` | **✅ INTEGRATED** | 初审 B P1×5 → r2 闭 4 项 → r2 复验发现 P1-5 残留 + 7/15 补丁不可应用 → r3 修（放宽收紧为"**名字须真被绑定**"，6 类自造绕过全报、`api_key=<已绑定变量名>` 不误报；15/15 在集成配置下可应用）→ Q r3 **PASS-with-notes** → 集成 + 登记 4 冒烟。四套 115/63/48/45、delta ∅ |
| EF | U13 托管/单实例/备份恢复 | `task/U13` | **✅ INTEGRATED** | 初审 P1×3 → r2 修（安装戳 fail-closed、`submit_recovery_reconcile()` 接线、KI02 收口）→ Q r2 **PASS-with-notes** → r3 按集成 HEAD **重生成 7 个补丁**（其余 5 文件零差异，因合并已带入 r1 共享改动）→ 7/7 干净应用 → **跨任务验收全绿**（U15/U01/U04 的套件均 exit 0）→ **KI02 消除：`smoke_windows_service_plan` 24/0**，**官方单命令入口首次端到端成功 `OFFLINE REGRESSION OK scripts=75`** |
| B | U02 策略仓管理与有界退出 | `task/U02` | **✅ INTEGRATED** | A 初审 P0×1 → r2 闭 P0+P1（迁移方案升级为"可执行"）→ r3 闭 3 条 P2（`_consumed_ids` 事实判重 / `release()` 幂等 / `confirm_exit` 结算使声明成真）→ **Q r3 PASS-with-notes** → 集成：6 补丁在 U13/U05 集成后**仍 6/6 干净应用**、登记冒烟（MANIFEST OK 76）、158/0、**check_quality OK files=280**、**OFFLINE REGRESSION OK scripts=80**。**残余（重要）**：`_consumed_ids` 是**纯内存 set** → "已消费不可重发"**仅在进程内成立**，重启后同 `exit_key` 会再次 `SUBMIT_REDUCE`（已登记 P2-9） |
| CD | U06a 影子最小隔离与口径修正 | `task/U06a` | **✅ INTEGRATED** | 初审硬结论：**影子样本确实会进发布门槛**（200→300）→ 登记为 **U09 硬前置**（`S08-SHADOW-GATE-LEAK`）。r2 修 P2-1（陈旧当新值）/P2-2（披露渲染）→ **Q r3 PASS-with-notes**（两项真闭合、13/13；漂移测试非空转；边界 11 全 A/0 M、共享文件与 `release_evidence.py` 逐文件 SAME）→ 集成：3 补丁、登记冒烟（77）、264/0、**check_quality OK files=283**、**OFFLINE REGRESSION OK scripts=81** |
| EF | U14 外部告警送达与主机失联 | `task/U14` | **✅ INTEGRATED** | r3 闭 **P1-4**（单条隔离改为对任意异常 fail-closed；三向量 + 兜底桶全收敛，`KeyboardInterrupt` 仍穿出，`_isolate_failure` 用 SQLite 触发器验证）→ **Q r4 PASS-with-notes**（七行混合批无早退；全模块仅 4 处 `except Exception`；delta 四树全 ∅）→ 集成：4 补丁、登记冒烟（78）、262/0、**check_quality OK files=286**、**OFFLINE REGRESSION OK scripts=82**。**残余（须传达 U12）**：`AlertQueue` **无生产消费者** → 门禁全绿 ≠ 无人值守告警可用；兜底桶把新 bug 表现为"该条持续退避"而非崩溃 → 接线须监控 `errors`（且 `PERSIST_FAILED` **不在** `errors`，有计数盲区） |
| CD | U08 策略漏斗与管理责任展示 | `task/U08` | **✅ INTEGRATED** | r4 修 **N6**：三异常（OSError/PermissionError/sqlite3.OperationalError）x 四路径 = 12 条，**前后全部 ESCAPED -> 全部 NO_ESCAPE**；其中 **`ledger.consume()` 写盘失败回滚内存**（`mem_consumed=false`/`disk_consumed=false`，不留脏"已消费"）、孤儿落盘可审计、异常名进 note（诊断未丢）；**`KeyboardInterrupt`/`SystemExit` 仍穿出**（两态一致）。N7 已修（`exec-<iso>`）。补丁 `9195782/` 对集成 HEAD `1a412440` **三配置 15/15 `--check` OK**（无 Skipped）；delta `83->84`、`NEW_FAIL=[]`（167 次执行全 rc=0）；harness **OK scripts=84**；test 135/0→208/0、smoke 58/0→91/0；**check_quality OK files=290**。**登记残余**：`release()` 落盘失败仍返回 True（盘上可能短暂仍记已消费）|
| EF | U10a 可运行发布包与运维说明 | `task/U10a` | **✅ INTEGRATED** | r2 修 3 条 P2 -> **Q r3 PASS-with-notes**（9 类变异硬失败且点名、**反向不误杀**：正常仓库 exit 0/216 成员；fence 门禁双向非空转；台账在包内且自检如实列出、移出即 5 条断链；**claim ② 承载它的 2 处均已更正**）-> 集成：4 补丁（旧目录已由作者移除）、登记冒烟（79）、smoke_u10a 37/0、**check_quality OK files=287**、**OFFLINE REGRESSION OK scripts=83**。**P3-7 附录 A 的 C1–C14 已可逐条核对**（补救上轮"无法 1:1 核对"）。**新 5 条 P3 已登记**，其中 N5：P3-10 未处理且交付物内无登记（需 U12/U15 归属） |

**W2 门禁状态：两个 RC 门禁持续全绿** —— `OFFLINE REGRESSION OK scripts=84`、`check_quality QUALITY CHECK OK files=290`。

**U08 集成落点**：`37716d6`（merge 已验证 head `08ca75d`，代码逐字节一致，`git diff 08ca75d HEAD` 为空）→ `17b1d6f`（应用 `9195782/` 5 补丁，05 承载 SCRIPTS 登记）。两态实测：`SMOKE MANIFEST OK included=80`、test 208/0、smoke 91/0、契约 173/0、quality files=290、`OFFLINE REGRESSION OK scripts=84`（**NEW_FAIL=∅**）。
**U08 r5 复验（Q）= PASS-with-notes**：N6 真闭合（3 异常 × 4 路径 12 条全部 `ESCAPED→NO_ESCAPE`，写盘失败真回滚内存、孤儿可审计、`KeyboardInterrupt` 仍穿出）；N7 真修。**新登记 `U08-R5-FU1`（N10，P2，fail-closed）**：`release()` 撤销会被**同轮收尾 `save()` 的 `_reload_locked()` 合并规则覆盖**（盘上 `consumed=True` 而内存 `False ⇒ 采纳盘上`），导致撤销静默丢弃、`release_count=0`、执行器侧"账本释放未完成"分支成**死代码**；须更正 README §七 的机制描述与那条不可达分支。**`U08-U12-WIRING`**：`graph.py` 未注入 funnel/ledger，`round_explanation()` 生产可达性 0。
达成路径：U13 修好 KI02（解释器解析层）→ 官方单命令回归不再中止；KI04 给密钥扫描器加**范围豁免**（仅审查报告与台账，代码/config 照扫）；清理 U04 补丁引入的 4 个 lint；修正扫描器自测的登记数断言；I 自身在 RESUME 里留下的 KI01 式字面量也已改写。

**集成进度：12 已 INTEGRATED** —— U00 · U01 · U02 · U03 · U04 · U05 · U06a · U08 · U10a · U13 · U14 · U15（**W1 5/5 + W2 7/7 全部集成**）。**剩余**：U09、U12（W3，尚未开始）；W4 结构性不可在窗口内完成（7 天观察窗）。

**W2 出口状态：✅ 完成** —— U05/U13/U02/U06a/U14/U08/U10a 全部 INTEGRATED。累计 12/12 已派发任务。
**U09 已解除前置阻塞**：U08 集成后 `S08-SHADOW-GATE-LEAK` 的处置方（U09）就绪；U09 定义方向候选分母前**必须**先给 `release_evidence.py` 加 `source` 过滤。

**W2 派发已内联 W1 的 7 条血教训**（见 §「复验范围经验」与 §「派发新任务时的强制要求」）：两态可跑的测试、迁移版本推进必须带伴随补丁并给 delta、禁止 SKIP/直接注入内部字段、共享适配必须交付 diff 文件、冒烟须登记、增量提交、自证 pristine delta。

**集成时必须原子处理（否则 `run_offline_regression` 红）**：U01/U03/U04 的 `scripts/smoke_*.py` 需在集成时与 `SCRIPTS` 登记**同批落地**（`run_offline_regression.py` 由 I 独占）。U15 用 `tests/acceptance/*.py` 命名，不触发该门禁。详见 JSON `dispatch.known_issues` 的 `W1-MANIFEST-TRANSIENT*`。

**KI02（既有基线缺陷，阻断完整回归）**：`scripts/smoke_windows_service_plan.py` 读 `config/windows_service.json`，其 `python_executable` 硬编码 `<project_root>/.venv/Scripts/python.exe` → **任意 worktree 内必然 `ValueError` 中止 `run_offline_regression`**（运行目录有 `.venv` 故不受影响）。已在 U15 补丁之前的提交上复现确认与 U15 无关。**集成其他任务时同样会遇到**：补跑中止点之后的脚本即可（用同一 harness 清单 + env），但该缺陷须由 U13/U10a 或 I 修复，不得静默忽略。详见 JSON `known_issues` 的 `KI02`。

**已知基线缺陷（非本批次引入，勿重复排查）**：`scripts/smoke_windows_service_plan.py` 在部分 worktree 失败 —— `config/windows_service.json` 指向 worktree 内 `.venv`，而该 worktree 无 `.venv`（A 线作者实测发现）。

**派发新任务时的强制要求（吸取 U15 首轮中断教训）**：提示词内联约束，只让它读契约实现与目标源文件；**必须**要求作者交付"完整 unified diff 文件"到 `docs/dispatch/patches/<ID>/<head>/`，而不是只在报告里文字描述。

### 事故记录
- **2026-09-21 01:48 U15（EF 线）作者会话异常中断**：仅运行 2.6 分钟 / 34 次工具调用，返回的是半句话（"Now I have a full picture. Let me write the core module."），**未产出任何文件**（`mat-wt/EF` 工作树干净、无提交）。
  - 根因研判：该会话的前置阅读量最大（手册 §4/§7/§8.1 + 角色卡 + 任务卡 + 契约 README 全文 + k01_identity.py + F 指纹报告 + 4 个目标源文件 ≈ 2000 行），疑似上下文耗尽即被截断。
  - 处置：**已用精简提示词重新派发**（内联约束、限定只读 k01_identity.py + README 的 K01 章节 + 指纹报告 + 4 个目标文件；并强制"尽早写代码、尽早提交"）。
  - 教训（后续派发沿用）：**不要把 2000+ 行的必读清单塞进提示词**；把约束内联进提示词，只让它读契约实现与目标源文件；并要求"第一个文件写完立刻提交"。
- **2026-09-21 02:01 U15 初审 C（`reviews/U15/C/r1`）审查会话异常中断**：运行 68 秒 / 16 次工具调用，返回半句话（"Both author suites pass. Now let me write my own independent probes."），未产出报告。
  - 已排除的原因：**不是测试输出过大** —— 实测作者两个套件输出仅 114 行 / 52 行（4.6KB / 1.7KB）。中断与 token 量也无强相关（此前 174k tokens 的会话正常完成）。
  - 研判：属交接说明 §5 记载的**间歇性会话中止**，非提示词设计缺陷。
  - 处置：**重试**，并加两条降险措施（后续全部沿用）：
    1. **产出边做边写盘**（"把发现随时追加写入报告文件"），中断只损失增量，不损失全部；
    2. **所有命令输出重定向到文件**（`> _scratch_X/xxx.txt 2>&1`），只 `tail`/`grep` 摘要进上下文；
    3. 编排者**预先核验作者套件**并把结果写进审查提示词，避免审查者重复消耗预算。
  - **总原则：中止是常态而非异常 —— 派发必须假设"这次可能死"，用短会话 + 增量提交 + 增量写盘把单次损失压到最小。**
- **2026-09-21 02:16 U01 r2 修复会话异常中止**（返回半句话"Now let me regenerate patch 01 with all five required changes:"）。
  - **损失**：+1743 行未提交改动（模块 +672、测试 +1680/-609），且**补丁 01 未重生成**。
  - **处置（按 §5 恢复流程）**：实测该状态**不通过** —— `test_income_reconciliation.py` exit=1（`TypeError: AuditDatabase.fetch_income_coverage() got an unexpected keyword argument 'start_ms'`，因作者已按要求移除 `[9]` 的 SKIP 从而暴露真实失败，而补丁 01 仍未重生成）。故**未**按"通过即 recovery commit"处理，而是保全为**明确标注 INCOMPLETE / tests RED 的 WIP 提交 `fb1378c`**，再派**接续会话**（`ab4b4b0a…`）从中断点继续。
  - 安全检查：该会话在工作区留下的 `data/`（7 个 `llm_coordination/*.lock`，11KB，已被 `.gitignore` 忽略）与 `.ruff_cache/` 为**测试残留**，扫描无凭据/账户标识，**非**真实数据；已要求接续会话不要提交它们。

### 恢复经验（写入此处以便复用）
- 中止后**不要**先假设"有未提交改动就落 recovery commit"：**必须先实跑该任务的验收命令**。测试红 → 保全为标注 INCOMPLETE 的 WIP 提交 + 接续会话；测试绿 → 才落正常 recovery commit。
- 接续会话的提示词必须带上**实测的精确失败**（命令 + 报错原文 + exit code），而不是"接着修吧"。

### 复验范围经验（W1 血的教训，W2/W3 必须沿用）
**缺陷集中在"单元已验证、但适配未落地"的缝隙里。在作者 head 上复验会系统性漏掉它们。**
已发生两例：
- **U03**：测试读**现网** `market_query.py` 再打补丁 —— 只在补丁**未应用**时成立；I 一落地即二次应用报 `patch context mismatch`。r3 复验记为 P3「证据卫生」→ 实为集成级失败。
- **U01**：补丁 01 的**迁移 17** 使 9 个既有共享冒烟新增失败（`versions==range(1,17)` 等），作者还谎称「对既有表零影响」。

**因此（强制）**：
1. 复验提示词必须要求复验者**在隔离沙箱中把该任务的补丁实际应用**，再跑该任务验收**及受影响的既有共享冒烟**，而不是只看作者 head。
2. 复验者必须**跑全量 `run_offline_regression`（或等价的受影响清单）并与 pristine 基线做增量对比**，明确区分"本任务引入的新失败"与"KI02 等既有失败"。
3. 集成前，I 自己也要对"补丁应用后的树"跑一遍；作者 README 的"零影响""已修"等断言**一律不作为证据**。
4. 作者产出的测试**不得假定补丁是否已应用**（U03 N4 / U01 P3-N4 同类），必须两态可跑。
5. **测试不得硬编码绝对迁移版本集合**（`versions == list(range(1,17))`、`== 16`、`== [1..16]`）。已两次致损：
   - **U01** 新增迁移 17 → 打破 9 个既有脚本的 `range(1,17)` 断言（靠补丁 05 伴随修复）；
   - **U04** 的 `test_execution_faults §[9]` 把集合写死为 `1..16`（在其 base 上成立），被 U01 的迁移 17 打破 → 集成态 166/1。
   **正确做法**：断言**相对性质**（"本任务的改动不改变集合" —— 同测试内取改动前后两次快照比较），而不是断言等于某个具体集合。
6. **同一列/同一字段的语义必须跨任务统一**。已致损：U05 的 patch 09 把 `latency_ms` 填成 `failed_rounds`，而 U01 的 `review.py:231-237` 用 `AVG(latency_ms)` 聚合每日复盘 → 跨任务数据污染（`U05-COUPLING-REVIEW`）。派发画布类任务时须声明"你要写入的共享列/字段的既有语义与消费者"。
7. **安全控制的放宽必须 AST/tokenize 级**：逐行文本级放宽会把**注释/docstring/内嵌 YAML·JSON** 里的明文凭据静默漏报。U05 patch 13 即因此削弱密钥检测（且该修法方向本是编排者建议的 —— **编排者的建议同样要经独立审查**）。
8. **共享适配补丁必须针对"将被应用的那棵树"生成**。U13 的 12 个补丁按 base `0e522b1` 生成，而集成 HEAD 已含 U15 对 `config_loader.py`/`app.py` 的改动 → 集成树上 11/12 直接失败、`--3way` 仍在 6 个文件冲突。**正确做法**：作者读集成版本（`git show <integration-HEAD>:<path>`）并在其上叠加自己的改动，再以"集成版本→目标内容"出 diff；且必须证明**没打坏已集成的他人改动**（跑其它任务的关键验收）。**不要**由 I 手工猜冲突解法。
9. **派发新任务时，task_base_commit 取"当时已审核的集成 HEAD"**，并在提示词里**明确告知该 base 已含哪些任务**，避免作者按陈旧 base 生成适配。
10. **建分支后必须回读分支名**：`git checkout -b <branch> <commit>` 在 worktree **有未提交改动**时会**静默不切换**（本次 EF 即如此：U13 r3 生成补丁后留下 7 个未提交的共享文件改动，`checkout -b task/U14` 直接 Aborting，而同一批的 CD 却成功了）。若同时已派发该分支的作者会话，作者会落在**错误的旧分支**上。**处置**：先确认那批改动是否等于已交付的补丁产物（用 `git apply -R --check` 逐文件反证），是则丢弃，再建分支，并**回读 `rev-parse --abbrev-ref HEAD` 确认**。
11. **集成清单必须包含 lint**：U04 集成时我只跑了测试与全量回归、**没跑 ruff**，结果 4 个 lint 错误（3 未用导入 + 1 多余 f 前缀）直到最后跑 `check_quality` 才暴露。**每次集成后都要跑 `check_quality`（含 ruff）**，不能只靠任务自己的套件。
12. **归档审查报告时，必须把报告目录与台账 JSON 放进同一次 `git add`**。已两次出现"报告 cp 到磁盘、但提交只 add 了台账 JSON"，结果报告一直处于未跟踪状态（靠 cron 检查点才发现）。未跟踪的评审证据等于没有证据 —— 一次误清理就永久丢失。
13. **新增失败模式：agent stall（`no progress for 600s`，watchdog 未恢复）**。U06a 首轮即如此，与之前的"截断式中止"不同 —— 它**静默停住约 10 分钟**（cron 观察到 3 分钟零写入）。**且该会话违反了增量提交**：跑了约 40 分钟、**零提交**，中断后只能靠 WIP 保全（`f0c0e48`），当时 `shadow_status.py` 未完成、测试不存在、共享文件 `src/intel/service.py` 被改并混进 WIP。
    - **处置**：WIP 保全 → 派续做会话，并要求它**先把共享文件改动导出为补丁、再用 `git checkout <base> -- <file>` 从分支还原**（任务分支最终不得含共享文件改动）。
    - **教训**：① 增量提交不是形式要求，是**这个环境里唯一可靠的损失上限**；② 续做提示词必须写明"上一会话零提交"并要求**开头就提交一次**；③ 作者工作树里出现共享文件改动时，续做会话有义务把它转成补丁并还原。
17. **应用任务补丁后，必须用 `git status` 核对"补丁实际触碰的全部文件"再提交**。U10a 集成时我按"文档清单"手写 `git add` 路径，漏了补丁 04 另外改到的 3 篇文档（`多Agent执行分派手册`/`开发规格书_v2`/`项目说明书` 各 1 行绝对路径占位化），导致它们以**未提交**状态留在树里 —— 靠随后的 `git status` 才发现并补交。**做法**：补丁应用后**先 `git status --short`**，把 `M` 的文件全部纳入同一次提交；**不要凭记忆列举路径**（补丁的触及面由补丁决定，不由任务卡决定）。

18. **不要把可运行的 `.py` 证据/探针归档进被 lint 的树内**。我把 F14/F18 演练的 7 个探针以 `.py` 复制进 `docs/dispatch/reviews/U12/drill_F14_F18/raw/`，`check_quality.py` 的 ruff 立刻扫到它们，产生 **25 条** E401/E402/E702/F401，**把 RC 门禁 `check_quality` 弄红**（此前一直是 `OK files=290`）。而 U13-FU1 的作者把这 25 条**误归因**给"U02/更早的归档脚本"，并据此提了一条"让 check_quality 变绿"的可选补丁（会给 ruff 加 exclude）。
    - **处置**：把 7 个探针改名为 `.py.txt`（内容逐字节不变），**ruff 的扫描范围逐字未变**、未新增任何 exclude、门禁未收窄，`check_quality` 恢复绿。
    - **做法**：归档**可运行脚本**时用 `.py.txt`/`.py.orig` 之类后缀，或存到被 lint 范围之外；**任何"让门禁变绿"的配置改动（exclude/ignore）都要先问"变红的原因到底是什么"** —— 按文件分组实测（`ruff check <dir> --output-format=concise | sed 's/:.*//' | sort | uniq -c`）比接受归因快得多。
    - **衍生教训**：作者对**集成树现状**的叙述可能错误（本例作者说 base 的 check_quality 就是红的，实测 base 在我改名前的红**是我造成的**）。**集成前凡涉及"某个门禁本来是红的/本来就是好的"的断言，一律自己复跑。**

16. **子代理会往集成树根写 git archive 产物**（已两次：`base.tar` 8.4MB、`ac.tar` 8.5MB —— 均来自用 cwd 在 `mat-wt/I` 里跑 `git archive`）。二者都**未被 .gitignore 忽略**，属"随时可能被误提交"的散件。
    - **处置**：会话运行中**不删**（可能正在被其沙箱使用），等其结束后删除；I 的 `git add` **一律带路径前缀**（`git add -A src/ scripts/ tests/`、`git add docs/...`），所以根级散件不会被误纳入。
    - **做法**：派发时继续强调"产物只落在自己 worktree 内"；若要建 pristine 沙箱，用 `git -C <主仓> archive ... | tar -x -C <自己的临时目录>`，或 `git -C <主仓> worktree add`，**不要 cd 进别人的工作区**。

15. **路径统一用绝对路径，不要依赖 cwd**。U08 建审查区时我在 cwd= 下执行 `mkdir -p "../mat-wt/reviews/U08/B"` -> 实际创建了嵌套的 `mat-wt/mat-wt/reviews/U08/B`，于是 `git worktree add` 因**正确父目录不存在**而失败；又因命令用 `&&` 串联，**台账更新被一并跳过**。已用绝对路径重建并回读确认，错误嵌套目录已清理。
    - **做法**：凡涉及 `mkdir`/`git worktree add`/`cp` 的路径一律写**绝对路径**；也**不要**把"建工作区"与"写台账"串在一条 `&&` 链里（前一步失败会静默吞掉后一步）。

14. **建审查 worktree 后必须回读确认**（与 lesson 10 同源，针对**审查区**）。U14 r3 复验时我**没成功建出** `reviews/U14/R3/r1`，却已在提示词里把它写成工作区；复验者落到一个**普通目录**，自行用 `git archive` 沙箱实现了 detached 语义并产出完整报告（**结果可用**），但这属于**运气**：提示词指向不存在的工作区时，不同会话可能做出不可预料的事。
    - **做法**：`git worktree add --detach <path> <commit>` 之后**必须 `git -C <path> rev-parse --short HEAD` 回读**，确认路径存在且指向目标提交，再把它写进提示词。
    - 附带发现：该会话的 Bash **无法把命令行里字面写出的中文路径透传给子进程**（`ls`/`python -c` 报 No such file or directory），它改用 `$(cd <glob>/mat-wt && pwd)` 动态取值绕过。派发时若涉及中文路径，宜让作者/复验者用动态取值而非字面量。

> **判定规则（按序判断，避免重复派发）：**
> 1. W1/W2 已收口（12/12 INTEGRATED）。**当前应做的是 §2 步骤 1：派发 W3 的 U09**。
> 2. 若 `task/U09` 上**已有作者提交**但 `docs/dispatch/reviews/U09/<candidate>/` 下无对应审查报告，且无审查会话在运行 → 审查会话中断 → 重新派发**审查**（不是作者）
> 3. 若 U09 的 `task/U09` 上**无作者提交**，且**确实没有** U09 作者会话在运行 → 作者会话已中断 → 走 §5 恢复流程
> 4. 若 U09 作者/审查会话**仍在运行** → 本回合**什么也不要做**，直接结束（避免并发重复劳动）
> 5. 若 U09 已 INTEGRATED → 进入 U12 BUILD（注意 D01/D02 未决则 U12 的宿主/渠道验收不可做，其余离线部分照做）
>
> 判断"是否仍在运行"：看 worktree 内 `git log`/`git status` 是否仍在变化，而不是凭 RESUME 的文字。

## 2. next_action（按顺序）

> **W1 / W2 / W3 工程侧全部完成** —— **首批 14 项任务全部 INTEGRATED**（U00–U15 中全部首批必需项 + U09 + U12）。**仅剩 W4，而 W4 结构性不可在本窗口完成**（7 天挂钟 + D01–D05 外部待决 + 实盘授权）。
> **当前在做的是「后置线」（非首批必需，P2）**：U06b 与 U10b 已派发（U12 集成后二者依赖就绪）。

### 后置线进行中（勿重复派发）
- **`U06b` 影子 v2 固定时点标签**：**✅ 已 INTEGRATED**（`d9a0734`，merge `e738e2c`）。工作树 `mat-wt/D2`，head `8ae0f65`（base `0706db1`）。**Q r1 = PASS-with-notes（无阻塞项）**，报告 `docs/dispatch/reviews/U06b/8ae0f65/Q_r1.md`。
  - **门禁不倒性由 Q 自建夹具独立复现**：`directional_candidates` 3→3、`release_readiness` NO_GO→NO_GO、报告除 `generated_at` 外逐字相同；**再加灌 500 条 v2 行候选仍 3**；**越权测试**传 `counts_toward_release_gate=True` 落库仍全 0；`grep -rn shadow_v2 src/core/` **0 命中**（门禁根本不读该表）；两关键 blob 未变（`shadow_status.py` `afa53766be`、`release_evidence.py` `c146b89c75`）。
  - 集成实测：shadow v2 套件 **100/0**、`smoke_u06a` 52/0、`smoke_u09` 60/0、契约 173/0、`check_quality OK files=301`、**`OFFLINE REGRESSION OK scripts=85`**。
  - **集成顺序已核实为真且必要**（Q 复现了单独打补丁 02 的 `ImportError`）。
  - 新登记 `U06B-P3-CLUSTER`（补丁 02 缺空行 / `autocrlf=true` 下整文件转 CRLF / `sampler.clock_verdict_for_window()` 死代码）。
- **`U07` 配对实验与独立样本统计**：**✅ 已 INTEGRATED**（`afbcf8f`，merge）。工作树 `mat-wt/D3`，head `e610319`（base `c2f1bab`）。**Q r1 = PASS-with-notes（无阻塞项）**，报告 `docs/dispatch/reviews/U07/e610319/Q_r1.md`。
  - **门禁不倒性加压**：Q 除自建夹具外，**用裸 SQL 绕过写入层**强行把 `counts=1/weight=9.9/promo=1` 塞进研究表，门禁报告**仍逐字段不变** → 门禁根本不读该表。两关键文件 diff 0 字节。
  - **补丁 02 经隔离反向测试裁定"不是削弱"**：字面删 `labels.py` 会先撞 `ModuleNotFoundError`（未到断言），Q 改做"改名 + 改 3 处导入让 import 链活着、只让 glob 少一个" → 断言**立刻变红**且**点名缺哪个模块**；完整 diff 仅 +8/−4、唯一 hunk 只动那一处 check。原 `==` 属过度约束。
  - **统计口径经数值审计**（91 项探针 BAD=0）：二项检验与 `math.comb` 及 **2^n 暴力枚举**三方一致；独立块数 7 组手算全吻合；`naive_understates` 在强重叠下 0.2236 vs 0.5000（真）、近似独立下差 0.0（不虚报）；费用压力插值=解析解且**区间外返回 None 不外推**。
  - 集成实测：statistics **127/0**、labels 100/0、smoke_u09 60/0、契约 173/0、`check_quality OK files=303`、**`OFFLINE REGRESSION OK scripts=85`**。
  - 新登记 `U07`/`U06B` 相关 P3 若干（`ENTRY_DIVERGENT` 原因码可细分、压力点退回 iid 未分块、`block_means` O(n·|keys|) 等）。
- **`U11` 核心模块渐进拆分**：**Q r1 = CHANGES_REQUESTED（2 阻塞 + 6 备注）→ 修复轮 `U11-FU1` 进行中**（同一分支 `task/U11`）。
  - **阻塞 1**：映射**漏掉 `decision_trace.py` 第 961–1799 行（839 行 = 46.6%、28 个公开名）**，且 §3.1 称"天然三块"实为 ≥6 块；`strategy_funnel.py` 同漏 1804–2138 段 4 名。**只搬前半、后半留门面，V1 照样逐字通过** → 漏 47% 文件的映射能通过它自己的出口。
  - **阻塞 2**：§5"不动清单"里**没有任何 `tests/`**（全文 `grep "tests/"` = 0），而 `tests/integration/*`（12 套件）才是行为断言真身（12 个 `smoke_*.py` 只是透传退出码的薄壳）→ 实施者改测试 import 指向新子模块，**smoke 仍 exit 0、V2 静默通过**。责任归属亦缺（未提 U08）。
  - **口径发现 C1**（非"作者算错"）：V1 用 `dir()`（86/158 名）而**不是 `__all__`**（61/71），且不检查 `__all__` → 会对"清理门面未用 import"产生**假红**。
  - Q 确认**通过**的部分：§7 的 10 条命令逐条重跑 **10/10 数字与 exit code 逐字符合**；V1 基线**独立复现 4/4 哈希一致**。
  - **门禁不倒性**：smoke_u09 60/0、test_release_evidence_v2 78/0、test_shadow_labels 100/0、契约 173/0、smoke_u06a 52/0 —— **前后同值**；套件内还有逐字段断言（灌统计快照后门禁报告除 `generated_at` 外相同、`directional_candidates` 3→3、来源过滤仍 `[SOURCE_A]`）。delta `NEW_FAIL=∅`；`check_quality OK files=303`。
  - **待 Q 裁决的关键一条**：补丁 02 把**别人（U06b）的**模块集断言由"集合相等"改为"集合包含"。**已要求 Q 做反向测试**：删掉 U06b 四模块之一必须立刻变红，否则=真削弱。另要求审**统计口径本身**（`naive_understates` 真假、独立块数手算、二项符号检验 p 值独立核对、费用压力区间外是否拒绝外推）。
- **`U11` 核心模块渐进拆分**：**作者已交付 → Q 复验中**。工作树 `mat-wt/F3`，head `00a49a8`（base `03967e9`）。**仅两个新增文件、未触碰任何 `src/`、0 个共享补丁**：`docs/重构接口映射.md`(356) + 补丁包 README。
  - 方案：拆 `decision_trace`(P1)/`strategy_funnel`(P2)/`runtime_identity`(P3，需 U15 作者在场)；**明确建议"不拆" `service.py`（最大模块）**，理由是它是 I 集成面 + N1/N2/N3 账本事实注入链路、无独立套件可证行为不变。另给 16 项"不动清单"与**公开面 SHA-256 快照**断言工具及基线。
  - **I 已抽查行数**（1799/2138/2464/1826）**与文档逐字相符**。
  - **待 Q 复验**：逐条重跑文档所列命令比对 exit code；**独立重新实现** V1 快照并与文档基线哈希比对；**独立枚举"绝不能动"清单找漏项**；核对拆分建议是否写清责任归属（`strategy_funnel` 属 U08、`runtime_identity` 属 U15，拆后谁复跑）。
- **两个复验会话均在运行，勿重复派发。**

<!-- 以下为 U06b 派发/交付时的记录，保留备查
- **`U06b` 影子 v2 固定时点标签**：工作树 `mat-wt/D2`，head `8ae0f65`（base `0706db1`），**共享文件已从分支还原**、只在补丁包 `docs/dispatch/patches/U06b/0f1f21c/`（2 diff）内。
  - 作者称门禁**不受影响**：写 50 条 v2 标签后 `directional_candidates` 3→3、`release_readiness` NO_G0→NO_GO、**门禁报告除 `generated_at` 外逐字相同**；`shadow_status.py` **一个字符未改**；`current_schema_version()` 仍 17；无新增 ruff exclude。两态 PASS=89/0（未打补丁）、100/0（打完）；delta **NEW_FAIL=∅**；补丁 6/6 干净。
  - **集成顺序（作者注明）**：**先合 `src/intel/shadow_v2/`，再应用两个补丁**，否则 `service.py` 的 `from intel import shadow_status, shadow_v2` 会 ImportError。
  - **未做（如实登记）**：v2 **未接入采集循环**（默认开关 `False`）、未做采集调度编排、未做 U07 的配对统计。
  - **复验已派发**：Q（`reviews/U06b/R1/q`，detached @ `8ae0f65`），重点用**自建夹具**独立复现门禁不倒性（不采信作者 evidence 脚本）。**仍在运行，勿重复派发。**
- **`U10b` 旧脚本与历史文档整理**：**作者已交付 → Q 复验中**。工作树 `mat-wt/F2`，head `0416408`（base `0706db1`）。
  - 18 个脚本 `git mv`（**文件名不变**）→ `scripts/diagnostics/`(13) / `scripts/maintenance/`(5)，其中 16 个修根深度 `parents[1]→parents[2]`；8 篇文档 `git mv` → `docs/archive/`（称 `-M` 全 `R100`）。30 个文件全在独占路径。
  - **覆盖未减少（作者实测）**：`SMOKE MANIFEST OK included=81 excluded=1`、`OFFLINE REGRESSION OK scripts=85` **前后同值**；**一个 `smoke_*.py` 都没动**（移出 `scripts/` 顶层 = 静默削减覆盖，已写成归档规则首条）。打包器 `smoke_u10a_release_package` 37/0 两态；delta `NEW_FAIL=∅`；`check_quality OK files=296`；补丁 3 配置干净。
  - **分支本身是"红"的（刻意）**：不应用补丁时 `docs/文档索引.md` 有 8 条入链断；共享补丁 `docs/dispatch/patches/U10b/273892c/01_docs_index_archive_links.diff` 应用后恢复。
  - **空删除清单**：`docs/archive/README.md` 给出核验方式（`--no-renames` 下 `D=26`、26/26 都有同名新增配对、孤儿删除=0）+ "已核实但不归档"保留清单及逐条理由。
  - **复验已派发**：Q（`reviews/U10b/R1/q`，detached @ `0416408`），重点：**自己算**每个改过的 `parents[...]`、**应用补丁后**是否仍有断链、**归档脚本是否还在发布包内**。**仍在运行，勿重复派发。**
- 依赖它们的：`U07`（等 U06b）、`U11`（等 U10b）。

### 后置线完成后
- **W4 不可在窗口内完成**（7 天挂钟观察窗 / 200 方向候选 / 30 完整闭合；D01–D05 外部待决；实盘启用须用户另行授权）—— 须如实报告，**不得标记为通过**。
- **RC 冻结仍待四项**：`D05`（SLO 时限冻结）、观察窗三项 `NOT_STARTED`、`MODEL_ROUTE_MOCK`（须配真实模型路由后重铸 manifest）、`U12-INTEGRATION-RUNTIME-INSTANCE-PLACEHOLDER`。

<!-- W3 起点记录，保留备查
> **W0 / W1 / W2 均已完成**（W1 5/5、W2 7/7 全部 INTEGRATED）。以下为 W3 起点。

1. **W3 第一步：U09（✅ 已 INTEGRATED）** —— 工作树 `mat-wt/W3`，分支 `task/U09`，base `448a4e9`。
   - 集成：merge `b675118`（owned 路径逐字节一致）→ 应用 `docs/dispatch/patches/U09/7cb6a71/` 的 01–04 补丁（04 承载 SCRIPTS 登记）→ 集成提交 `4e164a1`。
   - **双审查均无阻塞项**：`Q r1 = PASS`（10 项核心主张独立复现，且 **monkeypatch `release_gate_role` 使分母 200→300，证明同源非巧合**；三态 A 26/0 · B 65/0+冒烟 9/0 · C 78/0+冒烟 60/0）；`A r1 = PASS-with-notes`。报告归档 `docs/dispatch/reviews/U09/b675118/{Q_r1.md,A_r1.md}`。
   - 集成态实测：`SMOKE MANIFEST OK included=81`、U09 验收 78/0、smoke_u09 60/0、契约 173/0、`QUALITY CHECK OK files=294`、**`OFFLINE REGRESSION OK scripts=85`**。
   - **S08 泄漏已闭合**：方向候选分母 300→200（只计 `gate_role==execution_decision_copy` 的 A 组），且**无第二份 source 名单**。
   - **登记的两条 P2（U12 须裁决，不阻断集成）**：`U09-OUTCOME-VS-LIFECYCLE-COST`（门禁读 outcome 快照而非 lifecycle 权威态，**继承自基线**）、`U09-NET-UNKNOWN-PADDING`（净收益未知仍计入 30，1 个未知样本可把 29 条真凭证凑成 30）。另有 P3 若干。
   - **A 的关键定论**：`[S3]` 与 `[T5a]` 不矛盾（S3 的"冲突"因 `INSERT OR REPLACE` 同键就地覆盖而**根本不是冲突**，`PASS 30` 正确；T5a 是真冲突 -> `FAIL 29` fail-closed）-> **未发现"用冲突样本凑数"的 fail-open**。

<!-- 以下为 U09 首次派发时的记录，保留备查
1. **W3 第一步：U09（作者已交付，独立审查进行中）** —— 工作树 `mat-wt/W3`，分支 `task/U09`，base `448a4e9`。
   - **作者 head `7cb6a71`**（补丁包提交 `b675118`）。独占交付：`src/core/release_evidence.py`（538→1483）、`tests/acceptance/test_release_evidence_v2.py`（新 989）、`scripts/smoke_u09_release_evidence.py`（新 783）。补丁包 `docs/dispatch/patches/U09/7cb6a71/`（4 diff + README + evidence/）。
   - 作者自述关键数：**S08 泄漏 300→200（Δ−100）**；三态 A 26/0（基线证明缺陷存在）· B 65/0 · C 78/0，冒烟 9/0 → 60/0；沙箱 delta pristine 84 → patched 85、**NEW_FAIL=∅**；3 种 git 配置 4/4 干净应用。
   - **I 已独立抽查两项**：①`release_evidence.py` 内**无**第二份 source 名单（走 `intel.shadow_status.release_gate_role`）；②门禁阈值 **200/30/7.0/0.999/120 全部未变**、`release_readiness` 派生方式未变。
   - **审查已并行派发**：Q（`reviews/U09/R1/q`）与 A（`reviews/U09/R1/a`），均 detached @ `b675118`。**两者仍在运行，勿重复派发。**
   - 交付：`src/core/release_evidence.py`（扩展，不重写）＋ `tests/acceptance/test_release_evidence_v2.py` ＋ `scripts/smoke_u09_release_evidence.py` ＋ 补丁包 `docs/dispatch/patches/U09/<head>/`（含 `database.py`/`health_watch.py`/`report_release_readiness.py` 适配 + `run_offline_regression.py` 登记）。
   - 已内联进提示词：S08 泄漏的具体行号与修法（**从 `src.intel.shadow_status` 导入 gate_role 口径，禁止硬编码第二份 source 名单**）、NO_EDGE 不计分母、**窗口首尾缺失计入分母**（现 `observation_coverage` 自动取首尾健康行 = 规格 §12 明禁）、lifecycle 去重 + 晚到成本重算、ACK≠恢复、重启 vs 换版、不建第二套 GO 判断器、F10/F12/F16/F23/F24 证据、3 种 git 配置补丁可应用、两态测试、禁止硬编码迁移版本集合。
   - **作者会话仍在运行 → 本回合不要重复派发 U09。**
2. **U12 BUILD（✅ 已集成 · RC 仍为 DRAFT）**：RC 交付物与 F01–F25 矩阵已集成（candidate `044fccf`，逐字节一致）。**RC 未冻结**。矩阵覆盖：COVERED_OFFLINE 4 · PARTIAL_OFFLINE 20 · **DEFERRED 1（F11，逐字 `F11:v2_labels=DEFERRED`）** · 缺能力 9 类（§D 逐条列出），**25/25 有离线证据但 15 条带 ⚠**；`observed_status` 三项显式 `NOT_STARTED`。
   - **集成时暴露并已按证据收窄一处过严断言**：U12 的 §8 原写"身份完整 → 不得带任何阻断码"，该断言只在 `collect_artifact_files` 于真实 `src/` 树抛错（`U12-BUILD-F1`）时成立。hop1+hop2 修复后身份**真正完整**（顶层 `artifact_hash`/`effective_config_hash`/`model_route_hash` 均为真实哈希），余下只有 `MODEL_ROUTE_MOCK`（**环境级**阻断 = 配的是 Mock 模型路由）。已收窄为"完整身份不得带 `IDENTITY_COLLECT_FAILED`"；两态扫描/一致性断言未动。验收由 46/1 → **47/0**。
   - **⚠ 更正 I 先前一句写错的话**：我曾说"身份三字段仍为 None"——**错误**。当时我读的是 `manifest['identity']` 这个**并不存在**的嵌套键。实测顶层字段**均已非空**。故 `U12-BUILD-F1` **已真正修复**；`release_readiness` 在 Mock 路由下仍不可达 GO 属**正确行为**（不该用 Mock 路由发布），不是缺陷。
   - **新登记（U12 收口项）**：`U12-INTEGRATION-RUNTIME-INSTANCE-PLACEHOLDER` —— `runtime_instance_id` 实测为字面量 `'unknown'`（占位符）却因"非空"被判为完整身份，方向 fail-open，建议明确 `'unknown'` 应视为"运行代缺失"。
   - **⚠ 新发现的 RELEASE 阻断 —— `U12-BUILD-F1`（I 已独立复现）**：`collect_artifact_files('.')` 在真实 `src/` 树抛 `CredentialLeakError: ... artifacts.src/core/management_authorization.py` —— U02 的模块名命中冻结契约里 `SENSITIVE_KEY_MARKERS` 的 **`"authorization"` 子串**。后果：身份采集失败 → `IDENTITY_COLLECT_FAILED` → 身份三字段全空 → **`release_readiness` 永远不可能 GO**。**U15 验收套件对此全盲**（只用合成根/显式 relatives）。**修复已派发**：`U12-BF1`（工作树 `mat-wt/W3-bfix`，`task/U12-BF1` @ `7885e6a`）—— 取向：给**文件名**一条独立守卫，只拒凭据文件形态，**冻结契约一个字不改**，且必须给出"凭据文件名仍被拒"的**反向证据**。
   - U12 待裁决项 A1–A13 已全部登记台账（含 **A12 = D05 阻断 RC_FREEZE**）。
   - **`U12-BUILD-F1` 已 FIXED（两跳）**：hop1 = `U12-BF1` @ `a401633`（已 merge：工件路径改用**独立文件名守卫**；`collect_artifact_files('.')` 由**抛** `CredentialLeakError` 变 **OK 125 文件且含 `management_authorization.py`**；**反向 15/15 凭据文件名仍被拒**，且 `id_rsa`/`cert.p12` 在 pristine 下**根本未被拒**、现在被拒）；hop2 = **`CONTRACT-ERRATUM-1` @ `4eca189`（I 亲自应用）** —— `hash_artifact` 文档即"相对路径 → 内容"，却把该映射过**字段名**守卫 → 合法模块名命中 `authorization`。新增 `_structured_path_mapping()`（保留结构化载荷/Mapping 检查，去掉字段名守卫）；**字段名键的映射守卫不变**。契约冒烟仍 **PASS 173 / FAIL 0、golden 160 逐字未变**（只把误报的抛错变成正确行为）。**这是对冻结契约的修改，已作显式勘误登记**（`dispatch.contract_erratum`）。
   - **诚实边界**：修完后 `identity_blockers` 只剩 `('MODEL_ROUTE_MOCK',)`，**身份三字段仍为 `None`**（未配置真实模型路由 = 环境条件）。故本条**不能**单独推出"`release_readiness` 可达 GO"。
   - **RC 冻结前置阻断项（四条）**：`D05`（SLO 时限冻结，需外部决策）、观察窗三项 `NOT_STARTED`（结构性不可压缩）、`MODEL_ROUTE_MOCK`（须配真实模型路由后重铸 manifest 并复验身份/门禁）、`U12-INTEGRATION-RUNTIME-INSTANCE-PLACEHOLDER`（运行代占位符判据）。

### W3 收尾修复（并行）
- **`U13-FU2`（Q 复验 = CHANGES_REQUESTED，修复轮 `U13-FU3` 进行中）**：head `2ac0d6f`，补丁包 `docs/dispatch/patches/U13-FU2/ce412fa/`（3 diff）。**尚未集成。**
  - **N2 的"无可达行为差异"自述被 Q 推翻，且方向是放松（比不改更糟）**：门禁 CLEARED 后账本出现**非 ENTRY** 单（`purpose=EXIT`/`status=UNKNOWN`）时，pristine 阻断启动/停 `close_only`/拒 `set_control('normal')`；**patched 放行 / `normal` / `set_control('normal')` 成功**。
  - **根因（结构性）**：`service.py:891` 附近 `_recovery_gate_ledger_facts(facts=None)` 的 `unknown_orders`/`submitting_orders` **取自形参、缺省硬编码 0**；FU2 新接线的两条路径都是**无参调用** → 这两列恒 0，绕过门禁的**真实账本读取**（门禁不分 purpose 全表计数）。
  - **N1 PASS**（10 场景独立复现三处 fail-open 翻转）、**N3 PASS**、9 组绕过符合预期、`test_supervision.py` 的改动**不算放宽断言**（断言文本零改动；反证：patched `recovery.py` + pristine 测试 → 恰好 2 条失败，都是旧夹具**靠 N1 的 fail-open 才通过**的"合法对账应放行"断言）、full delta `NEW_FAIL=∅`、补丁 9/9、check_quality 两树均绿。Q 还独立确认**补丁 05 不必要**（base `ruff check .` 已全绿；把归档探针改回 `.py` 立刻报 30 错）。
  - **修复已交付：`U13-FU3` @ `1e9f760`（代码 head `5651531`）→ Q 复验进行中**（同一分支 `task/U13-FU2` 继续提交，工作树 `mat-wt/W3-fix2`）。
    - **修的是结构问题，不是特判**：新增 `_ledger_outbox_counts()`，三列**一律**从本进程 `order_outbox` 实读；SQL 谓词与门禁 `ledger_reconcile_facts` **逐字同源**（`FACTS_KEYS`/`UNRESOLVED_ORDER_STATUSES` 从 `core.ops.recovery` **导入**）；`unknown_orders`/`submitting_orders` 的 WHERE **无 purpose 条件** → 任意 purpose × 任意 status 同规则覆盖。形参 `facts` 降级为**额外下界（逐列 max）**：只可能更严、绝不更松。
    - Q 的原探针 `[C] unknown_exit` 三方对照：pristine **阻断/close_only/拒** → FU2 **放行/normal/成功** → **修复 阻断/close_only/拒，`unknown_orders`=1（账本实读），exit 0（10/0）**。扩展验证器 pristine 47/11 → FU2 54/4 → 修复 **58/0**。
    - N1/N3 未回退（`recovery.py` 未改；probe_n1 11/0、bypass 13/0、n3 6/0）；delta **NEW_FAIL=∅**；`check_quality.py` 两树逐字节相同、`OK files=295`、无新增 exclude；补丁 `01` 两种 base × 3 配置 6/6 干净、`02` 为分支内独占。
    - **如实登记**：`[unknown_exit] 冷启动就绪快照` 在 pristine 上本就 FAIL（其 `service.py:411` 本来也是无参调用）→ 属**顺带收严**，非"修复态比要求更严"；新计数**不按 `environment` 过滤**（刻意与门禁一致，多环境共库面未实测）；读不出账本时多开一次只读连接。
    - 复验会话：`reviews/U13-FU3/R1/q`（detached @ `1e9f760`）。**仍在运行，勿重复派发。**

- **`U13-FU2`（首次派发记录，保留备查）**：head `2ac0d6f`，补丁包 `docs/dispatch/patches/U13-FU2/ce412fa/`（3 diff）。
  - **N1 修好**：状态根**没有账本文件** / **没有 `order_outbox` 表** 两种形态由 `allowed=True`（fail-open）变为 `allowed=False / EVIDENCE_UNVERIFIED`；四态对照无回归无误伤；**复用** `EVIDENCE_UNVERIFIED` 无新码；before exit=1(4/3) → after exit=0(7/0)。
  - **N2 修好（接线形态）**：启动期 2 次调用由"1 次裸"变"0 次裸"并注入账本事实；`set_control` 由裸变注入。**作者如实说明：无"可达的行为翻转证据"**（服务开库即创建 `-shm`，把门禁读取从 `immutable` 切成 WAL 感知，行为结果两态相同）。**已要求 Q 独立判定这条"无行为差异"是否属实**——若构造得出可区分窗口即为重要发现。
  - **N3 修好**（README 与代码措辞一致）。副作用：`tests/acceptance/test_supervision.py` 的 [7]/[7b] 补了 `_make_gate_ledger()`，**断言原文一行未改**（已要求 Q 逐行核对是否算"放宽断言"）。
  - **未收紧的口子（已登记）**：`complete_reconcile` 在账本不可读时仍可自声明 `CLEARED`（N1 之后该 CLEARED 不产生放行能力，但状态位会被写脏）；完全收紧需在 `complete_reconcile` 侧也拒绝 → 下一轮。
  - 复验已派发：Q（`reviews/U13-FU2/R1/q`，detached @ `2ac0d6f`）。**仍在运行，勿重复派发。**

<!-- U12 首次派发记录，保留备查
2. **U12 BUILD（已派发，进行中）**：RC 集成、故障矩阵（F01–F25）、真实宿主/渠道验收（**依赖 D01/D02 决策**）。
   - 工作树 `mat-wt/W3-rc`（`task/U12` @ `7885e6a`）。交付：`docs/releases/unattended_rc/`（RC 说明 + F01–F25 矩阵 + `observed_status`）、`tests/acceptance/test_unattended_release.py`、CI 补丁、`docs/dispatch/patches/U12/<head>/`。
   - 已内联：**F11 必须逐字记 `F11:v2_labels=DEFERRED`**；缺能力的**不得写 N/A**；F14/F18 引原始演练报告与 U13-FU1 复验报告、**不得记为"完全通过"**；必须逐条消费台账里的跨任务挂账；状态写 **DRAFT**，**冻结 RC 由 I 决定**。
   - **仍有一个并行修复 `U13-FU2`**（工作树 `mat-wt/W3-fix2`，`task/U13-FU2` @ `7885e6a`）：修 `U13FU1-BARE-BEFORE_ENTRY-FAILOPEN`（N1 裸 `before_entry()` 缺账本时 fail-open + N2 `service.py:314/:1416` 未注入事实）+ N3 文档不一致。**RC 内容须含 U13-FU2 落地后的代码**。
   - **两个会话均在运行，勿重复派发。**
   **RC 前置阻断项（4 条 P1）**：`F14-DRILL-GATE-NOT-INSTALLED`、`F14-LIVE-GATE-LAYOUT`、`F14-GATE-EVIDENCE-SELFASSERTED`、`F18-STATE-ROOT-MISSING-UNREACHABLE` —— 由 `U13-FU1` 修复并复验后才可谈 RC 冻结。**F14/F18 的必达结果在这 4 条修好之前不算成立**，U12 的矩阵不得把它们记成通过。
   U12 必须一并消费的跨任务挂账：`W1-JOINT-WIRING`（U03 接线 × U15 身份，解开开仓 fail-closed）、`U08-U12-WIRING`（graph.py 整轮聚合）、`U08-R5-FU1`（N10 更正）、`AlertQueue` 无生产消费者（监控 `errors`，注意 `PERSIST_FAILED` 不在其中）。
3. **W4 不可在窗口内完成**（7 天挂钟观察窗、D01–D05 外部待决、实盘启用须用户另行授权）——须如实报告，**不得标记为通过**。

### 并行进行中（与本回合同时运行，勿重复派发）
- **U09 作者会话**：工作树 `mat-wt/W3`（`task/U09` @ `448a4e9`）。
  - 进度快照：已落**首个提交 `79d5c4a`**（独占产物：扩展后的 `release_evidence.py` 等）。
  - **注意（中断恢复要点）**：该分支工作树当前**有 4 个共享文件处于未提交修改态** —— `src/audit/database.py`、`src/core/health_watch.py`、`scripts/report_release_readiness.py`、`scripts/run_offline_regression.py`。按规则这些**不得提交进任务分支**。若该会话中断：接续会话必须**先把它们导出为 unified diff 到 `docs/dispatch/patches/U09/<head>/`，再用 `git checkout 448a4e9 -- <这 4 个文件>` 从分支还原**（lesson 13）。
  - 该分支**当前无作者提交**才走重派；有 `79d5c4a` 则走 §5 接续流程，**不要**重派整条线。
- **W3 隔离恢复演练（F14/F18）**：**✅ 已完成**（`mat-wt/W3-drill`，detached @ `448a4e9`）。报告与原始证据已归档：`docs/dispatch/reviews/U12/drill_F14_F18/REPORT.md` + `raw/`（7 个可复跑探针 + 22 份原始输出）。
  - **判定：F14 = PASS-with-notes（有条件的）；F18 = PASS-with-notes**。F14 在**生产恢复脚本**路径上必达成立（先对账再开仓、不重复下单，`broker.posts` 全程未增加）；F18 在**显式状态根**下必达成立（换代码根不换锁/不换身份、PID 复用拒启动、迁移回滚不损坏）。
  - **4 条 P1（I 已独立核对源码证实）**：`F14-DRILL-GATE-NOT-INSTALLED`、`F14-LIVE-GATE-LAYOUT`、`F14-GATE-EVIDENCE-SELFASSERTED`、`F18-STATE-ROOT-MISSING-UNREACHABLE`；另有 5 条 P2（`RECOVERY-BUNDLE-P2-CLUSTER`，含**既有**缺陷"截断锁文件单方接管 → 双持锁"）与 4 条 P3。全部已登记进台账 `dispatch.known_issues`。
  - KI02 天然检验通过：该树**无 `.venv`**，`smoke_windows_service_plan.py` 仍 24/0 无 `ValueError`；官方入口 `OFFLINE REGRESSION OK scripts=84` exit 0。
- **E 线修复 `U13-FU1`**：**✅ 已 INTEGRATED**（`acf9ced`）。工作树 `mat-wt/W3-fix`（`task/U13-FU1`，base `943d839`），head `af76443`。
  - **Q r1 = PASS-with-notes（无阻塞项）**，报告 `docs/dispatch/reviews/U13-FU1/af76443/Q_r1.md`。
  - **4 条 P1 全部 FIXED**（台账已置 FIXED）：原始探针在 pristine 上确证缺陷、在 patched 上**同一条断言翻转**（`drill_f14` 19/0 exit0 → 16/3 exit1；`drill_f18` 31/0 exit0 → 29/2 exit1；live 档状态 JSON 从平铺 `data/` 变为全落 `data/live/`）。
  - **12 项绕过尝试全部挡住**，含"先 CLEARED 再塞未收口单会**重新阻断**（非一次性门禁）"。仅两条**作者已声明的边界**确认存在：门禁文件+安装戳**同时**删、篡改账本 `order_outbox` 行。
  - **`drill_f14_e2e` 那 4 条失败：Q 独立裁决为"收紧"而非"修坏"**（自建探针 6/0：真对账后提交同一份证据 → `CLEARED`；再塞未收口单 → 重新阻断）。
  - **修正了作者的错误归因**：25 条 ruff **全部**来自 I 归档的演练探针（`docs/dispatch/patches/` 的 U02 归档 **All checks passed**）→ **补丁 05 已弃用**，未新增任何 exclude。
  - **新登记两条（U12 须裁决，不阻断集成）**：`U13FU1-BARE-BEFORE_ENTRY-FAILOPEN`（**N1+N2**：裸 `before_entry()` 在缺账本时 fail-open；且 `service.py:314` 启动 / `:1416` `set_control` **仍是未注入事实的裸调用**，作者验证器完全不触及 `service.py`）、`U13FU1-README-VS-CODE-N3`（README 称 demo 不阻止启动，实测 `assert_environment_bound('demo')` 会抛）。
  - 集成实测：verifier 39/0、U13 冒烟 `OK steps=4`、契约 173/0、`QUALITY CHECK OK files=295`、**`OFFLINE REGRESSION OK scripts=85`**。

- **E 线修复 `U13-FU1`（首次派发记录，保留备查）**：工作树 `mat-wt/W3-fix`（`task/U13-FU1`，base `943d839`），head `af76443`，工作树干净。
  - 分支内 6 文件：`src/core/ops/recovery.py`(+275)、`src/core/runtime_paths.py`(+56)、`scripts/ops/verify_recovery_plan.py`(+50)、`scripts/restore_recovery_bundle.py`(+40)、**新增** `scripts/ops/verify_u13f1_recovery_gate.py`(+400)、`scripts/smoke_u13_supervision.py`(+7)。
  - 补丁包 `docs/dispatch/patches/U13-FU1/bc112fe/`：`01`（必选·P1-3 生产接线）、`02`（必选·P1-2 建包 profile）、`03`/`04`（建议·P1-4 可观测）、`05`（**I 判定不采用**，见下）+ README + `evidence/`（before/after 日志）。
  - **`05_optional_docs_probe_lint.diff` 不采用**：作者把 `check_quality` 变红归因给"U02/base 的归档脚本"，**该归因经 I 按文件分组实测不成立** —— 25 条 ruff **全部来自 I 自己归档的演练探针**；已改名 `.py.txt` 修复，`check_quality` 现为 `OK files=290 untracked=0`。应用 `05` 会新增 exclude = 收窄门禁范围，故弃用。
  - **待 Q 裁决的关键一条**：`drill_f14_e2e.py` 打全补丁后 **11 通过 / 4 失败**，作者称为"P1-3 之后的收紧（必须先真对账再提交证据）"而非回归 —— 已要求复验会话独立判定。
  - **复验已派发**：Q（`reviews/U13-FU1/R1/q`，detached @ `af76443`）。**仍在运行，勿重复派发。**

### 两个会话的共同恢复要点
- U09 与 U13-FU1 的工作树**各有若干共享文件处于未提交修改态**。若任一会话中断：接续会话必须**先把共享改动导出为 unified diff 到各自的 `docs/dispatch/patches/<ID>/<head>/`，再用 `git checkout <base> -- <文件>` 还原**，任务分支最终不得含共享文件改动（lesson 13）。
- U09 工作树另有**已改未提交的独占文件**（`src/core/release_evidence.py`、`tests/acceptance/test_release_evidence_v2.py`）—— 接续会话应先核对它们是否自洽并**立即提交**。
  - F14 = 从恢复包启动期间有交易所已成交订单 → **先全量对账/增量回拉再开仓，不重复下单**。
  - F18 = 新代码目录/旧状态根/PID 复用/两账户同 profile·同账户不同 profile → **状态路径与身份一致、单账户互斥、不新建错误基线、迁移可回滚**。
  - 注意：该工作树**没有 `.venv`**，正好检验 **KI02**（解释器解析）是否真的修好。

**W1/W2 已完成动作（存档，勿重做）**：建 A/B/CD/EF 四个 worktree → W1 派发 U01/U04/U03/U15 → 交叉审查 → 修复轮 → Q 验收 → 集成；W2 派发 B线U02、CD线U05→U06a→U08、EF线U13/U14→U10a。

合法集成顺序（JSON `dispatch.first_release_example_order`）：
`U00 → U01 → U04 → U03 → U15 → U02 → U05 → U06a → U08 → U13 → U14 → U10a → U09 → U12`

## 3. W1 任务速查

| 线 | 目录/分支 | 任务 | 独占路径 | 集成目标（I 合入） | 审查 |
|---|---|---|---|---|---|
| A | `mat-wt/A` `task/U01` | U01 资金费流水与成本闭环 | `src/exchange/income_reconciliation.py`、`tests/integration/test_income_reconciliation.py`、`tests/fixtures/income/`、`src/agents/review.py`、`calibration.py`、`lessons.py`、`tests/integration/test_review_statistics.py` | `src/audit/database.py`、`src/exchange/trade_reconciliation.py`、`src/exchange/binance_futures.py` | B、Q |
| B | `mat-wt/B` `task/U04` | U04 部分成交/保护/重放 | `src/exchange/protection_commands.py`、`tests/acceptance/test_execution_faults.py`、`tests/fixtures/execution/` | `src/exchange/order_outbox.py`、`protection_mirror.py`、`binance_futures.py`、`trade_reconciliation.py`、`src/audit/database.py` | A、Q |
| CD | `mat-wt/CD` `task/U03` | U03 逐意图可验证研究证据 | `src/agents/research_evidence.py`、`tests/integration/test_research_evidence.py`、`tests/fixtures/evidence/`、`tests/integration/test_market_evidence_quality.py` | `src/agents/models.py`、`orchestrator.py`、`src/core/decision_policy.py`、`graph.py`、`decision_pipeline.py`、`src/data/market_query.py`(适配) | D、B、Q |
| EF | `mat-wt/EF` `task/U15` | U15 运行身份/有效配置/控制权限 | `src/core/runtime_identity.py`、`tests/acceptance/test_runtime_identity.py`、`tests/acceptance/test_control_boundary.py` | `src/core/release_manifest.py`、`config_loader.py`、`src/dashboard/app.py`、`src/core/entry_readiness.py` | E、B、C、Q |

**关键**：U15 必须先于 U02/U13；U04 必须先于 U02；U03 必须先于 U05。
**共享文件**（`src/audit/database.py`、`src/exchange/binance_futures.py`、`src/data/market_query.py`、`src/agents/models.py` 等）只由 **I** 合入；作者提交适配补丁建议，不得直接改。

## 4. U00 残留跟进项（不得静默忽略，登记在 JSON `dispatch.known_issues`）

| ID | 内容 | 处置 |
|---|---|---|
| `U00-R5-FU1` | T3/T5 新增拒绝面缺自动化 golden 覆盖（README `L20` 理由失实） | 建议 U12 RC 前补 op 驱动用例纳入 smoke 门禁（Q r5 已证 harness 支持，K05-N14 先例） |
| `U00-R5-FU2` | `rules` ≥5000 层嵌套触发裸 `RecursionError`（NEW-R5-P3-1） | 建议补 README §12 `L21` 登记或加 `RecursionError` 守卫 |
| `L17` | K03 三外部标志（`source_available`/`pages_complete`/`late_recheck_done`）仍 mint 侧可伪造 | 已诚实登记于 README §12（P2 已知限制，不阻断） |

## 5. 恢复流程（子代理会话中断时）

1. `git -C "C:\Users\Tao\Desktop\币安\mat-wt\<线>" status --short` 与 `git log --oneline -3`
2. 若改动属于该任务范围 → 跑该任务的离线验收命令；通过则以 **recovery commit** 落盘（提交信息注明）
3. 不通过或改动不完整 → **不要** `reset --hard` 清场；保留改动交给新作者会话继续，并把失败点写进本文件
4. 更新本文件 §1/§2 后继续

## 6. 硬约束（任何会话不得违反）

1. 运行目录 `C:\Users\Tao\Desktop\币安\multi-agent-trader` **只读**：禁止在其上 checkout/合并/改配置/执行开发代码。demo 服务自 9/16 起在跑（PID 12984/25928），不得停止。
2. 不读/不复制 `.env`、`data/`、密钥、账户标识；不启动真实服务、不下单撤单、不发真实告警。
3. 风险档、白名单、live 开关一律不动；`audit_only` 不得改 enforce。
4. 共享文件只由 I 合入；子代理提交适配补丁，不得直接改主台账。
5. JSON 台账只由 I 更新；审查报告归档到 `docs/dispatch/reviews/<ID>/<candidate>/<role>_<session>_r<n>.md`。
6. 作者可本地提交；**禁止 push / 合并主线 / 打 tag**（I 可本地集成与打标签；远程推送需用户另行授权）。
7. 发布门禁不降低（7 天 / 200 方向候选 / 30 完整闭合）；**实盘启用必须由用户另行明确授权**。
8. 子代理**增量提交**（每步一提交），禁止长会话持有大量未提交改动。

## 7. 关键路径与命令

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
export PYTHONUTF8=1
# cwd = C:/Users/Tao/Desktop/币安/mat-wt/I
"$PY" scripts/smoke_contracts_v3.py          # 期望 PASS 173 / FAIL 0；golden 160 (45+/115-)
"$PY" scripts/run_offline_regression.py      # 离线回归（含 smoke manifest）
```

- 台账：`docs/无人值守升级任务清单_2026-09-19.json`（唯一状态源，只由 I 写）
- 契约规格（**下游唯一依据**）：`docs/contracts/README.md`；schema `docs/contracts/*.schema.json`；golden `docs/contracts/golden/`
- 分派手册：`docs/多Agent执行分派手册_2026-09-20.md`；角色卡 `docs/dispatch/角色卡.md`；任务卡 `docs/dispatch/任务卡.md`
- 交接说明：`docs/dispatch/交接说明_2026-09-21.md`
- U00 审查报告：`docs/dispatch/reviews/U00/{db7e0d7,0b85bef,b36d8a9,5ee082b,71f8109}/`

## 8. 结构性不可在 8 小时内完成（须向用户如实报告，不得标记为通过）

- W4 发布门禁为 **7 天挂钟观察窗口**（手册 §3 明示"无法通过并行加速"）
- **D01–D05 外部待决项**阻断 DEPLOY_DEMO / OPS / RELEASE（D01 宿主托管、D03 账户风险覆盖需用户决策）
- 实盘启用需用户**另行明确授权**
- 真实宿主/渠道验收（U12 OPS）需 D01/D02 决策后才能进行

-->

-->

-->

# 执行恢复点（RESUME）

本文件是中断恢复的**唯一入口**。任何新会话（包括被 20 分钟定时任务唤醒的会话）先读本文件，再决定动作。

规则：**每完成一个步骤，I 必须立即更新本文件并 commit。** 禁止长时间持有未提交状态——交接说明 §5 记录的三次会话中断，全部损失都来自长会话未提交。

---

更新时间：2026-09-21 02:05（北京时间）
更新者：I（claude-code 编排会话，接自 opencode 交接）

## 1. 当前状态

- **U00 已完成并 INTEGRATED**；`contract-v3.0` 已冻结
- **contract_commit = `ffdab443cae45c1451b5cc13b24e3e73c22038ff`**（tag `contract-v3.0`）
- 已验收候选：`71f8109`（Q r5 复验 PASS-with-notes；与 tag 的差异仅为 dispatch 文档，`src/core/contracts` 与 `docs/contracts` 逐字节相同）
- 集成区分支：`integration/unattended-v3` @ `ffdab44`（契约冻结后又有 1 个台账提交 `4fdc6bf`）
- 四个实现线 worktree **已创建**（均从 `ffdab44`）：`mat-wt/A`(task/U01)、`mat-wt/B`(task/U04)、`mat-wt/CD`(task/U03)、`mat-wt/EF`(task/U15)
- 当前步骤：**W1 进行中**（U15 已交付并进入初审；U01/U04/U03 作者会话仍在运行）

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
| CD | U06a 影子最小隔离与口径修正 | `task/U06a` | **初审 有条件GO → r2 修复中** | 首轮 agent stall + 零提交 → WIP → 接续增量提交 10 次 + 共享文件还原（diff 0 行）。head `ebbe1e9`，delta ∅。**初审硬结论：影子样本确实会进发布门槛**（实测 50B+50C 使方向候选 200→300，Δ+100；成因 `release_evidence.py:338-346` 只按 release_id/environment/signal 过滤、不按 source）→ **判为范围外依赖且作者披露准确**，登记为 **U09 硬前置**（`S08-SHADOW-GATE-LEAK`）。阻断：P1-1 冒烟未登记（**I 的原子项**）、P2-1 聚合失败仍报 `REFRESHED`（**陈旧当新值**）、P2-2 source 过滤 caveat 只存 JSON 未渲染 |
| EF | U14 外部告警送达与主机失联 | `task/U14` | **初审 CHANGES_REQUESTED → r2 修复中** | 初审 F：**"未写既有表/列"成立**（全库 schema 差集只有新表 `alert_deliveries`、交易库以 `mode=ro`+`query_only` 打开）、**9 处断言均不触发成立**、**抖动已证明不可复现且与本补丁无关**（`runpy` 证明该路径 `AuditDatabase` 构造 0 次）、delta ∅。阻断 **P1×3**：**①`dispatch_due` 未捕获 `AlertLadderViolation`** —— 供应商回执略早于本地提交（真实网关常态）→ 整批投递中断且告警**永卡 ACCEPTED 每轮复发**；**②`ops_status` fail-open**（空队列+仅 `verified=true` 报 OPS_OK；历史 DELIVERED 使 `NO_CONFIRMED_DELIVERY` 永不触发）；**③回执未绑定消息身份且无上界**（他消息回执/非字符串/未来时间戳均可标 DELIVERED，违 R14）。修完即可转 GO |
| CD | U08 策略漏斗与管理责任展示 | — | 待派发 | 线内串行：U06a → U08 |
| EF | U10a 可运行发布包与运维说明 | — | 待派发 | 线内串行：U14 → U10a |

**W2 门禁状态：两个 RC 门禁首次同时全绿** —— `OFFLINE REGRESSION OK scripts=79`、`check_quality QUALITY CHECK OK files=261`。
达成路径：U13 修好 KI02（解释器解析层）→ 官方单命令回归不再中止；KI04 给密钥扫描器加**范围豁免**（仅审查报告与台账，代码/config 照扫）；清理 U04 补丁引入的 4 个 lint；修正扫描器自测的登记数断言。

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

> **判定规则（按序判断，避免重复派发）：**
> 1. 若 `mat-wt/A|B|CD|EF` 四个 worktree 尚未创建 → 走 §2 步骤 1
> 2. 若某 worktree 的 `task/U01|U04|U03|U15` 上**无作者提交**，且**确实没有**该线作者会话在运行 → 作者会话已中断 → 走 §5 恢复流程，**不要**重新派发整条线
> 3. 若某任务已有作者提交但 `docs/dispatch/reviews/<ID>/<candidate>/` 下无对应审查报告，且无审查会话在运行 → 审查会话中断 → 重新派发**审查**（不是作者）
> 4. 若作者会话**仍在运行** → 本回合**什么也不要做**，直接结束（避免并发重复劳动）
>
> 判断"是否仍在运行"：看 worktree 内 `git log`/`git status` 是否仍在变化，而不是凭 RESUME 的文字。

## 2. next_action（按顺序）

1. **建实现线 worktree**（从 contract_commit；若目录已存在则停止核对，禁止强制覆盖）：
   ```bash
   R="C:/Users/Tao/Desktop/币安/multi-agent-trader"; C=ffdab443cae45c1451b5cc13b24e3e73c22038ff
   git -C "$R" worktree add ../mat-wt/A  -b task/U01 $C
   git -C "$R" worktree add ../mat-wt/B  -b task/U04 $C
   git -C "$R" worktree add ../mat-wt/CD -b task/U03 $C
   git -C "$R" worktree add ../mat-wt/EF -b task/U15 $C
   ```
2. **W1 派发**（手册 §4 统一模板 + 角色卡 + 任务卡 + JSON 条目）：
   - A 线 U01（作者 A；审查 B、Q）
   - B 线 U04（作者 B；审查 A、Q）
   - CD 线 U03（作者 C；审查 D、B、Q）
   - EF 线 U15（作者 F；审查 E、B、C、Q）
3. 每条线作者交付后：派发交叉审查 → 修 CHANGES_REQUESTED → Q 独立验收 → I 组候选并 ff 晋级 → 标 INTEGRATED
4. W2：B 线 →U02；CD 线 →U05→U06a→U08；EF 线 →U13/U14→U10a；I 按依赖就绪队列集成
5. W3：U09 → U12 BUILD；隔离恢复演练（F14/F18）；Q 全量 F01–F25 矩阵

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

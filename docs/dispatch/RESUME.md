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
| EF | U15 | `38029e2` | **r2 修复完成 → 复验中** | 初审 E/B/C 一致 CHANGES_REQUESTED（P0×2 + P1×17）→ r2 九个提交，**两个 P0 均闭合**（省略 `expected` 由 `True/200`→`False/409`；19/19 漂移项现全部 CHANGED、六类来源入哈希）；14/15 项已修，第 15 项属 U02/U13 路径。4 个补丁 `apply --check` 全通过。**U02 阻塞项**：账本侧 `trade_reconciliation.py:66-69` 的 `account_ref=sha256(api_key)[:16]` 仍随轮换漂移且与 U15 派生值不一致。报告 `docs/dispatch/reviews/U15/a136cb5/{E,B,C}_r1.md` |
| A | U01 | `b4a322e` | **r2 修复完成 → 复验中** | 初审 B 曾判 CHANGES_REQUESTED（P0×2 + P1×9）→ r2 重生成补丁 01（**`content_hash` 改调 K03 单一真值源**、按窗口取数、冲突审计表）。**P0-B 三段对照**：崩溃 → **静默抹账（events=0 Σ=0）** → **NO_LOSS（events=4 Σ=-1.80）**；**`[9]` 不再 SKIP 且真实走 AuditDatabase**。income 139/0、review 44/0、契约 173/0。补丁 01/02/03/04 及组合 `apply --check` 全通过；**02↔04 同批约束已复现并写入补丁 README**。报告 `docs/dispatch/reviews/U01/c290c50/B_r1.md` |
| CD | U03 | `5ffc6c0` | **r2 复验 CHANGES_REQUESTED → r3 修复中** | r2 复验 CLOSED 6 / PARTIAL 1；阻断项：**Q-NEW-1 哨兵逃逸**（显式传 `UNPINNED_IDENTITY` 得 `identity_pinned=True/gate.ok=True`）+ **Q-NEW-3 字段级分源门禁在生产路径完全不生效**（`default_required_sources=()` 且 `TradeIntent` 无该字段）。报告 `docs/dispatch/reviews/U03/5ffc6c0/Q_r2.md` |
| B | U04 | `8eb0480` | **CHANGES_REQUESTED → r2 修复中** | 初审 A：P0=0（模块本体质量高、守恒与 Decimal 全程成立）/ P1×5。三处行为：**`can_cancel_protection:1153` 把未确认归零当已确认 → 可撤掉最后一道保护**（与同模块 `plan_protection_refresh` 自相矛盾）；`guard_fault_action` 只拦 4 个健康类，未登记的落入 UNKNOWN **被放行全平**；补丁 `last_protection_sync_reason` 跨标的且成功路径不复位 → **抑制兜底平仓、保留裸仓**，而作者测试**直接赋值该字段掩盖了缺陷**。报告 `docs/dispatch/reviews/U04/8eb0480/A_r1.md` |

**W1 出口状态**：**四条线作者全部交付**（U01/U03/U15/U04）；U01/U03/U15 已完成初审并进入 r2 修复/复验；U04 初审进行中。

**集成时必须原子处理（否则 `run_offline_regression` 红）**：U01/U03 的 `scripts/smoke_*.py` 需在集成时与 `SCRIPTS` 登记**同批落地**（`run_offline_regression.py` 由 I 独占）。U15 用 `tests/acceptance/*.py` 命名，不触发该门禁。详见 JSON `dispatch.known_issues` 的 `W1-MANIFEST-TRANSIENT`。

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

# 执行恢复点（RESUME）

本文件是中断恢复的**唯一入口**。任何新会话（包括被 20 分钟定时任务唤醒的会话）先读本文件，再决定动作。

规则：**每完成一个步骤，I 必须立即更新本文件并 commit。** 禁止长时间持有未提交状态——交接说明 §5 记录的三次会话中断，全部损失都来自长会话未提交。

---

更新时间：2026-09-21 01:50（北京时间）
更新者：I（claude-code 编排会话，接自 opencode 交接）

## 1. 当前状态

- 波次：**W0 收尾**（U00 契约冻结前夜），W1 尚未派发
- 当前步骤：**U00 r5 独立复验进行中**
- 进行中的子代理：**U00 r5 复验会话（Q 角色，默认模型）**，工作区 `mat-wt/reviews/U00/Q/w0r1`（detached @ `71f8109`）
- r5 作者会话（claude-opus）**已完成**，产出 4 个提交：`cca5f27`(rules 结构化拒绝) / `523db59`(finalize_costs 重算) / `2feccaa`(dropped_input_ids 守卫) / `71f8109`(README 同步)
- 基线提交：`71f8109`（`task/U00` HEAD，工作树干净）

> **判定规则：**若你在 `task/U00` 上看到 HEAD 仍是 `3de51d9` 或更早（无 r5 的 4 个修复提交），说明作者会话已中断 → 走 §4 恢复流程，**不要**重新派发一整个作者会话。
>
> 若 HEAD 已是 `71f8109` 但 `docs/dispatch/reviews/U00/71f8109/` 下**没有** r5 复验报告，说明复验会话中断 → 重新派发**复验**（不是修复）。

### r5 已完成项（待复验确认）
- `rules` 畸形载荷 → `RulesInputError`(build) / `RULES_MISMATCH`(validate)，不再崩溃
- `finalize_costs` 改为用入参 `coverage` 内部重算，签名未变；手工伪造 proof 不再通过
- README 全面同步（§3/§4/§5/§10/§11），新增 T1–T5 收紧登记
- 残余以 §12 已登记限制处理：`L16`(STRATEGY_SOURCE_FILES)、`L17`(三个 mint 侧标志)、`L18`(dropped_input_ids 不可重算)、`L20`(新增拒绝面无法用 JSON golden 表达)
- 作者自述：golden/schema/scripts **零改动**，未改任何期望值

## 2. next_action（按顺序）

1. 确认 U00 r5 作者会话产出修复提交（在 `f1579e8` 之上）
2. 跑冒烟确认全绿：`PYTHONUTF8=1 <venv>/python.exe scripts/smoke_contracts_v3.py` → 期望 `PASS / FAIL 0`
3. 派发 **U00 r5 独立复验**（Q 角色，**独立会话、非作者**），复验 `docs/dispatch/reviews/U00/5ee082b/Q_w0r1_r4.md` §3 的 3 条阻断项是否闭合
4. 复验 PASS（或 PASS-with-notes 且残余项已登记）→ **冻结 `contract-v3.0`**：
   `git -C mat-wt/I checkout integration/unattended-v3 && git merge --ff-only task/U00 && git tag contract-v3.0`
   随后用**独立台账提交**记录 `contract_commit`（不要让一个提交记录自己的 hash）
5. 建实现线 worktree（从 contract_commit）：
   `mat-wt/A`(-b task/U01)、`mat-wt/B`(-b task/U04)、`mat-wt/CD`(-b task/U03)、`mat-wt/EF`(-b task/U15)
6. W1 派发：A=U01、B=U04、CD=U03、EF=U15（手册 §4 统一模板 + 角色卡 + 任务卡 + JSON 条目）

## 3. U00 r5 待修清单（来自 r4 报告，权威）

**阻断项（P2，必须闭合）**

| 编号 | 位置 | 问题 | 期望修法 |
|---|---|---|---|
| NEW-R4-P2-1 | `src/core/contracts/k02_evidence.py:523-524`（`_rules_hash`），被 `:648-653`、`:849` 调用；崩溃源 `common.py:41-56` | `rules` 为 Mapping 但含非 JSON 值（set/bytes/object/datetime）或非 str 键 → 抛裸 `TypeError`（r4 新回归；r3 同类 K01 `AttributeError` 被判 P2） | `_rules_hash` 前 try/except，失败转结构化 `RULES_MISMATCH`；不得崩溃 |
| NEW-R4-P2-2 | `src/core/contracts/k03_income.py:619`、`:642-668` | `finalize_costs` 只要 `proof.provable` 为真即 `COST_COMPLETE`，不重算条件 → 手工构造 `ZeroFeeProof(provable=True, coverage_hash=<正确哈希>)` 可跳过 `TerminationProof` 等全部条件 | `finalize_costs` 内部用入参 `coverage` 重算 `zero_fee_proof` 并要求与传入 proof 一致；不得签名变更（若必须变更，需在 README §10 登记为小版本行为变更） |
| Q P3-3 ≡ B N14 | `docs/contracts/README.md:262-276` | README 在 r4 **一字未改**；36 条 K02 既有用例 + K03-P5 的 inputs 变更零登记；K01-P3/P4/P5、K03-N5 未逐条登记 | 补 §11 changelog 逐条登记 |

**非阻断但应一并修（P3）**

| 编号 | 位置 | 问题 |
|---|---|---|
| NEW-R4-P3-1 | `docs/contracts/README.md` | 未登记：`RULES_MISSING`、`ZERO_FEE_PROOF_MISMATCH`、`EvidenceBundle.dropped_input_ids`、`组.unknown_field:<键>` 口径；README `:92` 仍写"应显式传 rules"而实现已改为**必传**（§10 要求收紧必须登记） |
| NEW-R4-P3-2 | `k02_evidence.py:575-585` | `dropped_input_ids` 不可重算，可凭空申报（如 `('never-existed',)` → valid） |
| NEW-R4-P3-3 | `k02_evidence.py:575` 循环前 | `model_copy` 绕过校验后 `dropped_input_ids=7` → 迭代 `TypeError`；`'abc'` → 按字符当 3 个 id（加 isinstance 守卫） |
| C P2-6（r1 残余） | `k01_identity.py:23`、`:166-176` | `STRATEGY_SOURCE_FILES` 在 `_coverage_gaps` 零引用，仅作标签；未登记 |

**已确认闭合，勿再改**：Q P2-2/3/4/5/6、Q P3-1/2、C NR-1/2/3、B N12/N13。B N15/N16 为"未变但已登记"，无需处理。

**铁律**：不得静默修改 golden 期望值（r4 已证 0 条期望变更）；若修复确实改变期望，必须逐条登记到 README §11。修复不得降低已有检查强度。

## 4. 恢复流程（作者会话中断时）

1. `git -C "C:\Users\Tao\Desktop\币安\mat-wt\I" status --short` 与 `git log --oneline -3` 查看未提交/已提交改动
2. 若改动属于 r5 修复范围 → 跑冒烟；绿则以 **recovery commit** 落盘（提交信息注明"recovery commit after session abort"）
3. 若冒烟不绿或改动不完整 → **不要** `reset --hard` 清场；保留改动交给新的作者会话继续，并把失败点写进本文件
4. 更新本文件 §1/§2 后继续

## 5. 硬约束（任何会话不得违反）

1. 运行目录 `C:\Users\Tao\Desktop\币安\multi-agent-trader` **只读**：禁止在其上 checkout/合并/改配置/执行开发代码。demo 服务自 9/16 起在跑（PID 12984/25928），不得停止。
2. 不读/不复制 `.env`、`data/`、密钥、账户标识；不启动真实服务、不下单撤单、不发真实告警。
3. 风险档、白名单、live 开关一律不动；`audit_only` 不得改 enforce。
4. 共享文件（JSON `shared_files`）只由 I 合入；子代理提交适配补丁，不得直接改主台账。
5. JSON 台账只由 I 更新；审查报告归档到 `docs/dispatch/reviews/<ID>/<candidate>/<role>_<session>_r<n>.md`。
6. 作者可本地提交；**禁止 push / 合并主线 / 打 tag**（I 可在批准范围本地集成与打标签）。
7. 发布门禁不降低（7 天 / 200 方向候选 / 30 完整闭合）；**实盘启用必须由用户另行明确授权**。

## 6. 关键路径与命令

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
export PYTHONUTF8=1
# cwd = C:/Users/Tao/Desktop/币安/mat-wt/I
"$PY" scripts/smoke_contracts_v3.py          # 期望 PASS 173 / FAIL 0；golden 160 (45+/115-)
"$PY" scripts/scan_tracked_secrets.py        # 已知 KI01 误报 src/llm/factory.py:111，非本任务
```

- 台账：`docs/无人值守升级任务清单_2026-09-19.json`（唯一状态源，只由 I 写）
- 交接说明：`docs/dispatch/交接说明_2026-09-21.md`
- 分派手册：`docs/多Agent执行分派手册_2026-09-20.md`；角色卡 `docs/dispatch/角色卡.md`；任务卡 `docs/dispatch/任务卡.md`
- r4 审查报告：`docs/dispatch/reviews/U00/5ee082b/Q_w0r1_r4.md`

## 7. 任务状态速查

| ID | 状态 | 备注 |
|---|---|---|
| U00 | IN_PROGRESS | r5 修复轮；冻结后 → INTEGRATED（VERIFIED 留待 U12） |
| U01/A · U04/B · U03/CD · U15/EF | PLANNED | W1，依赖 U00 冻结 |
| 其余 U02/U05/U06a/U08/U13/U14/U10a | PLANNED | W2 |
| U09/U12 | PLANNED | W3–W4（I 主会话） |

**结构性不可在 8 小时内完成（需向用户如实报告，不得标记为通过）**

- W4 发布门禁为 **7 天挂钟观察窗口**（手册 §3 明示"无法通过并行加速"）
- **D01–D05 外部待决项**阻断 DEPLOY_DEMO / OPS / RELEASE（D01 宿主托管、D03 账户风险覆盖需用户决策）
- 实盘启用需用户**另行明确授权**

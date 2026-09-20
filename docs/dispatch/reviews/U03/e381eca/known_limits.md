# U03 提交包 known_limits（head `e381eca`，基线 `ffdab44`，分支 `task/U03`）

作者会话 C 修复轮 r3，回应 Q 在 `5ffc6c0` 的 CHANGES_REQUESTED。
本文件落在工作区 `mat-wt/CD`，请 I 归档进提交包
`docs/dispatch/reviews/U03/e381eca/`（作者不得直接写 I 的目录）。

## 范围与产物

- 只改独占路径：`src/agents/research_evidence.py`、
  `tests/integration/test_research_evidence.py`、
  `tests/integration/test_market_evidence_quality.py`、
  `tests/fixtures/evidence/u03_orchestrator_integration.patch`。
- 未改任何共享源文件（`src/agents/orchestrator.py`、`src/data/market_query.py`、
  `src/agents/models.py` 一律只以补丁形式交付）。未改 `src/core/contracts/`、
  `docs/contracts/`、`scripts/run_offline_regression.py`。
- `live_side_effects=none`：不联网、不启停服务、不构造 Broker、不动风险档/live 开关。

## 已知限制与残留风险（均带位置与启用方式）

1. **分源门禁的缺省口径仍是"最低下限"，不是策略口径**（Q-NEW-3 残留）。
   接线缺省 `RESEARCH_EVIDENCE_REQUIRED_SOURCES = ("ticker",)`（补丁
   `u03_orchestrator_integration.patch` 内），即每笔开仓必须有可核对且字段齐全的
   实时报价源。**资金费/OI 不在缺省集合内**：依赖资金费的策略仍需显式收紧，否则
   "声明依赖资金费却拿不到数据"这一原始形态在**未声明**时不会自动拒绝。
   启用方式：在该常量加入 `"mark_funding.funding_rate"` / `"open_interest"`。
   逐意图声明（`TradeIntent.required_sources`）**未提供**——`TradeIntent`/
   `DecisionEvidence` 属共享文件（`src/agents/models.py`），本任务未附该字段补丁；
   若需要，请 I 另开任务或由作者出补丁（会改契约 schema，需走 contract_version）。
2. **本模块不验证身份真实性**（Q-NEW-1 的边界）。`is_unpinned_identity()` 只保证
   "未固定身份不得被当成已固定"：判据是字段全等 `UNPINNED_IDENTITY` 或
   `environment == "unpinned"`。真实身份的内容仍需 U15/DL01 从运行环境注入
   （`identity=None` 是当前接线缺省，OPEN 一律 `IDENTITY_UNPINNED` 丢单）。
   刻意副作用：`environment="unpinned"` 的任何身份都被判为未固定。
3. **`binding_id` 的轮次分量取自 `run_id`，不是 `pipeline_decision_id`**（Q-NEW-2 残留）。
   `decision.pipeline_decision_id` 由 `core.decision_pipeline.sign_execution` 在
   RiskGuard **之后**写入，门禁处恒为 `None`。当前取 `service.py:777` 每轮
   `uuid4()` 的 `run_id`；**同一 run_id 被两轮复用时会重新出现绑定碰撞**（离线可复现，
   见 `_scratch_author_r3/probe_r3_out.txt` S3）。
4. **接线处"周期自证"是同义反复**（Q-NEW-6，未修）。补丁传
   `interval=result["interval"]`，与模块内交叉核对同一值，故该检查在接线处永不触发；
   防"15m 冒充 4h"实际由 `_locate_snapshot` 的 `(symbol, interval)` 精确匹配承担。
   若工具出参**自身**把 15m 标成 4h，本模块无从发现——离线无法验证，需真实交易所出参。
5. **U15 接线前系统只能退出、不能开仓，且每轮写一条 `severity="critical"` 事件**
   （Q-NEW-8，方向 fail-closed）。`identity=None` + `ALLOW_UNPINNED_RESEARCH_EVIDENCE=False`
   ⇒ 每个 OPEN 都被丢；补丁对"有 blocked"的轮次写 `research_evidence_blocked`。
   运营上表现为"开仓整体下线 + 每轮 critical"，**不得当故障处理**。
6. **`evidence.data_observed_at_ms` 必须与快照 `observed_at_ms` 精确相等**（Q-NEW-9）。
   差 1ms/秒级截断即 `EVIDENCE_TIME_MISMATCH` 丢整轮。fail-closed，未加容差
   （放宽属策略变更）；模型抄错该时间戳即丢单，需在 U15 接线时同步告知策略侧。
7. **`scripts/smoke_u03_*.py` 的冒烟登记不在本任务**（Q-NEW-7，属 I）。
   两个 U03 冒烟脚本、`s01_market_query_adaptation.patch` 与
   `u03_offline_regression_registration.patch` 均已交付且 `git apply --check` EXIT=0；
   在 I 登记之前，共享离线回归入口 `scripts/run_offline_regression.py` 仍会在
   `_validate_smoke_manifest()` 抛 `RuntimeError`（作者未改该文件）。
8. **接线补丁未应用、未跑端到端**。仅 `git apply --check`（EXIT=0）+ 内存副本静态/单元
   验证；运行期与风险层、`filter_unauthorized_opens`、下游 decision 呈现的交互需 LLM/联网。
   补丁 `index` 行的 **post-image 短哈希**由作者的可复现重建脚本
   （`_scratch_author_r3/rebuild_patch.py`，按 CR 保留口径重放纯插入）复算为 `890c08f`；
   原 r2 值 `4c340da` 无法用任一常规口径复现，`git apply --check` 不校验该值。
9. **行为增量（下游需知）**：①默认缺省非空后，报价源字段不齐的开仓意图会被拒
   （此前放行）；②`required_sources` 由"自报覆盖缺省"改为**并集**（自报只能加严）；
   ③畸形 `required_sources`（非序列/空条目/尾点）由静默忽略改为结构化拒绝；
   ④`cycle_id` 非空使 `evidence_id`/`binding_id` 的取值与 r2 不同
   （已落库的 r2 绑定 id 不可与 r3 直接比对）。

## 回滚

- 代码/测试/夹具：`git revert e381eca`（分支 `task/U03`）。本提交未触碰共享源文件，
  无共享侧残留；三个补丁均为提案，未应用前回滚无副作用。
- 若只想回退分源口径而不回退身份修复：把接线补丁里的
  `RESEARCH_EVIDENCE_REQUIRED_SOURCES` 改回 `()`（**不推荐**：即恢复 D-P1-2 缺口）。

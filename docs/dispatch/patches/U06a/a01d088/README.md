# U06a 共享适配补丁 · r2（前像 = 集成 HEAD `59660b2`，已复验于 `I@43210d8`）

> 目录名 = 生成这组补丁时 `task/U06a` 的 head 提交 **`a01d088`**（= 本轮修复提交）。
> **前像 = 集成 HEAD `59660b2`**（分支已按约定把 `src/intel/service.py` 还原为集成
> 版本，共享改动只存在于本目录的 `.diff` 中；`decisions.html` 从未随分支提交）。
>
> **与当前集成 HEAD 的一致性**：生成后实测 `git -C I diff --quiet 59660b2 43210d8 -- <目标文件>`
> 对 3 个目标文件全部为 **SAME**（逐字节相同）→ 本组补丁的 `index` 行对
> `I@43210d8` 同样有效，应用前像既可以是 `59660b2` 也可以是当前 `I` HEAD。
> 另在 `git archive 43210d8` 的沙箱里逐一 `--check` + 实应用成功（见 §3）。

## 1. 本轮（r2）改了什么

对照独立审查 `docs/dispatch/reviews/U06a/ebbe1e9/C_r1.md`：

| 条目 | 位置 | 处置 |
|---|---|---|
| **P2-1** 聚合失败仍报 `REFRESHED`/`age≈0`（陈旧当新值） | `src/intel/service.py` `_refresh_metrics_summary` | **已修**（见 §4） |
| **P2-2** `release_gate.note` 只存 JSON 未渲染，页面误示"门槛已干净" | `decisions.html` | **已修**（见 §5） |
| **P2-6** `LEGACY_LIMITATIONS` 遗漏"B/C 仍进 200 候选" | `src/intel/shadow_status.py`（独占） | **已修**（随分支提交，模板渲染该清单） |
| **P3-9** `shadow_metrics.state`/`age_seconds` 未渲染 | `decisions.html` | **已修**（新增「汇总指标缓存」卡片） |
| **P3-8** 未计算被 `or 0` 渲染成「24h 事件 0」 | `decisions.html` | **已修**（改渲染「—（未计算）」） |
| **P3-10** `_sample_loss` 未到期时是陈旧值且无 state | `src/intel/service.py` | **已修**（新增 `sample_loss_state`，同轮刷新+如实标注） |
| **P3-11** `gate_constants` 是硬编码副本，未声明来源 | `src/intel/shadow_status.py`（独占） | **已修**（新增 `gate_constants_source` 声明） |
| P3-7 `_empty_snapshot` 动态读模块常量 | `src/intel/service.py` | **如实登记**（见 §7） |
| P2-5 改动非集成目标的 `smoke_intel_sidecar.py` | 本目录 | **保留偏差声明**（见 §6） |

## 2. 补丁清单

| 文件 | 目标 | 内容 |
|---|---|---|
| `src_intel_service.py.diff` | `src/intel/service.py` | 影子披露接线 + C 未配置隔离 + 有界研究负载 + 汇总指标缓存（**含 r2 P2-1/P3-10 修复**） |
| `src_dashboard_templates_decisions.html.diff` | `src/dashboard/templates/decisions.html` | 旧口径展示 + A/B/C 隔离 + **发布门槛口径（渲染 note 全文 + 残留缺口）** + **汇总指标缓存状态/年龄** + 未计算不再显示为 0 |
| `scripts_smoke_intel_sidecar.py.diff` | `scripts/smoke_intel_sidecar.py` | **必要**：既有断言的是旧口径（C 组 `signal='unknown'` 计 2 行），与必达②直接冲突。除该两处外 17 条断言逐字未改（19 通过 / 0 失败） |

`src/core/service.py`、`src/intel/shadow_status.py`、`tests/**`、`scripts/smoke_u06a_*` 均**不在**本组补丁中
（前两个由既有实现/独占文件承载，后两个随任务分支提交）。

## 3. 可应用性（原始输出）

前像 = `59660b2`（cfgA = `-c core.autocrlf=false -c core.eol=lf`；cfgB = 默认配置）：

```text
--- src_intel_service.py.diff ---                 cfgA : exit=0    cfgB : exit=0
--- src_dashboard_templates_decisions.html.diff --- cfgA : exit=0    cfgB : exit=0
--- scripts_smoke_intel_sidecar.py.diff ---        cfgA : exit=0    cfgB : exit=0
```

额外在 `git archive 43210d8`（= 当前集成 HEAD）的**独立 repo 根**里实应用并核对
`git diff --stat`（避免"子目录 apply 静默 no-op"的假绿）：

```text
cfg A (autocrlf=false,eol=lf) : exit=0  ×3
 scripts/smoke_intel_sidecar.py         |   9 +-
 src/dashboard/templates/decisions.html |  57 ++++++-
 src/intel/service.py                   | 281 ++++++++++++++++++++++++++++++---
 3 files changed, 316 insertions(+), 31 deletions(-)
```

> `-c core.autocrlf=true` 下 `--check` 同样 exit=0，但 `git apply` 会把工作树换成 CRLF
> （三重实测 CR=813/489/127）。集成工作区 `mat-wt/I` 本地配置 `core.autocrlf=false`
> → cfg A 是实际路径；两种配置下都不产生真冲突。
> 三个 `.diff` 正文 `CR=0`（纯 LF），不依赖本机行尾配置。

## 4. P2-1 修复前后对照（原始输出，探针见分支 `_scratch_r2/probe_metrics.py`）

构造：真实 `ReadOnlyIntelService` + 真实 `AuditDatabase`；首次成功聚合后，把
`db.intel_observability` 换成抛 `RuntimeError` 的桩，再以 `allow_metrics=True` 刷新。

**修复前**（`ebbe1e9` + r1 补丁）：

```text
m1 首次(成功)   : state='REFRESHED' cached=False age_seconds=0.0 computed_at='...:34.120+00:00'
m2 聚合失败后   : state='REFRESHED' cached=False age_seconds=0.0 computed_at='...:34.120+00:00'
    cache_digest=6c658ebf8e78 仍是旧值=True errors=['metrics:RuntimeError']
m3 失败后未到期 : state='CACHED'    cached=True  age_seconds=1.101 computed_at='...:34.120+00:00'
```

→ 失败后仍报 `REFRESHED` + `age=0.0`，而缓存摘要 `6c658ebf8e78` 与 `computed_at` **未变**
（仍是旧值）；m3 的 1.101s 是从**失败的尝试**起算，真实年龄是 2.202s（被低估一半）。
**这就是"假新鲜"，与 R13 假健康同类。**

**修复后**（`a01d088` + 本目录补丁）：

```text
m1 首次(成功)   : state='REFRESHED' cached=False age_seconds=0.0 stale=False last_error=None sample_loss_state='REFRESHED'
m2 聚合失败后   : state='FAILED'    cached=True  age_seconds=1.101 stale=True last_error='RuntimeError'
    cache_digest=6c658ebf8e78 仍是旧值=True errors=['metrics:RuntimeError']
m3 失败后未到期 : state='CACHED'    cached=True  age_seconds=2.202 stale=False
```

→ 失败即 `FAILED` + `stale=True` + `cached=True`（确实在用旧缓存），`age` 以**上次成功**
为锚（1.101s，不再被失败重置为 0），`computed_at` 不动、`last_error` 如实登记。
m3 的年龄回到真实值 2.202s。

行为口径：`state ∈ {REFRESHED, CACHED, FAILED, UNAVAILABLE}`；只有**成功**的聚合更新
缓存/年龄/`computed_at`；失败不冻结重试（年龄继续增长 → 下一轮到期重试，与旧实现每轮
重算的负载同级）。

## 5. P2-2 修复前后对照（原始输出，探针见分支 `_scratch_r2/probe_render.py`）

构造：真实 `jinja2` 渲染真实 `decisions.html`，上下文 = 真实
`ReadOnlyIntelService.maybe_refresh(force=True)` 的整轮快照（网络层离线桩）。
判定只看**页面可见区**（截断到「查看 Web3/合约市场情报原始输出」，因为原始 JSON 折叠块
会把整份快照 `tojson` 出来，直接对整页判子串会得到**假 HIT** —— 这正是 r1 里
`metrics state` 被误判为已渲染的原因）。

**修复前**：`页面可见区含『研究样本可作执行候选 否』= HIT`，而
`含『尚未按 source 过滤』= MISS`、`含 release_gate.note 全文 = MISS`、
`含『汇总指标缓存』= MISS`、`含 metrics state = MISS`。

**修复后**：7 项全部 `HIT`：

```text
HIT 页面可见区含『研究样本可作执行候选 否』
HIT 页面可见区含『尚未按 source 过滤』(P2-2 关键 caveat)
HIT 页面可见区含『不建立第二套』
HIT 页面可见区含 release_gate.note 全文（与 JSON 同源）
HIT 页面可见区含 metrics state 值
HIT 页面可见区含『汇总指标缓存』陈旧提示
HIT 页面可见区含『旧口径已知缺陷』
HIT 原始 JSON 折叠块仍保留（未被裁剪）
```

渲染方式：新增「发布门槛口径 · 残留缺口」卡片，`{{ sgr.note }}` **整段引用披露字段**
（不是复制文本，改 JSON 就改页面），并以 `residual_gap.summary/impact/authority` 给出
一眼可读的结论；「影子口径」卡片同时加了"注意：B/C 研究影子行**目前仍会计入**发布门禁的
方向候选分母"。样本表下方原来的"不计入发布门槛"也改成如实表述。

## 6. 偏差声明（P2-5，与 r1 相同，未变）

`scripts/smoke_intel_sidecar.py` **不在**任务卡「集成目标」三个文件之列。做成补丁是因为
它是**既有共享门禁**的一部分，其断言与必达②直接冲突；不改则集成树上必红（实测量到
`AssertionError: B/C影子组同时记录`）。17 条其余断言逐字未改，未削弱覆盖。**请 I 确认。**

## 7. 未修但如实登记

- **P1-2（必达⑤未代码化）**：`src/core/release_evidence.py:338-346` 的候选查询只按
  `release_id/environment/signal` 过滤、**未按 source 过滤**，B/C 研究影子行确实会进
  「200 方向候选」分母（C 实测 200→300）。该文件**不在** U06a 独占路径与集成目标中，
  **本批不改**。→ 归属 **U09 硬前置**。本批的职责是**披露准确**：`LEGACY_LIMITATIONS`
  新增该条、页面渲染该条、`release_gate.residual_gap` 提供机器可读钩子
  （`id=release_gate_counts_research_rows, fixed_here=false, authority=U09`）。
  **未建第二套 GO 判断器**（全仓唯一 GO 计算仍在 `release_evidence`）。
- **P1-1（清单登记）**：`ebbe1e9`/`a01d088` 分支自身会让主门禁 raise
  `RuntimeError: 未登记冒烟脚本: scripts/smoke_u06a_shadow_isolation.py`
  （已实测复现）。按派发要求**不改** `scripts/run_offline_regression.py`，登记为
  **I 的原子登记项**（`W1-MANIFEST-TRANSIENT`）：新冒烟脚本与登记行必须同批落地。
  集成 HEAD 现为 `43210d8`（`listed=80`），该 raise 在集成树上同样成立。
- **P3-7**：`_empty_snapshot` 的 `execution_weight` 读模块常量 `shadow_status.EXECUTION_WEIGHT`。
  配置面/环境变量均无法抬升（r1 已穷举）；唯一抬升路径是**改源码常量**，属既有威胁模型
  （pristine 侧是字面量 `0.0`）。**登记不修**：改成字面量只是换一处同源常量，不改变威胁模型。
- **P3-（旧）`_cursor` 并发无锁自增**：既有问题，本批未引入未修复。

## 8. 打补丁沙箱的实测（原始输出）

前像 = 当前集成 HEAD `43210d8` 的独立 repo 根 + 本任务独占文件 + 清单登记行：

```text
tests/integration/test_shadow_status.py   exit=0  PASS 264 / FAIL 0   （未打补丁：PASS 205 / 0）
scripts/smoke_u06a_shadow_isolation.py    exit=0  PASS 52  / FAIL 0   （未打补丁：PASS 10  / 0）
scripts/smoke_contracts_v3.py             exit=0  PASS 173 / FAIL 0
scripts/smoke_intel_sidecar.py            exit=0  19 通过 / 0 失败
scripts/run_offline_regression.py         exit=0  OFFLINE REGRESSION OK scripts=81
scripts/check_quality.py                  exit=0  QUALITY CHECK OK files=283 untracked=0
```

（`scripts=81` = 集成 HEAD 的 80 条 + 本任务新增冒烟；在分支树上同口径为
`OFFLINE REGRESSION OK scripts=80`、`QUALITY CHECK OK files=264 untracked=0`。）

**全量 delta（逐条非 fail-fast，用 `^\s*FAIL\s+` 抽失败名 + 记录退出码）**：

```text
i_pristine（I@43210d8 + 独占文件 + 登记行，不打补丁）: TOTAL=81 BAD=0
ihead     （同上 + 本组 3 个补丁）                    : TOTAL=81 BAD=0
失败集合差集 = ∅
```

> 说明：`smoke_u02_strategy_position_manager.py` 会打印 10 行 `FAIL …`，那是它的
> **负向对照**（N1/N2 必须失败）且 exit=0、末行 `U02 SMOKE OK` → **不计为失败**，
> 两态一致。基线（`I@43210d8`）本身全绿，故补丁**未新增任何失败**。

## 9. 已知限制

- `FAILED` 态下仍向页面提供**旧**汇总值（这是有意的：丢掉旧值会更糟），但页面与
  `shadow_metrics.stale=True` 都明确说明"这是旧值"。真机 7×24 的失败持续时长未实测。
- `gate_constants` 是**字面量副本**：U09 改阈值后，测试会红（好），但运行时披露会
  先说出过期的数字。已用 `gate_constants_source` 如实声明，属披露时效限制。
- 修复的是**披露诚实性**，不改任何门禁判定：B/C 仍会进 200 候选分母（P1-2，属 U09）。
- `decisions.html` 改动为纯展示层：不改门禁、不改执行权重、不新增数据源；字段缺失时回落既有展示。

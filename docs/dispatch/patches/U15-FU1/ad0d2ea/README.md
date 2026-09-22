# U15-FU1 补丁包（目录名 `ad0d2ea`）

> 目录名 = **打包时（`.diff` 生成时）的分支 head** `ad0d2ea`。`.diff` 内容 =
> `02b93da..ad0d2ea`（base `02b93da` = 本轮起点，已确认干净）。本目录自身在本
> head 之后落库，不影响 `.diff`。
>
> 关闭的登记项：`U12-INTEGRATION-RUNTIME-INSTANCE-PLACEHOLDER`
> （RESUME §「新登记（U12 收口项）」原话：「`runtime_instance_id` 实测为字面量
> `'unknown'`（占位符）却因"非空"被判为完整身份，**方向 fail-open**，建议明确
> `'unknown'` 应视为"运行代缺失"」）。本包即按该建议逐字收口。

| 文件 | 交付方式 | 说明 |
|---|---|---|
| `src/core/service.py` | `01_core_service_runtime_instance_passthrough.diff` | **修复 A**：把服务**已经在手**的持久化运行代传给 `build_release_manifest`；降级路径传 `None`，不传临时值/字面量。 |
| `src/core/release_manifest.py` | `02_core_release_manifest_placeholder_normalisation.diff` | **修复 B（消费侧）**：运行代先归一化（占位符→缺失），清单字段两道闸都不漏哨兵。 |
| `src/core/runtime_identity.py` | `03_core_runtime_identity_placeholder_recognition.diff` | **修复 B（事实源）**：占位符识别/归一化 API；`collect_runtime_identity` 把"占位符"与"调用方明确没有（`None`）"按**运行代未证明**处理并记入既有阻断码。 |
| `tests/acceptance/test_unattended_release.py` | `05_acceptance_unattended_release_u12.diff` | U12 [8] 跟随新口径（下文逐条自证）。 |
| `tests/acceptance/test_runtime_identity.py` | `04_acceptance_runtime_identity.diff` | U15 新增两节：占位符口径 + **真实服务链路**（正常态/降级态）两态断言。 |

---

## 1. 缺陷（活路径，fail-open）

服务启动时（`src/core/service.py`）：

1. `:165-186` 用 `RunRegistry` 铸造/载入运行代 → `self.runtime_instance_id`
   **真实值已在手**（正常态）；
2. `:188-195`（修前）调 `build_release_manifest(...)` **没有传**这个值；
3. `release_manifest.py:297` 回退 `os.getenv("MTA_RUNTIME_INSTANCE_ID")` → 未设时 `None`；
4. `release_manifest.py:232`（修前）`str(runtime_instance_id or "unknown")` ——
   **静默用占位符**送去采集；
5. `runtime_identity.py:1684`（修前）只拒绝**空**值；
   `"unknown"` 是非空字符串 → 被当成合法运行代 → `bundle.blockers` **没有**任何条目；
6. 结果：`build_release_manifest(...).to_dict()["runtime_instance_id"] == "unknown"`
   且 `identity_blockers == []`。消费方读到的是"有身份"：

   - U12 验收 `complete = bool(artifact_hash) and bool(runtime_instance_id)` → 真；
   - U09 发布证据 `_identity_block` 把 `runtime_instance_id` 当 identity_ref →
     实测 `state=PASS`、`reason_codes=[]`（见 `evidence/probe_ev_base.log`）。

服务在**降级路径**（运行代未能持久化，`service.py:182-186` 置 `run_generation=None`
并记 `run_generation_error`）下同样写 `"unknown"` —— 恰恰是最需要被看出"没有运行代"
的那条路径，反而静默。

**同族**：`U09-FU1`（一个**读起来"存在"、实际不存在**的值）。

## 2. 修复 A（生产链路，必须）

```python
        self.release_manifest_obj = build_release_manifest(
            ...
            release_id=os.getenv("MTA_RELEASE_ID") or None,
+           # U15-FU1：把**已经在手**的持久化运行代传给清单。
+           runtime_instance_id=(
+               self.run_generation.runtime_instance_id
+               if self.run_generation is not None else None
+           ),
        )
```

**为什么降级路径传 `None` 而不是 `self.runtime_instance_id`**：降级态下
`self.runtime_instance_id` 是一个**未持久化的 uuid4**（只用于进程内 session/决策
身份），它不构成"运行代"——传它等于用一个临时值冒充身份，与本轮要修的缺陷同形。
传 `None` → 走修复 B 的显式缺失口径。**服务照常启动**（不新增任何启动期拒绝）。

## 3. 修复 B（口径，必须）

**选择：显式识别为"运行代缺失"并记入既有阻断码**（而不是拒绝/抛错）。

| 输入 | 修前 | 修后 |
|---|---|---|
| 真实运行代 | 正常身份 | 正常身份（不变） |
| `"unknown"` / `"n/a"` / `"UNKNOWN "` / …（占位符） | **当成合法运行代**，无阻断码 | 身份其余部分照常采集；`blockers += RUNTIME_GENERATION_UNPROVEN`；身份里的运行代字段**留空** |
| `None`（调用方明确"没有"） | 非字符串 → `MissingRuntimeInputError` → 整个清单退化为 `IDENTITY_COLLECT_FAILED` | 同"占位符"口径（未证明 + 留空 + 其余证据照常） |
| `""` / `"   "` / 非字符串 | `MissingRuntimeInputError` | `MissingRuntimeInputError`（**逐字不变，未放宽**） |

- **复用既有码** `RUNTIME_GENERATION_UNPROVEN`（`runtime_identity.py:196`，U15 r2 既有；
  K04 绑定门禁同码，语义即"调用方未提供运行代 → 无法证明绑定"）。**未新造**任何
  语义重叠的原因码。
- **不抛错**的理由：抛错会让
  `release_manifest._identity_evidence` 的兜底分支生效，**整份证据**（
  `artifact_hash` / `effective_config_hash` / `model_route_hash` /
  `dependency_comparison_status` / `git_dirty`）全部清空并改报
  `IDENTITY_COLLECT_FAILED` —— 把"缺一项"谎报成"采集失败"，反而看不清缺的是什么，
  也削弱了清单的可审性。因此：**识别为缺失 + 记码**，而不是把整份证据一起降级。
- **清单字段留空**（两道闸）：`collect_runtime_identity` 侧留空 +
  `build_release_manifest` 侧对采集结果**再归一化一次**（`_resolved_runtime_instance_id`，
  模块不可用时保守返回空串）。于是哨兵**永不**落进值，`bool(runtime_instance_id)`
  这类判据如实读成"无"。
- 哨兵名单（大小写/空白无关）：`unknown / none / null / nil / n/a / na / undefined /
  unset / missing / not-set / notset / - / -- / ? / placeholder / tbd`。
- 附带收紧（方向一致，可复核）：真实值现在会被 `strip()`（修前 `" rt-real "` 会带着
  空白进清单）。

**下游（U09 发布证据门禁）的读数变化**（`evidence/probe_ev_*.log`）：

| 场景 | 修前 | 修后 |
|---|---|---|
| 无运行代 / 占位符 | `state=PASS`、`reason_codes=[]`、`runtime_instance_id='unknown'` | `state=NO_DATA`、`reason_codes=['IDENTITY_REF_MISSING']`、`runtime_instance_id=None` |
| 真实运行代 | `state=PASS` | `state=PASS`（不变） |

即：占位符清单从"被祝福为通过"变成"显式 NO_DATA"。这是**收紧**，不是放宽；
`7 天 / 200 方向候选 / 30 完整闭合`（`DEFAULT_THRESHOLDS`）**一个数字未动**。

## 4. 测试改动自证（U12 [8] 逐条）

`tests/acceptance/test_unattended_release.py::test_identity_is_never_silently_empty`
（唯一被改的 U12 断言区）。逐条说明**为什么是跟随新口径而非为变绿而放宽**：

| # | 修前 | 修后 | 自证 |
|---|---|---|---|
| 1 | `complete = bool(artifact_hash) and bool(runtime_instance_id)` | `collected = bool(artifact_hash) and "IDENTITY_COLLECT_FAILED" not in blockers` | 把**两个不同的事实**（证据已采集 / 运行代已证明）拆开。修前的第二个合取项在无运行代时取的是**哨兵**（恒真），正是缺陷本身；拆开后"运行代"由下面第 4/5 条**单独且强制**断言。**强度是升不是降**：修前 3 条断言、修后 6 条。 |
| 2 | `check("身份完整（artifact_hash 与 runtime_instance_id 均非空）", True)` | `check("身份证据已采集（artifact_hash 非空且未报采集失败）", True)` | 仅同步 #1 的命名（`complete`→`collected`），判定条件**未放宽**（仍是"artifact_hash 非空"）。 |
| 3 | `check("身份采集结果与工件扫描状态一致", scan_ok == complete …)` | 同名，改用 `collected` | 一致性不变量的对象本就是"采集"，不是"运行代"；条件形状逐字不变。 |
| 4 | （无） | `instance == ""`（guard 态） | **新增**：不允许哨兵留在清单字段里。修前 `instance == "unknown"`，此断言在旧实现上必然失败。 |
| 5 | （无） | `RUNTIME_GENERATION_UNPROVEN in blockers`（guard 态） | **新增**：必须显式登记缺失，而不是"无阻断码"。 |
| 6 | （无） | 基线态分支：断言**缺陷形态**（`instance == "unknown"` 且无该码） | 两态可跑所必需：本套件要在**未打补丁的共享文件**上也能跑。此断言是**缺陷复现**，不是放宽——修后态走 guard 分支，此分支若被执行即失败（`hybrid_u12.log` 实测走基线分支 PASS，`u12_patched2.log` 实测走 guard 分支 PASS）。 |
| 7 | （无） | 反向证据：传 `runtime_instance_id="rt-u12-reverse"` → 清单透传该值**且**不带 `RUNTIME_GENERATION_UNPROVEN`（**两态都成立**，无分支） | 证明本次改动**没有**把正常路径也标成阻断。此断言在修前/修后都必须成立 → 不属于"为变绿而放宽"。 |

U12 套件总数：`PASS=47 FAIL=0`（修前，pristine）→ `PASS=50 FAIL=0`（修后）。
**没有任何一条旧断言被删除**（只有 1 条重命名 + 条件等价改写），新增 3 条。

`tests/acceptance/test_runtime_identity.py` 新增两节（**未改任何既有断言**）：

- `check_placeholder_runtime_instance_is_not_an_identity`：识别器/归一化；占位符 →
  `RUNTIME_GENERATION_UNPROVEN` + 字段留空 + **其余证据仍在**；`None` 同口径；
  **空串仍 `MissingRuntimeInputError`（未放宽，两态都断言）**；真实运行代不带该码
  （两态都断言）。
- `check_service_manifest_registers_missing_run_generation`：在**隔离临时状态根**
  （`MTA_STATE_ROOT`=临时目录、`EXECUTION_MODE=paper`）启动**真实** `TradingService`，
  正常态断言"清单值 == 服务手上的真实值"、降级态（`core.service.RunRegistry` 抛错）
  断言"清单留空 + 该阻断码"，两态都断言"服务仍能启动"。
- 因此该文件 docstring 的"不启动服务"边界随本轮更新为"除 U15-FU1 一节外不启动服务
  （该节 paper + 隔离临时状态根，不联网、不下单）"。
- 两态探测一律用 `getattr(module, "U15_FU1_RUNTIME_INSTANCE_GUARD", "")`，**不猜**
  共享文件是否已打补丁；未引入对基线不存在的符号的硬导入（`RUNTIME_GENERATION_UNPROVEN`
  在基线即存在）。

## 5. 前后对照（实测）

`evidence/probe_service_base.log` vs `evidence/probe_service_patch.log`
（同一脚本、同一机、同一 venv；命令见 `evidence/commands_and_exit_codes.txt`）：

```
== service: normal (RunRegistry 可用) ==
---- 修前：service.runtime_instance_id = '4fa9f71e…'  manifest.runtime_instance_id = 'unknown'  blockers = []
---- 修后：service.runtime_instance_id = '49ebeeab…'  manifest.runtime_instance_id = '49ebeeab…'  blockers = []
== service: degraded (RunRegistry 抛错) ==
---- 修前：run_generation = None  manifest.runtime_instance_id = 'unknown'  blockers = []
---- 修后：run_generation = None  manifest.runtime_instance_id = ''         blockers = ['RUNTIME_GENERATION_UNPROVEN']
== build_release_manifest API ==
---- 修前：(arg=None)    -> 'unknown'  blockers=['MODEL_ROUTE_MOCK']
---- 修后：(arg=None)    -> ''         blockers=['MODEL_ROUTE_MOCK','RUNTIME_GENERATION_UNPROVEN']
---- 修前/修后：(arg='rt-probe-1') -> 'rt-probe-1'  且**不新增**该阻断码（反向证据）
```

## 6. 两态 / 回归 / 质量

见 `evidence/full_delta_summary.txt`、`evidence/reg_*.tsv`、`evidence/regression_*_entry.log`、
`evidence/apply_check_3configs.log`、`evidence/check_quality_patched.log`。

## 7. 未尽事项（如实登记）

1. **`mint_runtime_generation` 未动**：它仍只拒绝空值，因此一份**损坏/遗留**的
   `runtime_generation.json`（其 `runtime_instance_id` 为 `"unknown"`）仍能铸造出一代
   实例号叫 `"unknown"` 的运行代。**但**该值随后必然进入发布清单并被本轮口径标成
   `RUNTIME_GENERATION_UNPROVEN`（端到端可见，不再是静默放行）。不改 mint 的理由：
   改它会把"可见但可用"的降级态变成"运行代完全不可用"的硬失败，收益低而行为面大。
   建议另立一条：`RunRegistry.load()` 把占位实例号视为**无记录**（重新铸造）而非沿用。
2. **`management_authorization.py:177`** 对 `RuntimeGeneration.runtime_instance_id`
   仍是"非空即通过" —— 同样的哨兵若以别的路径进入 K04 授权，仍不会被该处拦下。
   属同一族但不同消费者，本轮未扩面。
3. **`egress_id` 的 `"unknown"` 未动**：`service.py:1339` 已在 live 启动门禁把它当缺失
   处理（既有正确行为），但 `release_manifest` 的 `egress_id` 字段本身仍可写
   `"unknown"`。属已登记的相邻项，未顺手修。
4. 本轮的阻断码是**记录**不是**门禁**：服务不会因为 `RUNTIME_GENERATION_UNPROVEN`
   拒绝启动（符合要求）。它在闭环中出现的位置是：清单字段、`identity_blockers`、
   看板 `/api/mode` 拒绝切 live、U09 证据 `IDENTITY_REF_MISSING`。

---

## 附录（I 在集成时追加，不改原文）

**更正 §4 的一处不实陈述（Q r1 复验 Note ① 实测）**：§4 称本补丁已更新
`tests/acceptance/test_runtime_identity.py` 的模块 docstring，**实际未更新**——
原文仍写"不启动服务"，而新增的 `RUNTIME_GENERATION_UNPROVEN` 两态断言确实会构造真实的
`TradingService`。Q 已实测该构造是安全的（**paper 模式 + 隔离的临时状态根**，不触真实
`data/`、不联网、不下单、不启停既有服务），故无行为风险，但**文档与代码不符属实**。

**处置**：集成时已由 I 更正该 docstring（如实写明"例外"及其隔离条件），未改动任何断言。
此处保留原文并追加说明，以便回溯作者当时的声称。

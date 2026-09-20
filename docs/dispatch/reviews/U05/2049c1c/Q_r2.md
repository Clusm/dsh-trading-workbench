# Q-U05 r2 独立复验报告（角色 Q / 修复轮 r2 / 第 2 轮）

- 复验对象：`task/U05` head `2049c1c`（base `0e522b1`；`8a8e31e` / `f9a46bc` / `2049c1c`）
- 交付面：**补丁包** `docs/dispatch/patches/U05/8a8e31e/`（15 条，`10_*` 已撤回）
  - 位置说明：该目录实际位于 **`mat-wt/CD/docs/dispatch/patches/U05/8a8e31e/`**（`mat-wt/I/docs/dispatch/patches/` 下**没有** U05 目录，只有 U01/U13/U15）——见「新发现 N4」。
- 复验者：Q（独立会话，与作者不同会话），工作区 `reviews/U05/R2/r1`（detached `2049c1c`，只读）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`（3.13.12，`PYTHONUTF8=1`），无 pytest
- 上一轮：B 初审 `04a2c30` → **CHANGES_REQUESTED**（P0=0 / P1×5），报告 `I/docs/dispatch/reviews/U05/04a2c30/B_r1.md`
- 结论：**CHANGES_REQUESTED**（唯一阻断项 = P1-5 残留「明文凭据漏报」且未如实登记；另 1 条 P2 交付风险）

原始 stdout 全部落盘于 `_scratch_Q/`（`out_*.txt`、`probe_*.py`、`delta_logs_Q/`、`result_delta_Q.json`），
下文每条结论都可用文件名回溯。所有探针均自写，**未采信作者的自测对照**。

---

## 0. 结论摘要

| 项 | 判定 | 一句话 |
|---|---|---|
| P1-1 `latency_ms` 不再被 `failed_rounds` 污染 | **CLOSED** | 独立写库 + 跑 U01 的 `AVG(latency_ms)`：读作 1234.0，旧口径读作 3.0 |
| P1-2 分类以 `decision_gate()/report.ok` 为准 | **CLOSED** | 门禁整体失败 → `RESEARCH_INCOMPLETE`+`evidence_gate_failed`，**不再**出现 `NO_EDGE`+`evidence_gate_ok` |
| P1-3 `mock_allowed` 只来自显式开关 | **CLOSED** | 缺 key 且未开开关 → `mock_allowed=False`、守卫报 `SILENT_MOCK`；假值字符串/双开关均 fail-closed |
| P1-4 span 违规不击穿保护/对账 | **CLOSED** | 期限耗尽下三步全跑、返回值齐全、`span_errors` 留痕 3 条；`with trace.span` 不再抛 |
| **P1-5 密钥扫描器收窄** | **CHANGES_REQUESTED（部分闭合）** | 任务点名的绕过面**全部**已合上，但**代码位置裸明文凭据仍漏报**（`.env` 内容进 `.py` 即复现），且作者给出的理由**不成立**、交付文档**未登记**该残留 |
| 补丁可应用性（`git apply --check`） | **8/15（新发现 N1）** | 在评审/集成工作区（`core.autocrlf=false`）**7 条失败**；只有 `core.autocrlf=true`（或先把目标文件 LF 归一）才 15/15。作者"15/15 OK"未声明该前提 |
| 全量回归 delta（统一清单） | **新增失败 = ∅**（见 §6） | 独立双树沙箱、同一清单同一 env、逐条非 fail-fast |
| 登记是否如实 | **基本如实，但不完整（N3）** | market_query 撤回 / 生产未接线 / P2-7 登记**准确**；P2-3 / P3-3 / P3-4 / P3-5 在交付 README 与 RESUME 里**找不到登记文字** |

---

## 1. P1-5（安全控制，最严重）—— **部分闭合，判 CHANGES_REQUESTED**

### 1.1 我独立验证的三件必查事项

探针：`probe_secret.py`（输出 `out_probe_secret.txt`）、`probe_secret2.py`（`out_probe_secret2.txt`）、
`probe_secret3.py`（`out_probe_secret3.txt`）；用 `importlib` 直接加载**打补丁后**的
`scripts/scan_tracked_secrets.py`，全部用例自造。假凭据 `S = "zx8k2mQ9vLp4Rt7YbN1wXc4v"`（24 字符、字母开头）。

**(a) 注释 / docstring / 内嵌 YAML·JSON 里的明文凭据重新被报出 —— 成立 ✅**

| 用例（自造） | 打补丁后 | 判定 |
|---|---|---|
| B 注释行 `# api_key = <S>` | `[(1,'secret-assignment')]` | 报 ✅ |
| C `x = 1  # type: ignore  api_key = <S>` | 报 | 报 ✅ |
| D 单行 docstring `DOC = """api_key = <S>"""` | 报 | 报 ✅ |
| E 多行字符串（凭据单独成行） | 报（命中行 2） | 报 ✅ |
| F f-string 字面段 `f"api_key = <S>"` | 报 | 报 ✅ |
| G f-string 替换域内字符串 | 报 | 报 ✅ |
| H 装饰器引号参数 `@route(api_key="<S>")` | 报 | 报 ✅ |
| I `__doc__ = "api_key = <S>"` | 报 | 报 ✅ |
| J 跨行括号内字符串 | 报 | 报 ✅ |
| K `.py` 内嵌 YAML 裸标量（**整段在字符串里**） | 报（命中行 2） | 报 ✅ |
| L Unicode 转义字符串 | 报 | 报 ✅ |
| M 多行字符串里的注释形态 | 报 | 报 ✅ |

**(b) `api_key=<裸变量>` / `cfg.<裸属性链>` 仍不误报（KI01）—— 成立，但有边界 ⚠️**

| 用例 | 打补丁后 | 审计 |
|---|---|---|
| Q `api_key = fallback_key` | 不报 ✅（`skipped_bare_identifiers:1`） | — |
| R `api_key = cfg.llm.api_key` | 不报 ✅（`skipped:1`） | — |
| KP1 `build(api_key=fallback_key, model=m)`（**作者测的形态**） | 不报 ✅ | `skipped:1` |
| KP3 `build(api_key=cfg.llm.key, model=m)` | 不报 ✅ | `skipped:1` |
| KP6 `api_key = cfg.llm.key  # from config` | 不报 ✅ | `skipped:1` |
| KP7 `api_key = cfg.llm.key; model = m` | 不报 ✅ | `skipped:1` |
| **KP2 `client = build(api_key=fallback_key)`（右括号紧贴）** | **报 ❌ 误报** | 无留痕 |
| **KP4 `client = build(api_key=cfg.llm.key)`** | **报 ❌ 误报** | 无留痕 |

根因：`_SECRET_ASSIGNMENT_RE` 的裸值组是 `[^\s,#;]+`，**不排除 `)`**，所以
`_is_python_code_reference("fallback_key)")` 因 `fullmatch` 失败而**不放宽**：
```
'fallback_key'      -> True
'fallback_key)'     -> False     <-- 就是这里
'fallback_key,'     -> False
```
**性质判定（重要）**：这不是回归 —— 旧（未打补丁）扫描器**什么都报**，所以打补丁后的命中集合
永远是`原始集合 − 放宽集合`，**不可能产生新误报**。因此这是「KI01 未修尽」而非「引入新失败」，
定 **P3**。但 README §三/§四把 KI01 说成「保持关闭」，**只在带 `,` / 空格 / 行尾终止的形态上成立**；
`patch 14` 的测试也只覆盖带逗号的形态（作者自述「`api_<下划线>key=<裸变量名>,`」）。
仓库现状不受影响（我实跑 `SECRET SCAN OK ... registered=3 skipped=20 unparsable=0`，见 §1.4）。

**(c) 无法词法分析的文件整体放弃放宽（fail-closed）—— 成立 ✅**

```
N 语法错误的 .py:
   'def f(:\n    api_key = %s\n' % S
   -> [(2,'secret-assignment')]  审计 {'unparsable_python_files': 1}
```
`_non_code_spans()` 捕获 `TokenError/SyntaxError/IndentationError/ValueError` 后返回 `None`，
`find_secret_hits` 的放宽分支要求 `spans is not None`，因此**整份文件不放宽**，并在
`UNPARSABLE_PYTHON_KEY` 计数。`main()` 把它打印出来（可审计）。

### 1.2 绕过尝试（任务书点名 7 类，逐条结论）

| 任务书点名的藏匿位置 | 结果 | 证据 |
|---|---|---|
| 跨行字符串 | **报出** ✅ | E、J、M |
| f-string | **报出** ✅ | F、G |
| 装饰器参数 | **报出**（引号形态）✅ | H |
| `# type: ignore` 后的注释 | **报出** ✅ | C |
| `.py` 内的 JSON 多行块（**在字符串里**） | **报出** ✅ | K |
| `.py` 内的 JSON 多行块（**在代码里、带引号键**） | **不报** ⚠️ **既有正则限制**（未打补丁版本同样不报，与 patch 13 无关）：要求键后紧跟 `:`/`=`，`"api_key":` 的收尾引号把匹配挡掉 | `LZ5`/`LZ6`：`.py` 视图与 `.yaml` 视图**都不报**；pristine 亦不报 |
| `.py` 内的 JSON 多行块（**在代码里、裸键**） | **不报** ❌（见 1.3 残留） | `LZ4`：`.yaml` 视图报、`.py` 视图不报 |
| `__doc__` 赋值 | **报出** ✅ | I |
| Unicode 转义 | **报出** ✅ | L |

### 1.3 **残留泄漏（未闭合的实质）** —— 代码位置的裸明文凭据

同一套探针（`probe_secret3.py` / `out_probe_secret3.txt`），**未加引号**的明文凭据：

| 用例（真实形状） | 打补丁后 `.py` 视图 | 未打补丁 | 审计留痕 |
|---|---|---|---|
| `.env` 内容整体进了 `.py`（单行）`api_key=<40字符字母数字>` | **不报 ❌** | 报 | `skipped:1` |
| `.env` 内容整体进了 `.py`（多行 key+secret） | **不报 ❌** | 报 | `skipped:2` |
| `api_key = <64 位 binance 形状 key>`（赋值 + 空格） | **不报 ❌** | 报 | `skipped:1` |
| `.py` 顶层 YAML 形态 `api_key: <64 位 key>` | **不报 ❌** | 报 | `skipped:1` |
| `.py` 内裸键 JSON 块（模块层）
```
CONFIG = {
    api_key: <S>,
}
``` | **不报 ❌** | 报 | `skipped:1` |
| **对照**：`api_key = "<64 位 key>"`（引号） | 报 ✅ | 报 | — |
| **对照**：`api_key = <含横线的 token>`（非 identifier 形状） | 报 ✅ | 报 | — |
| **对照**：`# api_key = <64 位 key>`（注释） | 报 ✅ | 报 | — |

**机理**：放宽条件 = 「捕获的裸值是 identifier/属性链形状」**且**「起始列不在注释/字符串 span 内」。
`api_key = zx8k2mQ9vLp4Rt7YbN1w` 是**合法 Python 语法**（右值是名字表达式），tokenize 判为代码，
于是被整条跳过。作者在源码注释里写的理由 ——
> 「未加引号的右值**在代码位置**只能是表达式——内联明文凭据不加引号根本过不了语法解析」

**不成立**：`api_key = <identifier>` 能过语法解析（运行期才 `NameError`）。唯一真正"过不了语法解析"的
是**含非标识符字符**的凭据（如 `sk-`、`/`、`+`），那些恰好仍然会被报出。于是留下一个**真实的形状盲区**：
首字符为字母/下划线、其余为 `[A-Za-z0-9_.]` 的明文凭据，只要**不加引号且落在代码位置**就静默漏报；
其中最现实的形态就是**把 `.env`/YAML 内容粘进了后缀为 `.py` 的文件**。

**严重度与结论**：
- 相对未打补丁版本这是**收缩**（r1 是整行放宽，r2 收窄到代码位置），且现在**有留痕**
  （`skipped_bare_identifiers`），"静默"成分已消除 —— 好于 r1。
- 但它仍是**明文凭据漏报**，而且交付 README/源码注释**没有把它登记为已知残留**，反而用一句
  不成立的推理把它说成不可能发生。按本轮标尺「任何未登记的 P2 密钥漏报 ⇒ CHANGES_REQUESTED」，
  **判 CHANGES_REQUESTED**。
- **修复成本极低（任选其一）**：
  1. 把该残留写进 README §五「未接线/已知限制」，并**改掉那句错误理由**；或
  2. 收窄判定：只有该标识符**在同一文件内**作为 NAME token 出现/被绑定过（真变量引用）才放宽，
     孤立出现的 identifier 形状裸值照报；或
  3. 在 `skipped_bare_identifiers` 之外**额外打印被跳过候选的"行号 + 规则"**（不含值），
     让放行可复核。

### 1.4 登记表「行号 + 内容指纹」双锚（P2-9）—— **成立，且不放过真凭据 ✅**

探针 `probe_fingerprint.py`（`out_probe_fingerprint.txt`）：自建 git 仓库复刻三处登记行。

先独立核对三处登记指纹与真实文件**逐字一致**：
```
docs/dispatch/reviews/U00/0b85bef/C_w0r1_r2.md:31  rule=secret-assignment  fp=deab45caecdfeaaf 一致=True
tests/acceptance/test_runtime_identity.py:524      rule=provider-token     fp=98e78280165ece7f 一致=True
tests/acceptance/test_runtime_identity.py:708      rule=provider-token     fp=fd4ba52305b076b5 一致=True
```
再构造五种情形：

| 情形 | `stale_allowances` | 未被吸收的命中 | 门禁 |
|---|---|---|---|
| 原样（行号 + 内容都对） | `()` | 0 | OK |
| **行号漂移 +3（内容逐字不变）** | `()` | 0 | **OK** ← P2-9 主诉已消除 |
| **同一行号、内容换成真凭据形状** | 3 条 `STALE_ALLOWANCE` | **2**（`sk-LIVE0QZ...` 被报出） | **FAIL** ← 不放过真凭据 |
| 该行内容被删（登记不再命中） | 3 条 | 0 | FAIL（仍 fail-closed） |
| 原行保留 + 另一行内容相同 | `()` | 0 | OK（指纹相同，符合预期） |

→ **双锚机制安全**：行号漂移被容忍（跨任务耦合解除），内容一旦变化（含换成真凭据）**两个方向同时失败**。
`_allowance_matches` 的 `by_fingerprint` 分支以「路径 + 规则 + 行内容指纹」为键，不可能覆盖到别的行内容，
所以不会"顺手放行"。

### 1.5 实跑计数（独立复现作者声称的 `skipped=20 / unparsable=0`）

在独立沙箱（`git archive 8a8e31e` + `git init` + 15 补丁）直接跑补丁后的扫描器：
```
SECRET SCAN OK files=397 registered_false_positives=3 skipped_bare_identifiers=20 unparsable_python_files=0   (exit 0)
```
未打补丁的同一棵树：
```
SECRET SCAN FAIL docs\dispatch\reviews\U00\0b85bef\C_w0r1_r2.md:31 rule=secret-assignment
SECRET SCAN FAIL src\llm\factory.py:111 rule=secret-assignment          <- KI01 原始误报
SECRET SCAN FAIL tests\acceptance\test_runtime_identity.py:524 rule=provider-token
SECRET SCAN FAIL tests\acceptance\test_runtime_identity.py:708 rule=provider-token   (exit 1)
```
`src/llm/factory.py:111` 的原误报行实为 `api_key=fallback_key,`（**带逗号**，见 `out_scan_pristine.txt` 与
`sed -n '107,114p'`），正好落在放宽判定能覆盖的形态上。**计数 397/20/0 独立复现一致**。

---

## 2. P1-1（跨任务数据污染）—— **CLOSED ✅**

探针 `probe_p1_1.py`（`out_probe_p1_1.txt`）：在 `_scratch_Q/p11_db/` 建临时库，
调补丁后的 `AuditDatabase.log_decision_trace_summary`，再**照抄 U01 `src/agents/review.py:231-237` 的 SQL**
（`GROUP BY agent_name,status` + `COALESCE(AVG(latency_ms),0)`）做聚合。

```
1) latency_ms=1234, summary.failed_rounds=3
   agent_name=decision_trace status=degraded latency_ms=1234 error=None
   output_summary.failed_rounds=3  counters.failed_rounds=3  output_summary.latency_ms=1234
2) latency_ms=None（没测到）
   latency_ms=None  status='degraded'  failed_rounds=7          <- NULL，不是 0、不是 7
3) U01 聚合：agent_name=decision_trace status=degraded n=2 AVG(latency_ms)=1234.0
4) 对照：手工按 r1 旧口径写 latency_ms=3 的行 -> AVG(latency_ms)=3.0   <- B-r1 P1-1 的污染形态
```
- `latency_ms` 只写真实耗时或 `NULL`；`AVG` 天然忽略 `NULL`，`n=2` 仍得 `1234.0`。
- `failed_rounds` 改随 `output_summary` JSON（顶层 `failed_rounds` + `counters.failed_rounds`），
  **不占用耗时列**；docstring 也删掉了"工件进独立列"的承诺（`error` 恒 `None`）。
- **U01 `review.py:231-237` 的聚合路径不再被污染**，独立复现确认。

---

## 3. P1-2（门禁失败不得归因 `NO_EDGE`）—— **CLOSED ✅**

探针 `probe_p1_2_3_4.py`（`out_probe_p1_2_3_4.txt`），8 个自造 report：

| # | 构造 | primary | details |
|---|---|---|---|
| 1 | `decision_gate()["ok"]=False`、`intents=()` | `RESEARCH_INCOMPLETE` | `['evidence_gate_failed','gate:rejected:i1:NO_EDGE']` |
| 2 | 同上 + `rejected_intent_ids=('ghost-1',)` | `RESEARCH_INCOMPLETE` | `['evidence_gate_failed']` |
| 3 | 只有**非 OPEN** 意图被拒、`report.ok=False` | `RESEARCH_INCOMPLETE` | `['evidence_gate_failed']` |
| 4 | `identity_pinned=False`、`ok=False` | `RESEARCH_INCOMPLETE` | `['evidence_gate_failed','gate:identity_unpinned']` |
| 5 | **部分失败**：一个意图带 `RR_CONTRADICTION` + 门禁 `ok=False` | `RESEARCH_INCOMPLETE` | 带 `gate:rejected:i1:RR_CONTRADICTION` |
| 6 | `report.ok=True` 且无拒绝 | `NO_EDGE` | `['evidence_gate_ok']` |
| 7 | `report.ok=True` 但一意图被数值拒绝 | `NO_EDGE` | `['evidence_gate_ok']` |
| 8 | 既无 `decision_gate()` 又无 `ok` | `NO_EDGE` | `['evidence_gate_unknown']`（不冒充通过） |

- **门禁整体失败 / 部分失败 / 身份未固定 三类都不再落 `NO_EDGE`**，也不再在 `ok=False` 时报
  `evidence_gate_ok`；`decision_gate()` 优先于 `report.ok`（`_evidence_gate_state`，`decision_trace.py:253-285`）。
- 三种必需用例（整体失败 / 部分失败 / `ok=True`）全部构造并验证。断言全过。

---

## 4. P1-3（fail-open 静默 Mock）—— **CLOSED ✅**

探针同上。`mock_allowed` 唯一来源 = 显式参数或进程开关 `MTA_ALLOW_MOCK_LLM`（缺省 False）：

```
MTA_ALLOW_MOCK_LLM=None/''/'0'/'false'/'no'/'off'/'2'/'maybe'/' '  -> mock_allowed=False   (fail-closed)
MTA_ALLOW_MOCK_LLM='TRUE'/' Yes '/'1'/'on'                          -> mock_allowed=True
RouteExpectation(model="m1") 裸构造缺省                               -> mock_allowed=False
```
- **缺 key + 未开开关 = 不再"干净"**：`mock_allowed=False`，`ModelRouteGuard` 报
  `SILENT_MOCK('mock response without explicit allowance: provider=mock')` ✅
- **开了开关 = 显式登记而非静默**：`mock_allowed=True` → 守卫 `violations=()`；
  `build_llm_for_agent` 缺 key 时 `degraded_reason='missing_api_key'`、`route_role='degraded_mock'`；
  开关打开后 `degraded_reason=None`、`route_role='mock'` ✅
- **`degraded_reason` 无条件上报**：即使 `mock_allowed=True`，
  `observe_attempt(degraded_reason='missing_api_key')` 仍报
  `SILENT_MOCK('client degraded to mock at runtime: missing_api_key')` ✅
- **绕过尝试**：
  - 伪造开关为假值字符串（`'0'/'false'/'no'/'off'/'2'/'maybe'/' '`）→ 全部 `False` ✅
  - 双开关（显式参数 + env）→ **显式参数优先**（`mock_allowed=True` 不受 `env='0'` 影响）。
    这是 docstring 写明的优先级，**不是** bug；但要如实指出：**凡能调用 `route_expectation` 的代码
    都能用 `mock_allowed=True` 自证清白**，该参数在**本轮生产代码里零调用方**（见 §8），
    守卫目前只防"由缺 key 推导出的放宽"。
  - 无其它可绕过路径（`mock_allowed` 在源码里已无 `not api_key` 同源推导：`grep` 仅剩注释文本）。

---

## 5. P1-4（span 非法不得击穿保护/对账）—— **CLOSED ✅**

探针同上。构造「预算已耗尽（`remaining<0`）但保护已执行」：

```
执行顺序 = ['protection', 'reconciliation', 'model']         <- 顺序固定且三步齐全
protection.ran=True ok=True  副作用计数=1                    <- 保护真的跑了
reconciliation.ran=True ok=True
model.ran=True ok=True
blocked=False  reason={'primary':'NO_EDGE', ...}
span_errors=('NEGATIVE_DEADLINE_REMAINING:management',
             'NEGATIVE_DEADLINE_REMAINING:reconciliation',
             'NEGATIVE_DEADLINE_REMAINING:model_decision')   <- 违规被拒登 + 留痕（每阶段一条）
预算耗尽时 with trace.span 正常退出（未抛异常）               <- 不再从 finally 击穿
record_span（显式提交）仍 strict 抛 SpanValidationError       <- 契约未被整体弱化
对照：预算充足 -> span_errors=()                              <- 正常路径无噪声
```
实现要点已核对：`DecisionTrace.span` 的 `finally` 走 `_append(..., strict=False)`
（`src/core/decision_trace.py:774-796`），`run_protection_first` 先把步骤结果收进局部变量再退出 `with`
（`:1012-1028`），`CycleRunResult.span_errors` 回带（`:965`）。**保护结果可返回且三步齐全 —— 成立。**

---

## 6. 补丁可应用性与全量回归 delta

### 6.1 `git apply --check`（15 条 / 独立 git repo 根）—— **新发现 N1：只有 8/15**

| 环境 | `git apply --check` 结果 |
|---|---|
| 评审工作区 `reviews/U05/R2/r1`（= 集成树状态，`core.autocrlf=false`） | **8 OK / 7 FAIL** |
| 独立沙箱 `.git/config` 里 `core.autocrlf=false`（我第一版） | **8 OK / 7 FAIL** |
| 沙箱 `core.autocrlf=true`（= 机器全局默认，作者的运行环境） | **15 OK** |
| 沙箱 + 先把目标文件 LF 归一 | **15 OK** |

失败集合（两处环境一致）：`01 factory.py`、`02 failover_client.py`、`03 openai_compatible_client.py`、
`05 graph.py`、`09 database.py`、`11 macro_public.py`、`12 onchain_public.py`；
`--3way` 也一样失败。`04/06/07/08/13/14/15/16` 恒 OK。

**根因（非 U05 引入）**：仓库 blob 是**混合行尾**，而补丁是**纯 LF**（`probe_apply.py` / `out_apply.txt`）：
```
src/llm/factory.py        BLOB crlf= 34 lf_only= 101      <- 同一文件里两种行尾
src/audit/database.py     BLOB crlf=639 lf_only=1884
src/core/graph.py         BLOB crlf=432 lf_only= 279
src/data/macro_public.py  BLOB crlf=134 lf_only=   0      <- 纯 CRLF
补丁文件 01..16            crlf=  0（全部纯 LF）
```
补丁的上下文行落在 CRLF 区域 → `git apply` 精确匹配失败。`core.autocrlf=true` 时 git 在写盘时补回 CRLF，
所以恰好通过 —— 也就是说**作者的「15/15 OK」依赖了一个未声明的环境前提**。
**内容正确性不受影响**：`probe_apply2.py`（`out_apply2.txt`）对两种应用方式的结果做 EOL 归一后比较，
15 个目标文件**逐字节相同**（`files differing beyond EOL: 0`），因此 delta 与测试结论仍可用。
→ 定级 **P2（交付风险、未登记）**：若 I 在 `core.autocrlf=false` 的仓库里直接 `git apply`，7 条会失败。
建议：在 README §四 注明「需 `git apply --ignore-whitespace`／`-c core.autocrlf=true`／或先 LF 归一」，
或把补丁重出为保留目标文件行尾（顺带可修仓库混合行尾，但那属另一任务）。

### 6.2 全量回归 delta（强制项，独立复现）

方法：`git archive 8a8e31e` → 两棵新鲜树 `sb/pristine`、`sb/patched_lf`（15 补丁应用成功），
**同一份统一清单**（打补丁树 `run_offline_regression.py::SCRIPTS`）、**同一 env**
（复刻 `main()` 的规则：`EXECUTION_MODE=paper`、剔除 `MTA_PROFILE`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、
剔除 `*BINANCE*/*SENSENOVA*` 的 KEY/SECRET/TOKEN），**逐条非 fail-fast**（自写 `run_delta_Q.py`，
绕开 `main()` 的 `check=True` fail-fast 与 `_validate_smoke_manifest()` 前置校验）。

（结果见下方「DELTA 结果」小节。）

---

## 7. 回归抽样（≥5 项）—— `probe_sampling.py` / `out_probe_sampling.txt`

| # | 项 | 结果 |
|---|---|---|
| 1 | **共享绝对 deadline**（R22） | `shared() is b -> True`；3 个 span 反推绝对期限 **spread=0.000000**；`slice(5s).deadline <= parent`；`slice(9999s)` **不延长**父期限 ✅ |
| 2 | **失败不阻塞保护** | 模型抛错 → `protection.ok=True`、`reconciliation.ok=True`、`blocked=False` ✅ |
| 3 | **原因码九码** | `REASON_CODES(9) == 任务卡九码 -> True`（`任务卡.md:157` 逐字比对）；`REASON_ORDER` 长 9；未知码 `UnknownReasonCode`；`classify_cycle` 主/辅共存 `{'primary':'DATA_UNAVAILABLE','secondary':['HOLD_EXISTING']}` ✅ |
| 4 | **计数分离** | A(1 轮/5 请求/0 失败轮次)：`cycles_total=1 api_attempts_total=5 requests_failed=0 failed_rounds=0`；B(1 请求/1 失败轮次)：`1/1/1/1`；**`requests_total` 字段已删除**（P2-1 修复）；无结局时 `rounds_total=None`（P2-2 修复）✅ |
| 5 | **p95/p99 nearest-rank** | `ceil(q*n)-1` 无插值：n=1→10、n=2→20、n=3→30、n=10→100、n=20→190、n=20,q=1.0→200、n=9,q=.99→90（**全部与手算一致**）；空样本 `None`；`stage_latency_stats([])={}`；`q=0` 抛 `ValueError` ✅ |
| 6 | **S02–S04 四类路由风险** | `SILENT_MOCK`（client=mock）✅；`EARLY_RETURN`（expect 后返回无尝试）✅；`PARAMETER_INHERITANCE`（响应模型≠期望且未声明）✅ + **已声明 `declared_fallbacks` 时不报** ✅；`CROSS_PROCESS_QUOTA`（空作用域）✅，且 **B-r1 P3-1 已修**：只给 `span=` 不给作用域现在也报（`probe_p3_1.py` / `out_probe_p3_1.txt`：`只给 span -> [CROSS_PROCESS_QUOTA]`） |
| 7 | **P2-8 `attempt_timeout` 越界** | 已修：`remaining=0.100s -> 0.100`、`remaining=0.103s -> 0.103`，**不再越过** `remaining_seconds()` ✅（`probe_residuals.py`） |

---

## 8. 登记是否如实

| 登记项 | 位置 | 我的核对 |
|---|---|---|
| **market_query 撤回（S03 未落地）为显式偏差** | 补丁 README §二.4 + RESUME:36 | **准确** ✅。实测 `src/data/market_query.py` 里**没有** `reason_from_quality`（只有 U03 自带的 `_source_quality`）；撤回影响描述（11/12 的 helper 无生产调用方、U08 接漏斗时自行串接）与我的 grep 一致 |
| **生产接线缺口（归 U08 / U08+U12）** | 补丁 README §五 + §二.4 | **准确且无夸大** ✅。我独立 grep 逐一确认这些符号在 `src/` 里**只有定义处/`__all__`/docstring**：`run_protection_first`、`classify_evidence_report`（仅被同样无调用方的 `classify_cycle` 调）、`reason_from_source_errors`（仅被无调用方的 `reason_from_errors` 调）、`reason_from_errors`、`ModelRouteGuard`、`record_decision_trace`、`graph.model_deadline()`。**没有把未接线写成已接线** |
| **P2-7（数据侧原因码链路未接线）** | 补丁 README §二.4 / §五 | **已登记** ✅（文字准确） |
| **P2-4（04 依赖 09）** | 补丁 README §二.2（明确更正 r1 的错误陈述） | **已登记且更正** ✅ |
| **market_query 撤回的约束强度** | 补丁 README §二.4 | 与 B-r1 的独立结论一致（U03 独占测试 `resolve_patch_states()` 重建两态，改动即 `ValueError: patch context mismatch at line 100`）。本轮我**未重复**跑 B-r1 的 E 探针（该约束与 r2 改动无关），但核对到撤回**事实上已落地** |
| **P2-3 / P3-3 / P3-4 / P3-5 登记为"不修"** | — | **新发现 N3：找不到登记文字**。`8a8e31e/f9a46bc/2049c1c` 的提交信息、补丁 README §一–§六、`I/docs/dispatch/RESUME.md:36` 均**未出现** P2-3/P3-3/P3-4/P3-5。我实测这 4 条**仍然存在**（见 §9），其中 P2-3 只在 `decision_trace.py` 的模块/函数 docstring 里被顺带说明（"不置 `blocked`"），P3-3/P3-4/P3-5 无任何文字 |

---

## 9. 新发现（本轮 r2 独立探针）

| # | 级别 | 位置 | 问题 | 证据 |
|---|---|---|---|---|
| **N1** | **P2（交付风险，未登记）** | 15 条补丁整体 | 在 `core.autocrlf=false`（本仓库 `/multi-agent-trader/.git/config` 的实际取值）的树/仓库里，**7/15 条 `git apply --check` 失败**（`--3way` 同）。作者报告"15/15 OK"依赖未声明的 `autocrlf=true` 前提。根因是仓库既有混合行尾，非 U05 引入；应用后的**内容**两种方式逐字节一致 | §6.1；`out_apply.txt`、`out_apply2.txt` |
| **N2** | **P2（密钥漏报，未登记）** | `scripts/scan_tracked_secrets.py:227-232` | **代码位置、identifier 形状的裸明文凭据仍静默漏报**（`.env` 内容进 `.py`、`api_key = <64字符key>`、`.py` 顶层 YAML 行、模块层裸键 JSON 块），且源码注释给出的"过不了语法解析"理由**不成立**。有 `skipped_bare_identifiers` 留痕，故不属"静默" | §1.3；`out_probe_secret3.txt` |
| **N3** | **P3（登记不完整）** | 交付文档 | P2-3（`blocked` 恒 `False`）、P3-3（`attempt_observer` 回调元数 02=3 参 / 03=4 参）、P3-4（`SpanHandle` 可变无守卫）、P3-5（`admit()` 无 `deadline` 时仍 `started + default_queue_deadline_seconds`）在交付 README / RESUME 中**无登记文字** | §8；`probe_residuals.py` / `out_probe_residuals.txt`（实测四条仍在，P2-8 已修） |
| **N4** | **P3（流程/位置）** | `docs/dispatch/patches/U05/` | 补丁包**不在** `mat-wt/I/docs/dispatch/patches/U05/8a8e31e/`（I 下只有 U01/U13/U15），实际位于 **`mat-wt/CD/...`**，而评审对象是 `mat-wt/I` 里的 `task/U05`。评审入口（任务书给的路径）与实物不符；若 I 按 `I/docs/dispatch/patches/` 找会扑空 | `ls I/docs/dispatch/patches/` vs `ls CD/...` |
| N5 | P3（补充） | `src/llm/openai_compatible_client.py` | KI01 放宽未覆盖"右括号紧贴"形态：`build(api_key=fallback_key)` 仍误报（`fallback_key)` 过不了 identifier `fullmatch`）。**不是回归**（未打补丁版本也报），属"未修尽"；作者自测只覆盖带逗号形态 | §1.1(b)；`out_probe_secret2.txt` |

---

## 10. 未覆盖 / 无法离线验证

1. **KI02 未判因**：`scripts/smoke_windows_service_plan.py` 仍是唯一失败项，原因仍是树根缺 `.venv`
   （`python_executable 不存在`），与环境有关；**我未能判定其真实失效原因**，只证明与 U05 无关。
2. **无网络 / 未接真实 LLM**：`_mta.deadline_*` 在真实 Failover 往返中的取值、`Retry-After` 冷却、
   真实额度交互均未验。P1-3 的"运行时降级"只在**假客户端**上验证，`build_llm_for_agent` 的
   `degraded_reason` **没有任何生产代码把它送进 `ModelRouteGuard`**（未接线），所以"缺 key 会被守卫抓到"
   目前只在**库层**成立。
3. **租约回收只测假 pid**：S04 的 `LeaseRegistry.owner_alive()` Windows 分支（`OpenProcess`）未构造
   "真进程被 kill" 的跨进程场景。
4. **无 pytest**：四套 `tests/integration/*` 未用 pytest 跑；我改用**直连模块的探针**覆盖同一个 API 面，
   未复跑作者报告的 115/63/48/45 计数（这些计数由 B-r1 复现过，且 r2 已改断言）。
5. **补丁 13 的历史回溯**：只验证当前树的扫描结果，未做历史提交扫描。
6. **U08/U12 消费侧**：原因码/漏斗最终形态未验（未实现）。
7. **P1-5 的 `.py` 内 JSON 多行块（带引号键）**：不报，但我判定为**既有正则限制**（未打补丁版本同样不报），
   未追溯该限制的历史引入点。

---

## 11. 命令清单（可回溯）

```bash
# 沙箱与行尾/应用性
PY=<venv>/python.exe
git -C I archive 8a8e31e | tar -x -C _scratch_Q/sb/pristine   # 之后 git init/add
python _scratch_Q/eol_analyze.py            > _scratch_Q/out_eol.txt
python _scratch_Q/probe_apply.py            > _scratch_Q/out_apply.txt
python _scratch_Q/probe_apply2.py           > _scratch_Q/out_apply2.txt
# 在评审工作区逐个 git apply --check -p1 <patch>   （8/15；--3way 同）
# P1-5
python _scratch_Q/probe_secret.py           > _scratch_Q/out_probe_secret.txt
python _scratch_Q/probe_secret2.py          > _scratch_Q/out_probe_secret2.txt
python _scratch_Q/probe_secret3.py          > _scratch_Q/out_probe_secret3.txt
python _scratch_Q/probe_fingerprint.py      > _scratch_Q/out_probe_fingerprint.txt
(cd _scratch_Q/sb/patched_auto && $PY scripts/scan_tracked_secrets.py) > _scratch_Q/out_scan_auto.txt
(cd _scratch_Q/sb/pristine    && $PY scripts/scan_tracked_secrets.py) > _scratch_Q/out_scan_pristine.txt
# P1-1 / P1-2 / P1-3 / P1-4 / 抽样 / 残留
python _scratch_Q/probe_p1_1.py             > _scratch_Q/out_probe_p1_1.txt
python _scratch_Q/probe_p1_2_3_4.py         > _scratch_Q/out_probe_p1_2_3_4.txt
python _scratch_Q/probe_p3_1.py             > _scratch_Q/out_probe_p3_1.txt
python _scratch_Q/probe_sampling.py         > _scratch_Q/out_probe_sampling.txt
python _scratch_Q/probe_residuals.py        > _scratch_Q/out_probe_residuals.txt
# 全量 delta
python _scratch_Q/run_delta_Q.py            > _scratch_Q/out_delta_Q.txt
```
（全部以 `PYTHONUTF8=1` 运行；所有产物只落在 `reviews/U05/R2/r1/_scratch_Q/`，未写任何其它 worktree。）

---

## DELTA 结果（独立复现，强制项）

原始输出 `out_delta_Q.txt`（逐条 154 行）、逐脚本 stdout/stderr 落 `delta_logs_Q/`、
结构化结果 `result_delta_Q.json`。

```
统一清单条数（patched 树） = 77
pristine 自带清单条数 = 73
pristine 清单是统一清单的子集 ? True
只存在于 patched 的条目: ['scripts/smoke_u05_background_refresh_budget.py',
                          'scripts/smoke_u05_cross_process_admission.py',
                          'scripts/smoke_u05_decision_trace.py',
                          'scripts/smoke_u05_model_route_safety.py']

pristine 失败集合: ['scripts/smoke_windows_service_plan.py']
patched  失败集合: ['scripts/smoke_windows_service_plan.py']
新增失败 (patched - pristine): []
修好     (pristine - patched): []

4 个 U05 冒烟在 两棵树 的退出码: 全部 0
```

- **新增失败 = ∅，独立确认成立**（与作者声称一致）。
- 唯一失败 `scripts/smoke_windows_service_plan.py` = B-r1 已判定的 **KI02 既有环境项**（树根缺 `.venv`），
  与 U05 无关；**我未能判定其真实失效原因**（见 §10.1）。
- 与 B-r1 的差异：B-r1 报告其沙箱 `files=398`，我用同一方法得 **397/397**（与作者一致），
  不影响 delta 结论（两边同法构建、同一清单）。
- 注意：harness 的 `main()` 是 fail-fast（`check=True`）且开头 `_validate_smoke_manifest()`，
  我未用 `main()`，按 `SCRIPTS` 逐条非 fail-fast 跑，故绕开了"一旦 manifest 报错整轮中止"。

**作者自测计数的独立对照**（取自 `delta_logs_Q/`）：
```
patched : scripts/test_secret_scanner.py  结果：PASS 26 / FAIL 0      （作者称 26/0，一致）
pristine: scripts/test_secret_scanner.py  结果：PASS  7 / FAIL 0      （r1 版本为 13 条，r2 扩到 26）
两棵树 : scripts/smoke_contracts_v3.py    结果：PASS 173 / FAIL 0     （作者称 173/0，一致）
```
另核实：`route_expectation` / `mock_explicitly_allowed` 在 `src/` 里**除 `factory.py` 自身外零调用方**
（`grep -rn` 仅命中定义与内部调用），故 P1-3 的守卫目前只在库层可判定 —— 与作者 §五 未接线登记一致。

---

## 12. 放行条件（本轮结论）

**CHANGES_REQUESTED**（唯一阻断项 P1-5；其余 4 条 P1 全部独立确认 CLOSED）

必须处理：
1. **P1-5 残留（N2）**：把「代码位置、identifier 形状的裸明文凭据不报」**如实登记**为已知限制
   （并**改掉** `scripts/scan_tracked_secrets.py:27-29` 那句不成立的"过不了语法解析"理由）；
   或按 §1.3 的方案 2 收窄判定（只对同一文件内被绑定/使用的标识符放宽）。
   最低限度也要在 `skipped_bare_identifiers` 之外给出**行号级留痕**。
2. **N1 补丁可应用性**：在补丁 README §四 注明 `git apply` 的**行尾前提**
   （`--ignore-whitespace` 或 `core.autocrlf=true` 或先把目标文件 LF 归一），
   避免 I 在 `core.autocrlf=false` 的仓库里 7 条失败却无提示。

建议一并处理（P3，不阻断）：
3. N3：补登 P2-3 / P3-3 / P3-4 / P3-5 四条"登记不修"，或说明为何不单列。
4. N4：把补丁包放到任务书给的位置（或在 RESUME 里指明实际路径在 `CD/`）。
5. N5：KI01 未修尽的"右括号紧贴"形态，可在 README §三 补一句边界说明。

**已独立确认、可作为放行依据的部分**：
- P1-1 / P1-2 / P1-3 / P1-4 **全部 CLOSED**（探针自写，见 §2–§5）；
- 全量回归 **delta = ∅**（统一清单 77，唯一失败 = KI02）；
- 登记表**行号 + 内容指纹双锚安全**（行号漂移不再误报，内容变真凭据仍报且登记失效）；
- 作者声称的 `skipped=20 / unparsable=0`、`files=397`、`registered=3` **独立复现一致**；
- market_query 撤回、生产接线归 U08/U12、P2-7 **登记准确**，未把未修写成已修；
- P2-1 / P2-2 / P2-8 / P3-1 已修并验证。

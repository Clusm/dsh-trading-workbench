# Q-U05 r3 独立复验报告（角色 Q / 修复轮 r3 / 第 3 轮）

- 复验对象：`task/U05` head **`6197b83`**（`65651df` → `b2b7f96` → `6197b83`）；上一轮 `2049c1c`（Q-r2 判 CHANGES_REQUESTED）
- 补丁包：`docs/dispatch/patches/U05/65651df/`（15 条，`10_*` 仍撤回）
- 复验者：Q（独立会话），工作区 `reviews/U05/R3/r1`（detached `6197b83`，只读）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`，`PYTHONUTF8=1`，无 pytest
- 集成树：`integration/unattended-v3` = `a0bb7a4`（`78526d7` 为其祖先；15 个补丁目标文件 blob 与 `78526d7` **逐一相同**）

> 本文件**边做边写**。原始 stdout 全部落 `_scratch_Q/`。

---

## 0. 环境事实（先行核验）

```
worktree r121 = reviews/U05/R3/r1，detached 6197b83；git status --short 只有 ?? _scratch_Q/
repo .git/config: core.autocrlf = false（git config --global core.autocrlf 未设 → exit 1）
15 个补丁目标文件 blob: 6197b83 == 78526d7 == a0bb7a4（14/15 相同；
   scripts/run_offline_regression.py 在 U05 分支侧 DIFF —— 见 §2.2）
集成 HEAD 现状：integration/unattended-v3 = a0bb7a4（78526d7 是其祖先，见 out/u05_exclusive.txt）
r3 相对 r2 的提交：65651df(fix) → b2b7f96(目录改名) → 6197b83(登记口径) → 6197b83 == HEAD
```

> 本文件**边做边写**完成。所有原始 stdout 落 `_scratch_Q/out/`，逐脚本日志落 `_scratch_Q/delta_logs_Q/`。

---

## 1. P1-5（原阻断项）—— **已闭合（附一条已登记残余的边界说明）**

探针 `probe_p15.py` → `out/out_probe_p15.txt`、`result_p15_Q.json`。**三份扫描器直载**（自建）：
- pristine = `reviews/U05/R3/r1/scripts/scan_tracked_secrets.py`（= 集成 HEAD 版本，无 `_bound_names`）
- r2 = 由 `2049c1c` 历史里的 `8a8e31e/13_*.diff` **重新应用**得到（`vers/scan_r2.py`，365 行）
- r3 = 沙箱 `int_patched` 里应用 13 后得到（`vers/scan_r3.py`）

**全部用例自造**，未采信作者 `_scratch_author_r4/` 的任何输出。

### 1.1 任务点名"五类必须报出" —— **r3 全部报出，5/5 ✅**

| 用例 | pristine | r2 | **r3** |
|---|---|---|---|
| A1 `.env` 单行 `api_key=<40位>` | 报 | 不报 | **报** ✅ |
| A2 `.env` 多行（key+secret，2 行） | 报 2 | 不报 | **报 2** ✅ |
| A3 `api_key = <64 位 hex>`（空格赋值） | 报 | 不报 | **报** ✅ |
| A4 `.py` 顶层 YAML `api_key: <64hex>` | 报 | 不报 | **报** ✅ |
| A5 模块层裸键 JSON 块 | 报 | 不报 | **报** ✅ |

补充同族：A6 裸键 JSON（40 位）、A7 YAML（40 位）、A8 无空格、A9 base32 形态 —— **r3 全部报出** ✅

### 1.2 必须不报（真变量引用 / N5）—— **r3 全部不报，5/5 ✅**

| 用例 | pristine | r2 | **r3** |
|---|---|---|---|
| B1 `build(api_key=fallback_key, model=m)`（已绑定） | **误报** | 不报 | **不报** ✅ |
| B2 **N5** `build(api_key=fallback_key)`（右括号紧贴） | 误报 | **误报** | **不报** ✅（N5 已修） |
| B3 `api_key = cfg.llm.api_key`（属性链，根名绑定） | 误报 | 不报 | **不报** ✅ |
| B4 `api_key = runtime_secret`（普通变量） | 误报 | 不报 | **不报** ✅ |
| B5 行内实参带 `)` + 换行续行 | 误报 | 不报 | **不报** ✅ |

→ r2 报告的 **N5（KP2/KP4 右括号紧贴误报）已修**，独立复现。

### 1.3 不回退（引号 / 注释 / 语法错误）—— **成立，10/10 ✅**

C1 引号、C2 行内注释、C3 纯注释、C4 语法错误、C5 缩进错误、C6 三引号 docstring、
C7 f-string 字面段、C8 装饰器引号参数、C9 `.yaml` 后缀、C10 非 `.py` 后缀 —— **r3 全部报出**。
C4/C5 同时留痕 `unparsable_python_files=1`（fail-closed 路径生效）。
**注意 C5**：r2 在缩进错误下**漏报**（`skipped:1`），r3 修复（tokenize 抛 `IndentationError` → 整体放弃放宽）。

### 1.4 绕过尝试（任务书点名的 9 类，逐条独立构造）

| # | 绕过形态 | r3 结果 | 判定 |
|---|---|---|---|
| D1 | 裸标识符 `abcdef1234567890`（**全文件无绑定**） | **报出** | ✅ CLOSED |
| D2 | 超长 hex 裸值（无绑定） | **报出** | ✅ CLOSED |
| D3 | base32 形态裸值（无绑定） | **报出** | ✅ CLOSED |
| D8 | `match 1:` / `case <cred>:` 捕获模式 | **报出** | ✅ CLOSED（`MatchAs` 不进 AST 绑定表） |
| D9 | `. 前缀属性链` `obj.api_key = <64hex>` | **报出** | ✅ CLOSED |
| D10 | `dict(api_key=<64hex>)` 关键字实参 | **报出** | ✅ CLOSED |
| D4 | 同名绑定放在**未被调用的函数**里 | **不报** | ⚠️ 见 1.5 |
| D5 | `del <cred>` 污染 | **不报** | ⚠️ 见 1.5 |
| D6 | `lambda <cred>: 1` 参数污染 | **不报** | ⚠️ 见 1.5 |
| D7 | 推导式目标 `[<cred> for <cred> in []]` | **不报** | ⚠️ 见 1.5 |
| D11 | `global <cred>` **声明但未赋值** | **不报** | ⚠️ 见 1.5 |
| D12 | `nonlocal <cred>` 声明但未赋值 | **不报** | ⚠️ 见 1.5 |
| D13–D20 | `for` 目标 / `with as` / `def`·`class` 名 / `except as` / `import as` / 海象 / 解包 / **绑定写在赋值之后** | **不报** | ⚠️ 见 1.5 |

**额外独立反例（我自己加）**：

| # | 形态 | r3 | 备注 |
|---|---|---|---|
| E2 | 跨行调用实参（未绑定裸值） | 报 ✅ | |
| E3 | 类作用域裸键 JSON | 报 ✅ | |
| E4 | **N5 反向**：未绑定裸值 + 右括号紧贴 | 报 ✅ | 证明 N5 的剥离逻辑**没有顺带放宽**真凭据 |
| E5 | 同一行两个凭据 | 报 1 ✅ | |
| E8 | `def f(api_key=<64hex>)` 默认参数值 | 报 ✅ | |
| E10 | 三引号内嵌 YAML | 报 ✅ | |
| E11 | dict 字面量裸键 | 报 ✅ | |
| E12/E13/E14 | 空文件 / 短值 / 占位符 | 不报 ✅ | 与 pristine 一致 |

### 1.5 ⚠️ 唯一保留面 = **README §5.3 已登记的残余**（D4–D20），非未登记漏报

D4–D20 的**共同前提**是：明文凭据**逐字等于同一文件里某个被绑定的名字**。
r3 的放宽条件正是"根名在 AST 符号表里被绑定过"，所以这些形态全部落进放宽面。
这正是作者在 **README §5.3** 里明写的保留残余
（"明文凭据的形状与同一文件内某个已绑定名字逐字相同……属刻意保留的误报抑制代价"），
并且**源码 docstring（`scan_tracked_secrets.py:27-35`）也逐字登记了同一残余**。
→ 我判定这属**已如实登记**，不是 r2 那种"漏报 + 用不成立的理由否认"。

**但必须如实指出三点边界**（作者登记文字未覆盖）：

1. `_bound_names` 把 **`ast.Del`（`del x`）、`ast.Global`、`ast.Nonlocal`** 也算作"被绑定"
   （`scan_tracked_secrets.py:172`、`:186-187`）。这三者**在语义上并未绑定任何名字**：
   `global X` 只是作用域声明，`del X` 是删除。D11/D12/D5 因此可在"该名字从未被赋值"的文件里
   放行明文凭据。§5.3 的措辞是"某个**已绑定**名字"，严格讲**覆盖不到** `global`/`nonlocal`/`del` 这三类。
   影响：仍要求攻击者把凭据写第二遍（写在 `global`/`del` 行上），而**那行本身在任何版本都不被规则命中**
   （规则只认 `api_key=` 之类形态）—— 故不构成"U05 新增的可利用绕过"，属**登记措辞不够精确**。
2. **放行留痕只有总数**（`skipped_bare_identifiers=10`），**没有行号级留痕**。
   r2 复验给的最低要求是"在计数之外给出行号级留痕"（该轮我列为备选方案 3），
   r3 选择了方案 2（收紧判定），因此未加行号 —— 可接受，但"哪些行被放行"仍只能靠人工 grep 复核。
3. D4/D20（绑定在**未被调用的函数**里 / 绑定写在**赋值之后**）表明放宽是**全文件、跨作用域、顺序无关**的，
   比 §5.3 例子暗示的"同一处"更宽。

**D1–D3、D8–D10（任务书点名的"合法标识符但未被绑定"形态）全部报出**，
所以 r2 的实质漏报（`.env` 进 `.py`、64 位 hex、顶层 YAML、模块层裸键 JSON）**已真闭合**。

### 1.6 既有盲区（**pristine 同样漏，非 U05 引入**，仅记录）

| # | 形态 | pristine | r2 | r3 | 说明 |
|---|---|---|---|---|---|
| E1 | `BINANCE_API_KEY=<64hex>`（带主键前缀） | 不报 | 不报 | 不报 | `\b` 词边界：`_` 与 `A` 之间无边界 → 正则根本不匹配。**规则设计既有面** |
| E7 | `api_key = dGhp…Q=`（base64 补位/含空格） | 不报 | 不报 | 不报 | 含空格 → 裸值截断为 10 字符 < 12 阈值 |
| E9 | `api_key: str = <64hex>` | 不报 | 不报 | 不报 | 正则先匹配到 `api_key: str`（值 `str` 太短），后续 `= <64hex>` 前无关键字 |

三者在 **pristine 也不报**，故不属 U05 回归；但 E1 是现实中".env 粘进 .py"很常见的形态，**建议后续任务**补规则。

### 1.7 实测计数（独立复现作者声称）

```
CD 树（task/U05 = 6197b83，不含评审文档）
  scripts/scan_tracked_secrets.py -> SECRET SCAN OK files=397 registered_false_positives=3
                                     skipped_bare_identifiers=10 unparsable_python_files=0   (exit 0)
```
与作者 §5.5 的 `397/3/10/0` **逐字一致**（r2 为 skipped=20，r3 收紧到 10）。

---

## 2. 补丁可应用性 —— **15/15 成立，r2 的 N1 已闭合**

### 2.1 集成工作区等价配置（`core.autocrlf=false`，plain 无 override）

沙箱 `sb/int_pristine` = `git archive a0bb7a4 | tar -x` + `git init` + `core.autocrlf=false`
（= 真实集成工作区 `I` 的磁盘形态；**已用 `git -C I ls-files --eol` 只读核对**：目标文件 `w/lf`/`w/crlf`/`w/mixed`，
与沙箱一致，见 `out/int_eol.txt`）。

```
$ for p in 15 条; do git apply --check -p1 $p; done
01..09, 11..16                            -> 15/15 OK        <-- 新增：r2 时只有 8/15
--3way 同样                                -> 15/15 OK
```
原始输出 `out/apply_check_int.txt`。

**"全 CRLF 工作树"组合确认不会出现在集成工作区**：
`git -C I ls-files --eol` 统计（`out/int_eol.txt`）：`i/crlf = 37`、`i/lf = 330`、`i/mixed = 56`、`i/none = 8`。
即**只有 37/431 个文件是纯 CRLF**，仓库 blob 绝大多数是 LF 或混合行尾，
`autocrlf=false` 的检出**不可能**产出"全 CRLF 工作树"。作者 §四 场景 6（`autocrlf=false` + 全 CRLF = 2/15）
在本项目**不可达**，其"不支持"的措辞成立。

### 2.2 应用后的行为验证（不是只看 --check）

在 `sb/int_patched`（集成 HEAD + U05 独占 9 文件 + 15 补丁）实跑：

```
tests/integration/test_decision_trace.py              结果：PASS 115 / FAIL 0   （rc=0）
tests/integration/test_model_route_safety.py          结果：PASS  63 / FAIL 0   （rc=0）
tests/integration/test_background_refresh_budget.py   结果：PASS  48 / FAIL 0   （rc=0）
tests/integration/test_cross_process_llm_admission.py 结果：PASS  45 / FAIL 0   （rc=0）
scripts/smoke_contracts_v3.py                         结果：PASS 173 / FAIL 0   （rc=0）
scripts/test_secret_scanner.py                        结果：PASS  36 / FAIL 0   （rc=0）  r2 为 26
```
**四套计数 + smoke_contracts_v3 + 扫描器测试全部与作者声称逐字一致**（`out/test_*.log`）。

**唯一例外（非补丁问题）**：在本评审工作树 `6197b83` 上 `15_run_offline_regression_scripts.diff` `--check` 失败，
因为 U05 分支侧的 `scripts/run_offline_regression.py` 与集成 HEAD **本就不同**
（该文件在 U05 分支上不含 U04/U13 的登记行）。补丁基线是集成 HEAD，所以在**集成树**上 15/15、
在**U05 分支树**上 14/15 —— 这是基线的正常后果，不是缺陷（README §〇 已写明基线口径）。

---

## 4. 集成树 secret scan 18 条来源 —— **确认全部来自文档，零条来自 `src/`/`scripts/`**

在 `sb/int_patched`（集成树 + 15 补丁）实跑门禁，得 **恰好 18 条**（`out/scan_int_r3.txt`）：

```
docs\dispatch\reviews\U05\04a2c30\B_r1.md:217,218,219,220,221,222   rule=secret-assignment   (6)
docs\dispatch\reviews\U05\2049c1c\Q_r2.md:60,61,62,66,67,128,187,363 rule=secret-assignment  (8)
docs\无人值守升级任务清单_2026-09-19.json:1312,1363                 rule=secret-assignment   (2)
docs\无人值守升级任务清单_2026-09-19.json:3238                      rule=provider-token + secret-assignment (2)
```
- `grep -c -E 'FAIL (src|scripts)[\\/]'` = **0** → **没有任何一条来自 `src/` 或 `scripts/`** ✅
- 与 pristine 同一棵树对照（`out/scan_int_diff.txt`）：pristine = 22 条，r3 = 18 条，
  差集 = `docs/dispatch/reviews/U00/0b85bef/C_w0r1_r2.md:31`（已登记）、
  `src/llm/factory.py:111`（KI01 真误报，已修）、`tests/acceptance/test_runtime_identity.py:523,707`（已登记）。
  **r3 只减不增：新增失败 = ∅，消除 = 恰好 4 条已知误报** ✅
- 行号与作者 §5.5 略有出入（JSON 1283→1312 等，整体 +29 行），因我的集成树是 `a0bb7a4`（含 U02 两笔 docs 提交），
  基线 78526d7 之后的文档增长所致，**分类与计数完全一致**。

---

## 3. 登记补齐（P2-3/P3-3/P3-4/P3-5）—— **已补齐，位置准确，未把未修写成已修**

`README.md` §5.4「B-r1 遗留四项：**登记为"不修"**」（`docs/dispatch/patches/U05/65651df/README.md:180-205`）
以四列表格（位置 / 问题 / 影响 / 为何不修）逐条登记。**逐条独立核对**（原始输出 `out/registration_check.txt`、
`out/registration_grep.txt`）：

| # | 登记位置 | 我在**打补丁后的集成沙箱**实测 | 判定 |
|---|---|---|---|
| P2-3 | `src/core/decision_trace.py:961`（`blocked: bool = False`），唯一构造处 `:1044` | `:961` = `blocked: bool = False` ✅；全文 `blocked` 仅 4 处（`:961` 定义、`:973` 序列化、`:1044` 唯一构造 `blocked=False`），**确实没有置真路径** ✅ | **位置与事实均准确** |
| P3-3 | `src/llm/failover_client.py:58` vs `src/llm/openai_compatible_client.py:79` | `:58` = `Callable[[str, str|None, float|None], None]`（3 参）✅；`:79` = `Callable[[int, str, float|None, str|None], None]`（4 参）✅。`grep -rn attempt_observer src/` 只命中**两处定义 + 两处自身读取**，**无外部注入方** ✅ | **准确** |
| P3-4 | `src/core/decision_trace.py:902`（`SpanHandle`） | `:902` = `class SpanHandle:`，其上一行 `@dataclass`（**非 frozen**），`started_ms`/`ended_ms`/`deadline_remaining_ms` 均为可写字段 ✅。`grep SpanHandle` 仅命中 `decision_trace.py`（`:747`/`:760`/`:766`/`:902`/`:1771 __all__`）✅ | **准确** |
| P3-5 | `src/llm/request_coordinator.py:198-199` | `:198` = `if effective_deadline is None:`，`:199` = `effective_deadline = started + self.default_queue_deadline_seconds` ✅（正是"第二期限"） | **位置准确，措辞微瑕** |

**"没有把未修写成已修"** ✅：§5.4 标题即"登记为**不修**"，每条都给了"为何不修"，
且 §5.1/§5.5 仍如实承认生产未接线、门禁 18 条既有失败。§一 的补丁状态表也只把 13/14/15 标为"已改"。

**两处措辞微瑕（P3，不影响结论，但建议 I 记一笔）**：
1. P3-5 写"`admit()` 的 `deadline` 是可选参数，**生产默认不传**"。实测生产唯一调用点
   `src/llm/openai_compatible_client.py:128` **是传的**（`deadline=deadline`），
   只是 `_post(..., deadline: float | None = None)` 的缺省值是 `None`。
   净效果（`None` → 推导第二期限）与登记一致，**不是错误陈述**，但"不传"不准确。
2. P3-4 写"无第三方持有"，而 `SpanHandle` 列在模块 `__all__`（`:1771`）里，
   是对外公开类型；"无第三方持有"在**当前仓库**成立（无外部构造/持有），
   但措辞容易被读成"不可导出"。

**RESUME 侧**：`I/docs/dispatch/RESUME.md:36` 仍是 **r2 轮次**的状态行
（"P2-3/P3-3/P3-4/P3-5 登记文字缺失…r3 要求…"），尚未更新到 r3 结论。
按协作约定 RESUME 只由 I 更新，**不构成作者缺交付**；但 I 合入时需一并把该行推进。

**独立性自查（我加的风险项）**：四条登记都只描述"存在但不修"，**没有一条声称已修**；
且我实测四条**确实仍然存在**（P2-3 `blocked` 恒 `False`、P3-3 元数不一致、P3-4 可变句柄、
P3-5 双期限），与登记一致。

---

## 6. 回归抽样（≥5）—— **全部通过（FAIL 0）**

探针 `probe_sampling_Q.py` → `out/out_sampling_Q.txt`（原始 stdout）。
全部直载**打补丁后**的集成沙箱源码，用例自造。

| # | 项 | 关键独立断言 | 结果 |
|---|---|---|---|
| 1 | **共享绝对期限（R22）** | `shared() is parent`；子切片 `deadline <= parent.deadline`；父更紧时 3 个切片**逐位相同**；`attempt_timeout(1)/(3)` 不越界 `remaining`；余量 0.05s（< 下限）仍不越界；余量 0.103→0.103、0.100→0.100（**确定性时钟**消除 µs 抖动）；期限耗尽抛 `DeadlineExceeded` | **PASS** |
| 2 | **P1-2 门禁不得归因 NO_EDGE** | 门禁整体失败→`RESEARCH_INCOMPLETE`+`evidence_gate_failed`；门禁通过且无拒绝→`NO_EDGE`；无结论→`evidence_gate_unknown`；`identity_pinned=False`→`gate:identity_unpinned`；逐意图拒绝码/被丢意图均回带；部分失败主因保留逐意图码；门禁通过但源不可用→`RESEARCH_INCOMPLETE`+`DATA_UNAVAILABLE` 辅因 | **PASS（10/10）** |
| 3 | **P1-3 `mock_allowed` 仅显式开关** | 12 种 env 取值（`''/0/false/no/off/2/maybe/空格` → False；`TRUE/ Yes /1/on` → True）；裸构造 `False`；`SILENT_MOCK` 三条路径（mock client、mock 响应体、**运行时 degraded_reason 无条件上报**）；显式允许且无降级→不报；`EARLY_RETURN`、`CROSS_PROCESS_QUOTA`（空作用域报、有作用域不报）、`PARAMETER_INHERITANCE`（未声明报、已声明不报） | **PASS（23/23）** |
| 4 | **P1-4 span 不击穿保护** | 顺序恒为 `protection→reconciliation→model`；三步全 `ran/ok`；模型抛错仍先跑保护+对账；`blocked is False`；预算耗尽下 `with trace.span` **不抛**且 `span_errors` 留痕 `NEGATIVE_DEADLINE_REMAINING:management` | **PASS（10/10）** |
| 5 | **P1-1 `latency_ms` 真实** | 写库后直查 SQLite：真实耗时行 `latency_ms=1234`、未测到行 **NULL**、`error` 恒 NULL；`failed_rounds` 进 `output_summary` 顶层 **且** `counters`；按 U01 口径 `AVG(latency_ms)`=**1234.0**（`COUNT(latency_ms)=1`，NULL 被忽略） | **PASS（8/8）** |

**补充独立验证（作者未声称但我顺手做的）**：

- **r3 只改 13/14 的内容**：我把 `2049c1c` 历史里的 r2 包逐条取出（`vers/r2pkg/`），
  对两版补丁的 **`+`/`-` 净内容行**做逐行比对（`out/patch_content_diff.txt`）：
  **01–09、11、12、15、16 全部 `same`**，只有 **13 与 14 `DIFF`** —— 与 README §〇 声称一致 ✅
- `scripts/run_offline_regression.py` 的 `SCRIPTS` 已登记 4 个 `scripts/smoke_u05_*.py`
  （`:93-96`），且冒烟清单校验在打补丁树上成立。
- `test_secret_scanner.py`：**PASS 36 / FAIL 0**（r2 为 26），含"登记项带理由且不含凭据原文"
  与"无过期登记"两条。

---

## 5. 全量 delta（强制项，独立复现）—— **新增失败 = ∅，两棵树均 ∅**

方法：`run_delta_Q.py`（自写）。两棵树由 `git archive` 重建：
`sb/int_pristine` = 集成 HEAD `a0bb7a4` + U05 独占 9 文件；
`sb/int_patched` = 前者 + 15 补丁（逐条实跑 `git apply` 成功，见 §2.2）。
**统一清单取自打补丁树的 `scripts/run_offline_regression.py::SCRIPTS`（79 条）**，
**同一 env**（复刻 `main()`：`EXECUTION_MODE=paper`、剔除 `MTA_PROFILE`、`MTA_NO_PROXY=1`、
`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、剔除 `*BINANCE*/*SENSENOVA*` 的 KEY/SECRET/TOKEN），
**逐条非 fail-fast**（不用 `main()` 的 `check=True`，也不受 `_validate_smoke_manifest()` 前置校验阻断）。

原始输出 `out/out_delta_Q.txt`（158 行逐条）、逐脚本 stdout/stderr 落 `delta_logs_Q/`（158 个文件）、
结构化结果 `result_delta_Q.json`。

```
统一清单条数 = 79   （pristine 自带清单 = 75）
pristine 自带清单 ⊂ 统一清单 ? True
仅存在于统一清单: ['scripts/smoke_u05_background_refresh_budget.py',
                  'scripts/smoke_u05_cross_process_admission.py',
                  'scripts/smoke_u05_decision_trace.py',
                  'scripts/smoke_u05_model_route_safety.py']
pristine 失败: []
patched  失败: []
新增失败: []
修复    : []
```
- **158 次运行全部 `rc=0`**，`grep "rc=" | grep -v "rc=0"` 为空。
- **新增失败 = ∅ 独立确认**；与作者声称（79 / ∅ / ∅）逐项一致。
- 与 r2 的差别：r2 沙箱里 `scripts/smoke_windows_service_plan.py` 是唯一失败项（KI02，树根缺 `.venv`），
  本轮**两棵树都通过** —— 与集成侧 `e23946c`「KI02 closed」的结论一致，我不再把它列为未判因项
  （仍注明：我未去查它当时的确切失效原因，只确认它现在**两树都绿**且与 U05 无关）。

---

## 7. 新发现（本轮 r3 独立探针）

| # | 级别 | 位置 | 问题 | 证据 |
|---|---|---|---|---|
| **Q-a** | **P3（登记措辞不精确）** | `scripts/scan_tracked_secrets.py:172,186-187`（补丁 13） | `_bound_names` 把 **`ast.Del`（`del x`）、`ast.Global`、`ast.Nonlocal`** 计入"被绑定"。这三类**在语义上并不绑定名字**（`global X` 只是作用域声明、`del X` 是删除），因此一个**从未赋值**过该名字的文件也能把明文凭据放行（D5/D11/D12 实测不报）。README §5.3 的措辞是"与同一文件内某个**已绑定**名字逐字相同"，**严格讲覆盖不到**这三类。影响有限：攻击者需把凭据再写一遍到 `global`/`del` 行，而**那行本身在任何版本都不被任何规则命中**（规则只认 `api_*=` 形态），故不构成 U05 新增的可利用绕过 | `out_probe_p15.txt` D5/D11/D12 |
| **Q-b** | P3（可审计性） | 同上 `main()` | 放宽**只有计数**（`skipped_bare_identifiers=10`），**没有行号级留痕**（r2 复验给过"最低限度给行号"的选项；作者选了"收紧判定"这条更强的路，故可接受）。放行了哪 10 处仍只能人工 grep 复核 | `out/scan_r3_cdtree.txt`；`scan_tracked_secrets.py:410-418` |
| **Q-c** | P3（登记措辞） | 交付 README §5.4 P3-5 行 | "`admit()` 的 `deadline` 是可选参数，**生产默认不传**" —— 实测生产唯一调用点 `src/llm/openai_compatible_client.py:128` **传了** `deadline=deadline`，只是 `_post(deadline: float|None = None)` 的缺省值是 `None`。净效果一致，非错误陈述，但"不传"不准确 | `out/registration_grep2.txt` |
| **Q-d** | P3（登记措辞） | 交付 README §5.4 P3-4 行 | "无第三方持有" —— `SpanHandle` 列在模块 `__all__`（`decision_trace.py:1771`），是对外公开类型。当前仓库内确实无外部构造/持有 | `out/registration_grep.txt` |
| **Q-e** | P3（**既有**，非 U05） | `_SECRET_ASSIGNMENT_RE` 的 `\b` 边界 | **带主键前缀的真实 `.env` 键形态不报**：`BINANCE_API_KEY=<64 位 key>` 在 pristine / r2 / r3 **都不报**（`_`→`A` 之间无词边界，正则根本不匹配）。这恰是本轮威胁模型（"`.env` 内容粘进 `.py`"）最常见的现实形态。**不是回归**（三个版本一致），但建议列为后续任务或补登记 | `out_probe_p15.txt` E1（pristine=[] r2=[] r3=[]） |
| **Q-f** | 观察 | 同正则 | 另有两条**既有**盲区：`api_key: str = <64hex>`（先匹配到 `api_key: str`，值太短）与 `api_key = <base64 含空格/= 补位>`（裸值被空白截断成 <12 字符）。pristine 同样不报 | E7 / E9 |

**无一条属"未登记的 P2 fail-open / 崩溃 / 密钥漏报 / 既有脚本新增失败"。**

---

## 8. 结论

**PASS-with-notes**

| 项 | 判定 |
|---|---|
| **1. P1-5（原阻断项）** | **CLOSED**（任务点名的"未绑定标识符形状"绕过面 D1/D2/D3/D8/D9/D10 **全部报出**；五类必报全报；N5 已修；引号/注释/语法错误不回退；唯一保留面 = README §5.3 + 源码 docstring **已如实登记**的同名绑定残余） |
| **2. 补丁可应用性** | **15/15 成立**（集成工作区等价配置 `core.autocrlf=false` plain，无 override；`--3way` 亦 15/15；"全 CRLF 工作树"组合在集成工作区不可达） |
| **3. 登记补齐** | **已补齐**（README §5.4 四条，位置逐条实测准确、未把未修写成已修；仅两处措辞微瑕 Q-c/Q-d） |
| **4. 集成树 secret scan 18 条** | **全部来自文档**（`docs/dispatch/reviews/**` 14 条 + `docs/无人值守升级任务清单_2026-09-19.json` 4 条），`src/`、`scripts/` **零条**；相对 pristine 只减不增 |
| **5. 全量 delta** | **新增失败 = ∅**（统一清单 79，两树 158 次运行全 rc=0） |
| **6. 回归抽样 ≥5** | **5/5 全 PASS**（P1-1 / P1-2 / P1-3 / P1-4 / 共享绝对期限，共 **60 条断言 PASS / 0 FAIL**） |

**放行依据（独立复现，非采信作者）**：CD 树 `files=397 / registered=3 / skipped=10 / unparsable=0` 逐字一致；
四套集成测试 115/63/48/45 与 `smoke_contracts_v3` 173/0、`test_secret_scanner` 36/0 全部独立复跑一致；
r3 相对 r2 **只有 13/14 内容变化**（逐行净内容比对）；P2-9 双锚（行号 + 内容指纹）在 r3 仍安全。

**残留项级别**：Q-a ~ Q-f **全部为 P3**（其中 Q-e/Q-f 为**既有、非 U05 引入**），
Q-a 是 README §5.3 已登记残余的措辞边界，**无未登记的 P2 及以上残留**。

**若按任务书"任何一条明文凭据漏报即 NOT CLOSED"的字面口径**：D4/D5/D6/D7/D11–D20
（凭据**同时**出现在某绑定位置）仍会被放行 —— 但这正是 README §5.3 明写的保留残余，
也是 r2 复验给出的两条放行路径（"如实登记为已知限制"**或**"只对同文件内被绑定名字放宽"）之一
（作者两条都做了）。故我判 **CLOSED**，并把该边界原文列在 §1.5 供复核者自行裁定。

---

## 9. 未覆盖 / 无法离线验证

1. **未联网、未接真实 LLM**：`_mta.deadline_*` 在真实 Failover 往返中的取值、`Retry-After` 冷却、
   真实供应商额度交互均未验（`MTA_NO_PROXY=1`、沙箱无凭据）。
2. **租约回收只测假 pid**：`LeaseRegistry.owner_alive()` 的 Windows `OpenProcess` 分支未构造真进程被 kill 的场景。
3. **无 pytest**：四套 `tests/integration/*` 用各自 `__main__` 直跑（**与作者、B-r1 同一入口**），
   未用 pytest 收集；未验证是否存在"只有 pytest 才能发现的夹具/收集期问题"。
4. **KI02 未判因**：`smoke_windows_service_plan.py` 本轮两树皆绿，我**未回溯**它在 r2 沙箱失败的确切原因
   （只确认与 U05 无关、现两树均 rc=0）。
5. **U08/U12 消费侧**：原因码/漏斗最终形态、`attempt_observer` 回调元数收敛、共享期限注入均未验（未实现）。
6. **补丁 13/14 的历史回溯扫描**：只验证当前树，未做历史提交扫描。
7. **`.py` 内裸键 JSON（模块层）之外的 YAML/JSON 文件视图**：只验 `.py` 中的内嵌片段，未穷举所有后缀。
8. **行尾场景 3/4/5 未逐条实跑**：我只实跑了"集成工作区等价配置"（场景 2，15/15）与
   `--3way`，未复跑作者矩阵里的 6 种组合（任务只要求集成工作区等价配置）。

---

## 10. 命令清单（可回溯，全部产物只落在 `reviews/U05/R3/r1/_scratch_Q/`）

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"   # PYTHONUTF8=1
R="C:/Users/Tao/Desktop/币安/mat-wt/reviews/U05/R3/r1"; S="$R/_scratch_Q"

# 沙箱
git -C "$R" archive a0bb7a4 | tar -x -C "$S/sb/int_pristine"   # + git init + core.autocrlf=false + add/commit
git -C "$R" archive 6197b83 $U05_PATHS | tar -x -C "$S/sb/int_pristine"  # U05 独占 9 文件
cp -r int_pristine int_patched && cd int_patched && for p in 15 条; do git apply -p1 $p; done
git -C "$R" archive 6197b83 | tar -x -C "$S/sb/cd_tree"        # CD 树

# 可应用性 / 行尾
for f in 15 条补丁; do git -C "$R" apply --check -p1 "$f"; done      > out/apply_check_r121.txt
for f in 15 条补丁; do git apply --check [-p1|--3way] "$f"; done     > out/apply_check_int.txt
git -C "…/mat-wt/I" ls-files --eol <15 个目标文件>                   > out/int_eol.txt   # 只读

# P1-5
"$PY" probe_p15.py                       > out/out_probe_p15.txt          # pristine|r2|r3 三版对照
"$PY" probe_allowance_Q.py               > out/out_allowance_Q.txt        # P2-9 双锚
cd sb/cd_tree   && "$PY" scripts/scan_tracked_secrets.py > "$S/out/scan_r3_cdtree.txt"
cd sb/int_patched && "$PY" scripts/scan_tracked_secrets.py > "$S/out/scan_int_r3.txt"
cd sb/int_pristine && "$PY" scripts/scan_pristine_run.py  > "$S/out/scan_int_pristine.txt"
diff <(sort out/scan_int_pristine.txt) <(sort out/scan_int_r3.txt) > out/scan_int_diff.txt

# 登记
sed -n '955,966p;1040,1050p' src/core/decision_trace.py      > out/registration_check.txt
grep -rn attempt_observer / SpanHandle / admit( src/         > out/registration_grep.txt

# 测试
tests/integration/test_decision_trace.py（115/0）· test_model_route_safety.py（63/0）
tests/integration/test_background_refresh_budget.py（48/0）· test_cross_process_llm_admission.py（45/0）  → out/test_*.log
scripts/smoke_contracts_v3.py（173/0）· scripts/test_secret_scanner.py（36/0）

# 全量 delta + 抽样
"$PY" run_delta_Q.py                     > out/out_delta_Q.txt            # 158 次运行
"$PY" probe_sampling_Q.py                > out/out_sampling_Q.txt
```

> 除只读的 `git -C mat-wt/I ls-files --eol` 外，**未读写 `mat-wt/I`、`mat-wt/CD` 或任何其它 worktree**；
> 未提交、未推送、未打标签、未切分支；未读 `.env`/`data/`/密钥/账户标识；未联网；未启停服务。

---

## 附：早期草稿中的环境事实（保留）

```
worktree r121 = reviews/U05/R3/r1，detached 6197b83
repo .git/config: core.autocrlf = false（全局未设）
15 个补丁目标文件 blob: 6197b83 == 78526d7 == a0bb7a4（14/15 相同；
   scripts/run_offline_regression.py 在 U05 分支侧 DIFF —— 补丁基线是集成 HEAD，属正常）
```

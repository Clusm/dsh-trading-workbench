# U14 修复轮 r3 独立复验（Q 角色）R4

- 复验对象：`ad2c57d`（修复提交；分支 `task/U14`，最终 head `2722efe`）
- 上一轮报告：`mat-wt/I/docs/dispatch/reviews/U14/6ef116b/Q_r3.md`（CHANGES_REQUESTED，唯一阻断项 P1-4）
- 补丁目录：`mat-wt/EF/docs/dispatch/patches/U14/ad2c57d/`（4 条 diff + README，只读引用）
- 我的工作区：`mat-wt/reviews/U14/R4/r1`（独立 git worktree `r127`，detached @ `ad2c57d`，回读确认）
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（3.13.12，`PYTHONUTF8=1`）；无 pytest
- 本报告全部结论来自**本轮独立复现**（自建探针 + 自建前后对照），不采信作者/编排者对照。
  产物：`_scratch_Q/probes/`、`_scratch_Q/logs/`、`_scratch_Q/q_*/`、`_scratch_Q/sb/`、`_scratch_Q/delta/`。

---

## 0. 结论

**PASS-with-notes**

- **P1-4 真闭合**：三条向量（`transport.submit` 抛 `TimeoutError`/`RuntimeError`/`OSError`）+
  空白回执（模块自抛路径）+ `delivered_at_ms` 非数值 + 兜底桶（通道返回 `None`）**全部**满足
  "该条被隔离并退避、其余照常送达、不复发"；`6ef116b` 树（`sb/prev`）在同构造下逐条复现
  "整批中断 + 该行无退避 + 每轮复发"。**反向检查全部通过**：`KeyboardInterrupt`/`SystemExit`
  仍穿出；空白回执不再被当成"已送达"（停在 `SUBMITTED`/`ACCEPTED`，不是 `DELIVERED`）；
  无 `BaseException` 级误吞；`return` 语义未变（`continue`+循环后统一 `return`）。
- **`_isolate_failure`"连记失败都失败"路径成立**：独立用 SQLite 触发器阻断写库，**整批仍不中断**，
  以 `PERSIST_FAILED:<异常类名>` 如实暴露。
- **P3-6 已修**：三类原因的审计 `transition` 标签各自落对（`TRANSPORT_FAILED` /
  `TRANSITION_REJECTED` / `INTERNAL_ERROR`），且 `detail_json` 新增 `reason/error/attempt/backoff_seconds`。
- **P3-7 已修**：04 改尾部锚定后，在 `59660b2`、`fdd208c`、**当前集成 HEAD `cf9c4ae`** 三棵树的
  两种 git 配置下 **24/24 `git apply --check` rc=0**，且**实跑落盘**核对：登记行恰好插入
  `SCRIPTS` 元组尾部、无重复、`git diff --stat` = `2 +`。README §5.2 的 `:81-82` 引用已核准。
- **全量 delta**：`prev`(80) 与 `fix`(80) 均 `80/80 PASS, FAIL=0`，**失败集合差集 = ∅**；
  集成面 `integ_base`(81) 与 `integ_u14`(82) 见 §4。**基线全绿、新增失败 0**。
- **门禁全绿**：打补丁沙箱 `check_quality` / `scan_tracked_secrets` 均 rc=0
  （`files=440 … QUALITY CHECK OK files=264 untracked=0`）。
- **新发现（均为 P3，已由作者登记或本报告首次登记）**：
  1. `delivered_at_ms` 由"裸 `int()` 强转"改成**严格 `isinstance(..., int)`** 后，
     **数值字符串 `"1700000000000"` 与 `float` 也被拒**（`RECEIPT_TYPE_INVALID`）——
     `prev` 树这两种输入是**接受并送达**的。方向 fail-closed，故不是 fail-open，但属
     **超出 P1-4 最小修复面的行为收紧**，README 未点名（§6 新增 P3-8）。
  2. `PERSIST_FAILED` 桶**不进 `errors` 计数**（仅进 `reason_codes`），而作者登记的监控指令
     只点名"监控 `errors`"⇒ 该桶存在**计数盲区**（§6 新增 P3-9）。
  3. `attempt=0` 兜底路径把该行 `attempts` 写回 0，退避不升级（可自愈，影响极小）。

- 标尺：残留项均为 **P3 且已如实/可如实登记**，无未登记的 P2 fail-open / 崩溃 / 整批中断 /
  伪造送达 / 既有脚本新增失败 ⇒ **PASS-with-notes**（不触发 CHANGES_REQUESTED）。

---

## 1. 第 1 项 P1-4（三向量 + 兜底桶 + 反向检查）— **闭合**

探针 `probes/probe_p14.py`（Q 自建）；原始输出 `logs/p14_fix.log`、`logs/p14_prev.log`；
结构化 `q_p14/fix/result.json`、`q_p14/prev/result.json`。
构造：队列 3 条（`bad` 在**最前**，其后 `ok1`/`ok2`），`channel=verified`，
`dispatch_due(now=NOW)` 后再 `dispatch_due(now=NOW+3600s)` 观察复发。

### 1.1 向量一：`transport.submit()` 自身抛异常（最现实的向量）

| 通道抛出的异常 | 树 | `dispatch_due` | `bad` 行 | `ok1`/`ok2` | +3600s |
|---|---|---|---|---|---|
| `TimeoutError` | `sb/prev`(`6ef116b`) | **抛穿** | `attempts=0, next=0, last_error=NULL`（**无退避**） | **`UNKNOWN`（一条都没投递）** | **再次抛穿**（复发） |
| `TimeoutError` | `sb/fix`(`ad2c57d`) | 返回 `sub=2 conf=2 fail=1 rej=0 err=0, rc=['TRANSPORT_EXCEPTION:TimeoutError']` | `attempts=1, next=NOW+30s, last_error='TRANSPORT_EXCEPTION:TimeoutError'` | **两条都 `DELIVERED`** | `sub=0 fail=1`（按退避重试该条，**不中断**） |
| `RuntimeError` | `sb/prev` | **抛穿** | 同上 | `UNKNOWN` | **再次抛穿** |
| `RuntimeError` | `sb/fix` | 返回，`rc=['TRANSPORT_EXCEPTION:RuntimeError']` | 已退避 | `DELIVERED` | 只该条重试 |
| `OSError` | `sb/prev` | **抛穿** | 同上 | `UNKNOWN` | **再次抛穿** |
| `OSError` | `sb/fix` | 返回，`rc=['TRANSPORT_EXCEPTION:OSError']` | 已退避 | `DELIVERED` | 只该条重试 |

`sink.calls`：`prev` = `['bad','bad']`（第二、三条**从未被调用**）；`fix` = `['bad','ok1','ok2','bad']`。

### 1.2 向量二：空白回执 / `delivered_at_ms` 非数值 / 兜底桶

| 畸形输入（通道正常返回） | `prev` 结果 | `fix` 结果 | 判定 |
|---|---|---|---|
| `provider_receipt="   "` | **抛穿** `ValueError: provider_receipt required: 回执不得为空`；`bad` 停在 `SUBMITTED`、`next=NULL`；`ok1/ok2 UNKNOWN` | 返回；`rej=1 rc=['RECEIPT_EMPTY']`；`bad` `SUBMITTED`/`next=NOW+30s`/`last_error='TRANSITION_REJECTED:RECEIPT_EMPTY'`；`ok1/ok2 DELIVERED` | ✅ |
| `device_receipt="   "` | **抛穿** `ValueError: device_receipt required…` | 返回；`RECEIPT_EMPTY`；`bad` 停在 `ACCEPTED`（**非 `DELIVERED`**）；其余送达 | ✅ |
| `delivered_at_ms="not-a-number"` | **抛穿** `ValueError: invalid literal for int()` | 返回；`RECEIPT_TYPE_INVALID`；其余送达 | ✅ |
| `delivered_at_ms=[1,2]` | **抛穿** `TypeError: int() argument must be…` | 返回；`RECEIPT_TYPE_INVALID`；其余送达 | ✅ |
| **通道返回 `None`（兜底桶）** | **抛穿** `AttributeError: 'NoneType' object has no attribute 'ok'` | 返回；**`errors=1`**、`rc=['INTERNAL_ERROR:AttributeError']`；`bad` `next=NOW+30s`/`last_error='INTERNAL_ERROR:AttributeError'`；其余送达 | ✅ |

对照行（`fix`/`prev` 行为一致或更好，无回归）：`provider_receipt=12345`→`RECEIPT_TYPE_INVALID`；
`delivered_at_ms=now+10**12`→`RECEIPT_TIMESTAMP_FUTURE`；`delivered_at_ms=None`+有 `device_receipt`→
`DELIVERED_WITHOUT_DEVICE_RECEIPT`（**不伪造送达**）；`delivered_at_ms=True`：`prev` 是
`TIME_ORDER_VIOLATION`，`fix` 改为 `RECEIPT_TYPE_INVALID`（更准）。

### 1.3 反向检查（**最关键**）

| 检查 | 结果 |
|---|---|
| ① `KeyboardInterrupt` 必须穿出 | ✅ **穿出**（`propagated=true`；队列 3 行**全部 `UNKNOWN`、`attempts=0`**，即无任何隔离写入）。`SystemExit(3)` 同样穿出 |
| ② 空白回执不得被当成"已送达" | ✅ `bad` 停在 `SUBMITTED`/`ACCEPTED`，**从不到 `DELIVERED`**；`rejected=1` 而非 `confirmed` |
| ③ `BaseException` 级是否被误吞 | ✅ 全模块仅 4 处宽捕获，**全部是 `except Exception`**；无 `except BaseException`、无裸 `except:`（`alert_delivery.py:343/1616/1677/1726`；`:343` 是平台 `time.monotonic()` 兜底，非本路径） |
| ④ `return` 语义是否变化 | ✅ 隔离分支一律 `continue`；`for` 循环结束后才 `dict.fromkeys` 去重并 `return result`。实测：`bad` 之后的所有行**都被处理**（`ok1/ok2` 均 `DELIVERED`），无早退 |
| ⑤ 契约保留 | ✅ `AlertReceiptRejected` 仍是 `ValueError` 子类；空回执错误串仍含字段名：`'alert receipt rejected: RECEIPT_EMPTY (provider_receipt required: 回执不得为空)'`（`record_accepted` 直接 API 亦抛同型） |

### 1.4 七行混合批（5 种失败 + 2 条健康条夹在中间）——"不得早退"的判决性证据

`logs/p14c_fix.log`：7 条同批，`a_raise`(通道抛 `OSError`) / `b_blank`(空回执) /
`c_badint`(`delivered_at_ms="NaN"`) / `d_none`(通道返回 `None`) 四条**连续失败在最前**，
其后 `e_ok1`/`f_ok2` 两条健康条，最后 `g_binding`（他消息回执）：

```
RESULT: submitted=5 confirmed=2 failed=1 rejected=3 errors=1
        reason_codes=['TRANSPORT_EXCEPTION:OSError','RECEIPT_EMPTY','RECEIPT_TYPE_INVALID',
                      'INTERNAL_ERROR:AttributeError','RECEIPT_BINDING_MISMATCH']
ROWS: a_raise UNKNOWN/att=1/next=NOW+30s/TRANSPORT_EXCEPTION:OSError
      b_blank SUBMITTED/att=1/next=NOW+30s/TRANSITION_REJECTED:RECEIPT_EMPTY
      c_badint ACCEPTED/att=1/next=NOW+30s/TRANSITION_REJECTED:RECEIPT_TYPE_INVALID
      d_none  UNKNOWN/att=1/next=NOW+30s/INTERNAL_ERROR:AttributeError
      e_ok1   DELIVERED      f_ok2 DELIVERED
      g_binding SUBMITTED/att=1/next=NOW+30s/TRANSITION_REJECTED:RECEIPT_BINDING_MISMATCH
```

⇒ **四种不同失败模式连续命中后，紧随其后的两条健康条仍正常送达**；每条失败行**各自**退避，
异常分类互不混淆。这是"单条异常不得中断整批"最强的一次独立证明。

### 1.5 混批复现（F 原报场景的等价物）

`bad` 在最前 + 3 条正常（`ThrowingSink` 只对 `bad` 抛 `TimeoutError`）：

- `sb/prev`：`dispatch_due` **抛穿**，4 条**全部 `UNKNOWN`**（`ok1/ok2/ok3` 一条都没投递），
  `+1s` 再派发**又抛穿**（`calls_after=['bad','bad']`）。
- `sb/fix`：`submitted=3 confirmed=3 failed=1 errors=0`；`bad` `next=NOW+30s`、
  `ok1/ok2/ok3` 全 `DELIVERED`；`+1s` 再派发 `sub=0 conf=0 fail=0`（**退避窗口内不重派，不复发**）。

---

## 2. 第 2 项 `_isolate_failure`（"连记失败都失败"）— **成立**

探针 `probes/probe_p14.py` §5、`probes/probe_p14b.py`（Q 自建）。**故障注入方式独立**：
在队列库上建 `BEFORE UPDATE … RAISE(ABORT)` 触发器（不是 monkeypatch 方法）。

| 构造 | 结果 |
|---|---|
| 全表 `UPDATE` 被触发器阻断（`bad` 抛 `TimeoutError`，3 条） | `dispatch_due` **返回**（不抛）：`submitted=0 confirmed=0 failed=1 errors=2`，`rc=['PERSIST_FAILED:IntegrityError','TRANSPORT_EXCEPTION:TimeoutError','INTERNAL_ERROR:IntegrityError']`；`+3600s` 再派发 **仍不抛** |
| 只阻断 `bad` 行的 `UPDATE`（`ok1/ok2` 正常） | `submitted=2 confirmed=2 failed=1 errors=0`，`rc=['PERSIST_FAILED:IntegrityError','TRANSPORT_EXCEPTION:TimeoutError']`；`ok1/ok2 DELIVERED`，`bad` `next=0`（写不回去 ⇒ 下轮仍到期） |
| 阻断 `alert_audit` 的 `INSERT`（`_record_failure` 的审计步失败） | 同型：`failed=1 errors=0`，`PERSIST_FAILED:IntegrityError`，其余行照常送达 |

⇒ 作者声称的"`PERSIST_FAILED:OperationalError` 时仍不中断整批"**属实**（我实测到的是
`IntegrityError`，同类持久化故障；结论不变）。**但**退避保证在此前提下失效（`next_attempt_ms`
写不回去）——作者已在 README §7 item 10 **如实登记**（"持久化彻底不可用时无法退避…下一轮仍会到期重试"）。

**盲区（新增 P3-9）**：`_isolate_failure` 把 `PERSIST_FAILED:*` **只追加进 `reason_codes`，
不增加 `errors` 计数**（上表中间一行实测 `errors=0` 而确有失败桶）。作者登记的监控指令
只写"接线时必须监控 `errors` 计数" ⇒ 该桶在 `errors` 上**不可见**，须改为同时监控
`reason_codes` 中的 `PERSIST_FAILED:`。

---

## 3. 第 3 项 P3-6（审计 `transition` 标签与原因一致）— **已修**

探针 `probes/probe_p14.py` §6（直接读 `alert_audit` 表）：

| 场景 | `prev`(`6ef116b`) | `fix`(`ad2c57d`) |
|---|---|---|
| 通道抛 `TimeoutError` | **无审计行**（异常穿出，`_record_failure` 从未执行） | `transition='TRANSPORT_FAILED'`，note `attempt=1 backoff=30.0s error=TRANSPORT_EXCEPTION:TimeoutError` |
| 结构化传输失败 `ok=False, error='TRANSPORT_FAILED'` | `TRANSPORT_FAILED` | `TRANSPORT_FAILED`，`detail_json={'attempt':1,'backoff_seconds':30.0,'error':'TRANSPORT_FAILED','reason':'TRANSPORT_FAILED'}` |
| 回执绑定他消息 | **`TRANSPORT_FAILED`（语义不符 ← P3-6 原报）** | **`TRANSITION_REJECTED`**，error `TRANSITION_REJECTED:RECEIPT_BINDING_MISMATCH` ✅ |
| 空回执拒绝 | **无审计行** | `TRANSITION_REJECTED`，error `TRANSITION_REJECTED:RECEIPT_EMPTY` ✅ |
| 内部兜底异常 | **无审计行** | `INTERNAL_ERROR`，error `INTERNAL_ERROR:ValueError` ✅ |

⇒ 三类原因标签**各自落对**；`detail_json` 从 `{}`（`prev`）升级为结构化
`{reason, error, attempt, backoff_seconds}`。
注：通道异常走的是 `TRANSPORT_FAILED` 标签（`reason_code` 为 `TRANSPORT_EXCEPTION:*`）——
与任务给定的三分类（传输失败 ⇒ `TRANSPORT_FAILED`）一致，属**有意的归类**而非错标。

---

## 4. 第 4/5 项 P3-7 与补丁 apply

### 4.1 P3-7.1 README 行号引用 — **已核准**

独立核验 `sb/fix/scripts/smoke_u14_alert_delivery.py`：
`_ENV_STATE_ROOT_BACKUP` 定义在 **:81**，`MTA_STATE_ROOT`/`MTA_DATA_ROOT` 的 `pop` 在 **:82**。
README §5.2 现写 `:81-82` ⇒ **准确**（r2 的 `:69-70` 确为 `check()` 失败分支打印）。

### 4.2 补丁 01/02/03 — 逐字节未变

`sha256` 对比 `a89eaa5/` 与 `ad2c57d/`：
`01=af9f13b2fcd7225a`、`02=e6d393a088f1fbba`、`03=43fd75f1f243013d` **两包相同**；
`04` 由 `8b3c9293083cdf00` → `53f56de718ee9ac0`（**仅 04 改了锚定**）⇒ README §1 声明属实。

### 4.3 `git apply --check`（提交的 04 = 尾部锚定）

在**仓库根**执行（非子目录，避免静默 no-op），日志 `logs/applycheck.log`：

| 树 | 配置 | 01 | 02 | 03 | 04 |
|---|---|---|---|---|---|
| `59660b2`（补丁基线） | 默认（本机 `core.autocrlf=true`） | OK | OK | OK | **OK** |
| `59660b2` | `-c core.autocrlf=false -c core.eol=lf` | OK | OK | OK | **OK** |
| `fdd208c`（作者声明的集成 HEAD） | 默认 / strict | OK | OK | OK | **OK / OK** |
| **`cf9c4ae`（当前集成 HEAD，I 现已前进到此）** | 默认 / strict | OK | OK | OK | **OK / OK** |

**3 树 × 2 配置 × 4 补丁 = 24/24 rc=0**（作者只声称 16/16；我在**更靠前的集成 HEAD** 上也通过）。

**反"假绿"核对（实跑落盘）**——`logs/apply_real.log`，对每棵树每种配置**真 apply** 后：

- `scripts/run_offline_regression.py` 的 `git diff --stat` = **`2 +`**（1 行注释 + 1 行登记），
  无 CRLF 伪影放大（r3 观测到的"1605 行"未复现）；
- `grep -c smoke_u14_alert_delivery` = **1**（**无重复插入**）；
- 落盘位置：`59660b2` 上落在 **`smoke_u13_supervision.py` 之后**；
  `integ`(`cf9c4ae`) 上落在 **`smoke_u06a_shadow_isolation.py` 之后**——两处都是
  `SCRIPTS` 元组**最后一项**，紧接 `)` / `EXCLUDED_SMOKE_SCRIPTS = {`。✅

⇒ 04 的"尾部锚定"在同一份 patch 上跨**三个**不同集成状态都正确，P3-7.2 的适用范围问题**已修**。

### 4.4 全量回归 delta（强制项）

harness：`probes/harness_q4.py`（**Q 自建**，非 fail-fast）。它用 `ast` 读被侧树的
`SCRIPTS` 清单，逐行复制官方 `main()` 的 env 构造（`EXECUTION_MODE=paper`、`pop MTA_PROFILE`、
`MTA_NO_PROXY=1`、UTF-8、按名清 `*KEY*/*SECRET*/*TOKEN*`），并**对两侧统一**额外
`env.pop("MTA_STATE_ROOT"/"MTA_DATA_ROOT")`，逐脚本 `subprocess.run` 记返回码。
两棵同批并行、逐条不 fail-fast。

**Delta A（修复面，任务点名的 before/after）**

| 树 | 构成 | 结果 |
|---|---|---|
| `sb/prev` | `git archive 6ef116b` + `a89eaa5` 01–04 | `TOTAL=80 PASS=80 FAIL=0`，`failed set: []`，199.8 s |
| `sb/fix` | `git archive ad2c57d` + `ad2c57d` 01–04 | `TOTAL=80 PASS=80 FAIL=0`，`failed set: []`，199.6 s |

**失败集合差集 = ∅**（`fail→pass` 与 `pass→fail` 都空）；两轮**均未**出现 `smoke_llm_deadline` 抖动。
原始输出 `logs/delta_prev.log` / `logs/delta_fix.log`，结构化 `delta/prev.json` / `delta/fix.json`。

**Delta B（集成面，回应"基线现在应全绿（scripts=81）"）**

| 树 | 构成 | 脚本数 | 结果 |
|---|---|---|---|
| `sb/integ_base` | `git archive cf9c4ae`（当前集成 HEAD，未含 U14） | **81** | 见下 |
| `sb/integ_u14` | `cf9c4ae` + U14 三个独占文件/夹具 + 01–04 | 82 | 见下 |

> 注：`task/U14` 分支的 `SCRIPTS` 清单是 **80**（其基线 `59660b2` 尚无 U02/U06a 登记行）；
> **81 是当前集成 HEAD 的数**。两者均已覆盖，结论互不冲突。

**Delta B 结果**（`logs/delta_integ_base.log` / `logs/delta_integ_u14.log`，
结构化 `delta/integ_base.json` / `delta/integ_u14.json`）：

| 树 | 脚本数 | 结果 | 失败集合 |
|---|---|---|---|
| `sb/integ_base`（当前集成 HEAD `cf9c4ae`） | **81** | `81/81 PASS, FAIL=0`，195.2 s | `[]` — **基线全绿** ✅ |
| `sb/integ_u14`（`cf9c4ae` + U14 全部落地 + 01–04） | 82 | `82/82 PASS, FAIL=0`，198.1 s | `[]` |

**两两失败集合差集全为 ∅**：

| 对照 | `fail→pass` | `pass→fail` |
|---|---|---|
| Delta A：`prev` ↔ `fix` | ∅ | ∅ |
| Delta B：`integ_base` ↔ `integ_u14` | ∅ | ∅ |
| 交叉：`prev` ↔ `integ_base` | ∅ | ∅ |

⇒ **基线全绿、新增失败 0，四棵树零失败**。两批各 2 棵并行（≈2× 负载），
`smoke_llm_deadline` 四轮**均 rc=0**（未复现 r1 的抖动）。
`smoke_u02_strategy_position_manager.py` 输出中的 `FAIL` 行是其**自带负向对照**（exit 0），
我的 harness 只按返回码判失败，未误报。

### 4.6 门禁（打补丁沙箱）

| 脚本 | 树 | 结果 |
|---|---|---|
| `scripts/check_quality.py` | `sb/fix` | **rc=0** — `DEPENDENCY LOCK OK packages=50`；`SECRET SCAN OK files=440 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0`；`QUALITY CHECK OK files=264 untracked=0` |
| `scripts/scan_tracked_secrets.py` | `sb/fix` | **rc=0** — `SECRET SCAN OK files=440 …` |
| `scripts/check_quality.py` | `sb/prev`（对照） | **rc=0** — 同数值（`files=440` / `264 / untracked=0`） |

**两门禁全绿**，与 r3 数值逐字一致（无新增未跟踪/未登记文件）。
原始输出 `logs/gate_quality_fix.log`、`logs/gate_secrets_fix.log`、`logs/gate_quality_prev.log`。

---

## 5. 第 7 项 回归抽样（≥5，Q 自建 `probes/probe_sample.py`）

6 主题全部独立复现（原始输出 `logs/sample_fix.log`、结构化 `q_sample/fix/result.json`）：

| # | 主题 | 独立构造 | 实测 |
|---|---|---|---|
| 1 | **三级阶梯（R14）** | 仅 HTTP200；跳过 `provider_receipt` 直接 `record_delivered`；再走正规三级 | `http200_only→SUBMITTED`；跳过 → `AlertLadderViolation: ('TRANSITION_REJECTED:DELIVERED:DELIVERED requires provider_receipt',)` 且行**停在 `SUBMITTED`**（未落库）；正规路径 → `DELIVERED` |
| 2 | **两成因分开（R13）** | 8 组 | `PROCESS_MISSING`(uptime≫age) / `HOST_DOWN`(uptime≪age) / `HOST_CONTINUITY_UNKNOWN`(uptime 不可得) / `TRADING_STALLED`(存活+age=121s) / `HEALTHY`(**age=120 边界不陈旧**) / 存活但无证据 → `TRADING_STALLED`+`TRADING_EVIDENCE_MISSING`（**绝不判健康**） / `process_alive=None` → 追加 `PROCESS_UNVERIFIED` / 负 uptime → `HOST_CONTINUITY_UNKNOWN` |
| 3 | **`OPS_BLOCKED` 不伪造送达** | 未验证通道派发；`drill=True` | 未验证：`submitted=0 skipped=1 blocked=True`、状态留 `UNKNOWN`、`OPS_BLOCKED`；drill：`submitted=1` 但状态停在 `SUBMITTED`、`delivered_at_ms=NULL`、`OPS_BLOCKED` |
| 4 | **心跳带交易证据时间** | 9 组 + 1 边界 | `ok`→`HEARTBEAT_OK`(accepted/healthy)；缺证据→`TRADING_EVIDENCE_MISSING`；证据未来→`TRADING_EVIDENCE_FROM_FUTURE`（拒）；证据回退→`TRADING_EVIDENCE_REPLAY`；证据过旧→`…REPLAY+…STALE`；`emitted` 回退→`HEARTBEAT_REPLAY_OUT_OF_ORDER`；序号持平→`HEARTBEAT_SEQUENCE_REPLAY`；**无界**维护窗口→`MAINTENANCE_WINDOW_UNBOUNDED`（`accepted=False`）；**有界**窗口→`muted=True, accepted=True, healthy=False`；**age 恰好 120 s → 不陈旧、healthy** |
| 5 | **队列独立于交易库** | 交易目录快照；容量 50 灌 200；队列文件损坏 | 交易目录快照**不变**；`count=50, dropped=150`；损坏队列 → `sqlite3.DatabaseError: file is not a database`（响亮失败）且交易目录**仍不变** |
| 6 | **人工确认可审计** | 未提交先确认 / 缺 note / `confirm=False` / `actor="  AUTO "` / 合法 / 重复 | 未提交 → `MANUAL_ACK_WITHOUT_SUBMISSION`（**只写审计不改状态**，state 仍 `UNKNOWN`）；缺 note 与 `confirm=False` → `MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED`（**常量名==值**）；`"  AUTO "` → `MANUAL_ACK_ACTOR_INVALID`；合法 → `ACKNOWLEDGED` 且 `delivered_at_ms=NULL`/`provider_receipt=NULL`（**不伪造送达**），审计含 `(ACKNOWLEDGED, human-op, 'end-to-end drill ok')`；重复 → 幂等 `(True, [])` |

---

## 6. 问题清单

### 【新增 P3-8】`delivered_at_ms` 类型收紧超出 P1-4 最小修复面（数值字符串 / float 由"送达"变"拒绝"）

- 位置：`src/core/ops/alert_delivery.py:1642-1644`（`if not isinstance(outcome.delivered_at_ms, int) or isinstance(..., bool): raise AlertReceiptRejected((RECEIPT_TYPE_INVALID,))`）
- 实测（`probes/probe_p14.py` §2）：

| 输入 | `prev`(`6ef116b`) | `fix`(`ad2c57d`) |
|---|---|---|
| `delivered_at_ms="1700000000000"`（数值字符串） | **`DELIVERED`**（`int()` 强转成功，`confirmed=3`） | **`RECEIPT_TYPE_INVALID`**（拒绝） |
| `delivered_at_ms=1700000000000.0`（float） | **`DELIVERED`** | **`RECEIPT_TYPE_INVALID`**（拒绝） |
| `delivered_at_ms=True` | `TIME_ORDER_VIOLATION` | `RECEIPT_TYPE_INVALID`（更准 ✅） |

- 评估：方向是 **fail-closed**（宁拒不收，不会把畸形输入当送达），**不构成 fail-open**；
  但真实网关以 JSON 传 `delivered_at_ms` 时，某些 SDK/序列化会把整数还原为 `float`
  （`1700000000000.0`），届时**合法回执会被拒并进入退避**——这是 U12 接线时需知晓的
  行为面。README 未点名此收紧（§7 item 10 只谈了兜底桶/持久化/BaseException）。
  **建议**：接线时在通道适配层统一 `int()` 归一为 `int`，或在 README 显式登记该收紧。
  定级 **P3**（对既有离线行为无影响：`tests/`+`smoke` 全绿；无生产消费者）。

### 【新增 P3-9】`PERSIST_FAILED` 不入 `errors` 计数，而登记的监控指令只点名 `errors`

- 位置：`src/core/ops/alert_delivery.py:1723-1727`（`_isolate_failure` 仅
  `result["reason_codes"].append(f"{PERSIST_FAILED}:{type(exc).__name__}")`，**不** `result["errors"] += 1`）
- 实测（`probes/probe_p14b.py` §A/§C）：只阻断 `bad` 行的 `UPDATE` 时返回结构为
  `{submitted:2, confirmed:2, failed:1, errors:0, reason_codes:['PERSIST_FAILED:IntegrityError','TRANSPORT_EXCEPTION:TimeoutError']}`
  ⇒ **`errors` 为 0 但确有"失败桶"发生**。
- 评估：失败**被如实暴露**（在 `reason_codes` 里，且 README §7 item 10 已写明
  "`reason_codes` 里会出现 `PERSIST_FAILED:<异常类名>`"）⇒ **不是 fail-open、不是静默**。
  但作者给出的**监控指令**（"接线时必须监控 `errors` 计数"）对**这一个桶不成立**，
  存在计数盲区。**建议**：把登记改写为"监控 `errors` **及** `reason_codes` 中的
  `INTERNAL_ERROR:` / `PERSIST_FAILED:`"。定级 **P3**。

### 【观察，不列 P 级】`attempt=0` 兜底路径使 `attempts` 写回 0、退避不升级

`dispatch_due:1604` 把 `attempt` 预置为 0；若 `int(row["attempts"])` 本身抛（行数据损坏），
兜底分支以 `attempt=0` 调 `_record_failure` ⇒ `attempts` 被写回 **0**、退避恒为 base（30 s）。
实测（`probes/probe_p14b.py` §B）该行**下一轮自愈**（`attempts` 变整数 0 后正常升级
30→30→60→120 s）。影响极小，且属"数据已被损坏"的前提；不列 P 级。

### 【评估】作者对"兜底桶"的登记是否足够

README §7 item 10 登记四条：兜底桶掩盖新 bug（→ 监控 `errors`）、持久化不可用时无法退避、
仅覆盖 `Exception`（`BaseException` 不被吞）、返回结构新增 `errors` 键（加法，旧消费方不受影响）。
**独立核对结论：四条陈述与实现逐条相符**（本报告 §1.3/§2 实测）。**唯一不足**是 P3-9 的
计数盲区。总体**登记充分**（诚实、可操作），补上 P3-9 一行即可。

---

## 7. 未覆盖 / 无法离线验证（明说）

1. **真实通道送达**：`verified` 通道只存在于内存 fake sink / `RecordingSink`；真实回执、
   真实升级路径、宿主断电/失联演练属 **U12 OPS（D02）**。我未也无法验证"真的能联系到人"。
2. **`AlertQueue` 无生产消费者（P2-3，已登记）**：`AlertQueue(` 的构造点全部在
   验收 `tests/acceptance/test_alert_delivery.py`、`scripts/smoke_u14_alert_delivery.py`
   与 README。**不得因本包测试全绿就认为"无人值守外部告警送达"已可用**；P1-4 修复的正是
   **真实 transport 接入后**才会命中的输入面，U12 接线必须带故障注入复验。
3. **`host_uptime_seconds` 的平台性质**：Linux 读 `/proc/uptime`，其他平台退回
   `time.monotonic()`；离线只用注入值，未在真实宿主做断电演练（README §7.2 已登记）。
4. **多进程并发写队列 / 写阻塞压力模型**：未做（README §7.5 已登记）。
5. **`_record_failure` 直接单元（不由 `_isolate_failure` 调用）**：无外部调用点，
   我只验了"经 `dispatch_due` 隔离单元"的路径。
6. **NumPy/pandas 等重型依赖交互**：本任务无关，未跑。

---

## 8. 命令清单（可复现）

```bash
MW="C:/Users/Tao/Desktop/币安/mat-wt"; SC="$MW/reviews/U14/R4/r1/_scratch_Q"
PY="$MW/../multi-agent-trader/.venv/Scripts/python.exe"; export PYTHONUTF8=1

# 沙箱（提交树 + 补丁；apply --check 与实跑落盘）
git archive 59660b2|tar -x -C $SC/sb/base59660b2 ; git archive fdd208c|tar -x -C $SC/sb/basefdd208c
git archive cf9c4ae|tar -x -C $SC/sb/integ       ; git archive 6ef116b|tar -x -C $SC/sb/prev
git archive ad2c57d|tar -x -C $SC/sb/fix
for t in fix prev base59660b2 basefdd208c integ; do (cd $SC/sb/$t && git init -q . && git add -A \
  && git -c user.email=q@l -c user.name=q commit -qm base); done
# 24/24 apply --check（三树×两配置×4 补丁）——见 logs/applycheck.log
# 真 apply 落盘核对 —— 见 logs/apply_real.log
for p in 01_core_health_watch 02_scripts_health_watch 03_audit_database_alert_deliveries \
         04_run_offline_regression_registration; do
  git -C $SC/sb/fix  -c core.autocrlf=false -c core.eol=lf apply "$SC/patches/ad2c57d/$p.diff"
  git -C $SC/sb/prev -c core.autocrlf=false -c core.eol=lf apply "$SC/patches/a89eaa5/$p.diff"
done

# 探针
$PY $SC/probes/probe_p14.py    $SC/sb/fix  $SC/q_p14/fix    # P1-4 三向量+兜底桶+反向检查+P3-6
$PY $SC/probes/probe_p14.py    $SC/sb/prev $SC/q_p14/prev
$PY $SC/probes/probe_p14b.py   $SC/sb/fix  $SC/q_p14b/fix   # PERSIST_FAILED / errors 盲区
$PY $SC/probes/probe_sample.py $SC/sb/fix  $SC/q_sample/fix # 6 主题抽样

# 全量 delta（同 harness 清单+env，非 fail-fast；两批并行）
bash $SC/probes/run_delta.sh     # prev/fix 与 integ_base/integ_u14，输出 logs/delta_*.log

# 门禁
(cd $SC/sb/fix && $PY scripts/check_quality.py)      # logs/gate_quality_fix.log
(cd $SC/sb/fix && $PY scripts/scan_tracked_secrets.py) # logs/gate_secrets_fix.log
```

---

## 9. 标尺自评

- 无未登记的 **P2 fail-open / 崩溃 / 整批中断 / 伪造送达**；既有脚本**新增失败 0**。
- 残留项为 **P3-8（类型收紧，fail-closed）**、**P3-9（`errors` 计数盲区）**与两条观察项，
  均可如实登记 ⇒ **PASS-with-notes**。
- 与 r3 的差别：r3 判 CHANGES_REQUESTED 的唯一阻断项 **P1-4 已真闭合**（前后对照 + 反向检查全通过）。

---

## 10. Delta 汇总表（四棵树，全部 rc=0）

| 树 | 构成 | 脚本数 | PASS | FAIL | 失败集合 | 耗时 |
|---|---|---|---|---|---|---|
| `sb/prev` | `6ef116b` + `a89eaa5` 01–04 | 80 | 80 | 0 | `[]` | 199.8 s |
| `sb/fix` | `ad2c57d` + `ad2c57d` 01–04 | 80 | 80 | 0 | `[]` | 199.6 s |
| `sb/integ_base` | `cf9c4ae`（当前集成 HEAD） | **81** | **81** | 0 | `[]` | 195.2 s |
| `sb/integ_u14` | `cf9c4ae` + U14 全落地 + 01–04 | 82 | 82 | 0 | `[]` | 198.1 s |

三组对照的失败集合差集（双向）**全为 ∅** ⇒ **基线全绿、新增失败 0**。

## 11. 复验结论摘要（供派单方）

- **P1-4 真闭合**（三向量 + 空白回执 + 非数值时间戳 + 兜底桶），
  且 `KeyboardInterrupt`/`SystemExit` 仍穿出、空白回执**不**被当成送达、无 `BaseException` 误吞、
  `return` 语义未变。七行混合批（4 连败 + 2 健康 + 1 拒绝）证明**无早退**。
- **`_isolate_failure` 成立**（触发器注入持久化故障，整批不中断，`PERSIST_FAILED:*` 如实暴露）。
- **P3-6 已修**（三类标签各自落对 + `detail_json` 结构化）。
- **P3-7 已修**（04 尾部锚定：三树 × 两配置 **24/24** `apply --check` rc=0，实跑落盘无重复、
  `diff --stat` = `2 +`；README `:81-82` 核准）。
- **门禁全绿**（`check_quality` / `scan_tracked_secrets` 均 rc=0，`files=440`/`untracked=0`）。
- **新发现**：P3-8（`delivered_at_ms` 类型收紧：数值字符串/float 由"送达"变"拒绝"，fail-closed）、
  P3-9（`PERSIST_FAILED` 不入 `errors`，登记的监控指令有计数盲区）；另有 1 条不列级的观察项。
- **结论：PASS-with-notes**。


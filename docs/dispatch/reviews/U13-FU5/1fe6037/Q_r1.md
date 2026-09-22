# U13-FU5 独立验收报告 (Q 角色, R1)

工作树: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU5\R1\q` @ `1fe6037` (detached)
base: `1d737b5` (pristine) / head: `1fe6037` (patched)
解释器: `C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe` (无 pytest)

---

## 0. 结论

**PASS-with-notes**

- **FU5 本体修复完全成立，且可复现。** 用 Q 自写探针（真实存活 writer 子进程 +
  锁截断为 0 字节）两态对照：修前 `check_writer_stopped` **不抛**、`create_epoch_bundle`
  **产出包**、manifest 烙上 `writer_stopped: True`、`verify_epoch_bundle` **接受**
  （写者全程存活）；修后 `RECOVERY_LOCK_UNREADABLE`、**不产出任何包**、写者未被侵扰。
- **四情形逐条实测正确，且合法路径未被修坏**：①锁不存在→**仍允许**（实测包产出+verify 通过）；
  ③可解析+pid 已死→**仍允许**（实测包产出+verify 通过）；②可解析+pid 存活→拒绝且**文案逐字不变**；
  ④六形态（空/截断/坏 JSON/顶层非对象/非 UTF-8/无 pid/pid=0；实际 7 形态）**全部**被拒。
- **`read_lock_holder` 抽取逐字等价**：整段实现体从 `_read_holder` 原样搬移，
  仅 `self.path`→`path`（+ 一行 `path = Path(lock_path)`）；`except Exception` 仍不吞
  `KeyboardInterrupt`/`SystemExit`；FU4 验收套件修后**仍全绿**。
- **`scripts/create_recovery_bundle.py` 未绕过检查**：它走 `guard.acquire()`（**真持锁**，
  比只读判定更强），`except LockUnreadableError: raise` 是**控制流**改动但**不放宽**
  （两条分支都 raise，只改异常类型/文案）。
- 回归零新增失败、`check_quality` 未新增 exclude、补丁矩阵 12/12 `Checking patch` 0 Skipped。
- **残余（非阻塞，见 §3）**：`check_writer_stopped` **仍是只读判定**，锁不存在时放行后
  写者仍可能在取快照期间启动 ⇒ 仍可能产出撕裂包。但**它不再把"读不出"谎报成"已停"**，
  残余属"窗口很窄的普通一致性风险"，**不构成**上一轮 P0 定义的"被声明为同一时刻静止"的
  谎言。**无阻塞项。**

**Notes（不阻塞，但需登记）**
- **N1**：作者声称 `CHANGED=[]`，按**严格输出比对**不可复现 —— 我测得
  `CHANGED=[smoke_u10a_release_package.py（仅 release_id）, smoke_u13_supervision.py（新增 [5]/[5b] 段）]`；
  两项 rc 均 0、差异均为预期内，属口径差异而非缺陷（详见 §6.1）。
- **N2**：补丁包 `docs/dispatch/patches/U13-FU5/1fe6037/`（4 diff + README + 16 evidence）
  **未被 `1fe6037` 跟踪**，只存在于作者 worktree 的未跟踪文件；内容我按作者 worktree 原件
  验证通过（§7），但**无法证明与"最终交付件"逐字节同一**（详见 §8-1）。

---

## 1. 前后对照实测（真实存活 writer + 损坏锁）★

探针（Q 自写，未复用作者 `.py.txt`）：`_scratch_Q/_probe/holder.py`（真实子进程持锁保活）、
`_scratch_Q/_probe/scenario.py`、`_scratch_Q/_probe/run_all.py`。
日志 `_scratch_Q/scenarios.log`，原始结果 `_scratch_Q/scenario_results.json`。

命令：
```
export PYTHONUTF8=1
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
"$PY" _scratch_Q/_probe/run_all.py          # exit 0
# 单算例: "$PY" _scratch_Q/_probe/scenario.py <tree> corrupt_live
```
场景 `corrupt_live`：起**真实存活** writer 子进程（`InstanceLock.acquire()` 后保活）→
先确认基线为 `RECOVERY_WRITER_RUNNING` → **整文件截断为 0 字节** → 调
`check_writer_stopped` + `create_epoch_bundle` + `verify_epoch_bundle`。

| 事实 | pristine (`1d737b5`) | patched (`1fe6037`) |
|---|---|---|
| 基线（锁完好） | `RECOVERY_WRITER_RUNNING` | `RECOVERY_WRITER_RUNNING` |
| `check_writer_stopped` | **不抛**（`check_writer_stopped_raised=False`） | **`RECOVERY_LOCK_UNREADABLE`** |
| `bundle_created` | **True** | **False** |
| `manifest_writer_stopped` | **True**（虚假一致性声明） | —（未产出包） |
| `verify_epoch_bundle_accepted` | **True**（`verified_files=5`） | —（未产出包） |
| `published_bundles` | `['recovery_...']` | **`[]`** |
| `writer_alive_at_end` | True | True |

与作者声称表**逐格一致**。命令实测 exit code：runner 0；两个 `corrupt_live` 子进程均 0
（探针自身不崩）。**结论：上一轮 P0 的核心（"读不出被谎报成已停"）确已闭合。**

---

## 2. 四情形对照表 ★

全部由 `run_all.py` 实测（每算例独立子进程，`scenario_results.json`）。

| # | 情形 | pristine 判定 | pristine 建包 | **patched 判定** | **patched 建包** | 结论 |
|---|---|---|---|---|---|---|
| ① | **锁文件不存在** | 不抛 | **True**（verify 接受） | 不抛 | **True**（verify 接受） | **仍允许 ✓ 未修坏** |
| ② | 可解析 + pid **存活** | `RECOVERY_WRITER_RUNNING` | False | `RECOVERY_WRITER_RUNNING` | False | 拒绝 ✓ |
| ③ | 可解析 + pid **已死**（真实退出子进程） | 不抛 | **True**（verify 接受） | 不抛 | **True**（verify 接受） | **仍允许 ✓ 未修坏** |
| ④ | 存在但不可解析（7 形态） | 见下 | 5/7 **True** | `RECOVERY_LOCK_UNREADABLE` | **全 False** | 全部拒绝 ✓ |

### ①③ 仍允许的实测证据（重点，防"把合法入口修坏"）
```
[pristine] no_lock   cws=""  bundle=True   verify_accepted=True  published=['recovery_...']
[patched]  no_lock   cws=""  bundle=True   verify_accepted=True  published=['recovery_...']
[pristine] dead_pid  cws=""  bundle=True   verify_accepted=True  published=['recovery_...']
[patched]  dead_pid  cws=""  bundle=True   verify_accepted=True  published=['recovery_...']
```
- ①：`lock_exists=False`，两态均产出包且 `verify_epoch_bundle` 接受（`verified_files=5`）。
- ③：先 `Popen(sys.exit(0)); wait()` 得到确定已死 pid（`dead_pid_alive=False`），
  写入锁后两态均产出包并被 verify 接受。**灾难恢复的两条正常入口都完好。**

### ② 文案逐字不变（实测）
```
pristine: RECOVERY_WRITER_RUNNING: 交易服务仍在运行（PID=17140，锁文件=trading_service.lock）；请先正常停机，再创建同 epoch 恢复包
patched : RECOVERY_WRITER_RUNNING: 交易服务仍在运行（PID=3596，锁文件=trading_service.lock）；请先正常停机，再创建同 epoch 恢复包
```
除 PID 外**逐字相同**（与 `1d737b5` 源码字面量比对亦一致）。

### ④ 七形态明细
| 形态 | pristine code | pristine 建包 | patched code | patched 建包 |
|---|---|---|---|---|
| empty (0 字节) | （不抛） | **True** | `RECOVERY_LOCK_UNREADABLE` | **False** |
| truncated | （不抛） | **True** | `RECOVERY_LOCK_UNREADABLE` | **False** |
| bad_json | （不抛） | **True** | `RECOVERY_LOCK_UNREADABLE` | **False** |
| non_object (`[1,2,3]`) | `AttributeError` | False | `RECOVERY_LOCK_UNREADABLE` | **False** |
| non_utf8 | `UnicodeDecodeError` | False | `RECOVERY_LOCK_UNREADABLE` | **False** |
| no_pid | （不抛） | **True** | `RECOVERY_LOCK_UNREADABLE` | **False** |
| pid_zero | （不抛） | **True** | `RECOVERY_LOCK_UNREADABLE` | **False** |

- pristine 有 **5/7 fail-open**（产出 `writer_stopped: True` 的包且 verify 接受）；
  另 2 形态（non_object / non_utf8）虽也拒包，但抛的是**裸未受控异常**
  （`AttributeError` / `UnicodeDecodeError`），说明"未修前的拒绝"是碰巧而非设计。
- patched 7/7 全部收敛为**可区分**原因码 `RECOVERY_LOCK_UNREADABLE`、全部不产出包。
- 原因串抽样：`pid 不是有效进程号（0）` / `锁文件无法读取：UnicodeDecodeError: 'utf-8'...`
  / `锁文件内容不可解析（9 字节，UTF-8）：ValueError...`，且异常正文含"先确认服务是否
  正在运行"+Windows/POSIX 删锁命令（可行动）。

---

## 3. TOCTOU 独立严重度判定 ★

### 我构造的复现（修后树上）
探针 `_scratch_Q/_probe/toctou.py`（日志 `_scratch_Q/toctou.log`）。做法：把
`recovery.check_writer_stopped` 包一层 —— **真实调用原函数**（锁不存在 ⇒ 通过），
**紧接其后**起一个真实 writer 子进程拿锁，再让 `create_epoch_bundle` 往下走。

```
lock_absent_before=true → writer_started_after_check=true
lock_exists_during_snapshot=true
bundle_created=true   manifest_writer_stopped=true
writer_alive_during_snapshot=true   lock_present_while_bundle_exists=true
verify_accepted=true  verified_files=5
```
**即：修后仍可能产出一份 `writer_stopped: True` 的包，而取快照时写者活着、锁也在。**

### 但它是否仍属"谎报静止"（P0 核心）？—— **否，判定为普通一致性残余，非阻塞**

| 维度 | 原 P0（已修） | 本条残余 |
|---|---|---|
| 触发 | **静态输入**：锁读不出 ⇒ 必然误判 | **时序**：写者须在"检查后"窗口内启动 |
| 命中率 | 每次必中（确定性） | 需真实并发启动，窗口 = 单次只读检查→快照完成 |
| 机制 | 把"读不出"**当成结论** | check-then-act（TOCTOU），结论当时为真 |
| 后果 | 伪造停机证据 | **撕裂快照**（数据不一致） |
| 是否双持有/重复下单 | 原 FU4 是；FU5 本条否 | **否**（仅一个写者） |
| 生产入口 | 库 API + 脚本都中 | **仅库 API**；脚本已免疫（见下） |

**决定性证据 —— 生产入口 `scripts/create_recovery_bundle.py` 全窗持锁：**
把 `_copy_verified_json` 包一层，在整个复制循环内实测锁状态：
```
lock_present_during_copy_calls: [True, True, True, True]
lock_pid_while_copying: [24376, 24376, 24376, 24376]   our_pid: 24376
lock_released_after: True ; manifest writer_stopped: True
```
⇒ 脚本在**复制全程**真持锁（pid=自身），写者无法在窗口内启动。该入口的 TOCTOU
**已闭合**；残余只存在于**直接调用库 API** `core.ops.recovery.create_epoch_bundle`
的使用者（`recovery.py` 的 docstring 也明说"调用方必须保证交易服务已停机"）。

**结论**：上一轮 P0 的核心是"**把读不出谎报成已停**" —— 这条已**确定闭合**（§1/§2 实测）。
残余的只读检查窗口属于**另一种、更弱**的失效模式（TOCTOU），且被产品真实入口持锁所规避，
后果是撕裂包而非重复下单，恢复端另有 `RECONCILE_REQUIRED` 对账门。
**我不认为它构成"被声明为同一时刻静止"的未闭合谎报**，不判阻塞。
建议（非阻塞）：让 `create_epoch_bundle` 自身 `acquire()` 锁或显式要求调用方持锁并在
docstring 里写成前置条件/断言（现仅自然语言描述"调用方必须保证……"）。

---

## 4. `read_lock_holder` 抽取逐 hunk 审查

命令：
```
git diff 1d737b5..1fe6037 -- src/core/instance_lock.py     # 逐 hunk 审
```
该文件 diff 只有 2 个 hunk：①在 `LockUnreadableError` 之后**新增** `read_lock_holder`
模块级函数（+45 行）；②`_read_holder` 方法体**删除**、改一行转发 `return read_lock_holder(self.path)`。

**AST 抽取 + 归一化逐行比对**（`self.path` ↔ `path`，去掉 def 行与新增的
`path = Path(lock_path)` 行）：
```
normalized old lines: 25   normalized new lines: 25
DIFF LINES: 0
AST body equivalence: True
```
⇒ 实现体**逐行等价**，搬迁未改语义。逐条确认：
- 返回 `None` 条件：`except FileNotFoundError: return None` —— 相同。
- 抛 `LockUnreadableError` 条件：`(OSError, UnicodeDecodeError)`；`json.loads`/
  `isinstance(data, dict)`/`pid` 类型校验/`int(raw_pid)`/`pid <= 0` —— 相同。
- **不吞 `KeyboardInterrupt`/`SystemExit`**：内层是 `except Exception`（非
  `BaseException`）—— 相同（`except OSError`/`except (OSError, UnicodeDecodeError)`
  也不捕 BaseException）。✓
- 报错文本（`锁文件无法读取…` / `锁文件内容不可解析（{len(raw)} 字节，UTF-8）…` /
  `锁文件里的 pid 不是有效进程号（{pid}）`）—— 相同。
- `_read_holder` 转发后**返回类型/异常语义不变**（`Path(self.path)` 对 Path 输入幂等）。

**FU4 验收修后仍全绿（两条独立证据）**：
```
# (a) patched 树 + patched 测试（含 FU5 扩展）
$PY tests/acceptance/test_instance_lock_failclosed.py   → exit 0, 89 PASS
    ...U13-FU4/FU5 LOCK + RECOVERY FAIL-CLOSED OK
# (b) 把 1d737b5 的原版 FU4 测试（未改一字）注入 patched 树运行
$PY <fu4_original_test>.py                              → exit 0, 42 PASS
    ...U13-FU4 INSTANCE LOCK FAIL-CLOSED OK
```
(b) 是**最强证据**：FU4 的原始验收里 **[1]–[4]** 节（AST/源码比对也已确认这 4 节
的函数体不在本 diff 的 hunk 内）在**新代码**上仍全绿 ⇒ FU4 语义/幂等/符号不变。
[1]–[4] 与 [5]/[5b] 节在 patched 测试中均出现（日志 `fu5_test_patched.log`）。

---

## 5. `scripts/create_recovery_bundle.py` 调用链 / 控制流核对

**调用链（实读代码 + 动态实测）**：
```
create_recovery_bundle()            # scripts/create_recovery_bundle.py:73
  → guard = InstanceLock(paths.lock_file)   # :89
  → guard.acquire()                          # :91  ← 真持锁（O_EXCL 建锁 + 写 pid）
  → _create_locked_bundle(...)               # :102（复制 DB/JSON/写 manifest）
  → finally: guard.release()                 # :104
```
动态证据（§3）：复制循环 4 次回调中锁**始终存在且 pid=自身**；返回后锁被释放。
manifest 里 `writer_stopped: True`（:186）是在**持锁期间**写出的 ⇒ 该声明不是只读
判定，而是"我正握着这把锁"的强声明。**未绕过检查，且比
`check_writer_stopped` 的只读判定更强** —— 作者此说属实。

**`except LockUnreadableError: raise`（:92-98）是纯文案还是改了控制流？**
- 是**控制流**改动：`LockUnreadableError` 继承 `RuntimeError`，修前会落入
  `except RuntimeError`（:99）被**包装**成 `RuntimeError("交易服务仍在运行；请先正常停机……")`；
  修后先被捕获并**原样上抛**，不再包装。
- **但没放宽**：两条分支**都是 raise**，脚本在两种情况下都失败、都不产出包。
  实测两态（0 字节损坏锁 + 合法状态根）：
```
[tree_pristine] REJECTED RuntimeError: 交易服务仍在运行；请先正常停机，再创建一致恢复包
                lock_still_exists=True  bundles=[]
[tree_patched]  REJECTED LockUnreadableError: 单实例锁文件存在但无法解析，拒绝启动（读不出持有者 PID…）
                lock_still_exists=True  bundles=[]
```
⇒ 行为从"误导性报错"变为"精确报错"，**拒绝强度不变**。无下游把它当"预期良性
条件"消费的地方（`scripts/smoke_recovery_bundle.py` / `smoke_fault_matrix.py` /
`tests/acceptance/test_supervision.py` 均只在正常/已死路径调用它）。**未放宽。**

---

## 6. 回归：full delta + check_quality + smoke

### 6.1 full delta（L19）
两树解包后 `git init -q && git add -A`（**只暂存不 commit**）。
- `git ls-files`：**pristine=834 / patched=834**（与作者一致；非空 ⇒ u10a 可用）。
- 逐脚本跑 `scripts/run_offline_regression.py::SCRIPTS` 全量：
  `_scratch_Q/_probe/run_regression_both.py`，日志 `_scratch_Q/regression_both.log`，
  原始 `_scratch_Q/regression_both.json`。

```
union scripts = 94
BOTH_OK = 94    BOTH_FAIL = 0    nonzero rc = 0/94（两树均为 0）
NEW_FAIL (pristine ok -> patched fail) = []
FIXED    (pristine fail -> patched ok) = []
CHANGED  (rc 均 0，输出文本不同) = ['scripts/smoke_u10a_release_package.py',
                                     'scripts/smoke_u13_supervision.py']
```
**CHANGED 的 2 项均为预期内、良性**（作者声称 `CHANGED=[]`，按严格输出比对**不可复现**，
但不构成缺陷；仅口径差异）：
- `smoke_u10a_release_package.py`：两态均 `37 通过 / 0 失败`，差异**仅在 `release_id`**
  （`rel-8f32b5…` vs `rel-ee801f…`）——4 个文件变了，包内容哈希必然变。**关键**：
  两态都打印 **`沙箱已跟踪文件 834 个`** ⇒ `git ls-files` 真返回 834 条，L19 生效、
  **u10a 确实真跑到**（pristine 99.5s / patched 89.9s，rc 均 0）。
- `smoke_u13_supervision.py`：两态 `U13 SMOKE OK steps=5`，差异来自**新增的
  [5]/[5b] 段输出**与结尾 OK 文案（`U13-FU4 INSTANCE LOCK FAIL-CLOSED OK` →
  `U13-FU4/FU5 LOCK + RECOVERY FAIL-CLOSED OK`）。rc 均 0。

### 6.2 check_quality（不得新增 exclude）
- `scripts/check_quality.py` 两树**逐字节相同**（`diff -q` → IDENTICAL）⇒ **未新增 exclude**
  （该脚本无硬编码 exclude 表，仅用 `--exclude-standard`）。
- 两态均：`QUALITY CHECK OK files=304 untracked=0`；
  `SECRET SCAN OK files=748 registered_false_positives=2 unparsable_python_files=0`；
  `DEPENDENCY LOCK OK packages=50`。
  （304 两态相同 —— FU5 是**改**既有测试文件而非新增，故 834/304 与 base 同。）

### 6.3 显式 smoke（两树各跑一遍）
| 脚本 | pristine | patched |
|---|---|---|
| `scripts/smoke_contracts_v3.py` | `PASS 173 / FAIL 0；golden cases=160` | `PASS 173 / FAIL 0；golden cases=160` |
| `scripts/smoke_u13_supervision.py` | `U13 SMOKE OK steps=5` | `U13 SMOKE OK steps=5` |
| `scripts/smoke_recovery_bundle.py` | `11 通过 / 0 失败` | `11 通过 / 0 失败` |

---

## 7. 补丁矩阵

补丁来源：作者 worktree `mat-wt/U13F5/docs/dispatch/patches/U13-FU5/1fe6037/` 的 4 个 diff
（**未在 1fe6037 提交内跟踪**，见 §8-1；已复制到 `_scratch_Q/_probe/patches/`）。
在 pristine 导出树内以 3 种 git 配置 `git apply --check --verbose`：

```
config=default                                  : 01/02/03/04 全 exit=0
config=-c core.autocrlf=false -c core.eol=lf    : 01/02/03/04 全 exit=0
config=-c core.autocrlf=true                    : 01/02/03/04 全 exit=0
Checking patch 计数 = 12     Skipped = 0     error/does not apply/fatal = 0     nonzero = 0
```
日志 `_scratch_Q/patch_matrix.log`。**12/12 `Checking patch`、0 `Skipped`，与作者一致。**

**附加（Q 加做）真实 apply 等价性**：把 4 个 diff 真实 `git apply` 到 pristine 副本
（`_scratch_Q/applytest`，4 个均 exit 0），与 `tree_patched` 逐文件比对
（`git ls-files -z` + `core.quotePath=false`，834 个跟踪文件）：
```
=== total differing tracked files: 0 ===   （--strip-trailing-cr 忽略 CRLF/LF）
```
⇒ 补丁包**确为**目标变更的精确载体（含 834/834 路径全覆盖，无遗漏无多余）。

---

## 8. 未能验证的部分（明确列出）

1. **补丁包未随 commit 交付**：`docs/dispatch/patches/U13-FU5/1fe6037/` 在 `1fe6037`
   **未被跟踪**（`git ls-tree` 无任何 FU5 路径；`mat-wt/I` 与主仓亦无），只存在于作者
   worktree `mat-wt/U13F5` 的**未跟踪文件**里。我按其内容验证了 4 diff 与声称的
   16 evidence（清单见 `evidence/`），但**无法验证这些文件是否就是作者最终交付的那一份**
   （无哈希基线可比）。若交付要求"补丁包随 commit"，则**未满足**。
2. **未在真实交易服务上验证**：全程离线；"撕裂包 ⇒ 灾难恢复后果"是按代码路径推断，
   未跑真实下单/恢复链。
3. **TOCTOU 的 §3 复现用了注入**：把 writer 启动点精确插在只读检查之后（确定性构造）。
   真实并发下窗口命中率未计时、未做统计采样。真实触发路径（`O_EXCL` 建文件 →
   `os.write` 之间、或服务重启）未 vivo 计时。
4. **未测超长/深层嵌套 JSON、符号链接锁、锁为目录、磁盘满部分写入**等其它"读不出"
   形态；只覆盖了任务点名的 7 类 + 非对象。
5. **未跑 `tests/` 下与本次无关的完整 pytest 套件**（环境无 pytest）；仅跑
   `run_offline_regression.py` 的 94 脚本 + 两个验收套件 + 质量门禁。
6. **未全仓普查**其它"读不出 ⇒ 当已死/已停"的同类推断；只按要求核了
   `recovery.check_writer_stopped`。
7. **非本机平台**：`_pid_alive` 的 POSIX 分支未执行（Windows 上只走 `os.name=="nt"`）。
8. **`check_quality` 的 exclude 口径**：以"`check_quality.py` 逐字节相同"推断
   "未新增 exclude"，未逐一枚举任何隐式忽略规则（如 `.gitignore` 变更）。
9. **作者 `CHANGED=[]` 的口径**：其比对方法未公开，我只能给出严格输出比对的
   `CHANGED=[u10a(release_id), u13_supervision(新增段)]`，未能复现其空集。

---

## 9. 报告文件路径

`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU5\R1\q\_scratch_Q\Q_U13-FU5_r1.md`

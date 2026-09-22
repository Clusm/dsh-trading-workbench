# U13-FU4 独立验收报告 (Q 角色, R1)

工作树: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU4\R1\q` @ `109d97f` (detached)
代码 head: `709e11a` (patched) / `d52bd68` (pristine)
解释器: `C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe` (无 pytest)
日期: 2026-09-22

自建两棵树: `_scratch_Q/tree_pristine` (git archive d52bd68, 808 文件) /
`_scratch_Q/tree_patched` (git archive 709e11a, 809 文件)。
所有探针为 Q 自写，**未复用**作者的 `probe_lock_corruption.py.txt`。

---

## 0. 结论

**PASS-with-notes**

- **FU4 本体的修复：完全成立。** 跨进程两态前后对照 8/8 变体全部复现缺陷（pristine）
  并被修复（patched）；拒绝时锁文件**逐字节未动**；情形 2（确认已死）仍能回收；
  无关存活 PID 仍被拒；正常路径逐字不变；全量回归零新增失败。
- **但"锁文件损坏 ⇒ fail-OPEN"这一类并未完全关闭**：同源缺陷
  `src/core/ops/recovery.py::check_writer_stopped`（`json.JSONDecodeError` ⇒ `return`，
  把"读不出"当"写者已停"）**已独立复现为会造成危险动作**（见第 6 节）。
  作者"不删锁、不接管 ⇒ 无害"的论证**不成立**：它给恢复包盖上虚假的
  `writer_stopped: True` 一致性声明，并被下游 `verify_epoch_bundle` /
  `restore_recovery_bundle.py` 接受。
- 判定：FU4 补丁本身**不阻塞**（其范围就是 `InstanceLock.acquire`，且修得正确）；
  但若验收口径是"锁损坏 fail-open 类已闭合"，则应记为 **CHANGES_REQUESTED /
  必须补一条同源修复**。建议登记 **U13-FU5（P0）**，不要合入即宣布该类闭合。

阻塞项（按任务给的三条硬判据）：**无**。第 2 条（锁被删/被改）、第 3 条（情形 2 被
修坏）两条阻塞判据均**未触发**。

---

## 1.【最重要】跨进程前后对照（Q 自建探针，真实两个进程）

探针：`_scratch_Q/_probe/holder.py`（真实子进程，acquire 后保活 30s）、
`_scratch_Q/_probe/acquirer.py`（第二个真实进程）、
`_scratch_Q/_probe/run_crossprocess.py`（编排 + 记录）。
日志：`_scratch_Q/crossprocess.log`，原始结果 `_scratch_Q/crossprocess_results.json`。

命令：
```
PYTHONUTF8=1 <PY> _scratch_Q/_probe/run_crossprocess.py   # exit 0
```
（对每个变体：holder 进程拿到锁并保活 → 用损坏字节覆盖锁文件 → 第二个进程尝试 acquire）

| 变体 | pristine 第二个进程 | 持有者仍活 | 两个活持有者 | patched 第二个进程 | 持有者仍活 |
|---|---|---|---|---|---|
| empty (0 字节) | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |
| truncated_json | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |
| bad_json | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |
| non_object (`[1,2,3]`) | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |
| no_pid | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |
| pid_zero | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |
| pid_negative | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |
| non_utf8 | ACQUIRED, exit 0 | 是 | **是** | REFUSED, exit 3 | 是 |

**结论**：修前 8/8 变体都**接管成功且持有者仍在运行 = 两个活持有者**（重复下单根因
复现）；修后 8/8 变体都**被拒（exit 3）**，持有者仍在运行但未被侵扰。
`two_live_holders` 字段 pristine 全 true / patched 全 false。

---

## 2.【最重要】"未删锁"逐字节验证

每个变体记录拒绝前后锁文件的原始字节与 sha256（`crossprocess_results.json` 的
`lock_sha_before` / `lock_sha_after` / `lock_unchanged`）。

**patched：8/8 变体 `lock_unchanged = true`，且 before/after sha256 完全相等**，例如：
- `empty`: sha `e3b0c44298fc1c14` == `e3b0c44298fc1c14`（0 字节，仍存在）
- `truncated_json`: sha `6cbe1b801f07e869` == `6cbe1b801f07e869`
- `bad_json`: sha `317455fa1c343819` == `317455fa1c343819`
- `non_object`: sha `a36b1f2c3f84522d` == `a36b1f2c3f84522d`
- `no_pid`: sha `9858b0a489729318` == `9858b0a489729318`
- `pid_zero`: sha `db6c7eb814d090e4` == `db6c7eb814d090e4`
- `pid_negative`: sha `a649e80eda4ad8c4` == `a649e80eda4ad8c4`
- `non_utf8`: sha `6884e012beab78f6` == `6884e012beab78f6`

对比 pristine：8/8 变体 `lock_exists_after = false`（旧锁被删，随后被接管者重建又在其
退出时释放）——正是被修的破坏行为。**第 2 条阻塞判据未触发。**

---

## 3.【最重要"没修坏"】情形 2 仍能回收 + 无关 PID 被拒

探针：`_scratch_Q/_probe/run_scenarios.py`，日志 `_scratch_Q/scenarios.log`，
结果 `_scratch_Q/scenario_results.json`，命令 exit 0。

**情形 1（可解析 + 持有者真实存活）**：两态都拒绝。
- pristine: `RuntimeError: 交易服务已在运行（PID=23308，锁文件=state.lock），持有者={'lock_scope': 'q-probe-holder'}`
- patched : `RuntimeError: 交易服务已在运行（PID=21536，锁文件=state.lock），持有者={'lock_scope': 'q-probe-holder'}`
- `lock_unchanged = true`，`holder_alive_after = true`。**文案逐字不变。**

**情形 2（可解析 + pid = 已退出的真实子进程）**：两态**都回收成功**。
- 先 `Popen([PY, "-c", "sys.exit(0)"])` 并 `wait()` → 得到确定已死的 pid。
- pristine: acquirer `exit 0` / `RESULT=ACQUIRED`，新锁 `pid=10564`（= 接管者自身 pid，≠ 运行器）
- patched : acquirer `exit 0` / `RESULT=ACQUIRED`，新锁 `pid=28468`（= 接管者自身 pid）
- **第 3 条阻塞判据未触发：合法运维路径（回收遗留锁）未被修坏。**

**边界（pid 指向存在但非 trader 的无关进程）**：用运行器自身 PID（19704，真实存活）。
- pristine: REFUSED exit 3，`lock_unchanged=true`
- patched : REFUSED exit 3，`lock_unchanged=true`
- 即"存活但无关"的 PID 被正确判为存活并拒绝，**未误回收**（两态一致）。

---

## 4. 报错可行动性 / 异常类型 / 非 UTF-8

**继承关系**：`class LockUnreadableError(RuntimeError)`（`instance_lock.py:43`）。
跨进程实测 `EXC_IS_RUNTIMEERROR=True`（8/8 变体）⇒ 既有 `except RuntimeError` 调用方
语义不变。

**实际文案**（patched, 变体 empty，逐字）：
```
单实例锁文件存在但无法解析，拒绝启动（读不出持有者 PID，不能确认它是否已死亡）：<lock_path>
原因：锁文件内容不可解析（0 字节，UTF-8）：JSONDecodeError: Expecting value: line 1 column 1 (char 0)
为什么不能自动清理：读不出内容 ≠ 持有者已死亡；自动删除锁文件会让两个实例同时管理同一状态根（重复下单 / 控制面双写）。
操作者请这样做：
  1) 先确认交易服务是否正在运行（进程列表 / 控制台 / 健康巡检的核心环）；
  2) 确认**未**运行后，手工删除该锁文件再启动：
     Windows: del "<lock_path>"
     POSIX:   rm "<lock_path>"
  3) 若服务确实在运行，不要删除该文件——继续使用已在运行的服务即可。
```
**判定：可行动。** 同时给了"先确认服务是否在运行"（第 1 步）与**具体删除命令**
（Windows `del` / POSIX `rm`，含绝对路径），并明确"服务在跑就别删"。

**非 UTF-8 路径**：`non_utf8` 变体（`b"\xff\xfe\x00\x01\x80 ..."`）走
`_read_holder` 的 `except (OSError, UnicodeDecodeError)` 分支：
```
原因：锁文件无法读取：UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte
```
=> **确认已并入 `LockUnreadableError`**，首版裸抛 `UnicodeDecodeError` 的问题已解决
（类型仍是 `LockUnreadableError`，`EXC_IS_RUNTIMEERROR=True`）。

其他变体原因串：`no_pid` → `ValueError: pid 字段缺失或类型非法（NoneType）`；
`pid_zero`/`pid_negative` → `pid 不是有效进程号（0 / -5）`；`non_object` →
`JSON 顶层不是对象（list）`。

---

## 5. 正常路径逐字不变 + 未改动函数核对

| 脚本 | pristine | patched |
|---|---|---|
| `scripts/smoke_p0_account_safety.py` | 17 通过 / 0 失败 exit 0 | 17 通过 / 0 失败 exit 0 |
| `scripts/smoke_profile_isolation.py` | PROFILE ISOLATION OK exit 0 | PROFILE ISOLATION OK exit 0 |
| `scripts/smoke_u13_supervision.py` | U13 SMOKE OK **steps=4** exit 0 | U13 SMOKE OK **steps=5** exit 0 |

`smoke_p0_account_safety.py` 两态日志 `diff` = **IDENTICAL**（逐字节）。
u13 supervision 日志两态差异仅为：自建树路径不同 + 新增第 5 步，内部
`U13 SUPERVISION ACCEPTANCE PASS: 166 checks` 两态一致。

**函数级 AST 比对**（`il_pristine.py` vs `il_patched.py`）：
```
SAME  __del__ / __init__ / _pid_alive / _sanitise_metadata / describe / release / update_metadata
DIFF  acquire          （唯一被改的函数）
only in patched: _read_holder
```
=> `_pid_alive` / `release` / `describe` / `update_metadata` **确认逐字未改**（AST 源段
完全相等），另外 `__init__` / `__del__` / `_sanitise_metadata` 也未改。

**作者验收套件两态**（pristine 树无该新文件，按作者口径注入后运行）：
- pristine：`U13-FU4 FAILED: 24 项`，exit 1，PASS=18 / FAIL=24 —— 与作者一致。
- patched ：`U13-FU4 INSTANCE LOCK FAIL-CLOSED OK`，exit 0，PASS=42 / FAIL=0。
- 该套件确实用真实 `subprocess.Popen` + `sys.executable -c` 起跨进程持有者。

---

## 6.【独立严重度判定】`recovery.check_writer_stopped` 的第二条同源 fail-open

### 代码
`src/core/ops/recovery.py:345-365`（**未被本次补丁改动**）：
```python
try:
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
except (FileNotFoundError, OSError, json.JSONDecodeError):
    return                      # <-- 读不出 = 当作"写者已停"，fail-OPEN
try:
    pid = int(payload.get("pid") or 0)
except (TypeError, ValueError):
    pid = 0
if pid > 0 and InstanceLock._pid_alive(pid):
    raise RecoveryError(RECOVERY_WRITER_RUNNING, ...)
```
`{"pid": 0}` 甚至不抛异常（JSON 合法、pid=0 ⇒ 不 raise）⇒ 静默放行。

### 消费链（谁消费"写者已停"结论）
`check_writer_stopped` ← `create_epoch_bundle`（`recovery.py:477`）→ 该函数在
`manifest.json` 里**无条件写上 `"writer_stopped": True`**（`recovery.py:545`）→
- `verify_epoch_bundle` 强制要求该字段为真（`recovery.py:591`），
- 生产恢复脚本 `scripts/restore_recovery_bundle.py:132` 同样强制
  `service_was_stopped is True` 否则拒绝恢复。
- `recovery_plan` 第 1 步即明示该函数是"确认单实例锁无存活 PID"的**唯一停机证据**
  （`recovery.py:811`）。

即：`writer_stopped: True` 是"这份恢复包取自一个静止状态根、DB 与 JSON 同一时刻"的
**唯一声明**；restore 端全盘信任它。

### 最小复现（Q 自写）
探针 `_scratch_Q/_probe/repro_writer_stopped.py`（跑在 **patched** 树上，
日志 `_scratch_Q/repro_writer_stopped.log`，结果 `_scratch_Q/repro_writer_stopped.json`）：

步骤：建最小状态根(DB+4 个关键 JSON) → 起**真实存活**的 writer 子进程持有锁 →
把锁文件改成损坏内容 → 调 `check_writer_stopped` + `create_epoch_bundle` + `verify_epoch_bundle`。

```
### variant=empty_0_bytes      (writer_alive=true, lock 0 字节)
  check_writer_stopped_raised = false
  bundle_created = true
  manifest_writer_stopped = true        <-- 虚假一致性声明
  verify_epoch_bundle_accepted = true (verified_files=5)
  writer_still_alive_during_bundle = true
### variant=pid_zero           (writer_alive=true, lock={"pid": 0})
  check_writer_stopped_raised = false ; bundle_created = true
  manifest_writer_stopped = true ; verify_epoch_bundle_accepted = true
  writer_still_alive_during_bundle = true
### variant=truncated          (writer_alive=true)
  同上：bundle_created=true, manifest_writer_stopped=true, accepted=true
```

### 判定：**会造成危险动作 —— 作者"无害"的论证不成立**
- 作者称"不删锁、不接管 ⇒ 不造成双持有"——**这部分属实**（确实没有第二个写者）。
- 但危险动作**不在锁这一层，而在恢复包的可信度这一层**：函数把"读不出"当成"已停"，
  于是**在写者仍在运行时**建包，并把 `writer_stopped: True` 烙进 manifest；
  下游 `verify_epoch_bundle` / `restore_recovery_bundle.py` 因该字段为真而**接受**。
  该包宣称"DB 与状态 JSON 同一时刻"，实际是从一个**仍在变化**的状态根抓取的
  （DB 走 SQLite backup，各 JSON 在各自时刻单独 `read_bytes` 复制，见
  `recovery.py:536-539`），epoch 容差只约束墙上时钟、无法保证写者已静默。
  → 一份**撕裂的、却被声明为同一时刻**的恢复点流入灾难恢复路径。这正是
  `restore_recovery_bundle.py` 文档字符串所强调的"可执行证明"被伪造的场景。
- 触发输入与 FU4 **完全同源**（空/截断/坏 JSON/非对象/pid=0）。其中"空文件"在
  `acquire()` 的 `O_EXCL` 建文件到 `os.write` 之间存在真实竞态窗口，非纯理论。
- 缓解（非免责）：恢复后 `RecoveryGate` 强制 `RECONCILE_REQUIRED`，需全量对账才许开仓，
  因此不直接等同于立即重复下单；但它污染的是"停机证据"这一安全声明本身。

**结论：本任务漏修了一条同源缺陷（同一"锁读不出"输入、同一 fail-open 方向）。**
建议：`check_writer_stopped` 的 `except` 分支改为 fail-closed（读不出即
`raise RecoveryError(RECOVERY_WRITER_RUNNING / 新的 RECOVERY_LOCK_UNREADABLE, ...)`），
并对 `pid <= 0` 同样拒绝；登记 **U13-FU5 (P0)**。

---

## 7. 回归：full delta + check_quality + contracts

### 7.1 full delta（L19 修正已应用）
两棵树解包后 `git init -q && git add -A`（**只暂存不 commit**）。
- `git ls-files` 条数：**pristine = 808，patched = 809**（非空 ⇒ u10a 可用）。
- 逐脚本跑 `run_offline_regression.py::SCRIPTS` 全量：
  `_scratch_Q/_probe/run_regression_both.py`，日志 `_scratch_Q/regression_both.log`，
  结果 `_scratch_Q/regression_delta.json`。

```
union scripts = 94
BOTH_OK = 94   BOTH_FAIL = 0
NEW_FAIL (pristine ok -> patched fail) = []
FIXED    (pristine fail -> patched ok) = []
CHANGED  = 仅 smoke_u13_supervision.py 的 steps 4->5（本任务预期内的新增步骤）
```
- **`smoke_u10a_release_package.py` 确认真跑到**：pristine 82.9s / patched 82.0s，
  rc 均 0；patched 输出 `结果: 37 通过 / 0 失败`，且明确打印
  **`沙箱已跟踪文件 809 个，release_id=rel-8f32b54143f6c049c684`**
  ⇒ `git ls-files` 真的返回了 809 条，L19 修正生效。

### 7.2 check_quality（不得新增 exclude）
- `scripts/check_quality.py` 两棵树**文件完全相同**（`diff` IDENTICAL）⇒ **未新增 exclude**。
- pristine: `QUALITY CHECK OK files=303 untracked=0`（清理掉我为跑验收套件注入的测试文件后）
- patched : `QUALITY CHECK OK files=304 untracked=0` —— 差值 1 = 新增的
  `tests/acceptance/test_instance_lock_failclosed.py`，与 808→809 一致。
- 两态 `SECRET SCAN OK files=723/724 registered_false_positives=2 ... unparsable_python_files=0`。

### 7.3 smoke_contracts_v3
- pristine: `PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)` exit 0
- patched : `PASS 173 / FAIL 0；golden cases=160 (positive=45, negative=115)` exit 0

---

## 8. 补丁矩阵（3 补丁 × 3 git 配置）

命令（在 pristine 导出树内，`git init` 后）：
```
git <flags> apply --check --verbose 0X_*.diff
  flags ∈ { 默认, -c core.autocrlf=false -c core.eol=lf, -c core.autocrlf=true }
```
日志 `_scratch_Q/patch_matrix.log`：
```
config=default   : 01 exit=0 / 02 exit=0 / 03 exit=0
config=noeol     : 01 exit=0 / 02 exit=0 / 03 exit=0
config=autocrlf  : 01 exit=0 / 02 exit=0 / 03 exit=0
Checking patch 计数 = 9     Skipped 计数 = 0     error/fatal/"does not apply" = 0
```
**结论：9/9 `Checking patch`、0 `Skipped`，与作者证据一致。**

附加（Q 加做）：把 3 个 diff **真实 apply** 到 pristine 导出树
（`_scratch_Q/realapply.log`，三份均 exit 0），与 `tree_patched` 全树比对：
**忽略 CR 后完全相同**（`diff -r --strip-trailing-cr` → 无差异；`git diff --no-index` 的
`258 insertions / 258 deletions` 即 CRLF↔LF，属 `core.autocrlf` 检出差异，非补丁缺陷）。
⇒ 补丁包**确为**目标变更的精确载体。

---

## 9. 未能验证的部分（明确列出）

1. **未在真实交易服务上验证**：全程离线沙箱，未启动 demo 服务、未接触真实状态根/账户，
   因此"两个活持有者 ⇒ 实际重复下单"这一**下游后果**是按代码路径推断，未跑真实下单链。
2. **`O_EXCL` 建文件→`os.write` 的竞态窗口未在 vivo 计时**：第 6 节的"空文件"触发路径
   我按语义构造（直接写 0 字节）复现，未测量真实并发下的窗口命中率。
3. **未穷举所有"读不出"形态**：覆盖了任务点名的 7 类（空/截断/坏 JSON/非对象/无 pid/
   pid=0/非 UTF-8）+ pid 负数；未测超长/深层嵌套 JSON、符号链接锁文件、
   锁文件是目录、磁盘满导致的部分写入。
4. **`check_writer_stopped` 的修复未实现**（Q 只验证不改产品代码），第 6 节给的是
   最小复现与建议，非已修证据。
5. **未跑 `tests/` 下与本次无关的完整 pytest 套件**（环境无 pytest）；仅跑了
   `run_offline_regression.py` 登记的 94 个脚本 + 作者验收套件 + 两个质量门禁。
6. **未验证其他 fail-open 同源点**：只按要求核了 `recovery.py::check_writer_stopped`；
   未全仓普查"读不出 ⇒ 当作已停/已死"的同类推断（如其他模块的
   `except ...: return` / `pid = 0` 降级）。
7. **非本机平台未验证**：`_pid_alive` 的 POSIX 分支（`os.kill(pid, 0)`）在 Windows 上
   未执行，只跑了 `os.name == "nt"` 分支。

---

## 10. 报告文件路径

`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U13-FU4\R1\q\_scratch_Q\Q_U13-FU4_r1.md`

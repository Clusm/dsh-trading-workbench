# U13-FU4 补丁包（目录名 `709e11a`）

> 目录名 = **打包时（`.diff` 生成时）的分支 head** `709e11a`。`.diff` 内容 =
> `d52bd68..709e11a`（base `d52bd68` = 本轮起点，已确认干净）。本目录自身若在本
> head 之后落库，不影响 `.diff`。

本轮修的是复核确认仍存在的**活路径安全缺陷**：`InstanceLock.acquire()` 在锁文件
**存在但读不出**时 **fail-OPEN** —— 单方删除仍存活持有者的锁文件，让第二个活进程
拿到同一把锁（单实例互斥失效 → 重复下单 / 控制面重复动作 / 同一状态根双写）。

| 文件 | 交付方式 | 说明 |
|---|---|---|
| `src/core/instance_lock.py` | `01_core_instance_lock_failclosed.diff` | 核心修复：新增可区分的 `LockUnreadableError` + `_read_holder()`，`acquire()` 严格区分三种情形。 |
| `tests/acceptance/test_instance_lock_failclosed.py` | `02_acceptance_instance_lock_failclosed.diff` | 新增验收（独立可运行、无 pytest、**真实跨进程持有者**）：三情形 + 正常路径，两态可跑。 |
| `scripts/smoke_u13_supervision.py` | `03_smoke_u13_supervision_register.diff` | 把上述验收接入 U13 冒烟**第 5 步**（脚本数不变，离线回归仍 94）。 |

`src/core/ops/recovery.py` / `src/core/service.py` **本轮未改**（理由见 §7）。

---

## 1. 缺陷与根因（结构性）

修前 `acquire()`（`d52bd68` 的 `:146-163`）：

```python
except FileExistsError:
    try:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
    except Exception:
        pid = 0                      # ← 空/截断/JSON 坏/非 UTF-8/无 pid ⇒ pid = 0
    if self._pid_alive(pid):         # ← _pid_alive(0) 直接 return False（:62-63）
        ... raise RuntimeError("交易服务已在运行")
    try:
        self.path.unlink()           # ← 删除**仍存活**持有者的锁文件
    except FileNotFoundError:
        pass
# 循环 2 次，下一轮 O_EXCL 创建成功 ⇒ 第二个活进程拿到锁
```

代码注释自称"仅清理确认已死亡的遗留锁"，但**把"读不出"当成了"确认已死亡"**：
`except Exception: pid = 0` 之后，`_pid_alive(0)` 恒为 `False`，于是既不做存活判定，
也拿不到任何否定证据，径直走删除分支。**"读不出"与"已死亡"之间丢掉了全部信息。**

---

## 2. 修法：三种情形严格区分（方向论证）

新增 `_read_holder()` 把"读"这件事变成**只有一种成功出口**：

```python
def _read_holder(self) -> tuple[int, dict[str, Any]] | None:
    try:
        raw = self.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None                       # 文件消失 = 没有持有者（并发回收），不是损坏
    except (OSError, UnicodeDecodeError) as exc:
        raise LockUnreadableError(...)    # 读不到 ⇒ fail-closed
    try:
        data = json.loads(raw)
        if not isinstance(data, dict): ...raise ValueError...
        raw_pid = data.get("pid")
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (int, str)): ...raise...
        pid = int(raw_pid)
    except Exception as exc:
        raise LockUnreadableError(...)    # 解析不出 ⇒ fail-closed
    if pid <= 0:
        raise LockUnreadableError(...)    # 没有可用 pid ⇒ fail-closed
    return pid, data
```

| 情形 | 判定 | 处置 | 与修前 |
|---|---|---|---|
| 1 可解析 + 持有者存活 | `_pid_alive(pid)` True | 拒绝，`RuntimeError("交易服务已在运行…")` | **逐字不变** |
| 2 可解析 + 持有者确定已死 | `_pid_alive(pid)` False | 删遗留锁 → 下一轮 O_EXCL 重新竞争 | **逐字不变**（真运维需要） |
| 3 存在但不可解析 | 读/解析/pid 校验任一失败 | `LockUnreadableError`，**不删除、不接管** | **翻转：fail-open → fail-closed** |

方向论证（为什么不是"永不回收"，也不是"更宽的回收"）：

1. **情形 2 必须保留**：进程被 kill -9 / 断电后留下的锁文件若不回收，服务再也起不来。
   守卫它的判据是"能读出 pid**且** 该 pid 不存活"——两条都是**肯定性证据**。
2. **情形 3 的判据是"零证据"**：读不出 pid 时，我们对持有者一无所知。把"一无所知"
   当作"已死亡"，就是本案的根因。零证据下唯一安全的动作是停手并把决定交还操作者。
3. **不存在"更聪明的自动兜底"**：任何"文件大小/年龄/mtime"之类的替代判据都只是把
   "读不出"换成另一种不可靠推断（截断的锁文件 mtime 恰好是新的，反而更像在写）。
   fail-closed 是唯一不会引入新猜测的处置。
4. **不吞 `KeyboardInterrupt`/`SystemExit`**：`_read_holder` 用 `except Exception`
   （与既有实现同口径），只把"读不出"这一可预期故障转成 `LockUnreadableError`；
   `_pid_alive` 语义**未改**（`pid <= 0 → False` 保持原样——它的调用方 `health_watch`
   /`recovery` 的只读探针依赖这个口径，改它会波及别的门禁）。
5. **`LockUnreadableError` 继承 `RuntimeError`**：既有 `except RuntimeError` 调用方
   （`smoke_p0_account_safety`、`smoke_profile_isolation`、`service.py` 启动链）
   **语义不变**；同时上层可以单独识别"锁损坏"与"服务已在运行"，不必靠解析文案。
6. **竞态窗口的取舍**：`O_EXCL` 创建到写入之间有极短窗口，此时并发的第二进程会读到
   **空文件** → 判为情形 3 → 拒绝。这是**正确**的：那一瞬间持有者确实活着且持有锁，
   拒绝正是应有行为（修前这个窗口会让第二进程删掉它的锁）。故不需要"重试/睡眠"。

---

## 3. 前后对照（核心，**实测**）

### 3.1 跨进程探针（同一份脚本跑两态）

`evidence/probe_lock_corruption.py.txt`（归档为 `.py.txt`，不进被 lint 的树）。它只
用 `InstanceLock` 公开 API，因此**两态都能跑**；持有者是**真实子进程**（真实存活 PID），
探针进程扮演"第二个服务"——因此"两个活持有者"是真实两进程共存。

```bash
PYTHONUTF8=1 <venv>/python.exe evidence/probe_lock_corruption.py.txt <tree>   # 见 commands_and_exit_codes.txt
```

| 树 | 结果 | exit |
|---|---|---|
| pristine `d52bd68` | **3 通过 / 5 失败** | **1** |
| 修复 `709e11a` | **8 通过 / 0 失败** | **0** |

修前原文（`evidence/probe_pristine.log`，长行已折行）：

```
C3a  c3a_empty.lock 不可解析 → fail-closed  接管成功（探针PID=18628，持有者PID=27796 仍存活=True）→ 两个活持有者  FAIL
C3b  c3b_badjson.lock 不可解析 → fail-closed 接管成功（探针PID=18628，持有者PID=27796 仍存活=True）→ 两个活持有者  FAIL
C3c  c3c_nonutf8.lock 不可解析 → fail-closed 接管成功（探针PID=18628，持有者PID=27796 仍存活=True）→ 两个活持有者  FAIL
C3d  c3d_nopid.lock 不可解析 → fail-closed  接管成功（探针PID=18628，持有者PID=27796 仍存活=True）→ 两个活持有者  FAIL
C3e  c3e_pidzero.lock 不可解析 → fail-closed 接管成功（探针PID=18628，持有者PID=27796 仍存活=True）→ 两个活持有者  FAIL
C1   可解析+持有者存活 → 拒绝                 拒绝 RuntimeError: 交易服务已在运行（PID=27796，锁文件=c1_intact.lock）  PASS
C2   可解析+持有者已死 → 回收                 回收成功（旧PID=23876 已死；新PID=18628）                                    PASS
N    正常路径：首拿/次拒/释放后再拿               PASS                                                                  PASS
PROBE: 3 通过 / 5 失败
```

修后原文（`evidence/probe_patched.log`）：

```
C3a  c3a_empty.lock 不可解析 → fail-closed  拒绝 LockUnreadableError: 单实例锁文件存在但无法解析，拒绝启动…  PASS
C3b  c3b_badjson.lock 不可解析 → fail-closed 拒绝 LockUnreadableError: 单实例锁文件存在但无法解析，拒绝启动…  PASS
C3c  c3c_nonutf8.lock 不可解析 → fail-closed 拒绝 LockUnreadableError: 单实例锁文件存在但无法解析，拒绝启动…  PASS
C3d  c3d_nopid.lock 不可解析 → fail-closed  拒绝 LockUnreadableError: 单实例锁文件存在但无法解析，拒绝启动…  PASS
C3e  c3e_pidzero.lock 不可解析 → fail-closed 拒绝 LockUnreadableError: 单实例锁文件存在但无法解析，拒绝启动…  PASS
C1   可解析+持有者存活 → 拒绝                 拒绝 RuntimeError: 交易服务已在运行（PID=9732，锁文件=c1_intact.lock）  PASS
C2   可解析+持有者已死 → 回收                 回收成功（旧PID=27632 已死；新PID=27548）                                PASS
N    正常路径：首拿/次拒/释放后再拿               PASS                                                                PASS
PROBE: 8 通过 / 0 失败
```

`C3*` 的 PASS 判据 = **拒绝 ∧ 异常类型正是 `LockUnreadableError` ∧ 锁文件字节原样
未动**（探针逐条回读比对），即"不删除、不接管"。

### 3.2 三种情形对照表（含"没修坏"的反向证据）

| 情形（锁文件状态） | 持有者 | 修前 `d52bd68` | 修后 `709e11a` | 期望 |
|---|---|---|---|---|
| 可解析（正常 JSON，pid 有效） | **真实存活子进程** | 拒绝 `RuntimeError` | 拒绝 `RuntimeError`（**同文案**："交易服务已在运行（PID=…，锁文件=…）"） | 拒绝 ✓ |
| 可解析，pid = 已退出的真实子进程 | 确定已死 | **回收成功** | **回收成功**（新锁文件 pid = 本进程） | 回收 ✓（**没修坏**） |
| 空 / 截断为 0 字节 | 真实存活子进程 | **接管成功 → 两个活持有者** | 拒绝 `LockUnreadableError` + 锁文件未被删 | 拒绝 ✓（**修复点**） |
| 坏 JSON（`{"pid": 1234,`） | 真实存活子进程 | **接管成功 → 两个活持有者** | 拒绝 `LockUnreadableError` + 锁文件未被删 | 拒绝 ✓ |
| JSON 顶层非对象（`["pid",1234]`） | 真实存活子进程 | **接管成功 → 两个活持有者** | 拒绝 `LockUnreadableError` + 锁文件未被删 | 拒绝 ✓ |
| 非 UTF-8 字节 | 真实存活子进程 | **接管成功 → 两个活持有者** | 拒绝 `LockUnreadableError` + 锁文件未被删 | 拒绝 ✓ |
| 无 pid 字段（`{"thread": 5}`） | 真实存活子进程 | **接管成功 → 两个活持有者** | 拒绝 `LockUnreadableError` + 锁文件未被删 | 拒绝 ✓ |
| pid 为 0（不可用） | 真实存活子进程 | **接管成功 → 两个活持有者** | 拒绝 `LockUnreadableError` + 锁文件未被删 | 拒绝 ✓ |

### 3.3 落地验收（`tests/acceptance/test_instance_lock_failclosed.py`）

```bash
PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_instance_lock_failclosed.py
```

| 树 | 结果 | exit |
|---|---|---|
| pristine `d52bd68` | **18 通过 / 24 失败** | **1** |
| 修复 `709e11a` | **42 通过 / 0 失败** | **0** |

（`evidence/acceptance_{pristine,patched}.log`。该文件对 `LockUnreadableError` 的
import 做了降级，所以**修前也能跑完整流程**并逐条报 FAIL，而不是 import 崩掉——
"两态可跑"。）

---

## 4. 报错文案（可行动）

实际抛出的文案（`str(exc)`，证据日志同源）：

```
单实例锁文件存在但无法解析，拒绝启动（读不出持有者 PID，不能确认它是否已死亡）：<锁文件绝对路径>
原因：锁文件内容不可解析（0 字节，UTF-8）：JSONDecodeError: Expecting value: line 1 column 1 (char 0)
为什么不能自动清理：读不出内容 ≠ 持有者已死亡；自动删除锁文件会让两个实例同时管理同一状态根（重复下单 / 控制面双写）。
操作者请这样做：
  1) 先确认交易服务是否正在运行（进程列表 / 控制台 / 健康巡检的核心环）；
  2) 确认**未**运行后，手工删除该锁文件再启动：
     Windows: del "<锁文件绝对路径>"
     POSIX:   rm "<锁文件绝对路径>"
  3) 若服务确实在运行，不要删除该文件——继续使用已在运行的服务即可。
```

要点：**写明失败原因与文件绝对路径**、**明确"不自动清理"的理由**、**给出确认→手工
删除的具体命令**、并区分"确实在运行就不要删"。异常对象同时带 `lock_path` / `reason`
属性，上层无需解析文案。

---

## 5. 正常路径不变的反向证据（既有冒烟数字）

既有冒烟在两态下**数字相同**（`evidence/smoke_pristine_*.log` / `smoke_patched_*.log`）：

| 冒烟 | pristine `d52bd68` | 修复 `709e11a` |
|---|---|---|
| `scripts/smoke_p0_account_safety.py`（含 `[6] 单实例锁拒绝第二个服务`：首拿 / 次拒 / 释放后可再启动 + 释放后锁文件消失） | 17 通过 / 0 失败，exit 0 | **17 通过 / 0 失败，exit 0** |
| `scripts/smoke_profile_isolation.py`（demo/live 不同锁、同 profile 重复启动被拒、释放后不互相阻塞） | `PROFILE ISOLATION OK`，exit 0 | **`PROFILE ISOLATION OK`，exit 0** |
| `scripts/smoke_u13_supervision.py` | `U13 SMOKE OK steps=4`，exit 0（225 条 PASS） | **`U13 SMOKE OK steps=5`，exit 0**（225 条 PASS + 第 5 步 42 条） |
| `scripts/smoke_detached_profiles.py` | exit 0（沙箱回归 `reg_pristine.tsv`） | exit 0（沙箱回归 `reg_patched.tsv`） |
| `scripts/smoke_takeover_compatibility.py` | exit 0（沙箱回归） | exit 0（沙箱回归） |

`smoke_p0_account_safety` 的 `[6]` 三条与 `smoke_profile_isolation` 的重复启动拒绝，
正是"首进程拿锁 / 次进程被拒 / 释放后可再拿"的既有数字——两态逐字相同。

---

## 6. 沙箱 full delta + check_quality + 3 配置 `git apply --check`

方法：两棵树 `git archive` 导出后**立刻** `git init && git add -A -f`（本项目 L19：
**只暂存不 commit**，否则靠 `git ls-files` 建沙箱的冒烟得空列表、两态同崩、该脚本
实际未被执行），逐脚本跑各树自己 `scripts/run_offline_regression.py` 的 `SCRIPTS`
（AST 读入，不另抄；`EXECUTION_MODE=paper`、清 `MTA_PROFILE`/`MTA_STATE_ROOT`/
`MTA_DATA_ROOT`、清凭据变量、`PYTHONUTF8=1`）。

```
pristine=d52bd68 patched=709e11a
sets_equal=True
pristine_total=94 patched_total=94
pristine_nonzero=0 patched_nonzero=0
NEW_FAIL=[]
FIXED=[]
BOTH_NONZERO=[]
```

| 项 | 结果 |
|---|---|
| 脚本集合 | 94 / 94，两棵树**同一集合**（`sets_equal=True`） |
| pristine（`d52bd68`）非零 | **0 / 94** |
| patched（`709e11a`）非零 | **0 / 94** |
| **NEW_FAIL**（0→≠0） | **∅** |
| **FIXED**（≠0→0） | **∅** |
| BOTH_NONZERO | **∅** |

L19 校验（证明沙箱索引确实建起来了、两态都没有"空列表同崩"）：

- patched 沙箱 `git ls-files | wc -l = 809`；
- `scripts/smoke_release_package.py`（靠 `git ls-files` 建沙箱）**两态都真跑到了**
  （`结果: 6 通过 / 0 失败`，rc=0）；
- 新增验收**确实被执行**：patched 沙箱的 `scripts/smoke_u13_supervision.py` 输出
  `U13-FU4 INSTANCE LOCK FAIL-CLOSED OK` + `U13 SMOKE OK steps=5`（rc=0，8.5s），
  pristine 沙箱同脚本只有 4 步（`grep -c test_instance_lock_failclosed` 分别 = 2 / 0）。

逐脚本 rc 原文：`evidence/reg_pristine.tsv` / `evidence/reg_patched.tsv`，
汇总结论：`evidence/full_delta_summary.txt`。`check_quality`：
`evidence/check_quality_patched.log`。

`git apply --check`（pristine 导出树内，`evidence/apply_check_3configs.log`）：

| git 配置 | 01 | 02 | 03 | Skipped |
|---|---|---|---|---|
| 默认 | `Checking patch` exit 0 | exit 0 | exit 0 | 0 |
| `-c core.autocrlf=false -c core.eol=lf` | exit 0 | exit 0 | exit 0 | 0 |
| `-c core.autocrlf=true` | exit 0 | exit 0 | exit 0 | 0 |

---

## 7. 未尽事项（如实登记）

1. **`src/core/ops/recovery.py:check_writer_stopped` 未改**：它在 `json.JSONDecodeError`
   时 `return`（把"锁读不出"当作"写者已停"），是**同源 fail-open 的另一种表现**（不涉及
   删除/接管，故不造成两个实例同时持有锁）。本轮按任务边界（只修 `instance_lock.py`、
   其余"只出补丁"）**未动**，登记待triage。同理 `health_watch.probe_instance_lock`
   在损坏时给 `process_unverified`（而非 `process_missing`），**已经是**可区分口径，
   无需改。
2. **`O_EXCL` 创建 → `os.write` 之间的极短窗口**：并发第二进程可能读到空文件而判为
   情形 3 并拒绝。该行为**正确**（那一刻持有者确实活着），但会让"恰好同时启动"的
   第二进程报"锁文件损坏"而不是"服务已在运行"——文案会指向手工删除，而正确动作是
   "别启动"。未加"重试/退避"以避免在互斥路径引入时间依赖。
3. **本轮的验收文件未登记进 `scripts/run_offline_regression.py` 的 `SCRIPTS`**（保持
   94 脚本不变，便于 pristine vs patched 逐脚本对比）；它通过 `smoke_u13_supervision.py`
   第 5 步进入离线回归，因此**仍被每轮回归执行**。
4. **单进程多对象只在验收里做补充**：`[4] 正常路径` 与 `[2] 回收` 用同进程多对象；
   三情形的判定本身（情形 1/3）一律用**真实跨进程**持有者。
5. 本轮**未改** `_pid_alive`、`release()`、`describe()`、`update_metadata()` 的语义
   （`git diff d52bd68 709e11a -- src/core/instance_lock.py` 显示仅新增 + `acquire()`
   一处改写）。

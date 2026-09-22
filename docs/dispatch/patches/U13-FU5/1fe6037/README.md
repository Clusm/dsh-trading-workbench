# U13-FU5 补丁包（目录名 `1fe6037`）

> 目录名 = **打包时（`.diff` 生成时）的分支 head** `1fe6037`。`.diff` 内容 =
> `1d737b5..1fe6037`（base `1d737b5` = 本轮起点，已确认干净）。本目录自身在本
> head 之后落库，不影响 `.diff`。

本轮修的是**灾难恢复路径上的 P0**：锁文件损坏时，恢复包会被声明为"写者已停"
并据此验证通过，**而写者其实仍在运行** —— `create_epoch_bundle` 会产出一份
`writer_stopped: True` 的恢复包，`verify_epoch_bundle` **接受**它，
`scripts/restore_recovery_bundle.py` 亦信任它。用它恢复 = 把一个**正在被写入**
的状态根当成"同一时刻静止"来还原。

| 文件 | 交付方式 | 说明 |
|---|---|---|
| `src/core/instance_lock.py` | `01_core_instance_lock_read_helper.diff` | 把 U13-FU4 的 `_read_holder()` 实现本体抽成**模块级** `read_lock_holder()`，供只读消费者复用；FU4 语义逐字不变。 |
| `src/core/ops/recovery.py` | `02_core_ops_recovery_writer_stopped_failclosed.diff` | 核心修复：`check_writer_stopped` 严格区分四情形，锁读不出 → `RecoveryLockUnreadableError`（`RECOVERY_LOCK_UNREADABLE`）拒绝建包。 |
| `scripts/create_recovery_bundle.py` | `03_script_create_recovery_bundle_lock_unreadable.diff` | **核对结论：不需要新护栏**（它走 `InstanceLock.acquire()`，FU4 已封死）。只去掉把"锁读不出"误报成"服务仍在运行"的包装。 |
| `tests/acceptance/test_instance_lock_failclosed.py` | `04_acceptance_recovery_lock_failclosed.diff` | 扩展 U13-FU4 的验收（**直接在原文件上加 [5]/[5b] 两节**），两态可跑、无 pytest、真实跨进程 writer。 |

---

## 1. 缺陷与根因（结构性）

修前 `src/core/ops/recovery.py:345`（base `1d737b5`）：

```python
def check_writer_stopped(paths: RuntimePaths) -> None:
    lock_file = paths.lock_file
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return                       # ← 把"读不出"当成"写者已停"
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and InstanceLock._pid_alive(pid):
        raise RecoveryError(RECOVERY_WRITER_RUNNING, ...)
```

消费链把这次静默 `return` 变成了**被签名的谎言**：

| 位置 | 行为 |
|---|---|
| `create_epoch_bundle:477` | 调 `check_writer_stopped` —— 损坏锁时静默通过 |
| `create_epoch_bundle:545` | **无条件**写 `"writer_stopped": True` |
| `verify_epoch_bundle:591-593` | **强制要求**该标志为 True 才接受恢复包 |
| `scripts/restore_recovery_bundle.py:132` | 亦信任该标志 |

于是"读不出"→"写者已停"→"这份恢复包是同一时刻静止的一致快照"三段推理全部
无据，却一路被机器验证通过。**锁文件被截断为 0 字节（含 `O_EXCL` 创建与
`os.write` 之间的真实竞态窗口）即可触发**，坏 JSON / 非 UTF-8 / 顶层非对象 /
无 pid / pid<=0 同理。

**根因一句话："读不出"绝不等于"写者已停"——中间丢掉的全部信息，被一个
`return` 抹平了。**

---

## 2. 修法：四种情形严格区分（方向论证）

`check_writer_stopped` 现在的判定表（与 U13-FU4 同口径）：

| # | 锁文件事实 | `read_lock_holder` 结果 | 判定 | 处置 |
|---|---|---|---|---|
| 1 | **不存在**（`FileNotFoundError`） | `None` | 写者已停 | **允许**（合法路径，未修坏） |
| 2 | 可解析 + pid 有效且**存活** | `(pid, data)` + `_pid_alive` True | 写者在写 | **拒绝** `RECOVERY_WRITER_RUNNING`（逐字不变） |
| 3 | 可解析 + pid 有效且**已死** | `(pid, data)` + `_pid_alive` False | 遗留锁，写者已停 | **允许**（真运维需要，未修坏） |
| 4 | **存在但不可解析 / 无可用 pid** | 抛 `LockUnreadableError` | **无法确认** | **拒绝** `RECOVERY_LOCK_UNREADABLE`（fail-closed） |

情形 4 覆盖：空文件 / 截断 / 坏 JSON / 顶层非对象 / 非 UTF-8 / 缺 `pid` /
`pid<=0`。

方向论证（为什么不是另外两种更省事的写法）：

1. **不能"锁文件存在就一律拒绝"**：那会把情形 3 修坏。进程被 `kill -9` / 断电
   后留下的**遗留锁**是真实且常见的运维现场——持有者确实已死，恢复包必须能建。
   情形 1 同理（服务从未启动过、或已 `release()`）。这两条是本轮的**反向证据**
   （见 §4 [5b]），必须仍然允许。
2. **不能只扩大 `except` 名单**：把 `UnicodeDecodeError`、`AttributeError`
   补进 `except (...): return` 只会让 fail-open 的面更大。修前的行为其实已经
   暴露了这一点：非 UTF-8 与顶层非对象会从 `check_writer_stopped` 里**漏出**
   一个无关的 `UnicodeDecodeError` / `AttributeError`——既不是"允许"也不是
   "拒绝"，是一种**没人设计过的第三种状态**。
3. **必须可区分**：`RECOVERY_WRITER_RUNNING`（"确认写者还活着"）与
   `RECOVERY_LOCK_UNREADABLE`（"不知道写者在不在"）是两条不同的运维处置：
   前者要求停机，后者要求**先确认、再手工删锁**。合并成一个码会让操作者按错
   手册。故新增独立原因码 + 独立异常类（对齐 FU4 的 `LockUnreadableError`）。
4. **报错必须可行动**：`RecoveryLockUnreadableError` 的正文写明"读不出 ≠ 写者
   已停"的后果，并给出三步处置（先确认服务是否在运行 → 确认未运行后手工删锁的
   Windows/POSIX 命令 → 若在运行则不要删）。`__cause__` 指向底层
   `LockUnreadableError`，保留 FU4 的同款文案。

---

## 3. 复用 U13-FU4 的 `LockUnreadableError`（不引入第二套口径）

`read_lock_holder()` 是 FU4 `InstanceLock._read_holder()` 的实现本体，本轮**原样
抽成模块级函数**，`_read_holder()` 改为一行转发：

```python
def _read_holder(self) -> tuple[int, dict[str, Any]] | None:
    return read_lock_holder(self.path)
```

- `instance_lock.py` 的**改动只是搬位置**：同样的 `None`/异常条件、同样的报错
  文本、同样的 `except Exception`（不吞 `KeyboardInterrupt`/`SystemExit`）。
  FU4 的既有语义**逐字未变**（`tests/acceptance/test_instance_lock_failclosed.py`
  的 [1]–[4] 节在修复后仍然全绿，见 `evidence/acceptance_patched.log`）。
- 抽成模块级的理由是**只读消费者不该构造锁对象**：`InstanceLock.__init__` 有
  `mkdir(parents=True)` 副作用，而 `check_writer_stopped` 是纯只读判定。
  不抽的话就得把 `read_text + json.loads + pid 校验` 再写一遍——**那正是
  U13-FU5 的形态**（两套口径，其中一套偷懒）。
- 全仓现在只有**一处**"锁文件可读性"判定：`read_lock_holder`。FU4 的
  `acquire()` 与 FU5 的恢复包创建都走它。

---

## 4. 前后对照（实测，真实跨进程 writer）

### 4.1 主探针：`evidence/probe_recovery_lock_corruption.py.txt`

同一份脚本、两个树，都在"**真实存活 writer 子进程持锁** → 把锁文件截断为
0 字节"的现场跑：

```bash
PYTHONUTF8=1 <PY> evidence/probe_recovery_lock_corruption.py.txt <tree>
```

| 事实 | 修前 `1d737b5` | 修后 `1fe6037` |
|---|---|---|
| `writer_pid` / `lock_readable_and_writer_alive` | 8680 / `True` | 14084 / `True` |
| 锁完好 + 写者存活 → `check_writer_stopped` | `RECOVERY_WRITER_RUNNING` | `RECOVERY_WRITER_RUNNING` |
| **锁损坏 → `check_writer_stopped`** | **`NO_RAISE`（把"读不出"当成"写者已停"）** | **`RECOVERY_LOCK_UNREADABLE`** |
| **`bundle_created`** | **`True`（`recovery_20260922_013504_429490`）** | **`False`** |
| `bundle_error` | — | `RECOVERY_LOCK_UNREADABLE` |
| **`manifest_writer_stopped`** | **`True`** | （未产出包） |
| **`verify_epoch_bundle_accepted`** | **`True`（schema=3）** | （未产出包） |
| `writer_still_alive_at_end` | `True` | `True` |

修前那一列就是 P0 的完整证据链：**写者仍活，恢复包却造出来了、被标记为
`writer_stopped: True`、并被 `verify_epoch_bundle` 接受**。
日志：`evidence/probe_pristine.log` / `evidence/probe_patched.log`。

### 4.2 落地验收（扩展后的 U13-FU4 验收文件，两态可跑）

```bash
PYTHONUTF8=1 <PY> tests/acceptance/test_instance_lock_failclosed.py
```

| 树 | 退出码 | 结果 |
|---|---|---|
| 修前 `1d737b5` | **1** | `U13-FU4/FU5 FAILED: 23 项` —— 六种损坏形态下 `create_epoch_bundle` 均**未拒绝**（其中空文件/坏 JSON/无 pid 直接造出了伪称静止的包） |
| 修后 `1fe6037` | **0** | `U13-FU4/FU5 LOCK + RECOVERY FAIL-CLOSED OK` |

日志：`evidence/acceptance_pristine.log` / `evidence/acceptance_patched.log`。
[5] 节 39 项 + [5b] 反向证据 8 项，共 47 项新断言。

> 说明（如实）：修前树上"非 UTF-8 字节"与"顶层非对象"两种形态，验收里
> "拒绝建包"一项是 PASS —— 但不是因为被正确拒绝，而是 `check_writer_stopped`
> 漏出了无关的 `UnicodeDecodeError` / `AttributeError`。同组其余断言
> （原因码可区分、报错可行动）在这两种形态上均为 FAIL，因此整体仍判 FAIL，
> 不会把这两种形态洗成绿。修复后六种形态统一归到 `RECOVERY_LOCK_UNREADABLE`。

### 4.3 四情形对照表（含两条**反向证据**）

`check_writer_stopped` 在两态上的实测判定：

| # | 锁文件 | 修前 | 修后 | 修后**是否仍允许** |
|---|---|---|---|---|
| 1 | **不存在** | 允许 | 允许 | ✅ **仍允许**（反向证据 (a)，建包成功 + `verify` 接受） |
| 2 | 可解析 + pid 存活 | 拒绝 `RECOVERY_WRITER_RUNNING` | 拒绝 `RECOVERY_WRITER_RUNNING` | ❌ 拒绝（不变） |
| 3 | 可解析 + pid **已死** | 允许 | 允许 | ✅ **仍允许**（反向证据 (b)，建包成功 + `verify` 接受） |
| 4 | **存在但不可解析 / 无可用 pid** | **允许（P0）** | **拒绝 `RECOVERY_LOCK_UNREADABLE`** | ❌ 拒绝（**翻转**） |

情形 1 与 3 的"仍允许"由验收 [5b] 节**实测**：两例都真的建出了包、都为
`writer_stopped: True`、都通过 `verify_epoch_bundle(expected_environment="demo")`。
**合法运维路径没有被修坏。**

### 4.4 交付物 #4 核对：`scripts/create_recovery_bundle.py:179`

```bash
PYTHONUTF8=1 <PY> evidence/probe_create_bundle_lock_guard.py.txt <tree>
```

**结论：它不需要同一护栏——它没有绕过检查。** 该脚本不调
`check_writer_stopped`，而调 `InstanceLock.acquire()`（`:91`），这是**更强**的
守卫：它把锁**真的握在手里**直到建包完成（`:97` `finally: guard.release()`），
而 `acquire()` 早在 U13-FU4 就已对"锁存在但读不出"fail-closed。实测：

| 场景 | 修前 `1d737b5` | 修后 `1fe6037` |
|---|---|---|
| 锁完好 + 写者存活 | `REJECTED RuntimeError` | `REJECTED RuntimeError` |
| **锁损坏 + 写者存活** | `REJECTED RuntimeError`（`__cause__` = `LockUnreadableError`，文案含"手工删除"） | `REJECTED LockUnreadableError`（文案含"手工删除"） |
| `published_bundles` | `[]` | `[]` |
| `writer_still_alive` | `True` | `True` |

两态都**没有**在写者存活时写出 `writer_stopped: True`。本轮对该文件的唯一改动
是把 `except RuntimeError` 之前加一条 `except LockUnreadableError: raise`：
FU4 之后 `LockUnreadableError`（`RuntimeError` 子类）会被原样包装成
"交易服务仍在运行"——**fail-closed 没错，但把"不知道写者在不在"断言成了
"服务仍在运行"**，与本轮同源。改动只影响文案，不影响控制流。

日志：`evidence/probe_crb_pristine.log` / `evidence/probe_crb_patched.log`。

### 4.5 补丁 3 配置 `git apply --check`

```
git apply --check --verbose 01..04.diff                                        -> exit=0, Skipped=0
git -c core.autocrlf=false -c core.eol=lf apply --check --verbose 01..04.diff  -> exit=0, Skipped=0
git -c core.autocrlf=true apply --check --verbose 01..04.diff                  -> exit=0, Skipped=0
```

四份 diff 均 `Checking patch ...`，**0 Skipped**（`evidence/apply_check_3configs.log`）。

### 4.6 全量离线回归 full delta（94 脚本，两态）

驱动 `evidence/run_delta.py.txt`（沙箱两树各 94 脚本，逐条 rc 入 TSV）。按 **L19**
两棵树解包后都先 `git init -q && git add -A`（**只暂存、不 commit**），否则
`smoke_u10a_release_package.py` 靠 `git ls-files` 得空列表、两态同崩。

| 指标 | 值 |
|---|---|
| `git ls-files` 条数（pristine / patched 沙箱树） | **834 / 834** |
| （本仓 staged 工作树，含本轮新增的 docs 交付物） | 855 |
| 脚本总数 | 94 / 94（`sets_equal=True`） |
| 非零退出 | 0 / 0 |
| **NEW_FAIL** | **`[]`** |
| FIXED | `[]` |
| CHANGED | `[]` |
| BOTH_NONZERO | `[]` |

`smoke_u13_supervision.py`（内含本轮扩展后的验收第 5 步）两态均 rc=0：
pristine 树上是 FU4 版本的验收文件，patched 树上是 FU5 扩展版，**都绿** ——
FU5 的新断言只会在"锁损坏 + 写者存活"的现场触发，不会扰动既有路径。

见 `evidence/full_delta_summary.txt` / `reg_pristine.tsv` / `reg_patched.tsv` /
`regression_driver.log`。

### 4.7 质量门禁

```
PYTHONUTF8=1 <PY> scripts/check_quality.py   -> QUALITY CHECK OK files=304 untracked=0
```

退出码 0；`files=304 untracked=0` 与 base（FU4 的日志）**逐字相同**，
**未新增任何 exclude**（`evidence/check_quality_patched.log`）。

---

## 5. 未尽事项（如实登记）

1. **TOCTOU 未消除（修前修后同存）**：`create_epoch_bundle` 的
   `check_writer_stopped` 是**只读判定**——它在"锁不存在"时放行，服务仍可能在
   它返回之后、快照取完之前启动。本轮只把"读不出"这条 fail-open 路径封死，
   **没有**把建包改成"持锁建包"（`scripts/create_recovery_bundle.py` 本来就是
   持锁的，`core.ops.recovery.create_epoch_bundle` 不是）。这是既有设计，
   不在本轮范围；修它需要改 `create_epoch_bundle` 的调用契约，属独立条目。
2. **`verify_epoch_bundle` 仍只信 manifest 里的 `writer_stopped` 布尔**：
   校验时**无法**回溯证明"当时写者确实停了"。本轮保证的是"造包时不会把
   读不出的锁当成已停"，不是"事后可证明"。真正的可证明性需要把
   `read_lock_holder` 的观测结果（锁文件哈希/观测时刻）写进 manifest，
   属独立条目。
3. **`scripts/restore_recovery_bundle.py:132` 未改**：它读
   `service_was_stopped`（老字段），语义与本轮无关；本轮没有让它的信任对象
   变得更不可信（恰恰相反）。
4. **`InstanceLock.release()` / `describe()` 未处理"读不出"**：`release()`
   读不出时静默不删（fail-safe，不误删他人锁），`describe()` 读不出返回 `{}`
   （只读诊断）。两者都不是 fail-open 安全路径，本轮未动。

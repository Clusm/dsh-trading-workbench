# U15 r3 独立聚焦复验报告（Q 角色 · 修复轮 r3）

- 复验对象：`abf2eda`（分支 `task/U15`，父 `38029e2`；r3 提交 = `26d77a0` 修复、`331deb0`、`abf2eda` 登记）
- 复验工作区（独立、detached、只读）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15\R3\r1`
- 上一轮报告（必读，已独立通读）：`I/docs/dispatch/reviews/U15/38029e2/Q_r2.md`（结论 CHANGES_REQUESTED，唯一阻断项 NEW-A）
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 本报告全部结论来自**我自己写的探针**（`_scratch_Q/probe_q_r3_*.py`），原始 stdout 全部落盘
- 只读纪律：未提交/推送/打标签/切分支；未读 `.env`/`data/`/密钥/账户标识；未联网；未启停服务；
  未写入 `mat-wt/I`、`mat-wt/EF` 或任何其它 worktree（`git status --short` 仅 `?? _scratch_Q/`）
- 复验改动范围：`git diff --stat 38029e2 abf2eda` = `src/core/runtime_identity.py`(+251/-18)、
  两个验收脚本、`docs/dispatch/patches/U15/a136cb5/README.md`(+53)。**4 个补丁文件逐字节未改**
  （`git diff --stat 38029e2 abf2eda -- docs/dispatch/patches/U15/a136cb5/` 只列 README）

---

## 0. 结论

### **PASS-with-notes**

唯一阻断项 **NEW-A 真正闭合**：`bundle.identity`（environment / account_ref）与
`generation`（同名字段）的交叉校验已生效，我用 4 条正向对照 + 12 条独立绕过向量反复尝试，
**没有一条能在一个跨环境/跨账户的调用对上拿到 `allowed=True`**（§1）。

- brief 指定 6 项全部 **CLOSED**（NEW-A / NEW-D / NEW-E / NEW-F / NEW-G / NEW-H 判定方向成立）。
- 回归抽样 **11 组全部仍成立**（§7）；无 P0/P1 回归。
- 新发现全部为 **P3 且已在本报告登记**（§6）：无未登记的 P2 fail-open、无崩溃。
  按标尺（残留项须为 P2 及以下且已如实登记）→ PASS-with-notes。

| 项 | 判定 |
|---|---|
| 1 NEW-A（唯一阻断项） | **CLOSED**（正向 4/4、绕过 12 条全部 fail-closed；2 条 P3 口径残余见 §1c） |
| 2 NEW-D mint 类型校验 | **CLOSED**（brief 5 种 + 我扩的 29 种形态全部结构化拒绝；合法输入不受影响） |
| 3 NEW-E 依赖锁对照结论 | **CLOSED**（`compare_dependency_lock` 真产出 MATCH/MISMATCH/UNAVAILABLE 并经 `as_dict()` 交付；`diff-2` 登记据实） |
| 4 NEW-F `provider_env` | **CLOSED**（声明名生效 + 未设置回退默认名 + `sources["fallback_provider_env"]` 暴露证据） |
| 5 NEW-G 控制面语义 | **CLOSED**（400 / 400 `UNKNOWN_CONTROL_ACTION` / `CONTROL_STATE_ILLEGAL` / auto 只放行只读与更严方向） |
| 6 NEW-H 方向判定 | **成立**（已独立验证"不会放行非回环"，仅误拒合法本机请求；登记可接受） |

---

## 1. 项 1（NEW-A）—— `bundle.identity` ↔ `generation` 交叉校验：**CLOSED**

实现（`runtime_identity.py:1959-1980` + `2062-2066`）：

```python
def _identity_generation_mismatches(identity, generation) -> tuple[str, ...]:
    if _text(identity.environment).lower() != _text(generation.environment).lower():
        return (IDENTITY_GENERATION_MISMATCH,)
    if _text(identity.account_ref) != _text(generation.account_ref):
        return (IDENTITY_GENERATION_MISMATCH,)
    return ()
# authorize_new_entry:
if isinstance(bundle, IdentityBundle) and isinstance(generation, RuntimeGeneration):
    reasons.extend(_identity_generation_mismatches(bundle.identity, generation))
```

注意：这组校验在 `expected is None` 分支**之外**，所以缺 `expected` 时也照样生效（实测两码齐出）。

### 1a. 作者声称的前后对照 —— 我独立复现（原始 stdout：`out_q_r3_a.txt`）

夹具：我自己建的临时 root（`src/x.py` + 两个 `requirements*-lock.txt` + `knowledge/strategy_cards.json`），
bundle 用 `raw_settings`/`credentials_present` 显式注入（`blockers=()`）。

```
demo_bundle.identity : demo  | demo:acct-demo | inst=rt-demo   | idh=010c8643db2aa8
live_bundle.identity : live  | live:acct-live | inst=rt-liveb  | idh=6c28648672c5da

A1 demo bundle + live gen, expected=gen                    -> allowed=False 409 ('IDENTITY_GENERATION_MISMATCH',)
A2 A1 + 显式 environment/account_ref/instance/scope 全绑 gen -> allowed=False 409 ('IDENTITY_GENERATION_MISMATCH',)
A3 反向 live bundle + demo gen, expected=gen               -> allowed=False 409 ('IDENTITY_GENERATION_MISMATCH',)
A4 自洽 demo bundle + demo gen                             -> allowed=True  200 ()          <-- 无假阳性
A4b 自洽 live bundle + live 同账户 gen                      -> allowed=True  200 ()
```

r2 报告里的 `A1/A2/A3 -> allowed=True 200 reason_codes=()` **已不复存在**。

另外，缺 `expected` 的调用同样被拦（`out_q_r3_final.txt`）：

```
demo bundle + live gen, expected 省略 -> allowed=False http=409
    reasons=('IDENTITY_BINDING_UNPROVEN', 'IDENTITY_GENERATION_MISMATCH')
```

### 1b. 独立绕过尝试（12 条）—— 全部 fail-closed（原始 stdout：`out_q_r3_a.txt`、`out_q_r3_final.txt`）

| # | 绕过向量 | 结果 | 判定 |
|---|---|---|---|
| B1 | 只 `environment` 一致、`account_ref` 不同（demo 身份 vs `demo:acct-OTHER` 代） | 409 `IDENTITY_GENERATION_MISMATCH` | CLOSED |
| B2 | 只 `account_ref` 一致、`environment` 不同（identity demo vs gen live，账户串同） | 409 | CLOSED |
| B3 | `environment` 大小写差（identity `demo` vs gen `DEMO`） | **200**（`lower()` 归一，同环境） | 非缺陷（见 §1c-①） |
| B4 | `account_ref` 大小写差（`demo:acct-demo` vs `demo:ACCT-DEMO`） | 409 | CLOSED |
| B5 | `account_ref` 前后空白（`'  demo:acct-demo  '`） | **200**（`_text` strip，同账户） | 非缺陷（见 §1c-①） |
| B6 | `<env>:<declared>` 与裸串混用（identity `demo:acct-demo` vs gen `acct-demo`） | 409 | CLOSED |
| B7 | `<env>:local` 回退身份 bundle vs 声明了具体账户的运行代 | 409 | CLOSED（README 已声明属修复目标） |
| B8 | identity 经 `model_copy(environment='live', account_ref=gen.account_ref)` 后重组 bundle | 200 | **输入伪造**，非模块绕过（见 §1c-②） |
| B9 | 两侧 `environment=''`/`account_ref=''`（手搓 identity + 手搓 `RuntimeGeneration`） | 200 | 输入伪造（生产装配路径产不出空环境身份） |
| B10 | 同 env+账户、但 identity.inst=`rt-PROCESS-1` vs gen.inst=`rt-PROCESS-2` | 200 | 作者**刻意**不比实例号（见 §1c-③） |
| B11 | 两个 `identity_hash` 不同、env+账户相同的 bundle 共用同一运行代 | 200 | 结构使然（`RuntimeGeneration` 无哈希字段，见 §1c-③） |
| B12 | 两侧都走 `derive_account_ref('demo', declared_account_ref='acct-demo')` | 200 | 合法（对照组） |

补充：`object.__new__(IdentityBundle)` 后改写 `.identity` 的伪造对象也能得 200
（`out_q_r3_subclass.txt`），但这是**整个 bundle 都被伪造**（blockers/git_state/effective_config
同时可控），任何函数内部的检查都挡不住 —— 不属 NEW-A 修复范围。

### 1c. P3 口径残余（均非跨环境/跨账户放行，已登记）

- **① 归一化差异方向安全**：`environment` 比较用 `.lower()`、`account_ref` 比较用 `_text()`（strip）。
  B3/B5 得到的 200 是"同一环境的同一账户"，不构成跨账户放行；反向（大小写/空白造成的**不同**）
  一律 409。方向 fail-closed，可接受。
- **② 输入伪造类（B8/B9）**：本函数的契约是"`bundle` 是可信身份证据"。调用方若能任意重组
  `IdentityBundle`，则 `blockers`/`git_state`/`effective_config` 同样可控，防线不在本函数内。
  不计缺陷，但建议后续在 `authorize_new_entry` docstring 明写"bundle 必须来自
  `collect_runtime_identity`，不得由调用方拼装"。
- **③ 交叉校验只到 env+账户（B10/B11）**：`RuntimeGeneration` 没有 `artifact_hash`/
  `effective_config_hash` 等字段，所以"代与身份的哈希同源"在类型上无法表达，env+账户是本层
  **全部**可比的绑定。B10（同账户不同进程）是作者**刻意保留并在验收脚本里断言**的
  （`test_runtime_identity.py` 新增 `new-a-instance-difference-is-not-a-cross-mismatch`），
  理由：实例号另行用 `authorize_new_entry(runtime_instance_id=...)` 绑定（实测 B10b：
  调用方传 `runtime_instance_id='rt-demo'` → 409 `RUNTIME_INSTANCE_MISMATCH`，即该参数绑的是
  `expected↔generation` 而非 `identity↔generation`）。**doc 措辞不精确**：docstring 说
  "`IdentityBundle` 里没有该字段"，实际 `RuntimeIdentity.runtime_instance_id` 存在（K01 契约
  `k01_identity.py:96`）；且该参数并不能把 identity 的实例号绑到 generation 上。
  建议：docstring 改口径 + 在 README r3 登记表补一行"交叉校验只到 env+账户"。

---

## 2. 项 2（NEW-D）—— `mint_runtime_generation` 类型校验：**CLOSED**

原始 stdout：`out_q_r3_de.txt`。全部为**本模块结构化拒绝**（`MalformedInputError` / 既有
`RuntimeIdentityError`），无裸 `TypeError`/`ValueError`。

brief 指定的形态：

```
credential_epoch='3'   -> MalformedInputError ('credential_epoch must be an int, got str',)
credential_epoch=3.7   -> MalformedInputError ('credential_epoch must be an int, got float',)
credential_epoch=True  -> MalformedInputError ('credential_epoch must be an int, got bool',)
credential_epoch=None  -> MalformedInputError ('credential_epoch must be an int, got NoneType',)
started_at='now'       -> MalformedInputError ('started_at must be a real number or None, got str',)
credential_epoch=-1    -> RuntimeIdentityError: credential_epoch must be >= 0（既有语义）
```

我另扩的畸形形态（全部结构化拒绝）：

```
credential_epoch=Decimal('3') / 3+0j / [3]        -> MalformedInputError
environment=b'live' / 123 / None / ['live']       -> MalformedInputError('environment must be a string')
runtime_instance_id=b'rt' / None / 123 / ['rt']   -> MalformedInputError
account_ref=None / {'v':'x'} / ['a'] / 123 / b'..'-> MalformedInputError
scope_hash=123 / b'scope' / ['s']                 -> MalformedInputError
key_fingerprint_value=b'kf' / 123                 -> MalformedInputError
credential_epoch=10**40                           -> OK（合法大整数，不误拒）
```

合法输入不受影响：`AccountRef` 入参、裸串补 `env:` 前缀、`environment='DEMO'` 归一为 `demo`、
`credential_epoch=7`、`started_at=1750000000.123456`、`scope_hash`/`key_fingerprint_value=字符串`，
同一入参两次 mint 得到同一 `generation_id`（确定性保持）。

**P3 备注（新登记）**：`started_at=float('nan')` / `float('inf')` 通过校验并进入 `generation_id`
种子（`f"{started_at:.6f}"` → `'nan'`/`'inf'`）。不是 fail-open（不改变环境/账户绑定），
但建议后续显式拒绝非有限实数（`math.isfinite`）。

---

## 3. 项 3（NEW-E）—— 依赖锁对照结论 + `diff-2` 登记诚实性：**CLOSED**

原始 stdout：`out_q_r3_de.txt`、`out_q_r3_final.txt`。

```
declared=None, 两个锁文件都在            -> 'MATCH'
declared=None, dev 锁缺失                -> 'UNAVAILABLE'
declared=[]                              -> 'MISMATCH'
declared=['requirements-lock.txt']       -> 'MISMATCH'      (漏了 dev 锁)
declared=[两个锁 + 额外项]                -> 'MATCH'         (超集不算漂移)
declared=tuple(两个锁)                    -> 'MATCH'
declared=str / bytes / 123 / {'a','b'}    -> 'UNAVAILABLE'  (形状非法；保守方向)
declared=['']（空串项）                   -> 'MISMATCH'
```

三个取值全部落在声明的 `DEPENDENCY_COMPARISON_*` 常量集合内（`in_enum=True` 逐条核对）。

交付面：

```
IdentityBundle.dependency_comparison_status 字段默认 = 'UNAVAILABLE'
collect_runtime_identity(...)              -> 'MATCH'
collect_runtime_identity(dev 锁缺失的 root) -> 'UNAVAILABLE'
as_dict() 含 'dependency_comparison_status' -> True
```

`diff-2` 登记（`out_q_r3_final.txt`）：

```
status: fixed
note  : 缺口①…collect_dependency_lock_files；缺口②（比对结果另存）r3 起由
        compare_dependency_lock(root, declared) 真正产出 MATCH/MISMATCH/UNAVAILABLE，
        并随 IdentityBundle.dependency_comparison_status 一起交付；发布清单字段
        dependency_comparison_status 的接线在补丁 01，合入前清单侧仍为空值
```

**据实**：① 函数**真的存在并真的产出**三个值（上表实测，不是文案）；② note 明确写出
"合入前清单侧仍为空值"的未接线部分，未用 `fixed` 掩盖；③ 我核了补丁 01
（`git apply --check` 后读源），其 `_dependency_comparison_status()` 的
`MATCH/MISMATCH/UNAVAILABLE` 语义与模块函数**同口径**（README 的说法成立），
且 patch 01 才是"清单声明集合"所在侧 —— `MISMATCH` 正是从那里可达。r2 的
"声称已修但实际未修"形态**未再出现**。

**P3 备注（新登记）**：`collect_runtime_identity` 调用 `compare_dependency_lock(root)`
**不带 `declared`**，所以经身份工件这条路径只有 `MATCH`/`UNAVAILABLE` 可达，
`MISMATCH` 需要调用方自带声明集合（公开函数直达）。这不是虚假声称（注册文案描述的
是函数能力 + 清单侧接线），但"随 bundle 交付的证据位"实际只覆盖"锁文件是否齐备"。

---

## 4. 项 4（NEW-F）—— `decision_fallback.provider_env`：**CLOSED**

原始 stdout：`out_q_r3_fgh.txt`。

```
cfg provider_env='MY_FB', MY_FB=opencode        -> fallback='declared:opencode:oc-model'  provider_env='MY_FB'
cfg provider_env='MY_FB', 只设默认名            -> fallback='declared:opencode:oc-model'  provider_env='MY_FB'   (向后兼容回退)
cfg provider_env='MY_FB', 两者都未设            -> fallback='absent'                      provider_env='MY_FB'
cfg provider_env='MY_FB', 两者都设（声明名优先） -> fallback='declared:opencode:oc-model'  provider_env='MY_FB'
cfg provider_env='  '（空白 → 默认名）          -> fallback='declared:opencode:oc-model'  provider_env='MTA_AI_DECISION_FALLBACK_PROVIDER'
无 decision_fallback 配置                       -> provider_env='MTA_AI_DECISION_FALLBACK_PROVIDER'
cfg provider_env='MY_FB', MY_FB='  OpenCode  '  -> 归一化为 opencode（识别）
cfg provider_env='MY_FB', MY_FB=anthropic       -> blockers=('FALLBACK_PROVIDER_UNRECOGNISED',)
```

`ResolvedModelRoute.sources["fallback_provider_env"]` **确实暴露证据**（r2 的 C P3-1
"配置改名即静默丢备用"已消除）。`provider_env=123` 被 `_text` 转成 `'123'` 而非拒绝 ——
结果是回退到默认名，方向保守，不构成 fail-open。

---

## 5. 项 5（NEW-G）—— 控制面语义：**CLOSED**

原始 stdout：`out_q_r3_fgh.txt`、`out_q_r3_final.txt`。

```
operator emergency_stop target=close_only  -> allowed=False 400 ('EMERGENCY_STOP_TARGET_MISMATCH',)
operator emergency_stop target=paused      -> allowed=False 400 ('EMERGENCY_STOP_TARGET_MISMATCH',)
risk     emergency_stop target=close_only  -> allowed=False 400 ('EMERGENCY_STOP_TARGET_MISMATCH',)
operator emergency_stop target=emergency + confirm_secret -> True 200
operator emergency_stop target 省略（默认 emergency）+ confirm -> True 200

operator action='unspecified'  -> False 400 ('UNKNOWN_CONTROL_ACTION',)
operator action=''             -> False 400 ('UNKNOWN_CONTROL_ACTION',)
operator action='bogus_action' -> False 400 ('UNKNOWN_CONTROL_ACTION',)
health   action='unspecified'  -> False 400 ('UNKNOWN_CONTROL_ACTION',)
operator action='resolve_incident'（注册表里有角色）-> True 200

verify_control_write('bogus','bogus')  -> ok=False ('CONTROL_STATE_ILLEGAL',)
verify_control_write('paused','paused')-> ok=True  ()
verify_control_write('paused','normal')-> ok=False ('CONTROL_WRITE_UNVERIFIED',)
verify_control_write('','') / ('   ','   ') -> ok=False ('CONTROL_READBACK_MISSING',)
verify_control_write('normal','normal', read_back_ok=False) -> ok=False ('CONTROL_READBACK_FAILED',)

auto read_status / acknowledge_incident                      -> True 200
auto set_control close_only (normal→close_only, 更严)         -> True 200
auto set_control_severity_up paused (close_only→paused)       -> True 200
auto emergency_stop emergency + confirm_secret                -> True 200
auto set_control normal (paused→normal, 放宽)                 -> False 409 ('AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE',)
auto set_control_severity_down normal（放宽）                 -> False 409 ('AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE',)
auto resolve_incident                                         -> False 403 ('ROLE_NOT_PERMITTED',)
```

`unspecified` 被按"未知动作"正确归到 400（此前落到 `ROLE_NOT_PERMITTED`/403，把"动作不存在"
误报成"权限不足"）；`auto` 现在只放行只读与更严方向（`_role_allows(..., lowering=...)`）。

**P3 备注（新登记，均为更严方向，非 fail-open）**：
- `verify_control_write` 现在按**大小写敏感**查 `CONTROL_STATE_SEVERITY`：
  `('EMERGENCY','EMERGENCY')` → `CONTROL_STATE_ILLEGAL`（ok=False）。回读串若来自上游
  控制面的大小写变体，会被判成"非法状态"；属更严方向，但建议在 docstring 写明状态词表
  必须小写规范形。
- `auto` 的 `emergency_stop` 仍需 `confirm_secret='emergency'`（否则 400
  `EMERGENCY_CONFIRM_REQUIRED`）。语义上"自动收紧"被这条二次确认卡住，是否属预期
  （自动流程无法提供人工密钥）建议在 docstring 说明。

---

## 6. 项 6（NEW-H）方向判定：**成立**（登记可接受）

原始 stdout：`out_q_r3_fgh.txt`。

```
is_loopback_host('0:0:0:0:0:ffff:127.0.0.1') = False     <-- 只认 ::ffff: 缩写形式
is_loopback_host('::ffff:127.0.0.1')          = True
唯一消费者用法（runtime_identity.py:2190）: if not is_loopback_host(client_host):
                                          return PermissionDecision(False, ("NON_LOOPBACK_REJECTED",), 403)
```

**"不会放行非回环"经独立验证属实** —— 我把所有"非回环但**可能**被误判"的形态都试了一遍，
输出**全部 False**（= 拒绝）：

```
0.0.0.0 / 10.0.0.1 / 192.168.1.5 / 127.0.0.2 / ::ffff:10.0.0.1 / ::ffff:0.0.0.0
localhost.evil.com / 127.0.0.1.evil.com / 127.0.0.1:8080 / evil.com
'' / '   ' / None / ::ffff:7f00:1 / 0:0:0:0:0:ffff:7f00:0001
```

`LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})` 里没有任何非回环条目；
`is_loopback_host` 在模块内**只有 `authorize_control_request` 一个消费者**，且用法是
"False → 403"。HTTP 层复算：

```
client_host='0:0:0:0:0:ffff:127.0.0.1' + operator + emergency_stop
    -> allowed=False 403 ('NON_LOOPBACK_REJECTED',)
```

即该形态只会让**合法的本机管理请求被 403**（可用性问题），不会让任何非回环来源通过。
方向判定成立 → 按"已如实登记"处理，P3，不阻断。

---

## 7. 回归抽样（11 组，全部仍成立）

`out_q_r3_reg.txt`（PASS=23 / FAIL=1，见下）；`out_q_r3_reg2.txt`（R5 更正后 PASS）；
`out_q_r3_drift.txt`（R12）。

| 回归项 | 结果 | 证据 |
|---|---|---|
| R1 `authorize_new_entry` 省略 `expected` | ✅ 409 `IDENTITY_BINDING_UNPROVEN` | `out_q_r3_reg.txt` |
| R2 递归凭据过滤（顶层/嵌套 4 层/list 内/`token`/`private_key`） | ✅ `as_dict()` 不含任何秘密；`dropped_overrides` 记路径 | 同上 |
| R3 `SCOPE_HASH_UNBOUND`（要求 scope 而 actual 无） | ✅ `('SCOPE_HASH_UNBOUND',)` | 同上 |
| R4 mint 环境冲突（`environment='live'` + `AccountRef(demo)`） | ✅ `ExecutionModeError: refusing to silently prefer one` | 同上 |
| R5 WorkBuddy live 无能力证据 | ✅ `('PROVIDER_CAPABILITY_UNPROVEN',)`；demo 另加 `WORKBUDDY_OUTSIDE_LIVE`；paper 豁免 | `out_q_r3_reg2.txt`（注） |
| R6 `fallback_local` 可关 | ✅ `False` 且无声明账户 → `MissingRuntimeInputError`；默认档 `demo:local`/`stable=False` | `out_q_r3_reg.txt` |
| R7 模式未知按最严 | ✅ `None`/`''` 均 `WORKBUDDY_OUTSIDE_LIVE` | 同上 |
| R8 未识别备用 provider | ✅ `FALLBACK_PROVIDER_UNRECOGNISED` + `unrecognised-provider:anthropic` | 同上 |
| R9 artifact root 边界 | ✅ `../`、`src/../../..`、绝对路径 → `MalformedInputError`；`.env`/`config/.env`/`src/api_key.py` → `CredentialLeakError`；正常相对路径 ACCEPTED | 同上 |
| R10 畸形/空卡/空模型结构化拒绝 | ✅ `raw_settings=[1,2]`、`raw_agents='x'`、`runtime_instance_id='   '`、零卡策略卡、空模型 全部结构化拒绝 | 同上 |
| R11 mock 路由按模式阻断 | ✅ live/demo/testnet `MODEL_ROUTE_MOCK`；paper 空 | 同上 |
| R12 配置漂移双向敏感 | ✅ settings **11/11 CHANGED**；六类来源 **6/6 CHANGED**；注释/空白/键序/JSON 缩进 **5/5 SAME**；删除来源 → CHANGED + `missing` 可区分 | `out_q_r3_drift.txt` |

> 注（`out_q_r3_reg.txt` 里 R5 显示 FAIL）：那是**我的探针写错了两处**（把
> `PROVIDER_CAPABILITY_UNPROVEN` 当成 `model_route_blockers` 的输出，且漏了
> `env={"MTA_AI_DECISION_PROVIDER": "workbuddy"}`）。用 `resolve_model_route` 重测后
> 与 r2 报告逐字一致（`out_q_r3_reg2.txt`），**属探针缺陷，不是回归**。

---

## 8. 新发现汇总

| # | 严重度 | 问题 | 复现 |
|---|---|---|---|
| Q3-1 | **P3**（登记口径，非 fail-open） | 交叉校验只到 `environment`+`account_ref`：`RuntimeGeneration` 无哈希字段，同 env+账户但**不同进程/不同 `identity_hash`** 的身份 bundle 仍可授权该运行代（200）。docstring 说"`IdentityBundle` 里没有 runtime_instance_id"不准确（K01 `RuntimeIdentity` 有该字段），且 `authorize_new_entry(runtime_instance_id=...)` 绑的是 `expected↔generation`，绑不到 `identity↔generation`。建议改口径 + README r3 登记表补一行 | `out_q_r3_a.txt` B10/B11、`out_q_r3_final.txt` |
| Q3-2 | **P3** | `mint_runtime_generation(started_at=float('nan'/'inf'))` 通过校验并进入 `generation_id` 种子（`'nan'`/`'inf'`）。建议 `math.isfinite` | `out_q_r3_de.txt` |
| Q3-3 | **P3**（更严方向） | `verify_control_write` 按大小写敏感查状态词表：`('EMERGENCY','EMERGENCY')` → `CONTROL_STATE_ILLEGAL`。建议写明状态词表为小写规范形 | `out_q_r3_final.txt` |
| Q3-4 | **P3** | `auto` 的 `emergency_stop` 仍需 `confirm_secret='emergency'`（自动流程拿不到人工密钥）→ 400。建议写明是否预期 | `out_q_r3_final.txt` |
| Q3-5 | **P3** | `compare_dependency_lock(declared=<set>)` → `UNAVAILABLE`（`set` 不是 `Sequence`）。保守方向、不产生假 MATCH，但集合是自然入参形状 | `out_q_r3_de.txt` |
| Q3-6 | **P3** | 经 `collect_runtime_identity` 交付的 `dependency_comparison_status` 只有 `MATCH`/`UNAVAILABLE` 可达（该路径不传 `declared`），`MISMATCH` 仅公开函数直达 | `out_q_r3_de.txt`、`out_q_r3_final.txt` |
| Q3-7 | 说明（非缺陷） | `object.__new__(IdentityBundle)` 伪造 `.identity` 可得 200；属"整个 bundle 被伪造"，防线不在本函数内 | `out_q_r3_subclass.txt` |

**无未登记的 P2 fail-open；无未捕获异常/崩溃。**

---

## 9. 命令清单（原始输出全部落盘于 `_scratch_Q/`）

```bash
cd "C:/Users/Tao/Desktop/币安/mat-wt/reviews/U15/R3/r1"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"; export PYTHONUTF8=1

$PY _scratch_Q/probe_q_r3_a.py        > _scratch_Q/out_q_r3_a.txt        # NEW-A 正向 + 12 条绕过
$PY _scratch_Q/probe_q_r3_de.py       > _scratch_Q/out_q_r3_de.txt       # NEW-D 类型 / NEW-E 对照
$PY _scratch_Q/probe_q_r3_fgh.py      > _scratch_Q/out_q_r3_fgh.txt      # NEW-F / NEW-G / NEW-H
$PY _scratch_Q/probe_q_r3_reg.py      > _scratch_Q/out_q_r3_reg.txt      # 回归 R1-R11
$PY _scratch_Q/probe_q_r3_reg2.py     > _scratch_Q/out_q_r3_reg2.txt     # R5 更正（WorkBuddy）
$PY _scratch_Q/probe_q_r3_drift.py    > _scratch_Q/out_q_r3_drift.txt    # R12 配置漂移
$PY _scratch_Q/probe_q_r3_final.py    > _scratch_Q/out_q_r3_final.txt    # 假阳性 / 登记 / 补丁 / 消费者
$PY _scratch_Q/probe_q_r3_subclass.py > _scratch_Q/out_q_r3_subclass.txt # 伪造 bundle（说明项）

git diff 38029e2 abf2eda --stat                                    # 改动范围
git diff 38029e2 abf2eda -- docs/dispatch/patches/U15/a136cb5/     # 补丁未改（只 README）
for p in docs/dispatch/patches/U15/a136cb5/0*.diff; do git apply --check -p1 "$p"; done   # 4/4 rc=0
git status --short                                                 # 仅 ?? _scratch_Q/
```

我**未重跑**编排者已预验的 `smoke_contracts_v3.py` / `test_runtime_identity.py` /
`test_control_boundary.py`（明示不必重跑）；但我**静态核对**了 r3 测试新增断言
（`_scratch_Q/r3_test_diff.txt`）：新增用例是真实断言（含 `IDENTITY_GENERATION_MISMATCH`
逐项比较、`compare_dependency_lock` 三值断言、`provider_env` 改名断言），
**未见恒真/空断言**，唯一"刻意放行"的断言 `new-a-instance-difference-is-not-a-cross-mismatch`
是作者显式声明的口径选择（见 §1c-③）。

---

## 10. 未覆盖 / 无法离线验证（**不据此写"生产已验证"**）

1. **生产接线仍未验证（且仍未发生）**：`git grep runtime_identity` 在 `src/`、`scripts/`
   仍只命中 `src/core/contracts/k01_identity.py:311` 与 `scripts/smoke_contracts_v3.py:89` ——
   **模块零消费者**。4 个补丁只做 `git apply --check -p1`（4/4 rc=0，我独立复跑），**未落地**。
   因此"看板 `/api/control` 真的拒绝非环回""Demo/live 真的阻断新仓""release_manifest 真的消费
   `dependency_comparison_status`"**均未验证**，既不作为通过项也不作为失败项（集成归 I）。
2. **HTTP 层未验证**：`http_status` 200/400/403/409 与 `/api/control` 的映射只在纯函数层；
   未启动服务（且按纪律不启停服务）。
3. **`decision_fallback.provider_env` 的配置侧未验证**：我只验证了"传入 `raw_settings` 时生效"；
   真实 `config/settings.yaml` 是否声明该键、补丁 01/02 是否按声明名接线，**未验证**（属 I）。
4. **NEW-E 的清单侧未验证**：`compare_dependency_lock` 与补丁 01
   `_dependency_comparison_status()` 的"同口径"只做了**源码级比对**，未运行清单生成。
5. **`<env>:local` 回退身份 + 声明账户运行代被拒（B7）**是否会在真实运维路径造成误拒：
   需要 U02 的实际调用口径才能判定，我无法离线确认（README 已把它声明为"修复目标"）。
6. **同模型偏差**：本报告作者与 U15 作者会话同为 `deepseek-v4.1-flash`；虽为独立会话、
   独立工作区、反例全部自写、且工作区 detached 只读，但**不构成不同模型交叉审计**。
   建议 NEW-A 这类门禁语义的定性由另一模型复核一次。
7. **账本侧 `account_ref` 耦合（r2 B P1-4）未复验**：不属本次聚焦范围；我未读
   `src/exchange/trade_reconciliation.py` 的运行数据，也未访问 `data/`。

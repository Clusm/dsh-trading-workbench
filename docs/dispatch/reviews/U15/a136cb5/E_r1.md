# U15 初审报告（E 角色 · 可恢复性 / 权限工件）

- 任务：U15 运行身份、有效配置和控制权限（EF 线，作者角色 F）
- 候选：head `a136cb5`（分支 `task/U15`），基线 `ffdab44`
- 审查工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15\E\r1`（detached，只读）
- 审查角色：E（独立会话，与作者不同会话）；同模型限制见 §6
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`）
- 探针与原始输出：`_scratch_E/probe_drift.py`、`probe_failopen.py`、`probe_guards.py`、`probe_api.py`；
  对应原文 `_scratch_E/out_probe_*.txt`

## 0. 结论：**CHANGES_REQUESTED**

P0 × 1、P1 × 5、P2 × 9、P3 × 7。

模块的**纯策略语义基本成立且可独立复现**（依赖锁强制、策略卡文件级来源校验、未知 profile/模式 fail-closed、
备用路由非法参数拒绝、凭据值不外泄、环回判定、写后回读），作者两份验收脚本在我这里全 PASS。
但作为"运维身份工件"存在两个方向的实质缺陷：

1. **有效配置哈希的敏感度与风险倒挂**：把 `config/risk_profile.lock.json`（杠杆/日内止损/单笔风险）、
   `decision_policy.yaml`、`risk_learning.yaml`、`windows_service.json` 整段排除在身份之外，
   同时 `settings.yaml` 里 `ai_daily_loss_stop / require_initial_sl_tp / strategy_risk_per_trade /
   strategy_max_leverage / capital.* / execution.base_url_map` 等风险项全部落在白名单外；
   而 `src/` 下任何一个遗留诊断脚本会改身份。这与冻结契约 K01
   `EFFECTIVE_CONFIG_SOURCES`（`k01_identity.py:35-39`）直接冲突，使 F13"指纹必变"对风险漂移失效（P0-1）。
2. **门禁接口默认 fail-open**：`authorize_new_entry` 在省略 `expected` 时跳过全部账户/环境/运行代绑定，
   `readiness_allowed` 默认 `True`（P1-1）；`account_ref` 本地回退无任何标注（P1-2）。
   这两点正是 E 关注的"恢复后先对账再开仓""单账户执行者"的接口面。

另外：**本候选没有任何消费者**（`grep -rn runtime_identity src/ scripts/` 只命中契约 schema 与
`scripts/smoke_contracts_v3.py`），共享适配补丁未在候选内交付（`docs/dispatch/patches/` 不存在，
RESUME 亦记"共享适配补丁文件补交中"）。因此本模块目前**只有接口壳，没有生效路径**；
控制边界与身份门禁在运行系统上均未接线。这一点不做扣分（集成归 I），但必须写明：
本报告的"控制边界成立"只到纯函数层，**离线无法证明生产路径真的拒绝非环回请求**。

---

## 1. P0 问题

### P0-1 `effective_config_hash` 对风险配置与契约指定的 4 个来源完全失明

- 位置：`src/core/runtime_identity.py:132-156`（`SETTINGS_WHITELIST`，23 项）、
  `:941-953`（白名单采集）、`:924-983`（`collect_effective_config`）；
  契约侧：`src/core/contracts/k01_identity.py:35-39`（`EFFECTIVE_CONFIG_SOURCES`）
- 事实：
  - 白名单只覆盖 `app_name/stage/log_level`、`execution.default_mode|allow_paper_fallback`、
    `paths.*`(6)、`llm`(7)、`channels`(5)。
  - `decision_policy / risk_learning / risk_profile / windows_service` 在 `runtime_identity.py`
    中出现次数均为 **0**（见 §4 命令 4），即：文件内容既未被读取，也没有任何入参入口
    （`collect_runtime_identity`/`collect_effective_config`/`build_identity` 签名里没有它们，见
    `_scratch_E/out_probe_api.txt`）。
  - 用真实 `config/settings.yaml` 逐项漂移实测（`_scratch_E/out_probe_drift.txt`）：
    `ai_daily_loss_stop 0.30→0.99`、`require_initial_sl_tp true→false`、
    `ai_default_margin_mode ISOLATED→CROSSED`、`strategy_risk_per_trade 0.02→0.50`、
    `strategy_max_leverage 20→100`、`universal_cards false→true`、`capital.initial_equity 500→100000`、
    `reset_on_bankruptcy true→false`、`execution.base_url_map.live → https://evil.example.com`、
    `stability.risk_guard_mode audit_only→enforce`、`market_universe.core_symbols 20→1`
    **全部 `eff_cfg=same` 且 `deployment=same`**；只有白名单内的
    `execution.allow_paper_fallback` 与 `channels.strategy_weight` 会变。
  - 白名单裁剪实验：把真实 settings 裁成"只保留 23 个白名单键"，`effective_config_hash`
    与完整真实文件**逐字节同哈希**（`stripped(whitelist-only) eff hash == real settings eff hash : True`）。
- 影响：风险档、执行端点、熔断线、白名单变更（含 `risk_profile.lock.json` 的
  `max_leverage/max_risk_per_trade/daily_loss_stop`）**不改变身份**。
  身份哈希因此在最需要它的地方失效：无法回答"本次运行实际用了哪套风险配置"，
  "恢复后先对账再开仓"缺少可对账的配置指纹；U13（托管恢复）与 U09（发布证据）如需
  "同运行工件重启 vs 不同 RC 变更"的区分，也拿不到这个证据。
- 与声明冲突：`diff-3` 登记为 `fixed`（`:1478`），note 写"effective_config_hash 三字段采集"，
  但冻结契约 `EFFECTIVE_CONFIG_SOURCES` 明列六类来源；实测只落地一类。
- 复现：`PYTHONUTF8=1 <venv>/python.exe _scratch_E/probe_drift.py`

## 2. P1 问题

### P1-1 身份门禁默认 fail-open：省略 `expected` 即跳过全部绑定

- 位置：`src/core/runtime_identity.py:1302-1340`；关键分支 `:1329`（`elif expected is not None:`）、
  `:1312`（`readiness_allowed: bool = True`）
- 证据（`_scratch_E/out_probe_failopen.txt` B5）：
  ```
  expected generation : gen-bc7a3ba1a7dc37b4b04d live:A live
  actual  generation  : gen-ae0f58f97bc4bf4aa092 demo:B demo
  bundle ok , expected omitted      -> {'allowed': True, 'reason_codes': [], 'http_status': 200}
  bundle ok , expected=gen          -> {'allowed': False, 'reason_codes': ['ENVIRONMENT_MISMATCH',
     'ACCOUNT_REF_MISMATCH', 'RUNTIME_INSTANCE_MISMATCH', 'RUNTIME_GENERATION_MISMATCH'], ...}
  ```
- 即：`authorize_new_entry(bundle, generation)` 这个"最自然的调用形态"对**异环境、异账户、
  异运行代**的 generation 判 allowed=True。函数名/文档是"新开仓身份门禁（F13/DC14）"，
  但把"是否做绑定校验"交给可选的 `expected`，且 `readiness_allowed` 默认放行。
- E 视角：这正是"恢复后先对账再开仓"的反面——接口默认允许，除非调用方额外补参数。
  应由 U02/U13 绑定的接口本身不安全。修法建议：`expected` 设为必传（或缺失即
  `RUNTIME_GENERATION_UNPROVEN`），`readiness_allowed` 改为必传或默认 `False`。

### P1-2 `account_ref` 本地回退未被标注，"单账户执行者"不可验证

- 位置：`derive_account_ref` `:1005-1024`（`:1024` 回退 `f"{env}:local"`，`stable=False`）；
  `collect_runtime_identity` `:1244-1250`（从不传 `fallback_local=False`）；
  `IdentityBundle` `:1178-1209`（无 `account_ref_stable/source` 字段，无 advisory）
- 证据（`out_probe_failopen.txt` B2）：
  ```
  identity.environment : live
  identity.account_ref : live:local
  blockers             : ()
  advisories           : ('GIT_COMMIT_UNAVAILABLE',)
  entry_allowed        : True
  AccountRef.stable    : False source: local-fallback -> propagated to bundle: False
  ```
- 影响：未声明账户时身份把"哪个账户"退化成 `live:local`，且不产生任何 blocker/advisory，
  门禁照放。这相当于指纹清单 diff-7 ② 的"同代码/配置、不同账户可得到同一身份"未被真正关闭：
  只要两边都没声明 `MTA_ACCOUNT_REF`，两个真实账户的身份完全相同。
  `AccountRef.stable=False` 这个唯一的"未证明"信号**没有进入 IdentityBundle/门禁**。
- 修法建议：`stable=False` 时至少给 advisory（如 `ACCOUNT_REF_LOCAL_FALLBACK`），
  live 下应给 blocker；并把 `stable/source` 带入 `IdentityBundle.as_dict()`。

### P1-3 零卡策略卡被当作有效身份（`strategy_hash` 覆盖空内容）

- 位置：`load_strategy_cards` `:434-454`（`:452` 只拒 `len(payload) == 0`）、
  `_count_cards` `:423-431`；契约侧 `_coverage_gaps` 只拒空序列
  （`k01_identity.py:181-186`）
- 证据（`out_probe_failopen.txt` B1，端到端过 `collect_runtime_identity`）：
  ```
  normal 2 cards               card_count=2 strategy_hash=aeb390f68bf2 entry_allowed=True blockers=()
  zero cards  {'cards': []}    card_count=0 strategy_hash=6a085b092a7e entry_allowed=True blockers=()
  no cards key {'version': 3}  card_count=0 strategy_hash=9af23cea10a4 entry_allowed=True blockers=()
  ```
- 影响："改卡即改身份"只在一个方向上成立：**删光卡片**（或文件被截断/写坏成
  `{"version":3}`）依旧是合法身份且 `entry_allowed=True`，只换了 hash。
  K01 契约的口径是 `strategy_cards` "已声明且非空"；`{"cards": []}` 长度 1 被判非空，
  于是"空策略集"成为可发布状态。应要求 `card_count > 0`（或至少给 blocker）。

### P1-4 "profile 显式"未强制：live 可无 profile 放行，且无告警

- 位置：`resolve_execution_scope` `:529-567`；`collect_runtime_identity` `:1239-1243`
- 证据（`out_probe_failopen.txt` B3）：
  ```
  MTA_PROFILE absent + EXECUTION_MODE=live ->
    profile      : None  mode: live  locked: False  source: EXECUTION_MODE
    warnings     : ()
    blockers     : ()  entry_allowed: True
  ```
- 影响：`ExecutionScope.locked=False`（无 profile 语义）时仍可得到 `environment="live"` 的完整身份，
  既无 warning 也无 blocker。这保留了指纹清单 diff-3 ④ / decl-7 描述的原始风险
  （`profile=None` + `EXECUTION_MODE=live`），只是把"未知值"变成了 fail-closed。
- 与登记冲突：`decl-7` 在 `:1488` 标为 `fixed`，note 只写"未知模式/profile 拒绝"，
  未提 live 无 profile 仍放行。属**部分修复被登记为已修复**。
- 修法建议：`locked=False and mode in ("live",)` 至少产生 advisory，live 下建议 blocker。

### P1-5 登记表存在**无法复现的接口声明**（审查登记失真）

- 位置：`FINGERPRINT_R1_DISPOSITION` `:1482`，`diff-7` note：
  "account_ref 由声明值派生、与密钥解耦；**environment/状态根一致性校验接口已提供**，接线归 U13"
- 事实：模块内不存在任何状态根（state root）语义。`grep -n "state_root\|data_dir\|project_root" runtime_identity.py`
  只命中 5 行：两行是文档字符串/注释、一行是白名单字面量 `"paths.data_dir"`、两行是
  `project_root` 形参（见 §4 命令 4）。没有任何函数接收状态根、也没有任何
  "environment ↔ 状态根"一致性判定或理由码。
- 影响：这是给下游（U13/U09、以及 U00 残余关闭判定）的错误证据——按此 note，
  U13 只需"接线"即可，实际 U13 需要**新造**该校验。E 角色按手册要求核验登记诚实性，
  此处不成立。修法：把 note 改为 `registered`/`U13`，或真的补一个
  `verify_environment_state_root(environment, state_root) -> reason_codes` 级接口。

## 3. P2 / P3 问题

### P2

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| P2-1 | `collect_runtime_identity` 接受空/空白 `runtime_instance_id`，而 `mint_runtime_generation` 拒绝；该字段又在 `IDENTITY_MISMATCH_IGNORED_FIELDS` 中被忽略 → "运行代"可缺失而身份仍有效并放行 | `:1212-1235`（无校验）、`:1089-1090`（mint 有校验）、`k01_identity.py:48` | `'' -> identity.runtime_instance_id='' entry_allowed=True`；`'   ' -> ... entry_allowed=True`（§4 命令 5） |
| P2-2 | `build_identity` 接受 `environment/account_ref/git_commit/runtime_instance_id` **全为空串**的身份（K01 `RuntimeIdentity` 这些字段是裸 `str`，无 `min_length`） | `k01_identity.py:82-96`、`:212-257`（`_coverage_gaps` 不检查身份字段） | 实测 `empty-fields identity ACCEPTED: {... 'environment': '', 'account_ref': '', 'git_commit': '', 'runtime_instance_id': ''}` |
| P2-3 | `verify_account_binding` 在 `actual.scope_hash` 为 `None` 时**静默跳过** SCOPE_HASH_MISMATCH（fail-open） | `:1168-1169` | `actual.scope_hash=None, caller demands 'prod-scope' -> {'allowed': True}`；`'other' -> SCOPE_HASH_MISMATCH`（B6） |
| P2-4 | 守卫生效面不一致：`guard_no_raw_env_payload` 只作用于 `effective_config`（`:1259`），模型路由不经过它；多行 `.env` 文本可经模型名进入 `model_route_hash`，并以**明文**出现在 `IdentityBundle.as_dict()` | `:1259` vs `:1260-1264`；`ResolvedModelRoute.as_dict` `:586-594` | C1：`hash_model_route(...) -> 2b52a8547afa8b5d`（未拒绝）；C6：`raw env text present verbatim in as_dict() blob: True`。同时 `guard_no_raw_env_payload` 若被调用是能拒的（同探针第 3 行）→ 只是没调用 |
| P2-5 | 备用路由的凭据判定只看 env，忽略调用方 `credentials_present`，与主路（`:757-759`）不一致 → 忠实传布尔映射的调用方仍被硬拒 | `:680`（`bool(_text(spec_env(...)))`）vs `:757` | C2：`credentials_present={'OPENCODE_API_KEY': True}, env var absent -> FallbackRouteError fallback credential is not present` |
| P2-6 | 登记表漏项且注释声称"每一项都必须出现在这里"：报告 §3 的 `decl-2/8/9/10/11` 未登记；§2 RuntimePaths 漂移清单(A/B/C 共 6+ 条)未登记；§4"需 U15/E 后续验证"第 1 项（换 worktree 后实例锁与账户作用域互斥，K01 `:72`）与第 2 项（`MTA_RELEASE_ID` 可覆盖身份）未登记 | 注释 `:1474`，表 `:1475-1493` | `grep MTA_RELEASE_ID runtime_identity.py` = 0；表内无对应键 |
| P2-7 | `diff-2`/`decl-5` 标 `fixed`，但 K01 `IDENTITY_COVERAGE["dependency"]` 明列 `comparison_status`（"比对结果另存"）从未产出 | `:1477`、`:1486`；`k01_identity.py:42` | `grep -c comparison_status runtime_identity.py` = 0；指纹报告 diff-2 缺口② |
| P2-8 | `collect_runtime_identity(environment=...)` 形参被**静默忽略**（身份 environment 只取 `scope.mode`） | `:1215` 形参、`:1269`（只传 `scope.mode`） | B4：`passed environment='live', execution_mode='demo' -> identity.environment = demo` |
| P2-9 | 作者验收存在**空断言**：覆盖缺口用例在 lock 存在的夹具上永远走"无异常"分支并直接判 `True`；另一条用 `or` 近似恒真 | `tests/acceptance/test_runtime_identity.py:660-676`（`:674` `check(..., True, "covering lock present")`）、`:448-450` | 我独立验证真正缺口会抛错：`empty model -> IdentityCoverageError: identity coverage missing: model_route.model`（§4 命令 6）——**行为正确，但作者的这条测试没证明它** |

### P3

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| P3-1 | `emergency_stop` 动作不约束目标状态：`action=emergency_stop, requested_state=close_only` 被 allowed，审计上会记成"紧急停机"却只进 close_only | `:1424-1446` | B7：`operator emergency_stop -> close_only -> allowed True` |
| P3-2 | `action="unspecified"` 返回 `ROLE_NOT_PERMITTED/403` 而非 `UNKNOWN_CONTROL_ACTION/400`（"unspecified"在注册表里但无角色） | `:179-188`、`:1421-1422` | B7：`unknown action 'unspecified' -> ROLE_NOT_PERMITTED` |
| P3-3 | `verify_control_write` 只做文本相等，不校验状态合法：`("bogus","bogus") -> ok=True` | `:1378-1389` | B7 输出 |
| P3-4 | `auto` 角色实际上**什么都做不了**（连 `read_status` 都 403），与 docstring"自动恢复只能不变或放宽到更严"的语义不符（更严方向也拒绝） | `:179-188`、`:1448-1449` | B7：`auto read_status -> ROLE_NOT_PERMITTED`；`auto tighten normal->paused -> ROLE_NOT_PERMITTED` |
| P3-5 | 白名单键持有非标量时抛 `CredentialLeakError`（类别误导，实为形状错误），会污染"凭据外泄"告警信号 | `:874-875`、`:951-952` | C3：`credential-bearing fields rejected: effective_config.settings.llm.provider (non-scalar)` |
| P3-6 | 环回判定拒绝完整写法的 IPv4-mapped IPv6（`0:0:0:0:0:ffff:127.0.0.1`）；fail-closed 方向正确但实现不完整 | `:1348-1360`（`:1358` 要求 `count(":")==3`） | B7 loopback matrix |
| P3-7 | 哈希敏感度倒挂 + 白名单含模块从不解释的运行时开关：`src/` 下遗留 `diag_probe_leftover.py` 会改身份，而 `MTA_DRY_RUN/MTA_MARKET` 进了 `environment_overrides` 却在本模块无任何行为 | `:383-403`（artifact 收集）、`:121-129` | C5：`stray src/*.py file changes artifact/deployment hash: True`；`grep MTA_DRY_RUN --include=*.py src/` 只命中白名单自身 |

## 4. 我实际运行的命令与原始输出

> 说明：本项目无 pytest，全部脚本独立运行；所有探针只读 tracked 非敏感文件
> （`config/*.yaml`、`knowledge/strategy_cards.json`、`src/**`），未读 `.env`/`data/`/密钥，
> 未联网、未启停服务、未改任何 tracked 文件。

```bash
cd "C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15\E\r1"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"; export PYTHONUTF8=1
```

**命令 1** — 作者两份验收（不采信结论，只记录事实）

```
$ $PY tests/acceptance/test_runtime_identity.py   → U15 RUNTIME IDENTITY ACCEPTANCE PASS
  INFO fingerprint disposition: fixed=12 registered=5 ['decl-13','decl-15','decl-4','decl-6','diff-6']
$ $PY tests/acceptance/test_control_boundary.py   → U15 CONTROL BOUNDARY ACCEPTANCE PASS
```

**命令 2** — 配置漂移敏感度（真实 settings/agents/strategy_cards）

```
$ $PY _scratch_E/probe_drift.py        # 全文 _scratch_E/out_probe_drift.txt
baseline effective_config_hash : f987e139a03e995a3c87e693142035af678798d82a3b17cfbd03fef9edb8a10a
whitelist size                 : 23
channels.ai_daily_loss_stop 0.30->0.99               same      same
channels.require_initial_sl_tp true->false           same      same
signal_scanner.strategy_risk_per_trade 0.02->0.50    same      same
execution.base_url_map.live -> evil host             same      same
capital.initial_equity 500->100000                   same      same
stability.risk_guard_mode audit_only->enforce        same      same
market_universe.core_symbols 20->1                   same      same
execution.allow_paper_fallback false->true           CHANGED   CHANGED
channels.strategy_weight 0.6->0.0 [IN WHITELIST]     CHANGED   CHANGED
stripped(whitelist-only) eff hash == real settings eff hash : True
identity built with empty settings: YES
strategy payload '{"cards": []}'        -> identity BUILT, strategy_hash=6bffeba331b3
collect_runtime_identity(real repo) OK  account_ref=demo:local blockers=() entry_allowed=True
```

**命令 3** — fail-open / 空值 / 控制边界反例

```
$ $PY _scratch_E/probe_failopen.py     # 全文 _scratch_E/out_probe_failopen.txt
B1 zero cards  {'cards': []}  card_count=0 entry_allowed=True blockers=()
B2 account_ref=live:local  stable=False  blockers=()  entry_allowed=True
B3 MTA_PROFILE absent + EXECUTION_MODE=live -> profile=None mode=live locked=False warnings=()
B4 passed environment='live', execution_mode='demo' -> identity.environment = demo
B5 bundle ok, expected omitted -> {'allowed': True, ...}
B6 actual.scope_hash=None, caller demands 'prod-scope' -> {'allowed': True}
B7 operator emergency_stop -> close_only -> allowed True
   auto read_status -> ROLE_NOT_PERMITTED ; auto loosen -> AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE
   '127.0.0.1' True / '::ffff:127.0.0.1' True / '127.0.0.1:8000' False
   '0:0:0:0:0:ffff:127.0.0.1' False / requested='bogus',read_back='bogus' -> ok=True
```

**命令 4** — 契约指定来源是否进入身份

```
$ $PY _scratch_E/probe_api.py          # 全文 _scratch_E/out_probe_api.txt
K01 EFFECTIVE_CONFIG_SOURCES: settings/decision_policy/agents/risk_learning/risk_profile.lock/windows_service ...
IdentityBundle fields: ['identity','model_route','execution_scope','effective_config',
                        'strategy_source','git_state','blockers','advisories']
genuine coverage gap -> IdentityCoverageError : identity coverage missing: model_route.model

$ for pat in comparison_status decision_policy risk_learning risk_profile windows_service \
             restart heartbeat deliver instance_lock runtime_paths MTA_RELEASE_ID; do
      printf "%-20s %s\n" "$pat" "$(grep -c "$pat" src/core/runtime_identity.py)"; done
comparison_status 0 / decision_policy 0 / risk_learning 0 / risk_profile 0 / windows_service 0
restart 0 / heartbeat 0 / deliver 0 / instance_lock 1(仅 :1481 登记文本) / runtime_paths 0 / MTA_RELEASE_ID 0
```

**命令 5** — 空运行代 / 空字段身份

```
$ $PY -c "... collect_runtime_identity(runtime_instance_id=rid) ..."
''    -> identity.runtime_instance_id=''    identity_hash=b3ff1d136ad7 entry_allowed=True
'   ' -> identity.runtime_instance_id='   ' identity_hash=149ae6ba84f6 entry_allowed=True
'rt-1'-> identity.runtime_instance_id='rt-1' identity_hash=6337f410c073 entry_allowed=True

$ $PY -c "... build_identity(environment='',account_ref='',git_commit='',runtime_instance_id='') ..."
empty-fields identity ACCEPTED: {'schema_version':'3.1','environment':'','account_ref':'','artifact_hash':..., 'git_commit':'','runtime_instance_id':''}
```

**命令 6** — 真实仓库端到端（只读）

```
$ $PY _scratch_E/probe_guards.py       # 全文 _scratch_E/out_probe_guards.txt
C1 hash_model_route(raw env-shaped model) -> 2b52a8547afa8b5d   (no rejection)
C2 credentials_present={'OPENCODE_API_KEY':True}, env var absent -> FallbackRouteError
C3 MTA_PROFILE='' -> ExecutionModeError ; 白名单键持列表 -> CredentialLeakError("non-scalar")
C5 stray src/*.py changes artifact/deployment hash: True
C6 raw env text present verbatim in IdentityBundle.as_dict() blob: True
```

**命令 7** — 消费者/补丁工件核查

```
$ grep -rn "runtime_identity" --include=*.py src/ scripts/ | grep -v "^src/core/runtime_identity.py"
src/core/contracts/k01_identity.py:311:    runtime_identity: RuntimeIdentity
scripts/smoke_contracts_v3.py:89:    "k01_runtime_identity.schema.json": K01SchemaEnvelope
$ ls docs/dispatch/patches ; ls docs/dispatch/reviews/U15
ls: cannot access 'docs/dispatch/patches': No such file or directory
ls: cannot access 'docs/dispatch/reviews/U15': No such file or directory
```

## 5. 应对照的 E 审查要点逐条结论

| 要点 | 结论 |
|---|---|
| 1. "恢复后先对账再开仓""单账户执行者""profile 显式""人工禁用 live 优先于自动恢复"是否有可验证语义 | **部分成立，均为纯策略**。"人工禁用优先"在策略层成立且有理由码（`out_probe_failopen.txt` B7）；"profile 显式"未强制（P1-4）；"单账户执行者"不可验证（P1-2，无状态根校验、无账户稳定标记）；"恢复后先对账再开仓"在本模块**无钩子**（`authorize_new_entry` 只有 `readiness_allowed` 这个外部结论位，无 reconciled 事实位，且默认放行 P1-1）。且全模块**零消费者**（命令 7）→ 目前是接口壳。 |
| 2. 控制边界断言是否真的成立 | **纯函数层成立**：非环回 403（先于角色判定，无角色探测后门）、health 不可下发控制、risk 不可放宽、operator 放宽需 `confirm_manual_reenable`、emergency 双向需 `confirm_secret=="emergency"`、`AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE`（auto 放宽必 409）、`verify_control_write` 三态齐全——以上我逐条独立复现（B7）。**生产层未接线**：`src/dashboard/app.py` 未改、无补丁、无消费者，故"看板控制接口真的拒绝非环回请求"离线**无法证明**，本报告不写"通过"。另有 P3-1/P3-2/P3-3 三处语义粗糙。 |
| 3. 身份哈希对配置漂移的敏感性；空值/缺失是否被当有效值 | 依赖锁 ✅、策略卡 ✅（但对"零卡"失效 P1-3）、模型覆盖 ✅（仅 agents.yaml 的 model/effort，temperature/max_tokens 等不入哈希）、profile 覆盖 ✅（仅 `settings.profiles.<p>` 段）。风险配置 ✗（P0-1）。空值/缺失被当有效值：`settings={}` 可建身份、`{"cards":[]}` 可建身份、四类身份字段可全空、空 `runtime_instance_id` 可放行（P2-1/P2-2、P1-3）。 |
| 4. 与 U13 的交接面 | `RuntimeGeneration`/`mint_runtime_generation`/`rotate_credential`/`derive_account_ref` 提供了数据结构与轮换解耦；diff-6（运行代持久化+实例锁）归 U13 **登记诚实**。**但**：实例锁↔运行代绑定、`runtime_instance_id` 持久化都只有"登记"没有接口；"换代码目录后两实例可同管同一账户"（K01:72）与 `MTA_RELEASE_ID` 伪装**完全未登记**（P2-6）；`diff-7` 的"状态根校验接口已提供"是**虚假声明**（P1-5）；恢复后对账钩子缺失（要点 1）。因此交接面**不足以直接支撑 U13**，U13 需自行新造状态根校验、实例锁绑定与对账钩子。 |
| 5. E 禁止模式（心跳当健康/无限重启/虚拟送达） | **本模块不存在**：`grep restart/heartbeat/deliver` 均为 0（命令 4）。这三类的真实风险在 U13/U14（不在本候选范围），但需注意 P3-7 的"敏感度倒挂"会让运维误以为身份哈希代表运行健康。 |
| 未覆盖项 | 见 §7。 |

## 6. 同模型偏差声明

本报告作者与 U15 作者会话均为 `deepseek-v4.1-flash`（同模型）。虽为独立会话、独立工作区、
反例全部自写、不引用作者期望，但**不构成不同模型交叉审计**。P0/P1 的关闭判定建议由另一模型复核。

## 7. 未覆盖与无法离线验证

1. **看板/服务端真实接线未验证**：`src/dashboard/app.py`、`src/core/entry_readiness.py`、
   `src/core/config_loader.py`、`src/core/release_manifest.py` 本候选均未改，也无补丁工件
   （命令 7）。因此"非环回拒绝在 HTTP 层生效""写入回读在控制面生效""release_manifest 使用新身份"
   全部**未验证**，不作为通过项，也不作为失败项。
2. **未运行真实仓库端到端服务路径**：按硬约束未启动服务、未改配置；`scripts/run_offline_regression.py`
   未执行（未确认其是否写运行目录，保守跳过）。作者脚本我只跑了 `tests/acceptance/` 两个文件。
3. **U02/U13 的最终绑定未验证**：`verify_account_binding`/`authorize_new_entry` 只有接口契约，
   没有 U02 调用点可查。
4. **同模型限制**：见 §6。
5. **U15 独有的"审计字段消费者"未验证**：`IdentityBundle.as_dict()` 是否被写库/写盘
   （指纹报告显示 `release_manifest.json` 的消费方不存在）无法在本候选内确认。
6. **未读**：`.env`/`data/`/密钥/账户标识（按硬约束）；本报告所有断言均给 `file:line` 与原始 stdout，
   无法定位者标"未验证"。

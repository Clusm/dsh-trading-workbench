# U15 r2 独立复验报告（Q 角色 · 修复轮 r2）

- 复验对象：`38029e2`（分支 `task/U15`，父链 `6dd0d1a` → 9 个 r2 提交），上一版 `a136cb5`
- 复验工作区（独立、detached、只读）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15\R2\r1`
- 上一轮报告（已独立通读）：`I/docs/dispatch/reviews/U15/a136cb5/{E,B,C}_r1.md`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 本报告全部结论来自**我自己写的探针**（`_scratch_Q/`），不采信作者的前后对照；原始 stdout 全部落盘
- 只读纪律：未提交/推送/打标签/切分支；未读 `.env`/`data/`/密钥/账户标识；未联网；未启停服务；
  未写入 `mat-wt/I`、`mat-wt/EF` 或任何其它 worktree（`git status --short` 仅显示 `?? _scratch_Q/`）

---

## 0. 结论

### **CHANGES_REQUESTED**

**唯一阻断项：NEW-A（P2，未登记 fail-open）** —— `authorize_new_entry` 不交叉校验
`IdentityBundle.identity`（environment / account_ref）与它要授权的 `RuntimeGeneration`。
demo 身份的 bundle + live 且异账户的 generation（含把 `expected`/`environment`/`account_ref`/
`runtime_instance_id`/`scope_hash` 全部显式绑定到该 generation）→ `allowed=True / http 200 /
reason_codes=()`。反向（live bundle + demo generation）同样 200。

**其余全部闭合**：两个 P0 均已真正闭合，且我用独立反例做了反向验证（下 §2、§3）。
brief 指定的 12 项里 **11 项 CLOSED**，第 1 项在 brief 列出的 6 类绕过向量上全部 CLOSED、
但被我另找到 1 条新绕过路径（NEW-A）。作者上版的 P0×2 / 大量 P1 已修复并被我逐条独立复现；
**无任何"零消费者即生产已验证"的虚假声明**（本模块在 `src/`、`scripts/` 内仍是**零消费者**，见 §9.1）。

| 项 | 判定 |
|---|---|
| 1 `authorize_new_entry` fail-open | **PARTIAL**：brief 列出的 6 类向量 + 我新增 13 条 + 225 格网格全部 `allowed=False`；**但 NEW-A 仍可拿到 200** → 见 §1 |
| 2 配置漂移失明 | **CLOSED**（11/11 settings 漂移 + 6/6 来源文件漂移 + 5 删除全部 CHANGED；中性编辑 5/5 SAME；敏感度倒挂消除） |
| 3 嵌套凭据泄漏 | **CLOSED（递归）**；残余：全角/同形字键（README §12 L1 已登记）+ 单行 `NAME=VALUE` 值不扫描（L1） |
| 4 `scope_hash` | **CLOSED** |
| 5 `mint_runtime_generation` AccountRef 环境冲突 | **CLOSED** |
| 6 WorkBuddy live 无凭据 | **CLOSED** |
| 7 `fallback_local` 可显式关闭 | **CLOSED** |
| 8 `model_route_blockers` 模式未知 | **CLOSED**（None/"" 均按最严） |
| 9 未识别备用 provider / 非 ai_trader | **CLOSED** |
| 10 `artifact_relatives` root 边界 | **CLOSED** |
| 11 畸形 / 空输入结构化拒绝 | **CLOSED** |
| 12 登记真实性（diff-7 / decl-7） | **CLOSED**（另有 E P2-7 `comparison_status` 未闭合且未登记，P2 非 fail-open，见 §5） |

回归复验：**11/11 组全部仍成立**（§6）。

---

## 1. 项 1 — `authorize_new_entry` fail-open（PARTIAL）

### 1a. brief 列出的绕过向量：全部 CLOSED（原始 stdout：`out_p0_1.txt`）

夹具：真实仓库（`knowledge/strategy_cards.json` + `requirements-lock.txt`）以 `raw_settings`/
`credentials_present` 显式输入建 demo bundle（`blockers=()`）；`gen_ok`=demo/`demo:acct-demo`/
`rt-q-1`/`scope-A`；`gen_foreign`=live/`live:someone-else`/`rt-OTHER`/`scope-B`。

```
B5  bundle ok, expected omitted                        -> False 409 ('IDENTITY_BINDING_UNPROVEN',)
B5b bundle ok, expected omitted (foreign gen)          -> False 409 ('IDENTITY_BINDING_UNPROVEN',)
B5c expected omitted + readiness omitted               -> False 409 ('IDENTITY_BINDING_UNPROVEN','READINESS_UNPROVEN')
B5d bundle ok, expected=gen                            -> True  200 ()
B5e bundle ok, foreign gen, expected=gen               -> False 409 ('RUNTIME_GENERATION_MISMATCH','ACCOUNT_REF_MISMATCH','ENVIRONMENT_MISMATCH','SCOPE_HASH_MISMATCH')
C1  expected=None (显式)                               -> False 409 ('IDENTITY_BINDING_UNPROVEN',)
C2  expected=gen + scope_hash 正确                     -> True  200 ()
C2b expected=gen + scope_hash 错误                     -> False 409 ('SCOPE_HASH_MISMATCH',)
C3  expected=gen + account_ref 对 / environment 错     -> False 409 (4 个 mismatch)
C3b expected=gen + account_ref 对 / instance 错        -> False 409 ('ACCOUNT_REF_MISMATCH','RUNTIME_INSTANCE_MISMATCH')
C4  readiness_allowed=True                             -> True  200 ()
C4b readiness_allowed=False                            -> False 409 ('READINESS_BLOCKED',)
C4c readiness_allowed 省略                             -> False 409 ('READINESS_UNPROVEN',)
C5  expected_git_commit 不匹配                         -> False 409 ('GIT_COMMIT_MISMATCH',)
C6  expected=dict                                      -> False 409 ('RUNTIME_GENERATION_MALFORMED',)
C6b generation=None                                    -> False 409 ('RUNTIME_GENERATION_UNPROVEN',)
C6c bundle=None                                        -> False 409 ('IDENTITY_UNPROVEN',)
C6d bundle=object()                                    -> False 409 ('IDENTITY_MALFORMED',)
C7  expected/gen 同 gen_id 但 scope 不同（均非空）     -> False 409 ('SCOPE_HASH_MISMATCH',)
C8  expected=gen + environment/account_ref/instance='' -> False 409 (4 个 mismatch)
C9  225 格网格（expected×generation×readiness×scope_hash）: non-clean=4
    4 格 allowed=True 全部是 expected == generation 且 scope 自洽的**合法**组合，非绕过
```

上一版的 `True/200`（E B5 / B P3 / C 8.1）**已不复存在**。`expected` 缺省 → `IDENTITY_BINDING_UNPROVEN`／
`readiness_allowed` 缺省 → `READINESS_UNPROVEN`，与声明一致。

### 1b. NEW-A（P2，未登记 fail-open）—— 可拿到 `allowed=True` 的绕过路径

`authorize_new_entry` 只用 `bundle.blockers` / `bundle.git_state`，**从不比较 `bundle.identity`
与 `generation`**。因此任何"身份 bundle 与运行代不一致"的调用对都会静默 200。原始 stdout：`out_new_a.txt`

```
bundle.identity  : demo demo:acct-demo identity_hash=df0544c8108aed63
generation       : live live:live-acct-other rt-live gen_id=gen-dadef3e15fa302851e0f

A1 bundle=demo  gen=live   expected=gen                          -> allowed=True http=200 reasons=()
A2 A1 + 显式 environment='live'/account_ref=gen.account_ref/
      runtime_instance_id='rt-live'/scope_hash='scope-L'         -> allowed=True http=200 reasons=()
A3 bundle=live  gen=demo   expected=gen                          -> allowed=True http=200 reasons=()
A4 control（bundle=demo, gen=demo）                              -> allowed=True http=200 reasons=()
```

- **复现**：`PYTHONUTF8=1 <venv>/python.exe _scratch_Q/probe_new_a.py`
- **为什么算 fail-open**：函数 docstring（`runtime_identity.py:1861-1867`）自述"返回 200 只可能来自
  '身份、运行代、readiness 三者都被真正校验过'的路径"，并说 `expected` 缺失是"**无法证明本次运行与
  期望账户/环境/运行代绑定**"。A1/A2/A3 里"本次运行"的身份（bundle，environment=demo 或 live）
  与"期望运行代"的 environment/account_ref 直接矛盾，却仍得到 200 且 `reason_codes=()` —— 没有任何
  证据位置能区分"真的绑定成功"和"身份跟运行代根本不是一回事"。这正是本任务要消灭的 DC14/F13 形态。
- **可达性**：模块内**零消费者**，所以现在只到接口面；U02 接线后即是真实门禁路径。
- **建议**（一行级别的加严）：`bundle` 存活时，把 `bundle.identity.environment` 与
  `generation.environment`、`bundle.identity.account_ref` 与 `generation.account_ref` 逐项比对，
  不一致给 `IDENTITY_GENERATION_MISMATCH`（→409）；或在 docstring 明确写"bundle 与 generation
  的一致性由调用方保证，本函数不校验"**并登记**。
- **公平性说明（供编排者复核）**：也可以论证这是"调用方契约"而非模块缺陷 —— K04
  `validate_authorization` 会再比 `request.environment == authorization.environment`，形成第二道防线。
  但本模块 docstring 的措辞与"门禁"定位支持把它当缺口；我按"未登记的 P2 fail-open"处理。

---

## 2. 项 2（P0-2）— 配置漂移失明：**CLOSED**（原始 stdout：`out_p0_2.txt`）

夹具：临时目录内复制**真实** `config/`（六类来源）与 `knowledge/`（tracked 非敏感文件），
逐项改动后重算 `effective_config_hash`（`collect_source_config_files` + `collect_effective_config`
+ canonical JSON sha256）与 `identity_hash`。

### 2a. 漂移项必须 CHANGED —— 11/11 全 CHANGED

```
channels.ai_daily_loss_stop          0.3   -> 0.99                  eff=CHANGED ident=CHANGED
channels.require_initial_sl_tp       True  -> False                 eff=CHANGED ident=CHANGED
execution.base_url_map.live          binance -> evil.example.com    eff=CHANGED ident=CHANGED
signal_scanner.strategy_max_leverage 20    -> 100                   eff=CHANGED ident=CHANGED
capital.initial_equity               500   -> 100000                eff=CHANGED ident=CHANGED
stability.risk_guard_mode            audit_only -> enforce          eff=CHANGED ident=CHANGED
market_universe.core_symbols         20 项 -> 1                     eff=CHANGED ident=CHANGED
ai_default_margin_mode               (absent) -> CROSSED            eff=CHANGED ident=CHANGED
signal_scanner.strategy_risk_per_trade 0.02 -> 0.50                 eff=CHANGED ident=CHANGED
reset_on_bankruptcy                  (absent) -> False              eff=CHANGED ident=CHANGED
universal_cards                      (absent) -> True               eff=CHANGED ident=CHANGED
```

上一版这些全部 `same`（E_r1 §1 P0-1）；**现已全部 CHANGED**。

### 2b. K01 六类来源逐类 CHANGED + 缺失可区分

```
config_sources keys = ['agents','decision_policy','risk_learning','risk_profile.lock','settings','windows_service']
missing_sources = ()          # 六类齐备
risk_profile.lock version bump  -> eff=CHANGED ident=CHANGED
risk_profile.lock sha256 change -> eff=CHANGED ident=CHANGED
decision_policy / risk_learning / windows_service / agents 改动 -> 均 CHANGED
删除 decision_policy.yaml   -> eff=CHANGED missing=('config/decision_policy.yaml',)
删除 risk_learning.yaml     -> eff=CHANGED missing=('config/risk_learning.yaml',)
删除 risk_profile.lock.json -> eff=CHANGED missing=('config/risk_profile.lock.json',)
删除 windows_service.json   -> eff=CHANGED missing=('config/windows_service.json',)
删除 agents.yaml            -> eff=CHANGED missing=('config/agents.yaml',)
require_source_configs=True 且来源缺失 -> MissingRuntimeInputError（结构化拒绝）
```

（注：`config/risk_profile.lock.json` 本身是**哈希锁**（`version/description/expected/sha256`），
真实风险数值在 `settings.yaml`（`signal_scanner.*` / `channels.*` / `capital.*`）—— 这两处现在都进哈希。）

### 2c. 反向验证：中性改动**不得**改变哈希 —— 5/5 SAME（`out_reverse.txt`）

```
comment line in decision_policy.yaml        -> SAME
trailing blank lines in risk_learning.yaml  -> SAME
key order rewrite of agents.yaml            -> SAME
JSON reindent of windows_service.json       -> SAME
JSON reindent of risk_profile.lock.json     -> SAME
（另：settings.yaml 加注释 / 键序重排 / 尾部空行 -> 全部 SAME，`out_p0_2.txt` §B）
```

即：**没有**"什么都进哈希"的过度敏感；注释、空白、键序、JSON 缩进均不影响身份。

### 2d. `src/` 敏感度倒挂 —— 已消除

```
add src/diag_probe_leftover.py -> identity CHANGED   （artifact_hash 默认取 src/**/*.py，属代码身份）
add src/notes.txt (非 .py)     -> identity SAME
add src/__pycache__/x.pyc      -> identity SAME
```

上一版的"倒挂"是**风险配置不进哈希、而遗留脚本进哈希**。现在风险配置已进哈希，倒挂消失；
`src/*.py` 仍进 `artifact_hash` 属"代码身份"的正向设计（`collect_artifact_files:469-498`，
忽略 `IDENTITY_ARTIFACT_IGNORED_PARTS`），不是缺陷。

---

## 3. 项 3 / 10 / 11 — 凭据泄漏、工件边界、畸形输入（原始 stdout：`out_leak.txt`）

### 3. 嵌套凭据过滤：**CLOSED（递归）**；2 条残余

```
3a 顶层 api_key                  secret_survived=False dropped=('profile_overrides.api_key',)
3b 嵌套 llm.api_key              secret_survived=False dropped=('profile_overrides.llm.api_key',)
3c 深 4 层 a.b.c.api_key         secret_survived=False
3d list 内 dict ls[0].api_key    secret_survived=False
3g token                         secret_survived=False
3h auth.password                 secret_survived=False
3i wallet.private_key            secret_survived=False
3j LLM.API_KEY（大写）            secret_survived=False
3f 全角同形字 ｌｌｍ.ａｐｉ＿ｋｅｙ   secret_survived=True   dropped=()      <-- 残余
3k 单行 "BINANCE_LIVE_API_KEY=SK-..." 作为**值**  secret_survived=True          <-- 残余
3-extra 端到端 IdentityBundle.as_dict() 含秘密 -> False（已接线路径干净）
```

- 上版 C P1-1 的 `4i.2 secret survived True` **已闭合**（递归 `_sanitise_source_payload` 生效，
  并且丢弃路径记入 `dropped_overrides`）。
- **残余 = 全角/同形字键**（`is_sensitive_name` 只做 `str(name).lower()` 子串匹配，无 NFKC 归一化；
  `runtime_identity.py` 内 `grep unicodedata|NFKC` 零命中，`normalize` 仅命中
  `contracts/common.py` 的 `Decimal.normalize`，与 Unicode 归一化无关）。这条**已登记**：README §12 `L1`
  （"敏感守卫只查键名/载荷形态，不扫描值内容"，下游 U15），且 U00 的 Q 复验 r1/r2/r3 已把
  "全角同形字键 / 秘密仅在 value"显式登记为 `P2-8 PARTIAL（已登记未修）`。故按标尺属**已登记 P2 残余**。
- **残余 = 单行 `NAME=VALUE` 值**：`_looks_like_env_text` 要求 `>=2` 行且过半是赋值行
  （`runtime_identity.py:325-334`），单行赋值不拦。同属 `L1` 的"值不扫描"口径。
- `guard_no_raw_env_payload` 本身可拒多行 env 形态（探针末段实测能拒）；问题只在调用面覆盖不到
  这两类，且 `resolve_model_route` 的 `profile_overrides` 是 U05 直接调用的公开入口。

### 10. `artifact_relatives` root 边界：**CLOSED**

```
10a 绝对路径（root 外）        -> MalformedInputError（绝对或逃逸路径）
10b src/../../../../xxx        -> MalformedInputError
10c ../xxx                     -> MalformedInputError
10h sub/../../xxx              -> MalformedInputError（resolve 后 is_relative_to 复核）
10d .env                       -> CredentialLeakError
10e config/.env                -> CredentialLeakError
10f src/api_key.py             -> CredentialLeakError（键名命中敏感标记）
10g src/x.py                   -> ACCEPTED（正常）
```

上版 C P1-2 的"绝对路径可把 root 外文件（含 `.env`）读进 `artifact_hash`"**已闭合**；
另有 `_MAX_ARTIFACT_BYTES = 2 MiB` 上限。探针用临时目录里的假秘密，未触碰真实 `.env`。

### 11. 畸形 / 空输入：**CLOSED**

```
11a collect_effective_config(raw_settings=[1,2])   -> MalformedInputError: raw_settings must be a mapping
11b collect_runtime_identity(raw_settings=[1,2])   -> MalformedInputError
11c resolve_model_route(raw_settings=[1,2])        -> MalformedInputError
11d resolve_model_route(raw_agents='x')            -> MalformedInputError
11e resolve_model_route(env=[1])                   -> MalformedInputError
11f resolve_model_route(credentials_present=[1])   -> MalformedInputError
11g resolve_model_route(profile_overrides=[1])     -> MalformedInputError
11h resolve_model_route(provider_available=[1])    -> MalformedInputError
11i collect_runtime_identity(env=[1])              -> MalformedInputError
11j collect_runtime_identity(credentials_present=[1]) -> MalformedInputError
11k2 策略卡 {"cards": []} 端到端                    -> MissingRuntimeInputError（no strategy cards）
11k3 策略卡 {"version": 3} 端到端                   -> MissingRuntimeInputError
11l 空模型 resolve_model_route                     -> ModelRouteConfigError: MODEL_ROUTE_UNSET（本模块异常，非 pydantic ValidationError）
11m 空模型端到端                                    -> ModelRouteConfigError
11n runtime_instance_id=''                         -> MissingRuntimeInputError
11o runtime_instance_id='   '                      -> MissingRuntimeInputError
深 3000 层 / 自引用 profile_overrides               -> ACCEPTED（深度上限 24 截断，**不再 RecursionError**）
```

对比上一版：C P1-3（非 Mapping 静默当空配置）、E P1-3（零卡策略卡当有效身份）、C P2-5
（空模型裸 `ValidationError`）、E P2-1/P2-2（空运行代）、C P3-6（深嵌套 RecursionError）
**全部闭合**。唯一残余：`mint_runtime_generation(credential_epoch='3')` 仍抛裸
`TypeError: '<' not supported between instances of 'str' and 'int'`（`out_crosscheck.txt`）—— P3，非 fail-open。

---

## 4. 项 4 / 5 / 6 / 7 / 8 / 9

### 4. `scope_hash` 参与比较：**CLOSED**（`out_p0_1.txt` §D）

```
D1 调用方要求 scope-A，actual.scope_hash=None  -> False ('SCOPE_HASH_UNBOUND','SCOPE_HASH_MISMATCH')
D2 expected.scope=A, actual.scope=B            -> False ('SCOPE_HASH_MISMATCH',)
D3 两边都 None                                 -> True  ()
D4 expected 无 scope，actual 有                -> False ('SCOPE_HASH_MISMATCH',)
D5 actual=None                                 -> False ('RUNTIME_GENERATION_UNPROVEN',)
D6 expected=dict                               -> False ('RUNTIME_GENERATION_MALFORMED',)
```

上版 B P1-1 的两个缺口（`actual.scope_hash=None` 静默跳过、`expected.scope_hash` 从不比较）
均已闭合；`authorize_new_entry` 的 `scope_hash` 参数经同一函数生效（C2/C2b/C7）。

### 5. `mint_runtime_generation` 环境冲突：**CLOSED**（`out_p0_1.txt` §E）

```
E1 mint(environment='live', AccountRef(demo)) -> ExecutionModeError: environment 'live' conflicts with AccountRef.environment 'demo'; refusing to silently prefer one
E3 mint(environment='')     -> MissingRuntimeInputError
E3 mint(environment='   ')  -> MissingRuntimeInputError
E3 mint(environment='prod'/'unknown') -> ExecutionModeError: unknown environment; allowed: paper, demo, testnet, live
E3 mint(environment='LIVE'/'Live')    -> OK env='live'（大小写归一）
E4 mint(account_ref='binance-main').account_ref            -> live:binance-main
E5 mint(account_ref=derive_account_ref('live','binance-main')) -> live:binance-main   （两者相等）
```

上版 B P1-2（环境静默忽略）、B P1-3（裸字符串不补 `env:` 前缀 → 同账户两个 account_ref）、
B P2-3（环境值不校验）全部闭合。

### 6. WorkBuddy live：**CLOSED**（`out_routes.txt` §6）

```
6  mode=live   provider=workbuddy mock=False evidence='unproven' blockers=('PROVIDER_CAPABILITY_UNPROVEN',)
      mock_source='provider_local_cli'          <-- 不再是 'credential_present'
6  mode=demo   blockers=('WORKBUDDY_OUTSIDE_LIVE','PROVIDER_CAPABILITY_UNPROVEN')
6  mode=paper  blockers=()                      <-- paper 豁免
6b provider_available={'workbuddy': True}  -> evidence='declared_available' blockers=()
6b provider_available={'workbuddy': False} -> blockers=('PROVIDER_UNAVAILABLE',)
6b provider_available 未声明              -> blockers=('PROVIDER_CAPABILITY_UNPROVEN',)
6c live workbuddy 无凭据端到端 authorize_new_entry -> False 409 ('PROVIDER_CAPABILITY_UNPROVEN',)
6d 对照 sensenova 同样条件                          -> False 409 ('MODEL_ROUTE_MOCK',)
```

上版 B P1-6 的两条（缺凭据不阻断、写入伪证据 `mock_source="credential_present"`）均闭合。

### 7. `fallback_local` 可显式关闭：**CLOSED**（`out_routes.txt` §7）

```
demo fallback_local=None -> account_ref='demo:local' stable=False blockers=()  advisories 含 ACCOUNT_REF_LOCAL_FALLBACK
demo fallback_local=False -> MissingRuntimeInputError: account_ref (declared)
live fallback_local=None -> account_ref='live:local' entry_allowed=False blockers=('ACCOUNT_REF_UNPROVEN',)
live fallback_local=True -> entry_allowed=True blockers=()（显式知情，只留 advisory）
live fallback_local=False -> MissingRuntimeInputError
```

上版 B P1-5 / E P1-2 闭合：生产入口可 `fallback_local=False` 硬拒；默认档在 live 下**阻断**
（不再静默 `live:local` 跨账户碰撞）；`IdentityBundle.account_ref_stable/account_ref_source`
使回退事实可被下游检测。

### 8. `model_route_blockers` 模式缺失按最严：**CLOSED**（`out_routes.txt` §8）

```
execution_mode=None  environment=None -> ('WORKBUDDY_OUTSIDE_LIVE',)
execution_mode=''    environment=''   -> ('WORKBUDDY_OUTSIDE_LIVE',)
execution_mode=demo  -> ('WORKBUDDY_OUTSIDE_LIVE',)
execution_mode=live  -> ()
execution_mode=paper -> ()
```

上版 C P1-5 的"默认 `None` 永不阻断"闭合（`effective_mode != "live"` 判定，未知即非 live）。

### 9. 未识别备用 provider / 非 ai_trader：**CLOSED**（`out_routes.txt` §9）

```
fallback='opencode'        -> routes=(opencode…), status='declared:opencode:union-alpha', blockers=()
fallback='anthropic'       -> routes=(), status='unrecognised-provider:anthropic', blockers=('FALLBACK_PROVIDER_UNRECOGNISED',)
fallback='none'            -> blockers=('FALLBACK_PROVIDER_UNRECOGNISED',)
fallback=''                -> status='absent', blockers=()
fallback='OPENCODE'/'OpenCode'/'  opencode  ' -> 归一化为 opencode（识别）
9b agent=ai_trader      -> routes=(opencode…)
9b agent=market_analyst/risk_manager/other -> routes=(), status='skipped:agent-not-ai-trader'
9d 备用凭据取 credentials_present（True -> 建备用；False -> FallbackRouteError）
```

上版 C P1-4（未识别静默丢弃）、C P1-6（非 ai_trader 也生成备用，与 `factory.py:61` 不符）
均闭合。**残余 P3（C P3-1 未修）**：`settings.llm.decision_fallback.provider_env` 仍被忽略
（`resolve_model_route:950` 硬编码 `MTA_AI_DECISION_FALLBACK_PROVIDER`）→ 9c 配 `MY_FALLBACK_ENV=opencode`
得到 `status='absent'`，配置改名后备用静默丢失。

---

## 5. 项 12 — 登记真实性：**CLOSED**（原始 stdout：`out_regression.txt` §12/§12b）

- `diff-7` 现在 `status=registered`，owner `U13`；note 如实写"状态根一致性只到**纯函数判定**
  `verify_environment_state_root(...)` + config_loader 补丁的 `assert_environment_matches_state_root`；
  RuntimePaths 生成、启动期拦截与端到端验证**未做**"。而且这个接口**真的存在**（上版 E P1-5 的
  "虚假声明"已被真实实现取代）：
  ```
  env=live state_root=data     profile=None -> ('STATE_ROOT_PROFILE_UNLOCKED','LIVE_SHARED_STATE_ROOT')
  env=live state_root=data     profile=live -> ('LIVE_SHARED_STATE_ROOT',)
  env=live state_root=data/live profile=live -> ()
  env=live state_root=None     -> ('STATE_ROOT_UNBOUND','STATE_ROOT_PROFILE_UNLOCKED')
  env=demo/paper               -> ()      env=prod -> ('ENVIRONMENT_UNBOUND',)
  env=live state_root=data     profile=demo -> ('ENVIRONMENT_STATE_ROOT_MISMATCH','LIVE_SHARED_STATE_ROOT')
  ```
- `decl-7` 现在 `fixed` 且**实测成立**：
  ```
  MTA_PROFILE absent + EXECUTION_MODE=live -> profile=None locked=False
     blockers=('EXECUTION_SCOPE_UNLOCKED',) advisories 含 EXECUTION_PROFILE_NOT_DECLARED
     entry_allowed=False
  profile=live -> locked=True blockers=()
  execution_mode='prod' / profile='prod' / execution_mode='' -> ExecutionModeError（结构化拒绝）
  ```
- 登记表共 **28 项**，E P2-6 列举的漏项（`decl-2/8/9/10/11`、`fu-1`、`fu-2`、`fu-paths`）已补齐；
  `decl-14` note 如实写"执行层接线见本目录补丁 01/04，合入前运行系统仍无强制力"。
- **残余（P2，非 fail-open）**：E P2-7 的 `comparison_status` 仍未产出
  （`grep -c comparison_status runtime_identity.py` = 0），而 `diff-2` 仍标 `fixed`，note 只覆盖
  "依赖锁纳入 requirements-lock.txt"（对应 F 指纹报告 diff-2 缺口①），**缺口②（`verify_dependency_lock.py`
  比对结果"另存"未实现）既未修也未以 `registered` 登记**。这条不属 fail-open，但按"登记诚实性"
  口径建议补一句 note。
- 4 个共享适配补丁 `git apply --check -p1` **全部 OK**（我独立复跑）；`02_config_loader.diff`
  只 import `collect_effective_config/collect_source_config_files/verify_environment_state_root`，
  判定委托单一来源，且 `assert_environment_matches_state_root` 仅在 `environment == "live"` 时收紧。

---

## 6. 回归复验（**11/11 全部仍成立**）

| 回归项 | 结果 | 证据 |
|---|---|---|
| 非环回 403 | ✅ | `10.0.0.1`/`None` → 403 `NON_LOOPBACK_REJECTED`（先于角色判定）；环回矩阵 15/15 与上版一致（`::ffff:127.0.0.1` True，`0:0:0:0:0:ffff:127.0.0.1` 仍 False=P3 未修） |
| health 只读 | ✅ | `health set_control` → 403；`health read_status` → 200 |
| risk 仅更严 | ✅ | `risk loosen` → 403；`risk tighten` → 200 |
| operator 放宽需 confirm | ✅ | 无 `confirm_manual_reenable` → 409 `MANUAL_REENABLE_NOT_CONFIRMED`；带 confirm → 200；从 emergency 退出另需 `confirm_secret` → 409 `EMERGENCY_EXIT_NOT_CONFIRMED` |
| `AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE` | ✅ | auto 放宽 → 409 |
| 写后回读校验 | ✅ | `('paused','paused')` ok；`('paused','normal')` → `CONTROL_WRITE_UNVERIFIED`；`('','')`/`('   ','   ')` → `CONTROL_READBACK_MISSING` |
| 空/未设置/空白不产生虚假覆盖 | ✅ | `{}` / `''` / `'   '` / `'\n'` → 与未设置**同 hash**；设值 → 不同 |
| 非白名单秘密不进身份 | ✅ | `BINANCE_LIVE_API_KEY=SK-...` → 同 hash，序列化不含该值 |
| mock 在 demo/live 阻断而 paper 不阻断 | ✅ | live/demo/testnet route → `('MODEL_ROUTE_MOCK',)`；paper → `()`，`entry_allowed=True` |
| 覆盖优先级与 `factory.py:61` 一致 | ✅ | `agents.yaml[agent].model` > `environment:<model_env>` > `settings.llm.default_model`；备用仅 `ai_trader` 且 provider=opencode |
| 策略卡文件级来源校验 | ✅ | 正常 OK；加载后改文件 → `StrategySourceMismatchError` |
| 额外：`account_ref` 与轮换解耦 | ✅ | `rotate_credential` epoch 0→1，`account_ref`/`generation_id` 不变，指纹 16 hex 不含原文 |
| 额外：作者验收的空断言已删除 | ✅ | r2 测试中已无 `check(..., True, ...)`、无 `covering lock present`、无 `or` 恒真分支 |

**无任何 P0/P1 回归失败。**

---

## 7. 新发现汇总

| # | 严重度 | 问题 | 复现 |
|---|---|---|---|
| NEW-A | **P2（未登记 fail-open）→ 阻断项** | `authorize_new_entry` 不交叉校验 `bundle.identity` 与 `generation`：demo bundle + live/异账户 generation（`expected` 等全部显式绑定到该 generation）→ `allowed=True/200/reason_codes=()`；反向同样 200 | `_scratch_Q/probe_new_a.py` → `out_new_a.txt` |
| NEW-B | P2（**已登记**于 README §12 L1 / U00 Q r2 r3） | 全角同形字键（`ｌｌｍ.ａｐｉ＿ｋｅｙ`）绕过 `is_sensitive_name`，秘密原值进 `model_route_hash` 与 `ResolvedModelRoute.as_dict()`（公开入口 `resolve_model_route(profile_overrides=...)`） | `probe_leak_artifacts.py` 3f |
| NEW-C | P3（已登记 L1） | 单行 `NAME=VALUE` 值不触发 `guard_no_raw_env_payload`（`_looks_like_env_text` 要求 ≥2 行） | `probe_leak_artifacts.py` 3k |
| NEW-D | P3 | `mint_runtime_generation(credential_epoch='3')` → 裸 `TypeError`（非结构化拒绝） | `probe_crosscheck.py` |
| NEW-E | P2（非 fail-open，未登记） | E P2-7 `comparison_status` 仍未产出，`diff-2` 仍标 `fixed` 未登记缺口② | §5 |
| NEW-F | P3（C P3-1 未修） | `settings.llm.decision_fallback.provider_env` 被忽略，改名即静默丢备用 | `probe_routes_fallback.py` 9c |
| NEW-G | P3（E P3-1/2/3/4 未修） | `emergency_stop`→`close_only` 仍 allowed；`action='unspecified'`→403（非 400）；`verify_control_write('bogus','bogus')`→ok=True；`auto` 角色连 `read_status`/`tighten` 都 403 | `probe_control2.py` |
| NEW-H | P3（E P3-6 残余，方向 fail-closed） | `is_loopback_host('0:0:0:0:0:ffff:127.0.0.1')` 仍 False（只接受 `::ffff:` 缩写形式） | `probe_regression.py` |

---

## 8. 命令清单（全部原始输出已落盘于 `_scratch_Q/`）

```bash
cd "C:/Users/Tao/Desktop/币安/mat-wt/reviews/U15/R2/r1"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"; export PYTHONUTF8=1

$PY _scratch_Q/probe_p0_1.py            > _scratch_Q/out_p0_1.txt        # 项1/4/5
$PY _scratch_Q/probe_p0_2_drift.py      > _scratch_Q/out_p0_2.txt        # 项2
$PY _scratch_Q/probe_reverse.py         > _scratch_Q/out_reverse.txt     # 项2c/2d
$PY _scratch_Q/probe_leak_artifacts.py  > _scratch_Q/out_leak.txt        # 项3/10/11
$PY _scratch_Q/probe_routes_fallback.py > _scratch_Q/out_routes.txt      # 项6/7/8/9
$PY _scratch_Q/probe_regression.py      > _scratch_Q/out_regression.txt  # 项12 + 回归
$PY _scratch_Q/probe_control2.py        > _scratch_Q/out_control2.txt    # 控制边界 + mock 模式 + 轮换
$PY _scratch_Q/probe_crosscheck.py      > _scratch_Q/out_crosscheck.txt  # E P2-1 回归
$PY _scratch_Q/probe_new_a.py           > _scratch_Q/out_new_a.txt       # NEW-A
for p in docs/dispatch/patches/U15/a136cb5/0*.diff; do git apply --check -p1 "$p"; done   # 4/4 OK
git status --short                                                       # 仅 ?? _scratch_Q/
```

无 pytest（环境未安装），全部以"独立可运行脚本 + 原始 stdout"方式执行；未采信作者 `PASS` 文案。

## 9. 未覆盖 / 无法离线验证（**不据此写"生产已验证"**）

1. **生产接线未验证**：`grep -rn runtime_identity --include=*.py src/ scripts/` 仍只命中
   `src/core/contracts/k01_identity.py:311` 与 `scripts/smoke_contracts_v3.py:89` ——
   **模块零消费者**（作者自述属实）。4 个补丁只做 `git apply --check`（编排者已预验 + 我复跑），
   **未落地**。因此"看板 `/api/control` 真的拒绝非环回""Demo/live 真的阻断新仓""release_manifest
   真的消费新身份"全部**未验证**，不作为通过项也不作为失败项（集成归 I）。
2. **HTTP 层行为未验证**：`PermissionDecision.http_status`（200/409）与 `/api/control` 的 400/403/409
   映射只在纯函数层；未启动服务。
3. **账本侧 `account_ref` 耦合（B P1-4）未验证**：我未读 `src/exchange/trade_reconciliation.py`
   的实际运行数据、未访问 `data/`；该残余在 r2 是否被登记/处置我**未独立确认**（不属本次 12 项）。
4. **WorkBuddy 是否真的不需要 API key** 无法离线核实（无网络、未读 `.env`）—— 本次只验证
   "不再写伪证据、未声明能力即阻断"。
5. **同模型偏差**：本报告作者与 U15 作者会话同为 `deepseek-v4.1-flash`；虽为独立会话、独立工作区、
   反例全部自写，但**不构成不同模型交叉审计**。建议 P0/P1 及 NEW-A 的定性由另一模型复核。
6. **未运行作者的两个验收脚本**（编排者已预验 exit 0 并明示"不必重跑"）；我改为静态核对断言体，
   确认上版 E P2-9 / B P3-1 的空断言已删除，但未逐条复算 197/50 项断言。

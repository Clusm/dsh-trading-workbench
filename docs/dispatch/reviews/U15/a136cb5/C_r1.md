# C/U15 r1 审查报告 — 决策证据（配置语义 / 有效配置诚实性）

- 审查对象：`a136cb5`（分支 `task/U15`），基线 `ffdab44`
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U15\C\r1`（detached，只读）
- 解释器：`multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），全部离线
- 独立探针（原始 stdout 已保留）：
  - `_scratch_C/probe1_config_semantics.py` → `_scratch_C/probe1_out.txt`（189 行）
  - `_scratch_C/probe2_e2e.py` → `_scratch_C/probe2_out.txt`（71 行）
  - `_scratch_C/probe3_bounds.py` → `_scratch_C/probe3_out.txt`（36 行）
- 未重跑作者套件（编排者已预验 PASS）；作者"全 PASS"不作为本报告依据。

## 0. 结论

**CHANGES_REQUESTED（窄口）**

默认接线路径（`collect_runtime_identity` 走默认参数）在 C 关注的三条主线上是**诚实**的：
空环境/未设置键/空白值不产生虚假覆盖、非白名单秘密不进身份、mock/缺 key 在 demo/live 阻断且 paper 不阻断、
覆盖优先级与现行 `factory` 一致、策略卡文件级来源校验真实生效。

但在**冻结门禁接口**（`resolve_model_route` / `model_route_blockers` / `collect_artifact_files` /
`collect_runtime_identity` 的公开参数）上存在 6 项 P1：2 条"凭据进身份"路径、1 条静默接受畸形配置、
3 条 fail-open / 静默丢弃。这些接口正是 U05/U12 的消费面（`implementation_requires` 里的 DL01/S05 冻结面），
且模块三条"不可退让的边界"中有两条在这些路径上不成立。建议按 P1 清单窄口修改后复审；
其余 P2/P3 可与 I 合入并行处理。

---

## 1. P0

无。

## 2. P1

### P1-1 嵌套敏感键绕过过滤，原始凭据值进入 `ModelRoute` 与 `as_dict()`
- 位置：`src/core/runtime_identity.py:781-786`（`profile_effective` 只对**顶层**键做 `is_sensitive_name`）
- 模块自述不符处：`:9-12`「任何键名命中 K01 敏感标记的字段一律丢弃并计数」；`:577`「不含任何凭据值」
- 证据：`probe1_out.txt:108-110`
  ```
  4i.1 route.profile_overrides :: {..., 'profile': {'llm': {'api_key': 'SK-NESTED-SECRET', 'model': 'm'}}}
  4i.2 secret survived? :: True
  ```
  即 `resolve_model_route(profile_overrides={"llm": {"api_key": SECRET}})` 把 **secret 原值**放进
  `ModelRoute.profile_overrides` → 进 `model_route_hash`，并经 `ResolvedModelRoute.as_dict()` 外露
  （`as_dict` 被 `IdentityBundle.as_dict` 直接返回）。顶层敏感键过滤正常（`4i.3 :: False`）。
- 对照：同文件的 `_flatten_into`（`:859-878`）**会**递归丢弃嵌套敏感键；`build_identity` 对
  `model_route` 载荷也做敏感拒绝（`probe2_out.txt:66` → `SensitiveInputError: profile_overrides.api_key`）。
  所以是 `resolve_model_route` 这一条路径漏了递归。
- 已接线路径不受影响（`collect_runtime_identity:1263` 传的是已递归过滤的 `effective.profile_overrides`），
  故不是 P0；但这是 U05 要直接调用的公开解析入口。
- 建议：对 `profile_overrides` 复用 `_flatten_into` 的递归丢弃+计数语义，或补一次 `guard_no_raw_env_payload`。

### P1-2 `artifact_relatives` 无 root 边界、无凭据守卫：可将 root 之外任意文件（含 `.env`）纳入 `artifact_hash`
- 位置：`src/core/runtime_identity.py:396-397`（`Path(name).as_posix()` 后直接拼接）、`:357-361`（`_read_text` 无边界/无守卫）
- 证据：`probe2_out.txt:45-52`
  ```
  F.2 relatives [absolute outside path] [OK] :: {'C:/Users/Tao/AppData/Local/Temp/outside_secret.txt': 'SK-FAKE-LEAK-0001'}
  F.2 secret content hashed? :: True
  F.3 artifact_hash :: '44136fa355b3678a'   # 注入前后 artifact_hash 改变
  ```
  （探针用临时目录里的假秘密，未触碰真实 `.env`。）
- 影响：`collect_runtime_identity(artifact_relatives=...)` 是 I 要接线的公开参数；一旦传入绝对路径或
  深度足够的 `../` 相对路径，`.env` 内容即可进入 `artifact_hash`，且 `collect_artifact_files` **不过**
  `guard_no_raw_env_payload`（该守卫只在 `:1259` 对 `effective_config` 调用），同时命中模块边界 1
  （「不对原始 .env 整文件哈希」）。`_read_text` 亦无大小上限。
- 建议：相对路径 `resolve()` 后校验 `is_relative_to(root)`，拒绝绝对路径；对读入内容跑
  `guard_no_raw_env_payload` 并加尺寸上限。

### P1-3 畸形 `raw_settings` 被静默当成"空配置"，生成看似有效的身份且无 reason code
- 位置：`src/core/runtime_identity.py:938`（`settings = raw_settings or {}`）配合 `_dig`（`:600-606`）对
  非 Mapping 一律返回 `None` → 静默跳过；`:1242` 同理用于 `default_mode`
- 证据：`probe2_out.txt:34`（端到端 `raw_settings=[1,2]` 未报错、未阻断）
  ```
  E.1 raw_settings=list [OK] :: IdentityBundle(... settings={}, effort='default', blockers=(), ...)
  ```
  `probe3_out.txt:29` `M.3 settings not a Mapping [OK] :: EffectiveConfig(settings={}, ...)`
- 影响：调用方传错类型时，身份**照常生成**，`effective_config.settings` 为空、`effort` 从 `low` 掉到
  `default`，但没有任何 error/blocker/advisory —— 正是"静默放行"。K01 §3/§12 对非 Mapping 载荷的口径是
  「直接按 `组.*` 缺口结构化拒绝」。
- 建议：`raw_settings`/`raw_agents`/`env`/`credentials_present`/`profile_overrides` 入参先做 Mapping 守卫，
  非 Mapping 抛结构化错误。

### P1-4 未识别的备用 provider 被静默丢弃（同组"缺 key"却报错，口径不一致）
- 位置：`src/core/runtime_identity.py:674-675`（`if provider != "opencode": return ()`）
- 证据：`probe1_out.txt:84` `4e provider='anthropic' :: ((), ())`；`probe2_out.txt:30-31`（端到端同样
  `fallback_routes=()`、`blockers=()`，无任何原因码，仅 `environment_overrides` 留下变量名/值的痕迹）
- 对照：同函数 `:684` 对「provider=opencode 但参数非法」会抛 `FallbackRouteError`
  （`probe1_out.txt:89`）。即**参数非法 → 拒绝**，**provider 名非法 → 静默当"无备用"**。
- 影响：运维显式声明了备用路由，`model_route_hash` 里却是"无备用"，与 DL01「备用非法参数均有负例」
  及模块「禁止静默」原则冲突；备用缺失直接影响 DL01 的故障降级承诺。

### P1-5 WorkBuddy 非 live 阻断在缺省/空 mode 下不触发（冻结门禁接口 fail-open 默认值）
- 位置：`src/core/runtime_identity.py:844-846`
  ```python
  if resolved_provider == "workbuddy" and effective_mode and effective_mode != "live" \
          and not allow_workbuddy_in_demo:
  ```
- 证据：`probe1_out.txt:76-79`
  ```
  4d mode=None :: ('workbuddy', ())        # 无阻断
  4d mode=''   :: ('workbuddy', ())        # 无阻断
  4d mode='demo' :: ('workbuddy', ('WORKBUDDY_OUTSIDE_LIVE',))
  4d.7 model_route_blockers(wb, no mode) :: ()
  ```
  `resolve_model_route` 的 `execution_mode` 默认即 `None`（`:695`），因此**默认调用永不阻断 WorkBuddy**。
- 影响：`allow_workbuddy_in_demo=False` 的语义是"WorkBuddy 实盘专属"，`effective_mode` 未知时应按"非 live"
  处理（fail-closed），现在反向。已接线路径（`collect_runtime_identity:1262` 传 `scope.mode`）不受影响，
  但该判定函数是 S05/DL01 冻结面。
- 建议：`effective_mode` 为空时视为非 live（或直接拒绝"模式未知"）。

### P1-6 备用路由对非 `ai_trader` agent 也生成，`model_route_hash` 与真实有效路由不符
- 位置：`src/core/runtime_identity.py:667-685` + `:770`（`_fallback_route` 无 `agent_name` 参数）
- 对照实现：`src/llm/factory.py:61` `use_opencode_fallback = agent_name == "ai_trader" and fallback_provider == "opencode"`
- 证据：`probe1_out.txt:92`
  ```
  4f.1 market_analyst fallback_routes :: ({'provider': 'opencode', 'model': 'union-alpha', ...},)
  ```
  即 `market_analyst` 的路由里声明了备用，而真实运行时不会构建该备用。
- 影响：K01 §9 明确要求 `model_route_hash` 取「**解析路由后的有效值**」（含备用/profile 覆盖）。
  这里身份与事实不符（多报备用），属"有效配置诚实性"直接违背。

## 3. P2

### P2-1 `authorize_new_entry` 省略 `expected` 时静默跳过 U02 绑定校验
- `src/core/runtime_identity.py:1306`（`expected: RuntimeGeneration | None = None`）、`:1329`（`elif expected is not None`）
- 证据：`probe1_out.txt:179` `8.1 bundle+gen, no expected :: (True, ())` —— 不传 `expected` 即放行（200）。
- 同一文件对"缺 key 即阻断"的立场（`:756`）与此默认相反；调用方漏参不会得到任何提示。
- 建议：`expected` 改必传，或省略时给出明确 reason（如 `RUNTIME_GENERATION_UNPROVEN`）。

### P2-2 空串 `runtime_instance_id` 被当有效身份
- `collect_runtime_identity:1218` 无校验；`mint_runtime_generation:1088-1090` 对空串**拒绝**
- 证据：`probe2_out.txt:39` `E.6 runtime_instance_id='' [OK] :: ... runtime_instance_id=''`
- 影响：`runtime_instance_id` 是 `IDENTITY_MISMATCH_IGNORED_FIELDS` 默认忽略字段，空值使"运行代"退化为常量；
  且与 mint 侧口径不一（同库内两种语义）。

### P2-3 模块在整个系统内无消费者，F13/DC14"阻断新仓"未在运行路径生效
- 证据（全仓 grep，排除三个交付文件）：
  ```
  src/core/contracts/k01_identity.py:93,253,311   # 契约自身
  scripts/smoke_contracts_v3.py:89,104,131        # 冒烟占位
  → collect_runtime_identity / authorize_new_entry / model_route_blockers / resolve_model_route
    在 src/ 与 scripts/ 中 0 引用
  ```
- 作者自述「`src/llm/factory.py` 未接线，`WORKBUDDY_OUTSIDE_LIVE` 只在身份面生效」经核实成立；
  进一步：**`settings.llm.mock` 在 `factory.py`/`config_loader.py` 中零命中**，与 F 指纹清单 Q3
  「`llm.mock` 无任何代码读取」一致。
- 缺口影响面评估（C 口径）：
  - 方向**安全**：实际 LLM 客户端选择未变（`factory.py:53-54` 仍按"缺 key / 模型空 / none"回退 Mock），
    不会出现"身份说真实、运行是 Mock"的漏判；只有反向（身份说 Mock 而运行真实）→ 过度阻断。
  - 但门禁**无强制力**：`entry_readiness.py`/`service.py` 未消费该模块，
    "Demo/live 主模型无效时阻断新仓"（任务卡交付要点 / DC14 / F13）在部署态等于**未实现**。
  - `FINGERPRINT_R1_DISPOSITION` `:1491` decl-14「执行层接线见共享 diff」指向**不存在的产物**：
    本次提交未改任何共享文件，diff 中无接线补丁。
  - 归属：任务卡把 `release_manifest/config_loader/dashboard/app/entry_readiness` 列为"I 合入"的
    `integration_targets`，因此**不算作者越权**；但交付物的"边界 3（Mock/缺 key 显式阻断新仓）"在
    INTEGRATED 之前只是库内承诺。建议 I 合入时优先接线，并在 U12 前复核。

### P2-4 `FINGERPRINT_R1_DISPOSITION` 登记不实（diff-7 声称"状态根一致性校验接口已提供"）
- `src/core/runtime_identity.py:1482`
  `"diff-7": {"status": "fixed", "note": "account_ref 由声明值派生、与密钥解耦；environment/状态根一致性校验接口已提供，接线归 U13"}`
- 核实：`runtime_identity.py` 内 grep `state_root|MTA_STATE|data_root|runtime_paths` **零命中**；
  `collect_runtime_identity` / `verify_account_binding` / `derive_account_ref` 均无"状态根"入参。
  全仓 `RuntimePaths` 尚不存在（F 指纹报告 §2 同记）。
- 对照来源：F 指纹清单 `diff-7` 原文缺口为「② profile=None 时 `EXECUTION_MODE=live` 可让 live 账户写入
  公共 `data/` 根（`config_loader.py:41-43`），**无 environment 与状态根一致性校验**」。
  该缺口本次**未修**，"接口已提供"不成立。
- 影响：I/Q 依赖该处置表判定"哪些已修"。建议 `status` 改 `registered`（owner `U13`），
  或补最小接口（如 `verify_environment_root(environment, paths)`）。

### P2-5 空模型走裸 `pydantic.ValidationError`，非结构化原因码
- `src/core/runtime_identity.py:788-793`（`ModelRoute(model=model or "", ...)`）
- 证据：`probe3_out.txt:3`
  ```
  J.1 all empty (no default_model) [RAISED ValidationError] :: "1 validation error for ModelRoute\nmodel\n  String should have at least 1 character ..."
  ```
  `MODEL_ROUTE_UNSET` 在 `model_route_blockers` 里可达（`J.5`），但 `resolve_model_route` 永远先被
  `ModelRoute` 的 `min_length=1` 挡下 → 该原因码对解析入口是**死路径**，调用方拿到第三方异常类型。
- 建议：解析侧先判定空模型并返回带 `MODEL_ROUTE_UNSET` 的结果（或抛本模块结构化异常）。

## 4. P3

| # | 位置 | 现象 / 证据 |
|---|---|---|
| P3-1 | `:769` | `MTA_AI_DECISION_FALLBACK_PROVIDER` 变量名硬编码，忽略 `settings.llm.decision_fallback.provider_env`（`factory.py:60-62` 读配置）。`probe1_out.txt:95` `4g.1 renamed provider_env :: ()` —— 配置改名后备用被静默丢弃 |
| P3-2 | `:1240-1241` | `MTA_PROFILE`/`EXECUTION_MODE` 硬编码，忽略 `settings.execution.mode_env`（`src/exchange/factory.py:39` 读配置）。当前配置同名故无现网分歧，改名即静默不一致 |
| P3-3 | `:946` | settings 白名单值只判 `value == ""` 不 strip：`probe1_out.txt:21` `2b.1 settings :: {'log_level': '   '}`。与 `environment_override_values:917-919`（strip 后空即弃）口径不一致；K01 §3 把"空白串"与空串同等看待 |
| P3-4 | `:500,513` | `ExecutionScope.warnings` 从不赋值 → 死字段。`profile` 与 `EXECUTION_MODE` 冲突只进 `ignored`（`:561`），无 warning/blocker，`probe1_out.txt:44`。建议冲突至少进 `warnings` |
| P3-5 | `:875,952,970` | 非敏感 list/tuple/set 值被报成 `CredentialLeakError`（"credential-bearing"）：`probe1_out.txt:35-37`、`probe3_out.txt:28`。拒绝方向正确，错误语义误导 |
| P3-6 | `:257,872` | 递归遍历无深度/循环防护：`probe3_out.txt:13,23-24` 深 3000 或自引用 dict → `RecursionError`（非结构化崩溃） |
| P3-7 | `:914` / `:962` | `env` 非 Mapping → `AttributeError: 'list' object has no attribute 'get'`；`extra_profile_overrides` 非 Mapping → `ValueError: dictionary update sequence...`（`probe3_out.txt:31-32`）。均为裸异常 |
| P3-8 | `:760` | `llm.mock` 用 `is True` 严格判定：YAML `mock: "true"`（字符串）静默失效（`probe1_out.txt:69` `4c.2 :: (False, ())`）。建议显式拒绝非布尔或接受常见真值 |
| P3-9 | `:1424-1446` | 控制面方向未与动作名校验：`set_control_severity_down` 可用于**提高**严重度（`probe1_out.txt:169` `operator/set_control_severity_down/emergency/normal → (True, (), 200)`）。当前角色表下 operator 同时持有 up，故无实际提权；属潜在语义缺陷 |
| P3-10 | `:236-239` | 全角键 `ａｐｉ＿ｋｅｙ` 绕过敏感键匹配（`probe1_out.txt:121` `5 fullwidth key [OK]`）。**值不扫描属 README §12 L1 已登记**，但"键名同形字"未登记；实际防线是白名单（settings 仅白名单点分路径、env 仅白名单变量名），影响低。建议登记或做 NFKC 归一化 |
| P3-11 | `:1027-1036` | `key_fingerprint` = 无盐 `sha256(api_key)[:16]`。不可逆、不入身份哈希、仅轮换审计（符合 K01 L9 归属）；无盐快哈希对低熵密钥可被字典攻击。建议注明"仅自比对"或加域分隔 |

## 5. 已独立复现为**成立**的声明（正面证据）

| 声明 | 证据 |
|---|---|
| 空环境变量 / 未设置的 settings 键 → 不产生虚假覆盖 | `probe1_out.txt:3-5`（`{}`）；`:21` 缺键不回填；`probe2_out.txt:16` `B.7 h(secret env)==h(no env) :: True` |
| 空白/仅换行的环境值等同未设置 | `probe1_out.txt:6,11`；`probe2_out.txt:15` `B.6 h(blank)==h(no env) :: True` |
| 确实设置的值**会**改变 `effective_config_hash` | `probe2_out.txt:14` `B.5 h(no env)==h(env) :: False` |
| 非白名单秘密不进身份（`BINANCE_LIVE_API_KEY`） | `probe2_out.txt:16-18`（哈希不变、identity/bundle JSON 均不含秘密） |
| 秘密只出现在错误信息的**键路径**里 | `probe1_out.txt:131-133`（`SK-LEAKME in str(e) == False`）；`CredentialLeakError:216` 只报键名 |
| 覆盖优先级 = `agents.yaml[agent].model` > `env(model_env)` > `settings.llm.default_model`，与 `factory.py:49-52` 一致 | `probe1_out.txt:56-58` |
| 缺 key / 模型 `none`（含大写）→ mock 且 `MODEL_ROUTE_MOCK`；demo 阻断、paper 不阻断 | `probe1_out.txt:61-64`；`probe2_out.txt:22-26`（gate → 409 / paper → 无 blocker） |
| 备用路由非法参数全拒（none / 非 http(s) / URL 内嵌凭据 / 无凭据声明 / 未知键 / 非布尔凭据） | `probe1_out.txt:98-105`（8/8 抛 `FallbackRouteError`）；opencode 缺 key 抛错同 `factory.py:71-75` |
| 策略卡文件级来源校验（L16 部分关闭）真实生效 | `probe2_out.txt:56-58`（正常通过；内存篡改 payload → 拒绝；加载后改文件 → 拒绝） |
| `build_identity` 覆盖组 fail-closed（空组合法 / 缺字段 / 未知键 / None / 非 Mapping / 敏感键） | `probe2_out.txt:61-67`（`H.1` 通过；`H.2`-`H.7` 分别 `IdentityCoverageError` / `SensitiveInputError`） |
| 回环白名单、角色分离、人工禁用优先于自动恢复、写后回读 | `probe1_out.txt:136-153,156-176`（`0.0.0.0`/私网/域名拒绝；health 不能 `set_control`；auto 不能放宽；`emergency` 需二次确认） |
| `deployment_hash` 忽略运行代、`identity_hash` 不忽略 | `probe2_out.txt:70-71` |
| `account_ref` 与密钥轮换解耦 | `probe1_out.txt:184-186`；`rotate_credential` 不改 `generation_id` |

## 6. 未覆盖项与理由

- **未实际运行作者的两个验收套件**：按编排者指令省预算（已预验 exit 0）。因此对"作者套件是否覆盖上述缺口"
  只做了函数名级静态核对（`check_model_route_priority` / `check_dl01_fallback_illegal_params` /
  `check_defaults_are_not_effective_values` 等存在），未逐条读断言体。
- **未验证真实运行态**：不联网、不启服务、不读 `.env`/`data`/密钥，故"Demo/live 实际阻断新仓"只能在库内
  接口层验证（P2-3 已说明系统内无消费者）。
- **未与 `config_loader.py` 的内存 profile 合并结果逐字段比对**：`profiles.live` 段在
  `collect_effective_config` 下被正确采集（`probe1_out.txt:25`）；进一步比对会触及 `data/` 路径语义，
  且 K01 只要求三组子载荷。
- **`decl-15`（供应商模型/effort 枚举）无法离线核实**，作者标 `registered`/owner `-`，与 F 指纹报告一致，
  本报告不作断言。
- **控制面与 K04 授权绑定的端到端形态**属 B 角色重点，本报告只验了与配置语义相关的回环/角色/人工禁用优先。

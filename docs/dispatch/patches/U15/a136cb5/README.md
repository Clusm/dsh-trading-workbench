# U15 共享适配补丁（基线 `a136cb5`）

> **r3 说明（2026-09-21，作者会话 F）**：本轮**四个补丁逐字节未改动**
> （`git diff` 可核对），改动全部落在 U15 独占路径 `src/core/runtime_identity.py`
> 与两个验收脚本；`git apply --check -p1` × 4 于 r3 HEAD 重新验证通过。r3
> 与补丁相关的变化只有两点，均为**调用方义务**，不影响补丁可应用性：
>
> 1. **`authorize_new_entry` 现在交叉校验 `bundle.identity` 与 `generation`**
>    （NEW-A）：`identity.environment`（对 `generation.environment`）与
>    `identity.account_ref`（对 `generation.account_ref`）不一致即
>    `IDENTITY_GENERATION_MISMATCH` / 409。接线方（U02 / 服务）必须保证
>    **铸造运行代用的账户引用与身份采集用的账户引用同源**（都用
>    `derive_account_ref(env, declared_account_ref=...)` 或都走显式声明）；
>    用 `<env>:local` 回退的身份去授权一个声明了具体账户的运行代，现在会
>    被拒 —— 这是修复目标，不是回归。
> 2. **`decision_fallback.provider_env` 现在生效**（NEW-F）：补丁 01/02 若在
>    清单/配置里声明的该键名与运行环境变量名不一致，备用路由会按声明名读取；
>    声明名未设置时仍回退到 `MTA_AI_DECISION_FALLBACK_PROVIDER`。
>
> 补丁 01 的 `_dependency_comparison_status()` 与模块 r3 新增的
> `compare_dependency_lock(root, declared)` **同口径**（`MATCH` = 身份要求的
> 锁文件都在声明集合且都在盘上；`MISMATCH` = 两份名单漂移；`UNAVAILABLE` =
> 必需锁文件缺失/形状非法），发布清单字段即 K01
> `IDENTITY_COVERAGE["dependency"].comparison_status` 的落地；I 合入时可直接把
> 补丁 01 的实现改为委托该函数（单一判定源），r3 未改动补丁以免影响已验证的
> 上下文行。
>
> **r2 修订（2026-09-21，作者会话 F）**：仅 `02_config_loader.diff` 被替换，其余
> 三个补丁**逐字节未变**（`git diff` 可核对），因为它们调用的公开接口在 r2 保持
> 向后兼容（新增参数全部有默认值）：
>
> - `01_release_manifest.diff`：`collect_runtime_identity` 的调用形态不变；r2 起
>   它产出的 `effective_config_hash` 自动覆盖 K01 六类配置来源，且
>   `identity_blockers` 会多出 live 专属原因码（如 `EXECUTION_SCOPE_UNLOCKED`、
>   `ACCOUNT_REF_UNPROVEN`）——这是信息增加，不是接口破坏。
> - `03_dashboard_app.diff`：只用 `authorize_control_request` /
>   `verify_control_write`，二者 r2 未改签名与语义。
> - `04_entry_readiness.diff`：只消费 `identity_blockers` / `model_route_ok`
>   两个 facts 键，r2 未改其语义。
>
> `02_config_loader.diff` 的 r2 变化：① `effective_config_payload` 额外采集
> `collect_source_config_files`（decision_policy / agents / risk_learning /
> risk_profile.lock / windows_service），使风险档、止损线、执行端点与服务定义的
> 漂移都会改变指纹；② `assert_environment_matches_state_root` 的判定改为委托
> `verify_environment_state_root`（单一判定源），并**只在 `environment == "live"`
> 时收紧**——其余组合直接返回，行为与修订前一致。
>
> 修订后用 `git apply --check -p1` 重新验证通过；并在工作树外的 sandbox
> （`_scratch_F/patchgen/sandbox/`，原样 `src/` + 修订后的 `config_loader.py`）
> 实跑：live+无 profile → `LIVE_WITHOUT_PROFILE_STATE_ROOT`、live+公共根 → 同左、
> demo/testnet/paper/空值 → 6/6 不抛、`effective_config_payload` 为三字段且
> `config_sources` 含六个标签、payload 中无 `SK-` 字面量。


本目录是 U15「运行身份、有效配置和控制权限」的**共享文件适配补丁**，供 I 在
`task/U15`（`src/core/runtime_identity.py` + 两个验收脚本）之上把共享文件接线
合入。之前只提交了 `runtime_identity.py` 与验收脚本，共享文件的改动停留在文字
描述，没有可应用的补丁；本目录补齐这部分，**不重新实现**任何逻辑。

- 生成方式：在工作树外的临时目录复制原文件 → 施加改动 → `diff -u`（`a/`、`b/`
  前缀，`git apply -p1` 可直接使用）。
- 补丁生成时**未修改工作树中的任何共享文件**；4 个源文件在 `a136cb5` 时仍是
  原样（未接线状态）。
- 全部 4 个补丁已用 `git apply --check -p1` 针对 HEAD `a136cb5` 的实际工作树验证
  通过（只检查、不落地）。

## 文件清单与建议应用顺序

| 顺序 | 文件 | 行数 | 目标文件 | 主要改动 |
|---|---|---|---|---|
| 1 | `01_release_manifest.diff` | 237 | `src/core/release_manifest.py` | 依赖锁入指纹、策略卡入 `strategy_hash`、`release_id` 种子扩项、追加 9 个身份字段、best-effort 身份采集与降级 |
| 2 | `02_config_loader.diff` | 70 | `src/core/config_loader.py` | `Settings.effective_config_payload`、`assert_environment_matches_state_root` |
| 3 | `03_dashboard_app.diff` | 72 | `src/dashboard/app.py` | `/api/control` 走权限判定器 + 写后回读校验；`/api/mode` 身份阻断返回 409 |
| 4 | `04_entry_readiness.diff` | 60 | `src/core/entry_readiness.py` | 存在性触发并入 `identity_blockers` 与 `model_route_ok` 判定 |

顺序 1 与 4 无相互依赖，可单独应用；建议顺序只是让「身份来源 → 配置采集 →
控制面/门禁消费」的阅读顺序与依赖方向一致。**四个补丁互不依赖，可各自独立
`git apply`**，也可 `git apply 01.. 02.. 03.. 04..` 一次性应用。

## 逐补丁说明

### 01 `release_manifest.py`（向后兼容）

- `DEPENDENCY_FILES` 增加 `requirements-lock.txt`、`requirements-dev-lock.txt`
  —— 指纹报告不一致清单 #5 的文件级修复。
- `strategy_hash` 纳入 `knowledge/strategy_cards.json`（新增模块常量
  `STRATEGY_CARD_FILES`），改卡即改身份。
- `release_id` 种子增加 `git_commit | account_ref | strategy_cards_hash`：同一
  `release_id` 不再可能对应两份代码 / 两套策略卡。
- 追加字段（**全部有默认值**）：`artifact_hash`（取 `content_hash`）、
  `effective_config_hash`、`model_route_hash`、`runtime_instance_id`、
  `strategy_cards_hash`、`dependency_comparison_status`、`git_dirty`、
  `identity_blockers`、`release_id_overridden`。`asdict()` 消费者用 `.get(...)`
  读取，新增键不会破坏既有调用（`manifest_schema` 保持 `1`，未改既有字段语义）。
- `build_release_manifest` 内 best-effort 调用 `collect_runtime_identity`：
  - settings/agents 由本模块 **直读 YAML**（`_direct_yaml`），不经过
    `config_loader.load_settings`，因此**不会 `load_dotenv`**；
  - 环境变量按 `env={}` 传入，`.env` 内容不可能进入有效配置哈希；
  - 任何异常降级为 `identity_blockers=("IDENTITY_COLLECT_FAILED",)`，绝不抛出，
    清单仍可写出（`dependency_comparison_status="UNAVAILABLE"`）；
  - `dependency_comparison_status` 语义：`MATCH` = 身份模块要求的锁文件都在本清单
    声明集合内且确实存在；`MISMATCH` = 两份名单漂移（锁文件改动会漏出指纹）；
    `UNAVAILABLE` = 锁文件缺失或身份不可证明。
  - 新增**可选**入参 `runtime_instance_id`（默认取 `MTA_RUNTIME_INSTANCE_ID`，
    仍为空记 `unknown`）；不传时行为与旧签名兼容。I 合入时建议由服务传入
    `service.runtime_instance_id`。

### 02 `config_loader.py`（向后兼容，r2 已修订）

- `Settings.effective_config_payload`：调用 `collect_effective_config`，返回 K01
  三字段载荷（白名单点分路径 + 仅实际存在的值 + 白名单环境变量），凭据与
  `.env` 原文不可能进入；r2 起并入 `collect_source_config_files` 的六类来源
  （settings / decision_policy / agents / risk_learning / risk_profile.lock /
  windows_service），缺失来源以 `__missing__` 标记进入哈希。
- `Settings.assert_environment_matches_state_root(environment)`：仅在
  `environment == "live"` 时委托 `verify_environment_state_root` 判定，命中
  任一原因码（`STATE_ROOT_PROFILE_UNLOCKED` / `LIVE_SHARED_STATE_ROOT` /
  `ENVIRONMENT_STATE_ROOT_MISMATCH` / `STATE_ROOT_UNBOUND`）即抛
  `LiveWithoutProfileStateRootError`（`code` / 消息为
  `LIVE_WITHOUT_PROFILE_STATE_ROOT`，消息含具体原因码）。其余组合（demo /
  testnet / paper / 空值）不改变任何既有行为——函数直接返回。调用时机由 U13
  接线（启动前终止进程）。

### 03 `dashboard/app.py`（行为变更，见下）

- `POST /api/control`：新增 `Request` 参数，调用
  `authorize_control_request(role=os.getenv("MTA_CONTROL_ROLE","operator"),
  action=..., client_host=request.client.host, requested_state=..., 
  current_state=service.control.state, actor=role, ...)`，不通过时按返回的
  `http_status` 抛 `HTTPException`。`state=emergency` 映射为 `emergency_stop`
  动作，原有的 `confirm='emergency'` 400 校验与错误文案保持不变。
- `service.set_control` 之后回读 `service.control.state`，用
  `verify_control_write` 校验；失败返回 **503**（写丢/被回滚不得显示成功）。
- `POST /api/mode`：`mode == "live"` 且 `service.release_manifest` 的
  `identity_blockers` 非空时返回 **409**（在模式合法性 400 之后、Key 检查 400
  之前）。
- **向后兼容性**：默认角色 `operator`、回环来源下，读状态/升严/emergency 的
  既有调用不受影响；**行为变更**仅两类：① 从更严状态放宽（如
  paused/emergency → normal）现在需要 `confirm_manual_reenable`（emergency 退出
  还需 `confirm='emergency'`），否则 409；② 非环回来源 / 越权角色按策略器
  给出的 403 拒绝。二者都是 R11「人工禁用优先 + 权限分离」的落地目标。

### 04 `entry_readiness.py`（严格向后兼容）

- **仅存在性触发**：`facts["identity_blockers"]` 逐个并入 `blocking_reasons`
  （接受字符串序列，单字符串按一个原因码；空 / `None` / 其它类型视为未提供）；
  仅当 `"model_route_ok" in facts` 时判定，非 `True` 产出
  `MODEL_ROUTE_UNPROVEN`（`None`，无证据）或 `MODEL_ROUTE_INVALID`（`False`）。
- 两个键都不提供时，判定结果与原因码集合与改动前**逐字节一致**（缺省即维持
  现状，不新增默认闸门）。新原因码会参与原有 `dict.fromkeys` 去重与排序。

## 离线验证（本次补交时实际执行）

1. `python -m py_compile` 4 个改动后的文件：通过。
2. 工作树外 sandbox（原样 `src/` + 4 个改动文件）运行 58 项行为自检，全部通过：
   - 清单 9 个新字段存在、`artifact_hash == content_hash`、`json.dumps` 可序列化、
     旧字段位置参数构造仍可用、`.to_dict().get("content_hash")` 正常；
   - `release_id` 随 `account_ref` 变化、显式 `release_id` 置
     `release_id_overridden=True`、重复构建确定性一致；
   - 空目录 root 下不抛出且 `identity_blockers == ("IDENTITY_COLLECT_FAILED",)`；
   - 本仓无凭据时清单如实报出 `MODEL_ROUTE_MOCK`（未静默放行）；
   - `assert_environment_matches_state_root` 对 live/无 profile 抛错，其余 5 种
     取值不抛；`effective_config_payload` 为三字段 dict 且无敏感键名；
   - 就绪判定：无新键时与旧行为一致；`identity_blockers` 并入；`model_route_ok`
     三态分别产出 `()` / `MODEL_ROUTE_INVALID` / `MODEL_ROUTE_UNPROVEN`；
   - 控制策略：环回+operator 放行、非环回 403、放宽未确认 409、`auto` 恢复 409、
     回读一致 ok / 不一致 `CONTROL_WRITE_UNVERIFIED` / 缺回读
     `CONTROL_READBACK_MISSING`；`dashboard.app` 可正常导入。
3. `git apply --check -p1` × 4：针对 `a136cb5` 工作树**全部通过**。

## 已知限制登记（r3）

以下残余**不在** U15 独占路径内或属刻意保留，逐条写明义务与风险，避免悬空：

| # | 形态 | 方向 | 处置与依据 |
|---|---|---|---|
| NEW-B | 全角/同形字键名（如 `ｌｌｍ.ａｐｉ＿ｋｅｙ`）不被 `is_sensitive_name` 识别（只做 `lower()` 子串匹配，无 NFKC），其值可能进入 `model_route_hash` 与 `ResolvedModelRoute.as_dict()`（公开入口 `resolve_model_route(profile_overrides=...)`） | 价值泄漏面（非放行） | 已登记：`docs/contracts/README.md` §12 `L1`（“敏感守卫只查键名/载荷形态，不扫描值内容”）。**补充登记**：除“不扫值”外，键名口径也**不做 Unicode 归一化** —— 修正它要改 K01 共享标记口径（`src/core/contracts/common.py`），不在 U15 独占路径。**调用方义务**：不得把该守卫当值级防泄漏，`profile_overrides` 只允许放非敏感值 |
| NEW-C | 单行 `NAME=VALUE` 字符串作为**值**不触发 `guard_no_raw_env_payload`（`_looks_like_env_text` 要求 ≥2 行且过半为赋值行） | 同上 | 同 `L1`（“不扫描值内容”）。多行 env 形态仍被拒绝 |
| NEW-H | `is_loopback_host("0:0:0:0:0:ffff:127.0.0.1")` 返回 `False`（只认缩写 `::ffff:`） | **fail-closed（误拒）** | 唯一消费者 `authorize_control_request` 的用法是 `if not is_loopback_host(...): 403`，`False` = **拒绝**；该形态只会让合法本机管理请求 403，**不会**放行任何非回环来源。放宽需引入 IP 解析并重划白名单（`127.0.0.2` 属 `127/8` 环回但当前刻意排除并有断言），收益不抵风险，故只登记 |
| decl-11 | 发布包 `SAFE_DOCS` 白名单缺最新规格、tracked 诊断脚本入包 | 打包内容 | 文件在 `scripts/create_release_package.py`（非 U15 独占路径），归属待 I 指派（`FINGERPRINT_R1_DISPOSITION["decl-11"]`） |

## r3 离线验证（实际执行，原始输出在 `_scratch_F/r3/`）

```bash
cd "C:/Users/Tao/Desktop/币安/mat-wt/EF"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"; export PYTHONUTF8=1
$PY scripts/smoke_contracts_v3.py                  # PASS 173 / FAIL 0（out_smoke_after.txt）
$PY tests/acceptance/test_runtime_identity.py      # 233 PASS，exit 0（out_acc1_after.txt）
$PY tests/acceptance/test_control_boundary.py      #  63 PASS，exit 0（out_acc2_after.txt）
$PY _scratch_F/r3/probe_na_nd.py                   # NEW-A/NEW-D 前后对照（out_before_/out_after_na_nd.txt）
for p in docs/dispatch/patches/U15/a136cb5/0*.diff; do git apply --check -p1 "$p"; done   # 4/4 OK
```

**回滚**：r3 改动是 `task/U15` 上的一组提交（基线 `38029e2`），回滚即
`git revert <r3 提交>`（或由 I 决定 reset 到 `38029e2`）；补丁文件未改动，
无需回滚。风险档/live 开关、`scripts/run_offline_regression.py`、
`src/core/contracts/`、`docs/contracts/` 均未触碰。

## 未做与不确定之处

- 未把补丁应用到工作树；未修改 `src/`、`src/core/contracts/`、`docs/contracts/`。
- `03_dashboard_app.diff` 的上下文行保留原文件的混合行尾（60 个 CRLF），补丁以
  二进制安全方式生成，`git apply` 已验证可应用。
- `effective_config_payload` 读取 `os.environ`（白名单内已设置变量），与
  `runtime_identity` 的「仅记录确实被设置的值」一致；它**不读** `.env` 文件。
- `runtime_instance_id` 的最终取值（服务传入 vs 环境变量）属 I 合入时的接线
  选择，本补丁只提供可选入口。

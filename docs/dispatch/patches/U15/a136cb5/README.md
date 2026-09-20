# U15 共享适配补丁（基线 `a136cb5`）

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

### 02 `config_loader.py`（向后兼容）

- `Settings.effective_config_payload`：调用 `collect_effective_config`，返回 K01
  三字段载荷（白名单点分路径 + 仅实际存在的值 + 白名单环境变量），凭据与
  `.env` 原文不可能进入。
- `Settings.assert_environment_matches_state_root(environment)`：`profile_name is
  None` 且 `environment == "live"` 时抛 `LiveWithoutProfileStateRootError`
  （`code` / 消息为 `LIVE_WITHOUT_PROFILE_STATE_ROOT`）。其它组合（demo /
  testnet / paper / 空值 / 已设 profile）不改变任何既有行为。调用时机由 U13
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

## 未做与不确定之处

- 未把补丁应用到工作树；未修改 `src/`、`src/core/contracts/`、`docs/contracts/`。
- `03_dashboard_app.diff` 的上下文行保留原文件的混合行尾（60 个 CRLF），补丁以
  二进制安全方式生成，`git apply` 已验证可应用。
- `effective_config_payload` 读取 `os.environ`（白名单内已设置变量），与
  `runtime_identity` 的「仅记录确实被设置的值」一致；它**不读** `.env` 文件。
- `runtime_instance_id` 的最终取值（服务传入 vs 环境变量）属 I 合入时的接线
  选择，本补丁只提供可选入口。

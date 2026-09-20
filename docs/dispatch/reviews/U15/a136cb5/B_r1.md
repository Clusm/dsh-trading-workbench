# U15 初审报告 — 审查者 B（管理授权绑定）

- 审查对象：作者提交 `a136cb5`（分支 `task/U15`），基线 `ffdab44`
- 审查者工作区（独立、detached）：`reviews/U15/B/r1`
- 审查立场：**B = 管理授权绑定**，以“我（U02 作者）能否真的用它绑定”为唯一判据
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 只读审查：未提交/未推送/未切分支；未读 `.env`/`data/`/密钥；未联网；未启停服务；未写 `mat-wt/I`、`mat-wt/EF`

---

## 0. 结论

**CHANGES_REQUESTED**

模块本体（1522 行）工程质量高：纯函数、结构化拒绝、覆盖缺口 fail-closed、凭据不外泄的守卫链（白名单 + 键名敏感标记 + 整份 `.env` 形态拒绝）都实测有效，作者两份验收脚本在本机全 PASS（我的独立复跑，非采信其结论）。

但**作为 U02 的绑定接口，当前形态不能直接采用**。5 项直接命中 U15 任务卡交付要点的问题：

| 交付要点（`docs/dispatch/任务卡.md:99`） | 实测结论 |
|---|---|
| “提供账户/运行代/权限验证接口供 U02 绑定” | 接口在，但存在 3 处 fail-open（缺省不校验、scope 不校验、mint 环境参数被静默忽略），见 P0-1/P1-1/P1-2 |
| “account_ref 与密钥轮换解耦” | **仅在 U15 模块内成立**；仓库权威账本 account_ref 仍由 API key 派生并在轮换时漂移，作者未替换也未登记，见 P1-4 |
| “Mock/缺 key 在 Demo/live 显式阻断新仓”（DC14/F13） | sensenova 路径正确；**WorkBuddy live 路径完全不检查凭据**且写入伪证据，见 P1-6 |
| 依赖锁/策略卡/模型覆盖均影响身份 | 在 `runtime_identity.py` 内成立（作者测试 + 我的复跑均验证） |
| 集成目标 4 个共享文件 | 作者未交任何共享补丁（`git diff --name-only ffdab44 a136cb5` 仅 3 个文件）。是否属交付缺口取决于“作者写补丁、I 合入”的口径，但**无补丁则 U02/U13 只能自行发明嫁接逻辑**，见 P2-6 |

问题计数：**P0 × 1、P1 × 6、P2 × 7、P3 × 3**。

---

## 1. 复现命令与原始输出

所有探针保存在 `_scratch_B/`，原始 stdout 落盘未删改。

```bash
cd "C:/Users/Tao/Desktop/币安/mat-wt/reviews/U15/B/r1"
P="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"

# 作者验收（独立复跑，仅确认可运行性，不采信其结论）
PYTHONUTF8=1 "$P" tests/acceptance/test_runtime_identity.py      # -> EXIT=0, "... ACCEPTANCE PASS"
PYTHONUTF8=1 "$P" tests/acceptance/test_control_boundary.py      # -> EXIT=0, "... ACCEPTANCE PASS"

# 我的独立探针（自行设计反例，不复用作者期望）
PYTHONUTF8=1 "$P" _scratch_B/probe_binding.py     | tee _scratch_B/probe_binding.out.txt
PYTHONUTF8=1 "$P" _scratch_B/probe_account_ref.py | tee _scratch_B/probe_account_ref.out.txt
PYTHONUTF8=1 "$P" _scratch_B/probe_failclosed.py  | tee _scratch_B/probe_failclosed.out.txt
PYTHONUTF8=1 "$P" _scratch_B/probe_workbuddy.py   | tee _scratch_B/probe_workbuddy.out.txt
```

基数：作者两份脚本共 3 个测试文件、2439 行；未改共享文件（`git diff --name-only ffdab44 a136cb5` = `src/core/runtime_identity.py`、`tests/acceptance/test_control_boundary.py`、`tests/acceptance/test_runtime_identity.py`）。

---

## 2. 需求基线（我独立读取，非转述作者）

- `docs/dispatch/任务卡.md:93-104`（U15 卡）：独占 `runtime_identity.py` + 两份验收；集成目标 `release_manifest.py`/`config_loader.py`/`dashboard/app.py`/`entry_readiness.py`；交付要点含“提供账户/运行代/权限验证接口供 U02 绑定”“account_ref 与密钥轮换解耦”“Mock/缺 key 在 Demo/live 显式阻断新仓（DC14/F13）”。
- `docs/契约 README §12 L9`：K01 无 `credential_epoch/key_fingerprint`、`account_ref` 派生规则未冻结，**“轮换与账本连续归集由 U15 落地”**。
- `docs/无人值守交付…规格_2026-09-19.md:301`：**“U15 仅提供身份/权限验证接口，K04 管理授权的实现归 U02”** —— 因此 K04 的 `nonce/expires_at/revision/max_reduce/lifecycle/position_side/target_protection` 不由 U15 提供**是设计，不是缺陷**；但 U15 必须说清这条边界（见 P2-5）。
- `docs/契约 README §6 K04`：`ManagementAuthorization` / `validate_authorization` 要求账户、生命周期、方向、revision、时效、nonce、运行代、环境逐项绑定；`RUN_GENERATION_UNBOUND`/`ENVIRONMENT_UNBOUND` fail-closed。
- `docs/contracts/golden/k01_identity.golden.json`：K01 冻结样例用 `demo:SYNTH_ACCT`（`<env>:<declared>` 形态）—— 与 `derive_account_ref` 的**声明路径**一致；`<env>:local` 回落形态不在冻结样例中。
- 约束：K04 契约与 `docs` 全文**均无 HTTP 状态码定义**（`grep -rn "409" docs/contracts/README.md` 无命中）→ `http_status` 是 U15 自创面，见 P2-2。

---

## 3. 问题清单

### P0-1 `authorize_new_entry` 缺省形态**完全不校验绑定**，直接 200

- 位置：`src/core/runtime_identity.py:1302-1340`，关键分支 `:1329-1333`
  ```python
  elif expected is not None:
      reasons.extend(verify_account_binding(
          expected, generation, environment=environment, account_ref=account_ref, ...))
  ```
- 复现（`_scratch_B/probe_binding.py`，P3 段；原始输出 `probe_binding.out.txt`）：
  ```
  === P3: authorize_new_entry 省略 expected 时不校验绑定 ===
  demo bundle + live/foreign generation, 无 expected -> allowed: True
     reason_codes () http 200
  ```
  构造：`bundle` 为 demo 身份健康路由；`generation` 为 `environment="live"`、`account_ref="live:someone-else"`、`runtime_instance_id="rt-OTHER"` 的外来运行代；调用 `authorize_new_entry(bundle, generation)`（不传 `expected`）→ **allowed=True / HTTP 200 / 无任何 reason**。
- 影响：`expected` 是签名里的关键字可选参数（`expected: RuntimeGeneration | None = None`），而它是**唯一**触发 `verify_account_binding` 的开关。任何按签名自然调用的 U02 代码路径（例如只传 bundle+generation，或从配置中漏传 expected）都会把“绑定校验”整体跳过，且返回值与“校验通过”完全无法区分（`reason_codes=()`，`http_status=200`）。对“门禁”类接口，默认 fail-open 是 P0 级缺陷：**它静默放行而不是拒绝**，正是本任务要消灭的 DC14/F13 形态。
- 建议：`expected` 变为必填（或 `generation` 与 `expected` 二者缺一即 `IDENTITY_BINDING_UNPROVEN`），并把“未做绑定校验”显式写进 `reason_codes`，让 200 只可能来自真正校验过的路径。

### P1-1 `scope_hash` 绑定 fail-open，且 `expected.scope_hash` 从未参与比较

- 位置：`src/core/runtime_identity.py:1168-1169`
  ```python
  if _text(scope_hash) and actual.scope_hash and actual.scope_hash != _text(scope_hash):
      reasons.append("SCOPE_HASH_MISMATCH")
  ```
- 复现（`probe_binding.py`，P1/P2 段）：
  ```
  === P1: scope_hash 不匹配是否被检出（actual.scope_hash=None） ===
  expected=scope-X / actual=None -> allowed: True   reason_codes ()
  === P2: expected.scope_hash 与 actual.scope_hash 不一致是否被检出 ===
  expected.scope=A / actual.scope=B -> allowed: True   reason_codes ()
  ```
- 两个独立缺口：
  1. `actual.scope_hash is None` 时短路 → 显式要求 `scope_hash="scope-X"` 仍返回 allowed。而 `mint_runtime_generation` 的 `scope_hash` 默认 `None`（`:1077`），即**最常见的铸造方式产出的运行代永远不可能触发 SCOPE_HASH_MISMATCH**。
  2. `expected.scope_hash` 从不与 `actual.scope_hash` 比较（只比较 `generation_id/account_ref/environment`，`:1162-1167`）→ 两个仅 scope 不同的运行代互相验证为通过。
- 对 U02 的影响：U02 若把 `entry_readiness` 的 `scope_hash`（`src/core/entry_readiness.py:121,146,149`）接进 `generation.scope_hash` 做域绑定，会得到**永远通过**的假证。`authorize_new_entry` 的 `scope_hash` 参数（`:1310`）同理是装饰性的。
- 建议：缺失即拒绝（`SCOPE_HASH_UNBOUND` / `SCOPE_HASH_MISMATCH`），并把 `expected.scope_hash` 纳入比较。

### P1-2 `mint_runtime_generation` 静默忽略 `environment` 参数（传 AccountRef 时）

- 位置：`src/core/runtime_identity.py:1085-1087`（AccountRef 分支只取 `ref`）与 `:1097-1098`（`environment=ref.environment`）
- 复现（`probe_account_ref.py`，A 段）：
  ```
  === A: mint 的 environment kwarg vs AccountRef 内环境（谁生效） ===
  mint(environment='live', account_ref=demo-Ref): -> environment= 'demo' account_ref= 'demo:acct'
  ```
  即 `mint_runtime_generation(environment="live", account_ref=derive_account_ref("demo", declared_account_ref="acct"))` **成功返回 environment="demo"**，无异常、无警告。
- 影响：U02 会把 `generation.environment` 写进 `ManagementAuthorization.environment`。若 scope 与 AccountRef 来源不一致（例如 profile 解析出的 live scope + 上游缓存的 demo AccountRef），产出的是**绑定到 demo 的授权**，而 K04 只比对 `request.environment == authorization.environment` —— 两者都是 "demo" 时 K04 会放行。这是“live 进程用 demo 授权放行”的静默路径，且作者 docstring（`:1080-1083`）声称 generation_id “由环境/账户/实例/起始时间决定”，与实现的环境来源不一致（P3-3）。
- 建议：`environment` 与 `AccountRef.environment` 不一致时结构化拒绝，或删掉该参数只留 AccountRef。

### P1-3 `mint_runtime_generation(account_ref=<str>)` 不补 `env:` 前缀 → 同一账户两种 account_ref

- 位置：`src/core/runtime_identity.py:1085-1087`（字符串分支不做前缀归一化）
- 复现（`probe_failclosed.py`，F7 段）：
  ```
  mint(account_ref='binance-main').account_ref: binance-main
  mint(account_ref=derive('binance-main')).account_ref: live:binance-main
  两者 account_ref 相等（K04 比对口径）: False
  ```
- 影响：K04 `validate_authorization` 用 `str(request.get("account_ref")) == authorization.account_ref` 精确比对（`src/core/contracts/k04_management.py:243`）。同一账户因构造入口不同得到两个不同 `account_ref` → 稳定的 `ACCOUNT_MISMATCH` 假阳/假阴。
- 建议：字符串入参一律经 `derive_account_ref` 归一化（或直接拒绝裸字符串）。

### P1-4 account_ref 与密钥轮换解耦 **在仓库层面不成立**（作者自述属实，且未登记）

- 位置：`src/core/runtime_identity.py:1476-1482`（`FINGERPRINT_R1_DISPOSITION["diff-7"]` 标为 `fixed`，note 仅提“接线归 U13”）；对照 `src/exchange/trade_reconciliation.py:66-69`（`account_reference = f"{env}:{sha256(api_key)[:16]}"`）及其 live 调用点 `src/exchange/binance_futures.py:198`（`account_ref=account_reference(api_key, environment)`）。
- 复现（`probe_account_ref.py`，C 段）：
  ```
  U15 derive_account_ref('live', declared='binance-main'): live:binance-main
  ledger account_reference(key_v1,'live'): live:8c9c2ba2efcdc984
  ledger account_reference(key_v2,'live')  [轮换后]: live:ee8cf2aeb1a0929e
  ledger ref 是否随轮换漂移: True
  K04 ManagementAuthorization.account_ref 候选值是否与账本一致: False
  ```
- 事实链：
  1. U15 产出的 `RuntimeIdentity.account_ref`（`demo/live:local` 或 `<env>:<declared>`）与账本/生命周期/收入幂等键使用的 `account_ref`（`<env>:<sha256(api_key)[:16]>`）**不是同一个值**，同一进程内并存两个账户标识。
  2. 账本侧那个仍随密钥轮换漂移 —— 正是 `§12 L9`“轮换与账本连续归集由 U15 落地”要解决的问题，`trade_reconciliation.py` / `binance_futures.py` 均未替换。
  3. `runtime_identity.py` 全文**没有出现 `trade_reconciliation`/`binance_futures` 字样**（grep 无命中），即作者已知该耦合但未在模块的任何处置表里登记这一残余（`FINGERPRINT_R1_DISPOSITION` 只有 diff-7 的 U13 状态根口径）。
  4. `K03 IncomeEvent` 幂等键是 `(environment, account_ref, income_type, tran_id)`（`src/core/contracts/k03_income.py:4,74,97`）→ 账本 account_ref 漂移会直接切裂收入归集的账户分区。
- 对 U02 的直接影响：U02 必须给 `ManagementAuthorization.account_ref` 选一个值。选 U15 的（稳定但**与账本不一致**）→ 授权域与订单/生命周期归属域对不上；选账本 key 派生的（一致但**轮换即漂移**）→ 恰好把 U15 声称已解耦的东西绑回密钥。**两条路都不通**，这是本任务对 U02 最重要的未闭合项。
- 建议（择一，必须落到文字而非 imply）：(a) 在 U15 内提供替代适配（如 `account_ref_from_legacy_key_ref()` 或让 `trade_reconciliation` 接受外部 `account_ref`）；(b) 明确 `<env>:<declared>` 为唯一权威账户域，并把 `trade_reconciliation`/`binance_futures`/K03 的接线写成 U02 的 `implementation_requires` 前置条件；(c) 至少在 `FINGERPRINT_R1_DISPOSITION` 登记为 `registered`（owner U02/U13）而非 `fixed`。

### P1-5 身份装配无法要求“已声明 account_ref”，默认静默回落 `env:local`（跨账户碰撞）

- 位置：`derive_account_ref` 的 `fallback_local=False` 分支存在（`:1022-1023`）但 `collect_runtime_identity` 未暴露该开关（签名 `:1212-1231`，调用 `:1250`），`ACCOUNT_REF_KEYS`（`:159`）为死常量
- 复现（`probe_failclosed.py`，F1/F2/F3 段）：
  ```
  derive_account_ref('live', fallback_local=False): MissingRuntimeInputError: ... account_ref (declared)
  collect_runtime_identity 是否暴露 fallback_local: False
  derive_account_ref('live') x2 相同: True live:local
  === F3 === 常量在模块内被引用次数（定义行以外）: 1   # 仅 __all__ 字符串，代码 0 次
  ```
  另有：`MTA_ACCOUNT_REF` 仅出现在本模块（`:159/:1013/:1244`，且 `:1244` 硬编码字符串而非用常量）；`.env.example` 变量名清单无 `MTA_ACCOUNT_REF`；`config_loader.py` 无相关处理。
- 影响：**生产实际走的就是回落路径** —— 没人会设置一个未被任何模板/文档宣告的变量，于是 `identity.account_ref == "live:local"`，同环境所有账户共用同一 account_ref（不同 API key 不可区分）。“与密钥解耦”退化为“与账户也解耦”。`stable=False` 只是标志位，无任何消费者会因此拒绝。
- 建议：把 `fallback_local` 提升为 `collect_runtime_identity` 的参数并默认 fail-closed（生产入口传 `False`）；把 `MTA_ACCOUNT_REF` 写入 `.env.example` 与配置文档。

### P1-6 WorkBuddy（实盘主决策 provider）路径无凭据/可用性判定，live 下放行新仓且写入伪证据

- 位置：`src/core/runtime_identity.py:732-738`（workbuddy 分支不触碰 `creds`，`mock = False` 恒定）、`:777`（`mock_source` 默认取 `"credential_present"`）
- 复现（`probe_workbuddy.py` + 追加探针）：
  ```
  === G1: resolve_model_route 在 workbuddy 分支是否看凭据 ===
  provider: workbuddy mock False model 'deepseek-v4.1-flash'
  blockers: ()
  sources: {'model': 'settings.llm.workbuddy.model', 'effort': 'settings.llm.workbuddy.effort'}

  === G2: live + workbuddy + 无任何凭据 -> authorize_new_entry ===
  mode/provider/mock: live workbuddy False
  bundle.blockers: ()
  authorize_new_entry(live, workbuddy, 无凭据): allowed= True http= 200 reasons= ()

  === G3: 对照 sensenova（非 workbuddy）同样条件 ===
  authorize_new_entry(live, sensenova, 无 key): allowed= False http= 409 reasons= ('MODEL_ROUTE_MOCK',)

  workbuddy overrides: {... "mock": false, "mock_source": "credential_present", "provider": "workbuddy"}
  ```
- 两条独立问题：
  1. **缺凭据不阻断**：`credentials_present={"OPENCODE_API_KEY": False, "SENSENOVA_API_KEY": False}` 在 workbuddy 分支**完全不参与判定**，`mock=False`、`blockers=()`，live 下 `authorize_new_entry` 返回 200。任务卡明确要求“Mock/缺 key 在 Demo/live 显式阻断新仓”（DC14/F13）。作者把 workbuddy 的无凭据当作“无 key 需求”处理，但接口既无法表达、也无法审计该前提，U02/看板无从区分“provider 可用”与“provider 未证明”。
  2. **伪证据进身份哈希**：workbuddy 路径把 `mock_source` 写成 `"credential_present"`，而**全程没有检查任何凭据**（同一返回值在 sensenova 真实检查过凭据时也出现）→ `model_route_hash` 记录了一个未验证的事实声明。这与模块自述边界 #3（`:15-16`“解析路由后给出 mock 判定与阻断原因”）不符。
- 建议：workbuddy 分支至少产出显式可用性证据（如 `provider_capability_unproven` / `mock_source="provider_cli"`），并在缺证据时给出阻断码；若确定 workbuddy 无需凭据，请在契约/处置表登记该判定，别让 `credential_present` 承担它没有的含义。

### P2-1 畸形输入抛 `AttributeError/TypeError` 而非结构化拒绝

- 位置：`verify_account_binding:1154,1162`（直接属性访问）、`authorize_new_entry:1324`（`bundle.blockers`）、`mint_runtime_generation:1093`（`credential_epoch < 0`）
- 复现（`probe_binding.py` P6 段、`probe_failclosed.py` F5 段）：
  ```
  verify_account_binding generation=dict: AttributeError: 'dict' object has no attribute 'generation_id'
  authorize_new_entry expected=str: AttributeError: 'str' object has no attribute 'generation_id'
  authorize_new_entry bundle=object(): AttributeError: 'object' object has no attribute 'blockers'
  mint(credential_epoch='3'): TypeError: '<' not supported between instances of 'str' and 'int'
  rotate_credential(dict/str/None): AttributeError: ...
  ```
- 定性：**不是 fail-open**（不会放行），但不满足审查要点 4“畸形/缺失输入结构化拒绝，不崩溃”。模块自述面向看板控制接口（R11）消费，未捕获的 `AttributeError` 在 HTTP 处理器里表现为 500，而不是可判定的 409/400。
- 建议：入口做类型哨兵，返回 `*_MALFORMED` 原因码（与 K02 r5 的 `RulesInputError` 口径一致）。

### P2-2 `http_status` 是 U15 自创面，与 K04 无对应，且与控制面 400/403/409 口径不一致

- 位置：`PermissionDecision.http_status`（`:1288-1299`），取值仅 `200/409`（`:1337-1340`）
- 事实：`grep -rn "409" docs/contracts/README.md docs/无人值守…规格…md` **零命中**；K04 `AuthorizationVerdict`（`k04_management.py:196-208`）无 HTTP 字段。全仓**无任何消费者**（`grep -rn "authorize_new_entry|PermissionDecision|http_status" src/ tests/` 仅命中模块与作者测试）。
- 影响：不是契约冲突，但 (a) 身份类、readiness 类、git 类阻断全被压成同一个 409，丢失了控制面已建立的区分度；(b) 没有 400 分支承接 P2-1 的畸形输入；(c) 该字段目前是“看起来能用”的空壳。
- 建议：明确 `http_status` 仅为建议值并登记为 U15 扩展面（或删除，交由看板层映射）。

### P2-3 `mint_runtime_generation` 不校验 `environment` 合法值

- 位置：`:1085-1088`（无 `MODE_ALLOWED` 校验）
- 复现（`probe_failclosed.py` F6 段）：
  ```
  mint(environment=''): OK environment='unknown'
  mint(environment='   '): OK environment='unknown'
  mint(environment='prod'): OK environment='prod'
  ```
- 对比：同模块 `resolve_execution_scope`（`:529-567`）对未知模式/profile fail-closed，DL01 负例已验收。两处口径不一：执行作用域拒绝 `"prod"`，运行代铸造接受 `"prod"`。
- 建议：复用 `MODE_ALLOWED` 校验（注意 K01 golden 的 `demo` 与 `testnet/paper` 语义）。

### P2-4 `started_at=None` 时 `generation_id` 不能证明“同一运行代”

- 位置：`:1095-1096`（`started_at` 缺省为 `None` → seed 尾部为空串）
- 复现（`probe_failclosed.py` F4 段）：
  ```
  started_at=None 两次 mint 相同: True gen-16b653f8c8a05a22c39c
     => 旧/新进程同 rt-1 是否可经 generation_id 区分: NO
  ```
- 影响：U02 的绑定前提是“同一账户同一运行代”（`:1147` docstring），而两个并发/重启进程只要 `runtime_instance_id` 相同就得到**完全相同**的 `generation_id`。规格 §10.3 要求“新旧代不得同时持有可发单能力”，`generation_id` 无法承担该证明。作者已把 `runtime_instance_id` 持久化/实例锁登记为 `diff-6`（owner U13），但 U02 依赖的是“绑定有效性”，需要知道这个前提。
- 建议：把 `started_at` 从“可选”改为必传（或 require 单调 epoch），或在文档中显式写明 generation_id ≠ 进程身份证明。

### P2-5 U15/U02 边界未在模块内写清（K04 12 字段中仅 3 个由本模块提供）

- 复现（`probe_account_ref.py` E 段）：
  ```
  K04.authorization_id/lifecycle_id/position_side/max_reduce_qty/target_protection/
  position_revision/nonce/issued_at_ms/expires_at_ms: UNPROVIDED
  K04.account_ref / environment / runtime_instance_id: RuntimeGeneration
  ```
- 定性：**按 `规格 §301` 这属 U02 职责，不是缺陷**。但模块 docstring 只笼统写“向 U02 提供账户/运行代/权限验证接口”，没有列出“U02 必须自建：nonce、时效（issued/expires）、revision、max_reduce、lifecycle、position_side、target_protection、以及授权→意图→命令关联 ID（README §12 L7/B-O8）”。U02 直接按本模块的 `__all__` 推断能力边界会踩空：**本模块不提供任何时效/TTL 概念**（`RuntimeGeneration` 无 `expires_at`、无时钟注入），而“时效”正是 U02 的绑定需求之一。
- 建议：在模块 docstring 与处置表写明这条分工与 U02 的自建清单（下游缺口索引已有 B-O3/B-O8/L7，可交叉引用）。

### P2-6 零共享适配补丁；新增第二个新仓门禁与 `entry_readiness` 并存

- 事实：`git diff --name-only ffdab44 a136cb5` 仅 3 个独占路径文件；4 个集成目标与 `docs/` 均未改动。全仓 `authorize_new_entry` 无消费者。
- 影响：`src/core/entry_readiness.py` 已有 `new_entry_allowed` + `authorization_context()` + `scope_hash`（`:22-56,121-149`），其模块 docstring 明写“看板是否绿灯和执行层是否允许 open 必须是同一个确定性判定”。U15 新增的 `authorize_new_entry` 把 `readiness_allowed` 当**不透明入参**（`:1312,1334`），两边不互认、`scope_hash` 无共同派生（U15 由调用方给，entry_readiness 由 TradingScope 给）→ 又出现两套门禁的定义源，恰是该文件要防止的情形。
- 建议：至少给出 `IdentityBundle`/`PermissionDecision` ↔ `entry_readiness` 的适配补丁（作者交付、I 合入），或把 `authorize_new_entry` 明确标注为“待 U13 接线的候选接口”。

### P3-1 作者验收存在**恒真断言**

- 位置：`tests/acceptance/test_runtime_identity.py:448-450`
  ```python
  check("workbuddy-allowed-in-live", wb_demo.blockers == () or
        "WORKBUDDY_OUTSIDE_LIVE" not in model_route_blockers(
            wb_demo.route, mock=False, execution_mode="live", provider="workbuddy"))
  ```
- 复现：右操作数实测恒为 `True`（live 且 `mock=False` 时永不产生该码），故该 check 永不失败；而左操作数实测为 `('WORKBUDDY_OUTSIDE_LIVE',)`（真实期望）。
  ```
  wb_demo.blockers = ('WORKBUDDY_OUTSIDE_LIVE',)
  右操作数 = True
  作者断言 X or not Y 的求值 = True
  ```
- 建议：删除 `or` 分支。这也说明“全 PASS”不能作为该断言的证据。

### P3-2 `__all__` 存在重复项

- 位置：`:54`（字面量列表内）与 `:1517`（`__all__.extend`）重复 `CONTROL_STATE_SEVERITY`
- 复现：`__all__ size 60 unresolvable: [] duplicates in __all__: ['CONTROL_STATE_SEVERITY']`（60 个名字全部可解析，无拼写错误）

### P3-3 docstring 与实现不符（环境来源）

- 位置：`:1080-1083` 声称 generation_id 由“环境/账户/实例/起始时间”决定；实际“环境”取自 `AccountRef.environment`，与 `environment` 形参无关（见 P1-2）

---

## 4. U02 可用性评估（结论：当前**不可直接采用**）

### 4.1 可用部分（已独立验证）

- `RuntimeGeneration` 的 `account_ref/environment/runtime_instance_id/generation_id/scope_hash/credential_epoch/key_fingerprint` 与 `as_dict()`：**指纹不可逆**（`key_fingerprint` 实测 16 hex、原文不出现）、**轮换不改变 account_ref/generation_id/scope/instance**、epoch 正确 +1（`probe_account_ref.py` D 段：`verify_account_binding(gen, rot, scope_hash="scope-1").allowed == True`，`epoch 0 -> 1`）。给 `ManagementAuthorization` 提供 `account_ref/environment/runtime_instance_id` 三个字段是够用的（**前提**：按 P1-2/P1-3 修正构造口径）。
- 脱敏：`as_dict()` 与 `IdentityBundle.as_dict()` 不放原始密钥（作者测试的 SECRET 三例 + 我的 `env` 带 3 个凭据值均未出现在序列化结果中）。
- 控制面纯策略（`is_loopback_host` / `verify_control_write` / `authorize_control_request`）与 B 线审查范围无关，未发现问题；`is_loopback_host` 对 `[::1]`/`::ffff:127.0.0.1` 的接受与 `127.0.0.1.evil.com`/`::` 的拒绝方向正确。

### 4.2 U02 的绑定需求 vs 本模块供给

| U02 需求（K04 + §10.2） | 本模块供给 | 结论 |
|---|---|---|
| account_ref | `RuntimeGeneration.account_ref` | **有条件**：默认值 `<env>:local` 跨账户碰撞（P1-5）、字符串入口不归一（P1-3）、与账本不一致（P1-4） |
| environment | `generation.environment` | **有条件**：mint 可静默取错环境（P1-2），且无 `MODE_ALLOWED` 校验（P2-3） |
| runtime_instance_id | ✅ 直接可用 | 可用 |
| revision / max_reduce_qty / lifecycle_id / position_side / target_protection / nonce | ❌ 不提供 | 设计如此（§301），但需作者写明边界（P2-5）；U02 自建 |
| **时效**（issued_at / expires_at） | ❌ 不提供，模块无任何 TTL/时钟概念 | U02 自建；本模块**无法**校验“运行代是否仍有效” |
| scope_hash 域绑定 | 有字段与参数 | **不可用**：fail-open（P1-1） |
| credential_epoch（轮换审计） | ✅ 有，且不参与判定（轮换不否决在途） | 可用；但**没有**“要求 epoch 必须相等”的可选严格模式，U02 若需要“旧 epoch 签名作废”只能自建 |

### 4.3 需要作者补什么（按优先级）

1. **P0-1**：让 `authorize_new_entry` 在未做绑定校验时**拒绝**（或把 `expected` 变必填），并把“未校验”写进 `reason_codes`。
2. **P1-1**：修 `scope_hash` 双缺口（缺失即拒 + 比较 `expected.scope_hash`）。
3. **P1-2/P1-3**：`mint_runtime_generation` 环境一致性校验 + account_ref 字符串归一化。
4. **P1-4**：对 `account_ref` 与账本（`trade_reconciliation`/`binance_futures`/K03 幂等键）的关系给出**明确处置**：替代适配、或登记为 U02 前置条件（改 `diff-7` 状态），不能留“已修”的假象。
5. **P1-5**：生产入口默认 `fallback_local=False`；`MTA_ACCOUNT_REF` 进 `.env.example`/配置文档。
6. **P1-6**：WorkBuddy 分支给出显式可用性/凭据证据与阻断码，去掉未验证的 `mock_source="credential_present"`。
7. **P2-5**：在 docstring/处置表写明 U02 必须自建的字段清单（nonce/时效/revision/max_reduce/lifecycle/side/target_protection）。
8. **P2-6**：给出 `entry_readiness`（与 `release_manifest`/`config_loader`/`app.py`）的作者侧适配补丁，避免第二套门禁定义源。
9. **P2-1/P2-2**：畸形输入结构化拒绝；`http_status` 定性（建议值 vs 删除）。
10. **P3-1**：删除恒真断言。
11. 可选：`IdentityBundle` 增加 `account_ref` 溯源（`AccountRef.as_dict()`）与 `RuntimeGeneration`，让 U02 不必二次派生（当前 `__dataclass_fields__` 无 account 相关字段；`identity.as_dict()` 不含 stable）。

---

## 5. 未覆盖 / 无法离线验证

1. **账本侧真实接线**：未启动服务、未访问 `data/`/数据库，`trade_reconciliation` 的 account_ref 漂移对历史分区的影响是用源码+纯函数复现推断的，未做端到端（属于 U02/U13 集成期验证）。
2. **WorkBuddy 的“是否需要凭据”**：无网络、未读 `.env`，无法核实 `src/llm/workbuddy_client.py` 的真实凭据需求（`docs/WorkBuddy_DeepSeek_V4.1_实盘决策适配说明_2026-09-16.md:31-33` 只说明由 `MTA_AI_DECISION_PROVIDER` 启用）。因此 P1-6 的定性是“缺乏证据与阻断”，而非“必然缺 key 却放行”。
3. **看板 `/control` 与身份门禁的 HTTP 映射**：未启动 `dashboard/app.py`，未验证 500 vs 409 的实际表现（P2-1/P2-2 的推论基于无消费者 + 未捕获异常）。
4. **无 pytest**：仅按作者脚本的独立可运行入口复跑；未跑仓库其余测试，未做全量回归（作者改动仅 3 个新文件，回归面应为 0，但未实测共享文件的导入兼容）。
5. **K01/K04 golden 冒烟**：未运行（`docs/contracts/golden/` 由 I/Q 口径驱动）；已核对 K01 golden 用 `demo:SYNTH_ACCT` 形态，与 `derive_account_ref` 声明路径一致，但与 `<env>:local` 回落路径不一致 —— 该回落路径**没有任何 golden/契约覆盖**（P1-5 的旁证，非独立结论）。
6. **`started_at` 的实际调用约定**（U13 实例锁是否提供稳定起始时间）未核实，P2-4 只证明“缺省时不可区分”。
7. 作者交付物与其他 3 个共享适配目标的**接口契约**（如 `release_manifest.ReleaseManifest` 是否应改为消费 `IdentityBundle`）未评估 —— 无补丁即无审查对象。

---

## 6. 附：探针文件与原始输出

| 文件 | 覆盖 |
|---|---|
| `_scratch_B/probe_binding.py` / `.out.txt` | P0-1、P1-1、bundle↔generation 环境不绑定、paper 放行、畸形输入 |
| `_scratch_B/probe_account_ref.py` / `.out.txt` | P1-2、P1-4、P1-5、轮换解耦正例、K04 字段覆盖 |
| `_scratch_B/probe_failclosed.py` / `.out.txt` | P1-3、P1-5、P2-3、P2-4、P2-1 补充、bundle 溯源缺失 |
| `_scratch_B/probe_workbuddy.py` / `.out.txt` | P1-6（G1/G2/G3 + mock_source 伪证据） |

探针均为一次性脚本，未改动被审代码；工作区保持 detached 只读状态（`git status --short` 为空）。

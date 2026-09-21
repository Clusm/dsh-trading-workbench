# U11 补丁包

- 任务：U11 核心模块渐进拆分（F 线 / EF 线 / 后置 W4+）
- 作者分支：`task/U11`，base `03967e9`
- 承载提交：`00706d5`（`docs(U11): add core-module incremental split interface mapping`）
- 目录名 `00706d5` = 本任务唯一内容提交的短 hash。

## 补丁数量：**0**（无需任何共享文件补丁）

本任务**只写独占路径** `docs/重构接口映射.md`，**没有触碰任何 `src/`、`scripts/`、
`pyproject.toml`、`docs/文档索引.md` 或其它共享文件**。因此**不存在需要 I 应用的 diff**。

对照 U11 卡面：本任务是"**映射/设计**交付，不是代码重构"——卡面独占路径只有这一篇文档，
且任务卡原话"逐模块转移所有权后实施"。真正的拆分属**后续独立任务**，需 I 单独登记所有权
并分派，不在本补丁包范围内。

## 为什么不给 `docs/文档索引.md` 加一条链接

`文档索引.md` 是 U10a 的独占路径（**不是** U11 的），加链接会引入一条需要 I 合入的共享补丁。
考虑到：

1. 本任务是**后置规划映射**，尚未落地任何拆分，把它登记为"当前权威文档"会**夸大**它的性质
   （它是"方案意图"，不是"描述当前代码行为"）；
2. 少一条跨任务补丁，就少一处"单元已验证、适配未落地"的集成风险（见任务卡 C 系列教训）。

故**有意不加**。若 I 认为该文档应进索引，请按 U10a 流程单独派发。

## 门禁复核（作者实跑，base `03967e9` + 本提交）

| 命令 | 结果 |
|---|---|
| `python scripts/run_offline_regression.py` | 退出码 **0**；`SMOKE MANIFEST OK included=81 excluded=1`；`OFFLINE REGRESSION OK scripts=85` |
| `python scripts/check_quality.py` | 退出码 **0**；`QUALITY CHECK OK files=301 untracked=0`（与 base 同值：本任务只加 `.md`，不增 Python 文件） |
| `python scripts/smoke_contracts_v3.py` | 退出码 **0**；`PASS 173 / FAIL 0`，golden 160 逐字未变 |
| `python scripts/smoke_u10a_release_package.py` | 退出码 **0**；`所有相对文档链接可达（独立解析，43 篇 md）`——证明新文档的 Markdown 相对链接全部可达 |

**未登记 `scripts/run_offline_regression.py`**：本任务**没有新增任何 `scripts/smoke_u11_*.py`**，
也没有移动/改名任何 `smoke_*.py`，故 `SCRIPTS`/`EXCLUDED_SMOKE_SCRIPTS` **一行未改**。

## 应用方式

无补丁可应用。若 I 只想带走在产文档：

```bash
git checkout <U11 分支> -- docs/重构接口映射.md
```

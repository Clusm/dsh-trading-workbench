# 开发与维护约定

本项目参考 Google Python Style Guide 的可读性原则，但以交易安全、行为兼容和
可审计性为最高优先级。规范化不得隐式修改风险档、交易白名单或订单语义。

## 代码约定

- Python 3.11+，UTF-8，4 空格缩进，建议每行不超过 100 个字符。
- 模块、类、公开函数使用中文 docstring，说明职责、关键不变量、副作用和失败策略。
- 注释解释“为什么”和安全边界，不逐行翻译代码；单位必须写进名称或注释。
- 公开接口和跨模块数据使用类型注解；动态交易所响应允许保留 `dict[str, Any]`。
- 捕获宽泛异常时必须明确降级策略、记录审计事件，或仅用于资源清理。
- 核心路径禁止裸 `print`；运行事件进入结构化日志或审计数据库。
- 不在源码中读取、输出或硬编码密钥。诊断工具必须放在 `scripts/`，默认只读。

## 核心不变量

- 只处理币安 USD-M、USDT 本位数字货币永续合约白名单。
- `config/risk_profile.lock.json` 的哈希与锁定字段不得被维护性改动改变。
- Web3/市场情报保持影子模式和 `execution_weight=0`，除非另行完成评审授权。
- 下单必须经过 ActionSpace、确定性风控、Outbox 和保护单流程。
- 状态文件必须从 `project_root` 派生；测试必须使用临时项目目录，禁止污染真实 `data/`。

## 提交前检查

```powershell
.venv\Scripts\python.exe scripts\check_quality.py
.venv\Scripts\python.exe scripts\smoke_service_lifecycle.py
.venv\Scripts\python.exe scripts\smoke_ui.py
git diff --check
```

涉及数据库、订单、保护单、资金或情报时，还应运行对应的 `smoke_*.py`。测试通过只表示
代码路径满足断言；涉及服务状态的变更仍需重启后检查 `/api/health`、`/api/ready` 和
`/api/status`。

## 提交边界

- 每个提交只解决一个可描述的问题，维护性重构与交易行为变更必须分开。
- 不使用 `git add .`，避免提交运行数据、密钥、临时诊断文件和用户未完成工作。
- 不用 `git reset --hard` 或删除数据库来“修复”测试。

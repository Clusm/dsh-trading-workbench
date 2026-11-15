# 实际运行只读核验

核验时间：2026-11-13 21:59:52—22:00:12（北京时间）。此报告为实时 HTTP 核验，非 fixture；只读请求未提交交易。未记录 token、API key、secret 或账户标识。

## 已验证

| 检查 | 结果 |
|---|---|
| Service `/api/status` | HTTP 200，venue=demo，demo_only=true；凭据已配置 |
| 交易所连接 | reachable=true，stale=false，error=null；两次快照时间分别为 13:59:52Z、14:00:11Z |
| 账户数据 | 交易所权益/可用余额约 5012.5813；持仓 0、未结订单 0；outbox unknown=0、submitting=0 |
| BTCUSDT `/api/market/prices` | last=69543.5，mark=69565.81378781，age_ms=557，complete=true，errors=[] |
| `/api/market/health` | GOOD，score=1，issues=[]，REST ok，clock_trusted=true；WebSocket disabled，使用 REST |
| Host `/trading-workbench/api/state` | HTTP 200，service online/ready=true；portfolio source=service、非缓存、age=0 |
| Host 自动调度 | enabled=false，无运行中轮次 |

## 仍需处理

1. Host 当前加载版本仍为 `twb-20261112T000426Z-2d449fc4`，loaded_at=2026-11-12T00:04:26.977Z。需要重载新版并重新核验。
2. Host health 所选默认路由是 `opencode-go/deepseek-v4.1-flash`，但实际持久化的工作台角色配置为 `qoder/dfmodel`（revision=1）。这不等于已确认配置错误，但必须根据可用模型目录与实际调用证明其可用性，不能用 health 默认路由替代角色生效验收。
3. Host portfolio 报 `reviews unavailable (E_NOT_FOUND HTTP 404)`，对应 reviewsGap=`E_REVIEWS_ENDPOINT_MISSING`；其他账户/持仓数据已联通。
4. Service `equity.session` 约 4947.4255，`equity.exchange` 约 5012.5813；风控和界面部分字段使用 session。需要确认会话权益口径及更新行为，不能直接把二者差异认作接口离线。
5. 成交回填 complete=false，今天收益 coverage=local_records_only，净盈亏/资金费仍可能为 null。这些质量标签必须保留。

## 边界

本核验只证明账户、REST 行情与 Host→Service 的实际链路已通，不能证明新版 Host 已部署、模型完整决策链通过、模拟盘下单成功或长时运行稳定。GET 可能触发正常只读对账与本地缓存/台账更新；未调用运行、下单、平仓、修复保护或控制态接口。

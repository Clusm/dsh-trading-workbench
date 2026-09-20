# U14 告警夹具（tests/fixtures/alerts/）

全部为**合成**数据：不含真实账户标识、密钥、签名或原始 prompt；不含真实告警渠道
地址；不联网。每条夹具在 `derivation` 里写明期望值来自规格条款/公式，**禁止**
复制被测实现的输出自证（Q 独立验收据此复核）。

| 文件 | 覆盖的必达要点 | 期望来源 |
|---|---|---|
| `heartbeat_fresh.json` | 心跳携带交易证据时间 | 规格 §11.2「外部心跳包含发送时间、boot/session ID、序号、最后完成快循环与最后持久化证据时间」 |
| `heartbeat_rejections.json` | 陈旧重放/时钟异常不得更新健康时间（R13） | 规格 §11.2「陈旧payload、重放或时钟异常不更新健康时间」 |
| `outage_cases.json` | 本机主进程故障与主机关机分别可检测（F06/F12/F22） | 判别式：`host_uptime < evidence_age ⇒ HOST_DOWN`，否则 `PROCESS_MISSING` |
| `delivery_ladder.json` | 接收/送达/确认三级分开（R14） | K05 `evaluate_state`：HTTP200→SUBMITTED；有 `provider_receipt`→ACCEPTED；有 `delivered_at_ms` 且设备回执→DELIVERED；人确认→ACKNOWLEDGED |
| `message_redaction.json` | 告警消息不含密钥/签名/原始 prompt | 规格 §11.2 消息字段清单 + `docs/contracts/README.md` §12 L1（只查键名） |
| `maintenance_window.json` | 维护窗口有界、不无限静音 | 规格 §11.2「正常维护窗口也记录暂停范围和到期时间，不能无限静音」 |

离线用法：

```bash
PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_alert_delivery.py
PYTHONUTF8=1 <venv>/python.exe scripts/smoke_u14_alert_delivery.py
```

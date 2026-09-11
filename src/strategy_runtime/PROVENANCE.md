# 策略运行时来源记录

本目录是交易运行时所需的最小确定性子集，而非完整研究项目。它在 2026-09-11
从同一工作区的 `../../ai-trader/strategy_lab/src` 纳入，以消除运行时对外部绝对路径、
`sys.path` 修改及全局 `signals`/`features` 模块名的依赖。

纳入时未发现该研究目录的许可证文件。除 Python 包的相对导入改写外，函数算法、
参数和注册表均保持来源版本不变。以下为纳入前来源文件的 SHA-256，供日后升级时
逐项复核：

| 来源文件 | SHA-256 |
| --- | --- |
| `indicators.py` | `E2D4138AD0494AA048EA4C97457B416303C24E5EF659F93EA50D1223B5102392` |
| `features.py` | `FFFD1C64C1E43F638C598F786DF75F76D8FB13A9F9FFFD352E6592FF8A0E226C` |
| `signals.py` | `1C82D5305144BC12C5E0B009363BA9D09411A4B571BFAA1B9BEBE66DE9A667EF` |
| `signals_v2.py` | `3E0A5B1F1F712EB380046532B101C182B2317E2CAF064DF566F06407E2AE2D6C` |
| `signals_v3.py` | `69ECCDBB77CA78FBD15E16CE97279E965DBBA9E2A6A240A1A719949FC5B453F5` |

升级规则：先在研究仓库完成回测和策略卡验收，再对比注册表、合成 OHLCV 输出和本
目录哈希；未经独立评审，不得直接把研究中的新策略接入实盘扫描。

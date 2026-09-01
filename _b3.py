import re, json, time
from pathlib import Path
import httpx
env = Path(".env").read_text(encoding="utf-8")
key = re.search(r"OPENCODE_API_KEY=(.+)", env).group(1).strip()
# 模拟 orchestrator 的大 prompt：市场+宏观+链上情报 + 策略引用 + 工具说明
big_prompt = (
    "你是多Agent加密货币合约交易系统中的主决策Agent。\n"
    "## 市场情报\nBTCUSDT 1h 当前价 78340，24h涨跌幅+1.2%，ATR=512，资金费率0.01%，未平仓合约量OI环比+3.2%。"
    "ETHUSDT 1h 当前价 2470，24h涨跌幅+0.8%，ATR=18.5，资金费率0.005%。SOLUSDT 1h 当前价 152.3，涨跌幅-0.4%。"
    "整体市场情绪：risk_on，BTC 主导，山寨跟随。\n"
    "## 宏观情报\n美元指数 104.2 走弱，美债10Y收益率 4.15%，风险偏好回升。本周关注美联储官员讲话与CPI数据。\n"
    "## 链上情报\nBTC 交易所净流出 1200枚，稳定币市值+1.1%，巨鲸增持 BTC 500枚。ETH 质押率上升。\n"
    "## 策略引用\nF_S08_1H 策略卡：突破后动量跟踪，适用震荡转趋势行情，当前未触发信号。"
    "F_EMA_15M 策略卡：均线金叉做多死叉做空，15m级别适用，当前无信号。\n"
    "## 交易规则\n1. 所有开仓必须带止损和止盈。2. 逐仓模式。3. 常规杠杆20x封顶，强信心25x。"
    "4. 只有确定信号才开仓，否则等待。\n"
    "## 任务\n基于以上全部情报，输出你的交易决策JSON。格式："
    "{\"decision\":\"WAIT|OPEN\",\"action\":\"NONE|OPEN_LONG|OPEN_SHORT\",\"symbol\":\"BTCUSDT\","
    "\"side\":\"LONG|SHORT\",\"quantity\":0.01,\"entry_price\":78340,\"stop_loss\":77800,"
    "\"take_profit\":79500,\"leverage\":20,\"reasoning\":\"你的决策理由\"}"
)
for effort in ("high", "max"):
    payload = {"model": "glm-5.3-flash",
               "messages": [{"role": "user", "content": big_prompt}],
               "temperature": 0.3, "max_tokens": 8000, "stream": False,
               "reasoning_effort": effort, "thinking": {"type": "enabled"}}
    t0 = time.monotonic()
    try:
        r = httpx.post("https://tokenrhythm.studio/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=90)
        dt = time.monotonic() - t0
        d = json.loads(r.text)
        fr = d["choices"][0].get("finish_reason")
        rl = len(d["choices"][0]["message"].get("reasoning_content") or "")
        print(f"[{effort}] HTTP {r.status_code} | {dt:.1f}s | finish={fr} | reasoning_len={rl} | content={d['choices'][0]['message'].get('content','')[:60]!r}")
    except httpx.TimeoutException:
        print(f"[{effort}] CLIENT_TIMEOUT({time.monotonic()-t0:.1f}s)")
    except Exception as e:
        print(f"[{effort}] EXC {type(e).__name__}: {str(e)[:150]}")

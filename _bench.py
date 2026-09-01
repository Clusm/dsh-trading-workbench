import re, json, time
from pathlib import Path
import httpx
env = Path(".env").read_text(encoding="utf-8")
key = re.search(r"OPENCODE_API_KEY=(.+)", env).group(1).strip()
prompt = ("你是加密货币合约交易主决策Agent。当前BTCUSDT 1h震荡，无信号无持仓。"
          "请输出决策JSON：{\"decision\":\"WAIT\",\"action\":\"NONE\",\"symbol\":null,"
          "\"side\":null,\"quantity\":0,\"reasoning\":\"简述\"}")
for model, effort in (("qwen3.7-max", "high"), ("qwen3.7-flash", "low")):
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.3, "max_tokens": 2000, "stream": False,
               "reasoning_effort": effort, "thinking": {"type": "enabled"}}
    t0 = time.monotonic()
    try:
        r = httpx.post("https://tokenrhythm.studio/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=90)
        dt = time.monotonic() - t0
        try:
            d = json.loads(r.text)
            model_echo = d.get("model")
            fr = d["choices"][0].get("finish_reason")
            content = d["choices"][0]["message"].get("content", "") or ""
            print(f"[{model}] HTTP {r.status_code} | {dt:.1f}s | echoed_model={model_echo} | finish={fr} | content={content[:60]!r}")
        except Exception:
            print(f"[{model}] HTTP {r.status_code} | {dt:.1f}s | raw={r.text[:150]!r}")
    except Exception as e:
        print(f"[{model}] EXC {type(e).__name__}: {str(e)[:200]}")

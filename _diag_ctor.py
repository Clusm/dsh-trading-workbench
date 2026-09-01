"""诊断：构造 TradingService 并跑一轮，每 8s 转储线程栈，60s 超时。"""
import sys, threading, time, traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

stop = False

def watchdog():
    t0 = time.monotonic()
    while not stop:
        time.sleep(8)
        print(f"\n===== DUMP @ {time.monotonic()-t0:.0f}s =====", flush=True)
        for th in threading.enumerate():
            frame = sys._current_frames().get(th.ident)
            print(f"--- thread: {th.name} (alive={th.is_alive()}) ---", flush=True)
            traceback.print_stack(frame, limit=10)
            print(flush=True)
        print("===== END DUMP =====\n", flush=True)

threading.Thread(target=watchdog, daemon=True).start()

from core.config_loader import load_settings
settings = load_settings(PROJECT_ROOT)
print("settings loaded", flush=True)

from core.service import TradingService
print("constructing TradingService...", flush=True)
t0 = time.monotonic()
service = TradingService(settings)
print("TradingService constructed in %.1fs" % (time.monotonic()-t0), flush=True)
stop = True
try:
    service.close()
except Exception:
    pass
print("DONE", flush=True)

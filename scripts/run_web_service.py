"""旧版脱离拉起入口：记录日志后运行统一的 run_web.py。

为什么存在
----------
start_web_detached.py 用 Popen+DETACHED_PROCESS 拉起子进程，但在自动化
会话里（job object, kill-on-close），会话结束时会连同孙进程一起回收，
表现为「服务启动几秒内静默消失、日志无任何报错」。

由 WMI Win32_Process.Create 拉起的进程父级是 WMI 服务宿主，不受本会话
job 影响。本包装不再硬编码或覆盖代理；统一入口 ``run_web.py`` 会按启动环境
和项目 ``.env`` 的显式开关解析出口，避免启动器日志与实际请求出口分裂。

用法
----
    python scripts/run_web_service.py                 # 默认 127.0.0.1:8000
    python scripts/run_web_service.py --port 8001     # 透传 run_web.py 参数

日志追加到 data/web_console.log。
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.environ["PYTHONUNBUFFERED"] = "1"

_log_path = os.path.join(ROOT, "data", "web_console.log")
_log = open(_log_path, "a", encoding="utf-8", buffering=1)
print("\n===== legacy detached entry =====", file=_log, flush=True)
sys.stdout = _log
sys.stderr = _log

os.chdir(ROOT)
_run_web = os.path.join(ROOT, "scripts", "run_web.py")
_extra = sys.argv[1:] or ["--port", "8000"]
sys.argv = [_run_web, *_extra]
runpy.run_path(_run_web, run_name="__main__")

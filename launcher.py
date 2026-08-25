"""背景啟動 backend（subprocess detach，存活於父 batch 結束後）

前端已 build 成靜態檔（frontend/dist），由 backend 直接 serve，不需要 Vite dev server。
"""
import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
FLAGS = 0x08000000  # CREATE_NO_WINDOW

LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LAUNCHER_LOG = os.path.join(LOG_DIR, "launcher.log")
BACKEND_LOG = os.path.join(LOG_DIR, "backend.log")


def log(msg):
    with open(LAUNCHER_LOG, "a", encoding="utf-8") as f:
        f.write(f"{msg}\n")


log(f"=== launcher start: sys.executable={sys.executable} cwd={os.getcwd()} ===")

try:
    backend_log_fh = open(BACKEND_LOG, "a", encoding="utf-8", buffering=1)
    backend_log_fh.write(f"\n=== backend launched at {__import__('datetime').datetime.now()} ===\n")
    p = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.join(HERE, "backend"),
        creationflags=FLAGS,
        stdin=subprocess.DEVNULL,
        stdout=backend_log_fh,
        stderr=backend_log_fh,
    )
    log(f"backend Popen OK: pid={p.pid}")
except Exception:
    log(f"backend Popen FAILED:\n{traceback.format_exc()}")

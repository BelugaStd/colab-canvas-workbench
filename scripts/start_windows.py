"""Start the current canvas backend, or reuse a compatible running instance."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8765"


def running():
    try:
        with urllib.request.urlopen(URL + "/api/state", timeout=2) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def open_when_ready():
    for _ in range(60):
        state = running()
        if state and state.get("features", {}).get("creative_canvas"):
            webbrowser.open(URL)
            return
        time.sleep(.5)


def main():
    state = running()
    if state is not None:
        if not state.get("features", {}).get("creative_canvas"):
            print("旧后台仍占用 8765。请在原启动窗口按 Ctrl+C，再运行此启动器。")
            print("此启动器不会终止其他进程，也不会打开旧界面。")
            return 2
        webbrowser.open(URL)
        return 0
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    identity = ROOT / ".data/colab-accounts/ssh/id_ed25519"
    if identity.is_file():
        os.environ.setdefault("WORKBENCH_SSH_IDENTITY", str(identity))
    import uvicorn

    threading.Thread(target=open_when_ready, daemon=True).start()
    print("Canvas: " + URL + " · 按 Ctrl+C 正常退出")
    uvicorn.run("backend.app:app", host="127.0.0.1", port=8765, timeout_graceful_shutdown=420)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Chaquopy entry point. Java calls start(data_dir, web_dir, port).

Environment must be set before app.config is imported, because config.py reads
TUNEBOX_DATA / TUNEBOX_WEB at import time. So the `import` lives inside start(),
after os.environ is populated — not at module top.
"""

import os
import threading

_started = False


def start(data_dir: str, web_dir: str, port: int) -> str:
    global _started
    if _started:
        return "already running"
    os.environ["TUNEBOX_DATA"] = data_dir
    os.environ["TUNEBOX_WEB"] = web_dir

    # Imported here, after the env is set (see module docstring).
    from app.server import serve

    def run():
        serve(host="127.0.0.1", port=port)

    threading.Thread(target=run, name="tunebox-server", daemon=True).start()
    _started = True
    return f"tunebox starting on 127.0.0.1:{port}"

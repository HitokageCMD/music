"""Run the server: python -m app  [port]  (or set TUNEBOX_PORT / TUNEBOX_HOST)."""

import os
import sys

from .server import serve

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("TUNEBOX_PORT", 8730))
    host = os.environ.get("TUNEBOX_HOST", "0.0.0.0")
    serve(host, port)

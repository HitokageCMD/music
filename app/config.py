import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# On Android the app package is read-only, so data must live in the app's private
# writable dir — Chaquopy passes it in via TUNEBOX_DATA. Everywhere else it's
# ./data next to the code.
DATA = Path(os.environ.get("TUNEBOX_DATA") or (ROOT / "data"))

# The web assets ship read-only alongside the code; TUNEBOX_WEB lets the APK point
# at wherever it unpacks them.
WEB = Path(os.environ.get("TUNEBOX_WEB") or (ROOT / "web"))

AUDIO = DATA / "audio"
ART = DATA / "art"
DB_PATH = DATA / "tunebox.db"

CHUNK = 64 * 1024

for _d in (DATA, AUDIO, ART):
    _d.mkdir(parents=True, exist_ok=True)

"""One-time Plex sign-in. Prints a code for https://plex.tv/link, waits, saves the token."""
import os
import time
import uuid
from pathlib import Path

import requests

CONFIG = Path.home() / ".config" / "wiim-remote"
CLIENT_ID_FILE = CONFIG / "plex-client-id"
TOKEN_FILE = CONFIG / "plex-token"


def main():
    CONFIG.mkdir(parents=True, exist_ok=True)
    if not CLIENT_ID_FILE.exists():
        CLIENT_ID_FILE.write_text(str(uuid.uuid4()))
    headers = {"Accept": "application/json", "X-Plex-Product": "WiiM Remote",
               "X-Plex-Client-Identifier": CLIENT_ID_FILE.read_text().strip()}

    pin = requests.post("https://plex.tv/api/v2/pins?strong=false", headers=headers, timeout=10).json()
    print(f"go to https://plex.tv/link and enter: {pin['code']}", flush=True)

    deadline = time.time() + pin.get("expiresIn", 900)
    while time.time() < deadline:
        token = requests.get(f"https://plex.tv/api/v2/pins/{pin['id']}", headers=headers, timeout=10).json().get("authToken")
        if token:
            old = os.umask(0o077)
            try:
                TOKEN_FILE.write_text(token)
            finally:
                os.umask(old)
            print("plex approved; token saved. Restart: systemctl --user restart wiim-remote", flush=True)
            return
        time.sleep(3)
    raise SystemExit("code expired; run again")


if __name__ == "__main__":
    main()

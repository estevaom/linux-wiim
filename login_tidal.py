"""One-time Tidal sign-in. Prints a link, waits for approval, saves the session."""
import json
import os
from pathlib import Path

import tidalapi

CONFIG = Path.home() / ".config" / "wiim-remote"
SESSION_FILE = CONFIG / "tidal-session.json"
LINK_FILE = CONFIG / "tidal-login-link"


def main():
    CONFIG.mkdir(parents=True, exist_ok=True)
    session = tidalapi.Session()
    login, future = session.login_oauth()
    link = login.verification_uri_complete
    if not link.startswith("http"):
        link = "https://" + link
    LINK_FILE.write_text(link + "\n")
    print(f"open: {link}  (expires in {login.expires_in}s)", flush=True)

    future.result()
    if not session.check_login():
        raise SystemExit("tidal login failed")

    old_umask = os.umask(0o077)
    try:
        SESSION_FILE.write_text(json.dumps({
            "token_type": session.token_type,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expiry_time": session.expiry_time.isoformat() if session.expiry_time else None,
        }))
    finally:
        os.umask(old_umask)
    LINK_FILE.unlink(missing_ok=True)
    user = session.user
    print(f"tidal approved: user id {user.id}, country {session.country_code}", flush=True)


if __name__ == "__main__":
    main()

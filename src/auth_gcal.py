"""
One-time OAuth bootstrap for Google Calendar API.

Run this on your laptop (not in CI) to obtain a refresh token, which you
paste into GitHub Secrets as GCAL_REFRESH_TOKEN.

Usage:
    python -m src.auth_gcal --client-secret /path/to/client_secret.json

It prints:
    GCAL_CLIENT_ID=...
    GCAL_CLIENT_SECRET=...
    GCAL_REFRESH_TOKEN=...

Copy each into the corresponding GitHub repo Secret.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-secret", required=True, type=Path,
                        help="Path to the OAuth client_secret.json downloaded from Google Cloud")
    args = parser.parse_args(argv)

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secret), SCOPES)
    creds = flow.run_local_server(port=0)

    cs = json.loads(args.client_secret.read_text(encoding="utf-8"))
    installed = cs.get("installed", cs.get("web", {}))
    client_id = installed.get("client_id", "")
    client_secret = installed.get("client_secret", "")

    print()
    print("Paste these into your GitHub repo Secrets:")
    print()
    print(f"GCAL_CLIENT_ID={client_id}")
    print(f"GCAL_CLIENT_SECRET={client_secret}")
    print(f"GCAL_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("Don't commit client_secret.json to git.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

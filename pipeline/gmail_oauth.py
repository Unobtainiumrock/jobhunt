#!/usr/bin/env python3
"""Gmail user OAuth using a pasted redirect URL (any browser, including Cursor).

``InstalledAppFlow.run_local_server`` is fragile when the embedded browser or
remote VM cannot reach the ephemeral localhost port. This flow prints an
authorization URL, you complete consent in a browser, then paste the full
redirect URL from the address bar (``http://127.0.0.1:...?code=...``).

Requires ``data/google_credentials.json`` from Google Cloud **OAuth 2.0
Client ID → Desktop app** (JSON must contain top-level ``installed``, not a
service account).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from pipeline.config import (
    GMAIL_SCOPES,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_GMAIL_FILE,
)


def _validate_desktop_client(path: Path) -> None:
    if not path.exists():
        print(f"Missing {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    if data.get("type") == "service_account":
        print(
            f"{path} is a service account key. Gmail user mail needs an "
            "OAuth **Desktop** client JSON (top-level key `installed`).\n"
            "Create one: Google Cloud Console → APIs & Services → Credentials → "
            "Create Credentials → OAuth client ID → Desktop app → Download JSON.",
            file=sys.stderr,
        )
        sys.exit(1)
    if "installed" not in data and "web" not in data:
        print(
            f"{path} must be a downloaded OAuth client (Desktop or Web), "
            "not raw API keys.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials",
        type=Path,
        default=GOOGLE_CREDENTIALS_FILE,
        help="Path to OAuth client JSON (default: data/google_credentials.json)",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=GOOGLE_TOKEN_GMAIL_FILE,
        help="Path to write authorized user token",
    )
    args = parser.parse_args()

    _validate_desktop_client(args.credentials)

    raw = json.loads(args.credentials.read_text())
    block = raw.get("installed") or raw.get("web") or {}
    uris: list[str] = list(block.get("redirect_uris") or [])
    if not uris:
        print(
            "OAuth client JSON has no redirect_uris. Re-download Desktop client "
            "credentials from Google Cloud Console.",
            file=sys.stderr,
        )
        return 1
    # google-auth-oauthlib leaves redirect_uri unset until run_local_server;
    # authorization_url omits it unless we assign explicitly (Google requires it).
    redirect_uri = uris[0]

    flow = InstalledAppFlow.from_client_secrets_file(
        str(args.credentials),
        GMAIL_SCOPES,
    )
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes=True,
    )
    print("Open this URL in a browser (Cursor built-in browser is fine):\n")
    print(auth_url)
    print(
        "\nAfter Google redirects, copy the **entire** address bar URL "
        "(starts with http://127.0.0.1 or http://localhost) and paste it below, "
        "then press Enter. If the page says connection refused, the URL still "
        "contains the code — copy it anyway.\n"
    )
    try:
        redirect = input("Paste redirect URL: ").strip()
    except EOFError:
        print("No input; aborting.", file=sys.stderr)
        return 1
    if not redirect:
        print("Empty URL; aborting.", file=sys.stderr)
        return 1
    try:
        flow.fetch_token(authorization_response=redirect)
    except Exception as exc:  # pragma: no cover
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        return 1

    creds = flow.credentials
    args.token.parent.mkdir(parents=True, exist_ok=True)
    args.token.write_text(creds.to_json())
    print(f"\nWrote {args.token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared Google OAuth user credential loading (Calendar, Gmail, …).

Each integration uses its own token JSON file and scope list so widening
Gmail scopes does not invalidate an existing calendar-only refresh token.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


def _validate_oauth_client_json(credentials_path: Path) -> None:
    """Reject service-account JSON; ``InstalledAppFlow`` needs user OAuth client."""
    try:
        data = json.loads(credentials_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if data.get("type") == "service_account":
        print(
            f"Error: {credentials_path} is a service account file.\n"
            "Gmail needs an OAuth 2.0 **Desktop** client JSON (Create Credentials → "
            "OAuth client ID → Desktop app → Download). Save it as "
            f"{credentials_path}",
            file=sys.stderr,
        )
        sys.exit(1)


def load_authorized_user_credentials(
    scopes: list[str],
    *,
    token_path: Path,
    credentials_path: Path,
) -> Credentials:
    """Load or refresh OAuth credentials; run browser flow if missing."""
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                print(
                    f"Error: Google OAuth client secrets not found at {credentials_path}\n"
                    "Download OAuth 2.0 Desktop credentials from Google Cloud Console\n"
                    "and save as data/google_credentials.json",
                    file=sys.stderr,
                )
                sys.exit(1)
            _validate_oauth_client_json(credentials_path)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), scopes
            )
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds

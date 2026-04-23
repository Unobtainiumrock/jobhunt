#!/usr/bin/env python3
"""Print Gmail ingest readiness and paths (no API calls unless --probe).

Usage:
  python -m pipeline.gmail_setup
  python -m pipeline.gmail_setup --probe   # light Gmail API call if token exists
"""

from __future__ import annotations

import argparse
import sys

from pipeline.config import (
    EMAIL_THREADS_FILE,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_GMAIL_FILE,
    GMAIL_INGEST_ENABLED,
    GMAIL_MAX_MESSAGES,
    GMAIL_QUERY,
    GMAIL_SELF_EMAIL,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="If token exists, call Gmail users.getProfile once.",
    )
    args = parser.parse_args()

    print("Gmail ingest configuration")
    print(f"  GMAIL_INGEST_ENABLED={GMAIL_INGEST_ENABLED}")
    print(f"  GMAIL_QUERY={GMAIL_QUERY!r}")
    print(f"  GMAIL_MAX_MESSAGES={GMAIL_MAX_MESSAGES}")
    print(f"  GMAIL_SELF_EMAIL={GMAIL_SELF_EMAIL!r}")
    print(f"  credentials: {GOOGLE_CREDENTIALS_FILE} exists={GOOGLE_CREDENTIALS_FILE.exists()}")
    print(f"  token:       {GOOGLE_TOKEN_GMAIL_FILE} exists={GOOGLE_TOKEN_GMAIL_FILE.exists()}")
    print(f"  sidecar out: {EMAIL_THREADS_FILE}")

    if not GOOGLE_CREDENTIALS_FILE.exists():
        print(
            "\nNext: create an OAuth 2.0 **Desktop** client in Google Cloud Console, "
            "download JSON, save as data/google_credentials.json "
            "(enable Gmail API; add gmail.readonly on OAuth consent screen).",
        )
        return 1

    if not GOOGLE_TOKEN_GMAIL_FILE.exists():
        print(
            "\nNext: authorize Gmail (works with Cursor’s built-in browser):\n"
            "  npm run email:oauth\n"
            "Open the printed URL in the browser, sign in, then paste the full "
            "redirect URL back into the terminal when prompted.\n"
            "Alternative:  python -m pipeline.email_ingest --force  (loopback only).\n"
            "Copy the token file onto the Hetzner app-data volume for Docker.",
        )
        return 1

    if args.probe:
        try:
            from pipeline.email_gmail import build_gmail_service

            svc = build_gmail_service()
            prof = svc.users().getProfile(userId="me").execute()
            addr = (prof.get("emailAddress") or "").strip()
            print(f"\nProbe OK: Gmail address={addr!r}")
        except Exception as exc:  # pragma: no cover
            print(f"\nProbe failed: {exc}")
            return 1

    print("\nReady: run  python -m pipeline.email_ingest  or  npm run email:ingest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

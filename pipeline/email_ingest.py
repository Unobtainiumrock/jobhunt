#!/usr/bin/env python3
"""Fetch Gmail threads, link to LinkedIn conversations, write sidecar JSON.

Usage:
  python -m pipeline.email_ingest
  python -m pipeline.email_ingest --dry-run
  python -m pipeline.email_ingest --force   # run even if GMAIL_INGEST_ENABLED=0

Requires:
  data/google_credentials.json (OAuth desktop client)
  data/google_token_gmail.json (created on first auth; separate from Calendar token)

Env (see pipeline/config.py):
  GMAIL_INGEST_ENABLED, GMAIL_QUERY, GMAIL_MAX_MESSAGES, GMAIL_SELF_EMAIL,
  GMAIL_HEADLESS_SKIP (when 1: skip if token missing — use on Docker after laptop OAuth)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pipeline.config import (
    CLASSIFIED_FILE,
    EMAIL_LINK_OVERRIDES_FILE,
    EMAIL_SYNC_STATE_FILE,
    EMAIL_THREADS_FILE,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_GMAIL_FILE,
    GMAIL_INGEST_ENABLED,
    GMAIL_MAX_MESSAGES,
    GMAIL_QUERY,
    GMAIL_SELF_EMAIL,
)
from pipeline.email_gmail import (
    build_gmail_service,
    fetch_message_full,
    list_message_refs,
    normalize_gmail_message,
)
from pipeline.email_link import (
    build_recruiter_email_to_urn_index,
    link_thread_to_urn,
    load_link_overrides,
)


def _load_classified() -> dict[str, Any]:
    if not CLASSIFIED_FILE.exists():
        print("Error: inbox_classified.json missing.", file=sys.stderr)
        sys.exit(1)
    return json.loads(CLASSIFIED_FILE.read_text())


def ingest(*, dry_run: bool, force: bool) -> int:
    if not force and not GMAIL_INGEST_ENABLED:
        print("Gmail ingest skipped (GMAIL_INGEST_ENABLED not set).")
        return 0

    if not GOOGLE_CREDENTIALS_FILE.exists():
        print(
            "Gmail ingest skipped: missing data/google_credentials.json "
            "(OAuth desktop client JSON from Google Cloud Console).",
            file=sys.stderr,
        )
        return 0

    headless_skip = os.getenv("GMAIL_HEADLESS_SKIP", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if headless_skip and not GOOGLE_TOKEN_GMAIL_FILE.exists():
        print(
            "Gmail ingest skipped: GMAIL_HEADLESS_SKIP is set and "
            "data/google_token_gmail.json is missing. Run OAuth once on a machine "
            "with a browser: python -m pipeline.email_ingest --force, then copy "
            "the token file onto this host's app-data volume.",
            file=sys.stderr,
        )
        return 0

    service = build_gmail_service()
    refs = list_message_refs(service, GMAIL_QUERY, GMAIL_MAX_MESSAGES)
    print(f"Gmail list: {len(refs)} message ref(s) for query={GMAIL_QUERY!r}")

    by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_ids: set[str] = set()
    for ref in refs:
        mid = ref["id"]
        if mid in seen_ids:
            continue
        seen_ids.add(mid)
        raw = fetch_message_full(service, mid)
        norm = normalize_gmail_message(raw)
        by_thread[norm["gmail_thread_id"]].append(norm)

    threads_out: list[dict[str, Any]] = []
    data = _load_classified()
    convos = data.get("conversations") or []
    email_index = build_recruiter_email_to_urn_index(convos)
    overrides = load_link_overrides(EMAIL_LINK_OVERRIDES_FILE)

    for tid, msgs in by_thread.items():
        msgs.sort(key=lambda m: int(m.get("internal_date_ms") or 0))
        urn, reason, conf = link_thread_to_urn(
            tid,
            msgs,
            email_index=email_index,
            overrides=overrides,
            self_email=GMAIL_SELF_EMAIL,
        )
        threads_out.append({
            "gmail_thread_id": tid,
            "conversation_urn": urn,
            "link_reason": reason,
            "link_confidence": conf,
            "messages": msgs,
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": GMAIL_QUERY,
        "thread_count": len(threads_out),
        "threads": sorted(threads_out, key=lambda t: t["gmail_thread_id"]),
    }

    sync_state = {
        "last_ingest_at": payload["generated_at"],
        "message_refs_seen": len(refs),
        "query": GMAIL_QUERY,
    }

    if dry_run:
        linked = sum(1 for t in threads_out if t.get("conversation_urn"))
        print(f"Dry-run: would write {len(threads_out)} thread(s), {linked} linked.")
        return 0

    EMAIL_THREADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMAIL_THREADS_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    EMAIL_SYNC_STATE_FILE.write_text(json.dumps(sync_state, indent=2) + "\n")
    print(f"Wrote {EMAIL_THREADS_FILE} ({len(threads_out)} threads)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when GMAIL_INGEST_ENABLED is 0 (for local testing)",
    )
    args = parser.parse_args()
    try:
        return ingest(dry_run=args.dry_run, force=args.force)
    except Exception as exc:  # pragma: no cover
        print(f"email_ingest error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Push a single Telegram DM when new drafted replies are ready for review.

Runs at the tail of `npm run pipeline` (after generate_reply). Diffs current
draftable replies against a sidecar (`data/push_notified.json`); if any URN is
new or its `generated_at` advanced since last push, fire one summary DM with
an inline "Open" button that launches the mobile WebApp at `/m/`.

Batches per-run so a single pipeline sweep sends one chat message, not N.

Usage:
  python -m pipeline.push_drafts
  python -m pipeline.push_drafts --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import CLASSIFIED_FILE, DATA_DIR

PUSH_NOTIFIED_FILE = DATA_DIR / "push_notified.json"

DRAFTABLE_STATUSES = {"draft", "auto_send"}


def _load_sidecar() -> dict[str, str]:
    if not PUSH_NOTIFIED_FILE.exists():
        return {}
    try:
        data = json.loads(PUSH_NOTIFIED_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_sidecar(notified: dict[str, str]) -> None:
    PUSH_NOTIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUSH_NOTIFIED_FILE.write_text(json.dumps(notified, indent=2, sort_keys=True) + "\n")


def _recruiter_drafts(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for convo in conversations:
        if convo.get("classification", {}).get("category") != "recruiter":
            continue
        reply = convo.get("reply") or {}
        if reply.get("status") not in DRAFTABLE_STATUSES:
            continue
        if not reply.get("generated_at"):
            continue
        out.append(convo)
    return out


def _other_name(convo: dict[str, Any], self_name: str) -> str:
    for p in convo.get("participants") or []:
        name = p.get("name") or ""
        if name and name != self_name:
            return name
    return "?"


def _diff_new(
    drafts: list[dict[str, Any]], notified: dict[str, str]
) -> list[dict[str, Any]]:
    fresh = []
    for convo in drafts:
        urn = str(convo.get("conversationUrn") or "")
        if not urn:
            continue
        generated_at = str((convo.get("reply") or {}).get("generated_at") or "")
        if notified.get(urn) == generated_at:
            continue
        fresh.append(convo)
    return fresh


def _compose_message(fresh: list[dict[str, Any]], self_name: str) -> str:
    n = len(fresh)
    if n == 1:
        who = _other_name(fresh[0], self_name)
        return (
            f"🆕 New draft ready: <b>{who}</b>\n"
            f"Tap Open to review in the mobile UI."
        )
    head = f"🆕 <b>{n} new drafts ready</b>\n"
    names = [_other_name(c, self_name) for c in fresh[:5]]
    body = "• " + "\n• ".join(names)
    more = f"\n… and {n - 5} more" if n > 5 else ""
    return head + body + more + "\nTap Open to review."


def _build_webapp_url() -> str | None:
    base = os.getenv("PUBLIC_HOST", "").strip()
    if not base:
        return None
    # Allow either "m.host" or "host" — if caller already includes subdomain,
    # use it verbatim; else prepend m. per Caddy convention.
    if base.startswith("m."):
        host = base
    else:
        host = f"m.{base}"
    return f"https://{host}/m/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log candidate drafts without sending or updating the sidecar.",
    )
    args = parser.parse_args()

    if not CLASSIFIED_FILE.exists():
        print("No classified file yet; nothing to push.")
        return 0

    data = json.loads(CLASSIFIED_FILE.read_text())
    conversations = data.get("conversations") or []

    from pipeline.config import USER_NAME  # local import — avoids cold-path cost
    drafts = _recruiter_drafts(conversations)
    notified = _load_sidecar()
    fresh = _diff_new(drafts, notified)

    if not fresh:
        print(f"No new drafts to push (sidecar covers {len(drafts)} draft(s)).")
        return 0

    print(f"{len(fresh)} new draft(s) to push:")
    for c in fresh:
        urn = c.get("conversationUrn", "")
        print(f"  {_other_name(c, USER_NAME):30} {urn[-24:]}")

    if args.dry_run:
        print("(dry-run — not sending, not updating sidecar)")
        return 0

    from infra.notify import send_telegram_with_webapp_button

    webapp_url = _build_webapp_url()
    if not webapp_url:
        print(
            "PUBLIC_HOST not set; cannot build WebApp URL. "
            "Skipping push (set PUBLIC_HOST=<host> in .env).",
            file=sys.stderr,
        )
        return 0

    text = _compose_message(fresh, USER_NAME)
    sent = send_telegram_with_webapp_button(text, "Open", webapp_url)
    if not sent:
        print(
            "Telegram send failed (missing HEALTH_TELEGRAM_* or transport error). "
            "Sidecar not updated so the next run will retry.",
            file=sys.stderr,
        )
        return 1

    now_iso = datetime.now(timezone.utc).isoformat()
    for c in fresh:
        urn = str(c.get("conversationUrn") or "")
        if not urn:
            continue
        generated_at = str((c.get("reply") or {}).get("generated_at") or now_iso)
        notified[urn] = generated_at
    _save_sidecar(notified)
    print(f"Pushed notification for {len(fresh)} draft(s); sidecar updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

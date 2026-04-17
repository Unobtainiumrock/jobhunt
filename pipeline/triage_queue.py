#!/usr/bin/env python3
"""
Unified triage queue for LinkedIn outreach.

Rolls up the two independent draft surfaces into a single prioritized queue
that answers "who should I reply to right now?" without opening the review UI:

1. recruiter reply drafts from ``data/inbox_classified.json`` whose
   ``reply.status`` is ``draft``, ``auto_send``, or ``approved`` and which
   have not yet been dispatched.
2. follow-up drafts from ``data/entities/followups.json`` whose status is
   ``draft`` or ``approved``.

Items are sorted by score descending, then by most recent activity.

Usage:
  python -m pipeline.triage_queue             # human-readable summary
  python -m pipeline.triage_queue --json      # machine-readable JSON
  python -m pipeline.triage_queue --limit 20  # cap number shown
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pipeline.config import (
    CLASSIFIED_FILE,
    CONVERSATIONS_DIR,
    FOLLOWUP_QUEUE_FILE,
    OPPORTUNITIES_DIR,
    USER_NAME,
)

TriageKind = Literal["reply", "followup"]


_ACTIONABLE_REPLY_STATUSES: set[str] = {"draft", "auto_send", "approved"}
_ACTIONABLE_FOLLOWUP_STATUSES: set[str] = {"draft", "approved"}


@dataclass
class TriageItem:
    kind: TriageKind
    status: str
    score: int
    last_activity_at: str
    company: str
    role_title: str
    recipient: str
    subject: str
    thread_id: str
    conversation_urn: str
    preview: str
    reason: str
    task_id: str | None = None
    followup_number: int | None = None
    referenced_quote: str | None = None
    safety_passed: bool | None = None
    manually_edited: bool | None = None
    tier: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path) as handle:
        return json.load(handle)


def _load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def _other_participant(convo: dict[str, Any]) -> str:
    for participant in convo.get("participants", []):
        if participant.get("name") != USER_NAME:
            return participant.get("name", "Unknown")
    return "Unknown"


def _reply_items(classified: dict[str, Any]) -> list[TriageItem]:
    items: list[TriageItem] = []
    for convo in classified.get("conversations", []):
        reply = convo.get("reply") or {}
        status = reply.get("status", "")
        if status not in _ACTIONABLE_REPLY_STATUSES:
            continue
        classification = convo.get("classification") or {}
        if classification.get("category") != "recruiter":
            continue

        metadata = convo.get("metadata") or {}
        score = (convo.get("score") or {}).get("total", 0) or 0
        text = reply.get("text", "")
        preview = text.replace("\n", " ")[:140]

        subject = ""
        for message in reversed(convo.get("messages") or []):
            subject = message.get("subject") or subject
            if subject:
                break

        items.append(TriageItem(
            kind="reply",
            status=status,
            score=int(score),
            last_activity_at=convo.get("lastActivityAt", "") or "",
            company=metadata.get("company", "Unknown"),
            role_title=metadata.get("role_title", ""),
            recipient=_other_participant(convo),
            subject=subject,
            thread_id=convo.get("conversationUrn", ""),
            conversation_urn=convo.get("conversationUrn", ""),
            preview=preview,
            reason=_reason_for_reply(status, reply),
            safety_passed=reply.get("safety_passed"),
            manually_edited=reply.get("manually_edited"),
            tier=reply.get("tier"),
            extra={
                "next_action_needed": metadata.get("next_action_needed", ""),
                "urgency": metadata.get("urgency", ""),
            },
        ))
    return items


def _reason_for_reply(status: str, reply: dict[str, Any]) -> str:
    if status == "approved":
        return "approved draft ready for send"
    if status == "auto_send":
        return "auto-send tier (score >= threshold), awaiting confirmation"
    if not reply.get("safety_passed", True):
        return "draft flagged by safety, needs manual review"
    return "draft awaiting review"


def _followup_items() -> list[TriageItem]:
    queue = _load_json(FOLLOWUP_QUEUE_FILE)
    followups = queue.get("followups", [])
    if not followups:
        return []

    conversations = {record["id"]: record for record in _load_records(CONVERSATIONS_DIR)}
    opportunities = {record["id"]: record for record in _load_records(OPPORTUNITIES_DIR)}

    items: list[TriageItem] = []
    for entry in followups:
        status = entry.get("status", "")
        if status not in _ACTIONABLE_FOLLOWUP_STATUSES:
            continue
        conversation = conversations.get(entry.get("conversation_id", ""), {})
        opportunity = opportunities.get(entry.get("opportunity_id", ""), {})
        participants = conversation.get("participants", []) or []
        recipient = next((p.get("name", "") for p in participants if p.get("name") != USER_NAME), "")

        items.append(TriageItem(
            kind="followup",
            status=status,
            score=int(opportunity.get("score", {}).get("total", 0) or 0),
            last_activity_at=conversation.get("last_activity_at", "") or "",
            company=opportunity.get("company", "Unknown"),
            role_title=opportunity.get("role_title", ""),
            recipient=recipient or "Unknown",
            subject=conversation.get("subject", "") or "",
            thread_id=entry.get("thread_id", ""),
            conversation_urn=entry.get("thread_id", ""),
            preview=(entry.get("message", "") or "").replace("\n", " ")[:140],
            reason=f"follow-up #{entry.get('followup_number', '?')} {status}",
            task_id=entry.get("task_id"),
            followup_number=entry.get("followup_number"),
            referenced_quote=entry.get("referenced_quote"),
        ))
    return items


def _sort_key(item: TriageItem) -> tuple[int, str]:
    return (-item.score, item.last_activity_at or "")


def build_triage_queue() -> list[TriageItem]:
    classified = _load_json(CLASSIFIED_FILE)
    items = _reply_items(classified) + _followup_items()
    items.sort(key=_sort_key)
    return items


def _short_urn(urn: str) -> str:
    if not urn:
        return ""
    tail = urn.rsplit(",", 1)[-1]
    return tail.rstrip(")").strip("=")[:14] or urn[-14:]


def _format_row(index: int, item: TriageItem) -> str:
    label = "REPLY" if item.kind == "reply" else f"FOLLOWUP#{item.followup_number or '?'}"
    score = f"{item.score:>3}" if item.score else "  -"
    company = (item.company or "")[:28]
    role = (item.role_title or "")[:28]
    recipient = (item.recipient or "")[:24]
    preview = item.preview[:80]
    return (
        f"  {index:>2}. [{label:<10}] score={score} "
        f"{recipient:<24} | {company:<28} | {role:<28}\n"
        f"      status={item.status:<9} reason={item.reason}\n"
        f"      preview: {preview}\n"
        f"      thread={_short_urn(item.thread_id)}"
    )


def print_triage(items: list[TriageItem], limit: int | None = None) -> None:
    total = len(items)
    if total == 0:
        print("Triage queue is empty. Nothing to reply to.")
        return

    shown = items[:limit] if limit else items
    reply_count = sum(1 for i in items if i.kind == "reply")
    followup_count = total - reply_count

    print(f"LinkedIn triage queue — {total} item(s) "
          f"({reply_count} replies, {followup_count} follow-ups) "
          f"as of {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("-" * 90)
    for index, item in enumerate(shown, start=1):
        print(_format_row(index, item))
    if limit and total > limit:
        print(f"\n  ... {total - limit} more item(s) not shown (--limit={limit})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified triage queue for LinkedIn outreach")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--limit", type=int, default=None, help="Cap items displayed")
    args = parser.parse_args()

    items = build_triage_queue()
    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(items),
            "items": [item.to_dict() for item in items],
        }
        print(json.dumps(payload, indent=2))
        return
    print_triage(items, limit=args.limit)


if __name__ == "__main__":
    main()

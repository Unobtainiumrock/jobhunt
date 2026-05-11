#!/usr/bin/env python3
"""
Phase 3B: Calendar Booking Agent

Detects calendar links in conversations, checks availability against
Google Calendar, and books appointments via CDP browser automation.

Architecture:
  1. Detect calendar links (Calendly, Google Calendar, Cal.com)
  2. Check user's Google Calendar for conflicts
  3. Spawn CDP browser session to navigate and book the slot
  4. Create event in Google Calendar
  5. Notify user of the booking

Usage:
  python -m agents.calendar_agent
  python -m agents.calendar_agent --urn "urn:li:msg_conversation:..."
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from infra.google_oauth import load_authorized_user_credentials
from pipeline.config import CLASSIFIED_FILE, DATA_DIR, USER_NAME
from pipeline.extract_contacts import CALENDAR_DOMAINS

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = DATA_DIR / "google_token.json"
CREDENTIALS_FILE = DATA_DIR / "google_credentials.json"
BOOKINGS_FILE = DATA_DIR / "bookings.json"
LEAD_STATE_FILE = DATA_DIR / "lead_states.json"

# Lead-state values that release a slot for reuse. Anything else (e.g.
# awaiting_response, awaiting_their_feedback) keeps the slot reserved.
_RELEASED_LEAD_STATUSES = {"declined", "dead_end", "closed", "scheduled", "booked"}


def _canonical_slot_key(start_iso: str) -> str | None:
    """Normalise an ISO start time to UTC for set membership comparisons."""
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def collect_reserved_slot_starts(current_urn: str | None = None) -> set[str]:
    """Return canonical UTC ISO start times already proposed to OTHER
    active leads. Used by ``propose_slots`` to avoid suggesting the same
    time window to multiple recruiters concurrently.

    A slot is reserved if:
      - it is in the future (past slots are free)
      - the conversation it lives on is not the caller (``current_urn``)
      - the conversation's lead state is not in
        ``_RELEASED_LEAD_STATUSES`` (declined / dead_end / scheduled / etc.)
    """
    reserved: set[str] = set()
    try:
        with open(CLASSIFIED_FILE) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return reserved

    lead_states: dict[str, Any] = {}
    if LEAD_STATE_FILE.exists():
        try:
            with open(LEAD_STATE_FILE) as fh:
                lead_states = json.load(fh) or {}
        except json.JSONDecodeError:
            lead_states = {}

    now = datetime.now(timezone.utc)
    for convo in data.get("conversations", []) or []:
        urn = convo.get("conversationUrn")
        if not urn or urn == current_urn:
            continue
        status = (lead_states.get(urn) or {}).get("status") or ""
        if status in _RELEASED_LEAD_STATUSES:
            continue
        reply = convo.get("reply") or {}
        for slot in reply.get("proposed_slots") or []:
            start_iso = slot.get("start") if isinstance(slot, dict) else None
            key = _canonical_slot_key(start_iso) if start_iso else None
            if not key:
                continue
            try:
                slot_dt = datetime.fromisoformat(key)
            except ValueError:
                continue
            if slot_dt <= now:
                continue
            reserved.add(key)
    return reserved


def get_calendar_service() -> Any:
    """Authenticate and return a Google Calendar API service."""
    creds = load_authorized_user_credentials(
        SCOPES,
        token_path=TOKEN_FILE,
        credentials_path=CREDENTIALS_FILE,
    )
    return build("calendar", "v3", credentials=creds)


def get_busy_times(
    service: Any,
    start: datetime,
    end: datetime,
) -> list[dict[str, str]]:
    """Query Google Calendar for busy periods."""
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": "primary"}],
    }
    result = service.freebusy().query(body=body).execute()
    return result.get("calendars", {}).get("primary", {}).get("busy", [])


def create_calendar_event(
    service: Any,
    summary: str,
    description: str,
    start_time: datetime,
    duration_minutes: int = 30,
    attendee_email: str | None = None,
) -> dict[str, Any]:
    """Create an event on the user's Google Calendar."""
    end_time = start_time + timedelta(minutes=duration_minutes)
    event: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_time.isoformat(),
            "timeZone": "America/Los_Angeles",
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": "America/Los_Angeles",
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 15},
                {"method": "popup", "minutes": 60},
            ],
        },
    }
    if attendee_email:
        event["attendees"] = [{"email": attendee_email}]

    return service.events().insert(calendarId="primary", body=event).execute()


DEFAULT_WORKDAY_START_HOUR = 10  # 10:00 local
DEFAULT_WORKDAY_END_HOUR = 17    # 17:00 local
DEFAULT_LOCAL_TIMEZONE = "America/Los_Angeles"


def _iter_candidate_slots(
    start_after: datetime,
    duration_minutes: int,
    window_days: int,
    slots_per_day: int,
) -> list[datetime]:
    """Generate candidate start times across the next `window_days` business days."""
    candidates: list[datetime] = []
    day = start_after
    days_scanned = 0
    while days_scanned < window_days * 2 and len(candidates) < slots_per_day * window_days:
        weekday = day.weekday()  # Mon=0..Sun=6
        if weekday < 5:  # skip weekends
            base = day.replace(
                hour=DEFAULT_WORKDAY_START_HOUR,
                minute=0,
                second=0,
                microsecond=0,
            )
            step = max(duration_minutes, 60)
            day_end_hour = DEFAULT_WORKDAY_END_HOUR
            emitted_today = 0
            t = base
            while t.hour < day_end_hour and emitted_today < slots_per_day:
                if t > start_after:
                    candidates.append(t)
                    emitted_today += 1
                t += timedelta(minutes=step)
        day += timedelta(days=1)
        days_scanned += 1
    return candidates


def _format_slot(slot: datetime) -> str:
    """Format a slot in a human-friendly way for LinkedIn DMs."""
    # Format: "Mon 4/21 at 10:00 AM PT"
    return slot.strftime("%a %-m/%-d at %-I:%M %p PT")


def propose_slots(
    duration_minutes: int = 30,
    window_days: int = 5,
    slots_per_day: int = 4,
    start_after: datetime | None = None,
    max_slots: int = 3,
    excluded_starts: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return up to `max_slots` candidate meeting times.

    Shape:
        [{"start": <iso>, "end": <iso>, "label": "Mon 4/21 at 10:00 AM PT"}, ...]

    Tries Google Calendar first (if credentials are configured); skips any slot
    overlapping a busy block. Falls back to a pure heuristic (next business-day
    10am/2pm windows) when Google Calendar is unavailable so callers never fail.

    ``excluded_starts`` is a set of canonical UTC ISO start times that should
    be skipped (slots already proposed to other recipients in this run or in
    other still-open leads on disk). Use ``collect_reserved_slot_starts`` to
    seed it with the disk-side reservations before the run.
    """
    start_after = start_after or datetime.now(timezone.utc) + timedelta(hours=4)
    candidates = _iter_candidate_slots(
        start_after=start_after,
        duration_minutes=duration_minutes,
        window_days=window_days,
        slots_per_day=slots_per_day,
    )

    busy_blocks: list[tuple[datetime, datetime]] = []
    used_google = False
    if TOKEN_FILE.exists() and CREDENTIALS_FILE.exists():
        try:
            service = get_calendar_service()
            horizon_end = start_after + timedelta(days=window_days + 1)
            raw_busy = get_busy_times(service, start_after, horizon_end)
            for block in raw_busy:
                try:
                    busy_blocks.append((
                        datetime.fromisoformat(block["start"].replace("Z", "+00:00")),
                        datetime.fromisoformat(block["end"].replace("Z", "+00:00")),
                    ))
                except (KeyError, ValueError):
                    continue
            used_google = True
        except Exception:
            # Any error (auth, network, offline) -> fall back to heuristic slots.
            busy_blocks = []

    def _conflicts(slot_start: datetime) -> bool:
        slot_end = slot_start + timedelta(minutes=duration_minutes)
        for block_start, block_end in busy_blocks:
            if slot_start < block_end and slot_end > block_start:
                return True
        return False

    excluded = set(excluded_starts or [])

    proposals: list[dict[str, Any]] = []
    for slot in candidates:
        if used_google and _conflicts(slot):
            continue
        slot_key = _canonical_slot_key(slot.isoformat())
        if slot_key and slot_key in excluded:
            continue
        end = slot + timedelta(minutes=duration_minutes)
        proposals.append({
            "start": slot.isoformat(),
            "end": end.isoformat(),
            "label": _format_slot(slot),
            "source": "google_calendar" if used_google else "heuristic",
        })
        if len(proposals) >= max_slots:
            break

    return proposals


def find_calendar_links(convo: dict[str, Any]) -> list[str]:
    """Extract calendar scheduling links from a conversation."""
    links: list[str] = []
    seen: set[str] = set()
    for msg in convo.get("messages", []):
        text = msg.get("text", "")
        for word in text.split():
            cleaned = word.strip(".,;:()[]\"'<>")
            if any(domain in cleaned for domain in CALENDAR_DOMAINS):
                if cleaned.startswith("http") and cleaned not in seen:
                    links.append(cleaned)
                    seen.add(cleaned)
    return links


def build_event_description(convo: dict[str, Any]) -> str:
    """Build a rich event description from conversation context."""
    meta = convo.get("metadata", {})
    other = next(
        (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
        {"name": "Unknown", "headline": ""},
    )
    lines: list[str] = [
        f"Recruiter: {other.get('name', 'Unknown')}",
        f"Headline: {other.get('headline', '')}",
    ]
    if meta.get("role_title"):
        lines.append(f"Role: {meta['role_title']}")
    if meta.get("company"):
        lines.append(f"Company: {meta['company']}")
    if meta.get("role_description_summary"):
        lines.append(f"Description: {meta['role_description_summary']}")

    score = convo.get("score", {})
    if score.get("total"):
        lines.append(f"Match Score: {score['total']}/100")

    lines.append(f"\nConversation URN: {convo.get('conversationUrn', '')}")
    return "\n".join(lines)


def save_booking(booking: dict[str, Any]) -> None:
    """Persist booking to bookings file."""
    bookings: list[dict[str, Any]] = []
    if BOOKINGS_FILE.exists():
        with open(BOOKINGS_FILE) as f:
            bookings = json.load(f)

    bookings.append(booking)
    with open(BOOKINGS_FILE, "w") as f:
        json.dump(bookings, f, indent=2)


def process_conversation(convo: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single conversation for calendar booking.

    Returns booking info if a calendar link was found and event created,
    None otherwise. Actual slot selection on Calendly/Google Calendar
    links requires CDP browser automation (out-of-scope for this module;
    see src/linkedin-listener.mjs for the CDP integration point).
    """
    calendar_links = find_calendar_links(convo)
    if not calendar_links:
        return None

    other = next(
        (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
        {"name": "Unknown"},
    )
    meta = convo.get("metadata", {})

    booking = {
        "conversation_urn": convo.get("conversationUrn", ""),
        "recruiter_name": other.get("name", "Unknown"),
        "role_title": meta.get("role_title", ""),
        "company": meta.get("company", ""),
        "calendar_links": calendar_links,
        "status": "pending_browser_booking",
        "event_description": build_event_description(convo),
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }

    save_booking(booking)
    return booking


def scan_all(target_urn: str | None = None) -> None:
    """Scan all conversations for calendar booking opportunities."""
    if not CLASSIFIED_FILE.exists():
        print("Error: Run classify_leads first.", file=sys.stderr)
        sys.exit(1)

    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)

    conversations = data.get("conversations", [])
    if target_urn:
        conversations = [c for c in conversations if c.get("conversationUrn") == target_urn]

    found = 0
    for convo in conversations:
        result = process_conversation(convo)
        if result:
            found += 1
            print(f"  Calendar link detected: {result['recruiter_name']} "
                  f"({result['company'] or 'Unknown company'})")
            for link in result["calendar_links"]:
                print(f"    -> {link}")

    print(f"\nFound {found} conversations with calendar links")
    if found > 0:
        print(f"Bookings saved to {BOOKINGS_FILE}")


def main() -> None:
    target = None
    if "--urn" in sys.argv:
        idx = sys.argv.index("--urn")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]
    scan_all(target_urn=target)


if __name__ == "__main__":
    main()

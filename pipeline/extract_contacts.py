#!/usr/bin/env python3
"""
Phase 1B: Contact Information Extraction

Parses phone numbers, email addresses, and calendar links from conversation
messages. Normalizes phone numbers to E.164 format.

Usage:
  python -m pipeline.extract_contacts
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

import phonenumbers

from pipeline.config import CLASSIFIED_FILE, INBOX_FILE, USER_NAME

PHONE_PATTERN = re.compile(
    r"""
    (?:(?:\+?1[\s.-]?)?                     # optional country code
    (?:\(?\d{3}\)?[\s.-]?)                   # area code
    \d{3}[\s.-]?\d{4})                       # subscriber number
    |(?:C:\s*\+?\d[\d\s.-]{8,15})            # "C: +1 518 289-0457" format
    """,
    re.VERBOSE,
)

EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
)

CALENDAR_PATTERN = re.compile(
    r"https?://(?:calendar\.app\.google|calendly)\.com/[^\s)\"'>]+"
)

URL_PATTERN = re.compile(
    r"https?://[^\s)\"'>]+"
)

CALENDAR_DOMAINS = {"calendly.com", "calendar.app.google", "cal.com", "savvycal.com"}


@dataclass
class ExtractedContacts:
    name: str
    headline: str
    profile_urn: str
    phones: list[str] = field(default_factory=list)
    phones_e164: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    calendar_links: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)


def normalize_phone(raw: str, default_region: str = "US") -> str | None:
    """Attempt to parse and normalize a phone string to E.164."""
    cleaned = re.sub(r"^C:\s*", "", raw).strip()
    try:
        parsed = phonenumbers.parse(cleaned, default_region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except phonenumbers.NumberParseException:
        pass
    return None


def _is_calendar_link(url: str) -> bool:
    """Check if a URL is a calendar scheduling link."""
    for domain in CALENDAR_DOMAINS:
        if domain in url:
            return True
    return False


def extract_from_conversation(convo: dict[str, Any]) -> ExtractedContacts:
    """Extract all contact info from a single conversation."""
    other = next(
        (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
        {"name": "Unknown", "headline": "", "profileUrn": ""},
    )

    contacts = ExtractedContacts(
        name=other.get("name", "Unknown"),
        headline=other.get("headline", ""),
        profile_urn=other.get("profileUrn", ""),
    )

    seen_phones: set[str] = set()
    seen_emails: set[str] = set()
    seen_calendars: set[str] = set()
    seen_urls: set[str] = set()

    for msg in convo.get("messages", []):
        sender = msg.get("sender", "")
        text = msg.get("text", "")
        if not text:
            continue

        for match in PHONE_PATTERN.finditer(text):
            raw = match.group().strip()
            e164 = normalize_phone(raw)
            if e164 and e164 not in seen_phones:
                # Only track the other person's phone, not our own
                is_other = sender != USER_NAME
                if is_other or e164 not in seen_phones:
                    contacts.phones.append(raw)
                    contacts.phones_e164.append(e164)
                    seen_phones.add(e164)

        for match in EMAIL_PATTERN.finditer(text):
            email = match.group().lower()
            if email not in seen_emails:
                contacts.emails.append(email)
                seen_emails.add(email)

        for match in URL_PATTERN.finditer(text):
            url = match.group().rstrip(".,;:")
            if _is_calendar_link(url) and url not in seen_calendars:
                contacts.calendar_links.append(url)
                seen_calendars.add(url)
            elif url not in seen_urls and not _is_calendar_link(url):
                contacts.websites.append(url)
                seen_urls.add(url)

    return contacts


def extract_all() -> list[dict[str, Any]]:
    """Extract contacts from all conversations."""
    source = CLASSIFIED_FILE if CLASSIFIED_FILE.exists() else INBOX_FILE
    if not source.exists():
        print(f"Error: No data file found. Run the scraper first.", file=sys.stderr)
        sys.exit(1)

    with open(source) as f:
        data = json.load(f)

    results: list[dict[str, Any]] = []
    for convo in data.get("conversations", []):
        contacts = extract_from_conversation(convo)
        entry = asdict(contacts)
        entry["conversation_urn"] = convo.get("conversationUrn", "")
        entry["classification"] = convo.get("classification", {}).get("category", "unclassified")
        entry["last_activity"] = convo.get("lastActivityAt", "")
        results.append(entry)

    has_contact = sum(
        1 for r in results
        if r["phones"] or r["emails"] or r["calendar_links"]
    )
    print(f"Extracted contacts from {len(results)} conversations")
    print(f"  {has_contact} have at least one contact method")
    print(f"  {sum(1 for r in results if r['phones'])} with phone numbers")
    print(f"  {sum(1 for r in results if r['emails'])} with emails")
    print(f"  {sum(1 for r in results if r['calendar_links'])} with calendar links")

    return results


def main() -> None:
    extract_all()


if __name__ == "__main__":
    main()

"""Link Gmail threads to LinkedIn conversationUrns (pure helpers)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pipeline.extract_contacts import EMAIL_PATTERN, extract_from_conversation

FROM_EMAIL_RE = re.compile(
    r"<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>",
)


def parse_address_list(raw: str) -> list[str]:
    """Extract lowercase email addresses from a From/To/Cc header."""
    if not raw:
        return []
    found = {m.group().lower() for m in EMAIL_PATTERN.finditer(raw)}
    for m in FROM_EMAIL_RE.finditer(raw):
        found.add(m.group(1).lower())
    return sorted(found)


def build_recruiter_email_to_urn_index(
    conversations: list[dict[str, Any]],
) -> dict[str, str]:
    """Map normalized recruiter email -> conversationUrn (first wins)."""
    index: dict[str, str] = {}
    for convo in conversations:
        urn = convo.get("conversationUrn") or ""
        if not urn:
            continue
        if convo.get("classification", {}).get("category") != "recruiter":
            continue
        contacts = extract_from_conversation(convo)
        for em in contacts.emails:
            index.setdefault(em.lower(), urn)
    return index


def load_link_overrides(path: Path) -> dict[str, str]:
    """gmail_thread_id -> conversationUrn."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if k and v}
    return {}


def collect_thread_emails(
    messages: list[dict[str, Any]],
    *,
    self_email: str,
) -> set[str]:
    """All participant emails in thread headers, excluding self."""
    self_l = self_email.lower().strip()
    emails: set[str] = set()
    for m in messages:
        for raw in (m.get("from"), m.get("to")):
            for addr in parse_address_list(raw or ""):
                if self_l and addr == self_l:
                    continue
                emails.add(addr)
    return emails


def link_thread_to_urn(
    gmail_thread_id: str,
    messages: list[dict[str, Any]],
    *,
    email_index: dict[str, str],
    overrides: dict[str, str],
    self_email: str,
) -> tuple[str | None, str, float]:
    """Return (conversation_urn_or_none, reason, confidence 0..1)."""
    if gmail_thread_id in overrides:
        return overrides[gmail_thread_id], "manual_override", 1.0
    thread_emails = collect_thread_emails(messages, self_email=self_email)
    for em in thread_emails:
        urn = email_index.get(em)
        if urn:
            return urn, f"email_match:{em}", 0.9
    return None, "no_match", 0.0

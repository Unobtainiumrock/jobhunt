"""Compact email-derived context for LLM prompts (intent + reply).

Each LinkedIn ``conversationUrn`` maps to at most one merged Gmail thread in
``email_threads.json`` — that is the per-lead email session (no cross-lead mixing).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import (
    EMAIL_CONTEXT_LAST_N,
    EMAIL_CONTEXT_MAX_CHARS,
    EMAIL_THREADS_FILE,
)


def load_email_threads_doc(path: Path | None = None) -> dict[str, Any]:
    p = path or EMAIL_THREADS_FILE
    if not p.exists():
        return {"threads": []}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"threads": []}


def _threads_by_urn(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in doc.get("threads") or []:
        urn = (t.get("conversation_urn") or "").strip()
        if urn:
            out[urn] = t
    return out


def email_thread_last_activity_ms(
    conversation_urn: str,
    *,
    threads_path: Path | None = None,
) -> int | None:
    """Latest ``internal_date_ms`` in the linked Gmail thread, or None."""
    doc = load_email_threads_doc(threads_path)
    thread = _threads_by_urn(doc).get(conversation_urn)
    if not thread:
        return None
    msgs = thread.get("messages") or []
    if not msgs:
        return None
    times = [int(m.get("internal_date_ms") or 0) for m in msgs]
    best = max(times)
    return best if best > 0 else None


def _fmt_email_ts(ms: int) -> str:
    if ms <= 0:
        return "?"
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d UTC")


def email_sidebar_for_urn(
    conversation_urn: str,
    *,
    max_chars: int | None = None,
    threads_path: Path | None = None,
) -> str:
    """Return a short plain-text block for prompts, or empty string."""
    cap = max_chars if max_chars is not None else EMAIL_CONTEXT_MAX_CHARS
    last_n = max(1, EMAIL_CONTEXT_LAST_N)
    doc = load_email_threads_doc(threads_path)
    thread = _threads_by_urn(doc).get(conversation_urn)
    if not thread:
        return ""
    lines: list[str] = [
        "===LINKED EMAIL (same lead only; Gmail lookback is wider than LI scrape)===",
        "Use for: rejections, interview stages, offers, scheduling — do not contradict.",
        "If email clearly rejects or closes the process, treat that as authoritative",
        "even when LinkedIn still looks open.",
    ]
    msgs = sorted(
        thread.get("messages") or [],
        key=lambda m: int(m.get("internal_date_ms") or 0),
    )
    for m in msgs[-last_n:]:
        subj = (m.get("subject") or "").strip()
        sender = (m.get("from") or "").strip()
        ms = int(m.get("internal_date_ms") or 0)
        when = _fmt_email_ts(ms)
        snip = (m.get("snippet") or m.get("body_preview") or "")[:400]
        lines.append(f"[email {when}] {sender}")
        if subj:
            lines.append(f"  Subject: {subj}")
        if snip:
            lines.append(f"  {snip}")
    lines.append("===END EMAIL===")
    text = "\n".join(lines).strip()
    if len(text) > cap:
        return text[: cap - 3].rstrip() + "..."
    return text


def email_blob_for_intent_hash(conversation_urn: str) -> str:
    """Stable blob so intent cache invalidates when email sidecar changes."""
    doc = load_email_threads_doc()
    thread = _threads_by_urn(doc).get(conversation_urn)
    if not thread:
        return ""
    return json.dumps(
        {
            "thread_id": thread.get("gmail_thread_id"),
            "updated_at": doc.get("generated_at"),
            "n": len(thread.get("messages") or []),
        },
        sort_keys=True,
    )

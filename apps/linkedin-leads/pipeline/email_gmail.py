"""Thin Gmail API wrapper — fetch + normalize; no LinkedIn logic."""

from __future__ import annotations

import base64
import re
from typing import Any

from googleapiclient.discovery import build

from infra.google_oauth import load_authorized_user_credentials
from pipeline.config import (
    GMAIL_SCOPES,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_GMAIL_FILE,
)


def build_gmail_service() -> Any:
    creds = load_authorized_user_credentials(
        GMAIL_SCOPES,
        token_path=GOOGLE_TOKEN_GMAIL_FILE,
        credentials_path=GOOGLE_CREDENTIALS_FILE,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = (h.get("name") or "").strip()
        if not name:
            continue
        out[name.lower()] = (h.get("value") or "").strip()
    return out


def _decode_b64url(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode((data + pad).encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def _extract_plain_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain from MIME tree."""
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    if body.get("data") and mime.startswith("text/plain"):
        return _decode_b64url(body["data"])
    for part in payload.get("parts") or []:
        got = _extract_plain_body(part)
        if got.strip():
            return got
    if body.get("data") and mime.startswith("text/html"):
        # strip tags crudely if only HTML
        html = _decode_b64url(body["data"])
        return re.sub(r"<[^>]+>", " ", html)
    for part in payload.get("parts") or []:
        ptype = (part.get("mimeType") or "").lower()
        if ptype.startswith("multipart/"):
            got = _extract_plain_body(part)
            if got.strip():
                return got
    return ""


def normalize_gmail_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Flatten Gmail API users.messages.get response."""
    mid = msg.get("id", "")
    tid = msg.get("threadId", "")
    internal = int(msg.get("internalDate", "0") or 0)
    payload = msg.get("payload") or {}
    headers = _header_map(payload)
    body = _extract_plain_body(payload).strip()
    body = re.sub(r"\s+", " ", body)
    return {
        "gmail_message_id": mid,
        "gmail_thread_id": tid,
        "internal_date_ms": internal,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "snippet": (msg.get("snippet") or "").strip(),
        "body_preview": body[:4000],
    }


def list_message_refs(service: Any, query: str, max_results: int) -> list[dict[str, str]]:
    """Return {id, threadId} for messages matching query."""
    out: list[dict[str, str]] = []
    page_token: str | None = None
    while len(out) < max_results:
        remaining = max_results - len(out)
        batch = min(100, remaining)
        req = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=batch, pageToken=page_token)
        )
        resp = req.execute()
        for m in resp.get("messages") or []:
            mid = m.get("id")
            tid = m.get("threadId")
            if mid:
                out.append({"id": mid, "threadId": tid or ""})
            if len(out) >= max_results:
                break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def fetch_message_full(service: Any, message_id: str) -> dict[str, Any]:
    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

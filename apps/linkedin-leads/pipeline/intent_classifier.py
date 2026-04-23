#!/usr/bin/env python3
"""Terminal-state intent classifier for recruiter conversations.

Tags a thread's most recent turn so downstream draft generation can decide
whether to abstain, send a gentle ping, or branch to scheduling. One cheap LLM
call per thread, cached by a hash of the tail messages + user-name so we only
re-run when the conversation actually changes.

Tag taxonomy:
    awaiting_their_move      - user asked a question / shared a doc; their turn
    awaiting_their_feedback  - user shared resume/artifact; they said "reviewing"
    dead_end                 - they said "nothing right now, will reach out later"
    active_discussion        - genuine back-and-forth about role details
    ready_to_schedule        - they asked for availability / resume share / call
    unclassified             - last message ambiguous; defer to fallback heuristics

Fields written to convo["intent"]:
    tag:             one of the tags above
    confidence:      0-1 float
    rationale:       one-line LLM explanation
    abstain:         bool -- True when tag == "dead_end"
    abstain_reason:  str | None -- populated when abstain is True
    input_hash:      sha1(tail messages) -- cache key
    classified_at:   ISO timestamp
    model:           model id

Usage:
    python -m pipeline.intent_classifier
    python -m pipeline.intent_classifier --urn "urn:li:msg_conversation:..."
    python -m pipeline.intent_classifier --force
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Literal

from openai import AsyncOpenAI

from pipeline.config import CLASSIFIED_FILE, FAST_MODEL, MAX_CONCURRENT, USER_NAME
from pipeline.email_context import email_blob_for_intent_hash, email_sidebar_for_urn
from pipeline.followup_scheduler import load_lead_states, save_lead_states

IntentTag = Literal[
    "awaiting_their_move",
    "awaiting_their_feedback",
    "dead_end",
    "active_discussion",
    "ready_to_schedule",
    "unclassified",
]

VALID_TAGS: set[str] = {
    "awaiting_their_move",
    "awaiting_their_feedback",
    "dead_end",
    "active_discussion",
    "ready_to_schedule",
    "unclassified",
}

TAIL_MESSAGES = 6

INTENT_SYSTEM_PROMPT = f"""\
You are classifying the conversational state of a LinkedIn recruiter thread for \
{USER_NAME}. Read the final exchange and pick ONE tag. If a LINKED EMAIL block \
is present, treat it as authoritative for rejections / scheduling / interview \
updates / next steps even when the LinkedIn thread has not caught up yet.

Email signals (examples, not exhaustive):
- Rejection, role filled, pursuing other candidates, "not moving forward" → \
prefer dead_end even if LinkedIn still looks warm.
- Interview invite, loop update, take-home, offer, start date logistics → \
use ready_to_schedule or awaiting_their_feedback / active_discussion to match \
who owes the next move.

Return strict JSON: {{"tag": "<tag>", "confidence": <0..1>, \
"rationale": "<one sentence>"}}

Tag definitions:

- awaiting_their_move: User asked a concrete question or shared info; recruiter \
has not yet answered. Do not pester them.
- awaiting_their_feedback: User shared resume / availability / artifact; \
recruiter acknowledged ("thanks, will review", thumbs-up, "circle back") but \
hasn't decided yet. Respect their review time.
- dead_end: Recruiter stated explicitly that nothing is available now, thanked \
the user for reaching out, or said they'd reach out "if something comes up". \
No active opportunity. ABSTAIN from replying unless they re-engage. If email \
clearly closes the process, choose dead_end even when LinkedIn omits that news.
- active_discussion: Back-and-forth is alive about specific role/compensation/ \
stack. Continue the exchange naturally.
- ready_to_schedule: Recruiter asked for a call, availability, or resume share \
in the most recent LinkedIn message OR clearly in recent linked email. \
Prioritize scheduling next.
- unclassified: Cannot confidently fit any tag above.

Output ONLY the JSON object.
"""


def _iter_messages(convo: dict[str, Any]) -> list[dict[str, Any]]:
    return list(convo.get("messages") or [])


def _tail_text(convo: dict[str, Any], n: int = TAIL_MESSAGES) -> list[dict[str, str]]:
    messages = _iter_messages(convo)[-n:]
    tail: list[dict[str, str]] = []
    for msg in messages:
        sender = str(msg.get("sender") or "").strip() or "Unknown"
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        tail.append({
            "sender": sender,
            "text": _collapse_whitespace(text),
            "timestamp": str(msg.get("timestamp") or ""),
        })
    return tail


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _input_hash(convo: dict[str, Any]) -> str:
    urn = str(convo.get("conversationUrn") or "")
    payload = {
        "tail": _tail_text(convo),
        "user": USER_NAME,
        "schema": "intent.v1",
        "email": email_blob_for_intent_hash(urn),
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()


def _format_prompt(convo: dict[str, Any]) -> str:
    tail = _tail_text(convo)
    lines = [
        f"[{m['timestamp'] or '?'}] {m['sender']}: {m['text']}"
        for m in tail
    ]
    base = "RECENT MESSAGES (oldest -> newest):\n" + "\n".join(lines)
    urn = str(convo.get("conversationUrn") or "")
    email = email_sidebar_for_urn(urn) if urn else ""
    if email.strip():
        return base + "\n\n" + email.strip()
    return base


async def _classify_one(
    client: AsyncOpenAI,
    convo: dict[str, Any],
    semaphore: asyncio.Semaphore,
    model: str,
) -> dict[str, Any]:
    async with semaphore:
        try:
            resp = await client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": _format_prompt(convo)},
                ],
            )
            raw = resp.choices[0].message.content or "{}"
            parsed = json.loads(raw)
            tag = parsed.get("tag")
            if tag not in VALID_TAGS:
                tag = "unclassified"
            confidence = float(parsed.get("confidence") or 0)
            rationale = str(parsed.get("rationale") or "").strip()
            return {
                "tag": tag,
                "confidence": max(0.0, min(1.0, confidence)),
                "rationale": rationale,
                "model": model,
            }
        except Exception as exc:  # pragma: no cover - network/LLM edge
            from pipeline.error_log import log_error
            log_error("intent_classifier", type(exc).__name__, str(exc))
            return {
                "tag": "unclassified",
                "confidence": 0.0,
                "rationale": f"classifier_error: {exc}",
                "model": model,
                "error": str(exc),
            }


def _apply_intent(convo: dict[str, Any], result: dict[str, Any], input_hash: str) -> None:
    convo["intent"] = {
        "tag": result["tag"],
        "confidence": result["confidence"],
        "rationale": result.get("rationale", ""),
        "abstain": result["tag"] == "dead_end",
        "abstain_reason": (
            "recruiter or linked email indicates no active opportunity"
            if result["tag"] == "dead_end"
            else None
        ),
        "input_hash": input_hash,
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "model": result.get("model", ""),
    }
    if result.get("error"):
        convo["intent"]["error"] = result["error"]


async def classify_intents(
    target_urn: str | None = None,
    force: bool = False,
    model: str | None = None,
) -> None:
    if not CLASSIFIED_FILE.exists():
        print("Error: run classify_leads first.", file=sys.stderr)
        sys.exit(1)

    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)

    conversations = data.get("conversations") or []
    candidates: list[tuple[dict[str, Any], str]] = []
    for convo in conversations:
        if convo.get("classification", {}).get("category") != "recruiter":
            continue
        if target_urn and convo.get("conversationUrn") != target_urn:
            continue
        if not _iter_messages(convo):
            continue
        h = _input_hash(convo)
        existing = convo.get("intent") or {}
        if not force and existing.get("input_hash") == h and existing.get("tag") in VALID_TAGS:
            continue
        candidates.append((convo, h))

    if not candidates:
        print("No intents to classify -- cache hit for every recruiter thread.")
        _latch_declined(conversations)
        return

    chosen_model = model or FAST_MODEL
    print(f"Classifying intent for {len(candidates)} thread(s) via {chosen_model}")

    client = AsyncOpenAI()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [_classify_one(client, convo, semaphore, chosen_model) for convo, _ in candidates]
    results = await asyncio.gather(*tasks)

    for (convo, h), result in zip(candidates, results):
        _apply_intent(convo, result, h)
        other = next(
            (p.get("name") for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            "?",
        )
        print(f"  {other:30} -> {result['tag']:25} ({result['confidence']:.2f})")

    _latch_declined(conversations)

    with open(CLASSIFIED_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"Updated {CLASSIFIED_FILE}")


def _latch_declined(conversations: list[dict[str, Any]]) -> None:
    """Write durable declined status for any recruiter convo tagged dead_end.

    Once written, declined is not downgraded here — only the operator review UI
    (or a future unlatch path) can clear it. New recruiter conversations get a
    new URN and therefore a clean state, so this does not poison future leads
    from the same recruiter.
    """
    states = load_lead_states()
    latched = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for convo in conversations:
        if convo.get("classification", {}).get("category") != "recruiter":
            continue
        intent = convo.get("intent") or {}
        if intent.get("tag") != "dead_end":
            continue
        thread_id = str(
            convo.get("external_thread_id") or convo.get("conversationUrn") or ""
        )
        if not thread_id:
            continue
        existing = states.get(thread_id) or {}
        if existing.get("status") == "declined":
            continue
        urn = str(convo.get("conversationUrn") or "")
        source = "email" if urn and email_sidebar_for_urn(urn).strip() else "linkedin"
        rationale = str(intent.get("rationale") or "")[:200]
        states[thread_id] = {
            **existing,
            "status": "declined",
            "updated_at": now_iso,
            "closed_by": "intent_classifier",
            "closed_reason": rationale,
            "closed_source": source,
        }
        latched += 1
    if latched:
        save_lead_states(states)
        print(f"Latched {latched} lead(s) to declined status.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urn", help="Target a single conversation URN")
    parser.add_argument("--force", action="store_true", help="Ignore input-hash cache")
    parser.add_argument("--model", help="Override FAST_MODEL for this run")
    args = parser.parse_args()
    asyncio.run(classify_intents(target_urn=args.urn, force=args.force, model=args.model))


if __name__ == "__main__":
    main()

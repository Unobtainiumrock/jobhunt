#!/usr/bin/env python3
"""
Phase 1A: Lead Classification Pipeline

Two-stage combo model approach adapted from gravity-pulse/pipeline/llm_profiler.py:
  Stage 1 (o3): Classify each conversation as recruiter/networking/spam/personal
  Stage 2 (GPT-5): Extract structured metadata for recruiter conversations

Usage:
  python -m pipeline.classify_leads                  # classify all
  python -m pipeline.classify_leads --reclassify     # force reclassify already-classified
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from openai import AsyncOpenAI

from pipeline.config import (
    INBOX_FILE, CLASSIFIED_FILE, CLASSIFY_MODEL, GENERATION_MODEL, MAX_CONCURRENT, USER_NAME,
)

Category = Literal["recruiter", "networking", "spam", "personal"]

# --- PYDANTIC SCHEMAS ---

class ClassificationResult(BaseModel):
    reasoning: str = Field(
        description="1-3 sentences explaining the decision based on the rubric."
    )
    classification: Category
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguity_flags: list[Literal[
        "dual_role_participant", "evolved_intent",
        "unclear_initiator", "sparse_data", "mixed_signals"
    ]] = Field(description="List of flags, or empty array if clear.")

class MetadataResult(BaseModel):
    role_title: str | None
    company: str | None
    industry: str | None
    compensation_hints: str | None
    urgency: Literal["high", "medium", "low"] | None
    recruiter_type: Literal["agency", "in_house", "hiring_manager"] | None
    role_description_summary: str | None = Field(
        description="1-2 sentences summarizing the role."
    )
    skills_requested: list[str] = Field(
        description="Specific technical skills mentioned. Empty list if none."
    )
    location: str | None
    next_action_needed: str | None = Field(
        description="What the conversation is waiting on from the job seeker."
    )

# --- SYSTEM PROMPTS ---

CLASSIFICATION_SYSTEM = """\
<objective>
Classify the provided LinkedIn conversation into exactly one of the following categories: recruiter, networking, spam, or personal.
</objective>

<definitions>
- recruiter: High-intent outreach regarding a specific role. Includes agency, in-house talent, or hiring managers/founders actively looking to fill a position.
- networking: Professional connection building, introductions, or peer interactions. No active recruiting or interviewing intent.
- spam: Unsolicited sales pitches, automated vendor outreach (e.g., selling ad-tech services, offshore development, lead-gen), or irrelevant promotions.
- personal: Social messages, congratulations, or personal catch-ups with no professional intent.
</definitions>

<rubric_and_edge_cases>
1. Evaluate the participant's headline: A "Founder" messaging about their startup might be networking, but if they mention "growing the team" or "open roles", it is 'recruiter'.
2. Trajectory matters: If a conversation starts as "let's connect" (networking) but evolves into "I have a role for you" (recruiter), classify as 'recruiter'.
3. Value exchange: If they want to sell YOU a service, it is 'spam'. If they want to HIRE you, it is 'recruiter'.
</rubric_and_edge_cases>
"""

METADATA_SYSTEM = """\
<objective>
Extract structured metadata about the job opportunity from a LinkedIn recruiter conversation.
</objective>

<guidelines>
- urgency: "high" (explicit deadline/immediate need), "medium" (active search), "low" (exploratory/casual).
- recruiter_type: "agency" (third-party), "in_house" (company talent team), "hiring_manager" (founder or direct manager).
- skills_requested: Extract specific technical skills mentioned.
- next_action_needed: A short summary of what the job seeker needs to do next (e.g., "Send resume", "Provide availability").
</guidelines>

<few_shot_example>
Input Text:
[2026-03-10 14:00] Sarah (subject: Engineering at Acme Corp): Hi! I'm the engineering lead at Acme. We are looking for a Founding Engineer who knows Python and ML to join us ASAP. Comp is $180k-$220k. Let me know if you have time for a call this week!

Expected Output:
{
  "role_title": "Founding Engineer",
  "company": "Acme Corp",
  "industry": null,
  "compensation_hints": "$180k-$220k",
  "urgency": "high",
  "recruiter_type": "hiring_manager",
  "role_description_summary": "Looking for a Founding Engineer to join Acme Corp's team immediately.",
  "skills_requested": ["Python", "ML"],
  "location": null,
  "next_action_needed": "Provide availability for a call this week"
}
</few_shot_example>
"""

# --- PIPELINE FUNCTIONS ---

def _build_conversation_payload(convo: dict[str, Any]) -> dict[str, Any]:
    """Build the input payload for Stage 1 classification."""
    other_participants = [
        p for p in convo.get("participants", [])
        if p.get("name") != USER_NAME
    ]
    participant = other_participants[0] if other_participants else {
        "name": "Unknown", "headline": "", "profileUrn": ""
    }

    messages = convo.get("messages", [])
    initiator = messages[0]["sender"] if messages else "Unknown"

    if len(messages) <= 8:
        preview = messages
    else:
        preview = messages[:3] + messages[-2:]

    return {
        "participant": {
            "name": participant.get("name", "Unknown"),
            "headline": participant.get("headline", ""),
            "profileUrn": participant.get("profileUrn", ""),
        },
        "subject": messages[0].get("subject", "") if messages else "",
        "message_count": len(messages),
        "initiator": initiator,
        "messages_preview": [
            {"sender": m["sender"], "text": m.get("text", "")[:500]}
            for m in preview
        ],
    }


def _build_full_messages_text(convo: dict[str, Any], max_chars: int = 80_000) -> str:
    """Format full message thread for Stage 2."""
    lines: list[str] = []
    total = 0
    for m in convo.get("messages", []):
        ts = m.get("timestamp", "")[:16]
        sender = m.get("sender", "Unknown")
        text = m.get("text", "")
        subject = m.get("subject", "")
        line = f"[{ts}] {sender}: {text}"
        if subject:
            line = f"[{ts}] {sender} (subject: {subject}): {text}"
        if total + len(line) > max_chars:
            lines.append("... (truncated)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


async def classify_conversation(
    client: AsyncOpenAI,
    convo: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Stage 1: Use reasoning model to classify a conversation."""
    async with semaphore:
        payload = _build_conversation_payload(convo)
        user_prompt = (
            f"Classify this LinkedIn conversation.\n\n"
            f"<conversation_data>\n{json.dumps(payload, indent=2)}\n</conversation_data>"
        )
        try:
            resp = await client.beta.chat.completions.parse(
                model=CLASSIFY_MODEL,
                messages=[
                    {"role": "system", "content": CLASSIFICATION_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=ClassificationResult,
            )
            return resp.choices[0].message.parsed.model_dump()
        except Exception as e:
            print(f"  ERROR classifying: {e}", file=sys.stderr)
            return {
                "classification": "personal",
                "confidence": 0.0,
                "reasoning": f"Classification failed: {e}",
                "ambiguity_flags": ["error"],
            }


async def extract_metadata(
    client: AsyncOpenAI,
    convo: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any] | None:
    """Stage 2: Use fast model to extract metadata for recruiter conversations."""
    async with semaphore:
        messages_text = _build_full_messages_text(convo)
        participant = next(
            (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            {"name": "Unknown", "headline": ""},
        )
        user_prompt = (
            f"<recruiter_info>\nName: {participant.get('name')}\nHeadline: {participant.get('headline')}\n</recruiter_info>\n\n"
            f"<full_conversation>\n{messages_text}\n</full_conversation>"
        )
        try:
            resp = await client.beta.chat.completions.parse(
                model=GENERATION_MODEL,
                messages=[
                    {"role": "system", "content": METADATA_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=MetadataResult,
            )
            return resp.choices[0].message.parsed.model_dump()
        except Exception as e:
            print(f"  ERROR extracting metadata: {e}", file=sys.stderr)
            return None


async def classify_all(reclassify: bool = False) -> None:
    """Run the full two-stage classification pipeline."""
    if not INBOX_FILE.exists():
        print(f"Error: {INBOX_FILE} not found. Run the scraper first.", file=sys.stderr)
        sys.exit(1)

    with open(INBOX_FILE) as f:
        data = json.load(f)

    conversations: list[dict[str, Any]] = data.get("conversations", [])
    print(f"Loaded {len(conversations)} conversations from {INBOX_FILE}")

    existing: dict[str, dict[str, Any]] = {}
    if not reclassify and CLASSIFIED_FILE.exists():
        with open(CLASSIFIED_FILE) as f:
            existing_data = json.load(f)
        for c in existing_data.get("conversations", []):
            if "classification" in c:
                existing[c["conversationUrn"]] = c

    # Short-circuit connection-ack duplicate stubs flagged by pipeline.dedupe_threads.
    # These threads represent the same person as a canonical longer thread, so we
    # tag them as "duplicate_stub" without spending a classifier call.
    dedupe_stubs = [c for c in conversations if c.get("_category_override") == "networking_stub"]
    for convo in dedupe_stubs:
        if "classification" in convo and convo["classification"].get("category") == "duplicate_stub":
            continue
        convo["classification"] = {
            "category": "duplicate_stub",
            "confidence": 1.0,
            "reasoning": f"connection-ack stub of {convo.get('_duplicate_of', '?')}",
            "ambiguity_flags": [],
            "classified_at": datetime.now(timezone.utc).isoformat(),
            "model_versions": {"stage1": "dedupe_threads"},
        }
    if dedupe_stubs:
        print(f"  {len(dedupe_stubs)} connection-ack stub(s) auto-tagged as duplicate_stub")

    to_classify = [
        c for c in conversations
        if (reclassify or c["conversationUrn"] not in existing)
        and c.get("_category_override") != "networking_stub"
    ]
    already_done = len(conversations) - len(to_classify) - len(dedupe_stubs)
    if already_done > 0:
        print(f"  {already_done} already classified, {len(to_classify)} to process")

    client = AsyncOpenAI()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    print(f"\n--- Stage 1: Classification via {CLASSIFY_MODEL} ---")
    t0 = time.time()

    classification_tasks = [
        classify_conversation(client, c, semaphore) for c in to_classify
    ]
    classifications = await asyncio.gather(*classification_tasks)

    for convo, clf in zip(to_classify, classifications):
        convo["classification"] = {
            "category": clf["classification"],
            "confidence": clf["confidence"],
            "reasoning": clf["reasoning"],
            "ambiguity_flags": clf["ambiguity_flags"],
            "classified_at": datetime.now(timezone.utc).isoformat(),
            "model_versions": {"stage1": CLASSIFY_MODEL},
        }

    elapsed = time.time() - t0
    recruiter_count = sum(
        1 for c in classifications if c["classification"] == "recruiter"
    )
    print(f"  Classified {len(to_classify)} conversations in {elapsed:.1f}s")
    print(f"  Results: {recruiter_count} recruiter, "
          f"{sum(1 for c in classifications if c['classification'] == 'networking')} networking, "
          f"{sum(1 for c in classifications if c['classification'] == 'spam')} spam, "
          f"{sum(1 for c in classifications if c['classification'] == 'personal')} personal")

    recruiter_convos = [
        c for c in to_classify
        if c.get("classification", {}).get("category") == "recruiter"
    ]

    if recruiter_convos:
        print(f"\n--- Stage 2: Metadata extraction via {GENERATION_MODEL} ---")
        t0 = time.time()

        metadata_tasks = [
            extract_metadata(client, c, semaphore) for c in recruiter_convos
        ]
        metadatas = await asyncio.gather(*metadata_tasks)

        for convo, meta in zip(recruiter_convos, metadatas):
            if meta:
                convo["metadata"] = meta
                convo["classification"]["model_versions"]["stage2"] = GENERATION_MODEL

        elapsed = time.time() - t0
        print(f"  Extracted metadata for {len(recruiter_convos)} conversations in {elapsed:.1f}s")

    # Preserve enrichment fields written by later pipeline stages so that
    # re-classifying an inbox does not clobber drafts, stage machine output,
    # intent tags, etc. ``_SCRAPE_FIELDS`` is the set of fields owned by the
    # raw scrape + classifier; everything else in ``existing[urn]`` is
    # downstream enrichment and must be carried forward verbatim unless the
    # fresh ``convo`` already provides a newer value for that key.
    _SCRAPE_FIELDS = {
        "conversationUrn",
        "participants",
        "lastActivityAt",
        "createdAt",
        "unreadCount",
        "read",
        "title",
        "lastMessagePreview",
        "messages",
        "classification",
        "metadata",
        "_duplicate_of",
        "_category_override",
        "_dedupe_reason",
    }
    for convo in conversations:
        urn = convo["conversationUrn"]
        prev = existing.get(urn)
        if not prev:
            continue
        if "classification" not in convo:
            convo["classification"] = prev.get("classification")
            convo["metadata"] = prev.get("metadata")
        for key, value in prev.items():
            if key in _SCRAPE_FIELDS:
                continue
            convo.setdefault(key, value)

    output = {
        "classifiedAt": datetime.now(timezone.utc).isoformat(),
        "sourceFile": str(INBOX_FILE),
        "conversationCount": len(conversations),
        "conversations": conversations,
    }

    CLASSIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CLASSIFIED_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {len(conversations)} conversations to {CLASSIFIED_FILE}")


def main() -> None:
    reclassify = "--reclassify" in sys.argv
    asyncio.run(classify_all(reclassify=reclassify))


if __name__ == "__main__":
    main()

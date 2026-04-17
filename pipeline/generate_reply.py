#!/usr/bin/env python3
"""
Phase 3A: Dynamic Reply Generator

Generates personalized replies by combining template structure with
LLM-generated dynamic content. Uses the safety module for identity protection.

Usage:
  python -m pipeline.generate_reply
  python -m pipeline.generate_reply --urn "urn:li:msg_conversation:..."
  python -m pipeline.generate_reply --audit-drafts
  python -m pipeline.generate_reply --purge-stale
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from openai import AsyncOpenAI

from pipeline.config import (
    CLASSIFIED_FILE, PROFILE_FILE, TEMPLATES_DIR,
    GENERATION_MODEL, MAX_CONCURRENT, USER_NAME, USER_PHONE, USER_WEBSITE,
    SCORE_AUTO_REPLY, SCORE_REVIEW, REPLY_STALE_DAYS,
)
from pipeline.safety import (
    build_system_prompt, validate_outbound, wrap_conversation_context,
)
from search.search_leads import hybrid_search, search_profile

MAX_PROFILE_RETRIEVALS = 3
MAX_SIMILAR_MESSAGE_RETRIEVALS = 2
MAX_PROFILE_RETRIEVAL_CANDIDATES = 6
MAX_SIMILAR_MESSAGE_CANDIDATES = 12
MAX_RETRIEVAL_CHARS = 220
PROFILE_SCORE_FLOOR = 0.42
SIMILAR_MESSAGE_SCORE_FLOOR = 0.015
SIMILAR_MESSAGE_OVERLAP_FLOOR = 0.12
RETRIEVAL_STOPWORDS = {
    "about", "after", "also", "are", "asap", "best", "building", "came",
    "could", "engineer", "engineering", "for", "free", "from", "gone", "has",
    "have", "hello", "hey", "hiring", "hope", "interested", "looking", "morning",
    "nicholas", "open", "position", "reach", "regards", "role", "roles", "share",
    "short", "suits", "talk", "team", "thank", "thanks", "that", "the", "their",
    "them", "they", "this", "want", "with", "work", "would", "your",
}


def _load_templates() -> dict[str, Any]:
    template_file = TEMPLATES_DIR / "reply_templates.yaml"
    with open(template_file) as f:
        return yaml.safe_load(f)


def _load_profile() -> dict[str, Any]:
    with open(PROFILE_FILE) as f:
        return yaml.safe_load(f)


REPLY_GENERATION_PROMPT = """\
You are {user_name} replying to a recruiter on LinkedIn.

Write 1-3 SHORT sentences. Max 40 words total. This is the body only. Greeting
and signoff are added separately.

## PURPOSE
You're assessing fit and exchanging info, not selling yourself. Get to the point:
- If you're interested: say so briefly.
- If you need info: ask the specific question.
- If there's an ongoing thread: respond to what they said, don't monologue.

## TEMPLATE AWARENESS (critical, read carefully)
The following are ALREADY appended to your reply by a template system. Do NOT
include ANY of these in your response:
- Phone number, email, or website. NEVER say "you can reach me at" or give contact info.
- Resume offers. NEVER say "I can send my resume" or "happy to share my resume."
- Call-to-action. NEVER say "free to chat", "let's hop on a call", "what time works",
  "when can we talk", or any scheduling language. The template handles this.
Your job is ONLY the substantive body. No logistics, no contact info, no CTAs.

## BANNED WORDS AND PATTERNS
Never use em-dashes (—). Use periods, commas, or just start a new sentence.
Never use ANY of these: "thanks for reaching out", "excited about", "aligns with",
"contribute", "looking forward", "resonates", "I'd love to", "equipped", "honed",
"intrigued", "well-prepared", "add value", "positioned", "foundation", "sharpened",
"right up my alley", "sounds solid", "sounds interesting", "hooks me", "let's chat",
"particularly", "impactful", "innovative", "keen on", "fascinating", "tremendous",
"incredible", "leverage", "synergy", "passionate".

## ANTI-SYCOPHANCY (critical)
- Do NOT compliment the role, company, or recruiter. No "that sounds like a great
  project" or "what an interesting challenge." Just respond to the substance.
- Do NOT parrot back what the recruiter said. They know what they told you.
- Do NOT editorialize about innovation, impact, or how cool something is.
- A real engineer's reply is mostly: "yeah I've done similar work, free to talk?"
  Not a paragraph about how amazing the opportunity sounds.

## CREDENTIALS
Profile context: {highlights}

Mention ONE thing from your background ONLY if directly relevant. Keep it to a
clause, not a sentence. Example: "I've built similar systems before". Not
"My experience building RTB platforms at Gravity has given me deep expertise in..."

## RETRIEVAL CONTEXT
- The prompt may include retrieved profile chunks and past recruiter snippets.
- Use them only if they are directly relevant to the current recruiter message.
- Never dump retrieved context back verbatim.
- Prefer one precise detail over multiple weak references.

## TONE
{tone}. Terse > verbose. If the conversation is casual, match it.

Return valid JSON:
{{
  "dynamic_body": "<your 1-3 sentence reply, max 40 words>",
  "recruiter_first_name": "<first name from their messages>"
}}
"""


def _compact_text(value: str, max_chars: int = MAX_RETRIEVAL_CHARS) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def _normalize_query_part(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value).strip()


def _sanitize_retrieval_message_text(text: str) -> str:
    sanitized = _compact_text(text, max_chars=260)
    drop_patterns = [
        r"\bfree to chat(?: when suits you)?\b",
        r"\bwhat(?:'s|s) the best number to reach you on\b",
        r"\bbest number to reach you\b",
        r"\bbest time to talk\b",
        r"\bopen to connect\b",
        r"\bwaiting for your reply\b",
        r"\bthank you for connecting\b",
        r"\bshare (?:your )?resume\b",
        r"\bsend me your updated resume\b",
    ]
    for pattern in drop_patterns:
        sanitized = re.sub(pattern, " ", sanitized, flags=re.IGNORECASE)
    return _compact_text(sanitized, max_chars=220)


def _iter_recruiter_messages(convo: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    for msg in convo.get("messages", []):
        if msg.get("sender") == USER_NAME or not msg.get("text"):
            continue
        cleaned = _sanitize_retrieval_message_text(msg.get("text", "").strip())
        if cleaned:
            messages.append(cleaned)
    return messages


def _build_profile_retrieval_query(convo: dict[str, Any], meta: dict[str, Any]) -> str:
    other = next(
        (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
        {},
    )
    recruiter_messages = _iter_recruiter_messages(convo)
    parts = [
        _normalize_query_part(meta.get("role_title", "")),
        _normalize_query_part(meta.get("company", "")),
        _normalize_query_part(meta.get("role_description_summary", "")),
        _normalize_query_part(meta.get("skills_or_keywords", "")),
        _normalize_query_part(other.get("headline", "")),
        recruiter_messages[-1] if recruiter_messages else "",
    ]
    query = " ".join(part for part in parts if part)
    return _compact_text(query, max_chars=420)


def _build_similar_message_query(convo: dict[str, Any], meta: dict[str, Any]) -> str:
    other = next(
        (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
        {},
    )
    recruiter_messages = _iter_recruiter_messages(convo)
    opener = recruiter_messages[0] if recruiter_messages else ""
    parts = [
        _normalize_query_part(meta.get("role_title", "")),
        _normalize_query_part(meta.get("company", "")),
        _normalize_query_part(meta.get("skills_or_keywords", "")),
        _normalize_query_part(other.get("headline", "")),
        opener,
    ]
    query = " ".join(part for part in parts if part)
    return _compact_text(query, max_chars=320)


def _extract_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9+#./-]{3,}", query.lower()):
        term = raw.strip("._-/")
        if not term or term.isdigit() or term in RETRIEVAL_STOPWORDS:
            continue
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _lexical_overlap_score(text: str, query_terms: list[str]) -> float:
    if not text or not query_terms:
        return 0.0
    lowered = text.lower()
    matches = sum(1 for term in query_terms if term in lowered)
    return matches / len(query_terms)


def _retrieve_profile_context(query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    debug = {
        "query": query,
        "score_floor": PROFILE_SCORE_FLOOR,
        "candidates_considered": 0,
        "kept": 0,
        "skipped_low_score": 0,
        "skipped_duplicate": 0,
    }
    if not query:
        return [], debug

    try:
        results = search_profile(query, top_k=MAX_PROFILE_RETRIEVAL_CANDIDATES)
    except Exception as exc:
        debug["error"] = str(exc)
        return [], debug

    trimmed: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for result in results:
        debug["candidates_considered"] += 1
        score = float(result.get("score") or 0.0)
        text = _compact_text(result.get("text", ""))
        if score < PROFILE_SCORE_FLOOR:
            debug["skipped_low_score"] += 1
            continue
        if text in seen_text:
            debug["skipped_duplicate"] += 1
            continue
        seen_text.add(text)
        trimmed.append({
            "chunk_type": result.get("chunk_type", "unknown"),
            "score": score,
            "text": text,
        })
        if len(trimmed) >= MAX_PROFILE_RETRIEVALS:
            break

    debug["kept"] = len(trimmed)
    return trimmed, debug


def _anchor_terms(meta: dict[str, Any]) -> list[str]:
    """Terms that must appear in retrieved similar-message snippets.

    These are the CURRENT thread's company / role keywords. Snippets from
    unrelated historical threads that don't mention any of these anchors are
    discarded -- this is what prevents the recall of stale topics (e.g. pulling
    in a "Snorkel AI" snippet while drafting a reply about Adaptional).
    """
    candidates = [
        meta.get("company"),
        meta.get("role_title"),
    ]
    keywords = meta.get("skills_or_keywords") or ""
    if isinstance(keywords, str):
        candidates.append(keywords)
    elif isinstance(keywords, list):
        candidates.extend(str(k) for k in keywords)

    anchors: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        for term in re.findall(r"[A-Za-z][A-Za-z0-9+#.]{2,}", str(raw).lower()):
            if term in RETRIEVAL_STOPWORDS or term in seen:
                continue
            seen.add(term)
            anchors.append(term)
    return anchors


def _retrieve_similar_recruiter_messages(
    query: str,
    conversation_urn: str,
    anchors: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query_terms = _extract_query_terms(query)
    anchors = anchors or []
    debug = {
        "query": query,
        "query_terms": query_terms,
        "anchors": anchors,
        "score_floor": SIMILAR_MESSAGE_SCORE_FLOOR,
        "overlap_floor": SIMILAR_MESSAGE_OVERLAP_FLOOR,
        "candidates_considered": 0,
        "kept": 0,
        "skipped_same_conversation": 0,
        "skipped_self_message": 0,
        "skipped_low_score": 0,
        "skipped_low_overlap": 0,
        "skipped_no_anchor": 0,
        "skipped_duplicate": 0,
    }
    if not query:
        return [], debug

    try:
        results = hybrid_search(query, top_k=MAX_SIMILAR_MESSAGE_CANDIDATES, category="recruiter")
    except Exception as exc:
        debug["error"] = str(exc)
        return [], debug

    trimmed: list[dict[str, Any]] = []
    fallback_candidates: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    debug["fallback_used"] = False
    for result in results:
        debug["candidates_considered"] += 1
        if result.get("conversation_urn") == conversation_urn:
            debug["skipped_same_conversation"] += 1
            continue
        if result.get("sender") == USER_NAME:
            debug["skipped_self_message"] += 1
            continue

        score = float(result.get("hybrid_score", result.get("score")) or 0.0)
        if score < SIMILAR_MESSAGE_SCORE_FLOOR:
            debug["skipped_low_score"] += 1
            continue

        text = _compact_text(result.get("text", ""), max_chars=180)
        # Anchor gate: if we have company/role keywords, require at least one to
        # appear in the snippet. Prevents unrelated prior threads from leaking in.
        if anchors:
            lowered = text.lower()
            if not any(anchor in lowered for anchor in anchors):
                debug["skipped_no_anchor"] += 1
                continue

        overlap = _lexical_overlap_score(text, query_terms)
        key = f"{result.get('conversation_urn')}::{result.get('timestamp')}::{result.get('sender')}"
        if key in seen_keys:
            debug["skipped_duplicate"] += 1
            continue
        seen_keys.add(key)

        candidate = {
            "other_participant": result.get("other_participant", "Unknown"),
            "sender": result.get("sender", "Unknown"),
            "text": text,
            "score": score,
            "overlap": overlap,
        }
        if query_terms and overlap < SIMILAR_MESSAGE_OVERLAP_FLOOR:
            debug["skipped_low_overlap"] += 1
            fallback_candidates.append(candidate)
            continue

        trimmed.append(candidate)
        if len(trimmed) >= MAX_SIMILAR_MESSAGE_RETRIEVALS:
            break

    if not trimmed and fallback_candidates:
        fallback = sorted(
            fallback_candidates,
            key=lambda item: (item["overlap"], item["score"]),
            reverse=True,
        )[0]
        fallback["fallback_used"] = True
        trimmed.append(fallback)
        debug["fallback_used"] = True

    debug["kept"] = len(trimmed)
    return trimmed, debug


def _build_retrieval_context_block(
    profile_hits: list[dict[str, Any]],
    similar_messages: list[dict[str, Any]],
) -> str:
    if not profile_hits and not similar_messages:
        return ""

    lines = ["===RETRIEVED CONTEXT (use sparingly and only if relevant)==="]
    if profile_hits:
        lines.append("Relevant profile chunks:")
        for hit in profile_hits:
            lines.append(f"- [{hit['chunk_type']}] {hit['text']}")
    if similar_messages:
        lines.append("Similar recruiter-message snippets:")
        for hit in similar_messages:
            lines.append(f"- [{hit['other_participant']}] {hit['text']}")
    lines.append("===END RETRIEVED CONTEXT===")
    return "\n".join(lines)


def _get_recruiter_first_name(convo: dict[str, Any]) -> str:
    """Extract recruiter's first name from conversation."""
    other = next(
        (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
        {"name": "Unknown"},
    )
    return other.get("name", "Unknown").split()[0]


def _build_recruiter_context(convo: dict[str, Any], meta: dict[str, Any]) -> str:
    """Build structured context about who the recruiter is and what they represent."""
    other = next(
        (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
        {},
    )
    recruiter_name = other.get("name", "Unknown")
    headline = other.get("headline", "")
    rtype = meta.get("recruiter_type", "unknown")
    company = meta.get("company", "unknown")
    role = meta.get("role_title", "unknown")
    urgency = meta.get("urgency", "unknown")
    role_summary = meta.get("role_description_summary", "")

    type_labels = {
        "agency": f"{recruiter_name} is a THIRD-PARTY RECRUITER (agency/consultant). "
                  f"They do NOT work at {company}. They are recruiting on behalf of {company}. "
                  f"Do NOT say things like 'sounds like you're building cool stuff'. "
                  f"they aren't building anything, they're a middleman.",
        "in_house": f"{recruiter_name} works at {company} on the talent/recruiting team. "
                    f"They represent {company} but are not on the engineering team.",
        "hiring_manager": f"{recruiter_name} is likely a hiring manager or founder at {company}. "
                          f"They are directly involved in the work and team.",
    }
    type_desc = type_labels.get(rtype, f"Recruiter type unclear for {recruiter_name}.")

    lines = [
        "===RECRUITER CONTEXT (use this to calibrate your reply)===",
        f"Recruiter: {recruiter_name} ({headline})",
        f"Type: {rtype}",
        type_desc,
        f"Hiring company: {company}",
        f"Role: {role}",
        f"Urgency: {urgency}",
    ]
    if role_summary:
        lines.append(f"Role summary: {role_summary}")
    lines.append("===END RECRUITER CONTEXT===")
    return "\n".join(lines)


async def generate_reply_body(
    client: AsyncOpenAI,
    convo: dict[str, Any],
    profile: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Generate the dynamic body of a reply using LLM."""
    async with semaphore:
        score_data = convo.get("score", {})
        highlights = score_data.get("profile_highlights", [])
        if not highlights:
            highlights = [
                s["name"] for s in profile.get("skills", {}).get("technical", [])[:3]
            ]

        tone = profile.get("communication_style", {}).get("tone", "direct")
        conversation_context = wrap_conversation_context(convo.get("messages", []))

        meta = convo.get("metadata") or {}
        recruiter_context = _build_recruiter_context(convo, meta)
        profile_query = _build_profile_retrieval_query(convo, meta)
        similar_message_query = _build_similar_message_query(convo, meta)
        intent_tag = _intent_tag(convo)
        stage = derive_conversation_stage(convo)
        convo["stage"] = stage

        # Geo clarification: recruiter said on-site/hybrid but never stated a
        # region. Don't abstain (might be Bay), don't schedule yet -- just
        # ask where the role is based before anything else.
        geo = convo.get("geo") or {}
        ask_location = bool(geo.get("ask_location"))
        geo_context = ""
        if ask_location:
            geo_context = (
                "===GEO CLARIFICATION (HIGHEST PRIORITY)===\n"
                "The recruiter stated the role is "
                f"{geo.get('work_mode') or 'on-site/hybrid'} but never said "
                "where. Your reply MUST open by briefly asking where the role "
                f"is based (what city/metro). {USER_NAME} is based in the SF "
                "Bay Area and cannot commit to logistics without knowing the "
                "location. Keep the location question short (one sentence). "
                "Do NOT offer calendar slots, do NOT send the resume, do NOT "
                "commit to compensation yet. After the question, one sentence "
                "acknowledging their role pitch is fine.\n"
                "===END GEO CLARIFICATION===\n"
            )

        scheduling_block = ""
        proposed_slots: list[dict[str, Any]] = []
        # Skip scheduling entirely if we still need to confirm location --
        # offering slots before knowing the city wastes a turn.
        if stage == "ready_to_schedule" and not ask_location:
            try:
                from agents.calendar_agent import propose_slots
                proposed_slots = propose_slots(duration_minutes=30, window_days=5)
            except Exception as exc:  # pragma: no cover - calendar optional
                proposed_slots = []
                scheduling_block = (
                    f"===SCHEDULING CONTEXT===\n"
                    f"Calendar lookup failed ({exc}); offer to align on a time.\n"
                    f"===END SCHEDULING CONTEXT===\n"
                )
            if proposed_slots:
                slot_lines = "\n".join(f"- {s['label']}" for s in proposed_slots)
                scheduling_block = (
                    "===SCHEDULING CONTEXT (use these slots verbatim)===\n"
                    "They asked to schedule. Offer these options, phrased naturally:\n"
                    f"{slot_lines}\n"
                    "===END SCHEDULING CONTEXT===\n"
                )

        # Drop retrieval entirely when the recruiter is "reviewing" our artifact.
        # In that stage the generator just needs a light status ping, not a
        # freshly-pitched elevator pitch built on old threads.
        skip_retrieval = intent_tag == "awaiting_their_feedback"
        if skip_retrieval:
            profile_hits: list[dict[str, Any]] = []
            profile_debug: dict[str, Any] = {"skipped_reason": "intent_awaiting_their_feedback"}
            similar_messages: list[dict[str, Any]] = []
            similar_messages_debug: dict[str, Any] = {
                "skipped_reason": "intent_awaiting_their_feedback",
            }
        else:
            profile_hits, profile_debug = _retrieve_profile_context(profile_query)
            similar_messages, similar_messages_debug = _retrieve_similar_recruiter_messages(
                similar_message_query,
                convo.get("conversationUrn", ""),
                anchors=_anchor_terms(meta),
            )
        retrieval_context = _build_retrieval_context_block(profile_hits, similar_messages)

        system_prompt = build_system_prompt(USER_NAME, tone=tone, stage=stage)
        user_prompt = REPLY_GENERATION_PROMPT.format(
            user_name=USER_NAME,
            highlights=", ".join(highlights),
            tone=tone,
        ) + f"\n\n{geo_context}\n\n{recruiter_context}\n\n{scheduling_block}\n\n{retrieval_context}\n\n{conversation_context}"

        try:
            resp = await client.chat.completions.create(
                model=GENERATION_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)

            body = result.get("dynamic_body", "")
            validation = validate_outbound(body)
            if not validation.is_safe:
                return {
                    "error": "safety_violation",
                    "violations": validation.violations,
                    "needs_regeneration": True,
                }

            result["retrieval_query"] = profile_query
            result["retrieval_queries"] = {
                "profile": profile_query,
                "similar_messages": similar_message_query,
            }
            result["retrieval_debug"] = {
                "profile": profile_debug,
                "similar_messages": similar_messages_debug,
            }
            result["profile_hits"] = profile_hits
            result["similar_messages"] = similar_messages
            result["proposed_slots"] = proposed_slots
            return result
        except Exception as e:
            return {
                "error": str(e),
                "dynamic_body": "",
                "retrieval_query": profile_query,
                "retrieval_queries": {
                    "profile": profile_query,
                    "similar_messages": similar_message_query,
                },
                "retrieval_debug": {
                    "profile": profile_debug,
                    "similar_messages": similar_messages_debug,
                },
                "profile_hits": profile_hits,
                "similar_messages": similar_messages,
            }


def assemble_reply(
    templates: dict[str, Any],
    tier: str,
    dynamic_body: str,
    recruiter_first_name: str,
    score_data: dict[str, Any],
) -> str:
    """Assemble the full reply from template components + dynamic body."""
    t = templates.get(tier, templates.get("high_confidence", {}))

    greeting_variants = t.get("greeting_variants", ["Hi {recruiter_first_name},"])
    greeting = random.choice(greeting_variants).format(
        recruiter_first_name=recruiter_first_name,
    )

    contact_block = t.get("contact_block", "").format(
        phone=USER_PHONE, website=USER_WEBSITE,
    ).strip()
    resume_policy = t.get("resume_policy", "").strip()

    parts: list[str] = [greeting, ""]

    if tier == "medium_confidence":
        qualifiers = t.get("interest_qualifier_variants", [])
        if qualifiers:
            parts.append(random.choice(qualifiers))
            parts.append("")

    parts.append(dynamic_body)
    parts.append("")

    if tier in ("high_confidence", "medium_confidence") and resume_policy:
        parts.append(resume_policy)
        parts.append("")

    if contact_block:
        parts.append(contact_block)
        parts.append("")

    if tier == "high_confidence":
        cta_variants = t.get("cta_variants", [])
        if cta_variants:
            parts.append(random.choice(cta_variants))
    elif tier == "medium_confidence":
        questions = t.get("clarifying_questions", [])
        if questions:
            parts.append(random.choice(questions))
    elif tier == "low_confidence":
        gaps = score_data.get("gaps", [])
        strengths = score_data.get("strengths", [])
        raw_gap = gaps[0] if gaps else "my background may not fully align"
        raw_strength = strengths[0] if strengths else "my technical skills"
        gap_summary = raw_gap.split(".")[0].lower().strip()
        strength = raw_strength.split(".")[0].lower().strip()
        conditionals = t.get("conditional_interest_variants", [])
        if conditionals:
            parts.append(random.choice(conditionals).format(
                gap_summary=gap_summary, strength=strength,
            ))

    return "\n".join(parts).strip()


def _last_sender(convo: dict[str, Any]) -> str:
    """Return the sender name of the most recent message, or '' if none."""
    messages = convo.get("messages") or []
    if not messages:
        return ""
    return str(messages[-1].get("sender") or messages[-1].get("from") or "")


def _user_name_tokens() -> list[str]:
    return [t for t in USER_NAME.lower().split() if len(t) >= 3]


def _sender_matches_user(sender: str) -> bool:
    s = sender.lower()
    if not s:
        return False
    return any(tok in s for tok in _user_name_tokens())


def _user_was_last_sender(convo: dict[str, Any]) -> bool:
    last = _last_sender(convo).lower()
    if not last:
        return False
    return _sender_matches_user(last)


def _last_inbound_message(convo: dict[str, Any]) -> dict[str, Any] | None:
    """Most recent message not attributed to the user (recruiter / system)."""
    for msg in reversed(convo.get("messages") or []):
        sender = str(msg.get("sender") or msg.get("from") or "")
        if _sender_matches_user(sender):
            continue
        if sender.strip():
            return msg
    return None


def _inbound_context_fingerprint(convo: dict[str, Any]) -> str:
    """Stable hash of the latest inbound turn (sender + timestamp + text).

    Used to detect thread drift when message_count is unchanged but the
    scraper replaced or rewrote the tail (dedupe, re-sync, edited preview).
    """
    msg = _last_inbound_message(convo)
    if not msg:
        return "no_inbound"
    sender = (msg.get("sender") or msg.get("from") or "").strip()
    ts = str(msg.get("timestamp") or "")
    text = re.sub(r"\s+", " ", (msg.get("text") or "").strip())[:1200]
    raw = f"{sender}\n{ts}\n{text}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _intent_tag(convo: dict[str, Any]) -> str:
    intent = convo.get("intent") or {}
    return str(intent.get("tag") or "unclassified")


def _should_abstain(convo: dict[str, Any]) -> bool:
    return bool((convo.get("intent") or {}).get("abstain"))


# Intent tags that indicate an ongoing engagement where the freshness rule
# should NOT fire, even if the last inbound is old. A thread mid-interview
# can legitimately go quiet for >7 days; we still owe them a reply.
_ONGOING_INTENT_TAGS = frozenset({
    "ready_to_schedule",
    "awaiting_their_feedback",
})


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _last_inbound_timestamp(convo: dict[str, Any]) -> datetime | None:
    """Most recent inbound message time, or None."""
    msg = _last_inbound_message(convo)
    if not msg:
        return None
    return _parse_iso(msg.get("timestamp"))


def _is_stale_inbound(convo: dict[str, Any], stage: str) -> bool:
    """True if the last inbound is older than REPLY_STALE_DAYS and the
    conversation does NOT show a real ongoing engagement on our side.

    Exemption rule: only skip the stale gate when we have actually
    participated (user_turn_count > 0) AND the stage/intent indicates we are
    mid-process. A recruiter that pinged 14 days ago with an "interested in
    scheduling" tone but which we never answered is NOT an ongoing process --
    that's a cold thread we let lapse, and replying now reads as automated.
    """
    if REPLY_STALE_DAYS <= 0:
        return False
    last_in = _last_inbound_timestamp(convo)
    if last_in is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=REPLY_STALE_DAYS)
    if last_in >= cutoff:
        return False

    messages = convo.get("messages") or []
    user_turn_count = sum(
        1 for m in messages
        if _sender_matches_user(str(m.get("sender") or m.get("from") or ""))
    )
    if user_turn_count == 0:
        return True

    intent_tag = _intent_tag(convo)
    if intent_tag in _ONGOING_INTENT_TAGS:
        return False
    if stage in {"resume_shared", "ready_to_schedule", "awaiting_feedback"}:
        return False
    return True


RESUME_SHARED_MARKERS = (
    "resume",
    "cv",
    "fleischhauer.dev",
    "510-906-5492",
    "/in/nicholas",
)


def derive_conversation_stage(convo: dict[str, Any]) -> str:
    """Derive a multi-turn conversation stage from messages + intent tag.

    Stage lattice:
        cold_outreach -> info_gathering -> resume_shared -> call_scheduled
        -> (awaiting_feedback) -> (dead_end) -> (ready_to_schedule)

    Ordering rules:
    - If intent is `dead_end`, stage is `dead_end` (terminal).
    - If intent is `ready_to_schedule`, stage is `ready_to_schedule`.
    - If intent is `awaiting_their_feedback`, stage is `awaiting_feedback`.
    - Else, count user turns and look for resume markers to promote further.
    """
    intent_tag = _intent_tag(convo)
    if intent_tag == "dead_end":
        return "dead_end"
    if intent_tag == "ready_to_schedule":
        return "ready_to_schedule"
    if intent_tag == "awaiting_their_feedback":
        return "awaiting_feedback"

    messages = convo.get("messages") or []
    user_messages = [m for m in messages if (m.get("sender") or "") == USER_NAME]
    user_turn_count = len(user_messages)

    resume_shared = any(
        marker in (m.get("text") or "").lower()
        for m in user_messages
        for marker in RESUME_SHARED_MARKERS
    )

    if resume_shared:
        return "resume_shared"
    if user_turn_count == 0:
        return "cold_outreach"
    return "info_gathering"


def _needs_reply(convo: dict[str, Any], regenerate: bool) -> bool:
    """Check if a conversation needs a new reply generated."""
    if regenerate:
        return True
    reply = convo.get("reply")
    if not reply or reply.get("status") == "error":
        # Even for missing/errored drafts, refuse to regenerate if I already spoke last.
        return not _user_was_last_sender(convo)
    reply_status = reply.get("status")
    if reply_status in ("approved", "rejected", "sent", "manually_handled"):
        return False
    # Stale-inbound abstains should re-evaluate when a new message lands so
    # the recruiter pinging again immediately reopens the draft.
    if reply_status == "abstained":
        abstain_reason = str(reply.get("abstain_reason") or "")
        msg_count = len(convo.get("messages", []))
        reply_msg_count = reply.get("message_count_at_generation", 0)
        if abstain_reason.startswith("stale_inbound") and msg_count != reply_msg_count:
            return True
        return False
    # If the thread's last message is mine, skip: they need to respond first.
    if _user_was_last_sender(convo):
        return False
    msg_count = len(convo.get("messages", []))
    reply_msg_count = reply.get("message_count_at_generation", 0)
    if msg_count != reply_msg_count:
        return True
    stored_fp = reply.get("context_fingerprint")
    if stored_fp and stored_fp != _inbound_context_fingerprint(convo):
        return True
    return False


def reconcile_draft_threads(conversations: list[dict[str, Any]]) -> dict[str, int]:
    """Align pending reply drafts with the live thread tail.

    - If the user already sent the last message (manual LinkedIn reply, etc.),
      flip the draft to ``manually_handled`` so we never double-send.
    - If ``context_fingerprint`` no longer matches the latest inbound turn,
      bump ``message_count_at_generation`` so ``_needs_reply`` forces a regen
      on this pipeline pass (same message count but edited / re-synced tail).
    - Legacy drafts without a fingerprint: stamp the current tail once so we
      can detect drift on subsequent runs without mass-regenerating today.
    """
    now = datetime.now(timezone.utc).isoformat()
    stats = {"manually_handled": 0, "thread_drift": 0, "legacy_fp_stamped": 0}
    for convo in conversations:
        if convo.get("classification", {}).get("category") != "recruiter":
            continue
        reply = convo.get("reply")
        if not reply or reply.get("status") not in ("draft", "auto_send"):
            continue
        if _user_was_last_sender(convo):
            reply["status"] = "manually_handled"
            reply["manually_handled_at"] = now
            reply["manually_handled_reason"] = "user_was_last_sender"
            convo["reply"] = reply
            stats["manually_handled"] += 1
            continue

        msg_count = len(convo.get("messages", []))
        cur_fp = _inbound_context_fingerprint(convo)
        stored_fp = reply.get("context_fingerprint")
        if stored_fp and stored_fp != cur_fp:
            reply["message_count_at_generation"] = (msg_count - 1) if msg_count > 0 else -1
            reply["thread_drift_detected_at"] = now
            convo["reply"] = reply
            stats["thread_drift"] += 1
            continue

        if not stored_fp:
            reply["context_fingerprint"] = cur_fp
            convo["reply"] = reply
            stats["legacy_fp_stamped"] += 1

    return stats


def audit_draft_threads(target_urn: str | None = None) -> None:
    """Print a cross-check report for operator review (no writes)."""
    if not CLASSIFIED_FILE.exists():
        print("Error: classified inbox missing.", file=sys.stderr)
        sys.exit(1)
    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)

    rows: list[tuple[str, str, str, str, str, str, str]] = []
    for convo in data.get("conversations", []):
        if convo.get("classification", {}).get("category") != "recruiter":
            continue
        if target_urn and convo.get("conversationUrn") != target_urn:
            continue
        reply = convo.get("reply") or {}
        st = reply.get("status") or ""
        if st not in ("draft", "auto_send"):
            continue
        other = next(
            (p.get("name") for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            "?",
        )
        msgs = convo.get("messages") or []
        msg_count = len(msgs)
        try:
            at_gen = int(reply.get("message_count_at_generation") or 0)
        except (TypeError, ValueError):
            at_gen = 0
        user_last = _user_was_last_sender(convo)
        cur_fp = _inbound_context_fingerprint(convo)
        stored_fp = reply.get("context_fingerprint") or ""
        fp_ok = (not stored_fp) or (stored_fp == cur_fp)
        count_ok = msg_count == at_gen
        flags: list[str] = []
        if user_last:
            flags.append("double_reply_risk_user_last")
        if not count_ok:
            flags.append("message_count_drift")
        if stored_fp and not fp_ok:
            flags.append("inbound_tail_changed")
        if not stored_fp:
            flags.append("legacy_no_fingerprint")
        tail_bits: list[str] = []
        for m in msgs[-2:]:
            who = str(m.get("sender") or "?")[:22]
            bit = (m.get("text") or "")[:72].replace("\n", " ")
            tail_bits.append(f"[{who}] {bit}")
        tail_preview = " | ".join(tail_bits)
        rows.append((other, st, str(at_gen), str(msg_count), "ok" if fp_ok else "MISMATCH", ",".join(flags) or "clean", tail_preview))

    if not rows:
        print("No draft or auto_send replies to audit.")
        return

    print(f"Audited {len(rows)} pending reply draft(s)\n")
    hdr = f"{'name':<28} {'status':<10} {'at_gen':>6} {'now':>4} {'fp':<8} flags"
    print(hdr)
    print("-" * len(hdr))
    for name, st, ag, mc, fp, fl, tail in sorted(rows, key=lambda r: r[0].lower()):
        print(f"{name[:28]:<28} {st:<10} {ag:>6} {mc:>4} {fp:<8} {fl}")
        if tail:
            clip = tail[:220] + ("…" if len(tail) > 220 else "")
            print(f"    tail: {clip}")


async def generate_all_replies(
    target_urn: str | None = None,
    regenerate: bool = False,
) -> None:
    """Generate replies for scored recruiter conversations.

    By default, skips conversations where:
    - A reply already exists and message count hasn't changed
    - The reply was manually approved or rejected
    Use --regenerate to force all.
    """
    if not CLASSIFIED_FILE.exists():
        print("Error: Run classify_leads and score_leads first.", file=sys.stderr)
        sys.exit(1)

    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)

    templates = _load_templates()
    profile = _load_profile()

    conversations = data.get("conversations", [])
    rec_stats = reconcile_draft_threads(conversations)
    if any(rec_stats.values()):
        parts = [f"{k}={v}" for k, v in rec_stats.items() if v]
        print("  reconcile drafts: " + ", ".join(parts))
    all_recruiter = [
        c for c in conversations
        if c.get("classification", {}).get("category") == "recruiter"
        and "score" in c
        and (target_urn is None or c.get("conversationUrn") == target_urn)
    ]

    # Retroactive stale sweep: any existing draft / auto_send reply whose
    # latest inbound is older than REPLY_STALE_DAYS (and lacks an ongoing
    # engagement signal) gets abstained, even if _needs_reply would otherwise
    # skip it. Prevents the "draft has been sitting here for 3 weeks" bug.
    retroactive_stale = 0
    for convo in all_recruiter:
        reply = convo.get("reply") or {}
        if reply.get("status") not in ("draft", "auto_send"):
            continue
        if _should_abstain(convo):
            continue
        stage = derive_conversation_stage(convo)
        if not _is_stale_inbound(convo, stage):
            continue
        last_in = _last_inbound_timestamp(convo)
        age_days = (
            (datetime.now(timezone.utc) - last_in).days if last_in else None
        )
        convo["reply"] = {
            "status": "abstained",
            "tier": "abstain",
            "text": "",
            "abstain_reason": f"stale_inbound_{age_days}d" if age_days is not None else "stale_inbound",
            "intent_tag": (convo.get("intent") or {}).get("tag"),
            "intent_confidence": (convo.get("intent") or {}).get("confidence"),
            "message_count_at_generation": len(convo.get("messages", [])),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "last_inbound_at": last_in.isoformat() if last_in else None,
        }
        retroactive_stale += 1
        other = next(
            (p.get("name") for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            "?",
        )
        print(f"  retroactive abstain(stale_inbound={age_days}d): {other}")
    if retroactive_stale:
        print(f"  {retroactive_stale} existing draft(s) retroactively abstained as stale")

    recruiter_convos = [c for c in all_recruiter if _needs_reply(c, regenerate)]
    skipped = len(all_recruiter) - len(recruiter_convos)
    if skipped > 0:
        print(f"  {skipped} unchanged (skipped), {len(recruiter_convos)} to evaluate")

    # Abstain on dead-end threads before spending a generation call.
    abstain_now = [c for c in recruiter_convos if _should_abstain(c)]
    for convo in abstain_now:
        intent = convo.get("intent") or {}
        convo["reply"] = {
            "status": "abstained",
            "tier": "abstain",
            "text": "",
            "abstain_reason": intent.get("abstain_reason") or "intent.dead_end",
            "intent_tag": intent.get("tag"),
            "intent_confidence": intent.get("confidence"),
            "message_count_at_generation": len(convo.get("messages", [])),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        other = next(
            (p.get("name") for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            "?",
        )
        print(f"  abstain({intent.get('tag')}): {other}")

    # Stale-inbound abstain. Covers the case where a recruiter pinged weeks ago,
    # we never replied, and the draft has been sitting in review. Replying that
    # late reads as automated. Exemptions live inside `_is_stale_inbound`.
    stale_now: list[dict[str, Any]] = []
    for convo in recruiter_convos:
        if _should_abstain(convo):
            continue
        stage = derive_conversation_stage(convo)
        if not _is_stale_inbound(convo, stage):
            continue
        last_in = _last_inbound_timestamp(convo)
        age_days = (
            (datetime.now(timezone.utc) - last_in).days if last_in else None
        )
        intent = convo.get("intent") or {}
        convo["reply"] = {
            "status": "abstained",
            "tier": "abstain",
            "text": "",
            "abstain_reason": f"stale_inbound_{age_days}d" if age_days is not None else "stale_inbound",
            "intent_tag": intent.get("tag"),
            "intent_confidence": intent.get("confidence"),
            "message_count_at_generation": len(convo.get("messages", [])),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "last_inbound_at": last_in.isoformat() if last_in else None,
        }
        stale_now.append(convo)
        other = next(
            (p.get("name") for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            "?",
        )
        print(f"  abstain(stale_inbound={age_days}d): {other}")

    skip_urns = {
        c.get("conversationUrn") for c in abstain_now + stale_now
    }
    to_generate = [
        c for c in recruiter_convos
        if c.get("conversationUrn") not in skip_urns
    ]
    print(
        f"Generating replies for {len(to_generate)} conversations "
        f"({len(abstain_now)} dead-end, {len(stale_now)} stale)"
    )
    recruiter_convos = to_generate

    client = AsyncOpenAI()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    t0 = time.time()
    tasks = [
        generate_reply_body(client, c, profile, semaphore)
        for c in recruiter_convos
    ]
    results = await asyncio.gather(*tasks)

    for convo, result in zip(recruiter_convos, results):
        if result.get("error"):
            convo["reply"] = {"status": "error", "error": result["error"]}
            continue

        score_data = convo.get("score", {})
        total = score_data.get("total", 0)
        if total >= SCORE_AUTO_REPLY:
            tier = "high_confidence"
        elif total >= SCORE_REVIEW:
            tier = "medium_confidence"
        else:
            tier = "low_confidence"

        recruiter_name = result.get(
            "recruiter_first_name",
            _get_recruiter_first_name(convo),
        )
        dynamic_body = result.get("dynamic_body", "")

        full_reply = assemble_reply(templates, tier, dynamic_body, recruiter_name, score_data)

        safety_check = validate_outbound(full_reply)
        intent = convo.get("intent") or {}
        convo["reply"] = {
            "status": "auto_send" if tier == "high_confidence" and safety_check.is_safe else "draft",
            "tier": tier,
            "text": full_reply,
            "safety_passed": safety_check.is_safe,
            "safety_violations": safety_check.violations,
            "retrieval_query": result.get("retrieval_query"),
            "retrieval_queries": result.get("retrieval_queries", {}),
            "retrieval_debug": result.get("retrieval_debug", {}),
            "retrieved_profile_chunks": result.get("profile_hits", []),
            "retrieved_similar_messages": result.get("similar_messages", []),
            "message_count_at_generation": len(convo.get("messages", [])),
            "context_fingerprint": _inbound_context_fingerprint(convo),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "intent_tag": intent.get("tag"),
            "intent_confidence": intent.get("confidence"),
            "stage": convo.get("stage"),
            "proposed_slots": result.get("proposed_slots", []),
        }

    elapsed = time.time() - t0
    auto = sum(1 for c in recruiter_convos if c.get("reply", {}).get("status") == "auto_send")
    drafts = sum(1 for c in recruiter_convos if c.get("reply", {}).get("status") == "draft")
    errors = sum(1 for c in recruiter_convos if c.get("reply", {}).get("status") == "error")

    print(f"Generated {len(recruiter_convos)} replies in {elapsed:.1f}s")
    print(f"  {auto} auto-send, {drafts} drafts, {errors} errors")

    with open(CLASSIFIED_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Updated {CLASSIFIED_FILE}")


def _inspect_retrieval(target_urn: str | None = None) -> None:
    if not CLASSIFIED_FILE.exists():
        print("Error: Run classify_leads and score_leads first.", file=sys.stderr)
        sys.exit(1)

    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)

    conversations = [
        c for c in data.get("conversations", [])
        if c.get("classification", {}).get("category") == "recruiter"
        and (target_urn is None or c.get("conversationUrn") == target_urn)
    ]
    if target_urn and not conversations:
        print(f"No recruiter conversation found for URN: {target_urn}", file=sys.stderr)
        sys.exit(1)

    for convo in conversations[:5 if target_urn is None else len(conversations)]:
        meta = convo.get("metadata") or {}
        profile_query = _build_profile_retrieval_query(convo, meta)
        similar_query = _build_similar_message_query(convo, meta)
        profile_hits, profile_debug = _retrieve_profile_context(profile_query)
        similar_hits, similar_debug = _retrieve_similar_recruiter_messages(
            similar_query,
            convo.get("conversationUrn", ""),
        )

        other = next((p for p in convo.get("participants", []) if p.get("name") != USER_NAME), {})
        print(f"URN: {convo.get('conversationUrn', '')}")
        print(f"Recruiter: {other.get('name', 'Unknown')}")
        print(f"Role: {meta.get('role_title', 'Unknown')} @ {meta.get('company', 'Unknown')}")
        print(f"Profile query: {profile_query}")
        print(f"Similar-message query: {similar_query}")
        print(f"Profile debug: {json.dumps(profile_debug, indent=2)}")
        for hit in profile_hits:
            print(f"  PROFILE [{hit.get('chunk_type', '?')}] score={hit.get('score', 0):.4f} :: {hit.get('text', '')}")
        print(f"Similar-message debug: {json.dumps(similar_debug, indent=2)}")
        for hit in similar_hits:
            print(
                f"  SIMILAR [{hit.get('other_participant', '?')}] sender={hit.get('sender', '?')} "
                f"score={hit.get('score', 0):.4f} overlap={hit.get('overlap', 0):.2f} :: {hit.get('text', '')}"
            )
        print("-" * 100)


def purge_stale_drafts() -> int:
    """Rewrite drafts that are out of sync with the thread (same as first pass
    of ``generate_all_replies``).

    - User was last sender → ``manually_handled`` (avoid double replies).
    - Inbound tail changed vs stored fingerprint → bump generation anchor so
      the next ``generate_reply`` run regenerates.
    - Legacy drafts → stamp ``context_fingerprint`` for drift detection.

    Returns the number of conversations where the user-last-sender rule fired.
    """
    if not CLASSIFIED_FILE.exists():
        print("Error: Run classify_leads first.", file=sys.stderr)
        sys.exit(1)

    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)

    stats = reconcile_draft_threads(data.get("conversations", []))
    for k, v in stats.items():
        if v:
            print(f"  {k}: {v}")

    with open(CLASSIFIED_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    mh = stats.get("manually_handled", 0)
    print(f"Updated {CLASSIFIED_FILE} (manually_handled={mh}, see counts above).")
    return mh


def main() -> None:
    target = None
    regenerate = "--regenerate" in sys.argv
    inspect = "--inspect" in sys.argv
    purge_stale = "--purge-stale" in sys.argv
    audit = "--audit-drafts" in sys.argv
    if "--urn" in sys.argv:
        idx = sys.argv.index("--urn")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]
    if inspect:
        _inspect_retrieval(target)
        return
    if audit:
        audit_draft_threads(target_urn=target)
        return
    if purge_stale:
        purge_stale_drafts()
        return
    asyncio.run(generate_all_replies(target_urn=target, regenerate=regenerate))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Phase 2B: Lead Scoring System

Scores each classified recruiter lead (0-100) against the user profile using
LLM-based semantic matching. Produces category breakdowns and gap analysis.

Usage:
  python -m pipeline.score_leads
  python -m pipeline.score_leads --rescore
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

import yaml
from openai import AsyncOpenAI

from pipeline.config import (
    CLASSIFIED_FILE, PROFILE_FILE, GENERATION_MODEL, MAX_CONCURRENT,
    SCORE_AUTO_REPLY, SCORE_REVIEW,
)

SCORING_SYSTEM = """\
You are a job-opportunity scoring engine. Given a job seeker's profile and a \
recruiter's outreach, score how well the opportunity matches the candidate.

Score on a 0-100 scale across these categories (each 0-20):
1. skill_alignment: How well the candidate's technical skills match what's requested
2. experience_depth: Whether the candidate has done similar work at similar scale
3. role_level_fit: Is the seniority/title appropriate for the candidate
4. industry_match: Does the candidate's background align with the company's domain
5. logistics_fit: Location, remote preference, compensation alignment

Your output must be valid JSON:
{
  "total": <0-100>,
  "breakdown": {
    "skill_alignment": <0-20>,
    "experience_depth": <0-20>,
    "role_level_fit": <0-20>,
    "industry_match": <0-20>,
    "logistics_fit": <0-20>
  },
  "strengths": ["<what makes this a good match>", ...],
  "gaps": ["<skills or experience the role wants that the profile lacks>", ...],
  "reasoning": "<2-3 sentences explaining the score>",
  "profile_highlights": ["<most relevant profile items to mention in a reply>", ...]
}

Guidelines:
- A perfect 20 in a category means the candidate is an ideal fit in that dimension.
- A 0 means complete mismatch.
- Be honest about gaps. Don't inflate scores for partial matches.
- profile_highlights: Pick 2-4 specific skills, projects, or experiences from the \
profile that are most relevant to THIS specific role. These will be used to \
generate a personalized reply.
- If the recruiter didn't share enough role details, note that as a gap and \
score conservatively.
"""


def _format_profile_for_prompt(profile: dict[str, Any]) -> str:
    """Format the user profile into a concise prompt-friendly text."""
    lines: list[str] = []

    identity = profile.get("identity", {})
    lines.append(f"Name: {identity.get('name', 'Unknown')}")
    lines.append(f"Location: {identity.get('location', 'Not specified')}")
    lines.append(f"Remote preference: {identity.get('remote_preference', 'Not specified')}")

    skills = profile.get("skills", {}).get("technical", [])
    if skills:
        lines.append("\nTechnical Skills:")
        for s in skills:
            evidence = "; ".join(s.get("evidence", []))
            lines.append(f"  - {s['name']} ({s.get('proficiency', '?')}): {evidence}")

    projects = profile.get("projects", [])
    if projects:
        lines.append("\nProjects:")
        for p in projects:
            lines.append(f"  - {p['name']}: {p.get('description', '').strip()[:300]}")
            if p.get("technologies"):
                lines.append(f"    Tech: {', '.join(p['technologies'])}")

    positions = profile.get("experience", {}).get("positions", [])
    if positions:
        lines.append("\nWork Experience:")
        for pos in positions:
            lines.append(f"  - {pos['title']} at {pos['company']} ({pos.get('start_date', '?')} - {pos.get('end_date', 'present')})")
            if pos.get("description"):
                lines.append(f"    {pos['description'][:200]}")

    prefs = profile.get("preferences", {})
    if prefs.get("target_roles"):
        lines.append(f"\nTarget roles: {', '.join(prefs['target_roles'])}")
    if prefs.get("target_industries"):
        lines.append(f"Target industries: {', '.join(prefs['target_industries'])}")
    if prefs.get("excluded_industries"):
        lines.append(f"Excluded industries: {', '.join(prefs['excluded_industries'])}")
    comp = prefs.get("compensation", {})
    if comp.get("minimum"):
        lines.append(f"Compensation minimum: ${comp['minimum']:,}")
    if comp.get("target"):
        lines.append(f"Compensation target: ${comp['target']:,}")
    if prefs.get("deal_breakers"):
        lines.append(f"Deal breakers: {', '.join(prefs['deal_breakers'])}")

    return "\n".join(lines)


def _format_opportunity(convo: dict[str, Any]) -> str:
    """Format the recruiter opportunity from classified conversation."""
    meta = convo.get("metadata", {})
    participant = next(
        (p for p in convo.get("participants", []) if "recruiter" in p.get("headline", "").lower()
         or p.get("name") != convo.get("participants", [{}])[0].get("name")),
        convo.get("participants", [{}])[-1] if convo.get("participants") else {"name": "Unknown"},
    )

    lines: list[str] = []
    lines.append(f"Recruiter: {participant.get('name', 'Unknown')} — {participant.get('headline', '')}")

    if meta:
        if meta.get("role_title"):
            lines.append(f"Role: {meta['role_title']}")
        if meta.get("company"):
            lines.append(f"Company: {meta['company']}")
        if meta.get("industry"):
            lines.append(f"Industry: {meta['industry']}")
        if meta.get("compensation_hints"):
            lines.append(f"Compensation: {meta['compensation_hints']}")
        if meta.get("location"):
            lines.append(f"Location: {meta['location']}")
        if meta.get("role_description_summary"):
            lines.append(f"Description: {meta['role_description_summary']}")
        if meta.get("skills_requested"):
            lines.append(f"Skills requested: {', '.join(meta['skills_requested'])}")

    lines.append("\nConversation messages:")
    for msg in convo.get("messages", [])[:10]:
        sender = msg.get("sender", "Unknown")
        text = msg.get("text", "")[:400]
        lines.append(f"  [{sender}]: {text}")

    return "\n".join(lines)


async def score_conversation(
    client: AsyncOpenAI,
    convo: dict[str, Any],
    profile_text: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Score a single conversation against the profile."""
    async with semaphore:
        opportunity_text = _format_opportunity(convo)
        user_prompt = (
            f"Score this opportunity against the candidate's profile.\n\n"
            f"## Candidate Profile\n{profile_text}\n\n"
            f"## Opportunity\n{opportunity_text}"
        )
        try:
            resp = await client.chat.completions.create(
                model=GENERATION_MODEL,
                messages=[
                    {"role": "system", "content": SCORING_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)
            result.setdefault("total", 0)
            result.setdefault("breakdown", {})
            result.setdefault("strengths", [])
            result.setdefault("gaps", [])
            result.setdefault("reasoning", "")
            result.setdefault("profile_highlights", [])

            # Determine action tier
            total = result["total"]
            if total >= SCORE_AUTO_REPLY:
                result["action"] = "auto_reply"
            elif total >= SCORE_REVIEW:
                result["action"] = "review"
            else:
                result["action"] = "notify_gaps"

            return result
        except Exception as e:
            print(f"  ERROR scoring: {e}", file=sys.stderr)
            return {
                "total": 0,
                "breakdown": {},
                "strengths": [],
                "gaps": [f"Scoring failed: {e}"],
                "reasoning": f"Scoring failed: {e}",
                "profile_highlights": [],
                "action": "notify_gaps",
            }


async def score_all(rescore: bool = False) -> None:
    """Score all recruiter conversations."""
    if not CLASSIFIED_FILE.exists():
        print("Error: Run classify_leads first.", file=sys.stderr)
        sys.exit(1)
    if not PROFILE_FILE.exists():
        print("Error: User profile not found.", file=sys.stderr)
        sys.exit(1)

    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)
    with open(PROFILE_FILE) as f:
        profile = yaml.safe_load(f)

    profile_text = _format_profile_for_prompt(profile)
    conversations = data.get("conversations", [])

    recruiter_convos = [
        c for c in conversations
        if c.get("classification", {}).get("category") == "recruiter"
    ]
    if not rescore:
        recruiter_convos = [c for c in recruiter_convos if "score" not in c]

    print(f"Scoring {len(recruiter_convos)} recruiter conversations")

    client = AsyncOpenAI()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    t0 = time.time()
    tasks = [
        score_conversation(client, c, profile_text, semaphore)
        for c in recruiter_convos
    ]
    scores = await asyncio.gather(*tasks)

    for convo, score in zip(recruiter_convos, scores):
        score["scored_at"] = datetime.now(timezone.utc).isoformat()
        convo["score"] = score

    elapsed = time.time() - t0

    auto_count = sum(1 for s in scores if s.get("action") == "auto_reply")
    review_count = sum(1 for s in scores if s.get("action") == "review")
    gap_count = sum(1 for s in scores if s.get("action") == "notify_gaps")

    print(f"Scored {len(recruiter_convos)} conversations in {elapsed:.1f}s")
    print(f"  {auto_count} auto-reply (>={SCORE_AUTO_REPLY})")
    print(f"  {review_count} review ({SCORE_REVIEW}-{SCORE_AUTO_REPLY - 1})")
    print(f"  {gap_count} notify gaps (<{SCORE_REVIEW})")

    with open(CLASSIFIED_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Updated {CLASSIFIED_FILE}")


def main() -> None:
    rescore = "--rescore" in sys.argv
    asyncio.run(score_all(rescore=rescore))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Phase 2C: Lead Priority Ordering

Port of priority-forge's weighted heuristic scoring to Python, adapted for
lead prioritization. Lower score = higher priority (min-heap semantics).

Usage:
  python -m pipeline.lead_priority
"""

from __future__ import annotations

import heapq
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pipeline.config import CLASSIFIED_FILE


@dataclass
class HeuristicWeights:
    blocking: float = 10.0
    cross_project: float = 5.0
    time_sensitive: float = 8.0
    effort_value: float = 3.0
    match_score: float = 6.0


DEFAULT_WEIGHTS = HeuristicWeights()

PRIORITY_BASE: dict[str, int] = {
    "auto_reply": 0,
    "review": 100,
    "notify_gaps": 200,
    "unscored": 300,
}

URGENCY_SCORES: dict[str, int] = {
    "high": 10,
    "medium": 5,
    "low": 2,
}

RECRUITER_TYPE_EFFORT: dict[str, int] = {
    "hiring_manager": 9,
    "in_house": 6,
    "agency": 3,
}


@dataclass(order=True)
class PrioritizedLead:
    priority_score: float
    conversation_urn: str = field(compare=False)
    name: str = field(compare=False)
    action: str = field(compare=False)
    match_score: int = field(compare=False, default=0)
    urgency: str = field(compare=False, default="medium")


def calculate_lead_weights(convo: dict[str, Any]) -> dict[str, float]:
    """Calculate heuristic weights for a single lead."""
    score_data = convo.get("score", {})
    meta = convo.get("metadata", {})
    classification = convo.get("classification", {})

    # Time sensitivity: based on urgency + how recently they messaged
    urgency = meta.get("urgency", "medium")
    time_sensitivity = URGENCY_SCORES.get(urgency, 5)

    last_activity = convo.get("lastActivityAt", "")
    if last_activity:
        try:
            last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
            if days_since < 1:
                time_sensitivity = min(time_sensitivity + 3, 10)
            elif days_since < 3:
                time_sensitivity = min(time_sensitivity + 1, 10)
        except ValueError:
            pass

    # Effort/value: hiring managers > in-house > agency (direct paths are faster)
    recruiter_type = meta.get("recruiter_type", "agency")
    effort_value = RECRUITER_TYPE_EFFORT.get(recruiter_type, 3)

    # Match score contribution (0-100 scaled to 0-10)
    match_contribution = score_data.get("total", 0) / 10.0

    # Blocking: are we the bottleneck? Check next_action_needed
    blocking = 0
    next_action = meta.get("next_action_needed", "")
    if next_action and "reply" in next_action.lower():
        blocking = 5
    if next_action and ("schedule" in next_action.lower() or "call" in next_action.lower()):
        blocking = 7

    return {
        "blocking": blocking,
        "time_sensitivity": time_sensitivity,
        "effort_value": effort_value,
        "match_contribution": match_contribution,
        "cross_project": 0,
    }


def calculate_priority_score(
    weights: dict[str, float],
    action: str,
    hw: HeuristicWeights = DEFAULT_WEIGHTS,
) -> float:
    """Calculate final priority score. Lower = higher priority."""
    base = PRIORITY_BASE.get(action, 300)
    adjustment = -(
        hw.blocking * weights["blocking"]
        + hw.time_sensitive * weights["time_sensitivity"]
        + hw.effort_value * weights["effort_value"]
        + hw.match_score * weights["match_contribution"]
        + hw.cross_project * weights["cross_project"]
    )
    return base + adjustment


def prioritize_leads(
    conversations: list[dict[str, Any]],
    hw: HeuristicWeights = DEFAULT_WEIGHTS,
) -> list[PrioritizedLead]:
    """Build a priority-ordered list of leads."""
    leads: list[PrioritizedLead] = []

    for convo in conversations:
        clf = convo.get("classification", {})
        if clf.get("category") != "recruiter":
            continue

        score_data = convo.get("score", {})
        action = score_data.get("action", "unscored")
        meta = convo.get("metadata", {})

        other = next(
            (p for p in convo.get("participants", [])
             if not p.get("name", "").startswith("Nicholas")),
            {"name": "Unknown"},
        )

        weights = calculate_lead_weights(convo)
        priority_score = calculate_priority_score(weights, action, hw)

        leads.append(PrioritizedLead(
            priority_score=priority_score,
            conversation_urn=convo.get("conversationUrn", ""),
            name=other.get("name", "Unknown"),
            action=action,
            match_score=score_data.get("total", 0),
            urgency=meta.get("urgency", "medium"),
        ))

    heapq.heapify(leads)
    return leads


def print_priority_queue(leads: list[PrioritizedLead]) -> None:
    """Display the priority-ordered lead queue."""
    sorted_leads = sorted(leads)
    print(f"\n{'#':>3}  {'Score':>6}  {'Match':>5}  {'Urgency':<8}  {'Action':<12}  Name")
    print("-" * 70)
    for i, lead in enumerate(sorted_leads, 1):
        print(
            f"{i:>3}  {lead.priority_score:>6.1f}  "
            f"{lead.match_score:>5}  {lead.urgency:<8}  "
            f"{lead.action:<12}  {lead.name}"
        )


def main() -> None:
    if not CLASSIFIED_FILE.exists():
        print("Error: Run classify_leads and score_leads first.", file=sys.stderr)
        sys.exit(1)

    with open(CLASSIFIED_FILE) as f:
        data = json.load(f)

    leads = prioritize_leads(data.get("conversations", []))
    print(f"Prioritized {len(leads)} recruiter leads")
    print_priority_queue(leads)


if __name__ == "__main__":
    main()

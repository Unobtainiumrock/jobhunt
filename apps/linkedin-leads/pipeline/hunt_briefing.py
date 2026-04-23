#!/usr/bin/env python3
"""
Phase 3A: Canonical Hunt Briefing

Builds a daily briefing from canonical entity records rather than only from the
raw recruiter pipeline output.

Usage:
  python -m pipeline.hunt_briefing
  python -m pipeline.hunt_briefing --json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import (
    APPLICATIONS_DIR,
    INTERVIEW_LOOPS_DIR,
    LEADS_DIR,
    OPPORTUNITIES_DIR,
    PREP_ARTIFACTS_DIR,
    TASKS_DIR,
)
from pipeline.prep_packets import build_stage_prep_packet

SECTION_DIVIDER = "=" * 60


def _load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def _primary_stage(loop: dict[str, Any]) -> dict[str, Any] | None:
    stages = loop.get("stages", [])
    return stages[0] if stages else None


def build_hunt_briefing() -> dict[str, Any]:
    opportunities = _load_records(OPPORTUNITIES_DIR)
    applications = _load_records(APPLICATIONS_DIR)
    leads = {record["id"]: record for record in _load_records(LEADS_DIR)}
    tasks = _load_records(TASKS_DIR)
    interview_loops = {record["id"]: record for record in _load_records(INTERVIEW_LOOPS_DIR)}
    prep_artifacts = {record["id"]: record for record in _load_records(PREP_ARTIFACTS_DIR)}

    active_interviews = [record for record in interview_loops.values() if record.get("status") == "active"]
    pending_tasks = [record for record in tasks if record.get("status") != "complete"]
    scheduled_interviews = []
    for loop in active_interviews:
        opportunity = next((item for item in opportunities if item["id"] == loop["opportunity_id"]), None)
        if not opportunity:
            continue
        for stage in loop.get("stages", []):
            if stage.get("status") != "scheduled":
                continue
            stage_artifacts = [
                prep_artifacts[artifact_id]
                for artifact_id in stage.get("prep_artifact_ids", opportunity.get("prep_artifact_ids", []))
                if artifact_id in prep_artifacts
            ]
            scheduled_interviews.append({
                "company": opportunity["company"],
                "role_title": opportunity["role_title"],
                "stage_kind": stage.get("kind"),
                "scheduled_at": stage.get("scheduled_at"),
                "interviewers": stage.get("interviewer_names", []),
                "next_step": loop.get("next_step"),
                "prep_packet": build_stage_prep_packet(stage, opportunity, stage_artifacts),
            })
    scheduled_interviews.sort(key=lambda item: item.get("scheduled_at") or "")
    top_opportunities = sorted(
        opportunities,
        key=lambda record: (
            record.get("status") != "interviewing",
            -(record.get("fit_score") or 0),
            record.get("company", ""),
        ),
    )[:10]

    interview_packets = []
    for loop in active_interviews[:10]:
        opportunity = next((item for item in opportunities if item["id"] == loop["opportunity_id"]), None)
        if not opportunity:
            continue
        stage_packets = []
        for stage in loop.get("stages", []):
            stage_artifacts = [
                prep_artifacts[artifact_id]
                for artifact_id in stage.get("prep_artifact_ids", opportunity.get("prep_artifact_ids", []))
                if artifact_id in prep_artifacts
            ]
            stage_packets.append(build_stage_prep_packet(stage, opportunity, stage_artifacts))
        interview_packets.append({
            "company": opportunity["company"],
            "role_title": opportunity["role_title"],
            "next_step": loop.get("next_step"),
            "prep": [title for packet in stage_packets for title in packet["artifact_titles"]],
            "stage_packets": stage_packets,
        })

    follow_up_tasks = [task for task in pending_tasks if task.get("kind") == "follow_up"]
    prep_tasks = [task for task in pending_tasks if task.get("kind") in {"interview_prep", "study_session"}]
    debrief_tasks = [task for task in pending_tasks if task.get("kind") == "admin" and task.get("interview_loop_id")]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "opportunities": len(opportunities),
            "applications": len(applications),
            "active_interviews": len(active_interviews),
            "scheduled_interviews": len(scheduled_interviews),
            "pending_tasks": len(pending_tasks),
            "follow_up_tasks": len(follow_up_tasks),
            "prep_tasks": len(prep_tasks),
            "debrief_tasks": len(debrief_tasks),
        },
        "top_opportunities": [
            {
                "company": record["company"],
                "role_title": record["role_title"],
                "status": record["status"],
                "fit_score": record.get("fit_score"),
                "lead_name": leads.get(record["lead_ids"][0], {}).get("name") if record.get("lead_ids") else None,
                "next_action": record.get("next_action"),
            }
            for record in top_opportunities
        ],
        "scheduled_interviews": scheduled_interviews[:10],
        "interview_packets": interview_packets,
        "follow_up_tasks": [
            {
                "title": task["title"],
                "company": next(
                    (record["company"] for record in opportunities if record["id"] == task.get("opportunity_id")),
                    None,
                ),
                "notes": task.get("notes"),
            }
            for task in follow_up_tasks[:10]
        ],
        "applications": [
            {
                "company": next(
                    (record["company"] for record in opportunities if record["id"] == application["opportunity_id"]),
                    None,
                ),
                "status": application["status"],
                "notes": application.get("notes"),
            }
            for application in applications[:10]
        ],
        "debrief_tasks": [
            {
                "title": task["title"],
                "company": next(
                    (record["company"] for record in opportunities if record["id"] == task.get("opportunity_id")),
                    None,
                ),
                "notes": task.get("notes"),
            }
            for task in debrief_tasks[:10]
        ],
        "prep_tasks": [
            {
                "title": task["title"],
                "kind": task["kind"],
                "company": next(
                    (record["company"] for record in opportunities if record["id"] == task.get("opportunity_id")),
                    None,
                ),
                "interview_stage_id": task.get("interview_stage_id"),
            }
            for task in prep_tasks[:10]
        ],
    }


def print_hunt_briefing(briefing: dict[str, Any]) -> None:
    summary = briefing["summary"]

    print(f"\n{SECTION_DIVIDER}")
    print("  TODAY'S HUNT")
    print(SECTION_DIVIDER)
    print(
        f"\n  {summary['opportunities']} opportunities | "
        f"{summary['applications']} applications | "
        f"{summary['active_interviews']} active interviews | "
        f"{summary['scheduled_interviews']} scheduled interviews | "
        f"{summary['pending_tasks']} pending tasks"
    )

    print("\n--- TOP OPPORTUNITIES ---")
    for item in briefing["top_opportunities"][:5]:
        print(
            f"  {item['company']} — {item['role_title']} "
            f"[{item['status']}] fit={item['fit_score']}"
        )

    print("\n--- INTERVIEW PACKETS ---")
    if briefing["interview_packets"]:
        for item in briefing["interview_packets"][:5]:
            prep = ", ".join(sorted(set(item["prep"]))) if item["prep"] else "No attached prep yet"
            print(f"  {item['company']} — next: {item['next_step'] or 'TBD'}")
            print(f"    prep: {prep}")
            for packet in item.get("stage_packets", [])[:3]:
                focus = ", ".join(packet.get("focus_topics", [])[:3]) or "no focus topics yet"
                print(f"    stage {packet['stage_kind']}: {focus}")
    else:
        print("  No active interview packets.")

    print("\n--- SCHEDULED INTERVIEWS ---")
    if briefing["scheduled_interviews"]:
        for item in briefing["scheduled_interviews"][:5]:
            print(
                f"  {item['company']} — {item['role_title']} "
                f"[{item['stage_kind']}] at {item['scheduled_at'] or 'TBD'}"
            )
            packet = item.get("prep_packet") or {}
            if packet.get("suggested_actions"):
                print(f"    action: {packet['suggested_actions'][0]}")
    else:
        print("  No scheduled interviews.")

    print("\n--- FOLLOW-UPS ---")
    if briefing["follow_up_tasks"]:
        for item in briefing["follow_up_tasks"][:5]:
            print(f"  {item['company'] or '?'} — {item['title']}")
    else:
        print("  No follow-up tasks.")

    print("\n--- APPLICATIONS ---")
    if briefing["applications"]:
        for item in briefing["applications"][:5]:
            print(f"  {item['company'] or '?'} — {item['status']}")
    else:
        print("  No application records.")

    print("\n--- DEBRIEFS ---")
    if briefing["debrief_tasks"]:
        for item in briefing["debrief_tasks"][:5]:
            print(f"  {item['company'] or '?'} — {item['title']}")
    else:
        print("  No debrief tasks.")

    print("\n--- PREP TASKS ---")
    if briefing["prep_tasks"]:
        for item in briefing["prep_tasks"][:5]:
            suffix = f" ({item['interview_stage_id']})" if item.get("interview_stage_id") else ""
            print(f"  {item['company'] or '?'} — {item['title']}{suffix}")
    else:
        print("  No prep tasks.")

    print(f"\n{SECTION_DIVIDER}\n")


def main() -> None:
    briefing = build_hunt_briefing()
    if "--json" in sys.argv:
        print(json.dumps(briefing, indent=2))
    else:
        print_hunt_briefing(briefing)


if __name__ == "__main__":
    main()

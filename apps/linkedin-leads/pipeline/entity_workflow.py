#!/usr/bin/env python3
"""
Canonical workflow state writer.

Stores durable operational state that should survive entity resyncs.

Usage:
  python -m pipeline.entity_workflow show
  python -m pipeline.entity_workflow list-applications [--query TEXT]
  python -m pipeline.entity_workflow list-interviews [--query TEXT]
  python -m pipeline.entity_workflow lookup TEXT
  python -m pipeline.entity_workflow mark-application-submitted APP_ID [--application-url URL]
  python -m pipeline.entity_workflow set-application-status APP_ID STATUS
  python -m pipeline.entity_workflow set-interview-loop-status LOOP_ID STATUS
  python -m pipeline.entity_workflow set-interview-stage LOOP_ID STAGE_ID STATUS
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import (
    APPLICATIONS_DIR,
    INTERVIEW_LOOPS_DIR,
    OPPORTUNITIES_DIR,
    TASKS_DIR,
    WORKFLOW_STATE_FILE,
)

APPLICATION_STATUSES = {
    "planned",
    "drafting",
    "submitted",
    "screening",
    "interviewing",
    "rejected",
    "withdrawn",
    "offer",
}

INTERVIEW_LOOP_STATUSES = {
    "planned",
    "active",
    "completed",
    "offer",
    "rejected",
    "withdrawn",
}

INTERVIEW_STAGE_STATUSES = {
    "planned",
    "scheduled",
    "completed",
    "cancelled",
}

INTERVIEW_STAGE_KINDS = {
    "recruiter_screen",
    "hiring_manager",
    "technical",
    "system_design",
    "behavioral",
    "onsite",
    "take_home",
    "final_round",
    "other",
}

TASK_STATUSES = {
    "not_started",
    "in_progress",
    "waiting",
    "blocked",
    "complete",
    "cancelled",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "applications": {},
        "interview_loops": {},
        "tasks": {},
    }


def load_workflow_state() -> dict[str, Any]:
    if not WORKFLOW_STATE_FILE.exists():
        return _default_state()
    with open(WORKFLOW_STATE_FILE) as f:
        payload = json.load(f)
    payload.setdefault("applications", {})
    payload.setdefault("interview_loops", {})
    payload.setdefault("tasks", {})
    return payload


def save_workflow_state(payload: dict[str, Any]) -> None:
    WORKFLOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WORKFLOW_STATE_FILE, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def _load_record(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [_load_record(path) for path in sorted(directory.glob("*.json"))]


def _require_record(directory: Path, record_id: str, label: str) -> dict[str, Any]:
    path = directory / f"{record_id}.json"
    if not path.exists():
        raise SystemExit(f"Unknown {label}: {record_id}")
    return _load_record(path)


def _resolve_application_id(application_id: str | None, query: str | None) -> str:
    if application_id:
        _require_record(APPLICATIONS_DIR, application_id, "application")
        return application_id
    if not query:
        raise SystemExit("Provide an application ID or --query")

    matches = _list_applications(argparse.Namespace(query=query))["applications"]
    if not matches:
        raise SystemExit(f"No application matches query: {query}")
    if len(matches) > 1:
        formatted = "\n".join(
            f"- {item['application_id']} :: {item['company']} / {item['role_title']}"
            for item in matches
        )
        raise SystemExit(f"Application query is ambiguous:\n{formatted}")
    return matches[0]["application_id"]


def _resolve_loop(loop_id: str | None, query: str | None) -> dict[str, Any]:
    if loop_id:
        return _require_record(INTERVIEW_LOOPS_DIR, loop_id, "interview loop")
    if not query:
        raise SystemExit("Provide an interview loop ID or --query")

    matches = _list_interviews(argparse.Namespace(query=query))["interview_loops"]
    if not matches:
        raise SystemExit(f"No interview loop matches query: {query}")
    if len(matches) > 1:
        formatted = "\n".join(
            f"- {item['loop_id']} :: {item['company']} / {item['role_title']}"
            for item in matches
        )
        raise SystemExit(f"Interview loop query is ambiguous:\n{formatted}")
    return _require_record(INTERVIEW_LOOPS_DIR, matches[0]["loop_id"], "interview loop")


def _resolve_stage(loop: dict[str, Any], stage_id: str | None) -> dict[str, Any]:
    if stage_id:
        stage = next((item for item in loop.get("stages", []) if item.get("id") == stage_id), None)
        if not stage:
            raise SystemExit(f"Unknown stage {stage_id} for loop {loop['id']}")
        return stage

    stages = loop.get("stages", [])
    if len(stages) == 1:
        return stages[0]
    formatted = "\n".join(
        f"- {item.get('id')} :: {item.get('kind')} [{item.get('status')}]"
        for item in stages
    )
    raise SystemExit(f"Multiple stages found. Provide STAGE_ID explicitly:\n{formatted}")


def _resolve_task_id(task_id: str | None, query: str | None) -> str:
    if task_id:
        _require_record(TASKS_DIR, task_id, "task")
        return task_id
    if not query:
        raise SystemExit("Provide a task ID or --query")

    matches = _list_tasks(argparse.Namespace(query=query))["tasks"]
    if not matches:
        raise SystemExit(f"No task matches query: {query}")
    if len(matches) > 1:
        formatted = "\n".join(
            f"- {item['task_id']} :: {item['title']} [{item['status']}]"
            for item in matches
        )
        raise SystemExit(f"Task query is ambiguous:\n{formatted}")
    return matches[0]["task_id"]


def _update_application_state(args: argparse.Namespace) -> dict[str, Any]:
    application_id = _resolve_application_id(args.application_id, args.query)
    if args.status not in APPLICATION_STATUSES:
        raise SystemExit(f"Invalid application status: {args.status}")

    payload = load_workflow_state()
    applications = payload["applications"]
    current = applications.get(application_id, {})
    updated = dict(current)

    updated["status"] = args.status
    if args.submitted_at is not None:
        updated["submitted_at"] = args.submitted_at
    elif args.status in {"submitted", "screening", "interviewing", "offer"} and not current.get("submitted_at"):
        updated["submitted_at"] = _now_iso()

    if args.application_url is not None:
        updated["application_url"] = args.application_url
    if args.resume_variant is not None:
        updated["resume_variant"] = args.resume_variant
    if args.cover_letter_variant is not None:
        updated["cover_letter_variant"] = args.cover_letter_variant
    if args.notes is not None:
        updated["notes"] = args.notes
    if args.deadline_at is not None:
        updated["deadline_at"] = args.deadline_at

    updated["updated_at"] = _now_iso()
    applications[application_id] = updated
    save_workflow_state(payload)
    return {
        "application_id": application_id,
        "state": updated,
        "workflow_state_file": str(WORKFLOW_STATE_FILE),
    }


def _update_interview_loop_state(args: argparse.Namespace) -> dict[str, Any]:
    loop = _resolve_loop(args.loop_id, args.query)
    if args.status not in INTERVIEW_LOOP_STATUSES:
        raise SystemExit(f"Invalid interview loop status: {args.status}")

    payload = load_workflow_state()
    loops = payload["interview_loops"]
    current = loops.get(loop["id"], {"stages": {}})
    updated = dict(current)
    updated.setdefault("stages", {})
    updated["status"] = args.status
    if args.next_step is not None:
        updated["next_step"] = args.next_step
    if args.debrief_summary is not None:
        updated["debrief_summary"] = args.debrief_summary
    updated["updated_at"] = _now_iso()
    loops[loop["id"]] = updated
    save_workflow_state(payload)
    return {
        "loop_id": loop["id"],
        "state": updated,
        "workflow_state_file": str(WORKFLOW_STATE_FILE),
    }


def _update_interview_stage_state(args: argparse.Namespace) -> dict[str, Any]:
    loop = _resolve_loop(args.loop_id, args.query)
    stage = _resolve_stage(loop, args.stage_id)
    if args.status not in INTERVIEW_STAGE_STATUSES:
        raise SystemExit(f"Invalid interview stage status: {args.status}")

    payload = load_workflow_state()
    loops = payload["interview_loops"]
    current = loops.get(loop["id"], {"stages": {}})
    updated = dict(current)
    updated.setdefault("stages", {})

    stage_state = dict(updated["stages"].get(stage["id"], {}))
    stage_state["status"] = args.status
    if args.scheduled_at is not None:
        stage_state["scheduled_at"] = args.scheduled_at
    if args.duration_minutes is not None:
        stage_state["duration_minutes"] = args.duration_minutes
    if args.interviewer_names is not None:
        stage_state["interviewer_names"] = [name.strip() for name in args.interviewer_names.split(",") if name.strip()]
    if args.debrief is not None:
        stage_state["debrief"] = args.debrief

    stage_state["updated_at"] = _now_iso()
    updated["stages"][stage["id"]] = stage_state

    if args.loop_status is not None:
        if args.loop_status not in INTERVIEW_LOOP_STATUSES:
            raise SystemExit(f"Invalid interview loop status: {args.loop_status}")
        updated["status"] = args.loop_status
    elif args.status == "completed" and updated.get("status") == "planned":
        updated["status"] = "active"

    if args.next_step is not None:
        updated["next_step"] = args.next_step
    updated["updated_at"] = _now_iso()
    loops[loop["id"]] = updated
    save_workflow_state(payload)
    return {
        "loop_id": loop["id"],
        "stage_id": stage["id"],
        "state": stage_state,
        "loop_state": updated,
        "workflow_state_file": str(WORKFLOW_STATE_FILE),
    }


def _add_interview_stage(args: argparse.Namespace) -> dict[str, Any]:
    loop = _resolve_loop(args.loop_id, args.query)
    if args.kind not in INTERVIEW_STAGE_KINDS:
        raise SystemExit(f"Invalid interview stage kind: {args.kind}")

    payload = load_workflow_state()
    loops = payload["interview_loops"]
    current = loops.get(loop["id"], {"stages": {}})
    updated = dict(current)
    updated.setdefault("stages", {})

    if args.stage_id:
        stage_id = args.stage_id
    else:
        stage_id = f"stage_{loop['id']}_{args.kind}_{len(updated['stages']) + len(loop.get('stages', [])) + 1}"

    if stage_id in updated["stages"] or any(stage.get("id") == stage_id for stage in loop.get("stages", [])):
        raise SystemExit(f"Interview stage already exists: {stage_id}")

    stage_state = {
        "kind": args.kind,
        "status": args.status or "planned",
        "scheduled_at": args.scheduled_at or None,
        "duration_minutes": args.duration_minutes,
        "interviewer_names": [name.strip() for name in (args.interviewer_names or "").split(",") if name.strip()],
        "debrief": args.debrief or None,
        "updated_at": _now_iso(),
    }
    updated["stages"][stage_id] = stage_state

    stage_order = list(updated.get("stage_order") or [stage.get("id") for stage in loop.get("stages", [])])
    if args.after_stage_id:
        if args.after_stage_id not in stage_order:
            raise SystemExit(f"Unknown after-stage ID: {args.after_stage_id}")
        insert_at = stage_order.index(args.after_stage_id) + 1
        stage_order.insert(insert_at, stage_id)
    else:
        stage_order.append(stage_id)
    updated["stage_order"] = stage_order

    if args.next_step is not None:
        updated["next_step"] = args.next_step
    updated["updated_at"] = _now_iso()
    loops[loop["id"]] = updated
    save_workflow_state(payload)
    return {
        "loop_id": loop["id"],
        "stage_id": stage_id,
        "state": stage_state,
        "loop_state": updated,
        "workflow_state_file": str(WORKFLOW_STATE_FILE),
    }


def _update_task_state(args: argparse.Namespace) -> dict[str, Any]:
    task_id = _resolve_task_id(args.task_id, args.query)
    if args.status not in TASK_STATUSES:
        raise SystemExit(f"Invalid task status: {args.status}")

    payload = load_workflow_state()
    tasks = payload["tasks"]
    current = tasks.get(task_id, {})
    updated = dict(current)
    updated["status"] = args.status
    if args.notes is not None:
        updated["notes"] = args.notes
    if args.due_at is not None:
        updated["due_at"] = args.due_at
    updated["updated_at"] = _now_iso()
    tasks[task_id] = updated
    save_workflow_state(payload)
    return {
        "task_id": task_id,
        "state": updated,
        "workflow_state_file": str(WORKFLOW_STATE_FILE),
    }


def _show_state(_: argparse.Namespace) -> dict[str, Any]:
    return load_workflow_state()


def _matches_query(*values: str | None, query: str | None) -> bool:
    if not query:
        return True
    lowered = query.lower()
    return any(lowered in (value or "").lower() for value in values)


def _list_applications(args: argparse.Namespace) -> dict[str, Any]:
    opportunities = {record["id"]: record for record in _load_records(OPPORTUNITIES_DIR)}
    applications = _load_records(APPLICATIONS_DIR)
    items = []
    for application in applications:
        opportunity = opportunities.get(application.get("opportunity_id"), {})
        if not _matches_query(
            application.get("id"),
            opportunity.get("company"),
            opportunity.get("role_title"),
            query=args.query,
        ):
            continue
        items.append({
            "application_id": application["id"],
            "company": opportunity.get("company"),
            "role_title": opportunity.get("role_title"),
            "status": application.get("status"),
            "submitted_at": application.get("submitted_at"),
            "application_url": application.get("application_url"),
            "opportunity_id": application.get("opportunity_id"),
        })
    return {"applications": items}


def _list_interviews(args: argparse.Namespace) -> dict[str, Any]:
    opportunities = {record["id"]: record for record in _load_records(OPPORTUNITIES_DIR)}
    loops = _load_records(INTERVIEW_LOOPS_DIR)
    items = []
    for loop in loops:
        opportunity = opportunities.get(loop.get("opportunity_id"), {})
        primary_stage = (loop.get("stages") or [{}])[0]
        if not _matches_query(
            loop.get("id"),
            opportunity.get("company"),
            opportunity.get("role_title"),
            primary_stage.get("id"),
            query=args.query,
        ):
            continue
        items.append({
            "loop_id": loop["id"],
            "company": opportunity.get("company"),
            "role_title": opportunity.get("role_title"),
            "loop_status": loop.get("status"),
            "next_step": loop.get("next_step"),
            "primary_stage": {
                "stage_id": primary_stage.get("id"),
                "kind": primary_stage.get("kind"),
                "status": primary_stage.get("status"),
                "scheduled_at": primary_stage.get("scheduled_at"),
            },
            "stages": [
                {
                    "stage_id": stage.get("id"),
                    "kind": stage.get("kind"),
                    "status": stage.get("status"),
                    "scheduled_at": stage.get("scheduled_at"),
                    "interviewer_names": stage.get("interviewer_names", []),
                }
                for stage in loop.get("stages", [])
            ],
        })
    return {"interview_loops": items}


def _list_tasks(args: argparse.Namespace) -> dict[str, Any]:
    opportunities = {record["id"]: record for record in _load_records(OPPORTUNITIES_DIR)}
    tasks = _load_records(TASKS_DIR)
    items = []
    for task in tasks:
        opportunity = opportunities.get(task.get("opportunity_id"), {})
        if not _matches_query(
            task.get("id"),
            task.get("title"),
            opportunity.get("company"),
            opportunity.get("role_title"),
            query=args.query,
        ):
            continue
        items.append({
            "task_id": task["id"],
            "title": task.get("title"),
            "kind": task.get("kind"),
            "status": task.get("status"),
            "due_at": task.get("due_at"),
            "company": opportunity.get("company"),
            "role_title": opportunity.get("role_title"),
            "opportunity_id": task.get("opportunity_id"),
            "interview_loop_id": task.get("interview_loop_id"),
            "interview_stage_id": task.get("interview_stage_id"),
        })
    return {"tasks": items}


def _lookup_entities(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "query": args.query,
        "applications": _list_applications(args)["applications"],
        "interview_loops": _list_interviews(args)["interview_loops"],
        "tasks": _list_tasks(args)["tasks"],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage durable canonical workflow state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="Print the current workflow state overlay")
    show_parser.set_defaults(handler=_show_state)

    list_app_parser = subparsers.add_parser(
        "list-applications",
        help="List canonical applications with company and role context",
    )
    list_app_parser.add_argument("--query")
    list_app_parser.set_defaults(handler=_list_applications)

    list_interviews_parser = subparsers.add_parser(
        "list-interviews",
        help="List canonical interview loops with company, role, and stage context",
    )
    list_interviews_parser.add_argument("--query")
    list_interviews_parser.set_defaults(handler=_list_interviews)

    list_tasks_parser = subparsers.add_parser(
        "list-tasks",
        help="List canonical tasks with company and role context",
    )
    list_tasks_parser.add_argument("--query")
    list_tasks_parser.set_defaults(handler=_list_tasks)

    lookup_parser = subparsers.add_parser(
        "lookup",
        help="Search applications and interviews by company, role, or ID fragment",
    )
    lookup_parser.add_argument("query")
    lookup_parser.set_defaults(handler=_lookup_entities)

    mark_app_parser = subparsers.add_parser(
        "mark-application-submitted",
        help="Mark an application as submitted in durable workflow state",
    )
    mark_app_parser.add_argument("application_id", nargs="?")
    mark_app_parser.add_argument("--query")
    mark_app_parser.add_argument("--submitted-at")
    mark_app_parser.add_argument("--application-url")
    mark_app_parser.add_argument("--resume-variant")
    mark_app_parser.add_argument("--cover-letter-variant")
    mark_app_parser.add_argument("--deadline-at")
    mark_app_parser.add_argument("--notes")
    mark_app_parser.set_defaults(
        handler=_update_application_state,
        status="submitted",
    )

    set_app_parser = subparsers.add_parser(
        "set-application-status",
        help="Set an application status in durable workflow state",
    )
    set_app_parser.add_argument("application_id", nargs="?")
    set_app_parser.add_argument("--query")
    set_app_parser.add_argument("status")
    set_app_parser.add_argument("--submitted-at")
    set_app_parser.add_argument("--application-url")
    set_app_parser.add_argument("--resume-variant")
    set_app_parser.add_argument("--cover-letter-variant")
    set_app_parser.add_argument("--deadline-at")
    set_app_parser.add_argument("--notes")
    set_app_parser.set_defaults(handler=_update_application_state)

    set_loop_parser = subparsers.add_parser(
        "set-interview-loop-status",
        help="Set an interview loop status in durable workflow state",
    )
    set_loop_parser.add_argument("loop_id", nargs="?")
    set_loop_parser.add_argument("--query")
    set_loop_parser.add_argument("status")
    set_loop_parser.add_argument("--next-step")
    set_loop_parser.add_argument("--debrief-summary")
    set_loop_parser.set_defaults(handler=_update_interview_loop_state)

    set_stage_parser = subparsers.add_parser(
        "set-interview-stage",
        help="Set an interview stage status in durable workflow state",
    )
    set_stage_parser.add_argument("loop_id", nargs="?")
    set_stage_parser.add_argument("stage_id", nargs="?")
    set_stage_parser.add_argument("--query")
    set_stage_parser.add_argument("status")
    set_stage_parser.add_argument("--scheduled-at")
    set_stage_parser.add_argument("--duration-minutes", type=int)
    set_stage_parser.add_argument("--interviewer-names")
    set_stage_parser.add_argument("--debrief")
    set_stage_parser.add_argument("--loop-status")
    set_stage_parser.add_argument("--next-step")
    set_stage_parser.set_defaults(handler=_update_interview_stage_state)

    add_stage_parser = subparsers.add_parser(
        "add-interview-stage",
        help="Append or insert an interview stage into durable workflow state",
    )
    add_stage_parser.add_argument("loop_id", nargs="?")
    add_stage_parser.add_argument("kind")
    add_stage_parser.add_argument("--query")
    add_stage_parser.add_argument("--stage-id")
    add_stage_parser.add_argument("--after-stage-id")
    add_stage_parser.add_argument("--status")
    add_stage_parser.add_argument("--scheduled-at")
    add_stage_parser.add_argument("--duration-minutes", type=int)
    add_stage_parser.add_argument("--interviewer-names")
    add_stage_parser.add_argument("--debrief")
    add_stage_parser.add_argument("--next-step")
    add_stage_parser.set_defaults(handler=_add_interview_stage)

    set_task_parser = subparsers.add_parser(
        "set-task-status",
        help="Set a canonical task status in durable workflow state",
    )
    set_task_parser.add_argument("task_id", nargs="?")
    set_task_parser.add_argument("--query")
    set_task_parser.add_argument("status")
    set_task_parser.add_argument("--notes")
    set_task_parser.add_argument("--due-at")
    set_task_parser.set_defaults(handler=_update_task_state)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    result = args.handler(args)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

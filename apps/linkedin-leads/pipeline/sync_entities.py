#!/usr/bin/env python3
"""
Phase 4A: Canonical Entity Sync

Maps the current recruiter pipeline output into canonical entity records for the
unified hunt system.

Usage:
  python -m pipeline.sync_entities
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import (
    APPLICATIONS_DIR,
    CLASSIFIED_FILE,
    INBOX_FILE,
    CONVERSATIONS_DIR,
    ENRICHMENT_ARTIFACTS_DIR,
    ENTITY_MANIFEST_FILE,
    ENTITY_OVERRIDES_FILE,
    INTERVIEW_LOOPS_DIR,
    LEADS_DIR,
    OPPORTUNITIES_DIR,
    PREP_ARTIFACTS_DIR,
    PREP_DIR,
    SIGNALS_DIR,
    TASKS_DIR,
    USER_NAME,
)
from pipeline.entity_workflow import load_workflow_state
from pipeline.extract_contacts import extract_from_conversation
from pipeline.followup_scheduler import load_lead_states

MIGRATED_FLASHCARDS_FILE = PREP_DIR.parent / "data" / "knowledge" / "interview_flashcards.json"
ROLE_PATTERNS = [
    r"((?:Senior|Staff|Lead|Principal|Founding|Applied|Full[- ]Stack|Backend|Frontend|Machine Learning|ML|AI|Gen AI|Customer)? ?(?:Software|AI|ML|Machine Learning|Data|Full[- ]Stack|Backend|Frontend|Forward Deployed|Solutions|Customer)? ?(?:Engineer|Developer|Architect|Scientist|Researcher|Manager))",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def _stable_id(prefix: str, *parts: Any) -> str:
    normalized = [str(part).strip() for part in parts if str(part).strip()]
    base = "||".join(normalized) if normalized else prefix
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    label = _slugify(normalized[0], prefix) if normalized else prefix
    return f"{prefix}_{label[:24]}_{digest}"


def _load_source_data() -> tuple[Path, dict[str, Any]]:
    source = CLASSIFIED_FILE if CLASSIFIED_FILE.exists() else INBOX_FILE
    if not source.exists():
        print("Error: no inbox data found. Run the scraper or classifier first.", file=sys.stderr)
        sys.exit(1)
    with open(source) as f:
        return source, json.load(f)


def _load_overrides() -> dict[str, Any]:
    if not ENTITY_OVERRIDES_FILE.exists():
        return {"conversation_overrides": {}}
    with open(ENTITY_OVERRIDES_FILE) as f:
        data = json.load(f)
    if "conversation_overrides" not in data:
        data["conversation_overrides"] = {}
    return data


def _ensure_output_dirs() -> None:
    for directory in (
        LEADS_DIR,
        OPPORTUNITIES_DIR,
        CONVERSATIONS_DIR,
        SIGNALS_DIR,
        PREP_ARTIFACTS_DIR,
        INTERVIEW_LOOPS_DIR,
        TASKS_DIR,
        APPLICATIONS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def _prune_stale_records(directory: Path, keep_ids: set[str]) -> None:
    for path in directory.glob("*.json"):
        if path.stem not in keep_ids:
            path.unlink()


def _apply_override(record: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    if not override:
        return record
    for key, value in override.items():
        if key in record:
            record[key] = value
    return record


def _get_other_participant(convo: dict[str, Any]) -> dict[str, Any]:
    participants = convo.get("participants", [])
    other = next((p for p in participants if p.get("name") != USER_NAME), None)
    if other:
        return other
    return participants[0] if participants else {"name": "Unknown", "headline": "", "profileUrn": ""}


def _get_last_sender(convo: dict[str, Any]) -> str | None:
    messages = convo.get("messages", [])
    if not messages:
        return None
    return messages[-1].get("sender")


def _derive_lead_status(convo: dict[str, Any], state: dict[str, Any]) -> str:
    status = state.get("status")
    if status == "scheduled":
        return "scheduled"
    if status == "cold":
        return "cold"
    if status == "declined":
        return "declined"
    if _get_last_sender(convo) == USER_NAME:
        return "responded"
    if convo.get("messages"):
        return "active"
    return "new"


def _derive_conversation_status(convo: dict[str, Any], state: dict[str, Any]) -> str:
    classification = convo.get("classification", {}).get("category")
    status = state.get("status")
    if status == "scheduled":
        return "scheduled"
    if status == "cold":
        return "cold"
    if status == "declined":
        return "closed"
    if status in {"replied", "awaiting_response", "followed_up_1", "followed_up_2"}:
        return "awaiting_reply"
    if classification in {"spam", "personal"}:
        return "archived"
    if convo.get("reply", {}).get("status") == "draft":
        return "awaiting_reply"
    if _get_last_sender(convo) != USER_NAME and convo.get("messages"):
        return "awaiting_reply"
    if convo.get("messages"):
        return "active"
    return "new"


def _derive_opportunity_status(convo: dict[str, Any], state: dict[str, Any], has_interview_signal: bool) -> str:
    status = state.get("status")
    if status == "declined":
        return "withdrawn"
    if status == "cold":
        return "archived"
    if status == "scheduled" or has_interview_signal:
        return "interviewing"
    if status in {"replied", "awaiting_response", "followed_up_1", "followed_up_2"}:
        return "contacted"
    if _get_last_sender(convo) == USER_NAME:
        return "contacted"
    return "discovered"


def _signal_payload(kind: str, source_id: str, value: Any, notes: str | None = None) -> dict[str, Any]:
    return {
        "id": _stable_id("sig", kind, source_id, json.dumps(value, sort_keys=True)),
        "kind": kind,
        "source": "conversation",
        "source_id": source_id,
        "value": value,
        "confidence": None,
        "notes": notes,
        "created_at": _now_iso(),
    }


def _read_markdown_title(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").title()


def _extract_company_from_prep(path: Path, text: str) -> str | None:
    if path.parent.name == "companies":
        for line in text.splitlines():
            if line.strip().startswith("- company:"):
                return line.split(":", 1)[1].strip().strip("`")
        return path.stem.replace("-", " ").title()
    return None


def _parse_markdown_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"intro": []}
    current = "intro"
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            current = stripped[3:].strip().lower()
            sections.setdefault(current, [])
            continue
        if stripped.startswith("# "):
            continue
        if stripped.startswith("- "):
            sections.setdefault(current, []).append(stripped[2:].strip())
            continue
        sections.setdefault(current, []).append(stripped)
    return sections


def _clean_md_scalar(value: str) -> str:
    return value.strip().strip("`").strip()


def _parse_reusable_signals(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    start: int | None = None
    for idx, raw_line in enumerate(lines):
        if raw_line.strip().lower() == "## reusable signals":
            start = idx + 1
            break
    if start is None:
        return {}

    parsed: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in lines[start:]:
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            break
        if not stripped:
            continue
        if not stripped.startswith("- "):
            continue
        item = stripped[2:].strip()
        if ":" in item and not item.endswith(":"):
            key, value = item.split(":", 1)
            parsed[key.strip()] = _clean_md_scalar(value)
            current_key = None
            continue
        if item.endswith(":"):
            current_key = item[:-1].strip()
            parsed[current_key] = []
            continue
        if current_key:
            parsed.setdefault(current_key, []).append(_clean_md_scalar(item))
    return parsed


def _extract_prep_structured_data(path: Path, text: str, kind: str) -> dict[str, Any] | None:
    sections = _parse_markdown_sections(text)
    if kind == "company_dossier":
        signals = _parse_reusable_signals(text)
        return {
            "company_context": sections.get("company snapshot", []),
            "operating_thesis": sections.get("operating thesis", []),
            "interview_angles": sections.get("interview angles", []),
            "tailored_value_props": sections.get("tailored value proposition", []),
            "interview_stage_tags": signals.get("interview_stage_tags", []),
            "prep_tags": signals.get("prep_tags", []),
            "follow_on_actions": sections.get("follow-on actions", []),
        }
    if kind == "systems_design_topic":
        suggested_stage_tags = []
        for item in sections.get("next migration steps", []):
            if "tag topics by interview stage" in item.lower():
                suggested_stage_tags.extend(["system_design", "backend", "distributed_systems"])
        return {
            "primary_topics": sections.get("primary topics", []),
            "study_goals": sections.get("study goals", []),
            "flashcard_candidates": sections.get("flashcard candidates", []),
            "suggested_stage_tags": suggested_stage_tags,
        }
    return None


def _load_prep_artifacts() -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted(PREP_DIR.glob("*/*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text()
        relative_path = str(path.relative_to(PREP_DIR.parent))
        kind = "study_note"
        topic_tags: list[str] = []
        if path.parent.name == "companies":
            kind = "company_dossier"
            topic_tags = ["company_prep"]
        elif path.parent.name == "topics":
            kind = "systems_design_topic"
            topic_tags = ["systems_design"]
        elif path.parent.name == "flashcards":
            kind = "flashcard_set"
            topic_tags = ["flashcards"]
        elif path.parent.name == "debriefs":
            kind = "interview_debrief"
            topic_tags = ["debrief"]

        artifact = {
            "id": _stable_id("prep", relative_path),
            "kind": kind,
            "title": _read_markdown_title(path),
            "status": "active",
            "company": _extract_company_from_prep(path, text),
            "opportunity_id": None,
            "topic_tags": topic_tags,
            "source_paths": [relative_path],
            "summary": text.splitlines()[2].strip() if len(text.splitlines()) > 2 else None,
            "content_path": relative_path,
            "structured_data": _extract_prep_structured_data(path, text, kind),
            "related_signal_ids": [],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        artifacts.append(artifact)

    if MIGRATED_FLASHCARDS_FILE.exists():
        cards = json.loads(MIGRATED_FLASHCARDS_FILE.read_text())
        artifacts.append({
            "id": _stable_id("prep", "data/knowledge/interview_flashcards.json"),
            "kind": "flashcard_set",
            "title": "Engineering Interview Flashcards",
            "status": "active",
            "company": None,
            "opportunity_id": None,
            "topic_tags": ["flashcards", "algorithms", "coding_interview"],
            "source_paths": ["data/knowledge/interview_flashcards.json"],
            "summary": f"Interview flashcard set with {len(cards)} cards.",
            "content_path": "data/knowledge/interview_flashcards.json",
            "structured_data": None,
            "related_signal_ids": [],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        })

    if ENRICHMENT_ARTIFACTS_DIR.exists():
        for path in sorted(ENRICHMENT_ARTIFACTS_DIR.glob("*.json")):
            artifact = json.loads(path.read_text())
            if not isinstance(artifact, dict) or not artifact.get("id"):
                continue
            artifacts.append(artifact)
    return artifacts


def _normalize_company(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _clean_company_candidate(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate or candidate == "-":
        return None
    if candidate.lower().startswith(("lead recruiter", "recruitment consultant", "talent acquisition")):
        return None
    return candidate


def _derive_company_name(convo: dict[str, Any], other: dict[str, Any], meta: dict[str, Any]) -> str:
    company = _clean_company_candidate(meta.get("company"))
    if company:
        return company

    headline = (other.get("headline") or "").strip()
    if "@" in headline:
        after_at = headline.split("@", 1)[1].strip()
        extracted = re.split(r"[|,/]", after_at)[0].strip()
        cleaned = _clean_company_candidate(extracted)
        if cleaned:
            return cleaned

    title = convo.get("title") or ""
    subject_match = re.search(r"\bwith\s+([A-Z][A-Za-z0-9&.\- ]+)", title)
    if subject_match:
        cleaned = _clean_company_candidate(subject_match.group(1).strip())
        if cleaned:
            return cleaned

    summary = (meta.get("role_description_summary") or "").lower()
    if any(phrase in summary for phrase in ("start-ups", "startups", "several", "various roles")):
        return "Multiple Companies"
    if meta.get("recruiter_type") == "agency":
        return "Multiple Companies"

    return "Unknown Company"


def _infer_role_title(convo: dict[str, Any], meta: dict[str, Any]) -> str:
    role_title = (meta.get("role_title") or "").strip()
    if role_title:
        return role_title

    title = convo.get("title") or ""
    for pattern in ROLE_PATTERNS:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip().title()

    summary = meta.get("role_description_summary") or ""
    for pattern in ROLE_PATTERNS:
        match = re.search(pattern, summary, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip().title()

    messages = convo.get("messages", [])
    combined = "\n".join((message.get("subject") or "") + "\n" + (message.get("text") or "") for message in messages[:3])
    for pattern in ROLE_PATTERNS:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip().title()

    summary_lower = summary.lower()
    if any(phrase in summary_lower for phrase in ("engineers and researchers", "engineers & researchers", "various roles", "several roles")):
        return "Multiple Engineering Roles"
    if meta.get("recruiter_type") == "agency" and (meta.get("skills_requested") or []):
        return "Engineering Opportunity"

    return "Unknown role"


def _match_prep_artifacts(
    opportunity: dict[str, Any],
    all_artifacts: list[dict[str, Any]],
    has_interview_signal: bool,
) -> list[str]:
    matched: list[str] = []
    opportunity_company = _normalize_company(opportunity.get("company"))
    for artifact in all_artifacts:
        if artifact.get("status") not in {"active", "reviewed"}:
            continue
        artifact_company = _normalize_company(artifact.get("company"))
        if artifact_company and opportunity_company and artifact_company == opportunity_company:
            matched.append(artifact["id"])
        elif has_interview_signal and artifact["kind"] in {"systems_design_topic", "flashcard_set"}:
            matched.append(artifact["id"])
    return sorted(set(matched))


def _artifact_supports_stage(artifact: dict[str, Any], stage_kind: str) -> bool:
    kind = artifact.get("kind")
    structured = artifact.get("structured_data") or {}
    if kind == "company_dossier":
        return True
    if kind == "systems_design_topic":
        suggested = set(structured.get("suggested_stage_tags", []))
        stage_aliases = {
            "system_design": {"system_design", "backend", "distributed_systems"},
            "technical": {"backend", "distributed_systems", "system_design"},
            "onsite": {"backend", "distributed_systems", "system_design"},
            "final_round": {"system_design", "backend"},
        }.get(stage_kind, set())
        return bool(suggested & stage_aliases)
    if kind == "flashcard_set":
        return stage_kind in {
            "recruiter_screen",
            "hiring_manager",
            "technical",
            "system_design",
            "behavioral",
            "onsite",
            "final_round",
        }
    return False


def _select_stage_prep_artifact_ids(
    stage_kind: str,
    opportunity: dict[str, Any],
    prep_artifacts: list[dict[str, Any]],
) -> list[str]:
    selected: list[str] = []
    opportunity_company = _normalize_company(opportunity.get("company"))
    for artifact in prep_artifacts:
        if artifact.get("status") not in {"active", "reviewed"}:
            continue
        artifact_company = _normalize_company(artifact.get("company"))
        if artifact_company and opportunity_company and artifact_company == opportunity_company:
            selected.append(artifact["id"])
            continue
        if _artifact_supports_stage(artifact, stage_kind):
            selected.append(artifact["id"])

    if stage_kind == "recruiter_screen":
        selected = [
            artifact_id for artifact_id in selected
            if next((artifact.get("kind") for artifact in prep_artifacts if artifact["id"] == artifact_id), None)
            in {"company_dossier", "flashcard_set"}
        ]

    return sorted(set(selected))


def _infer_interview_stage_kind(signals: list[dict[str, Any]], next_action: str | None) -> str:
    text = " ".join(str(signal.get("value", "")) for signal in signals if signal["kind"] == "interview_request")
    if next_action:
        text = f"{text} {next_action}"
    lowered = text.lower()
    if "final interview" in lowered or "final round" in lowered:
        return "final_round"
    if "system design" in lowered:
        return "system_design"
    if "take home" in lowered or "take-home" in lowered:
        return "take_home"
    if "technical" in lowered:
        return "technical"
    return "recruiter_screen"


def _stage_kind_label(kind: str) -> str:
    return {
        "recruiter_screen": "recruiter screen",
        "hiring_manager": "hiring manager interview",
        "technical": "technical interview",
        "system_design": "system design interview",
        "behavioral": "behavioral interview",
        "onsite": "onsite interview",
        "take_home": "take-home",
        "final_round": "final round",
        "other": "interview stage",
    }.get(kind, kind.replace("_", " "))


def _build_interview_loop(
    opportunity: dict[str, Any],
    signals: list[dict[str, Any]],
    prep_artifact_ids: list[str],
    prep_artifacts: list[dict[str, Any]],
    workflow_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    interview_signals = [signal for signal in signals if signal["kind"] == "interview_request"]
    if not interview_signals:
        return None

    loop_id = _stable_id("loop", opportunity["id"])
    stage_kind = _infer_interview_stage_kind(interview_signals, opportunity.get("next_action"))
    loop = {
        "id": loop_id,
        "opportunity_id": opportunity["id"],
        "status": "active",
        "stages": [
            {
                "id": _stable_id("stage", loop_id, stage_kind),
                "kind": stage_kind,
                "status": "planned",
                "scheduled_at": None,
                "duration_minutes": None,
                "interviewer_names": [],
                "prep_artifact_ids": _select_stage_prep_artifact_ids(stage_kind, opportunity, prep_artifacts),
                "task_ids": [],
                "debrief": None,
            }
        ],
        "debrief_summary": None,
        "next_step": opportunity.get("next_action"),
        "created_at": opportunity["created_at"],
        "updated_at": opportunity["updated_at"],
    }
    if workflow_state:
        if workflow_state.get("status"):
            loop["status"] = workflow_state["status"]
        if "next_step" in workflow_state:
            loop["next_step"] = workflow_state["next_step"]
        if "debrief_summary" in workflow_state:
            loop["debrief_summary"] = workflow_state["debrief_summary"]
        stage_overrides = workflow_state.get("stages", {})
        stage_map = {stage["id"]: stage for stage in loop["stages"]}
        stage_order = list(workflow_state.get("stage_order") or [stage["id"] for stage in loop["stages"]])
        for stage_id, override in stage_overrides.items():
            if stage_id not in stage_map:
                kind = override.get("kind")
                if not kind:
                    continue
                stage_map[stage_id] = {
                    "id": stage_id,
                    "kind": kind,
                    "status": "planned",
                    "scheduled_at": None,
                    "duration_minutes": None,
                    "interviewer_names": [],
                    "prep_artifact_ids": _select_stage_prep_artifact_ids(kind, opportunity, prep_artifacts),
                    "task_ids": [],
                    "debrief": None,
                }
                if stage_id not in stage_order:
                    stage_order.append(stage_id)
            stage = stage_map[stage_id]
            for field in ("kind", "status", "scheduled_at", "duration_minutes", "interviewer_names", "debrief"):
                if field in override:
                    stage[field] = override[field]
            if not stage.get("prep_artifact_ids"):
                stage["prep_artifact_ids"] = _select_stage_prep_artifact_ids(
                    stage.get("kind", "other"),
                    opportunity,
                    prep_artifacts,
                )
        for stage_id in stage_map:
            if stage_id not in stage_order:
                stage_order.append(stage_id)
        loop["stages"] = [stage_map[stage_id] for stage_id in stage_order if stage_id in stage_map]
        loop["updated_at"] = workflow_state.get("updated_at", _now_iso())
    return loop


def _build_application(
    opportunity: dict[str, Any],
    conversation: dict[str, Any],
    workflow_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    notes = (opportunity.get("next_action") or "").lower()
    if opportunity["status"] not in {"contacted", "interviewing"} and "resume" not in notes:
        return None

    status = "drafting"
    if opportunity["status"] == "interviewing":
        status = "interviewing"
    elif "resume" in notes or "portfolio" in notes:
        status = "drafting"
    else:
        status = "submitted"

    application = {
        "id": _stable_id("app", opportunity["id"]),
        "opportunity_id": opportunity["id"],
        "source": "linkedin_easy_apply" if "easy apply" in notes else "manual",
        "status": status,
        "resume_variant": None,
        "cover_letter_variant": None,
        "submitted_at": None,
        "deadline_at": None,
        "application_url": None,
        "notes": opportunity.get("next_action") or conversation.get("summary"),
        "created_at": opportunity["created_at"],
        "updated_at": opportunity["updated_at"],
    }
    if workflow_state:
        for field in (
            "status",
            "resume_variant",
            "cover_letter_variant",
            "submitted_at",
            "deadline_at",
            "application_url",
            "notes",
            "phased_out_at",
        ):
            if field in workflow_state:
                application[field] = workflow_state[field]
        application["updated_at"] = workflow_state.get("updated_at", _now_iso())
    return application


def _build_tasks(
    opportunity: dict[str, Any],
    conversation: dict[str, Any],
    application: dict[str, Any] | None,
    prep_artifact_ids: list[str],
    prep_artifact_by_id: dict[str, dict[str, Any]],
    interview_loop: dict[str, Any] | None,
    state: dict[str, Any],
    task_states: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    matched_artifacts = [
        prep_artifact_by_id[artifact_id]
        for artifact_id in prep_artifact_ids
        if artifact_id in prep_artifact_by_id
    ]

    if interview_loop:
        for stage in interview_loop["stages"]:
            stage_task_status = "not_started"
            if stage.get("status") == "scheduled":
                stage_task_status = "in_progress"
            elif stage.get("status") == "completed":
                stage_task_status = "complete"
            elif stage.get("status") == "cancelled" or interview_loop.get("status") in {"rejected", "withdrawn"}:
                stage_task_status = "cancelled"

            stage_kind_label = _stage_kind_label(stage.get("kind", "other"))
            stage_prep_artifact_ids = stage.get("prep_artifact_ids", prep_artifact_ids)
            prep_task_id = _stable_id("task", stage["id"], "prep")
            tasks.append({
                "id": prep_task_id,
                "title": f"Prepare for {stage_kind_label}: {opportunity['role_title']} at {opportunity['company']}",
                "kind": "interview_prep",
                "status": stage_task_status,
                "priority": "P1",
                "due_at": stage.get("scheduled_at"),
                "opportunity_id": opportunity["id"],
                "lead_id": opportunity["lead_ids"][0] if opportunity["lead_ids"] else None,
                "interview_loop_id": interview_loop["id"],
                "interview_stage_id": stage["id"],
                "prep_artifact_ids": stage_prep_artifact_ids,
                "external_task_system": None,
                "notes": opportunity.get("next_action"),
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            })

            if stage.get("kind") == "system_design" and any(
                artifact["kind"] == "systems_design_topic" for artifact in matched_artifacts
            ):
                tasks.append({
                    "id": _stable_id("task", stage["id"], "systems-design-study"),
                    "title": f"Study systems design for {opportunity['company']}",
                    "kind": "study_session",
                    "status": stage_task_status,
                    "priority": "P2",
                    "due_at": stage.get("scheduled_at"),
                    "opportunity_id": opportunity["id"],
                    "lead_id": opportunity["lead_ids"][0] if opportunity["lead_ids"] else None,
                    "interview_loop_id": interview_loop["id"],
                    "interview_stage_id": stage["id"],
                    "prep_artifact_ids": stage_prep_artifact_ids,
                    "external_task_system": None,
                    "notes": "Generated from system design interview stage and attached prep artifacts.",
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                })

            if stage.get("status") == "completed" and not stage.get("debrief"):
                tasks.append({
                    "id": _stable_id("task", stage["id"], "debrief"),
                    "title": f"Write debrief for {stage_kind_label}: {opportunity['role_title']} at {opportunity['company']}",
                    "kind": "admin",
                    "status": "not_started",
                    "priority": "P1",
                    "due_at": None,
                    "opportunity_id": opportunity["id"],
                    "lead_id": opportunity["lead_ids"][0] if opportunity["lead_ids"] else None,
                    "interview_loop_id": interview_loop["id"],
                    "interview_stage_id": stage["id"],
                    "prep_artifact_ids": stage_prep_artifact_ids,
                    "external_task_system": None,
                    "notes": "Capture questions asked, weak spots, and follow-up actions while the interview is fresh.",
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                })

    elif conversation["status"] == "awaiting_reply":
        follow_up_status = "waiting"
        if _get_last_sender({"messages": conversation.get("messages", [])}) != USER_NAME and state.get("status") not in {
            "replied",
            "awaiting_response",
            "followed_up_1",
            "followed_up_2",
        }:
            follow_up_status = "not_started"
        tasks.append({
            "id": _stable_id("task", opportunity["id"], "follow-up"),
            "title": f"Reply or follow up: {opportunity['company']} / {opportunity['role_title']}",
            "kind": "follow_up",
            "status": follow_up_status,
            "priority": "P1",
            "due_at": None,
            "opportunity_id": opportunity["id"],
            "lead_id": opportunity["lead_ids"][0] if opportunity["lead_ids"] else None,
            "interview_loop_id": None,
            "interview_stage_id": None,
            "prep_artifact_ids": prep_artifact_ids,
            "external_task_system": None,
            "notes": opportunity.get("next_action"),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        })

    if application and application["status"] == "drafting":
        tasks.append({
            "id": _stable_id("task", opportunity["id"], "application-materials"),
            "title": f"Prepare application materials: {opportunity['company']} / {opportunity['role_title']}",
            "kind": "application",
            "status": "not_started",
            "priority": "P1",
            "due_at": None,
            "opportunity_id": opportunity["id"],
            "lead_id": opportunity["lead_ids"][0] if opportunity["lead_ids"] else None,
            "interview_loop_id": interview_loop["id"] if interview_loop else None,
            "interview_stage_id": None,
            "prep_artifact_ids": prep_artifact_ids,
            "external_task_system": None,
            "notes": application.get("notes"),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        })

    for task in tasks:
        override = task_states.get(task["id"])
        if not override:
            continue
        for field in ("status", "notes", "due_at", "priority"):
            if field in override:
                task[field] = override[field]
        task["updated_at"] = override.get("updated_at", _now_iso())

    return tasks


def _build_signals(
    convo: dict[str, Any],
    conversation_id: str,
) -> list[dict[str, Any]]:
    meta = convo.get("metadata") or {}
    score = convo.get("score") or {}
    contacts = extract_from_conversation(convo)

    signals: list[dict[str, Any]] = []

    if meta.get("urgency"):
        signals.append(_signal_payload("urgency", conversation_id, meta["urgency"]))
    if meta.get("compensation_hints"):
        signals.append(_signal_payload("compensation", conversation_id, meta["compensation_hints"]))
    if meta.get("location"):
        signals.append(_signal_payload("location", conversation_id, meta["location"]))

    next_action = meta.get("next_action_needed")
    if next_action:
        lowered = next_action.lower()
        if any(word in lowered for word in ("interview", "schedule", "call", "availability")):
            signals.append(_signal_payload("interview_request", conversation_id, next_action))

    for phone in contacts.phones_e164:
        signals.append(_signal_payload("phone_number", conversation_id, phone))
    for email in contacts.emails:
        signals.append(_signal_payload("email", conversation_id, email))
    for link in contacts.calendar_links:
        signals.append(_signal_payload("calendar_link", conversation_id, link))
        signals.append(_signal_payload("interview_request", conversation_id, link, notes="Derived from calendar link"))

    for gap in score.get("gaps", []):
        signals.append(_signal_payload("skill_gap", conversation_id, gap))

    deduped: dict[str, dict[str, Any]] = {}
    for signal in signals:
        deduped[signal["id"]] = signal
    return list(deduped.values())


def sync_entities() -> dict[str, Any]:
    source_path, data = _load_source_data()
    _ensure_output_dirs()

    states = load_lead_states()
    overrides = _load_overrides()
    workflow_state = load_workflow_state()
    conversation_overrides = overrides.get("conversation_overrides", {})
    application_states = workflow_state.get("applications", {})
    interview_loop_states = workflow_state.get("interview_loops", {})
    task_states = workflow_state.get("tasks", {})
    conversations = data.get("conversations", [])
    generated_at = _now_iso()

    unique_ids = {
        "leads": set(),
        "opportunities": set(),
        "conversations": set(),
        "signals": set(),
        "prep_artifacts": set(),
        "interview_loops": set(),
        "tasks": set(),
        "applications": set(),
    }
    prep_artifacts = _load_prep_artifacts()
    prep_artifact_by_id = {artifact["id"]: artifact for artifact in prep_artifacts}

    for artifact in prep_artifacts:
        _write_json(PREP_ARTIFACTS_DIR / f"{artifact['id']}.json", artifact)
        unique_ids["prep_artifacts"].add(artifact["id"])

    for convo in conversations:
        other = _get_other_participant(convo)
        conversation_urn = convo.get("conversationUrn", "")
        conversation_id = _stable_id("conv", conversation_urn or other.get("name", "unknown"), "linkedin")
        override_bundle = conversation_overrides.get(conversation_urn, {})
        state = states.get(conversation_urn, {})
        meta = convo.get("metadata") or {}
        score = convo.get("score") or {}
        signals = _build_signals(convo, conversation_id)
        signal_ids = [signal["id"] for signal in signals]
        has_interview_signal = any(signal["kind"] == "interview_request" for signal in signals)

        conversation_record = {
            "id": conversation_id,
            "source": "linkedin",
            "external_thread_id": conversation_urn or None,
            "participant_ids": [],
            "opportunity_id": None,
            "status": _derive_conversation_status(convo, state),
            "classification": convo.get("classification", {}).get("category"),
            "message_count": len(convo.get("messages", [])),
            "last_activity_at": convo.get("lastActivityAt"),
            "summary": convo.get("lastMessagePreview") or convo.get("title"),
            "messages": convo.get("messages", []),
            "signal_ids": signal_ids,
            "created_at": convo.get("createdAt") or generated_at,
            "updated_at": convo.get("lastActivityAt") or generated_at,
        }
        conversation_record = _apply_override(conversation_record, override_bundle.get("conversation"))

        classification = convo.get("classification", {}).get("category")
        if classification == "recruiter":
            company_name = _derive_company_name(convo, other, meta)
            lead_id = _stable_id(
                "lead",
                other.get("profileUrn") or other.get("name", "unknown"),
                "linkedin",
            )
            opportunity_id = _stable_id(
                "opp",
                company_name,
                meta.get("role_title") or convo.get("title") or conversation_urn or "unknown-role",
            )

            contacts = extract_from_conversation(convo)
            lead_record = {
                "id": lead_id,
                "name": other.get("name", "Unknown"),
                "source": "linkedin",
                "status": _derive_lead_status(convo, state),
                "headline": other.get("headline"),
                "company": company_name,
                "role_title": meta.get("role_title"),
                "recruiter_type": meta.get("recruiter_type"),
                "profile_url": None,
                "profile_urn": other.get("profileUrn"),
                "contact_methods": {
                    "emails": contacts.emails,
                    "phones_e164": contacts.phones_e164,
                    "calendar_links": contacts.calendar_links,
                    "websites": contacts.websites,
                },
                "opportunity_ids": [opportunity_id],
                "signal_ids": signal_ids,
                "notes": convo.get("classification", {}).get("reasoning"),
                "created_at": convo.get("createdAt") or generated_at,
                "updated_at": convo.get("lastActivityAt") or generated_at,
            }
            lead_record = _apply_override(lead_record, override_bundle.get("lead"))

            opportunity_record = {
                "id": opportunity_id,
                "company": company_name,
                "role_title": meta.get("role_title") or convo.get("title") or "Unknown role",
                "source": "linkedin",
                "status": _derive_opportunity_status(convo, state, has_interview_signal),
                "location": meta.get("location"),
                "compensation_hints": meta.get("compensation_hints"),
                "industry": meta.get("industry"),
                "fit_score": score.get("total"),
                "priority_score": None,
                "lead_ids": [lead_id],
                "conversation_ids": [conversation_id],
                "application_ids": [],
                "interview_loop_id": None,
                "prep_artifact_ids": [],
                "signal_ids": signal_ids,
                "job_url": None,
                "description_summary": meta.get("role_description_summary"),
                "next_action": meta.get("next_action_needed"),
                "created_at": convo.get("createdAt") or generated_at,
                "updated_at": convo.get("lastActivityAt") or generated_at,
            }
            opportunity_record = _apply_override(opportunity_record, override_bundle.get("opportunity"))

            matched_prep_ids = _match_prep_artifacts(opportunity_record, prep_artifacts, has_interview_signal)
            opportunity_record["prep_artifact_ids"] = matched_prep_ids

            for prep_id in matched_prep_ids:
                artifact = prep_artifact_by_id[prep_id]
                if artifact["company"] == opportunity_record["company"]:
                    artifact["opportunity_id"] = opportunity_record["id"]
                    artifact["updated_at"] = generated_at
                    _write_json(PREP_ARTIFACTS_DIR / f"{artifact['id']}.json", artifact)

            interview_loop_id = _stable_id("loop", opportunity_record["id"])
            application_id = _stable_id("app", opportunity_record["id"])
            interview_loop = _build_interview_loop(
                opportunity_record,
                signals,
                matched_prep_ids,
                [prep_artifact_by_id[artifact_id] for artifact_id in matched_prep_ids if artifact_id in prep_artifact_by_id],
                interview_loop_states.get(interview_loop_id),
            )
            application = _build_application(
                opportunity_record,
                conversation_record,
                application_states.get(application_id),
            )
            application = _apply_override(application, override_bundle.get("application")) if application else None
            if application:
                opportunity_record["application_ids"] = [application["id"]]
                _write_json(APPLICATIONS_DIR / f"{application['id']}.json", application)
                unique_ids["applications"].add(application["id"])

            tasks = _build_tasks(
                opportunity_record,
                conversation_record,
                application,
                matched_prep_ids,
                prep_artifact_by_id,
                interview_loop,
                state,
                task_states,
            )

            if interview_loop:
                tasks_by_stage: dict[str, list[str]] = {}
                loop_level_task_ids: list[str] = []
                for task in tasks:
                    stage_id = task.get("interview_stage_id")
                    if stage_id:
                        tasks_by_stage.setdefault(stage_id, []).append(task["id"])
                    elif task.get("interview_loop_id") == interview_loop["id"]:
                        loop_level_task_ids.append(task["id"])
                for stage in interview_loop["stages"]:
                    stage["task_ids"] = tasks_by_stage.get(stage["id"], [])
                interview_loop["task_ids"] = loop_level_task_ids
                opportunity_record["interview_loop_id"] = interview_loop["id"]
                _write_json(INTERVIEW_LOOPS_DIR / f"{interview_loop['id']}.json", interview_loop)
                unique_ids["interview_loops"].add(interview_loop["id"])

            for task in tasks:
                _write_json(TASKS_DIR / f"{task['id']}.json", task)
                unique_ids["tasks"].add(task["id"])

            conversation_record["participant_ids"] = [lead_id]
            conversation_record["opportunity_id"] = opportunity_id

            _write_json(LEADS_DIR / f"{lead_id}.json", lead_record)
            _write_json(OPPORTUNITIES_DIR / f"{opportunity_id}.json", opportunity_record)

            unique_ids["leads"].add(lead_id)
            unique_ids["opportunities"].add(opportunity_id)

        _write_json(CONVERSATIONS_DIR / f"{conversation_id}.json", conversation_record)
        unique_ids["conversations"].add(conversation_id)

        for signal in signals:
            _write_json(SIGNALS_DIR / f"{signal['id']}.json", signal)
            unique_ids["signals"].add(signal["id"])

    _prune_stale_records(LEADS_DIR, unique_ids["leads"])
    _prune_stale_records(OPPORTUNITIES_DIR, unique_ids["opportunities"])
    _prune_stale_records(CONVERSATIONS_DIR, unique_ids["conversations"])
    _prune_stale_records(SIGNALS_DIR, unique_ids["signals"])
    _prune_stale_records(PREP_ARTIFACTS_DIR, unique_ids["prep_artifacts"])
    _prune_stale_records(INTERVIEW_LOOPS_DIR, unique_ids["interview_loops"])
    _prune_stale_records(TASKS_DIR, unique_ids["tasks"])
    _prune_stale_records(APPLICATIONS_DIR, unique_ids["applications"])

    counts = {key: len(value) for key, value in unique_ids.items()}

    manifest = {
        "generated_at": generated_at,
        "source_file": str(source_path),
        "conversation_count": len(conversations),
        "counts": counts,
        "notes": "Generated by pipeline.sync_entities from recruiter pipeline outputs.",
    }
    _write_json(ENTITY_MANIFEST_FILE, manifest)
    return manifest


def main() -> None:
    manifest = sync_entities()
    print("Canonical entity sync complete")
    for key, value in manifest["counts"].items():
        print(f"  {key}: {value}")
    print(f"  manifest: {ENTITY_MANIFEST_FILE}")


if __name__ == "__main__":
    main()

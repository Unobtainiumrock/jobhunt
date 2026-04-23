#!/usr/bin/env python3
"""
Build deterministic stage-specific prep packets from canonical prep artifacts.
"""

from __future__ import annotations

from typing import Any

import yaml

from pipeline.config import PROFILE_FILE


def _read_profile() -> dict[str, Any]:
    with open(PROFILE_FILE) as f:
        return yaml.safe_load(f)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def _profile_highlights_for_stage(profile: dict[str, Any], stage_kind: str) -> list[str]:
    summary = profile.get("summary")
    achievements = profile.get("achievements", [])
    expertise = profile.get("expertise_areas", [])
    skills = profile.get("skills", {}).get("technical", [])
    highlights: list[str] = []

    if summary and stage_kind in {"recruiter_screen", "hiring_manager", "behavioral", "final_round"}:
        highlights.append(summary)

    if stage_kind in {"technical", "system_design", "onsite", "take_home"}:
        target_skills = {"Python", "JavaScript/Node.js", "LLM Engineering", "Vector Databases / Embeddings", "Data Engineering / ETL", "Full Stack Development"}
        for skill in skills:
            if skill.get("name") in target_skills:
                evidence = skill.get("evidence", [])
                snippet = evidence[0] if evidence else skill.get("name")
                highlights.append(f"{skill.get('name')}: {snippet}")

    if stage_kind in {"system_design", "hiring_manager", "final_round"}:
        for area in expertise[:3]:
            highlights.append(f"{area.get('area')}: {area.get('description')}")

    if stage_kind in {"behavioral", "hiring_manager", "final_round"}:
        for achievement in achievements[:2]:
            highlights.append(f"{achievement.get('title')}: {achievement.get('description')}")

    return _dedupe_keep_order(highlights)[:4]


def _artifact_relevant_for_stage(artifact: dict[str, Any], stage_kind: str) -> bool:
    kind = artifact.get("kind")
    structured = artifact.get("structured_data") or {}
    if kind == "systems_design_topic":
        suggested_stage_tags = set(structured.get("suggested_stage_tags", []))
        if suggested_stage_tags and stage_kind not in suggested_stage_tags:
            return False
        return stage_kind in {"system_design", "technical", "onsite", "final_round", "other"}
    return True


def _artifact_highlights_for_stage(artifact: dict[str, Any], stage_kind: str) -> dict[str, list[str]]:
    structured = artifact.get("structured_data") or {}
    kind = artifact.get("kind")

    company_context: list[str] = []
    focus_topics: list[str] = []
    talking_points: list[str] = []
    suggested_actions: list[str] = []

    if not _artifact_relevant_for_stage(artifact, stage_kind):
        return {
            "company_context": [],
            "focus_topics": [],
            "talking_points": [],
            "suggested_actions": [],
        }

    if kind == "company_dossier":
        company_context.extend(structured.get("company_context", [])[:3])
        talking_points.extend(structured.get("interview_angles", [])[:4])
        talking_points.extend(structured.get("tailored_value_props", [])[:4])
        if stage_kind in {"hiring_manager", "system_design", "final_round", "other"}:
            talking_points.extend(structured.get("risks_and_open_questions", [])[:2])
        suggested_actions.extend(structured.get("follow_on_actions", [])[:3])
        if stage_kind in {"recruiter_screen", "hiring_manager"}:
            company_context.extend(structured.get("operating_thesis", [])[:2])

    elif kind == "systems_design_topic":
        focus_topics.extend(structured.get("primary_topics", [])[:6])
        talking_points.extend(structured.get("study_goals", [])[:4])
        suggested_actions.extend(structured.get("flashcard_candidates", [])[:4])

    elif kind == "flashcard_set":
        focus_topics.append(artifact.get("summary") or artifact.get("title"))
        suggested_actions.append("Use flashcards for rapid recall before or after the stage.")

    return {
        "company_context": _dedupe_keep_order(company_context),
        "focus_topics": _dedupe_keep_order(focus_topics),
        "talking_points": _dedupe_keep_order(talking_points),
        "suggested_actions": _dedupe_keep_order(suggested_actions),
    }


def _stage_goal(stage_kind: str) -> str:
    return {
        "recruiter_screen": "Show strong role alignment, communicate interest clearly, and reduce perceived hiring risk.",
        "hiring_manager": "Translate your background into concrete business and engineering value for the team.",
        "technical": "Demonstrate implementation depth, debugging judgment, and fluency with the stack.",
        "system_design": "Explain tradeoffs crisply, choose pragmatic architecture, and justify complexity.",
        "behavioral": "Tell compact stories with ownership, conflict handling, and measurable outcomes.",
        "onsite": "Stay consistent across rounds and connect technical depth back to execution and team fit.",
        "take_home": "Produce a clean, scoped solution with explicit assumptions and good communication.",
        "final_round": "Synthesize technical strength, product judgment, and team fit into a coherent close.",
        "other": "Prepare concise, relevant talking points for the upcoming interview stage.",
    }.get(stage_kind, "Prepare concise, relevant talking points for the upcoming interview stage.")


def build_stage_prep_packet(
    stage: dict[str, Any],
    opportunity: dict[str, Any],
    prep_artifacts: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if profile is None:
        profile = _read_profile()

    company_context: list[str] = []
    focus_topics: list[str] = []
    talking_points: list[str] = []
    suggested_actions: list[str] = []
    relevant_artifact_titles: list[str] = []

    for artifact in prep_artifacts:
        if not _artifact_relevant_for_stage(artifact, stage.get("kind", "other")):
            continue
        if artifact.get("title"):
            relevant_artifact_titles.append(artifact["title"])
        extracted = _artifact_highlights_for_stage(artifact, stage.get("kind", "other"))
        company_context.extend(extracted["company_context"])
        focus_topics.extend(extracted["focus_topics"])
        talking_points.extend(extracted["talking_points"])
        suggested_actions.extend(extracted["suggested_actions"])

    profile_highlights = _profile_highlights_for_stage(profile, stage.get("kind", "other"))

    if stage.get("kind") == "system_design":
        suggested_actions.append("Practice explaining one architecture from requirements to scaling tradeoffs in under 10 minutes.")
    elif stage.get("kind") in {"technical", "take_home"}:
        suggested_actions.append("Review one or two concrete implementation stories that map directly to the role.")
    elif stage.get("kind") in {"recruiter_screen", "hiring_manager", "behavioral", "final_round"}:
        suggested_actions.append("Prepare a concise why-this-role answer and one or two high-signal accomplishment stories.")

    return {
        "stage_id": stage.get("id"),
        "stage_kind": stage.get("kind"),
        "goal": _stage_goal(stage.get("kind", "other")),
        "artifact_titles": _dedupe_keep_order(relevant_artifact_titles),
        "company_context": _dedupe_keep_order(company_context)[:4],
        "focus_topics": _dedupe_keep_order(focus_topics)[:6],
        "talking_points": _dedupe_keep_order(talking_points)[:6],
        "profile_highlights": _dedupe_keep_order(profile_highlights)[:4],
        "suggested_actions": _dedupe_keep_order(suggested_actions)[:4],
        "summary": f"{opportunity.get('company')} / {opportunity.get('role_title')} — focus this {stage.get('kind', 'interview')} round on {_stage_goal(stage.get('kind', 'other')).lower()}",
    }

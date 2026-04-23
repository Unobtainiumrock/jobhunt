#!/usr/bin/env python3
"""
Optional external company-research enrichment for canonical prep artifacts.

This module keeps research separate from the deterministic local baseline:
- queue research jobs
- submit optional Gemini Deep Research requests
- poll for completed reports
- parse reports into narrow draft PrepArtifact records
- require an explicit apply step before those artifacts affect prep matching

Usage:
  python -m pipeline.research_enrichment queue --company "Example"
  python -m pipeline.research_enrichment list
  python -m pipeline.research_enrichment start
  python -m pipeline.research_enrichment poll
  python -m pipeline.research_enrichment ingest-report --job-id JOB --report-file report.md
  python -m pipeline.research_enrichment apply --job-id JOB
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from urllib.parse import urlparse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import (
    ENRICHMENT_ARTIFACTS_DIR,
    ENRICHMENT_QUEUE_FILE,
    GEMINI_API_BASE,
    GEMINI_API_KEY,
    GEMINI_DEEP_RESEARCH_AGENT,
    OPPORTUNITIES_DIR,
    PREP_ARTIFACTS_DIR,
    PROJECT_ROOT,
)


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


def _ensure_storage_dirs() -> None:
    ENRICHMENT_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENRICHMENT_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_queue() -> dict[str, Any]:
    _ensure_storage_dirs()
    if not ENRICHMENT_QUEUE_FILE.exists():
        return {"jobs": []}
    with open(ENRICHMENT_QUEUE_FILE) as f:
        data = json.load(f)
    if "jobs" not in data or not isinstance(data["jobs"], list):
        data["jobs"] = []
    return data


def _save_queue(queue: dict[str, Any]) -> None:
    _ensure_storage_dirs()
    with open(ENRICHMENT_QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2, sort_keys=True)
        f.write("\n")


def _relative_to_project(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _load_opportunity(opportunity_id: str) -> dict[str, Any] | None:
    path = OPPORTUNITIES_DIR / f"{opportunity_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _find_job(queue: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    return next((job for job in queue["jobs"] if job["id"] == job_id), None)


def _build_job(
    queue: dict[str, Any],
    company: str,
    role_title: str | None = None,
    opportunity_id: str | None = None,
    context: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    opportunity = _load_opportunity(opportunity_id) if opportunity_id else None
    company = company or (opportunity or {}).get("company")
    role_title = role_title or (opportunity or {}).get("role_title")
    if not company:
        return None, "Error: provide company or a valid opportunity id."

    context_parts: list[str] = []
    if opportunity:
        if opportunity.get("next_action"):
            context_parts.append(f"Current next action: {opportunity['next_action']}")
        if opportunity.get("status"):
            context_parts.append(f"Opportunity status: {opportunity['status']}")
    if context:
        context_parts.append(context.strip())

    existing = next(
        (
            job for job in queue["jobs"]
            if job.get("company") == company
            and job.get("role_title") == role_title
            and job.get("opportunity_id") == opportunity_id
            and job.get("status") in {"queued", "submitted", "completed", "applied"}
        ),
        None,
    )
    if existing:
        return existing, None

    created_at = _now_iso()
    job = {
        "id": _stable_id("research", company, role_title or "", opportunity_id or "", created_at),
        "status": "queued",
        "provider": "gemini_deep_research",
        "company": company,
        "role_title": role_title,
        "opportunity_id": opportunity_id,
        "context": " ".join(context_parts) or None,
        "prompt": None,
        "interaction_id": None,
        "report_path": None,
        "artifact_path": None,
        "error": None,
        "created_at": created_at,
        "updated_at": created_at,
        "submitted_at": None,
        "completed_at": None,
        "applied_at": None,
    }
    return job, None


def _queue_job(args: argparse.Namespace) -> int:
    queue = _load_queue()
    opportunity = _load_opportunity(args.opportunity_id) if args.opportunity_id else None
    company = args.company or (opportunity or {}).get("company")
    job, error = _build_job(
        queue,
        company=company,
        role_title=args.role_title,
        opportunity_id=args.opportunity_id,
        context=args.context,
    )
    if error:
        print(error, file=sys.stderr)
        return 1
    assert job is not None
    if any(existing is job for existing in queue["jobs"]):
        print(f"Research job already queued: {job['id']}")
        return 0
    queue["jobs"].append(job)
    _save_queue(queue)
    print(job["id"])
    return 0


def _load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def _has_active_external_research(opportunity: dict[str, Any], prep_artifacts: list[dict[str, Any]]) -> bool:
    for artifact in prep_artifacts:
        if artifact.get("kind") != "company_dossier":
            continue
        if artifact.get("status") not in {"active", "reviewed"}:
            continue
        if "external_research" not in (artifact.get("topic_tags") or []):
            continue
        if artifact.get("opportunity_id") == opportunity.get("id"):
            return True
        if artifact.get("company") and artifact.get("company") == opportunity.get("company"):
            return True
    return False


def _build_prompt(job: dict[str, Any]) -> str:
    company = job["company"]
    role_title = job.get("role_title") or "unknown role"
    context = job.get("context")
    prompt_lines = [
        f"Research {company} for interview preparation.",
        f"Target role context: {role_title}.",
        "Focus on practical job-hunt prep, not a generic market overview.",
        "Use public company sources and high-quality external coverage when useful.",
        "Keep the report compact and signal-dense.",
    ]
    if context:
        prompt_lines.append(f"Additional recruiter or opportunity context: {context}")
    prompt_lines.extend([
        "",
        "Return markdown using exactly these sections in this order:",
        "## Company Snapshot",
        "## Operating Thesis",
        "## Interview Angles",
        "## Tailored Value Proposition",
        "## Risks and Open Questions",
        "## Sources",
        "",
        "Formatting rules:",
        "- Under each section, use short bullets.",
        "- In Tailored Value Proposition, explicitly tie the candidate profile to the company and role.",
        "- In Sources, include one bullet per source with a short label and URL or site name.",
        "- Do not add any other headings.",
    ])
    return "\n".join(prompt_lines)


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    request = urllib.request.Request(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _start_jobs(args: argparse.Namespace) -> int:
    queue = _load_queue()
    target_jobs = queue["jobs"]
    if args.job_id:
        job = _find_job(queue, args.job_id)
        if not job:
            print(f"Error: unknown job {args.job_id}", file=sys.stderr)
            return 1
        target_jobs = [job]

    pending = [job for job in target_jobs if job.get("status") == "queued"]
    if not pending:
        print("No queued research jobs to submit.")
        return 0

    for job in pending:
        try:
            prompt = _build_prompt(job)
            response = _post_json(
                f"{GEMINI_API_BASE.rstrip('/')}/interactions",
                {
                    "input": prompt,
                    "agent": GEMINI_DEEP_RESEARCH_AGENT,
                    "background": True,
                },
            )
            interaction_id = response.get("id")
            if not interaction_id:
                raise RuntimeError(f"Missing interaction id in response: {response}")
            job["prompt"] = prompt
            job["interaction_id"] = interaction_id
            job["status"] = "submitted"
            job["error"] = None
            job["submitted_at"] = _now_iso()
            job["updated_at"] = _now_iso()
            print(f"Submitted {job['id']} -> {interaction_id}")
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["updated_at"] = _now_iso()
            print(f"Failed to submit {job['id']}: {exc}", file=sys.stderr)
    _save_queue(queue)
    return 0


def _auto_queue_and_start(args: argparse.Namespace) -> int:
    queue = _load_queue()
    opportunities = _load_records(OPPORTUNITIES_DIR)
    prep_artifacts = _load_records(PREP_ARTIFACTS_DIR)
    eligible_statuses = {"interviewing"}
    if args.include_contacted:
        eligible_statuses.add("contacted")
    queued_count = 0

    for opportunity in opportunities:
        if args.limit is not None and queued_count >= args.limit:
            break
        if opportunity.get("status") not in eligible_statuses:
            continue
        if _has_active_external_research(opportunity, prep_artifacts):
            continue
        job, error = _build_job(
            queue,
            company=opportunity.get("company"),
            role_title=opportunity.get("role_title"),
            opportunity_id=opportunity.get("id"),
            context="Automatically queued from canonical opportunity state.",
        )
        if error or job is None:
            continue
        if any(existing is job for existing in queue["jobs"]):
            continue
        queue["jobs"].append(job)
        queued_count += 1

    _save_queue(queue)
    print(f"Queued {queued_count} research job(s).")

    if args.no_start:
        return 0
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY not configured; leaving queued jobs for later submission.")
        return 0
    start_args = argparse.Namespace(job_id=None)
    return _start_jobs(start_args)


def _collect_text_fragments(node: Any) -> list[str]:
    fragments: list[str] = []
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            fragments.append(text.strip())
        for value in node.values():
            fragments.extend(_collect_text_fragments(value))
    elif isinstance(node, list):
        for item in node:
            fragments.extend(_collect_text_fragments(item))
    return fragments


def _extract_report_text(result: dict[str, Any]) -> str:
    outputs = result.get("outputs") or []
    if outputs:
        fragments = _collect_text_fragments(outputs[-1])
        if fragments:
            return "\n\n".join(fragments)
    fragments = _collect_text_fragments(result)
    if fragments:
        return "\n\n".join(fragments)
    raise RuntimeError("Unable to extract report text from interaction response.")


def _parse_markdown_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
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
        if current is None:
            continue
        if stripped.startswith("- "):
            sections[current].append(stripped[2:].strip())
        else:
            sections[current].append(stripped)
    return sections


def _clean_research_text(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^\*\s+", "", cleaned)
    cleaned = re.sub(r"^\d+\.\s+", "", cleaned)
    cleaned = cleaned.replace("**", "")
    cleaned = re.sub(r"\[cite:\s*[^\]]+\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.strip("- ").strip()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def _clean_section_items(items: list[str], limit: int) -> list[str]:
    cleaned = [_clean_research_text(item) for item in items]
    cleaned = [item for item in cleaned if item and item.lower() != "sources:"]
    return _dedupe_keep_order(cleaned)[:limit]


def _extract_domain(value: str) -> str | None:
    match = re.search(r"https?://[^\s)]+", value)
    if not match:
        return None
    parsed = urlparse(match.group(0))
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if "vertexaisearch.cloud.google.com" in domain:
        return None
    return domain or None


def _normalize_source_entry(value: str) -> str | None:
    cleaned = _clean_research_text(value)
    if not cleaned:
        return None
    if cleaned.lower() == "sources:":
        return None
    if "vertexaisearch.cloud.google.com" in cleaned.lower():
        return None
    domain = _extract_domain(cleaned)
    if domain:
        label = cleaned.split(":", 1)[0].strip()
        label = re.sub(r"^\d+\.\s+", "", label).strip()
        if label and label.lower() != domain:
            return f"{label} ({domain})"
        return domain
    return cleaned


def _clean_source_items(items: list[str], limit: int = 8) -> list[str]:
    normalized: list[str] = []
    seen_domains: set[str] = set()
    seen_labels: set[str] = set()
    for item in items:
        normalized_item = _normalize_source_entry(item)
        if not normalized_item:
            continue
        domain = _extract_domain(item)
        if domain and domain in seen_domains:
            continue
        label_key = normalized_item.lower()
        if label_key in seen_labels:
            continue
        if domain:
            seen_domains.add(domain)
        seen_labels.add(label_key)
        normalized.append(normalized_item)
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_sections(sections: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        "company snapshot": _clean_section_items(sections.get("company snapshot", []), 4),
        "operating thesis": _clean_section_items(sections.get("operating thesis", []), 4),
        "interview angles": _clean_section_items(sections.get("interview angles", []), 5),
        "tailored value proposition": _clean_section_items(sections.get("tailored value proposition", []), 4),
        "risks and open questions": _clean_section_items(sections.get("risks and open questions", []), 4),
        "sources": _clean_source_items(sections.get("sources", []), 8),
    }


def _summarize_report(company: str, sections: dict[str, list[str]]) -> str:
    company_snapshot = sections.get("company snapshot", [])
    operating = sections.get("operating thesis", [])
    if company_snapshot:
        return company_snapshot[0]
    if operating:
        return operating[0]
    return f"External research dossier for {company}."


def _artifact_basename(job: dict[str, Any]) -> str:
    company_slug = _slugify(job["company"], "company")
    role_slug = _slugify(job.get("role_title") or "role", "role")
    return f"{company_slug}--{role_slug}--{job['id']}"


def _write_artifact_from_report(job: dict[str, Any], report_text: str) -> tuple[Path, Path]:
    _ensure_storage_dirs()
    basename = _artifact_basename(job)
    markdown_path = ENRICHMENT_ARTIFACTS_DIR / f"{basename}.md"
    artifact_path = ENRICHMENT_ARTIFACTS_DIR / f"{basename}.json"
    sections = _normalize_sections(_parse_markdown_sections(report_text))
    created_at = _now_iso()
    markdown_path.write_text(report_text.rstrip() + "\n")
    artifact = {
        "id": _stable_id("prep", _relative_to_project(markdown_path)),
        "kind": "company_dossier",
        "title": f"{job['company']} External Research Dossier",
        "status": "draft",
        "company": job["company"],
        "opportunity_id": job.get("opportunity_id"),
        "topic_tags": ["company_prep", "external_research"],
        "source_paths": [_relative_to_project(markdown_path)],
        "summary": _summarize_report(job["company"], sections),
        "content_path": _relative_to_project(markdown_path),
        "structured_data": {
            "company_context": sections.get("company snapshot", []),
            "operating_thesis": sections.get("operating thesis", []),
            "interview_angles": sections.get("interview angles", []),
            "tailored_value_props": sections.get("tailored value proposition", []),
            "interview_stage_tags": ["recruiter_screen", "hiring_manager", "final_round"],
            "prep_tags": ["company_research", "external_research"],
            "follow_on_actions": [],
            "risks_and_open_questions": sections.get("risks and open questions", []),
            "source_citations": sections.get("sources", []),
        },
        "related_signal_ids": [],
        "created_at": created_at,
        "updated_at": created_at,
    }
    with open(artifact_path, "w") as f:
        json.dump(artifact, f, indent=2, sort_keys=True)
        f.write("\n")
    return markdown_path, artifact_path


def _finalize_completed_job(job: dict[str, Any], report_text: str) -> None:
    markdown_path, artifact_path = _write_artifact_from_report(job, report_text)
    job["report_path"] = _relative_to_project(markdown_path)
    job["artifact_path"] = _relative_to_project(artifact_path)
    job["status"] = "completed"
    job["error"] = None
    job["completed_at"] = _now_iso()
    job["updated_at"] = _now_iso()


def _poll_jobs(args: argparse.Namespace) -> int:
    queue = _load_queue()
    target_jobs = queue["jobs"]
    if args.job_id:
        job = _find_job(queue, args.job_id)
        if not job:
            print(f"Error: unknown job {args.job_id}", file=sys.stderr)
            return 1
        target_jobs = [job]

    submitted = [job for job in target_jobs if job.get("status") == "submitted" and job.get("interaction_id")]
    if not submitted:
        print("No submitted research jobs to poll.")
        return 0

    for job in submitted:
        try:
            result = _get_json(f"{GEMINI_API_BASE.rstrip('/')}/interactions/{job['interaction_id']}")
            status = (result.get("status") or "").lower()
            if status == "completed":
                report_text = _extract_report_text(result)
                _finalize_completed_job(job, report_text)
                print(f"Completed {job['id']}")
            elif status == "failed":
                job["status"] = "failed"
                job["error"] = str(result.get("error") or "interaction failed")
                job["updated_at"] = _now_iso()
                print(f"Failed {job['id']}: {job['error']}", file=sys.stderr)
            else:
                job["updated_at"] = _now_iso()
                print(f"{job['id']}: {status or 'pending'}")
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["updated_at"] = _now_iso()
            print(f"Failed to poll {job['id']}: {exc}", file=sys.stderr)
    _save_queue(queue)
    return 0


def _ingest_report(args: argparse.Namespace) -> int:
    queue = _load_queue()
    job = _find_job(queue, args.job_id)
    if not job:
        print(f"Error: unknown job {args.job_id}", file=sys.stderr)
        return 1
    report_path = Path(args.report_file).expanduser().resolve()
    if not report_path.exists():
        print(f"Error: report file not found: {report_path}", file=sys.stderr)
        return 1
    report_text = report_path.read_text()
    _finalize_completed_job(job, report_text)
    _save_queue(queue)
    print(job["artifact_path"])
    return 0


def _refresh_job(args: argparse.Namespace) -> int:
    queue = _load_queue()
    job = _find_job(queue, args.job_id)
    if not job:
        print(f"Error: unknown job {args.job_id}", file=sys.stderr)
        return 1
    report_path = job.get("report_path")
    if not report_path:
        print(f"Error: job {args.job_id} does not have a stored report yet.", file=sys.stderr)
        return 1
    path = PROJECT_ROOT / report_path
    if not path.exists():
        print(f"Error: stored report missing: {path}", file=sys.stderr)
        return 1
    report_text = path.read_text()
    markdown_path, artifact_path = _write_artifact_from_report(job, report_text)
    job["report_path"] = _relative_to_project(markdown_path)
    job["artifact_path"] = _relative_to_project(artifact_path)
    job["updated_at"] = _now_iso()
    _save_queue(queue)
    print(job["artifact_path"])
    return 0


def _apply_job(args: argparse.Namespace) -> int:
    queue = _load_queue()
    job = _find_job(queue, args.job_id)
    if not job:
        print(f"Error: unknown job {args.job_id}", file=sys.stderr)
        return 1
    artifact_path = job.get("artifact_path")
    if not artifact_path:
        print(f"Error: job {args.job_id} does not have a parsed artifact yet.", file=sys.stderr)
        return 1
    path = PROJECT_ROOT / artifact_path
    if not path.exists():
        print(f"Error: artifact file missing: {path}", file=sys.stderr)
        return 1
    with open(path) as f:
        artifact = json.load(f)
    artifact["status"] = "active"
    artifact["updated_at"] = _now_iso()
    with open(path, "w") as f:
        json.dump(artifact, f, indent=2, sort_keys=True)
        f.write("\n")
    job["status"] = "applied"
    job["applied_at"] = _now_iso()
    job["updated_at"] = _now_iso()
    _save_queue(queue)
    print(artifact["id"])
    return 0


def _list_jobs(_: argparse.Namespace) -> int:
    queue = _load_queue()
    if not queue["jobs"]:
        print("No research enrichment jobs.")
        return 0
    for job in sorted(queue["jobs"], key=lambda item: item.get("created_at", "")):
        print(
            f"{job['id']}  {job['status']}  {job['company']}"
            + (f" / {job['role_title']}" if job.get("role_title") else "")
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage optional company research enrichment jobs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    queue_parser = subparsers.add_parser("queue", help="Queue a company research job.")
    queue_parser.add_argument("--company")
    queue_parser.add_argument("--role-title")
    queue_parser.add_argument("--opportunity-id")
    queue_parser.add_argument("--context")
    queue_parser.set_defaults(func=_queue_job)

    list_parser = subparsers.add_parser("list", help="List queued or completed research jobs.")
    list_parser.set_defaults(func=_list_jobs)

    start_parser = subparsers.add_parser("start", help="Submit queued jobs to Gemini Deep Research.")
    start_parser.add_argument("--job-id")
    start_parser.set_defaults(func=_start_jobs)

    poll_parser = subparsers.add_parser("poll", help="Poll submitted jobs and parse completed reports.")
    poll_parser.add_argument("--job-id")
    poll_parser.set_defaults(func=_poll_jobs)

    ingest_parser = subparsers.add_parser("ingest-report", help="Ingest a local markdown report into a queued job.")
    ingest_parser.add_argument("--job-id", required=True)
    ingest_parser.add_argument("--report-file", required=True)
    ingest_parser.set_defaults(func=_ingest_report)

    refresh_parser = subparsers.add_parser("refresh", help="Rebuild an artifact from the stored report using the latest cleanup logic.")
    refresh_parser.add_argument("--job-id", required=True)
    refresh_parser.set_defaults(func=_refresh_job)

    apply_parser = subparsers.add_parser("apply", help="Activate a parsed research artifact so sync can use it.")
    apply_parser.add_argument("--job-id", required=True)
    apply_parser.set_defaults(func=_apply_job)

    auto_parser = subparsers.add_parser(
        "auto",
        help="Queue missing research for active opportunities and start jobs if GEMINI_API_KEY is configured.",
    )
    auto_parser.add_argument("--no-start", action="store_true")
    auto_parser.add_argument("--include-contacted", action="store_true")
    auto_parser.add_argument("--limit", type=int, default=3)
    auto_parser.set_defaults(func=_auto_queue_and_start)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

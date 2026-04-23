"""Export applypilot's SQLite jobs table as Opportunity JSON entities.

Phase 3 of the job-hunt unification plan. One-way projection: SQLite (source
of truth) -> JSON files in linkedin-leads/data/entities/opportunities/. Every
record written conforms to linkedin-leads/schemas/opportunity.schema.json
(additionalProperties: false, so only schema-declared keys are emitted).

The ID scheme matches linkedin-leads's _stable_id helper in
pipeline/sync_entities.py: ``opp_<slug24>_<sha1[0:12]>``. This means the
same (company, role_title) pair hashes to the same ID on both sides,
enabling future cross-linking (a LinkedIn DM and an outbound application
targeting the same role collide to one Opportunity).

Idempotent: re-runs overwrite the JSON in full, preserving the stable ID.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Schema enums live in jobhunt_core.entities. The sets below are used only
# for the defensive coercion path (unexpected BAP state -> fallback enum).
# They stay hard-coded here to avoid a heavier pydantic import just for
# membership checks.

_STATUS_ENUM = {
    "discovered", "contacted", "applied", "screening",
    "interviewing", "offer", "rejected", "withdrawn", "archived",
}
_SOURCE_ENUM = {
    "linkedin", "company_site", "job_board", "referral", "manual", "other",
}

# --- ID generation (mirrors linkedin-leads/pipeline/sync_entities.py) ---

def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def _stable_id(prefix: str, *parts: Any) -> str:
    normalized = [str(part).strip() for part in parts if str(part).strip()]
    base = "||".join(normalized) if normalized else prefix
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    label = _slugify(normalized[0], prefix) if normalized else prefix
    return f"{prefix}_{label[:24]}_{digest}"


def opportunity_id(company: str, role_title: str) -> str:
    """Stable Opportunity ID from (company, role_title). Matches linkedin-leads."""
    return _stable_id("opp", company or "", role_title or "")


# --- Target directory resolution ---

def entities_dir() -> Path:
    """Resolve the target directory for entity JSON writes.

    Precedence:
      1. ``JOBHUNT_ENTITIES_DIR`` env var.
      2. Default: ``~/Desktop/github/linkedin-leads/data/entities``.
    """
    override = os.environ.get("JOBHUNT_ENTITIES_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Desktop" / "github" / "linkedin-leads" / "data" / "entities"


# --- Status + source derivation ---

def _derive_status(job: dict[str, Any]) -> str:
    """Map BAP jobs row state to Opportunity schema status enum.

    The schema has no 'scored' or 'tailored' — both pre-apply states map to
    'discovered'. Only actual submission transitions the record onwards.
    """
    apply_status = (job.get("apply_status") or "").lower()
    if apply_status == "applied":
        return "applied"
    if apply_status in {"expired", "archived"}:
        return "archived"
    if apply_status in {"failed", "captcha", "login_issue"}:
        # Permanent failures — archive so the record doesn't re-queue in
        # cross-system dashboards. Soft failures (retryable) stay discovered.
        return "archived"
    return "discovered"


def _derive_source(site: str | None, strategy: str | None) -> str:
    """Map BAP site+strategy fields to Opportunity schema source enum."""
    site_l = (site or "").lower()
    strategy_l = (strategy or "").lower()

    if "linkedin" in site_l or "linkedin" in strategy_l:
        return "linkedin"
    if strategy_l in {"workday", "smartextract"} or "career" in site_l:
        return "company_site"
    if strategy_l in {"jobspy"}:
        return "job_board"
    if site_l in {"indeed", "glassdoor", "ziprecruiter", "dice", "wellfound"}:
        return "job_board"
    if site_l:
        # Any other scraped static board -> job_board is the reasonable default.
        return "job_board"
    return "other"


# --- Record construction ---

def _summarize(text: str | None, limit: int = 600) -> str | None:
    if not text:
        return None
    text = text.strip()
    return (text[:limit] + "...") if len(text) > limit else text


def _scale_fit_score(raw: Any) -> float | None:
    """BAP uses 1-10; schema accepts 0-100. Multiply by 10, clamp to range."""
    if raw is None:
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, n * 10.0))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_opportunity(job: dict[str, Any]) -> dict[str, Any]:
    """Build a schema-valid Opportunity dict from a BAP jobs row.

    Leaves cross-link arrays (lead_ids, conversation_ids, application_ids,
    prep_artifact_ids, signal_ids) empty — those are populated by
    linkedin-leads's own pipeline when recruiter threads or interview
    events surface and reference the same company+role.
    """
    company = job.get("site") or "Unknown"
    role_title = job.get("title") or "Unknown"

    status = _derive_status(job)
    source = _derive_source(job.get("site"), job.get("strategy"))

    created = job.get("discovered_at") or _now_iso()

    return {
        "id": opportunity_id(company, role_title),
        "company": company,
        "role_title": role_title,
        "source": source,
        "status": status,
        "location": job.get("location") or None,
        "compensation_hints": job.get("salary") or None,
        "industry": None,
        "fit_score": _scale_fit_score(job.get("fit_score")),
        "priority_score": None,
        "lead_ids": [],
        "conversation_ids": [],
        "application_ids": [],
        "interview_loop_id": None,
        "prep_artifact_ids": [],
        "signal_ids": [],
        "job_url": job.get("application_url") or job.get("url") or None,
        "description_summary": _summarize(job.get("full_description") or job.get("description")),
        "next_action": None,
        "created_at": created,
        "updated_at": _now_iso(),
    }


# --- Writers ---

def export_opportunity(job: dict[str, Any], target_dir: Path | None = None) -> Path:
    """Write a single Opportunity JSON file. Returns the file path.

    Delegates pydantic validation, merge-with-existing, and file I/O to
    ``jobhunt_core.store.write_opportunity``. The jobhunt_core layer
    enforces the schema (``additionalProperties: false`` via pydantic
    ``extra='forbid'``), handles status-downgrade protection, and
    preserves linkedin-leads-populated cross-link fields.
    """
    from jobhunt_core.store import write_opportunity

    record = build_opportunity(job)

    # Defensive enum coercion — catch drift between BAP and schema early.
    # Strictly speaking jobhunt_core will raise ValidationError on bad enums,
    # but a warn-and-coerce keeps pipeline stages forward-compatible when a
    # new apply_status value appears that we haven't mapped yet.
    if record["status"] not in _STATUS_ENUM:
        log.warning(
            "Opportunity status %r not in schema enum; coercing to 'discovered'",
            record["status"],
        )
        record["status"] = "discovered"
    if record["source"] not in _SOURCE_ENUM:
        log.warning(
            "Opportunity source %r not in schema enum; coercing to 'other'",
            record["source"],
        )
        record["source"] = "other"

    return write_opportunity(record, target_dir or entities_dir())


def export_all_opportunities(jobs: Iterable[dict[str, Any]], target_dir: Path | None = None) -> dict[str, int]:
    """Batch export. Returns ``{"written": N, "errors": M}``."""
    written = errors = 0
    for job in jobs:
        try:
            export_opportunity(job, target_dir)
            written += 1
        except Exception as exc:  # pragma: no cover — defensive
            errors += 1
            log.warning(
                "Failed to export opportunity for url=%s: %s",
                job.get("url", "?"), exc,
            )
    return {"written": written, "errors": errors}


def sync_from_db(min_fit_score: int = 0, target_dir: Path | None = None) -> dict[str, int]:
    """Read applypilot's SQLite jobs table and export every row above threshold.

    Called from pipeline stage hooks and from the ``applypilot sync entities``
    CLI. Respects ``min_fit_score`` so early-pipeline unscored jobs aren't
    noise-exported. Passing 0 exports everything.
    """
    from applypilot.database import get_connection

    conn = get_connection()
    if min_fit_score > 0:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE fit_score IS NOT NULL AND fit_score >= ?",
            (min_fit_score,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs").fetchall()

    jobs = [dict(r) for r in rows] if rows and not isinstance(rows[0], dict) else list(rows)
    return export_all_opportunities(jobs, target_dir)

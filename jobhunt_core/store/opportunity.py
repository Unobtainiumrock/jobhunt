"""Entity I/O helpers: read existing JSON, merge, write schema-valid JSON.

Phase 4a scope: Opportunity read/merge/write. BetterApplyPilot's
``sync/entity_exporter.py`` calls into here so merge logic and schema
enforcement live in one place rather than being reimplemented per consumer.

Phase 4b will extend this with SQLite accessor + migrations (currently
still in BetterApplyPilot's ``database.py``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jobhunt_core.entities import Opportunity, OpportunityStatus

log = logging.getLogger(__name__)

# Rank used for status-downgrade protection on merge. Higher = later in
# lifecycle. Terminal states share a high rank so a record in rejected /
# withdrawn / archived is never pulled back to an earlier state.
_STATUS_RANK: dict[OpportunityStatus, int] = {
    OpportunityStatus.DISCOVERED: 0,
    OpportunityStatus.CONTACTED: 1,
    OpportunityStatus.APPLIED: 2,
    OpportunityStatus.SCREENING: 3,
    OpportunityStatus.INTERVIEWING: 4,
    OpportunityStatus.OFFER: 5,
    OpportunityStatus.REJECTED: 9,
    OpportunityStatus.WITHDRAWN: 9,
    OpportunityStatus.ARCHIVED: 9,
}

# Fields populated by linkedin-leads's inbound pipeline. BetterApplyPilot
# must never overwrite these on re-export — they represent cross-system
# state (recruiter threads, scheduled interviews) that the outbound side
# has no visibility into.
_LINKEDIN_OWNED_FIELDS = (
    "lead_ids",
    "conversation_ids",
    "application_ids",
    "interview_loop_id",
    "prep_artifact_ids",
    "signal_ids",
    "next_action",
    "industry",
    "priority_score",
)


def merge_opportunity(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge an incoming Opportunity dict with a pre-existing one on disk.

    - linkedin-leads-owned cross-link / narrative fields: kept from existing.
    - Status: later-lifecycle rank wins; terminal states are sticky.
    - ``created_at``: earliest preserved (first-seen timestamp).
    - All other BAP-owned fields refreshed from the incoming snapshot.
    """
    merged = dict(incoming)
    for field in _LINKEDIN_OWNED_FIELDS:
        if existing.get(field) not in (None, [], ""):
            merged[field] = existing[field]

    old_status = existing.get("status")
    new_status = incoming.get("status")
    old_rank = _rank(old_status)
    new_rank = _rank(new_status)
    if old_rank > new_rank:
        merged["status"] = old_status

    if existing.get("created_at") and (
        not incoming.get("created_at") or existing["created_at"] < incoming["created_at"]
    ):
        merged["created_at"] = existing["created_at"]
    return merged


def _rank(status_value: Any) -> int:
    try:
        return _STATUS_RANK[OpportunityStatus(status_value)]
    except (ValueError, KeyError):
        return -1


def write_opportunity(record: dict[str, Any], target_dir: Path) -> Path:
    """Validate + merge-if-exists + write Opportunity JSON to disk.

    Args:
        record: Opportunity dict. Must validate against the pydantic model.
        target_dir: Directory that should contain the ``opportunities/``
            subdir. Usually ``linkedin-leads/data/entities``.

    Returns:
        Path of the written file, ``<target_dir>/opportunities/<id>.json``.
    """
    base = Path(target_dir) / "opportunities"
    base.mkdir(parents=True, exist_ok=True)

    # Pydantic validation surfaces schema drift (enum mismatch, extra keys,
    # out-of-range fit_score) as a ValidationError before we touch disk.
    validated = Opportunity.model_validate(record).to_schema_dict()

    path = base / f"{validated['id']}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            validated = merge_opportunity(existing, validated)
            # Re-validate after merge so the on-disk result is always schema-valid.
            validated = Opportunity.model_validate(validated).to_schema_dict()
        except (OSError, ValueError) as exc:
            log.warning(
                "Could not merge existing %s (%s); overwriting with fresh record",
                path.name, exc,
            )

    with open(path, "w", encoding="utf-8") as f:
        json.dump(validated, f, indent=2, sort_keys=True)
        f.write("\n")
    return path

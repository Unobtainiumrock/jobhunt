"""Storage layer: entity JSON projection + relational SQL schemas.

Public API re-exported here so callers can write
``from jobhunt_core.store import write_opportunity`` regardless of how
the internal module split evolves. Same rule for the monorepo migration
in Phase 9 — `git subtree add packages/jobhunt_core/` keeps these
import paths working verbatim.
"""

from jobhunt_core.store.opportunity import (
    merge_opportunity,
    write_opportunity,
)
from jobhunt_core.store.jobs import (
    JOBS_COLUMN_REGISTRY,
    SOURCE_RUNS_COLUMN_REGISTRY,
    create_jobs_table,
    create_source_runs_table,
    ensure_jobs_columns,
    ensure_source_runs_columns,
)

__all__ = [
    # Opportunity JSON projection
    "merge_opportunity",
    "write_opportunity",
    # Jobs SQL schema
    "JOBS_COLUMN_REGISTRY",
    "create_jobs_table",
    "ensure_jobs_columns",
    # Source-run tracking (skip-recent discover)
    "SOURCE_RUNS_COLUMN_REGISTRY",
    "create_source_runs_table",
    "ensure_source_runs_columns",
]

"""Jobs table: schema definition + forward migration.

Used by BetterApplyPilot's outbound pipeline as its primary state store
(one row per discovered job, stages walk across columns). Centralized
here so a future linkedin-leads integration or the Phase-9 monorepo can
import the same schema without duplicating the column list.

Design choices:

- One flat table, all columns declared up front. Stages add data by
  UPDATE; no per-stage join tables. Keeps the surface small for a
  single-user pipeline that runs stages roughly linearly.
- Forward-only migrations: ``ensure_jobs_columns`` adds missing columns
  but never renames or drops. Upgrading from any prior schema version is
  additive.
- The ``url`` column is the primary key. Attempting to INSERT a duplicate
  URL raises IntegrityError, which callers use for dedup during
  discovery.

The connection is passed in rather than created here so the caller owns
connection lifecycle (thread-local pooling, pragmas, WAL). jobhunt_core
stays DB-location-agnostic; applypilot decides where the file lives.
"""

from __future__ import annotations

import sqlite3


# Complete column registry: column_name -> SQL type (+ optional default).
# Single source of truth. Adding a column here causes it to appear in both
# fresh databases (via CREATE TABLE) and existing ones (via ALTER TABLE in
# ensure_jobs_columns). The order reflects pipeline stages left-to-right.
JOBS_COLUMN_REGISTRY: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Eligibility (deterministic, computed from location + user's eligibility
    # policy — separate dimension from fit_score, which is skill-only).
    # Values: "eligible" | "hybrid_abroad" | "remote_abroad_ok" |
    # "fully_ineligible" | NULL (not yet classified).
    "geo_fit": "TEXT",
    "geo_fit_reasoning": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Resumable-apply progress. Comma-separated list of stage markers the
    # agent has emitted via ``PROGRESS: stage=<name>`` lines. On retry,
    # the prompt builder reads this and tells the agent to skip ahead.
    "apply_progress": "TEXT",
}


def create_jobs_table(conn: sqlite3.Connection) -> None:
    """Create the jobs table if it doesn't exist, using ``JOBS_COLUMN_REGISTRY``.

    Safe to call on every startup — ``CREATE TABLE IF NOT EXISTS`` won't
    destroy existing data. Commits the transaction. Callers should follow
    up with ``ensure_jobs_columns`` if the database may have been created
    by an older schema version.
    """
    cols_sql = ",\n            ".join(
        f"{name} {dtype}" for name, dtype in JOBS_COLUMN_REGISTRY.items()
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS jobs (
            {cols_sql}
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# source_runs table — "when did we last scrape source X?" tracking so the
# discover stage can skip sources that ran within a recent-enough window.
# ---------------------------------------------------------------------------

SOURCE_RUNS_COLUMN_REGISTRY: dict[str, str] = {
    "source": "TEXT PRIMARY KEY",     # e.g. "jobspy", "workday", "smartextract"
    "last_ran_at": "TEXT",            # ISO 8601 UTC
    "last_jobs_found": "INTEGER",     # total rows the scraper saw
    "last_jobs_new": "INTEGER",       # rows inserted (rest = dupes by URL)
    "last_error": "TEXT",             # last error message if run failed
}


def create_source_runs_table(conn: sqlite3.Connection) -> None:
    """Create the source_runs table if it doesn't exist. Commits."""
    cols_sql = ",\n            ".join(
        f"{name} {dtype}" for name, dtype in SOURCE_RUNS_COLUMN_REGISTRY.items()
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS source_runs (
            {cols_sql}
        )
    """)
    conn.commit()


def ensure_source_runs_columns(conn: sqlite3.Connection) -> list[str]:
    """Forward-migrate source_runs. Returns list of columns added."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(source_runs)").fetchall()}
    added: list[str] = []
    for col, dtype in SOURCE_RUNS_COLUMN_REGISTRY.items():
        if col in existing or "PRIMARY KEY" in dtype:
            continue
        conn.execute(f"ALTER TABLE source_runs ADD COLUMN {col} {dtype}")
        added.append(col)
    if added:
        conn.commit()
    return added


def ensure_jobs_columns(conn: sqlite3.Connection) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Compares the current table schema (via ``PRAGMA table_info``) against
    ``JOBS_COLUMN_REGISTRY`` and issues ``ALTER TABLE ADD COLUMN`` for
    anything missing. PRIMARY KEY columns are skipped because SQLite
    doesn't support adding them after table creation; they're always
    present because ``create_jobs_table`` includes them at CREATE time.

    Returns the list of column names that were added (empty if the schema
    was already current). Commits the transaction if anything was added.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added: list[str] = []

    for col, dtype in JOBS_COLUMN_REGISTRY.items():
        if col in existing:
            continue
        if "PRIMARY KEY" in dtype:
            continue
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
        added.append(col)

    if added:
        conn.commit()
    return added

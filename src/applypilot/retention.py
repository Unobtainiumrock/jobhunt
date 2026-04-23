"""Retention sweep for tailored resumes and cover letters.

High-volume spray-applying generates a lot of per-job artifacts:
  - tailored_resumes/{prefix}.txt         (plain text resume)
  - tailored_resumes/{prefix}.pdf         (rendered PDF)
  - tailored_resumes/{prefix}_JOB.txt     (captured job description)
  - tailored_resumes/{prefix}_REPORT.json (validation report)
  - cover_letters/{prefix}_CL.txt         (plain text cover letter)
  - cover_letters/{prefix}_CL.pdf         (rendered cover letter PDF)

Each approved job produces ~6 files. Without retention, the user's
~/.applypilot/ tree grows unbounded. This module enforces two rolling
time-to-lives:

  - retention_days           (default 180) — unapplied jobs
  - retention_days_applied   (default 210) — applied jobs (longer window
    so proof-of-submission survives for audit / reapply)

Exposes:
  - purge_expired(dry_run=False) — entry point used by the `cleanup`
    pipeline stage and by the CLI.

Logic:
  1. DB pass. Walk rows whose tailored_at / cover_letter_at is older than
     the stage-appropriate cutoff (longer if applied_at IS NOT NULL).
     Delete the path and its related siblings, then NULL the timestamp
     and path columns so the stage is marked "not tailored" — the pipeline
     can re-tailor the job if it still qualifies.
  2. Orphan pass. Walk the tailored_resumes/ and cover_letters/ dirs and
     delete any file whose mtime is older than the *longer* cutoff and
     which is not referenced by a DB row. Using the longer cutoff here
     avoids accidentally nuking an orphan whose DB row was recently
     cleared but which is still within the applied window.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot.config import (
    APPLY_WORKER_DIR,
    COVER_LETTER_DIR,
    DEFAULTS,
    LOG_DIR,
    TAILORED_DIR,
)
from applypilot.database import get_connection

log = logging.getLogger(__name__)

# ── Sibling-file resolution ──────────────────────────────────────────────


def _resume_siblings(resume_path: Path) -> list[Path]:
    """All files generated for one tailored resume.

    tailor.py creates these under TAILORED_DIR for each approved job:
      {prefix}.txt, {prefix}.pdf, {prefix}_JOB.txt, {prefix}_REPORT.json
    The DB stores the .txt path; the others are derived from the same stem.
    """
    stem = resume_path.with_suffix("")
    return [
        resume_path,
        resume_path.with_suffix(".pdf"),
        Path(f"{stem}_JOB.txt"),
        Path(f"{stem}_REPORT.json"),
    ]


def _cover_siblings(cover_path: Path) -> list[Path]:
    """All files generated for one cover letter: {prefix}_CL.txt and its .pdf."""
    return [cover_path, cover_path.with_suffix(".pdf")]


def _unlink_if_exists(path: Path) -> int:
    """Delete a file if it exists. Return bytes freed, or 0."""
    try:
        if path.is_file():
            size = path.stat().st_size
            path.unlink()
            return size
    except OSError as e:
        log.warning("Could not delete %s: %s", path, e)
    return 0


# ── Timestamp parsing ────────────────────────────────────────────────────


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-format timestamp from the DB. Returns None if unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ── Main entry point ─────────────────────────────────────────────────────


def purge_expired(
    retention_days: int | None = None,
    retention_days_applied: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Delete tailored resumes and cover letters older than their TTL.

    Args:
        retention_days: TTL in days for unapplied jobs. Defaults to
            DEFAULTS["retention_days"] (180).
        retention_days_applied: TTL in days for applied jobs. Defaults to
            DEFAULTS["retention_days_applied"] (210).
        dry_run: When True, log what WOULD be deleted without touching disk or DB.

    Returns:
        {
            "db_pruned_resumes": int,
            "db_pruned_covers": int,
            "orphans_pruned": int,
            "bytes_freed": int,
            "dry_run": bool,
            "cutoff": ISO timestamp (unapplied),
            "cutoff_applied": ISO timestamp (applied),
            "retention_days": int,
            "retention_days_applied": int,
        }
    """
    days = retention_days if retention_days is not None else DEFAULTS["retention_days"]
    days_applied = (
        retention_days_applied
        if retention_days_applied is not None
        else DEFAULTS["retention_days_applied"]
    )
    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(days=days)
    cutoff_applied_dt = now - timedelta(days=days_applied)
    cutoff_iso = cutoff_dt.isoformat()
    cutoff_applied_iso = cutoff_applied_dt.isoformat()
    # Orphan sweep + log/worker sweep use the longer (applied) cutoff so we
    # don't delete files that would still be within an applied row's window,
    # and so logs — which are needed to debug apply failures — are kept for
    # the same duration as the artifacts they describe.
    cutoff_applied_mtime = cutoff_applied_dt.timestamp()

    tag = "[dry-run] " if dry_run else ""
    log.info(
        "%sRetention sweep: unapplied>%dd (cutoff=%s), applied>%dd (cutoff=%s)",
        tag, days, cutoff_iso, days_applied, cutoff_applied_iso,
    )

    conn = get_connection()

    db_resumes = 0
    db_covers = 0
    bytes_freed = 0

    # ── Pass 1: DB-driven ────────────────────────────────────────────────
    # Fetch any row that *could* be expired under its applicable cutoff.
    # The per-row cutoff picks the applied vs unapplied value based on
    # applied_at. ISO-8601 UTC strings sort chronologically, so lexical
    # comparison is safe (tailored_at set at tailor.py:559, cover_letter_at
    # at cover_letter.py:281).
    rows = conn.execute(
        """SELECT url, tailored_resume_path, tailored_at,
                  cover_letter_path, cover_letter_at, applied_at
             FROM jobs
            WHERE (tailored_at IS NOT NULL AND tailored_at < ?)
               OR (cover_letter_at IS NOT NULL AND cover_letter_at < ?)""",
        (cutoff_iso, cutoff_iso),
    ).fetchall()

    for row in rows:
        url = row["url"]
        tailored_path = row["tailored_resume_path"]
        tailored_at = row["tailored_at"]
        cover_path = row["cover_letter_path"]
        cover_at = row["cover_letter_at"]
        applied_at = row["applied_at"]

        # Per-row cutoff: applied rows get the longer retention window.
        row_cutoff_iso = cutoff_applied_iso if applied_at else cutoff_iso

        if tailored_path and tailored_at and tailored_at < row_cutoff_iso:
            for sibling in _resume_siblings(Path(tailored_path)):
                if dry_run:
                    if sibling.is_file():
                        bytes_freed += sibling.stat().st_size
                        log.info("%swould delete resume sibling: %s", tag, sibling)
                else:
                    bytes_freed += _unlink_if_exists(sibling)
            if not dry_run:
                conn.execute(
                    "UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL "
                    "WHERE url=?",
                    (url,),
                )
            db_resumes += 1
            log.info(
                "%sexpired tailored resume (job=%s, applied=%s, tailored_at=%s)",
                tag, url, bool(applied_at), tailored_at,
            )

        if cover_path and cover_at and cover_at < row_cutoff_iso:
            for sibling in _cover_siblings(Path(cover_path)):
                if dry_run:
                    if sibling.is_file():
                        bytes_freed += sibling.stat().st_size
                        log.info("%swould delete cover sibling: %s", tag, sibling)
                else:
                    bytes_freed += _unlink_if_exists(sibling)
            if not dry_run:
                conn.execute(
                    "UPDATE jobs SET cover_letter_path=NULL, cover_letter_at=NULL "
                    "WHERE url=?",
                    (url,),
                )
            db_covers += 1
            log.info(
                "%sexpired cover letter (job=%s, applied=%s, cover_letter_at=%s)",
                tag, url, bool(applied_at), cover_at,
            )

    if not dry_run and (db_resumes or db_covers):
        conn.commit()

    # ── Pass 2: Orphan sweep ─────────────────────────────────────────────
    # Any file on disk older than cutoff that is NOT referenced by any DB
    # row gets deleted. Handles manually-nulled rows and stragglers.
    referenced: set[str] = set()
    ref_rows = conn.execute(
        "SELECT tailored_resume_path, cover_letter_path FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL OR cover_letter_path IS NOT NULL"
    ).fetchall()
    for r in ref_rows:
        if r["tailored_resume_path"]:
            for sibling in _resume_siblings(Path(r["tailored_resume_path"])):
                referenced.add(str(sibling.resolve()))
        if r["cover_letter_path"]:
            for sibling in _cover_siblings(Path(r["cover_letter_path"])):
                referenced.add(str(sibling.resolve()))

    orphans = 0
    for directory in (TAILORED_DIR, COVER_LETTER_DIR):
        if not directory.exists():
            continue
        for f in directory.iterdir():
            if not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff_applied_mtime:
                continue
            if str(f.resolve()) in referenced:
                continue
            if dry_run:
                bytes_freed += f.stat().st_size
                log.info("%swould delete orphan: %s (mtime=%s)",
                         tag, f, datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat())
            else:
                bytes_freed += _unlink_if_exists(f)
                log.info("deleted orphan: %s", f)
            orphans += 1

    # ── Pass 3: LOG_DIR sweep ────────────────────────────────────────────
    # Prompt dumps and per-job Claude session logs contain full PII (resume,
    # profile, JD). Prune anything past the 210d window.
    logs_pruned = 0
    if LOG_DIR.exists():
        for f in LOG_DIR.iterdir():
            if not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff_applied_mtime:
                continue
            if dry_run:
                bytes_freed += f.stat().st_size
                log.info("%swould delete log: %s", tag, f)
            else:
                bytes_freed += _unlink_if_exists(f)
                log.info("deleted log: %s", f)
            logs_pruned += 1

    # ── Pass 4: APPLY_WORKER_DIR sweep ───────────────────────────────────
    # {dir}/current/ holds copies of uploaded resume PDFs named with the
    # user's real name. Walk recursively since workers nest their own
    # subdirs.
    workers_pruned = 0
    if APPLY_WORKER_DIR.exists():
        for f in APPLY_WORKER_DIR.rglob("*"):
            if not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff_applied_mtime:
                continue
            if dry_run:
                bytes_freed += f.stat().st_size
                log.info("%swould delete worker artifact: %s", tag, f)
            else:
                bytes_freed += _unlink_if_exists(f)
                log.info("deleted worker artifact: %s", f)
            workers_pruned += 1

    result = {
        "db_pruned_resumes": db_resumes,
        "db_pruned_covers": db_covers,
        "orphans_pruned": orphans,
        "logs_pruned": logs_pruned,
        "worker_artifacts_pruned": workers_pruned,
        "bytes_freed": bytes_freed,
        "dry_run": dry_run,
        "cutoff": cutoff_iso,
        "cutoff_applied": cutoff_applied_iso,
        "retention_days": days,
        "retention_days_applied": days_applied,
    }

    log.info(
        "%sRetention sweep done: %d resumes, %d covers, %d orphans, "
        "%d logs, %d worker artifacts, %s freed",
        tag, db_resumes, db_covers, orphans,
        logs_pruned, workers_pruned, _human_bytes(bytes_freed),
    )
    return result


def _human_bytes(n: int) -> str:
    """Format a byte count for log readability."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

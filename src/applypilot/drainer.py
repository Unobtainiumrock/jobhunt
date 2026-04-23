"""Laptop apply-drainer for Mode B (server-authoritative pipeline).

Phase 7 of the job-hunt unification plan. When ``APPLYPILOT_BACKEND=hetzner``
the server owns discover → pdf; apply still has to run on the laptop
because it needs Claude Code CLI + ATS-form Chrome that can't fit in
the Hetzner 3.7 GB VM alongside linkedin-leads. This module bridges
that split.

Loop:
  1. Atomic claim — SSH a ``UPDATE ... RETURNING`` against the server
     SQLite so a single row transitions to ``apply_status='in_progress'``
     and is handed back to us. No local DB is involved until step 3.
  2. Fetch artifacts — rsync the tailored resume + cover letter PDFs
     referenced by the claimed row into a laptop-local scratch dir.
  3. Run ``applypilot apply --url <url>`` locally against the laptop's
     Chrome / Claude Code CLI. The status this writes to
     ``~/.applypilot/applypilot.db`` is NOT the source of truth in Mode
     B — we read the result in step 4 and echo it to the server.
  4. Push result — SSH a second UPDATE that sets ``apply_status`` /
     ``applied_at`` / ``apply_error`` etc. on the server row we claimed.

Concurrency: only this drainer (and ``mark_result`` inside the apply
subprocess) writes to the server row during step 1–4. The server
pipeline cron (Mode B) writes the same row earlier (tailor / cover /
pdf stages) and never during apply. The claim UPDATE is atomic — if
two drainers ever race (they shouldn't — this is single-laptop), the
second one gets zero rows and sleeps.

Rate limiting: configurable ``poll_interval_sec`` (default 60) and
``per_hour_cap`` (default 20) so we don't hammer ATS sites.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


DEFAULT_REMOTE_HOST = "hetzner"
DEFAULT_REMOTE_DATA_DIR = "/opt/jobhunt/data"
DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_PER_HOUR_CAP = 20
DEFAULT_MIN_SCORE = 7


@dataclass
class DrainerConfig:
    remote_host: str = DEFAULT_REMOTE_HOST
    remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC
    per_hour_cap: int = DEFAULT_PER_HOUR_CAP
    min_score: int = DEFAULT_MIN_SCORE
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "DrainerConfig":
        return cls(
            remote_host=os.environ.get("JOBHUNT_REMOTE_SSH_HOST", DEFAULT_REMOTE_HOST),
            remote_data_dir=os.environ.get("JOBHUNT_REMOTE_DATA_DIR", DEFAULT_REMOTE_DATA_DIR),
            poll_interval_sec=int(os.environ.get("DRAINER_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_SEC)),
            per_hour_cap=int(os.environ.get("DRAINER_PER_HOUR_CAP", DEFAULT_PER_HOUR_CAP)),
            min_score=int(os.environ.get("DRAINER_MIN_SCORE", DEFAULT_MIN_SCORE)),
            dry_run=os.environ.get("DRAINER_DRY_RUN", "").lower() in {"1", "true", "yes"},
        )


@dataclass
class ClaimedJob:
    url: str
    title: str
    site: str
    application_url: str | None
    tailored_resume_path: str | None
    cover_letter_path: str | None
    fit_score: int | None


@dataclass
class DrainerStats:
    started_at: float = field(default_factory=time.time)
    claims: int = 0
    applied: int = 0
    failed: int = 0
    rate_limited: int = 0
    _apply_timestamps: list[float] = field(default_factory=list)

    def record_apply(self, success: bool) -> None:
        now = time.time()
        self._apply_timestamps.append(now)
        if success:
            self.applied += 1
        else:
            self.failed += 1

    def hourly_count(self) -> int:
        cutoff = time.time() - 3600
        self._apply_timestamps = [t for t in self._apply_timestamps if t >= cutoff]
        return len(self._apply_timestamps)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _remote_sqlite(host: str, db_path: str, sql: str, timeout: int = 30) -> list[list[str]]:
    """Run a SQL statement on the remote SQLite via SSH. Returns rows as
    a list of column lists (sqlite3's ``.mode list`` output).

    ``sql`` must NOT contain shell metacharacters beyond what sqlite3 itself
    parses. We pass it via stdin to avoid quoting nightmares.
    """
    cmd = ["ssh", host, f"sqlite3 -separator '|' {shlex.quote(db_path)}"]
    result = subprocess.run(
        cmd,
        input=sql,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"remote sqlite3 failed (exit {result.returncode}): {result.stderr.strip()[:200]}"
        )
    return [line.split("|") for line in result.stdout.splitlines() if line]


def _remote_rsync_pull(host: str, remote_path: str, local_path: str, timeout: int = 120) -> None:
    cmd = ["rsync", "-az", "--no-owner", "--no-group", f"{host}:{remote_path}", local_path]
    subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def claim_next_job(cfg: DrainerConfig) -> ClaimedJob | None:
    """Atomically claim the highest-fit ready-to-apply row on the server.

    Uses the same filter the laptop-local ``acquire_job`` uses:
      - ``tailored_resume_path IS NOT NULL``
      - ``application_url IS NOT NULL``
      - ``apply_status IS NULL`` (not already applied / in progress / failed)
      - ``fit_score >= cfg.min_score``

    ``RETURNING`` gives us the row in a single transaction so no-one else
    can grab the same row. ``agent_id`` is set to this laptop's hostname
    so review UI shows where the apply is running.
    """
    agent_id = f"drainer-{socket.gethostname()}"
    db_path = f"{cfg.remote_data_dir}/jobhunt.db"

    sql = f"""
.mode list
.separator |
BEGIN IMMEDIATE;
UPDATE jobs
SET apply_status = 'in_progress',
    agent_id = '{agent_id}',
    last_attempted_at = datetime('now')
WHERE url = (
    SELECT url FROM jobs
    WHERE tailored_resume_path IS NOT NULL
      AND application_url IS NOT NULL
      AND apply_status IS NULL
      AND fit_score >= {cfg.min_score}
    ORDER BY fit_score DESC, discovered_at ASC
    LIMIT 1
)
RETURNING url, title, site, application_url,
          tailored_resume_path, cover_letter_path, fit_score;
COMMIT;
"""
    rows = _remote_sqlite(cfg.remote_host, db_path, sql)
    if not rows:
        return None
    row = rows[0]
    return ClaimedJob(
        url=row[0],
        title=row[1],
        site=row[2],
        application_url=row[3] or None,
        tailored_resume_path=row[4] or None,
        cover_letter_path=row[5] or None,
        fit_score=int(row[6]) if row[6] else None,
    )


def fetch_artifacts(job: ClaimedJob, cfg: DrainerConfig, scratch_dir: Path) -> dict[str, Path | None]:
    """rsync the resume + cover letter PDFs this job refers to.

    ``tailored_resume_path`` / ``cover_letter_path`` from the server are
    container-relative paths (``/data/tailored_resumes/foo.pdf``). We
    map them to the host path (``/opt/jobhunt/data/tailored_resumes/foo.pdf``)
    and rsync locally.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, Path | None] = {"resume": None, "cover": None}

    for label, remote_ref in [("resume", job.tailored_resume_path), ("cover", job.cover_letter_path)]:
        if not remote_ref:
            continue
        # Map /data/... (container view) -> /opt/jobhunt/data/... (host view).
        if remote_ref.startswith("/data/"):
            host_path = remote_ref.replace("/data/", f"{cfg.remote_data_dir}/", 1)
        else:
            host_path = remote_ref
        local_path = scratch_dir / Path(host_path).name
        _remote_rsync_pull(cfg.remote_host, host_path, str(local_path))
        mapping[label] = local_path
    return mapping


def report_result(
    cfg: DrainerConfig,
    url: str,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Push apply outcome back to the server row we claimed."""
    db_path = f"{cfg.remote_data_dir}/jobhunt.db"
    error_sql = "NULL" if error is None else f"'{error.replace(chr(39), chr(39) * 2)[:500]}'"
    duration_sql = "NULL" if duration_ms is None else str(int(duration_ms))
    applied_clause = "applied_at = datetime('now')," if status == "applied" else ""
    attempts_clause = "apply_attempts = 99" if status in ("failed", "captcha", "login_issue") else "apply_attempts = COALESCE(apply_attempts, 0) + 1"

    sql = f"""
UPDATE jobs
SET apply_status = '{status}',
    {applied_clause}
    apply_error = {error_sql},
    apply_duration_ms = {duration_sql},
    {attempts_clause},
    agent_id = NULL
WHERE url = '{url.replace(chr(39), chr(39) * 2)}';
"""
    _remote_sqlite(cfg.remote_host, db_path, sql)


def release_stale_claim(cfg: DrainerConfig, url: str) -> None:
    """Reset apply_status to NULL on the server so a later drainer run can retry.

    Used when we crash or get interrupted between claim and report. Not
    called from the happy path — only from the cleanup hook.
    """
    db_path = f"{cfg.remote_data_dir}/jobhunt.db"
    sql = f"""
UPDATE jobs
SET apply_status = NULL, agent_id = NULL
WHERE url = '{url.replace(chr(39), chr(39) * 2)}' AND apply_status = 'in_progress';
"""
    _remote_sqlite(cfg.remote_host, db_path, sql)


# ---------------------------------------------------------------------------
# Apply invocation
# ---------------------------------------------------------------------------

def _run_applypilot_apply(job: ClaimedJob, dry_run: bool) -> tuple[str, str | None, int]:
    """Invoke ``applypilot apply --url <url>`` as a subprocess.

    Returns ``(status, error_msg, duration_ms)``. The subprocess uses the
    laptop's local ~/.applypilot/ for its own state (fine — it's the
    scratch area for this attempt) and ~/.claude/ for Claude Code auth.
    """
    start = time.time()
    cmd = ["applypilot", "apply", "--url", job.url, "--limit", "1"]
    if dry_run:
        cmd.append("--dry-run")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30-minute apply ceiling
        )
    except subprocess.TimeoutExpired:
        return ("failed", "drainer: subprocess timeout after 30min", int((time.time() - start) * 1000))

    duration_ms = int((time.time() - start) * 1000)
    output = (proc.stdout or "") + (proc.stderr or "")

    # Parse the apply result. The apply subprocess writes its status via
    # mark_result into the laptop-local DB; we inspect that instead of
    # string-matching stdout.
    try:
        from applypilot.database import get_connection
        row = get_connection().execute(
            "SELECT apply_status, apply_error FROM jobs WHERE url = ?", (job.url,),
        ).fetchone()
    except Exception as exc:  # pragma: no cover — defensive
        return ("failed", f"drainer: could not read local DB after apply: {exc}", duration_ms)

    if row is None:
        return ("failed", "drainer: row vanished from laptop DB after apply", duration_ms)

    status = row[0] or "failed"
    error = row[1]
    if proc.returncode != 0 and status not in ("applied", "failed", "captcha", "login_issue"):
        status = "failed"
        error = error or f"drainer: apply exited {proc.returncode}"
    return (status, error, duration_ms)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

class _Stopped(Exception):
    pass


def _install_signal_handlers() -> list[int]:
    """Install SIGTERM / SIGINT handlers that set a stop flag."""
    stop_flag: list[int] = [0]

    def _handler(signum, _frame):
        log.info("drainer: signal %s received, will stop after current iteration", signum)
        stop_flag[0] = 1

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handler)
    return stop_flag


def run_forever(cfg: DrainerConfig | None = None) -> None:
    """Main drainer loop. Blocks until SIGINT / SIGTERM."""
    cfg = cfg or DrainerConfig.from_env()
    stats = DrainerStats()
    stop_flag = _install_signal_handlers()
    log.info("drainer: starting, cfg=%s", cfg)

    claimed_but_unfinished: str | None = None
    try:
        while stop_flag[0] == 0:
            # Respect rate cap
            if stats.hourly_count() >= cfg.per_hour_cap:
                stats.rate_limited += 1
                log.info("drainer: hourly cap %d reached, sleeping", cfg.per_hour_cap)
                time.sleep(cfg.poll_interval_sec)
                continue

            try:
                job = claim_next_job(cfg)
            except Exception as exc:
                log.warning("drainer: claim failed: %s", exc)
                time.sleep(cfg.poll_interval_sec)
                continue

            if job is None:
                time.sleep(cfg.poll_interval_sec)
                continue

            claimed_but_unfinished = job.url
            stats.claims += 1
            log.info("drainer: claimed url=%s title=%s score=%s",
                     job.url, job.title[:50], job.fit_score)

            with tempfile.TemporaryDirectory(prefix="jobhunt-drainer-") as scratch:
                try:
                    fetch_artifacts(job, cfg, Path(scratch))
                except Exception as exc:
                    log.warning("drainer: artifact fetch failed: %s", exc)
                    report_result(cfg, job.url, "failed",
                                  error=f"drainer: artifact fetch: {exc}", duration_ms=0)
                    stats.record_apply(False)
                    claimed_but_unfinished = None
                    continue

                status, error, duration_ms = _run_applypilot_apply(job, cfg.dry_run)
                report_result(cfg, job.url, status, error=error, duration_ms=duration_ms)
                stats.record_apply(status == "applied")
                claimed_but_unfinished = None
                log.info("drainer: finished url=%s status=%s duration=%dms",
                         job.url, status, duration_ms)

    finally:
        if claimed_but_unfinished:
            try:
                release_stale_claim(cfg, claimed_but_unfinished)
                log.info("drainer: released stale claim on %s", claimed_but_unfinished)
            except Exception as exc:
                log.warning("drainer: failed to release stale claim on %s: %s",
                            claimed_but_unfinished, exc)
        elapsed = time.time() - stats.started_at
        log.info("drainer: stopped. claims=%d applied=%d failed=%d rate_limited=%d uptime=%.0fs",
                 stats.claims, stats.applied, stats.failed, stats.rate_limited, elapsed)

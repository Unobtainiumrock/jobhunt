"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Per-job wall-clock timeout. The agent subprocess is killed if it runs
# longer than this. Default 20 min accommodates TR-scale multi-page Workday
# forms (observed 11-20 min success cases) while still catching internet-
# crash hangs (observed >20 min stuck on browser_wait_for). Override via
# env var for debugging or for ATS portals known to be slower.
APPLY_WATCHDOG_SEC = int(os.environ.get("APPLY_WATCHDOG_SEC", "1200"))

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _gmail_mcp_instances() -> list[tuple[str, Path, Path, str]]:
    """Return the list of Gmail MCP instances to register.

    Each tuple is ``(mcp_name, oauth_path, credentials_path, email)``.
    ``email`` is the mailbox the instance reads (used by the prompt
    builder to match a site's login email against the right MCP). May be
    empty for the default instance if the user hasn't set ats.identity_extra.email.

    The default instance (name ``gmail``,
    ``~/.gmail-mcp/{gcp-oauth.keys,credentials}.json``) is always included.
    Additional instances come from ``~/.applypilot/gmail_mailboxes.yaml``
    — a list of mailboxes the user wants the agent to be able to read
    (e.g. a Berkeley edu account that receives LinkedIn verification
    codes). Each extra mailbox needs its own OAuth flow via
    ``node dist/index.js auth --scopes=gmail.readonly`` run with
    ``GMAIL_OAUTH_PATH`` and ``GMAIL_CREDENTIALS_PATH`` pointed at
    dedicated files.

    YAML shape:
        mailboxes:
          - name: gmail_berkeley         # becomes mcp__gmail_berkeley__*
            oauth: ~/.gmail-mcp-berkeley/gcp-oauth.keys.json
            credentials: ~/.gmail-mcp-berkeley/credentials.json
            email: nick@berkeley.edu    # optional, improves prompt routing
    """
    default_dir = Path.home() / ".gmail-mcp"
    # Default instance email comes from the loaded profile's ATS email.
    default_email = ""
    try:
        from applypilot.config import load_profile
        default_email = (load_profile().get("personal", {}) or {}).get("email", "")
    except Exception:
        pass
    instances: list[tuple[str, Path, Path, str]] = [
        ("gmail",
         default_dir / "gcp-oauth.keys.json",
         default_dir / "credentials.json",
         default_email),
    ]

    mbox_file = config.APP_DIR / "gmail_mailboxes.yaml"
    if mbox_file.exists():
        try:
            import yaml
            data = yaml.safe_load(mbox_file.read_text(encoding="utf-8")) or {}
            for entry in (data.get("mailboxes") or []):
                name = (entry.get("name") or "").strip()
                oauth = Path(entry["oauth"]).expanduser() if entry.get("oauth") else None
                creds = Path(entry["credentials"]).expanduser() if entry.get("credentials") else None
                email = (entry.get("email") or "").strip()
                if not (name and oauth and creds):
                    logger.warning("skipping malformed mailbox entry in %s: %s", mbox_file, entry)
                    continue
                if not name.startswith("gmail"):
                    # Namespace every Gmail instance under a ``gmail*`` prefix
                    # so the MCP tool names follow a predictable pattern the
                    # prompt can reference (mcp__<name>__search_emails).
                    name = f"gmail_{name}"
                if not (oauth.exists() and creds.exists()):
                    logger.warning(
                        "skipping mailbox %r: oauth/credentials files missing "
                        "(%s / %s)", name, oauth, creds,
                    )
                    continue
                instances.append((name, oauth, creds, email))
        except Exception:
            logger.exception("failed to parse %s; ignoring", mbox_file)
    return instances


def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port.

    The Gmail section enumerates every mailbox returned by
    ``_gmail_mcp_instances`` — at minimum the default ``gmail`` instance,
    plus any additional inboxes declared in
    ``~/.applypilot/gmail_mailboxes.yaml``.
    """
    gmail_server = str(Path.home() / "Desktop/github/Gmail-MCP-Server/dist/index.js")
    mcp_servers: dict = {
        "playwright": {
            "command": "npx",
            "args": [
                "@playwright/mcp@latest",
                f"--cdp-endpoint=http://localhost:{cdp_port}",
                f"--viewport-size={config.DEFAULTS['viewport']}",
            ],
        },
    }
    # Gmail MCP — invoked from a locally-built fork of
    # ArtyMcLabin/Gmail-MCP-Server (maintained hardened fork of the archived
    # upstream GongRzhe/Gmail-MCP-Server). Using a local build instead of
    # `npx` removes the supply-chain surface of auto-installing the npm
    # package at every run. Scopes are pinned at auth time.
    #
    # Each mailbox gets its own MCP server process with env-scoped
    # credential paths. The agent sees them as mcp__<name>__* tool names.
    for name, oauth_path, creds_path, _email in _gmail_mcp_instances():
        mcp_servers[name] = {
            "command": "node",
            "args": [gmail_server],
            "env": {
                "GMAIL_OAUTH_PATH": str(oauth_path),
                "GMAIL_CREDENTIALS_PATH": str(creds_path),
            },
        }
    return {"mcpServers": mcp_servers}


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, cooldown_hours: float = 1.0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
            When set, cooldown is bypassed (user explicit intent).
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        cooldown_hours: Skip rows whose ``last_attempted_at`` is within
            this window. Prevents the scheduler re-picking the same
            failing row back-to-back (observed 2026-04-24: 2 consecutive
            TR Senior Research Engineer retries both hit turn-budget).
            0 = no cooldown.

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            # Note: `apply_status != 'in_progress'` evaluates to NULL (falsy)
            # when apply_status IS NULL — so a fresh row would be filtered out.
            # Explicit NULL-or-not-in-progress predicate fixes that.
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            # Fix 1 (opportunity-level apply dedup): if ANY other row with
            # the same (site, title) has already been applied, skip this row
            # too. Prevents submitting to the same real position via
            # different URLs (e.g. LinkedIn listing vs. company Workday
            # portal — both map to the same opportunity ID via the stable
            # hash in jobhunt_core, but they are separate rows in jobs).
            #
            # geo_fit filter: skip rows the geo_fit classifier marked as
            # fully_ineligible (on-site/hybrid in a country outside the
            # user's authorized list). NULL geo_fit passes through — a row
            # discovered before the classifier ran should still be pickable;
            # the scorer/enrichment pipeline will populate it on next pass.
            # Cooldown filter: skip any row whose last_attempted_at is
            # within cooldown_hours of now. Prevents back-to-back retries
            # on the same failing row (bad ROI — if the first attempt hit
            # a structural wall like a long-form turn-budget, the second
            # attempt likely hits the same wall). 0 disables cooldown.
            cooldown_clause = ""
            if cooldown_hours and cooldown_hours > 0:
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(hours=cooldown_hours)
                ).isoformat()
                cooldown_clause = (
                    "AND (last_attempted_at IS NULL OR last_attempted_at < ?)"
                )
                params.append(cutoff)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  AND (geo_fit IS NULL OR geo_fit != 'fully_ineligible')
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs dupe
                      WHERE dupe.site  = jobs.site
                        AND dupe.title = jobs.title
                        AND dupe.applied_at IS NOT NULL
                  )
                  {site_clause}
                  {url_clauses}
                  {cooldown_clause}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def _sync_opportunity_for_url(url: str) -> None:
    """Best-effort: project the single Opportunity JSON for a url, then push remote.

    Called on every apply-stage transition (mark_result, mark_job). Failures
    in either step log and continue — the JSON projection and the remote
    rsync are downstream visibility, never reason to fail the actual apply.
    """
    try:
        from applypilot.sync.entity_exporter import export_opportunity
        conn = get_connection()
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        if row is None:
            return
        export_opportunity(dict(row))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Opportunity export failed for %s: %s", url, exc)

    try:
        from applypilot.sync.remote import push_now
        push_now()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Remote push after apply-transition for %s failed: %s", url, exc)


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()
    _sync_opportunity_for_url(url)


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()
    _sync_opportunity_for_url(url)


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            budget_per_job: float = 0.0) -> tuple[str, int, float]:
    """Spawn a Claude Code session for one job application.

    Args:
        budget_per_job: Max USD the claude subprocess may spend on this
            single job before Anthropic's billing layer terminates the
            call (via Claude CLI's ``--max-budget-usd``). 0 = unlimited.

    Returns:
        Tuple of (status_string, duration_ms, cost_usd). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
    ]
    if budget_per_job and budget_per_job > 0:
        # Claude CLI terminates the call with a budget-exceeded signal if
        # the cumulative API cost for this subprocess exceeds the cap.
        cmd.extend(["--max-budget-usd", f"{budget_per_job:.2f}"])
    # Principle-of-least-privilege: block every gmail tool except the two
    # applypilot actually needs (search_emails + read_email for pulling
    # sign-up verification codes). The `gmail.readonly` OAuth scope already
    # prevents write operations server-side; this list is a defense-in-depth
    # layer at the MCP boundary so prompt injection cannot attempt sends,
    # deletes, filter creation, or thread/inbox enumeration beyond the
    # narrow case of reading a verification email the agent already knows
    # the subject of. The ban list is generated per Gmail MCP instance so
    # that it covers every mailbox registered — each instance gets its own
    # prefix (e.g. ``mcp__gmail_berkeley__*``).
    _FORBIDDEN_GMAIL_TOOLS = (
        "batch_delete_emails", "batch_modify_emails",
        "create_filter", "create_filter_from_template", "create_label",
        "delete_email", "delete_filter", "delete_label",
        "download_attachment", "download_email", "draft_email",
        "get_filter", "get_inbox_with_threads", "get_or_create_label",
        "get_thread", "list_email_labels", "list_filters",
        "list_inbox_threads", "modify_email", "modify_thread",
        "reply_all", "send_email", "update_label",
    )
    disallowed = []
    for mbox_name, _, _, _ in _gmail_mcp_instances():
        for tool in _FORBIDDEN_GMAIL_TOOLS:
            disallowed.append(f"mcp__{mbox_name}__{tool}")
    cmd += [
        "--disallowedTools", ",".join(disallowed),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None
    watchdog_fired = threading.Event()
    watchdog: threading.Timer | None = None
    # Cost is populated from the claude stream-json `result` message; read it
    # late-bound from `stats` so every return path can surface the same value.
    def _cost() -> float:
        return float(stats.get("cost_usd", 0.0) or 0.0)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        # Wall-clock watchdog. The stdout-read loop below blocks on
        # `for line in proc.stdout:`, so if the agent stalls (Chrome
        # stuck on browser_wait_for during a network drop, MCP server
        # deadlock, etc.) the for-loop waits forever and the existing
        # `proc.wait(timeout=300)` at the bottom never runs. A timer
        # running in a separate thread kills the process tree after
        # APPLY_WATCHDOG_SEC, which causes the read loop to exit on EOF
        # and we can return a clean `failed:watchdog_timeout`.
        def _fire_watchdog() -> None:
            if proc is not None and proc.poll() is None:
                watchdog_fired.set()
                logger.warning(
                    "[W%d] Watchdog firing after %ds on job %r",
                    worker_id, APPLY_WATCHDOG_SEC, job.get("title", "?")[:40],
                )
                _kill_process_tree(proc.pid)

        watchdog = threading.Timer(APPLY_WATCHDOG_SEC, _fire_watchdog)
        watchdog.daemon = True
        watchdog.start()

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        # Regex for the resumable-apply progress protocol. The agent emits
        # `PROGRESS: stage=<name>` lines as it crosses each milestone; we
        # persist to jobs.apply_progress incrementally so a mid-flow crash
        # still leaves a resumption point for the next retry.
        progress_re = re.compile(r"PROGRESS:\s*stage=([A-Za-z0-9_]+)")
        progress_markers: list[str] = []

        def _persist_progress(marker: str) -> None:
            if marker in progress_markers:
                return
            progress_markers.append(marker)
            try:
                pconn = get_connection()
                pconn.execute(
                    "UPDATE jobs SET apply_progress = ? WHERE url = ?",
                    (",".join(progress_markers), job["url"]),
                )
                pconn.commit()
            except Exception:
                logger.debug("progress persist failed for %s", job["url"], exc_info=True)

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                                # Scan text block for PROGRESS markers and
                                # persist each new stage. Multiple markers
                                # may appear in a single block.
                                for m in progress_re.finditer(block["text"]):
                                    _persist_progress(m.group(1))
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

        # If the watchdog fired, treat as a hard timeout regardless of
        # whatever partial RESULT:* token the agent may have emitted
        # (e.g. a speculative RESULT:APPLIED before the submit confirm
        # would be a false positive if the kill happened mid-submit).
        if watchdog_fired.is_set():
            elapsed = int(time.time() - start)
            duration_ms = int((time.time() - start) * 1000)
            add_event(f"[W{worker_id}] WATCHDOG ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status="failed",
                         last_action=f"WATCHDOG ({elapsed}s)")
            return "failed:watchdog_timeout", duration_ms, _cost()

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000), _cost()

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms, _cost()

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms, _cost()
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms, _cost()
            return "failed:unknown", duration_ms, _cost()

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms, _cost()

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms, _cost()
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms, _cost()
    finally:
        if watchdog is not None:
            watchdog.cancel()
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                budget_per_job: float = 0.0,
                budget_total: float = 0.0,
                cooldown_hours: float = 1.0) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.
        budget_per_job: Hard cap on Anthropic spend per single job, in USD.
            Passed through to claude subprocess as `--max-budget-usd`.
            0 = unlimited (legacy behavior).
        budget_total: Hard cap on cumulative spend across all jobs this
            worker processes in this run. When crossed, worker exits
            cleanly with ``budget_exhausted`` last_action. 0 = unlimited.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    cumulative_cost = 0.0

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        # Total-budget guard before picking the next job. Check here so we
        # don't even launch Chrome for a job we'd immediately abort.
        if budget_total and budget_total > 0 and cumulative_cost >= budget_total:
            add_event(
                f"[W{worker_id}] Budget cap ${budget_total:.2f} reached "
                f"(spent ${cumulative_cost:.2f}) — stopping."
            )
            update_state(worker_id, status="done",
                         last_action=f"budget_exhausted (${cumulative_cost:.2f})")
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id,
                          cooldown_hours=(0 if target_url else cooldown_hours))
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms, cost_usd = run_job(
                job, port=port, worker_id=worker_id,
                model=model, dry_run=dry_run,
                budget_per_job=budget_per_job,
            )
            cumulative_cost += cost_usd

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         budget_per_job: float = 0.0,
         budget_total: float = 0.0,
         cooldown_hours: float = 1.0) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        budget_per_job: Max USD each claude subprocess may spend per job.
            Passed through as ``--max-budget-usd``. 0 = unlimited.
        budget_total: Max cumulative USD the worker(s) may spend in this
            run. Loop exits cleanly (``budget_exhausted``) when crossed.
            0 = unlimited. With multi-worker, applied independently per
            worker (each worker gets the full budget).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    budget_per_job=budget_per_job,
                    budget_total=budget_total,
                    cooldown_hours=cooldown_hours,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            budget_per_job=budget_per_job,
                            budget_total=budget_total,
                            cooldown_hours=cooldown_hours,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()

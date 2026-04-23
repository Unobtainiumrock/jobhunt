"""Best-effort rsync of local SQLite + JSON-entity state to a remote SSH host.

Phase 5 of the job-hunt unification plan. Every pipeline stage completion
and apply status transition in BetterApplyPilot calls ``push_checkpoint()``
so the Hetzner-side copy of ``/opt/jobhunt/data/`` stays within a few
seconds of the laptop's authoritative state. This is how "laptop dies →
deploy picks up on a new machine" works before Phase 6 inverts the
write direction.

Opt-in via ``JOBHUNT_REMOTE_SSH_HOST`` env var. Silent no-op when unset,
so development checkouts and non-deployed users don't accidentally push.

All failures are logged and swallowed. A sync error must never fail an
apply stage or score / tailor stage — the local state is always the
authoritative copy; the remote is a projection.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def push_checkpoint(
    db_path: str | Path | None = None,
    entities_dir: str | Path | None = None,
    remote_host: str | None = None,
    remote_dir: str | None = None,
) -> dict[str, Any]:
    """rsync the given local paths to ``<remote_host>:<remote_dir>/...``.

    Args:
        db_path: Local SQLite DB file. Synced to ``<remote_dir>/jobhunt.db``.
        entities_dir: Local entities dir (containing ``opportunities/``
            etc.). Synced into ``<remote_dir>/entities/`` (trailing-slash
            semantics; contents of ``entities_dir`` appear directly under
            remote ``entities/``, schema-mirrored).
        remote_host: SSH host alias or ``user@host``. Defaults to
            ``JOBHUNT_REMOTE_SSH_HOST`` env, or no-op if unset.
        remote_dir: Base directory on the remote. Defaults to
            ``JOBHUNT_REMOTE_DATA_DIR`` env, else ``/opt/jobhunt/data``.

    Returns:
        A dict describing what happened. ``{"skipped": True}`` when remote
        isn't configured. Otherwise keys ``db`` / ``entities`` each map to
        ``"ok"`` or ``"error: <reason>"``.
    """
    host = remote_host or os.environ.get("JOBHUNT_REMOTE_SSH_HOST", "").strip()
    if not host:
        return {"skipped": True, "reason": "JOBHUNT_REMOTE_SSH_HOST not set"}

    remote_base = (
        remote_dir
        or os.environ.get("JOBHUNT_REMOTE_DATA_DIR", "").strip()
        or "/opt/jobhunt/data"
    )
    result: dict[str, Any] = {"host": host, "remote_dir": remote_base}

    if db_path:
        db = Path(db_path)
        if db.exists():
            result["db"] = _rsync(str(db), f"{host}:{remote_base}/jobhunt.db", timeout=30)
        else:
            result["db"] = "skipped: local path missing"

    if entities_dir:
        ed = Path(entities_dir)
        if ed.exists():
            # Trailing slash on source = rsync copies directory CONTENTS
            # into the target, so the local and remote dirs mirror exactly.
            result["entities"] = _rsync(
                f"{str(ed)}/", f"{host}:{remote_base}/entities/", timeout=60,
            )
        else:
            result["entities"] = "skipped: local path missing"

    return result


def _rsync(src: str, dst: str, timeout: int) -> str:
    """Execute a single rsync transfer. Returns ``"ok"`` or ``"error: ..."``."""
    cmd = ["rsync", "-az", "--no-owner", "--no-group", src, dst]
    try:
        subprocess.run(
            cmd, check=True, capture_output=True, timeout=timeout,
        )
        return "ok"
    except FileNotFoundError:
        # rsync not installed on this machine. Non-recoverable; caller sees it.
        return "error: rsync not installed"
    except subprocess.TimeoutExpired:
        log.warning("rsync timed out after %ds: %s -> %s", timeout, src, dst)
        return f"error: timeout after {timeout}s"
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[:200]
        log.warning(
            "rsync failed (exit %d): %s -> %s: %s",
            exc.returncode, src, dst, stderr.strip(),
        )
        return f"error: exit {exc.returncode}"

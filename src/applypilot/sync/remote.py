"""Thin adapter: pushes applypilot's local state to the remote via
``jobhunt_core.sync_remote``.

Every stage-completion / apply-transition hook in applypilot calls
:func:`push_now`. It resolves applypilot-specific defaults (DB path from
``applypilot.config.DB_PATH``, entities dir from the exporter) and
delegates the actual rsync to jobhunt_core. Best-effort; failures log
and do not raise.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def push_now() -> dict:
    """Push the current local DB + entities state to the configured remote.

    No-op if ``JOBHUNT_REMOTE_SSH_HOST`` isn't set. Exception-safe — any
    rsync / import failure is logged and swallowed so no applypilot
    stage can ever fail because of a remote-sync issue.
    """
    try:
        from applypilot.config import DB_PATH
        from applypilot.sync.entity_exporter import entities_dir
        from jobhunt_core.sync_remote import push_checkpoint
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("remote-sync imports failed: %s", exc)
        return {"skipped": True, "reason": f"import error: {exc}"}

    try:
        result = push_checkpoint(
            db_path=DB_PATH,
            entities_dir=entities_dir(),
        )
        if not result.get("skipped"):
            log.info("remote-sync: %s", result)
        return result
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("remote-sync: unexpected error: %s", exc)
        return {"skipped": True, "reason": f"unexpected: {exc}"}

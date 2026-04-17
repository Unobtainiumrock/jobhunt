"""Shared subprocess entry for ``src/send-approved.mjs``.

Used by ``pipeline.review_server`` (bulk send + approve-and-send) and
``infra.telegram_bot`` (phone approvals). All invocations serialize on a
cross-container ``fcntl`` lock under ``data/.send_approved.lock`` so only one
Chrome/CDP send runs at a time.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

from pipeline.config import DATA_DIR, PROJECT_ROOT

SEND_SCRIPT = PROJECT_ROOT / "src" / "send-approved.mjs"
_SEND_LOCK_PATH = DATA_DIR / ".send_approved.lock"


def env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "enabled")


def sender_rate_limit_env() -> dict[str, str]:
    """Mirror of ``review_server`` logic: one knob → per-run delay envs."""
    extra: dict[str, str] = {}
    raw = os.environ.get("SENDER_RATE_LIMIT")
    if not raw:
        return extra
    try:
        per_hour = max(1, int(raw))
    except ValueError:
        return extra
    extra["LINKEDIN_MAX_SENDS_PER_RUN"] = str(per_hour)
    base_gap_ms = int((3600 / per_hour) * 1000 * 0.75)
    extra["LINKEDIN_SEND_DELAY_MIN"] = str(max(15000, base_gap_ms))
    extra["LINKEDIN_SEND_DELAY_MAX"] = str(max(30000, int(base_gap_ms * 1.6)))
    return extra


@contextmanager
def _send_flock() -> Iterator[None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_SEND_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def build_send_argv(
    *,
    only: str,
    max_items: int,
    live: bool,
    reply_urn: str | None = None,
    followup_task_id: str | None = None,
) -> list[str]:
    """Assemble a ``send-approved.mjs`` argv list (exact-URN / task-id aware)."""
    argv: list[str] = ["node", str(SEND_SCRIPT)]
    if live:
        argv.append("--live")
    if only in ("replies", "followups", "all"):
        argv.extend(["--only", only])
    if max_items > 0:
        argv.extend(["--max", str(max_items)])
    if reply_urn:
        argv.extend(["--reply-urn", reply_urn])
    if followup_task_id:
        argv.extend(["--followup-task-id", followup_task_id])
    return argv


def run_send_approved_with_lock(
    argv: list[str],
    *,
    timeout_sec: int = 60 * 30,
) -> subprocess.CompletedProcess[str]:
    """Run the sender under the shared flock (blocks until the lock is free)."""
    env = {**os.environ, **sender_rate_limit_env()}
    with _send_flock():
        return subprocess.run(
            argv,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
        )

"""Append-only JSONL for pipeline errors that healthdog scans.

The classify / generate / intent paths each catch `Exception` broadly and
return graceful fallback dicts so one bad LLM call doesn't nuke the whole
batch. That's the right behavior, but it also swallows real failures
(OpenAI 429, bad key, parse errors) silently. This module gives those
sites a 1-line way to persist each exception so healthdog can alert.

Failure of the logger itself must NOT break the caller — we swallow all
OSErrors and carry on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import DATA_DIR

PIPELINE_ERRORS_FILE = DATA_DIR / "pipeline_errors.jsonl"


def log_error(module: str, kind: str, detail: str) -> None:
    """Append one error record to pipeline_errors.jsonl.

    Args:
        module: which pipeline stage (e.g. "classify_leads", "generate_reply").
        kind: short exception class name or error category.
        detail: free-text, typically the exception message (trimmed).
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": module,
        "kind": kind,
        "detail": detail[:500],
        "pid": os.getpid(),
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PIPELINE_ERRORS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass

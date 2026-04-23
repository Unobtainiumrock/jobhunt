#!/usr/bin/env python3
"""
Compatibility wrapper around the canonical hunt briefing.

Usage:
  python -m pipeline.morning_briefing
  python -m pipeline.morning_briefing --json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from pipeline.hunt_briefing import build_hunt_briefing, print_hunt_briefing


def build_briefing(target_date: Any | None = None) -> dict[str, Any]:
    briefing = build_hunt_briefing()
    briefing.setdefault("date", datetime.now(timezone.utc).date().isoformat())
    briefing["briefing_type"] = "canonical_hunt"
    if target_date is not None:
        briefing["target_date"] = str(target_date)
    return briefing


def print_briefing(briefing: dict[str, Any]) -> None:
    print_hunt_briefing(briefing)


def main() -> None:
    briefing = build_briefing()
    if "--json" in sys.argv:
        print(json.dumps(briefing, indent=2))
    else:
        print_briefing(briefing)


if __name__ == "__main__":
    main()

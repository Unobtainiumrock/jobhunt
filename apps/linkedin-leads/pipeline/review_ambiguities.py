#!/usr/bin/env python3
"""
Report canonical opportunities that still need enrichment or overrides.

Usage:
  python -m pipeline.review_ambiguities
  python -m pipeline.review_ambiguities --json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pipeline.config import CONVERSATIONS_DIR, OPPORTUNITIES_DIR

PLACEHOLDER_COMPANIES = {"Unknown Company"}
PLACEHOLDER_ROLES = {"Unknown role", "Engineering Role", "Engineering Roles", "Engineering Opportunity"}


def _load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def build_report() -> dict[str, Any]:
    conversations = {
        record["id"]: record
        for record in _load_records(CONVERSATIONS_DIR)
    }
    ambiguous = []
    for record in _load_records(OPPORTUNITIES_DIR):
        if record.get("company") in PLACEHOLDER_COMPANIES or record.get("role_title") in PLACEHOLDER_ROLES:
            conversation = conversations.get(record["conversation_ids"][0], {}) if record.get("conversation_ids") else {}
            ambiguous.append({
                "opportunity_id": record["id"],
                "company": record.get("company"),
                "role_title": record.get("role_title"),
                "next_action": record.get("next_action"),
                "conversation_thread": conversation.get("external_thread_id"),
            })
    return {
        "ambiguous_count": len(ambiguous),
        "opportunities": ambiguous,
    }


def main() -> None:
    report = build_report()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
        return

    print(f"Ambiguous opportunities: {report['ambiguous_count']}")
    for item in report["opportunities"]:
        print(f"- {item['opportunity_id']}: {item['company']} / {item['role_title']}")
        print(f"  next_action: {item['next_action']}")


if __name__ == "__main__":
    main()

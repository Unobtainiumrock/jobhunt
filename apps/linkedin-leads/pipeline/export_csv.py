#!/usr/bin/env python3
"""
Phase 1B: CSV/Spreadsheet Export

Generates a CSV mapping conversations to extracted contact info, classification,
and scoring data.

Usage:
  python -m pipeline.export_csv
  python -m pipeline.export_csv --output /path/to/output.csv
"""

from __future__ import annotations

import csv
import json
import sys
from typing import Any

from pipeline.config import CLASSIFIED_FILE, INBOX_FILE, CONTACTS_CSV
from pipeline.extract_contacts import extract_all


CSV_HEADERS = [
    "Name",
    "Headline",
    "Classification",
    "Phone (Raw)",
    "Phone (E.164)",
    "Email",
    "Calendar Link",
    "Last Activity",
    "Score",
    "Status",
    "Role Title",
    "Company",
    "Conversation URN",
]


def _get_metadata(convo: dict[str, Any]) -> dict[str, Any]:
    """Pull metadata from classified conversation if available."""
    return convo.get("metadata", {})


def export(output_path: str | None = None) -> None:
    """Export contacts and classification data to CSV."""
    contacts = extract_all()

    # Load classified data for metadata
    source = CLASSIFIED_FILE if CLASSIFIED_FILE.exists() else INBOX_FILE
    with open(source) as f:
        data = json.load(f)

    metadata_by_urn: dict[str, dict[str, Any]] = {}
    score_by_urn: dict[str, Any] = {}
    for convo in data.get("conversations", []):
        urn = convo.get("conversationUrn", "")
        metadata_by_urn[urn] = convo.get("metadata") or {}
        score_by_urn[urn] = convo.get("score") or {}

    dest = output_path or str(CONTACTS_CSV)
    with open(dest, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)

        for c in contacts:
            urn = c.get("conversation_urn", "")
            meta = metadata_by_urn.get(urn, {})
            score_data = score_by_urn.get(urn, {})

            writer.writerow([
                c["name"],
                c["headline"],
                c["classification"],
                "; ".join(c.get("phones", [])),
                "; ".join(c.get("phones_e164", [])),
                "; ".join(c.get("emails", [])),
                "; ".join(c.get("calendar_links", [])),
                c.get("last_activity", ""),
                score_data.get("total", ""),
                score_data.get("status", ""),
                meta.get("role_title", ""),
                meta.get("company", ""),
                urn,
            ])

    print(f"Exported {len(contacts)} rows to {dest}")


def main() -> None:
    output = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output = sys.argv[idx + 1]
    export(output_path=output)


if __name__ == "__main__":
    main()

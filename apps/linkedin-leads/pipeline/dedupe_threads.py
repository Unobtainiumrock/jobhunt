#!/usr/bin/env python3
"""Connection-ack duplicate-thread collapser.

LinkedIn creates a fresh message thread when someone accepts a connection
request. That "stub" thread usually contains a single auto-generated message
(often just the original invite note) while the real conversation continues in
an older thread with the same profile. Treating the two as independent leads
fragments context and causes stale drafts.

This module runs between `scrape` and `classify`. It is strictly additive --
nothing is deleted. Duplicate stubs are flagged with:

    _duplicate_of: <canonical_conversation_urn>
    _category_override: "networking_stub"

so downstream steps (`classify_leads`, `generate_reply`, etc.) can skip them
without losing the raw record.

Matching rules, in priority order:

1. Same participant `profileUrn`  -> same person, merge.
2. Same normalized full name AND exactly one thread has size <= 1 message
   (the connection-ack signature). Two rich threads with no profile URL are
   left separate so we never accidentally fuse legitimately distinct people
   who happen to share a name (e.g. multiple "Sunil" contacts).

Usage:
    python -m pipeline.dedupe_threads
    python -m pipeline.dedupe_threads --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.config import INBOX_FILE, USER_NAME

STUB_MESSAGE_THRESHOLD = 1


@dataclass
class MergeGroup:
    """Resolved set of threads identified as the same counterparty."""

    key: str
    canonical_urn: str
    duplicate_urns: list[str]
    reason: str


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.lower().split())


def _other_participant(convo: dict[str, Any]) -> dict[str, Any] | None:
    for participant in convo.get("participants", []) or []:
        if (participant.get("name") or "") == USER_NAME:
            continue
        return participant
    return None


def _message_count(convo: dict[str, Any]) -> int:
    return len(convo.get("messages") or [])


def _group_by_profile(
    conversations: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Strongest signal: same counterparty profileUrn -> same person."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for convo in conversations:
        participant = _other_participant(convo)
        if not participant:
            continue
        profile_urn = (participant.get("profileUrn") or "").strip()
        if not profile_urn:
            continue
        groups[profile_urn].append(convo)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _group_by_name_with_stub(
    conversations: list[dict[str, Any]],
    already_grouped: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Weaker fallback: same normalized full name AND one side is a stub.

    Only fires when at least one thread has <= STUB_MESSAGE_THRESHOLD messages,
    which is the connection-ack signature. Without that safety check we would
    fuse distinct people who happen to share a name (e.g. Sunil Shintre vs.
    Sunil Phatak vs. Sunil S. -- three different real people in the same inbox).
    """
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for convo in conversations:
        if convo.get("conversationUrn") in already_grouped:
            continue
        participant = _other_participant(convo)
        if not participant:
            continue
        name_key = _normalize_name(participant.get("name"))
        if not name_key:
            continue
        by_name[name_key].append(convo)

    result: dict[str, list[dict[str, Any]]] = {}
    for name_key, threads in by_name.items():
        if len(threads) < 2:
            continue
        counts = sorted(_message_count(t) for t in threads)
        if counts[0] > STUB_MESSAGE_THRESHOLD:
            continue
        result[name_key] = threads
    return result


def _pick_canonical(threads: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        threads,
        key=lambda c: (
            _message_count(c),
            c.get("lastActivityAt") or "",
        ),
    )


def _resolve_merge_groups(conversations: list[dict[str, Any]]) -> list[MergeGroup]:
    groups: list[MergeGroup] = []
    grouped_urns: set[str] = set()

    for profile_urn, threads in _group_by_profile(conversations).items():
        canonical = _pick_canonical(threads)
        dup_urns = [c["conversationUrn"] for c in threads if c is not canonical]
        groups.append(
            MergeGroup(
                key=f"profile:{profile_urn}",
                canonical_urn=canonical["conversationUrn"],
                duplicate_urns=dup_urns,
                reason="same_profile_urn",
            )
        )
        for thread in threads:
            grouped_urns.add(thread["conversationUrn"])

    for name_key, threads in _group_by_name_with_stub(conversations, grouped_urns).items():
        canonical = _pick_canonical(threads)
        dup_urns = [c["conversationUrn"] for c in threads if c is not canonical]
        groups.append(
            MergeGroup(
                key=f"name:{name_key}",
                canonical_urn=canonical["conversationUrn"],
                duplicate_urns=dup_urns,
                reason="same_name_stub_detected",
            )
        )

    return groups


def apply_merges(
    conversations: list[dict[str, Any]],
    groups: list[MergeGroup],
) -> tuple[int, int]:
    """Mutate `conversations` in place, flagging stubs. Returns (flagged, refreshed)."""
    by_urn = {c["conversationUrn"]: c for c in conversations if c.get("conversationUrn")}
    flagged = 0
    refreshed = 0
    for group in groups:
        canonical = by_urn.get(group.canonical_urn)
        if canonical is None:
            continue
        if canonical.pop("_duplicate_of", None) is not None:
            refreshed += 1
        canonical.pop("_category_override", None)
        for dup_urn in group.duplicate_urns:
            dup = by_urn.get(dup_urn)
            if dup is None:
                continue
            dup["_duplicate_of"] = group.canonical_urn
            dup["_category_override"] = "networking_stub"
            dup["_dedupe_reason"] = group.reason
            flagged += 1
    return flagged, refreshed


def dedupe_inbox(inbox_path: Path, dry_run: bool = False) -> list[MergeGroup]:
    if not inbox_path.exists():
        print(f"Error: inbox file not found at {inbox_path}", file=sys.stderr)
        sys.exit(1)

    with open(inbox_path) as handle:
        data = json.load(handle)

    conversations = data.get("conversations") or []
    groups = _resolve_merge_groups(conversations)

    if not groups:
        print("No duplicate threads detected.")
        return groups

    flagged, refreshed = apply_merges(conversations, groups)
    print(f"Found {len(groups)} merge group(s); flagging {flagged} stub(s).")
    for group in groups:
        canonical_name = _other_participant(
            next(c for c in conversations if c.get("conversationUrn") == group.canonical_urn)
        )
        display = (canonical_name or {}).get("name", "?")
        print(f"  [{group.reason}] {display}: canonical={group.canonical_urn[-18:]} "
              f"duplicates={[u[-18:] for u in group.duplicate_urns]}")

    if dry_run:
        print("(dry run -- no file changes written)")
        return groups

    with open(inbox_path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"Updated {inbox_path}")
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print merges without writing.")
    parser.add_argument(
        "--inbox",
        default=str(INBOX_FILE),
        help=f"Path to inbox.json (default: {INBOX_FILE})",
    )
    args = parser.parse_args()
    dedupe_inbox(Path(args.inbox), dry_run=args.dry_run)


if __name__ == "__main__":
    main()

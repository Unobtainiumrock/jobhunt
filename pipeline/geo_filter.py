#!/usr/bin/env python3
"""Geographic compatibility gate for recruiter threads.

Reads a work-mode + location signal out of ``metadata.location`` and message
text, and compares against the user's ``preferences.geo_policy`` block in
``profile/user_profile.yaml``. A role that is explicitly on-site in a region
that is NOT in ``on_site_allowed_tokens`` (or is on the ``on_site_blocked_tokens``
deny list) is marked incompatible: ``score.logistics_fit`` is zeroed, the total
is capped below ``SCORE_AUTO_REPLY``, ``score.action`` flips to ``notify_gaps``,
and ``intent.abstain`` is set so ``generate_reply`` skips drafting.

Deterministic -- no LLM call. Safe to run on every pipeline invocation.

Usage:
    python -m pipeline.geo_filter               # rescore + retag every recruiter
    python -m pipeline.geo_filter --urn ...     # one thread
    python -m pipeline.geo_filter --dry-run     # report without mutating
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from pipeline.config import (
    CLASSIFIED_FILE,
    PROFILE_FILE,
    SCORE_AUTO_REPLY,
    SCORE_REVIEW,
    USER_NAME,
)

WorkMode = Literal["remote", "hybrid", "on_site", "unknown"]
Verdict = Literal["compatible", "incompatible", "unknown", "needs_clarification"]

_REMOTE_TOKENS = (
    "fully remote",
    "100% remote",
    "100 percent remote",
    "remote-first",
    "remote first",
    "remote only",
    "work from anywhere",
    "work-from-anywhere",
    "distributed team",
    "anywhere in the us",
)
_SOFT_REMOTE_TOKENS = (" remote ", " remote.", " remote,", "\nremote", "remote role", "remote position")
_HYBRID_TOKENS = (
    "hybrid",
    "2 days in office",
    "3 days in office",
    "2 days in-office",
    "3 days in-office",
    "in the office",
    "in-office",
    "in office",
)
_ONSITE_TOKENS = (
    "on-site",
    "onsite",
    "on site",
    "in-person",
    "in person",
    "hq-based",
    "hq based",
    "based in the office",
    "co-located",
    "colocated",
)

_NEGATED_REMOTE_CTX = (
    "not remote",
    "no remote",
    "not a remote",
    "isn't remote",
    "is not remote",
    "not fully remote",
)


@dataclass
class GeoPolicy:
    base_city: str = ""
    remote_ok: bool = True
    hybrid_requires_local: bool = True
    on_site_allowed: list[str] = field(default_factory=list)
    on_site_blocked: list[str] = field(default_factory=list)

    @classmethod
    def from_profile(cls, profile: dict[str, Any]) -> "GeoPolicy":
        block = (profile.get("preferences") or {}).get("geo_policy") or {}
        return cls(
            base_city=str(block.get("base_city") or ""),
            remote_ok=bool(block.get("remote_ok", True)),
            hybrid_requires_local=bool(block.get("hybrid_requires_local", True)),
            on_site_allowed=[
                str(t).strip().lower()
                for t in (block.get("on_site_allowed_tokens") or [])
                if str(t).strip()
            ],
            on_site_blocked=[
                str(t).strip().lower()
                for t in (block.get("on_site_blocked_tokens") or [])
                if str(t).strip()
            ],
        )


@dataclass
class GeoVerdict:
    verdict: Verdict
    work_mode: WorkMode
    reason: str
    location_text: str
    matched_allowed: list[str] = field(default_factory=list)
    matched_blocked: list[str] = field(default_factory=list)
    ask_location: bool = False
    evaluated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "work_mode": self.work_mode,
            "reason": self.reason,
            "location_text": self.location_text,
            "matched_allowed": self.matched_allowed,
            "matched_blocked": self.matched_blocked,
            "ask_location": self.ask_location,
            "evaluated_at": self.evaluated_at,
        }


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _gather_text(convo: dict[str, Any]) -> tuple[str, str]:
    """Return (location_field, haystack) for token matching."""
    meta = convo.get("metadata") or {}
    loc = str(meta.get("location") or "").strip()
    parts: list[str] = []
    if loc:
        parts.append(loc)
    desc = str(meta.get("role_description_summary") or "")
    if desc:
        parts.append(desc)
    for msg in convo.get("messages") or []:
        sender = str(msg.get("sender") or "")
        if sender == USER_NAME:
            continue
        text = str(msg.get("text") or "")
        if text:
            parts.append(text)
    haystack = _collapse(" ".join(parts))
    return loc, haystack


def _infer_work_mode(haystack: str) -> WorkMode:
    if any(tok in haystack for tok in _NEGATED_REMOTE_CTX):
        remote_hit = False
    else:
        remote_hit = any(tok in haystack for tok in _REMOTE_TOKENS)
        if not remote_hit:
            remote_hit = any(tok in (" " + haystack + " ") for tok in _SOFT_REMOTE_TOKENS)

    hybrid_hit = any(tok in haystack for tok in _HYBRID_TOKENS)
    onsite_hit = any(tok in haystack for tok in _ONSITE_TOKENS)

    if onsite_hit and not remote_hit:
        return "on_site"
    if hybrid_hit:
        return "hybrid"
    if remote_hit:
        return "remote"
    return "unknown"


def _match_tokens(haystack: str, tokens: list[str]) -> list[str]:
    return [t for t in tokens if t and t in haystack]


def evaluate_convo(convo: dict[str, Any], policy: GeoPolicy) -> GeoVerdict:
    """Return a GeoVerdict for a conversation -- deterministic, no LLM."""
    loc_field, haystack = _gather_text(convo)
    work_mode = _infer_work_mode(haystack)
    allowed_hits = _match_tokens(haystack, policy.on_site_allowed)
    blocked_hits = _match_tokens(haystack, policy.on_site_blocked)
    now = datetime.now(timezone.utc).isoformat()

    if blocked_hits:
        # "Remote, HQ in $BLOCKED" is still fine if we're remote-ok.
        if work_mode == "remote" and not any(
            tok in haystack for tok in _ONSITE_TOKENS
        ) and not any(tok in haystack for tok in _HYBRID_TOKENS):
            return GeoVerdict(
                verdict="compatible",
                work_mode="remote",
                reason=f"explicitly remote; {blocked_hits[0]} mentioned but not required on-site",
                location_text=loc_field,
                matched_allowed=allowed_hits,
                matched_blocked=blocked_hits,
                evaluated_at=now,
            )
        # "Sunnyvale, CA or Austin, TX" -- the user can pick the allowed city.
        # Only veto when the thread exclusively points at a blocked region.
        if allowed_hits:
            return GeoVerdict(
                verdict="compatible",
                work_mode=work_mode,
                reason=(
                    f"thread mentions both allowed ({allowed_hits[0]}) and "
                    f"blocked ({blocked_hits[0]}); user can pick allowed"
                ),
                location_text=loc_field,
                matched_allowed=allowed_hits,
                matched_blocked=blocked_hits,
                evaluated_at=now,
            )
        return GeoVerdict(
            verdict="incompatible",
            work_mode=work_mode,
            reason=f"role location hits deny list ({blocked_hits[0]})",
            location_text=loc_field,
            matched_allowed=allowed_hits,
            matched_blocked=blocked_hits,
            evaluated_at=now,
        )

    if work_mode == "remote":
        if policy.remote_ok:
            return GeoVerdict(
                verdict="compatible",
                work_mode="remote",
                reason="remote role and remote_ok=true",
                location_text=loc_field,
                matched_allowed=allowed_hits,
                matched_blocked=blocked_hits,
                evaluated_at=now,
            )
        return GeoVerdict(
            verdict="incompatible",
            work_mode="remote",
            reason="remote role but remote_ok=false",
            location_text=loc_field,
            matched_allowed=allowed_hits,
            matched_blocked=blocked_hits,
            evaluated_at=now,
        )

    if work_mode == "hybrid":
        if allowed_hits or not policy.hybrid_requires_local:
            return GeoVerdict(
                verdict="compatible",
                work_mode="hybrid",
                reason=(
                    f"hybrid in allowed region ({allowed_hits[0]})"
                    if allowed_hits
                    else "hybrid and hybrid_requires_local=false"
                ),
                location_text=loc_field,
                matched_allowed=allowed_hits,
                matched_blocked=blocked_hits,
                evaluated_at=now,
            )
        # Hybrid with no region token -- could be Bay, could be elsewhere.
        # Don't veto; ask the recruiter before committing to a stance.
        return GeoVerdict(
            verdict="needs_clarification",
            work_mode="hybrid",
            reason="hybrid role but recruiter did not state a region",
            location_text=loc_field,
            matched_allowed=allowed_hits,
            matched_blocked=blocked_hits,
            ask_location=True,
            evaluated_at=now,
        )

    if work_mode == "on_site":
        if allowed_hits:
            return GeoVerdict(
                verdict="compatible",
                work_mode="on_site",
                reason=f"on-site in allowed region ({allowed_hits[0]})",
                location_text=loc_field,
                matched_allowed=allowed_hits,
                matched_blocked=blocked_hits,
                evaluated_at=now,
            )
        # On-site with no region token -- same treatment as hybrid unknown.
        # Could legitimately be SF; don't pre-block, just ask.
        return GeoVerdict(
            verdict="needs_clarification",
            work_mode="on_site",
            reason="on-site role but recruiter did not state a region",
            location_text=loc_field,
            matched_allowed=allowed_hits,
            matched_blocked=blocked_hits,
            ask_location=True,
            evaluated_at=now,
        )

    # work_mode == "unknown": fall back to location text only.
    if allowed_hits and not blocked_hits:
        return GeoVerdict(
            verdict="compatible",
            work_mode="unknown",
            reason=f"location mentions allowed region ({allowed_hits[0]})",
            location_text=loc_field,
            matched_allowed=allowed_hits,
            matched_blocked=blocked_hits,
            evaluated_at=now,
        )
    return GeoVerdict(
        verdict="unknown",
        work_mode="unknown",
        reason="no work-mode or region signal in thread",
        location_text=loc_field,
        matched_allowed=allowed_hits,
        matched_blocked=blocked_hits,
        evaluated_at=now,
    )


def apply_verdict(convo: dict[str, Any], verdict: GeoVerdict) -> bool:
    """Persist verdict on the convo. Return True if mutation happened.

    Incompatible verdicts demote the score tier and mark ``intent.abstain``.
    Compatible/unknown verdicts only stamp ``convo["geo"]`` and clear a prior
    geo-driven abstention if one existed.
    """
    prior = convo.get("geo")
    new_geo = verdict.to_dict()
    mutated = prior != new_geo
    convo["geo"] = new_geo

    score = convo.get("score") or {}
    breakdown = score.get("breakdown") or {}

    if verdict.verdict == "incompatible":
        # Snapshot pre-geo values on the FIRST demotion so we can restore
        # them cleanly if the verdict later flips (e.g., recruiter
        # clarifies, or we reclassify no-region on-site as
        # needs_clarification). Never overwrite an existing snapshot.
        if "_pre_geo" not in score and score:
            score["_pre_geo"] = {
                "total": score.get("total"),
                "action": score.get("action"),
                "logistics_fit": breakdown.get("logistics_fit"),
                "gaps": list(score.get("gaps") or []),
            }
            mutated = True
        if breakdown.get("logistics_fit", 0) != 0:
            breakdown["logistics_fit"] = 0
            mutated = True
        current_total = int(score.get("total") or 0)
        capped_total = min(current_total, SCORE_REVIEW - 1)
        if capped_total != current_total:
            score["total"] = capped_total
            mutated = True
        if score.get("action") != "notify_gaps":
            score["action"] = "notify_gaps"
            mutated = True
        gaps = list(score.get("gaps") or [])
        gap_note = f"geo_incompatible: {verdict.reason}"
        if gap_note not in gaps:
            gaps.insert(0, gap_note)
            score["gaps"] = gaps
            mutated = True
        score["breakdown"] = breakdown
        convo["score"] = score

        intent = dict(convo.get("intent") or {})
        if not intent.get("abstain") or intent.get("abstain_reason") != "geo_mismatch":
            intent["abstain"] = True
            intent["abstain_reason"] = "geo_mismatch"
            intent.setdefault("tag", intent.get("tag") or "dead_end")
            intent.setdefault("rationale", verdict.reason)
            intent["classified_at"] = intent.get("classified_at") or verdict.evaluated_at
            convo["intent"] = intent
            mutated = True

        reply = convo.get("reply") or {}
        if reply and reply.get("status") not in ("sent", "approved", "rejected", "manually_handled"):
            if reply.get("status") != "abstained" or reply.get("abstain_reason") != "geo_mismatch":
                reply["status"] = "abstained"
                reply["tier"] = "abstain"
                reply["abstain_reason"] = "geo_mismatch"
                reply["text"] = ""
                reply["geo_note"] = verdict.reason
                reply["updated_at"] = verdict.evaluated_at
                convo["reply"] = reply
                mutated = True
    else:
        # Clear a stale geo-mismatch abstention if the recruiter clarified,
        # or if we've reclassified on_site/hybrid-no-region from
        # incompatible to needs_clarification.
        intent = convo.get("intent") or {}
        if intent.get("abstain") and intent.get("abstain_reason") == "geo_mismatch":
            intent["abstain"] = False
            intent["abstain_reason"] = None
            convo["intent"] = intent
            mutated = True
        # Roll back prior demotion if we snapshotted the pre-geo score.
        score = convo.get("score") or {}
        snapshot = score.get("_pre_geo")
        if snapshot:
            score["total"] = snapshot.get("total", score.get("total"))
            score["action"] = snapshot.get("action", score.get("action"))
            breakdown = score.get("breakdown") or {}
            if snapshot.get("logistics_fit") is not None:
                breakdown["logistics_fit"] = snapshot["logistics_fit"]
                score["breakdown"] = breakdown
            if snapshot.get("gaps") is not None:
                score["gaps"] = list(snapshot["gaps"])
            score.pop("_pre_geo", None)
            convo["score"] = score
            mutated = True
        # Drop any lingering geo_incompatible gap note that never got
        # snapshotted (legacy rows from before this change).
        gaps = list(score.get("gaps") or [])
        cleaned = [g for g in gaps if not str(g).startswith("geo_incompatible:")]
        if cleaned != gaps:
            score["gaps"] = cleaned
            convo["score"] = score
            mutated = True
        # Same for a pending reply that was marked abstained by an earlier
        # run of this filter. Re-open it so the generator can re-draft
        # (with a location question, when ask_location is true).
        reply = convo.get("reply") or {}
        if (
            reply.get("status") == "abstained"
            and reply.get("abstain_reason") == "geo_mismatch"
        ):
            reply["status"] = "pending_regeneration"
            reply.pop("abstain_reason", None)
            reply.pop("tier", None)
            reply.pop("geo_note", None)
            reply["text"] = ""
            reply["message_count_at_generation"] = 0
            reply["updated_at"] = verdict.evaluated_at
            convo["reply"] = reply
            mutated = True

    return mutated


def _load_profile() -> dict[str, Any]:
    with open(PROFILE_FILE) as f:
        return yaml.safe_load(f) or {}


def run_sweep(target_urn: str | None, dry_run: bool) -> None:
    if not CLASSIFIED_FILE.exists():
        print(f"Error: {CLASSIFIED_FILE} not found. Run the pipeline first.", file=sys.stderr)
        sys.exit(1)

    policy = GeoPolicy.from_profile(_load_profile())
    if not policy.on_site_allowed and not policy.on_site_blocked:
        print("Warning: preferences.geo_policy is empty -- no filtering will occur.", file=sys.stderr)

    data = json.loads(Path(CLASSIFIED_FILE).read_text())
    conversations = data.get("conversations") or []

    touched = 0
    incompat = 0
    clarify = 0
    for convo in conversations:
        if convo.get("classification", {}).get("category") != "recruiter":
            continue
        if target_urn and convo.get("conversationUrn") != target_urn:
            continue
        verdict = evaluate_convo(convo, policy)
        if verdict.verdict == "incompatible":
            incompat += 1
        elif verdict.verdict == "needs_clarification":
            clarify += 1
        other = next(
            (p.get("name") for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            "?",
        )
        tag = verdict.verdict.upper().ljust(20)
        mode = verdict.work_mode.ljust(8)
        loc = verdict.location_text or "-"
        print(f"  [{tag}] [{mode}] {other:28} loc={loc!s:40} :: {verdict.reason}")
        if not dry_run:
            if apply_verdict(convo, verdict):
                touched += 1

    if dry_run:
        print(
            f"dry-run: {incompat} incompatible, {clarify} needs_clarification "
            f"of {len(conversations)} evaluated (no writes)"
        )
        return

    Path(CLASSIFIED_FILE).write_text(json.dumps(data, indent=2) + "\n")
    print(
        f"Updated {CLASSIFIED_FILE} "
        f"({touched} mutated, {incompat} incompatible, {clarify} needs_clarification)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urn", help="Target a single conversation URN")
    parser.add_argument("--dry-run", action="store_true", help="Report, do not mutate")
    args = parser.parse_args()
    run_sweep(target_urn=args.urn, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

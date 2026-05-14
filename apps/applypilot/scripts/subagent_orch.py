"""Sub-agent orchestrator for score / tailor / cover stages.

Designed to replace applypilot's direct Gemini/Anthropic SDK calls
(in scoring/scorer.py, scoring/tailor.py, scoring/cover_letter.py)
with Claude Code Agent sub-agents firing from inside an interactive
Claude Code session. All LLM work therefore bills against the
session's subscription instead of the user's API keys.

The script itself never calls any LLM. It only:
  - Lists candidate jobs needing each stage (as JSON on stdout)
  - Builds a self-contained prompt the driving agent hands to a sub-agent
  - Applies the sub-agent's text result back to BAP + disk

Driving from a session looks like:
  $ python -m scripts.subagent_orch list-score --limit 5
  # → agent reads JSON, fires N parallel Agent({model:"opus", prompt:...}) calls
  $ python -m scripts.subagent_orch apply-score --url URL --score 8 --keywords "..." --reasoning "..."

Subcommands:
  list-score   --limit N
  apply-score  --url U --score N --keywords K --reasoning R
  list-tailor  --limit N
  apply-tailor --url U --tailored-text-file PATH
  list-cover   --limit N
  apply-cover  --url U --cover-text-file PATH
  stats                       # current pipeline counts (sanity check)

Output JSON is one line per work item (jsonl-ish but wrapped in a
single list) so the agent can iterate without re-parsing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.config import (
    COVER_LETTER_DIR,
    RESUME_PATH,
    TAILORED_DIR,
    load_profile,
)
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.scoring import pdf as pdf_lib

# ── Prompts (verbatim copies of applypilot's internal SDK prompts) ─────────
# Keeping them duplicated rather than importing so a sub-agent can be given
# the full template inline without pulling in applypilot's LLM client glue.

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


TAILOR_USER_TEMPLATE = """Rewrite the candidate's resume to maximize match against this specific job description.

HARD RULES (any violation is a failure):
- NEVER fabricate experience, employers, dates, degrees, or projects not present in the source resume.
- NEVER claim skills the candidate doesn't have. Reordering / emphasizing real skills is OK.
- Keep all employers, dates, and education exactly as in the source.
- Output a complete tailored resume in plain text, ready to save as <name>_Resume.txt.

OUTPUT FORMAT: just the tailored resume text. No preamble, no commentary, no markdown.

SOURCE RESUME:
{resume_text}

USER PROFILE EXCERPT (for context, do NOT invent from this — only use what's in the source resume above):
{profile_excerpt}

JOB DESCRIPTION:
TITLE: {title}
COMPANY: {site}
LOCATION: {location}
URL: {url}

{full_description}"""


COVER_USER_TEMPLATE = """Write a focused cover letter for this specific role.

CONSTRAINTS:
- 3 short paragraphs, ≤ 200 words total.
- Open with the single most relevant achievement from the candidate's resume that maps to the role.
- Middle paragraph: 2 concrete experiences from the resume that map to specific JD requirements.
- Closing: 1 sentence on why this team / problem / company specifically.
- NO greetings beyond "Dear Hiring Manager,". NO signoff ("Sincerely, [name]") — just the body.
- NEVER fabricate. Everything must be grounded in the resume.

OUTPUT: just the cover letter text. No preamble, no commentary, no markdown.

CANDIDATE'S TAILORED RESUME:
{tailored_resume}

JOB:
TITLE: {title}
COMPANY: {site}
LOCATION: {location}
URL: {url}

{full_description}"""


# ── Helpers ────────────────────────────────────────────────────────────────

def _file_prefix(job: dict) -> str:
    """Mirror applypilot/scoring/tailor.py:553 — derive safe filename prefix."""
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "Untitled")[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job.get("site") or "Unknown")[:20].strip().replace(" ", "_")
    return f"{safe_site}_{safe_title}"


def _profile_excerpt(profile: dict, max_chars: int = 3500) -> str:
    """Compact profile dump for prompts. Drops bulky `ats` / `skills.evidence`
    fields the sub-agent doesn't need to write a resume."""
    keep = {
        "identity": profile.get("identity"),
        "summary": profile.get("summary"),
        "expertise_areas": profile.get("expertise_areas"),
        "preferences": (profile.get("preferences") or {}),
        "achievements": profile.get("achievements"),
    }
    text = json.dumps(keep, indent=2, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated]"
    return text


def _resume_text() -> str:
    return Path(RESUME_PATH).read_text(encoding="utf-8")


# ── list-* commands: produce sub-agent-ready work units ────────────────────

def cmd_list_score(args: argparse.Namespace) -> None:
    conn = get_connection()
    jobs = get_jobs_by_stage(conn, stage="pending_score", limit=args.limit)
    resume_text = _resume_text()
    out = []
    for job in jobs:
        job_text = (
            f"TITLE: {job['title']}\n"
            f"COMPANY: {job['site']}\n"
            f"LOCATION: {job.get('location') or 'N/A'}\n\n"
            f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
        )
        full_prompt = (
            f"{SCORE_PROMPT}\n\n---\n\n"
            f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}\n\n"
            "Return ONLY the three lines (SCORE:, KEYWORDS:, REASONING:) — no commentary."
        )
        out.append({
            "stage": "score",
            "url": job["url"],
            "title": job["title"],
            "site": job["site"],
            "prompt": full_prompt,
        })
    print(json.dumps(out, indent=2))


def cmd_list_tailor(args: argparse.Namespace) -> None:
    conn = get_connection()
    jobs = get_jobs_by_stage(conn, stage="pending_tailor",
                             min_score=args.min_score, limit=args.limit)
    resume_text = _resume_text()
    profile = load_profile()
    profile_excerpt = _profile_excerpt(profile)
    out = []
    for job in jobs:
        user_prompt = TAILOR_USER_TEMPLATE.format(
            resume_text=resume_text,
            profile_excerpt=profile_excerpt,
            title=job["title"],
            site=job["site"],
            location=job.get("location") or "N/A",
            url=job["url"],
            full_description=(job.get("full_description") or "")[:6000],
        )
        out.append({
            "stage": "tailor",
            "url": job["url"],
            "title": job["title"],
            "site": job["site"],
            "fit_score": job.get("fit_score"),
            "prompt": user_prompt,
        })
    print(json.dumps(out, indent=2))


def cmd_list_cover(args: argparse.Namespace) -> None:
    conn = get_connection()
    # Custom query — get_jobs_by_stage doesn't have a pending_cover stage.
    rows = conn.execute(
        """
        SELECT url, title, site, location, full_description, fit_score,
               tailored_resume_path
        FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND (cover_letter_path IS NULL OR cover_letter_path = '')
          AND COALESCE(cover_attempts, 0) < 5
          AND fit_score >= ?
        ORDER BY fit_score DESC, url
        LIMIT ?
        """,
        (args.min_score, args.limit),
    ).fetchall()
    out = []
    for row in rows:
        row = dict(row)
        tailored = ""
        tp = row.get("tailored_resume_path")
        if tp and Path(tp).exists():
            tailored = Path(tp).read_text(encoding="utf-8")
        user_prompt = COVER_USER_TEMPLATE.format(
            tailored_resume=tailored,
            title=row["title"],
            site=row["site"],
            location=row.get("location") or "N/A",
            url=row["url"],
            full_description=(row.get("full_description") or "")[:6000],
        )
        out.append({
            "stage": "cover",
            "url": row["url"],
            "title": row["title"],
            "site": row["site"],
            "fit_score": row.get("fit_score"),
            "prompt": user_prompt,
        })
    print(json.dumps(out, indent=2))


# ── apply-* commands: write sub-agent results back ─────────────────────────

def cmd_apply_score(args: argparse.Namespace) -> None:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    score = max(1, min(10, int(args.score)))
    conn.execute(
        """
        UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?
        WHERE url = ?
        """,
        (score, f"{args.keywords or ''}\n\n{args.reasoning or ''}".strip(), now, args.url),
    )
    conn.commit()
    print(json.dumps({"ok": True, "url": args.url, "score": score}))


def cmd_apply_tailor(args: argparse.Namespace) -> None:
    conn = get_connection()
    job_row = conn.execute(
        "SELECT url, title, site FROM jobs WHERE url = ?", (args.url,)
    ).fetchone()
    if not job_row:
        print(json.dumps({"ok": False, "error": "url not found"}))
        sys.exit(1)
    job = dict(job_row)
    text = Path(args.tailored_text_file).read_text(encoding="utf-8")

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    prefix = _file_prefix(job)
    txt_path = TAILORED_DIR / f"{prefix}.txt"
    txt_path.write_text(text, encoding="utf-8")

    pdf_path = None
    try:
        pdf_path = pdf_lib.convert_to_pdf(txt_path)
    except Exception as e:
        # PDF is best-effort; .txt is the source of truth in BAP.
        print(json.dumps({"ok": True, "url": args.url, "txt_path": str(txt_path),
                          "pdf_error": str(e)}))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE jobs SET tailored_resume_path = ?, tailored_at = ?,
                       tailor_attempts = COALESCE(tailor_attempts, 0) + 1
        WHERE url = ?
        """,
        (str(txt_path), now, args.url),
    )
    conn.commit()
    print(json.dumps({"ok": True, "url": args.url,
                      "txt_path": str(txt_path),
                      "pdf_path": str(pdf_path) if pdf_path else None}))


def cmd_apply_cover(args: argparse.Namespace) -> None:
    conn = get_connection()
    job_row = conn.execute(
        "SELECT url, title, site FROM jobs WHERE url = ?", (args.url,)
    ).fetchone()
    if not job_row:
        print(json.dumps({"ok": False, "error": "url not found"}))
        sys.exit(1)
    job = dict(job_row)
    text = Path(args.cover_text_file).read_text(encoding="utf-8")

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    prefix = _file_prefix(job)
    txt_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
    txt_path.write_text(text, encoding="utf-8")

    pdf_path = None
    try:
        pdf_path = pdf_lib.convert_to_pdf(txt_path)
    except Exception as e:
        print(json.dumps({"ok": True, "url": args.url, "txt_path": str(txt_path),
                          "pdf_error": str(e)}))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE jobs SET cover_letter_path = ?, cover_letter_at = ?,
                       cover_attempts = COALESCE(cover_attempts, 0) + 1
        WHERE url = ?
        """,
        (str(txt_path), now, args.url),
    )
    conn.commit()
    print(json.dumps({"ok": True, "url": args.url,
                      "txt_path": str(txt_path),
                      "pdf_path": str(pdf_path) if pdf_path else None}))


def cmd_stats(_: argparse.Namespace) -> None:
    conn = get_connection()
    s: dict[str, Any] = {}
    s["total_jobs"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    s["enriched"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL").fetchone()[0]
    s["pending_score"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL").fetchone()[0]
    s["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0]
    s["pending_tailor_ge7"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 "
        "AND full_description IS NOT NULL AND tailored_resume_path IS NULL "
        "AND COALESCE(tailor_attempts, 0) < 5").fetchone()[0]
    s["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL").fetchone()[0]
    s["pending_cover_ge7"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 "
        "AND tailored_resume_path IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < 5").fetchone()[0]
    s["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL").fetchone()[0]
    s["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL").fetchone()[0]
    print(json.dumps(s, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="subagent_orch")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list-score")
    lp.add_argument("--limit", type=int, default=10)
    lp.set_defaults(func=cmd_list_score)

    ap = sub.add_parser("apply-score")
    ap.add_argument("--url", required=True)
    ap.add_argument("--score", required=True, type=int)
    ap.add_argument("--keywords", default="")
    ap.add_argument("--reasoning", default="")
    ap.set_defaults(func=cmd_apply_score)

    lt = sub.add_parser("list-tailor")
    lt.add_argument("--limit", type=int, default=10)
    lt.add_argument("--min-score", type=int, default=7)
    lt.set_defaults(func=cmd_list_tailor)

    at = sub.add_parser("apply-tailor")
    at.add_argument("--url", required=True)
    at.add_argument("--tailored-text-file", required=True)
    at.set_defaults(func=cmd_apply_tailor)

    lc = sub.add_parser("list-cover")
    lc.add_argument("--limit", type=int, default=10)
    lc.add_argument("--min-score", type=int, default=7)
    lc.set_defaults(func=cmd_list_cover)

    ac = sub.add_parser("apply-cover")
    ac.add_argument("--url", required=True)
    ac.add_argument("--cover-text-file", required=True)
    ac.set_defaults(func=cmd_apply_cover)

    st = sub.add_parser("stats")
    st.set_defaults(func=cmd_stats)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

"""Deterministic geo_fit classifier: does this role's location/remote-posture
match the user's eligibility policy?

Separate from fit_score (which is skill-only, LLM-driven). This module runs
pure Python rules — fast, cheap, deterministic. The output column
``jobs.geo_fit`` is used by ``apply.acquire_job`` to filter the queue.

Policy input (from ``profile['eligibility']``):
    countries_authorized_to_work:        list[str] (hard — can submit to on-site roles here)
    countries_acceptable_if_remote:      list[str] (softer — remote-only roles in these countries)
    relocation_willing:                  bool
    sponsorship_willing:                 bool

Classifications:
    "eligible"          — role is unambiguously workable given policy
    "remote_abroad_ok"  — remote role in a country the user accepts remotely
    "hybrid_abroad"     — hybrid/on-site abroad but worth a manual look
                          (e.g. willing to relocate, or policy borderline)
    "fully_ineligible"  — hybrid/on-site in a country outside policy; skip
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# ─── Country detection ──────────────────────────────────────────────────
# Mapping keyed by lower-cased substrings we expect to see in the
# ``jobs.location`` or ``jobs.full_description`` text. Values are the
# canonical country names that the user's eligibility policy lists.
# Order matters: longest-first prevents "US" matching inside "Australia".

_COUNTRY_PATTERNS: list[tuple[str, str]] = [
    # Disambiguated synonyms first (prefixed with word boundary)
    ("united states of america", "United States"),
    ("united states",           "United States"),
    ("united kingdom",          "United Kingdom"),
    ("new zealand",             "New Zealand"),
    ("south korea",             "South Korea"),
    ("korea, republic of",      "South Korea"),
    ("korea republic of",       "South Korea"),
    ("republic of korea",       "South Korea"),
    ("hong kong",               "Hong Kong"),
    ("south africa",            "South Africa"),
    ("saudi arabia",            "Saudi Arabia"),
    ("united arab emirates",    "United Arab Emirates"),
    ("czech republic",          "Czech Republic"),
    # Unambiguous single-token countries
    ("brazil",      "Brazil"),
    ("mexico",      "Mexico"),
    ("argentina",   "Argentina"),
    ("colombia",    "Colombia"),
    ("chile",       "Chile"),
    ("peru",        "Peru"),
    ("canada",      "Canada"),
    ("india",       "India"),
    ("philippines", "Philippines"),
    ("indonesia",   "Indonesia"),
    ("malaysia",    "Malaysia"),
    ("singapore",   "Singapore"),
    ("thailand",    "Thailand"),
    ("vietnam",     "Vietnam"),
    ("china",       "China"),
    ("taiwan",      "Taiwan"),
    ("japan",       "Japan"),
    ("australia",   "Australia"),
    ("ireland",     "Ireland"),
    ("germany",     "Germany"),
    ("france",      "France"),
    ("spain",       "Spain"),
    ("portugal",    "Portugal"),
    ("italy",       "Italy"),
    ("netherlands", "Netherlands"),
    ("belgium",     "Belgium"),
    ("sweden",      "Sweden"),
    ("norway",      "Norway"),
    ("denmark",     "Denmark"),
    ("finland",     "Finland"),
    ("switzerland", "Switzerland"),
    ("austria",     "Austria"),
    ("poland",      "Poland"),
    ("romania",     "Romania"),
    ("bulgaria",    "Bulgaria"),
    ("hungary",     "Hungary"),
    ("greece",      "Greece"),
    ("turkey",      "Turkey"),
    ("israel",      "Israel"),
    ("egypt",       "Egypt"),
    ("nigeria",     "Nigeria"),
    ("kenya",       "Kenya"),
    # UK's regional cities (scraped data sometimes omits country)
    ("london",      "United Kingdom"),
    # US ambiguous token-last. Must come AFTER "united states".
    (" us,",        "United States"),
    (", us ",       "United States"),
    (" us-",        "United States"),
    ("-us ",        "United States"),
    ("remote-us",   "United States"),
    ("us, ca",      "United States"),
    ("us-ca",       "United States"),
    ("uk,",         "United Kingdom"),
    (", uk ",       "United Kingdom"),
    ("uk-",         "United Kingdom"),
    ("-uk ",        "United Kingdom"),
]

# Major US metros / regions that frequently appear without the state name
# (e.g. scrape of a remote-first job posting). Lowercased substrings.
_US_METROS = {
    "san francisco", "bay area", "silicon valley", "new york", "nyc",
    "los angeles", "seattle", "austin", "boston", "chicago", "denver",
    "atlanta", "washington dc", "washington d.c.", "miami",
    "salt lake city", "portland", "minneapolis", "philadelphia",
    "houston", "dallas", "phoenix", "san diego",
}

# Explicit US-state token check: if location contains a US state name (or
# standard 2-letter abbreviation in a "City, ST" pattern) we assume US even
# without the word "United States".
_US_STATES = {
    "alabama","alaska","arizona","arkansas","california","colorado",
    "connecticut","delaware","florida","georgia","hawaii","idaho",
    "illinois","indiana","iowa","kansas","kentucky","louisiana","maine",
    "maryland","massachusetts","michigan","minnesota","mississippi",
    "missouri","montana","nebraska","nevada","new hampshire","new jersey",
    "new mexico","new york","north carolina","north dakota","ohio",
    "oklahoma","oregon","pennsylvania","rhode island","south carolina",
    "south dakota","tennessee","texas","utah","vermont","virginia",
    "washington","west virginia","wisconsin","wyoming",
    "district of columbia",
}

# Canadian provinces (scraped data like "Canada Toronto Ontario" already
# hits canada match; this is for edge cases like "Ontario, Remote")
_CA_PROVINCES = {
    "alberta","british columbia","manitoba","new brunswick","newfoundland",
    "nova scotia","ontario","prince edward island","quebec","saskatchewan",
}


def detect_country(location: str | None) -> str | None:
    """Best-effort country inference from a ``jobs.location`` string.

    Returns the canonical country name (matching what the eligibility
    policy uses) or ``None`` if no country could be inferred. The caller
    should treat ``None`` as "couldn't classify — defer to manual review".
    """
    if not location:
        return None
    loc = location.lower()

    for pat, country in _COUNTRY_PATTERNS:
        if pat in loc:
            return country

    # US state substring check
    for state in _US_STATES:
        if state in loc:
            return "United States"

    # Canadian province substring check
    for prov in _CA_PROVINCES:
        if prov in loc:
            return "Canada"

    # Major US metros (Bay Area, NYC, etc. — common on remote-first postings)
    for metro in _US_METROS:
        if metro in loc:
            return "United States"

    return None


# ─── Remote detection ────────────────────────────────────────────────────

_REMOTE_PATTERNS = re.compile(
    r"\b(remote(?:-first)?|work from anywhere|fully remote|100% remote|"
    r"work from home|telework|teletravail|remote-ok|remote ok)\b",
    re.IGNORECASE,
)

_HYBRID_PATTERNS = re.compile(
    r"\b(hybrid|on-?site|in[- ]office|office-based|"
    r"(\d+\s*days?/?\s*(per\s*)?week\s+in\s+(the\s+)?office))\b",
    re.IGNORECASE,
)


def is_remote(location: str | None, full_description: str | None = None) -> bool:
    """Heuristic: does this role allow fully-remote work?

    Checks location string first (often contains "Remote" tag), falls back
    to the JD text for phrases like "work from anywhere".
    """
    if location and _REMOTE_PATTERNS.search(location):
        return True
    if full_description and _REMOTE_PATTERNS.search(full_description):
        # But if JD also mentions hybrid/on-site strongly, prefer that
        if _HYBRID_PATTERNS.search(full_description):
            # Both mentioned — disambiguate by proximity or pick hybrid
            return False
        return True
    return False


# ─── Classification ──────────────────────────────────────────────────────

def classify(
    location: str | None,
    full_description: str | None,
    eligibility: dict,
) -> tuple[str, str]:
    """Return ``(geo_fit, reasoning)`` for a single job.

    Args:
        location:         Value of ``jobs.location``.
        full_description: Value of ``jobs.full_description`` (may be None).
        eligibility:      Dict from ``profile['eligibility']``.

    Returns:
        ``(classification, one-line reasoning)`` — classification is one of
        ``"eligible" | "remote_abroad_ok" | "hybrid_abroad" | "fully_ineligible"``.
    """
    authorized = {c for c in eligibility.get("countries_authorized_to_work", [])}
    acceptable_remote = {c for c in eligibility.get("countries_acceptable_if_remote", [])}
    relocation = bool(eligibility.get("relocation_willing", False))

    country = detect_country(location)
    remote = is_remote(location, full_description)

    # Case 1 — country unclear, no remote signal
    if country is None and not remote:
        return "hybrid_abroad", "country undetected and no remote signal — manual review"

    # Case 2 — remote role
    if remote:
        if country is None:
            # Remote with no country restriction mentioned — usually safe
            return "remote_abroad_ok", "remote posting with no country gate"
        if country in authorized or country in acceptable_remote:
            return "eligible", f"remote in {country}"
        if "Remote (global)" in acceptable_remote:
            return "remote_abroad_ok", f"remote in {country}; user accepts global remote"
        return "remote_abroad_ok", f"remote in {country}; outside acceptable list but remote"

    # Case 3 — on-site/hybrid in authorized country
    if country in authorized:
        return "eligible", f"on-site/hybrid in {country} (authorized)"

    # Case 4 — on-site/hybrid abroad
    if relocation and country in acceptable_remote:
        return "hybrid_abroad", f"on-site in {country}; user willing to relocate"

    return "fully_ineligible", f"on-site/hybrid in {country or 'unknown'} outside authorized countries"


# ─── Batch classification for existing DB rows ──────────────────────────

def backfill_geo_fit(conn, eligibility: dict) -> dict:
    """One-time classifier pass over every row with geo_fit IS NULL.

    Returns a dict summarizing counts per classification (useful for a
    sanity check after migration).
    """
    rows = conn.execute("""
        SELECT url, location, full_description
        FROM jobs
        WHERE geo_fit IS NULL OR geo_fit = ''
    """).fetchall()

    tallies: dict[str, int] = {}
    for row in rows:
        # support both tuple and sqlite3.Row
        if isinstance(row, tuple):
            url, loc, desc = row
        else:
            url, loc, desc = row["url"], row["location"], row["full_description"]
        gfit, reason = classify(loc, desc, eligibility)
        conn.execute(
            "UPDATE jobs SET geo_fit = ?, geo_fit_reasoning = ? WHERE url = ?",
            (gfit, reason, url),
        )
        tallies[gfit] = tallies.get(gfit, 0) + 1

    conn.commit()
    log.info("geo_fit backfill: %s", tallies)
    return tallies

"""Lever public-API discovery adapter.

Lever exposes per-company postings at
    https://api.lever.co/v0/postings/<site>?mode=json
Response shape: a JSON array of postings (or {"ok": false, "error": ...}
if the slug is wrong / company isn't on Lever).

Each posting has: id, text (title), hostedUrl, description, descriptionPlain,
categories.location, categories.team, etc. Lever returns the full plaintext
JD inline so we set full_description directly and skip enrich.

Smaller adapter than greenhouse.py — fewer companies use Lever for tech
hiring (most migrated to Greenhouse).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from applypilot.config import PACKAGE_DIR, load_search_config
from applypilot.database import get_connection

log = logging.getLogger(__name__)

LEVER_SITES_FILE = PACKAGE_DIR / "config" / "lever_sites.yaml"
LEVER_API_BASE = "https://api.lever.co/v0/postings"


def load_sites() -> list[dict]:
    if not LEVER_SITES_FILE.exists():
        log.warning("Lever sites file not found at %s", LEVER_SITES_FILE)
        return []
    with open(LEVER_SITES_FILE) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("lever_sites", [])


def _load_location_filter(search_cfg: dict | None = None):
    cfg = search_cfg or load_search_config()
    accept = [s.lower() for s in cfg.get("location_accept", []) or []]
    reject = [s.lower() for s in cfg.get("location_reject_non_remote", []) or []]
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    if not location:
        return True
    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True
    for r in reject:
        if r in loc:
            return False
    for a in accept:
        if a in loc:
            return True
    return False


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", " ", html_lib.unescape(s))


def _http_get_json(url: str, timeout: int = 15):
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "applypilot/0.3 (lever-public-api)",
            "Accept": "application/json",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("Lever %d for %s", e.code, url)
    except Exception as e:
        log.warning("Lever fetch error for %s: %s", url, e)
    return None


def lever_search(site: dict, queries: list[str],
                 accept: list[str], reject: list[str]) -> list[dict]:
    slug = site["slug"]
    name = site["name"]
    url = f"{LEVER_API_BASE}/{slug}?mode=json"
    payload = _http_get_json(url)
    if not payload or not isinstance(payload, list):
        # Lever returns {"ok": false, "error": "Document not found"} for
        # slugs that aren't on Lever. Empty list otherwise = no postings.
        if isinstance(payload, dict) and payload.get("error"):
            log.warning("Lever[%s]: %s", name, payload.get("error"))
        return []
    out: list[dict] = []
    query_set = [q.lower() for q in queries]
    for p in payload:
        title = (p.get("text") or "").strip()
        if not title:
            continue
        tl = title.lower()
        if query_set and not any(q in tl for q in query_set):
            continue
        location = (p.get("categories") or {}).get("location") or ""
        if not _location_ok(location, accept, reject):
            continue
        full_desc = (
            p.get("descriptionPlain")
            or _strip_html(p.get("description") or "")
        )
        out.append({
            "url": p.get("hostedUrl"),
            "title": title,
            "location": location,
            "description": (full_desc or "")[:1000],
            "full_description": full_desc,
            "salary": None,
            "site": name,
            "strategy": "lever_api",
        })
    log.info("Lever[%s]: %d total → %d after filter", name, len(payload), len(out))
    return out


def _store(conn, jobs: list[dict], site_default: str) -> tuple[int, int]:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    for job in jobs:
        u = job.get("url")
        if not u:
            continue
        try:
            conn.execute(
                """
                INSERT INTO jobs (url, title, salary, description, location,
                                  site, strategy, discovered_at,
                                  full_description, detail_scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    u, job.get("title"), job.get("salary"), job.get("description"),
                    job.get("location"), job.get("site") or site_default,
                    job.get("strategy") or "lever_api", now,
                    job.get("full_description"), now,
                ),
            )
            new += 1
        except Exception:
            existing += 1
    conn.commit()
    return new, existing


def run_lever_discovery(workers: int = 1) -> dict:
    sites = load_sites()
    if not sites:
        log.warning("No Lever sites configured; skipping stage.")
        return {"sites": 0, "new": 0, "existing": 0}
    cfg = load_search_config()
    queries = [q.get("query", "") for q in (cfg.get("queries") or []) if q.get("query")]
    accept, reject = _load_location_filter(cfg)
    log.info("Lever: %d sites × %d queries; loc-filter accept=%d reject=%d",
             len(sites), len(queries), len(accept), len(reject))
    conn = get_connection()
    total_new = 0
    total_existing = 0

    def _one(site: dict):
        try:
            jobs = lever_search(site, queries, accept, reject)
            return site, jobs, None
        except Exception as e:
            return site, [], str(e)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_one, s) for s in sites]
            for f in as_completed(futures):
                site, jobs, err = f.result()
                if err:
                    log.warning("Lever[%s] failed: %s", site["name"], err)
                    continue
                n, e = _store(conn, jobs, site["name"])
                total_new += n
                total_existing += e
                time.sleep(0.2)
    else:
        for site in sites:
            site, jobs, err = _one(site)
            if err:
                continue
            n, e = _store(conn, jobs, site["name"])
            total_new += n
            total_existing += e
            time.sleep(0.2)
    log.info("Lever: done. new=%d existing=%d across %d sites",
             total_new, total_existing, len(sites))
    return {"sites": len(sites), "new": total_new, "existing": total_existing}

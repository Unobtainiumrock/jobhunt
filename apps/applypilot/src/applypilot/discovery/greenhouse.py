"""Greenhouse public-API discovery adapter.

Greenhouse exposes every company's job board as a public JSON endpoint at
    https://boards-api.greenhouse.io/v1/boards/<board_token>/jobs
No auth, no scraping — the response is the full list of postings for
that board. With ?content=true, each posting includes the full HTML JD.

This adapter mirrors the workday.py pattern:
  - load_boards()              reads config/greenhouse_boards.yaml
  - greenhouse_search(board)   fetches the board's full job list
  - run_greenhouse_discovery() iterates all configured boards × user's
                               search queries (title-substring filtered),
                               applies location filter, stores results

Because Greenhouse returns the full JD inline, this adapter sets
``full_description`` directly so the enrich stage can skip these rows.
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
from pathlib import Path

import yaml

from applypilot.config import PACKAGE_DIR, load_search_config
from applypilot.database import get_connection

log = logging.getLogger(__name__)

GREENHOUSE_BOARDS_FILE = PACKAGE_DIR / "config" / "greenhouse_boards.yaml"
BOARDS_API_BASE = "https://boards-api.greenhouse.io/v1/boards"


def load_boards() -> list[dict]:
    """Read the curated Greenhouse board list from config."""
    if not GREENHOUSE_BOARDS_FILE.exists():
        log.warning("Greenhouse boards file not found at %s", GREENHOUSE_BOARDS_FILE)
        return []
    with open(GREENHOUSE_BOARDS_FILE) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("greenhouse_boards", [])


def _load_location_filter(search_cfg: dict | None = None):
    """Mirror workday.py's location config loader."""
    cfg = search_cfg or load_search_config()
    accept = [s.lower() for s in cfg.get("location_accept", []) or []]
    reject = [s.lower() for s in cfg.get("location_reject_non_remote", []) or []]
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    if not location:
        return True  # unknown — keep, scorer decides
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
    """Quick HTML → text. Greenhouse content is HTML-encoded markup."""
    if not s:
        return ""
    # Decode entities, then strip tags
    decoded = html_lib.unescape(s)
    return re.sub(r"<[^>]+>", " ", decoded)


def _http_get_json(url: str, timeout: int = 15) -> dict | None:
    """Minimal urllib JSON fetch. Avoids `requests` so we don't have to
    worry about the user's HTTPS_PROXY env (which routes through mitm
    and breaks cert verification for Python — see jobspy SSL bug)."""
    # Use a no-proxy opener so we bypass the mitm rig even if the env
    # vars leak in.
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "applypilot/0.3 (greenhouse-public-api)",
            "Accept": "application/json",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("Greenhouse %d for %s", e.code, url)
    except Exception as e:
        log.warning("Greenhouse fetch error for %s: %s", url, e)
    return None


def greenhouse_search(board: dict, queries: list[str],
                      accept: list[str], reject: list[str]) -> list[dict]:
    """Fetch all jobs for one Greenhouse board, filter by query title-match +
    location, and return normalized job dicts ready for store_jobs.

    Returns:
        List of dicts with keys: url, title, location, description,
        full_description, salary, site, strategy.
    """
    token = board["token"]
    name = board["name"]
    url = f"{BOARDS_API_BASE}/{token}/jobs?content=true"
    payload = _http_get_json(url)
    if not payload:
        return []
    jobs = payload.get("jobs", []) or []
    out: list[dict] = []
    query_set = [q.lower() for q in queries]
    for j in jobs:
        title = (j.get("title") or "").strip()
        if not title:
            continue
        tl = title.lower()
        if query_set and not any(q in tl for q in query_set):
            continue
        location = (j.get("location") or {}).get("name") or ""
        if not _location_ok(location, accept, reject):
            continue
        out.append({
            "url": j.get("absolute_url"),
            "title": title,
            "location": location,
            "description": _strip_html(j.get("content") or "")[:1000],
            "full_description": _strip_html(j.get("content") or ""),
            "salary": None,  # Greenhouse public API doesn't expose comp
            "site": name,
            "strategy": "greenhouse_api",
            "_full_desc_already_set": True,
        })
    log.info("Greenhouse[%s]: %d total → %d after query+location filter", name, len(jobs), len(out))
    return out


def _store_with_full_desc(conn, jobs: list[dict], site_default: str) -> tuple[int, int]:
    """Like database.store_jobs but also writes full_description + sets
    detail_scraped_at so the enrich stage skips these rows (Greenhouse
    already gave us the full JD inline)."""
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
                    job.get("strategy") or "greenhouse_api", now,
                    job.get("full_description"), now,
                ),
            )
            new += 1
        except Exception:
            # most likely IntegrityError on duplicate URL — count and continue
            existing += 1
    conn.commit()
    return new, existing


def run_greenhouse_discovery(workers: int = 1) -> dict:
    """Top-level: iterate all configured boards, filter, store. Parallel
    by board (each board is one HTTP call)."""
    boards = load_boards()
    if not boards:
        log.warning("No Greenhouse boards configured; skipping stage.")
        return {"boards": 0, "new": 0, "existing": 0}

    cfg = load_search_config()
    queries = [q.get("query", "") for q in (cfg.get("queries") or []) if q.get("query")]
    accept, reject = _load_location_filter(cfg)
    log.info("Greenhouse: %d boards × %d queries; loc-filter accept=%d reject=%d",
             len(boards), len(queries), len(accept), len(reject))

    conn = get_connection()
    total_new = 0
    total_existing = 0

    def _one(board: dict):
        try:
            jobs = greenhouse_search(board, queries, accept, reject)
            return board, jobs, None
        except Exception as e:
            return board, [], str(e)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_one, b) for b in boards]
            for f in as_completed(futures):
                board, jobs, err = f.result()
                if err:
                    log.warning("Greenhouse[%s] failed: %s", board["name"], err)
                    continue
                n, e = _store_with_full_desc(conn, jobs, board["name"])
                total_new += n
                total_existing += e
                time.sleep(0.2)  # be polite
    else:
        for board in boards:
            board, jobs, err = _one(board)
            if err:
                continue
            n, e = _store_with_full_desc(conn, jobs, board["name"])
            total_new += n
            total_existing += e
            time.sleep(0.2)

    log.info("Greenhouse: done. new=%d existing=%d across %d boards",
             total_new, total_existing, len(boards))
    return {"boards": len(boards), "new": total_new, "existing": total_existing}

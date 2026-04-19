#!/usr/bin/env python3
"""
Health monitor for the linkedin-leads VPC deployment.

Checks CDP, LinkedIn session, Qdrant, and listener status on a loop.
Sends Telegram and/or webhook notifications on state changes.

Usage:
  python infra/healthcheck.py              # single check, exit
  python infra/healthcheck.py --watch      # continuous loop
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DOTENV = REPO_ROOT / ".env"


def _load_dotenv() -> None:
    if not DOTENV.exists():
        return
    for line in DOTENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()

CDP_URL = os.getenv("CDP_URL", "http://localhost:9222")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))
REMIND_INTERVAL = int(os.getenv("HEALTH_ALERT_REMIND_INTERVAL", "1800"))

TELEGRAM_TOKEN = os.getenv("HEALTH_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("HEALTH_TELEGRAM_CHAT_ID", "")
WEBHOOK_URL = os.getenv("HEALTH_WEBHOOK_URL", "")
LISTENER_HEALTH_URL = os.getenv("HEALTH_LISTENER_URL", "").strip()

GMAIL_INGEST_ENABLED = os.getenv("GMAIL_INGEST_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
GMAIL_STALE_HOURS = int(os.getenv("GMAIL_STALE_HOURS", "24"))
GMAIL_PROBE_INTERVAL = int(os.getenv("HEALTH_GMAIL_PROBE_INTERVAL", "900"))

DISK_FREE_THRESHOLD_PCT = float(os.getenv("HEALTH_DISK_FREE_PCT", "10"))
LISTENER_LIVE_STALE_HOURS = float(os.getenv("HEALTH_LISTENER_LIVE_STALE_HOURS", "6"))
SEND_ERROR_WINDOW_MIN = int(os.getenv("HEALTH_SEND_ERROR_WINDOW_MIN", "30"))
REVIEW_HEALTH_URL = os.getenv(
    "HEALTH_REVIEW_URL", "http://review:3457/api/send/status"
).strip()
BOT_HEARTBEAT_STALE_SEC = int(os.getenv("HEALTH_BOT_HEARTBEAT_STALE_SEC", "120"))
SENDER_LOCK_STALE_SEC = int(os.getenv("HEALTH_SENDER_LOCK_STALE_SEC", "600"))
# Cron heartbeat thresholds: expected-interval * 1.5 so we grace one missed
# tick. Keys map to data/.cron.<key>.heartbeat inside the app-data volume.
CRON_HEARTBEAT_MAX_SEC: dict[str, int] = {
    "scrape": int(os.getenv("HEALTH_CRON_SCRAPE_MAX_SEC", str(6 * 3600))),
    "pipeline": int(os.getenv("HEALTH_CRON_PIPELINE_MAX_SEC", str(9 * 3600))),
    "health": int(os.getenv("HEALTH_CRON_HEALTH_MAX_SEC", str(23 * 60))),
}

CHECKPOINT_PATTERNS = [
    "/checkpoint/",
    "/login",
    "/authwall",
    "/uas/login",
]


@dataclass
class CheckResult:
    name: str
    healthy: bool
    detail: str


@dataclass
class HealthState:
    statuses: dict[str, bool] = field(default_factory=dict)
    last_alert_at: float = 0.0
    alerted_unhealthy: bool = False


def _http_get(url: str, timeout: int = 5) -> str | None:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except Exception:
        return None


def _http_post_json(url: str, data: dict, timeout: int = 10) -> bool:
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def check_cdp() -> CheckResult:
    body = _http_get(f"{CDP_URL}/json")
    if body is None:
        return CheckResult("cdp", False, "Chrome CDP not reachable")
    try:
        tabs = json.loads(body)
        pages = [t for t in tabs if t.get("type") == "page"]
        return CheckResult("cdp", True, f"{len(pages)} page tabs open")
    except (json.JSONDecodeError, TypeError):
        return CheckResult("cdp", False, "CDP returned invalid JSON")


def check_linkedin_session() -> CheckResult:
    body = _http_get(f"{CDP_URL}/json")
    if body is None:
        return CheckResult("linkedin", False, "CDP not reachable")
    try:
        tabs = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return CheckResult("linkedin", False, "CDP returned invalid JSON")

    li_tabs = [
        t for t in tabs
        if t.get("type") == "page" and "linkedin.com" in t.get("url", "")
    ]
    if not li_tabs:
        return CheckResult("linkedin", False, "No LinkedIn tabs found")

    for tab in li_tabs:
        url = tab.get("url", "")
        if any(p in url for p in CHECKPOINT_PATTERNS):
            return CheckResult(
                "linkedin", False, f"Session expired (checkpoint page: {url[:80]})"
            )

    return CheckResult("linkedin", True, f"{len(li_tabs)} LinkedIn tabs active")


def check_qdrant() -> CheckResult:
    body = _http_get(f"{QDRANT_URL}/collections")
    if body is None:
        return CheckResult("qdrant", False, "Qdrant not reachable")
    try:
        data = json.loads(body)
        if data.get("status") == "ok":
            names = [c["name"] for c in data.get("result", {}).get("collections", [])]
            return CheckResult("qdrant", True, f"Collections: {', '.join(names)}")
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return CheckResult("qdrant", False, "Qdrant returned unexpected response")


_gmail_probe_cache: dict[str, object] = {"at": 0.0, "result": None}


def check_gmail_token() -> CheckResult:
    """Probe Gmail via users.getProfile. Healthy when ingest is disabled.

    Rate-limited to one real probe per GMAIL_PROBE_INTERVAL seconds; cached
    result returned in between. Avoids burning 1 quota call per healthdog tick.
    """
    if not GMAIL_INGEST_ENABLED:
        return CheckResult("gmail_token", True, "GMAIL_INGEST_ENABLED=0 (skipped)")

    # Import inside function so a missing pipeline dep in a barebones container
    # never crashes healthdog startup.
    try:
        from pipeline.config import GOOGLE_TOKEN_GMAIL_FILE
    except Exception as exc:
        return CheckResult(
            "gmail_token", False, f"pipeline.config import failed: {exc}"
        )

    if not Path(GOOGLE_TOKEN_GMAIL_FILE).exists():
        return CheckResult(
            "gmail_token",
            False,
            "token file missing — run `npm run email:oauth` on laptop",
        )

    now = time.time()
    cached = _gmail_probe_cache.get("result")
    if cached and (now - float(_gmail_probe_cache.get("at") or 0.0)) < GMAIL_PROBE_INTERVAL:
        return cached  # type: ignore[return-value]

    # Build credentials without the listener's load_authorized_user_credentials
    # helper: that path writes the refreshed access token back to disk, which
    # fails on healthdog's read-only mount (Errno 30). Healthdog's job is to
    # observe, not to persist. So we refresh in-memory only; the listener
    # container (which has RW) is responsible for the actual on-disk refresh.
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from pipeline.config import GMAIL_SCOPES
    except Exception as exc:
        return CheckResult(
            "gmail_token", False, f"google-auth import failed: {exc}"
        )

    try:
        creds = Credentials.from_authorized_user_file(
            str(GOOGLE_TOKEN_GMAIL_FILE), GMAIL_SCOPES
        )
    except Exception as exc:
        result = CheckResult(
            "gmail_token", False, f"token file unreadable: {type(exc).__name__}"
        )
        _gmail_probe_cache["at"] = now
        _gmail_probe_cache["result"] = result
        return result

    if not creds.refresh_token:
        result = CheckResult(
            "gmail_token", False, "no refresh_token in token file — re-auth required"
        )
        _gmail_probe_cache["at"] = now
        _gmail_probe_cache["result"] = result
        return result

    try:
        if creds.expired:
            # In-memory refresh only (no write-back); if Google rejects the
            # refresh_token we flag unhealthy. Successful refresh means the
            # listener can refresh + persist on its next run.
            creds.refresh(Request())
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "?")
        result = CheckResult("gmail_token", True, f"OAuth OK ({email})")
    except Exception as exc:
        msg = str(exc)
        if "invalid_grant" in msg.lower() or "RefreshError" in type(exc).__name__:
            detail = "refresh token revoked — re-auth required"
        elif "HttpError" in type(exc).__name__:
            detail = f"Gmail API error: {msg[:120]}"
        else:
            detail = f"{type(exc).__name__}: {msg[:120]}"
        result = CheckResult("gmail_token", False, detail)

    _gmail_probe_cache["at"] = now
    _gmail_probe_cache["result"] = result
    return result


def check_email_ingest_fresh() -> CheckResult:
    """Alert if email_threads.json mtime is older than GMAIL_STALE_HOURS."""
    if not GMAIL_INGEST_ENABLED:
        return CheckResult("email_ingest", True, "GMAIL_INGEST_ENABLED=0 (skipped)")
    try:
        from pipeline.config import EMAIL_THREADS_FILE
    except Exception as exc:
        return CheckResult(
            "email_ingest", False, f"pipeline.config import failed: {exc}"
        )
    path = Path(EMAIL_THREADS_FILE)
    if not path.exists():
        # gmail_token check already pages when the token is missing; don't
        # double-alert here. Treat no-file as "hasn't run yet" (healthy).
        return CheckResult("email_ingest", True, "no email_threads.json yet")
    age_s = time.time() - path.stat().st_mtime
    age_h = age_s / 3600
    if age_s > GMAIL_STALE_HOURS * 3600:
        return CheckResult(
            "email_ingest",
            False,
            f"ingest stale: last run {age_h:.1f}h ago (threshold {GMAIL_STALE_HOURS}h)",
        )
    return CheckResult("email_ingest", True, f"fresh ({age_h:.1f}h ago)")


def check_listener() -> CheckResult:
    if LISTENER_HEALTH_URL:
        base = LISTENER_HEALTH_URL.rstrip("/")
        body = _http_get(base + "/", timeout=5)
        if body is not None and body.strip().lower().startswith("ok"):
            return CheckResult("listener", True, "HTTP health OK")
        return CheckResult(
            "listener",
            False,
            "Listener health HTTP not reachable",
        )
    try:
        result = subprocess.run(
            ["pgrep", "-f", "linkedin-listener"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            pids = result.stdout.decode().strip().split("\n")
            return CheckResult("listener", True, f"Running (PIDs: {', '.join(pids)})")
    except Exception:
        pass
    return CheckResult("listener", False, "Listener process not found")


def check_disk_space() -> CheckResult:
    """Alert if the app-data volume is running out of headroom."""
    import shutil
    try:
        usage = shutil.disk_usage("/app/data")
    except FileNotFoundError:
        return CheckResult("disk", True, "/app/data not present (dev env)")
    free_pct = (usage.free / usage.total) * 100 if usage.total else 0
    free_gb = usage.free / (1024 ** 3)
    if free_pct < DISK_FREE_THRESHOLD_PCT:
        return CheckResult(
            "disk",
            False,
            f"{free_pct:.1f}% free ({free_gb:.1f}G) — below {DISK_FREE_THRESHOLD_PCT}% threshold",
        )
    return CheckResult("disk", True, f"{free_pct:.0f}% free ({free_gb:.1f}G)")


def check_listener_live_fresh() -> CheckResult:
    """Catch the 'listener HTTP OK but no WebSocket frames arriving' case.

    inbox_live.json is append-only for every LI frame the listener receives.
    Its mtime is the proof-of-life signal for the CDP WebSocket subscription
    independent of the HTTP health port.
    """
    path = Path("/app/data/inbox_live.json")
    if not path.exists():
        # Fresh install; no frames yet. Treat as healthy rather than paging
        # on an empty box.
        return CheckResult("listener_live", True, "inbox_live.json not yet written")
    age_s = time.time() - path.stat().st_mtime
    age_h = age_s / 3600
    if age_s > LISTENER_LIVE_STALE_HOURS * 3600:
        return CheckResult(
            "listener_live",
            False,
            f"no inbound frames for {age_h:.1f}h (threshold {LISTENER_LIVE_STALE_HOURS}h)",
        )
    return CheckResult("listener_live", True, f"last frame {age_h:.1f}h ago")


def check_send_errors() -> CheckResult:
    """Scan send_history.jsonl for failures inside the alert window."""
    path = Path("/app/data/send_history.jsonl")
    if not path.exists():
        return CheckResult("send", True, "no send_history yet")
    cutoff = time.time() - SEND_ERROR_WINDOW_MIN * 60
    failures: list[dict[str, object]] = []
    try:
        with open(path) as f:
            # Tail-scan: read the last ~8KB only — failures in the last 30
            # min can't be far from EOF even in a busy sender.
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().splitlines()
    except OSError as exc:
        return CheckResult("send", False, f"cannot read send_history: {exc}")
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = entry.get("timestamp") or ""
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if not entry.get("ok", True):
            failures.append(entry)
    if not failures:
        return CheckResult("send", True, f"no failures in last {SEND_ERROR_WINDOW_MIN}m")
    sample = failures[-1]
    who = sample.get("recipient") or sample.get("urn", "?")
    err = str(sample.get("error") or "unknown")[:80]
    return CheckResult(
        "send",
        False,
        f"{len(failures)} send failure(s) in last {SEND_ERROR_WINDOW_MIN}m — latest {who}: {err}",
    )


_pipeline_errors_watermark: float = time.time()


def check_pipeline_errors() -> CheckResult:
    """Alert on new pipeline_errors.jsonl entries since the last check.

    Watermark is in-memory (resets on container restart — acceptable, since
    restart implies operator attention already). First run after boot reads
    from its start time, so stale historical errors don't spam.
    """
    global _pipeline_errors_watermark
    path = Path("/app/data/pipeline_errors.jsonl")
    if not path.exists():
        return CheckResult("pipeline_errors", True, "no errors logged")
    new_entries: list[dict[str, object]] = []
    latest_ts = _pipeline_errors_watermark
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = str(entry.get("timestamp") or "")
                try:
                    ts = datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    continue
                if ts <= _pipeline_errors_watermark:
                    continue
                new_entries.append(entry)
                if ts > latest_ts:
                    latest_ts = ts
    except OSError as exc:
        return CheckResult("pipeline_errors", False, f"cannot read log: {exc}")

    # Advance watermark regardless of check outcome so we don't re-alert
    # on the same entries every tick.
    _pipeline_errors_watermark = latest_ts

    if not new_entries:
        return CheckResult("pipeline_errors", True, "no new errors")
    sample = new_entries[-1]
    mod = sample.get("module", "?")
    kind = sample.get("kind", "?")
    detail = str(sample.get("detail") or "")[:80]
    return CheckResult(
        "pipeline_errors",
        False,
        f"{len(new_entries)} new error(s); latest {mod}/{kind}: {detail}",
    )


def check_sender_lock() -> CheckResult:
    """Detect a wedged LinkedIn sender by reading its holder sidecar.

    The sender creates data/.send_approved.holder on fcntl acquire and
    deletes it on release. If we see it older than the threshold, the
    sender is still holding the lock past a normal run — something's
    stuck (CDP hang, network wait, etc.). Missing sidecar = nothing
    running = healthy.
    """
    path = Path("/app/data/.send_approved.holder")
    if not path.exists():
        return CheckResult("sender_lock", True, "idle (no holder)")
    age = time.time() - path.stat().st_mtime
    if age > SENDER_LOCK_STALE_SEC:
        try:
            payload = json.loads(path.read_text())
            pid = payload.get("pid", "?")
        except (OSError, json.JSONDecodeError):
            pid = "?"
        return CheckResult(
            "sender_lock",
            False,
            f"sender wedged for {age/60:.0f}m (pid {pid})",
        )
    return CheckResult("sender_lock", True, f"active ({age:.0f}s)")


def check_cron_heartbeats() -> CheckResult:
    """One CheckResult summarizing all cron subcommand heartbeats.

    Missing heartbeat files are tolerated (treated as 'first deploy, cron
    hasn't run yet'); a heartbeat older than its max threshold fails the
    check with the list of offenders.
    """
    now = time.time()
    missed: list[str] = []
    details: list[str] = []
    for name, max_sec in CRON_HEARTBEAT_MAX_SEC.items():
        path = Path(f"/app/data/.cron.{name}.heartbeat")
        if not path.exists():
            details.append(f"{name}=<none>")
            continue
        age = now - path.stat().st_mtime
        age_h = age / 3600 if age >= 3600 else 0
        if age > max_sec:
            missed.append(
                f"{name} {age_h:.1f}h old (limit {max_sec/3600:.1f}h)"
                if age_h
                else f"{name} {age/60:.0f}m old (limit {max_sec/60:.0f}m)"
            )
        else:
            details.append(
                f"{name}={age_h:.1f}h" if age_h else f"{name}={age/60:.0f}m"
            )
    if missed:
        return CheckResult("cron", False, "; ".join(missed))
    if not details:
        return CheckResult("cron", True, "no heartbeats yet (first deploy)")
    return CheckResult("cron", True, "ok (" + " ".join(details) + ")")


def check_bot_heartbeat() -> CheckResult:
    """Telegram bot touches this file every poll_loop iteration.

    Polls are long (default 30s); a stale heartbeat past BOT_HEARTBEAT_STALE_SEC
    means the loop is wedged or the process is dead even if the container
    itself is still up (crash-then-restart could also look wedged for ~10s).
    """
    path = Path("/app/data/.telegram_bot.heartbeat")
    if not path.exists():
        return CheckResult(
            "telegram_bot",
            False,
            "heartbeat file missing — bot has never started cleanly",
        )
    age = time.time() - path.stat().st_mtime
    if age > BOT_HEARTBEAT_STALE_SEC:
        return CheckResult(
            "telegram_bot",
            False,
            f"heartbeat {age:.0f}s old (threshold {BOT_HEARTBEAT_STALE_SEC}s)",
        )
    return CheckResult("telegram_bot", True, f"poll_loop alive ({age:.0f}s)")


def check_review_http() -> CheckResult:
    """Ensure the review server is responding; covers process death."""
    if not REVIEW_HEALTH_URL:
        return CheckResult("review", True, "HEALTH_REVIEW_URL unset (skipped)")
    body = _http_get(REVIEW_HEALTH_URL, timeout=5)
    if body is None:
        return CheckResult("review", False, f"{REVIEW_HEALTH_URL} not reachable")
    # /api/send/status returns JSON; a sanity parse guards against some
    # random process bound to the port that isn't our server.
    try:
        json.loads(body)
    except json.JSONDecodeError:
        return CheckResult("review", False, "review returned non-JSON")
    return CheckResult("review", True, "HTTP OK")


def run_checks() -> list[CheckResult]:
    return [
        check_cdp(),
        check_linkedin_session(),
        check_qdrant(),
        check_listener(),
        check_listener_live_fresh(),
        check_review_http(),
        check_bot_heartbeat(),
        check_sender_lock(),
        check_cron_heartbeats(),
        check_pipeline_errors(),
        check_gmail_token(),
        check_email_ingest_fresh(),
        check_send_errors(),
        check_disk_space(),
    ]


def format_report(results: list[CheckResult]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Health Check [{now}]", ""]
    for r in results:
        icon = "OK" if r.healthy else "FAIL"
        lines.append(f"  [{icon}] {r.name}: {r.detail}")
    return "\n".join(lines)


def format_alert(results: list[CheckResult], recovered: bool = False) -> str:
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    if recovered:
        return f"[{now}] linkedin-leads: All services recovered."
    failures = [r for r in results if not r.healthy]
    lines = [f"[{now}] linkedin-leads: {len(failures)} service(s) down:"]
    for f in failures:
        lines.append(f"  - {f.name}: {f.detail}")
    if any(r.name == "linkedin" and not r.healthy for r in results):
        lines.append("")
        lines.append("ACTION: Log in via noVNC to restore the LinkedIn session.")
    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    return _http_post_json(url, {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    })


def send_webhook(message: str) -> bool:
    if not WEBHOOK_URL:
        return False
    return _http_post_json(WEBHOOK_URL, {"text": message, "content": message})


def notify(message: str) -> None:
    sent = []
    if send_telegram(message):
        sent.append("telegram")
    if send_webhook(message):
        sent.append("webhook")
    if sent:
        print(f"  Notified via: {', '.join(sent)}")
    elif TELEGRAM_TOKEN or WEBHOOK_URL:
        print("  WARNING: Notification send failed")


def watch(state: HealthState) -> None:
    results = run_checks()
    report = format_report(results)
    print(report)

    all_healthy = all(r.healthy for r in results)
    was_healthy = all(state.statuses.get(r.name, True) for r in results)

    now = time.time()

    if all_healthy and state.alerted_unhealthy:
        notify(format_alert(results, recovered=True))
        state.alerted_unhealthy = False
        state.last_alert_at = now
    elif not all_healthy and was_healthy:
        notify(format_alert(results))
        state.alerted_unhealthy = True
        state.last_alert_at = now
    elif not all_healthy and (now - state.last_alert_at) >= REMIND_INTERVAL:
        notify(format_alert(results) + "\n(reminder)")
        state.last_alert_at = now

    state.statuses = {r.name: r.healthy for r in results}


def main() -> None:
    is_watch = "--watch" in sys.argv

    if not is_watch:
        results = run_checks()
        print(format_report(results))
        sys.exit(0 if all(r.healthy for r in results) else 1)

    print(f"Health monitor starting (interval={CHECK_INTERVAL}s, remind={REMIND_INTERVAL}s)")
    channels = []
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        channels.append("Telegram")
    if WEBHOOK_URL:
        channels.append("Webhook")
    print(f"Notification channels: {', '.join(channels) or 'none configured'}")
    print()

    state = HealthState()
    while True:
        try:
            watch(state)
            print()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\nShutting down health monitor.")
            break


if __name__ == "__main__":
    main()

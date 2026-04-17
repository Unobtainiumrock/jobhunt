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

DOTENV = Path(__file__).resolve().parent.parent / ".env"


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


def run_checks() -> list[CheckResult]:
    return [check_cdp(), check_linkedin_session(), check_qdrant(), check_listener()]


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

#!/usr/bin/env python3
"""
Thin notification helper shared by health monitoring and the sender pipeline.

Reads credentials from .env at the project root (HEALTH_TELEGRAM_BOT_TOKEN,
HEALTH_TELEGRAM_CHAT_ID, HEALTH_WEBHOOK_URL), and exposes both a library
interface and a CLI so Node processes can shell out without reimplementing
the transport.

CLI usage:
  python infra/notify.py --message "hello"
  python infra/notify.py --message "hello" --channel telegram

Library usage:
  from infra.notify import notify, send_telegram
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DOTENV = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv() -> None:
    if not DOTENV.exists():
        return
    for raw_line in DOTENV.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()

TELEGRAM_TOKEN = os.getenv("HEALTH_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("HEALTH_TELEGRAM_CHAT_ID", "")
WEBHOOK_URL = os.getenv("HEALTH_WEBHOOK_URL", "")


def _http_post_json(url: str, data: dict, timeout: int = 10) -> bool:
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    return _http_post_json(url, {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    })


def send_telegram_with_webapp_button(
    message: str,
    button_text: str,
    webapp_url: str,
) -> bool:
    """Send a Telegram message with a single inline button that opens a
    WebApp (mini-app) inside Telegram. Used for draft-review pushes so
    the operator lands in the swipe UI with auth already handled.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    return _http_post_json(url, {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": button_text, "web_app": {"url": webapp_url}}
            ]],
        },
    })


def send_webhook(message: str) -> bool:
    if not WEBHOOK_URL:
        return False
    return _http_post_json(WEBHOOK_URL, {"text": message, "content": message})


def notify(message: str, channel: str = "all") -> list[str]:
    sent: list[str] = []
    if channel in ("all", "telegram") and send_telegram(message):
        sent.append("telegram")
    if channel in ("all", "webhook") and send_webhook(message):
        sent.append("webhook")
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a notification.")
    parser.add_argument("--message", "-m", required=True, help="Message body")
    parser.add_argument(
        "--channel",
        choices=["all", "telegram", "webhook"],
        default="all",
        help="Which channel(s) to dispatch on",
    )
    args = parser.parse_args()

    sent = notify(args.message, channel=args.channel)
    if sent:
        print(f"sent via: {', '.join(sent)}")
        return 0
    print("no channels succeeded (check .env credentials)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

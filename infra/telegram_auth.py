"""Verify Telegram WebApp `initData` payloads.

Telegram WebApps pass a signed query-string to the embedded page via
`Telegram.WebApp.initData`. Signature is HMAC-SHA256 with a secret derived
from the bot token. This module validates the signature, the timestamp
freshness, and (for single-user deployments) the sender's user id against
`HEALTH_TELEGRAM_CHAT_ID`.

Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

DOTENV = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv() -> None:
    if not DOTENV.exists():
        return
    for raw in DOTENV.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_dotenv()


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()


def verify_init_data(
    init_data: str,
    *,
    bot_token: str | None = None,
    expected_user_id: str | int | None = None,
    max_age_s: int = 3600,
) -> dict[str, Any] | None:
    """Return parsed fields if the payload is authentic + fresh + authorized.

    Returns ``None`` on ANY failure (bad signature, stale, wrong user, missing
    bot token). Callers should treat a None return as "send 401 and close".
    """
    if not init_data:
        return None
    bot_token = bot_token or os.getenv("HEALTH_TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        return None

    # `parse_qsl` decodes percent-escapes but preserves order.
    pairs = parse_qsl(init_data, keep_blank_values=True)
    data: dict[str, str] = dict(pairs)

    received_hash = data.pop("hash", "")
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    expected_hash = hmac.new(
        key=_secret_key(bot_token),
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > max_age_s:
        return None

    user_json = data.get("user") or ""
    if not user_json:
        return None
    try:
        user = json.loads(user_json)
    except json.JSONDecodeError:
        return None

    if expected_user_id is None:
        expected_user_id = os.getenv("HEALTH_TELEGRAM_CHAT_ID", "").strip()
    if expected_user_id:
        if str(user.get("id")) != str(expected_user_id):
            return None

    return {
        "user": user,
        "auth_date": auth_date,
        "start_param": data.get("start_param") or "",
        "raw": dict(data),
    }

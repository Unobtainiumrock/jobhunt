#!/usr/bin/env python3
"""Telegram bot control surface for the lead engine.

Adds a phone-first approval loop so the operator can act on drafts without
opening the web review UI. Long-polls Telegram's ``getUpdates`` endpoint and
dispatches a small command grammar:

    /status                 - summary of drafts + lead states
    /list [replies|followups] [N]
                            - top-N by score; echoes short URN tokens
    /approve <token>        - flips matching draft to ``approved``
    /reject  <token>        - flips matching draft to ``rejected``
    /help                   - command reference

The bot writes to ``data/inbox_classified.json`` (same surface the web UI
touches). ``/approve`` marks a reply approved then, when
``LINKEDIN_SEND_ENABLED=1``, immediately runs ``send-approved.mjs`` for that
thread (serialized with the review UI via ``data/.send_approved.lock``).

Safety guarantees:
- Only the configured ``HEALTH_TELEGRAM_CHAT_ID`` may issue commands.
- Tokens must match a single draft; ambiguous prefixes are refused.
- All writes are atomic: read -> mutate -> temp write -> rename.

Run it standalone for local testing:

    python infra/telegram_bot.py

Or under the Docker compose stack (``telegram_bot`` service in
docker-compose.yml) so it restarts with the rest of the system.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CLASSIFIED_FILE = DATA_DIR / "inbox_classified.json"
LEAD_STATES_FILE = DATA_DIR / "lead_states.json"
FOLLOWUPS_FILE = DATA_DIR / "entities" / "followups.json"

# Make the sibling infra package importable when this file is run directly
# (e.g. `python infra/telegram_bot.py`) without `python -m`.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env the same way infra/notify.py does so this works standalone.
from infra.notify import send_telegram  # noqa: E402  (side-effect: loads .env)
from pipeline.send_approved_exec import (  # noqa: E402
    build_send_argv,
    env_truthy,
    run_send_approved_with_lock,
)

TELEGRAM_TOKEN = os.getenv("HEALTH_TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("HEALTH_TELEGRAM_CHAT_ID", "").strip()
POLL_TIMEOUT_S = int(os.getenv("TELEGRAM_BOT_POLL_TIMEOUT", "30"))
TOKEN_LENGTH = 10  # short prefix of the conversation URN tail

logger = logging.getLogger("telegram_bot")


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def _http_get_json(url: str, timeout: int = POLL_TIMEOUT_S + 5) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def _conversation_token(urn: str) -> str:
    """Short, stable prefix of the conversation URN tail used in chat."""
    tail = urn.rsplit(",", 1)[-1].rstrip(")")
    return tail[:TOKEN_LENGTH]


def _participant_name(convo: dict[str, Any]) -> str:
    user = os.environ.get("LINKEDIN_USER_NAME", "Nicholas J. Fleischhauer")
    for part in convo.get("participants", []):
        name = part.get("name") or ""
        if name and name != user:
            return name
    return "(unknown)"


def _draftable(convo: dict[str, Any]) -> bool:
    reply = convo.get("reply") or {}
    status = reply.get("status")
    return status in ("draft", "auto_send")


def _match_token(token: str, conversations: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Resolve ``token`` to exactly one conversation; otherwise explain why."""
    token = token.strip()
    if not token:
        return None, "token required"
    matches = [c for c in conversations if _conversation_token(c["conversationUrn"]).startswith(token)]
    if not matches:
        return None, f"no draft matches <code>{token}</code>"
    if len(matches) > 1:
        sample = ", ".join(_conversation_token(c["conversationUrn"]) for c in matches[:3])
        return None, f"ambiguous token <code>{token}</code> (matches: {sample})"
    return matches[0], ""


def cmd_help() -> str:
    return (
        "<b>Commands</b>\n"
        "/status - summary\n"
        "/list [replies|followups] [N] - top-N drafts\n"
        "/approve &lt;token&gt; - mark draft approved\n"
        "/reject &lt;token&gt; - mark draft rejected\n"
        "/help - this message\n\n"
        "<i>Tokens are the short prefixes shown by /list. Live sends require\n"
        "LINKEDIN_SEND_ENABLED=1 on the server (same as the review UI).</i>"
    )


def cmd_status() -> str:
    data = _load_json(CLASSIFIED_FILE, {"conversations": []})
    convos = data.get("conversations", [])

    status_counts: dict[str, int] = {}
    for c in convos:
        reply = c.get("reply")
        if not reply:
            continue
        status_counts[reply.get("status", "unknown")] = status_counts.get(reply.get("status", "unknown"), 0) + 1

    followups_doc = _load_json(FOLLOWUPS_FILE, {"followups": []}) or {}
    followup_list = followups_doc.get("followups", [])
    pending_followups = sum(
        1
        for f in followup_list
        if (f.get("status") or "pending") in ("pending", "draft", "auto_send")
    )

    lead_states = _load_json(LEAD_STATES_FILE, {}) or {}
    state_counts: dict[str, int] = {}
    for entry in lead_states.values():
        state = entry.get("state") or "unknown"
        state_counts[state] = state_counts.get(state, 0) + 1

    lines = ["<b>Lead engine status</b>"]
    if status_counts:
        lines.append("<b>Drafts:</b>")
        for k in sorted(status_counts):
            lines.append(f"  • {k}: {status_counts[k]}")
    lines.append(f"<b>Pending follow-ups:</b> {pending_followups}")
    if state_counts:
        lines.append("<b>Lead states:</b>")
        for k in sorted(state_counts):
            lines.append(f"  • {k}: {state_counts[k]}")
    return "\n".join(lines)


def cmd_list(args: list[str]) -> str:
    kind = "replies"
    limit = 5
    for a in args:
        if a in ("replies", "followups"):
            kind = a
        elif a.isdigit():
            limit = max(1, min(20, int(a)))

    if kind == "replies":
        data = _load_json(CLASSIFIED_FILE, {"conversations": []})
        convos = [c for c in data.get("conversations", []) if _draftable(c)]
        # Best-effort score extraction; absent scores sort last.
        convos.sort(
            key=lambda c: (c.get("reply", {}).get("score") or 0),
            reverse=True,
        )
        convos = convos[:limit]
        if not convos:
            return "No draftable replies."
        lines = [f"<b>Top {len(convos)} reply drafts</b>"]
        for c in convos:
            tok = _conversation_token(c["conversationUrn"])
            name = _participant_name(c)
            score = c.get("reply", {}).get("score")
            score_txt = f" · score {score:.2f}" if isinstance(score, (int, float)) else ""
            preview = (c.get("reply", {}).get("text") or "")[:90].replace("\n", " ")
            lines.append(f"<code>{tok}</code> {name}{score_txt}\n  <i>{preview}…</i>")
        return "\n\n".join(lines)

    followups_doc = _load_json(FOLLOWUPS_FILE, {"followups": []}) or {}
    items = [
        f
        for f in followups_doc.get("followups", [])
        if (f.get("status") or "pending") in ("pending", "draft", "auto_send")
    ]
    items = items[:limit]
    if not items:
        return "No pending follow-ups."
    lines = [f"<b>Top {len(items)} follow-ups</b>"]
    for f in items:
        urn = f.get("conversation_urn") or f.get("urn") or ""
        tok = _conversation_token(urn) if urn else "(no-urn)"
        name = f.get("recipient") or f.get("name") or "(unknown)"
        msg = (f.get("message") or "")[:90].replace("\n", " ")
        lines.append(f"<code>{tok}</code> {name}\n  <i>{msg}…</i>")
    return "\n\n".join(lines)


def _mutate_reply(token: str, new_status: str) -> str:
    data = _load_json(CLASSIFIED_FILE, {"conversations": []})
    convos = data.get("conversations", [])
    draftable = [c for c in convos if _draftable(c)]
    convo, err = _match_token(token, draftable)
    if convo is None:
        return err
    now = datetime.now(timezone.utc).isoformat()
    convo["reply"]["status"] = new_status
    convo["reply"][f"{new_status}_at"] = now
    if new_status == "approved":
        convo["reply"]["approved_text"] = convo["reply"].get("text", "")
    _atomic_write_json(CLASSIFIED_FILE, data)
    name = _participant_name(convo)
    base = (
        f"✅ <b>{new_status}</b> — {name} "
        f"(<code>{_conversation_token(convo['conversationUrn'])}</code>)"
    )
    if new_status != "approved":
        return base
    if not env_truthy("LINKEDIN_SEND_ENABLED", default=False):
        return base + "\n\n<i>Approved only — LINKEDIN_SEND_ENABLED=0 (no send).</i>"
    urn = str(convo.get("conversationUrn") or "")
    if not urn:
        return base + "\n\n⚠️ Missing conversation URN; cannot send."
    argv = build_send_argv(only="replies", max_items=1, live=True, reply_urn=urn)
    try:
        proc = run_send_approved_with_lock(argv)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("telegram approve autosend failed")
        return base + f"\n\n⚠️ Send error: {exc}"
    if proc.returncode == 0:
        return base + "\n\n<b>Sent</b> to LinkedIn."
    tail = (proc.stderr or proc.stdout or "").strip()[-300:]
    return base + f"\n\n⚠️ Send failed (exit {proc.returncode}). {tail}"


def cmd_approve(args: list[str]) -> str:
    if not args:
        return "usage: /approve &lt;token&gt;"
    return _mutate_reply(args[0], "approved")


def cmd_reject(args: list[str]) -> str:
    if not args:
        return "usage: /reject &lt;token&gt;"
    return _mutate_reply(args[0], "rejected")


COMMANDS = {
    "/help": lambda args: cmd_help(),
    "/start": lambda args: cmd_help(),
    "/status": lambda args: cmd_status(),
    "/list": cmd_list,
    "/approve": cmd_approve,
    "/reject": cmd_reject,
}


def dispatch(text: str) -> str:
    parts = text.strip().split()
    if not parts:
        return ""
    cmd, *rest = parts
    # Support /cmd@bot_name format from group chats.
    cmd = cmd.split("@", 1)[0].lower()
    handler = COMMANDS.get(cmd)
    if not handler:
        return f"unknown command: <code>{cmd}</code>\n\n{cmd_help()}"
    try:
        return handler(rest)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("command %s failed", cmd)
        return f"error: {exc}"


def _authorised(chat_id: Any) -> bool:
    if not TELEGRAM_CHAT_ID:
        return False
    return str(chat_id) == TELEGRAM_CHAT_ID


def poll_loop() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("HEALTH_TELEGRAM_BOT_TOKEN / HEALTH_TELEGRAM_CHAT_ID missing; bot idle")
        sys.exit(2)

    logger.info("Telegram bot online (chat %s)", TELEGRAM_CHAT_ID)
    offset: int | None = None

    while True:
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT_S}
        if offset is not None:
            params["offset"] = offset
        url = _api_url("getUpdates") + "?" + urllib.parse.urlencode(params)
        try:
            payload = _http_get_json(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("getUpdates failed: %s", exc)
            time.sleep(5)
            continue

        for update in payload.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text") or ""
            if not _authorised(chat_id):
                logger.warning("unauthorised chat %s tried: %r", chat_id, text)
                continue
            if not text:
                continue
            reply = dispatch(text)
            if reply:
                send_telegram(reply)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", help="Run a single command, print, and exit", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    if args.once:
        print(dispatch(args.once))
        return 0

    poll_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

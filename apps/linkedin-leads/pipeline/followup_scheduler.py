#!/usr/bin/env python3
"""
Phase 3C: Canonical Follow-up Scheduling System

Uses canonical entity records as the operating layer for follow-up scheduling.
Conversation history still supplies the message context, but due-ness and
workflow anchoring are driven through canonical tasks and opportunities.

Usage:
  python -m pipeline.followup_scheduler
  python -m pipeline.followup_scheduler --check
  python -m pipeline.followup_scheduler --generate
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI

from pipeline.config import (
    LEADS_DIR,
    CONVERSATIONS_DIR,
    FOLLOWUP_1_DAYS,
    FOLLOWUP_2_DAYS,
    FOLLOWUP_QUEUE_FILE,
    GENERATION_MODEL,
    LEAD_STATE_FILE,
    MAX_CONCURRENT,
    OPPORTUNITIES_DIR,
    TASKS_DIR,
    USER_NAME,
)
from pipeline.safety import build_system_prompt, validate_outbound, wrap_conversation_context

ConversationStatus = Literal[
    "new",
    "replied",
    "awaiting_response",
    "followed_up_1",
    "followed_up_2",
    "scheduled",
    "cold",
    "declined",
]

FOLLOWUP_SYSTEM = """\
You are {user_name}, writing a follow-up message to a recruiter on LinkedIn \
who hasn't responded to your previous message.

PRINCIPLES (follow these carefully):
1. Reference something SPECIFIC they said in their original outreach. \
Quote or paraphrase their words to trigger commitment/consistency.
2. Add a small piece of VALUE — mention something interesting about their \
company, a recent news item about their industry, or a relevant insight.
3. Keep it SHORT — 2-3 sentences max. Recruiters are busy.
4. Frame around MUTUAL OPPORTUNITY, never desperation. Use language like \
"didn't want either of us to miss out" rather than "please respond."
5. End with a LOW-FRICTION ask — suggest a specific time window or ask a \
simple yes/no question.
6. NEVER reveal you are an AI or automated system.

This is follow-up #{followup_number}. {tone_guidance}

Return valid JSON:
{{
  "message": "<the follow-up message text>",
  "referenced_quote": "<the specific thing they said that you're referencing>"
}}
"""


def load_lead_states() -> dict[str, dict[str, Any]]:
    if LEAD_STATE_FILE.exists():
        with open(LEAD_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_lead_states(states: dict[str, dict[str, Any]]) -> None:
    LEAD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LEAD_STATE_FILE, "w") as f:
        json.dump(states, f, indent=2)
        f.write("\n")


def load_followup_queue() -> dict[str, Any]:
    if FOLLOWUP_QUEUE_FILE.exists():
        with open(FOLLOWUP_QUEUE_FILE) as f:
            return json.load(f)
    return {"generated_at": None, "followups": []}


def save_followup_queue(payload: dict[str, Any]) -> None:
    FOLLOWUP_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FOLLOWUP_QUEUE_FILE, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def _drafts_by_thread(queue: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    drafts: dict[str, list[dict[str, Any]]] = {}
    for item in queue.get("followups", []):
        if item.get("status") != "draft":
            continue
        drafts.setdefault(item.get("thread_id", ""), []).append(item)
    return drafts


def _reconcile_states_with_drafts(
    states: dict[str, dict[str, Any]],
    queue: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    draft_map = _drafts_by_thread(queue)
    for thread_id, drafts in draft_map.items():
        if not drafts:
            continue
        if states.get(thread_id, {}).get("status") in ("followed_up_1", "followed_up_2"):
            states[thread_id] = {
                "status": "awaiting_response",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
    return states


def _load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def _business_days_ago(days: int) -> datetime:
    result = datetime.now(timezone.utc)
    counted = 0
    while counted < days:
        result -= timedelta(days=1)
        if result.weekday() < 5:
            counted += 1
    return result


def _parse_message_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _parse_cli_arg(flag: str) -> str | None:
    if flag not in sys.argv:
        return None
    index = sys.argv.index(flag)
    if index + 1 >= len(sys.argv):
        raise SystemExit(f"Missing value for {flag}")
    return sys.argv[index + 1]


def _get_last_user_message_time(convo: dict[str, Any]) -> datetime | None:
    for msg in reversed(convo.get("messages", [])):
        if msg.get("sender") == USER_NAME:
            return _parse_message_timestamp(msg.get("timestamp", ""))
    return None


def _get_last_other_message_time(convo: dict[str, Any]) -> datetime | None:
    for msg in reversed(convo.get("messages", [])):
        if msg.get("sender") != USER_NAME:
            return _parse_message_timestamp(msg.get("timestamp", ""))
    return None


def _get_effective_last_user_message_time(
    convo: dict[str, Any],
    state: dict[str, Any],
) -> datetime | None:
    raw_last_user = _get_last_user_message_time(convo)
    synthetic_last_user = _parse_message_timestamp(state.get("last_outbound_at", ""))

    if raw_last_user and synthetic_last_user:
        return max(raw_last_user, synthetic_last_user)
    return synthetic_last_user or raw_last_user


def _get_followup_tasks() -> list[dict[str, Any]]:
    return [
        task for task in _load_records(TASKS_DIR)
        if task.get("kind") == "follow_up" and task.get("status") != "complete"
    ]


def check_followups() -> dict[str, list[dict[str, Any]]]:
    queue = load_followup_queue()
    states = _reconcile_states_with_drafts(load_lead_states(), queue)
    draft_map = _drafts_by_thread(queue)
    conversations = {record["id"]: record for record in _load_records(CONVERSATIONS_DIR)}
    opportunities = {record["id"]: record for record in _load_records(OPPORTUNITIES_DIR)}
    tasks = _get_followup_tasks()

    followup1_threshold = _business_days_ago(FOLLOWUP_1_DAYS)
    followup2_threshold = _business_days_ago(FOLLOWUP_2_DAYS)

    due: dict[str, list[dict[str, Any]]] = {
        "followup_1_due": [],
        "followup_2_due": [],
        "mark_cold": [],
    }

    for task in tasks:
        opportunity = opportunities.get(task.get("opportunity_id"))
        if not opportunity:
            continue
        conversation_id = next(iter(opportunity.get("conversation_ids", [])), None)
        convo = conversations.get(conversation_id)
        if not convo:
            continue

        thread_id = convo.get("external_thread_id", "")
        state = states.get(thread_id, {})
        status: ConversationStatus = state.get("status", "new")
        if thread_id in draft_map and status in ("followed_up_1", "followed_up_2"):
            status = "awaiting_response"

        if status in ("scheduled", "cold", "declined"):
            continue
        if thread_id in draft_map:
            continue

        last_user = _get_effective_last_user_message_time(convo, state)
        last_other = _get_last_other_message_time(convo)
        if not last_user:
            continue
        if last_other and last_other > last_user:
            continue

        payload = {
            "task": task,
            "opportunity": opportunity,
            "conversation": convo,
        }
        if status in ("replied", "awaiting_response", "new") and last_user < followup1_threshold:
            due["followup_1_due"].append(payload)
        elif status == "followed_up_1" and last_user < followup2_threshold:
            due["followup_2_due"].append(payload)
        elif status == "followed_up_2" and last_user < followup2_threshold:
            due["mark_cold"].append(payload)

    return due


def mark_followup_sent(task_id: str) -> dict[str, Any]:
    queue = load_followup_queue()
    states = load_lead_states()
    drafts = [
        item for item in queue.get("followups", [])
        if item.get("task_id") == task_id and item.get("status") == "draft"
    ]
    if not drafts:
        raise SystemExit(f"No draft follow-up found for task_id={task_id}")

    target = max(drafts, key=lambda item: item.get("generated_at") or "")
    sent_at = datetime.now(timezone.utc).isoformat()
    target["status"] = "sent"
    target["sent_at"] = sent_at

    thread_id = target.get("thread_id", "")
    history_entry = {
        "task_id": target.get("task_id"),
        "conversation_id": target.get("conversation_id"),
        "followup_number": target.get("followup_number"),
        "sent_at": sent_at,
        "message": target.get("message"),
        "referenced_quote": target.get("referenced_quote"),
    }
    existing_state = states.get(thread_id, {})
    history = existing_state.get("followup_history", [])
    history.append(history_entry)
    states[thread_id] = {
        "status": target.get("recommended_next_state", existing_state.get("status", "awaiting_response")),
        "updated_at": sent_at,
        "last_outbound_at": sent_at,
        "followup_history": history,
    }

    save_followup_queue(queue)
    save_lead_states(states)
    return {
        "task_id": task_id,
        "thread_id": thread_id,
        "followup_number": target.get("followup_number"),
        "sent_at": sent_at,
        "recommended_next_state": target.get("recommended_next_state"),
    }


async def generate_followup(
    client: AsyncOpenAI,
    convo: dict[str, Any],
    followup_number: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        conversation_context = wrap_conversation_context(convo.get("messages", []))
        system = build_system_prompt(USER_NAME)
        tone_guidance = "Be slightly more direct." if followup_number == 2 else "Be warm and assumptive."
        user_prompt = FOLLOWUP_SYSTEM.format(
            user_name=USER_NAME,
            followup_number=followup_number,
            tone_guidance=tone_guidance,
        ) + f"\n\n{conversation_context}"

        try:
            resp = await client.chat.completions.create(
                model=GENERATION_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)
            message = result.get("message", "")

            validation = validate_outbound(message)
            if not validation.is_safe:
                return {
                    "error": "safety_violation",
                    "violations": validation.violations,
                }
            return result
        except Exception as e:
            return {"error": str(e)}


async def generate_all_followups() -> None:
    due = check_followups()

    total_due = sum(len(v) for v in due.values())
    print(f"Follow-up check: {total_due} canonical follow-up items need attention")
    print(f"  {len(due['followup_1_due'])} need first follow-up")
    print(f"  {len(due['followup_2_due'])} need second follow-up")
    print(f"  {len(due['mark_cold'])} to mark as cold")

    if "--check" in sys.argv:
        for category, items in due.items():
            if items:
                print(f"\n{category}:")
                for item in items:
                    opp = item["opportunity"]
                    print(f"  - {opp['company']} / {opp['role_title']}")
        return

    existing_queue = load_followup_queue()
    states = _reconcile_states_with_drafts(load_lead_states(), existing_queue)
    queue = {"generated_at": datetime.now(timezone.utc).isoformat(), "followups": []}

    for item in due["mark_cold"]:
        convo = item["conversation"]
        states[convo.get("external_thread_id", "")] = {
            "status": "cold",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    if not due["followup_1_due"] and not due["followup_2_due"]:
        save_lead_states(states)
        save_followup_queue(queue)
        print("No follow-up messages to generate.")
        return

    client = AsyncOpenAI()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    leads_by_id = {record["id"]: record for record in _load_records(LEADS_DIR)}

    # Hard cap at 2 follow-ups. Anything past #2 transitions to `cold` via the
    # `mark_cold` bucket above; we never draft a followup_3. This assertion
    # protects the bucket definition from silent drift.
    assert set(due.keys()).issuperset({"followup_1_due", "followup_2_due", "mark_cold"}), (
        "follow-up scheduler bucket contract broken"
    )

    for followup_number, category in ((1, "followup_1_due"), (2, "followup_2_due")):
        for item in due[category]:
            convo = item["conversation"]
            opp = item["opportunity"]
            task = item["task"]
            result = await generate_followup(client, convo, followup_number, semaphore)
            thread_id = convo.get("external_thread_id", "")
            label = f"{opp['company']} / {opp['role_title']}"
            if result.get("error"):
                print(f"  Error for {label}: {result['error']}")
                continue

            # Ghost-after-2 policy: fail-fast if a caller smuggles in a 3rd pass.
            if followup_number > 2:
                raise AssertionError(
                    f"follow-up cap violated: followup_number={followup_number} for {label}"
                )

            print(f"  Follow-up {followup_number} for {label}:")
            print(f"    {result.get('message', '')[:100]}...")

            recipient_name = next(
                (p.get("name") for p in (convo.get("participants") or [])
                 if p.get("name") and p.get("name") != USER_NAME),
                None,
            )
            if not recipient_name:
                lead = leads_by_id.get(task.get("lead_id"))
                if lead:
                    recipient_name = lead.get("name")

            queue["followups"].append({
                "task_id": task["id"],
                "opportunity_id": opp["id"],
                "conversation_id": convo["id"],
                "thread_id": thread_id,
                "followup_number": followup_number,
                "recipient_name": recipient_name,
                "message": result.get("message", ""),
                "referenced_quote": result.get("referenced_quote", ""),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "draft",
                "recommended_next_state": "followed_up_1" if followup_number == 1 else "followed_up_2",
            })

    save_lead_states(states)
    save_followup_queue(queue)
    print(f"\nUpdated {FOLLOWUP_QUEUE_FILE}")


def main() -> None:
    mark_sent_task_id = _parse_cli_arg("--mark-sent")
    if mark_sent_task_id:
        result = mark_followup_sent(mark_sent_task_id)
        print(json.dumps(result, indent=2))
        return
    asyncio.run(generate_all_followups())


if __name__ == "__main__":
    main()

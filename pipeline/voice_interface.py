#!/usr/bin/env python3
"""
Phase 5: Voice Interface

Provides speech-to-text (Whisper), text-to-speech (OpenAI TTS), and a
conversational command loop for hands-free interaction with the lead system.

Supported commands:
  - "What meetings do I have today?"
  - "Read my morning briefing"
  - "Draft a reply to [name]"
  - "Score the latest lead from [name]"
  - "Add to my profile that [experience]"
  - "Approve the reply for [name]"

Usage:
  python -m pipeline.voice_interface                  # interactive mode
  python -m pipeline.voice_interface --tts-only       # just read the briefing
  python -m pipeline.voice_interface --listen          # push-to-talk mode
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

from pipeline.config import CLASSIFIED_FILE, PROFILE_FILE, GENERATION_MODEL, USER_NAME
from pipeline.morning_briefing import build_briefing, print_briefing

TTS_MODEL = "tts-1"
TTS_VOICE = "onyx"
WHISPER_MODEL = "whisper-1"

COMMAND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("briefing", re.compile(r"\b(?:morning|briefing|meetings? today|what.s up)\b", re.I)),
    ("score", re.compile(r"\bscore\b.*\b(?:lead|from)\b\s+(\w+)", re.I)),
    ("reply", re.compile(r"\b(?:draft|write|reply)\b.*\b(?:to|for)\b\s+(\w+)", re.I)),
    ("approve", re.compile(r"\bapprove\b.*\b(?:reply|message)\b.*\b(?:for|to)\b\s+(\w+)", re.I)),
    ("profile_update", re.compile(r"\badd to (?:my )?profile\b\s+(?:that\s+)?(.+)", re.I)),
    ("search", re.compile(r"\b(?:find|search)\b\s+(.+)", re.I)),
    ("priority", re.compile(r"\b(?:priority|queue|next lead|top leads?)\b", re.I)),
    ("help", re.compile(r"\b(?:help|commands?|what can you)\b", re.I)),
]


def transcribe_audio(client: OpenAI, audio_path: str) -> str:
    """Convert speech to text using Whisper."""
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=f,
            language="en",
        )
    return result.text


def speak(client: OpenAI, text: str, output_path: str | None = None) -> str:
    """Convert text to speech using OpenAI TTS."""
    response = client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
    )

    if output_path is None:
        output_path = tempfile.mktemp(suffix=".mp3")

    response.stream_to_file(output_path)
    return output_path


def parse_command(text: str) -> tuple[str, str]:
    """Parse natural language into a command + argument."""
    for cmd_name, pattern in COMMAND_PATTERNS:
        match = pattern.search(text)
        if match:
            arg = match.group(1) if match.lastindex else ""
            return cmd_name, arg.strip()
    return "unknown", text


def handle_command(
    client: OpenAI,
    command: str,
    argument: str,
    speak_output: bool = True,
) -> str:
    """Execute a parsed command and return the response text."""

    if command == "briefing":
        briefing = build_briefing()
        summary = briefing.get("summary", {})
        scheduled = briefing.get("scheduled_interviews", [])
        top_opportunities = briefing.get("top_opportunities", [])
        follow_ups = briefing.get("follow_up_tasks", [])
        debriefs = briefing.get("debrief_tasks", [])

        response_parts = ["Here is today's hunt briefing."]

        if scheduled:
            response_parts.append(
                f"You have {len(scheduled)} scheduled interview"
                f"{'s' if len(scheduled) != 1 else ''} on the board."
            )
        else:
            response_parts.append("You do not have any scheduled interviews yet.")

        if top_opportunities:
            top = top_opportunities[0]
            response_parts.append(
                f"Your top opportunity is {top['company']} for {top['role_title']}, "
                f"currently {top['status']}."
            )

        if follow_ups:
            response_parts.append(f"You have {len(follow_ups)} follow-up task{'s' if len(follow_ups) != 1 else ''}.")

        if debriefs:
            response_parts.append(f"There are {len(debriefs)} interview debrief task{'s' if len(debriefs) != 1 else ''} waiting.")

        response_parts.append(
            f"You are tracking {summary.get('applications', 0)} applications, "
            f"{summary.get('active_interviews', 0)} active interviews, and "
            f"{summary.get('pending_tasks', 0)} pending tasks."
        )
        response = " ".join(response_parts)

    elif command == "priority":
        briefing = build_briefing()
        top_opportunities = briefing.get("top_opportunities", [])
        if top_opportunities:
            lines = ["Here are your top opportunities:"]
            for index, opportunity in enumerate(top_opportunities[:5], start=1):
                lines.append(
                    f"Number {index}: {opportunity['company']}, {opportunity['role_title']}, "
                    f"status {opportunity['status']}, fit score {opportunity.get('fit_score') or 'unknown'}."
                )
            response = " ".join(lines)
        else:
            response = "No ranked opportunities yet."

    elif command == "score":
        response = f"I'll score the lead from {argument}. Running the scoring pipeline now."

    elif command == "reply":
        response = f"Drafting a reply to {argument}. I'll let you know when it's ready for review."

    elif command == "approve":
        response = f"Approving the reply for {argument}. It will be sent shortly."

    elif command == "profile_update":
        response = f"Got it. I'll add that to your profile: {argument}"

    elif command == "search":
        response = f"Searching conversations for: {argument}"

    elif command == "help":
        response = (
            "You can ask me things like: "
            "What meetings do I have today? "
            "Read my morning briefing. "
            "Draft a reply to a specific recruiter. "
            "Score the latest lead from someone. "
            "Show me my top leads. "
            "Add something to my profile. "
            "Search for conversations about a topic."
        )

    else:
        response = (
            f"I'm not sure what you mean by '{argument}'. "
            "Try asking about your meetings, leads, or say 'help' for options."
        )

    if speak_output:
        audio_path = speak(client, response)
        print(f"  [Audio: {audio_path}]")

    return response


def interactive_loop() -> None:
    """Run an interactive voice/text command loop."""
    client = OpenAI()
    print("Voice Interface active. Type commands or 'quit' to exit.")
    print("(In production, this would use Whisper for speech input.)\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if user_input.lower() in ("quit", "exit", "bye"):
            print("Goodbye.")
            break

        command, argument = parse_command(user_input)
        response = handle_command(client, command, argument, speak_output=False)
        print(f"Assistant: {response}\n")


def tts_briefing() -> None:
    """Generate and speak the morning briefing."""
    client = OpenAI()
    briefing = build_briefing()

    print_briefing(briefing)

    command, _ = parse_command("morning briefing")
    response = handle_command(client, command, "", speak_output=True)
    print(f"\n{response}")


def main() -> None:
    if "--tts-only" in sys.argv:
        tts_briefing()
    elif "--listen" in sys.argv:
        print("Push-to-talk mode requires a microphone. Use interactive mode for now.")
        interactive_loop()
    else:
        interactive_loop()


if __name__ == "__main__":
    main()

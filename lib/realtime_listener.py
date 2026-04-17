"""
Phase 6A: Reusable Real-time Listener Pattern

Extracted common pattern from gravity-pulse (Slack listener) and linkedin-leads
(LinkedIn listener). Provides a framework for observing WebSocket traffic
via CDP and routing messages through a processing pipeline.

The listener pattern:
  1. Connect to Chrome via CDP
  2. Enable Network domain
  3. Observe WebSocket frames
  4. Parse and filter messages
  5. Route to handlers (classify, embed, store)
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass
class ParsedMessage:
    sender: str
    text: str
    channel: str
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)


class MessageHandler(ABC):
    """Base class for message processing handlers."""

    @abstractmethod
    def handle(self, message: ParsedMessage) -> None:
        ...

    def on_error(self, error: Exception, message: ParsedMessage) -> None:
        pass


class StorageHandler(MessageHandler):
    """Appends messages to a JSON file with lockfile."""

    def __init__(self, file_path: str, lock_path: str | None = None):
        self.file_path = file_path
        self.lock_path = lock_path or f"{file_path}.lock"

    def handle(self, message: ParsedMessage) -> None:
        import fcntl
        from pathlib import Path

        path = Path(self.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        lock_fd = open(self.lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            data: dict[str, Any] = {"messages": []}
            if path.exists():
                with open(path) as f:
                    data = json.load(f)

            data["messages"].append({
                "sender": message.sender,
                "text": message.text,
                "channel": message.channel,
                "timestamp": message.timestamp,
                "received_at": datetime.now(timezone.utc).isoformat(),
                **message.metadata,
            })
            data["lastUpdated"] = datetime.now(timezone.utc).isoformat()

            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


class PipelineHandler(MessageHandler):
    """Routes messages through a processing pipeline via stdin/stdout."""

    def __init__(self, process_stdin: Any):
        self.stdin = process_stdin

    def handle(self, message: ParsedMessage) -> None:
        if self.stdin and self.stdin.writable:
            line = json.dumps({
                "sender": message.sender,
                "text": message.text,
                "channel": message.channel,
                "timestamp": message.timestamp,
            })
            self.stdin.write(f"{line}\n".encode())
            self.stdin.flush()


@dataclass
class ListenerConfig:
    """Configuration for a real-time listener."""
    cdp_port: int = 9222
    handlers: list[MessageHandler] = field(default_factory=list)
    message_parser: Callable[[str], ParsedMessage | None] | None = None
    heartbeat_interval_ms: int = 60000
    filter_fn: Callable[[ParsedMessage], bool] | None = None


def route_message(message: ParsedMessage, config: ListenerConfig) -> None:
    """Route a parsed message through all configured handlers."""
    if config.filter_fn and not config.filter_fn(message):
        return
    for handler in config.handlers:
        try:
            handler.handle(message)
        except Exception as e:
            handler.on_error(e, message)

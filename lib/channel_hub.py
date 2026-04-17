"""
Phase 6B: Multi-Channel Communication Hub

Unified interface for sending/receiving messages across channels.
Each channel adapter implements the same interface, allowing the
classify -> score -> respond pipeline to work identically regardless
of where the message originated.

Channels:
  - LinkedIn (via CDP)
  - Email (via Gmail API)
  - SMS (via Twilio)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class UnifiedMessage:
    """Channel-agnostic message representation."""
    id: str
    channel: str
    sender_name: str
    sender_id: str
    text: str
    subject: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    success: bool
    message_id: str = ""
    error: str = ""
    channel: str = ""


class ChannelAdapter(ABC):
    """Base class for channel adapters."""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        ...

    @abstractmethod
    async def send(self, recipient_id: str, text: str, **kwargs: Any) -> SendResult:
        ...

    @abstractmethod
    async def fetch_new(self, since: datetime | None = None) -> list[UnifiedMessage]:
        ...

    @abstractmethod
    async def mark_read(self, message_id: str) -> bool:
        ...


class LinkedInAdapter(ChannelAdapter):
    """LinkedIn messaging via CDP browser automation."""

    @property
    def channel_name(self) -> str:
        return "linkedin"

    async def send(self, recipient_id: str, text: str, **kwargs: Any) -> SendResult:
        # CDP-based send: navigate to conversation, type message, submit
        # Requires active Chrome session with CDP
        return SendResult(
            success=False,
            error="CDP send not yet implemented — requires active Chrome session",
            channel=self.channel_name,
        )

    async def fetch_new(self, since: datetime | None = None) -> list[UnifiedMessage]:
        # Uses the listener or scraper to pull new messages
        return []

    async def mark_read(self, message_id: str) -> bool:
        return False


class EmailAdapter(ChannelAdapter):
    """Email via Gmail API.

    Inbound fetch + sidecar persistence live in ``pipeline.email_ingest`` /
    ``pipeline.email_gmail`` (run ``npm run email:ingest`` or the pipeline step).
    This adapter remains a thin placeholder until send is implemented.
    """

    @property
    def channel_name(self) -> str:
        return "email"

    async def send(self, recipient_id: str, text: str, **kwargs: Any) -> SendResult:
        # Gmail API send
        return SendResult(
            success=False,
            error="Gmail outbound send not yet implemented (use pipeline.email_ingest for inbound)",
            channel=self.channel_name,
        )

    async def fetch_new(self, since: datetime | None = None) -> list[UnifiedMessage]:
        return []

    async def mark_read(self, message_id: str) -> bool:
        return False


class SMSAdapter(ChannelAdapter):
    """SMS via Twilio."""

    @property
    def channel_name(self) -> str:
        return "sms"

    async def send(self, recipient_id: str, text: str, **kwargs: Any) -> SendResult:
        # Twilio API send
        return SendResult(
            success=False,
            error="Twilio not yet configured",
            channel=self.channel_name,
        )

    async def fetch_new(self, since: datetime | None = None) -> list[UnifiedMessage]:
        return []

    async def mark_read(self, message_id: str) -> bool:
        return False


class ChannelHub:
    """Routes messages through the unified pipeline regardless of channel."""

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        self._adapters[adapter.channel_name] = adapter

    def get_adapter(self, channel: str) -> ChannelAdapter | None:
        return self._adapters.get(channel)

    @property
    def channels(self) -> list[str]:
        return list(self._adapters.keys())

    async def send(self, channel: str, recipient_id: str, text: str, **kwargs: Any) -> SendResult:
        adapter = self._adapters.get(channel)
        if not adapter:
            return SendResult(success=False, error=f"Unknown channel: {channel}")
        return await adapter.send(recipient_id, text, **kwargs)

    async def fetch_all_new(self, since: datetime | None = None) -> list[UnifiedMessage]:
        """Fetch new messages from all registered channels."""
        all_messages: list[UnifiedMessage] = []
        for adapter in self._adapters.values():
            messages = await adapter.fetch_new(since=since)
            all_messages.extend(messages)
        all_messages.sort(key=lambda m: m.timestamp, reverse=True)
        return all_messages

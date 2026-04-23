"""
Phase 6C: Trial/Demo Access Manager

Provides time-limited access control for investor demos.
Generates trial tokens with configurable expiration and revokes
access to automation features after the trial period.

Usage:
  manager = TrialManager(storage_path="data/trials.json")
  token = manager.create_trial(email="investor@vc.com", days=30)
  is_valid = manager.validate(token)
  manager.revoke(token)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass
class TrialRecord:
    token: str
    email: str
    created_at: str
    expires_at: str
    revoked: bool = False
    revoked_at: str | None = None
    metadata: dict[str, Any] | None = None


class TrialManager:
    """Manages time-limited trial access tokens."""

    def __init__(self, storage_path: str = "data/trials.json"):
        self.storage_path = Path(storage_path)
        self._trials: dict[str, TrialRecord] = {}
        self._load()

    def _load(self) -> None:
        if self.storage_path.exists():
            with open(self.storage_path) as f:
                data = json.load(f)
            for entry in data.get("trials", []):
                record = TrialRecord(**entry)
                self._trials[record.token] = record

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "trials": [asdict(r) for r in self._trials.values()],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.storage_path, "w") as f:
            json.dump(data, f, indent=2)

    def create_trial(
        self,
        email: str,
        days: int = 30,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new trial token with expiration."""
        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)

        record = TrialRecord(
            token=token,
            email=email,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=days)).isoformat(),
            metadata=metadata,
        )
        self._trials[token] = record
        self._save()
        return token

    def validate(self, token: str) -> bool:
        """Check if a trial token is valid and not expired."""
        record = self._trials.get(token)
        if not record:
            return False
        if record.revoked:
            return False
        expires = datetime.fromisoformat(record.expires_at)
        return datetime.now(timezone.utc) < expires

    def revoke(self, token: str) -> bool:
        """Revoke a trial token."""
        record = self._trials.get(token)
        if not record:
            return False
        record.revoked = True
        record.revoked_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

    def get_active_trials(self) -> list[TrialRecord]:
        """List all active (non-expired, non-revoked) trials."""
        now = datetime.now(timezone.utc)
        return [
            r for r in self._trials.values()
            if not r.revoked
            and datetime.fromisoformat(r.expires_at) > now
        ]

    def cleanup_expired(self) -> int:
        """Mark expired trials as revoked."""
        now = datetime.now(timezone.utc)
        count = 0
        for record in self._trials.values():
            if not record.revoked and datetime.fromisoformat(record.expires_at) <= now:
                record.revoked = True
                record.revoked_at = now.isoformat()
                count += 1
        if count > 0:
            self._save()
        return count


def main() -> None:
    """CLI for managing trials."""
    manager = TrialManager()
    args = sys.argv[1:]

    if not args or args[0] == "--list":
        active = manager.get_active_trials()
        print(f"Active trials: {len(active)}")
        for t in active:
            print(f"  {t.email} — expires {t.expires_at[:10]} — token: {t.token[:16]}...")

    elif args[0] == "--create" and len(args) >= 2:
        email = args[1]
        days = int(args[2]) if len(args) > 2 else 30
        token = manager.create_trial(email=email, days=days)
        print(f"Created trial for {email} ({days} days)")
        print(f"Token: {token}")

    elif args[0] == "--validate" and len(args) >= 2:
        valid = manager.validate(args[1])
        print(f"Token valid: {valid}")

    elif args[0] == "--revoke" and len(args) >= 2:
        revoked = manager.revoke(args[1])
        print(f"Revoked: {revoked}")

    elif args[0] == "--cleanup":
        count = manager.cleanup_expired()
        print(f"Cleaned up {count} expired trials")

    else:
        print("Usage:")
        print("  --list                    List active trials")
        print("  --create EMAIL [DAYS]     Create trial (default 30 days)")
        print("  --validate TOKEN          Check if token is valid")
        print("  --revoke TOKEN            Revoke a trial")
        print("  --cleanup                 Mark expired trials as revoked")


if __name__ == "__main__":
    main()

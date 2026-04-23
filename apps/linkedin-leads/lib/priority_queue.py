"""
Phase 6A: Reusable Priority Queue with Weighted Heuristic Scoring

Extracted common pattern from priority-forge. Provides a generic min-heap
priority queue that can be used for any domain (task management, lead
prioritization, job scheduling, etc.).

Lower score = higher priority (extracted first).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar


@dataclass
class HeuristicConfig:
    """Configurable heuristic weights for priority scoring."""
    weights: dict[str, float] = field(default_factory=dict)
    base_scores: dict[str, float] = field(default_factory=dict)

    def score(self, base_tier: str, factors: dict[str, float]) -> float:
        """Calculate priority score from base tier and weighted factors."""
        base = self.base_scores.get(base_tier, 0)
        adjustment = sum(
            self.weights.get(name, 0) * value
            for name, value in factors.items()
        )
        return base - adjustment


@dataclass(order=True)
class PriorityItem:
    """Generic priority queue item."""
    score: float
    id: str = field(compare=False)
    data: dict[str, Any] = field(compare=False, default_factory=dict)


class PriorityQueue:
    """Min-heap priority queue with O(log n) operations."""

    def __init__(self) -> None:
        self._heap: list[PriorityItem] = []
        self._index: dict[str, PriorityItem] = {}
        self._removed: set[str] = set()

    def push(self, item: PriorityItem) -> None:
        heapq.heappush(self._heap, item)
        self._index[item.id] = item

    def pop(self) -> PriorityItem | None:
        while self._heap:
            item = heapq.heappop(self._heap)
            if item.id not in self._removed:
                self._index.pop(item.id, None)
                return item
            self._removed.discard(item.id)
        return None

    def peek(self) -> PriorityItem | None:
        while self._heap:
            if self._heap[0].id not in self._removed:
                return self._heap[0]
            item = heapq.heappop(self._heap)
            self._removed.discard(item.id)
        return None

    def update(self, item_id: str, new_item: PriorityItem) -> bool:
        """Update an item by marking old as removed and inserting new."""
        if item_id not in self._index:
            return False
        self._removed.add(item_id)
        self._index.pop(item_id, None)
        self.push(new_item)
        return True

    def remove(self, item_id: str) -> bool:
        if item_id not in self._index:
            return False
        self._removed.add(item_id)
        self._index.pop(item_id, None)
        return True

    def get(self, item_id: str) -> PriorityItem | None:
        item = self._index.get(item_id)
        if item and item.id not in self._removed:
            return item
        return None

    def has(self, item_id: str) -> bool:
        return item_id in self._index and item_id not in self._removed

    @property
    def size(self) -> int:
        return len(self._index) - len(self._removed)

    def to_sorted_list(self) -> list[PriorityItem]:
        return sorted(
            (item for item in self._index.values() if item.id not in self._removed),
        )

    def rebuild(self, items: list[PriorityItem]) -> None:
        """Rebuild the entire queue from a list of items."""
        self._heap = list(items)
        heapq.heapify(self._heap)
        self._index = {item.id: item for item in items}
        self._removed.clear()

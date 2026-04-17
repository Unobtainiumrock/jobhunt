"""
Phase 6A: Reusable Two-Stage Combo Model Classifier

Extracted common pattern from gravity-pulse (LLM profiler) and linkedin-leads
(lead classifier). Provides a generic framework for any classification task
that benefits from a reasoning model + fast model pipeline.

Usage:
    classifier = ComboClassifier(
        stage1_system="Classify this...",
        stage1_schema={"classification": str, "confidence": float, ...},
        stage2_system="Extract metadata...",
        stage2_schema={"field1": str, ...},
        stage2_filter=lambda result: result["classification"] == "target",
    )
    results = await classifier.classify_batch(items)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from openai import AsyncOpenAI

T = TypeVar("T")


@dataclass
class ClassificationResult:
    stage1: dict[str, Any]
    stage2: dict[str, Any] | None
    item_id: str
    classified_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model_versions: dict[str, str] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ComboClassifier:
    """Generic two-stage classification pipeline."""

    stage1_system: str
    stage1_model: str = "o3"
    stage2_system: str = ""
    stage2_model: str = "gpt-5"
    stage2_filter: Callable[[dict[str, Any]], bool] | None = None
    max_concurrent: int = 8

    async def _run_stage1(
        self,
        client: AsyncOpenAI,
        user_prompt: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        async with semaphore:
            try:
                resp = await client.chat.completions.create(
                    model=self.stage1_model,
                    messages=[
                        {"role": "system", "content": self.stage1_system},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=1,
                )
                return json.loads(resp.choices[0].message.content)
            except Exception as e:
                return {"error": str(e)}

    async def _run_stage2(
        self,
        client: AsyncOpenAI,
        user_prompt: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        if not self.stage2_system:
            return None
        async with semaphore:
            try:
                resp = await client.chat.completions.create(
                    model=self.stage2_model,
                    messages=[
                        {"role": "system", "content": self.stage2_system},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                )
                return json.loads(resp.choices[0].message.content)
            except Exception as e:
                return {"error": str(e)}

    async def classify_item(
        self,
        client: AsyncOpenAI,
        item_id: str,
        stage1_prompt: str,
        stage2_prompt: str | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> ClassificationResult:
        """Classify a single item through both stages."""
        sem = semaphore or asyncio.Semaphore(1)

        stage1_result = await self._run_stage1(client, stage1_prompt, sem)

        stage2_result = None
        if (
            self.stage2_system
            and stage2_prompt
            and (self.stage2_filter is None or self.stage2_filter(stage1_result))
        ):
            stage2_result = await self._run_stage2(client, stage2_prompt, sem)

        return ClassificationResult(
            stage1=stage1_result,
            stage2=stage2_result,
            item_id=item_id,
            model_versions={
                "stage1": self.stage1_model,
                "stage2": self.stage2_model if stage2_result else "",
            },
        )

    async def classify_batch(
        self,
        items: list[dict[str, Any]],
        id_key: str = "id",
        stage1_prompt_fn: Callable[[dict[str, Any]], str] = lambda x: json.dumps(x),
        stage2_prompt_fn: Callable[[dict[str, Any]], str] | None = None,
    ) -> list[ClassificationResult]:
        """Classify a batch of items concurrently."""
        client = AsyncOpenAI()
        semaphore = asyncio.Semaphore(self.max_concurrent)

        tasks = []
        for item in items:
            s2_prompt = stage2_prompt_fn(item) if stage2_prompt_fn else None
            tasks.append(
                self.classify_item(
                    client,
                    item_id=item.get(id_key, ""),
                    stage1_prompt=stage1_prompt_fn(item),
                    stage2_prompt=s2_prompt,
                    semaphore=semaphore,
                )
            )

        return await asyncio.gather(*tasks)

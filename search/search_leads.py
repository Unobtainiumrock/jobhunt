#!/usr/bin/env python3
"""
Phase 4B: Semantic Search over LinkedIn Conversations

Hybrid search combining Qdrant vector search with BM25 keyword matching.
Adapted from gravity-pulse/search/search_messages.py.

Usage:
  python -m search.search_leads "senior ML engineer at a startup"
  python -m search.search_leads "Kubernetes experience" --category recruiter
  python -m search.search_leads "calendar link" --top 5
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from typing import Any

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from rank_bm25 import BM25Okapi

from pipeline.config import (
    QDRANT_URL, QDRANT_API_KEY, CONVERSATIONS_COLLECTION, PROFILE_COLLECTION,
    EMBEDDING_MODEL, CLASSIFIED_FILE, INBOX_FILE,
)

# ---------------------------------------------------------------------------
# Temporal Decay
#
# Messages lose relevance as they age. We model this with exponential decay:
#
#   decay(t) = e^(-λt)
#
# where t is the message age in days and λ = ln(2) / half_life. With a 7-day
# half-life, a message from today scores 1.0, a week-old message scores 0.5,
# two weeks scores 0.25, etc. This is the same curve as radioactive decay --
# recent conversations dominate, but older ones never fully vanish.
# ---------------------------------------------------------------------------
TEMPORAL_DECAY_HALF_LIFE_DAYS = 7
TEMPORAL_DECAY_LAMBDA = math.log(2) / TEMPORAL_DECAY_HALF_LIFE_DAYS

_model: TextEmbedding | None = None
_client: QdrantClient | None = None
_bm25: BM25Okapi | None = None
_bm25_docs: list[dict[str, Any]] | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=EMBEDDING_MODEL)
    return _model


def _get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return _client


def _get_bm25() -> tuple[BM25Okapi, list[dict[str, Any]]]:
    global _bm25, _bm25_docs
    if _bm25 is not None and _bm25_docs is not None:
        return _bm25, _bm25_docs

    source = CLASSIFIED_FILE if CLASSIFIED_FILE.exists() else INBOX_FILE
    with open(source) as f:
        data = json.load(f)

    docs: list[dict[str, Any]] = []
    corpus: list[list[str]] = []
    for convo in data.get("conversations", []):
        for msg in convo.get("messages", []):
            text = msg.get("text", "")
            if not text or len(text) < 15:
                continue
            docs.append({
                "sender": msg.get("sender", "Unknown"),
                "conversation_urn": convo.get("conversationUrn", ""),
                "timestamp": msg.get("timestamp", ""),
                "text": text,
                "category": convo.get("classification", {}).get("category", ""),
                "other_participant": next(
                    (p.get("name", "") for p in convo.get("participants", [])
                     if not p.get("name", "").startswith("Nicholas")),
                    "Unknown",
                ),
            })
            corpus.append(text.lower().split())

    _bm25 = BM25Okapi(corpus)
    _bm25_docs = docs
    return _bm25, _bm25_docs


def vector_search(
    query: str,
    top_k: int = 20,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search via Qdrant."""
    model = _get_model()
    client = _get_client()

    embeddings = list(model.embed([query]))
    if not embeddings:
        return []

    query_filter = None
    if category:
        query_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        )

    response = client.query_points(
        collection_name=CONVERSATIONS_COLLECTION,
        query=embeddings[0].tolist(),
        query_filter=query_filter,
        limit=top_k,
    )

    return [
        {**hit.payload, "score": hit.score, "search_type": "vector"}
        for hit in response.points
    ]


def bm25_search(
    query: str,
    top_k: int = 20,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Keyword search via BM25."""
    bm25, docs = _get_bm25()
    tokens = query.lower().split()
    scores = bm25.get_scores(tokens)

    ranked = sorted(
        zip(range(len(docs)), scores),
        key=lambda x: x[1],
        reverse=True,
    )

    results: list[dict[str, Any]] = []
    for idx, score in ranked[:top_k * 2]:
        if score <= 0:
            break
        doc = docs[idx]
        if category and doc.get("category") != category:
            continue
        results.append({**doc, "score": float(score), "search_type": "bm25"})
        if len(results) >= top_k:
            break

    return results


def _temporal_decay(timestamp: str) -> float:
    """Apply temporal decay based on message age."""
    if not timestamp:
        return 0.5
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        days_old = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        return math.exp(-TEMPORAL_DECAY_LAMBDA * max(days_old, 0))
    except (ValueError, TypeError):
        return 0.5


def hybrid_search(
    query: str,
    top_k: int = 20,
    category: str | None = None,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """Hybrid search combining vector + BM25 via Reciprocal Rank Fusion.

    ## Why two retrieval systems?

    Vector search (Qdrant) and keyword search (BM25) have complementary
    failure modes:

    - **Vector search** captures semantic meaning ("ML engineer" matches
      "machine learning developer") but can miss exact terms the user cares
      about ("Pinterest", a proper noun with no semantic neighbors).
    - **BM25** excels at exact keyword matching but has zero understanding
      of synonyms or paraphrases.

    Running both and merging gives us the best of each world.

    ## Reciprocal Rank Fusion (RRF)

    The problem: vector scores (cosine similarity, 0-1) and BM25 scores
    (unbounded TF-IDF) live on incompatible scales, so we can't just add
    them. RRF sidesteps this entirely by ignoring raw scores and using only
    **rank positions**.

    For each retrieval system, a document at rank r contributes:

        score_contribution = 1 / (k + r)

    where k is a smoothing constant (default 60, from the original paper:
    Cormack, Clarke & Büttcher, 2009). A document's final RRF score is the
    sum of its contributions across all systems:

        RRF(d) = Σ  1 / (k + rank_i(d))
                 i

    **Intuition**: being ranked #1 in one system gives 1/(60+1) ≈ 0.0164.
    Being ranked #1 in *both* systems gives ≈ 0.0328. The k constant
    controls how much rank differences matter:
    - Small k (e.g. 1): top ranks dominate aggressively, #1 >> #5.
    - Large k (e.g. 60): ranking is "flatter", a #1 vs #5 difference is
      small. This is more robust when retrieval systems disagree.

    The k=60 default is well-studied and works across diverse IR tasks.

    ## Temporal Decay Modifier

    After RRF scoring, we multiply by an exponential decay factor:

        final_score(d) = RRF(d) × e^(-λ × age_days)

    This biases toward recent conversations without excluding older ones.
    A 7-day-old message retains 50% of its score; a 14-day-old keeps 25%.

    ## Worked Example

    Query: "ML engineer at a startup"

    Vector results:         BM25 results:
      #0 Message A            #0 Message C
      #1 Message B            #1 Message A
      #2 Message C            #2 Message D

    RRF scores (k=60):
      A: 1/61 + 1/62       = 0.0164 + 0.0161 = 0.0325  (high in both)
      C: 1/63 + 1/61       = 0.0159 + 0.0164 = 0.0323  (high in both)
      B: 1/62              = 0.0161                       (vector only)
      D: 1/63              = 0.0159                       (BM25 only)

    Message A wins because it ranked well in *both* systems, even though
    it was #1 in neither.
    """
    vec_results = vector_search(query, top_k=top_k * 2, category=category)
    bm25_results = bm25_search(query, top_k=top_k * 2, category=category)

    rrf_scores: dict[str, float] = {}
    result_map: dict[str, dict[str, Any]] = {}

    # Accumulate RRF contributions from each retrieval system.
    # rank is 0-indexed, so rank 0 → 1/(k+1), rank 1 → 1/(k+2), etc.
    for rank, r in enumerate(vec_results):
        key = f"{r.get('conversation_urn', '')}:{r.get('timestamp', '')}:{r.get('sender', '')}"
        rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (rrf_k + rank + 1)
        result_map[key] = r

    for rank, r in enumerate(bm25_results):
        key = f"{r.get('conversation_urn', '')}:{r.get('timestamp', '')}:{r.get('sender', '')}"
        rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (rrf_k + rank + 1)
        if key not in result_map:
            result_map[key] = r

    # Multiply by temporal decay so recent messages surface higher
    for key in rrf_scores:
        ts = result_map[key].get("timestamp", "")
        rrf_scores[key] *= _temporal_decay(ts)

    sorted_keys = sorted(rrf_scores, key=lambda k: rrf_scores[k], reverse=True)

    results: list[dict[str, Any]] = []
    for key in sorted_keys[:top_k]:
        entry = result_map[key].copy()
        entry["hybrid_score"] = rrf_scores[key]
        entry["search_type"] = "hybrid"
        results.append(entry)

    return results


def search_profile(
    query: str,
    top_k: int = 5,
    chunk_type: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search over the user_profile collection.

    Returns the most relevant profile chunks for a given query
    (e.g., a recruiter message about Kubernetes returns K8s skills/projects).
    """
    model = _get_model()
    client = _get_client()

    embeddings = list(model.embed([query]))
    if not embeddings:
        return []

    query_filter = None
    if chunk_type:
        query_filter = Filter(
            must=[FieldCondition(key="chunk_type", match=MatchValue(value=chunk_type))]
        )

    response = client.query_points(
        collection_name=PROFILE_COLLECTION,
        query=embeddings[0].tolist(),
        query_filter=query_filter,
        limit=top_k,
    )

    return [
        {**hit.payload, "score": hit.score}
        for hit in response.points
    ]


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m search.search_leads <query> [--category recruiter] [--top N] [--profile]")
        sys.exit(1)

    query = sys.argv[1]
    category = None
    top_k = 10
    profile_mode = "--profile" in sys.argv

    if "--category" in sys.argv:
        idx = sys.argv.index("--category")
        if idx + 1 < len(sys.argv):
            category = sys.argv[idx + 1]
    if "--top" in sys.argv:
        idx = sys.argv.index("--top")
        if idx + 1 < len(sys.argv):
            top_k = int(sys.argv[idx + 1])

    print(f"Searching{'  [profile]' if profile_mode else ''}: \"{query}\"")
    if category:
        print(f"Filtering by category: {category}")
    print()

    if profile_mode:
        results = search_profile(query, top_k=top_k)
        for i, r in enumerate(results, 1):
            print(f"{i:>2}. [{r.get('chunk_type', '?')}] "
                  f"score={r.get('score', 0):.4f}")
            print(f"    {r.get('text', '')[:140]}")
            print()
    else:
        results = hybrid_search(query, top_k=top_k, category=category)
        for i, r in enumerate(results, 1):
            print(f"{i:>2}. [{r.get('other_participant', r.get('sender', 'Unknown'))}] "
                  f"({r.get('date', 'Unknown date')}) "
                  f"score={r.get('hybrid_score', r.get('score', 0)):.4f}")
            print(f"    {r.get('text', '')[:120]}")
            print()


if __name__ == "__main__":
    main()

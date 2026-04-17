#!/usr/bin/env python3
"""
Phase 4B: Vector Embeddings for Conversations

Embeds all LinkedIn conversation messages into Qdrant for semantic search.
Adapted from gravity-pulse/pipeline/embed_messages.py.

Modes:
  - Batch: Embed all messages from inbox_classified.json
  - Worker: Read JSON lines from stdin (for real-time embedding via listener)

Usage:
  python -m pipeline.embed_conversations              # batch mode
  python -m pipeline.embed_conversations --worker      # stdin worker mode
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from typing import Any

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

from pipeline.config import (
    CLASSIFIED_FILE, INBOX_FILE, QDRANT_URL, QDRANT_API_KEY,
    CONVERSATIONS_COLLECTION, EMBEDDING_MODEL, VECTOR_DIM, EMBED_BATCH_SIZE,
    USER_NAME,
)


def make_point_id(conversation_urn: str, timestamp: str, sender: str) -> str:
    """Deterministic UUID from message identity."""
    key = f"{conversation_urn}:{timestamp}:{sender}"
    return str(uuid.UUID(hashlib.md5(key.encode()).hexdigest()))


def is_substantive(text: str) -> bool:
    """Filter out trivial messages."""
    if not text or len(text) < 15:
        return False
    lower = text.lower()
    if lower in ("thanks", "thank you", "ok", "okay", "sure", "sounds good", "got it"):
        return False
    return True


def extract_messages(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract embeddable messages from conversations."""
    messages: list[dict[str, Any]] = []
    for convo in conversations:
        urn = convo.get("conversationUrn", "")
        clf = convo.get("classification", {})
        category = clf.get("category", "unclassified")

        other = next(
            (p for p in convo.get("participants", []) if p.get("name") != USER_NAME),
            {"name": "Unknown", "headline": ""},
        )

        for msg in convo.get("messages", []):
            text = msg.get("text", "")
            if not is_substantive(text):
                continue

            sender = msg.get("sender", "Unknown")
            ts = msg.get("timestamp", "")
            date = ts[:10] if ts else ""

            messages.append({
                "sender": sender,
                "conversation_urn": urn,
                "timestamp": ts,
                "date": date,
                "text": text,
                "subject": msg.get("subject", ""),
                "category": category,
                "other_participant": other.get("name", "Unknown"),
                "other_headline": other.get("headline", ""),
            })

    return messages


def format_embed_text(msg: dict[str, Any]) -> str:
    """Format a message for embedding."""
    subject = f" (re: {msg['subject']})" if msg.get("subject") else ""
    return (
        f"{msg['sender']} in conversation with {msg['other_participant']}"
        f"{subject} ({msg['date']}): {msg['text'][:500]}"
    )


def batch_embed() -> None:
    """Embed all messages from the classified inbox."""
    source = CLASSIFIED_FILE if CLASSIFIED_FILE.exists() else INBOX_FILE
    if not source.exists():
        print(f"Error: {source} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading conversations from {source}...")
    with open(source) as f:
        data = json.load(f)

    messages = extract_messages(data.get("conversations", []))
    print(f"  {len(messages)} substantive messages to embed")

    if not messages:
        print("No messages to embed.")
        return

    print(f"\nLoading embedding model ({EMBEDDING_MODEL})...")
    t0 = time.time()
    model = TextEmbedding(model_name=EMBEDDING_MODEL)
    print(f"  Model loaded in {time.time() - t0:.1f}s")

    texts = [format_embed_text(m) for m in messages]

    print(f"\nEmbedding {len(texts)} messages...")
    t0 = time.time()
    embeddings = list(model.embed(texts, batch_size=EMBED_BATCH_SIZE))
    elapsed = time.time() - t0
    print(f"  Embedded in {elapsed:.1f}s ({len(texts) / max(elapsed, 0.1):.0f} msgs/sec)")

    print(f"\nConnecting to Qdrant at {QDRANT_URL}...")
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

    collections = [c.name for c in client.get_collections().collections]
    if CONVERSATIONS_COLLECTION not in collections:
        print(f"  Creating collection '{CONVERSATIONS_COLLECTION}' ({VECTOR_DIM} dims, cosine)...")
        client.create_collection(
            collection_name=CONVERSATIONS_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
    else:
        print(f"  Collection '{CONVERSATIONS_COLLECTION}' already exists")

    print(f"\nUpserting {len(messages)} points in batches of {EMBED_BATCH_SIZE}...")
    t0 = time.time()
    for i in range(0, len(messages), EMBED_BATCH_SIZE):
        batch_msgs = messages[i:i + EMBED_BATCH_SIZE]
        batch_vecs = embeddings[i:i + EMBED_BATCH_SIZE]

        points = []
        for m, vec in zip(batch_msgs, batch_vecs):
            point_id = make_point_id(m["conversation_urn"], m["timestamp"], m["sender"])
            points.append(PointStruct(
                id=point_id,
                vector=vec.tolist(),
                payload={
                    "sender": m["sender"],
                    "conversation_urn": m["conversation_urn"],
                    "timestamp": m["timestamp"],
                    "date": m["date"],
                    "text": m["text"],
                    "subject": m["subject"],
                    "category": m["category"],
                    "other_participant": m["other_participant"],
                    "other_headline": m["other_headline"],
                },
            ))

        client.upsert(collection_name=CONVERSATIONS_COLLECTION, points=points)
        print(f"  Upserted {min(i + EMBED_BATCH_SIZE, len(messages))}/{len(messages)}")

    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.1f}s")

    info = client.get_collection(CONVERSATIONS_COLLECTION)
    print(f"\nCollection '{CONVERSATIONS_COLLECTION}': {info.points_count} points")


def worker_mode() -> None:
    """Real-time embedding worker. Reads JSON lines from stdin."""
    sys.stderr.write(f"[embed-worker] Loading model ({EMBEDDING_MODEL})...\n")
    sys.stderr.flush()
    model = TextEmbedding(model_name=EMBEDDING_MODEL)

    sys.stderr.write(f"[embed-worker] Connecting to Qdrant ({QDRANT_URL})...\n")
    sys.stderr.flush()
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

    collections = [c.name for c in client.get_collections().collections]
    if CONVERSATIONS_COLLECTION not in collections:
        client.create_collection(
            collection_name=CONVERSATIONS_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )

    sys.stdout.write("READY\n")
    sys.stdout.flush()

    count = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        text = msg.get("text", "")
        if not is_substantive(text):
            continue

        sender = msg.get("sender", "Unknown")
        channel = msg.get("channel", "")
        timestamp = msg.get("timestamp", "")
        date = timestamp[:10] if timestamp else ""

        embed_text = f"{sender} in conversation ({date}): {text[:500]}"

        try:
            embeddings = list(model.embed([embed_text]))
            if not embeddings:
                continue

            point_id = make_point_id(channel, timestamp, sender)
            point = PointStruct(
                id=point_id,
                vector=embeddings[0].tolist(),
                payload={
                    "sender": sender,
                    "conversation_urn": channel,
                    "timestamp": timestamp,
                    "date": date,
                    "text": text,
                },
            )
            client.upsert(collection_name=CONVERSATIONS_COLLECTION, points=[point])
            count += 1
            sys.stderr.write(f"[embed-worker] #{count}: {sender}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[embed-worker] ERROR: {e}\n")
            sys.stderr.flush()

    sys.stderr.write(f"[embed-worker] Done. Embedded {count} messages.\n")


def main() -> None:
    if "--worker" in sys.argv:
        worker_mode()
    else:
        batch_embed()


if __name__ == "__main__":
    main()

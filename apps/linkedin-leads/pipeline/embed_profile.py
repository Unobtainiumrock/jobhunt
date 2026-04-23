#!/usr/bin/env python3
"""
Phase 4B (cont.): Embed user profile into Qdrant for semantic retrieval.

Chunks user_profile.yaml into semantically meaningful units (skills,
experience, projects, summary, preferences) and embeds each into the
'user_profile' collection. Reply generation can then query this collection
to surface the most relevant profile context for a given recruiter message.

Usage:
  python -m pipeline.embed_profile              # full re-embed
  python -m pipeline.embed_profile --stats      # show collection stats
"""

from __future__ import annotations

import hashlib
import sys
import time
import uuid
from typing import Any

import yaml
from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

from pipeline.config import (
    PROFILE_FILE, QDRANT_URL, QDRANT_API_KEY,
    PROFILE_COLLECTION, EMBEDDING_MODEL, VECTOR_DIM,
)


def _point_id(chunk_type: str, key: str) -> str:
    raw = f"{chunk_type}:{key}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def _chunk_identity(profile: dict[str, Any]) -> list[dict[str, Any]]:
    identity = profile.get("identity", {})
    summary = profile.get("summary", "").strip()
    achievements = profile.get("achievements", [])

    text_parts = [
        f"{identity.get('name', '')} is based in {identity.get('location', '')}.",
        f"Remote preference: {identity.get('remote_preference', 'flexible')}.",
    ]
    if summary:
        text_parts.append(summary)
    for ach in achievements:
        text_parts.append(f"Achievement: {ach.get('title', '')} -- {ach.get('description', '')}")

    return [{
        "chunk_type": "identity_summary",
        "key": "identity",
        "text": " ".join(text_parts),
        "metadata": {"section": "identity"},
    }]


def _chunk_expertise(profile: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for area in profile.get("expertise_areas", []):
        text = f"Expertise: {area['area']}. {area.get('description', '').strip()}"
        chunks.append({
            "chunk_type": "expertise",
            "key": area["area"],
            "text": text,
            "metadata": {"section": "expertise", "area": area["area"]},
        })
    return chunks


def _chunk_skills(profile: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    skills = profile.get("skills", {})

    for skill in skills.get("technical", []):
        evidence = skill.get("evidence", [])
        if isinstance(evidence, str):
            evidence = [evidence]
        text = (
            f"Skill: {skill['name']} (proficiency: {skill.get('proficiency', 'unknown')}, "
            f"{skill.get('years', '?')} years). "
            f"Evidence: {'; '.join(evidence)}"
        )
        chunks.append({
            "chunk_type": "skill",
            "key": skill["name"],
            "text": text,
            "metadata": {
                "section": "skills",
                "skill_name": skill["name"],
                "proficiency": skill.get("proficiency", ""),
                "years": skill.get("years", 0),
            },
        })

    list_sections = {
        "languages": "Programming language",
        "ml_frameworks": "ML framework",
        "frameworks": "Web/app framework",
        "infrastructure": "Infrastructure/platform",
        "data_tools": "Data tool",
    }
    for section_key, label in list_sections.items():
        items = skills.get(section_key, [])
        if items:
            text = f"{label}s: {', '.join(items)}"
            chunks.append({
                "chunk_type": "skill_list",
                "key": section_key,
                "text": text,
                "metadata": {"section": "skills", "subsection": section_key},
            })

    return chunks


def _chunk_experience(profile: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    experience = profile.get("experience", {})

    for pos in experience.get("positions", []):
        achievements = pos.get("key_achievements", [])
        if isinstance(achievements, str):
            achievements = [achievements]
        techs = pos.get("technologies_used", [])

        text_parts = [
            f"{pos.get('title', '')} at {pos.get('company', '')}",
            f"({pos.get('start_date', '')} - {pos.get('end_date', '')}).",
        ]
        if pos.get("description"):
            text_parts.append(pos["description"])
        if achievements:
            text_parts.append("Key achievements: " + "; ".join(achievements))
        if techs:
            text_parts.append("Technologies: " + ", ".join(techs))

        key = f"{pos.get('company', '')}_{pos.get('title', '')}"
        chunks.append({
            "chunk_type": "experience",
            "key": key,
            "text": " ".join(text_parts),
            "metadata": {
                "section": "experience",
                "company": pos.get("company", ""),
                "title": pos.get("title", ""),
                "start_date": pos.get("start_date", ""),
                "end_date": pos.get("end_date", ""),
            },
        })

    for mil in experience.get("military", []):
        achievements = mil.get("key_achievements", [])
        text = (
            f"Military: {mil.get('title', '')} in {mil.get('branch', '')} "
            f"({mil.get('start_date', '')} - {mil.get('end_date', '')}). "
            f"Key achievements: {'; '.join(achievements)}"
        )
        chunks.append({
            "chunk_type": "military",
            "key": mil.get("branch", "military"),
            "text": text,
            "metadata": {"section": "military"},
        })

    return chunks


def _chunk_projects(profile: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for proj in profile.get("projects", []):
        desc = proj.get("description", "").strip()
        techs = proj.get("technologies", [])
        patterns = proj.get("architectural_patterns", [])
        domain = proj.get("domain", "")

        text_parts = [f"Project: {proj['name']}."]
        if desc:
            text_parts.append(desc)
        if techs:
            text_parts.append(f"Technologies: {', '.join(techs)}.")
        if patterns:
            text_parts.append(f"Patterns: {', '.join(patterns)}.")
        if domain:
            text_parts.append(f"Domain: {domain}.")

        chunks.append({
            "chunk_type": "project",
            "key": proj["name"],
            "text": " ".join(text_parts),
            "metadata": {
                "section": "projects",
                "project_name": proj["name"],
                "domain": domain,
            },
        })
    return chunks


def _chunk_education(profile: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for edu in profile.get("education", []):
        text = (
            f"Education: {edu.get('degree', '')} in {edu.get('field', '')} "
            f"from {edu.get('institution', '')}."
        )
        chunks.append({
            "chunk_type": "education",
            "key": edu.get("institution", "education"),
            "text": text,
            "metadata": {"section": "education"},
        })
    for res in profile.get("research", []):
        text = f"Research interest: {res.get('area', '')}. {res.get('description', '')}"
        chunks.append({
            "chunk_type": "research",
            "key": res.get("area", "research"),
            "text": text,
            "metadata": {"section": "research"},
        })
    return chunks


def _chunk_preferences(profile: dict[str, Any]) -> list[dict[str, Any]]:
    prefs = profile.get("preferences", {})
    if not prefs:
        return []

    parts = []
    roles = prefs.get("target_roles", [])
    if roles:
        parts.append(f"Target roles: {', '.join(roles)}.")
    industries = prefs.get("target_industries", [])
    if industries:
        parts.append(f"Target industries: {', '.join(industries)}.")
    excluded = prefs.get("excluded_industries", [])
    if excluded:
        parts.append(f"Excluded industries: {', '.join(excluded)}.")
    comp = prefs.get("compensation", {})
    if comp:
        parts.append(
            f"Compensation: minimum ${comp.get('minimum', 0):,} "
            f"target ${comp.get('target', 0):,} {comp.get('currency', 'USD')}."
        )
    size = prefs.get("company_size", "")
    if size:
        parts.append(f"Company size preference: {size}.")
    deal_breakers = prefs.get("deal_breakers", [])
    if deal_breakers:
        parts.append(f"Deal breakers: {'; '.join(deal_breakers)}.")
    priorities = prefs.get("priorities", [])
    if priorities:
        parts.append(f"Top priorities: {', '.join(priorities)}.")

    return [{
        "chunk_type": "preferences",
        "key": "preferences",
        "text": " ".join(parts),
        "metadata": {"section": "preferences"},
    }]


def _chunk_communication(profile: dict[str, Any]) -> list[dict[str, Any]]:
    style = profile.get("communication_style", {})
    personal = profile.get("personal", {})
    if not style and not personal:
        return []

    parts = []
    if style.get("tone"):
        parts.append(f"Communication tone: {style['tone']}.")
    policies = style.get("recruiter_policies", [])
    if policies:
        parts.append(f"Recruiter policies: {'; '.join(policies)}.")
    interests = personal.get("interests", [])
    if interests:
        parts.append(f"Personal interests: {', '.join(interests)}.")
    traits = personal.get("traits", [])
    if traits:
        parts.append(f"Traits: {', '.join(traits)}.")

    return [{
        "chunk_type": "communication_personal",
        "key": "communication",
        "text": " ".join(parts),
        "metadata": {"section": "communication_personal"},
    }]


def chunk_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Split the profile into semantically meaningful chunks."""
    chunks: list[dict[str, Any]] = []
    chunks.extend(_chunk_identity(profile))
    chunks.extend(_chunk_expertise(profile))
    chunks.extend(_chunk_skills(profile))
    chunks.extend(_chunk_experience(profile))
    chunks.extend(_chunk_projects(profile))
    chunks.extend(_chunk_education(profile))
    chunks.extend(_chunk_preferences(profile))
    chunks.extend(_chunk_communication(profile))
    return chunks


def embed_profile() -> None:
    """Load profile, chunk, embed, and upsert into Qdrant."""
    if not PROFILE_FILE.exists():
        print(f"Error: {PROFILE_FILE} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading profile from {PROFILE_FILE}...")
    with open(PROFILE_FILE) as f:
        profile = yaml.safe_load(f)

    chunks = chunk_profile(profile)
    print(f"  {len(chunks)} chunks generated")

    if not chunks:
        print("No chunks to embed.")
        return

    print(f"\nLoading embedding model ({EMBEDDING_MODEL})...")
    t0 = time.time()
    model = TextEmbedding(model_name=EMBEDDING_MODEL)
    print(f"  Model loaded in {time.time() - t0:.1f}s")

    texts = [c["text"] for c in chunks]
    print(f"\nEmbedding {len(texts)} profile chunks...")
    t0 = time.time()
    embeddings = list(model.embed(texts))
    elapsed = time.time() - t0
    print(f"  Embedded in {elapsed:.1f}s ({len(texts) / max(elapsed, 0.1):.0f} chunks/sec)")

    print(f"\nConnecting to Qdrant at {QDRANT_URL}...")
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

    collections = [c.name for c in client.get_collections().collections]
    if PROFILE_COLLECTION in collections:
        print(f"  Recreating collection '{PROFILE_COLLECTION}' (fresh embed)...")
        client.delete_collection(PROFILE_COLLECTION)
    print(f"  Creating collection '{PROFILE_COLLECTION}' ({VECTOR_DIM} dims, cosine)...")
    client.create_collection(
        collection_name=PROFILE_COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )

    points: list[PointStruct] = []
    for chunk, vec in zip(chunks, embeddings):
        point_id = _point_id(chunk["chunk_type"], chunk["key"])
        points.append(PointStruct(
            id=point_id,
            vector=vec.tolist(),
            payload={
                "chunk_type": chunk["chunk_type"],
                "key": chunk["key"],
                "text": chunk["text"],
                **chunk.get("metadata", {}),
            },
        ))

    print(f"\nUpserting {len(points)} points...")
    t0 = time.time()
    client.upsert(collection_name=PROFILE_COLLECTION, points=points)
    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.1f}s")

    info = client.get_collection(PROFILE_COLLECTION)
    print(f"\nCollection '{PROFILE_COLLECTION}': {info.points_count} points")

    breakdown: dict[str, int] = {}
    for c in chunks:
        breakdown[c["chunk_type"]] = breakdown.get(c["chunk_type"], 0) + 1
    print("\nChunk breakdown:")
    for ct, count in sorted(breakdown.items()):
        print(f"  {ct}: {count}")


def show_stats() -> None:
    """Show collection statistics."""
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    try:
        info = client.get_collection(PROFILE_COLLECTION)
        print(f"Collection '{PROFILE_COLLECTION}':")
        print(f"  Points: {info.points_count}")
        print(f"  Vectors: {info.vectors_count}")
        print(f"  Status: {info.status}")
    except Exception as e:
        print(f"Collection '{PROFILE_COLLECTION}' not found: {e}", file=sys.stderr)


def main() -> None:
    if "--stats" in sys.argv:
        show_stats()
    else:
        embed_profile()


if __name__ == "__main__":
    main()

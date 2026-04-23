# Canonical Schemas

These JSON Schema files define the target data contract for the unified hunt system.

They are intentionally broader than the current recruiter-only pipeline. Existing pipeline outputs can be mapped into these entities incrementally.

## Entities

- `lead.schema.json`
- `opportunity.schema.json`
- `conversation.schema.json`
- `application.schema.json`
- `interview-loop.schema.json`
- `prep-artifact.schema.json`
- `task.schema.json`
- `signal.schema.json`

## Implementation Notes

- IDs use stable string identifiers so records can be merged across sources
- Timestamps should be stored as ISO-8601 strings in UTC whenever possible
- Fields that depend on extraction confidence include optional confidence metadata
- Source-specific raw payloads should remain outside these schemas and live in source snapshots or raw ingestion files

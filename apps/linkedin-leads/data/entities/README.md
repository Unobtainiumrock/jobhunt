# Entity Storage

Canonical entity records should live here as the unified hunt model becomes operational.

Expected record groups:

- `leads/`
- `opportunities/`
- `conversations/`
- `applications/`
- `interview_loops/`
- `prep_artifacts/`
- `tasks/`
- `signals/`

Manual corrections that should survive sync reruns live in `overrides.json`.

Generated follow-up drafts and reviewable follow-up state can live in `followups.json`.

Optional external company-research jobs live in `research_enrichment_queue.json`.

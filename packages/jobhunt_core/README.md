# jobhunt-core

Shared Python library for the unified job-hunt system. Consumed by both
[`apps/applypilot`](../../apps/applypilot/) (outbound automation) and
[`apps/linkedin-leads`](../../apps/linkedin-leads/) (inbound triage),
both of which now live in this monorepo. Phase 4 of the job-hunt unification plan.

## Scope

- `jobhunt_core.profile` — unified YAML profile loader. Renders the legacy
  BetterApplyPilot profile.json dict shape from `linkedin-leads/profile/user_profile.yaml`.
- `jobhunt_core.entities` — Pydantic models mirroring the JSON schemas at
  `linkedin-leads/schemas/`. Currently exposes `Opportunity`. Others follow.
- `jobhunt_core.store` — Opportunity JSON I/O utilities (reads existing,
  merges linkedin-leads-owned cross-link fields, writes schema-valid JSON).

## Install (laptop-local)

This library is laptop-local during Phase 4; there's no PyPI package and
no dedicated GitHub remote yet. Install as editable:

```bash
pip install -e ~/Desktop/github/jobhunt-core/
```

Both consumer projects assume the above path.

## Deferred

Phase 4b will lift BetterApplyPilot's SQLite schema (`database.py`) into
`jobhunt_core.store`. That migration is tracked separately because the
BetterApplyPilot live database must be backed up and test-migrated before
the move.

## Monorepo

Phase 9 of the overall plan consolidates this repo into
`Unobtainiumrock/jobhunt` alongside `apps/applypilot/` and
`apps/linkedin-leads/`. Until then, this lives laptop-local with a plain
`git init` for history; no remote.

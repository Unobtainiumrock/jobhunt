# jobhunt — unified monorepo

End-to-end job-hunt system consolidated from three previously-separate
repositories:

| Path | Source | Purpose |
|------|--------|---------|
| `apps/applypilot/` | `Unobtainiumrock/BetterApplyPilot` | Outbound automation: discover → enrich → score → tailor → cover → pdf → apply |
| `apps/linkedin-leads/` | `Unobtainiumrock/linkedin-leads` | Inbound triage: LinkedIn scraper, reply drafting, review UI, Docker deployment on Hetzner |
| `packages/jobhunt_core/` | Local `~/Desktop/github/jobhunt-core` | Shared entity models, unified profile loader, Opportunity merge/write, remote sync |

Merged via `git subtree add`; full commit history from each source is
preserved (not squashed).

## Architecture

See `apps/applypilot/infra/MODE_B_DEPLOY.md` for the Mode A (laptop-
authoritative) vs Mode B (Hetzner-authoritative) deployment model.

## Relationship to source repos

The three source repos on GitHub remain intact and authoritative for any
commits that predate this monorepo. Going forward, **new work happens
here**, and the originals are frozen at their last pre-merge commits
with a README pointer.

The live deployment at `178.104.92.205` currently clones from the
original repos (`/opt/BetterApplyPilot`, `/opt/linkedin-leads`, and a
rsync-only copy at `/opt/jobhunt-core`). A future cut-over will switch
the server to clone this monorepo instead; until then, changes made
here must be cherry-picked back to the originals to reach production.

## Development

```bash
# Install both Python packages editable, then BAP depends on jobhunt_core.
pip install -e packages/jobhunt_core
pip install -e apps/applypilot

# Run BAP pipeline (laptop, Mode A)
applypilot run

# Start linkedin-leads docker stack (server)
cd apps/linkedin-leads && docker compose up -d
```

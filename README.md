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

This monorepo is the single source of truth. The three pre-merge GitHub
repos (`Unobtainiumrock/BetterApplyPilot`, `Unobtainiumrock/linkedin-leads`,
and the local `jobhunt-core`) are frozen at their last pre-merge commits
and receive no further work — they exist only so the inherited commit
history resolves.

The live deployment at `<your-vps-ip>` clones this monorepo at
`/opt/jobhunt/src/` (remote: `Unobtainiumrock/jobhunt.git`). The pre-merge
`/opt/{BetterApplyPilot,linkedin-leads,jobhunt-core}` paths no longer
exist on the server. Server cron points at `/opt/jobhunt/src/apps/...`,
and `apps/linkedin-leads/docker-compose.yml` uses monorepo-relative bind
mounts (`../../packages/jobhunt_core`). No cherry-picking required —
push to `origin/main` here, `git pull` there.

## Install

> **⚠ Do NOT `pip install applypilot` from PyPI.** That installs the
> upstream `Pickle-Pixel/ApplyPilot` package and silently misses every
> improvement in this fork (multi-mailbox Gmail, budget caps, watchdog,
> Opus-4.7 default, resumable mid-form progress, etc.). Use the install
> script below — it editable-installs from this clone, guaranteeing you
> get the fork's code.

### Outbound (laptop) — applypilot CLI

Two commands, ~3 minutes:

```bash
git clone https://github.com/Unobtainiumrock/jobhunt.git
cd jobhunt && ./install.sh
```

`install.sh` preflight-checks Python 3.11+, creates a `.venv/`, editable-
installs both packages from this clone, handles the `python-jobspy`
`--no-deps` workaround, runs the interactive `applypilot init` wizard
(~30 prompts: resume, contact, work auth, comp, EEO, API keys), then
runs `applypilot doctor` to verify.

After install: `applypilot run` (stages 1-5) → `applypilot apply` (Tier 3
auto-submit; needs Chrome + Node + Claude Code CLI on the laptop).

### Inbound (your VPS) — linkedin-leads docker stack

Run on any Linux VPS you bring (Hetzner / DigitalOcean / Vultr / Lightsail /
GCP / Linode — provider-agnostic). Hard requirements: Ubuntu 22.04+ or
Debian 12+, Docker + Compose v2, ≥4 GB RAM, ≥2 GB swap (auto-created),
≥30 GB free disk.

```bash
ssh root@your-vps
git clone https://github.com/Unobtainiumrock/jobhunt.git /opt/jobhunt/src
cd /opt/jobhunt/src && ./setup-server.sh
```

`setup-server.sh` preflight-checks resources, initializes
`/opt/jobhunt/data/`, prompts for API keys + LinkedIn display name +
optional Telegram, generates `.env`, runs `docker compose up -d --build`,
and installs the 5 host crons. The noVNC desktop runs in-container — no
host GUI needed; you SSH-tunnel into `http://localhost:6080` to do the
one-time LinkedIn login.

### Manual install (developer / hacking on the code)

If you want to skip the script and install by hand:

```bash
git clone https://github.com/Unobtainiumrock/jobhunt.git
cd jobhunt
python3 -m venv .venv && source .venv/bin/activate
pip install -e packages/jobhunt_core
pip install -e apps/applypilot
pip install --no-deps python-jobspy
pip install pydantic tls-client requests markdownify regex
applypilot init && applypilot doctor
```

For the inbound stack: read `setup-server.sh` and run the steps you want.

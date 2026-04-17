# CLAUDE.md — linkedin-leads

Claude Code agent memory. Read `AGENTS.md` in this same directory for full
project context (architecture, cron, safety rails, debug pointers); this
file mirrors the critical deploy memory so you cannot miss it.

## The one rule you cannot forget

**Production runs on Hetzner, not the laptop.** A `git commit` alone does
nothing in prod. Shipping a code change is a two-step flow:

```bash
git push origin main    # 1. ship source to GitHub
./bin/deploy            # 2. pull + rebuild + restart on Hetzner
```

`./bin/deploy` wraps
`ssh hetzner 'cd /opt/linkedin-leads && git pull --ff-only && docker compose up -d --build'`.
It is idempotent and safe to re-run.

## Do NOT

- `rsync` / `scp` app code to Hetzner (retired path).
- Edit files directly on `/opt/linkedin-leads` over SSH (next `git pull`
  creates a working-tree conflict). Push through GitHub.
- Rotate VNC/Caddy passwords or set up GitHub Actions auto-deploy without the
  user explicitly approving.

## Where things live

- Secrets + flags: `.env` (gitignored, hand-synced laptop ↔ Hetzner via
  `scp`). Comments in the file document each key.
- Runtime state: Docker volumes on Hetzner (`app-data`, `chrome-profile`,
  `qdrant-storage`). They survive `docker compose up -d --build`.
- Task tracking: Priority Forge at `http://127.0.0.1:3456` (project:
  `job-hunt`). See `/home/unobtainium/Desktop/github/priority-forge/CLAUDE.md`
  for its own rules. Use `mcp_priority-forge_*` tools when available, else
  the REST API.

## When something breaks or feels stuck

Production is on Hetzner (`hetzner`, `/opt/linkedin-leads`). Read **`AGENTS.md` →
“When something breaks or feels stuck (operator playbook)”** for the full
symptom → action table (session/VNC, sends gate, stale inbox, logs, pause
sends, deploy drift).

For everything else, read `AGENTS.md`.

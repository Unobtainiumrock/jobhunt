# AGENTS.md — linkedin-leads

Project memory for AI coding agents (Cursor, Claude Code, Codex, etc.) and
for future me. Read this first before making changes.

## TL;DR — How to deploy a code change

Code runs in production on a Hetzner VM at `178.104.92.205` (SSH alias:
`hetzner`), from `/opt/linkedin-leads` which is a git checkout that tracks
`origin/main` on `github.com/Unobtainiumrock/linkedin-leads` (private).

**Changing code is a TWO-step flow. Do BOTH or the change is only on the laptop:**

```bash
git push origin main    # 1. ship source to GitHub
./bin/deploy            # 2. pull + rebuild + restart on Hetzner
```

`./bin/deploy` wraps `ssh hetzner 'cd /opt/linkedin-leads && git pull --ff-only
&& docker compose up -d --build'`. The one-liner is idempotent and safe to
re-run.

**Never** `rsync` from the laptop to the Hetzner box anymore. That path is
retired; the deploy key + git-pull is the only blessed flow. If you catch
yourself writing `scp` or `rsync` for app code, stop and use `./bin/deploy`.

## Architecture in one paragraph

Docker Compose stack on Hetzner: `desktop` (Xfce + Chrome + noVNC for
LinkedIn session), `listener` (Node scraper + pipeline runner), `review`
(Flask approval UI, mobile-responsive), `telegram_bot` (phone control
surface), `qdrant` (vector DB), `healthdog` (Telegram alerter). Caddy
runs on the host (not in Compose) and terminates HTTPS + basic-auth for
`review.178-104-92-205.sslip.io` and `vnc.178-104-92-205.sslip.io`.
State lives in three named volumes: `app-data`, `chrome-profile`,
`qdrant-storage`. These survive rebuilds.

## Files you should NOT commit (gitignored, stay local/Hetzner-only)

- `.env` — secrets + runtime flags. Both the laptop copy and
  `/opt/linkedin-leads/.env` on Hetzner must stay in sync. Hand-sync via
  `scp` when values change, since git-pull won't touch it.
- `profile/` — `user_profile.yaml` is personal; `repo_analysis.json` is
  generated.
- `data/` — runtime scratch. Real data lives in the `app-data` Docker
  volume, not on the host filesystem.

## Cron (runs on the Hetzner host, not in Compose)

```
0  */4 * * *   /opt/linkedin-leads/infra/cron.sh scrape
10 */6 * * *   /opt/linkedin-leads/infra/cron.sh pipeline
*/15 *  * * *  /opt/linkedin-leads/infra/cron.sh health
```

## Safety rails already in the code (don't weaken these silently)

- `LINKEDIN_SEND_ENABLED` in `.env` can pause live sends. Compose defaults to
  `1` (real sends on). Set `0` in `.env` to dry-run only: approve still saves
  JSON but nothing dispatches. When `1`, approving a reply or follow-up (web
  UI or Telegram `/approve`) immediately runs the LinkedIn sender for that
  thread (same `send-approved` pipeline as the bulk button, serialized via
  `data/.send_approved.lock`).
- `SENDER_RATE_LIMIT` caps sends/hour. Translated automatically into
  per-run delay envs by `pipeline/send_approved_exec.py` (used by both the
  review server and the Telegram bot).
- Follow-up scheduler enforces a hard cap of 2 follow-ups; a runtime
  `AssertionError` fires if anything tries to emit a `followup_3`.
- `pipeline/generate_reply.py` abstains on `dead_end` / `awaiting_*`
  intents and when the USER was the last sender.

## Debugging pointers

- Review UI not reachable: `ssh hetzner 'docker compose -f /opt/linkedin-leads/docker-compose.yml logs --tail=50 review'`.
- Telegram bot says "no drafts": check `inbox_classified.json` has the
  enrichment fields (`reply`, `stage`, `intent_tag`). `classify_leads.py`
  previously wiped these; the merge-carry-forward fix in `_SCRAPE_FIELDS`
  must remain intact.
- LinkedIn session expired: you'll get a Telegram alert. Open
  `https://vnc.178-104-92-205.sslip.io/` (two-layer auth; see `.env` for
  creds), re-login to LinkedIn, volume persists.
- OpenAI 429: check project-level spending limit in the OpenAI dashboard
  for the `pre-commit-hooks` project, not just the org balance.

## Out-of-scope without explicit user approval

- Rotating the Caddy basic-auth creds or VNC password.
- Changing `LINKEDIN_SEND_ENABLED` without you asking (pause vs live is your
  operational knob).
- Setting up a GitHub Actions auto-deploy (requires storing an SSH key
  as a repo secret — security tradeoff owner should decide).
- Deleting, rotating, or revoking the Hetzner deploy key (`hetzner-deploy-*`
  on `Unobtainiumrock/linkedin-leads`).

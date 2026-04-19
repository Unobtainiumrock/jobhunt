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
(approval UI + mobile swipe UI), `telegram_bot` (phone control
surface), `qdrant` (vector DB), `healthdog` (Telegram alerter). Caddy
runs on the host (not in Compose) and terminates HTTPS for three
subdomains. State lives in three named volumes: `app-data`,
`chrome-profile`, `qdrant-storage`. These survive rebuilds.

## Public HTTPS & auth

Three Caddy virtual hosts. All auto-provision Let's Encrypt certs on
first hit. `sslip.io` gives us free wildcard DNS without owning a
domain — the pattern `A-B-C-D.sslip.io` maps to `A.B.C.D`. Base host
on prod: `178-104-92-205.sslip.io`.

| Subdomain | Port | Auth                    | Purpose                  |
|-----------|------|-------------------------|--------------------------|
| `review.` | 3457 | HTTP basic auth         | Desktop review UI        |
| `vnc.`    | 6080 | HTTP basic auth         | noVNC for Chrome session |
| `m.`      | 3457 | Telegram `initData` HMAC | Mobile swipe UI (WebApp) |

`m.` **must not** have basic auth — Telegram's in-app WebView cannot
handle auth prompts. Auth is enforced inside the app by
`infra/telegram_auth.py::verify_init_data`, which validates the
HMAC-SHA256 signature, freshness (`auth_date` < 1h), and that
`user.id == HEALTH_TELEGRAM_CHAT_ID`. Changing the chat id revokes
mobile access.

Rotation: edit `/etc/caddy/Caddyfile` on the host, `sudo systemctl
reload caddy`. Basic auth hash: `caddy hash-password --plaintext <pw>`.
Bot / WebApp registration: BotFather → `/mybots` → `Bot Settings` →
`Menu Button` → paste `https://m.<host>/m/`.

## External reachability ping (laptop side)

Healthdog lives on the VPS and can't alert when the VPS itself is
down. A systemd **user** timer on the laptop polls
`https://m.<host>/m/` every 5 min and DMs via the shared Telegram
creds on failure. Rate-limited (one DM per 30 min while down) and
sends a "recovered" DM on the first success after a down streak.

Install (from repo root):

```bash
cp infra/systemd/linkedin-leads-external-ping.service \
   infra/systemd/linkedin-leads-external-ping.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now linkedin-leads-external-ping.timer
```

Inspect:

```bash
systemctl --user list-timers linkedin-leads-external-ping.timer
journalctl --user -u linkedin-leads-external-ping.service -n 20
```

State file: `$XDG_STATE_HOME/linkedin-leads/external_ping.state`
(defaults to `~/.local/state/linkedin-leads/`). Delete to reset
down/up tracking. Caveat: only alerts while the laptop is awake and
online — pair with an always-on monitor (e.g. UptimeRobot free tier,
pointed at the same URL) for 24/7 coverage.

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

## When something breaks or feels stuck (operator playbook)

Use Hetzner, repo path `/opt/linkedin-leads`, `docker compose -f
/opt/linkedin-leads/docker-compose.yml` (or `cd /opt/linkedin-leads && docker
compose`) unless you are debugging locally.

| Symptom | What to do |
|---------|------------|
| **LinkedIn session expired / send failures / auth wall** | Telegram often surfaces this. Open VNC (`https://vnc.178-104-92-205.sslip.io/`, creds in `.env`), re-login in Chrome; session persists in `chrome-profile` volume. Retry approve or bulk send after. |
| **Review UI unreachable** | `ssh hetzner 'cd /opt/linkedin-leads && docker compose logs --tail=80 review'`. Confirm Caddy on host; confirm `review` binds `127.0.0.1:3457` on the VM. |
| **Telegram `/status` or `/list` looks empty / “no drafts”** | Usually `inbox_classified.json` lost enrichment (`reply`, `stage`, `intent_tag`) on a bad classify merge — see “Telegram bot says no drafts” above. Confirm `telegram_bot` and `review` share `app-data` volume and restarted after deploy. |
| **Approves do not hit LinkedIn** | `docker compose exec -T review printenv LINKEDIN_SEND_ENABLED` — expect `1`. If `0`, set in `/opt/linkedin-leads/.env`, then `docker compose up -d review telegram_bot`. Another send may be holding `data/.send_approved.lock` (wait for in-flight send, or check stuck `send-approved` / Chrome). |
| **Pause all outbound (keep reviewing)** | Set `LINKEDIN_SEND_ENABLED=0` in `.env` (laptop + Hetzner), `scp` if needed, then `docker compose up -d review telegram_bot` (or `./bin/deploy`). |
| **Data feels stale (you replied on LinkedIn but drafts disagree)** | Force refresh: `docker compose exec -T listener npm run inbox` (or wait for cron scrape). Then pipeline: `docker compose exec -T listener npm run pipeline` (or cron). `generate_reply --purge-stale` runs after pipeline in `infra/cron.sh`. |
| **Pipeline / scrape errors** | `docker compose logs --tail=100 listener` and host logs under `/var/log/linkedin-leads/` from `infra/cron.sh`. OpenAI 429 → project spend limit (see above). Missing profile → `profile/user_profile.yaml` on server and in image context. |
| **Code fix shipped but prod unchanged** | You only pushed: run `./bin/deploy`. You only edited on server: stop — push via GitHub, then `./bin/deploy`. |

## Out-of-scope without explicit user approval

- Rotating the Caddy basic-auth creds or VNC password.
- Changing `LINKEDIN_SEND_ENABLED` without you asking (pause vs live is your
  operational knob).
- Setting up a GitHub Actions auto-deploy (requires storing an SSH key
  as a repo secret — security tradeoff owner should decide).
- Deleting, rotating, or revoking the Hetzner deploy key (`hetzner-deploy-*`
  on `Unobtainiumrock/linkedin-leads`).

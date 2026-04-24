# Mode B — server-authoritative pipeline deploy

Reference for flipping from Mode A (laptop writes, auto-pushes to a
server mirror) to Mode B (the server owns the pipeline; laptop only
triggers apply). Written around the current deploy layout — adapt
`<your-ssh-alias>` and paths to match your own setup.

## Server layout (post-unification)

```
/opt/jobhunt/
├── src/                                    ← monorepo clone
│   ├── apps/applypilot/                    ← BAP source (this package)
│   ├── apps/linkedin-leads/                ← docker-compose stack lives here
│   └── packages/jobhunt_core/              ← shared library, bind-mounted into pipeline
└── data/
    ├── jobhunt.db                          ← authoritative SQLite in Mode B
    ├── entities/                           ← JSON projection (Opportunity et al.)
    ├── tailored_resumes/                   ← PDFs produced by tailor+pdf stages
    ├── cover_letters/
    └── backups/                            ← daily gzipped .sql dumps (cron.daily)
```

## Checklist before flipping

- [ ] Server has `/opt/jobhunt/data/{jobhunt.db,entities,backups}` seeded
  via the Phase-5 rsync push (or manually).
- [ ] Server has a swap file; `free -h` shows non-zero Swap. On a 4 GB
  VM the chromium-using stages (enrich, smartextract, pdf) are tight
  without one.
- [ ] `apps/linkedin-leads/.env` on the server contains the BAP keys:
  `GEMINI_API_KEY`, `LLM_MODEL`, optionally `ANTHROPIC_API_KEY`.
- [ ] Pipeline image built:
  `docker compose --profile ondemand build pipeline`.
- [ ] Doctor passes inside the container:
  `docker compose --profile ondemand run --rm pipeline applypilot doctor`.

## Flip laptop to Mode B

Edit `~/.applypilot/.env`:

```
APPLYPILOT_BACKEND=remote
JOBHUNT_REMOTE_SSH_HOST=<your-ssh-alias-for-the-server>
```

(The legacy value `APPLYPILOT_BACKEND=hetzner` is still accepted as an
alias for `remote` to keep older deploys working.)

The laptop-side remote-sync auto-hook now silently skips
(`jobhunt_core.sync_remote.push_checkpoint` exits early with a "server
is authoritative" reason). Existing `applypilot run` invocations on the
laptop will still execute locally against the laptop's own SQLite — if
you want them to go to the server instead, SSH and run via compose:

```
ssh <your-ssh-alias> 'cd /opt/jobhunt/src/apps/linkedin-leads && \
    docker compose --profile ondemand run --rm pipeline \
    applypilot run --stages <stage>'
```

## Host cron on the server

Template (not auto-installed — copy into `crontab -e` as root when
ready):

```cron
# BAP pipeline stages, server-authoritative mode. Env loaded from
# apps/linkedin-leads/.env via docker compose.

# Discover every 4h. Longest-running stage.
 0 */4 * * *  cd /opt/jobhunt/src/apps/linkedin-leads && docker compose --profile ondemand run --rm pipeline applypilot run --stages discover  >>/var/log/jobhunt-discover.log 2>&1

# Enrich + score + tailor + cover + pdf every 2h, offset from discover.
30 */2 * * *  cd /opt/jobhunt/src/apps/linkedin-leads && docker compose --profile ondemand run --rm pipeline applypilot run --stages enrich,score,tailor,cover,pdf  >>/var/log/jobhunt-pipeline.log 2>&1

# Cleanup sweep once a day. Deletes tailored resume/cover artifacts past TTL.
15 3 * * *    cd /opt/jobhunt/src/apps/linkedin-leads && docker compose --profile ondemand run --rm pipeline applypilot run --stages cleanup  >>/var/log/jobhunt-cleanup.log 2>&1
```

Adjust cadence to your preference. Log rotation is left to your host's
standard logrotate.

## Apply stage (laptop)

The `apply` stage **does not** run in the pipeline container. Claude
Code CLI + ATS-form Chrome would blow the RAM budget on a small VM
alongside the LinkedIn scraper's browser. Apply-side execution stays
on the laptop; the drainer (see below) polls the server DB for
ready-to-apply rows, runs apply locally, and writes status back.

## Apply drainer (laptop-side Mode B companion)

`JOBHUNT_REMOTE_SSH_HOST` is required — there's no hardcoded default
alias in the drainer. Export it or put it in `~/.applypilot/.env`:

```
export JOBHUNT_REMOTE_SSH_HOST=<your-ssh-alias>
applypilot drainer --poll-interval 60 --per-hour-cap 20 --min-score 7
```

Or as a systemd user unit that auto-starts on login:

```ini
# ~/.config/systemd/user/applypilot-drainer.service
[Unit]
Description=ApplyPilot Mode-B apply drainer
After=network-online.target

[Service]
Type=simple
Environment=APPLYPILOT_BACKEND=remote
Environment=JOBHUNT_REMOTE_SSH_HOST=<your-ssh-alias>
ExecStart=%h/.pyenv/shims/applypilot drainer --poll-interval 60 --per-hour-cap 20
Restart=on-failure
RestartSec=30s

[Install]
WantedBy=default.target
```

Then:

```
systemctl --user daemon-reload
systemctl --user enable --now applypilot-drainer.service
journalctl --user -u applypilot-drainer.service -f
```

What the drainer does each poll tick (happy path):

1. `ssh <alias> "sqlite3 /opt/jobhunt/data/jobhunt.db ..."` — atomic
   `UPDATE ... RETURNING` sets the highest-fit ready-to-apply row to
   `apply_status='in_progress', agent_id='drainer-<hostname>'` and
   hands it back. No claim contention: the transaction is SQLite-side.
2. rsync the claimed row's tailored-resume + cover-letter PDFs from
   `/opt/jobhunt/data/tailored_resumes/` / `.../cover_letters/` into a
   laptop-local temp dir.
3. Subprocess: `applypilot apply --url <url> --limit 1`. This runs the
   existing apply code against the laptop's Chrome + Claude Code CLI,
   writing its status to `~/.applypilot/applypilot.db` as usual.
4. Read the status from the laptop DB, SSH a second UPDATE to set the
   same status on the server row. Clear `agent_id`.

On SIGINT / SIGTERM mid-apply, the drainer releases any unfinished
claim (`apply_status → NULL`) so the next run can retry.

## Rollback

On the laptop, edit `~/.applypilot/.env`:

```
APPLYPILOT_BACKEND=laptop
# (or just delete the line — default is laptop)
```

Disable the server cron:

```
crontab -e    # remove or comment out the BAP-pipeline lines
```

Laptop auto-sync resumes immediately. Server DB stays as the backup
mirror, exactly the Mode A state. No data loss; the two modes share
the same on-disk schema and path.

## Memory headroom (observed)

Live observation inside the pipeline container during a full `run`:

- Idle (between `docker compose run` invocations): **0 MB** (no container).
- Python + imports loaded: ~180 MB.
- Chromium launch (enrich / smartextract / pdf): peak ~700 MB.
- `mem_limit: 1g` in compose caps a runaway stage. Combined with a
  swap file, OOM risk is contained on 4 GB-class VMs.

# Mode B — server-authoritative pipeline deploy

Phase 6 of the job-hunt unification. Reference for flipping from Mode A
(laptop writes, auto-pushes to server) to Mode B (server cron owns the
pipeline, laptop only triggers apply).

## Checklist before flipping

- [ ] Hetzner has `/opt/jobhunt/data/{jobhunt.db,entities,backups}` seeded
  via the Phase-5 rsync push.
- [ ] Hetzner has 4 GB swap file (`free -h` should show Swap: 4.0Gi).
- [ ] `/opt/linkedin-leads/.env` on the server contains the BAP keys:
  `GEMINI_API_KEY`, `LLM_MODEL`, optionally `ANTHROPIC_API_KEY` for
  tailor routing.
- [ ] Pipeline image built: `docker compose --profile ondemand build pipeline`.
- [ ] Doctor passes inside the container:
  `docker compose --profile ondemand run --rm pipeline applypilot doctor`.

## Flip laptop to Mode B

Edit `~/.applypilot/.env`:

```
APPLYPILOT_BACKEND=hetzner
```

The laptop-side remote-sync auto-hook now silently skips (`jobhunt_core.
sync_remote.push_checkpoint` exits early with a "server is authoritative"
reason). Existing `applypilot run` invocations on the laptop will still
execute locally — if you want them to go to the server instead, SSH and
run via docker compose:

```
ssh hetzner 'cd /opt/linkedin-leads && docker compose --profile ondemand \
    run --rm pipeline applypilot run --stages <stage>'
```

## Host cron on Hetzner

Template (not auto-installed — copy into `crontab -e` as root when ready):

```cron
# BAP pipeline stages, Hetzner-authoritative mode.
# Env loaded from /opt/linkedin-leads/.env via docker compose.

# Discover every 4h. Longest-running stage.
 0 */4 * * *  cd /opt/linkedin-leads && docker compose --profile ondemand run --rm pipeline applypilot run --stages discover  >>/var/log/jobhunt-discover.log 2>&1

# Enrich + score + tailor + cover + pdf every 2h, offset from discover.
30 */2 * * *  cd /opt/linkedin-leads && docker compose --profile ondemand run --rm pipeline applypilot run --stages enrich,score,tailor,cover,pdf  >>/var/log/jobhunt-pipeline.log 2>&1

# Cleanup sweep once a day. Deletes tailored resume/cover artifacts past TTL.
15 3 * * *    cd /opt/linkedin-leads && docker compose --profile ondemand run --rm pipeline applypilot run --stages cleanup  >>/var/log/jobhunt-cleanup.log 2>&1
```

Adjust cadence to your preference. Cron output log rotation is left to
the host's standard logrotate.

## Apply stage (laptop)

The `apply` stage **does not** run in the pipeline container. Claude
Code CLI + ATS-form Chrome would blow the RAM budget on the 3.7 GB
Hetzner VM. Apply-side execution stays on the laptop; a drainer
(JH-PH7) polls the server DB for `apply_status='queued'` rows, runs
apply locally, and writes status back.

## Rollback

Two lines in `~/.applypilot/.env` on the laptop:

```
APPLYPILOT_BACKEND=laptop
# (or just delete the hetzner line — default is laptop)
```

Disable the server cron:

```
crontab -e    # remove or comment out the BAP-pipeline lines
```

Laptop auto-sync resumes immediately. Server DB stays as the backup
mirror exactly like Phase 5. No data loss; the two modes share the same
on-disk schema and path.

## Memory headroom

Live observation inside the pipeline container during a full `run`:

- Idle (between `docker compose run` invocations): **0 MB** (no container)
- Python + imports loaded: ~180 MB
- Chromium launch (enrich / smartextract / pdf): peak ~700 MB
- `mem_limit: 1g` in compose caps a runaway stage below linkedin-leads
  listener's 251 MB. Combined with 4 GB swap, OOM risk is contained.

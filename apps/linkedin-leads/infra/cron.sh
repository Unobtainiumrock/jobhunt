#!/usr/bin/env bash
# Belt-and-suspenders scheduler for the Hetzner deploy.
#
# The `listener` container already auto-runs the pipeline when new messages
# arrive over the LinkedIn socket stream. This script is the periodic safety
# net: it re-scrapes the inbox and re-runs the pipeline on a clock, so a
# stalled socket or a missed event can never leave drafts stale for more than
# a couple of hours.
#
# Intended to be invoked from the host crontab (see ttttt.md §Cutover) with
# one of these subcommands:
#
#     infra/cron.sh scrape     # re-scrape the inbox (every 4 h)
#     infra/cron.sh pipeline   # classify/embed/score/generate (every 6 h)
#     infra/cron.sh health     # one-shot health sweep (every 15 min)
#
# The script assumes it lives in the checked-out repo on the Hetzner host,
# and that `docker compose` can reach services. All commands exec inside the
# relevant container so we do not need host-level Python/Node.

set -euo pipefail

# Hardcoded hostname for alert messages (OS hostname is misspelled "inkedin-leads").
HOSTNAME_LABEL="linkedin-leads"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."

LOG_DIR="${CRON_LOG_DIR:-/var/log/linkedin-leads}"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
    # Fallback when running as a non-privileged user (e.g. first-time
    # local test on a dev box). Keep logs inside the repo.
    LOG_DIR="$HERE/../.cron-logs"
    mkdir -p "$LOG_DIR"
fi

log() {
    printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

alert() {
    # Best-effort Telegram alert via the already-built notify module.
    docker compose exec -T review python infra/notify.py --message "$1" --channel telegram || true
}

run_scrape() {
    log "cron scrape: starting"
    if ! docker compose exec -T listener npm run inbox 2>&1 | tee -a "$LOG_DIR/scrape.log"; then
        log "cron scrape: FAILED"
        alert "cron scrape failed on $HOSTNAME_LABEL. Check $LOG_DIR/scrape.log"
        return 1
    fi
    log "cron scrape: done"
}

run_pipeline() {
    log "cron pipeline: starting"
    # Pipeline is idempotent; running against an unchanged inbox is a no-op.
    if ! docker compose exec -T listener npm run pipeline 2>&1 | tee -a "$LOG_DIR/pipeline.log"; then
        log "cron pipeline: FAILED"
        alert "cron pipeline failed on $HOSTNAME_LABEL. Check $LOG_DIR/pipeline.log"
        return 1
    fi
    # Also purge drafts for threads the user has replied to manually since
    # last run. Safe to run even when there's nothing stale.
    docker compose exec -T listener python -m pipeline.generate_reply --purge-stale \
        2>&1 | tee -a "$LOG_DIR/pipeline.log" || true
    # Phase out applications stuck in 'drafting' for >21d so the workflow
    # kanban does not accumulate dead cards. Idempotent.
    docker compose exec -T listener python -m pipeline.entity_workflow auto-withdraw-stale --days 21 \
        2>&1 | tee -a "$LOG_DIR/pipeline.log" || true
    log "cron pipeline: done"
}

run_health() {
    log "cron health: sweep"
    docker compose exec -T healthdog python infra/healthcheck.py 2>&1 \
        | tee -a "$LOG_DIR/health.log" || true
}

write_heartbeat() {
    # Touch a heartbeat sidecar inside the listener's app-data volume
    # (healthdog reads it RO) so healthdog can detect cron jobs that
    # never fire. Runs on success OR failure of the subcommand.
    local name="$1"
    docker compose exec -T listener touch "/app/data/.cron.${name}.heartbeat" \
        2>/dev/null || true
}

case "${1:-}" in
    scrape)
        trap 'write_heartbeat scrape' EXIT
        run_scrape
        ;;
    pipeline)
        trap 'write_heartbeat pipeline' EXIT
        run_pipeline
        ;;
    health)
        trap 'write_heartbeat health' EXIT
        run_health
        ;;
    all)
        trap 'write_heartbeat scrape; write_heartbeat pipeline; write_heartbeat health' EXIT
        run_scrape || true
        run_pipeline || true
        run_health || true
        ;;
    *)
        cat <<EOF
Usage: $0 {scrape|pipeline|health|all}

Suggested crontab (as deploy user on the Hetzner host):

  # scrape every 4 hours
  0  */4 * * * /home/deploy/linkedin-leads/infra/cron.sh scrape
  # full pipeline every 6 hours (offset by 10 min to avoid overlap with scrape)
  10 */6 * * * /home/deploy/linkedin-leads/infra/cron.sh pipeline
  # health sweep every 15 minutes
  */15 *   * * * /home/deploy/linkedin-leads/infra/cron.sh health
EOF
        exit 2
        ;;
esac

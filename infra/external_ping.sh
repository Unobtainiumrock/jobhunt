#!/usr/bin/env bash
# External reachability ping — intended to run on the laptop, NOT Hetzner.
#
# Healthdog lives inside the Hetzner VPS and can't page us when the whole
# VPS goes down. This script runs every ~5 min from a systemd user timer
# (see infra/systemd/linkedin-leads-external-ping.{service,timer}) and
# fires a single Telegram DM when the public review URL stops responding.
#
# Rate-limited: one DM per EXTERNAL_PING_COOLDOWN_SEC window (default 30m)
# while the host is unreachable, so a long outage doesn't spam the chat.
# Sends a single "recovered" DM the first time the URL responds after a
# down streak.

set -uo pipefail

URL="${EXTERNAL_PING_URL:-https://m.178-104-92-205.sslip.io/m/}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/linkedin-leads"
STATE_FILE="$STATE_DIR/external_ping.state"
COOLDOWN="${EXTERNAL_PING_COOLDOWN_SEC:-1800}"

mkdir -p "$STATE_DIR"

notify() {
    python3 "$REPO_ROOT/infra/notify.py" \
        --message "$1" --channel telegram 2>/dev/null || true
}

read_state() {
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
    else
        echo "up 0"
    fi
}

write_state() {
    printf '%s %s\n' "$1" "$2" > "$STATE_FILE"
}

read -r last_status last_alert_ts < <(read_state)
now_ts=$(date +%s)

if curl -sf --connect-timeout 10 --max-time 15 "$URL" >/dev/null; then
    # URL responded. If we were previously down, announce recovery once.
    if [[ "$last_status" == "down" ]]; then
        notify "✅ linkedin-leads reachable again from laptop: $URL"
    fi
    write_state "up" "$now_ts"
    exit 0
fi

# URL unreachable. Rate-limit the alert.
if [[ "$last_status" == "up" ]] || (( now_ts - last_alert_ts >= COOLDOWN )); then
    notify "🚨 linkedin-leads unreachable from laptop: $URL (VPS or network likely down)"
    write_state "down" "$now_ts"
else
    # Still down, within cooldown — just update status without alerting.
    write_state "down" "$last_alert_ts"
fi
exit 1

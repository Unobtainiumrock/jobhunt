#!/usr/bin/env bash
# Server install: inbound linkedin-leads docker stack with VNC desktop.
# Provider-agnostic — works on any Linux VPS that meets the resource bar.
# Run on the SERVER as root. Friend brings their own VPS; this script does
# NOT provision the VPS itself.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

DATA_DIR="${JOBHUNT_DATA_DIR:-/opt/jobhunt/data}"
COMPOSE_DIR="$REPO_ROOT/apps/linkedin-leads"
ENV_FILE="$COMPOSE_DIR/.env"

c_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
c_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

c_bold "==> jobhunt / linkedin-leads — server install"
echo

# ─── Must be root ──────────────────────────────────────────────────────────
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    c_red "Run as root: sudo $0"
    exit 1
fi

# ─── Distro check ──────────────────────────────────────────────────────────
c_bold "==> Distro"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|debian) echo "  ✓ $PRETTY_NAME";;
        *) c_yellow "  ⚠ untested distro: $PRETTY_NAME (continuing — should work on any modern Linux)";;
    esac
else
    c_yellow "  ⚠ /etc/os-release missing — can't detect distro"
fi

# ─── Resource preflight ────────────────────────────────────────────────────
c_bold "==> Resources"
fail=0

ram_gb=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)
ram_gb_int=$(awk '/MemTotal/ {print int($2/1024/1024)}' /proc/meminfo)
if [ "$ram_gb_int" -lt 4 ]; then
    c_red "  ✗ RAM: ${ram_gb} GB — need >= 4 GB"
    c_red "    Resize your VPS plan to >= 4 GB before continuing."
    fail=1
else
    echo "  ✓ RAM: ${ram_gb} GB"
fi

disk_gb=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')
if [ "${disk_gb:-0}" -lt 30 ]; then
    c_red "  ✗ Free disk on /: ${disk_gb} GB — need >= 30 GB"
    fail=1
else
    echo "  ✓ Free disk on /: ${disk_gb} GB"
fi

swap_kb=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
swap_gb_int=$((swap_kb / 1024 / 1024))
if [ "$swap_gb_int" -lt 2 ]; then
    c_yellow "  ⚠ Swap: ${swap_gb_int} GB — need >= 2 GB (Chromium OOMs without)"
    read -r -p "    Auto-create /swapfile (2 GB)? [Y/n] " ans
    if [ "${ans:-Y}" != "n" ] && [ "${ans:-Y}" != "N" ]; then
        if ! [ -f /swapfile ]; then
            fallocate -l 2G /swapfile
            chmod 600 /swapfile
            mkswap /swapfile >/dev/null
            swapon /swapfile
            grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
            c_green "    ✓ /swapfile created, enabled, persisted in /etc/fstab"
        else
            c_yellow "    /swapfile exists but not active — try: swapon /swapfile"
        fi
    else
        c_yellow "    Skipped. Re-run later if Chrome OOMs."
    fi
else
    echo "  ✓ Swap: ${swap_gb_int} GB"
fi

# ─── Tooling ───────────────────────────────────────────────────────────────
c_bold "==> Tooling"
need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        c_red "  ✗ missing: $1"
        echo "    install: $2"
        fail=1
    else
        echo "  ✓ $1"
    fi
}
need git "apt install -y git"
need docker "https://docs.docker.com/engine/install/ubuntu/"

if command -v docker >/dev/null 2>&1; then
    if ! docker compose version >/dev/null 2>&1; then
        c_red "  ✗ docker compose v2 plugin missing"
        echo "    install: apt install -y docker-compose-plugin"
        fail=1
    else
        echo "  ✓ docker compose ($(docker compose version --short))"
    fi
fi

if [ "$fail" -ne 0 ]; then
    c_red "Resolve the issues above, then re-run."
    exit 1
fi
echo

# ─── Data directories ──────────────────────────────────────────────────────
c_bold "==> Data dirs at $DATA_DIR"
mkdir -p "$DATA_DIR"/{entities,tailored_resumes,cover_letters,backups}
echo "  ✓ created (or exist):"
echo "    $DATA_DIR/{entities,tailored_resumes,cover_letters,backups}"
echo "    (jobhunt.db will be created on first pipeline run)"
echo

# ─── .env setup ────────────────────────────────────────────────────────────
c_bold "==> Environment ($ENV_FILE)"
if [ -f "$ENV_FILE" ]; then
    c_yellow "  $ENV_FILE already exists — leaving untouched."
    echo "    Edit by hand or rm and re-run this script to regenerate."
else
    echo "  Generating $ENV_FILE — answer the prompts (Enter for skip on optional fields)."
    echo

    read -r -p "  OPENAI_API_KEY (required, sk-...): " openai_key
    if [ -z "$openai_key" ]; then
        c_red "  ✗ OPENAI_API_KEY is required for the inbound listener — abort."
        exit 1
    fi
    read -r -p "  GEMINI_API_KEY (required for shared pipeline service): " gemini_key
    read -r -p "  Your LinkedIn display name (e.g. 'Jane Doe'): " li_name
    vnc_pw="$(head -c 12 /dev/urandom | base64 | tr -d '+/=' | head -c 16)"
    echo "  VNC_PASSWORD auto-generated: $vnc_pw"
    echo "    (saved to $ENV_FILE — you'll need this to log into the noVNC desktop)"
    read -r -p "  HEALTH_TELEGRAM_BOT_TOKEN (optional, Enter to skip): " tg_token
    read -r -p "  HEALTH_TELEGRAM_CHAT_ID (optional): " tg_chat

    cat > "$ENV_FILE" <<EOF
# Generated by setup-server.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
OPENAI_API_KEY=$openai_key
GEMINI_API_KEY=$gemini_key
LLM_MODEL=gemini-2.5-flash
LINKEDIN_USER_NAME=$li_name
VNC_PASSWORD=$vnc_pw
LINKEDIN_SEND_ENABLED=0
SENDER_RATE_LIMIT=12
LINKEDIN_SCRAPE_DAYS=7
GMAIL_HEADLESS_SKIP=1
HEALTH_TELEGRAM_BOT_TOKEN=$tg_token
HEALTH_TELEGRAM_CHAT_ID=$tg_chat
EOF
    chmod 600 "$ENV_FILE"
    c_green "  ✓ .env written, mode 600"
fi
echo

# ─── Build & start stack ───────────────────────────────────────────────────
c_bold "==> Building & starting docker stack (this can take 5-10 min on first run)"
cd "$COMPOSE_DIR"
docker compose up -d --build
echo
docker compose ps

# ─── Host crons ────────────────────────────────────────────────────────────
c_bold "==> Host crons"
CRON_MARKER="# JOBHUNT_LINKEDIN_LEADS"
if crontab -l 2>/dev/null | grep -q "$CRON_MARKER"; then
    echo "  ✓ crons already installed (marker '$CRON_MARKER' found)"
else
    (crontab -l 2>/dev/null; cat <<EOF
$CRON_MARKER
0  */4 * * * $COMPOSE_DIR/infra/cron.sh scrape   >> /var/log/linkedin-leads-scrape.log   2>&1
10 */6 * * * $COMPOSE_DIR/infra/cron.sh pipeline >> /var/log/linkedin-leads-pipeline.log 2>&1
*/15 * * * * $COMPOSE_DIR/infra/cron.sh health   >> /var/log/linkedin-leads-health.log   2>&1
30 */2 * * * docker exec linkedin-leads-listener-1 python -m pipeline.email_ingest >> /var/log/linkedin-leads-email.log 2>&1
0  9   * * * docker restart linkedin-leads-desktop-1 >> /var/log/linkedin-leads-chrome-restart.log 2>&1
EOF
) | crontab -
    c_green "  ✓ installed 5 cron entries"
fi
echo

# ─── Done ──────────────────────────────────────────────────────────────────
c_green "==> Server install complete."
ip_hint=$(curl -fsS --max-time 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
cat <<EOF

Containers running. Verify with:  docker compose -f $COMPOSE_DIR/docker-compose.yml ps

Next manual steps (from your laptop):

  1. SSH-tunnel into the VNC desktop and log into LinkedIn (one-time):
       ssh -L 6080:127.0.0.1:6080 root@$ip_hint
     then open http://localhost:6080  (VNC password is in $ENV_FILE)
     → in the noVNC Chrome, log into linkedin.com normally. Cookies persist.

  2. SSH-tunnel into the review UI:
       ssh -L 3457:127.0.0.1:3457 root@$ip_hint
     then open http://localhost:3457

  3. (Optional) Public TLS via Caddy on ports 80/443:
       point a domain at $ip_hint, edit $COMPOSE_DIR/infra/Caddyfile

  4. (Optional) Mode B drainer — link laptop apply to this server's DB:
       see $REPO_ROOT/apps/applypilot/infra/MODE_B_DEPLOY.md

  5. After verifying drafts look good, flip live sends ON:
       sed -i 's/^LINKEDIN_SEND_ENABLED=0/LINKEDIN_SEND_ENABLED=1/' $ENV_FILE
       cd $COMPOSE_DIR && docker compose restart review

Logs: /var/log/linkedin-leads-*.log
EOF

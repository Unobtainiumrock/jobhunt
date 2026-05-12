Good question. Let me break this down concretely.

## Where it runs

A single Linux VPS (Virtual Private Server). One box, everything on it. No Kubernetes, no managed services, no multi-region complexity.

## Cost breakdown

| Item | Provider options | Monthly cost |
|------|-----------------|-------------|
| VPS (2 vCPU, 4GB RAM, 40GB SSD) | Hetzner CX22 | **$4.35/mo** |
| | DigitalOcean Basic | $12/mo |
| | Vultr Cloud Compute | $12/mo |
| | AWS Lightsail | $12/mo |
| | Oracle Cloud (Ampere free tier) | **$0** |
| Domain (optional) | Namecheap, Cloudflare | ~$1/mo ($10-12/yr) |
| OpenAI API usage | Depends on message volume | ~$2-5/mo |
| Telegram bot | Free | $0 |
| **Total (Hetzner)** | | **~$7-10/mo** |
| **Total (Oracle free tier)** | | **~$2-5/mo** |

Hetzner is the best bang for buck. Oracle's free tier ARM instance (4 OCPU, 24GB RAM) is absurdly generous if you can get one allocated -- they're often waitlisted.

## Granular steps

### Step 0: Get a Telegram bot (do this first, takes 2 minutes)

1. Open Telegram, message `@BotFather`
2. Send `/newbot`, name it something like "LinkedIn Leads Health"
3. Copy the bot token it gives you (looks like `7123456789:AAH...`)
4. Message `@userinfobot` in Telegram, copy your numeric chat ID
5. Save both for Step 4

### Step 1: Provision the server

**Using Hetzner (recommended):**

1. Go to https://console.hetzner.cloud
2. Create account, add payment method
3. New Project → "linkedin-leads"
4. Add Server:
   - Location: Ashburn or Hillsboro (US) or Falkenstein (EU, cheaper)
   - Image: **Ubuntu 24.04**
   - Type: **CX22** (2 vCPU, 4GB RAM, 40GB disk)
   - SSH key: paste your public key (`cat ~/.ssh/id_ed25519.pub`)
   - No extras needed
5. Create. Note the IP address.

### Step 2: Secure the server

```bash
# From your local machine
ssh root@<SERVER_IP>

# Create a non-root user
adduser deploy
usermod -aG sudo deploy

# Set up firewall
ufw allow OpenSSH
ufw allow 6080/tcp   # noVNC
ufw allow 3457/tcp   # review UI
ufw allow 443/tcp    # HTTPS (if using Caddy)
ufw enable

# Disable root SSH login
sed -i 's/PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart sshd

# Switch to deploy user for everything else
su - deploy
```

### Step 3: Install Docker

```bash
# Install Docker Engine
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Log out and back in for group to take effect
exit
ssh deploy@<SERVER_IP>

# Verify
docker --version
docker compose version
```

### Step 4: Clone and configure

```bash
# Clone your repo (private). linkedin-leads now lives inside the jobhunt monorepo.
git clone git@github.com:Unobtainiumrock/jobhunt.git
cd jobhunt/apps/linkedin-leads

# Create your .env from the template
cp .env.example .env
nano .env
```

Fill in these values in `.env`:

```
OPENAI_API_KEY=sk-...

VNC_PASSWORD=<pick something strong>

HEALTH_TELEGRAM_BOT_TOKEN=7123456789:AAH...
HEALTH_TELEGRAM_CHAT_ID=123456789

# Live sends: default in docker-compose is 1. Set =0 only to force dry-run.
LINKEDIN_SEND_ENABLED=1
# Soft cap on outbound sends per hour (translated into per-run delays).
SENDER_RATE_LIMIT=12
```

### Step 5: Launch

```bash
docker compose up -d
```

This builds and starts all 5 services. First build takes 3-5 minutes (downloading Chrome, Python deps, etc). Subsequent starts are instant.

```bash
# Watch the build/startup
docker compose logs -f

# Check everything came up
docker compose ps
```

### Step 6: Log into LinkedIn via VNC

1. Open your browser: `http://<SERVER_IP>:6080`
2. Enter your VNC password
3. You'll see an Xfce desktop with Chrome open to linkedin.com
4. Log into LinkedIn normally (2FA, the works)
5. Leave this tab alone. The listener connects via CDP to this Chrome instance.

### Step 7: Verify everything is working

```bash
# Check health from the server
docker compose exec healthdog python infra/healthcheck.py

# Expected output:
# Health Check [2026-03-12 20:15 UTC]
#   [OK] cdp: 3 page tabs open
#   [OK] linkedin: 1 LinkedIn tabs active
#   [OK] qdrant: Collections: linkedin_messages, user_profile
#   [OK] listener: Running (PIDs: 42)
```

Open `http://<SERVER_IP>:3457` to see the review UI.

### Step 8 (optional): Add HTTPS with a domain

If you want `review.yourdomain.com` and `vnc.yourdomain.com` instead of raw IPs:

```bash
# Install Caddy on the host (not in Docker)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# Point DNS: review.yourdomain.com → SERVER_IP
#            vnc.yourdomain.com    → SERVER_IP

# Generate a password hash
caddy hash-password --plaintext '<your-password>'

# Edit the Caddyfile with your domain and hash
export DOMAIN=yourdomain.com
export BASIC_AUTH_HASH='<the hash from above>'
export BASIC_AUTH_USER=admin
sudo caddy run --config /home/deploy/linkedin-leads/infra/Caddyfile
```

Caddy auto-provisions Let's Encrypt TLS certificates. No manual cert management.

### Step 9: Cutover — run on cron, stop local pipeline runs

Once the stack is healthy and you've approved a few drafts over VNC/UI to
confirm the sender works, flip the repo into "cloud is authoritative" mode.

On the Hetzner host (`deploy` user), install the cron entries printed by
`infra/cron.sh`:

```bash
cd ~/linkedin-leads
./infra/cron.sh   # prints suggested crontab
crontab -e        # paste the three lines
```

Then:

```bash
# Live sends default to on; only run this if you had set the gate to 0:
# sed -i 's/^LINKEDIN_SEND_ENABLED=.*/LINKEDIN_SEND_ENABLED=1/' .env
docker compose up -d
```

Stop doing these on your laptop:

- `npm run inbox`
- `npm run pipeline`
- `python -m pipeline.followup_scheduler`
- `node src/send-approved.mjs --live`

Your laptop's only remaining roles:

1. Open `http://<SERVER_IP>:3457` (or Caddy domain) to review drafts.
2. Telegram on your phone: `/status`, `/list`, `/approve <token>`,
   `/reject <token>` via the bot running in the `telegram_bot` service.

Everything else — scrape, classify, score, embed, generate, purge-stale,
follow-up scheduling, sending — runs on the server, on the cron clock, with
failure alerts piped straight to Telegram.

## Day-to-day operation

| Situation | What to do |
|-----------|-----------|
| New messages come in | Nothing. The listener auto-runs the pipeline. |
| Review reply drafts | Open `http://<SERVER_IP>:3457` (or `review.yourdomain.com`) |
| LinkedIn session expired | You get a Telegram alert. Open VNC, re-login. |
| Server rebooted | `docker compose up -d`. Chrome session persists (volume-mounted). |
| Update the code | `git pull && docker compose up -d --build` |
| Check logs | `docker compose logs -f listener` (or any service name) |
| Manual pipeline run | `docker compose exec listener npm run pipeline` |
| Approve a draft from your phone | Telegram the bot: `/list` then `/approve <token>` |
| Check status from your phone | Telegram the bot: `/status` |
| Force-pause all sending | `sed -i 's/^LINKEDIN_SEND_ENABLED=.*/LINKEDIN_SEND_ENABLED=0/' .env && docker compose up -d review telegram_bot` |

## What persists across restarts

All three Docker volumes survive `docker compose down` and `docker compose up`:

- **`chrome-profile`** -- your LinkedIn session cookies, Chrome history
- **`app-data`** -- inbox.json, classified data, entities, reply drafts, CSV exports
- **`qdrant-storage`** -- all vector embeddings

The only thing that doesn't survive: if you `docker compose down -v` (the `-v` flag deletes volumes). Don't do that unless you want a clean slate.
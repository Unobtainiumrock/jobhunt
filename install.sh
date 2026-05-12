#!/usr/bin/env bash
# Laptop install: outbound applypilot CLI (Mode A).
# Run from the repo root. Idempotent — safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

c_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
c_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

c_bold "==> jobhunt / applypilot — laptop install"
echo

# ─── Preflight ─────────────────────────────────────────────────────────────
fail=0
need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        c_red "  ✗ missing: $1 ($2)"
        fail=1
    else
        echo "  ✓ $1"
    fi
}
warn_missing() {
    if ! command -v "$1" >/dev/null 2>&1; then
        c_yellow "  ⚠ missing: $1 ($2)"
    else
        echo "  ✓ $1"
    fi
}

c_bold "Required prereqs:"
need git   "install with: sudo apt install git  /  brew install git"
need python3 "install Python 3.11+: https://www.python.org/downloads/"

if command -v python3 >/dev/null 2>&1; then
    pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    pymajor=$(python3 -c 'import sys; print(sys.version_info.major)')
    pyminor=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$pymajor" -lt 3 ] || { [ "$pymajor" -eq 3 ] && [ "$pyminor" -lt 11 ]; }; then
        c_red "  ✗ python3 is $pyver — need >= 3.11"
        fail=1
    else
        echo "    Python $pyver OK"
    fi
fi

if [ "$fail" -ne 0 ]; then
    c_red "Install one or more required prereqs above, then re-run."
    exit 1
fi

echo
c_bold "Optional prereqs (apply stage — Tier 3):"
warn_missing node   "Node 18+: https://nodejs.org/ (needed for Playwright MCP)"
warn_missing npx    "comes with Node"
warn_missing claude "Claude Code CLI: https://claude.ai/code (drives auto-apply)"
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 || command -v chrome >/dev/null 2>&1; then
    echo "  ✓ Chrome/Chromium"
else
    c_yellow "  ⚠ missing: Chrome/Chromium (auto-apply uses it)"
fi
echo "    (missing optional prereqs only block 'applypilot apply' — you can still run discover/score/tailor)"
echo

# ─── Venv ──────────────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    c_bold "==> Creating venv at .venv/"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo "  ✓ venv active: $(which python)"
python -m pip install --quiet --upgrade pip
echo

# ─── Install ───────────────────────────────────────────────────────────────
c_bold "==> Installing jobhunt_core (editable)"
pip install --quiet -e packages/jobhunt_core

c_bold "==> Installing applypilot (editable, your fork)"
pip install --quiet -e apps/applypilot

c_bold "==> Installing python-jobspy (--no-deps workaround)"
# python-jobspy pins an exact numpy version in metadata that conflicts with
# pip's resolver but works fine at runtime. Install with --no-deps then add
# the actual runtime deps.
pip install --quiet --no-deps python-jobspy
pip install --quiet pydantic tls-client requests markdownify regex

# ─── Verify install came from local clone, not PyPI ────────────────────────
loc=$(pip show applypilot 2>/dev/null | awk -F': ' '/Editable project location/ {print $2}')
if [ "$loc" != "$REPO_ROOT/apps/applypilot" ]; then
    c_red "  ✗ applypilot is NOT installed from this clone."
    c_red "    Editable location reported: '$loc'"
    c_red "    Expected: '$REPO_ROOT/apps/applypilot'"
    c_red "    You may have the upstream PyPI 'applypilot' shadowing this fork."
    c_red "    Fix: pip uninstall -y applypilot && re-run this script."
    exit 1
fi
c_green "  ✓ applypilot installed from your fork: $loc"
echo

# ─── Init wizard (interactive) ─────────────────────────────────────────────
if [ -f "$HOME/.applypilot/profile.json" ] || [ -L "$HOME/.applypilot/profile.yaml" ]; then
    c_yellow "==> ~/.applypilot already exists — skipping 'applypilot init'"
    echo "    (delete or back up ~/.applypilot to re-run the wizard)"
else
    c_bold "==> Running applypilot init (interactive — ~30 prompts)"
    echo "    You'll be asked for: resume file, contact info, work auth, comp, EEO defaults, API keys."
    echo
    applypilot init
fi
echo

# ─── Doctor ────────────────────────────────────────────────────────────────
c_bold "==> Running applypilot doctor"
applypilot doctor || true
echo

# ─── Next steps ────────────────────────────────────────────────────────────
c_green "==> Install complete."
cat <<EOF

Next:
  source .venv/bin/activate         # activate venv in new shells
  applypilot run                    # discover → score → tailor → cover (stages 1-5)
  applypilot apply --dry-run        # fill forms without submitting (test)
  applypilot apply                  # autonomous submit
  applypilot status                 # pipeline counts
  applypilot dashboard              # HTML results view

Edit before re-running:
  ~/.applypilot/searches.yaml       # job queries / locations / score threshold
  ~/.applypilot/profile.yaml        # resume / identity / ATS defaults

Docs:
  $REPO_ROOT/README.md                       # monorepo overview
  $REPO_ROOT/apps/applypilot/README.md       # outbound pipeline details
  $REPO_ROOT/apps/applypilot/infra/MODE_B_DEPLOY.md  # opt-in: server-authoritative pipeline

Inbound side (LinkedIn DM scraper) is OPTIONAL and runs on a VPS, not this laptop.
See setup-server.sh in the repo root if you want it.
EOF

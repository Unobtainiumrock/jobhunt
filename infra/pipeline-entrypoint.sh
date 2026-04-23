#!/bin/bash
# Pipeline container entrypoint.
#
# Installs the bind-mounted source trees as editable packages, then execs
# whatever command was passed (usually `applypilot run --stages ...`). The
# editable install is idempotent — first-run fast, repeat-run essentially
# a no-op since pip detects the already-installed distribution.
#
# Fails fast if any required mount is missing: we'd rather see a clear
# error than silently run against a stale snapshot.
set -euo pipefail

for required in /opt/jobhunt-core /app /data; do
    if [ ! -d "$required" ]; then
        echo "entrypoint: required mount $required is missing" >&2
        exit 2
    fi
done

# Editable installs. Quiet on success, loud on failure.
pip install --quiet --no-deps -e /opt/jobhunt-core >/dev/null
pip install --quiet --no-deps -e /app >/dev/null

# Applypilot reads ~/.applypilot/ for its data dir; the compose file
# overrides APPLYPILOT_DIR to /data (mounted from /opt/jobhunt/data on
# the host) so everything lands in the shared volume.
export APPLYPILOT_DIR="${APPLYPILOT_DIR:-/data}"

# Profile lives in linkedin-leads' on-host profile/ dir. The compose file
# mounts it read-only; this env var tells applypilot to read from that
# path rather than hunt in $APPLYPILOT_DIR.
if [ -n "${JOBHUNT_PROFILE_YAML:-}" ] && [ ! -f "$JOBHUNT_PROFILE_YAML" ]; then
    echo "entrypoint: JOBHUNT_PROFILE_YAML=$JOBHUNT_PROFILE_YAML but file missing" >&2
    exit 3
fi

exec "$@"

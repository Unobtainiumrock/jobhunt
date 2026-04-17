#!/bin/bash
set -e

VNC_PASSWORD="${VNC_PASSWORD:-linkedin}"
CDP_PORT="${CDP_PORT:-9222}"

mkdir -p /root/.vnc

if command -v vncpasswd >/dev/null 2>&1; then
  printf '%s\n' "$VNC_PASSWORD" | vncpasswd -f > /root/.vnc/passwd
elif command -v tigervncpasswd >/dev/null 2>&1; then
  printf '%s\n' "$VNC_PASSWORD" | tigervncpasswd -f > /root/.vnc/passwd
else
  echo "No VNC password command found (need vncpasswd/tigervncpasswd)." >&2
  exit 1
fi
chmod 600 /root/.vnc/passwd
echo "VNC password file created"

rm -f /data/chrome-profile/SingletonLock /data/chrome-profile/SingletonSocket /data/chrome-profile/SingletonCookie

cat > /root/.vnc/xstartup <<'XSTARTUP'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec startxfce4
XSTARTUP
chmod +x /root/.vnc/xstartup

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/linkedin.conf

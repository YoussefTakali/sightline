#!/bin/sh
# Headless Linux target: TigerVNC Xvnc (virtual X screen + VNC server in one process),
# a window manager, then the app.
#   RESOLUTION    WxH of the virtual screen (default 1280x800)
#   VNC_PASSWORD  set it unless the port is bound to 127.0.0.1 / an SSH tunnel only
#   NOVNC_PORT    optional: also serve the screen as a web page (noVNC)
set -e
: "${DISPLAY:=:99}" "${RESOLUTION:=1280x800}" "${VNC_PORT:=5900}"
export DISPLAY

if [ -n "$VNC_PASSWORD" ]; then
  printf '%s' "$VNC_PASSWORD" | vncpasswd -f > /tmp/vncpass
  chmod 600 /tmp/vncpass
  AUTH="-SecurityTypes VncAuth -PasswordFile /tmp/vncpass"
else
  AUTH="-SecurityTypes None"
fi
# SendPrimary/SetPrimary=0: only the real CLIPBOARD (Ctrl+C) is synced, not every selection
Xvnc "$DISPLAY" -geometry "$RESOLUTION" -depth 24 -dpi 96 -rfbport "$VNC_PORT" $AUTH \
  -AlwaysShared=1 -SendCutText=1 -AcceptCutText=1 -SendPrimary=0 -SetPrimary=0 \
  -nolisten tcp >/tmp/xvnc.log 2>&1 &
n=0
until [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ]; do
  n=$((n + 1)); [ "$n" -gt 100 ] && { echo "Xvnc did not start" >&2; cat /tmp/xvnc.log >&2; exit 1; }
  sleep 0.1
done
openbox >/dev/null 2>&1 &
# never start the app before the WM handles windows (else its window can stay invisible)
python3 /opt/wait_wm.py 10 2>/dev/null || sleep 2

if [ -n "$NOVNC_PORT" ]; then
  websockify --web /usr/share/novnc "$NOVNC_PORT" "localhost:$VNC_PORT" >/tmp/novnc.log 2>&1 &
fi

exec "$@"

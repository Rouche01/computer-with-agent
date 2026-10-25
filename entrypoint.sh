#!/bin/bash
set -e
Xvfb :1 -screen 0 ${WIDTH}x${HEIGHT}x24 &
sleep 1
dbus-launch startxfce4 &
sleep 2
/opt/start-firefox.sh &
x11vnc -display :1 -forever -shared -nopw -localhost -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 &
exec /opt/venv/bin/uvicorn app:app --app-dir /opt/server --host 0.0.0.0 --port 8000

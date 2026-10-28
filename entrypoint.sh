#!/bin/bash
set -e

# Geometry: prefer ASPECT_RATIO + one edge. Explicit WIDTH+HEIGHT still win if both set
# and ASPECT_RATIO is empty. Xvfb always needs integer pixels.
#
#   ASPECT_RATIO=16:9 WIDTH=1280     → 1280x720
#   ASPECT_RATIO=16:10 WIDTH=1280    → 1280x800  (historical default)
#   WIDTH=1920 HEIGHT=1080           → as given (when ASPECT_RATIO unset)

_even() {
  local n=$1
  if (( n % 2 == 1 )); then
    n=$((n - 1))
  fi
  if (( n < 2 )); then
    n=2
  fi
  echo "$n"
}

resolve_geometry() {
  local ar="${ASPECT_RATIO:-}"
  local w="${WIDTH:-}"
  local h="${HEIGHT:-}"

  if [[ -n "$ar" ]]; then
    local aw ah
    ar="${ar//\//:}"
    IFS=':' read -r aw ah <<<"$ar"
    if ! [[ "$aw" =~ ^[0-9]+$ && "$ah" =~ ^[0-9]+$ && "$aw" -gt 0 && "$ah" -gt 0 ]]; then
      echo "entrypoint: invalid ASPECT_RATIO='$ASPECT_RATIO' (use e.g. 16:9)" >&2
      exit 1
    fi
    if [[ -n "$w" && -z "$h" ]]; then
      h=$(echo "$w $ah $aw" | awk '{ printf "%d", ($1 * $2) / $3 }')
      h=$(_even "$h")
    elif [[ -n "$h" && -z "$w" ]]; then
      w=$(echo "$h $aw $ah" | awk '{ printf "%d", ($1 * $2) / $3 }')
      w=$(_even "$w")
    elif [[ -n "$w" && -n "$h" ]]; then
      # Both set with aspect: keep WIDTH, recompute HEIGHT from aspect
      h=$(echo "$w $ah $aw" | awk '{ printf "%d", ($1 * $2) / $3 }')
      h=$(_even "$h")
    else
      w=1280
      h=$(echo "$w $ah $aw" | awk '{ printf "%d", ($1 * $2) / $3 }')
      h=$(_even "$h")
    fi
  else
    w="${w:-1280}"
    h="${h:-800}"
  fi

  WIDTH=$(_even "$w")
  HEIGHT=$(_even "$h")
  export WIDTH HEIGHT
  echo "entrypoint: display ${WIDTH}x${HEIGHT}" \
    "${ASPECT_RATIO:+(ASPECT_RATIO=$ASPECT_RATIO)}"
}

port_open() {
  (echo >/dev/tcp/127.0.0.1/"$1") >/dev/null 2>&1
}

wait_for_x_socket() {
  local display_num="${1#:}"
  display_num="${display_num%%.*}"
  local sock="/tmp/.X11-unix/X${display_num}"
  local i
  for i in $(seq 1 60); do
    if [[ -S "$sock" ]]; then
      echo "entrypoint: X socket ready ($sock)"
      return 0
    fi
    sleep 0.5
  done
  echo "entrypoint: X socket missing after wait: $sock" >&2
  return 1
}

wait_for_port() {
  local port=$1
  local tries=${2:-60}
  local i
  for i in $(seq 1 "$tries"); do
    if port_open "$port"; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

# Keep x11vnc alive. Websockify accepting WS alone is not enough for noVNC —
# it must be able to open RFB on localhost:5900.
supervise_x11vnc() {
  local attempt=0
  while true; do
    attempt=$((attempt + 1))
    if ! wait_for_x_socket "$DISPLAY"; then
      echo "entrypoint: display gone; waiting before x11vnc restart" >&2
      sleep 2
      continue
    fi
    echo "entrypoint: starting x11vnc on ${DISPLAY} → :5900 (attempt ${attempt})"
    # -forever: stay across client disconnects; outer loop covers process exit
    # -noxdamage: fewer crashes under XFCE/compositing on slow cold boots
    x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -rfbport 5900 \
      -noxdamage -o /tmp/x11vnc.log || true
    echo "entrypoint: x11vnc exited (see /tmp/x11vnc.log); retry in 2s" >&2
    sleep 2
  done
}

supervise_websockify() {
  while true; do
    echo "entrypoint: starting websockify :6080 → localhost:5900"
    websockify --web /usr/share/novnc 6080 localhost:5900 || true
    echo "entrypoint: websockify exited; retry in 2s" >&2
    sleep 2
  done
}

resolve_geometry
export DISPLAY="${DISPLAY:-:1}"
DISPLAY_NUM="${DISPLAY#:}"
DISPLAY_NUM="${DISPLAY_NUM%%.*}"

# Cold boot (post-reboot) is slow — give Xvfb real time before clients attach.
Xvfb ":${DISPLAY_NUM}" -screen 0 "${WIDTH}x${HEIGHT}x24" &
if ! wait_for_x_socket "$DISPLAY"; then
  echo "entrypoint: fatal — Xvfb failed to create display socket" >&2
  exit 1
fi
# Extra grace: CPU contention after machine reboot
sleep "${BOOT_GRACE_SEC:-3}"

dbus-launch startxfce4 &
sleep 2
/opt/start-firefox.sh &

supervise_x11vnc &

# Do not expose noVNC until RFB is actually listening. If we never get :5900,
# exit so Docker restart:unless-stopped can retry (common after host reboot).
if wait_for_port 5900 90; then
  echo "entrypoint: x11vnc listening on :5900"
else
  echo "entrypoint: fatal — :5900 never opened (x11vnc); forcing container restart" >&2
  tail -50 /tmp/x11vnc.log 2>/dev/null || true
  exit 1
fi

supervise_websockify &
exec /opt/venv/bin/uvicorn app:app --app-dir /opt/server --host 0.0.0.0 --port 8000

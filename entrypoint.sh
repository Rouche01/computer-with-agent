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

resolve_geometry

Xvfb :1 -screen 0 ${WIDTH}x${HEIGHT}x24 &
sleep 1
dbus-launch startxfce4 &
sleep 2
/opt/start-firefox.sh &
x11vnc -display :1 -forever -shared -nopw -localhost -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 &
exec /opt/venv/bin/uvicorn app:app --app-dir /opt/server --host 0.0.0.0 --port 8000

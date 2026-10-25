#!/bin/bash
# Launch Firefox ESR with the dedicated relay-agent profile.
# Callers run this in the background (entrypoint &) or via Popen.
set -euo pipefail

PROFILE="${FIREFOX_PROFILE:-/home/agent/.mozilla/firefox/relay-agent}"

# Clear crash/session restore artifacts so a prior pkill never resurfaces UI.
rm -f \
  "$PROFILE/sessionstore.jsonlz4" \
  "$PROFILE/recovery.jsonlz4" \
  "$PROFILE/.parentlock" \
  2>/dev/null || true
rm -rf "$PROFILE/sessionstore-backups" 2>/dev/null || true

mkdir -p "$PROFILE"
exec firefox-esr -profile "$PROFILE" --no-remote

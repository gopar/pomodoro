#!/usr/bin/env bash
# Example pomo hook.
#
# Install (per machine): copy into the event dir you want and make executable:
#   mkdir -p ~/.config/pomo/hooks/pomodoro_start.d
#   cp hooks/examples/10-notify.sh ~/.config/pomo/hooks/pomodoro_start.d/
#   chmod +x ~/.config/pomo/hooks/pomodoro_start.d/10-notify.sh
#
# Context is available via env vars AND as JSON on stdin.
set -eu

# --- via environment variables -------------------------------------------
minutes=$(( ${POMO_DURATION:-0} / 60 ))
msg="pomo: ${POMO_EVENT} (${POMO_STATE}) on ${POMO_MACHINE} — ${minutes}m"

# --- via JSON on stdin (optional; needs a JSON tool like jq) --------------
# session_json="$(cat)"
# id="$(printf '%s' "$session_json" | jq -r '.id')"

# macOS notification (swap for notify-send on Linux, etc.)
if command -v osascript >/dev/null 2>&1; then
  osascript -e "display notification \"${msg}\" with title \"pomodoro\"" || true
else
  printf '%s\n' "$msg"
fi

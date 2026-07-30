#!/usr/bin/env bash
# pomo hook: announce overtime (spoken) and play an alarm sound.
#
# Replicates the old built-in `say` + `alarm` effects. The same script works
# for pomodoro_end and break_end (message derived from POMO_EVENT).
#
# Install into BOTH event dirs you want it in, e.g.:
#   mkdir -p ~/.config/pomo/hooks/pomodoro_end.d ~/.config/pomo/hooks/break_end.d
#   cp hooks/examples/pomodoro_end.d/10-announce.sh ~/.config/pomo/hooks/pomodoro_end.d/
#   cp hooks/examples/pomodoro_end.d/10-announce.sh ~/.config/pomo/hooks/break_end.d/
#   chmod +x ~/.config/pomo/hooks/*/10-announce.sh
set -eu

case "${POMO_EVENT:-}" in
  break_end) msg="Break Overtime" ;;
  *)         msg="Pomodoro Overtime" ;;
esac

ALARM="${HOME}/.config/media/alarm.mp3"

case "$(uname -s)" in
  Darwin)
    command -v say >/dev/null 2>&1 && say "$msg" || true
    [ -f "$ALARM" ] && command -v afplay >/dev/null 2>&1 && afplay "$ALARM" >/dev/null 2>&1 &
    ;;
  Linux)
    command -v spd-say >/dev/null 2>&1 && spd-say "$msg" || true
    if [ -f "$ALARM" ]; then
      if command -v paplay >/dev/null 2>&1; then
        paplay "$ALARM" >/dev/null 2>&1 &
      elif command -v ffplay >/dev/null 2>&1; then
        ffplay -nodisp -autoexit "$ALARM" >/dev/null 2>&1 &
      fi
    fi
    ;;
  *)
    # Windows: use PowerShell TTS + media player, e.g.
    #   powershell.exe -NoProfile -Command \
    #     "Add-Type -AssemblyName System.Speech; \
    #      (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('$msg')"
    :
    ;;
esac

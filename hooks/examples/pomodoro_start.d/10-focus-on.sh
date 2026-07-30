#!/usr/bin/env bash
# pomo hook: enable Focus/Do-Not-Disturb when a pomodoro starts.
#
# Install (per machine):
#   mkdir -p ~/.config/pomo/hooks/pomodoro_start.d
#   cp hooks/examples/pomodoro_start.d/10-focus-on.sh \
#      ~/.config/pomo/hooks/pomodoro_start.d/
#   chmod +x ~/.config/pomo/hooks/pomodoro_start.d/10-focus-on.sh
set -eu

case "$(uname -s)" in
  Darwin)
    # macOS Shortcuts app: create a "Focus On" shortcut that toggles a Focus.
    command -v shortcuts >/dev/null 2>&1 && shortcuts run "Focus On" || true
    ;;
  Linux)
    # Example: mute notifications via your DE. Swap for your setup.
    # gsettings set org.gnome.desktop.notifications show-banners false || true
    :
    ;;
  *)
    # Windows (Git Bash/WSL): call PowerShell to toggle Focus Assist, e.g.
    #   powershell.exe -NoProfile -Command "<your Focus Assist toggle>"
    :
    ;;
esac

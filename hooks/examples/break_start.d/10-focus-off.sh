#!/usr/bin/env bash
# pomo hook: disable Focus/Do-Not-Disturb when a break starts.
#
# Install: copy into ~/.config/pomo/hooks/break_start.d/ and chmod +x.
set -eu

case "$(uname -s)" in
  Darwin)
    command -v shortcuts >/dev/null 2>&1 && shortcuts run "Focus Off" || true
    ;;
  Linux)
    # gsettings set org.gnome.desktop.notifications show-banners true || true
    :
    ;;
  *)
    :
    ;;
esac

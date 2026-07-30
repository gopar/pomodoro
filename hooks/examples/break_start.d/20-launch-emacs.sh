#!/usr/bin/env bash
# pomo hook: launch a minimal Emacs when a break starts (opt-in).
#
# Replicates the old built-in `launch_emacs` behavior. NOTE: with the hook
# model this fires on break_start (and, if you also install it under
# session_stop.d, on every stop) rather than only on a non-break `pomo clear`.
#
# Install: copy into ~/.config/pomo/hooks/break_start.d/ and chmod +x.
set -eu

EMACS_INIT="${HOME}/.config/emacs.d/init.minimal.gui.el"

case "$(uname -s)" in
  Darwin)
    EMACS="/Applications/Emacs.app/Contents/MacOS/Emacs"
    [ -x "$EMACS" ] && "$EMACS" -q -l "$EMACS_INIT" >/dev/null 2>&1 &
    ;;
  Linux)
    command -v emacs >/dev/null 2>&1 && emacs -q -l "$EMACS_INIT" >/dev/null 2>&1 &
    ;;
  *)
    # Windows: launch runemacs.exe from your install, e.g.
    #   runemacs.exe -q -l "%USERPROFILE%\.config\emacs.d\init.minimal.gui.el"
    :
    ;;
esac

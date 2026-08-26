#!/usr/bin/env bash
# Install the tray control for a remote worker, on a Linux desktop.
#
# Re-runnable: every step overwrites its own target and nothing else, so running
# this after a `git pull` just updates the installed copies.
#
# Needs, from the distribution rather than from pip, because the tray talks to the
# panel through the system GTK bindings:
#   Debian/Ubuntu: apt install python3-gi gir1.2-ayatanaappindicator3-0.1 libnotify-bin
# On GNOME, the panel needs the AppIndicator extension (Ubuntu ships it enabled).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -Dm755 "$HERE/tray.py"            "$HOME/.local/share/postflight/tray.py"
install -Dm755 "$HERE/postflight-worker"  "$HOME/.local/bin/postflight-worker"
install -Dm644 "$HERE/postflight-worker.desktop" \
                                          "$HOME/.local/share/applications/postflight-worker.desktop"
for icon in postflight-worker postflight-worker-off; do
  install -Dm644 "$HERE/$icon.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/$icon.svg"
done
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

# The only file that is never overwritten: it holds this machine's identity, and
# clobbering it would silently rename the worker and orphan everything the
# dispatcher had measured about its speed.
if [ ! -f "$HOME/.config/postflight-worker.env" ]; then
  install -Dm600 "$HERE/postflight-worker.env.example" "$HOME/.config/postflight-worker.env"
  echo "Edit ~/.config/postflight-worker.env: set PF_API_URL and PF_WORKER_NAME."
fi
echo "Installed. Launch \"PostFlight worker\" from the app grid, or run: postflight-worker on"

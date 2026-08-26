#!/usr/bin/python3
"""Tray icon for the appoint PostFlight worker on this machine.

Run by /usr/bin/python3 on purpose, not `env python3`: PyGObject comes from the
distribution packages, and `env` picks up the miniforge interpreter first, which
has no `gi` at all. That is exactly how the first version failed.

An indicator rather than a GtkStatusIcon: the legacy tray is deprecated and would
not survive a move to Wayland, while AppIndicator is what the GNOME extension
already speaks. Nothing extra to install, both are packaged.

Everything here is idempotent by construction. The menu only ever calls
`postflight-worker on|off`, which are `docker compose up -d` and `stop`, so a
double click is a no-op rather than a second worker. And the app itself is single
instance: a second launch finds the lock held and exits without a word, so
clicking the launcher again does not stack up icons.

State is polled from `docker inspect` rather than remembered, because the worker
can also be started or stopped from a shell, and an icon that lies is worse than
no icon.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

CONTROL = str(Path.home() / ".local/bin/postflight-worker")
CONTAINER = "postflight-remote-worker-1"
DASHBOARD = "http://192.168.1.104:8080"
LOCK = Path.home() / ".cache/postflight-tray.lock"
LAUNCHER = Path.home() / ".local/share/applications/postflight-worker.desktop"
AUTOSTART = Path.home() / ".config/autostart/postflight-worker.desktop"
POLL_S = 3

# The app's own mark, installed as a user icon theme in
# ~/.local/share/icons/hicolor/scalable/apps. The wave without its background
# square, because a #09090b square is invisible on a dark panel; the off variant is
# the same wave dimmed and struck through, since opacity alone is too subtle at
# 16 px and a different silhouette would stop reading as PostFlight.
ICONS = {"running": "postflight-worker", "off": "postflight-worker-off"}
LABELS = {
    "running": "Worker is on",
    "restarting": "Worker is restarting",
    "created": "Worker is starting",
    "exited": "Worker is off",
    "absent": "Worker is off",
}


def single_instance() -> object | None:
    """Hold a lock for as long as we live, or tell the caller to give up.

    The handle is returned and kept, not closed: closing it would release the lock
    and let a second icon appear.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return handle


def container_state() -> str:
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else "absent"
    except (OSError, subprocess.TimeoutExpired):
        return "absent"


class Tray:
    def __init__(self) -> None:
        self.state = "absent"
        # Shown while a command is in flight, because `off` waits out the 60 s grace
        # period the worker needs to stop its ffmpeg cleanly.
        self.busy = ""
        self.quitting = False
        self.job: subprocess.Popen | None = None

        self.indicator = AppIndicator.Indicator.new(
            "postflight-worker", ICONS["off"], AppIndicator.IndicatorCategory.SYSTEM_SERVICES
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("PostFlight worker")

        menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label="...")
        self.status_item.set_sensitive(False)
        self.on_item = Gtk.MenuItem(label="Start")
        self.off_item = Gtk.MenuItem(label="Stop")
        self.on_item.connect("activate", lambda _w: self.run("on"))
        self.off_item.connect("activate", lambda _w: self.run("off"))
        # A symlink rather than a copy: one source of truth, so editing the
        # launcher cannot leave a stale autostart entry behind.
        self.autostart_item = Gtk.CheckMenuItem(label="Start at login")
        self.syncing = False
        self.autostart_item.set_active(AUTOSTART.is_symlink() or AUTOSTART.exists())
        self.autostart_item.connect("toggled", self.set_autostart)

        open_item = Gtk.MenuItem(label="Open PostFlight")
        open_item.connect("activate", lambda _w: webbrowser.open(DASHBOARD))
        self.quit_item = Gtk.MenuItem(label="Quit and stop the worker")
        self.quit_item.connect("activate", lambda _w: self.quit())

        for item in (
            self.status_item, Gtk.SeparatorMenuItem(), self.on_item, self.off_item,
            Gtk.SeparatorMenuItem(), self.autostart_item, open_item, self.quit_item,
        ):
            menu.append(item)
        menu.show_all()
        self.indicator.set_menu(menu)

        self.refresh()
        GLib.timeout_add_seconds(POLL_S, self.refresh)

    def run(self, verb: str) -> None:
        if self.job and self.job.poll() is None:
            return  # one command at a time, so a double click cannot race itself
        if verb == "on":
            self.busy = "Starting..."
        else:
            self.busy = "Stopping to quit..." if self.quitting else "Stopping..."
        self.job = subprocess.Popen([CONTROL, verb], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        self.paint()

    def set_autostart(self, item: Gtk.CheckMenuItem) -> None:
        """Presence of the entry is the setting, so this is idempotent either way."""
        if self.syncing:
            return
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        if item.get_active():
            if not AUTOSTART.is_symlink() and not AUTOSTART.exists():
                AUTOSTART.symlink_to(LAUNCHER)
        else:
            AUTOSTART.unlink(missing_ok=True)

    def quit(self) -> None:
        """Stop the worker, then go. Waiting for the stop rather than firing it and
        exiting: it can take up to the 60 s grace period the worker needs to end its
        ffmpeg cleanly, and an icon that vanished first would read as done."""
        if self.state != "running":
            Gtk.main_quit()
            return
        self.quitting = True
        self.run("off")

    def refresh(self) -> bool:
        if self.job and self.job.poll() is not None:
            self.job, self.busy = None, ""
            if self.quitting:
                Gtk.main_quit()
                return False
        self.state = container_state()
        self.paint()
        return True  # keep the timeout alive

    def paint(self) -> None:
        running = self.state == "running"
        self.indicator.set_icon_full(
            ICONS["running"] if running else ICONS["off"], "PostFlight worker"
        )
        self.status_item.set_label(self.busy or LABELS.get(self.state, f"Worker: {self.state}"))
        # Both commands are idempotent, so this is only to make the state readable.
        self.on_item.set_sensitive(not running and not self.busy)
        self.off_item.set_sensitive(running and not self.busy)
        self.quit_item.set_label(
            "Quit and stop the worker" if running else "Quit"
        )
        self.quit_item.set_sensitive(not self.busy)
        wanted = AUTOSTART.is_symlink() or AUTOSTART.exists()
        if wanted != self.autostart_item.get_active():
            # Guarded, or setting it here would fire the handler that writes it.
            self.syncing = True
            self.autostart_item.set_active(wanted)
            self.syncing = False


if __name__ == "__main__":
    lock = single_instance()
    if lock is None:
        sys.exit(0)  # already showing an icon
    os.environ.setdefault("DISPLAY", ":0")
    Tray()
    Gtk.main()

# Tray control for a remote worker

A tray icon on a Linux desktop that lends its CPU and GPU to a dispatcher running
elsewhere: turn the worker on before a session, off when the machine is needed for
something else.

![the two states](postflight-worker.svg)

    ./install.sh
    $EDITOR ~/.config/postflight-worker.env      # PF_API_URL, PF_WORKER_NAME

Then launch **PostFlight worker** from the app grid. The menu carries the state, Start,
Stop, a *Start at login* checkbox, a link to the dispatcher, and Quit.

Without a desktop, the same control works from a shell:

    postflight-worker on | off | toggle | status

## Things worth knowing

**Every verb is idempotent.** They are `docker compose up -d` and `stop` underneath,
which are themselves no-ops when there is nothing to do, and the notification says
what happened (`Started`, `Already running`) rather than what was asked. The tray is
single instance too: a second launch finds the lock held and exits, so clicking the
launcher twice does not stack up icons.

**Turning it off mid-job is safe.** The worker forwards SIGTERM to its ffmpeg or
gyroflow and hands the job back to the queue without spending an attempt, so the
dispatcher gives it to another machine. That is what `stop_grace_period: 60s` is for,
and why `off` can take up to a minute to return. **Quit** stops the worker and waits
for that, rather than exiting while it is still going.

**State is polled, never remembered.** The worker can also be started from a shell or
by `restart: unless-stopped` after a reboot, and an icon that lies is worse than no
icon.

**It runs on `/usr/bin/python3`, not `env python3`.** PyGObject comes from the
distribution packages; a conda or pyenv interpreter earlier in `PATH` has no `gi` at
all, which is exactly how the first version of this failed.

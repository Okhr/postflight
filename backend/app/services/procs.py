"""Running long subprocesses while reporting their progress.

ffmpeg, mp4_merge and gyroflow all write their progress to stdout, but none of
them ends a line the same way: mp4_merge rewrites the same line with `\\r`,
ffmpeg emits `key=value` pairs with `-progress pipe:1`, gyroflow emits
`Rendering progress: N/M frames` lines. So we read in chunks and split on `\\r`
comme sur `\\n`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]
LineParser = Callable[[str], "float | None"]

_SPLIT = re.compile(r"[\r\n]+")

# Every child still running, so a shutdown can ask them to stop instead of having
# them SIGKILLed along with the container.
#
# This is not cosmetic on AMD hardware. Killing ffmpeg outright while VAAPI decode
# jobs are in flight left this machine's amdgpu (AMD's out-of-tree DKMS build)
# waiting on a fence that never signalled: `Trying to push to a killed entity`,
# then every `ttm_bo_delayed_delete` worker stuck in `dma_fence_default_wait`, the
# GPU unusable and the container impossible to remove. Reboot only. ffmpeg given a
# SIGTERM flushes and releases its VAAPI context, and none of that happens.
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()


def terminate_all(grace: float = 20.0) -> int:
    """Ask every running child to stop, and wait for it. Returns how many were hit."""
    with _running_lock:
        children = list(_running)
    for proc in children:
        if proc.poll() is None:
            log.info("shutdown: asking pid %s to stop", proc.pid)
            proc.terminate()
    deadline = time.monotonic() + grace
    for proc in children:
        remaining = max(deadline - time.monotonic(), 0.5)
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log.warning("shutdown: pid %s ignored SIGTERM, killing it", proc.pid)
            proc.kill()
            proc.wait()
    return len(children)


class ProcessError(RuntimeError):
    def __init__(self, message: str, returncode: int | None, log_tail: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.log_tail = log_tail


def run_with_progress(
    cmd: list[str],
    on_line: LineParser | None = None,
    progress_cb: ProgressCallback | None = None,
    timeout: float | None = None,
    tail_size: int = 60,
) -> str:
    """Run `cmd` and return the tail of its log.

    `on_line` returns a 0..1 progress when the line carries one, None otherwise.
    Raises ProcessError if the process exits non-zero or exceeds `timeout`.
    """
    log.debug("exec: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=0,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert proc.stdout is not None
    with _running_lock:
        _running.add(proc)

    tail: deque[str] = deque(maxlen=tail_size)
    started = time.monotonic()
    buffer = ""
    timed_out = False

    def handle(line: str) -> None:
        tail.append(line)
        if on_line is None:
            return
        try:
            value = on_line(line)
        except Exception:  # une ligne inattendue ne doit pas tuer le job
            log.exception("line parsing failed: %r", line)
            return
        if value is not None and progress_cb is not None:
            progress_cb(max(0.0, min(1.0, value)), line)

    try:
        while True:
            chunk = proc.stdout.read(512)
            if not chunk:
                break
            buffer += chunk
            parts = _SPLIT.split(buffer)
            buffer = parts.pop()
            for part in parts:
                if part.strip():
                    handle(part.strip())
            if timeout is not None and time.monotonic() - started > timeout:
                timed_out = True
                break
        if not timed_out and buffer.strip():
            handle(buffer.strip())
    finally:
        if timed_out or proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        returncode = proc.wait()
        with _running_lock:
            _running.discard(proc)
        try:
            proc.stdout.close()
        except OSError:
            pass

    log_tail = "\n".join(tail)
    if timed_out:
        raise ProcessError(f"timed out after {timeout:.0f}s", returncode, log_tail)
    if returncode != 0:
        raise ProcessError(
            f"{os.path.basename(cmd[0])} exited with code {returncode}",
            returncode,
            log_tail,
        )
    return log_tail
